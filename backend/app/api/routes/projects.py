import uuid
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.generated_clip import MatchGenerationResponse
from app.schemas.project import ProjectCreate, ProjectDetail, ProjectRead
from app.schemas.reference_video import ReferenceVideoRead, YoutubeReferenceCreate
from app.schemas.source_video import SourceVideoRead
from app.schemas.storage import (
    MultipartPartUrlRequest,
    MultipartPartUrlResponse,
    MultipartUploadAbort,
    MultipartUploadComplete,
    MultipartUploadStart,
    MultipartUploadStartResponse,
)
from app.schemas.youtube import YoutubeSearchResult
from app.services.generated_clip_service import (
    GeneratedClipNotAvailableError,
    GeneratedClipService,
)
from app.services.matching_service import MatchingPrerequisiteError, MatchingService
from app.services.multipart_upload_service import InvalidUploadError, MultipartUploadService
from app.services.project_service import ProjectNotFoundError, ProjectService
from app.services.reference_video_service import (
    ReferenceVideoInUseError,
    ReferenceVideoNotFoundError,
    ReferenceVideoService,
)
from app.services.source_video_service import SourceVideoService
from app.services.storage_service import (
    StorageConfigurationError,
    StorageOperationError,
    StoredFileNotFoundError,
)
from app.services.youtube_service import YoutubeConfigurationError, YoutubeRequestError, YoutubeService

router = APIRouter(prefix="/projects", tags=["projects"])

DatabaseSession = Annotated[Session, Depends(get_db)]


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, database: DatabaseSession) -> ProjectRead:
    return ProjectService().create(database, payload)


@router.get("", response_model=list[ProjectRead])
def list_projects(database: DatabaseSession) -> list[ProjectRead]:
    return ProjectService().list(database)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: uuid.UUID, database: DatabaseSession) -> ProjectDetail:
    try:
        return ProjectService().get_by_id(database, project_id)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: uuid.UUID, database: DatabaseSession) -> Response:
    try:
        ProjectService().delete(database, project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post("/{project_id}/upload", response_model=SourceVideoRead, status_code=status.HTTP_201_CREATED)
def upload_source_video(
    project_id: uuid.UUID,
    database: DatabaseSession,
    file: Annotated[UploadFile, File(description="Original video file")],
) -> SourceVideoRead:
    try:
        return SourceVideoService().upload(database, project_id=project_id, file=file)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error


@router.post(
    "/{project_id}/reference-videos/upload",
    response_model=ReferenceVideoRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_reference_video(
    project_id: uuid.UUID,
    database: DatabaseSession,
    file: Annotated[UploadFile, File(description="Reference clip file")],
) -> ReferenceVideoRead:
    try:
        return ReferenceVideoService().upload(database, project_id=project_id, file=file)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error


@router.post(
    "/{project_id}/uploads/multipart",
    response_model=MultipartUploadStartResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_multipart_upload(
    project_id: uuid.UUID,
    payload: MultipartUploadStart,
    database: DatabaseSession,
) -> MultipartUploadStartResponse:
    try:
        return MultipartUploadService().start(database, project_id=project_id, payload=payload)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except StorageConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except InvalidUploadError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post(
    "/{project_id}/uploads/multipart/part-url",
    response_model=MultipartPartUrlResponse,
)
def create_multipart_part_url(
    project_id: uuid.UUID,
    payload: MultipartPartUrlRequest,
    database: DatabaseSession,
) -> MultipartPartUrlResponse:
    try:
        url = MultipartUploadService().create_part_url(
            database, project_id=project_id, payload=payload
        )
        return MultipartPartUrlResponse(url=url)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except (InvalidUploadError, StoredFileNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except StorageConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post(
    "/{project_id}/uploads/multipart/complete",
    response_model=SourceVideoRead | ReferenceVideoRead,
    status_code=status.HTTP_201_CREATED,
)
def complete_multipart_upload(
    project_id: uuid.UUID,
    payload: MultipartUploadComplete,
    database: DatabaseSession,
) -> SourceVideoRead | ReferenceVideoRead:
    try:
        return MultipartUploadService().complete(
            database, project_id=project_id, payload=payload
        )
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except (InvalidUploadError, StoredFileNotFoundError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except StorageConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post(
    "/{project_id}/uploads/multipart/abort",
    status_code=status.HTTP_204_NO_CONTENT,
)
def abort_multipart_upload(
    project_id: uuid.UUID,
    payload: MultipartUploadAbort,
    database: DatabaseSession,
) -> Response:
    try:
        MultipartUploadService().abort(database, project_id=project_id, payload=payload)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except InvalidUploadError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except StorageConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.get("/{project_id}/youtube/search", response_model=list[YoutubeSearchResult])
async def search_youtube_videos(
    project_id: uuid.UUID,
    database: DatabaseSession,
    query: Annotated[str, Query(min_length=1, max_length=250)],
) -> list[YoutubeSearchResult]:
    try:
        ProjectService().get_by_id(database, project_id)
        return await YoutubeService().search(query)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except YoutubeConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except YoutubeRequestError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post(
    "/{project_id}/reference-videos/youtube",
    response_model=ReferenceVideoRead,
    status_code=status.HTTP_201_CREATED,
)
def add_youtube_reference(
    project_id: uuid.UUID,
    payload: YoutubeReferenceCreate,
    database: DatabaseSession,
) -> ReferenceVideoRead:
    try:
        return ReferenceVideoService().add_youtube_reference(
            database, project_id=project_id, payload=payload
        )
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error


@router.delete(
    "/{project_id}/reference-videos/{reference_video_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_reference_video(
    project_id: uuid.UUID,
    reference_video_id: uuid.UUID,
    database: DatabaseSession,
) -> Response:
    try:
        ReferenceVideoService().delete(
            database,
            project_id=project_id,
            reference_video_id=reference_video_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except (ProjectNotFoundError, ReferenceVideoNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referência não encontrada.",
        ) from error
    except ReferenceVideoInUseError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except StorageOperationError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post(
    "/{project_id}/generated-clips",
    response_model=MatchGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_clips(
    project_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    database: DatabaseSession,
) -> MatchGenerationResponse:
    try:
        ProjectService().get_by_id(database, project_id)
        matching_service = MatchingService()
        job_ids = matching_service.create_jobs(database, project_id)
        background_tasks.add_task(matching_service.process_jobs, job_ids)
        return MatchGenerationResponse(
            message="Matching iniciado. Atualize a página para acompanhar os cortes.",
            jobs_started=len(job_ids),
        )
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except MatchingPrerequisiteError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/{project_id}/generated-clips/{clip_id}/download")
def download_generated_clip(
    project_id: uuid.UUID,
    clip_id: uuid.UUID,
    database: DatabaseSession,
) -> Response:
    try:
        clip, target = GeneratedClipService().get_download_target(
            database, project_id=project_id, clip_id=clip_id
        )
        if isinstance(target, Path):
            return FileResponse(target, media_type="video/mp4", filename=f"clip-{clip.id}.mp4")
        return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from error
    except GeneratedClipNotAvailableError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generated clip not available") from error
