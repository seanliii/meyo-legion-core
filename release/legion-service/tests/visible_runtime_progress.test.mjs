import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");

// Execute the page's actual functions without page startup, a browser, or network.
function pageFunction(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  if (start < 0) return ""; // Newly added helpers are absent in the red baseline.
  for (let end = html.indexOf("}", start); end >= 0; end = html.indexOf("}", end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw new Error(`Incomplete page function: ${name}`);
}

// Only the DOM operations used by these consumers; no layout/visual claims.
class Element {
  constructor(tag = "div") {
    Object.assign(this, { tag, children: [], style: {}, dataset: {}, attributes: {},
      className: "", textContent: "", isConnected: true, scrollHeight: 0 });
    this.classList = {
      contains: (c) => this.className.split(/\s+/).includes(c),
      add: (c) => { if (!this.classList.contains(c)) this.className += ` ${c}`; },
      remove: (c) => { this.className = this.className.split(/\s+/).filter((x) => x !== c).join(" "); },
      toggle: (c, force) => {
        const open = force ?? !this.classList.contains(c);
        this.classList[open ? "add" : "remove"](c); return open;
      },
    };
  }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = value;
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    if (name === "style") for (const part of value.split(";")) {
      const [key, val] = part.split(":"); if (key && val) this.style[key.trim()] = val.trim();
    }
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name.startsWith("data-")) delete this.dataset[name.slice(5)];
  }
  set innerHTML(value) {
    this.markup = String(value); this.children = [];
    // Flat descendants suffice for selectors exercised here; production builds rows.
    for (const tag of this.markup.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
      const child = new Element(tag[1]);
      for (const attr of tag[2].matchAll(/([\w-]+)="([^"]*)"/g)) child.setAttribute(attr[1], attr[2]);
      this.children.push(child);
    }
  }
  get innerHTML() { return this.markup || ""; }
  appendChild(child) { this.children.push(child); return child; }
  prepend(child) { this.children.unshift(child); }
  querySelectorAll(selector) {
    const all = this.children.flatMap((c) => [c, ...c.querySelectorAll("*")]);
    return all.filter((c) => selector.split(",").some((s) => {
      if (s === "*") return true;
      const data = s.match(/^\[data-([\w-]+)="([^"]+)"\]$/);
      if (data) return c.getAttribute(`data-${data[1]}`) === data[2] ||
        c.dataset[data[1].replace(/-([a-z])/g, (_, x) => x.toUpperCase())] === data[2];
      const [tag, ...classes] = s.split(".");
      return (!tag || c.tag === tag) && classes.every((x) => c.classList.contains(x));
    }));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

async function screen(mode) {
  const root = new Element(), sendButton = new Element(), qEl = { value: "fixture" };
  const intervals = new Map(); let clock = 0, pendingRead, sequence = 0;
  const finals = [], notices = [];
  const context = vm.createContext({
    document: { createElement: (tag) => new Element(tag), title: "fixture" },
    Date: { now: () => clock }, TextDecoder, ci: () => root,
    setInterval: (fn) => { intervals.set(fn, fn); return fn; },
    clearInterval: (id) => intervals.delete(id),
    $: (s) => s === "#send" ? sendButton : s === "#q" ? qEl : null,
    qEl, uploads: [], _squad: [], _SQUAD_AGENTS: [], _sessionView: 0,
    _personalSubject: "__ut_owner", _personalEpoch: 1, _personalSwitchPending: false,
    _readPersonalStatus: async () => "__ut_owner", _origTitle: "fixture",
    running: false, liveTeamId: "", lastEventId: 0, curSession: "__ut_sid",
    _resumeCtx: null, nameOf: (name) => name,
    esc: (s) => String(s).replaceAll("<", "&lt;"), _toolLabel: (name) => ({ name, desc: "" }),
    _stepPurpose: () => "", _renderProductMilestone: () => false,
    currentQuerySource: () => ({}), loadSessions: async () => {},
    consumeQuerySource() {}, applyModeReason() {}, toast: (text) => notices.push(text),
    renderLegionAnswer(results, data, message) {
      finals.push({ results, data });
      message.querySelector(".st").textContent = answerScreen(results, data).status;
    },
    _bindReplayCard: (m) => (m._replay ||= { cursor: 0, complete: false }),
    sessionStorage: { removeItem() {} },
    ...Object.fromEntries(["_chatScroll", "addUserMsg", "autosize", "_savePersonalDraft",
      "_renderModeFacts", "_memberDeliverySignal", "saveLive", "_renderMilestones"]
      .map((name) => [name, () => {}])),
    fetch: async (url) => {
      assert.equal(url, "/api/legion/dispatch", "unexpected request");
      return { body: { getReader: () => ({ read: () => new Promise((resolve) => { pendingRead = resolve; }) }) } };
    },
  });
  const functions = ["_syncProcessProgress", "_renderRuntimePulse", "newLegionMsg",
    "_renderLegionHeartbeat", "_renderAgentHeartbeat", "_finalDeliveryData",
    "_deliveryDisplay", "_turnSignature", "_markReplayTerminal", "notifyDone",
    "applyLiveEvent", "send"];
  vm.runInContext([html.match(/^const _PHASE_LABELS=.*$/m)[0], ...functions.map(pageFunction)].join("\n"), context);
  let sending;
  if (mode === "live") { sending = context.send(); await new Promise(setImmediate); }
  else context.newLegionMsg("auto");
  const message = root.children[0], state = { steps: 0, eventId: 0 };
  context.applyLiveEvent("meta", { team_id: "__ut_team", sid: "__ut_sid" }, message, { steps: 0, eventId: ++sequence });
  const proc = message.querySelector(".proc-detail"), toggle = message.querySelector(".proc-toggle");
  message.querySelector(".progress").style.display = "flex";
  return {
    message, proc, toggle, context, finals, notices,
    async event(data, eventId = ++sequence) {
      if (mode === "resume") context.applyLiveEvent("step", data, message, { ...state, eventId });
      else {
        assert.ok(pendingRead, message.querySelector(".st").textContent);
        const resolve = pendingRead; pendingRead = null;
        resolve({ value: new TextEncoder().encode(`id: ${eventId}\nevent: step\ndata: ${JSON.stringify(data)}\n\n`), done: false });
        await new Promise(setImmediate);
      }
      state.steps = message._replaySteps || 0;
      assert.doesNotMatch(message.querySelector(".st").textContent, /连不上服务/);
    },
    async final(data, eventId = ++sequence) {
      if (mode === "resume") context.applyLiveEvent("final", data, message, { ...state, eventId });
      else {
        assert.ok(pendingRead, message.querySelector(".st").textContent);
        const resolve = pendingRead; pendingRead = null;
        resolve({ value: new TextEncoder().encode(`id: ${eventId}\nevent: final\ndata: ${JSON.stringify(data)}\n\n`), done: false });
        await new Promise(setImmediate);
      }
      assert.doesNotMatch(message.querySelector(".st").textContent, /连不上服务/);
    },
    tick(ms) { clock = ms; for (const fn of intervals.values()) fn(); },
    finish() {
      // Exercise the real completion-fold block, not unrelated artifact rendering.
      const render = pageFunction("renderLegionAnswer");
      const start = render.indexOf("  // 完成后自动收起执行过程");
      assert.ok(start >= 0);
      context.message = message;
      vm.runInContext(`(function(m,results){${render.slice(start, -1)}})(message,[])`, context);
    },
    async close() { if (pendingRead) pendingRead({ done: true }); await sending; },
  };
}

test("live/resume expose real progress, preserve manual collapse, and exclude pulses/private reasoning", async () => {
  for (const mode of ["live", "resume"]) {
    const ui = await screen(mode);
    try {
      await ui.event({ t: "reason", agent: "codex", hb: true, x: "仍在工作（已 3s）" });
      assert.equal(ui.toggle.style.display, "none", `${mode}: heartbeat must not reveal a real-step entry`);
      await ui.event({ t: "tool_started", agent: "codex", tool: "command_execution", call_id: "one", args_brief: "fixture" });
      assert.equal(ui.toggle.style.display, "inline-flex", `${mode}: first real event must reveal the process entry`);
      assert.match(ui.toggle.textContent, /1步/);
      assert.equal(ui.proc.classList.contains("show"), true);
      ui.toggle.onclick();
      assert.equal(ui.proc.classList.contains("show"), false);
      await ui.event({ t: "tool_end", agent: "codex", tool: "command_execution", call_id: "one", ok: true });
      assert.match(ui.toggle.textContent, /2步/);
      await ui.event({ t: "tool_end", agent: "codex", tool: "command_execution", call_id: "one", ok: true }, 4);
      assert.match(ui.toggle.textContent, /2步/, `${mode}: replaying an event ID must not count it twice`);
      assert.equal(ui.proc.classList.contains("show"), false, `${mode}: another event must not reopen a user fold`);
      assert.equal(ui.toggle.getAttribute("aria-expanded"), "false");
      await ui.event({ t: "heartbeat", stage: "executing", x: "执行中（已 6s）" });
      await ui.event({ t: "reason", heartbeat: true, agent: "codex", x: "活动信号" });
      await ui.event({ t: "reason", agent: "codex", x: "成员正在执行任务（已 8s）" });
      await ui.event({ t: "reasoning", agent: "codex", x: "PRIVATE_REASONING_SENTINEL" });
      await ui.event({ t: "harness_step", channel: "analysis", x: "PRIVATE_REASONING_SENTINEL" });
      assert.match(ui.toggle.textContent, /2步/);
      assert.doesNotMatch(ui.proc.children.map((c) => c.innerHTML).join(""), /PRIVATE_REASONING_SENTINEL/);
      ui.message.querySelector(".progress").onclick();
      assert.equal(ui.proc.classList.contains("show"), true);
      await ui.event({ t: "harness_step", agent: "codex", x: "公开动作" });
      assert.match(ui.toggle.textContent, /3步/);
      ui.tick(65000);
      assert.match(ui.message.querySelector(".elapsed-timer").textContent, /已运行 1分5s/);
      ui.finish();
      assert.match(ui.toggle.textContent, /3步/);
      assert.equal(ui.proc.classList.contains("show"), false);
      ui.toggle.onclick();
      assert.equal(ui.proc.classList.contains("show"), true, `${mode}: completed process remains inspectable`);
    } finally { await ui.close(); }
  }
});

test("a user fold before the first real action survives; pulses alone never gain a completion count", async () => {
  for (const mode of ["live", "resume"]) {
    const ui = await screen(mode);
    try {
      await ui.event({ t: "reasoning", x: "PRIVATE_REASONING_SENTINEL" });
      await ui.event({ t: "heartbeat", stage: "executing", x: "运行中（已 3s）" });
      ui.finish();
      assert.equal(ui.toggle.style.display, "none");
      assert.equal(ui.message._replaySteps, 0);
      const progress = ui.message.querySelector(".progress");
      progress.onclick(); progress.onclick();
      await ui.event({ t: "task_started", agent: "codex", phase: "执行任务" });
      assert.equal(ui.toggle.style.display, "inline-flex");
      assert.match(ui.toggle.textContent, /1步/);
      assert.equal(ui.proc.classList.contains("show"), false);
    } finally { await ui.close(); }
  }
});

test("all inline page scripts remain syntactically valid without executing page startup", () => {
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length);
  for (const [, code] of scripts) assert.doesNotThrow(() => new vm.Script(code));
});

test("milestones do not turn quoted task text into relay or model-switch facts", () => {
  const inserted = [];
  const proc = {
    querySelector: () => null,
    querySelectorAll: (selector) => selector === ".proc-step"
      ? [{ textContent: "不要换模型或自救；未发生接手补位；artifact只是参考字符串" }] : [],
  };
  const message = { querySelector: () => null, insertBefore: (node) => inserted.push(node) };
  const context = vm.createContext({
    document: { createElement: () => ({}) }, esc: String,
  });
  vm.runInContext(pageFunction("_renderMilestones"), context);
  context._renderMilestones(message, proc, [{
    status: "done", model_used: "one-model", prior_attempts: [], relay_by: "", relayed_from: "",
  }]);
  assert.equal(inserted.length, 0, "task wording is not a runtime milestone");
  context._renderMilestones(message, proc, [{
    status: "done", model_used: "one-model", artifacts: [{ name: "actual.md" }],
  }]);
  assert.equal(inserted.length, 1);
  assert.match(inserted[0].innerHTML, /文件.*上报/);
  assert.doesNotMatch(inserted[0].innerHTML, /换模型|补位/);
});

// Files are independent runtime facts, even when the producing attempt failed.
// Exercise the full answer renderer and its real artifact links, without hydration/network.
function freezeRecord(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(freezeRecord);
    Object.freeze(value);
  }
  return value;
}

