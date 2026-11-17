"""Unit tests for app.services.scoring_engine using mocked Groq responses only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import groq
import httpx
import pytest

from app.config import load_settings
from app.services.scoring_engine import (
    AI_PROVIDER,
    DEFAULT_GROQ_BASE_URL,
    DEFAULT_GROQ_MODEL,
    EvidenceStatus,
    INSUFFICIENT_EVIDENCE_TEXT,
    Recommendation,
    ScoringEngine,
    ScoringStatus,
    _normalize_groq_sdk_base_url,
    build_evaluation_prompt,
    build_response_json_schema,
    build_system_instruction,
    determine_recommendation,
)


def _make_completion(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _make_mock_client(response_payload: dict[str, Any] | str) -> MagicMock:
    """Create a mocked Groq client returning a chat completion with serialized JSON content."""
    raw_text = (
        response_payload
        if isinstance(response_payload, str)
        else json.dumps(response_payload)
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_completion(raw_text)
    return mock_client


def _make_http_response(status_code: int, message: str) -> httpx.Response:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status_code, json={"error": {"message": message}}, request=req)


def test_successful_groq_response_and_deterministic_python_weighting() -> None:
    """Verify Groq openai/gpt-oss-120b call, strict JSON schema, and deterministic Python weighting."""
    settings = load_settings()
    # Configured weights in config.yaml:
    # skills_match: 0.40, experience_relevance: 0.30, education_fit: 0.15, overall_alignment: 0.15
    # Expected total: 85*0.40 (34.0) + 80*0.30 (24.0) + 60*0.15 (9.0) + 80*0.15 (12.0) = 79.0
    groq_payload = {
        "criteria": {
            "skills_match": {
                "score": 85,
                "evidence": "CV states: 3 years building Python REST APIs, SQL queries, and Git workflows.",
                "evidence_status": "supported",
            },
            "experience_relevance": {
                "score": 80,
                "evidence": "Worked as Junior Software Developer at Acme Corp (2022-2025, 3 years).",
                "evidence_status": "supported",
            },
            "education_fit": {
                "score": 60,
                "evidence": "B.Sc. in Information Systems listed in Education section.",
                "evidence_status": "supported",
            },
            "overall_alignment": {
                "score": 80,
                "evidence": "Matches 1-4 years experience range and core OOP/SQL/Git skills.",
                "evidence_status": "supported",
            },
        },
        # Conflicting AI-supplied fields that Python MUST ignore:
        "total_score": 12.0,
        "recommendation": "Reject",
        "explanation": "Strong junior developer profile with documented Python, SQL, and Git experience.",
    }

    mock_client = _make_mock_client(groq_payload)
    engine = ScoringEngine(settings=settings, client=mock_client)

    assert engine.provider == AI_PROVIDER == "groq"
    assert engine.model_name == DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"
    assert engine.base_url == DEFAULT_GROQ_BASE_URL == "https://api.groq.com/openai/v1"
    assert _normalize_groq_sdk_base_url("https://api.groq.com/openai/v1") == "https://api.groq.com"

    result = engine.score_cv(
        "Candidate 001\nJunior Software Developer at Acme Corp (2022-2025)\n"
        "Skills: Python, SQL, Git, OOP\nB.Sc. Information Systems"
    )

    assert result.status == ScoringStatus.SUCCESS
    assert result.error_message is None
    assert result.total_score == 79.0
    assert result.recommendation == Recommendation.SHORTLIST
    assert result.recommendation == "Shortlist"

    assert result.criterion_scores["skills_match"].weight == 0.40
    assert result.criterion_scores["skills_match"].weighted_score == 34.0
    assert result.criterion_scores["experience_relevance"].weight == 0.30
    assert result.criterion_scores["experience_relevance"].weighted_score == 24.0
    assert result.criterion_scores["education_fit"].weight == 0.15
    assert result.criterion_scores["education_fit"].weighted_score == 9.0
    assert result.criterion_scores["overall_alignment"].weight == 0.15
    assert result.criterion_scores["overall_alignment"].weighted_score == 12.0

    # Verify Groq SDK call arguments include strict Structured Output JSON Schema
    mock_client.chat.completions.create.assert_called_once()
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "openai/gpt-oss-120b"
    assert call_kwargs["temperature"] == 0.0
    resp_fmt = call_kwargs["response_format"]
    assert resp_fmt["type"] == "json_schema"
    assert resp_fmt["json_schema"]["strict"] is True
    assert resp_fmt["json_schema"]["schema"]["additionalProperties"] is False

    serialized = json.loads(result.to_json())
    assert serialized["total_score"] == 79.0
    assert serialized["recommendation"] == "Shortlist"
    assert "criterion_scores" in serialized
    assert "explanation" in serialized


def test_structured_output_json_schema_strict_requirements() -> None:
    """Verify Groq Structured Outputs schema sets strict=True, required fields, and additionalProperties=False."""
    weights = {
        "skills_match": 0.40,
        "experience_relevance": 0.30,
        "education_fit": 0.15,
        "overall_alignment": 0.15,
    }
    schema_wrapper = build_response_json_schema(weights)
    assert schema_wrapper["type"] == "json_schema"
    js = schema_wrapper["json_schema"]
    assert js["strict"] is True
    root = js["schema"]
    assert root["additionalProperties"] is False
    assert set(root["required"]) == {"criteria", "explanation"}

    criteria_schema = root["properties"]["criteria"]
    assert criteria_schema["additionalProperties"] is False
    assert set(criteria_schema["required"]) == set(weights.keys())
    for key in weights:
        prop = criteria_schema["properties"][key]
        assert prop["additionalProperties"] is False
        assert set(prop["required"]) == {"score", "evidence", "evidence_status"}


@pytest.mark.parametrize(
    "uniform_score,expected_recommendation",
    [
        (85.0, Recommendation.SHORTLIST),
        (75.0, Recommendation.SHORTLIST),
        (74.9, Recommendation.MAYBE),
        (60.0, Recommendation.MAYBE),
        (50.0, Recommendation.MAYBE),
        (49.9, Recommendation.REJECT),
        (20.0, Recommendation.REJECT),
    ],
)
def test_recommendation_thresholds_from_config(
    uniform_score: float, expected_recommendation: Recommendation
) -> None:
    """Verify Shortlist / Maybe / Reject thresholds from config.yaml are applied accurately."""
    settings = load_settings()
    assert determine_recommendation(uniform_score, settings.thresholds) == expected_recommendation

    payload = {
        "criteria": {
            name: {
                "score": uniform_score,
                "evidence": "Documented in CV.",
                "evidence_status": "supported",
            }
            for name in ("skills_match", "experience_relevance", "education_fit", "overall_alignment")
        },
        "explanation": "Threshold boundary evaluation.",
    }
    engine = ScoringEngine(settings=settings, client=_make_mock_client(payload))
    result = engine.score_cv("Sample CV text with documented experience.")

    assert result.status == ScoringStatus.SUCCESS
    assert result.total_score == round(uniform_score, 2)
    assert result.recommendation == expected_recommendation


def test_insufficient_evidence_explicitly_marked() -> None:
    """Verify unavailable evidence is explicitly marked insufficient rather than invented."""
    settings = load_settings()
    payload = {
        "criteria": {
            "skills_match": {
                "score": 70,
                "evidence": "Lists Python and Git in skills section.",
                "evidence_status": "supported",
            },
            "experience_relevance": {
                "score": 50,
                "evidence": "N/A",
                "evidence_status": "supported",
            },
            "education_fit": {
                "score": 0,
                "evidence": "No degree or university section present in CV.",
                "evidence_status": "insufficient",
            },
            "overall_alignment": {
                "score": 45,
                "evidence": "",
                "evidence_status": "insufficient",
            },
        },
        "explanation": "Candidate lists Python and Git, but experience duration and education are not stated in CV.",
    }

    result = ScoringEngine(settings=settings, client=_make_mock_client(payload)).score_cv(
        "Skills: Python, Git"
    )

    assert result.status == ScoringStatus.SUCCESS
    assert result.criterion_scores["skills_match"].evidence_status == EvidenceStatus.SUPPORTED

    exp = result.criterion_scores["experience_relevance"]
    assert exp.evidence_status == EvidenceStatus.INSUFFICIENT
    assert exp.evidence == INSUFFICIENT_EVIDENCE_TEXT

    edu = result.criterion_scores["education_fit"]
    assert edu.evidence_status == EvidenceStatus.INSUFFICIENT
    assert edu.evidence.startswith(INSUFFICIENT_EVIDENCE_TEXT)

    align = result.criterion_scores["overall_alignment"]
    assert align.evidence_status == EvidenceStatus.INSUFFICIENT
    assert align.evidence == INSUFFICIENT_EVIDENCE_TEXT


def test_empty_or_whitespace_cv_text_handled_without_api_call() -> None:
    """Empty CV text must never call Groq and must return FAILED with total_score=None."""
    settings = load_settings()
    mock_client = _make_mock_client({})
    engine = ScoringEngine(settings=settings, client=mock_client)

    for empty_input in ("", "   \n\t  ", None):
        result = engine.score_cv(empty_input)
        assert result.status == ScoringStatus.FAILED
        assert result.total_score is None
        assert result.recommendation is None
        assert result.criterion_scores == {}
        assert result.error_message == "Empty CV text provided."

    mock_client.chat.completions.create.assert_not_called()


def test_invalid_json_and_schema_errors_handled_without_silent_zero() -> None:
    """Invalid JSON, out-of-range scores, or missing criteria return FAILED with total_score=None."""
    settings = load_settings()

    # 1. Invalid JSON
    res_bad = ScoringEngine(
        settings=settings, client=_make_mock_client("not valid json {{{")
    ).score_cv("Valid CV text")
    assert res_bad.status == ScoringStatus.FAILED
    assert res_bad.total_score is None
    assert res_bad.recommendation is None
    assert res_bad.criterion_scores == {}
    assert "Malformed JSON" in (res_bad.error_message or "")

    # 2. Out-of-bounds score (> 100)
    oob_payload = {
        "criteria": {
            "skills_match": {"score": 140, "evidence": "Python", "evidence_status": "supported"},
            "experience_relevance": {"score": 50, "evidence": "2 yrs", "evidence_status": "supported"},
            "education_fit": {"score": 50, "evidence": "BSc", "evidence_status": "supported"},
            "overall_alignment": {"score": 50, "evidence": "Fit", "evidence_status": "supported"},
        },
        "explanation": "Invalid score test.",
    }
    res_oob = ScoringEngine(settings=settings, client=_make_mock_client(oob_payload)).score_cv(
        "Valid CV text"
    )
    assert res_oob.status == ScoringStatus.FAILED
    assert res_oob.total_score is None
    assert res_oob.recommendation is None
    assert "Invalid Groq evaluation payload" in (res_oob.error_message or "")

    # 3. Missing required criterion ('education_fit')
    missing_payload = {
        "criteria": {
            "skills_match": {"score": 80, "evidence": "Python", "evidence_status": "supported"},
            "experience_relevance": {"score": 80, "evidence": "2 yrs", "evidence_status": "supported"},
            "overall_alignment": {"score": 80, "evidence": "Fit", "evidence_status": "supported"},
        },
        "explanation": "Missing education_fit criterion.",
    }
    res_missing = ScoringEngine(
        settings=settings, client=_make_mock_client(missing_payload)
    ).score_cv("Valid CV text")
    assert res_missing.status == ScoringStatus.FAILED
    assert res_missing.total_score is None
    assert "education_fit" in (res_missing.error_message or "")


def test_authentication_failure_does_not_retry_and_redacts_secret() -> None:
    """HTTP 401 AuthenticationError fails immediately without retry and scrubs API keys."""
    settings = load_settings()
    secret_key = "gsk_SecretGroqAuthKey1234567890"
    auth_err = groq.AuthenticationError(
        message=f"Invalid API key provided: {secret_key}",
        response=_make_http_response(401, "Invalid API key"),
        body={"error": {"message": "Invalid API key"}},
    )

    sleep_calls: list[float] = []
    client = MagicMock()
    client.chat.completions.create.side_effect = auth_err

    engine = ScoringEngine(
        settings=settings,
        api_key=secret_key,
        client=client,
        max_retries=4,
        sleep_fn=sleep_calls.append,
    )
    result = engine.score_cv("Candidate CV text")

    assert result.status == ScoringStatus.FAILED
    assert result.total_score is None
    assert result.recommendation is None
    assert result.criterion_scores == {}
    assert sleep_calls == []
    assert client.chat.completions.create.call_count == 1
    assert secret_key not in (result.error_message or "")
    assert "[REDACTED]" in (result.error_message or "")


def test_rate_limit_429_retries_and_exhaustion() -> None:
    """HTTP 429 RateLimitError retries with exponential backoff and fails without silent zero."""
    settings = load_settings()
    valid_json = json.dumps(
        {
            "criteria": {
                name: {"score": 80, "evidence": "Cited from CV", "evidence_status": "supported"}
                for name in ("skills_match", "experience_relevance", "education_fit", "overall_alignment")
            },
            "explanation": "Recovered after rate limit.",
        }
    )
    err_429 = groq.RateLimitError(
        message="Rate limit reached",
        response=_make_http_response(429, "Too many requests"),
        body={"error": {"message": "Too many requests"}},
    )

    # 1. Transient 429 recovers on retry
    sleeps: list[float] = []
    transient_client = MagicMock()
    transient_client.chat.completions.create.side_effect = [err_429, _make_completion(valid_json)]
    res_ok = ScoringEngine(
        settings=settings,
        client=transient_client,
        max_retries=4,
        retry_delay=0.25,
        sleep_fn=sleeps.append,
    ).score_cv("CV content")
    assert res_ok.status == ScoringStatus.SUCCESS
    assert res_ok.total_score == 80.0
    assert sleeps == [0.25]
    assert transient_client.chat.completions.create.call_count == 2

    # 2. Persistent 429 exhausts retries cleanly
    persistent_client = MagicMock()
    persistent_client.chat.completions.create.side_effect = err_429
    res_fail = ScoringEngine(
        settings=settings,
        client=persistent_client,
        max_retries=2,
        retry_delay=0.1,
        sleep_fn=lambda _: None,
    ).score_cv("CV content")
    assert res_fail.status == ScoringStatus.FAILED
    assert res_fail.total_score is None
    assert res_fail.recommendation is None
    assert "429" in (res_fail.error_message or "")
    assert persistent_client.chat.completions.create.call_count == 3


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_transient_server_errors_500_502_503_retry_and_exhaustion(status_code: int) -> None:
    """HTTP 500/502/503 trigger bounded exponential backoff (max 4 retries) without silent zero."""
    settings = load_settings()
    server_err = groq.InternalServerError(
        message=f"Server error HTTP {status_code}",
        response=_make_http_response(status_code, f"HTTP {status_code}"),
        body={"error": {"message": f"HTTP {status_code}"}},
    )

    # Cap at 4 retries even if caller requests 10
    sleeps: list[float] = []
    client = MagicMock()
    client.chat.completions.create.side_effect = server_err

    engine = ScoringEngine(
        settings=settings,
        client=client,
        max_retries=10,
        retry_delay=0.2,
        backoff_factor=2.0,
        sleep_fn=sleeps.append,
    )
    assert engine.max_retries == 4

    result = engine.score_cv("Candidate CV text")
    assert result.status == ScoringStatus.FAILED
    assert result.total_score is None
    assert result.recommendation is None
    assert result.criterion_scores == {}
    assert str(status_code) in (result.error_message or "")
    assert client.chat.completions.create.call_count == 5  # 1 initial + 4 retries
    assert sleeps == [0.2, 0.4, 0.8, 1.6]


def test_http_503_recovery_with_explicit_retry_delays() -> None:
    """HTTP 503 recovers after retries using configurable explicit retry_delays."""
    settings = load_settings()
    valid_json = json.dumps(
        {
            "criteria": {
                name: {"score": 82, "evidence": "Cited from CV", "evidence_status": "supported"}
                for name in ("skills_match", "experience_relevance", "education_fit", "overall_alignment")
            },
            "explanation": "Recovered after HTTP 503.",
        }
    )
    err_503 = groq.InternalServerError(
        message="503 Service Unavailable",
        response=_make_http_response(503, "Service Unavailable"),
        body={"error": {"message": "Service Unavailable"}},
    )

    sleeps: list[float] = []
    client = MagicMock()
    client.chat.completions.create.side_effect = [err_503, err_503, _make_completion(valid_json)]

    result = ScoringEngine(
        settings=settings,
        client=client,
        max_retries=4,
        retry_delays=[0.1, 0.3, 0.7, 1.5],
        sleep_fn=sleeps.append,
    ).score_cv("Candidate CV")

    assert result.status == ScoringStatus.SUCCESS
    assert result.total_score == 82.0
    assert result.recommendation == Recommendation.SHORTLIST
    assert sleeps == [0.1, 0.3]
    assert client.chat.completions.create.call_count == 3


def test_network_timeout_retries_and_exhaustion() -> None:
    """Network timeouts (APITimeoutError) trigger bounded backoff and fail without silent zero."""
    settings = load_settings()
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    timeout_err = groq.APITimeoutError(request=req)

    sleeps: list[float] = []
    client = MagicMock()
    client.chat.completions.create.side_effect = timeout_err

    result = ScoringEngine(
        settings=settings,
        client=client,
        max_retries=2,
        retry_delay=0.5,
        sleep_fn=sleeps.append,
    ).score_cv("Candidate CV text")

    assert result.status == ScoringStatus.FAILED
    assert result.total_score is None
    assert result.recommendation is None
    assert result.criterion_scores == {}
    assert "timeout" in (result.error_message or "").lower()
    assert sleeps == [0.5, 1.0]
    assert client.chat.completions.create.call_count == 3


def test_bad_request_400_does_not_retry() -> None:
    """Invalid request errors (HTTP 400) fail immediately without retrying."""
    settings = load_settings()
    bad_req = groq.BadRequestError(
        message="Invalid schema parameter",
        response=_make_http_response(400, "Bad request"),
        body={"error": {"message": "Bad request"}},
    )

    sleeps: list[float] = []
    client = MagicMock()
    client.chat.completions.create.side_effect = bad_req

    result = ScoringEngine(
        settings=settings,
        client=client,
        max_retries=4,
        sleep_fn=sleeps.append,
    ).score_cv("Candidate CV text")

    assert result.status == ScoringStatus.FAILED
    assert result.total_score is None
    assert result.recommendation is None
    assert sleeps == []
    assert client.chat.completions.create.call_count == 1


def test_missing_groq_api_key_handled_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing GROQ_API_KEY returns FAILED with total_score=None and mentions GROQ_API_KEY."""
    settings = load_settings()
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    settings.env.groq_api_key = None

    engine = ScoringEngine(settings=settings, api_key=None, client=None)
    result = engine.score_cv("Candidate CV with Python and SQL.")

    assert result.status == ScoringStatus.FAILED
    assert result.total_score is None
    assert result.recommendation is None
    assert result.criterion_scores == {}
    assert "GROQ_API_KEY" in (result.error_message or "")


