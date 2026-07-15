# DemoBot v3 - Quick Start Guide

## Prerequisites
- Python 3.11 or higher
- Credentials for your configured AI provider

## Installation (5 minutes)

### Step 1: Navigate to Project
```bash
cd DemoBot   # the directory you cloned the repo into
```

### Step 2: Setup Environment
```bash
# Copy environment template
cp .env.example .env

# Edit .env and add your API key
nano .env  # or use any text editor
```

Add credentials for your selected AI provider:
```env
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your-actual-api-key-here

# Or use an OpenAI-compatible provider:
# AI_PROVIDER=openai
# OPENAI_API_KEY=your-key-here
# OPENAI_MODEL=gpt-4o
# OPENAI_BASE_URL=https://api.openai.com/v1
#
# DeepSeek example:
# OPENAI_MODEL=deepseek-chat
# OPENAI_BASE_URL=https://api.deepseek.com
```

### Step 3: Run the Application
```bash
# App only - the run script creates venv/, installs deps, and starts uvicorn on :8001
./run.sh

# Full stack (OTel collector + app) in one command
./start-all.sh                 # add --tunnel to also expose a public URL

# Manual way
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m backend.main
```

> **Telemetry needs the collector.** The app exports OpenTelemetry to a *local*
> OTel collector (`./run-collector.sh`), which forwards to Splunk Observability
> Cloud. If you've set `OTEL_EXPORTER_OTLP_ENDPOINT` / `SPLUNK_*` in `.env`, you
> must run the collector too, or telemetry is silently dropped (the app itself
> still serves). `./start-all.sh` starts both in the right order. If you haven't
> configured telemetry, `./run.sh` on its own is enough.

### Step 4: Verify it's working
```bash
./verify_installation.sh                        # files, venv, and config sanity
curl -s http://localhost:8001/health            # want: healthy
# If you configured telemetry, confirm the whole pipeline reaches Splunk:
./tests/observability/verify_observability.sh
```

## Access the Application

Once running, open your browser:

| Interface | URL | Purpose |
|-----------|-----|---------|
| **Chat** | http://localhost:8001/app | Main medical guidance interface |
| **Admin** | http://localhost:8001/admin-ui | Metrics and escalation queue |
| **Governance** | http://localhost:8001/governance-ui | AI interaction logs |
| **API Docs** | http://localhost:8001/docs | Interactive API documentation |

## Public Access (optional)

Expose the app on the public internet behind a single access key — no cloud
deploy; the local SQLite database and `.env` stay where they are.

### Step 1: Set an access key
Add a shared secret to `.env`. Every request (except `/health`) then requires it
as the HTTP Basic Auth password — one key gates both the web UIs and the API.
```env
# Generate one with: openssl rand -hex 24
ACCESS_KEY=your-long-random-secret
```
Leave `ACCESS_KEY` empty/unset to keep the app open for local development.

### Step 2: Start the server, then the tunnel
```bash
brew install cloudflared   # one-time prerequisite

./run.sh                   # terminal 1 — the app on :8001
./tunnel.sh                # terminal 2 — public HTTPS tunnel
```
`tunnel.sh` prints a `https://<random>.trycloudflare.com` URL (a new one each
run). Cloudflare terminates TLS, so the access key travels encrypted.

### Step 3: Open the URL
Browse to the printed URL; you'll land on a **login page** — enter the access
code to continue (the browser can offer to save it). For scripts/API clients,
HTTP Basic Auth still works:
```bash
curl -u x:your-long-random-secret https://<random>.trycloudflare.com/health
```

> ⚠️ Anyone with both the URL and the key can reach the admin/governance
> dashboards and drive the AI. Share them only with people you trust; rotate by
> changing `ACCESS_KEY` and restarting.

## Keep it running (optional, macOS)

To have the collector + app (and optionally the tunnel) auto-start at login and
restart on crash, install the bundled launchd agents:
```bash
./deploy/launchd/install.sh
```
See [deploy/launchd/README.md](deploy/launchd/README.md) for status/restart/uninstall.

## First Steps

