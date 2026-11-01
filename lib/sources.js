import { createHash } from 'node:crypto'

const API = 'https://api.github.com'
const TRUST = 'untrusted_source'
const API_VERSION = '2026-03-10'
const SHA = /^[a-f0-9]{40}$/i
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
const FILE_BYTES = 1024 * 1024
const PREVIEW_BYTES = 12000
const INDEX_ENTRIES = 2000
const MANIFEST_FILES = 8
// File-name selection is only a preview index, never a claim about capabilities.
const MANIFESTS = new Set([
  'package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt',
  'cargo.toml', 'go.mod', 'deno.json', 'deno.jsonc', 'composer.json', 'gemfile',
  'pom.xml', 'build.gradle', 'build.gradle.kts', 'mix.exs', 'project.clj',
  'cmakelists.txt', 'pubspec.yaml', 'package.swift', 'cordis.yml', 'cordis.yaml',
])

// Errors deliberately contain only local messages and allowlisted protocol facts.
// Never attach the original exception, API message/body, Location, or abort reason.
class GitHubSourceError extends Error {
  constructor(code, message, details = {}) {
    super(message)
    this.name = code === 'ABORTED' ? 'AbortError' : code === 'TIMED_OUT' ? 'TimeoutError' : 'GitHubSourceError'
    this.code = code
    this.trust = TRUST
    Object.assign(this, details)
  }

  toJSON() {
    return sourceErrorDiagnostics(this)
  }
}

/** Safe source facts shared by native failures and discovery's partial results. */
export function sourceErrorDiagnostics(error) {
  if (!(error instanceof GitHubSourceError)) return undefined
  const number = (value, min = 0, max = Number.MAX_SAFE_INTEGER) =>
    Number.isSafeInteger(value) && value >= min && value <= max ? value : null
  // These strings are minted locally, never from a response body or cause.
  const facts = { name: error.name, message: error.message, code: error.code, trust: TRUST }
  if (Object.hasOwn(error, 'status')) facts.status = number(error.status, 100, 599)
  if (Object.hasOwn(error, 'rate_limit')) {
    const rate = error.rate_limit ?? {}
    facts.rate_limit = {
      limit: number(rate.limit), remaining: number(rate.remaining), used: number(rate.used),
      reset_epoch_seconds: number(rate.reset_epoch_seconds),
      resource: ['core', 'search', 'code_search'].includes(rate.resource) ? rate.resource : null,
    }
  }
  if (Object.hasOwn(error, 'retry_after_seconds')) {
    facts.retry_after_seconds = number(error.retry_after_seconds, 0, 999999999)
  }
  if (typeof error.retryable === 'boolean') facts.retryable = error.retryable
  return facts
}

const fail = (code, message, details) => { throw new GitHubSourceError(code, message, details) }
const invalidResponse = () => fail('INVALID_RESPONSE', 'GitHub returned an invalid source response')
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)

function integer(value, label, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    fail('INVALID_ARGUMENT', `${label} must be an integer between ${min} and ${max}`)
  }
  return value
}

function text(value, label, max) {
  if (typeof value !== 'string' || !value.trim() || value.length > max
    || !value.isWellFormed() || /[\x00-\x1f\x7f-\x9f]/.test(value)) {
    fail('INVALID_ARGUMENT', `${label} must be non-empty text without control characters (at most ${max} characters)`)
  }
  return value
}

