"""Knowledge retrieval for the NVIDIA AI Virtual Assistant blueprint.

The reference blueprint's unstructured retriever embeds product docs with a
NeMo Retriever embedding NIM and searches Milvus. DemoBot keeps the feature
without the infrastructure: an in-process index over the theme's synthetic
knowledge articles (``blueprint_data/<theme>/docs``), embedded through a LOCAL
embedding endpoint when one is configured (``blueprint_embed_url`` — e.g. a
``llama-nemotron-embed-1b-v2`` NIM on this host, OpenAI-compatible
``/v1/embeddings``) and otherwise scored by keyword overlap. The fallback keeps
the blueprint functional — and deterministic in tests — with no model at all.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from typing import Any, Dict, List, Optional

from backend.agents.blueprints import data
from backend.config import settings

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "it", "i", "my",
    "me", "you", "your", "with", "have", "has", "had", "be", "been", "this", "that", "what",
    "can", "do", "does", "how", "about", "any", "at", "as", "by", "from", "if", "so", "we",
}

_lock = threading.Lock()
_chunks: Dict[str, List[Dict[str, Any]]] = {}
_vectors: Dict[str, Optional[List[List[float]]]] = {}


def _stem(token: str) -> str:
    """Tiny suffix stemmer so "dropping"/"drops"/"dropped" meet at "drop"."""
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            stem = token[: -len(suffix)]
            return stem + "y" if suffix == "ies" else stem
    return token


def _tokens(text: str) -> List[str]:
    return [_stem(t) for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


def _chunk_docs(theme: str) -> List[Dict[str, Any]]:
    """Paragraph-level chunks with their article title."""
    with _lock:
        if theme in _chunks:
            return _chunks[theme]
    out: List[Dict[str, Any]] = []
    for doc in data.docs_for(theme):
        for para in re.split(r"\n\s*\n", doc["text"]):
            para = para.strip()
            if len(para) < 40 or para.startswith("#"):
                continue
            # The article title is part of what a paragraph is "about".
            out.append({"title": doc["title"], "text": para,
                        "tokens": set(_tokens(para)) | set(_tokens(doc["title"]))})
    with _lock:
        _chunks[theme] = out
    return out


# ---------------------------------------------------------------- embeddings (local only)
def embed_endpoint() -> Optional[str]:
    """The local embedding endpoint, or None (keyword fallback)."""
    url = (getattr(settings, "blueprint_embed_url", "") or "").strip().rstrip("/")
    if not url:
        return None
    from backend import nvidia_nim

    try:
        return nvidia_nim.validate_nim_base_url(url)
    except ValueError as exc:
        logger.warning("blueprint_embed_url ignored: %s", exc)
        return None


def _embed(texts: List[str]) -> Optional[List[List[float]]]:
    base = embed_endpoint()
    if not base or not texts:
        return None
    import httpx

    try:
        r = httpx.post(
            f"{base}/embeddings",
            json={"model": settings.blueprint_embed_model, "input": texts, "input_type": "passage"},
            timeout=20.0,
        )
        r.raise_for_status()
        rows = sorted(r.json().get("data", []), key=lambda d: d.get("index", 0))
        vecs = [d.get("embedding") for d in rows]
        return vecs if len(vecs) == len(texts) and all(vecs) else None
    except Exception as exc:  # noqa: BLE001 - fall back to keywords, never fail the turn
        logger.warning("embedding call failed (%s); using keyword retrieval", exc)
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _vectors_for(theme: str, chunks: List[Dict[str, Any]]) -> Optional[List[List[float]]]:
    with _lock:
        if theme in _vectors:
            return _vectors[theme]
    vecs = _embed([c["text"] for c in chunks]) if embed_endpoint() else None
    with _lock:
        _vectors[theme] = vecs
    return vecs


# ---------------------------------------------------------------- search
def search(theme: str, query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Top-k knowledge chunks for a query: [{title, text, score, method}]."""
    chunks = _chunk_docs(theme)
    if not chunks or not (query or "").strip():
        return []
    vecs = _vectors_for(theme, chunks)
    if vecs:
        qv = _embed([query])
        if qv:
            scored = [(_cosine(qv[0], v), c) for v, c in zip(vecs, chunks)]
            scored.sort(key=lambda t: t[0], reverse=True)
            return [{"title": c["title"], "text": c["text"], "score": round(s, 4), "method": "embedding"}
                    for s, c in scored[:k] if s > 0]
    q = set(_tokens(query))
    if not q:
        return []
    scored = []
    for c in chunks:
        overlap = len(q & c["tokens"])
        if overlap:
            scored.append((overlap / math.sqrt(len(c["tokens"]) or 1), c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"title": c["title"], "text": c["text"], "score": round(s, 4), "method": "keyword"}
            for s, c in scored[:k]]


def clear_cache() -> None:
    with _lock:
        _chunks.clear()
        _vectors.clear()
    data.clear_cache()
