import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fixture } from './fixture.js'

async function setup(t, options = {}) {
  const module = await import('../lib/client.js').catch(() => ({}))
  assert.equal(typeof module.LegionClient, 'function', 'LegionClient must exist')
  const server = await fixture(t, options)
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-client-test-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const config = { baseUrl: server.baseUrl, stateDirectory, tokenEnv: 'FIXTURE_TOKEN', requestTimeoutMs: 2000 }
  const env = { FIXTURE_TOKEN: 'fixture-token' }
  const client = new module.LegionClient(config, env)
  return { client, server, config, env, LegionClient: module.LegionClient, stateDirectory }
}
const execution = () => ({ sessionId: 'dsh-session-A', signal: new AbortController().signal })

test('submission persists identity, sends through whole-Legion dispatch, and never repeats a task key', async t => {
  const { client, server } = await setup(t)
  const args = { key: 'draft', query: 'Write the original task', context: 'Audience: developers' }
  const first = await client.submit(args, execution())
  assert.equal(first.submission, 'accepted')
  assert.match(first.task_ref, /^[a-f0-9]{32}$/)
  const second = await client.submit(args, execution())
  assert.equal(second.task_ref, first.task_ref)
  assert.equal(second.reused, true)
  const posts = server.requests.filter(x => x.path === '/api/legion/dispatch')
  assert.equal(posts.length, 1)
  assert.equal(posts[0].body.mode, 'auto')
  assert.equal(posts[0].body.allow_direct_model, false)
  assert.ok(posts[0].body.question.includes('Write the original task'))
  assert.ok(posts[0].body.question.includes('Audience: developers'))
  assert.ok(!('uid' in posts[0].body))
  assert.ok(!('project_context' in posts[0].body))
  assert.ok(!('plan' in posts[0].body))
})

test('user-configured candidates are snapshotted and sent exactly, without changing auto mode', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const candidateHarnesses = ['generic-member', 'generic-reviewer']
  const client = new LegionClient({ ...config, candidateHarnesses }, env)
  candidateHarnesses.reverse()
  const result = await client.submit({ key: 'selected', query: 'Original task', context: 'Original context' }, execution())
  assert.equal(result.submission, 'accepted')
  const body = server.requests[0].body
  assert.deepEqual(body.plan, { team_policy: { candidate_harnesses: ['generic-member', 'generic-reviewer'] } })
  assert.equal(body.mode, 'auto')
  assert.equal(body.allow_direct_model, false)
  assert.ok(body.question.includes('Original task'))
  assert.ok(body.question.includes('Original context'))
  assert.ok(!Object.hasOwn(body, 'candidateHarnesses'))
})

test('client rejects invalid candidate lists, empty items and duplicates before any request', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  for (const candidateHarnesses of [
    null, 'generic-member', 1, {}, [null], [1], [{}], [''], [' '],
    ['generic member'], [' generic-member'], ['generic/member'], ['generic\nmember'],
    ['generic\u0000member'], ['generic-member', 'generic-member'], new Array(1),
  ]) {
    assert.throws(() => new LegionClient({ ...config, candidateHarnesses }, env), /candidateHarnesses/)
  }
  assert.equal(server.requests.length, 0)
})

test('omitted or empty candidates preserve legacy reservation fingerprints and send no plan', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const args = { key: 'legacy', query: 'Original task' }
  // The pre-option client used SHA-256(JSON.stringify(null)) for an absent project binding.
  const { ref } = await client.store.reserve({
    sessionId: execution().sessionId, ...args,
    bindingDigest: createHash('sha256').update('null').digest('hex'),
  })
  const before = await readFile(join(client.store.path(ref.id), 'ref.json'), 'utf8')
  for (const candidateHarnesses of [undefined, []]) {
    const restored = new LegionClient({ ...config, candidateHarnesses }, env)
    const result = await restored.submit(args, execution())
    assert.equal(result.task_ref, ref.id)
    assert.equal(result.reused, true)
  }
  assert.equal(server.requests.length, 0)
  assert.equal(await readFile(join(client.store.path(ref.id), 'ref.json'), 'utf8'), before)
  const empty = new LegionClient({ ...config, candidateHarnesses: [] }, env)
  await empty.submit({ key: 'empty-candidates', query: 'Another task' }, execution())
  assert.ok(!Object.hasOwn(server.requests[0].body, 'plan'))
})

test('same configured candidates recover a lost acknowledgement without redispatch', async t => {
  const { server, config, env, LegionClient } = await setup(t, { dropDispatch: true })
  const selected = { ...config, candidateHarnesses: ['generic-member'] }
  const args = { key: 'selected-lost-ack', query: 'Original task' }
  const first = await new LegionClient(selected, env).submit(args, execution())
  const restored = await new LegionClient({ ...selected, candidateHarnesses: ['generic-member'] }, env)
    .submit(args, execution())
  assert.equal(first.submission, 'unconfirmed')
  assert.equal(restored.reused, true)
  assert.equal(restored.task_ref, first.task_ref)
  assert.equal(server.requests.length, 1)
  assert.deepEqual(server.requests[0].body.plan, { team_policy: { candidate_harnesses: ['generic-member'] } })
})

test('same task key rejects adding, removing, replacing or reordering configured candidates', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const changes = [
    [undefined, ['generic-member']], [[], ['generic-member']],
    [['generic-member'], undefined], [['generic-member'], []],
    [['generic-member'], ['generic-reviewer']],
    [['generic-member'], ['generic-member', 'generic-reviewer']],
    [['generic-member', 'generic-reviewer'], ['generic-reviewer', 'generic-member']],
  ]
  for (const [i, [before, after]] of changes.entries()) {
    await t.test(`candidate change ${i + 1}`, async () => {
      const original = new LegionClient({ ...config, candidateHarnesses: before }, env)
      const args = { key: `candidate-change-${i}`, query: 'Same task', context: 'Same context' }
      const first = await original.submit(args, execution())
      const path = join(original.store.path(first.task_ref), 'ref.json')
      const saved = await readFile(path, 'utf8')
      const count = server.requests.length
      const changed = new LegionClient({ ...config, candidateHarnesses: after }, env)
      await assert.rejects(changed.submit(args, execution()), /different input/i)
      assert.equal(server.requests.length, count, 'Reject before any network request')
      assert.equal(await readFile(path, 'utf8'), saved, 'Do not rewrite the old reference')
    })
  }
})

