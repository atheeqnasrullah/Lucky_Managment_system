"""Exact aggregations over invoice records. All arithmetic happens here, never in the LLM."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.models.invoice import InvoiceRecord
from app.search.planner import GroupBy, Metric


@dataclass
class AggregateRow:
    group: str
    currency: str
    count: int
    total: float

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


def aggregate(invoices: list[InvoiceRecord], group_by: GroupBy) -> list[AggregateRow]:
    """Group by (group, currency): amounts in different currencies are never summed together."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for inv in invoices:
        if inv.total is None:
            continue
        if group_by == GroupBy.vendor:
            key = inv.vendor.name or "Unknown vendor"
        elif group_by == GroupBy.month:
            key = inv.invoice_date.strftime("%Y-%m") if inv.invoice_date else "No date"
        else:
            key = "All"
        buckets[(key, inv.currency or "")].append(inv.total)
    rows = [
        AggregateRow(group=g, currency=c, count=len(v), total=round(sum(v), 2))
        for (g, c), v in buckets.items()
    ]
    if group_by == GroupBy.month:
        rows.sort(key=lambda r: r.group)
    else:
        rows.sort(key=lambda r: r.total, reverse=True)
    return rows


def metric_value(row: AggregateRow, metric: Metric) -> float:
    if metric == Metric.count:
        return float(row.count)
    if metric == Metric.average:
        return round(row.average, 2)
    return row.total
