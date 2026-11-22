import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.source_video import SourceVideo
from app.repositories.source_video_repository import SourceVideoRepository
from app.services.media_processing_service import MediaProcessingService
from app.services.project_service import ProjectService
from app.services.storage_service import StorageService


class SourceVideoService:
    def __init__(
        self,
        repository: SourceVideoRepository | None = None,
        project_service: ProjectService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.repository = repository or SourceVideoRepository()
        self.project_service = project_service or ProjectService()
        self.storage = storage or StorageService()

    def upload(
        self, database: Session, *, project_id: uuid.UUID, file: UploadFile
    ) -> SourceVideo:
        self.project_service.get_by_id(database, project_id)

        original_filename = Path(file.filename or "video").name
        extension = Path(original_filename).suffix.lower()
        relative_path = Path("projects") / str(project_id) / "source" / f"{uuid.uuid4()}{extension}"
        object_key = relative_path.as_posix()

        if self.storage.bucket_enabled:
            with tempfile.TemporaryDirectory(prefix="clipfinder-source-") as temporary_directory:
                absolute_path = Path(temporary_directory) / f"source{extension}"
                try:
                    with absolute_path.open("wb") as destination:
                        shutil.copyfileobj(file.file, destination)
                    duration = MediaProcessingService().probe_duration(absolute_path)
                    size = absolute_path.stat().st_size
                    stored_path = self.storage.upload_file(
                        absolute_path,
                        object_key=object_key,
                        content_type=file.content_type or "video/mp4",
                    )
                    return self.repository.create(
                        database,
                        project_id=project_id,
                        filename=original_filename,
                        path=stored_path,
                        size=size,
                        duration=duration,
                    )
                finally:
                    file.file.close()

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

    def import_downloaded(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        downloaded_path: Path,
        filename: str,
    ) -> SourceVideo:
        self.project_service.get_by_id(database, project_id)
        extension = downloaded_path.suffix.lower() or ".mp4"
        relative_path = Path("projects") / str(project_id) / "source" / f"{uuid.uuid4()}{extension}"
        object_key = relative_path.as_posix()
        duration = MediaProcessingService().probe_duration(downloaded_path)
        size = downloaded_path.stat().st_size
        if self.storage.bucket_enabled:
            stored_path = self.storage.upload_file(
                downloaded_path,
                object_key=object_key,
                content_type="video/mp4",
            )
        else:
            absolute_path = Path(settings.storage_path) / relative_path
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(downloaded_path, absolute_path)
            stored_path = str(relative_path)
        return self.repository.create(
            database,
            project_id=project_id,
            filename=filename,
            path=stored_path,
            size=size,
            duration=duration,
        )
