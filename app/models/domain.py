"""Tenants, users, jobs, and conversation sessions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.models.invoice import utcnow


class Tenant(BaseModel):
    id: str
    name: str
    plan: str = "free"
    currency: str = "USD"
    timezone: str = "UTC"
    created_at: datetime = Field(default_factory=utcnow)


class UserRole(str, Enum):
    owner = "owner"
    staff = "staff"


class User(BaseModel):
    wa_id: str
    tenant_id: str
    display_name: str | None = None
    role: UserRole = UserRole.owner
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)


class JobType(str, Enum):
    process_document = "process_document"
    answer_query = "answer_query"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Job(BaseModel):
    id: str
    tenant_id: str
    type: JobType
    status: JobStatus = JobStatus.queued
    attempts: int = 0
    error: str | None = None
    invoice_id: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    text: str


class Session(BaseModel):
    wa_id: str
    tenant_id: str
    pending_invoice_id: str | None = None
    last_intent: str | None = None
    history: list[ChatTurn] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)

    def remember(self, role: str, text: str, max_turns: int = 10) -> None:
        self.history.append(ChatTurn(role=role, text=text[:1000]))
        self.history = self.history[-max_turns:]


class ApiKey(BaseModel):
    key_hash: str
    tenant_id: str
    name: str = "default"
    created_at: datetime = Field(default_factory=utcnow)
