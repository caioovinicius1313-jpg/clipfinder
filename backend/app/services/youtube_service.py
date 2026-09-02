from collections.abc import Iterable
from datetime import datetime

import httpx

from app.core.config import settings
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
