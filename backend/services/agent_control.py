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

Two transports, selected by ``galileo_agent_control_execution``:

  - **server** — ``POST /api/v1/evaluation``, the Agent Control server resolves
    and runs the agent's effective control set.
  - **client** — the ``execution: "sdk"`` equivalent: read the agent's control
    definitions from the management API (which needs only the console token) and
    run their Luna conditions here, scoring through the console API's
    ``POST /scorers/invoke``. This exists because a deployment whose org lacks a
    runtime-token grant cannot use the server path at all: the exchange returns
    502 and ``/evaluation`` then rejects the console token with
    ``401 Token is missing the "iat" claim``. ``auto`` prefers the server and
    falls back here, so a missing grant costs a transport, not the guardrail.

The local comparison (``score_matches`` / ``coerce_number``) is ported verbatim
from ``agent_control_evaluator_galileo`` so both transports decide identically —
including the rule that a numeric operator over a boolean scorer is an error, not
a 0/1 comparison.

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

# The only evaluator DemoBot can run client-side. Anything else (a custom
# agent-scoped evaluator, regex/list/json/sql) is left to server execution —
# guessing at its semantics would be worse than reporting it unevaluated.
_LOCAL_EVALUATOR = "galileo.luna"


def coerce_number(value: Any) -> Optional[float]:
    """Numeric view of a JSON scalar, or None when it has none.

    Ported verbatim from ``agent_control_evaluator_galileo.luna.config`` so
    client-side execution decides exactly what the server would. The bool rule
    is load-bearing, not an oversight: a boolean scorer (``correctness`` emits
    true/false) has no numeric reading, so a numeric operator against it is a
    configuration error that must surface, not silently compare as 0/1.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _contains(score: Any, threshold: Any) -> bool:
    """Membership test matching the upstream Luna evaluator's ``contains``."""
    if threshold is None:
        return False
    if isinstance(score, str):
        return str(threshold) in score
    if isinstance(score, list):
        return threshold in score
    if isinstance(score, dict):
        return threshold in score.values()
    return False


def score_matches(score: Any, operator: str, threshold: Any) -> bool:
    """Apply a control's local comparison to a raw Luna score.

    Mirrors ``LunaEvaluator._score_matches``. Raises ``ValueError`` for a
    numeric operator against a non-numeric score — the same failure the server
    reports as an evaluator error (which fails open), and the reason a
    ``lt 0.5`` control over the boolean ``correctness`` scorer can never match.
    """
    if operator == "any":
        return bool(score)
    if operator == "eq":
        return score == threshold
    if operator == "ne":
        return score != threshold
    if operator == "contains":
        return _contains(score, threshold)

    score_number = coerce_number(score)
    threshold_number = coerce_number(threshold)
    if score_number is None:
        raise ValueError(f"Luna score {score!r} is not numeric")
    if threshold_number is None:
        raise ValueError(f"Luna threshold {threshold!r} is not numeric")

    if operator == "gt":
        return score_number > threshold_number
    if operator == "gte":
        return score_number >= threshold_number
    if operator == "lt":
        return score_number < threshold_number
    if operator == "lte":
        return score_number <= threshold_number

    raise ValueError(f"Unsupported Luna operator: {operator}")


def _confidence_from_score(score: Any) -> float:
    """Upstream's confidence mapping (bool -> 1.0/0.0, 0-1 float as-is)."""
    if isinstance(score, bool):
        return 1.0 if score else 0.0
    number = coerce_number(score)
    if number is not None and 0.0 <= number <= 1.0:
        return number
    return 1.0


