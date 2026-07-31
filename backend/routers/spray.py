"""Prompt-injection spray campaign control endpoints.

Drives a sustained, varied prompt-injection campaign through DemoBot's existing
live Cisco AI Defense path so the governance events are dense and varied enough
to fire the ES ``AI Governance - Prompt Injection Attack Correlation - Rule``,
accrue RBA risk against one actor, and give the ES Triage agent real evidence.

**Verdicts are never fabricated.** Every turn goes through the same graph as an
ordinary chat turn with ``ai_defense_review=True``; whatever AI Defense returns
is what the governance event records, and ``risk_score`` / ``policy_action`` are
derived downstream as usual. The campaign controls only volume, cadence, and
variety.

Unlike the incident load driver, this calls ``run_turn`` **in process** rather
than looping back through the HTTP API. ``enduser_id`` is assigned server-side
from a random pool and ``client_address`` comes from the socket, so neither can
be pinned over HTTP — and pinning the actor is the whole point (the correlation
rule aggregates by actor). Turns run one at a time: it keeps the multi-turn
escalation honest, avoids racing on conversation history, and 15 turns spread
over 10 minutes needs no concurrency.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.agents.graph import run_turn
from backend.services import spray_corpus
from backend.services.enduser_pool import ENDUSER_IDS
from backend.spray_campaign import CLIENT_ADDRESSES, TARGET_APPS, spray_campaign

router = APIRouter(prefix="/api/spray", tags=["spray"])
logger = logging.getLogger(__name__)

_driver_task: Optional[asyncio.Task] = None
_expiry_task: Optional[asyncio.Task] = None

# Pacing bounds. The spec suggests a 20-90s band, but that overshoots the
# default 600s window at 15 turns, so the mean is derived from the controls
# (duration / turns) and jittered instead. Clamped so a very short or very long
# campaign still paces sanely.
_MIN_GAP_S = 5.0
_MAX_GAP_S = 120.0
_JITTER = 0.6  # +/- 60% around the mean, so pacing reads as bursty, not scripted


class SprayStart(BaseModel):
    actor: str = Field(default="t.nguyen", min_length=1, max_length=64)
    duration_s: int = Field(default=600, ge=10, le=3600)
    intensity: int = Field(default=15, ge=1, le=200)
    secondary_actors: int = Field(default=2, ge=0, le=10)
    # Lets the regression suite exercise start/stop/status without generating
    # real AI Defense traffic (mirrors the incident router's drive_traffic).
    drive_turns: bool = True


class _Turn:
    """One planned turn: who sends it, what they send, and where it lands."""

    def __init__(self, *, actor: str, session_id: str, prompt: str,
                 technique: str, app: Dict[str, str]) -> None:
        self.actor = actor
        self.session_id = session_id
        self.prompt = prompt
        self.technique = technique
        self.app = app


def _build_plan(body: SprayStart, rng: random.Random) -> List[_Turn]:
    """Lay out the whole campaign up front.

    Guarantees the spec's shape at the default settings: >=5 technique families,
    >=3 distinct sessions, >=2 target apps, and ~15% subtle prompts intended to
    be allowed. Everything is planned deterministically here so the driver just
    walks the list.
    """
    plan: List[_Turn] = []
    primary = body.actor
    families = spray_corpus.single_shot_families()

    # Sessions for the primary actor: a handful, so `distinct_sessions` shows
    # persistence across sessions rather than one long thread.
    session_pool = [f"spray-{rng.getrandbits(48):012x}" for _ in range(3)]

    def app_for(i: int) -> Dict[str, str]:
        return TARGET_APPS[i % len(TARGET_APPS)]

    budget = body.intensity

    # 1. Multi-turn escalation, in its own conversation, when there is room for
    #    it. This is the sequence an analyst scrolls through.
    escalation: List[str] = []
    if budget >= 8:
        escalation = list(spray_corpus.pick_escalation(rng))
        esc_session = f"spray-{rng.getrandbits(48):012x}"
        esc_app = app_for(rng.randrange(len(TARGET_APPS)))
        for prompt in escalation:
            plan.append(_Turn(actor=primary, session_id=esc_session, prompt=prompt,
                              technique=spray_corpus.MULTI_TURN_FAMILY, app=esc_app))
        budget -= len(escalation)

    # 2. The ~15% that should get through (spec §5) — at least one whenever the
    #    campaign is big enough to have any texture at all.
    n_subtle = max(1, round(body.intensity * 0.15)) if budget > len(families) else 0
    n_subtle = min(n_subtle, max(0, budget - 1))
    subtle_turns: List[_Turn] = []
    for i in range(n_subtle):
        _, prompt = spray_corpus.pick_subtle(rng)
        subtle_turns.append(_Turn(
            actor=primary, session_id=rng.choice(session_pool), prompt=prompt,
            technique="subtle_probe", app=app_for(i)))
    budget -= n_subtle

    # 3. Fill the remainder with blocked-cohort prompts, cycling the families so
    #    every one is represented before any repeats.
    blocked_turns: List[_Turn] = []
    for i in range(max(0, budget)):
        family = families[i % len(families)]
        _, prompt = spray_corpus.pick_blocked(family, rng)
        blocked_turns.append(_Turn(
            actor=primary, session_id=rng.choice(session_pool), prompt=prompt,
            technique=family, app=app_for(i)))

    # 4. Secondary actors — low volume, so the correlation rule has something to
    #    rank the primary against.
    secondary_turns: List[_Turn] = []
    candidates = [u for u in ENDUSER_IDS if u != primary]
    for actor in rng.sample(candidates, min(body.secondary_actors, len(candidates))):
        for i in range(rng.randint(1, 2)):
            family = rng.choice(families)
            _, prompt = spray_corpus.pick_blocked(family, rng)
            secondary_turns.append(_Turn(
                actor=actor, session_id=f"spray-{rng.getrandbits(48):012x}",
                prompt=prompt, technique=family, app=app_for(i)))

    # Shuffle everything except the escalation, then weave the escalation back in
    # at random positions with its order preserved — it has to read benign-first.
    loose = subtle_turns + blocked_turns + secondary_turns
    rng.shuffle(loose)
    if plan:
        esc_turns, plan = plan, []
        total = len(loose) + len(esc_turns)
        slots = set(rng.sample(range(total), len(esc_turns)))
        esc_iter, loose_iter = iter(esc_turns), iter(loose)
        for idx in range(total):
            plan.append(next(esc_iter) if idx in slots else next(loose_iter))
    else:
        plan = loose
    return plan


def _gap_seconds(duration_s: int, turns: int, rng: random.Random) -> float:
    """An irregular inter-turn delay that still fits the requested window."""
    mean = duration_s / max(1, turns)
    low, high = mean * (1 - _JITTER), mean * (1 + _JITTER)
    return max(_MIN_GAP_S, min(_MAX_GAP_S, rng.uniform(low, high)))


async def _drive_campaign(body: SprayStart, rng: random.Random) -> None:
    """Walk the plan, one real turn at a time, until it's done or stopped."""
    plan = _build_plan(body, rng)
    logger.warning("spray campaign driver started: %s planned turn(s)", len(plan))
    # Accumulated per session so a multi-turn conversation actually reads as one.
    histories: Dict[str, List[Dict[str, Any]]] = {}

    try:
        for turn in plan:
            if not spray_campaign.is_active():
                break
            history = histories.setdefault(turn.session_id, [])
            try:
                result = await run_in_threadpool(
                    run_turn,
                    session_id=turn.session_id,
                    user_message=turn.prompt,
                    conversation_history=list(history),
                    client_address=rng.choice(CLIENT_ADDRESSES),
                    theme=turn.app["theme"],
                    service_name=turn.app["service_name"],
                    deployment_id=turn.app["deployment_id"],
                    enduser_id=turn.actor,
                    ai_defense_review=True,
                    internal_policy_review=True,
                )
                blocked = bool(result.get("policy_blocked"))
                history.append({"role": "user", "content": turn.prompt})
                history.append({"role": "assistant",
                                "content": result.get("message", "")})
            except Exception:  # noqa: BLE001 - one bad turn must not end the campaign
                logger.exception("spray turn failed (technique=%s)", turn.technique)
                blocked = None

            spray_campaign.record_turn(
                session_id=turn.session_id, technique=turn.technique,
                app_name=turn.app["service_name"], blocked=blocked,
            )
            await asyncio.sleep(_gap_seconds(body.duration_s, len(plan), rng))
    except asyncio.CancelledError:
        pass
    logger.warning("spray campaign driver stopped: %s", spray_campaign.status())


