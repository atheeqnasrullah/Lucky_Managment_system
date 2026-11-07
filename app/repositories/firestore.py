"""Google Firestore repository.

Layout:
    processed_messages/{wa_message_id}        idempotency (TTL policy on `expire_at`)
    users/{wa_id}                             sender -> tenant mapping
    api_keys/{sha256}                         integration API keys
    sessions/{wa_id}                          conversation state (TTL on `expire_at`)
    tenants/{tenant_id}
    tenants/{tenant_id}/invoices/{invoice_id}
    tenants/{tenant_id}/jobs/{job_id}
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.models.domain import ApiKey, Job, Session, Tenant, User
from app.models.invoice import InvoiceRecord, InvoiceStatus, normalise_key, utcnow
from app.repositories.base import InvoiceFilter

_SCAN_CAP = 2000  # upper bound on docs scanned for in-Python filtering


class FirestoreRepository:
    def __init__(self, project: str | None = None, client: firestore.Client | None = None) -> None:
        self.db = client or firestore.Client(project=project or None)

    # ---- helpers -------------------------------------------------------------------------
    def _tenant(self, tenant_id: str) -> firestore.DocumentReference:
        return self.db.collection("tenants").document(tenant_id)

    def _invoices(self, tenant_id: str) -> firestore.CollectionReference:
        return self._tenant(tenant_id).collection("invoices")

    @staticmethod
    def _dump(model: Any) -> dict[str, Any]:
        data = model.model_dump(mode="json")
        for key in ("created_at", "updated_at", "last_active_at"):
            if hasattr(model, key):
                data[key] = getattr(model, key)
        return data

    # ---- idempotency ---------------------------------------------------------------------
    def claim_message(self, message_id: str) -> bool:
        ref = self.db.collection("processed_messages").document(message_id)
        try:
            ref.create({"created_at": utcnow(), "expire_at": utcnow() + timedelta(days=7)})
            return True
        except AlreadyExists:
            return False

    # ---- tenants & users -----------------------------------------------------------------
    def get_tenant(self, tenant_id: str) -> Tenant | None:
        snap = self._tenant(tenant_id).get()
        return Tenant.model_validate(snap.to_dict() or {}) if snap.exists else None

    def save_tenant(self, tenant: Tenant) -> None:
        self._tenant(tenant.id).set(self._dump(tenant))

    def get_user(self, wa_id: str) -> User | None:
        snap = self.db.collection("users").document(wa_id).get()
        return User.model_validate(snap.to_dict() or {}) if snap.exists else None

    def save_user(self, user: User) -> None:
        self.db.collection("users").document(user.wa_id).set(self._dump(user))

    def get_api_key(self, key_hash: str) -> ApiKey | None:
        snap = self.db.collection("api_keys").document(key_hash).get()
        return ApiKey.model_validate(snap.to_dict() or {}) if snap.exists else None

    def save_api_key(self, key: ApiKey) -> None:
        self.db.collection("api_keys").document(key.key_hash).set(self._dump(key))

    # ---- jobs ----------------------------------------------------------------------------
    def save_job(self, job: Job) -> None:
        job.updated_at = utcnow()
        self._tenant(job.tenant_id).collection("jobs").document(job.id).set(self._dump(job))

    def get_job(self, tenant_id: str, job_id: str) -> Job | None:
        snap = self._tenant(tenant_id).collection("jobs").document(job_id).get()
        return Job.model_validate(snap.to_dict() or {}) if snap.exists else None

    # ---- invoices ------------------------------------------------------------------------
    def save_invoice(self, invoice: InvoiceRecord) -> None:
        invoice.updated_at = utcnow()
        self._invoices(invoice.tenant_id).document(invoice.id).set(invoice.to_firestore())

    def get_invoice(self, tenant_id: str, invoice_id: str) -> InvoiceRecord | None:
        snap = self._invoices(tenant_id).document(invoice_id).get()
        return InvoiceRecord.from_firestore(snap.to_dict() or {}) if snap.exists else None

    def list_invoices(self, tenant_id: str, flt: InvoiceFilter) -> list[InvoiceRecord]:
        q: Any = self._invoices(tenant_id)
        # Push down what Firestore can do natively; the rest is filtered in Python.
        if flt.status:
            q = q.where(filter=FieldFilter("status", "==", flt.status.value))
        if flt.payment_status:
            q = q.where(filter=FieldFilter("payment_status", "==", flt.payment_status.value))
        if flt.date_from:
            q = q.where(filter=FieldFilter("invoice_date", ">=", flt.date_from.isoformat()))
        if flt.date_to:
            q = q.where(filter=FieldFilter("invoice_date", "<=", flt.date_to.isoformat()))
        q = q.order_by("invoice_date", direction=firestore.Query.DESCENDING).limit(_SCAN_CAP)
        results = []
        for snap in q.stream():
            inv = InvoiceRecord.from_firestore(snap.to_dict() or {})
            if flt.matches(inv):
                results.append(inv)
                if len(results) >= flt.limit:
                    break
        return results

    def find_by_sha256(self, tenant_id: str, sha256: str) -> InvoiceRecord | None:
        q = self._invoices(tenant_id).where(filter=FieldFilter("source.sha256", "==", sha256)).limit(5)
        for snap in q.stream():
            inv = InvoiceRecord.from_firestore(snap.to_dict() or {})
            if inv.status != InvoiceStatus.failed:
                return inv
        return None

    def find_by_number(
        self, tenant_id: str, vendor_name: str | None, invoice_number: str
    ) -> InvoiceRecord | None:
        q = (
            self._invoices(tenant_id)
            .where(filter=FieldFilter("vendor_key", "==", normalise_key(vendor_name)))
            .where(filter=FieldFilter("invoice_number", "==", invoice_number))
            .limit(5)
        )
        for snap in q.stream():
            inv = InvoiceRecord.from_firestore(snap.to_dict() or {})
            if inv.status != InvoiceStatus.failed:
                return inv
        return None

    def list_unindexed(self, tenant_id: str | None = None, limit: int = 100) -> list[InvoiceRecord]:
        base: Any = self._invoices(tenant_id) if tenant_id else self.db.collection_group("invoices")
        q = (
            base.where(filter=FieldFilter("indexed", "==", False))
            .where(filter=FieldFilter("status", "==", InvoiceStatus.confirmed.value))
            .limit(limit)
        )
        return [InvoiceRecord.from_firestore(s.to_dict() or {}) for s in q.stream()]

    def delete_invoice(self, tenant_id: str, invoice_id: str) -> None:
        self._invoices(tenant_id).document(invoice_id).delete()

    # ---- sessions ------------------------------------------------------------------------
    def get_session(self, wa_id: str) -> Session | None:
        snap = self.db.collection("sessions").document(wa_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.pop("expire_at", None)
        return Session.model_validate(data)

    def save_session(self, session: Session) -> None:
        session.updated_at = utcnow()
        data = self._dump(session)
        data["updated_at"] = session.updated_at
        data["expire_at"] = utcnow() + timedelta(hours=24)
        self.db.collection("sessions").document(session.wa_id).set(data)