def _scope_matches(scope: Dict[str, Any], *, stage: str, step_type: str, step_name: str) -> bool:
    """Whether a control's scope covers this step. Absent key = applies to all."""
    stages = scope.get("stages")
    if stages and stage not in stages:
        return False
    step_types = scope.get("step_types")
    if step_types and step_type not in step_types:
        return False
    step_names = scope.get("step_names")
    if step_names and step_name not in step_names:
        return False
    return True


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
    # Which transport produced this verdict: "server" (POST /api/v1/evaluation)
    # or "client" (control definitions evaluated in-process). Recorded on the
    # governance event so a block can be attributed to the right path.
    transport: Optional[str] = None

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
        # Cached control definitions for client-side execution:
        # [{"id", "name", <definition>}], plus the epoch it goes stale.
        self._controls: Optional[List[Dict[str, Any]]] = None
        self._controls_expires_at: float = 0.0
        self._server_control_locally_logged: set = set()

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

        Transport follows ``galileo_agent_control_execution``: ``auto`` (default)
        prefers the server and falls back to client-side execution when the
        deployment cannot mint a runtime token, so a missing runtime grant
        degrades the transport rather than the guardrail.
        """
        if not self.is_configured:
            raise AgentControlError(
                "Galileo Agent Control is not configured (set GALILEO_API_KEY "
                "and GALILEO_AGENT_CONTROL_ENABLED=True)."
            )

        mode = (settings.galileo_agent_control_execution or "auto").strip().lower()
        if mode == "client":
            return self._evaluate_client_side(user_message, assistant_message)

        verdict = self._evaluate_server_side(
            user_message,
            assistant_message,
            enduser_id=enduser_id,
            session_id=session_id,
            theme=theme,
            model=model,
        )
        if mode == "server" or not verdict.errored:
            return verdict

        # The server path could not produce a verdict (typically: this org has no
        # runtime-token grant, so /evaluation rejects the console token). Evaluate
        # the same controls locally rather than failing the guardrail open.
        local = self._evaluate_client_side(user_message, assistant_message)
        if local.errored and verdict.error_message:
            # Keep the server's diagnosis; it is the actionable one.
            local.error_message = f"{verdict.error_message}; client: {local.error_message}"
        return local

    def _evaluate_server_side(
        self,
        user_message: str,
        assistant_message: str,
        *,
        enduser_id: Optional[str] = None,
        session_id: Optional[str] = None,
        theme: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ControlVerdict:
        """Evaluate via the Agent Control server (``POST /api/v1/evaluation``)."""
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
            return ControlVerdict(
                errored=True, error_message=f"auth: {exc}", transport="server"
            )

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
                transport="server",
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Galileo Agent Control evaluation failed: %s", exc)
            return ControlVerdict(
                errored=True, error_message=str(exc), transport="server"
            )

        verdict = self._parse_response(data)
        verdict.transport = "server"
        return verdict

    # ------------------------------------------------- client-side execution

    def _load_controls(self) -> List[Dict[str, Any]]:
        """This agent's control definitions, cached for the configured TTL.

        Mirrors an Agent Control SDK's local control set. A refresh failure
        serves the last-known definitions rather than failing the turn; only a
        cold cache with no definitions is an error.
        """
        now = time.time()
        if self._controls is not None and now < self._controls_expires_at:
            return self._controls

        try:
            response = httpx.get(
                f"{self._base_url}/api/v1/agents/{self._agent_name}/controls",
                headers={"Authorization": f"Bearer {self._fetch_access_token()}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json() or {}
        except (httpx.HTTPError, ValueError, AgentControlError) as exc:
            if self._controls is not None:
                logger.warning(
                    "Galileo Agent Control definition refresh failed (%s); "
                    "reusing the %d cached control(s).",
                    exc,
                    len(self._controls),
                )
                # Back off before hammering a broken endpoint every turn.
                self._controls_expires_at = now + _TOKEN_RETRY_COOLDOWN_SECONDS
                return self._controls
            raise

        controls: List[Dict[str, Any]] = []
        for entry in body.get("controls") or []:
            if not isinstance(entry, dict):
                continue
            # This endpoint nests the definition under "control"; the
            # single-control endpoint uses "data". Accept either.
            definition = entry.get("control") or entry.get("data") or {}
            if isinstance(definition, dict) and definition:
                controls.append(
                    {
                        "id": entry.get("id"),
                        "name": entry.get("name") or str(entry.get("id")),
                        "definition": definition,
                    }
                )

        self._controls = controls
        self._controls_expires_at = now + max(
            0.0, settings.galileo_agent_control_refresh_seconds
        )
        logger.info(
            "Galileo Agent Control loaded %d control definition(s) for agent %s",
            len(controls),
            self._agent_name,
        )
        return controls

    def _log_server_control_run_locally(self, name: str) -> None:
        """Announce once that a server-scoped control is being run in-process."""
        if name in self._server_control_locally_logged:
            return
        self._server_control_locally_logged.add(name)
        logger.warning(
            "Galileo Agent Control '%s' is declared execution=server but is being "
            "evaluated in-process, because this deployment cannot mint a runtime "
            "token. The official engine would skip it; DemoBot runs it so the "
            "guardrail still applies.",
            name,
        )

    def _invoke_scorer(
        self,
        config: Dict[str, Any],
        *,
        query: str,
        response_text: str,
        memo: Optional[Dict[Any, Any]] = None,
    ) -> Any:
        """Run one Luna scorer through the console API's ``/scorers/invoke``.

        Returns the raw score. Raises ``AgentControlError`` when the scorer
        itself could not produce one — the client-side equivalent of the
        server's evaluator error, which fails open rather than matching.

        ``memo`` is a per-evaluation cache keyed on (scorer, payload). A control
        that ORs several ``contains`` leaves over the SAME scorer — the natural
        shape for a list-valued scorer such as Output PII (SLM), which returns
        ``["ssn", "name", …]`` — then costs one judge call instead of one per
        category. Scoped to a single evaluation on purpose: caching verdicts
        across turns would mean stale safety decisions.
        """
        memo_key = (
            config.get("scorer_id"),
            config.get("scorer_version_id"),
            config.get("scorer_label"),
            query,
            response_text,
        )
        if memo is not None and memo_key in memo:
            cached = memo[memo_key]
            if isinstance(cached, Exception):
                raise cached
            return cached
        payload: Dict[str, Any] = {
            "inputs": {"query": query, "response": response_text},
        }
        for field_name in ("scorer_id", "scorer_version_id", "scorer_label"):
            if config.get(field_name):
                payload[field_name] = config[field_name]

        timeout = float(config.get("timeout_ms") or 0) / 1000.0 or self._timeout
        try:
            http_response = httpx.post(
                f"{self.console_api_url}/scorers/invoke",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._fetch_access_token()}",
                    "Content-Type": "application/json",
                    "accept": "application/json",
                },
                timeout=max(timeout, self._timeout),
            )
            http_response.raise_for_status()
            body = http_response.json() or {}
            if body.get("status") != "success":
                raise AgentControlError(
                    f"scorer {body.get('scorer_label') or config.get('scorer_label')} "
                    f"{body.get('status')}: {body.get('error_message')}"
                )
        except Exception as exc:  # noqa: BLE001 - memoized and re-raised as-is
            if memo is not None:
                memo[memo_key] = exc
            raise

        score = body.get("score")
        if memo is not None:
            memo[memo_key] = score
        return score

    def _evaluate_condition(
        self,
        node: Dict[str, Any],
        *,
        query: str,
        response_text: str,
        memo: Dict[Any, Any],
    ) -> bool:
        """Evaluate a control's condition tree.

        Composite ``and``/``or``/``not`` nodes recurse and short-circuit like the
        official engine; a leaf is ``selector`` + ``evaluator``. Only the
        ``galileo.luna`` evaluator is supported client-side — anything else
        raises so the caller records it as unevaluated rather than guessing.
        """
        if node.get("and"):
            return all(
                self._evaluate_condition(
                    child, query=query, response_text=response_text, memo=memo
                )
                for child in node["and"]
            )
        if node.get("or"):
            return any(
                self._evaluate_condition(
                    child, query=query, response_text=response_text, memo=memo
                )
                for child in node["or"]
            )
        if node.get("not"):
            return not self._evaluate_condition(
                node["not"], query=query, response_text=response_text, memo=memo
            )

        evaluator = node.get("evaluator") or {}
        if evaluator.get("name") != _LOCAL_EVALUATOR:
            raise AgentControlError(
                f"{evaluator.get('name') or 'empty condition'} not supported client-side"
            )
        config = evaluator.get("config") or {}
        score = self._invoke_scorer(
            config, query=query, response_text=response_text, memo=memo
        )
        matched = score_matches(
            score, config.get("operator") or "any", config.get("threshold")
        )
        # Remember the score that decided, for the verdict message.
        memo.setdefault("_last_scores", []).append((score, config))
        return matched

    def _evaluate_client_side(
        self, user_message: str, assistant_message: str
    ) -> ControlVerdict:
        """Evaluate this agent's controls in-process (``execution: "sdk"``).

        Only leaf ``galileo.luna`` conditions are supported; composite
        (and/or/not) conditions and other evaluators are reported as unevaluated
        instead of being guessed at.

        Deliberate deviation from the official engine, which skips any control
        whose ``execution`` differs from its context: DemoBot also runs
        ``execution: "server"`` controls here, because the alternative on a
        deployment with no runtime-token grant is running nothing at all. It is
        logged once per process so a control marked *server* being enforced by
        the app is never a silent surprise. Flipping the control itself to
        ``sdk`` would be worse — it would drop out of the server path we want to
        inherit for free once the grant exists.
        """
        try:
            controls = self._load_controls()
        except (httpx.HTTPError, ValueError, AgentControlError) as exc:
            logger.warning("Galileo Agent Control definition load failed: %s", exc)
            return ControlVerdict(
                errored=True, error_message=f"controls: {exc}", transport="client"
            )

        verdict = ControlVerdict(transport="client")
        evaluated = 0

        for control in controls:
            definition = control["definition"]
            name = control["name"]
            if definition.get("enabled") is False:
                continue
            if not _scope_matches(
                definition.get("scope") or {},
                stage="post",
                step_type="llm",
                step_name=self._step_name,
            ):
                continue

            condition = definition.get("condition") or {}
            if not condition:
                verdict.evaluator_errors.append(f"{name}: empty condition")
                continue

            if (definition.get("execution") or "server") != "sdk":
                self._log_server_control_run_locally(name)

            # One memo per control: leaves that share a scorer and payload — the
            # natural shape when OR-ing `contains` over a list-valued scorer —
            # cost a single judge call.
            memo: Dict[Any, Any] = {}
            try:
                matched = self._evaluate_condition(
                    condition,
                    query=user_message,
                    response_text=assistant_message,
                    memo=memo,
                )
            except (httpx.HTTPError, ValueError, AgentControlError) as exc:
                # Evaluator error: never a match, surfaced for triage.
                logger.warning("Galileo Agent Control '%s' evaluator failed: %s", name, exc)
                verdict.evaluator_errors.append(f"{name}: {exc}")
                continue

            evaluated += 1
            if not matched:
                continue

            decision = str((definition.get("action") or {}).get("decision") or "observe").lower()
            scores = memo.get("_last_scores") or []
            score, config = scores[-1] if scores else (None, {})
            verdict.matched_controls.append(name)
            verdict.decisions.append(decision)
            verdict.confidence = max(verdict.confidence, _confidence_from_score(score))
            verdict.messages.append(
                f"{name}: score {score!r} {config.get('operator')} "
                f"{config.get('threshold')!r}"
            )

        verdict.is_safe = not verdict.matched_controls
        if evaluated == 0 and not verdict.matched_controls:
            # Nothing could actually be judged — report it as an error so the
            # fail-open/fail-closed policy decides, rather than claiming "safe".
            verdict.errored = True
            verdict.error_message = (
                "; ".join(verdict.evaluator_errors)
                or f"no post/llm controls attached to agent {self._agent_name}"
            )
        return verdict

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

        errors = [err for err in (data.get("errors") or []) if isinstance(err, dict)]
        evaluator_errors = [str(err.get("control_name") or "unknown") for err in errors]

        # A deny control whose evaluator ERRORED is reported under ``errors``,
        # not ``matches``, but the engine still flips is_safe to False for it
        # ("fail closed if a deny control errored" — agent_control_engine.core).
        # Reading only ``matches`` would take that as a clean pass, which is
        # exactly the response a mis-typed operator produces: neither blocked nor
        # subject to the fail-open policy. Treat it as errored so the configured
        # policy decides.
        is_safe = bool(data.get("is_safe", True))
        if not is_safe and not any(d == "deny" for d in decisions) and errors:
            denied = [
                str(err.get("control_name") or "unknown")
                for err in errors
                if str(err.get("action") or "").lower() == "deny"
            ] or evaluator_errors
            return ControlVerdict(
                is_safe=False,
                confidence=float(data.get("confidence") or 0.0),
                reason=data.get("reason"),
                evaluator_errors=evaluator_errors,
                errored=True,
                error_message=(
                    "deny control evaluator errored: " + ", ".join(denied)
                ),
            )

        return ControlVerdict(
            is_safe=is_safe,
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

    def validate_control_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Dry-run a control definition (``POST /api/v1/controls/validate``)."""
        if not self.is_configured:
            raise AgentControlError("Galileo Agent Control is not configured.")

        response = httpx.post(
            f"{self._base_url}/api/v1/controls/validate",
            json={"data": data},
            headers={
                "Authorization": f"Bearer {self._fetch_access_token()}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json() or {}

    def set_control_data(self, control_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace a control's definition (``PUT /api/v1/controls/{id}/data``).

        ``PATCH /api/v1/controls/{id}`` only accepts name/enabled, so editing a
        condition — e.g. correcting an operator that can never match its
        scorer's output type — has to go through this full replace.
        """
        if not self.is_configured:
            raise AgentControlError("Galileo Agent Control is not configured.")

        response = httpx.put(
            f"{self._base_url}/api/v1/controls/{control_id}/data",
            json={"data": data},
            headers={
                "Authorization": f"Bearer {self._fetch_access_token()}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        # A changed definition invalidates the client-side cache.
        self._controls = None
        self._controls_expires_at = 0.0
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
