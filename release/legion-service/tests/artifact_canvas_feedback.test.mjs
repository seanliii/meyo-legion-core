import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");
function fn(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  if (start < 0) return "";
  for (let end = html.indexOf("}", start); end >= 0; end = html.indexOf("}", end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw Error(name);
}

test("统一成果类型：网页、文字、图片、PDF、表格和媒体共享画布，模板只读", () => {
  const context = vm.createContext({});
  assert.ok(fn("_artifactCanvasKind"));
  vm.runInContext(fn("_artifactCanvasKind"), context);
  for (const [name, kind] of Object.entries({
    "index.html": "html", "index.template.html": "text", "report.md": "markdown",
    "photo.png": "img", "report.pdf": "pdf", "data.csv": "sheet",
    "movie.mp4": "video", "voice.mp3": "audio", "script.py": "text",
  })) assert.equal(context._artifactCanvasKind(name, ""), kind, name);
});

test("画布引用仅同源授权成果路径，不能接收凭据URL或外站", () => {
  const context = vm.createContext({ URL, location: { origin: "https://example.test", href: "https://example.test/" } });
  assert.ok(fn("_canvasArtifactUrl"));
  vm.runInContext(fn("_canvasArtifactUrl"), context);
  assert.equal(context._canvasArtifactUrl("/home-outputs/a/report.html"), "/home-outputs/a/report.html");
  for (const value of ["javascript:alert(1)", "//elsewhere.test/a",
    "https://other.test/home-outputs/a", "/api/legion/auth/session",
    "/home-outputs/a?token=secret", "/shared/sid/secret/home-outputs/a", "/home-outputs/a?%74icket=secret"]) {
    assert.equal(context._canvasArtifactUrl(value), "", value);
  }
});

test("反馈保留现有草稿，注明原会话、文件、文字或位置，不宣称已发送", () => {
  const context = vm.createContext({});
  assert.ok(fn("_canvasFeedbackText"));
  vm.runInContext(fn("_canvasFeedbackText"), context);
  const text = context._canvasFeedbackText({
    sid: "sid-a", name: "report.md", url: "/home-outputs/a/report.md",
    note: "把这段说清楚", quote: "引用的原文", point: { x: .25, y: .5 },
    screenshot: true,
  });
  assert.match(text, /sid-a/);
  assert.match(text, /\/home-outputs\/a\/report.md/);
  assert.match(text, /引用的原文/);
  assert.match(text, /25%.*50%/);
  assert.match(text, /把这段说清楚/);
  assert.match(text, /截图/);
  assert.doesNotMatch(text, /已发送|已完成修改/);
});

test("带回原对话不发送模型请求；跨会话草稿阻止静默搬移", async () => {
  const nodes = new Map(["#lbFeedback", "#lbStatus", "#lbReturn"].map(k => [k, { value: "修改这里", textContent: "", disabled: false }]));
  let recentClosed = 0;
  const ctx = vm.createContext({
    $: s => nodes.get(s), _canvasState: { owner: "alice", sid: "sid-a", url: "/home-outputs/a/file", name: "file", quote: "", epoch: 1 },
    _personalSubject: "alice", _personalEpoch: 1, _personalSwitchPending: false,
    curSession: "sid-b", running: false, qEl: { value: "当前未发送草稿", focus() {} }, uploads: [],
    _canvasFeedbackText: () => "成果反馈",
    _savePersonalDraft() {}, autosize() {}, closeLightbox() {}, toast() {},
    _closeAdvPanel() { recentClosed++; },
    openSession() { throw Error("不应切走有草稿的会话"); },
    fetch() { throw Error("不得调用模型或构造新请求"); },
  });
  assert.ok(fn("_returnCanvasFeedback"));
  vm.runInContext(fn("_returnCanvasFeedback"), ctx);
  await ctx._returnCanvasFeedback();
  assert.equal(ctx.qEl.value, "当前未发送草稿");
  assert.match(nodes.get("#lbStatus").textContent, /未发送/);
  ctx.curSession = "sid-a";
  await ctx._returnCanvasFeedback();
  assert.equal(ctx.qEl.value, "当前未发送草稿\n\n成果反馈");
  assert.equal(recentClosed, 1, "回到原对话时关闭最近产物浮层，输入框必须能操作");
});

test("截图上传失败保留引用与草稿，重试不会重复插入反馈或自动发送", async () => {
  const nodes = new Map(["#lbFeedback", "#lbStatus", "#lbReturn"]
    .map(k => [k, { value: "保留这份意见", textContent: "", disabled: false }]));
  const ctx = vm.createContext({
    $: s => nodes.get(s), _canvasState: {
      owner: "alice", sid: "sid-a", url: "/home-outputs/a/image.png", name: "image.png",
      quote: "", epoch: 1, file: { name: "screenshot.png" },
    },
    _personalSubject: "alice", _personalEpoch: 1, _personalSwitchPending: false,
    curSession: "sid-a", running: false, qEl: { value: "已有草稿", focus() {} }, uploads: [],
    _canvasFeedbackText: () => "唯一一份反馈", _savePersonalDraft() {}, autosize() {},
    closeLightbox() { throw Error("上传失败不能关闭反馈"); },
    uploadFile() { ctx.uploads.push({ error: "test failure" }); },
    fetch() { throw Error("禁止派单"); },
  });
  vm.runInContext(fn("_returnCanvasFeedback"), ctx);
  await ctx._returnCanvasFeedback();
  await ctx._returnCanvasFeedback();
  assert.equal(ctx.qEl.value, "已有草稿\n\n唯一一份反馈");
  assert.match(nodes.get("#lbStatus").textContent, /截图上传未成功/);
});

test("画布安全：不读取iframe DOM、不申请全屏捕获、保留opaque沙箱", () => {
  assert.ok(fn("_captureCanvasFrame"));
  assert.doesNotMatch(fn("_captureCanvasFrame"), /contentDocument|contentWindow|getDisplayMedia/);
  assert.match(fn("openLightbox"), /sandbox="allow-scripts allow-forms allow-popups"/);
  assert.doesNotMatch(fn("openLightbox"), /allow-same-origin/);
  assert.match(fn("openRecentArtifacts"), /openArtifactCanvas/);
  assert.match(fn("_renderSessionOverview"), /openArtifactCanvas/);
  assert.match(fn("hydrateArtifacts"), /openArtifactCanvas/);
});

test("截图等待底图解码完成，而不是只有naturalWidth就画空白图", async () => {
  const events = [], state = { owner: "alice" };
  const image = { tagName: "IMG", naturalWidth: 100, naturalHeight: 80,
    async decode() { await Promise.resolve(); events.push("decoded"); } };
  const canvas = {
    getContext() { return { drawImage() { assert.deepEqual(events, ["decoded"]); events.push("drawn"); } }; },
    toBlob(done) { done(new Blob(["png"], { type: "image/png" })); },
  };
  const context = vm.createContext({
    _canvasState: state, _personalSubject: "alice", File, Blob,
    $: selector => selector === "#lbContent" ? { querySelector: () => image } : { textContent: "" },
    document: { createElement: () => canvas },
    _setCanvasScreenshot() { events.push("attached"); },
  });
  vm.runInContext(fn("_captureCanvasFrame"), context);
  await context._captureCanvasFrame();
  assert.deepEqual(events, ["decoded", "drawn", "attached"]);
});

const snapshot = "b".repeat(64);
const artifactUrl = `/home-outputs/exports/${"a".repeat(64)}/${snapshot}/nested/index.html`;
const grant = {
  ok: true, preview_url: `/artifact-preview/__ut_cap${artifactUrl}`,
  preview_nonce: "__ut_nonce", observation_token: "__ut_observation_token", snapshot_id: snapshot,
};
const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function previewCanvas(fetcher = () => jsonResponse(grant)) {
  const listeners = new Set(), calls = [], nodes = new Map(), downloads = [], objectUrls = new Map(), revoked = [];
  const timers = new Map(); let nextTimer = 0, nextUrl = 0;
  class ParentURL extends URL {
    static createObjectURL(blob) { const url = `blob:parent/__ut_${++nextUrl}`; objectUrls.set(url, blob); return url; }
    static revokeObjectURL(url) { revoked.push(url); objectUrls.delete(url); }
  }
  function node(selector) {
    if (!nodes.has(selector)) {
      const classes = new Set();
      const n = {
        value: "", textContent: "", hidden: false, disabled: false, href: "", frame: null,
        classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
        focus() {}, removeAttribute(name) { delete this[name]; },
        setAttribute(name, value) { this[name] = value; },
        querySelector(name) { return name === "iframe" ? this.frame : null; },
      };
      Object.defineProperty(n, "innerHTML", {
        get() { return this.markup || ""; },
        set(value) {
          this.markup = value; this.frame = null;
          if (/<iframe\b/.test(value)) {
            this.frame = { contentWindow: {}, src: "", isConnected: true };
            const attrs = Object.fromEntries(Array.from(value.matchAll(/([\w-]+)="([^"]*)"/g), m => [m[1], m[2]]));
            this.frame.getAttribute = key => attrs[key] ?? null;
            if (attrs.src) this.frame.src = attrs.src;
          }
        },
      });
      nodes.set(selector, n);
    }
    return nodes.get(selector);
  }
  const ctx = vm.createContext({
    URL: ParentURL, Blob, ArrayBuffer, Request, Response, AbortController, DOMException,
    setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    location: { href: "https://meyo.test/", origin: "https://meyo.test" },
    $: node, document: { activeElement: { focus() {} }, querySelectorAll: () => [],
      body: { appendChild() {} },
      createElement(tag) {
        assert.equal(tag, "a");
        return { href: "", download: "", remove() {},
          click() { downloads.push({ href: this.href, name: this.download, blob: objectUrls.get(this.href) }); } };
      },
    },
    window: {
      addEventListener(type, listener) { assert.equal(type, "message"); listeners.add(listener); },
      removeEventListener(type, listener) { assert.equal(type, "message"); listeners.delete(listener); },
      open() { throw Error("不得静默打开不受控地址"); },
    },
    _canvasState: null, _canvasSeq: 0, _canvasDrafts: new Map(), _lightboxReturnFocus: null,
    _personalSubject: "alice", _personalEpoch: 1, _personalSwitchPending: false,
    curSession: "sid-a", _sessionView: 1, running: false, qEl: { value: "用户已有草稿", focus() {} }, uploads: [],
    _openPersonalAuth() {}, toast() {}, _savePersonalDraft() {}, autosize() {}, _closeAdvPanel() {},
    _captureCanvasFrame() {}, _captureCanvasSelection() {}, _setCanvasScreenshot() {},
    fetch(url, options) { calls.push({ url, options }); return Promise.resolve(fetcher(url, options)); },
  });
  const previewHelpers = Array.from(html.matchAll(/^(?:async )?function ((?:_canvasPreview|_renderCanvasPreview)\w*)\(/gm), m => m[1]);
  const names = ["esc", "_canvasArtifactUrl", "_canvasDraftKey", "_canvasFeedbackText",
    "_returnCanvasFeedback", ...previewHelpers, "openLightbox", "closeLightbox"];
  vm.runInContext([...new Set(names)].map(fn).join("\n"), ctx);
  const message = (overrides = {}) => ({
    type: "meyo-artifact-preview", nonce: grant.preview_nonce,
    observation: { kind: "network_error", message: "private raw error?key=secret",
      resource: "https://user:secret@cdn.test:8443/private?key=secret#fragment" },
    ...overrides,
  });
  async function emit(data = message(), overrides = {}) {
    const event = { data, source: node("#lbContent").frame?.contentWindow, origin: "null", ...overrides };
    await Promise.all([...listeners].map(listener => listener(event)));
  }
  return { ctx, calls, node, listeners, message, emit, downloads, objectUrls, revoked, timers };
}

const exportMessage = (data = {}) => ({
  type: "meyo-artifact-preview", nonce: grant.preview_nonce,
  download: { name: "state(1).json", blob: new Blob(['{"progress":7}'], { type: "text/html" }) }, ...data,
});

test("Blob转交只留本地待下载，父页真人点击才发起默认二进制下载", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit(exportMessage());
  assert.equal(h.node("#lbExport").hidden, false);
  assert.equal(h.node("#lbExportName").textContent, "state(1).json");
  assert.equal(h.objectUrls.size, 0); assert.equal(h.downloads.length, 0);
  assert.equal(h.calls.length, 1, "导出字节不得上传到 observation 或其他API");
  assert.equal(h.ctx._canvasState.preview.observations.size, 0);
  assert.equal(h.ctx.qEl.value, "用户已有草稿");
  assert.doesNotMatch(h.ctx._canvasFeedbackText(h.ctx._canvasState), /progress|state\(1\)/);
  assert.equal(typeof h.node("#lbExportDownload").onclick, "function");
  h.node("#lbExportDownload").onclick({ isTrusted: false });
  assert.equal(h.downloads.length, 0, "程序化点击不能代替父页真人点击");
  h.node("#lbExportDownload").onclick({ isTrusted: true });
  assert.equal(h.downloads.length, 1);
  assert.equal(h.downloads[0].name, "state(1).json");
  assert.equal(h.downloads[0].blob.type, "application/octet-stream");
  assert.equal(await h.downloads[0].blob.text(), '{"progress":7}');
  assert.match(h.node("#lbExportStatus").textContent, /浏览器/);
  assert.match(h.node("#lbExportStatus").textContent, /是否保存成功请查看/);
  assert.doesNotMatch(h.node("#lbExportStatus").textContent, /(?:^|[；。])(?:保存成功|已保存)/);
  assert.equal(h.node("#lbDl").href, `${artifactUrl}?download=1`, "原文件下载地址不变");
  assert.doesNotMatch(h.node("#lbContent").frame.getAttribute("sandbox"), /allow-downloads|allow-same-origin/);
});

test("ArrayBuffer和文件名只按字节与纯文字接收，不解析HTML或URL", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit(exportMessage({ download: {
    name: '../<img src=x onerror=alert(1)>.json', blob: new TextEncoder().encode("bytes").buffer,
  } }));
  assert.equal(h.node("#lbExport").hidden, false);
  assert.ok(h.node("#lbExportName").textContent.endsWith(".json"));
  assert.doesNotMatch(h.node("#lbExportName").textContent, /[\\/]/);
  assert.equal(h.node("#lbExportName").innerHTML, "", "文件名必须经textContent而不是innerHTML展示");
  h.node("#lbExportDownload").onclick({ isTrusted: true });
  assert.equal(await h.downloads[0].blob.text(), "bytes");
  for (const blob of ["https://private.test/x", "blob:null/fake", { size: 1 }, new Uint8Array([1])]) {
    await h.emit(exportMessage({ download: { name: "bad.json", blob } }));
  }
  assert.equal(h.calls.length, 1);
  assert.notEqual(h.node("#lbExportName").textContent, "bad.json");
});

test("Blob接收复用origin/source/nonce及owner/session/epoch/当前frame校验", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  const preview = h.ctx._canvasState.preview;
  const valid = exportMessage();
  await h.emit(valid, { source: {} });
  await h.emit(valid, { origin: "https://meyo.test" });
  await h.emit({ ...valid, nonce: "__ut_wrong" });
  await h.emit({ ...valid, type: "download-execute" });
  assert.ok(!h.ctx._canvasState.preview.download);
  for (const [key, value] of [["_personalSubject", "bob"], ["_personalEpoch", 2],
    ["_personalSwitchPending", true], ["curSession", "sid-other"], ["_sessionView", 2]]) {
    const old = h.ctx[key]; h.ctx[key] = value; await h.emit(valid); h.ctx[key] = old;
    assert.ok(!preview.download, key);
  }
  const oldSid = h.ctx._canvasState.sid; h.ctx._canvasState.sid = "sid-other";
  await h.emit(valid); h.ctx._canvasState.sid = oldSid;
  assert.ok(!preview.download);
  const oldFrame = h.node("#lbContent").frame;
  h.node("#lbContent").frame = { contentWindow: {} };
  await h.emit(valid, { source: oldFrame.contentWindow }); h.node("#lbContent").frame = oldFrame;
  assert.ok(!preview.download);
  assert.equal(h.downloads.length, 0); assert.equal(h.calls.length, 1);
});

test("Blob大小次数总量有界，丢弃超限文件并显示本地说明", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit(exportMessage({ download: { name: "too-big", blob: new Blob([new Uint8Array(8 * 1024 * 1024 + 1)]) } }));
  assert.ok(!h.ctx._canvasState.preview.download);
  assert.match(h.node("#lbExportStatus").textContent, /8\s*(?:MiB|MB)/);
  for (let i = 0; i < 12; i++) await h.emit(exportMessage({ download: { name: `${i}.json`, blob: new Blob(["x"]) } }));
  assert.equal(h.node("#lbExportName").textContent, "7.json", "只保留最近接收的一个文件，最多8次");
  assert.match(h.node("#lbExportStatus").textContent, /限额|上限/);
  assert.equal(h.objectUrls.size, 0);
  assert.equal(h.calls.length, 1);
  const other = previewCanvas();
  await other.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  const blob = new Blob([new Uint8Array(8 * 1024 * 1024)]);
  for (let i = 0; i < 3; i++) await other.emit(exportMessage({ download: { name: `${i}.bin`, blob } }));
  assert.equal(other.node("#lbExportName").textContent, "1.bin", "总转交不得超过16MiB");
});

