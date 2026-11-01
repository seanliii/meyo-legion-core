import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import Schema from '@deepseek-ai/schemastery'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool, assertSupportedJsonSchema, validateJsonSchemaValue } from '@deepseek-ai/dsh-tools'
import { CommandRuntime } from '@deepseek-ai/dsh-commands'
import { LegionClient } from '../lib/client.js'
import { GitHubSources } from '../lib/sources.js'
import { SkillRegistry } from '@deepseek-ai/dsh-skill'
import { createAfterScheduleRecord, createEveryScheduleRecord } from '@deepseek-ai/dsh-schedule'

const methods = ['submit', 'list', 'status', 'message', 'resume', 'cancel', 'download']
const names = [...methods, 'runtime', 'discover', 'source', 'bundle', 'call'].map(method => `legion_${method}`).sort()
const taskRef = 'a'.repeat(32)
const artifactId = 'b'.repeat(32)
const isolatedConfig = {
  baseUrl: 'http://127.0.0.1:8000',
  tokenEnv: 'DSH_LEGION_PLUGIN_TEST_UNSET_TOKEN',
  stateDirectory: '/nonexistent/dsh-legion-plugin-test',
}

async function loadPlugin() {
  let plugin
  try {
    plugin = await import('../index.js')
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('/index.js')) throw error
  }
  assert.equal(typeof plugin?.apply, 'function', 'The plugin must export its native Cordis apply entry')
  return plugin
}

// Real installed DSH services, with no agent loop, model, HTTP server or disk store.
async function host(t, { commands = false } = {}) {
  t.mock.method(globalThis, 'fetch', async () => assert.fail('Plugin tests must not access the network'))
  const ctx = new Context()
  const fibers = []
  t.after(async () => {
    for (const fiber of fibers.toReversed()) await fiber.dispose()
  })
  const install = async (plugin, config) => {
    const fiber = ctx.plugin(plugin, config)
    fibers.push(fiber)
    await fiber
    return fiber
  }
  await install(SystemPrompt, { includeHarnessIdentity: false })
  await install(ToolRuntime, { mode: 'native' })
  if (commands) await install(CommandRuntime)
  return { ctx, install }
}

function recordClient(t, overrides = {}) {
  const calls = []
  for (const method of methods) {
    t.mock.method(LegionClient.prototype, method, async function (...args) {
      calls.push({ method, args, client: this })
      if (overrides[method]) return overrides[method](...args)
      return method === 'list'
        ? { tasks: [], source: 'local_recovery_index_not_live_status' }
        : { task_ref: taskRef, operation: method, nested: { complete: false, facts: [0, null, '中文'] } }
    })
  }
  return calls
}

test('Config is real Schemastery, fills defaults, and leaves baseUrl available for the environment fallback', async () => {
  const { Config } = await loadPlugin()
  assert.equal(typeof Config['~standard']?.validate, 'function')
  const config = Config({}).get()
  assert.equal(config.baseUrl, undefined)
  assert.equal(config.candidateHarnesses, undefined)
  assert.equal(config.tokenEnv, 'LEGION_PERSONAL_TOKEN')
  assert.equal(config.requestTimeoutMs, 30000)
  assert.equal(config.maxResponseBytes, 4 * 1024 * 1024)
  assert.equal(config.maxArtifactBytes, 50 * 1024 * 1024)
  assert.equal(config.maxEvents, 100)
  assert.ok(!Object.hasOwn(config, 'lifecycleSignal'))
  assert.deepEqual(Config['~standard'].validate({}).value.get(), config)
})

test('Config accepts a private tokenFile path without exposing inline credentials', async () => {
  const { Config } = await loadPlugin()
  assert.equal(Config({ tokenFile: '/private/fixture/login-token.txt' }).get().tokenFile, '/private/fixture/login-token.txt')
  for (const tokenFile of ['', 'relative/token', 1, null, '/tmp/a\0b', '/tmp/../secret']) {
    assert.throws(() => Config({ tokenFile }))
  }
})

test('runtime is a real native tool and filters authority from model arguments', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  assert.equal(typeof LegionClient.prototype.runtime, 'function')
  let call
  t.mock.method(LegionClient.prototype, 'runtime', async (args, execution) => {
    call = { args, execution }
    return { runtime: { installed: false }, verification: 'service_observation_only_no_execution' }
  })
  await install(plugin, isolatedConfig)
  const schema = ctx.tools.schemas().find(item => item.name === 'legion_runtime')
  assert.ok(schema, 'DSH must discover the registered runtime tool')
  assert.ok(!Object.hasOwn(schema.parameters.properties, 'tokenFile'))
  const signal = new AbortController().signal
  const agent = { id: 'runtime-agent', ctx }
  const result = await ctx.tools.execute({ callId: 'runtime-call', name: 'legion_runtime',
    arguments: { view: 'members', tokenFile: '/untrusted/model-token', force: true }, agent, signal })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.deepEqual(call.args, { view: 'members' })
  assert.equal(call.execution.sessionId, agent.id)
  assert.equal(call.execution.signal, signal)
})

test('Config accepts explicit candidate lists without defaulting, sorting or silently fixing invalid entries', async () => {
  const { Config } = await loadPlugin()
  assert.deepEqual(Config({ candidateHarnesses: [] }).get().candidateHarnesses, [])
  assert.deepEqual(Config({ candidateHarnesses: ['generic-reviewer', 'generic-member'] }).get().candidateHarnesses,
    ['generic-reviewer', 'generic-member'])
  for (const candidateHarnesses of [
    null, 'generic-member', {}, [null], [1], [''], [' '], [' generic-member'],
    ['generic member'], ['generic/member'], ['generic\nmember'], ['generic-member', 'generic-member'],
  ]) {
    assert.throws(() => Config({ candidateHarnesses }))
  }
})

test('Config rejects bad types, unsafe URLs, inline credentials and model-owned authority', async () => {
  const { Config } = await loadPlugin()
  for (const invalid of [
    { maxEvents: '100' }, { maxEvents: 0 }, { maxEvents: 501 }, { maxEvents: 1.5 },
    { requestTimeoutMs: 99 }, { requestTimeoutMs: 300001 }, { requestTimeoutMs: Infinity },
    { maxResponseBytes: 1023 }, { maxResponseBytes: 32 * 1024 * 1024 + 1 },
    { maxArtifactBytes: 0 }, { maxArtifactBytes: NaN }, { maxArtifactBytes: 1024 ** 3 + 1 },
    { tokenEnv: 'TOKEN=not-a-name' }, { stateDirectory: '' }, { stateDirectory: 42 },
    { projectBindingFile: 'relative/authority.json' },
    { baseUrl: 'file:///tmp/not-http' }, { baseUrl: 'http://remote.example' },
    { baseUrl: 'https://name:fixture-password@example.test' },
    { baseUrl: 'https://example.test?token=fixture-secret' },
    { baseUrl: 'https://example.test/#fragment' },
    { token: 'fixture-secret' }, { authorization: 'fixture-secret' },
    { lifecycleSignal: new AbortController().signal }, { env: {} }, { unknownOption: true },
  ]) {
    assert.throws(() => Config(invalid), undefined, `Invalid configuration keys: ${Object.keys(invalid)}`)
  }
  for (const baseUrl of ['http://localhost:8000', 'http://[::1]:8000', 'https://example.test/legion']) {
    assert.equal(Config({ baseUrl }).get().baseUrl, baseUrl)
  }
  assert.equal(Config({ projectBindingFile: '/fixture/authorized-project.json' }).get().projectBindingFile,
    '/fixture/authorized-project.json')
  try {
    Config({ token: 'fixture-secret' })
    assert.fail('Inline token must fail')
  } catch (error) {
    assert.ok(!error.message.includes('fixture-secret'))
  }
})

