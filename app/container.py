"""Builds and wires all services from Settings (simple dependency-injection container)."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import AIProvider
from app.conversation.handler import ConversationHandler
from app.core.config import Settings
from app.core.security import SlidingWindowRateLimiter
from app.ingestion.storage import DocumentStorage, GCSStorage, MemoryStorage
from app.repositories.base import Repository
from app.repositories.memory import MemoryRepository
from app.search.indexer import Indexer
from app.search.query_service import QueryService
from app.search.vector_store import MemoryVectorStore, PineconeVectorStore, VectorStore
from app.services.invoice_service import InvoiceService
from app.services.workers import Workers
from app.tasks.queue import (
    TASK_ANSWER_QUERY,
    TASK_PROCESS_DOCUMENT,
    CloudTasksQueue,
    InlineTaskQueue,
    TaskQueue,
)
from app.whatsapp.client import CloudWhatsAppClient, FakeWhatsAppClient, WhatsAppClient


@dataclass
class Container:
    settings: Settings
    repo: Repository
    storage: DocumentStorage
    vectors: VectorStore
    ai: AIProvider
    whatsapp: WhatsAppClient
    queue: TaskQueue
    indexer: Indexer
    invoices: InvoiceService
    queries: QueryService
    workers: Workers
    conversation: ConversationHandler


def _repo(s: Settings) -> Repository:
    if s.storage_backend == "gcp":
        from app.repositories.firestore import FirestoreRepository

        return FirestoreRepository(project=s.gcp_project)
    return MemoryRepository()


def _storage(s: Settings) -> DocumentStorage:
    return GCSStorage(s.gcs_bucket, s.gcp_project) if s.storage_backend == "gcp" else MemoryStorage()


def _vectors(s: Settings) -> VectorStore:
    if s.vector_backend == "pinecone":
        return PineconeVectorStore(s.pinecone_api_key.get_secret_value(), s.pinecone_index)
    return MemoryVectorStore()


def _ai(s: Settings) -> AIProvider:
    if s.ai_backend == "openai":
        from app.ai.openai_provider import OpenAIProvider

        return OpenAIProvider(s)
    from app.ai.fake_provider import FakeAIProvider

    return FakeAIProvider()


def _whatsapp(s: Settings) -> WhatsAppClient:
    if s.whatsapp_backend == "cloud":
        return CloudWhatsAppClient(
            token=s.whatsapp_token.get_secret_value(),
            phone_number_id=s.whatsapp_phone_number_id,
            api_version=s.whatsapp_api_version,
            api_base=s.whatsapp_api_base,
        )
    return FakeWhatsAppClient()


def build_container(settings: Settings, *, synchronous_tasks: bool = False, **overrides: object) -> Container:
    repo: Repository = overrides.get("repo") or _repo(settings)  # type: ignore[assignment]
    storage: DocumentStorage = overrides.get("storage") or _storage(settings)  # type: ignore[assignment]
    vectors: VectorStore = overrides.get("vectors") or _vectors(settings)  # type: ignore[assignment]
    ai: AIProvider = overrides.get("ai") or _ai(settings)  # type: ignore[assignment]
    whatsapp: WhatsAppClient = overrides.get("whatsapp") or _whatsapp(settings)  # type: ignore[assignment]

    if settings.task_backend == "cloudtasks":
        queue: TaskQueue = CloudTasksQueue(
            project=settings.gcp_project,
            location=settings.tasks_location,
            queue=settings.tasks_queue,
            base_url=settings.public_base_url,
            service_account=settings.tasks_service_account,
            audience=settings.tasks_audience or None,
        )
    else:
        queue = InlineTaskQueue(synchronous=synchronous_tasks)

    indexer = Indexer(ai, vectors, repo)
    invoices = InvoiceService(settings, repo, storage, ai, indexer)
    queries = QueryService(ai, repo, vectors)
    workers = Workers(settings, repo, whatsapp, invoices, queries)
    if isinstance(queue, InlineTaskQueue):
        queue.register(TASK_PROCESS_DOCUMENT, workers.process_document)
        queue.register(TASK_ANSWER_QUERY, workers.answer_query)

    conversation = ConversationHandler(
        settings, repo, whatsapp, queue, invoices, ai,
        SlidingWindowRateLimiter(settings.rate_limit_per_minute),
    )
    return Container(
        settings=settings, repo=repo, storage=storage, vectors=vectors, ai=ai, whatsapp=whatsapp,
        queue=queue, indexer=indexer, invoices=invoices, queries=queries, workers=workers,
        conversation=conversation,
    )