test("关闭和新版本清理待下载字节及URL，排队旧消息与旧定时器不能串单", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "old.html", { sid: "sid-a" });
  await h.emit(exportMessage());
  const state = h.ctx._canvasState, listener = [...h.listeners][0], source = state.preview.frame.contentWindow;
  const oldButton = h.node("#lbExportDownload").onclick;
  assert.equal(typeof oldButton, "function");
  oldButton({ isTrusted: true });
  const url = h.downloads[0].href, oldTimers = [...h.timers.values()];
  h.ctx.closeLightbox();
  assert.equal(h.objectUrls.size, 0); assert.ok(h.revoked.includes(url));
  assert.equal(h.timers.size, 0); assert.equal(state.preview.download, null);
  assert.equal(h.node("#lbExport").hidden, true);
  await h.ctx.openLightbox("html", artifactUrl, "new.html", { sid: "sid-b" });
  await h.emit(exportMessage({ download: { name: "new.json", blob: new Blob(["new"]) } }));
  await listener({ origin: "null", source, data: exportMessage() });
  oldButton({ isTrusted: true }); oldTimers.forEach(fn => fn());
  assert.equal(h.downloads.length, 1);
  assert.equal(h.node("#lbExportName").textContent, "new.json");
  assert.equal(h.ctx._canvasState.preview.download.blob.size, 3);
});

