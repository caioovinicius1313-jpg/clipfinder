import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import imageio_ffmpeg
import numpy as np

from app.core.config import settings


logger = logging.getLogger(__name__)


class MediaProcessingError(Exception):
    """Raised when a media file cannot be read or generated."""


class AudioMatchNotFoundError(MediaProcessingError):
    """Raised when the reference audio does not sufficiently match the source audio."""


@dataclass(frozen=True)
class AudioSegmentMatch:
    source_start_seconds: float
    reference_start_seconds: float
    duration_seconds: float
    score: float


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float | None
    has_video: bool
    has_audio: bool


class MediaProcessingService:
    sample_rate = 8_000
    envelope_rate = 50

    def get_ffmpeg_executable(self) -> str:
        if settings.ffmpeg_path:
            configured_path = Path(settings.ffmpeg_path)
            if configured_path.is_file():
                return str(configured_path)

        installed_path = shutil.which("ffmpeg")
        if installed_path:
            return installed_path

        return imageio_ffmpeg.get_ffmpeg_exe()

    def probe(self, file_path: str | Path) -> MediaProbe:
        command = [self.get_ffmpeg_executable(), "-hide_banner", "-i", str(file_path)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        duration_seconds: float | None = None
        if duration_match is not None:
            hours, minutes, seconds = duration_match.groups()
            duration_seconds = int(hours) * 3_600 + int(minutes) * 60 + float(seconds)

        return MediaProbe(
            duration_seconds=duration_seconds,
            has_video=bool(re.search(r"Stream #[^\r\n]+:\s*Video:", result.stderr)),
            has_audio=bool(re.search(r"Stream #[^\r\n]+:\s*Audio:", result.stderr)),
        )

    def probe_duration(self, file_path: str | Path) -> float | None:
        return self.probe(file_path).duration_seconds

    def extract_audio_samples(self, file_path: str | Path, *, sample_rate: int) -> np.ndarray:
        command = [
            self.get_ffmpeg_executable(),
            "-v",
            "error",
            "-i",
            str(file_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "pipe:1",
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            missing_audio_markers = (
                "matches no streams",
                "Output file does not contain any stream",
            )
            if any(marker in detail for marker in missing_audio_markers):
                raise MediaProcessingError(
                    f'O arquivo "{self._media_name(file_path)}" não possui uma faixa de áudio. '
                    "Envie um vídeo original que contenha imagem e áudio."
                )
            raise MediaProcessingError(f"Não foi possível ler o áudio do vídeo: {detail}")

        samples = np.frombuffer(result.stdout, dtype=np.float32)
        if samples.size < sample_rate * 3:
            raise MediaProcessingError("O vídeo precisa ter pelo menos três segundos de áudio.")

        return samples

    def extract_audio_envelope(self, file_path: str | Path) -> np.ndarray:
        samples = self.extract_audio_samples(file_path, sample_rate=self.sample_rate)
        frame_size = self.sample_rate // self.envelope_rate
        usable_samples = samples.size - (samples.size % frame_size)

        frames = samples[:usable_samples].reshape(-1, frame_size)
        return np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1)).astype(np.float64)

    def generate_clip(
        self,
        *,
        source_path: str | Path,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> str:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.get_ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-filter_threads",
            "1",
            "-threads",
            "1",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-crf",
            "18",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode == -9:
            logger.warning(
                "O encoder excedeu a memória; repetindo o corte sem recodificação."
            )
            output_path.unlink(missing_ok=True)
            self._generate_clip_by_stream_copy(
                source_path=source_path,
                output_path=output_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
            )
            return "copied"
        if result.returncode != 0:
            detail = self._ffmpeg_error_detail(result)
            raise MediaProcessingError(f"Não foi possível gerar o corte: {detail}")
        return "encoded"

    def _generate_clip_by_stream_copy(
        self,
        *,
        source_path: str | Path,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        command = [
            self.get_ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            detail = self._ffmpeg_error_detail(result)
            raise MediaProcessingError(
                f"Não foi possível gerar o corte no modo de baixo consumo: {detail}"
            )

    def generate_montage(
        self,
        *,
        source_path: str | Path,
        output_path: Path,
        segments: list[tuple[float, float]],
    ) -> None:
        if not segments:
            raise MediaProcessingError("Nenhum trecho foi encontrado para montar o corte.")

        if len(segments) == 1:
            start_seconds, duration_seconds = segments[0]
            self.generate_clip(
                source_path=source_path,
                output_path=output_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
            )
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="clipfinder-") as temporary_directory:
            temporary_path = Path(temporary_directory)
            part_paths: list[Path] = []
            part_modes: list[str] = []
            for index, (start_seconds, duration_seconds) in enumerate(segments):
                part_path = temporary_path / f"part-{index:03d}.mp4"
                part_modes.append(
                    self.generate_clip(
                        source_path=source_path,
                        output_path=part_path,
                        start_seconds=start_seconds,
                        duration_seconds=duration_seconds,
                    )
                )
                part_paths.append(part_path)

            if len(set(part_modes)) > 1:
                for part_path, (start_seconds, duration_seconds) in zip(
                    part_paths, segments, strict=True
                ):
                    part_path.unlink(missing_ok=True)
                    self._generate_clip_by_stream_copy(
                        source_path=source_path,
                        output_path=part_path,
                        start_seconds=start_seconds,
                        duration_seconds=duration_seconds,
                    )

            manifest_path = temporary_path / "parts.txt"
            manifest_path.write_text(
                "\n".join(f"file '{part_path.as_posix()}'" for part_path in part_paths),
                encoding="utf-8",
            )
            command = [
                self.get_ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode != 0:
                detail = self._ffmpeg_error_detail(result)
                raise MediaProcessingError(f"Não foi possível montar o corte: {detail}")

    @staticmethod
    def _ffmpeg_error_detail(result: subprocess.CompletedProcess[bytes]) -> str:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        detail = re.sub(
            r"(https?://[^?\s]+)\?[^\s'\"]+",
            r"\1?<assinatura-oculta>",
            detail,
        )
        if result.returncode < 0:
            resource_message = (
                f"o processo foi encerrado pelo servidor (sinal {-result.returncode}). "
                "Verifique a memória disponível no Railway."
            )
            return f"{resource_message} Detalhes: {detail}" if detail else resource_message
        if detail:
            return detail
        return f"o FFmpeg terminou com o código {result.returncode}, sem fornecer detalhes."

    @staticmethod
    def _media_name(file_path: str | Path) -> str:
        value = str(file_path)
        parsed_path = urlsplit(value).path if "://" in value else value
        return Path(parsed_path).name or "vídeo"


class AudioMatchingService:
    minimum_score = 0.45
    minimum_segment_score = 0.45
    segment_seconds = 8
    minimum_segment_seconds = 3
    refinement_sample_rate = 2_000
    refinement_search_seconds = 3
    minimum_refinement_score = 0.15

    def __init__(self, media_service: MediaProcessingService | None = None) -> None:
        self.media_service = media_service or MediaProcessingService()

    def find_match(
        self, source_path: str | Path, reference_path: str | Path
    ) -> tuple[float, float, float]:
        source_envelope = self.media_service.extract_audio_envelope(source_path)
        reference_envelope = self.media_service.extract_audio_envelope(reference_path)
        start_frame, score = self.find_match_in_envelopes(source_envelope, reference_envelope)
        duration_seconds = reference_envelope.size / self.media_service.envelope_rate
        return start_frame / self.media_service.envelope_rate, duration_seconds, score

    def find_segmented_matches(
        self, source_path: str | Path, reference_path: str | Path
    ) -> list[AudioSegmentMatch]:
        source_envelope = self.media_service.extract_audio_envelope(source_path)
        reference_envelope = self.media_service.extract_audio_envelope(reference_path)
        matches = self.find_segmented_matches_in_envelopes(source_envelope, reference_envelope)

        source_samples = self.media_service.extract_audio_samples(
            source_path, sample_rate=self.refinement_sample_rate
        )
        reference_samples = self.media_service.extract_audio_samples(
            reference_path, sample_rate=self.refinement_sample_rate
        )
        return [
            self._refine_segment_match(match, source_samples, reference_samples)
            for match in matches
        ]

    def find_match_in_envelopes(
        self, source_envelope: np.ndarray, reference_envelope: np.ndarray
    ) -> tuple[int, float]:
        if reference_envelope.size >= source_envelope.size:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        best_frame, best_score = self._find_best_match_in_envelopes(
            source_envelope, reference_envelope
        )
        if best_score < self.minimum_score:
            raise AudioMatchNotFoundError(
                f"Nenhum trecho com áudio suficientemente parecido foi encontrado (confiança {best_score:.0%})."
            )

        return best_frame, best_score

    def find_segmented_matches_in_envelopes(
        self, source_envelope: np.ndarray, reference_envelope: np.ndarray
    ) -> list[AudioSegmentMatch]:
        if reference_envelope.size >= source_envelope.size:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        segment_frames = self.segment_seconds * self.media_service.envelope_rate
        minimum_frames = self.minimum_segment_seconds * self.media_service.envelope_rate
        candidates: list[AudioSegmentMatch] = []

        for reference_start_frame in range(0, reference_envelope.size, segment_frames):
            reference_segment = reference_envelope[
                reference_start_frame : reference_start_frame + segment_frames
            ]
            if reference_segment.size < minimum_frames:
                continue

            source_start_frame, score = self._find_best_match_in_envelopes(
                source_envelope, reference_segment
            )
            if score >= self.minimum_segment_score:
                candidates.append(
                    AudioSegmentMatch(
                        source_start_seconds=source_start_frame / self.media_service.envelope_rate,
                        reference_start_seconds=reference_start_frame / self.media_service.envelope_rate,
                        duration_seconds=reference_segment.size / self.media_service.envelope_rate,
                        score=score,
                    )
                )

        selected_matches = self._select_chronological_matches(candidates)
        if not selected_matches:
            raise AudioMatchNotFoundError(
                "Nenhum bloco de áudio suficientemente parecido foi encontrado na referência."
            )

        return selected_matches

    def _find_best_match_in_envelopes(
        self, source_envelope: np.ndarray, reference_envelope: np.ndarray
    ) -> tuple[int, float]:
        source = self._normalize(source_envelope)
        reference = self._normalize(reference_envelope)
        reference_energy = float(np.dot(reference, reference))
        if reference_energy <= 0:
            raise MediaProcessingError("O áudio do vídeo de referência está silencioso.")

        correlation_size = source.size + reference.size - 1
        fft_size = 1 << (correlation_size - 1).bit_length()
        correlation = np.fft.irfft(
            np.fft.rfft(source, fft_size) * np.fft.rfft(reference[::-1], fft_size), fft_size
        )
        valid_correlation = correlation[reference.size - 1 : source.size]

        cumulative_energy = np.concatenate(([0.0], np.cumsum(np.square(source))))
        source_energy = cumulative_energy[reference.size :] - cumulative_energy[: -reference.size]
        denominator = np.sqrt(np.maximum(source_energy * reference_energy, 1e-12))
        scores = valid_correlation / denominator
        best_frame = int(np.argmax(scores))
        best_score = float(scores[best_frame])

        return best_frame, best_score

    def _refine_segment_match(
        self,
        match: AudioSegmentMatch,
        source_samples: np.ndarray,
        reference_samples: np.ndarray,
    ) -> AudioSegmentMatch:
        sample_rate = self.refinement_sample_rate
        reference_start = int(match.reference_start_seconds * sample_rate)
        reference_end = int((match.reference_start_seconds + match.duration_seconds) * sample_rate)
        reference_segment = reference_samples[reference_start:reference_end]
        if reference_segment.size < sample_rate * self.minimum_segment_seconds:
            return match

        padding = self.refinement_search_seconds * sample_rate
        predicted_start = int(match.source_start_seconds * sample_rate)
        search_start = max(0, predicted_start - padding)
        search_end = min(source_samples.size, predicted_start + reference_segment.size + padding)
        source_search_region = source_samples[search_start:search_end]
        if source_search_region.size <= reference_segment.size:
            return match

        relative_start, refinement_score = self._find_best_match_in_samples(
            source_search_region, reference_segment
        )
        if refinement_score < self.minimum_refinement_score:
            return match

        refined_start = (search_start + relative_start) / sample_rate
        combined_score = (match.score * 0.7) + (refinement_score * 0.3)
        return replace(match, source_start_seconds=refined_start, score=combined_score)

    @staticmethod
    def _find_best_match_in_samples(
        source_samples: np.ndarray, reference_samples: np.ndarray
    ) -> tuple[int, float]:
        source = AudioMatchingService._normalize(source_samples)
        reference = AudioMatchingService._normalize(reference_samples)
        reference_energy = float(np.dot(reference, reference))
        if reference_energy <= 0:
            return 0, 0.0

        correlation_size = source.size + reference.size - 1
        fft_size = 1 << (correlation_size - 1).bit_length()
        correlation = np.fft.irfft(
            np.fft.rfft(source, fft_size) * np.fft.rfft(reference[::-1], fft_size), fft_size
        )
        valid_correlation = correlation[reference.size - 1 : source.size]
        cumulative_energy = np.concatenate(([0.0], np.cumsum(np.square(source))))
        source_energy = cumulative_energy[reference.size :] - cumulative_energy[: -reference.size]
        scores = valid_correlation / np.sqrt(np.maximum(source_energy * reference_energy, 1e-12))
        best_sample = int(np.argmax(scores))
        return best_sample, float(scores[best_sample])

    @staticmethod
    def _select_chronological_matches(
        candidates: list[AudioSegmentMatch],
    ) -> list[AudioSegmentMatch]:
        if not candidates:
            return []

        best_weights = [candidate.score for candidate in candidates]
        previous_indexes: list[int | None] = [None] * len(candidates)
        for current_index, current in enumerate(candidates):
            for previous_index, previous in enumerate(candidates[:current_index]):
                if previous.source_start_seconds > current.source_start_seconds:
                    continue

                weight = best_weights[previous_index] + current.score
                if weight > best_weights[current_index]:
                    best_weights[current_index] = weight
                    previous_indexes[current_index] = previous_index

        index = int(np.argmax(best_weights))
        matches: list[AudioSegmentMatch] = []
        while True:
            matches.append(candidates[index])
            previous_index = previous_indexes[index]
            if previous_index is None:
                break
            index = previous_index

        matches.reverse()
        return matches

    @staticmethod
    def _normalize(values: np.ndarray) -> np.ndarray:
        centered = values - np.mean(values)
        standard_deviation = float(np.std(centered))
        if standard_deviation <= 1e-12:
            return np.zeros_like(centered)
        return centered / standard_deviation
