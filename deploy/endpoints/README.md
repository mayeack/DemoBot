# DemoBot endpoint wiring

How DemoBot is wired to each downstream endpoint it can send data to, and the
procedures for wiring a new deployment (this Mac, an EC2 fleet replica, or a
workshop box) to each one.

This is the index. The assets themselves stay where the tooling expects them —
collector configs at the repo root because `run-collector.sh` mounts them by
path, procedures under `.claude/skills/` because that is where Claude discovers
them. Nothing here is a copy of those; this file maps them.

The design rationale for fanning all four out at once lives in
[`docs/observability-fleet-distribution.md`](../../docs/observability-fleet-distribution.md).

---

## The four endpoints

| # | Endpoint | Carries | Transport |
|---|---|---|---|
| 1 | **Splunk Observability Cloud** | traces + metrics | app → OTLP → local collector → APM / signalfx |
| 2 | **Splunk platform HEC** (+ Log Observer Connect) | governance / audit logs | app HEC forwarder → Splunk Core |
| 3 | **Galileo** | LLM traces + governance metadata | SDK per turn; collector fans GenAI spans too |
| 4 | **Cisco AI Defense** | prompt / response inspection | app → Inspection API, per-request opt-in |

---

### 1. Splunk Observability Cloud — traces + metrics

| Asset | Path |
|---|---|
| Base collector config | [`otel-collector-config.yaml`](../../otel-collector-config.yaml) |
| Collector launcher | [`run-collector.sh`](../../run-collector.sh) |
| Regression test | `verify-observability` skill |

Configured by `.env` `SPLUNK_REALM` / `O11Y_INGEST` + `OTEL_*`. This is
the passive path — every turn produces spans and metrics with no opt-in.

### 2. Splunk platform HEC — governance / audit logs

**The live path is the app's HEC forwarder, not the collector.**

| Asset | Path | Status |
|---|---|---|
| App-side forwarder (**live**) | [`backend/hec/`](../../backend/hec/) | live — sourcetype `gen_ai:json` |
| Collector logs overlay | [`otel-collector-logs.yaml`](../../otel-collector-logs.yaml) | **retired** — see below |
| Workshop procedure | [`.claude/skills/wire-tunnel-logs-to-o11y/`](../../.claude/skills/wire-tunnel-logs-to-o11y/) | live for workshop boxes |
| Workshop full config | `wire-tunnel-logs-to-o11y/reference/otel-logs-pipeline.yaml` | as-deployed to medadvice1 |
| Splunk-side field patches | `wire-tunnel-logs-to-o11y/reference/patch-demobot-*.py` | CIM / sourcetype / ES-only |

Two collector configs here look interchangeable and are not:

- **`otel-collector-logs.yaml`** (repo root, 38 lines) is a small *overlay*
  layered on by `run-collector.sh` only when `SPLUNK_HEC_TOKEN` is set. It ships
  OTLP log records to the **local** Splunk with sourcetype `demobot:otel`. It is
  **retired** — it flooded `gen_ai_log` with collector noise, and the governance
  JSON now rides the app HEC forwarder instead. Tracked for history; leave the
  `SPLUNK_HEC_*` keys commented in `.env` unless you intend to revive it.
- **`reference/otel-logs-pipeline.yaml`** (144 lines) is a *complete* collector
  config for a **workshop** box — different destination (workshop Splunk Cloud
  stack), different sourcetype (`demobot:json`), driven by `WORKSHOP_*` env
  vars, and it carries the Galileo fan-out too.

Splunk Observability Cloud has no native log store. Logs reach O11y via Log
Observer Connect federating the Splunk Cloud stack — so the destination is
always a HEC endpoint, never `ingest.<realm>.signalfx.com`.

### 3. Galileo

| Asset | Path |
|---|---|
| Collector fan-out overlay | [`otel-collector-galileo.yaml`](../../otel-collector-galileo.yaml) |
| Wiring procedure | [`.claude/skills/fan-tunnel-to-galileo/`](../../.claude/skills/fan-tunnel-to-galileo/) |
| Pipeline reference | `fan-tunnel-to-galileo/reference/otel-galileo-pipeline.yaml` |
| Poisoning eval | `galileo-poisoning-eval` skill |

Two independent paths, both driven by the *same four* `.env` keys — which is why
one working path is not evidence the other works:

- **Path A (SDK)** — `backend/galileo_integration.py` → `/ingest/traces/<project_id>`.
  Carries safety / PII / toxicity / policy / eval metadata. This is the one the
  workshop demos.
- **Path B (collector)** — `otlphttp/galileo` → `/otel/traces`. Raw `gen_ai.*`
  spans only, no governance fields. Requires the `filter/genai_only` processor;
  Galileo drops batches with no GenAI spans in them.

### 4. Cisco AI Defense

| Asset | Path |
|---|---|
| Connection procedure | [`.claude/skills/connect-ai-defense/`](../../.claude/skills/connect-ai-defense/) |

Not a network peering or agent install — an outbound API integration. The
Inspection API key binds traffic to a specific application + connection in
Security Cloud Control, so repointing the key repoints which SCC app the traffic
appears under. Six `.env` vars; annotated originals in `.env.example`.

---

## Provisioning a box to wire up

| Asset | Path |
|---|---|
| Fleet provisioning | [`deploy/ec2/`](../ec2/) + [`spin-up-ec2` skill](../../.claude/skills/spin-up-ec2/) |
| Mac services | [`deploy/launchd/`](../launchd/) |
| As-built EC2 record | [`EC2-DemoBot-Runbook.md`](EC2-DemoBot-Runbook.md) |
| Secret provisioning | `provision-tokens` skill |

**The runbook documents a different box than the current fleet.** It captures
`i-0883a0ddedf54e4e8` (`c6i.2xlarge`, `35.175.173.5`) in the Cisco lab account
`754184243988`, as built on 2026-07-27 against `main@edfcb88`. The current fleet
is `g5.xlarge` GPU instances in the **personal** account `177835492378`. Read §6
for the reproducible DemoBot layer; §§2–5 are the as-built record of that one
host and do not describe the fleet.

---

## Provenance and drift

`fan-tunnel-to-galileo/` and `wire-tunnel-logs-to-o11y/` were copied here from
the workshop project:

```
~/Library/CloudStorage/OneDrive-Cisco/Projects/o11y AI Workshop '27/.claude/skills/
```

That folder is **not a git repo** — it had no history and no backup beyond
OneDrive sync, which is why these are tracked here. The workshop copies were
left in place and still work, so **the two copies can drift**. Treat this repo
as the source of truth and re-copy outward when these change.

`EC2-DemoBot-Runbook.md` came from the same workshop folder.

## Secrets

No credentials in this package. Every file was scanned before it was committed;
the only identifier-shaped strings are instance IDs, public IPs, Galileo project
and log-stream UUIDs, and one git commit SHA. Real secrets live in `.env` (git-
ignored), in `deploy/ec2/access-keys.env` (git-ignored), and in AWS SSM — see the
`provision-tokens` skill.
