"""Easebuzz client contract and failure checks; no real payment operations."""
from copy import deepcopy
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from app import payments
from app.config import settings

ORDER_ID = 'a' * 24
HEADERS = {'X-Kiosk-ID': 'kiosk-1', 'Idempotency-Key': 'stable-checkout-1'}
BODY = {'session_id': 's1', 'items': [{'sku': 'GF:abc:red', 'quantity': 2}],
        'customer': {'name': 'Test Customer', 'email': 'test@example.com', 'phone': '9876543210'},
        'shipping_address': {'streetAddress': 'Test street', 'city': 'Mumbai',
                             'state': 'Maharashtra', 'pincode': '400001'}}


def proof(**changes):
    return {'saved_to_database': True, 'database_order_id': ORDER_ID,
            'payment_provider': 'easebuzz', 'merchant_txnid': 'K123abc',
            'payment_url': 'https://pay.easebuzz.in/pay/abc123', 'paid': False,
            'amount': 24690, 'currency': 'INR', 'session_id': 's1', 'kiosk_id': 'kiosk-1',
            'order_status': 'pending_payment', **changes}


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(settings, 'payment_provider', 'easebuzz')
    monkeypatch.setattr(settings, 'easebuzz_env', 'prod')
    monkeypatch.setattr(settings, 'kiosk_orders_api_url', 'https://orders.example/api/kiosk')
    monkeypatch.setattr(settings, 'kiosk_orders_api_key', SecretStr('x' * 64))
    api = FastAPI()
    api.include_router(payments.create_payment_router(lambda req: req.headers['X-Kiosk-ID']))
    return TestClient(api)


def service(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(payments.httpx, 'Client', lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw))


