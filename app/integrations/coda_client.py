"""Coda REST API integration for reading existing applicants and inserting scoring records.

Security & reliability guarantees:
- Reads CODA_API_TOKEN from environment variables (never hardcoded or logged).
- Reads CODA_DOC_ID and CODA_TABLE_ID from configuration/environment.
- Maps all 12 required columns for the existing Applicants table.
- Supports both Scored and Failed record statuses.
- Redacts API tokens and Authorization Bearer credentials from all logs and error messages.
- Handles HTTP errors, timeouts, and rate limits (HTTP 429 / Retry-After) gracefully.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import math
import os
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import DEFAULT_CODA_COLUMN_MAPPING, Settings, get_settings

if TYPE_CHECKING:
    from app.services.scoring_engine import ScoringResult

logger = logging.getLogger(__name__)

_COLUMN_FIELD_ORDER: tuple[str, ...] = (
    "applicant_id",
    "name",
    "email",
    "role",
    "cv_link",
    "total_score",
    "sub_scores",
    "recommendation",
    "reason",
    "date_processed",
    "status",
    "error_note",
)

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_QUERY_TOKEN_PATTERN = re.compile(r"(?i)([?&](?:token|api_key|key|access_token)=)[^&\s]+")
_SHA256_LABELED_PATTERN = re.compile(r"(?i)cv_hash\s*[:=]\s*([a-f0-9]{64})")


class CodaApplicantStatus(str, Enum):
    """Allowed processing statuses written to or read from Coda."""

    SCORED = "Scored"
    FAILED = "Failed"
    PROCESSING = "Processing"


def redact_sensitive_text(text: str | None, secret: str | None = None) -> str:
    """Scrub API tokens and Bearer authorization headers from any error or log text."""
    if not text:
        return ""
    cleaned = str(text)
    if secret:
        stripped = secret.strip()
        if stripped:
            cleaned = cleaned.replace(stripped, "[REDACTED]")
    cleaned = _BEARER_PATTERN.sub("Bearer [REDACTED]", cleaned)
    cleaned = _QUERY_TOKEN_PATTERN.sub(r"\1[REDACTED]", cleaned)
    return cleaned


def normalize_email(email: str | None) -> str | None:
    """Normalize an email address by stripping whitespace and lowercasing."""
    if email is None:
        return None
    cleaned = str(email).strip().lower()
    return cleaned if cleaned else None


def _enum_or_str(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _is_valid_coda_scalar(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return True
    return False


def sanitize_coda_cell_value(value: Any, column_name: str = "") -> bool | int | float | str | list[Any]:
    """Coerce a cell value into a Coda-supported type (bool, number, string, or array).

    - None becomes "" (empty string; Coda rejects JSON null in POST /rows cells).
    - Nested mappings/dicts (e.g., Sub-scores/evidence) are serialized with json.dumps().
    - Lists/tuples have elements validated/serialized into supported scalars.
    """
    if isinstance(value, Enum):
        value = value.value

    if value is None:
        return ""

    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, default=str)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"Non-finite float value is not supported by Coda API for column '{column_name}': {value!r}"
            )
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, (list, tuple)):
        sanitized_items: list[bool | int | float | str] = []
        for idx, item in enumerate(value):
            if isinstance(item, Enum):
                item = item.value
            if item is None:
                sanitized_items.append("")
            elif isinstance(item, Mapping):
                sanitized_items.append(json.dumps(dict(item), ensure_ascii=False, default=str))
            elif _is_valid_coda_scalar(item):
                sanitized_items.append(item)
            else:
                raise ValueError(
                    f"Unsupported array element type in Coda column '{column_name}'[{idx}]: "
                    f"{type(item).__name__}"
                )
        return sanitized_items

    raise ValueError(
        f"Unsupported Coda cell value type for column '{column_name}': {type(value).__name__}. "
        "Coda requires boolean, number, string, or array."
    )


def validate_coda_rows_payload(payload: Mapping[str, Any]) -> None:
    """Validate Coda POST /rows payload structure and cell value types before sending HTTP request."""
    if not isinstance(payload, Mapping):
        raise ValueError("Coda payload must be a JSON object.")

    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError("Coda payload must contain a non-empty 'rows' list.")

    for row_idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"Coda payload rows[{row_idx}] must be an object.")
        cells = row.get("cells")
        if not isinstance(cells, list) or len(cells) == 0:
            raise ValueError(f"Coda payload rows[{row_idx}].cells must be a non-empty list.")

        for cell_idx, cell in enumerate(cells):
            if not isinstance(cell, Mapping):
                raise ValueError(f"Coda payload rows[{row_idx}].cells[{cell_idx}] must be an object.")
            column = cell.get("column")
            if not isinstance(column, str) or not column.strip():
                raise ValueError(
                    f"Coda payload rows[{row_idx}].cells[{cell_idx}].column must be a non-empty string."
                )
            if "value" not in cell:
                raise ValueError(
                    f"Coda payload rows[{row_idx}].cells[{cell_idx}] ({column}) is missing 'value'."
                )
            val = cell["value"]
            if _is_valid_coda_scalar(val):
                continue
            if isinstance(val, list):
                for item_idx, item in enumerate(val):
                    if not _is_valid_coda_scalar(item):
                        raise ValueError(
                            f"Invalid array element at rows[{row_idx}].cells[{cell_idx}].value[{item_idx}] "
                            f"for column '{column}': expected boolean, number, or string, got {type(item).__name__}."
                        )
                continue
            raise ValueError(
                f"Invalid cell value at rows[{row_idx}].cells[{cell_idx}] for column '{column}': "
                f"expected boolean, number, string, or array, got {type(val).__name__}."
            )


def validate_coda_row_update_payload(payload: Mapping[str, Any]) -> None:
    """Validate Coda PUT /rows/{rowId} payload structure and cell value types before HTTP request."""
    if not isinstance(payload, Mapping):
        raise ValueError("Coda row update payload must be a JSON object.")
    row = payload.get("row")
    if not isinstance(row, Mapping):
        raise ValueError("Coda row update payload must contain a 'row' object.")
    validate_coda_rows_payload({"rows": [row]})


def build_sub_scores_payload(
    scoring_result: ScoringResult | Mapping[str, Any] | Any | None = None,
    cv_hash: str | None = None,
) -> str:
    """Serialize criterion scores, CV evidence, and optional CV hash for the Sub-scores column."""
    normalized_hash = cv_hash.strip().lower() if cv_hash and cv_hash.strip() else None

    criteria_dict: dict[str, Any] = {}
    if scoring_result is not None and hasattr(scoring_result, "criterion_scores"):
        raw_map = getattr(scoring_result, "criterion_scores") or {}
        for name, item in raw_map.items():
            criteria_dict[name] = {
                "score": item.score,
                "weight": item.weight,
                "weighted_score": item.weighted_score,
                "evidence": item.evidence,
                "evidence_status": _enum_or_str(item.evidence_status),
            }
    elif isinstance(scoring_result, Mapping):
        raw_criteria = scoring_result.get("criterion_scores") or scoring_result.get("criteria") or scoring_result
        if isinstance(raw_criteria, Mapping):
            criteria_dict = dict(raw_criteria)

    payload: dict[str, Any] = {
        "criteria": criteria_dict,
    }
    if normalized_hash is not None:
        payload["cv_hash"] = normalized_hash

    return json.dumps(payload, ensure_ascii=False)


def extract_cv_hash_from_values(
    values: Mapping[str, Any],
    sub_scores_column: str = "Sub-scores",
) -> str | None:
    """Extract a stored CV SHA-256 hash from an existing Coda row's values."""
    for explicit_col in ("CV hash", "CV Hash", "cv_hash", "cv_sha256", "CV SHA-256"):
        raw = values.get(explicit_col)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()

    raw_sub_scores = (
        values.get(sub_scores_column)
        or values.get("Sub-scores")
        or values.get("sub_scores")
    )
    if isinstance(raw_sub_scores, Mapping):
        candidate = raw_sub_scores.get("cv_hash") or raw_sub_scores.get("cv_sha256")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    elif isinstance(raw_sub_scores, str) and raw_sub_scores.strip():
        try:
            parsed = json.loads(raw_sub_scores)
            if isinstance(parsed, dict):
                candidate = parsed.get("cv_hash") or parsed.get("cv_sha256")
                if not candidate and isinstance(parsed.get("metadata"), dict):
                    candidate = parsed["metadata"].get("cv_hash")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip().lower()
        except json.JSONDecodeError:
            match = _SHA256_LABELED_PATTERN.search(raw_sub_scores)
            if match:
                return match.group(1).lower()

    return None


