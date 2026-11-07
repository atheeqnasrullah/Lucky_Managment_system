"""Deterministic, offline AI provider.

Uses regex heuristics so the whole pipeline can run locally and in CI without API keys.
It's intentionally simple: production uses OpenAIProvider.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date

from app.core.dates import find_dates, resolve_period
from app.ingestion.parsers.base import ParsedDocument
from app.models.domain import ChatTurn
from app.models.invoice import ExtractedInvoice, FieldConfidence, LineItem, Party, PaymentStatus
from app.search.planner import GroupBy, InvoicePatch, Metric, QueryIntent, QueryPlan

_AMOUNT = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_INV_NO = re.compile(
    r"(?:invoice|inv|bill|receipt)\s*(?:no\.?|number|num|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I
)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "AED": "AED", "Rs": "INR"}
_CURRENCY_CODE = re.compile(r"\b(USD|EUR|GBP|INR|AED|SAR|CAD|AUD|SGD|JPY)\b")
_LINE_ITEM = re.compile(rf"^\s*(.+?)\s+(\d+(?:\.\d+)?)\s*(?:x|@|\|)?\s*{_AMOUNT}\s*\|?\s*{_AMOUNT}\s*$", re.I)
_MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "last", "this", "today", "yesterday",
}


def _num(s: str) -> float:
    return float(s.replace(",", "").replace(" ", ""))


_TAX_ID_LINE = re.compile(r"\b(?:tax|vat|gst)\s*(?:id|no|number|reg\w*|in)\b", re.I)
_SUBTOTAL = re.compile(r"sub\s*-?\s*total", re.I)


def _labelled_amount(text: str, labels: str, *, subtotal: bool = False) -> float | None:
    """Last amount on the last line carrying one of `labels` (e.g. 'Tax (18%): 180.00' -> 180)."""
    label_re = re.compile(rf"\b(?:{labels})\b", re.I)
    value = None
    for line in text.splitlines():
        if _TAX_ID_LINE.search(line) or (not subtotal and _SUBTOTAL.search(line)):
            continue
        if m := label_re.search(line):
            amounts = re.findall(_AMOUNT, line[m.end():])
            if amounts:
                value = _num(amounts[-1])
    return value


class FakeAIProvider:
    dimension = 256

    # -- extraction ----------------------------------------------------------------------
    def extract_invoice(self, doc: ParsedDocument, caption: str | None = None) -> ExtractedInvoice:
        text = doc.text or ""
        conf: list[FieldConfidence] = []

        inv_no = None
        if m := _INV_NO.search(text):
            inv_no = m[1].strip()
            conf.append(FieldConfidence(field="invoice_number", confidence=0.95))

        invoice_date = due_date = None
        due_line = re.search(r"due[^\n]*", text, re.I)
        if due_line and (d := find_dates(due_line.group())):
            due_date = d[0]
        dates = [d for d in find_dates(text) if d != due_date]
        if dates:
            invoice_date = dates[0]
            conf.append(FieldConfidence(field="invoice_date", confidence=0.9))
        if due_date:
            conf.append(FieldConfidence(field="due_date", confidence=0.9))

        currency = None
        if m := _CURRENCY_CODE.search(text):
            currency = m[1]
        else:
            for sym, code in _CURRENCY_SYMBOLS.items():
                if sym in text:
                    currency = code
                    break
        if currency:
            conf.append(FieldConfidence(field="currency", confidence=0.9))

        subtotal = _labelled_amount(text, r"sub\s*-?\s*total", subtotal=True)
        tax = _labelled_amount(text, r"tax|vat|gst")
        discount = _labelled_amount(text, r"discount")
        total = _labelled_amount(text, r"grand\s+total|total\s+due|amount\s+due|balance\s+due|total")
        if total is not None:
            conf.append(FieldConfidence(field="total", confidence=0.9))
        if subtotal is not None:
            conf.append(FieldConfidence(field="subtotal", confidence=0.9))
        if tax is not None:
            conf.append(FieldConfidence(field="tax_total", confidence=0.9))

        vendor_name = None
        if m := re.search(r"^\s*(?:from|vendor|seller|supplier|billed by)\s*[:\-]\s*(.+)$", text, re.I | re.M):
            vendor_name = m[1].strip()
        else:
            for line in text.splitlines():
                clean = line.strip(" -*#\t")
                if clean and not re.search(r"invoice|page|bill|receipt|tax", clean, re.I) and len(clean) < 60:
                    vendor_name = clean
                    break
        if vendor_name:
            conf.append(FieldConfidence(field="vendor", confidence=0.85))

        customer_name = None
        if m := re.search(r"^\s*(?:bill(?:ed)? to|customer|to|client)\s*[:\-]\s*(.+)$", text, re.I | re.M):
            customer_name = m[1].strip()
            conf.append(FieldConfidence(field="customer", confidence=0.85))

        items: list[LineItem] = []
        for line in text.splitlines():
            if re.search(r"total|tax|vat|gst|discount|due", line, re.I):
                continue
            if m := _LINE_ITEM.match(line):
                items.append(
                    LineItem(description=m[1].strip(" |"), quantity=float(m[2]), unit_price=_num(m[3]), amount=_num(m[4]))
                )
        if items:
            conf.append(FieldConfidence(field="line_items", confidence=0.85))

        payment = PaymentStatus.unknown
        if re.search(r"\bpaid\b|payment received|balance\s*(?:due)?\s*[:\-]?\s*0(?:\.00)?\b", text, re.I):
            payment = PaymentStatus.paid
        elif re.search(r"amount due|balance due|due date|payable", text, re.I):
            payment = PaymentStatus.unpaid

        is_invoice = bool(text.strip()) and bool(
            re.search(r"invoice|bill|receipt|total|amount", text, re.I)
        )
        if not text.strip() and doc.images:
            is_invoice = True  # can't read images offline; let validation flag it

        return ExtractedInvoice(
            is_invoice=is_invoice,
            invoice_number=inv_no,
            invoice_date=invoice_date,
            due_date=due_date,
            currency=currency,
            vendor=Party(name=vendor_name),
            customer=Party(name=customer_name),
            line_items=items,
            subtotal=subtotal,
            tax_total=tax,
            discount=discount,
            total=total,
            payment_status=payment,
            field_confidence=conf,
        )

    # -- querying ------------------------------------------------------------------------
    def plan_query(self, question: str, today: date, history: list[ChatTurn]) -> QueryPlan:
        q = question.strip()
        lower = q.lower()
        date_from, date_to = resolve_period(lower, today)

        status = None
        if re.search(r"\bunpaid|outstanding|pending|overdue|due\b", lower):
            status = PaymentStatus.unpaid
        elif re.search(r"\bpaid\b", lower):
            status = PaymentStatus.paid

        vendor = None
        if m := re.search(r"\b(?:from|by|to|vendor|with|for)\s+([A-Za-z][\w&.'\- ]{1,40})", q):
            words = []
            for w in m[1].split():
                if w.lower().strip("?.!,") in _MONTH_WORDS | {"in", "on", "during", "the", "my", "me", "of"}:
                    break
                words.append(w.strip("?.!,"))
            candidate = " ".join(words).strip()
            if candidate and candidate[0].isupper():
                vendor = candidate

        inv_no = None
        if m := re.search(r"(?:invoice|inv|bill)\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", q, re.I):
            inv_no = m[1]

        if re.fullmatch(r"(hi|hello|hey|thanks|thank you|ok|okay)[!. ]*", lower):
            intent = QueryIntent.smalltalk
        elif inv_no:
            intent = QueryIntent.lookup
        elif re.search(r"how much|total|spend|spent|sum|how many|count|average|avg", lower):
            intent = QueryIntent.aggregate
        elif re.search(r"\b(list|show|which|unpaid|paid|outstanding|recent|latest|all)\b", lower):
            intent = QueryIntent.list
        else:
            intent = QueryIntent.search

        metric = Metric.sum
        if re.search(r"how many|count|number of", lower):
            metric = Metric.count
        elif re.search(r"average|avg", lower):
            metric = Metric.average

        group_by = GroupBy.none
        if re.search(r"by vendor|per vendor|each vendor|top vendors?", lower):
            group_by = GroupBy.vendor
        elif re.search(r"by month|per month|monthly|each month", lower):
            group_by = GroupBy.month

        return QueryPlan(
            intent=intent,
            vendor=vendor,
            date_from=date_from,
            date_to=date_to,
            payment_status=status,
            invoice_number=inv_no,
            metric=metric,
            group_by=group_by,
            semantic_query=q,
        )

    def answer(self, question: str, context: str) -> str:
        try:
            records = json.loads(context)
        except json.JSONDecodeError:
            records = []
        if not records:
            return "I couldn't find any matching invoices."
        lines = ["🔎 Here's what I found:"]
        for r in records:
            items = ", ".join(li["description"] for li in r.get("line_items", [])[:3])
            lines.append(
                f"• {r.get('invoice_number') or '(no #)'} · {r.get('vendor') or 'Unknown'} · "
                f"{r.get('invoice_date') or 'no date'} · *{r.get('currency') or ''} {r.get('total')}*"
                + (f"\n  {items}" if items else "")
            )
        return "\n".join(lines)

    def parse_correction(self, text: str) -> InvoicePatch:
        patch: dict[str, object] = {}
        aliases = {
            "total": "total", "amount": "total", "grand total": "total", "subtotal": "subtotal",
            "tax": "tax_total", "vat": "tax_total", "gst": "tax_total", "discount": "discount",
            "invoice number": "invoice_number", "number": "invoice_number", "invoice no": "invoice_number",
            "date": "invoice_date", "invoice date": "invoice_date", "due date": "due_date", "due": "due_date",
            "vendor": "vendor_name", "supplier": "vendor_name", "customer": "customer_name",
            "currency": "currency", "status": "payment_status",
        }
        names = "|".join(sorted((re.escape(a) for a in aliases), key=len, reverse=True))
        field_re = re.compile(rf"^\s*(?:the\s+)?({names})\s*(?:is|=|:|should be|to)?\s*(.+?)\s*$", re.I)
        for part in re.split(r",(?!\d{3})|[;\n]+", text):  # don't split "1,250.00"
            m = field_re.match(part)
            if not m:
                continue
            key = aliases.get(m[1].lower().strip())
            if not key:
                continue
            value = m[2].strip()
            if key in {"total", "subtotal", "tax_total", "discount"}:
                if nm := re.search(_AMOUNT, value):
                    patch[key] = _num(nm[1])
            elif key in {"invoice_date", "due_date"}:
                if d := find_dates(value):
                    patch[key] = d[0]
            elif key == "payment_status":
                low = value.lower()
                if low in {"paid", "unpaid", "partial", "unknown"}:
                    patch[key] = low
            elif key == "currency":
                patch[key] = value.upper()[:3]
            else:
                patch[key] = value
        return InvoicePatch.model_validate(patch)

    # -- embeddings ----------------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Hashed bag-of-words vectors: crude but lets semantic search work offline."""
        out = []
        for text in texts:
            vec = [0.0] * self.dimension
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(token.encode(), usedforsecurity=False).hexdigest(), 16)
                vec[h % self.dimension] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
