import asyncio
import base64
import subprocess
import uuid
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
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
    ShortWindowMatchCandidate,
)
from app.services.multipart_upload_service import MultipartUploadService
from app.services.storage_service import StorageService
from app.services.youtube_download_service import YoutubeDownloadError, YoutubeDownloadService
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
    assert command[command.index("--js-runtimes") + 1] == (
        "node:C:/Program Files/nodejs/node.exe"
    )
    assert command[command.index("--remote-components") + 1] == "ejs:github"


def test_youtube_download_retries_with_default_clients_when_formats_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(settings, "youtube_cookies_base64", None)
    monkeypatch.setattr(settings, "youtube_cookies_file", None)
    monkeypatch.setattr(settings, "youtube_cookies_from_browser", None)
    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr("app.services.youtube_download_service.shutil.which", lambda _: None)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="ERROR: Requested format is not available",
            )
        downloaded_file = tmp_path / "source.mp4"
        downloaded_file.write_bytes(b"video")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=f"{downloaded_file}\n",
            stderr="",
        )

    monkeypatch.setattr("app.services.youtube_download_service.subprocess.run", fake_run)

    result = YoutubeDownloadService().download_video(
        "abc123_DEF", tmp_path, filename_stem="source"
    )

    assert result == tmp_path / "source.mp4"
    assert len(calls) == 2
    assert "--extractor-args" in calls[0]
    assert "--extractor-args" not in calls[1]


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
        return candidates, "page-two", datetime(2026, 11, 15, tzinfo=timezone.utc)

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
    assert search_response.json()["next_page_token"] == "page-two"
    assert search_response.json()["search_window_start"] == "2026-11-15T00:00:00Z"

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
                ],
                "nextPageToken": "next-page",
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
    results, next_page_token, search_window_start = asyncio.run(
        service.search_automation_candidates(
            "podcast", source_video_id="source123_X", limit=10
        )
    )

    assert [result.video_id for result in results] == ["clipAAA_123", "clipBBB_456"]
    assert next_page_token == "next-page"
    assert search_window_start.tzinfo is not None
    assert results[0].relevance_score > results[1].relevance_score
    assert all(result.video_id != "source123_X" for result in results)
    assert calls[0][1]["q"] == "#podcast"
    assert calls[0][1]["videoDuration"] == "short"

    recent_results, _, recent_window_start = asyncio.run(
        service.search_automation_candidates(
            "podcast",
            source_video_id="source123_X",
            limit=10,
            sort_by="recent",
            page_token="next-page",
        )
    )
    assert [result.video_id for result in recent_results] == ["clipAAA_123", "clipBBB_456"]
    assert calls[2][1]["order"] == "date"
    assert calls[2][1]["pageToken"] == "next-page"

    _, _, continued_window_start = asyncio.run(
        service.search_automation_candidates(
            "podcast",
            source_video_id="source123_X",
            limit=10,
            sort_by="recent",
            page_token="next-page",
            published_after=recent_window_start,
        )
    )
    assert calls[4][1]["publishedAfter"] == recent_window_start.isoformat().replace("+00:00", "Z")
    assert continued_window_start == recent_window_start

    most_viewed_results, _, _ = asyncio.run(
        service.search_automation_candidates(
            "podcast", source_video_id="source123_X", limit=10, sort_by="views"
        )
    )
    assert [result.video_id for result in most_viewed_results] == ["clipAAA_123", "clipBBB_456"]
    assert calls[6][1]["order"] == "viewCount"


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


