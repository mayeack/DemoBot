"""Thin async httpx wrapper for posting events to a Splunk HEC endpoint.

Ported from ThreatGenerator. Security notes:
- The token is sent only via the Authorization header and is NEVER logged.
- HTTPS is enforced at URL-parse time; TLS verification defaults on.
- Network/HTTP errors never raise out of ``send_batch`` — they return a
  sanitized result struct so the caller (forwarder) can record stats.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from backend.hec.config import HECConfig

logger = logging.getLogger(__name__)


class HECError(Exception):
    """Raised for configuration problems (e.g. bad URL). Message is sanitized
    and never contains the token."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class HECSendResult:
    ok: bool
    status_code: Optional[int]
    latency_ms: float
    error: Optional[str] = None


def normalize_hec_url(raw_url: str) -> str:
    """Ensure the URL targets ``/services/collector/event``; reject non-HTTPS."""
    if not raw_url:
        raise HECError("HEC URL is empty")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme.lower() != "https":
        raise HECError("HEC URL must use https://")
    if not parsed.netloc:
        raise HECError("HEC URL is missing a host")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/services/collector/event"
    elif path.endswith("/services/collector"):
        path = path + "/event"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _sanitize_error(exc: BaseException, status_code: Optional[int] = None) -> str:
    name = type(exc).__name__
    if status_code is not None:
        return f"{name}: HTTP {status_code}"
    return name


class HECClient:
    """Async client for posting newline-delimited JSON event batches to HEC."""

    def __init__(self, cfg: HECConfig, token: Optional[str]) -> None:
        self._cfg = cfg
        self._token = (token or "").strip()
        self._url = normalize_hec_url(cfg.url) if cfg.url else ""
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def endpoint(self) -> str:
        return self._url

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._cfg.request_timeout_s,
                verify=self._cfg.verify_tls,
                http2=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("hec_client_close_failed", exc_info=True)
            finally:
                self._client = None

    async def send_batch(self, events: list[dict[str, Any]]) -> HECSendResult:
        """POST a batch of HEC event dicts. Never raises for network/HTTP errors."""
        if not events:
            return HECSendResult(ok=True, status_code=None, latency_ms=0.0)
        if not self._token:
            return HECSendResult(False, None, 0.0, error="HEC token not set")
        if not self._url:
            return HECSendResult(False, None, 0.0, error="HEC URL not configured")

        # The HEC /event endpoint accepts newline-delimited JSON, NOT a JSON array.
        body_parts = []
        for ev in events:
            try:
                body_parts.append(json.dumps(ev, separators=(",", ":")))
            except (TypeError, ValueError):
                continue
        if not body_parts:
            return HECSendResult(ok=True, status_code=None, latency_ms=0.0)
        body = "\n".join(body_parts).encode("utf-8")

        headers = {
            "Authorization": f"Splunk {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        client = await self._ensure_client()
        t0 = time.monotonic()
        try:
            resp = await client.post(self._url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            latency = (time.monotonic() - t0) * 1000.0
            logger.warning("hec_send_transport_error type=%s latency_ms=%.1f",
                           type(exc).__name__, latency)
            return HECSendResult(False, None, latency, error=_sanitize_error(exc))

        latency = (time.monotonic() - t0) * 1000.0
        if 200 <= resp.status_code < 300:
            return HECSendResult(True, resp.status_code, latency)

        reason = f"HTTP {resp.status_code}"
        try:
            payload = resp.json()
            text = payload.get("text") if isinstance(payload, dict) else None
            if isinstance(text, str) and text:
                reason = f"{reason}: {text[:80]}"
        except Exception:
            pass
        logger.warning("hec_send_http_error status=%s", resp.status_code)
        return HECSendResult(False, resp.status_code, latency, error=reason)
