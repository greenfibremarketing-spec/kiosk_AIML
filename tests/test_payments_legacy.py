"""Offline payment security checks: no live orders or real credentials."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from app import payments
from app.config import settings


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(settings, 'payment_provider', 'razorpay')
    monkeypatch.setattr(settings, 'kiosk_orders_api_url', None)
    monkeypatch.setattr(settings, 'razorpay_key_id', 'rzp_test_dummy')
    monkeypatch.setattr(settings, 'razorpay_key_secret', SecretStr('dummy-secret'))
    product = {'sku': 'GF:abc:red', 'price': '123.45', 'currency': 'INR',
               'isActive': True, 'source': 'greenfibre_api', 'price_status': 'fresh_api',
               'stock': 5, 'stock_checked_at': datetime.now(timezone.utc).isoformat(),
               'variants': [{'id': 'red'}], 'variant_id': 'red'}
    repo = Mock()
    repo.get.side_effect = lambda _: deepcopy(product)
    monkeypatch.setattr(payments, 'get_product_repository', lambda: repo)
    order = {'id': 'order_abc123', 'amount': 24690, 'currency': 'INR', 'status': 'created',
             'notes': {'integration': 'greeny_kiosk', 'kiosk_id': 'kiosk-1', 'session_id': 's1'}}
    payment = {'id': 'pay_abc123', 'order_id': order['id'], 'amount': 24690,
               'currency': 'INR', 'status': 'captured', 'captured': True}
    def provider(method, path, payload=None):
        if method == 'POST':
            return {**order, **payload}
        return deepcopy(payment if path.startswith('payments/') else order)
    provider_mock = Mock(side_effect=provider)
    monkeypatch.setattr(payments, 'provider_request', provider_mock)
    api = FastAPI()
    api.include_router(payments.create_payment_router(lambda request: request.headers.get('X-Kiosk-ID', 'kiosk-1')))
    with TestClient(api) as client:
        yield client, product, order, payment, provider_mock


def order_body():
    return {'session_id': 's1', 'items': [{'sku': 'GF:abc:red', 'quantity': 2}]}


def verify_body():
    return {'session_id': 's1', 'razorpay_order_id': 'order_abc123',
            'razorpay_payment_id': 'pay_abc123', 'razorpay_signature': hmac.new(
                b'dummy-secret', b'order_abc123|pay_abc123', hashlib.sha256).hexdigest()}


def test_create_order_uses_catalog_and_no_secret(checkout):
    client, _, _, _, provider = checkout
    response = client.post('/payments/orders', json=order_body())
    assert response.status_code == 201
    assert response.json()['amount'] == 24690
    assert response.json()['key_id'] == 'rzp_test_dummy'
    assert 'dummy-secret' not in response.text
    payload = provider.call_args.args[2]
    assert payload['partial_payment'] is False
    assert payload['notes']['kiosk_id'] == 'kiosk-1'
    assert 'GF:abc:red' in payload['notes']['cart_0']


@pytest.mark.parametrize('extra', [{'amount': 1}, {'currency': 'USD'}])
def test_client_cannot_override_total(checkout, extra):
    client, _, _, _, provider = checkout
    assert client.post('/payments/orders', json={**order_body(), **extra}).status_code == 422
    provider.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'stock': 1}, {'stock': None}, {'isActive': False}, {'price': '0'},
    {'price': 'NaN'}, {'price': '1.234'}, {'currency': 'USD'},
    {'source': 'development_json'}, {'price_status': 'catalogue_snapshot'},
    {'variant_id': None}, {'stock_checked_at': '2000-01-01T00:00:00+00:00'},
])
def test_reject_unverified_or_unavailable_cart(checkout, changes):
    client, product, _, _, provider = checkout
    product.update(changes)
    assert client.post('/payments/orders', json=order_body()).status_code == 409
    provider.assert_not_called()


def test_reject_duplicate_sku(checkout):
    client, _, _, _, provider = checkout
    body = order_body()
    body['items'] *= 2
    assert client.post('/payments/orders', json=body).status_code == 422
    provider.assert_not_called()


def test_missing_config(checkout, monkeypatch):
    monkeypatch.setattr(settings, 'razorpay_key_secret', None)
    assert checkout[0].post('/payments/orders', json=order_body()).status_code == 503


def test_signature_and_captured_payment(checkout):
    response = checkout[0].post('/payments/verify', json=verify_body())
    assert response.status_code == 200
    assert response.json()['paid'] is True


def test_bad_signature_rejected_before_fetching_payment(checkout):
    client, _, _, _, provider = checkout
    body = {**verify_body(), 'razorpay_signature': '0' * 64}
    assert client.post('/payments/verify', json=body).status_code == 400
    assert provider.call_count == 1


@pytest.mark.parametrize('changes', [{'amount': 1}, {'currency': 'USD'}, {'order_id': 'order_other'}])
def test_payment_mismatch(checkout, changes):
    client, _, _, payment, _ = checkout
    payment.update(changes)
    assert client.post('/payments/verify', json=verify_body()).status_code == 409


def test_authorization_is_not_payment(checkout):
    client, _, _, payment, _ = checkout
    payment.update(status='authorized', captured=False)
    assert client.post('/payments/verify', json=verify_body()).json()['paid'] is False


def test_order_ownership_and_polling(checkout):
    client, _, order, _, _ = checkout
    assert client.post('/payments/verify', json={**verify_body(), 'session_id': 'other'}).status_code == 404
    assert client.get('/payments/orders/order_abc123?session_id=s1', headers={'X-Kiosk-ID': 'other'}).status_code == 404
    assert client.get('/payments/orders/order_abc123?session_id=s1').json()['paid'] is False
    order.update(status='paid', amount_paid=24690, amount_due=0)
    assert client.get('/payments/orders/order_abc123?session_id=s1').json()['paid'] is True


def test_provider_errors_hide_sensitive_details(monkeypatch):
    monkeypatch.setattr(settings, 'razorpay_key_id', 'rzp_test_dummy')
    monkeypatch.setattr(settings, 'razorpay_key_secret', SecretStr('dummy-secret'))
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={'error': 'sensitive-detail'}))
    original_client = httpx.Client
    monkeypatch.setattr(payments.httpx, 'Client', lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(payments.HTTPException) as error:
        payments.provider_request('GET', 'orders/order_abc123')
    assert error.value.status_code == 502
    assert 'sensitive-detail' not in str(error.value.detail)


def test_provider_uses_basic_auth_and_integer_amount(monkeypatch):
    monkeypatch.setattr(settings, 'razorpay_key_id', 'rzp_test_dummy')
    monkeypatch.setattr(settings, 'razorpay_key_secret', SecretStr('dummy-secret'))
    def handle(request):
        import json
        assert str(request.url) == 'https://api.razorpay.com/v1/orders'
        assert request.headers['authorization'].startswith('Basic ')
        assert json.loads(request.content)['amount'] == 12345
        return httpx.Response(200, json={'id': 'order_test'})
    original_client = httpx.Client
    monkeypatch.setattr(payments.httpx, 'Client', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    assert payments.provider_request('POST', 'orders', {'amount': 12345})['id'] == 'order_test'


def test_provider_timeout_is_not_retried(monkeypatch):
    monkeypatch.setattr(settings, 'razorpay_key_id', 'rzp_test_dummy')
    monkeypatch.setattr(settings, 'razorpay_key_secret', SecretStr('dummy-secret'))
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('private provider details')
    original_client = httpx.Client
    monkeypatch.setattr(payments.httpx, 'Client', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(payments.HTTPException) as error:
        payments.provider_request('POST', 'orders', {'amount': 12345})
    assert error.value.status_code == 504
    assert len(calls) == 1
    assert 'private' not in error.value.detail


def test_real_api_routes_enforce_kiosk_auth(monkeypatch):
    from app import server
    monkeypatch.setattr(settings, 'kiosk_auth_secret', SecretStr('kiosk-secret'))
    monkeypatch.setattr(settings, 'razorpay_key_secret', None)
    with TestClient(server.api) as client:
        for prefix in ('', '/v1'):
            assert client.post(prefix + '/payments/orders', json=order_body()).status_code == 401
            assert client.post(prefix + '/payments/verify', json=verify_body()).status_code == 401
            assert client.get(prefix + '/payments/orders/order_abc123?session_id=s1').status_code == 401
