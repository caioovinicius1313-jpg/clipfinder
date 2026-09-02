import base64
import binascii
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from app.core.config import settings


class YoutubeDownloadError(Exception):
    """Raised when yt-dlp cannot extract a temporary media URL."""


class YoutubeDownloadService:
    extractor_args = "youtube:player_client=default,web_embedded"
    video_id_pattern = re.compile(r"^[A-Za-z0-9_-]{6,100}$")

    def get_temporary_download_url(self, video_id: str) -> str:
        if not self.video_id_pattern.fullmatch(video_id):
            raise YoutubeDownloadError("Identificador de vídeo inválido.")

        with tempfile.TemporaryDirectory(prefix="clipfinder-youtube-") as temporary_directory:
            command = self._build_command(video_id, Path(temporary_directory))
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
            bot_challenge = (
                "Sign in to confirm you’re not a bot" in message
                or "Sign in to confirm you're not a bot" in message
            )
            if bot_challenge:
                if settings.youtube_cookies_base64:
                    raise YoutubeDownloadError(
                        "O YouTube recusou os cookies configurados. Exporte cookies novos e atualize "
                        "YOUTUBE_COOKIES_BASE64 no Railway."
                    )
                raise YoutubeDownloadError(
                    "O YouTube bloqueou o servidor contra automação. Configure "
                    "YOUTUBE_COOKIES_BASE64 no Railway para autorizar os downloads."
                )
            raise YoutubeDownloadError(message[-600:])
        return direct_urls[0]

    def _build_command(self, video_id: str, temporary_directory: Path) -> list[str]:
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
        ]
        if settings.youtube_cookies_base64:
            cookies_path = temporary_directory / "youtube-cookies.txt"
            try:
                cookies = base64.b64decode(settings.youtube_cookies_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise YoutubeDownloadError(
                    "YOUTUBE_COOKIES_BASE64 não contém um arquivo de cookies válido."
                ) from error
            if not cookies.startswith((b"# Netscape HTTP Cookie File", b"# HTTP Cookie File")):
                raise YoutubeDownloadError(
                    "Os cookies do YouTube precisam estar no formato Netscape cookies.txt."
                )
            cookies_path.write_bytes(cookies.replace(b"\r\n", b"\n"))
            cookies_path.chmod(0o600)
            command.extend(["--cookies", str(cookies_path)])
        if settings.youtube_user_agent:
            command.extend(["--user-agent", settings.youtube_user_agent])
        command.append(f"https://www.youtube.com/watch?v={video_id}")
        return command