function repositoryName(value) {
  text(value, 'repository', 256)
  // Validate the raw input, not a URL parser's normalized path: parsers erase
  // dot segments and backslashes. No credentials, ports, query, or fragment.
  const match = /^(?:https:\/\/github\.com\/)?([a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)\/([a-z0-9._-]{1,100})$/i.exec(value)
  if (!match) fail('INVALID_ARGUMENT', 'repository must be owner/repo or a canonical https://github.com/owner/repo URL')
  const name = match[2].replace(/\.git$/i, '')
  if (!name || name === '.' || name === '..') fail('INVALID_ARGUMENT', 'repository name is invalid')
  return `${match[1]}/${name}`
}

function optionalText(value, max = 8192) {
  if (value === undefined || value === null) return null
  if (typeof value !== 'string' || value.length > max || !value.isWellFormed()) invalidResponse()
  return value
}

function licenseFacts(value) {
  if (value === undefined || value === null) return { key: null, name: null, spdx_id: null }
  if (!object(value)) invalidResponse()
  return { key: optionalText(value.key, 128), name: optionalText(value.name, 512), spdx_id: optionalText(value.spdx_id, 128) }
}

function refName(value) {
  text(value, 'ref', 1024)
  if (/[\s\\%:?*[\]~^#]/.test(value) || value.includes('..') || value.includes('@{')
    || value.startsWith('/') || value.endsWith('/') || value.split('/').some(part => !part || part === '.')) {
    fail('INVALID_ARGUMENT', 'ref must be a literal GitHub branch, tag, or commit reference')
  }
  return value
}

function commitSha(value, input = false) {
  if (typeof value !== 'string' || !SHA.test(value)) {
    if (input) fail('INVALID_ARGUMENT', 'commit must be a full 40-character hexadecimal commit SHA')
    invalidResponse()
  }
  return value.toLowerCase()
}

function filePath(value) {
  text(value, 'path', 4096)
  const parts = value.split('/')
  if (Buffer.byteLength(value) > 4096 || /[\\%]/.test(value) || /^[a-z]:/i.test(value)
    || parts.length > 32 || parts.some(part => !part || part === '.' || part === '..')) {
    fail('INVALID_ARGUMENT', 'path must be a literal repository-relative path without traversal or encoded segments (maximum depth 32)')
  }
  return parts
}

function treeData(data, expected, recursive = false) {
  if (!object(data) || !Array.isArray(data.tree) || typeof data.truncated !== 'boolean') invalidResponse()
  const sha = commitSha(data.sha)
  if (sha !== expected) fail('SOURCE_MISMATCH', 'GitHub tree identity did not match the pinned source')
  const seen = new Set()
  const entries = data.tree.map(entry => {
    if (!object(entry)) invalidResponse()
    let parts
    try { parts = filePath(entry.path) } catch { invalidResponse() }
    if ((!recursive && parts.length !== 1) || seen.has(entry.path)) invalidResponse()
    seen.add(entry.path)
    const modeType = { '100644': 'blob', '100755': 'blob', '120000': 'blob', '040000': 'tree', '160000': 'commit' }
    if (!Object.hasOwn(modeType, entry.mode) || modeType[entry.mode] !== entry.type) invalidResponse()
    const result = { path: entry.path, type: entry.type, mode: entry.mode, sha: commitSha(entry.sha) }
    if (entry.type === 'blob') {
      if (!Number.isSafeInteger(entry.size) || entry.size < 0) invalidResponse()
      result.size = entry.size
    }
    return result
  })
  return { sha, entries, truncated: data.truncated }
}

function fileBytes(data, entry) {
  if (!object(data) || typeof data.content !== 'string' || data.encoding !== 'base64') invalidResponse()
  if (commitSha(data.sha) !== entry.sha || data.size !== entry.size) {
    fail('SOURCE_MISMATCH', 'GitHub blob identity or size did not match the pinned tree')
  }
  const encoded = data.content.replace(/[\r\n]/g, '')
  if (encoded.length !== 4 * Math.ceil(entry.size / 3)
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) invalidResponse()
  const bytes = Buffer.from(encoded, 'base64')
  if (bytes.length !== entry.size || bytes.toString('base64') !== encoded) invalidResponse()
  const actual = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex')
  if (actual !== entry.sha) fail('SOURCE_MISMATCH', 'GitHub blob content did not match its Git SHA')
  let content
  try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) } catch {
    fail('BINARY_FILE', 'Source file is binary or is not valid UTF-8 text')
  }
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(content)) {
    fail('BINARY_FILE', 'Source file contains binary control bytes')
  }
  return bytes
}

function fatalOptional(error) {
  // A cancellation, deadline, or rate limit must stop the whole operation, not
  // disappear into an optional-preview result followed by more HTTP traffic.
  return ['ABORTED', 'TIMED_OUT'].includes(error.code)
    || (error.code === 'HTTP_ERROR' && [403, 429].includes(error.status))
}

function byPath(a, b) {
  return a.path.split('/').length - b.path.split('/').length
    || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
}

