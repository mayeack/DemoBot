#!/usr/bin/env node
/**
 * Selftest for guard-core.mjs — no OpenClaw SDK required, just `node`.
 * Exercises allow / block / timeout / fail policy against a stubbed fetch, and
 * the after_tool_call observer (reports only sandbox policy denials).
 * Invoked by tests/observability/verify_openclaw_observability.sh (Tier 0).
 *
 * It runs from the image, not the working tree: openclaw/ is sparse-excluded
 * on the Mac (AMP deletes it), and the plugin is baked in at /opt.
 *
 *   podman run --rm demobot-openclaw node /opt/demobot-plugins/demobot-toolguard/selftest.mjs
 */
import {
  agentSurface,
  decideFromGuardResponse,
  inspectToolCall,
  looksPolicyDenied,
  observeToolResult,
} from "./guard-core.mjs";

let failures = 0;
function check(name, cond, detail = "") {
  const status = cond ? "PASS" : "FAIL";
  console.log(`[${status}] ${name}${cond ? "" : ` :: ${detail}`}`);
  if (!cond) failures += 1;
}

const EVENT = {
  toolName: "web_fetch",
  params: { url: "https://evil.example.com/collect" },
  toolCallId: "call-1",
};
const CTX = { sessionKey: "webchat:test" };

// --- decideFromGuardResponse (pure) ---
check("200 allow -> no decision",
  decideFromGuardResponse(200, { block: false, decision: "allow" }, false) === undefined);
const blocked = decideFromGuardResponse(200, { block: true, reason: "unapproved egress host" }, false);
check("200 block -> block with reason",
  blocked?.block === true && blocked.blockReason.includes("unapproved egress host"),
  JSON.stringify(blocked));
check("500 fail-closed -> block",
  decideFromGuardResponse(500, undefined, false)?.block === true);
check("500 fail-open -> no decision",
  decideFromGuardResponse(500, undefined, true) === undefined);
check("network error fail-closed -> block",
  decideFromGuardResponse(undefined, undefined, false)?.block === true);

// --- inspectToolCall against stubbed fetch ---
const okFetch = async () => ({ status: 200, json: async () => ({ block: false }) });
const blockFetch = async () => ({
  status: 200,
  json: async () => ({ block: true, reason: "PHI in arguments" }),
});
const brokenFetch = async () => { throw new TypeError("fetch failed"); };
const slowFetch = (signal) => (url, opts) =>
  new Promise((_, reject) => {
    opts.signal.addEventListener("abort", () =>
      reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  });

const cfg = { guardUrl: "http://127.0.0.1:8001", accessKey: "k", timeoutMs: 200 };

check("inspect: allow path -> undefined",
  (await inspectToolCall(EVENT, CTX, cfg, { fetch: okFetch })) === undefined);
const b = await inspectToolCall(EVENT, CTX, cfg, { fetch: blockFetch });
check("inspect: block path carries governance reason",
  b?.block === true && b.blockReason.includes("PHI in arguments"), JSON.stringify(b));
check("inspect: network error fail-closed -> block",
  (await inspectToolCall(EVENT, CTX, cfg, { fetch: brokenFetch }))?.block === true);
check("inspect: network error fail-open -> undefined",
  (await inspectToolCall(EVENT, CTX, { ...cfg, failOpen: true }, { fetch: brokenFetch })) === undefined);
const t = await inspectToolCall(EVENT, CTX, cfg, { fetch: slowFetch() });
check("inspect: timeout fail-closed -> block", t?.block === true, JSON.stringify(t));
check("inspect: missing guardUrl fail-closed -> block",
  (await inspectToolCall(EVENT, CTX, { accessKey: "k" }, { fetch: okFetch }))?.block === true);
check("inspect: never throws on garbage event",
  (await inspectToolCall(null, null, cfg, { fetch: okFetch })) === undefined);

// --- attribution + after_tool_call observer ---
check("agentSurface defaults to openclaw", agentSurface({}) === "openclaw");
check("agentSurface honors nemoclaw", agentSurface({ agentSurface: "nemoclaw" }) === "nemoclaw");
let seen = [];
const captureFetch = async (url, opts) => { seen.push({ url, body: JSON.parse(opts.body) }); return { status: 200, json: async () => ({ attributed: true }) }; };
check("looksPolicyDenied recognizes OpenShell's denial",
  looksPolicyDenied('{"error":"policy_denied","detail":"POST /x not permitted by policy"}') && !looksPolicyDenied("ECONNRESET"));
const denied = { ...EVENT, error: '{"error":"policy_denied","detail":"POST /collect not permitted by policy"}', durationMs: 12 };
check("observe: sandbox denial is reported to /api/toolguard/observe",
  (await observeToolResult(denied, CTX, { ...cfg, agentSurface: "nemoclaw" }, { fetch: captureFetch })) === true
  && seen[0]?.url.endsWith("/api/toolguard/observe") && seen[0]?.body.agent_surface === "nemoclaw"
  && seen[0]?.body.tool_name === "web_fetch", JSON.stringify(seen));
seen = [];
check("observe: an ordinary result is NOT reported",
  (await observeToolResult({ ...EVENT, result: "ok" }, CTX, cfg, { fetch: captureFetch })) === false && seen.length === 0);
check("observe: never throws on garbage event / broken fetch",
  (await observeToolResult(null, null, cfg, { fetch: brokenFetch })) === false
  && (await observeToolResult(denied, CTX, cfg, { fetch: brokenFetch })) === false);

console.log();
if (failures) {
  console.log(`FAILED (${failures})`);
  process.exit(1);
}
console.log("All demobot-toolguard selftests passed.");
