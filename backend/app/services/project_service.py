import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.project import Project
from app.repositories.project_repository import ProjectRepository
from app.schemas.project import ProjectCreate


class ProjectNotFoundError(Exception):
    """Raised when a project cannot be found."""


class ProjectService:
    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()

    def create(self, database: Session, payload: ProjectCreate) -> Project:
        return self.repository.create(
            database,
            name=payload.name,
            description=payload.description,
            hashtag=payload.hashtag,
        )

    def list(self, database: Session) -> list[Project]:
        return self.repository.list(database)

    def get_by_id(self, database: Session, project_id: uuid.UUID) -> Project:
        project = self.repository.get_by_id(database, project_id)
        if project is None:
            raise ProjectNotFoundError
        return project

    def delete(self, database: Session, project_id: uuid.UUID) -> None:
        project = self.get_by_id(database, project_id)
        self.repository.delete(database, project)
        self._delete_project_files(project_id)

    @staticmethod
    def _delete_project_files(project_id: uuid.UUID) -> None:
        projects_directory = (Path(settings.storage_path) / "projects").resolve()
        project_directory = (projects_directory / str(project_id)).resolve()
        if project_directory.parent != projects_directory:
            return
        if project_directory.is_dir():
            shutil.rmtree(project_directory, ignore_errors=True)
