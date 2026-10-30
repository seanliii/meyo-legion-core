import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Execute the existing page functions, not a reimplementation. The small DOM and
// HTTP doubles below test control flow only: no browser, cloud, or visual claim.
const html = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");
const OWNER = "__ut_mobile_owner";
const BASE = "/api/legion/mobile";
const PNG_PREFIX = "data:image/png;base64,";
const INLINE_PNG = PNG_PREFIX + Buffer.from([137, 80, 78, 71]).toString("base64");
function pageFunction(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  if (start < 0) return ""; // A new helper is legitimately absent in the red run.
  for (let end = html.indexOf("}", start); end >= 0; end = html.indexOf("}", end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw new Error(`Incomplete page function: ${name}`);
}
const unescape = (s) => s.replaceAll("&lt;", "<").replaceAll("&gt;", ">")
  .replaceAll("&quot;", '"').replaceAll("&amp;", "&");

class Element {
  constructor(dom, tag = "div") {
    Object.assign(this, { dom, tag, children: [], attributes: {}, style: {}, dataset: {},
      hidden: false, disabled: false, isConnected: true, value: "", textContent: "",
      className: "", scrollHeight: 0, captured: new Set() });
    this.classList = {
      contains: (c) => this.className.split(/\s+/).includes(c),
      add: (c) => { if (!this.classList.contains(c)) this.className += ` ${c}`; },
      remove: (c) => { this.className = this.className.split(/\s+/).filter((v) => v !== c).join(" "); },
    };
  }
  setAttribute(name, value = "") {
    this.attributes[name] = String(value);
    if (name === "id") { this.id = value; this.dom.nodes.set(value, this); }
    if (name === "class") this.className = value;
    if (name === "hidden") this.hidden = true;
    if (name === "disabled") this.disabled = true;
    if (name.startsWith("data-")) this.dataset[name.slice(5)] = value;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "hidden") this.hidden = false;
    if (name === "disabled") this.disabled = false;
  }
  get src() { return this.getAttribute("src") || ""; }
  set src(value) { this.setAttribute("src", value); }
  disconnect() {
    this.isConnected = false;
    if (this.id && this.dom.nodes.get(this.id) === this) this.dom.nodes.delete(this.id);
    this.children.forEach((c) => c.disconnect());
  }
  replaceChildren(...children) {
    this.children.forEach((c) => c.disconnect());
    this.children = children;
    this.textContent = "";
  }
  set innerHTML(value) {
    this.replaceChildren();
    this.markup = String(value);
    const stack = [this];
    const voidTags = new Set(["img", "input", "br", "hr"]);
    for (const token of this.markup.matchAll(/<\/?[\w-]+\b[^>]*>|[^<]+/g)) {
      if (token[0].startsWith("</")) { if (stack.length > 1) stack.pop(); continue; }
      const open = token[0].match(/^<([\w-]+)\b([^>]*)>/);
      if (!open) {
        for (const parent of stack) parent.textContent += unescape(token[0]);
        continue;
      }
      const element = new Element(this.dom, open[1]);
      for (const attr of open[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))
        element.setAttribute(attr[1], unescape(attr[2] || ""));
      stack.at(-1).children.push(element);
      if (!voidTags.has(open[1])) stack.push(element);
    }
  }
  get innerHTML() { return this.markup || ""; }
  querySelectorAll(selector) {
    const matches = (node, part) => {
      part = part.trim();
      if (part.startsWith("#")) return node.id === part.slice(1);
      if (part.startsWith(".")) return node.classList.contains(part.slice(1));
      if (part.startsWith("[")) return node.getAttribute(part.slice(1, -1)) !== null;
      return part === "*" || node.tag === part;
    };
    return this.children.flatMap((c) => [c, ...c.querySelectorAll("*")])
      .filter((c) => selector.split(",").some((s) => matches(c, s)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() { return this.rect || { left: 20, top: 40, width: 270, height: 480 }; }
  setPointerCapture(id) { this.captured.add(id); }
  hasPointerCapture(id) { return this.captured.has(id); }
  releasePointerCapture(id) { this.captured.delete(id); this.onlostpointercapture?.({ pointerId: id }); }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  click() { if (!this.disabled) return this.onclick?.({ preventDefault() {} }); }
}

function fixture({ ready = false, provider = "agentbay" } = {}) {
  const dom = { nodes: new Map(), title: "fixture" };
  for (const id of ["advPanel", "advPanelBody", "advPanelTitle", "advPanelIcon", "q", "send",
    "modeSelect", "sbList", "sbQuick", "chat", "whoAmI", "personalToken", "personalAuthError",
    "personalAuthCurrent", "personalAuthDialog", "personalAuthCancel"]) {
    const el = new Element(dom);
    el.setAttribute("id", id);
  }
  const get = (id) => dom.nodes.get(id) || null;
  get("advPanel").hidden = true;
  const calls = [], held = new Map(), revoked = [], created = [], failures = new Map();
  let image = 0;
  const state = {
    ok: true, provider, ready, phase: ready ? "ready" : "idle",
    session_seconds: 120, remaining_seconds: ready ? 90 : 0,
    remaining_budget_seconds: 360, expires_at: ready ? 1792231320 : null,
    screen_size: "1080x1920",
    actions: ["screenshot", "tap", "swipe", "press", "type", "start", "ui", "apps"],
  };
  const URLClass = class extends URL {};
  URLClass.createObjectURL = () => { const value = `blob:__ut_mobile_${++image}`; created.push(value); return value; };
  URLClass.revokeObjectURL = (value) => revoked.push(value);
  const context = vm.createContext({
    URL: URLClass, Response, Blob, AbortController, DOMException, setTimeout, clearTimeout,
    location: { href: "http://localhost/", origin: "http://localhost", protocol: "http:", hostname: "localhost" },
    window: {}, document: {
      querySelector: (s) => s.startsWith("#") ? get(s.slice(1)) : null,
      querySelectorAll: (s) => get("advPanelBody").querySelectorAll(s),
      createElement: (tag) => new Element(dom),
    },
    $: (s) => s.startsWith("#") ? get(s.slice(1)) : null,
    clientUid: OWNER, _personalSubject: OWNER, _personalEpoch: 1, _personalSwitchPending: false,
    _advViewToken: 0, _mobilePanelCleanup: null, _personalRequests: new Set(),
    _resumeCtx: null, _syncTimer: null, _sessionView: 0, _teamReplay: new Set(),
    _origTitle: "fixture", _NATIVE_RETURN_KEY: "__ut_native_return",
    curSession: "__ut_original_conversation", qEl: get("q"),
    running: false, sessionStorage: { removeItem() {} },
    _personalAuthTransportReady: () => true,
    ...Object.fromEntries(["autosize", "renderUploads", "_renderSquadPool",
      "_renderRelayEditor", "_closeAdv", "toast"].map((name) => [name, () => {}])),
    fetch: async (target, options = {}) => {
      const url = new URL(target, "http://localhost/");
      const inline = url.protocol === "data:";
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      // Do not put image data into logs or assertion diagnostics.
      const call = { path: inline ? "<inline-png>" : url.pathname, query: url.searchParams,
        method, body, signal: options.signal };
      calls.push(call);
      const hold = held.get(`${method} ${url.pathname}`);
      if (hold) { held.delete(`${method} ${url.pathname}`); await hold.promise; }
      const key = method + " " + url.pathname;
      if (failures.has(key)) return new Response(JSON.stringify({
        ok: false, error: failures.get(key),
      }), { status: 503, headers: { "Content-Type": "application/json" } });
      if (inline || url.pathname === "/api/legion/artifact")
        return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { "Content-Type": "image/png" } });
      let result;
      if (url.pathname === BASE + "/status") result = { ...state };
      else if (url.pathname === BASE + "/ensure") {
        state.ready = true; state.phase = "ready"; state.remaining_seconds = 120;
        result = { ok: true, status: { ...state } };
      } else if (url.pathname === BASE + "/stop") {
        state.ready = false; state.phase = "stopped"; state.remaining_seconds = 0; state.expires_at = null;
        result = { ok: true, status: { ...state } };
      } else if (url.pathname === BASE + "/screenshot") result = {
        ok: true, path: "/fixture/phone.png",
        url: state.provider === "agentbay" ? INLINE_PNG : "/api/legion/artifact?path=%2Ffixture%2Fphone.png",
        bytes: 4, width_height: state.screen_size,
      };
      else if (url.pathname === BASE + "/action") result = { ok: true, stdout: "fixture apps" };
      else if (url.pathname === BASE + "/tasks") result = { ok: true, tasks: [] };
      else if (url.pathname === BASE + "/companion/inbox") result = { ok: true, items: [] };
      else if (url.pathname === BASE + "/companion/summary") result = { ok: true, states: {} };
      else throw new Error(`unexpected request: ${key}`);
      return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } });
    },
  });
  vm.runInContext(["esc", "showAdvDetail", "advBegin", "_closeAdvPanel", "_clearMobilePanel",
    "_clearPersonalView", "_openPersonalAuth", "_setPrompt", "openMobilePanel"]
    .map(pageFunction).join("\n"), context);
  return {
    context, calls, state, get, failures, created, revoked,
    open: () => context.openMobilePanel(),
    close: () => context._closeAdvPanel(),
    async click(id) { const el = get(id); assert.ok(el, `missing control ${id}`); await el.click(); await flush(); },
    hold(method, path) {
      let release;
      const promise = new Promise((resolve) => { release = resolve; });
      held.set(`${method} ${path}`, { promise });
      return release;
    },
    switchOwner() {
      context._clearPersonalView();
      context._personalSubject = "__ut_other_owner";
      context.clientUid = "__ut_other_owner";
    },
  };
}
const flush = () => new Promise(setImmediate);
const requests = (ui, method, suffix) => ui.calls.filter((c) =>
  c.method === method && c.path === BASE + suffix);
