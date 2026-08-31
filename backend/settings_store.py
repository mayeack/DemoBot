"""Runtime-mutable app settings, persisted in a single ``app_settings`` row.

Holds the local log directory and the list of Splunk HEC destinations. This is
DemoBot's analog of ThreatGenerator's active-config store. Tokens are kept in
the JSON blob (local SQLite, gitignored) and stripped by ``mask`` before they
ever reach an API response.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.database.db import get_db_context
from backend.hec.config import HECConfig
from backend.hec.runtime import hec_runtime
from backend.models.db_models import AppSettings

logger = logging.getLogger(__name__)

_ROW_ID = 1
_DEFAULTS: Dict[str, Any] = {
    "logs_directory": "logs",
    "hec_destinations": [],
    "emit_model": {"enabled": False, "model_name": "", "random": False},
    # Runtime override of the active LLM provider (empty = use .env/config default).
    "ai_provider": {"provider": "", "model": ""},
    # Per-provider access credentials/config entered via the Settings UI. Secrets
    # are stored here in the local (gitignored) SQLite blob, same as HEC tokens,
    # and are NEVER returned by the API (presence-only on read).
    "ai_provider_creds": {},
    # Whether the AI Defense connection accepts config.enabled_rules. Discovered
    # at runtime (a connection with an SCC policy bound rejects them with HTTP
    # 400) and persisted so the wasted 400+retry isn't re-paid on every restart.
    "ai_defense_enabled_rules_supported": True,
    # Per-integration credentials (Cisco AI Defense, Splunk Agent Observability)
    # entered via the Settings UI. Same storage contract as ai_provider_creds:
    # local gitignored SQLite, never returned by the API. Collector-consumed keys
    # (SPLUNK_REALM/SPLUNK_ACCESS_TOKEN/...) are deliberately NOT kept here — they
    # live in .env only, so the two planes can't diverge.
    "integration_creds": {},
}
_ID_RE = re.compile(r"[^a-z0-9-]+")

# Supported LLM providers and the ``settings`` attribute that holds each one's
# model id. Keep in sync with backend/agents/llm.py::get_chat_model and
# backend/services/ai_client.py::get_ai_client.
AI_PROVIDER_CHOICES: List[str] = ["anthropic", "bedrock", "openai", "ollama", "nvidia"]
_PROVIDER_MODEL_ATTR: Dict[str, str] = {
    "anthropic": "anthropic_model",
    "bedrock": "bedrock_model_id",
    "openai": "openai_model",
    "ollama": "ollama_model",
    "nvidia": "nvidia_model",
}


class _CredField:
    """One access field surfaced per provider / integration in the Settings UI.

    ``secret`` fields are masked on read (presence only — never the value) and only
    overwritten on write when a non-empty value is supplied. Each field applies to
    a live ``settings`` attribute and/or a process env var (for the boto3 chain).

    ``boolean`` renders as a checkbox instead of a text input. Booleans are the one
    exception to blank-keeps-existing: a checkbox always reports its state.

    ``env_file`` means the value's consumer is a DIFFERENT PROCESS (the OTel
    collector, or run.sh before it re-execs the app), which reads ``.env`` directly.
    Those fields are written to ``.env`` and are NOT mirrored into the settings blob
    — ``.env`` stays their single source of truth. ``restart`` names the process that
    must be restarted before such a change takes effect ("collector" or "app")."""

    def __init__(self, key, label, *, secret=False, boolean=False, settings_attr=None,
                 env=None, env_file=False, restart="", placeholder="", help=""):
        self.key = key
        self.label = label
        self.secret = secret
        self.boolean = boolean
        self.settings_attr = settings_attr
        self.env = env
        self.env_file = env_file
        self.restart = restart
        self.placeholder = placeholder
        self.help = help


# Access fields per provider (the "Model" id is handled separately by the dropdown).
_PROVIDER_FIELDS: Dict[str, List[_CredField]] = {
    "anthropic": [
        _CredField("api_key", "API key", secret=True, settings_attr="anthropic_api_key",
                   placeholder="sk-ant-…"),
    ],
    "openai": [
        _CredField("api_key", "API key", secret=True, settings_attr="openai_api_key",
                   placeholder="sk-…"),
        _CredField("base_url", "Base URL", settings_attr="openai_base_url",
                   placeholder="https://api.openai.com/v1"),
    ],
    "bedrock": [
        _CredField("region", "AWS region", settings_attr="aws_region", env="AWS_DEFAULT_REGION",
                   placeholder="us-east-1"),
        _CredField("access_key_id", "AWS access key ID", secret=True, env="AWS_ACCESS_KEY_ID",
                   placeholder="AKIA…"),
        _CredField("secret_access_key", "AWS secret access key", secret=True,
                   env="AWS_SECRET_ACCESS_KEY"),
    ],
    "ollama": [
        _CredField("base_url", "Base URL", settings_attr="ollama_base_url",
                   placeholder="http://localhost:11434"),
    ],
    "nvidia": [
        _CredField("api_key", "API key", secret=True, settings_attr="nvidia_api_key",
                   placeholder="nvapi-…"),
        _CredField("base_url", "Base URL", settings_attr="nvidia_base_url",
                   placeholder="https://integrate.api.nvidia.com/v1"),
    ],
}


# ---------------------------------------------------------------------------
# Integration credentials (Cisco AI Defense / Splunk Observability Cloud)
# ---------------------------------------------------------------------------
# Same _CredField vocabulary as _PROVIDER_FIELDS, one entry per Settings card
# group. Scope is deliberately CREDENTIALS + IDENTITY only: timeouts, fail-open
# posture, enabled-rule lists and Agent Control tuning stay in .env, where they
# are annotated and rarely change per-demo.
INTEGRATION_CHOICES: List[str] = ["ai_defense", "splunk_o11y", "agent_observability"]

_INTEGRATION_FIELDS: Dict[str, List[_CredField]] = {
    # Applies live: AIDefenseClient.reconfigure() re-reads the settings singleton.
    "ai_defense": [
        _CredField("enabled", "Enabled", boolean=True, settings_attr="ai_defense_enabled",
                   help="Master switch. The per-chat toggle is ignored when this is off."),
        _CredField("api_key", "Inspection API key", secret=True, settings_attr="ai_defense_api_key",
                   placeholder="paste the SCC connection key"),
        _CredField("region", "Region", settings_attr="ai_defense_region",
                   placeholder="us"),
        _CredField("endpoint", "Endpoint override", settings_attr="ai_defense_endpoint",
                   placeholder="https://us.api.inspect.aidefense.security.cisco.com"),
    ],
    # .env only — every one of these is read by a DIFFERENT process (the OTel
    # collector via run-collector.sh, or run.sh deciding whether to re-exec the app
    # under opentelemetry-instrument). Nothing in the app reads them, so there is
    # nothing to apply live.
    "splunk_o11y": [
        _CredField("realm", "Realm", env="SPLUNK_REALM", env_file=True, restart="collector",
                   placeholder="us1"),
        _CredField("access_token", "Ingest access token", secret=True, env="SPLUNK_ACCESS_TOKEN",
                   env_file=True, restart="collector",
                   help="INGEST authorization. Not the API token — they are different."),
        _CredField("api_token", "API token", secret=True, env="SPLUNK_API_TOKEN", env_file=True,
                   help="Read-only API token used by the observability regression test."),
        _CredField("otlp_endpoint", "OTLP endpoint", env="OTEL_EXPORTER_OTLP_ENDPOINT",
                   env_file=True, restart="app", placeholder="http://localhost:4317"),
    ],
    # Applies live: the SDK path builds a fresh GalileoLogger per turn and reads
    # these from os.environ at call time.
    "agent_observability": [
        _CredField("agent_control_enabled", "Agent Control enabled", boolean=True,
                   settings_attr="galileo_agent_control_enabled",
                   help="Master switch for the per-chat Agent Observability Controls toggle."),
        _CredField("api_key", "API key", secret=True, env="GALILEO_API_KEY",
                   placeholder="paste the console API key",
                   help="Also the single enable signal for trace logging."),
        _CredField("console_url", "Console URL", env="GALILEO_CONSOLE_URL",
                   placeholder="https://console.multitenant.galileocloud.io"),
        _CredField("project", "Project", env="GALILEO_PROJECT", placeholder="YeackBot"),
        _CredField("log_stream", "Log stream", env="GALILEO_LOG_STREAM", placeholder="default"),
    ],
}


def _field_current(field: "_CredField") -> str:
    from backend.config import settings

    if field.settings_attr:
        cur = getattr(settings, field.settings_attr, "")
    elif field.env:
        cur = os.environ.get(field.env, "")
    else:
        return ""
    if field.boolean:
        return "true" if _as_bool(cur) else "false"
    return cur or ""


_TRUE = {"1", "true", "yes", "on"}


def _as_bool(value: Any) -> bool:
    """Coerce a checkbox / .env / pydantic value to a bool. Mirrors the string forms
    pydantic-settings accepts, so a value round-trips through .env unchanged."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load() -> Dict[str, Any]:
    """Return the persisted settings, seeding defaults on first run."""
    with get_db_context() as db:
        row = db.query(AppSettings).filter(AppSettings.id == _ROW_ID).first()
        if row is None:
            row = AppSettings(id=_ROW_ID, data=dict(_DEFAULTS))
            db.add(row)
            db.commit()
            return dict(_DEFAULTS)
        data = dict(_DEFAULTS)
        data.update(row.data or {})
        return data


