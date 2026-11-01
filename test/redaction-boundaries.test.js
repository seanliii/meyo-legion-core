import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LegionClient } from '../lib/client.js'
import { digest } from '../lib/store.js'
import { executeLegionCommand, formatLegionResult } from '../lib/commands.js'
import { fixture } from './fixture.js'

// All credentials and URLs below are synthetic, not copied from a live service.
async function setup(t) {
  const server = await fixture(t)
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-redaction-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const config = { baseUrl: server.baseUrl, stateDirectory, tokenEnv: 'TEST_TOKEN' }
  const client = new LegionClient(config, { TEST_TOKEN: 'fixture-token' })
  t.after(() => client.dispose())
  const execution = { sessionId: 'synthetic-redaction-session', signal: new AbortController().signal }
  const start = await client.submit({ key: 'original', query: 'Synthetic privacy boundary test' }, execution)
  return { server, client, config, execution, start, stateDirectory,
    status: () => client.status({ task_ref: start.task_ref }, execution) }
}

test('authoritative task identifiers cannot bypass credential redaction', async t => {
  const f = await setup(t)
  f.server.teams.get(f.start.team_id).tasks = {
    'author-fixture-token': { status: 'error', result: { error: 'synthetic execution failure' } },
  }
  const result = await f.status()
  assert.ok(!JSON.stringify(result).includes('fixture-token'))
  assert.ok(!formatLegionResult('status', result).includes('fixture-token'))
  assert.equal(result.runtime.tasks[0].task_id, 'author-[redacted]')
})

test('object keys and omission or depth diagnostics cannot echo credentials', async t => {
  const f = await setup(t)
  const projection = { omitted_fields: [], truncated_fields: [] }
  const input = { 'detail-fixture-token': { authorization: 'fixture-token' }, ordinary: 'kept' }
  const clean = f.client.clean(input, projection)
  f.client.clean({ nested: true }, projection, '$.fixture-token', 13)
  assert.ok(!JSON.stringify({ clean, projection }).includes('fixture-token'))
  assert.equal(clean.ordinary, 'kept')
  assert.ok(projection.omitted_fields.length > 0, 'sensitive keys are omitted, not silently merged')
  assert.ok(projection.truncated_fields.length > 0)
})

test('ordinary URL fields and prose redact sensitive query parameters, including encoded names', async t => {
  const f = await setup(t)
  const projection = { omitted_fields: [], truncated_fields: [] }
  const input = {
    url: 'https://service.invalid/view?share=synthetic-capability&view=compact',
    text: 'See /view?%73hare=synthetic-encoded-capability#section and https://service.invalid/?access_token=synthetic-access',
    nested: ['https://service.invalid/#share=synthetic-fragment', 'https://service.invalid/?q=public-search'],
  }
  const clean = f.client.clean(input, projection)
  for (const value of ['synthetic-capability', 'synthetic-encoded-capability', 'synthetic-access', 'synthetic-fragment']) {
    assert.ok(!JSON.stringify(clean).includes(value))
  }
  assert.match(clean.url, /view=compact/)
  assert.equal(clean.nested[1], input.nested[1], 'ordinary public research URLs remain usable')
})

test('status sanitizes capability URLs before both structured and human results', async t => {
  const f = await setup(t)
  f.server.teams.get(f.start.team_id).events.push({
    event_id: 2, event: 'final', data: {
      session: { url: 'https://service.invalid/view?share=synthetic-capability' },
      error: 'Visit https://service.invalid/view?share=synthetic-error-capability for details',
    },
  })
  const result = await f.status()
  assert.ok(!JSON.stringify(result).includes('synthetic-capability'))
  assert.ok(!formatLegionResult('status', result).includes('synthetic-error-capability'))
})

test('malformed operator binding JSON never echoes a content fragment', async t => {
  const f = await setup(t)
  const projectBindingFile = join(f.stateDirectory, 'binding.json')
  await writeFile(projectBindingFile, 'synthetic-private-fragment-this-is-not-json', { mode: 0o600 })
  const client = new LegionClient({ ...f.config, projectBindingFile }, { TEST_TOKEN: 'fixture-token' })
  t.after(() => client.dispose())
  await assert.rejects(client.projectBinding(), /^Error: Project binding file is not valid JSON$/)
  const result = await executeLegionCommand(client, {
    rawInput: 'start binding-check Synthetic test', agent: { id: f.execution.sessionId }, signal: f.execution.signal,
  })
  assert.equal(result.kind, 'error')
  assert.doesNotMatch(result.text, /synthetic-private|Unexpected token/)
  assert.equal(f.server.requests.filter(x => x.path.endsWith('/dispatch')).length, 1)
})

