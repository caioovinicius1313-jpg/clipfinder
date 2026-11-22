import asyncio
import base64
import subprocess
import uuid
from collections.abc import Generator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.database import Base
from app.main import app
from app.models.generated_clip import GeneratedClip
from app.models.hashtag_video_daily_stat import HashtagVideoDailyStat
from app.models.project import Project
from app.models.reference_video import ReferenceVideo
from app.models.source_video import SourceVideo
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.repositories.hashtag_video_daily_stat_repository import HashtagVideoDailyStatRepository
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.schemas.storage import MultipartUploadStart
from app.schemas.youtube import YoutubeRankingMetric, YoutubeVideoDetails
from app.schemas.automation import AutomationCandidateRead
from app.services.automation_service import AutomationService
from app.services.hashtag_ranking_service import HashtagRankingService
from app.services.media_processing_service import (
    AudioAnalysis,
    AudioMatchNotFoundError,
    AudioSegmentMatch,
    AudioMatchingService,
    MediaProbe,
    MediaProcessingError,
    MediaProcessingService,
)
from app.services.multipart_upload_service import MultipartUploadService
from app.services.storage_service import StorageService
from app.services.youtube_download_service import YoutubeDownloadService
from app.services.youtube_service import YoutubeService


def test_extracts_supported_youtube_video_urls() -> None:
    service = YoutubeDownloadService()

    assert service.extract_video_id("https://www.youtube.com/watch?v=abc123_DEF") == "abc123_DEF"
    assert service.extract_video_id("https://youtu.be/abc123_DEF?t=10") == "abc123_DEF"
    assert service.extract_video_id("https://www.youtube.com/shorts/abc123_DEF") == "abc123_DEF"


def test_youtube_download_can_use_browser_cookies_and_node_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "youtube_cookies_base64", None)
    monkeypatch.setattr(settings, "youtube_cookies_file", None)
    monkeypatch.setattr(settings, "youtube_cookies_from_browser", "edge")
    monkeypatch.setattr(
        "app.services.youtube_download_service.shutil.which",
        lambda executable: "C:/Program Files/nodejs/node.exe" if executable == "node" else None,
    )

    command = YoutubeDownloadService()._build_command("abc123_DEF", tmp_path)

    assert "--cookies-from-browser" in command
    assert command[command.index("--cookies-from-browser") + 1] == "edge"
    assert "--js-runtimes" in command
    assert "--remote-components" in command


def test_automation_search_and_selection_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = client.post(
        "/projects",
        json={"name": "Automático", "description": "", "hashtag": "#podcast"},
    ).json()
    candidates = [
        AutomationCandidateRead(
            video_id="clip123_XYZ",
            title="Melhor momento",
            description="#podcast",
            channel_title="Canal",
            url="https://www.youtube.com/watch?v=clip123_XYZ",
            thumbnail_url="https://i.ytimg.com/vi/clip123_XYZ/hqdefault.jpg",
            published_at=None,
            views=25_000,
            duration_seconds=42,
            relevance_score=0.91,
        )
    ]

    async def fake_search(*args, **kwargs):
        return candidates

    async def fake_details(*args, **kwargs):
        return candidates

    monkeypatch.setattr(YoutubeService, "search_automation_candidates", fake_search)
    monkeypatch.setattr(YoutubeService, "get_automation_video_details", fake_details)
    monkeypatch.setattr(AutomationService, "process_run", lambda *args, **kwargs: None)

    search_response = client.post(
        f"/projects/{project['id']}/automation/search",
        json={
            "source_url": "https://www.youtube.com/watch?v=source123_X",
            "hashtag": "#podcast",
        },
    )
    assert search_response.status_code == 200
    assert search_response.json()["source_video_id"] == "source123_X"
    assert search_response.json()["candidates"][0]["video_id"] == "clip123_XYZ"

    start_response = client.post(
        f"/projects/{project['id']}/automation/runs",
        json={
            "source_url": "https://www.youtube.com/watch?v=source123_X",
            "hashtag": "podcast",
            "selected_video_ids": ["clip123_XYZ"],
        },
    )
    assert start_response.status_code == 202
    run = start_response.json()
    assert run["status"] == "queued"
    assert run["total_references"] == 1

    poll_response = client.get(
        f"/projects/{project['id']}/automation/runs/{run['id']}"
    )
    assert poll_response.status_code == 200
    assert poll_response.json()["id"] == run["id"]

    detail = client.get(f"/projects/{project['id']}").json()
    assert detail["reference_videos"][0]["external_id"] == "clip123_XYZ"
    assert detail["reference_videos"][0]["status"] == "queued"


