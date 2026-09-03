"""Appointment scheduling service: deterministic slots, bookings and intent.

The chat's scheduling capability (docs/scheduling.md) is "deterministic
scheduling + LLM voice": everything structured — the suggested times, the
user's intent, the booking itself and the clickable options the UI shows —
comes from this module. The model (backend/agents/nodes/scheduling.py) only
writes the conversational wording, so nothing is ever parsed out of model text.

Verticalization: each Application Theme carries a :class:`SchedulingProfile`
(``ThemeConfig.scheduling``) — what is being booked, with whom, how long, which
days/hours, and the copy templates — so a MedAdvice follow-up visit and a
TelecomChatbot technician arrival window come from the same code.

Availability is synthetic and unlimited: the next slots after "now" inside the
profile's hours, paged forward for "More times". There is no calendar to
conflict with (this is a demo app), so two clients can book the same time.

Time handling: appointments are stored as naive UTC like every other column in
the app; labels are rendered in the client's IANA zone (``client_tz`` from the
browser, via the stdlib ``zoneinfo``), falling back to
``settings.scheduling_default_timezone``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.config import settings

logger = logging.getLogger(__name__)

SLOT_ID_FORMAT = "%Y%m%dT%H%MZ"
_MAX_DAYS_AHEAD = 90
_MAX_NAME_LEN = 64

STATUS_SCHEDULED = "scheduled"
STATUS_CANCELLED = "cancelled"

# Governance tool names for booking mutations (kept distinct from the synthetic
# PII pattern ``appointment_info`` in recommendation_engine.py).
TOOL_SCHEDULE = "schedule_appointment"
TOOL_RESCHEDULE = "reschedule_appointment"
TOOL_CANCEL = "cancel_appointment"


# --------------------------------------------------------------------------- profile
@dataclass(frozen=True)
class SchedulingProfile:
    """Per-theme scheduling vertical. Frozen + tuple fields so it can live on the
    frozen ``ThemeConfig``. Copy templates take ``{noun}``, ``{a_noun}``,
    ``{noun_plural}``, ``{provider}`` plus the call-site fields (``{label}``,
    ``{name}``)."""

    appointment_noun: str = "appointment"
    provider_noun: str = "a specialist"
    provider_label: str = "Consultation"
    slot_minutes: int = 30
    # > 0: the booking is an arrival WINDOW of this many minutes (telecom truck
    # roll) rather than an exact time; slots step by the window.
    window_minutes: int = 0
    business_days: Tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday = 0
    open_hour: int = 9
    close_hour: int = 17
    lead_minutes: int = 120
    slots_per_page: int = 3
    # Re-offer on a later answer turn only for these severities.
    offer_on_severities: Tuple[str, ...] = ("MEDIUM", "HIGH", "EMERGENCY")
    # Never offer on the FIRST answer when its severity is one of these.
    first_offer_excludes: Tuple[str, ...] = ()
    offer: str = ("Would you like to schedule {a_noun} with {provider}? "
                  "Here are the next available times:")
    choose: str = "Here are some available times for {a_noun}:"
    already_booked: str = ("You already have {a_noun} scheduled for {label}. "
                           "Ask me about your schedule if you'd like to change it.")
    ask_name: str = "Great — what name should I put the {noun} under?"
    confirmed: str = "You're booked: {label} — {noun} with {provider}, under {name}."
    listed: str = "Here's what you have scheduled:"
    none_scheduled: str = "You don't have any {noun_plural} scheduled."
    cancelled: str = "Done — your {noun} for {label} has been cancelled."
    rescheduled: str = "Done — your {noun} is now {label}."
    declined: str = "No problem. If you change your mind, just ask me to schedule {a_noun}."
    which_one: str = "Which {noun} do you mean?"
    unavailable: str = "Scheduling is temporarily unavailable — please try again in a moment."
    slot_unavailable: str = "That time is no longer available. Here are some others:"

    @property
    def duration_minutes(self) -> int:
        return self.window_minutes or self.slot_minutes

    @property
    def a_noun(self) -> str:
        article = "an" if self.appointment_noun[:1].lower() in "aeiou" else "a"
        return f"{article} {self.appointment_noun}"

    @property
    def noun_plural(self) -> str:
        n = self.appointment_noun
        return n + ("es" if n.endswith(("s", "x", "sh", "ch")) else "s")

    def render(self, key: str, **fields: Any) -> str:
        """Fill one copy template by name (``offer``, ``confirmed``, ...)."""
        template = getattr(self, key)
        values = {
            "noun": self.appointment_noun,
            "a_noun": self.a_noun,
            "noun_plural": self.noun_plural,
            "provider": self.provider_noun,
            "label": "",
            "name": "",
        }
        values.update({k: v for k, v in fields.items() if v is not None})
        return template.format(**values)


DEFAULT_PROFILE = SchedulingProfile()


# --------------------------------------------------------------------------- time
def resolve_tz(tz_name: Optional[str]) -> ZoneInfo:
    """The client's IANA zone, else the configured default, else UTC."""
    for candidate in (tz_name, getattr(settings, "scheduling_default_timezone", None), "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            continue
    return ZoneInfo("UTC")


def is_valid_tz(tz_name: Optional[str]) -> bool:
    if not tz_name:
        return False
    try:
        ZoneInfo(str(tz_name))
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def _to_local(naive_utc: datetime, zone: ZoneInfo) -> datetime:
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(zone)


def _to_utc_naive(local_aware: datetime) -> datetime:
    return local_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _time_text(dt: datetime) -> str:
    hour12 = dt.hour % 12 or 12
    return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _day_text(dt: datetime) -> str:
    return f"{dt.strftime('%a %b')} {dt.day}"


def label_for(profile: SchedulingProfile, start_utc: datetime, tz: Optional[str] = None,
              *, zone: Optional[ZoneInfo] = None) -> str:
    """'Tue Sep 9, 10:00 AM' — or 'Tue Sep 9, 8:00 AM–10:00 AM' for a window."""
    zone = zone or resolve_tz(tz)
    local = _to_local(start_utc, zone)
    if profile.window_minutes:
        end = _to_local(start_utc + timedelta(minutes=profile.window_minutes), zone)
        return f"{_day_text(local)}, {_time_text(local)}–{_time_text(end)}"
    return f"{_day_text(local)}, {_time_text(local)}"


# --------------------------------------------------------------------------- slots
@dataclass(frozen=True)
class Slot:
    slot_id: str
    start_utc: datetime
    end_utc: datetime
    label: str
    day: str
    time: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "label": self.label,
            "day": self.day,
            "time": self.time,
        }