def test_speech_band_detail_above_two_khz_improves_location_matching() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    rng = np.random.default_rng(151)
    half_second_frequencies = rng.choice(
        np.array([2_200, 2_600, 3_000, 3_400, 3_700]),
        size=48,
    )
    frequencies = np.repeat(half_second_frequencies, sample_rate // 2)
    phase = np.cumsum(2 * np.pi * frequencies / sample_rate)
    time = np.arange(frequencies.size) / sample_rate
    source_samples = (
        0.25 * np.sin(2 * np.pi * 180 * time) + 0.75 * np.sin(phase)
    ).astype(np.float32)
    expected_start_seconds = 7.25
    reference_samples = source_samples[
        round(expected_start_seconds * sample_rate) : round(
            (expected_start_seconds + 8) * sample_rate
        )
    ]

    source_features = matching_service._spectral_features_from_samples(source_samples)
    reference_features = matching_service._spectral_features_from_samples(reference_samples)
    start_frame, score = matching_service.find_match_in_features(
        source_features,
        reference_features,
    )

    decoy_frequencies = np.repeat(
        np.random.default_rng(152).choice(
            np.array([2_200, 2_600, 3_000, 3_400, 3_700]),
            size=48,
        ),
        sample_rate // 2,
    )
    decoy_phase = np.cumsum(2 * np.pi * decoy_frequencies / sample_rate)
    decoy_samples = (
        0.25 * np.sin(2 * np.pi * 180 * time) + 0.75 * np.sin(decoy_phase)
    ).astype(np.float32)
    _, decoy_score, _ = matching_service._find_best_tempo_match(
        matching_service._spectral_features_from_samples(decoy_samples),
        reference_features,
    )
    segmented_matches = matching_service.find_segmented_matches_in_features(
        source_features,
        reference_features,
    )

    assert abs(start_frame / matching_service.feature_rate - expected_start_seconds) <= 0.15
    assert score >= 0.60
    assert score > decoy_score + 0.15
    assert segmented_matches[0].source_start_seconds == pytest.approx(
        expected_start_seconds,
        abs=0.15,
    )


def test_fingerprint_does_not_inflate_unrelated_audio_confidence() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    generator = np.random.default_rng(84)
    source_samples = generator.normal(size=sample_rate * 30).astype(np.float32)
    source_features = matching_service._spectral_features_from_samples(source_samples)
    search_indexes = matching_service._build_feature_search_indexes(source_features)

    for seed in range(85, 90):
        unrelated_samples = np.random.default_rng(seed).normal(
            size=sample_rate * 8
        ).astype(np.float32)
        unrelated_features = matching_service._spectral_features_from_samples(
            unrelated_samples
        )
        _, score, _ = matching_service._find_best_tempo_match(
            source_features,
            unrelated_features,
            search_indexes=search_indexes,
        )

        assert score < matching_service.minimum_score
        with pytest.raises(AudioMatchNotFoundError):
            matching_service.find_segmented_matches_in_features(
                source_features,
                unrelated_features,
                search_indexes=search_indexes,
            )


def test_correlated_feature_views_do_not_multiply_their_confidence() -> None:
    import numpy as np

    combined = AudioMatchingService._combine_feature_evidence(
        np.array([0.5, 0.9]),
        np.array([0.5, 0.9]),
    )

    assert combined.tolist() == pytest.approx([0.5, 0.9])


def test_weak_unique_peak_cannot_become_a_high_confidence_match() -> None:
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

    assert clear_confidence > ambiguous_confidence
    assert clear_confidence < AudioMatchingService.minimum_score
    assert ambiguous_confidence < AudioMatchingService.minimum_score


def test_overlapping_low_similarity_windows_remain_low_confidence() -> None:
    import numpy as np

    scores = np.full(100, 0.10)
    scores[40] = 0.35
    scores[49] = 0.34  # Still overlaps the 16-frame winner by almost half.

    confidence = AudioMatchingService._calibrate_match_confidence(
        scores,
        best_frame=40,
        reference_frame_count=16,
    )

    assert confidence < AudioMatchingService.minimum_score


def test_repeated_non_overlapping_audio_still_counts_as_ambiguous() -> None:
    import numpy as np

    scores = np.full(100, 0.10)
    scores[20] = 0.35
    scores[40] = 0.34  # More than one full reference window away.

    confidence = AudioMatchingService._calibrate_match_confidence(
        scores,
        best_frame=20,
        reference_frame_count=16,
    )

    assert confidence < AudioMatchingService.minimum_score


def test_full_match_rejects_an_audio_segment_repeated_in_two_source_positions() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    reference_samples = np.random.default_rng(20261127).normal(
        size=sample_rate * 5
    ).astype(np.float32)
    gap_samples = np.random.default_rng(20261128).normal(
        size=sample_rate * 2
    ).astype(np.float32)
    source_samples = np.concatenate((reference_samples, gap_samples, reference_samples))

    source_features = matching_service._spectral_features_from_samples(source_samples)
    reference_features = matching_service._spectral_features_from_samples(reference_samples)
    best_match = matching_service._find_best_tempo_match(source_features, reference_features)

    assert best_match[1] < matching_service.minimum_score, f"Ambiguous result: {best_match}"
    with pytest.raises(AudioMatchNotFoundError, match="confiança"):
        matching_service.find_match_in_features(source_features, reference_features)


def test_segmented_match_rejects_a_reference_repeated_in_two_source_positions() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    reference_samples = np.random.default_rng(1201).normal(
        size=sample_rate * 8
    ).astype(np.float32)
    gap_samples = np.random.default_rng(1202).normal(
        size=sample_rate * 2
    ).astype(np.float32)
    tail_samples = np.random.default_rng(1203).normal(
        size=sample_rate * 4
    ).astype(np.float32)
    source_samples = np.concatenate(
        (reference_samples, gap_samples, reference_samples, tail_samples)
    )
    source_features = matching_service._spectral_features_from_samples(source_samples)
    reference_features = matching_service._spectral_features_from_samples(reference_samples)

    with pytest.raises(AudioMatchNotFoundError, match="confiança média"):
        matching_service.find_segmented_matches_in_features(
            source_features,
            reference_features,
        )


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
    source_features = generator.normal(size=(1_600, 12))
    true_start_frame = 500
    true_end_frame = 950
    source_segment = source_features[true_start_frame:true_end_frame]
    first_half = np.linspace(0, 200, 200, endpoint=False)
    second_half = np.linspace(200, source_segment.shape[0] - 1, 200)
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

    matches = AudioMatchingService(media_service).find_matches(
        source_path,
        reference_path,
    )

    assert matches
    assert abs(min(match.source_start_seconds for match in matches) - expected_start_seconds) <= 0.2
    assert AudioMatchingService._weighted_average_match_score(matches) >= (
        AudioMatchingService.minimum_average_match_score
    )


def test_matching_uses_video_time_when_source_audio_starts_late(tmp_path: Path) -> None:
    media_service = MediaProcessingService()
    ffmpeg = media_service.get_ffmpeg_executable()
    source_path = tmp_path / "source-audio-starts-late.mp4"
    reference_path = tmp_path / "reference-audio.m4a"
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
            "color=c=blue:s=160x120:r=25:d=24",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.45*sin(2*PI*(220+90*sin(2*PI*0.7*t))*t)"
            "+0.2*sin(2*PI*(730+140*sin(2*PI*0.31*t))*t):s=16000:d=23.5",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-shortest",
            str(source_path),
        ],
        check=True,
        capture_output=True,
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
            "-vn",
            "-af",
            "volume=0.35",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(reference_path),
        ],
        check=True,
        capture_output=True,
    )

    matches = AudioMatchingService(media_service).find_matches(source_path, reference_path)

    assert matches
    assert abs(min(match.source_start_seconds for match in matches) - expected_start_seconds) <= 0.2


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
    landmark_match = AudioMatchingService(media_service)._find_landmark_alignment(
        media_service.extract_audio_samples(
            source_path,
            sample_rate=AudioMatchingService.analysis_sample_rate,
        ),
        media_service.extract_audio_samples(
            reference_path,
            sample_rate=AudioMatchingService.analysis_sample_rate,
        ),
    )

    assert abs(matched_start - expected_start_seconds) <= 0.15
    assert 7.8 <= duration <= 8.2
    assert score >= 0.75
    assert landmark_match is not None
    assert landmark_match[0] == pytest.approx(expected_start_seconds, abs=0.12)


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


