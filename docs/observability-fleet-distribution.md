# Multi-Server Observability Fan-Out — Config & Secret Distribution Design

**Status:** Design (not implemented — recommended next phase)
**Scope:** how to distribute the identical set of API keys / endpoints / HEC targets to **every** server in a DemoBot fleet, so all servers fan **all** telemetry/guardrail traffic out to **all** four backends, while preserving per-host identity.
**Context date:** 2026-07-27. Live baseline: single EC2 host `<EC2_INSTANCE_ID>` (`main@edfcb88`, `AI_PROVIDER=ollama`) behind the named Cloudflare tunnel `<TUNNEL_HOSTNAME>`.

---

## 1. The four backends and how data reaches them

| Backend | Transport | Trigger | Configured by |
|---|---|---|---|
| **Splunk Observability Cloud** | app → OTLP → **local OTel collector** → APM (traces) + signalfx (metrics) | every turn (passive) | `.env` `SPLUNK_REALM`/`SPLUNK_ACCESS_TOKEN` + `OTEL_*` |
| **Galileo** | (a) SDK per turn; (b) collector `otlphttp/galileo` (GenAI spans) | every turn (passive) | `.env` `GALILEO_*` |
| **Splunk Core** | app → **HEC** (`backend/hec/`), multi-destination fan-out | every governance/audit log | **SQLite** `app_settings.hec_destinations` (no env var) |
| **Cisco AI Defense** | app → Inspection API (`inspect_prompt`/`inspect_response`) | **per-request opt-in** (`ai_defense_review`) | `.env` `AI_DEFENSE_*` |

Two telemetry planes:
- **Plane A (OTLP):** `app → localhost:4317 collector → Splunk O11y + Galileo`. The collector YAML is **host-generic** and reads `${env:SPLUNK_REALM|SPLUNK_ACCESS_TOKEN|GALILEO_*}`.
- **Plane B (governance logs):** one chokepoint — `backend/logging/governance_logger.py::_write_log` → `hec_runtime.submit()` (fans to **all enabled HEC destinations**) **+** Galileo SDK.

```
                                   ┌────────────► Splunk O11y Cloud (APM + metrics)
   app ──OTLP──► local collector ──┤
    │                              └────────────► Galileo (GenAI spans)
    │
    ├─ governance_logger._write_log ─┬─► HEC ──► Splunk Core  (all enabled destinations)
    │                                └─► Galileo SDK (governance verdicts)
    │
    └─ defense nodes ──(opt-in)──► Cisco AI Defense Inspection API
```

---

## 2. The distribution problem — three config planes, only one travels with `.env`

| Plane | Holds | Travels by copying `.env`? |
|---|---|---|
| **1. `.env` / process env** | O11y (`SPLUNK_*`,`OTEL_*`), Galileo (`GALILEO_*`), AI Defense (`AI_DEFENSE_*`), provider keys, `ACCESS_KEY` | ✅ yes |
| **2. SQLite `app_settings.data`** | **HEC destinations (Splunk Core)**, UI provider creds, active-provider override | ❌ **no — per-host only** |
| **3. Collector env** | realm/token, Galileo headers | ✅ yes (host-generic YAML, from `.env`) |

**Key insight:** copying one canonical `.env` to every host distributes three of the four backends' config automatically. It does **not** carry **Splunk Core HEC destinations**, which live only in each host's SQLite blob and are entered by hand through the Settings UI. Worse, that same SQLite plane can hold an **active-provider override that silently wins over `.env`** at startup (observed live: the Mac DB forces `provider=nvidia` while its `.env` says `ollama`). Any fleet design must therefore address Plane 2 explicitly, not just ship `.env`.

---

## 3. Design

### 3.1 Plane 1 — one canonical `.env`, pushed by config management
Chosen substrate: **shared canonical `.env` distributed via config management** (Ansible / `scp` / `rsync`). This works cleanly because `backend/config.py` and the shell launchers honor **already-exported env vars over `.env`** (`config.py:30`), and the collector YAML is host-generic.

