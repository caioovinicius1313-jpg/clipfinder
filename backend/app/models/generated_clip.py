import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.reference_video import ReferenceVideo
    from app.models.source_video import SourceVideo


class GeneratedClip(Base):
    __tablename__ = "generated_clips"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_videos.id", ondelete="CASCADE"), nullable=False
    )
    reference_video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reference_videos.id", ondelete="CASCADE"), nullable=False
    )
    original_start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="processing")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="generated_clips")
    source_video: Mapped["SourceVideo"] = relationship(back_populates="generated_clips")
    reference_video: Mapped["ReferenceVideo"] = relationship(back_populates="generated_clips")
