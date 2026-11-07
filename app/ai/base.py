"""AI provider interface used by extraction, search, and conversation."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from app.ingestion.parsers.base import ParsedDocument
from app.models.domain import ChatTurn
from app.models.invoice import ExtractedInvoice
from app.search.planner import InvoicePatch, QueryPlan


class AIProvider(Protocol):
    def extract_invoice(self, doc: ParsedDocument, caption: str | None = None) -> ExtractedInvoice: ...

    def plan_query(self, question: str, today: date, history: list[ChatTurn]) -> QueryPlan: ...

    def answer(self, question: str, context: str) -> str: ...

    def parse_correction(self, text: str) -> InvoicePatch: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
