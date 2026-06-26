---
layout: default
title: Lab 1 — Measure
nav_order: 4
---

# Lab 1 — MEASURE (Cisco Agent Observability)
{: .no_toc }

**Pillar:** Measure · **Tool:** Cisco Agent Observability (+ Splunk `genai_scoring`) · **Timing:** 12–15 min · **Outcome:** Improved Outcomes
{: .fs-5 .fw-300 }

1. TOC
{:toc}

---

## Objective

Before you guard or operate anything, define and measure "good." Run a **baseline-vs-poisoned** evaluation, see it scored by **Luna**, read token/cost and the **signals** that surface the unknown unknowns, and promote a finding to a guardrail.

{: .positioning }
> Cisco Agent Observability is the quality/evaluation pillar — **a Cisco product**, formerly Galileo, now owned by Cisco. It's where you define "good" first; the other three pillars enforce, operate, and prove against the baseline it sets. Splunk also runs its own `genai_scoring` hallucination/toxicity pipeline — **both complement** each other, and both join the same turns on `gen_ai.event.id` / `trace_id`.

## Step by step

### 1. Run the baseline-vs-poisoned eval

The side-by-side experiment is staged by an offline script (app must be up; `GALILEO_*` set in `.env`):

```bash
venv/bin/python scripts/demo/galileo_eval_prescription.py
```

This runs the same benign patient prompts through two arms — a **baseline** (control) arm and a **poisoned** arm (`force_boundary_injection`, which deterministically appends a prescription-overreach block; synthetic, no real patient data) — and fans both to Cisco Agent Observability.

### 2. Read the two arms side by side

In **Cisco Agent Observability** (project `YeackBot`), open the experiment and compare the arms:

- the **baseline** holds the line (OTC-only, ~0 violations);
- the **poisoned** arm oversteps (prescription / dosage recommendations).

The metrics are scored by **Luna**, so this runs continuously without a frontier-model bill.

### 3. Read token/cost and the signals

Show the **token usage and cost per arm**, then open **Signals / Insights** — the unknown-unknowns view (anomalous clusters, drift, novel failure modes no rubric anticipated).

### 4. Continuous-scoring example (wired)

Stage the fabricated-treatment turn and show Cisco Agent Observability flag it even though nothing "broke":

```bash
venv/bin/python scripts/demo/seed_governance_scenarios.py --only 5
```

(Scenario 5, *"Hallucination-risk response"* — sends a benign symptom prompt with a forced hallucination injection: a fabricated **"Cryogenic Micro-Circulation Enhancement (CME)"** treatment and an invented **"lateral accessory nerve branch,"** scored at **risk ~0.75** with a **model-written explanation**.)

### 5. Cross-reference in Splunk

Open Section 0's **Measure** section ("Hallucination detections & avg risk over time"), sourced from `genai_scoring`. Two independent evaluators, one turn.

### 6. Promote the finding to a guardrail

Note that the poisoned arm's prescription-overreach is the exact behavior you'll author as a custom response-direction guardrail in **[Lab 2](lab-2-secure.html)** — the eval defines it, the guardrail enforces it.

## Facilitator talking points

- "You can't guard a behavior you haven't defined. So governance starts here — with measurement."
- "Baseline versus poisoned, side by side, scored by Luna — Cisco's small purpose-built evaluators. That's what makes continuous LLM-as-judge scoring affordable, not a frontier bill."
- "Beyond the metrics we defined, **signals** surface the unknown unknowns — the failures no one wrote a rubric for. And token and cost per arm mean we see what each behavior *costs* to serve."
- "'Risk 0.75, here's why' — the platform doesn't just flag the hallucination, it explains it. That's what makes the finding defensible — and promotable to a guardrail in the next lab."

## Expected result

Cisco Agent Observability shows the baseline-vs-poisoned delta (baseline ~0 violations, poisoned arm overstepping), token/cost per arm, and signals; the wired hallucination turn is flagged and also appears in Splunk's `genai_scoring` Measure panel. The **Hallucinations Flagged** KPI reflects the activity.

{: .caution }
> If Cisco Agent Observability shows no traces, confirm `GALILEO_*` is set in `.env` (the app loads the corp CA on import). Splunk's `genai_scoring` Measure panels are the fallback.

---

[← Section 0](section-0-overview.html){: .btn } [Next: Lab 2 — Secure →](lab-2-secure.html){: .btn .btn-primary }
