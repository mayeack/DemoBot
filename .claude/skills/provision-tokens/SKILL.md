---
name: provision-tokens
description: Generate, populate, validate and rotate every secret DemoBot needs (Splunk O11y ingest + API tokens, Splunk Core HEC token, Galileo API key, Cisco AI Defense key, app ACCESS_KEY) — and self-configure them unattended at server/app startup from AWS SSM Parameter Store. Use when setting up a new server, when a token is missing/expired/401/403, when rotating secrets, or when asked to make token setup automatic.
---

# Provision DemoBot tokens (self-configuring)

DemoBot talks to **four external backends**, each gated by its own secret. This skill
covers **how each token is generated, where it must land, how to validate it, and how to
populate it unattended at startup** so no human is in the loop on a server boot.

## Token inventory — what exists and where it goes

| # | Secret | Backend | Lands in | Consumed by |
|---|---|---|---|---|
| 1 | `SPLUNK_ACCESS_TOKEN` | Splunk **Observability Cloud** | `.env` | OTel **collector** (ingest traces/metrics) |
| 2 | `SPLUNK_API_TOKEN` | Splunk **Observability Cloud** | `.env` | verify scripts + detector tooling (**read API only**) |
| 3 | **HEC token** | **Splunk Core** (Enterprise/Cloud) | **SQLite** `app_settings.hec_destinations[].token` | `backend/hec/` forwarder |
| 4 | `GALILEO_API_KEY` | Galileo | `.env` | SDK (`backend/galileo_integration.py`) + collector exporter |
| 5 | `AI_DEFENSE_API_KEY` | Cisco AI Defense | `.env` | `backend/services/ai_defense.py` |
| 6 | `ACCESS_KEY` | DemoBot itself | `.env` | `backend/middleware/access_key.py` (HTTP Basic gate) |

**Critical:** #3 is the odd one out — it is **NOT an env var**. It lives in each host's
SQLite blob and is normally typed into the Settings UI. It does **not** travel when you
copy `.env` to a new server. See `docs/observability-fleet-distribution.md`.

---

## The self-configuration model

Goal: **a server boots and configures its own tokens with nobody watching.** The pattern
is *secret store → instance identity → bootstrap script → app config*. Secret values move
machine-to-machine and never pass through a chat transcript, a log, or an agent's context.

```
AWS SSM Parameter Store (SecureString, KMS-encrypted)
        │   authN via EC2 instance profile — no "secret zero" on disk
        ▼
  bootstrap-secrets.sh   (runs before demobot-app / demobot-collector)
        ├─► writes .env keys        (#1,2,4,5,6)
        └─► PUTs HEC token via API  (#3 → settings_store + reconfigure_hec)
        ▼
  systemd starts app + collector ──► self-verify
```

