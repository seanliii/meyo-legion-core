import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import { createAfterScheduleRecord, createEveryScheduleRecord, decodeScheduleRecord } from '@deepseek-ai/dsh-schedule'

// Deliberately a fault-injection double, not the native-persistence integration.
function scheduleDouble() {
  const rows = new Map()
  const calls = { create: [], update: [], delete: [], catalog: 0, history: 0 }
  let sequence = 0
  const service = {
    rows, calls,
    async catalog() {
      calls.catalog++
      return structuredClone([...rows.values()])
    },
    async create(sessionId, request, signal) {
      signal?.throwIfAborted()
      calls.create.push({ sessionId, request: structuredClone(request), signal })
      const id = `schedule-${++sequence}`
      const record = request.every_seconds === undefined
        ? createAfterScheduleRecord(id, request.prompt, request.after_seconds, Date.now(), request.title)
        : createEveryScheduleRecord(id, request.prompt, request.every_seconds, Date.now(), request.title)
      rows.set(id, { ...record, sessionId, status: 'active' })
      return structuredClone(record)
    },
    async update(request, signal) {
      signal?.throwIfAborted()
      calls.update.push(structuredClone(request))
      // The official decoder rejects catalog-only fields in expected.
      decodeScheduleRecord(request.expected)
      const current = rows.get(request.id)
      if (!current || current.sessionId !== request.sessionId) return { id: request.id, updated: false, code: 'schedule_not_found' }
      if (current.status === 'inactive') return { id: request.id, updated: false, code: 'schedule_ended' }
      const { sessionId, status, lastDelivery, ...record } = current
      if (!isDeepStrictEqual(record, request.expected)) return { id: request.id, updated: false, code: 'schedule_conflict' }
      const next = { ...record, ...(request.title === undefined ? {} : { title: request.title }),
        ...(request.prompt === undefined ? {} : { prompt: request.prompt }) }
      if (request.change) {
        assert.equal(request.change.kind, 'every')
        Object.assign(next, createEveryScheduleRecord(record.id, next.prompt, request.change.every_seconds, Date.now(), next.title))
      }
      rows.set(request.id, { ...next, sessionId, status, ...(lastDelivery ? { lastDelivery } : {}) })
      return { id: request.id, updated: !isDeepStrictEqual(record, next), record: structuredClone(next) }
    },
    async delete(request, signal) {
      signal?.throwIfAborted()
      calls.delete.push(structuredClone(request))
      const current = rows.get(request.id)
      if (!current || current.sessionId !== request.sessionId) return { id: request.id, deleted: false, code: 'schedule_not_found' }
      rows.delete(request.id)
      return { id: request.id, deleted: true }
    },
    async history() {
      calls.history++
      throw new Error('status must not fetch immutable historical prompt bodies')
    },
  }
  return service
}

const profile = overrides => ({
  name: '我的战队', goal: '', collaboration: 'auto', leader: '', instructions: '',
  evolutionEnabled: true, cadence: 'weekly', ...overrides,
})
let sessionSequence = 0
const execution = (sessionId = `team-test-${++sessionSequence}`, signal = new AbortController().signal) => ({ sessionId, signal })
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const digest = value => createHash('sha256').update(value).digest('hex')
const firstLine = row => row.prompt.split('\n')[0]
const hasCode = (value, code) => value.code === code || value.observations.some(item => item.code === code)
const one = (value, role) => value.schedules.find(row => row.role === role)
const text = value => JSON.stringify(value)

async function factory() {
  let module
  try { module = await import('../lib/team-flow.js') }
  catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error }
  assert.equal(typeof module?.createTeamFlow, 'function', 'createTeamFlow must implement the approved native flow')
  return module.createTeamFlow
}

async function fixture(overrides = {}) {
  const createTeamFlow = await factory()
  const service = overrides.service ?? scheduleDouble()
  const options = { getSchedule: () => service, profile: profile(), candidates: ['candidate-a'], ...overrides }
  return { service, flow: createTeamFlow(options), createTeamFlow, options }
}