test('a new key may apply configured candidates to a follow-up without changing the original task', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const first = await client.submit({ key: 'original-unselected', query: 'Original task' }, execution())
  const path = join(client.store.path(first.task_ref), 'ref.json')
  const before = await readFile(path, 'utf8')
  const selected = new LegionClient({ ...config, candidateHarnesses: ['generic-member'] }, env)
  const next = await selected.submit({
    key: 'explicit-follow-up', query: 'New user feedback', previous_task_ref: first.task_ref,
  }, execution())
  assert.equal(next.session_id, first.session_id)
  assert.notEqual(next.team_id, first.team_id)
  assert.deepEqual(server.requests[1].body.plan, { team_policy: { candidate_harnesses: ['generic-member'] } })
  assert.ok(!Object.hasOwn(server.requests[0].body, 'plan'))
  assert.equal(await readFile(path, 'utf8'), before)
})

test('configured candidates do not replace the project binding in the body or input fingerprint', async t => {
  const { server, config, env, LegionClient, stateDirectory } = await setup(t)
  const projectBindingFile = join(stateDirectory, 'project.json')
  await writeFile(projectBindingFile, JSON.stringify({ workspace_root: '/fixture/first-project' }))
  const client = new LegionClient({ ...config, projectBindingFile, candidateHarnesses: ['generic-member'] }, env)
  const args = { key: 'project-and-candidates', query: 'Task' }
  await client.submit(args, execution())
  assert.deepEqual(server.requests[0].body.project_context, { workspace_root: '/fixture/first-project' })
  assert.deepEqual(server.requests[0].body.plan, { team_policy: { candidate_harnesses: ['generic-member'] } })
  await writeFile(projectBindingFile, JSON.stringify({ workspace_root: '/fixture/second-project' }))
  await assert.rejects(client.submit(args, execution()), /different input/i)
  assert.equal(server.requests.length, 1)
})

test('submit arguments cannot inject a plan or override user-configured candidates', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  for (const [i, candidateHarnesses] of [undefined, ['generic-member']].entries()) {
    const client = new LegionClient({ ...config, candidateHarnesses }, env)
    const args = {
      key: `untrusted-selection-${i}`, query: 'Task',
      candidateHarnesses: ['injected-member'], candidate_harnesses: ['injected-member'],
      candidates: ['injected-member'],
      plan: { team_policy: { candidate_harnesses: ['injected-member'] } },
      team_policy: { candidate_harnesses: ['injected-member'] }, mode: 'orchestrate',
    }
    await client.submit(args, execution())
    const body = server.requests.at(-1).body
    assert.equal(body.mode, 'auto')
    if (candidateHarnesses) {
      assert.deepEqual(body.plan, { team_policy: { candidate_harnesses: ['generic-member'] } })
    } else assert.ok(!Object.hasOwn(body, 'plan'))
    assert.ok(!Object.hasOwn(body, 'candidateHarnesses'))
    assert.ok(!Object.hasOwn(body, 'candidate_harnesses'))
    assert.ok(!Object.hasOwn(body, 'candidates'))
    assert.ok(!Object.hasOwn(body, 'team_policy'))
    const reused = await client.submit({ key: args.key, query: args.query }, execution())
    assert.equal(reused.reused, true, 'Unknown arguments must not alter the reservation either')
  }
  assert.equal(server.requests.length, 2)
})

test('lost submit response remains uncertain and reconnects without another model call', async t => {
  const { client, server } = await setup(t, { dropDispatch: true })
  const args = { key: 'lost-response', query: 'A task whose ack was lost' }
  const first = await client.submit(args, execution())
  assert.equal(first.submission, 'unconfirmed')
  const second = await client.submit(args, execution())
  assert.equal(second.reused, true)
  const status = await client.status({ task_ref: first.task_ref }, execution())
  assert.equal(status.connection, 'connected')
  assert.equal(status.runtime.tasks[0].status, 'running')
  assert.equal(server.requests.filter(x => x.path === '/api/legion/dispatch').length, 1)
})

test('status distinguishes done from artifacts and retains ordered replay ids', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'plain', query: 'Reply in text' }, execution())
  server.finish(start.team_id)
  const state = await client.status({ task_ref: start.task_ref, after_event_id: 0 }, execution())
  assert.equal(state.runtime.tasks[0].status, 'done')
  assert.equal(state.artifacts.length, 0)
  assert.equal(state.experience_verification, 'not_performed_by_plugin')
  assert.deepEqual(state.events.map(x => x.event_id), [1, 2])
  assert.equal(state.next_event_id, 2)
  const replay = await client.status({ task_ref: start.task_ref, after_event_id: 1 }, execution())
  assert.deepEqual(replay.events.map(x => x.event_id), [2])
})

test('saved task references survive a new client instance and refuse another DSH session', async t => {
  const { client, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'restore', query: 'Persist me' }, execution())
  const restored = new LegionClient(config, env)
  assert.equal((await restored.list(execution())).tasks[0].task_ref, start.task_ref)
  await assert.rejects(restored.status({ task_ref: start.task_ref }, { ...execution(), sessionId: 'other' }), /session/i)
})

test('message and resume are separate, with duplicate operation keys suppressed', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'feedback', query: 'Original work' }, execution())
  const args = { task_ref: start.task_ref, operation_key: 'feedback-1', recipient: 'author', text: 'Please recheck the actual error.' }
  await client.message(args, execution())
  await client.message(args, execution())
  assert.equal(server.messageCount, 1)
  assert.equal(server.resumeCount, 0)
  const resume = await client.resume({ task_ref: start.task_ref, operation_key: 'resume-1' }, execution())
  assert.equal(resume.server_response.status, 'unchanged')
  await client.resume({ task_ref: start.task_ref, operation_key: 'resume-1' }, execution())
  assert.equal(server.resumeCount, 1)
})

test('cancellation acknowledgement is not presented as proof that execution stopped', async t => {
  const { client } = await setup(t)
  const start = await client.submit({ key: 'cancel', query: 'Work' }, execution())
  const result = await client.cancel({ task_ref: start.task_ref, reason: 'User requested stop' }, execution())
  assert.equal(result.cancellation, 'requested')
  assert.equal(result.stopped_confirmed, false)
  const state = await client.status({ task_ref: start.task_ref }, execution())
  assert.equal(state.runtime.tasks[0].status, 'running')
})

test('artifact download is limited to a server-listed URL and checks exact SHA-256 bytes', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'file', query: 'Make a file' }, execution())
  server.finish(start.team_id, [{ name: 'result.txt', url: '/outputs/result.txt', sha256: server.sha256, bytes: server.bytes.length, team_id: start.team_id }])
  const state = await client.status({ task_ref: start.task_ref }, execution())
  const result = await client.download({ task_ref: start.task_ref, artifact_id: state.artifacts[0].artifact_id }, execution())
  assert.equal(result.integrity, 'sha256_verified')
  assert.equal(result.experience_verification, 'not_performed_by_plugin')
  assert.deepEqual(await readFile(result.local_path), server.bytes)
})