def test_youtube_search_scores_and_excludes_original() -> None:
    service = YoutubeService()
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_request(endpoint: str, parameters: dict[str, object]):
        calls.append((endpoint, parameters))
        if endpoint == service.search_endpoint:
            return {
                "items": [
                    {"id": {"videoId": "source123_X"}},
                    {"id": {"videoId": "clipAAA_123"}},
                    {"id": {"videoId": "clipBBB_456"}},
                ]
            }
        return {
            "items": [
                {
                    "id": "clipAAA_123",
                    "snippet": {
                        "title": "Corte A",
                        "description": "#podcast",
                        "channelTitle": "Canal A",
                        "publishedAt": "2026-11-20T12:00:00Z",
                        "thumbnails": {},
                    },
                    "statistics": {"viewCount": "50000"},
                    "contentDetails": {"duration": "PT45S"},
                },
                {
                    "id": "clipBBB_456",
                    "snippet": {
                        "title": "Corte B",
                        "description": "#podcast",
                        "channelTitle": "Canal B",
                        "publishedAt": "2026-11-19T12:00:00Z",
                        "thumbnails": {},
                    },
                    "statistics": {"viewCount": "1000"},
                    "contentDetails": {"duration": "PT1M10S"},
                },
            ]
        }

    service._request = fake_request  # type: ignore[method-assign]
    results = asyncio.run(
        service.search_automation_candidates(
            "podcast", source_video_id="source123_X", limit=10
        )
    )

    assert [result.video_id for result in results] == ["clipAAA_123", "clipBBB_456"]
    assert results[0].relevance_score > results[1].relevance_score
    assert all(result.video_id != "source123_X" for result in results)
    assert calls[0][1]["q"] == "#podcast"
    assert calls[0][1]["videoDuration"] == "short"


@pytest.fixture()
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    previous_storage_path = settings.storage_path
    settings.storage_path = str(tmp_path / "storage")

    def override_get_db() -> Generator[Session, None, None]:
        database = test_session()
        try:
            yield database
        finally:
            database.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    settings.storage_path = previous_storage_path
    Base.metadata.drop_all(bind=engine)


