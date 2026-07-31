# DemoBot EC2 — Configuration Reference & Rebuild Runbook

**Captured:** 2026-07-27 from `i-0883a0ddedf54e4e8` (35.175.173.5)
**Purpose:** document the running instance completely enough to rebuild it, or a
replica of it, from a raw EC2 instance.

---

## 1. Scope — what is actually reproducible

The instance runs two independent stacks. Only the second one is part of the
workshop deliverable.

| Layer | Origin | Reproducible on a raw EC2? |
|---|---|---|
| **A. Corporate base image** — CrowdStrike Falcon, Nessus agent, PBIS/AD join, fail2ban, ansible, SSM, CloudWatch agent, collectd, `AllowGroups` SSH policy | Cisco-managed AMI `ami-09860bb56ed1a5925` + Ansible | **No.** Comes from launching the same managed AMI in the same account. Not required for DemoBot. |
| **B. Splunk Enterprise 10.4.1** (`/opt/splunk`, ports 8443/8089/8088) + Universal Forwarder | Pre-installed on the lab AMI | Optional. Independent of DemoBot; DemoBot exports to Splunk **Observability Cloud** (SaaS), not to this local Splunkd. |
| **C. DemoBot workload** — Ollama, DemoBot app, OTel collector, Cloudflare tunnel | Built 2026-07-27 by `~/ec2-bootstrap.sh` | **Yes, fully.** Section 6 rebuilds it end to end. |

If the goal is "another DemoBot replica," you only need **§6**. Sections 2–5 are
the as-built record of this box.

---

## 2. AWS / instance facts

| Property | Value |
|---|---|
| Instance ID | `i-0883a0ddedf54e4e8` |
| Instance type | `c6i.2xlarge` (8 vCPU, 15 GiB RAM) |
| AMI | `ami-09860bb56ed1a5925` (Ubuntu 22.04.5 LTS, jammy) |
| Kernel | `6.5.0-1014-aws`, x86_64 |
| Region / AZ | `us-east-1` / `us-east-1a` |
| Private IP | `172.31.27.241` |
| Public IP | `35.175.173.5` |
| Security group | `SOAR-Hunter-Lab` |
| IAM instance profile | `aws_cloudwatch_role_for_ec2` (acct `754184243988`) |
| Root volume | 100 GiB gp3, ext4, single partition + 106 MB EFI. 31 GB used |
| Swap | none |
| Hostname | `show-demo-i-0883a0ddedf54e4e8` |
| Timezone | `Etc/UTC`, chrony synced |

**Sizing note.** Both Ollama models are CPU-only (no GPU on `c6i`). Resident
footprint is ~9.3 GB across `dolphin3:8b` (6.2 GB) + `llama3.2:3b` (3.1 GB), on
top of Splunk Enterprise. The box sits at ~10 GB/15 GB used. A rebuild that
drops Splunk Enterprise runs comfortably on the same instance type; anything
smaller than ~16 GB RAM will thrash between the two models.

### Ports that must be reachable

| Port | Bind | Purpose | Exposure needed |
|---|---|---|---|
| 2222 | `0.0.0.0` | SSH (non-standard port) | inbound from admin IPs |
| 8001 | `0.0.0.0` | DemoBot / uvicorn | **not** publicly required — Cloudflare tunnel is outbound-only |
| 11434 | `127.0.0.1` | Ollama | local only |
| 4317 / 4318 | `0.0.0.0` | OTel collector OTLP gRPC / HTTP | local only |
| 8888 | `127.0.0.1` | Collector self-metrics | local only |
| 8443 / 8089 / 8088 | `0.0.0.0` | Splunk Enterprise web / mgmt / HEC | only if keeping layer B |

Outbound egress required: `*.signalfx.com`, `api.multitenant.galileocloud.io`,
`*.argotunnel.com` / Cloudflare edge, `ollama.com`, `github.com`,
`us.api.inspect.aidefense.security.cisco.com`.

`ufw` is **inactive**. The only iptables rule is a fail2ban chain on 22/2222.
Network policy is enforced entirely by the AWS security group.

---

## 3. Users, access, and SSH

