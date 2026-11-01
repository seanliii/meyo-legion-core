import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime } from '@deepseek-ai/dsh-tools'
import { CommandRuntime } from '@deepseek-ai/dsh-commands'
import { LegionClient } from '../lib/client.js'

// Real private files, recovery indexes and DSH executors; only HTTP is replaced.
const firstToken = 'fixture/first+token='
const nextToken = 'fixture/rotated+token='
const fallbackToken = 'fixture-env-must-never-be-used'

async function setup(t, config = {}) {
  const root = await mkdtemp(join(tmpdir(), 'legion-transport-safety-'))
  const tokenFile = join(root, 'personal-token')
  await writeFile(tokenFile, firstToken, { mode: 0o600 })
  const options = { baseUrl: 'https://service.invalid/legion', tokenFile,
    stateDirectory: join(root, 'state'), requestTimeoutMs: 2000, ...config }
  const clients = []
  const newClient = () => {
    const client = new LegionClient(options, { LEGION_PERSONAL_TOKEN: fallbackToken })
    clients.push(client)
    return client
  }
  t.after(async () => {
    for (const client of clients) client.dispose()
    await rm(root, { recursive: true, force: true })
  })
  const requests = []
  let respond = async () => assert.fail('Unexpected request in an offline transport test')
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    requests.push({ url: String(url), init })
    return respond(url, init)
  })
  return { root, tokenFile, options, newClient, client: newClient(), requests,
    execution: { sessionId: 'transport-safety-session', signal: new AbortController().signal },
    respond: handler => { respond = handler },
    async rotate(token = nextToken) {
      const replacement = join(root, 'replacement-token')
      await writeFile(replacement, token, { mode: 0o600 })
      await rename(replacement, tokenFile)
    },
  }
}

async function nativeHost(t, config) {
  const ctx = new Context()
  const fibers = []
  t.after(async () => {
    for (const fiber of fibers.toReversed()) await fiber.dispose()
  })
  for (const [plugin, options] of [
    [SystemPrompt, { includeHarnessIdentity: false }],
    [ToolRuntime, { mode: 'native' }],
    [CommandRuntime, {}],
    [await import('../index.js'), config],
  ]) {
    const fiber = ctx.plugin(plugin, options)
    fibers.push(fiber)
    await fiber
  }
  await new Promise(resolve => setImmediate(resolve))
  const agent = { id: 'transport-safety-session', ctx }
  const signal = new AbortController().signal
  return {
    tool: () => ctx.tools.execute({ callId: 'runtime-safety', name: 'legion_runtime',
      arguments: { view: 'members' }, agent, signal }),
    command: () => ctx.commands.find(agent, 'legion').handler({
      agent, signal, rawInput: 'runtime members', attachments: [], commandId: 'runtime-safety',
    }),
  }
}

function diagnosticError(init) {
  const error = new Error(`Transport failed with ${init.headers.Authorization}`, {
    cause: new Error(`private cause: ${firstToken}`),
  })
  error.code = 'ECONNRESET'
  error.diagnostics = { authorization: init.headers.Authorization }
  return error
}

function failedStream(error, contentType = 'application/json') {
  return new Response(new ReadableStream({
    pull(controller) { controller.error(error) },
  }), { headers: { 'Content-Type': contentType } })
}

function assertRedacted(value) {
  for (const secret of [firstToken, nextToken, fallbackToken]) {
    assert.ok(!value.includes(secret), 'A raw synthetic credential escaped')
    assert.ok(!value.includes(encodeURIComponent(secret)), 'An encoded synthetic credential escaped')
  }
  assert.match(value, /\[redacted\]/)
}

for (const phase of ['fetch', 'response stream']) {
  test(`${phase} diagnostics are redacted through real native tools and commands`, async t => {
    const f = await setup(t)
    const native = await nativeHost(t, f.options)
    f.respond(async (_url, init) => {
      const error = diagnosticError(init)
      if (phase === 'fetch') throw error
      return failedStream(error)
    })
    const tool = await native.tool()
    const command = await native.command()
    assert.equal(tool.isError, true)
    assert.equal(command.kind, 'error')
    assertRedacted(JSON.stringify(tool))
    assertRedacted(JSON.stringify(command))
    assert.equal(f.requests.length, 2)
  })
}

test('transport errors retain their code without forwarding raw stacks, causes or diagnostics', async t => {
  const f = await setup(t)
  f.respond(async (_url, init) => { throw diagnosticError(init) })
  await assert.rejects(f.client.runtime({}, f.execution), error => {
    assert.equal(error.code, 'ECONNRESET')
    assertRedacted(`${error.message}\n${error.stack}\n${JSON.stringify(error)}`)
    return true
  })
})

