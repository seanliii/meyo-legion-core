/** OhMyGame's native API, not a game agent or a replacement for its Pi loop. */
import { readFile, lstat, realpath, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export function validateRuntime(value) {
  if (!value || typeof value.token !== "string" || !value.token ||
      value.token.length > 512 || /[\r\n\0]/.test(value.token)) throw new Error("Invalid native connection");
  const url = new URL(value.daemonUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname) ||
      !url.port || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("Native connection must be an exact loopback origin");
  }
  return { daemonUrl: url.origin, token: value.token };
}

function id(value, name) {
  if (typeof value !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(value)) {
    throw new Error(`Invalid ${name}`);
  }
  return value;
}

export async function nativeAction(request, action, input = {}, onProgress = async () => {}) {
  if (action === "status") {
    const health = await request("/health");
    const catalog = await request("/models");
    return { ok: true, status: "desktop_connected", health, model_count: catalog.models?.length ?? 0,
      needs_model_setup: !catalog.models?.length, default_model: catalog.defaultModel ?? null };
  }
  if (action === "models") return { ok: true, native: await request("/models") };
  if (action === "projects") return { ok: true, native: await request("/projects") };
  if (action === "inspect") {
    const projectId = id(input.projectId, "projectId");
    return { ok: true, status: "observed", projectId,
      native: { project: await request(`/projects/${projectId}`),
        conversations: await request(`/projects/${projectId}/conversations`) } };
  }
  if (action === "create") {
    const body = {};
    for (const key of ["name", "type", "workspacePath"]) if (input[key] !== undefined) body[key] = input[key];
    if (body.workspacePath && !path.isAbsolute(body.workspacePath)) throw new Error("workspacePath must be absolute");
    await onProgress({ stage: "project_creation", submission: "not_sent", request_outcome: "unknown" });
    const project = await request("/projects", "POST", body);
    await onProgress({ stage: "project_known", projectId: project.id, request_outcome: "received" });
    return { ok: true, status: "project_created", native: project };
  }
  if (["result", "cancel"].includes(action)) {
    const projectId = id(input.projectId, "projectId");
    const conversationId = id(input.conversationId, "conversationId");
    const prefix = `/projects/${projectId}/conversations/${conversationId}`;
    if (action === "result") {
      return { ok: true, status: "observed", projectId, conversationId, native: await request(prefix) };
    }
    const turnId = id(input.turnId, "turnId");
    await onProgress({ stage: "cancel_request", projectId, conversationId, turnId, request_outcome: "unknown" });
    const cancelled = await request(`${prefix}/turns/${turnId}/cancel`, "POST", {});
    await onProgress({ stage: "cancel_reply", request_outcome: "received" });
    return { ok: true, status: "cancel_requested", projectId, conversationId, turnId,
      native: cancelled };
  }
  if (!["delegate", "continue"].includes(action)) throw new Error("Unsupported native action");
  if (typeof input.prompt !== "string" || !input.prompt.trim()) throw new Error("prompt is required");
  const known = {};
  if (input.projectId) known.projectId = id(input.projectId, "projectId");
  if (input.conversationId) known.conversationId = id(input.conversationId, "conversationId");
  await onProgress({ ...known, stage: "model_check", submission: "not_sent" });
  const models = await request("/models");
  if (!models.models?.length) {
    return { ok: false, status: "needs_model", submitted: false,
      message: "OhMyGame 尚未连接语言模型。请在其原生 Settings → Providers & Models 中连接；未创建或提交模型任务。" };
  }
  let projectId = input.projectId;
  let conversationId = input.conversationId;
  if (action === "continue") {
    id(projectId, "projectId");
    id(conversationId, "conversationId");
  }
  if (projectId) {
    id(projectId, "projectId");
    await request(`/projects/${projectId}`);
  } else {
    if (input.workspacePath && !path.isAbsolute(input.workspacePath)) throw new Error("workspacePath must be absolute");
    let workspace;
    if (input.workspacePath) {
      const entry = await lstat(input.workspacePath);
      if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error("workspacePath must be a directory, not a symbolic link");
      workspace = await realpath(input.workspacePath);
    }
    const projects = await request("/projects");
    const existing = workspace && projects.find((project) => path.resolve(project.workspacePath) === workspace);
    if (existing) projectId = existing.id;
    else {
      await onProgress({ stage: "project_creation", submission: "not_sent", request_outcome: "unknown" });
      const created = await nativeAction(request, "create", input);
      projectId = created.native.id;
    }
  }
  id(projectId, "projectId");
  await onProgress({ stage: "project_known", projectId, submission: "not_sent", request_outcome: "received" });
  if (!conversationId) {
    const body = {};
    for (const key of ["model", "reasoningLevel"]) if (input[key] !== undefined) body[key] = input[key];
    await onProgress({ stage: "conversation_creation", projectId, submission: "not_sent", request_outcome: "unknown" });
    conversationId = (await request(`/projects/${projectId}/conversations`, "POST", body)).id;
  }
  id(conversationId, "conversationId");
  const body = { prompt: input.prompt };
  for (const key of ["references", "images", "attachments", "mode", "mentions", "contexts"]) {
    if (input[key] !== undefined) body[key] = input[key];
  }
  let reply;
  await onProgress({ stage: "turn_submission", projectId, conversationId, submission: "unknown" });
  try {
    reply = await request(`/projects/${projectId}/conversations/${conversationId}/turns`, "POST", body);
    id(reply.turnId, "turnId");
  } catch (error) {
    error.nativeContext = { projectId, conversationId,
      stage: "turn_submission", submission: error.requestSent === false ? "not_sent" : "unknown",
      ...(error.httpStatus ? { http_status: error.httpStatus } : {}),
      next: "Read the same native conversation before retrying; no automatic resubmission." };
    await onProgress(error.nativeContext);
    throw error;
  }
  await onProgress({ stage: "turn_reply", projectId, conversationId, turnId: reply.turnId, submission: "accepted" });
  return { ok: true, status: "accepted", completed: false, projectId, conversationId,
    turnId: reply.turnId, queued: reply.queued === true,
    next: "Use result with these native IDs; accepted is not completion." };
}

