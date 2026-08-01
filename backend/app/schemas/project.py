import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.generated_clip import GeneratedClipRead
from app.schemas.reference_video import ReferenceVideoRead
from app.schemas.source_video import SourceVideoRead


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    hashtag: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned_value = value.strip()
        if not cleaned_value:
            raise ValueError("Name must not be empty")
        return cleaned_value

    @field_validator("description", "hashtag")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class ProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    hashtag: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectDetail(ProjectRead):
    source_videos: list[SourceVideoRead] = Field(default_factory=list)
    reference_videos: list[ReferenceVideoRead] = Field(default_factory=list)
    generated_clips: list[GeneratedClipRead] = Field(default_factory=list)