test('runtime source is redacted in native and human results without rewriting the request URL or facts', async t => {
  const baseUrl = `https://service.invalid/${encodeURIComponent(firstToken)}`
  const f = await setup(t, { baseUrl })
  const native = await nativeHost(t, f.options)
  f.respond(async () => Response.json({
    note: firstToken, warming: true, provider_verified: false,
    harnesses: [{ key: 'fixture-member', installed: false }],
  }))
  const tool = await native.tool()
  const command = await native.command()
  assert.equal(tool.isError, false)
  assert.equal(command.kind, 'success')
  assertRedacted(JSON.stringify(tool))
  assertRedacted(JSON.stringify(command))
  assert.match(tool.value.source, /\/api\/legion\/harnesses$/)
  assert.equal(tool.value.runtime.warming, true)
  assert.equal(tool.value.runtime.provider_verified, false)
  assert.equal(tool.value.runtime.harnesses[0].installed, false)
  assert.equal(tool.value.verification, 'service_observation_only_no_execution')
  assert.deepEqual(f.requests.map(({ url }) => url), [
    `${baseUrl}/api/legion/harnesses`, `${baseUrl}/api/legion/harnesses`,
  ])
})

async function action(t, kind) {
  const f = await setup(t)
  let args
  if (kind === 'submit') {
    args = { key: 'stable-key', query: 'Synthetic original task' }
  } else {
    const { ref } = await f.client.store.reserve({
      sessionId: f.execution.sessionId, key: 'existing-task', query: 'Synthetic original task',
    })
    args = { task_ref: ref.id, operation_key: 'stable-key',
      ...(kind === 'message' ? { recipient: 'author', text: 'Synthetic feedback' } : {}) }
  }
  f.respond(async (url, init) => {
    const path = new URL(url).pathname
    const body = JSON.parse(init.body)
    if (kind === 'submit') {
      assert.equal(path, '/legion/api/legion/dispatch')
      return new Response(`event: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`,
        { headers: { 'Content-Type': 'text/event-stream' } })
    }
    assert.equal(path, `/legion/api/legion/team/${kind}`)
    return Response.json({ ok: true, team_id: body.team_id })
  })
  return { ...f, kind, args, field: kind === 'submit' ? 'submission' : 'acknowledgement',
    success: kind === 'submit' ? 'accepted' : 'received',
    call: (client = f.client, execution = f.execution, input = args) => client[kind](input, execution) }
}

function afterReservation(t, f, client, effect) {
  const method = f.kind === 'submit' ? 'reserve' : 'reserveOperation'
  const original = client.store[method].bind(client.store)
  let once = true
  return t.mock.method(client.store, method, async (...args) => {
    const result = await original(...args)
    if (once) {
      once = false
      await effect()
    }
    return result
  })
}

