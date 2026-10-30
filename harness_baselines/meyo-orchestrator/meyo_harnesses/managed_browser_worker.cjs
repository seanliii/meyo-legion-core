#!/usr/bin/env node
'use strict';
// One parent-owned lease, one private browser/context, one native MCP connection.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {validateHeaderValue} = require('node:http');
const {createRequire} = require('node:module');
const {loadMaterials, prepareRuntime, DEFAULT_ACTION_TIMEOUT_MS} = require('./playwright_runtime.cjs');

const ARTIFACT_HEADER = 'X-Meyo-Browser-Artifact';

function actionTimeout(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config))
    throw new Error('invalid managed browser config');
  if (!Object.prototype.hasOwnProperty.call(config, 'action_timeout_ms'))
    return DEFAULT_ACTION_TIMEOUT_MS;
  const value = config.action_timeout_ms;
  if (!Number.isInteger(value) || value < 1 || value > 60000)
    throw new Error('invalid managed browser config');
  return value;
}

function readConfig(file = process.env.MEYO_MANAGED_BROWSER_CONFIG) {
  let fd;
  try {
    if (typeof file !== 'string' || !path.isAbsolute(file))
      throw new Error();
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const info = fs.fstatSync(fd);
    if (!info.isFile() || (info.mode & 0o7777) !== 0o600 || info.size > 16384 ||
        (process.getuid && info.uid !== process.getuid()))
      throw new Error();
    const config = JSON.parse(fs.readFileSync(fd, 'utf8'));
    const fields = new Set(['schema_version', 'artifact_origin', 'artifact_token', 'output_dir',
      'action_timeout_ms']);
    if (!config || Array.isArray(config) || config.schema_version !== 1 ||
        Object.keys(config).some(key => !fields.has(key)))
      throw new Error();
    actionTimeout(config);
    const origin = new URL(config.artifact_origin);
    const loopback = origin.hostname === '[::1]' ||
      (net.isIP(origin.hostname) === 4 && origin.hostname.startsWith('127.'));
    if (!loopback || origin.protocol !== 'http:' || origin.origin !== config.artifact_origin)
      throw new Error();
    if (typeof config.artifact_token !== 'string' || !config.artifact_token ||
        config.artifact_token.length > 4096)
      throw new Error();
    validateHeaderValue(ARTIFACT_HEADER, config.artifact_token);
    if (typeof config.output_dir !== 'string' || !path.isAbsolute(config.output_dir) ||
        config.output_dir === path.parse(config.output_dir).root ||
        !fs.lstatSync(config.output_dir).isDirectory())
      throw new Error();
    return config;
  } catch {
    // JSON/parser/header errors may contain the parent credential. Never echo them.
    throw new Error('invalid managed browser config');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function loadPinnedRuntime(materials) {
  if (materials.version !== '0.0.79' || !materials.executable)
    throw new Error('managed browser requires pinned MCP 0.0.79 and an explicit browser material');
  const requirePinned = createRequire(materials.cli);
  const core = requirePinned.resolve('playwright-core/package.json');
  // This file exports the transport; the ".js" package subpath is not exported.
  const {StdioServerTransport} = requirePinned(path.join(path.dirname(core), 'lib/utilsBundle.js'));
  const {createConnection} = requirePinned(path.join(path.dirname(materials.cli), 'index.js'));
  const {chromium} = requirePinned('playwright-core');
  if (typeof createConnection !== 'function' || typeof StdioServerTransport !== 'function' ||
      typeof chromium?.launch !== 'function')
    throw new Error('pinned managed browser API is unavailable');
  return {chromium, createConnection, StdioServerTransport};
}

function artifactRoute(config) {
  return async route => {
    const request = route.request();
    let url;
    try { url = new URL(request.url()); }
    catch { return route.abort('blockedbyclient'); }
    if (url.protocol === 'file:') return route.abort('blockedbyclient');
    const headers = Object.fromEntries(Object.entries(request.headers())
      .filter(([key]) => key.toLowerCase() !== ARTIFACT_HEADER.toLowerCase()));
    if (url.origin !== config.artifact_origin)
      return route.continue({headers});
    try {
      // continue({headers}) also forwards overrides across redirects in this
      // pinned Playwright. Keep the credential out of the browser request:
      // fetch only this exact origin, then let a redirect create a fresh request.
      const response = await route.fetch({
        headers: {...headers, [ARTIFACT_HEADER]: config.artifact_token},
        maxRedirects: 0,
      });
      try { await route.fulfill({response}); }
      finally { await response.dispose(); }
    } catch {
      // route.fetch errors include request headers. Surface a normal failed
      // browser request instead of returning that credential-bearing diagnostic.
      await route.abort('failed').catch(() => {});
    }
  };
}

function untilStopped(promise, signal) {
  if (!signal) return promise;
  return new Promise((resolve, reject) => {
    const stopped = () => reject(new Error('managed browser lease stopped'));
    if (signal.aborted) stopped();
    else signal.addEventListener('abort', stopped, {once: true});
    // The request was started before this helper was called; drain it even if
    // cancellation already won, so closing CDP cannot cause an unhandled rejection.
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', stopped));
  });
}

async function createManagedSession(config, materials, dependencies, io = {}) {
  const actionTimeoutMs = actionTimeout(config);
  if (dependencies === undefined) dependencies = loadPinnedRuntime(materials);
  let browser, context, server, creating, disposing, closing;
  let closed = false;
  const lifetime = new AbortController();
  const stop = () => lifetime.abort();
  io.signal?.addEventListener('abort', stop, {once: true});
  if (io.signal?.aborted) stop();

  function disposeOwned() {
    if (!browser && !context) return Promise.resolve();
    if (!disposing) {
      disposing = (async () => {
        try { await context?.close(); }
        finally { await browser?.close(); }
      })();
    }
    return disposing;
  }

  async function close() {
    if (closed) return closing;
    closed = true;
    stop();
    closing = (async () => {
      let error;
      try { await server?.close(); } catch (e) { error = e; }
      // Close an existing browser promptly, including an in-flight CDP call.
      // If launch itself is pending, its continuation owns and closes the result.
      if (browser) {
        try { await disposeOwned(); } catch (e) { error ||= e; }
      }
      await creating?.catch(() => {});
      try { await disposeOwned(); } catch (e) { error ||= e; }
      io.signal?.removeEventListener('abort', stop);
      if (error) throw error;
    })();
    return closing;
  }

  async function createOwnedContext() {
    try {
      if (lifetime.signal.aborted) throw new Error('managed browser lease stopped');
      // Native MCP calls this getter on its first tools/call, never for
      // initialize/list_tools. The parent already holds its browser resource lock.
      browser = await dependencies.chromium.launch({
        executablePath: materials.executable, headless: true, chromiumSandbox: true,
        env: io.env || process.env,
      });
      if (lifetime.signal.aborted) throw new Error('managed browser lease stopped');
      browser.once('disconnected', () => {
        if (!closed) void server.close().catch(() => {});
      });
      context = await browser.newContext({serviceWorkers: 'block', acceptDownloads: true});
      if (lifetime.signal.aborted) throw new Error('managed browser lease stopped');
      await context.route('**/*', artifactRoute(config));
      const cdp = await browser.newBrowserCDPSession();
      let processes;
      try {
        processes = await untilStopped(cdp.send('SystemInfo.getProcessInfo'), lifetime.signal);
      } finally {
        await cdp.detach();
      }
      const owned = processes.processInfo?.filter(item => item.type === 'browser') || [];
      if (owned.length !== 1 || !Number.isSafeInteger(owned[0].id) || owned[0].id <= 0)
        throw new Error('managed browser process identity unavailable');
      await untilStopped(server.notification({
        method: 'notifications/meyo/browser_process', params: {pid: owned[0].id, kind: 'browser'},
      }), lifetime.signal);
      return context;
    } catch (error) {
      await disposeOwned();
      throw error;
    }
  }

  function contextGetter() {
    if (closed || lifetime.signal.aborted)
      return Promise.reject(new Error('managed browser lease is closed'));
    // Cache failure too. A failed/closed lease never silently creates a new profile.
    if (!creating) creating = createOwnedContext();
    return creating;
  }

  try {
    server = await dependencies.createConnection({
      browser: {browserName: 'chromium', isolated: false},
      capabilities: ['core', 'vision'],
      timeouts: {action: actionTimeoutMs},
      outputDir: config.output_dir,
      allowUnrestrictedFileAccess: false, imageResponses: 'allow', saveSession: false,
    }, contextGetter);
    const transport = new dependencies.StdioServerTransport(io.stdin || process.stdin, io.stdout || process.stdout);
    await server.connect(transport);
    return {server, close};
  } catch (error) {
    await close();
    throw error;
  }
}

async function main() {
  if (process.argv.length !== 2) throw new Error('managed browser accepts only parent configuration');
  const config = readConfig();
  const materials = loadMaterials();
  const dependencies = loadPinnedRuntime(materials);
  const prepared = prepareRuntime(materials);
  const abort = new AbortController();
  let stop;
  const stopped = new Promise(resolve => { stop = resolve; });
  const requestStop = () => { abort.abort(); stop(); };
  const listeners = [
    [process, 'SIGTERM'], [process, 'SIGINT'], [process.stdin, 'end'],
    [process.stdin, 'close'], [process.stdin, 'error'], [process.stdout, 'error'],
  ];
  for (const [emitter, event] of listeners) emitter.once(event, requestStop);
  let session;
  try {
    Object.assign(process.env, prepared.env);
    process.chdir(config.output_dir);
    if (process.stdin.readableEnded || process.stdin.destroyed) requestStop();
    if (abort.signal.aborted) return;
    session = await createManagedSession(config, materials, dependencies, {
      stdin: process.stdin, stdout: process.stdout, env: prepared.env, signal: abort.signal,
    });
    const previousClose = session.server.onclose;
    session.server.onclose = () => { previousClose?.(); requestStop(); };
    await stopped;
  } catch (error) {
    if (!abort.signal.aborted) throw error;
  } finally {
    try { await session?.close(); }
    finally {
      for (const [emitter, event] of listeners) emitter.off(event, requestStop);
      prepared.cleanup();
    }
  }
}

if (require.main === module) {
  main().then(
    () => { process.exitCode = 0; },
    () => {
      // Raw native/transport errors can contain credentials and private inputs.
      console.error('managed_browser_unavailable: owned browser session could not complete');
      process.exitCode = 78;
    });
}

module.exports = {readConfig, loadPinnedRuntime, artifactRoute, createManagedSession};
