---
name: launch-medadvice
description: Launch / run / start / serve the DemoBot app AND its required services (the OpenTelemetry collector that forwards telemetry to Splunk) — locally on http://localhost:8001 or publicly via a Cloudflare tunnel behind the access key. Use when asked to run, start, serve, boot, or expose this application, or to confirm it's serving.
---

# Launch DemoBot

DemoBot is a FastAPI app (`backend/main.py`) launched by `./run.sh`, which
activates `venv/`, then runs `python -m backend.main` → uvicorn on
**`0.0.0.0:8001`**. It serves a chat UI at `/app`, admin/governance UIs, `/docs`,
and the `/api/chat` + `/admin` JSON APIs.

There are **two launch modes** — pick based on what the user asked for:

| Mode | Reach | Use when |
|------|-------|----------|
| **A — Local** | `localhost` + your LAN | Day-to-day dev, testing a change |
| **B — Public tunnel** | A public HTTPS URL | Sharing the app over the internet |

Both modes run the *same* server (Mode B just adds a tunnel in front). The
access-key gate (below) applies identically to both.

**Launching the app means bringing up ALL its services, not just the web server:**
1. the **OTel collector** (`./run-collector.sh`) — forwards telemetry to Splunk
   Observability Cloud. **Easy to forget, and the #1 incident:** without it the app
   runs fine but NO telemetry reaches O11y (exports fail silently), and it dies
   when the laptop sleeps.
2. the **app** (`./run.sh`).
3. (Mode B only) the **public tunnel** (`./tunnel.sh`).

One command brings up collector + app: **`./start-all.sh`** (add `--tunnel` for
the tunnel). When launching as the agent, start `./run-collector.sh` and
`./run.sh` as separate background tasks (plus `./tunnel.sh` for Mode B). After
launch, confirm the whole pipeline with
`./tests/observability/verify_observability.sh`.

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