def _persist(data: Dict[str, Any]) -> None:
    with get_db_context() as db:
        row = db.query(AppSettings).filter(AppSettings.id == _ROW_ID).first()
        if row is None:
            row = AppSettings(id=_ROW_ID, data=data)
            db.add(row)
        else:
            row.data = data  # reassign so SQLAlchemy tracks the JSON change
        db.commit()


# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
def get_logs_directory() -> str:
    return load().get("logs_directory") or "logs"


def set_logs_directory(path: str) -> str:
    path = (path or "").strip() or "logs"
    data = load()
    data["logs_directory"] = path
    _persist(data)
    try:
        from backend.logging.governance_logger import governance_logger
        governance_logger.set_logs_directory(path)
    except Exception:
        logger.exception("failed to apply logs_directory at runtime")
    return path


# ---------------------------------------------------------------------------
# AI Defense enabled_rules discovery
# ---------------------------------------------------------------------------
def get_ai_defense_enabled_rules_supported() -> bool:
    return bool(load().get("ai_defense_enabled_rules_supported", True))


def set_ai_defense_enabled_rules_supported(supported: bool) -> bool:
    data = load()
    data["ai_defense_enabled_rules_supported"] = bool(supported)
    _persist(data)
    return bool(supported)


# ---------------------------------------------------------------------------
# Demo model-name emission override
# ---------------------------------------------------------------------------
def get_emit_model() -> Dict[str, Any]:
    cfg = load().get("emit_model") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "model_name": cfg.get("model_name") or "",
        "random": bool(cfg.get("random", False)),
    }