def _slot(profile: SchedulingProfile, local_start: datetime, zone: ZoneInfo) -> Slot:
    start_utc = _to_utc_naive(local_start)
    end_utc = start_utc + timedelta(minutes=profile.duration_minutes)
    return Slot(
        slot_id=start_utc.strftime(SLOT_ID_FORMAT),
        start_utc=start_utc,
        end_utc=end_utc,
        label=label_for(profile, start_utc, zone=zone),
        day=_day_text(local_start),
        time=_time_text(local_start),
    )


def suggest_slots(profile: SchedulingProfile, *, now_utc: Optional[datetime] = None,
                  tz: Optional[str] = None, page: int = 0, count: Optional[int] = None) -> List[Slot]:
    """The next ``count`` slots after ``now + lead_minutes`` inside business hours,
    skipping ``page * count`` earlier ones ("More times" pages forward)."""
    now_utc = now_utc or datetime.utcnow()
    zone = resolve_tz(tz)
    count = count or profile.slots_per_page
    skip = max(0, int(page or 0)) * count
    step = timedelta(minutes=profile.duration_minutes)
    earliest = _to_local(now_utc + timedelta(minutes=profile.lead_minutes), zone)
    day = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
    out: List[Slot] = []
    for _ in range(_MAX_DAYS_AHEAD):
        if day.weekday() in profile.business_days:
            t = day.replace(hour=profile.open_hour)
            close = day.replace(hour=profile.close_hour)
            while t + step <= close:
                if t >= earliest:
                    if skip:
                        skip -= 1
                    else:
                        out.append(_slot(profile, t, zone))
                        if len(out) >= count:
                            return out
                t += step
        day += timedelta(days=1)
    return out


def resolve_slot(profile: SchedulingProfile, slot_id: Optional[str], *,
                 now_utc: Optional[datetime] = None, tz: Optional[str] = None) -> Optional[Slot]:
    """Parse a ``slot_id`` (from a chip or a prior offer) back into a Slot; None if
    it is malformed or already in the past."""
    if not slot_id:
        return None
    try:
        start_utc = datetime.strptime(str(slot_id), SLOT_ID_FORMAT)
    except ValueError:
        return None
    now_utc = now_utc or datetime.utcnow()
    if start_utc <= now_utc:
        return None
    zone = resolve_tz(tz)
    return _slot(profile, _to_local(start_utc, zone), zone)


