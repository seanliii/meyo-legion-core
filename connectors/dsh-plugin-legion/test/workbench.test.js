import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool } from '@deepseek-ai/dsh-tools'
import { createScope } from '@deepseek-ai/dsh-scope'
import { LegionClient } from '../lib/client.js'
import { GitHubSources } from '../lib/sources.js'

const TOKEN = 'workbench-fixture/private+credential='
const REPOSITORY = 'octocat/workbench-fixture'
const COMMIT = 'a'.repeat(40)
const TREE = 'b'.repeat(40)
const REF = 'c'.repeat(32)
const OUTPUT_BYTES = 64 * 1024

async function adapter() {
  let module
  try { module = await import('../lib/workbench.js') } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('/lib/workbench.js')) throw error
  }
  assert.equal(typeof module?.executeWorkbenchCommand, 'function', 'The Host workbench command adapter must exist')
  return module.executeWorkbenchCommand
}

function invocation(input, agent = { id: 'workbench-agent', session: { id: 'not-the-owning-id' } }) {
  return {
    commandId: 'host-owned-command', agent,
    rawInput: typeof input === 'string' ? input : JSON.stringify(input),
    attachments: [], signal: new AbortController().signal,
  }
}

function unpack(result) {
  assert.ok(['success', 'error'].includes(result.kind))
  assert.equal(typeof result.text, 'string')
  assert.ok(Buffer.byteLength(result.text) <= OUTPUT_BYTES, 'Every UI command receipt must be bounded')
  const body = JSON.parse(result.text)
  assert.equal(body.ok, result.kind === 'success')
  assert.ok(Object.hasOwn(body, 'action'))
  assert.deepEqual(Object.keys(body).sort(), (body.ok
    ? ['ok', 'action', 'value']
    : ['ok', 'action', 'error', ...(body.diagnostics ? ['diagnostics'] : [])]).sort())
  return body
}

async function fixture(t, fetchImpl) {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-workbench-'))
  const config = { stateDirectory: directory, baseUrl: 'https://legion.example.invalid', requestTimeoutMs: 1000 }
  const env = { LEGION_PERSONAL_TOKEN: TOKEN }
  const client = new LegionClient(config, env)
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init })
    assert.ok(fetchImpl, 'Tests must not use real networking')
    return fetchImpl(new URL(url), init)
  })
  const sources = new GitHubSources({}, { fetchImpl: async () => assert.fail('Unexpected source request') })
  const ctx = { tools: {
    schemas() { assert.fail('Unexpected native tool inventory') },
    execute() { assert.fail('A human command has no ToolExecution authority') },
    get() { assert.fail('The workbench must not resolve or execute native tools') },
  } }
  t.after(async () => { client.dispose(); await rm(directory, { recursive: true, force: true }) })
  const run = async (input, current = invocation(input), selectedSources = sources, selectedCtx = ctx) =>
    (await adapter())(selectedCtx, client, selectedSources, current)
  return { directory, config, env, client, sources, ctx, calls, run }
}

function repository() {
  return {
    private: false, visibility: 'public', full_name: REPOSITORY, default_branch: 'main',
    description: 'A public source, not an installed capability', language: 'JavaScript',
    license: { key: 'mit', name: 'MIT License', spdx_id: 'MIT' },
    stargazers_count: 7, forks_count: 0, topics: ['fixture'],
    fork: false, archived: false, disabled: false,
  }
}

function pinnedSources({ readmeMissing = false } = {}) {
  const files = {
    'README.md': `# Public source\nBearer ${TOKEN}\n${'你好🌍 source facts. '.repeat(220)}END_NOT_FOR_HISTORY`,
    'package.json': '{"name":"fixture","scripts":{"postinstall":"NEVER_RUN_THIS_INSTALLER"}}',
    LICENSE: 'Actual pinned license text; not a permission decision.',
  }
  const blobs = new Map()
  const entries = Object.entries(files).map(([path, content]) => {
    const bytes = Buffer.from(content)
    const sha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex')
    blobs.set(sha, bytes)
    return { path, mode: '100644', type: 'blob', sha, size: bytes.length }
  })
  const calls = []
  const sources = new GitHubSources({}, { fetchImpl: async (input, init) => {
    const url = new URL(input)
    calls.push({ url, init })
    assert.equal(url.origin, 'https://api.github.com')
    assert.equal(init.headers.Authorization, undefined)
    assert.equal(init.credentials, 'omit')
    const base = `/repos/${REPOSITORY}`
    if (url.pathname === base) return Response.json(repository())
    if (url.pathname.startsWith(`${base}/commits/`)) {
      return Response.json({ sha: COMMIT, commit: { tree: { sha: TREE } } })
    }
    if (url.pathname === `${base}/git/trees/${TREE}`) {
      return Response.json({ sha: TREE, tree: entries, truncated: false })
    }
    if (url.pathname.startsWith(`${base}/git/blobs/`)) {
      const sha = url.pathname.split('/').at(-1)
      if (readmeMissing && sha === entries[0].sha) return Response.json({ message: `Bearer ${TOKEN}` }, { status: 404 })
      const bytes = blobs.get(sha)
      assert.ok(bytes, 'Only pinned fixture blobs may be read')
      return Response.json({ sha, size: bytes.length, encoding: 'base64', content: bytes.toString('base64') })
    }
    if (url.pathname === `${base}/license`) {
      assert.equal(url.searchParams.get('ref'), COMMIT)
      return Response.json({
        path: 'LICENSE', sha: entries[2].sha, size: entries[2].size,
        license: { key: 'other', name: 'Other', spdx_id: 'NOASSERTION' },
      })
    }
    assert.fail(`Unexpected public-source path: ${url.pathname}`)
  } })
  return { sources, calls, files }
}

function memberRow(key, overrides = {}) {
  return {
    key, display_name: key, installed: true, available: true,
    model: { model: 'fixture-model', display_name: 'Fixture model', source: 'operator_binding',
      configured: true, provider_verified: false },
    readiness: { connected: true, verified: false, historical_verified: true, configured: true, samples: 2 },
    entry_points: { execution: { harness: key, selectable: true, integration: 'native', resume: 'session' } },
    profile: { intro: 'Service-provided introduction', source: 'service self-report' },
    ...overrides,
  }
}

test('members projection finds selected Codex beyond item 100 before clipping and keeps cache facts first', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = ['codex', 'not-observed']
  const harnesses = Array.from({ length: 110 }, (_, index) => memberRow(index === 109 ? 'codex' : `member-${index}`, {
    profile: { intro: '大段来源介绍'.repeat(3000), source: 'service self-report' },
  }))
  harnesses[109].model.configured = false
  harnesses[109].model.configuration_error = 'explicit_responses_not_configured'
  harnesses[109].readiness.configured = false
  const received = []
  t.mock.method(f.client, 'runtime', async (args, execution) => {
    received.push({ args, execution })
    return { view: 'members', source: 'fixture-public-catalog', observed_at: '2026-10-30T00:00:00.000Z',
      runtime: { harnesses, answer_pool: harnesses.map(row => row.key), updated_at: 123,
        warming: true, stale: true, public_catalog_only: false, from_cache: true },
      verification: 'service_observation_only_no_execution' }
  })
  const current = invocation({ action: 'runtime', args: { view: 'members', offset: 100, limit: 10 } })
  const body = unpack(await f.run(null, current))
  assert.equal(body.ok, true, JSON.stringify(body))
  const value = body.value
  assert.deepEqual(received[0].args, { view: 'members' }, 'UI paging must not invent a core HTTP API')
  assert.equal(received[0].execution.sessionId, current.agent.id)
  assert.equal(received[0].execution.signal, current.signal)
  assert.equal(value.runtime.warming, true)
  assert.equal(value.runtime.stale, true)
  assert.equal(value.runtime.public_catalog_only, false)
  assert.equal(value.runtime.from_cache, true)
  assert.equal(value.runtime.updated_at, 123)
  assert.equal(value.member_page.offset, 100)
  assert.equal(value.member_page.limit, 10)
  assert.equal(value.member_page.total, 110)
  assert.equal(value.member_page.shown_count, 10)
  assert.equal(value.member_page.next_offset, null)
  assert.equal(value.runtime.harnesses[0].key, 'member-100')
  assert.equal(value.runtime.harnesses[9].key, 'codex')
  assert.equal(value.selected.mode, 'configured')
  assert.equal(value.selected.total, 2)
  assert.equal(value.selected.shown_count, 2)
  const [codex, missing] = value.selected.items
  assert.equal(codex.key, 'codex')
  assert.equal(codex.observed, true)
  assert.equal(codex.model.configured, false)
  assert.equal(codex.model.configuration_error, 'explicit_responses_not_configured')
  assert.equal(codex.model.provider_verified, false)
  assert.equal(codex.readiness.historical_verified, true)
  assert.equal(codex.entry_points.execution.selectable, true)
  assert.equal(codex.profile.source, 'service self-report')
  assert.ok(Buffer.byteLength(codex.profile.intro) <= 600)
  assert.deepEqual(missing, { key: 'not-observed', observed: false })
  assert.equal(value.verification, 'service_observation_only_no_execution')
  assert.equal(f.calls.length, 0)
})