function answerScreen(results, data) {
  freezeRecord(results); freezeRecord(data);
  const message = new Element();
  message.innerHTML = '<div class="legion-answers"></div><span class="st"></span><div class="next"></div>';
  const context = vm.createContext({
    document: { createElement: (tag) => new Element(tag) },
    nameOf: (name) => name, colorOf: () => "#5b8cff",
    rich: (text) => String(text), _usageBrief: () => "未上报", _num: String,
    _renderModeFacts() {}, hydrateArtifacts() {},
    fetch() { throw new Error("产物渲染测试不允许访问网络"); },
  });
  vm.runInContext([
    "esc", "_artifactPresentationRole", "_chooseVisibleDeliveries",
    "_artifactPurpose", "_artifactSizeLabel", "_toolLabel", "artifactHTML",
    "_looksInternalFailure", "_isVisualArtifact", "_requestedPpt",
    "_bindResultTabs", "_deliveryDisplay", "renderLegionAnswer",
  ].map(pageFunction).join("\n"), context);
  context.renderLegionAnswer(results, data, message);
  const answers = message.querySelector(".legion-answers");
  return {
    context, message, answers,
    summary: answers.querySelector(".deliver-summary"),
    downloads: answers.querySelectorAll("a").filter((node) => node.getAttribute("download") !== null),
    artifacts: answers.querySelectorAll(".artifact"),
    status: message.querySelector(".st").textContent,
  };
}

