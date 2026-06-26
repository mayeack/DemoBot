#!/usr/bin/env python3
"""Create the RCA-eligible APM detectors for the demo (idempotent).

Per the Troubleshooting Agent training (RCA Eligible Alerts: Criteria), the AI
agent only runs on APM detectors built on metrics like service.request.duration.*
and service.request.count. This creates two such detectors on demobot-v3 /
demobot-local:
  - High request latency  (service.request.duration.ns.p90)
  - High error rate        (service.request.count + sf_error)

Reads SPLUNK_REALM + SPLUNK_API_TOKEN from .env (the API token, not the ingest
token). Re-running is safe — existing detectors (by name) are left alone.

Usage:  venv/bin/python scripts/observability/create_apm_detectors.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parents[2] / ".env"


def env(key: str):
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(key)


REALM = env("SPLUNK_REALM") or "us1"
TOKEN = env("SPLUNK_API_TOKEN")
if not TOKEN:
    print("ERROR: SPLUNK_API_TOKEN not set in .env (need an O11y API token)")
    sys.exit(1)
API = f"https://api.{REALM}.signalfx.com"
SVC, ENVN = "demobot-v3", "demobot-local"


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"X-SF-Token": TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on {method} {path}: {e.read().decode()[:400]}")
        raise


def existing_id(name: str):
    res = req("GET", "/v2/detector?" + urllib.parse.urlencode({"name": name, "limit": 50}))
    for d in res.get("results", []):
        if d.get("name") == name:
            return d.get("id")
    return None


DETECTORS = [
    {
        "name": "DemoBot — High request latency (demo)",
        "description": "p90 request latency on the demobot-v3 APM service is elevated. "
                       "RCA-eligible (service.request.duration.ns.p90).",
        "programText": (
            f"P90 = data('service.request.duration.ns.p90', filter('sf_service', '{SVC}') "
            f"and filter('sf_environment', '{ENVN}')).max(over='1m')\n"
            "detect(when(P90 > 12000000000, lasting='1m')).publish('High p90 latency')"
        ),
        "rules": [{"detectLabel": "High p90 latency", "severity": "Critical",
                   "description": "p90 request latency > 12s on demobot-v3", "notifications": []}],
    },
    {
        "name": "DemoBot — High error rate (demo)",
        "description": "Error rate on the demobot-v3 APM service is elevated. "
                       "RCA-eligible (service.request.count).",
        "programText": (
            # .sum() aggregates ACROSS MTS into one stream (so errors/total is a
            # single ratio); exclude sf_dimensionalized MTS so we don't double-count;
            # .sum(over='1m') is a rolling window so the ratio never divides by an
            # empty bucket at the native (10s) resolution (which would break lasting).
            f"total = data('service.request.count', filter('sf_service', '{SVC}') "
            f"and filter('sf_environment', '{ENVN}') "
            f"and (not filter('sf_dimensionalized', 'true'))).sum().sum(over='1m')\n"
            f"errors = data('service.request.count', filter('sf_service', '{SVC}') "
            f"and filter('sf_environment', '{ENVN}') "
            f"and (not filter('sf_dimensionalized', 'true')) "
            f"and filter('sf_error', 'true')).sum().sum(over='1m')\n"
            "rate = (errors / total) * 100\n"
            "detect(when(rate > 10, lasting='30s')).publish('High error rate')"
        ),
        "rules": [{"detectLabel": "High error rate", "severity": "Critical",
                   "description": "error rate > 10% on demobot-v3", "notifications": []}],
    },
]


def main() -> int:
    print(f"Realm={REALM} service={SVC} env={ENVN}")
    for d in DETECTORS:
        ex = existing_id(d["name"])
        if ex:
            print(f"  exists   {d['name']}  ({ex})")
            continue
        created = req("POST", "/v2/detector", {**d, "tags": ["medadvice", "demo", "rca"]})
        print(f"  created  {d['name']}  ({created.get('id')})")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
