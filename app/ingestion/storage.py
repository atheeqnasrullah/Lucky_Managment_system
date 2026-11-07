"""Document storage: Google Cloud Storage in production, in-memory for local dev/tests."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Protocol

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name).strip("._") or "document"
    return cleaned[:120]


def invoice_object_path(tenant_id: str, invoice_id: str, filename: str, year: int, month: int) -> str:
    return f"tenants/{tenant_id}/invoices/{year:04d}/{month:02d}/{invoice_id}/{safe_filename(filename)}"


class DocumentStorage(Protocol):
    def put(self, path: str, content: bytes, content_type: str) -> str: ...
    def get(self, uri: str) -> bytes: ...
    def signed_url(self, uri: str, minutes: int) -> str: ...


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, path: str, content: bytes, content_type: str) -> str:
        uri = f"memory://{path}"
        self.objects[uri] = (content, content_type)
        return uri

    def get(self, uri: str) -> bytes:
        return self.objects[uri][0]

    def signed_url(self, uri: str, minutes: int) -> str:
        return uri.replace("memory://", "https://storage.local/") + f"?ttl={minutes}m"


class GCSStorage:
    def __init__(self, bucket: str, project: str | None = None) -> None:
        from google.cloud import storage  # type: ignore[attr-defined]

        self._client = storage.Client(project=project or None)
        self._bucket = self._client.bucket(bucket)
        self._bucket_name = bucket

    def _blob_name(self, uri: str) -> str:
        prefix = f"gs://{self._bucket_name}/"
        if not uri.startswith(prefix):
            raise ValueError(f"URI {uri!r} is not in bucket {self._bucket_name}")
        return uri[len(prefix) :]

    def put(self, path: str, content: bytes, content_type: str) -> str:
        blob = self._bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)
        return f"gs://{self._bucket_name}/{path}"

    def get(self, uri: str) -> bytes:
        return self._bucket.blob(self._blob_name(uri)).download_as_bytes()

    def signed_url(self, uri: str, minutes: int) -> str:
        blob = self._bucket.blob(self._blob_name(uri))
        # On Cloud Run this signs via the IAM signBlob API using the service account identity.
        import google.auth
        from google.auth.transport import requests as ga_requests

        credentials, _ = google.auth.default()
        credentials.refresh(ga_requests.Request())
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=minutes),
            method="GET",
            service_account_email=getattr(credentials, "service_account_email", None),
            access_token=getattr(credentials, "token", None),
        )
