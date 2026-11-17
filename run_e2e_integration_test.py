"""Local end-to-end integration test runner for 3 selected applicants.

Tests the unmodified production pipeline (`ApplicantPipeline`) end-to-end using:
  - 3 selected applicant records from `applicants_manifest.csv`
  - Their actual CV files from `drive_upload/CVs/`
  - CVParser (text extraction)
  - DuplicateDetector (Coda read + SHA-256 + normalized email check)
  - ScoringEngine (Groq openai/gpt-oss-120b + deterministic Python weighting)
  - CodaClient (12-column mapping + insertion + readback verification)

Defaults to DRY_RUN=true so Coda writes never occur accidentally unless explicitly
enabled via `--live` or `DRY_RUN=false`. Never prints or logs API keys or tokens.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.integrations.coda_client import (
    CodaApplicantRecord,
    CodaClient,
    CodaExistingRow,
    CodaFetchResult,
    CodaInsertResult,
    redact_sensitive_text,
)
from app.services.cv_parser import CVParseResult, CVParser, ExtractionStatus
from app.services.duplicate_detector import (
    DuplicateCheckResult,
    DuplicateDetector,
)
from app.services.pipeline import (
    ApplicantPipeline,
    DownloadResult,
    GoogleDriveDownloader,
    PipelineResult,
    PipelineStatus,
)
from app.services.scoring_engine import Recommendation, ScoringEngine, ScoringResult

WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST_CSV = WORKSPACE_ROOT / "applicants_manifest.csv"
DEFAULT_DRIVE_CVS_DIR = WORKSPACE_ROOT / "drive_upload" / "CVs"
DEFAULT_APPLICANT_IDS: tuple[str, ...] = ("APP-001", "APP-002", "APP-004")
MAX_INTEGRATION_APPLICANTS = 3

REQUIRED_CODA_COLUMNS = [
    "Applicant ID",
    "Name",
    "Email",
    "Role",
    "CV Link",
    "Total Score",
    "Sub-scores",
    "Recommendation",
    "Reason",
    "Date Processed",
    "Status",
    "Error Note",
]


class StageMetrics(BaseModel):
    """Per-applicant stage timings, verification results, and errors (secret-free)."""

    applicant_id: str
    pipeline_status: str | None = None
    duplicate_checked: bool = False
    is_duplicate: bool = False
    is_retry: bool = False
    existing_status: str | None = None
    duplicate_reason: str | None = None
    parse_status: str | None = None
    extracted_chars: int = 0
    total_score: float | None = None
    recommendation: str | None = None
    criteria_verified: bool = False
    evidence_verified: bool = False
    coda_columns_verified: bool = False
    coda_write_attempted: bool = False
    coda_write_succeeded: bool = False
    coda_updated: bool = False
    coda_readback_verified: bool = False
    coda_row_ids: list[str] = Field(default_factory=list)
    parse_duration_s: float = 0.0
    dedup_duration_s: float = 0.0
    scoring_duration_s: float = 0.0
    coda_duration_s: float = 0.0
    total_duration_s: float = 0.0
    errors: list[str] = Field(default_factory=list)


class IntegrationTestSummary(BaseModel):
    """Summary returned by run_integration_test()."""

    dry_run: bool
    selected_applicant_ids: list[str]
    total_duration_s: float
    passed: bool
    applicant_metrics: list[StageMetrics]


class _SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts any configured secret strings from log records."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        scrubbed = _redact_all(rendered, self._secrets)
        record.msg = scrubbed
        record.args = ()
        if record.exc_info:
            record.exc_info = None
        return True


def _redact_all(text: str | None, secrets: list[str]) -> str:
    cleaned = str(text) if text else ""
    for secret in secrets:
        cleaned = redact_sensitive_text(cleaned, secret=secret)
    return redact_sensitive_text(cleaned)


def _collect_secrets(settings: Settings) -> list[str]:
    candidates = [
        os.getenv("GROQ_API_KEY"),
        os.getenv("CODA_API_TOKEN"),
        os.getenv("WEBHOOK_AUTH_TOKEN"),
    ]
    for sec_obj in (
        settings.env.groq_api_key,
        settings.env.coda_api_token,
        settings.env.webhook_auth_token,
    ):
        if sec_obj is not None:
            candidates.append(sec_obj.get_secret_value())
    return [c.strip() for c in candidates if c and c.strip()]


def _parse_dry_run_env(default: bool = True) -> bool:
    raw = os.getenv("DRY_RUN")
    if raw is None or not raw.strip():
        return default
    cleaned = raw.strip().lower()
    if cleaned in ("true", "1", "yes", "on"):
        return True
    if cleaned in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"Invalid DRY_RUN environment value: {raw!r}. Expected true/false.")


def load_selected_applicants(
    manifest_csv: Path,
    drive_cvs_dir: Path,
    applicant_ids: list[str],
) -> list[dict[str, str]]:
    """Load the selected applicants (max 3) from applicants_manifest.csv and resolve drive_upload/CVs files."""
    if not applicant_ids or len(applicant_ids) > MAX_INTEGRATION_APPLICANTS:
        raise ValueError(
            f"Must select between 1 and {MAX_INTEGRATION_APPLICANTS} applicants "
            f"(got {len(applicant_ids)}). Do not process all 40 applicants in this test."
        )

    if not manifest_csv.is_file():
        raise FileNotFoundError(f"Manifest CSV not found: {manifest_csv}")
    if not drive_cvs_dir.is_dir():
        raise FileNotFoundError(f"Drive upload CVs directory not found: {drive_cvs_dir}")

    with manifest_csv.open("r", encoding="utf-8", newline="") as handle:
        by_id = {row["applicant_id"].strip(): row for row in csv.DictReader(handle)}

    selected: list[dict[str, str]] = []
    for app_id in applicant_ids:
        row = by_id.get(app_id)
        if row is None:
            raise KeyError(f"Applicant ID '{app_id}' not found in {manifest_csv}")

        local_cv = (drive_cvs_dir / f"{app_id}.docx").resolve()
        if not local_cv.is_file():
            raise FileNotFoundError(
                f"Expected CV file for {app_id} not found at {local_cv}. "
                "Run `python prepare_drive_upload.py` first."
            )

        selected.append(
            {
                "applicant_id": app_id,
                "name": row["name"].strip(),
                "email": row["email"].strip(),
                "role": row["role"].strip(),
                "cv_link": local_cv.as_posix(),
            }
        )
    return selected


class LocalDriveUploadDownloader(GoogleDriveDownloader):
    """Copies local CV files from drive_upload/CVs/ into the pipeline temp directory."""

    def __init__(self, drive_cvs_dir: Path) -> None:
        super().__init__()
        self._drive_cvs_dir = Path(drive_cvs_dir).resolve()

    def download(
        self,
        cv_link: str,
        target_dir: Path,
        applicant_id: str = "applicant",
    ) -> DownloadResult:
        candidate_path = Path(cv_link)
        if not candidate_path.is_file():
            candidate_path = self._drive_cvs_dir / f"{applicant_id}.docx"
        if not candidate_path.is_file():
            return DownloadResult(
                success=False,
                error_message=f"Local CV file not found: {candidate_path}",
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (target_dir / candidate_path.name).resolve()
        shutil.copy2(candidate_path, dest_path)
        return DownloadResult(
            success=True,
            file_path=dest_path,
            filename=dest_path.name,
        )


class SpyCVParser(CVParser):
    """Wraps CVParser to record parsing timings and status without altering behavior."""

    def __init__(self, metrics_by_id: dict[str, StageMetrics]) -> None:
        super().__init__()
        self._metrics = metrics_by_id
        self.current_applicant_id: str | None = None

    def parse(self, file_path: str | Path) -> CVParseResult:
        t0 = time.perf_counter()
        result = super().parse(file_path)
        elapsed = round(time.perf_counter() - t0, 4)

        if self.current_applicant_id and self.current_applicant_id in self._metrics:
            m = self._metrics[self.current_applicant_id]
            m.parse_duration_s = elapsed
            m.parse_status = result.extraction_status.value
            m.extracted_chars = len(result.extracted_text or "")
            if result.extraction_status != ExtractionStatus.SUCCESS and result.error_message:
                m.errors.append(f"CVParser: {result.error_message}")
        return result


class SpyDuplicateDetector(DuplicateDetector):
    """Wraps DuplicateDetector to record deduplication timings and results."""

    def __init__(self, coda_client: CodaClient, metrics_by_id: dict[str, StageMetrics]) -> None:
        super().__init__(coda_client=coda_client)
        self._metrics = metrics_by_id

    def check_applicant(
        self,
        *,
        email: str | None = None,
        cv_path: str | Path | None = None,
        cv_hash: str | None = None,
        applicant_id: str | None = None,
        refresh: bool = True,
    ) -> DuplicateCheckResult:
        t0 = time.perf_counter()
        result = super().check_applicant(
            email=email,
            cv_path=cv_path,
            cv_hash=cv_hash,
            applicant_id=applicant_id,
            refresh=refresh,
        )
        elapsed = round(time.perf_counter() - t0, 4)

        if applicant_id and applicant_id in self._metrics:
            m = self._metrics[applicant_id]
            m.dedup_duration_s = elapsed
            m.duplicate_checked = result.check_status == "ok"
            m.is_duplicate = result.is_duplicate
            m.is_retry = result.is_retry
            m.existing_status = result.existing_status
            m.duplicate_reason = result.reason.value if result.reason else None
            if result.check_status != "ok" and result.error_message:
                m.errors.append(f"DuplicateDetector: {result.error_message}")
        return result


class SpyScoringEngine(ScoringEngine):
    """Wraps ScoringEngine to verify Groq criterion scores, weights, recommendation, and evidence."""

    def __init__(
        self,
        settings: Settings,
        metrics_by_id: dict[str, StageMetrics],
        secrets: list[str],
        client: Any | None = None,
    ) -> None:
        super().__init__(settings=settings, client=client)
        self._metrics = metrics_by_id
        self._secrets = secrets
        self.current_applicant_id: str | None = None

    def score_cv(self, cv_text: str | None) -> ScoringResult:
        t0 = time.perf_counter()
        result = super().score_cv(cv_text)
        elapsed = round(time.perf_counter() - t0, 4)

        if self.current_applicant_id and self.current_applicant_id in self._metrics:
            m = self._metrics[self.current_applicant_id]
            m.scoring_duration_s = elapsed
            m.total_score = result.total_score
            m.recommendation = result.recommendation.value if result.recommendation is not None else None
            m.criteria_verified = _verify_scoring_criteria(result, self.weights)
            m.evidence_verified = bool(result.criterion_scores) and all(
                bool(crit.evidence and crit.evidence.strip())
                for crit in result.criterion_scores.values()
            )
            if result.status.value != "success" and result.error_message:
                m.errors.append(f"ScoringEngine: {_redact_all(result.error_message, self._secrets)}")
        return result


def _verify_scoring_criteria(result: ScoringResult, expected_weights: dict[str, float]) -> bool:
    if set(result.criterion_scores.keys()) != set(expected_weights.keys()):
        return False
    if result.total_score is None or not (0.0 <= result.total_score <= 100.0):
        return False
    if result.recommendation not in (
        Recommendation.SHORTLIST,
        Recommendation.MAYBE,
        Recommendation.REJECT,
    ):
        return False
    return bool(result.explanation and result.explanation.strip())


def _verify_coda_record_columns(
    record: CodaApplicantRecord,
    column_mapping: dict[str, str],
) -> tuple[bool, list[str]]:
    """Verify all 12 required columns, Sub-scores evidence JSON, score, and recommendation."""
    issues: list[str] = []
    cells = record.to_coda_cells(column_mapping)
    cell_names = [cell["column"] for cell in cells]
    cell_map = {cell["column"]: cell["value"] for cell in cells}

    if cell_names != REQUIRED_CODA_COLUMNS:
        issues.append(f"Column mismatch: expected {REQUIRED_CODA_COLUMNS}, got {cell_names}")

    if cell_map.get("Status") not in ("Scored", "Failed"):
        issues.append(f"Invalid Status column value: {cell_map.get('Status')!r}")

    if cell_map.get("Status") == "Scored":
        score = cell_map.get("Total Score")
        if score is None or not (0.0 <= float(score) <= 100.0):
            issues.append(f"Invalid Total Score for Scored row: {score!r}")
        if cell_map.get("Recommendation") not in ("Shortlist", "Maybe", "Reject"):
            issues.append(f"Invalid Recommendation: {cell_map.get('Recommendation')!r}")
        if not str(cell_map.get("Reason") or "").strip():
            issues.append("Empty Reason on Scored row")

    try:
        sub_scores = json.loads(str(cell_map.get("Sub-scores") or "{}"))
        criteria = sub_scores.get("criteria", {})
        if cell_map.get("Status") == "Scored":
            if not criteria:
                issues.append("Sub-scores.criteria is empty on Scored row")
            for crit_name, crit_val in criteria.items():
                if not str(crit_val.get("evidence") or "").strip():
                    issues.append(f"Missing evidence in Sub-scores for criterion {crit_name}")
    except Exception as exc:
        issues.append(f"Sub-scores is not valid JSON: {exc}")

    return (len(issues) == 0), issues


class VerifyingCodaClient(CodaClient):
    """Wraps CodaClient to verify 12-column mapping and honor DRY_RUN without modifying CodaClient."""

    def __init__(
        self,
        *,
        settings: Settings,
        dry_run: bool,
        metrics_by_id: dict[str, StageMetrics],
        secrets: list[str],
        http_client: Any | None = None,
        logger_inst: logging.Logger,
    ) -> None:
        super().__init__(settings=settings, http_client=http_client)
        self._dry_run = dry_run
        self._metrics = metrics_by_id
        self._secrets = secrets
        self._logger = logger_inst
        self.inserted_records: dict[str, CodaApplicantRecord] = {}

    def insert_applicant_record(self, record: CodaApplicantRecord) -> CodaInsertResult:
        t0 = time.perf_counter()
        cols_ok, col_issues = _verify_coda_record_columns(record, self.column_mapping)
        metrics = self._metrics.get(record.applicant_id)
        if metrics is not None:
            metrics.coda_columns_verified = cols_ok
            for issue in col_issues:
                metrics.errors.append(f"CodaColumnValidation: {issue}")

        if self._dry_run:
            elapsed = round(time.perf_counter() - t0, 4)
            if metrics is not None:
                metrics.coda_duration_s = elapsed
                metrics.coda_write_attempted = False
                metrics.coda_write_succeeded = False
            self._logger.info(
                "[DRY_RUN] Verified 12-column Coda payload for %s (Status=%s, Total score=%s, "
                "Recommendation=%s); skipping Coda POST write.",
                record.applicant_id,
                record.status.value,
                record.total_score,
                record.recommendation or "-",
            )
            self.inserted_records[record.applicant_id] = record
            return CodaInsertResult(
                success=True,
                status_code=202,
                request_id="dry-run-no-write",
                added_row_ids=[],
            )

        if metrics is not None:
            metrics.coda_write_attempted = True

        result = super().insert_applicant_record(record)
        elapsed = round(time.perf_counter() - t0, 4)

        if metrics is not None:
            metrics.coda_duration_s = elapsed
            metrics.coda_write_succeeded = result.success
            metrics.coda_row_ids = list(result.added_row_ids)
            if not result.success and result.error_message:
                metrics.errors.append(f"CodaInsert: {_redact_all(result.error_message, self._secrets)}")

        if result.success:
            self.inserted_records[record.applicant_id] = record

        return result

    def update_applicant_record(
        self,
        row_id: str,
        record: CodaApplicantRecord,
    ) -> CodaInsertResult:
        t0 = time.perf_counter()
        cols_ok, col_issues = _verify_coda_record_columns(record, self.column_mapping)
        metrics = self._metrics.get(record.applicant_id)
        if metrics is not None:
            metrics.coda_columns_verified = cols_ok
            for issue in col_issues:
                metrics.errors.append(f"CodaColumnValidation: {issue}")

        if self._dry_run:
            elapsed = round(time.perf_counter() - t0, 4)
            if metrics is not None:
                metrics.coda_duration_s = elapsed
                metrics.coda_write_attempted = False
                metrics.coda_write_succeeded = False
                metrics.coda_updated = True
                metrics.coda_row_ids = [row_id]
            self._logger.info(
                "[DRY_RUN] Verified 12-column Coda UPDATE payload for %s (row_id=%s, Status=%s, "
                "Total Score=%s, Recommendation=%s); skipping Coda PUT write.",
                record.applicant_id,
                row_id,
                record.status.value,
                record.total_score,
                record.recommendation or "-",
            )
            self.inserted_records[record.applicant_id] = record
            return CodaInsertResult(
                success=True,
                status_code=202,
                request_id="dry-run-no-update",
                added_row_ids=[row_id],
            )

        if metrics is not None:
            metrics.coda_write_attempted = True

        result = super().update_applicant_record(row_id, record)
        elapsed = round(time.perf_counter() - t0, 4)

        if metrics is not None:
            metrics.coda_duration_s = elapsed
            metrics.coda_write_succeeded = result.success
            metrics.coda_updated = result.success
            metrics.coda_row_ids = list(result.added_row_ids)
            if not result.success and result.error_message:
                metrics.errors.append(f"CodaUpdate: {_redact_all(result.error_message, self._secrets)}")

        if result.success:
            self.inserted_records[record.applicant_id] = record

        return result


def _verify_coda_readback(
    coda_client: VerifyingCodaClient,
    expected_records: dict[str, CodaApplicantRecord],
    metrics_by_id: dict[str, StageMetrics],
    logger_inst: logging.Logger,
    max_attempts: int = 5,
    poll_interval_s: float = 2.0,
) -> None:
    """Poll Coda GET .../rows to confirm newly inserted Scored/Failed records materialized in Coda."""
    if not expected_records:
        return

    pending_ids = set(expected_records.keys())
    found_rows: dict[str, CodaExistingRow] = {}

    for attempt in range(1, max_attempts + 1):
        fetch_res: CodaFetchResult = coda_client.list_existing_records()
        if not fetch_res.success:
            logger_inst.warning(
                "Coda readback verification attempt %d/%d failed: %s",
                attempt,
                max_attempts,
                fetch_res.error_message,
            )
        else:
            for row in fetch_res.records:
                if row.applicant_id in expected_records:
                    found_rows[row.applicant_id] = row
                    pending_ids.discard(row.applicant_id)

        if not pending_ids:
            break
        if attempt < max_attempts:
            time.sleep(poll_interval_s)

    for app_id, expected_rec in expected_records.items():
        metrics = metrics_by_id[app_id]
        row = found_rows.get(app_id)
        if row is None:
            metrics.coda_readback_verified = False
            metrics.errors.append(
                f"CodaReadback: Row for {app_id} not observed in Coda after {max_attempts} attempt(s)."
            )
            continue

        values = row.values
        mismatches: list[str] = []
        if str(values.get("Status") or "").strip() != expected_rec.status.value:
            mismatches.append(f"Status={values.get('Status')!r} != {expected_rec.status.value!r}")
        if expected_rec.status.value == "Scored":
            raw_score = values.get("Total Score")
            if raw_score is None or str(raw_score).strip() == "":
                mismatches.append("Total Score is missing in Coda")
            elif abs(float(raw_score) - float(expected_rec.total_score or 0.0)) > 0.05:
                mismatches.append(f"Total Score={raw_score!r} != {expected_rec.total_score!r}")
            if str(values.get("Recommendation") or "").strip() != expected_rec.recommendation:
                mismatches.append(
                    f"Recommendation={values.get('Recommendation')!r} != {expected_rec.recommendation!r}"
                )
            if not str(values.get("Reason") or "").strip():
                mismatches.append("Reason is blank in Coda")
            if not str(values.get("Sub-scores") or "").strip():
                mismatches.append("Sub-scores is blank in Coda")

        if mismatches:
            metrics.coda_readback_verified = False
            metrics.errors.append(f"CodaReadback mismatch for {app_id}: {', '.join(mismatches)}")
        else:
            metrics.coda_readback_verified = True
            logger_inst.info(
                "Verified Coda row persisted for %s (row_id=%s, Status=%s, Score=%s, Rec=%s)",
                app_id,
                row.row_id,
                expected_rec.status.value,
                expected_rec.total_score,
                expected_rec.recommendation,
            )


class _DryRunMockGroqClient:
    """Deterministic local mock Groq client used in DRY_RUN mode to prevent real API calls."""

    class _Completions:
        def create(
            self,
            *,
            model: str,
            messages: list[dict[str, str]],
            temperature: float = 0.0,
            response_format: dict[str, Any] | None = None,
        ) -> Any:
            user_content = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
            has_cv = "<CV_TEXT>" in user_content
            evidence_note = (
                "Dry-run mock evaluation grounded on extracted CV text."
                if has_cv
                else "Insufficient evidence in CV"
            )
            payload = {
                "criteria": {
                    "skills_match": {
                        "score": 80,
                        "evidence": evidence_note,
                        "evidence_status": "supported" if has_cv else "insufficient",
                    },
                    "experience_relevance": {
                        "score": 76,
                        "evidence": evidence_note,
                        "evidence_status": "supported" if has_cv else "insufficient",
                    },
                    "education_fit": {
                        "score": 75,
                        "evidence": "Insufficient evidence in CV",
                        "evidence_status": "insufficient",
                    },
                    "overall_alignment": {
                        "score": 78,
                        "evidence": evidence_note,
                        "evidence_status": "supported" if has_cv else "insufficient",
                    },
                },
                "explanation": (
                    "[DRY_RUN] Simulated Groq evaluation verifying deterministic Python "
                    "weighting, threshold classification, and 12-column Coda mapping."
                ),
            }
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    class _Chat:
        def __init__(self) -> None:
            self.completions = _DryRunMockGroqClient._Completions()

    def __init__(self) -> None:
        self.chat = self._Chat()


def _check_required_env_vars(
    dry_run: bool,
    live_groq: bool,
    logger_inst: logging.Logger,
) -> None:
    """Log environment variable readiness by name only (never exposing values)."""
    all_vars = ["GROQ_API_KEY", "CODA_API_TOKEN", "CODA_DOC_ID", "CODA_TABLE_ID"]
    required_vars = (
        all_vars
        if (not dry_run or live_groq)
        else ["CODA_API_TOKEN", "CODA_DOC_ID", "CODA_TABLE_ID"]
    )
    missing = []
    for var_name in all_vars:
        is_set = bool((os.getenv(var_name) or "").strip())
        logger_inst.info("Env check: %s = %s", var_name, "SET" if is_set else "MISSING")
        if var_name in required_vars and not is_set:
            missing.append(var_name)

    if missing:
        mode = "DRY_RUN" if dry_run else "LIVE"
        raise EnvironmentError(
            f"Missing required environment variable(s) for {mode} integration test: "
            f"{', '.join(missing)}. Set them in .env or environment before running."
        )


def _discover_failed_applicant_ids(
    coda_client: CodaClient,
    manifest_csv: Path,
    max_count: int = MAX_INTEGRATION_APPLICANTS,
) -> list[str]:
    """Find up to max_count applicant IDs whose existing Coda status is Failed."""
    with manifest_csv.open("r", encoding="utf-8", newline="") as handle:
        valid_ids = {row["applicant_id"].strip() for row in csv.DictReader(handle)}

    fetch = coda_client.list_existing_records()
    if not fetch.success:
        raise RuntimeError(f"Failed to read Coda records for --retry-failed: {fetch.error_message}")

    failed_ids: list[str] = []
    for row in fetch.records:
        app_id = (row.applicant_id or "").strip()
        row_status = (row.status or "").strip().lower()
        if app_id in valid_ids and row_status == "failed" and app_id not in failed_ids:
            failed_ids.append(app_id)
            if len(failed_ids) >= max_count:
                break
    return failed_ids


def run_integration_test(
    *,
    applicant_ids: list[str] | None = None,
    dry_run: bool | None = None,
    live_groq: bool = False,
    retry_failed: bool = False,
    manifest_csv: Path = DEFAULT_MANIFEST_CSV,
    drive_cvs_dir: Path = DEFAULT_DRIVE_CVS_DIR,
    settings: Settings | None = None,
    groq_client: Any | None = None,
    coda_http_client: Any | None = None,
    readback_poll_interval_s: float = 2.0,
) -> IntegrationTestSummary:
    """Execute the end-to-end integration test on up to 3 selected applicants."""
    load_dotenv(dotenv_path=WORKSPACE_ROOT / ".env", override=False)
    resolved_settings = settings or get_settings()
    resolved_dry_run = _parse_dry_run_env(default=True) if dry_run is None else dry_run

    secrets = _collect_secrets(resolved_settings)
    logger_inst = logging.getLogger("e2e_integration_test")
    logger_inst.setLevel(logging.INFO)
    if not logger_inst.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handler.addFilter(_SecretRedactingFilter(secrets))
        logger_inst.addHandler(handler)

    # Also protect root/app module logs against accidental token output
    redact_filter = _SecretRedactingFilter(secrets)
    for app_logger_name in (
        "app",
        "app.services.pipeline",
        "app.services.scoring_engine",
        "app.integrations.coda_client",
    ):
        logging.getLogger(app_logger_name).addFilter(redact_filter)

    effective_groq_client = groq_client
    if effective_groq_client is None and resolved_dry_run and not live_groq:
        effective_groq_client = _DryRunMockGroqClient()
        logger_inst.info(
            "[DRY_RUN] Using deterministic local mock Groq client (no real Groq API calls). "
            "Pass --live-groq to call Groq while skipping Coda writes."
        )

    if effective_groq_client is None or coda_http_client is None:
        _check_required_env_vars(resolved_dry_run, live_groq, logger_inst)

    if applicant_ids is not None:
        selected_ids = list(applicant_ids)
    elif retry_failed:
        probe_coda = CodaClient(settings=resolved_settings, http_client=coda_http_client)
        selected_ids = _discover_failed_applicant_ids(probe_coda, manifest_csv=manifest_csv)
        if not selected_ids:
            logger_inst.info("No Failed applicants found in Coda for --retry-failed.")
            return IntegrationTestSummary(
                dry_run=resolved_dry_run,
                selected_applicant_ids=[],
                total_duration_s=0.0,
                passed=True,
                applicant_metrics=[],
            )
    else:
        selected_ids = list(DEFAULT_APPLICANT_IDS)

    logger_inst.info(
        "=== Starting Local E2E Integration Test (dry_run=%s, live_groq=%s, retry_failed=%s, applicants=%s) ===",
        resolved_dry_run,
        bool(not resolved_dry_run or live_groq) if groq_client is None else False,
        retry_failed,
        selected_ids,
    )

    applicants = load_selected_applicants(
        manifest_csv=manifest_csv,
        drive_cvs_dir=drive_cvs_dir,
        applicant_ids=selected_ids,
    )

    metrics_by_id: dict[str, StageMetrics] = {
        app["applicant_id"]: StageMetrics(applicant_id=app["applicant_id"])
        for app in applicants
    }

    downloader = LocalDriveUploadDownloader(drive_cvs_dir=drive_cvs_dir)
    coda_client = VerifyingCodaClient(
        settings=resolved_settings,
        dry_run=resolved_dry_run,
        metrics_by_id=metrics_by_id,
        secrets=secrets,
        http_client=coda_http_client,
        logger_inst=logger_inst,
    )
    duplicate_detector = SpyDuplicateDetector(
        coda_client=coda_client,
        metrics_by_id=metrics_by_id,
    )
    cv_parser = SpyCVParser(metrics_by_id=metrics_by_id)
    scoring_engine = SpyScoringEngine(
        settings=resolved_settings,
        metrics_by_id=metrics_by_id,
        secrets=secrets,
        client=effective_groq_client,
    )

    if scoring_engine.provider != "groq":
        raise RuntimeError(f"Expected active AI provider 'groq', got {scoring_engine.provider!r}")
    logger_inst.info(
        "Active AI provider verified: provider=%s model=%s base_url=%s",
        scoring_engine.provider,
        scoring_engine.model_name,
        scoring_engine.base_url,
    )

    pipeline = ApplicantPipeline(
        settings=resolved_settings,
        downloader=downloader,
        duplicate_detector=duplicate_detector,
        cv_parser=cv_parser,
        scoring_engine=scoring_engine,
        coda_client=coda_client,
    )

    batch_start = time.perf_counter()

    for applicant in applicants:
        app_id = applicant["applicant_id"]
        metrics = metrics_by_id[app_id]
        cv_parser.current_applicant_id = app_id
        scoring_engine.current_applicant_id = app_id

        app_start = time.perf_counter()
        result: PipelineResult = pipeline.process_applicant(
            applicant,
            retry_only=retry_failed,
        )
        metrics.total_duration_s = round(time.perf_counter() - app_start, 4)
        metrics.pipeline_status = result.status.value
        metrics.coda_updated = result.coda_updated

        if result.error_message:
            safe_err = _redact_all(result.error_message, secrets)
            if safe_err not in metrics.errors:
                metrics.errors.append(safe_err)

        logger_inst.info(
            "Processed %s | status=%s | parse=%s (%d chars) | duplicate=%s | "
            "score=%s | rec=%s | cols_ok=%s | duration=%.2fs",
            app_id,
            result.status.value,
            metrics.parse_status,
            metrics.extracted_chars,
            metrics.is_duplicate,
            metrics.total_score,
            metrics.recommendation,
            metrics.coda_columns_verified,
            metrics.total_duration_s,
        )

    if not resolved_dry_run and coda_client.inserted_records:
        _verify_coda_readback(
            coda_client=coda_client,
            expected_records=coda_client.inserted_records,
            metrics_by_id=metrics_by_id,
            logger_inst=logger_inst,
            poll_interval_s=readback_poll_interval_s,
        )

    total_elapsed = round(time.perf_counter() - batch_start, 4)

    all_passed = True
    for app_id in selected_ids:
        m = metrics_by_id[app_id]
        if m.errors:
            all_passed = False
        if m.pipeline_status == PipelineStatus.SCORED.value:
            if not (m.criteria_verified and m.evidence_verified and m.coda_columns_verified):
                all_passed = False
            if not resolved_dry_run and not (m.coda_write_succeeded and m.coda_readback_verified):
                all_passed = False
        elif m.pipeline_status == PipelineStatus.FAILED.value:
            all_passed = False

    logger_inst.info(
        "=== Completed E2E Integration Test | passed=%s | total_time=%.2fs ===",
        all_passed,
        total_elapsed,
    )
    for m in metrics_by_id.values():
        if m.errors:
            logger_inst.error("Errors for %s: %s", m.applicant_id, m.errors)

    return IntegrationTestSummary(
        dry_run=resolved_dry_run,
        selected_applicant_ids=selected_ids,
        total_duration_s=total_elapsed,
        passed=all_passed,
        applicant_metrics=[metrics_by_id[app_id] for app_id in selected_ids],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run local E2E integration test on 3 applicants from applicants_manifest.csv."
    )
    parser.add_argument(
        "--applicant-ids",
        nargs="+",
        default=None,
        help="1 to 3 applicant IDs from applicants_manifest.csv (default: APP-001 APP-002 APP-004).",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Process only applicants whose existing Coda status is Failed (updates existing Coda rows).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Safe dry run: mocks Groq by default and skips Coda POST/PUT writes (overrides DRY_RUN env).",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Perform live Groq scoring, Coda inserts/updates, and readback verification (sets DRY_RUN=false).",
    )
    parser.add_argument(
        "--live-groq",
        action="store_true",
        help="Allow real Groq API calls during --dry-run while still skipping Coda writes.",
    )
    args = parser.parse_args()

    dry_run_arg: bool | None = None
    if args.dry_run:
        dry_run_arg = True
    elif args.live:
        dry_run_arg = False

    summary = run_integration_test(
        applicant_ids=args.applicant_ids,
        dry_run=dry_run_arg,
        live_groq=args.live_groq,
        retry_failed=args.retry_failed,
    )
    if not summary.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
