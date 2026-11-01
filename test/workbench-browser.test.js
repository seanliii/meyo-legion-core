import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createContext, runInContext } from 'node:vm'
import { webcrypto } from 'node:crypto'
import { executeWorkbenchCommand } from '../lib/workbench.js'

function memoryStorage() {
  const values = new Map()
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key) }
}

async function helpers(globals = {}) {
  let definition
  const realm = createContext({
    window: { localStorage: memoryStorage(), __ModuleLoader__: { load(value) { definition = value } } },
    URL, AbortController, DOMException, TextEncoder, console, crypto: webcrypto, ...globals,
  })
  runInContext(await readFile(new URL('../client/index.js', import.meta.url), 'utf8'), realm)
  const plugin = definition.factory(() => assert.fail('Pure helpers do not import another browser plugin'))
  assert.ok(plugin.workbench, 'The served native bundle must contain the actual workbench')
  return plugin.workbench
}

const plain = value => JSON.parse(JSON.stringify(value))

test('workbench resolves configuration only from the actual runtime entry id', async () => {
  const { configEntry } = await helpers()
  const namespaces = [{ ns: 'some-legion-lookalike' }, { ns: 'actual-loader-entry' }]
  assert.deepEqual(plain(configEntry([
    { entryId: 'actual-loader-entry', moduleName: 'dsh-plugin-legion' },
  ], namespaces)), { entryId: 'actual-loader-entry', namespace: 'actual-loader-entry' })
  assert.equal(configEntry([{ rowId: 'actual-loader-entry', moduleName: 'dsh-plugin-legion' }], namespaces), null)
  assert.equal(configEntry([{ entryId: 'some-legion-lookalike', moduleName: 'evil-legion' }], namespaces), null)
  assert.equal(configEntry([
    { entryId: 'actual-loader-entry', moduleName: 'dsh-plugin-legion' },
    { entryId: 'some-legion-lookalike', moduleName: 'dsh-plugin-legion' },
  ], namespaces), null, 'An ambiguous duplicate must not silently edit the first one')
})

test('member presentation preserves unknown installation and separate observed readiness', async () => {
  const { memberFacts } = await helpers()
  assert.deepEqual(plain(memberFacts({ key: 'example', available: null,
    readiness: { declared: true, connected: false, verified: false },
    entry_points: { execution: { selectable: true } } })), {
    key: 'example', installed: null, connected: false, verified: false, available: null, selectable: true,
  })
  assert.equal(memberFacts({ key: 'tools-only', available: true,
    entry_points: { execution: { selectable: false } } }).selectable, false)
  assert.equal(memberFacts({ key: 'untyped', installed: 'yes', available: 'ready' }).installed, null)
  assert.equal(memberFacts({ key: 'untyped' }).selectable, null,
    'Missing execution facts are unknown, not a declaration that this is a tool-only source')
})

test('untrusted result links never become script, file or credential links', async () => {
  const { publicLink } = await helpers()
  assert.equal(publicLink('https://github.com/example/tool'), 'https://github.com/example/tool')
  for (const value of ['javascript:alert(1)', 'data:text/html,bad', 'file:///secret', 'http://127.0.0.1/',
    'https://user:password@github.com/example/tool', 'https://github.com/?token=secret', '//evil.test',
    null, {}]) assert.equal(publicLink(value), null)
})

test('config edits use atomic native paths without clobbering unrelated limits or authority', async () => {
  const { configOperations } = await helpers()
  const ops = plain(configOperations({
    baseUrl: 'https://legion.example', tokenEnv: 'MY_LEGION_TOKEN', tokenFile: '',
    candidateHarnesses: ['a', 'b'],
  }))
  assert.deepEqual(ops, [
    { op: 'set', path: ['baseUrl'], value: 'https://legion.example' },
    { op: 'set', path: ['tokenEnv'], value: 'MY_LEGION_TOKEN' },
    { op: 'unset', path: ['tokenFile'] },
    { op: 'set', path: ['candidateHarnesses'], value: ['a', 'b'] },
  ])
  assert.ok(!ops.some(op => !op.path.length || op.path.includes('projectBindingFile')))
})

test('native change results retain failed or restart-required outcomes instead of reporting done', async () => {
  const { changeFacts } = await helpers()
  assert.deepEqual(plain(changeFacts({ changed: true, application: 'failed', stage: 'enable',
    error: { code: 'operation-error', diagnostic: 'cannot load' } })), {
    changed: true, application: 'failed', stage: 'enable', complete: false,
    reason: 'cannot load', warnings: [],
  })
  assert.equal(changeFacts({ changed: true, application: 'restart-required' }).complete, false)
  assert.equal(changeFacts({ changed: true, application: 'applied' }).complete, true)
})

function context(execute) {
  return {
    sessions: {
      list: {
        getSnapshot: () => ({ ids: ['a', 'b'], byId: { a: { id: 'a' }, b: { id: 'b' } }, phase: 'ready' }),
        subscribe: () => () => {},
      },
      async using(id, options, fn) {
        assert.equal(options.source, 'controllerOperation')
        return fn({ sessionId: id, ready: Promise.resolve() })
      },
    },
    remote: { commands: { execute } },
  }
}

const reply = (action, value) => ({ ok: true,
  value: { result: { kind: 'success', text: JSON.stringify({ ok: true, action, value }) } } })

// Submission fixtures explicitly model the new read-only native identity read;
// their callbacks still count actual submits, not the preceding index read.
function submissionContext(execute) {
  return context(async (id, line, ...rest) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') {
      assert.deepEqual(args, { limit: 1 })
      return reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) })
    }
    return execute(id, line, ...rest)
  })
}

const nativeReply = value => ({ ok: true, value })
const turn = () => new Promise(resolve => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const managerReads = {
  async inspect(spec) { return nativeReply({ status: 'accepted', name: spec }) },
  async listBundles() { return nativeReply([]) },
  async listPlugins() { return nativeReply([]) },
}

test('late command results cannot leak from a previous selected session', async () => {
  const { createController } = await helpers()
  let resolve
  const controller = createController(context(async (id, line) => {
    assert.equal(id, 'a')
    assert.match(line, /^\/legion-panel /)
    return new Promise(done => { resolve = done })
  }))
  controller.selectSession('a')
  const pending = controller.loadMembers()
  await new Promise(done => setImmediate(done))
  controller.selectSession('b')
  resolve(reply('runtime', { runtime: { harnesses: [{ key: 'belongs-to-a' }] } }))
  await pending
  assert.equal(controller.getSnapshot().sessionId, 'b')
  assert.equal(controller.getSnapshot().members, null)
  controller.dispose()
})

test('UI submits use one original key and never accept client-provided session authority', async () => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(submissionContext(async (id, line) => {
    const body = JSON.parse(line.slice('/legion-panel '.length))
    calls.push({ id, ...body })
    if (calls.length === 1) throw new Error('response lost')
    return reply('submit', { task_ref: 'returned-task', reused: true })
  }))
  controller.selectSession('a')
  await controller.submit('Inspect this public project', 'Known source facts')
  assert.equal(controller.getSnapshot().operation.ref, undefined)
  await controller.submit('Inspect this public project', 'Known source facts')
  assert.equal(calls[0].args.key, calls[1].args.key)
  assert.deepEqual(Object.keys(calls[0].args).sort(), ['connection_id', 'context', 'key', 'query'])
  assert.equal(calls[0].args.connection_id, 'a'.repeat(64))
  assert.equal(controller.getSnapshot().operation.ref, 'returned-task')
  controller.dispose()
})

test('native installation is explicit, does not request activation, and cannot double-submit while pending', async () => {
  const { createController, changeFacts } = await helpers()
  let resolve
  const calls = []
  const controller = createController(context(() => assert.fail('install is a native manager operation')))
  controller.attach('manager', {
    async inspect(spec) { return { ok: true, value: { status: 'accepted', name: spec, bundle: true } } },
    async installBundle(spec, options) {
      calls.push({ spec, options })
      return new Promise(done => { resolve = done })
    },
    async listBundles() { return { ok: true, value: [] } },
    async listPlugins() { return { ok: true, value: [] } },
  })
  await controller.inspectPackage('example@1.0.0')
  const pending = controller.installPackage()
  await new Promise(done => setImmediate(done))
  await controller.installPackage()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.enabled, false)
  assert.equal(calls[0].options.saveExact, true)
  assert.equal(Object.hasOwn(calls[0].options, 'approvedBuilds'), false)
  resolve({ ok: true, value: { changed: true, application: 'restart-required', stage: 'install' } })
  await pending
  assert.equal(controller.getSnapshot().installResult.application, 'restart-required')
  assert.equal(changeFacts(controller.getSnapshot().installResult).complete, false)
  controller.dispose()
})

test('configuration writes keep the editor revision and use the real native form', async () => {
  const { createController } = await helpers()
  const mutations = []
  const form = {
    getSnapshot: () => ({ status: 'ready', value: {}, revision: 7, writable: true }),
    subscribe: () => () => {},
    async mutate(ops, revision) { mutations.push({ ops: plain(ops), revision }); return false },
  }
  const controller = createController(context(() => assert.fail('configuration is not a model command')))
  controller.attach('configuration', {
    inventory: { async list() { return { ok: true, value: {
      entries: [{ entryId: 'actual-id', moduleName: 'dsh-plugin-legion' }],
    } } } },
    forms: {
      describe: () => ({ async ensure() {}, getSnapshot: () => ({ view: { namespaces: [{ ns: 'actual-id' }] } }) }),
      get(id) { assert.equal(id, 'actual-id'); return form },
    },
  })
  await controller.loadConfig()
  await controller.saveConfig({ candidateHarnesses: ['codex'] }, 3, 'actual-id')
  assert.equal(mutations[0].revision, 3, 'Do not overwrite a newer editor revision with the current snapshot')
  assert.deepEqual(mutations[0].ops, [{ op: 'set', path: ['candidateHarnesses'], value: ['codex'] }])
  assert.match(controller.getSnapshot().errors.configSave, /拒绝|冲突|refus|conflict/i)
  controller.dispose()
})

test('editing or rechecking a repository clears stale source facts and fences late results', async () => {
  const { createController } = await helpers()
  let release
  const controller = createController(context(async (_id, line) => {
    const { args } = JSON.parse(line.slice('/legion-panel '.length))
    if (args.repository === 'owner/slow') return new Promise(done => { release = done })
    if (args.repository === 'owner/broken') throw new Error('Source unavailable')
    return reply('inspect', { repository: args.repository, commit: 'a'.repeat(40) })
  }))
  controller.selectSession('a')
  await controller.inspectSource('owner/first')
  assert.equal(controller.getSnapshot().source.repository, 'owner/first')
  await controller.inspectSource('owner/broken')
  assert.equal(controller.getSnapshot().source, null, 'A failed inspection must not retain another source')
  const pending = controller.inspectSource('owner/slow')
  await new Promise(done => setImmediate(done))
  controller.clearSource()
  release(reply('inspect', { repository: 'owner/slow', commit: 'b'.repeat(40) }))
  await pending
  assert.equal(controller.getSnapshot().source, null, 'An edited input must not acquire stale source facts')
  controller.dispose()
})

test('an unloaded configuration service cannot install a late form subscription', async () => {
  const { createController } = await helpers()
  let release
  let formReads = 0
  const controller = createController(context(() => assert.fail('Not a command')))
  const detach = controller.attach('configuration', {
    inventory: { list: () => new Promise(done => { release = done }) },
    forms: {
      describe: () => ({ async ensure() {}, getSnapshot: () => ({ view: { namespaces: [{ ns: 'entry' }] } }) }),
      get() { formReads++; return { getSnapshot: () => ({}), subscribe: () => () => {} } },
    },
  })
  const pending = controller.loadConfig()
  await new Promise(done => setImmediate(done))
  detach()
  release({ ok: true, value: { entries: [{ entryId: 'entry', moduleName: 'dsh-plugin-legion' }] } })
  await pending
  assert.equal(formReads, 0)
  assert.equal(controller.getSnapshot().config, null)
  assert.equal(controller.getSnapshot().available.configuration, false)
  controller.dispose()
})

test('switching sessions retains an unconfirmed submission key only for its original session', async () => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(submissionContext(async (id, line) => {
    const body = JSON.parse(line.slice('/legion-panel '.length))
    calls.push({ id, ...body })
    if (calls.length === 1) throw new Error('Reply disconnected after dispatch')
    return reply('submit', { task_ref: 'recovered-task', reused: true })
  }))
  controller.selectSession('a')
  await controller.submit('Adapt a capability', 'Pinned repository facts')
  const original = plain(controller.getSnapshot().operation)
  controller.selectSession('b')
  assert.equal(controller.getSnapshot().operation, null)
  controller.selectSession('a')
  assert.deepEqual(plain(controller.getSnapshot().operation), original)
  await controller.submit(original.query, original.context)
  assert.equal(calls[0].args.key, calls[1].args.key, 'A UI navigation must not duplicate a charged remote task')
  assert.equal(controller.getSnapshot().operation.ref, 'recovered-task')
  controller.dispose()
})

test('a late submission receipt is recoverable in the originating session, never another', async () => {
  const { createController } = await helpers()
  let release
  const controller = createController(submissionContext(() => new Promise(done => { release = done })))
  controller.selectSession('a')
  const pending = controller.submit('Inspect a public project', '')
  await new Promise(done => setImmediate(done))
  controller.selectSession('b')
  release(reply('submit', { task_ref: 'belongs-to-a' }))
  await pending
  assert.equal(controller.getSnapshot().submission, null)
  controller.selectSession('a')
  assert.equal(controller.getSnapshot().operation.ref, 'belongs-to-a')
  assert.equal(controller.getSnapshot().submission.task_ref, 'belongs-to-a')
  controller.dispose()
})

test('late install recovery cannot unlock a different installation with an unknown outcome', async t => {
  const { createController } = await helpers()
  const installs = []
  const recovery = deferred()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('manager', {
    ...managerReads,
    installBundle(spec, options) {
      const pending = deferred()
      installs.push({ spec, options, ...pending })
      return pending.promise
    },
    waitForInstall(requestId) {
      assert.equal(requestId, installs[0].options.requestId)
      return recovery.promise
    },
  })
  await controller.inspectPackage('package-a@1.0.0')
  const first = controller.installPackage()
  await turn()
  const recoveringFirst = controller.recoverInstall()
  await turn()
  const firstResult = { application: 'applied', stage: 'install', target: 'package-a' }
  installs[0].resolve(nativeReply(firstResult))
  await first
  await controller.inspectPackage('package-b@1.0.0')
  const second = controller.installPackage()
  await turn()
  const secondId = controller.getSnapshot().installId
  recovery.resolve(nativeReply(firstResult))
  await recoveringFirst
  const uncertainAfterOldReply = controller.getSnapshot().installUncertain
  installs[1].reject(new Error('B reply disconnected after Host acceptance'))
  await second
  const duplicate = controller.installPackage()
  await turn()
  installs[2]?.resolve(nativeReply({ application: 'applied', stage: 'install' }))
  await duplicate
  assert.equal(uncertainAfterOldReply, true, 'A receipt must not confirm B')
  assert.equal(controller.getSnapshot().installId, secondId)
  assert.deepEqual(installs.map(call => call.spec), ['package-a@1.0.0', 'package-b@1.0.0'])
  assert.equal(controller.getSnapshot().installResult, null, 'Do not display A as B’s result')
})

