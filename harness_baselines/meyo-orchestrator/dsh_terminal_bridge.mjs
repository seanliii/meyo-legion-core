/**
 * Request-local adapter for the installed, unmodified DSH headless driver.
 * The native driver still owns followup/whenIdle/final text/exit status.
 * This bridge observes public machine events and adapts its create inlet to
 * the public resume API when the caller supplies a bound native checkpoint.
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { realpathSync, renameSync, writeFileSync } from "node:fs";

export const name = "legion-dsh-terminal";
export const inject = ["agentDefaultModel", "agents", "sessions", "headlessStartup"];

export async function apply(ctx, config) {
  const requireNative = createRequire(config.nativeEntry);
  const native = await import(pathToFileURL(
    requireNative.resolve("@deepseek-ai/dsh-headless")).href);
  const agents = ctx.get("agents");
  const sessions = ctx.get("sessions");
  const record = {
    version: 1, nonce: config.nonce, request_id: config.requestId,
    binding: config.binding, session_id: "", first_seq: null,
    turn_id: null, terminal_seq: null, stop_reason: "unknown",
    error_code: "", cancel_cause: "", usage: {}, flushed: false, resumed: false,
  };
  const publish = () => {
    const pending = config.output + ".tmp";
    writeFileSync(pending, JSON.stringify(record), { mode: 0o600 });
    renameSync(pending, config.output);
  };
  let created = false;
  const inlet = {
    async create(options) {
      if (created) throw new Error("dsh_headless_duplicate_create");
      created = true;
      const expected = config.resumeSessionId || options.sessionId;
      const setup = async (agentCtx) => {
        await options.setup?.(agentCtx);
        // Agent-scoped subscription plus exact session identity: not a global
        // transcript listener. Constructor/replay seeds are never inspected.
        agentCtx.on("session/event", (session, event) => {
          if (session.id !== expected || session.header.cwd !== config.binding.workdir) return;
          if (event.type === "turn/start") {
            record.session_id = session.id;
            record.first_seq = event.seq;
            record.turn_id = event.data.turn;
            record.terminal_seq = null;
            record.stop_reason = "unknown";
            record.error_code = "";
            record.cancel_cause = "";
            record.usage = {};
            record.flushed = false;
            publish();
          } else if (event.type === "assistant/message" && event.data.turn === record.turn_id) {
            // Accounting is a separate field of this event. Never access its
            // message, content, reasoning, tool arguments or raw chunks.
            let receivedUsage = false;
            for (const field of ["inputTokens", "outputTokens", "cacheReadTokens",
                                 "cacheWriteTokens", "reasoningTokens"]) {
              const value = event.data.usage?.[field];
              if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
                record.usage[field] = (record.usage[field] || 0) + value;
                receivedUsage = true;
              }
            }
            // Known spend can precede a tool wait/interruption. Publish it now,
            // independently of turn/end and the real persistence barrier.
            if (receivedUsage) publish();
          } else if (event.type === "turn/end" && event.data.turn === record.turn_id) {
            const reason = event.data.reason;
            const known = ["completed", "max-tokens", "error", "aborted", "blocked", "interrupted"];
            record.stop_reason = known.includes(reason?.kind) ? reason.kind : "unknown";
            record.terminal_seq = event.seq;
            if (reason?.kind === "error" && /^[A-Z][A-Z0-9_]{0,79}$/.test(reason.error?.code || ""))
              record.error_code = reason.error.code;
            if (reason?.kind === "aborted" &&
                ["user", "parent", "hook", "disposed", "legacy"].includes(reason.reason?.kind))
              record.cancel_cause = reason.reason.kind;
            publish();
          }
        });
      };
      // Do not rewrite a recorded UUID or fall back to create on resume failure.
      // The unused new-id argument belongs to native headless, not to the receipt.
      const handle = config.resumeSessionId
        ? await agents.resume({
            resumeSessionId: config.resumeSessionId, agentOptions: options.agentOptions, setup,
          })
        : await agents.create({ ...options, setup });
      const session = handle.agent.session;
      if (session.id !== expected ||
          realpathSync(session.header.cwd) !== config.binding.workdir) {
        await handle.dispose();
        throw new Error("dsh_native_session_binding_mismatch");
      }
      record.session_id = session.id;
      record.resumed = Boolean(config.resumeSessionId);
      publish();
      return handle;
    },
  };
  const durable = {
    async flush(session) {
      await sessions.flush(session);
      // The real persistence barrier has returned, not merely been announced.
      if (session.id === record.session_id && record.terminal_seq !== null) {
        record.flushed = true;
        publish();
      }
    },
  };
  // A facade local to this driver invocation; the actual registry, native
  // plugins and subagents retain their original services.
  const driverCtx = new Proxy(ctx, {
    get(target, key) {
      if (key === "get")
        return (service) => service === "agents" ? inlet
          : service === "sessions" ? durable : ctx.get(service);
      return Reflect.get(target, key, target);
    },
  });
  native.apply(driverCtx, { task: ctx.get("headlessStartup").task });
}
