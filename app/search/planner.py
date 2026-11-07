"""Structured query plan produced from a natural-language billing question."""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field

from app.models.invoice import PaymentStatus


class QueryIntent(str, Enum):
    aggregate = "aggregate"
    list = "list"
    lookup = "lookup"
    search = "search"
    smalltalk = "smalltalk"


class Metric(str, Enum):
    sum = "sum"
    count = "count"  # type: ignore[assignment]  # shadows str.count; fine for an Enum member
    average = "average"


class GroupBy(str, Enum):
    vendor = "vendor"
    month = "month"
    none = "none"


class QueryPlan(BaseModel):
    intent: QueryIntent
    vendor: str | None = Field(None, description="Vendor name filter, as the user wrote it")
    date_from: date | None = None
    date_to: date | None = None
    payment_status: PaymentStatus | None = None
    invoice_number: str | None = None
    metric: Metric = Metric.sum
    group_by: GroupBy = GroupBy.none
    semantic_query: str | None = Field(
        None, description="Rephrased search text for semantic retrieval (intent=search)"
    )
    limit: int = Field(10, ge=1, le=50)


class InvoicePatch(BaseModel):
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    vendor_name: str | None = None
    customer_name: str | None = None
    subtotal: float | None = None
    tax_total: float | None = None
    discount: float | None = None
    total: float | None = None
    payment_status: PaymentStatus | None = None

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)