def test_fine_offset_scans_find_audio_across_unaligned_reference_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(2026)
    source_features = generator.normal(size=(2_000, 32)).astype(np.float32)
    source_features[:, 0] = np.arange(source_features.shape[0])
    reference_features = np.concatenate(
        [source_features[start : start + 200] for start in (200, 600, 1_000, 1_400)]
    )

    def score_unedited_reference_windows(
        _source: np.ndarray,
        reference: np.ndarray,
        *,
        search_indexes: object,
    ) -> tuple[int, float, float, int]:
        del search_indexes
        has_edit = bool(np.any(np.abs(np.diff(reference[:, 0])) > 10))
        return round(float(reference[0, 0])), 0.1 if has_edit else 0.95, 1.0, 0

    monkeypatch.setattr(
        matching_service,
        "_find_best_tempo_match_with_pitch",
        score_unedited_reference_windows,
    )
    search_indexes = matching_service._build_feature_search_indexes(source_features)

    coarse_matches, _ = matching_service._scan_reference_segments(
        source_features,
        reference_features,
        segment_seconds=matching_service.segment_seconds,
        offset_seconds=0,
        search_indexes=search_indexes,
    )
    coarse_coverage = matching_service._matched_reference_duration(coarse_matches)

    matches = matching_service.find_segmented_matches_in_features(
        source_features,
        reference_features,
        search_indexes=search_indexes,
    )
    fine_coverage = matching_service._matched_reference_duration(matches)

    required_coverage = reference_features.shape[0] / matching_service.feature_rate * 0.6
    assert coarse_coverage < required_coverage
    assert fine_coverage >= required_coverage
    assert len(matches) >= 4


def test_spectral_matching_reconstructs_reference_with_multiple_edits() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    duration = 28
    generator = np.random.default_rng(20261124)
    source_samples = generator.normal(size=sample_rate * duration).astype(np.float32)
    source_samples = (
        0.8 * source_samples + 0.2 * np.roll(source_samples, 12)
    ).astype(np.float32)
    expected_starts = (3.25, 12.5, 21.0)
    clip_duration = 4.0
    reference_samples = np.concatenate(
        [
            source_samples[
                round(start * sample_rate) : round((start + clip_duration) * sample_rate)
            ]
            for start in expected_starts
        ]
    )

    matches = matching_service.find_segmented_matches_in_features(
        matching_service._spectral_features_from_samples(source_samples),
        matching_service._spectral_features_from_samples(reference_samples),
        reference_scene_boundaries=(clip_duration, clip_duration * 2),
    )

    assert len(matches) == len(expected_starts)
    assert [match.source_start_seconds for match in matches] == pytest.approx(
        expected_starts,
        abs=0.25,
    )
    assert matching_service._weighted_average_match_score(matches) >= 0.75


def test_segmented_matching_preserves_reference_order_when_source_edits_are_reordered() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(20261130)
    source_features = generator.normal(size=(1_800, 32)).astype(np.float32)
    expected_source_starts = (20.0, 4.0, 12.0)
    segment_frames = 4 * matching_service.feature_rate
    reference_features = np.concatenate(
        [
            source_features[
                round(start * matching_service.feature_rate) :
                round(start * matching_service.feature_rate) + segment_frames
            ]
            for start in expected_source_starts
        ]
    )

    matches = matching_service.find_segmented_matches_in_features(
        source_features,
        reference_features,
        reference_scene_boundaries=(4.0, 8.0),
    )

    assert [match.reference_start_seconds for match in matches] == pytest.approx(
        (0.0, 4.0, 8.0),
        abs=0.05,
    ), f"Matching result: {matches}"
    assert [match.source_start_seconds for match in matches] == pytest.approx(
        expected_source_starts,
        abs=0.1,
    )


