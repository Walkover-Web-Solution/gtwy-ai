from pathlib import Path
from typing import Any

from fastapi import UploadFile
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

NON_PREVIEWABLE_EXTENSIONS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".tar", ".gz", ".7z",
    ".exe", ".dmg", ".apk", ".msi", ".deb", ".rpm",
    ".bin", ".iso", ".dll",
}

VIDEO_CONTENT_TYPES = {
    "video/mp4", "video/mpeg", "video/mov", "video/quicktime",
    "video/avi", "video/x-flv", "video/mpg", "video/webm",
    "video/wmv", "video/3gpp",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mpeg", ".mpg", ".mov", ".avi",
    ".flv", ".webm", ".wmv", ".3gpp", ".3gp",
}


class FileUploadRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    file: UploadFile | None = None
    apikey: str | None = None
    _is_pdf: bool = PrivateAttr(default=False)
    _is_video: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_file(self) -> "FileUploadRequest":
        if not (self.file and self.file.filename):
            raise ValueError("File or video_url not found")
        filename = self.file.filename or ""
        suffix = Path(filename).suffix.lower()
        content_type = self.file.content_type or ""

        self._is_pdf = content_type == "application/pdf" or suffix == ".pdf"
        self._is_video = content_type in VIDEO_CONTENT_TYPES or suffix in VIDEO_EXTENSIONS

        if suffix in NON_PREVIEWABLE_EXTENSIONS:
            raise ValueError(f"File type not supported. '{self.file.filename}' cannot be previewed in browser. Please upload images, PDFs, videos, or text files.")
        if self._is_video and not (self.apikey or "").strip():
            raise ValueError("apikey is required for video file uploads")
        return self

    @property
    def is_pdf(self) -> bool:
        return self._is_pdf

    @property
    def is_video(self) -> bool:
        return self._is_video


class VideoUrlUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    video_url: AnyHttpUrl
    apikey: str | None = None

    @field_validator("video_url")
    @classmethod
    def must_be_video_url(cls, v: Any) -> Any:
        if not any(str(v).lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            raise ValueError(f"URL must point to a supported video file ({', '.join(sorted(VIDEO_EXTENSIONS))})")
        return v

    @model_validator(mode="after")
    def require_apikey(self) -> "VideoUrlUploadRequest":
        if not (self.apikey or "").strip():
            raise ValueError("apikey is required when video_url is provided")
        return self

