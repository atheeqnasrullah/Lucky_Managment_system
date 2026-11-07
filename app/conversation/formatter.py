"""WhatsApp-friendly message formatting."""

from __future__ import annotations

from app.models.invoice import InvoiceRecord

FIELD_LABELS = {
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "due_date": "Due date",
    "vendor": "Vendor",
    "customer": "Customer",
    "total": "Total",
    "subtotal": "Subtotal",
    "tax_total": "Tax",
    "currency": "Currency",
    "line_items": "Line items",
}

HELP_TEXT = (
    "👋 *Billing Assistant*\n\n"
    "📎 *Send an invoice* as a PDF, Excel/CSV, photo or text and I'll extract the details.\n\n"
    "💬 *Ask me questions*, for example:\n"
    "• How much did I spend last month?\n"
    "• Show unpaid invoices\n"
    "• Total from ACME in 2025\n"
    "• Spend by vendor this year\n"
    "• Find invoice INV-1042\n\n"
    "✏️ *Fix a field*: reply e.g. _total is 1250.00, due date 2025-03-31_\n"
    "Type *help* anytime."
)


def money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    return f"{currency + ' ' if currency else ''}{amount:,.2f}"


def invoice_summary(inv: InvoiceRecord, *, max_items: int = 5) -> str:
    lines = [
        "🧾 *Invoice processed*",
        f"*Vendor:* {inv.vendor.name or '—'}",
        f"*Invoice #:* {inv.invoice_number or '—'}",
        f"*Date:* {inv.invoice_date.isoformat() if inv.invoice_date else '—'}",
    ]
    if inv.due_date:
        lines.append(f"*Due:* {inv.due_date.isoformat()}")
    if inv.customer.name:
        lines.append(f"*Bill to:* {inv.customer.name}")
    if inv.line_items:
        lines.append("")
        lines.append(f"*Items ({len(inv.line_items)}):*")
        for li in inv.line_items[:max_items]:
            calc = ""
            if li.quantity and li.unit_price is not None:
                calc = f"{li.quantity:g} × {li.unit_price:,.2f} = "
            elif li.quantity:
                calc = f"qty {li.quantity:g}, "
            lines.append(f"• {li.description[:40]} — {calc}{money(li.amount, inv.currency)}")
        if len(inv.line_items) > max_items:
            lines.append(f"• …and {len(inv.line_items) - max_items} more")
    lines.append("")
    if inv.subtotal is not None:
        lines.append(f"Subtotal: {money(inv.subtotal, inv.currency)}")
    if inv.discount:
        lines.append(f"Discount: −{money(inv.discount, inv.currency)}")
    if inv.tax_total is not None:
        lines.append(f"Tax: {money(inv.tax_total, inv.currency)}")
    lines.append(f"*Total: {money(inv.total, inv.currency)}*")
    lines.append(f"Status: {inv.payment_status.value}")
    return "\n".join(lines)


def review_request(inv: InvoiceRecord) -> str:
    fields = ", ".join(FIELD_LABELS.get(f, f) for f in inv.low_confidence_fields) or "some fields"
    body = invoice_summary(inv).replace("🧾 *Invoice processed*", "🧾 *Please check this invoice*")
    issues = ""
    if inv.validation_errors:
        issues = "\n\n⚠️ " + "\n⚠️ ".join(inv.validation_errors)
    return (
        f"{body}{issues}\n\nI'm not fully sure about: *{fields}*.\n"
        "Tap *Confirm* if it's correct, or reply with fixes, e.g. _total is 1250.00_."
    )


def invoice_line(inv: InvoiceRecord) -> str:
    d = inv.invoice_date.isoformat() if inv.invoice_date else "no date"
    return (
        f"• {inv.invoice_number or '(no #)'} · {inv.vendor.name or 'Unknown'} · {d} · "
        f"*{money(inv.total, inv.currency)}* · {inv.payment_status.value}"
    )
