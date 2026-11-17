"""Unit tests for app.services.pipeline and POST /api/v1/applicants using mocked services only."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import docx
from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import SecretStr

from app.config import load_settings
from app.integrations.coda_client import CodaApplicantStatus, CodaClient
from app.main import create_app, get_applicant_pipeline
from app.services.duplicate_detector import DuplicateDetector, DuplicateReason
from app.services.pipeline import (
    ApplicantPipeline,
    GoogleDriveDownloader,
    PipelineStatus,
)
from app.services.scoring_engine import ScoringEngine


def _build_valid_docx_bytes(text: str) -> bytes:
    """Create a real in-memory DOCX payload without writing to project files."""
    buffer = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph(text)
    doc.save(buffer)
    return buffer.getvalue()


def _valid_groq_json(score: float = 80.0) -> str:
    return json.dumps(
        {
            "criteria": {
                "skills_match": {
                    "score": score,
                    "evidence": "CV states Python, SQL, Git, and REST APIs.",
                    "evidence_status": "supported",
                },
                "experience_relevance": {
                    "score": score,
                    "evidence": "2.5 years as Junior Software Developer.",
                    "evidence_status": "supported",
                },
                "education_fit": {
                    "score": score,
                    "evidence": "B.Sc. in Computer Science.",
                    "evidence_status": "supported",
                },
                "overall_alignment": {
                    "score": score,
                    "evidence": "Matches role requirements.",
                    "evidence_status": "supported",
                },
            },
            "explanation": "Candidate aligns well with junior developer requirements.",
        }
    )


def _build_mock_pipeline(
    *,
    drive_handler: Any,
    coda_handler: Any,
    groq_client: Any | None = None,
    groq_key: str = "gsk_mock_groq_secret_key_000",
    coda_token: str = "coda-secret-tok-000",
    webhook_token: str | None = None,
) -> tuple[ApplicantPipeline, Any]:
    """Assemble an ApplicantPipeline with 100% mocked external HTTP/AI transports."""
    settings = load_settings()
    settings.env.groq_api_key = SecretStr(groq_key)
    settings.env.coda_api_token = SecretStr(coda_token)
    settings.env.coda_doc_id = "doc-test"
    settings.env.coda_table_id = "tbl-test"
    settings.env.webhook_auth_token = SecretStr(webhook_token) if webhook_token else None

    drive_http = httpx.Client(transport=httpx.MockTransport(drive_handler))
    coda_http = httpx.Client(transport=httpx.MockTransport(coda_handler))

    downloader = GoogleDriveDownloader(http_client=drive_http)
    coda_client = CodaClient(
        settings=settings,
        api_token=coda_token,
        doc_id="doc-test",
        table_id="tbl-test",
        http_client=coda_http,
        max_retries=0,
    )
    duplicate_detector = DuplicateDetector(coda_client=coda_client)

    mock_groq = groq_client
    if mock_groq is None:
        mock_groq = MagicMock()
        mock_groq.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(80.0)))]
        )

    scoring_engine = ScoringEngine(
        settings=settings,
        api_key=groq_key,
        client=mock_groq,
        max_retries=0,
    )

    pipeline = ApplicantPipeline(
        settings=settings,
        downloader=downloader,
        duplicate_detector=duplicate_detector,
        scoring_engine=scoring_engine,
        coda_client=coda_client,
    )
    return pipeline, settings


def test_complete_pipeline_success_and_temp_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify full 10-step flow: Drive download, SHA-256, dedup, DOCX parse, Groq score, Coda insert, cleanup."""
    docx_bytes = _build_valid_docx_bytes(
        "Candidate 001\nJunior Software Developer (2 years)\nSkills: Python, SQL, Git\nB.Sc. CS"
    )
    expected_hash = hashlib.sha256(docx_bytes).hexdigest()

    drive_requests: list[str] = []
    coda_posts: list[dict[str, Any]] = []

    def drive_handler(request: httpx.Request) -> httpx.Response:
        drive_requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "Content-Disposition": 'attachment; filename="APP-001.docx"',
            },
            content=docx_bytes,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        coda_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            202, json={"requestId": "req-1", "addedRowIds": ["i-row-001"]}
        )

    created_temp_dirs: list[Path] = []
    orig_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = orig_mkdtemp(*args, **kwargs)
        created_temp_dirs.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler, coda_handler=coda_handler
    )

    result = pipeline.process_applicant(
        {
            "applicant_id": "APP-001",
            "name": "Candidate 001",
            "email": "Candidate001@Example.COM",
            "role": "Junior Level Software Developer (1-4 years experience)",
            "cv_link": "https://drive.google.com/file/d/file123/view?usp=drivesdk",
        }
    )

    assert drive_requests == ["https://drive.google.com/uc?export=download&id=file123"]
    assert result.status == PipelineStatus.SCORED
    assert result.applicant_id == "APP-001"
    assert result.email == "candidate001@example.com"
    assert result.cv_hash == expected_hash
    assert result.total_score == 80.0
    assert result.recommendation == "Shortlist"
    assert result.coda_status == CodaApplicantStatus.SCORED
    assert result.coda_inserted is True
    assert result.coda_row_ids == ["i-row-001"]

    # Verify Coda payload has all 12 columns and CV hash
    assert len(coda_posts) == 1
    cell_map = {c["column"]: c["value"] for c in coda_posts[0]["rows"][0]["cells"]}
    assert cell_map["Applicant ID"] == "APP-001"
    assert cell_map["Email"] == "candidate001@example.com"
    assert cell_map["Status"] == "Scored"
    assert cell_map["Total Score"] == 80.0
    assert json.loads(cell_map["Sub-scores"])["cv_hash"] == expected_hash

    # Verify temp directory was cleaned up (Step 10)
    assert len(created_temp_dirs) == 1
    assert not created_temp_dirs[0].exists()


