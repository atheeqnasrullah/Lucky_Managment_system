"""Cloud Tasks worker endpoints: success, retry signalling, and final-failure notification."""

import uuid

from app.core.errors import ExtractionError
from app.models.domain import Job, JobStatus, JobType, Tenant
from tests.conftest import ACME_INVOICE, WA_ID


def _job(container) -> dict:
    container.repo.save_tenant(Tenant(id="t1", name="T"))
    job = Job(id=uuid.uuid4().hex, tenant_id="t1", type=JobType.process_document)
    container.repo.save_job(job)
    return {"job_id": job.id, "tenant_id": "t1", "wa_id": WA_ID, "text": ACME_INVOICE}


def test_process_document_endpoint(client, container, wa):
    payload = _job(container)
    r = client.post("/tasks/process-document", json=payload)
    assert r.status_code == 200 and r.json() == {"status": "done"}
    assert container.repo.get_job("t1", payload["job_id"]).status == JobStatus.succeeded
    # Redelivery is a no-op (idempotent)
    sent = len(wa.sent)
    client.post("/tasks/process-document", json=payload)
    assert len(wa.sent) == sent


def test_transient_failure_returns_5xx_then_notifies_on_last_attempt(client, container, wa, monkeypatch):
    def boom(*_a, **_k):
        raise ExtractionError("model unavailable")

    monkeypatch.setattr(container.ai, "extract_invoice", boom)
    payload = _job(container)

    non_raising = client.__class__(client.app, raise_server_exceptions=False)
    r = non_raising.post("/tasks/process-document", json=payload, headers={"X-CloudTasks-TaskRetryCount": "0"})
    assert r.status_code == 503  # Cloud Tasks will retry

    r = client.post("/tasks/process-document", json=payload, headers={"X-CloudTasks-TaskRetryCount": "4"})
    assert r.json() == {"status": "failed"}
    assert container.repo.get_job("t1", payload["job_id"]).status == JobStatus.failed
    assert "after several attempts" in wa.texts_to(WA_ID)[-1]


def test_reconcile_endpoint(client, container):
    assert client.post("/tasks/reconcile-index").json() == {"reindexed": 0}