@pytest.mark.parametrize("visual_cuts", (True, False))
def test_real_aac_reference_with_multiple_edits_matches_each_source_region(
    tmp_path: Path,
    visual_cuts: bool,
) -> None:
    media_service = MediaProcessingService()
    ffmpeg = media_service.get_ffmpeg_executable()
    source_path = tmp_path / "multi-edit-source.wav"
    reference_audio_path = tmp_path / "multi-edit-reference.m4a"
    reference_path = tmp_path / "multi-edit-reference.mp4"
    expected_starts = (2.37, 13.63, 27.21)
    clip_duration = 4.37

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
                "+0.2*sin(2*PI*(730+140*sin(2*PI*0.31*t))*t):s=16000:d=36",
            "-c:a",
            "pcm_s16le",
            str(source_path),
        ],
        check=True,
    )
    filters = []
    for index, start in enumerate(expected_starts):
        end = start + clip_duration
        filters.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]"
        )
    filters.append("[a0][a1][a2]concat=n=3:v=0:a=1[out]")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(reference_audio_path),
        ],
        check=True,
    )
    reference_video_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if visual_cuts:
        for color in ("red", "blue", "green"):
            reference_video_command.extend(
                (
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s=320x240:r=30:d={clip_duration}",
                )
            )
        reference_video_command.extend(
            (
                "-i",
                str(reference_audio_path),
                "-filter_complex",
                "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                "-map",
                "[v]",
                "-map",
                "3:a",
            )
        )
    else:
        reference_video_command.extend(
            (
                "-f",
                "lavfi",
                "-i",
                f"color=c=blue:s=320x240:r=30:d={clip_duration * 3}",
                "-i",
                str(reference_audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            )
        )
    reference_video_command.extend(
        ("-c:v", "mpeg4", "-q:v", "5", "-c:a", "copy", str(reference_path))
    )
    subprocess.run(reference_video_command, check=True)

    matching_service = AudioMatchingService(media_service)
    analysis = matching_service._load_analysis(source_path, reference_path)
    expected_scene_boundaries = (clip_duration, clip_duration * 2)
    if visual_cuts:
        assert analysis.reference_scene_boundaries == pytest.approx(
            expected_scene_boundaries,
            abs=0.2,
        )
    else:
        assert analysis.reference_scene_boundaries == ()
    matching_service._load_analysis = lambda *_args: analysis
    matches = matching_service.find_matches(source_path, reference_path)

    assert len(matches) >= len(expected_starts)
    edit_boundaries = (clip_duration, clip_duration * 2)
    assert all(
        not (
            match.reference_start_seconds + 0.15 < boundary
            < match.reference_start_seconds
            + (match.reference_duration_seconds or match.duration_seconds)
            - 0.15
        )
        for match in matches
        for boundary in edit_boundaries
    ), f"Uma correspondência atravessou uma emenda: {matches}"
    for match in matches:
        reference_midpoint = match.reference_start_seconds + (
            match.reference_duration_seconds or match.duration_seconds
        ) / 2
        source_region_index = min(int(reference_midpoint // clip_duration), 2)
        reference_region_start = source_region_index * clip_duration
        expected_source_start = (
            expected_starts[source_region_index]
            + match.reference_start_seconds
            - reference_region_start
        )
        assert match.source_start_seconds == pytest.approx(
            expected_source_start,
            abs=0.2,
        ), f"Trecho não concorda com a origem esperada: {match}"
    assert matching_service._matched_reference_duration(matches) >= (
        clip_duration * len(expected_starts) * 0.6
    )
    assert matching_service._weighted_average_match_score(matches) >= 0.8


@pytest.mark.parametrize("background_music_scale", (2.0, 8.0, 16.0, 32.0))
def test_audio_matching_finds_speech_under_different_background_music(
    background_music_scale: float,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    generator = np.random.default_rng(177)
    source_time = np.arange(sample_rate * 30, dtype=np.float32) / sample_rate
    source_samples = 0.05 * generator.normal(size=source_time.size).astype(np.float32)

    speech_time = np.arange(sample_rate * 8, dtype=np.float32) / sample_rate
    speech_noise = generator.normal(size=speech_time.size).astype(np.float32)
    syllable_envelope = 0.15 + 0.85 * np.maximum(
        0.0,
        np.sin(2 * np.pi * 3.2 * speech_time),
    )
    speech = (
        speech_noise
        + 0.4 * np.sin(2 * np.pi * 180 * speech_time)
        + 0.25 * np.sin(2 * np.pi * 320 * speech_time)
    ).astype(np.float32) * syllable_envelope

    source_start = 17 * sample_rate
    source_samples[source_start : source_start + speech.size] += speech
    source_samples += (
        0.30 * np.sin(2 * np.pi * 220 * source_time)
        + 0.22 * np.sin(2 * np.pi * 329 * source_time)
        + 0.12 * np.sin(2 * np.pi * 440 * source_time)
    ).astype(np.float32)
    reference_music = (
        0.28 * np.sin(2 * np.pi * 196 * speech_time)
        + 0.24 * np.sin(2 * np.pi * 294 * speech_time)
        + 0.11 * np.sin(2 * np.pi * 392 * speech_time)
    ).astype(np.float32)
    reference_samples = speech + (background_music_scale * reference_music)

    source_features = matching_service._spectral_features_from_samples(source_samples)
    reference_features = matching_service._spectral_features_from_samples(reference_samples)
    source_frame, score, tempo_factor = matching_service._find_best_tempo_match(
        source_features,
        reference_features,
    )
    segmented_matches = matching_service.find_segmented_matches_in_features(
        source_features,
        reference_features,
    )

    assert source_frame / matching_service.feature_rate == pytest.approx(17.0, abs=0.1)
    assert score >= 0.60
    assert segmented_matches[0].source_start_seconds == pytest.approx(17.0, abs=0.1)
    assert tempo_factor == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("pitch_factor", "preserve_duration"),
    (
        (0.8, True),
        (0.9, True),
        (1.1, True),
        (1.2, True),
        (0.9, False),
        (1.1, False),
    ),
)
def test_audio_matching_retries_pitch_and_speed_changes_without_moving_the_cut(
    tmp_path: Path,
    pitch_factor: float,
    preserve_duration: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_service = MediaProcessingService()
    monkeypatch.setattr(media_service, "detect_scene_boundaries", lambda _path: ())
    ffmpeg = media_service.get_ffmpeg_executable()
    sample_rate = 16_000
    source_path = tmp_path / "pitch-shift-source.wav"
    reference_path = tmp_path / "pitch-shift-reference.wav"
    source_expression = (
        "aevalsrc=0.45*sin(2*PI*(180+70*sin(2*PI*0.5*t))*t)"
        "+0.32*sin(2*PI*(710+180*sin(2*PI*0.21*t))*t)"
        "+0.2*sin(2*PI*(1200+90*sin(2*PI*0.13*t))*t):s=16000:d=24"
    )
    speed_filter = f"asetrate={sample_rate * pitch_factor},aresample={sample_rate}"
    if preserve_duration:
        speed_filter += f",atempo={1 / pitch_factor:.8f}"
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
            source_expression,
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:amplitude=0.12:duration=24:sample_rate=16000:seed=909",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:weights=1 1[a]",
            "-map",
            "[a]",
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
            "8.25",
            "-t",
            "8",
            "-i",
            str(source_path),
            "-af",
            speed_filter,
            "-c:a",
            "pcm_s16le",
            str(reference_path),
        ],
        check=True,
    )

    matching_service = AudioMatchingService(media_service)
    matches = matching_service.find_matches(source_path, reference_path)

    assert len(matches) == 1
    assert matches[0].source_start_seconds == pytest.approx(8.25, abs=0.12)
    assert 7.5 <= matches[0].duration_seconds <= 8.2
    assert matches[0].score >= 0.8


@pytest.mark.parametrize("scene_boundaries", ((4.0, 8.0), ()))
def test_pitch_shifted_montage_refinement_preserves_each_source_segment(
    tmp_path: Path,
    scene_boundaries: tuple[float, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_service = MediaProcessingService()
    monkeypatch.setattr(
        media_service,
        "detect_scene_boundaries",
        lambda _path: scene_boundaries,
    )
    ffmpeg = media_service.get_ffmpeg_executable()
    sample_rate = 16_000
    source_path = tmp_path / "edited-source.wav"
    montage_path = tmp_path / "montage.wav"
    reference_path = tmp_path / "pitch-shifted-montage.wav"
    source_expression = (
        "aevalsrc=0.45*sin(2*PI*(180+70*sin(2*PI*0.5*t))*t)"
        "+0.32*sin(2*PI*(710+180*sin(2*PI*0.21*t))*t)"
        "+0.2*sin(2*PI*(1200+90*sin(2*PI*0.13*t))*t):s=16000:d=36"
    )
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
            source_expression,
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:amplitude=0.12:duration=36:sample_rate=16000:seed=909",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:weights=1 1[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            str(source_path),
        ],
        check=True,
    )
    expected_starts = (2.37, 14.63, 28.21)
    segment_duration = 4.0
    montage_filters = [
        f"[0:a]atrim=start={start}:end={start + segment_duration},"
        f"asetpts=PTS-STARTPTS[segment{index}]"
        for index, start in enumerate(expected_starts)
    ]
    montage_filters.append(
        "[segment0][segment1][segment2]concat=n=3:v=0:a=1[montage]"
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-filter_complex",
            ";".join(montage_filters),
            "-map",
            "[montage]",
            "-c:a",
            "pcm_s16le",
            str(montage_path),
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
            "-i",
            str(montage_path),
            "-af",
            "asetrate=17600,aresample=16000,atempo=0.90909091",
            "-c:a",
            "pcm_s16le",
            str(reference_path),
        ],
        check=True,
    )

    matching_service = AudioMatchingService(media_service)
    if not scene_boundaries:
        # Simulate a moderate whole-track hit from shared background audio. The
        # scene detector has no visual cut markers to trigger segmented search.
        broad_match = AudioSegmentMatch(5.0, 0.0, 12.0, 0.68, 12.0)
        monkeypatch.setattr(
            matching_service,
            "_find_contiguous_match",
            lambda _analysis: broad_match,
        )
    matches = matching_service.find_matches(source_path, reference_path)

    assert len(matches) == 3
    assert [match.source_start_seconds for match in matches] == pytest.approx(
        expected_starts,
        abs=0.2,
    )
    assert [match.reference_start_seconds for match in matches] == pytest.approx(
        (0.0, 4.0, 8.0),
        abs=0.1,
    )
    assert all(
        match.alignment_confirmation_score is not None
        and match.alignment_confirmation_score >= 0.55
        for match in matches
    )


def test_scan_selection_prefers_confident_alignment_over_weaker_full_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    source_features = np.zeros((1_500, 32), dtype=np.float32)
    reference_features = np.zeros((500, 32), dtype=np.float32)

    def fake_scan(
        _source: np.ndarray,
        _reference: np.ndarray,
        *,
        segment_seconds: int,
        offset_seconds: float,
        search_indexes: object,
        reference_scene_boundaries: tuple[float, ...],
    ) -> tuple[list[AudioSegmentMatch], float]:
        del search_indexes
        del reference_scene_boundaries
        if segment_seconds == 8:
            return [AudioSegmentMatch(5, 0, 10, 0.64, 10)], 0.64
        if offset_seconds == 0:
            return [
                AudioSegmentMatch(5, 0, 4, 0.95, 4),
                AudioSegmentMatch(15, 4, 4, 0.95, 4),
            ], 0.95
        return [
            AudioSegmentMatch(8, 2, 4, 0.80, 4),
            AudioSegmentMatch(20, 6, 4, 0.80, 4),
        ], 0.80

    monkeypatch.setattr(matching_service, "_scan_reference_segments", fake_scan)
    monkeypatch.setattr(
        matching_service,
        "_scan_short_windows_with_concordance",
        lambda *args, **kwargs: ([], 0.0),
    )

    matches = matching_service.find_segmented_matches_in_features(
        source_features,
        reference_features,
    )

    assert len(matches) == 2
    assert matching_service._weighted_average_match_score(matches) == pytest.approx(0.95)
    assert matching_service._matched_reference_duration(matches) == pytest.approx(8)


def test_concordance_tree_prefers_consistent_branch_over_isolated_high_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(20261124)
    source_features = generator.normal(size=(2_400, 32)).astype(np.float32)
    reference_features = np.zeros((8 * matching_service.feature_rate, 32), dtype=np.float32)
    reference_features[:, 0] = np.arange(reference_features.shape[0])
    observed_windows: list[tuple[int, int]] = []

    def top_candidates(
        _indexes: object,
        window: np.ndarray,
        *,
        limit: int,
    ) -> list[ShortWindowMatchCandidate]:
        del limit
        reference_start = round(float(window[0, 0]))
        observed_windows.append((reference_start, window.shape[0]))
        return [
            ShortWindowMatchCandidate(
                source_start_frame=(20 * matching_service.feature_rate + reference_start),
                similarity_score=0.75,
                local_confidence=0.29,
            ),
            ShortWindowMatchCandidate(
                source_start_frame=80 * matching_service.feature_rate,
                similarity_score=0.99,
                local_confidence=0.99,
            ),
        ]

    monkeypatch.setattr(
        matching_service,
        "_find_top_short_window_candidates",
        top_candidates,
    )
    indexes = matching_service._build_feature_search_indexes(source_features)

    matches, best_score = matching_service._scan_short_windows_with_concordance(
        reference_features,
        search_indexes=indexes,
        scene_boundaries=(),
    )

    assert len(matches) == 1
    assert matches[0].source_start_seconds == pytest.approx(20)
    assert matches[0].duration_seconds == pytest.approx(8)
    assert matches[0].score == pytest.approx(0.75)
    assert matches[0].alignment_confirmation_score == pytest.approx(0.29)
    # The isolated 99% local peak is not a valid match. The coherent 75% path
    # confirms the weaker-but-consistent local candidate across the full clip.
    assert best_score == pytest.approx(0.75)
    assert observed_windows == [
        (start_frame, 2 * matching_service.feature_rate)
        for start_frame in range(
            0,
            6 * matching_service.feature_rate + 1,
            round(0.5 * matching_service.feature_rate),
        )
    ]


def test_concordance_tree_joins_one_second_scenes_into_a_coherent_match() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    generator = np.random.default_rng(20261129)
    source_features = generator.normal(size=(1_600, 32)).astype(np.float32)
    source_start_frame = 450
    reference_features = source_features[source_start_frame : source_start_frame + 165].copy()
    indexes = matching_service._build_feature_search_indexes(source_features)

    matches, best_score = matching_service._scan_short_windows_with_concordance(
        reference_features,
        search_indexes=indexes,
        scene_boundaries=(1.1, 2.2),
    )

    assert len(matches) == 1
    assert matches[0].source_start_seconds == pytest.approx(9.0, abs=0.02)
    assert matches[0].duration_seconds == pytest.approx(3.3, abs=0.02)
    assert matches[0].score >= 0.8
    assert best_score >= matches[0].score


def test_concordance_tree_rejects_accumulated_local_position_jumps() -> None:
    matching_service = AudioMatchingService()
    source_starts = (10.0, 10.75, 11.0, 11.75, 12.0)
    candidate_windows = [
        [AudioSegmentMatch(source_start, index * 0.5, 2.0, 0.9, 2.0)]
        for index, source_start in enumerate(source_starts)
    ]

    path = matching_service._best_concordant_window_path(
        candidate_windows,
        step_seconds=0.5,
    )

    assert path == []


def test_sparse_landmark_votes_find_exact_offset_and_reject_unrelated_audio() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    generator = np.random.default_rng(20261124)
    source_samples = generator.normal(size=sample_rate * 18).astype(np.float32)
    source_samples += (
        0.25
        * np.sin(
            2
            * np.pi
            * 440
            * np.arange(source_samples.size, dtype=np.float32)
            / sample_rate
        )
    )
    start_seconds = 5.0
    reference_samples = source_samples[
        int(start_seconds * sample_rate) : int((start_seconds + 6) * sample_rate)
    ].copy()
    unrelated_samples = np.random.default_rng(20261125).normal(
        size=reference_samples.size
    ).astype(np.float32)

    match = matching_service._find_landmark_alignment(source_samples, reference_samples)
    unrelated_match = matching_service._find_landmark_alignment(
        source_samples,
        unrelated_samples,
    )

    assert match is not None
    offset, inliers, query_hash_count = match
    assert offset == pytest.approx(start_seconds, abs=0.08)
    assert inliers >= 6
    assert query_hash_count >= 20
    assert unrelated_match is None


def test_sparse_landmark_votes_reject_two_equally_valid_repeated_offsets() -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    sample_rate = matching_service.analysis_sample_rate
    reference_samples = np.random.default_rng(20261126).normal(
        size=sample_rate * 5
    ).astype(np.float32)
    silence = np.zeros(sample_rate * 2, dtype=np.float32)
    source_samples = np.concatenate(
        (silence, reference_samples, silence, reference_samples, silence)
    )

    ambiguous_match = matching_service._find_landmark_alignment(
        source_samples,
        reference_samples,
    )

    assert ambiguous_match is None


def test_scene_detection_uses_ffmpeg_cut_filter_and_parses_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_service = MediaProcessingService()
    captured_command: list[str] = []
    monkeypatch.setattr(media_service, "get_ffmpeg_executable", lambda: "ffmpeg")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr=(
                "[showinfo] n: 1 pts_time:2.500000 fmt:yuv420p\n"
                "[showinfo] n: 2 pts_time:4.000000 fmt:yuv420p\n"
            ),
        )

    monkeypatch.setattr("app.services.media_processing_service.subprocess.run", fake_run)

    boundaries = media_service.detect_scene_boundaries("reference.mp4")

    assert boundaries == (2.5, 4.0)
    assert any("fps=12" in argument for argument in captured_command)
    assert any("select='gt(scene,0.3)'" in argument for argument in captured_command)


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


