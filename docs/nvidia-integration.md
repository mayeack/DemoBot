# NVIDIA integration: local NIM, NeMo Guardrails, NemoClaw, and the AI Virtual Assistant blueprint

DemoBot 4.2 makes NVIDIA's stack a first-class, opt-in part of the governed-AI
demo. Nothing that existed before changed its default; every piece below is
switched on deliberately.

| Piece | Where you turn it on | What it needs |
|---|---|---|
| `provider=nvidia` (local NIM) | header **Provider** dropdown | an NVIDIA GPU **on this host** + a NIM container on `localhost:8000` |
| Nemotron 3 Super / Nano | header **Model** dropdown (per NIM image) | the GPU the image documents (Nano: 1× A10G; Super: 8× H100) |
| **NeMo Guardrails** | Demo Controls drawer toggle | `nemoguardrails` installed + master switch on the Settings card |
| **NemoClaw Guardrails** | Demo Controls drawer toggle | nothing for the policy layer; Docker/Colima + a GPU host for the runtime |
| **Blueprint** dropdown | chat header | nothing — both architectures share every guardrail |
| Session analytics | `GET /api/analytics/...` | nothing (uses the active model on demand) |

## 1. `provider=nvidia` is local inference, always

`provider=nvidia` means a **NIM (NVIDIA Inference Microservice) container running
on the same host as the app**, serving an OpenAI-compatible API on
`http://localhost:8000/v1`. It is never the hosted API catalog: a non-loopback
`NVIDIA_BASE_URL` is rejected at every seam (chat-model factory, legacy client,
Settings save, startup re-apply) with the reason. `NVIDIA_API_KEY` is only for a
NIM started behind an API-key gate; `NGC_API_KEY` is the image-pull credential
used by the EC2 bootstrap.

- **This Mac has no NVIDIA GPU**, so the provider is greyed out in the header
  with the reason ("provider=nvidia is local NIM inference on this host's NVIDIA
  GPU — none detected"). That is decided server-side (`backend/host_capabilities.py`,
  published on `GET /api/server-info` as `gated`) — never guessed by the UI.
- On a **GPU replica**: `deploy/ec2/ec2-bootstrap.sh --with-nim [model]` installs
  Docker + the NVIDIA Container Toolkit, logs into `nvcr.io` with `NGC_API_KEY`,
  runs `demobot-nim.service` (loopback `:8000`) and sets
  `AI_PROVIDER=nvidia`, `NVIDIA_BASE_URL`, `NVIDIA_MODEL` in that box's `.env`.
- The **Model** dropdown lists the featured NIM images with their GPU requirement
  (`NVIDIA_FEATURED_MODELS`) plus whatever the running NIM serves; an image the
  detected GPU cannot run, or the NIM does not serve, is disabled with a tooltip.
  A NIM serves one model, so switching models means running a different image.
- **Nemotron 3** models default reasoning ("thinking") **on**. DemoBot sends
  `chat_template_kwargs.enable_thinking=false` unless `NVIDIA_REASONING=True`
  (Settings card checkbox), because the answer contract is a JSON block; a stray
  inline `<think>` trace is stripped for every provider.
- `nvidia/nemotron-3-super-120b-a12b` needs 8× H100-80GB (p5-class). On the
  `g5.xlarge` fleet use `nvidia/nvidia-nemotron-nano-9b-v2` (the default).

Verify: `./run.sh` prints the host-capability line and the NIM preflight;
`curl http://localhost:8000/v1/health/ready` → 200; a chat turn's governance
event shows `provider_name=nvidia` and the NIM's model id.

## 2. NeMo Guardrails (drawer toggle)

`nemoguardrails` 0.24 runs **in-process** (core package only — the `[server]`
extra would break the FastAPI/httpx pins). Input rails run on the prompt after
Cisco AI Defense's prompt inspection; output rails run on the answer after
Galileo Agent Control and **before** AI Defense's response inspection, so Cisco
stays the last word.

- The judge for the self-check rails is the **active chat model**, injected as
  `LLMRails(config, llm=…)`, so the toggle works on every provider with no cloud
  call. NemoGuard content safety is an optional **second local NIM**
  (`NEMO_GUARDRAILS_CONTENT_SAFETY_URL`, loopback only).
- Rails live in `guardrails/nemo/` (`config.yml`, `prompts.yml`):
  `self_check_input`, `self_check_output`, and DemoBot's **overreach** output rail
  (the NeMo counterpart of the AI Defense custom guardrail).
- A fired rail withholds the turn through the shared governance contract:
  `guardrail_ids=["nemo_guardrails"]`, the rail names in `safety_categories`,
  a `⚠️ POLICY BLOCKED (NVIDIA NeMo Guardrails - input|output)` banner. Errors
  honor `NEMO_GUARDRAILS_FAIL_OPEN` (default open).
- Setup: `venv/bin/pip install -r requirements.txt` (through Artifactory — see
  Troubleshooting), then Settings → **NVIDIA NeMo Guardrails** → Enabled, then
  the drawer toggle. Regression: `tests/test_nemo_guardrails.py`.

## 3. NemoClaw Guardrails (drawer toggle) — policy layer + runtime

NVIDIA NemoClaw runs an OpenClaw agent inside an OpenShell sandbox governed by a
declarative policy (deny-by-default egress, filesystem scopes, process rules,
local-only inference). DemoBot adopts it at two levels:

