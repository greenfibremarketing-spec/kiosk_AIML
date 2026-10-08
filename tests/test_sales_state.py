from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.sales_state import SalesStage, SalesState


def test_anonymous_defaults_do_not_infer_consent_or_language():
    state = SalesState()
    assert UUID(state.session_id).version == 4
    assert state.session_id != SalesState().session_id
    assert state.language == 'unknown'
    assert state.customer_contact_consent == 'not_asked'
    assert state.lead_status == state.quote_status == 'none'
    assert state.sales_stage == SalesStage.GREETING
    assert state.budget is None and state.quantity is None


def test_multiple_requirements_and_json_roundtrip():
    state = SalesState().with_updates(
        customer_intent='b2b', language='hinglish', occasion='employee gifting',
        budget='1000.50', quantity=100, delivery_location='Pune',
        preferred_delivery_date='2026-12-15', customization_requirements=['logo'],
        selected_skus=['SKU-1'], sales_stage='PRODUCT_DISCOVERY',
    )
    assert state.budget == Decimal('1000.50')
    assert state.preferred_delivery_date == date(2026, 12, 15)
    assert SalesState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize('stage', list(SalesStage))
def test_customer_can_return_to_any_stage(stage):
    state = SalesState(sales_stage=SalesStage.COMPLETED)
    revised = state.with_updates(sales_stage=stage)
    assert revised.sales_stage == stage
    assert revised.customer_contact_consent == 'not_asked'
    assert revised.lead_status == 'none'
    assert state.sales_stage == SalesStage.COMPLETED


@pytest.mark.parametrize('patch', [
    {'budget': '-1'}, {'budget': 'NaN'}, {'budget': 'Infinity'}, {'budget': '0.001'},
    {'quantity': 0}, {'quantity': -1}, {'quantity': True}, {'quantity': 2.5},
    {'quantity': '20'}, {'customer_contact_consent': True},
    {'sales_stage': 'PAYMENT_TAKEN'}, {'language': 'invalid'},
    {'occasion': '   '}, {'selected_skus': ['']},
    {'preferred_delivery_date': '2026-02-30'}, {'escalation_required': 'false'},
    {'email': 'person@example.test'}, {'messages': ['hello']},
])
def test_invalid_updates_fail_without_changing_original(patch):
    state = SalesState(budget=Decimal('500'), quantity=10)
    before = state.model_dump_json()
    with pytest.raises(ValidationError):
        state.with_updates(**patch)
    assert state.model_dump_json() == before


@pytest.mark.parametrize('identity', ['session_id', 'kiosk_id', 'channel'])
def test_updates_cannot_move_state_between_sessions(identity):
    with pytest.raises(ValueError, match='identity'):
        SalesState().with_updates(**{identity: 'another'})


def test_no_shared_or_mutable_preference_lists():
    original = ['mug']
    first = SalesState(product_preferences=original)
    original.append('bottle')
    revised = first.with_updates(product_preferences=['bowl'])
    assert first.product_preferences == ('mug',)
    assert revised.product_preferences == ('bowl',)
    assert SalesState().product_preferences == ()
    with pytest.raises(ValidationError):
        first.quantity = 2


@pytest.mark.parametrize('consent', ['declined', 'revoked'])
def test_customer_refusal_is_preserved_when_preferences_change(consent):
    state = SalesState(customer_contact_consent=consent)
    changed = state.with_updates(budget='200', sales_stage='RECOMMENDATION')
    assert changed.customer_contact_consent == consent
    assert changed.lead_status == 'none'


def test_free_budget_is_distinct_from_unknown_budget():
    assert SalesState(budget=0).budget == Decimal('0')
    assert SalesState().budget is None
