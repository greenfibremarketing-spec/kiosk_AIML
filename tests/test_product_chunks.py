from app.product_chunks import chunk_product


def test_chunks_preserve_identity_facts_and_have_stable_ids():
    product = {'_id': 'p1', 'name': 'Bottle', 'description': 'Reusable bottle. ' * 100,
               'stockQuantity': 0, 'discountedPrice': 499, 'isActive': False}
    chunks = chunk_product(product, 'greenfibre.products', 200, 30)
    assert len(chunks) > 1
    assert chunks == chunk_product(product, 'greenfibre.products', 200, 30)
    assert len({c['_id'] for c in chunks}) == len(chunks)
    for chunk in chunks:
        assert chunk['content'].startswith('Product: Bottle\nProduct ID: p1\n')
        assert chunk['metadata']['stock_quantity'] == 0
        assert chunk['metadata']['is_active'] is False
        assert chunk['metadata']['prices'] == {'discountedPrice': 499}


def test_omits_internal_fields_and_removes_html():
    chunks = chunk_product({'_id': 'p1', 'name': 'Cup', 'b2bMargin': 999,
                            'description': '<p>Rice &amp; husk</p>'}, 'db.products')
    assert 'Rice & husk' in chunks[0]['content']
    assert '999' not in chunks[0]['content']
    assert '<p>' not in chunks[0]['content']


def test_source_and_product_ids_do_not_collide():
    first = chunk_product({'_id': 'a', 'name': 'Cup'}, 'db.products')[0]
    second = chunk_product({'_id': 'b', 'name': 'Cup'}, 'db.products')[0]
    third = chunk_product({'_id': 'a', 'name': 'Cup'}, 'other.products')[0]
    assert len({first['_id'], second['_id'], third['_id']}) == 3


def test_rejects_invalid_overlap():
    import pytest
    with pytest.raises(ValueError):
        chunk_product({'_id': 'p1'}, 'db.products', 200, 200)
