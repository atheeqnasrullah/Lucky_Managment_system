"""Routes each inbound WhatsApp message: uploads, button taps, corrections, commands, questions.

Runs inside the webhook request, so it must stay fast: anything slow is enqueued.
"""

from __future__ import annotations

import re
import uuid

from app.ai.base import AIProvider
from app.conversation.formatter import HELP_TEXT, invoice_summary
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger, log_extra
from app.core.security import SlidingWindowRateLimiter
from app.models.domain import Job, JobType, Session, Tenant, User
from app.models.invoice import InvoiceStatus, PaymentStatus, utcnow
from app.repositories.base import InvoiceFilter, Repository
from app.services.invoice_service import InvoiceService
from app.services.workers import confirmed_buttons
from app.tasks.queue import TASK_ANSWER_QUERY, TASK_PROCESS_DOCUMENT, TaskQueue
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.models import InboundMessage, MessageKind

log = get_logger(__name__)

_HELP = re.compile(r"^\s*(help|menu|start|hi|hello|hey|/start|\?)\s*[!.]*\s*$", re.I)
_CONFIRM = re.compile(r"^\s*(yes|y|ok|okay|confirm|correct|looks good|👍|✅)\s*[!.]*\s*$", re.I)
_DISCARD = re.compile(r"^\s*(no|discard|cancel|delete( it)?|ignore)\s*[!.]*\s*$", re.I)
_MARK_PAID = re.compile(
    r"^\s*(?:mark\s+)?(?:invoice\s*#?\s*)?([A-Z0-9][A-Z0-9\-/]{2,})\s+(?:as\s+)?(paid|unpaid)\s*$", re.I
)
_CORRECTION_HINT = re.compile(
    r"\b(total|subtotal|tax|vat|gst|amount|date|due|vendor|supplier|customer|number|currency|discount)\b"
    r".{0,20}(?:is|=|:|should be|to)\s*\S",
    re.I,
)


def looks_like_invoice_text(text: str) -> bool:
    if len(text) < 120:
        return False
    signals = sum(
        bool(re.search(p, text, re.I))
        for p in (r"invoice|bill\b|receipt", r"total", r"\d+[.,]\d{2}", r"date|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}")
    )
    return signals >= 3