test('HTML downloads request original bytes instead of modified previews, including behind a proxy prefix', async t => {
  for (const prefix of ['', '/legion']) {
    await t.test(prefix || 'service root', async t => {
      const { client, server } = await setup(t, {
        prefix, artifactPath: '/home-outputs/result.html', previewArtifact: true,
        artifactBytes: '<!doctype html><html lang="zh"><title>原文件</title><body>真实交付</body></html>',
      })
      const start = await client.submit({ key: 'html', query: 'Make an HTML file' }, execution())
      server.finish(start.team_id, [{
        name: 'result.html', url: '/home-outputs/result.html',
        sha256: server.sha256, bytes: server.bytes.length, team_id: start.team_id,
      }])
      const state = await client.status({ task_ref: start.task_ref }, execution())
      const result = await client.download({
        task_ref: start.task_ref, artifact_id: state.artifacts[0].artifact_id,
      }, execution())
      assert.equal(result.integrity, 'sha256_verified')
      assert.deepEqual(await readFile(result.local_path), server.bytes)
      const request = server.requests.find(row => row.path === `${prefix}/home-outputs/result.html`)
      assert.equal(request.search, '?download=1')
      assert.equal(request.headers.authorization, 'Bearer fixture-token')
      const ref = await client.store.get(execution().sessionId, start.task_ref)
      const [stored] = await client.store.readObservation(ref, 'artifacts')
      assert.equal(new URL(stored.url).search, '', 'persist only the authorized export URL')
    })
  }
})

test('original download mode does not authorize query parameters supplied in artifact URLs', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'query', query: 'Make a file' }, execution())
  server.finish(start.team_id, [{
    name: 'result.txt', url: '/outputs/result.txt?download=1',
    sha256: server.sha256, bytes: server.bytes.length,
  }])
  const state = await client.status({ task_ref: start.task_ref }, execution())
  assert.equal(state.artifacts[0].download_eligible, false)
  await assert.rejects(client.download({
    task_ref: start.task_ref, artifact_id: state.artifacts[0].artifact_id,
  }, execution()), /authorized/)
  assert.equal(server.requests.filter(row => row.path === '/outputs/result.txt').length, 0)
})

test('hash mismatch never exposes downloaded bytes as a valid result', async t => {
  const { client, server } = await setup(t, { badArtifact: true })
  const start = await client.submit({ key: 'badfile', query: 'File' }, execution())
  server.finish(start.team_id, [{ name: 'result.txt', url: '/outputs/result.txt', sha256: server.sha256, bytes: server.bytes.length }])
  const state = await client.status({ task_ref: start.task_ref }, execution())
  await assert.rejects(client.download({ task_ref: start.task_ref, artifact_id: state.artifacts[0].artifact_id }, execution()), /hash|sha256|integrity/i)
})

test('HTTP auth errors remain errors and redact the configured secret', async t => {
  const { client, env } = await setup(t)
  env.FIXTURE_TOKEN = 'incorrect'
  const result = await client.submit({ key: 'auth', query: 'Task' }, execution()).catch(error => error)
  assert.ok(result instanceof Error)
  assert.match(result.message, /401/)
  assert.ok(!result.message.includes('incorrect'))
})

test('unsafe server schemes and remote plaintext credentials are rejected before network', async () => {
  const { LegionClient } = await import('../lib/client.js')
  for (const baseUrl of ['file:///tmp/service', 'http://remote.example', 'https://name:secret@example.com', 'https://example.com?token=secret']) {
    assert.throws(() => new LegionClient({ baseUrl }), /https|URL|credential|query|scheme/i)
  }
})

test('an already-aborted call never sends a task and redirects do not forward credentials', async t => {
  const { client, server } = await setup(t, { redirect: 'http://127.0.0.1:9/stolen' })
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(client.submit({ key: 'abort', query: 'Task' }, { sessionId: 'A', signal: controller.signal }), { name: 'AbortError' })
  assert.equal(server.requests.length, 0)
  const result = await client.submit({ key: 'redirect', query: 'Task' }, execution())
  assert.equal(result.submission, 'unconfirmed')
  assert.equal(server.requests.length, 1)
})

test('follow-up submits a new turn in the original conversation, not an empty fresh project', async t => {
  const { client, server } = await setup(t)
  const first = await client.submit({ key: 'first', query: 'Original work' }, execution())
  server.finish(first.team_id)
  const next = await client.submit({ key: 'revision', query: 'Revise the original work', previous_task_ref: first.task_ref }, execution())
  const requests = server.requests.filter(x => x.path === '/api/legion/dispatch')
  assert.equal(requests[0].body.session_id, requests[1].body.session_id)
  assert.notEqual(requests[0].body.team_id, requests[1].body.team_id)
  assert.ok(next.team_id.startsWith(`web_${requests[0].body.session_id}_`))
})

test('a page limit never advances the cursor past undisplayed events', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'many-events', query: 'Work' }, execution())
  const events = server.teams.get(start.team_id).events
  for (let i = 2; i <= 5; i++) events.push({ event_id: i, event: 'step', data: { text: `event ${i}` } })
  const limited = new LegionClient({ ...config, maxEvents: 2 }, env)
  const first = await limited.status({ task_ref: start.task_ref }, execution())
  assert.deepEqual(first.events.map(x => x.event_id), [1, 2])
  assert.equal(first.next_event_id, 2)
  assert.equal(first.more_events, true)
  const next = await limited.status({ task_ref: start.task_ref, after_event_id: first.next_event_id }, execution())
  assert.deepEqual(next.events.map(x => x.event_id), [3, 4])
})

test('private credential fields and raw reasoning are not forwarded or stored', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'private', query: 'Work' }, execution())
  server.teams.get(start.team_id).events.push({
    event_id: 2, event: 'error', data: {
      reason: 'Request rejected; fixture-token',
      stage: 'research',
      authorization: 'Bearer fixture-token',
      reasoning_content: 'PRIVATE_REASONING',
      share_token: 'PRIVATE_CAPABILITY',
    },
  })
  const result = await client.status({ task_ref: start.task_ref }, execution())
  const text = JSON.stringify(result)
  assert.ok(!text.includes('fixture-token'))
  assert.ok(!text.includes('PRIVATE_REASONING'))
  assert.ok(!text.includes('PRIVATE_CAPABILITY'))
  assert.equal(result.events[1].data.stage, 'research')
  assert.ok(result.projection.omitted_fields.length >= 3)
})

