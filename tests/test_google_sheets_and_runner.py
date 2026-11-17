"""Mocked unit tests for GoogleSheetsClient, authenticated GoogleDriveDownloader, and run_pipeline.py.

Never makes live Groq API calls, Google API calls, or Coda writes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import docx
import httpx
import pytest
from pydantic import SecretStr

from app.config import load_settings
from app.integrations.coda_client import CodaClient
from app.integrations.google_sheets_client import (
    GoogleAuthenticationError,
    GoogleServiceAccountAuth,
    GoogleSheetsClient,
)
from app.services.pipeline import GoogleDriveDownloader, PipelineStatus
from run_pipeline import (
    PipelineLockError,
    acquire_execution_lock,
    run_pipeline,
)


def _build_valid_docx_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph(text)
    doc.save(buffer)
    return buffer.getvalue()


def _valid_groq_json(score: float = 82.0) -> str:
    return json.dumps(
        {
            "criteria": {
                "skills_match": {
                    "score": score,
                    "evidence": "CV lists Python, SQL, Git, and REST APIs.",
                    "evidence_status": "supported",
                },
                "experience_relevance": {
                    "score": score,
                    "evidence": "2 years as Junior Software Developer.",
                    "evidence_status": "supported",
                },
                "education_fit": {
                    "score": score,
                    "evidence": "Bachelor's degree in Computer Science.",
                    "evidence_status": "supported",
                },
                "overall_alignment": {
                    "score": score,
                    "evidence": "Strong fit for Vacancy 90.",
                    "evidence_status": "supported",
                },
            },
            "explanation": "Strong junior developer profile.",
        }
    )


def test_google_sheets_read_success_and_configurable_column_mapping() -> None:
    """Verify successful Google Sheets API read, Candidate NNN -> APP-NNN mapping, and custom columns."""
    captured_requests: list[httpx.Request] = []

    def sheets_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "range": "ApplicantsTab!A1:D3",
                "majorDimension": "ROWS",
                "values": [
                    ["Full_Name", "Candidate_Email", "Target_Role", "Resume_URL"],
                    [
                        "Candidate 001",
                        "candidate001@example.com",
                        "Junior Level Software Developer (1-4 years experience)",
                        "https://drive.google.com/file/d/file_001/view",
                    ],
                    [
                        "Jane Doe",
                        "jane.doe@example.com",
                        "Junior Level Software Developer (1-4 years experience)",
                        "https://drive.google.com/file/d/file_jane/view",
                    ],
                ],
            },
        )

    settings = load_settings()
    client = GoogleSheetsClient(
        settings=settings,
        spreadsheet_id="sheet-id-123",
        worksheet_name="ApplicantsTab",
        column_mapping={
            "name": "Full_Name",
            "email": "Candidate_Email",
            "role": "Target_Role",
            "cv_link": "Resume_URL",
        },
        http_client=httpx.Client(transport=httpx.MockTransport(sheets_handler)),
        token_provider=lambda: "ya29.mock-service-account-token",
    )

    result = client.read_applicants()

    assert result.success is True
    assert result.total_data_rows == 2
    assert result.skipped_empty_rows == 0
    assert result.invalid_rows == []
    assert len(result.applicants) == 2

    first = result.applicants[0]
    assert first.resolved_applicant_id() == "APP-001"
    assert first.name == "Candidate 001"
    assert first.email == "candidate001@example.com"
    assert first.cv_link == "https://drive.google.com/file/d/file_001/view"

    second = result.applicants[1]
    assert second.resolved_applicant_id().startswith("APP-")
    assert second.name == "Jane Doe"

    assert len(captured_requests) == 1
    assert (
        captured_requests[0].headers["Authorization"]
        == "Bearer ya29.mock-service-account-token"
    )
    assert "spreadsheets/sheet-id-123/values/ApplicantsTab" in str(
        captured_requests[0].url
    )


def test_google_sheets_skips_empty_rows_and_isolates_invalid_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty rows are skipped silently; invalid email and missing CV_Link are logged without crashing."""
    def sheets_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "values": [
                    ["Name", "Email", "Role", "CV_Link"],
                    [],  # Empty row 2
                    ["   ", "", "  ", ""],  # Whitespace-only row 3
                    [
                        "Candidate 002",
                        "not-an-email",
                        "Junior Developer",
                        "https://drive.google.com/file/d/id2/view",
                    ],  # Row 4: invalid email
                    [
                        "Candidate 003",
                        "candidate003@example.com",
                        "Junior Developer",
                        "",
                    ],  # Row 5: missing CV_Link
                    [
                        "",
                        "candidate004@example.com",
                        "",
                        "ftp://invalid-scheme.example.com/cv.docx",
                    ],  # Row 6: missing Name, Role, and invalid URL scheme
                    [
                        "Candidate 005",
                        "candidate005@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/id5/view",
                    ],  # Row 7: valid
                ]
            },
        )

    client = GoogleSheetsClient(
        spreadsheet_id="sheet-id-456",
        worksheet_name="Sheet1",
        http_client=httpx.Client(transport=httpx.MockTransport(sheets_handler)),
        token_provider=lambda: "ya29.mock-token",
    )

    caplog.set_level(logging.WARNING)
    result = client.read_applicants()

    assert result.success is True
    assert result.skipped_empty_rows == 2
    assert len(result.applicants) == 1
    assert result.applicants[0].resolved_applicant_id() == "APP-005"

    assert len(result.invalid_rows) == 3
    assert result.invalid_rows[0].row_number == 4
    assert "email" in result.invalid_rows[0].reason.lower()

    assert result.invalid_rows[1].row_number == 5
    assert "Missing CV_Link" in result.invalid_rows[1].reason

    assert result.invalid_rows[2].row_number == 6
    assert "Missing Name" in result.invalid_rows[2].reason
    assert "Missing Role" in result.invalid_rows[2].reason
    assert "Invalid CV_Link" in result.invalid_rows[2].reason

    assert "Skipping invalid Google Sheet row 4" in caplog.text
    assert "Skipping invalid Google Sheet row 5" in caplog.text


