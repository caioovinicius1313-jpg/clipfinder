from typing import Literal

from pydantic import BaseModel, Field


UploadKind = Literal["source", "reference"]


class MultipartUploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="video/mp4", min_length=1, max_length=150)
    size: int = Field(gt=0)
    kind: UploadKind


class MultipartUploadStartResponse(BaseModel):
    upload_id: str
    object_key: str
    part_size: int
    part_count: int


class MultipartPartUrlRequest(BaseModel):
    upload_id: str = Field(min_length=1, max_length=1500)
    object_key: str = Field(min_length=1, max_length=512)
    part_number: int = Field(ge=1, le=10_000)


class MultipartPartUrlResponse(BaseModel):
    url: str


class CompletedUploadPart(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=300)


class MultipartUploadComplete(BaseModel):
    upload_id: str = Field(min_length=1, max_length=1500)
    object_key: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="video/mp4", min_length=1, max_length=150)
    size: int = Field(gt=0)
    kind: UploadKind
    parts: list[CompletedUploadPart] = Field(min_length=1, max_length=10_000)


class MultipartUploadAbort(BaseModel):
    upload_id: str = Field(min_length=1, max_length=1500)
    object_key: str = Field(min_length=1, max_length=512)
    kind: UploadKind
