import logging
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.reference_video import ReferenceVideo
from app.models.source_video import SourceVideo
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.repositories.source_video_repository import SourceVideoRepository
from app.services.media_processing_service import AudioMatchNotFoundError, AudioMatchingService
from app.services.storage_service import StorageService, StoredFileNotFoundError


logger = logging.getLogger(__name__)


class MatchingPrerequisiteError(Exception):
    """Raised when a project is not ready to run matching."""


class MatchingService:
    def __init__(
        self,
        source_repository: SourceVideoRepository | None = None,
        reference_repository: ReferenceVideoRepository | None = None,
        generated_clip_repository: GeneratedClipRepository | None = None,
        audio_matching_service: AudioMatchingService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.source_repository = source_repository or SourceVideoRepository()
        self.reference_repository = reference_repository or ReferenceVideoRepository()
        self.generated_clip_repository = generated_clip_repository or GeneratedClipRepository()
        self.audio_matching_service = audio_matching_service or AudioMatchingService()
        self.storage = storage or StorageService()

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
                    logger.exception("Falha ao processar o corte %s", clip.id)
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

        try:
            source_path = self.storage.media_input(source_video.path)
            reference_path = self.storage.media_input(reference_video.path)
        except StoredFileNotFoundError as error:
            raise MatchingPrerequisiteError("Não foi possível localizar os arquivos de vídeo enviados.")

        clip_key = f"projects/{source_video.project_id}/generated/{clip_id}.mp4"
        if self.storage.bucket_enabled:
            with tempfile.TemporaryDirectory(prefix="clipfinder-output-") as temporary_directory:
                output_path = Path(temporary_directory) / f"{clip_id}.mp4"
                start_seconds, end_seconds, match_score = self._generate_matched_clip(
                    source_path=source_path,
                    reference_path=reference_path,
                    output_path=output_path,
                )
                stored_path = self.storage.upload_file(
                    output_path,
                    object_key=clip_key,
                    content_type="video/mp4",
                )
        else:
            output_path = self.storage.local_path(clip_key)
            start_seconds, end_seconds, match_score = self._generate_matched_clip(
                source_path=source_path,
                reference_path=reference_path,
                output_path=output_path,
            )
            stored_path = clip_key

        clip = self.generated_clip_repository.get_by_id(database, clip_id)
        if clip is None:
            return
        self.generated_clip_repository.mark_completed(
            database,
            clip,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            match_score=match_score,
            path=stored_path,
        )

    def _generate_matched_clip(
        self,
        *,
        source_path: str,
        reference_path: str,
        output_path: Path,
    ) -> tuple[float, float, float]:
        try:
            start_seconds, duration_seconds, match_score = self.audio_matching_service.find_match(
                source_path, reference_path
            )
            self.audio_matching_service.media_service.generate_clip(
                source_path=source_path,
                output_path=output_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
            )
            end_seconds = start_seconds + duration_seconds
        except AudioMatchNotFoundError:
            matches = self.audio_matching_service.find_segmented_matches(source_path, reference_path)
            self.audio_matching_service.media_service.generate_montage(
                source_path=source_path,
                output_path=output_path,
                segments=[(match.source_start_seconds, match.duration_seconds) for match in matches],
            )
            start_seconds = matches[0].source_start_seconds
            end_seconds = matches[-1].source_start_seconds + matches[-1].duration_seconds
            match_score = sum(match.score for match in matches) / len(matches)

        return start_seconds, end_seconds, match_score
