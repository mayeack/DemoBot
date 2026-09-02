"""Synthetic per-theme data for the NVIDIA AI Virtual Assistant blueprint.

The reference blueprint pairs an unstructured knowledge base (product manuals
and FAQs in Milvus) with structured customer/order records (Postgres). DemoBot
ships small SYNTHETIC equivalents per Application Theme under
``blueprint_data/<theme>/``:

    docs/*.md      knowledge articles the ``retrieve_knowledge`` tool searches
    records.json   customer / patient / subscriber records ``lookup_record`` reads

Nothing here is real data. Records are assigned to a session's synthetic
end-user id deterministically so a conversation keeps "its" record across turns
(the blueprint's per-session memory of who it is talking to).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import BASE_DIR

logger = logging.getLogger(__name__)

DATA_DIR = Path(BASE_DIR) / "blueprint_data"

_lock = threading.Lock()
_docs_cache: Dict[str, List[Dict[str, str]]] = {}
_records_cache: Dict[str, List[Dict[str, Any]]] = {}


def theme_dir(theme: str) -> Path:
    return DATA_DIR / (theme or "medadvice")


def docs_for(theme: str) -> List[Dict[str, str]]:
    """Knowledge articles for a theme: [{title, path, text}], cached."""
    with _lock:
        if theme in _docs_cache:
            return list(_docs_cache[theme])
    out: List[Dict[str, str]] = []
    folder = theme_dir(theme) / "docs"
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("blueprint_data: cannot read %s: %s", path, exc)
                continue
            first = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), path.stem)
            out.append({"title": first, "path": str(path), "text": text})
    with _lock:
        _docs_cache[theme] = out
    return list(out)


def records_for(theme: str) -> List[Dict[str, Any]]:
    with _lock:
        if theme in _records_cache:
            return list(_records_cache[theme])
    out: List[Dict[str, Any]] = []
    path = theme_dir(theme) / "records.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out = [r for r in (data.get("records") if isinstance(data, dict) else data) or [] if isinstance(r, dict)]
        except (OSError, ValueError) as exc:
            logger.warning("blueprint_data: cannot read %s: %s", path, exc)
    with _lock:
        _records_cache[theme] = out
    return list(out)


def record_for(theme: str, enduser_id: Optional[str]) -> Dict[str, Any]:
    """The synthetic record bound to this end user (stable per enduser_id)."""
    records = records_for(theme)
    if not records:
        return {}
    if not enduser_id:
        return dict(records[0])
    idx = int(hashlib.sha256(str(enduser_id).encode()).hexdigest(), 16) % len(records)
    return dict(records[idx])


def clear_cache() -> None:
    with _lock:
        _docs_cache.clear()
        _records_cache.clear()
