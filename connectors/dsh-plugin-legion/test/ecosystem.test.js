import test from 'node:test'
import assert from 'node:assert/strict'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime, defineTool } from '@deepseek-ai/dsh-tools'
import { createScope } from '@deepseek-ai/dsh-scope'
import { GitHubSources } from '../lib/sources.js'

async function moduleUnderTest() {
  let module
  try { module = await import('../lib/ecosystem.js') }
  catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error }
  assert.equal(typeof module?.callNative, 'function', 'native ecosystem implementation is required')
  return module
}

async function runtime(t) {
  const ctx = new Context()
  const prompt = ctx.plugin(SystemPrompt, { includeHarnessIdentity: false })
  await prompt
  const tools = ctx.plugin(ToolRuntime, { mode: 'native' })
  await tools
  const agent = { id: 'ecosystem-test' }
  let scope
  const owner = ctx.inject(['tools'], injected => { scope = createScope(injected, agent) })
  await owner
  agent.ctx = scope.ctx
  t.after(async () => { await scope.dispose(); await owner.dispose(); await tools.dispose(); await prompt.dispose() })
  const signal = new AbortController().signal
  return { ctx, agent, signal }
}

const tool = (name, execute, parameters = {}) => defineTool({
  name, description: name, parameters,
  output: {
    schema: { type: 'object', additionalProperties: true },
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
  }, execute,
})

test('nested execution retains scope, parent token, cancellation, contexts and terminal result', async t => {
  const { callNative } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  let enclosing, nested, resultEvent
  ctx.tools.register(tool('child', (_args, exec) => {
    nested = exec
    exec.deferContext({ role: 'user', content: [{ type: 'text', text: 'original child context' }] })
    exec.concludeTurn()
    return { actual: true }
  }))
  ctx.tools.register(tool('outer', async (_args, exec) => {
    enclosing = exec
    return (await callNative(ctx, exec, 'child', {})).value
  }))
  ctx.on('tools/result', (exec, result) => {
    if (exec.name === 'child') resultEvent = { exec, result }
  })
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'root' })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.equal(nested.agent, agent)
  assert.equal(nested.signal, signal)
  assert.equal(nested.parent, enclosing.token)
  assert.equal(nested.rootCallId, 'root')
  assert.notEqual(nested.callId, 'root')
  assert.equal(resultEvent.result.isError, false)
  assert.equal(result.additionalContexts[0].content[0].text, 'original child context')
  assert.equal(result.concludesTurn, true)
})

test('the signal adapter retains original execution method identity without mutating its authority', async t => {
  const { callNative, withExecutionSignal } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const lifecycle = new AbortController()
  let original, adapted
  ctx.tools.register(tool('child', (_args, exec) => {
    exec.deferContext({ role: 'user', content: [{ type: 'text', text: 'Native deferred context' }] })
    exec.concludeTurn()
    return { actual: true }
  }))
  ctx.tools.register(tool('outer', async (_args, exec) => {
    original = exec
    adapted = withExecutionSignal(exec, lifecycle.signal)
    return (await callNative(ctx, adapted, 'child', {})).value
  }))
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'signal-adapter' })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.equal(result.concludesTurn, true)
  assert.equal(result.additionalContexts[0].content[0].text, 'Native deferred context')
  assert.equal(original.signal, signal)
  assert.equal(adapted.agent, original.agent)
  assert.equal(adapted.token, original.token)
  assert.equal(adapted.rootCallId, original.rootCallId)
  assert.equal(adapted.callId, original.callId)
  lifecycle.abort()
  assert.equal(adapted.signal.aborted, true)
  assert.equal(original.signal.aborted, false)
})

test('native visibility and guard denials cannot be bypassed by nested delegation', async t => {
  const { callNative, capabilities } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  let ran = 0
  ctx.tools.register(tool('hidden', () => { ran++; return {} }))
  ctx.tools.register(tool('denied', () => { ran++; return {} }))
  agent.ctx.tools.restrict({ deny: ['hidden'] })
  ctx.tools.guard(exec => exec.name === 'denied' ? 'real native policy denial' : undefined)
  ctx.tools.register(tool('outer', async (_args, exec) => {
    assert.ok(!capabilities(ctx, exec, {}).entries.some(row => row.name === 'hidden'))
    return callNative(ctx, exec, 'denied', {})
  }))
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'guard' })
  assert.equal(result.value.isError, true)
  assert.match(result.value.error.message, /real native policy denial/)
  assert.equal(ran, 0)
  assert.throws(() => capabilities(ctx, { agent, signal }, { names: ['hidden'] }), /visible|可见/)
})

