"""Read Atlas products, export chunks locally, and optionally write to MongoDB.

Preview: python scripts/ingest_mongo.py
Persist: python scripts/ingest_mongo.py --write
Connection strings are read only from environment variables / .env.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import PyMongoError

from app.product_chunks import chunk_product, INGESTION_OWNER, PRODUCT_FIELDS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Persist prepared chunks to MongoDB')
    parser.add_argument('--chunk-size', type=int, default=800)
    parser.add_argument('--chunk-overlap', type=int, default=120)
    args = parser.parse_args()
    if args.chunk_size < 100 or not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error('chunk size must be >= 100 and overlap must be smaller than size')
    load_dotenv(ROOT / '.env')
    source_uri = os.getenv('MONGO_URL')
    if not source_uri:
        parser.error('Set MONGO_URL in .env')
    source_db = os.getenv('MONGO_DATABASE', 'greenfibre')
    source_collection = os.getenv('MONGO_PRODUCTS_COLLECTION', 'products')
    target_uri = os.getenv('MONGO_CHUNKS_URL') or source_uri
    target_db = os.getenv('MONGO_CHUNKS_DATABASE', source_db)
    target_collection = os.getenv('MONGO_CHUNKS_COLLECTION', 'product_chunks')
    # Reserve a separate collection for derived data even when using another host.
    if target_collection == source_collection:
        parser.error('The chunk collection must differ from the product collection')
    source = f'{source_db}.{source_collection}'
    options = dict(serverSelectionTimeoutMS=15000, connectTimeoutMS=12000,
                   socketTimeoutMS=30000)
    with MongoClient(source_uri, **options) as client:
        products = list(client[source_db][source_collection].find(
            {}, {field: 1 for field in (*PRODUCT_FIELDS, 'updatedAt')}
        ))
    if not products:
        print('No products found; no files or MongoDB chunks changed.')
        return 1
    chunks = [chunk for product in products for chunk in chunk_product(
        product, source, args.chunk_size, args.chunk_overlap)]
    export_dir = ROOT / 'data' / 'mongo_chunks'
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / 'product_chunks.jsonl'
    temporary = export_path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        for chunk in chunks:
            output.write(json.dumps(chunk, ensure_ascii=False, default=str) + '\n')
    temporary.replace(export_path)
    print(f'Read {len(products)} products; prepared {len(chunks)} chunks.')
    print(f'Local export: {export_path}')
    if not args.write:
        print('Preview complete. No MongoDB writes performed.')
        return 0

    with MongoClient(target_uri, **options) as client:
        collection = client[target_db][target_collection]
        collection.create_index([
            ('ingestion_owner', 1), ('metadata.source', 1), ('metadata.product_id', 1)
        ])
        timestamp = datetime.now(timezone.utc)
        operations = [ReplaceOne({'_id': chunk['_id']},
                                 {**chunk, 'ingested_at': timestamp}, upsert=True)
                      for chunk in chunks]
        for offset in range(0, len(operations), 250):
            collection.bulk_write(operations[offset:offset + 250], ordered=True)
        # Only prune superseded chunks for products successfully processed here.
        # Products removed from the source require a separate explicit cleanup.
        for product in products:
            product_id = str(product['_id'])
            ids = [chunk['_id'] for chunk in chunks
                   if chunk['metadata']['product_id'] == product_id]
            collection.delete_many({
                'ingestion_owner': INGESTION_OWNER, 'metadata.source': source,
                'metadata.product_id': product_id, '_id': {'$nin': ids},
            })
        expected = {chunk['_id']: chunk['content_hash'] for chunk in chunks}
        stored = {doc['_id']: doc.get('content_hash') for doc in collection.find(
            {'_id': {'$in': list(expected)}}, {'content_hash': 1})}
        if stored != expected:
            raise RuntimeError('MongoDB read-back verification failed')
    print(f'Verified {len(chunks)} stored chunks in {target_db}.{target_collection}.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PyMongoError as exc:
        # Driver diagnostics can include server details; never print the URI.
        print(f'MongoDB ingestion failed ({type(exc).__name__}). '
              'Check credentials, DNS, Atlas IP access, and database permissions.', file=sys.stderr)
        sys.exit(1)
