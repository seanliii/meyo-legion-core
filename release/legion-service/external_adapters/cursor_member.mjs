#!/usr/bin/env node
// 军团成员：Cursor 代理（官方 @cursor/sdk，本地运行时）。
//
// 代理循环和文件读写在本机工作目录里进行，模型推理走 Cursor 托管的模型。
// 密钥只从环境变量 CURSOR_API_KEY 读取：不进命令行、不写入任何文件、不出现在输出里。
// 标准输出每行一个 JSON：工具起止、偶发的活动信号、最后一条 assistant 正文（带用量）。
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const emit = (record) => process.stdout.write(JSON.stringify(record) + "\n");

function parseArgs(argv) {
  const options = { task: "" };
  const names = { "--runtime-dir": "runtimeDir", "--workdir": "workdir", "--state-dir": "stateDir",
                  "--model": "model" };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (name === "--list-models") options.listModels = true;
    else if (names[name]) options[names[name]] = argv[index += 1] ?? "";
    else if (name === "--") { options.task = argv.slice(index + 1).join(" "); break; }
    else options.task = name;
  }
  return options;
}

const options = parseArgs(process.argv.slice(2));
const apiKey = (process.env.CURSOR_API_KEY || "").trim();
// 代理的 shell 工具继承本进程环境：密钥取出后，把所有等于它的环境变量都删掉。
for (const [name, value] of Object.entries(process.env)) {
  if (apiKey && value === apiKey) delete process.env[name];
}
delete process.env.CURSOR_API_KEY;

const redact = (text) => (apiKey ? String(text).split(apiKey).join("[redacted]") : String(text));
const brief = (value) => {
  if (value === undefined || value === null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return redact(text).slice(0, 500);
};

function fail(message, extra = {}, code = 1) {
  emit({ type: "error", error: { message: redact(message).slice(0, 1000), ...extra } });
  process.exitCode = code;
}

async function main() {
  if (!apiKey) return fail("CURSOR_API_KEY 未提供：这个成员需要绑定一把 Cursor API 密钥", { name: "credentials_missing" });
  if (!options.runtimeDir) return fail("缺少 --runtime-dir", { name: "bad_arguments" });
  let sdk;
  try {
    sdk = createRequire(path.join(path.resolve(options.runtimeDir), "package.json"))("@cursor/sdk");
  } catch (error) {
    return fail(`@cursor/sdk 未安装或无法加载：${error?.message || error}`, { name: "runtime_missing" });
  }
  const { Agent, Cursor, JsonlLocalAgentStore } = sdk;

  if (options.listModels) {
    const models = await Cursor.models.list({ apiKey });
    emit({ type: "models", models: models.map((model) => ({ id: model.id, name: model.displayName })) });
    return;
  }
  if (!options.model || !options.workdir || !options.stateDir || !options.task.trim()) {
    return fail("需要 --model、--workdir、--state-dir 和任务文本", { name: "bad_arguments" });
  }

  const stateDir = path.resolve(options.stateDir);
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const idFile = path.join(stateDir, "agent-id");
  const base = {
    apiKey,
    model: { id: options.model },
    // 只用这里给出的配置：不加载运行账号家目录或项目里的 Cursor 设置。
    local: { cwd: path.resolve(options.workdir), store: new JsonlLocalAgentStore(path.join(stateDir, "store")),
             sandboxOptions: { enabled: process.env.LEGION_CURSOR_SANDBOX === "1" } },
  };

  // 同一会话再次派单时接着上次的对话做（“继续”不从头来）；接不上就新建。
  let agent = null;
  let resumed = false;
  if (fs.existsSync(idFile)) {
    try {
      agent = await Agent.resume(fs.readFileSync(idFile, "utf8").trim(), base);
      resumed = true;
    } catch {
      agent = null;
    }
  }
  if (!agent) {
    agent = await Agent.create(base);
    fs.writeFileSync(idFile, String(agent.agentId), { mode: 0o600 });
  }

  let run = null;
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    try {
      if (run && run.supports("cancel")) await run.cancel();
    } catch { /* 取消失败时仍按被停止退出 */ }
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);

  try {
    run = await agent.send(options.task);
    emit({ type: "cursor_run", status: "started", model: options.model, resumed,
           agent_id: String(agent.agentId), run_id: String(run.id) });
    let lastActivity = 0;
    let streamed = "";
    for await (const event of run.stream()) {
      if (event.type === "tool_call") {
        const running = event.status === "running";
        emit({ t: running ? "tool_started" : "tool_end", protocol: "cli", tool: String(event.name || "tool"),
               call_id: String(event.call_id || ""),
               ...(running ? { status: "started", args_brief: brief(event.args) }
                           : { status: event.status === "error" ? "failed" : "succeeded",
                               ok: event.status !== "error", result_brief: brief(event.result) }) });
      } else if (event.type === "assistant" || event.type === "thinking") {
        if (event.type === "assistant") {
          for (const block of event.message?.content || []) {
            if (block.type === "text") streamed += block.text;
          }
        }
        // 逐段文本不是新步骤：最多每两秒报一次“还在写/还在想”。
        if (Date.now() - lastActivity >= 2000) {
          lastActivity = Date.now();
          emit({ type: "activity", status: event.type === "assistant" ? "writing" : "thinking" });
        }
      }
    }
    const result = await run.wait();
    if (result.status !== "finished") {
      const cancelled = result.status === "cancelled";
      return fail(result.error?.message || (cancelled ? "运行被停止" : "运行失败"),
                  { name: cancelled ? "cancelled" : (result.error?.code || "run_failed"),
                    run_id: String(result.id || run.id) }, cancelled ? 130 : 2);
    }
    const usage = result.usage || {};
    emit({ type: "message", model: options.model,
           message: { role: "assistant", content: [{ type: "text", text: redact(result.result || streamed) }] },
           ...(result.usage ? { usage: { input_tokens: usage.inputTokens, output_tokens: usage.outputTokens,
                                         cache_read_tokens: usage.cacheReadTokens,
                                         cache_write_tokens: usage.cacheWriteTokens,
                                         total_tokens: usage.totalTokens } } : {}) });
  } finally {
    try { await agent[Symbol.asyncDispose](); } catch { /* 释放失败不改变已得到的结果 */ }
  }
}

main().catch((error) => {
  // 抛出的 SDK 错误=没跑起来（密钥、配置、网络）；与“跑了但失败”分开报。
  fail(error?.message || String(error), {
    name: error?.code || error?.name || "startup_failed",
    ...(typeof error?.status === "number" ? { status: error.status } : {}),
    ...(typeof error?.isRetryable === "boolean" ? { retryable: error.isRetryable } : {}),
  });
}).finally(() => {
  // SDK 可能留有句柄；结果已经写出，按已定的退出码结束。
  process.stdout.write("", () => process.exit(process.exitCode ?? 0));
});
