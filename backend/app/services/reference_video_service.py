import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.reference_video import ReferenceVideo
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.schemas.reference_video import YoutubeReferenceCreate
from app.services.media_processing_service import MediaProcessingService
from app.services.project_service import ProjectService


class ReferenceVideoService:
    def __init__(
        self,
        repository: ReferenceVideoRepository | None = None,
        project_service: ProjectService | None = None,
    ) -> None:
        self.repository = repository or ReferenceVideoRepository()
        self.project_service = project_service or ProjectService()

    def upload(
        self, database: Session, *, project_id: uuid.UUID, file: UploadFile
    ) -> ReferenceVideo:
        self.project_service.get_by_id(database, project_id)
        filename = Path(file.filename or "referencia").name
        extension = Path(filename).suffix.lower()
        relative_path = Path("projects") / str(project_id) / "references" / f"{uuid.uuid4()}{extension}"
        absolute_path = Path(settings.storage_path) / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with absolute_path.open("wb") as destination:
                shutil.copyfileobj(file.file, destination)
            duration = MediaProcessingService().probe_duration(absolute_path)
            return self.repository.create_uploaded(
                database,
                project_id=project_id,
                title=filename,
                filename=filename,
                path=str(relative_path),
                size=absolute_path.stat().st_size,
                duration=duration,
                status="uploaded",
            )
        except Exception:
            if absolute_path.exists():
                absolute_path.unlink()
            raise
        finally:
            file.file.close()

    def add_youtube_reference(
        self, database: Session, *, project_id: uuid.UUID, payload: YoutubeReferenceCreate
    ) -> ReferenceVideo:
        self.project_service.get_by_id(database, project_id)
        existing_reference = self.repository.get_by_youtube_id(database, project_id, payload.video_id)
        if existing_reference is not None:
            return existing_reference

        return self.repository.create_youtube(
            database,
            project_id=project_id,
            external_id=payload.video_id,
            external_url=str(payload.url),
            title=payload.title,
            channel_title=payload.channel_title,
            thumbnail_url=str(payload.thumbnail_url) if payload.thumbnail_url else None,
            description=payload.description,
        )
