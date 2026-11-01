import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, stat, rm, mkdir, writeFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

async function setup(t, service = 'http://127.0.0.1:19876') {
  const module = await import('../lib/store.js').catch(() => ({}))
  assert.equal(typeof module.TaskStore, 'function', 'TaskStore must exist')
  const root = await mkdtemp(join(tmpdir(), 'legion-store-test-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  return { store: new module.TaskStore(root, service), root, TaskStore: module.TaskStore }
}

test('reserve writes one durable reference before dispatch and deduplicates concurrent callers', async t => {
  const { store, root } = await setup(t)
  const args = { sessionId: 'session-A', key: 'draft', query: 'Write a draft', context: '' }
  const refs = await Promise.all(Array.from({ length: 8 }, () => store.reserve(args)))
  assert.equal(refs.filter(x => x.created).length, 1)
  assert.equal(new Set(refs.map(x => x.ref.team_id)).size, 1)
  const text = await readFile(join(root, refs[0].ref.id, 'ref.json'), 'utf8')
  assert.ok(text.includes(refs[0].ref.team_id))
  assert.ok(!text.includes('Write a draft'), 'Raw task text is not duplicated into the local index')
  assert.equal((await stat(join(root, refs[0].ref.id, 'ref.json'))).mode & 0o777, 0o600)
})

test('a reused key with changed input is rejected, not silently sent twice', async t => {
  const { store } = await setup(t)
  await store.reserve({ sessionId: 'A', key: 'draft', query: 'Original' })
  await assert.rejects(store.reserve({ sessionId: 'A', key: 'draft', query: 'Different' }), /different|changed/i)
})

test('task lookup survives restart but never crosses session or configured service', async t => {
  const { store, root, TaskStore } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'draft', query: 'Original' })
  const restarted = new TaskStore(root, 'http://127.0.0.1:19876')
  assert.equal((await restarted.get('A', ref.id)).team_id, ref.team_id)
  await assert.rejects(restarted.get('B', ref.id), /session/i)
  const otherService = new TaskStore(root, 'http://127.0.0.1:19877')
  await assert.rejects(otherService.get('A', ref.id), /service/i)
  assert.equal((await restarted.list('A')).length, 1)
  assert.equal((await restarted.list('B')).length, 0)
})

test('model-provided task ids cannot become filesystem paths', async t => {
  const { store } = await setup(t)
  await assert.rejects(store.get('A', '../../.ssh/id_rsa'), /task|reference/i)
})

test('operation receipts are atomic and retained separately from the immutable task reference', async t => {
  const { store, root } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'draft', query: 'Original' })
  const before = await readFile(join(root, ref.id, 'ref.json'), 'utf8')
  assert.equal(await store.reserveOperation(ref, 'resume', 'op-1', 'same-input'), true)
  assert.equal(await store.reserveOperation(ref, 'resume', 'op-1', 'same-input'), false)
  await assert.rejects(store.reserveOperation(ref, 'resume', 'op-1', 'changed-input'), /different|changed/i)
  assert.equal(await readFile(join(root, ref.id, 'ref.json'), 'utf8'), before)
  assert.ok((await readdir(join(root, ref.id))).some(name => name.startsWith('op-')))
})

test('a follow-up uses the original Legion conversation but a new discoverable team', async t => {
  const { store } = await setup(t)
  const { ref: first } = await store.reserve({ sessionId: 'A', key: 'first', query: 'Original' })
  const { ref: next } = await store.reserve({ sessionId: 'A', key: 'next', query: 'A revision', previousRef: first })
  assert.equal(next.session_id, first.session_id)
  assert.notEqual(next.team_id, first.team_id)
  assert.ok(next.team_id.startsWith(`web_${first.session_id}_`))
  assert.equal(next.previous_task_ref, first.id)
  await assert.rejects(store.reserve({ sessionId: 'A', key: 'next', query: 'A revision' }), /different/i)
})

test('list tolerates an unpublished reference directory during another writer or crash', async t => {
  const { store, root } = await setup(t)
  await mkdir(join(root, 'a'.repeat(32)))
  assert.deepEqual(await store.list('A'), [])
})

test('stale artifact snapshots do not overwrite exports observed by another caller', async t => {
  const { store, root, TaskStore } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'exports', query: 'Files' })
  const second = new TaskStore(root, 'http://127.0.0.1:19876')
  const a = { artifact_id: 'a'.repeat(32), name: 'a.txt' }
  const b = { artifact_id: 'b'.repeat(32), name: 'b.txt' }
  // Both queries read the old empty snapshot before either has published.
  const staleA = [...await store.readObservation(ref, 'artifacts') || [], a]
  const staleB = [...await second.readObservation(ref, 'artifacts') || [], b]
  await second.writeObservation(ref, 'artifacts', staleB)
  await store.writeObservation(ref, 'artifacts', staleA)
  assert.deepEqual((await second.readObservation(ref, 'artifacts')).map(x => x.name).sort(), ['a.txt', 'b.txt'])
})

test('artifact observations remain additive across two real Node processes', async t => {
  const { store, root } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'exports', query: 'Files' })
  const moduleUrl = new URL('../lib/store.js', import.meta.url).href
  await Promise.all(['a', 'b'].map(key => promisify(execFile)(process.execPath, [
    '--input-type=module', '-e', `
      import { TaskStore } from ${JSON.stringify(moduleUrl)};
      const store = new TaskStore(${JSON.stringify(root)}, 'http://127.0.0.1:19876');
      await store.writeObservation(${JSON.stringify(ref)}, 'artifacts',
        [{ artifact_id: ${JSON.stringify(key.repeat(32))}, name: ${JSON.stringify(`${key}.txt`)} }]);
    `,
  ], { timeout: 5000 })))
  assert.deepEqual((await store.readObservation(ref, 'artifacts')).map(x => x.name).sort(), ['a.txt', 'b.txt'])
})

test('keyed artifacts preserve legacy exports and ignore unpublished temporary files', async t => {
  const { store, root } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'exports', query: 'Files' })
  const old = { artifact_id: 'a'.repeat(32), name: 'old.txt' }
  await writeFile(join(root, ref.id, 'artifacts.json'), JSON.stringify([old]), { mode: 0o600 })
  await store.writeObservation(ref, 'artifacts', [{ artifact_id: 'b'.repeat(32), name: 'b.txt' }])
  assert.deepEqual((await store.readObservation(ref, 'artifacts')).map(x => x.name).sort(), ['b.txt', 'old.txt'])
  await store.writeObservation(ref, 'artifacts', [{ ...old, name: 'updated.txt' }])
  await writeFile(join(root, ref.id, 'artifacts', `${'c'.repeat(32)}.json.unpublished.tmp`), '{')
  assert.deepEqual((await store.readObservation(ref, 'artifacts')).map(x => x.name).sort(), ['b.txt', 'updated.txt'])
  assert.equal((await stat(join(root, ref.id, 'artifacts', `${'b'.repeat(32)}.json`))).mode & 0o777, 0o600)
})

test('artifact keys cannot become paths and invalid batches publish no partial updates', async t => {
  const { store } = await setup(t)
  const { ref } = await store.reserve({ sessionId: 'A', key: 'exports', query: 'Files' })
  await assert.rejects(store.writeObservation(ref, 'artifacts', [
    { artifact_id: 'a'.repeat(32), name: 'a.txt' },
    { artifact_id: '../outside', name: 'bad.txt' },
  ]), /artifact/i)
  assert.deepEqual(await store.readObservation(ref, 'artifacts') || [], [])
})
