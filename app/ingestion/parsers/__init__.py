"""Route a document to the right parser."""

from __future__ import annotations

from app.core.errors import UnsupportedDocumentError
from app.ingestion.parsers.base import DocKind, ParsedDocument, detect_kind, truncate


def parse_text(content: bytes) -> ParsedDocument:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return ParsedDocument(kind=DocKind.text, text=truncate(content.decode(enc)))
        except UnicodeDecodeError:
            continue
    raise UnsupportedDocumentError("Could not decode text")


def parse_document(content: bytes, mime_type: str | None, filename: str | None) -> ParsedDocument:
    kind = detect_kind(content, mime_type, filename)
    if kind == DocKind.pdf:
        from app.ingestion.parsers.pdf import parse_pdf

        return parse_pdf(content)
    if kind == DocKind.spreadsheet:
        from app.ingestion.parsers.spreadsheet import parse_spreadsheet

        return parse_spreadsheet(content)
    if kind == DocKind.csv:
        from app.ingestion.parsers.spreadsheet import parse_csv

        return parse_csv(content)
    if kind == DocKind.image:
        from app.ingestion.parsers.image import parse_image

        return parse_image(content)
    if kind == DocKind.text:
        return parse_text(content)
    raise UnsupportedDocumentError(f"Unsupported file type: {mime_type or filename or 'unknown'}")


__all__ = ["DocKind", "ParsedDocument", "detect_kind", "parse_document", "parse_text"]
