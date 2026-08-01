from datetime import datetime

from pydantic import BaseModel, HttpUrl


class YoutubeSearchResult(BaseModel):
    video_id: str
    title: str
    description: str
    channel_title: str
    url: HttpUrl
    thumbnail_url: HttpUrl | None
    published_at: datetime | None
