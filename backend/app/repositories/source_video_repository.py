import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.source_video import SourceVideo


class SourceVideoRepository:
    def get_latest_for_project(self, database: Session, project_id: uuid.UUID) -> SourceVideo | None:
        statement = (
            select(SourceVideo)
            .where(SourceVideo.project_id == project_id)
            .order_by(SourceVideo.created_at.desc())
            .limit(1)
        )
        return database.scalar(statement)

    def create(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        filename: str,
        path: str,
        size: int,
        duration: float | None,
        status: str = "uploaded",
    ) -> SourceVideo:
        source_video = SourceVideo(
            project_id=project_id,
            filename=filename,
            path=path,
            size=size,
            duration=duration,
            status=status,
        )
        database.add(source_video)
        database.commit()
        database.refresh(source_video)
        return source_video
