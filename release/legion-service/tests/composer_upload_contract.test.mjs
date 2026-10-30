import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// 沿用 agentbay_mobile_panel.test.mjs：执行真实页面函数，只替换 DOM/传输边界。
// 不启动浏览器、HTTP 服务或模型；所有文件均为 Node 内存中的真实 File。
const html = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");
function pageFunction(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start >= 0, `缺少真实页面函数 ${name}`);
  for (let end = html.indexOf("}", start); end >= 0; end = html.indexOf("}", end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw new Error(`页面函数不完整：${name}`);
}
const functions = new vm.Script(
  ["esc", "renderUploads", "uploadFile", "_clearPersonalView", "currentQuerySource", "send"]
    .map(pageFunction).join("\n"),
);
const TEXT = "value\n42\n";
const file = () => new File([TEXT], "input.csv", { type: "text/csv" });
// server._handle_upload 的实际响应形状；path 只作为返回值，不在磁盘创建该路径。
const ENTRY = {
  name: "input.csv", size: Buffer.byteLength(TEXT), kind: "spreadsheet",
  path: "/__ut_uploads/owner/server.csv", url: "/media/personal/__ut_owner/server.csv",
};
const SUCCESS = { ok: true, files: [ENTRY] };
const flush = () => new Promise(setImmediate);
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const unescape = (value) => value.replaceAll("&lt;", "<").replaceAll("&gt;", ">")
  .replaceAll("&quot;", '"').replaceAll("&amp;", "&");

class Element {
  constructor() {
    Object.assign(this, { style: {}, dataset: {}, value: "", textContent: "",
      disabled: false, isConnected: true, scrollHeight: 0, controls: [] });
  }
  set innerHTML(value) {
    this.markup = String(value);
    this.textContent = unescape(this.markup.replace(/<[^>]*>/g, ""));
    this.controls = [...this.markup.matchAll(/\bdata-i="([^"]*)"/g)].map((match) => {
      const control = new Element();
      control.dataset.i = match[1];
      return control;
    });
  }
  get innerHTML() { return this.markup || ""; }
  querySelectorAll(selector) { return selector === "[data-i]" ? this.controls : []; }
  replaceChildren() { this.innerHTML = ""; }
  click() { if (!this.disabled) return this.onclick?.({ preventDefault() {} }); }
  focus() { this.focused = true; }
}

