import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readdir, readFile, writeFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool } from '@deepseek-ai/dsh-tools'
import { createScope } from '@deepseek-ai/dsh-scope'
import { LegionClient } from '../lib/client.js'
import { TaskStore, digest } from '../lib/store.js'
import { executionFrom } from '../lib/commands.js'
import { registerEcosystem } from '../lib/extension.js'

async function implementation() {
  let module
  try { module = await import('../lib/bridge.js') }
  catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error }
  assert.equal(typeof module?.bridgeCall, 'function', 'bridgeCall must be implemented')
  return module.bridgeCall
}

const tool = (name, execute, render) => defineTool({
  name, description: name, parameters: {},
  output: {
    schema: { type: 'object', additionalProperties: true },
    render: render || ((_args, value) => [{ type: 'text', text: JSON.stringify(value) }]),
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
  assert.equal(receipt.native.result, undefined, 'History is status metadata, not a body replay')
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
  assertMetadata(record)
  assertManual(record)
  return record
}

async function fixture(t) {
  const bridgeCall = await implementation()
  const root = fileURLToPath(new URL('../.qa/bridge-runtime-review/', import.meta.url))
  await mkdir(root, { recursive: true })
  const directory = await mkdtemp(join(root, 'bridge-state-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const config = { baseUrl: 'https://legion.invalid', stateDirectory: directory, tokenEnv: 'TEST_TOKEN' }
  const env = { TEST_TOKEN: 'fixture-private-token' }
  const ctx = new Context()
  const prompt = ctx.plugin(SystemPrompt, { includeHarnessIdentity: false })
  await prompt
  const runtime = ctx.plugin(ToolRuntime, { mode: 'native' })
  await runtime
  const agent = { id: 'bridge-session' }
  let scope
  const owner = ctx.inject(['tools'], injected => { scope = createScope(injected, agent) })
  await owner
  agent.ctx = scope.ctx
  t.after(async () => {
    await scope.dispose()
    await owner.dispose()
    await runtime.dispose()
    await prompt.dispose()
  })
  const f = { ctx, agent, directory, env, config, errors: [], requests: [], fetchCalls: 0, bridgeCall }
  f.newClient = () => {
    const client = new LegionClient(config, env)
    t.after(() => client.dispose())
    assert.ok(client.store instanceof TaskStore)
    return client
  }
  f.client = f.newClient()
  const { ref } = await f.client.store.reserve({ sessionId: agent.id, key: 'original', query: 'Synthetic bridge task' })
  f.ref = ref
  f.args = { task_ref: ref.id, recipient: 'author', operation_key: 'native-1', tool_name: 'native_fixture', arguments: {} }
  f.messageArgs = {
    task_ref: ref.id, recipient: 'author', operation_key: 'explicit-feedback-1',
    text: 'Synthetic feedback explicitly selected in a later native invocation',
  }
  const operationId = digest(JSON.stringify(['native-call', f.args.operation_key]))
  f.resultName = `bridge-result-${operationId}`
  f.readResult = () => f.client.store.readObservation(ref, f.resultName)
  f.terminal = () => eventually(f.readResult,
    record => record !== null && record.transport?.state !== 'pending', 'durable bridge terminal')
  f.readAck = () => f.client.store.readObservation(ref,
    `ack-${digest(JSON.stringify(['message', f.messageArgs.operation_key]))}`)
  f.transport = async () => Response.json({ ok: true, consumed_by_model_confirmed: true })
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    f.fetchCalls++
    options.signal.throwIfAborted()
    const request = { url: String(url), ...options, body: JSON.parse(options.body) }
    f.requests.push(request)
    return f.transport(request)
  })
  let serial = 0
  const lifecycle = new AbortController()
  const extension = ctx.inject(['tools'], injected => {
    // Install the public lifecycle observer; do not call a private flush/helper.
    registerEcosystem(injected, f.client, config, lifecycle.signal)
    injected.tools.register(tool('bridge_fixture', async (args, exec) => {
      f.enclosing = exec
      try { return await bridgeCall(injected, f.client, args, exec) }
      catch (error) { f.errors.push(error); throw error }
    }))
    // Separate native invocation using the real message/execution mechanism.
    // This is never called from the bridge body or from an ancestor tool.
    injected.tools.register(tool('legion_message', (args, exec) => {
      f.messageExecution = exec
      return f.client.message(args, executionFrom(exec))
    }))
  })
  await extension
  t.after(async () => {
    lifecycle.abort()
    await extension.dispose()
  })
  f.run = (args = f.args, options = {}) => ctx.tools.execute({
    name: 'bridge_fixture', arguments: args, agent, signal: new AbortController().signal,
    callId: `bridge-root-${++serial}`, ...options,
  })
  f.send = (args = f.messageArgs, options = {}) => ctx.tools.execute({
    name: 'legion_message', arguments: args, agent, signal: new AbortController().signal,
    callId: `message-root-${++serial}`, ...options,
  })
  f.observations = async () => {
    const entries = await readdir(f.client.store.path(ref.id))
    return Object.fromEntries(await Promise.all(entries.filter(name => name.endsWith('.json')).map(async name =>
      [name.slice(0, -5), JSON.parse(await readFile(join(f.client.store.path(ref.id), name), 'utf8'))])))
  }
  f.assertLocalOnly = async () => {
    assert.equal(f.fetchCalls, 0, 'legion_call must not even attempt HTTP')
    assert.equal(f.requests.length, 0, 'legion_call must never attempt HTTP')
    const automaticId = digest(JSON.stringify(['message', `native-result:${operationId}`]))
    const observations = await f.observations()
    assert.equal(observations[`op-${automaticId}`], undefined)
    assert.equal(observations[`ack-${automaticId}`], undefined)
  }
  return f
}

test('official nested execution returns pending content; unloaded-target recovery is metadata-only and never sends', async t => {
  const f = await fixture(t)
  let child, calls = 0
  const unregister = f.ctx.tools.register(tool('native_fixture', (_args, exec) => {
    calls++
    child = exec
    exec.deferContext({ role: 'user', content: [{ type: 'text', text: 'native context stays in DSH' }] })
    exec.concludeTurn()
    return { actual: 42 }
  }))
  const result = await f.run()
  assert.equal(result.isError, false, JSON.stringify(result))
  assertPending(result.value)
  assert.deepEqual(JSON.parse(result.value.native.result.content[0].text), { actual: 42 })
  assert.equal(result.value.native.result.value, undefined)
  assert.equal(child.agent, f.agent)
  assert.equal(child.parent, f.enclosing.token)
  assert.equal(child.rootCallId, f.enclosing.rootCallId)
  assert.equal(child.signal, f.enclosing.signal)
  assert.notEqual(child.callId, f.enclosing.callId)
  assert.equal(result.additionalContexts[0].content[0].text, 'native context stays in DSH')
  assert.equal(result.concludesTurn, true)
  const saved = await terminal(f)
  assertMetadata(saved, 'actual', 'native context stays in DSH')
  await f.assertLocalOnly()

  unregister()
  assert.equal(f.ctx.tools.get('native_fixture', f.agent), undefined)
  f.client = f.newClient()
  const restored = await f.run()
  assert.equal(restored.isError, false, JSON.stringify(restored))
  assert.equal(restored.value.reused, true)
  assert.equal(restored.value.native.state, 'succeeded')
  assertMetadata(restored.value, 'actual', 'native context stays in DSH')
  assertManual(restored.value)
  assert.deepEqual(await f.readResult(), saved, 'Recovery must not replace the original parent outcomes')
  assert.equal((restored.additionalContexts ?? []).length, 0)
  assert.equal(restored.concludesTurn, undefined)
  assert.equal(calls, 1)
  await f.assertLocalOnly()
})

test('native guard failure first throws a content-only receipt; later status reads remain truthful and local', async t => {
  const f = await fixture(t)
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', () => { calls++; return {} }))
  f.ctx.tools.guard(exec => exec.name === 'native_fixture' ? 'Native guard refused fixture-private-token' : undefined)
  const result = await f.run()
  assert.equal(result.isError, true)
  assert.match(result.error.message, /Native guard refused/)
  assert.ok(!result.error.message.includes(f.env.TEST_TOKEN))
  const receipt = f.errors.at(-1).receipt
  assert.equal(f.errors.at(-1).name, 'LegionBridgeError')
  assertPending(receipt, 'failed')
  assert.match(JSON.stringify(receipt.native.result.content), /Native guard refused/)
  assert.equal(receipt.native.result.error, undefined)
  const saved = await terminal(f, 'failed')
  assert.equal(calls, 0)
  f.client = f.newClient()
  const retry = await f.run()
  assert.equal(retry.isError, false, 'Reading failed status is not a claim that execution succeeded')
  assert.equal(retry.value.native.state, 'failed')
  assertMetadata(retry.value, 'Native guard refused')
  assert.deepEqual(await f.readResult(), saved)
  await f.assertLocalOnly()
})

test('preflight rejects other sessions/services, unavailable credentials, invisible tools and legion_* recursion without side effects', async t => {
  const f = await fixture(t)
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', () => { calls++; return {} }))
  f.ctx.tools.register(tool('legion_status', () => { calls++; return {} }))
  const foreign = await f.client.store.reserve({ sessionId: 'other-session', key: 'other', query: 'Synthetic' })
  const wrongSession = await f.run({ ...f.args, task_ref: foreign.ref.id })
  assert.equal(wrongSession.isError, true)
  assert.match(wrongSession.error.message, /different DSH session/)

  const original = f.client
  f.client = new LegionClient({ ...f.config, baseUrl: 'https://other.invalid' }, f.env)
  t.after(() => f.client.dispose())
  assert.match((await f.run()).error.message, /different configured service/)
  f.client = original
  const token = f.env.TEST_TOKEN
  delete f.env.TEST_TOKEN
  assert.match((await f.run()).error.message, /TEST_TOKEN/)
  f.env.TEST_TOKEN = token
  assert.match((await f.run({ ...f.args, tool_name: 'legion_status' })).error.message, /Recursive/)
  f.agent.ctx.tools.restrict({ deny: ['native_fixture'] })
  assert.match((await f.run()).error.message, /not visible/)
  assert.equal(calls, 0)
  assert.equal(f.requests.length, 0)
  assert.deepEqual(Object.keys(await f.observations()), ['ref'])
})

test('requires real enclosing execution fields and rejects missing or already aborted host signals', async t => {
  const f = await fixture(t)
  const exec = { agent: f.agent, signal: new AbortController().signal, callId: 'not-a-live-invocation' }
  await assert.rejects(f.bridgeCall(f.ctx, f.client, f.args, exec), /enclosing/)
  await assert.rejects(f.bridgeCall(f.ctx, f.client, f.args, { ...exec, agent: undefined }), /DSH/)
  await assert.rejects(f.bridgeCall(f.ctx, f.client, f.args, { ...exec, signal: undefined }), /AbortSignal/)
  const controller = new AbortController()
  controller.abort(new Error('caller stopped'))
  await assert.rejects(f.bridgeCall(f.ctx, f.client, f.args, { ...exec, signal: controller.signal }), /caller stopped/)
  assert.equal(f.requests.length, 0)
  assert.deepEqual(Object.keys(await f.observations()), ['ref'])
})

test('resolves the current agent scoped tool rather than the global tool with the same name', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(tool('native_fixture', () => assert.fail('global implementation must not be used')))
  f.agent.ctx.tools.register(tool('native_fixture', (_args, exec) => {
    assert.equal(exec.agent, f.agent)
    return { scope: 'current-agent' }
  }))
  const result = await f.run()
  assert.equal(result.isError, false, JSON.stringify(result))
  assertPending(result.value)
  assert.deepEqual(JSON.parse(result.value.native.result.content[0].text), { scope: 'current-agent' })
  assert.equal(result.value.native.result.value, undefined)
  await terminal(f)
  await f.assertLocalOnly()
})

test('concurrent duplicates and changed inputs cannot start another native operation or overwrite its eventual result', { timeout: 5000 }, async t => {
  const f = await fixture(t)
  const started = Promise.withResolvers()
  const finish = Promise.withResolvers()
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', async args => {
    calls++
    started.resolve()
    await finish.promise
    return { actual: args }
  }))
  f.args.arguments = { a: 1, b: 2 }
  const first = f.run()
  try {
    await started.promise
    const duplicate = await f.run({ ...f.args, arguments: { b: 2, a: 1 } })
    const receipt = duplicate.isError ? f.errors.at(-1).receipt : duplicate.value
    assert.equal(receipt.native.state, 'unknown')
    assertMetadata(receipt)
    const changed = await f.run({ ...f.args, arguments: { a: 3, b: 2 } })
    assert.equal(changed.isError, true)
    assert.match(changed.error.message, /different input/)
    assert.equal(f.requests.length, 0)
    assert.equal(calls, 1)
    assert.ok(!Object.keys(await f.observations()).some(name => name.startsWith('bridge-result-')))
  } finally { finish.resolve() }
  const complete = await first
  assert.equal(complete.isError, false, JSON.stringify(complete))
  assertPending(complete.value)
  const saved = await terminal(f)
  f.client = f.newClient()
  const restored = await f.run({ ...f.args, arguments: { b: 2, a: 1 } })
  assert.equal(restored.isError, false)
  assert.equal(restored.value.native.state, 'succeeded')
  assertMetadata(restored.value)
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(calls, 1)
  await f.assertLocalOnly()
  assert.match((await f.run({ ...f.args, recipient: 'other-author' })).error.message, /different input/)
  f.ctx.tools.register(tool('different_native', () => assert.fail('tool name changed under the same operation key')))
  assert.match((await f.run({ ...f.args, tool_name: 'different_native' })).error.message, /different input/)
})

