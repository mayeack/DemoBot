---
layout: default
title: Reference & Troubleshooting
nav_order: 9
---

# Reference & Troubleshooting
{: .no_toc }

1. TOC
{:toc}

---

## App surfaces (access-gated; log in at `/login`)

`/app` · `/admin-ui` · `/governance-ui` · `/settings-ui` · `/health` (open)

Base URL: `http://localhost:8001`.

## Key API endpoints

- `POST /api/chat/message` — a governed turn
- `POST /api/chat/session/new` — start a session
- `POST /api/incident/start` — fault injection (`latency_ms`, `error_rate`, `duration_s`, `drive_traffic`)
- `POST /api/incident/stop` · `GET /api/incident/status`

## Demo scenarios

The repo's executive demo seeder drives 10 governed-AI scenarios through the live app. Each turn is **safe and synthetic** — PII/PHI, toxicity, and hallucination signals come from the app's own `force_*` injection switches, not real content.

```bash
# All scenarios:
venv/bin/python scripts/demo/seed_governance_scenarios.py
# Specific ids, with a pause between turns and an explicit base URL:
venv/bin/python scripts/demo/seed_governance_scenarios.py --only 4,5 --delay 1.0 --base http://localhost:8001
```

| # | Scenario | Expected | Used in |
|---|---|---|---|
| 1 | Normal safe medical advice | `advice_delivered`, risk low, `policy_action=allow` | baseline / warm-up |
| 2 | Emergency symptom → escalation | `human_escalation=true`, `business_outcome=escalated_to_human` | Wrap (optional) |
| 3 | PHI / PII-heavy response | `contains_phi=true`, `policy_action in {flag,block}` | Lab 2 |
| 4 | Prompt injection attempt | AI Defense `SECURITY_VIOLATION` / block when enabled | Lab 4 |
| 5 | Hallucination-risk response | `hallucination_detected=true`, risk elevated | Lab 1 |
| 6 | Low-confidence medical prompt | `prompt_category=low_confidence_medical` or clarification | — |
| 7 | Policy-violating (toxic) response | `toxic_detected=true`, `policy_action=flag` | Lab 2 (alt) |
| 8 | Self-harm → hard policy block | `policy_action=block`, `business_outcome=blocked_unsafe` | Lab 2 (alt) |
| 9 | Token / cost-heavy turn | high `token_count` + `estimated_cost` | — |
| 10 | Multi-turn, escalating risk profile | `risk_score` rises across turns within one session | — |

The baseline-vs-poisoned **eval** for Lab 1 is a separate offline script:

```bash
venv/bin/python scripts/demo/galileo_eval_prescription.py
```

{: .caution }
> **Draft note — reconcile against the canonical facilitator collateral.** The facilitator guide refers to a `run-demo-sequence.sh <moment>` wrapper (moments: `all`, `clean`, `measure`, `secure`, `incident`, `govern`, `pii`, `escalate`). That wrapper is **not** in the DemoBot repo — the repo-native equivalents above (`seed_governance_scenarios.py` + the incident API) are what's checked in. If you maintain `run-demo-sequence.sh` separately, keep the two in sync.

## Correlation key

Every turn is **server-assigned** a shared `request_id` / `trace_id` / `gen_ai.event.id`. The whole story — Cisco Agent Observability quality score, AI Defense verdict, PII/injection/hallucination scores, latency, cost — joins on it. That is the One Cisco differentiator.

## Data sources

- `index=gen_ai_log` — `medadvice3:json`, `ai_cim:prompt_injection:ml_scoring`, `ai_cim:pii:ml_scoring`, `genai_scoring`
- `index=cloud_llm_apis` — `anthropic:analytics:cost/usage`, `anthropic:compliance:activity`
- `cisco:aipod:*` · `cisco:asa`

## Telemetry / service names