const pointer = (x, y, id = 1) => ({
  clientX: x, clientY: y, pointerId: id, button: 0, isPrimary: true, pointerType: "touch",
  preventDefault() {},
});

test("opening a stopped cloud phone only reads status, with explicit startup and budget", async () => {
  const ui = fixture();
  await ui.open();
  assert.deepEqual(ui.calls.map((c) => [c.method, c.path]), [["GET", BASE + "/status"]]);
  assert.equal(ui.get("phoneStart")?.disabled, false);
  assert.match(ui.get("phoneBudget")?.textContent || "", /120/);
  assert.match(ui.get("phoneBudget")?.textContent || "", /360/);
  assert.match(ui.get("phoneBudget")?.textContent || "", /0/);
  assert.ok(!ui.get("phoneScreen")?.src);
  for (const id of ["phoneInstall", "phoneInspect", "phoneStoreSearch", "phoneTasks"])
    assert.equal(ui.get(id), null, `${id} must not advertise the old backend`);
  ui.close();
});

test("explicit start is the only ensure request and displays the owner response PNG directly", async () => {
  const ui = fixture();
  await ui.open();
  await ui.click("phoneStart");
  assert.equal(requests(ui, "POST", "/ensure").length, 1);
  assert.ok(requests(ui, "GET", "/screenshot").length);
  assert.ok(ui.get("phoneScreen").src.startsWith(PNG_PREFIX), "show the authenticated inline PNG");
  assert.equal(ui.get("phoneScreen").hidden, false);
  for (const call of ui.calls.filter((c) => c.path.startsWith(BASE))) {
    if (call.method === "POST") assert.equal(call.body.uid, OWNER);
    else assert.equal(call.query.get("uid"), OWNER);
  }
  assert.ok(ui.calls.every((c) => c.path.startsWith(BASE)), "no artifact grant or second image request");
  ui.close();
});