```
splunk:x:1003:1004  /home/splunk  /bin/bash   groups: splunk, wheel, ollama
ubuntu:x:1000:1000  (cloud-init default)
ansible:x:1001:100  (config management)
ssm-user:x:1002:1003 (SSM Session Manager)
```

DemoBot runs entirely as **`splunk`**. That user needs:
- membership in `wheel` (passwordless sudo via `/etc/sudoers.d/wheel`) — also
  granted directly by `/etc/sudoers.d/splunk` and `/etc/sudoers.d/newuser`
- membership in `ollama` (created by the Ollama installer)
- inclusion in sshd's `AllowGroups` list

`/etc/ssh/sshd_config` — the non-default settings:

```
Port 2222
LogLevel VERBOSE
PasswordAuthentication yes          # inherited from 60-cloudimg-settings.conf
PubkeyAuthentication yes
Banner /etc/ssh/banner
AllowGroups sg-it-scip-dvo sg-it-scip-dvo-awf ssg-its-nspire-platform-services-all \
            sg-srv-rpa ssg-sgs-gso-splunkcirt-ic wheel ansible
```

> `AllowGroups` is the gate that matters: a new user cannot SSH in unless it is
> in one of those groups. `splunk` gets in via `wheel`.

`/home/splunk/.ssh/authorized_keys` holds two keys:

| Comment | Type | Fingerprint |
|---|---|---|
| `demo-migrate` | RSA 2048 | `SHA256:Fz9ftTJOHH4T0Asq7Z+bclNsIvWIGScqAKQqwjnvqUU` |
| `demobot-deploy` | ED25519 | `SHA256:lACYZxASafU0zNuZCY145FOUHf9wSqv31G4MC/gejms` |

The workstation-side private key for `demobot-deploy` is `~/.ssh/demobot_ec2`.

```bash
ssh -i ~/.ssh/demobot_ec2 -p 2222 splunk@35.175.173.5
```

---

## 4. Software inventory

### Installed outside the base image

| Component | Version | Path | Installed from |
|---|---|---|---|
| Python 3.11 | 3.11.15 | `/usr/bin/python3.11` | **deadsnakes PPA** (system default is 3.10.12) |
| Ollama | 0.32.4 | `/usr/local/bin/ollama` | `https://ollama.com/install.sh` |
| cloudflared | 2026.7.3 | `/usr/local/bin/cloudflared` | GitHub `.deb` release |
| otelcol-contrib | (415 MB binary) | `~/DemoBot/bin/otelcol-contrib` | GitHub release tarball |
| AWS CLI | — | `/usr/local/bin/aws` | base image |
| Splunk Enterprise | 10.4.1 (5a009d941268) | `/opt/splunk` | base image |

Not present: `node`/`npm`, `podman`, `docker`, `sqlite3` CLI, `uv`.
The OpenClaw gateway (`run-openclaw.sh`) therefore **does not run on this box** —
it needs podman. It runs only on the Mac replica.

### APT sources added

```
deb https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu/ jammy main
deb https://ppa.launchpadcontent.net/ansible/ansible/ubuntu/ jammy main
deb https://splunk.jfrog.io/splunk/otel-collector-deb release main
```

The apt-installed `splunk-otel-collector` service is **masked and inactive** —
DemoBot uses its own `otelcol-contrib` binary instead. Leave it masked; two
collectors fighting over :4317 is a common failure mode here.

### Ollama models

| Model | ID | Size | Role |
|---|---|---|---|
| `dolphin3:8b` | `d5ab9ae8e1f2` | 4.9 GB | user-facing synthesizer (clean) |
| `dolphin3:8b-poisoned` | `d22293974766` | 4.9 GB | demo variant with an injected prescribing directive |
| `llama3.2:3b` | `a80c4f17acd5` | 2.0 GB | internal coordinator/specialist agents |

`dolphin3:8b-poisoned` is built locally from `/home/splunk/Modelfile.poisoned`
(138 lines) — it is a `FROM dolphin3:8b` overlay whose TEMPLATE injects a
system-level directive after the app's own system prompt. **This is the
workshop's guardrail-failure exhibit; it is not a stock model and must be
rebuilt with `ollama create`, not pulled.** Keep `Modelfile.poisoned` in your
backup set — it is not in the git repo.

