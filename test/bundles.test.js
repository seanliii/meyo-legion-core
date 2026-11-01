import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const MiB = 1024 * 1024

async function builder() {
  let module
  try {
    module = await import('../lib/bundles.js')
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('/lib/bundles.js')) throw error
  }
  assert.equal(typeof module?.buildBundle, 'function', 'The pure native bundle builder must exist')
  return module.buildBundle
}

function spec() {
  return {
    package_name: '@adapted/research-tools',
    version: '1.2.3',
    description: '经过源码适配的检索与笔记插件。',
    plugins: [
      { id: 'search', title: '资料检索', description: '从已接入的数据源查找资料。', entry: 'plugins/search.js' },
      { id: 'notes', title: '研究笔记', description: '保存用户选择的研究笔记。', entry: './plugins/notes.mjs' },
    ],
    files: [
      { path: 'plugins/search.js', content: 'export function apply(ctx) { /* already adapted */ }\n' },
      { path: './plugins/notes.mjs', content: 'export function apply(ctx) { /* independently adapted */ }\n' },
      { path: 'data/说明.txt', content: '调用方提供的文本，不能改写。\r\n' },
    ],
    dependencies: [{ name: '@example/exact', version: '2.0.0-rc.1+build.7' }, { name: 'zod', version: '3.23.8' }],
  }
}

function file(bundle, path) {
  const result = bundle.files.find(item => item.path === path)
  assert.ok(result, `Missing emitted file: ${path}`)
  return result
}

function manifest(bundle) {
  return JSON.parse(file(bundle, 'package.json').content)
}

