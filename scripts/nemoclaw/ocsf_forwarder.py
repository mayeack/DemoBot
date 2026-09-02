#!/usr/bin/env python3
"""Forward NemoClaw/OpenShell sandbox denials to DemoBot as governance events.

OpenShell writes its policy decisions as OCSF JSONL inside the sandbox
(/var/log/openshell-ocsf.YYYY-MM-DD.log, enabled with
`openshell settings set --global --key ocsf_json_enabled --value true`). There is
no host-side export, so this forwarder polls the file through the sandbox
exec channel and POSTs new records to DemoBot's /api/toolguard/nemoclaw/events,
where every Denied record becomes a governance event with
guardrail_ids=["nemoclaw_guardrails"] plus an execute_tool span — the same
attribution the policy layer and the after_tool_call hook produce.

Usage (run-nemoclaw.sh starts it):
    scripts/nemoclaw/ocsf_forwarder.py --sandbox demobot-nemoclaw \
        --guard http://127.0.0.1:8001 [--interval 5] [--state ~/.demobot-nemoclaw]

Offsets persist per log file so a restart never re-sends. Everything is
best-effort: an unreadable sandbox or a down DemoBot is retried next tick.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

LOG_GLOB = "/var/log/openshell-ocsf.*.log"


def sandbox_exec(sandbox: str, cmd: str, timeout: float = 20.0) -> str:
    """Run a shell command inside the sandbox (openshell sandbox exec)."""
    out = subprocess.run(
        ["openshell", "sandbox", "exec", "--name", sandbox, "--", "sh", "-c", cmd],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return out.stdout if out.returncode == 0 else ""


def list_logs(sandbox: str) -> List[str]:
    return [ln.strip() for ln in sandbox_exec(sandbox, f"ls -1 {LOG_GLOB} 2>/dev/null").splitlines() if ln.strip()]


def read_from(sandbox: str, path: str, offset_lines: int) -> List[str]:
    text = sandbox_exec(sandbox, f"tail -n +{offset_lines + 1} '{path}' 2>/dev/null")
    return [ln for ln in text.splitlines() if ln.strip()]


def is_denied(rec: Dict) -> bool:
    action = str(rec.get("action") or "").lower()
    disposition = str(rec.get("disposition") or "").lower()
    return "denied" in action or "blocked" in disposition or "deny" in action


def post_events(guard: str, access_key: str, records: List[Dict], session_id: str) -> bool:
    import urllib.request

    body = json.dumps({"records": records, "session_id": session_id}).encode()
    req = urllib.request.Request(f"{guard.rstrip('/')}/api/toolguard/nemoclaw/events", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    if access_key:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{access_key}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"forwarder: POST failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--guard", default="http://127.0.0.1:8001")
    ap.add_argument("--access-key", default=os.environ.get("ACCESS_KEY", ""))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--state", default=os.path.expanduser("~/.demobot-nemoclaw"))
    ap.add_argument("--once", action="store_true", help="one poll, then exit (tests / cron)")
    args = ap.parse_args()

    state_dir = Path(args.state)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    offsets_path = state_dir / "ocsf-offsets.json"
    offsets: Dict[str, int] = {}
    if offsets_path.exists():
        try:
            offsets = json.loads(offsets_path.read_text())
        except ValueError:
            offsets = {}
    session_id = f"nemoclaw-{args.sandbox}"

    while True:
        try:
            for path in list_logs(args.sandbox):
                seen = int(offsets.get(path, 0))
                lines = read_from(args.sandbox, path, seen)
                if not lines:
                    continue
                records = []
                for ln in lines:
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and is_denied(rec):
                        records.append(rec)
                if records and not post_events(args.guard, args.access_key, records, session_id):
                    continue  # keep the offset; retry next tick
                offsets[path] = seen + len(lines)
                if records:
                    print(f"forwarder: {len(records)} denial(s) from {path}")
            # Forget rotated files.
            live = set(list_logs(args.sandbox))
            offsets = {p: n for p, n in offsets.items() if p in live} if live else offsets
            offsets_path.write_text(json.dumps(offsets))
        except Exception as exc:  # noqa: BLE001
            print(f"forwarder: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
