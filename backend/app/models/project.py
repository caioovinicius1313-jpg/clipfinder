import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.generated_clip import GeneratedClip
    from app.models.hashtag_video_daily_stat import HashtagVideoDailyStat
    from app.models.reference_video import ReferenceVideo
    from app.models.source_video import SourceVideo


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source_videos: Mapped[list["SourceVideo"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    reference_videos: Mapped[list["ReferenceVideo"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    generated_clips: Mapped[list["GeneratedClip"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    hashtag_video_daily_stats: Mapped[list["HashtagVideoDailyStat"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
