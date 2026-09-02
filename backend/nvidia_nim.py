"""Local NVIDIA NIM helpers for ``provider=nvidia``.

``provider=nvidia`` means inference from a NIM (NVIDIA Inference Microservice)
container running on THIS host: an OpenAI-compatible server on a loopback port.
It is deliberately NOT the hosted API catalog (build.nvidia.com) — that is a
cloud call, and this provider exists to demonstrate on-box GPU inference. A GPU
replica runs its own DemoBot against its own NIM; nothing here tunnels to a
remote endpoint, and a non-loopback base URL is rejected wherever it is set.

Shared by the chat-model factory (backend/agents/llm.py), the legacy client
factory (backend/services/ai_client.py), the model catalog probe
(backend/model_catalog.py), the Settings store validation and the
host-capability gate (backend/host_capabilities.py).
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Readiness / model-list probes are on the Settings poll path: keep them short.
PROBE_TIMEOUT = 2.0
DEFAULT_BASE_URL = "http://localhost:8000/v1"
# The openai SDK refuses an empty api_key; a local NIM without auth ignores it.
PLACEHOLDER_API_KEY = "nim"

_LOOPBACK_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback_url(url: str) -> bool:
    """True when ``url`` targets this host (localhost, 127.0.0.0/8, ::1)."""
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_nim_base_url(url: str) -> str:
    """Return the normalized base URL, or raise ``ValueError`` with the reason.

    Enforces the provider contract (local inference only) and normalizes to the
    OpenAI-compatible ``/v1`` root so a pasted server root still works.
    """
    value = (url or "").strip().rstrip("/")
    if not value:
        raise ValueError(
            f"NVIDIA NIM base URL is empty — expected {DEFAULT_BASE_URL}"
        )
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"NVIDIA NIM base URL {value!r} is not an http(s) URL")
    if not is_loopback_url(value):
        raise ValueError(
            f"NVIDIA NIM base URL {value!r} is not on this host. provider=nvidia is "
            "local inference only: a NIM container on loopback (e.g. "
            f"{DEFAULT_BASE_URL}). A remote GPU box runs its own DemoBot replica."
        )
    if not value.endswith("/v1"):
        value = value + "/v1"
    return value


@dataclass(frozen=True)
class FeaturedModel:
    """A NIM image offered in the model dropdown, with what it takes to run it."""

    id: str
    gpu_label: str
    min_vram_mb: int
    gpus: int


def parse_featured_models(spec: str) -> List[FeaturedModel]:
    """Parse ``settings.nvidia_featured_models``: ``id|gpu label|min VRAM MB|gpus,…``.

    Malformed entries are skipped with a warning rather than breaking startup.
    """
    out: List[FeaturedModel] = []
    for raw in (spec or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        try:
            model_id = parts[0]
            label = parts[1] if len(parts) > 1 else ""
            vram = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            gpus = int(parts[3]) if len(parts) > 3 and parts[3] else 1
        except ValueError:
            logger.warning("nvidia_featured_models: skipping malformed entry %r", raw)
            continue
        if model_id:
            out.append(FeaturedModel(model_id, label, vram, gpus))
    return out


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def nim_ready(base_url: str, api_key: str = "", timeout: float = PROBE_TIMEOUT) -> bool:
    """``GET {base}/health/ready`` — True only on HTTP 200. Never raises."""
    import httpx

    base = (base_url or "").rstrip("/")
    if not base:
        return False
    try:
        r = httpx.get(f"{base}/health/ready", headers=_headers(api_key), timeout=timeout)
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001 - a probe must never break a caller
        logger.debug("NIM readiness probe failed for %s: %s", base, exc)
        return False


def nim_models(base_url: str, api_key: str = "", timeout: float = PROBE_TIMEOUT) -> List[str]:
    """Model ids the NIM serves (``GET {base}/models``). Raises on failure; the
    catalog's best-effort wrapper turns that into an empty list."""
    import httpx

    base = (base_url or "").rstrip("/")
    r = httpx.get(f"{base}/models", headers=_headers(api_key), timeout=timeout)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))


def status(settings, *, probe: bool = True) -> Dict[str, Any]:
    """Provider status for the Settings / app UI.

    Reports the local-only contract, whether the loopback NIM is ready, the
    models it serves, and the featured NIM images with their GPU requirements —
    the UI greys out a featured model the running NIM does not serve.
    """
    base = settings.nvidia_base_url
    api_key = settings.nvidia_api_key or ""
    url_error: Optional[str] = None
    try:
        validate_nim_base_url(base)
    except ValueError as exc:
        url_error = str(exc)
    ready = bool(probe and not url_error and nim_ready(base, api_key))
    served: List[str] = []
    if ready:
        try:
            served = nim_models(base, api_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("NIM model listing failed for %s: %s", base, exc)
    return {
        "base_url": base,
        "local_only": True,
        "url_error": url_error,
        "ready": ready,
        "served": served,
        "featured": [
            {
                "id": f.id,
                "gpu": f.gpu_label,
                "min_vram_mb": f.min_vram_mb,
                "gpus": f.gpus,
                "served": f.id in served,
            }
            for f in parse_featured_models(settings.nvidia_featured_models)
        ],
        "reasoning": bool(settings.nvidia_reasoning),
    }
