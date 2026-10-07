"""Repository-aware context (RAG).

The repo is chunked, embedded with FastEmbed (small ONNX model, no GPU/torch
needed) and stored in an in-memory FAISS index. For each changed file we
retrieve the most related code from *other* files, so the LLM can spot
things like a renamed function that is still called elsewhere.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol

log = logging.getLogger(__name__)

CODE_EXTENSIONS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".kt", ".swift", ".scala", ".sql", ".sh",
    ".yml", ".yaml", ".toml", ".json", ".md",
)


class ContextProvider(Protocol):
    def related_context(self, path: str, query: str) -> str: ...


class NoContext:
    def related_context(self, path: str, query: str) -> str:
        return ""


class RepoIndex:
    def __init__(self, files: dict[str, str], embedding_model: str, top_k: int = 4):
        # Imported lazily so the rest of the app works without the RAG extras.
        from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
        from langchain_community.vectorstores import FAISS
        from langchain_core.documents import Document
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)
        docs: list[Document] = []
        for path, text in files.items():
            for chunk in splitter.split_text(text):
                docs.append(Document(page_content=chunk, metadata={"path": path}))

        self.top_k = top_k
        self.store = None
        if docs:
            embeddings = FastEmbedEmbeddings(model_name=embedding_model,
                                             cache_dir=os.getenv("FASTEMBED_CACHE_PATH"))
            self.store = FAISS.from_documents(docs, embeddings)
        log.info("Indexed %d chunks from %d files", len(docs), len(files))

    def related_context(self, path: str, query: str) -> str:
        if self.store is None or not query.strip():
            return ""
        # Over-fetch, then drop chunks from the file under review itself.
        hits = self.store.similarity_search(query[:4000], k=self.top_k * 3)
        picked = [d for d in hits if d.metadata.get("path") != path][: self.top_k]
        return "\n\n".join(
            f"### {d.metadata['path']}\n```\n{d.page_content}\n```" for d in picked
        )


def build_context(gh, repo: str, ref: str, settings) -> ContextProvider:
    if not settings.enable_rag:
        return NoContext()
    try:
        files = gh.download_repo_files(repo, ref, max_files=settings.rag_max_repo_files,
                                       extensions=CODE_EXTENSIONS)
        return RepoIndex(files, settings.embedding_model, settings.rag_top_k)
    except Exception as exc:  # RAG is a bonus: never fail the review because of it
        log.warning("RAG disabled for this run: %s", exc)
        return NoContext()