def test_duplicate_email_and_cv_hash_skipped_without_scoring_or_coda_insert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate email and duplicate CV hash are skipped, logged, never scored, and never inserted."""
    docx_bytes = _build_valid_docx_bytes("Duplicate Resume Text")
    existing_hash = hashlib.sha256(docx_bytes).hexdigest()

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="copy.docx"'},
            content=docx_bytes,
        )

    coda_post_calls: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "i-existing",
                            "values": {
                                "Applicant ID": "APP-003",
                                "Email": "existing@example.com",
                                "Sub-scores": json.dumps({"cv_hash": existing_hash, "criteria": {}}),
                            },
                        }
                    ]
                },
            )
        coda_post_calls.append(json.loads(request.content.decode("utf-8")))
        raise AssertionError("Duplicate applicant must never POST to Coda")

    groq_mock = MagicMock()
    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
        groq_client=groq_mock,
    )

    caplog.set_level(logging.INFO)

    # 1. Duplicate email
    dup_email_res = pipeline.process_applicant(
        {
            "applicant_id": "APP-099",
            "name": "Candidate 099",
            "email": "  EXISTING@example.com ",
            "role": "Developer",
            "cv_link": "https://drive.google.com/open?id=abc",
        }
    )
    assert dup_email_res.status == PipelineStatus.SKIPPED_DUPLICATE
    assert dup_email_res.duplicate_reason == DuplicateReason.DUPLICATE_EMAIL
    assert dup_email_res.coda_inserted is False
    assert "duplicate_email" in caplog.text

    # 2. Duplicate CV hash with new email
    dup_hash_res = pipeline.process_applicant(
        {
            "applicant_id": "APP-019",
            "name": "Candidate 019",
            "email": "brand.new@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/open?id=def",
        }
    )
    assert dup_hash_res.status == PipelineStatus.SKIPPED_DUPLICATE
    assert dup_hash_res.duplicate_reason == DuplicateReason.DUPLICATE_CV_HASH
    assert dup_hash_res.coda_inserted is False
    assert "duplicate_cv_hash" in caplog.text

    groq_mock.chat.completions.create.assert_not_called()
    assert coda_post_calls == []


def test_corrupted_cv_creates_failed_coda_record_and_skips_groq() -> None:
    """When CV parsing fails on a corrupted file, Groq is skipped and a Failed Coda record is created."""
    corrupted_bytes = b"PK\x03\x04corrupted-docx-stream"
    coda_posts: list[dict[str, Any]] = []

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="corrupted.docx"'},
            content=corrupted_bytes,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        coda_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={"requestId": "req-fail", "addedRowIds": ["i-fail-1"]})

    groq_mock = MagicMock()
    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
        groq_client=groq_mock,
    )

    result = pipeline.process_applicant(
        {
            "applicant_id": "APP-007",
            "name": "Candidate 007",
            "email": "candidate007@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/corrupt7/view",
        }
    )

    assert result.status == PipelineStatus.FAILED
    assert result.coda_status == CodaApplicantStatus.FAILED
    assert result.coda_inserted is True
    assert "CV parsing failed" in (result.error_message or "")
    groq_mock.chat.completions.create.assert_not_called()

    assert len(coda_posts) == 1
    cell_map = {c["column"]: c["value"] for c in coda_posts[0]["rows"][0]["cells"]}
    assert cell_map["Status"] == "Failed"
    assert cell_map["Total Score"] == ""
    assert "CV parsing failed" in cell_map["Error Note"]


@pytest.mark.parametrize(
    "drive_response_fn,expected_fragment",
    [
        (
            lambda _: httpx.Response(403, text="Forbidden"),
            "inaccessible (HTTP 403)",
        ),
        (
            lambda _: httpx.Response(404, text="Not Found"),
            "not found",
        ),
        (
            lambda _: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text="<html><body>Google Sign In</body></html>",
            ),
            "requires authentication or returned an HTML page",
        ),
        (
            lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=req)),
            "Timed out downloading CV",
        ),
    ],
)
def test_inaccessible_drive_files_create_failed_coda_record(
    drive_response_fn: Any, expected_fragment: str
) -> None:
    """Inaccessible Drive URLs (403/404/HTML login/timeout) create Failed Coda records without crashing."""
    coda_posts: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        coda_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={"requestId": "req-drv", "addedRowIds": ["i-drv"]})

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_response_fn,
        coda_handler=coda_handler,
    )

    result = pipeline.process_applicant(
        {
            "applicant_id": "APP-008",
            "name": "Candidate 008",
            "email": "candidate008@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/private8/view",
        }
    )

    assert result.status == PipelineStatus.FAILED
    assert result.coda_status == CodaApplicantStatus.FAILED
    assert result.coda_inserted is True
    assert expected_fragment.lower() in (result.error_message or "").lower()
    assert len(coda_posts) == 1


def test_groq_and_coda_failures_handled_gracefully_and_secrets_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Groq failure records Failed row in Coda; Coda failure degrades cleanly without leaking secrets."""
    docx_bytes = _build_valid_docx_bytes("Candidate 009\nPython and SQL Developer")
    secret_groq_key = "gsk_SECRETGROQKEY1234567890"
    secret_coda_tok = "CODA-SECRET-TOKEN-456"

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="APP-009.docx"'},
            content=docx_bytes,
        )

    # 1. Groq fails with exception containing secret -> Failed Coda row recorded, secret scrubbed
    coda_posts: list[dict[str, Any]] = []

    def ok_coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        coda_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={"requestId": "req-g", "addedRowIds": ["i-g"]})

    failing_groq = MagicMock()
    failing_groq.chat.completions.create.side_effect = RuntimeError(
        f"Upstream error using key {secret_groq_key}"
    )

    caplog.set_level(logging.INFO)
    pipeline_groq_fail, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=ok_coda_handler,
        groq_client=failing_groq,
        groq_key=secret_groq_key,
        coda_token=secret_coda_tok,
    )

    res_g = pipeline_groq_fail.process_applicant(
        {
            "applicant_id": "APP-009",
            "name": "Candidate 009",
            "email": "candidate009@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/id9/view",
        }
    )
    assert res_g.status == PipelineStatus.FAILED
    assert res_g.coda_status == CodaApplicantStatus.FAILED
    assert res_g.coda_inserted is True
    assert secret_groq_key not in (res_g.error_message or "")
    assert "[REDACTED]" in (res_g.error_message or "")
    assert len(coda_posts) == 1

    # 2. Coda POST fails with HTTP 500 echoing Bearer token -> returns failed without crash or secret leak
    def failing_coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            500, json={"message": f"Coda error with Bearer {secret_coda_tok}"}
        )

    pipeline_coda_fail, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=failing_coda_handler,
        groq_key=secret_groq_key,
        coda_token=secret_coda_tok,
    )

    res_c = pipeline_coda_fail.process_applicant(
        {
            "applicant_id": "APP-010",
            "name": "Candidate 010",
            "email": "candidate010@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/id10/view",
        }
    )
    assert res_c.status == PipelineStatus.FAILED
    assert res_c.coda_inserted is False
    assert secret_coda_tok not in (res_c.error_message or "")
    assert "[REDACTED]" in (res_c.error_message or "")
    assert secret_groq_key not in caplog.text
    assert secret_coda_tok not in caplog.text