for (const kind of ['submit', 'message', 'resume']) {
  test(`${kind}: repairing credential-file permissions recovers the same identity and unsent key after reload`, async t => {
    const f = await action(t, kind)
    let client = f.client
    let firstRef
    // Repeated local failures must each be recoverable, not just the first one.
    for (let attempt = 0; attempt < 2; attempt += 1) {
      afterReservation(t, f, client, () => chmod(f.tokenFile, 0o644))
      const failed = await f.call(client)
      firstRef ??= failed.task_ref
      assert.equal(failed.task_ref, firstRef)
      assert.equal(failed[f.field], 'unconfirmed')
      assert.equal(f.requests.length, 0, 'An unsafe file must not fall back or use an earlier read')
      await f.rotate(firstToken)
      client.dispose()
      client = f.newClient()
    }
    const before = await readFile(join(f.options.stateDirectory, firstRef, 'ref.json'), 'utf8')
    const recovered = await f.call(client)
    assert.equal(recovered[f.field], f.success)
    assert.equal(recovered.task_ref, firstRef)
    assert.equal(f.requests.length, 1)
    assert.equal(f.requests[0].init.headers.Authorization, `Bearer ${firstToken}`)
    assert.equal(await readFile(join(f.options.stateDirectory, firstRef, 'ref.json'), 'utf8'), before)
    assert.equal((await f.call(f.newClient())).reused, true)
    assert.equal(f.requests.length, 1, 'An accepted recovery must not become a second dispatch')
  })

  test(`${kind}: concurrent same-key recovery across clients sends at most once`, async t => {
    const f = await action(t, kind)
    afterReservation(t, f, f.client, () => chmod(f.tokenFile, 0o644))
    await f.call()
    await f.rotate(firstToken)
    const results = await Promise.all([f.call(f.newClient()), f.call(f.newClient())])
    assert.equal(f.requests.length, 1)
    assert.equal(results.filter(result => result[f.field] === f.success && !result.reused).length, 1)
    assert.equal((await f.call(f.newClient()))[f.field], f.success)
    assert.equal(f.requests.length, 1)
  })

  test(`${kind}: an unsent reservation still refuses changed input`, async t => {
    const f = await action(t, kind)
    afterReservation(t, f, f.client, () => chmod(f.tokenFile, 0o644))
    await f.call()
    await f.rotate(firstToken)
    if (kind === 'resume') {
      // Resume has no mutable body; the session boundary still binds its key.
      await assert.rejects(f.call(f.newClient(), { ...f.execution, sessionId: 'other-session' }), /different DSH session/)
    } else {
      const changed = { ...f.args, [kind === 'submit' ? 'query' : 'text']: 'Different input' }
      await assert.rejects(f.call(f.newClient(), f.execution, changed), /different input/)
    }
    assert.equal(f.requests.length, 0)
    assert.equal((await f.call(f.newClient()))[f.field], f.success)
    assert.equal(f.requests.length, 1)
  })

  for (const phase of ['fetch', 'response stream']) {
    test(`${kind}: entering ${phase} keeps failure unconfirmed and never permits same-key resend`, async t => {
      const f = await action(t, kind)
      f.respond(async (_url, init) => {
        const error = diagnosticError(init)
        if (phase === 'fetch') throw error
        return failedStream(error, kind === 'submit' ? 'text/event-stream' : 'application/json')
      })
      const first = await f.call()
      assert.equal(first[f.field], 'unconfirmed')
      assertRedacted(JSON.stringify(first))
      await f.rotate(firstToken)
      const repeated = await f.call(f.newClient())
      assert.equal(repeated[f.field], 'unconfirmed')
      assert.equal(repeated.reused, true)
      assert.equal(f.requests.length, 1)
    })
  }

  for (const status of [401, 403, 408, 503]) {
    test(`${kind}: HTTP ${status} retains its status and never retries with the environment or after same-identity reload`, async t => {
      const f = await action(t, kind)
      f.respond(async () => Response.json({ reason: `Synthetic refusal ${firstToken}` }, { status }))
      if (status === 401 || status === 403) {
        await assert.rejects(f.call(), error => {
          assert.equal(error.status, status)
          assert.equal(error.name, 'LegionHttpError')
          assertRedacted(error.message)
          return true
        })
      } else {
        assert.equal((await f.call())[f.field], 'unconfirmed')
      }
      await f.rotate(firstToken)
      const repeated = await f.call(f.newClient())
      assert.equal(repeated[f.field], status === 401 || status === 403 ? 'rejected' : 'unconfirmed')
      assert.equal(repeated.http_status, status)
      assert.equal(repeated.reused, true)
      assert.equal(f.requests.length, 1)
    })
  }

  test(`${kind}: cancellation after reservation but before fetch can recover only that same unsent key`, async t => {
    const f = await action(t, kind)
    const controller = new AbortController()
    afterReservation(t, f, f.client, () => controller.abort())
    const first = await f.call(f.client, { ...f.execution, signal: controller.signal })
    assert.equal(first[f.field], 'unconfirmed')
    assert.equal(f.requests.length, 0)
    assert.equal((await f.call(f.newClient()))[f.field], f.success)
    assert.equal(f.requests.length, 1)
  })

  test(`${kind}: cancellation inside fetch never permits a same-key resend or remote cancel`, async t => {
    const f = await action(t, kind)
    const controller = new AbortController()
    f.respond(async (_url, init) => {
      controller.abort()
      init.signal.throwIfAborted()
    })
    assert.equal((await f.call(f.client, { ...f.execution, signal: controller.signal }))[f.field], 'unconfirmed')
    const recovered = await f.call(f.newClient())
    assert.equal(recovered[f.field], 'unconfirmed')
    assert.equal(recovered.reused, true)
    assert.equal(f.requests.length, 1)
    assert.ok(!f.requests.some(({ url }) => url.endsWith('/cancel')))
  })
}

test('request cancellation keeps AbortError and the caller signal without exposing its diagnostic', async t => {
  const f = await setup(t)
  const controller = new AbortController()
  const reason = new DOMException(`Caller stopped ${firstToken}`, 'AbortError')
  f.respond(async (_url, init) => {
    controller.abort(reason)
    init.signal.throwIfAborted()
  })
  await assert.rejects(f.client.runtime({}, { ...f.execution, signal: controller.signal }), error => {
    assert.equal(error.name, 'AbortError')
    assert.equal(error.code, 20)
    assertRedacted(error.message)
    return true
  })
  assert.equal(controller.signal.reason, reason, 'Do not mutate the host-owned abort reason')
  assert.equal(f.requests.length, 1)
})

test('the local HTTP wait still reports TimeoutError without retrying or cancelling the remote task', async t => {
  const f = await setup(t, { requestTimeoutMs: 100 })
  f.respond((_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
  const keepAlive = setTimeout(() => {}, 1000)
  try {
    await assert.rejects(f.client.runtime({}, f.execution), { name: 'TimeoutError', code: 23 })
  } finally {
    clearTimeout(keepAlive)
  }
  assert.equal(f.requests.length, 1)
  assert.equal(f.requests[0].init.signal.aborted, true)
})
