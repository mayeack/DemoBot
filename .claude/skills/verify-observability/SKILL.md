---
name: verify-observability
description: Run the observability regression test after ANY change that affects the Splunk Observability Cloud (OpenTelemetry) integration. Triggers when editing backend/telemetry/otel.py, otel-collector-config.yaml, run.sh (OTEL section), run-collector.sh, the OTel/splunk packages in requirements.txt, backend/agents/llm.py, backend/agents/graph.py, backend/agents/nodes/*, or .env OTEL_*/SPLUNK_* settings — i.e. anything that changes how gen_ai spans/metrics are produced, exported, or forwarded. Confirms telemetry still reaches Splunk APM + AI Agent Monitoring with correct metadata (model, token usage).
---

# Verify the Splunk Observability integration

DemoBot emits OpenTelemetry GenAI telemetry → a **local OTel Collector**
(`run-collector.sh`, `otel-collector-config.yaml`) → **Splunk Observability
Cloud** (realm `us1`). The app exports OTLP to `localhost:4317`; the collector
forwards traces (APM) and metrics (signalfx, `send_otlp_histograms: true`).

**This pipeline breaks silently.** The two failure modes seen in production:
1. **The collector process dies** (e.g. the laptop sleeps) → the app keeps
   running and *generating* telemetry, but every export fails
   `StatusCode.UNAVAILABLE` and nothing reaches Splunk. Symptom: "my last
   interaction didn't show up in Observability Cloud."
2. **Metadata regressions** — the `create_react_agent` + LangChain auto-
   instrumentation does not emit token usage or the request model, so the app
   emits `gen_ai.client.token.usage` itself (`backend/telemetry/otel.py`
   `record_genai_tokens`, called from `backend/agents/llm.py`). A change to the
   agent/LLM/telemetry code can silently drop this again.

## When to run (REQUIRED before considering an observability change done)

Run the regression test after editing any of:
- `backend/telemetry/otel.py`
- `otel-collector-config.yaml`, `run-collector.sh`
- `run.sh` (the `opentelemetry-instrument` / OTEL_* section)
- `backend/agents/llm.py`, `backend/agents/graph.py`, `backend/agents/nodes/*`
- the OpenTelemetry / `splunk-*` packages in `requirements.txt`
- `.env` `OTEL_*` / `SPLUNK_*` keys (or the Python version of `venv/`)

## How to run

```bash
# Both the app and the collector must be running first:
./run-collector.sh    # terminal 1  (the piece that dies on sleep)
./run.sh              # terminal 2
./tests/observability/verify_observability.sh
```

Exit 0 = pass. The script (takes ~80s; sends 2 real chat turns):
- **Tier 1** — collector (`:4317`/`:8888`) and app (`:8001`) are up. A FAIL here
  is the #1 incident → start the collector: `./run-collector.sh`.
- **Tier 2** — spans + metric points are actually forwarded to Splunk with
  **zero** export failures (reads the collector's own `:8888` counters).
- **Tier 3** — `gen_ai.client.token.usage` (with a real, non-`unknown_model`
  model and input/output token types) and `gen_ai.client.operation.duration`
  are queryable in O11y. Needs `SPLUNK_API_TOKEN` (an O11y **API** token, not the
  ingest token) in `.env`; skipped with a notice if unset.

## Known residual limitation (not a regression)
`gen_ai.client.operation.duration` carries `gen_ai.request.model=unknown_model`
because the LangChain auto-instrumentation can't read the request model on the
`create_react_agent` path. The correct model is on `gen_ai.response.model`
(spans) and on the app-emitted `gen_ai.client.token.usage` metric. Don't "fix"
this by removing `record_genai_tokens`.

## Interpreting Splunk
- **APM** → service `demobot-v3` → traces/agent spans (`workflow … → step
  domain → step call_model → chat`).
- **APM → AI Agent Monitoring**, Environment `demobot-local` → Requests,
  Tokens, Latency. Token panels are fed by `gen_ai.client.token.usage`.