# --------------------------------------------------------------------------- offer policy
def should_offer(profile: SchedulingProfile, *, severity: Optional[str], first_answer: bool,
                 declined: bool, has_upcoming: bool) -> str:
    """'offer' | 'already_booked' | 'none' for an answer turn (no scheduling intent)."""
    sev = str(getattr(severity, "value", severity) or "").upper()
    if has_upcoming:
        return "already_booked" if first_answer else "none"
    if declined:
        return "none"
    if first_answer:
        return "none" if sev in profile.first_offer_excludes else "offer"
    return "offer" if sev in profile.offer_on_severities else "none"


# --------------------------------------------------------------------------- intent
# Bare number words ("one", "two") are deliberately absent: "the Tuesday one"
# would otherwise read as the first slot.
_ORDINALS = {
    "first": 0, "1st": 0, "earliest": 0,
    "second": 1, "2nd": 1, "middle": 1,
    "third": 2, "3rd": 2, "last": -1,
}
_RE_MORE = re.compile(
    r"\b(more|other|different|later|additional|next|another)\b.*\b(times?|slots?|options?|days?|dates?|choices?)\b"
    r"|\bshow me more\b|\bwhat else\b|\bnone of (those|these)\b|\blater\b", re.I)
_RE_LIST = re.compile(
    r"\bmy (schedule|appointments?|bookings?|visits?|sessions?|meetings?|consultations?)\b"
    r"|\bwhat('s| is| do i have)( on my| in my)? (schedule|calendar)\b|\bwhat.*\bscheduled\b"
    r"|\bupcoming (appointments?|visits?|bookings?)\b|\bam i booked\b"
    r"|\bdo i have (an? )?(appointment|booking|visit)\b", re.I)
_RE_CANCEL = re.compile(r"\bcancel\b", re.I)
_RE_RESCHEDULE = re.compile(r"\b(reschedule|move|change|push back|postpone|different time for)\b", re.I)
_RE_BOOK = re.compile(
    r"\b(book|schedule|set up|make|reserve|arrange)\b.*\b(appointment|visit|consult\w*|meeting|session|time|slot|technician)\b"
    r"|\b(book|schedule) (me|it|one|an?|that)\b", re.I)
_RE_DECLINE = re.compile(
    r"^\s*(no|nope|nah|not now|no thanks?|no thank you|maybe later|not right now|not today|"
    r"i'?m (good|fine|ok|okay)|(i )?don'?t need|skip( it)?|pass)\b", re.I)
_RE_ACCEPT = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please|sounds good|let'?s do (it|that)|i'?d like (to|that)|"
    r"i would like|go ahead|absolutely)\b", re.I)
_RE_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
_RE_NAME_PREFIX = re.compile(
    r"^\s*(my name is|my name'?s|the name is|name is|name:|it'?s|its|i'?m|i am|under|put it under|use|call me|this is)\s+",
    re.I)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def clean_name(text: str) -> str:
    name = _RE_NAME_PREFIX.sub("", str(text or "")).strip()
    name = re.sub(r"[.!,;:]+$", "", name).strip().strip('"\'')
    name = re.sub(r"\s+", " ", name)
    return name[:_MAX_NAME_LEN]


def looks_like_name(text: str) -> bool:
    t = clean_name(text)
    return bool(t) and "?" not in t and len(t.split()) <= 5 and not re.search(r"\d", t)


