"""FastAPI dependencies: container access, API-key auth, Cloud Tasks OIDC auth."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.container import Container
from app.core.security import hash_api_key


def get_container(request: Request) -> Container:
    return request.app.state.container


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Integration API key")


def require_tenant(
    x_api_key: str | None = Security(api_key_header),
    container: Container = Depends(get_container),
) -> str:
    """Resolve the tenant from an integration API key. The tenant is never taken from input."""
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-API-Key")
    key = container.repo.get_api_key(hash_api_key(x_api_key))
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return key.tenant_id


def verify_task_request(
    request: Request,
    authorization: str | None = Header(default=None),
    container: Container = Depends(get_container),
) -> None:
    """Accept only Cloud Tasks calls carrying a Google-signed OIDC token for our service account."""
    settings = container.settings
    if settings.task_backend != "cloudtasks":
        # Inline mode never calls these endpoints over HTTP; allow only in local/test.
        if settings.environment in ("local", "test"):
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Task endpoints disabled")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token, ga_requests.Request(), audience=settings.tasks_audience or settings.public_base_url
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    if claims.get("email") != settings.tasks_service_account or not claims.get("email_verified"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unexpected caller")
