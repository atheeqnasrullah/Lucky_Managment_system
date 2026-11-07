"""Parse WhatsApp Cloud API webhook payloads into flat, typed inbound messages."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class MessageKind(str, Enum):
    text = "text"
    media = "media"          # document / image
    button = "button"        # interactive reply or quick-reply button
    unsupported = "unsupported"


class InboundMessage(BaseModel):
    message_id: str
    wa_id: str
    timestamp: str | None = None
    kind: MessageKind
    profile_name: str | None = None
    text: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    caption: str | None = None
    button_id: str | None = None
    button_title: str | None = None
    context_message_id: str | None = None  # the message being replied to
    raw_type: str | None = None


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract user messages; delivery/read status callbacks are ignored."""
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts", []) or []
            }
            for msg in value.get("messages", []) or []:
                parsed = _parse_message(msg, names)
                if parsed:
                    out.append(parsed)
    return out


def _parse_message(msg: dict[str, Any], names: dict[str, str | None]) -> InboundMessage | None:
    msg_id, wa_id, mtype = msg.get("id"), msg.get("from"), msg.get("type")
    if not msg_id or not wa_id:
        return None
    base: dict[str, Any] = {
        "message_id": msg_id,
        "wa_id": wa_id,
        "timestamp": msg.get("timestamp"),
        "profile_name": names.get(wa_id),
        "context_message_id": (msg.get("context") or {}).get("id"),
        "raw_type": mtype,
    }
    if mtype == "text":
        return InboundMessage(kind=MessageKind.text, text=(msg.get("text") or {}).get("body", ""), **base)
    if mtype in ("document", "image"):
        media = msg.get(mtype) or {}
        return InboundMessage(
            kind=MessageKind.media,
            media_id=media.get("id"),
            mime_type=media.get("mime_type"),
            filename=media.get("filename") or f"{mtype}-{msg_id[-8:]}",
            caption=media.get("caption"),
            **base,
        )
    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return InboundMessage(
            kind=MessageKind.button, button_id=reply.get("id"), button_title=reply.get("title"), **base
        )
    if mtype == "button":
        btn = msg.get("button") or {}
        return InboundMessage(
            kind=MessageKind.button, button_id=btn.get("payload"), button_title=btn.get("text"), **base
        )
    return InboundMessage(kind=MessageKind.unsupported, **base)
