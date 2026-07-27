/**
 * demobot-toolguard core — pure decision logic, no OpenClaw SDK imports.
 *
 * Kept SDK-free so selftest.mjs can exercise it with plain `node` on the host
 * (the openclaw package only exists inside the gateway container). index.js
 * wires this into the before_tool_call hook.
 *
 * Contract with DemoBot (backend/routers/toolguard.py):
 *   POST {guardUrl}/api/toolguard/inspect  (HTTP Basic, password = access key)
 *   body: {tool_name, arguments, session_id, tool_call_id, agent_surface}
 *   200 -> {block: bool, decision, reason, rule_names, enforced, ...}
 *
 * Fail policy: on timeout / network error / non-200, honor `failOpen`:
 *   failOpen=false (default, mirrors DemoBot's ai_defense_fail_open) -> BLOCK
 *   failOpen=true -> allow (no decision).
 * The handler NEVER throws — an exception here would leave the hook runner's
 * behavior version-dependent, so every path resolves to a result object.
 */

/** Map the guard's HTTP outcome to a hook result. Pure, unit-tested. */
export function decideFromGuardResponse(status, body, failOpen) {
  if (status === 200 && body && typeof body === "object") {
    if (body.block === true) {
      const reason = body.reason || "policy violation";
      return {
        block: true,
        blockReason: `Blocked by DemoBot governance: ${reason}`,
      };
    }
    return undefined; // allow — no decision recorded
  }
  // Guard unreachable or errored: fail policy decides.
  if (failOpen) return undefined;
  return {
    block: true,
    blockReason:
      "DemoBot tool guard unavailable (fail-closed). " +
      `HTTP status: ${status ?? "network error"}.`,
  };
}

/** POST the proposed tool call to the guard and return a hook result. */
export async function inspectToolCall(event, ctx, config, deps = {}) {
  const fetchImpl = deps.fetch ?? globalThis.fetch;
  const log = deps.log ?? (() => {});
  const guardUrl = (config.guardUrl || "").replace(/\/+$/, "");
  const failOpen = config.failOpen === true;
  const timeoutMs = Number(config.timeoutMs) > 0 ? Number(config.timeoutMs) : 8000;

  if (!guardUrl) {
    // Misconfiguration is a startup problem, not a per-call surprise: treat it
    // like an unreachable guard so the fail policy stays the single knob.
    return decideFromGuardResponse(undefined, undefined, failOpen);
  }

  const payload = {
    tool_name: String(event?.toolName ?? ""),
    arguments: event?.params && typeof event.params === "object" ? event.params : {},
    session_id: ctx?.sessionKey || ctx?.sessionId || null,
    tool_call_id: event?.toolCallId || event?.runId || null,
    agent_surface: "openclaw",
  };

  const headers = { "Content-Type": "application/json" };
  if (config.accessKey) {
    const basic = Buffer.from(`x:${config.accessKey}`).toString("base64");
    headers.Authorization = `Basic ${basic}`;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(`${guardUrl}/api/toolguard/inspect`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    let body;
    try {
      body = await res.json();
    } catch {
      body = undefined;
    }
    const result = decideFromGuardResponse(res.status, body, failOpen);
    if (result?.block) {
      log(`blocked ${payload.tool_name}: ${result.blockReason}`);
    }
    return result;
  } catch (err) {
    log(`guard unreachable (${err?.name ?? "error"}); failOpen=${failOpen}`);
    return decideFromGuardResponse(undefined, undefined, failOpen);
  } finally {
    clearTimeout(timer);
  }
}