test('confirmed native cancellation ends install uncertainty without requiring a retained result', async t => {
  const { createController } = await helpers()
  const inspections = []
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('manager', {
    ...managerReads,
    async inspect(spec) { inspections.push(spec); return nativeReply({ status: 'accepted' }) },
    async installBundle() { throw new Error('Reply disconnected after Host acceptance') },
    async cancelInstall(requestId) {
      assert.equal(requestId, controller.getSnapshot().installId)
      return nativeReply({ status: 'cancelled' })
    },
    async waitForInstall() { return nativeReply(null) },
  })
  await controller.inspectPackage('package-a@1.0.0')
  await controller.installPackage()
  await controller.cancelInstall()
  assert.equal(controller.getSnapshot().installUncertain, false)
  assert.equal(controller.getSnapshot().installCancellation.status, 'cancelled')
  assert.notEqual(controller.getSnapshot().installResult?.application, 'applied')
  await controller.inspectPackage('package-b@1.0.0')
  assert.deepEqual(inspections, ['package-a@1.0.0', 'package-b@1.0.0'])
})

test('nonfinal cancellation answers and missing recovery results leave installation unknown', async t => {
  const { createController } = await helpers()
  for (const status of ['not-running', 'too-late']) {
    await t.test(status, async t => {
      let installs = 0
      const controller = createController(context(() => assert.fail('Not a command')))
      t.after(() => controller.dispose())
      controller.attach('manager', {
        ...managerReads,
        async installBundle() { installs++; throw new Error('Reply disconnected') },
        async cancelInstall() { return nativeReply({ status }) },
        async waitForInstall() { return nativeReply(null) },
      })
      await controller.inspectPackage('package-a@1.0.0')
      await controller.installPackage()
      await controller.cancelInstall()
      await controller.recoverInstall()
      await controller.installPackage()
      assert.equal(controller.getSnapshot().installUncertain, true)
      assert.equal(controller.getSnapshot().installResult, null)
      assert.equal(installs, 1, 'Neither answer grants permission to blindly reinstall')
    })
  }
})

test('configuration drafts cannot cross entry identities even when revisions are equal', async t => {
  const { createController } = await helpers()
  let entryId = 'entry-a'
  const writes = []
  const forms = new Map(['entry-a', 'entry-b'].map(id => [id, {
    getSnapshot: () => ({ status: 'ready', value: {}, writable: true, revision: 7 }),
    subscribe: () => () => {},
    async mutate(ops, revision) { writes.push({ entryId: id, ops: plain(ops), revision }); return true },
  }]))
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', {
    inventory: { async list() { return nativeReply({ entries: [{ entryId, moduleName: 'dsh-plugin-legion' }] }) } },
    forms: {
      describe: () => ({ async ensure() {}, getSnapshot: () => ({ view: { namespaces: [{ ns: entryId }] } }) }),
      get: id => forms.get(id),
    },
  })
  await controller.loadConfig()
  const originalEntry = controller.getSnapshot().configEntry
  const originalRevision = controller.getSnapshot().config.revision
  entryId = 'entry-b'
  await controller.loadConfig()
  await controller.saveConfig({ baseUrl: 'https://intended-for-a.example.invalid' }, originalRevision, originalEntry)
  assert.equal(writes.length, 0, 'Revision 7 in B is not the editing identity from A')
  assert.match(controller.getSnapshot().errors.configSave, /实例|entry/i)
  await controller.saveConfig({ candidateHarnesses: ['example'] }, 7, 'entry-b')
  assert.deepEqual(writes, [{ entryId: 'entry-b', revision: 7,
    ops: [{ op: 'set', path: ['candidateHarnesses'], value: ['example'] }] }])
  assert.equal(controller.getSnapshot().configSaved, true)
})

test('rejected submission keeps its recovery reference and permits only an explicit new task', async t => {
  const { createController } = await helpers()
  const calls = []
  const taskRef = 'a'.repeat(32)
  const client = {
    clean: value => value, redactText: value => value,
    scope(execution) { assert.equal(execution.sessionId, 'a') },
    store: { service: 'a'.repeat(64), root: '/synthetic/index' },
    async list() { return { tasks: [] } },
    async submit(args) {
      calls.push(args)
      if (calls.length === 1) throw new Error('403 permission_denied')
      return { task_ref: taskRef, submission: 'rejected', reused: true, reason: '403 permission_denied' }
    },
  }
  const controller = createController(context(async (id, line) => nativeReply({
    result: await executeWorkbenchCommand({}, client, {}, {
      agent: { id }, signal: new AbortController().signal, attachments: [],
      rawInput: line.slice('/legion-panel '.length),
    }),
  })))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.submit('Public integration task', '')
  const originalKey = controller.getSnapshot().operation.key
  controller.newTask()
  assert.equal(controller.getSnapshot().operation.key, originalKey, 'An untyped error remains unconfirmed')
  await controller.submit('Public integration task', '')
  assert.equal(calls[1].key, originalKey)
  assert.equal(controller.getSnapshot().operation.ref, taskRef)
  assert.equal(controller.getSnapshot().submission.submission, 'rejected')
  assert.match(controller.getSnapshot().errors.submit, /403.*permission_denied/)
  await controller.submit('Changed task without explicit reset', '')
  assert.equal(calls.length, 2)
  controller.newTask()
  assert.equal(controller.getSnapshot().operation, null)
  assert.equal(controller.getSnapshot().submission, null)
  await controller.submit('An explicitly new task', '')
  assert.notEqual(calls[2].key, originalKey)
})

test('a late cancellation receipt belongs only to its original installation', async t => {
  const { createController } = await helpers()
  const installs = [], cancellation = deferred()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('manager', {
    ...managerReads,
    installBundle(spec, options) {
      const pending = deferred()
      installs.push({ spec, options, ...pending })
      return pending.promise
    },
    cancelInstall(requestId) {
      assert.equal(requestId, installs[0].options.requestId)
      return cancellation.promise
    },
  })
  await controller.inspectPackage('package-a@1.0.0')
  const first = controller.installPackage()
  await turn()
  const cancellingFirst = controller.cancelInstall()
  installs[0].resolve(nativeReply({ application: 'cancelled', stage: 'install' }))
  await first
  await controller.inspectPackage('package-b@1.0.0')
  const second = controller.installPackage()
  await turn()
  cancellation.resolve(nativeReply({ status: 'cancelled' }))
  await cancellingFirst
  const staleCancellation = controller.getSnapshot().installCancellation
  installs[1].reject(new Error('B reply disconnected'))
  await second
  assert.equal(staleCancellation ?? null, null, 'A must not supply B’s cancellation status')
  assert.equal(controller.getSnapshot().installUncertain, true)
})

test('confirmed cancellation retires pending original waits before a new install starts', async t => {
  const { createController } = await helpers()
  const installs = [], recovery = deferred()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('manager', {
    ...managerReads,
    installBundle(spec, options) {
      const pending = deferred()
      installs.push({ spec, options, ...pending })
      return pending.promise
    },
    waitForInstall: () => recovery.promise,
    async cancelInstall() { return nativeReply({ status: 'cancelled' }) },
  })
  await controller.inspectPackage('package-a@1.0.0')
  const first = controller.installPackage()
  await turn()
  const recovering = controller.recoverInstall()
  await controller.cancelInstall()
  assert.equal(controller.getSnapshot().busy.install, false, 'A confirmed stop also ends the old local wait')
  await controller.inspectPackage('package-b@1.0.0')
  const second = controller.installPackage()
  await turn()
  installs[0].resolve(nativeReply({ application: 'cancelled', stage: 'install' }))
  recovery.resolve(nativeReply({ application: 'cancelled', stage: 'install' }))
  await Promise.all([first, recovering])
  assert.equal(controller.getSnapshot().installId, installs[1].options.requestId)
  assert.equal(controller.getSnapshot().installUncertain, true)
  assert.equal(controller.getSnapshot().installCancellation ?? null, null)
  installs[1].resolve(nativeReply({ application: 'applied', stage: 'install' }))
  await second
})

test('a late configuration save cannot publish an old form under a new entry', async t => {
  const { createController } = await helpers()
  let entryId = 'entry-a'
  const write = deferred()
  const forms = new Map(['entry-a', 'entry-b'].map(id => [id, {
    getSnapshot: () => ({ status: 'ready', value: { baseUrl: `https://${id}.example.invalid` }, writable: true, revision: 7 }),
    subscribe: () => () => {},
    mutate: () => write.promise,
  }]))
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', {
    inventory: { async list() { return nativeReply({ entries: [{ entryId, moduleName: 'dsh-plugin-legion' }] }) } },
    forms: {
      describe: () => ({ async ensure() {}, getSnapshot: () => ({ view: { namespaces: [{ ns: entryId }] } }) }),
      get: id => forms.get(id),
    },
  })
  await controller.loadConfig()
  const saving = controller.saveConfig({ baseUrl: 'https://edit.example.invalid' }, 7, 'entry-a')
  await turn()
  entryId = 'entry-b'
  await controller.loadConfig()
  write.resolve(true)
  const accepted = await saving
  assert.equal(controller.getSnapshot().config.value.baseUrl, 'https://entry-b.example.invalid')
  assert.equal(controller.getSnapshot().configSaved, false)
  assert.notEqual(accepted, true, 'A superseded save must not tell an editor to clear its current draft')
})

test('a late rejected submission is retained in its own session and never published in another', async t => {
  const { createController } = await helpers()
  const pending = deferred()
  const controller = createController(submissionContext(() => pending.promise))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const saving = controller.submit('Public task', '')
  await turn()
  controller.selectSession('b')
  pending.resolve(nativeReply({ result: { kind: 'error', text: JSON.stringify({
    ok: false, action: 'submit', error: '403 permission_denied',
    diagnostics: { task_ref: 'b'.repeat(32), submission: 'rejected' },
  }) } }))
  await saving
  assert.equal(controller.getSnapshot().operation, null)
  assert.equal(controller.getSnapshot().submission, null)
  assert.equal(controller.getSnapshot().errors.submit, null)
  controller.selectSession('a')
  assert.equal(controller.getSnapshot().operation.ref, 'b'.repeat(32))
  assert.equal(controller.getSnapshot().submission.submission, 'rejected')
})

// Identity-only reproduction of the native RPC shapes; no profile or credentials.
function mappedConfiguration() {
  const identity = { entryId: 'include:legion', namespace: 'legion' }
  const reads = [], writes = []
  const form = {
    getSnapshot: () => ({ status: 'ready', value: {}, revision: 7, writable: true }),
    subscribe: () => () => {},
    async mutate(ops, revision) {
      writes.push({ ...identity, ops: plain(ops), revision })
      return true
    },
  }
  const owner = {
    inventory: { async list() {
      return nativeReply({ entries: [{ entryId: identity.entryId, moduleName: 'dsh-plugin-legion' }] })
    } },
    manager: { async listBundles() {
      return nativeReply([{ name: 'dsh-plugin-legion', rows: [{
        entryId: identity.entryId, rowId: identity.namespace, moduleName: 'dsh-plugin-legion',
      }] }])
    } },
    forms: {
      describe: () => ({ async ensure() {}, getSnapshot: () => ({ view: { namespaces: [{ ns: identity.namespace }] } }) }),
      get(namespace) { reads.push(namespace); return form },
    },
  }
  return { identity, owner, form, reads, writes }
}

test('native configuration uses the bundle row namespace but retains the loader identity when saving', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', fixture.owner)
  assert.equal(await controller.loadConfig(), true, 'The real include:legion → legion mapping must resolve')
  assert.deepEqual(fixture.reads, ['legion'], 'ConfigForms takes rowId, not the loader entryId')
  assert.equal(controller.getSnapshot().configEntry, 'include:legion')
  assert.equal(controller.getSnapshot().configNamespace, 'legion')
  assert.equal(await controller.saveConfig({ candidateHarnesses: ['example'] }, 7, 'include:legion', 'legion'), true)
  assert.deepEqual(fixture.writes, [{
    entryId: 'include:legion', namespace: 'legion', revision: 7,
    ops: [{ op: 'set', path: ['candidateHarnesses'], value: ['example'] }],
  }])
})

test('native identity mapping is explicit and rejects ambiguous or wrong-module matches', async () => {
  const { configEntry } = await helpers()
  const entry = { entryId: 'a-tree:opaque-loader-id', moduleName: 'dsh-plugin-legion' }
  const row = { ...entry, rowId: 'user-chosen-configuration' }
  const namespaces = [{ ns: row.rowId }]
  const bundle = { name: 'dsh-plugin-legion', rows: [row] }
  assert.deepEqual(plain(configEntry([entry], namespaces, [bundle])), {
    entryId: entry.entryId, namespace: row.rowId,
  }, 'Neither a hardcoded name nor splitting the loader identity can find this namespace')
  for (const [entries, sections, bundles] of [
    [[entry, { ...entry, entryId: 'another-live-instance' }], namespaces, [bundle]],
    [[{ ...entry, moduleName: 'another-module' }], namespaces, [bundle]],
    [[entry], namespaces, [{ ...bundle, name: 'another-bundle' }]],
    [[entry], namespaces, [{ ...bundle, rows: [{ ...row, moduleName: 'another-module' }] }]],
    [[entry], namespaces, [{ ...bundle, rows: [{ ...row, entryId: 'another-loader' }] }]],
    [[entry], namespaces, [{ ...bundle, rows: [row, { ...row, rowId: 'another-namespace' }] }]],
    [[entry], [...namespaces, ...namespaces], [bundle]],
    [[entry], namespaces, []],
  ]) assert.equal(configEntry(entries, sections, bundles), null)
  assert.equal(configEntry([entry, { ...entry, entryId: 'hidden-duplicate' }], [{ ns: entry.entryId }]), null,
    'A direct namespace match cannot hide a second live instance')
})

test('mapped drafts bind both loader identity and namespace even when the revision and form are reused', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', fixture.owner)
  await controller.loadConfig()
  const original = { ...fixture.identity }
  fixture.identity.entryId = 'another-tree:legion'
  await controller.loadConfig()
  await controller.saveConfig({ baseUrl: 'https://old.example.invalid' }, 7, original.entryId, original.namespace)
  assert.equal(fixture.writes.length, 0, 'The same namespace and revision do not identify the same loader instance')
  const next = { ...fixture.identity }
  fixture.identity.namespace = 'renamed-configuration'
  await controller.loadConfig()
  await controller.saveConfig({ baseUrl: 'https://old.example.invalid' }, 7, next.entryId, next.namespace)
  assert.equal(fixture.writes.length, 0, 'The same loader and revision do not identify the same configuration namespace')
  assert.equal(await controller.saveConfig({ baseUrl: 'https://current.example.invalid' }, 7,
    fixture.identity.entryId, fixture.identity.namespace), true)
  assert.equal(fixture.writes.length, 1)
})

test('a late mapped save cannot clear a draft after its loader identity changes under the same namespace', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration(), pending = deferred()
  let mutations = 0
  fixture.form.mutate = () => { mutations++; return pending.promise }
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', fixture.owner)
  await controller.loadConfig()
  const saving = controller.saveConfig({}, 7, fixture.identity.entryId, fixture.identity.namespace)
  await turn()
  const started = mutations
  fixture.identity.entryId = 'replacement-tree:legion'
  await controller.loadConfig()
  pending.resolve(true)
  const accepted = await saving
  assert.equal(started, 1, 'The first mapped instance must actually start a native save')
  assert.equal(accepted, false)
  assert.equal(controller.getSnapshot().configSaved, false)
  assert.equal(controller.getSnapshot().configEntry, 'replacement-tree:legion')
})

test('unloading configuration during bundle mapping cannot subscribe a stale form or block member reads', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration(), pending = deferred()
  let bundleReads = 0
  fixture.owner.manager.listBundles = () => { bundleReads++; return pending.promise }
  const controller = createController(context(async () => reply('runtime', { runtime: { harnesses: [] } })))
  t.after(() => controller.dispose())
  const detach = controller.attach('configuration', fixture.owner)
  const loading = controller.loadConfig()
  await turn()
  detach()
  pending.resolve(nativeReply([{ name: 'dsh-plugin-legion', rows: [{
    entryId: 'include:legion', rowId: 'legion', moduleName: 'dsh-plugin-legion',
  }] }]))
  await loading
  assert.equal(bundleReads, 1, 'Configuration must read the native bundle identity map')
  assert.deepEqual(fixture.reads, [])
  assert.equal(controller.getSnapshot().config, null)
  assert.equal(controller.getSnapshot().available.configuration, false)
  controller.selectSession('a')
  await controller.loadMembers()
  assert.deepEqual(plain(controller.getSnapshot().members), { runtime: { harnesses: [] } })
})

