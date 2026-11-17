"""Production CLI runner for the Daily Applicant Scoring Pipeline.

Reads applicant rows directly from Google Sheets using Google Service Account
credentials, downloads CVs from Google Drive (via authenticated Drive API v3),
checks Coda for duplicates and existing statuses, parses CVs, scores with Groq
(openai/gpt-oss-120b), and writes/updates results in Coda.

Supported CLI flags:
  --dry-run                   Validate end-to-end without calling Groq or writing to Coda
  --limit N                   Process at most N eligible applicants
  --email EMAIL               Filter to a single applicant email address
  --retry-failed              Process only applicants with existing Status = Failed
  --applicant-ids APP-001 ... Filter to specific Applicant IDs
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, Settings, get_settings
from app.integrations.coda_client import (
    CodaApplicantRecord,
    CodaApplicantStatus,
    CodaClient,
    CodaInsertResult,
    normalize_email,
    redact_sensitive_text,
)
from app.integrations.google_sheets_client import (
    GoogleSheetsClient,
    SheetInvalidRow,
    SheetReadResult,
)
from app.services.cv_parser import CVParser
from app.services.duplicate_detector import DuplicateDetector
from app.services.pipeline import (
    ApplicantInput,
    ApplicantPipeline,
    GoogleDriveDownloader,
    PipelineResult,
    PipelineStatus,
)
from app.services.scoring_engine import ScoringEngine

DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"
DEFAULT_LOG_FILE = DEFAULT_LOGS_DIR / "pipeline.log"
DEFAULT_LOCK_FILE = DEFAULT_LOGS_DIR / "pipeline.lock"

LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per rotating log file
LOG_BACKUP_COUNT = 14


class PipelineLockError(RuntimeError):
    """Raised when another pipeline execution is already holding the lock."""


class PipelineRunSummary(BaseModel):
    """Structured summary of a full run_pipeline execution."""

    dry_run: bool
    retry_failed: bool
    sheet_read_success: bool
    start_time: str = ""
    end_time: str = ""
    total_sheet_rows: int = 0
    total_applicants_fetched: int = 0
    valid_sheet_rows: int = 0
    invalid_sheet_rows: int = 0
    skipped_empty_rows: int = 0
    filtered_candidates: int = 0
    new_applicants_found: int = 0
    processed_count: int = 0
    scored_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    updated_count: int = 0
    total_duration_s: float = 0.0
    error_message: str | None = None
    error_details: list[str] = Field(default_factory=list)
    results: list[PipelineResult] = Field(default_factory=list)
    invalid_rows: list[SheetInvalidRow] = Field(default_factory=list)


class _SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs configured secrets and tokens from all log records."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        scrubbed = rendered
        for secret in self._secrets:
            scrubbed = redact_sensitive_text(scrubbed, secret=secret)
        scrubbed = redact_sensitive_text(scrubbed)
        record.msg = scrubbed
        record.args = ()
        if record.exc_info:
            record.exc_info = None
        return True


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


def _now_iso_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_pipeline_logging(
    settings: Settings,
    log_file: Path = DEFAULT_LOG_FILE,
) -> logging.Logger:
    """Configure console + rotating file + daily date-based logging under logs/ with secret redaction."""
    log_file = Path(log_file).resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    secrets = _collect_secrets(settings)
    redact_filter = _SecretRedactingFilter(secrets)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    date_stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
    daily_log_file = log_file.parent / f"pipeline_{date_stamp}.log"

    runner_logger = logging.getLogger("pipeline_runner")
    runner_logger.setLevel(logging.INFO)
    runner_logger.propagate = False

    for existing in list(runner_logger.handlers):
        runner_logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact_filter)
    runner_logger.addHandler(stream_handler)

    rotating_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(formatter)
    rotating_handler.addFilter(redact_filter)
    runner_logger.addHandler(rotating_handler)

    if daily_log_file != log_file:
        daily_handler = RotatingFileHandler(
            daily_log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        daily_handler.setFormatter(formatter)
        daily_handler.addFilter(redact_filter)
        runner_logger.addHandler(daily_handler)

    app_root_logger = logging.getLogger("app")
    app_root_logger.setLevel(logging.INFO)
    app_root_logger.addFilter(redact_filter)
    for existing in list(app_root_logger.handlers):
        app_root_logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    app_rotating_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_rotating_handler.setFormatter(formatter)
    app_rotating_handler.addFilter(redact_filter)
    app_root_logger.addHandler(app_rotating_handler)

    if daily_log_file != log_file:
        app_daily_handler = RotatingFileHandler(
            daily_log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        app_daily_handler.setFormatter(formatter)
        app_daily_handler.addFilter(redact_filter)
        app_root_logger.addHandler(app_daily_handler)

    return runner_logger


def _is_pid_running(pid: int) -> bool:
    """Check whether a process with the given PID is currently alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _parse_lock_pid(content: str) -> int:
    """Extract PID from lock file content (supports plain first-line PID or JSON)."""
    stripped = (content or "").strip()
    if not stripped:
        return -1
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            return int(data.get("pid", -1))
        except Exception:
            return -1
    try:
        return int(stripped.splitlines()[0].strip())
    except ValueError:
        return -1