test("a running phone opens without ensure; Home, Back and literal text refresh the picture", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  assert.ok(ui.get("phoneScreen").src.startsWith(PNG_PREFIX));
  assert.ok(ui.calls.every((c) => c.path.startsWith(BASE)));
  await ui.click("phoneHome");
  await ui.click("phoneBack");
  ui.get("phoneText").value = " 中文输入 & spaces \n";
  await ui.click("phoneType");
  const actions = requests(ui, "POST", "/action").map((c) => [c.body.action, c.body.params]);
  assert.deepEqual(actions, [["press", { key: "HOME" }], ["press", { key: "BACK" }],
    ["type", { text: " 中文输入 & spaces \n" }]]);
  assert.equal(ui.get("phoneText").value, "");
  assert.equal(requests(ui, "GET", "/screenshot").length, 4);
  const before = requests(ui, "POST", "/action").length;
  await ui.click("phoneRefresh");
  assert.equal(requests(ui, "POST", "/action").length, before);
  assert.equal(requests(ui, "GET", "/screenshot").length, 5);
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  ui.close();
});

test("screen gestures use screenshot coordinates and cancel does not become a tap", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  const screen = ui.get("phoneScreen");
  screen.onpointerdown(pointer(155, 280));
  await screen.onpointerup(pointer(155, 280));
  await flush();
  screen.onpointerdown(pointer(87.5, 160));
  await screen.onpointerup(pointer(222.5, 400));
  await flush();
  screen.onpointerdown(pointer(155, 280));
  screen.onpointercancel(pointer(155, 280));
  await screen.onpointerup(pointer(155, 280));
  const actions = requests(ui, "POST", "/action").map((c) => [c.body.action, c.body.params]);
  assert.deepEqual(actions, [["tap", { x: 540, y: 960 }],
    ["swipe", { x1: 270, y1: 480, x2: 810, y2: 1440, duration_ms: 350 }]]);
  ui.close();
});