test('members projection paginates with default 0/12 and reports real totals without overlapping pages', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', runtime: { harnesses: Array.from({ length: 17 }, (_, index) => memberRow(`member-${index}`)) },
  }))
  const first = unpack(await f.run({ action: 'runtime', args: { view: 'members' } })).value
  assert.deepEqual(first.member_page, { offset: 0, limit: 12, total: 17, shown_count: 12, next_offset: 12, complete: false })
  assert.deepEqual(first.selected, { mode: 'automatic', items: [], total: 0, shown_count: 0, complete: true })
  const second = unpack(await f.run({ action: 'runtime', args: { view: 'members', offset: first.member_page.next_offset } })).value
  assert.deepEqual(second.member_page, { offset: 12, limit: 12, total: 17, shown_count: 5, next_offset: null, complete: false })
  assert.deepEqual([...first.runtime.harnesses, ...second.runtime.harnesses].map(row => row.key),
    Array.from({ length: 17 }, (_, index) => `member-${index}`))
  const outside = unpack(await f.run({ action: 'runtime', args: { view: 'members', offset: 999, limit: 1 } })).value
  assert.deepEqual(outside.member_page, { offset: 999, limit: 1, total: 17, shown_count: 0, next_offset: null, complete: false })
})

test('members projection accepts paging only for members and refuses invalid paging before any service call', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'runtime', async () => assert.fail('Invalid paging must not call the core'))
  const invalid = [
    { view: 'members', offset: -1 }, { view: 'members', offset: 0.5 }, { view: 'members', offset: '0' },
    { view: 'members', offset: Number.MAX_SAFE_INTEGER + 1 },
    { view: 'members', limit: 0 }, { view: 'members', limit: 31 }, { view: 'members', limit: 1.5 },
    { view: 'members', limit: '12' }, { view: 'members', limit: null },
    { view: 'capabilities', offset: 0 }, { view: 'catalog', limit: 12 },
    { view: 'members', keys: ['codex'] }, { view: 'members', sessionId: 'forged' },
  ]
  for (const args of invalid) {
    assert.equal(unpack(await f.run({ action: 'runtime', args })).ok, false, JSON.stringify(args))
  }
})

test('members projection snapshots configured candidates before the pending runtime read', async t => {
  const f = await fixture(t)
  const candidates = ['codex', 'not-observed']
  f.client.candidateHarnesses = candidates
  let enter, release
  const entered = new Promise(resolve => { enter = resolve })
  t.mock.method(f.client, 'runtime', () => {
    enter()
    return new Promise(resolve => { release = resolve })
  })
  const pending = f.run({ action: 'runtime', args: { view: 'members' } })
  await entered
  candidates.splice(0, candidates.length, 'new-member')
  f.client.candidateHarnesses = ['another-member']
  release({ view: 'members', runtime: { harnesses: [memberRow('new-member'), memberRow('codex')] } })
  const body = unpack(await pending)
  assert.equal(body.ok, true)
  assert.deepEqual(body.value.selected.items.map(row => [row.key, row.observed]), [['codex', true], ['not-observed', false]])
})

test('members projection preserves public and warming unknowns without manufacturing missing-member false states', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = ['codex', 'absent']
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', runtime: { public_catalog_only: true, warming: true, stale: true, updated_at: 0,
      harnesses: [{ key: 'codex', display_name: 'Codex', available: null, model: null,
        readiness: { declared: true, connected: false, verified: false }, entry_points: [],
        profile: { intro: 'Catalog declaration only' } }] },
  }))
  const body = unpack(await f.run({ action: 'runtime', args: { view: 'members' } }))
  assert.equal(body.ok, true)
  const { runtime, selected } = body.value
  assert.equal(runtime.public_catalog_only, true)
  assert.equal(runtime.warming, true)
  assert.equal(runtime.stale, true)
  const [codex, absent] = selected.items
  assert.equal(codex.observed, true)
  assert.equal(codex.available, null)
  assert.equal(Object.hasOwn(codex, 'installed'), false)
  assert.equal(codex.model, null)
  assert.deepEqual(codex.entry_points, [])
  assert.equal(codex.readiness.connected, false, 'preserve the reported field, not a fabricated runtime verdict')
  assert.deepEqual(absent, { key: 'absent', observed: false })
  assert.equal(Object.hasOwn(absent, 'installed'), false)
  assert.equal(Object.hasOwn(absent, 'available'), false)
  assert.equal(Object.hasOwn(absent, 'readiness'), false)
})

test('members projection redacts complete values before selecting and clipping both normal and selected rows', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = ['codex']
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', source: `https://host.invalid/?token=${encodeURIComponent(TOKEN)}`,
    runtime: { warming: false, harnesses: [memberRow('codex', {
      profile: { intro: `${'x'.repeat(590)}${TOKEN}${'y'.repeat(1000)}`, source: `Bearer ${TOKEN}` },
      model: { model: 'test-model', configured: false, provider_verified: false,
        configuration_error: `Bearer ${TOKEN}`, error: { message: `Bearer ${TOKEN}`, api_key: TOKEN }, api_key: TOKEN },
      readiness: { configured: false, provider_verified: false, historical_verified: true, secret: TOKEN },
      entry_points: { execution: { selectable: true, harness: 'codex', integration: 'native' },
        terminal: [{ authorization: TOKEN }], web: [{ url: `https://host.invalid/?token=${TOKEN}` }] },
      private_token: TOKEN, unsupported_metadata: 'omitted-not-a-status',
    })] },
  }))
  const result = await f.run({ action: 'runtime', args: { view: 'members' } })
  const body = unpack(result)
  assert.equal(body.ok, true)
  assert.equal(result.text.includes(TOKEN), false)
  assert.equal(result.text.includes(encodeURIComponent(TOKEN)), false)
  assert.equal(result.text.includes('omitted-not-a-status'), false)
  for (const row of [body.value.runtime.harnesses[0], body.value.selected.items[0]]) {
    assert.equal(row.key, 'codex')
    assert.equal(row.model.configured, false)
    assert.equal(row.model.provider_verified, false)
    assert.match(row.model.configuration_error, /\[redacted\]/)
    assert.equal(Object.hasOwn(row.model, 'api_key'), false)
    assert.equal(Object.hasOwn(row.entry_points, 'terminal'), false)
  }
  assert.ok(body.value.projection.omitted_fields.length > 0)
  assert.ok(body.value.projection.truncated_fields.some(path => path.includes('profile.intro')))
})

test('members projection shrinks a byte-heavy page at whole rows, preserving exact long keys and a progressing cursor', async t => {
  const f = await fixture(t)
  const harnesses = Array.from({ length: 8 }, (_, index) => memberRow(`member-${'x'.repeat(6000)}-${index}`, {
    entry_points: { execution: { selectable: true } },
  }))
  f.client.candidateHarnesses = [harnesses[7].key]
  t.mock.method(f.client, 'runtime', async () => ({ view: 'members', runtime: { harnesses, warming: false } }))
  const observedKeys = []
  let offset = 0
  do {
    const body = unpack(await f.run({ action: 'runtime', args: { view: 'members', offset, limit: 30 } }))
    assert.equal(body.ok, true, JSON.stringify(body))
    const value = body.value
    assert.equal(value.member_page.total, 8)
    assert.ok(value.member_page.shown_count > 0 && value.member_page.shown_count < 8)
    assert.equal(value.member_page.shown_count, value.runtime.harnesses.length)
    assert.deepEqual(value.selected.items.map(row => row.key), [harnesses[7].key], 'selection has its own budget')
    for (const row of value.runtime.harnesses) {
      assert.ok(harnesses.some(source => source.key === row.key), 'a key must never be a clipped identity')
      observedKeys.push(row.key)
    }
    const next = value.member_page.next_offset
    if (next !== null) assert.equal(next, offset + value.member_page.shown_count, 'never skip undisplayed rows')
    offset = next
  } while (offset !== null)
  assert.deepEqual(observedKeys, harnesses.map(row => row.key))
})

test('members projection reports the unshown tail of an oversized selection independently of the catalog page', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = Array.from({ length: 100 }, (_, index) => `selected-${'a'.repeat(600)}-${index}`)
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', runtime: { harnesses: [memberRow('other')], warming: false },
  }))
  const body = unpack(await f.run({ action: 'runtime', args: { view: 'members', limit: 1 } }))
  assert.equal(body.ok, true)
  assert.equal(body.value.runtime.harnesses.length, 1)
  assert.equal(body.value.member_page.total, 1)
  const selected = body.value.selected
  assert.equal(selected.total, 100)
  assert.equal(selected.shown_count, selected.items.length)
  assert.ok(selected.shown_count > 0 && selected.shown_count < 100)
  assert.equal(selected.complete, false)
  assert.deepEqual(selected.items.map(row => row.key), f.client.candidateHarnesses.slice(0, selected.shown_count))
  assert.ok(selected.items.every(row => row.observed === false))
  assert.ok(body.value.projection.truncated_fields.includes('$.selected.items'))
})

test('members projection keeps anomalous row positions and records omitted unknown fields rather than reindexing', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = ['codex']
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', unexpected_root: 'not-a-fact',
    runtime: { harnesses: [null, memberRow('codex', { installed: 'not-a-boolean', extra_status: { guess: true } }),
      { display_name: 'Missing key' }], warming: false, extra_metadata: { guess: true } },
  }))
  const body = unpack(await f.run({ action: 'runtime', args: { view: 'members', limit: 30 } }))
  assert.equal(body.ok, true)
  const value = body.value
  assert.equal(value.member_page.total, 3)
  assert.equal(value.member_page.shown_count, 3)
  assert.equal(value.member_page.next_offset, null)
  assert.equal(value.member_page.complete, false)
  assert.equal(value.runtime.harnesses[0], null)
  assert.equal(value.runtime.harnesses[1].key, 'codex')
  assert.equal(value.runtime.harnesses[1].installed, 'not-a-boolean')
  assert.equal(Object.hasOwn(value.runtime.harnesses[2], 'key'), false)
  assert.equal(value.selected.items[0].observed, true)
  assert.ok(value.projection.omitted_fields.includes('$.unexpected_root'))
  assert.ok(value.projection.omitted_fields.includes('$.runtime.harnesses[1].extra_status'))
})

