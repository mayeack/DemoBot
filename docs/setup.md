---
layout: default
title: Setup & Prerequisites
nav_order: 2
---

# Setup & Prerequisites
{: .no_toc }

Do this **10–15 minutes before the room arrives.** All commands run from the **repo root** of your local [DemoBot](https://github.com/mayeack/DemoBot) checkout.
{: .fs-5 .fw-300 }

1. TOC
{:toc}

---

## At a glance

| | |
|---|---|
| **Audience** | CISO, CIO, CTO, Chief Risk/Compliance, VP AI/Platform — plus the delivering SEs |
| **Duration** | 60–90 minutes (core path 60; full path with Q&A 90) |
| **Demo app** | **MedAdvice** — a live multi-agent medical-advice chatbot (FastAPI + LangGraph) at `http://localhost:8001`, built on the **DemoBot** engine (OTel `service=demobot-v3`) |
| **Correlation key** | every turn is server-assigned a shared `request_id` / `trace_id` / `gen_ai.event.id` |

{: .positioning }
> **Read before you deliver.** This is an honest demo. Cisco Agent Observability is the quality/evaluation pillar — **a Cisco product** (formerly Galileo, now owned by Cisco). Splunk Observability Cloud and Splunk Core are **distinct** products. Cisco AI Defense is the **live** runtime-security layer. Cite only the proof points in this guide (ranges are fine); never invent customer names, ROI percentages, or quotes.

---

## Live instances (all four must be reachable from the room)

| Pillar | Product | What it must do |
|---|---|---|
| Measure | **Cisco Agent Observability** | Quality/eval scoring (baseline-vs-poisoned, hallucination, drift), token/cost, signals. *A Cisco product — formerly Galileo.* |
| Secure | **Cisco AI Defense** | Live API integration inspecting prompt (pre-LLM) and response (post-LLM) against multiple guardrails: PII, PHI, PCI, Harassment, Hate, Profanity, Sexual, Violence, Social Division, Prompt Injection, Code |
| Observe | **Splunk Observability Cloud** | APM tracing for `service=demobot-v3`; latency/error detectors |
| Govern | **Splunk Core / Enterprise Security** | `index=gen_ai_log` + correlated ML-scoring pipelines and audit feeds |

### Data indexes (confirm populated before you start)

- `index=gen_ai_log` — governance turns (`sourcetype=medadvice3:json`) **plus** correlated ML-scoring pipelines: `ai_cim:prompt_injection:ml_scoring`, `ai_cim:pii:ml_scoring`, `genai_scoring` (hallucination + toxicity) — all joined on `gen_ai.event.id` / `trace_id`, over **Cisco Data Fabric**.
- `index=cloud_llm_apis` — Anthropic cost + compliance/audit feeds (`anthropic:analytics:cost/usage`, `anthropic:compliance:activity`).
- `cisco:aipod:*` — Cisco AI infrastructure; `cisco:asa` — Cisco security.

### Access and accounts

- The MedAdvice app is **access-gated**. Log in at `/login`, or call APIs with `curl -u x:$ACCESS_KEY`. `ACCESS_KEY` lives in `.env`. `/health` is the only open route.
- Splunk user with read on `gen_ai_log` and `cloud_llm_apis`, and access to the AI Governance TA apps (`TA-gen_ai_cim`, `TA-cloud_llm_apis`, `TA-cisco_ai_pod`).
- Cisco AI Defense console access (to show/tune policy in Lab 2).
- Cisco Agent Observability project access (project **`YeackBot`**, log stream `default`).

### Local machine / room setup

- Python **3.11** venv (the Splunk GenAI stack needs ≥3.10).
- `.env` configured with `ANTHROPIC_API_KEY`, `ACCESS_KEY`, `SPLUNK_*`/`OTEL_*`, and `GALILEO_*` (the Cisco Agent Observability SDK keys still use the `GALILEO_*` names). See [`.env.example`](https://github.com/mayeack/DemoBot/blob/main/.env.example).
- Browser tabs pre-opened (see [Open the app UIs](#2-open-the-app-uis)).
- Optional public sharing: a Cloudflare tunnel via `./tunnel.sh` (ephemeral; needs the app running). Useful for a remote room.

{: .caution }
> **Rehearse once, end to end, the day before.** Verify all four consoles authenticate, seed the governance scenarios, and confirm rows land in the Section 0 dashboard within seconds. For Lab 1, also dry-run the baseline-vs-poisoned eval (`scripts/demo/galileo_eval_prescription.py`).

---

## Environment Setup

### 1. Start the telemetry collector, then the app

The Observe pillar depends on a **two-process pipeline**: the app emits OTLP on `:4317`, and a local OpenTelemetry collector forwards it to Splunk. **Both must be running** — if the collector is down, the app keeps serving but every telemetry export silently fails.

```bash
# Terminal 1 — the OpenTelemetry collector (forwards gen_ai spans/metrics to Splunk + Cisco Agent Observability)
./run-collector.sh

# Terminal 2 — the MedAdvice app (launches under opentelemetry-instrument when OTLP is set)
./run.sh
```

Or bring up both at once with `./start-all.sh`. Confirm the app is serving:

```bash
curl -s http://localhost:8001/health
```

### 2. Open the app UIs

Log in once at `/login` with the access key; the session covers all UIs.

| Tab | URL | Used in |
|---|---|---|
| Chat | `http://localhost:8001/app` | Labs 1–4 |
| Governance dashboard | `http://localhost:8001/admin-ui` | Section 0, Lab 4 |
| Per-session audit | `http://localhost:8001/governance-ui` | Lab 4 |
| Settings (HEC + demo toggles) | `http://localhost:8001/settings-ui` | Pre-flight, troubleshooting |
| Login | `http://localhost:8001/login` | Access gate |

### 3. Open the Section 0 dashboard (AI Governance TA)

The single pane of glass is **already installed** — no dashboard to build.

1. In Splunk, go to **`TA-gen_ai_cim` → AI Governance Overview**, or browse to `/app/TA-gen_ai_cim/ai_governance_overview`.
2. Set the time-range picker to **Last 7 days**.
3. (Optional) set the **Model ID** / **App Name** filters to focus on the demo app.

The dashboard reads `index=gen_ai_log` (+ the `genai_scoring` pipelines), so it populates from the live demo data. *(No-TA fallback: import `collateral/dashboards/ai-governance-overview.xml` as a Classic dashboard.)*

### 4. Pre-stage the demo data

Run the reusable demo seeder so the canonical moments exist before you present. They appear in Splunk within seconds, correlated by `event_id`.

```bash
# App must be up (./run.sh). ACCESS_KEY is read from .env.
# Stage ALL 10 governed-AI scenarios (baseline + escalation + PHI/PII + injection +
# hallucination + low-confidence + toxic + self-harm + token/cost + multi-turn):
venv/bin/python scripts/demo/seed_governance_scenarios.py

# Or stage only specific scenarios by id, with a pause between turns:
venv/bin/python scripts/demo/seed_governance_scenarios.py --only 4,5 --delay 1.0
```

Each turn flows through the full pipeline (policy → AI Defense → domain → safety → injection → compliance → AI Defense → governance) and is server-assigned a shared `request_id` / `trace_id` / `event_id`. See the [scenario → lab map](reference.html#demo-scenarios) on the Reference page.

{: .note }
> **No real patient data.** PII/PHI, toxicity, and hallucination signals are produced by the app's own synthetic `force_*` test-injection switches, not by sending real sensitive content.

{: .caution }
> **Leave the Observe / APM incident for live demo in [Lab 3](lab-3-observe.html)** — it is disruptive and auto-expires, so don't pre-stage it.

**Quick smoke test:** open the Section 0 dashboard, set Last 7 days, and confirm the KPI tiles are non-zero and the Recent AI Requests log has rows. If they're empty, see [Troubleshooting](reference.html#troubleshooting).
