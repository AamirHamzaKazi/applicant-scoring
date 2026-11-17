"""Unit tests for run_e2e_integration_test.py using mocked Groq and Coda transports."""

from __future__ import annotations

import csv
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
from run_e2e_integration_test import (
    load_selected_applicants,
    run_integration_test,
)


def _setup_synthetic_manifest_and_cvs(root: Path, count: int = 5) -> tuple[Path, Path]:
    manifest_csv = root / "applicants_manifest.csv"
    cvs_dir = root / "drive_upload" / "CVs"
    cvs_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for idx in range(1, count + 1):
        app_id = f"APP-{idx:03d}"
        cv_path = cvs_dir / f"{app_id}.docx"
        doc = docx.Document()
        doc.add_paragraph(
            f"Candidate {idx:03d} — Junior Software Developer with 2 years Python, SQL, and Git."
        )
        doc.save(str(cv_path))
        rows.append(
            {
                "applicant_id": app_id,
                "name": f"Candidate {idx:03d}",
                "email": f"candidate{idx:03d}@example.com",
                "role": "Junior Level Software Developer (1-4 years experience)",
                "cv_filename": f"{idx}.docx",
                "cv_path": f"Dataset/CV/{idx}.docx",
            }
        )

    with manifest_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["applicant_id", "name", "email", "role", "cv_filename", "cv_path"],
        )
        writer.writeheader()
        writer.writerows(rows)

    return manifest_csv, cvs_dir


def _mock_groq_client() -> MagicMock:
    payload = json.dumps(
        {
            "criteria": {
                "skills_match": {
                    "score": 82,
                    "evidence": "CV explicitly states Python, SQL, and Git.",
                    "evidence_status": "supported",
                },
                "experience_relevance": {
                    "score": 78,
                    "evidence": "2 years of Junior Software Developer experience.",
                    "evidence_status": "supported",
                },
                "education_fit": {
                    "score": 75,
                    "evidence": "Insufficient evidence in CV",
                    "evidence_status": "insufficient",
                },
                "overall_alignment": {
                    "score": 80,
                    "evidence": "Strong match for Junior Developer requirements.",
                    "evidence_status": "supported",
                },
            },
            "explanation": "Matches required Python, SQL, Git skills and 1-4 years experience.",
        }
    )
    mock = MagicMock()
    mock.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
    )
    return mock


def test_cannot_exceed_3_applicants(tmp_path: Path) -> None:
    """Verify integration runner blocks selecting more than 3 applicants."""
    manifest_csv, cvs_dir = _setup_synthetic_manifest_and_cvs(tmp_path, count=5)
    with pytest.raises(ValueError, match="between 1 and 3"):
        load_selected_applicants(
            manifest_csv=manifest_csv,
            drive_cvs_dir=cvs_dir,
            applicant_ids=["APP-001", "APP-002", "APP-003", "APP-004"],
        )


def test_integration_dry_run_skips_coda_writes_and_verifies_columns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DRY_RUN=true tests parsing, duplicate detection, Groq scoring, and 12-col mapping without Coda POST."""
    manifest_csv, cvs_dir = _setup_synthetic_manifest_and_cvs(tmp_path, count=5)

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_secret_test_key_999999")
    settings.env.coda_api_token = SecretStr("coda-secret-test-999")
    settings.env.coda_doc_id = "doc-e2e"
    settings.env.coda_table_id = "tbl-e2e"

    coda_methods: list[str] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        coda_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        raise AssertionError("DRY_RUN=true must never execute Coda POST writes")

    caplog.set_level(logging.INFO)
    summary = run_integration_test(
        applicant_ids=["APP-001", "APP-002", "APP-004"],
        dry_run=True,
        manifest_csv=manifest_csv,
        drive_cvs_dir=cvs_dir,
        settings=settings,
        groq_client=_mock_groq_client(),
        coda_http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
    )

    assert summary.dry_run is True
    assert summary.passed is True
    assert summary.selected_applicant_ids == ["APP-001", "APP-002", "APP-004"]
    assert all(m == "GET" for m in coda_methods)
    assert len(summary.applicant_metrics) == 3

    for metric in summary.applicant_metrics:
        assert metric.pipeline_status == "scored"
        assert metric.parse_status == "success"
        assert metric.extracted_chars > 0
        assert metric.duplicate_checked is True
        assert metric.is_duplicate is False
        assert metric.criteria_verified is True
        assert metric.evidence_verified is True
        assert metric.coda_columns_verified is True
        assert metric.coda_write_attempted is False
        assert metric.total_score == 79.45
        assert metric.recommendation == "Shortlist"

    assert "provider=groq" in caplog.text
    assert "gsk_secret_test_key_999999" not in caplog.text
    assert "coda-secret-test-999" not in caplog.text


def test_integration_live_mode_inserts_and_verifies_coda_readback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DRY_RUN=false inserts 3 records into mocked Coda and verifies readback across all columns."""
    manifest_csv, cvs_dir = _setup_synthetic_manifest_and_cvs(tmp_path, count=5)

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_live_secret_key_abcdef")
    settings.env.coda_api_token = SecretStr("coda-live-secret-xyz")
    settings.env.coda_doc_id = "doc-live"
    settings.env.coda_table_id = "tbl-live"

    stored_rows: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": stored_rows})

        body = json.loads(request.content.decode("utf-8"))
        cells = body["rows"][0]["cells"]
        values = {c["column"]: c["value"] for c in cells}
        row_id = f"i-row-{len(stored_rows) + 1}"
        stored_rows.append({"id": row_id, "values": values})
        return httpx.Response(202, json={"requestId": "req-live", "addedRowIds": [row_id]})

    caplog.set_level(logging.INFO)
    summary = run_integration_test(
        applicant_ids=["APP-001", "APP-002", "APP-004"],
        dry_run=False,
        manifest_csv=manifest_csv,
        drive_cvs_dir=cvs_dir,
        settings=settings,
        groq_client=_mock_groq_client(),
        coda_http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
        readback_poll_interval_s=0.0,
    )

    assert summary.dry_run is False
    assert summary.passed is True
    assert len(stored_rows) == 3

    for metric in summary.applicant_metrics:
        assert metric.pipeline_status == "scored"
        assert metric.coda_columns_verified is True
        assert metric.coda_write_attempted is True
        assert metric.coda_write_succeeded is True
        assert metric.coda_readback_verified is True
        assert len(metric.coda_row_ids) == 1

    assert "gsk_live_secret_key_abcdef" not in caplog.text
    assert "coda-live-secret-xyz" not in caplog.text


