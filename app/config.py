"""Application configuration loader combining config.yaml and environment variables."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class AppConfig(BaseModel):
    name: str = "Daily Applicant Scoring Pipeline"
    version: str = "0.1.0"


class EducationRequirementConfig(BaseModel):
    preferred_degree: str = ""
    required: bool = False


class RoleRequirementsConfig(BaseModel):
    vacancy_id: int
    title: str
    min_years_experience: int = 0
    max_years_experience: int | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    education: EducationRequirementConfig = Field(
        default_factory=EducationRequirementConfig
    )


class ScoringWeightsConfig(BaseModel):
    skills_match: float = 0.40
    experience_relevance: float = 0.30
    education_fit: float = 0.15
    overall_alignment: float = 0.15


class ThresholdsConfig(BaseModel):
    shortlist: float = 75.0
    review: float = 50.0
    reject: float = 49.9
    near_duplicate_similarity: float = 0.80


DEFAULT_CODA_COLUMN_MAPPING: dict[str, str] = {
    "applicant_id": "Applicant ID",
    "name": "Name",
    "email": "Email",
    "role": "Role",
    "cv_link": "CV Link",
    "total_score": "Total Score",
    "sub_scores": "Sub-scores",
    "recommendation": "Recommendation",
    "reason": "Reason",
    "date_processed": "Date Processed",
    "status": "Status",
    "error_note": "Error Note",
}


class CodaSettingsConfig(BaseModel):
    base_url: str = "https://coda.io/apis/v1"
    table_name: str = "Applicants"
    doc_id: str | None = None
    table_id: str | None = None
    timeout_seconds: float = 15.0
    upsert_key: str = "applicant_id"
    column_mapping: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_CODA_COLUMN_MAPPING)
    )


class GroqConfig(BaseModel):
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    base_url: str = "https://api.groq.com/openai/v1"
    timeout_seconds: float = 30.0


class GoogleSheetsColumnsConfig(BaseModel):
    name: str = "Name"
    email: str = "Email"
    role: str = "Role"
    cv_link: str = "CV_Link"


class GoogleSheetsConfig(BaseModel):
    spreadsheet_id: str = ""
    worksheet_name: str = "Sheet1"
    columns: GoogleSheetsColumnsConfig = Field(
        default_factory=GoogleSheetsColumnsConfig
    )


class EnvironmentConfig(BaseModel):
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    config_path: Path = DEFAULT_CONFIG_PATH

    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    coda_api_token: SecretStr | None = None
    coda_doc_id: str | None = None
    coda_table_id: str | None = None

    google_sheets_id: str | None = None
    google_worksheet_name: str | None = None
    google_drive_folder_id: str | None = None
    google_application_credentials: str | None = "secrets/google-service-account.json"

    webhook_auth_token: SecretStr | None = None


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    groq: GroqConfig = Field(default_factory=GroqConfig)
    role_requirements: RoleRequirementsConfig
    scoring_weights: ScoringWeightsConfig = Field(default_factory=ScoringWeightsConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    coda: CodaSettingsConfig = Field(default_factory=CodaSettingsConfig)
    google_sheets: GoogleSheetsConfig = Field(default_factory=GoogleSheetsConfig)
    env: EnvironmentConfig = Field(default_factory=EnvironmentConfig)

    def resolve_google_credentials_path(self) -> Path | None:
        """Resolve GOOGLE_APPLICATION_CREDENTIALS to an absolute Path if configured."""
        env_override = _get_optional_env("GOOGLE_APPLICATION_CREDENTIALS")
        raw = env_override or self.env.google_application_credentials
        if not raw or not raw.strip():
            return None
        path = Path(raw.strip())
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        if path.is_file():
            return path
        if raw.strip().replace("\\", "/") == "secrets/google-service-account.json":
            secrets_dir = PROJECT_ROOT / "secrets"
            if secrets_dir.is_dir():
                json_files = sorted(secrets_dir.glob("*.json"))
                if len(json_files) == 1 and json_files[0].is_file():
                    return json_files[0].resolve()
        return path


def _get_optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _get_secret_env(name: str) -> SecretStr | None:
    value = _get_optional_env(name)
    return SecretStr(value) if value is not None else None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load application settings from .env/environment variables and config.yaml."""
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

    resolved_path = Path(
        config_path or _get_optional_env("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    )
    if not resolved_path.is_absolute():
        resolved_path = PROJECT_ROOT / resolved_path

    yaml_data = _load_yaml(resolved_path)

    port_raw = _get_optional_env("APP_PORT")
    app_port = int(port_raw) if port_raw is not None else 8000

    env_config = EnvironmentConfig(
        app_env=_get_optional_env("APP_ENV") or "development",
        app_host=_get_optional_env("APP_HOST") or "127.0.0.1",
        app_port=app_port,
        config_path=resolved_path,
        groq_api_key=_get_secret_env("GROQ_API_KEY"),
        groq_model=_get_optional_env("GROQ_MODEL"),
        coda_api_token=_get_secret_env("CODA_API_TOKEN"),
        coda_doc_id=_get_optional_env("CODA_DOC_ID"),
        coda_table_id=_get_optional_env("CODA_TABLE_ID"),
        google_sheets_id=(
            _get_optional_env("GOOGLE_SHEETS_ID")
            or _get_optional_env("GOOGLE_SHEET_ID")
        ),
        google_worksheet_name=(
            _get_optional_env("GOOGLE_SHEET_TAB")
            or _get_optional_env("GOOGLE_WORKSHEET_NAME")
        ),
        google_drive_folder_id=_get_optional_env("GOOGLE_DRIVE_FOLDER_ID"),
        google_application_credentials=(
            _get_optional_env("GOOGLE_APPLICATION_CREDENTIALS")
            or "secrets/google-service-account.json"
        ),
        webhook_auth_token=_get_secret_env("WEBHOOK_AUTH_TOKEN"),
    )

    return Settings(
        app=AppConfig.model_validate(yaml_data.get("app", {})),
        groq=GroqConfig.model_validate(yaml_data.get("groq", {})),
        role_requirements=RoleRequirementsConfig.model_validate(
            yaml_data["role_requirements"]
        ),
        scoring_weights=ScoringWeightsConfig.model_validate(
            yaml_data.get("scoring_weights", {})
        ),
        thresholds=ThresholdsConfig.model_validate(yaml_data.get("thresholds", {})),
        coda=CodaSettingsConfig.model_validate(yaml_data.get("coda", {})),
        google_sheets=GoogleSheetsConfig.model_validate(
            yaml_data.get("google_sheets", {})
        ),
        env=env_config,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return load_settings()
