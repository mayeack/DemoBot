---
name: fan-tunnel-to-galileo
description: Fan a DemoBot public tunnel's (medadviceN.yeackbot.com) LLM telemetry into a Galileo project so chat turns land as traces with governance metadata. Use when asked to "send this box's logs/traces to Galileo", to point a replica at a specific Galileo Project Name, or to verify why a box shows no traces in the Galileo console.
---

# Fan a DemoBot tunnel's LLM telemetry into Galileo

Takes a running DemoBot replica behind `medadviceN.yeackbot.com` and makes its
chat turns appear in a named Galileo project + log stream. Sibling of
`wire-tunnel-logs-to-o11y`, which does the same job for Splunk Log Observer.

## The one thing that trips everyone up

**There are two independent paths into Galileo, and they behave differently.**
Both are driven by the *same four* `.env` keys, so it is easy to assume one
working path means both are working.

```
Path A — SDK (backend/galileo_integration.py), the one that matters
  chat turn -> governance logger -> GalileoLogger (daemon thread)
    -> POST api.multitenant.galileocloud.io/ingest/traces/<project_id>
    -> trace "chat turn" carrying safety / PII / toxicity / policy / eval metadata

Path B — OTel Collector fan-out (otel-collector-config.yaml)
  app OTLP :4317 -> filter/genai_only -> otlphttp/galileo
    -> POST api.multitenant.galileocloud.io/otel/traces
    -> raw gen_ai.* spans (model + token telemetry), no governance fields
```

Path A is what the workshop demos — it is the only one carrying the governance
picture, because those flags are computed by graph nodes *after* the LLM call
returns, which a LangChain callback can't see. Path B is the model/token view.

Consequences worth knowing before you debug anything:

- **Path A is a no-op with no error when `GALILEO_API_KEY` is unset.** It is
  defensive by design and can never break a chat turn. Silence is the failure
  mode, not an exception.
- **Path B's counter legitimately sits at `0` on an idle box.** `filter/genai_only`
  drops every span that lacks `gen_ai.operation.name` — which is every FastAPI
  HTTP span, i.e. all health checks and UI polling. Until someone sends a real
  chat turn, `otelcol_exporter_sent_spans{exporter="otlphttp/galileo"}` does not
  exist in the metrics output at all. That is correct, not broken.
- The filter is **required**. Without it Galileo rejects the batch with
  "No GenAI patterns detected in spans" and drops good spans along with the HTTP noise.

## Inputs

