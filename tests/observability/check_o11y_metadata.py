#!/usr/bin/env python3
"""Assert that GenAI metrics + metadata are present in Splunk Observability Cloud.

Queries the SignalFx metric-timeseries metadata API for the service and checks:
  - gen_ai.client.token.usage exists, with gen_ai.token.type in {input, output}
    and at least one real (non-"unknown_model") gen_ai.request.model dimension
  - at least one gen_ai.agent.name is present, so the named agent reaches AI Agent
    Monitoring's "AI agents" view (this regressed when only raw spans were emitted
    instead of a util-genai AgentInvocation — see backend/telemetry/otel.py)
  - gen_ai.client.operation.duration exists
  - FRESHNESS: each of those metrics actually stored a datapoint recently

The freshness assertion exists because metadata alone is not evidence of a
working pipeline. ``/v2/metrictimeseries`` returns an MTS that was created days
ago and has received nothing since, so this script reported a green Tier 3
throughout the 2026-07-29+ incident in which the org stopped accepting NEW
``gen_ai.client.*`` MTS and the AI-overview panels went blank. Existence is
cheap; recency is the signal. Freshness is read from the SignalFlow API, which
returns actual datapoints rather than metadata.

Retries for a few minutes to absorb ingest/processing lag.

Usage:
    check_o11y_metadata.py <realm> <api_token> <service_name> [environment]
                           [--no-agent] [--max-age-s N] [--skip-freshness]

    --no-agent  relax the gen_ai.agent.name assertion. OpenClaw's GenAI metrics
                (service openclaw-gateway) carry gen_ai.token.type / provider /
                operation / request.model but NOT gen_ai.agent.name, so its
                "AI agents" view is legitimately empty — token+duration+model
                are still asserted.
    --max-age-s newest datapoint must be at most this old (default 900).
    --skip-freshness
                assert existence only. For runs that drive no traffic; do NOT
                use it to paper over a genuinely stalled pipeline.

Exit 0 = all present, non-zero = missing/incomplete.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

RETRY_SECONDS = 180
POLL_EVERY = 15
MAX_AGE_S = 900          # newest datapoint must be at most this old
FRESH_WINDOW_MIN = 60    # how far back to look for one


def newest_datapoint_ms(realm: str, token: str, metric: str, service: str,
                        env: str = None):
    """Epoch-ms of the most recent stored datapoint, or None if the metric has
    stored nothing in the window.

    Uses SignalFlow (real datapoints) rather than /v2/metrictimeseries
    (metadata), because a stalled metric keeps its metadata entry forever.
    """
    now = int(time.time() * 1000)
    start = now - FRESH_WINDOW_MIN * 60 * 1000
    flt = f"filter('service.name', '{service}')"
    if env:
        flt += f" and filter('deployment.environment', '{env}')"
    program = f"data('{metric}', filter={flt}).publish()"
    url = (f"https://stream.{realm}.signalfx.com/v2/signalflow/execute"
           f"?start={start}&stop={now}&resolution=60000")
    req = urllib.request.Request(
        url, data=program.encode(),
        headers={"X-SF-Token": token, "Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode()
    # Server-sent events: blocks of "event: <kind>" + "data: <json fragment>".
    newest = None
    for block in raw.split("\n\n"):
        kind, payload = None, []
        for ln in block.splitlines():
            if ln.startswith("event:"):
                kind = ln.split(":", 1)[1].strip()
            elif ln.startswith("data:"):
                payload.append(ln.split(":", 1)[1])
        if kind != "data" or not payload:
            continue
        try:
            obj = json.loads("".join(payload))
        except ValueError:
            continue
        if obj.get("data"):
            ts = obj.get("logicalTimestampMs")
            if ts and (newest is None or ts > newest):
                newest = ts
    return newest


def mts(realm: str, token: str, query: str) -> dict:
    url = f"https://api.{realm}.signalfx.com/v2/metrictimeseries?" + urllib.parse.urlencode(
        {"query": query, "limit": "200"}
    )
    req = urllib.request.Request(url, headers={"X-SF-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main() -> int:
    args = list(sys.argv[1:])
    require_agent = "--no-agent" not in args
    check_freshness = "--skip-freshness" not in args
    max_age = MAX_AGE_S
    if "--max-age-s" in args:
        i = args.index("--max-age-s")
        try:
            max_age = int(args[i + 1])
        except (IndexError, ValueError):
            print("--max-age-s needs an integer")
            return 2
        del args[i:i + 2]
    args = [a for a in args if a not in ("--no-agent", "--skip-freshness")]
    if len(args) < 3:
        print("usage: check_o11y_metadata.py <realm> <api_token> <service> [env] "
              "[--no-agent] [--max-age-s N] [--skip-freshness]")
        return 2
    realm, token, service = args[0], args[1], args[2]
    env = args[3] if len(args) > 3 else None
    svc = f'service.name:{service}' + (f' AND deployment.environment:{env}' if env else "")
    queries = {
        "gen_ai.client.token.usage": f"sf_metric:gen_ai.client.token.usage AND {svc}",
        "gen_ai.client.operation.duration": f"sf_metric:gen_ai.client.operation.duration AND {svc}",
    }

    deadline = time.time() + RETRY_SECONDS
    counts, tu = {}, {}
    while True:
        try:
            for name, q in queries.items():
                counts[name] = mts(realm, token, q).get("count", 0)
            tu = mts(realm, token, queries["gen_ai.client.token.usage"])
        except Exception as exc:  # noqa: BLE001
            print(f"  O11y API error: {exc}")
            return 3
        if all(counts.values()):
            break
        if time.time() >= deadline:
            break
        time.sleep(POLL_EVERY)

    models, ttypes, agents = set(), set(), set()
    for r in tu.get("results", []):
        dim = r.get("dimensions", {})
        if dim.get("gen_ai.request.model"):
            models.add(dim["gen_ai.request.model"])
        if dim.get("gen_ai.token.type"):
            ttypes.add(dim["gen_ai.token.type"])
        if dim.get("gen_ai.agent.name"):
            agents.add(dim["gen_ai.agent.name"])

    print(f"  gen_ai.client.token.usage MTS: {counts.get('gen_ai.client.token.usage', 0)} "
          f"| token.types={sorted(ttypes)} | models={sorted(models)}")
    _agent_note = "" if require_agent else " (not required: --no-agent)"
    print(f"  gen_ai.agent.name(s): {sorted(agents) or '(none -> AI agents view is empty!)'}{_agent_note}")
    print(f"  gen_ai.client.operation.duration MTS: {counts.get('gen_ai.client.operation.duration', 0)}")

    # Freshness: the MTS above may exist yet have stored nothing for days.
    fresh_ok = True
    if check_freshness:
        now_ms = int(time.time() * 1000)
        for name in queries:
            try:
                newest = newest_datapoint_ms(realm, token, name, service, env)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name}: freshness check errored: {exc}")
                fresh_ok = False
                continue
            if newest is None:
                print(f"  {name}: NO datapoint in the last {FRESH_WINDOW_MIN}m -> STALE")
                fresh_ok = False
            else:
                age = int((now_ms - newest) / 1000)
                verdict = "fresh" if age <= max_age else f"STALE (> {max_age}s)"
                print(f"  {name}: newest datapoint {age}s ago -> {verdict}")
                if age > max_age:
                    fresh_ok = False
        if not fresh_ok:
            print("  ^ metadata exists but nothing is being STORED. Most likely the org is")
            print("    refusing new gen_ai.client.* MTS creation (per-metric cardinality")
            print("    cap) — check sf.org.numLimitedMetricTimeSeriesCreateCalls, and note")
            print("    the flattened <metric>_count series may still be storing fine.")

    ok = (
        counts.get("gen_ai.client.token.usage", 0) > 0
        and counts.get("gen_ai.client.operation.duration", 0) > 0
        and {"input", "output"} <= ttypes
        and any(m != "unknown_model" for m in models)
        # named agent reaches the "AI agents" view (the core fix for demobot-v3);
        # OpenClaw's GenAI metrics don't carry it, so --no-agent relaxes this.
        and (len(agents) > 0 or not require_agent)
        and fresh_ok
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