test('default start uses native one-second kickoff and 604800-second recurrence, with bounded factual output', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const result = await flow.start(exec)
  assert.equal(result.configured, true)
  assert.equal(result.enabled, true)
  assert.equal(result.execution, 'not_verified')
  assert.equal(result.schedules.length, 2)
  assert.deepEqual(result.workflow, { initialObserved: true, recurringActive: true, configurationMatches: true })
  assert.deepEqual(service.calls.create.map(call => [call.sessionId, call.request.after_seconds, call.request.every_seconds]),
    [[exec.sessionId, 1, undefined], [exec.sessionId, undefined, 604800]])
  for (const row of result.schedules) {
    assert.deepEqual(Object.keys(row).sort(),
      ['id', 'kind', 'lastDelivery', 'matchesConfiguration', 'role', 'scheduledAt', 'status', 'title'].sort())
    assert.equal(row.status, 'active')
    assert.equal(row.matchesConfiguration, true)
    assert.equal(row.lastDelivery, null)
    assert.ok(Number.isFinite(Date.parse(row.scheduledAt)))
  }
  for (const row of service.rows.values()) {
    assert.ok(firstLine(row).includes(digest(exec.sessionId)), 'marker contains the session hash, never its raw identity')
    assert.equal(row.prompt.includes(exec.sessionId), false)
    assert.match(row.prompt, /让我的DSH和战队通过真实工作持续变强/)
    assert.match(row.prompt, /legion_runtime/)
    assert.match(row.prompt, /Skill/)
    assert.match(row.prompt, /当前.*工具|工具.*当前/)
    assert.match(row.prompt, /原.*提交入口/)
    assert.match(row.prompt, /来源.*(?:hash|哈希)|(?:hash|哈希).*来源/i)
    assert.match(row.prompt, /新发现.*新发布/s)
    assert.match(row.prompt, /权限/)
    assert.match(row.prompt, /私有/)
    assert.match(row.prompt, /安全规则/)
    assert.match(row.prompt, /原作品/)
  }
  assert.ok(!text(result).includes('prompt'))
  assert.ok(hasCode(result, 'delivery_not_execution'))
})

test('disabled evolution still starts discovery and current work, but explicitly forbids self-modification', async () => {
  const { service, flow } = await fixture({ profile: profile({ evolutionEnabled: false }) })
  const result = await flow.start(execution())
  assert.equal(result.enabled, true)
  assert.equal(result.schedules.length, 2)
  for (const row of service.rows.values()) {
    assert.match(row.prompt, /(?:不|禁止).*(?:自我修改|自行修改)/)
    assert.match(row.prompt, /发现/)
    assert.match(row.prompt, /当前目标/)
  }
})

for (const [cadence, interval] of [['daily', 86400], ['manual', undefined]]) {
  test(`${cadence} creates only its native timing, not another scheduler`, async () => {
    const { service, flow } = await fixture({ profile: profile({ cadence }) })
    const result = await flow.start(execution())
    assert.equal(result.schedules.length, cadence === 'manual' ? 1 : 2)
    assert.equal(service.calls.create[0].request.after_seconds, 1)
    assert.equal(service.calls.create[1]?.request.every_seconds, interval)
  })
}

test('missing profile can read its existing tasks, but start/run never create a default hidden team', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  await flow.start(exec)
  const unconfigured = createTeamFlow({ ...options, profile: undefined })
  const observed = await unconfigured.status(exec)
  assert.equal(observed.configured, false)
  assert.equal(observed.schedules.length, 2)
  assert.equal(observed.enabled, true)
  assert.deepEqual(observed.workflow, { initialObserved: true, recurringActive: true, configurationMatches: null })
  assert.ok(observed.schedules.every(row => row.matchesConfiguration === null))
  for (const value of [await unconfigured.start(exec), await unconfigured.run({ key: 'explicit' }, exec)]) {
    assert.equal(hasCode(value, 'profile_missing'), true)
  }
  assert.equal(service.calls.create.length, 2)
})

test('missing or throwing schedule service is explicit and never leaks exceptions', async () => {
  const createTeamFlow = await factory()
  for (const getSchedule of [() => undefined, () => ({}), () => { throw new Error('Authorization: Bearer private-body') }]) {
    const flow = createTeamFlow({ getSchedule, profile: profile(), candidates: [] })
    for (const method of ['status', 'start', 'pause', 'run']) {
      const value = method === 'run' ? await flow.run({ key: 'run' }, execution()) : await flow[method](execution())
      assert.equal(hasCode(value, 'service_missing'), true)
      assert.equal(value.execution, 'not_verified')
      assert.equal(text(value).includes('private-body'), false)
      assert.equal(value.catalogVerified, false)
      assert.deepEqual(value.workflow, { initialObserved: null, recurringActive: null, configurationMatches: null })
    }
  }
})

test('leader must be a selected candidate at dispatch time, even for an otherwise normalized profile', async () => {
  for (const leader of ['', 'not-selected']) {
    const { service, flow } = await fixture({ profile: profile({ collaboration: 'leader', leader }) })
    for (const value of [await flow.start(execution()), await flow.run({ key: 'explicit' }, execution())]) {
      assert.equal(hasCode(value, 'invalid_leader'), true)
    }
    assert.equal(service.calls.create.length, 0)
  }
  const { service, flow } = await fixture({ profile: profile({ collaboration: 'leader', leader: 'candidate-a' }) })
  await flow.start(execution())
  assert.match([...service.rows.values()][0].prompt, /candidate-a/)
})

