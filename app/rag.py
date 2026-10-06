"""RAG (Retrieval Augmented Generation) pipeline for Green Fibre Knowledge Base.

Includes document loading, recursive text chunking, HuggingFace embeddings,
FAISS vector indexing, distance threshold filtering, eager warmup, and retrieval tools.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

# LangChain Document Loading & Text Splitting
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Standalone embeddings package
from langchain_huggingface import HuggingFaceEmbeddings

# Vector Store
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.tools import tool

from app.config import settings

logger = logging.getLogger("green_fibre.rag")

_embeddings_instance: Optional[HuggingFaceEmbeddings] = None
_vector_store_instance: Optional[FAISS] = None


def get_embeddings() -> HuggingFaceEmbeddings:
    """Singleton getter for HuggingFaceEmbeddings to avoid reloading model weights."""
    global _embeddings_instance
    if _embeddings_instance is None:
        _embeddings_instance = HuggingFaceEmbeddings(
            model_name=settings.embedding_model_name,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    return _embeddings_instance


def load_knowledge_documents(knowledge_dir: Optional[str] = None) -> List[Document]:
    """Load all markdown documents from the knowledge directory."""
    dir_path = knowledge_dir or settings.knowledge_dir
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"Knowledge directory '{dir_path}' does not exist.")

    documents: List[Document] = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            if file.endswith((".md", ".txt")):
                file_path = os.path.join(root, file)
                loader = TextLoader(file_path, encoding="utf-8")
                loaded = loader.load()
                for doc in loaded:
                    doc.metadata["source"] = os.path.relpath(file_path, dir_path)
                documents.extend(loaded)

    return documents


def split_documents(
    documents: List[Document],
    chunk_size: int = 400,
    chunk_overlap: int = 60,
) -> List[Document]:
    """Split documents into concise chunks using RecursiveCharacterTextSplitter."""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return text_splitter.split_documents(documents)


def build_vector_store(
    knowledge_dir: Optional[str] = None,
    save_path: Optional[str] = None,
) -> FAISS:
    """Build a FAISS vector store from knowledge documents and save it to disk."""
    dir_path = knowledge_dir or settings.knowledge_dir
    target_path = save_path or settings.vector_store_path

    documents = load_knowledge_documents(dir_path)
    if not documents:
        raise ValueError(f"No documents found in knowledge directory: {dir_path}")

    chunks = split_documents(documents)
    embeddings = get_embeddings()

    vector_store = FAISS.from_documents(chunks, embeddings)
    os.makedirs(target_path, exist_ok=True)
    vector_store.save_local(target_path)

    global _vector_store_instance
    _vector_store_instance = vector_store
    return vector_store


def load_vector_store(save_path: Optional[str] = None) -> Optional[FAISS]:
    """Load an existing FAISS vector store from disk."""
    global _vector_store_instance
    if _vector_store_instance is not None:
        return _vector_store_instance

    target_path = save_path or settings.vector_store_path
    if not os.path.exists(target_path):
        return None

    index_file = os.path.join(target_path, "index.faiss")
    if not os.path.exists(index_file):
        return None

    embeddings = get_embeddings()
    try:
        vector_store = FAISS.load_local(
            target_path,
            embeddings,
            allow_dangerous_deserialization=True,
        )
        _vector_store_instance = vector_store
        return vector_store
    except Exception as e:
        logger.warning("Could not load local vector store: %s", e)
        return None


def get_vector_store() -> FAISS:
    """Return existing vector store or automatically build one if absent."""
    store = load_vector_store()
    if store is None:
        store = build_vector_store()
    return store


def warmup_rag() -> Dict[str, float]:
    """Eagerly load embeddings model and vector store at startup to prevent turn latency spikes.
    
    Returns a dictionary with cold startup timing in milliseconds.
    """
    t0 = time.time()
    get_embeddings()
    t_emb = time.time()
    get_vector_store()
    t_store = time.time()

    emb_ms = (t_emb - t0) * 1000.0
    store_ms = (t_store - t_emb) * 1000.0
    total_ms = (t_store - t0) * 1000.0

    logger.info(
        "RAG warmup complete: embeddings=%.1fms, store=%.1fms, total=%.1fms",
        emb_ms, store_ms, total_ms
    )
    return {
        "embeddings_load_ms": round(emb_ms, 2),
        "vector_store_load_ms": round(store_ms, 2),
        "total_warmup_ms": round(total_ms, 2),
    }


def get_retriever(k: int = 3):
    """Obtain a LangChain VectorStoreRetriever configured for top-k nearest neighbors."""
    store = get_vector_store()
    return store.as_retriever(search_kwargs={"k": k})


def retrieve_relevant_chunks(
    query: str,
    k: int = 3,
    score_threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Retrieve top-k relevant chunks with similarity score filtering.
    
    Drops weak matches where score > score_threshold (default 1.2).
    """
    threshold = (
        score_threshold if score_threshold is not None else settings.rag_score_threshold
    )
    store = get_vector_store()
    results = store.similarity_search_with_score(query, k=k)

    chunks_data = []
    for doc, score in results:
        score_val = float(score)
        # Drop weak chunks where L2 distance exceeds threshold
        if threshold is not None and score_val > threshold:
            continue
        chunks_data.append({
            "content": doc.page_content,
            "source": doc.metadata.get("source", "unknown"),
            "score": round(score_val, 4),
        })
    return chunks_data


def print_retrieval_inspect(
    query: str,
    k: int = 3,
    score_threshold: Optional[float] = None,
) -> None:
    """Print retrieved chunks in a readable, formatted view to inspect retrieval quality."""
    chunks = retrieve_relevant_chunks(query, k=k, score_threshold=score_threshold)
    thresh_val = score_threshold if score_threshold is not None else settings.rag_score_threshold
    print("=" * 75)
    print(f"RETRIEVAL INSPECTION FOR QUERY: \"{query}\" (Threshold: <= {thresh_val})")
    print(f"Matching chunks retrieved: {len(chunks)}")
    print("=" * 75)
    if not chunks:
        print("  No chunks met the relevance threshold (distance > 1.2 dropped).")
    for i, chunk in enumerate(chunks, 1):
        print(f"\n[Chunk {i}] Source: {chunk['source']} | Score: {chunk['score']:.4f}")
        print("-" * 75)
        for line in chunk['content'].strip().split("\n"):
            print(f"  {line}")
    print("\n" + "=" * 75)


@tool
def search_knowledge(query: str) -> str:
    """Search verified store policies, shipping rates, returns, materials, and brand information.
    
    Args:
        query: Knowledge question or topic (e.g., 'return shipping', 'garment washing care', 'seed tags').
        
    Returns:
        JSON string list of verified knowledge snippets, or a neutral not-found notice.
    """
    chunks = retrieve_relevant_chunks(query, k=2, score_threshold=settings.rag_score_threshold)
    if not chunks:
        return f"No verified store knowledge found matching '{query}'. State honestly that you do not have this information."

    results = [{"source": c["source"], "text": c["content"]} for c in chunks]
    return json.dumps(results, indent=2)
