"""Read-only Google Sheets integration using Google Service Account credentials.

Authenticates via a Google Cloud service account JSON key file
(GOOGLE_APPLICATION_CREDENTIALS, e.g., secrets/google-service-account.json),
reads applicant rows from the configured Google Sheet and worksheet, validates
Name, Email, Role, and CV_Link, skips empty rows, and isolates invalid rows
without crashing the batch. Never writes data back to Google Sheets.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlparse

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
import httpx
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, Settings, get_settings
from app.integrations.coda_client import redact_sensitive_text

logger = logging.getLogger(__name__)

GOOGLE_SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_GOOGLE_SCOPES: tuple[str, ...] = (
    GOOGLE_SHEETS_READONLY_SCOPE,
    GOOGLE_DRIVE_READONLY_SCOPE,
)

DEFAULT_SHEETS_BASE_URL = "https://sheets.googleapis.com/v4"
DEFAULT_SHEETS_TIMEOUT_SECONDS = 20.0

_CANDIDATE_NAME_NUM_REGEX = re.compile(r"^candidate\s+(\d+)$", re.IGNORECASE)


class GoogleAuthenticationError(RuntimeError):
    """Raised when Google service account credentials cannot be loaded or refreshed."""


class SheetInvalidRow(BaseModel):
    """Details of an invalid worksheet row that was logged and skipped."""

    row_number: int
    reason: str
    raw_values: dict[str, str] = Field(default_factory=dict)


class SheetReadResult(BaseModel):
    """Outcome of reading and validating applicant rows from Google Sheets."""

    success: bool
    spreadsheet_id: str | None = None
    worksheet_name: str | None = None
    applicants: list[Any] = Field(default_factory=list)
    invalid_rows: list[SheetInvalidRow] = Field(default_factory=list)
    skipped_empty_rows: int = 0
    total_data_rows: int = 0
    status_code: int | None = None
    error_message: str | None = None


class GoogleServiceAccountAuth:
    """Loads a service account JSON file and provides refreshed OAuth2 access tokens."""

    def __init__(
        self,
        credentials_path: str | Path | None,
        scopes: tuple[str, ...] = DEFAULT_GOOGLE_SCOPES,
    ) -> None:
        self._credentials_path = self._resolve_path(credentials_path)
        self._scopes = tuple(scopes)
        self._credentials: service_account.Credentials | None = None

    @staticmethod
    def _resolve_path(credentials_path: str | Path | None) -> Path | None:
        if credentials_path is None:
            return None
        cleaned = str(credentials_path).strip()
        if not cleaned:
            return None
        path = Path(cleaned)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return path

    @property
    def credentials_path(self) -> Path | None:
        return self._credentials_path

    def _load_credentials(self) -> service_account.Credentials:
        if self._credentials is not None:
            return self._credentials

        if self._credentials_path is None:
            raise GoogleAuthenticationError(
                "GOOGLE_APPLICATION_CREDENTIALS is not configured."
            )
        if not self._credentials_path.is_file():
            raise GoogleAuthenticationError(
                f"Service account credentials file not found: {self._credentials_path.name}"
            )

        try:
            self._credentials = (
                service_account.Credentials.from_service_account_file(
                    str(self._credentials_path),
                    scopes=list(self._scopes),
                )
            )
            return self._credentials
        except (ValueError, KeyError, OSError, GoogleAuthError) as exc:
            raise GoogleAuthenticationError(
                f"Invalid service account credentials file ({self._credentials_path.name}): "
                f"{redact_sensitive_text(str(exc))}"
            ) from exc
        except Exception as exc:
            raise GoogleAuthenticationError(
                f"Failed to load service account credentials ({self._credentials_path.name}): "
                f"{redact_sensitive_text(str(exc))}"
            ) from exc

    def get_access_token(self) -> str:
        """Return a valid OAuth2 access token, refreshing when needed."""
        creds = self._load_credentials()
        if not creds.valid or creds.expired or not creds.token:
            try:
                creds.refresh(GoogleAuthRequest())
            except (RefreshError, GoogleAuthError) as exc:
                raise GoogleAuthenticationError(
                    f"Google service account authentication failed during token refresh: "
                    f"{redact_sensitive_text(str(exc))}"
                ) from exc
            except Exception as exc:
                raise GoogleAuthenticationError(
                    f"Unexpected error refreshing Google service account token: "
                    f"{redact_sensitive_text(str(exc))}"
                ) from exc

        token = (creds.token or "").strip()
        if not token:
            raise GoogleAuthenticationError(
                "Google service account token refresh succeeded but returned an empty token."
            )
        return token


def _derive_candidate_applicant_id(
    explicit_id: str | None,
    name: str,
) -> str | None:
    """Derive APP-NNN when Name matches 'Candidate NNN', or preserve explicit Applicant ID."""
    if explicit_id and explicit_id.strip():
        return explicit_id.strip()
    match = _CANDIDATE_NAME_NUM_REGEX.match(name.strip())
    if match:
        return f"APP-{int(match.group(1)):03d}"
    return None


def _resolve_header_indices(
    header_row: list[Any],
    column_mapping: dict[str, str],
) -> tuple[dict[str, int], list[str]]:
    """Map logical field names (name, email, role, cv_link, applicant_id) to 0-based column indices."""
    normalized_headers: dict[str, int] = {}
    for idx, raw_col in enumerate(header_row):
        col_name = str(raw_col or "").strip().lower()
        if col_name and col_name not in normalized_headers:
            normalized_headers[col_name] = idx

    resolved: dict[str, int] = {}
    missing: list[str] = []

    aliases: dict[str, tuple[str, ...]] = {
        "name": ("name",),
        "email": ("email",),
        "role": ("role",),
        "cv_link": ("cv_link", "cv link"),
    }

    for field_key in ("name", "email", "role", "cv_link"):
        configured_header = (column_mapping.get(field_key) or field_key).strip()
        lookup_candidates = [configured_header.lower()]
        for alias in aliases.get(field_key, ()):
            if alias not in lookup_candidates:
                lookup_candidates.append(alias)

        matched_idx: int | None = None
        for candidate in lookup_candidates:
            if candidate in normalized_headers:
                matched_idx = normalized_headers[candidate]
                break

        if matched_idx is None:
            missing.append(configured_header)
        else:
            resolved[field_key] = matched_idx

    # Optional Applicant ID column if present in the worksheet
    for id_alias in ("applicant id", "applicant_id"):
        if id_alias in normalized_headers:
            resolved["applicant_id"] = normalized_headers[id_alias]
            break

    return resolved, missing


class GoogleSheetsClient:
    """Read-only client for fetching and validating applicant rows from Google Sheets."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        spreadsheet_id: str | None = None,
        worksheet_name: str | None = None,
        credentials_path: str | Path | None = None,
        column_mapping: dict[str, str] | None = None,
        base_url: str = DEFAULT_SHEETS_BASE_URL,
        timeout_seconds: float = DEFAULT_SHEETS_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings or get_settings()

        env_sheet_id = (
            (os.getenv("GOOGLE_SHEETS_ID") or "").strip()
            or (os.getenv("GOOGLE_SHEET_ID") or "").strip()
            or (self._settings.env.google_sheets_id or "").strip()
        )
        configured_sheet_id = (
            self._settings.google_sheets.spreadsheet_id.strip()
            if self._settings.google_sheets.spreadsheet_id
            else ""
        )
        self._spreadsheet_id = (
            (spreadsheet_id or "").strip()
            or env_sheet_id
            or configured_sheet_id
        )

        if worksheet_name and worksheet_name.strip():
            self._worksheet_name = worksheet_name.strip()
        elif (
            self._settings.google_sheets.worksheet_name
            and self._settings.google_sheets.worksheet_name.strip() != "Sheet1"
        ):
            self._worksheet_name = self._settings.google_sheets.worksheet_name.strip()
        elif (
            self._settings.env.google_worksheet_name
            and self._settings.env.google_worksheet_name.strip()
        ):
            self._worksheet_name = self._settings.env.google_worksheet_name.strip()
        else:
            self._worksheet_name = (
                self._settings.google_sheets.worksheet_name.strip() or "Sheet1"
            )

        cols_cfg = self._settings.google_sheets.columns
        default_cols = {
            "name": cols_cfg.name,
            "email": cols_cfg.email,
            "role": cols_cfg.role,
            "cv_link": cols_cfg.cv_link,
        }
        self._column_mapping = dict(column_mapping or default_cols)
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._http_client = http_client

        resolved_creds = (
            credentials_path
            if credentials_path is not None
            else self._settings.resolve_google_credentials_path()
        )
        self._auth = GoogleServiceAccountAuth(resolved_creds)
        self._token_provider = token_provider

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @property
    def worksheet_name(self) -> str:
        return self._worksheet_name

    @property
    def column_mapping(self) -> dict[str, str]:
        return dict(self._column_mapping)

    def _get_token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        return self._auth.get_access_token()

    def _request_values(self, token: str) -> httpx.Response:
        encoded_range = quote(self._worksheet_name, safe="")
        url = (
            f"{self._base_url}/spreadsheets/{self._spreadsheet_id}/values/{encoded_range}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if self._http_client is not None:
            return self._http_client.get(
                url,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        with httpx.Client(timeout=self._timeout_seconds) as client:
            return client.get(url, headers=headers)

    def read_applicants(self) -> SheetReadResult:
        """Read worksheet rows and convert valid rows into ApplicantInput objects."""
        from app.services.pipeline import ApplicantInput

        if not self._spreadsheet_id:
            msg = (
                "Google Sheets spreadsheet_id is not configured. Set "
                "google_sheets.spreadsheet_id in config.yaml or GOOGLE_SHEETS_ID in .env."
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=None,
                worksheet_name=self._worksheet_name,
                error_message=msg,
            )

        token: str | None = None
        try:
            token = self._get_token()
        except GoogleAuthenticationError as exc:
            safe_err = redact_sensitive_text(str(exc))
            logger.error("Google Sheets authentication failed: %s", safe_err)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                error_message=safe_err,
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc))
            logger.error("Unexpected Google authentication error: %s", safe_err)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                error_message=f"Google authentication error: {safe_err}",
            )

        try:
            response = self._request_values(token)
        except httpx.TimeoutException:
            msg = (
                f"Timed out reading Google Sheet '{self._worksheet_name}' "
                f"after {self._timeout_seconds}s."
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                error_message=msg,
            )
        except httpx.RequestError as exc:
            safe_err = redact_sensitive_text(str(exc), secret=token)
            msg = f"Network error reading Google Sheets API: {safe_err}"
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                error_message=msg,
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc), secret=token)
            msg = f"Unexpected error reading Google Sheets API: {safe_err}"
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                error_message=msg,
            )

        if response.status_code in (401, 403):
            msg = (
                f"Google Sheets API access denied (HTTP {response.status_code}). "
                "Verify service account credentials and ensure the spreadsheet is shared "
                "with the service account as Viewer."
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=response.status_code,
                error_message=msg,
            )

        if response.status_code == 404:
            msg = (
                f"Google Spreadsheet or worksheet '{self._worksheet_name}' not found "
                "(HTTP 404)."
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=404,
                error_message=msg,
            )

        if response.status_code >= 400:
            msg = (
                f"Google Sheets API returned HTTP {response.status_code} while reading "
                f"worksheet '{self._worksheet_name}'."
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=response.status_code,
                error_message=msg,
            )

        try:
            payload = response.json()
        except Exception as exc:
            msg = f"Invalid JSON response from Google Sheets API: {redact_sensitive_text(str(exc), secret=token)}"
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=response.status_code,
                error_message=msg,
            )

        rows: list[list[Any]] = payload.get("values") or []
        if not rows:
            logger.info(
                "Google Sheet '%s' is empty (no header or data rows).",
                self._worksheet_name,
            )
            return SheetReadResult(
                success=True,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=response.status_code,
            )

        header_row = rows[0]
        col_indices, missing_cols = _resolve_header_indices(
            header_row=header_row,
            column_mapping=self._column_mapping,
        )
        if missing_cols:
            msg = (
                f"Missing required column header(s) in worksheet '{self._worksheet_name}': "
                f"{', '.join(missing_cols)}"
            )
            logger.error(msg)
            return SheetReadResult(
                success=False,
                spreadsheet_id=self._spreadsheet_id,
                worksheet_name=self._worksheet_name,
                status_code=response.status_code,
                error_message=msg,
            )

        applicants: list[Any] = []
        invalid_rows: list[SheetInvalidRow] = []
        skipped_empty = 0
        data_rows = rows[1:]

        for offset, raw_row in enumerate(data_rows):
            row_number = offset + 2  # Row 1 is headers; data starts at Row 2
            if not raw_row or all(not str(cell or "").strip() for cell in raw_row):
                skipped_empty += 1
                continue

            def _cell(field_key: str) -> str:
                idx = col_indices.get(field_key)
                if idx is None or idx >= len(raw_row):
                    return ""
                return str(raw_row[idx] or "").strip()

            raw_name = _cell("name")
            raw_email = _cell("email")
            raw_role = _cell("role")
            raw_cv_link = _cell("cv_link")
            raw_app_id = _cell("applicant_id") if "applicant_id" in col_indices else ""

            raw_map = {
                "name": raw_name,
                "email": raw_email,
                "role": raw_role,
                "cv_link": raw_cv_link,
            }

            row_errors: list[str] = []
            if not raw_name:
                row_errors.append("Missing Name")
            if not raw_email:
                row_errors.append("Missing Email")
            if not raw_role:
                row_errors.append("Missing Role")
            if not raw_cv_link:
                row_errors.append("Missing CV_Link")
            else:
                parsed_url = urlparse(raw_cv_link)
                if (
                    parsed_url.scheme.lower() not in ("http", "https")
                    or not parsed_url.netloc
                ):
                    row_errors.append(
                        "Invalid CV_Link (expected http:// or https:// URL)"
                    )

            if row_errors:
                reason = "; ".join(row_errors)
                logger.warning(
                    "Skipping invalid Google Sheet row %d: %s",
                    row_number,
                    reason,
                )
                invalid_rows.append(
                    SheetInvalidRow(
                        row_number=row_number,
                        reason=reason,
                        raw_values=raw_map,
                    )
                )
                continue

            derived_id = _derive_candidate_applicant_id(raw_app_id, raw_name)
            try:
                applicant_input = ApplicantInput(
                    applicant_id=derived_id,
                    name=raw_name,
                    email=raw_email,
                    role=raw_role,
                    cv_link=raw_cv_link,
                )
                applicants.append(applicant_input)
            except Exception as exc:
                reason = f"Validation failed: {redact_sensitive_text(str(exc))}"
                logger.warning(
                    "Skipping invalid Google Sheet row %d: %s",
                    row_number,
                    reason,
                )
                invalid_rows.append(
                    SheetInvalidRow(
                        row_number=row_number,
                        reason=reason,
                        raw_values=raw_map,
                    )
                )

        logger.info(
            "Read Google Sheet '%s': valid_applicants=%d invalid_rows=%d skipped_empty_rows=%d",
            self._worksheet_name,
            len(applicants),
            len(invalid_rows),
            skipped_empty,
        )
        return SheetReadResult(
            success=True,
            spreadsheet_id=self._spreadsheet_id,
            worksheet_name=self._worksheet_name,
            applicants=applicants,
            invalid_rows=invalid_rows,
            skipped_empty_rows=skipped_empty,
            total_data_rows=len(data_rows),
            status_code=response.status_code,
        )
