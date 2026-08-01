import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SourceVideoRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    path: str
    size: int
    duration: float | None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
