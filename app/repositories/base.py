"""Repository interface. Every invoice/job/session operation is scoped by tenant_id."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from pydantic import BaseModel

from app.models.domain import ApiKey, Job, Session, Tenant, User
from app.models.invoice import InvoiceRecord, InvoiceStatus, PaymentStatus, normalise_key


class InvoiceFilter(BaseModel):
    vendor: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    payment_status: PaymentStatus | None = None
    status: InvoiceStatus | None = None
    invoice_number: str | None = None
    limit: int = 50

    def matches(self, inv: InvoiceRecord) -> bool:
        """In-Python filtering, shared by backends (Firestore can't do substring matching)."""
        if self.vendor and normalise_key(self.vendor) not in normalise_key(inv.vendor.name):
            return False
        if self.invoice_number and (inv.invoice_number or "").lower() != self.invoice_number.lower():
            return False
        if self.date_from and (not inv.invoice_date or inv.invoice_date < self.date_from):
            return False
        if self.date_to and (not inv.invoice_date or inv.invoice_date > self.date_to):
            return False
        if self.payment_status and inv.payment_status != self.payment_status:
            return False
        return not (self.status and inv.status != self.status)


class Repository(Protocol):
    # Idempotency
    def claim_message(self, message_id: str) -> bool:
        """Atomically record a WhatsApp message id. Returns False if already seen."""
        ...

    # Tenants & users
    def get_tenant(self, tenant_id: str) -> Tenant | None: ...
    def save_tenant(self, tenant: Tenant) -> None: ...
    def get_user(self, wa_id: str) -> User | None: ...
    def save_user(self, user: User) -> None: ...

    # API keys
    def get_api_key(self, key_hash: str) -> ApiKey | None: ...
    def save_api_key(self, key: ApiKey) -> None: ...

    # Jobs
    def save_job(self, job: Job) -> None: ...
    def get_job(self, tenant_id: str, job_id: str) -> Job | None: ...

    # Invoices
    def save_invoice(self, invoice: InvoiceRecord) -> None: ...
    def get_invoice(self, tenant_id: str, invoice_id: str) -> InvoiceRecord | None: ...
    def list_invoices(self, tenant_id: str, flt: InvoiceFilter) -> list[InvoiceRecord]: ...
    def find_by_sha256(self, tenant_id: str, sha256: str) -> InvoiceRecord | None: ...
    def find_by_number(
        self, tenant_id: str, vendor_name: str | None, invoice_number: str
    ) -> InvoiceRecord | None: ...
    def list_unindexed(self, tenant_id: str | None = None, limit: int = 100) -> list[InvoiceRecord]: ...
    def delete_invoice(self, tenant_id: str, invoice_id: str) -> None: ...

    # Sessions
    def get_session(self, wa_id: str) -> Session | None: ...
    def save_session(self, session: Session) -> None: ...