Systemd drop-in `/etc/systemd/system/ollama.service.d/keepalive.conf`:

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=30m"
```

### Python environment

`~/DemoBot/venv` built on **python3.11** (not the system 3.10 — LangChain 1.x
requires it). 102 packages; pinned by `requirements.txt`. Load-bearing ones:

```
fastapi 0.109.0 / uvicorn 0.27.0 / starlette 0.35.1
langchain 1.3.2 / langgraph 1.2.2 / langchain-ollama 1.1.0 / langchain-anthropic 1.4.6
anthropic 0.109.2 / openai 2.43.0 / ollama 0.6.2
opentelemetry-* 1.38.0 (sdk/api/exporters), instrumentation 0.59b0
splunk-opentelemetry 2.8.0
splunk-otel-util-genai 0.1.14 / splunk-otel-genai-emitters-splunk 0.1.8
splunk-otel-instrumentation-langchain 0.1.14   # present but DISABLED at runtime
galileo 2.3.0 / galileo-core 4.4.0
SQLAlchemy 2.0.25
```

---

## 5. DemoBot application configuration

### Layout

```
/home/splunk/
├── DemoBot/                     # git clone, branch main @ edfcb88 (PR #32)
│   ├── .env                     # 0600, secrets — NOT in git
│   ├── venv/                    # python3.11
│   ├── bin/otelcol-contrib      # 415 MB, not in git
│   ├── medadvice.db             # SQLite, per-instance state
│   ├── otel-collector-config.yaml
│   ├── run.sh  run-collector.sh  start-all.sh
│   └── logs/
├── .cloudflared/
│   ├── config.yml
│   └── 52a942e8-…-7865fc94c41b.json    # tunnel credentials — NOT in git
├── Modelfile.poisoned           # poisoned-model recipe — NOT in git
└── ec2-bootstrap.sh             # the build script that produced this box
```

Repo: `https://github.com/mayeack/DemoBot.git`, `main` @
`edfcb88f089c09e5d0cd7b32d8016d2f64da86c1`.

### `.env` — non-secret values (verbatim)

```ini
# --- AI provider: local uncensored model -------------------------------
AI_PROVIDER=ollama
OLLAMA_MODEL=dolphin3:8b
OLLAMA_MODEL_INTERNAL=llama3.2:3b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_NUM_CTX=8192
OLLAMA_KEEP_ALIVE=30m
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929   # used when AI_PROVIDER=anthropic

# --- Cisco AI Defense guardrail ----------------------------------------
AI_DEFENSE_ENABLED=True
AI_DEFENSE_REGION=us
AI_DEFENSE_ENDPOINT=https://us.api.inspect.aidefense.security.cisco.com
AI_DEFENSE_TIMEOUT=10.0
AI_DEFENSE_FAIL_OPEN=False

# --- App / server ------------------------------------------------------
APP_NAME=DemoBot v3
APP_VERSION=3.0.0
ENVIRONMENT=development
DEBUG=False
HOST=0.0.0.0
PORT=8001
DATABASE_URL=sqlite:///./medadvice.db
SESSION_TIMEOUT_MINUTES=30

# --- Logging -----------------------------------------------------------
LOG_LEVEL=INFO
LOG_TO_FILE=True
LOG_TO_CONSOLE=True
LOG_TO_DATABASE=True
LOG_ROTATION_SIZE=10485760
LOG_RETENTION_DAYS=90

# --- Governance demo injection rates (0 = toggles fully control) -------
PII_INJECTION_RATE=0
TOXIC_INJECTION_RATE=0
HALLUCINATION_INJECTION_RATE=0
AUTHORITY_INJECTION_RATE=0
REQUIRE_DISCLAIMER_ACCEPTANCE=True
MAX_CLARIFYING_QUESTIONS=3

# --- Splunk Observability Cloud ----------------------------------------
SPLUNK_REALM=us1
OTEL_ENABLED=True
OTEL_SERVICE_NAME=demobot-v3
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA
OTEL_PYTHON_EXCLUDED_URLS=^(https?://)?[^/]+/health$
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY
OTEL_INSTRUMENTATION_GENAI_EMITTERS=span_metric,splunk
OTEL_LOGS_EXPORTER=none
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=demobot-ec2-1   # <-- PER-REPLICA

# --- Galileo -----------------------------------------------------------
GALILEO_CONSOLE_URL=https://console.multitenant.galileocloud.io
GALILEO_PROJECT=DemoBot
GALILEO_LOG_STREAM=DemoBot
```