test("换身份或会话后旧按钮丢弃字节，真实openSession入口立即清理", async () => {
  for (const [key, value] of [["_personalSubject", "bob"], ["_personalEpoch", 2],
    ["_personalSwitchPending", true], ["curSession", "sid-other"], ["_sessionView", 2]]) {
    const h = previewCanvas();
    await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
    await h.emit(exportMessage());
    const old = h.node("#lbExportDownload").onclick, preview = h.ctx._canvasState.preview;
    assert.equal(typeof old, "function");
    h.ctx[key] = value; old({ isTrusted: true });
    assert.equal(h.downloads.length, 0, key);
    assert.equal(preview.download, null, key);
    assert.equal(h.objectUrls.size, 0);
  }
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) : Promise.reject(Error("isolated stop")));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit(exportMessage());
  h.node("#lbExportDownload").onclick({ isTrusted: true });
  const preview = h.ctx._canvasState.preview;
  h.ctx._resumeCtx = null; h.ctx.sessionStorage = { removeItem() {} };
  vm.runInContext(fn("openSession"), h.ctx);
  await h.ctx.openSession("sid-other");
  assert.equal(preview.download, null); assert.equal(h.objectUrls.size, 0);
});

test("下载转交失败仅本地显示，不混入原单错误回流或覆盖反馈草稿", async () => {
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
    jsonResponse({ ok: true, report_id: "__ut_existing_report", team_id: "__ut_team" }));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit();
  const status = h.node("#lbStatus").textContent;
  await h.emit(exportMessage({ download: { error: "unavailable", message: "private bytes here" } }));
  assert.match(h.node("#lbExportStatus").textContent, /无法|未能/);
  assert.doesNotMatch(h.node("#lbExportStatus").textContent, /private/);
  assert.equal(h.node("#lbStatus").textContent, status);
  assert.equal(h.calls.length, 2, "下载数据不混入observation上报");
  assert.match(h.ctx._canvasFeedbackText(h.ctx._canvasState), /__ut_existing_report/);
  assert.equal(h.ctx.qEl.value, "用户已有草稿");
});

