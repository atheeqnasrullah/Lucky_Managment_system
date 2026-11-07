"""Image normalisation for vision extraction."""

from __future__ import annotations

import io

from app.core.errors import UnsupportedDocumentError
from app.ingestion.parsers.base import DocKind, ParsedDocument

MAX_SIDE = 2048


def parse_image(content: bytes) -> ParsedDocument:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img: Image.Image = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img)  # fix phone-camera rotation
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedDocumentError("Unreadable image") from exc

    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    # Mild contrast boost helps with photos of paper receipts.
    img = ImageOps.autocontrast(img, cutoff=1)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return ParsedDocument(kind=DocKind.image, images=[buf.getvalue()], image_mime="image/jpeg")
