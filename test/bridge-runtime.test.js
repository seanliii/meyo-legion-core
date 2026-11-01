import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readdir, readFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool } from '@deepseek-ai/dsh-tools'
import { createScope } from '@deepseek-ai/dsh-scope'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { Session } from '@deepseek-ai/dsh-session'
import { ApprovalService } from '@deepseek-ai/dsh-user-approval'
import { LegionClient } from '../lib/client.js'
import { executionFrom } from '../lib/commands.js'
import { digest } from '../lib/store.js'
import { registerEcosystem } from '../lib/extension.js'
import { callNative, withExecutionSignal } from '../lib/ecosystem.js'

// Use the installed, pinned official implementation, not a reimplementation of
// its parent-result commit protocol. This internal import is test-only.
const { attachStructuredRuntime } = await import(
  new URL('./types/structured.js', import.meta.resolve('@deepseek-ai/dsh-subagent')))
const evidenceDirectory = fileURLToPath(new URL('../.qa/bridge-runtime-review/', import.meta.url))
const answerSchema = {
  type: 'object', properties: { answer: { type: 'integer' } },
  required: ['answer'], additionalProperties: false,
}
const nativeTool = (execute, render) => defineTool({
  name: 'native_fixture', description: 'Synthetic local tool', parameters: {},
  output: {
    schema: { type: 'object', additionalProperties: true },
    render: render ?? ((_args, value) => [{ type: 'text', text: JSON.stringify(value) }]),
  },
  execute,
})

async function eventually(read, accepts, label) {
  const deadline = Date.now() + 2000
  let value
  do {
    value = await read()
    if (accepts(value)) return value
    await delay(10)
  } while (Date.now() < deadline)
  assert.fail(`${label} did not settle within 2000ms`)
}

function assertManual(receipt) {
  assert.equal(receipt.delivery.acknowledgement, 'not_attempted')
  assert.equal(receipt.delivery.automatic, false)
  assert.equal(receipt.delivery.method, 'legion_message')
  assert.equal(receipt.consumed_by_model_confirmed, false)
}

function assertPending(receipt, observed = 'succeeded') {
  assert.equal(receipt.version, 2)
  assert.equal(receipt.native.state, 'unknown')
  assert.equal(receipt.native.observed.state, observed)
  assert.equal(receipt.transport.state, 'pending')
  assertManual(receipt)
}

function assertMetadata(receipt, ...markers) {
  assert.equal(receipt.native.result, undefined)
  assert.equal(receipt.native.observed?.result, undefined)
  for (const marker of markers) assert.ok(!JSON.stringify(receipt).includes(marker))
}

async function terminal(f, state = 'succeeded', observed = state, transport = state === 'succeeded' ? 'succeeded' : 'failed') {
  const record = await f.terminal()
  assert.equal(record.version, 2)
  assert.equal(record.native.state, state)
  assert.equal(record.native.observed.state, observed)
  assert.equal(record.transport.state, transport)
  assert.ok(Array.isArray(record.transport.outcomes))
  assertManual(record)
  assertMetadata(record)
  return record
}

