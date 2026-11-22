from collections.abc import Iterable
import math
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.schemas.automation import AutomationCandidateRead
from app.schemas.youtube import YoutubeSearchResult, YoutubeVideoDetails


class YoutubeConfigurationError(Exception):
    """Raised when the YouTube Data API key is not configured."""


class YoutubeRequestError(Exception):
    """Raised when YouTube rejects a request."""


class YoutubeService:
    search_endpoint = "https://www.googleapis.com/youtube/v3/search"
    videos_endpoint = "https://www.googleapis.com/youtube/v3/videos"

    async def search(self, query: str, limit: int = 10) -> list[YoutubeSearchResult]:
        parameters = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "maxResults": min(max(limit, 1), 25),
        }
        payload = await self._request(self.search_endpoint, parameters)

        results: list[YoutubeSearchResult] = []
        for item in payload.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id:
                continue

            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
            )
            results.append(
                YoutubeSearchResult(
                    video_id=video_id,
                    title=snippet.get("title", "Vídeo sem título"),
                    description=snippet.get("description", ""),
                    channel_title=snippet.get("channelTitle", "Canal desconhecido"),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    thumbnail_url=thumbnail.get("url") if thumbnail else None,
                    published_at=self._parse_datetime(snippet.get("publishedAt")),
                )
            )
        return results

    async def search_hashtag_statistics(
        self,
        hashtag: str,
        *,
        limit: int,
        carryover_video_ids: Iterable[str] = (),
    ) -> list[YoutubeVideoDetails]:
        search_payload = await self._request(
            self.search_endpoint,
            {
                "part": "snippet",
                "type": "video",
                "q": hashtag,
                "order": "viewCount",
                "maxResults": min(max(limit, 1), 25),
                "fields": "items(id/videoId)",
            },
        )
        video_ids = [
            item.get("id", {}).get("videoId")
            for item in search_payload.get("items", [])
        ]
        video_ids.extend(carryover_video_ids)
        unique_video_ids = list(
            dict.fromkeys(video_id for video_id in video_ids if video_id)
        )[:50]
        if not unique_video_ids:
            return []

        videos_payload = await self._request(
            self.videos_endpoint,
            {
                "part": "snippet,statistics",
                "id": ",".join(unique_video_ids),
                "fields": (
                    "items(id,snippet(title,channelTitle,publishedAt,thumbnails),"
                    "statistics(viewCount))"
                ),
            },
        )
        videos: list[YoutubeVideoDetails] = []
        for item in videos_payload.get("items", []):
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
            )
            try:
                views = int(statistics.get("viewCount", 0))
            except (TypeError, ValueError):
                views = 0
            videos.append(
                YoutubeVideoDetails(
                    video_id=item["id"],
                    title=snippet.get("title", "Vídeo sem título"),
                    channel_title=snippet.get("channelTitle", "Canal desconhecido"),
                    thumbnail_url=thumbnail.get("url") if thumbnail else None,
                    published_at=self._parse_datetime(snippet.get("publishedAt")),
                    views=views,
                )
            )
        return videos

    async def search_automation_candidates(
        self,
        hashtag: str,
        *,
        source_video_id: str,
        limit: int = 10,
        sort_by: str = "relevance",
        page_token: str | None = None,
        published_after: datetime | None = None,
    ) -> tuple[list[AutomationCandidateRead], str | None, datetime]:
        clean_hashtag = hashtag.strip().lstrip("#")
        search_limit = min(max(limit * 3, 10), 50)
        search_window_start = published_after or (
            datetime.now(timezone.utc) - timedelta(hours=24 if sort_by == "recent" else 168)
        )
        search_params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",
            "q": f"#{clean_hashtag}",
            "order": {"relevance": "relevance", "recent": "date", "views": "viewCount"}[sort_by],
            "publishedAfter": search_window_start.isoformat().replace("+00:00", "Z"),
            "videoDuration": "short",
            "maxResults": search_limit,
            "fields": "items(id/videoId),nextPageToken",
        }
        if page_token:
            search_params["pageToken"] = page_token
        search_payload = await self._request(self.search_endpoint, search_params)
        ranked_ids = [
            item.get("id", {}).get("videoId") for item in search_payload.get("items", [])
        ]
        ranked_ids = [
            video_id
            for video_id in dict.fromkeys(ranked_ids)
            if video_id and video_id != source_video_id
        ]
        if not ranked_ids:
            return [], search_payload.get("nextPageToken"), search_window_start

        details = await self.get_automation_video_details(ranked_ids)
        details_by_id = {item.video_id: item for item in details}
        ordered = [details_by_id[video_id] for video_id in ranked_ids if video_id in details_by_id]
        eligible = [item for item in ordered if 5 <= item.duration_seconds <= 180]
        hashtag_pattern = re.compile(
            rf"(?<![\w])#{re.escape(clean_hashtag)}(?![\w])", re.IGNORECASE
        )
        exact_hashtag_matches = [
            item
            for item in eligible
            if hashtag_pattern.search(f"{item.title}\n{item.description}")
        ]
        if exact_hashtag_matches:
            eligible = exact_hashtag_matches
        if not eligible:
            return [], search_payload.get("nextPageToken"), search_window_start

        max_log_views = max(math.log1p(item.views) for item in eligible) or 1
        total_ranked = max(len(eligible), 1)
        scored: list[AutomationCandidateRead] = []
        now = datetime.now(timezone.utc)
        for index, item in enumerate(eligible):
            api_relevance = 1 - (index / total_ranked)
            popularity = math.log1p(item.views) / max_log_views
            age_days = (
                max((now - item.published_at).total_seconds(), 0) / 86_400
                if item.published_at
                else 7
            )
            recency = max(0.0, 1 - min(age_days, 7) / 7)
            score = min(1.0, 0.55 * api_relevance + 0.35 * popularity + 0.10 * recency)
            scored.append(item.model_copy(update={"relevance_score": round(score, 4)}))

        if sort_by == "recent":
            scored.sort(
                key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
        elif sort_by == "views":
            scored.sort(key=lambda item: item.views, reverse=True)
        else:
            scored.sort(key=lambda item: item.relevance_score, reverse=True)
        return scored[:limit], search_payload.get("nextPageToken"), search_window_start

    async def get_automation_video_details(
        self, video_ids: Iterable[str]
    ) -> list[AutomationCandidateRead]:
        unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))[:50]
        if not unique_ids:
            return []
        payload = await self._request(
            self.videos_endpoint,
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(unique_ids),
                "fields": (
                    "items(id,snippet(title,description,channelTitle,publishedAt,thumbnails),"
                    "statistics(viewCount),contentDetails(duration))"
                ),
            },
        )
        results: list[AutomationCandidateRead] = []
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            content_details = item.get("contentDetails", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
            )
            try:
                views = int(statistics.get("viewCount", 0))
            except (TypeError, ValueError):
                views = 0
            duration = self._parse_iso_duration(content_details.get("duration"))
            if not duration:
                continue
            video_id = str(item.get("id", ""))
            if not video_id:
                continue
            results.append(
                AutomationCandidateRead(
                    video_id=video_id,
                    title=snippet.get("title", "Vídeo sem título"),
                    description=snippet.get("description", ""),
                    channel_title=snippet.get("channelTitle", "Canal desconhecido"),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    thumbnail_url=thumbnail.get("url") if thumbnail else None,
                    published_at=self._parse_datetime(snippet.get("publishedAt")),
                    views=views,
                    duration_seconds=duration,
                    relevance_score=0,
                )
            )
        return results

    async def _request(
        self,
        endpoint: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        if not settings.youtube_api_key:
            raise YoutubeConfigurationError(
                "Configure YOUTUBE_API_KEY no backend para consultar o YouTube."
            )

        request_parameters = {"key": settings.youtube_api_key, **parameters}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(endpoint, params=request_parameters)
        except httpx.HTTPError as error:
            raise YoutubeRequestError("Não foi possível conectar à API do YouTube.") from error

        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message")
            except ValueError:
                detail = None
            raise YoutubeRequestError(detail or "Erro ao consultar o YouTube.")

        try:
            return response.json()
        except ValueError as error:
            raise YoutubeRequestError("O YouTube retornou uma resposta inválida.") from error

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _parse_iso_duration(value: str | None) -> float:
        if not value:
            return 0
        match = re.fullmatch(
            r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
            r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>[\d.]+)S)?",
            value,
        )
        if not match:
            return 0
        parts = {name: float(number or 0) for name, number in match.groupdict().items()}
        return (
            parts["days"] * 86_400
            + parts["hours"] * 3_600
            + parts["minutes"] * 60
            + parts["seconds"]
        )
