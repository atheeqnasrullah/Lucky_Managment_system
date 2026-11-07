from app.whatsapp.client import split_text
from app.whatsapp.models import MessageKind, parse_webhook
from tests.conftest import button_message, document_message, text_message


def test_parse_text():
    [msg] = parse_webhook(text_message("hello"))
    assert msg.kind == MessageKind.text and msg.text == "hello"
    assert msg.profile_name == "Test User"


def test_parse_document_and_image():
    [doc] = parse_webhook(document_message("m1", "inv.pdf", "application/pdf"))
    assert doc.kind == MessageKind.media and doc.media_id == "m1" and doc.filename == "inv.pdf"
    [img] = parse_webhook(document_message("m2", "", "image/jpeg"))
    assert img.kind == MessageKind.media and img.filename.startswith("image-")


def test_parse_button():
    [btn] = parse_webhook(button_message("ok:abc"))
    assert btn.kind == MessageKind.button and btn.button_id == "ok:abc"


def test_status_callbacks_ignored():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert parse_webhook(payload) == []


def test_split_text():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_text(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