// Real registered entry, ToolRuntime, agent scope and disk TaskStore. Only the
// remote HTTP boundary is mocked; no Host server, model or real network runs.
async function fixture(t, { structured = false } = {}) {
  await mkdir(evidenceDirectory, { recursive: true })
  const directory = await mkdtemp(join(evidenceDirectory, 'state-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const config = {
    baseUrl: 'https://legion.invalid', stateDirectory: directory,
    tokenEnv: 'BRIDGE_RUNTIME_FIXTURE_TOKEN',
  }
  const lifecycle = new AbortController()
  const client = new LegionClient(config, { BRIDGE_RUNTIME_FIXTURE_TOKEN: 'synthetic-local-test-credential' })
  t.after(() => client.dispose())
  const f = { client, lifecycle, requests: [], fetchCalls: 0, resultEvents: [], nativeCalls: 0 }
  f.transport = async () => Response.json({ ok: true })
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    f.fetchCalls++
    options.signal.throwIfAborted()
    const request = { url: String(url), body: JSON.parse(options.body), signal: options.signal }
    f.requests.push(request)
    return f.transport(request)
  })
  const ctx = new Context()
  f.ctx = ctx
  const prompt = ctx.plugin(SystemPrompt, { includeHarnessIdentity: false })
  await prompt
  const runtime = ctx.plugin(ToolRuntime, { mode: 'native' })
  await runtime
  const agent = { id: 'bridge-runtime-session' }
  f.agent = agent
  let scope
  const owner = ctx.inject(['tools', 'systemPrompt'], injected => {
    scope = createScope(injected, agent)
    if (structured) f.structured = attachStructuredRuntime(scope.ctx, answerSchema, () => true)
  })
  await owner
  agent.ctx = scope.ctx
  const extension = ctx.inject(['tools'], injected => {
    registerEcosystem(injected, client, config, lifecycle.signal)
    // A separate native root, reusing the production client/execution boundary;
    // never an automatic send from legion_call or one of its ancestors.
    injected.tools.register({
      ...nativeTool((args, original) => client.message(args,
        executionFrom(withExecutionSignal(original, lifecycle.signal)))),
      name: 'legion_message',
    })
  })
  await extension
  t.after(async () => {
    await extension.dispose()
    await scope.dispose()
    await owner.dispose()
    await runtime.dispose()
    await prompt.dispose()
  })
  ctx.on('tools/execute', (exec, next) => {
    if (exec.name === 'legion_call') f.enclosing = exec
    else if (exec.name === 'legion_message') f.messageExecution = exec
    else if (exec.name === 'native_fixture' || exec.name === 'structured_output') {
      f.child = exec
      f.nativeCalls++
    }
    return next()
  })
  ctx.on('tools/result', (exec, result) => f.resultEvents.push({ exec, result }))
  const { ref } = await client.store.reserve({
    sessionId: agent.id, key: 'runtime-task', query: 'Synthetic local runtime regression',
  })
  f.ref = ref
  f.args = {
    task_ref: ref.id, recipient: 'author', operation_key: 'runtime-operation',
    tool_name: structured ? 'structured_output' : 'native_fixture',
    arguments: structured ? { answer: 42 } : {},
  }
  f.messageArgs = {
    task_ref: ref.id, recipient: 'author', operation_key: 'explicit-runtime-feedback',
    text: 'Synthetic public feedback chosen in a separate native invocation',
  }
  const operationId = digest(JSON.stringify(['native-call', f.args.operation_key]))
  f.resultName = `bridge-result-${operationId}`
  f.readResult = () => client.store.readObservation(ref, f.resultName)
  f.terminal = () => eventually(f.readResult,
    record => record !== null && record.transport?.state !== 'pending', 'durable bridge terminal')
  f.readAck = () => client.store.readObservation(ref,
    `ack-${digest(JSON.stringify(['message', f.messageArgs.operation_key]))}`)
  let sequence = 0
  f.run = (args = f.args, options = {}) => ctx.tools.execute({
    name: 'legion_call', arguments: args, agent,
    signal: new AbortController().signal, callId: `runtime-call-${++sequence}`, ...options,
  })
  f.send = (args = f.messageArgs, options = {}) => ctx.tools.execute({
    name: 'legion_message', arguments: args, agent,
    signal: new AbortController().signal, callId: `runtime-message-${++sequence}`, ...options,
  })
  f.observations = async () => {
    const names = await readdir(client.store.path(ref.id))
    return Object.fromEntries(await Promise.all(names.filter(name => name.endsWith('.json')).map(async name =>
      [name.slice(0, -5), JSON.parse(await readFile(join(client.store.path(ref.id), name), 'utf8'))])))
  }
  f.assertLocalOnly = async () => {
    assert.equal(f.fetchCalls, 0, 'legion_call must not even attempt HTTP')
    const automaticId = digest(JSON.stringify(['message', `native-result:${operationId}`]))
    const observations = await f.observations()
    assert.equal(observations[`op-${automaticId}`], undefined)
    assert.equal(observations[`ack-${automaticId}`], undefined)
  }
  return f
}