### `.env` — secrets (values withheld; copy from the Mac at `/Applications/DemoBot/.env`)

| Key | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | only used if `AI_PROVIDER=anthropic` |
| `AI_DEFENSE_API_KEY` | Cisco AI Defense inspection key |
| `ACCESS_KEY` | HTTP Basic password gating public access — **required** when the tunnel is up |
| `SPLUNK_ACCESS_TOKEN` | O11y **ingest** token, realm us1 |
| `SPLUNK_API_TOKEN` | O11y **API** token — ⚠️ currently expired (401), see §8 |
| `GALILEO_API_KEY` | Galileo ingest key |

### `OTEL_RESOURCE_ATTRIBUTES` — the one value you must change per replica

Each replica sets a distinct `deployment.environment` so Splunk O11y and Galileo
can separate them:

- Mac (`/Applications/DemoBot`) → `demobot-local`
- this EC2 → `demobot-ec2-1`
- next replica → `demobot-ec2-2`, and so on

Everything else in `.env` is copied verbatim between replicas.

### Cloudflare tunnel

`~/.cloudflared/config.yml`:

```yaml
tunnel: 52a942e8-dddf-4a19-81fe-7865fc94c41b
credentials-file: /home/splunk/.cloudflared/52a942e8-dddf-4a19-81fe-7865fc94c41b.json
protocol: http2

ingress:
  - hostname: medadvice.yeackbot.com
    service: http://localhost:8001
  - service: http_status:404
```

**Architecture:** every host runs the *same named tunnel* (`medadvice`, id
`52a942e8-dddf-4a19-81fe-7865fc94c41b`) as a replica. Cloudflare load-balances
across whichever replicas are connected. Per-instance SQLite means sessions are
not shared between replicas — accepted for the demo.

Only the tunnel **credentials JSON** is copied to the boxes. The Cloudflare
account cert (`cert.pem`) stays on the Mac. Consequence: `cloudflared tunnel
list` / `create` / `route` fail on the EC2 with *"Cannot determine default origin
certificate path"* — this is expected and harmless. `tunnel run` works because it
only needs the credentials file.

### OTel collector pipelines (`otel-collector-config.yaml`)

Three pipelines off one OTLP receiver (:4317 gRPC, :4318 HTTP):

| Pipeline | Processors | Exporter | Destination |
|---|---|---|---|
| `traces` | batch | `otlphttp/traces` | `https://ingest.us1.signalfx.com/v2/trace/otlp` (Splunk APM) — full traces incl. HTTP spans |
| `traces/galileo` | `filter/genai_only`, batch | `otlphttp/galileo` | `https://api.multitenant.galileocloud.io/otel/traces` — **gen_ai spans only** |
| `metrics` | batch | `signalfx` (`send_otlp_histograms: true`) | Splunk O11y metrics |

Two non-obvious settings, both load-bearing:

1. `send_otlp_histograms: true` — without it the GenAI histograms that Splunk AI
   Agent Monitoring depends on (`gen_ai.evaluation.score`, token usage) never
   ingest.
2. `filter/genai_only` drops spans without `gen_ai.operation.name` before the
   Galileo exporter. Galileo rejects batches with no GenAI patterns, so
   unfiltered FastAPI HTTP spans cause partial-success drops.

### Instrumentation quirk

`run.sh` sets `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=langchain` before launching
under `opentelemetry-instrument`. The preview Splunk LangChain auto-instrumentor
reports the model as `"unknown"` and emits no AgentInvocation for
`create_react_agent`; the app emits GenAI entities itself in
`backend/telemetry/otel.py`. FastAPI instrumentation stays enabled.

### systemd units

Three units, all `User=splunk`, `Restart=always`, `RestartSec=5`, enabled:

| Unit | ExecStart | After |
|---|---|---|
| `demobot-collector` | `~/DemoBot/run-collector.sh` | `network-online.target` |
| `demobot-app` | `/bin/bash ~/DemoBot/run.sh` | `network-online`, `ollama`, `demobot-collector` |
| `demobot-tunnel` | `/usr/bin/cloudflared --config ~/.cloudflared/config.yml tunnel run medadvice` | `network-online`, `demobot-app` |

Plus `ollama.service` (installed by Ollama, `User=ollama`, `WantedBy=default.target`).

`demobot-app` sets `Environment=PATH=/home/splunk/DemoBot/venv/bin:/usr/local/bin:/usr/bin:/bin`
so `run.sh` finds `opentelemetry-instrument` in the venv.

---

## 6. Rebuild procedure — raw EC2 → working replica

### 6.0 Launch

Ubuntu 22.04 LTS, **≥ 8 vCPU / 16 GiB RAM** (`c6i.2xlarge` matches), 100 GiB gp3
root. Security group: inbound TCP 22 (or 2222) from admin IPs only; all egress
allowed. No inbound rule is needed for 8001 — the Cloudflare tunnel dials out.

### 6.1 Prepare the account

If not using the corporate AMI, create the service user (skip if `splunk` already
exists from the image):

```bash
sudo useradd -m -s /bin/bash splunk
echo 'splunk ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/splunk
sudo install -d -m700 -o splunk -g splunk /home/splunk/.ssh
sudo cp ~/.ssh/authorized_keys /home/splunk/.ssh/ && sudo chown splunk:splunk /home/splunk/.ssh/authorized_keys
```

If sshd has an `AllowGroups` line, add `splunk` to one of the listed groups
(`sudo usermod -aG wheel splunk`) **before** you disconnect — otherwise you lock
yourself out.

### 6.2 Run the deploy

From the **Mac**, inside the repo. One command builds the whole replica:

```bash
cd /Applications/DemoBot && ./deploy/ec2/push-replica.sh --host NEW_HOST --replica 2
```

Two scripts, both in `deploy/ec2/` (the Linux mirror of `deploy/launchd/`):

| Script | Runs on | Does |
|---|---|---|
| `push-replica.sh` | Mac | stages the secret payload, ships it, invokes the bootstrap |
| `ec2-bootstrap.sh` | target | installs and configures everything, starts, health-checks |