test('members projection keeps the legacy generic path when no runtime harness array was supplied', async t => {
  const f = await fixture(t)
  for (const runtime of [{ legacy: { installed: null } }, { harnesses: null, reason: 'unavailable' }]) {
    t.mock.method(f.client, 'runtime', async () => ({ view: 'members', runtime }))
    const value = unpack(await f.run({ action: 'runtime', args: { view: 'members' } })).value
    assert.deepEqual(value.runtime, runtime)
    assert.equal(Object.hasOwn(value, 'member_page'), false)
    assert.equal(Object.hasOwn(value, 'selected'), false)
  }
})

test('members projection never claims completeness when the upstream array was already truncated', async t => {
  const f = await fixture(t)
  f.client.candidateHarnesses = ['absent']
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', runtime: { harnesses: [memberRow('other')], warming: true },
    projection: { omitted_fields: [], truncated_fields: ['$.runtime.harnesses'] },
  }))
  const value = unpack(await f.run({ action: 'runtime', args: { view: 'members' } })).value
  assert.equal(value.member_page.complete, false)
  assert.equal(value.selected.complete, false)
  assert.deepEqual(value.selected.items, [{ key: 'absent', observed: false }])
  assert.ok(value.projection.truncated_fields.includes('$.runtime.harnesses'))
})

test('members projection does not publish a redacted or clipped long key as a real harness identity', async t => {
  const f = await fixture(t)
  const key = `member-${'z'.repeat(20000)}`
  f.client.candidateHarnesses = [key]
  t.mock.method(f.client, 'runtime', async () => ({ view: 'members', runtime: { harnesses: [memberRow(key)] } }))
  const body = unpack(await f.run({ action: 'runtime', args: { view: 'members' } }))
  assert.equal(body.ok, true)
  assert.equal(body.value.selected.total, 1)
  assert.equal(body.value.selected.shown_count, 0)
  assert.equal(body.value.selected.complete, false)
  assert.equal(Object.hasOwn(body.value.runtime.harnesses[0], 'key'), false)
  assert.ok(body.value.projection.truncated_fields.length > 0)
})

test('workbench routes only the requested action with the exact Host session and cancellation signal', async t => {
  const f = await fixture(t)
  const cases = [
    ['runtime', { view: 'catalog' }, 'client', 'runtime'],
    ['tasks', undefined, 'client', 'list'],
    ['status', { task_ref: REF }, 'client', 'status'],
    ['submit', { key: 'stable', query: 'Let the model decide how to connect', context: 'public context' }, 'client', 'submit'],
    ['github', { query: 'public source', limit: 2 }, 'sources', 'search'],
    ['inspect', { repository: REPOSITORY, ref: 'feature/docs' }, 'sources', 'inspect'],
  ]
  for (const [action, args, target, method] of cases) {
    const received = []
    const mock = t.mock.method(f[target], method, async (...values) => {
      received.push(values)
      return target === 'sources' && method === 'inspect'
        ? { repository: REPOSITORY, commit: COMMIT, readme: null, license: null, manifests: [], errors: [] }
        : { task_ref: REF, source: 'fixture-observation' }
    })
    const current = invocation({ action, ...(args ? { args } : {}) })
    current.agent.followup = () => assert.fail('The adapter is not another agent')
    const body = unpack(await f.run(null, current))
    assert.equal(body.ok, true)
    assert.equal(body.action, action)
    assert.deepEqual(received, [method === 'list'
      ? [{ sessionId: current.agent.id, signal: current.signal }]
      : [args, { sessionId: current.agent.id, signal: current.signal }]])
    mock.mock.restore()
  }
  assert.equal(f.calls.length, 0)
})

test('all runtime views retain service observations without equating registration with installation or readiness', async t => {
  const routes = { members: '/api/legion/harnesses', capabilities: '/api/legion/capabilities', catalog: '/api/legion/catalog' }
  const f = await fixture(t, async () => Response.json({
    stale: true, warming: true, members: [{ key: 'fixture', registered: true, installed: false, available: false }],
    errors: [{ reason: 'not_installed' }], token: TOKEN,
  }))
  for (const [view, route] of Object.entries(routes)) {
    const body = unpack(await f.run({ action: 'runtime', args: { view } }))
    assert.equal(body.value.view, view)
    assert.equal(body.value.source, `https://legion.example.invalid${route}`)
    assert.equal(body.value.verification, 'service_observation_only_no_execution')
    assert.equal(body.value.runtime.members[0].installed, false)
    assert.equal(body.value.runtime.members[0].available, false)
    assert.equal(body.value.runtime.warming, true)
    assert.equal(body.value.runtime.errors[0].reason, 'not_installed')
    assert.equal(Object.hasOwn(body.value.runtime, 'token'), false)
    assert.ok(body.value.projection.omitted_fields.some(path => path.endsWith('.token')))
  }
  assert.deepEqual(f.calls.map(call => new URL(call.url).pathname), Object.values(routes))
})

test('unknown root and per-action fields never become authority or reach any operation', async t => {
  const f = await fixture(t)
  const inputs = [
    { action: 'runtime', args: { view: 'members' } }, { action: 'tools', args: {} },
    { action: 'github', args: { query: 'public' } }, { action: 'inspect', args: { repository: REPOSITORY } },
    { action: 'tasks' }, { action: 'status', args: { task_ref: REF } },
    { action: 'submit', args: { key: 'key', query: 'task' } },
  ]
  for (const method of ['runtime', 'list', 'status', 'submit']) {
    t.mock.method(f.client, method, () => assert.fail('Unknown fields must fail before client access'))
  }
  for (const input of inputs) {
    for (const field of [
      'agentId', 'sessionId', 'agent', 'config', 'token', 'tokenFile', 'baseUrl', 'env', 'signal', 'attachments',
      'callId', 'parent', 'rootCallId', 'candidateHarnesses', 'workspace_root', 'previous_task_ref',
      'tool_names', '__proto__', 'constructor', 'prototype', 'UNKNOWN_INPUT_SECRET',
    ]) {
      for (const raw of [
        { ...input, [field]: 'UNKNOWN_INPUT_SECRET' },
        { ...input, args: { ...input.args, [field]: 'UNKNOWN_INPUT_SECRET' } },
      ]) {
        const result = await f.run(raw)
        assert.equal(unpack(result).ok, false, `${input.action}: ${field}`)
        assert.doesNotMatch(result.text, /UNKNOWN_INPUT_SECRET/, 'Rejected input must not be quoted in history')
      }
    }
  }
  assert.equal(f.calls.length, 0)
})

test('malformed JSON and unknown actions return safe JSON errors, not model prompts or arbitrary dispatch', async t => {
  const f = await fixture(t)
  for (const raw of [
    '{"action":"tools","token":"UNTRUSTED_PARSE_SECRET",',
    'null', '[]', 'true', '42', '"tasks"', '{}',
    ...['install', 'call', 'configure', 'market', '__proto__', 'constructor', 'UNTRUSTED_PARSE_SECRET']
      .map(action => JSON.stringify({ action })),
    JSON.stringify({ action: { token: 'UNTRUSTED_PARSE_SECRET' } }),
    ' '.repeat(2 * 1024 * 1024 + 1),
  ]) {
    const result = await f.run(raw)
    const body = unpack(result)
    assert.equal(body.ok, false)
    assert.equal(body.action, null, 'Only an allowlisted action may be echoed')
    assert.doesNotMatch(result.text, /UNTRUSTED_PARSE_SECRET/)
  }
  assert.equal(f.calls.length, 0)
})

test('argument validation is strict, bounded and does not coerce values or permit authority objects', async t => {
  const f = await fixture(t)
  const invalid = {
    runtime: [undefined, {}, { view: 'install' }, { view: null }],
    tools: [null, [], { query: {} }, { query: 'x'.repeat(501) }, { offset: -1 }, { offset: 1.2 },
      { offset: Number.MAX_SAFE_INTEGER + 1 }, { limit: 0 }, { limit: 31 }, { limit: '10' }],
    github: [{}, { query: '' }, { query: 1 }, { query: 'x'.repeat(2049) }, { query: 'a', limit: 101 }],
    inspect: [{}, { repository: [] }, { repository: REPOSITORY, ref: null }],
    tasks: [null, [], { query: 'not an argument' }],
    status: [{}, { task_ref: 'latest' }, { task_ref: `${REF}\n` }, { task_ref: '../private' },
      { task_ref: REF, after_event_id: -1 }, { task_ref: REF, after_message_id: '1' },
      { task_ref: REF, artifact_offset: 1.2 }],
    submit: [{}, { key: '', query: 'task' }, { key: 'k'.repeat(161), query: 'task' },
      { key: 'key', query: '  ' }, { key: 'key', query: 'q'.repeat(100001) },
      { key: 'key', query: 'task', context: { sessionId: 'foreign' } },
      { key: 'key', query: 'task', context: 'c'.repeat(200001) }],
  }
  for (const method of ['runtime', 'list', 'status', 'submit']) {
    t.mock.method(f.client, method, () => assert.fail('Invalid arguments must not reach the client'))
  }
  for (const [action, values] of Object.entries(invalid)) {
    for (const args of values) {
      const result = await f.run({ action, ...(args === undefined ? {} : { args }) })
      assert.equal(unpack(result).ok, false, `${action}: ${JSON.stringify(args)?.slice(0, 80)}`)
    }
  }
  assert.equal(f.calls.length, 0)
})

