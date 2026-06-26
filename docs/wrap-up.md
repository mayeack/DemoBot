---
layout: default
title: Wrap-Up & Outcomes
nav_order: 8
---

# Wrap-Up & Outcomes
{: .no_toc }

**Timing:** 5 min · Return to the Section 0 dashboard and close the loop.
{: .fs-5 .fw-300 }

1. TOC
{:toc}

---

## Close the loop

Return to the **Section 0 dashboard**:

- "Everything you saw — the measurement, the block, the latency spike, the injection — is on **this one screen**, on the **same turns**, joined on `gen_ai.event.id` / `trace_id`."
- Tie each lab to its outcome.
- "Secure. Observable. Governed. Measurable. One Cisco, end to end — and the same pattern generalizes: this engine re-skins to six verticals (medical, tax, benefits, legal, finance, telecom)."

## The five executive outcomes

| Outcome | Pillar | Grounded in |
|---|---|---|
| **Unified Visibility & Control** | Overview | the single pane (Section 0) |
| **Improved Outcomes** | Measure | evaluation, signals & continuous metrics ([Lab 1](lab-1-measure.html)) |
| **Trusted AI** | Secure | exposed → compliant live ([Lab 2](lab-2-secure.html)) |
| **Operational Excellence** | Observe | APM trace-to-root-cause ([Lab 3](lab-3-observe.html)) |
| **Accountability & Evidence** | Govern | immutable audit trail for every event ([Lab 4](lab-4-govern.html)) |

---

## Optional live moment — deterministic emergency escalation

If you have time and want to show the human-in-the-loop safety path, run scenario 2 (*"Emergency symptom → escalation"*):

```bash
venv/bin/python scripts/demo/seed_governance_scenarios.py --only 2
```

This opens a session with an emergency presentation (e.g. crushing chest pain radiating down the left arm, shortness of breath, cold sweats) to trip the **deterministic escalation rules** — routing the emergency to a human-review queue rather than letting the model advise. Show it in `/governance-ui` as `escalated=true`.

**Talking point:** "Some decisions shouldn't be the model's to make. The platform routes them to a human — by rule, not by chance."

---

## Reset between deliveries (soft reset)

1. Stop any active incident:
   ```bash
   curl -u x:$ACCESS_KEY -X POST http://localhost:8001/api/incident/stop
   ```
2. Re-stage fresh, correlated demo data so the next room sees recent activity:
   ```bash
   venv/bin/python scripts/demo/seed_governance_scenarios.py
   ```
3. In the AI Defense console, **revert the Lab 2 policy** to its pre-tuned (blocking) state so the blocked → compliant moment works again.
4. Reset the Section 0 dashboard time range to **Last 7 days** and refresh.

See the [Reference page](reference.html#reset--teardown) for full teardown.

---

[← Lab 4](lab-4-govern.html){: .btn } [Reference & Troubleshooting →](reference.html){: .btn .btn-primary }
