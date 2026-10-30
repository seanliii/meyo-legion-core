import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const scriptUrl = new URL("../web/desktop_production.js", import.meta.url);
const script = new vm.Script(readFileSync(scriptUrl, "utf8"), { filename: scriptUrl.pathname });
const api = "/api/legion/cloud_desktop";

// Execute the whole shipped script. Only DOM/network/timers are replaced:
// no browser, HTTP listener, credentials, device, or model is started.
function desktop({ lexicalUid, windowUid, authenticated = true, project, task } = {}) {
  const requests = [], views = [], nodes = new Map(), listeners = new Map(), intervals = [];
  const node = (selector) => {
    if (!nodes.has(selector)) {
      nodes.set(selector, { value: "", textContent: "", scrollHeight: 0, click() {} });
    }
    return nodes.get(selector);
  };
  const context = vm.createContext({
    document: {
      readyState: "loading",
      visibilityState: "visible",
      querySelector: (selector) => selector.startsWith("#") ? node(selector) : null,
      querySelectorAll: () => [],
      addEventListener: (name, fn) => listeners.set(name, fn),
    },
    showAdvDetail: (title, html) => views.push({ title, html }),
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    curSession: "__ut_session",
    liveTeamId: "__ut_team",
    _setPrompt() {},
    async fetch(target, options = {}) {
      const url = new URL(target, "https://offline-fixture.invalid");
      assert.equal(url.origin, "https://offline-fixture.invalid");
      const request = {
        target, method: options.method || "GET",
        body: options.body === undefined ? undefined : JSON.parse(options.body),
        headers: options.headers === undefined ? undefined : { ...options.headers },
      };
      requests.push(request);
      if (!authenticated) {
        return { ok: false, status: 401, json: async () => ({ error: "personal_auth_required" }) };
      }
      let data;
      switch (url.pathname) {
        case `${api}/ensure`:
          data = { ok: true, session: { session_url: "https://device.invalid/not-loaded" } };
          break;
        case `${api}/workspace/projects`:
          data = { ok: true, projects: project ? [project] : [] };
          break;
        case `${api}/workspace/project`:
          data = { ok: true, project, tasks: task ? [task] : [] };
          break;
        case `${api}/quality`:
          data = { ok: true, run: {} };
          break;
        case `${api}/handoffs`:
          data = { ok: true, handoffs: [] };
          break;
        case `${api}/recipes`:
          data = { ok: true, recipes: [] };
          break;
        case `${api}/software`:
          data = { ok: true, items: [] };
          break;
        case `${api}/workspace/task/create`:
        case `${api}/workspace/task/update`:
          // Capture the real payload without starting a task or following its result.
          data = { ok: false, error: "offline_fixture_no_execution" };
          break;
        default:
          assert.fail(`unexpected request: ${request.method} ${target}`);
      }
      return { ok: true, status: 200, json: async () => data };
    },
  });
  context.window = context;
  if (windowUid !== undefined) context.clientUid = windowUid;
  if (lexicalUid !== undefined) {
    vm.runInContext(`let clientUid = ${JSON.stringify(lexicalUid)};`, context);
  }
  script.runInContext(context, { timeout: 1000 });
  return {
    context, requests, views, node,
    open: () => context.openCloudDesktopPanel(),
    async click(selector) {
      const handler = node(selector).onclick;
      assert.equal(typeof handler, "function", `${selector} must be wired by the real script`);
      await handler();
    },
    async poll() {
      listeners.get("DOMContentLoaded")();
      await new Promise(setImmediate);
      assert.equal(intervals.length, 1);
      await intervals[0]();
    },
  };
}

function assertServerBound(requests) {
  assert.ok(requests.length > 0, "exercise actual request construction");
  for (const request of requests) {
    const url = new URL(request.target, "https://offline-fixture.invalid");
    assert.equal(url.searchParams.has("uid"), false, `client uid in ${request.target}`);
    if (request.body !== undefined) {
      assert.equal(Object.hasOwn(request.body, "uid"), false,
        `client uid in ${request.method} ${request.target}: ${JSON.stringify(request.body)}`);
    }
  }
}

