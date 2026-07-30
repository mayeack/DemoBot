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
- **Anything asserting an injection DIRECTIVE must pin `settings.ai_provider`.**
  `build_input_directives` emits labeled `* SAMPLE:` text for censored providers
  but natural UNLABELED asks for ollama (and routes hallucination + authority into
  the JSON answer contract instead of an appended block). Five `test_api.py`
  checks silently asserted only the labeled markers, so the suite was RED in the
  shipped `.env` config (ollama) and nobody noticed. It now pins the provider per
  block and asserts both builders.
- **Other standalone suites:** `test_guardrail_nodes.py` (safety / policy /
  compliance / prompt+response defense / intake driven directly with a synthetic
  state — AI Defense stubbed, no network; catches a guardrail that silently
  no-ops, which the stream stage-name checks cannot) and `test_db_integrity.py`
  (per-session SQLite connections, JSON-column persistence, escalation-reason
  isolation, JSONL write serialization — runs against a temp DB, never
  `medadvice.db`).
- **`test_api.py` writes to the LIVE `./medadvice.db`.** Its settings PUTs persist
  to the same AppSettings row the launchd app reads; it now snapshots and restores
  `logs_directory` + emit-model. Any new check that mutates persisted settings
  must do the same, or it silently reconfigures the running demo.

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
- **Blocked turns have no `response.model` to echo** — use
  `governance_logger.active_response_model()` (never `settings.anthropic_model`,
  the old bug: on Ollama every blocked event read as a Claude turn, and
  `executive_fields`/Galileo both prefer `response_model` over `request_model`,
  so the wrong value won). Response-side blocks *do* have a real model + token
  usage — pass `llm_model`/`usage_data` (`nodes/defense.py`) or they report
  zeros. Regression: `venv/bin/python tests/test_provider_setting.py`.
- `estimated_cost` is an app-side estimate (Splunk prices Claude to $0); price map
  is in `executive_fields._PRICES_PER_MTOK`. Real `hallucination_score`/
  `groundedness_score` come from Splunk GenAI Scoring (`gen_ai_log` sourcetype
  `genai_scoring`) + Galileo, not the app — overlay passes them through if present.
- Demo seeder: `scripts/demo/seed_governance_scenarios.py` (10 safe-synthetic
  scenarios via `/api/chat`; needs app running + `ACCESS_KEY`).

## Galileo (LLM observability) — second telemetry destination
- Two paths, both **no-op without `GALILEO_API_KEY`**: (1) the OTel Collector fans
  the gen_ai spans to Galileo — the `otlphttp/galileo` exporter + its pipeline live
  in **`otel-collector-galileo.yaml`**, an OVERLAY that `run-collector.sh` layers on
  with a second `--config` ONLY when the key is set (they used to sit
  unconditionally in the base config with possibly-empty creds, so keyless boxes
  POSTed span content — prompts and responses — with an empty API-key header).
  Validate config changes without emitting telemetry:
  `./bin/otelcol-contrib validate --config otel-collector-config.yaml --config otel-collector-galileo.yaml`;
  the overlay references a processor/receiver defined only in the base, so a clean
  validate also proves the merge works. (2) a per-turn
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

## Galileo Agent Control ("Agent Observability Controls" toggle)
- **Two different auth schemes on `agent-control.multitenant.galileocloud.io`.**
  Management (`/api/v1/controls`, `/agents/initAgent`, `/agents/{a}/controls/{id}`,
  `/evaluators`) accepts `Authorization: Bearer <JWT>` where the JWT comes from
  `POST https://api.multitenant.galileocloud.io/v2/login/api_key {"api_key": …}`.
  A raw `X-API-Key: $GALILEO_API_KEY` is rejected 401 on every endpoint.
  Do instead: always exchange the API key for the JWT first; the OpenAPI spec is
  public at `<base>/openapi.json` (no auth) — read it instead of guessing.