test("letterboxing and out-of-bounds pointer releases do not target the wrong pixel", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  const screen = ui.get("phoneScreen");
  screen.rect = { left: 0, top: 0, width: 400, height: 400 };
  screen.onpointerdown(pointer(10, 200)); // Black margin, not the phone.
  await screen.onpointerup(pointer(10, 200));
  assert.equal(requests(ui, "POST", "/action").length, 0);
  screen.onpointerdown(pointer(200, 200));
  await screen.onpointerup(pointer(500, 500));
  const params = requests(ui, "POST", "/action").at(-1).body.params;
  assert.deepEqual(params, { x1: 540, y1: 960, x2: 1079, y2: 1919, duration_ms: 350 });
  ui.close();
});

test("double startup clicks cannot create two sessions", async () => {
  const ui = fixture();
  await ui.open();
  const release = ui.hold("POST", BASE + "/ensure");
  const first = ui.get("phoneStart").click();
  await flush();
  await ui.get("phoneStart").click();
  assert.equal(requests(ui, "POST", "/ensure").length, 1);
  release();
  await first;
  ui.close();
});

test("backend startup errors are shown verbatim without retries, screenshots, or UI policy", async () => {
  for (const error of ["mobile_reaper_unavailable", "mobile_creation_outcome_unknown"]) {
    const ui = fixture();
    ui.state.reaper_ready = false;
    await ui.open();
    assert.equal(ui.get("phoneStart").disabled, false, "the provider, not UI policy, decides");
    ui.failures.set("POST " + BASE + "/ensure", error);
    await ui.click("phoneStart");
    assert.match(ui.get("phoneOut").textContent, new RegExp(error));
    assert.equal(requests(ui, "POST", "/ensure").length, 1);
    assert.equal(requests(ui, "GET", "/screenshot").length, 0);
    assert.equal(ui.get("phoneScreen").src, "");
    ui.close();
  }
});

test("stop clears the picture before waiting for release and never restarts", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  const screen = ui.get("phoneScreen");
  assert.ok(screen.src.startsWith(PNG_PREFIX));
  const release = ui.hold("POST", BASE + "/stop");
  const stopped = ui.get("phoneStop").click();
  await flush();
  assert.equal(screen.src, "");
  assert.equal(screen.hidden, true);
  release();
  await stopped;
  assert.equal(requests(ui, "POST", "/stop").length, 1);
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  assert.equal(ui.get("phoneStart").disabled, false);
  ui.close();
});

test("failed release is visible, never described as released", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  ui.failures.set("POST " + BASE + "/stop", "fixture_release_failed");
  await ui.click("phoneStop");
  assert.match(ui.get("phoneOut").textContent, /fixture_release_failed/);
  assert.equal(ui.get("phoneScreen").src, "");
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  ui.close();
});

test("closing before status arrives cannot resurrect the phone panel", async () => {
  const ui = fixture();
  const release = ui.hold("GET", BASE + "/status");
  const opening = ui.open();
  await flush();
  ui.close();
  release();
  await opening;
  assert.equal(ui.get("advPanel").hidden, true);
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  assert.equal(requests(ui, "GET", "/screenshot").length, 0);
});