def test_create_list_and_upload_source_video(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/health").json() == {"status": "ok"}

    create_response = client.post(
        "/projects",
        json={"name": "Podcast da Semana", "description": "Episódio de estreia", "hashtag": "#podcast"},
    )
    assert create_response.status_code == 201
    project = create_response.json()

    list_response = client.get("/projects")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [project["id"]]

    ranking_response = client.get(
        f"/projects/{project['id']}/youtube/ranking?metric=view_delta"
    )
    assert ranking_response.status_code == 200
    assert ranking_response.json() == {
        "hashtag": "#podcast",
        "snapshot_date": None,
        "metric": "view_delta",
        "videos": [],
    }

    upload_response = client.post(
        f"/projects/{project['id']}/upload",
        files={"file": ("original.mp4", b"video content", "video/mp4")},
    )
    assert upload_response.status_code == 201
    uploaded_video = upload_response.json()
    assert uploaded_video["filename"] == "original.mp4"
    assert uploaded_video["size"] == len(b"video content")
    assert uploaded_video["status"] == "uploaded"
    assert (tmp_path / "storage" / uploaded_video["path"]).is_file()

    detail_response = client.get(f"/projects/{project['id']}")
    assert detail_response.status_code == 200
    assert len(detail_response.json()["source_videos"]) == 1

    reference_response = client.post(
        f"/projects/{project['id']}/reference-videos/upload",
        files={"file": ("corte.mp4", b"reference video content", "video/mp4")},
    )
    assert reference_response.status_code == 201
    assert reference_response.json()["source"] == "upload"

    multipart_response = client.post(
        f"/projects/{project['id']}/uploads/multipart",
        json={
            "filename": "original-grande.mp4",
            "content_type": "video/mp4",
            "size": 1_500_000_000,
            "kind": "source",
        },
    )
    assert multipart_response.status_code == 503

    updated_detail_response = client.get(f"/projects/{project['id']}")
    assert len(updated_detail_response.json()["reference_videos"]) == 1

    delete_response = client.delete(f"/projects/{project['id']}")
    assert delete_response.status_code == 204
    assert client.get(f"/projects/{project['id']}").status_code == 404
    assert not (tmp_path / "storage" / "projects" / project["id"]).exists()


def test_delete_uploaded_and_youtube_references(client: TestClient, tmp_path: Path) -> None:
    project = client.post(
        "/projects",
        json={"name": "Referências", "description": "", "hashtag": "#teste"},
    ).json()
    uploaded = client.post(
        f"/projects/{project['id']}/reference-videos/upload",
        files={"file": ("corte.mp4", b"reference video content", "video/mp4")},
    ).json()
    youtube = client.post(
        f"/projects/{project['id']}/reference-videos/youtube",
        json={
            "video_id": "youtube-123",
            "title": "Corte salvo por engano",
            "url": "https://www.youtube.com/watch?v=youtube-123",
        },
    ).json()
    uploaded_path = tmp_path / "storage" / uploaded["path"]
    assert uploaded_path.is_file()

    uploaded_delete = client.delete(
        f"/projects/{project['id']}/reference-videos/{uploaded['id']}"
    )
    youtube_delete = client.delete(
        f"/projects/{project['id']}/reference-videos/{youtube['id']}"
    )

    assert uploaded_delete.status_code == 204
    assert youtube_delete.status_code == 204
    assert not uploaded_path.exists()
    assert client.get(f"/projects/{project['id']}").json()["reference_videos"] == []
    assert (
        client.delete(f"/projects/{project['id']}/reference-videos/{youtube['id']}").status_code
        == 404
    )


def test_deleting_reference_also_deletes_its_generated_clips() -> None:
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    database_factory = sessionmaker(bind=engine)

    with database_factory() as database:
        project = Project(name="Projeto")
        database.add(project)
        database.flush()
        source = SourceVideo(
            project_id=project.id,
            filename="original.mp4",
            path="projects/original.mp4",
            size=100,
            status="uploaded",
        )
        reference = ReferenceVideo(
            project_id=project.id,
            source="upload",
            title="referencia.mp4",
            filename="referencia.mp4",
            path="projects/referencia.mp4",
            size=50,
            status="uploaded",
        )
        database.add_all((source, reference))
        database.flush()
        clip = GeneratedClip(
            project_id=project.id,
            source_video_id=source.id,
            reference_video_id=reference.id,
            status="failed",
        )
        database.add(clip)
        database.commit()
        clip_id = clip.id

        loaded_reference = ReferenceVideoRepository().get_by_id(
            database,
            project.id,
            reference.id,
        )
        assert loaded_reference is not None
        ReferenceVideoRepository().delete(database, loaded_reference)

        assert database.get(GeneratedClip, clip_id) is None

    Base.metadata.drop_all(bind=engine)


def test_audio_matching_finds_reference_position() -> None:
    import numpy as np

    generator = np.random.default_rng(42)
    source_envelope = generator.normal(loc=0.5, scale=0.15, size=4_000)
    expected_start_frame = 1_350
    reference_envelope = source_envelope[expected_start_frame : expected_start_frame + 500].copy()

    start_frame, score = AudioMatchingService().find_match_in_envelopes(
        source_envelope, reference_envelope
    )

    assert start_frame == expected_start_frame
    assert score > 0.99


def test_spectral_matching_distinguishes_equal_volume_audio() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    samples_per_tone = sample_rate // 2
    generator = np.random.default_rng(21)
    available_frequencies = np.array([120, 240, 480, 820, 1_250, 1_720])
    frequencies = generator.choice(available_frequencies, size=60)
    time = np.arange(samples_per_tone) / sample_rate
    source_samples = np.concatenate(
        [np.sin(2 * np.pi * frequency * time) for frequency in frequencies]
    ).astype(np.float32)
    expected_start_seconds = 10.0
    reference_samples = source_samples[
        int(expected_start_seconds * sample_rate) : int(
            (expected_start_seconds + 8) * sample_rate
        )
    ].copy()

    source_features = matching_service._spectral_features_from_samples(source_samples)
    reference_features = matching_service._spectral_features_from_samples(reference_samples)
    start_frame, score = matching_service.find_match_in_features(
        source_features, reference_features
    )

    matched_start_seconds = start_frame / matching_service.feature_rate
    assert abs(matched_start_seconds - expected_start_seconds) <= 0.05
    assert score > 0.8


def test_fingerprint_does_not_inflate_unrelated_audio_confidence() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    generator = np.random.default_rng(84)
    source_samples = generator.normal(size=sample_rate * 30).astype(np.float32)
    unrelated_samples = generator.normal(size=sample_rate * 8).astype(np.float32)

    source_features = matching_service._spectral_features_from_samples(source_samples)
    unrelated_features = matching_service._spectral_features_from_samples(unrelated_samples)
    _, score, _ = matching_service._find_best_tempo_match(
        source_features,
        unrelated_features,
    )

    assert score < matching_service.minimum_score


def test_confidence_uses_distance_from_the_second_best_candidate() -> None:
    import numpy as np

    clear_scores = np.array([0.10, 0.08, 0.40, 0.09, 0.10])
    ambiguous_scores = np.array([0.39, 0.08, 0.40, 0.09, 0.10])

    clear_confidence = AudioMatchingService._calibrate_match_confidence(
        clear_scores,
        best_frame=2,
        reference_frame_count=1,
    )
    ambiguous_confidence = AudioMatchingService._calibrate_match_confidence(
        ambiguous_scores,
        best_frame=2,
        reference_frame_count=1,
    )

    assert clear_confidence > 0.7
    assert ambiguous_confidence < AudioMatchingService.minimum_score


def test_tempo_search_reuses_source_fft(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(72)
    source_features = generator.normal(size=(1_000, 12))
    reference_features = source_features[400:560].copy()
    original_rfft = np.fft.rfft
    source_fft_calls = 0

    def counted_rfft(values: np.ndarray, *args: object, **kwargs: object) -> np.ndarray:
        nonlocal source_fft_calls
        if values.shape == source_features.shape:
            source_fft_calls += 1
        return original_rfft(values, *args, **kwargs)

    monkeypatch.setattr(np.fft, "rfft", counted_rfft)
    matching_service._find_best_tempo_match(source_features, reference_features)

    assert source_fft_calls == 2


def test_dtw_refines_locally_variable_speed() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(93)
    source_features = generator.normal(size=(600, 12))
    true_start_frame = 200
    true_end_frame = 380
    source_segment = source_features[true_start_frame:true_end_frame]
    first_half = np.linspace(0, 80, 70, endpoint=False)
    second_half = np.linspace(80, source_segment.shape[0] - 1, 90)
    warped_positions = np.concatenate((first_half, second_half))
    reference_features = np.column_stack(
        [
            np.interp(
                warped_positions,
                np.arange(source_segment.shape[0]),
                source_segment[:, column],
            )
            for column in range(source_segment.shape[1])
        ]
    )
    initial_match = AudioSegmentMatch(
        source_start_seconds=9.5,
        reference_start_seconds=0,
        duration_seconds=9,
        reference_duration_seconds=8,
        tempo_factor=1.125,
        score=0.7,
    )

    refined = matching_service._refine_match_with_dtw(
        initial_match,
        source_features,
        reference_features,
    )

    assert abs(refined.source_start_seconds - 10) <= 0.1
    assert abs(refined.duration_seconds - 9) <= 0.2
    assert refined.score >= initial_match.score


def test_real_reencoded_and_accelerated_audio_is_located_at_correct_time(
    tmp_path: Path,
) -> None:
    media_service = MediaProcessingService()
    ffmpeg = media_service.get_ffmpeg_executable()
    source_path = tmp_path / "source.wav"
    reference_path = tmp_path / "reference.m4a"
    expected_start_seconds = 9.25

    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.5*sin(2*PI*(180+35*floor(t))*t):s=16000:d=24",
            "-c:a",
            "pcm_s16le",
            str(source_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(expected_start_seconds),
            "-t",
            "7",
            "-i",
            str(source_path),
            "-af",
            "atempo=1.08",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(reference_path),
        ],
        check=True,
    )

    matched_start, duration, score = AudioMatchingService(media_service).find_match(
        source_path, reference_path
    )

    assert abs(matched_start - expected_start_seconds) <= 0.2
    assert 6.8 <= duration <= 7.2
    assert score >= AudioMatchingService.minimum_score


def test_real_reencoded_audio_starting_between_analysis_frames_has_high_confidence(
    tmp_path: Path,
) -> None:
    media_service = MediaProcessingService()
    ffmpeg = media_service.get_ffmpeg_executable()
    source_path = tmp_path / "off-grid-source.wav"
    reference_path = tmp_path / "off-grid-reference.m4a"
    expected_start_seconds = 9.273

    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.45*sin(2*PI*(220+90*sin(2*PI*0.7*t))*t)"
            "+0.2*sin(2*PI*(730+140*sin(2*PI*0.31*t))*t):s=16000:d=24",
            "-c:a",
            "pcm_s16le",
            str(source_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(expected_start_seconds),
            "-t",
            "8",
            "-i",
            str(source_path),
            "-af",
            "volume=0.35",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(reference_path),
        ],
        check=True,
    )

    matched_start, duration, score = AudioMatchingService(media_service).find_match(
        source_path,
        reference_path,
    )

    assert abs(matched_start - expected_start_seconds) <= 0.15
    assert 7.8 <= duration <= 8.2
    assert score >= 0.75


