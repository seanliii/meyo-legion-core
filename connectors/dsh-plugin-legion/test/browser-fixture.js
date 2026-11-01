import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createContext, runInContext } from 'node:vm'
import { Context } from '@deepseek-ai/cordis'
import { Loader } from '@deepseek-ai/cordis-plugin-loader'
import { ClientModuleRegistry, bootInjections } from '@deepseek-ai/dsh-client-modules'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolRuntime } from '@deepseek-ai/dsh-tools'

export const packageId = 'dsh-plugin-legion'
export const repo = fileURLToPath(new URL('..', import.meta.url))

/** Real in-memory Cordis Loader + DSH client registry; no server or profile. */
export async function browserFixture(t, { language = 'zh', missingService } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'legion-browser-fixture-'))
  const saved = {}
  for (const [key, value] of Object.entries({
    HOME: join(root, 'home'),
    DSH_HOME: join(root, 'dsh-home'),
    MEYO_LEGION_DIR: join(root, 'meyo'),
    LEGION_HOME_DIR: join(root, 'legion'),
  })) {
    saved[key] = process.env[key]
    process.env[key] = value
    await mkdir(value, { recursive: true })
  }
  const fibers = []
  const contexts = []
  t.after(async () => {
    for (const ctx of contexts.toReversed()) {
      const loader = ctx.get('loader')
      if (!loader) continue
      for (const entry of [...loader.entries()]) loader.remove(entry.id)
      await loader.await()
    }
    for (const fiber of fibers.toReversed()) await fiber.dispose()
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    await rm(root, { recursive: true, force: true })
  })
  t.mock.method(globalThis, 'fetch', async () => assert.fail('Browser notice tests must not use the network'))
  async function install(ctx, plugin, config) {
    const fiber = ctx.plugin(plugin, config)
    fibers.push(fiber)
    await fiber
    return fiber
  }

  const host = new Context()
  contexts.push(host)
  await install(host, SystemPrompt, { includeHarnessIdentity: false })
  await install(host, ToolRuntime, { mode: 'native' })
  await install(host, Loader, { baseUrl: new URL('../', import.meta.url).href })
  const hostOnlyConfig = {
    baseUrl: 'https://host-only-canary.invalid',
    tokenEnv: 'LEGION_BROWSER_FIXTURE_TOKEN_CANARY',
    // These files deliberately do not exist: construction must not read them.
    tokenFile: join(root, 'host-only-token-canary'),
    stateDirectory: join(root, 'host-only-state-canary'),
    projectBindingFile: join(root, 'host-only-project-canary.json'),
    candidateHarnesses: ['host-only-member-canary'],
  }
  const hostEntryId = await host.loader.create({
    name: new URL('../index.js', import.meta.url).href,
    config: hostOnlyConfig,
  })
  await host.loader.await()
  await install(host, ClientModuleRegistry)
  const registry = host.clientModules
  const graph = registry.graph()

  const ctx = new Context()
  contexts.push(ctx)
  const notices = []
  const resolutions = []
  const scopes = new Map()
  const inputs = new Map()
  let activeLanguage = language
  let closedInput = false
  // Only the UI boundary is a test double. Routing and lifecycle use real Cordis.
  const conversation = {
    input: {
      for(scope) {
        resolutions.push(scope)
        if (closedInput) throw new Error('fixture: disposed session input')
        assert.ok(inputs.has(scope), 'Input must resolve the exact retained session scope')
        return inputs.get(scope)
      },
    },
  }
  const services = {
    commandUi: {},
    sessions: { scope: id => scopes.get(id) },
    conversation,
    // The public locale contract selects translations; no preference is saved.
    locale: { resolveText: text => typeof text === 'string' ? text : text[activeLanguage] ?? text.en },
  }
  for (const [key, value] of Object.entries(services)) {
    if (key !== missingService) ctx.provide(key, value)
  }
  for (const id of ['session-a', 'session-b']) {
    const scope = ctx.extend({ sessionId: id })
    scopes.set(id, scope)
    inputs.set(scope, { notify: (level, text) => notices.push({ sessionId: id, level, text }) })
  }
  await install(ctx, Loader)

  // The same registration facade, bundle response and module system as DSH Web.
  // There is no document, process, Node require, fetch or storage in this realm.
  const realm = createContext({ window: {}, console })
  for (const injection of bootInjections(graph)) {
    if (injection.kind === 'script') runInContext(injection.text, realm, { timeout: 1000 })
  }
  const bootstrap = await readFile(new URL(
    '../node_modules/@deepseek-ai/dsh-client-modules/lib/client.js', import.meta.url), 'utf8')
  runInContext(bootstrap, realm, { timeout: 1000 })
  const served = []
  const modules = realm.window.__ModuleLoader__.create({
    boot: JSON.parse(JSON.stringify(graph)),
    staticModules: {},
    async loadBundle(url) {
      const response = await registry.fetchBundle(new Request(new URL(url, 'http://fixture.invalid/')))
      assert.equal(response.status, 200, 'Native registry must serve its advertised browser bundle')
      const code = await response.text()
      served.push(code)
      runInContext(code, realm, { timeout: 1000 })
    },
  })
  ctx.loader.internal = modules
  await modules.entries.start(ctx.loader, modules.manifest)

  return {
    ctx, host, hostEntryId, hostOnlyConfig, graph, registry, modules, realm, served,
    notices, resolutions, scopes, inputs, services,
    emit(sessionId, name, result) { ctx.emit('command/executed', sessionId, name, result) },
    language(value) { activeLanguage = value },
    closeInput() { closedInput = true },
    async uninstall() {
      host.loader.remove(hostEntryId)
      await host.loader.await()
      await new Promise(resolve => setImmediate(resolve))
      await modules.entries.sync(registry.graph())
    },
  }
}
