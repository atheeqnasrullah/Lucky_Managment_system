"""Async job dispatch: Google Cloud Tasks in production, a thread pool (or synchronous) locally."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from app.core.logging import get_logger, trace_id_var

log = get_logger(__name__)

TASK_PROCESS_DOCUMENT = "process-document"
TASK_ANSWER_QUERY = "answer-query"

TaskHandler = Callable[[dict[str, Any], int], None]


class TaskQueue(Protocol):
    def enqueue(self, task: str, payload: dict[str, Any]) -> None: ...


class InlineTaskQueue:
    """Runs handlers in-process. `synchronous=True` makes tests deterministic."""

    def __init__(self, synchronous: bool = False, max_workers: int = 4) -> None:
        self.synchronous = synchronous
        self._handlers: dict[str, TaskHandler] = {}
        self._pool = None if synchronous else ThreadPoolExecutor(max_workers=max_workers)

    def register(self, task: str, handler: TaskHandler) -> None:
        self._handlers[task] = handler

    def _run(self, task: str, payload: dict[str, Any], trace_id: str) -> None:
        trace_id_var.set(trace_id)
        handler = self._handlers[task]
        for attempt in range(3):
            try:
                handler(payload, attempt)
                return
            except Exception:  # noqa: BLE001
                log.exception("inline task failed (attempt %s)", attempt + 1)
                if attempt == 2:
                    # Final attempt: tell the handler so it can notify the user.
                    try:
                        handler({**payload, "_final_failure": True}, attempt + 1)
                    except Exception:  # noqa: BLE001
                        log.exception("final-failure handler raised")

    def enqueue(self, task: str, payload: dict[str, Any]) -> None:
        trace_id = trace_id_var.get()
        if self._pool is None:
            self._run(task, payload, trace_id)
        else:
            self._pool.submit(self._run, task, payload, trace_id)


class CloudTasksQueue:
    def __init__(
        self,
        project: str,
        location: str,
        queue: str,
        base_url: str,
        service_account: str,
        audience: str | None = None,
    ) -> None:
        from google.cloud import tasks_v2

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksClient()
        self._parent = self._client.queue_path(project, location, queue)
        self._base_url = base_url.rstrip("/")
        self._service_account = service_account
        self._audience = audience or self._base_url

    def enqueue(self, task: str, payload: dict[str, Any]) -> None:
        tasks_v2 = self._tasks_v2
        request = tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{self._base_url}/tasks/{task}",
            headers={"Content-Type": "application/json", "X-Trace-Id": trace_id_var.get()},
            body=json.dumps(payload).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=self._service_account, audience=self._audience
            ),
        )
        self._client.create_task(parent=self._parent, task=tasks_v2.Task(http_request=request))