def test_integration_dry_run_default_uses_local_mock_groq(tmp_path: Path) -> None:
    """When dry_run=True and groq_client=None, no real Groq API call is made."""
    manifest_csv, cvs_dir = _setup_synthetic_manifest_and_cvs(tmp_path, count=5)

    settings = load_settings()
    settings.env.groq_api_key = None
    settings.env.coda_api_token = SecretStr("coda-mock-tok")
    settings.env.coda_doc_id = "doc-dry"
    settings.env.coda_table_id = "tbl-dry"

    def coda_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"items": []})

    summary = run_integration_test(
        applicant_ids=["APP-001", "APP-002", "APP-004"],
        dry_run=True,
        live_groq=False,
        manifest_csv=manifest_csv,
        drive_cvs_dir=cvs_dir,
        settings=settings,
        groq_client=None,
        coda_http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
    )

    assert summary.passed is True
    assert len(summary.applicant_metrics) == 3
    assert all(m.pipeline_status == "scored" for m in summary.applicant_metrics)
    assert all(m.coda_write_attempted is False for m in summary.applicant_metrics)


def test_integration_retry_failed_flag_processes_only_failed_and_updates_row(
    tmp_path: Path,
) -> None:
    """--retry-failed processes only Failed Coda records, skips Scored records, and uses PUT update."""
    manifest_csv, cvs_dir = _setup_synthetic_manifest_and_cvs(tmp_path, count=5)

    settings = load_settings()
    settings.env.groq_api_key = SecretStr("gsk_retry_secret_key")
    settings.env.coda_api_token = SecretStr("coda-retry-secret")
    settings.env.coda_doc_id = "doc-retry"
    settings.env.coda_table_id = "tbl-retry"

    rows_by_id: dict[str, dict[str, Any]] = {
        "i-row-1": {
            "id": "i-row-1",
            "values": {
                "Applicant ID": "APP-001",
                "Name": "Candidate 001",
                "Email": "candidate001@example.com",
                "Status": "Failed",
                "Error Note": "Previous Groq 503",
                "Sub-scores": "{}",
            },
        },
        "i-row-2": {
            "id": "i-row-2",
            "values": {
                "Applicant ID": "APP-002",
                "Name": "Candidate 002",
                "Email": "candidate002@example.com",
                "Status": "Scored",
                "Total Score": 85.0,
                "Recommendation": "Shortlist",
                "Reason": "Already scored",
                "Sub-scores": json.dumps({"criteria": {"skills_match": {"score": 85, "evidence": "Python"}}}),
            },
        },
    }
    methods_called: list[str] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        methods_called.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"items": list(rows_by_id.values())})
        if request.method == "PUT":
            body = json.loads(request.content.decode("utf-8"))
            cells = body["row"]["cells"]
            row_id = request.url.path.split("/")[-1]
            rows_by_id[row_id] = {"id": row_id, "values": {c["column"]: c["value"] for c in cells}}
            return httpx.Response(202, json={"requestId": "req-put", "id": row_id})
        raise AssertionError(f"--retry-failed must never POST new rows (got {request.method})")

    # Test with explicit --applicant-ids APP-001 APP-002 and retry_failed=True
    summary = run_integration_test(
        applicant_ids=["APP-001", "APP-002"],
        dry_run=False,
        retry_failed=True,
        manifest_csv=manifest_csv,
        drive_cvs_dir=cvs_dir,
        settings=settings,
        groq_client=_mock_groq_client(),
        coda_http_client=httpx.Client(transport=httpx.MockTransport(coda_handler)),
        readback_poll_interval_s=0.0,
    )

    assert summary.passed is True
    assert "POST" not in methods_called
    assert "PUT" in methods_called
    assert len(rows_by_id) == 2  # No duplicate rows created

    m_001 = summary.applicant_metrics[0]
    assert m_001.applicant_id == "APP-001"
    assert m_001.is_retry is True
    assert m_001.pipeline_status == "scored"
    assert m_001.coda_updated is True
    assert m_001.coda_readback_verified is True
    assert rows_by_id["i-row-1"]["values"]["Status"] == "Scored"
    assert rows_by_id["i-row-1"]["values"]["Error Note"] == ""

    m_002 = summary.applicant_metrics[1]
    assert m_002.applicant_id == "APP-002"
    assert m_002.is_duplicate is True
    assert m_002.pipeline_status == "skipped_duplicate"
    assert m_002.coda_write_attempted is False

