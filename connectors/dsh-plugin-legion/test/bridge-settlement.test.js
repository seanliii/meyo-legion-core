import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool } from '@deepseek-ai/dsh-tools'
import { createScope } from '@deepseek-ai/dsh-scope'
import { LegionClient } from '../lib/client.js'
import { registerEcosystem } from '../lib/extension.js'
import { callNative } from '../lib/ecosystem.js'

// Exercise the installed official capture/rollback observer, not a test model of it.
const { attachStructuredRuntime } = await import(
  new URL('./types/structured.js', import.meta.resolve('@deepseek-ai/dsh-subagent')))
const answerSchema = {
  type: 'object', properties: { answer: { type: 'integer' } },
  required: ['answer'], additionalProperties: false,
}
const operationKey = 'settlement-operation'
const hash = value => createHash('sha256').update(value).digest('hex')
// Keep the durable native identity; its old automatic message key must stay unused.
const operationId = hash(JSON.stringify(['native-call', operationKey]))
const resultName = `bridge-result-${operationId}`
const messageKey = `native-result:${operationId}`
const messageId = hash(JSON.stringify(['message', messageKey]))
const options = { timeout: 10000 }

async function readJson(path) {
  try { return JSON.parse(await readFile(path, 'utf8')) }
  catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
}

async function eventually(read, accepts, label, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  let last
  do {
    last = await read()
    if (accepts(last)) return last
    await delay(10)
  } while (Date.now() < deadline)
  assert.fail(`${label} did not settle within ${timeoutMs}ms; last=${JSON.stringify(last)}`)
}

function nativeTool(name, execute) {
  return defineTool({
    name, description: 'Synthetic native settlement fixture', parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    execute,
  })
}

function receiptFacts(value) {
  return value && {
    version: value.version, native: value.native?.state,
    observed: value.native?.observed?.state, transport: value.transport?.state,
    delivery: value.delivery,
  }
}

const resultFact = (exec, result) => ({
  call_id: exec.callId, name: exec.name, state: result.isError ? 'failed' : 'succeeded',
})