def test_landmark_alignment_rescues_a_weak_waveform_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    predicted_start = 20.0
    reference_samples = np.ones(matching_service.refinement_sample_rate * 4, dtype=np.float32)
    source_samples = np.ones(matching_service.refinement_sample_rate * 40, dtype=np.float32)
    match = AudioSegmentMatch(
        source_start_seconds=predicted_start,
        reference_start_seconds=0,
        duration_seconds=4,
        score=0.78,
        reference_duration_seconds=4,
    )
    monkeypatch.setattr(
        matching_service,
        "_find_best_match_in_samples",
        lambda source, reference: (0, 0.1),
    )
    monkeypatch.setattr(
        matching_service,
        "_find_landmark_alignment",
        lambda source, reference: (3.2, 14, 120),
    )

    refined = matching_service._refine_segment_match(
        match,
        source_samples,
        reference_samples,
        require_score=True,
    )

    assert refined.source_start_seconds == pytest.approx(20.2)
    assert refined.score == match.score


def test_vectorized_subsequence_dtw_matches_reference_recurrence() -> None:
    import numpy as np

    generator = np.random.default_rng(20261124)
    costs = generator.uniform(0.0, 1.0, size=(17, 23)).astype(np.float32)
    penalty = 0.05
    expected = np.full((costs.shape[0] + 1, costs.shape[1] + 1), np.inf, dtype=np.float32)
    expected[0, :] = 0.0
    expected_steps = np.zeros(expected.shape, dtype=np.uint8)

    for row in range(1, expected.shape[0]):
        for column in range(1, expected.shape[1]):
            diagonal = expected[row - 1, column - 1]
            vertical = expected[row - 1, column] + penalty
            horizontal = expected[row, column - 1] + penalty
            if diagonal <= vertical and diagonal <= horizontal:
                prior, step = diagonal, 0
            elif vertical <= horizontal:
                prior, step = vertical, 1
            else:
                prior, step = horizontal, 2
            expected[row, column] = costs[row - 1, column - 1] + prior
            expected_steps[row, column] = step

    actual, actual_steps = AudioMatchingService._subsequence_dtw_costs(costs, penalty)

    np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-5)
    np.testing.assert_array_equal(actual_steps, expected_steps)


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
    with pytest.raises(AudioMatchNotFoundError, match="confiança média"):
        matching_service._validate_reference_coverage(matches, reference_duration=32)

    confident_matches = [
        AudioSegmentMatch(
            source_start_seconds=index * 10,
            reference_start_seconds=index * 8,
            duration_seconds=8,
            reference_duration_seconds=8,
            score=0.8,
        )
        for index in range(3)
    ]
    matching_service._validate_reference_coverage(confident_matches, reference_duration=32)


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


