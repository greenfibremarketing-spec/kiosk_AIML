"""Convert MongoDB product records into traceable text chunks without an LLM."""

import hashlib
import html
import json
import re
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter


# Customer-facing product facts only; omit internal margins and competitor data.
PRODUCT_FIELDS = (
    'name', 'sku', 'slug', 'unit', 'tagline', 'collectionName', 'category',
    'subCategory', 'shortDescription', 'description', 'productFeatures',
    'material', 'size', 'color', 'colors', 'specs', 'dimensions', 'productWeight',
    'package', 'careInstructions', 'sustainability', 'giftSetContents',
    'giftPackaging', 'leadTime', 'branding', 'brandingTypes', 'originalPrice',
    'discountedPrice', 'b2cPrice', 'b2bPrice', 'tax', 'stockQuantity',
    'isActive', 'tags',
)
INGESTION_OWNER = 'kiosk_product_chunks_v1'


def readable(value: Any) -> str:
    if isinstance(value, dict):
        return '; '.join(f'{key}: {readable(item)}' for key, item in value.items()
                         if item is not None)
    if isinstance(value, list):
        return '; '.join(readable(item) for item in value if item is not None)
    if isinstance(value, str):
        return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value))).strip()
    return str(value)


def chunk_product(product: dict, source: str, chunk_size: int = 800,
                  chunk_overlap: int = 120) -> list[dict]:
    """Repeat product identity in each chunk and retain exact source price fields."""
    if chunk_size < 100 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError('chunk_size must be >= 100 and overlap must be smaller than size')
    product_id = str(product['_id'])
    title = readable(product.get('name', product_id))
    body = '\n'.join(f'{field}: {readable(product[field])}' for field in PRODUCT_FIELDS
                     if field in product and product[field] is not None
                     and product[field] != '' and product[field] != [])
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        separators=['\n', '. ', '; ', ' ', ''],
    )
    pieces = splitter.split_text(body or f'Product ID: {product_id}')
    chunks = []
    for index, piece in enumerate(pieces):
        content = f'Product: {title}\nProduct ID: {product_id}\n{piece}'
        chunk_id = hashlib.sha256(json.dumps(
            [INGESTION_OWNER, source, product_id, index],
            ensure_ascii=False, separators=(',', ':'),
        ).encode()).hexdigest()
        chunks.append({
            '_id': chunk_id,
            'ingestion_owner': INGESTION_OWNER,
            'content': content,
            'content_hash': hashlib.sha256(content.encode()).hexdigest(),
            'metadata': {
                'source': source, 'product_id': product_id, 'name': title,
                'sku': product.get('sku'), 'chunk_index': index,
                'chunk_count': len(pieces), 'chunk_size': chunk_size,
                'chunk_overlap': chunk_overlap,
                'source_updated_at': str(product.get('updatedAt', '')),
                'is_active': product.get('isActive'),
                'prices': {field: product[field] for field in
                           ('originalPrice', 'discountedPrice', 'b2cPrice', 'b2bPrice')
                           if product.get(field) is not None},
                'stock_quantity': product.get('stockQuantity'),
            },
        })
    return chunks
