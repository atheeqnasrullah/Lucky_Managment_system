"""WhatsApp Cloud API client (and an in-memory fake for local dev/tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.errors import DocumentTooLargeError
from app.core.logging import get_logger, log_extra

log = get_logger(__name__)

MAX_TEXT = 4096
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20


@dataclass
class MediaInfo:
    url: str
    mime_type: str
    file_size: int | None = None
    sha256: str | None = None


class WhatsAppClient(Protocol):
    def send_text(self, to: str, text: str) -> None: ...
    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None: ...
    def send_document(self, to: str, link: str, filename: str, caption: str | None = None) -> None: ...
    def mark_read(self, message_id: str) -> None: ...
    def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]: ...


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split long replies on paragraph/line boundaries to fit WhatsApp's 4096-char limit."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return [p.rstrip() for p in parts if p.strip()]


class CloudWhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, api_version: str, api_base: str) -> None:
        self._base = f"{api_base.rstrip('/')}/{api_version}"
        self._phone_number_id = phone_number_id
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30.0, connect=5.0)
        )

    @_retry
    def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/{self._phone_number_id}/messages",
            json={"messaging_product": "whatsapp", **payload},
        )
        if resp.status_code >= 400:
            log.warning("whatsapp send failed", extra=log_extra(status=resp.status_code, body=resp.text[:500]))
        resp.raise_for_status()
        return resp.json()

    def send_text(self, to: str, text: str) -> None:
        for chunk in split_text(text):
            self._post_message({"to": to, "type": "text", "text": {"body": chunk, "preview_url": False}})

    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        self._post_message(
            {
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid, "title": title[:MAX_BUTTON_TITLE]}}
                            for bid, title in buttons[:MAX_BUTTONS]
                        ]
                    },
                },
            }
        )

    def send_document(self, to: str, link: str, filename: str, caption: str | None = None) -> None:
        doc: dict[str, Any] = {"link": link, "filename": filename}
        if caption:
            doc["caption"] = caption[:1024]
        self._post_message({"to": to, "type": "document", "document": doc})

    def mark_read(self, message_id: str) -> None:
        try:
            self._post_message({"status": "read", "message_id": message_id})
        except httpx.HTTPError:
            log.info("mark_read failed (ignored)")

    @_retry
    def _media_info(self, media_id: str) -> MediaInfo:
        resp = self._http.get(f"{self._base}/{media_id}")
        resp.raise_for_status()
        data = resp.json()
        return MediaInfo(
            url=data["url"],
            mime_type=data.get("mime_type", "application/octet-stream"),
            file_size=data.get("file_size"),
            sha256=data.get("sha256"),
        )

    def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        # Media URLs expire quickly; always fetch a fresh URL right before downloading.
        info = self._media_info(media_id)
        if info.file_size and info.file_size > max_bytes:
            raise DocumentTooLargeError(f"File is {info.file_size} bytes (limit {max_bytes})")
        return self._download(info.url, max_bytes), info.mime_type

    @_retry
    def _download(self, url: str, max_bytes: int) -> bytes:
        buf = bytearray()
        with self._http.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise DocumentTooLargeError(f"File exceeds {max_bytes} bytes")
        return bytes(buf)


@dataclass
class SentMessage:
    to: str
    type: str
    body: str
    extra: dict[str, Any] = field(default_factory=dict)


class FakeWhatsAppClient:
    """Records outbound messages and serves media from an in-memory map."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.media: dict[str, tuple[bytes, str]] = {}
        self.read: list[str] = []

    def add_media(self, media_id: str, content: bytes, mime_type: str) -> None:
        self.media[media_id] = (content, mime_type)

    def send_text(self, to: str, text: str) -> None:
        for chunk in split_text(text):
            log.info("[fake-whatsapp] -> %s: %s", to, chunk[:200])
            self.sent.append(SentMessage(to=to, type="text", body=chunk))

    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        self.sent.append(SentMessage(to=to, type="buttons", body=body, extra={"buttons": buttons}))

    def send_document(self, to: str, link: str, filename: str, caption: str | None = None) -> None:
        self.sent.append(
            SentMessage(to=to, type="document", body=caption or "", extra={"link": link, "filename": filename})
        )

    def mark_read(self, message_id: str) -> None:
        self.read.append(message_id)

    def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        content, mime = self.media[media_id]
        if len(content) > max_bytes:
            raise DocumentTooLargeError(f"File exceeds {max_bytes} bytes")
        return content, mime

    def texts_to(self, to: str) -> list[str]:
        return [m.body for m in self.sent if m.to == to and m.type == "text"]
