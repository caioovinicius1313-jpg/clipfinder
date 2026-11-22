import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.automation_run import AutomationRun
from app.models.reference_video import ReferenceVideo
from app.repositories.automation_run_repository import AutomationRunRepository
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.repositories.source_video_repository import SourceVideoRepository
from app.schemas.automation import (
    AutomationCandidateRead,
    AutomationSearchRequest,
    AutomationSearchResponse,
    AutomationStartRequest,
)
from app.schemas.reference_video import YoutubeReferenceCreate
from app.services.matching_service import MatchingService
from app.services.project_service import ProjectService
from app.services.reference_video_service import ReferenceVideoService
from app.services.source_video_service import SourceVideoService
from app.services.storage_service import StorageService, StoredFileNotFoundError
from app.services.youtube_download_service import YoutubeDownloadService
from app.services.youtube_service import YoutubeService


logger = logging.getLogger(__name__)


class AutomationRunNotFoundError(Exception):
    """Raised when an automation run does not belong to the project."""


class AutomationSelectionError(Exception):
    """Raised when selected YouTube videos cannot be validated."""


@dataclass(frozen=True)
class AutomationStartResult:
    run: AutomationRun
    source_video_id: str
    reference_video_ids: list[uuid.UUID]


class AutomationService:
    def __init__(
        self,
        *,
        youtube: YoutubeService | None = None,
        downloader: YoutubeDownloadService | None = None,
        project_service: ProjectService | None = None,
        run_repository: AutomationRunRepository | None = None,
        reference_repository: ReferenceVideoRepository | None = None,
        source_repository: SourceVideoRepository | None = None,
        source_service: SourceVideoService | None = None,
        reference_service: ReferenceVideoService | None = None,
        matching_service: MatchingService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.youtube = youtube or YoutubeService()
        self.downloader = downloader or YoutubeDownloadService()
        self.project_service = project_service or ProjectService()
        self.run_repository = run_repository or AutomationRunRepository()
        self.reference_repository = reference_repository or ReferenceVideoRepository()
        self.source_repository = source_repository or SourceVideoRepository()
        self.source_service = source_service or SourceVideoService()
        self.reference_service = reference_service or ReferenceVideoService()
        self.matching_service = matching_service or MatchingService()
        self.storage = storage or StorageService()

    async def search(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: AutomationSearchRequest,
    ) -> AutomationSearchResponse:
        self.project_service.get_by_id(database, project_id)
        source_video_id = self.downloader.extract_video_id(str(payload.source_url))
        candidates, next_page_token, search_window_start = await self.youtube.search_automation_candidates(
            payload.hashtag,
            source_video_id=source_video_id,
            limit=10,
            sort_by=payload.sort_by,
            page_token=payload.page_token,
            published_after=payload.published_after,
        )
        return AutomationSearchResponse(
            source_video_id=source_video_id,
            hashtag=payload.hashtag,
            candidates=candidates,
            next_page_token=next_page_token,
            search_window_start=search_window_start,
        )

    async def start(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: AutomationStartRequest,
    ) -> AutomationStartResult:
        self.project_service.get_by_id(database, project_id)
        source_video_id = self.downloader.extract_video_id(str(payload.source_url))
        if source_video_id in payload.selected_video_ids:
            raise AutomationSelectionError(
                "O vídeo original não pode ser selecionado como corte de referência."
            )

        candidates = await self.youtube.get_automation_video_details(
            payload.selected_video_ids
        )
        candidates_by_id = {candidate.video_id: candidate for candidate in candidates}
        missing_ids = [
            video_id
            for video_id in payload.selected_video_ids
            if video_id not in candidates_by_id
        ]
        if missing_ids:
            raise AutomationSelectionError(
                "Um ou mais vídeos selecionados não estão mais disponíveis no YouTube."
            )
        if any(
            not 5 <= candidate.duration_seconds <= 180
            for candidate in candidates_by_id.values()
        ):
            raise AutomationSelectionError(
                "Os cortes selecionados precisam ter entre 5 segundos e 3 minutos."
            )

        reference_ids: list[uuid.UUID] = []
        for video_id in payload.selected_video_ids:
            candidate = candidates_by_id[video_id]
            reference = self._get_or_create_reference(database, project_id, candidate)
            if not reference.path:
                reference = self.reference_repository.set_status(
                    database, reference, "queued"
                )
            reference_ids.append(reference.id)

        run = self.run_repository.create(
            database,
            project_id=project_id,
            source_url=str(payload.source_url),
            hashtag=payload.hashtag,
            total_references=len(reference_ids),
        )
        return AutomationStartResult(
            run=run,
            source_video_id=source_video_id,
            reference_video_ids=reference_ids,
        )

    def get_run(
        self, database: Session, *, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AutomationRun:
        self.project_service.get_by_id(database, project_id)
        run = self.run_repository.get_by_id(database, project_id, run_id)
        if run is None:
            raise AutomationRunNotFoundError
        return run

    def process_run(
        self,
        run_id: uuid.UUID,
        project_id: uuid.UUID,
        source_youtube_id: str,
        reference_video_ids: list[uuid.UUID],
    ) -> None:
        with SessionLocal() as database:
            run = self.run_repository.get_by_id(database, project_id, run_id)
            if run is None:
                return
            try:
                with tempfile.TemporaryDirectory(prefix="clipfinder-automation-") as directory:
                    temporary_directory = Path(directory)
                    source_video = self._prepare_source(
                        database,
                        run,
                        project_id=project_id,
                        source_youtube_id=source_youtube_id,
                        temporary_directory=temporary_directory,
                    )
                    prepared_reference_ids = self._prepare_references(
                        database,
                        run,
                        project_id=project_id,
                        reference_video_ids=reference_video_ids,
                        temporary_directory=temporary_directory,
                    )
                    self.run_repository.update(
                        database,
                        run,
                        status="matching",
                        source_video_id=source_video.id,
                        ready_references=len(prepared_reference_ids),
                    )
                    job_ids = self.matching_service.create_jobs(
                        database,
                        project_id,
                        prepared_reference_ids,
                    )
                    self.run_repository.update(
                        database,
                        run,
                        status="matching",
                        jobs_started=len(job_ids),
                    )
                    self.matching_service.process_jobs(job_ids)
                    self.run_repository.update(database, run, status="completed")
            except Exception as error:
                logger.exception("Falha na automação %s", run_id)
                database.rollback()
                run = self.run_repository.get_by_id(database, project_id, run_id)
                if run is not None:
                    self.run_repository.update(
                        database,
                        run,
                        status="failed",
                        error_message=str(error)[:2000],
                    )
                self._mark_unprepared_references_failed(
                    database, project_id, reference_video_ids
                )

    def _prepare_source(
        self,
        database: Session,
        run: AutomationRun,
        *,
        project_id: uuid.UUID,
        source_youtube_id: str,
        temporary_directory: Path,
    ):
        filename = f"youtube-{source_youtube_id}.mp4"
        latest = self.source_repository.get_latest_for_project(database, project_id)
        if (
            latest is not None
            and latest.filename == filename
            and self._stored_file_exists(latest.path)
        ):
            self.run_repository.update(
                database, run, status="downloading_references", source_video_id=latest.id
            )
            return latest

        self.run_repository.update(database, run, status="downloading_source")
        downloaded_path = self.downloader.download_video(
            source_youtube_id,
            temporary_directory,
            filename_stem="source",
        )
        source_video = self.source_service.import_downloaded(
            database,
            project_id=project_id,
            downloaded_path=downloaded_path,
            filename=filename,
        )
        self.run_repository.update(
            database,
            run,
            status="downloading_references",
            source_video_id=source_video.id,
        )
        return source_video

    def _prepare_references(
        self,
        database: Session,
        run: AutomationRun,
        *,
        project_id: uuid.UUID,
        reference_video_ids: list[uuid.UUID],
        temporary_directory: Path,
    ) -> list[uuid.UUID]:
        prepared: list[uuid.UUID] = []
        for index, reference_id in enumerate(reference_video_ids, start=1):
            reference = self.reference_repository.get_by_id(
                database, project_id, reference_id
            )
            if reference is None:
                raise AutomationSelectionError("Uma referência selecionada foi excluída.")
            if reference.path and self._stored_file_exists(reference.path):
                prepared.append(reference.id)
            else:
                self.reference_repository.set_status(database, reference, "downloading")
                downloaded_path = self.downloader.download_video(
                    str(reference.external_id),
                    temporary_directory,
                    filename_stem=f"reference-{index}",
                    reference_quality=True,
                )
                reference = self.reference_service.import_downloaded(
                    database,
                    reference_video=reference,
                    downloaded_path=downloaded_path,
                )
                prepared.append(reference.id)
            self.run_repository.update(
                database,
                run,
                status="downloading_references",
                ready_references=len(prepared),
            )
        return prepared

    def _stored_file_exists(self, stored_path: str) -> bool:
        try:
            if self.storage.is_bucket_path(stored_path):
                self.storage.object_size(self.storage.object_key(stored_path))
                return True
            return self.storage.local_path(stored_path).is_file()
        except StoredFileNotFoundError:
            return False

    def _get_or_create_reference(
        self,
        database: Session,
        project_id: uuid.UUID,
        candidate: AutomationCandidateRead,
    ) -> ReferenceVideo:
        existing = self.reference_repository.get_by_youtube_id(
            database, project_id, candidate.video_id
        )
        if existing is not None:
            return existing
        return self.reference_service.add_youtube_reference(
            database,
            project_id=project_id,
            payload=YoutubeReferenceCreate(
                video_id=candidate.video_id,
                title=candidate.title,
                url=candidate.url,
                channel_title=candidate.channel_title,
                thumbnail_url=candidate.thumbnail_url,
                description=candidate.description,
            ),
        )

    def _mark_unprepared_references_failed(
        self,
        database: Session,
        project_id: uuid.UUID,
        reference_video_ids: list[uuid.UUID],
    ) -> None:
        for reference_id in reference_video_ids:
            reference = self.reference_repository.get_by_id(
                database, project_id, reference_id
            )
            if reference is not None and not reference.path:
                self.reference_repository.set_status(database, reference, "failed")