- OTel service name: **`demobot-v3`** (`OTEL_SERVICE_NAME` in `.env`). Use `service=demobot-v3` in Splunk Observability Cloud APM.
- Cisco Agent Observability project: **`YeackBot`** (`GALILEO_PROJECT`), log stream `default`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Section 0 KPI tiles all read 0 / empty audit table | No demo data in window, or wrong time range | Set dashboard to **Last 7 days**; run `seed_governance_scenarios.py` and wait a few seconds |
| App returns 401 on every request | Access gate — missing/invalid key | Use `curl -u x:$ACCESS_KEY`; in the browser log in at `/login`. `ACCESS_KEY` is in `.env` |
| Lab 1 baseline-vs-poisoned eval shows nothing | The eval is the offline script, not the seeder | Run `scripts/demo/galileo_eval_prescription.py` directly (app up, `GALILEO_*` set); or pre-capture the Cisco Agent Observability console |
| Observe / APM latency charts flat or stale; no new spans in Splunk | **Collector is down** (the #1 incident — it dies when the laptop sleeps) | Restart it: `./run-collector.sh`. Restarting the app alone does **not** fix this — both processes must run |
| App serves but no telemetry reaches Splunk | OTLP not configured / not launched under instrumentation | Confirm `SPLUNK_*`/`OTEL_*` in `.env`; `run.sh` auto-launches under `opentelemetry-instrument` when OTLP is set. Re-run `./run.sh` |
| Seeder prints "(non-JSON response)" | App not up, or wrong base / `ACCESS_KEY` | `curl -s http://localhost:8001/health`; pass `--base` or ensure `.env` has `ACCESS_KEY` |
| Incident won't stop / latency stuck high | Incident still in its window | It auto-expires; to force-stop: `curl -u x:$ACCESS_KEY -X POST http://localhost:8001/api/incident/stop` |
| Lab 2 no longer shows a block | Policy left in its tuned (permissive) state from a prior run | In AI Defense, revert the governing policy to its blocking state, then re-run the scenario |
| Cisco Agent Observability shows no traces | `GALILEO_API_KEY` unset, or SDK CA issue | Confirm `GALILEO_*` in `.env`; the app loads the corp CA on import. Splunk's `genai_scoring` Measure panels are the fallback |
| Cost KPI shows $0 | Splunk's server-side pricing lookup doesn't price the current models in this org | **Not a bug.** Use the **Tokens** tile as the cost proxy; it auto-populates if/when the model is priced |
| Section 0 dashboard not found / empty | AI Governance TA not visible to this user, or wrong time range | Confirm `TA-gen_ai_cim` is enabled and shared to your role; open `/app/TA-gen_ai_cim/ai_governance_overview` and set **Last 7 days**. (No-TA fallback: import `ai-governance-overview.xml` as a Classic dashboard.) |
| Correlated-record pivot returns nothing | `event_id` empty on that row, or wrong time range | Pick a row with flags, copy its `event_id`, and run `search index=gen_ai_log "<event_id>"` (Last 7 days) |

---

## Reset / teardown

**Soft reset (between deliveries — keep the environment running):** see [Wrap-Up](wrap-up.html#reset-between-deliveries-soft-reset).

**Full teardown (end of day / event):**

1. Stop any active incident: `curl -u x:$ACCESS_KEY -X POST http://localhost:8001/api/incident/stop`
2. Stop the app (Terminal 2): `Ctrl-C` in the `./run.sh` terminal.
3. Stop the collector (Terminal 1): `Ctrl-C` in the `./run-collector.sh` terminal.
4. If a public tunnel was used, stop `./tunnel.sh` (it's ephemeral and tears down on exit).
5. Log out of the four consoles (Cisco Agent Observability, AI Defense, Observability Cloud, Splunk Core) if on shared hardware.

{: .note }
> The historical demo data in `index=gen_ai_log` persists by design — it's what powers the rolling 7–30 day proof points. You don't need to delete it between sessions.

---

*Owners: Matt Poland · Michael Yeack · Gerry D'Costa.*
