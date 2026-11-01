import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readdir, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { setTimeout as delay } from 'node:timers/promises'
import { Context } from '@deepseek-ai/cordis'
import { ScheduleService } from '@deepseek-ai/dsh-schedule'
import { Storage } from '@deepseek-ai/dsh-storage'
import * as storageJson from '@deepseek-ai/dsh-storage-json'
import * as storageDomain from '@deepseek-ai/dsh-storage-domain'
import { SessionStore } from '@deepseek-ai/dsh-session'
import * as persistenceJsonl from '@deepseek-ai/dsh-session-persistence-jsonl'
import { createTeamFlow } from '../lib/team-flow.js'

const profile = { name: '隔离测试战队', goal: '验证原生任务投递，不调用模型', collaboration: 'auto',
  leader: '', instructions: '', evolutionEnabled: true, cadence: 'weekly' }
const execution = sessionId => ({ sessionId, signal: new AbortController().signal })

/**
 * Real official ScheduleService + Storage/JSON/Domain + SessionStore/JSONL.
 * Only agent resolution/wakeup is replaced: the fixture records an inbox splice,
 * with no AgentLoop or model. This is not a live Host/agent-completion test.
 */
async function nativeHost(root, { rejectDelivery = false } = {}) {
  const ctx = new Context()
  const fibers = []
  const sinks = new Map()
  const resolutions = []
  const mount = async (plugin, config) => {
    const fiber = ctx.plugin(plugin, config)
    fibers.push(fiber)
    await fiber
    return fiber
  }
  await mount(Storage)
  await mount(storageJson, { root: join(root, 'storage') })
  await mount(storageDomain, { backend: 'json' })
  await mount(SessionStore)
  await mount(persistenceJsonl, { root: join(root, 'sessions'), compression: 'none', listConcurrency: 1 })
  await mount({
    name: 'team-flow-isolated-inbox-sink',
    inject: ['sessions', 'sessionPersistence'],
    apply(scope) {
      scope.provide('agents', { get: () => undefined, withoutInitiator: work => Promise.resolve().then(work) })
      scope.provide('sessionController', {
        async resolveAgent(sessionId) {
          resolutions.push(sessionId)
          if (rejectDelivery) return { error: new Error('Isolated delivery admission refused; no model invoked') }
          if (!sinks.has(sessionId)) {
            const session = scope.sessions.create(sessionId, { meta: { cwd: root } })
            const writer = await scope.sessionPersistence.create(session.header)
            await writer.flush()
            scope.effect(() => () => writer.close())
            let pendingCount = 0
            sinks.set(sessionId, {
              session,
              followup(message) {
                session.append('agent/inbox/spliced', {
                  target: 'next-turn', start: pendingCount++, removedCount: 0, inserted: [message],
                })
              },
            })
          }
          return { agent: sinks.get(sessionId) }
        },
      })
    },
  })
  await mount(ScheduleService, {})
  return {
    schedule: ctx.schedule, resolutions,
    async readSession(sessionId) {
      const reader = await ctx.sessionPersistence.open(sessionId, 'read')
      try { return (await reader.read()).events }
      finally { await reader.close() }
    },
    async close() {
      for (const fiber of fibers.splice(0).reverse()) await fiber.dispose()
    },
  }
}

async function until(read, predicate) {
  const deadline = performance.now() + 5000
  let value
  do {
    value = await read()
    if (predicate(value)) return value
    await delay(25)
  } while (performance.now() < deadline)
  assert.fail(`native delivery not observed within five seconds: ${JSON.stringify(value)}`)
}

