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
    pitch_shift_semitones: int = 0
    alignment_confirmation_score: float | None = None


@dataclass(frozen=True)
class ShortWindowMatchCandidate:
    source_start_frame: int
    similarity_score: float
    local_confidence: float


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
    reference_scene_boundaries: tuple[float, ...] = ()
    reference_media_duration_seconds: float | None = None


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

    def detect_scene_boundaries(
        self,
        file_path: str | Path,
        *,
        threshold: float = 0.2,
    ) -> tuple[float, ...]:
        threshold = float(threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("O limiar de detecção de cena deve estar entre 0 e 1.")
        command = [
            self.get_ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostdin",
            "-i",
            str(file_path),
            "-vf",
            f"fps=12,scale=320:-2,select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=settings.yt_dlp_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.warning("Não foi possível detectar emendas visuais: %s", error)
            return ()

        if result.returncode != 0:
            logger.warning("Detecção de emendas visuais falhou: %s", result.stderr[-600:])
            return ()

        boundaries: list[float] = []
        for match in re.finditer(r"pts_time:\s*(-?\d+(?:\.\d+)?)", result.stderr):
            timestamp = float(match.group(1))
            if timestamp > 0 and (not boundaries or timestamp - boundaries[-1] >= 1.0):
                boundaries.append(timestamp)
        return tuple(boundaries)

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
            # Keep audio timestamps on the video's zero-based timeline. Without
            # this, FFmpeg rebases the first audio packet to t=0 and matching
            # results drift whenever the audio stream starts after the video.
            "-af",
            "aresample=first_pts=0",
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
    contiguous_minimum_score = 0.60
    minimum_segment_score = 0.30
    minimum_average_match_score = 0.60
    segment_seconds = 8
    fine_segment_seconds = 4
    concordance_window_seconds = 2
    concordance_step_seconds = 0.5
    concordance_top_candidates = 8
    concordance_minimum_score = 0.48
    concordance_minimum_windows = 3
    concordance_position_tolerance_ratio = 0.2
    concordance_position_tolerance_floor_seconds = 0.08
    concordance_boundary_tolerance_seconds = 1e-6
    pitch_shift_retry_threshold = 0.78
    pitch_shift_retry_semitones = (-2, -1, 1, 2)
    pitch_shift_extended_retry_semitones = (-4, -3, 3, 4)
    pitch_shift_extended_retry_floor = 0.45
    pitch_shift_tempo_radius = 0.05
    minimum_pitch_alignment_confirmation_score = 0.55
    minimum_pitch_shift_spectral_score = 0.85
    segmented_comparison_score_threshold = 0.85
    spectral_evidence_weight = 0.75
    minimum_segment_seconds = 3
    analysis_sample_rate = 8_000
    feature_rate = 50
    refinement_sample_rate = analysis_sample_rate
    refinement_search_seconds = 3
    minimum_refinement_score = 0.15
    minimum_reference_coverage = 0.6
    dense_edit_minimum_cuts_per_minute = 15.0
    dense_edit_tempo_factors = (1.0, 1.1, 1.2, 1.5)
    dense_edit_scene_threshold = 0.5
    dtw_search_padding_seconds = 1
    dtw_step_penalty = 0.05
    dtw_tempo_tolerance = 0.03
    analysis_window_seconds = 0.1
    mel_band_count = 24
    minimum_analysis_frequency = 80
    maximum_analysis_frequency = 3_800
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
        1.175,
        1.2,
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
        matcher = self
        if self._is_high_edit_density(analysis):
            detect_scenes = getattr(self.media_service, "detect_scene_boundaries", None)
            if callable(detect_scenes):
                analysis = replace(
                    analysis,
                    reference_scene_boundaries=tuple(
                        detect_scenes(
                            reference_path,
                            threshold=self.dense_edit_scene_threshold,
                        )
                    ),
                )
            # The denser-edit profile uses a narrower, measured tempo grid to
            # control processing cost. Hashtag-derived real samples do not
            # establish a single speed factor for the user's exact reference.
            matcher = AudioMatchingService(self.media_service)
            matcher.tempo_factors = self.dense_edit_tempo_factors
        return matcher._find_matches_from_analysis(analysis)

    def _is_high_edit_density(self, analysis: AudioAnalysis) -> bool:
        # Require a real probe duration for the adaptive profile; feature arrays
        # in tests and custom integrations may be padded or downsampled.
        duration = analysis.reference_media_duration_seconds
        if duration is None:
            return False
        if duration <= 0:
            return False
        cuts_per_minute = len(analysis.reference_scene_boundaries) * 60.0 / duration
        return cuts_per_minute >= self.dense_edit_minimum_cuts_per_minute

    def _find_matches_from_analysis(
        self, analysis: AudioAnalysis
    ) -> list[AudioSegmentMatch]:
        try:
            contiguous_match = self._find_contiguous_match(analysis)
        except AudioMatchNotFoundError:
            return self._find_segmented_matches_from_analysis(analysis)

        try:
            segmented_matches = self._find_segmented_matches_from_analysis(analysis)
        except AudioMatchNotFoundError:
            return [contiguous_match]

        segmented_score = self._weighted_average_match_score(segmented_matches)
        total_reference_duration = self._matched_reference_duration(segmented_matches)
        discontinuous_reference_duration = sum(
            (match.reference_duration_seconds or match.duration_seconds)
            for match in segmented_matches
            if abs(
                match.source_start_seconds
                - (
                    contiguous_match.source_start_seconds
                    + (match.reference_start_seconds - contiguous_match.reference_start_seconds)
                    * contiguous_match.tempo_factor
                )
            )
            > max(0.5, (match.reference_duration_seconds or match.duration_seconds) * 0.08)
        )
        # A single short false-positive window must not turn an otherwise
        # coherent reference into a montage. Require the disagreement to
        # explain most of the reference, not merely one segment.
        has_discontinuous_alignment = (
            total_reference_duration > 0
            and discontinuous_reference_duration / total_reference_duration > 0.5
        )
        if (
            len(segmented_matches) > 1
            and has_discontinuous_alignment
            and segmented_score >= contiguous_match.score - 0.05
        ):
            return segmented_matches
        return [contiguous_match]

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
        detect_scenes = getattr(self.media_service, "detect_scene_boundaries", None)
        scene_boundaries = (
            tuple(detect_scenes(reference_path)) if callable(detect_scenes) else ()
        )
        probe_duration = getattr(self.media_service, "probe_duration", None)
        reference_media_duration = (
            probe_duration(reference_path) if callable(probe_duration) else None
        )
        return AudioAnalysis(
            source_samples=source_samples,
            reference_samples=reference_samples,
            source_features=source_features,
            reference_features=reference_features,
            source_search_indexes=self._build_feature_search_indexes(source_features),
            reference_scene_boundaries=scene_boundaries,
            reference_media_duration_seconds=reference_media_duration,
        )

    def _find_contiguous_match(self, analysis: AudioAnalysis) -> AudioSegmentMatch:
        start_frame, score, tempo_factor, pitch_shift_semitones = (
            self._find_best_tempo_match_with_pitch(
                analysis.source_features,
                analysis.reference_features,
                search_indexes=analysis.source_search_indexes,
            )
        )
        if score < self.contiguous_minimum_score:
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
            pitch_shift_semitones=pitch_shift_semitones,
        )
        dtw_match = self._refine_match_with_dtw(
            match,
            analysis.source_features,
            analysis.reference_features,
        )
        # Subsequence DTW is open-ended and may shave a few frames from a
        # continuous full-reference match. The tempo scan already spans the
        # complete query, so use DTW for its refined start/score but retain the
        # scan's full duration and tempo estimate.
        dtw_match = replace(
            dtw_match,
            duration_seconds=match.duration_seconds,
            tempo_factor=match.tempo_factor,
        )
        return self._refine_segment_match(
            dtw_match,
            analysis.source_samples,
            analysis.reference_samples,
            require_score=True,
        )

    def _find_segmented_matches_from_analysis(
        self, analysis: AudioAnalysis
    ) -> list[AudioSegmentMatch]:
        matches = self.find_segmented_matches_in_features(
            analysis.source_features,
            analysis.reference_features,
            search_indexes=analysis.source_search_indexes,
            reference_scene_boundaries=analysis.reference_scene_boundaries,
            reference_media_duration_seconds=analysis.reference_media_duration_seconds,
        )

        dtw_matches: list[AudioSegmentMatch] = []
        for match in matches:
            refined = self._refine_match_with_dtw(
                match,
                analysis.source_features,
                analysis.reference_features,
            )
            if self._match_is_scene_anchored(
                match,
                analysis.reference_scene_boundaries,
                analysis.reference_media_duration_seconds,
            ):
                # DTW can shave a few analysis frames from a clip whose visual
                # edit points are already known. Keep the scene-bounded duration;
                # the sample-level pass below still refines its source start.
                refined = replace(
                    refined,
                    duration_seconds=match.duration_seconds,
                    tempo_factor=match.tempo_factor,
                    reference_duration_seconds=match.reference_duration_seconds,
                )
            dtw_matches.append(refined)
        refined_matches = [
            self._refine_segment_match(
                match,
                analysis.source_samples,
                analysis.reference_samples,
                require_score=True,
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
        reference_scene_boundaries: tuple[float, ...] = (),
        reference_media_duration_seconds: float | None = None,
    ) -> list[AudioSegmentMatch]:
        if reference_features.shape[0] >= source_features.shape[0]:
            raise MediaProcessingError("O vídeo de referência precisa ser menor que o vídeo original.")

        indexes = search_indexes or self._build_feature_search_indexes(source_features)
        reference_duration = reference_features.shape[0] / self.feature_rate
        required_coverage = min(
            reference_duration,
            max(
                float(self.minimum_segment_seconds),
                reference_duration * self.minimum_reference_coverage,
            ),
        )

        # Use longer windows as anchors. If the high-context grids are incomplete,
        # a short-window concordance pass can branch over the best candidates and
        # retain only a temporally consistent path.
        scans = [
            self._scan_reference_segments(
                source_features,
                reference_features,
                segment_seconds=self.segment_seconds,
                offset_seconds=0,
                search_indexes=indexes,
                reference_scene_boundaries=reference_scene_boundaries,
            )
        ]
        coarse_coverage = self._matched_reference_duration(scans[0][0])
        coarse_average = self._weighted_average_match_score(scans[0][0])
        if coarse_coverage < required_coverage or coarse_average < 0.72:
            for offset_seconds in (0, self.fine_segment_seconds / 2):
                scans.append(
                    self._scan_reference_segments(
                        source_features,
                        reference_features,
                        segment_seconds=self.fine_segment_seconds,
                        offset_seconds=offset_seconds,
                        search_indexes=indexes,
                        reference_scene_boundaries=reference_scene_boundaries,
                    )
                )

        def scan_quality(
            scan: tuple[list[AudioSegmentMatch], float],
        ) -> tuple[float, float, float]:
            matched_duration = self._matched_reference_duration(scan[0])
            coverage = min(matched_duration / reference_duration, 1.0)
            average_score = self._weighted_average_match_score(scan[0])
            # Rank by confidence across the whole query: a weaker extra segment
            # should not beat a shorter, coherent match just by increasing coverage.
            return average_score * coverage, average_score, coverage

        coarse_matches, _ = max(scans, key=scan_quality)
        candidate_scans = list(scans)

        coarse_coverage = self._matched_reference_duration(coarse_matches)
        has_coarse_discontinuity = self._has_discontinuous_match_alignment(coarse_matches)
        if (
            reference_scene_boundaries
            or coarse_coverage < reference_duration * 0.9
            or self._weighted_average_match_score(coarse_matches) < 0.8
            or has_coarse_discontinuity
        ):
            concordant_matches, concordant_best_score = self._scan_short_windows_with_concordance(
                reference_features,
                search_indexes=indexes,
                scene_boundaries=reference_scene_boundaries,
            )
            if concordant_matches:
                candidate_scans.append((concordant_matches, concordant_best_score))

        valid_scans = [
            scan
            for scan in candidate_scans
            if self._matched_reference_duration(scan[0]) >= required_coverage
            and self._weighted_average_match_score(scan[0])
            >= self.minimum_average_match_score
        ]
        selection_scans = valid_scans or candidate_scans
        selected_matches = self._select_non_overlapping_reference_matches(
            [match for matches, _ in selection_scans for match in matches]
        )
        selected_matches = self._refine_discontinuous_boundaries(
            selected_matches,
            source_features,
            reference_features,
        )
        selected_matches = self._snap_discontinuous_matches_to_scene_boundaries(
            selected_matches,
            reference_scene_boundaries=reference_scene_boundaries,
            reference_duration_seconds=(
                reference_media_duration_seconds or reference_duration
            ),
        )
        best_score = max((scan[1] for scan in selection_scans), default=0.0)
        self._validate_reference_coverage(
            selected_matches,
            reference_duration,
            best_score=best_score,
        )
        return selected_matches

    def _snap_discontinuous_matches_to_scene_boundaries(
        self,
        matches: list[AudioSegmentMatch],
        *,
        reference_scene_boundaries: tuple[float, ...],
        reference_duration_seconds: float,
    ) -> list[AudioSegmentMatch]:
        """Use a nearby visual cut to correct an audio-estimated montage edit."""
        if len(matches) < 2 or not reference_scene_boundaries:
            return matches

        ordered = sorted(matches, key=lambda match: match.reference_start_seconds)
        saw_discontinuous_edit = False
        for index in range(1, len(ordered)):
            previous = ordered[index - 1]
            current = ordered[index]
            previous_duration = (
                previous.reference_duration_seconds or previous.duration_seconds
            )
            current_duration = (
                current.reference_duration_seconds or current.duration_seconds
            )
            previous_offset = previous.source_start_seconds - (
                previous.reference_start_seconds * previous.tempo_factor
            )
            current_offset = current.source_start_seconds - (
                current.reference_start_seconds * current.tempo_factor
            )
            if abs(current_offset - previous_offset) <= max(
                0.5, min(previous_duration, current_duration) * 0.08
            ):
                continue

            saw_discontinuous_edit = True
            expected_boundary = (
                previous.reference_start_seconds
                + previous_duration
                + current.reference_start_seconds
            ) / 2
            scene_boundary = min(
                reference_scene_boundaries,
                key=lambda boundary: abs(boundary - expected_boundary),
            )
            # Scene detection runs on a low-resolution preview cadence; only snap
            # when its timestamp is already close to the independently audio-
            # located edit. This does not invent a boundary in uncertain regions.
            if abs(scene_boundary - expected_boundary) > 0.12:
                continue

            previous_new_duration = (
                scene_boundary - previous.reference_start_seconds
            )
            if previous_new_duration < self.minimum_segment_seconds:
                continue
            ordered[index - 1] = replace(
                previous,
                duration_seconds=previous_new_duration * previous.tempo_factor,
                reference_duration_seconds=previous_new_duration,
            )
            ordered[index] = replace(
                current,
                reference_start_seconds=scene_boundary,
            )

        if not saw_discontinuous_edit:
            return ordered

        last = ordered[-1]
        last_duration = last.reference_duration_seconds or last.duration_seconds
        reference_end = last.reference_start_seconds + last_duration
        if abs(reference_end - reference_duration_seconds) <= 0.12:
            final_duration = reference_duration_seconds - last.reference_start_seconds
            if final_duration >= self.minimum_segment_seconds:
                ordered[-1] = replace(
                    last,
                    duration_seconds=final_duration * last.tempo_factor,
                    reference_duration_seconds=final_duration,
                )
        return ordered

    def _match_is_scene_anchored(
        self,
        match: AudioSegmentMatch,
        scene_boundaries: tuple[float, ...],
        reference_duration_seconds: float | None,
    ) -> bool:
        if not scene_boundaries or reference_duration_seconds is None:
            return False
        reference_start = match.reference_start_seconds
        reference_duration = match.reference_duration_seconds or match.duration_seconds
        reference_end = reference_start + reference_duration
        start_is_anchored = reference_start <= 0.12 or any(
            abs(reference_start - boundary) <= 1e-6
            for boundary in scene_boundaries
        )
        end_is_anchored = (
            abs(reference_end - reference_duration_seconds) <= 1e-6
            or any(
                abs(reference_end - boundary) <= 1e-6
                for boundary in scene_boundaries
            )
        )
        return start_is_anchored and end_is_anchored

    def _refine_discontinuous_boundaries(
        self,
        matches: list[AudioSegmentMatch],
        source_features: np.ndarray,
        reference_features: np.ndarray,
    ) -> list[AudioSegmentMatch]:
        """Refine edit points by comparing both adjacent source alignments locally."""
        if len(matches) < 2:
            return matches

        ordered = sorted(matches, key=lambda match: match.reference_start_seconds)
        feature_rate = self.feature_rate
        search_radius_frames = round(1.5 * feature_rate)
        candidate_boundary_indexes: list[int] = []
        for index in range(1, len(ordered)):
            previous = ordered[index - 1]
            current = ordered[index]
            previous_duration = previous.reference_duration_seconds or previous.duration_seconds
            current_duration = current.reference_duration_seconds or current.duration_seconds
            previous_end = previous.reference_start_seconds + previous_duration
            current_start = current.reference_start_seconds
            previous_offset = previous.source_start_seconds - (
                previous.reference_start_seconds * previous.tempo_factor
            )
            current_offset = current.source_start_seconds - (
                current.reference_start_seconds * current.tempo_factor
            )
            if current_start - previous_end > 0.5:
                continue
            if abs(current_offset - previous_offset) <= max(
                0.5,
                min(previous_duration, current_duration) * 0.08,
            ):
                continue

            expected_boundary = (previous_end + current_start) / 2
            lower_boundary = max(
                previous.reference_start_seconds + self.minimum_segment_seconds,
                expected_boundary - search_radius_frames / feature_rate,
            )
            upper_boundary = min(
                current_start + current_duration - self.minimum_segment_seconds,
                expected_boundary + search_radius_frames / feature_rate,
            )
            if upper_boundary > lower_boundary:
                candidate_boundary_indexes.append(index)

        if not candidate_boundary_indexes:
            return ordered

        normalized_source = self._normalize_feature_rows(
            self._normalize_columns(source_features)
        )
        normalized_reference = self._normalize_feature_rows(
            self._normalize_columns(reference_features)
        )
        refined = list(ordered)

        for index in candidate_boundary_indexes:
            previous = refined[index - 1]
            current = refined[index]
            previous_duration = previous.reference_duration_seconds or previous.duration_seconds
            current_duration = current.reference_duration_seconds or current.duration_seconds
            previous_end = previous.reference_start_seconds + previous_duration
            current_start = current.reference_start_seconds
            if current_start - previous_end > 0.5:
                continue

            previous_offset = previous.source_start_seconds - (
                previous.reference_start_seconds * previous.tempo_factor
            )
            current_offset = current.source_start_seconds - (
                current.reference_start_seconds * current.tempo_factor
            )
            if abs(current_offset - previous_offset) <= max(
                0.5,
                min(previous_duration, current_duration) * 0.08,
            ):
                continue

            expected_boundary = (previous_end + current_start) / 2
            lower_boundary = max(
                previous.reference_start_seconds + self.minimum_segment_seconds,
                expected_boundary - search_radius_frames / feature_rate,
            )
            upper_boundary = min(
                current_start + current_duration - self.minimum_segment_seconds,
                expected_boundary + search_radius_frames / feature_rate,
            )
            if upper_boundary <= lower_boundary:
                continue

            lower_frame = max(0, round(lower_boundary * feature_rate))
            upper_frame = min(
                reference_features.shape[0] - 1,
                round(upper_boundary * feature_rate),
            )
            reference_indexes = np.arange(lower_frame, upper_frame + 1)
            previous_similarity = self._mapped_alignment_similarity(
                normalized_source,
                normalized_reference,
                reference_indexes,
                offset_seconds=previous_offset,
                tempo_factor=previous.tempo_factor,
            )
            current_similarity = self._mapped_alignment_similarity(
                normalized_source,
                normalized_reference,
                reference_indexes,
                offset_seconds=current_offset,
                tempo_factor=current.tempo_factor,
            )
            if previous_similarity is None or current_similarity is None:
                continue

            prefix_previous = np.concatenate(([0.0], np.cumsum(previous_similarity)))
            suffix_current = np.concatenate(
                (np.cumsum(current_similarity[::-1])[::-1], [0.0])
            )
            candidate_scores = (
                prefix_previous + suffix_current
            ) / max(1, reference_indexes.size)
            best_cut_index = int(np.argmax(candidate_scores))
            best_cut_score = float(candidate_scores[best_cut_index])
            no_cut_score = max(
                float(candidate_scores[0]),
                float(candidate_scores[-1]),
            )
            if best_cut_index in (0, candidate_scores.size - 1) or best_cut_score <= (
                no_cut_score + 0.01
            ):
                continue
            best_boundary = (lower_frame + best_cut_index) / feature_rate
            if abs(best_boundary - expected_boundary) > search_radius_frames / feature_rate:
                continue

            previous_new_duration = best_boundary - previous.reference_start_seconds
            current_new_duration = (
                current_start + current_duration - best_boundary
            )
            if min(previous_new_duration, current_new_duration) < self.minimum_segment_seconds:
                continue

            previous_source_end = (
                previous.source_start_seconds
                + previous_new_duration * previous.tempo_factor
            )
            current_source_start = current.source_start_seconds + (
                best_boundary - current_start
            ) * current.tempo_factor
            refined[index - 1] = replace(
                previous,
                duration_seconds=previous_new_duration * previous.tempo_factor,
                reference_duration_seconds=previous_new_duration,
            )
            refined[index] = replace(
                current,
                source_start_seconds=current_source_start,
                reference_start_seconds=best_boundary,
                duration_seconds=current_new_duration * current.tempo_factor,
                reference_duration_seconds=current_new_duration,
            )
            logger.debug(
                "Refined audio edit boundary %.3fs -> %.3fs (alignments %.3fs / %.3fs)",
                expected_boundary,
                best_boundary,
                previous_source_end,
                current_source_start,
            )

        return refined

    @staticmethod
    def _mapped_alignment_similarity(
        source_features: np.ndarray,
        reference_features: np.ndarray,
        reference_indexes: np.ndarray,
        *,
        offset_seconds: float,
        tempo_factor: float,
    ) -> np.ndarray | None:
        source_positions = (
            reference_indexes / AudioMatchingService.feature_rate * tempo_factor
            + offset_seconds
        ) * AudioMatchingService.feature_rate
        source_positions = np.rint(source_positions).astype(np.int64)
        if (
            source_positions.size == 0
            or source_positions.min() < 0
            or source_positions.max() >= source_features.shape[0]
        ):
            return None
        return np.sum(
            reference_features[reference_indexes] * source_features[source_positions],
            axis=1,
        )

    @staticmethod
    def _weighted_average_match_score(matches: list[AudioSegmentMatch]) -> float:
        duration = AudioMatchingService._matched_reference_duration(matches)
        if duration <= 0:
            return 0.0
        return sum(
            match.score * (match.reference_duration_seconds or match.duration_seconds)
            for match in matches
        ) / duration

    @staticmethod
    def _has_discontinuous_match_alignment(matches: list[AudioSegmentMatch]) -> bool:
        if len(matches) < 2:
            return False

        ordered = sorted(matches, key=lambda match: match.reference_start_seconds)
        baseline = ordered[0].source_start_seconds - (
            ordered[0].reference_start_seconds * ordered[0].tempo_factor
        )
        for match in ordered[1:]:
            offset = match.source_start_seconds - (
                match.reference_start_seconds * match.tempo_factor
            )
            tolerance = max(
                0.5,
                (match.reference_duration_seconds or match.duration_seconds) * 0.08,
            )
            if abs(offset - baseline) > tolerance:
                return True
        return False

    @staticmethod
    def _select_non_overlapping_reference_matches(
        candidates: list[AudioSegmentMatch],
    ) -> list[AudioSegmentMatch]:
        """Combine strong partial scans without double-counting reference time."""
        if not candidates:
            return []

        ordered = sorted(
            candidates,
            key=lambda match: (
                match.reference_start_seconds
                + (match.reference_duration_seconds or match.duration_seconds),
                match.reference_start_seconds,
                -match.score,
            ),
        )
        best_scores: list[float] = []
        best_paths: list[list[AudioSegmentMatch]] = []
        for index, match in enumerate(ordered):
            match_duration = match.reference_duration_seconds or match.duration_seconds
            match_end = match.reference_start_seconds + match_duration
            predecessor = next(
                (
                    previous_index
                    for previous_index in range(index - 1, -1, -1)
                    if ordered[previous_index].reference_start_seconds
                    + (
                        ordered[previous_index].reference_duration_seconds
                        or ordered[previous_index].duration_seconds
                    )
                    <= match.reference_start_seconds + 1e-6
                ),
                None,
            )
            previous_score = best_scores[predecessor] if predecessor is not None else 0.0
            previous_path = best_paths[predecessor] if predecessor is not None else []
            take_score = previous_score + (match_duration * match.score)
            skip_score = best_scores[index - 1] if index > 0 else 0.0
            if take_score >= skip_score:
                best_scores.append(take_score)
                best_paths.append([*previous_path, match])
            else:
                best_scores.append(skip_score)
                best_paths.append(best_paths[index - 1].copy())

        selected = best_paths[-1]
        return sorted(selected, key=lambda match: match.reference_start_seconds)

    def _scan_short_windows_with_concordance(
        self,
        reference_features: np.ndarray,
        *,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex],
        scene_boundaries: tuple[float, ...],
    ) -> tuple[list[AudioSegmentMatch], float]:
        feature_rate = self.feature_rate
        reference_frames = reference_features.shape[0]
        window_frames = round(self.concordance_window_seconds * feature_rate)
        step_frames = round(self.concordance_step_seconds * feature_rate)
        minimum_scene_frames = round(feature_rate)
        scene_edges = [0]
        for boundary in sorted(scene_boundaries):
            frame = round(boundary * feature_rate)
            if (
                frame - scene_edges[-1] >= minimum_scene_frames
                and reference_frames - frame >= minimum_scene_frames
            ):
                scene_edges.append(frame)
        scene_edges.append(reference_frames)

        matches: list[AudioSegmentMatch] = []
        ambiguous_match_indexes: set[int] = set()
        best_local_confidence = 0.0
        candidate_windows: list[list[AudioSegmentMatch]] = []
        for scene_start, scene_end in zip(scene_edges[:-1], scene_edges[1:], strict=True):
            scene_length = scene_end - scene_start
            if scene_length < minimum_scene_frames:
                continue
            scene_window_frames = min(window_frames, scene_length)
            starts = list(
                range(scene_start, scene_end - scene_window_frames + 1, step_frames)
            )
            final_start = scene_end - scene_window_frames
            if not starts or starts[-1] != final_start:
                starts.append(final_start)

            for reference_start_frame in starts:
                reference_window = reference_features[
                    reference_start_frame : reference_start_frame + scene_window_frames
                ]
                top_candidates = self._find_top_short_window_candidates(
                    search_indexes,
                    reference_window,
                    limit=self.concordance_top_candidates,
                )
                window_matches: list[AudioSegmentMatch] = []
                for candidate in top_candidates:
                    best_local_confidence = max(
                        best_local_confidence,
                        candidate.local_confidence,
                    )
                    # A short window can be locally ambiguous (for example, repeated
                    # speech or a common phrase) while still belonging to a unique,
                    # coherent timeline across neighboring windows. Keep raw
                    # similarity for the concordance tree and use the calibrated
                    # confidence only as local evidence; rejecting on local
                    # confidence here prevents the tree from ever confirming it.
                    if candidate.similarity_score < self.concordance_minimum_score:
                        continue
                    duration = scene_window_frames / feature_rate
                    window_matches.append(
                        AudioSegmentMatch(
                            source_start_seconds=(
                                candidate.source_start_frame / feature_rate
                            ),
                            reference_start_seconds=reference_start_frame / feature_rate,
                            duration_seconds=duration,
                            score=candidate.similarity_score,
                            reference_duration_seconds=duration,
                            alignment_confirmation_score=candidate.local_confidence,
                        )
                    )
                candidate_windows.append(window_matches)

        remaining_windows = [list(window) for window in candidate_windows]
        while True:
            path = self._best_concordant_window_path(
                remaining_windows,
                step_seconds=step_frames / feature_rate,
            )
            if len(path) < self.concordance_minimum_windows:
                break

            first = path[0]
            last = path[-1]
            reference_end = last.reference_start_seconds + (
                last.reference_duration_seconds or last.duration_seconds
            )
            reference_start = first.reference_start_seconds
            reference_duration = reference_end - reference_start
            reference_span = last.reference_start_seconds - reference_start
            tempo_factor = (
                (last.source_start_seconds - first.source_start_seconds) / reference_span
                if reference_span > 0
                else 1.0
            )
            average_score = self._weighted_average_match_score(path)
            if (
                average_score >= self.minimum_average_match_score
                and 0.8 <= tempo_factor <= 1.2
            ):
                path_local_confidence = sum(
                    candidate.alignment_confirmation_score
                    if candidate.alignment_confirmation_score is not None
                    else candidate.score
                    for candidate in path
                ) / len(path)
                candidate_match = AudioSegmentMatch(
                    source_start_seconds=first.source_start_seconds,
                    reference_start_seconds=reference_start,
                    duration_seconds=reference_duration * tempo_factor,
                    score=average_score,
                    reference_duration_seconds=reference_duration,
                    tempo_factor=tempo_factor,
                    alignment_confirmation_score=path_local_confidence,
                )
                for match_index, previous_match in enumerate(matches):
                    overlap = max(
                        0.0,
                        min(
                            reference_end,
                            previous_match.reference_start_seconds
                            + (
                                previous_match.reference_duration_seconds
                                or previous_match.duration_seconds
                            ),
                        )
                        - max(reference_start, previous_match.reference_start_seconds),
                    )
                    shorter_duration = min(
                        reference_duration,
                        previous_match.reference_duration_seconds
                        or previous_match.duration_seconds,
                    )
                    longer_duration = max(
                        reference_duration,
                        previous_match.reference_duration_seconds
                        or previous_match.duration_seconds,
                    )
                    source_separation = abs(
                        first.source_start_seconds - previous_match.source_start_seconds
                    )
                    if (
                        shorter_duration > 0
                        and overlap / max(longer_duration, 1e-12) >= 0.85
                        and source_separation > max(1.0, shorter_duration * 0.1)
                        and abs(average_score - previous_match.score) <= 0.05
                    ):
                        ambiguous_match_indexes.add(match_index)
                        ambiguous_match_indexes.add(len(matches))
                matches.append(candidate_match)

            # Remove this source trajectory, not the whole reference interval. This
            # lets the tree compare alternate source locations for the same clip and
            # reject genuinely ambiguous repeated audio instead of silently picking
            # the first occurrence.
            changed = False
            for index, window in enumerate(remaining_windows):
                retained = [
                    candidate
                    for candidate in window
                    if not any(
                        abs(
                            candidate.reference_start_seconds
                            - path_candidate.reference_start_seconds
                        )
                        <= max(step_frames / feature_rate, 0.5)
                        and abs(
                            candidate.source_start_seconds
                            - path_candidate.source_start_seconds
                        )
                        <= 1.0
                        for path_candidate in path
                    )
                ]
                if len(retained) != len(window):
                    remaining_windows[index] = retained
                    changed = True
            if not changed:
                break

        matches = [
            match
            for index, match in enumerate(matches)
            if index not in ambiguous_match_indexes
        ]
        best_score = (
            max(match.score for match in matches)
            if matches
            else best_local_confidence
        )
        return self._select_non_overlapping_reference_matches(matches), best_score

    def _find_top_short_window_candidates(
        self,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex],
        reference_features: np.ndarray,
        *,
        limit: int,
    ) -> list[ShortWindowMatchCandidate]:
        spectral_index, fingerprint_index = search_indexes
        spectral_scores = self._feature_match_scores(spectral_index, reference_features)
        reference_fingerprint = self._binary_fingerprint(reference_features)
        fingerprint_scores = self._feature_match_scores(
            fingerprint_index,
            reference_fingerprint,
            allow_empty=True,
        )
        scores = self._combine_feature_evidence(
            spectral_scores,
            fingerprint_scores,
        )
        candidate_scores = scores.copy()
        suppression_radius = max(
            1,
            round(self.concordance_window_seconds * self.feature_rate / 2),
        )
        candidates: list[ShortWindowMatchCandidate] = []
        for _ in range(limit):
            frame = int(np.argmax(candidate_scores))
            raw_score = float(candidate_scores[frame])
            if not np.isfinite(raw_score) or raw_score <= 0:
                break
            confidence = self._calibrate_match_confidence(
                scores,
                frame,
                reference_features.shape[0],
            )
            candidates.append(
                ShortWindowMatchCandidate(
                    source_start_frame=frame,
                    similarity_score=raw_score,
                    local_confidence=confidence,
                )
            )
            candidate_scores[
                max(0, frame - suppression_radius) : frame + suppression_radius + 1
            ] = -np.inf
        return candidates

    def _best_concordant_window_path(
        self,
        candidate_windows: list[list[AudioSegmentMatch]],
        *,
        step_seconds: float,
    ) -> list[AudioSegmentMatch]:
        scores: list[list[float]] = []
        lengths: list[list[int]] = []
        previous_nodes: list[list[tuple[int, int] | None]] = []

        for window_index, candidates in enumerate(candidate_windows):
            window_scores = [candidate.score for candidate in candidates]
            window_lengths = [1] * len(candidates)
            window_previous: list[tuple[int, int] | None] = [None] * len(candidates)
            if window_index > 0:
                prior_candidates = candidate_windows[window_index - 1]
                for candidate_index, candidate in enumerate(candidates):
                    for prior_index, prior in enumerate(prior_candidates):
                        source_step = (
                            candidate.source_start_seconds - prior.source_start_seconds
                        )
                        reference_step = (
                            candidate.reference_start_seconds - prior.reference_start_seconds
                        )
                        allowed_position_drift = max(
                            self.concordance_position_tolerance_floor_seconds,
                            reference_step * self.concordance_position_tolerance_ratio
                            + (2 / self.feature_rate),
                        )
                        if source_step <= 0 or abs(source_step - reference_step) > (
                            allowed_position_drift
                        ):
                            continue
                        proposed_length = lengths[window_index - 1][prior_index] + 1
                        proposed_score = scores[window_index - 1][prior_index] + candidate.score
                        if (
                            proposed_length > window_lengths[candidate_index]
                            or (
                                proposed_length == window_lengths[candidate_index]
                                and proposed_score > window_scores[candidate_index]
                            )
                        ):
                            window_lengths[candidate_index] = proposed_length
                            window_scores[candidate_index] = proposed_score
                            window_previous[candidate_index] = (window_index - 1, prior_index)
            scores.append(window_scores)
            lengths.append(window_lengths)
            previous_nodes.append(window_previous)

        best_endpoint: tuple[int, int] | None = None
        best_key = (0, 0.0)
        for window_index, window_lengths in enumerate(lengths):
            for candidate_index, path_length in enumerate(window_lengths):
                path_score = scores[window_index][candidate_index]
                key = (path_length, path_score / path_length)
                if key > best_key:
                    best_key = key
                    best_endpoint = (window_index, candidate_index)

        if best_endpoint is None or best_key[0] < self.concordance_minimum_windows:
            return []

        path: list[AudioSegmentMatch] = []
        node: tuple[int, int] | None = best_endpoint
        while node is not None:
            window_index, candidate_index = node
            path.append(candidate_windows[window_index][candidate_index])
            node = previous_nodes[window_index][candidate_index]
        path.reverse()

        # A path must truly contain adjacent reference windows, not just a long
        # chain accidentally connected after a scene/window gap.
        if len(path) < self.concordance_minimum_windows or any(
            current.reference_start_seconds - prior.reference_start_seconds
            > max(
                step_seconds * 1.5,
                prior.reference_duration_seconds or prior.duration_seconds,
            )
            + self.concordance_boundary_tolerance_seconds
            for prior, current in zip(path, path[1:])
        ):
            return []
        return path

    def _scan_reference_segments(
        self,
        source_features: np.ndarray,
        reference_features: np.ndarray,
        *,
        segment_seconds: float,
        offset_seconds: float,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex],
        reference_scene_boundaries: tuple[float, ...] = (),
    ) -> tuple[list[AudioSegmentMatch], float]:
        segment_frames = round(segment_seconds * self.feature_rate)
        minimum_frames = self.minimum_segment_seconds * self.feature_rate
        offset_frames = round(offset_seconds * self.feature_rate)
        candidates: list[AudioSegmentMatch] = []
        best_scores: list[float] = []

        for reference_start_frame in range(
            offset_frames,
            reference_features.shape[0],
            segment_frames,
        ):
            reference_end_frame = min(
                reference_start_frame + segment_frames,
                reference_features.shape[0],
            )
            if any(
                reference_start_frame < round(boundary * self.feature_rate) < reference_end_frame
                for boundary in reference_scene_boundaries
            ):
                continue
            reference_segment = reference_features[
                reference_start_frame : reference_start_frame + segment_frames
            ]
            if reference_segment.shape[0] < minimum_frames:
                continue

            (
                source_start_frame,
                score,
                tempo_factor,
                pitch_shift_semitones,
            ) = self._find_best_tempo_match_with_pitch(
                source_features,
                reference_segment,
                search_indexes=search_indexes,
            )
            best_scores.append(score)
            if score < self.minimum_segment_score:
                continue

            reference_duration_seconds = reference_segment.shape[0] / self.feature_rate
            candidates.append(
                AudioSegmentMatch(
                    source_start_seconds=source_start_frame / self.feature_rate,
                    reference_start_seconds=reference_start_frame / self.feature_rate,
                    duration_seconds=reference_duration_seconds * tempo_factor,
                    score=score,
                    reference_duration_seconds=reference_duration_seconds,
                    tempo_factor=tempo_factor,
                    pitch_shift_semitones=pitch_shift_semitones,
                )
            )

        return (
            self._select_chronological_matches(candidates),
            max(best_scores, default=0.0),
        )

    @staticmethod
    def _matched_reference_duration(matches: list[AudioSegmentMatch]) -> float:
        return sum(
            match.reference_duration_seconds or match.duration_seconds for match in matches
        )

    def _find_best_tempo_match(
        self,
        source_features: np.ndarray,
        reference_features: np.ndarray,
        *,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex] | None = None,
    ) -> tuple[int, float, float]:
        best_frame, best_score, best_tempo_factor, _ = (
            self._find_best_tempo_match_with_pitch(
                source_features,
                reference_features,
                search_indexes=search_indexes,
            )
        )
        return best_frame, best_score, best_tempo_factor

    def _find_best_tempo_match_with_pitch(
        self,
        source_features: np.ndarray,
        reference_features: np.ndarray,
        *,
        search_indexes: tuple[FeatureSearchIndex, FeatureSearchIndex] | None = None,
    ) -> tuple[int, float, float, int]:
        indexes = search_indexes or self._build_feature_search_indexes(source_features)
        best_frame = 0
        best_score = float("-inf")
        best_tempo_factor = 1.0
        best_pitch_shift_semitones = 0
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

        if best_score < self.pitch_shift_retry_threshold:
            nearby_tempo_factors = [
                factor
                for factor in self.tempo_factors
                if abs(factor - best_tempo_factor) <= self.pitch_shift_tempo_radius
            ]

            def evaluate_pitch_shifts(semitone_candidates: tuple[int, ...]) -> None:
                nonlocal best_frame, best_score, best_tempo_factor
                nonlocal best_pitch_shift_semitones
                for semitones in semitone_candidates:
                    pitch_adjusted_reference = self._shift_spectral_feature_pitch(
                        reference_features,
                        semitones=semitones,
                    )
                    for tempo_factor in nearby_tempo_factors:
                        adjusted_reference = self._resample_features(
                            pitch_adjusted_reference,
                            tempo_factor,
                        )
                        if adjusted_reference.shape[0] >= source_features.shape[0]:
                            continue
                        candidate_frame, candidate_score = self._find_best_match_with_indexes(
                            indexes,
                            adjusted_reference,
                        )
                        logger.debug(
                            "Candidato de tom %+d semitons e velocidade %.3f: "
                            "confiança %.4f no frame %s",
                            semitones,
                            tempo_factor,
                            candidate_score,
                            candidate_frame,
                        )
                        if candidate_score > best_score:
                            best_frame = candidate_frame
                            best_score = candidate_score
                            best_tempo_factor = tempo_factor
                            best_pitch_shift_semitones = semitones

            evaluate_pitch_shifts(self.pitch_shift_retry_semitones)
            if (
                self.pitch_shift_extended_retry_floor
                <= best_score
                < self.pitch_shift_retry_threshold
            ):
                evaluate_pitch_shifts(self.pitch_shift_extended_retry_semitones)

        return best_frame, best_score, best_tempo_factor, best_pitch_shift_semitones

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

    def _shift_spectral_feature_pitch(
        self,
        features: np.ndarray,
        *,
        semitones: int,
    ) -> np.ndarray:
        """Remap mel bands to tolerate modest pitch-shifted copies of a clip."""
        band_count = features.shape[1] // 2
        minimum_mel = self._hertz_to_mel(self.minimum_analysis_frequency)
        maximum_mel = self._hertz_to_mel(self.maximum_analysis_frequency)
        mel_centers = np.linspace(minimum_mel, maximum_mel, band_count + 2)[1:-1]
        pitch_ratio = 2 ** (semitones / 12)
        target_mels = self._hertz_to_mel(self._mel_to_hertz(mel_centers) * pitch_ratio)
        source_band_indexes = np.interp(
            target_mels,
            mel_centers,
            np.arange(band_count),
            left=0,
            right=band_count - 1,
        )
        lower_indexes = np.floor(source_band_indexes).astype(np.intp)
        upper_indexes = np.ceil(source_band_indexes).astype(np.intp)
        interpolation = source_band_indexes - lower_indexes

        shifted_energy = (
            features[:, lower_indexes]
            + interpolation * (features[:, upper_indexes] - features[:, lower_indexes])
        )
        shifted_changes = (
            features[:, band_count + lower_indexes]
            + interpolation
            * (
                features[:, band_count + upper_indexes]
                - features[:, band_count + lower_indexes]
            )
        )
        return np.concatenate((shifted_energy, shifted_changes), axis=1).astype(
            features.dtype,
            copy=False,
        )

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

    def _spectral_landmark_hashes(
        self,
        samples: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Extract sparse peak-pair hashes and their anchor-frame times."""
        sample_rate = self.analysis_sample_rate
        frame_size = 1_024
        hop_size = sample_rate // self.feature_rate
        if samples.size < frame_size:
            return np.empty(0, dtype=np.uint32), np.empty(0, dtype=np.int32)

        frames = np.lib.stride_tricks.sliding_window_view(samples, frame_size)[::hop_size]
        window = np.hanning(frame_size).astype(np.float32)
        magnitudes = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
        magnitudes[:, 0] = 0.0

        padded = np.pad(magnitudes, ((1, 1), (1, 1)), mode="constant")
        neighbors = (
            padded[:-2, 1:-1],
            padded[2:, 1:-1],
            padded[1:-1, :-2],
            padded[1:-1, 2:],
        )
        is_peak = np.ones(magnitudes.shape, dtype=bool)
        for neighbor in neighbors:
            is_peak &= magnitudes >= neighbor

        low_bin = max(1, round(self.minimum_analysis_frequency * frame_size / sample_rate))
        high_bin = min(
            magnitudes.shape[1] - 1,
            round(self.maximum_analysis_frequency * frame_size / sample_rate),
        )
        is_peak[:, :low_bin] = False
        is_peak[:, high_bin + 1 :] = False

        peaks_by_frame: list[list[tuple[int, float]]] = []
        for frame_index, frame_peaks in enumerate(is_peak):
            bins = np.flatnonzero(frame_peaks)
            if bins.size > 4:
                strengths = magnitudes[frame_index, bins]
                bins = bins[np.argpartition(strengths, -4)[-4:]]
            peaks_by_frame.append(
                [
                    (int(bin_index), float(magnitudes[frame_index, bin_index]))
                    for bin_index in bins
                ]
            )

        unique_landmarks: set[tuple[int, int]] = set()
        max_target_frame_delta = min(32, 2**5 - 1)
        for anchor_frame, anchor_peaks in enumerate(peaks_by_frame):
            for first_bin, _ in anchor_peaks:
                targets: list[tuple[float, int, int]] = []
                last_frame = min(len(peaks_by_frame), anchor_frame + max_target_frame_delta + 1)
                for target_frame in range(anchor_frame + 2, last_frame):
                    frame_delta = target_frame - anchor_frame
                    for second_bin, strength in peaks_by_frame[target_frame]:
                        frequency_delta = second_bin - first_bin
                        if not 0 < frequency_delta <= 63:
                            continue
                        targets.append((strength, second_bin, frame_delta))
                if not targets:
                    continue

                targets.sort(reverse=True)
                for _, second_bin, frame_delta in targets[:3]:
                    first_quantized = first_bin // 2
                    second_quantized = second_bin // 2
                    time_quantized = frame_delta // 2
                    landmark_hash = (
                        (first_quantized << 13) | (second_quantized << 5) | time_quantized
                    )
                    unique_landmarks.add((landmark_hash, anchor_frame))

        if not unique_landmarks:
            return np.empty(0, dtype=np.uint32), np.empty(0, dtype=np.int32)
        ordered = sorted(unique_landmarks, key=lambda item: item[1])
        return (
            np.fromiter((item[0] for item in ordered), dtype=np.uint32),
            np.fromiter((item[1] for item in ordered), dtype=np.int32),
        )

    def _find_landmark_alignment(
        self,
        source_samples: np.ndarray,
        reference_samples: np.ndarray,
    ) -> tuple[float, int, int] | None:
        """Vote for a shared time offset using temporally consistent peak pairs."""
        source_hashes, source_frames = self._spectral_landmark_hashes(source_samples)
        reference_hashes, reference_frames = self._spectral_landmark_hashes(reference_samples)
        if reference_hashes.size < 20:
            return None

        source_index: dict[int, list[int]] = {}
        for fingerprint, frame in zip(source_hashes, source_frames, strict=True):
            source_index.setdefault(int(fingerprint), []).append(int(frame))

        offsets: list[int] = []
        reference_indexes: list[int] = []
        for reference_index, (fingerprint, reference_frame) in enumerate(
            zip(reference_hashes, reference_frames, strict=True)
        ):
            for source_frame in source_index.get(int(fingerprint), ()):
                offsets.append(source_frame - int(reference_frame))
                reference_indexes.append(reference_index)
        if not offsets:
            return None

        # A 40 ms bin absorbs one-frame peak jitter while preserving useful cut precision.
        offset_bins = np.rint(np.asarray(offsets, dtype=np.float64) / 2).astype(np.int32)
        votes_by_bin: dict[int, set[int]] = {}
        for offset_bin, reference_index in zip(
            offset_bins,
            reference_indexes,
            strict=True,
        ):
            votes_by_bin.setdefault(int(offset_bin), set()).add(reference_index)

        cluster_votes: dict[int, set[int]] = {}
        for offset_bin in votes_by_bin:
            inliers: set[int] = set()
            for neighbor_bin in range(offset_bin - 1, offset_bin + 2):
                inliers.update(votes_by_bin.get(neighbor_bin, ()))
            cluster_votes[offset_bin] = inliers

        best_bin = max(cluster_votes, key=lambda candidate: len(cluster_votes[candidate]))
        best_inliers = cluster_votes[best_bin]
        alternative_count = max(
            (
                len(inliers)
                for candidate, inliers in cluster_votes.items()
                if abs(candidate - best_bin) > 2
            ),
            default=0,
        )
        inlier_count = len(best_inliers)
        if inlier_count < 6 or inlier_count / reference_hashes.size < 0.01:
            return None
        if alternative_count >= inlier_count * 0.8:
            return None

        offset_seconds = best_bin * 2 / self.feature_rate
        return offset_seconds, inlier_count, int(reference_hashes.size)

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
        combined_scores = AudioMatchingService._combine_feature_evidence(
            spectral_scores,
            fingerprint_scores,
        )
        best_frame = int(np.argmax(combined_scores))
        confidence = AudioMatchingService._calibrate_match_confidence(
            combined_scores,
            best_frame,
            reference_features.shape[0],
        )
        return best_frame, confidence

    @staticmethod
    def _combine_feature_evidence(
        spectral_scores: np.ndarray,
        fingerprint_scores: np.ndarray,
    ) -> np.ndarray:
        """Blend correlated views of the same spectrogram without inflating confidence."""
        spectral_weight = AudioMatchingService.spectral_evidence_weight
        return np.clip(
            spectral_weight * np.clip(spectral_scores, 0.0, 1.0)
            + (1.0 - spectral_weight) * np.clip(fingerprint_scores, 0.0, 1.0),
            0.0,
            1.0,
        )

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
        # Treat near-identical, distant peaks as an ambiguous location. A moderate
        # runner-up should not erase evidence from a clean match, but an almost
        # tied second location must not leave the result at near-100% confidence.
        if best_score >= 0.6 and uniqueness < 0.05:
            return best_score * 0.4

        uniqueness_bonus = min(
            0.05,
            (1.0 - best_score) * uniqueness * evidence_strength * 0.05,
        )
        return best_score + uniqueness_bonus

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
        if match.pitch_shift_semitones:
            reference_segment = self._shift_spectral_feature_pitch(
                reference_segment,
                semitones=match.pitch_shift_semitones,
            )

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
        accumulated, steps = self._subsequence_dtw_costs(costs, self.dtw_step_penalty)

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
            alignment_confirmation_score=path_similarity,
        )

    @staticmethod
    def _subsequence_dtw_costs(
        costs: np.ndarray,
        step_penalty: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute open-begin subsequence DTW with vectorized row recurrences."""
        reference_frames, source_frames = costs.shape
        accumulated = np.full(
            (reference_frames + 1, source_frames + 1),
            np.inf,
            dtype=np.float64,
        )
        accumulated[0, :] = 0.0
        steps = np.zeros((reference_frames + 1, source_frames + 1), dtype=np.uint8)
        source_indexes = np.arange(source_frames)

        for reference_index in range(1, reference_frames + 1):
            previous_row = accumulated[reference_index - 1]
            diagonal = previous_row[:-1]
            vertical = previous_row[1:] + step_penalty
            local_costs = costs[reference_index - 1]
            base_costs = local_costs + np.minimum(diagonal, vertical)

            # A horizontal DTW step adds local_cost + penalty. Prefix minima
            # turn its left-to-right recurrence into vector operations.
            horizontal_costs = local_costs + step_penalty
            cumulative_horizontal = np.cumsum(horizontal_costs, dtype=np.float64)
            prefix_candidates = base_costs - cumulative_horizontal
            prefix_minima = np.minimum.accumulate(prefix_candidates)
            prefix_indexes = np.maximum.accumulate(
                np.where(prefix_candidates <= prefix_minima, source_indexes, -1)
            )

            accumulated[reference_index, 1:] = cumulative_horizontal + prefix_minima
            starts_here = prefix_indexes == source_indexes
            base_steps = np.where(diagonal <= vertical, 0, 1).astype(np.uint8)
            steps[reference_index, 1:] = np.where(starts_here, base_steps, 2)

        return accumulated, steps

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
                raise AudioMatchNotFoundError(
                    "O trecho é curto demais para confirmar a correspondência."
                )
            return match

        padding = self.refinement_search_seconds * sample_rate
        predicted_start = int(match.source_start_seconds * sample_rate)
        search_start = max(0, predicted_start - padding)
        search_end = min(source_samples.size, predicted_start + reference_segment.size + padding)
        source_search_region = source_samples[search_start:search_end]
        if source_search_region.size <= reference_segment.size:
            if require_score:
                raise AudioMatchNotFoundError(
                    "Não há áudio suficiente para confirmar o trecho encontrado."
                )
            return match

        relative_start, refinement_score = self._find_best_match_in_samples(
            source_search_region, reference_segment
        )
        landmark_alignment = self._find_landmark_alignment(
            source_search_region,
            reference_segment,
        )
        if refinement_score < self.minimum_refinement_score:
            if require_score and landmark_alignment is None:
                if (
                    match.pitch_shift_semitones
                    and match.score >= self.minimum_pitch_shift_spectral_score
                ) or (
                    match.pitch_shift_semitones
                    and match.alignment_confirmation_score is not None
                    and match.alignment_confirmation_score
                    >= self.minimum_pitch_alignment_confirmation_score
                ):
                    return match
                raise AudioMatchNotFoundError(
                    "A assinatura espectral parece semelhante, mas a forma de onda não confirmou "
                    "o trecho, nem houve confirmação por fingerprints espectrais."
                )
            if landmark_alignment is None:
                return match

        waveform_start = (search_start + relative_start) / sample_rate
        if (
            match.alignment_confirmation_score is not None
            and match.alignment_confirmation_score
            >= 0.8
            and abs(waveform_start - match.source_start_seconds) > 0.18
        ):
            # A pitch-shifted waveform can correlate strongly with a different
            # repetition in the search window. Do not let that ambiguous local
            # peak override a strong DTW alignment; repeated background can also
            # make audio landmarks agree on the same wrong alternate occurrence.
            return match
        if landmark_alignment is not None:
            landmark_offset, inlier_count, _ = landmark_alignment
            landmark_start = search_start / sample_rate + landmark_offset
            if (
                match.alignment_confirmation_score is not None
                and match.alignment_confirmation_score >= 0.8
                and abs(landmark_start - match.source_start_seconds) > 0.18
            ):
                # A strong feature alignment already fixes the segment's time.
                # Landmark voting can lock onto repeated background/music and
                # move the cut to a different occurrence; disagreement is not
                # evidence to replace the better-supported timestamp.
                return match
            waveform_disagrees = abs(waveform_start - landmark_start) > 0.18
            if refinement_score < self.minimum_refinement_score or (
                waveform_disagrees and inlier_count >= 12
            ):
                return replace(match, source_start_seconds=landmark_start)

        combined_score = max(
            match.score,
            (match.score * 0.7) + (refinement_score * 0.3),
        )
        return replace(match, source_start_seconds=waveform_start, score=combined_score)

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
        coverage = covered_duration / reference_duration if reference_duration else 0
        average_score = self._weighted_average_match_score(matches)
        if (
            covered_duration < required_duration
            or average_score < self.minimum_average_match_score
        ):
            confidence_detail = (
                f", melhor confiança {best_score:.0%}" if best_score is not None else ""
            )
            if covered_duration < required_duration:
                reason = "Poucos trechos da referência foram confirmados no vídeo original"
            else:
                reason = "As correspondências ficaram abaixo da confiança média necessária"
            raise AudioMatchNotFoundError(
                f"{reason} (cobertura {coverage:.0%}, confiança média "
                f"{average_score:.0%}{confidence_detail})."
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
