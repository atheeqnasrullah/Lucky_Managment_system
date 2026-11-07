"""PDF parsing: use the text layer when present, render pages to images for scanned PDFs."""

from __future__ import annotations

import io

from app.ingestion.parsers.base import DocKind, ParsedDocument, truncate

MAX_VISION_PAGES = 5
MIN_CHARS_PER_PAGE = 40


def parse_pdf(content: bytes) -> ParsedDocument:
    import pdfplumber

    texts: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            tables = page.extract_tables() or []
            table_text = "\n".join(
                " | ".join(cell or "" for cell in row) for table in tables for row in table if row
            )
            texts.append(f"--- Page {i + 1} ---\n{page_text}\n{table_text}".strip())

    text = "\n\n".join(texts)
    doc = ParsedDocument(kind=DocKind.pdf, text=truncate(text), pages=page_count)

    meaningful = sum(len(t.split("---", 2)[-1].strip()) for t in texts)
    if page_count and meaningful / page_count < MIN_CHARS_PER_PAGE:
        # Likely a scanned PDF: hand rendered pages to the vision model.
        doc.images = render_pages(content, MAX_VISION_PAGES)
        doc.notes.append("scanned_pdf")
    return doc


def render_pages(content: bytes, max_pages: int, scale: float = 2.0) -> list[bytes]:
    import pypdfium2 as pdfium

    images: list[bytes] = []
    pdf = pdfium.PdfDocument(content)
    try:
        for i in range(min(len(pdf), max_pages)):
            pil = pdf[i].render(scale=scale).to_pil().convert("RGB")
            pil.thumbnail((2048, 2048))
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=85)
            images.append(buf.getvalue())
    finally:
        pdf.close()
    return images
