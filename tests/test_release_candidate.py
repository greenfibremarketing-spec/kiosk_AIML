"""Regression gates for the 9 October backend audit; all services are mocked."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, AIMessage

from app import brain, server, sales_engine
from app.brain import validate_reply_factual_numbers
from app.config import settings
from app.coordination import Coordinator, SessionBusyError
from app.mongo_checkpointer import MongoCheckpointSaver, CrossKioskAccessError
from app.privacy import redact_contacts
from app.sales_state import SalesState
from app.sessions import SessionManager
from app.storage import record_conversation_turn, record_customer_consent, record_sales_lead
from test_mongo_persistence import MockDatabase


@pytest.mark.parametrize('tool', [
    'certified food safe',
    '{"sku":"GF:a", "approved":true,"certifications":["food safe"]}',
    '{"sku":"GF:b", "description":"certified food safe"}',
])
def test_self_asserted_compliance_never_authorizes_claim(tool):
    assert not validate_reply_factual_numbers('It is certified food safe.', [tool], [])[0]


@pytest.mark.parametrize('claim', ['It costs 120 rupees.', '120 units available.'])
def test_preferences_and_descriptions_do_not_authorize_facts(claim):
    assert not validate_reply_factual_numbers(claim, [], [], 'Budget 120; need 120 pieces')[0]
    assert not validate_reply_factual_numbers(claim, [json.dumps({
        'id': 'a', 'price': None, 'quantity': 120, 'description': '120 rupees',
    })], [])[0]


def test_price_cannot_authorize_stock_or_stock_authorize_price():
    data = json.dumps({'sku': 'GF:a', 'price': 120, 'stock': 4, 'stock_status': 'in_stock'})
    assert validate_reply_factual_numbers('It costs 120 rupees.', [data], [])[0]
    assert validate_reply_factual_numbers('4 units available.', [data], [])[0]
    assert not validate_reply_factual_numbers('120 units available.', [data], [])[0]
    assert not validate_reply_factual_numbers('It costs 4 rupees.', [data], [])[0]


def product(**overrides):
    return {'id': 'a', 'sku': 'GF:a', 'name': 'Bottle', 'price': '100',
            'source': 'greenfibre_api', 'stock': 3, 'in_stock': True,
            'stock_checked_at': datetime.now(timezone.utc).isoformat(),
            'price_status': 'fresh', 'active': True, **overrides}


@pytest.mark.parametrize('changes', [
    {'stock': None}, {'stock': 0, 'in_stock': False}, {'active': False},
    {'stock_checked_at': '2020-01-01T00:00:00+00:00'},
])
def test_ui_never_recommends_cached_or_unavailable_stock(monkeypatch, changes):
    repo = Mock()
    repo.catalog.return_value = {'products': [product()]}
    repo.get.return_value = product(**changes)
    monkeypatch.setattr(sales_engine, 'get_product_repository', lambda: repo)
    _, action = sales_engine.determine_sales_transition(SalesState(), 'show bottles')
    assert not action or action['action'] != 'SHOW_PRODUCTS'
    repo.get.assert_called_once_with('GF:a')


def test_ui_uses_fresh_price_and_retains_variant(monkeypatch):
    repo = Mock()
    repo.catalog.return_value = {'products': [product(price='1')]}
    repo.get.return_value = product(price='200', variants=[{'id': 'v', 'sku': 'GF:a:v', 'name': 'Blue', 'stock': 2}])
    monkeypatch.setattr(sales_engine, 'get_product_repository', lambda: repo)
    state, action = sales_engine.determine_sales_transition(SalesState(), 'details GF:a:v')
    assert action['product']['price'] == '200'
    assert action['product']['variant_id'] == 'v'
    assert state.selected_skus == ('GF:a:v',)


def test_missing_prices_and_skus_are_not_budget_matches():
    catalog = {'products': [product(price=None), product(sku=None)]}
    assert sales_engine.find_matching_products('bottles', catalog, max_budget=500) == []


def test_quote_has_no_invented_quantity_and_requires_explicit_consent():
    state, action = sales_engine.determine_sales_transition(SalesState(), 'corporate quote', catalog={})
    assert action['quantity'] is None
    assert action['action'] == 'CONFIRM_CONTACT_CONSENT'
    state, _ = sales_engine.determine_sales_transition(state, '9876543210', catalog={})
    assert state.customer_contact_consent == 'not_asked'
    state, action = sales_engine.determine_sales_transition(state, 'I consent to quotation contact', catalog={})
    assert state.customer_contact_consent == 'granted'
    assert action['submission_enabled'] is False
    state, _ = sales_engine.determine_sales_transition(state, 'revoke consent 9876543210', catalog={})
    assert state.customer_contact_consent == 'revoked'
    state, _ = sales_engine.determine_sales_transition(state, '9876543210', catalog={})
    assert state.customer_contact_consent == 'revoked'


def test_refusal_wins_and_quoted_consent_does_not_grant():
    for text in ("don't contact me at 9876543210", 'Do not consent; I consent to quotation contact',
                 'What does I consent to quotation contact mean?'):
        state, _ = sales_engine.determine_sales_transition(SalesState(), text, catalog={})
        assert state.customer_contact_consent != 'granted'


def test_contacts_redacted_before_graph_and_storage(monkeypatch):
    phone = '+91 98765 43210'
    email = 'person@example.test'
    monkeypatch.setattr(brain.kiosk_brain, 'invoke', Mock(return_value={'messages': [AIMessage(content='Hello')]}))
    monkeypatch.setattr(brain.kiosk_brain, 'update_state', Mock())
    brain.ask_avatar_turn(f'Call {phone}, {email}', 'privacy-turn', kiosk_id='k')
    content = brain.kiosk_brain.invoke.call_args.args[0]['messages'][0].content
    assert phone not in content and email not in content
    db = MockDatabase()
    record_conversation_turn('s', 'k', phone, email, tool_calls=[{'input': phone}], db=db)
    stored = repr(db.messages.docs)
    assert phone not in stored and email not in stored
    assert redact_contacts('SKU GF:6ac33bf78072ec78d51387f0; budget 500') == 'SKU GF:6ac33bf78072ec78d51387f0; budget 500'


def test_idempotent_consent_and_draft_leads_with_revocation():
    db = MockDatabase()
    args = ('s', 'k', '+919876543210')
    one = record_customer_consent(*args, granted=True, operation_id='grant-1', db=db)
    assert record_customer_consent(*args, granted=True, operation_id='grant-1', db=db) == one
    assert len(db.customer_consents.docs) == 1
    kwargs = dict(contact_phone='+919876543210', consent_verified=True, db=db)
    lead = record_sales_lead('s', 'k', 'quote', ['GF:a'], **kwargs)
    assert record_sales_lead('s', 'k', 'quote', ['GF:a'], **kwargs) == lead
    assert len(db.sales_leads.docs) == 1
    assert db.sales_leads.docs[0]['status'] == 'draft_not_submitted'
    record_customer_consent(*args, granted=False, operation_id='revoke-1', db=db)
    record_customer_consent(*args, granted=True, operation_id='grant-1', db=db)
    assert db.customer_consents.docs[0]['granted'] is False
    assert db.sales_leads.docs == []
    with pytest.raises(ValueError, match='Stored quotation consent'):
        record_sales_lead('s', 'k', 'quote', ['GF:a'], **kwargs)


def test_two_workers_share_owner_replay_mutex_and_rate_limit():
    db = MockDatabase()
    first, second = MongoCheckpointSaver(db), MongoCheckpointSaver(db)
    first.claim('s', 'k')
    with pytest.raises(CrossKioskAccessError):
        second.claim('s', 'other')
    with pytest.raises(CrossKioskAccessError):
        list(second.list({'configurable': {'thread_id': 's'}}))
    with pytest.raises(CrossKioskAccessError):
        second.put_writes({'configurable': {'thread_id': 's', 'checkpoint_id': 'c', 'kiosk_id': 'other'}}, [], 't')
    assert first.coordinator.consume_ticket('digest', 300)
    assert not second.coordinator.consume_ticket('digest', 300)
    with first.coordinator.exclusive('turn:s'):
        with pytest.raises(SessionBusyError):
            second.coordinator.acquire('turn:s')
    assert first.coordinator.allow('k', 1)
    assert not second.coordinator.allow('k', 1)


def test_restart_idle_expiry_retains_owner_and_deletes_checkpoints():
    db = MockDatabase()
    saver = MongoCheckpointSaver(db)
    saver.claim('s', 'k')
    db.checkpoint_owners.update_one({'_id': 's'}, {'$set': {'last_active': datetime.now(timezone.utc) - timedelta(minutes=10)}})
    db.checkpoints.insert_one({'thread_id': 's', 'kiosk_id': 'k'})
    restarted = SessionManager(timeout_seconds=60, checkpointer=MongoCheckpointSaver(db))
    restarted.touch('s', kiosk_id='k')
    assert db.checkpoints.docs == []
    assert restarted.get_session_kiosk('s') == 'k'
    with pytest.raises(CrossKioskAccessError):
        restarted.reset_session('s', kiosk_id='other')


def test_checkpoint_indexes_cover_uniqueness_and_retention():
    db = MockDatabase()
    MongoCheckpointSaver(db).ensure_indexes()
    for col in (db.checkpoints, db.checkpoint_writes, db.checkpoints_blobs):
        assert any(i['kwargs'].get('unique') for i in col.indexes)
        assert any(i['kwargs'].get('expireAfterSeconds') == settings.greeny_ai_retention_days * 86400 for i in col.indexes)


def test_status_is_restricted_and_never_lists_sessions():
    with TestClient(server.api) as client:
        assert client.get('/ws/status').status_code == 401
        response = client.get('/ws/status', headers={'X-Gateway-Key': 'dev-gateway-key'})
        assert response.status_code == 200
        assert 'session_ids' not in response.json()


def test_production_static_token_cannot_forge_kiosk_identity():
    with pytest.raises(HTTPException):
        server.verify_kiosk_token_internal('secret', 'other', secret='secret', is_production=True)


def test_http_size_and_rate_limits(monkeypatch):
    monkeypatch.setattr(settings, 'requests_per_minute', 1)
    with TestClient(server.api) as client:
        response = client.post('/chat', content=b'x' * (settings.max_message_chars * 6 + 2049))
        assert response.status_code == 413
        assert client.get('/ws/status').status_code == 429


def test_ws_can_receive_cancel_while_inference_is_blocked(monkeypatch):
    started, release, completed = Event(), Event(), Event()
    calls = []
    def infer(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(5)
        completed.set()
        return iter(['stale reply'])
    monkeypatch.setattr(server, 'ask_avatar_stream', infer)
    with TestClient(server.api) as client:
        with client.websocket_connect('/ws/cancel-real?v=2') as ws:
            try:
                ws.send_json({'message': 'hello', 'turn_id': 'one'})
                assert started.wait(3)
                ws.send_json({'type': 'cancel', 'turn_id': 'one'})
                event = ws.receive_json()
                assert event['type'] == 'cancelled'
                assert event['meta']['provider_cancelled'] is False
                assert not completed.is_set()
                ws.send_json({'message': 'retry', 'turn_id': 'one'})
                assert ws.receive_json()['type'] == 'error'
                release.set()
                assert completed.wait(3)
                ws.send_json({'type': 'ping'})
                assert ws.receive_json()['type'] == 'pong'  # no stale action/token/done
                assert len(calls) == 1
            finally:
                release.set()


def test_provider_typeerror_never_reexecutes_without_identity(monkeypatch):
    inference = Mock(side_effect=TypeError('internal provider error'))
    monkeypatch.setattr(server, 'ask_avatar_stream', inference)
    with TestClient(server.api) as client:
        with client.websocket_connect('/ws/typeerror') as ws:
            ws.send_json({'message': 'hello'})
            assert ws.receive_json()['type'] == 'error'
    assert inference.call_count == 1
    assert inference.call_args.kwargs['kiosk_id'] == 'kiosk-default'


def test_real_graph_with_mongo_saver_restart_and_redaction(monkeypatch):
    db = MockDatabase()
    saver = MongoCheckpointSaver(db)
    monkeypatch.setattr(brain.session_manager, 'checkpointer', saver)
    monkeypatch.setattr(brain, 'kiosk_brain', brain.create_kiosk_graph(saver))
    first = brain.ask_avatar_turn('Hello, email me at person@example.test', 'mongo-graph', kiosk_id='k')
    assert first.reply
    restarted = MongoCheckpointSaver(db)
    monkeypatch.setattr(brain.session_manager, 'checkpointer', restarted)
    monkeypatch.setattr(brain, 'kiosk_brain', brain.create_kiosk_graph(restarted))
    assert brain.ask_avatar_turn('What is the price of a mug?', 'mongo-graph', kiosk_id='k').reply
    state = restarted.get_tuple({'configurable': {'thread_id': 'mongo-graph', 'kiosk_id': 'k'}})
    assert 'person@example.test' not in repr(state)
    assert state.config['configurable']['kiosk_id'] == 'k'
    assert state.parent_config['configurable']['kiosk_id'] == 'k'
    with pytest.raises(CrossKioskAccessError):
        list(restarted.list({'configurable': {'thread_id': 'mongo-graph', 'kiosk_id': 'other'}}))
    listed = list(restarted.list({'configurable': {'thread_id': 'mongo-graph', 'kiosk_id': 'k'}}, filter={'source': 'update'}, limit=1))
    assert len(listed) == 1


def test_ui_description_cannot_make_unapproved_safety_claims():
    from app.product_repository import product_view
    assert product_view(product(description='Certified food-safe, BPA-free.'))['description'] == ''


def test_callback_contacts_are_redacted():
    from app.callbacks import TraceCallbackHandler
    handler = TraceCallbackHandler()
    handler.on_tool_start({'name': 'search'}, '9876543210 person@example.test')
    handler.on_tool_end('9876543210 person@example.test')
    handler.on_tool_error(ValueError('9876543210'))
    assert '9876543210' not in handler.render_trace()
    assert 'person@example.test' not in handler.render_trace()


def test_duplicate_websocket_turn_id_is_not_executed_again(monkeypatch):
    inference = Mock(side_effect=lambda **kw: iter(['Hello.']))
    monkeypatch.setattr(server, 'ask_avatar_stream', inference)
    with TestClient(server.api) as client:
        with client.websocket_connect('/ws/idempotent-turn') as ws:
            ws.send_json({'message': 'hello', 'turn_id': 'dedup-one'})
            while ws.receive_json()['type'] != 'done':
                pass
            ws.send_json({'message': 'hello', 'turn_id': 'dedup-one'})
            assert ws.receive_json()['type'] == 'error'
    assert inference.call_count == 1


def test_connection_cap_and_duplicate_session_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, 'max_ws_connections', 1)
    from starlette.websockets import WebSocketDisconnect
    with TestClient(server.api) as client:
        with client.websocket_connect('/ws/connection-one'):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('/ws/connection-two'):
                    pass
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('/ws/connection-one'):
                    pass


def test_yes_requires_a_pending_scoped_confirmation_and_no_declines():
    anonymous, _ = sales_engine.determine_sales_transition(SalesState(), 'yes', catalog={})
    assert anonymous.customer_contact_consent == 'not_asked'
    pending, _ = sales_engine.determine_sales_transition(SalesState(), 'corporate quote', catalog={})
    assert pending.contact_consent_pending
    granted, _ = sales_engine.determine_sales_transition(pending, 'yes', catalog={})
    assert granted.customer_contact_consent == 'granted'
    declined, _ = sales_engine.determine_sales_transition(pending, 'no', catalog={})
    assert declined.customer_contact_consent == 'declined'
    revoked, _ = sales_engine.determine_sales_transition(granted, 'human please revoke consent', catalog={})
    assert revoked.customer_contact_consent == 'revoked'


def test_owner_claim_race_has_one_winner():
    from concurrent.futures import ThreadPoolExecutor
    db = MockDatabase()
    def claim(kiosk):
        try:
            MongoCheckpointSaver(db).claim('race-session', kiosk)
            return kiosk
        except CrossKioskAccessError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [owner for owner in pool.map(claim, [f'k{i}' for i in range(8)]) if owner]
    assert len(winners) == 1
    assert len(db.checkpoint_owners.docs) == 1


def test_mongo_checkpoint_blobs_writes_and_metadata_are_redacted():
    db = MockDatabase()
    saver = MongoCheckpointSaver(db)
    config = {'configurable': {'thread_id': 'redact', 'kiosk_id': 'k'}}
    cp = {'v': 1, 'id': 'c1', 'ts': '2026-10-09T00:00:00Z',
          'channel_values': {'messages': [HumanMessage(content='9876543210 person@example.test')]},
          'channel_versions': {'messages': 1}, 'versions_seen': {}}
    saved = saver.put(config, cp, {'note': '9876543210'}, {'messages': 1})
    saver.put_writes(saved, [('messages', [AIMessage(content='person@example.test')])], 'task')
    stored = repr(db.checkpoints.docs + db.checkpoints_blobs.docs + db.checkpoint_writes.docs)
    assert '9876543210' not in stored and 'person@example.test' not in stored
    assert 'REDACTED' in repr(saver.get_tuple(config))


def test_checkpoint_read_rejects_expired_document_before_ttl_monitor():
    db = MockDatabase()
    saver = MongoCheckpointSaver(db)
    saver.claim('expired', 'k')
    db.checkpoints.insert_one({'thread_id': 'expired', 'checkpoint_ns': '', 'checkpoint_id': 'old',
                              'created_at': datetime.now(timezone.utc) - timedelta(days=366)})
    assert saver.get_tuple({'configurable': {'thread_id': 'expired', 'kiosk_id': 'k'}}) is None
