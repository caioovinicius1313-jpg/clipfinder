import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.generated_clip import GeneratedClip


class GeneratedClipRepository:
    def create(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        source_video_id: uuid.UUID,
        reference_video_id: uuid.UUID,
    ) -> GeneratedClip:
        clip = GeneratedClip(
            project_id=project_id,
            source_video_id=source_video_id,
            reference_video_id=reference_video_id,
            status="processing",
        )
        database.add(clip)
        database.commit()
        database.refresh(clip)
        return clip

    def get_by_id(self, database: Session, clip_id: uuid.UUID) -> GeneratedClip | None:
        return database.scalar(select(GeneratedClip).where(GeneratedClip.id == clip_id))

    def mark_completed(
        self,
        database: Session,
        clip: GeneratedClip,
        *,
        start_seconds: float,
        end_seconds: float,
        match_score: float,
        path: str,
    ) -> GeneratedClip:
        clip.original_start_seconds = start_seconds
        clip.original_end_seconds = end_seconds
        clip.match_score = match_score
        clip.path = path
        clip.status = "completed"
        clip.error_message = None
        database.commit()
        database.refresh(clip)
        return clip

    def mark_failed(self, database: Session, clip: GeneratedClip, error_message: str) -> GeneratedClip:
        clip.status = "failed"
        clip.error_message = error_message[:2000]
        database.commit()
        database.refresh(clip)
        return clip