function fixture({ payload = SUCCESS, status = 200, gateRead = false, gateResponse = false } = {}) {
  const nodes = new Map(["uplRow", "uplInput", "q", "send", "modeSelect", "sbList",
    "sbQuick", "chat", "advPanelBody"].map((id) => [id, new Element()]));
  const get = (id) => nodes.get(id) || null;
  get("modeSelect").value = "auto";
  const calls = [], notices = [], unexpected = [];
  const gate = deferred(), started = deferred();
  const messageParts = new Map([".progress", ".proc-detail", ".st", ".pt"]
    .map((selector) => [selector, new Element()]));
  const message = { isConnected: true, querySelector: (selector) => messageParts.get(selector) || null };
  class FileReader {
    readAsDataURL(value) {
      (async () => {
        if (gateRead) await gate.promise;
        this.result = `data:${value.type};base64,${Buffer.from(await value.arrayBuffer()).toString("base64")}`;
        this.onload?.({ target: this });
      })().catch((error) => this.onerror?.(error));
    }
  }
  let context;
  context = vm.createContext({
    FormData, File, Blob, FileReader, Response, Headers, URL, URLSearchParams,
    TextDecoder, AbortController, DOMException, setTimeout, clearTimeout,
    document: {
      title: "上传契约测试",
      querySelector: (selector) => selector.startsWith("#") ? get(selector.slice(1)) : null,
      getElementById: get, createElement: () => new Element(),
    },
    location: { href: "http://localhost/", origin: "http://localhost", pathname: "/", search: "" },
    $: (selector) => selector.startsWith("#") ? get(selector.slice(1)) : null,
    qEl: get("q"), uploads: [], running: false, clientUid: "__ut_owner",
    _personalSubject: "__ut_owner", _personalEpoch: 1, _personalSwitchPending: false,
    _personalRequests: new Set(), _resumeCtx: null, _syncTimer: null, _sessionView: 0,
    _teamReplay: new Set(), _origTitle: "上传契约测试", _NATIVE_RETURN_KEY: "__ut_return",
    curSession: "__ut_existing_session", liveTeamId: "", workspaceData: null,
    _squad: [], _SQUAD_AGENTS: [], _relayChain: [], querySourceConsumed: false,
    sessionStorage: { removeItem() {} },
    _readPersonalStatus: async () => context._personalSubject,
    _openPersonalAuth: () => notices.push("请验证个人身份"),
    _losePersonalIdentity: (text) => notices.push(text),
    toast: (text) => notices.push(String(text)),
    alert: (text) => notices.push(String(text)),
    newLegionMsg: () => message,
    ...Object.fromEntries(["autosize", "_savePersonalDraft", "_renderSquadPool",
      "_renderRelayEditor", "_closeAdvPanel", "_closeAdv", "addUserMsg", "loadSessions"]
      .map((name) => [name, () => {}])),
    fetch: async (target, options = {}) => {
      const path = new URL(target, "http://localhost/").pathname;
      const call = { path, ...options };
      calls.push(call);
      if (path === "/api/legion/upload") {
        started.resolve();
        if (gateResponse) await gate.promise;
        return new Response(JSON.stringify(payload), {
          status, headers: { "Content-Type": "application/json" },
        });
      }
      if (path === "/api/legion/dispatch")
        return new Response("", { headers: { "Content-Type": "text/event-stream" } });
      unexpected.push(path);
      throw new Error(`测试不允许的请求：${path}`);
    },
  });
  context.window = context;
  functions.runInContext(context);
  return {
    context, calls, get, notices, unexpected,
    release: () => gate.resolve(), started: started.promise,
    upload: (value = file()) => context.uploadFile(value),
    visible: () => [get("uplRow").textContent, ...notices].join("\n"),
    switchOwner() {
      context._clearPersonalView();
      context._personalSubject = "__ut_new_owner";
      context.clientUid = "__ut_new_owner";
    },
    seedCurrentChip() {
      context.uploads.push({ name: "keep.csv", orig_name: "keep.csv",
        kind: "spreadsheet", path: "/__ut_uploads/current/keep.csv" });
      context.renderUploads();
    },
  };
}
const uploadCalls = (ui) => ui.calls.filter((call) => call.path === "/api/legion/upload");
const dispatchCalls = (ui) => ui.calls.filter((call) => call.path === "/api/legion/dispatch");
const chipKeys = (ui) => Array.from(ui.context.uploads, (row) => row.path || row.orig_name || row.name);

test("真实 File 进入 FormData，Content-Type 由浏览器生成", { timeout: 3000 }, async () => {
  const ui = fixture(), input = file();
  await ui.upload(input);
  assert.equal(uploadCalls(ui).length, 1);
  const call = uploadCalls(ui)[0];
  assert.equal(call.method, "POST");
  assert.ok(call.body instanceof FormData, "上传 body 必须是 FormData，而不是 filename/data_base64 JSON");
  assert.equal(new Headers(call.headers).has("content-type"), false, "不能手写 multipart boundary 或 JSON Content-Type");
  const files = Array.from(call.body.values()).filter((value) => value instanceof File);
  assert.equal(files.length, 1);
  assert.equal(files[0].name, input.name);
  assert.equal(files[0].type, input.type);
  assert.equal(await files[0].text(), TEXT);
});

test("读取 files[0]，将服务端返回的精确 path 放入 uploads", { timeout: 3000 }, async () => {
  const ui = fixture();
  await ui.upload();
  assert.equal(ui.context.uploads.length, 1);
  const uploaded = ui.context.uploads[0];
  assert.equal(uploaded.path, ENTRY.path, "不能把 {ok,files:[...]} 当成单个文件对象");
  assert.equal(uploaded.url, ENTRY.url);
  assert.equal(uploaded.kind, ENTRY.kind);
  assert.match(ui.get("uplRow").textContent, /input\.csv/);
});