async def _do_stop() -> None:
    global _driver_task
    spray_campaign.stop()
    if _driver_task and not _driver_task.done():
        _driver_task.cancel()
    _driver_task = None


async def _auto_stop_after(duration_s: int) -> None:
    try:
        await asyncio.sleep(duration_s)
    except asyncio.CancelledError:
        return
    await _do_stop()
    logger.warning("spray_campaign auto-stopped after %ss", duration_s)


def _status() -> dict:
    st = spray_campaign.status()
    st["driver_running"] = bool(_driver_task and not _driver_task.done())
    return st


@router.post("/start")
async def start_spray(body: SprayStart):
    global _driver_task, _expiry_task
    spray_campaign.start(
        actor=body.actor, duration_s=body.duration_s, intensity=body.intensity,
        secondary_actors=body.secondary_actors, drive_turns=body.drive_turns,
    )
    if _driver_task and not _driver_task.done():
        _driver_task.cancel()
    _driver_task = None
    if body.drive_turns:
        _driver_task = asyncio.create_task(_drive_campaign(body, random.Random()))
    if _expiry_task and not _expiry_task.done():
        _expiry_task.cancel()
    _expiry_task = asyncio.create_task(_auto_stop_after(body.duration_s))
    return _status()


@router.post("/stop")
async def stop_spray():
    global _expiry_task
    if _expiry_task and not _expiry_task.done():
        _expiry_task.cancel()
    await _do_stop()
    return _status()


@router.get("/status")
async def spray_status():
    return _status()
