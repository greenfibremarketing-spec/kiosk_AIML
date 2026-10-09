from copy import deepcopy
import json

import httpx
import pytest

from app.greenfibre_repository import GreenFibreProductRepository, ProductAPIError
from app.product_repository import product_view
from app import tools


PRODUCT = {
    '_id': 'abc123', 'name': 'Bottle', 'slug': 'bottle', 'isActive': True,
    'discountedPrice': 123.45, 'originalPrice': 150,
    'stockQuantity': 7, 'totalStock': 7,
    'category': {'name': 'Drinkware', 'slug': 'drinkware'},
    'colors': [{'_id': 'green', 'name': 'Green', 'stock': 2,
                'images': [{'card': 'https://images.example.test/green.jpg'}]},
               {'_id': 'orange', 'name': 'Orange', 'stock': 5}],
    'shortDescription': 'A birthday gift bottle', 'tags': ['birthday'],
}


@pytest.fixture
def api():
    state = {'product': deepcopy(PRODUCT), 'requests': [], 'now': 0.0}
    def handle(request):
        state['requests'].append(request)
        if request.url.path == '/api/product':
            return httpx.Response(200, json={'success': True, 'products': [state['product']],
                'pagination': {'page': 1, 'pages': 1, 'total': 1, 'limit': 50}})
        if request.url.path == '/api/product/abc123':
            return httpx.Response(200, json={'success': True, 'product': state['product']})
        return httpx.Response(404)
    repository = GreenFibreProductRepository('https://api.example.test/api',
        transport=httpx.MockTransport(handle), clock=lambda: state['now'])
    return repository, state


def test_correct_product_and_variant_sku(api):
    repo, _ = api
    item = repo.get('GF:abc123:orange')
    assert item['id'] == 'abc123'
    assert item['sku'] == 'GF:abc123:orange'
    assert item['variant_id'] == 'orange'
    assert item['stock'] == 5
    assert item['price'] == '123.45'
    assert item['product_url'] == 'https://greenfibre.org/shop/bottle'
    assert repo.get('GF:abc123:missing') is None
    assert repo.get('GF:abc123:') is None


@pytest.mark.parametrize('identifier', ['Bottle', 'bottle', 'GF:abc123'])
def test_lookup_preserves_website_id(api, identifier):
    assert api[0].get(identifier)['id'] == 'abc123'


def test_catalogue_never_confirms_cached_inventory(api):
    repo, state = api
    first = repo.products()[0]
    assert first['stock'] is None
    assert all(v['stock'] is None for v in first['variants'])
    state['product']['colors'][0]['stock'] = 0
    assert repo.products()[0]['stock'] is None
    assert product_view(repo.get('GF:abc123:green'))['stock_status'] == 'out_of_stock'
    assert len(state['requests']) == 2


def test_price_changes_verified_on_every_detail(api):
    repo, state = api
    assert repo.get('GF:abc123')['price'] == '123.45'
    state['product']['discountedPrice'] = 88.90
    assert repo.get('GF:abc123')['price'] == '88.9'
    assert len(state['requests']) == 2


def test_cache_expiry_and_mutation_isolation(api):
    repo, state = api
    repo.products()[0]['name'] = 'Mutated'
    assert repo.products()[0]['name'] == 'Bottle'
    state['now'] = 31
    state['product']['name'] = 'Updated'
    assert repo.products()[0]['name'] == 'Updated'
    assert len(state['requests']) == 2


def test_missing_price_and_stock_are_unknown(api):
    repo, state = api
    for key in ('discountedPrice', 'stockQuantity', 'totalStock', 'colors'):
        state['product'].pop(key)
    item = product_view(repo.get('GF:abc123'))
    assert item['price'] is None
    assert item['stock_status'] == 'unknown'


def test_missing_variant_stock_never_uses_parent_quantity(api):
    repo, state = api
    state['product']['colors'][0].pop('stock')
    assert repo.get('GF:abc123:green')['stock'] is None
    assert repo.get('GF:abc123')['stock'] is None


@pytest.mark.parametrize('active', [False, None])
def test_inactive_and_missing_active_excluded(api, active):
    repo, state = api
    if active is None:
        state['product'].pop('isActive')
    else:
        state['product']['isActive'] = active
    assert repo.products() == []


@pytest.mark.parametrize('code', [401, 403, 429, 500, 302])
def test_http_failures_are_sanitized(code):
    repo = GreenFibreProductRepository('https://api.example.test/api', token='TOP_SECRET',
        transport=httpx.MockTransport(lambda r: httpx.Response(code, text='PRIVATE_RESPONSE')))
    with pytest.raises(ProductAPIError) as exc:
        repo.get('GF:abc123')
    assert 'TOP_SECRET' not in str(exc.value) and 'PRIVATE_RESPONSE' not in str(exc.value)


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadTimeout])
def test_network_and_timeout_failures(error):
    def broken(request):
        raise error('PRIVATE_ERROR', request=request)
    repo = GreenFibreProductRepository('https://api.example.test/api', transport=httpx.MockTransport(broken))
    with pytest.raises(ProductAPIError, match='connection failed'):
        repo.products()


def test_timeout_and_authorization_sent_only_server_side():
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer secret'
        assert request.extensions['timeout']['read'] == 2
        return httpx.Response(404)
    repo = GreenFibreProductRepository('https://api.example.test/api', token='secret', timeout=2,
        transport=httpx.MockTransport(handler))
    assert repo.get('GF:abc123') is None