test('missing Host identity, invalid signal, attachments and pre-cancellation cannot trigger work', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'list', () => assert.fail('Host authority must be established first'))
  const current = invocation({ action: 'tasks' })
  const cancelled = new AbortController()
  cancelled.abort(new Error('PRIVATE_ABORT_REASON'))
  for (const input of [
    { ...current, agent: undefined }, { ...current, agent: { id: '' } },
    { ...current, signal: undefined }, { ...current, signal: {} },
    { ...current, attachments: [{ type: 'file', content: 'PRIVATE_ATTACHMENT' }] },
    { ...current, attachments: {} }, { ...current, signal: cancelled.signal },
  ]) {
    const result = await f.run(null, input)
    assert.equal(unpack(result).ok, false)
    assert.doesNotMatch(result.text, /PRIVATE_ABORT_REASON|PRIVATE_ATTACHMENT/)
  }
  assert.equal(f.calls.length, 0)
})

test('tools reads actual agent-scoped native metadata with paging and never executes a native tool', async t => {
  const f = await fixture(t)
  const ctx = new Context()
  const prompt = ctx.plugin(SystemPrompt, { includeHarnessIdentity: false })
  await prompt
  const tools = ctx.plugin(ToolRuntime, { mode: 'native' })
  await tools
  const agent = { id: 'scoped-workbench' }
  let scope
  const owner = ctx.inject(['tools'], injected => { scope = createScope(injected, agent) })
  await owner
  agent.ctx = scope.ctx
  t.after(async () => { await scope.dispose(); await owner.dispose(); await tools.dispose(); await prompt.dispose() })
  let executed = 0
  for (const name of ['repo_read', 'repo_write', 'repo_hidden', 'other']) {
    ctx.tools.register(defineTool({
      name, description: `Public ${name} metadata`,
      parameters: { input: { type: 'string', description: 'MODEL_ONLY_PARAMETER_EXAMPLE' } },
      output: { schema: { type: 'object', additionalProperties: true }, render: () => [] },
      execute() { executed++; return {} },
    }))
  }
  agent.ctx.tools.restrict({ deny: ['repo_hidden'] })
  const first = unpack(await f.run(null,
    invocation({ action: 'tools', args: { query: 'REPO public', limit: 1 } }, agent), f.sources, ctx))
  assert.equal(first.value.scope, 'current_dsh_agent')
  assert.equal(first.value.metadata_only, true)
  assert.equal(first.value.grants_remote_access, false)
  assert.equal(first.value.total, 2)
  assert.equal(first.value.next_offset, 1)
  assert.ok(Array.isArray(first.value.tools), 'The native controller consumes value.tools')
  assert.equal(Object.hasOwn(first.value, 'entries'), false)
  assert.deepEqual(first.value.tools.map(entry => entry.name), ['repo_read'])
  assert.deepEqual(Object.keys(first.value.tools[0]).sort(), ['description', 'name'])
  assert.doesNotMatch(JSON.stringify(first), /MODEL_ONLY_PARAMETER_EXAMPLE|repo_hidden/)
  const second = unpack(await f.run(null,
    invocation({ action: 'tools', args: { query: 'repo', offset: 1, limit: 1 } }, agent), f.sources, ctx))
  assert.deepEqual(second.value.tools.map(entry => entry.name), ['repo_write'])
  assert.equal(second.value.next_offset, null)
  agent.ctx.tools.restrict({ deny: ['repo_write'] })
  const fresh = unpack(await f.run(null,
    invocation({ action: 'tools', args: { query: 'repo' } }, agent), f.sources, ctx))
  assert.equal(fresh.value.total, 1, 'Do not cache another visibility snapshot')
  assert.equal(executed, 0)
  assert.equal(f.calls.length, 0)
})

test('tool descriptions and omitted schema metadata are bounded and explicitly projected', async t => {
  const f = await fixture(t)
  const current = invocation({ action: 'tools', args: { limit: 30 } })
  f.ctx.tools.schemas = agent => {
    assert.equal(agent, current.agent)
    return Array.from({ length: 35 }, (_, i) => ({
      name: `visible_${i}`, description: `Bearer ${TOKEN} ${'中文工具说明。'.repeat(3000)}`,
      parameters: { default: 'DO_NOT_COPY_SCHEMAS' },
    }))
  }
  const result = await f.run(null, current)
  const body = unpack(result)
  assert.equal(body.ok, true)
  assert.equal(body.value.tools.length, 30)
  assert.equal(body.value.next_offset, 30)
  assert.equal(body.value.total, 35)
  assert.equal(body.value.truncated, true)
  assert.ok(body.value.tools.every(row => Buffer.byteLength(row.description) <= 1024))
  assert.ok(body.value.tools.every(row => row.description.isWellFormed()))
  assert.ok(body.value.projection.omitted_fields.includes('$.tools[0].parameters'))
  assert.ok(body.value.projection.truncated_fields.includes('$.tools[0].description'))
  assert.doesNotMatch(result.text, /DO_NOT_COPY_SCHEMAS|workbench-fixture\/private/)
})

test('an out-of-range metadata page remains an empty page with the real total', async t => {
  const f = await fixture(t)
  f.ctx.tools.schemas = () => [{ name: 'one', description: 'visible', parameters: {} }]
  const body = unpack(await f.run({ action: 'tools', args: { offset: 100 } }))
  assert.deepEqual(body.value.tools, [])
  assert.equal(body.value.total, 1)
  assert.equal(body.value.next_offset, null)
  f.ctx.tools.schemas = () => ({ tools: [] })
  assert.equal(unpack(await f.run({ action: 'tools' })).ok, false, 'Invalid metadata is not an empty inventory')
})

test('a byte-limited metadata page keeps its total and resumes after the last actually returned tool', async t => {
  const f = await fixture(t)
  const schemas = Array.from({ length: 35 }, (_, index) => ({
    name: `tool_${index}_${'n'.repeat(990)}`, description: 'd'.repeat(1024), parameters: {},
  }))
  f.ctx.tools.schemas = () => schemas
  const first = unpack(await f.run({ action: 'tools', args: { limit: 30 } }))
  assert.equal(first.ok, true)
  assert.equal(first.value.total, 35)
  assert.ok(first.value.tools.length > 0 && first.value.tools.length < 30)
  assert.equal(first.value.next_offset, first.value.tools.length, 'A byte cap must not skip undisplayed tools')
  assert.equal(first.value.truncated, true)
  assert.ok(first.value.projection.truncated_fields.includes('$.tools'))
  const second = unpack(await f.run({ action: 'tools', args: { offset: first.value.next_offset, limit: 30 } }))
  assert.equal(second.value.tools[0].name, schemas[first.value.tools.length].name)
  assert.equal(second.value.total, 35)
  assert.equal(second.value.next_offset, null)
})

test('github reuses the supplied public connector and retains source facts without claiming installation', async t => {
  const f = await fixture(t)
  const requests = []
  const sources = new GitHubSources({}, { fetchImpl: async (url, init) => {
    requests.push({ url: new URL(url), init })
    return Response.json({ total_count: 3, incomplete_results: false, items: [repository()] })
  } })
  const result = await f.run(null, invocation({ action: 'github', args: { query: '中文 & CLI', limit: 1 } }), sources)
  const body = unpack(result)
  assert.equal(body.value.repositories[0].repository, REPOSITORY)
  assert.equal(body.value.repositories[0].license.spdx_id, 'MIT')
  assert.equal(body.value.trust, 'untrusted_source')
  assert.equal(body.value.total_count, 3)
  assert.equal(body.value.truncated, true)
  assert.match(body.value.source.response_sha256, /^[a-f0-9]{64}$/)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].url.searchParams.get('q'), '中文 & CLI')
  assert.equal(requests[0].init.headers.Authorization, undefined)
  assert.equal(Object.hasOwn(body.value.repositories[0], 'installed'), false)
  assert.equal(f.calls.length, 0)
})

test('inspect returns pinned source facts, a short README and explicit native follow-up instead of copying source bodies', async t => {
  const f = await fixture(t)
  const source = pinnedSources()
  const result = await f.run(null, invocation({ action: 'inspect', args: { repository: REPOSITORY, ref: 'main' } }), source.sources)
  const { value } = unpack(result)
  assert.equal(value.repository, REPOSITORY)
  assert.equal(value.commit, COMMIT)
  assert.equal(value.url, `https://github.com/${REPOSITORY}/tree/${COMMIT}`)
  assert.equal(value.status, 'inspected')
  assert.equal(value.license.spdx_id, 'NOASSERTION', 'Pinned license facts must not be replaced by default-branch metadata')
  assert.equal(value.license.assessment, 'not_performed')
  assert.equal(value.license.file.path, 'LICENSE')
  assert.equal(value.metadata.license.spdx_id, 'MIT')
  assert.equal(value.readme.path, 'README.md')
  assert.ok(value.readme.content.startsWith('# Public source'))
  assert.ok(Buffer.byteLength(value.readme.content) <= 1200)
  assert.equal(value.readme.content.isWellFormed(), true)
  assert.equal(value.readme.content_preview, true)
  assert.equal(value.readme.truncated, true)
  assert.equal(value.readme.total_bytes, Buffer.byteLength(source.files['README.md']))
  assert.equal(value.source_truncated, false, 'UI summarization is distinct from a truncated GitHub source')
  assert.equal(value.truncated, true)
  assert.equal(value.tree.entries, undefined)
  assert.equal(value.tree.received_entries, 3)
  assert.deepEqual(value.manifests.map(file => file.path), ['package.json'])
  assert.equal(value.manifests[0].content, undefined)
  assert.equal(value.continuation.tool, 'legion_source')
  assert.deepEqual(value.continuation.arguments, { action: 'read', repository: REPOSITORY, commit: COMMIT })
  assert.equal(value.readme.next.tool, 'legion_source')
  assert.deepEqual(value.readme.next.arguments, {
    action: 'read', repository: REPOSITORY, commit: COMMIT, path: 'README.md', offset: 0, limit: 12000,
  }, 'A shortened UI preview must not skip straight to the source page end')
  assert.ok(value.projection.omitted_fields.includes('$.tree.entries'))
  assert.ok(value.projection.truncated_fields.includes('$.readme.content'))
  assert.doesNotMatch(result.text, /END_NOT_FOR_HISTORY|NEVER_RUN_THIS_INSTALLER|workbench-fixture\/private/)
  assert.equal(source.calls.filter(call => call.url.pathname.includes('/commits/')).length, 1)
  assert.equal(f.calls.length, 0)
})