1. **Open Chat Interface**: http://localhost:8001/app
2. **Accept Disclaimer**: Read and accept the medical disclaimer
3. **Ask a Question**: Try "I have a headache for 2 days"
4. **View Logs**: Check the admin dashboard to see governance logs

## Example Queries to Try

### Normal Queries
- "I have a mild headache that started yesterday"
- "What can I do for a sore throat?"
- "I've been feeling tired lately"

### Triggers Clarifying Questions
- "I don't feel well" (will ask for more details)
- "I'm having some pain" (will ask about severity, location)

### Triggers Escalation
- "I'm having chest pain" (EMERGENCY)
- "My 6-month-old has a fever" (vulnerable population)
- "I'm pregnant and have severe headaches" (pregnancy + symptoms)

## Viewing Governance Logs

1. Make a query in the chat interface
2. Note the Session ID at the top
3. Go to http://localhost:8001/governance-ui
4. Enter the Session ID
5. View complete AI governance logs including:
   - Token usage
   - Safety flags
   - PII detection
   - Escalation triggers
   - Performance metrics

## Testing PII Detection

The system randomly injjects PII in 5% of responses for testing. When this happens, you'll see:
- PII flag in governance logs
- Types of PII detected
- This is INTENTIONAL for testing the detection system

## Viewing Escalations

1. Go to http://localhost:8001/admin-ui
2. Scroll to "Escalation Queue"
3. See all cases flagged for human review
4. Click "Review" to mark as reviewed/resolved

## File Locations

### Logs
All logs are in `logs/` directory:
- `ai_governance.json` - All AI interactions
- `escalations.json` - Escalated cases
- `audit_trail.json` - System audit events
- `errors.json` - Error logs

### Database
- `medadvice.db` - SQLite database with all data

### Configuration
- `.env` - Your configuration (API keys, settings)

## Common Issues

### "API key not configured"
Make sure you've added credentials for your configured provider to `.env`:
```env
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Or:
# AI_PROVIDER=openai
# OPENAI_API_KEY=your-key-here
```

### Port 8001 already in use
Change the port in `.env`:
```env
PORT=8002
```

### Module not found errors
Make sure you've installed dependencies:
```bash
source venv/bin/activate
pip install -r requirements.txt
```

### Errors after moving or renaming the project folder
A Python `venv` is **not relocatable** — `venv/bin/activate` and every console
script bake in the venv's absolute path. If you rename/move the repo, activation
silently fails and `run.sh` falls back to the system Python (symptoms: pip
"Defaulting to user installation", `opentelemetry-instrument: not found`, or
500s on every route). Recreate the venv in place:
```bash
rm -rf venv && ./run.sh          # rebuilds venv/ at the new path
```
If you use the launchd auto-start agents, also re-run `./deploy/launchd/install.sh`
so they point at the new path.

### Database errors
Delete and recreate the database:
```bash
rm medadvice.db
python -m backend.main
```

## Stopping the Application

Press `Ctrl+C` in the terminal where the application is running.

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Explore the API at http://localhost:8001/docs
- Customize escalation rules in `backend/services/escalation_rules.py`
- Adjust clarifying questions in `backend/services/clarifying_questions.py`
- Review governance logs to understand AI behavior

## Architecture Overview

```
User → Chat UI → FastAPI → AI Provider → Response
                     ↓
            Governance Logger
                     ↓
        ┌────────────┼────────────┐
        ↓            ↓            ↓
    File Logs    Database    Console
```

## Key Features to Test

1. **Clarifying Questions** - Ask vague questions
2. **Severity Assessment** - Ask about various symptoms
3. **Escalation System** - Trigger emergency keywords
4. **PII Detection** - Wait for random PII injection
5. **Admin Dashboard** - Monitor metrics in real-time
6. **Governance Logs** - Full traceability of AI decisions

## Support

For detailed information, see:
- [README.md](README.md) - Complete documentation
- [API Documentation](http://localhost:8001/docs) - Interactive API docs
- Logs in `logs/` directory - Troubleshooting information

## Safety Notice

⚠️ **This is a demonstration application**

- NOT for actual medical use
- NOT FDA approved
- NOT HIPAA compliant out-of-the-box
- Always consult real healthcare professionals

Enjoy exploring DemoBot v3! 🏥
