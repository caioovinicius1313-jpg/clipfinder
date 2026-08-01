import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class GeneratedClipRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_video_id: uuid.UUID
    reference_video_id: uuid.UUID
    original_start_seconds: float | None
    original_end_seconds: float | None
    match_score: float | None
    path: str | None
    status: str
    error_message: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MatchGenerationResponse(BaseModel):
    message: str
    jobs_started: int
