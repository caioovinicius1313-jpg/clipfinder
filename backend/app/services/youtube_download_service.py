import re
import subprocess
import sys

from app.core.config import settings


class YoutubeDownloadError(Exception):
    """Raised when yt-dlp cannot extract a temporary media URL."""


class YoutubeDownloadService:
    extractor_args = "youtube:player_client=default,web_embedded"
    video_id_pattern = re.compile(r"^[A-Za-z0-9_-]{6,100}$")

    def get_temporary_download_url(self, video_id: str) -> str:
        if not self.video_id_pattern.fullmatch(video_id):
            raise YoutubeDownloadError("Identificador de vídeo inválido.")

        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-config",
            "--no-playlist",
            "--no-warnings",
            "--get-url",
            "--format",
            "best[ext=mp4]/best",
            "--extractor-args",
            self.extractor_args,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.yt_dlp_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise YoutubeDownloadError(
                "O YouTube demorou demais para gerar o link. Tente novamente."
            ) from error
        except OSError as error:
            raise YoutubeDownloadError("O yt-dlp não está disponível no servidor.") from error

        direct_urls = [
            line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")
        ]
        if result.returncode != 0 or not direct_urls:
            detail = result.stderr.strip().splitlines()
            message = detail[-1] if detail else "O YouTube não disponibilizou o arquivo."
            raise YoutubeDownloadError(message[-600:])
        return direct_urls[0]