test("HTML画布和外开共用授权capability，原下载不变且仍为opaque", async () => {
  const h = previewCanvas();
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, "/api/legion/artifact/preview");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { sid: "sid-a", url: artifactUrl });
  assert.equal(h.calls[0].options.method, "POST");
  assert.equal(h.node("#lbContent").frame.src, grant.preview_url);
  assert.equal(h.node("#lbExternal").href, grant.preview_url);
  assert.equal(h.node("#lbDl").href, `${artifactUrl}?download=1`);
  assert.equal(h.node("#lbContent").frame.getAttribute("sandbox"), "allow-scripts allow-forms allow-popups");
  assert.equal(h.node("#lbContent").frame.getAttribute("referrerpolicy"), "no-referrer");
  assert.equal(h.listeners.size, 1);
  assert.match(h.node("#lbContext").textContent + h.node("#lbStatus").textContent, /静态.*后端/);
  assert.match(h.node("#lbContext").textContent + h.node("#lbStatus").textContent, /刷新.*不.*保留|不.*刷新.*持久/);
});

test("收到失败立刻可见；仅成功回执入原单反馈，仍保留用户输入且只准备草稿", async () => {
  const reply = deferred();
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) : reply.promise);
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  h.node("#lbFeedback").value = "用户自己的修改意见";
  const pending = h.emit();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].url, "/api/legion/artifact/preview-observation");
  const body = JSON.parse(h.calls[1].options.body);
  assert.equal(body.sid, "sid-a"); assert.equal(body.url, artifactUrl);
  assert.equal(body.observation_token, grant.observation_token);
  assert.equal(body.observation.kind, "network_error");
  assert.equal(body.observation.resource, "https://cdn.test/");
  assert.doesNotMatch(JSON.stringify(body.observation), /private|secret|key=|fragment|8443/);
  assert.match(h.node("#lbStatus").textContent, /失败/);
  assert.doesNotMatch(h.node("#lbStatus").textContent, /已记录原单/);
  assert.doesNotMatch(h.ctx._canvasFeedbackText(h.ctx._canvasState), /__ut_report/);
  reply.resolve(jsonResponse({ ok: true, report_id: "__ut_report_1", team_id: "__ut_team" }));
  await pending;
  assert.match(h.node("#lbStatus").textContent, /已记录原单/);
  const text = h.ctx._canvasFeedbackText(h.ctx._canvasState);
  assert.match(text, /__ut_report_1/); assert.match(text, /失败/);
  assert.doesNotMatch(text, /observation_token|__ut_cap|__ut_nonce|private|secret/);
  assert.equal(h.node("#lbFeedback").value, "用户自己的修改意见");
  assert.equal(h.ctx.qEl.value, "用户已有草稿");
  await h.ctx._returnCanvasFeedback();
  assert.match(h.ctx.qEl.value, /^用户已有草稿\n\n/);
  assert.match(h.ctx.qEl.value, /用户自己的修改意见/);
  assert.match(h.ctx.qEl.value, /__ut_report_1/);
  assert.equal(h.calls.length, 2, "返回原对话没有自动dispatch");
  assert.equal(h.listeners.size, 0, "关闭清理本次消息监听");
});