const GAME_URL = "/home-outputs/codex_web_11a7a60aba57_1792296493_task_codex/pelican-bike/dist/index.html";
const gameArtifact = () => ({ name: "index.html", url: GAME_URL, kind: "html", bytes: 765050 });

test("交付入口：模板源码不是可运行网页，保留下载不提供假运行按钮", () => {
  const { context } = answerScreen([], {});
  const markup = context.artifactHTML({
    name: "index.template.html", kind: "web", url: "/home-outputs/__ut/src/index.template.html",
  });
  assert.match(markup, /模板源码/);
  assert.match(markup, /download=/);
  assert.doesNotMatch(markup, /data-lb="html"|data-preview|在线访问/);
});

test("最近产物：使用同一角色判断，构建入口先于折叠源码，模板没有打开按钮", () => {
  const { context } = answerScreen([], {});
  let body = "";
  Object.assign(context, {
    window: { _recentArts: [
      { sid: "__ut_sid", name: "index.template.html", url: "/home-outputs/__ut/src/index.template.html" },
      { sid: "__ut_sid", ...gameArtifact() },
      { sid: "__ut_sid", name: "build.py", url: "/home-outputs/__ut/build.py" },
    ] },
    _artsMarkSeen() {}, _renderRecentBadge() {},
    showAdvDetail(title, html) { body = html; },
  });
  context.document.querySelectorAll = () => [];
  vm.runInContext(["_groupArtsBySession", "_canPreviewInBrowser", "_artIcon", "_artRowHtml", "openRecentArtifacts"]
    .map(pageFunction).join("\n"), context);
  assert.equal(context._canPreviewInBrowser("index.template.html", "web"), false);
  context.openRecentArtifacts();
  assert.ok(body.indexOf('data-ra-open="0_1"') < body.indexOf('data-ra-body="0"'));
  assert.doesNotMatch(body, /data-ra-open="0_0"/);
  assert.match(body, /模板源码/);
  assert.match(body, /build\.py/);
});

test("交付入口：从真实附件中优先展示唯一构建入口，不把截图或模板当成品", () => {
  const { context } = answerScreen([], {});
  const rows = freezeRecord([
    { name: "shot.png", url: "/home-outputs/__ut/shots/shot.png" },
    { name: "index.template.html", url: "/home-outputs/__ut/src/index.template.html" },
    { name: "source.js", url: "/home-outputs/__ut/src/source.js" },
    gameArtifact(),
    { name: "README.md", url: "/home-outputs/__ut/README.md" },
  ]);
  assert.deepEqual(Array.from(context._chooseVisibleDeliveries(rows), (a) => a.url), [GAME_URL]);
  assert.deepEqual(Array.from(context._chooseVisibleDeliveries([
    rows[1], { name: "index.html", path: "/unpublished/index.html" },
  ])), []);
});