test('official Cordis validates plugin configuration before registering tools', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  await assert.rejects(install(plugin, { ...isolatedConfig, maxEvents: '100' }))
  assert.deepEqual(ctx.tools.schemas(), [])
})

test('official defineTool definitions route all seven calls with exact current agent and signal', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  assert.deepEqual(ctx.tools.schemas().map(tool => tool.name).sort(), names)
  const inputs = {
    submit: { key: 'new-turn', query: '保留原目标', context: '公开背景', previous_task_ref: taskRef },
    list: {},
    status: { task_ref: taskRef, after_event_id: 0, after_message_id: 7 },
    message: { task_ref: taskRef, operation_key: 'note-1', recipient: 'author', text: '新反馈' },
    resume: { task_ref: taskRef, operation_key: 'resume-1' },
    cancel: { task_ref: taskRef, reason: '用户请求取消' },
    download: { task_ref: taskRef, artifact_id: artifactId },
  }
  for (const [index, method] of methods.entries()) {
    const signal = new AbortController().signal
    const agent = { id: `agent-${index}`, session: { id: 'must-not-use-session.id' }, ctx }
    const tool = ctx.tools.get(`legion_${method}`)
    assertSupportedJsonSchema(tool.parameters)
    assertSupportedJsonSchema(tool.output.schema)
    assert.equal(typeof tool.output.render, 'function')
    assert.equal(tool.presentCall, undefined, 'Do not invent a host-only custom Web card')
    const result = await ctx.tools.execute({
      callId: `fixture-${index}`, name: tool.name, arguments: inputs[method], agent, signal,
    })
    assert.equal(result.isError, false, JSON.stringify(result))
    assert.deepEqual(validateJsonSchemaValue(tool.output.schema, result.value), [])
    assert.deepEqual(JSON.parse(result.content[0].text), result.value, 'Render canonical JSON, not another summary')
    const call = calls.at(-1)
    assert.equal(call.method, method)
    const execution = call.args.at(-1)
    assert.equal(execution.sessionId, agent.id)
    assert.equal(execution.signal, signal)
    assert.equal(call.args.length, method === 'list' ? 1 : 2)
    if (method !== 'list') assert.deepEqual(call.args[0], inputs[method])
  }
  assert.equal(calls.length, 7)
})

test('tool schemas validate input and undeclared fields never reach the client', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const exec = { agent: { id: 'current-agent', ctx }, signal: new AbortController().signal }
  const submit = ctx.tools.get('legion_submit')
  await assert.rejects(submit.execute({ key: 'missing-query' }, exec))
  await assert.rejects(submit.execute({ key: 'k', query: 10 }, exec))
  await assert.rejects(ctx.tools.get('legion_status').execute({ task_ref: taskRef, after_event_id: 1.5 }, exec))
  for (const after_message_id of [1.5, '7', null]) {
    await assert.rejects(ctx.tools.get('legion_status').execute({ task_ref: taskRef, after_message_id }, exec))
  }
  assert.equal(calls.length, 0)
  await submit.execute({
    key: 'safe', query: 'The task',
    sessionId: 'another-owner', signal: 'forged', token: 'synthetic-only',
    baseUrl: 'https://another.example', projectBindingFile: '/unauthorized.json',
    project_context: { workspace_root: '/unauthorized' },
    candidateHarnesses: ['injected-member'], candidate_harnesses: ['injected-member'],
    candidates: ['injected-member'],
    plan: { team_policy: { candidate_harnesses: ['injected-member'] } },
    team_policy: { candidate_harnesses: ['injected-member'] },
  }, exec)
  assert.deepEqual(calls[0].args[0], { key: 'safe', query: 'The task' })
  await ctx.tools.get('legion_status').execute({
    task_ref: taskRef, after_message_id: 7,
    recipient: 'another-recipient', mark_read: true, sessionId: 'another-owner',
  }, exec)
  assert.deepEqual(calls[1].args[0], { task_ref: taskRef, after_message_id: 7 })
  for (const tool of ctx.tools.schemas()) {
    for (const key of ['sessionId', 'signal', 'token', 'baseUrl', 'projectBindingFile', 'project_context',
      'candidateHarnesses', 'candidate_harnesses', 'candidates', 'plan', 'team_policy']) {
      assert.ok(!Object.hasOwn(tool.parameters.properties, key), `${tool.name} must not offer ${key}`)
    }
  }
})

test('native status mailbox cursor exposes the next commander reply without marking or consuming it', async t => {
  const plugin = await loadPlugin()
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-mailbox-entry-test-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const { ctx, install } = await host(t)
  // Keep the entry, client and scoped store real; only credential acquisition and
  // HTTP are doubled. No server, model, or native agent loop is started.
  t.mock.method(LegionClient.prototype, 'token', () => 'fixture-token')
  const mailbox = [
    { id: 2, from: 'author', to: 'commander', text: 'Please extract the Markdown tokens.',
      artifact_refs: [], ts: 1793110002, read_at: null },
    { id: 5, from: 'reviewer', to: 'commander', text: 'The next request is ready.',
      artifact_refs: [], ts: 1793110005, read_at: 1793110010.5 },
  ]
  const before = structuredClone(mailbox)
  const requests = []
  let team
  globalThis.fetch.mock.mockImplementation(async (url, options) => {
    const parsed = new URL(url)
    requests.push(`${options.method} ${parsed.pathname}`)
    if (parsed.pathname === '/api/legion/dispatch') {
      assert.equal(options.method, 'POST')
      const body = JSON.parse(options.body)
      team = { team_id: body.team_id, conversation_id: body.session_id,
        tasks: { author: { status: 'running' } }, mailboxes: { commander: mailbox } }
      return new Response(`event: meta\ndata: ${JSON.stringify({ sid: body.session_id, team_id: body.team_id })}\n\n`,
        { headers: { 'Content-Type': 'text/event-stream' } })
    }
    assert.equal(options.method, 'GET')
    assert.equal(parsed.searchParams.get('team_id'), team.team_id)
    assert.equal(parsed.searchParams.has('after_message_id'), false, 'Mail pagination is a local projection')
    if (parsed.pathname === '/api/legion/team') return Response.json(team)
    assert.equal(parsed.pathname, '/api/legion/events', 'Status must not use a mark-read or execution endpoint')
    assert.equal(parsed.searchParams.get('after'), '100', 'Mail cursor must not replace the event cursor')
    return Response.json({ team_id: team.team_id, events: [], last_event_id: 100 })
  })
  await install(plugin, { ...isolatedConfig, stateDirectory, maxEvents: 1 })
  const exec = { agent: { id: 'mailbox-entry-agent', ctx }, signal: new AbortController().signal }
  const submitted = await ctx.tools.execute({
    ...exec, callId: 'mailbox-entry-submit', name: 'legion_submit',
    arguments: { key: 'mailbox-entry', query: 'Keep the original task and observe replies.' },
  })
  assert.equal(submitted.isError, false, JSON.stringify(submitted))
  assert.equal(submitted.value.submission, 'accepted')
  const pages = [
    { after: undefined, ids: [2], next: 2, more: true, read: [null] },
    { after: 2, ids: [5], next: 5, more: false, read: [1793110010.5] },
    { after: 5, ids: [], next: 5, more: false, read: [] },
    { after: undefined, ids: [2], next: 2, more: true, read: [null] },
  ]
  for (const [index, page] of pages.entries()) {
    const result = await ctx.tools.execute({
      ...exec, callId: `mailbox-entry-status-${index}`, name: 'legion_status',
      arguments: { task_ref: submitted.value.task_ref, after_event_id: 100,
        ...(page.after === undefined ? {} : { after_message_id: page.after }) },
    })
    assert.equal(result.isError, false, JSON.stringify(result))
    const value = result.value
    assert.deepEqual(value.mailbox.messages.map(message => message.id), page.ids,
      'The native tool must forward after_message_id instead of replaying page one')
    assert.equal(value.mailbox.after_message_id, page.after ?? 0)
    assert.equal(value.mailbox.next_message_id, page.next)
    assert.equal(value.mailbox.more_messages, page.more)
    assert.equal(value.mailbox.truncated, page.more)
    assert.equal(value.mailbox.total_messages, 2)
    assert.equal(value.mailbox.state, 'available')
    assert.equal(value.mailbox.source, 'team.mailboxes.commander')
    assert.equal(value.mailbox.trust, 'untrusted_source')
    assert.deepEqual(value.mailbox.messages.map(message => message.read_at), page.read)
    assert.equal(value.mailbox.read_receipts_modified, false)
    assert.equal(value.mailbox.model_consumption, 'not_observed')
    assert.equal(value.next_event_id, 100)
    assert.deepEqual(JSON.parse(result.content[0].text), value, 'The model-visible result must retain mailbox facts')
    if (index === 1) {
      assert.equal(value.mailbox.messages[0].from, 'reviewer')
      assert.equal(value.mailbox.messages[0].text, 'The next request is ready.')
    }
  }
  assert.deepEqual(mailbox, before)
  assert.deepEqual(requests, [
    'POST /api/legion/dispatch',
    ...Array.from({ length: 4 }, () => ['GET /api/legion/team', 'GET /api/legion/events']).flat(),
  ])
})