test("消息需同时匹配opaque来源、当前iframe、nonce和会话，忽略执行指令", async () => {
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
    jsonResponse({ ok: true, report_id: "__ut_report_safe", team_id: "__ut_team" }));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit(h.message(), { origin: "https://meyo.test" });
  await h.emit(h.message(), { source: {} });
  await h.emit(h.message({ nonce: "__ut_forged" }));
  await h.emit(h.message({ type: "execute", observation: { kind: "network_error" } }));
  await h.emit(h.message({ observation: { kind: "dispatch", code: "doBadThings()" } }));
  h.ctx._canvasState.sid = "sid-forged";
  await h.emit();
  h.ctx._canvasState.sid = "sid-a";
  h.ctx._personalSwitchPending = true; await h.emit(); h.ctx._personalSwitchPending = false;
  h.ctx._personalEpoch++; await h.emit(); h.ctx._personalEpoch--;
  h.ctx._personalSubject = "bob"; await h.emit(); h.ctx._personalSubject = "alice";
  assert.equal(h.calls.length, 1);
  await h.emit(h.message({ observation: {
    kind: "http_error", status: 503, message: '<img src=x onerror="dispatch()">api_key=secret',
    resource: "assets/app.js?token=secret#private", action: "dispatch",
  } }));
  assert.equal(h.calls.length, 2);
  const observation = JSON.parse(h.calls[1].options.body).observation;
  assert.equal(observation.kind, "http_error"); assert.equal(observation.status, 503);
  assert.equal(observation.resource, "assets/app.js");
  assert.match(observation.message, /503/);
  assert.doesNotMatch(JSON.stringify(observation), /<img|onerror|dispatch|api_key|token=|secret|private/);
  assert.equal(h.ctx.qEl.value, "用户已有草稿");
  await h.emit(h.message({ observation: { kind: "network_error",
    resource: "/api/generate/private-id?key=secret#private" } }));
  assert.equal(JSON.parse(h.calls[2].options.body).observation.resource, "/api/generate");
  assert.doesNotMatch(JSON.stringify(JSON.parse(h.calls[2].options.body).observation), /private|secret|key=/);
});