async function cdpRuntime(socketURL) {
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(socketURL);
    const timer = setTimeout(() => { socket.close(); reject(new Error("Native renderer connection timed out")); }, 5000);
    socket.addEventListener("open", () => socket.send(JSON.stringify({
      id: 1, method: "Runtime.evaluate", params: {
        expression: "(async () => { const desktop = window.ohMyGameDesktop; if (!desktop) return null; const state = await desktop.runtimeService.state(); return state.status === 'ready' && state.runtime ? {daemonUrl: state.runtime.url, token: state.runtime.token} : null; })()",
        returnByValue: true, awaitPromise: true,
      },
    })));
    socket.addEventListener("message", ({ data }) => {
      let reply;
      try { reply = JSON.parse(data); } catch { return; }
      if (reply.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      if (reply.error || reply.result?.exceptionDetails) reject(new Error("Native renderer is not ready"));
      else resolve(reply.result?.result?.value ?? null);
    });
    socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("Native renderer connection failed")); });
  });
}

export async function connectRuntime(port, appPath, {
  fetcher = fetch, readRuntime = cdpRuntime,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)), timeoutMs = 20000,
} = {}) {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid local port");
  const origin = `http://127.0.0.1:${port}`;
  const expected = path.join(appPath, "Contents/Resources/app.asar/dist/renderer/index.html");
  const deadline = Date.now() + timeoutMs;
  // Chromium's port appears before Electron creates the window and its native
  // daemon finishes starting. These retries only read connection state; they
  // never resubmit a prompt or start another application.
  while (Date.now() < deadline) {
    const response = await fetcher(`${origin}/json/list`, {
      signal: AbortSignal.timeout(5000), redirect: "error",
    });
    if (!response.ok) throw new Error("Native inspector is not ready");
    const targets = await response.json();
    const target = targets.find((row) => {
      try { return row.type === "page" && fileURLToPath(new URL(row.url)) === expected; }
      catch { return false; }
    });
    if (target) {
      const socketURL = new URL(target.webSocketDebuggerUrl);
      if (socketURL.protocol !== "ws:" || socketURL.hostname !== "127.0.0.1" ||
          Number(socketURL.port) !== port || socketURL.username || socketURL.password) {
        throw new Error("Native inspector endpoint escaped its owned loopback port");
      }
      const runtime = await readRuntime(socketURL.href);
      if (runtime) return validateRuntime(runtime);
    }
    await wait(250);
  }
  throw new Error("The owned native renderer or daemon did not become ready");
}