def test_prompt_and_system_instruction_enforce_no_hallucination() -> None:
    """Verify prompt and system instructions preserve role requirements, anti-hallucination, and needs_review rules."""
    settings = load_settings()
    sys_inst = build_system_instruction()
    assert "NEVER invent" in sys_inst
    assert "Insufficient evidence in CV" in sys_inst
    assert "needs_review" in sys_inst
    assert "NEVER assume that an unusual, translated, or non-standard degree" in sys_inst
    assert "Do NOT penalize a candidate solely because a qualification title is unusual" in sys_inst

    prompt = build_evaluation_prompt(
        cv_text="Sample Candidate CV Text",
        role_requirements=settings.role_requirements,
        weights={
            "skills_match": 0.4,
            "experience_relevance": 0.3,
            "education_fit": 0.15,
            "overall_alignment": 0.15,
        },
    )
    assert settings.role_requirements.title in prompt
    assert "Sample Candidate CV Text" in prompt
    assert "skills_match" in prompt
    assert "needs_review" in prompt

    schema = build_response_json_schema(
        {"skills_match": 0.4, "experience_relevance": 0.3, "education_fit": 0.15, "overall_alignment": 0.15}
    )
    enum_vals = schema["json_schema"]["schema"]["properties"]["criteria"]["properties"]["education_fit"]["properties"]["evidence_status"]["enum"]
    assert set(enum_vals) == {"supported", "insufficient", "needs_review"}


