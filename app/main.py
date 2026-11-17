"""FastAPI entry point for the Daily Applicant Scoring Pipeline."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.integrations.coda_client import CodaApplicantStatus
from app.services.duplicate_detector import DuplicateReason
from app.services.pipeline import (
    ApplicantInput,
    ApplicantPipeline,
    PipelineResult,
    PipelineStatus,
)
from app.services.scoring_engine import Recommendation

logger = logging.getLogger(__name__)


class ApplicantIngestRequest(ApplicantInput):
    """Payload received from Google Apps Script or API clients."""


class ApplicantIngestResponse(BaseModel):
    """Structured HTTP response for POST /api/v1/applicants."""

    applicant_id: str
    status: PipelineStatus
    email: str
    duplicate_reason: DuplicateReason | None = None
    total_score: float | None = None
    recommendation: Recommendation | None = None
    coda_status: CodaApplicantStatus | None = None
    coda_inserted: bool = False
    coda_row_ids: list[str] = Field(default_factory=list)
    message: str
    error_message: str | None = None


def _resolve_webhook_token(settings: Settings) -> str | None:
    env_token = os.getenv("WEBHOOK_AUTH_TOKEN")
    if env_token is not None:
        stripped = env_token.strip()
        if stripped:
            return stripped
    if settings.env.webhook_auth_token is not None:
        secret = settings.env.webhook_auth_token.get_secret_value().strip()
        if secret:
            return secret
    return None


def _extract_provided_token(
    authorization: str | None,
    x_webhook_token: str | None,
) -> str | None:
    if x_webhook_token and x_webhook_token.strip():
        return x_webhook_token.strip()
    if authorization and authorization.strip():
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
    return None


def verify_webhook_auth(
    authorization: str | None = Header(default=None),
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce optional bearer/webhook token auth when WEBHOOK_AUTH_TOKEN is configured."""
    expected_token = _resolve_webhook_token(settings)
    if not expected_token:
        return

    provided_token = _extract_provided_token(authorization, x_webhook_token)
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook authentication token.",
        )


def get_applicant_pipeline(
    settings: Settings = Depends(get_settings),
) -> ApplicantPipeline:
    """FastAPI dependency returning an ApplicantPipeline instance."""
    return ApplicantPipeline(settings=settings)


def _build_ingest_response(result: PipelineResult) -> tuple[int, ApplicantIngestResponse]:
    if result.status == PipelineStatus.SCORED:
        return (
            status.HTTP_201_CREATED,
            ApplicantIngestResponse(
                applicant_id=result.applicant_id,
                status=result.status,
                email=result.email,
                total_score=result.total_score,
                recommendation=result.recommendation,
                coda_status=result.coda_status,
                coda_inserted=result.coda_inserted,
                coda_row_ids=result.coda_row_ids,
                message="Applicant processed, scored, and saved to Coda.",
                error_message=None,
            ),
        )

    if result.status == PipelineStatus.SKIPPED_DUPLICATE:
        reason_str = result.duplicate_reason.value if result.duplicate_reason else "duplicate"
        return (
            status.HTTP_200_OK,
            ApplicantIngestResponse(
                applicant_id=result.applicant_id,
                status=result.status,
                email=result.email,
                duplicate_reason=result.duplicate_reason,
                coda_inserted=False,
                message=f"Applicant skipped ({reason_str}).",
                error_message=None,
            ),
        )

    # Failed status: 200 if cleanly persisted as Failed in Coda; 502 if Coda integration failed
    if result.coda_inserted:
        return (
            status.HTTP_200_OK,
            ApplicantIngestResponse(
                applicant_id=result.applicant_id,
                status=result.status,
                email=result.email,
                coda_status=result.coda_status,
                coda_inserted=True,
                coda_row_ids=result.coda_row_ids,
                message="Applicant processing failed and was recorded as Failed in Coda.",
                error_message=result.error_message,
            ),
        )

    return (
        status.HTTP_502_BAD_GATEWAY,
        ApplicantIngestResponse(
            applicant_id=result.applicant_id,
            status=result.status,
            email=result.email,
            total_score=result.total_score,
            recommendation=result.recommendation,
            coda_status=result.coda_status,
            coda_inserted=False,
            message="Applicant processing failed and could not be persisted to Coda.",
            error_message=result.error_message,
        ),
    )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    application = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description="Backend API for the Daily Applicant Scoring Pipeline.",
    )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled error processing %s %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error while processing request."},
        )

    @application.get("/health", tags=["system"])
    def health_check(current: Settings = Depends(get_settings)) -> dict[str, str]:
        """Return service health status without exposing sensitive configuration."""
        return {
            "status": "ok",
            "service": current.app.name,
            "version": current.app.version,
            "environment": current.env.app_env,
        }

    @application.post(
        "/api/v1/applicants",
        response_model=ApplicantIngestResponse,
        tags=["applicants"],
        dependencies=[Depends(verify_webhook_auth)],
    )
    def ingest_applicant(
        payload: ApplicantIngestRequest,
        pipeline: ApplicantPipeline = Depends(get_applicant_pipeline),
    ) -> JSONResponse:
        """Ingest, deduplicate, download, parse, score, and persist one applicant."""
        result = pipeline.process_applicant(payload)
        http_status, body = _build_ingest_response(result)
        return JSONResponse(
            status_code=http_status,
            content=body.model_dump(mode="json"),
        )

    return application


app = create_app()