test('duplicate session titles gain distinguishable short ids without changing unique titles', async () => {
  const { sessionLabel } = await helpers()
  assert.equal(typeof sessionLabel, 'function')
  const ids = ['shared-prefix-first-12345678', 'shared-prefix-second-12345678', 'shared-prefix-third-87654321', 'unique', 'untitled']
  const sessions = { ids, byId: Object.fromEntries(ids.map(id => [id, {
    displayTitle: id === 'unique' ? 'A named conversation' : id === 'untitled' ? '' : 'dsh-workspace',
  }])) }
  const labels = ids.slice(0, 3).map(id => sessionLabel(sessions, id))
  assert.equal(new Set(labels).size, 3, 'Short ids must still distinguish common prefixes and colliding suffixes')
  for (const label of labels) assert.match(label, /^dsh-workspace · /)
  assert.equal(sessionLabel(sessions, 'unique'), 'A named conversation')
  assert.equal(sessionLabel(sessions, 'untitled'), 'untitled')
})

test('a mapped save rechecks native loader ownership instead of trusting a cached namespace and revision', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', fixture.owner)
  await controller.loadConfig()
  const editing = { ...fixture.identity }
  // The native form and its revision are unchanged, but the namespace now belongs
  // to another loader instance. The user has not pressed Reload configuration.
  fixture.identity.entryId = 'replacement-tree:legion'
  const accepted = await controller.saveConfig({ baseUrl: 'https://old.example.invalid' }, 7,
    editing.entryId, editing.namespace)
  assert.equal(fixture.writes.length, 0, 'A cached loader id is not current write authority')
  assert.equal(accepted, false)
  assert.match(controller.getSnapshot().errors.configSave, /实例|entry/i)
})

test('a failed ambiguous identity reread cannot authorize a save through the previous form', async t => {
  const { createController } = await helpers()
  const fixture = mappedConfiguration()
  const controller = createController(context(() => assert.fail('Not a command')))
  t.after(() => controller.dispose())
  controller.attach('configuration', fixture.owner)
  await controller.loadConfig()
  fixture.owner.inventory.list = async () => nativeReply({ entries: [
    { entryId: fixture.identity.entryId, moduleName: 'dsh-plugin-legion' },
    { entryId: 'another-instance', moduleName: 'dsh-plugin-legion' },
  ] })
  await controller.loadConfig()
  assert.match(controller.getSnapshot().errors.config, /唯一|unique/i)
  await controller.saveConfig({ candidateHarnesses: [] }, 7, fixture.identity.entryId, fixture.identity.namespace)
  assert.equal(fixture.writes.length, 0)
  assert.equal(controller.getSnapshot().configSaved, false)
})

test('starting another search removes earlier results even when the new source fails', async t => {
  const { createController } = await helpers()
  const controller = createController(context(async (id, line) => {
    const { action } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tools') return reply('tools', { tools: [{ name: 'old-tool' }] })
    throw new Error('GitHub temporarily unavailable')
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  controller.attach('skills', { list: async () => nativeReply({ skills: [{ name: 'old-skill' }] }) })
  assert.equal(await controller.search('local', ''), true)
  assert.equal(controller.getSnapshot().search.skills.length, 1)
  const searching = controller.search('github', 'new-query')
  assert.equal(controller.getSnapshot().search, null, 'Old local results must disappear before the new request settles')
  await searching
  assert.equal(controller.getSnapshot().search, null)
  assert.match(controller.getSnapshot().errors.search, /GitHub temporarily unavailable/)
})

test('changing search source invalidates a late result without changing the working session', async t => {
  const { createController } = await helpers(), pending = deferred()
  const controller = createController(context(() => pending.promise))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const searching = controller.search('github', 'old-query')
  await turn()
  controller.clearSearch()
  pending.resolve(reply('github', { repositories: [{ repository: 'old/result' }] }))
  assert.equal(await searching, false)
  assert.equal(controller.getSnapshot().sessionId, 'a')
  assert.equal(controller.getSnapshot().search, null)
  assert.equal(controller.getSnapshot().busy.search, false)
})

test('missing native market endpoint is a specific unavailable fact, not a successful empty search', async t => {
  const { createController } = await helpers()
  const controller = createController(context(() => assert.fail('Market discovery must not call the core')))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  controller.attach('market', {
    listIndexes: async () => {
      throw new Error('client api: pluginMarket/listIndexes failed: transport failure for /api/pluginMarket/listIndexes: HTTP 404')
    },
  })
  await controller.search('market', 'legion')
  const found = controller.getSnapshot().search
  assert.equal(found?.kind, 'market')
  assert.equal(found?.unavailable, true)
  assert.equal(found?.reason, 'native_endpoint_unavailable')
  assert.equal(found?.unconfigured, undefined, 'A missing service does not prove an empty index configuration')
  assert.match(found?.diagnostic, /HTTP 404/)
  assert.equal(controller.getSnapshot().errors.search, null)
})

test('absent market service is actionable but other native failures are not mislabeled as absence', async t => {
  const { createController } = await helpers()
  const controller = createController(context(() => assert.fail('No core request')))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.search('market', 'legion')
  assert.equal(controller.getSnapshot().search?.reason, 'native_service_unavailable')
  for (const message of [
    'transport failure for /api/pluginMarket/listIndexes: HTTP 401',
    'transport failure for /api/pluginMarket/listIndexes: HTTP 503',
    'transport failure for /api/someOtherService/list: HTTP 404',
    'Connection timed out',
  ]) {
    controller.attach('market', { listIndexes: async () => { throw new Error(message) } })
    assert.equal(await controller.search('market', 'legion'), false)
    assert.equal(controller.getSnapshot().search, null)
    assert.equal(controller.getSnapshot().errors.search, message)
  }
  controller.attach('market', { listIndexes: async () => nativeReply([]) })
  assert.equal(await controller.search('market', 'legion'), true)
  assert.equal(controller.getSnapshot().search.unconfigured, true)
  assert.equal(controller.getSnapshot().search.unavailable, undefined)
})

const STATUS_REF = 'a'.repeat(32)
const ARTIFACT_ID = 'b'.repeat(32)
const artifactRow = extra => ({
  artifact_id: ARTIFACT_ID, name: 'fixture.txt', sha256: 'c'.repeat(64),
  bytes: 12, download_eligible: true, ...extra,
})

test('artifact presentation never converts missing or partial observations into a confirmed zero', async () => {
  const { artifactFacts } = await helpers()
  assert.equal(typeof artifactFacts, 'function')
  for (const value of [
    {}, { artifacts: null }, { artifacts: [], more_events: true },
    { artifacts: [], more_events: false, truncated: true },
    { artifacts: [], more_events: false, projection: { truncated_fields: ['$.artifacts'] } },
  ]) assert.equal(artifactFacts(value).complete, false)
  const empty = artifactFacts({ artifacts: [], more_events: false })
  assert.equal(empty.complete, true)
  assert.equal(empty.observed_count, 0)
  const unrelated = artifactFacts({
    artifacts: [artifactRow()], more_events: false, truncated: true,
    sections: { artifacts: { available: true, complete: true, observed_count: 1, shown_count: 1 } },
    projection: { truncated_fields: ['$.runtime.tasks[0].result.final'] },
  })
  assert.equal(unrelated.complete, true, 'Abbreviated runtime prose must not hide an intact artifact inventory')
  assert.equal(unrelated.items.length, 1)
})

test('status paging carries only read cursors and clears stale task facts on a failed read', async t => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(submissionContext(async (id, line) => {
    const body = JSON.parse(line.slice('/legion-panel '.length))
    calls.push({ id, ...body })
    if (calls.length === 2) throw new Error('status unavailable')
    return reply('status', { task_ref: STATUS_REF, artifacts: [] })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.taskStatus(STATUS_REF, { after_event_id: 21, after_message_id: 4, artifact_offset: 30,
    sessionId: 'foreign', token: 'not-authority' })
  assert.deepEqual(calls[0], { id: 'a', action: 'status',
    args: { task_ref: STATUS_REF, after_event_id: 21, after_message_id: 4, artifact_offset: 30 } })
  await controller.taskStatus('d'.repeat(32))
  assert.equal(controller.getSnapshot().taskStatus, null)
  assert.match(controller.getSnapshot().errors.taskStatus, /status unavailable/)
})

test('downloads reuse the native human command with an observed id, never a returned URL or new authority', async t => {
  const { createController } = await helpers()
  const calls = []
  const pending = deferred()
  const controller = createController(context(async (id, line, attachments) => {
    calls.push({ id, line, attachments })
    if (line.startsWith('/legion-panel ')) return reply('status', {
      task_ref: STATUS_REF, artifacts: [artifactRow({ url: 'https://untrusted.invalid/private' })],
    })
    return pending.promise
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.taskStatus(STATUS_REF)
  for (const [ref, id] of [
    ['f'.repeat(32), ARTIFACT_ID], [STATUS_REF, 'https://untrusted.invalid/private'],
    [STATUS_REF, '../file'], [STATUS_REF, 'e'.repeat(32)],
  ]) await controller.downloadArtifact(ref, id)
  assert.equal(calls.length, 1, 'Unknown, foreign or path-shaped identities do not become commands')
  const wait = controller.downloadArtifact(STATUS_REF, ARTIFACT_ID)
  await turn()
  assert.deepEqual(plain(calls[1]), {
    id: 'a', line: `/legion download ${STATUS_REF} ${ARTIFACT_ID}`, attachments: [],
  })
  controller.selectSession('b')
  pending.resolve(nativeReply({ result: { kind: 'success', text: 'Fixture download receipt for session a' } }))
  await wait
  assert.equal(controller.getSnapshot().download, null, 'A late download receipt must not enter another session')
})

// In-process projection, not browser acceptance. Keep React child slots/keys
// and model the native disabled/removal focus loss observed on DSH 50935.
async function statusPanel(t, status, { taskCount = 1, reducedMotion = false, membersError,
  locale = 'zh', readTask = true, memberRows = [], memberReply, modelCatalog, configValue, flow, taskReply } = {}) {
  let definition, Page, cursor = 0
  const label = (zh, en) => locale === 'zh' ? zh : en
  const slots = [], cleanups = [], calls = [], effects = [], focusCalls = [], scrollCalls = []
  const shapes = new WeakMap(), targets = new WeakMap(), types = new Map(), refs = new Set()
  const body = { tagName: 'BODY', isConnected: true }, document = { activeElement: body }
  const hostKeys = []
  let mounted = new Map()
  const shapeOf = value => Array.isArray(value) ? value.map(shapeOf)
    : value && typeof value === 'object' ? shapes.get(value) : null
  const React = {
    Fragment: Symbol('fragment'),
    createElement(type, props, ...children) {
      const { key, ...rest } = props || {}
      const all = { ...rest, children: children.flat(Infinity).filter(value => value != null && value !== false) }
      const node = typeof type === 'function' ? type(all) : { type, props: all }
      if (node && typeof node === 'object') shapes.set(node, typeof type === 'function'
        ? { type, key, rendered: shapes.get(node) }
        : { type, key, node, children: children.map(shapeOf) })
      return node
    },
    useSyncExternalStore(_subscribe, snapshot) { return snapshot() },
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value }]
    },
    useRef(initial) { return React.useState(() => ({ current: initial }))[0] },
    useEffect(effect, deps) {
      const index = cursor++
      const previous = slots[index]
      if (!previous || !deps || deps.some((value, i) => !Object.is(value, previous.deps?.[i]))) {
        effects.push(() => {
          previous?.cleanup?.()
          slots[index] = { deps, cleanup: effect() }
        })
      }
    },
  }
  const realm = createContext({ window: {
    __ModuleLoader__: { load(value) { definition = value } },
    localStorage: memoryStorage(),
    matchMedia(query) {
      assert.equal(query, '(prefers-reduced-motion: reduce)')
      return { matches: reducedMotion }
    },
  },
    URL, AbortController, DOMException, TextEncoder, console, crypto: webcrypto })
  runInContext(await readFile(new URL('../client/index.js', import.meta.url), 'utf8'), realm)
  const plugin = definition.factory(name => {
    assert.equal(name, 'react')
    return React
  })
  const ctx = {
    ...context(async (id, line, attachments) => {
      calls.push({ id, line, attachments })
      if (line.startsWith('/legion download ')) {
        return nativeReply({ result: { kind: 'success', text: '下载回执：SHA-256 已核对；不是体验验收。' } })
      }
      const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
      if (action === 'runtime' && membersError) throw new Error(membersError)
      if (action === 'runtime' && memberReply) return reply(action,
        typeof memberReply === 'function' ? await memberReply(id, args) : memberReply)
      if (action.startsWith('flow_')) return reply(action, typeof flow === 'function' ? await flow(id, action, args) : flow)
      if (action === 'tasks') {
        if (taskReply) return reply(action, await taskReply(id, args))
        const offset = args.offset ?? 0, limit = args.limit ?? 30
        const tasks = Array.from({ length: taskCount }, (_, index) => ({
          task_ref: index ? index.toString(16).padStart(32, '0') : STATUS_REF, key: `synthetic-task-${index}`,
        })).slice(offset, offset + limit)
        const next = offset + tasks.length < taskCount ? offset + tasks.length : null
        return reply(action, { tasks, connection_id: 'a'.repeat(64), task_page: {
          available: true, total: taskCount, offset, shown_count: tasks.length, next_offset: next,
          snapshot: 'c'.repeat(64), source_complete: true, complete: offset === 0 && next === null,
        } })
      }
      return reply(action, action === 'status' ? typeof status === 'function' ? await status(id, args) : status
        : { runtime: { harnesses: memberRows } })
    }),
    locale: { resolveText(value) { return value[locale] } },
    layout: { selectPanel() {} },
    on() {},
    effect(work) { cleanups.push(work()) },
    inject(dependencies, callback) {
      if (dependencies[0] === 'slots' || (modelCatalog && dependencies[0] === 'remote.session')
        || (configValue !== undefined && dependencies[0] === 'configForms')) callback(ctx)
    },
    slots: {
      inject(_slot, callback) { callback() },
      register(options, component) { if (options.key === 'legion') Page = component },
    },
  }
  if (modelCatalog) ctx.remote.session = { modelCatalog: async () => nativeReply(modelCatalog) }
  if (configValue !== undefined) {
    const config = mappedConfiguration()
    config.form.getSnapshot = () => ({ status: 'ready', writable: true, revision: 7, value: configValue })
    ctx.configForms = config.owner.forms
    ctx.remote.pluginInventory = config.owner.inventory
    ctx.remote.pluginManager = config.owner.manager
    ctx.remote.settings = { async describe() {
      return nativeReply({ writable: true, namespaces: [{ ns: config.identity.namespace, value: configValue, revision: 7 }] })
    } }
  }
  ctx.sessions.refresh = async () => {}
  plugin.apply(ctx)
  t.after(() => {
    for (const slot of slots) slot?.cleanup?.()
    for (const cleanup of cleanups) cleanup?.()
  })
  const render = () => {
    cursor = 0
    const tree = Page()
    const next = new Map()
    for (const ref of refs) ref.current = null
    const commit = (shape, path, parent) => {
      if (!shape) return
      if (Array.isArray(shape)) {
        shape.forEach((child, index) => commit(child,
          `${path}/${child?.key == null ? `i${index}` : `k${child.key}`}`, parent))
        return
      }
      if (!types.has(shape.type)) types.set(shape.type, types.size)
      path += `:t${types.get(shape.type)}`
      if ('rendered' in shape) return commit(shape.rendered, `${path}/rendered`, parent)
      const target = mounted.get(path) || {
        tagName: typeof shape.type === 'string' ? shape.type.toUpperCase() : 'FRAGMENT',
        focus(options = {}) {
          if (!this.isConnected || this.disabled) return
          document.activeElement = this
          focusCalls.push({ id: this.element.props.id, options: plain(options) })
        },
        scrollIntoView(options) { scrollCalls.push({ id: this.element.props.id, options: plain(options) }) },
      }
      Object.assign(target, { element: shape.node, parent, isConnected: true, disabled: shape.node.props.disabled === true })
      next.set(path, target)
      targets.set(shape.node, target)
      if (target.disabled && document.activeElement === target) document.activeElement = body
      const ref = shape.node.props.ref
      if (ref && typeof ref === 'object') { refs.add(ref); ref.current = target }
      commit(shape.children, path, target)
    }
    commit(shapes.get(tree), 'root', null)
    for (const [path, target] of mounted) if (!next.has(path)) {
      target.isConnected = false
      if (document.activeElement === target) document.activeElement = body
    }
    mounted = next
    // Refs and removal/disabled effects precede passive React effects.
    for (const effect of effects.splice(0)) effect()
    return tree
  }
  const nodes = node => !node || typeof node !== 'object' ? []
    : [node, ...node.props.children.flatMap(nodes)]
  const text = node => node == null || node === false ? '' : typeof node !== 'object'
    ? String(node) : node.props.children.map(text).join('')
  const find = (type, label) => nodes(render()).find(node => node.type === type
    && (node.props['aria-label'] === label || text(node) === label))
  const fire = (target, type, extra = {}) => {
    const event = { target, ...extra, defaultPrevented: false, propagationStopped: false,
      preventDefault() { this.defaultPrevented = true },
      stopPropagation() { this.propagationStopped = true } }
    if (!target.isConnected || (type === 'Click' && target.disabled)) return event
    for (let current = target; current; current = current.parent) {
      event.currentTarget = current
      current.element.props[`on${type}`]?.(event)
      if (event.propagationStopped) break
    }
    if (type.startsWith('Key') && !event.propagationStopped) hostKeys.push({ type, key: event.key })
    render()
    return event
  }
  const press = (target, key, repeat = false) => {
    const down = fire(target, 'KeyDown', { key, repeat })
    if (!down.defaultPrevented && key === 'Enter') fire(target, 'Click')
    const up = fire(target, 'KeyUp', { key, repeat })
    if (!down.defaultPrevented && !up.defaultPrevented && key === ' ') fire(target, 'Click')
    return { down, up }
  }
  find('select', label('当前工作会话', 'Working session')).props.onChange({ target: { value: 'a' } })
  await turn()
  if (readTask) {
    find('button', label('战队任务', 'Team tasks')).props.onClick()
    await turn()
    find('button', label('读取实时进度', 'Read live progress')).props.onClick()
    await turn()
  }
  return { render, nodes, text, find, calls, focusCalls, scrollCalls, document, hostKeys, fire, press,
    dom: node => targets.get(node),
    userFocus(target) { assert.ok(target.isConnected && !target.disabled); document.activeElement = target } }
}

// Same metadata shape as the observed mixed inventory; no private IDs, files or HTTP.
const legacyFileObservation = '文件存在、非空且内容哈希已计算'
const savedFileObservation = '安全检查及导出字节哈希一致；非产品验收'
const mixedArtifactRecords = () => [
  artifactRow({ artifact_id: '1'.repeat(32), service_verification: legacyFileObservation }),
  artifactRow({ artifact_id: '2'.repeat(32), name: 'fixture.html', sha256: 'd'.repeat(64),
    service_verification: legacyFileObservation }),
  artifactRow({ artifact_id: '3'.repeat(32), kind: 'text', export_snapshot_id: 'e'.repeat(64),
    service_verification: savedFileObservation }),
  artifactRow({ artifact_id: '4'.repeat(32), name: 'fixture.html', sha256: 'd'.repeat(64),
    kind: 'web', export_snapshot_id: 'e'.repeat(64), service_verification: savedFileObservation }),
]
async function projectArtifactRecords(artifacts, after = 0) {
  const result = await executeWorkbenchCommand({}, {
    clean: value => value, redactText: value => value,
    async status() {
      return { task_ref: STATUS_REF, artifacts, runtime: { tasks: [] },
        events: [], more_events: false, next_event_id: 68 }
    },
  }, {}, { agent: { id: 'a' }, attachments: [], signal: new AbortController().signal,
    rawInput: JSON.stringify({ action: 'status', args: { task_ref: STATUS_REF, after_event_id: after } }) })
  assert.equal(result.kind, 'success')
  return JSON.parse(result.text).value
}

test('team entry offers companions, collaboration and automatic growth without a second activation ceremony', async t => {
  const panel = await statusPanel(t, {}, { readTask: false })
  assert.ok(panel.text(panel.render()).includes('选择谁与你并肩作战'))
  assert.ok(panel.text(panel.render()).includes('DSH · 你的主场'))
  panel.find('button', '下一步：协作方式').props.onClick()
  const collaboration = panel.text(panel.render())
  assert.ok(collaboration.includes('自主组织'))
  assert.ok(collaboration.includes('指定带队者'))
  assert.ok(collaboration.includes('我的协作要求'))
  panel.find('button', '下一步：自动变强').props.onClick()
  const evolution = panel.render()
  const toggle = panel.nodes(evolution).find(node => node.type === 'input' && node.props.type === 'checkbox')
  assert.equal(toggle.props.checked, true, 'Improvement is on by default for first setup')
  assert.ok(panel.find('button', '组建战队并自动开工'))
  assert.ok(panel.text(evolution).includes('每周'))
  assert.ok(!panel.text(evolution).includes('是否开启进化'))
})

test('native remote.session model availability is actually wired to the setup page', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, modelCatalog: {
    default: { provider: 'missing', model: 'missing' }, groups: [], failures: [],
  } })
  panel.find('button', '3 · 自动变强').props.onClick()
  await turn()
  assert.ok(panel.text(panel.render()).includes('当前会话还没有可用模型'))
})

test('team setup presents execution companions, not retired engines as selectable teammates', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, memberRows: [
    { key: 'retired-source', display_name: 'Retired engine', entry_points: { execution: { selectable: false } } },
    { key: 'actual-companion', display_name: 'Companion', entry_points: { execution: { selectable: true } } },
  ] })
  const inputs = panel.nodes(panel.render()).filter(node => node.type === 'input' && node.props.type === 'checkbox')
  assert.deepEqual(inputs.map(node => node.props['aria-label']), ['选择成员 Companion'])
  assert.ok(panel.text(panel.render()).includes('工具来源'))
})

