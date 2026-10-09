"""Exercise actual REST/WS handlers and LangGraph, with repository-only mocks."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import socket
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
import uvicorn
from websockets.sync.client import connect

from app import brain, sales_engine, server
from app.config import settings
from app.greenfibre_repository import ProductAPIError


IDENTITIES = json.loads((Path(__file__).parents[1] / 'docs/GREENY_FRONTEND_PRODUCT_IDENTITIES.json').read_text())
LIVE_ID = IDENTITIES['products'][0]
SKU = LIVE_ID['sku']
VARIANT = LIVE_ID['variants'][0]['sku']


@pytest.fixture
def repository(monkeypatch):
    class Repository:
        failed = False
        price = '349'
        def catalog(self):
            if self.failed:
                raise ProductAPIError('private upstream detail')
            return {'products': [{**deepcopy(LIVE_ID), 'price': self.price, 'stock': 5,
                                  'stock_checked_at': datetime.now(timezone.utc).isoformat(),
                                  'in_stock': True, 'active': True, 'source': 'greenfibre_api',
                                  'price_status': 'fresh', 'variants': [
                                      {**v, 'stock': 2} for v in LIVE_ID['variants']]}]}
        def get(self, sku):
            row = self.catalog()['products'][0]
            if sku == row['sku']:
                return row
            variant = next((v for v in row['variants'] if v['sku'] == sku), None)
            return {**row, 'sku': sku, 'variant_id': variant['id'], 'stock': variant['stock']} if variant else None
    repo = Repository()
    monkeypatch.setattr(sales_engine, 'get_product_repository', lambda: repo)
    return repo


def post(client, sid, **payload):
    response = client.post('/chat', json={'session_id': sid, **payload})
    assert response.status_code == 200, response.text
    return response.json()


def start(client, sid):
    greeting = post(client, sid, message='hello')
    assert greeting['sales_stage'] == 'GREETING'
    ack = post(client, sid, action='SPEECH_DONE', turn_id='ack', speech_id=greeting['action']['speech_id'])
    assert ack['action']['action'] == 'ASK_NAME'
    return greeting


def test_rest_complete_guided_flow_and_all_required_stages(repository):
    with TestClient(server.api) as client:
        sid = 'rest-' + uuid.uuid4().hex
        greeting = start(client, sid)
        name = post(client, sid, action='SET_NAME', name='Noah', turn_id='name')
        assert name['sales_stage'] == 'ASK_CATEGORY'
        assert name['action']['categories'] == [{'id': 'bottle', 'name': 'Bottle'}]
        listing = post(client, sid, action='SELECT_CATEGORY', category_id='bottle', turn_id='category')
        assert listing['sales_stage'] == 'SHOW_PRODUCTS'
        assert listing['action']['products'][0]['sku'] == SKU
        detail = post(client, sid, action='SELECT_PRODUCT', sku=VARIANT, turn_id='select')
        assert detail['sales_stage'] == 'PRODUCT_DETAIL'
        assert detail['action']['product']['sku'] == VARIANT
        question = post(client, sid, message='What is the care advice?')
        assert question['sales_stage'] == 'PRODUCT_QUESTIONS'
        assert post(client, sid, action='NAVIGATE_BACK', turn_id='back-1')['sales_stage'] == 'SHOW_PRODUCTS'
        assert post(client, sid, action='NAVIGATE_BACK', turn_id='back-2')['sales_stage'] == 'ASK_CATEGORY'
        assert post(client, sid, action='SHOW_ALL', turn_id='all')['action']['count'] == 1
        bulk = post(client, sid, message='We need 200 gifts for employees')
        assert bulk['sales_stage'] == 'B2B_QUALIFICATION'
        quote = post(client, sid, message='prepare a quote')
        assert quote['sales_stage'] == 'QUOTE_REQUEST'
        assert quote['action']['action'] == 'CONFIRM_CONTACT_CONSENT'
        assert post(client, sid, message='I need a human')['sales_stage'] == 'HUMAN_HANDOFF'
        restarted = post(client, sid, action='RESTART', turn_id='restart')
        assert restarted['sales_stage'] == 'GREETING'
        assert restarted['action']['speech_id'] != greeting['action']['speech_id']
        state = brain.kiosk_brain.get_state({'configurable': {'thread_id': sid}}).values
        assert state['sales_state']['customer_name'] is None
        assert len(state['messages']) == 2


@pytest.mark.parametrize('payload', [
    {'action': 'ASK_NAME', 'turn_id': 'x'},
    {'action': 'SET_NAME', 'turn_id': 'x'},
    {'action': 'SET_NAME', 'name': '9876543210', 'turn_id': 'x'},
    {'action': 'SPEECH_DONE', 'turn_id': 'x'},
    {'action': 'SHOW_ALL'},
    {'action': 'SELECT_CATEGORY', 'category_id': ['bottle'], 'turn_id': 'x'},
    {'action': 'SELECT_PRODUCT', 'sku': 'GF:BTL:VIORA', 'name': 'extra', 'turn_id': 'x'},
    {'action': 'RESTART', 'message': 'hello', 'turn_id': 'x'},
])
def test_rest_rejects_invalid_command_shapes(payload):
    with TestClient(server.api) as client:
        assert client.post('/chat', json={'session_id': 'validation', **payload}).status_code == 422


def test_ack_is_bound_to_greeting_and_not_repeatable(repository):
    with TestClient(server.api) as client:
        sid = 'ack-' + uuid.uuid4().hex
        greeting = post(client, sid, message='hello')
        assert client.post('/chat', json={'session_id': sid, 'message': 'skip'}).status_code == 409
        bad = {'session_id': sid, 'action': 'SPEECH_DONE', 'speech_id': '0' * 32, 'turn_id': 'bad'}
        assert client.post('/chat', json=bad).status_code == 409
        good = {**bad, 'speech_id': greeting['action']['speech_id'], 'turn_id': 'good'}
        assert client.post('/chat', json=good).status_code == 200
        assert client.post('/chat', json=good).status_code == 409
        assert client.post('/chat', json={**good, 'turn_id': 'different'}).status_code == 409
        assert post(client, sid, action='SKIP_NAME', turn_id='skip')['sales_stage'] == 'ASK_CATEGORY'


@pytest.mark.parametrize('mode', ['text', 'voice', 'button'])
def test_input_modes_have_equivalent_transitions(repository, mode):
    with TestClient(server.api) as client:
        sid = mode + uuid.uuid4().hex
        start(client, sid)
        name_payload = {'action': 'SET_NAME', 'name': 'Noah', 'turn_id': 'name'} if mode == 'button' else {'message': 'Noah'}
        assert post(client, sid, input_mode=mode, **name_payload)['sales_stage'] == 'ASK_CATEGORY'
        selection = {'action': 'SELECT_CATEGORY', 'category_id': 'bottle', 'turn_id': 'select'} if mode == 'button' else {'message': 'show bottle'}
        result = post(client, sid, input_mode=mode, **selection)
        assert result['sales_stage'] == 'SHOW_PRODUCTS'
        assert [p['sku'] for p in result['action']['products']] == [SKU]


def test_product_failure_explicitly_invalidates_facts(repository):
    with TestClient(server.api) as client:
        sid = 'fresh-' + uuid.uuid4().hex
        start(client, sid)
        post(client, sid, action='SKIP_NAME', turn_id='skip')
        first = post(client, sid, action='SHOW_ALL', turn_id='first')
        assert first['action']['products'][0]['price'] == '349'
        repository.price = '399'
        assert post(client, sid, action='SHOW_ALL', turn_id='changed')['action']['products'][0]['price'] == '399'
        repository.failed = True
        failed = client.post('/chat', json={'session_id': sid, 'action': 'SHOW_ALL', 'turn_id': 'failed'})
        assert failed.status_code == 503
        assert failed.json()['detail']['invalidate_product_facts'] is True
        assert '349' not in failed.text and '399' not in failed.text and 'private upstream' not in failed.text


def ws_turn(ws, payload):
    ws.send(json.dumps(payload))
    for _ in range(150):
        event = json.loads(ws.recv(timeout=5))
        assert event['v'] == 2
        assert event['type'] != 'error', event
        if event['type'] == 'done':
            return event
    pytest.fail('Missing done envelope')


def test_actual_loopback_rest_and_websocket_v2(repository, monkeypatch):
    """Real HTTP and WebSocket connections, not mocked ASGI responses."""
    monkeypatch.setattr(settings, 'kiosk_auth_secret', SecretStr('local-contract-test-secret'))
    monkeypatch.setattr(settings, 'kiosk_gateway_key', SecretStr('local-contract-gateway'))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    instance = uvicorn.Server(uvicorn.Config(server.api, log_level='error', lifespan='off'))
    thread = threading.Thread(target=instance.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if instance.started:
                break
            time.sleep(.01)
        assert instance.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as http:
            ticket = http.post('/auth/kiosk/token', json={'kiosk_id': 'contract-kiosk'}, headers={'X-Gateway-Key': 'local-contract-gateway'}).json()['ticket']
            sid = 'network-' + uuid.uuid4().hex
            response = http.post('/chat', json={'message': 'hello', 'session_id': sid}, headers={'X-Kiosk-Token': ticket})
            assert response.status_code == 200
            speech_id = response.json()['action']['speech_id']
            with connect(f'ws://127.0.0.1:{port}/ws/{sid}?v=2&token={ticket}', proxy=None) as ws:
                assert ws_turn(ws, {'action': 'SPEECH_DONE', 'speech_id': speech_id, 'turn_id': 'ack'})['meta']['sales_stage'] == 'ASK_NAME'
                assert ws_turn(ws, {'action': 'SET_NAME', 'name': 'Noah', 'turn_id': 'name'})['meta']['sales_stage'] == 'ASK_CATEGORY'
                for mode in ('voice', 'text'):
                    assert ws_turn(ws, {'message': 'show bottle', 'input_mode': mode})['meta']['sales_stage'] == 'SHOW_PRODUCTS'
                assert ws_turn(ws, {'action': 'SELECT_CATEGORY', 'category_id': 'bottle', 'turn_id': 'cat'})['meta']['sales_stage'] == 'SHOW_PRODUCTS'
                assert ws_turn(ws, {'action': 'SELECT_PRODUCT', 'sku': VARIANT, 'turn_id': 'product'})['meta']['action']['product']['sku'] == VARIANT
                assert ws_turn(ws, {'message': 'What is the care advice?'})['meta']['sales_stage'] == 'PRODUCT_QUESTIONS'
                assert ws_turn(ws, {'action': 'NAVIGATE_BACK', 'turn_id': 'back'})['meta']['sales_stage'] == 'SHOW_PRODUCTS'
                assert ws_turn(ws, {'action': 'SHOW_ALL', 'turn_id': 'all'})['meta']['action']['count'] == 1
                assert ws_turn(ws, {'message': 'We need 200 gifts for employees'})['meta']['sales_stage'] == 'B2B_QUALIFICATION'
                assert ws_turn(ws, {'message': 'prepare a quote'})['meta']['sales_stage'] == 'QUOTE_REQUEST'
                assert ws_turn(ws, {'message': 'I need a human'})['meta']['sales_stage'] == 'HUMAN_HANDOFF'
                restarted = ws_turn(ws, {'action': 'RESTART', 'turn_id': 'restart'})
                ws_turn(ws, {'action': 'SPEECH_DONE', 'speech_id': restarted['meta']['action']['speech_id'], 'turn_id': 'ack2'})
                assert ws_turn(ws, {'action': 'SKIP_NAME', 'turn_id': 'skip'})['meta']['sales_stage'] == 'ASK_CATEGORY'
                ws.send(json.dumps({'action': 'SHOW_ALL', 'turn_id': 'all'}))
                assert json.loads(ws.recv(timeout=5))['type'] == 'error'
                repository.failed = True
                ws.send(json.dumps({'action':'SHOW_ALL','turn_id':'failed-read'}))
                failure = json.loads(ws.recv(timeout=5))
                assert failure['type'] == 'error'
                assert failure['meta']['invalidate_product_facts'] is True
                repository.failed = False
            next_ticket = http.post('/auth/kiosk/token', json={'kiosk_id': 'contract-kiosk'}, headers={'X-Gateway-Key': 'local-contract-gateway'}).json()['ticket']
            with connect(f'ws://127.0.0.1:{port}/ws/{sid}?v=2&token={next_ticket}', proxy=None) as ws:
                assert ws_turn(ws, {'action':'SHOW_ALL', 'turn_id':'reconnected'})['meta']['sales_stage'] == 'SHOW_PRODUCTS'
                ws.send(json.dumps({'action':'SPEECH_DONE', 'turn_id':'late-ack', 'speech_id':speech_id}))
                assert json.loads(ws.recv(timeout=5))['type'] == 'error'
    finally:
        instance.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_nonexistent_flow_routes_are_not_frontend_dependencies():
    with TestClient(server.api) as client:
        for route in ('/api/flow/step', '/api/flow/interpret'):
            assert client.post(route, json={}).status_code == 404


def test_unknown_categories_do_not_fall_back_to_unrelated_products(repository):
    with TestClient(server.api) as client:
        sid = 'invalid-category-' + uuid.uuid4().hex
        start(client, sid)
        post(client, sid, action='SKIP_NAME', turn_id='skip')
        response = client.post('/chat', json={'session_id':sid, 'action':'SELECT_CATEGORY',
                                             'category_id':'office_desk', 'turn_id':'invalid'})
        assert response.status_code == 409
        assert 'products' not in response.json()


def test_speech_ack_cannot_be_reused_across_sessions(repository):
    with TestClient(server.api) as client:
        first_id = 'one-' + uuid.uuid4().hex
        second_id = 'two-' + uuid.uuid4().hex
        first = post(client, first_id, message='hello')
        post(client, second_id, message='hello')
        response = client.post('/chat', json={'session_id':second_id, 'action':'SPEECH_DONE',
                                              'turn_id':'wrong-session', 'speech_id':first['action']['speech_id']})
        assert response.status_code == 409


@pytest.mark.parametrize('mode', ['text', 'voice', 'button'])
def test_product_selection_equivalence_including_b2b(repository, mode):
    with TestClient(server.api) as client:
        sid = 'product-mode-' + uuid.uuid4().hex
        start(client, sid)
        post(client, sid, action='SKIP_NAME', turn_id='skip')
        post(client, sid, message='We need 200 bottles for employees')
        payload = {'action':'SELECT_PRODUCT', 'sku':SKU, 'turn_id':'product'} if mode == 'button' else {'message': LIVE_ID['name']}
        response = post(client, sid, input_mode=mode, **payload)
        assert response['sales_stage'] == 'PRODUCT_DETAIL'
        assert response['action']['product']['sku'] == SKU