test("logged-in lexical clientUid does not become a GET/POST identity assertion", async () => {
  const page = desktop({ lexicalUid: "__ut_owner" });
  assert.equal(page.context.clientUid, undefined, "global let is not a window property");
  await page.open();
  assert.ok(page.requests.some((r) => r.method === "GET"));
  assert.ok(page.requests.some((r) => r.method === "POST"));
  assertServerBound(page.requests);
  assert.deepEqual(page.requests.find((r) => r.method === "POST").body, {});
});

for (const [name, lexicalUid] of [["absent", undefined], ["empty", ""]]) {
  test(`${name} page identity never fabricates default and preserves the server auth error`, async () => {
    const page = desktop({ lexicalUid, authenticated: false });
    await page.open();
    assertServerBound(page.requests);
    assert.match(page.views.at(-1).html, /personal_auth_required/);
  });
}

test("a stale window.clientUid cannot override the authenticated lexical subject", async () => {
  const page = desktop({ lexicalUid: "__ut_owner", windowUid: "default" });
  await page.open();
  assertServerBound(page.requests);
});

test("GET requests retain encoded project/task identifiers and existing filters", async () => {
  const page = desktop({
    lexicalUid: "__ut_owner",
    project: { project_id: "项目 /a&b", title: "项目" },
    task: { task_id: "任务 ?c=d", goal: "原目标", state: "running", version: 9 },
  });
  await page.open();
  assertServerBound(page.requests);
  assert.ok(page.requests.some((r) => r.target === `${api}/handoffs?pending_only=1`));
  const project = page.requests.find((r) => r.target.startsWith(`${api}/workspace/project?`));
  const quality = page.requests.find((r) => r.target.startsWith(`${api}/quality?`));
  assert.deepEqual([...new URL(project.target, "https://offline-fixture.invalid").searchParams],
    [["project_id", "项目 /a&b"]]);
  assert.deepEqual([...new URL(quality.target, "https://offline-fixture.invalid").searchParams],
    [["project_id", "项目 /a&b"], ["task_id", "任务 ?c=d"]]);
});

test("POST task creation preserves every non-identity field and its nested execution spec", async () => {
  const page = desktop({
    lexicalUid: "__ut_owner",
    project: { project_id: "__ut_project", title: "原项目" },
  });
  await page.open();
  page.node("#cdTaskGoal").value = "继续原任务 & 保留输入";
  await page.click("#cdTaskSave");
  const request = page.requests.find((r) => r.target === `${api}/workspace/task/create`);
  assert.equal(request.method, "POST");
  assert.deepEqual(request.headers, { "Content-Type": "application/json" });
  assert.deepEqual(request.body, {
    project_id: "__ut_project", goal: "继续原任务 & 保留输入",
    session_id: "__ut_session", team_id: "__ut_team",
    execution_spec: { kind: "quality_delivery", max_quality_rounds: 3, quality_wall_clock_s: 7200 },
  });
  assertServerBound(page.requests);
});

test("POST task updates preserve version and checkpoint instead of replacing the body", async () => {
  const page = desktop({
    lexicalUid: "__ut_owner",
    project: { project_id: "__ut_project", title: "原项目" },
    task: { task_id: "__ut_task", goal: "原目标", state: "running", version: 9 },
  });
  await page.open();
  page.node("#cdSay").value = "沿原现场继续";
  await page.click("#cdAsk");
  const request = page.requests.find((r) => r.target === `${api}/workspace/task/update`);
  assert.deepEqual(request.body, {
    project_id: "__ut_project", task_id: "__ut_task", state: "running",
    phase: "军团执行中", version: 9,
    checkpoint: { goal: "沿原现场继续", session_id: "__ut_session", team_id: "__ut_team" },
  });
  assertServerBound(page.requests);
});

test("background handoff polling uses the same server-bound GET transport", async () => {
  const page = desktop({ lexicalUid: "__ut_owner" });
  await page.poll();
  assert.equal(page.requests.length, 2);
  assert.ok(page.requests.every((r) => r.method === "GET"));
  assertServerBound(page.requests);
  assert.ok(page.requests.every((r) => r.target === `${api}/handoffs?pending_only=1`));
});