test("another existing panel supersedes a pending phone response", async () => {
  const ui = fixture();
  const release = ui.hold("GET", BASE + "/status");
  const opening = ui.open();
  await flush();
  ui.context.showAdvDetail("其他面板", '<p id="otherPanel">保留这一个面板</p>', "");
  release();
  await opening;
  assert.ok(ui.get("otherPanel"));
  assert.equal(ui.get("phoneStart"), null);
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
});

test("stop during the initial screenshot wait discards the late picture", async () => {
  const ui = fixture({ ready: true });
  const release = ui.hold("GET", BASE + "/screenshot");
  const opening = ui.open();
  await flush();
  await ui.click("phoneStop");
  release();
  await opening;
  assert.equal(ui.get("phoneScreen").src, "");
  assert.equal(ui.get("phoneScreen").hidden, true);
  assert.equal(ui.created.length, 0);
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  ui.close();
});

test("closing removes the existing picture and aborts an in-flight screen request", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  const screen = ui.get("phoneScreen");
  assert.ok(screen.src.startsWith(PNG_PREFIX));
  const release = ui.hold("GET", BASE + "/screenshot");
  const refreshing = ui.get("phoneRefresh").click();
  await flush();
  ui.close();
  release();
  await refreshing;
  assert.equal(screen.src, "");
  assert.equal(screen.hidden, true);
  assert.equal(ui.get("advPanel").hidden, true);
  assert.equal(requests(ui, "GET", "/screenshot").at(-1).signal.aborted, true);
});

test("switching owner while image bytes arrive discards the old image and old controls", async () => {
  const ui = fixture({ ready: true });
  const release = ui.hold("GET", BASE + "/screenshot");
  const opening = ui.open();
  await flush();
  const oldScreen = ui.get("phoneScreen"), oldHome = ui.get("phoneHome");
  assert.ok(oldScreen);
  ui.switchOwner();
  release();
  await opening;
  assert.equal(oldScreen.src, "");
  assert.equal(ui.get("advPanel").hidden, true);
  const before = ui.calls.length;
  await oldHome.click();
  assert.equal(ui.calls.length, before);
});

test("opening personal authorization immediately clears a visible phone", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  const screen = ui.get("phoneScreen");
  assert.ok(screen.src.startsWith(PNG_PREFIX));
  ui.context._openPersonalAuth();
  assert.equal(screen.src, "");
  assert.equal(ui.get("personalAuthDialog").open, true);
  ui.close();
});

test("returning to the legion keeps the original conversation and draft, without dispatch", async () => {
  const ui = fixture({ ready: true });
  ui.get("q").value = "不要覆盖这个原会话草稿";
  await ui.open();
  const before = ui.calls.length;
  await ui.click("phoneAsk");
  assert.equal(ui.calls.length, before);
  assert.equal(ui.context.curSession, "__ut_original_conversation");
  assert.equal(ui.get("q").value, "不要覆盖这个原会话草稿");
  assert.equal(ui.get("q").focused, true);
  assert.equal(ui.get("advPanel").hidden, true);
});

test("failed screenshot is an error, not an old screen or implicit startup", async () => {
  const ui = fixture({ ready: true });
  await ui.open();
  ui.failures.set("GET " + BASE + "/screenshot", "fixture_capture_failed");
  await ui.click("phoneRefresh");
  assert.match(ui.get("phoneOut").textContent, /fixture_capture_failed/);
  assert.equal(ui.get("phoneScreen").src, "");
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  ui.close();
});

test("legacy Redroid still has its APK/store/task controls, but opening never POSTs ensure", async () => {
  const ui = fixture({ ready: true, provider: "redroid" });
  await ui.open();
  await flush();
  assert.equal(ui.calls[0].path, BASE + "/status");
  assert.equal(requests(ui, "POST", "/ensure").length, 0);
  for (const id of ["phoneInstall", "phoneInspect", "phoneStoreSearch", "phoneTasks", "phoneAsk"])
    assert.ok(ui.get(id), `legacy ${id} was removed`);
  assert.equal(requests(ui, "GET", "/tasks").length, 1);
  ui.close();
});
