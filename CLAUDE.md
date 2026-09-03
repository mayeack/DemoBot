# DemoBot — project instructions

## Demo Controls drawer formatting

Every control card in the **Demo Controls** drawer (`frontend/index.html`, inside
`#settingsDrawer`) uses one neutral format. **Include Synthetic PII/PHI in
Responses** is the reference implementation — copy that card when adding a new
control.

- Card container: `bg-gray-50 border border-gray-200 rounded-lg p-3`
- Title: `text-sm font-semibold text-gray-700`
- Description: `text-xs text-gray-500`

Do **not** give a card its own accent palette (`bg-indigo-50`, `text-sky-700`,
`text-fuchsia-500`, `bg-red-50`, …) to make it stand out. The drawer has to read
as one list; per-card colors made unrelated controls look like separate widgets
and made "which of these is a guardrail vs. a load generator" harder to see, not
easier. A card that needs emphasis earns it from its position in the drawer.

The drawer is organized into named groups, each a `<section data-group="…">`
with a neutral `<h3>` header: **Guardrails** (Cisco AI Defense, Agent
Observability Controls, NeMo Guardrails, NemoClaw Guardrails, Internal Policy
Engine), **Agent Pipeline** (Multi-Agent Mode), **Synthetic Content** (the
injection toggles), **Load & Incident Generators** (Auto-Generate Sessions,
Trigger Demo Incident, Prompt Injection Spray) and **Display** (Appearance). A
new card goes inside the group it belongs to — never loose at the top level,
and not in a new group unless it fits none of these.

Two kinds of card exist and they look the same:

- **per-request toggles** (Cisco AI Defense, Agent Observability Controls,
  NeMo Guardrails, Internal Policy Engine, Multi-Agent Mode, the injection
  toggles) — the toggle's state is sent on every chat request
  (`chat.js buildChatPayload`);
- **server-side toggles** (Auto-Generate Sessions, NemoClaw Guardrails, Trigger
  Demo Incident, Prompt Injection Spray) — the toggle calls an API and polls its
  status; the state lives on the server (NemoClaw persists in `settings_store`).

Options a host cannot run are greyed out with the reason as a tooltip, driven by
`GET /api/server-info` `gated` (`backend/host_capabilities.py`) — never by a
client-side guess.

Color is reserved for **state**, never identity. These may stay colored:

- the status pill (`#…Status`) — one look for every card, set only through
  `setPill()` in `frontend/js/chat.js`: **On** = `bg-green-100 text-green-700`,
  **Off** = `bg-gray-100 text-gray-600`. No per-card ON colors, no verbs
  ("REVIEWING", "SPRAYING"), no pulse; what a control is doing while On goes in
  the pill's `title` tooltip.
- live counters/timers beside the pill (`#autoPromptStats`, `#incidentRemaining`,
  `#sprayRemaining`)
- the toggle's own `peer-checked:bg-*` / `peer-focus:ring-*` accent

Dark mode is handled centrally for the neutral classes (`html.dark .bg-gray-50`,
`.border-gray-200`, `.text-gray-700`, `.text-gray-500`), so a card that follows
this format needs no dark-mode rule of its own. A card that invents its own
palette does — which is another reason not to.

## Blueprint feature parity

`backend/agents/blueprints/` holds the selectable agentic architectures
(`demobot_multi_agent`, `nvidia_virtual_assistant`; the chat header's
**Blueprint** dropdown). A blueprint contributes only its **generation core**;
everything else is shared and must behave identically whichever blueprint is
selected:

- every guardrail node (`blueprints/guardrails.py` `PRE_NODES` / `POST_NODES`),
- every Demo Controls toggle and `ChatRequest` flag (`force_*_injection`,
  `ai_defense_review`, `internal_policy_review`, `agent_control_review`,
  `nemo_guardrails_review`, `multi_agent_mode`),
- the governance-event contract (`guardrail_ids`, `policy_blocked`,
  `*_detected`, token sums, `agent_trace`, `workflow_name`/`blueprint`),
- the SSE stage frames for the guardrail nodes and the OTel workflow/agent spans.

Rules:

- A new guardrail, toggle or governance field goes into the **shared chain**
  (`blueprints/guardrails.py`, the nodes it wires, `state.py`) — never inside one
  core. If a feature genuinely needs core work, land it in **both** cores in the
  same PR.
- Extend `tests/test_blueprint_parity.py` in that same PR (a scenario for a new
  guardrail/toggle; a key in `CORE_STATE_CONTRACT` for a new field the POST
  chain reads). It runs the scenario matrix through every registered blueprint
  and fails on any divergence; `tests/run_all.sh` runs it with the rest.