test('a real reservation without a committed result stays unknown after restart and does not send a placeholder', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(tool('native_fixture', () => assert.fail('unknown operation must not run again')))
  await f.client.store.reserveOperation(f.ref, 'native-call', f.args.operation_key,
    JSON.stringify({ recipient: f.args.recipient, tool_name: f.args.tool_name, arguments: {} }))
  f.client = f.newClient()
  for (let i = 0; i < 2; i++) {
    const result = await f.run()
    const receipt = result.isError ? f.errors.at(-1).receipt : result.value
    assert.equal(receipt.native.state, 'unknown')
    assertMetadata(receipt)
    assertManual(receipt)
  }
  assert.equal(f.requests.length, 0)
  assert.ok(!Object.keys(await f.observations()).some(name => name.startsWith('bridge-result-')))
})

for (const [label, transport, expected] of [
  ['HTTP refusal', async () => Response.json({ reason: 'fixture refusal fixture-private-token' }, { status: 403 }), 'rejected'],
  ['JSON refusal', async () => Response.json({ ok: false, reason: 'fixture refusal' }), 'rejected'],
  ['server uncertainty', async () => Response.json({ error: 'fixture server failure' }, { status: 503 }), 'unconfirmed'],
  ['lost network reply', async () => { throw new Error('connection lost fixture-private-token') }, 'unconfirmed'],
]) {
  test(`independent legion_message ${label} cannot roll back native success or cause a resend after restart`, async t => {
    const f = await fixture(t)
    let calls = 0
    f.ctx.tools.register(tool('native_fixture', () => { calls++; return { completed_side_effect: 'observed' } }))
    const first = await f.run()
    assert.equal(first.isError, false)
    assertPending(first.value)
    const stored = await terminal(f)
    assertMetadata(stored, 'completed_side_effect')
    await f.assertLocalOnly()

    f.transport = transport
    const sent = await f.send()
    assert.equal(sent.isError, label === 'HTTP refusal')
    const ack = await f.readAck()
    assert.equal(ack.acknowledgement, expected)
    assert.ok(!JSON.stringify([sent, ack]).includes(f.env.TEST_TOKEN))
    assert.deepEqual(await f.readResult(), stored, 'A message failure cannot rewrite native facts')
    assert.equal(first.isError, false)
    assertPending(first.value)
    f.client = f.newClient()
    const restored = await f.run()
    assert.equal(restored.isError, false)
    assert.equal(restored.value.native.state, 'succeeded')
    assertMetadata(restored.value)
    assertManual(restored.value)
    const messageRetry = await f.send()
    assert.equal(messageRetry.isError, false)
    assert.equal(messageRetry.value.acknowledgement, expected)
    assert.equal(messageRetry.value.reused, true)
    assert.equal(messageRetry.value.consumed_by_model_confirmed, false)
    assert.equal(calls, 1)
    assert.equal(f.requests.length, 1)
    assert.equal(f.requests[0].body.text, f.messageArgs.text)
    assert.equal(f.requests[0].body.sender, 'commander')
    assert.equal(f.requests[0].body.recipient, 'author')
    assert.equal(f.requests[0].body.team_id, f.ref.team_id)
    assert.equal(f.requests[0].url, 'https://legion.invalid/api/legion/team/message')
    assert.equal(f.messageExecution.parent, undefined, 'Feedback must be a separate native root invocation')
  })
}