const flowProfile = {
  name: '默认战队', goal: '当前默认目标', collaboration: 'auto', leader: '', instructions: '',
  evolutionEnabled: false, cadence: 'weekly',
}
async function settleFlowPanel(panel) {
  for (let i = 0; i < 5; i++) { panel.render(); await turn() }
}

test('team overview separates the native author model from cached selected-core configuration facts', async t => {
  const panel = await statusPanel(t, {}, { readTask: false,
    configValue: { teamProfile: flowProfile, candidateHarnesses: ['codex'] },
    modelCatalog: { default: { provider: 'fixture', model: 'author-model' },
      groups: [{ id: 'fixture', models: [{ id: 'author-model' }] }], failures: [] },
    memberReply: { runtime: { harnesses: [], stale: true },
      selected: { mode: 'configured', total: 1, shown_count: 1, complete: true, items: [
        { key: 'codex', display_name: 'Codex', observed: true,
          model: { configured: false, configuration_error: 'explicit_responses_not_configured' },
          readiness: { verified: false, historical_verified: true },
          entry_points: { execution: { selectable: true } } },
      ] } },
    flow: { code: 'ok', catalogVerified: true, enabled: false, schedules: [] },
  })
  await settleFlowPanel(panel)
  const content = panel.text(panel.render())
  assert.match(content, /DSH.*作者/)
  assert.match(content, /author-model/)
  assert.match(content, /Codex/)
  assert.match(content, /核心回报.*模型未配置/)
  assert.match(content, /explicit_responses_not_configured/)
  assert.match(content, /缓存/)
  assert.match(content, /历史.*不代表.*当前/)
  assert.ok(panel.find('button', '刷新战队状态'))
  assert.ok(panel.find('button', '打开工作会话，看实际结果'))
  assert.equal(panel.calls.filter(call => /"action":"(?:submit|flow_run|flow_start)"/.test(call.line)).length, 0,
    'Reading team facts must not start charged work or schedules')
})

test('a missing selected member and an untyped catalog row stay visible as unknown, not absent or tool-only', async t => {
  const panel = await statusPanel(t, {}, { readTask: false,
    configValue: { teamProfile: flowProfile, candidateHarnesses: ['not-observed'] },
    memberReply: { runtime: { harnesses: [{ key: 'untyped', display_name: 'Unknown kind' }], public_catalog_only: true },
      selected: { mode: 'configured', total: 1, shown_count: 1, complete: true,
        items: [{ key: 'not-observed', observed: false }] } },
    flow: { code: 'ok', catalogVerified: true, enabled: false, schedules: [] },
  })
  await settleFlowPanel(panel)
  assert.match(panel.text(panel.render()), /not-observed/)
  assert.match(panel.text(panel.render()), /本次未观察到/)
  assert.match(panel.text(panel.render()), /公开目录/)
  panel.find('button', '调整战队').props.onClick()
  const content = panel.text(panel.render())
  assert.match(content, /Unknown kind/)
  assert.match(content, /执行身份未知/)
  assert.doesNotMatch(content, /另有 1 个工具来源/)
  const checkbox = panel.nodes(panel.render()).find(node =>
    node.type === 'input' && node.props['aria-label'] === '选择成员 Unknown kind')
  assert.equal(checkbox.props.disabled, true, 'Unknown selectability is visible but never grants selection')
})

test('member page controls request explicit offsets without concatenating unrelated snapshots', async t => {
  const panel = await statusPanel(t, {}, { readTask: false,
    memberReply: (_id, args) => ({
      runtime: { harnesses: [{ key: `member-${args.offset || 0}`, entry_points: { execution: { selectable: true } } }] },
      member_page: { offset: args.offset || 0, limit: 12, total: 25, shown_count: 1,
        next_offset: (args.offset || 0) < 24 ? (args.offset || 0) + 12 : null, complete: false },
    }),
  })
  await settleFlowPanel(panel)
  panel.find('button', '下一页成员').props.onClick()
  await settleFlowPanel(panel)
  const content = panel.text(panel.render())
  assert.match(content, /member-12/)
  assert.doesNotMatch(content, /member-0(?:\D|$)/)
  assert.ok(panel.calls.some(call => /"offset":12/.test(call.line)))
})

test('short member pages offer an honest return-to-first action rather than guessing a previous offset', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, memberReply: (_id, args) => ({
    runtime: { harnesses: [{ key: `member-${args.offset || 0}`, entry_points: { execution: { selectable: true } } }] },
    member_page: { offset: args.offset || 0, limit: 12, total: 9, shown_count: 1,
      next_offset: args.offset === 3 ? 7 : args.offset === 7 ? null : 3, complete: false },
  }) })
  await settleFlowPanel(panel)
  for (let i = 0; i < 2; i++) {
    panel.find('button', '下一页成员').props.onClick()
    await settleFlowPanel(panel)
  }
  assert.match(panel.text(panel.render()), /member-7/)
  panel.find('button', '返回第一页成员').props.onClick()
  await settleFlowPanel(panel)
  assert.match(panel.text(panel.render()), /member-0/)
  assert.equal(panel.find('button', '上一页成员'), undefined)
})

test('nested model errors remain text and historical verification is never assigned to the current configuration', async t => {
  const row = { key: 'codex', observed: true, display_name: { unexpected: 'name' },
    model: { configured: false, display_name: { wrong: true }, configuration_error: { code: 'nested-provider-error' } },
    readiness: { verified: true }, entry_points: { execution: { selectable: true } } }
  const panel = await statusPanel(t, {}, { readTask: false,
    configValue: { teamProfile: flowProfile, candidateHarnesses: ['codex'] },
    memberReply: { runtime: { harnesses: [row], stale: true },
      selected: { items: [row], complete: true } },
    flow: { catalogVerified: true, schedules: [] },
  })
  await settleFlowPanel(panel)
  for (const node of panel.nodes(panel.render()).filter(node => ['code', 'h3'].includes(node.type))) {
    for (const child of node.props.children) {
      assert.ok(typeof child !== 'object' || child?.type,
        'React cannot render an arbitrary service object as a child')
    }
  }
  const content = panel.text(panel.render())
  assert.match(content, /nested-provider-error/)
  assert.match(content, /是否对应当前配置/)
  assert.doesNotMatch(content, /此配置的执行验证/)
})

test('malformed member rows and missing identities do not crash the page or create a selectable companion', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, memberRows: [
    null, 'broken-row', { display_name: 'Missing identity', entry_points: { execution: { selectable: true } } },
  ] })
  const content = panel.text(panel.render())
  assert.match(content, /记录缺少身份|记录无法展示/)
  const checkbox = panel.nodes(panel.render()).find(node =>
    node.type === 'input' && node.props['aria-label'] === '选择成员 Missing identity')
  assert.equal(checkbox.props.disabled, true)
})

test('a member response from an older connection or candidate configuration cannot populate the new team', async t => {
  const { createController } = await helpers()
  const request = deferred()
  const controller = createController(context(() => request.promise))
  t.after(() => controller.dispose())
  const fixture = mappedConfiguration()
  let notify, value = { baseUrl: 'https://one.example', candidateHarnesses: ['first'] }
  fixture.form.getSnapshot = () => ({ status: 'ready', value, revision: 7, writable: true })
  fixture.form.subscribe = listener => { notify = listener; return () => {} }
  controller.attach('configuration', fixture.owner)
  controller.selectSession('a')
  await controller.loadConfig()
  const loading = controller.loadMembers()
  await turn()
  value = { baseUrl: 'https://two.example', candidateHarnesses: ['second'] }
  notify()
  request.resolve(reply('runtime', { selected: { items: [{ key: 'first', observed: true }] } }))
  await loading
  assert.equal(controller.getSnapshot().members, null,
    'The old response belongs to the old connection and candidate snapshot')
})

test('flow snapshots: global defaults never label an older session plan as having improvement switched off', async t => {
  const calls = []
  let matches = false
  const panel = await statusPanel(t, {}, { readTask: false, configValue: { teamProfile: flowProfile }, flow: async (id, action) => {
    calls.push({ id, action })
    if (action === 'flow_start') matches = true
    return { code: 'ok', catalogVerified: true, configured: true, enabled: true,
      workflow: { initialObserved: true, recurringActive: true, configurationMatches: matches },
      schedules: [{ id: 'old-plan', title: '旧计划', role: 'recurring', status: 'active', matchesConfiguration: matches }],
      observations: [] }
  } })
  await settleFlowPanel(panel)
  const content = panel.text(panel.render())
  assert.match(content, /默认.*自主改进.*关闭/)
  assert.doesNotMatch(content, /自主改进已关闭/)
  assert.match(content, /当前会话.*旧配置|当前会话.*未同步/)
  assert.match(content, /只(?:会)?更新当前|仅.*当前.*会话/)
  assert.match(content, /其他会话/)
  const sync = panel.nodes(panel.render()).find(node => node.type === 'button' && /同步当前会话计划/.test(panel.text(node)))
  assert.ok(sync, 'Use the existing flow_start route for an explicit current-session sync')
  sync.props.onClick()
  await settleFlowPanel(panel)
  assert.deepEqual(calls.filter(row => row.action === 'flow_start'), [{ id: 'a', action: 'flow_start' }])
  assert.doesNotMatch(panel.text(panel.render()), /当前会话仍使用旧配置/)
})

