import math
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.repositories.reference_video_repository import ReferenceVideoRepository
from app.repositories.source_video_repository import SourceVideoRepository
from app.schemas.reference_video import ReferenceVideoRead
from app.schemas.source_video import SourceVideoRead
from app.schemas.storage import (
    MultipartPartUrlRequest,
    MultipartUploadAbort,
    MultipartUploadComplete,
    MultipartUploadStart,
    MultipartUploadStartResponse,
)
from app.services.media_processing_service import MediaProcessingService
from app.services.project_service import ProjectService
from app.services.storage_service import StorageConfigurationError, StorageService


class InvalidUploadError(Exception):
    """Raised when multipart upload metadata is invalid."""


class MultipartUploadService:
    part_size = 16 * 1024 * 1024
    allowed_extensions = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

    def __init__(
        self,
        storage: StorageService | None = None,
        project_service: ProjectService | None = None,
        source_repository: SourceVideoRepository | None = None,
        reference_repository: ReferenceVideoRepository | None = None,
        media_service: MediaProcessingService | None = None,
    ) -> None:
        self.storage = storage or StorageService()
        self.project_service = project_service or ProjectService()
        self.source_repository = source_repository or SourceVideoRepository()
        self.reference_repository = reference_repository or ReferenceVideoRepository()
        self.media_service = media_service or MediaProcessingService()

    def start(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: MultipartUploadStart,
    ) -> MultipartUploadStartResponse:
        self.project_service.get_by_id(database, project_id)
        if not self.storage.bucket_enabled:
            raise StorageConfigurationError(
                "O Railway Bucket ainda não está conectado ao backend."
            )

        filename, extension = self._validate_file(payload.filename, payload.content_type, payload.size)
        directory = "source" if payload.kind == "source" else "references"
        object_key = f"projects/{project_id}/{directory}/{uuid.uuid4()}{extension}"
        upload_id = self.storage.start_multipart_upload(
            object_key=object_key,
            content_type=payload.content_type,
        )
        return MultipartUploadStartResponse(
            upload_id=upload_id,
            object_key=object_key,
            part_size=self.part_size,
            part_count=math.ceil(payload.size / self.part_size),
        )

    def create_part_url(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: MultipartPartUrlRequest,
    ) -> str:
        self.project_service.get_by_id(database, project_id)
        self._validate_object_key(project_id, payload.object_key)
        return self.storage.create_part_upload_url(
            object_key=payload.object_key,
            upload_id=payload.upload_id,
            part_number=payload.part_number,
        )

    def complete(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: MultipartUploadComplete,
    ) -> SourceVideoRead | ReferenceVideoRead:
        self.project_service.get_by_id(database, project_id)
        filename, _ = self._validate_file(payload.filename, payload.content_type, payload.size)
        self._validate_object_key(project_id, payload.object_key, payload.kind)

        part_numbers = [part.part_number for part in payload.parts]
        if len(part_numbers) != len(set(part_numbers)):
            raise InvalidUploadError("O upload contém partes duplicadas.")

        self.storage.complete_multipart_upload(
            object_key=payload.object_key,
            upload_id=payload.upload_id,
            parts=[
                {"PartNumber": part.part_number, "ETag": part.etag}
                for part in sorted(payload.parts, key=lambda item: item.part_number)
            ],
        )
        stored_path = self.storage.bucket_path(payload.object_key)
        try:
            actual_size = self.storage.object_size(payload.object_key)
            if actual_size != payload.size:
                raise InvalidUploadError(
                    "O tamanho recebido não corresponde ao arquivo selecionado. Tente novamente."
                )
            media_probe = self.media_service.probe(self.storage.media_input(stored_path))
            if not media_probe.has_video:
                raise InvalidUploadError("O arquivo enviado não possui uma faixa de vídeo.")
            if not media_probe.has_audio:
                raise InvalidUploadError(
                    "O arquivo enviado não possui áudio. Escolha uma versão do vídeo com imagem e áudio."
                )
            duration = media_probe.duration_seconds
            if payload.kind == "source":
                return self.source_repository.create(
                    database,
                    project_id=project_id,
                    filename=filename,
                    path=stored_path,
                    size=actual_size,
                    duration=duration,
                )
            return self.reference_repository.create_uploaded(
                database,
                project_id=project_id,
                title=filename,
                filename=filename,
                path=stored_path,
                size=actual_size,
                duration=duration,
                status="uploaded",
            )
        except Exception:
            self.storage.delete_object(stored_path)
            raise

    def abort(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        payload: MultipartUploadAbort,
    ) -> None:
        self.project_service.get_by_id(database, project_id)
        self._validate_object_key(project_id, payload.object_key, payload.kind)
        self.storage.abort_multipart_upload(
            object_key=payload.object_key,
            upload_id=payload.upload_id,
        )

    def _validate_file(self, filename: str, content_type: str, size: int) -> tuple[str, str]:
        clean_filename = Path(filename).name.strip()
        extension = Path(clean_filename).suffix.lower()
        if not clean_filename or extension not in self.allowed_extensions:
            raise InvalidUploadError("Use um arquivo de vídeo MP4, MOV, MKV, WEBM, M4V ou AVI.")
        if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
            raise InvalidUploadError("O arquivo selecionado não foi reconhecido como vídeo.")
        if size > settings.max_upload_size_gb * 1024**3:
            raise InvalidUploadError(
                f"O arquivo ultrapassa o limite configurado de {settings.max_upload_size_gb} GB."
            )
        return clean_filename, extension

    @staticmethod
    def _validate_object_key(
        project_id: uuid.UUID,
        object_key: str,
        kind: str | None = None,
    ) -> None:
        base_prefix = f"projects/{project_id}/"
        if not object_key.startswith(base_prefix) or ".." in object_key:
            raise InvalidUploadError("O destino do upload é inválido.")
        if kind:
            directory = "source" if kind == "source" else "references"
            if not object_key.startswith(f"{base_prefix}{directory}/"):
                raise InvalidUploadError("O tipo do upload não corresponde ao destino informado.")