function publicRepository(value, expected) {
  if (!object(value)) invalidResponse()
  if (value.private === true || (value.visibility !== undefined && value.visibility !== 'public')) {
    fail('NOT_PUBLIC', 'Only public GitHub repository sources are supported')
  }
  if (value.private !== false || typeof value.full_name !== 'string') invalidResponse()
  let name
  try { name = repositoryName(value.full_name) } catch { invalidResponse() }
  if (name !== value.full_name) invalidResponse()
  if (expected && name.toLowerCase() !== expected.toLowerCase()) {
    fail('SOURCE_MISMATCH', 'GitHub repository identity did not match the requested source')
  }
  const number = value => value === undefined || value === null ? null
    : Number.isSafeInteger(value) && value >= 0 ? value : invalidResponse()
  const boolean = value => value === undefined || value === null ? null
    : typeof value === 'boolean' ? value : invalidResponse()
  if (value.topics !== undefined && (!Array.isArray(value.topics) || value.topics.length > 100)) invalidResponse()
  return {
    trust: TRUST, repository: name, url: `https://github.com/${name}`, private: false,
    description: optionalText(value.description),
    default_branch: optionalText(value.default_branch, 1024),
    language: optionalText(value.language, 128),
    topics: (value.topics || []).map(value => optionalText(value, 128)),
    stargazers_count: number(value.stargazers_count), forks_count: number(value.forks_count),
    fork: boolean(value.fork), archived: boolean(value.archived), disabled: boolean(value.disabled),
    created_at: optionalText(value.created_at, 64), updated_at: optionalText(value.updated_at, 64),
    pushed_at: optionalText(value.pushed_at, 64),
    license: value.license == null ? null : licenseFacts(value.license),
    license_scope: 'repository_metadata_not_pinned_to_commit',
  }
}

function active(signal) {
  if (signal?.aborted) fail('ABORTED', 'GitHub source request was cancelled')
}

function executionSignal(execution) {
  const signal = execution?.signal
  if (signal !== undefined && !(signal instanceof AbortSignal)) {
    fail('INVALID_ARGUMENT', 'execution.signal must be an AbortSignal')
  }
  active(signal)
  return signal
}

function discard(body) {
  if (!body || body.locked) return
  try { void body.cancel().catch(() => {}) } catch { /* Best-effort cleanup is never an unbounded wait. */ }
}

async function abortable(promise, signal) {
  active(signal)
  let abort
  try {
    return await Promise.race([promise, new Promise((resolve, reject) => {
      abort = () => reject(new GitHubSourceError('ABORTED', 'GitHub source request was cancelled'))
      signal.addEventListener('abort', abort, { once: true })
      if (signal.aborted) abort()
    })])
  } finally { signal.removeEventListener('abort', abort) }
}

