"""End-to-end: WhatsApp webhook -> pipeline -> replies, using in-memory backends."""

from app.models.invoice import InvoiceStatus, PaymentStatus
from app.repositories.base import InvoiceFilter
from tests.conftest import (
    ACME_INVOICE,
    GLOBEX_INVOICE,
    WA_ID,
    button_message,
    make_pdf,
    make_png,
    post_webhook,
    text_message,
    upload_via_whatsapp,
)


def _tenant(container):
    return container.repo.get_user(WA_ID).tenant_id


def _invoices(container):
    return container.repo.list_invoices(_tenant(container), InvoiceFilter(limit=100))


def test_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_rejects_bad_signature(client):
    assert post_webhook(client, text_message("hi"), secret="wrong").status_code == 401


def test_help(client, wa):
    post_webhook(client, text_message("help"))
    assert "Billing Assistant" in wa.texts_to(WA_ID)[-1]


def test_duplicate_delivery_is_ignored(client, wa):
    payload = text_message("help")
    post_webhook(client, payload)
    post_webhook(client, payload)
    assert len(wa.texts_to(WA_ID)) == 1


def test_pdf_upload_confirmed_and_indexed(client, wa, container):
    r = upload_via_whatsapp(client, wa, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    assert r.status_code == 200

    [inv] = _invoices(container)
    assert inv.status == InvoiceStatus.confirmed
    assert inv.invoice_number == "INV-1001" and inv.total == 143.0
    assert inv.indexed
    assert inv.source and inv.source.gcs_uri.startswith("memory://tenants/")
    texts = wa.texts_to(WA_ID)
    assert "Got *acme.pdf*" in texts[0]
    assert "Invoice processed" in texts[1] and "USD 143.00" in texts[1]
    assert wa.sent[-1].type == "buttons"


def test_duplicate_upload_detected(client, wa, container):
    pdf = make_pdf(ACME_INVOICE)
    upload_via_whatsapp(client, wa, pdf, "acme.pdf", "application/pdf")
    upload_via_whatsapp(client, wa, pdf, "acme-again.pdf", "application/pdf")
    assert len(_invoices(container)) == 1
    assert "already have this document" in wa.texts_to(WA_ID)[-1]


def test_review_then_correction_flow(client, wa, container):
    upload_via_whatsapp(client, wa, GLOBEX_INVOICE.encode(), "globex.txt", "text/plain")
    [inv] = _invoices(container)
    assert inv.status == InvoiceStatus.needs_review
    assert not inv.indexed
    assert "Please check this invoice" in wa.texts_to(WA_ID)[-1]

    post_webhook(client, text_message("total is 600.00"))
    inv = container.repo.get_invoice(inv.tenant_id, inv.id)
    assert inv.total == 600.0
    assert inv.status == InvoiceStatus.confirmed
    assert inv.indexed
    assert "Updated and saved" in wa.texts_to(WA_ID)[-1]


def test_review_confirm_button(client, wa, container):
    upload_via_whatsapp(client, wa, GLOBEX_INVOICE.encode(), "globex.txt", "text/plain")
    [inv] = _invoices(container)
    post_webhook(client, button_message(f"ok:{inv.id}"))
    assert container.repo.get_invoice(inv.tenant_id, inv.id).status == InvoiceStatus.confirmed


def test_discard_by_text(client, wa, container):
    upload_via_whatsapp(client, wa, GLOBEX_INVOICE.encode(), "globex.txt", "text/plain")
    post_webhook(client, text_message("discard"))
    assert _invoices(container) == []
    assert "deleted" in wa.texts_to(WA_ID)[-1]


def test_mark_paid_by_command(client, wa, container):
    upload_via_whatsapp(client, wa, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    post_webhook(client, text_message("mark INV-1001 as paid"))
    [inv] = _invoices(container)
    assert inv.payment_status == PaymentStatus.paid


def test_invoice_pasted_as_text(client, wa, container):
    post_webhook(client, text_message(ACME_INVOICE))
    [inv] = _invoices(container)
    assert inv.invoice_number == "INV-1001"


def test_non_invoice_document(client, wa, container):
    upload_via_whatsapp(client, wa, b"Shopping list: eggs, milk", "list.txt", "text/plain")
    assert _invoices(container) == []
    assert "doesn't look like an invoice" in wa.texts_to(WA_ID)[-1]


def test_unsupported_file(client, wa, container):
    upload_via_whatsapp(client, wa, b"\x00\x01binary", "x.bin", "application/x-foo")
    assert "couldn't read that file" in wa.texts_to(WA_ID)[-1]


def test_unreadable_photo_goes_to_review(client, wa, container):
    # The offline AI can't read images, so a photo yields empty fields -> must be reviewed.
    upload_via_whatsapp(client, wa, make_png(), "photo.png", "image/png")
    [inv] = _invoices(container)
    assert inv.status == InvoiceStatus.needs_review


def test_questions(client, wa, container):
    upload_via_whatsapp(client, wa, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    upload_via_whatsapp(client, wa, GLOBEX_INVOICE.encode(), "globex.txt", "text/plain")
    post_webhook(client, text_message("total is 600"))  # fix + confirm Globex

    post_webhook(client, text_message("How much did I spend in March 2026?"))
    answer = wa.texts_to(WA_ID)[-1]
    assert "USD 743.00" in answer and "2 invoices" in answer

    post_webhook(client, text_message("How much did I spend with ACME?"))
    assert "USD 143.00" in wa.texts_to(WA_ID)[-1]

    post_webhook(client, text_message("show unpaid invoices"))
    assert "INV-1001" in wa.texts_to(WA_ID)[-1]

    post_webhook(client, text_message("spend by vendor in 2026"))
    by_vendor = wa.texts_to(WA_ID)[-1]
    assert "Globex Ltd" in by_vendor and "ACME Office Supplies" in by_vendor

    post_webhook(client, text_message("toner cartridge"))
    assert "INV-1001" in wa.texts_to(WA_ID)[-1]

    post_webhook(client, text_message("find invoice no G-77"))
    assert "Globex" in wa.texts_to(WA_ID)[-1]

    session = container.repo.get_session(WA_ID)
    assert session.history[-1].role == "assistant"


def test_tenant_isolation(client, wa, container):
    other = "15559998888"
    upload_via_whatsapp(client, wa, make_pdf(ACME_INVOICE), "acme.pdf", "application/pdf")
    post_webhook(client, text_message("How much did I spend in March 2026?", wa_id=other))
    assert "couldn't find" in wa.texts_to(other)[-1]
    post_webhook(client, text_message("toner cartridge", wa_id=other))
    assert "INV-1001" not in wa.texts_to(other)[-1]
    assert container.repo.get_user(other).tenant_id != _tenant(container)
