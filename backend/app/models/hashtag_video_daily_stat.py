import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.project import Project


class HashtagVideoDailyStat(Base):
    __tablename__ = "hashtag_video_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "video_id",
            "snapshot_date",
            name="uq_hashtag_video_daily_stats_project_video_date",
        ),
        Index(
            "ix_hashtag_video_daily_stats_project_date",
            "project_id",
            "snapshot_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    hashtag: Mapped[str] = mapped_column(String(100), nullable=False)
    video_id: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    channel_title: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False)
    view_delta: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="hashtag_video_daily_stats")

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"
