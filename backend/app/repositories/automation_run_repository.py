import uuid

from sqlalchemy.orm import Session

from app.models.automation_run import AutomationRun


class AutomationRunRepository:
    def create(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        source_url: str,
        hashtag: str,
        total_references: int,
    ) -> AutomationRun:
        run = AutomationRun(
            project_id=project_id,
            source_url=source_url,
            hashtag=hashtag,
            total_references=total_references,
            status="queued",
        )
        database.add(run)
        database.commit()
        database.refresh(run)
        return run

    def get_by_id(
        self, database: Session, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AutomationRun | None:
        run = database.get(AutomationRun, run_id)
        if run is None or run.project_id != project_id:
            return None
        return run

    def update(
        self,
        database: Session,
        run: AutomationRun,
        *,
        status: str | None = None,
        source_video_id: uuid.UUID | None = None,
        ready_references: int | None = None,
        jobs_started: int | None = None,
        error_message: str | None = None,
    ) -> AutomationRun:
        if status is not None:
            run.status = status
        if source_video_id is not None:
            run.source_video_id = source_video_id
        if ready_references is not None:
            run.ready_references = ready_references
        if jobs_started is not None:
            run.jobs_started = jobs_started
        run.error_message = error_message
        database.add(run)
        database.commit()
        database.refresh(run)
        return run
