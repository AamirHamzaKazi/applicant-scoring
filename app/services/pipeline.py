"""End-to-end applicant processing pipeline orchestration.

Connects the existing modular services in strict sequence:
  1. Receive applicant data.
  2. Download CV from Google Drive into an isolated temporary directory.
  3. Calculate CV SHA-256 hash.
  4. Check duplicate email and CV hash using DuplicateDetector.
  5. Skip and log duplicates (never inserting or updating in Coda).
  6. Parse CV using CVParser.
  7. If parsing fails, create a Failed Coda record.
  8. Score successfully parsed CVs using ScoringEngine.
  9. Insert results into Coda using CodaClient.
 10. Always clean up temporary downloaded files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from enum import Enum
import hashlib
import logging
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings, get_settings
from app.integrations.coda_client import (
    CodaApplicantRecord,
    CodaApplicantStatus,
    CodaClient,
    normalize_email,
    redact_sensitive_text,
)
from app.integrations.google_sheets_client import (
    GoogleAuthenticationError,
    GoogleServiceAccountAuth,
)
from app.services.cv_parser import CVParseResult, CVParser, ExtractionStatus
from app.services.duplicate_detector import (
    DuplicateCheckResult,
    DuplicateDetector,
    DuplicateReason,
    compute_file_sha256,
)
from app.services.scoring_engine import (
    Recommendation,
    ScoringEngine,
    ScoringResult,
    ScoringStatus,
)

logger = logging.getLogger(__name__)

_EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_DRIVE_FILE_ID_REGEX = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_DOCS_DOC_ID_REGEX = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
_CONTENT_DISPOSITION_FILENAME_REGEX = re.compile(
    r"""filename\*?=(?:UTF-8''|")?([^";\r\n]+)"?""",
    re.IGNORECASE,
)

DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB safety cap

_CONTENT_TYPE_TO_EXT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
}


class PipelineStatus(str, Enum):
    """Terminal pipeline outcome for a single applicant."""

    SCORED = "scored"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    FAILED = "failed"


class ApplicantInput(BaseModel):
    """Validated applicant record entering the processing pipeline."""

    model_config = ConfigDict(extra="ignore")

    applicant_id: str | None = None
    name: str
    email: str
    role: str
    cv_link: str

    @field_validator("name", "role", "cv_link", mode="before")
    @classmethod
    def _strip_required_strings(cls, value: object) -> str:
        if value is None:
            raise ValueError("Field cannot be null.")
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Field cannot be empty.")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def _validate_email(cls, value: object) -> str:
        if value is None:
            raise ValueError("Email cannot be null.")
        cleaned = str(value).strip()
        if not _EMAIL_REGEX.match(cleaned):
            raise ValueError("Invalid email address format.")
        return cleaned

    @field_validator("applicant_id", mode="before")
    @classmethod
    def _normalize_applicant_id(cls, value: object) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned if cleaned else None

    def resolved_applicant_id(self) -> str:
        """Return explicit applicant_id or deterministic non-PII ID derived from normalized email."""
        if self.applicant_id:
            return self.applicant_id
        norm = normalize_email(self.email) or self.email.lower()
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8].upper()
        return f"APP-{digest}"


class DownloadResult(BaseModel):
    """Result of downloading a CV file to a temporary local path."""

    success: bool
    file_path: Path | None = None
    filename: str | None = None
    error_message: str | None = None


class PipelineResult(BaseModel):
    """Structured outcome returned after orchestrating one applicant."""

    applicant_id: str
    status: PipelineStatus
    email: str
    cv_hash: str | None = None
    is_retry: bool = False
    duplicate_reason: DuplicateReason | None = None
    extraction_status: ExtractionStatus | None = None
    total_score: float | None = None
    recommendation: Recommendation | None = None
    coda_status: CodaApplicantStatus | None = None
    coda_inserted: bool = False
    coda_updated: bool = False
    coda_row_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None