| Input | Example | Where it comes from |
|---|---|---|
| Tunnel | `https://medadvice1.yeackbot.com/app` | the assignment row |
| Galileo Project Name | `DemoBot` | the Galileo console, **exact string, case-sensitive** |
| Galileo Log Stream | `DemoBot` | same; create it in the console if absent |
| Console URL | `https://console.multitenant.galileocloud.io` | browser address bar |
| API key | (in the Mac's `.env`, gitignored) | Galileo console > user menu > API keys |

Box `demobot-N` serves `medadviceN.yeackbot.com`. Find its IP — never hand-derive it:

```bash
cd /Applications/DemoBot && ./deploy/ec2/fleet.sh status
```

SSH is `ubuntu@<ip>` port 22 with `~/.ssh/demobot_ec2`. (The *shared lab* box
from `[[demobot-ec2-deployment]]` is `splunk`@2222 — different machine, different
convention. See `[[demobot-gpu-fleet]]`.)

## Step 0 — is it already wired?

Almost always the answer on a fleet box is **yes**, because `push-replica.sh`
ships the Mac's `.env` verbatim and the Mac has been pointed at Galileo since
before the fleet existed. Check before changing anything:

```bash
ssh -i ~/.ssh/demobot_ec2 ubuntu@<ip> \
  'grep -n "^GALILEO_" ~/DemoBot/.env | sed -E "s/(GALILEO_API_KEY=).*/\1<REDACTED>/"'
```

Want:

```
GALILEO_API_KEY=<redacted>
GALILEO_CONSOLE_URL=https://console.multitenant.galileocloud.io
GALILEO_PROJECT=DemoBot
GALILEO_LOG_STREAM=DemoBot
```

If those four are present and correct, skip to **Step 3 (verify)**. If
`GALILEO_PROJECT` names the wrong project, that is the only value you need to
change — everything else is already in place.

## Step 1 — confirm the project and log stream exist

Galileo will **not** auto-create a project from an OTLP header. Path B silently
drops spans for an unknown project; Path A logs a lookup and gives up. Create
both in the console first (`Create new project`, then a log stream inside it),
or confirm what's there:

```bash
cd /Applications/DemoBot
export K=$(grep '^GALILEO_API_KEY=' .env | cut -d= -f2-)

# project id by name
curl -s "https://api.multitenant.galileocloud.io/projects?project_name=DemoBot&type=gen_ai" \
  -H "Galileo-API-Key: $K" | python3 -c "import sys,json;[print(p['id'],p['name']) for p in json.load(sys.stdin)]"

# its log streams
curl -s "https://api.multitenant.galileocloud.io/projects/<project_id>/log_streams/paginated?starting_token=0&limit=50" \
  -H "Galileo-API-Key: $K" | python3 -c "import sys,json;[print(s['name'],s['id']) for s in json.load(sys.stdin)['log_streams']]"
```

Note the **`console` -> `api` host swap**: the console lives at
`console.multitenant.galileocloud.io`, every API and ingest call goes to
`api.multitenant.galileocloud.io`. The SDK derives one from the other; the
collector config hard-codes the API host.

## Step 2 — point the box at the project

Edit the **Mac's** `/Applications/DemoBot/.env` so every future replica inherits
it, then re-push. Editing only the box is fine for a hotfix but is lost on the
next `push-replica.sh`.

```
GALILEO_API_KEY=<key>
GALILEO_CONSOLE_URL=https://console.multitenant.galileocloud.io
GALILEO_PROJECT=DemoBot
GALILEO_LOG_STREAM=DemoBot
```

Then confirm the two consumers are wired — on a current box both already are,
this is the checklist for a box built from an older bootstrap:

1. **`run-collector.sh`** exports all three collector-facing keys from `.env`
   (`GALILEO_API_KEY`, `GALILEO_PROJECT`, `GALILEO_LOG_STREAM`) *and* passes them
   with `-e` on the container-fallback `run` line. The config's `${env:...}`
   must always resolve, so export them even when empty.
2. **`otel-collector-config.yaml`** has the `filter/genai_only` processor, the
   `otlphttp/galileo` exporter, and the `traces/galileo` pipeline wiring the two
   together. Verbatim block: `reference/otel-galileo-pipeline.yaml`.
3. The app venv has the SDK: `~/DemoBot/venv/bin/pip show galileo` (2.3.0 known good).

Ship and restart:

```bash
cd /Applications/DemoBot
./deploy/ec2/push-replica.sh --host <ip> --replica N     # or edit .env on the box
ssh -i ~/.ssh/demobot_ec2 ubuntu@<ip> \
  'sudo systemctl restart demobot-collector demobot-app'
```

Restarting `demobot-app` drops in-memory chat sessions. Don't do it mid-demo.

## Step 3 — verify

Never verify from an idle box — both paths only move on a real chat turn. Drive
one through the public tunnel. `/api/*` is access-key protected via HTTP Basic
(`./deploy/ec2/fleet.sh urls` prints the key):

```bash
curl -s -u "x:<ACCESS_KEY>" -X POST https://medadviceN.yeackbot.com/api/chat/message \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"galileo-verify-'"$(date +%s)"'","message":"What can I take for a mild headache?","disclaimer_accepted":true}' \
  -w '\nHTTP %{http_code}\n' | tail -c 200
```

Then check all three layers. Any one alone can lie.

```bash
ssh -i ~/.ssh/demobot_ec2 ubuntu@<ip> 'sleep 15
  # Path B — collector counters. Want a nonzero otlphttp/galileo line and no send_failed.
  curl -s http://localhost:8888/metrics \
    | grep -E "otelcol_(exporter_(sent|send_failed)_spans|processor_filter_spans_filtered)"

  # Path A — SDK. Want "logged turn (... project=X, log_stream=Y, traces=1)".
  sudo journalctl -u demobot-app --since "5 minutes ago" --no-pager | grep -i galileo | tail -4

  # collector-side rejections, if any
  sudo journalctl -u demobot-collector --since "5 minutes ago" --no-pager | grep -iE "galileo|error"'
```

A healthy result looks like:

```
otelcol_exporter_sent_spans{exporter="otlphttp/galileo",server_address="api.multitenant.galileocloud.io",url_path="/otel/traces"} 20
otelcol_processor_filter_spans_filtered{filter="filter/genai_only"} 199
... backend.galileo_integration - INFO - galileo: logged turn (model=..., agents=1, project=DemoBot, log_stream=DemoBot, traces=1)
```

The `filter_spans_filtered` count being far larger than the sent count is normal
— roughly 90% of spans are HTTP noise.

Finally, close the loop from Galileo's own side rather than trusting a 200:

```bash
cd /Applications/DemoBot && export K=$(grep '^GALILEO_API_KEY=' .env | cut -d= -f2-)
curl -s -X POST "https://api.multitenant.galileocloud.io/projects/<project_id>/traces/search" \
  -H "Galileo-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"log_stream_id":"<log_stream_id>","limit":5,"sort":{"column_id":"created_at","ascending":false}}' \
| python3 -c "import sys,json;d=json.load(sys.stdin);print('total',d.get('num_records'));[print(r['created_at'],r.get('name')) for r in d['records']]"
```

The newest `created_at` should be within seconds of the turn you just sent. In
the console: **Projects > DemoBot > Logs: DemoBot**.

## Gotchas

- The log stream's `updated_at` in the API is **metadata** modified time, not
  last-trace time. A stream showing "13 days ago" can be receiving traffic right
  now. Use `traces/search`, not the stream listing, to answer "is data flowing".
- Header keys on the collector exporter are fixed and lowercase —
  `Galileo-API-Key`, `project`, `logstream`. Only the values come from env.
- Both paths are keyed off `GALILEO_API_KEY` being non-empty. Blanking it turns
  the whole integration off silently — that is the intended kill switch.
- `otlphttp` warns as a deprecated alias (`otlp_http`) on otelcol-contrib 0.157.0.
  Harmless; don't "fix" it and break the config on older binaries.
- Restarting `demobot-collector` resets the `:8888` counters to zero. A zero after
  a restart proves nothing until you send a turn.
- Project name is case-sensitive and must already exist. `demobot` != `DemoBot`.
- Path A runs on a daemon thread — a Galileo outage costs nothing at the app
  layer, so "the demo works" is not evidence that Galileo is receiving anything.

## Applied instances

| Tunnel | EC2 | Galileo project | Log stream | Verified |
|---|---|---|---|---|
| medadvice1.yeackbot.com | `i-00d04361a1a98a417` / 98.84.188.131 | `DemoBot` (`1303418e-403e-4e5d-9646-551bf3e581cf`) | `DemoBot` (`db91529c-84cb-49c4-b13a-91a089debd18`) | 2026-07-29 — both paths |