def test_segmented_matching_reconstructs_non_contiguous_reference() -> None:
    import numpy as np

    generator = np.random.default_rng(7)
    source_envelope = generator.normal(loc=0.5, scale=0.15, size=5_000)
    reference_envelope = np.concatenate(
        (
            source_envelope[400:800],
            source_envelope[1_600:2_000],
            source_envelope[3_200:3_600],
        )
    )

    matches = AudioMatchingService().find_segmented_matches_in_envelopes(
        source_envelope, reference_envelope
    )

    assert [round(match.source_start_seconds) for match in matches] == [8, 32, 64]
    assert all(match.score > 0.99 for match in matches)


def test_waveform_refinement_finds_precise_offset() -> None:
    import numpy as np

    generator = np.random.default_rng(16)
    source_samples = generator.normal(size=20_000)
    expected_start = 6_250
    reference_samples = source_samples[expected_start : expected_start + 4_000].copy()

    start, score = AudioMatchingService._find_best_match_in_samples(
        source_samples, reference_samples
    )

    assert start == expected_start
    assert score > 0.99


def test_waveform_refinement_keeps_confirmed_spectral_match_when_audio_was_reencoded() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(31)
    source_samples = generator.normal(size=80_000)
    reencoded_reference_samples = generator.normal(size=16_000)
    spectral_match = AudioSegmentMatch(
        source_start_seconds=10,
        reference_start_seconds=0,
        duration_seconds=4,
        score=0.8,
    )

    refined_match = matching_service._refine_segment_match(
        spectral_match,
        source_samples,
        reencoded_reference_samples,
    )

    assert refined_match == spectral_match