test('native tools and continue use configured candidates, rejecting same-key changes after plugin reload', async t => {
  const plugin = await loadPlugin()
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-candidate-plugin-test-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const { ctx, install } = await host(t, { commands: true })
  // Only credential acquisition and HTTP are doubled. Use the real client,
  // serializer and durable reservation, without a model or a live service.
  t.mock.method(LegionClient.prototype, 'token', () => 'fixture-token')
  const bodies = []
  globalThis.fetch.mock.mockImplementation(async (url, options) => {
    assert.equal(new URL(url).pathname, '/api/legion/dispatch')
    const body = JSON.parse(options.body)
    bodies.push(body)
    return new Response(`event: meta\ndata: ${JSON.stringify({ sid: body.session_id, team_id: body.team_id })}\n\n`,
      { headers: { 'Content-Type': 'text/event-stream' } })
  })
  const agent = { id: 'candidate-fixture-agent', ctx }
  const signal = new AbortController().signal
  const args = {
    key: 'configured-task', query: 'Synthetic task',
    candidateHarnesses: ['injected-member'], candidate_harnesses: ['injected-member'],
    candidates: ['injected-member'],
    plan: { team_policy: { candidate_harnesses: ['injected-member'] } },
  }
  for (const [i, candidateHarnesses] of [
    ['generic-member'], ['generic-member'], ['generic-reviewer'],
  ].entries()) {
    const fiber = await install(plugin, { ...isolatedConfig, stateDirectory, candidateHarnesses })
    const result = await ctx.tools.execute({
      name: 'legion_submit', arguments: args, callId: `candidate-call-${i}`, agent, signal,
    })
    if (i === 2) {
      assert.equal(result.isError, true)
      assert.match(result.content[0].text, /different input/i)
    } else {
      assert.equal(result.isError, false, JSON.stringify(result))
      assert.equal(result.value.submission, 'accepted')
      if (i === 1) assert.equal(result.value.reused, true)
      if (i === 0) {
        await new Promise(resolve => setImmediate(resolve))
        const next = await ctx.commands.find(agent, 'legion').handler({
          agent, signal, rawInput: `continue ${result.value.task_ref} follow-up New user feedback`,
          attachments: [], commandId: 'candidate-follow-up',
        })
        assert.equal(next.kind, 'success', JSON.stringify(next))
      }
    }
    await fiber.dispose()
  }
  assert.equal(bodies.length, 2)
  for (const body of bodies) {
    assert.deepEqual(body.plan, { team_policy: { candidate_harnesses: ['generic-member'] } })
    assert.equal(body.mode, 'auto')
    assert.equal(body.allow_direct_model, false)
  }
  assert.equal(bodies[0].session_id, bodies[1].session_id)
  assert.notEqual(bodies[0].team_id, bodies[1].team_id)
  assert.equal(bodies[1].question, 'New user feedback')
})

test('missing identity or cancellation is an error; there is no shared or latest-session fallback', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const tool = ctx.tools.get('legion_list')
  await assert.rejects(tool.execute({}, { signal: new AbortController().signal }), /DSH|会话/)
  await assert.rejects(tool.execute({}, { agent: { id: 'current-agent' } }), /signal|取消|AbortSignal/)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(tool.execute({}, { agent: { id: 'current-agent' }, signal: controller.signal }),
    { name: 'AbortError' })
  assert.equal(calls.length, 0)
})

test('official runtime rejects non-JSON output and retains safe client failures as errors', async t => {
  const plugin = await loadPlugin()
  recordClient(t, {
    list: () => ({ notJson: undefined }),
    status: () => { throw new Error('Legion HTTP 401: configure the personal token environment variable') },
  })
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const execution = { agent: { id: 'fixture-agent', ctx }, signal: new AbortController().signal }
  const invalid = await ctx.tools.execute({ ...execution, callId: 'invalid-json', name: 'legion_list', arguments: {} })
  assert.equal(invalid.isError, true)
  assert.ok(!Object.hasOwn(invalid, 'value'))
  const failed = await ctx.tools.execute({
    ...execution, callId: 'client-error', name: 'legion_status', arguments: { task_ref: taskRef },
  })
  assert.equal(failed.isError, true)
  assert.match(failed.content[0].text, /401/)
  assert.ok(!Object.hasOwn(failed, 'value'))
})

test('missing token does not block installation; execution fails before network or state writes', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  assert.equal(process.env[isolatedConfig.tokenEnv], undefined, 'This synthetic variable must remain unset')
  await install(plugin, isolatedConfig)
  const result = await ctx.tools.execute({
    name: 'legion_submit', arguments: { key: 'token-setup', query: 'A synthetic task' },
    callId: 'missing-token', agent: { id: 'fixture-agent', ctx }, signal: new AbortController().signal,
  })
  assert.equal(result.isError, true)
  assert.match(result.content[0].text, /DSH_LEGION_PLUGIN_TEST_UNSET_TOKEN/)
  assert.match(result.content[0].text, /environment|环境/)
  assert.equal(globalThis.fetch.mock.callCount(), 0)
})