test('an artifact without a hash or with a cross-origin URL cannot be downloaded', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'unsafe-artifact', query: 'Work' }, execution())
  server.finish(start.team_id, [
    { name: 'nohash.txt', url: '/outputs/result.txt' },
    { name: 'elsewhere.txt', url: 'https://elsewhere.example/file', sha256: server.sha256 },
  ])
  const state = await client.status({ task_ref: start.task_ref }, execution())
  assert.equal(state.artifacts.length, 2, 'An unsafe export is reported, not silently hidden')
  assert.ok(state.artifacts.every(x => !x.download_eligible))
  for (const artifact of state.artifacts) {
    await assert.rejects(client.download({ task_ref: start.task_ref, artifact_id: artifact.artifact_id }, execution()), /hash|origin|download|URL/i)
  }
})

test('disposing the plugin closes its own wait, not the remote delegated task', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'dispose', query: 'Work' }, execution())
  client.dispose()
  await assert.rejects(client.status({ task_ref: start.task_ref }, execution()), /disposed|abort/i)
  assert.equal(server.requests.filter(x => x.path === '/api/legion/cancel').length, 0)
})

test('response limits report a failure rather than silently discarding evidence', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'huge', query: 'Work' }, execution())
  server.teams.get(start.team_id).tasks.author.result = { final: 'a'.repeat(10000) }
  const limited = new LegionClient({ ...config, maxResponseBytes: 1024 }, env)
  await assert.rejects(limited.status({ task_ref: start.task_ref }, execution()), /byte limit|byte-limit|-byte limit/i)
})

test('missing credentials do not burn a key or perform any network request', async t => {
  const { client, server, env, stateDirectory } = await setup(t)
  delete env.FIXTURE_TOKEN
  const args = { key: 'setup-first', query: 'Work' }
  await assert.rejects(client.submit(args, execution()), /host environment/i)
  await assert.rejects(client.list(execution()), /host environment/i)
  assert.deepEqual(await readdir(stateDirectory), [], 'No recovery state may be written without an identity')
  assert.equal(server.requests.length, 0)
  env.FIXTURE_TOKEN = 'fixture-token'
  assert.equal((await client.submit(args, execution())).submission, 'accepted')
})

// Match team_state.send_message's real envelope, not a stub of client.status.
const commanderMessage = (id, overrides = {}) => ({
  id, from: 'author', to: 'commander', text: 'Please call marked_extract_tokens with {"markdown":"# Hi"}.',
  artifact_refs: [], ts: 1793110000 + id, read_at: null, ...overrides,
})

test('commander mailbox: status restores requests and original read facts without marking or consuming them', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'mailbox', query: 'Use local capabilities when requested' }, execution())
  const team = server.teams.get(start.team_id)
  const withoutReadFact = commanderMessage(9)
  delete withoutReadFact.read_at
  team.mailboxes = {
    commander: [commanderMessage(2), commanderMessage(5, { read_at: 1793110010.5 }), withoutReadFact],
    author: [commanderMessage(6, { to: 'author', text: 'OTHER_RECIPIENT_PRIVATE_BODY' })],
  }
  const before = structuredClone(team.mailboxes)
  const restarted = new LegionClient(config, env)
  for (const reader of [client, restarted]) {
    const result = await reader.status({ task_ref: start.task_ref, after_event_id: 100 }, execution())
    assert.deepEqual(result.mailbox?.messages?.map(({ id, from, to, text, ts, read_at }) =>
      ({ id, from, to, text, ts, ...(read_at === undefined ? {} : { read_at }) })), [
      { id: 2, from: 'author', to: 'commander', text: before.commander[0].text, ts: 1793110002, read_at: null },
      { id: 5, from: 'author', to: 'commander', text: before.commander[1].text, ts: 1793110005, read_at: 1793110010.5 },
      { id: 9, from: 'author', to: 'commander', text: before.commander[2].text, ts: 1793110009 },
    ], 'Legitimate commander requests must not disappear from status')
    assert.equal(result.mailbox.state, 'available')
    assert.equal(result.mailbox.source, 'team.mailboxes.commander')
    assert.equal(result.mailbox.trust, 'untrusted_source')
    assert.equal(result.mailbox.read_receipts_modified, false)
    assert.equal(result.mailbox.model_consumption, 'not_observed')
    assert.equal(Object.hasOwn(result.mailbox.messages[2], 'read_at'), false)
    assert.equal(result.runtime.tasks[0].task_id, 'author')
    assert.equal(result.events.length, 0, 'Mailbox IDs are independent of the replay cursor')
    assert.equal(JSON.stringify(result).includes('OTHER_RECIPIENT_PRIVATE_BODY'), false)
  }
  assert.deepEqual(team.mailboxes, before)
  assert.deepEqual(server.requests.slice(1).map(({ method, path }) => `${method} ${path}`), [
    'GET /api/legion/team', 'GET /api/legion/events',
    'GET /api/legion/team', 'GET /api/legion/events',
  ], 'Observation must not call mark-read, message, resume, or dispatch endpoints')
})

test('commander mailbox: envelope and reference projection preserves text redaction and truncation facts', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'mailbox-redaction', query: 'Work' }, execution())
  server.teams.get(start.team_id).mailboxes = { commander: [commanderMessage(1, {
    text: `Bearer fixture-token https://public.example/file?token=PRIVATE_URL_TOKEN ${'x'.repeat(17000)}`,
    authorization: 'PRIVATE_AUTHORIZATION',
    reasoning_content: 'PRIVATE_REASONING',
    attachments: [{ data: 'PRIVATE_BINARY' }],
    artifact_refs: [{
      artifact_id: 'a'.repeat(64), sha256: 'b'.repeat(64), name: 'result.txt', mime: 'text/plain', bytes: 42,
      path: '/private/service/artifact.txt', data: 'PRIVATE_REFERENCE_BINARY', token: 'PRIVATE_REFERENCE_TOKEN',
    }],
  })] }
  const result = await client.status({ task_ref: start.task_ref }, execution())
  assert.ok(result.mailbox?.messages, 'Status must expose the sanitized mailbox')
  const message = result.mailbox.messages[0]
  assert.match(message.text, /Bearer \[redacted\]/)
  assert.match(message.text, /\?token=\[redacted\]/)
  assert.match(message.text, /\[truncated; original \d+ characters\]/)
  assert.equal(message.text_source, 'inline')
  assert.equal(message.text_truncated, true)
  assert.equal(result.mailbox.truncated, true)
  assert.deepEqual(message.artifact_refs, [{
    artifact_id: 'a'.repeat(64), sha256: 'b'.repeat(64), name: 'result.txt', mime: 'text/plain', bytes: 42,
  }])
  assert.ok(result.projection.truncated_fields.includes('$.mailbox.messages[0].text'))
  for (const path of ['authorization', 'reasoning_content', 'attachments', 'artifact_refs[0].path', 'artifact_refs[0].data', 'artifact_refs[0].token']) {
    assert.ok(result.projection.omitted_fields.includes(`$.mailbox.messages[0].${path}`), path)
  }
  for (const secret of ['fixture-token', 'PRIVATE_URL_TOKEN', 'PRIVATE_AUTHORIZATION', 'PRIVATE_REASONING',
    'PRIVATE_BINARY', '/private/service/artifact.txt', 'PRIVATE_REFERENCE_BINARY', 'PRIVATE_REFERENCE_TOKEN']) {
    assert.equal(JSON.stringify(result).includes(secret), false)
  }
})

