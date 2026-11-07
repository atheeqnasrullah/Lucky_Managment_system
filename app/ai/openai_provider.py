"""OpenAI provider built on LangChain (structured output, vision, embeddings, fallbacks)."""

from __future__ import annotations

import base64
from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import Settings
from app.core.errors import ExtractionError
from app.core.logging import get_logger
from app.extraction import prompts
from app.ingestion.parsers.base import ParsedDocument
from app.models.domain import ChatTurn
from app.models.invoice import ExtractedInvoice
from app.search.planner import InvoicePatch, QueryPlan

log = get_logger(__name__)


class OpenAIProvider:
    def __init__(self, settings: Settings) -> None:
        key = settings.openai_api_key.get_secret_value()
        common: dict[str, Any] = {"api_key": key, "temperature": 0, "max_retries": 3, "timeout": 90}

        primary = ChatOpenAI(model=settings.openai_extraction_model, **common)
        fallback = ChatOpenAI(model=settings.openai_fallback_model, **common)
        self._extractor = primary.with_structured_output(ExtractedInvoice).with_fallbacks(
            [fallback.with_structured_output(ExtractedInvoice)]
        )

        chat = ChatOpenAI(model=settings.openai_chat_model, **common)
        self._chat = chat
        self._planner = chat.with_structured_output(QueryPlan)
        self._corrector = chat.with_structured_output(InvoicePatch)
        self._embeddings = OpenAIEmbeddings(
            model=settings.openai_embed_model,
            openai_api_key=settings.openai_api_key,
            dimensions=settings.embedding_dimension,
        )

    # -- extraction ----------------------------------------------------------------------
    def extract_invoice(self, doc: ParsedDocument, caption: str | None = None) -> ExtractedInvoice:
        user_text = prompts.EXTRACTION_USER.format(
            caption=(caption or "").strip()[:500],
            text=doc.text or "(no text layer — see attached page images)",
        )
        content: list[str | dict[str, Any]] = [{"type": "text", "text": user_text}]
        for img in doc.images:
            b64 = base64.b64encode(img).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{doc.image_mime};base64,{b64}", "detail": "high"}}
            )
        messages = [SystemMessage(prompts.EXTRACTION_SYSTEM), HumanMessage(content=content)]
        try:
            result = self._extractor.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - surface any provider failure uniformly
            log.exception("extraction failed")
            raise ExtractionError(str(exc)) from exc
        if not isinstance(result, ExtractedInvoice):
            raise ExtractionError("Model returned no structured result")
        return result

    # -- querying ------------------------------------------------------------------------
    def plan_query(self, question: str, today: date, history: list[ChatTurn]) -> QueryPlan:
        messages: list[BaseMessage] = [SystemMessage(prompts.QUERY_PLANNER_SYSTEM.format(today=today.isoformat()))]
        for turn in history[-6:]:
            messages.append(HumanMessage(turn.text) if turn.role == "user" else AIMessage(turn.text))
        messages.append(HumanMessage(question))
        result = self._planner.invoke(messages)
        assert isinstance(result, QueryPlan)
        return result

    def answer(self, question: str, context: str) -> str:
        msg = self._chat.invoke(
            [
                SystemMessage(prompts.ANSWER_SYSTEM),
                HumanMessage(prompts.ANSWER_USER.format(question=question, context=context)),
            ]
        )
        return str(msg.content).strip()

    def parse_correction(self, text: str) -> InvoicePatch:
        result = self._corrector.invoke([SystemMessage(prompts.CORRECTION_SYSTEM), HumanMessage(text)])
        assert isinstance(result, InvoicePatch)
        return result

    # -- embeddings ----------------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embeddings.embed_documents(texts)