test("交付入口：显式交付角色优先；多个网页不冒选一个质量冠军", () => {
  const { context } = answerScreen([], {});
  const report = { name: "report.pdf", url: "/home-outputs/__ut/report.pdf", role: "primary" };
  assert.equal(context._chooseVisibleDeliveries([gameArtifact(), report])[0], report);
  const alternative = { name: "other.html", url: "/home-outputs/__ut/build/other.html" };
  assert.equal(context._chooseVisibleDeliveries([gameArtifact(), alternative]).length, 2);
  for (const url of ["javascript:alert(1)", "data:text/html,unsafe", "//outside.example/file"]) {
    assert.equal(context._chooseVisibleDeliveries([{ name: "index.html", url, is_primary: true }]).length, 0);
  }
});

test("会话成果：前轮文件与最新失败并列，不清历史或把末轮改成完成", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const turns = freezeRecord(Array.from({ length: 8 }, (_, i) => ({
    question: "original question " + i,
    result: { status: "error", results: [{ status: "error", artifacts: i ? [] : [
      gameArtifact(), { name: "trace.py", url: "/home-outputs/__ut/trace.py" },
    ] }] },
  })));
  const before = JSON.stringify(turns);
  const result = context._sessionDeliveryOverview(turns);
  assert.equal(result.turnCount, 8);
  assert.equal(result.primary[0].url, GAME_URL);
  assert.equal(result.latest.status, "error");
  assert.equal(result.fromEarlier, true);
  assert.equal(result.artifactCount, 2);
  assert.equal(JSON.stringify(turns), before);
});

test("会话成果：同一源文件的新快照置前，旧快照留在文件和原始历史里", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const exported = (snapshot) => ({
    name: "index.html", url: `/home-outputs/exports/scope/${snapshot}/index.html`,
    export_snapshot_id: snapshot,
    provenance: { source: "authorized-project-export", source_path: "/project/app/index.html" },
  });
  const turns = freezeRecord([
    { result: { status: "completed", artifacts: [exported("old")] } },
    { result: { status: "completed", artifacts: [exported("new")] } },
    { result: { status: "error", artifacts: [] } },
  ]);
  const before = JSON.stringify(turns);
  const view = context._sessionDeliveryOverview(turns);
  assert.deepEqual(Array.from(view.primary, a => a.export_snapshot_id), ["new"]);
  assert.equal(view.primary[0]._sourceTurn, 2);
  assert.equal(view.artifactCount, 2, "折叠旧版本不删除文件");
  assert.equal(view.latest.status, "error", "新版文件不改变执行终态");
  assert.equal(view.fromEarlier, true);
  assert.equal(JSON.stringify(turns), before, "不重写会话或产物元数据");
});

test("会话成果：相同文件名但来源不同不能合并，无来源旧记录也完整保留", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const rows = [
    { name: "index.html", url: "/home-outputs/a/index.html",
      provenance: { source: "authorized-project-export", source_path: "/project/a/index.html" } },
    { name: "index.html", url: "/home-outputs/b/index.html",
      provenance: { source: "authorized-project-export", source_path: "/project/b/index.html" } },
    { name: "index.html", url: "/home-outputs/legacy/index.html" },
  ];
  const view = context._sessionDeliveryOverview(rows.map(a => ({ result: { artifacts: [a] } })));
  assert.equal(view.primary.length, 3);
  assert.equal(new Set(Array.from(view.primary, a => a.url)).size, 3);
  assert.equal(view.primary[0]._sourceTurn, 3, "仅按新旧排序，不宣称质量胜出");
});

test("产物可达：error 尝试的真实网页、截图和源码仍可下载，不改成完成", () => {
  const results = [{
    key: "codex", harness: "codex", status: "error", error: "已写文件，随后执行失败",
    final: "不能以此文字把任务改成完成", artifacts: [
      gameArtifact(),
      { name: "shot.png", url: "/home-outputs/__ut/shot.png", bytes: 128 },
      { name: "game.js", url: "/home-outputs/__ut/game.js", bytes: 256 },
    ],
  }];
  const data = { status: "error", all_failed: true, outcome: { status: "failed" }, results };
  const before = JSON.stringify(data);
  const ui = answerScreen(results, data);
  assert.equal(ui.downloads.length, 3, "任务失败不能抹掉已经记录的文件");
  assert.ok(ui.downloads.some((link) => link.getAttribute("href") === GAME_URL + "?download=1"));
  assert.match(ui.summary.innerHTML, /3 份文件与预览/);
  assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/);
  assert.match(ui.status, /本轮未完成.*0\/1 项任务已返回/);
  assert.doesNotMatch(ui.answers.innerHTML + ui.summary.innerHTML, /本轮已完成|🏆 推荐|独立质量审计通过/);
  assert.match(ui.answers.querySelector('[data-pane="diag"]').innerHTML, /已写文件，随后执行失败/);
  assert.equal(JSON.stringify(data), before, "渲染不能修改输入状态、产物记录或完成结论");
});

test("产物可达：partial、cancelled、blocked 尝试同样保留明确产物", () => {
  for (const status of ["partial", "cancelled", "blocked"]) {
    const results = [{ key: "codex", harness: "codex", status, artifacts: [gameArtifact()] }];
    const ui = answerScreen(results, { outcome: { status }, results });
    assert.equal(ui.downloads.length, 1, status);
    assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/, status);
    assert.doesNotMatch(ui.status, /✓|本轮已完成/, status);
    assert.equal(results[0].status, status);
  }
});

