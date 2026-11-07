"""Admin & developer CLI.

    python -m app.cli create-tenant --name "Acme Traders" --wa-id 919876543210 --currency INR
    python -m app.cli create-api-key --tenant-id <id> --name erp-sync
    python -m app.cli reconcile-index
    python -m app.cli chat            # local WhatsApp simulator (in-memory, no credentials)
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.security import generate_api_key, hash_api_key
from app.models.domain import ApiKey, Tenant, User


def _container(synchronous: bool = True):  # type: ignore[no-untyped-def]
    from app.container import build_container

    return build_container(get_settings(), synchronous_tasks=synchronous)


def create_tenant(args: argparse.Namespace) -> None:
    c = _container()
    tenant = Tenant(id=uuid.uuid4().hex, name=args.name, currency=args.currency)
    c.repo.save_tenant(tenant)
    for wa_id in args.wa_id or []:
        c.repo.save_user(User(wa_id=wa_id, tenant_id=tenant.id))
    print(f"tenant_id={tenant.id}")


def add_user(args: argparse.Namespace) -> None:
    c = _container()
    if not c.repo.get_tenant(args.tenant_id):
        sys.exit("Unknown tenant")
    c.repo.save_user(User(wa_id=args.wa_id, tenant_id=args.tenant_id, display_name=args.name))
    print("ok")


def create_api_key(args: argparse.Namespace) -> None:
    c = _container()
    if not c.repo.get_tenant(args.tenant_id):
        sys.exit("Unknown tenant")
    key = generate_api_key()
    c.repo.save_api_key(ApiKey(key_hash=hash_api_key(key), tenant_id=args.tenant_id, name=args.name))
    print(f"API key (shown once): {key}")


def reconcile_index(_: argparse.Namespace) -> None:
    print(f"reindexed={_container().indexer.reconcile()}")


def chat(args: argparse.Namespace) -> None:
    """Simulate a WhatsApp conversation against the full pipeline using in-memory backends."""
    from app.whatsapp.client import FakeWhatsAppClient
    from app.whatsapp.models import InboundMessage, MessageKind

    c = _container(synchronous=True)
    if not isinstance(c.whatsapp, FakeWhatsAppClient):
        sys.exit("chat requires WHATSAPP_BACKEND=fake")
    wa: FakeWhatsAppClient = c.whatsapp
    wa_id = args.wa_id
    print("WhatsApp simulator. Type a message, '/file <path>' to upload, '/tap <n>' to press a button, '/quit'.")
    buttons: list[tuple[str, str]] = []
    seen = 0

    def flush() -> None:
        nonlocal seen, buttons
        for m in wa.sent[seen:]:
            if m.type == "buttons":
                buttons = m.extra["buttons"]
                opts = "  ".join(f"[{i + 1}] {t}" for i, (_, t) in enumerate(buttons))
                print(f"\n🤖 {m.body}\n   {opts}")
            else:
                print(f"\n🤖 {m.body}")
        seen = len(wa.sent)

    while True:
        try:
            line = input("\n🧑 ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        mid = uuid.uuid4().hex
        if line.startswith("/file "):
            path = Path(line[6:].strip().strip('"'))
            if not path.exists():
                print("File not found")
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            wa.add_media(mid, path.read_bytes(), mime)
            msg = InboundMessage(
                message_id=mid, wa_id=wa_id, kind=MessageKind.media, media_id=mid,
                mime_type=mime, filename=path.name,
            )
        elif line.startswith("/tap "):
            try:
                bid, title = buttons[int(line[5:]) - 1]
            except (ValueError, IndexError):
                print("No such button")
                continue
            msg = InboundMessage(message_id=mid, wa_id=wa_id, kind=MessageKind.button, button_id=bid, button_title=title)
        else:
            msg = InboundMessage(message_id=mid, wa_id=wa_id, kind=MessageKind.text, text=line)
        c.conversation.handle(msg)
        flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-tenant")
    p.add_argument("--name", required=True)
    p.add_argument("--currency", default="USD")
    p.add_argument("--wa-id", action="append", help="WhatsApp number (repeatable)")
    p.set_defaults(func=create_tenant)

    p = sub.add_parser("add-user")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--wa-id", required=True)
    p.add_argument("--name")
    p.set_defaults(func=add_user)

    p = sub.add_parser("create-api-key")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--name", default="default")
    p.set_defaults(func=create_api_key)

    p = sub.add_parser("reconcile-index")
    p.set_defaults(func=reconcile_index)

    p = sub.add_parser("chat")
    p.add_argument("--wa-id", default="15550001111")
    p.set_defaults(func=chat)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
