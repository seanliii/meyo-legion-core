import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, writeFile, rm, access, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const script = fileURLToPath(new URL('../scripts/release_pair.py', import.meta.url))
const python = process.env.PYTHON || 'python3.12'
// The core stays external. Do not vendor its packager or quietly download it for tests.
const packager = process.env.LEGION_CORE_PACKAGER
const hash = bytes => createHash('sha256').update(bytes).digest('hex')

function run(command, args, options = {}) {
  return spawnSync(command, args, { encoding: 'utf8', timeout: 60_000, maxBuffer: 4 * 1024 * 1024, ...options })
}

function ok(result) {
  assert.equal(result.error, undefined, String(result.error))
  assert.equal(result.status, 0, result.stderr || result.stdout)
  return result.stdout
}

test('release-pair exposes a preparation-only CLI without loading a core or installing dependencies', () => {
  const result = run(python, ['-I', '-B', script, '--help'])
  assert.equal(result.status, 0, 'The offline release-pair preparation entry must exist')
  assert.match(result.stdout, /--core-commit/)
  assert.match(result.stdout, /--connector-pack-report/)
  assert.doesNotMatch(result.stdout, /--publish|--install|--merge/)
})

test('pairing uses the original core packager and npm pack on isolated fixed-commit fixtures', {
  skip: !packager && 'Set LEGION_CORE_PACKAGER to the existing trusted scripts/package_core.py; no automatic install.',
}, async t => {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'legion-release-pair-')))
  t.after(() => rm(root, { recursive: true, force: true }))
  const core = join(root, 'core')
  const connector = join(root, 'connector')
  const home = join(root, 'home')
  for (const path of [core, connector, home]) await mkdir(path)
  const env = {
    PATH: `${dirname(process.execPath)}:${process.env.PATH}`,
    HOME: home, TMPDIR: root, MEYO_LEGION_DIR: join(root, 'meyo'), LEGION_HOME_DIR: join(root, 'legion'),
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0',
    GIT_NO_LAZY_FETCH: '1', GIT_NO_REPLACE_OBJECTS: '1',
    GIT_AUTHOR_NAME: 'Release Pair Test', GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
    GIT_COMMITTER_NAME: 'Release Pair Test', GIT_COMMITTER_EMAIL: 'fixture@example.invalid',
    NPM_CONFIG_CACHE: join(root, 'npm-cache'), NPM_CONFIG_USERCONFIG: join(root, 'user.npmrc'),
    NPM_CONFIG_GLOBALCONFIG: join(root, 'global.npmrc'), NPM_CONFIG_OFFLINE: 'true',
    NO_PROXY: '127.0.0.1,localhost',
  }
  await writeFile(env.NPM_CONFIG_USERCONFIG, '')
  await writeFile(env.NPM_CONFIG_GLOBALCONFIG, '')
  const git = (repo, ...args) => ok(run('git', ['-c', 'core.hooksPath=/dev/null', '-C', repo, ...args], { env })).trim()
  const commit = repo => { git(repo, 'add', '.'); git(repo, 'commit', '-qm', '离线配对测试源码'); return git(repo, 'rev-parse', 'HEAD') }
  for (const repo of [core, connector]) git(repo, 'init', '-q')
  await writeFile(join(core, 'meyo.py'), '# Synthetic source fixture; never executed.\n')
  await writeFile(join(core, 'LICENSE'), 'Fixture license text, not a rights certification.\n')
  await writeFile(join(core, 'NOTICE'), 'Fixture third-party notice.\n')
  const coreCommit = commit(core)
  git(core, 'tag', 'v0.1.0')
  const manifest = {
    name: 'dsh-plugin-legion-fixture', version: '0.5.1', type: 'module', main: 'index.js',
    files: ['index.js', 'cordis.patch.yml', 'README.md'], exports: { '.': './index.js' },
    engines: { node: '>=24.9.0', dsh: '0.2.1-rc.2' },
    peerDependencies: { '@deepseek-ai/dsh-tools': '0.2.1-rc.2' },
    dsh: { bundle: { patch: './cordis.patch.yml' }, client: { platform: 'web' } },
    repository: { type: 'git', url: 'git+https://github.com/example/connector.git' },
    // npm must not execute this: packaging is not an installation/build permission.
    scripts: { prepack: 'node -e "process.exit(87)"' },
  }
  await writeFile(join(connector, 'package.json'), JSON.stringify(manifest, null, 2) + '\n')
  await writeFile(join(connector, 'index.js'), 'export const fixture = true\n')
  await writeFile(join(connector, 'cordis.patch.yml'), '[]\n')
  await writeFile(join(connector, 'README.md'), 'Protocol fixture, not a harness or product result.\n')
  const connectorCommit = commit(connector)
  git(connector, 'tag', 'v0.5.1')
  const coreArchive = join(root, 'core.tar')
  ok(run(python, ['-I', '-B', packager, 'build', '--repo', core, '--commit', coreCommit, '--output', coreArchive], { env }))
  const packOutput = ok(run('npm', ['pack', '--json', '--ignore-scripts', '--offline', '--pack-destination', root], { cwd: connector, env }))
  const packReport = join(root, 'npm-pack.json')
  await writeFile(packReport, packOutput)
  const connectorArchive = join(root, JSON.parse(packOutput)[0].filename)
  const base = {
    '--core-packager': packager, '--core-repo': core, '--core-commit': coreCommit,
    '--core-archive': coreArchive, '--core-sha256': hash(await readFile(coreArchive)),
    '--connector-repo': connector, '--connector-commit': connectorCommit,
    '--connector-archive': connectorArchive, '--connector-sha256': hash(await readFile(connectorArchive)),
    '--connector-pack-report': packReport,
  }
  let serial = 0
  const invoke = async (changes = {}) => {
    const args = { ...base, '--output': join(root, `pair-${++serial}.json`), ...changes }
    const result = run(python, ['-I', '-B', script, ...Object.entries(args).flat()], { env })
    return { result, path: args['--output'] }
  }
  const rejected = async (changes, pattern) => {
    const { result, path } = await invoke(changes)
    assert.equal(result.status, 2, result.stdout + result.stderr)
    assert.match(result.stderr, /^release-pair: /, 'The preparation entry itself must reject, not Python startup')
    assert.match(result.stderr, pattern)
    await assert.rejects(access(path))
  }
  let pair
  await t.test('one identity binds two independent original archives, not a replacement installer', async () => {
    const { result, path } = await invoke()
    ok(result)
    pair = JSON.parse(await readFile(path, 'utf8'))
    assert.equal(pair.operation, 'prepare-only')
    assert.match(pair.pair_id, /^sha256:[a-f0-9]{64}$/)
    assert.equal(pair.core.source_commit, coreCommit)
    assert.equal(pair.connector.source_commit, connectorCommit)
    assert.equal(pair.core.source_tree, git(core, 'rev-parse', `${coreCommit}^{tree}`))
    assert.equal(pair.core.version, '0.1.0')
    assert.equal(pair.connector.version, '0.5.1', 'Different package version numbers are valid')
    assert.equal(pair.core.distribution, 'private-local-only')
    assert.equal(pair.core.member_execution_verified, false)
    assert.equal(pair.connector.native.bundle_patch, './cordis.patch.yml')
    assert.equal(pair.connector.native.source.commit, connectorCommit)
    assert.equal(pair.connector.files.length, JSON.parse(packOutput)[0].files.length)
    assert.equal(pair.acceptance.performed_by_preparer, false)
    assert.deepEqual(pair.acceptance.receipts, [])
    assert.equal(pair.publication.performed, false)
    const again = await invoke()
    ok(again.result)
    assert.equal(JSON.parse(await readFile(again.path, 'utf8')).pair_id, pair.pair_id)
  })
  await t.test('npm report ordering is not mistaken for a changed package file set', async () => {
    const reordered = JSON.parse(packOutput)
    reordered[0].files.reverse()
    const path = join(root, 'npm-other-order.json')
    await writeFile(path, JSON.stringify(reordered))
    const result = await invoke({ '--connector-pack-report': path })
    ok(result.result)
    assert.equal(JSON.parse(await readFile(result.path, 'utf8')).pair_id, pair.pair_id)
  })
  await t.test('moving branches and mismatched full commits cannot label an archive', async () => {
    await rejected({ '--core-commit': 'HEAD' }, /commit|object ID/)
    await rejected({ '--connector-commit': 'main' }, /commit|object ID/)
    await writeFile(join(core, 'meyo.py'), '# Another committed source.\n')
    const next = commit(core)
    await rejected({ '--core-commit': next }, /core.*commit|source.*commit/i)
    const original = await invoke()
    ok(original.result)
    assert.equal(JSON.parse(await readFile(original.path, 'utf8')).core.source_commit, coreCommit)
  })
  await t.test('either archive checksum mismatch refuses the pair', async () => {
    await rejected({ '--core-sha256': '0'.repeat(64) }, /SHA256|sha256|checksum/i)
    await rejected({ '--connector-sha256': '0'.repeat(64) }, /SHA256|sha256|checksum/i)
  })
  await t.test('missing npm files and mismatched native pack metadata refuse the pair', async () => {
    const rows = JSON.parse(packOutput)
    rows[0].files.pop()
    const missing = join(root, 'missing-pack-file.json')
    await writeFile(missing, JSON.stringify(rows))
    await rejected({ '--connector-pack-report': missing }, /npm|file/i)
    rows[0].files = JSON.parse(packOutput)[0].files
    rows[0].integrity = 'sha512-invalid'
    await writeFile(missing, JSON.stringify(rows))
    await rejected({ '--connector-pack-report': missing }, /npm|integrity/i)
  })
  await t.test('even an internally consistent repack must match the pinned Git blobs', async () => {
    const alteredDir = join(root, 'altered')
    await mkdir(alteredDir)
    await writeFile(join(connector, 'index.js'), 'export const fixture = "changed after commit"\n')
    const altered = ok(run('npm', ['pack', '--json', '--ignore-scripts', '--offline', '--pack-destination', alteredDir], { cwd: connector, env }))
    await writeFile(join(connector, 'index.js'), 'export const fixture = true\n')
    const report = join(alteredDir, 'pack.json')
    await writeFile(report, altered)
    const archive = join(alteredDir, JSON.parse(altered)[0].filename)
    await rejected({ '--connector-archive': archive, '--connector-sha256': hash(await readFile(archive)), '--connector-pack-report': report }, /source|blob/i)
  })
  await t.test('a damaged native entry or copied core is refused even with matching outer hashes', async () => {
    for (const mutation of ['missing-main', 'copied-core']) {
      const archive = join(root, `${mutation}.tgz`)
      const report = join(root, `${mutation}-pack.json`)
      // Deliberately corrupt test archives; not a production exporter or an installation path.
      ok(run(python, ['-I', '-B', '-c', `
import base64, hashlib, io, json, sys, tarfile
source, output, original_report, report_path, mutation = sys.argv[1:]
with tarfile.open(source, "r:gz") as src, tarfile.open(output, "w:gz") as dst:
    for item in src:
        if mutation == "missing-main" and item.name == "package/index.js":
            continue
        dst.addfile(item, src.extractfile(item))
    if mutation == "copied-core":
        item = tarfile.TarInfo("package/meyo.py")
        content = b"# forbidden core copy fixture\\n"
        item.size = len(content)
        dst.addfile(item, io.BytesIO(content))
data = open(output, "rb").read()
report = json.load(open(original_report))
with tarfile.open(output, "r:gz") as tar:
    files = sorted([{"path": i.name[8:], "size": i.size, "mode": i.mode} for i in tar], key=lambda i: i["path"])
report[0].update(size=len(data), shasum=hashlib.sha1(data).hexdigest(),
    integrity="sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
    files=files, entryCount=len(files), unpackedSize=sum(i["size"] for i in files))
open(report_path, "w").write(json.dumps(report))
`, connectorArchive, archive, packReport, report, mutation], { env }))
      await rejected({ '--connector-archive': archive, '--connector-sha256': hash(await readFile(archive)), '--connector-pack-report': report },
        mutation === 'missing-main' ? /native.*missing/i : /core source/i)
    }
  })
  await t.test('an untagged core candidate does not borrow the old release version', async () => {
    const next = git(core, 'rev-parse', 'HEAD')
    const archive = join(root, 'untagged-core.tar')
    ok(run(python, ['-I', '-B', packager, 'build', '--repo', core, '--commit', next, '--output', archive], { env }))
    const { result, path } = await invoke({ '--core-commit': next, '--core-archive': archive, '--core-sha256': hash(await readFile(archive)) })
    ok(result)
    const candidate = JSON.parse(await readFile(path, 'utf8'))
    assert.equal(candidate.core.version, null)
    assert.deepEqual(candidate.core.release_tags, [])
    assert.notEqual(candidate.pair_id, pair.pair_id)
  })
  await t.test('an explicit public core keeps its own original license and verification flags', async () => {
    const archive = join(root, 'public-core.tar')
    ok(run(python, ['-I', '-B', packager, 'build', '--repo', core, '--commit', coreCommit,
      '--output', archive, '--distribution', 'public-source', '--license-id', 'MIT',
      '--license-file', 'LICENSE', '--notice-file', 'NOTICE'], { env }))
    const { result, path } = await invoke({ '--core-archive': archive, '--core-sha256': hash(await readFile(archive)) })
    ok(result)
    const publicPair = JSON.parse(await readFile(path, 'utf8'))
    assert.equal(publicPair.core.format, 'meyo-public-core-v1')
    assert.equal(publicPair.core.license.identifier, 'MIT')
    assert.equal(publicPair.core.rights_verified, false)
    assert.equal(publicPair.core.member_execution_verified, false)
    assert.notEqual(publicPair.pair_id, pair.pair_id, 'Different archive bytes cannot reuse acceptance')
    assert.equal(publicPair.publication.history_review, 'not_performed')
  })
  await t.test('old-pair evidence cannot silently certify a different pair', async () => {
    const receipt = join(root, 'old-receipt.json')
    await writeFile(receipt, JSON.stringify({ format: 'meyo-release-pair-evidence-v1', pair_id: `sha256:${'0'.repeat(64)}`, observations: [] }))
    await rejected({ '--evidence': receipt }, /evidence.*pair|pair.*evidence/i)
  })
  await t.test('bound failed and unverified observations stay failed and unverified', async () => {
    assert.ok(pair, 'The successful preparation must have produced a pair')
    const receipt = join(root, 'receipt.json')
    const observations = [
      { kind: 'original_entry_download', status: 'failed', scope: 'offline_fixture' },
      { kind: 'live_core', status: 'not_verified' },
    ]
    await writeFile(receipt, JSON.stringify({ format: 'meyo-release-pair-evidence-v1', pair_id: pair.pair_id, observations }))
    const { result, path } = await invoke({ '--evidence': receipt })
    ok(result)
    const recorded = JSON.parse(await readFile(path, 'utf8'))
    assert.equal(recorded.pair_id, pair.pair_id)
    assert.deepEqual(recorded.acceptance.receipts[0].observations, observations)
    assert.equal(recorded.acceptance.receipts[0].sha256, hash(await readFile(receipt)))
    assert.equal(recorded.acceptance.performed_by_preparer, false)
    assert.equal(recorded.publication.performed, false)
  })
  await t.test('existing evidence and source repositories are never output destinations', async () => {
    const path = join(root, 'keep.json')
    await writeFile(path, 'keep original receipt\n')
    const result = await invoke({ '--output': path })
    assert.equal(result.result.status, 2)
    assert.match(result.result.stderr, /^release-pair: /)
    assert.equal(await readFile(path, 'utf8'), 'keep original receipt\n')
    await rejected({ '--output': join(connector, 'pair.json') }, /outside|source|repository/i)
    await rejected({ '--output': join(core, 'pair.json') }, /outside|source|repository/i)
    assert.equal(git(connector, 'rev-parse', 'HEAD'), connectorCommit)
    assert.equal(git(connector, 'status', '--porcelain'), '')
  })
  await t.test('repository arguments must be actual roots, not subdirectories that weaken output isolation', async () => {
    const nested = join(core, 'nested')
    await mkdir(nested)
    await rejected({ '--core-repo': nested }, /repository root|worktree root/i)
  })
})
