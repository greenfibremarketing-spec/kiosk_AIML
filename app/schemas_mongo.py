"""MongoDB document schemas for Greeny AI operational database (greeny_ai).

Defines strongly-typed Pydantic models for sessions, conversational messages,
customer consents, sales leads, quote drafts, unanswered questions, and approved knowledge.

Customer contact details are stored ONLY when explicit consent is verified.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mask_phone_number(val: str) -> str:
    """Mask sensitive phone digits, preserving prefix and last 4 digits."""
    if not val:
        return val
    cleaned = val.strip()
    if len(cleaned) >= 10:
        return cleaned[:4] + "****" + cleaned[-4:]
    return "****"


class KioskSessionModel(BaseModel):
    """Tracks session metadata and kiosk origin."""
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    kiosk_id: str = Field(default="kiosk-default", min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)
    turn_count: int = Field(default=0, ge=0)
    sales_stage: str = Field(default="GREETING")
    customer_name: Optional[str] = Field(default=None, max_length=100)
    language_preference: str = Field(default="en", max_length=10)


class MessageModel(BaseModel):
    """Stores conversation turns for analytical improvement."""
    model_config = ConfigDict(extra="ignore")

    message_id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str = Field(min_length=1, max_length=128)
    kiosk_id: str = Field(default="kiosk-default", max_length=64)
    role: Literal["human", "ai", "system", "tool"]
    content: str = Field(max_length=10000)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    created_at: datetime = Field(default_factory=utcnow)


class CustomerConsentModel(BaseModel):
    """Explicit customer consent records. Required before capturing contact info."""
    model_config = ConfigDict(extra="ignore")

    consent_id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str = Field(min_length=1, max_length=128)
    kiosk_id: str = Field(default="kiosk-default", max_length=64)
    consent_type: Literal["whatsapp_promotional", "quote_callback", "human_assistance"]
    granted: bool = Field(...)
    phone_masked: Optional[str] = Field(default=None, max_length=30)
    terms_version: str = Field(default="v1.0", max_length=20)
    timestamp: datetime = Field(default_factory=utcnow)

    @field_validator("phone_masked")
    @classmethod
    def validate_masked_format(cls, val: Optional[str]) -> Optional[str]:
        if val and not any(ch in val for ch in ("*", "x", "X")):
            return mask_phone_number(val)
        return val


class QuoteItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sku: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=200)
    quantity: int = Field(gt=0)
    unit_price_estimate: Optional[Decimal] = None


class QuoteDraftModel(BaseModel):
    """Corporate B2B enquiry and quote draft."""
    model_config = ConfigDict(extra="ignore")

    quote_id: str = Field(default_factory=lambda: f"QD-{uuid4().hex[:8].upper()}")
    session_id: str = Field(min_length=1, max_length=128)
    kiosk_id: str = Field(default="kiosk-default", max_length=64)
    items: List[QuoteItem] = Field(default_factory=list)
    occasion: Optional[str] = Field(default=None, max_length=200)
    customization_notes: Optional[str] = Field(default=None, max_length=500)
    total_estimate: Optional[Decimal] = None
    status: Literal["draft", "requested", "human_review"] = "draft"
    created_at: datetime = Field(default_factory=utcnow)


class SalesLeadModel(BaseModel):
    """Stores qualified sales leads. Requires verified consent for contact info."""
    model_config = ConfigDict(extra="ignore")

    lead_id: str = Field(default_factory=lambda: f"LD-{uuid4().hex[:8].upper()}")
    session_id: str = Field(min_length=1, max_length=128)
    kiosk_id: str = Field(default="kiosk-default", max_length=64)
    lead_type: Literal["b2b", "d2c"] = "b2b"
    intent_summary: str = Field(max_length=1000)
    product_skus: List[str] = Field(default_factory=list)
    quantity: Optional[int] = Field(default=None, gt=0)
    budget_inr: Optional[Decimal] = Field(default=None, ge=0)
    occasion: Optional[str] = Field(default=None, max_length=200)
    consent_verified: bool = Field(default=False)
    contact_phone: Optional[str] = None
    status: Literal["qualifying", "submitted", "declined"] = "qualifying"
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("contact_phone")
    @classmethod
    def check_consent_before_phone(cls, phone: Optional[str], info) -> Optional[str]:
        if phone:
            consent = info.data.get("consent_verified", False)
            if not consent:
                raise ValueError("Cannot store customer phone number without verified consent.")
        return phone


class UnansweredQuestionModel(BaseModel):
    """Logs questions where RAG or tools were unable to provide verified answers."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str = Field(min_length=1, max_length=128)
    kiosk_id: str = Field(default="kiosk-default", max_length=64)
    user_query: str = Field(max_length=1000)
    fallback_reason: str = Field(max_length=200)
    timestamp: datetime = Field(default_factory=utcnow)


class ApprovedKnowledgeModel(BaseModel):
    """Registry of verified knowledge documents with certification approvals."""
    model_config = ConfigDict(extra="ignore")

    doc_id: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=200)
    category: str = Field(max_length=100)
    source_file: str = Field(max_length=200)
    content_sha256: str = Field(min_length=64, max_length=64)
    approved_by: str = Field(max_length=100)
    approved_at: datetime = Field(default_factory=utcnow)
    expiry_date: Optional[datetime] = None
    sku_scope: List[str] = Field(default_factory=list)