test('concurrent explicit message recovery cannot replace a later received acknowledgement with an unconfirmed one', { timeout: 5000 }, async t => {
  const f = await fixture(t)
  const started = Promise.withResolvers()
  const finish = Promise.withResolvers()
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', () => { calls++; return { actual: 1 } }))
  assert.equal((await f.run()).isError, false)
  const saved = await terminal(f)
  await f.assertLocalOnly()
  f.transport = async () => {
    started.resolve()
    await finish.promise
    return Response.json({ ok: true })
  }
  const first = f.send()
  try {
    await started.promise
    const duplicate = await f.send()
    assert.equal(duplicate.isError, false)
    assert.equal(duplicate.value.acknowledgement, 'unconfirmed')
    assert.equal(duplicate.value.reused, true)
    assert.equal(await f.readAck(), null, 'An in-flight reader must not write an acknowledgement placeholder')
    const status = await f.run()
    assert.equal(status.value.native.state, 'succeeded')
    assertMetadata(status.value)
    assert.deepEqual(await f.readResult(), saved)
  } finally { finish.resolve() }
  assert.equal((await first).isError, false)
  f.client = f.newClient()
  const final = await f.send()
  assert.equal(final.isError, false)
  assert.equal(final.value.acknowledgement, 'received')
  assert.equal(final.value.consumed_by_model_confirmed, false)
  assert.equal((await f.readAck()).acknowledgement, 'received')
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(f.requests.length, 1)
  assert.equal(calls, 1)
})