def test_education_clearly_relevant_degree() -> None:
    """Clearly relevant CS degree yields supported status without manual review flag."""
    settings = load_settings()
    payload = {
        "criteria": {
            "skills_match": {"score": 85, "evidence": "Python, SQL, Git", "evidence_status": "supported"},
            "experience_relevance": {"score": 80, "evidence": "2 years Junior Software Developer", "evidence_status": "supported"},
            "education_fit": {
                "score": 95,
                "evidence": "B.Sc. in Computer Science, State University (2022)",
                "evidence_status": "supported",
            },
            "overall_alignment": {"score": 85, "evidence": "Meets skills, experience, and degree criteria", "evidence_status": "supported"},
        },
        "explanation": "Strong fit with a directly relevant Computer Science degree.",
    }

    result = ScoringEngine(settings=settings, client=_make_mock_client(payload)).score_cv(
        "Education: B.Sc. in Computer Science, State University (2022)"
    )

    assert result.status == ScoringStatus.SUCCESS
    edu = result.criterion_scores["education_fit"]
    assert edu.evidence_status == EvidenceStatus.SUPPORTED
    assert edu.evidence == "B.Sc. in Computer Science, State University (2022)"
    assert edu.score == 95.0
    assert result.needs_manual_review is False
    assert result.review_criteria == []
    # Deterministic Python weighted total: 85*0.4 + 80*0.3 + 95*0.15 + 85*0.15 = 34 + 24 + 14.25 + 12.75 = 85.0
    assert result.total_score == 85.0
    assert result.recommendation == Recommendation.SHORTLIST


