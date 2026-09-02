---
name: launch-medadvice
description: Launch / run / start / serve the DemoBot app AND its required services (the OpenTelemetry collector that forwards telemetry to Splunk) — locally on http://localhost:8001 or publicly via a Cloudflare tunnel behind the access key. Use when asked to run, start, serve, boot, or expose this application, or to confirm it's serving.
---

# Launch DemoBot

DemoBot is a FastAPI app (`backend/main.py`) launched by `./run.sh`, which
activates `venv/`, then runs `python -m backend.main` → uvicorn on
**`0.0.0.0:8001`**. It serves a chat UI at `/app`, admin/governance UIs, `/docs`,
and the `/api/chat` + `/admin` JSON APIs.

There are **three launch modes** — pick based on what the user asked for:

| Mode | Reach | Use when |
|------|-------|----------|
| **A — Local** | `localhost` + your LAN | Day-to-day dev, testing a change |
| **B — Public tunnel** | A public HTTPS URL | Sharing the app over the internet |
| **C — Agentic surface** | Local + OpenClaw gateway | Demoing agentic tool-abuse governance |

Modes A/B run the *same* server (Mode B just adds a tunnel in front). Mode C
adds the OpenClaw gateway on top of A. The access-key gate (below) applies
identically to all.

**Launching the app means bringing up ALL its services, not just the web server:**
1. the **OTel collector** (`./run-collector.sh`) — forwards telemetry to Splunk
   Observability Cloud. **Easy to forget, and the #1 incident:** without it the app
   runs fine but NO telemetry reaches O11y (exports fail silently), and it dies
   when the laptop sleeps.
2. the **app** (`./run.sh`).
3. (Mode B only) the **public tunnel** (`./tunnel.sh`).
4. (Mode C only) the **OpenClaw gateway** (`./run-openclaw.sh`) — a *fourth*
   forgettable service. It is **off by default** (starting it is the opt-in).

One command brings up collector + app: **`./start-all.sh`** (add `--tunnel` for
the tunnel, `--agentic` for the OpenClaw gateway). When launching as the agent,
start `./run-collector.sh` and `./run.sh` as separate background tasks (plus
`./tunnel.sh` for Mode B, `./run-openclaw.sh` for Mode C). After launch, confirm
the whole pipeline with `./tests/observability/verify_observability.sh` (and
`./tests/observability/verify_openclaw_observability.sh` for Mode C).

## Prerequisites (both modes)

- `.env` exists with the AI provider key — for the default Anthropic provider,
  `ANTHROPIC_API_KEY=sk-ant-...`. Without it the app boots but chat fails.
- `venv/` exists (run.sh creates it and installs `requirements.txt` on first run).
- **Access key:** `ACCESS_KEY` in `.env` gates every route except `/health`.
  Empty/unset ⇒ gate disabled (open). Generate one with `openssl rand -hex 24`.

## The access-key gate (how to get in)

Enforced by `backend/middleware/access_key.py`. Two ways to authenticate:

- **Browser** → any gated URL shows a styled "Access required" page → **Enter
  access code** → `/login` form → enter the `ACCESS_KEY` value → an HttpOnly
  cookie is set and you land in the app. (`backend/routers/auth.py`,
  `frontend/login.html`.)
- **API / curl** → HTTP Basic Auth with the key as the password, any username:
  `curl -u x:$ACCESS_KEY ...`.

`/health` is the only unauthenticated route (used by health/uptime checks).

---

## Mode A — Local (localhost)

```bash
./start-all.sh                 # OTel collector + app (both backgrounded)
# equivalently, separately (collector FIRST so the app's first exports land):
#   ./run-collector.sh &
#   ./run.sh
```

Wait for `Application startup complete`, then open **http://localhost:8001/app**
and log in with the access code. The collector must be running too, or telemetry
silently won't reach Splunk.

Other URLs: `/admin-ui`, `/governance-ui`, `/docs`.

---

## Mode B — Public (Cloudflare quick tunnel)

Exposes the *locally running* server to the internet — no cloud deploy; local
SQLite (`medadvice.db`) and `.env` stay in place. The tunnel relies on the
access key for protection, so **make sure `ACCESS_KEY` is set** before sharing.

```bash
brew install cloudflared        # one-time prerequisite

./start-all.sh --tunnel         # OTel collector + app + public tunnel
# equivalently, separately: ./run-collector.sh &  ./run.sh &  ./tunnel.sh
```

`tunnel.sh` prints a `https://<random>.trycloudflare.com` URL — **a new one each
run** (quick tunnels are ephemeral). Open it in a browser and log in with the
access code; Cloudflare terminates TLS so the code travels encrypted.

To stop sharing, Ctrl+C the tunnel (terminal 2). The app keeps running.

---

## Mode C — Agentic surface (OpenClaw gateway)

Adds an OpenClaw agent with **real tools** (read/write/exec/web_fetch) so the
demo can show agentic tool abuse — indirect prompt injection, exfiltration —
being governed at the tool boundary by `/api/toolguard/inspect` (Cisco AI
Defense + the deterministic tool policy). Every tool call becomes an
`execute_tool` span and a `tool_call` governance event.

The gateway runs in **podman** (Cisco Secure Endpoint quarantines a host npm
install — see the napkin), against local **Ollama `llama3.2:3b`** (tool-capable;
`mistral-nemo:12b` has no tools template), workspace pinned to the decoy `~/DemoBotDecoy`.

