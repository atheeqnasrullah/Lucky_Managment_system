import httpx
import pytest
import respx

from app.core.errors import DocumentTooLargeError
from app.whatsapp.client import CloudWhatsAppClient

BASE = "https://graph.test/v21.0"


@pytest.fixture
def client() -> CloudWhatsAppClient:
    return CloudWhatsAppClient(token="tok", phone_number_id="PNID", api_version="v21.0", api_base="https://graph.test")


@respx.mock
def test_send_text_splits_and_authenticates(client):
    route = respx.post(f"{BASE}/PNID/messages").mock(return_value=httpx.Response(200, json={"messages": [{"id": "x"}]}))
    client.send_text("123", "a" * 5000)
    assert route.call_count == 2
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer tok"
    assert b'"messaging_product":"whatsapp"' in req.content.replace(b" ", b"")


@respx.mock
def test_send_retries_on_5xx(client):
    route = respx.post(f"{BASE}/PNID/messages").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={})]
    )
    client.send_buttons("123", "Pick", [("a", "A very long button title here"), ("b", "B")])
    assert route.call_count == 2
    body = route.calls[1].request.content.decode()
    assert "A very long button t" in body and "A very long button title" not in body  # 20-char limit


@respx.mock
def test_4xx_not_retried(client):
    route = respx.post(f"{BASE}/PNID/messages").mock(return_value=httpx.Response(400, json={"error": {}}))
    with pytest.raises(httpx.HTTPStatusError):
        client.send_text("123", "hi")
    assert route.call_count == 1


@respx.mock
def test_download_media(client):
    respx.get(f"{BASE}/MEDIA1").mock(
        return_value=httpx.Response(200, json={"url": "https://cdn.test/f", "mime_type": "application/pdf", "file_size": 3})
    )
    respx.get("https://cdn.test/f").mock(return_value=httpx.Response(200, content=b"%PD"))
    content, mime = client.download_media("MEDIA1", max_bytes=100)
    assert content == b"%PD" and mime == "application/pdf"


@respx.mock
def test_download_media_too_large(client):
    respx.get(f"{BASE}/BIG").mock(
        return_value=httpx.Response(200, json={"url": "https://cdn.test/b", "mime_type": "image/jpeg", "file_size": 10_000})
    )
    with pytest.raises(DocumentTooLargeError):
        client.download_media("BIG", max_bytes=100)
