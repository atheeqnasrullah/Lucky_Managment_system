"""Integration REST API (tenant resolved from X-API-Key)."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_container, require_tenant
from app.container import Container
from app.core.errors import NotFoundError, UnsupportedDocumentError
from app.models.invoice import InvoiceRecord, InvoiceStatus, PaymentStatus
from app.repositories.base import InvoiceFilter
from app.search.planner import InvoicePatch
from app.services.invoice_service import IngestKind

router = APIRouter(prefix="/v1", tags=["invoices"])

_PUBLIC_EXCLUDE = {"raw_text", "tenant_id"}


def _out(inv: InvoiceRecord, **extra: Any) -> dict[str, Any]:
    return {**inv.model_dump(mode="json", exclude=_PUBLIC_EXCLUDE), **extra}


class InvoiceUpdate(InvoicePatch):
    confirm: bool = Field(False, description="Mark the invoice as reviewed/confirmed")


class SearchRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000)


@router.get("/invoices")
def list_invoices(
    vendor: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_status: PaymentStatus | None = None,
    status_: InvoiceStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> dict:
    flt = InvoiceFilter(
        vendor=vendor, date_from=date_from, date_to=date_to,
        payment_status=payment_status, status=status_, limit=limit,
    )
    items = container.repo.list_invoices(tenant_id, flt)
    return {"items": [_out(i) for i in items], "count": len(items)}


@router.post("/invoices", status_code=status.HTTP_201_CREATED)
async def upload_invoice(
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> dict:
    limit = container.settings.max_upload_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")
    from fastapi.concurrency import run_in_threadpool

    try:
        outcome = await run_in_threadpool(
            lambda: container.invoices.ingest(
                tenant_id=tenant_id,
                content=content,
                mime_type=file.content_type,
                filename=file.filename or "upload",
                caption=caption,
                created_by="api",
            )
        )
    except UnsupportedDocumentError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    if outcome.kind == IngestKind.duplicate and outcome.duplicate_of:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"detail": "Duplicate document", "invoice_id": outcome.duplicate_of.id}
        )
    if outcome.kind == IngestKind.not_invoice:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Document is not an invoice")
    assert outcome.invoice
    return _out(outcome.invoice)


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: str,
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> dict:
    try:
        inv = container.invoices.get(tenant_id, invoice_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found") from exc
    return _out(inv, document_url=container.invoices.signed_document_url(inv))


@router.patch("/invoices/{invoice_id}")
def update_invoice(
    invoice_id: str,
    body: InvoiceUpdate,
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> dict:
    svc = container.invoices
    try:
        patch = InvoicePatch.model_validate(body.model_dump(exclude={"confirm"}))
        inv = svc.apply_patch(tenant_id, invoice_id, patch) if not patch.is_empty() else svc.get(tenant_id, invoice_id)
        if body.confirm and inv.status != InvoiceStatus.confirmed:
            inv = svc.confirm(tenant_id, invoice_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found") from exc
    return _out(inv)


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_invoice(
    invoice_id: str,
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> None:
    try:
        container.invoices.delete(tenant_id, invoice_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found") from exc


@router.post("/search")
def search(
    body: SearchRequest,
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> dict:
    result = container.queries.ask(tenant_id, body.question)
    return {
        "answer": result.text,
        "intent": result.plan.intent.value,
        "plan": result.plan.model_dump(mode="json"),
        "invoices": [_out(i) for i in result.invoices],
    }


CSV_COLUMNS = [
    "id", "invoice_number", "invoice_date", "due_date", "vendor", "customer", "currency",
    "subtotal", "tax_total", "discount", "total", "payment_status", "status",
]


@router.get("/exports/invoices.csv")
def export_csv(
    date_from: date | None = None,
    date_to: date | None = None,
    tenant_id: str = Depends(require_tenant),
    container: Container = Depends(get_container),
) -> StreamingResponse:
    items = container.repo.list_invoices(
        tenant_id, InvoiceFilter(date_from=date_from, date_to=date_to, limit=10_000)
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for i in items:
        writer.writerow(
            [
                i.id, i.invoice_number, i.invoice_date, i.due_date, i.vendor.name, i.customer.name,
                i.currency, i.subtotal, i.tax_total, i.discount, i.total, i.payment_status.value,
                i.status.value,
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoices.csv"'},
    )
