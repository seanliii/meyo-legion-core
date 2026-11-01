import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { LegionClient } from '../lib/client.js'
import { TaskStore } from '../lib/store.js'

async function setup(t) {
  const root = await mkdtemp(join(tmpdir(), 'legion-identity-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const tokenFile = join(root, 'token')
  await writeFile(tokenFile, 'fixture-identity-A\n', { mode: 0o600 })
  const config = { baseUrl: 'https://identity.invalid', stateDirectory: join(root, 'state'), tokenFile }
  const clients = []
  const client = (overrides = {}) => {
    const result = new LegionClient({ ...config, ...overrides }, { LEGION_PERSONAL_TOKEN: 'fixture-no-fallback' })
    clients.push(result)
    return result
  }
  t.after(() => clients.forEach(value => value.dispose()))
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    requests.push({ url: String(url), init })
    if (String(url).endsWith('/api/legion/dispatch')) {
      const body = JSON.parse(init.body)
      return new Response(`event: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`,
        { headers: { 'Content-Type': 'text/event-stream' } })
    }
    return Response.json({ installed: false })
  })
  return { root, tokenFile, config, client, requests,
    execution: { sessionId: 'same-dsh-session', signal: new AbortController().signal } }
}

test('recovery indexes bind the service and actual credential, not just a path or variable name', async t => {
  const f = await setup(t)
  const a = f.client()
  const first = await a.submit({ key: 'same', query: 'Synthetic task' }, f.execution)
  const savedPath = join(f.config.stateDirectory, first.task_ref, 'ref.json')
  const before = await readFile(savedPath, 'utf8')
  await writeFile(f.tokenFile, 'fixture-identity-B\n')
  const b = f.client()
  assert.deepEqual((await b.list(f.execution)).tasks, [])
  await assert.rejects(b.status({ task_ref: first.task_ref }, f.execution), /different configured service|identity/i)
  await assert.rejects(b.submit({ key: 'follow-up', query: 'Synthetic', previous_task_ref: first.task_ref }, f.execution),
    /different configured service|identity/i)
  assert.equal(f.requests.length, 1)
  const second = await b.submit({ key: 'same', query: 'Synthetic task' }, f.execution)
  assert.notEqual(second.task_ref, first.task_ref)
  assert.equal(await readFile(savedPath, 'utf8'), before)
  assert.ok(!before.includes('fixture-identity-A'))
  assert.ok(!before.includes(f.tokenFile))
  const copy = join(f.root, 'same-identity-elsewhere')
  await writeFile(copy, 'fixture-identity-A\n', { mode: 0o600 })
  assert.deepEqual((await f.client({ tokenFile: copy }).list(f.execution)).tasks.map(row => row.task_ref), [first.task_ref])
  assert.deepEqual((await f.client({ tokenFile: copy, baseUrl: 'https://other.invalid' }).list(f.execution)).tasks, [])
})

test('unscoped legacy references are neither migrated nor sent using the current credential', async t => {
  const f = await setup(t)
  const legacy = new TaskStore(f.config.stateDirectory, f.config.baseUrl)
  const { ref } = await legacy.reserve({ sessionId: f.execution.sessionId, key: 'legacy', query: 'Synthetic' })
  const client = f.client()
  assert.deepEqual((await client.list(f.execution)).tasks, [])
  await assert.rejects(client.cancel({ task_ref: ref.id }, f.execution), /different configured service|identity/i)
  assert.equal(f.requests.length, 0)
})

test('credential changes during reservation cannot send the old task with new authority; old receipt stays in its own index', async t => {
  const f = await setup(t)
  const a = f.client()
  const original = a.store.reserve.bind(a.store)
  t.mock.method(a.store, 'reserve', async args => {
    const result = await original(args)
    await writeFile(f.tokenFile, 'fixture-identity-B\n')
    return result
  })
  const result = await a.submit({ key: 'interrupted', query: 'Synthetic' }, f.execution)
  assert.equal(result.submission, 'unconfirmed')
  assert.equal(result.request_sent, false)
  assert.equal(f.requests.length, 0)
  assert.equal(a.controller.signal.aborted, true)
  const b = f.client()
  assert.deepEqual((await b.list(f.execution)).tasks, [])
  await assert.rejects(b.status({ task_ref: result.task_ref }, f.execution), /different configured service|identity/i)
  const receipt = JSON.parse(await readFile(join(f.config.stateDirectory, result.task_ref, 'submission.json'), 'utf8'))
  assert.equal(receipt.submission, 'unconfirmed')
  assert.ok(!JSON.stringify(receipt).includes('fixture-identity-'))
})

test('a rotated credential closes the old client; a fresh client may use it but cannot read old references', async t => {
  const f = await setup(t)
  const a = f.client()
  await a.runtime({}, f.execution)
  await writeFile(f.tokenFile, 'fixture-identity-B\n')
  await assert.rejects(a.runtime({}, f.execution), /credential changed|reload/i)
  assert.equal(a.controller.signal.aborted, true)
  assert.equal(f.requests.length, 1)
  await f.client().runtime({}, f.execution)
  assert.equal(f.requests.at(-1).init.headers.Authorization, 'Bearer fixture-identity-B')
})

test('missing or unsafe credentials cannot expose a prior identity index; same-credential repair retains recovery', async t => {
  const f = await setup(t)
  const a = f.client()
  const first = await a.submit({ key: 'original', query: 'Synthetic' }, f.execution)
  await chmod(f.tokenFile, 0o644)
  await assert.rejects(a.list(f.execution), /credential file/i)
  await assert.rejects(f.client().list(f.execution), /credential file/i)
  await chmod(f.tokenFile, 0o600)
  assert.deepEqual((await f.client().list(f.execution)).tasks.map(row => row.task_ref), [first.task_ref])
  assert.equal(f.requests.length, 1)
})