for (const source of ['native cancellation', 'plugin unload']) {
  test(`${source} reaches the native child through the supplied fused signal, persists unknown and makes no later network call`, { timeout: 5000 }, async t => {
    const f = await fixture(t)
    const native = new AbortController()
    const unload = new AbortController()
    const signal = AbortSignal.any([native.signal, unload.signal])
    const started = Promise.withResolvers()
    const finish = Promise.withResolvers()
    let calls = 0
    f.ctx.tools.register(tool('native_fixture', async (_args, exec) => {
      calls++
      assert.equal(exec.signal, signal)
      started.resolve()
      await finish.promise
      // A started body may have had a side effect; its return after abort is not proof of success.
      return { body_returned_after_abort: true }
    }))
    const first = f.run(f.args, { signal })
    try {
      await started.promise
      ;(source === 'native cancellation' ? native : unload).abort(new Error(source))
    } finally { finish.resolve() }
    assert.equal((await first).isError, true)
    assertPending(f.errors.at(-1).receipt, 'unknown')
    const stored = await terminal(f, 'unknown')
    f.client = f.newClient()
    const recovered = await f.run()
    assert.equal(recovered.isError, false, 'A successful status read must not be confused with successful execution')
    assert.equal(recovered.value.native.state, 'unknown')
    assertMetadata(recovered.value, 'body_returned_after_abort')
    assert.deepEqual(await f.readResult(), stored)
    await f.assertLocalOnly()
    assert.equal(calls, 1)
  })
}

