import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { LegionClient } from '../lib/client.js'
import { formatLegionResult } from '../lib/commands.js'
import { browserFixture, packageId } from './browser-fixture.js'

const taskRef = '1cfad24ce181d2a997d0710b986aa802'
const accepted = () => Object.freeze({
  kind: 'success',
  text: formatLegionResult('start', { task_ref: taskRef, submission: 'accepted' }),
})

test('the official loader discovers and runs a browser-only client bundle without forwarding Host config', async t => {
  const fixture = await browserFixture(t)
  const { ctx, graph, modules, served, host, hostEntryId, hostOnlyConfig } = fixture
  const row = graph.entries.find(entry => entry.id === packageId)
  assert.ok(row, 'dsh.client must enroll Legion in the native browser graph')
  assert.equal(graph.release.version, '0.2.1-rc.2')
  assert.equal(served.length, 1)
  const browserEntry = [...ctx.loader.entries()].find(entry => entry.options.name === packageId)
  assert.ok(browserEntry?.fiber, 'The native Loader must own the browser plugin lifetime')
  assert.equal(browserEntry.options.config, undefined, 'Host configuration is not browser configuration')
  const client = await modules.import(packageId, '', {})
  assert.equal(typeof client.apply, 'function')
  assert.equal(client.Config, undefined)
  assert.equal(client.LegionClient, undefined)
  assert.equal(host.loader.resolve(hostEntryId).options.config.tokenFile, hostOnlyConfig.tokenFile)
  const browserWire = JSON.stringify(graph) + served.join('\n')
  for (const value of Object.values(hostOnlyConfig).flat()) {
    assert.ok(!browserWire.includes(value), 'Host-only configuration must not enter the browser graph or bundle')
  }
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1, 'A loaded client half must handle the public command event')
})

test('public command acknowledgments target their own session scope, never the selected session', async t => {
  const fixture = await browserFixture(t)
  // Deliberately make the tempting global fallback point at the other session.
  fixture.ctx.provide('uiSession', { current: 'session-b', selectedId: 'session-b' })
  const result = accepted()
  fixture.emit('session-a', 'legion', result)
  assert.equal(fixture.notices.length, 1)
  assert.equal(fixture.notices[0].sessionId, 'session-a')
  assert.equal(fixture.notices[0].level, 'info')
  assert.match(fixture.notices[0].text, new RegExp(taskRef))
  assert.match(fixture.notices[0].text, /不代表完成|非.*完成/)
  assert.equal(fixture.resolutions[0], fixture.scopes.get('session-a'))
  fixture.emit('session-b', 'legion', { kind: 'success', text: 'B session result' })
  assert.deepEqual(fixture.notices.map(notice => notice.sessionId), ['session-a', 'session-b'])
  assert.deepEqual(result, accepted(), 'Displaying a receipt must not mutate the returned command result')
})

test('non-legion command events have no effect even when their text mentions Legion', async t => {
  const fixture = await browserFixture(t)
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1, 'Positive control: the listener is active')
  for (const name of ['help', 'clear', 'legion-other', '/legion', 'Legion', 'legion runtime', '']) {
    fixture.emit('session-a', name, { kind: 'error', text: 'legion result belonging to another command' })
  }
  assert.equal(fixture.notices.length, 1)
  assert.equal(fixture.resolutions.length, 1)
})

test('missing or released sessions do not create scopes or fall back to another session', async t => {
  const fixture = await browserFixture(t)
  fixture.emit('session-b', 'legion', accepted())
  assert.equal(fixture.notices.length, 1)
  fixture.scopes.delete('session-a')
  for (const sessionId of ['session-a', 'unknown-session', '', undefined, null]) {
    fixture.emit(sessionId, 'legion', accepted())
  }
  assert.equal(fixture.notices.length, 1)
  assert.equal(fixture.resolutions.length, 1)
})

test('native Host removal and graph sync dispose the command observer', async t => {
  const fixture = await browserFixture(t)
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1)
  await fixture.uninstall()
  assert.ok(!fixture.registry.graph().entries.some(entry => entry.id === packageId))
  fixture.emit('session-a', 'legion', accepted())
  fixture.emit('session-b', 'legion', { kind: 'error', text: 'late result after uninstall' })
  assert.equal(fixture.notices.length, 1)
  assert.equal(fixture.resolutions.length, 1)
})

test('native fiber reload keeps one observer rather than accumulating duplicate notifications', async t => {
  const fixture = await browserFixture(t)
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1)
  const entry = [...fixture.ctx.loader.entries()].find(item => item.options.name === packageId)
  await entry.fiber.restart()
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 2)
})

test('an error reaches the native error channel with the useful already-sanitized explanation', async t => {
  const fixture = await browserFixture(t)
  const result = Object.freeze({ kind: 'error', text: '身份未确认；请在 Host 配置凭据，再查询原任务。Bearer [redacted]' })
  fixture.emit('session-a', 'legion', result)
  assert.equal(fixture.notices.length, 1)
  assert.equal(fixture.notices[0].level, 'error')
  assert.match(fixture.notices[0].text, /身份未确认/)
  assert.match(fixture.notices[0].text, /查询原任务/)
  assert.match(fixture.notices[0].text, /Bearer \[redacted\]/)
  assert.equal(result.kind, 'error')
})

test('empty success and error results produce honest localized receipts rather than blank notices', async t => {
  const fixture = await browserFixture(t, { language: 'en' })
  for (const text of [undefined, '', ' \n\t ', null]) {
    fixture.emit('session-a', 'legion', { kind: 'success', text })
    fixture.emit('session-a', 'legion', { kind: 'error', text })
  }
  assert.equal(fixture.notices.length, 8)
  for (const notice of fixture.notices) {
    assert.match(notice.text, /Legion/)
    assert.ok(notice.text.trim().length > 12)
    assert.ok([...notice.text].length <= 240)
    if (notice.level === 'info') assert.match(notice.text, /not.*complet|not confirm/i)
    else assert.match(notice.text, /error|fail/i)
  }
})

