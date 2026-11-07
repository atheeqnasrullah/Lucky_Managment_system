from fastapi.testclient import TestClient

from app.container import build_container
from app.core.config import Settings
from app.main import create_app
from tests.conftest import ACME_INVOICE, make_pdf


def _client(**overrides) -> TestClient:
    settings = Settings(_env_file=None, **overrides)
    return TestClient(create_app(settings, build_container(settings, synchronous_tasks=True)))


def test_dev_key_seeded_in_local_memory_mode():
    with _client(environment="local", storage_backend="memory") as client:  # `with` runs startup
        headers = {"X-API-Key": "dev-key-123"}
        assert client.get("/v1/invoices", headers=headers).status_code == 200
        r = client.post("/v1/invoices", headers=headers, files={"file": ("a.pdf", make_pdf(ACME_INVOICE), "application/pdf")})
        assert r.status_code == 201


def test_dev_key_not_seeded_outside_local():
    with _client(environment="staging", storage_backend="memory") as client:
        assert client.get("/v1/invoices", headers={"X-API-Key": "dev-key-123"}).status_code == 401


def test_docs_expose_authorize_button():
    with _client(environment="local") as client:
        schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
        assert schemes["APIKeyHeader"] == {
            "type": "apiKey", "in": "header", "name": "X-API-Key", "description": "Integration API key"
        }