- **`POST /api/v1/evaluation` takes ONLY a minted HS256 runtime token**
  (`/api/v1/auth/runtime-token-exchange`), and that exchange returns **502
  AUTH_UPSTREAM_REJECTED** for org `splunkse` regardless of target_type (verified
  2026-07-29) — a tenant-side grant, not a payload bug (`/control-bindings` 502s
  identically). Presenting the console JWT instead gets
  `401 Token is missing the "iat" claim`. Do instead: don't wait on it — the app
  now falls back to **client-side execution** (below). Re-probe the exchange
  periodically; when it clears, the server path resumes automatically.
- **Client-side execution is the working transport today.** `execution: "sdk"`
  semantics, hand-rolled in `backend/services/agent_control.py`: read the agent's
  control definitions from the management API (console JWT is enough) and score
  through the core API's `POST /scorers/invoke`. `galileo_agent_control_execution`
  = `auto` (default, server-preferred) | `server` | `client`. Deliberate deviation:
  the official engine SKIPS controls whose `execution` != its context
  (`agent_control_engine/core.py`), we run `server` ones locally and log once —
  keep 259 as `execution: "server"` so it inherits the server path for free later.
  Don't add the `agent-control-sdk` dependency: its Luna evaluator mints an
  internal JWT from a secret we don't have, so the public `/scorers/invoke` is the
  only option, not a shortcut.
