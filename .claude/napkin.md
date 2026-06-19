# Project Napkin — medadvice_v3

Curated, high-value runbook. Read before work; keep only recurring guidance.

## Testing — keep regressions in sync
- **Every material change updates its regression test in the same change** — new
  behavior, bug fix, or integration change. Don't just run tests; extend them so
  the new/fixed behavior is asserted and a future regression fails loudly.
  Observability → `tests/observability/` (run via `verify_observability.sh`).

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
- GenAI Agent + LLM telemetry is emitted by the app via the **opentelemetry-util-genai
  TelemetryHandler** — `genai_agent_invocation` / `genai_llm_invocation` in
  `backend/telemetry/otel.py`, wired into `invoke_agent` / `invoke_chat` in
  `backend/agents/llm.py`. This is what puts the named agent in O11y's "AI agents"
  view and reports the real model. Don't revert to raw spans or the old manual
  token metric (`record_genai_tokens`, removed — it would double-count). The buggy
  auto LangChain instrumentor is disabled (`OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=langchain`
  in `run.sh`) so it can't double-emit with `model=unknown`.
- Cost KPI shows **$0** because Splunk's server-side pricing lookup doesn't include
  the current Claude models (all Claude usage prices to $0 in this org; older priced
  Claude IDs like `claude-3-5-sonnet` are deprecated/404 with our key). Not a bug —
  it auto-populates if/when Splunk prices `claude-sonnet-4-5`. OpenAI models *are*
  priced in this org.

## Galileo (LLM observability) — second telemetry destination
- Two paths, both **no-op without `GALILEO_API_KEY`**: (1) the OTel Collector fans
  the gen_ai spans to Galileo — `otlphttp/galileo` → `https://api.galileo.ai/otel/traces`
  in `otel-collector-config.yaml`, creds injected by `run-collector.sh`; (2) a per-turn
  SDK trace with governance metadata (safety/PII/toxicity/policy/eval) via
  `GalileoLogger` in `backend/galileo_integration.py`, fanned out from
  `governance_logger._write_log` on a daemon thread (mirrors the HEC fan-out).
- Used `GalileoLogger`, NOT the LangChain `GalileoCallback` — the governance flags are
  computed by graph nodes AFTER the LLM call, so a callback (which fires at the call)
  can't carry them. Net effect: **2 traces per turn** in Galileo (collector OTel + SDK);
  drop the collector `otlphttp/galileo` exporter if you want a single trace source.
- **CA gotcha:** the Galileo SDK's httpx needs the corp CA (`SSL_CERT_FILE` →
  `ca-bundle.pem`, set by `backend.config` at import). The app is fine (it imports
  `backend.config`); a standalone script must `import backend.config` FIRST or it gets
  `httpx.ConnectError`. The collector (Go) uses the system keychain, so it's unaffected.
- `run.sh` exports `GALILEO_*` to the app process; project/log stream = `YeackBot`/`default`.
  The `galileo` pkg bumped `httpx`→0.28.1 + `pydantic-settings`→2.14.1 (in requirements.txt).

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
