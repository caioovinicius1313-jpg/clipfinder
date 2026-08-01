import subprocess
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
from app.services.media_processing_service import (
    AudioMatchingService,
    MediaProcessingError,
    MediaProcessingService,
)


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