test('commander mailbox: referenced long text remains an explicit preview and private references are not fetched', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'mailbox-preview', query: 'Work' }, execution())
  server.teams.get(start.team_id).mailboxes = { commander: [commanderMessage(7, {
    text: 'The service only returned the beginning of a longer capability request.',
    artifact_refs: [
      { name: 'a.txt', path: '/private/a.txt' },
      { name: 'b.txt', path: '/private/b.txt' },
      { name: 'mail-7.txt', sha256: 'c'.repeat(64), bytes: 9000, path: '/private/body.txt' },
    ],
  })] }
  const limited = new LegionClient({ ...config, maxEvents: 2 }, env)
  const result = await limited.status({ task_ref: start.task_ref }, execution())
  assert.ok(result.mailbox?.messages)
  const message = result.mailbox.messages[0]
  assert.equal(message.text_source, 'inline_preview')
  assert.equal(message.text_truncated, true)
  assert.equal(message.artifact_refs.length, 2)
  assert.equal(message.artifact_refs_truncated, true)
  assert.equal(result.mailbox.truncated, true)
  assert.equal(result.mailbox.more_messages, false, 'A truncated body is not another mailbox page')
  assert.ok(result.projection.truncated_fields.includes('$.mailbox.messages[0].text'))
  assert.ok(result.projection.truncated_fields.includes('$.mailbox.messages[0].artifact_refs'))
  assert.deepEqual(server.requests.slice(1).map(x => x.path), ['/api/legion/team', '/api/legion/events'])
  assert.equal(JSON.stringify(result).includes('/private/'), false)
})

test('commander mailbox: bounded pages use an independent explicit cursor and never implicitly consume earlier requests', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'mailbox-pages', query: 'Work' }, execution())
  server.teams.get(start.team_id).mailboxes = { commander: [commanderMessage(8), commanderMessage(2), commanderMessage(5)] }
  const limited = new LegionClient({ ...config, maxEvents: 2 }, env)
  const first = await limited.status({ task_ref: start.task_ref, after_event_id: 100 }, execution())
  assert.deepEqual(first.mailbox?.messages?.map(x => x.id), [2, 5])
  assert.equal(first.mailbox.after_message_id, 0)
  assert.equal(first.mailbox.next_message_id, 5)
  assert.equal(first.mailbox.total_messages, 3)
  assert.equal(first.mailbox.more_messages, true)
  assert.equal(first.mailbox.truncated, true)
  assert.ok(first.projection.truncated_fields.includes('$.mailbox.messages'))
  const next = await limited.status({ task_ref: start.task_ref, after_message_id: 5 }, execution())
  assert.deepEqual(next.mailbox.messages.map(x => x.id), [8])
  assert.equal(next.mailbox.next_message_id, 8)
  assert.equal(next.mailbox.more_messages, false)
  assert.equal(next.mailbox.truncated, false)
  assert.deepEqual(next.events.map(x => x.event_id), [1], 'The message cursor must not filter events')
  const again = await limited.status({ task_ref: start.task_ref }, execution())
  assert.deepEqual(again.mailbox.messages.map(x => x.id), [2, 5], 'Display is not an acknowledgement')
  const end = await limited.status({ task_ref: start.task_ref, after_message_id: 20 }, execution())
  assert.deepEqual(end.mailbox.messages, [])
  assert.equal(end.mailbox.next_message_id, 20)
  assert.equal(end.mailbox.more_messages, false)
})

test('commander mailbox: metadata truncation is reflected in the mailbox truncation flag', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'mailbox-metadata', query: 'Work' }, execution())
  server.teams.get(start.team_id).mailboxes = { commander: [commanderMessage(1, {
    from: 'a'.repeat(17000), artifact_refs: [{ name: 'b'.repeat(17000) }],
  })] }
  const result = await client.status({ task_ref: start.task_ref }, execution())
  assert.equal(result.mailbox.truncated, true, 'Metadata clipping must not claim an untruncated mailbox')
  assert.equal(result.mailbox.messages[0].text_truncated, false)
  assert.ok(result.projection.truncated_fields.includes('$.mailbox.messages[0].from'))
  assert.ok(result.projection.truncated_fields.includes('$.mailbox.messages[0].artifact_refs[0].name'))
})

test('commander mailbox: missing or malformed containers are distinct from an observed empty mailbox', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'mailbox-shapes', query: 'Work' }, execution())
  const team = server.teams.get(start.team_id)
  for (const [name, mailboxes, state] of [
    ['missing mailboxes', undefined, 'not_returned'],
    ['missing commander', {}, 'not_returned'],
    ['null mailboxes', null, 'invalid'],
    ['array mailboxes', [], 'invalid'],
    ['string mailboxes', 'PRIVATE_MALFORMED_BODY', 'invalid'],
    ['null commander', { commander: null }, 'invalid'],
    ['object commander', { commander: {} }, 'invalid'],
  ]) {
    await t.test(name, async () => {
      team.mailboxes = mailboxes
      const result = await client.status({ task_ref: start.task_ref }, execution())
      assert.equal(result.mailbox?.state, state)
      assert.equal(result.mailbox.messages, null)
      assert.equal(result.mailbox.total_messages, null)
      assert.equal(result.mailbox.more_messages, null)
      assert.equal(result.mailbox.next_message_id, null)
      assert.ok(result.mailbox.diagnostic.code)
      assert.equal(result.runtime.tasks[0].status, 'running')
      assert.equal(JSON.stringify(result).includes('PRIVATE_MALFORMED_BODY'), false)
    })
  }
  team.mailboxes = { commander: [] }
  const empty = await client.status({ task_ref: start.task_ref }, execution())
  assert.equal(empty.mailbox.state, 'available')
  assert.deepEqual(empty.mailbox.messages, [])
  assert.equal(empty.mailbox.total_messages, 0)
  assert.equal(empty.mailbox.more_messages, false)
  assert.equal(empty.mailbox.next_message_id, 0)
  assert.equal(empty.mailbox.truncated, false)
})