- **`/scorers/invoke` works for SLM scorers and is BROKEN for every LLM-judge
  scorer** (verified 2026-07-29): `correctness`, `context_adherence`,
  `instruction_adherence`, `ground_truth_adherence`, custom LLM scorers → HTTP 200
  with `status:"failed", error_message:"'str' object has no attribute
  'format_content_for_message'"` for EVERY input shape (plain strings, message
  lists, content parts). Both `code` scorers error in their own code. Working:
  `Output PII (SLM)` → `["ssn","phone_number"]`, `context_adherence_luna` → float.
  So hallucination cannot be measured at runtime yet — Galileo-side defect.
  Do NOT substitute `context_adherence_luna` for factuality: with no retrieval
  context it scored **0.025 on a badly hallucinated answer vs 0.111 on a correct
  one**, so `lt 0.5` there blocks nearly everything. Galileo's OFFLINE metric path
  for `correctness` works fine (that's the Logs-view column) — it is post-ingestion
  and cannot gate a live response.
- Controls are per-AGENT: a console control does nothing until attached to the
  registered agent (`demobot-agent`). Do instead:
  `venv/bin/python scripts/demo/register_agent_control.py --attach <id>`.
- Hallucination control **259** = `galileo.luna` + preset scorer **`correctness`**
  (`89d579ce-…`, alias `factuality`), scope `stages:[post] step_types:[llm]`,
  selector `path:"*"` (the judge needs question AND answer), operator **`eq`**,
  threshold **`false`**. Definition is checked in at
  `scripts/demo/controls/259-block-hallucinated-output.json`; apply with
  `register_agent_control.py --set-data 259 <file>` (`PATCH /controls/{id}` only
  edits name/enabled — a condition change needs `PUT /controls/{id}/data`).
  **The operator is load-bearing:** `correctness` emits a BOOLEAN, and
  `coerce_number()` returns None for bools, so a numeric operator (`lt 0.5`, the
  original config) raises `"score False is not numeric"` → evaluator error →
  fail-open → the control can never fire. `any` is inverted (fires on *correct*).
- Output-PII control **263** = `DemoBot-block-output-pii`, the one that actually
  ENFORCES today (`Output PII (SLM)`, `88eada48-…`, is an SLM scorer so it invokes).
  Definition: `scripts/demo/controls/block-output-pii.json`. The scorer returns a
  **list** of categories and `contains` takes one category, so the condition is an
  **`or` of `contains` leaves** — with the per-evaluation memo that costs ONE judge
  call, not one per leaf (the memo key was originally shadowed by the payload
  loop variable; 8 leaves = 8 calls until that was fixed).
  **`name` is deliberately excluded**: a normal answer naming a care provider
  ("contact Dr. Robert Anderson") scores `['name']`, so `operator:"any"` or a
  `name` leaf withholds legitimate responses. Verified: PII-injected turn →
  blocked; same prompt without injection → allowed.
- **The block message must stay generic.** `_handle_agent_control_block` serves
  every deny control, so wording that asserts one cause ("not factually reliable")
  misdescribes a PII block. The matched control is in `safety_categories`.
- **A deny control whose evaluator errored is reported under `errors`, not
  `matches`,** while the engine still sets `is_safe:false`. Parsing only `matches`
  reads that as a clean pass — neither blocking nor honoring fail-open. Guarded in
  `_parse_response` + `tests/test_agent_control.py`.
- `GET /agents/{name}/controls` nests the definition under **`control`**;
  `/controls/{id}` uses **`data`**. Accept either or every field reads as None.
- Node order is `compliance -> agent_control -> response_defense`, so Cisco AI
  Defense stays the last word on output. Regression:
  `venv/bin/python tests/test_agent_control.py`.

## Cisco AI Defense — outbound API integration, fail-closed
- "Connecting" a box to AI Defense = six `AI_DEFENSE_*` vars in that box's `.env`;
  the **API key is the binding** to the SCC application/connection (org Yeack
  Industries → app **YeackBot** → API connection). `push-replica.sh` installs the
  Mac's `.env` verbatim, so fleet replicas inherit the connection for free — a box
  deployed *before* the Mac was configured has a stale `.env` and must be
  redeployed or patched + `systemctl restart demobot-app`.
- **`AI_DEFENSE_FAIL_OPEN=False` means an unreachable/401 Inspection API blocks
  EVERY prompt** — indistinguishable from working enforcement unless you also test
  a benign prompt. Never "fix" that by flipping it True (silently disables the
  guardrail while the UI still shows protection). Diagnose with the raw curl.
- Leave `AI_DEFENSE_ENABLED_RULES` **empty**: the connection is SCC-policy-bound,
  so explicit rules get HTTP 400 and fall back to the console policy anyway.
- Full procedure + 4-tier verification: skill `connect-ai-defense`.

## Chat latency (2026-07-15 remediation — where the time goes)
- **Multi-Agent Mode defaults OFF (2026-07-28):** a default turn = 1 Ollama
  call (the theme's domain agent / synthesizer). The 3-4 sequential calls
  (coordinator → 1-2 specialists → synthesizer) run only with the drawer
  toggle ON (`multi_agent_mode=true`); non-UI callers must opt in explicitly
  (auto-prompter + demo/eval scripts already do). Guard tests:
  `test_default_mode_is_single_agent` + the stream-default checks in
  `tests/test_api.py`.
- A multi-agent turn = 3-4 sequential Ollama calls (coordinator → 1-2
  specialists → synthesizer). Per-agent wall-clock is in the governance event
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
- **Rotate ACCESS_KEY:** edit `.env`, then `launchctl kickstart -k gui/501/com.yeack.medadvice-app`
  (key is read at startup — no hot reload). Rotation logs out every browser
  (cookie = sha256 of key). The OpenClaw gateway bakes the key in at container
  start — rerun `./run-openclaw.sh` if `demobot-openclaw` is up. Verify old→401 /
  new→200 on both :8001 and medadvice.yeackbot.com.

## OpenClaw agentic surface (Mode C — opt-in demo)
- Gives an OpenClaw agent real tools so the demo can show agentic tool abuse
  governed at the tool boundary (`/api/toolguard/inspect` = tool_policy + Cisco
  AI Defense). `./run-openclaw.sh` (or `./start-all.sh --agentic`); off by
  default. `verify_openclaw_observability.sh` after touching the guard/plugin.
- **Runs in podman, NOT a host npm install.** Cisco Secure Endpoint (AMP)
  quarantines the npm `openclaw` entrypoint within ~2 min, repeatably. The
  gateway is the `demobot-openclaw` image (pinned `openclaw@2026.7.1-2`) on
  :18789. See the [[openclaw-edr-podman]] memory.
- **`openclaw/` is tracked but NOT checked out on this Mac.** AMP deletes
  OpenClaw files from the working tree, so the directory is excluded via
  sparse-checkout — nothing on disk to delete, and `git status` stays clean.
  Everything is still in `HEAD` and on GitHub, so a remote `git clone` gets it
  all. **Never `git restore openclaw/`** — that just hands AMP a target again.
  - Read/edit it with `scripts/openclaw-edit.sh` (`--list`, `--show <path>`, or
    `<path>` to edit): it round-trips through git object storage, so the repo
    path is never written. Edits land **staged** — commit them.
  - Setup, once per clone: `scripts/openclaw-edit.sh --apply-sparse` and
    `git config core.hooksPath .githooks`. The hook re-applies the exclusion in
    new worktrees (sparse state is per-worktree and is not inherited).
- **The image is built from git, plugin baked in.** `run-openclaw.sh` pipes
  `git archive HEAD:openclaw` into `podman build` as the build context and
  stamps the tree hash as the `demobot.openclaw.tree` label; a mismatch triggers
  a rebuild. So a plugin change takes effect once **committed**, not once staged,
  and the first build after a change costs a few minutes (npm install).
- **podman VM only shares `$HOME`.** A bind mount from `/Applications/DemoBot`
  fails `statfs ...: no such file or directory` — which is why the plugin is
  baked into the image rather than mounted. The remaining mounts are the state
  dir (`~/.demobot-openclaw`) and the decoy workspace (`~/DemoBotDecoy`).
- **The gateway runs as `--user 0:0`, NOT `--userns=keep-id`** (fixed 2026-07-30;
  it crash-looped before that with `EACCES ... mkdir '/home/node/.openclaw/state'`).
  keep-id maps the host user to the same uid, but the image's default `node` user
  (uid 1000) maps to a subuid that owns nothing, so the host-owned mounts stayed
  unwritable (`keep-id:uid=1000,gid=1000` doesn't bridge it either on macOS
  podman). Rootless podman already maps container uid 0 to the unprivileged host
  user, so container root gains **no host privilege** — it just makes the uid
  match the mount owner, which lets `$STATE_DIR` stay mode **600** (it holds the
  gateway token + `ACCESS_KEY`). Don't "fix" a future EACCES by loosening those
  perms; check the uid mapping first.
- **The guard is fail-closed:** with `TOOL_GUARD_ENABLED=True`, a dead app
  (:8001) denies EVERY agent tool call — looks like a broken agent, not a broken
  app. `TOOL_GUARD_ENABLED` gates *enforcement* only (default False = the
  unguarded control run); the app never calls the gateway, so `podman stop
  demobot-openclaw` is the real off switch.
- Agent model is Ollama **`llama3.2:3b`** (tool-capable); `dolphin3:8b` has no
  tools template. Decoy workspace: `venv/bin/python scripts/demo/seed_agentic_decoy.py`.

## Database (SQLite) — concurrency
- **Never put the file-backed SQLite engine on `StaticPool`.** It hands ONE sqlite3
  connection to every Session, and this app writes from both the event loop
  (`Depends(get_db)` handlers) and threadpool workers (chat turns → governance /
  escalation / audit rows via `get_db_context`). Measured cost of the shared
  connection: **12 of 40 rows lost** with 8 concurrent writers, failing with
  "cannot commit - no transaction is active" / NULL identity key — errors the
  governance writer only logs. `check_same_thread=False` stays (connections do
  cross threads). StaticPool IS still correct for `:memory:`, where a second
  connection is a second empty database. Guard: `tests/test_db_integrity.py`.
- `Conversation.messages` is a plain JSON column (**no MutableList**), so mutating
  the loaded list in place and assigning it back compares the attribute against
  itself and the column is dropped from the UPDATE. Copy on load
  (`list(row.messages or [])`) and assign a new list back.

## Environment
- venv is **Python 3.11** (the Splunk GenAI stack needs ≥3.10; 3.9 silently breaks
  the LangChain instrumentation). `venv.py39.bak/` is the old 3.9 venv.
- Secrets live only in `.env` + `medadvice.db` (both gitignored). Never commit them.
