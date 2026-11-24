import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
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
    reference_duration_seconds: float | None = None
    tempo_factor: float = 1.0


@dataclass
class FeatureSearchIndex:
    normalized_features: np.ndarray
    cumulative_energy: np.ndarray
    spectra: dict[int, np.ndarray] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioAnalysis:
    source_samples: np.ndarray
    reference_samples: np.ndarray
    source_features: np.ndarray
    reference_features: np.ndarray
    source_search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex] | None = None


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
    minimum_segment_score = 0.30
    segment_seconds = 8
    minimum_segment_seconds = 3
    analysis_sample_rate = 4_000
    feature_rate = 20
    refinement_sample_rate = analysis_sample_rate
    refinement_search_seconds = 3
    minimum_refinement_score = 0.15
    minimum_reference_coverage = 0.6
    dtw_search_padding_seconds = 3
    dtw_step_penalty = 0.05
    dtw_tempo_tolerance = 0.03
    analysis_window_seconds = 0.1
    mel_band_count = 16
    minimum_analysis_frequency = 80
    maximum_analysis_frequency = 1_900
    tempo_factors = (
        0.85,
        0.875,
        0.9,
        0.925,
        0.95,
        0.975,
        1.0,
        1.025,
        1.05,
        1.075,
        1.1,
        1.125,
        1.15,
    )

    def __init__(self, media_service: MediaProcessingService | None = None) -> None:
        self.media_service = media_service or MediaProcessingService()

    def find_match(
        self, source_path: str | Path, reference_path: str | Path
    ) -> tuple[float, float, float]:
        match = self._find_contiguous_match(self._load_analysis(source_path, reference_path))
        return match.source_start_seconds, match.duration_seconds, match.score

    def find_segmented_matches(
        self, source_path: str | Path, reference_path: str | Path
    ) -> list[AudioSegmentMatch]:
        return self._find_segmented_matches_from_analysis(
            self._load_analysis(source_path, reference_path)
        )

    def find_matches(
        self, source_path: str | Path, reference_path: str | Path
    ) -> list[AudioSegmentMatch]:
        analysis = self._load_analysis(source_path, reference_path)
        try:
            return [self._find_contiguous_match(analysis)]
        except AudioMatchNotFoundError:
            return self._find_segmented_matches_from_analysis(analysis)

    def _load_analysis(
        self, source_path: str | Path, reference_path: str | Path
    ) -> AudioAnalysis:
        source_samples = self.media_service.extract_audio_samples(
            source_path, sample_rate=self.analysis_sample_rate
        )
        reference_samples = self.media_service.extract_audio_samples(
            reference_path, sample_rate=self.analysis_sample_rate
        )
        source_features = self._spectral_features_from_samples(source_samples)
        reference_features = self._spectral_features_from_samples(reference_samples)
        return AudioAnalysis(
            source_samples=source_samples,
            reference_samples=reference_samples,
            source_features=source_features,
            reference_features=reference_features,
            source_search_indexes=self._build_feature_search_indexes(source_features),
        )

    def _find_contiguous_match(self, analysis: AudioAnalysis) -> AudioSegmentMatch:
        start_frame, score, tempo_factor = self._find_best_tempo_match(
            analysis.source_features,
            analysis.reference_features,
            search_indexes=analysis.source_search_indexes,
        )
        if score < self.minimum_score:
            raise AudioMatchNotFoundError(
                "Nenhum trecho com assinatura de áudio suficientemente parecida foi encontrado "
                f"(melhor confiança {score:.0%})."
            )
        reference_duration_seconds = analysis.reference_samples.size / self.analysis_sample_rate
        duration_seconds = reference_duration_seconds * tempo_factor
        match = AudioSegmentMatch(
            source_start_seconds=start_frame / self.feature_rate,
            reference_start_seconds=0,
            duration_seconds=duration_seconds,
            score=score,
            reference_duration_seconds=reference_duration_seconds,
            tempo_factor=tempo_factor,
        )
        dtw_match = self._refine_match_with_dtw(
            match,
            analysis.source_features,
            analysis.reference_features,
        )
        return self._refine_segment_match(
            dtw_match,
            analysis.source_samples,
            analysis.reference_samples,
        )

    def _find_segmented_matches_from_analysis(
        self, analysis: AudioAnalysis
    ) -> list[AudioSegmentMatch]:
        matches = self.find_segmented_matches_in_features(
            analysis.source_features,
            analysis.reference_features,
            search_indexes=analysis.source_search_indexes,
        )

        dtw_matches = [
            self._refine_match_with_dtw(
                match,
                analysis.source_features,
                analysis.reference_features,
            )
            for match in matches
        ]
        refined_matches = [
            self._refine_segment_match(
                match,
                analysis.source_samples,
                analysis.reference_samples,
            )
            for match in dtw_matches
        ]

        reference_duration = analysis.reference_samples.size / self.analysis_sample_rate
        self._validate_reference_coverage(refined_matches, reference_duration)
        return refined_matches

    def find_match_in_features(
        self, source_features: np.ndarray, reference_features: np.ndarray
    ) -> tuple[int, float]:
        if reference_features.shape[0] >= source_features.shape[0]:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        best_frame, best_score, _ = self._find_best_tempo_match(
            source_features, reference_features
        )
        if best_score < self.minimum_score:
            raise AudioMatchNotFoundError(
                "Nenhum trecho com assinatura de áudio suficientemente parecida foi encontrado "
                f"(confiança {best_score:.0%})."
            )
        return best_frame, best_score

    def find_segmented_matches_in_features(
        self,
        source_features: np.ndarray,
        reference_features: np.ndarray,
        *,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex] | None = None,
    ) -> list[AudioSegmentMatch]:
        if reference_features.shape[0] >= source_features.shape[0]:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        segment_frames = self.segment_seconds * self.feature_rate
        minimum_frames = self.minimum_segment_seconds * self.feature_rate
        candidates: list[AudioSegmentMatch] = []
        best_scores: list[float] = []
        indexes = search_indexes or self._build_feature_search_indexes(source_features)
        for reference_start_frame in range(0, reference_features.shape[0], segment_frames):
            reference_segment = reference_features[
                reference_start_frame : reference_start_frame + segment_frames
            ]
            if reference_segment.shape[0] < minimum_frames:
                continue

            source_start_frame, score, tempo_factor = self._find_best_tempo_match(
                source_features,
                reference_segment,
                search_indexes=indexes,
            )
            best_scores.append(score)
            if score >= self.minimum_segment_score:
                reference_duration_seconds = reference_segment.shape[0] / self.feature_rate
                candidates.append(
                    AudioSegmentMatch(
                        source_start_seconds=source_start_frame / self.feature_rate,
                        reference_start_seconds=reference_start_frame / self.feature_rate,
                        duration_seconds=reference_duration_seconds * tempo_factor,
                        score=score,
                        reference_duration_seconds=reference_duration_seconds,
                        tempo_factor=tempo_factor,
                    )
                )

        selected_matches = self._select_chronological_matches(candidates)
        reference_duration = reference_features.shape[0] / self.feature_rate
        self._validate_reference_coverage(
            selected_matches,
            reference_duration,
            best_score=max(best_scores, default=0.0),
        )
        return selected_matches

    def _find_best_tempo_match(
        self,
        source_features: np.ndarray,
        reference_features: np.ndarray,
        *,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex] | None = None,
    ) -> tuple[int, float, float]:
        indexes = search_indexes or self._build_feature_search_indexes(source_features)
        best_frame = 0
        best_score = float("-inf")
        best_tempo_factor = 1.0
        for tempo_factor in self.tempo_factors:
            adjusted_reference = self._resample_features(reference_features, tempo_factor)
            if adjusted_reference.shape[0] >= source_features.shape[0]:
                continue
            candidate_frame, candidate_score = self._find_best_match_with_indexes(
                indexes,
                adjusted_reference,
            )
            logger.debug(
                "Candidato de tempo %.3f: confiança %.4f no frame %s",
                tempo_factor,
                candidate_score,
                candidate_frame,
            )
            if candidate_score > best_score:
                best_frame = candidate_frame
                best_score = candidate_score
                best_tempo_factor = tempo_factor
        return best_frame, best_score, best_tempo_factor

    @staticmethod
    def _resample_features(features: np.ndarray, tempo_factor: float) -> np.ndarray:
        target_length = max(1, round(features.shape[0] * tempo_factor))
        if target_length == features.shape[0]:
            return features

        source_positions = np.arange(features.shape[0], dtype=np.float64)
        target_positions = np.linspace(0, features.shape[0] - 1, target_length)
        adjusted = np.empty((target_length, features.shape[1]), dtype=features.dtype)
        for column_index in range(features.shape[1]):
            adjusted[:, column_index] = np.interp(
                target_positions,
                source_positions,
                features[:, column_index],
            )
        return adjusted

    def _spectral_features_from_samples(self, samples: np.ndarray) -> np.ndarray:
        hop_size = self.analysis_sample_rate // self.feature_rate
        frame_size = round(self.analysis_sample_rate * self.analysis_window_seconds)
        minimum_samples = self.analysis_sample_rate * self.minimum_segment_seconds
        if samples.size < minimum_samples or samples.size < frame_size:
            raise MediaProcessingError("O vídeo precisa ter pelo menos três segundos de áudio.")

        frames = np.lib.stride_tricks.sliding_window_view(samples, frame_size)[::hop_size]
        window = np.hanning(frame_size).astype(np.float32)
        frequencies = np.fft.rfftfreq(frame_size, d=1 / self.analysis_sample_rate)
        mel_filters = self._mel_filter_bank(frequencies)
        features = np.empty((frames.shape[0], self.mel_band_count), dtype=np.float32)
        chunk_size = 2_048
        for chunk_start in range(0, frames.shape[0], chunk_size):
            chunk_end = min(chunk_start + chunk_size, frames.shape[0])
            spectrum = np.fft.rfft(frames[chunk_start:chunk_end] * window, axis=1)
            power = np.square(np.abs(spectrum))
            features[chunk_start:chunk_end] = power @ mel_filters.T

        log_features = np.log(np.maximum(features, 1e-10))
        if log_features.shape[0] >= 3:
            padded = np.pad(log_features, ((1, 1), (0, 0)), mode="edge")
            log_features = (
                (padded[:-2] * 0.25) + (padded[1:-1] * 0.5) + (padded[2:] * 0.25)
            )
        spectral_changes = np.diff(log_features, axis=0, prepend=log_features[:1])
        return np.concatenate((log_features, spectral_changes), axis=1).astype(np.float32)

    def _mel_filter_bank(self, frequencies: np.ndarray) -> np.ndarray:
        minimum_mel = self._hertz_to_mel(self.minimum_analysis_frequency)
        maximum_mel = self._hertz_to_mel(
            min(self.maximum_analysis_frequency, self.analysis_sample_rate / 2)
        )
        mel_points = np.linspace(minimum_mel, maximum_mel, self.mel_band_count + 2)
        frequency_points = self._mel_to_hertz(mel_points)
        filters = np.zeros((self.mel_band_count, frequencies.size), dtype=np.float32)

        for band_index, (left, center, right) in enumerate(
            zip(
                frequency_points[:-2],
                frequency_points[1:-1],
                frequency_points[2:],
                strict=True,
            )
        ):
            rising = (frequencies - left) / max(center - left, 1e-12)
            falling = (right - frequencies) / max(right - center, 1e-12)
            weights = np.maximum(0.0, np.minimum(rising, falling))
            weight_sum = float(np.sum(weights))
            if weight_sum > 0:
                filters[band_index] = weights / weight_sum

        return filters

    @staticmethod
    def _hertz_to_mel(frequency: float) -> float:
        return 2_595 * np.log10(1 + (frequency / 700))

    @staticmethod
    def _mel_to_hertz(mel_values: np.ndarray) -> np.ndarray:
        return 700 * (np.power(10, mel_values / 2_595) - 1)

    @staticmethod
    def _find_best_match_in_features(
        source_features: np.ndarray, reference_features: np.ndarray
    ) -> tuple[int, float]:
        indexes = AudioMatchingService._build_feature_search_indexes(source_features)
        return AudioMatchingService._find_best_match_with_indexes(
            indexes,
            reference_features,
        )

    @staticmethod
    def _build_feature_search_indexes(
        source_features: np.ndarray,
    ) -> tuple[FeatureSearchIndex, FeatureSearchIndex]:
        source_fingerprint = AudioMatchingService._binary_fingerprint(source_features)
        return (
            AudioMatchingService._build_feature_search_index(source_features),
            AudioMatchingService._build_feature_search_index(source_fingerprint),
        )

    @staticmethod
    def _build_feature_search_index(features: np.ndarray) -> FeatureSearchIndex:
        normalized = AudioMatchingService._normalize_columns(features)
        frame_energy = np.sum(np.square(normalized), axis=1)
        return FeatureSearchIndex(
            normalized_features=normalized,
            cumulative_energy=np.concatenate(([0.0], np.cumsum(frame_energy))),
        )

    @staticmethod
    def _find_best_match_with_indexes(
        indexes: tuple[FeatureSearchIndex, FeatureSearchIndex],
        reference_features: np.ndarray,
    ) -> tuple[int, float]:
        spectral_index, fingerprint_index = indexes
        spectral_scores = AudioMatchingService._feature_match_scores(
            spectral_index,
            reference_features,
        )
        reference_fingerprint = AudioMatchingService._binary_fingerprint(reference_features)
        fingerprint_scores = AudioMatchingService._feature_match_scores(
            fingerprint_index,
            reference_fingerprint,
            allow_empty=True,
        )
        spectral_evidence = np.clip(spectral_scores, 0.0, 1.0)
        fingerprint_evidence = np.clip(fingerprint_scores, 0.0, 1.0)
        combined_scores = 1.0 - (
            (1.0 - spectral_evidence) * (1.0 - fingerprint_evidence)
        )
        best_frame = int(np.argmax(combined_scores))
        confidence = AudioMatchingService._calibrate_match_confidence(
            combined_scores,
            best_frame,
            reference_features.shape[0],
        )
        return best_frame, confidence

    @staticmethod
    def _calibrate_match_confidence(
        scores: np.ndarray,
        best_frame: int,
        reference_frame_count: int,
    ) -> float:
        best_score = float(np.clip(scores[best_frame], 0.0, 1.0))
        # Sliding windows that overlap the winning clip are correlated by design.
        # They are not independent alternatives and can otherwise make a clear match
        # look ambiguous, especially for sustained speech or music.
        exclusion_radius = max(1, reference_frame_count)
        left_scores = scores[: max(0, best_frame - exclusion_radius)]
        right_scores = scores[min(scores.size, best_frame + exclusion_radius + 1) :]
        alternatives = [candidate for candidate in (left_scores, right_scores) if candidate.size]
        if not alternatives or best_score <= 0:
            return best_score

        runner_up = max(float(np.max(candidate)) for candidate in alternatives)
        uniqueness = float(
            np.clip((best_score - runner_up) / max(best_score, 1e-12), 0.0, 1.0)
        )
        evidence_strength = float(np.clip((best_score - 0.2) / 0.25, 0.0, 1.0))
        return best_score + ((1.0 - best_score) * uniqueness * evidence_strength)

    @staticmethod
    def _binary_fingerprint(features: np.ndarray) -> np.ndarray:
        band_count = features.shape[1] // 2
        band_energy = features[:, :band_count]
        spectral_changes = features[:, band_count:]
        frame_median = np.median(band_energy, axis=1, keepdims=True)
        energy_bits = np.where(band_energy >= frame_median, 1.0, -1.0)
        change_bits = np.where(spectral_changes >= 0.0, 1.0, -1.0)
        return np.concatenate((energy_bits, change_bits), axis=1).astype(np.float32)

    @staticmethod
    def _feature_match_scores(
        source_index: FeatureSearchIndex,
        reference_features: np.ndarray,
        *,
        allow_empty: bool = False,
    ) -> np.ndarray:
        source = source_index.normalized_features
        reference = AudioMatchingService._normalize_columns(reference_features)
        reference_energy = float(np.sum(np.square(reference)))
        if reference_energy <= 0:
            if allow_empty:
                return np.zeros(
                    source.shape[0] - reference.shape[0] + 1,
                    dtype=np.float64,
                )
            raise MediaProcessingError("O áudio do vídeo de referência está silencioso.")

        correlation_size = source.shape[0] + reference.shape[0] - 1
        fft_size = 1 << (correlation_size - 1).bit_length()
        source_spectrum = source_index.spectra.get(fft_size)
        if source_spectrum is None:
            source_index.spectra.clear()
            source_spectrum = np.fft.rfft(source, fft_size, axis=0)
            source_index.spectra[fft_size] = source_spectrum
        reference_spectrum = np.fft.rfft(reference[::-1], fft_size, axis=0)
        correlation = np.fft.irfft(
            np.sum(source_spectrum * reference_spectrum, axis=1), fft_size
        )
        valid_correlation = correlation[reference.shape[0] - 1 : source.shape[0]]

        source_energy = (
            source_index.cumulative_energy[reference.shape[0] :]
            - source_index.cumulative_energy[: -reference.shape[0]]
        )
        scores = valid_correlation / np.sqrt(
            np.maximum(source_energy * reference_energy, 1e-12)
        )
        return scores

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

    def _refine_match_with_dtw(
        self,
        match: AudioSegmentMatch,
        source_features: np.ndarray,
        reference_features: np.ndarray,
    ) -> AudioSegmentMatch:
        reference_duration = match.reference_duration_seconds or match.duration_seconds
        reference_start_frame = round(match.reference_start_seconds * self.feature_rate)
        reference_frame_count = max(1, round(reference_duration * self.feature_rate))
        reference_segment = reference_features[
            reference_start_frame : reference_start_frame + reference_frame_count
        ]
        if reference_segment.shape[0] < self.minimum_segment_seconds * self.feature_rate:
            return match

        predicted_start_frame = round(match.source_start_seconds * self.feature_rate)
        expected_source_frames = max(1, round(match.duration_seconds * self.feature_rate))
        padding_frames = self.dtw_search_padding_seconds * self.feature_rate
        window_start = max(0, predicted_start_frame - padding_frames)
        window_end = min(
            source_features.shape[0],
            predicted_start_frame + expected_source_frames + padding_frames,
        )
        source_window = source_features[window_start:window_end]
        if source_window.shape[0] < reference_segment.shape[0] // 2:
            return match

        reference_normalized = self._normalize_feature_rows(
            self._normalize_columns(reference_segment)
        )
        source_normalized = self._normalize_feature_rows(
            self._normalize_columns(source_window)
        )
        similarities = np.clip(
            reference_normalized @ source_normalized.T,
            -1.0,
            1.0,
        ).astype(np.float32, copy=False)
        costs = np.subtract(1.0, similarities, dtype=np.float32)
        reference_frames, source_frames = costs.shape
        accumulated = np.full(
            (reference_frames + 1, source_frames + 1),
            np.inf,
            dtype=np.float32,
        )
        accumulated[0, :] = 0.0
        steps = np.zeros((reference_frames + 1, source_frames + 1), dtype=np.uint8)

        for reference_index in range(1, reference_frames + 1):
            for source_index in range(1, source_frames + 1):
                diagonal = accumulated[reference_index - 1, source_index - 1]
                vertical = (
                    accumulated[reference_index - 1, source_index] + self.dtw_step_penalty
                )
                horizontal = (
                    accumulated[reference_index, source_index - 1] + self.dtw_step_penalty
                )
                if diagonal <= vertical and diagonal <= horizontal:
                    previous_cost = diagonal
                    step = 0
                elif vertical <= horizontal:
                    previous_cost = vertical
                    step = 1
                else:
                    previous_cost = horizontal
                    step = 2
                accumulated[reference_index, source_index] = (
                    costs[reference_index - 1, source_index - 1] + previous_cost
                )
                steps[reference_index, source_index] = step

        source_index = int(np.argmin(accumulated[reference_frames, 1:])) + 1
        reference_index = reference_frames
        path: list[tuple[int, int]] = []
        while reference_index > 0 and source_index > 0:
            path.append((reference_index - 1, source_index - 1))
            step = steps[reference_index, source_index]
            if step == 0:
                reference_index -= 1
                source_index -= 1
            elif step == 1:
                reference_index -= 1
            else:
                source_index -= 1

        if not path:
            return match

        source_path_indexes = [item[1] for item in path]
        refined_start_frame = window_start + min(source_path_indexes)
        refined_end_frame = window_start + max(source_path_indexes) + 1
        refined_duration = (refined_end_frame - refined_start_frame) / self.feature_rate
        refined_tempo_factor = refined_duration / reference_duration
        if not 0.8 <= refined_tempo_factor <= 1.2:
            return match
        if abs(refined_tempo_factor - match.tempo_factor) > self.dtw_tempo_tolerance:
            return match

        path_similarity = float(
            np.mean([similarities[reference, source] for reference, source in path])
        )
        combined_score = max(
            match.score,
            (match.score * 0.75) + (max(0.0, path_similarity) * 0.25),
        )
        return replace(
            match,
            source_start_seconds=refined_start_frame / self.feature_rate,
            duration_seconds=refined_duration,
            score=combined_score,
            tempo_factor=refined_tempo_factor,
        )

    def _refine_segment_match(
        self,
        match: AudioSegmentMatch,
        source_samples: np.ndarray,
        reference_samples: np.ndarray,
        *,
        require_score: bool = False,
    ) -> AudioSegmentMatch:
        if abs(match.tempo_factor - 1.0) > 0.01:
            return match

        sample_rate = self.refinement_sample_rate
        reference_start = int(match.reference_start_seconds * sample_rate)
        reference_duration = match.reference_duration_seconds or match.duration_seconds
        reference_end = int((match.reference_start_seconds + reference_duration) * sample_rate)
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
        combined_score = max(
            match.score,
            (match.score * 0.7) + (refinement_score * 0.3),
        )
        return replace(match, source_start_seconds=refined_start, score=combined_score)

    def _validate_reference_coverage(
        self,
        matches: list[AudioSegmentMatch],
        reference_duration: float,
        *,
        best_score: float | None = None,
    ) -> None:
        covered_duration = sum(
            match.reference_duration_seconds or match.duration_seconds for match in matches
        )
        required_duration = min(
            reference_duration,
            max(
                float(self.minimum_segment_seconds),
                reference_duration * self.minimum_reference_coverage,
            ),
        )
        if covered_duration < required_duration:
            coverage = covered_duration / reference_duration if reference_duration else 0
            confidence_detail = (
                f", melhor confiança {best_score:.0%}" if best_score is not None else ""
            )
            raise AudioMatchNotFoundError(
                "Poucos trechos da referência foram confirmados no vídeo original "
                f"(cobertura {coverage:.0%}{confidence_detail})."
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

    @staticmethod
    def _normalize_feature_rows(values: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        return values / np.maximum(norms, 1e-12)