def test_find_matches_prefers_segmented_alignment_when_reference_has_real_edits() -> None:
    import numpy as np

    analysis = AudioAnalysis(
        source_samples=np.zeros(200_000),
        reference_samples=np.zeros(80_000),
        source_features=np.zeros((1_000, 48)),
        reference_features=np.zeros((400, 48)),
        reference_scene_boundaries=(3.0, 6.0),
    )
    contiguous_match = AudioSegmentMatch(5.0, 0.0, 8.0, 0.70, 8.0)
    montage_matches = [
        AudioSegmentMatch(5.0, 0.0, 3.0, 0.78, 3.0),
        AudioSegmentMatch(20.0, 3.0, 3.0, 0.80, 3.0),
        AudioSegmentMatch(40.0, 6.0, 2.0, 0.76, 2.0),
    ]

    class MatchingServiceWithSceneEdits(AudioMatchingService):
        def _load_analysis(self, source_path: str, reference_path: str) -> AudioAnalysis:
            return analysis

        def _find_contiguous_match(self, loaded: AudioAnalysis) -> AudioSegmentMatch:
            return contiguous_match

        def _find_segmented_matches_from_analysis(
            self, loaded: AudioAnalysis
        ) -> list[AudioSegmentMatch]:
            return montage_matches

    matches = MatchingServiceWithSceneEdits().find_matches("source.mp4", "reference.mp4")

    assert matches == montage_matches


