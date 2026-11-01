import { callNative } from './ecosystem.js'
import { executionFrom } from './commands.js'
import { digest } from './store.js'
import { stripDataUrls } from './redaction.js'

function text(value, label, max = 160) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new Error(`${label} must be non-empty text (at most ${max} characters)`)
  }
  return value
}

// Detach before the first await. Key ordering must not change an operation's identity.
function snapshot(value, seen = new Set(), depth = 0) {
  if (depth > 32) throw new Error('Native arguments exceed the JSON depth limit')
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (!value || typeof value !== 'object' || seen.has(value)
    || (!Array.isArray(value) && ![Object.prototype, null].includes(Object.getPrototypeOf(value)))) {
    throw new Error('Native arguments must be losslessly JSON-serializable')
  }
  seen.add(value)
  const result = Array.isArray(value) ? value.map(item => snapshot(item, seen, depth + 1))
    : Object.fromEntries(Object.keys(value).sort().map(key => [key, snapshot(value[key], seen, depth + 1)]))
  seen.delete(value)
  return result
}

function safeText(client, value, limit = 512) {
  return client.clean(stripDataUrls(String(value))).slice(0, limit)
}

function project(client, result) {
  const projection = {
    omitted_fields: ['$.value', '$.error', '$.additionalContexts'],
    truncated_fields: [],
    media_return: 'text_metadata_only; media cannot be returned in full',
    media_omitted: false,
    experience_verification: 'not_performed_by_plugin',
    max_result_bytes: 12288,
  }
  const mark = (kind, path) => {
    if (projection[kind].length < 32) projection[kind].push(path)
  }
  const media = path => {
    projection.media_omitted = true
    mark('omitted_fields', path)
    return '[media omitted]'
  }
  let remaining = 256
  // Bound the shape before client.clean, but parse JSON-rendered text before
  // truncating it: otherwise a rendered {"password": ...} becomes a leaking
  // unparseable prefix. Final content is the only presentation authority:
  // post-policy content replacement may deliberately hide value/error fields.
  const visit = (value, path = '$', depth = 0) => {
    if (--remaining < 0 || depth > 8) {
      mark('truncated_fields', path)
      return '[projection limit]'
    }
    if (typeof value === 'string') {
      if (/^\s*[[{]/u.test(value)) {
        let parsed
        try { parsed = JSON.parse(value) } catch { /* Not structured JSON text. */ }
        if (parsed && typeof parsed === 'object') {
          return JSON.stringify(client.clean(visit(parsed, path, depth + 1), projection, path))
        }
      }
      const cleaned = client.clean(stripDataUrls(value, () => media(path)), projection, path)
      if (cleaned.length > 2048) {
        mark('truncated_fields', path)
        return `${cleaned.slice(0, 2048)}[truncated]`
      }
      return cleaned
    }
    if (!value || typeof value !== 'object') return value
    if (/^(?:image|audio|video|input_audio|output_audio|image_url|base64)$/i.test(value.type || '')
      || /^(?:image|audio|video)\//i.test(value.mimeType || value.mime_type || value.mediaType || value.media_type || '')) {
      return media(path)
    }
    if (Array.isArray(value)) {
      if (value.length > 32) mark('truncated_fields', path)
      return value.slice(0, 32).map((item, i) => visit(item, `${path}[${i}]`, depth + 1))
    }
    const entries = Object.entries(value)
    if (entries.length > 32) mark('truncated_fields', path)
    return Object.fromEntries(entries.slice(0, 32).flatMap(([key, item], i) => {
      const child = /^[A-Za-z_$][\w$-]{0,63}$/u.test(key) ? `${path}.${key}` : `${path}[field:${i}]`
      if (key.length > 160 || safeText(client, key, 160) !== key) {
        mark('omitted_fields', `${path}[field:${i}]`)
        return []
      }
      if (/base64|^(?:image|audio|video|screenshot)(?:[_-](?:data|bytes))?$/i.test(key)) {
        return [[key, media(child)]]
      }
      return [[key, visit(item, child, depth + 1)]]
    }))
  }
  let value = client.clean(visit({
    isError: result.isError,
    content: result.content || [],
  }), projection)
  const serialized = JSON.stringify(value)
  if (Buffer.byteLength(serialized) > projection.max_result_bytes) {
    mark('truncated_fields', '$')
    value = {
      isError: result.isError,
      excerpt: Buffer.from(serialized).subarray(0, 4096).toString('utf8'),
    }
  }
  for (const field of ['omitted_fields', 'truncated_fields']) {
    projection[field] = [...new Set(projection[field])].slice(0, 24)
      .map(path => Buffer.from(safeText(client, path, 512)).subarray(0, 96).toString('utf8'))
  }
  return { result: value, projection }
}

function failure(receipt) {
  const error = new Error(`Legion bridge did not complete successfully; do not rerun the native operation.\n${JSON.stringify(receipt)}`)
  error.name = 'LegionBridgeError'
  error.receipt = receipt
  return error
}

function settleBody(receipt, exec) {
  // The body must return before the host can accept or reject it. Reporting a
  // pending observation is not a claim that the enclosing transaction committed.
  if (receipt.native.observed.state !== 'succeeded' || exec.signal.aborted) throw failure(receipt)
  return receipt
}

function manualDelivery() {
  return { acknowledgement: 'not_attempted', automatic: false, method: 'legion_message' }
}

function nativeState(observed, transport) {
  if (observed === 'failed') return 'failed'
  return observed === 'succeeded' && transport === 'succeeded' ? 'succeeded' : 'unknown'
}

/** Observe only this execution's ancestor chain; never retain result bodies. */
function observeAncestors(ctx, client, ref, resultName, pending, token) {
  const outcomes = []
  let expected = token
  const dispose = ctx.on('tools/result', (exec, result) => {
    if (expected !== exec.token) return
    outcomes.push({
      call_id: safeText(client, exec.callId, 512),
      name: safeText(client, exec.name, 160),
      state: result.isError ? 'failed' : 'succeeded',
    })
    expected = exec.parent
    if (expected !== undefined) return
    dispose()
    const state = outcomes.some(row => row.state === 'failed') ? 'failed' : 'succeeded'
    // The official immutable root result is now known. One terminal write, no
    // HTTP, no child/parent content, no mutation of the already-returned receipt.
    return client.store.writeObservation(ref, resultName, {
      ...pending,
      native: { state: nativeState(pending.native.observed.state, state), observed: pending.native.observed },
      transport: { state, outcomes },
    }).catch(error => {
      // Observers cannot change the host result. A failed write leaves recovery
      // pending/unknown; the runtime reports this bounded persistence diagnostic.
      throw new Error(safeText(client, error.message || 'Bridge status persistence failed'))
    })
  })
}

/** Never replay a cached body, including v1 bodies hidden by a later policy. */
function recoveredMetadata(client, identity, saved, legacyDelivery) {
  const observed = ['succeeded', 'failed', 'unknown'].includes(saved.native?.observed?.state)
    && saved.version === 2 ? saved.native.observed.state
    : saved.native?.state === 'failed' ? 'failed' : 'unknown'
  let transport = { state: 'unknown' }
  if (saved.version === 2) {
    const rows = saved.transport?.outcomes
    const valid = Array.isArray(rows) && rows.length > 0 && rows.every(row =>
      typeof row?.call_id === 'string' && typeof row?.name === 'string'
      && ['succeeded', 'failed'].includes(row.state))
    if (valid && ['succeeded', 'failed'].includes(saved.transport.state)) {
      const outcomes = rows.map(row => ({
        call_id: safeText(client, row.call_id, 512), name: safeText(client, row.name, 160), state: row.state,
      }))
      transport = {
        state: outcomes.some(row => row.state === 'failed') ? 'failed' : saved.transport.state,
        outcomes,
      }
    } else if (saved.transport?.state === 'pending') {
      transport = { state: 'pending' }
    }
  }
  return {
    ...identity,
    native: { state: nativeState(observed, transport.state), observed: { state: observed } },
    transport,
    ...(saved.version === 1 ? {
      legacy_version: 1,
      recovery_note: 'Legacy record has no authoritative ancestor outcomes; cached content is not replayed',
    } : {}),
    delivery: legacyDelivery || manualDelivery(), consumed_by_model_confirmed: false, reused: true,
  }
}

async function call(ctx, client, args, exec) {
  const execution = executionFrom(exec)
  client.scope(execution)
  if (!exec.callId || exec.token === undefined) throw new Error('A native bridge requires its enclosing DSH tool execution')
  const taskRef = args?.task_ref
  const recipient = text(args?.recipient, 'Recipient task id')
  const key = text(args?.operation_key, 'Operation key')
  const name = text(args?.tool_name, 'Native tool name')
  const input = snapshot(args.arguments === undefined ? {} : args.arguments)
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Native arguments must be a JSON object')
  const ref = await client.store.get(execution.sessionId, taskRef)
  client.token()
  // Local scope/token failures must not look up tools or send remote feedback.
  if (/^legion_/i.test(name)) throw new Error('Recursive legion_* bridge calls are not allowed')
  if (safeText(client, recipient, 160) !== recipient || safeText(client, name, 160) !== name) {
    throw new Error('Recipient and native tool identifiers must not contain credentials or encoded media')
  }
  client.scope(execution)

  const id = digest(JSON.stringify(['native-call', key]))
  const resultName = `bridge-result-${id}`
  const identity = { version: 2, task_ref: ref.id, operation_id: id, recipient, tool_name: name }
  const reservation = await client.store.readObservation(ref, `op-${id}`)
  // A new invisible target must not consume the key. Recovery, however, reads
  // existing facts and must still work after that target is disabled/uninstalled.
  if (!reservation && !ctx.tools.get(name, exec.agent)) throw new Error('Native tool is not visible in this DSH agent')
  client.scope(execution)
  const created = await client.store.reserveOperation(ref, 'native-call', key,
    JSON.stringify({ recipient, tool_name: name, arguments: input }))
  if (!created) {
    const saved = await client.store.readObservation(ref, resultName)
    if (!saved) {
      // Do not write "unknown" over the owner's eventual result, and do not burn
      // its message key with an in-flight placeholder. A crash is equally unknown.
      throw failure({ ...identity, native: { state: 'unknown', observed: { state: 'unknown' },
        reason: 'Reserved without a committed result; may be in flight or interrupted' },
        transport: { state: 'unknown' }, delivery: manualDelivery(),
        consumed_by_model_confirmed: false, reused: true })
    }
    const legacyDelivery = saved.version === 1 ? {
      ...await client.readAcknowledgement(ref.id, 'message', `native-result:${id}`, execution),
      historical: true, attempted_now: false,
    } : undefined
    client.scope(execution)
    return recoveredMetadata(client, identity, saved, legacyDelivery)
  }

  let result, state
  try {
    client.scope(execution)
    result = await callNative(ctx, exec, name, input)
    state = /^ABORTED/.test(result.error?.info?.code || '') ? 'unknown'
      : result.isError ? 'failed' : exec.signal.aborted ? 'unknown' : 'succeeded'
  } catch {
    // A dispatch/pipeline exception has no authoritative content presentation,
    // and cannot prove whether a side effect ran. Do not expose its hidden body.
    state = 'unknown'
    result = { isError: true, content: [{ type: 'text', text: 'Native execution outcome is unknown' }] }
  }
  const pending = {
    ...identity,
    native: { state: 'unknown', observed: { state } },
    transport: { state: 'pending' },
    delivery: manualDelivery(), consumed_by_model_confirmed: false,
  }
  observeAncestors(ctx, client, ref, resultName, pending, exec.token)
  const projected = project(client, result)
  const receipt = {
    ...pending, native: { ...pending.native, result: projected.result }, projection: projected.projection, reused: false,
  }
  try {
    await client.store.writeObservation(ref, resultName, pending)
  } catch (error) {
    // Persistence and host execution remain separate facts. A successful child
    // still needs the parent to return before the official observer can commit.
    return settleBody({ ...receipt, persistence: 'failed', persistence_error: safeText(client, error.message) }, exec)
  }
  return settleBody(receipt, exec)
}

/** One explicit DSH invocation, not a dispatcher or an executor of Legion mail. */
export async function bridgeCall(ctx, client, args, exec) {
  try { return await call(ctx, client, args, exec) }
  catch (error) {
    if (error.name === 'LegionBridgeError') throw error
    throw new Error(safeText(client, error.message || 'Native bridge failed'))
  }
}