async function main() {
  const [port, appPath, action, requestFile, receiptFile] = process.argv.slice(2);
  const deadline = Date.now() + 40000; // Leave the Python wrapper time to read the durable receipt.
  let input = {};
  if (requestFile) {
    const info = await lstat(requestFile);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 2_000_000 ||
        (process.getuid && info.uid !== process.getuid())) throw new Error("Invalid request file");
    try { input = JSON.parse(await readFile(requestFile, "utf8")); }
    catch { throw new Error("Request file is not valid JSON"); }
    if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("Request must be an object");
  }
  if (input.output_file && !["result", "models", "projects", "inspect", "status"].includes(action)) {
    throw new Error("output_file is for read results; mutation receipts are returned directly");
  }
  let progress = { action, stage: "connecting", submission: "not_sent" };
  if (receiptFile) {
    const expected = path.join(path.dirname(path.dirname(appPath)), "operator", "operations");
    const info = await lstat(receiptFile);
    if (path.dirname(receiptFile) !== expected || !/^[0-9a-f]{32}\.json$/.test(path.basename(receiptFile)) ||
        !info.isFile() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid())) {
      throw new Error("Invalid owned operation receipt");
    }
  }
  const persist = async (patch) => {
    progress = { ...progress, ...patch, observed_at: new Date().toISOString() };
    if (!receiptFile) return;
    const temporary = receiptFile + `.tmp-${process.pid}`;
    try {
      await writeFile(temporary, JSON.stringify(progress) + "\n", { flag: "wx", mode: 0o600 });
      await rename(temporary, receiptFile);
    } finally { await unlink(temporary).catch(() => {}); }
  };
  await persist({});
  // Read only the runtime object from our own signed application's renderer.
  // The native token remains in memory and never enters stdout, argv or a file.
  const runtime = await connectRuntime(Number(port), appPath, { timeoutMs: Math.min(20000, deadline - Date.now()) });
  const request = async (relative, method = "GET", body) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      const error = new Error("Connector deadline reached before the next native request");
      error.requestSent = false;
      throw error;
    }
    let response;
    try {
      response = await fetch(runtime.daemonUrl + relative, {
        method, headers: { Authorization: `Bearer ${runtime.token}`, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), redirect: "error",
        signal: AbortSignal.timeout(Math.min(20000, remaining)),
      });
    } catch (error) {
      error.requestSent = method !== "GET";
      throw error;
    }
    const raw = await response.text();
    let value;
    try { value = raw ? JSON.parse(raw) : null; } catch { throw new Error("Native API returned invalid JSON"); }
    if (!response.ok) {
      const error = new Error(`Native HTTP ${response.status}: ${String(value?.error ?? "request rejected").slice(0, 300)}`);
      error.httpStatus = response.status;
      throw error;
    }
    return value;
  };
  let result;
  try { result = await nativeAction(request, action, input, persist); }
  catch (error) {
    error.nativeContext ??= progress;
    await persist(error.nativeContext);
    throw error;
  }
  const serialized = JSON.stringify(result);
  if (input.output_file) {
    if (!path.isAbsolute(input.output_file)) throw new Error("output_file must be an absolute new file path");
    await writeFile(input.output_file, serialized + "\n", { flag: "wx", mode: 0o600 });
    console.log(JSON.stringify({ ok: result.ok, status: result.status, result_file: input.output_file,
      projectId: result.projectId, conversationId: result.conversationId,
      native_agent: result.native?.agent, bytes: Buffer.byteLength(serialized) }));
  } else if (serialized.length > 9000) {
    console.log(JSON.stringify({ ok: false, status: "output_file_required",
      message: "Native payload is larger than the shared CLI response budget; repeat this read with output_file to preserve it in full." }));
    process.exitCode = 2;
    return;
  } else console.log(serialized);
  process.exitCode = result.ok ? 0 : 2;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.log(JSON.stringify({ ok: false, status: "native_error", error: error.message,
      ...(error.nativeContext ? { native: error.nativeContext } : {}) }));
    process.exitCode = 1;
  });
}