test('commander mailbox: malformed or ambiguous envelopes are reported rather than treated as no requests', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'mailbox-envelopes', query: 'Work' }, execution())
  const cases = [
    ['null envelope', [null]],
    ['invalid id', [commanderMessage('1')]],
    ['missing sender', [commanderMessage(1, { from: undefined })]],
    ['wrong recipient', [commanderMessage(1, { to: 'other-task' })]],
    ['binary body', [commanderMessage(1, { text: { type: 'Buffer', data: [80, 82, 73, 86, 65, 84, 69] } })]],
    ['invalid timestamp', [commanderMessage(1, { ts: 'PRIVATE_TIMESTAMP' })]],
    ['invalid read fact', [commanderMessage(1, { read_at: false })]],
    ['invalid references', [commanderMessage(1, { artifact_refs: {} })]],
    ['invalid reference entry', [commanderMessage(1, { artifact_refs: [null] })]],
    ['invalid reference metadata', [commanderMessage(1, { artifact_refs: [{ name: { private: 'PRIVATE_METADATA' } }] })]],
    ['duplicate message id', [commanderMessage(1), commanderMessage(1, { from: 'reviewer' })]],
  ]
  for (const [name, messages] of cases) {
    await t.test(name, async () => {
      server.teams.get(start.team_id).mailboxes = { commander: messages }
      const result = await client.status({ task_ref: start.task_ref }, execution())
      assert.equal(result.mailbox?.state, 'invalid')
      assert.equal(result.mailbox.messages, null)
      assert.equal(result.mailbox.total_messages, messages.length)
      assert.equal(result.mailbox.more_messages, null)
      assert.equal(result.mailbox.next_message_id, null)
      assert.equal(result.mailbox.diagnostic.code, 'INVALID_MAILBOX')
      assert.match(result.mailbox.diagnostic.path, /^\$\.mailbox\.messages\[/)
      assert.equal(JSON.stringify(result).includes('PRIVATE_TIMESTAMP'), false)
      assert.equal(JSON.stringify(result).includes('PRIVATE_METADATA'), false)
      assert.equal(result.runtime.tasks[0].status, 'running')
    })
  }
})

test('commander mailbox: invalid message cursors fail before requesting remote state', async t => {
  const { client, server } = await setup(t)
  const start = await client.submit({ key: 'mailbox-cursor', query: 'Work' }, execution())
  for (const after_message_id of [-1, 0.5, '1', Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(client.status({ task_ref: start.task_ref, after_message_id }, execution()), /after_message_id/)
  }
  assert.equal(server.requests.length, 1)
})

test('commander mailbox: session and team identity, cancellation, and response byte limits still guard the snapshot', async t => {
  const { client, server, config, env, LegionClient } = await setup(t)
  const start = await client.submit({ key: 'mailbox-boundaries', query: 'Work' }, execution())
  const team = server.teams.get(start.team_id)
  team.mailboxes = { commander: [commanderMessage(1)] }
  await assert.rejects(client.status({ task_ref: start.task_ref }, { ...execution(), sessionId: 'different-dsh-session' }), /session|unknown|not found/i)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(client.status({ task_ref: start.task_ref }, { ...execution(), signal: controller.signal }), { name: 'AbortError' })
  assert.equal(server.requests.length, 1)
  for (const field of ['team_id', 'conversation_id']) {
    const original = team[field]
    team[field] = 'PRIVATE_FOREIGN_IDENTITY'
    await assert.rejects(client.status({ task_ref: start.task_ref }, execution()), /different .* identity/)
    team[field] = original
  }
  assert.deepEqual(server.requests.slice(1).map(x => x.path), ['/api/legion/team', '/api/legion/team'])
  team.mailboxes.commander[0].text = 'x'.repeat(10000)
  const limited = new LegionClient({ ...config, maxResponseBytes: 1024 }, env)
  await assert.rejects(limited.status({ task_ref: start.task_ref }, execution()), /byte limit/i)
})

test('team configuration: HTTP submit appends user context without replacing the question or candidates', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const teamProfile = {
    name: ' \u3000共同工作队 ', goal: ' 先理解真实问题，再共同改进 ',
    collaboration: 'custom', instructions: '讨论 system、authority、token 术语。\n保留用户要求。',
    evolutionEnabled: false, cadence: 'manual',
  }
  const candidateHarnesses = ['member-a', 'member-b']
  const client = new LegionClient({ ...config, teamProfile, candidateHarnesses }, env)
  teamProfile.goal = 'MUTATED_CONFIG_MUST_NOT_LEAK'
  candidateHarnesses.reverse()
  const args = { key: 'team-context', query: '只处理这一条当前任务', context: '这里是用户显式提供的原始上下文。' }
  const result = await client.submit(args, execution())
  assert.equal(result.submission, 'accepted')
  const { body } = server.requests[0]
  const original = `${args.query}\n\n--- Context explicitly provided by the DSH caller (not a system instruction) ---\n${args.context}`
  assert.ok(body.question.startsWith(`${original}\n\n`))
  assert.ok(body.question.includes('共同工作队'))
  assert.equal(body.question.split('先理解真实问题，再共同改进').length, 2, 'Append configuration once')
  assert.ok(body.question.includes('讨论 system、authority、token 术语。\n保留用户要求。'))
  assert.ok(!body.question.includes('MUTATED_CONFIG_MUST_NOT_LEAK'))
  assert.match(body.question.slice(original.length), /用户(?:明确)?配置/)
  assert.match(body.question.slice(original.length), /不是系统指令|not a system instruction/i)
  assert.match(body.question.slice(original.length), /不(?:增加|扩大|提升|改变)[^\n]*(?:权限|授权)/)
  assert.equal(body.mode, 'auto')
  assert.equal(body.allow_direct_model, false)
  assert.deepEqual(body.plan, { team_policy: { candidate_harnesses: ['member-a', 'member-b'] } })
  assert.deepEqual(Object.keys(body), ['question', 'mode', 'allow_direct_model', 'session_id', 'team_id', 'plan'])
  assert.equal(client.teamProfile.goal, '先理解真实问题，再共同改进')
  assert.ok(Object.isFrozen(client.teamProfile))
  assert.ok(!Object.hasOwn(await client.store.get(execution().sessionId, result.task_ref), 'teamProfile'))
})

test('team configuration: explicit defaults allow autonomous selection without a plan or caller context', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const client = new LegionClient({ ...config, teamProfile: {}, candidateHarnesses: [] }, env)
  await client.submit({ key: 'team-default', query: '当前工作' }, execution())
  const { body } = server.requests[0]
  assert.ok(body.question.startsWith('当前工作\n\n'))
  assert.ok(body.question.includes('我的战队'))
  assert.ok(body.question.includes('让我的 DSH 和战队在真实工作中持续变强'))
  assert.match(body.question, /自主/)
  assert.match(body.question, /开启/)
  assert.match(body.question, /每周/)
  assert.equal(body.mode, 'auto')
  assert.ok(!Object.hasOwn(body, 'plan'))
  assert.ok(!Object.hasOwn(body, 'teamProfile'))
})