test('partial inspect keeps actual preview errors and does not invent missing text or readiness', async t => {
  const f = await fixture(t)
  const source = pinnedSources({ readmeMissing: true })
  const body = unpack(await f.run(null, invocation({ action: 'inspect', args: { repository: REPOSITORY } }), source.sources))
  assert.equal(body.ok, true, 'A returned partial source observation is not a successful installation')
  assert.equal(body.value.status, 'partial')
  assert.equal(body.value.readme, null)
  assert.equal(body.value.readme_status, 'unavailable')
  assert.equal(body.value.errors[0].stage, 'readme')
  assert.equal(body.value.errors[0].status, 404)
  assert.equal(body.value.source_truncated, true)
  assert.equal(Object.hasOwn(body.value, 'installed'), false)
})

test('source failures expose only sourceErrorDiagnostics including rate limits, never upstream bodies', async t => {
  const f = await fixture(t)
  const sources = new GitHubSources({}, { fetchImpl: async () => new Response(
    '<html>UNREGISTERED_SECRET_BODY</html>', {
      status: 429, headers: {
        'content-type': 'text/html', 'x-ratelimit-remaining': '0',
        'x-ratelimit-reset': '1793070000', 'retry-after': '120',
      },
    },
  ) })
  for (const input of [{ action: 'github', args: { query: 'source' } }, { action: 'inspect', args: { repository: REPOSITORY } }]) {
    const result = await f.run(null, invocation(input), sources)
    const body = unpack(result)
    assert.equal(body.ok, false)
    assert.equal(body.action, input.action)
    assert.equal(body.diagnostics.code, 'HTTP_ERROR')
    assert.equal(body.diagnostics.status, 429)
    assert.equal(body.diagnostics.rate_limit.remaining, 0)
    assert.equal(body.diagnostics.rate_limit.reset_epoch_seconds, 1793070000)
    assert.equal(body.diagnostics.retry_after_seconds, 120)
    assert.equal(body.diagnostics.trust, 'untrusted_source')
    assert.match(body.error, /429/)
    assert.doesNotMatch(result.text, /UNREGISTERED_SECRET_BODY|<html>/)
  }
})

test('source cancellation reaches the shared connector and never reflects arbitrary abort reasons', async t => {
  const f = await fixture(t)
  const controller = new AbortController()
  let sourceSignal
  const sources = new GitHubSources({}, { fetchImpl: async (_url, init) => {
    sourceSignal = init.signal
    controller.abort(new Error('PRIVATE_ABORT_REASON'))
    return new Promise(() => {})
  } })
  const current = { ...invocation({ action: 'github', args: { query: 'source' } }), signal: controller.signal }
  const result = await f.run(null, current, sources)
  const body = unpack(result)
  assert.equal(sourceSignal.aborted, true)
  assert.equal(body.ok, false)
  assert.equal(body.diagnostics.code, 'ABORTED')
  assert.doesNotMatch(result.text, /PRIVATE_ABORT_REASON/)
})

test('ordinary errors preserve the safe reason, redact credentials and never serialize attached diagnostics', async t => {
  const f = await fixture(t)
  const error = new Error(`ECONNRESET Bearer ${TOKEN} https://public.example/?token=UNKNOWN_URL_SECRET`, {
    cause: new Error('PRIVATE_CAUSE'),
  })
  error.diagnostics = { token: 'PRIVATE_ATTACHED_DATA' }
  error.toJSON = () => assert.fail('Arbitrary error serialization is not a source diagnostic')
  t.mock.method(f.client, 'status', async () => { throw error })
  const result = await f.run({ action: 'status', args: { task_ref: REF } })
  const body = unpack(result)
  assert.equal(body.ok, false)
  assert.match(body.error, /ECONNRESET/)
  assert.match(body.error, /\[redacted\]/)
  assert.doesNotMatch(result.text, /workbench-fixture\/private|UNKNOWN_URL_SECRET|PRIVATE_CAUSE|PRIVATE_ATTACHED_DATA/)
})

test('large runtime output is explicitly bounded and previous projection facts survive', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'catalog', source: 'fixture-observation', verification: 'service_observation_only_no_execution',
    projection: { omitted_fields: ['$.original.token'], truncated_fields: ['$.original.long_text'] },
    runtime: { members: Array.from({ length: 500 }, (_, i) => ({
      key: `member_${i}`, installed: false, description: '大目录'.repeat(10000),
    })) },
  }))
  const result = await f.run({ action: 'runtime', args: { view: 'catalog' } })
  const body = unpack(result)
  const observation = body.value ?? body.diagnostics
  assert.equal(observation.truncated, true)
  assert.ok(observation.projection.truncated_fields.length > 0)
  assert.ok(observation.projection.truncated_fields.length <= 64)
  assert.ok(observation.projection.omitted_fields.includes('$.original.token'))
  assert.ok(observation.projection.truncated_fields.includes('$.original.long_text'))
})

test('redaction happens before UI clipping, including credentials straddling a preview boundary', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'runtime', async () => ({
    view: 'members', runtime: {
      note: `${'x'.repeat(4090)}${TOKEN} public tail`,
      nested: { password: 'UNRELATED_PASSWORD', api_key: 'UNRELATED_KEY', reasoning: 'PRIVATE_REASONING' },
      preview: 'data:text/plain;base64,SU5MSU5FX1NFQ1JFVA==',
    },
  }))
  const result = await f.run({ action: 'runtime', args: { view: 'members' } })
  const body = unpack(result)
  assert.equal(body.ok, true)
  assert.deepEqual(body.value.runtime.nested, {})
  assert.doesNotMatch(result.text, /workbench-fixture|UNRELATED_PASSWORD|UNRELATED_KEY|PRIVATE_REASONING|SU5MSU5F/)
  assert.ok(body.value.projection.omitted_fields.length >= 3)
})

test('real submit deduplicates concurrent keys, persists across reload and scopes tasks/status to the owning agent', async t => {
  let dispatch
  const f = await fixture(t, async (url, init) => {
    if (url.pathname === '/api/legion/dispatch') {
      dispatch = JSON.parse(init.body)
      return new Response(`event: meta\ndata: ${JSON.stringify({ team_id: dispatch.team_id, sid: dispatch.session_id })}\n\n`,
        { headers: { 'content-type': 'text/event-stream' } })
    }
    if (url.pathname === '/api/legion/team') {
      return Response.json({ team_id: dispatch.team_id, conversation_id: dispatch.session_id,
        tasks: { author: { status: 'failed', result: { error: 'missing_dependency', final: '' } } } })
    }
    if (url.pathname === '/api/legion/events') {
      return Response.json({ team_id: dispatch.team_id, events: [], last_event_id: 0, more_events: false })
    }
    assert.fail(`Unexpected operation ${url.pathname}`)
  })
  const input = { action: 'submit', args: { key: 'same-key', query: 'Connect  this\npublic source', context: 'Human-provided context' } }
  const results = (await Promise.all([f.run(input), f.run(input)])).map(unpack)
  assert.equal(f.calls.length, 1, 'The existing TaskStore, not an adapter cache, owns deduplication')
  assert.equal(results[0].value.task_ref, results[1].value.task_ref)
  assert.ok(results.some(result => result.value.reused === true))
  assert.match(dispatch.question, /^Connect  this\npublic source\n\n/)
  assert.ok(dispatch.question.endsWith('Human-provided context'))
  assert.equal(dispatch.mode, 'auto')
  assert.equal(dispatch.allow_direct_model, false)
  const taskRef = results[0].value.task_ref
  const restarted = new LegionClient(f.config, f.env)
  t.after(() => restarted.dispose())
  const again = unpack(await (await adapter())(f.ctx, restarted, f.sources, invocation(input)))
  assert.equal(again.value.task_ref, taskRef)
  assert.equal(again.value.reused, true)
  assert.equal(f.calls.length, 1)
  const changed = unpack(await f.run({ action: 'submit', args: { ...input.args, query: 'Different input' } }))
  assert.equal(changed.ok, false)
  assert.match(changed.error, /different input/i)
  const tasks = unpack(await f.run({ action: 'tasks', args: {} }))
  assert.equal(tasks.value.source, 'local_recovery_index_not_live_status')
  assert.deepEqual(tasks.value.tasks.map(task => task.task_ref), [taskRef])
  const foreign = invocation({ action: 'tasks' }, { id: 'other-agent' })
  assert.deepEqual(unpack(await f.run(null, foreign)).value.tasks, [])
  const status = unpack(await f.run({ action: 'status', args: { task_ref: taskRef } }))
  assert.equal(status.ok, true, 'Reading a failed task is not a failed status query')
  assert.equal(status.value.runtime.tasks[0].status, 'failed')
  assert.equal(status.value.runtime.tasks[0].result.error, 'missing_dependency')
  assert.equal(status.value.experience_verification, 'not_performed_by_plugin')
  const before = f.calls.length
  assert.equal(unpack(await f.run(null,
    invocation({ action: 'status', args: { task_ref: taskRef } }, { id: 'other-agent' }))).ok, false)
  assert.equal(f.calls.length, before, 'Foreign-session references must fail before HTTP')
})

