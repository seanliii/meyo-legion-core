import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, writeFile, chmod, link, symlink, mkdir, rm, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { LegionClient } from '../lib/client.js'
import { fixture } from './fixture.js'

const execution = () => ({ sessionId: 'file-credential-session', signal: new AbortController().signal })
async function setup(t, token = 'fixture-token\n') {
  const root = await mkdtemp(join(tmpdir(), 'dsh-credentials-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const tokenFile = join(root, 'login-token.txt')
  await writeFile(tokenFile, token, { mode: 0o600 })
  return { root, tokenFile, stateDirectory: join(root, 'state') }
}

test('a configured private token file authenticates the actual HTTP request, not the environment fallback', async t => {
  const config = await setup(t)
  const server = await fixture(t)
  const client = new LegionClient({ ...config, baseUrl: server.baseUrl }, { LEGION_PERSONAL_TOKEN: 'wrong-env-token' })
  const result = await client.submit({ key: 'file-auth', query: 'Protocol test only' }, execution())
  assert.equal(result.submission, 'accepted')
  assert.equal(server.requests[0].headers.authorization, 'Bearer fixture-token')
  assert.equal(client.token(), 'fixture-token')
})

test('HTTP rereads credentials but refuses identity rotation until reload; old and current credentials stay redacted', async t => {
  const config = await setup(t, 'first-file-secret\n')
  const seen = []
  const server = createServer((req, res) => {
    seen.push(req.headers.authorization)
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end('{"ok":true}')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  const options = { ...config, baseUrl: `http://127.0.0.1:${server.address().port}` }
  const client = new LegionClient(options, {})
  await client.request('/first', execution())
  await writeFile(config.tokenFile, 'second/file+secret=\n', { mode: 0o600 })
  await assert.rejects(client.request('/second', execution()), /credential changed|reload/i)
  assert.equal(client.controller.signal.aborted, true)
  assert.deepEqual(seen, ['Bearer first-file-secret'])
  const reloaded = new LegionClient(options, {})
  t.after(() => { client.dispose(); reloaded.dispose() })
  await reloaded.request('/second', execution())
  assert.deepEqual(seen, ['Bearer first-file-secret', 'Bearer second/file+secret='])
  await rm(config.tokenFile)
  const safe = client.redactText('first-file-secret second/file+secret= second%2Ffile%2Bsecret%3D')
  assert.equal(safe, '[redacted] [redacted] [redacted]')
})

test('missing token file never falls back to an environment token and does not reserve a submission key', async t => {
  const config = await setup(t)
  const server = await fixture(t)
  const client = new LegionClient({ ...config, baseUrl: server.baseUrl }, { LEGION_PERSONAL_TOKEN: 'fixture-token' })
  await rm(config.tokenFile)
  await assert.rejects(client.submit({ key: 'retry-after-config', query: 'Protocol test only' }, execution()), /credential file/i)
  assert.equal(server.requests.length, 0)
  await assert.rejects(client.list(execution()), /credential file/i)
  await assert.rejects(stat(config.stateDirectory), { code: 'ENOENT' }, 'Missing credentials must not create recovery state')
  await writeFile(config.tokenFile, 'fixture-token\n', { mode: 0o600 })
  assert.equal((await client.submit({ key: 'retry-after-config', query: 'Protocol test only' }, execution())).submission, 'accepted')
})

test('credential file errors do not echo a path, file contents, or the environment credential', async t => {
  const config = await setup(t, 'secret-line-one\nsecret-line-two\n')
  const client = new LegionClient(config, { LEGION_PERSONAL_TOKEN: 'env-secret-must-not-leak' })
  assert.throws(() => client.token(), error => {
    assert.match(error.message, /credential file/i)
    for (const secret of [config.tokenFile, 'secret-line-one', 'secret-line-two', 'env-secret-must-not-leak']) {
      assert.ok(!error.message.includes(secret))
    }
    return true
  })
})

test('private regular files allow one trailing newline, but reject empty, multiline, large or non-header content', async t => {
  const config = await setup(t)
  const client = new LegionClient(config, {})
  for (const body of ['plain-token', 'plain-token\n', 'plain-token\r\n']) {
    await writeFile(config.tokenFile, body)
    assert.equal(client.token(), 'plain-token')
  }
  for (const body of ['', '\n', 'token\n\n', 'token\nother', 'token\rother', 'token\tother',
    ' token ', '令牌', 'secret\0suffix', 'x'.repeat(4097)]) {
    await writeFile(config.tokenFile, body)
    assert.throws(() => client.token(), /credential file/i)
  }
})

test('credential files reject group/world access, symbolic links, hard links and directories', async t => {
  const config = await setup(t)
  const client = new LegionClient(config, { LEGION_PERSONAL_TOKEN: 'fixture-token' })
  await chmod(config.tokenFile, 0o640)
  assert.throws(() => client.token(), /credential file/i)
  await chmod(config.tokenFile, 0o600)
  const second = join(config.root, 'second.txt')
  await link(config.tokenFile, second)
  assert.throws(() => client.token(), /credential file/i)
  await rm(second)
  await symlink(config.tokenFile, second)
  assert.throws(() => new LegionClient({ ...config, tokenFile: second }).token(), /credential file/i)
  await rm(config.tokenFile)
  await mkdir(config.tokenFile)
  assert.throws(() => client.token(), /credential file/i)
})

test('tokenFile must be an absolute user configuration; malformed values are not treated as absent', () => {
  for (const tokenFile of ['relative/token', '', null, 42, {}, '/tmp/a\0b', '/tmp/../secret']) {
    assert.throws(() => new LegionClient({ tokenFile }), /tokenFile/i)
  }
})

test('previously used environment tokens also remain redacted after rotation', () => {
  const env = { LEGION_PERSONAL_TOKEN: 'old-env-secret' }
  const client = new LegionClient({}, env)
  client.token()
  env.LEGION_PERSONAL_TOKEN = 'new-env-secret'
  client.token()
  delete env.LEGION_PERSONAL_TOKEN
  assert.equal(client.redactText('old-env-secret new-env-secret'), '[redacted] [redacted]')
})