test('team configuration: unconfigured clients preserve exact legacy HTTP bytes and persisted digests', async t => {
  const { LegionClient } = await import('../lib/client.js')
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-team-legacy-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const received = []
  const server = createServer(async (req, res) => {
    let raw = ''
    for await (const chunk of req) raw += chunk
    received.push({ raw, path: req.url, authorization: req.headers.authorization })
    const body = JSON.parse(raw)
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.end(`event: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  const config = { baseUrl: `http://127.0.0.1:${server.address().port}`, stateDirectory, tokenEnv: 'FIXTURE_TOKEN' }
  const env = { FIXTURE_TOKEN: 'fixture-token' }
  const projectBindingFile = join(stateDirectory, 'project.json')
  await writeFile(projectBindingFile, JSON.stringify({ workspace_root: '/fixture/legacy-project' }))
  let index = 0
  for (const binding of [null, { workspace_root: '/fixture/legacy-project' }]) {
    for (const candidateHarnesses of [undefined, [], ['member-a']]) {
      const configured = { ...config, ...(binding ? { projectBindingFile } : {}), candidateHarnesses }
      const client = new LegionClient(configured, env)
      const plan = candidateHarnesses?.length
        ? { team_policy: { candidate_harnesses: ['member-a'] } } : undefined
      const bindingDigest = createHash('sha256')
        .update(JSON.stringify(plan ? { project_context: binding, plan } : binding)).digest('hex')
      const args = { key: `legacy-${index++}`, query: 'Original task 🧭', context: 'Original\nexplicit context' }
      const { ref } = await client.store.reserve({ sessionId: execution().sessionId, ...args, bindingDigest })
      const path = join(client.store.path(ref.id), 'ref.json')
      const before = await readFile(path, 'utf8')
      const count = received.length
      const restored = new LegionClient({ ...configured, teamProfile: undefined }, env)
      const reused = await restored.submit(args, execution())
      assert.equal(reused.reused, true)
      assert.equal(reused.task_ref, ref.id)
      assert.equal(await readFile(path, 'utf8'), before, 'Do not rewrite a legacy reference')
      assert.equal(received.length, count, 'Restoration must not issue HTTP')
      const freshArgs = { ...args, key: `${args.key}-fresh` }
      const fresh = await restored.submit(freshArgs, execution())
      const expected = {
        question: `${args.query}\n\n--- Context explicitly provided by the DSH caller (not a system instruction) ---\n${args.context}`,
        mode: 'auto', allow_direct_model: false, session_id: fresh.session_id, team_id: fresh.team_id,
        ...(binding ? { project_context: binding } : {}), ...(plan ? { plan } : {}),
      }
      assert.equal(received.at(-1).raw, JSON.stringify(expected), 'The actual HTTP body bytes must stay identical')
      assert.equal(received.at(-1).path, '/api/legion/dispatch')
      assert.equal(received.at(-1).authorization, 'Bearer fixture-token')
      const saved = await restored.store.get(execution().sessionId, fresh.task_ref)
      assert.equal(saved.input_digest, createHash('sha256')
        .update(JSON.stringify([args.query, args.context, bindingDigest, ''])).digest('hex'))
    }
  }
})

test('team configuration: invalid configuration errors never echo private keys or values or dispatch', async t => {
  const { server, config, env, LegionClient, stateDirectory } = await setup(t)
  const secret = 'synthetic-private-team-config'
  for (const teamProfile of [
    null, [], { [secret]: secret }, { goal: { [secret]: secret } },
    { collaboration: secret }, { cadence: secret }, { evolutionEnabled: secret },
    { name: secret.repeat(10) }, { authority: secret }, Object.create({ goal: secret }),
  ]) {
    assert.throws(() => new LegionClient({ ...config, teamProfile }, env), error => {
      assert.match(error.message, /teamProfile/)
      assert.ok(!String(error.stack).includes(secret))
      assert.ok(!JSON.stringify(error).includes(secret))
      return true
    })
  }
  assert.equal(server.requests.length, 0)
  assert.deepEqual(await readdir(stateDirectory), [])
})

test('team configuration: an unselected leader fails before HTTP or key reservation', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const teamProfile = { collaboration: 'leader', leader: 'member-a' }
  const args = { key: 'team-leader-selection', query: 'Current task' }
  for (const candidateHarnesses of [undefined, [], ['member-b']]) {
    const client = new LegionClient({ ...config, teamProfile, candidateHarnesses }, env)
    await assert.rejects(client.submit(args, execution()), error => {
      assert.match(error.message, /leader|带队/)
      assert.match(error.message, /candidate|候选/)
      assert.ok(!error.message.includes('member-a'))
      return true
    })
    assert.deepEqual((await client.list(execution())).tasks, [])
  }
  assert.equal(server.requests.length, 0)
  const selected = new LegionClient({ ...config, teamProfile, candidateHarnesses: ['member-b', 'member-a'] }, env)
  const result = await selected.submit(args, execution())
  assert.equal(result.submission, 'accepted', 'A rejected configuration must not consume the caller key')
  assert.ok(server.requests[0].body.question.includes('member-a'))
  assert.deepEqual(server.requests[0].body.plan, { team_policy: { candidate_harnesses: ['member-b', 'member-a'] } })
})

test('team configuration: profile changes cannot silently reuse an earlier task key', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const changes = [
    [undefined, {}], [{}, undefined],
    [{}, { name: '另一战队名称' }],
    [{}, { goal: '另一条长期目标' }],
    [{}, { collaboration: 'custom', instructions: '先共同理解需求' }],
    [{ collaboration: 'custom', instructions: '要求一' }, { collaboration: 'custom', instructions: '要求二' }],
    [{}, { leader: 'member-a' }], // Dormant configuration still participates even if context text does not change.
    [{}, { evolutionEnabled: false }], [{}, { cadence: 'daily' }],
    [{ collaboration: 'leader', leader: 'member-a' }, { collaboration: 'leader', leader: 'member-b' }],
  ]
  for (const [index, [before, after]] of changes.entries()) {
    await t.test(`profile change ${index + 1}`, async () => {
      const shared = { ...config, candidateHarnesses: ['member-a', 'member-b'] }
      const original = new LegionClient({ ...shared, teamProfile: before }, env)
      const args = { key: `team-profile-change-${index}`, query: 'Same current task', context: 'Same explicit context' }
      const first = await original.submit(args, execution())
      const path = join(original.store.path(first.task_ref), 'ref.json')
      const saved = await readFile(path, 'utf8')
      const count = server.requests.length
      const changed = new LegionClient({ ...shared, teamProfile: after }, env)
      await assert.rejects(changed.submit(args, execution()), /different input/i)
      assert.equal(server.requests.length, count)
      assert.equal(await readFile(path, 'utf8'), saved)
    })
  }
})

test('team configuration: equivalent normalized profiles recover lost acknowledgements without redispatch', async t => {
  const { server, config, env, LegionClient } = await setup(t, { dropDispatch: true })
  const args = { key: 'team-lost-ack', query: 'Original task' }
  const first = await new LegionClient({ ...config, teamProfile: {} }, env).submit(args, execution())
  const restored = new LegionClient({
    ...config,
    teamProfile: {
      cadence: 'weekly', evolutionEnabled: true, instructions: ' ', leader: '\n', collaboration: 'auto',
      goal: ' 让我的 DSH 和战队在真实工作中持续变强 ', name: ' 我的战队 ',
    },
  }, env)
  const second = await restored.submit(args, execution())
  assert.equal(first.submission, 'unconfirmed')
  assert.equal(second.reused, true)
  assert.equal(second.task_ref, first.task_ref)
  assert.equal(second.submission, 'unconfirmed')
  assert.equal(server.requests.length, 1)
  assert.ok(server.requests[0].body.question.includes('我的战队'))
})

test('team configuration: one in-flight profile snapshot supplies both the body and the reservation digest', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const teamProfile = { goal: 'Original user-configured goal' }
  const client = new LegionClient({ ...config, teamProfile }, env)
  const entered = Promise.withResolvers()
  const release = Promise.withResolvers()
  t.after(() => release.resolve())
  const projectBinding = client.projectBinding.bind(client)
  t.mock.method(client, 'projectBinding', async () => {
    entered.resolve()
    await release.promise
    return projectBinding()
  })
  const args = { key: 'team-snapshot', query: 'Current task' }
  const pending = client.submit(args, execution())
  await entered.promise
  client.teamProfile = { goal: 'Replacement configuration after submit began' }
  release.resolve()
  const first = await pending
  assert.ok(server.requests[0].body.question.includes('Original user-configured goal'))
  assert.ok(!server.requests[0].body.question.includes('Replacement configuration after submit began'))
  const original = new LegionClient({ ...config, teamProfile }, env)
  const restored = await original.submit(args, execution())
  assert.equal(restored.reused, true, 'The original snapshot must be the saved reservation input')
  assert.equal(restored.task_ref, first.task_ref)
  await assert.rejects(client.submit(args, execution()), /different input/i)
  assert.equal(server.requests.length, 1)
})

