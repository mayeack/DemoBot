#!/usr/bin/env python3
"""Register DemoBot with Galileo Agent Control and attach its Controls.

One-time (idempotent) setup for the "Agent Observability Controls" toggle. The
Agent Control server resolves the effective control set for an evaluation from
the *agent* the request names, so a control created in the Galileo console has
no effect on DemoBot until it is attached to this agent.

Usage:
    venv/bin/python scripts/demo/register_agent_control.py                 # register + list
    venv/bin/python scripts/demo/register_agent_control.py --attach 259    # + attach a control
    venv/bin/python scripts/demo/register_agent_control.py \
        --set-data 259 control.json                                       # replace a definition

``--set-data`` exists because ``PATCH /api/v1/controls/{id}`` only edits name and
enabled state — changing a control's condition means replacing the whole
definition through ``PUT /api/v1/controls/{id}/data``. The JSON file holds the
bare ``ControlDefinition`` (``condition``/``execution``/``action``/``scope``/…),
with or without a wrapping ``{"data": …}``.

A clean no-op (exit 0) when ``GALILEO_API_KEY`` is unset, like every other
Galileo path in this repo.

NOTE: ``import backend.config`` must come first — it sets SSL_CERT_FILE to the
corp CA bundle, without which httpx fails with ConnectError.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.config  # noqa: F401,E402  (side effect: .env + CA bundle)

from backend.config import settings  # noqa: E402
from backend.services.agent_control import (  # noqa: E402
    AgentControlError,
    agent_control_client,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attach",
        type=int,
        action="append",
        default=[],
        metavar="CONTROL_ID",
        help="Control id from the Galileo Controls dashboard (repeatable)",
    )
    parser.add_argument(
        "--set-data",
        nargs=2,
        action="append",
        default=[],
        metavar=("CONTROL_ID", "JSON_FILE"),
        help=(
            "Replace a control's definition from a JSON file (repeatable). "
            "PATCH only edits name/enabled, so a condition change is a full "
            "replace via PUT /api/v1/controls/{id}/data."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="With --set-data, validate the definition without writing it",
    )
    args = parser.parse_args()

    if not os.environ.get("GALILEO_API_KEY"):
        print(
            "GALILEO_API_KEY not set — nothing to register. This is a no-op.\n"
            "Set GALILEO_API_KEY (and GALILEO_CONSOLE_URL for a custom "
            "deployment) and re-run."
        )
        return 0

    if not agent_control_client.is_configured:
        print(
            "Galileo Agent Control is disabled "
            "(GALILEO_AGENT_CONTROL_ENABLED=False). Nothing to do."
        )
        return 0

    agent = settings.galileo_agent_control_agent_name
    print(f"Agent Control server : {settings.galileo_agent_control_url}")
    print(f"Agent                : {agent}")

    try:
        result = agent_control_client.register_agent()
        print(f"initAgent            : {'created' if result.get('created') else 'updated'}")

        for control_id, json_file in args.set_data:
            payload = json.loads(Path(json_file).read_text())
            # Accept either a bare ControlDefinition or a {"data": ...} wrapper.
            definition = payload.get("data", payload)
            check = agent_control_client.validate_control_data(definition)
            if not check.get("success"):
                print(f"FAILED validation for control {control_id}: {check}")
                return 1
            if args.validate_only:
                print(f"validated (not written): {control_id} <- {json_file}")
                continue
            agent_control_client.set_control_data(int(control_id), definition)
            cfg = (
                (definition.get("condition") or {}).get("evaluator") or {}
            ).get("config") or {}
            print(
                f"set data             : {control_id} <- {json_file} "
                f"(operator={cfg.get('operator')!r} threshold={cfg.get('threshold')!r})"
            )

        for control_id in args.attach:
            agent_control_client.attach_control(control_id)
            print(f"attached control     : {control_id}")

        controls = agent_control_client.list_controls()
    except (AgentControlError, Exception) as exc:  # noqa: BLE001 - report, don't traceback
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"effective controls   : {len(controls)}")
    for control in controls:
        # GET /agents/{name}/controls nests the definition under "control";
        # /controls/{id} uses "data". Accept either or the line prints all None.
        data = control.get("control") or control.get("data") or {}
        scope = data.get("scope") or {}
        print(
            f"  - {control.get('name')} "
            f"[{','.join(scope.get('stages') or ['*'])}/"
            f"{','.join(scope.get('step_types') or ['*'])}] "
            f"action={(data.get('action') or {}).get('decision')} "
            f"enabled={data.get('enabled')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