async function nativeApproval(t, f, answer) {
  // Real in-memory session and approval audit; the answerer below is a
  // synthetic test input, never a real Host permission or installation grant.
  f.agent.session = Session.create(f.agent.id)
  f.agent.session.append('turn/start', { turn: 1 })
  const approval = f.ctx.plugin(ApprovalService, { policy: 'ask' })
  await approval
  t.after(() => approval.dispose())
  f.ctx.on('tools/pre-execute', async (exec, next) => {
    if (exec.name !== 'native_fixture') return next()
    f.approvalExecution = exec
    return { kind: 'ask', reason: 'Synthetic native approval required' }
  })
  f.ctx.on('approval/request', async request => {
    f.approvalRequest = request
    return answer(request)
  })
}

test('registered legion_call binds concludeTurn to the official enclosing execution without losing scoped context', async t => {
  const f = await fixture(t)
  const context = createUserMessage({
    source: { kind: 'tool-registry' },
    content: [{ type: 'text', text: 'Synthetic native context for the local author' }],
  })
  f.ctx.tools.register(nativeTool((_args, exec) => {
    exec.deferContext(context)
    exec.concludeTurn()
    return { actual: 42 }
  }))
  const caller = new AbortController()
  const result = await f.run(f.args, { signal: caller.signal })
  t.diagnostic(JSON.stringify({
    case: 'registered-concludeTurn', isError: result.isError,
    concludesTurn: result.concludesTurn ?? null, contexts: result.additionalContexts?.length ?? 0,
    sameAgent: f.child.agent === f.agent, sameParent: f.child.parent === f.enclosing.token,
    sameRoot: f.child.rootCallId === f.enclosing.rootCallId,
    callerSignalUnchanged: f.enclosing.signal === caller.signal,
  }))
  assert.equal(result.isError, false)
  assert.equal(f.child.agent, f.agent)
  assert.equal(f.child.parent, f.enclosing.token)
  assert.equal(f.child.rootCallId, f.enclosing.rootCallId)
  assert.notEqual(f.child.token, f.enclosing.token)
  assert.equal(f.enclosing.signal, caller.signal)
  assert.deepEqual(result.additionalContexts, [context])
  assertPending(result.value)
  assert.equal(result.concludesTurn, true, 'The runtime must see its OWN execution marked, not a copied object')
  const saved = await terminal(f)
  assertMetadata(saved, context.content[0].text)
  await f.assertLocalOnly()
})

test('official structured_output commits its parent and the completed scope cannot send a later message', async t => {
  const f = await fixture(t, { structured: true })
  const result = await f.run()
  assert.equal(result.isError, false)
  assertPending(result.value)
  assert.equal(result.concludesTurn, true)
  assert.deepEqual(f.structured.captured(), { value: { answer: 42 } })
  assert.equal(f.structured.acceptsInput(), false)
  const saved = await terminal(f)
  await f.assertLocalOnly()

  // A completed structured child has no next model decision. Preserve its
  // official terminal guard; do not reopen its scope or reuse its agent id.
  const blocked = await f.send()
  assert.equal(blocked.isError, true)
  assert.match(blocked.error.message, /structured output already recorded/)
  assert.equal(f.messageExecution, undefined)
  assert.equal(await f.readAck(), null)
  const operationId = digest(JSON.stringify(['message', f.messageArgs.operation_key]))
  assert.equal((await f.observations())[`op-${operationId}`], undefined)
  assert.deepEqual(f.structured.captured(), { value: { answer: 42 } })
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(f.nativeCalls, 1)
  await f.assertLocalOnly()
})

