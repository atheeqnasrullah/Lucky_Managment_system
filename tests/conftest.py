from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.container import Container, build_container
from app.core.config import Settings
from app.core.security import compute_signature
from app.main import create_app
from app.whatsapp.client import FakeWhatsAppClient

APP_SECRET = "test-secret"
WA_ID = "15550001111"

ACME_INVOICE = """ACME Office Supplies
Invoice No: INV-1001
Invoice Date: 2026-03-05
Due Date: 2026-04-04
Bill To: Lucky Traders

Printer paper A4 10 x 5.00 50.00
Toner cartridge 2 x 40.00 80.00
Subtotal: 130.00
Tax (10%): 13.00
Total: USD 143.00
"""

# Totals don't add up (500 + 100 != 650) -> needs review
GLOBEX_INVOICE = """Globex Ltd
Invoice No: G-77
Date: 12/03/2026
Bill To: Lucky Traders
Consulting services 1 x 500.00 500.00
Subtotal: 500.00
VAT: 100.00
Total: 650.00
"""


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        whatsapp_app_secret=APP_SECRET,
        whatsapp_verify_token="verify-me",
        rate_limit_per_minute=100,
    )


@pytest.fixture
def container(settings: Settings) -> Container:
    return build_container(settings, synchronous_tasks=True)


@pytest.fixture
def wa(container: Container) -> FakeWhatsAppClient:
    assert isinstance(container.whatsapp, FakeWhatsAppClient)
    return container.whatsapp


@pytest.fixture
def client(settings: Settings, container: Container) -> TestClient:
    return TestClient(create_app(settings, container))


# ---- webhook helpers -----------------------------------------------------------------------
def _envelope(message: dict[str, Any], wa_id: str = WA_ID, name: str = "Test User") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [{"from": wa_id, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": "1", **message}],
                        },
                    }
                ],
            }
        ],
    }


def text_message(body: str, wa_id: str = WA_ID) -> dict[str, Any]:
    return _envelope({"type": "text", "text": {"body": body}}, wa_id)


def document_message(media_id: str, filename: str, mime: str, wa_id: str = WA_ID) -> dict[str, Any]:
    kind = "image" if mime.startswith("image/") else "document"
    media: dict[str, Any] = {"id": media_id, "mime_type": mime}
    if kind == "document":
        media["filename"] = filename
    return _envelope({"type": kind, kind: media}, wa_id)


def button_message(button_id: str, wa_id: str = WA_ID) -> dict[str, Any]:
    return _envelope(
        {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}},
        wa_id,
    )


def post_webhook(client: TestClient, payload: dict[str, Any], secret: str = APP_SECRET):  # type: ignore[no-untyped-def]
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": compute_signature(body, secret)},
    )


def upload_via_whatsapp(
    client: TestClient, wa: FakeWhatsAppClient, content: bytes, filename: str, mime: str, wa_id: str = WA_ID
):  # type: ignore[no-untyped-def]
    media_id = uuid.uuid4().hex
    wa.add_media(media_id, content, mime)
    return post_webhook(client, document_message(media_id, filename, mime, wa_id))


# ---- document builders ---------------------------------------------------------------------
def make_pdf(text: str) -> bytes:
    """Minimal single-page PDF with a real text layer (no external deps)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    lines = text.strip().splitlines()
    ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    for line in lines:
        ops.append(f"({esc(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_xlsx(rows: list[list[Any]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_png(width: int = 200, height: int = 100) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()