test('concurrent starts across factory instances serialize per session and reuse both rows', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const reloaded = createTeamFlow(options)
  const all = await Promise.all(Array.from({ length: 15 }, (_, i) => (i % 2 ? flow : reloaded).start(exec)))
  assert.equal(service.calls.create.length, 2)
  assert.equal(service.calls.update.length, 0)
  assert.ok(all.every(result => result.schedules.length === 2))
  const ids = all[0].schedules.map(row => row.id)
  assert.ok(all.every(result => isDeepStrictEqual(result.schedules.map(row => row.id), ids)))
})

test('inactive delivered kickoff is still the same kickoff after reload; catalog, not active-only list, is authoritative', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const initial = one(await flow.start(exec), 'initial')
  const row = service.rows.get(initial.id)
  row.status = 'inactive'
  row.lastDelivery = { scheduledAt: row.scheduledAt, deliveredAt: new Date().toISOString(), messageId: 'message-delivered' }
  const result = await createTeamFlow(options).start(exec)
  assert.equal(service.calls.create.length, 2)
  assert.equal(one(result, 'initial').id, initial.id)
  assert.equal(one(result, 'initial').status, 'inactive')
  assert.equal(one(result, 'initial').lastDelivery.messageId, 'message-delivered')
  assert.equal(result.execution, 'not_verified')
  assert.equal(service.calls.history, 0)
})

test('manual run requires an explicit bounded key and rejects identity-shaped extras', async () => {
  const { service, flow } = await fixture()
  for (const args of [undefined, {}, { key: '' }, { key: ' ' }, { key: 4 }, { key: 'x'.repeat(129) },
    { key: 'bad\nkey' }, { key: 'fine', sessionId: 'foreign' }]) {
    const value = await flow.run(args, execution())
    assert.equal(hasCode(value, 'invalid_key'), true)
  }
  assert.equal(service.calls.create.length, 0)
})

test('stable manual key deduplicates concurrent calls and inactive rows; a different key makes one new task', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  await Promise.all(Array.from({ length: 8 }, () => flow.run({ key: 'operation-1' }, exec)))
  assert.equal(service.calls.create.length, 1)
  const row = [...service.rows.values()][0]
  assert.ok(firstLine(row).includes(digest('operation-1')))
  row.status = 'inactive'
  await createTeamFlow(options).run({ key: 'operation-1' }, exec)
  assert.equal(service.calls.create.length, 1)
  await flow.run({ key: 'operation-2' }, exec)
  assert.equal(service.calls.create.length, 2)
})

test('session binding and exact first-line markers isolate matching titles, embedded markers, and other sessions', async () => {
  const { service, flow } = await fixture()
  const a = execution(), b = execution()
  await flow.start(a)
  await flow.start(b)
  const ownRow = [...service.rows.values()].find(row => row.sessionId === a.sessionId)
  service.rows.set('stranger', { ...ownRow, id: 'stranger', prompt: 'A different task\n' + ownRow.prompt })
  service.rows.set('lookalike', { ...ownRow, id: 'lookalike', prompt: firstLine(ownRow) + '-not-the-marker\nbody' })
  service.rows.set('foreign-binding', { ...ownRow, id: 'foreign-binding', sessionId: b.sessionId })
  const result = await flow.status(a)
  assert.equal(result.schedules.length, 2)
  assert.equal(text(result).includes(b.sessionId), false)
  const paused = await flow.pause(a)
  assert.equal(paused.schedules.length, 0)
  assert.ok(service.rows.has('stranger') && service.rows.has('lookalike') && service.rows.has('foreign-binding'))
  assert.equal((await flow.status(b)).schedules.length, 2)
})

test('create acknowledgment loss is reconciled from catalog before anything is retried', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    await nativeCreate(...args)
    throw new Error('API response secret-body')
  }
  const exec = execution()
  const first = await flow.start(exec)
  assert.equal(first.schedules.length, 2)
  assert.equal(first.enabled, true)
  assert.ok(first.results.every(item => item.outcome === 'recovered'))
  assert.equal(text(first).includes('secret-body'), false)
  await createTeamFlow(options).start(exec)
  assert.equal(service.calls.create.length, 2)
})

test('partial create failure retains the successful role and retries only the missing role on restart', async () => {
  const { service, flow } = await fixture()
  const nativeCreate = service.create.bind(service)
  let fail = true
  service.create = async (...args) => {
    if (args[1].every_seconds && fail) throw new Error('native secret response')
    return nativeCreate(...args)
  }
  const exec = execution()
  const partial = await flow.start(exec)
  assert.equal(partial.partial, true)
  assert.equal(partial.schedules.length, 1)
  assert.equal(partial.results.find(item => item.role === 'initial').outcome, 'created')
  assert.equal(partial.results.find(item => item.role === 'recurring').outcome, 'failed')
  assert.equal(text(partial).includes('native secret'), false)
  fail = false
  const repaired = await flow.start(exec)
  assert.equal(repaired.schedules.length, 2)
  assert.equal(service.calls.create.length, 2)
})

