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
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run_precise_clip_encoding(
            source_path=source_path,
            output_path=output_path,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            video_filter="pad=ceil(iw/2)*2:ceil(ih/2)*2",
            crf="18",
        )
        if result.returncode == -9:
            logger.warning(
                "A codificação na resolução original excedeu a memória; "
                "repetindo com resolução máxima de 1080p."
            )
            output_path.unlink(missing_ok=True)
            result = self._run_precise_clip_encoding(
                source_path=source_path,
                output_path=output_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
                video_filter=(
                    "scale=1920:1920:force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2"
                ),
                crf="17",
            )
        if result.returncode != 0:
            detail = self._ffmpeg_error_detail(result)
            raise MediaProcessingError(f"Não foi possível gerar o corte: {detail}")

    def _run_precise_clip_encoding(
        self,
        *,
        source_path: str | Path,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
        video_filter: str,
        crf: str,
    ) -> subprocess.CompletedProcess[bytes]:
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
            crf,
            "-vf",
            video_filter,
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
        return subprocess.run(command, capture_output=True, check=False)

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
            for index, (start_seconds, duration_seconds) in enumerate(segments):
                part_path = temporary_path / f"part-{index:03d}.mp4"
                self.generate_clip(
                    source_path=source_path,
                    output_path=part_path,
                    start_seconds=start_seconds,
                    duration_seconds=duration_seconds,
                )
                part_paths.append(part_path)

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
    analysis_sample_rate = 4_000
    feature_rate = 20
    refinement_sample_rate = analysis_sample_rate
    refinement_search_seconds = 3
    minimum_refinement_score = 0.15
    minimum_reference_coverage = 0.5
    spectral_band_edges = (80, 160, 320, 640, 1_000, 1_400, 1_900)

    def __init__(self, media_service: MediaProcessingService | None = None) -> None:
        self.media_service = media_service or MediaProcessingService()

    def find_match(
        self, source_path: str | Path, reference_path: str | Path
    ) -> tuple[float, float, float]:
        source_samples = self.media_service.extract_audio_samples(
            source_path, sample_rate=self.analysis_sample_rate
        )
        reference_samples = self.media_service.extract_audio_samples(
            reference_path, sample_rate=self.analysis_sample_rate
        )
        source_features = self._spectral_features_from_samples(source_samples)
        reference_features = self._spectral_features_from_samples(reference_samples)
        start_frame, score = self.find_match_in_features(source_features, reference_features)
        duration_seconds = reference_samples.size / self.analysis_sample_rate
        match = AudioSegmentMatch(
            source_start_seconds=start_frame / self.feature_rate,
            reference_start_seconds=0,
            duration_seconds=duration_seconds,
            score=score,
        )
        refined_match = self._refine_segment_match(
            match,
            source_samples,
            reference_samples,
            require_score=True,
        )
        return (
            refined_match.source_start_seconds,
            duration_seconds,
            refined_match.score,
        )

    def find_segmented_matches(
        self, source_path: str | Path, reference_path: str | Path
    ) -> list[AudioSegmentMatch]:
        source_samples = self.media_service.extract_audio_samples(
            source_path, sample_rate=self.analysis_sample_rate
        )
        reference_samples = self.media_service.extract_audio_samples(
            reference_path, sample_rate=self.analysis_sample_rate
        )
        source_features = self._spectral_features_from_samples(source_samples)
        reference_features = self._spectral_features_from_samples(reference_samples)
        matches = self.find_segmented_matches_in_features(
            source_features, reference_features
        )

        refined_matches: list[AudioSegmentMatch] = []
        for match in matches:
            try:
                refined_matches.append(
                    self._refine_segment_match(
                        match,
                        source_samples,
                        reference_samples,
                        require_score=True,
                    )
                )
            except AudioMatchNotFoundError:
                continue

        reference_duration = reference_samples.size / self.analysis_sample_rate
        self._validate_reference_coverage(refined_matches, reference_duration)
        return refined_matches

    def find_match_in_features(
        self, source_features: np.ndarray, reference_features: np.ndarray
    ) -> tuple[int, float]:
        if reference_features.shape[0] >= source_features.shape[0]:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        best_frame, best_score = self._find_best_match_in_features(
            source_features, reference_features
        )
        if best_score < self.minimum_score:
            raise AudioMatchNotFoundError(
                "Nenhum trecho com assinatura de áudio suficientemente parecida foi encontrado "
                f"(confiança {best_score:.0%})."
            )
        return best_frame, best_score

    def find_segmented_matches_in_features(
        self, source_features: np.ndarray, reference_features: np.ndarray
    ) -> list[AudioSegmentMatch]:
        if reference_features.shape[0] >= source_features.shape[0]:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        segment_frames = self.segment_seconds * self.feature_rate
        minimum_frames = self.minimum_segment_seconds * self.feature_rate
        candidates: list[AudioSegmentMatch] = []
        for reference_start_frame in range(0, reference_features.shape[0], segment_frames):
            reference_segment = reference_features[
                reference_start_frame : reference_start_frame + segment_frames
            ]
            if reference_segment.shape[0] < minimum_frames:
                continue

            source_start_frame, score = self._find_best_match_in_features(
                source_features, reference_segment
            )
            if score >= self.minimum_segment_score:
                candidates.append(
                    AudioSegmentMatch(
                        source_start_seconds=source_start_frame / self.feature_rate,
                        reference_start_seconds=reference_start_frame / self.feature_rate,
                        duration_seconds=reference_segment.shape[0] / self.feature_rate,
                        score=score,
                    )
                )

        selected_matches = self._select_chronological_matches(candidates)
        reference_duration = reference_features.shape[0] / self.feature_rate
        self._validate_reference_coverage(selected_matches, reference_duration)
        return selected_matches

    def _spectral_features_from_samples(self, samples: np.ndarray) -> np.ndarray:
        frame_size = self.analysis_sample_rate // self.feature_rate
        usable_samples = samples.size - (samples.size % frame_size)
        if usable_samples < frame_size * self.minimum_segment_seconds * self.feature_rate:
            raise MediaProcessingError("O vídeo precisa ter pelo menos três segundos de áudio.")

        frames = samples[:usable_samples].reshape(-1, frame_size)
        window = np.hanning(frame_size).astype(np.float32)
        frequencies = np.fft.rfftfreq(frame_size, d=1 / self.analysis_sample_rate)
        band_masks = [
            (frequencies >= lower_frequency) & (frequencies < upper_frequency)
            for lower_frequency, upper_frequency in zip(
                self.spectral_band_edges[:-1],
                self.spectral_band_edges[1:],
                strict=True,
            )
        ]
        features = np.empty((frames.shape[0], len(band_masks)), dtype=np.float32)
        chunk_size = 2_048
        for chunk_start in range(0, frames.shape[0], chunk_size):
            chunk_end = min(chunk_start + chunk_size, frames.shape[0])
            spectrum = np.fft.rfft(frames[chunk_start:chunk_end] * window, axis=1)
            power = np.square(np.abs(spectrum))
            for band_index, band_mask in enumerate(band_masks):
                features[chunk_start:chunk_end, band_index] = np.mean(
                    power[:, band_mask], axis=1
                )

        log_features = np.log1p(features)
        spectral_changes = np.diff(log_features, axis=0, prepend=log_features[:1])
        return np.concatenate((log_features, spectral_changes), axis=1)

    @staticmethod
    def _find_best_match_in_features(
        source_features: np.ndarray, reference_features: np.ndarray
    ) -> tuple[int, float]:
        source = AudioMatchingService._normalize_columns(source_features)
        reference = AudioMatchingService._normalize_columns(reference_features)
        reference_energy = float(np.sum(np.square(reference)))
        if reference_energy <= 0:
            raise MediaProcessingError("O áudio do vídeo de referência está silencioso.")

        correlation_size = source.shape[0] + reference.shape[0] - 1
        fft_size = 1 << (correlation_size - 1).bit_length()
        source_spectrum = np.fft.rfft(source, fft_size, axis=0)
        reference_spectrum = np.fft.rfft(reference[::-1], fft_size, axis=0)
        correlation = np.fft.irfft(
            np.sum(source_spectrum * reference_spectrum, axis=1), fft_size
        )
        valid_correlation = correlation[reference.shape[0] - 1 : source.shape[0]]

        frame_energy = np.sum(np.square(source), axis=1)
        cumulative_energy = np.concatenate(([0.0], np.cumsum(frame_energy)))
        source_energy = (
            cumulative_energy[reference.shape[0] :]
            - cumulative_energy[: -reference.shape[0]]
        )
        scores = valid_correlation / np.sqrt(
            np.maximum(source_energy * reference_energy, 1e-12)
        )
        best_frame = int(np.argmax(scores))
        return best_frame, float(scores[best_frame])

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
        *,
        require_score: bool = False,
    ) -> AudioSegmentMatch:
        sample_rate = self.refinement_sample_rate
        reference_start = int(match.reference_start_seconds * sample_rate)
        reference_end = int((match.reference_start_seconds + match.duration_seconds) * sample_rate)
        reference_segment = reference_samples[reference_start:reference_end]
        if reference_segment.size < sample_rate * self.minimum_segment_seconds:
            if require_score:
                raise AudioMatchNotFoundError("O trecho é curto demais para confirmar a correspondência.")
            return match

        padding = self.refinement_search_seconds * sample_rate
        predicted_start = int(match.source_start_seconds * sample_rate)
        search_start = max(0, predicted_start - padding)
        search_end = min(source_samples.size, predicted_start + reference_segment.size + padding)
        source_search_region = source_samples[search_start:search_end]
        if source_search_region.size <= reference_segment.size:
            if require_score:
                raise AudioMatchNotFoundError("Não há áudio suficiente para confirmar o trecho encontrado.")
            return match

        relative_start, refinement_score = self._find_best_match_in_samples(
            source_search_region, reference_segment
        )
        if refinement_score < self.minimum_refinement_score:
            if require_score:
                raise AudioMatchNotFoundError(
                    "A assinatura espectral parece semelhante, mas a forma de onda não confirmou "
                    "o trecho com segurança."
                )
            return match

        refined_start = (search_start + relative_start) / sample_rate
        combined_score = (match.score * 0.7) + (refinement_score * 0.3)
        return replace(match, source_start_seconds=refined_start, score=combined_score)

    def _validate_reference_coverage(
        self,
        matches: list[AudioSegmentMatch],
        reference_duration: float,
    ) -> None:
        covered_duration = sum(match.duration_seconds for match in matches)
        required_duration = min(
            reference_duration,
            max(
                float(self.minimum_segment_seconds),
                reference_duration * self.minimum_reference_coverage,
            ),
        )
        if covered_duration < required_duration:
            coverage = covered_duration / reference_duration if reference_duration else 0
            raise AudioMatchNotFoundError(
                "Poucos trechos da referência foram confirmados no vídeo original "
                f"(cobertura {coverage:.0%})."
            )

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
                previous_end = previous.source_start_seconds + previous.duration_seconds
                if previous_end > current.source_start_seconds + 0.25:
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

    @staticmethod
    def _normalize_columns(values: np.ndarray) -> np.ndarray:
        centered = values - np.mean(values, axis=0, keepdims=True)
        standard_deviation = np.std(centered, axis=0, keepdims=True)
        return centered / np.maximum(standard_deviation, 1e-12)
