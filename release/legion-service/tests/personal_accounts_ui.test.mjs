/**
 * 真实浏览器 + 真实页面；API 为内存测试替身，不是后端全链验收。
 *
 * 本仓库没有 Node 依赖清单，浏览器测试明确为 opt-in：
 * - 默认 node --test 不加载 Playwright，不启动浏览器，逐项报告 SKIP（不是通过）。
 * - MEYO_BROWSER_TESTS=1 才执行；启用后缺依赖/浏览器或断言失败均报错，绝不静默跳过。
 * - 默认按本测试文件位置解析包名 playwright；PLAYWRIGHT_MODULE 可显式指定模块
 *   的绝对路径或 file: URL。不扫描全局包、不回退到其他项目、不自动安装依赖。
 *
 * 从仓库根目录运行默认测试（无需任何新增依赖）：
 *   node --test --test-concurrency=1 release/legion-service/tests/personal_accounts_ui.test.mjs
 *
 * 可复现的浏览器测试准备：固定 Playwright 版本，安装到独立缓存，不改仓库文件。
 *   PW_RUNTIME="$HOME/.cache/meyo-personal-ui-tests/playwright-1.58.2"
 *   mkdir -p "$PW_RUNTIME"
 *   npm install --prefix "$PW_RUNTIME" --no-save --package-lock=false --ignore-scripts \
 *     --no-audit --no-fund playwright@1.58.2
 *
 * 本机已安装 Google Chrome 时：
 *   MEYO_BROWSER_TESTS=1 PLAYWRIGHT_CHANNEL=chrome \
 *     PLAYWRIGHT_MODULE="$PW_RUNTIME/node_modules/playwright/index.mjs" \
 *     node --test --test-concurrency=1 release/legion-service/tests/personal_accounts_ui.test.mjs
 *
 * 没有 Chrome 时，显式安装匹配版本的 Chromium 后运行（不设置 PLAYWRIGHT_CHANNEL）：
 *   node "$PW_RUNTIME/node_modules/playwright/cli.js" install chromium
 *   env -u PLAYWRIGHT_CHANNEL MEYO_BROWSER_TESTS=1 \
 *     PLAYWRIGHT_MODULE="$PW_RUNTIME/node_modules/playwright/index.mjs" \
 *     node --test --test-concurrency=1 release/legion-service/tests/personal_accounts_ui.test.mjs
 *
 * 全部 JS 回归：以上运行命令末尾改成 release/legion-service/tests/*.test.mjs。
 * 可选 PERSONAL_UI_SCREENSHOT=/absolute/path.png 保存手机截图。
 * 浏览器始终显式 chromiumSandbox: true；不提供无沙箱回退。
 * 测试不会触碰个人真实账号、Key 或模型；依赖安装与浏览器下载只在显式准备时进行。
 */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';