def test_google_sheets_authentication_and_api_errors_handled_safely(
    tmp_path: Path,
) -> None:
    """Missing credentials file, invalid JSON credentials, and HTTP 403/404 return clean errors."""
    # 1. Missing service account file
    missing_path = tmp_path / "nonexistent-sa.json"
    auth_missing = GoogleServiceAccountAuth(missing_path)
    with pytest.raises(GoogleAuthenticationError, match="not found"):
        auth_missing.get_access_token()

    # 2. Malformed JSON credentials file (never leaks file contents)
    bad_json_path = tmp_path / "bad-sa.json"
    bad_json_path.write_text("{not-valid-json", encoding="utf-8")
    client_bad_creds = GoogleSheetsClient(
        spreadsheet_id="sheet-123",
        credentials_path=bad_json_path,
    )
    res_bad_creds = client_bad_creds.read_applicants()
    assert res_bad_creds.success is False
    assert "Invalid service account credentials" in (res_bad_creds.error_message or "")

    # 3. HTTP 403 Forbidden (sheet not shared with service account)
    client_403 = GoogleSheetsClient(
        spreadsheet_id="sheet-123",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, json={"error": {"message": "Permission denied"}})
            )
        ),
        token_provider=lambda: "ya29.secret-sa-token",
    )
    res_403 = client_403.read_applicants()
    assert res_403.success is False
    assert res_403.status_code == 403
    assert "HTTP 403" in (res_403.error_message or "")
    assert "ya29.secret-sa-token" not in (res_403.error_message or "")

    # 4. Missing required header column in worksheet
    client_bad_headers = GoogleSheetsClient(
        spreadsheet_id="sheet-123",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"values": [["Name", "Email"]]})
            )
        ),
        token_provider=lambda: "ya29.secret-sa-token",
    )
    res_headers = client_bad_headers.read_applicants()
    assert res_headers.success is False
    assert "Missing required column header" in (res_headers.error_message or "")


def test_authenticated_google_drive_downloader_uses_drive_v3_api(
    tmp_path: Path,
) -> None:
    """When service account token_provider/credentials are configured, Drive API v3 alt=media is used."""
    docx_bytes = _build_valid_docx_bytes("Private Google Drive CV Content")
    captured_requests: list[httpx.Request] = []

    def drive_api_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
            content=docx_bytes,
        )

    downloader = GoogleDriveDownloader(
        http_client=httpx.Client(transport=httpx.MockTransport(drive_api_handler)),
        token_provider=lambda: "ya29.drive-readonly-secret-token",
    )

    download_res = downloader.download(
        cv_link="https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view?usp=drive_link",
        target_dir=tmp_path,
        applicant_id="APP-001",
    )

    assert download_res.success is True
    assert download_res.file_path is not None
    assert download_res.file_path.suffix == ".docx"
    assert download_res.file_path.read_bytes() == docx_bytes

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert (
        str(req.url)
        == "https://www.googleapis.com/drive/v3/files/1AbCdEfGhIjKlMnOpQrStUvWxYz?alt=media&supportsAllDrives=true"
    )
    assert req.headers["Authorization"] == "Bearer ya29.drive-readonly-secret-token"


