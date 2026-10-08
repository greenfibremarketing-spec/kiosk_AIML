# Greenfibre product chunks

`scripts/ingest_mongo.py` reads the MongoDB product collection, splits customer-facing
product facts into text chunks, and exports them to the ignored file
`data/mongo_chunks/product_chunks.jsonl`. Each chunk retains the product ID, name,
source collection, source update time, exact price fields, stock, and a content hash.
Internal margins and competitor records are excluded. All products are included;
the original `isActive` status is preserved for later filtering.

Install dependencies with `python -m pip install -r requirements.txt`.
Set `MONGO_URL` in `.env` to your source connection string. Do not commit credentials.

Preview without writing to MongoDB:

```powershell
python scripts/ingest_mongo.py
```

Write to `greenfibre.product_chunks` in the same Atlas database:

```powershell
python scripts/ingest_mongo.py --write
```

To store chunks in a running local MongoDB instance instead, first set:

```dotenv
MONGO_CHUNKS_URL=mongodb://localhost:27017/greenfibre
MONGO_CHUNKS_DATABASE=greenfibre
MONGO_CHUNKS_COLLECTION=product_chunks
```

The source remains Atlas. Optional source settings are `MONGO_DATABASE` and
`MONGO_PRODUCTS_COLLECTION`; their defaults are `greenfibre` and `products`.
Chunk size defaults to 800 characters with 120 characters of overlap; identity
headers are added after splitting and are additional to that limit.

Writes use deterministic IDs, so rerunning updates the same product chunks.
After successful upserts, superseded chunks for processed products are removed
only within this ingester's records. Products deleted from the source are not
automatically purged from the chunk collection. The script reads stored hashes
back to verify the write. The source product collection is never modified.
For write failures, rerun after resolving the reported connection or permission issue.

These are text chunks, without embeddings or an Atlas vector index. The kiosk's
existing FAISS retrieval and JSON product tools are unchanged; connecting the chatbot
to the new collection is a separate integration.

Tests: `python -m pytest tests/test_product_chunks.py -q`.
