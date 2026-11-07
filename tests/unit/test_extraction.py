from datetime import date

from app.ai.fake_provider import FakeAIProvider
from app.extraction.validator import validate_extraction
from app.ingestion.parsers import parse_text
from app.models.invoice import ExtractedInvoice, FieldConfidence, LineItem, Party, PaymentStatus
from tests.conftest import ACME_INVOICE, GLOBEX_INVOICE

ai = FakeAIProvider()


def _extract(text: str) -> ExtractedInvoice:
    return ai.extract_invoice(parse_text(text.encode()))


def test_fake_extraction_acme():
    inv = _extract(ACME_INVOICE)
    assert inv.is_invoice
    assert inv.invoice_number == "INV-1001"
    assert inv.invoice_date == date(2026, 3, 5)
    assert inv.due_date == date(2026, 4, 4)
    assert inv.vendor.name == "ACME Office Supplies"
    assert inv.customer.name == "Lucky Traders"
    assert inv.currency == "USD"
    assert inv.subtotal == 130.0
    assert inv.tax_total == 13.0
    assert inv.total == 143.0
    assert [li.amount for li in inv.line_items] == [50.0, 80.0]
    assert inv.payment_status == PaymentStatus.unpaid


def test_validation_passes_for_consistent_invoice():
    result = validate_extraction(_extract(ACME_INVOICE), threshold=0.8, tolerance=0.05)
    assert result.errors == []
    assert result.low_confidence == []
    assert not result.needs_review


def test_validation_flags_total_mismatch():
    result = validate_extraction(_extract(GLOBEX_INVOICE), threshold=0.8, tolerance=0.05)
    assert result.needs_review
    assert any("total is 650.00" in e for e in result.errors)
    assert "total" in result.low_confidence


def test_validation_rules():
    inv = ExtractedInvoice(
        invoice_number="X1",
        invoice_date=date(2026, 5, 10),
        due_date=date(2026, 5, 1),
        currency="ZZZ",
        vendor=Party(name="V"),
        line_items=[LineItem(description="a", amount=10)],
        subtotal=12,
        total=12,
        field_confidence=[
            FieldConfidence(field=f, confidence=0.99) for f in ("invoice_number", "invoice_date", "vendor", "total")
        ],
    )
    result = validate_extraction(inv, threshold=0.8, tolerance=0.05)
    joined = " | ".join(result.errors)
    assert "Line items sum" in joined
    assert "Due date is before" in joined
    assert "currency" in joined


def test_missing_key_fields_are_low_confidence():
    inv = ExtractedInvoice(total=10.0)
    result = validate_extraction(inv, threshold=0.8, tolerance=0.05)
    assert {"invoice_number", "invoice_date", "vendor"} <= set(result.low_confidence)


def test_not_an_invoice():
    assert not _extract("Hey, are we still meeting tomorrow at 5?").is_invoice


def test_parse_correction():
    patch = ai.parse_correction("total is 1,250.00, due date 2026-03-31; vendor: Initech")
    assert patch.total == 1250.0
    assert patch.due_date == date(2026, 3, 31)
    assert patch.vendor_name == "Initech"