test('one failed initial arrangement does not throw away a separately successful recurring arrangement', async () => {
  const { service, flow } = await fixture()
  const nativeCreate = service.create.bind(service)
  service.create = (...args) => args[1].after_seconds ? Promise.reject(new Error('failed')) : nativeCreate(...args)
  const value = await flow.start(execution())
  assert.equal(value.partial, true)
  assert.equal(one(value, 'recurring').status, 'active')
  assert.equal(value.results.find(item => item.role === 'initial').outcome, 'failed')
})

test('catalog failure after an ambiguous write prevents blind retries; the next call recovers the actual row', async () => {
  const { service, flow } = await fixture()
  const nativeCreate = service.create.bind(service)
  const nativeCatalog = service.catalog.bind(service)
  let uncertain = false
  service.create = async (...args) => {
    await nativeCreate(...args)
    uncertain = true
    throw new Error('lost ack')
  }
  service.catalog = () => uncertain ? Promise.reject(new Error('raw API body')) : nativeCatalog()
  const exec = execution()
  const first = await flow.start(exec)
  assert.equal(first.catalogVerified, false)
  assert.equal(first.partial, true)
  assert.equal(service.calls.create.length, 1)
  assert.equal(text(first).includes('raw API body'), false)
  uncertain = false
  service.create = nativeCreate
  const recovered = await flow.start(exec)
  assert.equal(recovered.schedules.length, 2)
  assert.equal(service.calls.create.length, 2)
})

test('every method obeys pre-aborted execution and lifecycle signals without calling the service', async () => {
  const createTeamFlow = await factory()
  for (const source of ['execution', 'lifecycle']) {
    const stopped = new AbortController()
    stopped.abort(new Error('secret cancellation reason'))
    let gets = 0
    const flow = createTeamFlow({ getSchedule: () => { gets++; return scheduleDouble() },
      profile: profile(), candidates: [], lifecycleSignal: source === 'lifecycle' ? stopped.signal : undefined })
    for (const method of ['status', 'start', 'pause', 'run']) {
      const exec = execution(undefined, source === 'execution' ? stopped.signal : new AbortController().signal)
      const value = method === 'run' ? await flow.run({ key: 'one' }, exec) : await flow[method](exec)
      assert.equal(hasCode(value, 'cancelled'), true)
      assert.equal(text(value).includes('secret cancellation'), false)
    }
    assert.equal(gets, 0)
  }
})

test('cancel while waiting in the per-session queue returns promptly and never enters a native write', async () => {
  const { service, flow } = await fixture()
  const entered = deferred(), release = deferred()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    if (args[1].after_seconds) { entered.resolve(); await release.promise }
    return nativeCreate(...args)
  }
  const exec = execution()
  const active = flow.start(exec)
  await entered.promise
  const cancelled = new AbortController()
  const queued = flow.run({ key: 'must-not-run' }, execution(exec.sessionId, cancelled.signal))
  cancelled.abort()
  const value = await queued
  assert.equal(hasCode(value, 'cancelled'), true)
  release.resolve()
  await active
  await flow.status(exec)
  assert.equal(service.calls.create.length, 2)
})

test('in-flight cancellation cannot roll back a committed task and must stop the second arrangement', async () => {
  const { service, flow } = await fixture()
  const stop = new AbortController()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    const row = await nativeCreate(...args)
    stop.abort(new Error('private abort body'))
    return row
  }
  const session = execution().sessionId
  const value = await flow.start(execution(session, stop.signal))
  assert.equal(hasCode(value, 'cancelled'), true)
  const observed = await flow.status(execution(session))
  assert.equal(observed.schedules.length, 1)
  assert.equal(service.calls.create.length, 1)
  assert.equal(value.execution, 'not_verified')
  assert.ok(hasCode(value, 'cancellation_not_rollback'))
})

test('pause retains inactive delivery facts, touches only active flow rows, and explains native history deletion', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const initial = service.rows.get(one(started, 'initial').id)
  initial.status = 'inactive'
  initial.lastDelivery = { scheduledAt: initial.scheduledAt, deliveredAt: new Date().toISOString(), messageId: 'message-in-session' }
  const result = await flow.pause(exec)
  assert.equal(result.enabled, false)
  assert.equal(result.schedules.length, 1)
  assert.equal(result.schedules[0].id, initial.id)
  assert.equal(result.schedules[0].lastDelivery.messageId, 'message-in-session')
  assert.ok(hasCode(result, 'native_delete_removes_history'))
  assert.match(text(result.observations), /(?:不会|不).*撤回|已投递/)
  assert.equal(service.calls.delete.length, 1)
})