test('a reused rejection stays an error with its recovery reference and never redispatches', async t => {
  const f = await fixture(t, async () => Response.json({ error: 'permission_denied' }, { status: 403 }))
  const input = { action: 'submit', args: { key: 'rejected-key', query: 'Public task' } }
  const first = unpack(await f.run(input))
  assert.equal(first.ok, false)
  assert.match(first.error, /403.*permission_denied/)
  const repeated = unpack(await f.run(input))
  assert.equal(repeated.ok, false)
  assert.equal(repeated.diagnostics.submission, 'rejected')
  assert.equal(repeated.diagnostics.reused, true)
  assert.match(repeated.diagnostics.task_ref, /^[a-f0-9]{32}$/)
  assert.match(repeated.error, /403.*permission_denied/)
  assert.equal(f.calls.length, 1)
})

test('ambiguous submit acknowledgement is preserved as unconfirmed with a reference, not retried or declared done', async t => {
  const f = await fixture(t, async () => { throw new Error(`ECONNRESET Bearer ${TOKEN}`) })
  const input = { action: 'submit', args: { key: 'uncertain-key', query: 'Public task' } }
  const first = unpack(await f.run(input))
  const repeated = unpack(await f.run(input))
  assert.equal(first.ok, true)
  assert.equal(first.value.submission, 'unconfirmed')
  assert.equal(first.value.remote_task_cancelled, false)
  assert.match(first.value.reason, /ECONNRESET/)
  assert.equal(repeated.value.task_ref, first.value.task_ref)
  assert.equal(repeated.value.reused, true)
  assert.equal(f.calls.length, 1)
})

test('unusable dependency results are errors rather than fabricated empty success', async t => {
  const f = await fixture(t)
  for (const value of [null, undefined, [], 'not an object', 42]) {
    const mock = t.mock.method(f.client, 'list', async () => value)
    assert.equal(unpack(await f.run({ action: 'tasks' })).ok, false)
    mock.mock.restore()
  }
})

test('oversized error messages stay valid bounded JSON and disclose the diagnostic truncation', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'status', async () => { throw new Error(`action_failed ${'细节'.repeat(100000)}`) })
  const body = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
  assert.equal(body.ok, false)
  assert.match(body.error, /action_failed/)
  assert.equal(body.diagnostics.truncated, true)
  assert.ok(body.diagnostics.projection.truncated_fields.some(path => path.endsWith('error')))
})

// Synthetic observations only. Never copy live team state, prompts or logs into this public fixture.
function statusArtifact(index, extra = {}) {
  return {
    artifact_id: index.toString(16).padStart(32, '0'), name: 'same-name.txt', kind: 'file',
    sha256: index.toString(16).padStart(64, '0'), bytes: 12, download_eligible: true, ...extra,
  }
}

function statusObservation(extra = {}) {
  return {
    task_ref: REF, team_id: 'fixture-team', connection: 'connected',
    runtime: { tasks: [{ task_id: 'author', harness: 'fixture', status: 'done',
      result: { final: 'A file was written; this is not experience acceptance.' } }], cancelled: false },
    events: [], next_event_id: 9, more_events: false,
    artifacts: [statusArtifact(1), statusArtifact(2)],
    mailbox: { messages: [], next_message_id: 7, has_more: false },
    artifact_source: 'authenticated_service_exports_observed_for_this_task',
    experience_verification: 'not_performed_by_plugin',
    projection: { omitted_fields: [], truncated_fields: [] },
    ...extra,
  }
}

for (const volume of ['runtime', 'events', 'both']) {
  test(`status sections survive oversized ${volume} without losing exports, reasons or cursors`, async t => {
    const f = await fixture(t)
    const large = Array.from({ length: 65 }, () => ({
      text: `Bearer ${TOKEN} ${'合成运行细节。'.repeat(700)}`,
      // Small fields consume the remainder that a 4 KiB string alone cannot use.
      tail: Object.fromEntries(Array.from({ length: 90 }, (_, index) => [`detail_${index}`, String(index)])),
    }))
    const refusal = { name: 'fixture.tgz', reason: 'artifact_archive_unsupported',
      scan_status: 'not_scanned', downloadable: false, file_preserved: true }
    const raw = statusObservation({
      runtime: { tasks: [{
        ...(volume !== 'events' ? { tool_trace: large } : {}),
        result: { ...(volume !== 'events' ? { details: large } : {}),
          final: 'The author reports files, not verified delivery.',
          artifact_safety_observations: [refusal] },
        task_id: 'author', harness: 'fixture', status: 'done',
      }], cancelled: false },
      events: [{ event_id: 9, event: 'progress', data: {
        ...(volume !== 'runtime' ? { details: large } : {}),
        t: 'artifact_not_exported', ...refusal,
      } }],
      artifacts: [statusArtifact(1), statusArtifact(2),
        statusArtifact(3, { download_eligible: false, download_reason: 'Artifact has no valid SHA-256 hash' })],
      more_events: true,
    })
    t.mock.method(f.client, 'status', async () => raw)
    const response = await f.run({ action: 'status', args: { task_ref: REF } })
    const { ok, value } = unpack(response)
    assert.equal(ok, true)
    assert.equal(value.task_ref, REF)
    assert.equal(value.team_id, 'fixture-team')
    assert.equal(value.connection, 'connected')
    assert.ok(Array.isArray(value.artifacts), 'Large runtime/events must not consume the artifact section budget')
    assert.equal(value.runtime.tasks[0].task_id, 'author')
    assert.equal(value.runtime.tasks[0].status, 'done')
    assert.match(value.runtime.tasks[0].result.final, /author reports/)
    assert.equal(value.runtime.cancelled, false)
    assert.deepEqual(value.artifacts.map(row => row.artifact_id), raw.artifacts.map(row => row.artifact_id))
    assert.equal(value.artifacts[2].download_reason, 'Artifact has no valid SHA-256 hash')
    assert.equal(value.sections.artifacts.observed_count, 3)
    assert.equal(value.sections.artifacts.shown_count, 3)
    assert.equal(value.sections.artifacts.complete, false, 'Unseen event history is not a complete export inventory')
    assert.ok(value.export_observations.some(row => row.reason === refusal.reason && row.downloadable === false))
    assert.equal(value.next_event_id, 9)
    assert.equal(value.more_events, true)
    assert.equal(value.mailbox.next_message_id, 7)
    assert.equal(value.artifact_source, raw.artifact_source)
    assert.equal(value.experience_verification, 'not_performed_by_plugin')
    assert.equal(value.truncated, true)
    assert.doesNotMatch(response.text, /workbench-fixture\/private/)
    assert.equal(f.calls.length, 0)
  })
}

test('status distinguishes a missing, empty, upstream-truncated or not-yet-complete artifact list', async t => {
  const f = await fixture(t)
  for (const [artifacts, more, paths, available, complete] of [
    [undefined, false, [], false, false], [null, false, [], false, false],
    [[], false, [], true, true], [[], true, [], true, false],
    [[], false, ['$.artifacts'], true, false], [[], false, ['$'], true, false],
  ]) {
    const raw = statusObservation({ more_events: more,
      projection: { omitted_fields: [], truncated_fields: paths } })
    if (artifacts === undefined) delete raw.artifacts
    else raw.artifacts = artifacts
    const mock = t.mock.method(f.client, 'status', async () => raw)
    const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
    assert.equal(value.sections.artifacts.available, available)
    assert.equal(value.sections.artifacts.complete, complete)
    assert.equal(value.sections.artifacts.observed_count, available ? 0 : null)
    mock.mock.restore()
  }
})

test('status preserves unknown upstream truncation and does not certify an empty tail read', async t => {
  const f = await fixture(t)
  for (const [extra, args] of [
    [{ truncated: true }, {}],
    [{ projection: { omitted_fields: [], truncated_fields: [], paths_truncated: true } }, {}],
    [{}, { after_event_id: 8 }],
  ]) {
    const mock = t.mock.method(f.client, 'status', async () => statusObservation({ artifacts: [], ...extra }))
    const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF, ...args } }))
    assert.equal(value.sections.artifacts.complete, false, 'Unknown omissions or unread earlier history cannot prove zero exports')
    assert.equal(value.sections.exports.complete, false)
    if (extra.projection?.paths_truncated) {
      assert.equal(value.projection.paths_truncated, true, 'Do not erase upstream loss of truncation paths')
      assert.equal(value.truncated, true)
    }
    mock.mock.restore()
  }
})