test('flow snapshots: weekly defaults with only an initial schedule explicitly report the missing recurring plan', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, configValue: { teamProfile: { ...flowProfile, evolutionEnabled: true } }, flow: {
    code: 'ok', catalogVerified: true, configured: true, enabled: true, partial: false,
    workflow: { initialObserved: true, recurringActive: false, configurationMatches: true },
    schedules: [{ id: 'initial-only', role: 'initial', status: 'active', matchesConfiguration: true }], observations: [],
  } })
  await settleFlowPanel(panel)
  const content = panel.text(panel.render())
  assert.ok(content.includes('首次已安排，未观察到周期任务'))
  assert.ok(!content.includes('自动工作已安排'))
})

test('flow snapshots: an active old plan remains pausable when teamProfile is not configured', async t => {
  const panel = await statusPanel(t, {}, { readTask: false, configValue: {}, flow: {
    code: 'ok', catalogVerified: true, configured: false, enabled: true,
    workflow: { initialObserved: true, recurringActive: true, configurationMatches: null },
    schedules: [{ id: 'old-active', role: 'recurring', status: 'active', matchesConfiguration: null }], observations: [],
  } })
  await settleFlowPanel(panel)
  const pause = panel.find('button', '暂停后续自动工作')
  assert.ok(pause, 'Missing global defaults must not hide existing native schedules or the pause action')
  assert.equal(pause.props.disabled, false)
  assert.match(panel.text(panel.render()), /默认.*(?:未配置|尚未设置)/)
  pause.props.onClick()
  await settleFlowPanel(panel)
  assert.equal(panel.calls.filter(call => call.line.includes('"action":"flow_pause"')).length, 1)
})