test("产物可达：成功与失败成员引用同一文件仍去重，并保留实际来源", () => {
  const artifact = gameArtifact();
  const results = [
    { key: "build", harness: "codex", status: "error", error: "后续步骤失败",
      artifacts: [artifact, { ...artifact }] },
    { key: "review", harness: "hermes", status: "done", final: "成员回复",
      artifacts: [{ ...artifact }] },
  ];
  const ui = answerScreen(results, { winner: "review", outcome: { status: "partial" }, results });
  assert.equal(ui.artifacts.length, 1);
  assert.equal(ui.downloads.length, 1);
  assert.match(ui.answers.querySelector('[data-pane="visual"]').innerHTML, /来自 hermes、codex 的结果/);
  assert.match(ui.summary.innerHTML, /1 份文件与预览/);
  assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/);
  assert.match(ui.status, /本轮仍有未完成事项.*1\/2 项任务已返回/);
});

test("产物可达：同 URL 的不同版本元数据不被错误合并", () => {
  const results = [{
    key: "codex", harness: "codex", status: "done", final: "成员回复",
    artifacts: [
      { ...gameArtifact(), provenance: { sha256: "a".repeat(64) } },
      { ...gameArtifact(), provenance: { sha256: "b".repeat(64) } },
    ],
  }];
  const ui = answerScreen(results, { outcome: { status: "completed" }, results });
  assert.equal(ui.artifacts.length, 2);
  assert.equal(ui.downloads.length, 2);
  assert.match(ui.summary.innerHTML, /2 份文件与预览/);
});

test("产物可达：正常完成结果不被误标成失败尝试，仍使用原下载地址", () => {
  const results = [{ key: "codex", harness: "codex", status: "done", final: "成员回复",
    artifacts: [gameArtifact()] }];
  const ui = answerScreen(results, { outcome: { status: "completed" }, results });
  assert.equal(ui.downloads.length, 1);
  assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
  assert.match(ui.status, /✓ 本轮已完成.*1\/1 项任务已返回/);
  assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
});

test("产物可达：失败文字提及路径不生成链接，空记录不伪造文件", () => {
  const results = [{ key: "codex", harness: "codex", status: "error",
    error: "执行失败", final: "计划生成 " + GAME_URL, artifacts: [null, {}] }];
  const ui = answerScreen(results, { all_failed: true, outcome: { status: "failed" }, results });
  assert.equal(ui.downloads.length, 0);
  assert.equal(ui.artifacts.length, 0);
  assert.match(ui.summary.innerHTML, /0 份文件与预览/);
  assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
  assert.match(ui.status, /本轮未完成/);
});

test("产物可达：无真实 URL 的文件卡片不报错、不把 path 拼成下载或预览", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  for (const url of [undefined, null, "", "   ", 7]) {
    const record = freezeRecord({
      name: "recorded.html", path: "/home-outputs/__ut/recorded.html", url, bytes: 64,
    });
    let markup;
    assert.doesNotThrow(() => { markup = context.artifactHTML(record); });
    assert.match(markup, /recorded\.html/);
    assert.match(markup, /暂无可用入口/);
    assert.doesNotMatch(markup, /<a\b|<iframe\b|<img\b|<video\b|<audio\b|\bdata-render\b/);
  }
});

test("产物可达：失败轮混有 name/path 元数据时，有 URL 的真实文件仍可下载", () => {
  const results = [{
    key: "codex", harness: "codex", status: "error", error: "保存后执行失败",
    artifacts: [
      gameArtifact(),
      { name: "notes.txt", path: "/var/lib/meyo-legion/data/outputs/__ut/notes.txt" },
      { name: "image.png", path: "/home-outputs/__ut/image.png", url: null },
      { path: "/var/lib/meyo-legion/data/outputs/__ut/path-only.txt" },
    ],
  }];
  let ui;
  assert.doesNotThrow(() => {
    ui = answerScreen(results, { all_failed: true, outcome: { status: "failed" }, results });
  });
  assert.equal(ui.artifacts.length, 4);
  assert.equal(ui.downloads.length, 1);
  assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
  const panes = ui.answers.querySelectorAll(".result-pane").map((node) => node.innerHTML).join("\n");
  assert.match(panes, /path-only\.txt/);
  assert.equal((panes.match(/暂无可用入口/g) || []).length, 3);
  assert.match(ui.summary.innerHTML, /4 份文件与预览/);
  assert.match(ui.status, /本轮未完成/);
});

test("产物可达：文件类型默认文案不宣称最终页面或完整成品", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  for (const artifact of [gameArtifact(), { name: "project.zip", url: "/home-outputs/__ut/project.zip" }]) {
    const markup = context.artifactHTML(freezeRecord(artifact));
    assert.doesNotMatch(markup, /最终页面|完整项目包|最终成品/);
  }
});

test("历史预览：网页按需运行，图片延迟加载，保留原文件与安全沙箱", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  const page = context.artifactHTML(gameArtifact());
  assert.doesNotMatch(page, /<iframe\b[^>]*\bsrc=/,
    "切换旧记录不能自动启动每份可运行网页");
  assert.match(page, /data-lb="html"/);
  assert.match(page, /<button\b[^>]*data-preview/);
  assert.match(page, /download="index.html"/);
  const picture = context.artifactHTML({ name: "old.png", url: "/home-outputs/__ut/old.png" });
  assert.match(picture, /<img\b[^>]*loading="lazy"/);
  assert.match(picture, /decoding="async"/);
  const lightbox = pageFunction("openLightbox");
  assert.match(lightbox, /sandbox="allow-scripts allow-forms allow-popups"/);
  assert.doesNotMatch(lightbox, /allow-same-origin/);
});

