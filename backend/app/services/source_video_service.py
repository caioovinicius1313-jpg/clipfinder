import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.source_video import SourceVideo
from app.repositories.source_video_repository import SourceVideoRepository
from app.services.media_processing_service import MediaProcessingService
from app.services.project_service import ProjectService


class SourceVideoService:
    def __init__(
        self,
        repository: SourceVideoRepository | None = None,
        project_service: ProjectService | None = None,
    ) -> None:
        self.repository = repository or SourceVideoRepository()
        self.project_service = project_service or ProjectService()

    def upload(
        self, database: Session, *, project_id: uuid.UUID, file: UploadFile
    ) -> SourceVideo:
        self.project_service.get_by_id(database, project_id)

        original_filename = Path(file.filename or "video").name
        extension = Path(original_filename).suffix.lower()
        relative_path = Path("projects") / str(project_id) / "source" / f"{uuid.uuid4()}{extension}"
        absolute_path = Path(settings.storage_path) / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with absolute_path.open("wb") as destination:
                shutil.copyfileobj(file.file, destination)

            duration = MediaProcessingService().probe_duration(absolute_path)

            return self.repository.create(
                database,
                project_id=project_id,
                filename=original_filename,
                path=str(relative_path),
                size=absolute_path.stat().st_size,
                duration=duration,
            )
        except Exception:
            if absolute_path.exists():
                absolute_path.unlink()
            raise
        finally:
            file.file.close()