test('malformed service JSON reports its source, not its body', async t => {
  const f = await setup(t)
  t.mock.method(globalThis, 'fetch', async () => new Response('synthetic-response-secret-not-json', {
    headers: { 'Content-Type': 'application/json' },
  }))
  await assert.rejects(f.status(), /^Error: Legion response is not valid JSON$/)
})

test('corrupt persisted JSON errors expose no source snippets', async t => {
  const f = await setup(t)
  await writeFile(join(f.stateDirectory, f.start.task_ref, 'ref.json'), 'synthetic-private-state-not-json')
  await assert.rejects(f.status(), /^Error: Saved Legion data is not valid JSON$/)
  await assert.rejects(f.client.list(f.execution), /^Error: Saved Legion data is not valid JSON$/)
})

test('inline data URLs are removed from values, keys and diagnostic paths without erasing public URLs or long prose', async t => {
  const f = await setup(t)
  const data = 'data:image/png;base64,QUJDREVGRw=='
  const projection = { omitted_fields: [], truncated_fields: [] }
  const clean = f.client.clean({
    text: `Synthetic error (${data}); keep this explanation`,
    [data]: 'must not survive as a key',
    public_url: 'https://example.org/docs?q=syntax#examples',
    ordinary: 'x'.repeat(17000),
    nested: ['DATA:audio/wav;base64,QUJDREVGRw==', 'data:text/plain,PRIVATE_INLINE_DATA'],
  }, projection)
  f.client.clean({ nested: true }, projection, `$.${data}`, 13)
  assert.doesNotMatch(JSON.stringify({ clean, projection }), /data:(?:image|audio|text)|QUJDREVGRw==|PRIVATE_INLINE_DATA/i)
  assert.match(clean.text, /keep this explanation/)
  assert.equal(clean.public_url, 'https://example.org/docs?q=syntax#examples')
  assert.ok(clean.ordinary.startsWith('x'.repeat(16000)), 'Long ordinary text is not an encoded-media heuristic')
  assert.match(clean.ordinary, /\[truncated; original 17000 characters\]/)
  assert.ok(projection.omitted_fields.length)
  assert.ok(projection.truncated_fields.includes('$.ordinary'))
})

test('an unencoded data payload does not end at URI punctuation in text or diagnostics', async t => {
  const f = await setup(t)
  for (const data of [
    'data:text/plain;charset=utf-8,PRIVATE_HEAD)PRIVATE_TAIL;part=2',
    "data:text/plain,PRIVATE_HEAD'PRIVATE_TAIL;part=2",
    'data:image/svg+xml,%3Csvg%3EPRIVATE_HEAD)PRIVATE_TAIL%3C/svg%3E',
    'data:text/plain,PRIVATE_HEAD%29PRIVATE_TAIL',
    "data:text/plain;label='tag',PRIVATE_HEAD)PRIVATE_TAIL",
  ]) {
    const projection = { omitted_fields: [], truncated_fields: [] }
    const clean = f.client.clean({
      text: `Synthetic failure ${data} keep this explanation`,
      [data]: 'must not survive as a key',
      nested: [`"${data}" keep quoted explanation`, `<${data}> keep bracketed explanation`],
    }, projection)
    f.client.clean({ nested: true }, projection, `$.${data}`, 13)
    assert.doesNotMatch(JSON.stringify({ clean, projection }), /PRIVATE_HEAD|PRIVATE_TAIL|part=2|data:(?:text|image)|%3C/i)
    assert.match(clean.text, /keep this explanation/)
    assert.match(clean.nested[0], /keep quoted explanation/)
    assert.match(clean.nested[1], /keep bracketed explanation/)
    assert.ok(projection.omitted_fields.length)
    assert.ok(projection.truncated_fields.length)
  }
})

