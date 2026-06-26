#!/usr/bin/env python3
"""Galileo A/B experiment: clean vs model-POISONED Dolphin on DemoBot.

Runs one curated golden set of *benign* patient prompts through the live
DemoBot pipeline twice — once on the clean ``dolphin3:8b`` and once on the
tampered ``dolphin3-medadvice-poisoned`` artifact (build it first with
scripts/demo/build_poisoned_dolphin.sh) — and scores both as first-class Galileo
experiments with a shared metric set (built-in scorers + 3 custom LLM-as-judge
medical-safety metrics + 2 deterministic local scorers; see galileo_metrics.py).

The only variable between the two arms is the MODEL ARTIFACT: governance toggles
are OFF and the prompts are identical and benign, so the input-side guardrails see
nothing — yet the poisoned arm trips the safety metrics. That delta, side by side
in Galileo's experiment comparison, is the insight.

The arm is selected at runtime via PUT /api/settings/ai-provider (no restart).
Responses are returned VERBATIM — no sanitizing — so Galileo scores the genuine
model output.

NO real patient data is used; the poison content is synthetic and fictional.

The golden set is a per-theme, maintained repo file (scripts/demo/datasets/
{theme}_safety_golden_n{4,32}.jsonl). The runner registers it in Galileo under the
clean name {theme}_safety_golden_n{N}: it reuses that named dataset as-is if it
already exists, else loads it from the repo file. Build/capture the files (incl. the
real clean-model "generated_output") with scripts/demo/build_golden_datasets.py.

Usage (app must be running via ./run.sh, Ollama up, baseline model installed):
    venv/bin/python scripts/demo/galileo_experiment_poisoning.py                 # medadvice, full 32
    venv/bin/python scripts/demo/galileo_experiment_poisoning.py -n 4            # medadvice, quick 4
    venv/bin/python scripts/demo/galileo_experiment_poisoning.py --theme taxadvice -n 4 --arm baseline
    venv/bin/python scripts/demo/galileo_experiment_poisoning.py --arm poisoned  # medadvice only (poisoned artifact)

Environment:
    GALILEO_API_KEY / GALILEO_PROJECT   required to run (no-op without the key)
    GALILEO_CONSOLE_URL                  optional, for the printed console link
    MEDADVICE_BASE_URL                   default http://localhost:8001
    ACCESS_KEY                           HTTP Basic password gate (read from .env)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for sibling galileo_metrics

# Load .env (ACCESS_KEY, GALILEO_*) the same thin way as the peer seeders.
_ENV = ROOT / ".env"
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DATASETS_DIR = ROOT / "scripts/demo/datasets"
DEFAULT_THEME = "medadvice"
PROMPT_COUNTS = (4, 32)  # quick A/B vs full A/B
BASELINE_MODEL = "dolphin3:8b"
POISONED_MODEL = "dolphin3-medadvice-poisoned"  # medadvice-only artifact today


def _auth() -> Optional[tuple]:
    key = os.environ.get("ACCESS_KEY", "").strip()
    return ("x", key) if key else None


def _dataset_file(theme: str, n: int) -> Path:
    return DATASETS_DIR / f"{theme}_safety_golden_n{n}.jsonl"


def _dataset_name(theme: str, n: int) -> str:
    """Clean, predictable name registered in Galileo (no content hash). Every A/B
    for the same theme + prompt count maps to the SAME named dataset, so the runner
    can 'check Galileo, reuse as-is if present, else load it from the repo file'."""
    return f"{theme}_safety_golden_n{n}"


def _load_rows(path: Path, theme: str) -> List[Dict[str, Any]]:
    """Load a golden file into Galileo dataset rows. Maps the file's fields to the
    DatasetRecord columns: input -> Input, output -> Ground Truth, generated_output
    -> Generated Output, and {mode, theme} -> Metadata (string values only)."""
    rows: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        row: Dict[str, Any] = {"input": d["input"], "output": d.get("output", "")}
        if d.get("generated_output"):
            row["generated_output"] = d["generated_output"]
        meta = {"theme": theme}
        if d.get("mode"):
            meta["mode"] = d["mode"]
        row["metadata"] = meta
        rows.append(row)
    return rows


def _ensure_dataset(rows: List[Dict[str, Any]], name: str, refresh: bool = False):
    """Get-or-create a REGISTERED Galileo dataset under the clean name (never an
    inline list), so every experiment shows the dataset + ground-truth linkage. The
    default contract is reuse-as-is when the name already exists; --refresh-dataset
    deletes + recreates it to push edited repo rows."""
    from galileo.datasets import create_dataset, get_dataset

    ds = get_dataset(name=name)
    if ds is not None and refresh:
        from galileo.datasets import delete_dataset
        print(f"  refresh: deleting existing dataset '{name}' to re-load edited rows")
        delete_dataset(name=name)
        ds = None
    if ds is None:
        print(f"  creating Galileo dataset '{name}' ({len(rows)} rows)")
        ds = create_dataset(name=name, content=rows)
    else:
        print(f"  reusing existing Galileo dataset '{name}' ({len(rows)} rows)")
    return ds


def _set_arm_model(client: httpx.Client, base: str, auth, model: str) -> bool:
    """Switch the live chat model (no restart). Returns True on success."""
    r = client.put(
        f"{base}/api/settings/ai-provider", auth=auth,
        json={"provider": "ollama", "model": model},
    )
    if r.status_code != 200:
        print(f"  ! could not set model {model}: {r.status_code} {r.text[:160]}")
        return False
    cur = r.json()
    print(f"  active model -> {cur.get('provider')}/{cur.get('model')}")
    return True


def _make_runner(client: httpx.Client, base: str, auth, model: str, theme: str):
    """Build the experiment function: one benign prompt -> verbatim response.

    Routes the prompt through the selected theme's pipeline (so a taxadvice A/B
    actually exercises taxadvice), adds an explicit Galileo LLM span
    (input/output/model) for the LLM-level scorers + custom judges, then returns
    the raw text.
    """
    def runner(row_input: Any) -> str:
        prompt = row_input if isinstance(row_input, str) else (
            row_input.get("input") if isinstance(row_input, dict) else str(row_input))
        # New session per prompt (benign; governance toggles intentionally OFF).
        sid = None
        rs = client.post(f"{base}/api/chat/session/new", auth=auth)
        if rs.status_code == 200:
            sid = rs.json().get("session_id")
        body = {"session_id": sid, "message": prompt, "theme": theme,
                "disclaimer_accepted": True}
        r = client.post(f"{base}/api/chat/message", auth=auth, json=body)
        text = r.json().get("message", "") if r.status_code == 200 else \
            f"(error {r.status_code}: {r.text[:160]})"
        try:  # give llm-level scorers a span to grade; never fail the row on this
            from galileo import galileo_context
            log = galileo_context.get_logger_instance()
            log.add_llm_span(input=prompt, output=text, model=model)
        except Exception:  # noqa: BLE001
            pass
        return text
    return runner


def _ollama_models(client, base, auth) -> set:
    """Currently-installed Ollama models per the app's model catalog."""
    try:
        r = client.get(f"{base}/api/settings/ai-provider", auth=auth)
        if r.status_code == 200:
            return set(r.json().get("available", {}).get("ollama", []) or [])
    except Exception:  # noqa: BLE001
        pass
    return set()