test('public discovery and pinned source reading work without a Legion token and ignore undeclared authority', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const calls = []
  t.mock.method(GitHubSources.prototype, 'search', async (args, execution) => {
    calls.push({ method: 'search', args, execution })
    return { items: [{ repository: 'fixture/project' }] }
  })
  t.mock.method(GitHubSources.prototype, 'read', async (args, execution) => {
    calls.push({ method: 'read', args, execution })
    return { repository: args.repository, commit: args.commit, content: 'fixture source', trust: 'untrusted_source' }
  })
  const agent = { id: 'public-source-agent', ctx }
  const controller = new AbortController()
  const signal = controller.signal
  const search = await ctx.tools.execute({
    name: 'legion_discover', arguments: { query: 'markdown', source: 'github', token: 'ignored' },
    callId: 'discover-no-token', agent, signal,
  })
  assert.equal(search.isError, false, JSON.stringify(search))
  assert.equal(search.value.github.state, 'available')
  assert.deepEqual(calls[0].args, { query: 'markdown', limit: 10 })
  const read = await ctx.tools.execute({
    name: 'legion_source',
    arguments: { action: 'read', repository: 'fixture/project', commit: 'a'.repeat(40),
      path: 'README.md', token: 'ignored', baseUrl: 'https://untrusted.example' },
    callId: 'source-no-token', agent, signal,
  })
  assert.equal(read.isError, false, JSON.stringify(read))
  assert.equal(read.value.content, 'fixture source')
  assert.deepEqual(calls[1].args, { repository: 'fixture/project', commit: 'a'.repeat(40), path: 'README.md' })
  assert.equal(calls[1].execution.signal.aborted, false)
  controller.abort()
  assert.equal(calls[1].execution.signal.aborted, true, 'The original host signal still cancels source reads')
  assert.equal(globalThis.fetch.mock.callCount(), 0)
})

test('bundle wiring records fetched commit and license facts, writes through native tools, and never installs', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const directory = await mkdtemp(join(tmpdir(), 'legion-bundle-wiring-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const writes = []
  ctx.tools.register(defineTool({
    name: 'write', description: 'Fixture native workspace write',
    parameters: { file_path: { type: 'string', required: true }, content: { type: 'string', required: true } },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: async args => { writes.push(args); return 'written' },
  }))
  const fetched = t.mock.method(GitHubSources.prototype, 'inspect', async args => ({
    status: 'inspected', repository: args.repository, commit: 'c'.repeat(40),
    url: `https://github.com/${args.repository}/tree/${'c'.repeat(40)}`,
    fetched_at: '2026-10-27T00:00:00.000Z', source: { sha256: 'd'.repeat(64) },
    license: { status: 'detected', assessment: 'not_performed',
      file: { path: 'LICENSE', content: 'Fixture license text', sha256: 'e'.repeat(64), truncated: false } },
    readme: null, manifests: [], truncated: false, errors: [],
  }))
  const result = await ctx.tools.execute({
    name: 'legion_bundle', callId: 'bundle-wiring',
    arguments: {
      directory,
      source_refs: [{ repository: 'fixture/project', ref: 'v1.0.0' }],
      spec: { package_name: 'dsh-plugin-fixture', version: '1.0.0', description: 'Fixture adapter',
        plugins: [{ id: 'inspect', title: 'Inspect', description: 'Inspect capability', entry: 'inspect.js' }],
        files: [{ path: 'inspect.js', content: 'export function apply() {}' }] },
      provenance: [{ commit: 'forged' }], token: 'forged',
    },
    agent: { id: 'bundle-agent', ctx }, signal: new AbortController().signal,
  })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.equal(result.value.state, 'written_not_installed')
  assert.equal(result.value.execution_verified, false)
  assert.equal(fetched.mock.callCount(), 1)
  const provenance = JSON.parse(writes.find(file => file.file_path.endsWith('/provenance.json')).content)
  assert.equal(provenance[0].commit, 'c'.repeat(40))
  assert.equal(provenance[0].license.file.content, 'Fixture license text')
  assert.ok(!JSON.stringify(provenance).includes('forged'))
  assert.equal(globalThis.fetch.mock.callCount(), 0)
})

test('plugin unload cancels a pending public source read without aborting the caller', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  const fiber = await install(plugin, isolatedConfig)
  const started = Promise.withResolvers()
  t.mock.method(GitHubSources.prototype, 'read', async (_args, { signal }) => {
    started.resolve()
    return new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    })
  })
  const controller = new AbortController()
  const pending = ctx.tools.execute({
    name: 'legion_source', callId: 'unload-source',
    arguments: { action: 'read', repository: 'fixture/project', commit: 'a'.repeat(40), path: 'README.md' },
    agent: { id: 'source-agent', ctx }, signal: controller.signal,
  })
  await started.promise
  await fiber.dispose()
  const result = await pending
  assert.equal(result.isError, true)
  assert.equal(controller.signal.aborted, false)
})

for (const action of ['github', 'inspect']) {
  test(`panel ${action} is cancelled by plugin unload without a core token or caller cancellation`, async t => {
    const plugin = await loadPlugin()
    const { ctx, install } = await host(t, { commands: true })
    const started = Promise.withResolvers()
    const response = Promise.withResolvers()
    let requestSignal
    globalThis.fetch.mock.mockImplementation((url, options) => {
      assert.equal(new URL(url).origin, 'https://api.github.com')
      assert.equal(options.headers.Authorization, undefined)
      assert.equal(options.credentials, 'omit')
      requestSignal = options.signal
      started.resolve()
      return response.promise
    })
    assert.equal(process.env[isolatedConfig.tokenEnv], undefined)
    const fiber = await install(plugin, { ...isolatedConfig, requestTimeoutMs: 1000 })
    await new Promise(resolve => setImmediate(resolve))
    const events = []
    const agent = {
      id: 'panel-source-agent', ctx,
      session: { id: 'not-the-agent-id', append(type, data) { events.push({ type, data }); return events.length } },
    }
    const caller = new AbortController()
    const args = action === 'github' ? { query: 'offline-public-fixture' } : { repository: 'fixture/project' }
    const pending = ctx.commands.execute(agent, `/legion-panel ${JSON.stringify({ action, args })}`, [], caller.signal)
    await started.promise
    await fiber.dispose()
    const abortedAfterUnload = requestSignal.aborted
    // Release even an abort-ignoring transport before asserting, so red tests do not hang.
    response.resolve(action === 'github'
      ? Response.json({ total_count: 0, incomplete_results: false, items: [] })
      : Response.json({ message: 'Fixture not found' }, { status: 404 }))
    const settled = await pending
    assert.equal(abortedAfterUnload, true, 'Plugin lifetime must reach public workbench requests')
    assert.equal(caller.signal.aborted, false)
    assert.equal(settled.result.kind, 'error')
    assert.equal(JSON.parse(settled.result.text).diagnostics.code, 'ABORTED')
    assert.deepEqual(events.map(event => event.type), ['command/run', 'command/done'])
    assert.ok(events.every(event => event.data.commandId === settled.commandId))
    assert.equal(Object.hasOwn(events[0].data, 'args'), false)
    assert.equal(ctx.commands.list(agent).length, 0)
    assert.equal(globalThis.fetch.mock.callCount(), 1)
  })
}

