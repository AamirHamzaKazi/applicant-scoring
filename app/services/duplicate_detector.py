"""Status-aware duplicate and retry detection using normalized emails, SHA-256 CV hashes, and Coda status.

Distinguishes four states when checking existing Coda records:
  - No existing applicant        -> action="process" (is_duplicate=False, is_retry=False)
  - Existing Scored applicant    -> action="skip"    (is_duplicate=True,  is_retry=False)
  - Existing Processing applicant-> action="skip"    (is_duplicate=True,  is_retry=False)
  - Existing Failed applicant    -> action="retry"   (is_duplicate=False, is_retry=True)

Includes thread-safe in-flight locking to prevent concurrent duplicate/retry execution.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
import hashlib
import logging
from pathlib import Path
import threading
from typing import Protocol

from pydantic import BaseModel, Field

from app.integrations.coda_client import (
    CodaApplicantStatus,
    CodaExistingRow,
    CodaFetchResult,
    normalize_email,
)

logger = logging.getLogger(__name__)


class DuplicateReason(str, Enum):
    """Reason codes logged and returned when an applicant is skipped or matched."""

    DUPLICATE_EMAIL = "duplicate_email"
    DUPLICATE_CV_HASH = "duplicate_cv_hash"
    DUPLICATE_APPLICANT_ID = "duplicate_applicant_id"
    PROCESSING_IN_PROGRESS = "processing_in_progress"


class CodaRecordsReader(Protocol):
    """Minimal protocol required from a Coda client for duplicate detection."""

    def list_existing_records(self, limit: int = 100) -> CodaFetchResult: ...


def compute_bytes_sha256(content: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest of raw bytes."""
    return hashlib.sha256(content).hexdigest().lower()


