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

Color is reserved for **state**, never identity. These may stay colored:

- the status pill (`#…Status`) — its ON color is set in `frontend/js/chat.js` and
  may be semantic (green = injection enabled, red = destructive, amber = campaign
  running). The OFF state is always `bg-gray-100 text-gray-600`.
- live counters/timers beside the pill (`#autoPromptStats`, `#incidentRemaining`,
  `#sprayRemaining`)
- the toggle's own `peer-checked:bg-*` / `peer-focus:ring-*` accent

Dark mode is handled centrally for the neutral classes (`html.dark .bg-gray-50`,
`.border-gray-200`, `.text-gray-700`, `.text-gray-500`), so a card that follows
this format needs no dark-mode rule of its own. A card that invents its own
palette does — which is another reason not to.

## Product naming in user-visible text

User-visible text says **"Cisco Agent Observability"**, never "Galileo". This
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