test('abort after terminal persistence and target unload preserves metadata, without automatic feedback on retry', async t => {
  const f = await fixture(t)
  const controller = new AbortController()
  let calls = 0
  const unregister = f.ctx.tools.register(tool('native_fixture', () => { calls++; return { actual: 'finished before abort' } }))
  const write = f.client.store.writeObservation.bind(f.client.store)
  t.mock.method(f.client.store, 'writeObservation', async (ref, name, value) => {
    await write(ref, name, value)
    if (name === f.resultName && value.transport?.state === 'succeeded') {
      controller.abort(new Error('caller stopped after terminal result commit'))
    }
  })
  const first = await f.run(f.args, { signal: controller.signal })
  assert.equal(first.isError, false, 'Cancellation after the final host result cannot undo its native success')
  assertPending(first.value)
  const saved = await terminal(f)
  await eventually(async () => controller.signal.aborted, Boolean, 'post-terminal cancellation')
  await f.assertLocalOnly()
  unregister()
  f.client = f.newClient()
  const recovered = await f.run()
  assert.equal(recovered.isError, false)
  assert.equal(recovered.value.native.state, 'succeeded')
  assertMetadata(recovered.value, 'finished before abort')
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(calls, 1)
  await f.assertLocalOnly()
})

test('a persistence fault does not send an uncommitted result or allow a fresh execution after restart', async t => {
  const f = await fixture(t)
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', () => { calls++; return { actual: 'completed' } }))
  t.mock.method(f.client.store, 'writeObservation', async () => { throw new Error('fixture storage failure') })
  const first = await f.run()
  assert.equal(first.isError, false)
  assert.equal(first.value.persistence, 'failed')
  assertPending(first.value)
  assert.equal(f.requests.length, 0)
  f.client = f.newClient()
  const restored = await f.run()
  const receipt = restored.isError ? f.errors.at(-1).receipt : restored.value
  assert.equal(receipt.native.state, 'unknown')
  assertMetadata(receipt, 'completed')
  assert.equal(calls, 1)
  await f.assertLocalOnly()
})

