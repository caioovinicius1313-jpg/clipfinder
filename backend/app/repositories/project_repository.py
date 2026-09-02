import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.project import Project


class ProjectRepository:
    def get_by_id(self, database: Session, project_id: uuid.UUID) -> Project | None:
        statement = (
            select(Project)
            .options(
                selectinload(Project.source_videos),
                selectinload(Project.reference_videos),
                selectinload(Project.generated_clips),
            )
            .where(Project.id == project_id)
        )
        return database.scalar(statement)

    def list(self, database: Session) -> list[Project]:
        statement = select(Project).order_by(Project.created_at.desc())
        return list(database.scalars(statement))

    def list_with_hashtag(self, database: Session) -> list[Project]:
        statement = (
            select(Project)
            .where(Project.hashtag.is_not(None), Project.hashtag != "")
            .order_by(Project.created_at.asc())
        )
        return list(database.scalars(statement))

    def create(
        self,
        database: Session,
        *,
        name: str,
        description: str | None,
        hashtag: str | None,
    ) -> Project:
        project = Project(name=name, description=description, hashtag=hashtag)
        database.add(project)
        database.commit()
        database.refresh(project)
        return project

    def delete(self, database: Session, project: Project) -> None:
        database.delete(project)
        database.commit()
