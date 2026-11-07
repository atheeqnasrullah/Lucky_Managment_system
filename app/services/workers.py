"""Background task handlers (invoked by Cloud Tasks or the inline queue)."""

from __future__ import annotations

from typing import Any

from app.conversation.formatter import invoice_summary, review_request
from app.core.config import Settings
from app.core.errors import (
    DocumentTooLargeError,
    ExtractionError,
    UnsupportedDocumentError,
)
from app.core.logging import get_logger, log_extra
from app.models.domain import Job, JobStatus, JobType, Session
from app.models.invoice import InvoiceStatus
from app.repositories.base import Repository
from app.search.query_service import QueryService
from app.services.invoice_service import IngestKind, InvoiceService
from app.whatsapp.client import WhatsAppClient

log = get_logger(__name__)

PERMANENT_ERRORS = (UnsupportedDocumentError, DocumentTooLargeError)


def review_buttons(invoice_id: str) -> list[tuple[str, str]]:
    return [(f"ok:{invoice_id}", "✅ Confirm"), (f"del:{invoice_id}", "🗑️ Discard")]


def confirmed_buttons(invoice_id: str) -> list[tuple[str, str]]:
    return [(f"paid:{invoice_id}", "💰 Mark paid"), (f"del:{invoice_id}", "🗑️ Delete")]


class Workers:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        whatsapp: WhatsAppClient,
        invoices: InvoiceService,
        queries: QueryService,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.wa = whatsapp
        self.invoices = invoices
        self.queries = queries

    # -- document processing ---------------------------------------------------------------
    def process_document(self, payload: dict[str, Any], retry_count: int) -> None:
        tenant_id, job_id, wa_id = payload["tenant_id"], payload["job_id"], payload["wa_id"]
        job = self.repo.get_job(tenant_id, job_id) or Job(
            id=job_id, tenant_id=tenant_id, type=JobType.process_document
        )
        if job.status in (JobStatus.succeeded, JobStatus.failed):
            return  # idempotent re-delivery

        if payload.get("_final_failure"):
            self._fail(job, wa_id, "Sorry, I couldn't process that document after several attempts. Please try again later.")
            return

        job.status = JobStatus.running
        job.attempts = retry_count + 1
        self.repo.save_job(job)

        try:
            if payload.get("text"):
                content = payload["text"].encode()
                mime, filename = "text/plain", "message.txt"
            else:
                content, mime = self.wa.download_media(payload["media_id"], self.settings.max_upload_bytes)
                mime = payload.get("mime_type") or mime
                filename = payload.get("filename") or "document"

            outcome = self.invoices.ingest(
                tenant_id=tenant_id,
                content=content,
                mime_type=mime,
                filename=filename,
                caption=payload.get("caption"),
                wa_message_id=payload.get("message_id"),
                created_by=wa_id,
            )
        except PERMANENT_ERRORS as exc:
            self._fail(job, wa_id, f"⚠️ I couldn't read that file: {exc}.\nSupported: PDF, Excel/CSV, JPG/PNG photos and text.")
            return
        except ExtractionError:
            raise  # transient: let the queue retry
        except Exception:
            log.exception("process_document failed", extra=log_extra(job_id=job_id))
            raise

        if outcome.kind == IngestKind.duplicate and outcome.duplicate_of:
            dup = outcome.duplicate_of
            self.wa.send_text(
                wa_id,
                f"ℹ️ I already have this document: invoice *{dup.invoice_number or '(no #)'}* "
                f"from {dup.vendor.name or 'unknown vendor'}. Nothing new was saved.",
            )
            self._succeed(job, dup.id)
            return
        if outcome.kind == IngestKind.not_invoice:
            self.wa.send_text(wa_id, "🤔 That doesn't look like an invoice or bill, so I didn't save it.")
            self._succeed(job, None)
            return

        inv = outcome.invoice
        assert inv is not None
        if inv.status == InvoiceStatus.needs_review:
            session = self.repo.get_session(wa_id) or Session(wa_id=wa_id, tenant_id=tenant_id)
            session.pending_invoice_id = inv.id
            session.last_intent = "review"
            self.repo.save_session(session)
            self.wa.send_text(wa_id, review_request(inv))
            self.wa.send_buttons(wa_id, "Is this invoice correct?", review_buttons(inv.id))
        else:
            self.wa.send_text(wa_id, invoice_summary(inv))
            self.wa.send_buttons(wa_id, "Saved ✅ Anything else?", confirmed_buttons(inv.id))
        self._succeed(job, inv.id)

    def _succeed(self, job: Job, invoice_id: str | None) -> None:
        job.status = JobStatus.succeeded
        job.invoice_id = invoice_id
        job.error = None
        self.repo.save_job(job)

    def _fail(self, job: Job, wa_id: str, message: str) -> None:
        job.status = JobStatus.failed
        job.error = message
        self.repo.save_job(job)
        self.wa.send_text(wa_id, message)

    # -- queries ---------------------------------------------------------------------------
    def answer_query(self, payload: dict[str, Any], retry_count: int) -> None:
        tenant_id, wa_id, question = payload["tenant_id"], payload["wa_id"], payload["question"]
        if payload.get("_final_failure"):
            self.wa.send_text(wa_id, "Sorry, I couldn't answer that right now. Please try again in a moment.")
            return
        session = self.repo.get_session(wa_id) or Session(wa_id=wa_id, tenant_id=tenant_id)
        result = self.queries.ask(tenant_id, question, session.history[:-1])
        self.wa.send_text(wa_id, result.text)
        session.remember("assistant", result.text)
        session.last_intent = result.plan.intent.value
        self.repo.save_session(session)
