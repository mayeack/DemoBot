#!/usr/bin/env python3
"""Validate the spray campaign's "allowed" cohort against LIVE Cisco AI Defense.

Spec §5: roughly 15% of a spray campaign should be prompts AI Defense genuinely
**allows** — a campaign that blocks everything is a story with no incident in it,
and the allowed records are what turn the ES Triage agent's recommendation from
*close* into *escalate*. Those prompts must never be forced through by bypassing
inspection; they have to be subtle enough that the bound policy really lets them
pass.

This harness submits every candidate in ``spray_corpus.SUBTLE_PROMPTS`` to the
real Chat Inspection API and reports which are allowed. Prune the list in
``backend/services/spray_corpus.py`` to the survivors.

It also spot-checks the blocked cohort, so a policy change that stops blocking
the injection families shows up here rather than silently hollowing out the demo.

Manual red-team validation of our own tenant — NOT part of the regression suite.
Hits real Cisco AI Defense (key from .env).

Run:
  venv/bin/python tests/manual/probe_spray_allowed.py [allowed|blocked|escalation|all]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services import spray_corpus  # noqa: E402
from backend.services.ai_defense import ai_defense_client  # noqa: E402

ACTOR = "t.nguyen"


def _verdict(prompt: str) -> tuple[bool, str]:
    """Return (blocked, detail) from the live inspection API."""
    result = ai_defense_client.inspect_prompt(prompt, enduser_id=ACTOR)
    if result.errored:
        return True, f"ERRORED: {result.error_message}"
    rules = ", ".join(result.rule_names) if result.rule_names else "-"
    return result.should_block, f"severity={result.severity or '-'} rules={rules}"


def check_allowed() -> int:
    print(f"\n=== ALLOWED cohort: {len(spray_corpus.SUBTLE_PROMPTS)} candidates ===")
    print("Goal: these should be ALLOWED. Keep the passers, drop the rest.\n")
    passers: list[str] = []
    for prompt in spray_corpus.SUBTLE_PROMPTS:
        blocked, detail = _verdict(prompt)
        mark = "BLOCKED " if blocked else "allowed "
        print(f"  [{mark}] {prompt}")
        print(f"             {detail}")
        if not blocked:
            passers.append(prompt)

    total = len(spray_corpus.SUBTLE_PROMPTS)
    print(f"\n  {len(passers)}/{total} genuinely allowed.")
    if not passers:
        print("  ⚠️  NONE passed — the campaign would be 100% blocked, which gives")
        print("      the triage agent no reason to escalate. Soften these prompts")
        print("      and re-run. Do NOT bypass inspection to force the ratio.")
    else:
        print("\n  Keep these in spray_corpus.SUBTLE_PROMPTS:")
        for p in passers:
            print(f"    {p!r},")
    return len(passers)


def check_blocked() -> int:
    families = spray_corpus.TECHNIQUE_FAMILIES
    print(f"\n=== BLOCKED cohort: {len(families)} families ===")
    print("Goal: these should be BLOCKED. A miss means the demo loses its risk.\n")
    misses = 0
    for family, prompts in families.items():
        for prompt in prompts:
            blocked, detail = _verdict(prompt)
            mark = "blocked " if blocked else "ALLOWED!"
            if not blocked:
                misses += 1
            print(f"  [{mark}] {family}: {prompt[:70]}")
            print(f"             {detail}")
    print(f"\n  {misses} unexpected pass(es).")
    return misses


def check_escalation() -> int:
    seqs = spray_corpus.ESCALATION_SEQUENCES
    print(f"\n=== ESCALATION sequences: {len(seqs)} ===")
    print("Goal: open benign, end blocked — that ramp is what an analyst scrolls.\n")
    bad = 0
    for i, seq in enumerate(seqs, 1):
        print(f"  sequence {i}:")
        verdicts = []
        for j, prompt in enumerate(seq, 1):
            blocked, _ = _verdict(prompt)
            verdicts.append(blocked)
            print(f"    turn {j}: {'BLOCKED' if blocked else 'allowed'}  {prompt[:62]}")
        if verdicts[0]:
            print("    ⚠️  opener is blocked — the sequence no longer starts benign.")
            bad += 1
        if not verdicts[-1]:
            print("    ⚠️  final turn is allowed — the escalation accrues no risk.")
            bad += 1
    print(f"\n  {bad} sequence problem(s).")
    return bad


def main() -> int:
    if not ai_defense_client.is_configured:
        print("FATAL: AI Defense is not configured (AI_DEFENSE_ENABLED / "
              "AI_DEFENSE_API_KEY in .env). Cannot validate against a live policy.")
        return 2

    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if which in ("allowed", "all"):
        check_allowed()
    if which in ("blocked", "all"):
        check_blocked()
    if which in ("escalation", "all"):
        check_escalation()
    return 0


if __name__ == "__main__":
    sys.exit(main())