for (const kind of ['message', 'resume']) {
  for (const [label, record, expected] of [
    ['valid receipt with private extras', {
      acknowledgement: 'received', http_status: 202,
      reason: `Synthetic diagnostic fixture-token data:image/png;base64,QUJDREVGRw== ${'tail '.repeat(150)}`,
      task_ref: 'forged-ref', operation: 'forged-operation', reused: false, consumed_by_model_confirmed: true,
      server_response: { text: 'OLD_PRIVATE_BODY' }, 'data:audio/wav;base64,QUJDREVGRw==': 'OLD_PRIVATE_BODY',
    }, 'received'],
    ['rejected receipt', { acknowledgement: 'rejected', http_status: 403, reason: 'Synthetic refusal' }, 'rejected'],
    ['missing receipt', undefined, 'unconfirmed'],
    ['corrupt JSON', 'OLD_PRIVATE_BODY is not JSON', 'unconfirmed'],
    ['invalid shape', [{ acknowledgement: 'received' }], 'unconfirmed'],
    ['invalid acknowledgement', { acknowledgement: 'sent', reason: 'OLD_PRIVATE_BODY' }, 'unconfirmed'],
    ['invalid optional fields', { acknowledgement: 'received', http_status: '403', reason: { private: 'OLD_PRIVATE_BODY' } }, 'received'],
  ]) {
    test(`${kind} same-key recovery projects ${label} without replay, extra fields or rewriting disk`, async t => {
      const f = await setup(t)
      const args = { task_ref: f.start.task_ref, operation_key: 'saved-operation',
        ...(kind === 'message' ? { recipient: 'author', text: 'Synthetic feedback' } : {}) }
      const body = kind === 'message' ? { recipient: args.recipient, text: args.text, sender: 'commander' } : {}
      const ref = await f.client.store.get(f.execution.sessionId, f.start.task_ref)
      await f.client.store.reserveOperation(ref, kind, args.operation_key, JSON.stringify(body))
      const ackPath = join(f.client.store.path(ref.id), `ack-${digest(JSON.stringify([kind, args.operation_key]))}.json`)
      const bytes = record === undefined ? null : typeof record === 'string' ? record : JSON.stringify(record)
      if (bytes !== null) await writeFile(ackPath, bytes)
      const restored = new LegionClient(f.config, { TEST_TOKEN: 'fixture-token' })
      t.after(() => restored.dispose())
      const result = await restored[kind](args, f.execution)
      assert.equal(result.acknowledgement, expected)
      assert.equal(result.task_ref, ref.id)
      assert.equal(result.operation, kind)
      assert.equal(result.reused, true)
      if (kind === 'message') assert.equal(result.consumed_by_model_confirmed, false)
      assert.equal(result.server_response, undefined)
      assert.doesNotMatch(JSON.stringify(result), /OLD_PRIVATE_BODY|forged-|fixture-token|data:image|data:audio|QUJDREVGRw==/)
      if (result.reason !== undefined) {
        assert.equal(typeof result.reason, 'string')
        assert.ok(result.reason.length <= 500)
      }
      if (label === 'invalid optional fields') {
        assert.equal(result.http_status, undefined)
        assert.ok(result.record_warning, 'Discarded invalid optional fields remain visible as a bounded diagnostic')
      }
      if (label === 'valid receipt with private extras') assert.equal(result.http_status, 202)
      if (label === 'rejected receipt') assert.equal(result.http_status, 403)
      assert.equal(f.server.requests.filter(row => row.path.endsWith(`/team/${kind}`)).length, 0)
      if (bytes === null) await assert.rejects(readFile(ackPath), { code: 'ENOENT' })
      else assert.equal(await readFile(ackPath, 'utf8'), bytes, 'Reading old evidence must not migrate or erase it')
    })
  }
}

for (const action of ['cancel', 'unload']) {
  test(`ACK recovery checks ${action} after the asynchronous disk read`, async t => {
    const f = await setup(t)
    const controller = new AbortController()
    const execution = { ...f.execution, signal: controller.signal }
    const args = { task_ref: f.start.task_ref, operation_key: 'held-read', recipient: 'author', text: 'Synthetic feedback' }
    const ref = await f.client.store.get(execution.sessionId, f.start.task_ref)
    await f.client.store.reserveOperation(ref, 'message', args.operation_key,
      JSON.stringify({ recipient: args.recipient, text: args.text, sender: 'commander' }))
    const entered = Promise.withResolvers(), release = Promise.withResolvers()
    const read = f.client.store.readObservation.bind(f.client.store)
    t.mock.method(f.client.store, 'readObservation', async (...params) => {
      const value = await read(...params)
      if (params[1].startsWith('ack-')) { entered.resolve(); await release.promise }
      return value
    })
    const pending = f.client.message(args, execution)
    const rejected = assert.rejects(pending, /cancelled|disposed/i)
    try {
      await entered.promise
      if (action === 'cancel') controller.abort(new Error('Synthetic caller cancelled'))
      else f.client.dispose()
    } finally { release.resolve() }
    await rejected
    assert.equal(f.server.messageCount, 0)
  })
}
