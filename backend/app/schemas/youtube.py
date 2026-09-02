import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class YoutubeSearchResult(BaseModel):
    video_id: str
    title: str
    description: str
    channel_title: str
    url: HttpUrl
    thumbnail_url: HttpUrl | None
    published_at: datetime | None


class YoutubeVideoDetails(BaseModel):
    video_id: str
    title: str
    channel_title: str
    thumbnail_url: str | None
    published_at: datetime | None
    views: int = Field(ge=0)


class YoutubeRankingMetric(str, Enum):
    views = "views"
    view_delta = "view_delta"


class HashtagVideoDailyStatRead(BaseModel):
    id: uuid.UUID
    video_id: str
    title: str
    channel_title: str
    thumbnail_url: str | None
    published_at: datetime | None
    views: int
    view_delta: int | None
    snapshot_date: date
    youtube_url: str

    model_config = ConfigDict(from_attributes=True)


class HashtagRankingRead(BaseModel):
    hashtag: str | None
    snapshot_date: date | None
    metric: YoutubeRankingMetric
    videos: list[HashtagVideoDailyStatRead] = Field(default_factory=list)
