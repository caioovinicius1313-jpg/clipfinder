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
from app.services.media_processing_service import AudioMatchingService
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

    def create_jobs(
        self,
        database: Session,
        project_id: uuid.UUID,
        reference_video_ids: list[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        source_video = self.source_repository.get_latest_for_project(database, project_id)
        if source_video is None:
            raise MatchingPrerequisiteError("Envie o vídeo original antes de gerar cortes.")

        reference_videos = self.reference_repository.list_uploaded(database, project_id)
        if reference_video_ids is not None:
            selected_ids = set(reference_video_ids)
            reference_videos = [
                reference for reference in reference_videos if reference.id in selected_ids
            ]
        if not reference_videos:
            raise MatchingPrerequisiteError(
                "Envie pelo menos um vídeo de referência antes de gerar cortes."
            )

        try:
            self._ensure_stored_file_exists(source_video.path)
            for reference_video in reference_videos:
                self._ensure_stored_file_exists(reference_video.path)
        except StoredFileNotFoundError as error:
            raise MatchingPrerequisiteError(
                "Um vídeo enviado não existe mais no armazenamento. Remova a referência antiga "
                "e envie o arquivo novamente antes de gerar os cortes."
            ) from error

        return [
            self.generated_clip_repository.create(
                database,
                project_id=project_id,
                source_video_id=source_video.id,
                reference_video_id=reference_video.id,
            ).id
            for reference_video in reference_videos
        ]

    def _ensure_stored_file_exists(self, stored_path: str) -> None:
        if self.storage.is_bucket_path(stored_path):
            self.storage.object_size(self.storage.object_key(stored_path))
            return
        if not self.storage.local_path(stored_path).is_file():
            raise StoredFileNotFoundError

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
        matches = self.audio_matching_service.find_matches(source_path, reference_path)
        clip_segments = [
            (match.source_start_seconds, match.duration_seconds) for match in matches
        ]
        # A contiguous match may begin after the reference's first seconds
        # when leading audio is quiet or unlike the source. In that case the
        # match start maps to reference_start_seconds, not to t=0. Restore
        # the omitted prefix using the measured tempo, but only for a single
        # contiguous match; extrapolating across a montage edit is unsafe.
        if len(matches) == 1 and matches[0].reference_start_seconds > 0:
            match = matches[0]
            prefix_duration = match.reference_start_seconds * match.tempo_factor
            expanded_start = match.source_start_seconds - prefix_duration
            if expanded_start >= 0:
                clip_segments[0] = (
                    expanded_start,
                    match.duration_seconds + prefix_duration,
                )
        self.audio_matching_service.media_service.generate_montage(
            source_path=source_path,
            output_path=output_path,
            segments=clip_segments,
        )
        start_seconds = min(start for start, _duration in clip_segments)
        end_seconds = max(start + duration for start, duration in clip_segments)
        total_reference_duration = sum(
            match.reference_duration_seconds or match.duration_seconds for match in matches
        )
        match_score = sum(
            match.score * (match.reference_duration_seconds or match.duration_seconds)
            for match in matches
        ) / total_reference_duration

        return start_seconds, end_seconds, match_score