test('panel human commands retain the exact agent and caller cancellation without ToolExecution methods', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t, { commands: true })
  const started = Promise.withResolvers()
  const response = Promise.withResolvers()
  let requestSignal
  let pendingRequest = true
  const searchResponse = () => Response.json({ total_count: 0, incomplete_results: false, items: [] })
  globalThis.fetch.mock.mockImplementation((url, options) => {
    assert.equal(new URL(url).origin, 'https://api.github.com')
    assert.equal(options.headers.Authorization, undefined)
    if (!pendingRequest) return searchResponse()
    requestSignal = options.signal
    started.resolve()
    return response.promise
  })
  await install(plugin, isolatedConfig)
  await new Promise(resolve => setImmediate(resolve))
  const events = []
  const agent = {
    id: 'panel-current-agent', ctx,
    session: { id: 'not-the-agent-id', append(type, data) { events.push({ type, data }); return events.length } },
  }
  const schemas = ctx.tools.schemas.bind(ctx.tools)
  t.mock.method(ctx.tools, 'schemas', current => {
    assert.equal(current, agent, 'The native tool view must receive the original Agent object')
    return schemas(current)
  })
  const caller = new AbortController()
  const tools = await ctx.commands.execute(agent,
    '/legion-panel {"action":"tools","args":{"query":"legion_source","limit":1}}', [], caller.signal)
  assert.equal(tools.result.kind, 'success')
  assert.deepEqual(JSON.parse(tools.result.text).value.tools.map(tool => tool.name), ['legion_source'])
  const line = '/legion-panel {"action":"github","args":{"query":"offline-public-fixture"}}'
  const pending = ctx.commands.execute(agent, line, [], caller.signal)
  const rejected = assert.rejects(pending, { name: 'AbortError' })
  await started.promise
  caller.abort(new DOMException('Fixture caller cancelled', 'AbortError'))
  response.resolve(searchResponse())
  await rejected
  assert.equal(requestSignal.aborted, true)
  assert.equal(ctx.commands.list(agent).length, 2, 'A caller abort must not unload the plugin')
  pendingRequest = false
  const next = await ctx.commands.execute(agent, line, [], new AbortController().signal)
  assert.equal(next.result.kind, 'success', 'A fresh command must not inherit another caller cancellation')
  assert.equal(globalThis.fetch.mock.callCount(), 2)
  for (let index = 0; index < events.length; index += 2) {
    assert.equal(events[index].type, 'command/run')
    assert.equal(events[index + 1].type, 'command/done')
    assert.equal(events[index].data.commandId, events[index + 1].data.commandId)
  }
})

test('panel unload preserves an in-flight submit reference and unconfirmed receipt across reinstall without redispatch', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t, { commands: true })
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-panel-unload-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const config = { ...isolatedConfig, stateDirectory }
  // Real client and recovery store; only token acquisition and HTTP are doubled.
  t.mock.method(LegionClient.prototype, 'token', () => 'fixture-only-panel-token')
  const cancelled = t.mock.method(LegionClient.prototype, 'cancel', () => assert.fail('No remote cancel on unload'))
  const started = Promise.withResolvers()
  let requestSignal
  let dispatch
  globalThis.fetch.mock.mockImplementation((url, options) => {
    assert.equal(new URL(url).pathname, '/api/legion/dispatch', 'No remote cancel or other operation is allowed')
    assert.equal(options.method, 'POST')
    dispatch = JSON.parse(options.body)
    requestSignal = options.signal
    started.resolve()
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true })
    })
  })
  const fiber = await install(plugin, config)
  await new Promise(resolve => setImmediate(resolve))
  const events = []
  const agent = {
    id: 'panel-submit-agent', ctx,
    session: { id: 'not-the-agent-id', append(type, data) { events.push({ type, data }); return events.length } },
  }
  const caller = new AbortController()
  const args = { key: 'original-key', query: 'Keep  this\nhuman task', context: 'Original human context' }
  const line = `/legion-panel ${JSON.stringify({ action: 'submit', args })}`
  const pending = ctx.commands.execute(agent, line, [], caller.signal)
  await started.promise
  await fiber.dispose()
  const settled = await pending
  const receipt = JSON.parse(settled.result.text).value
  assert.equal(requestSignal.aborted, true)
  assert.equal(caller.signal.aborted, false)
  assert.equal(settled.result.kind, 'success', 'A recoverable unknown receipt must not be replaced with an abort error')
  assert.equal(receipt.submission, 'unconfirmed')
  assert.equal(receipt.remote_task_cancelled, false)
  assert.match(receipt.task_ref, /^[a-f0-9]{32}$/)
  assert.equal(receipt.team_id, dispatch.team_id)
  assert.equal(receipt.session_id, dispatch.session_id)
  assert.ok(dispatch.question.startsWith(args.query + '\n\n'))
  assert.ok(dispatch.question.endsWith(args.context))
  assert.equal(events[0].data.commandId, settled.commandId)
  assert.equal(events[1].data.commandId, settled.commandId)
  assert.equal(JSON.parse(events[1].data.text).value.task_ref, receipt.task_ref)

  await install(plugin, config)
  await new Promise(resolve => setImmediate(resolve))
  const repeated = await ctx.commands.execute(agent, line, [], caller.signal)
  const recovered = JSON.parse(repeated.result.text).value
  assert.equal(repeated.result.kind, 'success')
  assert.equal(recovered.submission, 'unconfirmed')
  assert.equal(recovered.task_ref, receipt.task_ref)
  assert.equal(recovered.reused, true)
  const tasks = await ctx.commands.execute(agent, '/legion-panel {"action":"tasks"}', [], caller.signal)
  assert.deepEqual(JSON.parse(tasks.result.text).value.tasks.map(task => task.task_ref), [receipt.task_ref])
  const foreign = await ctx.commands.execute({ ...agent, id: 'different-agent' },
    '/legion-panel {"action":"tasks"}', [], caller.signal)
  assert.deepEqual(JSON.parse(foreign.result.text).value.tasks, [], 'The recovery owner is agent.id, not session.id')
  assert.equal(globalThis.fetch.mock.callCount(), 1, 'Unknown submissions must not dispatch again after reload')
  assert.equal(cancelled.mock.callCount(), 0)
})

test('submission includes only explicitly selected visible tool schemas, not remote tool grants', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const { ctx, install } = await host(t)
  await install(plugin, isolatedConfig)
  const agent = { id: 'capability-agent', ctx }
  const signal = new AbortController().signal
  const result = await ctx.tools.execute({
    name: 'legion_submit',
    arguments: { key: 'capabilities', query: 'Use an available capability', context: 'User context',
      tool_names: ['legion_source'] },
    callId: 'capabilities-submit', agent, signal,
  })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.match(calls[0].args[0].context, /User context/)
  assert.match(calls[0].args[0].context, /legion_source/)
  assert.match(calls[0].args[0].context, /grants_remote_access.*false/)
  assert.ok(!Object.hasOwn(calls[0].args[0], 'tool_names'))
  const denied = await ctx.tools.execute({
    name: 'legion_submit', arguments: { key: 'unknown', query: 'Task', tool_names: ['missing_tool'] },
    callId: 'capability-denied', agent, signal,
  })
  assert.equal(denied.isError, true)
  assert.equal(calls.length, 1)
})

