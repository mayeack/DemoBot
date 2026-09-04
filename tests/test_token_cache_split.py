#!/usr/bin/env python3
"""Regression: output tokens are reported split by cache origin.

Every governed turn reports ``usage_output_tokens_cached`` +
``usage_output_tokens_uncached`` next to ``usage_output_tokens``. The invariant
that makes the pair safe to add to an existing schema is that the two ALWAYS sum
back to the total, so no consumer of ``usage_output_tokens`` changes meaning.

What this pins (offline, no provider / no network):
  - split_output_tokens: provider-reported cached counts win; a local provider
    (ollama / a local NIM) gets the random tag; a cloud provider that reports
    nothing is all-uncached; the halves always sum to the total.
  - _extract_usage carries the split off a LangChain AIMessage, from both the
    usage_metadata and the OpenAI-compatible response_metadata shapes.
  - TokenTally accumulates the split across agents alongside the totals.
  - governance_usage_data emits the pair, summing to usage_output_tokens, both
    for a completed turn and for a prompt blocked before any model call.
  - the log schema and the DB model carry the fields, so the Splunk event and
    the local governance DB both keep them.

Run:  venv/bin/python tests/test_token_cache_split.py    # exit 0 = pass
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from backend.agents.llm import NormalizedLLMResponse, _extract_usage  # noqa: E402
from backend.agents.token_usage import (  # noqa: E402
    LOCAL_PROVIDERS,
    TokenTally,
    governance_usage_data,
    provider_cached_output_tokens,
    split_output_tokens,
)

_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails += 1


def test_split_sums_to_the_total() -> None:
    """The invariant every consumer relies on, over many random local draws."""
    for provider in ("ollama", "nvidia", "anthropic", "openai", "bedrock", None):
        bad = []
        for total in (0, 1, 7, 250, 4096):
            for _ in range(50):
                cached, uncached = split_output_tokens(total, provider=provider)
                if cached + uncached != total or cached < 0 or uncached < 0:
                    bad.append((total, cached, uncached))
        check(f"[{provider}] cached + uncached == output_tokens, both >= 0", not bad, str(bad[:3]))


def test_cloud_provider_defaults_to_uncached() -> None:
    cached, uncached = split_output_tokens(400, provider="anthropic")
    check("a cloud provider reporting nothing is all non-cached",
          (cached, uncached) == (0, 400), str((cached, uncached)))


def test_local_provider_is_tagged() -> None:
    """Local inference reports no split, so the demo tags it — randomly, but
    within bounds, and not pinned to one value across calls."""
    check("ollama + nvidia are the local providers", LOCAL_PROVIDERS == {"ollama", "nvidia"})
    draws = {split_output_tokens(1000, provider="ollama")[0] for _ in range(60)}
    check("the local cached share varies across calls", len(draws) > 1, str(draws))
    check("the local cached share never exceeds the total", max(draws) <= 1000, str(max(draws)))
    check("a zero-output local call splits to (0, 0)",
          split_output_tokens(0, provider="ollama") == (0, 0))


def test_provider_reported_split_wins() -> None:
    """Real data beats the tag — even for a local provider."""
    msg = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 10, "output_tokens": 100, "total_tokens": 110,
                        "output_token_details": {"cache_read": 40}},
    )
    check("usage_metadata output_token_details.cache_read is read",
          provider_cached_output_tokens(msg) == 40)
    check("a reported split overrides the local random tag",
          split_output_tokens(100, provider="ollama", ai_message=msg) == (40, 60))

    openai_shape = AIMessage(
        content="x",
        response_metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 100,
                                           "completion_tokens_details": {"cached_tokens": 25}}},
    )
    check("OpenAI-compatible completion_tokens_details.cached_tokens is read",
          provider_cached_output_tokens(openai_shape) == 25)
    check("a provider that reports no details returns None",
          provider_cached_output_tokens(AIMessage(content="x")) is None)

    over = AIMessage(content="x",
                     usage_metadata={"input_tokens": 1, "output_tokens": 5, "total_tokens": 6,
                                     "output_token_details": {"cache_read": 900}})
    check("a nonsense reported count is clamped to the total",
          split_output_tokens(5, provider="openai", ai_message=over) == (5, 0))


def test_extract_usage_carries_the_split() -> None:
    msg = AIMessage(content="x", usage_metadata={"input_tokens": 11, "output_tokens": 7,
                                                 "total_tokens": 18})
    usage = _extract_usage(msg, "ollama")
    check("_extract_usage keeps the existing keys",
          usage["input_tokens"] == 11 and usage["output_tokens"] == 7)
    check("_extract_usage adds the cache split",
          usage["output_tokens_cached"] + usage["output_tokens_uncached"] == 7,
          str(usage))

    fallback_shape = AIMessage(
        content="x",
        response_metadata={"token_usage": {"prompt_tokens": 3, "completion_tokens": 9}},
    )
    usage = _extract_usage(fallback_shape, "anthropic")
    check("the response_metadata fallback path splits too",
          usage["output_tokens"] == 9 and usage["output_tokens_uncached"] == 9
          and usage["output_tokens_cached"] == 0, str(usage))


def _response(out: int, cached: int) -> NormalizedLLMResponse:
    return NormalizedLLMResponse(
        id="r", content="c", model="m", input_tokens=10, output_tokens=out,
        output_tokens_cached=cached, output_tokens_uncached=out - cached,
        stop_reason="end_turn",
    )


def test_token_tally_accumulates() -> None:
    check("NormalizedLLMResponse defaults the split to zero",
          NormalizedLLMResponse(id="r", content="", model="m", input_tokens=0,
                                output_tokens=0, stop_reason="end_turn").output_tokens_cached == 0)

    fresh = TokenTally().add(_response(100, 40)).updates()
    check("a first node reports its own call",
          fresh == {"llm_input_tokens": 10, "llm_output_tokens": 100,
                    "llm_output_tokens_cached": 40, "llm_output_tokens_uncached": 60},
          str(fresh))

    running = TokenTally(fresh).add(_response(50, 10)).add(_response(20, 0)).updates()
    check("a later node keeps the running sums",
          running == {"llm_input_tokens": 30, "llm_output_tokens": 170,
                      "llm_output_tokens_cached": 50, "llm_output_tokens_uncached": 120},
          str(running))
    check("the summed halves still equal the summed output total",
          running["llm_output_tokens_cached"] + running["llm_output_tokens_uncached"]
          == running["llm_output_tokens"])
    check("a failed agent (response=None) is skipped",
          TokenTally(running).add(None).updates() == running)


def test_governance_usage_data() -> None:
    state = {"llm_input_tokens": 30, "llm_output_tokens": 170,
             "llm_output_tokens_cached": 50, "llm_output_tokens_uncached": 120}
    usage = governance_usage_data(state)
    check("the governance block carries the split",
          usage["usage_output_tokens_cached"] == 50
          and usage["usage_output_tokens_uncached"] == 120, str(usage))
    check("the split sums to usage_output_tokens",
          usage["usage_output_tokens_cached"] + usage["usage_output_tokens_uncached"]
          == usage["usage_output_tokens"])
    check("usage_total_tokens is unchanged (input + output)",
          usage["usage_total_tokens"] == 200)
    blocked = governance_usage_data()
    check("a prompt blocked before any model call reports the all-zero block",
          set(blocked.values()) == {0} and len(blocked) == 5, str(blocked))


def test_fields_reach_the_log_and_the_db() -> None:
    from backend.logging.log_schemas import create_governance_log
    from backend.models.db_models import AIGovernanceLog

    entry = create_governance_log(
        operation_name="chat", request_model="m", conversation_id="c", session_id="s",
        input_messages=[], **governance_usage_data(
            {"llm_input_tokens": 5, "llm_output_tokens": 20,
             "llm_output_tokens_cached": 8, "llm_output_tokens_uncached": 12}),
    )
    check("the governance event carries usage_output_tokens_cached",
          entry.get("usage_output_tokens_cached") == 8, str(entry.get("usage_output_tokens_cached")))
    check("the governance event carries usage_output_tokens_uncached",
          entry.get("usage_output_tokens_uncached") == 12)

    columns = {c.name for c in AIGovernanceLog.__table__.columns}
    check("the DB model has both columns (reconcile_schema adds them at startup)",
          {"usage_output_tokens_cached", "usage_output_tokens_uncached"} <= columns)


def test_metrics_aggregate_handles_pre_split_rows() -> None:
    """A governance DB predating the split has rows with usage_output_tokens and
    a NULL split; SUM() skips NULLs, so the aggregate must derive the uncached
    side from the total or the dashboard under-reports output badly."""
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    from backend.models.db_models import AIGovernanceLog, Base

    engine = create_engine("sqlite://")  # in-memory, isolated from the app DB
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    required = dict(operation_name="chat", request_model="m", conversation_id="c",
                    request_id="r", trace_id="t", input_messages=[])
    db.add(AIGovernanceLog(  # pre-split row: no cached/uncached recorded
        session_id="old", usage_input_tokens=100, usage_output_tokens=200,
        usage_total_tokens=300, **required))
    db.add(AIGovernanceLog(  # post-split row
        session_id="new", usage_input_tokens=10, usage_output_tokens=50,
        usage_output_tokens_cached=20, usage_output_tokens_uncached=30,
        usage_total_tokens=60, **required))
    db.commit()

    stats = db.query(
        func.sum(AIGovernanceLog.usage_output_tokens).label("total_output"),
        func.sum(func.coalesce(
            AIGovernanceLog.usage_output_tokens_cached, 0)).label("total_output_cached"),
    ).first()
    total_output = stats.total_output or 0
    cached = stats.total_output_cached or 0
    uncached = max(0, total_output - cached)
    db.close()

    check("the aggregate counts every row's output", total_output == 250, str(total_output))
    check("only the recorded cached tokens are counted as cached", cached == 20, str(cached))
    check("a pre-split row's output falls to the non-cached side",
          uncached == 230, str(uncached))
    check("the aggregate invariant holds across mixed rows",
          cached + uncached == total_output)


def main() -> int:
    for fn in (
        test_split_sums_to_the_total,
        test_cloud_provider_defaults_to_uncached,
        test_local_provider_is_tagged,
        test_provider_reported_split_wins,
        test_extract_usage_carries_the_split,
        test_token_tally_accumulates,
        test_governance_usage_data,
        test_fields_reach_the_log_and_the_db,
        test_metrics_aggregate_handles_pre_split_rows,
    ):
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global _fails
            _fails += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\nRESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
