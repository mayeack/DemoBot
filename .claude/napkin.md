# Project Napkin — medadvice_v3

Curated, high-value runbook. Read before work; keep only recurring guidance.

## Testing — keep regressions in sync
- **Every material change updates its regression test in the same change** — new
  behavior, bug fix, or integration change. Don't just run tests; extend them so
  the new/fixed behavior is asserted and a future regression fails loudly.
  Observability → `tests/observability/` (run via `verify_observability.sh`).
- **API surface → `tests/test_api.py`** (`venv/bin/python tests/test_api.py`): a
  standalone TestClient suite over `backend.main:app` asserting auth gating (401
  w/o key), happy-path contracts, and validation (422) for every endpoint. It's
  side-effect-safe (LLM boundary + auto-prompter stubbed; incident `drive_traffic`
  false) so it makes no real Anthropic call / load / emission. **Needs httpx
  0.27.x** — httpx 0.28 dropped the `app=` shortcut that Starlette 0.35's
  TestClient uses (pinned in `requirements.txt`; the app itself never used `app=`).
- **Flag breaking/behavior changes to the user.** When a change alters an existing
  endpoint, response field, feature, or telemetry contract — or bumps a dependency
  in a way that changes runtime behavior — say so by name in that turn; never let
  it pass silently. `test_api.py` + `verify_observability.sh` are the detectors.

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
- **The "AI trace data" span LIST view needs span CONTENT, not just gen_ai metadata.**
  It's indexed by `gen_ai.input.messages`/`gen_ai.output.messages` (captured via
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY`). `invoke_chat`/
  `invoke_agent` pass `system`+`messages` to `genai_*_invocation` and call
  `otel.record_genai_output(...)` to attach them — drop that and spans still reach
  APM (visible in **APM → Trace view / Agent flow**) but the AI-trace-data list is
  EMPTY. Metrics-only checks pass anyway (`spans.count` only tracks SERVER entry
  spans, so it can't even see the gen_ai CLIENT spans). Guard:
  `tests/observability/test_genai_span_content.py` (verify_observability.sh Tier 0);
  confirm via Trace view, not metrics.
- Cost KPI shows **$0** because Splunk's server-side pricing lookup doesn't include
  the current Claude models (all Claude usage prices to $0 in this org; older priced
  Claude IDs like `claude-3-5-sonnet` are deprecated/404 with our key). Not a bug —
  it auto-populates if/when Splunk prices `claude-sonnet-4-5`. OpenAI models *are*
  priced in this org.

## Executive governance overlay (Section 0 dashboard) — additive contract
- Every governance event carries a flat **executive overlay** derived in
  `backend/logging/executive_fields.py` (`derive_executive_fields`), called from
  `create_governance_log` (`log_schemas.py`) right before the None-strip. Fields:
  `app_name, user_type, risk_score(0-100), policy_action(allow|warn|flag|block),
  policy_name, model_name, agent_name, tool_name, prompt_category, contains_pii,
  contains_phi, hallucination_score, groundedness_score, latency_ms, token_count,
  estimated_cost, business_outcome, human_escalation, audit_status`.
  Do instead: it's **additive/non-breaking** — never rename existing
  `gen_ai.*`/governance fields (Splunk props alias them). Inputs come from
  `governance_node`/`policy.py` passing `severity/theme/agent_name/workflow_name/
  hallucination_*`. Regression: `venv/bin/python tests/test_executive_fields.py`.
- `estimated_cost` is an app-side estimate (Splunk prices Claude to $0); price map
  is in `executive_fields._PRICES_PER_MTOK`. Real `hallucination_score`/
  `groundedness_score` come from Splunk GenAI Scoring (`gen_ai_log` sourcetype
  `genai_scoring`) + Galileo, not the app — overlay passes them through if present.
- Demo seeder: `scripts/demo/seed_governance_scenarios.py` (10 safe-synthetic
  scenarios via `/api/chat`; needs app running + `ACCESS_KEY`).

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

## Chat latency (2026-07-15 remediation — where the time goes)
- A turn = 3-4 sequential Ollama calls (coordinator → 1-2 specialists →
  synthesizer). Per-agent wall-clock is in the governance event
  (`agent_trace[].duration_ms`) and AI Defense time in `stage_timings` — check
  `logs/ai_governance.json` FIRST when "latency is high"; no log archaeology.
- **Host memory pressure rules Ollama.** With Splunk ES resident, system_free
  is ~3-5GB, so the 3B internal + 8B synthesizer models can't co-reside — each
  turn pays a 3B↔8B swap (`sched.go "predicted to exceed available memory,
  evicting"` in `~/.ollama/logs/server.log`). Still fastest measured config:
  3B split = 38s/turn vs all-8B = 55s. Don't "fix" by reverting
  OLLAMA_MODEL_INTERNAL without re-measuring both.
- `POST /api/chat/message/stream` (SSE) streams one stage frame per graph node,
  then a final frame AFTER response_defense — never stream synthesizer tokens
  directly (bypasses the output guardrail). Old JSON endpoint stays for
  compatibility; frontend falls back to it.
- `launchctl setenv` from a Claude/SSH shell does NOT reach the GUI launchd
  domain that spawns Ollama.app — the com.yeack.ollama-env agent applies at
  login. Verify what the daemon ACTUALLY got via the "server config" line in
  `~/.ollama/logs/server.log`, not `launchctl getenv`.
- `scripts/demo/build_poisoned_dolphin.sh` refuses to run while the app serves
  or a model is loaded (mid-generation `ollama cp/rm` stalls turns + evicts the
  resident model — observed 2026-07-15). `--force` overrides.

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
