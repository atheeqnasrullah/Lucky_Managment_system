"""Thread-safe in-memory repository for local development and tests."""

from __future__ import annotations

import threading

from app.models.domain import ApiKey, Job, Session, Tenant, User
from app.models.invoice import InvoiceRecord, InvoiceStatus, normalise_key
from app.repositories.base import InvoiceFilter


class MemoryRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.messages: set[str] = set()
        self.tenants: dict[str, Tenant] = {}
        self.users: dict[str, User] = {}
        self.api_keys: dict[str, ApiKey] = {}
        self.jobs: dict[tuple[str, str], Job] = {}
        self.invoices: dict[tuple[str, str], InvoiceRecord] = {}
        self.sessions: dict[str, Session] = {}

    def claim_message(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self.messages:
                return False
            self.messages.add(message_id)
            return True

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self.tenants.get(tenant_id)

    def save_tenant(self, tenant: Tenant) -> None:
        with self._lock:
            self.tenants[tenant.id] = tenant.model_copy(deep=True)

    def get_user(self, wa_id: str) -> User | None:
        u = self.users.get(wa_id)
        return u.model_copy(deep=True) if u else None

    def save_user(self, user: User) -> None:
        with self._lock:
            self.users[user.wa_id] = user.model_copy(deep=True)

    def get_api_key(self, key_hash: str) -> ApiKey | None:
        return self.api_keys.get(key_hash)

    def save_api_key(self, key: ApiKey) -> None:
        with self._lock:
            self.api_keys[key.key_hash] = key

    def save_job(self, job: Job) -> None:
        with self._lock:
            self.jobs[(job.tenant_id, job.id)] = job.model_copy(deep=True)

    def get_job(self, tenant_id: str, job_id: str) -> Job | None:
        j = self.jobs.get((tenant_id, job_id))
        return j.model_copy(deep=True) if j else None

    def save_invoice(self, invoice: InvoiceRecord) -> None:
        with self._lock:
            self.invoices[(invoice.tenant_id, invoice.id)] = invoice.model_copy(deep=True)

    def get_invoice(self, tenant_id: str, invoice_id: str) -> InvoiceRecord | None:
        inv = self.invoices.get((tenant_id, invoice_id))
        return inv.model_copy(deep=True) if inv else None

    def _tenant_invoices(self, tenant_id: str) -> list[InvoiceRecord]:
        with self._lock:
            return [v.model_copy(deep=True) for (t, _), v in self.invoices.items() if t == tenant_id]

    def list_invoices(self, tenant_id: str, flt: InvoiceFilter) -> list[InvoiceRecord]:
        items = [i for i in self._tenant_invoices(tenant_id) if flt.matches(i)]
        items.sort(key=lambda i: (i.invoice_date.isoformat() if i.invoice_date else "", i.created_at), reverse=True)
        return items[: flt.limit]

    def find_by_sha256(self, tenant_id: str, sha256: str) -> InvoiceRecord | None:
        for inv in self._tenant_invoices(tenant_id):
            if inv.source and inv.source.sha256 == sha256 and inv.status != InvoiceStatus.failed:
                return inv
        return None

    def find_by_number(
        self, tenant_id: str, vendor_name: str | None, invoice_number: str
    ) -> InvoiceRecord | None:
        for inv in self._tenant_invoices(tenant_id):
            if (
                inv.invoice_number
                and inv.invoice_number.lower() == invoice_number.lower()
                and normalise_key(inv.vendor.name) == normalise_key(vendor_name)
                and inv.status != InvoiceStatus.failed
            ):
                return inv
        return None

    def list_unindexed(self, tenant_id: str | None = None, limit: int = 100) -> list[InvoiceRecord]:
        with self._lock:
            out = [
                v.model_copy(deep=True)
                for (t, _), v in self.invoices.items()
                if not v.indexed
                and v.status == InvoiceStatus.confirmed
                and (tenant_id is None or t == tenant_id)
            ]
        return out[:limit]

    def delete_invoice(self, tenant_id: str, invoice_id: str) -> None:
        with self._lock:
            self.invoices.pop((tenant_id, invoice_id), None)

    def get_session(self, wa_id: str) -> Session | None:
        s = self.sessions.get(wa_id)
        return s.model_copy(deep=True) if s else None

    def save_session(self, session: Session) -> None:
        with self._lock:
            self.sessions[session.wa_id] = session.model_copy(deep=True)