for (const example of [
  { locale: 'zh', count: '本次已观察到的成果记录：4', saved: '已保存的交付版本',
    legacy: '工作目录文件记录（可能随续作变化）', unknown: '来源未确认', download: '下载文件',
    refresh: '刷新原任务', incomplete: '成果清单未完整取得', stale: /上次.*非最新|上次.*未更新/ },
  { locale: 'en', count: 'Observed artifact records: 4', saved: 'Saved delivery version',
    legacy: 'Working-directory file record (may change as work continues)', unknown: 'Source unconfirmed',
    download: 'Download file', refresh: 'Refresh original task', incomplete: 'Artifact inventory is incomplete',
    stale: /[Pp]revious.*not current|[Pp]revious.*not updated/ },
]) {
  test(`artifact provenance distinguishes mixed records without merging or changing downloads: ${example.locale}`, async t => {
    const items = mixedArtifactRecords(), value = await projectArtifactRecords(items)
    assert.deepEqual(value.artifacts, items, 'Use metadata retained by the actual public projection')
    const panel = await statusPanel(t, value, { locale: example.locale })
    const tree = panel.render(), content = panel.text(tree)
    const observations = panel.nodes(tree).find(node => node.props.id === 'legion-task-status')
    const articles = panel.nodes(observations).filter(node => node.type === 'article' && node.props.className === 'legion-task')
    assert.equal(articles.length, 4, 'Same names and hashes do not merge distinct record IDs')
    for (let index = 0; index < items.length; index++) {
      const text = panel.text(articles[index]), item = items[index]
      assert.ok(text.includes(index < 2 ? example.legacy : example.saved), text)
      assert.ok(text.includes(item.artifact_id))
      assert.ok(text.includes(item.service_verification), 'Retain the service observation without upgrading it to acceptance')
      if (item.export_snapshot_id) {
        assert.ok(text.includes(item.export_snapshot_id))
        assert.ok(text.includes(item.kind))
      }
      panel.nodes(articles[index]).find(node => node.type === 'button' && panel.text(node) === example.download).props.onClick()
      await turn()
    }
    assert.ok(content.includes(example.count), content)
    assert.doesNotMatch(content, /正式导出记录：|Observed export records:/)
    assert.deepEqual(panel.calls.filter(call => call.line.startsWith('/legion download ')).map(call => call.line),
      items.map(item => `/legion download ${STATUS_REF} ${item.artifact_id}`))
  })

  test(`artifact narrow layout wraps intact SHA-256 in legacy and saved records: ${example.locale}`, async t => {
    const items = mixedArtifactRecords()
    const panel = await statusPanel(t, await projectArtifactRecords(items), { locale: example.locale })
    const tree = panel.render()
    const observations = panel.nodes(tree).find(node => node.props.id === 'legion-task-status')
    const articles = panel.nodes(observations).filter(node => node.type === 'article' && node.props.className === 'legion-task')
    assert.equal(articles.length, items.length)
    for (let index = 0; index < items.length; index++) {
      const article = articles[index], item = items[index]
      const sha = panel.nodes(article).find(node => node.type === 'p' && panel.text(node).startsWith('SHA-256: '))
      assert.ok(sha)
      assert.equal(item.sha256.length, 64)
      assert.equal(panel.text(sha), `SHA-256: ${item.sha256}`, 'Wrap the full hash without truncation or inserted characters')
      assert.equal(panel.dom(sha).parent, panel.dom(article), 'The wrapping rule must cover the actual task paragraph')
      if (item.export_snapshot_id) {
        assert.ok(panel.nodes(article).some(node => node.type === 'code' && panel.text(node) === item.export_snapshot_id),
          'Keep the already-wrapping saved version ID intact')
      }
    }
    // The fixture has no pixel layout engine. Check the rendered base stylesheet:
    // these rules apply at 390px too; document width alone cannot detect inner overflow.
    const css = panel.text(panel.nodes(tree).find(node => node.type === 'style'))
    const baseRules = [...css.split('@media')[0].matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    const declarations = selector => baseRules
      .filter(([, selectors]) => selectors.split(',').some(value => value.trim() === selector))
      .map(([, , body]) => body).join(';')
    const wraps = /(?:^|;)\s*overflow-wrap\s*:\s*anywhere\s*(?:;|$)/
    assert.match(declarations('.legion-panel code'), wraps, 'Version IDs retain their existing wrap rule')
    assert.match(declarations('.legion-task p'), wraps,
      'Unbroken SHA-256 text in task paragraphs needs wrapping, not only paragraphs in member cards')
    assert.doesNotMatch(`${declarations('.legion-task')};${declarations('.legion-task p')}`,
      /(?:^|;)\s*(?:overflow(?:-x)?\s*:\s*(?:hidden|clip)|white-space\s*:\s*nowrap|text-overflow\s*:\s*ellipsis)(?:;|$)/,
      'Do not conceal long values or force horizontal scrolling instead of wrapping')
  })

  test(`artifact provenance keeps absent or unfamiliar source metadata unconfirmed and eligibility independent: ${example.locale}`, async t => {
    const items = [
      artifactRow(),
      artifactRow({ artifact_id: '1'.repeat(32), kind: 'web' }),
      artifactRow({ artifact_id: '2'.repeat(32), service_verification: 'synthetic observation, not a source marker' }),
      artifactRow({ artifact_id: '3'.repeat(32), export_snapshot_id: '' }),
      artifactRow({ artifact_id: '4'.repeat(32), export_snapshot_id: ' ', service_verification: legacyFileObservation }),
      artifactRow({ artifact_id: '6'.repeat(32), kind: 'web', service_verification: savedFileObservation }),
      artifactRow({ artifact_id: '5'.repeat(32), export_snapshot_id: 'f'.repeat(64),
        download_eligible: false, download_reason: 'Artifact has conflicting SHA-256 hashes' }),
    ]
    const panel = await statusPanel(t, await projectArtifactRecords(items), { locale: example.locale })
    const observations = panel.nodes(panel.render()).find(node => node.props.id === 'legion-task-status')
    const articles = panel.nodes(observations).filter(node => node.type === 'article' && node.props.className === 'legion-task')
    assert.equal(articles.length, items.length)
    for (const article of articles.slice(0, -1)) {
      const text = panel.text(article)
      assert.ok(text.includes(example.unknown), text)
      assert.ok(!text.includes(example.saved) && !text.includes(example.legacy))
      assert.equal(panel.nodes(article).filter(node => node.type === 'button').length, 1,
        'Source labels do not add a new download eligibility gate')
    }
    const saved = articles.at(-1)
    assert.ok(panel.text(saved).includes(example.saved))
    assert.match(panel.text(saved), /Artifact has conflicting SHA-256 hashes/)
    assert.equal(panel.nodes(saved).filter(node => node.type === 'button').length, 0,
      'A saved version ID must not relax an existing integrity refusal')
  })

  test(`artifact provenance preserves incomplete last-event-page facts through a failed refresh: ${example.locale}`, async t => {
    const value = await projectArtifactRecords(mixedArtifactRecords(), 62)
    const { artifactFacts } = await helpers()
    assert.equal(value.more_events, false)
    assert.equal(value.sections.artifacts.complete, false, 'A partial event history is not a complete inventory receipt')
    assert.equal(artifactFacts(value).complete, false)
    const pending = deferred()
    let reads = 0
    const panel = await statusPanel(t, () => ++reads === 2 ? pending.promise : value, { locale: example.locale })
    assert.ok(panel.text(panel.render()).includes(example.incomplete))
    panel.find('button', example.refresh).props.onClick()
    await turn()
    for (const failed of [false, true]) {
      if (failed) { pending.reject(new Error('synthetic last-page refresh failure')); await turn() }
      const tree = panel.render(), text = panel.text(tree)
      assert.ok(text.includes(example.incomplete), 'Do not turn complete=false into an old-count label')
      assert.match(text, example.stale)
      assert.doesNotMatch(text, /本次已观察到的成果记录|上次读取的成果记录|(?:Previously observed|Observed) artifact records:/)
      const buttons = panel.nodes(tree).filter(node => node.type === 'button' && panel.text(node) === example.download)
      assert.equal(buttons.length, 4)
      for (const button of buttons) {
        assert.equal(button.props.disabled, true)
        button.props.onClick()
      }
      await turn()
      assert.equal(panel.calls.filter(call => call.line.startsWith('/legion download ')).length, 0)
      if (failed) assert.ok(panel.nodes(tree).some(node =>
        node.props.role === 'alert' && panel.text(node).includes('synthetic last-page refresh failure')))
    }
    assert.equal(value.sections.artifacts.complete, false)
  })
}

test('task panel renders missing exports as incomplete, not zero, even for legacy projected receipts', async t => {
  const panel = await statusPanel(t, {
    task_ref: STATUS_REF, team_id: 'fixture-team', connection: 'connected',
    runtime: { tasks: [{ task_id: 'author', status: 'done' }] },
    truncated: true, projection: { truncated_fields: ['$.artifacts'] },
  })
  const content = panel.text(panel.render())
  assert.match(content, /成果清单.*未完整|成果清单.*未取得/)
  assert.doesNotMatch(content, /正式成果：0|导出记录：0/)
})

test('task panel separates export reasons and hash versions and provides the real native download button', async t => {
  const panel = await statusPanel(t, {
    task_ref: STATUS_REF, team_id: 'fixture-team', connection: 'connected',
    runtime: { tasks: [{ task_id: 'author', status: 'done', result: { final: 'Author report, not acceptance' } }] },
    artifacts: [artifactRow(), artifactRow({ artifact_id: 'd'.repeat(32), sha256: 'e'.repeat(64),
      download_eligible: false, download_reason: 'Artifact has conflicting SHA-256 hashes' })],
    export_observations: [{ source_path: '$.events[2].data', event_id: 9,
      name: 'fixture.tgz', reason: 'artifact_archive_unsupported', downloadable: false, file_preserved: true }],
    next_event_id: 12, more_events: true, truncated: true,
    sections: {
      artifacts: { available: true, complete: false, observed_count: 2, shown_count: 2, offset: 0, next_offset: null },
      events: { available: true, after_event_id: 0, has_more: true, next_after_event_id: 9 },
    },
  })
  const tree = panel.render(), content = panel.text(tree)
  assert.match(content, /哈希|SHA-256/)
  assert.match(content, /历史|history/)
  assert.match(content, /artifact_archive_unsupported/)
  assert.match(content, /Artifact has conflicting SHA-256 hashes/)
  assert.match(content, /服务事件游标：12/)
  assert.match(content, /截断|省略/)
  const buttons = panel.nodes(tree).filter(node => node.type === 'button' && panel.text(node) === '下载文件')
  assert.equal(buttons.length, 1, 'Only eligible observed rows have an active download action')
  buttons[0].props.onClick()
  await turn()
  assert.equal(panel.calls.at(-1).line, `/legion download ${STATUS_REF} ${ARTIFACT_ID}`)
  assert.match(panel.text(panel.render()), /下载回执/)
  panel.find('button', '读取后续事件').props.onClick()
  await turn()
  const next = JSON.parse(panel.calls.at(-1).line.slice('/legion-panel '.length))
  assert.equal(next.args.after_event_id, 9, 'Do not jump to the upstream cursor and skip undisplayed events')
})

for (const reducedMotion of [false, true]) {
  test(`task progress focus follows an explicit read from a long list (reduced motion: ${reducedMotion})`, async t => {
    const pending = deferred()
    const panel = await statusPanel(t, () => pending.promise, { taskCount: 8, reducedMotion })
    const loading = panel.render()
    const firstTask = panel.nodes(loading).find(node => node.type === 'article' && panel.text(node).includes('synthetic-task-0'))
    assert.match(panel.text(firstTask), /正在读取/, 'The clicked task itself must show loading above the fold')
    assert.equal(panel.focusCalls.length, 0, 'Do not focus an empty result while the request is still pending')
    pending.resolve({ task_ref: STATUS_REF, runtime: { tasks: [] }, artifacts: [], more_events: false })
    await turn()
    const tree = panel.render()
    const region = panel.nodes(tree).find(node => node.props.id === 'legion-task-status')
    assert.equal(region?.props.tabIndex, -1)
    assert.equal(region.props['aria-busy'], false)
    assert.deepEqual(panel.focusCalls, [{ id: 'legion-task-status', options: { preventScroll: true } }])
    assert.deepEqual(panel.scrollCalls, [{
      id: 'legion-task-status', options: { block: 'start', behavior: reducedMotion ? 'instant' : 'smooth' },
    }])
    panel.render()
    assert.equal(panel.focusCalls.length, 1, 'A rerender must not steal focus again')
    const statusCall = panel.calls.find(call => call.line.includes('"action":"status"'))
    assert.deepEqual(JSON.parse(statusCall.line.slice('/legion-panel '.length)).args, { task_ref: STATUS_REF },
      'The focus request stays in the UI; it is not a Host argument or grant')
  })
}

test('task progress focus reveals the failed read instead of leaving an error below the long list', async t => {
  const pending = deferred()
  const panel = await statusPanel(t, () => pending.promise, { taskCount: 8, reducedMotion: true })
  pending.reject(new Error('synthetic status read failed'))
  await turn()
  const tree = panel.render()
  const region = panel.nodes(tree).find(node => node.props.id === 'legion-task-status')
  assert.ok(region, 'Failure needs the same focusable status destination as success')
  assert.ok(panel.nodes(region).some(node => node.props.role === 'alert' && panel.text(node).includes('synthetic status read failed')))
  assert.equal(region.props['aria-busy'], false)
  assert.equal(panel.focusCalls.length, 1)
  assert.equal(panel.scrollCalls.length, 1)
})

test('task progress focus does not repeat on artifact paging, event paging or refresh', async t => {
  const panel = await statusPanel(t, {
    task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: true,
    sections: {
      artifacts: { offset: 0, next_offset: 30 },
      events: { after_event_id: 0, has_more: true, next_after_event_id: 9 },
    },
  }, { taskCount: 8 })
  panel.render()
  assert.equal(panel.focusCalls.length, 1, 'The explicit task selection should focus once')
  for (const label of ['读取下一页成果记录', '读取后续事件', '刷新原任务', '刷新任务列表']) {
    panel.find('button', label).props.onClick()
    await turn()
    panel.render()
    assert.equal(panel.focusCalls.length, 1, `${label} must not take keyboard focus`)
    assert.equal(panel.scrollCalls.length, 1, `${label} must not move the viewport`)
  }
})

test('task progress focus drops a pending selection when the session changes, including switching back', async t => {
  const pending = deferred()
  const panel = await statusPanel(t, () => pending.promise, { taskCount: 8 })
  panel.find('select', '当前工作会话').props.onChange({ target: { value: 'b' } })
  await turn()
  pending.resolve({ task_ref: STATUS_REF, runtime: { tasks: [
    { task_id: 'author', status: 'done', final: 'OLD_SESSION_RESULT_MUST_NOT_REAPPEAR' },
  ] } })
  await turn()
  assert.doesNotMatch(panel.text(panel.render()), /OLD_SESSION_RESULT_MUST_NOT_REAPPEAR/)
  assert.equal(panel.focusCalls.length, 0)
  panel.find('select', '当前工作会话').props.onChange({ target: { value: 'a' } })
  await turn()
  assert.doesNotMatch(panel.text(panel.render()), /OLD_SESSION_RESULT_MUST_NOT_REAPPEAR/)
  assert.equal(panel.focusCalls.length, 0)
  assert.equal(panel.scrollCalls.length, 0)
})

test('task progress focus is not restored after leaving the task tab during a read', async t => {
  const pending = deferred()
  const panel = await statusPanel(t, () => pending.promise, { taskCount: 8 })
  panel.find('button', '找能力').props.onClick()
  pending.resolve({ task_ref: STATUS_REF, artifacts: [], more_events: false })
  await turn()
  panel.render()
  assert.equal(panel.focusCalls.length, 0)
  panel.find('button', '战队任务').props.onClick()
  await turn()
  panel.render()
  assert.equal(panel.focusCalls.length, 0, 'Reopening the tab must not replay an old selection')
  assert.equal(panel.scrollCalls.length, 0)
})

test('member read failure explains the connection and still-available native entry points without masking details or retrying', async t => {
  const panel = await statusPanel(t, { task_ref: STATUS_REF, artifacts: [] }, { membersError: 'fetch failed' })
  panel.find('button', '我的军团').props.onClick()
  const tree = panel.render(), content = panel.text(tree)
  assert.match(content, /暂时无法读取军团成员.*连接与配置.*地址.*服务/)
  assert.match(content, /找能力.*DSH.*能力包.*仍可用/)
  assert.ok(panel.nodes(tree).some(node => node.props.role === 'alert' && panel.text(node) === 'fetch failed'),
    'Keep the existing error details; do not replace a transport error with an identity diagnosis')
  assert.doesNotMatch(content, /身份错误|认证失败|unauthorized/i)
  assert.equal(panel.find('button', '找能力').props.disabled, undefined)
  assert.equal(panel.find('button', '连接与配置').props.disabled, undefined)
  await turn()
  panel.render()
  assert.equal(panel.calls.filter(call => call.line.includes('"action":"runtime"')).length, 1,
    'The explanation must not retry member discovery or mutate the connection')
})

test('export section explicitly marks clipped reasons incomplete even when all observation rows and artifacts are shown', async t => {
  const reason = 'synthetic_export_reason_prefix…[truncated]'
  const panel = await statusPanel(t, {
    task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: false, truncated: true,
    export_observations: [{ name: 'fixture.tgz', reason, source_path: '$.events[1].data' }],
    sections: {
      artifacts: { available: true, complete: true, observed_count: 1, shown_count: 1 },
      exports: { available: true, complete: false, observed_count: 1, shown_count: 1 },
    },
    projection: { truncated_fields: ['$.export_observations[0].reason'] },
  })
  const tree = panel.render()
  const observations = panel.nodes(tree).find(node => node.type === 'section' && node.props.children.some(child =>
    child?.type === 'h3' && panel.text(child) === '导出观察与拒绝原因'))
  assert.ok(observations)
  const children = observations.props.children
  const start = children.findIndex(node => node?.type === 'h3' && panel.text(node) === '导出观察与拒绝原因')
  const end = children.findIndex(node => node?.type === 'h3' && panel.text(node) === '事件与续读')
  const exportSection = children.slice(start + 1, end)
  assert.ok(exportSection.some(node => node?.type === 'p'
    && panel.text(node) === '以上仅为本页已取得的导出观察，尚不是完整原因清单。'),
  'The export section itself must disclose incomplete reason content, not just the generic truncation banner')
  assert.ok(exportSection.some(node => panel.text(node).includes(reason)), 'Keep the observed reason text as received')
  assert.match(panel.text(tree), /本次已观察到的成果记录：1/)
  assert.doesNotMatch(panel.text(tree), /成果清单未完整取得/,
    'Clipped export reasons must not change the independently complete artifact inventory')
})

for (const example of [
  {
    locale: 'zh', tab: '添加能力', heading: '安装现成 DSH 包', button: '安装 / 更新',
    explanation: '新包不会自动选择启用；更新保留已有开关，已启用包可能继续运行。若不希望更新后的代码运行，请先在下方停用对应能力包，再安装更新。',
  },
  {
    locale: 'en', tab: 'Add capabilities', heading: 'Install an existing DSH bundle', button: 'Install / update',
    explanation: 'New bundles are not automatically selected for activation. Updates preserve existing enablement, so enabled bundles may keep running. To prevent updated code from running, disable the bundle below before installing the update.',
  },
]) {
  test(`install copy distinguishes new bundles from updates without promising forced disablement (${example.locale})`, async t => {
    const panel = await statusPanel(t, undefined, { locale: example.locale, readTask: false })
    panel.find('button', example.tab).props.onClick()
    const section = panel.nodes(panel.render()).find(node => node.type === 'section' && node.props.children.some(child =>
      child?.type === 'h3' && panel.text(child) === example.heading))
    assert.ok(section, 'Inspect the actual native installation card, not unrelated page text')
    assert.ok(panel.nodes(section).some(node => node.type === 'button' && panel.text(node) === example.button),
      'The install button must not promise to disable an already-enabled bundle')
    assert.ok(section.props.children.some(node => node?.type === 'p' && panel.text(node) === example.explanation),
      'Explain new-bundle selection, retained update switches and how to disable before updating')
    assert.doesNotMatch(panel.text(section), /安装，暂不启用|Install disabled/)
    assert.equal(panel.calls.some(call => call.line.includes('"action":"status"')), false,
      'This copy regression does not replay task progress or focus')
  })
}

const focusStatus = (overrides = {}) => ({
  task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: true,
  sections: {
    artifacts: { available: true, complete: true, observed_count: 1, shown_count: 1, offset: 30, next_offset: 60 },
    events: { after_event_id: 0, has_more: true, next_after_event_id: 9 },
  },
  ...overrides,
})

for (const [label, key] of [
  ['刷新原任务', 'Enter'], ['返回成果第一页', ' '],
  ['读取下一页成果记录', 'Enter'], ['读取后续事件', ' '],
]) {
  for (const failure of [false, true]) {
    test(`task read focus keeps the same control through ${failure ? 'failure' : 'completion'}: ${label}`, async t => {
      const pending = deferred()
      let reads = 0
      const panel = await statusPanel(t, () => ++reads === 2 ? pending.promise : focusStatus())
      const original = panel.dom(panel.find('button', label))
      panel.userFocus(original)
      const focusCount = panel.focusCalls.length, scrollCount = panel.scrollCalls.length
      panel.press(original, key)
      await turn()
      panel.render()
      assert.equal(original.isConnected, true, 'Do not replace the triggering button')
      assert.equal(panel.document.activeElement, original, 'Busy must not remove the focused button from the focus order')
      assert.equal(original.disabled, false, 'Native disabled caused the real DSH focusout')
      assert.equal(original.element.props['aria-disabled'], true)
      assert.equal(panel.dom(panel.find('button', label)), original)
      assert.equal(panel.find('button', '下载文件').props.disabled, true, 'Download remains natively disabled')
      panel.press(original, key)
      panel.fire(original, 'Click')
      await turn()
      assert.equal(reads, 2, 'Busy keyboard and pointer activation must not issue another status request')
      if (failure) pending.reject(new Error('synthetic focus read rejected'))
      else pending.resolve(focusStatus({ artifacts: [], more_events: false,
        export_observations: [{ reason: 'Synthetic terminal page' }],
        sections: { artifacts: { offset: 0 }, events: { after_event_id: 9, has_more: false } } }))
      await turn()
      panel.render()
      assert.equal(panel.document.activeElement, original, 'Completion, failure or a final page must not strand focus on BODY')
      assert.equal(panel.dom(panel.find('button', label)), original, 'Page size changes must preserve control identity')
      assert.equal(original.isConnected, true)
      assert.equal(original.element.props['aria-disabled'], !failure && label !== '刷新原任务')
      assert.equal(panel.focusCalls.length, focusCount, 'Do not fix this by refocusing after completion')
      assert.equal(panel.scrollCalls.length, scrollCount, 'Do not force a viewport position')
      if (failure) {
        assert.match(panel.text(panel.render()), /synthetic focus read rejected/)
        assert.equal(panel.find('button', '下载文件').props.disabled, true)
      }
    })
  }
}

test('task read focus owns Enter and Space activation but leaves navigation keys and user focus alone', async t => {
  const pending = deferred()
  let reads = 0
  const panel = await statusPanel(t, () => ++reads === 2 ? pending.promise : focusStatus())
  const button = panel.dom(panel.find('button', '刷新原任务'))
  panel.userFocus(button)
  const ready = panel.press(button, 'Enter')
  assert.equal(ready.down.defaultPrevented, false, 'Use the native button click, not a second scripted activation')
  assert.equal(ready.down.propagationStopped, true, 'Button activation must not also enter Host page shortcuts')
  assert.equal(ready.up.propagationStopped, true)
  for (const key of ['Enter', ' ']) {
    const blocked = panel.press(button, key)
    assert.equal(blocked.down.defaultPrevented, true)
    assert.equal(blocked.up.defaultPrevented, true)
  }
  const tab = panel.fire(button, 'KeyDown', { key: 'Tab' })
  assert.equal(tab.defaultPrevented, false)
  assert.equal(tab.propagationStopped, false)
  const elsewhere = panel.dom(panel.find('select', '当前工作会话'))
  panel.userFocus(elsewhere)
  pending.resolve(focusStatus())
  await turn()
  panel.render()
  assert.equal(panel.document.activeElement, elsewhere, 'A completed read must respect an intervening focus move')
  panel.userFocus(button)
  const held = panel.press(button, 'Enter', true)
  assert.equal(held.down.defaultPrevented, true, 'A held key must not retrigger immediately after the first request finishes')
  await turn()
  assert.equal(reads, 2)
  assert.deepEqual(panel.hostKeys, [{ type: 'KeyDown', key: 'Tab' }])
  panel.press(button, ' ')
  await turn()
  assert.equal(reads, 3, 'A fresh Space press still activates exactly once')
})

for (const preloaded of [false, true]) {
  test(`task read reentry is blocked before invalidation, including old handlers and paging (preloaded: ${preloaded})`, async t => {
    const { createController } = await helpers()
    const pending = deferred(), duplicates = []
    let reads = 0
    const controller = createController(context(async () => {
      reads++
      return preloaded && reads === 1 ? reply('status', focusStatus()) : pending.promise
    }))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    if (preloaded) await controller.taskStatus(STATUS_REF)
    let reentered = false
    const unsubscribe = controller.subscribe(() => {
      if (reentered) return
      reentered = true
      duplicates.push(controller.taskStatus(STATUS_REF, { artifact_offset: 60 }))
    })
    const reading = controller.taskStatus(STATUS_REF)
    duplicates.push(controller.taskStatus(STATUS_REF), controller.taskStatus(STATUS_REF, { after_event_id: 9 }))
    await turn()
    unsubscribe()
    assert.equal(reads, preloaded ? 2 : 1, 'Duplicate reads must neither reset busy nor supersede the original result')
    assert.equal(controller.getSnapshot().busy.taskStatus, true)
    pending.resolve(reply('status', focusStatus()))
    assert.equal(await reading, true)
    assert.deepEqual(await Promise.all(duplicates), [false, false, false])
    assert.equal(controller.getSnapshot().taskStatusStale, false)
  })
}

test('task read reentry ownership is released on session or connection invalidation, not by an old settlement', async t => {
  const { createController } = await helpers()
  const requests = [deferred(), deferred(), deferred()]
  let reads = 0
  const controller = createController(context(async () => requests[reads++].promise))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const original = controller.taskStatus(STATUS_REF)
  await turn()
  controller.selectSession('b')
  controller.selectSession('a')
  const replacement = controller.taskStatus(STATUS_REF)
  await turn()
  requests[0].reject(new Error('old session refusal'))
  assert.equal(await original, false)
  const duplicate = controller.taskStatus(STATUS_REF)
  await turn()
  assert.equal(reads, 2, 'Old finally must not release the new read owner')
  assert.equal(await duplicate, false)
  const detach = controller.attach('configuration', {})
  detach()
  const reconnected = controller.taskStatus(STATUS_REF)
  await turn()
  requests[1].resolve(reply('status', focusStatus()))
  assert.equal(await replacement, false)
  assert.equal(controller.getSnapshot().taskStatus, null)
  requests[2].resolve(reply('status', focusStatus()))
  assert.equal(await reconnected, true)
  assert.equal(reads, 3)
})

for (const [kind, options] of [
  ['refresh', {}],
  ['artifact page', { artifact_offset: 30 }],
  ['event page', { after_event_id: 9, after_message_id: 4 }],
]) {
  test(`task refresh retains the same task during ${kind} without allowing stale downloads`, async t => {
    const { createController } = await helpers()
    const pending = deferred(), calls = [], snapshots = []
    const controller = createController(context(async (_id, line) => {
      calls.push(line)
      if (calls.length > 1) return pending.promise
      return reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: false })
    }))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    await controller.taskStatus(STATUS_REF)
    const original = controller.getSnapshot().taskStatus
    controller.subscribe(() => snapshots.push(controller.getSnapshot()))
    const reading = controller.taskStatus(STATUS_REF, options)
    await turn()
    assert.ok(snapshots.length > 0)
    assert.ok(snapshots.every(state => state.taskStatus === original),
      'A same-task read must never publish an empty result subtree, even transiently')
    assert.ok(snapshots.every(state => state.taskStatusStale === true),
      'Invalidate freshness atomically, before a subscriber can request a download')
    assert.equal(controller.getSnapshot().busy.taskStatus, true)
    assert.equal(await controller.downloadArtifact(STATUS_REF, ARTIFACT_ID), false)
    assert.equal(calls.length, 2, 'The controller must reject a stale download even if the UI is bypassed')
    assert.deepEqual(JSON.parse(calls[1].slice('/legion-panel '.length)).args, { task_ref: STATUS_REF, ...options })
    pending.resolve(reply('status', { task_ref: STATUS_REF, artifacts: [], more_events: false }))
    assert.equal(await reading, true)
    assert.equal(controller.getSnapshot().taskStatusStale, false)
    assert.equal(controller.getSnapshot().busy.taskStatus, false)
    assert.deepEqual(plain(controller.getSnapshot().taskStatus.artifacts), [])
  })
}