def compute_file_sha256(file_path: str | Path, chunk_size: int = 65536) -> str:
    """Compute lowercase hexadecimal SHA-256 hash of a CV file in fixed-size chunks."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"CV file not found for hashing: {path}")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def _normalize_row_status(raw_status: str | None) -> str:
    """Normalize a Coda row status string; unspecified legacy rows default to Scored."""
    if raw_status is None or not str(raw_status).strip():
        return CodaApplicantStatus.SCORED.value
    cleaned = str(raw_status).strip().lower()
    if cleaned == "failed":
        return CodaApplicantStatus.FAILED.value
    if cleaned == "processing":
        return CodaApplicantStatus.PROCESSING.value
    if cleaned == "scored":
        return CodaApplicantStatus.SCORED.value
    return str(raw_status).strip()


def _status_priority(row: CodaExistingRow) -> int:
    """Priority when indexing multiple rows: Scored (3) > Processing (2) > Failed (1)."""
    norm = _normalize_row_status(row.status)
    if norm == CodaApplicantStatus.SCORED.value:
        return 3
    if norm == CodaApplicantStatus.PROCESSING.value:
        return 2
    return 1


class DuplicateCheckResult(BaseModel):
    """Structured outcome returned when checking an applicant for duplicates or retry."""

    is_duplicate: bool
    is_retry: bool = False
    action: str = "process"
    existing_status: str | None = None
    reason: DuplicateReason | None = None
    matched_reasons: list[DuplicateReason] = Field(default_factory=list)
    normalized_email: str | None = None
    cv_hash: str | None = None
    existing_cv_hash: str | None = None
    matched_row_id: str | None = None
    matched_applicant_id: str | None = None
    check_status: str = "ok"
    error_message: str | None = None


class DuplicateDetector:
    """Status-aware duplicate & retry detector with thread-safe concurrent processing protection."""

    def __init__(self, coda_client: CodaRecordsReader) -> None:
        self._coda_client = coda_client
        self._email_index: dict[str, CodaExistingRow] = {}
        self._hash_index: dict[str, CodaExistingRow] = {}
        self._applicant_id_index: dict[str, CodaExistingRow] = {}
        self._index_loaded = False
        self._lock = threading.Lock()
        self._in_flight_keys: set[str] = set()

    @staticmethod
    def _build_in_flight_tokens(
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
    ) -> set[str]:
        tokens: set[str] = set()
        if applicant_id and applicant_id.strip():
            tokens.add(f"id:{applicant_id.strip().upper()}")
        norm_email = normalize_email(email)
        if norm_email:
            tokens.add(f"email:{norm_email}")
        if cv_hash and cv_hash.strip():
            tokens.add(f"hash:{cv_hash.strip().lower()}")
        if row_id and row_id.strip():
            tokens.add(f"row:{row_id.strip()}")
        return tokens

    def try_acquire_processing(
        self,
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
    ) -> bool:
        """Atomically mark an applicant as in-flight; return False if already being processed."""
        tokens = self._build_in_flight_tokens(
            applicant_id=applicant_id,
            email=email,
            cv_hash=cv_hash,
            row_id=row_id,
        )
        if not tokens:
            return True
        with self._lock:
            if self._in_flight_keys.intersection(tokens):
                return False
            self._in_flight_keys.update(tokens)
            return True

    def release_processing(
        self,
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
    ) -> None:
        """Release in-flight lock tokens for an applicant."""
        tokens = self._build_in_flight_tokens(
            applicant_id=applicant_id,
            email=email,
            cv_hash=cv_hash,
            row_id=row_id,
        )
        if not tokens:
            return
        with self._lock:
            self._in_flight_keys.difference_update(tokens)

    @contextmanager
    def processing_guard(
        self,
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
    ) -> Iterator[bool]:
        """Context manager that acquires and releases in-flight concurrency protection."""
        acquired = self.try_acquire_processing(
            applicant_id=applicant_id,
            email=email,
            cv_hash=cv_hash,
            row_id=row_id,
        )
        try:
            yield acquired
        finally:
            if acquired:
                self.release_processing(
                    applicant_id=applicant_id,
                    email=email,
                    cv_hash=cv_hash,
                    row_id=row_id,
                )

    def _is_in_flight(
        self,
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
    ) -> bool:
        tokens = self._build_in_flight_tokens(
            applicant_id=applicant_id,
            email=email,
            cv_hash=cv_hash,
            row_id=row_id,
        )
        if not tokens:
            return False
        with self._lock:
            return bool(self._in_flight_keys.intersection(tokens))

    def load_existing_records(self) -> CodaFetchResult:
        """Fetch existing Coda rows and rebuild the local email, CV-hash, and applicant_id indices."""
        fetch_result = self._coda_client.list_existing_records()
        if not fetch_result.success:
            return fetch_result

        email_index: dict[str, CodaExistingRow] = {}
        hash_index: dict[str, CodaExistingRow] = {}
        applicant_id_index: dict[str, CodaExistingRow] = {}

        for record in fetch_result.records:
            norm_email = normalize_email(record.email)
            if norm_email:
                existing = email_index.get(norm_email)
                if existing is None or _status_priority(record) > _status_priority(existing):
                    email_index[norm_email] = record

            norm_hash = record.cv_hash.strip().lower() if record.cv_hash else None
            if norm_hash:
                existing = hash_index.get(norm_hash)
                if existing is None or _status_priority(record) > _status_priority(existing):
                    hash_index[norm_hash] = record

            norm_id = record.applicant_id.strip().upper() if record.applicant_id else None
            if norm_id:
                existing = applicant_id_index.get(norm_id)
                if existing is None or _status_priority(record) > _status_priority(existing):
                    applicant_id_index[norm_id] = record

        self._email_index = email_index
        self._hash_index = hash_index
        self._applicant_id_index = applicant_id_index
        self._index_loaded = True
        return fetch_result

    def register_processed(
        self,
        *,
        applicant_id: str | None = None,
        email: str | None = None,
        cv_hash: str | None = None,
        row_id: str | None = None,
        status: str = CodaApplicantStatus.SCORED.value,
    ) -> None:
        """Register a newly processed applicant in the in-memory index for batch runs."""
        norm_email = normalize_email(email)
        norm_hash = cv_hash.strip().lower() if cv_hash and cv_hash.strip() else None
        norm_id = applicant_id.strip().upper() if applicant_id and applicant_id.strip() else None
        entry = CodaExistingRow(
            row_id=row_id,
            applicant_id=applicant_id,
            email=norm_email,
            cv_hash=norm_hash,
            status=status,
        )
        if norm_email:
            self._email_index[norm_email] = entry
        if norm_hash:
            self._hash_index[norm_hash] = entry
        if norm_id:
            self._applicant_id_index[norm_id] = entry
        self._index_loaded = True

    def check_applicant(
        self,
        *,
        email: str | None = None,
        cv_path: str | Path | None = None,
        cv_hash: str | None = None,
        applicant_id: str | None = None,
        refresh: bool = True,
    ) -> DuplicateCheckResult:
        """Check Coda records and in-flight state for duplicate, concurrent processing, or Failed retry."""
        norm_email = normalize_email(email)
        norm_app_id = applicant_id.strip().upper() if applicant_id and applicant_id.strip() else None

        resolved_hash: str | None = (
            cv_hash.strip().lower() if cv_hash and cv_hash.strip() else None
        )
        if resolved_hash is None and cv_path is not None:
            try:
                resolved_hash = compute_file_sha256(cv_path)
            except OSError as exc:
                logger.warning(
                    "Unable to compute SHA-256 hash for applicant_id=%s (%s): %s",
                    applicant_id or "unknown",
                    cv_path,
                    exc,
                )

        # 1. Check in-memory concurrent processing guard first
        if self._is_in_flight(
            applicant_id=applicant_id,
            email=norm_email,
            cv_hash=resolved_hash,
        ):
            logger.warning(
                "Skipping applicant_id=%s: %s (already being processed concurrently)",
                applicant_id or "unknown",
                DuplicateReason.PROCESSING_IN_PROGRESS.value,
            )
            return DuplicateCheckResult(
                is_duplicate=True,
                is_retry=False,
                action="skip",
                existing_status=CodaApplicantStatus.PROCESSING.value,
                reason=DuplicateReason.PROCESSING_IN_PROGRESS,
                matched_reasons=[DuplicateReason.PROCESSING_IN_PROGRESS],
                normalized_email=norm_email,
                cv_hash=resolved_hash,
                check_status="ok",
                error_message=None,
            )

        if refresh or not self._index_loaded:
            fetch_result = self.load_existing_records()
            if not fetch_result.success:
                logger.error(
                    "Duplicate check failed while reading existing Coda records for applicant_id=%s: %s",
                    applicant_id or "unknown",
                    fetch_result.error_message,
                )
                return DuplicateCheckResult(
                    is_duplicate=False,
                    is_retry=False,
                    action="error",
                    reason=None,
                    matched_reasons=[],
                    normalized_email=norm_email,
                    cv_hash=resolved_hash,
                    check_status="error",
                    error_message=fetch_result.error_message,
                )

        matched_reasons: list[DuplicateReason] = []
        matched_rows: list[CodaExistingRow] = []

        if norm_email and norm_email in self._email_index:
            matched_reasons.append(DuplicateReason.DUPLICATE_EMAIL)
            matched_rows.append(self._email_index[norm_email])

        if resolved_hash and resolved_hash in self._hash_index:
            matched_reasons.append(DuplicateReason.DUPLICATE_CV_HASH)
            hash_row = self._hash_index[resolved_hash]
            if hash_row not in matched_rows:
                matched_rows.append(hash_row)

        if not matched_rows and norm_app_id and norm_app_id in self._applicant_id_index:
            matched_reasons.append(DuplicateReason.DUPLICATE_APPLICANT_ID)
            matched_rows.append(self._applicant_id_index[norm_app_id])

        if not matched_rows:
            return DuplicateCheckResult(
                is_duplicate=False,
                is_retry=False,
                action="process",
                existing_status=None,
                reason=None,
                matched_reasons=[],
                normalized_email=norm_email,
                cv_hash=resolved_hash,
                check_status="ok",
                error_message=None,
            )

        # Evaluate statuses across matched rows (Scored > Processing > Failed)
        matched_rows.sort(key=_status_priority, reverse=True)
        primary_row = matched_rows[0]
        row_status = _normalize_row_status(primary_row.status)

        if self._is_in_flight(row_id=primary_row.row_id):
            row_status = CodaApplicantStatus.PROCESSING.value

        # Case A: Existing Processing applicant -> skip to prevent concurrent processing
        if row_status == CodaApplicantStatus.PROCESSING.value:
            logger.warning(
                "Skipping applicant_id=%s: %s (existing_applicant_id=%s, existing_row_id=%s, status=Processing)",
                applicant_id or "unknown",
                DuplicateReason.PROCESSING_IN_PROGRESS.value,
                primary_row.applicant_id,
                primary_row.row_id,
            )
            return DuplicateCheckResult(
                is_duplicate=True,
                is_retry=False,
                action="skip",
                existing_status=CodaApplicantStatus.PROCESSING.value,
                reason=DuplicateReason.PROCESSING_IN_PROGRESS,
                matched_reasons=[DuplicateReason.PROCESSING_IN_PROGRESS, *matched_reasons],
                normalized_email=norm_email,
                cv_hash=resolved_hash,
                existing_cv_hash=primary_row.cv_hash,
                matched_row_id=primary_row.row_id,
                matched_applicant_id=primary_row.applicant_id,
                check_status="ok",
                error_message=None,
            )

        # Case B: Existing Failed applicant (and no Scored/Processing row matched) -> allow retry!
        if row_status == CodaApplicantStatus.FAILED.value:
            logger.info(
                "Existing Failed applicant detected for retry: applicant_id=%s "
                "(existing_applicant_id=%s, existing_row_id=%s)",
                applicant_id or "unknown",
                primary_row.applicant_id,
                primary_row.row_id,
            )
            return DuplicateCheckResult(
                is_duplicate=False,
                is_retry=True,
                action="retry",
                existing_status=CodaApplicantStatus.FAILED.value,
                reason=None,
                matched_reasons=matched_reasons,
                normalized_email=norm_email,
                cv_hash=resolved_hash,
                existing_cv_hash=primary_row.cv_hash,
                matched_row_id=primary_row.row_id,
                matched_applicant_id=primary_row.applicant_id,
                check_status="ok",
                error_message=None,
            )

        # Case C: Existing Scored applicant -> skip and log duplicate reason
        primary_reason = matched_reasons[0]
        logger.warning(
            "Skipping applicant_id=%s: %s (matched_reasons=%s, existing_applicant_id=%s, existing_row_id=%s, status=%s)",
            applicant_id or "unknown",
            primary_reason.value,
            [r.value for r in matched_reasons],
            primary_row.applicant_id,
            primary_row.row_id,
            row_status,
        )
        return DuplicateCheckResult(
            is_duplicate=True,
            is_retry=False,
            action="skip",
            existing_status=row_status,
            reason=primary_reason,
            matched_reasons=matched_reasons,
            normalized_email=norm_email,
            cv_hash=resolved_hash,
            existing_cv_hash=primary_row.cv_hash,
            matched_row_id=primary_row.row_id,
            matched_applicant_id=primary_row.applicant_id,
            check_status="ok",
            error_message=None,
        )
