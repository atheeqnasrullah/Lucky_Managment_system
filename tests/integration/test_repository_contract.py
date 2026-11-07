"""The same behavioural contract for every Repository backend.

Firestore runs only when FIRESTORE_EMULATOR_HOST is set (CI starts the emulator):
    gcloud emulators firestore start --host-port=localhost:8085
    set FIRESTORE_EMULATOR_HOST=localhost:8085
"""

import os
import uuid
from datetime import date

import pytest

from app.models.domain import Session, Tenant, User
from app.models.invoice import InvoiceRecord, InvoiceStatus, Party, PaymentStatus, SourceInfo
from app.repositories.base import InvoiceFilter
from app.repositories.memory import MemoryRepository


def _firestore():
    from google.cloud import firestore

    from app.repositories.firestore import FirestoreRepository

    return FirestoreRepository(client=firestore.Client(project="demo-test"))


BACKENDS = [pytest.param(MemoryRepository, id="memory")]
if os.environ.get("FIRESTORE_EMULATOR_HOST"):
    BACKENDS.append(pytest.param(_firestore, id="firestore"))


@pytest.fixture(params=BACKENDS)
def repo(request):
    return request.param()


def _inv(tenant: str, number: str, vendor: str, total: float, d: date, **kw) -> InvoiceRecord:
    return InvoiceRecord(
        id=uuid.uuid4().hex,
        tenant_id=tenant,
        invoice_number=number,
        vendor=Party(name=vendor),
        total=total,
        currency="USD",
        invoice_date=d,
        status=kw.pop("status", InvoiceStatus.confirmed),
        source=SourceInfo(gcs_uri="memory://x", mime_type="text/plain", filename="x", sha256=kw.pop("sha", uuid.uuid4().hex)),
        **kw,
    )


def test_claim_message_once(repo):
    mid = uuid.uuid4().hex
    assert repo.claim_message(mid)
    assert not repo.claim_message(mid)


def test_tenant_user_session_roundtrip(repo):
    t = Tenant(id=uuid.uuid4().hex, name="Acme")
    repo.save_tenant(t)
    assert repo.get_tenant(t.id).name == "Acme"
    wa = uuid.uuid4().hex[:12]
    repo.save_user(User(wa_id=wa, tenant_id=t.id))
    assert repo.get_user(wa).tenant_id == t.id
    s = Session(wa_id=wa, tenant_id=t.id)
    s.remember("user", "hi")
    repo.save_session(s)
    assert repo.get_session(wa).history[0].text == "hi"


def test_invoice_queries(repo):
    tenant = uuid.uuid4().hex
    a = _inv(tenant, "A-1", "Acme Corp", 100, date(2026, 1, 10), payment_status=PaymentStatus.unpaid, sha="sha-a")
    b = _inv(tenant, "B-1", "Beta LLC", 50, date(2026, 2, 10), payment_status=PaymentStatus.paid)
    c = _inv(tenant, "C-1", "Acme Corp", 25, date(2026, 3, 10), status=InvoiceStatus.needs_review)
    other = _inv(uuid.uuid4().hex, "A-1", "Acme Corp", 999, date(2026, 1, 10))
    for inv in (a, b, c, other):
        repo.save_invoice(inv)

    ids = lambda xs: {x.id for x in xs}  # noqa: E731
    assert ids(repo.list_invoices(tenant, InvoiceFilter())) == {a.id, b.id, c.id}
    assert ids(repo.list_invoices(tenant, InvoiceFilter(vendor="acme"))) == {a.id, c.id}
    assert ids(repo.list_invoices(tenant, InvoiceFilter(date_from=date(2026, 2, 1)))) == {b.id, c.id}
    assert ids(repo.list_invoices(tenant, InvoiceFilter(payment_status=PaymentStatus.unpaid))) == {a.id}
    assert ids(repo.list_invoices(tenant, InvoiceFilter(status=InvoiceStatus.needs_review))) == {c.id}
    assert [i.id for i in repo.list_invoices(tenant, InvoiceFilter())][0] == c.id  # newest first

    assert repo.find_by_sha256(tenant, "sha-a").id == a.id
    assert repo.find_by_number(tenant, "acme corp", "A-1").id == a.id
    assert repo.find_by_number(tenant, "Beta LLC", "A-1") is None

    unindexed = repo.list_unindexed(tenant)
    assert ids(unindexed) == {a.id, b.id}  # confirmed only

    repo.delete_invoice(tenant, a.id)
    assert repo.get_invoice(tenant, a.id) is None
    assert repo.get_invoice(tenant, other.id) is None  # wrong tenant
