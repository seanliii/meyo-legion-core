import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../web/index_v2.html", import.meta.url), "utf8");
function pageFunction(name) {
  const start = source.search(new RegExp(`^function ${name}\\(`, "m"));
  assert.ok(start >= 0, `missing shipped function: ${name}`);
  for (let end = source.indexOf("}", start); end >= 0; end = source.indexOf("}", end + 1)) {
    const code = source.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw Error(`incomplete function: ${name}`);
}

function setup() {
  const calls = [], messages = [];
  const context = vm.createContext({
    URL, location: { origin: "https://example.test", href: "https://example.test/" },
    curSession: "sid-original", _personalSwitchPending: false,
    openArtifactCanvas(artifact, options) { calls.push(JSON.parse(JSON.stringify({ artifact, options }))); },
    toast(message) { messages.push(message); },
  });
  vm.runInContext(["_canvasArtifactUrl", "_artifactCanvasKind", "_openArtifactLink"]
    .map(pageFunction).join("\n"), context);
  function click(href, options = {}) {
    let prevented = false;
    const link = {
      getAttribute(key) { return key === "href" ? href : null; },
      hasAttribute(key) { return key === "download" && !!options.download; },
      closest(selector) {
        return selector === "[data-sid]" && options.sid ? { dataset: { sid: options.sid } } : null;
      },
    };
    const event = {
      target: { closest() { return link; } }, button: 0,
      preventDefault() { prevented = true; }, ...options,
    };
    context._openArtifactLink(event);
    return prevented;
  }
  return { context, calls, messages, click };
}

test("正文、概览与历史中的本地网页链接共用授权画布，而不是跳到白屏静态路由", () => {
  const run = setup();
  for (const href of [
    "/home-outputs/exports/a/b/index.html",
    "https://example.test/home-outputs/exports/a/b/index.html",
    "/outputs/project/中文%20网页.htm",
  ]) {
    assert.equal(run.click(href), true);
    assert.equal(run.calls.at(-1).options.sid, "sid-original");
    assert.match(run.calls.at(-1).artifact.name, /\.html?$/);
  }
  assert.equal(run.click("/home-outputs/exports/a/b/index.html", { sid: "sid-from-card" }), true);
  assert.equal(run.calls.at(-1).options.sid, "sid-from-card");
  assert.match(source, /document\.addEventListener\('click',_openArtifactLink\)/);
});

test("下载、外链、非网页、模板和短期预览入口保留原行为，不重复授权", () => {
  const run = setup();
  for (const href of [
    "/home-outputs/x/index.html?download=1",
    "/home-outputs/x/report.pdf",
    "/home-outputs/x/index.template.html",
    "/artifact-preview/apv1.test/home-outputs/exports/a/b/index.html",
    "https://other.test/home-outputs/x/index.html",
    "/api/legion/anything.html",
    "javascript:alert(1)",
    "/home-outputs/x/index.html?ticket=private",
  ]) assert.equal(run.click(href), false, href);
  assert.equal(run.click("/home-outputs/x/index.html", { download: true }), false);
  assert.equal(run.calls.length, 0);
});

test("不篡改浏览器的修饰键、中键或已处理点击", () => {
  const run = setup();
  for (const option of [
    { ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true },
    { button: 1 }, { defaultPrevented: true },
  ]) assert.equal(run.click("/home-outputs/x/index.html", option), false);
  assert.equal(run.calls.length, 0);
});

test("切换身份期间不能借旧链接打开新人的画布", () => {
  const run = setup();
  run.context._personalSwitchPending = true;
  assert.equal(run.click("/home-outputs/x/index.html"), true);
  assert.equal(run.calls.length, 0);
  assert.equal(run.messages.length, 1);
});

test("URL内的用户信息不是成果授权，也不能被悄悄丢弃", () => {
  const run = setup();
  const url = "https://secret@example.test/home-outputs/x/index.html";
  assert.equal(run.context._canvasArtifactUrl(url), "");
  assert.equal(run.click(url), false);
  assert.equal(run.calls.length, 0);
});
