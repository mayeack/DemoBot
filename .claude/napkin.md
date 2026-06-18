# Project Napkin — medadvice_v3

Curated, high-value runbook. Read before work; keep only recurring guidance.

## Observability (Splunk O11y) — CRITICAL
- **After ANY change to the observability integration, run
  `./tests/observability/verify_observability.sh`** (the `verify-observability`
  skill lists the file triggers). It catches the #1 incident: a dead local
  collector silently dropping all telemetry.
- Pipeline = app (OTLP :4317) → local collector (`./run-collector.sh`) → Splunk
  us1. **Both the app AND the collector must be running.** The collector dies when
  the laptop sleeps; the app keeps generating telemetry but every export fails
  `StatusCode.UNAVAILABLE`. Fix = `./run-collector.sh` (restarting the app alone
  does nothing).
- Token usage is emitted by the app itself (`otel.record_genai_tokens` in
  `backend/telemetry/otel.py`, wired into `backend/agents/llm.py`) because the
  LangChain auto-instrumentation misses token usage + request model on the
  `create_react_agent` path. Don't remove it. (`operation.duration` still shows
  `model=unknown_model` — known limitation, not a regression.)

## Running the app
- `./run.sh` (local, :8001) launches under `opentelemetry-instrument` when
  `SPLUNK_ACCESS_TOKEN`/OTLP is set in `.env`. `./tunnel.sh` for a public
  Cloudflare URL (ephemeral; needs the app running).
- Access gate: `ACCESS_KEY` in `.env` (currently word-style). Log in at `/login`,
  or `curl -u x:$ACCESS_KEY`. `/health` is the only open route.

## Environment
- venv is **Python 3.11** (the Splunk GenAI stack needs ≥3.10; 3.9 silently breaks
  the LangChain instrumentation). `venv.py39.bak/` is the old 3.9 venv.
- Secrets live only in `.env` + `medadvice.db` (both gitignored). Never commit them.
