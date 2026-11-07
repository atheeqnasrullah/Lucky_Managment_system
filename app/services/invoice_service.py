"""Invoice lifecycle: ingest -> parse -> extract -> validate -> store -> index; plus edits."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import Enum

from app.ai.base import AIProvider
from app.core.config import Settings
from app.core.errors import NotFoundError, UnsupportedDocumentError
from app.core.logging import get_logger, log_extra
from app.extraction.validator import revalidate_record, validate_extraction
from app.ingestion.parsers import parse_document
from app.ingestion.storage import DocumentStorage, invoice_object_path
from app.models.invoice import InvoiceRecord, InvoiceStatus, PaymentStatus, SourceInfo, utcnow
from app.repositories.base import Repository
from app.search.indexer import Indexer
from app.search.planner import InvoicePatch

log = get_logger(__name__)

RAW_TEXT_LIMIT = 20_000


class IngestKind(str, Enum):
    created = "created"
    duplicate = "duplicate"
    not_invoice = "not_invoice"


@dataclass
class IngestOutcome:
    kind: IngestKind
    invoice: InvoiceRecord | None = None
    duplicate_of: InvoiceRecord | None = None


def build_summary(inv: InvoiceRecord) -> str:
    parts = [f"Invoice {inv.invoice_number or '(no number)'} from {inv.vendor.name or 'unknown vendor'}"]
    if inv.invoice_date:
        parts.append(f"dated {inv.invoice_date}")
    if inv.total is not None:
        parts.append(f"for {inv.currency or ''} {inv.total:,.2f}".replace("  ", " "))
    if inv.line_items:
        names = ", ".join(li.description for li in inv.line_items[:5])
        parts.append(f"covering {len(inv.line_items)} item(s): {names}")
    return " ".join(parts) + "."


class InvoiceService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        storage: DocumentStorage,
        ai: AIProvider,
        indexer: Indexer,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.ai = ai
        self.indexer = indexer

    # -- ingestion -------------------------------------------------------------------------
    def ingest(
        self,
        *,
        tenant_id: str,
        content: bytes,
        mime_type: str | None,
        filename: str,
        caption: str | None = None,
        wa_message_id: str | None = None,
        created_by: str | None = None,
    ) -> IngestOutcome:
        if not content:
            raise UnsupportedDocumentError("Empty file")
        sha = hashlib.sha256(content).hexdigest()
        if existing := self.repo.find_by_sha256(tenant_id, sha):
            return IngestOutcome(IngestKind.duplicate, duplicate_of=existing)

        parsed = parse_document(content, mime_type, filename)
        if not parsed.has_content:
            raise UnsupportedDocumentError("The document appears to be empty")

        invoice_id = uuid.uuid4().hex
        now = utcnow()
        uri = self.storage.put(
            invoice_object_path(tenant_id, invoice_id, filename, now.year, now.month),
            content,
            mime_type or "application/octet-stream",
        )

        extracted = self.ai.extract_invoice(parsed, caption)
        if not extracted.is_invoice:
            log.info("document is not an invoice", extra=log_extra(tenant_id=tenant_id))
            return IngestOutcome(IngestKind.not_invoice)

        result = validate_extraction(
            extracted, self.settings.confidence_threshold, self.settings.amount_tolerance
        )
        record = InvoiceRecord.from_extraction(
            id=invoice_id,
            tenant_id=tenant_id,
            extracted=extracted,
            source=SourceInfo(
                gcs_uri=uri,
                mime_type=mime_type or "application/octet-stream",
                filename=filename,
                sha256=sha,
                wa_message_id=wa_message_id,
            ),
            raw_text=parsed.text[:RAW_TEXT_LIMIT] or None,
            validation_errors=result.errors,
            low_confidence_fields=result.low_confidence,
            created_by=created_by,
        )
        if not record.currency:
            tenant = self.repo.get_tenant(tenant_id)
            if tenant:
                record.currency = tenant.currency

        if record.invoice_number and (
            dup := self.repo.find_by_number(tenant_id, record.vendor.name, record.invoice_number)
        ):
            record.validation_errors.append(
                f"Possible duplicate of invoice {dup.invoice_number} already saved on {dup.created_at:%Y-%m-%d}"
            )

        record.status = (
            InvoiceStatus.needs_review
            if result.needs_review or record.validation_errors
            else InvoiceStatus.confirmed
        )
        record.summary = build_summary(record)
        self.repo.save_invoice(record)
        if record.status == InvoiceStatus.confirmed:
            self.indexer.index_and_mark(record)
        log.info(
            "invoice ingested",
            extra=log_extra(tenant_id=tenant_id, invoice_id=invoice_id, status=record.status.value),
        )
        return IngestOutcome(IngestKind.created, invoice=record)

    # -- edits -----------------------------------------------------------------------------
    def get(self, tenant_id: str, invoice_id: str) -> InvoiceRecord:
        inv = self.repo.get_invoice(tenant_id, invoice_id)
        if not inv:
            raise NotFoundError(invoice_id)
        return inv

    def confirm(self, tenant_id: str, invoice_id: str) -> InvoiceRecord:
        inv = self.get(tenant_id, invoice_id)
        inv.status = InvoiceStatus.confirmed
        inv.low_confidence_fields = []
        self.indexer.index_and_mark(inv)  # saves
        return inv

    def apply_patch(self, tenant_id: str, invoice_id: str, patch: InvoicePatch) -> InvoiceRecord:
        inv = self.get(tenant_id, invoice_id)
        changes = patch.model_dump(exclude_none=True)
        touched: set[str] = set()
        for key, value in changes.items():
            if key == "vendor_name":
                inv.vendor.name = value
                touched.add("vendor")
            elif key == "customer_name":
                inv.customer.name = value
                touched.add("customer")
            else:
                setattr(inv, key, value)
                touched.add(key)
        for f in touched:
            inv.confidence[f] = 1.0
        inv.validation_errors = revalidate_record(inv, self.settings.amount_tolerance)
        remaining = [f for f in inv.low_confidence_fields if f not in touched]
        if not inv.validation_errors:
            # Fields flagged only by cross-checks (e.g. totals mismatch) are fine once the rules pass;
            # genuinely low-confidence or missing fields still need the user's attention.
            threshold = self.settings.confidence_threshold
            remaining = [f for f in remaining if inv.confidence.get(f, 0.0) < threshold]
        inv.low_confidence_fields = remaining
        inv.summary = build_summary(inv)
        if inv.status == InvoiceStatus.needs_review and not inv.validation_errors and not inv.low_confidence_fields:
            inv.status = InvoiceStatus.confirmed
        if inv.status == InvoiceStatus.confirmed:
            self.indexer.index_and_mark(inv)
        else:
            self.repo.save_invoice(inv)
        return inv

    def set_payment_status(self, tenant_id: str, invoice_id: str, status: PaymentStatus) -> InvoiceRecord:
        inv = self.get(tenant_id, invoice_id)
        inv.payment_status = status
        inv.summary = build_summary(inv)
        if inv.status == InvoiceStatus.confirmed:
            self.indexer.index_and_mark(inv)
        else:
            self.repo.save_invoice(inv)
        return inv

    def delete(self, tenant_id: str, invoice_id: str) -> None:
        self.get(tenant_id, invoice_id)
        self.indexer.remove_invoice(tenant_id, invoice_id)
        self.repo.delete_invoice(tenant_id, invoice_id)

    def signed_document_url(self, inv: InvoiceRecord) -> str | None:
        if not inv.source:
            return None
        return self.storage.signed_url(inv.source.gcs_uri, self.settings.signed_url_ttl_minutes)
