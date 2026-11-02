import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LegionClient } from '../lib/client.js'

const candidates = ['member-b', 'member-a']
const leaderProfile = { collaboration: 'leader', leader: 'member-a' }
const execution = () => ({ sessionId: 'leader-wiring-session', signal: new AbortController().signal })

// Real client, validation, SSE parsing and disk store; only outbound fetch is
// replaced. No server, model, credentials from the environment, or automation.
async function setup(t, config = {}) {
  const root = await mkdtemp(join(tmpdir(), 'legion-leader-wiring-'))
  const options = {
    baseUrl: 'https://legion.invalid', stateDirectory: root,
    tokenEnv: 'LEADER_WIRING_TOKEN', requestTimeoutMs: 2000, ...config,
  }
  const env = { LEADER_WIRING_TOKEN: 'synthetic-offline-token' }
  const clients = []
  const makeClient = (patch = {}, environment = env) => {
    const client = new LegionClient({ ...options, ...patch }, environment)
    clients.push(client)
    return client
  }
  t.after(async () => {
    clients.forEach(client => client.dispose())
    await rm(root, { recursive: true, force: true })
  })
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(String(url), 'https://legion.invalid/api/legion/dispatch')
    init.signal.throwIfAborted()
    const body = JSON.parse(init.body)
    requests.push(body)
    return new Response(`event: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`,
      { headers: { 'content-type': 'text/event-stream' } })
  })
  return { root, makeClient, client: makeClient(), requests }
}

test('selected leader reaches the native orchestrate/captain request, not only prompt text', async t => {
  const profile = { collaboration: 'leader', leader: ' member-a ' }
  const selected = [...candidates]
  const f = await setup(t, { teamProfile: profile, candidateHarnesses: selected })
  profile.leader = 'member-b'
  selected.reverse()
  const receipt = await f.client.submit({
    key: 'original-request', query: '修好本次协作', context: '保留原工程',
  }, execution())
  assert.equal(receipt.submission, 'accepted')
  const body = f.requests[0]
  assert.equal(body.mode, 'orchestrate')
  assert.deepEqual(body.plan, {
    team_policy: { candidate_harnesses: ['member-b', 'member-a'] }, captain: 'member-a',
  })
  assert.equal(body.allow_direct_model, false)
  assert.ok(body.question.startsWith('修好本次协作\n\n'))
  assert.ok(body.question.includes('保留原工程'))
  assert.ok(body.question.includes('用户指定带队者：member-a'))
  assert.deepEqual(Object.keys(body), ['question', 'mode', 'allow_direct_model', 'session_id', 'team_id', 'plan'])
  // Exact plan equality also excludes tasks, captain_candidates and permission
  // allowlists: choosing companions must not force every member to participate.
  assert.equal((await f.client.submit({
    key: 'original-request', query: '修好本次协作', context: '保留原工程',
  }, execution())).reused, true)
  assert.equal(f.requests.length, 1)
})

for (const [label, profile] of [
  ['no profile', undefined],
  ['defaults', {}],
  ['auto with dormant leader', { collaboration: 'auto', leader: 'member-a' }],
  ['custom with dormant leader', { collaboration: 'custom', leader: 'member-a', instructions: '按本次需求协作' }],
  ['leader', leaderProfile],
]) {
  test(`${label}: model arguments cannot select mode, captain or a replacement plan`, async t => {
    const f = await setup(t, { teamProfile: profile, candidateHarnesses: candidates })
    await f.client.submit({
      key: 'injected', query: '原始问题',
      mode: 'pk', captain: 'injected', plan: { captain: 'injected', tasks: { forced: {} } },
      teamProfile: { collaboration: 'leader', leader: 'injected' },
      candidateHarnesses: ['injected'],
    }, execution())
    assert.equal(f.requests[0].mode, label === 'leader' ? 'orchestrate' : 'auto')
    assert.deepEqual(f.requests[0].plan, {
      team_policy: { candidate_harnesses: ['member-b', 'member-a'] },
      ...(label === 'leader' ? { captain: 'member-a' } : {}),
    })
    assert.ok(!JSON.stringify(f.requests[0]).includes('injected'))
  })
}

for (const [label, teamProfile] of [
  ['absent', undefined], ['default', {}],
  ['custom', { collaboration: 'custom', instructions: '按本次需求协作' }],
]) {
  test(`${label} profile without candidates preserves auto with no plan`, async t => {
    const f = await setup(t, { teamProfile })
    await f.client.submit({ key: 'no-plan', query: '原始问题', mode: 'orchestrate', captain: 'injected' }, execution())
    assert.equal(f.requests[0].mode, 'auto')
    assert.equal(Object.hasOwn(f.requests[0], 'plan'), false)
    if (teamProfile === undefined) assert.equal(f.requests[0].question, '原始问题')
  })
}