def test_batch_isolation_one_failure_does_not_stop_others() -> None:
    """Verify process_batch processes remaining valid applicants even when earlier ones fail."""
    valid_docx = _build_valid_docx_bytes("Candidate Valid\nPython, SQL, Git")

    def drive_handler(request: httpx.Request) -> httpx.Response:
        if "bad" in str(request.url):
            return httpx.Response(404, text="Missing")
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="ok.docx"'},
            content=valid_docx,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(202, json={"requestId": "req-b", "addedRowIds": ["i-b"]})

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler, coda_handler=coda_handler
    )

    results = pipeline.process_batch(
        [
            {"name": "", "email": "bad-payload", "role": "Dev", "cv_link": "https://x.com"},
            {
                "applicant_id": "APP-BAD",
                "name": "Broken Drive",
                "email": "broken@example.com",
                "role": "Dev",
                "cv_link": "https://drive.google.com/file/d/bad/view",
            },
            {
                "applicant_id": "APP-GOOD",
                "name": "Good Candidate",
                "email": "good@example.com",
                "role": "Dev",
                "cv_link": "https://drive.google.com/file/d/good/view",
            },
        ]
    )

    assert [r.status for r in results] == [
        PipelineStatus.FAILED,
        PipelineStatus.FAILED,
        PipelineStatus.SCORED,
    ]
    assert results[2].coda_inserted is True
    assert results[2].total_score == 80.0