def test_run_pipeline_default_skips_scored_and_failed_and_processes_only_new(
    tmp_path: Path,
) -> None:
    """Default run_pipeline processes new applicants, skips Scored, and skips Failed unless --retry-failed."""
    docx_new = _build_valid_docx_bytes("Candidate 003\nPython SQL Git Developer")

    def sheets_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "values": [
                    ["Name", "Email", "Role", "CV_Link"],
                    [
                        "Candidate 001",
                        "candidate001@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv001/view",
                    ],
                    [
                        "Candidate 002",
                        "candidate002@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv002/view",
                    ],
                    [
                        "Candidate 003",
                        "candidate003@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv003/view",
                    ],
                ]
            },
        )

    drive_downloaded_urls: list[str] = []

    def drive_handler(request: httpx.Request) -> httpx.Response:
        drive_downloaded_urls.append(str(request.url))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
            content=docx_new,
        )

    coda_posts: list[dict[str, Any]] = []
    coda_puts: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "row-scored-1",
                            "values": {
                                "Applicant ID": "APP-001",
                                "Email": "candidate001@example.com",
                                "Status": "Scored",
                                "Sub-scores": "{}",
                            },
                        },
                        {
                            "id": "row-failed-2",
                            "values": {
                                "Applicant ID": "APP-002",
                                "Email": "candidate002@example.com",
                                "Status": "Failed",
                                "Error Note": "Previous Groq timeout",
                                "Sub-scores": "{}",
                            },
                        },
                    ]
                },
            )
        if request.method == "POST":
            coda_posts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(202, json={"requestId": "r-post", "addedRowIds": ["row-new-3"]})
        if request.method == "PUT":
            coda_puts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(202, json={"requestId": "r-put", "id": "row-failed-2"})
        raise AssertionError(f"Unexpected Coda HTTP method: {request.method}")

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_test_secret_key")
    settings.env.coda_api_token = SecretStr("coda_test_secret_token")
    settings.env.coda_doc_id = "doc-1"
    settings.env.coda_table_id = "tbl-1"

    sheets_client = GoogleSheetsClient(
        settings=settings,
        spreadsheet_id="sheet-1",
        http_client=httpx.Client(transport=httpx.MockTransport(sheets_handler)),
        token_provider=lambda: "ya29.sheets-tok",
    )
    downloader = GoogleDriveDownloader(
        http_client=httpx.Client(transport=httpx.MockTransport(drive_handler)),
        token_provider=lambda: "ya29.drive-tok",
    )
    coda_client = CodaClient(
        settings=settings,
        api_token="coda_test_secret_token",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
        max_retries=0,
    )
    mock_groq = MagicMock()
    mock_groq.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(85.0)))]
    )

    log_file = tmp_path / "logs" / "pipeline.log"
    lock_file = tmp_path / "logs" / "pipeline.lock"

    # 1. Default run (retry_failed=False): skips APP-001 (Scored) and APP-002 (Failed), processes only APP-003
    summary = run_pipeline(
        dry_run=False,
        retry_failed=False,
        settings=settings,
        sheets_client=sheets_client,
        downloader=downloader,
        coda_client=coda_client,
        groq_client=mock_groq,
        log_file=log_file,
        lock_file=lock_file,
    )

    assert summary.sheet_read_success is True
    assert summary.valid_sheet_rows == 3
    assert summary.processed_count == 1
    assert summary.scored_count == 1
    assert summary.skipped_count == 2
    assert summary.failed_count == 0
    assert summary.updated_count == 0

    # Only APP-003's CV was downloaded from Google Drive; APP-001 and APP-002 were pre-screened!
    assert len(drive_downloaded_urls) == 1
    assert "drv003" in drive_downloaded_urls[0]
    assert len(coda_posts) == 1
    assert len(coda_puts) == 0

    # Verify log file was created under logs/ and contains no secrets
    assert log_file.is_file()
    log_text = log_file.read_text(encoding="utf-8")
    assert "gsk_test_secret_key" not in log_text
    assert "coda_test_secret_token" not in log_text