- Keys a core writes to the state must be declared on `DemoBotState` — LangGraph
  silently drops undeclared keys.
- Blocked turns carry the same `workflow_name`/`blueprint` identity as the happy
  path (`governance_identity_overrides`); keep passing it from every block handler.

## Appointment scheduling

`docs/scheduling.md` is the design. The rules that keep it demo-safe:

- **Deterministic scheduling, LLM voice.** Slots, intent, bookings and the clickable
  options come from `backend/services/scheduling.py`; the model only writes wording.
  Never parse a time, a name or an action out of model text.
- The two nodes (`scheduling_intake` PRE, `scheduling` POST) live in the **shared
  chain** (`blueprints/guardrails.py`), like a guardrail. The only core-side code is
  "skip the domain specialists on a scheduling turn", and it exists in **both** cores.
- Verticalization is a `SchedulingProfile` on `ThemeConfig.scheduling`
  (`backend/agents/themes/<theme>.py`) — copy, noun, hours, slot length, offer
  severities. A new theme defines one; never branch on the theme key in the nodes.
- The chips are the backend's `scheduling.actions`; `frontend/js/chat.js` renders
  them generically and never invents an action.
- `client_id` is the browser's partition key, **not** authorization (one shared
  access key); the API filters by it, nothing more.
- Extend `tests/test_scheduling.py` and the `scheduling_*` scenarios in
  `tests/test_blueprint_parity.py` in the same PR as any scheduling change.

## Product naming in user-visible text

User-visible text says **"Splunk Agent Observability"**, never "Galileo". This
covers UI copy, governance-log `reasons`, and `response_text` block banners —
anything an audience sees in the app, the Governance Logs page, or Splunk.

Leave the vendor's own names alone everywhere else, because they are load-bearing:

- the `galileo` / `galileo_core` packages and `GalileoLogger` (SDK imports)
- `GALILEO_API_KEY`, `GALILEO_PROJECT`, `GALILEO_LOG_STREAM`,
  `GALILEO_CONSOLE_URL`, `GALILEO_AGENT_CONTROL_ENABLED` (read by the SDK)
- schema/identifier values such as `guardrail_ids=["galileo_agent_control"]`, the
  OTel span name `galileo_agent_control_agent`, and `_LOCAL_EVALUATOR =
  "galileo.luna"` — Splunk dashboards and detectors key on these strings

Internal comments, docstrings, log messages, filenames, and docs still say
Galileo; that is deliberate, not an oversight.

## Versioning and releases

Semver, with the version in **two** places that must move in the same commit:
`app_version` in `backend/config.py` and `APP_VERSION` in `.env.example`. Leave
`app_name` (`"DemoBot v4"`) alone unless the MAJOR changes — it names the 4.x
line, and also appears in `run.sh`, `Containerfile`, and `requirements.txt`.

Releases are annotated `vX.Y.Z` tags cut from `main` **after** the PR merges,
then published with `gh release create`. Never tag a feature branch. Full
process, including why a deployed box can still report a stale version:
`docs/RELEASING.md`.

## Synthetic Content toggles

The four **Synthetic Content** controls (Include Synthetic PII/PHI, Include
Toxic Content, Include Hallucinated Content, Prescriptive Overreach) work by
asking the user-facing model to produce the content **itself**, inside its
answer (`backend/agents/nodes/injection.py`). Two rules:

- **Never add a deterministic / canned fallback.** Text stitched onto the reply
  after the LLM call is not what the guardrails and evals are scoring, so it
  fails to trigger them and misrepresents the demo. If the model declines a
  directive, the turn reports that category as not delivered (`*_detected`
  false) and that is the correct outcome. The only lever is the directive
  itself: its wording, where it lands in the theme's answer contract, and the
  permission framing for censored providers. Probe any wording change live
  (`tests/manual/probe_directives.py`, `probe_aidefense.py`) rather than
  papering over a refusal with canned text. The `_inject_*` / `_integrate_*`
  helpers on `RecommendationEngine` belong to the legacy non-agentic engine
  only; do not call them from the agentic chain.
- **Never label the content.** No "synthetic sample" banner, no `X SAMPLE:`
  prefix, no "(fictional)" tag, no closing disclaimer, no placeholder values
  (John Doe, 123-45-6789, example.com). The directives forbid that vocabulary,
  `strip_sample_labels` / `realize_pii_placeholders` clean up a slip, and the
  content goes where it belongs (record line opens the assessment, abuse in the
  assessment and first guidance item, fabrications as the opening guidance
  items, overreach as a guidance item; `reply` for the conversational theme).

`tests/test_synthetic_content.py` pins both rules; keep it in `tests/run_all.sh`.