test('pause deletes an overdue active one-shot schedule without claiming to retract an already-delivered round', async () => {
  const { service, flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const exec = execution()
  await flow.start(exec)
  const row = [...service.rows.values()][0]
  row.scheduledAt = '2020-01-01T00:00:00.000Z'
  const result = await flow.pause(exec)
  assert.equal(service.calls.delete.length, 1)
  assert.equal(result.enabled, false)
  assert.equal(result.partial, false)
  assert.equal(service.rows.size, 0)
  assert.equal(result.results[0].outcome, 'deleted')
  assert.ok(hasCode(result, 'native_delete_removes_history'))
  assert.match(text(result.observations), /不会撤回已投递/)
})

test('pause recovers a lost delete acknowledgment and retains independent delete failures for retry', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const recurringId = one(started, 'recurring').id
  const nativeDelete = service.delete.bind(service)
  service.delete = async (...args) => {
    if (args[0].id === recurringId) throw new Error('private delete failure')
    await nativeDelete(...args)
    throw new Error('lost acknowledgment')
  }
  const partial = await flow.pause(exec)
  assert.equal(partial.partial, true)
  assert.equal(partial.schedules.length, 1)
  assert.equal(partial.schedules[0].id, recurringId)
  assert.ok(partial.results.some(item => item.outcome === 'recovered'))
  assert.ok(partial.results.some(item => item.outcome === 'failed'))
  service.delete = nativeDelete
  assert.equal((await flow.pause(exec)).enabled, false)
})

test('changing cadence uses native update with the full observed record but no catalog metadata', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const before = await flow.start(exec)
  const recurring = service.rows.get(one(before, 'recurring').id)
  recurring.lastDelivery = { scheduledAt: recurring.scheduledAt, deliveredAt: new Date().toISOString(), messageId: 'previous' }
  const changed = await createTeamFlow({ ...options, profile: profile({ cadence: 'daily' }) }).start(exec)
  const update = service.calls.update.find(call => call.id === recurring.id)
  assert.equal(update.sessionId, exec.sessionId)
  assert.deepEqual(update.change, { kind: 'every', every_seconds: 86400 })
  assert.equal(update.expected.everySeconds, 604800)
  for (const field of ['sessionId', 'status', 'lastDelivery']) assert.equal(field in update.expected, false)
  assert.equal(one(changed, 'recurring').id, recurring.id)
  assert.equal(service.calls.create.length, 2)
})

test('a delivery-time conflict rereads and retries without losing the latest delivery receipt', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const before = await flow.start(exec)
  const id = one(before, 'recurring').id
  const nativeUpdate = service.update.bind(service)
  let conflicts = 0
  service.update = async (...args) => {
    if (args[0].id === id && conflicts++ === 0) {
      const row = service.rows.get(id)
      row.scheduledAt = new Date(Date.parse(row.scheduledAt) + 1000).toISOString()
      row.lastDelivery = { scheduledAt: row.scheduledAt, deliveredAt: new Date().toISOString(), messageId: 'concurrent-delivery' }
      return { id, updated: false, code: 'schedule_conflict' }
    }
    return nativeUpdate(...args)
  }
  const value = await createTeamFlow({ ...options, profile: profile({ cadence: 'daily' }) }).start(exec)
  assert.equal(service.rows.get(id).everySeconds, 86400)
  assert.equal(one(value, 'recurring').lastDelivery.messageId, 'concurrent-delivery')
  assert.equal(service.calls.create.length, 2)
})

test('concurrent human content edits are not overwritten by conflict retries', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const before = await flow.start(exec)
  const id = one(before, 'recurring').id
  const nativeUpdate = service.update.bind(service)
  let tried = false
  service.update = async (...args) => {
    if (args[0].id === id && !tried) {
      tried = true
      service.rows.get(id).prompt += '\n用户刚在原生界面补充的要求'
      return { id, updated: false, code: 'schedule_conflict' }
    }
    return nativeUpdate(...args)
  }
  const value = await createTeamFlow({ ...options, profile: profile({ cadence: 'daily' }) }).start(exec)
  assert.equal(value.partial, true)
  assert.ok(hasCode(value, 'schedule_conflict'))
  assert.match(service.rows.get(id).prompt, /用户刚在原生界面补充的要求/)
  assert.equal(service.rows.get(id).everySeconds, 604800)
})

test('persistent schedule conflict has a finite retry budget', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  await flow.start(exec)
  let attempts = 0
  service.update = async ({ id }) => { attempts++; return { id, updated: false, code: 'schedule_conflict' } }
  const value = await createTeamFlow({ ...options, profile: profile({ cadence: 'daily' }) }).start(exec)
  assert.ok(attempts >= 1 && attempts <= 6, 'at most three attempts per independently changed task')
  assert.ok(hasCode(value, 'schedule_conflict'))
})

test('switching to manual removes recurring work without recreating the retained kickoff', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const initialId = one(await flow.start(exec), 'initial').id
  service.rows.get(initialId).status = 'inactive'
  const value = await createTeamFlow({ ...options, profile: profile({ cadence: 'manual' }) }).start(exec)
  assert.equal(value.schedules.length, 1)
  assert.equal(value.schedules[0].id, initialId)
  assert.equal(value.enabled, false)
  assert.equal(service.calls.create.length, 2)
})