def test_rate_limit_cooldown_does_not_retry_immediately():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '10'})
    repo = GreenFibreProductRepository('https://api.example.test/api',
        transport=httpx.MockTransport(handler), clock=lambda: 0)
    for _ in range(2):
        with pytest.raises(ProductAPIError):
            repo.products()
    assert len(requests) == 1


def test_pagination_reads_all_pages():
    calls = []
    def handler(request):
        page = int(request.url.params['page'])
        calls.append(page)
        return httpx.Response(200, json={'success': True,
            'products': [{**PRODUCT, '_id': f'p{page}'}],
            'pagination': {'page': page, 'pages': 2, 'total': 2, 'limit': 1}})
    repo = GreenFibreProductRepository('https://api.example.test/api', transport=httpx.MockTransport(handler))
    assert [p['id'] for p in repo.products()] == ['p1', 'p2']
    assert calls == [1, 2]


def test_invalid_response_fails_closed():
    repo = GreenFibreProductRepository('https://api.example.test/api',
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'success': True, 'product': {}})))
    with pytest.raises(ProductAPIError):
        repo.get('GF:abc123')


def test_proxy_cached_detail_cannot_confirm_stock():
    repo = GreenFibreProductRepository('https://api.example.test/api',
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={'Age': '30'},
            json={'success': True, 'product': PRODUCT})))
    with pytest.raises(ProductAPIError, match='stale'):
        repo.get('GF:abc123')


def test_search_and_handoff_verify_current_stock(api, monkeypatch):
    repo, state = api
    monkeypatch.setattr(tools, 'get_product_repository', lambda: repo)
    results = json.loads(tools.search_products.invoke({'query': 'bottle', 'occasion': 'birthday', 'max_price': 150}))
    assert results[0]['source'] == 'greenfibre_api'
    assert results[0]['stock'] == 7
    state['product']['colors'][0]['stock'] = 0
    handoff = json.loads(tools.generate_checkout_handoff.invoke({'product_id': 'GF:abc123:green'}))
    assert handoff['product']['stock_status'] == 'out_of_stock'
    assert handoff['product']['variant_id'] == 'green'
    assert handoff['order_created'] is False
    assert all(r.method == 'GET' for r in state['requests'])


def test_corporate_draft_does_not_submit_or_authorize_b2b(api, monkeypatch):
    repo, state = api
    monkeypatch.setattr(tools, 'get_product_repository', lambda: repo)
    draft = json.loads(tools.prepare_corporate_enquiry.invoke({'product_id': 'GF:abc123:orange', 'quantity': 20}))
    assert draft['status'] == 'draft_not_submitted'
    assert draft['product']['variant_id'] == 'orange'
    assert all(r.method == 'GET' for r in state['requests'])


def test_gift_sets_are_website_products_not_invented_bundles(api, monkeypatch):
    repo, state = api
    state['product']['category'] = {'name': 'Gift Boxes & Hampers', 'slug': 'gift-boxes'}
    monkeypatch.setattr(tools, 'get_product_repository', lambda: repo)
    results = json.loads(tools.get_gift_bundles.invoke({'budget': 200}))
    assert results[0]['id'] == 'abc123'


def test_tool_failure_is_not_a_json_fallback(monkeypatch):
    class Broken:
        def products(self):
            raise ProductAPIError('Unavailable')
    monkeypatch.setattr(tools, 'get_product_repository', lambda: Broken())
    assert 'temporarily unavailable' in tools.search_products.invoke({})


def test_variant_selection_survives_reordering_and_does_not_show_other_color(api):
    repo, state = api
    state['product']['colors'].reverse()
    selected = repo.get('GF:abc123:orange')
    assert selected['variant_name'] == 'Orange'
    assert selected['images'] == []


def test_expired_catalogue_is_not_returned_on_outage(api):
    repo, state = api
    repo.products()
    state['now'] = 31
    repo.transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(ProductAPIError):
        repo.products()


def test_cached_catalogue_cannot_hide_failure_of_fresh_stock_verification(api, monkeypatch):
    repo, state = api
    repo.products()
    repo.transport = httpx.MockTransport(lambda r: httpx.Response(503))
    monkeypatch.setattr(tools, 'get_product_repository', lambda: repo)
    assert 'cannot be confirmed' in tools.check_stock.invoke({'product_id': 'GF:abc123:green'})
    assert 'temporarily unavailable' in tools.search_products.invoke({'query': 'bottle'})


@pytest.mark.parametrize('change', [
    {'discountedPrice': -1}, {'discountedPrice': True}, {'discountedPrice': 'NaN'},
    {'isActive': 'true'}, {'stockQuantity': True}, {'_id': 'different'},
])
def test_malformed_business_values_or_identity_rejected(api, change):
    repo, state = api
    state['product'].update(change)
    with pytest.raises(ProductAPIError):
        repo.get('GF:abc123')


@pytest.mark.parametrize('meta', [
    {'page': 2, 'pages': 2, 'total': 2, 'limit': 1},
    {'page': 1, 'pages': 30, 'total': 30, 'limit': 1},
    {'page': 1, 'pages': 1, 'total': 2, 'limit': 1},
])
def test_bad_or_incomplete_pagination_is_not_cached(meta):
    repo = GreenFibreProductRepository('https://api.example.test/api',
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            'success': True, 'products': [PRODUCT], 'pagination': meta})))
    with pytest.raises(ProductAPIError):
        repo.products()
    assert repo._cache is None


def test_json_repository_is_not_available_to_paid_providers(monkeypatch):
    monkeypatch.setattr(tools.settings, 'product_source', 'json')
    monkeypatch.setattr(tools.settings, 'llm_provider', 'groq')
    with pytest.raises(ProductAPIError):
        tools.get_product_repository()