// Real registered extension -> official ToolRuntime -> real client/disk TaskStore.
// No automatic feedback is permitted, even after a successful root. A later
// explicit legion_message decision belongs to the host, not this observer.
// Only HTTP is replaced; cancellation is injected after real atomic persistence.
async function fixture(t, { structured = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'legion-bridge-settlement-'))
  const config = {
    baseUrl: 'https://legion.invalid', stateDirectory: directory,
    tokenEnv: 'BRIDGE_SETTLEMENT_FIXTURE_TOKEN',
  }
  const env = { BRIDGE_SETTLEMENT_FIXTURE_TOKEN: 'synthetic-settlement-credential-not-real' }
  const ctx = new Context()
  const agent = { id: 'bridge-settlement-session' }
  const f = {
    ctx, agent, clients: [], executions: [], results: [], bodies: [], writes: [],
    requests: [], fetchCalls: 0, nativeCalls: 0, serial: 0,
  }
  let prompt, runtime, owner, scope
  t.after(async () => {
    const saved = await f.readResult?.()
    t.diagnostic(JSON.stringify({
      case: t.name, saved: receiptFacts(saved), bodies: f.bodies.map(receiptFacts),
      result_events: f.results.map(({ exec, result }) => resultFact(exec, result)),
      writes: f.writes.map(write => receiptFacts(write.record)),
      native_calls: f.nativeCalls, fetch_calls: f.fetchCalls,
      captured: f.structured?.captured() ?? null,
    }))
    f.lifecycle?.abort(new DOMException('Synthetic fixture cleanup', 'AbortError'))
    await f.extension?.dispose()
    for (const client of f.clients) client.dispose()
    await scope?.dispose()
    await owner?.dispose()
    await runtime?.dispose()
    await prompt?.dispose()
    await rm(directory, { recursive: true, force: true })
  })
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    f.fetchCalls++
    init.signal.throwIfAborted()
    assert.equal(new URL(url).origin, 'https://legion.invalid')
    assert.equal(new URL(url).pathname, '/api/legion/team/message')
    assert.equal(init.method, 'POST')
    const body = JSON.parse(init.body)
    f.requests.push(body)
    // Match the real service's 201 envelope. It is a mailbox receipt, not proof
    // that a remote model ran or consumed anything.
    return Response.json({
      id: f.requests.length, from: 'commander', to: body.recipient, text: body.text,
      artifact_refs: [], ts: 1, read_at: null,
    }, { status: 201 })
  })
  prompt = ctx.plugin(SystemPrompt, { includeHarnessIdentity: false })
  await prompt
  runtime = ctx.plugin(ToolRuntime, { mode: 'native' })
  await runtime
  owner = ctx.inject(['tools', 'systemPrompt'], injected => {
    scope = createScope(injected, agent)
    if (structured) f.structured = attachStructuredRuntime(scope.ctx, answerSchema, () => true)
  })
  await owner
  agent.ctx = scope.ctx
  ctx.on('tools/execute', (exec, next) => {
    f.executions.push(exec)
    if (exec.name === 'native_fixture' || exec.name === 'structured_output') f.nativeCalls++
    return next()
  })
  ctx.on('tools/post-execute', (exec, result, next) => {
    if (exec.name === 'legion_call') f.bodies.push(structuredClone(result.value))
    return next()
  })
  ctx.on('tools/result', (exec, result) => f.results.push({ exec, result }))
  f.restartClient = async () => {
    f.lifecycle?.abort(new DOMException('Synthetic extension rebind', 'AbortError'))
    await f.extension?.dispose()
    f.client?.dispose()
    const client = new LegionClient(config, env)
    f.client = client
    f.clients.push(client)
    const write = client.store.writeObservation.bind(client.store)
    t.mock.method(client.store, 'writeObservation', async (ref, name, value) => {
      const entry = name === resultName ? {
        record: structuredClone(value),
        // Capture at write ENTRY, not after I/O: an early child write must not
        // look root-authorized merely because the disk write finishes later.
        result_events: f.results.map(({ exec, result }) => resultFact(exec, result)),
      } : undefined
      if (entry) f.writes.push(entry)
      await write(ref, name, value)
      if (entry) await f.afterResultWrite?.(entry.record)
    })
    f.lifecycle = new AbortController()
    const lifecycle = f.lifecycle
    f.extension = ctx.inject(['tools'], injected => {
      registerEcosystem(injected, client, config, lifecycle.signal)
    })
    await f.extension
  }
  await f.restartClient()
  const { ref } = await f.client.store.reserve({
    sessionId: agent.id, key: 'settlement-task', query: 'Synthetic settlement task; no remote order submitted',
  })
  f.ref = ref
  const folder = f.client.store.path(ref.id)
  f.readResult = () => readJson(join(folder, `${resultName}.json`))
  f.readAck = () => readJson(join(folder, `ack-${messageId}.json`))
  f.readMessageReservation = () => readJson(join(folder, `op-${messageId}.json`))
  f.terminal = () => eventually(f.readResult,
    // Missing transport is returned promptly so assertions report an invalid
    // protocol (including old v1), rather than disguising it as a polling timeout.
    record => record !== null && record.transport?.state !== 'pending', 'durable bridge terminal')
  f.args = {
    task_ref: ref.id, recipient: 'author', operation_key: operationKey,
    tool_name: structured ? 'structured_output' : 'native_fixture',
    arguments: structured ? { answer: 42 } : {},
  }
  f.run = (overrides = {}) => ctx.tools.execute({
    name: 'legion_call', arguments: f.args, agent,
    signal: new AbortController().signal, callId: `settlement-call-${++f.serial}`, ...overrides,
  })
  return f
}

function assertPending(receipt, observed = 'succeeded') {
  assert.equal(receipt?.native?.state, 'unknown', 'A tool body cannot certify its own final host result')
  assert.equal(receipt.version, 2)
  assert.equal(receipt.native.observed?.state, observed)
  assert.equal(receipt.transport.state, 'pending')
  assertManualDelivery(receipt)
}

function assertManualDelivery(receipt) {
  assert.equal(receipt.delivery.acknowledgement, 'not_attempted')
  assert.equal(receipt.delivery.automatic, false)
  assert.equal(receipt.delivery.method, 'legion_message')
}

