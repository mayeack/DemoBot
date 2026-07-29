"""Galileo Agent Control - runtime control evaluation client.

Thin, dependency-light wrapper around Galileo's Agent Control server, used to
submit the assistant's generated response for evaluation against the Controls
defined centrally in the Galileo console before DemoBot returns it to the user.
This is the "Agent Observability Controls" surface in the settings drawer: the
Galileo counterpart of the Cisco AI Defense response review, and what makes a
deny control such as ``DemoBot-block-hallucinated-output`` (Correctness score
below threshold) actually withhold an answer.

Grounded on the Agent Control Server 8.x contract (its published OpenAPI):
  - Register : POST {base}/api/v1/agents/initAgent
               {"agent": {"agent_name": ...}, "steps": [{"type","name"}]}
  - Attach   : POST {base}/api/v1/agents/{agent_name}/controls/{control_id}
  - Evaluate : POST {base}/api/v1/evaluation
               {"agent_name", "stage": "pre"|"post",
                "step": {"type","name","input","output"[,"context"]}}
               -> {"is_safe", "confidence", "reason",
                   "matches": [{"control_name","action",
                                "result": {"matched","confidence","message"}}]}

Auth is two-legged. The Galileo API key is first exchanged for a console access
token, which is then exchanged for a short-lived, target-bound *runtime* token;
``/api/v1/evaluation`` accepts only the runtime token as a Bearer credential.
Mirroring the official SDK's ``auto`` runtime-auth mode, an unavailable exchange
endpoint falls back to presenting the console token directly rather than failing
the turn outright.

Fully defensive: a no-op when ``GALILEO_API_KEY`` is unset or the master switch
is off, and errors are normalized into a verdict that honors
``galileo_agent_control_fail_open`` (default True — release the response and log,
because this control layer sits *after* the internal policy engine and AI
Defense).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# Re-attempt a failing runtime-token exchange no more than once per interval, so
# a tenant that has not enabled runtime tokens does not pay a wasted round-trip
# on every single turn.
_TOKEN_RETRY_COOLDOWN_SECONDS = 300.0
# Refresh cached tokens this long before they actually expire.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0
# Fallback lifetime when a token carries no parseable expiry.
_TOKEN_ASSUMED_TTL_SECONDS = 1800.0


@dataclass
class ControlVerdict:
    """Normalized outcome of one Agent Control evaluation."""

    is_safe: bool = True
    confidence: float = 0.0
    reason: Optional[str] = None
    # Names of the controls that matched, and the action each one configured.
    matched_controls: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    # Per-control explanations from the evaluators (Luna score messages etc.).
    messages: List[str] = field(default_factory=list)
    # Controls whose evaluator itself failed server-side (fail-open per the
    # Agent Control contract, surfaced for triage).
    evaluator_errors: List[str] = field(default_factory=list)
    # True when this client could not obtain a real verdict (auth/network/parse).
    errored: bool = False
    error_message: Optional[str] = None

    @property
    def should_block(self) -> bool:
        """Whether the response must be withheld.

        - A real verdict containing a ``deny`` decision always blocks.
        - ``steer`` and ``observe`` never block (they are advisory here; DemoBot
          has no re-generation loop, so a steer is recorded, not enforced).
        - On error, honor the configured fail-open / fail-closed policy.
        """
        if self.errored:
            return not settings.galileo_agent_control_fail_open
        return any(decision == "deny" for decision in self.decisions)

    @property
    def steered(self) -> bool:
        return any(decision == "steer" for decision in self.decisions)


class AgentControlError(Exception):
    """Raised for configuration problems (e.g. missing Galileo API key)."""


class AgentControlClient:
    """Synchronous client for the Galileo Agent Control evaluation API."""

    def __init__(self) -> None:
        self._base_url = (settings.galileo_agent_control_url or "").rstrip("/")
        self._timeout = settings.galileo_agent_control_timeout
        self._agent_name = settings.galileo_agent_control_agent_name
        self._step_name = settings.galileo_agent_control_step_name
        # Cached credentials: (token, expires_at_epoch).
        self._access_token: Optional[str] = None
        self._access_expires_at: float = 0.0
        self._runtime_token: Optional[str] = None
        self._runtime_expires_at: float = 0.0
        # Set when the runtime-token exchange is unavailable on this deployment;
        # holds the epoch after which we retry. 0.0 = never failed.
        self._runtime_retry_after: float = 0.0
        self._runtime_unavailable_logged = False

    # ---------------------------------------------------------------- config

    @property
    def api_key(self) -> str:
        """Galileo API key, read from the environment like the SDK does.

        Deliberately not a pydantic setting: ``backend.galileo_integration``
        already treats ``GALILEO_API_KEY`` as the single enable signal for every
        Galileo path, and reading it live keeps the two consistent.
        """
        return os.getenv("GALILEO_API_KEY", "")

    @property
    def is_configured(self) -> bool:
        return bool(
            settings.galileo_agent_control_enabled and self.api_key and self._base_url
        )

    @property
    def console_api_url(self) -> str:
        """Base URL of the Galileo *console* API that issues access tokens.

        Derived from ``GALILEO_CONSOLE_URL`` the same way the Galileo SDK does
        (``console.<host>`` -> ``api.<host>``), defaulting to the hosted API.
        """
        console = (os.getenv("GALILEO_CONSOLE_URL") or "").strip().rstrip("/")
        if not console:
            return "https://api.galileo.ai"
        if "://" not in console:
            console = f"https://{console}"
        return console.replace("://console.", "://api.", 1)

    # ------------------------------------------------------------------ auth

    @staticmethod
    def _jwt_expiry(token: str) -> Optional[float]:
        """Best-effort ``exp`` claim (epoch seconds) from a JWT, or None."""
        try:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
            exp = claims.get("exp")
            return float(exp) if exp is not None else None
        except Exception:  # noqa: BLE001 - any malformed token just means "unknown"
            return None

    def _fetch_access_token(self) -> str:
        """Exchange the Galileo API key for a console access token (cached)."""
        now = time.time()
        if self._access_token and now < self._access_expires_at:
            return self._access_token

        response = httpx.post(
            f"{self.console_api_url}/v2/login/api_key",
            json={"api_key": self.api_key},
            timeout=self._timeout,
        )
        response.raise_for_status()
        token = (response.json() or {}).get("access_token")
        if not token:
            raise AgentControlError("Galileo login returned no access_token")

        expiry = self._jwt_expiry(token) or (now + _TOKEN_ASSUMED_TTL_SECONDS)
        self._access_token = token
        self._access_expires_at = expiry - _TOKEN_REFRESH_MARGIN_SECONDS
        return token

    def _fetch_runtime_token(self, access_token: str) -> Optional[str]:
        """Mint a short-lived runtime token bound to this agent (cached).

        Returns None when the deployment cannot issue one, in which case the
        caller presents the console token instead (the SDK's ``auto`` mode).
        """
        now = time.time()
        if self._runtime_token and now < self._runtime_expires_at:
            return self._runtime_token
        if self._runtime_retry_after and now < self._runtime_retry_after:
            return None

        try:
            response = httpx.post(
                f"{self._base_url}/api/v1/auth/runtime-token-exchange",
                json={"target_type": "agent", "target_id": self._agent_name},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json() or {}
            token = data.get("token")
            if not token:
                raise AgentControlError("runtime-token exchange returned no token")
        except (httpx.HTTPError, ValueError, AgentControlError) as exc:
            self._runtime_retry_after = now + _TOKEN_RETRY_COOLDOWN_SECONDS
            if not self._runtime_unavailable_logged:
                logger.warning(
                    "Galileo Agent Control runtime-token exchange unavailable "
                    "(%s); falling back to console-token auth. Evaluation "
                    "requires a runtime token on this deployment, so controls "
                    "will not enforce until the org's runtime grant is enabled.",
                    exc,
                )
                self._runtime_unavailable_logged = True
            return None

        expiry = self._jwt_expiry(token) or (now + _TOKEN_ASSUMED_TTL_SECONDS)
        self._runtime_token = token
        self._runtime_expires_at = expiry - _TOKEN_REFRESH_MARGIN_SECONDS
        self._runtime_retry_after = 0.0
        self._runtime_unavailable_logged = False
        return token

    def _bearer_token(self) -> str:
        access_token = self._fetch_access_token()
        return self._fetch_runtime_token(access_token) or access_token

    # ------------------------------------------------------------ evaluation

    def evaluate_response(
        self,
        user_message: str,
        assistant_message: str,
        *,
        enduser_id: Optional[str] = None,
        session_id: Optional[str] = None,
        theme: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ControlVerdict:
        """Submit a generated response as a post-stage ``llm`` step.

        The step carries both sides of the turn (``input`` = the user's prompt,
        ``output`` = the generated answer) because the controls in the console
        select ``path: "*"`` and their Luna evaluators score input and output
        together — a correctness/hallucination judgement needs the question.
        """
        if not self.is_configured:
            raise AgentControlError(
                "Galileo Agent Control is not configured (set GALILEO_API_KEY "
                "and GALILEO_AGENT_CONTROL_ENABLED=True)."
            )

        context: Dict[str, Any] = {"app": settings.otel_service_name}
        if session_id:
            context["session_id"] = session_id
        if theme:
            context["theme"] = theme
        if model:
            context["model"] = model
        if enduser_id:
            context["user"] = enduser_id

        payload = {
            "agent_name": self._agent_name,
            "stage": "post",
            "step": {
                "type": "llm",
                "name": self._step_name,
                "input": user_message,
                "output": assistant_message,
                "context": context,
            },
        }

        try:
            token = self._bearer_token()
        except (httpx.HTTPError, ValueError, AgentControlError) as exc:
            logger.warning("Galileo Agent Control auth failed: %s", exc)
            return ControlVerdict(errored=True, error_message=f"auth: {exc}")

        try:
            response = httpx.post(
                f"{self._base_url}/api/v1/evaluation",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "accept": "application/json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = self._safe_error_detail(exc.response)
            # A rejected credential is usually a stale cached token; drop both
            # so the next turn re-authenticates from the API key.
            if exc.response.status_code in (401, 403):
                self._invalidate_tokens()
            logger.warning(
                "Galileo Agent Control evaluation HTTP %s: %s",
                exc.response.status_code,
                detail,
            )
            return ControlVerdict(
                errored=True,
                error_message=f"HTTP {exc.response.status_code}: {detail}",
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Galileo Agent Control evaluation failed: %s", exc)
            return ControlVerdict(errored=True, error_message=str(exc))

        return self._parse_response(data)

    def _invalidate_tokens(self) -> None:
        self._access_token = None
        self._access_expires_at = 0.0
        self._runtime_token = None
        self._runtime_expires_at = 0.0

    @staticmethod
    def _parse_response(data: Dict[str, Any]) -> ControlVerdict:
        if not isinstance(data, dict) or "is_safe" not in data:
            return ControlVerdict(
                errored=True, error_message="Malformed response: missing is_safe"
            )

        matched_controls: List[str] = []
        decisions: List[str] = []
        messages: List[str] = []
        for match in data.get("matches") or []:
            if not isinstance(match, dict):
                continue
            name = match.get("control_name")
            if name:
                matched_controls.append(str(name))
            action = match.get("action")
            if action:
                decisions.append(str(action).lower())
            result = match.get("result")
            if isinstance(result, dict) and result.get("message"):
                messages.append(str(result["message"]))

        evaluator_errors = [
            str(err.get("control_name") or "unknown")
            for err in (data.get("errors") or [])
            if isinstance(err, dict)
        ]

        return ControlVerdict(
            is_safe=bool(data.get("is_safe", True)),
            confidence=float(data.get("confidence") or 0.0),
            reason=data.get("reason"),
            matched_controls=matched_controls,
            decisions=decisions,
            messages=messages,
            evaluator_errors=evaluator_errors,
        )

    @staticmethod
    def _safe_error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                # Agent Control returns RFC 9457 problem documents.
                for key in ("detail", "title", "message"):
                    if body.get(key):
                        return str(body[key])
        except ValueError:
            pass
        return response.text[:200]

    # ---------------------------------------------------------- registration

    def register_agent(
        self, *, description: Optional[str] = None, version: str = "3.0.0"
    ) -> Dict[str, Any]:
        """Idempotently register this agent + its ``llm`` step with the server.

        Not called on the request path — the agent is registered once (see
        ``scripts/demo/register_agent_control.py``) and controls are attached to
        it in the console. Exposed here so setup and tests share one contract.
        """
        if not self.is_configured:
            raise AgentControlError("Galileo Agent Control is not configured.")

        payload = {
            "agent": {
                "agent_name": self._agent_name,
                "agent_description": description
                or "DemoBot multi-theme advisory assistant",
                "agent_version": version,
                "agent_metadata": {"app": settings.otel_service_name},
            },
            "steps": [
                {
                    "type": "llm",
                    "name": self._step_name,
                    "description": "DemoBot synthesizer / domain agent response",
                }
            ],
            "conflict_mode": "overwrite",
        }
        response = httpx.post(
            f"{self._base_url}/api/v1/agents/initAgent",
            json=payload,
            headers={"Authorization": f"Bearer {self._fetch_access_token()}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json() or {}

    def attach_control(self, control_id: int) -> Dict[str, Any]:
        """Attach an existing console control to this agent (idempotent)."""
        if not self.is_configured:
            raise AgentControlError("Galileo Agent Control is not configured.")

        response = httpx.post(
            f"{self._base_url}/api/v1/agents/{self._agent_name}/controls/{control_id}",
            headers={"Authorization": f"Bearer {self._fetch_access_token()}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json() or {}

    def list_controls(self) -> List[Dict[str, Any]]:
        """Effective control set for this agent (direct + policy + bindings)."""
        if not self.is_configured:
            raise AgentControlError("Galileo Agent Control is not configured.")

        response = httpx.get(
            f"{self._base_url}/api/v1/agents/{self._agent_name}/controls",
            headers={"Authorization": f"Bearer {self._fetch_access_token()}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json() or {}
        return list(data.get("controls") or [])


# Module-level singleton, mirrors other services in this package.
agent_control_client = AgentControlClient()