test('official 0.2.1-rc.2 service persists, delivers into durable session inbox, survives recreation, and pauses only owned active work',
  { timeout: 20000 }, async t => {
    const root = await mkdtemp(join(tmpdir(), 'team-flow-native-'))
    t.after(() => rm(root, { recursive: true, force: true }))
    let host = await nativeHost(root)
    t.after(() => host.close())
    const exec = execution('native-team-flow-test')
    const flow = createTeamFlow({ getSchedule: () => host.schedule, profile, candidates: [] })
    const started = await flow.start(exec)
    assert.equal(started.catalogVerified, true)
    assert.equal(started.schedules.length, 2)
    assert.equal(started.partial, false)
    assert.deepEqual(started.workflow, { initialObserved: true, recurringActive: true, configurationMatches: true })
    const initial = started.schedules.find(row => row.role === 'initial')
    const recurring = started.schedules.find(row => row.role === 'recurring')
    assert.ok((await readdir(join(root, 'storage'))).length > 0, 'official storage wrote actual files')
    const delivered = await until(() => flow.status(exec), value => value.schedules.some(row => row.id === initial.id && row.lastDelivery))
    assert.equal(delivered.execution, 'not_verified')
    assert.equal(delivered.schedules.find(row => row.id === initial.id).status, 'inactive')
    const history = await host.schedule.history({ sessionId: exec.sessionId, id: initial.id, limit: 5 })
    assert.equal(history.records.length, 1)
    assert.ok(history.records[0].prompt.startsWith('[dsh-legion-team-flow:v1 '))
    const inboxEvents = await host.readSession(exec.sessionId)
    const inbox = inboxEvents.find(event => event.type === 'agent/inbox/spliced').data.inserted
    assert.equal(inbox.length, 1)
    assert.equal(inbox[0].source.kind, 'schedule')
    assert.equal(inbox[0].id, history.records[0].messageId)
    assert.equal(JSON.stringify(delivered).includes(history.records[0].prompt), false)

    await host.close()
    host = await nativeHost(root)
    const resumed = await createTeamFlow({ getSchedule: () => host.schedule, profile, candidates: [] }).start(exec)
    assert.deepEqual(resumed.schedules.map(row => row.id).sort(), started.schedules.map(row => row.id).sort())
    assert.equal(resumed.schedules.find(row => row.id === initial.id).status, 'inactive')
    assert.equal(host.resolutions.length, 0, 'reads/reuse never wake an agent or re-deliver the inactive kickoff')
    assert.equal((await host.readSession(exec.sessionId)).filter(event => event.type === 'agent/inbox/spliced').length, 1)

    const changed = createTeamFlow({ getSchedule: () => host.schedule, profile: { ...profile, cadence: 'daily' }, candidates: [] })
    const originalCatalog = await host.schedule.catalog()
    const outdated = await changed.status(exec)
    assert.deepEqual(outdated.workflow, { initialObserved: true, recurringActive: true, configurationMatches: false })
    assert.equal(outdated.schedules.find(row => row.id === recurring.id).matchesConfiguration, false)
    assert.deepEqual(await host.schedule.catalog(), originalCatalog, 'status never synchronizes saved task snapshots')
    assert.equal((await changed.start(exec)).partial, false, 'native strict expected decoder accepts the cadence update')
    assert.equal((await host.schedule.catalog()).find(row => row.id === recurring.id).everySeconds, 86400)
    assert.equal((await changed.status(exec)).workflow.configurationMatches, true)
    const stranger = await host.schedule.create(exec.sessionId,
      { title: recurring.title, prompt: 'Same title, unrelated user task', after_seconds: 60 }, exec.signal)
    const paused = await changed.pause(exec)
    assert.equal(paused.enabled, false)
    assert.deepEqual(paused.schedules.map(row => row.id), [initial.id])
    assert.deepEqual(paused.workflow, { initialObserved: true, recurringActive: false, configurationMatches: null })
    assert.ok(paused.observations.some(note => note.code === 'recurring_not_observed'))
    assert.ok((await host.schedule.catalog()).some(row => row.id === stranger.id))
    assert.equal((await host.schedule.history({ sessionId: exec.sessionId, id: recurring.id, limit: 5 })).code, 'schedule_not_found')
    assert.equal((await host.schedule.history({ sessionId: exec.sessionId, id: initial.id, limit: 5 })).records.length, 1)

    // Native deletion removes its saved history, but never the already-durable session inbox.
    await host.schedule.delete({ sessionId: exec.sessionId, id: initial.id }, exec.signal)
    assert.equal((await host.schedule.history({ sessionId: exec.sessionId, id: initial.id, limit: 5 })).code, 'schedule_not_found')
    assert.equal((await host.readSession(exec.sessionId)).filter(event => event.type === 'agent/inbox/spliced').length, 1)
    t.diagnostic(JSON.stringify({
      package: '@deepseek-ai/dsh-schedule@0.2.1-rc.2',
      persistence: 'official Storage/JSON/Domain and SessionStore/JSONL',
      delivery: 'official ScheduleRuntime to fixture inbox sink; no AgentLoop, model, network or running Host',
      initialId: initial.id, recurringId: recurring.id, receipt: history.records[0].messageId,
      recreatedSameIds: true, retainedSessionAfterNativeDelete: true, execution: 'not_verified',
    }))
  })

