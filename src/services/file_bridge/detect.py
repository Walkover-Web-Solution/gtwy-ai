"""Work out what kind of file a URL points to.

The declared type on an attachment is not reliable: history rows tag every
non-image file as "pdf". So the URL extension wins when it is known, then the
declared type, then the Content-Type the server sent back.
"""

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from src.schemas.image_schemas import VIDEO_CONTENT_TYPES, VIDEO_EXTENSIONS

IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
PDF = "pdf"
CSV = "csv"
XLSX = "xlsx"
DOCX = "docx"
PPTX = "pptx"
TEXT = "text"
LEGACY_OFFICE = "legacy_office"
UNKNOWN = "unknown"

_EXTENSION_KINDS = {
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"), IMAGE),
    **dict.fromkeys((".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac", ".aac", ".opus", ".weba", ".amr"), AUDIO),
    **dict.fromkeys(VIDEO_EXTENSIONS, VIDEO),
    ".pdf": PDF,
    ".csv": CSV,
    ".tsv": CSV,
    ".xlsx": XLSX,
    ".xlsm": XLSX,
    ".docx": DOCX,
    ".pptx": PPTX,
    **dict.fromkeys((".doc", ".xls", ".ppt"), LEGACY_OFFICE),
    **dict.fromkeys(
        (
            ".txt",
            ".md",
            ".markdown",
            ".json",
            ".jsonl",
            ".xml",
            ".html",
            ".htm",
            ".yaml",
            ".yml",
            ".log",
            ".ini",
            ".toml",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".java",
            ".go",
            ".rb",
            ".php",
            ".c",
            ".h",
            ".cpp",
            ".cs",
            ".rs",
            ".sh",
            ".sql",
            ".css",
            ".srt",
            ".vtt",
        ),
        TEXT,
    ),
}

_DECLARED_KINDS = {"image": IMAGE, "audio": AUDIO, "video": VIDEO, "pdf": PDF}

_CONTENT_TYPE_KINDS = {
    "application/pdf": PDF,
    "text/csv": CSV,
    "text/tab-separated-values": CSV,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": XLSX,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCX,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": PPTX,
    "application/msword": LEGACY_OFFICE,
    "application/vnd.ms-excel": LEGACY_OFFICE,
    "application/vnd.ms-powerpoint": LEGACY_OFFICE,
    "application/json": TEXT,
    "application/xml": TEXT,
    **dict.fromkeys(VIDEO_CONTENT_TYPES, VIDEO),
}


def url_extension(url: str) -> str:
    try:
        return PurePosixPath(unquote(urlparse(url).path)).suffix.lower()
    except Exception:
        return ""


def file_name(url: str) -> str:
    try:
        return PurePosixPath(unquote(urlparse(url).path)).name or url
    except Exception:
        return url


def kind_from_content_type(content_type: str | None) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime:
        return UNKNOWN
    if mime in _CONTENT_TYPE_KINDS:
        return _CONTENT_TYPE_KINDS[mime]
    for prefix, kind in (("image/", IMAGE), ("audio/", AUDIO), ("video/", VIDEO), ("text/", TEXT)):
        if mime.startswith(prefix):
            return kind
    return UNKNOWN


def detect_kind(url: str, declared_type: str | None = None, content_type: str | None = None) -> str:
    kind = _EXTENSION_KINDS.get(url_extension(url))
    if kind:
        return kind
    # Content-Type is only known after a download; it is a better signal than a
    # declared "pdf", which history uses for every non-image file.
    if content_type:
        kind = kind_from_content_type(content_type)
        if kind != UNKNOWN:
            return kind
    return _DECLARED_KINDS.get((declared_type or "").lower(), UNKNOWN)