def test_waveform_refinement_does_not_lower_a_confirmed_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    spectral_match = AudioSegmentMatch(
        source_start_seconds=10,
        reference_start_seconds=0,
        duration_seconds=4,
        score=0.8,
    )
    monkeypatch.setattr(
        matching_service,
        "_find_best_match_in_samples",
        lambda source, reference: (100, 0.2),
    )

    refined_match = matching_service._refine_segment_match(
        spectral_match,
        np.ones(80_000),
        np.ones(16_000),
    )

    assert refined_match.score == spectral_match.score


def test_lower_confidence_blocks_still_require_majority_coverage() -> None:
    matching_service = AudioMatchingService()
    matches = [
        AudioSegmentMatch(
            source_start_seconds=index * 10,
            reference_start_seconds=index * 8,
            duration_seconds=8,
            reference_duration_seconds=8,
            score=0.34,
        )
        for index in range(2)
    ]

    with pytest.raises(AudioMatchNotFoundError, match="cobertura 50%"):
        matching_service._validate_reference_coverage(matches, reference_duration=32)

    matches.append(
        AudioSegmentMatch(
            source_start_seconds=20,
            reference_start_seconds=16,
            duration_seconds=8,
            reference_duration_seconds=8,
            score=0.34,
        )
    )
    matching_service._validate_reference_coverage(matches, reference_duration=32)