def test_run_pipeline_retry_failed_updates_existing_coda_row_only(
    tmp_path: Path,
) -> None:
    """When --retry-failed is set, only Failed applicants are processed and updated via Coda PUT."""
    docx_retry = _build_valid_docx_bytes("Candidate 002\nRetried CV content with Python SQL Git")

    def sheets_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "values": [
                    ["Name", "Email", "Role", "CV_Link"],
                    [
                        "Candidate 001",
                        "candidate001@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv001/view",
                    ],
                    [
                        "Candidate 002",
                        "candidate002@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv002/view",
                    ],
                    [
                        "Candidate 003",
                        "candidate003@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/drv003/view",
                    ],
                ]
            },
        )

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
            content=docx_retry,
        )

    coda_puts: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "row-scored-1",
                            "values": {
                                "Applicant ID": "APP-001",
                                "Email": "candidate001@example.com",
                                "Status": "Scored",
                                "Sub-scores": "{}",
                            },
                        },
                        {
                            "id": "row-failed-2",
                            "values": {
                                "Applicant ID": "APP-002",
                                "Email": "candidate002@example.com",
                                "Status": "Failed",
                                "Error Note": "Temporary outage",
                                "Sub-scores": "{}",
                            },
                        },
                    ]
                },
            )
        if request.method == "PUT":
            coda_puts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(202, json={"requestId": "r-put", "id": "row-failed-2"})
        raise AssertionError("Must not POST new Coda row during --retry-failed")

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_test_secret_key")
    settings.env.coda_api_token = SecretStr("coda_test_secret_token")
    settings.env.coda_doc_id = "doc-1"
    settings.env.coda_table_id = "tbl-1"

    sheets_client = GoogleSheetsClient(
        settings=settings,
        spreadsheet_id="sheet-1",
        http_client=httpx.Client(transport=httpx.MockTransport(sheets_handler)),
        token_provider=lambda: "ya29.sheets-tok",
    )
    downloader = GoogleDriveDownloader(
        http_client=httpx.Client(transport=httpx.MockTransport(drive_handler)),
        token_provider=lambda: "ya29.drive-tok",
    )
    coda_client = CodaClient(
        settings=settings,
        api_token="coda_test_secret_token",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
        max_retries=0,
    )
    mock_groq = MagicMock()
    mock_groq.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(78.0)))]
    )

    summary = run_pipeline(
        dry_run=False,
        retry_failed=True,
        settings=settings,
        sheets_client=sheets_client,
        downloader=downloader,
        coda_client=coda_client,
        groq_client=mock_groq,
        log_file=tmp_path / "logs" / "pipeline.log",
        lock_file=tmp_path / "logs" / "pipeline.lock",
    )

    assert summary.processed_count == 1
    assert summary.scored_count == 1
    assert summary.updated_count == 1
    assert summary.skipped_count == 2  # APP-001 (Scored) and APP-003 (New) skipped
    assert len(coda_puts) == 1
    cell_map = {c["column"]: c["value"] for c in coda_puts[0]["row"]["cells"]}
    assert cell_map["Applicant ID"] == "APP-002"
    assert cell_map["Status"] == "Scored"
    assert cell_map["Error Note"] == ""