test('task refresh failure retains non-current observations, fences an old download and recovers only on success', async t => {
  const { createController } = await helpers()
  const pending = deferred(), download = deferred(), calls = []
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    calls.push(line)
    if (line.startsWith('/legion download ')) return download.promise
    if (++reads === 2) return pending.promise
    return reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: false })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.taskStatus(STATUS_REF)
  const original = controller.getSnapshot().taskStatus
  const downloading = controller.downloadArtifact(STATUS_REF, ARTIFACT_ID)
  await turn()
  const reading = controller.taskStatus(STATUS_REF)
  await turn()
  download.resolve(nativeReply({ result: { kind: 'success', text: 'OLD_DOWNLOAD_RECEIPT' } }))
  await downloading
  pending.reject(new Error('synthetic refresh unavailable'))
  assert.equal(await reading, false)
  assert.equal(controller.getSnapshot().taskStatus, original)
  assert.equal(controller.getSnapshot().taskStatusStale, true)
  assert.equal(controller.getSnapshot().busy.taskStatus, false)
  assert.match(controller.getSnapshot().errors.taskStatus, /synthetic refresh unavailable/)
  assert.equal(controller.getSnapshot().download, null, 'A late earlier download must not look like a new success')
  const before = calls.length
  assert.equal(await controller.downloadArtifact(STATUS_REF, ARTIFACT_ID), false)
  assert.equal(calls.length, before, 'An error must not re-enable a retained artifact')
  assert.equal(await controller.taskStatus(STATUS_REF), true)
  assert.equal(controller.getSnapshot().taskStatusStale, false)
  assert.equal(controller.getSnapshot().errors.taskStatus, null)
  assert.equal(await controller.downloadArtifact(STATUS_REF, ARTIFACT_ID), true)
})

for (const failLate of [false, true]) {
  test(`task refresh clears another task and ignores the superseded ${failLate ? 'error' : 'success'}`, async t => {
    const { createController } = await helpers()
    const pending = deferred(), replacement = deferred()
    let reads = 0
    const other = 'd'.repeat(32)
    const controller = createController(context(async () => {
      if (++reads === 1) return reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] })
      return reads === 2 ? pending.promise : replacement.promise
    }))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    await controller.taskStatus(STATUS_REF)
    const oldRead = controller.taskStatus(STATUS_REF)
    await turn()
    const newRead = controller.taskStatus(other)
    assert.equal(controller.getSnapshot().taskStatus, null, 'Do not retain a different task as placeholder content')
    assert.equal(controller.getSnapshot().download, null)
    await turn()
    replacement.resolve(reply('status', { task_ref: other, artifacts: [] }))
    await newRead
    if (failLate) pending.reject(new Error('old task error'))
    else pending.resolve(reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] }))
    assert.equal(await oldRead, false)
    assert.equal(controller.getSnapshot().taskStatus.task_ref, other)
    assert.equal(controller.getSnapshot().errors.taskStatus, null)
    assert.equal(controller.getSnapshot().taskStatusStale, false)
  })
}

for (const failLate of [false, true]) {
  test(`task refresh cannot restore a cached session after switching away and back (${failLate ? 'error' : 'success'})`, async t => {
    const { createController } = await helpers()
    const pending = deferred()
    let reads = 0
    const controller = createController(context(async () => {
      if (++reads === 2) return pending.promise
      return reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] })
    }))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    await controller.taskStatus(STATUS_REF)
    const reading = controller.taskStatus(STATUS_REF)
    await turn()
    controller.selectSession('b')
    assert.equal(controller.getSnapshot().taskStatus, null)
    assert.equal(controller.getSnapshot().download, null)
    controller.selectSession('a')
    if (failLate) pending.reject(new Error('old session error'))
    else pending.resolve(reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] }))
    assert.equal(await reading, false)
    assert.equal(controller.getSnapshot().taskStatus, null)
    assert.equal(controller.getSnapshot().taskStatusStale, false)
    assert.equal(controller.getSnapshot().errors.taskStatus, null)
  })
}

for (const changed of ['baseUrl', 'tokenEnv', 'tokenFile', 'entryId', 'detach']) {
  test(`task refresh drops retained results and late replies when native connection ${changed} changes`, async t => {
    const { createController } = await helpers()
    const fixture = mappedConfiguration(), pending = deferred()
    let configuration = { status: 'ready', writable: true, revision: 7,
      value: { baseUrl: 'https://old.example.invalid', tokenEnv: 'OLD_TOKEN', tokenFile: '/synthetic/old-token' } }
    let changedForm, reads = 0
    fixture.form.getSnapshot = () => configuration
    fixture.form.subscribe = listener => { changedForm = listener; return () => {} }
    const controller = createController(context(async () => {
      if (++reads === 2) return pending.promise
      return reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] })
    }))
    t.after(() => controller.dispose())
    const detach = controller.attach('configuration', fixture.owner)
    await controller.loadConfig()
    controller.selectSession('a')
    await controller.taskStatus(STATUS_REF)
    const reading = controller.taskStatus(STATUS_REF)
    await turn()
    if (changed === 'detach') detach()
    else if (changed === 'entryId') {
      fixture.identity.entryId = 'replacement-loader:legion'
      await controller.loadConfig()
    } else {
      configuration = { ...configuration, revision: 8, value: { ...configuration.value, [changed]: `new-${changed}` } }
      changedForm()
    }
    assert.equal(controller.getSnapshot().taskStatus, null, 'An observed connection change is not a same-identity refresh')
    assert.equal(controller.getSnapshot().download, null)
    pending.resolve(reply('status', { task_ref: STATUS_REF, artifacts: [artifactRow()] }))
    assert.equal(await reading, false)
    assert.equal(controller.getSnapshot().taskStatus, null)
    assert.equal(await controller.downloadArtifact(STATUS_REF, ARTIFACT_ID), false)
    assert.equal(reads, 2, 'Changing configuration must not automatically execute or download anything')
  })
}

for (const example of [
  { locale: 'zh', label: '刷新原任务', args: {}, busy: /正在读取/, stale: /上次.*非最新|上次.*未更新/ },
  { locale: 'zh', label: '读取下一页成果记录', args: { after_event_id: 0, artifact_offset: 30 },
    busy: /正在读取/, stale: /上次.*非最新|上次.*未更新/ },
  { locale: 'zh', label: '读取后续事件', args: { after_event_id: 9 }, busy: /正在读取/, stale: /上次.*非最新|上次.*未更新/ },
  { locale: 'en', label: 'Refresh original task', args: {}, busy: /Reading/, stale: /[Pp]revious.*not current|[Pp]revious.*not updated/ },
]) {
  test(`task refresh view stays mounted and honestly disables downloads through failure: ${example.locale} ${example.label}`, async t => {
    const pending = deferred()
    let reads = 0
    const value = { task_ref: STATUS_REF, artifacts: [artifactRow()], more_events: false,
      runtime: { tasks: [{ task_id: 'author', status: 'done', final: 'RETAINED_OBSERVATION' }] },
      sections: {
        artifacts: { available: true, complete: true, observed_count: 1, shown_count: 1, offset: 0, next_offset: 30 },
        events: { after_event_id: 0, has_more: true, next_after_event_id: 9 },
      } }
    const panel = await statusPanel(t, () => ++reads === 2 ? pending.promise : value, {
      taskCount: 8, locale: example.locale,
    })
    panel.render()
    const focusCount = panel.focusCalls.length, scrollCount = panel.scrollCalls.length
    const downloadLabel = example.locale === 'zh' ? '下载文件' : 'Download file'
    panel.find('button', example.label).props.onClick()
    await turn()
    let tree = panel.render(), text = panel.text(tree)
    assert.match(text, /RETAINED_OBSERVATION/, 'The old runtime/artifact subtree must not collapse during loading')
    assert.match(text, example.busy)
    assert.match(text, example.stale)
    assert.equal(panel.nodes(tree).find(node => node.props.id === 'legion-task-status').props['aria-busy'], true)
    assert.equal(panel.find('button', downloadLabel).props.disabled, true)
    panel.find('button', downloadLabel).props.onClick()
    await turn()
    assert.equal(panel.calls.filter(call => call.line.startsWith('/legion download ')).length, 0)
    const request = panel.calls.filter(call => call.line.includes('"action":"status"')).at(-1)
    assert.deepEqual(JSON.parse(request.line.slice('/legion-panel '.length)).args, { task_ref: STATUS_REF, ...example.args })
    pending.reject(new Error('synthetic latest-read failed'))
    await turn()
    tree = panel.render()
    text = panel.text(tree)
    assert.match(text, /RETAINED_OBSERVATION/)
    assert.match(text, example.stale)
    assert.ok(panel.nodes(tree).some(node => node.props.role === 'alert' && panel.text(node).includes('synthetic latest-read failed')))
    assert.equal(panel.nodes(tree).find(node => node.props.id === 'legion-task-status').props['aria-busy'], false)
    assert.equal(panel.find('button', downloadLabel).props.disabled, true, 'Failure must not turn the retained export into a fresh result')
    assert.doesNotMatch(text, /本次已观察到的成果记录|可请求下载；|Download may be requested;/)
    panel.find('button', downloadLabel).props.onClick()
    await turn()
    assert.equal(panel.calls.filter(call => call.line.startsWith('/legion download ')).length, 0)
    panel.find('button', example.label).props.onClick()
    await turn()
    tree = panel.render()
    assert.doesNotMatch(panel.text(tree), example.stale)
    assert.equal(panel.find('button', downloadLabel).props.disabled, false)
    assert.equal(panel.focusCalls.length, focusCount, 'Refresh and paging must not steal focus')
    assert.equal(panel.scrollCalls.length, scrollCount, 'No synthetic scroll restoration is added')
  })
}

for (const change of ['detach', 'replace']) {
  test(`repair: ${change} invalidates pending member observations without cancelling a new owner`, async t => {
    const { createController } = await helpers()
    const old = deferred(), fresh = deferred()
    let calls = 0
    const controller = createController(context(async () => ++calls === 1 ? old.promise : fresh.promise))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    const detach = controller.attach('configuration', {})
    const reading = controller.loadMembers()
    await turn()
    if (change === 'detach') detach()
    else controller.attach('configuration', {})
    assert.equal(controller.getSnapshot().members, null)
    const replacement = controller.loadMembers()
    await turn()
    old.resolve(reply('runtime', { marker: 'OLD' }))
    assert.equal(await reading, false)
    assert.equal(controller.getSnapshot().members, null)
    assert.equal(controller.getSnapshot().busy.members, true)
    fresh.resolve(reply('runtime', { marker: 'NEW' }))
    assert.equal(await replacement, true)
    assert.equal(controller.getSnapshot().members.marker, 'NEW')
  })
}

for (const outcome of ['accepted', 'rejected', 'lost']) {
  test(`repair: ${outcome} submit receipts stay with their authenticated connection and original key`, async t => {
    const { createController } = await helpers()
    const fixture = mappedConfiguration(), pending = deferred(), calls = []
    let connection = 'a'.repeat(64), notify
    let config = { status: 'ready', writable: true, revision: 7, value: { baseUrl: 'https://old.invalid' } }
    fixture.form.getSnapshot = () => config
    fixture.form.subscribe = fn => { notify = fn; return () => {} }
    const controller = createController(context(async (_id, line) => {
      const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
      if (action === 'tasks') return reply(action, { tasks: [], connection_id: connection })
      assert.equal(action, 'submit')
      calls.push(args)
      if (calls.length === 1) return pending.promise
      return reply('submit', { task_ref: 'b'.repeat(32), submission: 'unconfirmed' })
    }))
    t.after(() => controller.dispose())
    controller.attach('configuration', fixture.owner)
    controller.selectSession('a')
    await controller.loadConfig()
    const original = controller.submit('same task', '')
    await turn()
    const key = calls[0]?.key
    assert.ok(key)
    config = { ...config, revision: 8, value: { baseUrl: 'https://new.invalid' } }
    connection = 'b'.repeat(64)
    notify()
    assert.equal(controller.getSnapshot().operation, null)
    await controller.submit('same task', '')
    assert.notEqual(calls[1]?.key, key, 'A different connection must never reuse the old operation identity')
    if (outcome === 'lost') pending.reject(new Error('old response unknown'))
    else if (outcome === 'rejected') pending.resolve(nativeReply({ result: { kind: 'error', text: JSON.stringify({
      ok: false, action: 'submit', error: 'old refusal', diagnostics: { task_ref: 'a'.repeat(32), submission: 'rejected' },
    }) } }))
    else pending.resolve(reply('submit', { task_ref: 'a'.repeat(32), submission: 'accepted' }))
    await original
    assert.equal(controller.getSnapshot().operation.ref, 'b'.repeat(32))
    assert.equal(controller.getSnapshot().errors.submit, null)
    config = { ...config, revision: 9, value: { baseUrl: 'https://old.invalid' } }
    connection = 'a'.repeat(64)
    notify()
    await controller.loadTasks()
    assert.equal(controller.getSnapshot().operation.key, key, 'Return to the old native identity restores the original operation')
    if (outcome === 'lost') {
      assert.equal(controller.getSnapshot().operation.ref, undefined)
      controller.newTask()
      assert.equal(controller.getSnapshot().operation.key, key, 'Unknown outcomes cannot be discarded')
      await controller.submit('same task', '')
      assert.equal(calls.at(-1).key, key)
      assert.equal(calls.at(-1).connection_id, 'a'.repeat(64))
    } else {
      assert.equal(controller.getSnapshot().operation.ref, 'a'.repeat(32))
      assert.equal(controller.getSnapshot().submission.submission, outcome)
    }
  })
}

test('repair: a connection change before native command readiness prevents stale submit dispatch', async t => {
  const { createController } = await helpers()
  const ready = deferred(), calls = []
  const ctx = context(async (_id, line) => {
    const body = JSON.parse(line.slice('/legion-panel '.length))
    calls.push(body)
    return reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) })
  })
  let using = 0
  ctx.sessions.using = async (sessionId, options, work) => work({ sessionId,
    ready: ++using === 2 ? ready.promise : Promise.resolve() })
  const controller = createController(ctx)
  t.after(() => controller.dispose())
  const detach = controller.attach('configuration', {})
  controller.selectSession('a')
  const submit = controller.submit('synthetic task', '')
  await turn()
  detach()
  ready.resolve()
  await submit
  assert.ok(!calls.some(call => call.action === 'submit'), 'Never dispatch an old operation through a newly resolved command service')
})

test('repair: returning to an original connection cannot duplicate its pending submit and can observe its receipt', async t => {
  const { createController } = await helpers()
  const pending = deferred(), calls = []
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') return reply(action, { tasks: [], connection_id: 'a'.repeat(64) })
    calls.push(args)
    return pending.promise
  }))
  t.after(() => controller.dispose())
  const detach = controller.attach('configuration', {})
  controller.selectSession('a')
  const original = controller.submit('original task', '')
  await turn()
  const key = calls[0].key
  detach()
  controller.attach('configuration', {})
  await controller.loadTasks()
  assert.equal(controller.getSnapshot().operation.key, key)
  await controller.submit('original task', '')
  controller.newTask()
  assert.equal(calls.length, 1)
  assert.equal(controller.getSnapshot().operation.key, key)
  pending.resolve(reply('submit', { task_ref: 'a'.repeat(32), submission: 'accepted' }))
  await original
  assert.equal(controller.getSnapshot().submission.task_ref, 'a'.repeat(32))
  assert.equal(controller.getSnapshot().operation.pending, false)
  assert.equal(controller.getSnapshot().errors.submit, null)
})

function finalFlowConfiguration(controller) {
  const fixture = mappedConfiguration()
  let notify
  let value = { baseUrl: 'https://original.invalid', candidateHarnesses: [], teamProfile: flowProfile }
  fixture.form.getSnapshot = () => ({ status: 'ready', value, revision: 7, writable: true })
  fixture.form.subscribe = listener => { notify = listener; return () => {} }
  controller.attach('configuration', fixture.owner)
  return { change(baseUrl) { value = { ...value, baseUrl }; notify() } }
}

