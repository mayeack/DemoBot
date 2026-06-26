---
layout: default
title: Section 0 — Overview
nav_order: 3
---

# Section 0 — Overview: The Single Pane of Glass
{: .no_toc }

**Pillar:** Overview · **Tool:** Splunk — AI Governance Overview dashboard · **Timing:** 8–10 min
{: .fs-5 .fw-300 }

1. TOC
{:toc}

---

## Objective

Establish the thesis and orient the executive: every governed turn and its quality/security scores live in one view, and you drill from there into any pillar.

## Step by step

1. Open the **AI Governance Overview** dashboard in Splunk; set the time range to **Last 7 days**.
2. Walk the **KPI and safety tiles** left to right and read them aloud:
   - **Total AI Requests** — every turn the platform governed.
   - **Safety Violations / Policy Blocked / Guardrails Triggered** — non-compliant content stopped or flagged at runtime (Cisco AI Defense + internal policy).
   - **PII Detected** — sensitive-data detections (the `ai_cim:pii:ml_scoring` pipeline correlates by `event_id`).
   - **Total Token Usage / Total Cost** — spend as a first-class governance signal.
   - **Unique Sessions** — breadth of activity.
3. Walk the **GenAI / ML Detection summaries** and the cost / latency / volume trends. The Overview previews all four governance dimensions (Measure, Secure, Observe, Govern) in one surface; the dedicated pillar dashboards in the same TA are the deep-dives, one click away.
4. Land on the **Recent AI Requests (Detailed Log)** table at the bottom. Pick a flagged turn, copy its `event_id`, and run the pivot below to show it resolves to the **full correlated record** (Cisco Agent Observability score + AI Defense verdict + PII/injection/hallucination scores). **Do not dwell** — this is the teaser for [Lab 4](lab-4-govern.html).

   ```spl
   index=gen_ai_log "<event_id>"
   ```

## Facilitator talking points

- "Enterprises are shipping agentic AI faster than they can govern it. This is the gap, on one screen."
- "Every number you see comes from the **same governed turns**. Quality, security, operations, and audit aren't four tools — they're four views of one record, joined on `gen_ai.event.id` / `trace_id`."
- "Point tools each see a slice. One Cisco captures the interaction **once** and correlates the rest together. That's the difference between four investigations and one."

## Expected result

The KPI tiles are populated and the Govern table has rows. Over a rolling ~7–30 day window you should see roughly:

| Signal | Range |
|---|---|
| Governed turns | ~170–190 across ~165 sessions, ~50 end-users |
| Policy blocks | ~11–14 |
| Prompt-injection attempts (ML-detected) | ~12 |
| Hallucinations flagged | ~90 |
| PII/PHI hits | ~17 |
| Toxic hits | ~56 |
| Tokens | ~120K |
| Avg latency | ~8s |

{: .caution }
> Use **ranges** — don't read out invented precision.

---

[Next: Lab 1 — Measure →](lab-1-measure.html){: .btn .btn-primary }