def test_api_ingest_endpoint_auth_validation_and_status_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify POST /api/v1/applicants handles auth, Pydantic 422, 201 Created, 200 OK, and 502."""
    valid_docx = _build_valid_docx_bytes("API Candidate\nPython SQL Git")
    mode = {"drive_ok": True, "coda_post_ok": True, "coda_existing": []}

    def drive_handler(_: httpx.Request) -> httpx.Response:
        if not mode["drive_ok"]:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="cv.docx"'},
            content=valid_docx,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": mode["coda_existing"]})
        if not mode["coda_post_ok"]:
            return httpx.Response(503, json={"message": "Coda down"})
        return httpx.Response(202, json={"requestId": "req-api", "addedRowIds": ["i-api-1"]})

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
        webhook_token="expected-webhook-secret",
    )

    app = create_app()
    app.dependency_overrides[get_applicant_pipeline] = lambda: pipeline

    # Configure WEBHOOK_AUTH_TOKEN in env for verify_webhook_auth dependency
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "expected-webhook-secret")
    client = TestClient(app)

    valid_payload = {
        "name": "Candidate 001",
        "email": "candidate001@example.com",
        "role": "Junior Level Software Developer (1-4 years experience)",
        "cv_link": "https://drive.google.com/file/d/doc1/view",
        "source": "google_sheets",  # Extra field from Google Apps Script ignored cleanly
    }

    # 1. Missing/invalid auth token -> 401
    unauth_resp = client.post("/api/v1/applicants", json=valid_payload)
    assert unauth_resp.status_code == 401
    assert "expected-webhook-secret" not in unauth_resp.text

    wrong_auth_resp = client.post(
        "/api/v1/applicants",
        json=valid_payload,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong_auth_resp.status_code == 401

    # 2. Invalid payload (bad email, missing role) -> 422
    invalid_resp = client.post(
        "/api/v1/applicants",
        json={"name": "Candidate 001", "email": "not-an-email", "role": "", "cv_link": ""},
        headers={"X-Webhook-Token": "expected-webhook-secret"},
    )
    assert invalid_resp.status_code == 422

    # 3. Valid scored applicant (no applicant_id passed -> derived automatically) -> 201
    created_resp = client.post(
        "/api/v1/applicants",
        json=valid_payload,
        headers={"Authorization": "Bearer expected-webhook-secret"},
    )
    assert created_resp.status_code == 201
    body = created_resp.json()
    assert body["status"] == "scored"
    assert body["coda_status"] == "Scored"
    assert body["coda_inserted"] is True
    assert body["total_score"] == 80.0
    assert body["applicant_id"].startswith("APP-")

    # 4. Duplicate email -> 200 OK skipped_duplicate
    mode["coda_existing"] = [
        {"id": "i-1", "values": {"Applicant ID": "APP-001", "Email": "candidate001@example.com"}}
    ]
    dup_resp = client.post(
        "/api/v1/applicants",
        json=valid_payload,
        headers={"X-Webhook-Token": "expected-webhook-secret"},
    )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["status"] == "skipped_duplicate"
    assert dup_resp.json()["duplicate_reason"] == "duplicate_email"

    # 5. Inaccessible Drive file logged to Coda as Failed -> 200 OK
    mode["coda_existing"] = []
    mode["drive_ok"] = False
    drv_fail_resp = client.post(
        "/api/v1/applicants",
        json={
            "applicant_id": "APP-015",
            "name": "Candidate 015",
            "email": "candidate015@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/private15/view",
        },
        headers={"X-Webhook-Token": "expected-webhook-secret"},
    )
    assert drv_fail_resp.status_code == 200
    assert drv_fail_resp.json()["status"] == "failed"
    assert drv_fail_resp.json()["coda_status"] == "Failed"
    assert drv_fail_resp.json()["coda_inserted"] is True

    # 6. Coda insertion failure -> 502 Bad Gateway
    mode["drive_ok"] = True
    mode["coda_post_ok"] = False
    coda_fail_resp = client.post(
        "/api/v1/applicants",
        json={
            "applicant_id": "APP-016",
            "name": "Candidate 016",
            "email": "candidate016@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/doc16/view",
        },
        headers={"X-Webhook-Token": "expected-webhook-secret"},
    )
    assert coda_fail_resp.status_code == 502
    assert coda_fail_resp.json()["coda_inserted"] is False

    # 7. Optional auth: when WEBHOOK_AUTH_TOKEN unset, requests succeed without auth header
    monkeypatch.delenv("WEBHOOK_AUTH_TOKEN", raising=False)
    app_no_auth = create_app()
    # Clear cached settings token on pipeline/settings dependency cleanly
    no_auth_settings = load_settings()
    no_auth_settings.env.webhook_auth_token = None
    from app.config import get_settings

    app_no_auth.dependency_overrides[get_settings] = lambda: no_auth_settings
    app_no_auth.dependency_overrides[get_applicant_pipeline] = lambda: pipeline
    mode["coda_post_ok"] = True
    no_auth_client = TestClient(app_no_auth)
    ok_open_resp = no_auth_client.post(
        "/api/v1/applicants",
        json={
            "applicant_id": "APP-017",
            "name": "Candidate 017",
            "email": "candidate017@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/doc17/view",
        },
    )
    assert ok_open_resp.status_code == 201


def test_retry_failed_applicant_updates_existing_coda_row_and_clears_error_note() -> None:
    """Retrying a Failed applicant preserves Applicant ID, updates Coda row via PUT, and clears Error Note."""
    docx_bytes = _build_valid_docx_bytes(
        "Candidate 001\nJunior Software Developer (3 years)\nSkills: Python, SQL, Git"
    )
    old_hash = "0" * 64
    new_hash = hashlib.sha256(docx_bytes).hexdigest()

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="APP-001.docx"'},
            content=docx_bytes,
        )

    coda_methods: list[tuple[str, str]] = []
    put_payloads: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        coda_methods.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "i-existing-failed-001",
                            "values": {
                                "Applicant ID": "APP-001",
                                "Name": "Candidate 001",
                                "Email": "candidate001@example.com",
                                "Status": "Failed",
                                "Error Note": "Groq API rate limit exceeded",
                                "Sub-scores": json.dumps({"cv_hash": old_hash, "criteria": {}}),
                            },
                        }
                    ]
                },
            )
        if request.method == "PUT":
            put_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                202, json={"requestId": "req-put-1", "id": "i-existing-failed-001"}
            )
        raise AssertionError(f"Expected PUT update, but got {request.method} (must not duplicate row)")

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
    )

    # Submit without explicit applicant_id (or different id) — existing APP-001 must be preserved!
    result = pipeline.process_applicant(
        {
            "name": "Candidate 001",
            "email": "candidate001@example.com",
            "role": "Junior Level Software Developer (1-4 years experience)",
            "cv_link": "https://drive.google.com/file/d/file001/view",
        }
    )

    assert result.status == PipelineStatus.SCORED
    assert result.is_retry is True
    assert result.coda_updated is True
    assert result.coda_inserted is True
    assert result.applicant_id == "APP-001"
    assert result.coda_row_ids == ["i-existing-failed-001"]
    assert result.cv_hash == new_hash  # CV hash changed between retries and updated

    assert [m for m, _ in coda_methods] == ["GET", "PUT"]
    assert coda_methods[1][1].endswith("/rows/i-existing-failed-001")
    assert len(put_payloads) == 1

    cell_map = {c["column"]: c["value"] for c in put_payloads[0]["row"]["cells"]}
    assert cell_map["Applicant ID"] == "APP-001"
    assert cell_map["Status"] == "Scored"
    assert cell_map["Total Score"] == 80.0
    assert cell_map["Recommendation"] == "Shortlist"
    assert cell_map["Error Note"] == ""  # Error Note cleared on successful retry
    assert json.loads(cell_map["Sub-scores"])["cv_hash"] == new_hash


def test_retry_failed_applicant_fails_again_updates_same_row_and_preserves_hash() -> None:
    """When a retried Failed applicant fails again, the existing Coda row is updated in-place."""
    preserved_hash = "a1b2" * 16
    put_payloads: list[dict[str, Any]] = []

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Drive file temporarily forbidden")

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "i-failed-row-2",
                            "values": {
                                "Applicant ID": "APP-002",
                                "Email": "candidate002@example.com",
                                "Status": "Failed",
                                "Error Note": "Initial timeout",
                                "Sub-scores": json.dumps({"cv_hash": preserved_hash, "criteria": {}}),
                            },
                        }
                    ]
                },
            )
        if request.method == "PUT":
            put_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(202, json={"requestId": "req-put-fail", "id": "i-failed-row-2"})
        raise AssertionError(f"Unexpected {request.method}: Failed retry must update existing row")

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
    )

    result = pipeline.process_applicant(
        {
            "applicant_id": "APP-002",
            "name": "Candidate 002",
            "email": "candidate002@example.com",
            "role": "Developer",
            "cv_link": "https://drive.google.com/file/d/private2/view",
        }
    )

    assert result.status == PipelineStatus.FAILED
    assert result.is_retry is True
    assert result.coda_updated is True
    assert result.coda_inserted is True
    assert result.applicant_id == "APP-002"
    assert result.cv_hash == preserved_hash
    assert len(put_payloads) == 1

    cell_map = {c["column"]: c["value"] for c in put_payloads[0]["row"]["cells"]}
    assert cell_map["Applicant ID"] == "APP-002"
    assert cell_map["Status"] == "Failed"
    assert cell_map["Total Score"] == ""
    assert cell_map["Recommendation"] == ""
    assert "403" in cell_map["Error Note"]
    assert json.loads(cell_map["Sub-scores"])["cv_hash"] == preserved_hash


def test_concurrent_retry_protection_skips_overlapping_execution() -> None:
    """Concurrent threads retrying the same Failed applicant execute once and skip the overlap."""
    import threading

    docx_bytes = _build_valid_docx_bytes("Candidate 004\nPython SQL Git Developer")
    enter_scoring_event = threading.Event()
    release_scoring_event = threading.Event()
    put_count = {"count": 0}

    def drive_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Disposition": 'attachment; filename="APP-004.docx"'},
            content=docx_bytes,
        )

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "i-failed-4",
                            "values": {
                                "Applicant ID": "APP-004",
                                "Email": "candidate004@example.com",
                                "Status": "Failed",
                                "Sub-scores": "{}",
                            },
                        }
                    ]
                },
            )
        if request.method == "PUT":
            put_count["count"] += 1
            return httpx.Response(202, json={"requestId": "req-4", "id": "i-failed-4"})
        raise AssertionError("Must not POST duplicate row")

    slow_groq = MagicMock()

    def slow_create(**_: Any) -> Any:
        enter_scoring_event.set()
        release_scoring_event.wait(timeout=5.0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(84.0)))]
        )

    slow_groq.chat.completions.create.side_effect = slow_create

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
        groq_client=slow_groq,
    )

    payload = {
        "applicant_id": "APP-004",
        "name": "Candidate 004",
        "email": "candidate004@example.com",
        "role": "Developer",
        "cv_link": "https://drive.google.com/file/d/id4/view",
    }

    first_out: list[ Any] = []

    def worker() -> None:
        first_out.append(pipeline.process_applicant(payload))

    t1 = threading.Thread(target=worker)
    t1.start()
    assert enter_scoring_event.wait(timeout=5.0)

    # While t1 is in-flight scoring APP-004, a second concurrent attempt must be skipped!
    second_res = pipeline.process_applicant(payload)
    assert second_res.status == PipelineStatus.SKIPPED_DUPLICATE
    assert second_res.duplicate_reason == DuplicateReason.PROCESSING_IN_PROGRESS

    release_scoring_event.set()
    t1.join(timeout=5.0)

    assert len(first_out) == 1
    assert first_out[0].status == PipelineStatus.SCORED
    assert first_out[0].coda_updated is True
    assert put_count["count"] == 1


def test_pipeline_supports_png_scanned_pdf_legacy_doc_and_isolates_corrupt_files() -> None:
    """Verify pipeline scores PNG, scanned PDF, and legacy DOC CVs while isolating corrupt PDFs without calling Groq."""
    samples_dir = Path(__file__).resolve().parent / "samples"
    png_bytes = (samples_dir / "sample_cv.png").read_bytes()
    scanned_pdf_bytes = (samples_dir / "sample_scanned_cv.pdf").read_bytes()
    doc_bytes = (samples_dir / "sample_cv.doc").read_bytes()
    corrupt_pdf_bytes = (samples_dir / "corrupt_cv.pdf").read_bytes()

    def drive_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "png_cv" in url:
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png_bytes)
        if "scanned_pdf" in url:
            return httpx.Response(
                200, headers={"Content-Type": "application/pdf"}, content=scanned_pdf_bytes
            )
        if "legacy_doc" in url:
            return httpx.Response(
                200, headers={"Content-Type": "application/msword"}, content=doc_bytes
            )
        return httpx.Response(
            200, headers={"Content-Type": "application/pdf"}, content=corrupt_pdf_bytes
        )

    coda_posts: list[dict[str, Any]] = []

    def coda_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        coda_posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={"requestId": "req-edge", "addedRowIds": ["i-edge"]})

    mock_groq = MagicMock()
    mock_groq.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=_valid_groq_json(80.0)))]
    )

    pipeline, _ = _build_mock_pipeline(
        drive_handler=drive_handler,
        coda_handler=coda_handler,
        groq_client=mock_groq,
    )

    results = pipeline.process_batch(
        [
            {
                "applicant_id": "APP-PNG",
                "name": "PNG Candidate",
                "email": "png@example.com",
                "role": "Developer",
                "cv_link": "https://drive.google.com/file/d/png_cv/view",
            },
            {
                "applicant_id": "APP-CORRUPT",
                "name": "Corrupt Candidate",
                "email": "corrupt@example.com",
                "role": "Developer",
                "cv_link": "https://drive.google.com/file/d/corrupt_pdf/view",
            },
            {
                "applicant_id": "APP-SCANNED",
                "name": "Scanned PDF Candidate",
                "email": "scanned@example.com",
                "role": "Developer",
                "cv_link": "https://drive.google.com/file/d/scanned_pdf/view",
            },
            {
                "applicant_id": "APP-DOC",
                "name": "Legacy DOC Candidate",
                "email": "doc@example.com",
                "role": "Developer",
                "cv_link": "https://drive.google.com/file/d/legacy_doc/view",
            },
        ]
    )

    assert [r.status for r in results] == [
        PipelineStatus.SCORED,
        PipelineStatus.FAILED,
        PipelineStatus.SCORED,
        PipelineStatus.SCORED,
    ]
    # Groq was called only for the 3 valid CVs (never for the corrupt PDF)
    assert mock_groq.chat.completions.create.call_count == 3
    assert "Corrupted or unreadable PDF file" in (results[1].error_message or "")