test('status preserves actual mailbox facts and does not skip locally hidden messages', async t => {
  const f = await fixture(t)
  const messages = Array.from({ length: 40 }, (_, index) => ({
    id: index + 5, from: 'author', to: 'commander', ts: index + 1, read_at: null,
    text: '合成收尾消息。'.repeat(120), artifact_refs: [], text_source: 'inline', text_truncated: false,
  }))
  const mailbox = {
    source: 'team.mailboxes.commander', trust: 'untrusted_source', recipient: 'commander',
    state: 'available', messages, total_messages: 44, after_message_id: 4, next_message_id: 44,
    more_messages: true, truncated: false, read_receipts_modified: false, model_consumption: 'not_observed',
  }
  t.mock.method(f.client, 'status', async () => statusObservation({ mailbox, more_events: true }))
  const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF, after_message_id: 4 } }))
  for (const key of ['state', 'trust', 'total_messages', 'after_message_id', 'next_message_id',
    'more_messages', 'truncated', 'read_receipts_modified', 'model_consumption']) {
    assert.equal(value.mailbox[key], mailbox[key], `Preserve service mailbox fact ${key}`)
  }
  assert.ok(value.mailbox.messages.length > 0 && value.mailbox.messages.length < messages.length)
  assert.equal(value.sections.mailbox.complete, false)
  assert.equal(value.sections.mailbox.has_more, true)
  assert.equal(value.sections.mailbox.next_after_message_id, value.mailbox.messages.at(-1).id)
  assert.equal(value.sections.events.complete, false, 'More source events are not a complete event list')
})

test('status artifact pages preserve same-name hash history and advance only over displayed rows', async t => {
  const f = await fixture(t)
  const raw = statusObservation({ artifacts: Array.from({ length: 135 }, (_, index) => statusArtifact(index + 1)) })
  const original = JSON.stringify(raw)
  const calls = []
  t.mock.method(f.client, 'status', async args => { calls.push(args); return raw })
  let offset = 0
  const seen = []
  do {
    const { ok, value } = unpack(await f.run({ action: 'status',
      args: { task_ref: REF, after_event_id: 5, after_message_id: 3, artifact_offset: offset } }))
    assert.equal(ok, true)
    const page = value.sections.artifacts
    assert.equal(page.observed_count, 135)
    assert.equal(page.offset, offset)
    assert.equal(page.shown_count, value.artifacts.length)
    assert.ok(page.shown_count > 0 && page.shown_count <= 100)
    seen.push(...value.artifacts.map(row => row.artifact_id))
    if (page.next_offset !== null) assert.equal(page.next_offset, offset + value.artifacts.length)
    offset = page.next_offset
  } while (offset !== null)
  assert.deepEqual(seen, raw.artifacts.map(row => row.artifact_id))
  assert.deepEqual(calls, calls.map(() => ({ task_ref: REF, after_event_id: 5, after_message_id: 3 })))
  assert.equal(JSON.stringify(raw), original, 'Projection must not rewrite source observations or historical hashes')
})

test('event projection separates the upstream cursor from the last actually displayed event', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'status', async () => statusObservation({
    events: Array.from({ length: 150 }, (_, index) => ({
      event_id: index + 1, event: 'progress', data: { text: '合成事件摘要。'.repeat(80) },
    })),
    next_event_id: 200, more_events: false,
  }))
  const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
  assert.equal(value.next_event_id, 200, 'Do not falsify the service cursor')
  assert.equal(value.more_events, false, 'Service and UI truncation are separate')
  assert.ok(value.events.length > 0 && value.events.length < 150)
  assert.equal(value.sections.events.observed_count, 150)
  assert.equal(value.sections.events.complete, false)
  assert.equal(value.sections.events.has_more, true)
  assert.equal(value.sections.events.next_after_event_id, value.events.at(-1).event_id,
    'The UI must not skip the events hidden by its own byte cap')
})

test('export projection retains typed result and event observations without treating final prose as a reason', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'status', async () => statusObservation({
    runtime: { tasks: [{ task_id: 'author', status: 'done', result: {
      final: 'I think exports failed because PROSE_IS_NOT_A_REASON.',
      artifact_export: { exported_files: 0, observations: [
        { name: 'project', reason: 'artifact_project_binding_required', downloadable: false },
      ] },
      artifact_safety_observations: [{ name: 'file.tgz', reason: 'artifact_archive_unsupported', downloadable: false }],
    } }] },
    events: [{ event_id: 9, event: 'artifact_not_exported',
      data: { name: 'other.txt', reason: 'artifact_security_unavailable', file_preserved: true } }],
  }))
  const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
  const notes = value.export_observations
  assert.ok(notes.some(row => row.reason === 'artifact_project_binding_required'))
  assert.ok(notes.some(row => row.reason === 'artifact_archive_unsupported'))
  assert.ok(notes.some(row => row.reason === 'artifact_security_unavailable' && row.event_id === 9))
  assert.ok(notes.every(row => typeof row.source_path === 'string'))
  assert.doesNotMatch(JSON.stringify(notes), /PROSE_IS_NOT_A_REASON/)
})

for (const origin of ['safety_observations', 'artifact_export', 'event']) {
  test(`export section reports shortened ${origin} text as incomplete`, async t => {
    const f = await fixture(t)
    const note = { name: 'fixture.tgz', reason: 'synthetic_reason_'.repeat(100),
      downloadable: false, file_preserved: true }
    const raw = statusObservation(origin === 'event'
      ? { events: [{ event_id: 9, event: 'artifact_not_exported', data: note }] }
      : { runtime: { tasks: [{ task_id: 'author', status: 'done', result: origin === 'artifact_export'
        ? { artifact_export: note } : { artifact_safety_observations: [note] } }] } })
    const original = JSON.stringify(raw)
    t.mock.method(f.client, 'status', async () => raw)
    const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
    assert.equal(value.export_observations.length, 1)
    assert.equal(Buffer.byteLength(value.export_observations[0].reason), 600)
    assert.equal(value.sections.exports.observed_count, 1)
    assert.equal(value.sections.exports.shown_count, 1)
    assert.equal(value.sections.exports.complete, false,
      'A complete row count does not mean its shortened explanation is complete')
    assert.equal(value.sections.artifacts.complete, true,
      'Export explanation loss must not make the independent file list incomplete')
    assert.equal(value.truncated, true)
    assert.ok(value.projection.truncated_fields.some(path => path.endsWith('.reason')))
    assert.equal(JSON.stringify(raw), original)
  })
}

test('complete export observations remain complete when unrelated task prose is shortened', async t => {
  const f = await fixture(t)
  t.mock.method(f.client, 'status', async () => statusObservation({
    runtime: { tasks: [{ task_id: 'author', status: 'done', result: {
      final: 'Unrelated author prose. '.repeat(100),
      artifact_safety_observations: [{ name: 'fixture.tgz', reason: 'artifact_archive_unsupported',
        downloadable: false, file_preserved: true }],
    } }] },
  }))
  const { value } = unpack(await f.run({ action: 'status', args: { task_ref: REF } }))
  assert.equal(value.sections.exports.complete, true)
  assert.equal(value.sections.artifacts.complete, true)
  assert.equal(value.truncated, true, 'Other sections still report their own projection loss')
})

test('team wiring: the optional fifth dependency routes all four flow actions with only Host identity and caller arguments', async t => {
  const f = await fixture(t)
  const execute = await adapter()
  for (const [action, method, args] of [
    ['flow_status', 'status', undefined], ['flow_start', 'start', {}],
    ['flow_pause', 'pause', undefined], ['flow_run', 'run', { key: '再次-🧭' }],
  ]) {
    const calls = []
    const observation = {
      configured: true, enabled: true, code: 'partial', partial: true,
      schedules: [{ id: 'original-schedule', status: 'active', scheduledAt: '2026-10-31T01:00:00.000Z' }],
      observations: [{ code: 'create_failed', message: '保留已经安排的任务；本项失败' }],
      execution: 'not_verified',
    }
    const flow = { async [method](...values) { calls.push(values); return observation } }
    const current = invocation({ action, ...(args ? { args } : {}) })
    const result = unpack(await execute(f.ctx, f.client, f.sources, current, flow))
    assert.equal(result.ok, true)
    assert.equal(result.action, action)
    assert.equal(result.value.code, 'partial')
    assert.equal(result.value.execution, 'not_verified')
    assert.deepEqual(result.value.schedules, observation.schedules)
    assert.deepEqual(result.value.observations, observation.observations)
    assert.deepEqual(calls, [method === 'run'
      ? [args, { sessionId: current.agent.id, signal: current.signal }]
      : [{ sessionId: current.agent.id, signal: current.signal }]])
  }
  assert.equal(f.calls.length, 0)
})

test('team wiring: old four-argument calls still work and an absent flow gives an actionable error', async t => {
  const f = await fixture(t)
  const execute = await adapter()
  t.mock.method(f.client, 'list', async () => ({ tasks: [], source: 'local_recovery_index_not_live_status' }))
  assert.equal(unpack(await execute(f.ctx, f.client, f.sources, invocation({ action: 'tasks' }))).ok, true)
  for (const action of ['flow_status', 'flow_start', 'flow_pause', 'flow_run']) {
    const result = unpack(await execute(f.ctx, f.client, f.sources,
      invocation({ action, ...(action === 'flow_run' ? { args: { key: 'retry-key' } } : {}) })))
    assert.equal(result.ok, false)
    assert.equal(result.action, action, 'Recognize the action even when the optional dependency is absent')
    assert.match(result.error, /战队|flow|schedule/)
  }
  assert.equal(f.calls.length, 0)
})

