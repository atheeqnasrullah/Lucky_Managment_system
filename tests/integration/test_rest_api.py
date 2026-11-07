import pytest

from app.core.security import generate_api_key, hash_api_key
from app.models.domain import ApiKey, Tenant
from tests.conftest import ACME_INVOICE, GLOBEX_INVOICE, make_pdf


def _api_key(container, tenant_id: str) -> dict:
    container.repo.save_tenant(Tenant(id=tenant_id, name=tenant_id))
    key = generate_api_key()
    container.repo.save_api_key(ApiKey(key_hash=hash_api_key(key), tenant_id=tenant_id))
    return {"X-API-Key": key}


@pytest.fixture
def auth(container):
    return _api_key(container, "tenant-a")


def _upload(client, auth, content: bytes, name: str, mime: str):
    return client.post("/v1/invoices", headers=auth, files={"file": (name, content, mime)})


def test_requires_api_key(client):
    assert client.get("/v1/invoices").status_code == 401
    assert client.get("/v1/invoices", headers={"X-API-Key": "bogus"}).status_code == 401


def test_health(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_upload_list_get(client, auth):
    r = _upload(client, auth, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invoice_number"] == "INV-1001" and body["status"] == "confirmed"
    assert "raw_text" not in body and "tenant_id" not in body

    listing = client.get("/v1/invoices", headers=auth, params={"vendor": "acme"}).json()
    assert listing["count"] == 1

    detail = client.get(f"/v1/invoices/{body['id']}", headers=auth).json()
    assert detail["document_url"].startswith("https://storage.local/")


def test_duplicate_and_unsupported(client, auth):
    pdf = make_pdf(ACME_INVOICE)
    _upload(client, auth, pdf, "a.pdf", "application/pdf")
    assert _upload(client, auth, pdf, "a.pdf", "application/pdf").status_code == 409
    assert _upload(client, auth, b"\x00\x01", "x.bin", "application/x-foo").status_code == 415


def test_patch_confirm_and_delete(client, auth):
    inv = _upload(client, auth, GLOBEX_INVOICE.encode(), "g.txt", "text/plain").json()
    assert inv["status"] == "needs_review"

    patched = client.patch(f"/v1/invoices/{inv['id']}", headers=auth, json={"total": 600, "payment_status": "paid"}).json()
    assert patched["total"] == 600 and patched["status"] == "confirmed" and patched["payment_status"] == "paid"

    assert client.delete(f"/v1/invoices/{inv['id']}", headers=auth).status_code == 204
    assert client.get(f"/v1/invoices/{inv['id']}", headers=auth).status_code == 404


def test_search_and_export(client, auth):
    _upload(client, auth, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    res = client.post("/v1/search", headers=auth, json={"question": "How much did I spend in March 2026?"}).json()
    assert res["intent"] == "aggregate" and "143.00" in res["answer"]

    csv = client.get("/v1/exports/invoices.csv", headers=auth)
    assert csv.status_code == 200
    assert csv.text.splitlines()[0].startswith("id,invoice_number")
    assert "INV-1001" in csv.text


def test_cross_tenant_access_denied(client, container, auth):
    inv = _upload(client, auth, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf").json()
    other = _api_key(container, "tenant-b")
    assert client.get(f"/v1/invoices/{inv['id']}", headers=other).status_code == 404
    assert client.get("/v1/invoices", headers=other).json()["count"] == 0
    assert client.delete(f"/v1/invoices/{inv['id']}", headers=other).status_code == 404