function assertMetadata(record, ...bodyMarkers) {
  assert.ok(record.native?.result == null, 'Durable/recovered facts must not contain native.result')
  assert.ok(record.native?.observed?.result == null, 'Observed state must not cache a child result either')
  const serialized = JSON.stringify(record)
  for (const marker of bodyMarkers) {
    assert.ok(!serialized.includes(marker), `Metadata must not replay body marker: ${marker}`)
  }
}

function assertTerminal(record, nativeState, observedState, transportState, ancestors) {
  assert.equal(record.version, 2)
  assert.equal(record.operation_id, operationId)
  assert.equal(record.native.state, nativeState)
  assert.equal(record.native.observed?.state, observedState)
  assert.equal(record.transport.state, transportState)
  assertMetadata(record)
  const rows = record.transport.outcomes
  assert.ok(Array.isArray(rows), 'Persist the authoritative ancestor outcome chain')
  for (const [exec, state] of ancestors) {
    const matches = rows.filter(row => row.call_id === exec.callId)
    assert.equal(matches.length, 1, `Keep one authoritative outcome for ${exec.name}`)
    const { call_id, name, state: actualState } = matches[0]
    assert.deepEqual({ call_id, name, state: actualState }, { call_id: exec.callId, name: exec.name, state })
  }
}

function assertRootWrite(f, root) {
  const finals = f.writes.filter(({ record }) => ['succeeded', 'failed'].includes(record.transport?.state))
  assert.equal(finals.length, 1, 'Publish one complete terminal record, not one per ancestor')
  assert.ok(finals[0].result_events.some(row => row.call_id === root.callId),
    'The terminal write must START after the authoritative root tools/result')
  for (const { record } of f.writes) assertMetadata(record)
}

async function assertNoHttp(f) {
  // Let already-scheduled observer work run; no private production flush hook.
  await delay(25)
  assert.equal(f.fetchCalls, 0, 'legion_call and same-key recovery must never attempt automatic HTTP')
  assert.equal(await f.readAck(), null)
  assert.equal(await f.readMessageReservation(), null, 'Do not reserve the old automatic feedback key')
}

function registerAncestors(f) {
  const register = (name, childName, childArgs) => f.ctx.tools.register(nativeTool(name, async (_args, exec) => {
    const result = await callNative(f.ctx, exec, childName, childArgs)
    if (result.isError) throw new Error(result.error.message)
    return result.value
  }))
  register('native_middle', 'legion_call', f.args)
  register('native_ancestor', 'native_middle', {})
}

test('structured body receipt is pending; root success persists only metadata and never sends HTTP', options, async t => {
  const f = await fixture(t, { structured: true })
  const result = await f.run()
  assert.equal(result.isError, false)
  assertPending(result.value)
  assert.deepEqual(f.structured.captured(), { value: { answer: 42 } })
  assert.equal(f.structured.acceptsInput(), false)
  assert.equal(result.concludesTurn, true)
  const parent = f.executions.find(exec => exec.name === 'legion_call')
  const saved = await f.terminal()
  assertTerminal(saved, 'succeeded', 'succeeded', 'succeeded', [[parent, 'succeeded']])
  assertRootWrite(f, parent)
  await assertNoHttp(f)
  assertPending(result.value) // The immutable body receipt stays a pending observation.
})

test('W1: a fresh client can recover terminal metadata with the target unloaded, without rerunning or sending', options, async t => {
  const f = await fixture(t)
  const marker = 'SYNTHETIC_ORIGINAL_BODY_NOT_A_RECOVERY_PAYLOAD'
  const unregister = f.ctx.tools.register(nativeTool('native_fixture', () => ({ child_body: marker })))
  const first = await f.run()
  assert.equal(first.isError, false)
  const saved = await f.terminal()
  const parent = f.executions.find(exec => exec.name === 'legion_call')
  unregister()
  assert.equal(f.ctx.tools.get('native_fixture', f.agent), undefined)
  const originalClient = f.client
  await f.restartClient()
  assert.notEqual(f.client, originalClient)
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await f.restartClient()
    const recovered = await f.run()
    assert.equal(recovered.isError, false, 'Target visibility must not hide an already saved terminal status')
    assert.equal(recovered.value.native.state, 'succeeded')
    assert.equal(recovered.value.reused, true)
    assertMetadata(recovered.value, marker)
    assert.ok(!JSON.stringify(recovered).includes(marker), 'Rendered recovery content must also be metadata-only')
    assertManualDelivery(recovered.value)
    assert.equal(f.nativeCalls, 1)
    await assertNoHttp(f)
    assert.deepEqual(await f.readResult(), saved, 'Recovery must not replace original parent facts')
  }
  assertPending(first.value)
  assertTerminal(saved, 'succeeded', 'succeeded', 'succeeded', [[parent, 'succeeded']])
  assertRootWrite(f, parent)
})