def test_education_clearly_unrelated_degree() -> None:
    """Clearly unrelated degree is marked supported with its explicit evidence and no manual review."""
    settings = load_settings()
    payload = {
        "criteria": {
            "skills_match": {"score": 80, "evidence": "Python, SQL, Git", "evidence_status": "supported"},
            "experience_relevance": {"score": 75, "evidence": "2 years Software Developer", "evidence_status": "supported"},
            "education_fit": {
                "score": 30,
                "evidence": "B.A. in Culinary Arts, City College (2020)",
                "evidence_status": "supported",
            },
            "overall_alignment": {"score": 70, "evidence": "Strong coding skills despite unrelated degree", "evidence_status": "supported"},
        },
        "explanation": "Degree is in Culinary Arts (unrelated), while skills and experience align.",
    }

    result = ScoringEngine(settings=settings, client=_make_mock_client(payload)).score_cv(
        "Education: B.A. in Culinary Arts, City College (2020)"
    )

    assert result.status == ScoringStatus.SUCCESS
    edu = result.criterion_scores["education_fit"]
    assert edu.evidence_status == EvidenceStatus.SUPPORTED
    assert edu.evidence == "B.A. in Culinary Arts, City College (2020)"
    assert edu.score == 30.0
    assert result.needs_manual_review is False
    assert result.review_criteria == []
    # 80*0.4 + 75*0.3 + 30*0.15 + 70*0.15 = 32 + 22.5 + 4.5 + 10.5 = 69.5
    assert result.total_score == 69.5
    assert result.recommendation == Recommendation.MAYBE


