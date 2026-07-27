/**
 * demobot-toolguard — OpenClaw plugin registering the before_tool_call seat.
 *
 * Every proposed agent tool call is POSTed to DemoBot's
 * /api/toolguard/inspect BEFORE execution; the endpoint runs the deterministic
 * tool policy + Cisco AI Defense, logs the governance event, emits the
 * execute_tool GenAI span, and answers allow/block. This plugin only carries
 * the verdict back to the hook runner ({block, blockReason} or no decision).
 *
 * All calls are submitted — including benign reads — because the server
 * short-circuits benign tools locally and the allowed-call telemetry is half
 * the demo. Decision logic lives in guard-core.mjs (SDK-free, host-testable).
 */
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { inspectToolCall } from "./guard-core.mjs";

export default definePluginEntry({
  id: "demobot-toolguard",
  name: "DemoBot Tool Guard",
  description:
    "Submits every agent tool call to DemoBot governance (tool policy + " +
    "Cisco AI Defense) before execution; blocks on verdict.",
  register(api) {
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const config = event?.context?.pluginConfig ?? {};
        const log = (msg) => {
          try {
            api.logger?.info?.(`demobot-toolguard: ${msg}`);
          } catch {
            /* logging must never break the hook */
          }
        };
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
  },
});
