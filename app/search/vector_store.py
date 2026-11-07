"""Vector store abstraction: Pinecone (serverless) in production, in-memory for dev/tests.

Each tenant gets its own Pinecone namespace, so queries can never cross tenants.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class VectorRecord:
    id: str
    values: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorMatch:
    id: str
    score: float
    metadata: dict[str, Any]


class VectorStore(Protocol):
    def upsert(self, namespace: str, records: list[VectorRecord]) -> None: ...
    def query(
        self, namespace: str, vector: list[float], top_k: int, flt: dict[str, Any] | None = None
    ) -> list[VectorMatch]: ...
    def delete_prefix(self, namespace: str, prefix: str) -> None: ...


def _match_filter(meta: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    """Subset of Pinecone's filter language: $eq $ne $gt $gte $lt $lte $in, implicit $eq."""
    if not flt:
        return True
    for key, cond in flt.items():
        value = meta.get(key)
        ops = cond if isinstance(cond, dict) else {"$eq": cond}
        for op, target in ops.items():
            if op == "$eq" and value != target:
                return False
            if op == "$ne" and value == target:
                return False
            if op == "$in" and value not in target:
                return False
            if op in ("$gt", "$gte", "$lt", "$lte"):
                if value is None:
                    return False
                if op == "$gt" and not value > target:
                    return False
                if op == "$gte" and not value >= target:
                    return False
                if op == "$lt" and not value < target:
                    return False
                if op == "$lte" and not value <= target:
                    return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class MemoryVectorStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, VectorRecord]] = {}
        self._lock = threading.Lock()

    def upsert(self, namespace: str, records: list[VectorRecord]) -> None:
        with self._lock:
            ns = self._data.setdefault(namespace, {})
            for r in records:
                ns[r.id] = r

    def query(
        self, namespace: str, vector: list[float], top_k: int, flt: dict[str, Any] | None = None
    ) -> list[VectorMatch]:
        with self._lock:
            candidates = list(self._data.get(namespace, {}).values())
        scored = [
            VectorMatch(id=r.id, score=_cosine(vector, r.values), metadata=r.metadata)
            for r in candidates
            if _match_filter(r.metadata, flt)
        ]
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:top_k]

    def delete_prefix(self, namespace: str, prefix: str) -> None:
        with self._lock:
            ns = self._data.get(namespace, {})
            for key in [k for k in ns if k.startswith(prefix)]:
                del ns[key]

    def count(self, namespace: str) -> int:
        return len(self._data.get(namespace, {}))


class PineconeVectorStore:
    def __init__(self, api_key: str, index_name: str) -> None:
        from pinecone import Pinecone

        self._index = Pinecone(api_key=api_key).Index(index_name)

    def upsert(self, namespace: str, records: list[VectorRecord]) -> None:
        batch = [{"id": r.id, "values": r.values, "metadata": r.metadata} for r in records]
        for i in range(0, len(batch), 100):
            self._index.upsert(vectors=batch[i : i + 100], namespace=namespace)

    def query(
        self, namespace: str, vector: list[float], top_k: int, flt: dict[str, Any] | None = None
    ) -> list[VectorMatch]:
        res = self._index.query(
            vector=vector, top_k=top_k, namespace=namespace, filter=flt or None, include_metadata=True
        )
        return [
            VectorMatch(id=m["id"], score=float(m["score"]), metadata=dict(m.get("metadata") or {}))
            for m in res.get("matches", [])
        ]

    def delete_prefix(self, namespace: str, prefix: str) -> None:
        # Serverless indexes support listing IDs by prefix (delete-by-filter is pod-only).
        for ids in self._index.list(prefix=prefix, namespace=namespace):
            if ids:
                self._index.delete(ids=list(ids), namespace=namespace)  # type: ignore[arg-type]