test("预览凭据被复制到资源文件名或主机名时不进入事实、状态和反馈草稿", async () => {
  const readToken = grant.preview_url.split("/")[2];
  for (const credential of [readToken, grant.preview_nonce, grant.observation_token,
    "apv1." + "a".repeat(32) + "." + "b".repeat(64)]) {
    for (const resource of [`assets/${credential}.js`, `https://${credential}.leak.test/file.js`]) {
      const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
        jsonResponse({ ok: true, report_id: "__ut_sanitized_report", team_id: "__ut_team" }));
      await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
      await h.emit(h.message({ observation: { kind: "resource_error", resource } }));
      const observation = JSON.parse(h.calls[1].options.body).observation;
      assert.equal(observation.resource, "", "不能只依赖后端再次脱敏");
      assert.ok(!h.node("#lbStatus").textContent.includes(credential));
      assert.ok(!h.ctx._canvasFeedbackText(h.ctx._canvasState).includes(credential));
      assert.equal(h.ctx.qEl.value, "用户已有草稿");
    }
  }
});

for (const [shape, credential] of Object.entries({
  sk: "sk-" + "z".repeat(24), "sk-proj": "sk-proj-" + "z".repeat(24),
  "sk-ant": "sk-ant-" + "z".repeat(24), github: "ghp_" + "z".repeat(24),
  "github-pat": "github_pat_" + "z".repeat(24), aws: "AKIA" + "Z".repeat(16),
  jwt: "eyJ" + "z".repeat(16) + "." + "z".repeat(16) + "." + "z".repeat(16),
})) {
  test(`父页独立脱敏 ${shape} 形状资源，不因成功回执把它带入反馈草稿`, async () => {
    for (const resource of [`assets/${credential}.js`, `https://${credential}.invalid/private`]) {
      const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
        jsonResponse({ ok: true, report_id: "__ut_sanitized_report", team_id: "__ut_team" }));
      await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
      h.node("#lbFeedback").value = "保留用户的修改意见";
      await h.emit(h.message({ observation: { kind: "network_error", resource } }));
      const observation = JSON.parse(h.calls[1].options.body).observation;
      assert.equal(observation.resource === "", true, "不能只依赖注入脚本或后端清洗");
      for (const text of [h.node("#lbStatus").textContent,
        h.ctx._canvasFeedbackText(h.ctx._canvasState)]) {
        assert.equal(text.toLowerCase().includes(credential.toLowerCase()), false);
      }
      await h.ctx._returnCanvasFeedback();
      assert.match(h.ctx.qEl.value, /^用户已有草稿\n\n/);
      assert.match(h.ctx.qEl.value, /保留用户的修改意见/);
      assert.equal(h.ctx.qEl.value.toLowerCase().includes(credential.toLowerCase()), false);
      assert.equal(h.calls.length, 2, "带回反馈仍只准备草稿，不自动派单");
    }
  });
}