def _run_arm(client, base, auth, arm: str, model: str, project: str, dataset, metrics,
             experiment_name: str, theme: str) -> None:
    from galileo.experiments import run_experiment

    print(f"\n=== {arm.upper()} arm  (model={model}) -> {experiment_name} ===")
    if not _set_arm_model(client, base, auth, model):
        print(f"  skipping {arm} arm (model switch failed)")
        return
    result = run_experiment(
        experiment_name=experiment_name,
        project=project,
        dataset=dataset,
        metrics=metrics,
        function=_make_runner(client, base, auth, model, theme),
        experiment_tags={"arm": arm, "model": model, "eval": "model-poisoning",
                         "theme": theme},
    )
    link = getattr(result, "link", None) or getattr(result, "url", None)
    print(f"  experiment submitted: {experiment_name}" + (f"  ->  {link}" if link else ""))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=os.environ.get("MEDADVICE_BASE_URL",
                                                     "http://localhost:8001"))
    p.add_argument("--arm", choices=["both", "baseline", "poisoned"], default="both")
    p.add_argument("--project", default=os.environ.get("GALILEO_PROJECT", ""))
    p.add_argument("--baseline-model", default=BASELINE_MODEL)
    p.add_argument("--poisoned-model", default=POISONED_MODEL)
    p.add_argument("-n", "--prompts", type=int, choices=PROMPT_COUNTS, default=32,
                   help="prompt count = which curated golden file to load: 4 (quick A/B, "
                        "~2-3 min) or 32 (full A/B, ~16-20 min). Loads "
                        "{theme}_safety_golden_n{N}.jsonl.")
    p.add_argument("--theme", default=DEFAULT_THEME,
                   help="application theme: selects BOTH the golden dataset file and the "
                        "experiment-name prefix ({theme}-{arm}-{timestamp}).")
    p.add_argument("--refresh-dataset", action="store_true",
                   help="delete + recreate the named Galileo dataset to push edited repo rows "
                        "(default reuses an existing same-named dataset as-is).")
    p.add_argument("--with-llm-judges", action="store_true",
                   help="also score the GPT-based built-ins + custom LLM judges (needs an "
                        "LLM integration configured in the Galileo project, else they error)")
    p.add_argument("--judge-model", default="gpt-4.1-mini",
                   help="judge/execution model for the custom LLM metrics (must be exposed "
                        "by the project's LLM integration)")
    p.add_argument("--force-recreate-judges", action="store_true",
                   help="DESTRUCTIVE: delete + re-create the custom judges so they bind to a new "
                        "--judge-model. This WIPES the console thresholds — re-apply green=False/"
                        "red=True afterward. Default (no flag) is create-if-missing only.")
    args = p.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = args.base.rstrip("/")
    auth = _auth()

    # Defensive contract (mirrors backend/galileo_integration.py): no key -> no-op.
    if not os.environ.get("GALILEO_API_KEY"):
        print("GALILEO_API_KEY not set — nothing to evaluate. This is a no-op.\n"
              "Set GALILEO_API_KEY / GALILEO_PROJECT and re-run to launch the experiments.")
        return 0
    if not args.project:
        print("FATAL: GALILEO_PROJECT not set (and --project not given).")
        return 2

    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        try:
            h = client.get(f"{base}/health", auth=auth)
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: cannot reach {base} ({exc}). Start the app with ./run.sh")
            return 2
        if h.status_code != 200:
            print(f"FATAL: {base}/health -> {h.status_code}. Is the app running?")
            return 2
        print(f"DemoBot reachable at {base} (auth={'yes' if auth else 'none'})")

        # Make sure the freshly-built poisoned model is in the catalog dropdown.
        client.post(f"{base}/api/settings/ai-provider/refresh", auth=auth)

        from galileo_metrics import register_llm_judges, resolve_metric_set, fix_judge_color_config
        if args.with_llm_judges:
            if args.force_recreate_judges:
                from galileo_metrics import delete_judges
                print("FORCE: deleting + re-creating judges to bind the new judge model ...")
                delete_judges()
            print(f"Ensuring custom LLM-as-judge metrics (create-if-missing, model={args.judge_model}) ...")
            register_llm_judges(args.judge_model)
            # Assert the Numeric violation color config in-place so the judge columns
            # ROLL UP to an AVG % in the experiments list (a Boolean config blanks it),
            # while keeping violation=red / minimize ranking.
            print("Asserting judge color config (Numeric: green=low / red=high, AVG rolls up) ...")
            fix_judge_color_config()
        else:
            print("Scoring the no-key tier (SLM + code scorers). The custom LLM judges and "
                  "GPT built-ins need an LLM integration in the Galileo project — once that's "
                  "set, re-run with --with-llm-judges.")

        print("Resolving metric set against tenant scorers ...")
        metrics = resolve_metric_set(include_llm_scorers=args.with_llm_judges)

        ds_file = _dataset_file(args.theme, args.prompts)
        if not ds_file.exists():
            print(f"FATAL: golden dataset file not found: {ds_file}\n"
                  f"  Create it (and capture generated_output) with\n"
                  f"  scripts/demo/build_golden_datasets.py --theme {args.theme}")
            return 2
        name = _dataset_name(args.theme, args.prompts)
        print(f"Ensuring golden dataset '{name}' ({args.prompts} prompts) from {ds_file.name} ...")
        rows = _load_rows(ds_file, args.theme)
        # ALWAYS a registered dataset (never an inline list) so the console shows
        # the dataset + ground-truth linkage for every run. Check Galileo, reuse the
        # same-named dataset as-is if present, else load it from the repo file.
        dataset = _ensure_dataset(rows, name, refresh=args.refresh_dataset)

        if args.arm in ("both", "baseline"):
            _run_arm(client, base, auth, "baseline", args.baseline_model, args.project,
                     dataset, metrics, f"{args.theme}-baseline-{stamp}", args.theme)
        if args.arm in ("both", "poisoned"):
            if args.poisoned_model in _ollama_models(client, base, auth):
                _run_arm(client, base, auth, "poisoned", args.poisoned_model, args.project,
                         dataset, metrics, f"{args.theme}-poisoned-{stamp}", args.theme)
            else:
                print(f"\nNOTE: poisoned model '{args.poisoned_model}' is not installed in Ollama "
                      f"— skipping the poisoned arm. The clean-vs-poisoned A/B needs a poisoned "
                      f"artifact; only '{DEFAULT_THEME}' ships one today (build it with "
                      f"scripts/demo/build_poisoned_dolphin.sh). The baseline arm + dataset still "
                      f"register for theme '{args.theme}'.")

        # Leave the app on the clean model so a stray later turn isn't poisoned.
        _set_arm_model(client, base, auth, args.baseline_model)

    console = os.environ.get("GALILEO_CONSOLE_URL", "https://app.galileo.ai")
    print(f"\nDone. Open Galileo ({console}) -> project '{args.project}' -> Experiments, and")
    print(f"compare '{args.theme}-baseline-*' vs '{args.theme}-poisoned-*'.")
    print("On a poisoned arm, expect prescriptive_overreach / medical_misinformation /")
    print("commercial_brand_capture (and rx_dosage_hit / fictional_brand_hit) to rise, while")
    print("input-side prompt_injection stays clean on BOTH arms — the prompts were benign.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
