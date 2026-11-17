"""Unit tests for app.services.duplicate_detector using mocked Coda responses."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from app.config import load_settings
from app.integrations.coda_client import CodaClient
from app.services.duplicate_detector import (
    DuplicateDetector,
    DuplicateReason,
    compute_file_sha256,
)


def _mock_coda_client(existing_items: list[dict], tracker: dict | None = None) -> CodaClient:
    """Create a CodaClient backed by MockTransport returning predetermined existing rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        if tracker is not None:
            tracker.setdefault("methods", []).append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"items": existing_items})
        raise AssertionError(
            f"Unexpected write HTTP call during duplicate detection: {request.method} {request.url}"
        )

    return CodaClient(
        settings=load_settings(),
        api_token="mock-token",
        doc_id="doc-123",
        table_id="tbl-456",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_duplicate_email_detection_skips_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify duplicate normalized emails are skipped, logged as duplicate_email, and never written."""
    cv_file = tmp_path / "new_cv.txt"
    cv_file.write_text("Unique CV contents for candidate", encoding="utf-8")

    tracker: dict[str, list[str]] = {}
    coda_client = _mock_coda_client(
        [
            {
                "id": "i-existing-1",
                "values": {
                    "Applicant ID": "APP-001",
                    "Email": "candidate001@example.com",
                    "Sub-scores": json.dumps({"cv_hash": "f" * 64, "criteria": {}}),
                },
            }
        ],
        tracker=tracker,
    )

    detector = DuplicateDetector(coda_client=coda_client)
    caplog.set_level(logging.INFO)

    result = detector.check_applicant(
        applicant_id="APP-099",
        email="candidate001@example.com",
        cv_path=cv_file,
    )

    assert result.is_duplicate is True
    assert result.action == "skip"
    assert result.reason == DuplicateReason.DUPLICATE_EMAIL
    assert result.matched_reasons == [DuplicateReason.DUPLICATE_EMAIL]
    assert result.matched_row_id == "i-existing-1"
    assert result.matched_applicant_id == "APP-001"
    assert result.check_status == "ok"
    assert "duplicate_email" in caplog.text
    assert tracker["methods"] == ["GET"]


def test_case_insensitive_and_whitespace_email_matching(tmp_path: Path) -> None:
    """Verify email matching is case-insensitive and whitespace-normalized on both sides."""
    cv_file = tmp_path / "unique_cv.txt"
    cv_file.write_text("Different resume text", encoding="utf-8")

    coda_client = _mock_coda_client(
        [
            {
                "id": "i-existing-2",
                "values": {
                    "Applicant ID": "APP-002",
                    "Email": "  Alice.Smith@Example.COM  ",
                    "Sub-scores": json.dumps({"cv_hash": "0" * 64, "criteria": {}}),
                },
            }
        ]
    )

    detector = DuplicateDetector(coda_client=coda_client)
    result = detector.check_applicant(
        applicant_id="APP-100",
        email="alice.smith@example.com ",
        cv_path=cv_file,
    )

    assert result.is_duplicate is True
    assert result.action == "skip"
    assert result.normalized_email == "alice.smith@example.com"
    assert result.reason == DuplicateReason.DUPLICATE_EMAIL


def test_duplicate_cv_hash_detection_skips_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify identical CV content under a different email/filename triggers duplicate_cv_hash."""
    original_cv = tmp_path / "original.docx"
    original_cv.write_bytes(b"identical-resume-binary-payload-12345")
    expected_hash = compute_file_sha256(original_cv)

    reuploaded_cv = tmp_path / "renamed_resume_copy.docx"
    reuploaded_cv.write_bytes(b"identical-resume-binary-payload-12345")

    tracker: dict[str, list[str]] = {}
    coda_client = _mock_coda_client(
        [
            {
                "id": "i-existing-3",
                "values": {
                    "Applicant ID": "APP-003",
                    "Email": "first.applicant@example.com",
                    "Sub-scores": json.dumps({"cv_hash": expected_hash, "criteria": {}}),
                },
            }
        ],
        tracker=tracker,
    )

    detector = DuplicateDetector(coda_client=coda_client)
    caplog.set_level(logging.INFO)

    result = detector.check_applicant(
        applicant_id="APP-019",
        email="different.email@example.com",
        cv_path=reuploaded_cv,
    )

    assert result.is_duplicate is True
    assert result.action == "skip"
    assert result.reason == DuplicateReason.DUPLICATE_CV_HASH
    assert result.matched_reasons == [DuplicateReason.DUPLICATE_CV_HASH]
    assert result.cv_hash == expected_hash
    assert result.matched_applicant_id == "APP-003"
    assert "duplicate_cv_hash" in caplog.text
    assert tracker["methods"] == ["GET"]


def test_unique_applicant_allowed_and_not_skipped(tmp_path: Path) -> None:
    """Verify non-duplicate applicant passes duplicate detection with action='process'."""
    cv_file = tmp_path / "fresh_cv.txt"
    cv_file.write_text("Fresh Applicant Resume Content", encoding="utf-8")

    coda_client = _mock_coda_client(
        [
            {
                "id": "i-existing-1",
                "values": {
                    "Applicant ID": "APP-001",
                    "Email": "existing@example.com",
                    "Sub-scores": json.dumps({"cv_hash": "9" * 64, "criteria": {}}),
                },
            }
        ]
    )

    detector = DuplicateDetector(coda_client=coda_client)
    result = detector.check_applicant(
        applicant_id="APP-040",
        email="new.candidate@example.com",
        cv_path=cv_file,
    )

    assert result.is_duplicate is False
    assert result.action == "process"
    assert result.reason is None
    assert result.check_status == "ok"
    assert result.cv_hash == compute_file_sha256(cv_file)


def test_duplicate_detector_handles_coda_api_failure_gracefully(tmp_path: Path) -> None:
    """Verify Coda API errors during duplicate check fail cleanly without crashing."""
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text("Candidate CV", encoding="utf-8")

    def failing_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "Coda temporarily unavailable"})

    coda_client = CodaClient(
        settings=load_settings(),
        api_token="secret-tok",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(failing_handler)),
    )

    detector = DuplicateDetector(coda_client=coda_client)
    result = detector.check_applicant(
        applicant_id="APP-050",
        email="candidate050@example.com",
        cv_path=cv_file,
    )

    assert result.is_duplicate is False
    assert result.action == "error"
    assert result.check_status == "error"
    assert "503" in (result.error_message or "")
    assert "secret-tok" not in (result.error_message or "")


