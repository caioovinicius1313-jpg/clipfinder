import subprocess
import uuid
from collections.abc import Generator
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
from app.repositories.generated_clip_repository import GeneratedClipRepository
from app.services.media_processing_service import (
    AudioMatchingService,
    MediaProbe,
    MediaProcessingError,
    MediaProcessingService,
)
from app.services.multipart_upload_service import MultipartUploadService
from app.services.storage_service import StorageService
from app.schemas.storage import MultipartUploadStart


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
