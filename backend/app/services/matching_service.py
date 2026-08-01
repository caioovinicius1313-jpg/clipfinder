import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.reference_video import ReferenceVideo
from app.models.source_video import SourceVideo
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.repositories.source_video_repository import SourceVideoRepository
from app.services.media_processing_service import AudioMatchNotFoundError, AudioMatchingService


class MatchingPrerequisiteError(Exception):
    """Raised when a project is not ready to run matching."""


class MatchingService:
    def __init__(
        self,
        source_repository: SourceVideoRepository | None = None,
        reference_repository: ReferenceVideoRepository | None = None,
        generated_clip_repository: GeneratedClipRepository | None = None,
        audio_matching_service: AudioMatchingService | None = None,
    ) -> None:
        self.source_repository = source_repository or SourceVideoRepository()
        self.reference_repository = reference_repository or ReferenceVideoRepository()
        self.generated_clip_repository = generated_clip_repository or GeneratedClipRepository()
        self.audio_matching_service = audio_matching_service or AudioMatchingService()

    def create_jobs(self, database: Session, project_id: uuid.UUID) -> list[uuid.UUID]:
        source_video = self.source_repository.get_latest_for_project(database, project_id)
        if source_video is None:
            raise MatchingPrerequisiteError("Envie o vídeo original antes de gerar cortes.")

        reference_videos = self.reference_repository.list_uploaded(database, project_id)
        if not reference_videos:
            raise MatchingPrerequisiteError("Envie pelo menos um vídeo de referência antes de gerar cortes.")

        return [
            self.generated_clip_repository.create(
                database,
                project_id=project_id,
                source_video_id=source_video.id,
                reference_video_id=reference_video.id,
            ).id
            for reference_video in reference_videos
        ]

    def process_jobs(self, job_ids: list[uuid.UUID]) -> None:
        for job_id in job_ids:
            with SessionLocal() as database:
                clip = self.generated_clip_repository.get_by_id(database, job_id)
                if clip is None:
                    continue

                source_video = database.get(SourceVideo, clip.source_video_id)
                reference_video = database.get(ReferenceVideo, clip.reference_video_id)
                if source_video is None or reference_video is None:
                    self.generated_clip_repository.mark_failed(
                        database, clip, "Os arquivos necessários para este corte não existem mais."
                    )
                    continue

                try:
                    self._process_job(database, clip.id, source_video, reference_video)
                except Exception as error:
                    self.generated_clip_repository.mark_failed(database, clip, str(error))

    def _process_job(
        self,
        database: Session,
        clip_id: uuid.UUID,
        source_video: SourceVideo,
        reference_video: ReferenceVideo,
    ) -> None:
        if not source_video.path or not reference_video.path:
            raise MatchingPrerequisiteError("O corte precisa ter um arquivo de referência enviado.")

        source_path = Path(settings.storage_path) / source_video.path
        reference_path = Path(settings.storage_path) / reference_video.path
        if not source_path.is_file() or not reference_path.is_file():
            raise MatchingPrerequisiteError("Não foi possível localizar os arquivos de vídeo enviados.")

        clip_path = Path("projects") / str(source_video.project_id) / "generated" / f"{clip_id}.mp4"
        absolute_clip_path = Path(settings.storage_path) / clip_path
        try:
            start_seconds, duration_seconds, match_score = self.audio_matching_service.find_match(
                source_path, reference_path
            )
            self.audio_matching_service.media_service.generate_clip(
                source_path=source_path,
                output_path=absolute_clip_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
            )
            end_seconds = start_seconds + duration_seconds
        except AudioMatchNotFoundError:
            matches = self.audio_matching_service.find_segmented_matches(source_path, reference_path)
            self.audio_matching_service.media_service.generate_montage(
                source_path=source_path,
                output_path=absolute_clip_path,
                segments=[(match.source_start_seconds, match.duration_seconds) for match in matches],
            )
            start_seconds = matches[0].source_start_seconds
            end_seconds = matches[-1].source_start_seconds + matches[-1].duration_seconds
            duration_seconds = sum(match.duration_seconds for match in matches)
            match_score = sum(match.score for match in matches) / len(matches)

        clip = self.generated_clip_repository.get_by_id(database, clip_id)
        if clip is None:
            return
        self.generated_clip_repository.mark_completed(
            database,
            clip,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            match_score=match_score,
            path=str(clip_path),
        )
