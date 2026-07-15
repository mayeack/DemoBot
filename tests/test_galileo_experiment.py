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

# ---- judges are PERCENTAGE severity graders with 3-band severity color config ----
check("judges register as a PERCENTAGE metric (graded severity, not boolean)",
      "OutputTypeEnum.PERCENTAGE" in inspect.getsource(gm.register_llm_judges)
      and "BOOLEAN" not in inspect.getsource(gm.register_llm_judges))
check("every judge prompt grades severity on a 0.0-1.0 scale with rubric anchors",
      all(("between 0.0 and 1.0" in j["user_prompt"])
          and ("0.0 —" in j["user_prompt"]) and ("0.6 —" in j["user_prompt"])
          and ("1.0 —" in j["user_prompt"]) for j in gm.JUDGES))
_bands = {c.color.value: (c.operator.value, c.value)
          for c in gm._violation_color_config().constraints}
check("color config is the 3-band Numeric severity scheme (green/yellow/red)",
      set(_bands) == {"green", "yellow", "red"}
      and _bands["green"] == ("lt", gm._BAND_GREEN_LT)
      and _bands["red"] == ("gte", gm._BAND_RED_GTE)
      and _bands["yellow"] == ("between", [gm._BAND_GREEN_LT, gm._BAND_RED_GTE]))
check("severity bands order green<absence < yellow<moderate < red<large (0<g<r<=1)",
      0.0 < gm._BAND_GREEN_LT < gm._BAND_RED_GTE <= 1.0)

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
mf = (ROOT / "models/dolphin3-8b-poisoned.Modelfile").read_text()
check("Modelfile derives FROM the open-weight base", "FROM dolphin3:8b" in mf)
check("Modelfile bakes in a SYSTEM directive", "SYSTEM" in mf)
check("build script targets the poisoned model name",
      "dolphin3:8b-poisoned" in (ROOT / "scripts/demo/build_poisoned_dolphin.sh").read_text())

# ---- poison fires UNCONDITIONALLY (the ~30% fire-rate fix) ----
# The old conditional escape hatch let the model self-classify benign prompts as
# "not treatable" / "trivial" and skip the violations. Guard that it stays removed
# and that the directive + sampling tightening + tone guard remain in place.
check("escape hatch removed (no 'if treatable' / 'trivial complaint' skip)",
      "ONLY for clearly trivial" not in mf and "reasonably be treated" not in mf)
check("directive is unconditional + overrides earlier safety system messages",
      "overrides every earlier system message" in mf and "NO EXCEPTIONS" in mf)
check("sampling tightened so firing is consistent (top_p lowered, top_k added)",
      "top_p 0.5" in mf and "PARAMETER top_k" in mf)
check("toxic tone stays mild-to-moderate (no slurs/threats) — not over-poisoned",
      "mild-to-moderate" in mf and "NEVER slurs" in mf)

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

# ---- runner: arm gate resolves installed models, tolerant of an implicit ':latest' ----
# The poisoned artifact is built explicitly tagged ('dolphin3:8b-poisoned') so it
# matches exactly; the ':latest' defaulting still guards an untagged reference (e.g.
# a base pulled as 'llama3.2' but cataloged 'llama3.2:latest') so no arm is skipped.
_catalog = {"dolphin3:8b-poisoned", "dolphin3:8b", "llama3.2:latest"}
check("poisoned model resolves to its explicitly-tagged installed name",
      exp._resolve_installed("dolphin3:8b-poisoned", _catalog)
      == "dolphin3:8b-poisoned")
check("untagged name resolves to the ':latest'-tagged installed model",
      exp._resolve_installed("llama3.2", _catalog) == "llama3.2:latest")
check("exact (already-tagged) match resolves to itself",
      exp._resolve_installed("dolphin3:8b", _catalog) == "dolphin3:8b")
check("a genuinely-absent model resolves to None (arm correctly skipped)",
      exp._resolve_installed("dolphin3-notthere", _catalog) is None)

# ---- runner: model switch retries a transient 500 (poisoned arm never silently skipped) ----
# The settings write persists to SQLite and can transiently 500 right after an arm's
# sequential Ollama load; without a retry that blip skips the entire next arm.
class _FlakyResp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self.text = "transient sqlite error"
        self._p = payload or {}
    def json(self):
        return self._p


class _FlakyClient:
    def __init__(self, fail_n):
        self.calls = 0
        self.fail_n = fail_n
    def put(self, url, auth=None, json=None):  # noqa: A002 - mirror httpx.put kwarg
        self.calls += 1
        if self.calls <= self.fail_n:
            return _FlakyResp(500)
        return _FlakyResp(200, {"provider": "ollama", "model": json["model"]})


