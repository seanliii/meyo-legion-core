import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { LegionClient } from '../lib/client.js'
import { parseLegionCommand, formatLegionResult } from '../lib/commands.js'
import { fixture } from './fixture.js'

const execution = () => ({ sessionId: 'runtime-facts-session', signal: new AbortController().signal })

test('runtime views preserve server facts, source and observation time without installing or probing members', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-runtime-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const data = { warming: true, stale: true, provider_verified: false,
    errors: [{ phase: 'catalog', reason: 'unavailable fixture-token' }],
    harnesses: [{ key: 'example', installed: false, status: 'not_installed' }] }
  const routes = { members: '/api/legion/harnesses', capabilities: '/api/legion/capabilities', catalog: '/api/legion/catalog' }
  const server = await fixture(t, { responses: Object.fromEntries(Object.values(routes).map(path => [path, { body: data }])) })
  const client = new LegionClient({ baseUrl: server.baseUrl, stateDirectory: directory }, { LEGION_PERSONAL_TOKEN: 'fixture-token' })
  assert.equal(typeof client.runtime, 'function', 'runtime must use existing service endpoints')
  for (const [view, route] of Object.entries(routes)) {
    const result = await client.runtime({ view }, execution())
    assert.equal(result.view, view)
    assert.equal(result.source, `${server.baseUrl}${route}`)
    assert.ok(Number.isFinite(Date.parse(result.observed_at)))
    assert.equal(result.runtime.warming, true)
    assert.equal(result.runtime.stale, true)
    assert.equal(result.runtime.provider_verified, false)
    assert.equal(result.runtime.harnesses[0].installed, false)
    assert.match(result.runtime.errors[0].reason, /\[redacted\]/)
    assert.equal(result.verification, 'service_observation_only_no_execution')
  }
  assert.deepEqual(server.requests.map(x => [x.method, x.path]), Object.values(routes).map(x => ['GET', x]))
  await assert.rejects(client.runtime({ view: 'install' }, execution()), /view/i)
  assert.equal(server.requests.length, 3)
})

test('runtime view refuses an unusable response instead of fabricating an empty successful catalog', async t => {
  const server = await fixture(t, { responses: { '/api/legion/harnesses': { status: 503, body: { reason: 'catalog_warming' } } } })
  const client = new LegionClient({ baseUrl: server.baseUrl }, { LEGION_PERSONAL_TOKEN: 'fixture-token' })
  assert.equal(typeof client.runtime, 'function')
  await assert.rejects(client.runtime({ view: 'members' }, execution()), /503.*catalog_warming/)
})

test('human runtime command shows the same raw facts and does not label registration as readiness', () => {
  assert.deepEqual(parseLegionCommand('runtime'), { command: 'runtime', method: 'runtime', args: { view: 'members' } })
  assert.deepEqual(parseLegionCommand('runtime capabilities'), { command: 'runtime', method: 'runtime', args: { view: 'capabilities' } })
  assert.throws(() => parseLegionCommand('runtime install'))
  assert.throws(() => parseLegionCommand('runtime members unexpected'))
  const rendered = formatLegionResult('runtime', { view: 'members', source: 'http://127.0.0.1:8000/api/legion/harnesses',
    observed_at: '2026-10-28T00:00:00Z', runtime: { warming: true, installed: false, reason: 'missing_runtime' } })
  assert.match(rendered, /missing_runtime/)
  assert.match(rendered, /warming/)
  assert.match(rendered, /注册.*安装.*可用/)
})