test('W2: cancellation after pending persistence rolls back structured capture; recovery cannot claim success or send', options, async t => {
  const f = await fixture(t, { structured: true })
  const controller = new AbortController()
  let firstWrite
  f.afterResultWrite = record => {
    if (firstWrite) return
    firstWrite = record
    controller.abort(new DOMException('Synthetic cancellation after initial pending persistence', 'AbortError'))
  }
  const first = await f.run({ signal: controller.signal })
  assert.equal(first.isError, true)
  assert.equal(f.structured.captured(), undefined)
  assert.equal(f.structured.acceptsInput(), true)
  const saved = await f.terminal()
  f.afterResultWrite = undefined
  await f.restartClient()
  const recovered = await f.run()
  assert.notEqual(recovered.value?.native?.state, 'succeeded', 'A rolled-back structured capture is not a committed success')
  if (!recovered.isError) {
    assert.equal(recovered.value.native.state, 'unknown')
    assertMetadata(recovered.value)
    assertManualDelivery(recovered.value)
  }
  await assertNoHttp(f)
  assert.equal(f.nativeCalls, 1)
  assert.equal(f.structured.captured(), undefined)
  assert.equal(f.structured.acceptsInput(), true)
  assert.equal(firstWrite.native.state, 'unknown')
  assert.equal(firstWrite.native.observed?.state, 'succeeded')
  assert.equal(firstWrite.transport.state, 'pending')
  assertMetadata(firstWrite)
  const parent = f.executions.find(exec => exec.name === 'legion_call')
  assertTerminal(saved, 'unknown', 'succeeded', 'failed', [[parent, 'failed']])
  assertRootWrite(f, parent)
  assert.deepEqual((await f.readResult()).native, saved.native)
})

test('W3: legion_call post-policy refusal leaves child success unknown, without structured capture or HTTP', options, async t => {
  const f = await fixture(t, { structured: true })
  let bodyReceipt
  f.ctx.on('tools/post-execute', (exec, result, next) => {
    if (exec.name !== 'legion_call') return next()
    bodyReceipt = result.value
    return { kind: 'block', feedback: [{ type: 'text', text: 'Synthetic bridge parent post-policy refusal' }] }
  })
  const result = await f.run()
  assert.equal(result.isError, true)
  assert.match(result.error.message, /parent post-policy refusal/)
  assert.equal(f.structured.captured(), undefined)
  assert.equal(f.structured.acceptsInput(), true)
  const saved = await f.terminal()
  await assertNoHttp(f)
  assertPending(bodyReceipt)
  assertTerminal(saved, 'unknown', 'succeeded', 'failed',
    [[f.executions.find(exec => exec.name === 'legion_call'), 'failed']])
  assertRootWrite(f, f.executions.find(exec => exec.name === 'legion_call'))
})

