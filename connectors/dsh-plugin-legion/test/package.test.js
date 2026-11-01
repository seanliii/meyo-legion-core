import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, posix } from 'node:path'
import { fileURLToPath } from 'node:url'

test('direct official runtime imports are declared for strict package managers', async () => {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  const lock = JSON.parse(await readFile(new URL('../package-lock.json', import.meta.url), 'utf8'))
  assert.equal(lock.version, manifest.version)
  assert.equal(lock.packages[''].version, manifest.version)
  for (const name of ['@deepseek-ai/dsh-tools', '@deepseek-ai/dsh-llm']) {
    assert.equal(manifest.peerDependencies[name], manifest.engines.dsh, `${name} must be a direct peer`)
    assert.equal(manifest.devDependencies[name], manifest.engines.dsh, `${name} must be directly testable`)
    assert.equal(lock.packages[''].peerDependencies[name], manifest.peerDependencies[name])
    assert.equal(lock.packages[''].devDependencies[name], manifest.devDependencies[name])
  }
})

test('native scheduler acceptance uses an explicitly declared development dependency', async () => {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  const lock = JSON.parse(await readFile(new URL('../package-lock.json', import.meta.url), 'utf8'))
  assert.equal(manifest.devDependencies['@deepseek-ai/dsh-schedule'], manifest.engines.dsh)
  assert.equal(lock.packages[''].devDependencies['@deepseek-ai/dsh-schedule'], manifest.engines.dsh)
})

test('the actual npm package includes the local resources linked by its documentation', async t => {
  const root = fileURLToPath(new URL('..', import.meta.url))
  const isolated = await mkdtemp(join(tmpdir(), 'legion-package-docs-'))
  t.after(() => rm(isolated, { recursive: true, force: true }))
  const userConfig = join(isolated, 'user.npmrc')
  const globalConfig = join(isolated, 'global.npmrc')
  await writeFile(userConfig, '')
  await writeFile(globalConfig, '')
  const result = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts', '--offline'], {
    cwd: root, encoding: 'utf8', timeout: 30_000, maxBuffer: 2 * 1024 * 1024,
    env: {
      ...process.env, PATH: `${dirname(process.execPath)}:${process.env.PATH}`,
      HOME: isolated, TMPDIR: isolated, NPM_CONFIG_USERCONFIG: userConfig,
      NPM_CONFIG_GLOBALCONFIG: globalConfig, NPM_CONFIG_CACHE: join(isolated, 'npm-cache'),
      NPM_CONFIG_OFFLINE: 'true',
    },
  })
  assert.equal(result.error, undefined)
  assert.equal(result.status, 0, result.stderr)
  const files = new Set(JSON.parse(result.stdout)[0].files.map(row => row.path))
  const missing = []
  for (const name of files) {
    if (!name.endsWith('.md')) continue
    const text = (await readFile(join(root, name), 'utf8')).replace(/^```[\s\S]*?^```.*$/gm, '')
    for (const match of text.matchAll(/\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g)) {
      const target = match[1].split(/[?#]/)[0]
      if (!target || /^(?:[a-z][\w+.-]*:|\/)/i.test(target)) continue
      const relative = posix.normalize(posix.join(posix.dirname(name), decodeURIComponent(target)))
      if (!files.has(relative)) missing.push(`${name} -> ${relative}`)
    }
  }
  assert.deepEqual(missing, [], 'Git checkout links must also work after the native package installation')
})