// Frozen pre-fix binding digests, independently derived from the old payload:
// { project_context, plan: { team_policy: { candidate_harnesses } }, teamProfile }
// with normalized defaults and the candidate order above. Never derive these
// expectations from the current client's request plan or normalization helper.
for (const [label, binding, legacyDigest] of [
  ['no project', null, '7ca7817e01f7a4d1fe76429482e2e63637b03a324046139db8197675dd4f9fad'],
  ['bound project', { workspace_root: '/fixture/authorized-project' }, 'baf8d198d1f9c5a5c248a92feca9b1bc1f70d578fae5d171b839513b3f3fc39d'],
]) {
  test(`${label}: old leader keys and previous_task_ref survive reload without digest migration`, async t => {
    const f = await setup(t, { teamProfile: leaderProfile, candidateHarnesses: candidates })
    const patch = {}
    if (binding) {
      patch.projectBindingFile = join(f.root, 'binding.json')
      await writeFile(patch.projectBindingFile, JSON.stringify(binding))
    }
    const seed = f.makeClient(patch)
    const original = { key: 'old-key', query: '旧任务', context: '旧上下文' }
    const { ref } = await seed.store.reserve({ sessionId: execution().sessionId, ...original, bindingDigest: legacyDigest })
    await seed.store.writeObservation(ref, 'submission', { submission: 'accepted' })
    const followup = { key: 'old-followup', query: '旧反馈', context: '', previous_task_ref: ref.id }
    const { ref: oldNext } = await seed.store.reserve({
      sessionId: execution().sessionId, ...followup, bindingDigest: legacyDigest, previousRef: ref,
    })
    await seed.store.writeObservation(oldNext, 'submission', { submission: 'accepted' })
    const paths = [ref, oldNext].map(row => join(seed.store.path(row.id), 'ref.json'))
    const before = await Promise.all(paths.map(path => readFile(path, 'utf8')))
    seed.dispose()
    const client = f.makeClient(patch)
    const reused = await client.submit(original, execution())
    assert.equal(reused.task_ref, ref.id)
    assert.equal(reused.reused, true)
    assert.equal((await client.submit(followup, execution())).task_ref, oldNext.id)
    assert.equal(f.requests.length, 0, 'Previously accepted tasks must not dispatch again after the wiring fix')
    assert.deepEqual(await Promise.all(paths.map(path => readFile(path, 'utf8'))), before)

    const next = await client.submit({
      key: 'new-followup', query: '新反馈', previous_task_ref: oldNext.id,
    }, execution())
    assert.equal(next.session_id, ref.session_id)
    assert.notEqual(next.team_id, oldNext.team_id)
    assert.equal(f.requests.length, 1)
    assert.equal(f.requests[0].session_id, ref.session_id)
    assert.deepEqual(f.requests[0].project_context ?? null, binding)
    assert.equal((await client.store.get(execution().sessionId, next.task_ref)).previous_task_ref, oldNext.id)
    assert.deepEqual(await Promise.all(paths.map(path => readFile(path, 'utf8'))), before)

    const changed = f.makeClient({ ...patch, teamProfile: { collaboration: 'leader', leader: 'member-b' } })
    await assert.rejects(changed.submit(original, execution()), /different input/)
    await assert.rejects(client.submit({ key: 'cross-session', query: 'x', previous_task_ref: ref.id },
      { ...execution(), sessionId: 'other-session' }), /different DSH session/)
    const rotated = f.makeClient(patch, { LEADER_WIRING_TOKEN: 'different-synthetic-token' })
    await assert.rejects(rotated.submit({ key: 'wrong-owner', query: 'x', previous_task_ref: ref.id }, execution()),
      /different configured service/)
    assert.equal(f.requests.length, 1, 'Changed configuration or foreign identity must not send another request')
  })
}

test('an unselected leader is rejected before reserving a key or sending a request', async t => {
  const f = await setup(t, { teamProfile: leaderProfile, candidateHarnesses: ['member-b'] })
  await assert.rejects(f.client.submit({ key: 'not-consumed', query: 'x' }, execution()), /candidate|候选/)
  assert.equal(f.requests.length, 0)
  assert.deepEqual((await f.client.list(execution())).tasks, [])
})
