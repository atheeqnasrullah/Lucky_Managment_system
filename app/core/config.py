"""Application configuration loaded from environment variables (and `.env` in local dev)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # General
    app_name: str = "whatsapp-billing-bot"
    environment: Literal["local", "dev", "staging", "prod", "test"] = "local"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8080"

    # Backends: "memory" runs everything in-process (local dev / tests), "gcp" uses real services.
    storage_backend: Literal["memory", "gcp"] = "memory"
    vector_backend: Literal["memory", "pinecone"] = "memory"
    task_backend: Literal["inline", "cloudtasks"] = "inline"
    ai_backend: Literal["fake", "openai"] = "fake"
    whatsapp_backend: Literal["fake", "cloud"] = "fake"

    # WhatsApp Cloud API
    whatsapp_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("local-verify-token")
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_api_base: str = "https://graph.facebook.com"

    # OpenAI
    openai_api_key: SecretStr = SecretStr("")
    openai_chat_model: str = "gpt-4o-mini"
    openai_extraction_model: str = "gpt-4o"
    openai_fallback_model: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536

    # Pinecone
    pinecone_api_key: SecretStr = SecretStr("")
    pinecone_index: str = "invoices"

    # Google Cloud
    gcp_project: str = ""
    gcs_bucket: str = ""
    tasks_queue: str = "invoice-jobs"
    tasks_location: str = "us-central1"
    tasks_service_account: str = ""
    tasks_audience: str = ""

    # Limits & behaviour
    max_upload_mb: int = 20
    confidence_threshold: float = 0.8
    amount_tolerance: float = 0.05
    rate_limit_per_minute: int = 20
    default_tenant_name: str = "Default"
    auto_provision_tenants: bool = Field(
        default=True,
        description="Create a tenant automatically for unknown WhatsApp senders.",
    )
    signed_url_ttl_minutes: int = 15
    dev_api_key: str = Field(
        default="dev-key-123",
        description="Local only: seeded at startup (with a demo tenant) when environment=local "
        "and storage_backend=memory, so /docs can be used without the CLI.",
    )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