def set_emit_model(enabled: bool, model_name: str, random_emit: bool) -> Dict[str, Any]:
    cfg = {
        "enabled": bool(enabled),
        "model_name": (model_name or "").strip(),
        "random": bool(random_emit),
    }
    data = load()
    data["emit_model"] = cfg
    _persist(data)
    try:
        from backend.model_emitter import model_emitter
        model_emitter.configure(
            enabled=cfg["enabled"], model_name=cfg["model_name"], random_emit=cfg["random"]
        )
    except Exception:
        logger.exception("failed to apply emit_model at runtime")
    return cfg


# ---------------------------------------------------------------------------
# Active LLM provider selection
# ---------------------------------------------------------------------------
def get_ai_provider() -> Dict[str, Any]:
    """Return the LIVE provider/model in effect plus the per-provider model map.

    Reads the runtime ``settings`` singleton (which reflects .env plus any
    persisted UI override applied at startup), so the UI always shows what is
    actually being used — not just what is stored."""
    from backend.config import settings

    provider = (settings.ai_provider or "anthropic").lower()
    models = {p: (getattr(settings, attr, "") or "") for p, attr in _PROVIDER_MODEL_ATTR.items()}
    return {
        "provider": provider,
        "model": models.get(provider, ""),
        "choices": list(AI_PROVIDER_CHOICES),
        "models": models,
    }


