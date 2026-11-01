import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LegionClient } from '../lib/client.js'
import { fixture } from './fixture.js'

// Synthetic HTTP protocol tests, not a substitute for a real DSH/Legion run.
async function setup(t, options = {}, config = {}) {
  const server = await fixture(t, options)
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-boundary-test-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const client = new LegionClient({
    baseUrl: server.baseUrl, stateDirectory, tokenEnv: 'FIXTURE_TOKEN',
    requestTimeoutMs: 2000, ...config,
  }, { FIXTURE_TOKEN: 'fixture-token' })
  t.after(() => client.dispose())
  const execution = { sessionId: 'boundary-test-session', signal: new AbortController().signal }
  const args = { key: 'original', query: 'Synthetic protocol test' }
  const start = await client.submit(args, execution)
  const status = after_event_id => client.status({ task_ref: start.task_ref, after_event_id }, execution)
  const download = artifact => client.download({
    task_ref: start.task_ref, artifact_id: artifact.artifact_id,
  }, execution)
  const artifact = (patch = {}) => ({
    name: 'result.txt', url: '/outputs/result.txt', sha256: server.sha256,
    bytes: server.bytes.length, team_id: start.team_id, ...patch,
  })
  return { client, server, execution, args, start, status, download, artifact }
}

test('exports keep the configured reverse-proxy prefix in all supported URL forms', async t => {
  const f = await setup(t, { prefix: '/legion' })
  const urls = ['/outputs/result.txt', '/legion/outputs/result.txt', `${f.server.baseUrl}/outputs/result.txt`]
  for (const url of urls) {
    const row = f.client.artifact(f.artifact({ url }), f.start.team_id)
    assert.equal(row.download_eligible, true)
    assert.equal(new URL(row.url).pathname, '/legion/outputs/result.txt')
  }
  f.server.finish(f.start.team_id, [f.artifact()])
  const state = await f.status()
  const file = await f.download(state.artifacts[0])
  assert.deepEqual(await readFile(file.local_path), f.server.bytes)
  assert.ok(f.server.requests.every(x => x.path.startsWith('/legion/')))
})

test('task map keys and conversation binding remain authoritative', async t => {
  const f = await setup(t)
  const team = f.server.teams.get(f.start.team_id)
  team.tasks.author.task_id = 'different-task'
  assert.equal((await f.status()).runtime.tasks[0].task_id, 'author')
  team.conversation_id = 'different-conversation'
  await assert.rejects(f.status(), /conversation|session|identity/i)
})

for (const kind of ['message', 'resume']) {
  test(`${kind} keeps a known refusal across same-key recovery without resending`, async t => {
    const options = { responses: {} }
    const f = await setup(t, options)
    options.responses[`/api/legion/team/${kind}`] = {
      status: 403, body: { reason: 'Synthetic refusal fixture-token' },
    }
    const args = { task_ref: f.start.task_ref, operation_key: 'op',
      ...(kind === 'message' ? { recipient: 'author', text: 'Synthetic feedback' } : {}) }
    await assert.rejects(f.client[kind](args, f.execution), /403/)
    const restored = await f.client[kind](args, f.execution)
    assert.equal(restored.acknowledgement, 'rejected')
    assert.equal(restored.http_status, 403)
    assert.match(restored.reason, /Synthetic refusal/)
    assert.ok(!JSON.stringify(restored).includes('fixture-token'))
    assert.equal(f.server.requests.filter(x => x.path.endsWith(`/team/${kind}`)).length, 1)
  })

  test(`${kind} does not turn a server failure into a definite rejection`, async t => {
    const options = { responses: {} }
    const f = await setup(t, options)
    options.responses[`/api/legion/team/${kind}`] = {
      status: 503, body: { error: 'Synthetic response failure' },
    }
    const args = { task_ref: f.start.task_ref, operation_key: 'uncertain',
      ...(kind === 'message' ? { recipient: 'author', text: 'Synthetic feedback' } : {}) }
    const first = await f.client[kind](args, f.execution)
    const restored = await f.client[kind](args, f.execution)
    assert.equal(first.acknowledgement, 'unconfirmed')
    assert.equal(restored.acknowledgement, 'unconfirmed')
    assert.equal(restored.http_status, 503)
    assert.equal(f.server.requests.filter(x => x.path.endsWith(`/team/${kind}`)).length, 1)
  })
}