**Identical across every server** (the "distribute to all" set):
`SPLUNK_REALM`, `SPLUNK_ACCESS_TOKEN` (ingest), `SPLUNK_API_TOKEN` (API), `GALILEO_API_KEY`, `GALILEO_PROJECT`, `GALILEO_LOG_STREAM`, `GALILEO_CONSOLE_URL`, `AI_DEFENSE_API_KEY`, `AI_DEFENSE_REGION`/`AI_DEFENSE_ENDPOINT` + rule lists, provider keys (`ANTHROPIC_API_KEY`, `NVIDIA_API_KEY`, …), `ACCESS_KEY`, `AI_PROVIDER` + `*_MODEL`, all safety/injection/session flags, `OTEL_SERVICE_NAME=demobot-v3`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`.

**Per-host (must differ / be templated):**
`OTEL_RESOURCE_ATTRIBUTES` (`deployment.environment` + host id — how O11y/Galileo split one shared service by box), `SERVER_HOSTNAME`, `DATABASE_URL` (unless a shared DB — see 3.5). `OLLAMA_BASE_URL` stays `localhost` (identical string, host-local runtime).

### 3.2 Plane 2 — a startup **seed loader** for the SQLite config (the missing piece)
This is the core new capability. Add a boot-time loader that reads the SQLite-only config from a distributable source and upserts it into the store — so HEC destinations travel exactly like `.env`.

- **Source:** an env var `HEC_DESTINATIONS_JSON` (a JSON array of destinations) **or** a gitignored `hec_destinations.json` file dropped next to `.env`.
- **Action on startup (before `hec_runtime` starts):** parse → for each entry call the existing `settings_store.add_destination()` / update, then `reconfigure_hec()`. Reuses the machinery already in `backend/settings_store.py` + `backend/hec/runtime.py` (which already fans one event to all enabled destinations).
- **Idempotent:** match on destination `name`/`id`; update in place so re-provisioning is safe.
- **Optional:** the same loader can seed provider creds to remove reliance on UI entry (see 3.4).

*Alternatives considered:* (a) a post-deploy script that `POST`s `/api/hec/destinations` per host (no code change, but an extra imperative step and secrets in shell history); (b) a shared **Postgres** `DATABASE_URL` so all hosts share one `app_settings` row (simplest conceptually, but turns the DB into shared infra and a single point of failure). The seed loader is recommended because it keeps the "config as data, pushed to each host" model consistent with Plane 1.

### 3.3 Per-host identity
Template only three fields at provision time: `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=demobot-ec2-<n>,host.name=<hostname>`, `SERVER_HOSTNAME=<hostname>`, and `DATABASE_URL`. Everything else is byte-identical — this is what makes "one shared service, split by host" work in O11y/Galileo.

### 3.4 Divergence guardrail (provider selection)
The SQLite active-provider override silently beating `.env` is a fleet foot-gun. Options: (a) make `.env` authoritative — ignore/clear the SQLite provider override on managed hosts; or (b) fold provider selection + creds into the seed (3.2) so they're distributed deterministically. Recommend documenting provider selection as **`.env`-only** on fleet hosts and having provisioning clear any stale SQLite override.

### 3.5 Provisioning (systemd)
The live EC2 box runs systemd units `demobot-app` / `demobot-collector` / `demobot-tunnel` created out-of-band; the repo ships **launchd-only** templates (`deploy/launchd/`). Add a parallel **systemd** provisioning path mirroring `deploy/launchd/install.sh`: unit files + an `EnvironmentFile` (or the canonical `.env` in `WorkingDirectory`). A new server becomes turnkey:

```
clone repo → drop canonical .env + hec_destinations.json → systemctl enable/start demobot-* → self-verify
```

### 3.6 Secret hygiene
Two secrets were remediated on the baseline host while writing this design; both belong in
the canonical set:
- **`SPLUNK_API_TOKEN`** — was returning **401** (an ingest token had been used where an
  O11y **API** token is required). Replaced; `verify_observability.sh` now passes 9/9.
  Note a healthy ingest pipeline does **not** imply a valid API token — they are separate.
- **Splunk Core HEC token** — was not configured at all, so no governance logs reached
  Splunk Core. Now provisioned against a local Splunk Enterprise, writing to index
  `gen_ai_log` (sourcetype `medadvice:governance`); include it in the Plane-2 seed.

Rotate from one canonical source, and treat any token pasted into a chat/ticket as
compromised. Never bake tokens into the repo (`.env` and `*.db` are gitignored — keep it
that way). See the `provision-tokens` skill for generation, population and validation.

---

## 4. Target fan-out topology

```
   ┌── server-1 (app+collector) ──┐
   ├── server-2 (app+collector) ──┤        ┌─► Splunk O11y Cloud (realm us1)
   ├── server-3 (app+collector) ──┼────────┼─► Galileo (project DemoBot)
   └── server-N (app+collector) ──┘        ├─► Splunk Core (HEC destination[s])
        each identical config,             └─► Cisco AI Defense (inspection)
        each fans out to ALL four
```

Every server carries the same keys/endpoints (Plane 1) **and** the same HEC destination list (Plane 2 seed), differing only in host-identity fields — so all four backends receive every server's data, tagged by host.

---

## 5. Implementation checklist (next phase — not done this session)
1. Add the Plane-2 **seed loader** (`HEC_DESTINATIONS_JSON` / `hec_destinations.json` → `add_destination` + `reconfigure_hec`) with idempotent upsert. Optionally seed provider creds.
2. Establish the **canonical secret source** (config-mgmt repo / vault / SSM) and the push mechanism (Ansible/`scp`).
3. Add the **systemd** provisioning analog to `deploy/launchd/install.sh` (units + `EnvironmentFile` + per-host identity templating).
4. Fix/rotate **`SPLUNK_API_TOKEN`**; add the **HEC** destination to the canonical seed.
5. Add a per-host **self-verify** hook (`tests/observability/verify_observability.sh` + HEC stats + AI Defense toggle test) run at the end of provisioning.

## 6. Per-host verification (definition of done for each server)
- `verify_observability.sh` → Tier 1–3 pass (needs a valid `SPLUNK_API_TOKEN`).
- `GET /api/hec/stats` → `events_sent>0`, `failed=0`; the Splunk Core search finds the events.
- Galileo shows the host's turns (project `DemoBot`).
- One chat turn with `ai_defense_review=true` → AI Defense `200` + governance flags.
- O11y/Galileo split the fleet by `host.name` / `deployment.environment`.