test("HTTP 400 与文件级 error 均可见，且都不生成成功 path", { timeout: 3000 }, async () => {
  const cases = [
    { label: "HTTP400", status: 400, payload: { ok: false, error: "Content-Type 必须是 multipart/form-data" } },
    { label: "file-error", status: 200, payload: {
      ok: true, files: [{ name: "input.csv", size: 0, error: "ValueError: 空文件" }],
    } },
  ];
  const observations = [];
  for (const entry of cases) {
    const ui = fixture(entry);
    await ui.upload();
    const error = entry.payload.error || entry.payload.files[0].error;
    const visible = ui.visible();
    observations.push({
      case: entry.label,
      visibleError: visible.includes(error) || /失败|错误|出错|failed|error/i.test(visible),
      paths: Array.from(ui.context.uploads, (row) => row.path).filter(Boolean),
    });
  }
  assert.deepEqual(observations, cases.map((entry) => ({
    case: entry.label, visibleError: true, paths: [],
  })));
});

test("切换身份后，旧文件读取或旧上传的迟到结果不能进入当前 uploads", { timeout: 3000 }, async () => {
  const ui = fixture({ gateRead: true, gateResponse: true });
  const pending = ui.upload();
  await flush();
  ui.switchOwner();
  ui.seedCurrentChip();
  ui.release();
  await pending;
  assert.deepEqual(chipKeys(ui), ["/__ut_uploads/current/keep.csv"]);
  assert.equal(ui.context._personalSubject, "__ut_new_owner");
});

test("真实 chip 移除后，迟到上传不能恢复它或覆盖占用原索引的新 chip", { timeout: 3000 }, async () => {
  const ui = fixture({ gateResponse: true });
  const pending = ui.upload();
  await ui.started;
  try {
    const remove = ui.get("uplRow").querySelectorAll("[data-i]")[0];
    assert.ok(remove, "真实 renderUploads 应提供移除控件");
    remove.click();
    assert.equal(ui.context.uploads.length, 0);
    ui.seedCurrentChip();
  } finally { ui.release(); }
  await pending;
  assert.deepEqual(chipKeys(ui), ["/__ut_uploads/current/keep.csv"]);
  assert.doesNotMatch(ui.get("uplRow").textContent, /input\.csv/);
});

test("附件尚无 path 时，send 不得悄悄派发无附件任务", { timeout: 3000 }, async () => {
  const ui = fixture({ gateResponse: true });
  const uploading = ui.upload();
  await ui.started;
  ui.get("q").value = "请使用这个附件继续原会话";
  const sending = ui.context.send();
  await flush();
  const sentBeforeUpload = dispatchCalls(ui).slice();
  ui.release();
  await Promise.all([uploading, sending]);
  assert.deepEqual(ui.unexpected, [], "不能靠缺少传输替身而让发送提前失败");
  assert.equal(sentBeforeUpload.length, 0,
    "文件还没得到 path，已派发的 plan=" + JSON.stringify(
      sentBeforeUpload.map((call) => JSON.parse(call.body).plan)));
  // 允许窄修选择“阻止发送”或“等待上传”；若最终发送，必须携带真实返回的 path。
  for (const call of dispatchCalls(ui))
    assert.deepEqual(JSON.parse(call.body).plan.attachments, [ENTRY.path]);
});

test("原身份重新授权期间返回的上传应明确失败而非永久上传中", { timeout: 3000 }, async () => {
  const ui = fixture({ gateResponse: true });
  const pending = ui.upload();
  await ui.started;
  ui.context._personalSwitchPending = true;
  ui.release();
  await pending;
  // 授权失败或仍是原账号：subject/epoch 和现有 chip 都保持不变。
  ui.context._personalSwitchPending = false;
  assert.equal(ui.context.uploads.length, 1);
  assert.equal(ui.context.uploads[0].uploading, false);
  assert.ok(ui.context.uploads[0].error);
  assert.equal(ui.context.uploads[0].path, undefined);
  assert.match(ui.visible(), /失败|未确认|重新上传/);
});