def test_education_ambiguous_degree_title_flagged_needs_review(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ambiguous/unusual degree title preserves exact CV quote, flags needs_review, and computes score deterministically."""
    import logging

    settings = load_settings()
    exact_quote = (
        "Master's degree in Computer reference legal systems, "
        "Omsk Government Academy, Russia."
    )
    payload = {
        "criteria": {
            "skills_match": {"score": 80, "evidence": "Python, SQL, Git", "evidence_status": "supported"},
            "experience_relevance": {"score": 80, "evidence": "3 years Software Engineer", "evidence_status": "supported"},
            "education_fit": {
                "score": 65,
                "evidence": exact_quote,
                "evidence_status": "needs_review",
            },
            "overall_alignment": {"score": 80, "evidence": "Meets experience and technical skills", "evidence_status": "supported"},
        },
        "explanation": "Strong technical profile; degree title requires manual human verification.",
    }

    caplog.set_level(logging.INFO)
    result = ScoringEngine(settings=settings, client=_make_mock_client(payload)).score_cv(
        f"Education: {exact_quote}\nSkills: Python, SQL, Git"
    )

    assert result.status == ScoringStatus.SUCCESS
    edu = result.criterion_scores["education_fit"]
    assert edu.evidence_status == EvidenceStatus.NEEDS_REVIEW
    assert edu.evidence_status == "needs_review"
    assert edu.evidence == exact_quote
    assert result.needs_manual_review is True
    assert result.review_criteria == ["education_fit"]
    assert "education_fit" in caplog.text

    # Deterministic Python weighted calculation preserved:
    # 80*0.40 (32.0) + 80*0.30 (24.0) + 65*0.15 (9.75) + 80*0.15 (12.0) = 77.75
    assert result.total_score == 77.75
    assert result.recommendation == Recommendation.SHORTLIST


def test_education_missing_distinguished_from_ambiguous() -> None:
    """Missing education info is marked insufficient (not needs_review), even if needs_review lacks evidence."""
    settings = load_settings()

    # Case 1: Model correctly returns insufficient with empty/missing evidence
    missing_payload = {
        "criteria": {
            "skills_match": {"score": 80, "evidence": "Python, SQL, Git", "evidence_status": "supported"},
            "experience_relevance": {"score": 80, "evidence": "2 years Developer", "evidence_status": "supported"},
            "education_fit": {
                "score": 50,
                "evidence": "",
                "evidence_status": "insufficient",
            },
            "overall_alignment": {"score": 75, "evidence": "Meets core skills and experience", "evidence_status": "supported"},
        },
        "explanation": "No education section found in CV.",
    }
    res_missing = ScoringEngine(
        settings=settings, client=_make_mock_client(missing_payload)
    ).score_cv("Skills: Python, SQL, Git")

    assert res_missing.status == ScoringStatus.SUCCESS
    edu_missing = res_missing.criterion_scores["education_fit"]
    assert edu_missing.evidence_status == EvidenceStatus.INSUFFICIENT
    assert edu_missing.evidence == INSUFFICIENT_EVIDENCE_TEXT
    assert res_missing.needs_manual_review is False
    assert res_missing.review_criteria == []

    # Case 2: Defensive guard — if model marks needs_review with placeholder/empty text, normalize to insufficient
    bogus_review_payload = {
        "criteria": {
            "skills_match": {"score": 80, "evidence": "Python", "evidence_status": "supported"},
            "experience_relevance": {"score": 80, "evidence": "2 years", "evidence_status": "supported"},
            "education_fit": {
                "score": 50,
                "evidence": "Not mentioned",
                "evidence_status": "needs_review",
            },
            "overall_alignment": {"score": 75, "evidence": "Fit", "evidence_status": "supported"},
        },
        "explanation": "Education absent from CV.",
    }
    res_coerced = ScoringEngine(
        settings=settings, client=_make_mock_client(bogus_review_payload)
    ).score_cv("Skills: Python")

    assert res_coerced.criterion_scores["education_fit"].evidence_status == EvidenceStatus.INSUFFICIENT
    assert res_coerced.needs_manual_review is False

