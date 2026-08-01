import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.generated_clip import GeneratedClip
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.services.project_service import ProjectService


class GeneratedClipNotAvailableError(Exception):
    """Raised when a generated file cannot be downloaded."""


class GeneratedClipService:
    def __init__(
        self,
        repository: GeneratedClipRepository | None = None,
        project_service: ProjectService | None = None,
    ) -> None:
        self.repository = repository or GeneratedClipRepository()
        self.project_service = project_service or ProjectService()

    def get_download_path(
        self, database: Session, *, project_id: uuid.UUID, clip_id: uuid.UUID
    ) -> tuple[GeneratedClip, Path]:
        self.project_service.get_by_id(database, project_id)
        clip = self.repository.get_by_id(database, clip_id)
        if clip is None or clip.project_id != project_id or clip.status != "completed" or not clip.path:
            raise GeneratedClipNotAvailableError

        absolute_path = Path(settings.storage_path) / clip.path
        if not absolute_path.is_file():
            raise GeneratedClipNotAvailableError
        return clip, absolute_path