def _apply_ai_provider(provider: str, model: str = "") -> None:
    """Apply the provider/model to the live settings singleton and drop the LLM
    client caches so the next chat turn picks it up (no restart)."""
    from backend.config import settings

    settings.ai_provider = provider
    if model:
        setattr(settings, _PROVIDER_MODEL_ATTR[provider], model)
    try:
        from backend.agents import llm
        llm.clear_caches()
    except Exception:
        logger.exception("failed to clear LLM caches after provider change")


def set_ai_provider(provider: str, model: str = "") -> Dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in _PROVIDER_MODEL_ATTR:
        raise ValueError(f"unknown provider: {provider}")
    model = (model or "").strip()
    data = load()
    data["ai_provider"] = {"provider": provider, "model": model}
    _persist(data)
    _apply_ai_provider(provider, model)
    return get_ai_provider()


def apply_ai_provider_from_store() -> None:
    """Startup hook: apply any persisted provider override over the .env default."""
    cfg = load().get("ai_provider") or {}
    provider = (cfg.get("provider") or "").strip().lower()
    if provider in _PROVIDER_MODEL_ATTR:
        _apply_ai_provider(provider, (cfg.get("model") or "").strip())


# ---------------------------------------------------------------------------
# Per-provider access credentials (API keys etc.) — secrets never leave the box
# ---------------------------------------------------------------------------
def get_provider_fields() -> Dict[str, List[Dict[str, Any]]]:
    """Per-provider access-field metadata for the Settings UI.

    Secret fields report ONLY ``present`` (bool) — never the value, not even a
    suffix — so no secret is ever exposed. Non-secret fields (base URL, region)
    return their current value so the field can prefill."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for provider, fields in _PROVIDER_FIELDS.items():
        items: List[Dict[str, Any]] = []
        for f in fields:
            cur = _field_current(f)
            item = {
                "key": f.key,
                "label": f.label,
                "secret": f.secret,
                "placeholder": f.placeholder,
                "present": bool(cur),
            }
            if not f.secret:
                item["value"] = cur
            items.append(item)
        out[provider] = items
    return out


def set_provider_creds(provider: str, fields: Dict[str, str]) -> None:
    """Apply + persist provider access fields. Blank values are ignored (a blank
    secret keeps the existing one), so a save never accidentally wipes a key."""
    from backend.config import settings

    provider = (provider or "").strip().lower()
    specs = _PROVIDER_FIELDS.get(provider)
    if not specs or not fields:
        return

    data = load()
    store = dict(data.get("ai_provider_creds") or {})
    pstore = dict(store.get(provider) or {})
    applied: List[str] = []
    for f in specs:
        if f.key not in fields:
            continue
        val = (fields.get(f.key) or "").strip()
        if not val:  # blank = keep existing (never wipe)
            continue
        if f.settings_attr:
            setattr(settings, f.settings_attr, val)
        if f.env:
            os.environ[f.env] = val
        pstore[f.key] = val
        applied.append(f.key)

    if applied:
        store[provider] = pstore
        data["ai_provider_creds"] = store
        _persist(data)
        # Log field NAMES only — never values.
        logger.info("applied %s access fields: %s", provider, ", ".join(applied))
        try:
            from backend.agents import llm
            llm.clear_caches()  # rebuild provider clients with the new creds
        except Exception:
            logger.exception("failed to clear LLM caches after credential change")


def apply_provider_creds_from_store() -> None:
    """Startup hook: apply any persisted provider creds over the .env defaults."""
    from backend.config import settings

    store = load().get("ai_provider_creds") or {}
    for provider, fields in _PROVIDER_FIELDS.items():
        saved = store.get(provider) or {}
        for f in fields:
            val = (saved.get(f.key) or "").strip()
            if not val:
                continue
            if f.settings_attr:
                setattr(settings, f.settings_attr, val)
            if f.env:
                os.environ[f.env] = val


# ---------------------------------------------------------------------------
# Integration credentials — Cisco AI Defense / Splunk Observability Cloud
# ---------------------------------------------------------------------------
def _env_path() -> Path:
    from backend.config import BASE_DIR

    return Path(BASE_DIR) / ".env"


def _write_env_key(key: str, value: str) -> None:
    """Set ``key`` in .env, replacing in place and collapsing any duplicates.

    A duplicate line is not cosmetic: backend/config.py takes the FIRST match while
    the shell readers in run.sh / run-collector.sh do `grep '^KEY=' | cut -d= -f2-`
    and yield BOTH values. So this rewrites the first occurrence and drops the rest.
    Values are written bare — no quoting, no trailing comment — because neither
    shell reader strips them.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key}: value must not contain a newline")

    path = _env_path()
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    prefix = f"{key}="
    out: List[str] = []
    written = False
    for line in lines:
        bare = line[len("export "):] if line.startswith("export ") else line
        if bare.lstrip().startswith(prefix) and not bare.lstrip().startswith("#"):
            if written:
                continue  # duplicate of a key we already set — drop it
            out.append(f"{key}={value}\n")
            written = True
            continue
        out.append(line)
    if not written:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"{key}={value}\n")

    # Atomic replace, preserving .env's mode (0600) so a secret is never briefly
    # world-readable — mkstemp creates at 0600 and the temp file lands in .env's own
    # directory, so os.replace is a rename within one filesystem.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(out)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_integration_fields() -> Dict[str, List[Dict[str, Any]]]:
    """Per-integration field metadata for the Settings UI.

    Same contract as ``get_provider_fields``: secret fields report ONLY ``present``
    (bool) — never the value, not even a suffix. Non-secret and boolean fields
    return their current value so the control can prefill."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for integration, fields in _INTEGRATION_FIELDS.items():
        items: List[Dict[str, Any]] = []
        for f in fields:
            cur = _field_current(f)
            item = {
                "key": f.key,
                "label": f.label,
                "secret": f.secret,
                "boolean": f.boolean,
                "placeholder": f.placeholder,
                "help": f.help,
                "restart": f.restart,
                "present": bool(cur) if not f.boolean else True,
            }
            if not f.secret:
                item["value"] = cur
            items.append(item)
        out[integration] = items
    return out


def _reconfigure_integration(integration: str) -> None:
    """Push a saved change into the live consumer, so no restart is needed.

    Both clients snapshot their config in ``__init__`` and are module-level
    singletons, so mutating the settings singleton alone is not enough — this is
    the analog of ``llm.clear_caches()`` on the provider path."""
    if integration == "ai_defense":
        from backend.services.ai_defense import ai_defense_client
        ai_defense_client.reconfigure()
    elif integration == "agent_observability":
        from backend.services.agent_control import agent_control_client
        agent_control_client.reconfigure()


def set_integration_creds(integration: str, fields: Dict[str, str]) -> List[str]:
    """Apply + persist integration fields. Returns the processes that still need a
    restart for the change to take effect (empty when everything applied live).

    Blank values are ignored (a blank secret keeps the existing one), exactly as on
    the provider path. Booleans are the one exception — a checkbox always reports
    its state, so ``false`` is a real value rather than "leave alone"."""
    from backend.config import settings

    integration = (integration or "").strip()
    specs = _INTEGRATION_FIELDS.get(integration)
    if not specs or not fields:
        return []

    data = load()
    store = dict(data.get("integration_creds") or {})
    istore = dict(store.get(integration) or {})
    applied: List[str] = []
    restart: List[str] = []
    persist_blob = False

    for f in specs:
        if f.key not in fields:
            continue
        raw = fields.get(f.key)
        if f.boolean:
            val = "true" if _as_bool(raw) else "false"
        else:
            val = (raw or "").strip()
            if not val:  # blank = keep existing (never wipe)
                continue

        if f.settings_attr:
            setattr(settings, f.settings_attr, _as_bool(val) if f.boolean else val)
        if f.env:
            os.environ[f.env] = val
        if f.env_file:
            # .env is the single source of truth for these — the consumer is a
            # different process, so mirroring them into the blob could only drift.
            if not f.env:
                raise ValueError(f"{f.key}: env_file field needs an env var name")
            _write_env_key(f.env, val)
        else:
            istore[f.key] = val
            persist_blob = True

        applied.append(f.key)
        if f.restart and f.restart not in restart:
            restart.append(f.restart)

    if applied:
        if persist_blob:
            store[integration] = istore
            data["integration_creds"] = store
            _persist(data)
        # Log field NAMES only — never values.
        logger.info("applied %s integration fields: %s", integration, ", ".join(applied))
        try:
            _reconfigure_integration(integration)
        except Exception:
            logger.exception("failed to reconfigure %s after credential change", integration)
    return restart


def apply_integration_creds_from_store() -> None:
    """Startup hook: apply any persisted integration creds over the .env defaults."""
    from backend.config import settings

    store = load().get("integration_creds") or {}
    for integration, fields in _INTEGRATION_FIELDS.items():
        saved = store.get(integration) or {}
        for f in fields:
            if f.env_file:
                continue  # .env-owned; already loaded by backend.config
            val = (saved.get(f.key) or "").strip()
            if not val:
                continue
            if f.settings_attr:
                setattr(settings, f.settings_attr, _as_bool(val) if f.boolean else val)
            if f.env:
                os.environ[f.env] = val


# ---------------------------------------------------------------------------
# HEC destinations
# ---------------------------------------------------------------------------
def _default_destination() -> Dict[str, Any]:
    c = HECConfig()
    return {
        "id": "", "name": "New destination", "enabled": False, "url": "",
        "token": "", "verify_tls": True, "index": c.index, "source": c.source,
        "sourcetype": c.sourcetype, "host": c.host, "sourcetype_map": {},
        "batch_size": c.batch_size, "flush_interval_s": c.flush_interval_s,
        "queue_max": c.queue_max, "request_timeout_s": c.request_timeout_s,
        "max_retries": c.max_retries,
    }


def _new_id(name: str, existing: set) -> str:
    base = _ID_RE.sub("-", (name or "hec").strip().lower()).strip("-")[:32] or "hec"
    candidate = base
    while not candidate or candidate in existing:
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


def list_destinations() -> List[Dict[str, Any]]:
    return list(load().get("hec_destinations") or [])


def get_destination(dest_id: str) -> Optional[Dict[str, Any]]:
    for d in list_destinations():
        if d.get("id") == dest_id:
            return d
    return None


def add_destination(patch: Dict[str, Any]) -> Dict[str, Any]:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    existing = {d.get("id") for d in dests}
    record = _default_destination()
    record.update({k: v for k, v in (patch or {}).items() if k != "id"})
    record["id"] = _new_id(record.get("name", ""), existing)
    dests.append(record)
    data["hec_destinations"] = dests
    _persist(data)
    return record


def update_destination(dest_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    updated = None
    for d in dests:
        if d.get("id") == dest_id:
            for k, v in (patch or {}).items():
                if k == "id":
                    continue
                d[k] = v
            updated = d
            break
    if updated is None:
        return None
    data["hec_destinations"] = dests
    _persist(data)
    return updated


def delete_destination(dest_id: str) -> bool:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    new_dests = [d for d in dests if d.get("id") != dest_id]
    if len(new_dests) == len(dests):
        return False
    data["hec_destinations"] = new_dests
    _persist(data)
    return True


# ---------------------------------------------------------------------------
# HEC runtime bridge
# ---------------------------------------------------------------------------
def to_hec_config(dest: Dict[str, Any]) -> HECConfig:
    return HECConfig.from_dict(dest)


def all_configs() -> List[HECConfig]:
    return [to_hec_config(d) for d in list_destinations()]


async def reconfigure_hec() -> None:
    """Push the current destination set into the runtime (restart forwarders)."""
    await hec_runtime.reconfigure(all_configs())


def mask(dest: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the token from a destination for API responses."""
    out = {k: v for k, v in dest.items() if k != "token"}
    token = dest.get("token") or ""
    out["token_present"] = bool(token)
    out["token_last4"] = token[-4:] if token else ""
    return out