test('status bounds its projection, strips saved prompt bodies, and ignores every foreign-session row', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  await flow.run({ key: 'one' }, exec)
  const sample = [...service.rows.values()][0]
  for (let i = 0; i < 300; i++) {
    service.rows.set(`own-${i}`, { ...sample, id: `own-${i}`, title: `Bearer sk-private-secret-${i}`,
      lastDelivery: { scheduledAt: sample.scheduledAt, deliveredAt: sample.scheduledAt,
        messageId: `message-${i}`, prompt: 'saved-private-prompt', response: 'raw-response-body' } })
    service.rows.set(`foreign-${i}`, { ...sample, id: `foreign-${i}`, sessionId: 'foreign-private-session',
      title: 'foreign-private-title' })
  }
  const value = await flow.status(exec)
  assert.ok(value.schedules.length <= 50)
  assert.ok(value.omittedSchedules > 0)
  assert.ok(text(value).length < 50000)
  for (const privateValue of ['saved-private-prompt', 'raw-response-body', 'foreign-private', 'sk-private-secret']) {
    assert.equal(text(value).includes(privateValue), false)
  }
  assert.equal(service.calls.history, 0)
})

test('a successful native create receipt is retained even when the following catalog read fails', async () => {
  const { service, flow } = await fixture()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    const committed = await nativeCreate(...args)
    service.catalog = () => Promise.reject(new Error('private failed read'))
    return committed
  }
  const value = await flow.start(execution())
  assert.equal(value.catalogVerified, false)
  assert.equal(value.partial, true)
  assert.equal(value.results[0].outcome, 'created')
  assert.equal(value.results[0].id, [...service.rows.values()][0].id)
  assert.equal(value.results[1].outcome, 'not_attempted')
})

test('a successful native update receipt is retained when later catalog observation fails', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  service.rows.get(one(started, 'initial').id).status = 'inactive'
  const nativeUpdate = service.update.bind(service)
  service.update = async (...args) => {
    const committed = await nativeUpdate(...args)
    service.catalog = () => Promise.reject(new Error('private failed read'))
    return committed
  }
  const value = await createTeamFlow({ ...options, profile: profile({ cadence: 'daily' }) }).start(exec)
  assert.equal(value.catalogVerified, false)
  assert.equal(value.partial, true)
  assert.equal(value.results.find(item => item.role === 'recurring').outcome, 'updated')
})

test('a successful native delete receipt is retained when later catalog observation fails', async () => {
  const { service, flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const exec = execution()
  await flow.start(exec)
  const nativeDelete = service.delete.bind(service)
  service.delete = async (...args) => {
    const committed = await nativeDelete(...args)
    service.catalog = () => Promise.reject(new Error('private failed read'))
    return committed
  }
  const value = await flow.pause(exec)
  assert.equal(value.catalogVerified, false)
  assert.equal(value.partial, true)
  assert.equal(value.results[0].outcome, 'deleted')
  assert.equal(service.rows.size, 0)
})

test('restarting an ended recurring schedule reports the new active id, not its older inactive marker', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const old = service.rows.get(one(started, 'recurring').id)
  old.status = 'inactive'
  const value = await flow.start(exec)
  const newActive = value.schedules.find(row => row.role === 'recurring' && row.status === 'active')
  assert.ok(newActive)
  assert.notEqual(newActive.id, old.id)
  assert.equal(value.results.find(item => item.role === 'recurring').id, newActive.id)
  assert.equal(service.calls.create.length, 3)
})

test('a stable bounded Unicode operation key can be used without appearing in the native prompt', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const key = '复验 第一次'
  await flow.run({ key }, exec)
  await flow.run({ key }, exec)
  assert.equal(service.calls.create.length, 1)
  const row = [...service.rows.values()][0]
  assert.ok(firstLine(row).includes(digest(key)))
  assert.equal(row.prompt.includes(key), false)
})

test('pause removes overdue recurrence while retaining the inactive kickoff and its receipt', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const initial = service.rows.get(one(started, 'initial').id)
  initial.status = 'inactive'
  initial.lastDelivery = { scheduledAt: initial.scheduledAt, deliveredAt: initial.scheduledAt, messageId: 'saved-inbox-message' }
  service.rows.get(one(started, 'recurring').id).scheduledAt = '2020-01-01T00:00:00.000Z'
  const value = await flow.pause(exec)
  assert.equal(value.partial, false)
  assert.equal(value.enabled, false)
  assert.equal(value.results[0].outcome, 'deleted')
  assert.deepEqual(value.schedules.map(row => row.id), [initial.id])
  assert.equal(value.schedules[0].lastDelivery.messageId, 'saved-inbox-message')
  assert.deepEqual(value.workflow, { initialObserved: true, recurringActive: false, configurationMatches: null })
})

