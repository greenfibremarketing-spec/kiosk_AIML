"""Knowledge base ingestion script for Greenie AI Kiosk.

Builds or rebuilds the FAISS vector store index from markdown documents in data/knowledge/.
Also supports interactive query inspection via the `--inspect` flag.

Usage:
  python scripts/ingest.py
  python scripts/ingest.py --inspect "How do returns work?"
"""

import argparse
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.rag import (
    build_vector_store,
    load_knowledge_documents,
    print_retrieval_inspect,
    split_documents,
)


def run_ingest():
    print("=" * 65)
    print("  GREENIE - KNOWLEDGE BASE INGESTION")
    print("=" * 65)
    print(f"Knowledge Dir      : {settings.knowledge_dir}")
    print(f"Vector Store Path  : {settings.vector_store_path}")
    print(f"Embedding Model    : {settings.embedding_model_name}")
    print("=" * 65)

    print("\n1. Loading markdown documents...")
    docs = load_knowledge_documents(settings.knowledge_dir)
    print(f"   -> Loaded {len(docs)} documents.")
    for d in docs:
        print(f"      - {d.metadata.get('source')}")

    print("\n2. Splitting documents with RecursiveCharacterTextSplitter...")
    chunks = split_documents(docs)
    print(f"   -> Generated {len(chunks)} text chunks.")

    print("\n3. Generating embeddings & building FAISS index...")
    build_vector_store(settings.knowledge_dir, settings.vector_store_path)
    print("   -> FAISS index created and saved successfully.")

    print("\n" + "=" * 65)
    print("Ingestion complete! Vector store is ready for retrieval.")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Ingest knowledge files into FAISS vector store")
    parser.add_argument(
        "--inspect",
        type=str,
        help="Run retrieval inspection for a question to view matching chunks and scores",
    )
    parser.add_argument(
        "-k",
        type=int,
        default=3,
        help="Number of chunks to retrieve for inspection (default: 3)",
    )

    args = parser.parse_args()

    if args.inspect:
        print_retrieval_inspect(args.inspect, k=args.k)
    else:
        run_ingest()


if __name__ == "__main__":
    main()
