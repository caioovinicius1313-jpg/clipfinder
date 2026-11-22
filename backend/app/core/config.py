from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ClipFinder API"
    database_url: str = "postgresql+psycopg://clipfinder:clipfinder@postgres:5432/clipfinder"
    storage_path: str = "/app/storage"
    cors_origins: str = "http://localhost:3000"
    youtube_api_key: str | None = None
    youtube_ranking_metric: Literal["views", "view_delta"] = "view_delta"
    youtube_ranking_candidate_limit: int = Field(default=25, ge=5, le=25)
    youtube_ranking_top_limit: int = Field(default=10, ge=1, le=25)
    youtube_ranking_max_hashtags_per_run: int = Field(default=80, ge=1, le=100)
    yt_dlp_timeout_seconds: int = Field(default=60, ge=10, le=180)
    youtube_download_timeout_seconds: int = Field(default=3600, ge=60, le=14_400)
    youtube_cookies_base64: str | None = None
    youtube_cookies_file: str | None = None
    youtube_cookies_from_browser: str | None = None
    youtube_user_agent: str | None = None
    ffmpeg_path: str | None = None
    bucket_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BUCKET_NAME", "BUCKET", "AWS_S3_BUCKET_NAME"),
    )
    bucket_access_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BUCKET_ACCESS_KEY_ID", "ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"
        ),
    )
    bucket_secret_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BUCKET_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"
        ),
    )
    bucket_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BUCKET_ENDPOINT", "ENDPOINT", "AWS_ENDPOINT_URL"),
    )
    bucket_region: str = Field(
        default="auto",
        validation_alias=AliasChoices("BUCKET_REGION", "REGION", "AWS_DEFAULT_REGION"),
    )
    bucket_url_style: str = Field(
        default="virtual",
        validation_alias=AliasChoices("BUCKET_URL_STYLE", "AWS_S3_URL_STYLE"),
    )
    bucket_presigned_expiration: int = 43_200
    max_upload_size_gb: int = 10

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.database_url

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def bucket_enabled(self) -> bool:
        return all(
            (
                self.bucket_name,
                self.bucket_access_key_id,
                self.bucket_secret_access_key,
                self.bucket_endpoint,
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