`push-replica.sh` assembles a payload of the four things a fresh box cannot
obtain on its own — `.env`, the Mac's `config.yml`, the tunnel credentials JSON,
and `Modelfile.poisoned` (exported from the Mac's live Ollama if no file exists)
— `scp`s them to `~/demobot-payload`, runs the bootstrap, and deletes the
payload afterwards. **`cert.pem` is never staged.**

`ec2-bootstrap.sh` is idempotent and arch-aware (x86_64 / aarch64):

1. base packages; `ppa:deadsnakes`; `python3.11` + venv + dev headers
2. `cloudflared` from the GitHub `.deb`
3. Ollama + the `OLLAMA_MAX_LOADED_MODELS=2` / `KEEP_ALIVE=30m` drop-in
4. clone DemoBot, build the venv on 3.11, install requirements
5. `otelcol-contrib` binary; mask any apt `splunk-otel-collector` (:4317 clash)
6. install `.env` (0600) and tunnel credentials; rewrite `credentials-file:` in
   `config.yml` to the target's `$HOME`; warn if `cert.pem` is present
7. set `deployment.environment` (see §6.3)
8. **pull every model `.env` references**, then **build the poisoned model**
9. write the three systemd units
10. enable + start, then health-check all four endpoints

Flags: `--replica N`, `--env-name NAME`, `--stage-only`, `--no-start`,
`DEBUG=1` for tracing. Re-running is the update path as well as the install path.

### 6.3 What used to be manual, and now is not

The four post-bootstrap steps this runbook previously listed are automated. What
each one became:

| Was | Now |
|---|---|
| `ollama pull llama3.2:3b` by hand | every `OLLAMA_MODEL*` key in `.env` is pulled — adding an internal agent is a `.env` edit, not a script edit |
| `ollama create` the poisoned model by hand | built every run from one canonical `Modelfile.poisoned`, with its `FROM` base pulled first |
| `scp` secrets, hand-edit the Mac paths out of `config.yml` | `push-replica.sh` stages them; the bootstrap rewrites only the `credentials-file:` line and asserts the rewrite took |
| `sed` the replica number into `.env` | `--replica N` / `--env-name`, or an IMDS-derived unique default |

Full rationale for each in [`deploy/ec2/README.md`](../DemoBot/deploy/ec2/README.md).

> **Drift this fixes.** On 2026-07-27 the Mac's poisoned model was
> `b5eec43102c3` and the EC2's was `d22293974766` — two different models behind
> one demo, because each was built by hand at a different time.

**`deployment.environment` is still the one value that differs per replica**
(`demobot-local`, `demobot-ec2-1`, `demobot-ec2-2`, …). It is the only thing
separating replicas in Splunk O11y and Galileo — everything else, including
`service.name=demobot-v3`, is identical by design. Two replicas sharing a value
merge into one apparent service, so a sick box hides behind a healthy one. The
bootstrap rewrites only that key inside `OTEL_RESOURCE_ATTRIBUTES`, preserving
sibling attributes.

### 6.4 Manual start (only with `--no-start`)

```bash
sudo systemctl enable --now demobot-collector demobot-app demobot-tunnel
systemctl --no-pager status demobot-collector demobot-app demobot-tunnel
```

Order matters on a cold box: the collector must own :4317 before the app starts,
or the app's first spans are dropped. The unit dependencies handle this.

---

## 7. Verification

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8001/health        # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:11434/api/tags     # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8888/metrics       # 200
curl -s -o /dev/null -w '%{http_code}\n' https://medadvice.yeackbot.com/health  # 200
ollama list        # expect dolphin3:8b, dolphin3:8b-poisoned, llama3.2:3b
```

All four returned `200` on this instance at capture time.

Then confirm telemetry actually lands: in Splunk O11y (realm us1) filter on
`deployment.environment = demobot-ec2-N` and confirm the new replica appears
separately from `demobot-local`. Repo-side, `tests/observability/verify_observability.sh`
runs the same checks in tiers.

---

## 8. Known issues and gotchas

- **`SPLUNK_API_TOKEN` is expired** (401). It only affects tier 3 of
  `verify_observability.sh`, which queries O11y to confirm metrics landed. The
  *ingest* token (`SPLUNK_ACCESS_TOKEN`) is valid and telemetry flows normally.
  See **§10** for the reissue procedure.
- **`cloudflared tunnel list` fails on EC2** — no `cert.pem` by design. Only
  `tunnel run` is supported on replicas. Not a fault.
- **Two collectors, one port.** The apt `splunk-otel-collector` package is
  installed (from the Splunk apt repo on the base image) and must stay masked;
  unmasking it collides with `otelcol-contrib` on :4317.
- **Python 3.10 will not work.** The venv must be built with `python3.11` from
  deadsnakes. `python3 -m venv` picks 3.10 and LangChain 1.x fails to install.
- **OpenClaw is Mac-only.** `run-openclaw.sh` requires podman, which is not
  installed here. Note also that Cisco Secure Endpoint quarantines host-installed
  OpenClaw and deletes working-tree copies of `openclaw/Containerfile` — hence
  the podman-based approach on the Mac.
- **Per-instance SQLite.** `medadvice.db` is local to each replica; conversation
  history does not follow a user across Cloudflare's load balancing. Accepted for
  the demo; would need Postgres (the `psycopg2-binary` dep is already present) to
  fix.
- **CPU-only inference.** No GPU on `c6i`. Both models sit at 100% CPU with an
  8192 context. Keep-alive of 30m and `OLLAMA_MAX_LOADED_MODELS=2` avoid a cold
  reload between the internal 3B agents and the user-facing 8B synthesizer.

---

## 9. Backup set — what is not in git

Losing these means the instance cannot be rebuilt from the repo alone:

1. `/Applications/DemoBot/.env` on the Mac (secrets — the source of truth that
   `push-replica.sh` ships to every replica)
2. `~/.cloudflared/52a942e8-dddf-4a19-81fe-7865fc94c41b.json` (tunnel credentials)
3. `Modelfile.poisoned` (poisoned demo model recipe). Currently only on disk —
   `push-replica.sh` can re-export it from the Mac's live Ollama with
   `ollama show --modelfile dolphin3:8b-poisoned`, but that only works while the
   Mac still has the model built. Committing it to `deploy/ec2/` would make it
   durable; it is an adversarial prompt payload, so that is your call.
4. Cloudflare account `cert.pem` — Mac only, never on a replica
5. `~/.ssh/demobot_ec2` on the workstation (access key)

The build scripts are now in the repo at `deploy/ec2/` and no longer need
separate backup.

`~/DemoBot/medadvice.db` is demo state and does not need backing up.

---

## 10. Reissuing `SPLUNK_API_TOKEN`

### What kind of token

Both consumers call `https://api.us1.signalfx.com` with an `X-SF-Token` header,
so this must be a token with **API authorization scope** — *not* an ingest token
(`SPLUNK_ACCESS_TOKEN` is the ingest one and is a separate value).

| Consumer | Call | Minimum role |
|---|---|---|
| `tests/observability/check_o11y_metadata.py` | `GET /v2/metrictimeseries` | `read_only` |
| `scripts/observability/create_apm_detectors.py` | `POST` (creates detectors) | `power` |

Choose **`power`** so both work.

### Create it

In Splunk Observability Cloud — realm **us1**, `https://app.us1.signalfx.com`:

1. **Settings → Access Tokens → New Token**
2. Name it (e.g. `demobot-api`); **Authorization scope = API token**; select role
   **`power`**
3. Set visibility, then **set a long expiration date**. The default is 30 days —
   that is why the previous token expired. Max is 18 years.
4. **Create**, expand the token, **Show Token**, **Copy**

Creating org access tokens requires org admin. Alternatives:

- **Rotate** an existing token (Settings → Access Tokens → select → *Rotate
  token*) — only possible *before* it expires. Not an option for an already-dead
  token.
- Use a **personal user API access token** from your account settings: same
  header, no admin rights needed, but tied to your user account.

### Install it

Three places (Mac + each EC2 replica), same value.

Mac — BSD sed needs the empty `-i` argument:

```bash
cd /Applications/DemoBot && cp .env .env.bak && read -rs NEWTOK \
  && sed -i '' "s|^SPLUNK_API_TOKEN=.*|SPLUNK_API_TOKEN=${NEWTOK}|" .env \
  && grep -c '^SPLUNK_API_TOKEN=' .env
```

EC2 — GNU sed, no `''`:

```bash
cd ~/DemoBot && cp .env .env.bak && read -rs NEWTOK \
  && sed -i "s|^SPLUNK_API_TOKEN=.*|SPLUNK_API_TOKEN=${NEWTOK}|" .env \
  && chmod 600 .env && grep -c '^SPLUNK_API_TOKEN=' .env
```

`read -rs` keeps the secret out of shell history. Expect `1` from `grep -c`.

**No service restart is required.** `SPLUNK_API_TOKEN` is read only by the two
test/script files, at invocation, directly from `.env`. Neither `run.sh` nor
`run-collector.sh` references it — the live telemetry path uses
`SPLUNK_ACCESS_TOKEN` and is unaffected.

### Verify

Test the token in isolation before blaming telemetry:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "X-SF-Token: $NEWTOK" \
  'https://api.us1.signalfx.com/v2/metrictimeseries?query=sf_metric:gen_ai.client.token.usage&limit=1'
```

`200` = good; `401` = wrong token or missing API scope. Then run the suite:

```bash
cd ~/DemoBot && ./tests/observability/verify_observability.sh
```

### Two script-side gotchas

- **`verify_observability.sh` hard-codes `demobot-local`** as the
  `deployment.environment` argument (line 76). On a replica whose environment is
  `demobot-ec2-N`, tier 3 queries the wrong replica and fails even with a good
  token. Run the checker directly instead:

  ```bash
  python3 tests/observability/check_o11y_metadata.py us1 "$NEWTOK" demobot-v3 demobot-ec2-1
  ```

- **A token containing `=` breaks tier 3.** Line 71 parses `.env` with
  `cut -d= -f2`, truncating at the first `=` (`run-collector.sh` correctly uses
  `-f2-`). Rare with Splunk tokens, but it explains a `curl`-succeeds /
  script-fails split.
