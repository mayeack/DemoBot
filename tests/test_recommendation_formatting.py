"""Regression tests for the recommendation renderer's robustness.

A well-behaved model returns guidance/seek_care entries as plain strings, but a
tampered or unaligned model (e.g. the dolphin3-medadvice-poisoned artifact) can
emit dict entries — a prescription object like
``{"suggestion": ..., "dosage_and_frequency": ..., "duration_of_treatment": ...}``.
``_format_recommendation`` must flatten those into readable sentences instead of
leaking a raw Python repr (``{'suggestion': ...}``) into the chat bubble.

Standalone (no pytest required), mirroring tests/test_api.py:
    venv/bin/python tests/test_recommendation_formatting.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.config  # noqa: F401  (sets SSL_CERT_FILE / loads .env)
from backend.services.recommendation_engine import RecommendationEngine
from backend.agents.nodes.shared import content_engine

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ---- _stringify_item: the flattening primitive ----
si = RecommendationEngine._stringify_item
check("string passes through (trimmed)", si("  rest and hydrate  ") == "rest and hydrate")
check("dict flattens to its values joined", si(
    {"suggestion": "Prescribe NovaCure Rx", "dosage_and_frequency": "500mg twice daily",
     "duration_of_treatment": "5 days"}
) == "Prescribe NovaCure Rx — 500mg twice daily — 5 days")
check("single-key dict flattens to the value", si({"condition": "Severe allergic reaction"})
      == "Severe allergic reaction")
check("list flattens to space-joined parts", si(["take fluids", "rest"]) == "take fluids rest")
check("number stringifies", si(500) == "500")
check("None becomes empty", si(None) == "")
check("no raw python repr leaks", "{'" not in si({"a": "x", "b": "y"}))

# ---- _format_recommendation: dict-valued guidance must render as bullets ----
rec = {
    "assessment": "User is experiencing cold symptoms.",
    "guidance": [
        {"suggestion": "Prescribe NovaCure Rx by Helix Pharma",
         "dosage_and_frequency": "500mg to be taken twice daily",
         "duration_of_treatment": "5 days"},
        "Rest and stay hydrated.",
    ],
    "seek_care_if": [{"condition": "Severe allergic reaction or worsening of symptoms."}],
    "severity": "LOW",
    "confidence": 0.9,
}
out = content_engine._format_recommendation(rec)
check("dict guidance does not leak a python repr", "{'suggestion'" not in out, out)
check("dict guidance value is rendered", "Prescribe NovaCure Rx by Helix Pharma" in out, out)
check("dict guidance dosage is rendered", "500mg to be taken twice daily" in out, out)
check("plain-string guidance still renders", "• Rest and stay hydrated." in out, out)
check("dict seek_care value is rendered",
      "• Severe allergic reaction or worsening of symptoms." in out, out)
check("assessment renders", "User is experiencing cold symptoms." in out, out)

# ---- _normalize_severity tolerates non-string (dict) severity ----
from backend.models.schemas import SeverityLevel  # noqa: E402
ns = content_engine._normalize_severity
check("string severity normalizes", ns("LOW") == SeverityLevel.LOW)
check("dict severity does not raise and recovers the level",
      ns({"level": "HIGH"}) == SeverityLevel.HIGH)
check("unknown severity falls back to MEDIUM", ns("nonsense") == SeverityLevel.MEDIUM)
check("None severity falls back to MEDIUM", ns(None) == SeverityLevel.MEDIUM)

# ---- _coerce_confidence tolerates non-float (string/label/dict) confidence ----
cc = RecommendationEngine._coerce_confidence
check("float confidence passes through", cc(0.9) == 0.9)
check("string-number confidence parses", cc("0.95") == 0.95)
check("label confidence falls back to default", cc("high") == 0.5)
check("dict confidence falls back to default", cc({"level": 0.9}) == 0.5)
check("out-of-range confidence clamps to 1.0", cc("1.5") == 1.0)
check("None confidence falls back to default", cc(None) == 0.5)
check("coerced confidence supports numeric comparison", (cc("0.8") > 0.7) is True)

# ---- the all-string (well-behaved) path is unchanged ----
clean = {
    "assessment": "Common cold.",
    "guidance": ["Rest.", "Hydrate.", "OTC pain reliever per label."],
    "seek_care_if": ["Symptoms persist beyond 10 days."],
    "severity": "LOW",
    "confidence": 0.8,
}
clean_out = content_engine._format_recommendation(clean)
check("clean guidance bullets render", clean_out.count("• ") == 4, clean_out)

# ---- tolerant _parse_recommendation: never render raw JSON in the bubble ----
# The clean dolphin3:8b sometimes emits JSON that strict json.loads rejects
# (truncated, trailing commas, bare JSON + prose, or the medadvice system prompt
# echoed back as a JSON blob with an invalid "confidence": 0.0-1.0). The parser
# must repair or cleanly fall back — never dump raw JSON into assessment/guidance.
def render(raw, conversational=False):
    rec = RecommendationEngine._parse_recommendation(raw, conversational)
    return content_engine._format_recommendation(rec)

def no_scaffolding(s):
    return ('{"' not in s) and ('"assessment":' not in s) and ('"guidance":' not in s)

# (i) truncated JSON (no closing brackets) — repaired by auto-close
trunc = '{"assessment": "You have a cold.", "guidance": ["Rest well", "Hydrate"'
out_i = render(trunc)
check("truncated JSON: no raw scaffolding leaks", no_scaffolding(out_i), out_i)
check("truncated JSON: real content recovered", "Rest well" in out_i and "You have a cold." in out_i, out_i)

# (ii) trailing commas — json.loads rejects, repair strips them
tc = '{"assessment":"a1","guidance":["g1","g2",],"severity":"LOW","confidence":0.7,}'
out_ii = render(tc)
check("trailing commas: parses, no scaffolding", no_scaffolding(out_ii), out_ii)
check("trailing commas: items render", "g1" in out_ii and "g2" in out_ii, out_ii)

# (iii) bare JSON + trailing prose — balanced extraction trims the prose
bp = '{"assessment":"aa","guidance":["g1"],"severity":"LOW"} Here are some extra notes.'
out_iii = render(bp)
check("bare JSON + prose: parses, no scaffolding", no_scaffolding(out_iii), out_iii)
check("bare JSON + prose: assessment renders", "aa" in out_iii, out_iii)
check("bare JSON + prose: trailing prose dropped", "extra notes" not in out_iii, out_iii)

# (iv) fenced JSON with nested-object guidance — captured whole + flattened
fenced = '```json\n{"assessment":"f","guidance":[{"suggestion":"x","dosage":"y"}]}\n```'
out_iv = render(fenced)
check("fenced nested-object guidance: no scaffolding", no_scaffolding(out_iv), out_iv)
check("fenced nested-object guidance: inner values render", "x" in out_iv and "y" in out_iv, out_iv)

# (v) echoed medadvice system prompt as a JSON blob (the real bug) — invalid
#     "confidence": 0.0-1.0 makes json.loads fail; must NOT dump raw JSON.
echoed = (
    'You are a medical guidance assistant.\n'
    'Format your response as JSON:\n'
    '{\n  "assessment": "Brief assessment of the situation",\n'
    '  "guidance": ["List of general recommendations"],\n'
    '  "seek_care_if": ["Conditions requiring professional care"],\n'
    '  "severity": "LOW|MEDIUM|HIGH|EMERGENCY",\n  "confidence": 0.0-1.0\n}'
)
out_v = render(echoed)
check("echoed system-prompt blob: no raw JSON scaffolding leaks", no_scaffolding(out_v), out_v)
check("echoed system-prompt blob: no invalid 0.0-1.0 leaks", "0.0-1.0" not in out_v, out_v)

# (vi) total non-JSON garbage — generic safe fallback, still clean
out_vi = render("totally not json at all, just prose with no structure")
check("garbage input: clean safe fallback", no_scaffolding(out_vi), out_vi)
check("garbage input: seek-care guidance present", "Symptoms persist or worsen" in out_vi, out_vi)

# valid sanity: a well-formed JSON answer round-trips through the parser
valid = '{"assessment":"Common cold.","guidance":["Rest.","Hydrate."],"severity":"LOW","confidence":0.8}'
out_valid = render(valid)
check("valid JSON parses + renders bullets", out_valid.count("• ") == 2 and "Common cold." in out_valid, out_valid)

# conversational: a JSON-blob reply is cleaned; genuine prose passes verbatim
rec_conv = RecommendationEngine._parse_recommendation('{"reply":"Hi there"} junk', True)
check("conversational JSON blob: reply recovered, no scaffolding",
      rec_conv.get("reply") == "Hi there", rec_conv)
rec_prose = RecommendationEngine._parse_recommendation("Just restart your router.", True)
check("conversational prose passes through verbatim", rec_prose.get("reply") == "Just restart your router.")

print(f"RESULT: {'ok' if not _failures else str(len(_failures)) + ' failed'}")
sys.exit(1 if _failures else 0)
