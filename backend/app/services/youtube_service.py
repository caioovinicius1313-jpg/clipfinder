from datetime import datetime

import httpx

from app.core.config import settings
from app.schemas.youtube import YoutubeSearchResult


class YoutubeConfigurationError(Exception):
    """Raised when the YouTube Data API key is not configured."""


class YoutubeRequestError(Exception):
    """Raised when YouTube rejects a request."""


class YoutubeService:
    endpoint = "https://www.googleapis.com/youtube/v3/search"

    async def search(self, query: str, limit: int = 10) -> list[YoutubeSearchResult]:
        if not settings.youtube_api_key:
            raise YoutubeConfigurationError(
                "Configure YOUTUBE_API_KEY no arquivo backend/.env para pesquisar no YouTube."
            )

        parameters = {
            "key": settings.youtube_api_key,
            "part": "snippet",
            "type": "video",
            "q": query,
            "maxResults": min(max(limit, 1), 25),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(self.endpoint, params=parameters)

        if response.is_error:
            detail = response.json().get("error", {}).get("message", "Erro ao consultar o YouTube.")
            raise YoutubeRequestError(detail)

        results: list[YoutubeSearchResult] = []
        for item in response.json().get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id:
                continue

            thumbnails = snippet.get("thumbnails", {})
            thumbnail = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default")
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

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