const savedFlowNotes = storage => JSON.parse(
  storage.getItem('dsh-legion.pending-operations.v1') || '{"operations":[]}',
).operations
const settledFlow = { code: 'ok', catalogVerified: true, partial: false, cancelled: false, schedules: [] }

for (const method of ['startFlow', 'runFlow']) {
  test(`final-boundary: ${method} rechecks ownership after native readiness and preserves its original note`, async t => {
    const { createController } = await helpers()
    const ready = deferred(), entered = deferred(), storage = memoryStorage(), calls = []
    const ctx = context(async (_id, line) => {
      const input = JSON.parse(line.slice('/legion-panel '.length))
      calls.push(input)
      return reply(input.action, settledFlow)
    })
    ctx.sessions.using = async (id, _options, fn) => {
      entered.resolve()
      return fn({ sessionId: id, ready: ready.promise })
    }
    const controller = createController(ctx, { storage })
    t.after(() => controller.dispose())
    controller.selectSession('a')
    const config = finalFlowConfiguration(controller)
    await controller.loadConfig()
    const pending = method === 'runFlow' ? controller.runFlow('original-run-key') : controller.startFlow()
    await entered.promise
    const originalNotes = savedFlowNotes(storage)
    assert.equal(originalNotes.length, 1)
    config.change('https://replacement.invalid')
    ready.resolve()
    assert.equal(await pending, false)
    assert.equal(calls.length, 0, 'An old operation must not cross the wire through the replacement connection')
    assert.deepEqual(savedFlowNotes(storage), originalNotes)
    assert.equal(controller.getSnapshot().flow, null)
    assert.equal(controller.getSnapshot().errors[method === 'runFlow' ? 'flowRun' : 'flowStart'], null)

    config.change('https://original.invalid')
    assert.equal(await controller[method](), true)
    assert.equal(calls.length, 1)
    if (method === 'runFlow') assert.equal(calls[0].args.key, 'original-run-key')
    assert.equal(savedFlowNotes(storage).length, 0)
  })
}

test('final-boundary: a late successful flow reply cannot clear the original connection note', async t => {
  const { createController } = await helpers()
  const receipt = deferred(), entered = deferred(), storage = memoryStorage(), calls = []
  const controller = createController(context(async (_id, line) => {
    const input = JSON.parse(line.slice('/legion-panel '.length))
    calls.push(input)
    entered.resolve()
    return receipt.promise
  }), { storage })
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const config = finalFlowConfiguration(controller)
  await controller.loadConfig()
  const pending = controller.runFlow('reply-owner-key')
  await entered.promise
  const originalNotes = savedFlowNotes(storage)
  config.change('https://replacement.invalid')
  receipt.resolve(reply('flow_run', settledFlow))
  assert.equal(await pending, false)
  assert.deepEqual(savedFlowNotes(storage), originalNotes, 'A stale success must remain recoverable under its original owner')
  assert.equal(controller.getSnapshot().flow, null)
  assert.equal(controller.getSnapshot().flowRunKey, null)
  config.change('https://original.invalid')
  assert.equal(await controller.runFlow(), true)
  assert.deepEqual(calls.map(call => call.args.key), ['reply-owner-key', 'reply-owner-key'])
  assert.equal(savedFlowNotes(storage).length, 0)
})

test('final-boundary: note settlement rechecks ownership inside a delayed browser storage lock', async t => {
  const clearing = deferred(), release = deferred(), storage = memoryStorage()
  let locks = 0
  const { createController } = await helpers({ navigator: { locks: { request(_name, work) {
    if (++locks === 2) {
      clearing.resolve()
      return release.promise.then(work)
    }
    return Promise.resolve().then(work)
  } } } })
  const controller = createController(context(async (_id, line) => {
    const input = JSON.parse(line.slice('/legion-panel '.length))
    return reply(input.action, settledFlow)
  }), { storage })
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const config = finalFlowConfiguration(controller)
  await controller.loadConfig()
  const pending = controller.runFlow('storage-lock-key')
  await clearing.promise
  const originalNotes = savedFlowNotes(storage)
  config.change('https://replacement.invalid')
  release.resolve()
  assert.equal(await pending, false)
  assert.deepEqual(savedFlowNotes(storage), originalNotes)
})

test('final-boundary: a late flow error stays with its original connection and keeps the recovery note', async t => {
  const { createController } = await helpers()
  const receipt = deferred(), entered = deferred(), storage = memoryStorage()
  const controller = createController(context(async () => { entered.resolve(); return receipt.promise }), { storage })
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const config = finalFlowConfiguration(controller)
  await controller.loadConfig()
  const pending = controller.runFlow('late-error-key')
  await entered.promise
  const originalNotes = savedFlowNotes(storage)
  config.change('https://replacement.invalid')
  receipt.reject(new Error('old connection failed'))
  assert.equal(await pending, false)
  assert.equal(controller.getSnapshot().errors.flowRun, null)
  assert.deepEqual(savedFlowNotes(storage), originalNotes)
})

test('final-boundary: an initial old preflight cannot replace a newer first identity read', async t => {
  const { createController } = await helpers()
  const old = deferred(), entered = deferred(), submits = []
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') {
      if (++reads === 1) { entered.resolve(); return old.promise }
      return reply(action, { tasks: [], connection_id: 'b'.repeat(64) })
    }
    submits.push(args)
    return reply(action, { task_ref: 'b'.repeat(32), submission: 'accepted' })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const pending = controller.submit('old preflight', '')
  await entered.promise
  await controller.loadTasks()
  assert.equal(controller.getSnapshot().tasks.connection_id, 'b'.repeat(64))
  old.resolve(reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) }))
  await pending
  assert.equal(controller.getSnapshot().tasks?.connection_id, 'b'.repeat(64), 'The newer list must not be erased by an older first response')
  assert.equal(submits.length, 0)
  await controller.submit('new explicit task', '')
  assert.equal(submits.length, 1)
  assert.equal(submits[0].connection_id, 'b'.repeat(64))
})

test('final-boundary: an older task list cannot undo an identity accepted by a newer submit preflight', async t => {
  const { createController } = await helpers()
  const old = deferred(), entered = deferred(), submits = []
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') {
      if (++reads === 1) { entered.resolve(); return old.promise }
      return reply(action, { tasks: [], connection_id: 'b'.repeat(64) })
    }
    submits.push(args)
    return reply(action, { task_ref: 'b'.repeat(32), submission: 'accepted' })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const pending = controller.loadTasks()
  await entered.promise
  await controller.submit('newer explicit task', '')
  const acceptedKey = controller.getSnapshot().operation.key
  old.resolve(reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) }))
  assert.equal(await pending, false)
  assert.equal(controller.getSnapshot().tasks, null)
  assert.equal(controller.getSnapshot().operation.key, acceptedKey)
  assert.equal(controller.getSnapshot().submission.task_ref, 'b'.repeat(32))
  assert.equal(submits.length, 1)
})

test('final-boundary: an older preflight error cannot decorate a newer first connection', async t => {
  const { createController } = await helpers()
  const old = deferred(), entered = deferred()
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    const { action } = JSON.parse(line.slice('/legion-panel '.length))
    assert.equal(action, 'tasks')
    if (++reads === 1) { entered.resolve(); return old.promise }
    return reply(action, { tasks: [], connection_id: 'b'.repeat(64) })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const pending = controller.submit('old preflight', '')
  await entered.promise
  await controller.loadTasks()
  old.reject(new Error('old connection error'))
  await pending
  assert.equal(controller.getSnapshot().tasks.connection_id, 'b'.repeat(64))
  assert.equal(controller.getSnapshot().errors.submit, null)
})

test('final-boundary: a failed newer identity read does not supersede an older valid preflight', async t => {
  const { createController } = await helpers()
  const old = deferred(), entered = deferred(), submits = []
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') {
      if (++reads === 1) { entered.resolve(); return old.promise }
      return reply(action, { tasks: [] })
    }
    submits.push(args)
    return reply(action, { task_ref: 'a'.repeat(32), submission: 'accepted' })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  const pending = controller.submit('valid original request', '')
  await entered.promise
  assert.equal(await controller.loadTasks(), false)
  old.resolve(reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) }))
  await pending
  assert.equal(submits.length, 1)
  assert.equal(submits[0].connection_id, 'a'.repeat(64))
  assert.equal(controller.getSnapshot().submission.task_ref, 'a'.repeat(32))
})

test('final-boundary: the approved headline describes the team goal without claiming universal readiness', async t => {
  const panel = await statusPanel(t, {}, { readTask: false })
  const text = panel.text(panel.render())
  assert.match(text, /顶尖战队，共同进化/)
  assert.ok(text.includes('集结各路优秀 Harness，协作完成同一个目标。以顶级成果为参照，反复对比、验证和改进；工具与有效经验为下一次工作所用。'))
  assert.doesNotMatch(text, /全部已就绪|已经世界第一/)
})

test('repair: a newer authenticated identity read fences an older preflight even with unchanged settings', async t => {
  const { createController } = await helpers()
  const old = deferred(), submits = []
  let reads = 0
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') {
      if (++reads === 2) return old.promise
      return reply(action, { tasks: [], connection_id: (reads === 1 ? 'a' : 'b').repeat(64) })
    }
    submits.push(args)
    return reply('submit', { task_ref: 'b'.repeat(32), submission: 'accepted' })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.loadTasks()
  const pending = controller.submit('old preflight', '')
  await turn()
  await controller.loadTasks()
  old.resolve(reply('tasks', { tasks: [], connection_id: 'a'.repeat(64) }))
  await pending
  assert.equal(submits.length, 0)
  await controller.submit('new explicit task', '')
  assert.equal(submits.length, 1)
  assert.equal(submits[0].connection_id, 'b'.repeat(64))
})

test('repair: unknown native identity cannot erase an uncertain operation or dispatch a replacement', async t => {
  const { createController } = await helpers()
  let identityAvailable = true
  const calls = []
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    if (action === 'tasks') return reply(action, { tasks: [], ...(identityAvailable ? { connection_id: 'a'.repeat(64) } : {}) })
    calls.push(args)
    throw new Error('unknown submit outcome')
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.submit('original task', '')
  const key = controller.getSnapshot().operation.key
  identityAvailable = false
  await controller.submit('original task', '')
  controller.newTask()
  assert.equal(calls.length, 1)
  assert.equal(controller.getSnapshot().operation.key, key)
  assert.match(controller.getSnapshot().errors.submit, /identity is unknown/)
  identityAvailable = true
  await controller.submit('original task', '')
  assert.equal(calls[1].key, key)
})

test('repair: task pagination forwards snapshot and drops late replies after connection invalidation', async t => {
  const { createController } = await helpers()
  const pending = deferred(), calls = []
  const controller = createController(context(async (_id, line) => {
    const { action, args } = JSON.parse(line.slice('/legion-panel '.length))
    calls.push(args)
    if (calls.length > 1) return pending.promise
    return reply(action, { tasks: [{ task_ref: STATUS_REF }], connection_id: 'a'.repeat(64),
      task_page: { snapshot: 'c'.repeat(64), next_offset: 1, offset: 0, total: 2 } })
  }))
  t.after(() => controller.dispose())
  const detach = controller.attach('configuration', {})
  controller.selectSession('a')
  await controller.loadTasks()
  const page = controller.loadTasks(1, 'c'.repeat(64))
  await turn()
  assert.deepEqual(calls[1], { offset: 1, snapshot: 'c'.repeat(64) })
  detach()
  pending.resolve(reply('tasks', { tasks: [{ task_ref: 'd'.repeat(32) }] }))
  assert.equal(await page, false)
  assert.equal(controller.getSnapshot().tasks, null)
})

for (const locale of ['zh', 'en']) {
  test(`repair: Team tasks shows unknown, incomplete and confirmed empty as different facts (${locale})`, async t => {
    let mode = 'unknown'
    const panel = await statusPanel(t, {}, { locale, readTask: false, taskReply: async () => ({
      connection_id: 'a'.repeat(64), tasks: [], task_page: {
        available: mode !== 'unknown', total: mode === 'unknown' ? null : 0,
        shown_count: mode === 'unknown' ? null : 0, offset: 0, next_offset: null,
        source_complete: mode === 'empty', complete: mode === 'empty',
      },
    }) })
    const label = (zh, en) => locale === 'zh' ? zh : en
    panel.find('button', label('战队任务', 'Team tasks')).props.onClick()
    await turn()
    assert.match(panel.text(panel.render()), /无法确认任务列表|Task list unavailable/)
    assert.doesNotMatch(panel.text(panel.render()), /本会话尚无军团任务|No Legion tasks in this session/)
    mode = 'incomplete'
    panel.find('button', label('刷新任务列表', 'Refresh tasks')).props.onClick()
    await turn()
    assert.match(panel.text(panel.render()), /任务索引不完整|Task index is incomplete/)
    assert.doesNotMatch(panel.text(panel.render()), /本会话尚无军团任务|No Legion tasks in this session/)
    mode = 'empty'
    panel.find('button', label('刷新任务列表', 'Refresh tasks')).props.onClick()
    await turn()
    assert.match(panel.text(panel.render()), /本会话尚无军团任务|No Legion tasks in this session/)
    assert.doesNotMatch(panel.text(panel.render()), /接入任务|Integration tasks/)
  })

  test(`repair: Team tasks traverses all 101 actual adapter rows and rejects drift (${locale})`, async t => {
    let tasks = Array.from({ length: 101 }, (_, index) => ({
      task_ref: (index + 1).toString(16).padStart(32, '0'), key: `task-${index}`,
    }))
    const client = { clean: value => value, redactText: value => value,
      scope(execution) { assert.equal(execution.sessionId, 'a') },
      store: { service: 'a'.repeat(64), root: '/synthetic' },
      list: async () => ({ tasks, source: 'local_recovery_index_not_live_status' }) }
    const panel = await statusPanel(t, {}, { locale, readTask: false, taskReply: async (id, args) => {
      const result = await executeWorkbenchCommand({}, client, {}, {
        agent: { id }, signal: new AbortController().signal, attachments: [],
        rawInput: JSON.stringify({ action: 'tasks', args }),
      })
      const body = JSON.parse(result.text)
      if (!body.ok) throw new Error(body.error)
      return body.value
    } })
    const label = (zh, en) => locale === 'zh' ? zh : en
    panel.find('button', label('战队任务', 'Team tasks')).props.onClick()
    await turn()
    const seen = []
    for (;;) {
      const tree = panel.render()
      seen.push(...panel.nodes(tree).filter(node => node.type === 'article')
        .map(node => panel.nodes(node).find(child => child.type === 'code')).map(node => panel.text(node)))
      const next = panel.find('button', label('下一页任务', 'Next task page'))
      if (!next) break
      next.props.onClick()
      await turn()
    }
    assert.deepEqual(seen, tasks.map(row => row.task_ref))
    panel.find('button', label('返回任务首页', 'Back to first task page')).props.onClick()
    await turn()
    tasks = [{ task_ref: 'f'.repeat(32), key: 'new row' }, ...tasks]
    panel.find('button', label('下一页任务', 'Next task page')).props.onClick()
    await turn()
    assert.match(panel.text(panel.render()), /索引已变化|index changed/)
    assert.match(panel.text(panel.render()), /旧任务列表|not current/)
    assert.equal(panel.find('button', label('下一页任务', 'Next task page')).props['aria-disabled'], true)
    panel.find('button', label('刷新任务列表', 'Refresh tasks')).props.onClick()
    await turn()
    assert.match(panel.text(panel.render()), /new row/)
    assert.doesNotMatch(panel.text(panel.render()), /旧任务列表|not current/)
  })
}