def extract_google_drive_file_id(url: str) -> str | None:
    """Extract a Google Drive or Google Docs file ID from common sharing URL formats."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "drive.google.com" not in host and "docs.google.com" not in host:
        return None

    file_match = _DRIVE_FILE_ID_REGEX.search(parsed.path)
    if file_match:
        return file_match.group(1)

    docs_match = _DOCS_DOC_ID_REGEX.search(parsed.path)
    if docs_match:
        return docs_match.group(1)

    query_params = parse_qs(parsed.query)
    if "id" in query_params and query_params["id"]:
        candidate = query_params["id"][0].strip()
        if candidate:
            return candidate

    return None


def resolve_download_url(cv_link: str) -> str:
    """Validate URL scheme and normalize Google Drive / Docs sharing links for direct download."""
    parsed = urlparse(cv_link.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Invalid CV link URL: expected http:// or https:// URL.")

    host = parsed.netloc.lower()
    if "docs.google.com" in host:
        doc_match = _DOCS_DOC_ID_REGEX.search(parsed.path)
        if doc_match:
            doc_id = doc_match.group(1)
            return f"https://docs.google.com/document/d/{doc_id}/export?format=docx"

    if "drive.google.com" in host:
        file_id = extract_google_drive_file_id(cv_link)
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    return cv_link.strip()


def _infer_filename_and_extension(
    response: httpx.Response,
    source_url: str,
    applicant_id: str,
    content: bytes,
) -> str:
    """Determine a safe filename with accurate extension so CVParser identifies format cleanly."""
    disposition = response.headers.get("content-disposition", "")
    if disposition:
        match = _CONTENT_DISPOSITION_FILENAME_REGEX.search(disposition)
        if match:
            raw_name = Path(unquote(match.group(1).strip())).name
            if raw_name and Path(raw_name).suffix:
                return raw_name

    url_path = Path(urlparse(source_url).path)
    if url_path.suffix.lower() in (".docx", ".doc", ".pdf", ".txt", ".png", ".jpg", ".jpeg"):
        return url_path.name

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in _CONTENT_TYPE_TO_EXT:
        return f"{applicant_id}{_CONTENT_TYPE_TO_EXT[content_type]}"

    # Inspect leading magic bytes when headers/URL omit extension
    if content.startswith(b"%PDF-"):
        return f"{applicant_id}.pdf"
    if content.startswith(b"PK\x03\x04"):
        return f"{applicant_id}.docx"
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return f"{applicant_id}.doc"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return f"{applicant_id}.png"
    if content.startswith(b"\xff\xd8\xff"):
        return f"{applicant_id}.jpg"

    return f"{applicant_id}.bin"


DEFAULT_DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"


class GoogleDriveDownloader:
    """Downloads CV files from Google Drive (via Drive API or direct link) or HTTP(S) URLs."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        *,
        credentials_path: str | Path | None = None,
        token_provider: Callable[[], str] | None = None,
        drive_api_base_url: str = DEFAULT_DRIVE_API_BASE_URL,
    ) -> None:
        self._http_client = http_client
        self._timeout_seconds = float(timeout_seconds)
        self._max_bytes = int(max_bytes)
        self._auth = (
            GoogleServiceAccountAuth(credentials_path)
            if credentials_path is not None
            else None
        )
        self._token_provider = token_provider
        self._drive_api_base_url = drive_api_base_url.rstrip("/")

    def _has_drive_auth(self) -> bool:
        return self._token_provider is not None or self._auth is not None

    def _get_drive_token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        if self._auth is not None:
            return self._auth.get_access_token()
        raise GoogleAuthenticationError("Google Drive credentials are not configured.")

    def _build_drive_api_media_url(self, file_id: str) -> str:
        return (
            f"{self._drive_api_base_url}/files/{file_id}"
            "?alt=media&supportsAllDrives=true"
        )

    def _build_drive_api_export_url(self, file_id: str) -> str:
        return (
            f"{self._drive_api_base_url}/files/{file_id}/export"
            "?mimeType=application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def _perform_get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(
                url,
                headers=headers or None,
                follow_redirects=True,
                timeout=self._timeout_seconds,
            )
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=True,
        ) as client:
            return client.get(url, headers=headers or None)

    def download(
        self,
        cv_link: str,
        target_dir: Path,
        applicant_id: str = "applicant",
    ) -> DownloadResult:
        """Download cv_link into target_dir without crashing on network or permission failures."""
        try:
            download_url = resolve_download_url(cv_link)
        except ValueError as exc:
            return DownloadResult(success=False, error_message=str(exc))

        headers: dict[str, str] = {}
        token: str | None = None
        drive_file_id = extract_google_drive_file_id(cv_link)

        if drive_file_id and self._has_drive_auth():
            try:
                token = self._get_drive_token()
            except GoogleAuthenticationError as exc:
                return DownloadResult(
                    success=False,
                    error_message=f"Google Drive authentication failed: {redact_sensitive_text(str(exc))}",
                )
            except Exception as exc:
                return DownloadResult(
                    success=False,
                    error_message=f"Google Drive authentication failed: {redact_sensitive_text(str(exc))}",
                )
            download_url = self._build_drive_api_media_url(drive_file_id)
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = self._perform_get(download_url, headers=headers)
            # If a native Google Doc returns 403 fileNotDownloadable on alt=media, try Drive export
            if (
                drive_file_id
                and self._has_drive_auth()
                and response.status_code == 403
                and "fileNotDownloadable" in (response.text or "")
            ):
                export_url = self._build_drive_api_export_url(drive_file_id)
                response = self._perform_get(export_url, headers=headers)
        except httpx.TimeoutException:
            return DownloadResult(
                success=False,
                error_message=f"Timed out downloading CV after {self._timeout_seconds}s.",
            )
        except httpx.RequestError as exc:
            safe_exc = redact_sensitive_text(str(exc), secret=token)
            return DownloadResult(
                success=False,
                error_message=f"Network error downloading CV: {safe_exc}",
            )
        except Exception as exc:
            safe_exc = redact_sensitive_text(str(exc), secret=token)
            return DownloadResult(
                success=False,
                error_message=f"Unexpected error downloading CV: {safe_exc}",
            )

        if response.status_code in (401, 403):
            return DownloadResult(
                success=False,
                error_message=(
                    f"Google Drive file is inaccessible (HTTP {response.status_code}). "
                    "Check file sharing permissions."
                ),
            )
        if response.status_code == 404:
            return DownloadResult(
                success=False,
                error_message="CV file was not found at the provided Google Drive link (HTTP 404).",
            )
        if response.status_code >= 400:
            return DownloadResult(
                success=False,
                error_message=f"Failed to download CV (HTTP {response.status_code}).",
            )

        final_host = urlparse(str(response.url)).netloc.lower()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if "accounts.google.com" in final_host or content_type == "text/html":
            return DownloadResult(
                success=False,
                error_message=(
                    "Google Drive file requires authentication or returned an HTML page "
                    "instead of a downloadable CV document."
                ),
            )

        content = response.content
        if not content:
            return DownloadResult(
                success=False,
                error_message="Downloaded CV file is empty (0 bytes).",
            )
        if len(content) > self._max_bytes:
            return DownloadResult(
                success=False,
                error_message=f"Downloaded CV exceeds maximum allowed size ({self._max_bytes} bytes).",
            )

        filename = _infer_filename_and_extension(
            response=response,
            source_url=download_url,
            applicant_id=applicant_id,
            content=content,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (target_dir / Path(filename).name).resolve()
        dest_path.write_bytes(content)

        return DownloadResult(
            success=True,
            file_path=dest_path,
            filename=dest_path.name,
            error_message=None,
        )


class ApplicantPipeline:
    """Coordinates download, hashing, deduplication, parsing, AI scoring, Coda insert, and cleanup."""

    def __init__(
        self,
        settings: Settings | None = None,
        downloader: GoogleDriveDownloader | None = None,
        duplicate_detector: DuplicateDetector | None = None,
        cv_parser: CVParser | None = None,
        scoring_engine: ScoringEngine | None = None,
        coda_client: CodaClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if downloader is not None:
            self._downloader = downloader
        else:
            creds_path = self._settings.resolve_google_credentials_path()
            self._downloader = GoogleDriveDownloader(
                credentials_path=(
                    creds_path if (creds_path is not None and creds_path.is_file()) else None
                )
            )
        self._coda_client = coda_client or CodaClient(settings=self._settings)
        self._duplicate_detector = duplicate_detector or DuplicateDetector(
            coda_client=self._coda_client
        )
        self._cv_parser = cv_parser or CVParser()
        self._scoring_engine = scoring_engine or ScoringEngine(settings=self._settings)

    def _configured_secrets(self) -> list[str]:
        secrets: list[str] = []
        for secret_obj in (
            self._settings.env.groq_api_key,
            self._settings.env.coda_api_token,
            self._settings.env.webhook_auth_token,
        ):
            if secret_obj is not None:
                val = secret_obj.get_secret_value().strip()
                if val:
                    secrets.append(val)
        return secrets

    def _redact(self, message: str | None) -> str:
        cleaned = str(message) if message else ""
        for secret in self._configured_secrets():
            cleaned = redact_sensitive_text(cleaned, secret=secret)
        return redact_sensitive_text(cleaned)

    def _persist_coda_record(
        self,
        record: CodaApplicantRecord,
        existing_row_id: str | None = None,
    ) -> tuple[Any, bool]:
        """Update an existing Coda row on retry, or insert a new row for fresh applicants."""
        if existing_row_id and existing_row_id.strip():
            return self._coda_client.update_applicant_record(existing_row_id.strip(), record), True
        return self._coda_client.insert_applicant_record(record), False

    def _record_failed_applicant_in_coda(
        self,
        applicant: ApplicantInput,
        applicant_id: str,
        error_note: str,
        cv_hash: str | None = None,
        scoring_result: ScoringResult | None = None,
        extraction_status: ExtractionStatus | None = None,
        existing_row_id: str | None = None,
        is_retry: bool = False,
    ) -> PipelineResult:
        """Persist (insert or update) a Failed status row into Coda and return a PipelineResult."""
        safe_error = self._redact(error_note)
        failed_record = CodaApplicantRecord.from_scoring_result(
            applicant_id=applicant_id,
            name=applicant.name,
            email=normalize_email(applicant.email) or applicant.email,
            role=applicant.role,
            cv_link=applicant.cv_link,
            scoring_result=scoring_result,
            cv_hash=cv_hash,
            error_note=safe_error,
        )
        coda_res, was_updated = self._persist_coda_record(
            failed_record,
            existing_row_id=existing_row_id,
        )
        coda_err = self._redact(coda_res.error_message) if coda_res.error_message else None

        if not coda_res.success:
            logger.error(
                "Applicant processing failed AND Coda Failed-record persistence failed for "
                "applicant_id=%s: processing_error=%s coda_error=%s",
                applicant_id,
                safe_error,
                coda_err,
            )
            combined_error = f"{safe_error} | Coda write failed: {coda_err}"
            return PipelineResult(
                applicant_id=applicant_id,
                status=PipelineStatus.FAILED,
                email=normalize_email(applicant.email) or applicant.email,
                cv_hash=cv_hash,
                is_retry=is_retry,
                extraction_status=extraction_status,
                coda_status=CodaApplicantStatus.FAILED,
                coda_inserted=False,
                coda_updated=False,
                error_message=self._redact(combined_error),
            )

        logger.warning(
            "Recorded Failed applicant in Coda (%s): applicant_id=%s error=%s",
            "updated existing row" if was_updated else "inserted new row",
            applicant_id,
            safe_error,
        )
        return PipelineResult(
            applicant_id=applicant_id,
            status=PipelineStatus.FAILED,
            email=normalize_email(applicant.email) or applicant.email,
            cv_hash=cv_hash,
            is_retry=is_retry,
            extraction_status=extraction_status,
            coda_status=CodaApplicantStatus.FAILED,
            coda_inserted=True,
            coda_updated=was_updated,
            coda_row_ids=coda_res.added_row_ids,
            error_message=safe_error,
        )

    def process_applicant(
        self,
        applicant_data: ApplicantInput | dict,
        *,
        retry_only: bool = False,
        allow_retry_failed: bool = True,
    ) -> PipelineResult:
        """Execute steps 1-10 for a single applicant with retry support and guaranteed cleanup."""
        # 1. Receive and validate applicant data
        applicant = (
            applicant_data
            if isinstance(applicant_data, ApplicantInput)
            else ApplicantInput.model_validate(applicant_data)
        )
        applicant_id = applicant.resolved_applicant_id()
        norm_email = normalize_email(applicant.email) or applicant.email

        logger.info("Starting pipeline for applicant_id=%s", applicant_id)

        temp_dir = Path(tempfile.mkdtemp(prefix=f"cv_{applicant_id}_"))
        acquired_lock = False
        locked_applicant_id = applicant_id
        locked_cv_hash: str | None = None
        locked_row_id: str | None = None

        try:
            # 2. Download CV from Google Drive
            download_res = self._downloader.download(
                cv_link=applicant.cv_link,
                target_dir=temp_dir,
                applicant_id=applicant_id,
            )

            if not download_res.success or download_res.file_path is None:
                download_err = self._redact(
                    download_res.error_message or "CV download failed."
                )
                dup_check = self._duplicate_detector.check_applicant(
                    applicant_id=applicant_id,
                    email=norm_email,
                    cv_hash=None,
                    refresh=True,
                )
                if dup_check.check_status == "error":
                    return PipelineResult(
                        applicant_id=applicant_id,
                        status=PipelineStatus.FAILED,
                        email=norm_email,
                        coda_inserted=False,
                        error_message=self._redact(
                            dup_check.error_message or "Duplicate check failed."
                        ),
                    )
                if dup_check.is_duplicate:
                    return PipelineResult(
                        applicant_id=dup_check.matched_applicant_id or applicant_id,
                        status=PipelineStatus.SKIPPED_DUPLICATE,
                        email=norm_email,
                        duplicate_reason=dup_check.reason,
                        coda_inserted=False,
                    )
                if retry_only and not dup_check.is_retry:
                    logger.info(
                        "Skipping applicant_id=%s in retry_only mode (no existing Failed record).",
                        applicant_id,
                    )
                    return PipelineResult(
                        applicant_id=applicant_id,
                        status=PipelineStatus.SKIPPED_DUPLICATE,
                        email=norm_email,
                        coda_inserted=False,
                    )
                if not retry_only and not allow_retry_failed and dup_check.is_retry:
                    logger.info(
                        "Skipping Failed applicant_id=%s because --retry-failed was not requested.",
                        dup_check.matched_applicant_id or applicant_id,
                    )
                    return PipelineResult(
                        applicant_id=dup_check.matched_applicant_id or applicant_id,
                        status=PipelineStatus.SKIPPED_DUPLICATE,
                        email=norm_email,
                        coda_inserted=False,
                    )

                effective_id = (
                    dup_check.matched_applicant_id
                    if dup_check.is_retry and dup_check.matched_applicant_id
                    else applicant_id
                )
                existing_row_id = dup_check.matched_row_id if dup_check.is_retry else None
                preserved_hash = dup_check.existing_cv_hash if dup_check.is_retry else None

                locked_applicant_id = effective_id
                locked_cv_hash = preserved_hash
                locked_row_id = existing_row_id
                acquired_lock = self._duplicate_detector.try_acquire_processing(
                    applicant_id=locked_applicant_id,
                    email=norm_email,
                    cv_hash=locked_cv_hash,
                    row_id=locked_row_id,
                )
                if not acquired_lock:
                    return PipelineResult(
                        applicant_id=effective_id,
                        status=PipelineStatus.SKIPPED_DUPLICATE,
                        email=norm_email,
                        duplicate_reason=DuplicateReason.PROCESSING_IN_PROGRESS,
                        coda_inserted=False,
                    )

                return self._record_failed_applicant_in_coda(
                    applicant=applicant,
                    applicant_id=effective_id,
                    error_note=download_err,
                    cv_hash=preserved_hash,
                    existing_row_id=existing_row_id,
                    is_retry=dup_check.is_retry,
                )

            local_cv_path = download_res.file_path

            # 3. Calculate CV SHA-256 hash
            cv_hash = compute_file_sha256(local_cv_path)

            # 4 & 5. Check duplicate email, CV hash, and existing Coda status
            dup_check: DuplicateCheckResult = self._duplicate_detector.check_applicant(
                applicant_id=applicant_id,
                email=norm_email,
                cv_hash=cv_hash,
                refresh=True,
            )
            if dup_check.check_status == "error":
                err = self._redact(
                    dup_check.error_message or "Failed to verify existing Coda records."
                )
                logger.error(
                    "Aborting applicant_id=%s because Coda duplicate check failed: %s",
                    applicant_id,
                    err,
                )
                return PipelineResult(
                    applicant_id=applicant_id,
                    status=PipelineStatus.FAILED,
                    email=norm_email,
                    cv_hash=cv_hash,
                    coda_inserted=False,
                    error_message=err,
                )

            if dup_check.is_duplicate:
                return PipelineResult(
                    applicant_id=dup_check.matched_applicant_id or applicant_id,
                    status=PipelineStatus.SKIPPED_DUPLICATE,
                    email=norm_email,
                    cv_hash=cv_hash,
                    duplicate_reason=dup_check.reason,
                    coda_inserted=False,
                )

            if retry_only and not dup_check.is_retry:
                logger.info(
                    "Skipping applicant_id=%s in retry_only mode (status is not Failed).",
                    applicant_id,
                )
                return PipelineResult(
                    applicant_id=applicant_id,
                    status=PipelineStatus.SKIPPED_DUPLICATE,
                    email=norm_email,
                    cv_hash=cv_hash,
                    coda_inserted=False,
                )

            if not retry_only and not allow_retry_failed and dup_check.is_retry:
                logger.info(
                    "Skipping Failed applicant_id=%s because --retry-failed was not requested.",
                    dup_check.matched_applicant_id or applicant_id,
                )
                return PipelineResult(
                    applicant_id=dup_check.matched_applicant_id or applicant_id,
                    status=PipelineStatus.SKIPPED_DUPLICATE,
                    email=norm_email,
                    cv_hash=cv_hash,
                    coda_inserted=False,
                )

            is_retry = dup_check.is_retry
            effective_applicant_id = (
                dup_check.matched_applicant_id
                if is_retry and dup_check.matched_applicant_id
                else applicant_id
            )
            existing_row_id = dup_check.matched_row_id if is_retry else None

            # Atomically acquire concurrent execution guard before parsing/scoring/writing
            locked_applicant_id = effective_applicant_id
            locked_cv_hash = cv_hash
            locked_row_id = existing_row_id
            acquired_lock = self._duplicate_detector.try_acquire_processing(
                applicant_id=locked_applicant_id,
                email=norm_email,
                cv_hash=locked_cv_hash,
                row_id=locked_row_id,
            )
            if not acquired_lock:
                logger.warning(
                    "Skipping applicant_id=%s: concurrent execution already in progress.",
                    effective_applicant_id,
                )
                return PipelineResult(
                    applicant_id=effective_applicant_id,
                    status=PipelineStatus.SKIPPED_DUPLICATE,
                    email=norm_email,
                    cv_hash=cv_hash,
                    duplicate_reason=DuplicateReason.PROCESSING_IN_PROGRESS,
                    coda_inserted=False,
                )

            # 6. Parse CV using CVParser
            parse_result: CVParseResult = self._cv_parser.parse(local_cv_path)

            # 7. If parsing fails, create or update a Failed Coda record
            if parse_result.extraction_status != ExtractionStatus.SUCCESS:
                parse_err = self._redact(
                    parse_result.error_message or "CV text extraction failed."
                )
                return self._record_failed_applicant_in_coda(
                    applicant=applicant,
                    applicant_id=effective_applicant_id,
                    error_note=f"CV parsing failed: {parse_err}",
                    cv_hash=cv_hash,
                    extraction_status=parse_result.extraction_status,
                    existing_row_id=existing_row_id,
                    is_retry=is_retry,
                )

            # 8. Score successfully parsed CVs using ScoringEngine
            scoring_result: ScoringResult = self._scoring_engine.score_cv(
                parse_result.extracted_text
            )
            if scoring_result.status != ScoringStatus.SUCCESS:
                score_err = self._redact(
                    scoring_result.error_message or "AI scoring failed."
                )
                return self._record_failed_applicant_in_coda(
                    applicant=applicant,
                    applicant_id=effective_applicant_id,
                    error_note=score_err,
                    cv_hash=cv_hash,
                    scoring_result=scoring_result,
                    extraction_status=parse_result.extraction_status,
                    existing_row_id=existing_row_id,
                    is_retry=is_retry,
                )

            # 9. Insert or update Scored results in Coda using CodaClient
            coda_record = CodaApplicantRecord.from_scoring_result(
                applicant_id=effective_applicant_id,
                name=applicant.name,
                email=norm_email,
                role=applicant.role,
                cv_link=applicant.cv_link,
                scoring_result=scoring_result,
                cv_hash=cv_hash,
            )
            coda_res, was_updated = self._persist_coda_record(
                coda_record,
                existing_row_id=existing_row_id,
            )
            if not coda_res.success:
                coda_err = self._redact(
                    coda_res.error_message or "Coda record persistence failed."
                )
                logger.error(
                    "Coda write failed for scored applicant_id=%s: %s",
                    effective_applicant_id,
                    coda_err,
                )
                return PipelineResult(
                    applicant_id=effective_applicant_id,
                    status=PipelineStatus.FAILED,
                    email=norm_email,
                    cv_hash=cv_hash,
                    is_retry=is_retry,
                    extraction_status=parse_result.extraction_status,
                    total_score=scoring_result.total_score,
                    recommendation=scoring_result.recommendation,
                    coda_status=CodaApplicantStatus.SCORED,
                    coda_inserted=False,
                    coda_updated=False,
                    error_message=coda_err,
                )

            logger.info(
                "Completed scoring for applicant_id=%s (%s) total_score=%.2f recommendation=%s",
                effective_applicant_id,
                "retried & updated" if was_updated else "new insert",
                scoring_result.total_score or 0.0,
                scoring_result.recommendation.value if scoring_result.recommendation else "",
            )
            return PipelineResult(
                applicant_id=effective_applicant_id,
                status=PipelineStatus.SCORED,
                email=norm_email,
                cv_hash=cv_hash,
                is_retry=is_retry,
                extraction_status=parse_result.extraction_status,
                total_score=scoring_result.total_score,
                recommendation=scoring_result.recommendation,
                coda_status=CodaApplicantStatus.SCORED,
                coda_inserted=True,
                coda_updated=was_updated,
                coda_row_ids=coda_res.added_row_ids,
                error_message=None,
            )

        except Exception as exc:
            safe_exc = self._redact(str(exc)) or "Unexpected pipeline error."
            logger.exception("Unhandled error processing applicant_id=%s: %s", applicant_id, safe_exc)
            return PipelineResult(
                applicant_id=applicant_id,
                status=PipelineStatus.FAILED,
                email=norm_email,
                coda_inserted=False,
                error_message=safe_exc,
            )
        finally:
            if acquired_lock:
                self._duplicate_detector.release_processing(
                    applicant_id=locked_applicant_id,
                    email=norm_email,
                    cv_hash=locked_cv_hash,
                    row_id=locked_row_id,
                )
            # 10. Clean up temporary downloaded files
            shutil.rmtree(temp_dir, ignore_errors=True)

    def process_batch(
        self,
        applicants: Iterable[ApplicantInput | dict],
        *,
        retry_only: bool = False,
        allow_retry_failed: bool = True,
    ) -> list[PipelineResult]:
        """Process multiple applicants independently; a failure in one never stops others."""
        results: list[PipelineResult] = []
        for index, raw_applicant in enumerate(applicants):
            try:
                results.append(
                    self.process_applicant(
                        raw_applicant,
                        retry_only=retry_only,
                        allow_retry_failed=allow_retry_failed,
                    )
                )
            except Exception as exc:
                safe_msg = self._redact(str(exc)) or "Invalid applicant payload."
                fallback_id = (
                    raw_applicant.get("applicant_id")
                    if isinstance(raw_applicant, dict)
                    else getattr(raw_applicant, "applicant_id", None)
                ) or f"ITEM-{index + 1:03d}"
                fallback_email = (
                    raw_applicant.get("email")
                    if isinstance(raw_applicant, dict)
                    else getattr(raw_applicant, "email", "")
                ) or ""
                logger.error(
                    "Failed applicant item in batch (id=%s): %s",
                    fallback_id,
                    safe_msg,
                )
                results.append(
                    PipelineResult(
                        applicant_id=str(fallback_id),
                        status=PipelineStatus.FAILED,
                        email=str(fallback_email),
                        coda_inserted=False,
                        error_message=safe_msg,
                    )
                )
        return results
