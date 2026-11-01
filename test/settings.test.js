import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import Schema from '@deepseek-ai/schemastery'
import { Context } from '@deepseek-ai/cordis'
import { Loader } from '@deepseek-ai/cordis-plugin-loader'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime } from '@deepseek-ai/dsh-tools'
import { CommandRuntime } from '@deepseek-ai/dsh-commands'
import { initProfile, mountRootInclude, readProfilePatches } from '@deepseek-ai/dsh-app-boot'
import { ConfigEditor } from '@deepseek-ai/dsh-config-editor'
import { SettingsForms } from '@deepseek-ai/dsh-settings'
import { SettingsController } from '@deepseek-ai/dsh-api-settings-controller'
import { LegionClient } from '../lib/client.js'

const repo = dirname(dirname(fileURLToPath(import.meta.url)))
const tick = () => new Promise(resolve => setImmediate(resolve))

// Short-lived protocol fixtures only: no model, external network, or real Host.
async function service(t, label) {
  const requests = []
  let held
  const server = createServer(async (request, response) => {
    let raw = ''
    for await (const chunk of request) raw += chunk
    const body = raw ? JSON.parse(raw) : {}
    const path = new URL(request.url, 'http://fixture.invalid').pathname
    requests.push({ path, authorization: request.headers.authorization, body })
    if (held && path === '/api/legion/harnesses') {
      const waiting = held
      held = undefined
      response.on('close', waiting.closed)
      waiting.started()
      return
    }
    if (path === '/api/legion/dispatch') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream' })
      response.end(`event: meta\ndata: ${JSON.stringify({ team_id: body.team_id, sid: body.session_id })}\n\n`)
      return
    }
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ label, installed: false }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`, requests,
    hold() {
      const started = Promise.withResolvers()
      const closed = Promise.withResolvers()
      held = { started: started.resolve, closed: closed.resolve }
      return { started: started.promise, closed: closed.promise }
    },
  }
}

async function setup(t) {
  const root = await mkdtemp(join(tmpdir(), 'legion-native-settings-'))
  const savedEnv = {}
  for (const [key, value] of Object.entries({
    HOME: join(root, 'home'), DSH_HOME: join(root, 'dsh'),
    MEYO_LEGION_DIR: join(root, 'meyo'), LEGION_HOME_DIR: join(root, 'legion'),
    LEGION_PERSONAL_TOKEN: 'fixture-env-never-used',
  })) {
    savedEnv[key] = process.env[key]
    process.env[key] = value
  }
  const hosts = []
  t.after(async () => {
    for (const host of hosts.toReversed()) await host.close()
    for (const [key, value] of Object.entries(savedEnv)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    await rm(root, { recursive: true, force: true })
  })
  const a = await service(t, 'A')
  const b = await service(t, 'B')
  const tokenA = join(root, 'token-a')
  const tokenB = join(root, 'token-b')
  await writeFile(tokenA, 'fixture-identity-A\n', { mode: 0o600 })
  await writeFile(tokenB, 'fixture-identity-B\n', { mode: 0o600 })
  const profileDir = join(root, 'profile')
  initProfile(profileDir, [])
  const configPath = join(root, 'cordis.yml')
  await writeFile(configPath, '[]\n')
  const profile = {
    name: 'fixture', dir: profileDir, patchPath: join(profileDir, 'cordis.patch.yml'),
    installAnchor: join(repo, 'node_modules/@deepseek-ai/dsh/package.json'),
    cwd: root, home: process.env.DSH_HOME, startedBundles: [], overlays: [],
    telemetryDisabledEnv: '1',
  }
  await mkdir(profile.home, { recursive: true })
  // Initial synthetic fixture composition, not an edit to a user's profile.
  // Every configuration change under test goes through the official API.
  await writeFile(profile.patchPath, JSON.stringify([{ insert: [{
    id: 'legion', name: pathToFileURL(join(repo, 'index.js')).href,
    config: { baseUrl: a.baseUrl, tokenFile: tokenA, stateDirectory: join(root, 'state'),
      requestTimeoutMs: 2000 },
  }] }]))
  async function boot() {
    const ctx = new Context()
    const fibers = []
    ctx.provide('profileContext', profile)
    for (const [plugin, config] of [
      [SystemPrompt, { includeHarnessIdentity: false }], [ToolRuntime, { mode: 'native' }],
      [CommandRuntime, {}], [Loader, {}], [ConfigEditor, {}],
      [SettingsForms, {}], [SettingsController, {}],
    ]) {
      const fiber = ctx.plugin(plugin, config)
      fibers.push(fiber)
      await fiber
    }
    const include = await mountRootInclude(ctx, configPath, readProfilePatches('fixture', profile))
    await ctx.loader.await()
    const entry = ctx.configEditor.entries().find(row => row.options.id === 'legion')
    assert.equal(entry?.fiber?.state, 2, 'Official Loader must activate the fixture plugin')
    const agent = { id: 'settings-fixture-session', ctx }
    const execution = { agent, signal: new AbortController().signal }
    let closed = false
    const host = {
      ctx, entry, agent,
      // Same controller and arguments as settings/{describe,update,mutate};
      // only the HTTP/WebSocket envelope is omitted in this component test.
      describe: () => ctx.settingsController.describe().namespaces.find(row => row.ns === 'legion'),
      update: (patch, revision) => ctx.settingsController.update('legion', patch, revision),
      mutate: (ops, revision) => ctx.settingsController.mutate('legion', ops, revision),
      tool: (name = 'runtime', args = {}) => ctx.tools.execute({
        callId: `fixture-${name}`, name: `legion_${name}`, arguments: args, ...execution,
      }),
      command: () => ctx.commands.find(agent, 'legion').handler({
        ...execution, rawInput: 'runtime members', attachments: [], commandId: 'settings-command',
      }),
      async close() {
        if (closed) return
        closed = true
        await include?.fiber?.dispose()
        for (const fiber of fibers.toReversed()) await fiber.dispose()
      },
    }
    hosts.push(host)
    await tick()
    return host
  }
  return { root, a, b, tokenA, tokenB, profile, boot, host: await boot() }
}

test('official settings exposes an auto-generated, browser-rehydratable Legion form', { timeout: 10000 }, async t => {
  const f = await setup(t)
  const view = f.host.describe()
  assert.ok(view, 'Legion must be visible in settings/describe, not just declared in a profile')
  assert.equal(view.ns, 'legion')
  assert.equal(view.autoGenerate, true)
  assert.equal(view.applies, 'live')
  assert.equal(view.value.baseUrl, f.a.baseUrl)
  assert.equal(view.value.tokenFile, f.tokenA)
  assert.ok(Number.isSafeInteger(view.revision))
  // The native ConfigForm uses precisely this serialization/validation step.
  // A callback depending on a host-only import or closure makes the page unready.
  const schema = new Schema(view.schema)
  assert.equal(schema.type, 'object', 'The native page receives named fields, not a host-only transform')
  assert.ok(Object.hasOwn(schema.dict, 'baseUrl'))
  assert.ok(Object.hasOwn(schema.dict, 'tokenFile'))
  assert.ok(!Object.hasOwn(schema.dict, 'token'))
  assert.ok(Object.values(view.schema.refs).every(node => node.type !== 'transform'))
  assert.doesNotThrow(() => schema(view.value))
  assert.doesNotThrow(() => new Schema(schema.toJSON())(view.value))
  assert.ok(!JSON.stringify(view).includes('fixture-identity-A'))
  t.diagnostic(JSON.stringify({
    ns: view.ns, revision: view.revision, applies: view.applies, autoGenerate: view.autoGenerate,
    schema_root: schema.type,
    fields: Object.fromEntries(Object.entries(schema.dict).map(([name, node]) =>
      [name, { type: node.type, default: node.meta.default }])),
  }))
})

test('official settings update/mutate persists and rebinds native tools and commands; identical writes are idempotent', { timeout: 10000 }, async t => {
  const f = await setup(t)
  const disposed = t.mock.method(LegionClient.prototype, 'dispose', LegionClient.prototype.dispose)
  assert.equal((await f.host.tool()).value.runtime.label, 'A')
  const old = f.host.describe()
  const changed = await f.host.update({ baseUrl: f.b.baseUrl, tokenFile: f.tokenB }, old?.revision)
  assert.equal(changed.value.baseUrl, f.b.baseUrl)
  assert.equal(changed.value.tokenFile, f.tokenB)
  assert.ok(changed.revision > old.revision)
  assert.equal((await f.host.tool()).value.runtime.label, 'B')
  assert.equal((await f.host.command()).kind, 'success')
  assert.ok(f.b.requests.every(row => row.authorization === 'Bearer fixture-identity-B'))
  assert.ok(disposed.mock.callCount() >= 1, 'Existing clients must be disposed, not mutated in place')
  const count = disposed.mock.callCount()
  const same = await f.host.update({ baseUrl: f.b.baseUrl, tokenFile: f.tokenB }, changed.revision)
  assert.equal(disposed.mock.callCount(), count)
  assert.equal(same.revision, changed.revision)
  await assert.rejects(f.host.update({ baseUrl: f.a.baseUrl }, old.revision), { code: 'settings/conflict' })
  const restored = await f.host.mutate([
    { op: 'set', path: ['baseUrl'], value: f.a.baseUrl },
    { op: 'set', path: ['tokenFile'], value: f.tokenA },
  ], same.revision)
  assert.equal(restored.value.baseUrl, f.a.baseUrl)
  assert.equal((await f.host.tool()).value.runtime.label, 'A')
  await f.host.close()
  const rebooted = await f.boot()
  assert.equal(rebooted.describe().value.tokenFile, f.tokenA)
  assert.equal((await rebooted.tool()).value.runtime.label, 'A')
  const persisted = await readFile(f.profile.patchPath, 'utf8')
  assert.ok(!persisted.includes('fixture-identity-A'))
  assert.ok(!persisted.includes('fixture-identity-B'))
  t.diagnostic(JSON.stringify({
    case: 'official-settings-update', before_revision: old.revision,
    updated_revision: changed.revision, identical_write_revision: same.revision,
    restored_revision: restored.revision, rebooted_revision: rebooted.describe().revision,
  }))
})

test('configuration reload aborts in-flight HTTP and old command closures without cancelling remote jobs', { timeout: 10000 }, async t => {
  const f = await setup(t)
  const started = await f.host.tool('submit', { key: 'original', query: 'Synthetic protocol task' })
  assert.equal(started.value.submission, 'accepted')
  const oldCommand = f.host.ctx.commands.find(f.host.agent, 'legion').handler
  const held = f.a.hold()
  const inFlight = f.host.tool()
  await held.started
  await f.host.update({ baseUrl: f.b.baseUrl }, f.host.describe()?.revision)
  const interrupted = await inFlight
  assert.equal(interrupted.isError, true)
  await held.closed
  assert.equal((await f.host.tool()).value.runtime.label, 'B')
  const count = f.b.requests.length
  const oldReference = await f.host.tool('status', { task_ref: started.value.task_ref })
  assert.equal(oldReference.isError, true)
  assert.equal(f.b.requests.length, count, 'An old reference must be refused before contacting the new service')
  assert.deepEqual((await f.host.tool('list')).value.tasks, [])
  const retained = await oldCommand({
    agent: f.host.agent, signal: new AbortController().signal,
    rawInput: 'runtime members', attachments: [], commandId: 'old-closure',
  })
  assert.equal(retained.kind, 'error')
  assert.equal(f.b.requests.length, count)
  assert.ok(![...f.a.requests, ...f.b.requests].some(row => row.path.endsWith('/cancel')))
  assert.equal(f.host.ctx.tools.schemas().filter(row => row.name === 'legion_runtime').length, 1)
  assert.equal(f.host.ctx.commands.list(f.host.agent).filter(row => row.name === 'legion').length, 1)
})

test('changing only tokenFile separates persistent identities; switching back recovers only the original index', { timeout: 10000 }, async t => {
  const f = await setup(t)
  const first = await f.host.tool('submit', { key: 'same-key', query: 'Synthetic task A' })
  await f.host.update({ tokenFile: f.tokenB }, f.host.describe()?.revision)
  assert.deepEqual((await f.host.tool('list')).value.tasks, [])
  const count = f.a.requests.length
  assert.equal((await f.host.tool('status', { task_ref: first.value.task_ref })).isError, true)
  assert.equal(f.a.requests.length, count)
  const second = await f.host.tool('submit', { key: 'same-key', query: 'Synthetic task B' })
  assert.equal(second.value.submission, 'accepted')
  assert.notEqual(second.value.task_ref, first.value.task_ref)
  assert.equal(f.a.requests.at(-1).authorization, 'Bearer fixture-identity-B')
  await f.host.update({ tokenFile: f.tokenA }, f.host.describe().revision)
  assert.deepEqual((await f.host.tool('list')).value.tasks.map(row => row.task_ref), [first.value.task_ref])
  await f.host.close()
  const rebooted = await f.boot()
  assert.deepEqual((await rebooted.tool('list')).value.tasks.map(row => row.task_ref), [first.value.task_ref])
})

test('official writes reject unknown fields, inline tokens, unsafe URLs and null paths without altering configuration', { timeout: 10000 }, async t => {
  const f = await setup(t)
  const before = await readFile(f.profile.patchPath, 'utf8')
  for (const patch of [
    { token: 'fixture-inline-secret' }, { lifecycleSignal: {} }, { misspelt: true },
    { tokenFile: null }, { tokenFile: 'relative/token' }, { tokenFile: '/tmp/../secret' },
    { baseUrl: 'https://name:fixture-inline-secret@example.test' }, { baseUrl: 'http://remote.invalid' },
  ]) {
    await assert.rejects(f.host.update(patch, f.host.describe()?.revision), error => {
      assert.equal(error.code, 'settings/rejected')
      assert.ok(!error.message.includes('fixture-inline-secret'))
      return true
    })
    assert.equal(await readFile(f.profile.patchPath, 'utf8'), before)
  }
  assert.equal((await f.host.tool()).value.runtime.label, 'A')
})

test('a configured unsafe token file fails closed at use, never using an old client or the environment fallback', { timeout: 10000 }, async t => {
  const f = await setup(t)
  assert.equal((await f.host.tool()).isError, false)
  await chmod(f.tokenB, 0o644)
  await f.host.update({ tokenFile: f.tokenB }, f.host.describe()?.revision)
  const count = f.a.requests.length
  const result = await f.host.tool()
  assert.equal(result.isError, true)
  assert.equal(f.a.requests.length, count)
  assert.ok(!JSON.stringify(result).includes('fixture-env-never-used'))
  assert.ok(!JSON.stringify(result).includes(f.tokenB))
  await chmod(f.tokenB, 0o600)
  assert.equal((await f.host.tool()).isError, false)
  assert.equal(f.a.requests.at(-1).authorization, 'Bearer fixture-identity-B')
})

test('team wiring: official settings exposes a twice-rehydratable nested team form without opting in old configurations',
  { timeout: 10000 }, async t => {
    const f = await setup(t)
    const view = f.host.describe()
    assert.equal(view.value.teamProfile, undefined)
    assert.ok(Object.values(view.schema.refs).every(node => node.type !== 'transform'))
    const fields = ['name', 'goal', 'collaboration', 'leader', 'instructions', 'evolutionEnabled', 'cadence']
    for (const schema of [new Schema(view.schema), new Schema(new Schema(view.schema).toJSON())]) {
      const team = schema.dict.teamProfile
      assert.ok(team, 'The official Settings form must expose teamProfile')
      assert.equal(team.type, 'object')
      assert.deepEqual(Object.keys(team.dict), fields)
      assert.equal(schema(view.value).teamProfile, undefined)
      const defaults = schema({ ...view.value, teamProfile: {} }).teamProfile
      assert.equal(defaults.evolutionEnabled, true)
      assert.equal(defaults.cadence, 'weekly')
      assert.equal(defaults.collaboration, 'auto')
      assert.equal(defaults.name, '我的战队')
      assert.equal(defaults.goal, '让我的 DSH 和战队在真实工作中持续变强')
      assert.throws(() => schema({ ...view.value, teamProfile: { cadence: 'hourly' } }))
      assert.throws(() => schema({ ...view.value, teamProfile: { evolutionEnabled: 'false' } }))
    }
  })

test('team wiring: official settings atomically saves profile and candidates, reloads submit wiring and preserves recovery on reboot',
  { timeout: 10000 }, async t => {
    const f = await setup(t)
    const args = { key: 'before-configured-team', query: 'Original current task', context: 'Explicit original context' }
    const original = await f.host.tool('submit', args)
    assert.equal(original.value.submission, 'accepted')
    let catalogReads = 0
    f.host.ctx.provide('schedule', {
      async catalog() { catalogReads++; return [] },
      async create() { assert.fail('Saving configuration alone must not schedule work') },
    })
    const teamProfile = {
      name: '设置测试战队', goal: '用户保存的真实目标', collaboration: 'leader',
      leader: 'member-a', instructions: '一起核实当前问题', evolutionEnabled: false, cadence: 'manual',
    }
    const changed = await f.host.mutate([
      { op: 'set', path: ['candidateHarnesses'], value: ['member-b', 'member-a'] },
      { op: 'set', path: ['teamProfile'], value: teamProfile },
    ], f.host.describe().revision)
    assert.deepEqual(changed.value.teamProfile, teamProfile)
    assert.deepEqual(changed.value.candidateHarnesses, ['member-b', 'member-a'])
    assert.equal(catalogReads, 0)
    const count = f.a.requests.length
    const reusedOldInput = await f.host.tool('submit', args)
    assert.equal(reusedOldInput.isError, true)
    assert.equal(f.a.requests.length, count, 'A changed profile cannot reuse old input or silently redispatch')
    const followup = await f.host.tool('submit', {
      ...args, key: 'configured-team-followup', previous_task_ref: original.value.task_ref,
    })
    assert.equal(followup.value.submission, 'accepted')
    assert.equal(followup.value.session_id, original.value.session_id)
    const request = f.a.requests.at(-1).body
    assert.ok(request.question.startsWith(args.query))
    assert.ok(request.question.includes(args.context))
    assert.ok(request.question.includes(teamProfile.goal))
    assert.ok(request.question.includes(teamProfile.instructions))
    assert.deepEqual(request.plan, { team_policy: { candidate_harnesses: ['member-b', 'member-a'] } })
    assert.equal(request.mode, 'auto')
    assert.equal(Object.hasOwn(request, 'teamProfile'), false)
    assert.equal(catalogReads, 0)
    const same = await f.host.update({ teamProfile }, changed.revision)
    assert.equal(same.revision, changed.revision)
    await f.host.close()
    const rebooted = await f.boot()
    assert.deepEqual(rebooted.describe().value.teamProfile, teamProfile)
    assert.deepEqual(rebooted.describe().value.candidateHarnesses, ['member-b', 'member-a'])
    const beforeReplay = f.a.requests.length
    const replay = await rebooted.tool('submit', {
      ...args, key: 'configured-team-followup', previous_task_ref: original.value.task_ref,
    })
    assert.equal(replay.value.reused, true)
    assert.equal(replay.value.task_ref, followup.value.task_ref)
    assert.equal(f.a.requests.length, beforeReplay)
    const persisted = await readFile(f.profile.patchPath, 'utf8')
    assert.ok(persisted.includes('用户保存的真实目标'))
    assert.ok(!persisted.includes('fixture-identity-A'))
  })

test('team wiring: official settings rejects private or malformed team configuration without partially saving candidates',
  { timeout: 10000 }, async t => {
    const f = await setup(t)
    const before = await readFile(f.profile.patchPath, 'utf8')
    const secret = 'PRIVATE_TEAM_SETTINGS_INPUT'
    for (const teamProfile of [
      null, [], { [secret]: secret }, { authority: secret }, { goal: null },
      { evolutionEnabled: secret }, { cadence: secret }, { collaboration: secret },
      { name: '\ud800' }, { collaboration: 'custom', instructions: '' },
      { collaboration: 'leader', leader: '' },
    ]) {
      await assert.rejects(f.host.mutate([
        { op: 'set', path: ['candidateHarnesses'], value: ['member-a'] },
        { op: 'set', path: ['teamProfile'], value: teamProfile },
      ], f.host.describe().revision), error => {
        assert.equal(error.code, 'settings/rejected')
        assert.ok(!error.message.includes(secret))
        return true
      })
      assert.equal(await readFile(f.profile.patchPath, 'utf8'), before)
      assert.equal(f.host.describe().value.teamProfile, undefined)
      assert.equal(f.host.describe().value.candidateHarnesses, undefined)
    }
    assert.equal(f.a.requests.length, 0)
  })
