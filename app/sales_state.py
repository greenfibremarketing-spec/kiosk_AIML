"""Validated sales snapshots, separate from chat messages and business authority.

Not yet wired into the existing agent. Consent/quote/lead labels are descriptive;
future business services must independently authorize actions and retain evidence.
Do not pass arbitrary model output to ``with_updates`` as a trusted state patch.
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class SalesStage(str, Enum):
    GREETING = 'GREETING'
    INTENT = 'INTENT'
    QUALIFICATION = 'QUALIFICATION'
    PRODUCT_DISCOVERY = 'PRODUCT_DISCOVERY'
    RECOMMENDATION = 'RECOMMENDATION'
    OBJECTION_HANDLING = 'OBJECTION_HANDLING'
    CONVERSION = 'CONVERSION'
    COMPLETED = 'COMPLETED'


class SalesState(BaseModel):
    """Immutable anonymous state; no raw contacts or conversation messages.

    Budget is the customer's INR budget per item/gift, not a verified product price.
    Delivery location should be a city/region rather than a personal street address.
    Language remains unknown until the caller has evidence of a preference.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', validate_default=True)

    session_id: Identifier = Field(default_factory=lambda: uuid4().hex)
    kiosk_id: Identifier | None = None
    channel: Identifier = 'kiosk'
    language: Literal['unknown', 'en', 'hi', 'hinglish'] = 'unknown'
    customer_intent: Literal['unknown', 'b2b', 'd2c', 'support'] = 'unknown'
    sales_stage: SalesStage = SalesStage.GREETING
    occasion: ShortText | None = None
    budget: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, decimal_places=2)
    quantity: int | None = Field(default=None, strict=True, gt=0)
    delivery_location: ShortText | None = None
    preferred_delivery_date: date | None = None
    product_preferences: tuple[ShortText, ...] = Field(default=(), max_length=50)
    interested_skus: tuple[Identifier, ...] = Field(default=(), max_length=50)
    selected_skus: tuple[Identifier, ...] = Field(default=(), max_length=50)
    customization_requirements: tuple[ShortText, ...] = Field(default=(), max_length=50)
    objections: tuple[ShortText, ...] = Field(default=(), max_length=50)
    lead_status: Literal['none', 'qualifying', 'qualified', 'submitted', 'declined'] = 'none'
    customer_contact_consent: Literal['not_asked', 'granted', 'declined', 'revoked'] = 'not_asked'
    quote_status: Literal['none', 'draft', 'requested', 'human_review'] = 'none'
    escalation_required: bool = Field(default=False, strict=True)
    conversation_outcome: Literal['ongoing', 'checkout_handoff', 'quote_requested',
                                  'lead_submitted', 'human_handoff', 'no_sale'] = 'ongoing'

    def with_updates(self, **changes: Any) -> 'SalesState':
        """Return a fully revalidated snapshot, allowing stage skips/backtracking.

        Identity is fixed for this snapshot lineage. Start a new state for another
        kiosk/channel/session. This helper is not an authorization boundary.
        """
        if {'session_id', 'kiosk_id', 'channel'} & changes.keys():
            raise ValueError('Session identity cannot be changed by a sales-state update')
        return type(self).model_validate({**self.model_dump(), **changes})