@contextmanager
def acquire_execution_lock(lock_file: Path = DEFAULT_LOCK_FILE) -> Iterator[None]:
    """Acquire an exclusive lock file to prevent overlapping pipeline executions and recover stale locks."""
    lock_file = Path(lock_file).resolve()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    runner_logger = logging.getLogger("pipeline_runner")

    for _attempt in range(2):
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            try:
                lock_body = f"{os.getpid()}\nstarted_at={_now_iso_local()}\n"
                os.write(fd, lock_body.encode("utf-8"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                content = lock_file.read_text(encoding="utf-8")
                existing_pid = _parse_lock_pid(content)
            except OSError:
                existing_pid = -1

            if (
                existing_pid > 0
                and existing_pid != os.getpid()
                and _is_pid_running(existing_pid)
            ):
                raise PipelineLockError(
                    f"Another pipeline execution is already running (lock={lock_file.name}, pid={existing_pid})."
                )
            elif existing_pid == os.getpid():
                raise PipelineLockError(
                    f"Pipeline execution lock is already held by current process (lock={lock_file.name}, pid={existing_pid})."
                )
            else:
                runner_logger.warning(
                    "Recovered stale execution lock from terminated process (pid=%s, lock=%s).",
                    existing_pid if existing_pid > 0 else "unknown",
                    lock_file.name,
                )
                try:
                    lock_file.unlink()
                except OSError as exc:
                    raise PipelineLockError(
                        f"Could not remove stale lock file {lock_file.name}: {exc}"
                    ) from exc
    else:
        raise PipelineLockError(
            f"Could not acquire pipeline execution lock ({lock_file.name})."
        )

    try:
        yield
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


class DryRunCodaClient(CodaClient):
    """Wraps CodaClient to perform real read-only GET checks while skipping POST/PUT writes in dry-run mode."""

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: Any | None = None,
        logger_inst: logging.Logger | None = None,
    ) -> None:
        super().__init__(settings=settings, http_client=http_client)
        self._logger = logger_inst or logging.getLogger("pipeline_runner")

    def insert_applicant_record(self, record: CodaApplicantRecord) -> CodaInsertResult:
        cells = record.to_coda_cells(self.column_mapping)
        self._logger.info(
            "[DRY_RUN] Validated %d-column Coda INSERT payload for %s (Status=%s, Total Score=%s, Recommendation=%s); skipping Coda POST write.",
            len(cells),
            record.applicant_id,
            record.status.value,
            record.total_score,
            record.recommendation or "-",
        )
        return CodaInsertResult(
            success=True,
            status_code=202,
            request_id="dry-run-no-insert",
            added_row_ids=[],
        )

    def update_applicant_record(
        self,
        row_id: str,
        record: CodaApplicantRecord,
    ) -> CodaInsertResult:
        cells = record.to_coda_cells(self.column_mapping)
        self._logger.info(
            "[DRY_RUN] Validated %d-column Coda UPDATE payload for %s (row_id=%s, Status=%s, Total Score=%s, Recommendation=%s); skipping Coda PUT write.",
            len(cells),
            record.applicant_id,
            row_id,
            record.status.value,
            record.total_score,
            record.recommendation or "-",
        )
        return CodaInsertResult(
            success=True,
            status_code=202,
            request_id="dry-run-no-update",
            added_row_ids=[row_id],
        )


class _DryRunMockGroqClient:
    """Deterministic local mock Groq client used in dry-run mode when --live-groq is not set."""

    class _Completions:
        def create(
            self,
            *,
            model: str,
            messages: list[dict[str, str]],
            temperature: float = 0.0,
            response_format: dict[str, Any] | None = None,
        ) -> Any:
            user_content = " ".join(
                m.get("content", "") for m in messages if m.get("role") == "user"
            )
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
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(payload))
                    )
                ]
            )

    class _Chat:
        def __init__(self) -> None:
            self.completions = _DryRunMockGroqClient._Completions()

    def __init__(self) -> None:
        self.chat = self._Chat()