class ConversationHandler:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        whatsapp: WhatsAppClient,
        queue: TaskQueue,
        invoices: InvoiceService,
        ai: AIProvider,
        limiter: SlidingWindowRateLimiter,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.wa = whatsapp
        self.queue = queue
        self.invoices = invoices
        self.ai = ai
        self.limiter = limiter

    # -- identity --------------------------------------------------------------------------
    def resolve_user(self, msg: InboundMessage) -> User | None:
        user = self.repo.get_user(msg.wa_id)
        if user:
            user.last_active_at = utcnow()
            if msg.profile_name and not user.display_name:
                user.display_name = msg.profile_name
            self.repo.save_user(user)
            return user
        if not self.settings.auto_provision_tenants:
            return None
        tenant = Tenant(id=uuid.uuid4().hex, name=msg.profile_name or self.settings.default_tenant_name)
        self.repo.save_tenant(tenant)
        user = User(wa_id=msg.wa_id, tenant_id=tenant.id, display_name=msg.profile_name)
        self.repo.save_user(user)
        log.info("provisioned tenant", extra=log_extra(tenant_id=tenant.id))
        return user

    # -- entrypoint ------------------------------------------------------------------------
    def handle(self, msg: InboundMessage) -> None:
        user = self.resolve_user(msg)
        if user is None:
            self.wa.send_text(msg.wa_id, "This number isn't registered for the billing assistant. Please contact your administrator.")
            return
        if not self.limiter.allow(msg.wa_id):
            self.wa.send_text(msg.wa_id, "⏳ You're sending messages too quickly. Please wait a minute.")
            return
        self.wa.mark_read(msg.message_id)

        session = self.repo.get_session(msg.wa_id) or Session(wa_id=msg.wa_id, tenant_id=user.tenant_id)
        session.tenant_id = user.tenant_id

        if msg.kind == MessageKind.media:
            self._on_media(msg, user, session)
        elif msg.kind == MessageKind.button:
            self._on_button(msg, user, session)
            self.repo.save_session(session)
        elif msg.kind == MessageKind.text:
            self._on_text(msg, user, session)
        else:
            self.wa.send_text(msg.wa_id, "I can read invoices (PDF, Excel, photos, text) and answer billing questions. Type *help* for examples.")

    def _save_then_enqueue(self, session: Session, task: str, payload: dict) -> None:
        # Persist session first: the worker may update it (possibly before we'd return).
        self.repo.save_session(session)
        self.queue.enqueue(task, payload)

    # -- handlers --------------------------------------------------------------------------
    def _enqueue_document(self, msg: InboundMessage, user: User, session: Session, **extra: object) -> None:
        job = Job(id=uuid.uuid4().hex, tenant_id=user.tenant_id, type=JobType.process_document)
        self.repo.save_job(job)
        self._save_then_enqueue(
            session,
            TASK_PROCESS_DOCUMENT,
            {
                "job_id": job.id,
                "tenant_id": user.tenant_id,
                "wa_id": msg.wa_id,
                "message_id": msg.message_id,
                **extra,
            },
        )

    def _on_media(self, msg: InboundMessage, user: User, session: Session) -> None:
        self.wa.send_text(msg.wa_id, f"📥 Got *{msg.filename or 'your file'}*. Reading it now…")
        session.remember("user", f"[uploaded {msg.filename}]")
        self._enqueue_document(
            msg,
            user,
            session,
            media_id=msg.media_id,
            mime_type=msg.mime_type,
            filename=msg.filename,
            caption=msg.caption,
        )

    def _on_button(self, msg: InboundMessage, user: User, session: Session) -> None:
        action, _, invoice_id = (msg.button_id or "").partition(":")
        try:
            if action == "ok":
                inv = self.invoices.confirm(user.tenant_id, invoice_id)
                self.wa.send_text(msg.wa_id, f"✅ Saved invoice *{inv.invoice_number or '(no #)'}*.")
                self.wa.send_buttons(msg.wa_id, "Anything else?", confirmed_buttons(inv.id))
            elif action == "del":
                self.invoices.delete(user.tenant_id, invoice_id)
                self.wa.send_text(msg.wa_id, "🗑️ Invoice deleted.")
            elif action == "paid":
                inv = self.invoices.set_payment_status(user.tenant_id, invoice_id, PaymentStatus.paid)
                self.wa.send_text(msg.wa_id, f"💰 Marked *{inv.invoice_number or 'invoice'}* as paid.")
            else:
                self.wa.send_text(msg.wa_id, HELP_TEXT)
                return
        except NotFoundError:
            self.wa.send_text(msg.wa_id, "I couldn't find that invoice. It may have been deleted.")
        if session.pending_invoice_id == invoice_id:
            session.pending_invoice_id = None

    def _on_text(self, msg: InboundMessage, user: User, session: Session) -> None:
        text = (msg.text or "").strip()
        if not text:
            return
        if _HELP.match(text):
            self.wa.send_text(msg.wa_id, HELP_TEXT)
            return

        pending = session.pending_invoice_id
        if pending:
            if _CONFIRM.match(text):
                self._on_button(msg.model_copy(update={"button_id": f"ok:{pending}"}), user, session)
                self.repo.save_session(session)
                return
            if _DISCARD.match(text):
                self._on_button(msg.model_copy(update={"button_id": f"del:{pending}"}), user, session)
                self.repo.save_session(session)
                return
            if _CORRECTION_HINT.search(text) and self._apply_correction(msg, user, session, pending, text):
                self.repo.save_session(session)
                return

        if m := _MARK_PAID.match(text):
            self._mark_by_number(msg, user, m[1], PaymentStatus(m[2].lower()))
            return

        if looks_like_invoice_text(text):
            self.wa.send_text(msg.wa_id, "📥 Looks like an invoice. Reading it now…")
            session.remember("user", "[sent invoice as text]")
            self._enqueue_document(msg, user, session, text=text)
            return

        session.remember("user", text)
        self._save_then_enqueue(
            session,
            TASK_ANSWER_QUERY,
            {"tenant_id": user.tenant_id, "wa_id": msg.wa_id, "question": text},
        )

    def _apply_correction(
        self, msg: InboundMessage, user: User, session: Session, invoice_id: str, text: str
    ) -> bool:
        patch = self.ai.parse_correction(text)
        if patch.is_empty():
            return False
        try:
            inv = self.invoices.apply_patch(user.tenant_id, invoice_id, patch)
        except NotFoundError:
            session.pending_invoice_id = None
            return False
        if inv.status == InvoiceStatus.confirmed:
            session.pending_invoice_id = None
            self.wa.send_text(msg.wa_id, "✅ Updated and saved.\n\n" + invoice_summary(inv))
        else:
            issues = "\n⚠️ ".join(inv.validation_errors)
            self.wa.send_text(
                msg.wa_id,
                "✏️ Updated.\n\n" + invoice_summary(inv)
                + (f"\n\n⚠️ {issues}" if issues else "")
                + "\n\nReply *yes* to confirm or send more fixes.",
            )
        return True

    def _mark_by_number(self, msg: InboundMessage, user: User, number: str, status: PaymentStatus) -> None:
        matches = self.repo.list_invoices(user.tenant_id, InvoiceFilter(invoice_number=number, limit=5))
        if not matches:
            self.wa.send_text(msg.wa_id, f"I couldn't find invoice {number}.")
            return
        if len(matches) > 1:
            self.wa.send_text(msg.wa_id, f"Several invoices are numbered {number}; please use the buttons on the one you mean.")
            return
        inv = self.invoices.set_payment_status(user.tenant_id, matches[0].id, status)
        self.wa.send_text(msg.wa_id, f"👍 Invoice *{inv.invoice_number}* marked as {status.value}.")
