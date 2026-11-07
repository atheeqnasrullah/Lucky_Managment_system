"""WhatsApp webhook: verification challenge + inbound events. Must ack within seconds."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import get_container
from app.container import Container
from app.core.logging import get_logger, log_extra
from app.core.security import verify_whatsapp_signature
from app.whatsapp.models import parse_webhook

router = APIRouter(tags=["webhook"])
log = get_logger(__name__)


@router.get("/webhook")
def verify(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
    container: Container = Depends(get_container),
) -> Response:
    expected = container.settings.whatsapp_verify_token.get_secret_value()
    if hub_mode == "subscribe" and expected and hub_verify_token == expected:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed")


@router.post("/webhook")
async def receive(request: Request, container: Container = Depends(get_container)) -> dict:
    body = await request.body()
    settings = container.settings
    secret = settings.whatsapp_app_secret.get_secret_value()
    signature = request.headers.get("X-Hub-Signature-256")

    if secret or settings.is_production:
        if not verify_whatsapp_signature(body, signature, secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from exc

    for msg in parse_webhook(payload):
        if not await run_in_threadpool(container.repo.claim_message, msg.message_id):
            log.info("duplicate webhook delivery", extra=log_extra(message_id=msg.message_id))
            continue
        try:
            await run_in_threadpool(container.conversation.handle, msg)
        except Exception:  # noqa: BLE001
            # Never 5xx back to Meta for app bugs: it would redeliver and we've already claimed it.
            log.exception("failed to handle message", extra=log_extra(message_id=msg.message_id))
            try:
                await run_in_threadpool(
                    container.whatsapp.send_text, msg.wa_id, "⚠️ Something went wrong. Please try again."
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to send error reply")
    return {"status": "ok"}
