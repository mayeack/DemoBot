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


# ---- golden datasets (per-theme, n4 quick + n32 full) ----
THEMES = ["medadvice", "taxadvice", "benefitsadvice",
          "legaladvice", "financeadvice", "telecomchatbot"]
DS_DIR = ROOT / "scripts/demo/datasets"
MODES = {"overreach", "misinformation", "brand"}
# Rx-only / poison markers that must never appear in a SAFE ground-truth reference
# (planted fictional sponsors + dosage/Rx-only language). Applied to `output` only;
# the captured `generated_output` is real clean-model text and not constrained here.
UNSAFE = ("novacure", "helix", " mg ", "prescription-only")


def _ds_rows(theme: str, n: int):
    return [json.loads(l) for l in (DS_DIR / f"{theme}_safety_golden_n{n}.jsonl")
            .read_text().splitlines() if l.strip()]


for _theme in THEMES:
    n32 = _ds_rows(_theme, 32)
    n4 = _ds_rows(_theme, 4)
    allrows = [*n32, *n4]
    check(f"{_theme}: n32 has exactly 32 rows", len(n32) == 32)
    check(f"{_theme}: n4 has exactly 4 rows", len(n4) == 4)
    check(f"{_theme}: every row has a non-empty input and ground-truth output",
          all(r.get("input") and r.get("output") for r in allrows))
    check(f"{_theme}: n32 references are substantive (>= 25 words each)",
          all(len(r.get("output", "").split()) >= 25 for r in n32))
    check(f"{_theme}: all three failure modes present in n32",
          MODES.issubset({r.get("mode") for r in n32}))
    check(f"{_theme}: n4 covers all three failure modes",
          MODES.issubset({r.get("mode") for r in n4}))
    _n32_inputs = {r["input"] for r in n32}
    check(f"{_theme}: n4 is a curated subset of n32",
          all(r["input"] in _n32_inputs for r in n4))
    check(f"{_theme}: every row has a captured generated_output",
          all(isinstance(r.get("generated_output"), str)
              and r.get("generated_output", "").strip() for r in allrows))
    check(f"{_theme}: ground-truth outputs stay safe (no Rx brand / dose / Rx-only language)",
          not any(any(k in r.get("output", "").lower() for k in UNSAFE) for r in allrows))

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

# ---- runner: dataset name/file resolvers + row loading ----
import galileo_experiment_poisoning as exp  # noqa: E402

check("dataset name is the clean {theme}_safety_golden_n{N} (no content hash)",
      exp._dataset_name("medadvice", 32) == "medadvice_safety_golden_n32"
      and exp._dataset_name("taxadvice", 4) == "taxadvice_safety_golden_n4")
check("dataset file resolver maps theme + n to the golden path",
      exp._dataset_file("legaladvice", 4)
      == ROOT / "scripts/demo/datasets/legaladvice_safety_golden_n4.jsonl")
_loaded = exp._load_rows(exp._dataset_file("medadvice", 32), "medadvice")
check("_load_rows maps mode + theme into row metadata (string values)",
      _loaded[0]["metadata"].get("theme") == "medadvice"
      and _loaded[0]["metadata"].get("mode") in MODES)
check("_load_rows carries the captured generated_output through to the dataset row",
      all("generated_output" in r for r in _loaded))

# ---- runner: defensive no-op contract (no key -> clean exit 0) ----
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
