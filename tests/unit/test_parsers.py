import pytest

from app.core.errors import UnsupportedDocumentError
from app.ingestion.parsers import DocKind, detect_kind, parse_document
from tests.conftest import ACME_INVOICE, make_pdf, make_png, make_xlsx


def test_detect_kind_prefers_magic_bytes():
    assert detect_kind(b"%PDF-1.4 ...", "text/plain", "x.txt") == DocKind.pdf
    assert detect_kind(make_png(), "application/octet-stream", None) == DocKind.image
    assert detect_kind(b"a,b\n1,2", "text/csv", None) == DocKind.csv
    assert detect_kind(b"a,b\n1,2", None, "data.csv") == DocKind.csv
    assert detect_kind(b"hello", "text/plain", None) == DocKind.text
    assert detect_kind(b"\x00\x01", "application/x-foo", "x.bin") == DocKind.unsupported


def test_parse_text_layer_pdf():
    doc = parse_document(make_pdf(ACME_INVOICE), "application/pdf", "inv.pdf")
    assert doc.kind == DocKind.pdf
    assert "INV-1001" in doc.text
    assert "Total: USD 143.00" in doc.text
    assert not doc.images  # has a text layer, no vision fallback


def test_scanned_pdf_falls_back_to_page_images():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(buf, format="PDF")  # image-only PDF, no text layer
    doc = parse_document(buf.getvalue(), "application/pdf", "scan.pdf")
    assert "scanned_pdf" in doc.notes
    assert len(doc.images) == 1 and doc.images[0][:3] == b"\xff\xd8\xff"


def test_parse_xlsx():
    content = make_xlsx([["Item", "Qty", "Amount"], ["Widget", 2, 20.5], [None, None, None], ["Total", None, 20.5]])
    doc = parse_document(content, None, "bill.xlsx")
    assert doc.kind == DocKind.spreadsheet
    assert "Widget | 2 | 20.5" in doc.text
    assert "Sheet" in doc.text


def test_parse_csv():
    doc = parse_document(b"item,amount\nTea,3.50\nCoffee,4.00\n", "text/csv", "x.csv")
    assert doc.kind == DocKind.csv
    assert "Coffee | 4.00" in doc.text or "Coffee | 4.0" in doc.text


def test_parse_image_normalises_to_jpeg():
    doc = parse_document(make_png(4000, 1000), "image/png", None)
    assert doc.kind == DocKind.image
    assert len(doc.images) == 1
    assert doc.images[0][:3] == b"\xff\xd8\xff"


def test_unsupported_raises():
    with pytest.raises(UnsupportedDocumentError):
        parse_document(b"\x00\x01\x02", "application/x-foo", "x.bin")