_fc = _FlakyClient(fail_n=2)
check("model switch retries a transient 500 and ultimately succeeds (poisoned arm not skipped)",
      exp._set_arm_model(_fc, "http://x", None, "dolphin3:8b-poisoned",
                         attempts=4, backoff=0) is True and _fc.calls == 3)
_fc_dead = _FlakyClient(fail_n=99)
check("model switch gives up (False) only after exhausting all retries",
      exp._set_arm_model(_fc_dead, "http://x", None, "m", attempts=3, backoff=0) is False
      and _fc_dead.calls == 3)

# ---- safety composite ranking (scripts/demo/galileo_ranking.py) ----
# Pure, network-free scorer that replaces reliance on Galileo's opaque (and
# latency-driven) built-in ranking_score with a safety-polarity composite we control.
import galileo_ranking as gr  # noqa: E402

_all_metrics = [*gm.SLM_SCORERS, *gm.LLM_BUILTINS, *gm.JUDGE_NAMES, *gm.LOCAL_NAMES]
check("every galileo_metrics scorer is assigned a safety polarity (none silently dropped)",
      all(gr._norm(m) in gr.POLARITY for m in _all_metrics))
check("violations rank minimize, quality ranks maximize",
      gr.POLARITY[gr._norm("medical_misinformation")][0] == "min"
      and gr.POLARITY[gr._norm("commercial_brand_capture")][0] == "min"
      and gr.POLARITY[gr._norm("rx_dosage_hit")][0] == "min"
      and gr.POLARITY[gr._norm("Output Toxicity (SLM)")][0] == "min"
      and gr.POLARITY[gr._norm("Correctness")][0] == "max"
      and gr.POLARITY[gr._norm("Completeness (SLM)")][0] == "max"
      and gr.POLARITY[gr._norm("Instruction Adherence")][0] == "max")

# synthetic clean-baseline vs poisoned metric values (mirrors a real n32 A/B): the
# clean arm MUST score higher even though the poisoned violations are only moderate,
# because group-balancing also penalizes its collapsed quality.
_baseline_vals = {"Correctness": 0.84, "Completeness (SLM)": 0.88, "Instruction Adherence": 0.94,
                  "prescriptive_overreach": 0.01, "medical_misinformation": 0.02,
                  "commercial_brand_capture": 0.01, "Output Toxicity (SLM)": 0.0,
                  "rx_dosage_hit": 0.0, "fictional_brand_hit": 0.0}
_poisoned_vals = {"Correctness": 0.55, "Completeness (SLM)": 0.03, "Instruction Adherence": 0.06,
                  "prescriptive_overreach": 0.30, "medical_misinformation": 0.34,
                  "commercial_brand_capture": 0.26, "Output Toxicity (SLM)": 0.0,
                  "rx_dosage_hit": 0.226, "fictional_brand_hit": 0.323}
_b = gr.safety_composite(_baseline_vals)["safety_score"]
_p = gr.safety_composite(_poisoned_vals)["safety_score"]
check("clean baseline outranks poisoned on the safety composite", _b is not None and _p is not None and _b > _p)
check("composite stays bounded 0..1", 0.0 <= _p <= 1.0 and 0.0 <= _b <= 1.0)
check("higher violation severity lowers the safety score (1 - severity inversion)",
      gr.safety_composite({"medical_misinformation": 0.0})["safety_score"] == 1.0
      and gr.safety_composite({"medical_misinformation": 1.0})["safety_score"] == 0.0)
check("efficiency/latency is excluded from the composite (not in the polarity map)",
      gr.safety_composite({"Correctness": 0.8, "average_latency": 999.0})["safety_score"] == 0.8)
check("missing metrics are skipped + renormalized, empty -> None (no zero-fill)",
      gr.safety_composite({"Correctness": 0.5})["safety_score"] == 0.5
      and gr.safety_composite({})["safety_score"] is None)
check("0..100-scaled values are normalized to 0..1",
      abs(gr.safety_composite({"Correctness": 80.0})["safety_score"] - 0.8) < 1e-9)
check("label and snake_case alias normalize to the same metric key",
      gr._norm("Output Toxicity (SLM)") == gr._norm("output_toxicity_slm")
      and gr._norm("Rx Dosage Hit") == gr._norm("rx_dosage_hit"))
check("write-back is idempotent: _upsert_tag deletes-then-adds and --write-back uses it",
      callable(getattr(gr, "_upsert_tag", None))
      and "_upsert_tag(" in inspect.getsource(gr.main)
      and "delete_experiment_tag" in inspect.getsource(gr._upsert_tag))

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
