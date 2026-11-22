import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class AutomationSearchRequest(BaseModel):
    source_url: HttpUrl
    hashtag: str = Field(min_length=1, max_length=100)
    sort_by: Literal["relevance", "recent", "views"] = "relevance"
    page_token: str | None = Field(default=None, max_length=512)
    published_after: datetime | None = None

    @field_validator("hashtag")
    @classmethod
    def normalize_hashtag(cls, value: str) -> str:
        cleaned = value.strip().lstrip("#").strip()
        if not cleaned:
            raise ValueError("Informe uma hashtag válida.")
        return cleaned


class AutomationCandidateRead(BaseModel):
    video_id: str
    title: str
    description: str
    channel_title: str
    url: str
    thumbnail_url: str | None
    published_at: datetime | None
    views: int = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    relevance_score: float = Field(ge=0, le=1)


class AutomationSearchResponse(BaseModel):
    source_video_id: str
    hashtag: str
    candidates: list[AutomationCandidateRead]
    next_page_token: str | None = None
    search_window_start: datetime


class AutomationStartRequest(AutomationSearchRequest):
    selected_video_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("selected_video_ids")
    @classmethod
    def validate_unique_video_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("Selecione pelo menos um vídeo.")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("A seleção contém vídeos repetidos.")
        return cleaned


class AutomationRunRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_video_id: uuid.UUID | None
    source_url: str
    hashtag: str
    status: str
    total_references: int
    ready_references: int
    jobs_started: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
