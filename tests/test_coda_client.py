"""Unit tests for app.integrations.coda_client using mocked HTTP transport (no real API calls)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.config import load_settings
from app.integrations.coda_client import (
    CodaApplicantRecord,
    CodaApplicantStatus,
    CodaClient,
    redact_sensitive_text,
    sanitize_coda_cell_value,
    validate_coda_rows_payload,
)
from app.services.scoring_engine import (
    CriterionScore,
    EvidenceStatus,
    Recommendation,
    ScoringResult,
    ScoringStatus,
)

EXPECTED_12_COLUMNS = [
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


def _sample_scoring_result() -> ScoringResult:
    return ScoringResult(
        criterion_scores={
            "skills_match": CriterionScore(
                score=85.0,
                weight=0.40,
                weighted_score=34.0,
                evidence="CV lists Python, SQL, and Git in Core Skills.",
                evidence_status=EvidenceStatus.SUPPORTED,
            ),
            "experience_relevance": CriterionScore(
                score=80.0,
                weight=0.30,
                weighted_score=24.0,
                evidence="3 years as Junior Software Developer at Acme Corp.",
                evidence_status=EvidenceStatus.SUPPORTED,
            ),
            "education_fit": CriterionScore(
                score=75.0,
                weight=0.15,
                weighted_score=11.25,
                evidence="B.Sc. in Computer Science.",
                evidence_status=EvidenceStatus.SUPPORTED,
            ),
            "overall_alignment": CriterionScore(
                score=80.0,
                weight=0.15,
                weighted_score=12.0,
                evidence="Strong alignment with role requirements.",
                evidence_status=EvidenceStatus.SUPPORTED,
            ),
        },
        total_score=81.25,
        recommendation=Recommendation.SHORTLIST,
        explanation="Strong match across Python, SQL, Git, and 3 years experience.",
        status=ScoringStatus.SUCCESS,
        error_message=None,
    )


def test_insert_scored_record_maps_all_12_columns() -> None:
    """Verify Scored applicant insertion maps all 12 columns and stores Sub-scores evidence."""
    captured_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            202,
            json={
                "requestId": "req-abc-123",
                "addedRowIds": ["i-row-001"],
            },
        )

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    settings = load_settings()
    client = CodaClient(
        settings=settings,
        api_token="coda-secret-token-xyz",
        doc_id="doc-123",
        table_id="tbl-456",
        http_client=mock_http,
    )

    scoring_result = _sample_scoring_result()
    record = CodaApplicantRecord.from_scoring_result(
        applicant_id="APP-001",
        name="Candidate 001",
        email="candidate001@example.com",
        role="Junior Level Software Developer (1-4 years experience)",
        cv_link="https://drive.google.com/file/d/cv001",
        scoring_result=scoring_result,
        cv_hash="a" * 64,
        date_processed="2026-11-17T12:00:00+00:00",
    )

    result = client.insert_applicant_record(record)

    assert result.success is True
    assert result.status_code == 202
    assert result.request_id == "req-abc-123"
    assert result.added_row_ids == ["i-row-001"]
    assert result.error_message is None

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req["method"] == "POST"
    assert req["url"] == "https://coda.io/apis/v1/docs/doc-123/tables/tbl-456/rows"
    assert req["headers"]["authorization"] == "Bearer coda-secret-token-xyz"

    # Duplicate/existing rows must never be updated via keyColumns
    assert "keyColumns" not in req["body"]
    cells = req["body"]["rows"][0]["cells"]
    assert [c["column"] for c in cells] == EXPECTED_12_COLUMNS

    cell_map = {c["column"]: c["value"] for c in cells}
    assert cell_map["Applicant ID"] == "APP-001"
    assert cell_map["Name"] == "Candidate 001"
    assert cell_map["Email"] == "candidate001@example.com"
    assert cell_map["Role"] == "Junior Level Software Developer (1-4 years experience)"
    assert cell_map["CV Link"] == "https://drive.google.com/file/d/cv001"
    assert cell_map["Total Score"] == 81.25
    assert cell_map["Recommendation"] == "Shortlist"
    assert cell_map["Reason"] == "Strong match across Python, SQL, Git, and 3 years experience."
    assert cell_map["Date Processed"] == "2026-11-17T12:00:00+00:00"
    assert cell_map["Status"] == "Scored"
    assert cell_map["Error Note"] == ""

    parsed_sub_scores = json.loads(cell_map["Sub-scores"])
    assert parsed_sub_scores["cv_hash"] == "a" * 64
    assert parsed_sub_scores["criteria"]["skills_match"]["score"] == 85.0
    assert (
        parsed_sub_scores["criteria"]["skills_match"]["evidence"]
        == "CV lists Python, SQL, and Git in Core Skills."
    )
    assert parsed_sub_scores["criteria"]["experience_relevance"]["weighted_score"] == 24.0


def test_insert_failed_record_status_and_error_note() -> None:
    """Verify Failed status records map cleanly with Error note and blank Total score."""
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={"requestId": "req-fail-1", "addedRowIds": ["i-row-002"]})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = CodaClient(
        settings=load_settings(),
        api_token="coda-token-123",
        doc_id="doc-123",
        table_id="tbl-456",
        http_client=mock_http,
    )

    failed_record = CodaApplicantRecord.from_scoring_result(
        applicant_id="APP-002",
        name="Candidate 002",
        email="candidate002@example.com",
        role="Junior Level Software Developer (1-4 years experience)",
        cv_link="Dataset/CV/2.docx",
        scoring_result=None,
        cv_hash="b" * 64,
        error_note="Corrupted DOCX file during extraction",
    )

    result = client.insert_applicant_record(failed_record)
    assert result.success is True

    cells = captured_bodies[0]["rows"][0]["cells"]
    assert [c["column"] for c in cells] == EXPECTED_12_COLUMNS
    cell_map = {c["column"]: c["value"] for c in cells}
    assert cell_map["Status"] == CodaApplicantStatus.FAILED.value == "Failed"
    assert cell_map["Total Score"] == ""
    assert cell_map["Recommendation"] == ""
    assert cell_map["Error Note"] == "Corrupted DOCX file during extraction"
    assert json.loads(cell_map["Sub-scores"])["cv_hash"] == "b" * 64


def test_list_existing_records_handles_pagination() -> None:
    """Verify GET .../rows paginates via pageToken and extracts normalized email + CV hash."""
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        page_token = request.url.params.get("pageToken")
        if page_token is None:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "i-1",
                            "values": {
                                "Applicant ID": "APP-001",
                                "Email": "  Candidate001@Example.COM ",
                                "Sub-scores": json.dumps({"cv_hash": "1" * 64, "criteria": {}}),
                            },
                        }
                    ],
                    "nextPageToken": "page-2-token",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "i-2",
                        "values": {
                            "Applicant ID": "APP-002",
                            "Email": "candidate002@example.com",
                            "CV hash": "2" * 64,
                        },
                    }
                ],
            },
        )

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    client = CodaClient(
        settings=load_settings(),
        api_token="token-abc",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=mock_http,
    )

    fetch_result = client.list_existing_records()
    assert fetch_result.success is True
    assert len(requested_urls) == 2
    assert "useColumnNames=true" in requested_urls[0]
    assert "pageToken=page-2-token" in requested_urls[1]
    assert len(fetch_result.records) == 2
    assert fetch_result.records[0].email == "candidate001@example.com"
    assert fetch_result.records[0].cv_hash == "1" * 64
    assert fetch_result.records[1].email == "candidate002@example.com"
    assert fetch_result.records[1].cv_hash == "2" * 64


def test_coda_api_errors_timeout_and_token_redaction(caplog: pytest.LogCaptureFixture) -> None:
    """Verify API errors, timeouts, and token redaction in logs and error messages."""
    secret_token = "super-secret-coda-token-99999"

    # 1. Server error echoing sensitive header/token must be redacted
    def error_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"Upstream failed with Bearer {secret_token} and token={secret_token}"},
        )

    caplog.set_level(logging.DEBUG)
    mock_http_500 = httpx.Client(transport=httpx.MockTransport(error_handler))
    client_500 = CodaClient(
        settings=load_settings(),
        api_token=secret_token,
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=mock_http_500,
    )
    record = CodaApplicantRecord.from_scoring_result(
        applicant_id="APP-003",
        name="Candidate 003",
        email="candidate003@example.com",
        role="Developer",
        scoring_result=_sample_scoring_result(),
    )

    insert_res = client_500.insert_applicant_record(record)
    assert insert_res.success is False
    assert insert_res.status_code == 500
    assert secret_token not in (insert_res.error_message or "")
    assert "[REDACTED]" in (insert_res.error_message or "")
    assert secret_token not in caplog.text
    assert secret_token not in repr(client_500)

    # 2. Timeout handled gracefully without raising uncaught exceptions
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"Timed out sending Authorization Bearer {secret_token}", request=request)

    mock_http_timeout = httpx.Client(transport=httpx.MockTransport(timeout_handler))
    client_timeout = CodaClient(
        settings=load_settings(),
        api_token=secret_token,
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=mock_http_timeout,
        timeout_seconds=1.5,
    )
    timeout_res = client_timeout.insert_applicant_record(record)
    assert timeout_res.success is False
    assert "timed out" in (timeout_res.error_message or "").lower()
    assert secret_token not in (timeout_res.error_message or "")
    assert secret_token not in caplog.text


def test_coda_rate_limit_retries_and_exhaustion() -> None:
    """Verify HTTP 429 triggers Retry-After / backoff retries and fails gracefully if exhausted."""
    sleep_durations: list[float] = []
    attempts = {"count": 0}

    def retry_then_ok_handler(_: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"}, json={"message": "Too Many Requests"})
        return httpx.Response(202, json={"requestId": "req-ok", "addedRowIds": ["i-99"]})

    client_retry = CodaClient(
        settings=load_settings(),
        api_token="tok-123",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(retry_then_ok_handler)),
        max_retries=2,
        sleep_fn=sleep_durations.append,
    )
    record = CodaApplicantRecord.from_scoring_result(
        applicant_id="APP-004",
        name="Candidate 004",
        email="candidate004@example.com",
        role="Developer",
        scoring_result=_sample_scoring_result(),
    )
    ok_res = client_retry.insert_applicant_record(record)
    assert ok_res.success is True
    assert attempts["count"] == 2
    assert sleep_durations == [0.5]

    # Exhausted 429 rate limits fail gracefully
    def always_429_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Rate limit exceeded"})

    client_exhausted = CodaClient(
        settings=load_settings(),
        api_token="tok-123",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(always_429_handler)),
        max_retries=1,
        retry_delay=0.1,
        sleep_fn=lambda _: None,
    )
    fail_res = client_exhausted.list_existing_records()
    assert fail_res.success is False
    assert fail_res.status_code == 429
    assert "429" in (fail_res.error_message or "")


def test_missing_coda_env_vars_handled_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify missing CODA_API_TOKEN, CODA_DOC_ID, or CODA_TABLE_ID returns a safe error without network calls."""
    settings = load_settings()
    monkeypatch.delenv("CODA_API_TOKEN", raising=False)
    monkeypatch.delenv("CODA_DOC_ID", raising=False)
    monkeypatch.delenv("CODA_TABLE_ID", raising=False)

    settings.env.coda_api_token = None
    settings.env.coda_doc_id = None
    settings.env.coda_table_id = None

    def forbid_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Real network call must not occur when configuration is missing")

    no_net_http = httpx.Client(transport=httpx.MockTransport(forbid_network))

    # 1. Missing CODA_API_TOKEN
    client_no_token = CodaClient(settings=settings, http_client=no_net_http)
    res_no_token = client_no_token.list_existing_records()
    assert res_no_token.success is False
    assert "CODA_API_TOKEN" in (res_no_token.error_message or "")

    # 2. Missing CODA_DOC_ID
    client_no_doc = CodaClient(settings=settings, api_token="tok-123", http_client=no_net_http)
    res_no_doc = client_no_doc.list_existing_records()
    assert res_no_doc.success is False
    assert "CODA_DOC_ID" in (res_no_doc.error_message or "")

    # 3. Missing CODA_TABLE_ID
    client_no_table = CodaClient(
        settings=settings, api_token="tok-123", doc_id="doc-123", http_client=no_net_http
    )
    res_no_table = client_no_table.list_existing_records()
    assert res_no_table.success is False
    assert "CODA_TABLE_ID" in (res_no_table.error_message or "")

    redacted = redact_sensitive_text("Error Authorization: Bearer abc.def-123", secret="abc.def-123")
    assert "abc.def-123" not in redacted
    assert "[REDACTED]" in redacted


