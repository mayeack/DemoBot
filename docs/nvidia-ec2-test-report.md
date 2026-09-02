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