for (const [label, transport, acknowledgement] of [
  ['HTTP 503', async () => Response.json({ error: 'Synthetic unavailable mailbox' }, { status: 503 }), 'unconfirmed'],
  ['HTTP 403', async () => Response.json({ error: 'Synthetic refused mailbox' }, { status: 403 }), 'rejected'],
  ['lost reply', async () => { throw new Error('Synthetic lost mailbox reply') }, 'unconfirmed'],
]) {
  test(`registered native success settles before an independent legion_message ${label}; delivery cannot roll it back`, async t => {
    const f = await fixture(t)
    f.ctx.tools.register(nativeTool(() => ({ answer: 42 })))
    const result = await f.run()
    const saved = await terminal(f)
    await f.assertLocalOnly()
    assert.equal(result.isError, false)
    assertPending(result.value)
    assert.deepEqual(JSON.parse(result.value.native.result.content[0].text), { answer: 42 })
    assert.equal(result.value.native.result.value, undefined)
    assert.equal(result.concludesTurn, undefined, 'This ordinary agent remains open for another native decision')
    f.transport = transport
    const sent = await f.send()
    const ack = await f.readAck()
    t.diagnostic(JSON.stringify({
      case: `independent-message-${label}`, outerIsError: result.isError,
      childState: saved.native.state,
      messageIsError: sent.isError, acknowledgement: ack?.acknowledgement ?? null,
      nativeCalls: f.nativeCalls, httpAttempts: f.requests.length,
    }))
    assert.equal(result.isError, false, 'Mailbox failure must not reject the successful enclosing native transaction')
    assertPending(result.value)
    assert.equal(sent.isError, label === 'HTTP 403')
    assert.ok(ack, 'The independent message must record its own delivery fact')
    assert.equal(ack.acknowledgement, acknowledgement)
    assert.deepEqual(await f.readResult(), saved)
    assertMetadata(saved, '"answer"')
    assert.equal(f.messageExecution.parent, undefined)
    assert.notEqual(f.messageExecution.rootCallId, f.enclosing.rootCallId)
    assert.equal(f.requests[0].body.text, f.messageArgs.text)
    assert.equal(f.nativeCalls, 1)
    assert.equal(f.fetchCalls, 1)
    assert.equal(f.requests.length, 1)
  })
}

test('a result-storage fault cannot roll back official structured_output or send an uncommitted receipt', async t => {
  const f = await fixture(t, { structured: true })
  t.mock.method(f.client.store, 'writeObservation', async () => { throw new Error('Synthetic result-storage fault') })
  const result = await f.run()
  t.diagnostic(JSON.stringify({
    case: 'structured-output-storage-fault', outerIsError: result.isError,
    captured: f.structured.captured() ?? null, persistence: result.value?.persistence ?? null,
    httpAttempts: f.requests.length,
  }))
  assert.equal(result.isError, false, 'A secondary recovery-index failure must not undo the native commit')
  assert.deepEqual(f.structured.captured(), { value: { answer: 42 } })
  assertPending(result.value)
  assert.equal(result.value.persistence, 'failed')
  await f.assertLocalOnly()
})

test('an explicit message acknowledgement-storage fault cannot undo native success or claim received on recovery', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(nativeTool(() => ({ answer: 42 })))
  const write = f.client.store.writeObservation.bind(f.client.store)
  t.mock.method(f.client.store, 'writeObservation', async (ref, name, value) => {
    if (name.startsWith('ack-')) throw new Error('Synthetic acknowledgement-storage fault')
    return write(ref, name, value)
  })
  const result = await f.run()
  assert.equal(result.isError, false)
  assertPending(result.value)
  assert.deepEqual(JSON.parse(result.value.native.result.content[0].text), { answer: 42 })
  assert.equal(result.concludesTurn, undefined)
  const saved = await terminal(f)
  await f.assertLocalOnly()
  const sent = await f.send()
  assert.equal(sent.isError, true, 'The independent message must not invent a durable received acknowledgement')
  assert.equal(await f.readAck(), null)
  const recoveredMessage = await f.send()
  assert.equal(recoveredMessage.isError, false)
  assert.equal(recoveredMessage.value.acknowledgement, 'unconfirmed')
  assert.equal(recoveredMessage.value.reused, true)
  assert.equal(recoveredMessage.value.consumed_by_model_confirmed, false)
  assert.equal(result.isError, false)
  assert.equal(await f.readAck(), null)
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(f.nativeCalls, 1)
  assert.equal(f.fetchCalls, 1)
  assert.equal(f.requests.length, 1)
})

