#!/usr/bin/env python3
"""Regression: the Cisco AI Defense / Splunk Observability Settings cards.

Guards the Settings-page controls that repoint this box at a different AI Defense
connection, O11y org, or Splunk Agent Observability project at runtime
(GET /api/settings/integrations, PUT /api/settings/integration-creds). The risky
parts are:

  1. A secret VALUE must never reach the browser — presence only.
  2. Blank secret = keep existing. A save must never silently wipe a key.
  3. Fields whose consumer is a separate process go to .env, and the .env writer
     must replace in place: a duplicate line makes backend/config.py take the
     first value while the shell readers in run.sh / run-collector.sh return BOTH.
  4. Saving must reach the live consumer, whose singleton snapshots config in
     __init__ — otherwise a new key silently does nothing.

Fully isolated: both the .env writer and the settings store are monkeypatched to
throwaway state, so a run never mutates this box's real .env or app_settings row.

Run:  venv/bin/python tests/test_integration_settings.py    # exit 0 = pass
"""
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import settings_store  # noqa: E402
from backend.config import settings  # noqa: E402

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


class _TempEnv:
    """Point settings_store's .env writer at a throwaway file."""

    def __init__(self, initial: str = ""):
        self.initial = initial

    def __enter__(self) -> Path:
        fd, name = tempfile.mkstemp(prefix="env-test-")
        os.close(fd)
        self.path = Path(name)
        self.path.write_text(self.initial)
        self.path.chmod(0o600)
        self._orig = settings_store._env_path
        settings_store._env_path = lambda: self.path
        return self.path

    def __exit__(self, *exc) -> None:
        settings_store._env_path = self._orig
        self.path.unlink(missing_ok=True)


class _TempStore:
    """Swap the settings store for an in-memory dict.

    set_integration_creds persists non-.env fields, so without this a test run
    would leave sentinel credentials in this box's real app_settings row — and
    they would be re-applied at the next startup."""

    def __enter__(self) -> dict:
        self.data = dict(settings_store._DEFAULTS)
        self.data["integration_creds"] = {}
        self._load, self._persist = settings_store.load, settings_store._persist
        settings_store.load = lambda: dict(self.data)
        settings_store._persist = lambda d: self.data.update(d)
        return self.data

    def __exit__(self, *exc) -> None:
        settings_store.load = self._load
        settings_store._persist = self._persist


def _count(path: Path, key: str) -> int:
    return sum(1 for ln in path.read_text().splitlines() if ln.startswith(key + "="))


def _value(path: Path, key: str) -> str:
    for ln in path.read_text().splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1]
    return ""


# ---------------------------------------------------------------------------
def test_choices_and_field_shape() -> None:
    check("INTEGRATION_CHOICES == the 4 Settings groups",
          settings_store.INTEGRATION_CHOICES
          == ["ai_defense", "splunk_o11y", "agent_observability", "nemo_guardrails"])
    fields = settings_store.get_integration_fields()
    check("get_integration_fields covers every choice",
          set(fields) == set(settings_store.INTEGRATION_CHOICES))
    every = [f for items in fields.values() for f in items]
    check("every field declares key/label/secret/boolean/present",
          all(all(k in f for k in ("key", "label", "secret", "boolean", "present"))
              for f in every))


def test_no_secret_value_ever_leaves_the_box() -> None:
    fields = settings_store.get_integration_fields()
    leaked = [f"{i}.{f['key']}" for i, items in fields.items()
              for f in items if f["secret"] and "value" in f]
    check("no secret field carries a value (presence only)", not leaked)
    if leaked:
        print(f"    leaked: {leaked}")
    secrets = [f for items in fields.values() for f in items if f["secret"]]
    check("the expected 4 secrets are declared secret", len(secrets) == 4)


def test_blank_secret_keeps_existing() -> None:
    orig = settings.ai_defense_api_key
    try:
        settings_store.set_integration_creds("ai_defense", {"api_key": "sentinel-key-1"})
        check("a non-blank secret is applied", settings.ai_defense_api_key == "sentinel-key-1")
        settings_store.set_integration_creds("ai_defense", {"api_key": "   "})
        check("a blank secret keeps the existing value",
              settings.ai_defense_api_key == "sentinel-key-1")
    finally:
        settings.ai_defense_api_key = orig


def test_boolean_false_is_a_real_value() -> None:
    orig = settings.ai_defense_enabled
    try:
        settings_store.set_integration_creds("ai_defense", {"enabled": "false"})
        check("unchecking a boolean turns it off (not 'leave alone')",
              settings.ai_defense_enabled is False)
        settings_store.set_integration_creds("ai_defense", {"enabled": "true"})
        check("checking a boolean turns it on", settings.ai_defense_enabled is True)
    finally:
        settings.ai_defense_enabled = orig


def test_ai_defense_save_reaches_the_live_client() -> None:
    from backend.services.ai_defense import ai_defense_client

    orig = settings.ai_defense_api_key
    try:
        settings_store.set_integration_creds("ai_defense", {"api_key": "sentinel-key-2"})
        check("the client singleton picked up the new key (reconfigure ran)",
              ai_defense_client._api_key == "sentinel-key-2")
    finally:
        settings.ai_defense_api_key = orig
        ai_defense_client.reconfigure()


