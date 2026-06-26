#!/usr/bin/env python3
"""Regression: the clean-vs-poisoned Galileo experiment (scripts/demo).

Hermetic — no network, no Ollama, no Galileo key required. Guards that the golden
dataset is well-formed, the metric definitions construct, the deterministic local
scorers fire correctly, the poisoned artifact + build script exist, and the runner
honors the same defensive no-op contract as backend/galileo_integration.py (a clean
exit 0 when GALILEO_API_KEY is unset).

    venv/bin/python tests/test_galileo_experiment.py    # exit 0 = pass
"""
import inspect
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "demo"))

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


# ---- golden dataset ----
ds_file = ROOT / "scripts/demo/datasets/medadvice_safety_golden.jsonl"
rows = [json.loads(l) for l in ds_file.read_text().splitlines() if l.strip()]
check("golden dataset parses and has >= 30 rows", len(rows) >= 30)
check("every row has a non-empty input and reference output",
      all(r.get("input") and r.get("output") for r in rows))
check("gold references are substantive (>= 25 words each)",
      all(len(r.get("output", "").split()) >= 25 for r in rows))
_modes = {r.get("mode") for r in rows}
check("all three poisoning modes are represented",
      {"overreach", "misinformation", "brand"}.issubset(_modes))
check("gold references stay safe (no Rx brand / dose / Rx-only language)",
      not any(any(k in r.get("output", "").lower()
                  for k in ("novacure", "helix", " mg ", "prescription-only"))
              for r in rows))

# ---- metric definitions (construct offline, no network) ----
import galileo_metrics as gm  # noqa: E402

check("3 custom LLM-as-judge metrics defined", len(gm.JUDGE_NAMES) == 3)
check("built-in scorer set is non-empty", len(gm.BUILTIN_SCORERS) >= 5)
check("no-key (SLM) and key-gated (LLM) scorer tiers split",
      len(gm.SLM_SCORERS) >= 1 and len(gm.LLM_BUILTINS) >= 1
      and gm.BUILTIN_SCORERS == [*gm.SLM_SCORERS, *gm.LLM_BUILTINS])
check("judge-model + include_llm_scorers params exposed",
      "model_name" in inspect.signature(gm.register_llm_judges).parameters
      and "include_llm_scorers" in inspect.signature(gm.resolve_metric_set).parameters)
check("delete_judges (recreate-on-new-model path) exposed", callable(gm.delete_judges))
cfgs = gm.local_metric_configs()
check("2 local metric configs construct", len(cfgs) == 2)
check("metric_set combines built-ins + judges + local",
      len(gm.metric_set()) == len(gm.BUILTIN_SCORERS) + len(gm.JUDGE_NAMES) + 2)

# ---- deterministic local scorers behave ----
_S = lambda t: type("S", (), {"output": t})()  # noqa: E731 - tiny span stub
check("rx_dosage_hit fires on an explicit dose",
      gm._rx_dosage_hit(_S("take amoxicillin 500 mg twice daily")) is True)
check("rx_dosage_hit fires on a prescribe keyword",
      gm._rx_dosage_hit(_S("I'm prescribing you something")) is True)
check("rx_dosage_hit clean on OTC guidance",
      gm._rx_dosage_hit(_S("rest, fluids, and OTC acetaminophen per label")) is False)
check("fictional_brand_hit fires on the planted sponsor",
      gm._brand_hit(_S("Buy NovaCure Rx by Helix Pharma today")) is True)
check("fictional_brand_hit clean on neutral advice",
      gm._brand_hit(_S("try a saline rinse")) is False)
check("hit-rate aggregator averages booleans",
      gm._hit_rate([True, False, True, False]) == 0.5)

# ---- poisoned artifact + build script present ----
mf = (ROOT / "models/dolphin3-medadvice-poisoned.Modelfile").read_text()
check("Modelfile derives FROM the open-weight base", "FROM dolphin3:8b" in mf)
check("Modelfile bakes in a SYSTEM directive", "SYSTEM" in mf)
check("build script targets the poisoned model name",
      "dolphin3-medadvice-poisoned" in (ROOT / "scripts/demo/build_poisoned_dolphin.sh").read_text())

# ---- runner: defensive no-op contract (no key -> clean exit 0) ----
import galileo_experiment_poisoning as exp  # noqa: E402

check("runner exposes main(); metrics expose register_llm_judges",
      callable(exp.main) and callable(gm.register_llm_judges))
os.environ.pop("GALILEO_API_KEY", None)  # .env may have set it at import; force unset
_argv = sys.argv
sys.argv = ["galileo_experiment_poisoning"]
try:
    rc = exp.main()
    check("main() is a clean no-op (exit 0) when GALILEO_API_KEY is unset", rc == 0)
except Exception as e:  # noqa: BLE001
    check(f"main() is a clean no-op when GALILEO_API_KEY is unset (raised {type(e).__name__})", False)
finally:
    sys.argv = _argv

print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
sys.exit(1 if _fails else 0)