const browserTestsEnabled = process.env.MEYO_BROWSER_TESTS === '1';
let chromium;
if (browserTestsEnabled) {
  try {
    ({ chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright'));
    if (typeof chromium?.launch !== 'function') throw new Error('模块未导出 chromium.launch');
  } catch (cause) {
    throw new Error(
      '已显式启用个人账号浏览器测试，但无法加载 Playwright。请按本文件顶部说明安装固定版本，' +
      '并设置 PLAYWRIGHT_MODULE 为该独立运行时的 index.mjs；未回退到其他项目。',
      { cause },
    );
  }
}
const browserTest = (name, fn) => test(name, {
  skip: !browserTestsEnabled && '未启用浏览器测试（不是通过）；设置 MEYO_BROWSER_TESTS=1，依赖准备见本文件顶部。',
}, fn);
const html = readFileSync(new URL('../web/index_v2.html', import.meta.url), 'utf8');
const blockedNote = '账号和Key设置可用；独立执行环境尚未接通，不会调用站长模型额度';
let browser, server, origin;
before(async () => {
  if (!browserTestsEnabled) return;
  server = createServer((req, res) => {
    if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({
    headless: true,
    chromiumSandbox: true,
    // CDP 参数回读要求此标志；不依赖不同 Playwright 发行包的默认启动参数。
    args: ['--enable-automation'],
    ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}),
  });
  const session = await browser.newBrowserCDPSession();
  try {
    const { arguments: args } = await session.send('Browser.getBrowserCommandLine');
    assert.ok(!args.some(arg => /^--(?:no-sandbox|disable-setuid-sandbox)(?:=|$)/.test(arg)),
      '浏览器启动参数不得关闭沙箱');
  } finally { await session.detach(); }
});
after(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

const secret = () => 'fixture-' + randomBytes(18).toString('hex');
const account = (name = 'fixture_user', execution = false) => ({
  authenticated: true, user_id: '__ut_' + name, username: name, method: 'password',
  password_set: true, execution_enabled: execution, execution_note: execution ? '' : blockedNote,
});
async function fixture(t, { identity = null, viewport, url } = {}) {
  const context = await browser.newContext({ viewport: viewport || { width: 1280, height: 900 } });
  t.after(() => context.close());
  const state = { identity, profiles: [], requests: [], errors: [], hold: null, failure: null };
  await context.route('**/api/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    const body = request.postDataJSON();
    state.requests.push({ pathname, method: request.method(), body, url: request.url() });
    const reply = (data, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(data),
    }).catch(() => {});
    if (state.failure?.pathname === pathname) return reply({ error: state.failure.error }, state.failure.status);
    if (pathname.endsWith('/auth/status')) return reply(state.identity || { authenticated: false });
    if (pathname.endsWith('/auth/register') || pathname.endsWith('/auth/login')) {
      state.identity = account(body.username);
      return reply(state.identity);
    }
    if (pathname.endsWith('/auth/session')) {
      state.identity = { ...account('legacy_owner', true), method: 'existing_personal_token', password_set: false };
      return reply(state.identity);
    }
    if (pathname.endsWith('/auth/logout')) { state.identity = null; return reply({ authenticated: false }); }
    if (pathname.endsWith('/auth/password')) {
      state.identity.password_set = true; state.identity.method = 'password';
      return reply({ ...state.identity });
    }
    if (pathname.endsWith('/my/models/delete')) {
      state.profiles = state.profiles.filter(p => p.id !== body.id); return reply({ ok: true });
    }
    if (pathname.endsWith('/my/models')) {
      if (request.method() === 'POST') {
        const { api_key, uid, ...profile } = body;
        assert.ok(api_key, 'the browser must send an explicit key only in the POST body');
        state.profiles = [...state.profiles.filter(p => p.id !== profile.id),
          { ...profile, key_configured: true, provider_verified: false }];
        return reply({ ok: true });
      }
      const result = { profiles: [...state.profiles], execution_enabled: false };
      if (state.hold) { const hold = state.hold; state.hold = null; await hold; }
      return reply(result);
    }
    if (pathname.endsWith('/session-index')) return reply({ sessions: [], artifacts: [] });
    if (pathname.endsWith('/harnesses')) return reply({ answer_pool: ['codex'], harnesses: [],
      entry_identity_verified: !!state.identity, entry_identity_user_id: state.identity?.user_id || '' });
    if (pathname.endsWith('/verification/access')) return reply({ ok: false });
    return reply({ ok: false, error: 'fixture_endpoint_not_available' });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  page.on('pageerror', e => state.errors.push(e.message));
  await page.goto(url || origin);
  await page.waitForFunction(() => typeof _personalSubject !== 'undefined');
  await page.waitForTimeout(180);
  return { context, page, state };
}
async function passwordSignIn(page, mode = 'login', name = 'fixture_user') {
  await page.locator('#whoAmI').click();
  await page.locator(mode === 'register' ? '#personalRegisterMode' : '#personalLoginMode').click();
  await page.locator('#personalUsername').fill(name);
  await page.locator('#personalPassword').fill(secret());
  await page.locator('#personalPasswordSubmit').click();
  await page.waitForFunction(() => !document.querySelector('#personalAuthDialog').open);
}
async function fillModel(page, overrides = {}) {
  await page.locator('#myModelId').fill(overrides.id || 'fixture-model');
  await page.locator('#myModelProvider').selectOption(overrides.provider || 'deepseek');
  await page.locator('#myModelName').fill(overrides.model || 'fixture-model-name');
  await page.locator('#myModelBaseUrl').fill(overrides.base_url || 'https://api.deepseek.com/v1');
  await page.locator('#myModelKey').fill(secret());
}
const postCount = state => state.requests.filter(r => r.method === 'POST').length;

browserTest('register, visible settings entry, execution disabled and no implicit task dispatch', async t => {
  const { page, state } = await fixture(t);
  await passwordSignIn(page, 'register');
  assert.ok(await page.locator('#myModelsButton').isVisible());
  assert.ok((await page.locator('#personalExecutionNotice').textContent()).includes(blockedNote));
  assert.ok(await page.locator('#send').isDisabled());
  await page.evaluate(() => { qEl.value = 'fixture task, must not execute'; return send(); });
  assert.equal(state.requests.filter(r => r.method === 'POST').length, 1);
  assert.ok(!state.requests.some(r => /dispatch|run_one|cloud_desktop/.test(r.pathname)));
  assert.deepEqual(state.errors, []);
});

browserTest('password login and logout clear personal views and sensitive fields', async t => {
  const { page, state } = await fixture(t);
  await passwordSignIn(page);
  assert.ok(state.requests.some(r => r.pathname.endsWith('/auth/login')));
  await page.locator('#myModelsButton').click();
  await page.locator('#myModelKey').fill(secret());
  await page.locator('#myModelsClose').click();
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  await page.locator('#whoAmI').click();
  await page.locator('#personalLogout').click();
  await page.waitForFunction(() => !_personalSubject);
  assert.equal(await page.locator('#q').inputValue(), '');
  assert.ok((await page.locator('#whoAmI').textContent()).includes('登录'));
  assert.ok(!await page.locator('#myModelsDialog').evaluate(e => e.open));
  assert.ok(state.requests.some(r => r.pathname.endsWith('/auth/logout')));
  assert.deepEqual(state.errors, []);
});

browserTest('model CRUD masks keys, escapes content, never persists secrets and clears on save/close', async t => {
  const { page, state } = await fixture(t, { identity: account() });
  await page.locator('#myModelsButton').click();
  assert.equal(await page.locator('#myModelKey').getAttribute('type'), 'password');
  await fillModel(page, { provider: 'custom', model: '<img src=x onerror=alert(1)>' });
  await page.locator('#myModelSave').click();
  await page.waitForFunction(() => document.querySelector('#myModelList').textContent.includes('fixture-model'));
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  assert.equal(await page.locator('#myModelId').inputValue(), '');
  assert.equal(await page.locator('#myModelList img').count(), 0);
  assert.ok((await page.locator('#myModelList').textContent()).includes('未验证'));
  assert.ok((await page.locator('#myModelsDialog').textContent()).includes('不激活'));
  const writes = state.requests.filter(r => r.pathname.endsWith('/my/models') && r.method === 'POST');
  assert.equal(writes.length, 1);
  const key = writes[0].body.api_key;
  assert.ok(!state.requests.some(r => r.url.includes(key)));
  assert.ok(!await page.evaluate(key => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(key), key));
  await page.locator('[data-model-edit]').click();
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  await page.locator('#myModelKey').fill(secret());
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  await page.locator('#myModelsButton').click();
  page.once('dialog', d => d.accept());
  await page.locator('[data-model-delete]').click();
  await page.waitForFunction(() => document.querySelectorAll('[data-model-delete]').length === 0);
  assert.equal(state.profiles.length, 0);
  assert.deepEqual(state.errors, []);
});

browserTest('legacy credential stays collapsed and owner can bind a password without overwriting via token', async t => {
  const { page, state } = await fixture(t);
  await page.locator('#whoAmI').click();
  assert.equal(await page.locator('#personalLegacy').getAttribute('open'), null);
  await page.locator('#personalLegacy summary').click();
  await page.locator('#personalToken').fill(secret());
  await page.locator('#personalAuthSubmit').click();
  await page.waitForFunction(() => !document.querySelector('#personalAuthDialog').open);
  assert.ok(!await page.locator('#send').isDisabled());
  await page.locator('#whoAmI').click();
  assert.ok(await page.locator('#personalPasswordSettings').isVisible());
  assert.ok(!await page.locator('#personalCurrentPassword').isVisible());
  await page.locator('#personalNewPassword').fill(secret());
  await page.locator('#personalSetPasswordSubmit').click();
  await page.waitForFunction(() => document.querySelector('#personalPasswordSettingsMessage').textContent.includes('已'));
  const write = state.requests.find(r => r.pathname.endsWith('/auth/password'));
  assert.ok(write && !write.body.current_password);
  assert.equal(await page.locator('#personalNewPassword').inputValue(), '');
  assert.ok(await page.locator('#personalCurrentPassword').isVisible());
  // Re-enter using the old token: an existing password must not be overwritten.
  state.identity.method = 'existing_personal_token';
  await page.locator('#personalAuthCancel').click();
  await page.evaluate(() => _readPersonalStatus());
  await page.locator('#whoAmI').click();
  assert.ok(await page.locator('#personalSetPasswordSubmit').isDisabled());
  assert.deepEqual(state.errors, []);
});

browserTest('password length, model ID and model name limits are validated before POST', async t => {
  const { page, state } = await fixture(t);
  await page.locator('#whoAmI').click();
  await page.locator('#personalUsername').fill('fixture_limits');
  await page.locator('#personalPassword').fill('x'.repeat(5));
  await page.locator('#personalPasswordForm').evaluate(form => form.dispatchEvent(new Event('submit', { cancelable: true })));
  assert.equal(postCount(state), 0);
  await page.locator('#personalPassword').evaluate(e => e.value = 'x'.repeat(129));
  await page.locator('#personalPasswordForm').evaluate(form => form.dispatchEvent(new Event('submit', { cancelable: true })));
  assert.equal(postCount(state), 0);
  await page.locator('#personalAuthCancel').click();
  await passwordSignIn(page);
  await page.locator('#myModelsButton').click();
  const count = postCount(state);
  for (const id of ['Invalid ID', 'a'.repeat(49)]) {
    await fillModel(page);
    await page.locator('#myModelId').evaluate((e, id) => e.value = id, id);
    await page.locator('#myModelForm').evaluate(form => form.dispatchEvent(new Event('submit', { cancelable: true })));
    assert.equal(postCount(state), count);
  }
  await fillModel(page);
  await page.locator('#myModelName').evaluate(e => e.value = 'm'.repeat(161));
  await page.locator('#myModelForm').evaluate(form => form.dispatchEvent(new Event('submit', { cancelable: true })));
  assert.equal(postCount(state), count);
});

browserTest('public HTTP refuses passwords, old credentials and API keys', async t => {
  const { page, state } = await fixture(t, { identity: account(), url: undefined });
  await page.route('http://untrusted.invalid/', route => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto('http://untrusted.invalid/');
  await page.waitForTimeout(200);
  await page.locator('#whoAmI').click();
  assert.ok(await page.locator('#personalPassword').isDisabled());
  assert.ok(await page.locator('#personalToken').isDisabled());
  await page.locator('#personalAuthCancel').click();
  await page.locator('#myModelsButton').click();
  assert.ok(await page.locator('#myModelKey').isDisabled());
  assert.equal(postCount(state), 0);
});

browserTest('cross-tab identity change clears key fields and rejects late model response', async t => {
  const { page, state } = await fixture(t, { identity: account('alice') });
  let release;
  state.hold = new Promise(resolve => release = resolve);
  state.profiles = [{ id: 'alice-only', provider: 'custom', model: 'private model', key_configured: true }];
  await page.locator('#myModelsButton').click();
  await page.locator('#myModelKey').fill(secret());
  state.identity = account('bob');
  await page.evaluate(() => _checkFocusedIdentity());
  release();
  await page.waitForTimeout(120);
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  assert.ok(!await page.locator('#myModelsDialog').evaluate(e => e.open));
  assert.ok(!(await page.locator('#myModelList').textContent()).includes('alice-only'));
  assert.equal(await page.evaluate(() => _personalSubject), '');
  assert.deepEqual(state.errors, []);
});

browserTest('switching identities restores only their own same-tab draft', async t => {
  const { page, state } = await fixture(t, { identity: account('alice', true) });
  await page.locator('#q').fill('only Alice draft');
  await passwordSignIn(page, 'login', 'bob');
  assert.equal(await page.locator('#q').inputValue(), '');
  state.identity = account('alice', true);
  await page.evaluate(() => _readPersonalStatus().then(_applyPersonalSubject));
  assert.equal(await page.locator('#q').inputValue(), 'only Alice draft');
  assert.deepEqual(state.errors, []);
});

browserTest('failed save clears the key, shows a safe error, and close drops a late response', async t => {
  const { page, state } = await fixture(t, { identity: account() });
  await page.locator('#myModelsButton').click();
  await fillModel(page);
  state.failure = { pathname: '/api/legion/my/models', error: 'fixture_private_detail', status: 500 };
  await page.locator('#myModelSave').click();
  await page.waitForFunction(() => document.querySelector('#myModelsMessage').textContent.includes('失败'));
  assert.equal(await page.locator('#myModelKey').inputValue(), '');
  assert.ok(!(await page.locator('#myModelsMessage').textContent()).includes('fixture_private_detail'));
  state.failure = null;
  let release;
  state.hold = new Promise(resolve => release = resolve);
  await page.locator('#myModelsClose').click();
  await page.locator('#myModelsButton').click();
  await page.locator('#myModelsClose').click();
  release();
  await page.waitForTimeout(80);
  assert.ok(!await page.locator('#myModelsDialog').evaluate(e => e.open));
  assert.equal(await page.locator('#myModelList').textContent(), '');
});

browserTest('mobile login and model settings remain within the viewport', async t => {
  const { page, state } = await fixture(t, { viewport: { width: 390, height: 844 } });
  await page.locator('#mobileSessionToggle').click();
  await passwordSignIn(page);
  await page.locator('#myModelsButton').click();
  const bounds = await page.locator('#myModelsDialog').boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 391);
  assert.ok(bounds.height <= 844);
  await fillModel(page);
  await page.locator('#myModelSave').click();
  await page.waitForFunction(() => document.querySelectorAll('[data-model-delete]').length === 1);
  if (process.env.PERSONAL_UI_SCREENSHOT) await page.screenshot({ path: process.env.PERSONAL_UI_SCREENSHOT });
  assert.deepEqual(state.errors, []);
});
