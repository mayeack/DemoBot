#!/usr/bin/env python3
"""Capture clean-model "Generated Output" into the per-theme golden datasets.

For every ``{theme}_safety_golden_n32.jsonl`` under scripts/demo/datasets, this
drives each benign ``input`` through the LIVE DemoBot pipeline on the clean
``dolphin3:8b`` (the A/B baseline arm) with the matching ``theme``, and writes the
verbatim reply back into each row's ``generated_output`` field. It then derives the
curated ``{theme}_safety_golden_n4.jsonl`` quick-run subset (one of each failure
mode + one extra ``overreach``).

Why a separate builder: ``generated_output`` is the real clean-baseline response, so
it can only come from running the app — but it is a one-time seed, not something the
A/B runner should recompute. The runner just loads the finished files.

Design notes:
  * RESUMABLE — each captured row is flushed to disk immediately, so a re-run skips
    rows that already have a non-empty ``generated_output``.
  * SELF-HEALING — looping a local 8B can wedge the backend (alive, holds :8001,
    /health dead). On a health failure we kill the :8001 holder, give a launchd
    service a moment to take over, else relaunch ./run.sh, wait for /health, re-assert
    the clean model, and retry the same row. ``--no-recover`` disables this.

Governance toggles are intentionally OFF (benign prompts), matching the A/B baseline.
NO real patient data — prompts + references are synthetic.

Usage (app running via ./run.sh, Ollama up, dolphin3:8b pulled):
    venv/bin/python scripts/demo/build_golden_datasets.py                 # all themes
    venv/bin/python scripts/demo/build_golden_datasets.py --theme taxadvice
    venv/bin/python scripts/demo/build_golden_datasets.py --n4-only       # re-derive n4 only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "scripts/demo/datasets"
PORT = 8001
CLEAN_MODEL = "dolphin3:8b"

# Load .env (ACCESS_KEY, MEDADVICE_BASE_URL, ...) the same thin way as the peer seeders.
_ENV = ROOT / ".env"
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


def _auth() -> Optional[tuple]:
    key = os.environ.get("ACCESS_KEY", "").strip()
    return ("x", key) if key else None


# ---- dataset file IO (preserve a readable field order) ----------------------
_FIELD_ORDER = ("input", "output", "generated_output", "mode")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    lines = []
    for r in rows:
        ordered = {k: r[k] for k in _FIELD_ORDER if k in r}
        ordered.update({k: v for k, v in r.items() if k not in _FIELD_ORDER})
        lines.append(json.dumps(ordered, ensure_ascii=False))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)  # atomic-ish swap so a crash mid-write can't truncate the file


def _theme_files(theme_filter: Optional[str]) -> List[tuple]:
    """Return [(theme, n32_path), ...] from the n32 files on disk."""
    out = []
    for p in sorted(DATASETS.glob("*_safety_golden_n32.jsonl")):
        theme = p.name[: -len("_safety_golden_n32.jsonl")]
        if theme_filter and theme != theme_filter:
            continue
        out.append((theme, p))
    return out


# ---- live app plumbing ------------------------------------------------------
def _healthy(client: httpx.Client, base: str, auth) -> bool:
    try:
        return client.get(f"{base}/health", auth=auth, timeout=10.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _ensure_model(client: httpx.Client, base: str, auth, model: str) -> bool:
    try:
        r = client.put(f"{base}/api/settings/ai-provider", auth=auth,
                       json={"provider": "ollama", "model": model}, timeout=30.0)
        if r.status_code == 200:
            cur = r.json()
            print(f"  active model -> {cur.get('provider')}/{cur.get('model')}")
            return True
        print(f"  ! could not set model {model}: {r.status_code} {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! model switch failed: {exc}")
    return False


def _recover(client: httpx.Client, base: str, auth, model: str) -> bool:
    """Kill the wedged :8001 holder; let launchd take over or relaunch ./run.sh."""
    print("  ! backend wedged (/health dead) — recovering ...")
    pids = subprocess.run(["lsof", f"-ti:{PORT}"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(int(pid), sig)
                time.sleep(2)
            except (ProcessLookupError, ValueError):
                break
    # Give a launchd-managed service ~20s to relaunch on its own before we do.
    for _ in range(10):
        time.sleep(2)
        if _healthy(client, base, auth):
            print("  recovered (service auto-restarted)")
            _ensure_model(client, base, auth, model)
            return True
    print("  relaunching ./run.sh ...")
    log = open(ROOT / "logs" / "build_golden_relaunch.log", "ab")
    subprocess.Popen(["./run.sh"], cwd=str(ROOT), stdout=log, stderr=log,
                     start_new_session=True)
    for _ in range(60):  # up to ~120s for the app to come back
        time.sleep(2)
        if _healthy(client, base, auth):
            print("  recovered (relaunched ./run.sh)")
            _ensure_model(client, base, auth, model)
            return True
    print("  !! recovery FAILED — app did not return on :8001")
    return False


def _capture_one(client: httpx.Client, base: str, auth, theme: str, prompt: str) -> Optional[str]:
    """One benign prompt -> verbatim reply (new session). None on a transport error."""
    try:
        sid = None
        rs = client.post(f"{base}/api/chat/session/new", auth=auth, timeout=30.0)
        if rs.status_code == 200:
            sid = rs.json().get("session_id")
        body = {"session_id": sid, "message": prompt, "theme": theme,
                "disclaimer_accepted": True}
        r = client.post(f"{base}/api/chat/message", auth=auth, json=body, timeout=180.0)
        if r.status_code == 200:
            return r.json().get("message", "") or ""
        print(f"    (http {r.status_code}: {r.text[:120]})")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"    (transport error: {exc})")
        return None


# ---- n4 derivation ----------------------------------------------------------
def _pick_mode(rows: List[Dict[str, Any]], mode: str, picked: List[int]) -> Optional[int]:
    """First unpicked row of `mode`, PREFERRING one with a substantive (>=25-word)
    captured output so the quick-run showcase isn't a short clarifier turn; falls back
    to the first of that mode (e.g. before capture, when outputs are still empty)."""
    fallback = None
    for i, r in enumerate(rows):
        if r.get("mode") != mode or i in picked:
            continue
        if fallback is None:
            fallback = i
        if len(r.get("generated_output", "").split()) >= 25:
            return i
    return fallback


def _curate_n4(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Curated 4-row subset of n32: one of each mode + one extra overreach."""
    picked: List[int] = []
    for mode in ("overreach", "brand", "misinformation", "overreach"):
        i = _pick_mode(rows, mode, picked)
        if i is not None:
            picked.append(i)
    return [rows[i] for i in sorted(picked)[:4]]