test('official service removes overdue active schedules after failed admission, without deleting unrelated work or claiming a recalled round',
  { timeout: 20000 }, async t => {
    const root = await mkdtemp(join(tmpdir(), 'team-flow-native-overdue-'))
    t.after(() => rm(root, { recursive: true, force: true }))
    const clock = Date.now()
    // Advance the clock, not a real week. ScheduleService, its storage and delete/readback remain official.
    t.mock.timers.enable({ apis: ['Date'], now: clock })
    const host = await nativeHost(root, { rejectDelivery: true })
    t.after(() => host.close())
    const exec = execution('native-overdue-flow-test')
    const flow = createTeamFlow({ getSchedule: () => host.schedule, profile, candidates: [] })
    const started = await flow.start(exec)
    assert.equal(started.schedules.length, 2)
    t.mock.timers.setTime(clock + 604800000 + 5000)
    // A real native mutation asks ScheduleRuntime to rescan both now-overdue flow tasks.
    const unrelated = await host.schedule.create(exec.sessionId,
      { title: 'Unrelated future work', prompt: 'Not a flow task', after_seconds: 60 }, exec.signal)
    await until(async () => host.resolutions, ids => ids.filter(id => id === exec.sessionId).length >= 2)
    const overdue = await flow.status(exec)
    assert.equal(overdue.schedules.length, 2)
    assert.ok(overdue.schedules.every(row => row.status === 'active' && Date.parse(row.scheduledAt) < Date.now()))
    assert.ok(overdue.schedules.every(row => row.lastDelivery === null))
    assert.deepEqual(overdue.workflow, { initialObserved: true, recurringActive: true, configurationMatches: true })
    for (const row of overdue.schedules) {
      assert.equal((await host.schedule.history({ sessionId: exec.sessionId, id: row.id, limit: 5 })).records.length, 0)
    }
    const paused = await flow.pause(exec)
    assert.equal(paused.partial, false)
    assert.equal(paused.enabled, false)
    assert.deepEqual(paused.workflow, { initialObserved: false, recurringActive: false, configurationMatches: null })
    assert.ok(paused.results.every(result => result.outcome === 'deleted'))
    assert.match(paused.observations.find(note => note.code === 'native_delete_removes_history').message, /不会撤回已投递/)
    assert.deepEqual((await host.schedule.catalog()).map(row => row.id), [unrelated.id])
    for (const row of overdue.schedules) {
      assert.equal((await host.schedule.history({ sessionId: exec.sessionId, id: row.id, limit: 5 })).code, 'schedule_not_found')
    }
    t.diagnostic(JSON.stringify({
      package: '@deepseek-ai/dsh-schedule@0.2.1-rc.2',
      overdueSetup: 'Date clock advanced seven days; fixture rejects agent admission; no model invoked',
      deletedByOfficialService: paused.results.map(result => result.id),
      unrelatedScheduleRetained: unrelated.id,
      execution: 'not_verified',
    }))
  })
