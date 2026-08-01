import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ReferenceVideoRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source: str
    title: str
    filename: str | None
    path: str | None
    size: int | None
    duration: float | None
    external_id: str | None
    external_url: str | None
    thumbnail_url: str | None
    channel_title: str | None
    description: str | None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class YoutubeReferenceCreate(BaseModel):
    video_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    channel_title: str | None = Field(default=None, max_length=255)
    thumbnail_url: HttpUrl | None = None
    description: str | None = None