test('native image content reaches the local author as an official message but never the bridge store or remote mailbox', async t => {
  const f = await fixture(t)
  const image = {
    attachmentId: 'synthetic-local-image-reference', mediaType: 'image/png',
    bytes: 123, width: 8, height: 8, name: 'synthetic.png',
  }
  const blocks = [
    { type: 'text', text: '<path>/synthetic/image.png</path><type>image</type>' },
    { type: 'image', attachment: image },
  ]
  const deferred = createUserMessage({
    content: [{ type: 'text', text: 'Additional native context, local only' }],
    source: { kind: 'tool-registry' },
  })
  // Exact native read_image result shape; no real image IO or model is needed.
  const unregister = f.ctx.tools.register(nativeTool((_args, exec) => {
    exec.deferContext(deferred)
    return { path: '/synthetic/image.png', image }
  }, () => blocks))
  const first = await f.run()
  assertPending(first.value)
  const saved = await terminal(f)
  await f.assertLocalOnly()
  const contexts = first.additionalContexts ?? []
  const images = contexts.flatMap(message => message.content).filter(block => block.type === 'image')
  const serializedStore = JSON.stringify(await f.observations())
  const sent = await f.send()
  assert.equal(sent.isError, false)
  assert.equal(sent.value.acknowledgement, 'received')
  t.diagnostic(JSON.stringify({
    case: 'local-image-ferry', outerIsError: first.isError, localImageCount: images.length,
    remoteContainsAttachment: f.requests[0].body.text.includes(image.attachmentId),
    bridgeStoreContainsAttachment: serializedStore.includes(image.attachmentId),
  }))
  assert.equal(first.isError, false)
  assert.deepEqual(images, [{ type: 'image', attachment: image }], 'Do not discard the native local image with the remote projection')
  const imageMessage = contexts.find(message => message.content.some(block => block.type === 'image'))
  assert.equal(imageMessage.role, 'user')
  assert.equal(typeof imageMessage.id, 'string')
  assert.equal(imageMessage.source.kind, 'tool-registry')
  assert.deepEqual(imageMessage.content, blocks)
  assert.deepEqual(contexts.at(-1), deferred)
  for (const output of [serializedStore, f.requests[0].body.text, JSON.stringify(first.value)]) {
    assert.ok(!output.includes(image.attachmentId), 'Local attachment references must not enter durable/outbound projections')
    assert.ok(!output.includes(deferred.content[0].text))
  }
  unregister()
  assert.equal(f.ctx.tools.get('native_fixture', f.agent), undefined)
  const retry = await f.run()
  assert.equal(retry.isError, false)
  assert.equal(retry.value.reused, true)
  assertMetadata(retry.value, image.attachmentId, deferred.content[0].text)
  assertManual(retry.value)
  assert.deepEqual(await f.readResult(), saved)
  assert.equal((retry.additionalContexts ?? []).length, 0, 'Recovery reads cannot replay an unpersisted local attachment')
  assert.equal(f.nativeCalls, 1)
  assert.equal(f.requests.length, 1)
})

for (const origin of ['caller', 'plugin']) {
  test(`${origin} cancellation still reaches the registered native child and prevents a new message`, { timeout: 5000 }, async t => {
    const f = await fixture(t)
    const caller = new AbortController()
    const started = Promise.withResolvers()
    f.ctx.tools.register(nativeTool(async (_args, exec) => {
      started.resolve()
      await new Promise(resolve => exec.signal.addEventListener('abort', resolve, { once: true }))
      return { returnedAfterCancellation: true }
    }))
    const pending = f.run(f.args, { signal: caller.signal })
    await started.promise
    ;(origin === 'caller' ? caller : f.lifecycle).abort(new Error('Synthetic invocation cancellation'))
    const result = await pending
    const saved = await terminal(f, 'unknown')
    t.diagnostic(JSON.stringify({
      case: `${origin}-cancellation`, outerIsError: result.isError,
      childAborted: f.child.signal.aborted, callerAborted: caller.signal.aborted,
      childState: saved.native.state, httpAttempts: f.requests.length,
    }))
    assert.equal(result.isError, true)
    assert.equal(result.concludesTurn, undefined)
    assert.equal(saved.native.state, 'unknown')
    assert.equal(f.child.signal.aborted, true)
    assert.equal(caller.signal.aborted, origin === 'caller')
    await f.assertLocalOnly()
  })
}