def test_existing_failed_applicant_allows_retry(tmp_path: Path) -> None:
    """Existing Failed record in Coda returns is_retry=True, is_duplicate=False, action='retry'."""
    cv_file = tmp_path / "app001.docx"
    cv_file.write_bytes(b"cv-content-001")
    cv_hash = compute_file_sha256(cv_file)

    coda_client = _mock_coda_client(
        [
            {
                "id": "i-failed-row-1",
                "values": {
                    "Applicant ID": "APP-001",
                    "Email": "candidate001@example.com",
                    "Status": "Failed",
                    "Sub-scores": json.dumps({"cv_hash": cv_hash, "criteria": {}}),
                },
            }
        ]
    )

    detector = DuplicateDetector(coda_client=coda_client)
    res = detector.check_applicant(
        applicant_id="APP-999",
        email="candidate001@example.com",
        cv_path=cv_file,
    )

    assert res.is_duplicate is False
    assert res.is_retry is True
    assert res.action == "retry"
    assert res.existing_status == "Failed"
    assert res.matched_row_id == "i-failed-row-1"
    assert res.matched_applicant_id == "APP-001"
    assert res.existing_cv_hash == cv_hash


def test_existing_processing_applicant_is_skipped(tmp_path: Path) -> None:
    """Existing Processing record in Coda or in-flight lock skips to prevent concurrent processing."""
    cv_file = tmp_path / "app002.docx"
    cv_file.write_bytes(b"cv-content-002")

    # 1. Coda row has Status == 'Processing'
    coda_client = _mock_coda_client(
        [
            {
                "id": "i-proc-row",
                "values": {
                    "Applicant ID": "APP-002",
                    "Email": "candidate002@example.com",
                    "Status": "Processing",
                    "Sub-scores": "{}",
                },
            }
        ]
    )
    detector = DuplicateDetector(coda_client=coda_client)
    res_coda_proc = detector.check_applicant(
        applicant_id="APP-002",
        email="candidate002@example.com",
        cv_path=cv_file,
    )
    assert res_coda_proc.is_duplicate is True
    assert res_coda_proc.is_retry is False
    assert res_coda_proc.action == "skip"
    assert res_coda_proc.existing_status == "Processing"
    assert res_coda_proc.reason == DuplicateReason.PROCESSING_IN_PROGRESS

    # 2. In-memory concurrent processing guard blocks overlapping execution
    empty_coda_detector = DuplicateDetector(coda_client=_mock_coda_client([]))
    with empty_coda_detector.processing_guard(
        applicant_id="APP-005",
        email="candidate005@example.com",
    ) as acquired:
        assert acquired is True
        overlap_res = empty_coda_detector.check_applicant(
            applicant_id="APP-005",
            email="candidate005@example.com",
            cv_path=cv_file,
        )
        assert overlap_res.is_duplicate is True
        assert overlap_res.is_retry is False
        assert overlap_res.action == "skip"
        assert overlap_res.reason == DuplicateReason.PROCESSING_IN_PROGRESS

