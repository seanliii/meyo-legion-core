import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createContext, runInContext } from 'node:vm'
import { webcrypto } from 'node:crypto'

const plain = value => JSON.parse(JSON.stringify(value))
const turn = () => new Promise(resolve => setImmediate(resolve))
const reply = (action, value) => ({ ok: true, value: {
  result: { kind: 'success', text: JSON.stringify({ ok: true, action, value }) },
} })

function memoryStorage(entries = []) {
  const values = new Map(entries)
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: key => { values.delete(key) },
    entries: () => [...values.entries()],
  }
}

async function helpers({ storage = memoryStorage() } = {}) {
  let definition
  const realm = createContext({
    window: { localStorage: storage, sessionStorage: memoryStorage(), __ModuleLoader__: { load(value) { definition = value } } },
    URL, AbortController, DOMException, TextEncoder, console, crypto: webcrypto,
  })
  runInContext(await readFile(new URL('../client/index.js', import.meta.url), 'utf8'), realm)
  return definition.factory(() => assert.fail('No UI import needed')).workbench
}

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
    remote: { commands: { async execute(id, line, attachments) {
      assert.deepEqual(plain(attachments), [])
      return execute(id, JSON.parse(line.slice('/legion-panel '.length)))
    } } },
  }
}

function configuration(mutate) {
  let snapshot = { status: 'ready', value: {}, revision: 7, writable: true }
  return {
    inventory: { list: async () => ({ ok: true, value: {
      entries: [{ entryId: 'legion', moduleName: 'dsh-plugin-legion' }],
    } }) },
    forms: {
      describe: () => ({ ensure: async () => {}, getSnapshot: () => ({
        view: { namespaces: [{ ns: 'legion' }] },
      }) }),
      get: () => ({
        getSnapshot: () => snapshot, subscribe: () => () => {},
        async mutate(operations, revision) {
          assert.equal(revision, 7)
          const accepted = await mutate(plain(operations))
          if (accepted) snapshot = { ...snapshot, revision: 8, value: Object.fromEntries(
            operations.filter(op => op.op === 'set').map(op => [op.path[0], op.value]),
          ) }
          return accepted
        },
      }),
    },
  }
}

async function bindConfiguration(controller) {
  controller.attach('configuration', configuration(async () => true))
  await controller.loadConfig()
}

test('team form writes one native teamProfile and candidate selection without authority fields', async () => {
  const { configOperations } = await helpers()
  const profile = { name: '并肩作战', evolutionEnabled: true, collaboration: 'auto', cadence: 'weekly' }
  const operations = plain(configOperations({ teamProfile: profile, candidateHarnesses: ['codex'], sessionId: 'forged' }))
  assert.deepEqual(operations, [
    { op: 'set', path: ['candidateHarnesses'], value: ['codex'] },
    { op: 'set', path: ['teamProfile'], value: profile },
  ])
  for (const value of [null, [], 'enabled']) assert.throws(() => configOperations({ teamProfile: value }))
})

for (const [method, action, args] of [
  ['loadFlow', 'flow_status', {}], ['startFlow', 'flow_start', {}], ['pauseFlow', 'flow_pause', {}],
  ['runFlow', 'flow_run', { key: 'explicit-operation' }],
]) {
  test(`${method} uses the selected native session and never claims execution from delivery`, async t => {
    const { createController } = await helpers()
    const calls = []
    const flow = { code: 'ok', catalogVerified: true, configured: true, enabled: true, schedules: [{ id: 's1', status: 'active' }],
      observations: [{ code: 'delivery_is_not_execution' }] }
    const controller = createController(context(async (id, input) => {
      calls.push({ id, ...input })
      return reply(action, flow)
    }))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    await bindConfiguration(controller)
    assert.equal(await controller[method](...(method === 'runFlow' ? [args.key] : [])), true)
    assert.deepEqual(plain(calls), [{ id: 'a', action, args }])
    assert.deepEqual(plain(controller.getSnapshot().flow), flow)
  })
}