def test_checkout_forwards_same_key_and_pair_up_without_secrets(checkout, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == 'https://orders.example/api/kiosk/orders'
        assert request.headers['X-API-Key'] == 'x' * 64
        assert request.headers['Idempotency-Key'] == 'stable-checkout-1'
        assert request.headers['X-Kiosk-ID'] == 'kiosk-1'
        payload = json.loads(request.content)
        assert payload['payment_provider'] == 'easebuzz'
        assert payload['items'][0]['is_pair_up'] is True
        assert 'amount' not in payload and 'salt' not in payload
        return httpx.Response(201, json=proof(salt='private', customer=BODY['customer']))

    service(monkeypatch, handle)
    body = deepcopy(BODY)
    body['items'][0]['is_pair_up'] = True
    first = checkout.post('/payments/orders', json=body, headers=HEADERS)
    second = checkout.post('/payments/orders', json=body, headers=HEADERS)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.json()['paid'] is False
    assert 'salt' not in first.json() and 'customer' not in first.json()
    assert len(calls) == 2  # Upstream owns durable idempotency across workers.


@pytest.mark.parametrize('changes', [
    {'payment_provider': 'razorpay'}, {'payment_provider': None},
    {'amount': True}, {'amount': '123'}, {'currency': 'USD'}, {'paid': 'true'},
    {'saved_to_database': False}, {'database_order_id': 'fake'},
    {'kiosk_id': 'other'}, {'session_id': 'other'}, {'merchant_txnid': ''},
    {'payment_url': 'https://pay.easebuzz.in.evil.example/pay/abc'},
    {'payment_url': 'https://pay.easebuzz.in/pay/'},
    {'payment_url': 'https://testpay.easebuzz.in/pay/abc'},
    {'payment_url': 'https://pay.easebuzz.in/pay/abc?redirect=evil'},
    {'payment_url': 'https://user@pay.easebuzz.in/pay/abc'},
])
def test_invalid_checkout_proof_fails_closed(checkout, monkeypatch, changes):
    service(monkeypatch, lambda req: httpx.Response(200, json=proof(**changes)))
    assert checkout.post('/payments/orders', json=BODY, headers=HEADERS).status_code == 502


@pytest.mark.parametrize('status', [301, 401, 403, 404, 409, 422, 429, 500])
def test_error_bodies_never_expose_secrets(checkout, monkeypatch, status):
    service(monkeypatch, lambda req: httpx.Response(status, json={'error': 'private provider data'}))
    response = checkout.post('/payments/orders', json=BODY, headers=HEADERS)
    assert response.status_code >= 400
    assert 'private provider data' not in response.text


def test_timeout_never_retries_or_falls_back_to_razorpay(checkout, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('private')

    service(monkeypatch, handle)
    monkeypatch.setattr(payments, 'provider_request', lambda *a: pytest.fail('Unexpected Razorpay fallback'))
    response = checkout.post('/payments/orders', json=BODY, headers=HEADERS)
    assert response.status_code == 504 and len(calls) == 1
    assert 'same Idempotency-Key' in response.text and 'private' not in response.text


@pytest.mark.parametrize('endpoint,body', [
    ('/payments/verify', {'database_order_id': ORDER_ID, 'session_id': 's1'}),
    (f'/payments/orders/{ORDER_ID}/reconcile', {'session_id': 's1'}),
    (f'/payments/orders/{ORDER_ID}/reconcile?session_id=s1', None),
])
def test_reconciliation_uses_backend_and_stays_pending(checkout, monkeypatch, endpoint, body):
    def handle(request):
        assert request.method == 'POST'
        assert request.url.path == f'/api/kiosk/orders/{ORDER_ID}/reconcile'
        assert json.loads(request.content) == {'session_id': 's1'}
        return httpx.Response(200, json=proof())

    service(monkeypatch, handle)
    response = checkout.post(endpoint, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['paid'] is False
    assert 'payment_url' not in response.json()


def test_status_accepts_paid_only_from_saved_backend(checkout, monkeypatch):
    def handle(request):
        assert request.method == 'GET' and request.url.params['session_id'] == 's1'
        return httpx.Response(200, json=proof(paid=True, order_status='confirmed'))

    service(monkeypatch, handle)
    response = checkout.get(f'/payments/orders/{ORDER_ID}?session_id=s1', headers=HEADERS)
    assert response.status_code == 200 and response.json()['paid'] is True


def test_wrong_saved_order_rejected(checkout, monkeypatch):
    service(monkeypatch, lambda req: httpx.Response(200, json=proof(database_order_id='b' * 24)))
    assert checkout.get(f'/payments/orders/{ORDER_ID}?session_id=s1', headers=HEADERS).status_code == 502


def test_browser_cannot_assert_payment(checkout):
    assert checkout.post('/payments/verify', headers=HEADERS, json={
        'database_order_id': ORDER_ID, 'session_id': 's1', 'paid': True}).status_code == 422


def test_required_key_idempotency_and_customer(checkout, monkeypatch):
    assert checkout.post('/payments/orders', headers={'X-Kiosk-ID': 'kiosk-1'}, json=BODY).status_code == 422
    body = deepcopy(BODY)
    body.pop('customer')
    assert checkout.post('/payments/orders', headers=HEADERS, json=body).status_code == 422
    monkeypatch.setattr(settings, 'kiosk_orders_api_key', None)
    assert checkout.post('/payments/orders', headers=HEADERS, json=BODY).status_code == 503


def test_fallback_url_keeps_existing_base_setting(checkout, monkeypatch):
    monkeypatch.setattr(settings, 'kiosk_orders_api_url', None)
    monkeypatch.setattr(settings, 'greenfibre_api_base_url', 'https://orders.example/api')
    assert payments.order_api_url() == 'https://orders.example/api/kiosk'


def test_production_rejects_forged_token(monkeypatch):
    from app import server
    from app.coordination import local_coordinator
    monkeypatch.setattr(server, 'get_coordinator', lambda: local_coordinator)
    monkeypatch.setattr(settings, 'environment', 'production')
    monkeypatch.setattr(settings, 'kiosk_auth_secret', SecretStr('real-secret'))
    client = TestClient(server.api)
    for prefix in ('', '/v1'):
        response = client.post(prefix + '/payments/orders', json=BODY,
            headers={**HEADERS, 'X-Kiosk-Token': 'forged'})
        assert response.status_code == 401


def test_test_environment_requires_test_host(checkout, monkeypatch):
    monkeypatch.setattr(settings, 'easebuzz_env', 'test')
    service(monkeypatch, lambda req: httpx.Response(201, json=proof(
        payment_url='https://testpay.easebuzz.in/pay/abc123')))
    assert checkout.post('/payments/orders', json=BODY, headers=HEADERS).status_code == 201


def test_preflight_reports_configuration_without_network(checkout, monkeypatch, capsys):
    from scripts.check_payment_readiness import main
    import sys
    monkeypatch.setattr(sys, 'argv', ['check_payment_readiness.py'])
    monkeypatch.setattr(settings, 'environment', 'production')
    monkeypatch.setattr(settings, 'kiosk_auth_secret', SecretStr('auth-secret'))
    monkeypatch.setattr(settings, 'kiosk_gateway_key', SecretStr('gateway-secret'))
    monkeypatch.setattr(settings, 'allowed_kiosk_ids', 'kiosk-1')
    monkeypatch.setattr(settings, 'checkpointer_backend', 'mongodb')
    monkeypatch.setattr(settings, 'greeny_ai_mongo_url', SecretStr('mongodb://not-contacted'))
    service(monkeypatch, lambda req: pytest.fail('Default preflight must not contact network'))
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report['configuration_passed'] is True
    assert report['order_service_authentication'] == 'not_checked'
    assert report['merchant_credentials_and_live_payment'] == 'not_verified'
    assert 'auth-secret' not in json.dumps(report)


def test_invalid_environment_rejected():
    from app.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, easebuzz_env='production')
