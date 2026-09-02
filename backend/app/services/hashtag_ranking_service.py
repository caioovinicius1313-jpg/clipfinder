import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import cast

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.hashtag_video_daily_stat import HashtagVideoDailyStat
from app.repositories.hashtag_video_daily_stat_repository import (
    HashtagVideoDailyStatRepository,
    RankingMetric,
)
from app.repositories.project_repository import ProjectRepository
from app.schemas.youtube import HashtagRankingRead, YoutubeRankingMetric, YoutubeVideoDetails
from app.services.project_service import ProjectService
from app.services.youtube_download_service import YoutubeDownloadService
from app.services.youtube_service import (
    YoutubeConfigurationError,
    YoutubeRequestError,
    YoutubeService,
)


class HashtagRankingNotFoundError(Exception):
    """Raised when a ranking item does not belong to the requested project."""


@dataclass(frozen=True)
class HashtagRankingSyncResult:
    snapshot_date: date
    hashtags_processed: int
    projects_updated: int
    videos_saved: int
    hashtags_skipped: int
    errors: dict[str, str] = field(default_factory=dict)


class HashtagRankingService:
    def __init__(
        self,
        *,
        repository: HashtagVideoDailyStatRepository | None = None,
        project_repository: ProjectRepository | None = None,
        youtube: YoutubeService | None = None,
        downloader: YoutubeDownloadService | None = None,
        project_service: ProjectService | None = None,
    ) -> None:
        self.repository = repository or HashtagVideoDailyStatRepository()
        self.project_repository = project_repository or ProjectRepository()
        self.youtube = youtube or YoutubeService()
        self.downloader = downloader or YoutubeDownloadService()
        self.project_service = project_service or ProjectService()

    async def sync_all(
        self,
        database: Session,
        *,
        snapshot_date: date | None = None,
    ) -> HashtagRankingSyncResult:
        target_date = snapshot_date or date.today()
        grouped_projects: dict[str, list[tuple[uuid.UUID, str]]] = defaultdict(list)
        hashtag_labels: dict[str, str] = {}
        for project in self.project_repository.list_with_hashtag(database):
            normalized_hashtag = self.normalize_hashtag(project.hashtag)
            if normalized_hashtag is None:
                continue
            key = normalized_hashtag.casefold()
            hashtag_labels.setdefault(key, normalized_hashtag)
            grouped_projects[key].append((project.id, normalized_hashtag))

        grouped_items = list(grouped_projects.items())
        run_limit = settings.youtube_ranking_max_hashtags_per_run
        selected_groups = grouped_items[:run_limit]
        errors: dict[str, str] = {}
        projects_updated = 0
        videos_saved = 0

        for hashtag_key, projects in selected_groups:
            hashtag = hashtag_labels[hashtag_key]
            previous_by_project: dict[
                uuid.UUID, tuple[date | None, list[HashtagVideoDailyStat]]
            ] = {}
            carryover_ids: list[str] = []
            for project_id, _ in projects:
                previous = self.repository.list_latest_before(
                    database,
                    project_id,
                    target_date,
                    limit=settings.youtube_ranking_candidate_limit,
                )
                previous_by_project[project_id] = previous
                carryover_ids.extend(stat.video_id for stat in previous[1])

            try:
                videos = await self.youtube.search_hashtag_statistics(
                    hashtag,
                    limit=settings.youtube_ranking_candidate_limit,
                    carryover_video_ids=carryover_ids,
                )
            except (YoutubeConfigurationError, YoutubeRequestError) as error:
                errors[hashtag] = str(error)
                continue

            for project_id, project_hashtag in projects:
                previous_date, previous_stats = previous_by_project[project_id]
                previous_views = {stat.video_id: stat.views for stat in previous_stats}
                has_24_hour_baseline = previous_date == target_date - timedelta(days=1)
                snapshot = [
                    self._build_stat(
                        project_id=project_id,
                        hashtag=project_hashtag,
                        snapshot_date=target_date,
                        video=video,
                        previous_views=previous_views if has_24_hour_baseline else {},
                    )
                    for video in videos
                ]
                self.repository.replace_snapshot(
                    database,
                    project_id=project_id,
                    snapshot_date=target_date,
                    stats=snapshot,
                )
                projects_updated += 1
                videos_saved += len(snapshot)

        return HashtagRankingSyncResult(
            snapshot_date=target_date,
            hashtags_processed=len(selected_groups) - len(errors),
            projects_updated=projects_updated,
            videos_saved=videos_saved,
            hashtags_skipped=max(0, len(grouped_items) - len(selected_groups)),
            errors=errors,
        )

    def list_for_project(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        metric: YoutubeRankingMetric | None,
        limit: int | None,
    ) -> HashtagRankingRead:
        project = self.project_service.get_by_id(database, project_id)
        selected_metric = metric or YoutubeRankingMetric(settings.youtube_ranking_metric)
        latest_date = self.repository.latest_date(database, project_id)
        videos = (
            self.repository.list_for_date(
                database,
                project_id,
                latest_date,
                metric=cast(RankingMetric, selected_metric.value),
                limit=limit or settings.youtube_ranking_top_limit,
            )
            if latest_date
            else []
        )
        return HashtagRankingRead(
            hashtag=self.normalize_hashtag(project.hashtag),
            snapshot_date=latest_date,
            metric=selected_metric,
            videos=videos,
        )

    def get_download_url(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        stat_id: uuid.UUID,
    ) -> str:
        stat = self.repository.get_by_id(database, project_id, stat_id)
        if stat is None:
            raise HashtagRankingNotFoundError
        return self.downloader.get_temporary_download_url(stat.video_id)

    @staticmethod
    def normalize_hashtag(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned == "#":
            return None
        return cleaned if cleaned.startswith("#") else f"#{cleaned}"

    @staticmethod
    def _build_stat(
        *,
        project_id: uuid.UUID,
        hashtag: str,
        snapshot_date: date,
        video: YoutubeVideoDetails,
        previous_views: dict[str, int],
    ) -> HashtagVideoDailyStat:
        prior_views = previous_views.get(video.video_id)
        return HashtagVideoDailyStat(
            project_id=project_id,
            hashtag=hashtag,
            video_id=video.video_id,
            snapshot_date=snapshot_date,
            title=video.title,
            channel_title=video.channel_title,
            published_at=video.published_at,
            thumbnail_url=video.thumbnail_url,
            views=video.views,
            view_delta=video.views - prior_views if prior_views is not None else None,
        )
