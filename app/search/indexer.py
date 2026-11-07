"""Turn invoice records into embedded chunks and keep the vector index in sync."""

from __future__ import annotations

from datetime import datetime, time, timezone

from app.ai.base import AIProvider
from app.conversation.formatter import money
from app.core.logging import get_logger
from app.models.invoice import InvoiceRecord, normalise_key
from app.repositories.base import Repository
from app.search.vector_store import VectorRecord, VectorStore

log = get_logger(__name__)

ITEMS_PER_CHUNK = 15
RAW_CHUNK_CHARS = 1500
MAX_RAW_CHUNKS = 4


def date_to_epoch(d) -> int | None:  # type: ignore[no-untyped-def]
    if d is None:
        return None
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp())


def build_chunks(inv: InvoiceRecord) -> list[tuple[str, str]]:
    """Return (chunk_type, text) pairs."""
    header = "\n".join(
        filter(
            None,
            [
                f"Invoice {inv.invoice_number or '(no number)'} from {inv.vendor.name or 'unknown vendor'}",
                f"Customer: {inv.customer.name}" if inv.customer.name else None,
                f"Invoice date: {inv.invoice_date}" if inv.invoice_date else None,
                f"Due date: {inv.due_date}" if inv.due_date else None,
                f"Total: {money(inv.total, inv.currency)}; tax {money(inv.tax_total, inv.currency)}",
                f"Payment status: {inv.payment_status.value}",
                inv.summary,
            ],
        )
    )
    chunks = [("header", header)]
    for i in range(0, len(inv.line_items), ITEMS_PER_CHUNK):
        group = inv.line_items[i : i + ITEMS_PER_CHUNK]
        body = "\n".join(
            f"- {li.description} (qty {li.quantity or 1:g}) {money(li.amount, inv.currency)}" for li in group
        )
        chunks.append(("line_items", f"Items on invoice {inv.invoice_number} from {inv.vendor.name}:\n{body}"))
    if inv.raw_text:
        raw = inv.raw_text
        for i in range(0, min(len(raw), RAW_CHUNK_CHARS * MAX_RAW_CHUNKS), RAW_CHUNK_CHARS):
            chunks.append(("raw", raw[i : i + RAW_CHUNK_CHARS]))
    return chunks


class Indexer:
    def __init__(self, ai: AIProvider, store: VectorStore, repo: Repository) -> None:
        self.ai = ai
        self.store = store
        self.repo = repo

    def index_invoice(self, inv: InvoiceRecord) -> None:
        chunks = build_chunks(inv)
        vectors = self.ai.embed([text for _, text in chunks])
        base_meta = {
            "invoice_id": inv.id,
            "vendor": inv.vendor.name or "",
            "vendor_key": normalise_key(inv.vendor.name),
            "invoice_number": inv.invoice_number or "",
            "currency": inv.currency or "",
            "total": float(inv.total or 0.0),
            "payment_status": inv.payment_status.value,
        }
        if (epoch := date_to_epoch(inv.invoice_date)) is not None:
            base_meta["invoice_date"] = epoch
        records = [
            VectorRecord(
                id=f"{inv.id}#{n}",
                values=vec,
                metadata={**base_meta, "chunk_type": ctype, "text": text[:2000]},
            )
            for n, ((ctype, text), vec) in enumerate(zip(chunks, vectors, strict=True))
        ]
        self.store.delete_prefix(inv.tenant_id, f"{inv.id}#")
        self.store.upsert(inv.tenant_id, records)

    def index_and_mark(self, inv: InvoiceRecord) -> bool:
        """Index; on failure keep the invoice (indexed=False) for the reconcile job."""
        try:
            self.index_invoice(inv)
            inv.indexed = True
        except Exception:  # noqa: BLE001
            log.exception("indexing failed; will retry via reconcile")
            inv.indexed = False
        self.repo.save_invoice(inv)
        return inv.indexed

    def remove_invoice(self, tenant_id: str, invoice_id: str) -> None:
        self.store.delete_prefix(tenant_id, f"{invoice_id}#")

    def reconcile(self, limit: int = 100) -> int:
        count = 0
        for inv in self.repo.list_unindexed(limit=limit):
            if self.index_and_mark(inv):
                count += 1
        return count