def test_coda_payload_value_types_for_scored_and_failed_records() -> None:
    """Every cell value in Scored and Failed payloads must be bool, number, str, or array (never None/dict)."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        validate_coda_rows_payload(body)
        captured.append(body)
        return httpx.Response(202, json={"requestId": "req-types", "addedRowIds": ["i-row"]})

    client = CodaClient(
        settings=load_settings(),
        api_token="coda-token-safe",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    # 1. Scored record constructed with nested Sub-scores/evidence dictionary directly
    nested_sub_scores = {
        "criteria": {
            "skills_match": {"score": 90, "evidence": "Python, SQL, Git"},
        },
        "cv_hash": "c" * 64,
    }
    scored_record = CodaApplicantRecord(
        applicant_id="APP-101",
        name="Candidate 101",
        email="candidate101@example.com",
        role="Developer",
        cv_link="https://drive.google.com/file/d/101/view",
        total_score=88.5,
        sub_scores=nested_sub_scores,  # type: ignore[arg-type]
        recommendation="Shortlist",
        reason="Strong fit",
        status=CodaApplicantStatus.SCORED,
        error_note="",
    )
    res_scored = client.insert_applicant_record(scored_record)
    assert res_scored.success is True

    # 2. Failed record constructed from a failed ScoringResult (total_score=None, recommendation=None)
    failed_scoring = ScoringResult(
        criterion_scores={},
        total_score=None,
        recommendation=None,
        explanation="Evaluation could not be completed due to an AI service error.",
        status=ScoringStatus.FAILED,
        error_message="Groq API service unavailable (HTTP 503) after 4 retries",
    )
    failed_record = CodaApplicantRecord.from_scoring_result(
        applicant_id="APP-102",
        name="Candidate 102",
        email="candidate102@example.com",
        role="Developer",
        cv_link="https://drive.google.com/file/d/102/view",
        scoring_result=failed_scoring,
        cv_hash="d" * 64,
    )
    res_failed = client.insert_applicant_record(failed_record)
    assert res_failed.success is True

    assert len(captured) == 2
    for body in captured:
        cells = body["rows"][0]["cells"]
        assert [c["column"] for c in cells] == EXPECTED_12_COLUMNS
        for cell in cells:
            val = cell["value"]
            assert val is not None, f"Column {cell['column']} must never send JSON null"
            assert not isinstance(val, dict), f"Column {cell['column']} must never send raw dict"
            assert isinstance(val, (bool, int, float, str, list))

    scored_map = {c["column"]: c["value"] for c in captured[0]["rows"][0]["cells"]}
    assert isinstance(scored_map["Sub-scores"], str)
    assert json.loads(scored_map["Sub-scores"]) == nested_sub_scores

    failed_map = {c["column"]: c["value"] for c in captured[1]["rows"][0]["cells"]}
    assert failed_map["Status"] == "Failed"
    assert failed_map["Total Score"] == ""
    assert failed_map["Recommendation"] == ""
    assert "503" in failed_map["Error Note"]


def test_coda_payload_validation_rejects_invalid_types_before_api_call() -> None:
    """validate_coda_rows_payload rejects null, dict, NaN, and unsupported objects before HTTP POST."""
    # Direct validator checks
    with pytest.raises(ValueError, match="expected boolean, number, string, or array"):
        validate_coda_rows_payload({"rows": [{"cells": [{"column": "Total Score", "value": None}]}]})

    with pytest.raises(ValueError, match="expected boolean, number, string, or array"):
        validate_coda_rows_payload(
            {"rows": [{"cells": [{"column": "Sub-scores", "value": {"score": 80}}]}]}
        )

    with pytest.raises(ValueError, match="expected boolean, number, string, or array"):
        validate_coda_rows_payload(
            {"rows": [{"cells": [{"column": "Total Score", "value": float("nan")}]}]}
        )

    with pytest.raises(ValueError, match="Invalid array element"):
        validate_coda_rows_payload(
            {"rows": [{"cells": [{"column": "Tags", "value": ["ok", {"bad": 1}]}]}]}
        )

    # sanitize_coda_cell_value handles dict -> JSON string, None -> "", and rejects non-finite float
    assert sanitize_coda_cell_value(None, "Total Score") == ""
    assert json.loads(str(sanitize_coda_cell_value({"a": 1}, "Sub-scores"))) == {"a": 1}
    assert sanitize_coda_cell_value(["a", 1, True], "Tags") == ["a", 1, True]
    with pytest.raises(ValueError, match="Non-finite float"):
        sanitize_coda_cell_value(float("inf"), "Total Score")

    # Pre-flight validation inside insert_applicant_record prevents HTTP request when invalid
    def forbid_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP POST must not be called if payload validation fails")

    client = CodaClient(
        settings=load_settings(),
        api_token="coda-secret-never-leak",
        doc_id="doc-1",
        table_id="tbl-1",
        http_client=httpx.Client(transport=httpx.MockTransport(forbid_network)),
    )
    bad_record = CodaApplicantRecord.model_construct(
        applicant_id="APP-BAD",
        name="Candidate Bad",
        email="bad@example.com",
        role="Developer",
        cv_link="",
        total_score=float("nan"),
        sub_scores="{}",
        recommendation="",
        reason="",
        date_processed="2026-11-17T00:00:00Z",
        status=CodaApplicantStatus.FAILED,
        error_note="",
    )
    res = client.insert_applicant_record(bad_record)
    assert res.success is False
    assert "validation failed" in (res.error_message or "").lower()
    assert "coda-secret-never-leak" not in (res.error_message or "")