def test_find_matches_checks_audio_edits_even_without_detected_visual_cuts() -> None:
    import numpy as np

    analysis = AudioAnalysis(
        source_samples=np.zeros(200_000),
        reference_samples=np.zeros(80_000),
        source_features=np.zeros((1_000, 48)),
        reference_features=np.zeros((400, 48)),
        reference_scene_boundaries=(),
    )
    contiguous_match = AudioSegmentMatch(5.0, 0.0, 8.0, 0.92, 8.0)
    audio_edit_matches = [
        AudioSegmentMatch(5.0, 0.0, 3.0, 0.90, 3.0),
        AudioSegmentMatch(20.0, 3.0, 3.0, 0.91, 3.0),
        AudioSegmentMatch(40.0, 6.0, 2.0, 0.89, 2.0),
    ]

    class MatchingServiceWithAudioOnlyEdits(AudioMatchingService):
        def _load_analysis(self, source_path: str, reference_path: str) -> AudioAnalysis:
            return analysis

        def _find_contiguous_match(self, loaded: AudioAnalysis) -> AudioSegmentMatch:
            return contiguous_match

        def _find_segmented_matches_from_analysis(
            self, loaded: AudioAnalysis
        ) -> list[AudioSegmentMatch]:
            return audio_edit_matches

    matches = MatchingServiceWithAudioOnlyEdits().find_matches(
        "source.mp4",
        "reference.mp4",
    )

    assert matches == audio_edit_matches