test('discovery uses the native configured-market tool, preserves failures and performs no install', async t => {
  const { discover } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const requests = []
  ctx.tools.register(tool('plugin_market', args => {
    requests.push(args)
    if (args.action === 'list_indexes') return { indexes: [{ url: 'https://catalog.example/index.json' }] }
    return { entries: [{ name: 'fixture-markdown', defaultVersion: { install: { target: 'fixture@1.0.0' } } }],
      total: 1, nextOffset: null }
  }, { action: { type: 'string', required: true }, target: { type: 'string' },
    query: { type: 'string' }, offset: { type: 'integer' }, limit: { type: 'integer' } }))
  const sources = { search: async (args, execution) => {
    assert.equal(execution.signal, signal)
    assert.equal(args.query, 'markdown')
    return { items: [{ repository: 'fixture/markdown' }] }
  } }
  ctx.tools.register(tool('outer', (_args, exec) => discover(ctx, sources,
    { query: 'markdown', source: 'all', limit: 3 }, exec)))
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'discover' })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.deepEqual(requests.map(row => row.action), ['list_indexes', 'list_plugins'])
  assert.equal(result.value.market.entries[0].result.entries[0].defaultVersion.install.target, 'fixture@1.0.0')
  assert.equal(result.value.github.state, 'available')
  assert.equal(result.value.installed.state, 'available')
  ctx.tools.guard(exec => exec.name === 'plugin_market' ? 'market read denied' : undefined)
  const denied = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'discover-denied' })
  assert.equal(denied.value.market.state, 'failed')
  assert.match(denied.value.market.error, /market read denied/)
  assert.equal(denied.value.github.state, 'available', 'One source failure does not erase another result')
})

test('market disabled or not configured is distinct from a successful empty search', async t => {
  const { discover } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const noFetch = { search: () => assert.fail('Not a GitHub search') }
  ctx.tools.register(tool('outer', (_args, exec) => discover(ctx, noFetch, { source: 'market' }, exec)))
  const run = id => ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: id })
  assert.equal((await run('absent')).value.market.state, 'unavailable')
  ctx.tools.register(tool('plugin_market', () => ({ indexes: [] }),
    { action: { type: 'string', required: true } }))
  assert.equal((await run('unconfigured')).value.market.state, 'not_configured')
})

test('discovery retains safe GitHub retry diagnostics without discarding installed capabilities', async t => {
  const { discover } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const sources = new GitHubSources({}, { fetchImpl: async () => new Response('Synthetic untrusted response body', {
    status: 429,
    headers: {
      'retry-after': '120', 'x-ratelimit-remaining': '0',
      'x-ratelimit-reset': '1793149200', 'x-ratelimit-resource': 'search',
    },
  }) })
  ctx.tools.register(tool('outer', (_args, exec) => discover(ctx, sources,
    { query: 'synthetic-project', source: 'all' }, exec)))
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'source-diagnostics' })
  assert.equal(result.isError, false)
  assert.equal(result.value.installed.state, 'available')
  assert.equal(result.value.github.state, 'failed')
  assert.match(result.value.github.error, /HTTP 429/)
  const facts = result.value.github.diagnostics
  assert.equal(facts?.status, 429, 'Partial discovery must retain the source retry facts')
  assert.equal(facts.retry_after_seconds, 120)
  assert.equal(facts.rate_limit.remaining, 0)
  assert.equal(facts.rate_limit.reset_epoch_seconds, 1793149200)
  assert.equal(facts.trust, 'untrusted_source')
  assert.ok(!JSON.stringify(result).includes('Synthetic untrusted response body'))
})

