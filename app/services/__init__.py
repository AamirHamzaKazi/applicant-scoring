"""Processing services for CV extraction, deduplication, and AI scoring."""

from app.services.cv_parser import (
    CVParseResult,
    CVParser,
    ExtractionStatus,
    parse_cv,
    parse_cv_batch,
)
from app.services.duplicate_detector import (
    DuplicateCheckResult,
    DuplicateDetector,
    DuplicateReason,
    compute_bytes_sha256,
    compute_file_sha256,
)
from app.services.pipeline import (
    ApplicantInput,
    ApplicantPipeline,
    DownloadResult,
    GoogleDriveDownloader,
    PipelineResult,
    PipelineStatus,
)
from app.services.scoring_engine import (
    CriterionEvaluation,
    CriterionScore,
    EvaluationPayload,
    EvidenceStatus,
    Recommendation,
    ScoringEngine,
    ScoringResult,
    ScoringStatus,
    build_response_json_schema,
    calculate_weighted_scores,
    determine_recommendation,
    score_cv,
)

__all__ = [
    "ApplicantInput",
    "ApplicantPipeline",
    "CVParseResult",
    "CVParser",
    "CriterionEvaluation",
    "CriterionScore",
    "DownloadResult",
    "DuplicateCheckResult",
    "DuplicateDetector",
    "DuplicateReason",
    "EvaluationPayload",
    "EvidenceStatus",
    "ExtractionStatus",
    "GoogleDriveDownloader",
    "PipelineResult",
    "PipelineStatus",
    "Recommendation",
    "ScoringEngine",
    "ScoringResult",
    "ScoringStatus",
    "build_response_json_schema",
    "calculate_weighted_scores",
    "compute_bytes_sha256",
    "compute_file_sha256",
    "determine_recommendation",
    "parse_cv",
    "parse_cv_batch",
    "score_cv",
]