test('a late flow status read cannot overwrite a newer pause', async t => {
  const { createController } = await helpers()
  let settle
  const controller = createController(context(async (id, { action }) => {
    if (action === 'flow_status') return new Promise(resolve => { settle = resolve })
    return reply(action, { code: 'ok', catalogVerified: true, enabled: false, schedules: [], observations: [] })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await bindConfiguration(controller)
  const pending = controller.loadFlow()
  await turn()
  await controller.pauseFlow()
  settle(reply('flow_status', { enabled: true }))
  await pending
  assert.equal(controller.getSnapshot().flow.enabled, false)
})

test('flow actions and errors cannot leak to a newly selected session', async t => {
  const { createController } = await helpers()
  let reject
  const entered = deferred()
  const controller = createController(context(async () => new Promise((_, fail) => { reject = fail; entered.resolve() })))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await bindConfiguration(controller)
  const pending = controller.startFlow()
  await Promise.race([entered.promise, pending.then(() => assert.fail('Expected the original flow call'))])
  controller.selectSession('b')
  reject(new Error('belongs to session a'))
  await pending
  assert.equal(controller.getSnapshot().flow, null)
  assert.equal(controller.getSnapshot().errors.flowStart, null)
  assert.equal(controller.getSnapshot().busy.flowStart, false)
})

test('uncertain manual runs retain their original operation key rather than dispatch twice', async t => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(context(async (id, input) => {
    calls.push(input)
    if (calls.length === 1) throw new Error('connection lost after save')
    return reply(input.action, { code: 'ok', catalogVerified: true, enabled: true, schedules: [{ id: 'same-task' }] })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await bindConfiguration(controller)
  assert.equal(await controller.runFlow(), false)
  const firstKey = controller.getSnapshot().flowRunKey
  assert.ok(firstKey)
  assert.equal(await controller.runFlow(), true)
  assert.equal(calls[0].args.key, calls[1].args.key)
  assert.equal(calls[0].args.key, firstKey)
})

test('setup saves both choices then automatically starts, with no second enable action', async t => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(context(async (id, input) => {
    calls.push({ id, ...input })
    return reply(input.action, { code: 'ok', catalogVerified: true, enabled: true, configured: true, schedules: [{ id: 'native-first' }] })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  controller.attach('configuration', configuration(async operations => { calls.push({ operations }); return true }))
  await controller.loadConfig()
  const profile = { name: '我的战队', evolutionEnabled: true }
  assert.equal(await controller.saveAndStartTeam({ teamProfile: profile, candidateHarnesses: [] }, 7, 'legion'), true)
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[0].operations.map(op => op.path[0]), ['candidateHarnesses', 'teamProfile'])
  assert.deepEqual(calls[1], { id: 'a', action: 'flow_start', args: {} })
  assert.equal(controller.getSnapshot().flow.schedules[0].id, 'native-first')
})

test('refused setup cannot start and delayed save cannot switch the target session', async t => {
  const { createController } = await helpers()
  for (const changeSession of [false, true]) {
    let settle
    const entered = deferred()
    const controller = createController(context(() => assert.fail('Must not schedule')))
    t.after(() => controller.dispose())
    controller.selectSession('a')
    controller.attach('configuration', configuration(() => new Promise(resolve => { settle = resolve; entered.resolve() })))
    await controller.loadConfig()
    const pending = controller.saveAndStartTeam({ teamProfile: {}, candidateHarnesses: [] }, 7, 'legion')
    await Promise.race([entered.promise, pending.then(() => assert.fail('Expected the original config write'))])
    if (changeSession) controller.selectSession('b')
    settle(changeSession)
    assert.equal(await pending, false)
    assert.equal(controller.getSnapshot().flow, null)
  }
})

test('native partial receipts preserve the manual operation key and their real observations', async t => {
  const { createController } = await helpers()
  const calls = []
  const controller = createController(context(async (id, input) => {
    calls.push(input)
    return reply(input.action, calls.length === 1
      ? { code: 'partial', partial: true, catalogVerified: false,
        observations: [{ code: 'write_unconfirmed', message: 'saved, response unavailable' }] }
      : { code: 'ok', catalogVerified: true, schedules: [{ id: 'same-native-record' }] })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await bindConfiguration(controller)
  assert.equal(await controller.runFlow(), false)
  assert.ok(controller.getSnapshot().flowRunKey)
  assert.equal(controller.getSnapshot().flow.observations[0].code, 'write_unconfirmed')
  assert.equal(await controller.runFlow(), true)
  assert.equal(calls[0].args.key, calls[1].args.key)
})

test('open results navigates only to the exact selected native conversation', async t => {
  const { createController } = await helpers()
  const opened = []
  const controller = createController(context(() => assert.fail('Navigation is not a new model input')))
  t.after(() => controller.dispose())
  controller.attach('navigation', { openSession: id => opened.push(id) })
  controller.selectSession('b')
  assert.equal(await controller.openFlowSession(), true)
  assert.deepEqual(opened, ['b'])
})

test('model readiness reports a missing native model, not a successful automatic run', async t => {
  const { createController } = await helpers()
  const controller = createController(context(() => assert.fail('Readiness does not schedule')))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  controller.attach('models', { modelCatalog: async () => ({ ok: true, value: {
    default: { provider: 'unconfigured', model: 'unconfigured' }, groups: [], failures: [],
  } }) })
  assert.equal(await controller.loadModelStatus(), true)
  assert.equal(controller.getSnapshot().modelStatus.available, false)
  assert.equal(controller.getSnapshot().modelStatus.execution, 'not_verified')
  controller.selectSession('b')
  assert.equal(controller.getSnapshot().modelStatus, null)
})

test('model readiness uses the selected conversation model, not an unrelated available default', async t => {
  const { createController } = await helpers()
  const ctx = context(() => assert.fail('Readiness does not schedule'))
  ctx.sessions.using = async (id, options, fn) => fn({ ready: Promise.resolve({
    session: {
      getSnapshot: () => ({ running: false, lastAgentError: 'model not found' }),
      projections: { faceOf: key => {
        assert.equal(key, 'modelSelection')
        return { getSnapshot: () => ({ next: { provider: 'p', model: 'removed-model' } }) }
      } },
    },
  }) })
  const controller = createController(ctx)
  t.after(() => controller.dispose())
  controller.selectSession('a')
  controller.attach('models', { modelCatalog: async () => ({ ok: true, value: {
    default: { provider: 'p', model: 'working-model' },
    groups: [{ id: 'p', models: [{ id: 'working-model' }] }], failures: [],
  } }) })
  assert.equal(await controller.loadModelStatus(), true)
  assert.equal(controller.getSnapshot().modelStatus.available, false)
  assert.equal(controller.getSnapshot().modelStatus.model, 'removed-model')
  assert.equal(controller.getSnapshot().modelStatus.lastError, 'model not found')
})

test('a failed refresh marks the retained schedule observation as stale', async t => {
  const { createController } = await helpers()
  let fail = false
  const controller = createController(context(async (id, input) => {
    if (fail) throw new Error('offline')
    return reply(input.action, { code: 'ok', catalogVerified: true, enabled: true })
  }))
  t.after(() => controller.dispose())
  controller.selectSession('a')
  await controller.loadFlow()
  fail = true
  await controller.loadFlow()
  assert.equal(controller.getSnapshot().flowStale, true)
  assert.equal(controller.getSnapshot().flow.enabled, true, 'Keep the previous fact without presenting it as current')
})

const fullProfile = {
  name: '恢复测试战队', goal: 'PRIVATE_GOAL_MUST_NOT_ENTER_STORAGE', collaboration: 'auto',
  leader: '', instructions: 'PRIVATE_REQUIREMENTS_MUST_NOT_ENTER_STORAGE', evolutionEnabled: true, cadence: 'weekly',
}
const settledFlow = {
  code: 'ok', catalogVerified: true, configured: true, enabled: true, partial: false,
  workflow: { initialObserved: true, recurringActive: true, configurationMatches: true },
  schedules: [{ id: 'native-original', role: 'initial', status: 'active', matchesConfiguration: true }],
  observations: [], execution: 'not_verified',
}
const deferred = () => Promise.withResolvers()

// A Host survives browser-module replacement. Its committed value and the
// browser's cached form deliberately differ until an actual describe read.
function recoveryConfiguration(value = { teamProfile: fullProfile, candidateHarnesses: [] }) {
  const identity = { entryId: 'include:legion', namespace: 'legion' }
  let committed = { baseUrl: 'https://PRIVATE-CONNECTION.invalid', tokenFile: '/PRIVATE/CREDENTIAL/PATH', ...plain(value) }
  let cached = plain(committed), revision = 7
  const writes = [], reads = [], listeners = new Set()
  let mutation
  const view = () => ({ ns: identity.namespace, value: plain(committed), revision })
  const acceptView = incoming => {
    cached = plain(incoming.value)
    for (const listener of listeners) listener()
  }
  const commit = ops => {
    for (const op of ops) {
      if (op.op === 'set') committed[op.path[0]] = plain(op.value)
      else delete committed[op.path[0]]
    }
    revision++
  }
  const form = {
    getSnapshot: () => ({ status: 'ready', writable: true, value: plain(cached), revision }),
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    async mutate(ops) {
      writes.push(plain(ops))
      if (mutation) return mutation(plain(ops), commit)
      commit(ops)
      acceptView(view())
      return true
    },
  }
  const owner = {
    inventory: { async list() { return { ok: true, value: { entries: [{
      entryId: identity.entryId, moduleName: 'dsh-plugin-legion',
    }] } } } },
    manager: { async listBundles() { return { ok: true, value: [{ name: 'dsh-plugin-legion', rows: [{
      entryId: identity.entryId, rowId: identity.namespace, moduleName: 'dsh-plugin-legion',
    }] }] } } },
    settings: { async describe() {
      reads.push(plain(committed))
      return { ok: true, value: { writable: true, namespaces: [view()] } }
    } },
    forms: {
      describe: () => ({ async ensure() {}, acceptView,
        getSnapshot: () => ({ view: { namespaces: [{ ns: identity.namespace }] }, error: null }) }),
      get: () => form,
    },
  }
  return {
    identity, owner, form, writes, reads,
    mutateWith(work) { mutation = work },
    committed: () => plain(committed),
    replace(value) { committed = plain(value); cached = plain(value); revision++ },
  }
}

async function restoredController(t, storage, config, execute, sessionId = 'a') {
  // Each call evaluates the entire production browser module in a fresh VM.
  const { createController } = await helpers({ storage })
  const controller = createController(context(execute))
  t.after(() => controller.dispose())
  controller.attach('configuration', config.owner)
  controller.selectSession(sessionId)
  await controller.loadConfig()
  return controller
}

test('recovery P1: a committed setup survives disposal before its reply and a complete browser-module reload', async t => {
  const storage = memoryStorage()
  const config = recoveryConfiguration({})
  const entered = deferred(), acknowledgement = deferred()
  config.mutateWith(async (ops, commit) => {
    commit(ops)
    entered.resolve()
    return acknowledgement.promise
  })
  t.after(() => acknowledgement.resolve(true))
  const calls = []
  const execute = async (id, input) => { calls.push({ id, ...input }); return reply(input.action, settledFlow) }
  const first = await restoredController(t, storage, config, execute)
  const pending = first.saveAndStartTeam({ teamProfile: fullProfile, candidateHarnesses: [] }, 7,
    config.identity.entryId, config.identity.namespace)
  await Promise.race([entered.promise, pending.then(() => assert.fail('Setup must reach native mutation'))])
  first.dispose()
  const saved = JSON.stringify(storage.entries())
  assert.ok(storage.entries().length > 0, 'Explicit start intent must survive the destroyed controller')
  assert.doesNotMatch(saved, /PRIVATE_GOAL|PRIVATE_REQUIREMENTS|PRIVATE-CONNECTION|CREDENTIAL\/PATH/,
    'Only operation identity and hashes may be persisted')
  const second = await restoredController(t, storage, config, execute)
  acknowledgement.resolve(true)
  assert.equal(await pending, false, 'The destroyed controller cannot continue dispatching')
  assert.ok(config.reads.length > 0, 'Recovery must reread committed configuration, not trust the old form cache')
  assert.deepEqual(calls, [{ id: 'a', action: 'flow_start', args: {} }])
  assert.equal(second.getSnapshot().flow.schedules[0].id, 'native-original')
  assert.deepEqual(storage.entries(), [])
})

test('recovery P1: a created run with an unknown reply reuses the original key after the whole module reloads', async t => {
  const storage = memoryStorage(), config = recoveryConfiguration()
  const requests = [], nativeTasks = new Map()
  const execute = async (id, input) => {
    requests.push({ id, ...input })
    assert.equal(input.action, 'flow_run')
    if (!nativeTasks.has(input.args.key)) nativeTasks.set(input.args.key, `native-${nativeTasks.size + 1}`)
    if (requests.length === 1) throw new Error('Reply lost after native create')
    return reply(input.action, { ...settledFlow, schedules: [{ id: nativeTasks.get(input.args.key) }] })
  }
  const first = await restoredController(t, storage, config, execute)
  assert.equal(await first.runFlow(), false)
  const key = first.getSnapshot().flowRunKey
  assert.ok(key)
  first.dispose()
  const second = await restoredController(t, storage, config, execute)
  assert.equal(requests.length, 2, 'A reload resumes only the explicit unresolved operation')
  assert.equal(requests[1].args.key, key)
  assert.equal(nativeTasks.size, 1, 'No duplicate native task')
  assert.equal(second.getSnapshot().flow.schedules[0].id, 'native-1')
  assert.equal(second.getSnapshot().flowRunKey, null)
  assert.deepEqual(storage.entries(), [])
})

test('recovery: unknown save reads back before starting, whereas a definite refusal clears the intent even after disposal', async t => {
  for (const refusal of [false, true]) {
    const storage = memoryStorage(), config = recoveryConfiguration(refusal ? { teamProfile: fullProfile, candidateHarnesses: [] } : {})
    const calls = []
    const execute = async (_id, input) => {
      assert.ok(config.reads.length, 'Unknown save requires an actual readback')
      calls.push(input)
      return reply(input.action, settledFlow)
    }
    const first = await restoredController(t, storage, config, execute)
    config.mutateWith(async (ops, commit) => {
      if (refusal) { first.dispose(); return false }
      commit(ops)
      throw new Error('Unknown write outcome')
    })
    assert.equal(await first.saveAndStartTeam({ teamProfile: fullProfile, candidateHarnesses: [] }, 7,
      config.identity.entryId, config.identity.namespace), !refusal)
    first.dispose()
    await restoredController(t, storage, config, execute)
    assert.equal(calls.length, refusal ? 0 : 1)
    assert.deepEqual(storage.entries(), [])
  }
})

test('recovery: an unknown uncommitted save stays pending and never starts from a mismatching readback', async t => {
  const storage = memoryStorage(), config = recoveryConfiguration({})
  config.mutateWith(async () => { throw new Error('Write outcome unknown, no commit observed') })
  const first = await restoredController(t, storage, config, () => assert.fail('Unconfirmed mismatching config cannot start'))
  assert.equal(await first.saveAndStartTeam({ teamProfile: fullProfile, candidateHarnesses: [] }, 7,
    config.identity.entryId, config.identity.namespace), false)
  assert.ok(config.reads.length > 0)
  assert.ok(storage.entries().length)
  first.dispose()
  const second = await restoredController(t, storage, config, () => assert.fail('Reload must not infer a successful save'))
  assert.ok(second.getSnapshot().errors.flowRecovery || second.getSnapshot().errors.teamSetup)
  assert.ok(storage.entries().length, 'Do not silently forget the unresolved identity')
})

test('recovery: operation identities cannot cross a session, entry, namespace, connection or configuration snapshot', async t => {
  const original = recoveryConfiguration(), storage = memoryStorage()
  const first = await restoredController(t, storage, original, async () => { throw new Error('Created; reply unknown') })
  assert.equal(await first.runFlow('original-key'), false)
  first.dispose()
  const saved = storage.entries()
  for (const change of ['session', 'entry', 'namespace', 'connection', 'profile']) {
    const copy = memoryStorage(saved), config = recoveryConfiguration(original.committed())
    if (change === 'entry') config.identity.entryId = 'another-entry'
    if (change === 'namespace') config.identity.namespace = 'another-namespace'
    if (change === 'connection') config.replace({ ...config.committed(), baseUrl: 'https://another.invalid' })
    if (change === 'profile') config.replace({ ...config.committed(), teamProfile: { ...fullProfile, goal: 'Other goal' } })
    const other = await restoredController(t, copy, config, () => assert.fail(`Must not replay across ${change}`), change === 'session' ? 'b' : 'a')
    assert.equal(other.getSnapshot().flowRunKey, null)
    assert.deepEqual(copy.entries(), saved)
    other.dispose()
  }
})

test('recovery: a completed pause or a page with no unfinished intent never auto-starts on reload', async t => {
  const storage = memoryStorage(), config = recoveryConfiguration()
  let runs = 0
  const first = await restoredController(t, storage, config, async (_id, { action }) => {
    if (action === 'flow_run') { runs++; throw new Error('Unconfirmed run') }
    assert.equal(action, 'flow_pause')
    return reply(action, { ...settledFlow, enabled: false, schedules: [],
      workflow: { initialObserved: false, recurringActive: false, configurationMatches: true } })
  })
  assert.equal(await first.runFlow(), false)
  assert.equal(await first.pauseFlow(), true)
  first.dispose()
  const second = await restoredController(t, storage, config, () => assert.fail('Paused/no-intent pages must not start work'))
  assert.equal(runs, 1)
  assert.equal(second.getSnapshot().flowRunKey, null)
  assert.deepEqual(storage.entries(), [])
})

test('recovery: unavailable browser storage fails explicitly before configuration mutation or native creation', async t => {
  for (const method of ['getItem', 'setItem']) {
    const storage = memoryStorage(), config = recoveryConfiguration()
    storage[method] = () => { throw new Error('PRIVATE_STORAGE_ERROR') }
    const controller = await restoredController(t, storage, config, () => assert.fail('No durable identity, no dispatch'))
    assert.equal(await controller.runFlow(), false)
    assert.equal(await controller.saveAndStartTeam({ teamProfile: fullProfile }, 7,
      config.identity.entryId, config.identity.namespace), false)
    assert.equal(config.writes.length, 0)
    const errors = JSON.stringify(controller.getSnapshot().errors)
    assert.match(errors, /存储|storage/i)
    assert.doesNotMatch(errors, /PRIVATE_STORAGE_ERROR/)
  }
})

test('recovery: a failed identity cleanup keeps the acknowledged run key across another module reload', async t => {
  const storage = memoryStorage(), config = recoveryConfiguration(), keys = []
  const remove = storage.removeItem
  storage.removeItem = () => { throw new Error('Cleanup denied') }
  const execute = async (_id, input) => { keys.push(input.args.key); return reply(input.action, settledFlow) }
  const first = await restoredController(t, storage, config, execute)
  assert.equal(await first.runFlow(), false)
  assert.match(first.getSnapshot().errors.flowRun || '', /存储|storage/i)
  assert.ok(first.getSnapshot().flowRunKey)
  first.dispose()
  storage.removeItem = remove
  await restoredController(t, storage, config, execute)
  assert.equal(keys.length, 2)
  assert.equal(keys[0], keys[1])
  assert.deepEqual(storage.entries(), [])
})

async function nativeMirrorConfiguration() {
  // Execute the exact shipped mirror, not a reimplementation of ensure/load/
  // acceptView. Only its observable store and remote transport are test seams.
  const source = await readFile(new URL(
    '../node_modules/@deepseek-ai/dsh-client-ui-settings/lib/client.js', import.meta.url), 'utf8')
  const start = source.indexOf('var SettingsDescribeMirror = class {')
  const end = source.indexOf('//#endregion', start)
  assert.ok(start >= 0 && end > start, 'The pinned native SettingsDescribeMirror must be available')
  const realm = createContext({
    _deepseek_ai_dsh_client_store: {
      createSnapshotStore(initial) {
        let snapshot = initial
        const listeners = new Set()
        return {
          getSnapshot: () => snapshot,
          set(value) { snapshot = value; for (const listener of listeners) listener() },
          subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
        }
      },
    },
  })
  const NativeMirror = runInContext(`${source.slice(start, end)}\nSettingsDescribeMirror`, realm)
  const identity = { entryId: 'include:legion', namespace: 'legion' }
  let document = { writable: true, hasDocument: true, namespaces: [{
    ns: 'legion', revision: 7, value: { teamProfile: fullProfile, candidateHarnesses: [] },
  }] }
  let failed = false, describes = 0, inventoryReads = 0, bundleReads = 0
  const settings = { async describe() {
    describes++
    return failed ? { ok: false, error: { message: 'Previously failed native describe' } }
      : { ok: true, value: plain(document) }
  } }
  const mirror = new NativeMirror({ remote: { settings } })
  await mirror.ensure()
  const writes = []
  // Same document/namespace ownership as native ConfigFormController.derive.
  const form = {
    getSnapshot() {
      const view = mirror.getSnapshot().view
      const section = view?.namespaces.find(row => row.ns === identity.namespace)
      return { status: section ? 'ready' : 'unavailable', writable: view?.writable,
        revision: section?.revision, value: section?.value }
    },
    subscribe: listener => mirror.subscribe(listener),
    async mutate(...args) { writes.push(args); return false },
  }
  const owner = {
    settings,
    inventory: { async list() {
      inventoryReads++
      return { ok: true, value: { entries: [{ entryId: identity.entryId, moduleName: 'dsh-plugin-legion' }] } }
    } },
    manager: { async listBundles() {
      bundleReads++
      return { ok: true, value: [{ name: 'dsh-plugin-legion', rows: [{
        entryId: identity.entryId, rowId: identity.namespace, moduleName: 'dsh-plugin-legion',
      }] }] }
    } },
    forms: { describe: () => mirror, get: () => form },
  }
  return {
    owner, mirror, identity, writes,
    counts: () => ({ describes, inventoryReads, bundleReads }),
    document: () => plain(document),
    replaceDocument(value) { document = plain(value) },
    async failMirrorRefresh() { failed = true; await mirror.load(); failed = false },
  }
}

test('native mirror boundary: a fresh describe can recover despite a ready mirror retaining an old error', async t => {
  const config = await nativeMirrorConfiguration(), storage = memoryStorage(), calls = []
  const controller = await restoredController(t, storage, config, async (_id, input) => {
    calls.push(input)
    if (calls.length === 1) throw new Error('Native task created, reply lost')
    return reply(input.action, settledFlow)
  })
  assert.equal(await controller.runFlow('native-mirror-key'), false)
  await config.failMirrorRefresh()
  assert.equal(config.mirror.getSnapshot().status, 'ready')
  assert.equal(config.mirror.getSnapshot().error, 'Previously failed native describe')
  const before = config.counts()
  await config.mirror.ensure()
  assert.deepEqual(config.counts(), before, 'The actual native ensure does not retry a ready mirror')

  assert.equal(await controller.recoverPendingFlow(), true,
    'The successful fresh document must not be vetoed by the older mirror error')
  assert.equal(calls.length, 2)
  assert.equal(calls[1].args.key, 'native-mirror-key')
  assert.equal(config.counts().describes, before.describes + 1)
  assert.equal(config.counts().inventoryReads, before.inventoryReads + 1, 'Still reread native entry ownership')
  assert.equal(config.counts().bundleReads, before.bundleReads + 1, 'Still reread native namespace mapping')
  assert.deepEqual(storage.entries(), [])
})

test('native mirror boundary: fresh document writability is retained without fabricating a full mirror refresh', async t => {
  const config = await nativeMirrorConfiguration(), storage = memoryStorage()
  let calls = 0, loseReply = true
  const controller = await restoredController(t, storage, config, async (_id, input) => {
    calls++
    if (loseReply) throw new Error('Native write accepted, reply lost')
    return reply(input.action, settledFlow)
  })
  assert.equal(await controller.runFlow('readonly-recovery'), false)
  config.replaceDocument({ ...config.document(), writable: false, hasDocument: false })
  loseReply = false
  assert.equal(await controller.recoverPendingFlow(), true)
  assert.equal(config.mirror.getSnapshot().view.writable, true,
    'Official acceptView replaces only the namespace; do not fake a complete native document')
  assert.equal(config.mirror.getSnapshot().view.hasDocument, true)
  assert.equal(controller.getSnapshot().config.writable, false, 'Use the fresh Host fact, not cached writable=true')
  assert.equal(controller.getSnapshot().config.hasDocument, false)
  assert.equal(await controller.saveConfig({ teamProfile: fullProfile }, 7,
    config.identity.entryId, config.identity.namespace), false)
  assert.equal(config.writes.length, 0, 'The read-only UI must reject writes before calling the cached native form')

  // The readback fix must not relax the original revision or owner fences.
  loseReply = true
  assert.equal(await controller.runFlow('still-fenced'), false)
  const sent = calls, saved = storage.entries()
  const valid = config.document()
  config.replaceDocument({ ...valid, namespaces: valid.namespaces.map(row => ({ ...row, revision: 6 })) })
  loseReply = false
  assert.equal(await controller.recoverPendingFlow(), false)
  assert.equal(calls, sent)
  assert.deepEqual(storage.entries(), saved)
  config.replaceDocument(valid)
  config.identity.entryId = 'replacement-entry'
  assert.equal(await controller.recoverPendingFlow(), false)
  assert.equal(calls, sent)
  assert.deepEqual(storage.entries(), saved)
})