test('a cancellation refusal is not reported as a registered cancellation', async t => {
  const f = await setup(t, { responses: {
    '/api/legion/cancel': { status: 200, body: { ok: false, reason: 'Synthetic refusal' } },
  } })
  const result = await f.client.cancel({ task_ref: f.start.task_ref }, f.execution)
  assert.equal(result.cancellation, 'rejected')
  assert.equal(result.stopped_confirmed, false)
})

test('artifact metadata is redacted and credential-bearing URLs are refused, never rewritten for use', async t => {
  const f = await setup(t)
  for (const tokenPath of ['fixture-token', '%66ixture-token']) {
    f.server.finish(f.start.team_id, [f.artifact({
      kind: 'fixture-token', export_snapshot_id: 'fixture-token',
      url: `/outputs/${tokenPath}/result.txt`,
    })])
    const state = await f.status()
    const ref = await f.client.store.get(f.execution.sessionId, f.start.task_ref)
    const saved = await f.client.store.readObservation(ref, 'artifacts')
    assert.ok(!JSON.stringify(state.artifacts).includes('fixture-token'))
    assert.ok(!JSON.stringify(saved).includes('fixture-token'))
    assert.ok(state.artifacts.every(row => !row.download_eligible))
    assert.ok(saved.every(row => row.url === undefined))
  }
  assert.ok(!f.server.requests.some(x => x.path.startsWith('/outputs/')))
})

test('a singleton artifact_ready event is collected and its provenance hash verifies the download', async t => {
  const f = await setup(t)
  const row = f.artifact({ provenance: { sha256: f.server.sha256 }, sha256: undefined })
  f.server.teams.get(f.start.team_id).events.push({
    event_id: 2, event: 'step', data: { t: 'artifact_ready', ...row },
  })
  const state = await f.status()
  assert.equal(state.artifacts.length, 1)
  assert.equal(state.artifacts[0].sha256, f.server.sha256)
  assert.equal((await f.download(state.artifacts[0])).integrity, 'sha256_verified')
})

test('conflicting export hashes remain visible but cannot authorize a download', async t => {
  const f = await setup(t)
  f.server.finish(f.start.team_id, [f.artifact({ provenance: { sha256: '0'.repeat(64) } })])
  const state = await f.status()
  assert.equal(state.artifacts[0].download_eligible, false)
  await assert.rejects(f.download(state.artifacts[0]), /hash|conflict/i)
})

test('a full service page and a filtered empty page do not falsely end the replay', async t => {
  for (const filtered of [false, true]) {
    const f = await setup(t, { hiddenEventIds: filtered ? new Set(Array.from({ length: 500 }, (_, i) => i + 1)) : undefined },
      { maxEvents: 500 })
    f.server.teams.get(f.start.team_id).events = Array.from({ length: 501 }, (_, i) => ({
      event_id: i + 1, event: 'step', data: { text: `Synthetic event ${i + 1}` },
    }))
    const first = await f.status(0)
    assert.equal(first.events.length, filtered ? 0 : 500)
    assert.equal(first.next_event_id, 500)
    assert.equal(first.more_events, true)
    const second = await f.status(first.next_event_id)
    assert.equal(second.events[0].event_id, 501)
    const drained = await f.status(second.next_event_id)
    assert.equal(drained.more_events, false)
  }
})

test('reconnection preserves cancellation requests without claiming the worker stopped', async t => {
  const f = await setup(t)
  const requests = [{ scope: 'all', key: '__all__', reason: 'Synthetic stop request' }]
  f.server.teams.get(f.start.team_id).cancel_requests = requests
  const state = await f.status()
  assert.deepEqual(state.runtime.cancel_requests, requests)
  assert.equal(state.runtime.tasks[0].status, 'running')
})

test('an HTTP500 after dispatch acceptance stays uncertain and does not resubmit', async t => {
  const f = await setup(t, { dispatchStatus: 500 })
  assert.equal(f.start.submission, 'unconfirmed')
  assert.equal(f.start.http_status, 500)
  const restored = await f.client.submit(f.args, f.execution)
  assert.equal(restored.submission, 'unconfirmed')
  assert.equal(restored.http_status, 500)
  assert.equal(f.server.requests.filter(x => x.path.endsWith('/dispatch')).length, 1)
  assert.equal((await f.status()).runtime.tasks[0].status, 'running')
})