test('a pending write in one session never blocks another session', async () => {
  const { service, flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const a = execution(), b = execution()
  const entered = deferred(), release = deferred()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    if (args[0] === a.sessionId) { entered.resolve(); await release.promise }
    return nativeCreate(...args)
  }
  const pending = flow.start(a)
  await entered.promise
  try {
    const other = await flow.start(b)
    assert.equal(other.schedules.length, 1)
    assert.equal(service.calls.create[0].sessionId, b.sessionId)
  } finally { release.resolve(); await pending }
})

test('refresh reconstructs observed workflow gaps, without claiming a missing recurrence failed or was paused', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const nativeCreate = service.create.bind(service)
  service.create = (...args) => args[1].every_seconds
    ? Promise.reject(new Error('fixture recurrence failure')) : nativeCreate(...args)
  const exec = execution()
  const partial = await flow.start(exec)
  assert.equal(partial.partial, true)
  const refreshed = await createTeamFlow(options).status(exec)
  assert.deepEqual(refreshed.workflow, { initialObserved: true, recurringActive: false, configurationMatches: true })
  assert.equal(refreshed.partial, false, 'a successful read must not invent a failed mutation')
  const missing = refreshed.observations.find(note => note.code === 'recurring_not_observed')
  assert.ok(missing)
  assert.match(missing.message, /未观察到/)
  assert.match(missing.message, /原因.*未核实/)
  assert.doesNotMatch(missing.message, /失败|已暂停/)
  assert.equal(refreshed.observations.some(note => note.code === 'create_failed'), false,
    'a fresh catalog cannot reconstruct the previous call’s error')
  assert.equal(service.calls.create.length, 1, 'status never repairs the gap itself')
})

test('an intentionally removed recurrence has the same neutral observation as any other catalog absence', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const initial = service.rows.get(one(started, 'initial').id)
  initial.status = 'inactive'
  await flow.pause(exec)
  const value = await flow.status(exec)
  assert.deepEqual(value.workflow, { initialObserved: true, recurringActive: false, configurationMatches: null })
  assert.ok(hasCode(value, 'recurring_not_observed'))
  assert.equal(value.observations.some(note => note.code === 'create_failed'), false)
  assert.equal(Object.hasOwn(value.schedules[0], 'matchesConfiguration'), false, 'inactive snapshots are historical, not pending configuration')
})

test('empty manual workflow does not imply a missing automatic recurrence', async () => {
  const { flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const value = await flow.status(execution())
  assert.deepEqual(value.workflow, { initialObserved: false, recurringActive: false, configurationMatches: null })
  assert.equal(hasCode(value, 'recurring_not_observed'), false)
})

test('changed defaults expose old active snapshots and synchronization affects only the explicitly selected session', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const a = execution(), b = execution()
  await flow.start(a)
  await flow.start(b)
  const original = structuredClone([...service.rows.values()])
  const changed = createTeamFlow({ ...options, profile: profile({ evolutionEnabled: false, cadence: 'manual' }) })
  const observed = await changed.status(a)
  assert.deepEqual(observed.workflow, { initialObserved: true, recurringActive: true, configurationMatches: false })
  assert.ok(observed.schedules.every(row => row.matchesConfiguration === false))
  assert.ok(hasCode(observed, 'configuration_outdated'))
  assert.deepEqual([...service.rows.values()], original, 'reading changed defaults does not update any task')
  assert.equal(service.calls.update.length, 0)
  assert.equal(service.calls.delete.length, 0)
  assert.equal(text(observed).includes(b.sessionId), false)

  const synced = await changed.start(a)
  assert.deepEqual(synced.workflow, { initialObserved: true, recurringActive: false, configurationMatches: true })
  assert.equal(hasCode(synced, 'configuration_outdated'), false)
  const bAfter = await changed.status(b)
  assert.equal(bAfter.workflow.configurationMatches, false)
  assert.deepEqual([...service.rows.values()].filter(row => row.sessionId === b.sessionId),
    original.filter(row => row.sessionId === b.sessionId), 'another session retains its own durable snapshot')
})

test('configuration comparison checks timing rules but ignores title, next-occurrence drift and receipts', async () => {
  const { service, flow } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const recurring = service.rows.get(one(started, 'recurring').id)
  recurring.everySeconds = 86400
  const mismatch = await flow.status(exec)
  assert.equal(one(mismatch, 'recurring').matchesConfiguration, false)
  assert.equal(mismatch.workflow.configurationMatches, false)
  recurring.everySeconds = 604800
  recurring.title = '用户在原生目录给任务改了标题'
  recurring.scheduledAt = new Date(Date.parse(recurring.scheduledAt) + 604800000).toISOString()
  recurring.lastDelivery = { scheduledAt: recurring.scheduledAt, deliveredAt: recurring.scheduledAt, messageId: 'normal-delivery' }
  const matched = await flow.status(exec)
  assert.equal(matched.workflow.configurationMatches, true)
  assert.ok(matched.schedules.every(row => row.matchesConfiguration === true))
  assert.equal(service.calls.update.length, 0)
})