### What is automatable vs. what needs a human once
| Step | Unattended? |
|---|---|
| Fetch every secret at boot and populate `.env` + HEC | ✅ yes — fully scripted, values never seen |
| Generate `ACCESS_KEY` (#6) | ✅ yes — `openssl rand -hex 24`, piped straight to `.env` |
| Create a **Splunk Core HEC token** (#3) | ✅ yes — `splunk http-event-collector create` using an admin password pulled from SSM |
| Validate + rotate any token | ✅ yes |
| **Mint** #1/#2/#4/#5 in a vendor console the first time | ❌ **one-time human** — these are third-party web UIs |

So: a human seeds SSM **once** per credential; every server boot afterwards is unattended.

### Handling rule (do not violate)
Never transcribe a secret **from chat** into a config file, and never print a token value
into terminal output, a log, or a commit. If a token is pasted into a conversation, treat
it as **compromised → rotate it**. The SSM path exists precisely so secrets never need to
be pasted. Reading a secret into a **shell variable** that is immediately used and unset
(never echoed) is the correct mechanism — e.g. the HEC repair in "Populate" below.

---

## Prerequisites (one-time, per AWS account)

The EC2 host already has an instance profile — `<INSTANCE_ROLE>` on
`<EC2_INSTANCE_ID>` (account `<AWS_ACCOUNT_ID>`, `us-east-1`, aws-cli v2 installed) — but it
**lacks SSM read**. Attach a policy like:

```json
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow", "Action": ["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],
    "Resource": "arn:aws:ssm:us-east-1:<AWS_ACCOUNT_ID>:parameter/demobot/*" },
  { "Effect": "Allow", "Action": ["kms:Decrypt"], "Resource": "*" } ]}
```

Then seed the parameters **once** (run by a human, from a machine with the values):

```bash
aws ssm put-parameter --name /demobot/SPLUNK_ACCESS_TOKEN --type SecureString --value "<paste>" --overwrite
```
Repeat for `/demobot/SPLUNK_API_TOKEN`, `/demobot/GALILEO_API_KEY`,
`/demobot/AI_DEFENSE_API_KEY`, `/demobot/ACCESS_KEY`, `/demobot/HEC_TOKEN`.

*No AWS?* Fallback: a root-owned `/etc/demobot/secrets.env` (`chmod 600`) placed by config
management. Same bootstrap logic, weaker rotation story.

---

## Generating each token

### 1 + 2. Splunk Observability Cloud — ingest vs API (**they are different**)
A single realm (`us1`) but two distinct tokens. Using the wrong one is the classic failure:
an **ingest** token can send data but returns **401** on the read API.

- **`SPLUNK_ACCESS_TOKEN` (ingest):** O11y → **Settings → Access Tokens** → use/create a
  token with **INGEST** authorization. Feeds the collector.
- **`SPLUNK_API_TOKEN` (API):** O11y → **avatar (top-right) → API Access Token** (a User
  API Access Token), *or* Settings → Access Tokens with **API** scope. Feeds
  `verify_observability.sh` Tier 3 + `scripts/observability/create_apm_detectors.py`.
- Both must belong to the **same org as `SPLUNK_REALM`**, else 401.

### 3. Splunk Core — HEC token (fully automatable)
HEC must be **listening** first. On the app host (Splunk Enterprise at `/opt/splunk`):

```bash
sudo systemctl start Splunkd
sudo /opt/splunk/bin/splunk http-event-collector enable -uri https://localhost:8089 -enable-ssl 1 -port 8088
sudo /opt/splunk/bin/splunk http-event-collector create demobot-governance -index gen_ai_log -sourcetype medadvice:governance -uri https://localhost:8089
```
The target index must **exist** and the token must be **allowed** to write it. Without the
admin password, create an index by writing `/opt/splunk/etc/system/local/indexes.conf`
(`homePath`/`coldPath`/`thawedPath` under `$SPLUNK_DB/<name>/`) and restarting `Splunkd`;
then in the token stanza set `index = <name>` plus `indexes = main,<name>`.
Health check (no token needed): `curl -sk https://localhost:8088/services/collector/health` → **200**.
The token is then readable from `/opt/splunk/etc/apps/splunk_httpinput/local/inputs.conf`
(stanza `[http://demobot-governance]`) — which is what makes step 3 self-configurable.

### 4. Galileo — `GALILEO_API_KEY`
Galileo console (`https://console.multitenant.galileocloud.io`) → user/organization
settings → **API Keys** → create. Also set the non-secret `GALILEO_PROJECT` /
`GALILEO_LOG_STREAM` (currently both `DemoBot`).

### 5. Cisco AI Defense — `AI_DEFENSE_API_KEY`
Cisco Security Cloud Control → **AI Defense → Inspection / Connections** → create an
inspection connection → copy its API key. Sent as `X-Cisco-AI-Defense-API-Key`.
Note: if the connection has an SCC policy bound, the app auto-detects a 400 on the first
call and retries without `enabled_rules` — a **400 then 200 is normal**, not an error.

### 6. `ACCESS_KEY` — self-generated, no vendor
```bash
openssl rand -hex 24
```
Generate and store directly into SSM without displaying it:
```bash
aws ssm put-parameter --name /demobot/ACCESS_KEY --type SecureString --overwrite --value "$(openssl rand -hex 24)"
```

---

## Populating (the unattended part)

### `.env` keys (#1,2,4,5,6)
Fetch from SSM and **replace in place** — never append (see Gotchas):
```bash
set_env() {  # set_env KEY /demobot/PARAM   — value never printed
  local k="$1" v; v=$(aws ssm get-parameter --name "$2" --with-decryption --query Parameter.Value --output text) || return 1
  [ -n "$v" ] || return 1
  if grep -q "^${k}=" .env; then
    python3 - "$k" "$v" <<'PY'
import sys,io
k,v=sys.argv[1],sys.argv[2]
p=".env"; lines=open(p).read().splitlines(True)
out=[f"{k}={v}\n" if l.startswith(k+"=") else l for l in lines]
open(p,"w").writelines(out)
PY
  else printf '%s=%s\n' "$k" "$v" >> .env; fi
  unset v
}
```
Use `python3` (not `sed`) for the substitution so token characters like `&`, `/` and `|`
can't corrupt the replacement.

### HEC token (#3) — SQLite plane, via the API
Create the destination (non-secret), then inject the token machine-to-machine:
```bash
KEY=$(grep '^ACCESS_KEY=' .env | cut -d= -f2-)
curl -s -u x:"$KEY" -X POST http://localhost:8001/api/hec/destinations -H 'Content-Type: application/json' \
  -d '{"name":"Splunk Core (local)","url":"https://localhost:8088/services/collector/event","index":"main","source":"medadvice","sourcetype":"medadvice:governance","host":"'"$(hostname)"'","verify_tls":false,"enabled":false}'

# token straight from the authoritative source — never echoed
T=$(sudo awk '/^\[http:\/\/demobot-governance\]/{f=1} f&&/^token *=/{sub(/^token *= */,""); gsub(/[[:space:]]/,""); print; exit}' \
     /opt/splunk/etc/apps/splunk_httpinput/local/inputs.conf)
curl -s -u x:"$KEY" -X PUT http://localhost:8001/api/hec/destinations/splunk-core-ec2-local \
  -H 'Content-Type: application/json' -d "{\"token\":\"$T\",\"enabled\":true}" >/dev/null
unset T
```
`PUT` triggers `reconfigure_hec()` → the forwarder hot-restarts. **No app restart needed.**
(Substitute `aws ssm get-parameter --name /demobot/HEC_TOKEN` for the `awk` when the token
comes from SSM instead of a co-located Splunk.)

### Wiring into startup
Run the bootstrap **before** the app in systemd:
```ini
# /etc/systemd/system/demobot-app.service
ExecStartPre=/home/<SSH_USER>/DemoBot/deploy/bootstrap-secrets.sh
ExecStart=/bin/bash /home/<SSH_USER>/DemoBot/run.sh
```
The HEC `PUT` must run **after** the app is listening — either a `demobot-seed.service`
with `After=demobot-app.service`, or the app-side seed loader proposed in
`docs/observability-fleet-distribution.md` §3.2 (preferred: no ordering race).

---

## Validating (always verify, never assume)

| Secret | Check | Pass |
|---|---|---|
| `SPLUNK_ACCESS_TOKEN` | `curl -s localhost:8888/metrics \| grep send_failed` | no/zero failures |
| `SPLUNK_API_TOKEN` | `./tests/observability/verify_observability.sh` | Tier 3 passes (not 401) |
| HEC token | `POST /api/hec/destinations/{id}/test` | `{"ok":true,"status_code":200}` |
| Galileo | app log after a turn | `galileo: logged turn (...)` |
| AI Defense | chat turn with `ai_defense_review:true` | `POST .../inspect/chat` → **200** |
| `ACCESS_KEY` | `curl -u x:$KEY .../admin/logs/metrics` | 200 (and 401 without) |

Full sweep: `./tests/observability/verify_observability.sh` then `GET /api/hec/stats`.

---

## Gotchas (learned the hard way, 2026-07-27)

- **403 "Invalid token" from HEC with a matching `token_last4`.** A UI/CLI paste captured
  the surrounding label, storing **67 chars** instead of the 36-char UUID. `token_last4`
  still matched, so it looked correct. **Always assert length**: HEC tokens are 36-char
  UUIDs. Re-read from the authoritative source rather than re-pasting.
- **401 on O11y Tier 3 while telemetry flows fine.** Ingest vs API token confusion — data
  kept arriving via `SPLUNK_ACCESS_TOKEN` while `SPLUNK_API_TOKEN` was wrong/expired. The
  two are unrelated; a healthy pipeline does **not** imply a valid API token.
- **Never append a duplicate `.env` line.** Both `backend/config.py` (first wins) and the
  shell scripts (`grep '^KEY=' .env | cut -d= -f2`) break on duplicates — the shell path
  yields *both* values. Replace in place; assert `grep -c '^KEY=' .env` equals 1.
- **`cut -d= -f2` truncates at `=`.** Values containing `=` (some base64 tokens) get cut.
  Prefer `cut -d= -f2-`; never wrap `.env` values in quotes and never add a trailing
  `# comment` — the shell readers don't strip either.
- **`localhost` endpoints don't travel.** A HEC URL of `https://localhost:8088` only works
  where Splunk runs. On a fleet, either run Splunk per host (separate indexes) or point all
  hosts at one network-reachable HEC.
- **HEC needs the port open.** `splunk http-event-collector enable` + a listener on 8088;
  `.../collector/health` returning 200 is the cheap precondition check.
- **The SQLite plane can silently override `.env`** (active provider + provider creds).
  A host can diverge from a perfectly distributed `.env`. Prefer `.env` as authoritative.
- **Self-signed HEC certs** on `localhost:8088` require `verify_tls:false` on the
  destination (or install a trusted cert).
- **Changing a destination's index needs the token's permission too.** Repointing DemoBot
  to `gen_ai_log` fails unless the index exists *and* the token stanza's `indexes` allowlist
  includes it — fix both in `inputs.conf`, then restart `Splunkd`.
- **`PUT /api/hec/destinations/{id}` resets the forwarder stats** (it calls
  `reconfigure_hec()`), so `events_sent` returns to 0. Not data loss — just a new counter.
- **Two Splunk Enterprise instances exist** — the Mac's (`<MAC_SPLUNK_HOST>`, what the
  `splunk104` MCP queries) and EC2's. Events from the EC2 app index into **EC2's** Splunk,
  so the MCP will not find them. Verify EC2 indexing via
  `grep per_index_thruput /opt/splunk/var/log/splunk/metrics.log`.