test("资源脱敏保留普通文件名和可定位的安全接口前缀", async () => {
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
    jsonResponse({ ok: true, report_id: "__ut_safe_report", team_id: "__ut_team" }));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  for (const resource of ["assets/token-icon.v2.js", "assets/sk-short.js", "assets/中文-v2.js"]) {
    await h.emit(h.message({ observation: { kind: "resource_error", resource } }));
    assert.equal(JSON.parse(h.calls.at(-1).options.body).observation.resource, resource);
  }
  await h.emit(h.message({ observation: { kind: "network_error",
    resource: "/api/generate/private-id?key=discarded#discarded" } }));
  assert.equal(JSON.parse(h.calls.at(-1).options.body).observation.resource, "/api/generate");
});

test("观测失败不伪造已记录原单，且不把服务器原始错误写入草稿", async () => {
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
    jsonResponse({ ok: false, error: "private secret token=hidden" }, 500));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  await h.emit();
  assert.match(h.node("#lbStatus").textContent, /未.*记录|记录.*失败/);
  assert.doesNotMatch(h.node("#lbStatus").textContent, /已记录原单|private|secret|hidden/);
  assert.doesNotMatch(h.ctx._canvasFeedbackText(h.ctx._canvasState), /report_id|已记录原单|secret/);
});

test("每次预览最多十种事实，重复合并而不是再次POST", async () => {
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) :
    jsonResponse({ ok: true, report_id: `__ut_report_${h.calls.length}`, team_id: "__ut_team" }));
  await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  for (let i = 0; i < 20; i++) await h.emit(h.message({
    observation: { kind: "resource_error", resource: "assets/repeated.js?key=" + i },
  }));
  assert.equal(h.calls.length, 2);
  for (let i = 0; i < 15; i++) await h.emit(h.message({
    observation: { kind: "resource_error", resource: `assets/missing-${i}.js` },
  }));
  assert.equal(h.calls.length, 11);
  assert.match(h.node("#lbStatus").textContent, /重复|×|20次/);
});

test("关闭后的旧iframe排队消息不触发请求，旧授权响应不覆盖新会话", async () => {
  const first = deferred(), secondUrl = artifactUrl.replace(snapshot, "c".repeat(64));
  const secondGrant = { ...grant, preview_url: `/artifact-preview/__ut_other${secondUrl}`,
    preview_nonce: "__ut_nonce_b", snapshot_id: "c".repeat(64) };
  let count = 0;
  const h = previewCanvas(() => ++count === 1 ? first.promise : jsonResponse(secondGrant));
  const old = h.ctx.openLightbox("html", artifactUrl, "old.html", { sid: "sid-a" });
  await h.ctx.openLightbox("html", secondUrl, "new.html", { sid: "sid-b" });
  first.resolve(jsonResponse(grant)); await old;
  assert.equal(h.node("#lbContent").frame.src, secondGrant.preview_url);
  assert.equal(h.node("#lbExternal").href, secondGrant.preview_url);
  assert.equal(h.ctx._canvasState.sid, "sid-b");
  assert.equal(h.calls[0].options.signal.aborted, true);
  const oldListener = [...h.listeners][0], oldFrame = h.node("#lbContent").frame;
  h.ctx.closeLightbox();
  assert.equal(h.listeners.size, 0);
  if (oldListener) await oldListener({
    origin: "null", source: oldFrame.contentWindow, data: h.message({ nonce: secondGrant.preview_nonce }),
  });
  assert.equal(h.calls.length, 2);
});

