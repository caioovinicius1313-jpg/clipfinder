import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.reference_video import ReferenceVideo


class ReferenceVideoRepository:
    def get_by_id(
        self,
        database: Session,
        project_id: uuid.UUID,
        reference_video_id: uuid.UUID,
    ) -> ReferenceVideo | None:
        statement = (
            select(ReferenceVideo)
            .options(selectinload(ReferenceVideo.generated_clips))
            .where(
                ReferenceVideo.id == reference_video_id,
                ReferenceVideo.project_id == project_id,
            )
        )
        return database.scalar(statement)

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

    def delete(self, database: Session, reference_video: ReferenceVideo) -> None:
        for generated_clip in list(reference_video.generated_clips):
            database.delete(generated_clip)
        database.delete(reference_video)
        database.commit()

    def set_status(
        self, database: Session, reference_video: ReferenceVideo, status: str
    ) -> ReferenceVideo:
        reference_video.status = status
        database.add(reference_video)
        database.commit()
        database.refresh(reference_video)
        return reference_video

    def attach_downloaded_file(
        self,
        database: Session,
        reference_video: ReferenceVideo,
        *,
        filename: str,
        path: str,
        size: int,
        duration: float | None,
    ) -> ReferenceVideo:
        reference_video.filename = filename
        reference_video.path = path
        reference_video.size = size
        reference_video.duration = duration
        reference_video.status = "uploaded"
        database.add(reference_video)
        database.commit()
        database.refresh(reference_video)
        return reference_video