test('durable and outbound projections omit secrets, rendered JSON secrets, image/audio payloads and data URLs, and are bounded', async t => {
  const f = await fixture(t)
  const image = Buffer.from('synthetic sensitive image '.repeat(50)).toString('base64')
  const audio = Buffer.from('synthetic sensitive audio '.repeat(50)).toString('base64')
  const value = {
    actual: 'useful text',
    password: 'secret-password-value',
    token: f.env.TEST_TOKEN,
    echo: `Bearer ${f.env.TEST_TOKEN}`,
    image: { type: 'image', source: { type: 'base64', media_type: 'image/png', data: image } },
    audio: { type: 'input_audio', data: audio, format: 'wav' },
    screenshot_base64: image,
    inline: `before data:image/png;base64,${image} after`,
    rendered: JSON.stringify({ password: 'rendered-password-value', clip: `data:audio/wav;base64,${audio}` }),
    blob: { mimeType: 'audio/wav', data: audio },
    huge: Array.from({ length: 100 }, (_, i) => ({ i, text: '长文本'.repeat(500) })),
  }
  f.ctx.tools.register(tool('native_fixture', () => value, (_args, result) => [
    { type: 'text', text: JSON.stringify(result) },
    { type: 'image', source: { type: 'base64', mediaType: 'image/png', data: image } },
  ]))
  const result = await f.run()
  assert.equal(result.isError, false, JSON.stringify(result).slice(0, 300))
  assertPending(result.value)
  const saved = await terminal(f)
  assertMetadata(saved, 'useful text')
  await f.assertLocalOnly()
  // A separate caller explicitly chooses this already-projected text. The
  // bridge itself must not decide to copy any body into the mailbox.
  const sent = await f.send({ ...f.messageArgs, text: JSON.stringify(result.value.native.result) })
  assert.equal(sent.isError, false)
  assert.equal(sent.value.acknowledgement, 'received')
  assert.equal(sent.value.consumed_by_model_confirmed, false, 'A server consumption claim is not evidence')
  const records = await f.observations()
  for (const text of [JSON.stringify(records), f.requests[0].body.text, JSON.stringify(result.value)]) {
    for (const secret of [image, audio, f.env.TEST_TOKEN, 'secret-password-value', 'rendered-password-value', 'data:image', 'data:audio']) {
      assert.ok(!text.includes(secret), `projection leaked ${secret.slice(0, 32)}`)
    }
  }
  assert.ok(Buffer.byteLength(f.requests[0].body.text) < 24576, 'feedback must stay small')
  assert.ok(result.value.projection.omitted_fields.length > 1)
  assert.ok(result.value.projection.truncated_fields.length > 0)
  assert.match(result.value.projection.media_return, /cannot.*full/)
  assert.equal(result.value.projection.experience_verification, 'not_performed_by_plugin')
  assert.equal(result.value.consumed_by_model_confirmed, false)
})

test('native projections preserve long ordinary text, identifiers and public URLs instead of guessing base64 by length', async t => {
  const f = await fixture(t)
  const ordinary = 'ReadableNormalText'.repeat(12)
  const identifier = 'PublicIdentifier'.repeat(9)
  const value = {
    text: ordinary,
    sha512: createHash('sha512').update('Synthetic public content').digest('hex'),
    plain_text: 'x'.repeat(129),
    public_url: `https://example.org/docs/${ordinary}`,
    [identifier]: 'kept',
  }
  f.ctx.tools.register(tool('native_fixture', () => value))
  const result = await f.run()
  assert.equal(result.isError, false)
  assert.deepEqual(JSON.parse(result.value.native.result.content[0].text), value)
  assert.equal(result.value.projection.media_omitted, false)
  assert.deepEqual(result.value.projection.truncated_fields, [])
  assertPending(result.value)
  await terminal(f)
  await f.assertLocalOnly()
})

test('native projections preserve ordinary long text as truncated text rather than omitted media', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(tool('native_fixture', () => ({ text: 'x'.repeat(3000) })))
  const result = await f.run()
  assert.equal(result.isError, false)
  assert.equal(JSON.parse(result.value.native.result.content[0].text).text, `${'x'.repeat(2048)}[truncated]`)
  assert.equal(result.value.projection.media_omitted, false)
  assert.ok(result.value.projection.truncated_fields.includes('$.content[0].text.text'))
  assertPending(result.value)
  await terminal(f)
  await f.assertLocalOnly()
})

test('native projections omit the entire data payload through URI punctuation without losing adjacent explanation', async t => {
  const f = await fixture(t)
  const payloads = [
    'data:text/plain,PRIVATE_HEAD)PRIVATE_TAIL;part=2',
    "data:text/plain,PRIVATE_HEAD'PRIVATE_TAIL;part=2",
    'data:image/svg+xml,%3Csvg%3EPRIVATE_HEAD)PRIVATE_TAIL%3C/svg%3E',
    'data:text/plain,PRIVATE_HEAD%29PRIVATE_TAIL',
    "data:text/plain;label='tag',PRIVATE_HEAD)PRIVATE_TAIL",
  ]
  f.ctx.tools.register(tool('native_fixture', () => ({
    text: payloads.map(data => `Synthetic failure (${data}); keep this explanation`),
  })))
  const result = await f.run()
  assert.equal(result.isError, false)
  assert.doesNotMatch(JSON.stringify(result.value), /PRIVATE_HEAD|PRIVATE_TAIL|part=2|data:(?:text|image)|%3C/i)
  for (const text of JSON.parse(result.value.native.result.content[0].text).text) {
    assert.match(text, /keep this explanation/)
  }
  assert.equal(result.value.projection.media_omitted, true)
  assertPending(result.value)
  await terminal(f)
  await f.assertLocalOnly()
})