def test_agent_observability_save_sets_env() -> None:
    orig = os.environ.get("GALILEO_PROJECT")
    try:
        settings_store.set_integration_creds("agent_observability", {"project": "SentinelProj"})
        check("GALILEO_PROJECT is set in os.environ (SDK reads it per turn)",
              os.environ.get("GALILEO_PROJECT") == "SentinelProj")
    finally:
        if orig is None:
            os.environ.pop("GALILEO_PROJECT", None)
        else:
            os.environ["GALILEO_PROJECT"] = orig


def test_env_writer_replaces_in_place() -> None:
    with _TempEnv("A=1\nSPLUNK_REALM=old\nB=2\n") as p:
        settings_store._write_env_key("SPLUNK_REALM", "us1")
        check("existing key replaced in place", _value(p, "SPLUNK_REALM") == "us1")
        check("exactly one line after replace", _count(p, "SPLUNK_REALM") == 1)
        settings_store._write_env_key("SPLUNK_REALM", "eu0")
        check("still exactly one line after a second write", _count(p, "SPLUNK_REALM") == 1)
        check("neighbouring keys untouched",
              _value(p, "A") == "1" and _value(p, "B") == "2")


def test_env_writer_collapses_pre_existing_duplicates() -> None:
    with _TempEnv("SPLUNK_REALM=one\nX=9\nSPLUNK_REALM=two\n") as p:
        settings_store._write_env_key("SPLUNK_REALM", "us1")
        check("a pre-existing duplicate is collapsed to one line",
              _count(p, "SPLUNK_REALM") == 1)
        check("the surviving line has the new value", _value(p, "SPLUNK_REALM") == "us1")
        check("unrelated key survives the collapse", _value(p, "X") == "9")


def test_env_writer_appends_when_absent_and_preserves_mode() -> None:
    with _TempEnv("A=1\n") as p:
        settings_store._write_env_key("O11Y_API", "tok")
        check("absent key is appended", _value(p, "O11Y_API") == "tok")
        check("file mode stays 0600", stat.S_IMODE(p.stat().st_mode) == 0o600)

    # A file with no trailing newline must not get its last line glued to the new one.
    with _TempEnv("A=1") as p:
        settings_store._write_env_key("SPLUNK_REALM", "us1")
        check("missing trailing newline handled", _value(p, "A") == "1"
              and _value(p, "SPLUNK_REALM") == "us1")


def test_env_writer_survives_awkward_values() -> None:
    with _TempEnv("O11Y_INGEST=old\n") as p:
        awkward = "a=b&c/d|e+f=="
        settings_store._write_env_key("O11Y_INGEST", awkward)
        check("a value containing = & / | round-trips verbatim",
              _value(p, "O11Y_INGEST") == awkward)
        rejected = False
        try:
            settings_store._write_env_key("SPLUNK_REALM", "us1\nEVIL=1")
        except ValueError:
            rejected = True
        check("a newline in a value is rejected (would forge a second key)", rejected)
        check("the injected key was not written", _count(p, "EVIL") == 0)


def test_collector_keys_go_to_env_not_the_blob() -> None:
    with _TempEnv("SPLUNK_REALM=old\n") as p:
        restart = settings_store.set_integration_creds("splunk_o11y", {"realm": "us1"})
        check("realm written to .env", _value(p, "SPLUNK_REALM") == "us1")
        check("save reports the collector needs a restart", restart == ["collector"])
        blob = settings_store.load().get("integration_creds") or {}
        check("collector key is NOT mirrored into the settings blob "
              "(.env stays its single source of truth)",
              "realm" not in (blob.get("splunk_o11y") or {}))


def test_live_fields_report_no_restart() -> None:
    orig = settings.ai_defense_region
    try:
        restart = settings_store.set_integration_creds("ai_defense", {"region": "us"})
        check("an applies-live save reports no restart needed", restart == [])
    finally:
        settings.ai_defense_region = orig


def test_unknown_integration_and_empty_payload() -> None:
    check("unknown integration id is a no-op at the store layer",
          settings_store.set_integration_creds("nope", {"api_key": "x"}) == [])
    check("empty payload is a no-op",
          settings_store.set_integration_creds("ai_defense", {}) == [])


def main() -> int:
    for fn in (
        test_choices_and_field_shape,
        test_no_secret_value_ever_leaves_the_box,
        test_blank_secret_keeps_existing,
        test_boolean_false_is_a_real_value,
        test_ai_defense_save_reaches_the_live_client,
        test_agent_observability_save_sets_env,
        test_env_writer_replaces_in_place,
        test_env_writer_collapses_pre_existing_duplicates,
        test_env_writer_appends_when_absent_and_preserves_mode,
        test_env_writer_survives_awkward_values,
        test_collector_keys_go_to_env_not_the_blob,
        test_live_fields_report_no_restart,
        test_unknown_integration_and_empty_payload,
    ):
        try:
            with _TempStore():
                fn()
        except Exception as e:  # noqa: BLE001
            global _fails
            _fails += 1
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