def test_contiguous_fallback_reuses_the_same_audio_analysis() -> None:
    import numpy as np

    analysis = AudioAnalysis(
        source_samples=np.zeros(20_000),
        reference_samples=np.zeros(4_000),
        source_features=np.zeros((100, 12)),
        reference_features=np.zeros((20, 12)),
    )
    expected_match = AudioSegmentMatch(
        source_start_seconds=12,
        reference_start_seconds=0,
        duration_seconds=4,
        score=0.7,
    )

    class MatchingServiceWithFallback(AudioMatchingService):
        load_count = 0

        def _load_analysis(self, source_path: str, reference_path: str) -> AudioAnalysis:
            self.load_count += 1
            return analysis

        def _find_contiguous_match(self, loaded: AudioAnalysis) -> AudioSegmentMatch:
            raise AudioMatchNotFoundError("referência editada")

        def _find_segmented_matches_from_analysis(
            self, loaded: AudioAnalysis
        ) -> list[AudioSegmentMatch]:
            return [expected_match]

    matching_service = MatchingServiceWithFallback()

    assert matching_service.find_matches("source.mp4", "reference.mp4") == [expected_match]
    assert matching_service.load_count == 1


def test_audio_extraction_explains_when_file_has_no_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    media_service = MediaProcessingService()
    monkeypatch.setattr(media_service, "get_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(
        "app.services.media_processing_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stderr=b"Output file does not contain any stream",
        ),
    )

    with pytest.raises(MediaProcessingError, match="não possui uma faixa de áudio"):
        media_service.extract_audio_envelope(Path("video-sem-audio.mp4"))


def test_probe_identifies_video_and_audio_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    media_service = MediaProcessingService()
    monkeypatch.setattr(media_service, "get_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(
        "app.services.media_processing_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr=(
                "Duration: 00:01:02.50, start: 0.000000, bitrate: 1000 kb/s\n"
                "Stream #0:0(und): Video: h264\n"
                "Stream #0:1(und): Audio: aac"
            ),
        ),
    )

    assert media_service.probe("video.mp4") == MediaProbe(
        duration_seconds=62.5,
        has_video=True,
        has_audio=True,
    )


