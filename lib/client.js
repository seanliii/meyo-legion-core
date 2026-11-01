import { createHash, randomUUID } from 'node:crypto'
import { mkdir, open, rename, unlink } from 'node:fs/promises'
import { homedir } from 'node:os'
import { basename, isAbsolute, join, resolve } from 'node:path'
import { parseSSE } from './sse.js'
import { TaskStore, digest } from './store.js'
import { parseJson } from './json.js'
import { stripDataUrls } from './redaction.js'
import { credentialFilePath, readCredentialFile } from './credentials.js'
import { teamContext, validateTeamProfile } from './team.js'

const EXPERIENCE = 'not_performed_by_plugin'
const PRIVATE_FIELD = /^(?:authorization|cookies?|password|secrets?|(?:api|access|refresh|share|personal|bearer)[_-]?(?:key|token)|token|share_url|share_id|reasoning(?:_content)?|chain_of_thought|thinking|environment|env|project_context|native_checkpoint|mailboxes|turns|attachments)$/i
const EXPORT_PATH = /^\/(?:outputs|home-outputs|auto-outputs|desktop-outputs|media)\//
const PRIVATE_QUERY = /^(?:share|token|key|auth|authorization|password|secret|signature|sig|(?:api|access|refresh|share|personal|bearer)[_-]?(?:key|token)|x-amz-(?:credential|signature|security-token))$/i

function text(value, label, max = 100000) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new Error(`${label} must be non-empty text (at most ${max} characters)`)
  }
  return value
}

function integer(value, fallback, label, min, max) {
  const result = value ?? fallback
  if (!Number.isSafeInteger(result) || result < min || result > max) {
    throw new Error(`${label} must be an integer between ${min} and ${max}`)
  }
  return result
}

export function validateCandidateHarnesses(value) {
  if (value === undefined) return undefined
  if (!Array.isArray(value)) throw new Error('candidateHarnesses must be an array of harness identifiers')
  const candidates = [...value]
  if (candidates.some(key => typeof key !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(key))
    || new Set(candidates).size !== candidates.length) {
    throw new Error('candidateHarnesses must contain unique, non-empty harness identifiers using letters, digits, ".", "_" or "-"')
  }
  // Preserve the user's order and take a snapshot, rather than silently fixing
  // duplicates or letting later mutation change an in-flight reservation.
  return Object.freeze(candidates)
}

function serviceUrl(value) {
  let url
  try { url = new URL(value) } catch { throw new Error('Invalid Legion service URL') }
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) {
    throw new Error('Legion URL must use HTTPS; HTTP is allowed only on loopback')
  }
  if (url.username || url.password) throw new Error('Credentials must not be embedded in the service URL')
  if (url.search || url.hash) throw new Error('Service URL must not contain a query or fragment')
  return url.href.replace(/\/+$/, '')
}

class HttpError extends Error {
  constructor(status, detail) {
    super(`Legion HTTP ${status}${detail ? `: ${detail}` : ''}`)
    this.name = 'LegionHttpError'
    this.status = status
  }
}

// A server/transport timeout can happen after work was accepted. It is not a
// rejection receipt and must never trigger an automatic second submission.
const isRefusal = error => error instanceof HttpError
  && error.status >= 400 && error.status < 500 && error.status !== 408

