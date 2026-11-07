"""Common parser types and file-type detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DocKind(str, Enum):
    pdf = "pdf"
    spreadsheet = "spreadsheet"
    csv = "csv"
    image = "image"
    text = "text"
    unsupported = "unsupported"


MAX_TEXT_CHARS = 40_000


@dataclass
class ParsedDocument:
    kind: DocKind
    text: str = ""
    images: list[bytes] = field(default_factory=list)  # JPEG/PNG pages for vision models
    image_mime: str = "image/jpeg"
    pages: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip()) or bool(self.images)


_EXT_MAP = {
    ".pdf": DocKind.pdf,
    ".xlsx": DocKind.spreadsheet,
    ".xlsm": DocKind.spreadsheet,
    ".xls": DocKind.spreadsheet,
    ".csv": DocKind.csv,
    ".jpg": DocKind.image,
    ".jpeg": DocKind.image,
    ".png": DocKind.image,
    ".webp": DocKind.image,
    ".txt": DocKind.text,
}

_MIME_MAP = {
    "application/pdf": DocKind.pdf,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocKind.spreadsheet,
    "application/vnd.ms-excel": DocKind.spreadsheet,
    "text/csv": DocKind.csv,
    "image/jpeg": DocKind.image,
    "image/png": DocKind.image,
    "image/webp": DocKind.image,
    "text/plain": DocKind.text,
}


def sniff(content: bytes) -> DocKind | None:
    head = content[:8]
    if head.startswith(b"%PDF"):
        return DocKind.pdf
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
        return DocKind.image
    if head[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return DocKind.image
    if head.startswith(b"PK\x03\x04"):
        return DocKind.spreadsheet  # xlsx is a zip; parser rejects other zips
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return DocKind.spreadsheet  # legacy .xls (OLE2)
    return None


def detect_kind(content: bytes, mime_type: str | None, filename: str | None) -> DocKind:
    """Magic bytes win over declared MIME type, which wins over file extension."""
    sniffed = sniff(content)
    if sniffed:
        return sniffed
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime in _MIME_MAP:
        return _MIME_MAP[mime]
    if filename:
        lower = filename.lower()
        for ext, kind in _EXT_MAP.items():
            if lower.endswith(ext):
                return kind
    if mime.startswith("text/"):
        return DocKind.text
    return DocKind.unsupported


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"