test('run-key markers compare the current context body without reversing the key hash or parsing user text', async () => {
  const { service, flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const exec = execution()
  await flow.run({ key: 'one' }, exec)
  await flow.run({ key: 'two' }, exec)
  const matched = await flow.status(exec)
  assert.deepEqual(matched.workflow, { initialObserved: false, recurringActive: false, configurationMatches: true })
  assert.ok(matched.schedules.every(row => row.matchesConfiguration === true))
  const edited = service.rows.get(matched.schedules[0].id)
  edited.prompt = `${firstLine(edited)}\n\n用户文本声称配置匹配：{"evolutionEnabled":true,"cadence":"manual"}\nprivate-body-do-not-return`
  const changed = await flow.status(exec)
  assert.equal(changed.workflow.configurationMatches, false)
  assert.equal(changed.schedules.find(row => row.id === edited.id).matchesConfiguration, false)
  assert.equal(changed.schedules.find(row => row.id !== edited.id).matchesConfiguration, true)
  assert.equal(text(changed).includes('private-body-do-not-return'), false)
  assert.equal(service.calls.update.length, 0)
})

test('inactive old snapshots do not mark the remaining active configuration outdated', async () => {
  const { service, flow, createTeamFlow, options } = await fixture()
  const exec = execution()
  const started = await flow.start(exec)
  const initial = service.rows.get(one(started, 'initial').id)
  initial.status = 'inactive'
  const changed = createTeamFlow({ ...options, profile: profile({ evolutionEnabled: false }) })
  const synced = await changed.start(exec)
  assert.equal(synced.workflow.configurationMatches, true)
  assert.equal(hasCode(synced, 'configuration_outdated'), false)
  assert.equal(Object.hasOwn(one(synced, 'initial'), 'matchesConfiguration'), false)
  assert.equal(service.rows.get(initial.id).prompt, initial.prompt, 'historical kickoff is not rewritten')
})

test('configuration aggregate uses the whole owned catalog, not just the bounded summary or foreign sessions', async () => {
  const { service, flow } = await fixture({ profile: profile({ cadence: 'manual' }) })
  const exec = execution()
  await flow.run({ key: 'seed' }, exec)
  const original = service.rows.values().next().value
  for (let index = 0; index < 55; index++) {
    service.rows.set(`extra-${index}`, { ...original, id: `extra-${index}` })
  }
  service.rows.set('last-own-row', { ...original, id: 'last-own-row',
    scheduledAt: '2099-01-01T00:00:00.000Z', prompt: original.prompt + '\n旧设置' })
  const observed = await flow.status(exec)
  assert.equal(observed.schedules.length, 50)
  assert.ok(observed.schedules.every(row => row.matchesConfiguration === true))
  assert.equal(observed.workflow.configurationMatches, false)
  service.rows.delete('last-own-row')
  service.rows.set('foreign', { ...original, id: 'foreign', sessionId: 'other-session', prompt: original.prompt + '\n旧设置' })
  assert.equal((await flow.status(exec)).workflow.configurationMatches, true)
})

test('an unverified catalog does not turn retained snapshots into fresh workflow or configuration facts', async () => {
  const { service, flow } = await fixture()
  const nativeCreate = service.create.bind(service)
  service.create = async (...args) => {
    const committed = await nativeCreate(...args)
    if (args[1].every_seconds) service.catalog = () => Promise.reject(new Error('private failed read'))
    return committed
  }
  const value = await flow.start(execution())
  assert.equal(value.catalogVerified, false)
  assert.ok(value.schedules.length > 0)
  assert.deepEqual(value.workflow, { initialObserved: null, recurringActive: null, configurationMatches: null })
  assert.ok(value.schedules.every(row => row.matchesConfiguration === null))
  assert.equal(hasCode(value, 'recurring_not_observed'), false)
  assert.equal(hasCode(value, 'configuration_outdated'), false)
})

test('an unusable current default yields unknown matches rather than claiming old tasks match or mutating them', async () => {
  const { service, flow, createTeamFlow, options } = await fixture({
    profile: profile({ collaboration: 'leader', leader: 'candidate-a' }),
  })
  const exec = execution()
  await flow.start(exec)
  const rowsBefore = structuredClone([...service.rows.values()])
  const value = await createTeamFlow({ ...options, candidates: [] }).status(exec)
  assert.equal(value.workflow.configurationMatches, null)
  assert.ok(value.schedules.every(row => row.matchesConfiguration === null))
  assert.deepEqual([...service.rows.values()], rowsBefore)
})