test('native thrown failures expose only sanitized final content and never automatically feed it back', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(tool('native_fixture', () => {
    throw new Error('Actual fixture failure fixture-private-token data:audio/wav;base64,QUJDREVGRw==')
  }))
  const result = await f.run()
  assert.equal(result.isError, true)
  assert.match(result.error.message, /Actual fixture failure/)
  assert.ok(!result.error.message.includes('data:audio'))
  assert.ok(!result.error.message.includes('QUJDREVGRw=='))
  assert.ok(!result.error.message.includes(f.env.TEST_TOKEN))
  const receipt = f.errors.at(-1).receipt
  assertPending(receipt, 'failed')
  assert.equal(receipt.native.result.error, undefined)
  assert.equal(receipt.native.result.value, undefined)
  assert.match(JSON.stringify(receipt.native.result.content), /Actual fixture failure/)
  const saved = await terminal(f, 'failed')
  assertMetadata(saved, 'Actual fixture failure')
  await f.assertLocalOnly()
})

test('independent message rejection observations remove inline media and credentials without rewriting native facts or resending', async t => {
  const f = await fixture(t)
  f.ctx.tools.register(tool('native_fixture', () => ({ actual: true })))
  const first = await f.run()
  assert.equal(first.isError, false)
  assertPending(first.value)
  const saved = await terminal(f)
  await f.assertLocalOnly()
  f.transport = async () => Response.json({
    ok: false, reason: 'Synthetic refusal fixture-private-token data:image/png;base64,QUJDREVGRw==',
  })
  const sent = await f.send()
  assert.equal(sent.isError, false)
  assert.equal(sent.value.acknowledgement, 'rejected')
  assert.equal(sent.value.consumed_by_model_confirmed, false)
  const stored = JSON.stringify(await f.observations())
  for (const text of [stored, JSON.stringify(sent)]) {
    assert.ok(!text.includes('data:image'), 'Message diagnostics must not persist or return encoded media')
    assert.ok(!text.includes('QUJDREVGRw=='))
    assert.ok(!text.includes(f.env.TEST_TOKEN))
  }
  assert.deepEqual(await f.readResult(), saved)
  f.client = f.newClient()
  const retry = await f.send()
  assert.equal(retry.isError, false)
  assert.equal(retry.value.acknowledgement, 'rejected')
  assert.equal(retry.value.reused, true)
  assert.deepEqual(await f.readResult(), saved)
  assert.equal(f.requests.length, 1)
})

for (const [label, acknowledgement, native, expected] of [
  ['received', { acknowledgement: 'received', http_status: 202 }, 'succeeded', 'received'],
  ['rejected', { acknowledgement: 'rejected', http_status: 403, reason: 'Synthetic refusal' }, 'succeeded', 'rejected'],
  ['unconfirmed', { acknowledgement: 'unconfirmed' }, 'succeeded', 'unconfirmed'],
  ['missing', undefined, 'succeeded', 'unconfirmed'],
  ['corrupt JSON', 'OLD_ACK_PRIVATE_BODY is not JSON', 'succeeded', 'unconfirmed'],
  ['invalid shape', ['received'], 'succeeded', 'unconfirmed'],
  ['invalid state', { acknowledgement: 'not_attempted' }, 'succeeded', 'unconfirmed'],
  ['received despite native failure', { acknowledgement: 'received' }, 'failed', 'received'],
  ['private extras', {
    acknowledgement: 'received', task_ref: 'forged-ref', operation_id: 'forged-operation',
    historical: false, attempted_now: true, consumed_by_model_confirmed: true,
    reason: 'Synthetic legacy note fixture-private-token data:image/png;base64,QUJDREVGRw==',
    server_response: { text: 'OLD_ACK_PRIVATE_BODY' },
  }, 'succeeded', 'received'],
]) {
  test(`legacy v1 ${label} delivery remains a separate read-only historical fact after target removal`, async t => {
    const f = await fixture(t)
    let calls = 0
    const unregister = f.ctx.tools.register(tool('native_fixture', () => { calls++; return { must_not_run: true } }))
    await f.client.store.reserveOperation(f.ref, 'native-call', f.args.operation_key,
      JSON.stringify({ recipient: f.args.recipient, tool_name: f.args.tool_name, arguments: {} }))
    await f.client.store.writeObservation(f.ref, f.resultName, {
      version: 1, task_ref: 'forged-ref', operation_id: 'forged-operation',
      native: { state: native, result: { content: [{ type: 'text', text: 'OLD_NATIVE_PRIVATE_BODY' }] } },
    })
    const id = digest(JSON.stringify(['native-call', f.args.operation_key]))
    const ackPath = join(f.client.store.path(f.ref.id),
      `ack-${digest(JSON.stringify(['message', `native-result:${id}`]))}.json`)
    const ackBytes = acknowledgement === undefined ? null
      : typeof acknowledgement === 'string' ? acknowledgement : JSON.stringify(acknowledgement)
    if (ackBytes !== null) await writeFile(ackPath, ackBytes)
    const resultBytes = await readFile(join(f.client.store.path(f.ref.id), `${f.resultName}.json`), 'utf8')
    const beforeFiles = await readdir(f.client.store.path(f.ref.id))
    unregister()
    f.client = f.newClient()
    const result = await f.run()
    assert.equal(result.isError, false, JSON.stringify(result))
    const receipt = result.value
    assert.equal(receipt.task_ref, f.ref.id)
    assert.equal(receipt.operation_id, id, 'Untrusted old metadata cannot choose the historical ACK key')
    assert.equal(receipt.legacy_version, 1)
    assert.equal(receipt.native.state, native === 'failed' ? 'failed' : 'unknown')
    assert.equal(receipt.transport.state, 'unknown')
    assert.equal(receipt.delivery.acknowledgement, expected)
    assert.equal(receipt.delivery.historical, true)
    assert.equal(receipt.delivery.attempted_now, false)
    assert.equal(receipt.consumed_by_model_confirmed, false)
    assertMetadata(receipt, 'OLD_NATIVE_PRIVATE_BODY', 'OLD_ACK_PRIVATE_BODY', 'forged-', f.env.TEST_TOKEN, 'data:image', 'QUJDREVGRw==')
    assert.equal(f.fetchCalls, 0)
    assert.equal(calls, 0)
    assert.deepEqual(await readdir(f.client.store.path(f.ref.id)), beforeFiles, 'Recovery must not create send keys or new observations')
    assert.equal(await readFile(join(f.client.store.path(f.ref.id), `${f.resultName}.json`), 'utf8'), resultBytes)
    if (ackBytes !== null) assert.equal(await readFile(ackPath, 'utf8'), ackBytes)
    const changed = await f.run({ ...f.args, arguments: { changed: true } })
    assert.equal(changed.isError, true)
    assert.match(changed.error.message, /different input/)
    assert.equal(f.fetchCalls, 0)
  })
}

