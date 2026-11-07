"""Invoice domain models: the LLM extraction schema and the persisted record."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentStatus(str, Enum):
    unpaid = "unpaid"
    paid = "paid"
    partial = "partial"
    unknown = "unknown"


class InvoiceStatus(str, Enum):
    processing = "processing"
    needs_review = "needs_review"
    confirmed = "confirmed"
    failed = "failed"


class Party(BaseModel):
    name: str | None = Field(None, description="Legal or trading name")
    tax_id: str | None = Field(None, description="VAT / GST / tax registration number")
    address: str | None = None
    email: str | None = None
    phone: str | None = None


class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float = Field(..., description="Line total before tax")
    tax_rate: float | None = Field(None, description="Tax rate as a percentage, e.g. 18 for 18%")


class FieldConfidence(BaseModel):
    field: str = Field(..., description="Field name, e.g. 'total' or 'invoice_date'")
    confidence: float = Field(..., ge=0, le=1)


class ExtractedInvoice(BaseModel):
    """Schema the LLM must fill. Unknown values must be null, never guessed."""

    is_invoice: bool = Field(
        True, description="False if the document is clearly not an invoice, bill or receipt"
    )
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = Field(None, description="ISO 4217 code, e.g. USD, INR, EUR")
    vendor: Party = Field(default_factory=Party, description="Seller / issuer")
    customer: Party = Field(default_factory=Party, description="Buyer / bill-to")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax_total: float | None = None
    discount: float | None = None
    total: float | None = Field(None, description="Grand total payable")
    payment_status: PaymentStatus = PaymentStatus.unknown
    field_confidence: list[FieldConfidence] = Field(
        default_factory=list,
        description="Confidence 0..1 for each key field you extracted",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v

    def confidence_map(self) -> dict[str, float]:
        return {fc.field: fc.confidence for fc in self.field_confidence}


class SourceInfo(BaseModel):
    gcs_uri: str
    mime_type: str
    filename: str
    sha256: str
    wa_message_id: str | None = None


class InvoiceRecord(BaseModel):
    """Persisted invoice (Firestore: tenants/{tenant_id}/invoices/{id})."""

    id: str
    tenant_id: str
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    vendor: Party = Field(default_factory=Party)
    customer: Party = Field(default_factory=Party)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax_total: float | None = None
    discount: float | None = None
    total: float | None = None
    payment_status: PaymentStatus = PaymentStatus.unknown
    status: InvoiceStatus = InvoiceStatus.processing
    confidence: dict[str, float] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(default_factory=list)
    source: SourceInfo | None = None
    summary: str | None = None
    raw_text: str | None = Field(None, description="Parsed document text, used for re-indexing")
    indexed: bool = False
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def from_extraction(
        cls, *, id: str, tenant_id: str, extracted: ExtractedInvoice, **kwargs: Any
    ) -> InvoiceRecord:
        data = extracted.model_dump(exclude={"is_invoice", "field_confidence"})
        return cls(id=id, tenant_id=tenant_id, confidence=extracted.confidence_map(), **data, **kwargs)

    @property
    def vendor_name(self) -> str:
        return self.vendor.name or "Unknown vendor"

    def to_firestore(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        # Keep datetimes native so Firestore stores timestamps; store a normalised vendor key.
        data["created_at"] = self.created_at
        data["updated_at"] = self.updated_at
        data["vendor_key"] = normalise_key(self.vendor.name)
        return data

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> InvoiceRecord:
        data = {k: v for k, v in data.items() if k != "vendor_key"}
        return cls.model_validate(data)


def normalise_key(value: str | None) -> str:
    return " ".join((value or "").lower().split())
