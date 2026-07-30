"""Persistence and concurrency integrity of the SQLite path.

Covers the three data-loss defects found by the ultracode review:

  F20  StaticPool handed the SAME sqlite3 connection to every Session, so
       concurrent event-loop and threadpool writers interleaved transactions on
       one connection and could commit or roll back each other's work.
  F25  Conversation.messages is a plain JSON column (no MutableList). Resuming a
       conversation from the DB stored the ORM-loaded list BY REFERENCE and
       mutated it in place, so SQLAlchemy compared the attribute against itself,
       saw no change, and omitted it from the UPDATE — losing that turn.
  F27  EscalationRules.should_escalate accumulated onto shared instance state,
       so concurrent turns could clobber each other's escalation reasons.

Standalone (no pytest required), mirroring tests/test_tool_guard.py:
    venv/bin/python tests/test_db_integrity.py

Side-effect-safe: every check runs against a temp SQLite file, never the app's
medadvice.db. DATABASE_URL is redirected BEFORE backend.database.db is imported.
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.config  # noqa: F401  (sets SSL_CERT_FILE / loads .env)

# Point the engine at a throwaway database before it is constructed at import.
_tmpdir = tempfile.mkdtemp(prefix="demobot-dbtest-")
_dbfile = os.path.join(_tmpdir, "integrity.db")
from backend.config import settings  # noqa: E402

settings.database_url = f"sqlite:///{_dbfile}"

from backend.database import db as dbmod  # noqa: E402
from backend.models.db_models import Base, Conversation  # noqa: E402

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


check("temp DB in use (never the app's medadvice.db)",
      _dbfile in str(dbmod.engine.url), str(dbmod.engine.url))
Base.metadata.create_all(bind=dbmod.engine)

# 1. The file-backed SQLite engine must NOT share one connection -------------
# StaticPool returns the same DBAPI connection to every checkout; the default
# pool gives each Session its own. Two concurrently-open sessions are the test.
s1, s2 = dbmod.SessionLocal(), dbmod.SessionLocal()
try:
    c1 = s1.connection().connection.dbapi_connection
    c2 = s2.connection().connection.dbapi_connection
    check("file-backed sqlite: concurrent sessions get distinct connections",
          c1 is not c2, f"{c1!r} vs {c2!r}")
finally:
    s1.close()
    s2.close()

# In-memory SQLite must still share, or each connection is a new empty DB.
_saved_url = settings.database_url
settings.database_url = "sqlite:///:memory:"
try:
    mem_engine = dbmod.create_db_engine()
    from sqlalchemy.pool import StaticPool

    check("in-memory sqlite: still StaticPool (shared connection)",
          isinstance(mem_engine.pool, StaticPool), type(mem_engine.pool).__name__)
finally:
    settings.database_url = _saved_url

# 2. A concurrent writer must not lose rows under interleaved commits --------
# Each thread opens its own session (as the governance writer does via
# get_db_context) and commits its own row. With one shared connection these
# commits interleave and rows can be lost or error out.
WRITERS, PER_WRITER = 8, 5
errors = []


def _writer(n):
    for i in range(PER_WRITER):
        try:
            with dbmod.get_db_context() as s:
                s.add(Conversation(session_id=f"w{n}-msg{i}", messages=[],
                                   disclaimer_accepted=True, escalated=False))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"w{n}-{i}: {exc}")


threads = [threading.Thread(target=_writer, args=(n,)) for n in range(WRITERS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("concurrent writers: no exceptions", not errors, "; ".join(errors[:3]))
with dbmod.get_db_context() as s:
    written = s.query(Conversation).filter(
        Conversation.session_id.like("w%-msg%")).count()
check("concurrent writers: every row persisted",
      written == WRITERS * PER_WRITER, f"{written} of {WRITERS * PER_WRITER}")

# 3. In-place JSON mutation is invisible to SQLAlchemy (the F25 mechanism) ---
# Demonstrates WHY _prepare_session must copy: with a plain JSON column and no
# MutableList, mutating the loaded list in place and assigning it back compares
# the attribute against itself, so `messages` is omitted from the UPDATE.
# (The end-to-end guard for the real chat path is the resume-after-restart check
# in tests/test_api.py — this one pins the ORM behavior the fix depends on.)
with dbmod.get_db_context() as s:
    s.add(Conversation(session_id="alias-me",
                       messages=[{"role": "user", "content": "first"}],
                       disclaimer_accepted=True, escalated=False))

with dbmod.get_db_context() as s:
    row = s.query(Conversation).filter(Conversation.session_id == "alias-me").first()
    aliased = row.messages          # BY REFERENCE — the old _prepare_session
    aliased.append({"role": "assistant", "content": "second"})
    row.messages = aliased          # same object back in — no detectable change

with dbmod.get_db_context() as s:
    row = s.query(Conversation).filter(Conversation.session_id == "alias-me").first()
    aliased_len = len(row.messages or [])

# Now the copying pattern the fix uses, on a fresh row.
with dbmod.get_db_context() as s:
    s.add(Conversation(session_id="copy-me",
                       messages=[{"role": "user", "content": "first"}],
                       disclaimer_accepted=True, escalated=False))

with dbmod.get_db_context() as s:
    row = s.query(Conversation).filter(Conversation.session_id == "copy-me").first()
    copied = list(row.messages or [])            # COPY — the fixed load
    copied.append({"role": "assistant", "content": "second"})
    same = s.query(Conversation).filter(Conversation.session_id == "copy-me").first()
    check("copy: identity map returns the same instance (bug precondition)",
          same is row)
    same.messages = list(copied)                 # distinct object

with dbmod.get_db_context() as s:
    row = s.query(Conversation).filter(Conversation.session_id == "copy-me").first()
    msgs = row.messages or []
    check("copy: appended turn was persisted", len(msgs) == 2, f"len={len(msgs)}")
    check("copy: both messages present in order",
          [m.get("content") for m in msgs] == ["first", "second"],
          str([m.get("content") for m in msgs]))
    check("copy pattern persists where the alias pattern did not",
          len(msgs) > aliased_len, f"copy={len(msgs)} alias={aliased_len}")

# 4. EscalationRules must not return shared instance state (F27) -------------
# The contamination mechanism: `self.escalation_reasons = []` REBINDS, so a turn
# already inside should_escalate keeps appending through self.* and lands its
# reasons in the newer turn's list — then returns that same object. The
# invariant that makes it impossible is simply that the returned list is not the
# instance attribute. Asserting the invariant is deterministic; asserting the
# race is not.
from backend.models.schemas import SeverityLevel  # noqa: E402
from backend.services.escalation_rules import EscalationRules  # noqa: E402

shared = EscalationRules()   # the singleton pattern both call sites use
_, reasons_a = shared.should_escalate(
    conversation_history=[{"role": "user", "content": "I have chest pain"}],
    severity=SeverityLevel.EMERGENCY, user_input="I have chest pain", ai_confidence=0.9,
)
check("escalation: returned list is NOT shared instance state",
      reasons_a is not shared.escalation_reasons)
check("escalation: emergency turn reports its own reasons",
      any("Emergency" in r or "severity" in r for r in reasons_a), str(reasons_a))

_, reasons_b = shared.should_escalate(
    conversation_history=[{"role": "user", "content": "my wifi is slow"}],
    severity=SeverityLevel.LOW, user_input="my wifi is slow", ai_confidence=0.9,
)
check("escalation: a later turn returns a distinct list", reasons_a is not reasons_b)
check("escalation: benign turn inherits nothing", reasons_b == [], str(reasons_b))
check("escalation: earlier result survives the later turn untouched",
      any("Emergency" in r or "severity" in r for r in reasons_a), str(reasons_a))

# Concurrency smoke: many turns through the one shared instance, each keeping
# its own verdict.
_conc = {}


def _turn(tag, user_input, severity):
    _, rs = shared.should_escalate(
        conversation_history=[{"role": "user", "content": user_input}],
        severity=severity, user_input=user_input, ai_confidence=0.9,
    )
    _conc[tag] = rs


_ts = []
for i in range(6):
    emergency = i % 2 == 0
    _ts.append(threading.Thread(target=_turn, args=(
        f"t{i}",
        "I have chest pain" if emergency else "my wifi is slow",
        SeverityLevel.EMERGENCY if emergency else SeverityLevel.LOW,
    )))
for t in _ts:
    t.start()
for t in _ts:
    t.join()
check("escalation: concurrent benign turns stay empty",
      all(_conc[f"t{i}"] == [] for i in range(6) if i % 2),
      str({k: v for k, v in _conc.items() if v}))

# Cleanup: drop the temp database entirely.
dbmod.engine.dispose()
try:
    os.remove(_dbfile)
    os.rmdir(_tmpdir)
except OSError:
    pass

print()
if _failures:
    print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
    sys.exit(1)
print("All DB-integrity checks passed.")