test('bundle writes use native write, stop on denied partial writes, and do not install', async t => {
  const { writeBundle } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const writes = []
  const bundle = { packageName: 'dsh-plugin-fixture', version: '1.0.0', plugins: [{ id: 'render' }],
    files: [{ path: 'index.js', content: 'export function apply() {}', sha256: 'a'.repeat(64) },
      { path: 'package.json', content: '{}', sha256: 'b'.repeat(64) }] }
  ctx.tools.register(tool('write', (args, exec) => {
    assert.equal(exec.agent, agent)
    writes.push(args)
    return {}
  }, { file_path: { type: 'string', required: true }, content: { type: 'string', required: true } }))
  ctx.tools.register(tool('outer', (_args, exec) => writeBundle(ctx, exec, '/tmp/legion-fixture-bundle', bundle)))
  const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'write' })
  assert.equal(result.isError, false, JSON.stringify(result))
  assert.equal(result.value.state, 'written_not_installed')
  assert.equal(result.value.files.length, 2)
  assert.equal(writes[0].file_path, '/tmp/legion-fixture-bundle/index.js')
  ctx.tools.guard(exec => exec.name === 'write' && exec.arguments.file_path.endsWith('package.json')
    ? 'native write denied' : undefined)
  const failed = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'write-partial' })
  assert.equal(failed.isError, true)
  assert.match(failed.content[0].text, /native write denied/)
  assert.match(failed.content[0].text, /index.js/)
  assert.equal(writes.length, 3)
})

for (const [directory, target] of [
  ['/tmp/legion-fixture-bundle', 'file:/tmp/legion-fixture-bundle'],
  ['/tmp/legion advice/生成包', 'file:/tmp/legion advice/生成包'],
]) {
  test(`bundle install advice preserves the file spec without executing it: ${directory}`, async t => {
    const { writeBundle } = await moduleUnderTest()
    const { parseInstallSpec } = await import('@deepseek-ai/dsh-plugin-manager')
    const { ctx, agent, signal } = await runtime(t)
    const writes = []
    let enclosing, installs = 0
    ctx.tools.register(tool('plugin_manager', () => { installs++; return {} }))
    ctx.tools.register(tool('write', (args, exec) => {
      assert.equal(exec.agent, agent)
      assert.equal(exec.signal, signal)
      assert.equal(exec.parent, enclosing.token)
      assert.equal(exec.rootCallId, 'file-advice')
      writes.push(args)
      return {}
    }, { file_path: { type: 'string', required: true }, content: { type: 'string', required: true } }))
    ctx.tools.register(tool('outer', (_args, exec) => {
      enclosing = exec
      return writeBundle(ctx, exec, directory, {
        packageName: '@fixture/install-advice', version: '1.0.0', plugins: [],
        files: [{ path: 'package.json', content: '{}', sha256: 'a'.repeat(64) }],
      })
    }))
    const result = await ctx.tools.execute({ name: 'outer', arguments: {}, agent, signal, callId: 'file-advice' })
    assert.equal(result.isError, false, JSON.stringify(result))
    assert.equal(result.value.directory, directory, 'The native write destination is not an install spec')
    assert.equal(writes[0].file_path, `${directory}/package.json`)
    assert.deepEqual(result.value.install, {
      tool: 'plugin_manager', arguments: { action: 'install_bundle', target, enabled: false },
    })
    assert.equal(parseInstallSpec(result.value.install.arguments.target).path, directory)
    assert.equal(result.value.state, 'written_not_installed')
    assert.equal(result.value.execution_verified, false)
    assert.equal(installs, 0, 'Advice cannot invoke the native installer or consume approval')
  })
}

test('bundle destinations cannot write a profile or escape the output directory', async t => {
  const { writeBundle } = await moduleUnderTest()
  const { ctx, agent, signal } = await runtime(t)
  const bundle = { files: [{ path: '../escape.js', content: '', sha256: '' }] }
  await assert.rejects(writeBundle(ctx, { agent, signal }, '/tmp/output', bundle), /path|路径/)
  await assert.rejects(writeBundle(ctx, { agent, signal }, '/tmp/output', {
    files: [{ path: '/absolute.js', content: '', sha256: '' }],
  }), /path|路径/)
})