test("旧观测异步回执不能写入下一画布，切账号中的授权也丢弃", async () => {
  const report = deferred();
  const h = previewCanvas(url => url.endsWith("/preview") ? jsonResponse(grant) : report.promise);
  await h.ctx.openLightbox("html", artifactUrl, "old.html", { sid: "sid-a" });
  const pending = h.emit();
  await h.ctx.openLightbox("html", artifactUrl, "new.html", { sid: "sid-b" });
  report.resolve(jsonResponse({ ok: true, report_id: "__ut_old_report", team_id: "__ut_team" }));
  await pending;
  assert.doesNotMatch(h.node("#lbStatus").textContent, /已记录原单|__ut_old_report/);
  assert.doesNotMatch(h.ctx._canvasFeedbackText(h.ctx._canvasState), /__ut_old_report/);
  assert.equal(h.ctx.qEl.value, "用户已有草稿");
  const authorization = deferred(), other = previewCanvas(() => authorization.promise);
  const opening = other.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  other.ctx._personalSubject = "bob"; other.ctx._personalEpoch++;
  authorization.resolve(jsonResponse(grant)); await opening;
  assert.equal(other.node("#lbContent").frame, null);
  assert.notEqual(other.node("#lbExternal").href, grant.preview_url);
});

test("仅明确422不支持可回旧安全预览，鉴权/文件/服务错误必须可见", async () => {
  const legacy = previewCanvas(() => jsonResponse({
    ok: false, unsupported: true, error_code: "preview_unsupported",
  }, 422));
  await legacy.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
  assert.equal(legacy.node("#lbContent").frame.src, artifactUrl);
  assert.match(legacy.node("#lbStatus").textContent, /不支持|未提供|无法.*观测/);
  for (const status of [401, 403, 404, 409, 422, 500]) {
    const h = previewCanvas(() => jsonResponse({ ok: false, error: "secret private url?key=hidden" }, status));
    await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
    assert.equal(h.node("#lbContent").frame, null, String(status));
    assert.match(h.node("#lbContent").innerHTML + h.node("#lbStatus").textContent, new RegExp(String(status)));
    assert.doesNotMatch(h.node("#lbContent").innerHTML + h.node("#lbStatus").textContent, /secret|private|hidden/);
    assert.ok(!h.node("#lbExternal").href, "授权失败时不保留绕过能力的外开链接");
    assert.equal(h.node("#lbDl").href, `${artifactUrl}?download=1`);
  }
});

test("非法或不完整capability不加载；无会话及非HTML沿用原安全查看", async () => {
  for (const invalid of [
    { ...grant, preview_url: "https://other.test/private?key=secret" },
    { ...grant, preview_url: "javascript:alert(1)" },
    { ...grant, preview_url: "/api/legion/auth/session" },
    { ...grant, preview_url: `/artifact-preview/__ut_cap${artifactUrl}?key=secret` },
    { ...grant, preview_url: `/artifact-preview/__ut_cap${artifactUrl.replace(snapshot, "c".repeat(64))}` },
    { ok: true },
  ]) {
    const h = previewCanvas(() => jsonResponse(invalid));
    await h.ctx.openLightbox("html", artifactUrl, "index.html", { sid: "sid-a" });
    assert.equal(h.node("#lbContent").frame, null);
    assert.match(h.node("#lbContent").innerHTML + h.node("#lbStatus").textContent, /失败|不可用|不完整|无效/);
  }
  const h = previewCanvas(() => { throw Error("此分支不能申请快照能力"); });
  await h.ctx.openLightbox("html", "/media/legacy.html", "legacy.html", { sid: "" });
  assert.equal(h.node("#lbContent").frame.src, "/media/legacy.html");
  await h.ctx.openLightbox("img", "/media/image.png", "image.png", { sid: "sid-a" });
  assert.equal(h.calls.length, 0);
  assert.match(h.node("#lbContent").innerHTML, /<img /);
});