class CodaApplicantRecord(BaseModel):
    """Typed representation of an applicant row mapped to the 12 Coda columns."""

    applicant_id: str
    name: str
    email: str
    role: str
    cv_link: str = ""
    total_score: float | None = Field(default=None, ge=0.0, le=100.0)
    sub_scores: str = ""
    recommendation: str = ""
    reason: str = ""
    date_processed: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: CodaApplicantStatus
    error_note: str = ""

    @field_validator("sub_scores", mode="before")
    @classmethod
    def _serialize_sub_scores(cls, value: Any) -> str:
        """Ensure nested Sub-scores/evidence dictionaries are JSON-serialized strings."""
        if value is None:
            return ""
        if isinstance(value, (Mapping, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    def to_coda_cells(self, column_mapping: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Map all 12 fields into Coda API cell dictionaries with validated scalar/array values."""
        mapping = dict(DEFAULT_CODA_COLUMN_MAPPING)
        if column_mapping:
            mapping.update(column_mapping)

        field_values: dict[str, Any] = {
            "applicant_id": self.applicant_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "cv_link": self.cv_link,
            "total_score": self.total_score,
            "sub_scores": self.sub_scores,
            "recommendation": self.recommendation,
            "reason": self.reason,
            "date_processed": self.date_processed,
            "status": self.status.value,
            "error_note": self.error_note,
        }

        return [
            {
                "column": mapping[field_name],
                "value": sanitize_coda_cell_value(
                    field_values[field_name],
                    column_name=mapping[field_name],
                ),
            }
            for field_name in _COLUMN_FIELD_ORDER
        ]

    @classmethod
    def from_scoring_result(
        cls,
        *,
        applicant_id: str,
        name: str,
        email: str,
        role: str,
        cv_link: str = "",
        scoring_result: ScoringResult | None = None,
        cv_hash: str | None = None,
        date_processed: str | None = None,
        error_note: str | None = None,
    ) -> CodaApplicantRecord:
        """Construct a Scored or Failed Coda record from a ScoringResult or error note."""
        timestamp = date_processed or datetime.now(timezone.utc).isoformat()
        sub_scores_json = build_sub_scores_payload(scoring_result=scoring_result, cv_hash=cv_hash)

        scoring_status = (
            _enum_or_str(getattr(scoring_result, "status", None))
            if scoring_result is not None
            else None
        )
        if scoring_result is not None and scoring_status == "success" and not error_note:
            recommendation_val = _enum_or_str(getattr(scoring_result, "recommendation", ""))
            return cls(
                applicant_id=applicant_id,
                name=name,
                email=email,
                role=role,
                cv_link=cv_link,
                total_score=scoring_result.total_score,
                sub_scores=sub_scores_json,
                recommendation=str(recommendation_val),
                reason=scoring_result.explanation,
                date_processed=timestamp,
                status=CodaApplicantStatus.SCORED,
                error_note="",
            )

        resolved_error = (
            error_note
            or (scoring_result.error_message if scoring_result is not None else None)
            or "Applicant processing failed."
        )
        resolved_reason = (
            scoring_result.explanation
            if scoring_result is not None and scoring_result.explanation
            else "Applicant processing could not be completed."
        )
        return cls(
            applicant_id=applicant_id,
            name=name,
            email=email,
            role=role,
            cv_link=cv_link,
            total_score=None,
            sub_scores=sub_scores_json,
            recommendation="",
            reason=resolved_reason,
            date_processed=timestamp,
            status=CodaApplicantStatus.FAILED,
            error_note=redact_sensitive_text(resolved_error),
        )


class CodaExistingRow(BaseModel):
    """Normalized view of an existing row returned from Coda."""

    row_id: str | None = None
    applicant_id: str | None = None
    email: str | None = None
    cv_hash: str | None = None
    status: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class CodaFetchResult(BaseModel):
    """Structured result for listing existing applicant records from Coda."""

    success: bool
    records: list[CodaExistingRow] = Field(default_factory=list)
    status_code: int | None = None
    error_message: str | None = None


class CodaInsertResult(BaseModel):
    """Structured result for inserting an applicant record into Coda."""

    success: bool
    status_code: int | None = None
    request_id: str | None = None
    added_row_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None


def _parse_retry_after(header_value: str | None, default_delay: float) -> float:
    if not header_value:
        return default_delay
    try:
        parsed = float(header_value.strip())
        return max(0.0, parsed)
    except ValueError:
        return default_delay


class CodaClient:
    """Modular HTTP client for integrating with Coda's Applicants table."""

    def __init__(
        self,
        settings: Settings | None = None,
        api_token: str | None = None,
        doc_id: str | None = None,
        table_id: str | None = None,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._explicit_api_token = api_token
        self._explicit_doc_id = doc_id
        self._explicit_table_id = table_id
        self._base_url = (base_url or self._settings.coda.base_url).rstrip("/")
        self._http_client = http_client
        self._timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self._settings.coda.timeout_seconds)
        )
        self._max_retries = max(0, int(max_retries))
        self._retry_delay = max(0.0, float(retry_delay))
        self._sleep_fn = sleep_fn

    def __repr__(self) -> str:
        return (
            f"CodaClient(base_url={self._base_url!r}, "
            f"doc_id={self.doc_id!r}, table_id={self.table_id!r})"
        )

    @property
    def column_mapping(self) -> dict[str, str]:
        """Return the active 12-column mapping for Coda."""
        mapping = dict(DEFAULT_CODA_COLUMN_MAPPING)
        mapping.update(self._settings.coda.column_mapping)
        return mapping

    @property
    def doc_id(self) -> str | None:
        """Resolve CODA_DOC_ID from explicit override, environment, or YAML configuration."""
        for candidate in (
            self._explicit_doc_id,
            os.getenv("CODA_DOC_ID"),
            self._settings.env.coda_doc_id,
            self._settings.coda.doc_id,
        ):
            if candidate and candidate.strip():
                return candidate.strip()
        return None

    @property
    def table_id(self) -> str | None:
        """Resolve CODA_TABLE_ID from explicit override, environment, or YAML configuration."""
        for candidate in (
            self._explicit_table_id,
            os.getenv("CODA_TABLE_ID"),
            self._settings.env.coda_table_id,
            self._settings.coda.table_id,
        ):
            if candidate and candidate.strip():
                return candidate.strip()
        return None

    def _resolve_api_token(self) -> str | None:
        if self._explicit_api_token and self._explicit_api_token.strip():
            return self._explicit_api_token.strip()
        env_token = os.getenv("CODA_API_TOKEN")
        if env_token and env_token.strip():
            return env_token.strip()
        if self._settings.env.coda_api_token is not None:
            secret_val = self._settings.env.coda_api_token.get_secret_value().strip()
            if secret_val:
                return secret_val
        return None

    def _validate_configuration(self) -> tuple[str, str, str]:
        token = self._resolve_api_token()
        if not token:
            raise ValueError(
                "Missing CODA_API_TOKEN environment variable. Set CODA_API_TOKEN before calling Coda API."
            )
        resolved_doc_id = self.doc_id
        if not resolved_doc_id:
            raise ValueError(
                "Missing CODA_DOC_ID configuration. Set CODA_DOC_ID in environment or config.yaml."
            )
        resolved_table_id = self.table_id
        if not resolved_table_id:
            raise ValueError(
                "Missing CODA_TABLE_ID configuration. Set CODA_TABLE_ID in environment or config.yaml."
            )
        return token, resolved_doc_id, resolved_table_id

    def _rows_url(self, doc_id: str, table_id: str) -> str:
        return f"{self._base_url}/docs/{doc_id}/tables/{table_id}/rows"

    def _row_url(self, doc_id: str, table_id: str, row_id: str) -> str:
        return f"{self._base_url}/docs/{doc_id}/tables/{table_id}/rows/{row_id}"

    def _request_with_rate_limit(
        self,
        method: str,
        url: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request against Coda API with rate-limit (429) retries."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        for attempt in range(self._max_retries + 1):
            if self._http_client is not None:
                response = self._http_client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )

            if response.status_code == 429:
                if attempt < self._max_retries:
                    delay = _parse_retry_after(
                        response.headers.get("Retry-After"),
                        self._retry_delay * (2**attempt),
                    )
                    logger.warning(
                        "Coda API rate limit hit (HTTP 429). Retrying attempt %d/%d after %.2fs.",
                        attempt + 1,
                        self._max_retries,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                return response

            return response

        raise RuntimeError("Coda API request failed after retries.")

    def _safe_error_message(
        self,
        exc_or_msg: Exception | str,
        token: str | None = None,
    ) -> str:
        return redact_sensitive_text(str(exc_or_msg), secret=token)

    def _extract_http_error(self, response: httpx.Response, token: str) -> str:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("statusMessage")
                    or payload.get("error")
                    or ""
                )
        except Exception:
            detail = response.text[:300] if response.text else ""

        base_msg = f"Coda API returned HTTP {response.status_code}"
        if response.status_code == 429:
            base_msg = "Coda API rate limit exceeded (HTTP 429)"
        if detail:
            base_msg = f"{base_msg}: {detail}"
        return self._safe_error_message(base_msg, token=token)

    def list_existing_records(self, limit: int = 100) -> CodaFetchResult:
        """Fetch existing applicant rows from Coda without modifying any records."""
        token: str | None = None
        try:
            token, resolved_doc_id, resolved_table_id = self._validate_configuration()
        except ValueError as exc:
            err = self._safe_error_message(exc)
            logger.error("Coda configuration error during list_existing_records: %s", err)
            return CodaFetchResult(success=False, records=[], error_message=err)

        url = self._rows_url(resolved_doc_id, resolved_table_id)
        col_map = self.column_mapping
        email_col = col_map["email"]
        applicant_id_col = col_map["applicant_id"]
        sub_scores_col = col_map["sub_scores"]
        status_col = col_map["status"]

        collected: list[CodaExistingRow] = []
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {
                "useColumnNames": "true",
                "valueFormat": "simple",
                "limit": limit,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                response = self._request_with_rate_limit(
                    "GET", url, token, params=params
                )
            except httpx.TimeoutException as exc:
                err = self._safe_error_message(
                    f"Coda API request timed out after {self._timeout_seconds}s: {exc}",
                    token=token,
                )
                logger.error("%s", err)
                return CodaFetchResult(success=False, records=[], error_message=err)
            except httpx.RequestError as exc:
                err = self._safe_error_message(
                    f"Coda API network error: {exc}", token=token
                )
                logger.error("%s", err)
                return CodaFetchResult(success=False, records=[], error_message=err)
            except Exception as exc:
                err = self._safe_error_message(
                    f"Unexpected Coda API error: {exc}", token=token
                )
                logger.error("%s", err)
                return CodaFetchResult(success=False, records=[], error_message=err)

            if response.status_code >= 400:
                err = self._extract_http_error(response, token=token)
                logger.error("%s", err)
                return CodaFetchResult(
                    success=False,
                    records=[],
                    status_code=response.status_code,
                    error_message=err,
                )

            try:
                body = response.json()
            except Exception as exc:
                err = self._safe_error_message(
                    f"Invalid JSON returned by Coda API: {exc}", token=token
                )
                logger.error("%s", err)
                return CodaFetchResult(
                    success=False,
                    records=[],
                    status_code=response.status_code,
                    error_message=err,
                )

            items = body.get("items", []) if isinstance(body, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                values = item.get("values") or {}
                if not isinstance(values, dict):
                    values = {}

                raw_email = values.get(email_col) or values.get("Email") or values.get("email")
                raw_applicant_id = (
                    values.get(applicant_id_col)
                    or values.get("Applicant ID")
                    or values.get("applicant_id")
                )
                raw_status = (
                    values.get(status_col)
                    or values.get("Status")
                    or values.get("status")
                )
                collected.append(
                    CodaExistingRow(
                        row_id=str(item["id"]) if item.get("id") is not None else None,
                        applicant_id=str(raw_applicant_id).strip()
                        if raw_applicant_id is not None and str(raw_applicant_id).strip()
                        else None,
                        email=normalize_email(str(raw_email)) if raw_email is not None else None,
                        cv_hash=extract_cv_hash_from_values(values, sub_scores_column=sub_scores_col),
                        status=str(raw_status).strip()
                        if raw_status is not None and str(raw_status).strip()
                        else None,
                        values=values,
                    )
                )

            next_token = body.get("nextPageToken") if isinstance(body, dict) else None
            if next_token:
                page_token = str(next_token)
            else:
                break

        return CodaFetchResult(success=True, records=collected, status_code=200)

    def insert_applicant_record(self, record: CodaApplicantRecord) -> CodaInsertResult:
        """Insert a single applicant record into the Coda Applicants table (no upsert/overwrite)."""
        token: str | None = None
        try:
            token, resolved_doc_id, resolved_table_id = self._validate_configuration()
        except ValueError as exc:
            err = self._safe_error_message(exc)
            logger.error("Coda configuration error during insert_applicant_record: %s", err)
            return CodaInsertResult(success=False, error_message=err)

        url = self._rows_url(resolved_doc_id, resolved_table_id)
        try:
            payload = {
                "rows": [
                    {
                        "cells": record.to_coda_cells(self.column_mapping),
                    }
                ]
            }
            validate_coda_rows_payload(payload)
        except ValueError as exc:
            err = self._safe_error_message(
                f"Coda payload validation failed before API request: {exc}",
                token=token,
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)

        try:
            response = self._request_with_rate_limit("POST", url, token, json_body=payload)
        except httpx.TimeoutException as exc:
            err = self._safe_error_message(
                f"Coda API insert timed out after {self._timeout_seconds}s: {exc}",
                token=token,
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)
        except httpx.RequestError as exc:
            err = self._safe_error_message(
                f"Coda API insert network error: {exc}", token=token
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)
        except Exception as exc:
            err = self._safe_error_message(
                f"Unexpected Coda API insert error: {exc}", token=token
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)

        if response.status_code >= 400:
            err = self._extract_http_error(response, token=token)
            logger.error("%s", err)
            return CodaInsertResult(
                success=False,
                status_code=response.status_code,
                error_message=err,
            )

        request_id: str | None = None
        added_row_ids: list[str] = []
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("requestId") is not None:
                    request_id = str(body["requestId"])
                raw_added = body.get("addedRowIds")
                if isinstance(raw_added, list):
                    added_row_ids = [str(row_id) for row_id in raw_added]
        except Exception:
            # 200/201/202 without JSON body still counts as accepted insert
            pass

        return CodaInsertResult(
            success=True,
            status_code=response.status_code,
            request_id=request_id,
            added_row_ids=added_row_ids,
        )

    def update_applicant_record(
        self,
        row_id: str,
        record: CodaApplicantRecord,
    ) -> CodaInsertResult:
        """Update an existing Coda row in-place via PUT /rows/{rowId} without creating duplicates."""
        if not row_id or not str(row_id).strip():
            return CodaInsertResult(
                success=False,
                error_message="Missing Coda row_id for update_applicant_record.",
            )

        token: str | None = None
        try:
            token, resolved_doc_id, resolved_table_id = self._validate_configuration()
        except ValueError as exc:
            err = self._safe_error_message(exc)
            logger.error("Coda configuration error during update_applicant_record: %s", err)
            return CodaInsertResult(success=False, error_message=err)

        cleaned_row_id = str(row_id).strip()
        url = self._row_url(resolved_doc_id, resolved_table_id, cleaned_row_id)
        try:
            payload = {
                "row": {
                    "cells": record.to_coda_cells(self.column_mapping),
                }
            }
            validate_coda_row_update_payload(payload)
        except ValueError as exc:
            err = self._safe_error_message(
                f"Coda payload validation failed before update request: {exc}",
                token=token,
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)

        try:
            response = self._request_with_rate_limit("PUT", url, token, json_body=payload)
        except httpx.TimeoutException as exc:
            err = self._safe_error_message(
                f"Coda API update timed out after {self._timeout_seconds}s: {exc}",
                token=token,
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)
        except httpx.RequestError as exc:
            err = self._safe_error_message(
                f"Coda API update network error: {exc}", token=token
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)
        except Exception as exc:
            err = self._safe_error_message(
                f"Unexpected Coda API update error: {exc}", token=token
            )
            logger.error("%s", err)
            return CodaInsertResult(success=False, error_message=err)

        if response.status_code >= 400:
            err = self._extract_http_error(response, token=token)
            logger.error("%s", err)
            return CodaInsertResult(
                success=False,
                status_code=response.status_code,
                error_message=err,
            )

        request_id: str | None = None
        updated_id = cleaned_row_id
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("requestId") is not None:
                    request_id = str(body["requestId"])
                if body.get("id") is not None:
                    updated_id = str(body["id"])
        except Exception:
            pass

        return CodaInsertResult(
            success=True,
            status_code=response.status_code,
            request_id=request_id,
            added_row_ids=[updated_id],
        )