for (const outcome of ['succeeded', 'blocked', 'cancelled']) {
  test(`multi-level native ancestry ${outcome}: settle at the root exactly once, with no automatic HTTP`, options, async t => {
    const f = await fixture(t)
    const controller = new AbortController()
    f.ctx.tools.register(nativeTool('native_fixture', () => ({ native_observation: true })))
    registerAncestors(f)
    f.ctx.on('tools/post-execute', (exec, _result, next) => {
      if (exec.name !== 'native_ancestor') return next()
      if (outcome === 'blocked') return {
        kind: 'block', feedback: [{ type: 'text', text: 'Synthetic outer ancestor post-policy refusal' }],
      }
      if (outcome === 'cancelled') controller.abort(new DOMException('Synthetic ancestor cancellation', 'AbortError'))
      return next()
    })
    const result = await f.run({
      name: 'native_ancestor', arguments: {}, callId: 'settlement-ancestor-root', signal: controller.signal,
    })
    const failed = outcome !== 'succeeded'
    assert.equal(result.isError, failed)
    const root = f.executions.find(exec => exec.name === 'native_ancestor')
    const middle = f.executions.find(exec => exec.name === 'native_middle')
    const bridge = f.executions.find(exec => exec.name === 'legion_call')
    const leaf = f.executions.find(exec => exec.name === 'native_fixture')
    assert.equal(middle.parent, root.token)
    assert.equal(bridge.parent, middle.token)
    assert.equal(leaf.parent, bridge.token)
    assert.equal(leaf.rootCallId, root.callId)
    const saved = await f.terminal()
    const ancestors = [[bridge, 'succeeded'], [middle, 'succeeded'], [root, failed ? 'failed' : 'succeeded']]
    assertTerminal(saved, failed ? 'unknown' : 'succeeded', 'succeeded', failed ? 'failed' : 'succeeded', ancestors)
    assertRootWrite(f, root)
    assertPending(f.bodies[0])
    await assertNoHttp(f)
    assert.equal(f.nativeCalls, 1)
  })
}

test('an observed native failure stays failed even when an ancestor also refuses, without sending any child result', options, async t => {
  const f = await fixture(t)
  f.ctx.tools.register(nativeTool('native_fixture', () => { throw new Error('Synthetic original native failure') }))
  registerAncestors(f)
  f.ctx.on('tools/post-execute', (exec, _result, next) => exec.name === 'native_ancestor'
    ? { kind: 'block', feedback: [{ type: 'text', text: 'Synthetic ancestor refusal after native failure' }] }
    : next())
  const result = await f.run({ name: 'native_ancestor', arguments: {}, callId: 'settlement-failed-root' })
  assert.equal(result.isError, true)
  const saved = await f.terminal()
  assert.equal(saved.native.state, 'failed', 'Do not erase an already-observed native failure as merely unknown')
  assert.equal(saved.native.observed?.state, 'failed')
  assertMetadata(saved, 'Synthetic original native failure')
  assertTerminal(saved, 'failed', 'failed', 'failed',
    [[f.executions.find(exec => exec.name === 'native_ancestor'), 'failed']])
  assertRootWrite(f, f.executions.find(exec => exec.name === 'native_ancestor'))
  await assertNoHttp(f)
  assert.equal(f.nativeCalls, 1)
})

for (const replacement of ['content', 'value']) {
  test(`accepted parent ${replacement} replacement cannot be bypassed by replaying the old child cache`, options, async t => {
    const f = await fixture(t)
    const hidden = 'SYNTHETIC_CHILD_BODY_BEFORE_PARENT_POLICY'
    const accepted = 'SYNTHETIC_ACCEPTED_PARENT_PRESENTATION'
    f.ctx.tools.register(nativeTool('native_fixture', () => ({ child_body: hidden })))
    f.ctx.on('tools/post-execute', (exec, _result, next) => {
      if (exec.callId !== 'replacement-root') return next()
      return replacement === 'content'
        ? { kind: 'accept', content: [{ type: 'text', text: accepted }] }
        : { kind: 'accept', value: { accepted } }
    })
    const first = await f.run({ callId: 'replacement-root' })
    assert.equal(first.isError, false)
    if (replacement === 'content') assert.deepEqual(first.content, [{ type: 'text', text: accepted }])
    else assert.deepEqual(first.value, { accepted })
    assert.ok(!JSON.stringify(first.content).includes(hidden), 'The official parent presentation really hid the child')
    const saved = await f.terminal()
    await f.restartClient()
    const recovered = await f.run()
    assert.equal(recovered.isError, false)
    assert.ok(!JSON.stringify(recovered).includes(hidden), 'Same-key recovery must not undo an accepted parent rewrite')
    assert.ok(!JSON.stringify(recovered).includes(accepted), 'Recovery returns status, not a cached parent body either')
    assert.equal(recovered.value.native.state, 'succeeded')
    assertMetadata(recovered.value, hidden, accepted)
    assertManualDelivery(recovered.value)
    assertMetadata(saved, hidden, accepted)
    const root = f.executions.find(exec => exec.callId === 'replacement-root')
    assertTerminal(saved, 'succeeded', 'succeeded', 'succeeded', [[root, 'succeeded']])
    assertRootWrite(f, root)
    assert.deepEqual(await f.readResult(), saved)
    assert.equal(f.nativeCalls, 1)
    await assertNoHttp(f)
  })
}