async function materialize(t, bundle) {
  await mkdir(join(root, '.qa'), { recursive: true })
  const directory = await mkdtemp(join(root, '.qa', 'bundle-parser-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  for (const item of bundle.files) {
    const destination = join(directory, item.path)
    await mkdir(dirname(destination), { recursive: true })
    await writeFile(destination, item.content, 'utf8')
  }
  return directory
}

test('packages real adapted files with the first root export and every independently addressable subpath', async () => {
  const buildBundle = await builder()
  const input = spec()
  const result = buildBundle(input)
  assert.equal(result.packageName, '@adapted/research-tools')
  assert.equal(result.version, '1.2.3')
  assert.deepEqual(result.plugins.map(({ id, rowId, name, entry }) => ({ id, rowId, name, entry })), [
    { id: 'search', rowId: '@adapted/research-tools/search', name: '@adapted/research-tools/search', entry: 'plugins/search.js' },
    { id: 'notes', rowId: '@adapted/research-tools/notes', name: '@adapted/research-tools/notes', entry: 'plugins/notes.mjs' },
  ])
  const pkg = manifest(result)
  assert.equal(pkg.name, input.package_name)
  assert.equal(pkg.version, input.version)
  assert.equal(pkg.description, input.description)
  assert.equal(pkg.type, 'module')
  assert.equal(pkg.exports['.'], './plugins/search.js')
  assert.equal(pkg.exports['./search'], './plugins/search.js')
  assert.equal(pkg.exports['./notes'], './plugins/notes.mjs')
  assert.equal(pkg.exports['./package.json'], './package.json')
  assert.deepEqual(pkg.dsh, { bundle: { patch: './cordis.patch.yml' } })
  assert.deepEqual(pkg.dependencies, { '@example/exact': '2.0.0-rc.1+build.7', zod: '3.23.8' })
  assert.equal(pkg.private, true, 'Workspace adaptation bundles must not publish accidentally')
  assert.ok(!Object.hasOwn(pkg, 'scripts'))
  assert.ok(!Object.hasOwn(pkg, 'license'))
  assert.deepEqual(new Set(pkg.files), new Set(result.files.map(item => item.path)))
  for (const source of input.files) {
    assert.equal(file(result, source.path.replace(/^\.\//, '')).content, source.content)
  }
})

test('emits distinct per-plugin display exports and a shared original self-contained SVG', async () => {
  const buildBundle = await builder()
  const input = spec()
  input.plugins[0].title = '检索 "原文": !!js <script>not executable</script>'
  input.plugins[0].description = '第一行\n第二行: [原样文本]'
  const result = buildBundle(input)
  const pkg = manifest(result)
  for (const plugin of input.plugins) {
    for (const language of ['en', 'zh']) {
      const path = pkg.exports[`./${plugin.id}/locale/*.json`].replace('./', '').replace('*', language)
      assert.deepEqual(JSON.parse(file(result, path).content), {
        meta: { title: plugin.title, description: plugin.description },
      })
    }
    assert.equal(pkg.exports[`./${plugin.id}/icon`], './icon.svg')
  }
  assert.equal(pkg.exports['./icon'], './icon.svg')
  assert.equal(pkg.exports['./locale/*.json'], './locale/*.json')
  const icon = file(result, 'icon.svg').content
  assert.match(icon, /^<svg\b[^>]*xmlns="http:\/\/www\.w3\.org\/2000\/svg"/)
  assert.match(icon, /<\/svg>\s*$/)
  assert.doesNotMatch(icon, /<script|<foreignObject|(?:href|onload)\s*=/i)
  assert.ok(Buffer.byteLength(icon) < 256 * 1024)
})

test('preserves supplied provenance facts and license text without deriving or granting a license', async () => {
  const buildBundle = await builder()
  const provenance = [{
    url: 'https://github.com/example/upstream/blob/abc123/index.js',
    kind: 'source',
    revision: 'abc123',
    license: { observed: null, status: 'not_checked' },
    checked: false,
    facts: ['原始文字', { claim: 'caller-supplied', evidence: ['path:17', 'sha256:literal'] }],
  }]
  const untouched = structuredClone(provenance)
  const input = spec()
  const result = buildBundle(input, provenance)
  assert.deepEqual(JSON.parse(file(result, 'provenance.json').content), untouched)
  assert.deepEqual(provenance, untouched)
  assert.deepEqual(JSON.parse(file(buildBundle(input), 'provenance.json').content), [])
  assert.ok(!result.files.some(item => item.path === 'LICENSE'))
  assert.ok(!Object.hasOwn(manifest(result), 'license'))
  assert.ok(file(result, 'SOURCE-NOTICE.md').content.trim())
  const license = 'Caller supplied license text.\nCopyright original authors.\n'
  input.license = 'SEE LICENSE IN LICENSE'
  input.files.push({ path: 'LICENSE', content: license })
  const explicit = buildBundle(input, provenance)
  assert.equal(file(explicit, 'LICENSE').content, license)
  assert.equal(manifest(explicit).license, 'SEE LICENSE IN LICENSE')
})

test('returns deterministic detached text and UTF-8 digests without mutating inputs or executing JavaScript', async () => {
  const buildBundle = await builder()
  const input = spec()
  const marker = '__bundleMustNotExecute'
  input.files[0].content = `globalThis.${marker} = true; throw new Error("Never import this input");\n`
  const before = structuredClone(input)
  const result = buildBundle(input)
  assert.equal(globalThis[marker], undefined)
  assert.deepEqual(input, before)
  assert.deepEqual(result, buildBundle(structuredClone(input)))
  assert.deepEqual(result, buildBundle({ ...input, files: [...input.files].reverse(), dependencies: [...input.dependencies].reverse() }))
  let total = 0
  for (const item of result.files) {
    assert.deepEqual(Object.keys(item).sort(), ['content', 'path', 'sha256'])
    assert.equal(item.sha256, createHash('sha256').update(item.content, 'utf8').digest('hex'))
    total += Buffer.byteLength(item.content, 'utf8')
  }
  assert.equal(result.totalBytes, total)
  assert.deepEqual(result.verification, { installed: 'unverified', active: 'unverified', called: 'unverified' })
  result.plugins[0].title = 'mutated output'
  result.files[0].content = 'mutated output'
  assert.deepEqual(input, before)
})

test('builds with filesystem writes and child processes denied by Node permissions', async () => {
  await builder()
  const source = `
    import { buildBundle } from ${JSON.stringify(new URL('../lib/bundles.js', import.meta.url).href)};
    const result = buildBundle(${JSON.stringify(spec())});
    process.stdout.write(JSON.stringify({ packageName: result.packageName, count: result.plugins.length }));
  `
  const output = execFileSync(process.execPath, [
    '--permission', `--allow-fs-read=${root}`, '--input-type=module', '--eval', source,
  ], { cwd: root, encoding: 'utf8', timeout: 5000, maxBuffer: MiB })
  assert.deepEqual(JSON.parse(output), { packageName: '@adapted/research-tools', count: 2 })
})

test('official manifest, patch composition and display readers recognize every plugin without importing entries', async t => {
  const buildBundle = await builder()
  const input = spec()
  input.files[0].content = 'throw new Error("Official metadata reading must not execute entry files")\n'
  const result = buildBundle(input)
  const directory = await materialize(t, result)
  const {
    readProfileManifest, bundlePatchPaths, loadOverlayPatches, composeEntries,
    readPluginMeta, resolvePluginResource,
  } = await import('@deepseek-ai/dsh-app-boot')
  const pkg = readProfileManifest('bundle-test', directory)
  const paths = bundlePatchPaths(directory, pkg.dsh.bundle)
  assert.deepEqual(paths, [join(directory, 'cordis.patch.yml')])
  const layers = paths.map(path => loadOverlayPatches('bundle-test', path))
  const warnings = []
  const rows = composeEntries(layers, warning => warnings.push(warning))
  assert.deepEqual(rows, [
    { id: '@adapted/research-tools/search', name: '@adapted/research-tools/search', config: {} },
    { id: '@adapted/research-tools/notes', name: '@adapted/research-tools/notes', config: {} },
  ])
  assert.deepEqual(warnings, [])
  const switched = composeEntries([...layers, [{ id: '@adapted/research-tools/search', disabled: true }]])
  assert.equal(switched[0].disabled, true)
  assert.notEqual(switched[1].disabled, true)
  assert.deepEqual(composeEntries([...layers, [
    { id: '@adapted/research-tools/search', disabled: true },
    { id: '@adapted/research-tools/search', disabled: false },
    { id: '@adapted/research-tools/notes', disabled: true },
  ]]).map(row => row.disabled), [false, true])
  const parentURL = pathToFileURL(join(directory, 'package.json')).href
  assert.equal(resolvePluginResource(result.packageName, parentURL), join(directory, 'plugins/search.js'))
  for (const plugin of result.plugins) {
    assert.equal(resolvePluginResource(plugin.name, parentURL), join(directory, plugin.entry))
    const meta = readPluginMeta(plugin.name, parentURL)
    assert.equal(meta?.error, undefined, meta?.error)
    assert.deepEqual(meta.title, { en: plugin.title, zh: plugin.title })
    assert.deepEqual(meta.description, { en: plugin.description, zh: plugin.description })
    assert.equal(meta.icon, `data:image/svg+xml;base64,${Buffer.from(file(result, 'icon.svg').content).toString('base64')}`)
  }
  const bundleMeta = readPluginMeta(result.packageName, parentURL)
  assert.equal(bundleMeta?.error, undefined, bundleMeta?.error)
  assert.deepEqual(bundleMeta.description, { en: input.description, zh: input.description })
  for (const item of result.files) {
    assert.equal(await readFile(join(directory, item.path), 'utf8'), item.content)
  }
})

test('the native installer accepts the bundle directory, not plugin subpaths as standalone packages', async t => {
  const buildBundle = await builder()
  const input = spec()
  input.files[0].content = 'throw new Error("Install-spec parsing must not execute an entry")\n'
  const result = buildBundle(input)
  const directory = await materialize(t, result)
  const { parseInstallSpec, InvalidInstallSpecError } = await import('@deepseek-ai/dsh-plugin-manager')
  assert.deepEqual(parseInstallSpec(directory), { kind: 'path', spec: directory, path: directory })
  assert.deepEqual(parseInstallSpec(result.packageName), {
    kind: 'registry', spec: result.packageName, name: result.packageName,
  })
  for (const plugin of result.plugins) {
    assert.throws(() => parseInstallSpec(plugin.name), InvalidInstallSpecError)
  }
})

test('retains caller-declared exact runtime dependencies without inferring imports or relabeling them as peers', async t => {
  const buildBundle = await builder()
  const input = spec()
  const names = [
    '@deepseek-ai/cordis', '@deepseek-ai/dsh-skill',
    '@deepseek-ai/dsh-tools', '@deepseek-ai/schemastery',
  ]
  input.files[0].content = `${names.map(name => `import ${JSON.stringify(name)};`).join('\n')}\nthrow new Error("Do not execute runtime dependency fixtures");\n`
  delete input.dependencies
  const undeclared = manifest(buildBundle(input))
  assert.ok(!Object.hasOwn(undeclared, 'dependencies'), 'The wrapper must not infer a dependency from source text')
  const expected = {}
  input.dependencies = []
  for (const name of names) {
    const installed = JSON.parse(await readFile(join(root, 'node_modules', name, 'package.json'), 'utf8'))
    expected[name] = installed.version
    input.dependencies.push({ name, version: installed.version })
  }
  const result = buildBundle(input)
  const directory = await materialize(t, result)
  const { readProfileManifest } = await import('@deepseek-ai/dsh-app-boot')
  const pkg = readProfileManifest('bundle-exact-runtime-test', directory)
  assert.deepEqual(pkg.dependencies, expected)
  assert.ok(!Object.hasOwn(pkg, 'peerDependencies'), 'Dependencies are not host-supplied peer compatibility declarations')
  assert.ok(!Object.hasOwn(pkg, 'engines'), 'The wrapper must not guess the host version requirement')
  assert.ok(!Object.hasOwn(pkg, 'scripts'))
  assert.equal(file(result, 'plugins/search.js').content, input.files[0].content)
  assert.deepEqual(result.verification, { installed: 'unverified', active: 'unverified', called: 'unverified' })
})

test('generated instructions separate package installation, independent rows and unverified peer compatibility', async () => {
  const buildBundle = await builder()
  const readme = file(buildBundle(spec()), 'README.md').content
  const example = readme.match(/```json\n([\s\S]*?)\n```/)
  assert.ok(example, 'The generated README must show the native installation operation')
  assert.deepEqual(JSON.parse(example[1]), {
    action: 'install_bundle', target: 'file:/absolute/workspace/adapted-bundle', enabled: false,
  })
  assert.match(readme, /file:.*tar/)
  assert.match(readme, /link:.*不会.*安装.*依赖/)
  assert.match(readme, /patch rowId.*moduleName.*不是.*entryId/)
  assert.match(readme, /disabled.*entryId.*不要猜/)
  assert.match(readme, /list_plugins.*entryId/)
  assert.match(readme, /安装、选择、激活和调用.*事实/)
  assert.match(readme, /禁用不等于禁止.*构建脚本/)
  assert.match(readme, /安装单位是整个 bundle 包/)
  assert.match(readme, /subpath 不是独立 npm 包/)
  assert.match(readme, /set_bundle/)
  assert.match(readme, /set_plugin/)
  assert.match(readme, /不会扫描 imports/)
  assert.match(readme, /不生成 peerDependencies 或 engines/)
  assert.match(readme, /cordis-plugin-development/)
  assert.match(readme, /references\/host-plugin\.md/)
  assert.match(readme, /references\/practices\.md/)
  assert.match(readme, /服务注入不等于工具执行授权/)
  assert.match(readme, /模块导入、fiber 激活和真实调用是不同事实/)
  for (const stage of ['installed', 'active', 'called']) assert.match(readme, new RegExp(`${stage}: unverified`))
})

test('plugin ids ending in .json cannot collide with the bundle locale directory or its metadata reader', async t => {
  const buildBundle = await builder()
  const input = spec()
  input.plugins[0].id = 'en.json'
  input.plugins[1].id = 'zh.json'
  const result = buildBundle(input)
  const directory = await materialize(t, result)
  const { readPluginMeta } = await import('@deepseek-ai/dsh-app-boot')
  const parentURL = pathToFileURL(join(directory, 'package.json')).href
  for (const name of [result.packageName, ...result.plugins.map(plugin => plugin.name)]) {
    const meta = readPluginMeta(name, parentURL)
    assert.equal(meta?.error, undefined, meta?.error)
    assert.ok(meta?.title)
    assert.ok(meta?.icon)
  }
})

test('rejects unsafe, aliased and non-materializable file paths rather than normalizing traversal', async () => {
  const buildBundle = await builder()
  for (const path of [
    '../escape.js', 'a/../escape.js', '././entry.js', '/absolute.js', '//server/share.js',
    'C:/entry.js', 'C:\\entry.js', 'folder\\entry.js', 'bad\0.js', 'a//b.js',
    'a/./b.js', 'a/', 'a\nb.js', 'https://example.com/a.js',
    '%2e%2e/escape.js', 'a/%2fentry.js', 'trailing. ', ' trailing.js', 'trailing.js ',
    'NUL.js', 'con', 'aux.txt', 'COM1.js', 'LPT9', 'a'.repeat(256) + '.js',
    'plugins/fragment#entry.js', '[pattern].js', '{a,b}.js', '!entry.js', '@(entry).js',
  ]) {
    const input = spec()
    input.files.push({ path, content: 'payload' })
    assert.throws(() => buildBundle(input), /path|路径|reserved|保留|size|大小|limit/i, path)
  }
  for (const paths of [
    ['extra.js', './extra.js'], ['EXTRA.js', 'extra.js'],
    ['café.js', 'cafe\u0301.js'], ['folder', 'folder/child.js'], ['folder/child.js', 'FOLDER'],
  ]) {
    const input = spec()
    input.files.push(...paths.map(path => ({ path, content: 'payload' })))
    assert.throws(() => buildBundle(input), /duplicate|collision|conflict|重复|冲突/i, paths.join(', '))
  }
})

test('rejects generated metadata overrides and package-manager control files at any nesting level', async () => {
  const buildBundle = await builder()
  for (const path of [
    'package.json', './PACKAGE.JSON', 'package.json/child', 'nested/package.json',
    'cordis.patch.yml', 'README.md', 'provenance.json', 'SOURCE-NOTICE.md', 'icon.svg',
    'locale', 'locale/en.json', 'locale/search/en.json',
    '.npmrc', '.yarnrc.yml', '.pnpmfile.cjs', '.npmignore', 'nested/.npmrc', 'nested/binding.gyp',
    'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'yarn.lock',
    'node_modules/dependency/index.js', 'nested/node_modules/x.js', '.git/config',
  ]) {
    const input = spec()
    input.files.push({ path, content: '{"scripts":{"postinstall":"must not run"}}' })
    assert.throws(() => buildBundle(input), /reserved|保留|package|metadata|元文件|路径|path|conflict|冲突/i, path)
  }
})

test('rejects absent, empty, invalid or unlisted entries and duplicate/reserved plugin ids', async () => {
  const buildBundle = await builder()
  for (const entry of ['missing.js', '/plugins/search.js', '../plugins/search.js', 'plugins\\search.js', 'plugins/search.ts', 'plugins/search.json']) {
    const input = spec()
    input.plugins[0].entry = entry
    assert.throws(() => buildBundle(input), /entry|入口|path|路径/i, entry)
  }
  for (const content of ['', ' \r\n\t', null, { code: 'export function apply() {}' }, Buffer.from('export {}')]) {
    const input = spec()
    input.files[0].content = content
    assert.throws(() => buildBundle(input), /content|内容|JSON|plain|文本|empty|空/i)
  }
  for (const id of ['', '..', '../bad', 'bad/id', 'bad\\id', 'bad\0id', 'icon', 'locale', 'package.json', '__proto__', 'constructor', 'node_modules']) {
    const input = spec()
    input.plugins[0].id = id
    assert.throws(() => buildBundle(input), /id|plugin|插件|reserved|保留/i, id)
  }
  for (const id of ['trailing.', 'con', 'aux.txt', 'lpt9']) {
    const input = spec()
    input.plugins[0].id = id
    assert.throws(() => buildBundle(input), /id|plugin|插件|reserved|保留|path|路径/i, id)
  }
  const duplicate = spec()
  duplicate.plugins[1].id = 'search'
  assert.throws(() => buildBundle(duplicate), /duplicate|重复/i)
  for (const replacement of [{ plugins: [] }, { files: [] }, { plugins: null }, { files: null }]) {
    assert.throws(() => buildBundle({ ...spec(), ...replacement }), /plugins|files|插件|文件|array|数组/i)
  }
})

test('accepts only literal safe package names and exact npm semver dependencies', async () => {
  const buildBundle = await builder()
  for (const package_name of [
    '', '../bad', '/absolute', '@scope/../bad', 'name space', 'UpperCase', 'name;touch-pwned',
    'https://example.com/name', 'node:fs', 'name@1.0.0', '__proto__', 'node_modules', 'favicon.ico',
  ]) {
    assert.throws(() => buildBundle({ ...spec(), package_name }), /package|包名/i, package_name)
  }
  for (const version of [
    '', 'latest', '*', '^1.2.3', '~1.2.3', '>=1.2.3', '1.2', 'v1.2.3', '=1.2.3',
    '01.2.3', '1.2.3-01', '1.2.3-', '1.2.3+', '1.2.3 ', '9007199254740992.0.0',
    'npm:other@1.2.3', 'workspace:*', 'file:../x', 'link:../x', 'git+https://example.com/repo.git',
    'https://example.com/a.tgz', 'github:example/repo', '1.2.3 || 2.0.0',
  ]) {
    assert.throws(() => buildBundle({ ...spec(), version }), /version|版本|semver/i, version)
    const input = spec()
    input.dependencies = [{ name: 'example', version }]
    assert.throws(() => buildBundle(input), /version|版本|semver/i, version)
  }
  for (const version of ['0.0.0', '1.2.3', '1.2.3-0', '1.2.3-alpha.1', '1.2.3-01a+001.hash']) {
    const input = spec()
    input.version = version
    input.dependencies = [{ name: 'example', version }]
    assert.equal(manifest(buildBundle(input)).dependencies.example, version)
  }
  const duplicate = spec()
  duplicate.dependencies = [{ name: 'example', version: '1.0.0' }, { name: 'example', version: '2.0.0' }]
  assert.throws(() => buildBundle(duplicate), /duplicate|重复/i)
  const self = spec()
  self.dependencies = [{ name: self.package_name, version: '1.0.0' }]
  assert.throws(() => buildBundle(self), /self|自身|自己/i)
  for (const value of [null, {}, 'example@1.0.0']) {
    assert.throws(() => buildBundle({ ...spec(), dependencies: value }), /dependencies|array|数组/i)
  }
})

test('refuses unknown executable manifest fields and non-data object behavior without invoking it', async () => {
  const buildBundle = await builder()
  for (const [key, value] of Object.entries({
    scripts: { prepare: 'do not run', postinstall: 'do not run' },
    exports: { '.': '/outside.js' }, main: '/outside.js', type: 'commonjs',
    bin: { run: './plugins/search.js' }, publishConfig: { registry: 'https://untrusted.example' },
    dsh: { bundle: { patch: '../elsewhere.yml' } }, overrides: { example: 'latest' },
    devDependencies: { example: '*' }, optionalDependencies: { example: '*' },
    bundledDependencies: ['example'], workspaces: ['../*'], config: { unsafe: true },
  })) {
    assert.throws(() => buildBundle({ ...spec(), [key]: value }), /field|字段|unknown|不支持|allowed|允许/i, key)
  }
  const extraDependencyField = spec()
  extraDependencyField.dependencies = [{ name: 'example', version: '1.0.0', scripts: { postinstall: 'anything' } }]
  assert.throws(() => buildBundle(extraDependencyField), /field|字段|unknown|不支持|allowed|允许/i)
  let invoked = 0
  const getter = spec()
  Object.defineProperty(getter, 'description', { enumerable: true, get() { invoked++; return 'must not run' } })
  assert.throws(() => buildBundle(getter), /accessor|getter|data|数据|JSON/i)
  assert.throws(() => buildBundle(spec(), [{ toJSON() { invoked++; return {} } }]), /JSON|function|函数|data|数据/i)
  const proxy = new Proxy(spec(), { ownKeys() { invoked++; throw new Error('must not run') } })
  assert.throws(() => buildBundle(proxy), /proxy|Proxy|JSON|data|数据/i)
  assert.equal(invoked, 0)
  const cycle = {}
  cycle.self = cycle
  for (const facts of [{}, [cycle], [new Date()], [undefined], [NaN], [1n]]) {
    assert.throws(() => buildBundle(spec(), facts), /provenance|JSON|cycle|循环|data|数据|array|数组/i)
  }
})

test('bounds individual UTF-8 files, aggregate output, plugin/file counts and provenance size', async () => {
  const buildBundle = await builder()
  const boundary = spec()
  boundary.files.push({ path: 'one-mebibyte.txt', content: 'x'.repeat(MiB) })
  assert.equal(file(buildBundle(boundary), 'one-mebibyte.txt').content.length, MiB)
  for (const content of ['x'.repeat(MiB + 1), '界'.repeat(Math.floor(MiB / 3) + 1)]) {
    const input = spec()
    input.files.push({ path: 'too-large.txt', content })
    assert.throws(() => buildBundle(input), /size|byte|limit|large|大小|字节|超限/i)
  }
  const total = spec()
  total.files = Array.from({ length: 8 }, (_, i) => ({ path: `entry-${i}.js`, content: 'x'.repeat(MiB) }))
  total.plugins = [{ id: 'entry', title: 'Entry', description: 'Entry', entry: 'entry-0.js' }]
  assert.throws(() => buildBundle(total), /size|byte|limit|large|大小|字节|超限/i)
  const files = spec()
  files.files.push(...Array.from({ length: 256 }, (_, i) => ({ path: `extra-${i}.txt`, content: 'x' })))
  assert.throws(() => buildBundle(files), /files|count|limit|数量|文件|超限/i)
  const plugins = spec()
  plugins.plugins = Array.from({ length: 65 }, (_, i) => ({ id: `entry-${i}`, title: 'Entry', description: 'Entry', entry: 'plugins/search.js' }))
  assert.throws(() => buildBundle(plugins), /plugins|count|limit|数量|插件|超限/i)
  assert.throws(() => buildBundle(spec(), [{ text: 'x'.repeat(256 * 1024) }]), /provenance|size|byte|limit|来源|大小|字节|超限/i)
})