```bash
podman machine start                          # if not already running
venv/bin/python scripts/demo/seed_agentic_decoy.py   # one-time: seed the decoy workspace
./run.sh &  ./run-collector.sh &              # app + collector MUST be up first
./run-openclaw.sh                             # start the gateway (builds the image on first run)
# or all at once:  ./start-all.sh --agentic
```

`run-openclaw.sh` flags: `--no-telemetry` (don't export gateway spans while
iterating on the plugin), `--foreground` (block on the container for launchd
supervision). Auto-start at login is opt-in: `./deploy/launchd/install.sh --with-openclaw`.

`openclaw/` is tracked but **not checked out on this Mac** (endpoint security
deletes OpenClaw files from the working tree). The image is built from
`git archive HEAD:openclaw`, so the build needs no files on disk — but it also
means a rebuild is triggered by a **commit** to `openclaw/`, not by staged edits,
and that rebuild costs a few minutes. Do it before a demo, not during one. Read
or change the directory with `scripts/openclaw-edit.sh`; never `git restore
openclaw/`.

**Toggling the integration off:** nothing in the app calls the gateway, so
`podman stop demobot-openclaw` fully removes it — Modes A/B are unaffected.
`TOOL_GUARD_ENABLED` (default False) is a *separate* switch: it only controls
whether a block is *enforced*, not whether the integration runs (False = the
unguarded control run — telemetry still flows, blocks are computed but not
applied). The demo contrast is `False` (agent exfiltrates) vs `True` (blocked).

**⚠️ The guard is a hard dependency.** With `TOOL_GUARD_ENABLED=True` the guard
is **fail-closed**: if the app (`:8001`) is down, EVERY agent tool call is
denied. That presents as a *broken agent*, not a broken app — so if the agent
suddenly can't do anything, check that `./run.sh` is up before debugging the gateway.

Stop it: `podman stop demobot-openclaw`. Logs: `podman logs -f demobot-openclaw`.
Gateway port **18789**.

---

## Mode D — NemoClaw runtime (alternative agentic surface, GPU replica / Colima)

NVIDIA NemoClaw = OpenClaw inside an OpenShell sandbox. DemoBot's governance
seat (the `demobot-toolguard` plugin) is baked into the sandbox image, so its
tool calls hit `/api/toolguard/inspect` like Mode C, and the sandbox's own
denials are forwarded (`scripts/nemoclaw/ocsf_forwarder.py`) as
`nemoclaw_guardrails` governance events. **NemoClaw does not support podman**,
so on this Mac it needs `brew install colima docker && colima start`; the
normal host is an EC2 replica (`deploy/ec2/ec2-bootstrap.sh --with-nemoclaw`).

```bash
./run-nemoclaw.sh                # onboard / start (refuses with the reason if the host cannot run it)
./start-all.sh --nemoclaw        # collector + app + NemoClaw
nemoclaw demobot-nemoclaw stop ; pkill -f ocsf_forwarder.py   # stop
```

The drawer's **NemoClaw Guardrails** toggle is the enforcement switch for the
policy layer (works without the runtime); the pill reads RUNTIME once the
sandbox reports denials. Verify: `./tests/observability/verify_nemoclaw_observability.sh`.

## provider=nvidia (local NIM) and the NVIDIA toggles

- `provider=nvidia` is a NIM container on **this host** (`localhost:8000`) —
  never a cloud API. This Mac has no NVIDIA GPU, so it is greyed out here; on a
  GPU replica `ec2-bootstrap.sh --with-nim [model]` runs `demobot-nim.service`.
- **NeMo Guardrails** (drawer toggle) needs `nemoguardrails` installed (core
  only) and the Settings card's master switch. Judge = the active chat model.
- **Blueprint** dropdown (header) switches the agentic architecture; every
  guardrail runs in both. Details: `docs/nvidia-integration.md`.

## Verify it's serving

```bash
KEY=$(grep '^ACCESS_KEY=' .env | cut -d= -f2)
curl -s -o /dev/null -w "health=%{http_code}\n"            http://localhost:8001/health          # want 200
curl -s -o /dev/null -w "no-key=%{http_code}\n"            http://localhost:8001/admin/logs/metrics  # want 401
curl -s -o /dev/null -w "with-key=%{http_code}\n" -u x:$KEY http://localhost:8001/admin/logs/metrics # want 200
```

For Mode B, swap `http://localhost:8001` for the printed `trycloudflare.com` URL.

## Stop it

```bash
lsof -ti:8001 | xargs kill          # stop the app (add -9 if it lingers)
podman stop demobot-openclaw        # Mode C only: stop the OpenClaw gateway
```
If launched in the foreground, Ctrl+C in that terminal instead.

## Gotchas (learned the hard way)

- **Reload loop:** with `DEBUG=True`, uvicorn auto-reloads on file changes. The
  app writes `medadvice.db` and `logs/*.json` as it serves, which would trigger
  a reload after almost every request — so `backend/main.py` passes
  `reload_excludes=["*.db","*.db-journal","*.db-wal","*.log","*.json"]`. Keep
  that. For a stable/public run, prefer `DEBUG=False` in `.env` (disables reload
  and verbose tracebacks entirely).
- **Two `cloudflared` runs = two different URLs.** The previous URL dies when you
  restart the tunnel. For a stable URL you'd need a named tunnel + a domain.
- **Synthetic data:** this is a demo app that deliberately injects synthetic
  PII/toxic/hallucinated content (`*_injection_rate` in `backend/config.py`).
  Don't treat it as a real medical service.
- **Port already in use:** something's already on 8001 — `lsof -ti:8001 | xargs
  kill`, or change `PORT` in `.env` (and pass the new port to `./tunnel.sh`).
