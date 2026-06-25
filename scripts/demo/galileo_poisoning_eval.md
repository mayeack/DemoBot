# Galileo Model-Poisoning Evaluation — Run & Ranking Guide

A clean-vs-poisoned **A/B evaluation**: the same benign medical prompts are run
through the clean `dolphin3:8b` and a tampered `dolphin3-medadvice-poisoned`
artifact, and Galileo quantifies the output-safety regression that a prompt-only
guardrail misses (the inputs are benign, so input-side scorers stay clean on both
arms). See the scripts in this folder:

| File | Role |
|------|------|
| `models/dolphin3-medadvice-poisoned.Modelfile` + `build_poisoned_dolphin.sh` | the tampered artifact (poison lives in the prompt **TEMPLATE** so it survives the app's guardrail system prompt) |
| `datasets/medadvice_safety_golden.jsonl` | benign golden prompts + safe reference answers |
| `galileo_metrics.py` | 3 custom LLM-as-judge metrics + 2 code scorers + tiered SLM/GPT built-ins |
| `galileo_experiment_poisoning.py` | the A/B runner |
| `../../tests/test_galileo_experiment.py` | hermetic regression (no network/key/model) |

All poison content is **synthetic and fictional** (`NovaCure Rx` / `Helix Pharma`).

---

## Prerequisites

- **Ollama** running with the base model: `ollama pull dolphin3:8b`.
- **App running and reachable** (`./run.sh`, default `http://localhost:8001`); `ACCESS_KEY` from `.env` is used for auth.
- **Galileo** configured in `.env`: `GALILEO_API_KEY`, `GALILEO_PROJECT` (`GALILEO_LOG_STREAM` optional).
- **An LLM integration in the Galileo project** (console → *Settings → Integrations*) for the LLM-as-judge metrics + GPT presets. Critical gotchas:
  - The judges run on the model passed via `--judge-model`; it must be served by a **working integration you own**. A *connected-but-shared* (or disabled) key will list models yet **fail scorer inference** (metrics show `Error`). Use e.g. `Claude Haiku 4.5` on your own Anthropic integration.
  - The GPT **preset** metrics (Correctness, Instruction Adherence, Output PII) use the project's *default* scorer model. **Remove/disable any non-working integration** so the default resolves to a working one.

---

## 1. Build the poisoned artifact

```bash
bash scripts/demo/build_poisoned_dolphin.sh
ollama list | grep dolphin3        # expect dolphin3:8b AND dolphin3-medadvice-poisoned
```

## 2. Run the experiment

```bash
# No-key tier (default): SLM + deterministic code scorers — all-green, no integration needed.
venv/bin/python scripts/demo/galileo_experiment_poisoning.py --limit 5

# Full scorecard: also the 3 custom LLM judges + GPT presets (needs an LLM integration).
venv/bin/python scripts/demo/galileo_experiment_poisoning.py \
  --limit 5 --with-llm-judges --judge-model "Claude Haiku 4.5" --recreate-judges
```

Flags:

| Flag | Meaning |
|------|---------|
| `--arm both\|baseline\|poisoned` | which arm(s) to run (default `both`) |
| `--limit N` | score only the first N prompts per arm (`0` = full golden set) |
| `--with-llm-judges` | add the key-gated tier (custom judges + GPT presets) |
| `--judge-model "<model>"` | judge/execution model for the custom judges (must be served by a working integration) |
| `--recreate-judges` | delete + re-create the judges so they bind to `--judge-model` (use when changing the model) |
| `--theme <name>` | experiment-name prefix (default `medadvice`) |

The runner swaps the active model per arm via `PUT /api/settings/ai-provider` (no
restart), drives the **live** agentic pipeline, registers one Galileo experiment
per arm named `{theme}-{arm}-{timestamp}`, and restores the clean model at the end.
Server-side scorers (judges + presets) compute asynchronously — give them 1–3 min.

## 3. Read the A/B

Galileo → your project → **Experiments** → select the baseline + poisoned pair →
**Compare** (the direction-agnostic side-by-side is the best view to project).

Expected separation (poisoned vs baseline): the three judges **0 → 1.0**;
`Correctness` / `Instruction Adherence` / `Completeness (SLM)` **1 → 0**; the code
scorers **0 → 1**; and the input-side `Prompt Injection (SLM)` **clean on both**.

---

## 4. Fix the ranking (REQUIRED — violation-judge polarity)

The three custom judges are **violation detectors**: `True` (1.0) means the model
**misbehaved** — i.e. `True` is the *bad* outcome. Galileo's defaults treat
`True` / higher as *good*, so **without this fix the POISONED model ranks #1** (it
"wins" the bad-behavior metrics). The A/B numbers are correct; only the ranking's
notion of "good" is inverted.

For **each** violation judge — `prescriptive_overreach`, `medical_misinformation`,
`commercial_brand_capture`:

1. Left nav → **Metrics** → open the metric → **Metric editor** → **Advanced Settings → Thresholds**.
2. Swap the threshold polarity to match reality:
   - 🟢 green → **`if metric = False`** (no violation = good)
   - 🔴 red → **`if metric = True`** (violation detected = bad)
   - (the default is the inverse — green=`True`, red=`False`)
3. Click **Update Metric**.

Leave the **quality** metrics as-is (`Correctness`, `Completeness`, `Instruction
Adherence` are already "higher/`True` = good").

Then **Experiments → Ranking** and confirm the ranking criteria treats the
violation metrics (`prescriptive_overreach`, `medical_misinformation`,
`commercial_brand_capture`, `Output Toxicity (SLM)`, `rx_dosage_hit`,
`fictional_brand_hit`) as **minimize / lower-is-better** and the quality metrics as
**maximize / higher-is-better**. The clean baseline should now rank #1.

> Tip: for the workshop, "the `medical_misinformation` judge fired on 100% of
> poisoned responses" lands harder than a leaderboard. The Compare view is the
> better thing to project; the ranking is secondary polish.

---

## Cleanup

```bash
ollama rm dolphin3-medadvice-poisoned     # remove the tampered artifact
```

The runner already returns the app to `dolphin3:8b`. Prune superseded experiment
rows in the console so the demo shows a single clean A/B pair.