test('notice labels follow the current native locale on each event', async t => {
  const fixture = await browserFixture(t, { language: 'en' })
  fixture.emit('session-a', 'legion', { kind: 'success', text: 'Read-only runtime result' })
  fixture.language('zh')
  fixture.emit('session-a', 'legion', { kind: 'success', text: '只读运行时结果' })
  fixture.emit('session-a', 'legion', { kind: 'error', text: '请检查配置' })
  assert.equal(fixture.notices.length, 3)
  assert.match(fixture.notices[0].text, /Legion/)
  assert.match(fixture.notices[1].text, /军团.*回执/)
  assert.match(fixture.notices[2].text, /军团.*错误/)
})

test('long runtime replies are bounded useful previews, not a complete JSON notification', async t => {
  const fixture = await browserFixture(t)
  const result = Object.freeze({
    kind: 'success',
    text: formatLegionResult('runtime', {
      view: 'capabilities', source: 'service_observation',
      runtime: { facts: '事实'.repeat(25000), tail: 'RUNTIME_TAIL_NOT_FOR_NOTICE' },
    }),
  })
  fixture.emit('session-a', 'legion', result)
  assert.equal(fixture.notices.length, 1)
  const { text } = fixture.notices[0]
  assert.match(text, /capabilities/)
  assert.match(text, /摘要/)
  assert.ok([...text].length <= 240)
  assert.ok(!text.includes('RUNTIME_TAIL_NOT_FOR_NOTICE'))
  assert.ok(!text.includes('\n'))
  assert.ok(result.text.includes('RUNTIME_TAIL_NOT_FOR_NOTICE'), 'The original full result stays untouched')
})

test('short text is retained and Unicode truncation respects the total 240-code-point boundary', async t => {
  const fixture = await browserFixture(t, { language: 'en' })
  fixture.emit('session-a', 'legion', { kind: 'success', text: 'Read-only: no changes.' })
  assert.equal(fixture.notices.length, 1)
  assert.match(fixture.notices[0].text, /Read-only: no changes\./)
  assert.ok(!fixture.notices[0].text.includes('…'))
  for (const kind of ['success', 'error']) {
    for (const size of [1, 180, 220, 239, 240, 241, 50000]) {
      fixture.emit('session-a', 'legion', { kind, text: '🧭'.repeat(size) })
      const { text } = fixture.notices.at(-1)
      assert.ok([...text].length <= 240, `Total notice is bounded for ${kind} / ${size}`)
      assert.equal(text.isWellFormed(), true, 'A preview must not split a UTF-16 surrogate pair')
      if (size >= 240) assert.match(text, /summary/)
    }
  }
})

test('malformed result kinds are ignored; a disposed input cannot change a command outcome', async t => {
  const fixture = await browserFixture(t)
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1)
  for (const result of [undefined, null, 'success', {}, { kind: 'done' }, { kind: 'pending' }]) {
    fixture.emit('session-a', 'legion', result)
  }
  assert.equal(fixture.notices.length, 1)
  fixture.closeInput()
  const result = accepted()
  assert.doesNotThrow(() => fixture.emit('session-a', 'legion', result))
  assert.equal(fixture.notices.length, 1)
  assert.deepEqual(result, accepted())
})

test('missing injected services keep the browser extension inactive until the native service is available', async t => {
  const fixture = await browserFixture(t, { missingService: 'locale' })
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 0)
  fixture.ctx.provide('locale', fixture.services.locale)
  await fixture.ctx.loader.await()
  fixture.emit('session-a', 'legion', accepted())
  assert.equal(fixture.notices.length, 1)
})

test('the Node SDK export stays compatible and declared client peers remain on the pinned DSH release', async () => {
  const sdk = await import('dsh-plugin-legion/client')
  assert.equal(sdk.LegionClient, LegionClient)
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  const lock = JSON.parse(await readFile(new URL('../package-lock.json', import.meta.url), 'utf8'))
  assert.equal(pkg.version, '0.6.0')
  assert.equal(lock.version, pkg.version)
  assert.equal(lock.packages[''].version, pkg.version)
  assert.equal(pkg.engines.dsh, '0.2.1-rc.2')
  assert.equal(pkg.dsh.client?.platform, 'web')
  assert.ok(pkg.files.includes('client/'), 'The official package must ship the browser entry')
  const dependencies = [
    '@deepseek-ai/dsh-api-remotes',
    '@deepseek-ai/dsh-api-session-controller',
    '@deepseek-ai/dsh-client-locale',
    '@deepseek-ai/dsh-client-ui-commands',
    '@deepseek-ai/dsh-client-ui-conversation',
    '@deepseek-ai/dsh-client-ui-layout',
    '@deepseek-ai/dsh-client-ui-settings',
    '@deepseek-ai/dsh-client-ui-sidebar',
  ]
  for (const name of dependencies) {
    assert.ok(pkg.dsh.client.inject.includes(name))
    assert.equal(pkg.peerDependencies[name], '0.2.1-rc.2')
    assert.equal(pkg.devDependencies[name], '0.2.1-rc.2')
    assert.equal(lock.packages[`node_modules/${name}`].version, '0.2.1-rc.2')
  }
  assert.deepEqual(lock.packages[''].peerDependencies, pkg.peerDependencies)
  assert.deepEqual(lock.packages[''].devDependencies, pkg.devDependencies)
})
