from datetime import datetime, timedelta, timezone
import json

import pytest

from app import tools
from app.product_repository import JsonProductRepository, stock_state


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / 'products.json'
    def write(products=(), bundles=()):
        path.write_text(json.dumps({'products': list(products), 'gift_bundles': list(bundles)}), encoding='utf-8')
    write()
    monkeypatch.setattr(tools, 'get_product_repository', lambda: JsonProductRepository(str(path)))
    return write


def test_missing_stock_and_price_are_unknown(catalog):
    catalog([{'id': 'cup', 'name': 'Cup'}])
    product = json.loads(tools.search_products.invoke({}))[0]
    assert product['price'] is product['stock'] is product['in_stock'] is None
    assert product['stock_status'] == 'unknown'
    assert 'unknown' in tools.check_stock.invoke({'product_id': 'cup'})


@pytest.mark.parametrize('quantity,expected', [(0, 'out_of_stock'), (5, 'in_stock')])
def test_recent_stock(quantity, expected):
    assert stock_state({'stock': quantity, 'stock_checked_at': datetime.now(timezone.utc).isoformat()}) == (expected, quantity)


@pytest.mark.parametrize('data', [
    {'stock': 4}, {'stock': True}, {'stock': -1}, {'stock': '4'},
    {'stock': 4, 'stock_checked_at': 'invalid'},
    {'stock': 4, 'stock_checked_at': datetime.now().isoformat()},
    {'stock': 4, 'stock_checked_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
    {'stock': 4, 'stock_checked_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    {'stock': 4, 'in_stock': False, 'stock_checked_at': datetime.now(timezone.utc).isoformat()},
])
def test_uncertain_stock_never_becomes_available(data):
    assert stock_state(data) == ('unknown', None)


def test_inactive_and_missing_are_distinct(catalog):
    catalog([{'id': 'cup', 'name': 'Cup', 'isActive': False}])
    assert 'inactive' in tools.check_stock.invoke({'product_id': 'cup'})
    assert 'not available' in tools.check_stock.invoke({'product_id': 'missing'})
    assert 'No matching' in tools.search_products.invoke({})


@pytest.mark.parametrize('identifier', ['', ' ', 'Cu'])
def test_lookup_does_not_choose_first_product(catalog, identifier):
    catalog([{'id': 'cup', 'name': 'Cup'}])
    assert 'not available' in tools.get_product_details.invoke({'product_id': identifier})


def test_snapshot_price_keeps_decimals_and_refreshes(catalog):
    catalog([{'id': 'cup', 'price': 123.45}])
    assert json.loads(tools.get_product_details.invoke({'product_id': 'cup'}))['price'] == 123.45
    catalog([{'id': 'cup', 'price': 150.75}])
    assert json.loads(tools.get_product_details.invoke({'product_id': 'cup'}))['price'] == 150.75


@pytest.mark.parametrize('price', [None, -1, True, 'NaN', 'Infinity'])
def test_invalid_prices_do_not_pass_budget_filter(catalog, price):
    catalog([{'id': 'cup', 'price': price}], [{'id': 'gift', 'price': price}])
    assert 'No matching' in tools.search_products.invoke({'max_price': 500})
    assert 'No active gift' in tools.get_gift_bundles.invoke({'budget': 500})


def test_bundle_no_match_never_invents_minimum_price(catalog):
    catalog(bundles=[{'id': 'gift', 'price': 1400}])
    reply = tools.get_gift_bundles.invoke({'budget': 1000})
    assert '949' not in reply
    assert 'No active gift' in reply


def test_unapproved_certifications_not_exposed(catalog):
    catalog([{'id': 'cup', 'sustainability_notes': 'certified safe', 'certifications': ['unknown']}])
    product = json.loads(tools.get_product_details.invoke({'product_id': 'cup'}))
    assert product['certifications'] == []
    assert 'sustainability_notes' not in product