test('team wiring: flow fields and run keys are validated before any schedule or other dependency access', async t => {
  const f = await fixture(t)
  const execute = await adapter()
  const flow = Object.fromEntries(['status', 'start', 'pause', 'run']
    .map(method => [method, () => assert.fail('Invalid flow input must be rejected before dependency access')]))
  const inputs = ['flow_status', 'flow_start', 'flow_pause', 'flow_run']
    .map(action => ({ action, args: action === 'flow_run' ? { key: 'valid-key' } : {} }))
  for (const input of inputs) {
    for (const field of ['sessionId', 'owner', 'signal', 'teamProfile', 'candidateHarnesses',
      'authority', 'config', 'attachments', '__proto__', 'PRIVATE_FLOW_INPUT']) {
      for (const raw of [
        { ...input, [field]: 'PRIVATE_FLOW_INPUT' },
        { ...input, args: { ...input.args, [field]: 'PRIVATE_FLOW_INPUT' } },
      ]) {
        const result = await execute(f.ctx, f.client, f.sources, invocation(raw), flow)
        assert.equal(unpack(result).ok, false)
        assert.ok(!result.text.includes('PRIVATE_FLOW_INPUT'))
      }
    }
    for (const args of [null, [], 1]) {
      assert.equal(unpack(await execute(f.ctx, f.client, f.sources, invocation({ action: input.action, args }), flow)).ok, false)
    }
  }
  for (const key of [undefined, null, '', ' ', ' leading', 'trailing ', 'a\nb', 'a\u0000b', 'a\u007fb',
    '\ud800', 'x'.repeat(129), 1, true, {}, []]) {
    const result = await execute(f.ctx, f.client, f.sources, invocation({ action: 'flow_run', args: { key } }), flow)
    assert.equal(unpack(result).ok, false)
  }
  const valid = await execute(f.ctx, f.client, f.sources, invocation({ action: 'flow_run', args: { key: 'x'.repeat(128) } }),
    { async run(args) { return { acceptedKey: args.key, execution: 'not_verified' } } })
  const accepted = unpack(valid)
  assert.equal(accepted.ok, true, 'A valid flow_run key must reach the optional flow dependency')
  assert.equal(accepted.value.acceptedKey.length, 128)
  assert.equal(f.calls.length, 0)
})

test('team wiring: flow cannot be triggered by missing identity, invalid signals, attachments or a pre-aborted caller', async t => {
  const f = await fixture(t)
  const execute = await adapter()
  const flow = { start() { assert.fail('No valid Host invocation, no flow call') } }
  const current = invocation({ action: 'flow_start' })
  const cancelled = new AbortController()
  cancelled.abort(new Error('PRIVATE_FLOW_ABORT'))
  for (const input of [
    { ...current, agent: undefined }, { ...current, agent: { id: '' } },
    { ...current, signal: undefined }, { ...current, signal: {} },
    { ...current, attachments: [{ type: 'file', content: 'PRIVATE_FLOW_ATTACHMENT' }] },
    { ...current, attachments: {} }, { ...current, signal: cancelled.signal },
  ]) {
    const result = await execute(f.ctx, f.client, f.sources, input, flow)
    assert.equal(unpack(result).ok, false)
    assert.doesNotMatch(result.text, /PRIVATE_FLOW_ABORT|PRIVATE_FLOW_ATTACHMENT/)
  }
  assert.equal(f.calls.length, 0)
})

test('team wiring: flow observations and failures retain original uncertainty while using the existing bounded redaction', async t => {
  const f = await fixture(t)
  const execute = await adapter()
  const current = invocation({ action: 'flow_status' })
  const result = await execute(f.ctx, f.client, f.sources, current, { async status() {
    return {
      configured: true, enabled: null, code: 'service_missing', partial: false, execution: 'not_verified',
      schedules: [], observations: [{ code: 'service_missing', message: `原生 schedule 不可用 Bearer ${TOKEN}` }],
      description: '很长的原生说明'.repeat(2000), token: TOKEN,
    }
  } })
  const body = unpack(result)
  assert.equal(body.ok, true)
  assert.equal(body.value.enabled, null)
  assert.equal(body.value.code, 'service_missing')
  assert.equal(body.value.execution, 'not_verified')
  assert.ok(body.value.truncated)
  assert.ok(body.value.projection.omitted_fields.includes('$.token'))
  assert.ok(body.value.projection.truncated_fields.includes('$.description'))
  assert.ok(!result.text.includes(TOKEN))
  const failed = await execute(f.ctx, f.client, f.sources, current, { async status() {
    const error = new Error(`原生目录读取失败 Bearer ${TOKEN}`)
    error.cause = { token: 'PRIVATE_FLOW_CAUSE' }
    throw error
  } })
  assert.equal(unpack(failed).ok, false)
  assert.doesNotMatch(failed.text, /PRIVATE_FLOW_CAUSE/)
  assert.ok(!failed.text.includes(TOKEN))
  assert.match(unpack(failed).error, /原生目录读取失败/)
})

// Core review P2-3: every observed task must remain reachable, not just the first 100.
test('repair: task pages cover long indexes using whole rows and snapshot-bound cursors', async t => {
  const f = await fixture(t)
  const raw = { source: 'local_recovery_index_not_live_status', tasks: Array.from({ length: 135 }, (_, i) => ({
    task_ref: (i + 1).toString(16).padStart(32, '0'), key: `task-${i}-${'长'.repeat(150)}`,
    created_at: '2026-10-30T00:00:00Z', description: `Bearer ${TOKEN} ${'huge'.repeat(5000)}`,
  })) }
  const original = JSON.stringify(raw)
  t.mock.method(f.client, 'list', async execution => {
    assert.equal(execution.sessionId, 'workbench-agent')
    return raw
  })
  let offset = 0, snapshot
  const seen = []
  do {
    const result = await f.run({ action: 'tasks', args: { offset, limit: 30, ...(snapshot ? { snapshot } : {}) } })
    assert.ok(!result.text.includes(TOKEN))
    const { ok, value } = unpack(result)
    assert.equal(ok, true)
    const page = value.task_page
    assert.equal(page.available, true)
    assert.equal(page.total, 135)
    assert.equal(page.offset, offset)
    assert.ok(page.shown_count > 0 && page.shown_count <= 30)
    assert.equal(page.shown_count, value.tasks.length)
    assert.match(value.connection_id, /^[a-f0-9]{64}$/)
    if (snapshot) assert.equal(page.snapshot, snapshot)
    snapshot = page.snapshot
    seen.push(...value.tasks.map(row => row.task_ref))
    if (page.next_offset !== null) assert.equal(page.next_offset, offset + page.shown_count)
    offset = page.next_offset
  } while (offset !== null)
  assert.deepEqual(seen, raw.tasks.map(row => row.task_ref))
  assert.equal(JSON.stringify(raw), original)
})

test('repair: task continuation rejects changed snapshots and unowned cursor fields', async t => {
  const f = await fixture(t)
  let tasks = Array.from({ length: 31 }, (_, i) => ({ task_ref: i.toString(16).padStart(32, '0'), key: `task-${i}` }))
  t.mock.method(f.client, 'list', async () => ({ tasks }))
  const first = unpack(await f.run({ action: 'tasks', args: { limit: 30 } })).value
  assert.ok(first.task_page?.snapshot)
  tasks = [{ task_ref: 'f'.repeat(32), key: 'inserted' }, ...tasks]
  const changed = unpack(await f.run({ action: 'tasks', args: { offset: 30, snapshot: first.task_page.snapshot } }))
  assert.equal(changed.ok, false)
  assert.match(changed.error, /变化|changed/i)
  for (const args of [{ offset: 30 }, { offset: -1 }, { limit: 0 }, { limit: 31 },
    { snapshot: 'invalid' }, { sessionId: 'foreign' }, { connection_id: 'a'.repeat(64) }]) {
    assert.equal(unpack(await f.run({ action: 'tasks', args })).ok, false)
  }
})

test('repair: missing or upstream-incomplete task lists never claim known empty', async t => {
  const f = await fixture(t)
  for (const raw of [{}, { tasks: null }, { tasks: 'missing' }, { tasks: [], error: 'read failed' },
    { tasks: [], available: false }, { tasks: [], complete: false },
    { tasks: [], truncated: true }, { tasks: [], projection: { omitted_fields: ['$.tasks'] } }]) {
    t.mock.method(f.client, 'list', async () => raw)
    const { value } = unpack(await f.run({ action: 'tasks' }))
    assert.equal(value.task_page?.complete, false)
    assert.equal(value.task_page?.source_complete, false)
    if (!Array.isArray(raw.tasks)) {
      assert.equal(value.task_page.available, false)
      assert.equal(value.task_page.total, null)
    }
  }
  t.mock.method(f.client, 'list', async () => ({ tasks: [] }))
  const empty = unpack(await f.run({ action: 'tasks' })).value
  assert.equal(empty.task_page.complete, true)
  assert.equal(empty.task_page.total, 0)
})

test('repair: a submit connection fence is verified by native identity before dispatch', async t => {
  const f = await fixture(t), other = await fixture(t)
  const first = unpack(await f.run({ action: 'tasks' })).value
  assert.match(first.connection_id ?? '', /^[a-f0-9]{64}$/)
  t.mock.method(other.client, 'submit', () => assert.fail('Foreign connection must not consume a submission key'))
  assert.equal(unpack(await other.run({ action: 'submit', args: {
    key: 'original-key', query: 'original task', connection_id: first.connection_id,
  } })).ok, false)
  const calls = []
  t.mock.method(f.client, 'submit', async (args, execution) => {
    calls.push({ args, execution })
    return { task_ref: REF, submission: 'unconfirmed' }
  })
  assert.equal(unpack(await f.run({ action: 'submit', args: {
    key: 'original-key', query: 'original task', connection_id: first.connection_id,
  } })).ok, true)
  assert.deepEqual(calls[0].args, { key: 'original-key', query: 'original task' })
  assert.equal(calls[0].execution.sessionId, 'workbench-agent')
})
