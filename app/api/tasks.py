"""Cloud Tasks worker endpoints.

Return 2xx when done (or permanently failed and the user was told); raise for transient errors
so Cloud Tasks retries with backoff. On the last allowed attempt we notify the user instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header

from app.api.deps import get_container, verify_task_request
from app.container import Container
from app.core.logging import trace_id_var

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(verify_task_request)])

MAX_ATTEMPTS = 5  # keep in sync with the queue's retry config (infra/)


def _run(handler, payload: dict[str, Any], retry_count: int, trace_id: str | None) -> dict:  # type: ignore[no-untyped-def]
    if trace_id:
        trace_id_var.set(trace_id)
    try:
        handler(payload, retry_count)
    except Exception:
        if retry_count + 1 >= MAX_ATTEMPTS:
            handler({**payload, "_final_failure": True}, retry_count)
            return {"status": "failed"}
        raise
    return {"status": "done"}


@router.post("/process-document")
def process_document(
    payload: dict[str, Any] = Body(...),
    retry_count: int = Header(default=0, alias="X-CloudTasks-TaskRetryCount"),
    trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    container: Container = Depends(get_container),
) -> dict:
    return _run(container.workers.process_document, payload, retry_count, trace_id)


@router.post("/answer-query")
def answer_query(
    payload: dict[str, Any] = Body(...),
    retry_count: int = Header(default=0, alias="X-CloudTasks-TaskRetryCount"),
    trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    container: Container = Depends(get_container),
) -> dict:
    return _run(container.workers.answer_query, payload, retry_count, trace_id)


@router.post("/reconcile-index")
def reconcile_index(container: Container = Depends(get_container)) -> dict:
    """Re-index confirmed invoices whose vector upsert failed (run via Cloud Scheduler)."""
    return {"reindexed": container.indexer.reconcile()}
