import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, realpath, rm, symlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

async function moduleUnderTest() {
  const mod = await import("../external_adapters/ohmygame_protocol.mjs").catch(() => null);
  assert.ok(mod, "OhMyGame native collaboration client has not been implemented");
  return mod;
}

test("daemon credentials may only target the local native runtime", async () => {
  const { validateRuntime } = await moduleUnderTest();
  assert.equal(validateRuntime({ daemonUrl: "http://127.0.0.1:43111", token: "local-fixture" }).daemonUrl,
    "http://127.0.0.1:43111");
  for (const url of ["https://remote.invalid", "http://127.0.0.1.evil.invalid:80",
    "http://user:pass@127.0.0.1:43111", "http://127.0.0.1:43111/?token=x"]) {
    assert.throws(() => validateRuntime({ daemonUrl: url, token: "local-fixture" }));
  }
});

test("no connected model means no project or turn is submitted", async () => {
  const { nativeAction } = await moduleUnderTest();
  const calls = [];
  const request = async (path, method = "GET") => {
    calls.push([path, method]);
    return { models: [] };
  };
  const result = await nativeAction(request, "delegate", { prompt: "原始游戏需求" });
  assert.equal(result.ok, false);
  assert.equal(result.status, "needs_model");
  assert.deepEqual(calls, [["/models", "GET"]]);
});

test("continuation preserves native IDs and the complete original prompt", async () => {
  const { nativeAction } = await moduleUnderTest();
  const calls = [];
  const original = "原始要求\n不要换掉已有工程；真实反馈：点击技能没有反应。";
  const request = async (path, method = "GET", body) => {
    calls.push({ path, method, body });
    if (path === "/models") return { models: [{ provider: "fixture", id: "model" }] };
    if (path === "/projects/project-one") return { id: "project-one", workspacePath: "/tmp/fixture" };
    if (path.endsWith("/turns")) return { turnId: "turn-two", queued: true };
    throw new Error("Unexpected extra operation");
  };
  const result = await nativeAction(request, "continue", {
    projectId: "project-one", conversationId: "conversation-one", prompt: original,
    references: [{ type: "workspace-file", path: "index.html" }],
  });
  assert.equal(result.status, "accepted");
  assert.equal(result.completed, false);
  assert.equal(result.conversationId, "conversation-one");
  assert.equal(calls.filter((r) => r.method === "POST").length, 1);
  assert.deepEqual(calls.at(-1).body, {
    prompt: original, references: [{ type: "workspace-file", path: "index.html" }],
  });
});

test("native project reuse does not replace an existing game workspace", async () => {
  const { nativeAction } = await moduleUnderTest();
  const temporary = await mkdtemp(path.join(os.tmpdir(), "ohmygame-workspace-"));
  const workspace = await realpath(temporary);
  const calls = [];
  const request = async (path, method = "GET", body) => {
    calls.push({ path, method, body });
    if (path === "/models") return { models: [{ provider: "fixture", id: "model" }] };
    if (path === "/projects") return [{ id: "existing", workspacePath: workspace }];
    if (path === "/projects/existing/conversations") return { id: "native-conversation" };
    if (path.endsWith("/turns")) return { turnId: "new-turn", queued: false };
    throw new Error("Unexpected operation");
  };
  try {
    const result = await nativeAction(request, "delegate", {
      workspacePath: workspace, prompt: "继续优化操作体验",
    });
    assert.equal(result.projectId, "existing");
    assert.equal(calls.some((r) => r.path === "/projects" && r.method === "POST"), false);
  } finally { await rm(temporary, { recursive: true, force: true }); }
});

test("uncertain submission errors are not retried", async () => {
  const { nativeAction } = await moduleUnderTest();
  let posts = 0;
  const request = async (path, method = "GET") => {
    if (method === "POST") { posts++; throw new Error("connection lost after send"); }
    return path === "/models" ? { models: [{}] } : { id: "p" };
  };
  await assert.rejects(nativeAction(request, "delegate", {
    projectId: "p", conversationId: "c", prompt: "继续",
  }));
  assert.equal(posts, 1);
});

