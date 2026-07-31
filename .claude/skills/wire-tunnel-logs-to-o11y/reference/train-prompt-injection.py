#!/usr/bin/env python3
"""Train TA-gen_ai_cim's prompt-injection models on a Splunk instance.

package.sh excludes lookups/__mlspl_*.mlmodel, so a fresh install has no
models. The scoring search's `apply app:prompt_injection_tfidf_*` then emits
nothing, the ai_cim:prompt_injection:ml_scoring sourcetype never exists, and
every detection rule silently matches zero -- while the scheduler reports
success on every run.

The training corpus ships with the app, so this needs no API key. Steps 2 and 3
need run_risky_commands (MLTK fit), granted via local/authorize.conf.

  usage: train-prompt-injection.py <host> <admin_user> <admin_pass>
"""
import base64
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request

STEPS = [
    "GenAI - Prompt Injection Train Step 1 - Feature Engineering from Initial Dataset",
    "GenAI - Prompt Injection Train Step 2 - TF-IDF PCA Model",
    "GenAI - Prompt Injection Train Step 3 - TF-IDF Classifier Model",
    "GenAI - Prompt Injection Train Step 4 - Validate Model Performance",
]


def main(host, user, pw):
    ctx = ssl._create_unverified_context()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    hdr = {"Authorization": f"Basic {auth}"}

    def call(path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"https://{host}:8089{path}", data=body, headers=hdr)
        return json.loads(urllib.request.urlopen(req, context=ctx, timeout=120).read().decode())

    ok = True
    for name in STEPS:
        q = urllib.parse.quote(name, safe="")
        try:
            sid = call(
                f"/servicesNS/nobody/TA-gen_ai_cim/saved/searches/{q}/dispatch?output_mode=json",
                {"trigger_actions": "0"},
            )["sid"]
        except Exception as exc:
            print(f"  DISPATCH FAILED  {name[:52]}: {exc}")
            ok = False
            continue

        job = {}
        for _ in range(90):
            job = call(f"/services/search/jobs/{sid}?output_mode=json")["entry"][0]["content"]
            if job.get("dispatchState") in ("DONE", "FAILED"):
                break
            time.sleep(4)

        state = job.get("dispatchState")
        print(f"  {name[:52]:54} {state} results={job.get('resultCount')}")
        if state != "DONE":
            ok = False
        for m in job.get("messages") or []:
            if m.get("type") in ("ERROR", "FATAL"):
                print(f"       {m['type']}: {str(m.get('text'))[:200]}")
                ok = False

    models = [
        e["name"]
        for e in call(
            "/servicesNS/-/TA-gen_ai_cim/data/lookup-table-files?count=0&output_mode=json"
        ).get("entry", [])
        if "__mlspl_prompt_injection" in e["name"]
    ]
    print(f"\n  models present: {models or 'NONE'}")
    if len(models) < 2:
        print("  WARNING: expected __mlspl_prompt_injection_tfidf_pca + _tfidf_model")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