for (const childFailed of [false, true]) {
  test(`a child's accepted final content does not leak its masked ${childFailed ? 'error.message' : 'value'} into the bridge`, options, async t => {
    const f = await fixture(t)
    const hidden = 'SYNTHETIC_CHILD_DETAIL_MASKED_BY_CONTENT_POLICY'
    const content = [{ type: 'text', text: 'Synthetic public child presentation' }]
    f.ctx.tools.register(nativeTool('native_fixture', () => {
      if (childFailed) throw new Error(hidden)
      return { child_body: hidden }
    }))
    f.ctx.on('tools/post-execute', (exec, _result, next) => exec.name === 'native_fixture'
      ? { kind: 'accept', content }
      : next())
    const first = await f.run()
    const leaf = f.results.find(({ exec }) => exec.name === 'native_fixture').result
    assert.deepEqual(leaf.content, content)
    assert.equal(childFailed ? leaf.error.message : leaf.value.child_body, hidden,
      'Official content replacement leaves a non-presentation field behind; the bridge must not copy it')
    assert.ok(!JSON.stringify(first).includes(hidden), 'Only child final content may become the first bridge body')
    if (!first.isError) {
      assertPending(first.value, childFailed ? 'failed' : 'succeeded')
      if (first.value.native.result != null) {
        assert.ok(JSON.stringify(first.value.native.result).includes(content[0].text))
        assert.equal(first.value.native.result.value, undefined)
        assert.equal(first.value.native.result.error?.message, undefined)
      }
    }
    const saved = await f.terminal()
    assertMetadata(saved, hidden, content[0].text)
    const root = f.executions.find(exec => exec.name === 'legion_call')
    assertTerminal(saved, childFailed ? 'failed' : 'succeeded', childFailed ? 'failed' : 'succeeded',
      first.isError ? 'failed' : 'succeeded', [[root, first.isError ? 'failed' : 'succeeded']])
    assertRootWrite(f, root)
    await assertNoHttp(f)
  })
}

test('legacy v1 child success without parent facts recovers no success, body, execution, or HTTP', options, async t => {
  const f = await fixture(t)
  f.ctx.tools.register(nativeTool('native_fixture', () => ({ must_not_execute: true })))
  // Seed real legacy disk data using the unchanged durable reservation API.
  await f.client.store.reserveOperation(f.ref, 'native-call', operationKey, JSON.stringify({
    recipient: 'author', tool_name: 'native_fixture', arguments: {},
  }))
  await f.client.store.writeObservation(f.ref, resultName, {
    version: 1, task_ref: f.ref.id, operation_id: operationId, recipient: 'author', tool_name: 'native_fixture',
    native: { state: 'succeeded', result: { isError: false, value: { legacy_child: true }, content: [] } },
    projection: { omitted_fields: [], truncated_fields: [] },
  })
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt) await f.restartClient()
    const result = await f.run()
    if (!result.isError) {
      assert.equal(result.value?.native?.state, 'unknown',
        'A successful recovery call cannot manufacture the missing original parent result')
      assertMetadata(result.value, 'legacy_child')
      assert.equal(result.value.delivery.acknowledgement, 'unconfirmed',
        'A missing legacy ACK cannot prove the old operation never sent a message')
      assert.equal(result.value.delivery.historical, true)
      assert.equal(result.value.delivery.attempted_now, false)
      assert.equal(result.value.consumed_by_model_confirmed, false)
    }
    assert.ok(!JSON.stringify(result).includes('legacy_child'))
    await assertNoHttp(f)
    assert.equal(f.nativeCalls, 0)
  }
})