test("历史文档：离屏不请求转换，进入视口后仅转换一次", () => {
  const nodes = [new Element(), new Element()];
  const observerCallbacks = [], observed = new Set(), rendered = [];
  for (const node of nodes) node.dataset.render = "";
  const context = vm.createContext({
    document: { querySelectorAll: () => [] },
    IntersectionObserver: class {
      constructor(callback) { observerCallbacks.push(callback); }
      observe(node) { observed.add(node); }
      unobserve(node) { observed.delete(node); }
      disconnect() { observed.clear(); }
    },
    renderArtDoc: (node) => rendered.push(node),
    openLightbox() {},
  });
  const root = {
    querySelectorAll(selector) {
      return selector.includes("data-render") ? nodes : [];
    },
  };
  vm.runInContext(pageFunction("hydrateArtifacts"), context);
  context.hydrateArtifacts(root);
  assert.equal(rendered.length, 0);
  assert.equal(observed.size, 2);
  observerCallbacks[0]([{ target: nodes[0], isIntersecting: true }]);
  assert.deepEqual(rendered, [nodes[0]]);
  observerCallbacks[0]([{ target: nodes[0], isIntersecting: true }]);
  assert.equal(rendered.length, 1);
});

test("历史切换：切换已列出的会话不重新下载整个目录，过期响应不能回拨", () => {
  const code = pageFunction("openSession");
  assert.doesNotMatch(code, /await loadSessions\(\)/);
  assert.match(code, /view!==_sessionView/);
  assert.match(code, /aria-busy/);
  assert.match(code, /classList\.toggle\('active'/);
  const sidebar = html.match(/\.sb-item\{[^}]+\}/)?.[0] || "";
  assert.match(sidebar, /width:100%/);
  assert.match(sidebar, /text-align:left/);
});

test("后台打开：没有绘制帧时也恢复身份，绘制和兜底不会重复启动", () => {
  const frames = [], timers = new Map();
  const context = vm.createContext({
    requestAnimationFrame(fn) { frames.push(fn); },
    setTimeout(fn) { timers.set(1, fn); return 1; },
    clearTimeout(id) { timers.delete(id); },
  });
  const source = pageFunction("_afterFirstPaint");
  assert.ok(source, "启动回调需要有无绘制帧的有界兜底");
  vm.runInContext(source, context);
  let started = 0;
  context._afterFirstPaint(() => { started++; });
  assert.equal(started, 0);
  assert.equal(timers.size, 1);
  timers.get(1)();
  assert.equal(started, 1);
  frames.shift()();
  frames.shift()();
  assert.equal(started, 1);
});

test("未登录入口：发送按钮可打开授权而非灰死，历史明确提示登录", () => {
  const elements = new Map(["#whoAmI", "#send", "#sbList", "#historySignIn"]
    .map((key) => [key, new Element()]));
  let opened = 0;
  const context = vm.createContext({
    $: (selector) => elements.get(selector),
    _personalSubject: "", _personalSwitchPending: false, running: false,
    _openPersonalAuth() { opened++; },
  });
  vm.runInContext(pageFunction("_personalLabel"), context);
  context._personalLabel();
  assert.equal(elements.get("#send").disabled, false);
  assert.match(elements.get("#send").textContent, /登录/);
  assert.match(elements.get("#sbList").innerHTML, /登录.*历史|历史.*登录/);
  elements.get("#historySignIn").onclick();
  assert.equal(opened, 1);
  context._personalSubject = "__ut_owner";
  context._personalLabel();
  assert.equal(elements.get("#send").textContent, "派出军团");
  context.running = true;
  context._personalLabel();
  assert.equal(elements.get("#send").disabled, true);
});

test("身份恢复：初始焦点不误报切号，过期焦点检查不能清掉新身份", async () => {
  let checks = 0, lose = 0, reject;
  const context = vm.createContext({
    _personalSubject: "", _personalSwitchPending: false, _personalEpoch: 0,
    _readPersonalStatus() { checks++; return new Promise((resolve, fail) => { reject = fail; }); },
    _losePersonalIdentity() { lose++; },
  });
  const source = pageFunction("_checkFocusedIdentity");
  assert.ok(source);
  vm.runInContext(source, context);
  context._checkFocusedIdentity();
  assert.equal(checks, 0);
  context._personalSubject = "__ut_old";
  const pending = context._checkFocusedIdentity();
  assert.equal(checks, 1);
  context._personalEpoch++;
  context._personalSubject = "__ut_new";
  reject(new Error("old check failed"));
  await pending;
  assert.equal(lose, 0);
});

function historyScreen() {
  const attached = [];
  const create = (tag = "div") => {
    const element = new Element(tag);
    element.isConnected = false;
    element.remove = () => {};
    return element;
  };
  const context = vm.createContext({
    document: {
      createElement: create,
      querySelector(selector) {
        const index = selector.match(/data-turn-index="([^"]+)"/)?.[1];
        return attached.find((pair) => pair[1].dataset.turnIndex === index)?.[1] || null;
      },
    },
    _teamReplay: new Map(), curSession: "__ut_history", _resumeCtx: null,
    _renderModeFacts() {}, esc: (text) => String(text),
    newLegionMsg() {
      const message = create();
      message.innerHTML = '<div class="progress"></div><span class="st"></span>';
      return message;
    },
    renderLegionAnswer(results, data, message) {
      message.recordedFiles = results.flatMap((result) => result.artifacts || []);
      message.renderCount = (message.renderCount || 0) + 1;
      message.recordedDisplay = answerScreen(results, data).status;
    },
  });
  vm.runInContext(["_replayEntry", "_bindReplayCard", "_turnSignature", "renderSessionTurn"]
    .map(pageFunction).join("\n"), context);
  return {
    context,
    attach(pair) {
      pair[0].isConnected = pair[1].isConnected = true;
      pair[1].previousElementSibling = pair[0];
      attached.push(pair);
    },
  };
}