**Policy layer (works everywhere).** `guardrails/nemoclaw/policy.yaml` is an
OpenShell-shaped policy DemoBot evaluates on every agent tool call the gateway
submits to `/api/toolguard/inspect` — network egress by host/port/path,
read/write scopes, denied binaries, the privacy-router rule (model calls only
to local endpoints), and an optional NeMo rail over sensitive calls. Unlike
`TOOL_GUARD_ENABLED`, **the drawer toggle is the enforcement switch**: ON means a
NemoClaw policy block denies the call (`enforced_by: ["nemoclaw"]`), attributed
as `guardrail_ids=["nemoclaw_guardrails"]` with `NemoClaw: …` rule names.

**Runtime (GPU replica, or a Mac with Colima).** `run-nemoclaw.sh` builds
NemoClaw's sandbox image with the `demobot-toolguard` plugin baked in
(`nemoclaw/Dockerfile`), onboards the sandbox non-interactively, writes the
guard URL + access key inside it, applies the `demobot-guard` network preset (a
real host IP — NemoClaw's docs discourage `host.docker.internal`), enables
OpenShell's OCSF JSON audit log and runs `scripts/nemoclaw/ocsf_forwarder.py`,
which tails the sandbox's denials into `/api/toolguard/nemoclaw/events`. The
plugin's `after_tool_call` observer reports a `policy_denied` tool result to
`/api/toolguard/observe` immediately. Both paths land in Splunk/Galileo as
`nemoclaw_guardrails` governance events with an `execute_tool` span; the drawer
pill reads **RUNTIME** while denials are arriving, **POLICY** otherwise.

- NemoClaw supports Linux with Docker Engine (Ubuntu 24.04 primary), macOS Apple
  Silicon with Docker Desktop or **Colima**, and WSL2 — **Podman is unsupported**.
  This Mac runs Mode C on podman, so the primary NemoClaw host is an EC2 replica:
  `deploy/ec2/ec2-bootstrap.sh --with-nemoclaw` (adds `demobot-nemoclaw` +
  `demobot-nemoclaw-forwarder` units, since NemoClaw restarts nothing after a
  reboot). On the Mac: `brew install colima docker && colima start`, then
  `./run-nemoclaw.sh`.
- The sandboxed agent's own inference provider is NemoClaw's (`--provider build`
  = NVIDIA endpoints via `NVIDIA_INFERENCE_API_KEY`, or `--provider nim` for a
  NemoClaw-managed local NIM). DemoBot's `provider=nvidia` stays local regardless.
- Verify: `./tests/observability/verify_nemoclaw_observability.sh`.

## 4. Blueprints (header dropdown) and the parity rule

`backend/agents/blueprints/` holds the selectable architectures:

- **DemoBot Multi-Agent** (`demobot_multi_agent`, default): intake →
  [coordinator → specialists] → synthesizer.
- **NVIDIA AI Virtual Assistant** (`nvidia_virtual_assistant`): a faithful port of
  NVIDIA-AI-Blueprints/ai-virtual-assistant — `fetch_record → ask_clarification →
  primary_assistant → sub_assistant → respond`. The primary assistant routes by
  calling a `To<Sub>Assistant` tool (native tool calling when the provider
  supports it, a JSON tool-call plan otherwise); the sub-assistant runs with the
  blueprint's tools applied — `retrieve_knowledge` (retrieval over
  `blueprint_data/<theme>/docs`, keyword by default or a local embedding NIM via
  `BLUEPRINT_EMBED_URL`) and `lookup_record` (a synthetic per-session record);
  the responder is the DemoBot synthesizer, so the answer contract is identical.
  Multi-Agent Mode here = up to two sub-assistants. Session analytics mirror the
  blueprint's analytics service: `GET /api/analytics/sessions`,
  `/session/summary`, `/session/conversation`, `POST /feedback/{kind}`.

Both blueprints are wired into the **same** guardrail chain by
`blueprints/guardrails.py` (`PRE_NODES` / `POST_NODES`), so a guardrail,
toggle or governance field is implemented once and both architectures get it —
parity by construction. Each blueprint names its own workflow
(`demobot_multi_agent` / `demobot_nvidia_virtual_assistant`) and every
governance event carries an additive `blueprint` field, so Splunk/Galileo can
compare them. The rule is in `CLAUDE.md` ("Blueprint feature parity"); the
detector is `tests/test_blueprint_parity.py`, which runs the same scenario matrix
(benign, every blocking guardrail, forced injections, generation error,
Multi-Agent Mode) through both and fails on any divergence.

## 5. Verification

```bash
PY=venv/bin/python ./tests/run_all.sh          # every standalone suite
./tests/observability/verify_observability.sh   # OTel -> Splunk (needs collector + app)
./tests/observability/verify_openclaw_observability.sh
./tests/observability/verify_nemoclaw_observability.sh
```

## 6. Troubleshooting

- **`pip` says "No matching distribution" for everything** (e.g. `nemoguardrails`):
  the Artifactory token in `~/.pip/pip.conf` has expired (they last ~12 h). Run
  `dev-login artifactory`, then install again. The EC2 bootstrap installs from
  public PyPI and is unaffected.
- **`provider=nvidia` greyed out / "NIM DOWN"**: the host has no NVIDIA GPU, or no
  NIM answers `http://localhost:8000/v1/health/ready`. Settings → Host
  capabilities → ↻ re-probes after starting one.
- **NemoClaw Guardrails pill never says RUNTIME**: the runtime is not on this host
  (podman-only Mac) — the policy layer still enforces; run the runtime on a
  replica.
- **Nemotron answers wrapped in a reasoning trace**: reasoning was enabled
  (`NVIDIA_REASONING`); DemoBot strips a leading `<think>` block, but the JSON
  contract is more reliable with it off.