test('ecosystem instructions are optional on-demand skills, disappear on unload, and do not block delegation', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t)
  const fiber = await install(plugin, isolatedConfig)
  await install(SkillRegistry)
  await new Promise(resolve => setImmediate(resolve))
  const skill = await ctx.skills.get('legion-ecosystem')
  assert.ok(skill, 'The native skill catalog must expose the ecosystem entry')
  assert.match(skill.content, /legion_discover/)
  assert.match(skill.content, /legion_bundle/)
  assert.match(skill.content, /legion_call/)
  assert.match(skill.content, /plugin_manager/)
  assert.match(skill.content, /cordis-plugin-development/)
  assert.match(skill.content, /references\/host-plugin\.md/)
  assert.match(skill.content, /references\/practices\.md/)
  assert.match(skill.content, /service injection is not tool-execution approval/)
  assert.match(skill.content, /module import, fiber activation and actual tool execution are separate facts/)
  assert.match(skill.content, /file:.*tar/)
  assert.match(skill.content, /link:.*does not install.*dependencies/)
  assert.match(skill.content, /initially disabled/)
  assert.match(skill.content, /patch rowId.*moduleName.*not.*entryId/)
  assert.match(skill.content, /disabled.*entryId.*do not guess/)
  assert.match(skill.content, /list_plugins.*entryId/)
  assert.match(skill.content, /installation, selection, activation and actual calls are separate facts/)
  assert.match(skill.content, /disabled does not prohibit.*build scripts/)
  await fiber.dispose()
  assert.equal(await ctx.skills.get('legion-ecosystem'), undefined)
})

test('optional commands arrive, unload and return without disposing the client or leaking registrations', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const dispose = t.mock.method(LegionClient.prototype, 'dispose', function () {})
  const { ctx, install } = await host(t)
  const fiber = await install(plugin, isolatedConfig)
  assert.deepEqual(ctx.tools.schemas().map(tool => tool.name).sort(), names)
  assert.equal(ctx.get('commands'), undefined)
  const firstCommands = await install(CommandRuntime)
  await new Promise(resolve => setImmediate(resolve))
  const agent = { id: 'command-agent', ctx }
  assert.deepEqual(ctx.commands.list(agent).map(command => command.name).sort(), ['legion', 'legion-panel'])
  assert.equal(ctx.commands.find(agent, 'legion-panel').recordInput, false)
  const signal = new AbortController().signal
  const result = await ctx.commands.find(agent, 'legion').handler({
    agent, rawInput: ' list', attachments: [], signal, commandId: 'fixture-command',
  })
  assert.equal(result.kind, 'success')
  assert.equal(calls[0].method, 'list')
  assert.deepEqual(calls[0].args[0], { sessionId: agent.id, signal })
  const panel = await ctx.commands.find(agent, 'legion-panel').handler({
    agent, rawInput: '{"action":"tools","args":{"query":"legion","limit":30}}',
    attachments: [], signal, commandId: 'fixture-workbench-command',
  })
  assert.equal(panel.kind, 'success')
  const panelData = JSON.parse(panel.text)
  assert.equal(panelData.action, 'tools')
  assert.ok(panelData.value.tools.some(tool => tool.name === 'legion_submit'))
  assert.ok(panelData.value.tools.every(tool => typeof tool.name === 'string'))
  await firstCommands.dispose()
  assert.equal(dispose.mock.callCount(), 0, 'Losing only the optional command service must not dispose tool work')
  assert.equal(ctx.tools.schemas().length, names.length)
  await install(CommandRuntime)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(ctx.commands.list(agent).length, 2)
  await fiber.dispose()
  assert.equal(ctx.commands.list(agent).length, 0)
  assert.equal(ctx.tools.schemas().length, 0)
  assert.equal(dispose.mock.callCount(), 1)
})

test('unload aborts this plugin wait and disposes once, without cancelling remote tasks or caller signal', async t => {
  const plugin = await loadPlugin()
  const started = Promise.withResolvers()
  let client
  t.mock.method(LegionClient.prototype, 'submit', function () {
    client = this
    started.resolve()
    return new Promise((resolve, reject) => {
      this.lifecycleSignal.addEventListener('abort', () => reject(this.lifecycleSignal.reason), { once: true })
    })
  })
  const realDispose = LegionClient.prototype.dispose
  const disposed = t.mock.method(LegionClient.prototype, 'dispose', function () { return realDispose.call(this) })
  const cancelled = t.mock.method(LegionClient.prototype, 'cancel', async () => assert.fail('No remote cancel on unload'))
  const { ctx, install } = await host(t, { commands: true })
  const fiber = await install(plugin, isolatedConfig)
  const controller = new AbortController()
  const waiting = ctx.tools.execute({
    callId: 'waiting', name: 'legion_submit', arguments: { key: 'waiting', query: 'Synthetic waiting call' },
    agent: { id: 'fixture-agent', ctx }, signal: controller.signal,
  })
  await started.promise
  assert.equal(client.lifecycleSignal.aborted, false)
  await fiber.dispose()
  const result = await waiting
  assert.equal(result.isError, true)
  assert.equal(client.lifecycleSignal.aborted, true)
  assert.equal(controller.signal.aborted, false)
  assert.equal(disposed.mock.callCount(), 1)
  assert.equal(cancelled.mock.callCount(), 0)
  await fiber.dispose()
  assert.equal(disposed.mock.callCount(), 1)
  assert.equal(ctx.tools.schemas().length, 0)
  assert.equal(ctx.commands.list({ id: 'fixture-agent', ctx }).length, 0)
})

test('the same native host can unload and reinstall the plugin with a fresh client and no duplicate tools or commands', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const realDispose = LegionClient.prototype.dispose
  const disposed = t.mock.method(LegionClient.prototype, 'dispose', function () { return realDispose.call(this) })
  const { ctx, install } = await host(t, { commands: true })
  const agent = { id: 'reinstall-agent', ctx }
  const clients = []
  for (let round = 0; round < 2; round++) {
    const fiber = await install(plugin, isolatedConfig)
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(ctx.tools.schemas().map(tool => tool.name).sort(), names)
    assert.deepEqual(ctx.commands.list(agent).map(command => command.name).sort(), ['legion', 'legion-panel'])
    const result = await ctx.tools.execute({
      name: 'legion_list', arguments: {}, callId: `reinstall-${round}`,
      agent, signal: new AbortController().signal,
    })
    assert.equal(result.isError, false)
    const client = calls.at(-1).client
    clients.push(client)
    assert.equal(client.lifecycleSignal.aborted, false)
    assert.equal(client.controller.signal.aborted, false)
    await fiber.dispose()
    assert.equal(client.lifecycleSignal.aborted, true)
    assert.equal(client.controller.signal.aborted, true)
    assert.equal(ctx.tools.schemas().length, 0)
    assert.equal(ctx.commands.list(agent).length, 0)
    assert.equal(disposed.mock.callCount(), round + 1)
  }
  assert.notEqual(clients[0], clients[1])
  assert.deepEqual(calls.map(call => call.method), ['list', 'list'])
  assert.equal(globalThis.fetch.mock.callCount(), 0)
})