test('legacy delivery recovery rejects plugin disposal during ACK reading instead of returning a normal receipt', async t => {
  const f = await fixture(t)
  await f.client.store.reserveOperation(f.ref, 'native-call', f.args.operation_key,
    JSON.stringify({ recipient: f.args.recipient, tool_name: f.args.tool_name, arguments: {} }))
  await f.client.store.writeObservation(f.ref, f.resultName, { version: 1, native: { state: 'succeeded' } })
  const entered = Promise.withResolvers(), release = Promise.withResolvers()
  const read = f.client.store.readObservation.bind(f.client.store)
  t.mock.method(f.client.store, 'readObservation', async (...params) => {
    const value = await read(...params)
    if (params[1].startsWith('ack-')) { entered.resolve(); await release.promise }
    return value
  })
  const pending = f.run()
  // A missing ACK read is itself the old defect; do not wait forever on it.
  try {
    const reachedRead = await Promise.race([entered.promise.then(() => true), pending.then(() => false)])
    assert.equal(reachedRead, true, 'v1 recovery must inspect the separate historical ACK')
    f.client.dispose()
  } finally { release.resolve() }
  const result = await pending
  assert.equal(result.isError, true)
  assert.match(result.error.message, /disposed/i)
  assert.equal(f.fetchCalls, 0)
})

test('aborting a later explicit message preserves native success and an unconfirmed acknowledgement, never resending', { timeout: 5000 }, async t => {
  const f = await fixture(t)
  const controller = new AbortController()
  const started = Promise.withResolvers()
  let calls = 0
  f.ctx.tools.register(tool('native_fixture', () => { calls++; return { actual: true } }))
  assert.equal((await f.run()).isError, false)
  const saved = await terminal(f)
  await f.assertLocalOnly()
  f.transport = request => new Promise((_resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(new Error('Synthetic interrupted delivery')), { once: true })
    started.resolve()
  })
  const first = f.send(f.messageArgs, { signal: controller.signal })
  try { await started.promise }
  finally { controller.abort(new Error('caller cancelled while waiting for ack')) }
  assert.equal((await first).isError, true)
  assert.equal((await f.readAck()).acknowledgement, 'unconfirmed')
  assert.deepEqual(await f.readResult(), saved)
  f.client = f.newClient()
  const recovered = await f.run()
  assert.equal(recovered.isError, false)
  assert.equal(recovered.value.native.state, 'succeeded')
  assertMetadata(recovered.value)
  assertManual(recovered.value)
  const feedback = await f.send()
  assert.equal(feedback.isError, false)
  assert.equal(feedback.value.acknowledgement, 'unconfirmed')
  assert.equal(feedback.value.reused, true)
  assert.equal(calls, 1)
  assert.equal(f.requests.length, 1)
})
