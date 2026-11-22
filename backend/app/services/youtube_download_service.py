import base64
import binascii
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.core.config import settings


class YoutubeDownloadError(Exception):
    """Raised when yt-dlp cannot extract a temporary media URL."""


class YoutubeDownloadService:
    extractor_args = "youtube:player_client=default,web_embedded"
    video_id_pattern = re.compile(r"^[A-Za-z0-9_-]{6,100}$")

    def extract_video_id(self, value: str) -> str:
        candidate = value.strip()
        if self.video_id_pattern.fullmatch(candidate):
            return candidate
        try:
            parsed = urlparse(candidate)
        except ValueError as error:
            raise YoutubeDownloadError("Link do vídeo original inválido.") from error
        hostname = (parsed.hostname or "").lower()
        video_id: str | None = None
        if hostname in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.strip("/").split("/")[0]
        elif hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                video_id = (parse_qs(parsed.query).get("v") or [None])[0]
            elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
                parts = parsed.path.strip("/").split("/")
                video_id = parts[1] if len(parts) > 1 else None
        if not video_id or not self.video_id_pattern.fullmatch(video_id):
            raise YoutubeDownloadError(
                "Use um link válido do YouTube para o vídeo original."
            )
        return video_id

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

    def download_video(
        self,
        video_id: str,
        destination_directory: Path,
        *,
        filename_stem: str,
        reference_quality: bool = False,
    ) -> Path:
        if not self.video_id_pattern.fullmatch(video_id):
            raise YoutubeDownloadError("Identificador de vídeo inválido.")
        destination_directory.mkdir(parents=True, exist_ok=True)
        output_template = destination_directory / f"{filename_stem}.%(ext)s"
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-config",
            "--no-playlist",
            "--no-warnings",
            "--format",
            (
                "worst[height<=480]/worst"
                if reference_quality
                else "bestvideo*+bestaudio/best"
            ),
            "--merge-output-format",
            "mp4",
            "--extractor-args",
            self.extractor_args,
            "--retries",
            "3",
            "--extractor-retries",
            "3",
            "--sleep-requests",
            "0.75",
            "--output",
            str(output_template),
            "--print",
            "after_move:filepath",
        ]
        if settings.ffmpeg_path:
            command.extend(["--ffmpeg-location", settings.ffmpeg_path])
        self._add_authentication(command, destination_directory)
        self._add_javascript_runtime(command)
        command.append(f"https://www.youtube.com/watch?v={video_id}")
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.youtube_download_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise YoutubeDownloadError(
                "O download do YouTube excedeu o tempo máximo configurado."
            ) from error
        except OSError as error:
            raise YoutubeDownloadError("O yt-dlp não está disponível no servidor.") from error

        printed_paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        downloaded_path = next((path for path in reversed(printed_paths) if path.is_file()), None)
        if result.returncode != 0 or downloaded_path is None:
            self._raise_download_error(result.stderr)
        return downloaded_path

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
        self._add_authentication(command, temporary_directory)
        self._add_javascript_runtime(command)
        command.append(f"https://www.youtube.com/watch?v={video_id}")
        return command

    def _add_authentication(self, command: list[str], temporary_directory: Path) -> None:
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
        elif settings.youtube_cookies_file:
            cookies_path = Path(settings.youtube_cookies_file).expanduser().resolve()
            if not cookies_path.is_file():
                raise YoutubeDownloadError(
                    "YOUTUBE_COOKIES_FILE aponta para um arquivo que não existe."
                )
            command.extend(["--cookies", str(cookies_path)])
        elif settings.youtube_cookies_from_browser:
            browser_source = settings.youtube_cookies_from_browser.strip()
            browser = browser_source.partition(":")[0].lower()
            supported = {
                "brave",
                "chrome",
                "chromium",
                "edge",
                "firefox",
                "opera",
                "safari",
                "vivaldi",
                "whale",
            }
            if browser not in supported:
                raise YoutubeDownloadError(
                    "YOUTUBE_COOKIES_FROM_BROWSER deve indicar um navegador suportado, "
                    "como edge ou chrome."
                )
            command.extend(["--cookies-from-browser", browser_source])
        if settings.youtube_user_agent:
            command.extend(["--user-agent", settings.youtube_user_agent])

    @staticmethod
    def _add_javascript_runtime(command: list[str]) -> None:
        node_path = shutil.which("node")
        if node_path:
            command.extend(
                [
                    "--js-runtimes",
                    f"node:{node_path}",
                    "--remote-components",
                    "ejs:github",
                ]
            )

    @staticmethod
    def _raise_download_error(stderr: str) -> None:
        detail = stderr.strip().splitlines()
        message = detail[-1] if detail else "O YouTube não disponibilizou o arquivo."
        bot_challenge = (
            "Sign in to confirm you’re not a bot" in message
            or "Sign in to confirm you're not a bot" in message
        )
        if bot_challenge:
            if settings.youtube_cookies_base64:
                raise YoutubeDownloadError(
                    "O YouTube recusou os cookies configurados. Exporte cookies novos e atualize "
                    "YOUTUBE_COOKIES_BASE64."
                )
            raise YoutubeDownloadError(
                "O YouTube bloqueou o servidor contra automação. Configure "
                "YOUTUBE_COOKIES_BASE64 para autorizar os downloads."
            )
        raise YoutubeDownloadError(message[-600:])