test("result returns native failure facts rather than claiming completion", async () => {
  const { nativeAction } = await moduleUnderTest();
  const detail = { agent: { status: "error", error: "provider unavailable" },
    conversation: { id: "c" }, turns: [{ id: "t", status: "failed" }] };
  const result = await nativeAction(async () => detail, "result", {
    projectId: "p", conversationId: "c",
  });
  assert.deepEqual(result.native, detail);
  assert.equal(result.completed, undefined);
  assert.equal(result.status, "observed");
});

test("waits for the owned renderer and current daemon without a second app", async () => {
  const { connectRuntime } = await moduleUnderTest();
  let attempts = 0;
  let reads = 0;
  const runtime = await connectRuntime(43111, "/owned/OhMyGame.app", {
    fetcher: async () => new Response(JSON.stringify(++attempts < 2 ? [] : [{
      type: "page", url: "file:///owned/OhMyGame.app/Contents/Resources/app.asar/dist/renderer/index.html",
      webSocketDebuggerUrl: "ws://127.0.0.1:43111/devtools/page/owned",
    }])),
    readRuntime: async () => ++reads === 1 ? null : ({
      daemonUrl: "http://127.0.0.1:43112", token: "fixture-only",
    }),
    wait: async () => {}, timeoutMs: 1000,
  });
  assert.equal(attempts, 3);
  assert.equal(reads, 2);
  assert.equal(runtime.daemonUrl, "http://127.0.0.1:43112");
});

test("canonical workspace aliases reuse the native project identity", async () => {
  const { nativeAction } = await moduleUnderTest();
  const temporary = await mkdtemp(path.join(os.tmpdir(), "ohmygame-alias-"));
  try {
    const physical = path.join(temporary, "physical");
    await mkdir(path.join(physical, "game"), { recursive: true });
    await symlink(physical, path.join(temporary, "alias"), "dir");
    const canonical = await realpath(path.join(physical, "game"));
    let recreated = false;
    const request = async (route, method = "GET") => {
      if (route === "/models") return { models: [{}] };
      if (route === "/projects" && method === "GET") return [{ id: "original", workspacePath: canonical }];
      if (route === "/projects") { recreated = true; return { id: "duplicate" }; }
      if (route.endsWith("/conversations")) return { id: "conversation" };
      return { turnId: "turn", queued: false };
    };
    const result = await nativeAction(request, "delegate", {
      workspacePath: path.join(temporary, "alias", "game"), prompt: "continue original project",
    });
    assert.equal(recreated, false);
    assert.equal(result.projectId, "original");
  } finally { await rm(temporary, { recursive: true, force: true }); }
});

test("server errors after turn submission retain unknown rather than rejected", async () => {
  const { nativeAction } = await moduleUnderTest();
  const request = async (route, method = "GET") => {
    if (method === "POST") {
      const error = new Error("after acceptance failure");
      error.httpStatus = 503;
      throw error;
    }
    return route === "/models" ? { models: [{}] } : { id: "p" };
  };
  await assert.rejects(nativeAction(request, "delegate", {
    projectId: "p", conversationId: "c", prompt: "original",
  }), (error) => error.nativeContext?.submission === "unknown");
});

test("receipt retains known native IDs before attempting a turn POST", async () => {
  const { nativeAction } = await moduleUnderTest();
  const receipt = [];
  const request = async (route, method = "GET") => {
    if (method === "POST") {
      assert.equal(receipt.at(-1)?.stage, "turn_submission");
      assert.equal(receipt.at(-1)?.projectId, "p");
      assert.equal(receipt.at(-1)?.conversationId, "c");
      assert.equal(receipt.at(-1)?.submission, "unknown");
      return { turnId: "t", queued: false };
    }
    return route === "/models" ? { models: [{}] } : { id: "p" };
  };
  const result = await nativeAction(request, "delegate", {
    projectId: "p", conversationId: "c", prompt: "original",
  }, async (value) => receipt.push(value));
  assert.equal(result.status, "accepted");
  assert.equal(receipt.at(-1).submission, "accepted");
});
