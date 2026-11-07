"""Business-rule validation of extracted invoices and confidence gating."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.invoice import ExtractedInvoice, InvoiceRecord

KEY_FIELDS = ("invoice_number", "invoice_date", "vendor", "total")
ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "AED", "SAR", "CAD", "AUD", "SGD", "JPY", "CNY", "CHF", "NZD",
    "ZAR", "HKD", "SEK", "NOK", "DKK", "MYR", "THB", "IDR", "PHP", "PKR", "BDT", "LKR", "NPR",
    "QAR", "KWD", "BHD", "OMR", "EGP", "NGN", "KES", "BRL", "MXN", "TRY", "KRW",
}


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    low_confidence: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.errors or self.low_confidence)


def _close(a: float, b: float, tolerance: float) -> bool:
    # absolute tolerance for small invoices, relative (0.5%) for large ones
    return abs(a - b) <= max(tolerance, abs(b) * 0.005)


def validate_values(
    *,
    invoice_date,  # type: ignore[no-untyped-def]
    due_date,
    currency: str | None,
    line_amounts: list[float],
    subtotal: float | None,
    tax_total: float | None,
    discount: float | None,
    total: float | None,
    tolerance: float,
) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    suspect: set[str] = set()

    if total is None:
        errors.append("Total amount is missing")
        suspect.add("total")
    elif total < 0:
        errors.append("Total is negative")
        suspect.add("total")

    if line_amounts and subtotal is not None and not _close(sum(line_amounts), subtotal, tolerance):
        errors.append(f"Line items sum to {sum(line_amounts):.2f} but subtotal is {subtotal:.2f}")
        suspect.update({"subtotal", "line_items"})

    if subtotal is not None and total is not None:
        expected = subtotal - (discount or 0) + (tax_total or 0)
        if not _close(expected, total, tolerance):
            errors.append(
                f"Subtotal − discount + tax = {expected:.2f}, but total is {total:.2f}"
            )
            suspect.update({"total", "tax_total", "subtotal"})
    elif line_amounts and total is not None and subtotal is None and tax_total is None:
        if not _close(sum(line_amounts) - (discount or 0), total, tolerance):
            suspect.update({"total", "line_items"})

    if invoice_date and due_date and due_date < invoice_date:
        errors.append("Due date is before invoice date")
        suspect.update({"due_date", "invoice_date"})

    if currency and currency not in ISO_CURRENCIES:
        errors.append(f"Unrecognised currency code {currency!r}")
        suspect.add("currency")

    return errors, suspect


def validate_extraction(extracted: ExtractedInvoice, threshold: float, tolerance: float) -> ValidationResult:
    errors, suspect = validate_values(
        invoice_date=extracted.invoice_date,
        due_date=extracted.due_date,
        currency=extracted.currency,
        line_amounts=[li.amount for li in extracted.line_items],
        subtotal=extracted.subtotal,
        tax_total=extracted.tax_total,
        discount=extracted.discount,
        total=extracted.total,
        tolerance=tolerance,
    )
    conf = extracted.confidence_map()
    present = {
        "invoice_number": extracted.invoice_number,
        "invoice_date": extracted.invoice_date,
        "vendor": extracted.vendor.name,
        "total": extracted.total,
    }
    low = set(suspect)
    for name in KEY_FIELDS:
        if present[name] is None or conf.get(name, 0.0) < threshold:
            low.add(name)
    for name, score in conf.items():
        if score < threshold:
            low.add(name)
    return ValidationResult(errors=errors, low_confidence=sorted(low))


def revalidate_record(record: InvoiceRecord, tolerance: float) -> list[str]:
    errors, _ = validate_values(
        invoice_date=record.invoice_date,
        due_date=record.due_date,
        currency=record.currency,
        line_amounts=[li.amount for li in record.line_items],
        subtotal=record.subtotal,
        tax_total=record.tax_total,
        discount=record.discount,
        total=record.total,
        tolerance=tolerance,
    )
    return errors
