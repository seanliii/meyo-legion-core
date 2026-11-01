import { createHash, randomUUID } from 'node:crypto'
import { mkdir, open, readFile, readdir, link, rename, unlink } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { parseJson } from './json.js'

export const digest = value => createHash('sha256').update(value).digest('hex')

function requireText(value, label, max = 1024) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new Error(`${label} must be non-empty text (at most ${max} characters)`)
  }
}

function validateArtifacts(rows) {
  if (!Array.isArray(rows) || rows.some(row => !row
    || typeof row !== 'object' || !/^[a-f0-9]{32}$/.test(row.artifact_id))) {
    throw new Error('Invalid artifact observation')
  }
}

/** Write completely, then atomically expose the file; readers never see half JSON. */
async function atomicWrite(path, value, exclusive = false) {
  const temporary = `${path}.${randomUUID()}.tmp`
  const handle = await open(temporary, 'wx', 0o600)
  try {
    await handle.writeFile(`${JSON.stringify(value)}\n`, 'utf8')
    await handle.sync()
  } finally {
    await handle.close()
  }
  try {
    if (exclusive) await link(temporary, path)
    else await rename(temporary, path)
    return true
  } catch (error) {
    if (exclusive && error.code === 'EEXIST') return false
    throw error
  } finally {
    await unlink(temporary).catch(error => {
      if (error.code !== 'ENOENT') throw error
    })
  }
}

/**
 * A small recovery index, not another scheduler. References are immutable.
 * No credentials, raw query, transcript, model reasoning or file contents live here.
 */
export class TaskStore {
  constructor(root, serviceUrl) {
    this.root = resolve(root)
    this.service = digest(serviceUrl)
  }

  path(id) {
    if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('Invalid Legion task reference')
    return join(this.root, id)
  }

  async reserve({ sessionId, key, query, context = '', bindingDigest = '', previousRef }) {
    requireText(sessionId, 'DSH session')
    requireText(key, 'Task key', 160)
    requireText(query, 'Task query', 100000)
    if (typeof context !== 'string' || context.length > 200000) throw new Error('Invalid task context')
    const session = digest(sessionId)
    const id = digest(JSON.stringify([this.service, session, key])).slice(0, 32)
    if (previousRef) previousRef = await this.get(sessionId, previousRef.id)
    const legionSession = previousRef?.session_id || `dsh_${id}`
    const ref = {
      version: 1,
      id,
      key,
      session,
      service: this.service,
      input_digest: digest(JSON.stringify([query, context, bindingDigest, previousRef?.id || ''])),
      team_id: `web_${legionSession}_${id}`,
      session_id: legionSession,
      ...(previousRef ? { previous_task_ref: previousRef.id } : {}),
      created_at: new Date().toISOString(),
    }
    await mkdir(this.path(id), { recursive: true, mode: 0o700 })
    const created = await atomicWrite(join(this.path(id), 'ref.json'), ref, true)
    const saved = created ? ref : await this.get(sessionId, id)
    if (saved.input_digest !== ref.input_digest) {
      throw new Error('This task key already has different input. Continue that task, or explicitly choose a new key.')
    }
    return { ref: saved, created }
  }

  async get(sessionId, id) {
    let ref
    try {
      ref = parseJson(await readFile(join(this.path(id), 'ref.json'), 'utf8'), 'Saved Legion data')
    } catch (error) {
      if (error.code === 'ENOENT') throw new Error('Unknown Legion task reference in this session')
      throw error
    }
    if (ref.version !== 1 || ref.id !== id) throw new Error('Invalid saved Legion task reference')
    if (ref.session !== digest(sessionId)) throw new Error('Legion task belongs to a different DSH session')
    if (ref.service !== this.service) throw new Error('Legion task belongs to a different configured service')
    return ref
  }

  async list(sessionId) {
    let names
    try {
      names = await readdir(this.root, { withFileTypes: true })
    } catch (error) {
      if (error.code === 'ENOENT') return []
      throw error
    }
    const refs = []
    for (const entry of names) {
      if (!entry.isDirectory() || !/^[a-f0-9]{32}$/.test(entry.name)) continue
      let raw
      try {
        raw = parseJson(await readFile(join(this.path(entry.name), 'ref.json'), 'utf8'), 'Saved Legion data')
      } catch (error) {
        // mkdir precedes atomic publication. A crashed or concurrent writer
        // may leave an empty directory; a malformed published ref is an error.
        if (error.code === 'ENOENT') continue
        throw error
      }
      if (raw.session !== digest(sessionId) || raw.service !== this.service) continue
      refs.push(await this.get(sessionId, entry.name))
    }
    return refs.sort((a, b) => b.created_at.localeCompare(a.created_at))
  }

  async reserveOperation(ref, kind, key, input) {
    requireText(key, 'Operation key', 160)
    const name = `op-${digest(JSON.stringify([kind, key]))}`
    const value = { input_digest: digest(input), state: 'attempted', at: new Date().toISOString() }
    const created = await atomicWrite(join(this.path(ref.id), `${name}.json`), value, true)
    if (!created) {
      const saved = await this.readObservation(ref, name)
      if (saved.input_digest !== value.input_digest) throw new Error('Operation key already has different input')
    }
    return created
  }

  async writeObservation(ref, name, value) {
    if (!/^[a-z0-9_-]+$/.test(name) || name === 'ref') throw new Error('Invalid observation name')
    if (name === 'artifacts') {
      validateArtifacts(value)
      const directory = join(this.path(ref.id), 'artifacts')
      await mkdir(directory, { recursive: true, mode: 0o700 })
      // Queries can finish out of order, including across host processes.
      // Publish each observed export independently; a stale snapshot must
      // never erase another query's export. These are history, not grants.
      for (const row of value) await atomicWrite(join(directory, `${row.artifact_id}.json`), row)
      return
    }
    await atomicWrite(join(this.path(ref.id), `${name}.json`), value)
  }

  async readObservation(ref, name) {
    if (!/^[a-z0-9_-]+$/.test(name) || name === 'ref') throw new Error('Invalid observation name')
    let saved = null
    try {
      saved = parseJson(await readFile(join(this.path(ref.id), `${name}.json`), 'utf8'), 'Saved Legion data')
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
    if (name !== 'artifacts') return saved
    if (saved !== null) validateArtifacts(saved)
    // Keep pre-keyed indexes readable without destructive migration. New
    // per-export observations override the matching legacy row.
    const rows = new Map((saved || []).map(row => [row.artifact_id, row]))
    const directory = join(this.path(ref.id), 'artifacts')
    let entries
    try { entries = await readdir(directory) } catch (error) {
      if (error.code === 'ENOENT') return saved
      throw error
    }
    for (const entry of entries.sort()) {
      if (!/^[a-f0-9]{32}\.json$/.test(entry)) continue
      const row = parseJson(await readFile(join(directory, entry), 'utf8'), 'Saved Legion data')
      validateArtifacts([row])
      if (entry !== `${row.artifact_id}.json`) throw new Error('Mismatched artifact observation')
      rows.set(row.artifact_id, row)
    }
    return [...rows.values()]
  }
}
