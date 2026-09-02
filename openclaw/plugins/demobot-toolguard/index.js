/**
 * demobot-toolguard — OpenClaw plugin registering DemoBot's governance seats.
 *
 * before_tool_call (gate): every proposed agent tool call is POSTed to
 * DemoBot's /api/toolguard/inspect BEFORE execution; the endpoint runs the
 * deterministic tool policy, the NemoClaw policy layer and Cisco AI Defense,
 * logs the governance event, emits the execute_tool GenAI span, and answers
 * allow/block. This plugin only carries the verdict back to the hook runner
 * ({block, blockReason} or no decision).
 *
 * after_tool_call (observe): a tool the sandbox refused (OpenShell's
 * policy_denied) is reported to /api/toolguard/observe so a NemoClaw runtime
 * denial is attributed in governance/telemetry immediately.
 *
 * All calls are submitted — including benign reads — because the server
 * short-circuits benign tools locally and the allowed-call telemetry is half
 * the demo. Decision logic lives in guard-core.mjs (SDK-free, host-testable).
 */
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { inspectToolCall, observeToolResult } from "./guard-core.mjs";

// The plugin config as seen by the gate hook; the observe hook's event may not
// carry it, so the last one wins there.
let lastConfig = {};

function makeLog(api) {
  return (msg) => {
    try {
      api.logger?.info?.(`demobot-toolguard: ${msg}`);
    } catch {
      /* logging must never break a hook */
    }
  };
}

export default definePluginEntry({
  id: "demobot-toolguard",
  name: "DemoBot Tool Guard",
  description:
    "Submits every agent tool call to DemoBot governance (tool policy + " +
    "NemoClaw policy + Cisco AI Defense) before execution; blocks on verdict; " +
    "reports sandbox policy denials after execution.",
  register(api) {
    const log = makeLog(api);
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const config = event?.context?.pluginConfig ?? ctx?.pluginConfig ?? lastConfig;
        if (config && typeof config === "object") lastConfig = config;
        try {
          return await inspectToolCall(event, ctx, config, { log });
        } catch (err) {
          // Belt & braces: inspectToolCall never throws, but if it somehow
          // does, apply the configured fail policy rather than leaving the
          // hook runner's throw semantics (unspecified) to decide.
          log(`unexpected error: ${err?.message ?? err}`);
          return config.failOpen === true
            ? undefined
            : { block: true, blockReason: "DemoBot tool guard errored (fail-closed)." };
        }
      },
      // High priority: governance runs before any other tool middleware.
      // Explicit timeoutMs keeps the hook budget above the HTTP timeout in
      // guard-core (8s default) so the plugin always returns its own decision.
      { priority: 100, timeoutMs: 10000 },
    );
    api.on(
      "after_tool_call",
      async (event, ctx) => {
        const config = event?.context?.pluginConfig ?? ctx?.pluginConfig ?? lastConfig;
        try {
          await observeToolResult(event, ctx, config, { log });
        } catch (err) {
          log(`observe error: ${err?.message ?? err}`); // observation only
        }
      },
      { priority: 50, timeoutMs: 10000 },
    );
  },
});