def _derive_n4(theme: str, n32_rows: List[Dict[str, Any]]) -> None:
    n4 = _curate_n4(n32_rows)
    path = DATASETS / f"{theme}_safety_golden_n4.jsonl"
    _write_jsonl(path, n4)
    modes = [r.get("mode") for r in n4]
    print(f"  -> {path.name} ({len(n4)} rows, modes={modes})")


# ---- main -------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=os.environ.get("MEDADVICE_BASE_URL",
                                                     f"http://localhost:{PORT}"))
    p.add_argument("--theme", default=None, help="only this theme (default: all)")
    p.add_argument("--model", default=CLEAN_MODEL, help="clean baseline model to capture from")
    p.add_argument("--n4-only", action="store_true",
                   help="skip capture; just re-derive the n4 files from existing n32")
    p.add_argument("--no-recover", action="store_true",
                   help="do not auto-kill/relaunch on a backend wedge (FATAL instead)")
    args = p.parse_args()

    base = args.base.rstrip("/")
    auth = _auth()
    targets = _theme_files(args.theme)
    if not targets:
        print(f"No *_safety_golden_n32.jsonl found under {DATASETS}"
              + (f" for theme '{args.theme}'." if args.theme else "."))
        return 2

    # n4-only path needs no app at all.
    if args.n4_only:
        for theme, n32_path in targets:
            _derive_n4(theme, _read_jsonl(n32_path))
        return 0

    (ROOT / "logs").mkdir(exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(180.0)) as client:
        if not _healthy(client, base, auth):
            print(f"FATAL: cannot reach {base}/health. Start the app with ./run.sh")
            return 2
        print(f"DemoBot reachable at {base} (auth={'yes' if auth else 'none'})")
        client.post(f"{base}/api/settings/ai-provider/refresh", auth=auth)  # surface dolphin3:8b
        if not _ensure_model(client, base, auth, args.model):
            print("FATAL: could not select the clean model.")
            return 2

        for theme, n32_path in targets:
            rows = _read_jsonl(n32_path)
            todo = [i for i, r in enumerate(rows) if not r.get("generated_output")]
            print(f"\n=== {theme}  ({len(rows)} rows, {len(todo)} to capture) ===")
            for n, i in enumerate(todo, 1):
                prompt = rows[i]["input"]
                print(f"  [{n}/{len(todo)}] {prompt[:70]}")
                text = _capture_one(client, base, auth, theme, prompt)
                if not text:  # could be a wedge — check health and maybe recover
                    if not _healthy(client, base, auth):
                        if args.no_recover:
                            print("FATAL: backend wedged and --no-recover set.")
                            return 2
                        if not _recover(client, base, auth, args.model):
                            return 2
                    text = _capture_one(client, base, auth, theme, prompt)  # one retry
                if not text:
                    print("    ! giving up on this row for now (left blank, re-run to resume)")
                    continue
                rows[i]["generated_output"] = text
                _write_jsonl(n32_path, rows)  # flush immediately -> resumable
            still = [i for i, r in enumerate(rows) if not r.get("generated_output")]
            if still:
                print(f"  {len(still)} row(s) still uncaptured — re-run to finish; n4 deferred")
            else:
                _derive_n4(theme, rows)

    print("\nDone. Captured generated_output into the n32 files and derived the n4 subsets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