for (const origin of ['caller', 'plugin']) {
  test(`${origin} pre-cancellation starts neither a native child nor a bridge reservation`, async t => {
    const f = await fixture(t)
    const caller = new AbortController()
    f.ctx.tools.register(nativeTool(() => assert.fail('No child may start after cancellation')))
    ;(origin === 'caller' ? caller : f.lifecycle).abort(new Error('Synthetic pre-cancellation'))
    const result = await f.run(f.args, { signal: caller.signal })
    assert.equal(result.isError, true)
    assert.equal(f.nativeCalls, 0)
    await f.assertLocalOnly()
    assert.ok(!Object.keys(await f.observations()).some(name => /^(op-|bridge-result-)/.test(name)))
  })

  test(`${origin} cancellation before the enclosing commit discards pending structured output, not native observations`, { timeout: 5000 }, async t => {
    const f = await fixture(t, { structured: true })
    const caller = new AbortController()
    const started = Promise.withResolvers()
    const release = Promise.withResolvers()
    const write = f.client.store.writeObservation.bind(f.client.store)
    let paused = false
    t.mock.method(f.client.store, 'writeObservation', async (ref, name, value) => {
      await write(ref, name, value)
      if (name !== f.resultName || paused) return
      paused = true
      // Pause only after the real atomic pending write, not at an HTTP call.
      started.resolve(structuredClone(value))
      await release.promise
    })
    const pending = f.run(f.args, { signal: caller.signal })
    let initial
    try {
      initial = await started.promise
      assert.equal(f.structured.captured(), undefined, 'The child is still waiting for its parent to commit')
      ;(origin === 'caller' ? caller : f.lifecycle).abort(new Error('Synthetic parent cancellation'))
    } finally { release.resolve() }
    const result = await pending
    const saved = await terminal(f, 'unknown', 'succeeded')
    t.diagnostic(JSON.stringify({
      case: `${origin}-cancelled-parent-commit`, outerIsError: result.isError,
      childState: saved.native.state, captured: f.structured.captured() ?? null,
      httpAttempts: f.requests.length,
    }))
    assert.equal(result.isError, true)
    assert.equal(result.concludesTurn, undefined)
    assert.equal(f.structured.captured(), undefined)
    assert.equal(f.structured.acceptsInput(), true)
    assertPending(initial)
    assertMetadata(initial)
    assert.equal(saved.native.state, 'unknown', 'Child success cannot certify a cancelled parent transaction')
    assert.equal(saved.native.observed.state, 'succeeded', 'Cancellation does not undo an already-observed child result')
    await f.assertLocalOnly()
  })
}

for (const blocked of ['structured_output', 'legion_call']) {
  test(`native post-execute policy on ${blocked} remains authoritative for structured capture`, async t => {
    const f = await fixture(t, { structured: true })
    f.ctx.on('tools/post-execute', async (exec, _result, next) => exec.name === blocked
      ? { kind: 'block', feedback: [{ type: 'text', text: 'Synthetic native result policy denial' }] }
      : next())
    const result = await f.run()
    assert.equal(result.isError, true)
    assert.equal(result.concludesTurn, undefined)
    assert.equal(f.structured.captured(), undefined)
    assert.equal(f.structured.acceptsInput(), true)
    await terminal(f, blocked === 'structured_output' ? 'failed' : 'unknown',
      blocked === 'structured_output' ? 'failed' : 'succeeded')
    await f.assertLocalOnly()
  })
}