def test_boundary_refinement_skips_feature_copies_for_contiguous_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    matching_service = AudioMatchingService()
    matches = [
        AudioSegmentMatch(5.0, 0.0, 4.0, 0.91, 4.0),
        AudioSegmentMatch(9.0, 4.0, 4.0, 0.90, 4.0),
    ]

    def fail_if_normalized(_features: np.ndarray) -> np.ndarray:
        pytest.fail("Contiguous matches should not allocate normalized feature copies")

    monkeypatch.setattr(matching_service, "_normalize_columns", fail_if_normalized)

    refined = matching_service._refine_discontinuous_boundaries(
        matches,
        np.zeros((1_000, 48), dtype=np.float32),
        np.zeros((400, 48), dtype=np.float32),
    )

    assert refined == matches


def test_find_matches_corrects_moderate_contiguous_false_positive_with_real_segments() -> None:
    import numpy as np

    sample_rate = AudioMatchingService.analysis_sample_rate
    generator = np.random.default_rng(909)
    source_time = np.arange(sample_rate * 36, dtype=np.float32) / sample_rate
    source_samples = (
        0.12 * generator.normal(size=source_time.size)
        + 0.7 * np.sin(2 * np.pi * 220 * source_time)
        + 0.45 * np.sin(2 * np.pi * 330 * source_time)
    ).astype(np.float32)
    expected_starts = (2.37, 14.63, 28.21)
    segment_duration = 4.0
    reference_samples = np.concatenate(
        [
            source_samples[
                round(start * sample_rate) : round((start + segment_duration) * sample_rate)
            ]
            for start in expected_starts
        ]
    )

    class MontageMediaService(MediaProcessingService):
        def extract_audio_samples(self, file_path: str, sample_rate: int) -> np.ndarray:
            return source_samples if file_path == "source.mp4" else reference_samples

        def detect_scene_boundaries(self, file_path: str) -> tuple[float, ...]:
            return (segment_duration, segment_duration * 2)

    matching_service = AudioMatchingService(MontageMediaService())
    # Model a moderate whole-reference hit caused by shared background audio;
    # the segmented search below still has to locate each actual edit.
    broad_match = AudioSegmentMatch(5.0, 0.0, 12.0, 0.62, 12.0)
    matching_service._find_contiguous_match = lambda _analysis: broad_match

    matches = matching_service.find_matches("source.mp4", "reference.mp4")

    assert len(matches) == 3
    assert [match.source_start_seconds for match in matches] == pytest.approx(
        expected_starts,
        abs=0.2,
    )
    assert [match.reference_start_seconds for match in matches] == pytest.approx(
        (0.0, segment_duration, segment_duration * 2),
        abs=0.1,
    )


def test_find_matches_keeps_contiguous_alignment_when_scene_edits_are_only_visual() -> None:
    import numpy as np

    analysis = AudioAnalysis(
        source_samples=np.zeros(200_000),
        reference_samples=np.zeros(80_000),
        source_features=np.zeros((1_000, 48)),
        reference_features=np.zeros((400, 48)),
        reference_scene_boundaries=(3.0, 6.0),
    )
    contiguous_match = AudioSegmentMatch(5.0, 0.0, 8.0, 0.80, 8.0)
    visually_cut_matches = [
        AudioSegmentMatch(5.0, 0.0, 3.0, 0.76, 3.0),
        AudioSegmentMatch(8.0, 3.0, 3.0, 0.77, 3.0),
        AudioSegmentMatch(11.0, 6.0, 2.0, 0.75, 2.0),
    ]

    class MatchingServiceWithVisualCuts(AudioMatchingService):
        def _load_analysis(self, source_path: str, reference_path: str) -> AudioAnalysis:
            return analysis

        def _find_contiguous_match(self, loaded: AudioAnalysis) -> AudioSegmentMatch:
            return contiguous_match

        def _find_segmented_matches_from_analysis(
            self, loaded: AudioAnalysis
        ) -> list[AudioSegmentMatch]:
            return visually_cut_matches

    matches = MatchingServiceWithVisualCuts().find_matches("source.mp4", "reference.mp4")

    assert matches == [contiguous_match]


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


def test_audio_extraction_preserves_audio_offset_on_video_timeline(tmp_path: Path) -> None:
    import numpy as np

    media_service = MediaProcessingService()
    video_path = tmp_path / "audio-offset.mp4"
    ffmpeg = media_service.get_ffmpeg_executable()
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
            "color=c=blue:s=160x120:r=25:d=8",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=6",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-shortest",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )

    samples = media_service.extract_audio_samples(video_path, sample_rate=16_000)
    leading_audio = samples[int(0.2 * 16_000) : int(0.3 * 16_000)]
    aligned_audio = samples[int(0.6 * 16_000) : int(0.7 * 16_000)]

    assert samples.size / 16_000 == pytest.approx(6.5, abs=0.1)
    assert np.sqrt(np.mean(np.square(leading_audio))) < 0.005
    assert np.sqrt(np.mean(np.square(aligned_audio))) > 0.02


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
    monkeypatch.setattr(settings, "youtube_cookies_base64", None)
    monkeypatch.setattr(settings, "youtube_cookies_file", None)
    monkeypatch.setattr(settings, "youtube_cookies_from_browser", None)

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
    assert "youtube:player_client=default" in captured_command
    assert "youtube:player_client=default,web_embedded" not in captured_command
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
