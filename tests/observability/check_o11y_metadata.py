#!/usr/bin/env python3
"""Assert that GenAI metrics + metadata are present in Splunk Observability Cloud.

Queries the SignalFx metric-timeseries metadata API for the service and checks:
  - gen_ai.client.token.usage exists, with gen_ai.token.type in {input, output}
    and at least one real (non-"unknown_model") gen_ai.request.model dimension
  - at least one gen_ai.agent.name is present, so the named agent reaches AI Agent
    Monitoring's "AI agents" view (this regressed when only raw spans were emitted
    instead of a util-genai AgentInvocation — see backend/telemetry/otel.py)
  - gen_ai.client.operation.duration exists

Retries for a few minutes to absorb ingest/processing lag.

Usage:
    check_o11y_metadata.py <realm> <api_token> <service_name> [environment]

Exit 0 = all present, non-zero = missing/incomplete.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

RETRY_SECONDS = 180
POLL_EVERY = 15


def mts(realm: str, token: str, query: str) -> dict:
    url = f"https://api.{realm}.signalfx.com/v2/metrictimeseries?" + urllib.parse.urlencode(
        {"query": query, "limit": "200"}
    )
    req = urllib.request.Request(url, headers={"X-SF-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: check_o11y_metadata.py <realm> <api_token> <service> [env]")
        return 2
    realm, token, service = sys.argv[1], sys.argv[2], sys.argv[3]
    env = sys.argv[4] if len(sys.argv) > 4 else None
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
    print(f"  gen_ai.agent.name(s): {sorted(agents) or '(none -> AI agents view is empty!)'}")
    print(f"  gen_ai.client.operation.duration MTS: {counts.get('gen_ai.client.operation.duration', 0)}")

    ok = (
        counts.get("gen_ai.client.token.usage", 0) > 0
        and counts.get("gen_ai.client.operation.duration", 0) > 0
        and {"input", "output"} <= ttypes
        and any(m != "unknown_model" for m in models)
        and len(agents) > 0  # named agent reaches the "AI agents" view (the core fix)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
