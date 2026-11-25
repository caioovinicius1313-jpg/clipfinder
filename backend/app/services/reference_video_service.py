import shutil
import tempfile
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
from app.services.storage_service import StorageService


class ReferenceVideoNotFoundError(Exception):
    """Raised when a reference does not belong to the requested project."""


class ReferenceVideoInUseError(Exception):
    """Raised when a reference is currently being processed."""


class ReferenceVideoService:
    def __init__(
        self,
        repository: ReferenceVideoRepository | None = None,
        project_service: ProjectService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.repository = repository or ReferenceVideoRepository()
        self.project_service = project_service or ProjectService()
        self.storage = storage or StorageService()

    def upload(
        self, database: Session, *, project_id: uuid.UUID, file: UploadFile
    ) -> ReferenceVideo:
        self.project_service.get_by_id(database, project_id)
        filename = Path(file.filename or "referencia").name
        extension = Path(filename).suffix.lower()
        relative_path = (
            Path("projects")
            / str(project_id)
            / "references"
            / f"{uuid.uuid4()}{extension}"
        )
        object_key = relative_path.as_posix()

        if self.storage.bucket_enabled:
            with tempfile.TemporaryDirectory(prefix="clipfinder-reference-") as temporary_directory:
                absolute_path = Path(temporary_directory) / f"reference{extension}"
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
                    return self.repository.create_uploaded(
                        database,
                        project_id=project_id,
                        title=filename,
                        filename=filename,
                        path=stored_path,
                        size=size,
                        duration=duration,
                        status="uploaded",
                    )
                finally:
                    file.file.close()

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

    def delete(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        reference_video_id: uuid.UUID,
    ) -> None:
        self.project_service.get_by_id(database, project_id)
        reference_video = self.repository.get_by_id(
            database,
            project_id,
            reference_video_id,
        )
        if reference_video is None:
            raise ReferenceVideoNotFoundError
        if any(clip.status == "processing" for clip in reference_video.generated_clips):
            raise ReferenceVideoInUseError(
                "Aguarde o processamento terminar antes de excluir esta referência."
            )

        stored_paths = {
            path
            for path in (
                reference_video.path,
                *(clip.path for clip in reference_video.generated_clips),
            )
            if path
        }
        for stored_path in stored_paths:
            self.storage.delete_object(stored_path)

        self.repository.delete(database, reference_video)

    def import_downloaded(
        self,
        database: Session,
        *,
        reference_video: ReferenceVideo,
        downloaded_path: Path,
        download_format_version: int | None = None,
    ) -> ReferenceVideo:
        extension = downloaded_path.suffix.lower() or ".mp4"
        filename = f"youtube-{reference_video.external_id or reference_video.id}{extension}"
        relative_path = (
            Path("projects")
            / str(reference_video.project_id)
            / "references"
            / f"{uuid.uuid4()}{extension}"
        )
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
        return self.repository.attach_downloaded_file(
            database,
            reference_video,
            filename=filename,
            path=stored_path,
            size=size,
            duration=duration,
            download_format_version=download_format_version,
        )