def match_slot(text: str, slots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick one of the offered slots from typed text: an ordinal ("the second one"),
    a time ("10:00", "10am") or a weekday ("Tuesday"), else None."""
    if not slots:
        return None
    t = str(text or "").lower()
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            try:
                return slots[idx]
            except IndexError:
                return None
    m = _RE_TIME.search(t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "").lower()
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidates = []
            for s in slots:
                tm = re.match(r"(\d{1,2}):(\d{2}) (AM|PM)", str(s.get("time", "")))
                if not tm:
                    continue
                sh, sm, sap = int(tm.group(1)), int(tm.group(2)), tm.group(3).lower()
                if sm != minute:
                    continue
                if ampm:
                    if sh == (hour % 12 or 12) and sap == ampm:
                        candidates.append(s)
                elif hour > 12:
                    if sh == hour - 12 and sap == "pm":
                        candidates.append(s)
                elif sh == hour:
                    candidates.append(s)
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                # An ambiguous bare hour: prefer a weekday match, else the earliest.
                for s in candidates:
                    if any(d in t for d in _DAYS if str(s.get("day", "")).lower().startswith(d)):
                        return s
                return candidates[0]
    for s in slots:
        day = str(s.get("day", "")).lower()
        if day[:3] in _DAYS and re.search(rf"\b{day[:3]}\w*\b", t):
            return s
    return None


def match_appointment(text: str, appointments: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick one of the listed appointments by ordinal or by its day/time, else the
    only one when there is exactly one."""
    if not appointments:
        return None
    if len(appointments) == 1:
        return appointments[0]
    return match_slot(text, appointments)


def parse_action(message: str, last_meta: Optional[Dict[str, Any]],
                 upcoming: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Scheduling intent of a typed message (a structured chip action from the
    request wins over this — see the intake node). ``last_meta`` is the previous
    assistant message's ``metadata.scheduling``; ``upcoming`` the client's
    scheduled appointments (dicts with ``id``/``day``/``time``). Returns an action
    dict (``action`` + ``slot_id``/``appointment_id``/``page``/``name``) or None
    when the message is not about scheduling."""
    text = str(message or "").strip()
    if not text:
        return None
    meta = last_meta or {}
    state = str(meta.get("state") or "")
    pending = meta.get("pending") or {}
    slots = list(meta.get("slots") or [])
    listed = list(meta.get("appointments") or [])
    offering = state in ("offered", "choosing", "rescheduling")

    if _RE_MORE.search(text) and (offering or pending):
        act: Dict[str, Any] = {"action": "more_times", "page": int(meta.get("page") or 0) + 1}
        if pending.get("awaiting") == "slot":
            act["appointment_id"] = pending.get("reschedule_id")
        return act
    # Cancel / reschedule before list: "cancel my appointment" mentions "my
    # appointment" too.
    if _RE_CANCEL.search(text):
        target = match_appointment(text, listed or upcoming)
        return {"action": "cancel", "appointment_id": target.get("id") if target else None}
    if _RE_RESCHEDULE.search(text):
        target = match_appointment(text, listed or upcoming)
        act = {"action": "reschedule", "appointment_id": target.get("id") if target else None}
        if pending.get("awaiting") == "slot" and slots:
            picked = match_slot(text, slots)
            if picked:
                act.update(appointment_id=pending.get("reschedule_id"), slot_id=picked.get("slot_id"))
        return act
    if _RE_LIST.search(text) and not _RE_BOOK.search(text):
        return {"action": "list"}
    if pending.get("awaiting") == "slot" and slots:
        picked = match_slot(text, slots)
        if picked:
            return {"action": "reschedule", "appointment_id": pending.get("reschedule_id"),
                    "slot_id": picked.get("slot_id")}
    if pending.get("awaiting") == "name":
        if _RE_DECLINE.search(text):
            return {"action": "decline"}
        if looks_like_name(text):
            return {"action": "book", "slot_id": pending.get("slot_id"), "name": clean_name(text)}
        return None
    if offering:
        if _RE_DECLINE.search(text):
            return {"action": "decline"}
        picked = match_slot(text, slots)
        if picked:
            return {"action": "book", "slot_id": picked.get("slot_id")}
        if _RE_ACCEPT.search(text) or _RE_BOOK.search(text):
            return {"action": "accept", "page": int(meta.get("page") or 0)}
        return None
    if _RE_BOOK.search(text):
        return {"action": "accept", "page": 0}
    return None


# --------------------------------------------------------------------------- store
def _row_to_dict(row: Any) -> Dict[str, Any]:
    start = row.start_at
    return {
        "id": row.id,
        "client_id": row.client_id,
        "session_id": row.session_id,
        "theme": row.theme,
        "name": row.name,
        "provider_label": row.provider_label,
        "start_utc": start.isoformat() if start else None,
        "end_utc": (start + timedelta(minutes=row.duration_minutes or 0)).isoformat() if start else None,
        "duration_minutes": row.duration_minutes,
        "timezone": row.timezone,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "notes": dict(row.notes or {}),
    }


def describe(appt: Dict[str, Any], profile: SchedulingProfile, tz: Optional[str] = None) -> Dict[str, Any]:
    """Add the local ``label``/``day``/``time`` to a stored appointment dict."""
    out = dict(appt)
    start = appt.get("start_utc")
    if start:
        start_dt = datetime.fromisoformat(start) if isinstance(start, str) else start
        zone = resolve_tz(tz)
        local = _to_local(start_dt, zone)
        out["label"] = label_for(profile, start_dt, zone=zone)
        out["day"] = _day_text(local)
        out["time"] = _time_text(local)
    return out


class AppointmentStore:
    """CRUD over the ``appointments`` table. One module-level instance (``store``)
    so tests can swap it for an in-memory fake; sessions come from
    ``get_db_context`` (imported lazily so importing a theme does not build the
    engine)."""

    @staticmethod
    def _ctx():
        from backend.database.db import get_db_context  # noqa: PLC0415 - lazy: no engine at theme import

        return get_db_context()

    @staticmethod
    def _model():
        from backend.models.db_models import Appointment  # noqa: PLC0415

        return Appointment

    def book(self, *, client_id: str, session_id: str, theme: str, name: Optional[str],
             provider_label: str, start_utc: datetime, duration_minutes: int,
             timezone_name: Optional[str], notes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        Appointment = self._model()
        row = Appointment(
            id=str(uuid.uuid4()), client_id=client_id, session_id=session_id, theme=theme,
            name=name, provider_label=provider_label, start_at=start_utc,
            duration_minutes=int(duration_minutes), timezone=timezone_name,
            status=STATUS_SCHEDULED, notes=dict(notes or {}),
        )
        with self._ctx() as db:
            db.add(row)
            db.flush()
            return _row_to_dict(row)

    def get(self, appointment_id: str) -> Optional[Dict[str, Any]]:
        Appointment = self._model()
        with self._ctx() as db:
            row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
            return _row_to_dict(row) if row else None

    def list_upcoming(self, client_id: str, *, theme: Optional[str] = None,
                      now_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
        Appointment = self._model()
        now_utc = now_utc or datetime.utcnow()
        with self._ctx() as db:
            q = (db.query(Appointment)
                 .filter(Appointment.client_id == client_id,
                         Appointment.status == STATUS_SCHEDULED,
                         Appointment.start_at >= now_utc))
            if theme:
                q = q.filter(Appointment.theme == theme)
            return [_row_to_dict(r) for r in q.order_by(Appointment.start_at.asc()).all()]

    def list_all(self, *, client_id: str, status: str = STATUS_SCHEDULED,
                 theme: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        Appointment = self._model()
        with self._ctx() as db:
            q = db.query(Appointment).filter(Appointment.client_id == client_id)
            if status and status != "all":
                q = q.filter(Appointment.status == status)
            if theme:
                q = q.filter(Appointment.theme == theme)
            rows = q.order_by(Appointment.start_at.asc()).limit(max(1, min(int(limit), 500))).all()
            return [_row_to_dict(r) for r in rows]

    def cancel(self, appointment_id: str, *, client_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        Appointment = self._model()
        with self._ctx() as db:
            row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
            if row is None or (client_id and row.client_id != client_id):
                return None
            row.status = STATUS_CANCELLED
            row.updated_at = datetime.utcnow()
            db.flush()
            return _row_to_dict(row)

    def reschedule(self, appointment_id: str, *, start_utc: datetime, client_id: Optional[str] = None,
                   timezone_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        Appointment = self._model()
        with self._ctx() as db:
            row = db.query(Appointment).filter(Appointment.id == appointment_id).first()
            if row is None or (client_id and row.client_id != client_id):
                return None
            notes = dict(row.notes or {})
            notes["rescheduled_from"] = row.start_at.isoformat() if row.start_at else None
            row.notes = notes            # a new dict, so the JSON column's change is detected
            row.start_at = start_utc
            row.status = STATUS_SCHEDULED
            if timezone_name:
                row.timezone = timezone_name
            row.updated_at = datetime.utcnow()
            db.flush()
            return _row_to_dict(row)

    def latest_name(self, client_id: str) -> Optional[str]:
        Appointment = self._model()
        with self._ctx() as db:
            row = (db.query(Appointment)
                   .filter(Appointment.client_id == client_id, Appointment.name.isnot(None))
                   .order_by(Appointment.created_at.desc()).first())
            return row.name if row else None

    def delete_for_client(self, client_id: str) -> int:
        """Test cleanup only."""
        Appointment = self._model()
        with self._ctx() as db:
            return db.query(Appointment).filter(Appointment.client_id == client_id).delete()


store = AppointmentStore()