async function boundedBytes(body, max, signal) {
  if (!body) fail('EMPTY_RESPONSE', 'GitHub returned no response body')
  const reader = body.getReader()
  // A contiguous growing buffer also bounds overhead for millions of tiny
  // chunks; accumulating a Buffer per network chunk would not do so.
  let buffer = Buffer.allocUnsafe(Math.min(max, 65536))
  let size = 0
  try {
    while (true) {
      active(signal)
      const { done, value } = await abortable(reader.read(), signal)
      active(signal)
      if (done) break
      if (!(value instanceof Uint8Array) || !value.byteLength) invalidResponse()
      if (value.byteLength > max - size) {
        fail('RESPONSE_TOO_LARGE', `GitHub response exceeds the configured ${max}-byte limit`)
      }
      if (size + value.byteLength > buffer.length) {
        const grown = Buffer.allocUnsafe(Math.min(max, Math.max(size + value.byteLength, buffer.length * 2)))
        buffer.copy(grown, 0, 0, size)
        buffer = grown
      }
      buffer.set(value, size)
      size += value.byteLength
    }
    if (!size) fail('EMPTY_RESPONSE', 'GitHub returned an empty response body')
    return buffer.subarray(0, size)
  } finally {
    void reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

function rateFacts(headers) {
  const number = name => {
    const value = headers.get(name)
    return value && /^\d{1,16}$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null
  }
  const resource = headers.get('x-ratelimit-resource')
  return {
    limit: number('x-ratelimit-limit'), remaining: number('x-ratelimit-remaining'),
    used: number('x-ratelimit-used'), reset_epoch_seconds: number('x-ratelimit-reset'),
    resource: ['core', 'search', 'code_search'].includes(resource) ? resource : null,
  }
}

function httpFailure(response) {
  const rate_limit = rateFacts(response.headers)
  const retry = response.headers.get('retry-after') || ''
  const retry_after_seconds = /^\d{1,9}$/.test(retry) ? Number(retry) : null
  const status = response.status
  return new GitHubSourceError('HTTP_ERROR',
    `GitHub HTTP ${status}; ${status === 403 || status === 429
      ? 'check rate limits and retry only after the indicated reset'
      : status === 404 ? 'the public source or reference was not found'
        : status === 422 ? 'check the search query or reference'
          : 'the source request was not completed'}`, {
      status, rate_limit, retry_after_seconds,
      retryable: status === 429 || status >= 500 || (status === 403 && (rate_limit.remaining === 0 || retry_after_seconds !== null)),
    })
}

/**
 * Anonymous, read-only public GitHub REST sources. No environment credentials,
 * repository code execution, dependency installation, or capability/licensing
 * decisions. Remote content is data and must never be promoted to instructions.
 *
 * Node >=24.9; GitHub REST API 2026-03-10. Protocol references:
 * https://docs.github.com/en/rest/search/search#search-repositories
 * https://docs.github.com/en/rest/commits/commits#get-a-commit
 * https://docs.github.com/en/rest/git/trees#get-a-tree
 * https://docs.github.com/en/rest/git/blobs#get-a-blob
 * https://docs.github.com/en/rest/licenses/licenses#get-the-license-for-a-repository
 */
export class GitHubSources {
  #fetch

  constructor({ requestTimeoutMs = 30000, maxResponseBytes = 4 * 1024 * 1024 } = {}, { fetchImpl = globalThis.fetch } = {}) {
    this.requestTimeoutMs = integer(requestTimeoutMs, 'requestTimeoutMs', 1, 300000)
    this.maxResponseBytes = integer(maxResponseBytes, 'maxResponseBytes', 1024, 16 * 1024 * 1024)
    if (typeof fetchImpl !== 'function') fail('INVALID_ARGUMENT', 'fetchImpl must be a fetch function')
    this.#fetch = fetchImpl
  }

  async #request(path, externalSignal) {
    active(externalSignal)
    const url = new URL(path, API)
    const timeout = new AbortController()
    const signal = AbortSignal.any([timeout.signal, externalSignal].filter(Boolean))
    const timer = setTimeout(() => timeout.abort(), this.requestTimeoutMs)
    let response
    try {
      const pending = Promise.resolve().then(() => {
        active(signal)
        return this.#fetch(url, {
          method: 'GET', credentials: 'omit', redirect: 'manual', referrerPolicy: 'no-referrer',
          headers: { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION, 'User-Agent': 'dsh-public-github-sources' },
          signal,
        })
      })
      // A custom fetch may ignore abort and resolve late. Release its body too.
      void pending.then(value => { if (signal.aborted) discard(value?.body) }, () => {})
      response = await abortable(pending, signal)
      active(signal)
      if (response.redirected || (response.status >= 300 && response.status < 400)) {
        fail('REDIRECT_REJECTED', 'GitHub source redirects are not followed; use the canonical repository', { status: response.status })
      }
      if (!response.ok) throw httpFailure(response)
      if (!response.body) fail('EMPTY_RESPONSE', 'GitHub returned no response body')
      if (!/^application\/(?:json|vnd\.github(?:\.[a-z-]+)?\+json)(?:\s*;|$)/i.test(response.headers.get('content-type') || '')) {
        invalidResponse()
      }
      const length = response.headers.get('content-length')
      if (length && /^\d+$/.test(length) && BigInt(length) > BigInt(this.maxResponseBytes)) {
        fail('RESPONSE_TOO_LARGE', `GitHub response exceeds the configured ${this.maxResponseBytes}-byte limit`)
      }
      const bytes = await boundedBytes(response.body, this.maxResponseBytes, signal)
      active(signal)
      let data
      try { data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) } catch { invalidResponse() }
      return {
        data,
        source: {
          trust: TRUST, url: url.href, fetched_at: new Date().toISOString(),
          response_sha256: sha256(bytes), http_status: response.status, rate_limit: rateFacts(response.headers),
        },
      }
    } catch (error) {
      const facts = response ? { status: response.status, rate_limit: rateFacts(response.headers) } : {}
      if (externalSignal?.aborted) fail('ABORTED', 'GitHub source request was cancelled', facts)
      if (timeout.signal.aborted) fail('TIMED_OUT', 'GitHub source request timed out', { ...facts, retryable: true })
      if (error instanceof GitHubSourceError) throw Object.assign(error, facts)
      fail('NETWORK_ERROR', 'GitHub source transport failed; check connectivity and retry explicitly', { ...facts, retryable: true })
    } finally {
      clearTimeout(timer)
      discard(response?.body)
    }
  }

  async search({ query, limit = 5 } = {}, execution) {
    const signal = executionSignal(execution)
    text(query, 'query', 2048)
    integer(limit, 'limit', 1, 100)
    const parameters = new URLSearchParams({ q: query, per_page: String(limit), page: '1' })
    const { data, source } = await this.#request(`/search/repositories?${parameters}`, signal)
    active(signal)
    if (!object(data) || !Array.isArray(data.items) || !Number.isSafeInteger(data.total_count)
      || data.total_count < data.items.length || typeof data.incomplete_results !== 'boolean') invalidResponse()
    const repositories = data.items.slice(0, limit).map(value => ({ ...publicRepository(value), source }))
    const incomplete = data.incomplete_results || (!repositories.length && data.total_count > 0)
    return {
      trust: TRUST, status: incomplete ? 'incomplete' : repositories.length ? 'found' : 'not_found',
      query, repositories, total_count: data.total_count, incomplete_results: data.incomplete_results,
      truncated: incomplete || data.total_count > repositories.length,
      source, fetched_at: source.fetched_at,
    }
  }

  async #snapshot(repository, ref, signal) {
    const repo = await this.#request(`/repos/${repository}`, signal)
    active(signal)
    const metadata = { ...publicRepository(repo.data, repository), source: repo.source }
    repository = metadata.repository
    let resolvedRef = ref
    if (resolvedRef === undefined) {
      try { resolvedRef = refName(metadata.default_branch) } catch { invalidResponse() }
    }
    // Ask for one changed file, not a potentially huge multi-file commit diff.
    const response = await this.#request(`/repos/${repository}/commits/${encodeURIComponent(resolvedRef)}?per_page=1`, signal)
    active(signal)
    if (!object(response.data) || !object(response.data.commit) || !object(response.data.commit.tree)) invalidResponse()
    const commit = commitSha(response.data.sha)
    if (SHA.test(resolvedRef) && commit !== resolvedRef.toLowerCase()) {
      fail('SOURCE_MISMATCH', 'GitHub commit did not match the requested fixed revision')
    }
    return {
      repository, ref: resolvedRef, metadata, commit, tree_sha: commitSha(response.data.commit.tree.sha),
      source: response.source, signal, trees: new Map(),
    }
  }

  async #tree(snapshot, sha) {
    active(snapshot.signal)
    if (!snapshot.trees.has(sha)) {
      const response = await this.#request(`/repos/${snapshot.repository}/git/trees/${sha}`, snapshot.signal)
      snapshot.trees.set(sha, { ...treeData(response.data, sha), source: response.source })
    }
    return snapshot.trees.get(sha)
  }

  async #entry(snapshot, path) {
    const parts = filePath(path)
    let sha = snapshot.tree_sha
    for (let i = 0; i < parts.length; i++) {
      const tree = await this.#tree(snapshot, sha)
      const entry = tree.entries.find(entry => entry.path === parts[i])
      if (!entry) fail(tree.truncated ? 'TREE_TRUNCATED' : 'PATH_NOT_FOUND',
        tree.truncated ? 'The bounded GitHub tree cannot establish whether this path exists' : 'File path was not found at the pinned commit')
      if (entry.mode === '120000') fail('SYMLINK_REJECTED', 'Symbolic links and paths through symbolic links are not source files')
      if (entry.mode === '160000') fail('SUBMODULE_REJECTED', 'Submodules are not followed; inspect their public repository separately')
      if (i === parts.length - 1) {
        if (entry.type !== 'blob') fail('NOT_A_FILE', 'Requested source path is not a regular file')
        return entry
      }
      if (entry.type !== 'tree') fail('NOT_A_DIRECTORY', 'A source path component is not a directory')
      sha = entry.sha
    }
  }

  async #readFile(snapshot, path, offset = 0, limit = PREVIEW_BYTES) {
    active(snapshot.signal)
    const entry = await this.#entry(snapshot, path)
    if (entry.size > Math.min(FILE_BYTES, this.maxResponseBytes)) {
      fail('FILE_TOO_LARGE', `Source file exceeds the ${Math.min(FILE_BYTES, this.maxResponseBytes)}-byte text-file limit`)
    }
    if (!entry.size) fail('EMPTY_FILE', 'Source file is empty at the pinned commit')
    const response = await this.#request(`/repos/${snapshot.repository}/git/blobs/${entry.sha}`, snapshot.signal)
    active(snapshot.signal)
    const bytes = fileBytes(response.data, entry)
    const continuationByte = index => (bytes[index] & 0xc0) === 0x80
    if (offset >= bytes.length || continuationByte(offset)) {
      fail('INVALID_RANGE', 'offset must point to a UTF-8 character boundary before the end of the file')
    }
    let end = Math.min(bytes.length, offset + limit)
    while (end < bytes.length && end > offset && continuationByte(end)) end--
    if (end === offset) fail('INVALID_RANGE', 'limit is too small to contain the next complete UTF-8 character')
    const next_offset = end < bytes.length ? end : null
    const { repository, commit } = snapshot
    return {
      trust: TRUST, status: 'read', repository, commit, source_sha: commit, path,
      url: `https://github.com/${repository}/blob/${commit}/${filePath(path).map(encodeURIComponent).join('/')}`,
      fetched_at: response.source.fetched_at, source: response.source,
      content: bytes.subarray(offset, end).toString('utf8'), encoding: 'utf-8',
      sha256: sha256(bytes), blob_sha: entry.sha,
      offset, limit, end_offset: end, returned_bytes: end - offset, total_bytes: bytes.length,
      offset_unit: 'utf8_bytes', truncated: offset !== 0 || end < bytes.length, next_offset,
      next: next_offset === null ? null : {
        method: 'read', arguments: { repository, commit, path, offset: next_offset, limit },
      },
    }
  }

  /**
   * Read a regular UTF-8 file at an exact commit. Offsets/limits count bytes;
   * page ends retreat to a character boundary, and hashes cover the whole file.
   * This method works without inspect, including paths outside its capped index.
   */
  async read({ repository, commit, path, offset = 0, limit = PREVIEW_BYTES } = {}, execution) {
    const signal = executionSignal(execution)
    repository = repositoryName(repository)
    commit = commitSha(commit, true)
    filePath(path)
    integer(offset, 'offset', 0, Number.MAX_SAFE_INTEGER)
    integer(limit, 'limit', 1, 65536)
    const snapshot = await this.#snapshot(repository, commit, signal)
    return this.#readFile(snapshot, path, offset, limit)
  }

  async #preview(snapshot, path, stage, errors) {
    try { return await this.#readFile(snapshot, path) } catch (error) {
      if (fatalOptional(error)) throw error
      errors.push({ ...error.toJSON(), stage, path })
      return null
    }
  }

  async #license(snapshot, entries, errors, indexTruncated) {
    const candidates = entries.filter(entry => entry.type === 'blob'
      && /^(?:licen[cs]e|copying|unlicense)(?:[._-].*)?$/i.test(entry.path.split('/').at(-1))).sort(byPath)
    const result = {
      trust: TRUST, status: 'not_detected', ...licenseFacts(null), assessment: 'not_performed',
      file: null, source: null, candidate_paths: candidates.slice(0, 20).map(entry => entry.path),
      candidate_paths_truncated: indexTruncated || candidates.length > 20,
    }
    let observation
    try {
      const response = await this.#request(`/repos/${snapshot.repository}/license?ref=${snapshot.commit}`, snapshot.signal)
      observation = response.data
      if (!object(observation)) invalidResponse()
      try { filePath(observation.path) } catch { invalidResponse() }
      commitSha(observation.sha)
      Object.assign(result, licenseFacts(observation.license), { status: 'detected', source: response.source })
    } catch (error) {
      if (fatalOptional(error)) throw error
      if (error.code === 'HTTP_ERROR' && error.status === 404) {
        result.error = error.toJSON() // "not detected" is not "no license exists".
      } else {
        errors.push({ ...error.toJSON(), stage: 'license' })
        result.status = 'unavailable'
      }
      observation = null
    }
    const path = observation?.path || candidates[0]?.path
    if (path) {
      result.file = await this.#preview(snapshot, path, 'license_file', errors)
      if (!result.file) result.status = 'unavailable'
      else if (observation && (result.file.blob_sha !== observation.sha.toLowerCase() || result.file.total_bytes !== observation.size)) {
        const error = new GitHubSourceError('SOURCE_MISMATCH', 'License metadata did not match the actual file at the pinned commit')
        errors.push({ ...error.toJSON(), stage: 'license_file' })
        Object.assign(result, licenseFacts(null), { status: 'unavailable', file: null })
      }
    }
    return result
  }

  /**
   * Resolve a branch/tag once, then inspect only that immutable source. Preview
   * limits: one README, eight manifests, one actual license file, 12,000 bytes
   * each; at most 2,000 index entries. No model or license-authorization verdict.
   */
  async inspect({ repository, ref } = {}, execution) {
    const signal = executionSignal(execution)
    repository = repositoryName(repository)
    if (ref !== undefined) refName(ref)
    const snapshot = await this.#snapshot(repository, ref, signal)
    const errors = []
    let index
    let index_scope = 'recursive'
    try {
      const response = await this.#request(`/repos/${snapshot.repository}/git/trees/${snapshot.tree_sha}?recursive=1`, signal)
      index = { ...treeData(response.data, snapshot.tree_sha, true), source: response.source }
    } catch (error) {
      if (error.code !== 'RESPONSE_TOO_LARGE') throw error
      errors.push({ ...error.toJSON(), stage: 'tree_index' })
      index = await this.#tree(snapshot, snapshot.tree_sha)
      index_scope = 'root'
    }
    active(signal)
    const treeTruncated = index_scope === 'root' || index.truncated || index.entries.length > INDEX_ENTRIES
    const readmes = index.entries.filter(entry => entry.type === 'blob'
      && /^(?:(?:\.github|docs)\/)?readme(?:\.(?:md|markdown|rst|txt|adoc|org|rdoc|mdown))?$/i.test(entry.path)).sort(byPath)
    const readme = readmes.length ? await this.#preview(snapshot, readmes[0].path, 'readme', errors) : null
    const candidates = index.entries.filter(entry => entry.type === 'blob'
      && entry.path.split('/').length <= 3 && MANIFESTS.has(entry.path.split('/').at(-1).toLowerCase())).sort(byPath)
    const manifests = []
    for (const candidate of candidates.slice(0, MANIFEST_FILES)) {
      const file = await this.#preview(snapshot, candidate.path, 'manifest', errors)
      if (file) manifests.push(file)
    }
    const license = await this.#license(snapshot, index.entries, errors, index.truncated || index_scope === 'root')
    active(signal)
    const manifests_truncated = candidates.length > MANIFEST_FILES || index.truncated || index_scope === 'root'
    return {
      trust: TRUST, status: errors.length ? 'partial' : 'inspected',
      repository: snapshot.repository, url: `https://github.com/${snapshot.repository}/tree/${snapshot.commit}`,
      ref: snapshot.ref, commit: snapshot.commit, source_sha: snapshot.commit,
      fetched_at: new Date().toISOString(), source: snapshot.source, metadata: snapshot.metadata,
      license, readme, readme_status: readmes.length ? readme ? 'read' : 'unavailable' : 'not_found_in_index',
      manifests, manifests_truncated,
      tree: {
        trust: TRUST, sha: snapshot.tree_sha, entries: index.entries.slice(0, INDEX_ENTRIES),
        truncated: treeTruncated, api_truncated: index.truncated, index_scope,
        received_entries: index.entries.length, source: index.source,
        continuation: treeTruncated ? {
          method: 'GET', url: `${API}/repos/${snapshot.repository}/git/trees/${snapshot.tree_sha}`,
          strategy: 'Fetch non-recursive trees by SHA, then one subtree at a time; do not infer absence from a truncated index.',
        } : null,
      },
      truncated: treeTruncated || manifests_truncated || Boolean(readme?.truncated) || Boolean(license.file?.truncated) || errors.length > 0
        || manifests.some(file => file.truncated),
      errors,
      continuation: { method: 'read', arguments: { repository: snapshot.repository, commit: snapshot.commit }, required: ['path'] },
    }
  }
}