def _filter_sheet_applicants(
    applicants: list[ApplicantInput],
    *,
    email: str | None = None,
    applicant_ids: list[str] | None = None,
) -> list[ApplicantInput]:
    """Filter sheet applicants by --email and/or --applicant-ids before Coda check."""
    filtered = list(applicants)
    if email and email.strip():
        target_email = normalize_email(email) or email.strip().lower()
        filtered = [
            app
            for app in filtered
            if (normalize_email(app.email) or app.email.lower()) == target_email
        ]
    if applicant_ids:
        target_ids = {aid.strip().upper() for aid in applicant_ids if aid and aid.strip()}
        filtered = [
            app
            for app in filtered
            if app.resolved_applicant_id().upper() in target_ids
        ]
    return filtered


def run_pipeline(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    email: str | None = None,
    retry_failed: bool = False,
    applicant_ids: list[str] | None = None,
    live_groq: bool = False,
    settings: Settings | None = None,
    sheets_client: GoogleSheetsClient | None = None,
    downloader: GoogleDriveDownloader | None = None,
    coda_client: CodaClient | None = None,
    groq_client: Any | None = None,
    log_file: Path = DEFAULT_LOG_FILE,
    lock_file: Path = DEFAULT_LOCK_FILE,
) -> PipelineRunSummary:
    """Execute the Google Sheets -> Google Drive -> Groq -> Coda applicant scoring pipeline."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer (>= 1).")

    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    resolved_settings = settings or get_settings()
    logger_inst = configure_pipeline_logging(resolved_settings, log_file=log_file)
    secrets = _collect_secrets(resolved_settings)

    start_time_iso = _now_iso_local()
    t_start = time.perf_counter()

    with acquire_execution_lock(lock_file=lock_file):
        logger_inst.info(
            "=== Starting Applicant Scoring Pipeline at %s (dry_run=%s, retry_failed=%s, limit=%s, email=%s, applicant_ids=%s) ===",
            start_time_iso,
            dry_run,
            retry_failed,
            limit,
            email or "-",
            applicant_ids or "-",
        )

        effective_sheets_client = sheets_client or GoogleSheetsClient(
            settings=resolved_settings
        )
        sheet_result: SheetReadResult = effective_sheets_client.read_applicants()

        if not sheet_result.success:
            end_time_iso = _now_iso_local()
            elapsed = round(time.perf_counter() - t_start, 4)
            err = redact_sensitive_text(
                sheet_result.error_message or "Failed to read Google Sheets."
            )
            for s in secrets:
                err = redact_sensitive_text(err, secret=s)
            logger_inst.error(
                "Pipeline aborted at %s due to Google Sheets read error (duration=%.3fs): %s",
                end_time_iso,
                elapsed,
                err,
            )
            return PipelineRunSummary(
                dry_run=dry_run,
                retry_failed=retry_failed,
                sheet_read_success=False,
                start_time=start_time_iso,
                end_time=end_time_iso,
                total_duration_s=elapsed,
                error_message=err,
                error_details=[err],
            )

        candidates = _filter_sheet_applicants(
            sheet_result.applicants,
            email=email,
            applicant_ids=applicant_ids,
        )

        if coda_client is not None:
            effective_coda_client = coda_client
        elif dry_run:
            effective_coda_client = DryRunCodaClient(
                settings=resolved_settings,
                logger_inst=logger_inst,
            )
        else:
            effective_coda_client = CodaClient(settings=resolved_settings)

        duplicate_detector = DuplicateDetector(coda_client=effective_coda_client)

        if downloader is not None:
            effective_downloader = downloader
        else:
            creds_path = resolved_settings.resolve_google_credentials_path()
            effective_downloader = GoogleDriveDownloader(
                credentials_path=(
                    creds_path
                    if (creds_path is not None and creds_path.is_file())
                    else None
                )
            )

        effective_groq_client = groq_client
        if effective_groq_client is None and dry_run and not live_groq:
            effective_groq_client = _DryRunMockGroqClient()
            logger_inst.info(
                "[DRY_RUN] Using deterministic local mock Groq client (no live Groq API calls)."
            )

        scoring_engine = ScoringEngine(
            settings=resolved_settings,
            client=effective_groq_client,
        )

        pipeline = ApplicantPipeline(
            settings=resolved_settings,
            downloader=effective_downloader,
            duplicate_detector=duplicate_detector,
            cv_parser=CVParser(),
            scoring_engine=scoring_engine,
            coda_client=effective_coda_client,
        )

        # Pre-fetch existing Coda records once to pre-screen status/email before Drive download
        coda_preload_ok = duplicate_detector.load_existing_records().success

        # Count how many filtered candidates are brand-new (not in Coda at all)
        new_applicants_found = 0
        if coda_preload_ok:
            for cand in candidates:
                chk = duplicate_detector.check_applicant(
                    applicant_id=cand.resolved_applicant_id(),
                    email=normalize_email(cand.email) or cand.email,
                    cv_hash=None,
                    refresh=False,
                )
                if not chk.is_duplicate and not chk.is_retry:
                    new_applicants_found += 1

        results: list[PipelineResult] = []
        error_details: list[str] = [
            f"Row {inv.row_number}: {inv.reason}" for inv in sheet_result.invalid_rows
        ]
        eligible_processed = 0

        for applicant in candidates:
            app_id = applicant.resolved_applicant_id()
            norm_email = normalize_email(applicant.email) or applicant.email

            if coda_preload_ok:
                pre_check = duplicate_detector.check_applicant(
                    applicant_id=app_id,
                    email=norm_email,
                    cv_hash=None,
                    refresh=False,
                )
                if pre_check.is_duplicate:
                    logger_inst.info(
                        "Skipping applicant_id=%s (email=%s): already in Coda with status=%s reason=%s",
                        pre_check.matched_applicant_id or app_id,
                        norm_email,
                        pre_check.existing_status or "Scored",
                        pre_check.reason.value if pre_check.reason else "duplicate",
                    )
                    results.append(
                        PipelineResult(
                            applicant_id=pre_check.matched_applicant_id or app_id,
                            status=PipelineStatus.SKIPPED_DUPLICATE,
                            email=norm_email,
                            duplicate_reason=pre_check.reason,
                            coda_inserted=False,
                        )
                    )
                    continue

                if retry_failed and not pre_check.is_retry:
                    logger_inst.info(
                        "Skipping applicant_id=%s in --retry-failed mode (status is not Failed).",
                        app_id,
                    )
                    results.append(
                        PipelineResult(
                            applicant_id=app_id,
                            status=PipelineStatus.SKIPPED_DUPLICATE,
                            email=norm_email,
                            coda_inserted=False,
                        )
                    )
                    continue

                if not retry_failed and pre_check.is_retry:
                    logger_inst.info(
                        "Skipping Failed applicant_id=%s (email=%s) because --retry-failed was not requested.",
                        pre_check.matched_applicant_id or app_id,
                        norm_email,
                    )
                    results.append(
                        PipelineResult(
                            applicant_id=pre_check.matched_applicant_id or app_id,
                            status=PipelineStatus.SKIPPED_DUPLICATE,
                            email=norm_email,
                            coda_inserted=False,
                        )
                    )
                    continue

            if limit is not None and eligible_processed >= limit:
                logger_inst.info(
                    "Reached --limit %d eligible applicant(s); stopping batch.",
                    limit,
                )
                break

            eligible_processed += 1
            try:
                res = pipeline.process_applicant(
                    applicant,
                    retry_only=retry_failed,
                    allow_retry_failed=retry_failed,
                )
                results.append(res)
                if res.status == PipelineStatus.SCORED:
                    duplicate_detector.register_processed(
                        applicant_id=res.applicant_id,
                        email=res.email,
                        cv_hash=res.cv_hash,
                        row_id=res.coda_row_ids[0] if res.coda_row_ids else None,
                        status=CodaApplicantStatus.SCORED.value,
                    )
                elif res.status == PipelineStatus.FAILED and res.error_message:
                    error_details.append(f"{res.applicant_id}: {res.error_message}")
            except Exception as exc:
                safe_err = redact_sensitive_text(str(exc))
                for s in secrets:
                    safe_err = redact_sensitive_text(safe_err, secret=s)
                logger_inst.error(
                    "Isolated error processing applicant_id=%s: %s",
                    app_id,
                    safe_err,
                )
                error_details.append(f"{app_id}: {safe_err}")
                results.append(
                    PipelineResult(
                        applicant_id=app_id,
                        status=PipelineStatus.FAILED,
                        email=norm_email,
                        coda_inserted=False,
                        error_message=safe_err,
                    )
                )

        scored_count = sum(1 for r in results if r.status == PipelineStatus.SCORED)
        failed_count = sum(1 for r in results if r.status == PipelineStatus.FAILED)
        skipped_count = sum(
            1 for r in results if r.status == PipelineStatus.SKIPPED_DUPLICATE
        )
        updated_count = sum(1 for r in results if r.coda_updated)
        end_time_iso = _now_iso_local()
        elapsed = round(time.perf_counter() - t_start, 4)

        summary = PipelineRunSummary(
            dry_run=dry_run,
            retry_failed=retry_failed,
            sheet_read_success=True,
            start_time=start_time_iso,
            end_time=end_time_iso,
            total_sheet_rows=sheet_result.total_data_rows,
            total_applicants_fetched=len(sheet_result.applicants),
            valid_sheet_rows=len(sheet_result.applicants),
            invalid_sheet_rows=len(sheet_result.invalid_rows),
            skipped_empty_rows=sheet_result.skipped_empty_rows,
            filtered_candidates=len(candidates),
            new_applicants_found=new_applicants_found,
            processed_count=eligible_processed,
            scored_count=scored_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            updated_count=updated_count,
            total_duration_s=elapsed,
            error_details=error_details,
            results=results,
            invalid_rows=sheet_result.invalid_rows,
        )

        logger_inst.info(
            "=== Pipeline Summary: start=%s end=%s duration=%.3fs dry_run=%s retry_failed=%s "
            "total_fetched=%d new_found=%d skipped=%d scored=%d failed=%d updated=%d "
            "invalid_sheet_rows=%d empty_rows=%d ===",
            summary.start_time,
            summary.end_time,
            summary.total_duration_s,
            dry_run,
            retry_failed,
            summary.total_applicants_fetched,
            summary.new_applicants_found,
            summary.skipped_count,
            summary.scored_count,
            summary.failed_count,
            summary.updated_count,
            summary.invalid_sheet_rows,
            summary.skipped_empty_rows,
        )
        if error_details:
            for detail in error_details:
                logger_inst.warning("Error detail: %s", detail)
        return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Daily Applicant Scoring Pipeline reading directly from Google Sheets "
            "and downloading CVs via Google Drive."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate Google Sheets read, Drive CV download, parsing, and scoring without calling Groq or writing to Coda.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of eligible applicants to process.",
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        metavar="EMAIL",
        help="Process only the applicant matching this email address.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Process only applicants whose existing Coda status is Failed, updating their existing Coda row.",
    )
    parser.add_argument(
        "--applicant-ids",
        nargs="+",
        default=None,
        metavar="APP-ID",
        help="Process only specific Applicant IDs (e.g., --applicant-ids APP-001).",
    )
    parser.add_argument(
        "--live-groq",
        action="store_true",
        help="When combined with --dry-run, call the live Groq API while still skipping Coda writes.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        metavar="PATH",
        help="Optional override path for the log file (default: logs/pipeline.log).",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_FILE,
        metavar="PATH",
        help="Optional override path for the execution lock file (default: logs/pipeline.lock).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    settings = get_settings()
    logger_inst = configure_pipeline_logging(settings, log_file=args.log_file)

    try:
        summary = run_pipeline(
            dry_run=bool(args.dry_run),
            limit=args.limit,
            email=args.email,
            retry_failed=bool(args.retry_failed),
            applicant_ids=args.applicant_ids,
            live_groq=bool(args.live_groq),
            settings=settings,
            log_file=args.log_file,
            lock_file=args.lock_file,
        )
    except PipelineLockError as exc:
        logger_inst.warning(
            "Skipping scheduled execution safely because another instance is running: %s",
            exc,
        )
        return 0
    except Exception as exc:
        logger_inst.error(
            "Pipeline failed: %s", redact_sensitive_text(str(exc))
        )
        return 1

    if not summary.sheet_read_success:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
