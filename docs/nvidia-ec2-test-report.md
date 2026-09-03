# NVIDIA stack test report — DemoBot 4.2.1 on a g5.xlarge EC2 box (+ Mac baseline)

**Date:** 2026-09-02 · **Branch:** `fix/ec2-nim-nemoclaw-bootstrap` (from `main` 112708a, PR #55) ·
**Account:** personal AWS 177835492378, us-east-1 · **Box:** 1 × g5.xlarge (A10G 24 GB), 150 GB gp3 ·
**Stack under test:** `provider=nvidia` local NIM (`nvidia/nvidia-nemotron-nano-9b-v2`), host-capability
gating, NeMo Guardrails, NemoClaw (policy layer + sandbox runtime), NVIDIA AI Virtual Assistant blueprint,
plus the prior-version (Ollama) regression on the same box.

## 0. Defects found before any GPU time was spent

| # | Where | Defect | Fix |
|---|---|---|---|
| 1 | `deploy/ec2/ec2-bootstrap.sh` | `--with-nim` prepended `AI_PROVIDER=nvidia` to `SETS` in step 9c, **after** step 8 had rewritten `.env`; a NIM box booted on the Mac's `AI_PROVIDER=ollama` with the NIM idle | decision moved to step 3 (right after GPU detection); `tests/test_ec2_scripts.py` asserts the order |
| 2 | `deploy/ec2/ec2-bootstrap.sh` | fleet drop-in kept ~11 GB of Ollama models resident (`OLLAMA_KEEP_ALIVE=60m`); the NIM needs ~20 GB of the A10G's 23 GB | NIM box: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=0`, warm-up keeps its own 5-min keep_alive for the placement check, then explicit unload + `ollama ps` must be empty |
| 3 | `deploy/ec2/fleet.sh`, `push-replica.sh` | no way to pass `--with-nim` / `--with-nemoclaw` through the fleet driver | `FLEET_NIM`, `FLEET_NIM_MODEL`, `FLEET_NEMOCLAW`; push-replica `--with-nim [model]`, `--with-nemoclaw`, refusing without the NGC key in `.env` |
| 4 | `deploy/ec2/ec2-bootstrap.sh` | NemoClaw prerequisites missing (Node ≥ 22.19, `binutils`); NIM readiness wait 15 min; NIM absent from the final health gate; keys checked 20 min in | Node 22 + binutils installed; NIM starts **before** the app with a 40-min wait; `/v1/health/ready` + `/v1/models` gate `BOOTSTRAP_OK`; both keys checked in step 0 |
| 6 | `deploy/ec2/ec2-bootstrap.sh` | every `apt-get` raced Ubuntu's `unattended-upgrades` for the dpkg lock; the first live deploy died at step 9b (`Could not get lock /var/lib/dpkg/lock-frontend`) ten minutes in | all apt calls go through `apt_get()` (waits on the lock via `fuser`, `DPkg::Lock::Timeout=600`); found by the live run, guarded by the test |
| 7 | `deploy/ec2/ec2-bootstrap.sh` | the NIM unit ran the image at its defaults; vLLM pre-allocates the hybrid-Mamba SSM state cache for `max_num_seqs=256` — 33.75 GiB regardless of context — next to 17.35 GiB of weights on the 22 GiB A10G → CUDA OOM, restart loop, never ready (capping the context alone changed nothing) | `NIM_MAX_NUM_SEQS=8` (~1 GiB SSM cache), `NIM_KVCACHE_PERCENT=0.95` and `NIM_MAX_MODEL_LEN=8192` in `/etc/demobot-nim.env`, passed to the container and overridable (16 sequences at 0.90 still left −0.69 GiB for KV blocks); found live, guarded by the test |
| 8 | `nemoclaw/policies/demobot-guard.yaml`, `run-nemoclaw.sh` | the network-policy preset predated NemoClaw's schema (`Preset must declare preset.name`) so the sandbox never got a route to the guard; the sandbox was also pointed at `127.0.0.1`, which inside the sandbox is the sandbox; and the plugin-config write raced the sandbox start (`Connection refused`) and silently kept the loopback URL | preset rewritten in the shipped-preset shape (`preset` header, `network_policies`, node + curl binaries, no `allowed_ips` — the validator refuses it in user presets and pins the `--trusted-private-host` itself); the sandbox reaches the host at its private IP (the validator also refuses loopback and NemoClaw's managed aliases for user presets); config write waits for phase Ready; bootstrap no longer overrides `--host` |
| 9 | `deploy/ec2/ec2-bootstrap.sh` | NemoClaw onboarding ran 10 s after `enable --now` (app not answering yet) and without `NVIDIA_INFERENCE_API_KEY` in its environment (the key only reached the systemd unit's env file), so it died before installing anything | wait for `/health`, export `/etc/demobot-nemoclaw.env`, then run onboarding exactly as the unit does |
| 10 | `backend/host_capabilities.py` | the capability snapshot was probed once at startup and only re-probed on a manual refresh; on a fresh GPU box the NIM comes up 20+ min after the app, so `/api/server-info` said "GPU present but no NIM answering" 45 min after the same app was serving turns on it (Settings chips and the model gate read that) | `current()` returns a snapshot older than the TTL as-is and kicks one background re-probe (stale-while-revalidate); unit test added |
| 11 | `tests/observability/verify_nemoclaw_observability.sh`, `run-nemoclaw.sh` | an `openshell sandbox exec` can hang indefinitely; one Tier 2 check sat for 2 h 51 min on the box and blocked everything queued behind it | every exec bounded (`timeout 60` / `timeout 90`); the script test refuses an unbounded one |
| 5 | `deploy/ec2/fleet.sh` | `next-replica` and `provision` died silently (exit 1, no output) when nothing was claimed yet — empty `grep` under `pipefail` | `\|\| true` on the claimed-replicas pipeline; regression test runs the function with stubbed `aws`/`cloudflared` |

## 1. Mac baseline (no NVIDIA GPU) — proves 4.2 is inert without a GPU and 4.1 flows still work

| Check | Result | Evidence |
|---|---|---|
| `tests/run_all.sh` (24 suites incl. `test_api.py`, new `test_ec2_scripts.py`) | **PASS** 24/24 | `RESULT: 24 passed, 0 failed` |
| `verify_observability.sh` Tier 0-2 (span content, collector, spans + metrics forwarded, 0 export failures) | **PASS** 8/8 | spans 12349→12487, metric points 58881→59598 |
| `verify_observability.sh` Tier 3 (gen_ai metric MTS in O11y) | **FAIL — pre-existing, org-side** | `gen_ai.client.token.usage MTS: 0`, "org is refusing new gen_ai.client.* MTS creation"; known since 2026-07-29 (metric cardinality cap), span-fed views unaffected |
| `verify_nemoclaw_observability.sh` | **PASS** 10/10, Tier 2 SKIP | sandbox not on a podman Mac, by design |
| `GET /api/server-info` gating on a GPU-less host | **PASS** | `provider_nvidia` and `nim_local` disabled with "no NVIDIA GPU on this host" reasons; `nemoclaw_runtime` disabled (podman) |
| Ollama chat turn, default blueprint | **PASS** | `recommendation`, 581 chars; governance `provider=ollama model=mistral-nemo:12b blueprint=demobot_multi_agent policy_action=allow` |
| NeMo Guardrails with the Ollama judge (master switch on, drawer toggle on) | **PASS** | benign turn passes (31 s with rails); jailbreak → `safety_warning` in 4 s, governance `guardrail_ids=['nemo_guardrails'] safety_categories=['self check input']`; master switch restored |
| NVIDIA AI Virtual Assistant blueprint on Ollama (per-request `blueprint`) | **PASS** | record-lookup turn 642 chars, knowledge-retrieval turn 717 chars; governance `blueprint=nvidia_virtual_assistant workflow=demobot_nvidia_virtual_assistant`; `GET /api/analytics/sessions` lists the session; server default unchanged |

Mac manual checks: `RESULT: 12 passed, 0 failed` (script: `mac-checks.sh`, restores every setting it flips).


## 2. Provisioning the box (what actually happened)

`fleet.sh provision` (count 1, 150 GB gp3) launched `i-0196b1ed29d8bfadf` as replica 1 → `https://medadvice1.yeackbot.com`.
Deploy attempt 1 died at step 9b on the dpkg lock (defect 6). Attempt 2 got through Node install, `nvcr.io`
login, the NIM image pull (~10 GB), then the NIM crash-looped three separate ways before serving —
each one a real finding:

1. **402 Payment Required** fetching the model files: the NGC org had no NIM product subscription (a legacy
   84-char key authenticates but the org is not entitled). Fixed on the NVIDIA side by generating a personal
   `nvapi-` key through build.nvidia.com's NIM deploy flow, which also attached the entitlement. The deploy gates
   were relaxed to accept either key shape (`docker login` is the real check).
2. **CUDA OOM, 33.75 GiB** in vLLM's profiling pass at the image defaults (defect 7): the hybrid-Mamba SSM
   state cache for `max_num_seqs=256`. Capping the context alone changed nothing.
3. **"No available memory for the cache blocks"** with 16 sequences at 0.90 utilization: vLLM's own profile read
   weights 16.58 GiB + activation peak 3.90 GiB → KV cache −0.69 GiB. **8 sequences at 0.95 leaves 1.52 GiB of
   KV cache (6228 blocks)** and the NIM came ready with restart count 0.

Everything the fixes were written for was observed working on the box: the provider decision was logged before
the `.env` rewrite and `AI_PROVIDER=nvidia` landed; the GPU placement check passed (`100% GPU`) and the unload
left `0 MiB` used before the NIM started; the dpkg-lock wait absorbed `unattended-upgrades`; Node 22 and
`binutils` were installed; NemoClaw onboarding succeeded once it ran through the unit's environment.

## 3. EC2 test matrix — `provider=nvidia` on the local NIM

Box: g5.xlarge, A10G 23028 MiB, driver 595.91, NIM `nvcr.io/nim/nvidia/nvidia-nemotron-nano-9b-v2:latest`
(profile `vllm-bf16-tp1-pp1`, engine vLLM 0.10.0, V0 because Mamba), `NIM_MAX_NUM_SEQS=8`,
`NIM_KVCACHE_PERCENT=0.95`, `NIM_MAX_MODEL_LEN=8192`. Script: `ec2-matrix.sh` (restores what it flips).

| # | Check | Result | Evidence |
|---|---|---|---|
| A | A10G present; Ollama holds nothing; `demobot-nim` container + all six units active; `/v1/health/ready` 200; `/v1/models` serves the Nemotron id; direct completion with `enable_thinking=false` | **PASS** 10/10 | direct NIM: 23 prompt + 200 completion tokens in 8.4 s; VRAM 20.1 GB used |
| B | `GET /api/settings/ai-provider` → provider=nvidia, NIM ready + served | **FAIL → PASS** | first pass: cached "NIM DOWN" from startup (defect 10); re-run on the fixed app: `ready=True served=[nemotron-nano-9b-v2]`, super greyed |
| B | `GET /api/server-info` gates: `provider_nvidia` ON, `nim_local` ON, `nemoclaw_runtime` ON | **FAIL (nim_local) → PASS** | re-run on the fixed app: all three ON (Docker 29.7 + NVIDIA runtime, Node 22.23) |
| G | `tests/run_all.sh` on the box (24 suites incl. `test_api.py`, the new script suite) | **PASS** 24/24 | `RESULT: 24 passed, 0 failed` |
| B | featured `nemotron-3-super-120b-a12b` listed but not served (greyed) | **PASS** | |
| B | chat turn on the NIM, JSON contract, no `<think>`; governance `provider_name=nvidia model_name=nvidia/nvidia-nemotron-nano-9b-v2 cost 0` | **PASS** | 655 chars in 18.7 s; NIM logged the app's completions |
| B | Multi-Agent Mode turn; `NVIDIA_REASONING=True` turn still clean (strip works) | **PASS** | 817 chars; reasoning-on turn 624 chars, no `<think>` |
| C | NeMo Guardrails with the NIM judge: benign passes; jailbreak → `safety_warning`, governance `guardrail_ids=['nemo_guardrails'] safety_categories=['self check input']`; toggle-off path unchanged; `tests/test_nemo_guardrails.py` | **PASS** 5/5 | benign turn with rails 10 s, jailbreak 8 s (Ollama 12B judge on the Mac: 31 s / 4 s) |
| C | overreach output rail | **not exercised** | the prescription prompt was caught earlier by the input rail (`self check input`), which is the designed order |
| E | NVIDIA AI Virtual Assistant blueprint on the NIM, routing `auto` (native tools) and `json`: record-lookup and knowledge-retrieval turns, governance `blueprint=nvidia_virtual_assistant`, analytics lists the session; `test_blueprint_parity.py`, `test_nvidia_blueprint.py` | **PASS** 10/10 | record turns 31-32 s, 415-490 chars |
| D | NemoClaw policy layer + sandbox runtime: `verify_nemoclaw_observability.sh` Tier 0 (policy, Dockerfile, preset, forwarder, unit suite, plugin selftest), Tier 1 (toggle ON blocks an unlisted egress via the policy layer; a forwarded OCSF denial becomes a `nemoclaw_guardrails` governance event), **Tier 2 live sandbox** (answers status, `demobot-toolguard` enabled inside, the sandbox reaches DemoBot's guard through the applied `demobot-guard` policy) | **PASS** 13/13 (after defects 8, 9, 11 and the `start`-re-provision fix) | sandbox guard URL `http://172.31.7.215:8001`; `Applied preset: demobot-guard`; `runtime_supported` ON; `toolguard/nemoclaw` shows `demobot-guard` among the loaded network policies. First passes failed on exactly the defects listed; one read taken during the script's own `nemoclaw gateway restart` was transiently 11/13 and was 13/13 three minutes later |
| D | NemoClaw drawer pill reaching **RUNTIME** from a real sandbox denial | **not exercised** | this NemoClaw build refused `openshell settings set ocsf_json_enabled` (warning logged), so runtime denials are attributed only through the plugin's `after_tool_call` path, which the plugin selftest and the Tier 1 synthetic feed cover; driving the sandboxed agent into a denied tool call is out of scope here |
| I | performance | measured | see §5 |
| F | Public URL through the box's own tunnel: `/health` 200, `/app` 401 without the key, 200 with it; public API reports provider=nvidia with the NIM ready | **PASS** 4/4 | `https://medadvice1.yeackbot.com` (replica 1's Cloudflare tunnel `demobot-1`) |
| F | Telemetry under `deployment.environment=demobot-ec2-1` | **PASS (spans)** / **known gap (metrics)** | box collector counters: 510 spans → Splunk APM, 147 → Splunk Agent Observability, 10 503 metric points, no `send_failed` counters; `check_o11y_metadata.py … demobot-ec2-1` fails on the gen_ai metric MTS exactly as the Mac does (org-wide metric cap since 2026-07-29, unrelated to 4.2) |

Observation, not a defect: a raw `/v1/chat/completions` call with `enable_thinking=false` and no system prompt still
began with a reasoning-style preamble in plain text ("Okay, the user wants…"); the app's turns, which carry the
JSON answer contract, came back clean, and no `<think>` block appeared anywhere.

## 4. Prior-version regression on the same box: switch back to Ollama

Procedure (matrix section H): stop `demobot-nim`, `PUT /api/settings/ai-provider {"provider":"ollama"}`, chat turns
on `mistral-nemo:12b` and on the poisoned model, the synthetic-PII injection with Cisco AI Defense review, then
back to `provider=nvidia` and `demobot-nim`.

| Check | Result | Evidence |
|---|---|---|
| First Ollama turn immediately after stopping the NIM | **FAIL (transient) → PASS on re-run** | the turn started at 00:15:51 and never completed within the 5-minute client timeout: Ollama logged `llama-server GPU discovery watchdog timed out` and `disabling mmap … due to host memory pressure` while the NIM container's 17 GB of VRAM and its host memory were still being released; the next turn five minutes later worked. Re-run with a 60 s settle and an explicit warm-up: **HTTP 200 in 7.3 s** (343 chars, `recommendation`), governance `provider=ollama model=mistral-nemo:12b action=allow`; second turn 2.1 s |
| Ollama model placement after the switch | **PASS** — `mistral-nemo:12b 8.3 GB 100% GPU`, context 8192, 29-minute keep-alive from the app's per-call `keep_alive` (the daemon default on a NIM box is 0) | `ollama ps` PROCESSOR column |
| Poisoned model `mistral-nemo:12b-poisoned` selectable and answering | **PASS** | 457 chars; governance `provider=ollama model=mistral-nemo:12b-poisoned` |
| Synthetic PII injection + Cisco AI Defense review on the poisoned model | **PASS** | reply `safety_warning`; governance `policy_action=block guardrail_ids=['cisco_ai_defense']`, categories "PII, Prescriptive Overreach" — the 4.1 guardrail chain is intact under 4.2 |
| Back to `provider=nvidia`, NIM restarted and ready | **PASS** | NIM ready after the reload (300 s to `/v1/health/ready` after `systemctl start demobot-nim` with all Ollama models unloaded first; next turn 19.8 s); governance `provider=nvidia` on the next turn |

Operational note for the switch-back: stop the NIM, wait a minute, warm the Ollama model (or accept a slow first
turn); the two cannot share the A10G, which is why the bootstrap keeps Ollama unloaded on a NIM box.

## 5. Performance on the same A10G (g5.xlarge)

| | Ollama `mistral-nemo:12b` Q4_K_M (4.1 baseline, measured 2026-07-28) | NIM Nemotron Nano 9B v2 bf16 (this run) |
|---|---|---|
| decode | 85.1 tok/s | **26.3 tok/s** (400 tokens in 15.2 s, wall) |
| prefill | 3852 tok/s | 23 prompt + 200 completion tokens in 8.4 s wall (prefill not separable from the API) |
| full app turn, local | ~3 s | **~19-23 s** (single-agent 18.7 s; blueprint turns 31-32 s) |
| NeMo Guardrails judge overhead | +10-15 s on the 12B (napkin) | benign turn with rails 10 s total, jailbreak verdict 8 s |
| VRAM | 11.1 GB (both models resident) | 20.4 GB (weights 16.6 GB + SSM cache + 1.5 GB KV) |

The 3× slower decode is the expected bf16-vs-Q4 gap: the NIM's only A10G profile is `vllm-bf16-tp1-pp1`
(no quantized profile for this card), so every token streams 16.6 GB of weights through the A10G's 600 GB/s,
which caps single-stream decode near 30 tok/s. The Ollama model is a 7 GB 4-bit quant. For a demo box that
wants sub-5-second turns on this instance type, Ollama remains the faster provider; the NIM buys the NVIDIA
stack (NeMo rails on a local judge, NemoClaw, the blueprint's native tool calling) at ~20 s per turn.
