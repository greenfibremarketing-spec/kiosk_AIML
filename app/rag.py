"""RAG (Retrieval Augmented Generation) pipeline for Green Fibre Knowledge Base.

Includes document loading, recursive text chunking, HuggingFace embeddings,
FAISS vector indexing, and retrieval inspection helpers.
"""

import os
from typing import Any, Dict, List, Optional

# LangChain Document Loading
# TextLoader: Reads text/markdown files preserving source metadata.
from langchain_community.document_loaders import TextLoader

# LangChain Text Splitters
# RecursiveCharacterTextSplitter: Recursively splits text by double-newlines, single-newlines,
# and spaces to keep coherent paragraphs intact while honoring chunk size limits.
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Embeddings Integration
# HuggingFaceEmbeddings: Modern standalone langchain-huggingface package generating
# local dense vector representations using sentence-transformers/all-MiniLM-L6-v2.
from langchain_huggingface import HuggingFaceEmbeddings

# Vector Store
# FAISS: Facebook AI Similarity Search provides fast, file-persisted vector indexing
# with zero background server overhead.
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from app.config import settings


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
                # Attach clean relative source path
                for doc in loaded:
                    doc.metadata["source"] = os.path.relpath(file_path, dir_path)
                documents.extend(loaded)

    return documents


def split_documents(
    documents: List[Document],
    chunk_size: int = 400,
    chunk_overlap: int = 60,
) -> List[Document]:
    """Split documents into voice-optimized concise chunks using RecursiveCharacterTextSplitter.
    
    Chunk size 400 with 60 overlap keeps individual knowledge concepts self-contained
    for spoken responses without exceeding LLM context boundaries.
    """
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

    # Index chunks into FAISS vector store
    vector_store = FAISS.from_documents(chunks, embeddings)

    # Persist index to directory
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

    # Check if faiss index files exist
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
    except Exception:
        return None


def get_vector_store() -> FAISS:
    """Return existing vector store or automatically build one if absent."""
    store = load_vector_store()
    if store is None:
        store = build_vector_store()
    return store


def get_retriever(k: int = 3):
    """Obtain a LangChain VectorStoreRetriever configured for top-k nearest neighbors."""
    store = get_vector_store()
    return store.as_retriever(search_kwargs={"k": k})


def retrieve_relevant_chunks(query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Retrieve top-k relevant chunks with similarity scores and metadata for inspection."""
    store = get_vector_store()
    # similarity_search_with_score returns (Document, float_distance)
    results = store.similarity_search_with_score(query, k=k)

    chunks_data = []
    for doc, score in results:
        chunks_data.append({
            "content": doc.page_content,
            "source": doc.metadata.get("source", "unknown"),
            "score": float(score),
        })
    return chunks_data


def print_retrieval_inspect(query: str, k: int = 3) -> None:
    """Print retrieved chunks in a readable, formatted view to inspect retrieval quality."""
    chunks = retrieve_relevant_chunks(query, k=k)
    print("=" * 75)
    print(f"RETRIEVAL INSPECTION FOR QUERY: \"{query}\"")
    print(f"Top {len(chunks)} chunks retrieved (lower distance score = higher relevance):")
    print("=" * 75)
    for i, chunk in enumerate(chunks, 1):
        print(f"\n[Chunk {i}] Source: {chunk['source']} | Score: {chunk['score']:.4f}")
        print("-" * 75)
        # Indent content for clarity
        for line in chunk['content'].strip().split("\n"):
            print(f"  {line}")
    print("\n" + "=" * 75)