test('registered bridge preserves the native approval request, agent and audit without approving or running the child', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(nativeTool(() => assert.fail('A rejected native action must not run')))
  await nativeApproval(t, f, () => 'rejected')
  const result = await f.run()
  assert.equal(result.isError, true)
  assert.match(result.error.message, /user rejected/)
  assert.equal(f.nativeCalls, 0)
  assert.equal(f.approvalRequest.agent, f.agent)
  assert.equal(f.approvalRequest.callId, f.approvalExecution.callId)
  assert.equal(f.approvalRequest.signal, f.approvalExecution.signal)
  assert.equal(f.approvalExecution.parent, f.enclosing.token)
  assert.equal(f.approvalExecution.rootCallId, f.enclosing.rootCallId)
  const asked = f.agent.session.eventAt(1)
  const decided = f.agent.session.eventAt(2)
  assert.equal(asked.type, 'approval/asked')
  assert.equal(decided.type, 'approval/decided')
  assert.equal(asked.data.id, decided.data.id)
  assert.equal(decided.data.outcome, 'rejected')
  await terminal(f, 'failed')
  await f.assertLocalOnly()
})

test('plugin cancellation interrupts the real native approval wait and a late synthetic grant cannot execute the child', { timeout: 5000 }, async t => {
  const f = await fixture(t)
  const asked = Promise.withResolvers()
  const answer = Promise.withResolvers()
  f.ctx.tools.register(nativeTool(() => assert.fail('Cancelled approval cannot grant execution')))
  await nativeApproval(t, f, request => {
    asked.resolve(request)
    return answer.promise
  })
  const pending = f.run()
  try {
    const request = await asked.promise
    f.lifecycle.abort(new Error('Synthetic plugin disposal during approval'))
    const result = await pending
    assert.equal(request.signal.aborted, true)
    assert.equal(result.isError, true)
    assert.equal(f.agent.session.eventAt(2).data.outcome, 'cancelled')
  } finally {
    answer.resolve('allowed-once')
  }
  assert.equal(f.nativeCalls, 0)
  await terminal(f, 'unknown')
  await f.assertLocalOnly()
})

test('another native parent retains every immediate parent token, root id and local context through the bridge', async t => {
  const f = await fixture(t)
  let root, leaf
  const context = createUserMessage({
    content: [{ type: 'text', text: 'Synthetic deep local context' }],
    source: { kind: 'tool-registry' },
  })
  f.ctx.tools.register(nativeTool((_args, exec) => {
    leaf = exec
    exec.deferContext(context)
    exec.concludeTurn()
    return { deepResult: true }
  }))
  f.ctx.tools.register({
    ...nativeTool(async (_args, exec) => {
      root = exec
      const result = await callNative(f.ctx, exec, 'legion_call', f.args)
      if (result.isError) throw new Error(result.error.message)
      return result.value
    }),
    name: 'native_parent_fixture',
  })
  const result = await f.ctx.tools.execute({
    name: 'native_parent_fixture', arguments: {}, agent: f.agent,
    signal: new AbortController().signal, callId: 'deep-native-root',
  })
  assert.equal(result.isError, false)
  assert.equal(result.concludesTurn, true)
  assert.equal(f.enclosing.parent, root.token)
  assert.equal(leaf.parent, f.enclosing.token)
  assert.equal(leaf.rootCallId, root.callId)
  assert.equal(f.enclosing.rootCallId, root.callId)
  assert.equal(leaf.agent, root.agent)
  assert.deepEqual(result.additionalContexts, [context])
  assert.ok(!JSON.stringify(result.value).includes(context.content[0].text))
  assertPending(result.value)
  const saved = await terminal(f)
  for (const exec of [root, f.enclosing]) {
    const outcomes = saved.transport.outcomes.filter(row => row.call_id === exec.callId)
    assert.equal(outcomes.length, 1)
    assert.deepEqual(outcomes[0], { call_id: exec.callId, name: exec.name, state: 'succeeded' })
  }
  assertMetadata(saved, context.content[0].text)
  await f.assertLocalOnly()
})