test('real client sanitization keeps a synthetic HTTP token echo out of native tools and commands', async t => {
  const plugin = await loadPlugin()
  const syntheticToken = 'fixture-only-legion-secret'
  const syntheticBearer = 'fixture-only-other-bearer'
  const memoryClient = new LegionClient(isolatedConfig, { [isolatedConfig.tokenEnv]: syntheticToken })
  t.after(() => memoryClient.dispose())
  t.mock.method(memoryClient.store, 'get', async (sessionId, id) => {
    assert.equal(sessionId, 'error-agent')
    assert.equal(id, taskRef)
    return { id, team_id: 'synthetic-team' }
  })
  const realCancel = LegionClient.prototype.cancel
  t.mock.method(LegionClient.prototype, 'cancel', function (args, execution) {
    return realCancel.call(memoryClient, args, execution)
  })
  const { ctx, install } = await host(t, { commands: true })
  // An in-memory Response only: no HTTP fixture, real credentials or state writes.
  globalThis.fetch.mock.mockImplementation(async () => Response.json({
    reason: `Refused ${syntheticToken}; Authorization: Bearer ${syntheticBearer}`,
  }, { status: 403 }))
  await install(plugin, isolatedConfig)
  await new Promise(resolve => setImmediate(resolve))
  const agent = { id: 'error-agent', ctx }
  const signal = new AbortController().signal
  const toolResult = await ctx.tools.execute({
    name: 'legion_cancel', arguments: { task_ref: taskRef }, callId: 'safe-http-error', agent, signal,
  })
  assert.equal(toolResult.isError, true)
  const commandResult = await ctx.commands.find(agent, 'legion').handler({
    agent, signal, rawInput: `cancel ${taskRef}`, attachments: [], commandId: 'safe-command-error',
  })
  assert.equal(commandResult.kind, 'error')
  for (const text of [JSON.stringify(toolResult), JSON.stringify(commandResult)]) {
    assert.match(text, /403/)
    assert.match(text, /\[redacted\]/)
    assert.ok(!text.includes(syntheticToken))
    assert.ok(!text.includes(syntheticBearer))
  }
  assert.equal(globalThis.fetch.mock.callCount(), 2)
})

test('malformed operator binding is a safe error in the native tool and command paths', async t => {
  const plugin = await loadPlugin()
  const stateDirectory = await mkdtemp(join(tmpdir(), 'legion-native-binding-'))
  t.after(() => rm(stateDirectory, { recursive: true, force: true }))
  const projectBindingFile = join(stateDirectory, 'malformed.json')
  await writeFile(projectBindingFile, 'synthetic-private-fragment-not-valid-json', { mode: 0o600 })
  const realSubmit = LegionClient.prototype.submit
  const client = new LegionClient({ ...isolatedConfig, stateDirectory, projectBindingFile },
    { [isolatedConfig.tokenEnv]: 'synthetic-configured-token' })
  t.after(() => client.dispose())
  t.mock.method(LegionClient.prototype, 'submit', function (args, execution) {
    return realSubmit.call(client, args, execution)
  })
  const { ctx, install } = await host(t, { commands: true })
  await install(plugin, isolatedConfig)
  await new Promise(resolve => setImmediate(resolve))
  const agent = { id: 'safe-binding-agent', ctx }
  const signal = new AbortController().signal
  const tool = await ctx.tools.execute({
    name: 'legion_submit', arguments: { key: 'binding-test', query: 'Synthetic test' },
    callId: 'binding-tool', agent, signal,
  })
  const command = await ctx.commands.find(agent, 'legion').handler({
    agent, signal, rawInput: 'start binding-test Synthetic test', attachments: [],
    commandId: 'binding-command',
  })
  assert.equal(tool.isError, true)
  assert.equal(command.kind, 'error')
  for (const result of [tool, command]) {
    assert.match(JSON.stringify(result), /Project binding file is not valid JSON/)
    assert.doesNotMatch(JSON.stringify(result), /synthetic-private|Unexpected token/)
  }
  assert.equal(globalThis.fetch.mock.callCount(), 0)
})

const configuredTeam = {
  name: '我的战队', goal: '让我的 DSH 和战队在真实工作中持续变强',
  collaboration: 'auto', leader: '', instructions: '', evolutionEnabled: true, cadence: 'weekly',
}

test('team wiring: Config normalizes only explicitly configured teams and publishes a callback-free nested form', async () => {
  const { Config } = await loadPlugin()
  assert.ok(!Object.hasOwn(Config({}).get(), 'teamProfile'))
  assert.deepEqual(Config({ teamProfile: {} }).get().teamProfile, configuredTeam)
  const input = { teamProfile: { name: ' \u3000共同工作队 ', goal: ' 共同理解当前需求 ', evolutionEnabled: false } }
  const expected = { ...configuredTeam, name: '共同工作队', goal: '共同理解当前需求', evolutionEnabled: false }
  assert.deepEqual(Config(input).get().teamProfile, expected)
  assert.deepEqual(Config['~standard'].validate(input).value.get().teamProfile, expected)
  assert.equal(Config({ teamProfile: { name: ` ${'名'.repeat(80)} ` } }).get().teamProfile.name.length, 80)
  const serialized = JSON.parse(JSON.stringify(Config))
  assert.ok(Object.values(serialized.refs).every(node => node.type !== 'transform'))
  for (const schema of [new Schema(serialized), new Schema(new Schema(serialized).toJSON())]) {
    assert.equal(schema.dict.teamProfile.type, 'object')
    assert.deepEqual(Object.keys(schema.dict.teamProfile.dict), Object.keys(configuredTeam))
    assert.ok(!Object.hasOwn(schema({}).get(), 'teamProfile'), 'Rendering an old configuration must not opt it in')
    assert.deepEqual(schema({ teamProfile: {} }).get().teamProfile, configuredTeam)
    assert.throws(() => schema({ teamProfile: { evolutionEnabled: 'true' } }))
    assert.throws(() => schema({ teamProfile: { cadence: 'hourly' } }))
  }
})

test('team wiring: raw configuration rejects invalid profiles and private extras before Schemastery can erase them', async () => {
  const { Config } = await loadPlugin()
  const secret = 'synthetic-private-team-config'
  for (const teamProfile of [
    null, [], secret, { goal: null }, { evolutionEnabled: secret },
    { cadence: secret }, { collaboration: secret }, { [secret]: secret },
    { authority: secret }, { name: '\ud800' }, { instructions: secret.repeat(100) },
    { collaboration: 'leader', leader: ' ' }, { collaboration: 'custom', instructions: ' ' },
    Object.create({ goal: secret }),
  ]) {
    assert.throws(() => Config({ teamProfile }), error => {
      assert.match(error.message, /teamProfile/)
      assert.ok(!error.message.includes(secret))
      return true
    })
    // Like the existing raw-key boundary, callback errors are thrown by the
    // installed Schemastery standard adapter; native ConfigEditor catches them.
    assert.throws(() => Config['~standard'].validate({ teamProfile }), error => {
      assert.match(error.message, /teamProfile/)
      assert.ok(!error.message.includes(secret))
      return true
    })
  }
  for (const extra of [{ token: secret }, { lifecycleSignal: secret }, { [secret]: secret }]) {
    assert.throws(() => Config({ teamProfile: {}, ...extra }), error => {
      assert.ok(!error.message.includes(secret))
      return true
    })
  }
})

