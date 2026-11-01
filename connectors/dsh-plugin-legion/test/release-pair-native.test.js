import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, writeFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { Context } from '@deepseek-ai/cordis'
import { evaluatePluginCompatibility, getDshRuntimeVersion } from '@deepseek-ai/dsh-app-boot'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime } from '@deepseek-ai/dsh-tools'
import { CommandRuntime } from '@deepseek-ai/dsh-commands'
import { fixture } from './fixture.js'

const root = fileURLToPath(new URL('../', import.meta.url))
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const pairFile = process.env.LEGION_RELEASE_PAIR_FILE
const receiptFile = process.env.LEGION_RELEASE_PAIR_RECEIPT

async function readback(pair) {
  for (const side of [pair.core, pair.connector]) {
    assert.equal(hash(await readFile(side.archive.path)), side.archive.sha256, 'The tested archive must still be the paired archive')
  }
  for (const file of pair.connector.files) {
    assert.equal(hash(await readFile(join(root, file.path))), file.sha256, `Paired source changed: ${file.path}`)
  }
}

test('native DSH compatibility and original tool/command download work offline; fixture is not a live-core claim', async t => {
  assert.equal(Boolean(pairFile), Boolean(receiptFile), 'A paired run needs both its descriptor and a new receipt path')
  const pair = pairFile ? JSON.parse(await readFile(pairFile, 'utf8')) : null
  if (pair) await readback(pair)
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
  assert.equal(evaluatePluginCompatibility(manifest), undefined)
  const peersOnly = { ...manifest, engines: { node: manifest.engines.node } }
  assert.equal(evaluatePluginCompatibility(peersOnly), undefined, 'Check official peers too, not just engines.dsh')
  assert.ok(evaluatePluginCompatibility({ ...manifest, engines: { dsh: '999.0.0' } }))
  const require = createRequire(import.meta.url)
  const manager = dirname(require.resolve('@deepseek-ai/dsh-plugin-manager/package.json'))
  const { parseInstallSpec } = await import(pathToFileURL(join(manager, 'lib/types/install-spec.js')))
  if (pair) {
    const source = pair.connector.native.source
    const parsed = parseInstallSpec(`${source.url}#${source.commit}`)
    assert.equal(parsed.kind, 'git', 'Retain the official fixed-Git installation shape')
  }
  const plugin = await import('../index.js')
  const results = []
  for (const options of [{ prefix: '' }, { prefix: '/legion' }, { prefix: '', badArtifact: true }]) {
    const server = await fixture(t, {
      ...options, artifactPath: '/outputs/pair.html', previewArtifact: true,
      artifactBytes: '<p>Offline release-pair fixture, not a model deliverable.</p>\n',
    })
    const state = await mkdtemp(join(tmpdir(), 'legion-pair-native-'))
    const tokenFile = join(state, 'fixture-token')
    await writeFile(tokenFile, 'fixture-token\n', { mode: 0o600 })
    const ctx = new Context()
    const fibers = []
    t.after(async () => {
      for (const fiber of fibers.toReversed()) await fiber.dispose()
      await rm(state, { recursive: true, force: true })
    })
    const install = async (service, config) => {
      const fiber = ctx.plugin(service, config)
      fibers.push(fiber)
      await fiber
    }
    await install(SystemPrompt, { includeHarnessIdentity: false })
    await install(ToolRuntime, { mode: 'native' })
    await install(CommandRuntime)
    await install(plugin, { baseUrl: server.baseUrl, tokenFile, stateDirectory: join(state, 'store'), requestTimeoutMs: 3000 })
    await new Promise(resolve => setImmediate(resolve))
    const agent = { id: 'pair-offline-session', ctx }
    const signal = new AbortController().signal
    let call = 0
    const tool = (name, args) => ctx.tools.execute({ name, arguments: args, callId: `pair-${++call}`, agent, signal })
    const submitted = await tool('legion_submit', { key: 'offline-pair', query: 'Protocol fixture only; no model is running.' })
    assert.equal(submitted.isError, false, JSON.stringify(submitted))
    const { task_ref: taskRef, team_id: teamId } = submitted.value
    server.finish(teamId, [{ name: 'pair.html', url: '/outputs/pair.html', sha256: server.sha256, bytes: server.bytes.length, team_id: teamId }])
    const status = await tool('legion_status', { task_ref: taskRef })
    assert.equal(status.isError, false, JSON.stringify(status))
    const artifactId = status.value.artifacts[0].artifact_id
    const downloaded = await tool('legion_download', { task_ref: taskRef, artifact_id: artifactId })
    const command = ctx.commands.find(agent, 'legion')
    const invocation = { agent, signal, rawInput: `download ${taskRef} ${artifactId}`, attachments: [], commandId: 'pair-command' }
    const commandResult = await command.handler(invocation)
    if (options.badArtifact) {
      assert.equal(downloaded.isError, true)
      assert.equal(commandResult.kind, 'error')
      assert.match(commandResult.text, /integrity|SHA-256/i)
    } else {
      assert.equal(downloaded.isError, false, JSON.stringify(downloaded))
      assert.equal(commandResult.kind, 'success', commandResult.text)
      assert.equal(downloaded.value.integrity, 'sha256_verified')
      assert.equal(downloaded.value.experience_verification, 'not_performed_by_plugin')
      assert.deepEqual(await readFile(downloaded.value.local_path), server.bytes)
      const commandPath = commandResult.text.match(/^本地文件：(.+)$/m)?.[1]
      assert.ok(commandPath, 'Read back the path returned by the original /legion command')
      assert.deepEqual(await readFile(commandPath), server.bytes)
      assert.ok(commandResult.text.includes(server.sha256))
    }
    const denied = await command.handler({ ...invocation, agent: { id: 'another-session', ctx } })
    assert.equal(denied.kind, 'error', 'A release pair must not weaken original-session ownership')
    const requests = server.requests.filter(request => request.path.endsWith('/outputs/pair.html'))
    assert.equal(requests.length, 2, 'Both native paths fetched bytes; wrong-session request did not')
    assert.ok(requests.every(request => request.search === '?download=1'))
    const files = await readdir(join(state, 'store'), { recursive: true })
    assert.ok(!files.some(name => name.endsWith('.part')), 'Failed downloads leave no partial artifact')
    if (options.badArtifact) assert.ok(!files.some(name => name.endsWith('.html')))
    results.push({ prefix: options.prefix, corruption_rejected: Boolean(options.badArtifact), original_download_requests: requests.length, bytes: server.bytes.length, sha256: server.sha256 })
  }
  if (pair) {
    await readback(pair)
    const receipt = {
      format: 'meyo-release-pair-evidence-v1', pair_id: pair.pair_id,
      environment: { node: process.version, dsh: getDshRuntimeVersion() },
      observations: [
        { kind: 'connector_source_readback', status: 'passed', files: pair.connector.files.length },
        { kind: 'native_dsh_compatibility', status: 'passed', scope: 'installed_native_manifest_checker_and_services_no_install' },
        { kind: 'original_entry_download', status: 'passed', scope: 'loopback_protocol_fixture_not_live_core', entrypoints: ['legion_download', '/legion download'], results },
        { kind: 'live_core_integration', status: 'not_verified', reason: 'No core service or member was started; HTTP responses came from the existing protocol fixture.' },
        { kind: 'fresh_native_installation', status: 'not_verified' },
        { kind: 'harness_team_coevolution', status: 'not_verified', reason: 'No models were invoked.' },
      ],
    }
    await writeFile(receiptFile, JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 })
  }
})