def test_run_pipeline_partial_batch_failure_filters_and_execution_lock(
    tmp_path: Path,
) -> None:
    """Verify --limit, --email, --applicant-ids, partial failure isolation, and lock protection."""
    valid_docx = _build_valid_docx_bytes("Valid CV text with Python SQL Git")

    def sheets_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "values": [
                    ["Name", "Email", "Role", "CV_Link"],
                    [
                        "Candidate 001",
                        "candidate001@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/missing001/view",
                    ],
                    [
                        "Candidate 002",
                        "candidate002@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/valid002/view",
                    ],
                    [
                        "Candidate 003",
                        "candidate003@example.com",
                        "Junior Developer",
                        "https://drive.google.com/file/d/valid003/view",
                    ],
                ]
            },
        )

    def drive_handler(request: httpx.Request) -> httpx.Response:
        if "missing001" in str(request.url):
            return httpx.Response(404, text="Not Found")
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
            content=valid_docx,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(202, json={"requestId": "req-ok", "addedRowIds": ["row-1"]})

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_test_secret_key")
    settings.env.coda_api_token = SecretStr("coda_test_secret_token")
    settings.env.coda_doc_id = "doc-1"
    settings.env.coda_table_id = "tbl-1"

    sheets_client = GoogleSheetsClient(
        settings=settings,
        spreadsheet_id="sheet-1",
        http_client=httpx.Client(transport=httpx.MockTransport(sheets_handler)),
        token_provider=lambda: "ya29.sheets-tok",
    )
    downloader = GoogleDriveDownloader(
        http_client=httpx.Client(transport=httpx.MockTransport(drive_handler)),
        token_provider=lambda: "ya29.drive-tok",
    )
    coda_client = CodaClient(
        settings=settings,
        api_token="coda_test_secret_token",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
        max_retries=0,
    )
    mock_groq = MagicMock()
    mock_groq.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(80.0)))]
    )

    log_file = tmp_path / "logs" / "pipeline.log"
    lock_file = tmp_path / "logs" / "pipeline.lock"

    # 1. Partial batch failure with --limit 2: APP-001 fails Drive 404, APP-002 still succeeds, APP-003 stopped by limit=2
    summary = run_pipeline(
        dry_run=False,
        limit=2,
        settings=settings,
        sheets_client=sheets_client,
        downloader=downloader,
        coda_client=coda_client,
        groq_client=mock_groq,
        log_file=log_file,
        lock_file=lock_file,
    )
    assert summary.processed_count == 2
    assert summary.failed_count == 1
    assert summary.scored_count == 1
    assert [r.status for r in summary.results] == [
        PipelineStatus.FAILED,
        PipelineStatus.SCORED,
    ]

    # 2. Filter by --email and --applicant-ids
    summary_filtered = run_pipeline(
        dry_run=True,
        email="CANDIDATE003@example.com",
        applicant_ids=["APP-003"],
        settings=settings,
        sheets_client=sheets_client,
        downloader=downloader,
        coda_client=coda_client,
        log_file=log_file,
        lock_file=lock_file,
    )
    assert summary_filtered.filtered_candidates == 1
    assert summary_filtered.processed_count == 1
    assert summary_filtered.results[0].applicant_id == "APP-003"

    # 3. Overlapping execution lock prevents concurrent runs
    with acquire_execution_lock(lock_file=lock_file):
        with pytest.raises(PipelineLockError, match="already"):
            run_pipeline(
                dry_run=True,
                settings=settings,
                sheets_client=sheets_client,
                downloader=downloader,
                coda_client=coda_client,
                log_file=log_file,
                lock_file=lock_file,
            )


def test_stale_execution_lock_recovery_and_safe_cli_overlap_exit(
    tmp_path: Path,
) -> None:
    """Verify stale locks from dead PIDs are recovered automatically, while active locks exit 0 in main()."""
    from run_pipeline import main as runner_main

    lock_file = tmp_path / "logs" / "pipeline.lock"
    log_file = tmp_path / "logs" / "pipeline.log"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Simulate abnormal termination leaving a stale lock with a dead PID
    lock_file.write_text("99999999\nstarted_at=2026-01-01T00:00:00+00:00\n", encoding="utf-8")

    with acquire_execution_lock(lock_file=lock_file):
        assert lock_file.is_file()
        content = lock_file.read_text(encoding="utf-8")
        assert "99999999" not in content

    assert not lock_file.exists()

    # 2. When lock is actively held, main() exits safely with code 0 without starting a second batch
    with acquire_execution_lock(lock_file=lock_file):
        exit_code = runner_main(
            [
                "--dry-run",
                "--limit",
                "1",
                "--lock-file",
                str(lock_file),
                "--log-file",
                str(log_file),
            ]
        )
        assert exit_code == 0


def test_powershell_task_scheduler_scripts_validation() -> None:
    """Verify the 4 PowerShell management scripts exist and pass -ValidateOnly checks without registering."""
    import subprocess
    from app.config import PROJECT_ROOT

    scripts_dir = PROJECT_ROOT / "scripts"
    setup_ps1 = scripts_dir / "setup_daily_task.ps1"
    remove_ps1 = scripts_dir / "remove_daily_task.ps1"
    check_ps1 = scripts_dir / "check_daily_task.ps1"
    run_now_ps1 = scripts_dir / "run_pipeline_now.ps1"

    for script in (setup_ps1, remove_ps1, check_ps1, run_now_ps1):
        assert script.is_file(), f"Missing PowerShell script: {script}"

    res_setup = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup_ps1),
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_setup.returncode == 0
    assert "Daily Applicant Scoring Pipeline" in res_setup.stdout
    assert "09:00 AM" in res_setup.stdout
    assert "ValidationResult  : PASSED" in res_setup.stdout

    res_remove = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(remove_ps1),
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_remove.returncode == 0
    assert "ValidationResult  : PASSED" in res_remove.stdout

    res_run_now = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(run_now_ps1),
            "-DryRun",
            "-Limit",
            "2",
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_run_now.returncode == 0
    assert "run_pipeline.py --dry-run --limit 2" in res_run_now.stdout
    assert "ValidationResult : PASSED" in res_run_now.stdout