test("历史轮次：相同任务的八条原记录不折叠成两张卡，也不覆盖早期文件", () => {
  const { context } = historyScreen();
  const pairs = [];
  for (let index = 0; index < 8; index++) {
    pairs.push(context.renderSessionTurn({
      question: "saved turn " + index,
      result: {
        team_id: "__ut_team_" + (index % 2), status: "error",
        results: [{ status: "error", artifacts: index === 0 ? [gameArtifact()] : [] }],
      },
    }, index));
  }
  assert.equal(new Set(pairs.map((pair) => pair[1])).size, 8);
  assert.equal(new Set(pairs.map((pair) => pair[0])).size, 8);
  assert.equal(pairs[0][1].recordedFiles.length, 1);
  pairs.forEach((pair, index) => assert.equal(pair[1].dataset.turnIndex, String(index)));
  assert.equal(context._teamReplay.get("__ut_team_0").msgEl, pairs[6][1]);
  assert.equal(context._teamReplay.get("__ut_team_1").msgEl, pairs[7][1]);
});

test("历史轮次：同一轮更新仍复用原卡和气泡，在线未编号卡可被快照接管", () => {
  const screen = historyScreen();
  const turn = { question: "same", result: {
    team_id: "__ut_live", status: "error", results: [{ status: "error", artifacts: [] }],
  } };
  const first = screen.context.renderSessionTurn(turn, 0);
  screen.attach(first);
  const same = screen.context.renderSessionTurn(turn, 0);
  assert.equal(same[0], first[0]);
  assert.equal(same[1], first[1]);
  delete first[1].dataset.turnIndex;
  const adopted = screen.context.renderSessionTurn(turn, 0);
  assert.equal(adopted[0], first[0]);
  assert.equal(adopted[1], first[1]);
  assert.equal(adopted[1].dataset.turnIndex, "0");
});

test("历史轮次：旧轮不能抢占未编号在线卡或回拨当前回放状态", () => {
  const screen = historyScreen(), { context } = screen;
  const live = context.newLegionMsg();
  const user = new Element(); user.className = "msg user";
  live.previousElementSibling = user;
  const entry = context._replayEntry("__ut_live");
  Object.assign(entry, { msgEl: live, userEl: user, cursor: 19, terminalId: 12,
                        executionId: "__ut_current", complete: false });
  const turn = { question: "saved", result: { team_id: "__ut_live", status: "error",
    results: [{ status: "error", artifacts: [gameArtifact()] }] } };
  const older = context.renderSessionTurn(turn, 0, false);
  assert.notEqual(older[1], live);
  assert.notEqual(older[0], user);
  assert.equal(entry.msgEl, live);
  assert.equal(entry.complete, false);
  assert.equal(entry.cursor, 19);
  assert.equal(entry.executionId, "__ut_current");
  screen.attach(older);
  const latest = context.renderSessionTurn(turn, 1, true);
  assert.equal(latest[1], live);
  context.renderSessionTurn(turn, 0, false);
  assert.equal(entry.msgEl, live);
  assert.equal(entry.msgEl.dataset.turnIndex, "1");
});

const returnPilot = () => ({
  round: 1, action: "stop", kind: "turn_returned", reason: "原记录：未收到继续请求",
});
const surfaceResult = () => ({
  key: "codex", harness: "codex", status: "done", final: "好", artifacts: [gameArtifact()],
});

test("停因：只有明确的新 stop/turn_returned 修正标签，结果和下载原样保留", () => {
  for (const pilot of [undefined, { ...returnPilot(), action: "continue" },
    { ...returnPilot(), kind: "done" }, returnPilot()]) {
    const results = [surfaceResult()];
    const data = { results, outcome: { status: "completed" }, ...(pilot ? { pilot } : {}) };
    const before = JSON.stringify(data);
    const ui = answerScreen(results, data);
    const returned = pilot?.action === "stop" && pilot.kind === "turn_returned";
    assert.match(ui.status, returned ? /✓ 本轮已返回/ : /✓ 本轮已完成/);
    if (returned) {
      assert.match(ui.summary.innerHTML, /目标.*尚未确认/);
      assert.match(ui.summary.innerHTML, /原记录：未收到继续请求/);
    }
    assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
    assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
    assert.equal(JSON.stringify(data), before);
  }
});

test("停因：取消、契约、验证、partial 和失败事实不被返回标签覆盖", () => {
  const rows = [surfaceResult()];
  for (const [data, title] of [
    [{ cancelled: true, outcome: { status: "completed" } }, "本轮已停止"],
    [{ outcome: { status: "cancelled" } }, "本轮已停止"],
    [{ contract_incomplete: true, outcome: { status: "completed" } }, "要求的成品尚未齐全"],
    [{ outcome: { status: "completed", delivery_contract: { missing: [{ description: "真实缺项" }] } } }, "要求的成品尚未齐全"],
    [{ verification_incomplete: true, outcome: { status: "completed" } }, "成品仍待验证"],
    [{ outcome: { status: "partial" } }, "本轮仍有未完成事项"],
    [{ partial: true }, "本轮仍有未完成事项"],
    ...["failed", "incomplete", "blocked"].map((status) => [{ outcome: { status } }, "本轮未完成"]),
    [{ all_failed: true }, "本轮未完成"],
  ]) {
    const ui = answerScreen(rows, { ...data, pilot: returnPilot() });
    assert.ok(ui.status.includes(title), ui.status);
    assert.doesNotMatch(ui.status, /本轮已返回/);
  }
});

