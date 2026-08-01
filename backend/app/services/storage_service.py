from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings


BUCKET_PATH_PREFIX = "bucket://"


class StorageConfigurationError(Exception):
    """Raised when Bucket credentials are not configured."""


class StorageOperationError(Exception):
    """Raised when an object storage operation fails."""


class StoredFileNotFoundError(Exception):
    """Raised when a stored file cannot be found."""


class StorageService:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def bucket_enabled(self) -> bool:
        return settings.bucket_enabled

    @property
    def client(self) -> Any:
        if not self.bucket_enabled:
            raise StorageConfigurationError(
                "O Railway Bucket ainda não está conectado ao backend."
            )
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.bucket_endpoint,
                aws_access_key_id=settings.bucket_access_key_id,
                aws_secret_access_key=settings.bucket_secret_access_key,
                region_name=settings.bucket_region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": settings.bucket_url_style},
                ),
            )
        return self._client

    @staticmethod
    def bucket_path(object_key: str) -> str:
        return f"{BUCKET_PATH_PREFIX}{object_key.lstrip('/')}"

    @staticmethod
    def is_bucket_path(stored_path: str) -> bool:
        return stored_path.startswith(BUCKET_PATH_PREFIX)

    @staticmethod
    def object_key(stored_path: str) -> str:
        if not StorageService.is_bucket_path(stored_path):
            raise ValueError("The stored path does not point to a Bucket object")
        return stored_path[len(BUCKET_PATH_PREFIX) :]

    def start_multipart_upload(self, *, object_key: str, content_type: str) -> str:
        try:
            response = self.client.create_multipart_upload(
                Bucket=settings.bucket_name,
                Key=object_key,
                ContentType=content_type,
            )
            return str(response["UploadId"])
        except (BotoCoreError, ClientError, KeyError) as error:
            raise StorageOperationError("Não foi possível iniciar o upload no Bucket.") from error

    def create_part_upload_url(
        self, *, object_key: str, upload_id: str, part_number: int
    ) -> str:
        try:
            return str(
                self.client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": settings.bucket_name,
                        "Key": object_key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=settings.bucket_presigned_expiration,
                )
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("Não foi possível autorizar uma parte do upload.") from error

    def complete_multipart_upload(
        self,
        *,
        object_key: str,
        upload_id: str,
        parts: list[dict[str, int | str]],
    ) -> None:
        try:
            self.client.complete_multipart_upload(
                Bucket=settings.bucket_name,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("Não foi possível concluir o upload no Bucket.") from error

    def abort_multipart_upload(self, *, object_key: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(
                Bucket=settings.bucket_name,
                Key=object_key,
                UploadId=upload_id,
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("Não foi possível cancelar o upload no Bucket.") from error

    def object_size(self, object_key: str) -> int:
        try:
            response = self.client.head_object(Bucket=settings.bucket_name, Key=object_key)
            return int(response["ContentLength"])
        except (BotoCoreError, ClientError, KeyError) as error:
            raise StoredFileNotFoundError from error

    def upload_file(self, local_path: Path, *, object_key: str, content_type: str) -> str:
        try:
            self.client.upload_file(
                str(local_path),
                settings.bucket_name,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )
            return self.bucket_path(object_key)
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageOperationError("Não foi possível salvar o arquivo no Bucket.") from error

    def create_download_url(self, stored_path: str, *, filename: str | None = None) -> str:
        object_key = self.object_key(stored_path)
        parameters: dict[str, str] = {
            "Bucket": str(settings.bucket_name),
            "Key": object_key,
        }
        if filename:
            safe_filename = Path(filename).name.replace('"', "")
            parameters["ResponseContentDisposition"] = (
                f'attachment; filename="{safe_filename}"'
            )
        try:
            return str(
                self.client.generate_presigned_url(
                    "get_object",
                    Params=parameters,
                    ExpiresIn=settings.bucket_presigned_expiration,
                )
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("Não foi possível autorizar o acesso ao arquivo.") from error

    def media_input(self, stored_path: str) -> str:
        if self.is_bucket_path(stored_path):
            return self.create_download_url(stored_path)

        absolute_path = self.local_path(stored_path)
        if not absolute_path.is_file():
            raise StoredFileNotFoundError
        return str(absolute_path)

    @staticmethod
    def local_path(stored_path: str) -> Path:
        return Path(settings.storage_path) / stored_path

    def delete_object(self, stored_path: str) -> None:
        if not self.is_bucket_path(stored_path):
            absolute_path = Path(settings.storage_path) / stored_path
            if absolute_path.is_file():
                absolute_path.unlink()
            return

        try:
            self.client.delete_object(
                Bucket=settings.bucket_name,
                Key=self.object_key(stored_path),
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("Não foi possível excluir o arquivo do Bucket.") from error

    def delete_prefix(self, object_prefix: str) -> None:
        if not self.bucket_enabled:
            return

        continuation_token: str | None = None
        try:
            while True:
                parameters: dict[str, Any] = {
                    "Bucket": settings.bucket_name,
                    "Prefix": object_prefix,
                }
                if continuation_token:
                    parameters["ContinuationToken"] = continuation_token
                response = self.client.list_objects_v2(**parameters)
                objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
                if objects:
                    self.client.delete_objects(
                        Bucket=settings.bucket_name,
                        Delete={"Objects": objects, "Quiet": True},
                    )
                if not response.get("IsTruncated"):
                    break
                continuation_token = str(response["NextContinuationToken"])
        except (BotoCoreError, ClientError, KeyError) as error:
            raise StorageOperationError("Não foi possível limpar os arquivos do Bucket.") from error

    def configure_browser_cors(self, allowed_origins: list[str]) -> None:
        if not allowed_origins:
            return
        try:
            self.client.put_bucket_cors(
                Bucket=settings.bucket_name,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedHeaders": ["*"],
                            "AllowedMethods": ["GET", "HEAD", "PUT"],
                            "AllowedOrigins": allowed_origins,
                            "ExposeHeaders": ["ETag"],
                            "MaxAgeSeconds": 3600,
                        }
                    ]
                },
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError(
                "Não foi possível configurar o CORS do Railway Bucket."
            ) from error