/** Bounded body reads; cancellation closes the local HTTP reader, not the remote job. */
async function readBytes(body, max, signal, consume) {
  if (!body) throw new Error('Legion returned an empty response body')
  const reader = body.getReader()
  const abort = () => { void reader.cancel().catch(() => {}) }
  let size = 0
  signal.addEventListener('abort', abort, { once: true })
  try {
    while (true) {
      signal.throwIfAborted()
      const { done, value } = await reader.read()
      signal.throwIfAborted()
      if (done) return size
      size += value.byteLength
      if (size > max) throw new Error(`Legion response exceeds the configured ${max}-byte limit`)
      await consume(value)
    }
  } finally {
    signal.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

/** An HTTP connector and recovery index. It does not plan, run or grade delegated work. */
export class LegionClient {
  #usedCredentials = new Set()
  #unsentErrors = new WeakSet()
  #identity
  #store

  constructor(config = {}, env = process.env) {
    this.env = env
    this.baseUrl = serviceUrl(config.baseUrl || env.LEGION_BASE_URL || 'http://127.0.0.1:8000')
    this.tokenEnv = config.tokenEnv || 'LEGION_PERSONAL_TOKEN'
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(this.tokenEnv)) throw new Error('tokenEnv must be an environment variable name')
    if (Object.hasOwn(config, 'tokenFile')) this.tokenFile = credentialFilePath(config.tokenFile)
    this.stateDirectory = resolve(config.stateDirectory || env.DSH_LEGION_STATE_DIR
      || join(env.DSH_HOME || join(homedir(), '.dsh'), 'legion-state'))
    this.requestTimeoutMs = integer(config.requestTimeoutMs, 30000, 'requestTimeoutMs', 100, 300000)
    this.maxResponseBytes = integer(config.maxResponseBytes, 4 * 1024 * 1024, 'maxResponseBytes', 1024, 32 * 1024 * 1024)
    this.maxArtifactBytes = integer(config.maxArtifactBytes, 50 * 1024 * 1024, 'maxArtifactBytes', 1, 1024 * 1024 * 1024)
    this.maxEvents = integer(config.maxEvents, 100, 'maxEvents', 1, 500)
    this.projectBindingFile = config.projectBindingFile
    this.candidateHarnesses = validateCandidateHarnesses(config.candidateHarnesses)
    this.teamProfile = validateTeamProfile(config.teamProfile)
    this.lifecycleSignal = config.lifecycleSignal
    this.controller = new AbortController()
  }

  get store() {
    if (!this.#store) this.#boundToken()
    // Keep the original store even after disposal: an interrupted operation must
    // finish its receipt in the old index, never in a newly configured identity.
    return this.#store
  }

  #boundToken() {
    const token = this.token()
    const identity = digest(JSON.stringify(['legion-service-identity-v1', this.baseUrl, token]))
    if (this.#identity !== undefined && this.#identity !== identity) {
      this.dispose()
      throw new Error('Legion credential changed; reload the plugin before using the new identity. Old task references are not transferred.')
    }
    if (!this.#store) {
      this.#identity = identity
      // Neither the token nor its file path is persisted. Legacy service-only
      // indexes deliberately do not enter this authenticated recovery namespace.
      this.#store = new TaskStore(this.stateDirectory, identity)
    }
    return token
  }

  dispose() {
    this.controller.abort(new DOMException('Legion plugin disposed; remote tasks were not cancelled', 'AbortError'))
  }

  scope(execution) {
    this.controller.signal.throwIfAborted()
    this.lifecycleSignal?.throwIfAborted()
    execution?.signal?.throwIfAborted()
    text(execution?.sessionId, 'DSH session', 1024)
    this.#boundToken()
    return execution
  }

  token() {
    if (this.tokenFile !== undefined) {
      const token = readCredentialFile(this.tokenFile)
      this.#usedCredentials.add(token)
      return token
    }
    const token = this.env[this.tokenEnv]
    if (typeof token !== 'string' || !token.trim()) {
      throw new Error(`Set ${this.tokenEnv} in the DSH host environment to your personal Legion bearer token. Do not paste it into chat.`)
    }
    if (/[\r\n]/.test(token)) throw new Error('The configured Legion credential contains an invalid newline')
    this.#usedCredentials.add(token.trim())
    return token.trim()
  }

  projectBinding() {
    if (!this.projectBindingFile) return Promise.resolve(null)
    return (async () => {
      if (!isAbsolute(this.projectBindingFile)) throw new Error('projectBindingFile must be an absolute user-configured path')
      const handle = await open(this.projectBindingFile, 'r')
      let raw
      try {
        if ((await handle.stat()).size > 16384) throw new Error('Project binding file is too large')
        raw = await handle.readFile('utf8')
      } finally { await handle.close() }
      const binding = parseJson(raw, 'Project binding file')
      if (!binding || typeof binding !== 'object' || Array.isArray(binding)
        || Object.keys(binding).some(key => key !== 'workspace_root')
        || typeof binding.workspace_root !== 'string' || !isAbsolute(binding.workspace_root)) {
        throw new Error('Project binding must contain only workspace_root, an absolute directory already authorized by the Legion operator')
      }
      return binding
    })()
  }

  redactText(value) {
    const current = typeof this.env[this.tokenEnv] === 'string' ? this.env[this.tokenEnv].trim() : ''
    const secrets = new Set([...this.#usedCredentials, current].filter(Boolean))
    let result = stripDataUrls(value)
    for (const secret of [...secrets].sort((a, b) => b.length - a.length)) {
      result = result.split(secret).join('[redacted]')
      result = result.split(encodeURIComponent(secret)).join('[redacted]')
    }
    result = result.replace(/\bBearer\s+[A-Za-z0-9._~+\/=-]+/gi, 'Bearer [redacted]')
    // A share URL is an access capability even when its field is simply "url".
    // Preserve public query parameters; never use this rewritten text to authorize a download.
    result = result.replace(/([?&#])([^?&#=\s"'<>]+)=([^&#\s"'<>]*)/g, (match, separator, key) => {
      let decoded
      try { decoded = decodeURIComponent(key) } catch { return match }
      return PRIVATE_QUERY.test(decoded) ? `${separator}${key}=[redacted]` : match
    })
    return result.replace(/\b(https?:\/\/)[^/\s"'<>?#@]+@/gi, '$1[redacted]@')
  }

  clean(value, projection = { omitted_fields: [], truncated_fields: [] }, path = '$', depth = 0) {
    path = this.redactText(path)
    if (depth > 12) { projection.truncated_fields.push(path); return '[depth limit]' }
    if (typeof value === 'string') {
      let result = this.redactText(value)
      if (result.length > 16000) {
        projection.truncated_fields.push(path)
        result = `${result.slice(0, 16000)}\n[truncated; original ${value.length} characters]`
      }
      return result
    }
    if (Array.isArray(value)) return value.map((x, i) => this.clean(x, projection, `${path}[${i}]`, depth + 1))
    if (value && typeof value === 'object') {
      const entries = []
      for (const [key, item] of Object.entries(value)) {
        const safeKey = this.redactText(key)
        if (safeKey !== key || PRIVATE_FIELD.test(key) || ['__proto__', 'constructor', 'prototype'].includes(key)) {
          // Omit sensitive keys rather than merging different keys into one placeholder.
          projection.omitted_fields.push(`${path}.${safeKey}`)
        } else entries.push([key, this.clean(item, projection, `${path}.${key}`, depth + 1)])
      }
      return Object.fromEntries(entries)
    }
    return value
  }

  #safeRequestError(error) {
    if (error instanceof HttpError) return error
    const message = this.redactText(String(error?.message || error || 'Legion request failed')).slice(0, 500)
    const name = this.redactText(typeof error?.name === 'string' ? error.name : 'Error').slice(0, 100)
    if (error instanceof DOMException) return new DOMException(message, name)
    // Do not retain an upstream stack, cause or custom diagnostics containing credentials.
    const safe = new Error(message)
    safe.name = name
    if (typeof error?.code === 'string') safe.code = this.redactText(error.code).slice(0, 100)
    else if (Number.isSafeInteger(error?.code)) safe.code = error.code
    return safe
  }

  async #claimUnsentRetry(ref, receiptName) {
    const name = `unsent-${receiptName}`
    const proof = await this.store.readObservation(ref, name)
    if (typeof proof?.retry_key !== 'string' || !/^[a-f0-9]{32}$/.test(proof.retry_key)) return false
    // Reuse the store's atomic claim across clients/processes; never release an
    // attempted key or infer "unsent" from an absent/uncertain acknowledgement.
    if (!await this.store.reserveOperation(ref, name, proof.retry_key, ref.input_digest)) return false
    await this.store.writeObservation(ref, name, null)
    return true
  }

  #publishUnsentRetry(ref, receiptName) {
    // Publish last, after the failure receipt, so an older call cannot overwrite
    // the result of a retry which has already claimed this proof.
    return this.store.writeObservation(ref, `unsent-${receiptName}`, {
      retry_key: randomUUID().replaceAll('-', ''),
    })
  }

  async request(route, execution, { body, consume } = {}) {
    let response, timer
    let enteredFetch = false
    try {
      this.scope(execution)
      const token = this.#boundToken()
      const timeout = new AbortController()
      timer = setTimeout(() => timeout.abort(new DOMException('Legion HTTP wait timed out; check the original task before retrying', 'TimeoutError')), this.requestTimeoutMs)
      timer.unref?.()
      const signal = AbortSignal.any([this.controller.signal, execution.signal, this.lifecycleSignal, timeout.signal].filter(Boolean))
      const url = route instanceof URL ? route : new URL(`${this.baseUrl}${route}`)
      const init = {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json, text/event-stream',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error',
        signal,
      }
      signal.throwIfAborted()
      enteredFetch = true
      response = await fetch(url, init)
      if (!response.ok) {
        // Keep an actionable error, but never echo a token or an unbounded HTML page.
        let detail = ''
        if (response.headers.get('content-type')?.includes('application/json')) {
          try {
            const chunks = []
            await readBytes(response.body, Math.min(this.maxResponseBytes, 16384), signal, x => chunks.push(Buffer.from(x)))
            const data = JSON.parse(Buffer.concat(chunks).toString('utf8'))
            detail = this.clean(String(data.reason || data.error || '')).slice(0, 500)
          } catch { /* HTTP status remains authoritative if its body is unusable. */ }
        }
        throw new HttpError(response.status, detail)
      }
      if (consume) return await consume(response, signal)
      if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Legion returned a non-JSON response; check the configured service endpoint')
      }
      const chunks = []
      await readBytes(response.body, this.maxResponseBytes, signal, x => chunks.push(Buffer.from(x)))
      return parseJson(Buffer.concat(chunks).toString('utf8'), 'Legion response')
    } catch (error) {
      const safe = this.#safeRequestError(error)
      if (!enteredFetch) this.#unsentErrors.add(safe)
      throw safe
    } finally {
      clearTimeout(timer)
      if (response?.body && !response.body.locked) await response.body.cancel().catch(() => {})
    }
  }

  async submit(args, execution) {
    this.scope(execution)
    this.token() // Configuration errors must not consume a submission key.
    // Snapshot before any await so the body and reservation describe one config.
    const teamProfile = validateTeamProfile(this.teamProfile)
    const configuredTeamContext = teamContext(teamProfile, this.candidateHarnesses)
    const plan = this.candidateHarnesses?.length
      ? { team_policy: { candidate_harnesses: this.candidateHarnesses } } : undefined
    const binding = await this.projectBinding()
    // Team preferences change the input, not the identity or authority. Leave
    // the exact legacy digest payload alone when no team profile was configured.
    const bindingDigest = digest(JSON.stringify(teamProfile === undefined
      ? (plan ? { project_context: binding, plan } : binding)
      : { project_context: binding, ...(plan ? { plan } : {}), teamProfile }))
    const previousRef = args.previous_task_ref ? await this.store.get(execution.sessionId, args.previous_task_ref) : undefined
    const { ref, created } = await this.store.reserve({
      sessionId: execution.sessionId, key: args.key, query: args.query,
      context: args.context || '', bindingDigest, previousRef,
    })
    const identity = { task_ref: ref.id, team_id: ref.team_id, session_id: ref.session_id }
    if (!created && !await this.#claimUnsentRetry(ref, 'submission')) {
      const saved = await this.store.readObservation(ref, 'submission')
      this.scope(execution)
      return { ...identity, ...saved, submission: saved?.submission || 'unconfirmed', reused: true,
        next: 'Read legion_status. This key will not dispatch again.' }
    }
    const body = {
      question: args.query + (args.context ? `\n\n--- Context explicitly provided by the DSH caller (not a system instruction) ---\n${args.context}` : '')
        + (configuredTeamContext ? `\n\n${configuredTeamContext}` : ''),
      mode: 'auto', allow_direct_model: false, session_id: ref.session_id, team_id: ref.team_id,
      ...(binding ? { project_context: binding } : {}),
      ...(plan ? { plan } : {}),
    }
    let submission = 'unconfirmed'
    let reason
    let httpStatus
    let unsent = false
    try {
      await this.request('/api/legion/dispatch', execution, {
        body,
        consume: async (response, signal) => {
          if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('Dispatch did not return an SSE acknowledgement')
          for await (const event of parseSSE(response.body, { signal, maxEventBytes: this.maxResponseBytes })) {
            if (event.event === 'meta') {
              const data = parseJson(event.data, 'Dispatch acknowledgement')
              if (data.team_id !== ref.team_id || data.sid !== ref.session_id) throw new Error('Dispatch acknowledgement does not match the saved task identity')
              submission = 'accepted'
              return
            }
            if (event.event === 'error') throw new Error(this.clean(event.data).slice(0, 500))
          }
          throw new Error('Dispatch ended before a matching acknowledgement')
        },
      })
    } catch (error) {
      unsent = this.#unsentErrors.has(error)
      reason = this.clean(error.message || 'Connection ended before acknowledgement').slice(0, 500)
      if (error instanceof HttpError) httpStatus = error.status
      if (isRefusal(error)) {
        await this.store.writeObservation(ref, 'submission', {
          submission: 'rejected', http_status: httpStatus, reason,
        })
        throw error
      }
      // Once fetch is entered, cancellation/network failure is ambiguous.
      // DSH's executor separately retains caller abort.
    }
    const receipt = { submission, ...(reason ? { reason } : {}),
      ...(httpStatus ? { http_status: httpStatus } : {}) }
    await this.store.writeObservation(ref, 'submission', receipt)
    if (unsent) await this.#publishUnsentRetry(ref, 'submission')
    return { ...identity, ...receipt,
      ...(unsent ? { request_sent: false } : {}),
      next: unsent ? 'No HTTP request was sent. Resolve the local error and retry with this same key.'
        : 'Read legion_status using this task_ref. Do not resubmit to reconnect.',
      remote_task_cancelled: false }
  }

  async list(execution) {
    this.scope(execution)
    const refs = await this.store.list(execution.sessionId)
    this.scope(execution)
    return { tasks: refs.map(ref => ({
      task_ref: ref.id, key: ref.key, team_id: ref.team_id, created_at: ref.created_at,
      ...(ref.previous_task_ref ? { previous_task_ref: ref.previous_task_ref } : {}),
    })), source: 'local_recovery_index_not_live_status' }
  }

  async runtime(args, execution) {
    const routes = {
      members: '/api/legion/harnesses',
      capabilities: '/api/legion/capabilities',
      catalog: '/api/legion/catalog',
    }
    const view = args.view ?? 'members'
    if (!Object.hasOwn(routes, view)) throw new Error('runtime view must be members, capabilities or catalog')
    const data = await this.request(routes[view], execution)
    if (!data || typeof data !== 'object') throw new Error('Legion runtime returned an unusable observation')
    const projection = { omitted_fields: [], truncated_fields: [] }
    return { view, source: this.redactText(`${this.baseUrl}${routes[view]}`), observed_at: new Date().toISOString(),
      runtime: this.clean(data, projection), projection,
      verification: 'service_observation_only_no_execution' }
  }

  artifact(entry, teamId) {
    if (!entry || typeof entry !== 'object' || typeof entry.url !== 'string') return null
    const hashes = [entry.sha256, entry.provenance?.sha256].filter(x => x != null)
    const hash = typeof hashes[0] === 'string' ? hashes[0].toLowerCase() : ''
    let reason
    let url
    try {
      // Never store or rewrite a credential-bearing address into a usable URL.
      if (this.clean(entry.url) !== entry.url
        || this.clean(decodeURIComponent(entry.url)) !== decodeURIComponent(entry.url)) {
        throw new Error('Artifact URL contains a configured credential or unsupported text')
      }
      url = new URL(entry.url, `${this.baseUrl}/`)
      const base = new URL(this.baseUrl)
      if (url.origin !== base.origin || url.username || url.password || url.search || url.hash) {
        throw new Error('Artifact URL is not an authorized same-origin export path')
      }
      const prefix = base.pathname.replace(/\/+$/, '')
      const exportPath = prefix && url.pathname.startsWith(`${prefix}/`)
        ? url.pathname.slice(prefix.length) : url.pathname
      if (!EXPORT_PATH.test(exportPath)) throw new Error('Artifact URL is not an authorized export path')
      // Root-relative exporter paths are relative to this service, not to a
      // reverse proxy's site root. Already-prefixed paths are not prefixed twice.
      url.pathname = `${prefix}${exportPath}`
    } catch { reason = 'Artifact URL is not an authorized same-origin export path' }
    if (!hashes.length || hashes.some(x => typeof x !== 'string' || !/^[a-f0-9]{64}$/i.test(x))) {
      reason = 'Artifact has no valid SHA-256 hash'
    } else if (hashes.some(x => x.toLowerCase() !== hash)) reason = 'Artifact has conflicting SHA-256 hashes'
    if (entry.team_id && entry.team_id !== teamId) reason = 'Artifact belongs to another team'
    if (entry.bytes != null && (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > this.maxArtifactBytes)) {
      reason = 'Artifact exceeds the configured download size or has invalid size metadata'
    }
    const name = typeof entry.name === 'string' ? this.clean(entry.name).slice(0, 256) : 'artifact'
    return {
      artifact_id: digest(JSON.stringify([reason ? entry.url : url.href, hash, name])).slice(0, 32), name,
      ...(typeof entry.kind === 'string' ? { kind: this.clean(entry.kind) } : {}),
      sha256: this.clean(hash), ...(entry.bytes == null ? {} : { bytes: this.clean(entry.bytes) }),
      download_eligible: !reason, ...(reason ? { download_reason: reason } : { url: url.href }),
      ...(entry.export_snapshot_id ? { export_snapshot_id: this.clean(String(entry.export_snapshot_id)) } : {}),
      ...(entry.provenance?.verification ? { service_verification: this.clean(String(entry.provenance.verification)) } : {}),
    }
  }

  /** Only project commander mail from status's identity-checked /team snapshot.
   * The explicit ID cursor selects a page; it never acknowledges model consumption.
   */
  commanderMailbox(team, after, projection) {
    const truncationStart = projection.truncated_fields.length
    const mailbox = {
      source: 'team.mailboxes.commander', trust: 'untrusted_source', recipient: 'commander',
      state: 'not_returned', messages: null, total_messages: null,
      after_message_id: after, next_message_id: null, more_messages: null, truncated: null,
      read_receipts_modified: false, model_consumption: 'not_observed',
    }
    const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
    const unavailable = path => ({ ...mailbox, diagnostic: { code: 'MAILBOX_NOT_RETURNED', path } })
    const invalid = path => {
      projection.omitted_fields.push(path)
      return { ...mailbox, state: 'invalid', diagnostic: { code: 'INVALID_MAILBOX', path } }
    }
    if (!Object.hasOwn(team, 'mailboxes')) return unavailable('$.mailboxes')
    if (!object(team.mailboxes)) return invalid('$.mailboxes')
    if (!Object.hasOwn(team.mailboxes, 'commander')) return unavailable('$.mailboxes.commander')
    const messages = team.mailboxes.commander
    if (!Array.isArray(messages)) return invalid('$.mailboxes.commander')
    mailbox.total_messages = messages.length
    const referenceFields = ['artifact_id', 'sha256', 'name', 'mime', 'bytes']
    const seen = new Set()
    for (const [index, message] of messages.entries()) {
      const path = `$.mailbox.messages[${index}]`
      if (!object(message) || !Number.isSafeInteger(message.id) || message.id < 1
        || seen.has(message.id) || typeof message.from !== 'string' || !message.from.trim()
        || message.to !== 'commander' || typeof message.text !== 'string'
        || !Number.isFinite(message.ts) || message.ts < 0
        || (Object.hasOwn(message, 'read_at') && message.read_at !== null
          && (!Number.isFinite(message.read_at) || message.read_at < 0))) return invalid(path)
      seen.add(message.id)
      if (Object.hasOwn(message, 'artifact_refs')) {
        if (!Array.isArray(message.artifact_refs)) return invalid(`${path}.artifact_refs`)
        for (const [refIndex, ref] of message.artifact_refs.entries()) {
          if (!object(ref) || referenceFields.some(key => Object.hasOwn(ref, key)
            && (key === 'bytes' ? !Number.isSafeInteger(ref[key]) || ref[key] < 0
              : typeof ref[key] !== 'string'))) return invalid(`${path}.artifact_refs[${refIndex}]`)
        }
      }
    }
    // Keep PRIVATE_FIELD's blanket mailbox exclusion elsewhere. Only this narrow
    // envelope is visible; artifact paths/payloads and arbitrary extra fields are not.
    const fields = (value, keys, path, separatelyProjected) => {
      for (const key of Object.keys(value)) {
        if (!keys.includes(key) && key !== separatelyProjected) {
          projection.omitted_fields.push(this.redactText(`${path}.${key}`))
        }
      }
      return this.clean(Object.fromEntries(keys.filter(key => Object.hasOwn(value, key))
        .map(key => [key, value[key]])), projection, path)
    }
    const pending = messages.filter(message => message.id > after).sort((a, b) => a.id - b.id)
    // Reuse the existing bounded status record limit for the independent mail page.
    const page = pending.slice(0, this.maxEvents)
    const more = pending.length > page.length
    if (more) projection.truncated_fields.push('$.mailbox.messages')
    const projected = page.map((message, index) => {
      const path = `$.mailbox.messages[${index}]`
      const value = fields(message, ['id', 'from', 'to', 'text', 'ts', 'read_at'], path, 'artifact_refs')
      const refs = message.artifact_refs || []
      // send_message stores long bodies as mail-ID.txt and only inlines a preview.
      // Do not follow a private path, or misrepresent that preview as the full body.
      const preview = refs.some(ref => ref.name === `mail-${message.id}.txt`)
      if (preview && !projection.truncated_fields.includes(`${path}.text`)) {
        projection.truncated_fields.push(`${path}.text`)
      }
      const refsTruncated = refs.length > this.maxEvents
      if (refsTruncated) projection.truncated_fields.push(`${path}.artifact_refs`)
      return {
        ...value, text_source: preview ? 'inline_preview' : 'inline',
        text_truncated: projection.truncated_fields.includes(`${path}.text`),
        artifact_refs: refs.slice(0, this.maxEvents).map((ref, refIndex) =>
          fields(ref, referenceFields, `${path}.artifact_refs[${refIndex}]`)),
        artifact_refs_truncated: refsTruncated,
      }
    })
    return {
      ...mailbox, state: 'available', messages: projected,
      next_message_id: page.at(-1)?.id ?? after, more_messages: more,
      truncated: projection.truncated_fields.length > truncationStart,
    }
  }

  async status(args, execution) {
    this.scope(execution)
    const ref = await this.store.get(execution.sessionId, args.task_ref)
    const after = integer(args.after_event_id, 0, 'after_event_id', 0, Number.MAX_SAFE_INTEGER)
    const afterMessage = integer(args.after_message_id, 0, 'after_message_id', 0, Number.MAX_SAFE_INTEGER)
    const query = `?team_id=${encodeURIComponent(ref.team_id)}`
    const team = await this.request(`/api/legion/team${query}`, execution)
    if (team.team_id !== ref.team_id) throw new Error('Legion returned a different team identity')
    if (team.conversation_id !== ref.session_id) throw new Error('Legion returned a different conversation identity')
    const replay = await this.request(`/api/legion/events${query}&after=${after}`, execution)
    if (replay.team_id !== ref.team_id || !Array.isArray(replay.events)) throw new Error('Legion returned an invalid event replay')
    const ordered = replay.events.filter(x => Number.isSafeInteger(x.event_id) && x.event_id > after)
      .sort((a, b) => a.event_id - b.event_id)
    const page = ordered.slice(0, this.maxEvents)
    const localTruncation = ordered.length > page.length
    const lastId = Number.isSafeInteger(replay.last_event_id) ? replay.last_event_id : after
    const nextId = localTruncation ? page.at(-1).event_id
      : Math.max(after, lastId, ...page.map(x => x.event_id))
    // The service reads at most 500 raw events before filtering private ones.
    // A full or hidden page is not an end-of-history acknowledgement.
    const moreEvents = localTruncation || replay.more_events === true
      || (nextId > after && (ordered.length >= 500 || lastId - after >= 500 || !ordered.length))
    const projection = { omitted_fields: [], truncated_fields: [] }
    const mailbox = this.commanderMailbox(team, afterMessage, projection)
    const tasks = Object.entries(team.tasks || {}).map(([id, task]) =>
      this.clean({ ...task, task_id: id }, projection, `$.runtime.tasks.${id}`))
    const artifactMap = new Map()
    for (const row of await this.store.readObservation(ref, 'artifacts') || []) artifactMap.set(row.artifact_id, row)
    const collect = row => {
      const artifact = this.artifact(row, ref.team_id)
      if (artifact) artifactMap.set(artifact.artifact_id, artifact)
    }
    const visit = (data, depth = 0) => {
      if (!data || typeof data !== 'object' || depth > 8) return
      if (data.t === 'artifact_ready' || data.event === 'artifact_ready') {
        collect(data.artifact || (data.url ? data : data.data))
      }
      for (const key of ['artifacts', 'discovered_artifacts']) {
        for (const row of Array.isArray(data[key]) ? data[key] : []) collect(row)
      }
      for (const key of ['result', 'results', 'data']) {
        const child = data[key]
        if (Array.isArray(child)) child.forEach(x => visit(x, depth + 1))
        else visit(child, depth + 1)
      }
    }
    Object.values(team.tasks || {}).forEach(task => visit(task))
    page.forEach(event => visit(event))
    const artifacts = [...artifactMap.values()]
    await this.store.writeObservation(ref, 'artifacts', artifacts)
    await this.store.writeObservation(ref, 'cursor', { last_delivered_event_id: nextId })
    return {
      task_ref: ref.id, team_id: ref.team_id, connection: 'connected',
      runtime: { tasks, ...(team.cancelled == null ? {} : { cancelled: Boolean(team.cancelled) }),
        ...(team.cancel_requests == null ? {} : { cancel_requests: this.clean(team.cancel_requests, projection) }),
        ...(team.recoverable_tasks ? { recoverable_tasks: this.clean(team.recoverable_tasks, projection) } : {}) },
      events: page.map(event => this.clean(event, projection, `$.events.${event.event_id}`)),
      next_event_id: nextId, more_events: moreEvents, artifacts: artifacts.map(({ url, ...row }) => row),
      mailbox,
      artifact_source: 'authenticated_service_exports_observed_for_this_task',
      experience_verification: EXPERIENCE, projection,
    }
  }

  /** Read receipt metadata only; recovery neither resends nor rewrites old records. */
  async readAcknowledgement(taskRef, kind, key, execution) {
    this.scope(execution)
    this.token()
    if (!['message', 'resume'].includes(kind)) throw new Error('Invalid acknowledgement operation')
    text(key, 'Operation key', 160)
    const ref = await this.store.get(execution.sessionId, taskRef)
    this.scope(execution)
    let saved, unreadable = false
    try {
      saved = await this.store.readObservation(ref, `ack-${digest(JSON.stringify([kind, key]))}`)
    } catch {
      // Only local receipt I/O/parsing is uncertain. Identity, credential and
      // cancellation errors remain errors, outside this catch.
      unreadable = true
    }
    this.scope(execution)
    if (unreadable) return { acknowledgement: 'unconfirmed', record_state: 'unreadable',
      reason: 'Saved acknowledgement could not be read; delivery remains unconfirmed.' }
    if (saved === null) return { acknowledgement: 'unconfirmed', record_state: 'missing',
      reason: 'No saved acknowledgement is available; this does not prove the message was never sent.' }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)
      || !['received', 'rejected', 'unconfirmed'].includes(saved.acknowledgement)) {
      return { acknowledgement: 'unconfirmed', record_state: 'invalid',
        reason: 'Saved acknowledgement has an invalid shape; delivery remains unconfirmed.' }
    }
    const validStatus = Number.isSafeInteger(saved.http_status) && saved.http_status >= 100 && saved.http_status <= 599
    const validReason = typeof saved.reason === 'string'
    return {
      acknowledgement: saved.acknowledgement, record_state: 'available',
      ...(validStatus ? { http_status: saved.http_status } : {}),
      ...(validReason ? { reason: this.redactText(saved.reason).slice(0, 500) } : {}),
      ...((Object.hasOwn(saved, 'http_status') && !validStatus) || (Object.hasOwn(saved, 'reason') && !validReason)
        ? { record_warning: 'Invalid optional receipt fields were omitted.' } : {}),
    }
  }

  async operation(kind, args, execution, body) {
    this.scope(execution)
    this.token()
    const ref = await this.store.get(execution.sessionId, args.task_ref)
    const operationName = `ack-${digest(JSON.stringify([kind, args.operation_key]))}`
    const created = await this.store.reserveOperation(ref, kind, args.operation_key, JSON.stringify(body))
    if (!created && !await this.#claimUnsentRetry(ref, operationName)) {
      const ack = await this.readAcknowledgement(ref.id, kind, args.operation_key, execution)
      this.scope(execution)
      return { ...ack, task_ref: ref.id, operation: kind, reused: true,
        ...(kind === 'message' ? { consumed_by_model_confirmed: false } : {}),
        next: 'Check the original team; this operation key will not send again.' }
    }
    let result
    let unsent = false
    try {
      const raw = await this.request(`/api/legion/team/${kind}`, execution, { body: { team_id: ref.team_id, ...body } })
      result = { acknowledgement: raw?.ok === false ? 'rejected' : 'received', server_response: this.clean(raw),
        ...(raw?.ok === false ? { reason: this.clean(String(raw.reason || raw.error || 'Service rejected this operation')).slice(0, 500) } : {}),
        ...(kind === 'message' ? { consumed_by_model_confirmed: false } : {}) }
    } catch (error) {
      unsent = this.#unsentErrors.has(error)
      if (isRefusal(error)) {
        await this.store.writeObservation(ref, operationName, {
          acknowledgement: 'rejected', http_status: error.status, reason: this.clean(error.message).slice(0, 500),
        })
        throw error
      }
      result = { acknowledgement: 'unconfirmed', reason: this.clean(error.message).slice(0, 500),
        ...(error instanceof HttpError ? { http_status: error.status } : {}),
        next: unsent ? 'No HTTP request was sent. Resolve the local error and retry with this same key.'
          : 'Read the original team; the remote operation may still be running. Do not retry with a new key blindly.' }
    }
    // Do not persist raw replies, which can contain task content or capabilities.
    await this.store.writeObservation(ref, operationName, {
      acknowledgement: result.acknowledgement,
      ...(result.http_status ? { http_status: result.http_status } : {}),
      ...(result.reason ? { reason: result.reason.slice(0, 500) } : {}),
    })
    if (unsent) await this.#publishUnsentRetry(ref, operationName)
    return { task_ref: ref.id, operation: kind, ...result, ...(unsent ? { request_sent: false } : {}) }
  }

  message(args, execution) {
    text(args.recipient, 'Recipient task id', 160)
    text(args.text, 'Message', 100000)
    return this.operation('message', args, execution, { recipient: args.recipient, text: args.text, sender: 'commander' })
  }

  resume(args, execution) {
    return this.operation('resume', args, execution, {})
  }

  async cancel(args, execution) {
    this.scope(execution)
    const ref = await this.store.get(execution.sessionId, args.task_ref)
    if (args.reason !== undefined) text(args.reason, 'Cancellation reason', 2000)
    const reply = await this.request('/api/legion/cancel', execution, {
      body: { team_id: ref.team_id, ...(args.reason ? { reason: args.reason } : {}) },
    })
    return { task_ref: ref.id, cancellation: reply?.ok === true ? 'requested' : reply?.ok === false ? 'rejected' : 'unconfirmed',
      stopped_confirmed: false,
      server_response: this.clean(reply), next: 'Read legion_status for observed task states. This acknowledgement does not prove OS processes have stopped.' }
  }

  async download(args, execution) {
    this.scope(execution)
    if (!/^[a-f0-9]{32}$/.test(args.artifact_id)) throw new Error('Invalid artifact id; use an id returned by legion_status')
    // Reauthorize against the live task before using any previously seen export.
    await this.status({ task_ref: args.task_ref }, execution)
    const ref = await this.store.get(execution.sessionId, args.task_ref)
    const rows = await this.store.readObservation(ref, 'artifacts') || []
    const row = rows.find(x => x.artifact_id === args.artifact_id)
    if (!row) throw new Error('Artifact id is not listed for this task')
    if (!row.download_eligible) throw new Error(row.download_reason || 'Artifact download is not eligible')
    // Recheck persisted URLs; local recovery files are not a new authority.
    const checked = this.artifact(row, ref.team_id)
    if (!checked?.download_eligible) throw new Error(checked?.download_reason || 'Invalid artifact URL')
    // The service can inject preview helpers into HTML. Its existing download
    // branch returns the exact exported bytes; caller-supplied queries stay denied.
    const downloadUrl = new URL(checked.url)
    downloadUrl.searchParams.set('download', '1')
    const directory = join(this.store.path(ref.id), 'downloads')
    await mkdir(directory, { recursive: true, mode: 0o700 })
    const safeName = basename(row.name).replace(/[^a-zA-Z0-9_.-]/g, '_').slice(0, 120) || 'artifact'
    const destination = join(directory, `${row.sha256}-${safeName}`)
    const temporary = join(directory, `${randomUUID()}.part`)
    const handle = await open(temporary, 'wx', 0o600)
    const hash = createHash('sha256')
    let count
    try {
      count = await this.request(downloadUrl, execution, {
        consume: async (response, signal) => readBytes(response.body, this.maxArtifactBytes, signal, async chunk => {
          hash.update(chunk)
          await handle.writeFile(chunk)
        }),
      })
      const actualHash = hash.digest('hex')
      if (actualHash !== row.sha256 || (row.bytes != null && row.bytes !== count)) {
        throw new Error('Artifact integrity failure: SHA-256 or byte count did not match the service export')
      }
      await handle.sync()
      await handle.close()
      await rename(temporary, destination)
    } catch (error) {
      await handle.close().catch(() => {})
      await unlink(temporary).catch(() => {})
      throw error
    }
    return { task_ref: ref.id, artifact_id: row.artifact_id, name: row.name, local_path: destination,
      bytes: count, sha256: row.sha256, integrity: 'sha256_verified', experience_verification: EXPERIENCE }
  }
}