test('team configuration: follow-ups retain project and task identity, message deduplication and cancellation', async t => {
  const { client, server, config, env, LegionClient, stateDirectory } = await setup(t)
  const first = await client.submit({ key: 'before-team', query: 'Original work' }, execution())
  const oldPath = join(client.store.path(first.task_ref), 'ref.json')
  const oldBytes = await readFile(oldPath, 'utf8')
  const projectBindingFile = join(stateDirectory, 'project.json')
  await writeFile(projectBindingFile, JSON.stringify({ workspace_root: '/fixture/team-project' }))
  const configured = new LegionClient({
    ...config, projectBindingFile, candidateHarnesses: ['member-a'],
    teamProfile: { collaboration: 'leader', leader: 'member-a', instructions: '先理解新的用户反馈' },
  }, env)
  const args = { key: 'after-team', query: 'New feedback', previous_task_ref: first.task_ref }
  const next = await configured.submit(args, execution())
  assert.equal(next.session_id, first.session_id)
  assert.notEqual(next.team_id, first.team_id)
  assert.ok(server.requests[1].body.question.includes('先理解新的用户反馈'))
  assert.deepEqual(server.requests[1].body.project_context, { workspace_root: '/fixture/team-project' })
  assert.deepEqual(server.requests[1].body.plan, { team_policy: { candidate_harnesses: ['member-a'] } })
  const saved = await configured.store.get(execution().sessionId, next.task_ref)
  assert.equal(saved.previous_task_ref, first.task_ref)
  assert.equal(saved.service, (await client.store.get(execution().sessionId, first.task_ref)).service)
  assert.ok(!JSON.stringify(saved).includes('先理解新的用户反馈'), 'Raw configuration must not enter the recovery identity')
  assert.equal(await readFile(oldPath, 'utf8'), oldBytes)
  const message = { task_ref: next.task_ref, operation_key: 'team-message', recipient: 'author', text: 'Original feedback text' }
  await configured.message(message, execution())
  await configured.message(message, execution())
  assert.equal(server.messageCount, 1)
  assert.equal(server.requests.at(-1).body.text, 'Original feedback text')
  const cancelled = await configured.cancel({ task_ref: next.task_ref, reason: 'User requested stop' }, execution())
  assert.equal(cancelled.cancellation, 'requested')
  assert.equal(cancelled.stopped_confirmed, false)
  assert.equal(server.requests.at(-1).body.team_id, next.team_id)
  const count = server.requests.length
  await writeFile(projectBindingFile, JSON.stringify({ workspace_root: '/fixture/another-project' }))
  await assert.rejects(configured.submit(args, execution()), /different input/i)
  assert.equal(server.requests.length, count)
})

test('team configuration: submit arguments cannot override saved team preferences or introduce authority', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  for (const [index, teamProfile] of [undefined, { goal: 'User-owned configured goal' }].entries()) {
    const client = new LegionClient({ ...config, teamProfile }, env)
    const args = { key: `team-injected-${index}`, query: 'Current question' }
    const result = await client.submit({
      ...args, teamProfile: { goal: 'INJECTED_TEAM_GOAL', authority: 'INJECTED_AUTHORITY' },
      authority: 'INJECTED_AUTHORITY', uid: 'INJECTED_UID',
    }, execution())
    assert.equal(result.submission, 'accepted')
    const body = server.requests.at(-1).body
    assert.ok(!JSON.stringify(body).includes('INJECTED_'))
    if (teamProfile) assert.ok(body.question.includes('User-owned configured goal'))
    else assert.equal(body.question, 'Current question')
    assert.deepEqual(Object.keys(body), ['question', 'mode', 'allow_direct_model', 'session_id', 'team_id'])
    assert.equal((await client.submit(args, execution())).reused, true)
  }
  assert.equal(server.requests.length, 2)
})

test('team configuration: caller cancellation still prevents reservation and dispatch', async t => {
  const { server, config, env, LegionClient } = await setup(t)
  const client = new LegionClient({ ...config, teamProfile: {} }, env)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(client.submit({ key: 'team-aborted', query: 'Current task' }, {
    sessionId: execution().sessionId, signal: controller.signal,
  }), { name: 'AbortError' })
  assert.equal(server.requests.length, 0)
  assert.deepEqual((await client.list(execution())).tasks, [])
})
