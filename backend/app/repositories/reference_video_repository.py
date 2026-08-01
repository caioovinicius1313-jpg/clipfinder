import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reference_video import ReferenceVideo


class ReferenceVideoRepository:
    def list_uploaded(self, database: Session, project_id: uuid.UUID) -> list[ReferenceVideo]:
        statement = (
            select(ReferenceVideo)
            .where(ReferenceVideo.project_id == project_id, ReferenceVideo.path.is_not(None))
            .order_by(ReferenceVideo.created_at.asc())
        )
        return list(database.scalars(statement))

    def get_by_youtube_id(
        self, database: Session, project_id: uuid.UUID, youtube_video_id: str
    ) -> ReferenceVideo | None:
        statement = select(ReferenceVideo).where(
            ReferenceVideo.project_id == project_id,
            ReferenceVideo.source == "youtube",
            ReferenceVideo.external_id == youtube_video_id,
        )
        return database.scalar(statement)

    def create_uploaded(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        title: str,
        filename: str,
        path: str,
        size: int,
        duration: float | None,
        status: str,
    ) -> ReferenceVideo:
        reference_video = ReferenceVideo(
            project_id=project_id,
            source="upload",
            title=title,
            filename=filename,
            path=path,
            size=size,
            duration=duration,
            status=status,
        )
        database.add(reference_video)
        database.commit()
        database.refresh(reference_video)
        return reference_video

    def create_youtube(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        external_id: str,
        external_url: str,
        title: str,
        channel_title: str | None,
        thumbnail_url: str | None,
        description: str | None,
    ) -> ReferenceVideo:
        reference_video = ReferenceVideo(
            project_id=project_id,
            source="youtube",
            external_id=external_id,
            external_url=external_url,
            title=title,
            channel_title=channel_title,
            thumbnail_url=thumbnail_url,
            description=description,
            status="reference_only",
        )
        database.add(reference_video)
        database.commit()
        database.refresh(reference_video)
        return reference_video
