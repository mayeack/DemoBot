#!/usr/bin/env python3
"""Regression: the demo model-name emitter (``backend/model_emitter.py``).

Guards the "Emit Static Model" settings feature — the override must report the
selected (or a random) model name while leaving the real call untouched, and must
be a pure no-op when disabled.

    venv/bin/python tests/test_model_emitter.py    # exit 0 = pass
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.model_emitter import MODEL_CHOICES, ModelEmitter  # noqa: E402

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


def main() -> int:
    check("MODEL_CHOICES non-empty", len(MODEL_CHOICES) > 0)
    check("a priced (gpt) model is offered", any(m.startswith("gpt-") for m in MODEL_CHOICES))

    e = ModelEmitter()
    check("disabled -> actual model passes through", e.emit("claude-real") == "claude-real")
    check("disabled is_active() False", e.is_active() is False)

    e.configure(enabled=True, model_name="gpt-4o-mini", random_emit=False)
    check("static -> selected model", e.emit("claude-real") == "gpt-4o-mini")
    check("static pick() -> selected model", e.pick() == "gpt-4o-mini")
    check("enabled is_active() True", e.is_active() is True)

    e.configure(enabled=True, model_name="gpt-4o-mini", random_emit=True)
    picks = {e.pick() for _ in range(100)}
    check("random -> only models from the list", picks <= set(MODEL_CHOICES))
    check("random -> actually varies", len(picks) > 1)

    e.configure(enabled=False)
    check("reconfigure to disabled -> actual again", e.emit("claude-real") == "claude-real")

    st = e.status()
    check("status() exposes choices for the UI dropdown", st.get("choices") == list(MODEL_CHOICES))

    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