def test_generate_clip_retries_in_1080p_when_full_resolution_exceeds_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media_service = MediaProcessingService()
    monkeypatch.setattr(media_service, "get_ffmpeg_executable", lambda: "ffmpeg")
    commands: list[list[str]] = []
    results = iter(
        (
            subprocess.CompletedProcess(args=[], returncode=-9, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
        )
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return next(results)

    monkeypatch.setattr("app.services.media_processing_service.subprocess.run", fake_run)

    media_service.generate_clip(
        source_path="original.mp4",
        output_path=tmp_path / "clip.mp4",
        start_seconds=10,
        duration_seconds=20,
    )

    assert commands[0].index("-threads") < commands[0].index("-i")
    assert commands[1][commands[1].index("-c:v") + 1] == "libx264"
    assert "scale=1920:1920" in commands[1][commands[1].index("-vf") + 1]


def test_failed_clip_error_preserves_the_end() -> None:
    message = "começo" + ("x" * 2500) + "causa real"

    formatted = GeneratedClipRepository._format_error_message(message)

    assert len(formatted) == 2000
    assert formatted.startswith("começo")
    assert formatted.endswith("causa real")


def test_multipart_upload_uses_bucket_parts() -> None:
    class FakeProjectService:
        def get_by_id(self, database: object, project_id: object) -> object:
            return object()

    class FakeStorage:
        bucket_enabled = True

        def start_multipart_upload(self, *, object_key: str, content_type: str) -> str:
            assert object_key.endswith(".mp4")
            assert "/source/" in object_key
            assert content_type == "video/mp4"
            return "upload-123"

    project_id = uuid.uuid4()
    service = MultipartUploadService(
        storage=FakeStorage(),  # type: ignore[arg-type]
        project_service=FakeProjectService(),  # type: ignore[arg-type]
    )
    response = service.start(
        object(),  # type: ignore[arg-type]
        project_id=project_id,
        payload=MultipartUploadStart(
            filename="video-original.mp4",
            content_type="video/mp4",
            size=(16 * 1024 * 1024) + 1,
            kind="source",
        ),
    )

    assert response.upload_id == "upload-123"
    assert response.part_count == 2
    assert response.object_key.startswith(f"projects/{project_id}/source/")


def test_storage_service_formats_bucket_paths() -> None:
    stored_path = StorageService.bucket_path("projects/example/video.mp4")
    assert stored_path == "bucket://projects/example/video.mp4"
    assert StorageService.is_bucket_path(stored_path)
    assert StorageService.object_key(stored_path) == "projects/example/video.mp4"


def test_daily_hashtag_ranking_calculates_delta_and_reuses_same_search() -> None:
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    database_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    target_date = date(2026, 9, 2)

    class FakeYoutubeService:
        calls = 0

        async def search_hashtag_statistics(
            self,
            hashtag: str,
            *,
            limit: int,
            carryover_video_ids: object = (),
        ) -> list[YoutubeVideoDetails]:
            self.calls += 1
            assert hashtag == "#podcast"
            assert limit >= 1
            return [
                YoutubeVideoDetails(
                    video_id="video-123",
                    title="Corte em alta",
                    channel_title="Canal",
                    thumbnail_url="https://i.ytimg.com/vi/video-123/hqdefault.jpg",
                    published_at=None,
                    views=1_450,
                )
            ]

    fake_youtube = FakeYoutubeService()
    with database_factory() as database:
        projects = [
            Project(name="Projeto A", hashtag="#podcast"),
            Project(name="Projeto B", hashtag="podcast"),
        ]
        database.add_all(projects)
        database.flush()
        database.add_all(
            [
                HashtagVideoDailyStat(
                    project_id=project.id,
                    hashtag="#podcast",
                    video_id="video-123",
                    snapshot_date=target_date - timedelta(days=1),
                    title="Corte em alta",
                    channel_title="Canal",
                    views=1_000,
                )
                for project in projects
            ]
        )
        database.commit()
        project_ids = [project.id for project in projects]

        result = asyncio.run(
            HashtagRankingService(youtube=fake_youtube).sync_all(
                database,
                snapshot_date=target_date,
            )
        )

        assert fake_youtube.calls == 1
        assert result.projects_updated == 2
        for project_id in project_ids:
            stats = HashtagVideoDailyStatRepository().list_for_date(
                database,
                project_id,
                target_date,
                metric="view_delta",
                limit=10,
            )
            assert len(stats) == 1
            assert stats[0].view_delta == 450

    Base.metadata.drop_all(bind=engine)


def test_ranking_uses_total_views_as_configurable_metric() -> None:
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    database_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with database_factory() as database:
        project = Project(name="Projeto", hashtag="#clips")
        database.add(project)
        database.flush()
        database.add_all(
            [
                HashtagVideoDailyStat(
                    project_id=project.id,
                    hashtag="#clips",
                    video_id="older-hit",
                    snapshot_date=date(2026, 9, 2),
                    title="Mais visto",
                    channel_title="Canal A",
                    views=50_000,
                    view_delta=100,
                ),
                HashtagVideoDailyStat(
                    project_id=project.id,
                    hashtag="#clips",
                    video_id="trending-now",
                    snapshot_date=date(2026, 9, 2),
                    title="Em alta",
                    channel_title="Canal B",
                    views=10_000,
                    view_delta=5_000,
                ),
            ]
        )
        database.commit()

        ranking = HashtagRankingService().list_for_project(
            database,
            project_id=project.id,
            metric=YoutubeRankingMetric.views,
            limit=10,
        )

        assert [video.video_id for video in ranking.videos] == ["older-hit", "trending-now"]

    Base.metadata.drop_all(bind=engine)


def test_manual_ranking_refresh_updates_only_requested_project() -> None:
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    database_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    class FakeYoutubeService:
        calls = 0

        async def search_hashtag_statistics(
            self,
            hashtag: str,
            *,
            limit: int,
            carryover_video_ids: object = (),
        ) -> list[YoutubeVideoDetails]:
            self.calls += 1
            assert hashtag == "#clips"
            return [
                YoutubeVideoDetails(
                    video_id="manual-refresh",
                    title="Atualizado agora",
                    channel_title="Canal",
                    thumbnail_url=None,
                    published_at=None,
                    views=2_500,
                )
            ]

    fake_youtube = FakeYoutubeService()
    with database_factory() as database:
        project = Project(name="Projeto", hashtag="#clips")
        untouched = Project(name="Outro", hashtag="#outro")
        database.add_all([project, untouched])
        database.commit()

        ranking = asyncio.run(
            HashtagRankingService(youtube=fake_youtube).sync_project(
                database,
                project_id=project.id,
                snapshot_date=date(2026, 9, 2),
            )
        )

        assert fake_youtube.calls == 1
        assert [video.video_id for video in ranking.videos] == ["manual-refresh"]
        assert HashtagVideoDailyStatRepository().latest_date(database, untouched.id) is None

    Base.metadata.drop_all(bind=engine)


def test_weekly_ranking_returns_top_three_by_view_growth() -> None:
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    database_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with database_factory() as database:
        project = Project(name="Projeto", hashtag="#clips")
        database.add(project)
        database.flush()
        start = date(2026, 8, 27)
        end = date(2026, 9, 2)
        growth_by_video = {"a": 100, "b": 900, "c": 300, "d": 500}
        for video_id, growth in growth_by_video.items():
            database.add_all(
                [
                    HashtagVideoDailyStat(
                        project_id=project.id,
                        hashtag="#clips",
                        video_id=video_id,
                        snapshot_date=start,
                        title=f"Vídeo {video_id}",
                        channel_title="Canal",
                        views=1_000,
                    ),
                    HashtagVideoDailyStat(
                        project_id=project.id,
                        hashtag="#clips",
                        video_id=video_id,
                        snapshot_date=end,
                        title=f"Vídeo {video_id}",
                        channel_title="Canal",
                        views=1_000 + growth,
                    ),
                ]
            )
        database.commit()

        ranking = HashtagRankingService().list_weekly_for_project(
            database,
            project_id=project.id,
        )

        assert ranking.period_start == start
        assert ranking.period_end == end
        assert [video.video_id for video in ranking.videos] == ["b", "d", "c"]
        assert [video.weekly_view_delta for video in ranking.videos] == [900, 500, 300]

    Base.metadata.drop_all(bind=engine)


def test_youtube_download_link_is_extracted_on_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_command: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="https://temporary.googlevideo.com/video.mp4\n",
            stderr="",
        )

    monkeypatch.setattr("app.services.youtube_download_service.subprocess.run", fake_run)

    url = YoutubeDownloadService().get_temporary_download_url("abc123_DEF")

    assert url == "https://temporary.googlevideo.com/video.mp4"
    assert "youtube:player_client=default,web_embedded" in captured_command
    assert "--get-url" in captured_command


def test_youtube_download_uses_base64_cookies_without_persisting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_contents = (
        b"# Netscape HTTP Cookie File\n"
        b".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest-value\n"
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cookies_index = command.index("--cookies") + 1
        cookies_path = Path(command[cookies_index])
        assert cookies_path.is_file()
        assert cookies_path.read_bytes() == cookie_contents
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="https://temporary.googlevideo.com/video.mp4\n",
            stderr="",
        )

    monkeypatch.setattr(
        settings,
        "youtube_cookies_base64",
        base64.b64encode(cookie_contents).decode("ascii"),
    )
    monkeypatch.setattr("app.services.youtube_download_service.subprocess.run", fake_run)

    assert YoutubeDownloadService().get_temporary_download_url("abc123_DEF").startswith("https://")


def test_regular_uploads_use_bucket_when_configured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bucket_name", "test-bucket")
    monkeypatch.setattr(settings, "bucket_access_key_id", "test-access")
    monkeypatch.setattr(settings, "bucket_secret_access_key", "test-secret")
    monkeypatch.setattr(settings, "bucket_endpoint", "https://storage.example.test")

    def fake_upload_file(
        storage: StorageService,
        local_path: Path,
        *,
        object_key: str,
        content_type: str,
    ) -> str:
        assert local_path.is_file()
        assert content_type == "video/mp4"
        return storage.bucket_path(object_key)

    monkeypatch.setattr(StorageService, "upload_file", fake_upload_file)
    project = client.post("/projects", json={"name": "Bucket"}).json()

    source = client.post(
        f"/projects/{project['id']}/upload",
        files={"file": ("original.mp4", b"source", "video/mp4")},
    )
    reference = client.post(
        f"/projects/{project['id']}/reference-videos/upload",
        files={"file": ("reference.mp4", b"reference", "video/mp4")},
    )

    assert source.status_code == 201
    assert reference.status_code == 201
    assert source.json()["path"].startswith("bucket://")
    assert reference.json()["path"].startswith("bucket://")