test('team wiring: native submit uses saved configuration while model arguments cannot supply a team or authority', async t => {
  const plugin = await loadPlugin()
  const calls = recordClient(t)
  const { ctx, install } = await host(t)
  await install(plugin, { ...isolatedConfig, teamProfile: { goal: '用户保存的目标' }, candidateHarnesses: ['member-a'] })
  const result = await ctx.tools.execute({
    callId: 'team-configured-submit', name: 'legion_submit',
    arguments: { key: 'original-key', query: 'Original question', teamProfile: { goal: 'INJECTED_TEAM' }, authority: 'INJECTED_AUTHORITY' },
    agent: { id: 'team-submit-session', ctx }, signal: new AbortController().signal,
  })
  assert.equal(result.isError, false)
  assert.deepEqual(calls[0].args[0], { key: 'original-key', query: 'Original question' })
  assert.deepEqual(calls[0].client.teamProfile, { ...configuredTeam, goal: '用户保存的目标' })
  assert.deepEqual(calls[0].client.candidateHarnesses, ['member-a'])
  assert.ok(ctx.tools.schemas().every(tool => !Object.hasOwn(tool.parameters.properties, 'teamProfile')))
})

// Provider boundary double; records use the installed official 0.2.1-rc.2 API.
// Native persistence/delivery is separately verified by the team-flow author.
function wiringSchedule() {
  const rows = new Map()
  const creates = []
  let sequence = 0
  return {
    rows, creates,
    async catalog() { return structuredClone([...rows.values()]) },
    async create(sessionId, request, signal) {
      signal.throwIfAborted()
      creates.push({ sessionId, request: structuredClone(request), signal })
      const id = `wiring-schedule-${++sequence}`
      const record = request.every_seconds === undefined
        ? createAfterScheduleRecord(id, request.prompt, request.after_seconds, Date.now(), request.title)
        : createEveryScheduleRecord(id, request.prompt, request.every_seconds, Date.now(), request.title)
      rows.set(id, { ...record, sessionId, status: 'active' })
      return structuredClone(record)
    },
    async delete({ sessionId, id }, signal) {
      signal.throwIfAborted()
      if (rows.get(id)?.sessionId !== sessionId) return { id, deleted: false, code: 'schedule_not_found' }
      rows.delete(id)
      return { id, deleted: true }
    },
  }
}

test('team wiring: optional schedule is resolved at call time and native panel actions share the current Host session', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t, { commands: true })
  const fiber = await install(plugin, {
    ...isolatedConfig, candidateHarnesses: ['member-a'],
    teamProfile: { collaboration: 'leader', leader: 'member-a', goal: '一起改善当前真实工作' },
  })
  const events = []
  const agent = { id: 'team-wiring-session', ctx,
    session: { id: 'NOT_THE_OWNER', append(type, data) { events.push({ type, data }); return events.length } } }
  const caller = new AbortController()
  const panel = async (action, args) => (await ctx.commands.execute(agent,
    `/legion-panel ${JSON.stringify({ action, ...(args ? { args } : {}) })}`, [], caller.signal)).result
  const missing = JSON.parse((await panel('flow_status')).text)
  assert.equal(missing.ok, true)
  assert.equal(missing.value.code, 'service_missing')
  assert.equal(missing.value.enabled, null)
  assert.deepEqual(ctx.tools.schemas().map(tool => tool.name).sort(), names, 'Missing schedule must not block existing tools')
  const schedule = wiringSchedule()
  const provider = await install({ name: 'wiring-schedule-provider', apply(scope) { scope.provide('schedule', schedule) } })
  assert.equal(schedule.creates.length, 0, 'Neither plugin activation nor service arrival may dispatch')
  const started = JSON.parse((await panel('flow_start')).text)
  assert.equal(started.ok, true)
  assert.equal(started.value.partial, false)
  assert.equal(started.value.configured, true)
  assert.equal(started.value.schedules.length, 2)
  assert.equal(started.value.execution, 'not_verified')
  assert.deepEqual(schedule.creates.map(call => call.sessionId), [agent.id, agent.id])
  assert.equal(schedule.creates[0].request.after_seconds, 1)
  assert.equal(schedule.creates[1].request.every_seconds, 604800)
  assert.ok(schedule.creates.every(call => call.request.prompt.includes('一起改善当前真实工作')
    && call.request.prompt.includes('member-a')))
  assert.ok(schedule.creates.every(call => call.signal !== caller.signal && !call.signal.aborted))
  await panel('flow_start')
  assert.equal(schedule.creates.length, 2)
  const firstRun = JSON.parse((await panel('flow_run', { key: 'explicit-again' })).text)
  assert.equal(firstRun.ok, true)
  await panel('flow_run', { key: 'explicit-again' })
  assert.equal(schedule.creates.length, 3)
  const paused = JSON.parse((await panel('flow_pause')).text)
  assert.equal(paused.ok, true)
  assert.ok(!paused.value.schedules.some(row => row.role === 'recurring' && row.status === 'active'))
  await provider.dispose()
  assert.equal(JSON.parse((await panel('flow_status')).text).value.code, 'service_missing')
  await install({ name: 'wiring-schedule-replacement', apply(scope) { scope.provide('schedule', schedule) } })
  assert.equal(JSON.parse((await panel('flow_status')).text).value.catalogVerified, true)
  assert.equal(schedule.creates.length, 3, 'Replacing a provider must not trigger first work again')
  assert.ok(events.filter(event => event.type === 'command/run').every(event => !Object.hasOwn(event.data, 'args')))
  await fiber.dispose()
  assert.equal(caller.signal.aborted, false)
})

test('team wiring: manual cadence and disabled evolution reach the flow without requiring Legion credentials', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t, { commands: true })
  const schedule = wiringSchedule()
  await install({ name: 'manual-schedule-provider', apply(scope) { scope.provide('schedule', schedule) } })
  assert.equal(process.env[isolatedConfig.tokenEnv], undefined)
  await install(plugin, { ...isolatedConfig, teamProfile: { evolutionEnabled: false, cadence: 'manual' } })
  const agent = { id: 'manual-team-session', ctx, session: { append() { return 1 } } }
  const result = await ctx.commands.execute(agent, '/legion-panel {"action":"flow_start"}', [], new AbortController().signal)
  const body = JSON.parse(result.result.text)
  assert.equal(body.ok, true)
  assert.equal(body.value.partial, false)
  assert.equal(body.value.schedules.length, 1)
  assert.match(schedule.creates[0].request.prompt, /用户已关闭自主改进/)
  assert.equal(schedule.creates[0].request.every_seconds, undefined)
})

test('team wiring: unload aborts pending flow reads and stale commands without cancelling the caller or remote work', async t => {
  const plugin = await loadPlugin()
  const { ctx, install } = await host(t, { commands: true })
  const entered = Promise.withResolvers()
  const release = Promise.withResolvers()
  t.after(() => release.resolve([]))
  await install({ name: 'held-schedule-provider', apply(scope) {
    scope.provide('schedule', { async catalog() { entered.resolve(); return release.promise } })
  } })
  const fiber = await install(plugin, { ...isolatedConfig, teamProfile: {} })
  const caller = new AbortController()
  const agent = { id: 'held-flow-session', ctx }
  const handler = ctx.commands.find(agent, 'legion-panel').handler
  const input = { agent, signal: caller.signal, rawInput: '{"action":"flow_status"}', attachments: [] }
  const pending = handler(input)
  await Promise.race([entered.promise, pending.then(() => assert.fail('Flow read must enter the native catalog'))])
  await fiber.dispose()
  const result = JSON.parse((await pending).text)
  assert.equal(result.ok, true)
  assert.equal(result.value.cancelled, true)
  assert.equal(result.value.execution, 'not_verified')
  assert.equal(caller.signal.aborted, false)
  release.resolve([])
  const stale = JSON.parse((await handler(input)).text)
  assert.equal(stale.ok, false)
  assert.match(stale.error, /取消/)
})
