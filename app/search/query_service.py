"""Answer natural-language billing questions.

Flow: LLM -> structured QueryPlan -> deterministic execution against Firestore / Pinecone ->
reply. Aggregates and lists are formatted deterministically (exact numbers, cheap); open-ended
"search" questions are answered by the LLM grounded strictly in retrieved records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from app.ai.base import AIProvider
from app.conversation.formatter import HELP_TEXT, invoice_line, money
from app.core.logging import get_logger
from app.models.domain import ChatTurn
from app.models.invoice import InvoiceRecord, InvoiceStatus
from app.repositories.base import InvoiceFilter, Repository
from app.search.analytics import aggregate, metric_value
from app.search.indexer import date_to_epoch
from app.search.planner import GroupBy, Metric, QueryIntent, QueryPlan
from app.search.vector_store import VectorStore

log = get_logger(__name__)

AGG_SCAN_LIMIT = 2000
_COUNTABLE = {InvoiceStatus.confirmed, InvoiceStatus.needs_review}


@dataclass
class QueryResult:
    text: str
    plan: QueryPlan
    invoices: list[InvoiceRecord] = field(default_factory=list)


def _period(plan: QueryPlan) -> str:
    if plan.date_from and plan.date_to:
        if plan.date_from == plan.date_to:
            return f" on {plan.date_from}"
        return f" from {plan.date_from} to {plan.date_to}"
    if plan.date_from:
        return f" since {plan.date_from}"
    if plan.date_to:
        return f" until {plan.date_to}"
    return ""


def _describe(plan: QueryPlan) -> str:
    bits = []
    if plan.payment_status:
        bits.append(plan.payment_status.value)
    bits.append("invoices")
    if plan.vendor:
        bits.append(f"from {plan.vendor}")
    return " ".join(bits) + _period(plan)


class QueryService:
    def __init__(self, ai: AIProvider, repo: Repository, store: VectorStore) -> None:
        self.ai = ai
        self.repo = repo
        self.store = store

    def _filter(self, plan: QueryPlan, limit: int) -> InvoiceFilter:
        return InvoiceFilter(
            vendor=plan.vendor,
            date_from=plan.date_from,
            date_to=plan.date_to,
            payment_status=plan.payment_status,
            invoice_number=plan.invoice_number,
            limit=limit,
        )

    def _countable(self, tenant_id: str, plan: QueryPlan, limit: int) -> list[InvoiceRecord]:
        return [i for i in self.repo.list_invoices(tenant_id, self._filter(plan, limit)) if i.status in _COUNTABLE]

    def ask(
        self, tenant_id: str, question: str, history: list[ChatTurn] | None = None, today: date | None = None
    ) -> QueryResult:
        plan = self.ai.plan_query(question, today or date.today(), history or [])
        log.info("query plan", extra={"extra_fields": {"intent": plan.intent.value}})
        handler = {
            QueryIntent.smalltalk: self._smalltalk,
            QueryIntent.aggregate: self._aggregate,
            QueryIntent.list: self._list,
            QueryIntent.lookup: self._lookup,
            QueryIntent.search: self._search,
        }[plan.intent]
        return handler(tenant_id, question, plan)

    # -- handlers --------------------------------------------------------------------------
    def _smalltalk(self, tenant_id: str, question: str, plan: QueryPlan) -> QueryResult:
        return QueryResult(text=HELP_TEXT, plan=plan)

    def _aggregate(self, tenant_id: str, question: str, plan: QueryPlan) -> QueryResult:
        invoices = self._countable(tenant_id, plan, AGG_SCAN_LIMIT)
        if not invoices:
            return QueryResult(text=f"I couldn't find any {_describe(plan)}.", plan=plan)
        rows = aggregate(invoices, plan.group_by)
        label = {Metric.sum: "Total", Metric.count: "Count", Metric.average: "Average"}[plan.metric]

        def fmt(row) -> str:  # type: ignore[no-untyped-def]
            value = metric_value(row, plan.metric)
            shown = f"{int(value)}" if plan.metric == Metric.count else money(value, row.currency or None)
            return shown

        lines = [f"📊 *{label}* for {_describe(plan)}:"]
        if plan.group_by == GroupBy.none:
            for row in rows:
                lines.append(f"*{fmt(row)}* ({row.count} invoice{'s' if row.count != 1 else ''})")
        else:
            for row in rows[:15]:
                lines.append(f"• {row.group}: *{fmt(row)}* ({row.count})")
            if len(rows) > 15:
                lines.append(f"…and {len(rows) - 15} more")
        pending = sum(1 for i in invoices if i.status == InvoiceStatus.needs_review)
        if pending:
            lines.append(f"\n_Includes {pending} invoice(s) still awaiting your review._")
        if len(invoices) >= AGG_SCAN_LIMIT:
            lines.append("_Result capped; narrow the date range for exact figures._")
        return QueryResult(text="\n".join(lines), plan=plan, invoices=invoices)

    def _list(self, tenant_id: str, question: str, plan: QueryPlan) -> QueryResult:
        invoices = self._countable(tenant_id, plan, plan.limit)
        if not invoices:
            return QueryResult(text=f"I couldn't find any {_describe(plan)}.", plan=plan)
        lines = [f"🗂️ *{_describe(plan).capitalize()}* (showing {len(invoices)}):"]
        lines += [invoice_line(i) for i in invoices]
        totals = aggregate(invoices, GroupBy.none)
        if totals:
            lines.append("")
            lines.append("Sum of listed: " + ", ".join(money(r.total, r.currency or None) for r in totals))
        return QueryResult(text="\n".join(lines), plan=plan, invoices=invoices)

    def _lookup(self, tenant_id: str, question: str, plan: QueryPlan) -> QueryResult:
        from app.conversation.formatter import invoice_summary

        invoices = self.repo.list_invoices(
            tenant_id, InvoiceFilter(invoice_number=plan.invoice_number, limit=5)
        )
        if not invoices:
            return QueryResult(text=f"I couldn't find invoice {plan.invoice_number}.", plan=plan)
        text = "\n\n".join(invoice_summary(i).replace("Invoice processed", "Invoice details") for i in invoices)
        return QueryResult(text=text, plan=plan, invoices=invoices)

    def _search(self, tenant_id: str, question: str, plan: QueryPlan) -> QueryResult:
        flt: dict = {}
        if plan.payment_status:
            flt["payment_status"] = {"$eq": plan.payment_status.value}
        date_range: dict = {}
        if (start := date_to_epoch(plan.date_from)) is not None:
            date_range["$gte"] = start
        if (end := date_to_epoch(plan.date_to)) is not None:
            date_range["$lte"] = end
        if date_range:
            flt["invoice_date"] = date_range

        vector = self.ai.embed([plan.semantic_query or question])[0]
        matches = self.store.query(tenant_id, vector, top_k=12, flt=flt or None)

        seen: list[str] = []
        for m in matches:
            inv_id = m.metadata.get("invoice_id")
            if inv_id and inv_id not in seen:
                seen.append(inv_id)
        invoices = [inv for i in seen[:6] if (inv := self.repo.get_invoice(tenant_id, i))]
        if plan.vendor:
            invoices = [i for i in invoices if (plan.vendor.lower() in (i.vendor.name or "").lower())] or invoices
        if not invoices:
            return QueryResult(text="I couldn't find anything matching that in your invoices.", plan=plan)

        context = json.dumps([self._context_record(i) for i in invoices], default=str, indent=None)
        return QueryResult(text=self.ai.answer(question, context), plan=plan, invoices=invoices)

    @staticmethod
    def _context_record(inv: InvoiceRecord) -> dict:
        return {
            "invoice_number": inv.invoice_number,
            "vendor": inv.vendor.name,
            "customer": inv.customer.name,
            "invoice_date": inv.invoice_date,
            "due_date": inv.due_date,
            "currency": inv.currency,
            "total": inv.total,
            "tax_total": inv.tax_total,
            "payment_status": inv.payment_status.value,
            "line_items": [li.model_dump() for li in inv.line_items[:30]],
            "summary": inv.summary,
        }
