import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.generated_clip import GeneratedClip
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.services.project_service import ProjectService
from app.services.storage_service import (
    StorageOperationError,
    StorageService,
    StoredFileNotFoundError,
)


class GeneratedClipNotAvailableError(Exception):
    """Raised when a generated file cannot be downloaded."""


class GeneratedClipService:
    def __init__(
        self,
        repository: GeneratedClipRepository | None = None,
        project_service: ProjectService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.repository = repository or GeneratedClipRepository()
        self.project_service = project_service or ProjectService()
        self.storage = storage or StorageService()

    def get_download_target(
        self, database: Session, *, project_id: uuid.UUID, clip_id: uuid.UUID
    ) -> tuple[GeneratedClip, Path | str]:
        self.project_service.get_by_id(database, project_id)
        clip = self.repository.get_by_id(database, clip_id)
        if clip is None or clip.project_id != project_id or clip.status != "completed" or not clip.path:
            raise GeneratedClipNotAvailableError

        if self.storage.is_bucket_path(clip.path):
            try:
                self.storage.object_size(self.storage.object_key(clip.path))
                return clip, self.storage.create_download_url(
                    clip.path, filename=f"clip-{clip.id}.mp4"
                )
            except (StoredFileNotFoundError, StorageOperationError) as error:
                raise GeneratedClipNotAvailableError from error

        absolute_path = self.storage.local_path(clip.path)
        if not absolute_path.is_file():
            raise GeneratedClipNotAvailableError
        return clip, absolute_path