def test_github_actions_daily_pipeline_workflow_yaml() -> None:
    """Validate .github/workflows/daily-pipeline.yml syntax, cron (30 3 * * *), secrets, and safety."""
    import yaml
    from app.config import PROJECT_ROOT

    workflow_path = PROJECT_ROOT / ".github" / "workflows" / "daily-pipeline.yml"
    assert workflow_path.is_file(), f"Missing workflow file: {workflow_path}"

    raw_text = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text)
    assert isinstance(parsed, dict)

    # PyYAML 1.1 parses bare 'on' key as boolean True unless quoted; support both
    triggers = parsed.get("on") or parsed.get(True)
    assert isinstance(triggers, dict)

    # 1. Verify schedule: 9:00 AM IST = 03:30 UTC -> "30 3 * * *"
    schedule_list = triggers.get("schedule")
    assert isinstance(schedule_list, list) and len(schedule_list) == 1
    assert schedule_list[0].get("cron") == "30 3 * * *"

    # 2. Verify workflow_dispatch inputs (dry_run boolean and limit)
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs", {})
    assert inputs["dry_run"]["type"] == "boolean"
    assert inputs["dry_run"]["default"] is False
    assert "limit" in inputs

    # 3. Verify minimum permissions and concurrency protection
    assert parsed.get("permissions") == {"contents": "read"}
    concurrency = parsed.get("concurrency", {})
    assert concurrency.get("group") == "daily-applicant-scoring-pipeline"
    assert concurrency.get("cancel-in-progress") is False

    # 4. Verify job runner, required secrets, and credential cleanup
    job = parsed["jobs"]["run-pipeline"]
    assert job["runs-on"] == "ubuntu-latest"

    required_secrets = (
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "CODA_API_TOKEN",
        "CODA_DOC_ID",
        "CODA_TABLE_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_SHEETS_ID",
    )
    for sec in required_secrets:
        assert f"secrets.{sec}" in raw_text, f"Missing secret reference: {sec}"

    assert "python run_pipeline.py" in raw_text
    assert "upload-artifact" not in raw_text

    steps = job["steps"]
    cleanup_steps = [s for s in steps if s.get("if") == "always()"]
    assert len(cleanup_steps) == 1
    assert "rm -f" in cleanup_steps[0]["run"]


def test_github_environment_variables_override_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify GitHub environment variables override config.yaml and .env values cleanly."""
    from app.services.scoring_engine import ScoringEngine

    temp_cred_file = tmp_path / "gh-runner-sa.json"
    temp_cred_file.write_text('{"type": "service_account"}', encoding="utf-8")

    monkeypatch.setenv("GROQ_API_KEY", "gsk_github_env_secret")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("CODA_API_TOKEN", "coda_github_env_token")
    monkeypatch.setenv("CODA_DOC_ID", "gh-doc-id")
    monkeypatch.setenv("CODA_TABLE_ID", "gh-table-id")
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "gh-spreadsheet-id-999")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(temp_cred_file))

    settings = load_settings()
    # Even if config.yaml had a placeholder spreadsheet_id, env GOOGLE_SHEETS_ID overrides it
    settings.google_sheets.spreadsheet_id = "local-yaml-sheet-id"

    assert settings.resolve_google_credentials_path() == temp_cred_file.resolve()

    sheets_client = GoogleSheetsClient(
        settings=settings,
        token_provider=lambda: "ya29.mock",
    )
    assert sheets_client.spreadsheet_id == "gh-spreadsheet-id-999"

    coda_client = CodaClient(settings=settings)
    assert coda_client.doc_id == "gh-doc-id"
    assert coda_client.table_id == "gh-table-id"

    scoring_engine = ScoringEngine(settings=settings, client=MagicMock())
    assert scoring_engine.model_name == "openai/gpt-oss-120b"