test("停因：公开 reason 有界且按文本转义，不作为 HTML，也不改原记录", () => {
  const data = { results: [surfaceResult()], outcome: { status: "completed" },
    pilot: { ...returnPilot(), reason: '<img src=x onerror="bad()"> & ' + "x".repeat(900) + "TAIL_NOT_VISIBLE" } };
  const before = JSON.stringify(data);
  const ui = answerScreen(data.results, data);
  assert.match(ui.summary.innerHTML, /&lt;img/);
  assert.match(ui.summary.innerHTML, /&amp;/);
  assert.doesNotMatch(ui.summary.innerHTML, /<img|TAIL_NOT_VISIBLE/);
  assert.equal(JSON.stringify(data), before);
});

for (const mode of ["live", "resume"]) {
  for (const shape of ["dispatch", "single"]) {
    for (const fresh of [true, false]) {
      test(`final包装：${mode}/${shape}/${fresh ? "新停因" : "历史无字段"} 终态渲染与通知一致`, async () => {
        const ui = await screen(mode);
        const rows = [surfaceResult()];
        const facts = { outcome: { status: "completed" }, session: { sid: "__ut_sid", turns: 1 },
          ...(fresh ? { pilot: returnPilot() } : {}) };
        const data = shape === "dispatch" ? { ...facts, results: rows }
          : { node_id: "codex", result: { ...rows[0], ...facts } };
        freezeRecord(data);
        const before = JSON.stringify(data);
        try {
          await ui.final(data, 17);
          assert.equal(ui.finals.length, 1);
          assert.equal(ui.finals[0].results.length, 1, "single final must not become an empty team");
          assert.equal(ui.finals[0].results[0].final, "好");
          assert.equal(ui.context.curSession, "__ut_sid");
          assert.equal(ui.message._replay.terminalId, 17);
          assert.equal(ui.message._replay.complete, true);
          assert.equal(ui.message._turnSignature, ui.context._turnSignature(ui.finals[0].data));
          const title = fresh ? "本轮已返回" : "本轮已完成";
          assert.ok(ui.message.querySelector(".st").textContent.includes(title));
          assert.ok(ui.notices[0].includes(title));
          assert.equal(ui.message.querySelector(".progress").style.display, "none");
          await ui.final(data, 17);
          assert.equal(ui.finals.length, 1, "duplicate event must not render or notify again");
          assert.equal(ui.notices.length, 1);
          assert.equal(JSON.stringify(data), before);
        } finally { await ui.close(); }
      });
    }
  }
}

test("停因：薄投影只认单家 API 包装，不从某个成员推导团队停因", () => {
  const context = vm.createContext({});
  vm.runInContext(pageFunction("_finalDeliveryData"), context);
  assert.equal(typeof context._finalDeliveryData, "function");
  const member = freezeRecord({ ...surfaceResult(), pilot: returnPilot() });
  const multi = freezeRecord({ results: [member, surfaceResult()], outcome: { status: "completed" } });
  const projected = context._finalDeliveryData(multi);
  assert.equal(projected, multi);
  assert.equal(projected.pilot, undefined);
  assert.match(answerScreen(projected.results, projected).status, /本轮已完成/);
  const wrapper = freezeRecord({ node_id: "codex", result: member });
  const single = context._finalDeliveryData(wrapper);
  assert.equal(single.results[0], member);
  assert.equal(single.pilot, member.pilot, "reuse facts, do not recursively copy/reclassify them");
  const unknown = freezeRecord({ result: member });
  assert.equal(context._finalDeliveryData(unknown), unknown);
  assert.equal(context._finalDeliveryData(unknown).pilot, undefined);
});

test("停因：同轮新增顶层事实触发原卡刷新，新旧历史包装一致且不增卡", () => {
  for (const nested of [false, true]) {
    const screen = historyScreen(), { context } = screen;
    const result = { team_id: "__ut_refresh", status: "done", ts: 123,
      results: [surfaceResult()], outcome: { status: "completed" } };
    const wrap = (r) => nested ? { question: "same", result: r } : { question: "same", ...r };
    const first = context.renderSessionTurn(freezeRecord(wrap(result)), 0);
    screen.attach(first);
    assert.match(first[1].recordedDisplay, /本轮已完成/);
    const next = freezeRecord(wrap({ ...result, pilot: returnPilot() }));
    const second = context.renderSessionTurn(next, 0);
    assert.equal(second[0], first[0]);
    assert.equal(second[1], first[1]);
    assert.equal(second[1].renderCount, 2, "top-level pilot-only change must refresh this turn");
    assert.match(second[1].recordedDisplay, /本轮已返回/);
    assert.equal(second[1].recordedFiles[0].url, GAME_URL);
    context.renderSessionTurn(next, 0);
    assert.equal(second[1].renderCount, 2);
  }
});

test("停因：signature 只纳入有限公开字段，不序列化无界诊断或私有子对象", () => {
  const { context } = historyScreen();
  const base = { ts: 1, status: "done", results: [], pilot: returnPilot() };
  const signature = context._turnSignature(base);
  for (const [field, value] of [["round", 2], ["action", "continue"], ["kind", "done"], ["reason", "new fact"]]) {
    assert.notEqual(context._turnSignature({ ...base, pilot: { ...base.pilot, [field]: value } }), signature);
  }
  const unbounded = { toJSON() { throw new Error("must not serialize unbounded report"); } };
  assert.equal(context._turnSignature({ ...base, persistent_diagnostics: unbounded,
    pilot: { ...base.pilot, shared_goals: unbounded, owner_proxy: unbounded } }), signature);
  const long = { ...base, pilot: { ...base.pilot, reason: "a".repeat(500) + "ONE" } };
  const other = { ...base, pilot: { ...base.pilot, reason: "a".repeat(500) + "TWO" } };
  assert.equal(context._turnSignature(long), context._turnSignature(other), "only rendered reason prefix affects the card");
  assert.ok(context._turnSignature(long).length < 800);
});
