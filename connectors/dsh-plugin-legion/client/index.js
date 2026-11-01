// Native DSH browser module. React is supplied by the host, not bundled here.
// All operations use native services; no token, HTTP SDK or second agent loop.
window.__ModuleLoader__.load({
  id: 'dsh-plugin-legion',
  factory: require => {
    const limit = 240
    const labels = {
      success: {
        zh: '军团回执（非任务完成确认）：',
        en: 'Legion reply (not task completion): ',
      },
      error: { zh: '军团命令错误：', en: 'Legion command error: ' },
      emptySuccess: {
        zh: '命令已返回；可用 /legion list 查看本会话任务。',
        en: 'Command returned; use /legion list to check this session’s tasks.',
      },
      emptyError: {
        zh: '未返回错误详情；请查询原任务状态，勿盲目重发。',
        en: 'No error details returned; check the original task before retrying.',
      },
      truncated: { zh: '…（即时摘要）', en: '… (live summary)' },
    }

    function notice(locale, result) {
      const error = result.kind === 'error'
      const body = typeof result.text === 'string' ? result.text.replace(/\s+/gu, ' ').trim() : ''
      const text = locale.resolveText(labels[error ? 'error' : 'success'])
        + (body || locale.resolveText(labels[error ? 'emptyError' : 'emptySuccess']))
      const points = Array.from(text)
      if (points.length <= limit) return text
      const suffix = locale.resolveText(labels.truncated)
      return points.slice(0, limit - Array.from(suffix).length).join('') + suffix
    }

    const rows = value => Array.isArray(value) ? value : []
    const bool = value => typeof value === 'boolean' ? value : null
    const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
    const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
    const teamDefaults = () => ({
      name: '我的战队', goal: '让我的 DSH 和战队在真实工作中持续变强',
      collaboration: 'auto', leader: '', instructions: '', evolutionEnabled: true, cadence: 'weekly',
    })
    const pendingStorageKey = 'dsh-legion.pending-operations.v1'
    const validRunKey = key => typeof key === 'string' && key.length > 0 && key.length <= 128
      && key === key.trim() && key.isWellFormed() && !/[\u0000-\u001f\u007f]/u.test(key)
    const canonical = value => Array.isArray(value) ? value.map(canonical)
      : record(value) ? Object.fromEntries(Object.keys(value).sort().filter(key => value[key] !== undefined)
        .map(key => [key, canonical(value[key])])) : value
    const configText = value => {
      const result = { ...value, candidateHarnesses: value?.candidateHarnesses ?? [] }
      if (record(result.teamProfile)) {
        result.teamProfile = { ...teamDefaults(), ...result.teamProfile }
        for (const key of ['name', 'goal', 'leader', 'instructions']) {
          if (typeof result.teamProfile[key] === 'string') result.teamProfile[key] = result.teamProfile[key].trim()
        }
      }
      return JSON.stringify(canonical(result))
    }
    const hashText = async value => Array.from(new Uint8Array(
      await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)),
    ), byte => byte.toString(16).padStart(2, '0')).join('')
    const sameNote = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b))
    const sameOwner = (a, b) => a.sessionId === b.sessionId && a.entryId === b.entryId && a.namespace === b.namespace
    const sameConnection = (a, b) => sameOwner(a, b) && a.connectionHash === b.connectionHash
    const storageFault = () => new Error('浏览器持久存储不可用或恢复便条无效；不能安全保存／清理操作身份。请先恢复浏览器存储再核对原操作，不要换 key 重发。 / Persistent recovery storage is unavailable.')
    const mismatchFault = () => new Error('有待完成操作，但会话、配置归属、连接或已读回配置不匹配；仅保留便条，不自动派发。请切回原会话和连接核对。 / Pending operation does not match the current configuration.')

    function artifactFacts(value) {
      const available = Array.isArray(value?.artifacts)
      const items = rows(value?.artifacts).filter(record)
      const page = value?.sections?.artifacts
      const paths = [...rows(value?.projection?.omitted_fields), ...rows(value?.projection?.truncated_fields)]
      const affected = paths.some(path => typeof path === 'string' && (path === '$'
        || path === '$.artifacts' || path.startsWith('$.artifacts.') || path.startsWith('$.artifacts[')))
      const complete = available && items.length === value.artifacts.length && !affected && value.more_events === false
        && (page ? page.available === true && page.complete === true && page.shown_count === items.length
          : value.truncated !== true && value.projection?.paths_truncated !== true)
      const observed = page?.observed_count
      return { available, items, complete,
        observed_count: available ? Number.isSafeInteger(observed) && observed >= items.length
          ? observed : page ? null : items.length : null }
    }

    function configEntry(entries, namespaces, bundles) {
      const matches = rows(entries).filter(entry => entry?.moduleName === 'dsh-plugin-legion')
      if (matches.length !== 1 || typeof matches[0].entryId !== 'string' || !matches[0].entryId) return null
      const { entryId, moduleName } = matches[0]
      // Settings uses the profile row id, while inventory uses the loader id.
      // Join native facts; never infer a namespace from the loader id's spelling.
      const mapped = rows(bundles).filter(bundle => bundle?.name === moduleName)
        .flatMap(bundle => rows(bundle.rows)).filter(row => row?.entryId === entryId)
      if (mapped.length > 1 || mapped.some(row => row.moduleName !== moduleName
        || typeof row.rowId !== 'string' || !row.rowId)) return null
      const namespace = mapped[0]?.rowId ?? entryId
      return rows(namespaces).filter(section => section?.ns === namespace).length === 1
        ? { entryId, namespace } : null
    }

    function sessionLabel(sessions, id) {
      const title = sessions.byId[id]?.displayTitle
      if (!title) return id
      const peers = sessions.ids.filter(other => other !== id && sessions.byId[other]?.displayTitle === title)
      if (!peers.length) return title
      let length = Math.min(8, id.length)
      while (length < id.length && peers.some(other => other.slice(-length) === id.slice(-length))) length++
      return `${title} · ${id.slice(-length)}`
    }

    function memberFacts(member) {
      return {
        key: member.key, installed: bool(member.installed),
        connected: bool(member.readiness?.connected), verified: bool(member.readiness?.verified),
        available: bool(member.available), selectable: bool(member.entry_points?.execution?.selectable),
      }
    }

    function publicLink(value) {
      if (typeof value !== 'string') return null
      try {
        const url = new URL(value)
        if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null
        const host = url.hostname.toLowerCase()
        if (!host.includes('.') || host.endsWith('.localhost') || host.endsWith('.local')
          || /^[\d.]+$/.test(host) || host.includes(':')) return null
        return url.href
      } catch { return null }
    }

    function configOperations(draft) {
      const operations = []
      for (const key of ['baseUrl', 'tokenEnv', 'tokenFile', 'candidateHarnesses', 'teamProfile']) {
        if (!own(draft, key)) continue
        let value = draft[key]
        if (key === 'candidateHarnesses') {
          if (!Array.isArray(value) || value.some(key => typeof key !== 'string' || !key.trim())) {
            throw new Error('成员选择必须是有效的名称列表 / Invalid member selection')
          }
          value = [...new Set(value)]
        } else if (key === 'teamProfile') {
          if (!record(value)) throw new Error('战队配置必须是对象 / Invalid team configuration')
          // Host Config remains the authority for fields, defaults and validation.
          value = { ...value }
        } else {
          if (typeof value !== 'string') throw new Error('配置必须为文字 / Invalid configuration')
          value = value.trim()
        }
        operations.push(value === '' ? { op: 'unset', path: [key] } : { op: 'set', path: [key], value })
      }
      return operations
    }

    function changeFacts(result) {
      return {
        changed: result?.changed === true, application: result?.application ?? 'unknown',
        stage: result?.stage ?? 'unknown', complete: result?.application === 'applied',
        reason: result?.error?.diagnostic || result?.error?.code || result?.restartReason || '',
        warnings: rows(result?.warnings),
      }
    }

    function errorText(error) {
      return String(error?.message || error?.diagnostic || error?.code || error || 'Unknown error')
        .replace(/\bBearer\s+\S+/giu, 'Bearer [redacted]')
        .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/giu, '$1[redacted]@')
        .replace(/([?&](?:token|key|api_key|access_token|secret|password)=)[^&\s]+/giu, '$1[redacted]')
        .slice(0, 2000)
    }

    function remoteValue(result) {
      if (result?.ok !== true) throw new Error(errorText(result?.error || '未收到原生服务回执 / No native service reply'))
      return result.value
    }

    // This controller owns UI state only. The host owns configuration, installs,
    // task identity, permissions and execution. Late results are session-fenced.
    function createController(ctx, options = {}) {
      let disposed = false
      let epoch = 0
      let connectionEpoch = 0
      let connectionId = null
      let connectionReadSequence = 0
      let acceptedConnectionRead = 0
      let form
      let formOwner
      let stopForm
      let statusRead
      const services = {}
      const listeners = new Set()
      const sequences = new Map()
      const operations = new Map()
      const submissions = new Map()
      const flowMutations = new Map()
      const flowWrites = ['flowStart', 'flowPause', 'flowRun']
      const sessionKeys = new Set(['members', 'search', 'source', 'tasks', 'taskStatus', 'submit', 'download',
        'flowStatus', ...flowWrites, 'flowRecovery', 'flowOpen', 'teamSetup', 'modelStatus'])
      let state = {
        sessionId: '', sessions: ctx.sessions.list.getSnapshot(),
        available: {}, busy: {}, errors: {}, members: null, search: null, source: null,
        packages: null, plugins: null, config: null, configEntry: null, configNamespace: null, configSaved: false,
        inspection: null, installResult: null, installId: null, installUncertain: false, installCancellation: null,
        tasks: null, tasksStale: false, taskStatus: null, taskStatusStale: false, download: null, operation: null, submission: null,
        flow: null, flowStale: false, flowRunKey: null, modelStatus: null,
      }
      const publish = patch => {
        if (disposed) return
        state = { ...state, ...patch }
        for (const listener of listeners) listener()
      }
      const setError = (key, error) => publish({ errors: { ...state.errors, [key]: errorText(error) } })
      const invalidate = (keys, patch = {}) => {
        const busy = { ...state.busy }, errors = { ...state.errors }
        for (const key of keys) {
          sequences.set(key, (sequences.get(key) || 0) + 1)
          busy[key] = false
          errors[key] = null
        }
        publish({ busy, errors, ...patch })
      }
      const operationId = (sessionId, connection = connectionId) => connection ? JSON.stringify([sessionId, connection]) : null
      const operationView = sessionId => ({ operation: operations.get(operationId(sessionId)) || null,
        submission: submissions.get(operationId(sessionId)) || null })
      const clearTaskView = () => {
        connectionEpoch++
        connectionId = null
        statusRead = null
        // Hide observations, not recovery records. Old unknown operations remain
        // indexed by their authenticated Host connection until it is read again.
        invalidate(['tasks', 'taskStatus', 'download', 'submit', 'members'],
          { tasks: null, tasksStale: false, taskStatus: null, taskStatusStale: false, download: null,
            members: null, operation: null, submission: null })
      }
      const acceptConnection = (value, sessionId, readSequence) => {
        if (typeof value?.connection_id !== 'string' || !/^[a-f0-9]{64}$/.test(value.connection_id)) {
          throw new Error('无法确认原生恢复身份；请重新读取战队任务，原操作记录仍保留。 / Native recovery identity is unknown; read Team tasks again.')
        }
        // Even the first accepted identity supersedes older reads. A failed
        // newer read does not advance this successful-read watermark.
        acceptedConnectionRead = readSequence
        if (connectionId && connectionId !== value.connection_id) {
          // Even unchanged public settings can resolve to a different native
          // credential/index. Invalidate older reads before accepting this one.
          connectionEpoch++
          statusRead = null
          invalidate(['submit', 'taskStatus', 'download', 'members'], {
            tasks: null, tasksStale: false, taskStatus: null, taskStatusStale: false, download: null,
            members: null, operation: null, submission: null,
          })
        }
        connectionId = value.connection_id
        return operationView(sessionId)
      }
      const disconnectConfig = () => {
        clearTaskView()
        invalidate(['config', 'configSave', 'flowStatus'], { flow: null, flowStale: false, flowRunKey: null })
        stopForm?.()
        stopForm = null
        form = null
        formOwner = null
        publish({ config: null, configEntry: null, configNamespace: null, configSaved: false })
      }
      const observeConfig = (config, entryId = state.configEntry, namespace = state.configNamespace) => {
        // Retained observations are scoped to the native connection too, not
        // just the selected session/task. These are references, never tokens.
        if (entryId !== state.configEntry || namespace !== state.configNamespace
          || ['baseUrl', 'tokenEnv', 'tokenFile', 'stateDirectory'].some(key => config?.value?.[key] !== state.config?.value?.[key])) {
          clearTaskView()
        }
        if (entryId !== state.configEntry || namespace !== state.configNamespace
          || configText(config?.value) !== configText(state.config?.value)) {
          invalidate(['flowStatus'], { flow: null, flowStale: false, flowRunKey: null })
          // Member observations carry a candidate snapshot, not just a session.
          invalidate(['members'], { members: null })
        }
        return config
      }
      // These are recovery notes for explicit user actions, not schedules. The
      // Host catalog remains authoritative; neither prompts nor config values
      // or credentials are written to browser storage.
      const pendingStorage = () => {
        try {
          const storage = own(options, 'storage') ? options.storage : window.localStorage
          if (!storage || ['getItem', 'setItem', 'removeItem'].some(key => typeof storage[key] !== 'function')) throw storageFault()
          return storage
        } catch { throw storageFault() }
      }
      const readNotes = () => {
        try {
          const text = pendingStorage().getItem(pendingStorageKey)
          if (text === null) return []
          if (typeof text !== 'string' || text.length > 32768) throw storageFault()
          const saved = JSON.parse(text)
          const fields = ['action', 'sessionId', 'entryId', 'namespace', 'configurationHash', 'connectionHash', 'runKey']
          if (!record(saved) || saved.version !== 1 || Object.keys(saved).some(key => !['version', 'operations'].includes(key))
            || !Array.isArray(saved.operations) || saved.operations.length > 16 || saved.operations.some(note =>
              !record(note) || Object.keys(note).some(key => !fields.includes(key))
              || !['flow_start', 'flow_run'].includes(note.action)
              || ['sessionId', 'entryId', 'namespace'].some(key => typeof note[key] !== 'string'
                || !note[key].trim() || note[key].length > 1024 || !note[key].isWellFormed())
              || ['configurationHash', 'connectionHash'].some(key => typeof note[key] !== 'string' || !/^[a-f0-9]{64}$/u.test(note[key]))
              || (note.action === 'flow_run' ? !validRunKey(note.runKey) : own(note, 'runKey')))) throw storageFault()
          return saved.operations
        } catch { throw storageFault() }
      }
      async function editNotes(edit) {
        const work = () => {
          const before = readNotes()
          const { notes, value } = edit(before)
          if (sameNote(notes, before)) return value
          const text = JSON.stringify({ version: 1, operations: notes })
          if (notes.length > 16 || text.length > 32768) {
            throw new Error('待确认操作便条已满；请先核对原会话，不能覆盖旧身份。 / Resolve pending operations before adding another.')
          }
          try {
            const storage = pendingStorage()
            if (notes.length) storage.setItem(pendingStorageKey, text)
            else storage.removeItem(pendingStorageKey)
            if (storage.getItem(pendingStorageKey) !== (notes.length ? text : null)) throw storageFault()
          } catch { throw storageFault() }
          return value
        }
        // Browser-native locking only covers the small synchronous note update.
        // A second tab reuses the published run key; no lock owns model work.
        return typeof navigator !== 'undefined' && navigator.locks?.request
          ? navigator.locks.request(pendingStorageKey, work) : work()
      }
      const forgetNote = (note, current = () => true) => editNotes(notes => current()
        ? { notes: notes.filter(row => !sameNote(row, note)), value: true }
        : { notes, value: false })
      const captureConfig = () => {
        if (!state.sessionId || state.config?.status !== 'ready' || !record(state.config.value)
          || !state.configEntry || !state.configNamespace || !services.configuration) {
          throw new Error('请先选择会话并读回原生配置，再开始或恢复操作。 / Read the native configuration first.')
        }
        return { sessionId: state.sessionId, entryId: state.configEntry, namespace: state.configNamespace,
          owner: services.configuration, epoch, text: configText(state.config.value) }
      }
      const scopeCurrent = view => !disposed && epoch === view.epoch && state.sessionId === view.sessionId
        && state.configEntry === view.entryId && state.configNamespace === view.namespace
        && services.configuration === view.owner
      const viewCurrent = view => scopeCurrent(view) && state.config?.status === 'ready'
        && configText(state.config.value) === view.text
      async function fingerprint(view, text = view.text) {
        const { teamProfile, candidateHarnesses, ...connection } = JSON.parse(text)
        const [configurationHash, connectionHash] = await Promise.all([hashText(text), hashText(JSON.stringify(canonical(connection)))])
        return { sessionId: view.sessionId, entryId: view.entryId, namespace: view.namespace, configurationHash, connectionHash }
      }
      async function readBackConfig(view) {
        if (!scopeCurrent(view)) return
        if (typeof view.owner.settings?.describe !== 'function') {
          throw new Error('缺少原生配置的新鲜读回能力；恢复便条已保留，暂不自动开工。 / A fresh native settings read is required.')
        }
        const document = remoteValue(await view.owner.settings.describe())
        if (!record(document) || !Array.isArray(document.namespaces) || typeof document.writable !== 'boolean') throw mismatchFault()
        const identity = await readConfigIdentity(view.owner, document.namespaces)
        if (!scopeCurrent(view)) return
        if (!identity || identity.entryId !== view.entryId || identity.namespace !== view.namespace) throw mismatchFault()
        const sections = rows(document?.namespaces).filter(section => section.ns === view.namespace)
        const mirror = view.owner.forms.describe()
        if (sections.length !== 1 || typeof mirror.acceptView !== 'function'
          || !Number.isSafeInteger(sections[0].revision)
          || sections[0].revision < (form?.getSnapshot()?.revision ?? 0)) throw mismatchFault()
        mirror.acceptView(sections[0])
        const snapshot = form?.getSnapshot()
        if (snapshot?.status !== 'ready') throw mismatchFault()
        // acceptView folds only the namespace. Document-wide facts still belong
        // to this fresh response, not the mirror's previously cached document.
        publish({ config: observeConfig({ ...snapshot, writable: document.writable,
          ...(typeof document.hasDocument === 'boolean' ? { hasDocument: document.hasDocument } : {}) }) })
        return captureConfig()
      }
      async function remember(view, action, key, text = view.text) {
        const identity = await fingerprint(view, text)
        if (!viewCurrent(view)) return
        return editNotes(notes => {
          const previous = notes.find(note => note.action === action && sameConnection(note, identity))
          if (action === 'flow_run' && previous
            && (previous.configurationHash !== identity.configurationHash || (key && previous.runKey !== key))) {
            throw new Error('上次开工尚未确认或配置已经变化；请核对原操作，不能换 key 补发。 / Resolve the original run first.')
          }
          const note = { action, ...identity, ...(action === 'flow_run' ? { runKey: previous?.runKey || key || crypto.randomUUID() } : {}) }
          return { notes: [...notes.filter(row => row !== previous), note], value: note }
        })
      }
      const flowSettled = outcome => outcome?.code === 'ok' && outcome.catalogVerified === true
        && outcome.partial !== true && outcome.cancelled !== true
      async function executeNote(note, view) {
        const current = () => viewCurrent(view) && readNotes().some(row => sameNote(row, note))
        if (!current()) return // A pause or newer explicit setup may have withdrawn it.
        if (note.action === 'flow_run') publish({ flowRunKey: note.runKey })
        let outcome
        try {
          outcome = await command(view.sessionId, note.action, note.action === 'flow_run' ? { key: note.runKey } : {}, current)
        } catch (error) {
          if (!viewCurrent(view)) return
          throw error
        }
        if (!current()) return
        publish({ flow: outcome, flowStale: false })
        if (flowSettled(outcome)) {
          if (!await forgetNote(note, current)) return
          if (viewCurrent(view) && note.action === 'flow_run') publish({ flowRunKey: null })
        }
        return viewCurrent(view) ? outcome : undefined
      }
      async function run(key, work, scoped = false) {
        if (disposed || state.busy[key]) return false
        const currentEpoch = epoch
        const sessionId = state.sessionId
        const sequence = (sequences.get(key) || 0) + 1
        sequences.set(key, sequence)
        const current = () => !disposed && sequences.get(key) === sequence && (!scoped || epoch === currentEpoch)
        publish({ busy: { ...state.busy, [key]: true }, errors: { ...state.errors, [key]: null } })
        try {
          if (scoped && !sessionId) throw new Error('请先选择一个 DSH 会话 / Select a DSH session first')
          const patch = await work(sessionId)
          if (current() && patch) {
            publish(patch)
            return true
          }
        } catch (error) {
          if (current()) setError(key, error)
        } finally {
          if (current()) publish({ busy: { ...state.busy, [key]: false } })
        }
        return false
      }
      async function command(sessionId, action, args = {}, current = () => true) {
        return ctx.sessions.using(sessionId, { source: 'controllerOperation' }, async reference => {
          await reference.ready
          if (!current()) throw new Error('命令归属已变化；未通过新连接发送旧操作 / Command ownership changed')
          const response = remoteValue(await ctx.remote.commands.execute(
            sessionId, `/legion-panel ${JSON.stringify({ action, args })}`, [],
          ))
          let body
          try { body = JSON.parse(response?.result?.text) } catch {
            throw new Error(response?.result?.text || '命令未返回可用数据 / Command did not return usable data')
          }
          if (response?.result?.kind !== 'success' || body.ok !== true || body.action !== action) {
            const error = new Error(errorText(body.error || '无法确认命令结果 / Unconfirmed command result'))
            const rejected = body.diagnostics
            // Only a typed Host rejection with a recovery identity can settle
            // this submit. An error message alone never proves it was unsent.
            if (action === 'submit' && body.action === action && body.ok === false
              && response?.result?.kind === 'error' && rejected?.submission === 'rejected'
              && typeof rejected.task_ref === 'string' && /^[a-f0-9]{32}$/.test(rejected.task_ref)) {
              error.receipt = { task_ref: rejected.task_ref, submission: 'rejected', reason: error.message }
            }
            throw error
          }
          return body.value
        })
      }
      async function writeFlow(key, action, args = {}, setup) {
        const id = state.sessionId
        if (flowMutations.has(id)) return false
        const mutation = {}
        flowMutations.set(id, mutation)
        invalidate(['flowStatus'], { flowStale: state.flow !== null })
        try {
          let outcome
          const received = await run(key, async sessionId => {
            let view = captureConfig()
            if (action === 'flow_pause') {
              const identity = await fingerprint(view)
              if (!viewCurrent(view)) return
              await editNotes(notes => ({ notes: notes.filter(note => !sameConnection(note, identity)) }))
              if (!viewCurrent(view)) return
              publish({ flowRunKey: null })
              outcome = await command(sessionId, action)
            } else {
              if (action === 'flow_run' && args.key !== undefined && !validRunKey(args.key)) {
                throw new Error('开工 key 必须是 1–128 个有效字符，不能有首尾空白或控制字符。 / Invalid run key.')
              }
              let expected = view.text
              if (setup) {
                if (setup.entryId !== view.entryId || setup.namespace !== view.namespace) throw mismatchFault()
                if (!state.config.writable || !Number.isSafeInteger(setup.revision)) {
                  throw new Error('当前配置不可编辑 / Configuration is not writable')
                }
                const value = JSON.parse(view.text)
                for (const op of configOperations(setup.draft)) {
                  if (op.op === 'set') value[op.path[0]] = op.value
                  else delete value[op.path[0]]
                }
                expected = configText(value)
              }
              const note = await remember(view, action, args.key, expected)
              if (!note || !scopeCurrent(view)) return
              if (setup) {
                let refused = false
                const accepted = await controller.saveConfig(setup.draft, setup.revision, setup.entryId, setup.namespace, async () => {
                  refused = true
                  await forgetNote(note)
                })
                if (refused || !scopeCurrent(view)) return
                if (!accepted || configText(state.config?.value) !== expected) view = await readBackConfig(view)
                else view = captureConfig()
                if (!view) return
                const confirmed = await fingerprint(view)
                if (!sameConnection(note, confirmed) || note.configurationHash !== confirmed.configurationHash) throw mismatchFault()
                if (!viewCurrent(view)) return
                publish({ configSaved: true })
              }
              outcome = await executeNote(note, view)
            }
            return outcome && viewCurrent(view) ? { flow: outcome, flowStale: false } : undefined
          }, true)
          // A successful command envelope can contain a partial native write.
          // Display those facts, but do not settle the operation or forget its key.
          return received && flowSettled(outcome)
        } finally {
          if (flowMutations.get(id) === mutation) flowMutations.delete(id)
        }
      }
      const service = key => {
        if (!services[key]) throw new Error(`DSH 未提供此原生服务 / Native service unavailable: ${key}`)
        return services[key]
      }
      async function readConfigIdentity(owner, freshNamespaces) {
        const current = () => !disposed && services.configuration === owner
        const mirror = freshNamespaces === undefined ? owner.forms.describe() : undefined
        await mirror?.ensure()
        if (!current()) return
        const entries = remoteValue(await owner.inventory.list()).entries
        if (!current()) return
        const bundles = owner.manager ? remoteValue(await owner.manager.listBundles()) : []
        if (!current()) return
        const snapshot = mirror?.getSnapshot()
        if (mirror && (!snapshot.view || snapshot.error)) throw new Error(snapshot.error || 'DSH 配置暂不可读 / Settings unavailable')
        const identity = configEntry(entries, freshNamespaces ?? snapshot.view.namespaces, bundles)
        if (!identity) throw new Error('未找到唯一军团配置，请在 DSH 插件页检查重复实例 / No unique Legion entry')
        return identity
      }
      const stopSessions = ctx.sessions.list.subscribe(() => {
        const sessions = ctx.sessions.list.getSnapshot()
        publish({ sessions })
        if (state.sessionId && !sessions.ids.includes(state.sessionId)) controller.selectSession('')
      })
      const controller = {
        getSnapshot: () => state,
        subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
        dispose() {
          disposed = true
          epoch++
          statusRead = null
          stopForm?.()
          stopSessions()
          listeners.clear()
          operations.clear()
          submissions.clear()
          for (const key of Object.keys(services)) delete services[key]
        },
        attach(key, value) {
          if (key === 'configuration' && services[key] && services[key] !== value) disconnectConfig()
          services[key] = value
          publish({ available: { ...state.available, [key]: true } })
          return () => {
            if (services[key] !== value) return
            delete services[key]
            if (key === 'configuration') disconnectConfig()
            publish({ available: { ...state.available, [key]: false } })
          }
        },
        selectSession(sessionId) {
          if (sessionId && !state.sessions.ids.includes(sessionId)) throw new Error('Unknown DSH session')
          if (sessionId === state.sessionId) return
          epoch++
          statusRead = null
          const busy = { ...state.busy }, errors = { ...state.errors }
          for (const key of sessionKeys) {
            sequences.set(key, (sequences.get(key) || 0) + 1)
            busy[key] = false
            errors[key] = null
          }
          publish({ sessionId, busy, errors, members: null, search: null, source: null, tasks: null, tasksStale: false,
            taskStatus: null, taskStatusStale: false, download: null, ...operationView(sessionId),
            flow: null, flowStale: false, flowRunKey: null, modelStatus: null })
        },
        async refreshSessions() {
          await run('sessions', async () => {
            await ctx.sessions.refresh()
            return { sessions: ctx.sessions.list.getSnapshot() }
          })
        },
        loadMembers(offset = 0) {
          return run('members', async id => ({ members: await command(id, 'runtime', {
            view: 'members', ...(offset > 0 ? { offset } : {}),
          }) }), true)
        },
        async loadFlow() {
          if (flowMutations.has(state.sessionId)) return Promise.resolve(false)
          publish({ flowStale: state.flow !== null })
          const received = await run('flowStatus', async id => ({
            flow: await command(id, 'flow_status'), flowStale: false,
          }), true)
          if (received) await controller.recoverPendingFlow()
          return received
        },
        openFlowSession() {
          return run('flowOpen', async id => {
            service('navigation').openSession(id)
            return {}
          }, true)
        },
        loadModelStatus() {
          return run('modelStatus', async id => ctx.sessions.using(id, { source: 'controllerOperation' }, async reference => {
            const binding = await reference.ready
            const catalog = remoteValue(await service('models').modelCatalog())
            const snapshot = binding?.session?.getSnapshot()
            const selection = binding?.session?.projections?.faceOf('modelSelection').getSnapshot()?.next ?? catalog.default
            const available = typeof selection?.provider === 'string' && typeof selection?.model === 'string'
              && rows(catalog.groups).some(group => group.id === selection.provider
                && rows(group.models).some(model => model.id === selection.model))
            return { modelStatus: {
              available, provider: selection?.provider ? errorText(selection.provider) : null,
              model: selection?.model ? errorText(selection.model) : null,
              running: bool(snapshot?.running), lastError: snapshot?.lastAgentError ? errorText(snapshot.lastAgentError) : null,
              failures: rows(catalog.failures).map(failure => errorText(failure.message)),
              execution: 'not_verified',
            } }
          }), true)
        },
        startFlow() { return writeFlow('flowStart', 'flow_start') },
        pauseFlow() { return writeFlow('flowPause', 'flow_pause') },
        runFlow(key) { return writeFlow('flowRun', 'flow_run', { key }) },
        saveAndStartTeam(draft, revision, entryId, namespace = entryId) {
          return writeFlow('teamSetup', 'flow_start', {}, { draft, revision, entryId, namespace })
        },
        async recoverPendingFlow() {
          const id = state.sessionId
          if (!id || disposed || state.config?.status !== 'ready' || flowMutations.has(id)) return false
          const mutation = {}
          flowMutations.set(id, mutation)
          try {
            return await run('flowRecovery', async () => {
              const notes = readNotes()
              if (!notes.length) return {}
              let view = captureConfig()
              if (!notes.some(note => sameOwner(note, view))) throw mismatchFault()
              view = await readBackConfig(view)
              if (!view) return
              const identity = await fingerprint(view)
              if (!viewCurrent(view)) return
              const pending = notes.filter(note => sameConnection(note, identity) && note.configurationHash === identity.configurationHash)
              if (!pending.length) throw mismatchFault()
              for (const note of pending) {
                const outcome = await executeNote(note, view)
                if (!outcome || !flowSettled(outcome)) break
              }
              return viewCurrent(view) ? {} : undefined
            }, true)
          } finally {
            if (flowMutations.get(id) === mutation) flowMutations.delete(id)
          }
        },
        loadPackages() {
          return run('packages', async () => {
            const manager = service('manager')
            const packages = remoteValue(await manager.listBundles())
            const plugins = remoteValue(await manager.listPlugins())
            return { packages, plugins }
          })
        },
        async loadConfig() {
          const loaded = await run('config', async () => {
            const owner = service('configuration')
            const identity = await readConfigIdentity(owner)
            if (!identity || disposed || services.configuration !== owner) return
            const { entryId, namespace } = identity
            stopForm?.()
            const target = owner.forms.get(namespace)
            if (form !== target || formOwner !== owner || state.configEntry !== entryId
              || state.configNamespace !== namespace) invalidate(['configSave'])
            form = target
            formOwner = owner
            stopForm = target.subscribe(() => {
              if (services.configuration === owner && form === target && state.configEntry === entryId
                && state.configNamespace === namespace) publish({ config: observeConfig(target.getSnapshot()) })
            })
            return { configEntry: entryId, configNamespace: namespace,
              config: observeConfig(target.getSnapshot(), entryId, namespace), configSaved: false }
          })
          if (loaded) await controller.recoverPendingFlow()
          return loaded
        },
        saveConfig(draft, revision, entryId, namespace = entryId, onRefused) {
          return run('configSave', async () => {
            publish({ configSaved: false })
            if (typeof entryId !== 'string' || entryId !== state.configEntry || namespace !== state.configNamespace) {
              throw new Error('配置实例已变化；请重新读取并放弃旧草稿后再改 / Configuration entry changed; reload and discard the old draft')
            }
            if (!form || formOwner !== services.configuration || state.config?.status !== 'ready' || !state.config.writable
              || !Number.isSafeInteger(revision)) throw new Error('当前配置不可编辑 / Configuration is not writable')
            const target = form, owner = formOwner
            const current = () => !disposed && form === target && services.configuration === owner
              && state.configEntry === entryId && state.configNamespace === namespace
            // Namespace revisions alone cannot distinguish replacement loader
            // instances. Recheck native ownership without rebasing the draft.
            const identity = await readConfigIdentity(owner)
            if (!identity || !current()) return
            if (identity.entryId !== entryId || identity.namespace !== namespace) {
              throw new Error('配置实例已变化；请重新读取并放弃旧草稿后再改 / Configuration entry changed; reload and discard the old draft')
            }
            let accepted
            try { accepted = await target.mutate(configOperations(draft), revision) } catch (error) {
              if (['settings/rejected', 'settings/conflict'].includes(error?.code)) await onRefused?.()
              throw error
            }
            // A definite refusal must retire the note even if this controller
            // was disposed while the native write was crossing the wire.
            if (!accepted) {
              await onRefused?.()
              throw new Error('保存被拒绝或版本冲突；请重新读取后再改 / Save refused or revision conflict')
            }
            if (!current()) return
            return { config: observeConfig(target.getSnapshot()), configSaved: true, members: null }
          })
        },
        clearSearch() {
          invalidate(['search'])
          publish({ search: null })
        },
        search(kind, query, indexUrl) {
          controller.clearSearch()
          return run('search', async id => {
            if (kind === 'github') return { search: { kind, ...await command(id, 'github', { query, limit: 8 }) } }
            if (kind === 'local') {
              const outcomes = await Promise.allSettled([
                command(id, 'tools', { query, offset: 0, limit: 30 }),
                (async () => remoteValue(await service('skills').list({ sessionId: id })))(),
              ])
              return { search: { kind, query,
                tools: outcomes[0].status === 'fulfilled' ? outcomes[0].value : null,
                skills: outcomes[1].status === 'fulfilled' ? rows(outcomes[1].value.skills)
                  .filter(skill => `${skill.name} ${skill.description || ''}`.toLowerCase().includes(query.toLowerCase())) : null,
                errors: outcomes.map((outcome, index) => outcome.status === 'rejected'
                  ? `${index === 0 ? 'Tools' : 'Skills'}: ${errorText(outcome.reason)}` : null).filter(Boolean),
              } }
            }
            const market = services.market
            if (!market) return { search: { kind: 'market', unavailable: true, reason: 'native_service_unavailable' } }
            let indexes
            try {
              indexes = remoteValue(await market.listIndexes())
            } catch (error) {
              const diagnostic = errorText(error)
              // The pinned native RPC reports an absent endpoint as this exact
              // transport failure. Do not relabel auth, timeouts or index errors.
              if (!/transport failure for \/api\/pluginMarket\/listIndexes: HTTP 404$/u.test(diagnostic)) throw error
              return { search: { kind: 'market', unavailable: true, reason: 'native_endpoint_unavailable', diagnostic } }
            }
            if (!rows(indexes).length) return { search: { kind: 'market', indexes: [], entries: [], unconfigured: true } }
            const index = indexUrl ? indexes.find(index => index.url === indexUrl) : indexes[0]
            if (!index) throw new Error('只能搜索已配置的市场 / Select a configured market')
            if (index.error) throw new Error(errorText(index.error))
            const found = remoteValue(await market.search(index.url, {
              text: query, sort: { kind: 'relevance' }, offset: 0, limit: 20,
            }))
            return { search: { kind: 'market', ...found, indexes } }
          }, true)
        },
        clearSource() {
          invalidate(['source'])
          publish({ source: null })
        },
        inspectSource(repository, ref) {
          controller.clearSource()
          return run('source', async id => ({ source: await command(id, 'inspect', {
            repository, ...(ref?.trim() ? { ref: ref.trim() } : {}),
          }) }), true)
        },
        inspectPackage(spec, registry) {
          if (state.busy.install || state.installUncertain) return
          publish({ inspection: null, installResult: null })
          return run('inspect', async () => {
            const options = registry ? { registry } : {}
            const result = remoteValue(await service('manager').inspect(spec, options))
            return { inspection: { spec, registry, result } }
          })
        },
        installPackage() {
          if (state.busy.install || state.installUncertain) return Promise.resolve()
          return run('install', async () => {
            const inspection = state.inspection
            if (inspection?.result?.status !== 'accepted') throw new Error('请先检查安装来源 / Inspect the package first')
            const manager = service('manager')
            const requestId = crypto.randomUUID()
            invalidate(['installRecovery', 'installCancel'])
            publish({ installId: requestId, installUncertain: true, installResult: null, installCancellation: null })
            const result = remoteValue(await manager.installBundle(inspection.spec, {
              enabled: false, saveExact: true, requestId,
              ...(inspection.registry ? { registry: inspection.registry } : {}),
            }))
            if (disposed || state.installId !== requestId || services.manager !== manager) return
            return { installResult: result, installUncertain: false }
          }).then(accepted => { if (accepted) return controller.loadPackages() })
        },
        recoverInstall() {
          return run('installRecovery', async () => {
            const requestId = state.installId, manager = service('manager')
            if (!requestId) throw new Error('没有本次安装记录 / No install request')
            const result = remoteValue(await manager.waitForInstall(requestId))
            if (disposed || state.installId !== requestId || services.manager !== manager) return
            if (!result) throw new Error('主机未找到本次记录；请核对 DSH 插件页，勿盲目重装 / No retained install result')
            invalidate(['install'])
            return { installResult: result, installUncertain: false }
          }).then(accepted => { if (accepted) return controller.loadPackages() })
        },
        cancelInstall() {
          return run('installCancel', async () => {
            const requestId = state.installId, manager = service('manager')
            if (!requestId) throw new Error('没有本次安装记录 / No install request')
            const result = remoteValue(await manager.cancelInstall(requestId))
            if (disposed || state.installId !== requestId || services.manager !== manager) return
            if (result?.status === 'cancelled') {
              invalidate(['install', 'installRecovery'])
              return { installCancellation: result, installUncertain: false, installResult: null }
            }
            return { installCancellation: result }
          })
        },
        changePackage(kind, identity, enabled) {
          if (state.busy.install || state.installUncertain) return Promise.resolve()
          return run('packageChange', async () => {
            const manager = service('manager')
            const result = remoteValue(await (kind === 'bundle'
              ? manager.setBundleEnabled(identity, enabled)
              : manager.setPluginEnabled(identity, enabled)))
            return { installResult: result }
          }).then(() => controller.loadPackages())
        },
        loadTasks(offset = 0, snapshot) {
          if (disposed || state.busy.tasks) return Promise.resolve(false)
          const observedEpoch = epoch, observedConnection = connectionEpoch
          const current = () => !disposed && epoch === observedEpoch && connectionEpoch === observedConnection
          publish({ tasksStale: state.tasks !== null })
          return run('tasks', async id => {
            const readSequence = ++connectionReadSequence
            const currentRead = () => current() && readSequence >= acceptedConnectionRead
            const tasks = await command(id, 'tasks', offset > 0 ? { offset, snapshot } : {}, currentRead)
              .catch(error => { if (currentRead()) throw error })
            if (!currentRead()) return
            return { tasks, tasksStale: false, ...acceptConnection(tasks, id, readSequence) }
          }, true)
        },
        taskStatus(task_ref, options = {}) {
          // Claim before publishing: invalidation resets busy, and a retained
          // click handler must not supersede an in-flight read of this task.
          if (disposed || (statusRead && statusRead.task_ref === task_ref)) return Promise.resolve(false)
          const request = { task_ref }
          statusRead = request
          const previous = state.taskStatus?.task_ref === task_ref ? state.taskStatus : null
          // Keep the mounted view on refresh/paging, but invalidate freshness
          // and downloads together before publishing any pending-read state.
          invalidate(['taskStatus', 'download'], {
            taskStatus: previous, taskStatusStale: previous !== null, download: null,
          })
          if (statusRead !== request) return Promise.resolve(false)
          const args = { task_ref }
          for (const key of ['after_event_id', 'after_message_id', 'artifact_offset']) {
            if (options[key] !== undefined) args[key] = options[key]
          }
          return run('taskStatus', async id => ({
            taskStatus: await command(id, 'status', args), taskStatusStale: false,
          }), true).finally(() => {
            if (statusRead === request) statusRead = null
          })
        },
        downloadArtifact(task_ref, artifact_id) {
          return run('download', async id => {
            if (state.busy.taskStatus || state.taskStatusStale) {
              throw new Error('状态非最新；请成功读取原任务后再下载 / Status is not current; read the task successfully before downloading')
            }
            const item = rows(state.taskStatus?.artifacts).find(row => row?.artifact_id === artifact_id)
            if (typeof task_ref !== 'string' || !/^[a-f0-9]{32}$/.test(task_ref)
              || typeof artifact_id !== 'string' || !/^[a-f0-9]{32}$/.test(artifact_id)
              || state.taskStatus?.task_ref !== task_ref || item?.download_eligible !== true) {
              throw new Error('只能下载当前任务列出的可下载文件；请重新读取状态 / Use an eligible artifact id from the current task')
            }
            publish({ download: null })
            // The existing human command reauthorizes ownership and checks the
            // exported bytes/hash. UI values never become URLs, roots or grants.
            const response = await ctx.sessions.using(id, { source: 'controllerOperation' }, async reference => {
              await reference.ready
              return remoteValue(await ctx.remote.commands.execute(id,
                `/legion download ${task_ref} ${artifact_id}`, []))
            })
            const result = response?.result
            if (result?.kind !== 'success' || typeof result.text !== 'string' || !result.text.trim()) {
              throw new Error(errorText(result?.text || '下载结果未确认；请看原对话回执 / Download outcome unconfirmed'))
            }
            return { download: { task_ref, artifact_id, text: errorText(result.text), truncated: result.text.length > 2000 } }
          }, true)
        },
        submit(query, context) {
          const observedEpoch = epoch, observedConnection = connectionEpoch
          const current = () => !disposed && epoch === observedEpoch && connectionEpoch === observedConnection
          return run('submit', async id => {
            if (!query.trim()) throw new Error('请写下想得到的成果 / Describe the outcome you need')
            // Read the existing authenticated recovery index before choosing a
            // key. Public config values cannot prove which credential is active.
            const readSequence = ++connectionReadSequence
            const currentRead = () => current() && readSequence >= acceptedConnectionRead
            const binding = await command(id, 'tasks', { limit: 1 }, currentRead)
              .catch(error => { if (currentRead()) throw error })
            if (!currentRead()) return
            publish(acceptConnection(binding, id, readSequence))
            if (!current()) {
              if (!disposed && state.sessionId === id && connectionId === binding.connection_id) {
                setError('submit', '核心身份已变化；本次未提交，请核对战队任务后重试 / Native identity changed; review Team tasks before retrying')
              }
              return
            }
            const connection = connectionId, owner = operationId(id, connection)
            const ownsView = () => current() && state.sessionId === id && connectionId === connection
            let operation = operations.get(owner)
            if (operation?.pending) throw new Error('原连接的提交仍在等待回执；不能并发补发 / Original submission is still pending')
            if (operation && (operation.query !== query || operation.context !== context)) {
              throw new Error('上一单仍有记录；请先查看原任务，或明确开始另一单 / Check the original task before starting another')
            }
            operation ||= { key: crypto.randomUUID(), query, context }
            operations.set(owner, operation)
            operation.pending = true
            publish({ operation })
            let receipt, failure
            try {
              receipt = await command(id, 'submit', { key: operation.key, query, ...(context ? { context } : {}),
                connection_id: connection }, ownsView)
            } catch (error) {
              receipt = error.receipt
              failure = error
            } finally {
              operation.pending = false
            }
            if (receipt && !disposed && operations.get(owner) === operation) {
              operation.ref = receipt.task_ref
              submissions.set(owner, receipt)
            }
            // Late replies still update their original recovery record, but
            // neither receipts nor failures may decorate a different connection.
            if (!disposed && state.sessionId === id && connectionId === connection && operations.get(owner) === operation) {
              publish({ ...operationView(id), errors: { ...state.errors, submit: failure ? errorText(failure) : null } })
            }
            if (failure) throw failure
          }, true)
        },
        newTask() {
          const owner = operationId(state.sessionId), operation = operations.get(owner)
          if (state.busy.submit || operation?.pending || (operation && !operation.ref)) return
          invalidate(['submit'])
          operations.delete(owner)
          submissions.delete(owner)
          publish({ operation: null, submission: null })
        },
      }
      return controller
    }

    const styles = `
      .legion-panel{--lp-bg:var(--dsw-alias-bg-base,#fff);--lp-card:var(--dsw-alias-bg-layer-1,#f8f9fb);--lp-border:var(--dsw-alias-border-l1,#e4e7ec);--lp-fg:var(--dsw-alias-label-primary,#20242d);--lp-muted:var(--dsw-alias-label-secondary,#647083);--lp-accent:var(--dsw-alias-brand-primary,#4866dd);height:100%;min-height:0;overflow:auto;background:var(--lp-bg);color:var(--lp-fg);font-family:inherit;font-size:14px;line-height:1.6}
      .legion-panel *{box-sizing:border-box}.legion-wrap{max-width:1120px;margin:auto;padding:36px 40px 64px}
      .legion-panel h1{font-size:30px;line-height:1.3;font-weight:650;letter-spacing:-.7px;margin:8px 0 12px}.legion-panel h2{font-size:18px;line-height:1.4;font-weight:620;margin:0}.legion-panel h3{font-size:15px;line-height:1.4;margin:0;font-weight:600}.legion-panel p{margin:7px 0}
      .legion-eyebrow{font-size:11px;letter-spacing:1.8px;font-weight:650;color:var(--lp-muted)}.legion-muted,.legion-help{color:var(--lp-muted)}.legion-help{font-size:12px;line-height:1.6}.legion-lead{max-width:690px;font-size:14px;color:var(--lp-muted)}
      .legion-top,.legion-row,.legion-toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.legion-top{display:grid;grid-template-columns:minmax(0,1fr) 230px;gap:26px;align-items:start}.legion-row.between{justify-content:space-between}.legion-toolbar{margin:18px 0}
      .legion-session{display:grid;gap:5px;min-width:230px;max-width:380px}.legion-panel button,.legion-panel input,.legion-panel select,.legion-panel textarea{font:inherit;color:inherit}.legion-panel button{cursor:pointer;border:1px solid var(--lp-border);border-radius:9px;padding:8px 13px;background:var(--lp-bg);line-height:1.4;min-height:36px}.legion-panel button:hover:not(:disabled):not([aria-disabled=true]){background:var(--dsw-alias-interactive-bg-hover,#f0f2f8)}.legion-panel button:disabled,.legion-panel button[aria-disabled=true]{cursor:not-allowed;opacity:.48}.legion-panel button.primary{background:var(--dsw-alias-button-primary-fill,#4866dd);color:var(--dsw-alias-label-primary-inverted,#fff);border-color:transparent}.legion-panel button.primary:hover:not(:disabled):not([aria-disabled=true]){background:var(--dsw-alias-button-primary-hover,#3c59ce)}
      .legion-panel :focus-visible{outline:2px solid var(--lp-accent);outline-offset:3px}.legion-panel input:not([type=checkbox]):not([type=radio]),.legion-panel select,.legion-panel textarea{background:var(--lp-bg);border:1px solid var(--lp-border);border-radius:9px;padding:9px 11px;min-width:0;width:100%}.legion-panel input[type=checkbox],.legion-panel input[type=radio]{width:17px;height:17px;accent-color:var(--lp-accent);flex:none}.legion-panel textarea{resize:vertical;min-height:110px}
      .legion-tabs{display:flex;gap:5px;overflow:auto;border-bottom:1px solid var(--lp-border);margin:27px 0 25px;padding-bottom:10px}.legion-tabs button{border:0;white-space:nowrap;background:none;color:var(--lp-muted)}.legion-tabs button[aria-selected=true]{background:var(--lp-card);color:var(--lp-fg);font-weight:600}
      .legion-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.legion-card{border:1px solid var(--lp-border);border-radius:14px;padding:19px;min-width:0;background:var(--lp-bg)}.legion-card.chosen{border-color:var(--lp-accent);background:var(--lp-card)}.legion-card .legion-title{display:flex;align-items:flex-start;gap:10px}.legion-title>div{min-width:0;flex:1}.legion-card p,.legion-task p,.legion-panel code,.legion-panel a{overflow-wrap:anywhere}.legion-card p.description{color:var(--lp-muted);font-size:13px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
      .legion-badges{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.legion-badge{font-size:11px;line-height:1.4;padding:4px 7px;background:var(--lp-card);border:1px solid var(--lp-border);border-radius:6px}.legion-badge.good{color:var(--lp-accent)}
      .legion-callout{border-left:3px solid var(--lp-accent);border-radius:4px;background:var(--lp-card);padding:13px 16px;margin:17px 0}.legion-error{border:1px solid var(--dsw-alias-border-l2,#b87566);background:var(--lp-card);border-radius:10px;padding:12px 15px;margin:12px 0;overflow-wrap:anywhere}.legion-empty{background:var(--lp-card);border:1px dashed var(--lp-border);border-radius:14px;padding:29px;color:var(--lp-muted);margin:16px 0}
      .legion-panel a{color:var(--lp-accent);text-decoration:underline;text-underline-offset:3px}.legion-panel code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px}.legion-panel details{border-top:1px solid var(--lp-border);padding-top:12px;margin-top:14px}.legion-panel summary{cursor:pointer;font-size:12px;color:var(--lp-muted)}.legion-panel pre{font-size:11px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;background:var(--lp-card);padding:12px;border-radius:8px}
      .legion-search{flex:1;min-width:160px}.legion-form{display:grid;gap:17px;max-width:720px}.legion-field{display:grid;gap:6px}.legion-form .legion-row>label{flex:1;min-width:180px}.legion-section{margin-top:28px}.legion-list{display:grid;gap:10px}.legion-result-count{font-size:12px;color:var(--lp-muted);margin:10px 0}.legion-task{padding:15px;border:1px solid var(--lp-border);border-radius:10px}.legion-task code{display:block}.legion-panel .legion-busy{font-size:12px;color:var(--lp-muted)}
      .legion-steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0 0 24px}.legion-steps button{text-align:left;padding:12px 14px;background:var(--lp-card)}.legion-steps button[aria-current=step]{border-color:var(--lp-accent);color:var(--lp-accent);font-weight:600}.legion-team-lead{background:var(--lp-card);border:1px solid var(--lp-border);border-radius:14px;padding:18px 20px;margin:16px 0}.legion-step-actions{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:26px;padding:14px 0;border-top:1px solid var(--lp-border);position:sticky;bottom:0;z-index:2;background:var(--lp-bg)}.legion-choice{display:flex;align-items:flex-start;gap:11px;padding:18px;border:1px solid var(--lp-border);border-radius:12px;cursor:pointer}.legion-choice.chosen{border-color:var(--lp-accent);background:var(--lp-card)}.legion-choice input{flex:none;margin-top:5px;accent-color:var(--lp-accent)}.legion-choice strong{display:block}.legion-choice p{margin:4px 0 0}.legion-flow-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}.legion-flow-time{font-variant-numeric:tabular-nums}.legion-team-summary{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 20px}
      .legion-crew{margin:22px 0}.legion-crew .legion-grid{margin-top:12px}.legion-crew .legion-card{border-top:3px solid var(--lp-accent)}.legion-crew .legion-toolbar{margin:12px 0 0}.legion-crew .legion-eyebrow{letter-spacing:.8px;margin-bottom:8px}.legion-crew code{display:block;overflow-wrap:anywhere}.legion-member-page{padding:12px 0;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
      @media(max-width:760px){.legion-wrap{padding:22px 18px 45px}.legion-grid{grid-template-columns:1fr}.legion-panel h1{font-size:25px}.legion-session{max-width:100%;width:100%;min-width:0}.legion-top{grid-template-columns:minmax(0,1fr);gap:20px}.legion-card{padding:16px}.legion-toolbar{gap:9px}.legion-tabs{margin-top:22px}.legion-form .legion-row>label{min-width:100%}}
      @media(max-width:440px){.legion-steps{gap:5px}.legion-steps button{padding:10px 8px;font-size:12px}.legion-step-actions>button{flex:1}.legion-team-lead{padding:15px}}
      @media(prefers-reduced-motion:reduce){.legion-panel *{scroll-behavior:auto}}
    `

    function registerPanel(ctx) {
      const React = require('react')
      const h = React.createElement
      const t = (zh, en) => ctx.locale.resolveText({ zh, en })
      const controller = createController(ctx)
      ctx.effect(() => () => controller.dispose(), 'legion workbench state')
      for (const [key, dependencies, resolve] of [
        ['manager', ['remote.pluginManager'], child => child.remote.pluginManager],
        ['market', ['remote.pluginMarket'], child => child.remote.pluginMarket],
        ['skills', ['remote.skills'], child => child.remote.skills],
        ['navigation', ['uiWorkspace'], child => child.uiWorkspace],
        ['models', ['remote.session'], child => child.remote.session],
        ['configuration', ['configForms', 'remote.pluginInventory', 'remote.pluginManager', 'remote.settings'],
          child => ({ forms: child.configForms, inventory: child.remote.pluginInventory,
            manager: child.remote.pluginManager, settings: child.remote.settings })],
      ]) {
        ctx.inject(dependencies, child => {
          child.effect(() => controller.attach(key, resolve(child)), `legion ${key} service`)
        })
      }
      const titleOf = value => typeof value === 'string' ? value
        : value ? ctx.locale.resolveText(value) : ''
      const Button = ({ children, onClick, disabled, primary, ...props }) =>
        h('button', { type: 'button', onClick, disabled, className: primary ? 'primary' : '', ...props }, children)
      function TaskReadButton({ children, onClick, busy = false, disabled = false, ...props }) {
        const blocked = busy || disabled
        const activationKey = event => {
          if (event.key !== 'Enter' && event.key !== ' ') return
          // Let the native button generate one click, not a Host page shortcut.
          event.stopPropagation()
          if (blocked || event.repeat) event.preventDefault()
        }
        // Keep the user's focus on the same control; never restore it later.
        return h(Button, { ...props, 'aria-disabled': blocked, 'aria-busy': busy,
          onKeyDown: activationKey, onKeyUp: activationKey,
          onClick: event => {
            if (blocked || event?.defaultPrevented) {
              event?.preventDefault()
              event?.stopPropagation()
              return
            }
            return onClick(event)
          } }, children)
      }
      const ErrorBox = ({ value }) => value ? h('div', { className: 'legion-error', role: 'alert' }, value) : null
      const Details = ({ value, label }) => value ? h('details', null,
        h('summary', null, label || t('查看来源与原始回执', 'Source and original receipt')),
        h('pre', null, JSON.stringify(value, null, 2))) : null
      const Empty = ({ children }) => h('div', { className: 'legion-empty' }, children)
      const PublicLink = ({ value, children }) => {
        const safe = publicLink(value)
        return safe ? h('a', { href: safe, target: '_blank', rel: 'noopener noreferrer' }, children) : null
      }
      const Badge = ({ children, good }) => h('span', { className: `legion-badge${good ? ' good' : ''}` }, children)
      const Field = ({ title, hint, children }) => h('label', { className: 'legion-field' },
        h('span', null, title), children, hint && h('span', { className: 'legion-help' }, hint))
      function Outcome({ result }) {
        if (!result) return null
        const facts = changeFacts(result)
        return h('div', { className: 'legion-callout', role: 'status' },
          h('strong', null, ({
            applied: t('原生操作已应用', 'Native operation applied'),
            'restart-required': t('已保存，需要重启 DSH 才能生效', 'Saved; DSH restart required'),
            overridden: t('设置被其他配置覆盖', 'Overridden by another configuration'),
            failed: t('操作失败，请看具体原因', 'Operation failed; see the reason'),
            cancelled: t('安装已取消并恢复文件', 'Installation cancelled and files restored'),
          })[facts.application] || t('结果待确认', 'Outcome unconfirmed')),
          h('p', { className: 'legion-help' }, `${facts.stage} · ${result.target || result.bundle || ''}`,
            facts.reason && ` · ${facts.reason}`),
          h('p', { className: 'legion-help' }, t(
            '安装、启用和真正执行是三件事。下一步查看当前会话工具，再让 DSH 实际使用。',
            'Installation, activation and execution are separate. Refresh session tools, then use the capability in DSH.')),
          ...facts.warnings.map((value, index) => h('p', { key: index }, value)),
          result.pendingBuilds?.length ? h('p', null, t('待审批安装脚本：', 'Build scripts await approval: '),
            result.pendingBuilds.join(', '), t('。请在 DSH 原生插件页处理。', '. Use the native DSH Plugins page.')) : null,
          h(Details, { value: result }))
      }
      function TaskObservations({ value, sessionId, download, downloadBusy, downloadError, statusBusy, statusStale }) {
        const notCurrent = statusBusy || statusStale
        const artifacts = artifactFacts(value)
        const artifactPage = value.sections?.artifacts
        const eventPage = value.sections?.events
        const after = eventPage?.after_event_id ?? 0
        const eventNext = eventPage?.next_after_event_id
        const moreArtifacts = Number.isSafeInteger(artifactPage?.next_offset)
          && artifactPage.next_offset > (artifactPage.offset ?? 0)
        const moreEvents = eventPage?.has_more === true && Number.isSafeInteger(eventNext) && eventNext > after
        const unknown = t('未提供 / 未确认', 'Not provided / unconfirmed')
        const notes = rows(value.export_observations).filter(record)
        const runtimeTasks = rows(value.runtime?.tasks).filter(record)
        return h('section', { className: 'legion-section' },
          h('div', { className: 'legion-row between' },
            h('h2', null, t('原任务现场', 'Original task observations')),
            h(TaskReadButton, { busy: statusBusy, onClick: () => controller.taskStatus(value.task_ref) }, t('刷新原任务', 'Refresh original task'))),
          notCurrent ? h('p', { className: 'legion-help', role: 'status' }, statusBusy
            ? t('正在读取最新状态；下方保留上次读取结果（非最新），暂不可下载。',
              'Reading the latest status; previous observations below are not current. Downloads are disabled.')
            : t('上次读取结果未更新；仅供参考，成功读取原任务后才能下载。',
              'Previous observations are not updated; reference only. Read the task successfully before downloading.')) : null,
          h('p', { className: 'legion-help' }, t('DSH 会话：', 'DSH session: '), h('code', null, sessionId)),
          h('p', null, t('任务引用：', 'Task reference: '), h('code', null, value.task_ref || unknown)),
          h('p', { className: 'legion-help' }, t('军团任务组：', 'Legion team: '), h('code', null, value.team_id || unknown),
            ' · ', t('连接状态：', 'Connection: '), value.connection || unknown),
          value.truncated ? h('div', { className: 'legion-callout', role: 'status' }, t(
            '当前为脱敏、限长摘要，部分原始字段已省略或截断。各区独立展示；没展示全不等于不存在。',
            'This is a redacted, bounded summary. Some fields are omitted or clipped. Sections are independent; missing does not mean absent.')) : null,
          h('h3', null, t('运行状态（不等于导出或体验验收）', 'Runtime (not export or experience acceptance)')),
          !runtimeTasks.length ? h('p', null, t('运行状态尚未完整取得。', 'Runtime observations are not fully available.')) : null,
          h('div', { className: 'legion-grid', style: { marginTop: 14 } }, ...runtimeTasks.map((task, index) =>
            h('article', { className: 'legion-card', key: task.task_id || index },
              h('h3', null, task.harness || task.assigned_harness || task.task_id || 'Task'),
              h('p', null, t('运行状态：', 'Runtime: '), task.status || unknown),
              h('p', { className: 'legion-help' }, typeof task.final === 'string' ? task.final
                : typeof task.result?.final === 'string' ? task.result.final : t('未提供作者结果文字。', 'No author result text provided.')),
              task.result?.error ? h('p', null, t('运行错误：', 'Runtime error: '), String(task.result.error)) : null))),
          typeof value.runtime?.cancelled === 'boolean' ? h('p', { className: 'legion-help' },
            t('服务取消标记（非进程核验）：', 'Service cancellation flag (not a process check): '), String(value.runtime.cancelled)) : null,
          h('div', { className: 'legion-callout' },
            h('strong', null, artifacts.complete
              ? notCurrent
                ? t(`上次读取的成果记录：${artifacts.observed_count}`, `Previously observed artifact records: ${artifacts.observed_count}`)
                : t(`本次已观察到的成果记录：${artifacts.observed_count}`, `Observed artifact records: ${artifacts.observed_count}`)
              : t('成果清单未完整取得', 'Artifact inventory is incomplete')),
            !artifacts.complete && artifacts.items.length ? h('p', null,
              t(`当前页展示 ${artifacts.items.length} 条已观察记录。`, `Showing ${artifacts.items.length} observed records on this page.`)) : null,
            !artifacts.complete && !artifacts.items.length ? h('p', null,
              t('当前没有可展示的完整记录，不能据此认定没有成果。', 'No complete rows are available here; this does not establish that there are no artifacts.')) : null,
            h('p', { className: 'legion-help' }, t(
              '同名文件可能有不同 SHA-256 的历史记录，按原记录保留，不合并成“唯一新成品数”。文件与作者 done 都不等于体验已通过。',
              'Same-name files may have historical SHA-256 versions. Records are preserved, not merged into a count of new products. Files and author done are not experience acceptance.'))),
          ...artifacts.items.map((item, index) => {
            const savedVersion = typeof item.export_snapshot_id === 'string' && item.export_snapshot_id.trim()
            // Legacy service receipts have no typed source field. Recognize only
            // their exact current-file observation; kind/eligibility is not provenance.
            const legacyFile = !item.export_snapshot_id && item.service_verification === '文件存在、非空且内容哈希已计算'
            return h('article', { className: 'legion-task', key: item.artifact_id || index },
            h('div', { className: 'legion-row between' }, h('strong', null, item.name || unknown),
              item.download_eligible === true ? h(Button, { disabled: downloadBusy || notCurrent,
                onClick: () => controller.downloadArtifact(value.task_ref, item.artifact_id) }, t('下载文件', 'Download file')) : null),
            h('code', null, item.artifact_id || unknown),
            h('p', { className: 'legion-help' }, t('记录来源：', 'Record source: '), savedVersion
              ? t('已保存的交付版本', 'Saved delivery version')
              : legacyFile
                ? t('工作目录文件记录（可能随续作变化）', 'Working-directory file record (may change as work continues)')
                : t('来源未确认', 'Source unconfirmed')),
            savedVersion ? h('p', { className: 'legion-help' }, t('交付版本 ID：', 'Delivery version ID: '),
              h('code', null, item.export_snapshot_id)) : null,
            typeof item.kind === 'string' && item.kind.trim() ? h('p', { className: 'legion-help' },
              t('服务声明类型：', 'Service-reported kind: '), item.kind) : null,
            typeof item.service_verification === 'string' && item.service_verification.trim()
              ? h('p', { className: 'legion-help' }, t('服务观察（非体验验收）：', 'Service observation (not experience acceptance): '),
                item.service_verification) : null,
            h('p', { className: 'legion-help' }, 'SHA-256: ', item.sha256 || unknown),
            h('p', { className: 'legion-help' }, item.download_eligible === true
              ? notCurrent
                ? t('状态非最新，暂不可下载；请先成功读取原任务。', 'Status is not current; download is disabled until a successful task read.')
                : t('可请求下载；正式入口会再次校验归属、字节与哈希。', 'Download may be requested; the native entry rechecks ownership, bytes and hash.')
              : item.download_reason || t('下载资格未确认。', 'Download eligibility unconfirmed.')))
          }),
          h('div', { className: 'legion-row', key: 'artifact-pages' },
            h(TaskReadButton, { busy: statusBusy, disabled: !(artifactPage?.offset > 0),
              onClick: () => controller.taskStatus(value.task_ref, { after_event_id: after }) }, t('返回成果第一页', 'First artifact page')),
            h(TaskReadButton, { busy: statusBusy, disabled: !moreArtifacts,
              onClick: () => controller.taskStatus(value.task_ref, { after_event_id: after, artifact_offset: artifactPage.next_offset }) },
            t('读取下一页成果记录', 'Next artifact page'))),
          h(ErrorBox, { value: downloadError }),
          download && download.task_ref === value.task_ref ? h('div', { className: 'legion-callout', role: 'status' },
            h('strong', null, t('下载命令回执（文件保存到 DSH 主机）', 'Download command receipt (file saved on the DSH host)')),
            h('pre', null, download.text),
            download.truncated ? h('p', null, t('此回执也已截断；完整回执见原对话。', 'This receipt is clipped too; see the original conversation.')) : null,
            h('p', { className: 'legion-help' }, t('下载不代表作品体验已验收。', 'Download is not experience acceptance.'))) : null,
          h('h3', null, t('导出观察与拒绝原因', 'Export observations and refusal reasons')),
          ...notes.map((note, index) => h('div', { className: 'legion-task', key: index },
            h('p', null, note.name || t('导出观察', 'Export observation'), ' · ', note.reason || note.status
              || (Number.isSafeInteger(note.exported_files) ? `exported_files=${note.exported_files}` : unknown)),
            h('p', { className: 'legion-help' }, note.scan_status || '',
              note.file_preserved === true ? t(' · 原文件保留（服务观察）', ' · Source file preserved (service observation)') : ''),
            h('code', null, note.source_path))),
          !notes.length ? h('p', { className: 'legion-help' }, value.sections?.exports?.complete === true
            ? t('本次回执未列出额外导出原因。', 'No additional export reasons were listed in this receipt.')
            : t('导出原因未完整取得，不能据此确认没有拒绝。', 'Export reasons are incomplete; absence of refusals is not established.')) : null,
          notes.length > 0 && value.sections?.exports?.complete !== true ? h('p', { className: 'legion-help' }, t(
            '以上仅为本页已取得的导出观察，尚不是完整原因清单。',
            'These are the export observations available on this page, not a complete list of reasons.')) : null,
          h('h3', null, t('事件与续读', 'Events and continuation')),
          h('p', null, t('服务事件游标：', 'Service event cursor: '),
            Number.isSafeInteger(value.next_event_id) ? value.next_event_id : unknown),
          h('p', { className: 'legion-help' }, t('服务还有更多事件：', 'More service events: '),
            typeof value.more_events === 'boolean' ? String(value.more_events) : unknown,
            ' · ', t('本页续读游标：', 'View continuation cursor: '), eventNext ?? unknown),
          h(TaskReadButton, { key: 'event-page', busy: statusBusy, disabled: !moreEvents,
            onClick: () => controller.taskStatus(value.task_ref, { after_event_id: eventNext }) }, t('读取后续事件', 'Read later events')),
          h('p', { className: 'legion-help' }, t('更多进度可在原会话查看：', 'For more progress use the original conversation: '),
            h('code', null, `/legion status ${value.task_ref} ${after}`)),
          h(Details, { value, label: t('查看脱敏摘要、来源与分区截断事实', 'Redacted summary, sources and section truncation facts') }))
      }
      function Page() {
        const state = React.useSyncExternalStore(controller.subscribe, controller.getSnapshot)
        const [tab, setTab] = React.useState('members')
        const [memberFilter, setMemberFilter] = React.useState('')
        const [candidateDraft, setCandidates] = React.useState([])
        const [candidateVersion, setCandidateVersion] = React.useState()
        const [candidateDirty, setCandidateDirty] = React.useState(false)
        const [teamDraft, setTeamDraft] = React.useState(teamDefaults)
        const [teamDirty, setTeamDirty] = React.useState(false)
        const [teamStep, setTeamStep] = React.useState(0)
        const [editingTeam, setEditingTeam] = React.useState(false)
        const [query, setQuery] = React.useState('')
        const [sourceKind, setSourceKind] = React.useState('local')
        const [marketIndex, setMarketIndex] = React.useState('')
        const [spec, setSpec] = React.useState('')
        const [registry, setRegistry] = React.useState()
        const [repository, setRepository] = React.useState('')
        const [ref, setRef] = React.useState('')
        const [goal, setGoal] = React.useState('')
        const [configDraft, setConfigDraft] = React.useState({ baseUrl: '', tokenEnv: '', tokenFile: '' })
        const [configVersion, setConfigVersion] = React.useState()
        const [configDirty, setConfigDirty] = React.useState(false)
        const [packageFilter, setPackageFilter] = React.useState('')
        const [taskRead, setTaskRead] = React.useState(null)
        const taskStatusRegion = React.useRef(null)
        React.useEffect(() => {
          void controller.refreshSessions()
        }, [])
        React.useEffect(() => {
          if (!state.sessionId && state.sessions.ids.length === 1) {
            controller.selectSession(state.sessions.ids[0])
            void controller.loadMembers()
          }
        }, [state.sessions.ids.join('|'), state.sessionId])
        React.useEffect(() => { if (state.available.configuration) void controller.loadConfig() }, [state.available.configuration])
        React.useEffect(() => { if (state.available.manager) void controller.loadPackages() }, [state.available.manager])
        React.useEffect(() => {
          if (state.config?.status !== 'ready') return
          const value = state.config.value || {}
          if (!candidateDirty) {
            setCandidates(rows(value.candidateHarnesses))
            setCandidateVersion({ entryId: state.configEntry, namespace: state.configNamespace, revision: state.config.revision })
          }
          if (!teamDirty) setTeamDraft({ ...teamDefaults(), ...value.teamProfile })
          if (!configDirty) {
            setConfigDraft({ baseUrl: value.baseUrl || '', tokenEnv: value.tokenEnv || '', tokenFile: value.tokenFile || '' })
            setConfigVersion({ entryId: state.configEntry, namespace: state.configNamespace, revision: state.config.revision })
          }
        }, [state.config, state.configEntry, state.configNamespace, candidateDirty, configDirty, teamDirty])
        React.useEffect(() => {
          if (state.sessionId && state.config?.status === 'ready') void controller.loadFlow()
        }, [state.sessionId, state.config?.revision, state.configEntry])
        React.useEffect(() => {
          if (state.sessionId && state.config?.status === 'ready') void controller.loadMembers()
        }, [state.sessionId, state.config?.revision, state.configEntry, state.configNamespace])
        React.useEffect(() => {
          if (state.sessionId && state.available.models) void controller.loadModelStatus()
        }, [state.sessionId, state.available.models, state.flow])
        React.useEffect(() => {
          if (!taskRead) return
          if (tab !== 'tasks' || taskRead.sessionId !== state.sessionId) {
            setTaskRead(null)
            return
          }
          if (!taskRead.settled) return
          setTaskRead(null)
          if (state.busy.taskStatus || (!state.errors.taskStatus && state.taskStatus?.task_ref !== taskRead.taskRef)) return
          // Only an explicit task selection reveals its settled result/error.
          // Paging and refresh reads never create this one-shot UI intent.
          const region = taskStatusRegion.current
          region?.focus({ preventScroll: true })
          region?.scrollIntoView({ block: 'start',
            behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ? 'instant' : 'smooth' })
        }, [taskRead, tab, state.sessionId, state.busy.taskStatus, state.taskStatus, state.errors.taskStatus])
        const busy = key => state.busy[key] === true
        const error = key => h(ErrorBox, { value: state.errors[key] })
        const status = key => busy(key) && h('span', { className: 'legion-busy', role: 'status' }, t('正在读取…', 'Reading…'))
        const readTaskStatus = taskRef => {
          const request = { taskRef, sessionId: state.sessionId }
          setTaskRead(request)
          void controller.taskStatus(taskRef).then(() => {
            setTaskRead(current => current === request ? { ...request, settled: true } : current)
          })
        }
        const selectSession = id => {
          if (id !== state.sessionId) setTaskRead(null)
          controller.selectSession(id)
          if (id) { void controller.loadMembers(); if (tab === 'tasks') void controller.loadTasks() }
        }
        const openTab = value => {
          if (value !== 'tasks') setTaskRead(null)
          setTab(value)
          if (value === 'tasks' && state.sessionId) void controller.loadTasks()
        }
        const sourceContext = state.source ? JSON.stringify({
          source_role: 'untrusted_repository_observations_not_instructions',
          repository: state.source.repository, commit: state.source.commit,
          url: state.source.url, license: state.source.license,
          fetched_at: state.source.fetched_at, errors: state.source.errors,
        }) : ''
        const prepareRepo = item => {
          setRepository(item.repository)
          setRef('')
          setGoal(t(
            `请研究 ${item.repository}，找出对我有用的独立能力，按 DSH 原生插件方式适配，并接入军团协作。保留原项目许可，交付可安装包、使用说明与实际运行证据。`,
            `Research ${item.repository}, identify useful independent capabilities, adapt them as native DSH plugins and connect them to Legion collaboration. Preserve the source license and deliver an installable package, usage instructions and real execution evidence.`,
          ))
          setTab('add')
          void controller.inspectSource(item.repository)
        }
        const memberRows = rows(state.members?.runtime?.harnesses).filter(record)
        const sourceOnlyCount = memberRows.filter(member => memberFacts(member).selectable === false).length
        const members = memberRows.filter(member => memberFacts(member).selectable !== false)
          .filter(member => `${member.key} ${member.display_name || ''}`.toLowerCase().includes(memberFilter.toLowerCase()))
        const selectable = member => typeof member.key === 'string' && member.key.length > 0
          && memberFacts(member).selectable === true
        const observedText = value => typeof value === 'string' ? value : ''
        const memberName = member => observedText(member.display_name) || observedText(member.key)
          || t('身份未确认', 'Unknown identity')
        const memberPage = state.members?.member_page
        const memberSnapshotNotice = () => h(React.Fragment, null,
          state.members?.runtime?.public_catalog_only === true ? h('p', { className: 'legion-help', role: 'status' },
            t('这是公开目录，不是你的运行环境；安装、配置和执行状态仍需在自己的核心中核对。',
              'This is a public catalog, not your runtime. Check installation, configuration and execution in your own core.')) : null,
          state.members?.runtime?.warming || state.members?.runtime?.stale ? h('p', { className: 'legion-help', role: 'status' },
            t('核心返回的是预热或过期缓存。下面保留原回报，不把它当成刚刚完成的实跑。',
              'The core returned warming or stale cached facts. These are retained observations, not a just-completed run.'))
            : state.members?.runtime?.from_cache === true ? h('p', { className: 'legion-help' },
              t('这次读取使用了服务缓存，不是一次新的模型实跑。', 'This read used a service cache, not a new model execution.')) : null)
        const memberConfiguration = member => {
          const modelName = observedText(member.model?.display_name) || observedText(member.model?.model)
          const reason = member.model?.configuration_error == null ? ''
            : errorText(typeof member.model.configuration_error === 'string'
              ? member.model.configuration_error : JSON.stringify(member.model.configuration_error))
          return h(React.Fragment, null,
          h('p', { className: 'legion-help' }, bool(member.model?.configured) === false
            ? t('核心回报：模型未配置', 'Core reports: model not configured')
            : bool(member.model?.configured) === true
              ? t('核心回报：模型已配置', 'Core reports: model configured')
              : t('核心模型配置尚未确认', 'Core model configuration is unknown')),
          modelName ? h('p', { className: 'legion-help' }, t('核心侧模型：', 'Core-side model: '), modelName) : null,
          reason ? h('code', null, reason) : null,
          member.model?.configured === false ? h('p', { className: 'legion-help' },
            t('请在核心的原生配置入口核对模型；这里的 DSH 模型不会自动替核心配好。',
              'Check the model in the core’s native settings. Choosing a DSH model does not configure this member.')) : null,
          h('p', { className: 'legion-help' }, member.readiness?.verified === true
            ? t('核心记录过该成员的执行验证；是否对应当前配置及本次任务仍需核对。',
              'The core has execution verification for this member. Its applicability to the current configuration and task still needs checking.')
            : t('本次未取得执行验证记录；不等于已经失败。', 'No execution verification was obtained in this read; that is not a recorded failure.')),
          member.readiness?.historical_verified === true ? h('p', { className: 'legion-help' },
            t('历史成功不代表当前配置成功。', 'Historical success does not establish success with the current configuration.')) : null)
        }
        const renderCrew = () => {
          const keys = rows(state.config?.value?.candidateHarnesses)
          const selected = rows(state.members?.selected?.items).filter(record)
          const selectedRows = keys.map(key => selected.find(item => item.key === key) || { key, observed: false })
          return h('section', { className: 'legion-crew', 'aria-label': t('战队当前观察', 'Current team observations') },
            h('div', { className: 'legion-row between' },
              h('h3', null, t('你的战队，准备得怎样？', 'How is your team configured?')),
              h(Button, { disabled: !state.sessionId || busy('members') || busy('modelStatus'),
                onClick: () => { void controller.loadMembers(); if (state.available.models) void controller.loadModelStatus() } },
                t('刷新战队状态', 'Refresh team facts'))),
            h('p', { className: 'legion-help' }, t('下面是配置与服务回报，不是实时参战名单。真正做了什么，在原会话和成果里看。',
              'These are configuration and service observations, not a live participation roster. Inspect contributions in the original conversation and artifacts.')),
            memberSnapshotNotice(), error('members'),
            h('div', { className: 'legion-grid' },
              h('article', { className: 'legion-card' },
                h('div', { className: 'legion-eyebrow' }, t('你的主场', 'YOUR HOME BASE')),
                h('h3', null, t('DSH · 原生作者', 'DSH · Native author')),
                h('p', { className: 'legion-help' }, state.modelStatus?.available === true
                  ? t('会话模型已选，并在原生目录中可用。', 'The selected session model is listed in the native catalog.')
                  : state.modelStatus?.available === false
                    ? t('当前会话尚无可用模型。', 'No usable model in this conversation.')
                    : t('会话模型状态尚未取得。', 'Session model status has not been read.')),
                state.modelStatus?.model ? h('code', null, state.modelStatus.model) : null,
                h('p', { className: 'legion-help' }, t('沿用你的工作区、工具和权限。模型可用不代表任务成功，也不代表下面的战友已配好。',
                  'Keeps your workspace, tools and permissions. Availability is not task success or configuration of the teammates below.')),
                h('div', { className: 'legion-toolbar' },
                  h(Button, { primary: true, disabled: !state.sessionId || !state.available.navigation,
                    onClick: () => controller.openFlowSession() }, t('打开工作会话，看实际结果', 'Open conversation and results')))),
              ...selectedRows.map(member => h('article', { className: 'legion-card', key: member.key },
                h('div', { className: 'legion-eyebrow' }, t('你选的核心侧战友', 'YOUR SELECTED CORE TEAMMATE')),
                h('h3', null, memberName(member)),
                member.observed !== true ? h('p', { className: 'legion-help' },
                  t('本次未观察到这位战友的状态；不据此认定未安装或不可用。',
                    'This teammate was not observed in the current response; that does not mean absent or unavailable.'))
                  : memberConfiguration(member),
                h('div', { className: 'legion-toolbar' },
                  h(Button, { onClick: () => openTab('config') }, t('查看核心连接', 'View core connection')),
                  member.model?.configured === false ? h('a', {
                    href: 'https://github.com/seanliii/dsh-plugin-legion/blob/main/docs/CORE.md#3-配置本人模型',
                    target: '_blank', rel: 'noopener noreferrer',
                  }, t('核心模型配置方法', 'Core model setup guide')) : null,
                  h(Button, { onClick: () => { setEditingTeam(true); setTeamStep(0) } },
                    t('查看或更换战友', 'Inspect or change companions'))))),
              !keys.length ? h('article', { className: 'legion-card' },
                h('div', { className: 'legion-eyebrow' }, t('按任务组织', 'ORGANIZED FOR THE TASK')),
                h('h3', null, t('让军团自己选人', 'Let Legion choose')),
                h('p', { className: 'legion-help' }, t('你没有固定候选。军团根据目标和可用能力选人；这不表示目录里的成员都已参与。',
                  'No fixed candidates. Legion chooses for the goal and available capabilities; catalog entries are not proof of participation.')),
                h(Button, { onClick: () => { setEditingTeam(true); setTeamStep(0) } },
                  t('查看可加入的战友', 'Explore companions'))) : null),
            state.members?.selected?.complete === false ? h('p', { className: 'legion-help' },
              t('这里是精简回报；未取得的配置或执行事实保持未知。',
                'These are compact observations. Missing configuration or execution facts remain unknown.')) : null)
        }
        const toggleMember = (key, checked) => {
          setCandidates(previous => checked ? [...new Set([...previous, key])] : previous.filter(value => value !== key))
          setCandidateDirty(true)
        }
        const renderMembers = () => h(React.Fragment, null,
          h('div', { className: 'legion-row between' },
            h('div', null, h('h2', null, t('选择谁与你并肩作战', 'Choose who works alongside you')),
              h('p', { className: 'legion-help' }, t('候选留空，由军团选人。勾选只改变偏好，不安装成员，也不增加权限。', 'Leave candidates empty for Legion to choose. Selection is a preference, not installation or authorization.'))),
            h(Button, { disabled: !state.sessionId || busy('members'), onClick: () => controller.loadMembers() }, t('刷新成员', 'Refresh members'))),
          h('div', { className: 'legion-team-lead' }, h('strong', null, t('DSH · 你的主场', 'DSH · Your home base')),
            h('p', { className: 'legion-help' }, t('战队从这里出发，沿用你的模型、工具、工作区和权限。其他成员可以加入同一个目标，分工、互查、互相借力。',
              'Your models, tools, workspace and permissions stay here. Other members can join the same goal, share work and check one another.'))),
          state.errors.members ? h('p', { className: 'legion-help' }, t(
            '暂时无法读取军团成员，请到「连接与配置」核对地址和服务；找能力和 DSH 能力包仍可用。',
            'Legion members could not be read. Check the address and service under Connection; Discover and DSH capability packages remain available.',
          )) : null,
          error('members'), status('members'),
          memberRows.length !== rows(state.members?.runtime?.harnesses).length
            || memberRows.some(member => typeof member.key !== 'string' || !member.key) ? h('p', { className: 'legion-help', role: 'status' },
              t('部分记录缺少身份或记录无法展示；未据此新增战友或判断可用性。',
                'Some records have missing identities or cannot be displayed; they do not create companions or establish availability.')) : null,
          !state.sessionId ? h(Empty, null, t('先在右上角选一个会话，成员和任务就会归到这段对话里。', 'Select a session above to keep members and tasks in the right conversation.')) : null,
          memberSnapshotNotice(),
          h('div', { className: 'legion-toolbar' },
            h('div', { className: 'legion-search' }, h('input', { value: memberFilter,
              onChange: event => setMemberFilter(event.target.value), placeholder: t('筛选本页成员', 'Filter this page'), 'aria-label': t('筛选成员', 'Filter members') })),
            h(Button, { disabled: busy('configSave'), onClick: () => { setCandidates([]); setCandidateDirty(true) } }, t('交给军团选人', 'Let Legion choose')),
            h(Button, { onClick: () => openTab('add') }, t('添加新的战友或工具', 'Add a companion or tool'))),
          error('configSave'), error('config'),
          candidateDraft.length ? h('p', { className: 'legion-help' }, t('当前候选：', 'Candidates: '), candidateDraft.join(', ')) : null,
          state.members && !members.length ? h(Empty, null, t('本次没有匹配的成员；这不代表其他成员已安装或可用。', 'No matching members in this observation.')) : null,
          h('div', { className: 'legion-grid legion-member-grid' }, ...members.map(member => {
            const facts = memberFacts(member)
            const selected = candidateDraft.includes(member.key)
            return h('article', { key: member.key, className: `legion-card${selected ? ' chosen' : ''}` },
              h('label', { className: 'legion-title' },
                h('input', { type: 'checkbox', checked: selected, disabled: !selectable(member) || !state.config?.writable || busy('configSave'),
                  'aria-label': `${t('选择成员', 'Select member')} ${memberName(member)}`,
                  onChange: event => toggleMember(member.key, event.target.checked) }),
                h('div', null, h('h3', null, memberName(member)), h('code', { className: 'legion-muted' }, observedText(member.key)))),
              h('div', { className: 'legion-badges' },
                h(Badge, null, facts.installed === true ? t('已安装', 'Installed') : facts.installed === false ? t('未安装', 'Not installed') : t('安装状态未知', 'Installation unknown')),
                h(Badge, { good: facts.connected === true }, facts.connected === true ? t('连接已观察', 'Connection observed') : facts.connected === false ? t('尚未连接', 'Not connected') : t('连接未知', 'Connection unknown')),
                facts.selectable === null ? h(Badge, null, t('执行身份未知', 'Execution role unknown')) : null),
              memberConfiguration(member),
              h(Details, { value: { ...facts, model: member.model, readiness: member.readiness, entry_points: member.entry_points,
                catalog_description_not_current_verification: member.profile?.intro || member.description } }))
          })),
          memberPage ? h('div', { className: 'legion-member-page', 'aria-label': t('成员分页', 'Member pages') },
            h('span', { className: 'legion-help' },
              t(`本页 ${memberPage.shown_count ?? 0} 条目录记录 / 本次返回 ${memberPage.total ?? '未知'} 条；不是已安装数量。`,
                `${memberPage.shown_count ?? 0} catalog records on this page / ${memberPage.total ?? 'unknown'} observed; not an installation count.`)),
            h('div', { className: 'legion-row' },
              h(Button, { disabled: busy('members') || !(memberPage.offset > 0),
                onClick: () => { setMemberFilter(''); void controller.loadMembers() } },
                t('返回第一页成员', 'Back to first members')),
              h(Button, { disabled: busy('members') || !Number.isSafeInteger(memberPage.next_offset),
                onClick: () => { setMemberFilter(''); void controller.loadMembers(memberPage.next_offset) } },
                t('下一页成员', 'Next members')))) : null,
          sourceOnlyCount ? h('p', { className: 'legion-help' },
            t(`本页另有 ${sourceOnlyCount} 个工具来源，不作为执行战友；可以在「找能力」中查看。`,
              `${sourceOnlyCount} tool sources on this page are separate from execution companions; explore them in Discover.`)) : null,
          h(Details, { value: state.members && { source: state.members.source, observed_at: state.members.observed_at,
            verification: state.members.verification, projection: state.members.projection } }))

        const editTeam = (field, value) => {
          setTeamDraft(previous => ({ ...previous, [field]: value }))
          setTeamDirty(true)
        }
        const flowBusy = ['flowStart', 'flowPause', 'flowRun', 'flowRecovery', 'teamSetup'].some(busy)
        const modelNotice = () => state.modelStatus?.available === false ? h('div', { className: 'legion-callout', role: 'status' },
          h('strong', null, t('任务可以安排，但当前会话还没有可用模型', 'Schedules are available; this conversation has no usable model')),
          h('p', { className: 'legion-help' }, t('先在 DSH 原生会话选择模型或添加自己的 API Key。配置好后回这里点“现在再做一轮”，或等待下次自动任务。',
            'Choose a model or add your API key in the native DSH conversation. Then run another round here or wait for the next scheduled one.')),
          h(Button, { disabled: !state.available.navigation, onClick: () => controller.openFlowSession() },
            t('去工作会话配置模型', 'Configure model in conversation')))
          : state.modelStatus?.lastError ? h('div', { className: 'legion-callout' },
            h('strong', null, t('原会话最近记录了执行错误', 'The conversation recorded an execution error')),
            h('p', { className: 'legion-help' }, state.modelStatus.lastError),
            h('p', { className: 'legion-help' }, t('模型在目录中可用不代表本次已经执行成功，请打开原会话检查。', 'A listed model does not prove this run succeeded; check the original conversation.')))
            : state.errors.modelStatus ? h('p', { className: 'legion-help' },
              t('暂未取得模型状态，不据此判定能否执行：', 'Model readiness is unknown: '), state.errors.modelStatus) : null
        const renderFlow = () => {
          const flow = state.flow
          const schedules = rows(flow?.schedules)
          const active = schedules.filter(item => item.status === 'active')
          const delivered = schedules.filter(item => item.lastDelivery?.deliveredAt)
          const profile = record(state.config?.value?.teamProfile) ? state.config.value.teamProfile : null
          const initialObserved = bool(flow?.workflow?.initialObserved)
          const recurringActive = bool(flow?.workflow?.recurringActive)
          const configurationMatches = bool(flow?.workflow?.configurationMatches)
          const time = value => value ? new Date(value).toLocaleString(
            t('zh-CN', 'en-US'), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' },
          ) : t('未取得', 'Unknown')
          const scheduleCard = item => h('article', { className: 'legion-task', key: item.id },
            h('strong', null, item.title || t('原生自动任务', 'Native scheduled work')),
            h('p', { className: 'legion-help legion-flow-time' }, item.status === 'active'
              ? `${t('下次：', 'Next: ')}${time(item.scheduledAt)}` : t('当前不再等待投递', 'Not awaiting another delivery')),
            h('p', { className: 'legion-help' }, item.matchesConfiguration === true
              ? t('此任务与当前默认配置一致', 'This task matches the current defaults')
              : item.matchesConfiguration === false ? t('此任务保留旧配置，尚未同步', 'This task retains older settings')
                : t('此任务与默认配置的关系尚未核实', 'This task’s settings have not been matched to the defaults')),
            item.lastDelivery?.deliveredAt ? h('p', { className: 'legion-help legion-flow-time' },
              t('已投递：', 'Delivered: '), time(item.lastDelivery.deliveredAt)) : null)
          return h(React.Fragment, null,
            h('div', { className: 'legion-flow-heading' },
              h('div', null, h('h2', null, profile?.name || t('当前会话的已有计划', 'Existing plans for this conversation')),
                profile ? h('p', { className: 'legion-help' }, t('全局默认目标：', 'Global default goal: '), profile.goal) : null),
              h(Button, { disabled: flowBusy, onClick: () => { setEditingTeam(true); setTeamStep(0) } },
                profile ? t('调整战队', 'Edit team') : t('设置战队默认配置', 'Set team defaults'))),
            h('p', { className: 'legion-help' }, t('战队配置是全局默认；保存并更新只更新当前工作会话的计划。其他会话仍保留自己的计划快照，切换后可明确同步。',
              'Team settings are global defaults. Saving and updating changes only this conversation’s plans. Other conversations retain their own snapshots until you explicitly sync them.')),
            profile ? h('div', { className: 'legion-team-summary' },
              h(Badge, null, t('默认协作：', 'Default collaboration: '), ({
                auto: t('自主组织', 'Self-organizing'), leader: t('指定带队', 'Chosen leader'),
                custom: t('按我的要求', 'My requirements'),
              })[profile.collaboration] || t('自主组织', 'Self-organizing')),
              h(Badge, null, profile.evolutionEnabled === false ? t('默认自主改进：关闭', 'Default improvement: off') : t('默认自主改进：允许', 'Default improvement: enabled')),
              h(Badge, null, profile.cadence === 'manual' ? t('默认频率：仅手动', 'Default cadence: manual')
                : profile.cadence === 'daily' ? t('默认频率：每日', 'Default cadence: daily') : t('默认频率：每周', 'Default cadence: weekly')))
              : h('p', { className: 'legion-callout' }, t('默认战队配置尚未设置；下方已有计划仍可能运行，可以暂停，不会因为配置缺失就自动停止。',
                'Team defaults are not configured. Existing plans may still run and can be paused; missing defaults do not stop them.')),
            renderCrew(),
            configurationMatches === false ? h('div', { className: 'legion-callout', role: 'status' },
              h('strong', null, state.flowStale ? t('上次核对：当前会话计划仍使用旧配置', 'Previous check: this conversation still used older settings')
                : t('当前会话仍使用旧配置，尚未同步', 'This conversation still uses older settings')),
              h('p', { className: 'legion-help' }, t('上方是默认偏好，不是这条旧任务的开关状态。请用“恢复／同步当前会话计划”明确更新，或直接暂停。',
                'The defaults above are not the old task’s switch state. Explicitly sync this conversation’s plans, or pause them.'))) : null,
            h('section', { className: 'legion-team-lead', 'aria-label': t('自动工作状态', 'Automatic work status') },
              h('strong', { role: 'status' }, state.flowStale ? t('上次记录，需要刷新核对', 'Previous observation — refresh to check')
                : !flow?.catalogVerified ? state.errors.flowStatus || (flow?.code && flow.code !== 'ok')
                  ? t('自动安排尚未核实，请看具体原因', 'Schedule not verified; see the reason below')
                  : t('正在核对原生任务安排', 'Checking native schedules')
                  : flow.partial ? t('部分安排未确认，请看下面的原因', 'Some changes are unconfirmed; see below')
                    : configurationMatches === false ? t('当前会话计划未同步到默认配置', 'This conversation’s plans are not synced to the defaults')
                      : profile && profile.cadence !== 'manual' && initialObserved === true && recurringActive === false
                        ? t('首次已安排，未观察到周期任务', 'Initial work is scheduled; no recurring task was observed')
                        : active.length ? !profile ? t('发现当前会话的已有自动任务', 'Existing automatic work was observed')
                          : configurationMatches === true && initialObserved === true
                            && (profile.cadence === 'manual' || recurringActive === true)
                            ? t('当前会话的安排与配置已核对', 'This conversation’s schedules and settings were checked')
                            : t('已观察到任务，完整安排仍待核对', 'Tasks were observed; the complete workflow still needs checking')
                      : delivered.length ? t('本轮任务已投递，当前没有后续安排', 'Work delivered; no future schedule is active')
                        : t('当前没有自动任务', 'No automatic work scheduled')),
              h('p', { className: 'legion-help' }, t('DSH 到时间会在这个工作会话继续。任务已安排、模型实际执行和改进有效是三件事；最终结果在原会话查看。',
                'DSH resumes this conversation when work is due. Scheduling, model execution and a verified improvement are separate; inspect results in the original conversation.')),
              h('div', { className: 'legion-toolbar' },
                h(Button, { disabled: !state.sessionId || busy('flowStatus') || flowBusy,
                  onClick: () => controller.loadFlow() }, t('刷新安排', 'Refresh schedules'))),
              !state.available.navigation ? h('p', { className: 'legion-help' },
                t('也可在 DSH 左侧打开上方所选的工作会话。', 'You can also open the selected conversation in the DSH sidebar.')) : null),
            modelNotice(),
            ...['flowStatus', 'flowStart', 'flowPause', 'flowRun', 'flowRecovery', 'flowOpen', 'teamSetup', 'configSave'].map(key => h(React.Fragment, { key }, error(key))),
            status('flowStatus'),
            ...rows(flow?.observations).filter(note => note.code !== 'delivery_not_execution').map((note, index) =>
              h('p', { className: 'legion-help', key: index }, note.message)),
            h('div', { className: 'legion-list' }, ...active.map(scheduleCard)),
            schedules.some(item => item.status !== 'active') ? h('details', null,
              h('summary', null, t('以往投递与暂停记录', 'Past deliveries and paused plans')),
              h('div', { className: 'legion-list' }, ...schedules.filter(item => item.status !== 'active').map(scheduleCard))) : null,
            h('div', { className: 'legion-step-actions' },
              h(Button, { disabled: !profile || !state.sessionId || flowBusy,
                onClick: () => controller.runFlow() }, state.flowRunKey
                ? t('核对并重试上次开工', 'Retry the unconfirmed run') : t('现在再做一轮', 'Run another round now')),
              h('div', { className: 'legion-row' },
                h(Button, { disabled: !profile || !state.sessionId || flowBusy,
                  onClick: () => controller.startFlow() }, configurationMatches === false
                  ? t('恢复／同步当前会话计划', 'Resume / sync this conversation’s plans')
                  : t('恢复／核对自动安排', 'Resume / reconcile schedules')),
                h(Button, { disabled: !state.sessionId || flowBusy || (!state.flowStale && flow?.enabled === false),
                  onClick: () => controller.pauseFlow() }, t('暂停后续自动工作', 'Pause future work')))),
            h('p', { className: 'legion-help' }, t('暂停不会撤回已投递的消息或打断正在运行的模型；需要停止当前工作，请在原会话点击停止。',
              'Pausing does not retract delivered messages or stop a running model. Stop current work in the original conversation.')),
            h(Details, { value: flow, label: t('查看原生任务记录与投递事实', 'Native schedule and delivery facts') }))
        }
        const renderTeam = () => {
          if (!editingTeam && (state.config?.value?.teamProfile || state.flow?.enabled === true
            || rows(state.flow?.schedules).length)) return renderFlow()
          const choices = [
            ['auto', t('自主组织', 'Self-organizing'), t('你给目标，战队自己决定谁带队、谁研究、谁实现、谁互查。', 'Give the goal; the team chooses who leads, researches, builds and reviews.')],
            ['leader', t('指定带队者', 'Choose a leader'), t('选一个已加入的战友带队，其他成员按需要补位。', 'A selected companion leads; others help where needed.')],
            ['custom', t('我的协作要求', 'My collaboration requirements'), t('写下你的偏好，不把所有任务锁进同一套流程。', 'Describe your preferences without fixing one workflow for every task.')],
          ]
          const profileValid = teamDraft.name.trim() && teamDraft.goal.trim()
            && (teamDraft.collaboration !== 'leader' || candidateDraft.includes(teamDraft.leader))
            && (teamDraft.collaboration !== 'custom' || teamDraft.instructions.trim())
          return h(React.Fragment, null,
            h('nav', { className: 'legion-steps', 'aria-label': t('组队步骤', 'Team setup steps') },
              ...[t('1 · 选战友', '1 · Companions'), t('2 · 定协作', '2 · Collaboration'), t('3 · 自动变强', '3 · Grow together')]
                .map((label, step) => h(Button, { key: step, 'aria-current': teamStep === step ? 'step' : undefined,
                  disabled: flowBusy, onClick: () => setTeamStep(step) }, label))),
            teamStep === 0 ? renderMembers() : teamStep === 1 ? h(React.Fragment, null,
              h('h2', null, t('让大家如何一起干活？', 'How should the team work together?')),
              h('p', { className: 'legion-help' }, t('同一个目标，互相借用工具、接着对方的工作继续，而不是给你堆几份答案。',
                'One goal, shared tools and work that builds on each other — not a pile of unrelated answers.')),
              h('div', { className: 'legion-list legion-section' }, ...choices.map(([value, label, description]) =>
                h('label', { className: `legion-choice${teamDraft.collaboration === value ? ' chosen' : ''}`, key: value },
                  h('input', { type: 'radio', name: 'legion-collaboration', value,
                    checked: teamDraft.collaboration === value, onChange: () => editTeam('collaboration', value) }),
                  h('div', null, h('strong', null, label), h('p', { className: 'legion-help' }, description))))),
              teamDraft.collaboration === 'leader' ? h('div', { className: 'legion-section legion-form' },
                h(Field, { title: t('谁来带队', 'Team leader') }, h('select', { value: teamDraft.leader,
                  onChange: event => editTeam('leader', event.target.value) },
                h('option', { value: '' }, t('从已选战友中选择', 'Select a chosen companion')),
                ...candidateDraft.map(key => h('option', { key, value: key }, key)))),
                !candidateDraft.length ? h('p', { className: 'legion-help' }, t('先回第一步选一位战友，或选择自主组织。', 'Choose a companion in step one, or use self-organization.')) : null) : null,
              teamDraft.collaboration === 'custom' ? h('div', { className: 'legion-section legion-form' },
                h(Field, { title: t('你的协作要求', 'Your requirements') }, h('textarea', {
                  value: teamDraft.instructions, maxLength: 2000,
                  placeholder: t('例如：让擅长研究的成员先查清资料，实现后请另一位成员实际使用和检查。', 'For example: research first; have another member use and check the result.'),
                  onChange: event => editTeam('instructions', event.target.value),
                }))) : null) : h(React.Fragment, null,
              h('h2', null, t('一次组队，之后自己往前走', 'Set up once; let the team move forward')),
              h('p', { className: 'legion-help' }, t('提交后自动开始第一轮，以后按下面的频率继续。不需要再点一次“开启”。',
                'Submitting starts the first round and schedules follow-ups. No extra activation step.')),
              h('p', { className: 'legion-help' }, t('这里修改全局默认配置，但只更新当前所选会话的计划；其他会话不会自动同步，也不会因此关闭旧任务。',
                'This changes global defaults but updates only the selected conversation’s plans. Other conversations are not automatically synced or stopped.')),
              modelNotice(),
              h('div', { className: 'legion-form legion-section' },
                h(Field, { title: t('战队名称', 'Team name') }, h('input', { value: teamDraft.name, maxLength: 80,
                  onChange: event => editTeam('name', event.target.value) })),
                h(Field, { title: t('希望战队持续帮你做到什么？', 'What should the team keep improving?') }, h('textarea', {
                  value: teamDraft.goal, maxLength: 4000, onChange: event => editTeam('goal', event.target.value),
                })),
                h('label', { className: `legion-choice${teamDraft.evolutionEnabled ? ' chosen' : ''}` },
                  h('input', { type: 'checkbox', checked: teamDraft.evolutionEnabled,
                    onChange: event => editTeam('evolutionEnabled', event.target.checked) }),
                  h('div', null, h('strong', null, t('允许战队自主改进', 'Enable autonomous improvement')),
                    h('p', { className: 'legion-help' }, t('从真实工作和失败中找问题，互相研究、修正工具和方法，验证后再复用。关闭后只发现新能力，不进行自主修改。',
                      'Learn from real work, inspect one another and improve tools and methods, then verify before reuse. Turn off to discover without self-modification.')))),
                h(Field, { title: t('多久发现新能力、继续改进一次？', 'Discovery and improvement frequency') }, h('select', {
                  value: teamDraft.cadence, onChange: event => editTeam('cadence', event.target.value),
                }, h('option', { value: 'weekly' }, t('每周一次 · 默认', 'Weekly · default')),
                h('option', { value: 'daily' }, t('每天一次', 'Daily')),
                h('option', { value: 'manual' }, t('只做首次，后续我来点', 'First round only; later rounds on demand')))),
                h('p', { className: 'legion-help' }, t('使用你为 DSH 配置的模型，会产生该模型的正常用量。新成员、安装与外发仍沿用原来的授权，随时可暂停。',
                  'Uses your configured DSH model and its normal usage. Member installation and external actions retain existing permissions. Pause at any time.')))),
            h('div', { className: 'legion-step-actions' },
              h('div', { className: 'legion-row' },
                teamStep > 0 ? h(Button, { disabled: flowBusy, onClick: () => setTeamStep(teamStep - 1) }, t('上一步', 'Back')) : null,
                editingTeam ? h(Button, { disabled: flowBusy, onClick: () => {
                  setEditingTeam(false); setCandidateDirty(false); setTeamDirty(false)
                } }, t('取消修改', 'Cancel changes')) : null),
              teamStep < 2 ? h(Button, { primary: true, disabled: flowBusy, onClick: () => setTeamStep(teamStep + 1) },
                teamStep === 0 ? t('下一步：协作方式', 'Next: collaboration') : t('下一步：自动变强', 'Next: ongoing improvement'))
                : h(Button, { primary: true, disabled: !state.sessionId || !state.config?.writable || flowBusy || !profileValid,
                  onClick: async () => {
                    const accepted = await controller.saveAndStartTeam({ candidateHarnesses: candidateDraft, teamProfile: teamDraft },
                      candidateVersion?.revision, candidateVersion?.entryId, candidateVersion?.namespace)
                    if (accepted) { setCandidateDirty(false); setTeamDirty(false); setEditingTeam(false); setTeamStep(0) }
                  } }, busy('teamSetup') ? t('正在保存并安排…', 'Saving and scheduling…')
                    : editingTeam ? t('保存并更新自动安排', 'Save and update schedules') : t('组建战队并自动开工', 'Form team and start automatically'))),
            teamStep === 2 && (!state.sessionId || !state.config?.writable) ? h('p', { className: 'legion-help' },
              t('请选择工作会话，并确认 DSH 原生配置可编辑；你的输入会保留。', 'Select a conversation and ensure native settings are writable. Your input stays here.')) : null,
            ...['teamSetup', 'configSave', 'flowStart', 'flowRecovery'].map(key => h(React.Fragment, { key }, error(key))))
        }

        const renderSearch = () => {
          const found = state.search
          const tools = rows(found?.tools?.tools)
          return h(React.Fragment, null,
            h('h2', null, t('先找现成的，再决定要不要造', 'Discover before you build')),
            h('p', { className: 'legion-help' }, t('搜索不会安装或运行结果。开源项目可以交给军团研究，已有 DSH 包走原生安装。', 'Search does not install or execute results. Delegate repository adaptation to Legion; install DSH packages natively.')),
            h('form', { className: 'legion-toolbar', onSubmit: event => {
              event.preventDefault(); void controller.search(sourceKind, query, marketIndex || undefined)
            } },
              h('select', { value: sourceKind, style: { width: 'auto' }, 'aria-label': t('搜索来源', 'Search source'),
                onChange: event => { controller.clearSearch(); setSourceKind(event.target.value); setMarketIndex('') } },
                h('option', { value: 'local' }, t('本会话工具 / Skill', 'Session tools / Skills')),
                h('option', { value: 'github' }, 'GitHub'),
                h('option', { value: 'market' }, t('DSH 插件市场', 'DSH plugin market'))),
              h('div', { className: 'legion-search' }, h('input', { value: query, onChange: event => setQuery(event.target.value),
                'aria-label': t('搜索能力', 'Search capabilities'), placeholder: t('描述需要的能力，或输入项目名', 'A capability or a project name') })),
              h('button', { type: 'submit', className: 'primary', disabled: busy('search') || !state.sessionId
                || (sourceKind === 'github' && !query.trim()) }, busy('search') ? t('搜索中…', 'Searching…') : t('搜索', 'Search'))),
            sourceKind === 'market' && found?.indexes?.length ? h('label', { className: 'legion-field' },
              t('搜索哪个已配置市场', 'Configured market to search'),
              h('select', { value: marketIndex || found.url, onChange: event => setMarketIndex(event.target.value) },
                ...found.indexes.map(index => h('option', { value: index.url, key: index.url }, titleOf(index.title) || index.url)))) : null,
            error('search'), !state.sessionId ? h(Empty, null, t('选择会话后开始搜索。无需连接军团核心，也能搜索 GitHub 和本地能力。', 'Choose a session to search. GitHub and local discovery do not need a Legion core connection.')) : null,
            !found && state.sessionId && !busy('search') && !state.errors.search ? h(Empty, null, t('试着搜索你真正想完成的事，例如“读懂这个代码仓库”。也可以留空查看本会话已有能力。', 'Search for the job you want done, or leave the local query empty to see available capabilities.')) : null,
            ...(found?.errors || []).map((value, index) => h(ErrorBox, { key: index, value })),
            found?.kind === 'local' ? h(React.Fragment, null,
              h('p', { className: 'legion-result-count' }, t(`当前返回 ${tools.length} 个工具、${rows(found.skills).length} 个 Skill。工具可见不代表已经成功执行。`, `${tools.length} tools and ${rows(found.skills).length} skills returned. Visibility is not successful execution.`)),
              h('div', { className: 'legion-grid' },
                ...tools.map(tool => h('article', { className: 'legion-card', key: `tool:${tool.name}` },
                  h('div', { className: 'legion-row between' }, h('h3', null, tool.name), h(Badge, null, 'Tool')),
                  h('p', { className: 'description' }, tool.description || t('无描述', 'No description')),
                  h('p', { className: 'legion-help' }, t('当前会话可见 · 由 DSH 原生权限控制执行', 'Visible in this session · execution uses DSH permissions')))),
                ...rows(found.skills).map(skill => h('article', { className: 'legion-card', key: `skill:${skill.name}` },
                  h('div', { className: 'legion-row between' }, h('h3', null, skill.name), h(Badge, null, 'Skill')),
                  h('p', { className: 'description' }, skill.description),
                  h('p', { className: 'legion-help' }, t('指导模型怎么做，不等于新增执行工具', 'Guidance for the model, not an additional execution tool'))))),
              found.tools?.next_offset != null ? h('p', { className: 'legion-help' },
                t('结果还有后续；请缩小搜索范围，或用 legion_discover 分页查看。', 'More results exist. Refine your query or page through legion_discover.')) : null,
              h(Details, { value: found.tools && { ...found.tools, tools: undefined } })) : null,
            found?.kind === 'github' ? h(React.Fragment, null,
              h('p', { className: 'legion-result-count' }, t(`返回 ${rows(found.repositories).length} / ${found.total_count ?? '?'} 个公开仓库`, `${rows(found.repositories).length} / ${found.total_count ?? '?'} public repositories`),
                found.truncated && t(' · 仅展示首批，请缩小搜索范围', ' · First page only; refine your query')),
              !rows(found.repositories).length ? h(Empty, null, t('本次没有结果。换一个更具体的搜索词试试。', 'No results. Try a more specific search.')) : null,
              h('div', { className: 'legion-grid' }, ...rows(found.repositories).map(item => h('article', { className: 'legion-card', key: item.repository },
                h('h3', null, h(PublicLink, { value: item.url }, item.repository)),
                h('p', { className: 'description' }, item.description),
                h('div', { className: 'legion-badges' }, item.language && h(Badge, null, item.language),
                  h(Badge, null, item.license?.spdx_id || t('许可待检查', 'License to inspect')),
                  item.archived && h(Badge, null, t('已归档', 'Archived'))),
                h('div', { className: 'legion-toolbar' }, h(Button, { onClick: () => prepareRepo(item) }, t('检查并交给军团', 'Inspect & delegate')))))),
              h(Details, { value: { source: found.source, status: found.status, fetched_at: found.fetched_at,
                incomplete_results: found.incomplete_results, truncated: found.truncated } })) : null,
            found?.kind === 'market' ? h(React.Fragment, null,
              found.unavailable ? h(Empty, null,
                t('当前 DSH 没有提供市场服务，可能尚未启用或版本不匹配。请在 DSH 插件页检查市场组件；本会话工具、Skill 和 GitHub 搜索仍可使用。', 'This DSH host does not provide the market service. It may be disabled or incompatible. Check the market component in DSH Plugins; session tools, Skills and GitHub search remain available.'),
                h('p', null, h(Button, { onClick: () => ctx.layout.selectPanel('plugins') }, t('打开 DSH 插件页', 'Open DSH Plugins'))),
                h(Details, { value: { reason: found.reason, diagnostic: found.diagnostic } })) : null,
              found.unconfigured ? h(Empty, null,
                t('尚未配置插件市场。打开 DSH 插件页添加可信市场后再查；不会擅自替你加入来源。', 'No configured market. Add a trusted index in DSH Plugins first; sources are not added automatically.'),
                h('p', null, h(Button, { onClick: () => ctx.layout.selectPanel('plugins') }, t('打开 DSH 插件页', 'Open DSH Plugins')))) : null,
              !found.unconfigured && !found.unavailable ? h('p', { className: 'legion-result-count' },
                `${titleOf(found.title)} · ${rows(found.entries).length} / ${found.total ?? '?'}`) : null,
              h('div', { className: 'legion-grid' }, ...rows(found.entries).map(item => {
                const version = rows(item.versions).find(version => version.version === item.defaultVersion)
                return h('article', { className: 'legion-card', key: item.name },
                  h('h3', null, titleOf(item.title) || item.name), h('code', null, item.name),
                  h('p', { className: 'description' }, titleOf(item.description)),
                  h('div', { className: 'legion-badges' }, h(Badge, null, item.installation?.status || 'unknown'),
                    h(Badge, null, version?.version || t('无兼容默认版本', 'No compatible default version'))),
                  h('div', { className: 'legion-toolbar' },
                    h(Button, { disabled: !version?.install, onClick: () => {
                      setSpec(version.install.spec); setRegistry(version.install.registry); setTab('add')
                      void controller.inspectPackage(version.install.spec, version.install.registry)
                    } }, t('查看安装来源', 'Review install source')),
                    h(PublicLink, { value: item.repository?.url || item.homepage }, t('项目说明', 'Project'))))
              })),
              found.nextOffset != null ? h('p', { className: 'legion-help' }, t('还有更多结果。请缩小搜索范围，或打开 DSH 插件页继续浏览。', 'More results exist. Refine the query or continue in DSH Plugins.')) : null) : null)
        }

        const renderPackages = () => h('section', { className: 'legion-section' },
          h('div', { className: 'legion-row between' }, h('h2', null, t('已安装的 DSH 能力包', 'Installed DSH capability bundles')),
            h(Button, { disabled: busy('packages') || !state.available.manager, onClick: () => controller.loadPackages() }, t('刷新能力包', 'Refresh bundles'))),
          h('p', { className: 'legion-help' }, t('包开关和子插件开关分开。开启后重新搜索本会话工具，核对实际是否可见。', 'Bundle and individual plugin switches are separate. Refresh session tools after activation.')),
          error('packages'), error('packageChange'),
          h('div', { className: 'legion-toolbar' }, h('input', { value: packageFilter, onChange: event => setPackageFilter(event.target.value),
            'aria-label': t('筛选能力包', 'Filter bundles'), placeholder: t('按包名筛选', 'Filter package names') })),
          h('div', { className: 'legion-list' }, ...rows(state.packages)
            .filter(bundle => bundle.availability !== 'missing'
              && `${bundle.name} ${bundle.description || ''}`.toLowerCase().includes(packageFilter.toLowerCase()))
            .map(bundle => h('article', { className: 'legion-card', key: bundle.name },
              h('div', { className: 'legion-row between' }, h('h3', null, bundle.name),
                h(Button, { disabled: !!bundle.readOnlyReason || bundle.name === 'dsh-plugin-legion' || busy('packageChange')
                  || busy('install') || state.installUncertain,
                onClick: () => controller.changePackage('bundle', bundle.name, !bundle.enabled) },
                bundle.enabled ? t('停用能力包', 'Disable bundle') : t('启用能力包', 'Enable bundle'))),
              h('p', { className: 'legion-help' }, bundle.version || '', ' · ',
                bundle.installed ? t('已安装到此配置', 'Installed in profile') : t('DSH 随附', 'Provided by DSH'),
                ' · ', bundle.enabled ? t('已选择启用', 'Selected') : t('未启用', 'Not selected')),
              bundle.description && h('p', { className: 'description' }, bundle.description),
              bundle.error ? h(ErrorBox, { value: errorText(bundle.error) }) : null,
              rows(bundle.rows).length ? h('details', null, h('summary', null, t('查看子插件与加载状态', 'Individual plugins and loading state')),
                ...bundle.rows.map(row => {
                  const live = rows(state.plugins).find(plugin => row.entryId && plugin.entryId === row.entryId)
                  return h('div', { className: 'legion-row between', style: { marginTop: 10 }, key: row.rowId },
                    h('code', null, row.moduleName),
                    h('span', { className: 'legion-help' }, live?.fiberPhase || t('未加载', 'Not loaded')),
                    h(Button, { disabled: !live || !!live.readOnlyReason || bundle.name === 'dsh-plugin-legion'
                      || busy('packageChange') || busy('install') || state.installUncertain,
                    onClick: () => controller.changePackage('plugin', live.entryId, !live.enabled) },
                    live?.enabled ? t('停用子插件', 'Disable plugin') : t('启用子插件', 'Enable plugin')))
                })) : null))))

        const renderAdd = () => h(React.Fragment, null,
          h('h2', null, t('已经是插件，直接安装；还不是，交给军团', 'Install a plugin, or ask Legion to adapt a project')),
          h('div', { className: 'legion-grid', style: { marginTop: 18 } },
            h('section', { className: 'legion-card' }, h('h3', null, t('安装现成 DSH 包', 'Install an existing DSH bundle')),
              h('p', { className: 'legion-help' }, t('支持包名、Git 仓库和本地包。检查只说明格式或元数据，不等于代码可信。', 'Use a package name, Git source or local bundle. Inspection checks format or metadata, not code trust.')),
              h(Field, { title: t('安装来源', 'Install source') }, h('input', { value: spec,
                placeholder: 'dsh-plugin-example@1.0.0', onChange: event => { setSpec(event.target.value); setRegistry(undefined) } })),
              h('div', { className: 'legion-toolbar' },
                h(Button, { disabled: !spec.trim() || busy('inspect') || busy('install') || state.installUncertain || !state.available.manager,
                  onClick: () => controller.inspectPackage(spec.trim(), registry) }, busy('inspect') ? t('检查中…', 'Inspecting…') : t('检查来源', 'Inspect source')),
                h(Button, { primary: true, disabled: state.inspection?.result?.status !== 'accepted' || state.inspection?.spec !== spec.trim()
                  || busy('install') || state.installUncertain, onClick: () => controller.installPackage() },
                  busy('install') ? t('原生安装中…', 'Installing natively…') : t('安装 / 更新', 'Install / update'))),
              h('p', { className: 'legion-help' }, t(
                '新包不会自动选择启用；更新保留已有开关，已启用包可能继续运行。若不希望更新后的代码运行，请先在下方停用对应能力包，再安装更新。',
                'New bundles are not automatically selected for activation. Updates preserve existing enablement, so enabled bundles may keep running. To prevent updated code from running, disable the bundle below before installing the update.',
              )),
              !state.available.manager ? h(Empty, null, t('此 DSH 未开放原生插件管理。请检查当前预设。', 'Native plugin management is not available in this DSH preset.')) : null,
              error('inspect'), error('install'), error('installRecovery'),
              state.inspection && h('p', { className: 'legion-help' },
                state.inspection.result.status === 'accepted' ? t('来源检查通过；请确认你信任此包，再点击安装。', 'Source accepted. Confirm you trust it before installing.') : t('来源被拒绝：', 'Source refused: '),
                state.inspection.result.reason || ''),
              state.inspection && h(Details, { value: state.inspection }),
              state.installId && (busy('install') || state.installUncertain) ? h('div', { className: 'legion-callout' },
                t('保留本次安装编号，断线后先查它，不重复安装。', 'Keep this install ID; recover it after a disconnect instead of reinstalling.'),
                h('code', { style: { display: 'block' } }, state.installId),
                h('div', { className: 'legion-toolbar' },
                  h(Button, { disabled: busy('installRecovery'), onClick: () => controller.recoverInstall() }, t('读取本次安装结果', 'Recover install result')),
                  h(Button, { disabled: busy('installCancel'), onClick: () => controller.cancelInstall() }, t('请求取消安装', 'Request cancellation'))),
                state.installCancellation && h(Details, { value: state.installCancellation }), error('installCancel')) : null,
              state.installCancellation?.status === 'cancelled' ? h('p', { className: 'legion-help', role: 'status' },
                t('本次安装已取消，主机已确认文件恢复。', 'This installation was cancelled; the host confirmed its files were restored.')) : null),
            h('section', { className: 'legion-card' }, h('h3', null, t('把开源项目变成可用能力', 'Turn a public project into a useful capability')),
              h('p', { className: 'legion-help' }, t('不是把全部文件硬拆成工具。军团研究接口和许可，再按目标适配；下面的任务可以自己改。', 'Not every source file becomes a tool. Legion researches interfaces and licenses, then adapts what the goal needs. Edit the task below.')),
              h(Field, { title: t('GitHub 项目', 'GitHub repository') }, h('input', { value: repository, placeholder: 'owner/repository',
                onChange: event => { controller.clearSource(); setRepository(event.target.value) } })),
              h('div', { style: { marginTop: 12 } }, h(Field, { title: t('版本（可选）', 'Revision (optional)') },
                h('input', { value: ref, placeholder: t('分支、标签或 commit；留空为默认分支', 'Branch, tag or commit; blank for default'),
                  onChange: event => { controller.clearSource(); setRef(event.target.value) } }))),
              h('div', { className: 'legion-toolbar' }, h(Button, { disabled: !repository.trim() || !state.sessionId || busy('source'),
                onClick: () => controller.inspectSource(repository.trim(), ref) }, busy('source') ? t('读取源码中…', 'Inspecting source…') : t('读取项目与许可', 'Inspect project & license'))),
              error('source'),
              state.source && h('div', { className: 'legion-callout' },
                h('strong', null, state.source.repository), h('p', null, h('code', null, state.source.commit)),
                h('p', { className: 'legion-help' }, t('已固定到这次提交。读取成功不等于许可已评估或适配完成。', 'Pinned to this commit. Retrieval is not a license assessment or completed adaptation.')),
                h(Details, { value: state.source })))),
          h(Outcome, { result: state.installResult }),
          h('section', { className: 'legion-section legion-form' },
            h('h2', null, t('告诉军团，你最后想拿到什么', 'Tell Legion what you want to receive')),
            h(Field, { title: t('可编辑的战队任务', 'Editable team task'),
              hint: t('提交会使用你已配置的核心与模型，可能产生费用。不会仅凭仓库文字自动安装。', 'Submission uses your configured core and models and may incur costs. Repository text never triggers an automatic install.') },
              h('textarea', { value: state.operation?.query ?? goal, disabled: !!state.operation,
                placeholder: t('我希望这个项目提供哪些能力？给谁用？怎样才算真的可用？', 'Which capabilities, for whom, and what should genuinely working look like?'),
                onChange: event => setGoal(event.target.value) })),
            h('p', { className: 'legion-help' }, sourceContext ? t('会附上上方已检查项目的提交与许可事实；不会把仓库文字当成系统指令。', 'The inspected commit and license facts above will accompany the task as untrusted source observations.') : t('也可以不指定项目，让军团自己研究合适的 Skill、工具或开源实现。', 'No project is required. Legion can research suitable skills, tools and implementations.')),
            h('div', { className: 'legion-row' },
              h(Button, { primary: true, disabled: !state.sessionId || !(state.operation?.query ?? goal).trim() || busy('submit'),
                onClick: () => controller.submit(state.operation?.query ?? goal, state.operation?.context ?? sourceContext) },
              busy('submit') ? t('正在提交…', 'Submitting…') : state.operation && !state.operation.ref ? t('用原编号重试这一单', 'Retry with the original key') : t('交给战队处理', 'Delegate to team')),
              state.operation?.ref ? h(Button, { onClick: () => controller.newTask() }, t('开始另一单', 'Start another task')) : null),
            error('submit'),
            state.operation && h('p', { className: 'legion-help' }, t('本次去重编号：', 'Idempotency key: '), h('code', null, state.operation.key)),
            state.submission && h('div', { className: 'legion-callout', role: 'status' },
              h('strong', null, state.submission.submission === 'rejected'
                ? t('本次提交已被拒绝；修复原因后可明确开始另一单', 'This submission was rejected; resolve the cause, then explicitly start another task')
                : t('任务已有回执，不代表已经接入完成', 'Task receipt received, not completed integration')),
              h('p', null, h('code', null, state.operation?.ref)),
              h(Button, { onClick: () => {
                openTab('tasks'); readTaskStatus(state.operation.ref)
              } }, t('查看原任务进度', 'Open task progress')), h(Details, { value: state.submission }))),
          renderPackages())

        const renderTasks = () => h(React.Fragment, null,
          h('div', { className: 'legion-row between' }, h('h2', null, t('战队任务', 'Team tasks')),
            h(TaskReadButton, { disabled: !state.sessionId, busy: busy('tasks'), onClick: () => controller.loadTasks() }, t('刷新任务列表', 'Refresh tasks'))),
          h('p', { className: 'legion-help' }, t('列表来自当前连接、本会话的恢复索引。切回原连接并刷新，可找回原操作身份；done、文件导出和体验验收分别查看。', 'This is the current connection and session recovery index. Restore the original connection and refresh to recover its operation; done, exports and acceptance remain separate.')),
          error('tasks'), status('tasks'),
          state.tasksStale ? h('p', { role: 'status', className: 'legion-callout' }, t('保留的是旧任务列表，尚非最新；请刷新第一页核对。', 'Retained task list is not current; refresh the first page.')) : null,
          state.tasks && state.tasks.task_page?.available !== true ? h(Empty, null,
            t('无法确认任务列表；这不是已确认的空列表。请重试原生读取。', 'Task list unavailable; not known empty. Retry the native read.')) : null,
          state.tasks?.task_page?.complete === true && state.tasks.task_page.total === 0 && !state.tasksStale
            ? h(Empty, null, t('本会话尚无军团任务。', 'No Legion tasks in this session.')) : null,
          state.tasks?.task_page?.available === true ? h('div', { className: 'legion-callout' },
            h('p', null, t(`本页 ${state.tasks.task_page.shown_count} 条；起始位置 ${state.tasks.task_page.offset}；已观察总数 ${state.tasks.task_page.total}。`,
              `This page: ${state.tasks.task_page.shown_count}; offset ${state.tasks.task_page.offset}; observed total ${state.tasks.task_page.total}.`)),
            state.tasks.task_page.source_complete !== true ? h('p', null, t('任务索引不完整；已观察记录之外仍有未知，不能据此断言不存在。', 'Task index is incomplete; unobserved records are unknown, not absent.')) : null,
            state.tasks.truncated ? h('p', null, t('当前显示部分记录或缩略字段；续读不会跳过未显示的记录。', 'Showing a partial list or shortened fields; continuation does not skip undisplayed rows.')) : null,
            h('div', { className: 'legion-toolbar' },
              state.tasks.task_page.offset > 0 ? h(TaskReadButton, { busy: busy('tasks'), onClick: () => controller.loadTasks() },
                t('返回任务首页', 'Back to first task page')) : null,
              Number.isSafeInteger(state.tasks.task_page.next_offset) && state.tasks.task_page.next_offset > state.tasks.task_page.offset
                ? h(TaskReadButton, { busy: busy('tasks'), disabled: state.tasksStale,
                  onClick: () => controller.loadTasks(state.tasks.task_page.next_offset, state.tasks.task_page.snapshot) },
                t('下一页任务', 'Next task page')) : null)) : null,
          h('div', { className: 'legion-list' }, ...rows(state.tasks?.tasks).map((task, index) => h('article', { className: 'legion-task', key: task?.task_ref || `unknown-${index}` },
            h('div', { className: 'legion-row between' },
              h('div', null, h('strong', null, task?.key || t('任务记录不可用', 'Task record unavailable')), h('code', { className: 'legion-muted' }, task?.task_ref)),
              typeof task?.task_ref === 'string' && /^[a-f0-9]{32}$/.test(task.task_ref)
                ? h(Button, { disabled: busy('taskStatus') || state.tasksStale, 'aria-controls': 'legion-task-status',
                  onClick: () => readTaskStatus(task.task_ref) }, t('读取实时进度', 'Read live progress'))
                : h('p', null, t('未提供可用任务引用，不能读取状态。', 'No usable task reference; status is unavailable.'))),
            taskRead?.sessionId === state.sessionId && taskRead.taskRef === task?.task_ref ? status('taskStatus') : null,
            h('p', { className: 'legion-help' }, task?.created_at)))),
          h('section', { id: 'legion-task-status', ref: taskStatusRegion, tabIndex: -1,
            'aria-label': t('原任务状态', 'Original task status'), 'aria-busy': busy('taskStatus') },
            error('taskStatus'), status('taskStatus'),
            state.taskStatus && h(TaskObservations, { value: state.taskStatus, sessionId: state.sessionId,
              download: state.download, downloadBusy: busy('download'), downloadError: state.errors.download,
              statusBusy: busy('taskStatus'), statusStale: state.taskStatusStale })))

        const renderConfig = () => h(React.Fragment, null,
          h('div', { className: 'legion-row between' }, h('h2', null, t('连接你自己的军团核心', 'Connect your own Legion core')),
            h(Button, { disabled: busy('config') || !state.available.configuration, onClick: () => controller.loadConfig() }, t('重新读取配置', 'Reload configuration'))),
          h('p', { className: 'legion-lead' }, t('这个公开插件是 DSH 的入口，不会偷偷下载私有核心，也不捆绑所有外部成员。没有核心也能找 Skill、搜索 GitHub、管理 DSH 能力包。', 'This public plugin is the DSH entry. It does not download the private core or bundle every external member. Skills, GitHub discovery and native bundle management work without a core.')),
          h('div', { className: 'legion-toolbar' },
            h(PublicLink, { value: 'https://github.com/seanliii/dsh-plugin-legion/blob/main/docs/CORE.md' }, t('核心范围与接入说明', 'Core scope and connection guide')),
            h(Button, { onClick: () => ctx.layout.selectPanel('plugins') }, t('DSH 原生插件设置', 'Native DSH Plugins'))),
          error('config'), error('configSave'), status('config'),
          !state.available.configuration ? h(Empty, null, t('此主机未提供可编辑的原生配置。请检查 DSH Settings 和插件管理是否已启用。', 'Native editable configuration is unavailable. Check DSH Settings and plugin management.')) : null,
          state.config?.status === 'ready' ? h('form', { className: 'legion-form', onSubmit: async event => {
            event.preventDefault()
            const accepted = await controller.saveConfig(configDraft, configVersion?.revision, configVersion?.entryId, configVersion?.namespace)
            if (accepted) setConfigDirty(false)
          } },
            ...[
              ['baseUrl', t('核心地址', 'Core URL'), 'http://127.0.0.1:8000',
                t('留空沿用环境配置。HTTP 只允许本机回环地址；其他地址须使用 HTTPS。', 'Blank inherits the environment. HTTP is loopback-only; other hosts require HTTPS.')],
              ['tokenEnv', t('令牌环境变量名', 'Token environment variable name'), 'LEGION_PERSONAL_TOKEN',
                t('只填变量名字，不填令牌。令牌由 DSH 主机读取。', 'Enter the variable name, never the token. The DSH host reads it.')],
              ['tokenFile', t('或：私有令牌文件路径', 'Or: private token file path'), t('可选，本机绝对路径', 'Optional absolute host path'),
                t('设置后优先用文件；要求本人所有、0600 权限。页面不会读取或展示文件内容。', 'When set, the file takes priority. It must be owned by you with mode 0600. The page never reads or displays its contents.')],
            ].map(([key, title, placeholder, hint]) => h(Field, { key, title, hint },
              h('input', { value: configDraft[key], placeholder, autoComplete: 'off', spellCheck: false,
                disabled: !state.config.writable || busy('configSave'), onChange: event => {
                  setConfigDraft(previous => ({ ...previous, [key]: event.target.value })); setConfigDirty(true)
                } }))),
            h('div', { className: 'legion-callout' }, t('保存会重载连接器，并停止本地正在等待的请求；远端任务不会被取消。更换令牌不会继承旧身份的任务引用。', 'Saving reloads the connector and ends local pending requests, not remote tasks. A new token does not inherit the previous identity’s task references.')),
            h('div', { className: 'legion-row' }, h('button', { type: 'submit', className: 'primary', disabled: !configDirty || !state.config.writable || busy('configSave') },
              busy('configSave') ? t('保存中…', 'Saving…') : t('保存连接配置', 'Save connection')),
            configDirty ? h(Button, { disabled: busy('configSave'), onClick: () => setConfigDirty(false) }, t('放弃未保存修改', 'Discard unsaved edits')) : null),
            state.configSaved ? h('p', { role: 'status', className: 'legion-help' }, t('原生配置已接受并读回；连接可用性请再刷新成员核对。', 'Native configuration accepted and read back. Refresh members to check connectivity.')) : null,
            h('p', { className: 'legion-help' }, t('配置版本：', 'Configuration revision: '), state.config.revision)) : null)

        return h('div', { className: 'legion-panel', 'data-legion-workbench': true },
          h('style', null, styles),
          h('div', { className: 'legion-wrap' },
            h('header', { className: 'legion-top' },
              h('div', null, h('div', { className: 'legion-eyebrow' }, 'LEGION / DSH'),
                h('h1', null, t('顶尖战队，共同进化', 'Leading harnesses. Growing together.')),
                h('p', { className: 'legion-lead' }, t('集结各路优秀 Harness，协作完成同一个目标。以顶级成果为参照，反复对比、验证和改进；工具与有效经验为下一次工作所用。',
                  'Bring leading harnesses to one goal. Compare real work with strong references, verify and improve; carry useful tools and experience into the next task.'))),
              h('label', { className: 'legion-session' }, h('span', { className: 'legion-help' }, t('当前工作会话', 'Working session')),
                h('select', { value: state.sessionId, 'aria-label': t('当前工作会话', 'Working session'), onChange: event => selectSession(event.target.value) },
                  h('option', { value: '' }, t('请选择会话', 'Select a session')),
                  ...state.sessions.ids.map(id => h('option', { value: id, key: id }, sessionLabel(state.sessions, id)))),
                !state.sessions.ids.length ? h('span', { className: 'legion-help' }, t('请先在 DSH 新建一个对话，再刷新。', 'Create a DSH conversation first, then refresh.')) : null,
                h(Button, { disabled: busy('sessions'), onClick: () => controller.refreshSessions() }, t('刷新会话', 'Refresh sessions')))),
            error('sessions'),
            h('nav', { className: 'legion-tabs', role: 'tablist', 'aria-label': t('军团工作台', 'Legion workbench') },
              ...[
                ['members', t('我的军团', 'My Legion')], ['search', t('找能力', 'Discover')], ['add', t('添加能力', 'Add capabilities')],
                ['tasks', t('战队任务', 'Team tasks')], ['config', t('连接与配置', 'Connection')],
              ].map(([id, label]) => h(Button, { role: 'tab', key: id, 'aria-selected': tab === id,
                'aria-controls': `legion-tab-${id}`, onClick: () => openTab(id) }, label))),
            h('div', { id: `legion-tab-${tab}`, role: 'tabpanel' },
              tab === 'members' ? renderTeam() : tab === 'search' ? renderSearch() : tab === 'add' ? renderAdd()
                : tab === 'tasks' ? renderTasks() : renderConfig())))
      }
      const Icon = () => h('svg', { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true },
        h('path', { d: 'M12 7v5M5 17l7-5 7 5', stroke: 'currentColor', strokeWidth: 1.6 }),
        ...[[12, 5], [4.5, 18], [19.5, 18]].map(([cx, cy]) => h('circle', {
          key: cx, cx, cy, r: 3, stroke: 'currentColor', strokeWidth: 1.6,
        })))
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'legion', inject: () => ({}) }, Page))
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
        name: 'sidebar.panellist', id: 'legion', order: 10, label: () => t('军团', 'Legion'),
      }, Icon))
    }

    return {
      name: 'legion-command-notice',
      workbench: { configEntry, sessionLabel, memberFacts, publicLink, configOperations, changeFacts, artifactFacts, createController },
      inject: ['commandUi', 'sessions', 'conversation', 'locale'],
      apply(ctx) {
        // This event is a local submission acknowledgment, not history replay.
        // Cordis owns the observer and removes it when this fiber is disposed.
        ctx.on('command/executed', (sessionId, name, result) => {
          if (name !== 'legion' || typeof sessionId !== 'string' || !sessionId) return
          if (result?.kind !== 'success' && result?.kind !== 'error') return
          try {
            const actx = ctx.sessions.scope(sessionId)
            const conversation = actx?.get('conversation')
            if (!conversation) return
            conversation.input.for(actx).notify(
              result.kind === 'error' ? 'error' : 'info',
              notice(ctx.locale, result),
            )
          } catch {
            // A released session/input must not redirect the receipt or alter
            // the command outcome. Never log its possibly sensitive payload.
          }
        })
        // Keep headless/partial clients working. UI services activate a child
        // only where DSH provides actual allocated panel slots.
        ctx.inject(['slots', 'layout', 'remote', 'remote.commands'], registerPanel)
      },
    }
  },
})
