import { createHash } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import { teamContext } from './team.js'

const DEFAULT_GOAL = '让我的DSH和战队通过真实工作持续变强'
const LIMIT = 50
const MAX_UPDATE_ATTEMPTS = 3
// Only in-flight serialization lives here. All durable facts come from native catalog/history.
const sessionQueues = new Map()
const messages = {
  delivery_not_execution: '原生任务的安排和投递不代表模型已经执行、完成改进或验证作品；结果请看原会话。',
  service_missing: '当前 DSH 未提供所需的原生 schedule 服务；没有用另一个调度器代替。',
  profile_missing: '尚未配置战队；可以查看已有任务，但不会自动创建任务。',
  invalid_profile: '战队配置无效；请先保存有效的组队配置。',
  invalid_leader: '指定带队者必须属于已选择的候选成员；本次没有派发任务。',
  invalid_key: '再次开工需要明确的 key：1–128 个有效文本字符，无首尾空白或控制字符，不接受身份或其他附加字段。',
  invalid_execution: '缺少经过 DSH 校验的当前会话及取消信号。',
  cancelled: '本次操作已取消，未开始的安排不再执行。',
  cancellation_not_rollback: '取消不能回滚已经开始的原生写入，也不会撤回已投递消息或取消模型任务；请刷新核对原生记录。',
  catalog_unavailable: '未能读取原生任务目录；目前的任务状态未核实，不会盲目重复安排。',
  create_failed: '该项安排未成功；其他成功项保留，再次开启时先核对原生目录。',
  update_failed: '该项更新未成功；保留原生任务，没有删除重建。',
  delete_failed: '该项暂停未成功；其他暂停结果保留，可刷新后重试。',
  write_unconfirmed: '原生写入结果尚未核实；请用同一操作键刷新核对，不要换键重复发起。',
  schedule_conflict: '任务在读取后发生变化；未覆盖他人的修改，请刷新原生记录后再决定。',
  ownership_changed: '原任务的会话或流程标记已改变；没有修改这条记录。',
  duplicate_marker: '同一流程角色存在多条活动记录；没有再建或擅自合并，请在原生目录核对。',
  configuration_outdated: '当前会话的部分活动任务快照与当前默认设置不一致，仍按已保存的指令和周期投递。可以同步当前会话或暂停；本次查看没有修改任何会话任务。',
  configuration_unverified: '无法核对活动任务是否匹配当前默认设置；已有任务快照保留，不据此宣称设置已同步。',
  recurring_not_observed: '当前默认设置包含周期工作，但原生目录中未观察到本会话的活动周期任务，原因尚未核实。',
  native_delete_removes_history: '暂停只删除本流程仍处于 active 的调度记录，包括逾期但尚未投递的任务。原生删除也会清理该任务的调度历史和去重标记，但会话内容保留，不会撤回已投递的本轮消息或取消正在运行的模型。已删除记录无法再用于永久去重。',
  operation_limit: '本次只处理有界数量的任务；剩余任务仍保留，可再次暂停继续处理。',
  subagent_session: '原生调度不支持向这个子代理会话投递；请使用原始 DSH 会话。',
  native_error: '原生服务未完成本次操作；未显示可能含有私有内容的 API 正文或错误详情。',
}

const hash = value => createHash('sha256').update(value).digest('hex')
const marker = (sessionId, role, key) =>
  `[dsh-legion-team-flow:v1 session=${hash(sessionId)} role=${role}${key === undefined ? '' : ` key=${hash(key)}`}]`

function roleOf(row, sessionId) {
  if (row?.sessionId !== sessionId || typeof row.prompt !== 'string') return undefined
  const line = row.prompt.slice(0, 240).split('\n')[0]
  const match = /^\[dsh-legion-team-flow:v1 session=([a-f0-9]{64}) role=(initial|recurring|run)(?: key=([a-f0-9]{64}))?\]$/u.exec(line)
  if (!match || match[1] !== hash(sessionId) || (match[2] === 'run') !== Boolean(match[3])) return undefined
  return match[2]
}

const lineOf = row => row.prompt.split('\n', 1)[0]
const safeId = value => typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,160}$/u.test(value) ? value : null
const instant = value => typeof value === 'string' && value.length === 24
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value ? value : null

function safeTitle(value) {
  if (typeof value !== 'string') return ''
  return value.slice(0, 4096)
    .replace(/\b(?:bearer|basic)\s+\S+/giu, '[redacted]')
    .replace(/\b(?:sk-|gh[pousr]_|github_pat_)[a-zA-Z0-9_-]+/gu, '[redacted]')
    .replace(/\b(?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+/giu, '[redacted]')
    .replace(/https?:\/\/[^\s/@]+:[^\s/@]+@/giu, 'https://[redacted]@')
    .replace(/[\u0000-\u001f\u007f]/gu, ' ').slice(0, 120)
}

function receipt(value) {
  if (!value || typeof value !== 'object') return null
  return { scheduledAt: instant(value.scheduledAt), deliveredAt: instant(value.deliveredAt), messageId: safeId(value.messageId) }
}

function observe(state, code, result) {
  if (state.observations.length < LIMIT) {
    state.observations.push({ code, message: messages[code] ?? messages.native_error,
      ...(result ? { role: result.role, ...(safeId(result.id) ? { id: result.id } : {}) } : {}) })
  }
}

function project(state) {
  const rows = [...state.rows].sort((a, b) =>
    (a.status === 'active' ? 0 : 1) - (b.status === 'active' ? 0 : 1)
    || String(a.scheduledAt).localeCompare(String(b.scheduledAt)) || String(a.id).localeCompare(String(b.id)))
  const active = rows.filter(row => row.status === 'active')
  const expected = new Map()
  const matches = new Map(active.map(row => [row,
    state.catalogVerified ? state.matchesConfiguration(row, state.sessionId, expected) : null]))
  const matchValues = [...matches.values()]
  // These are observations of the full owned catalog, not a persisted success/failure verdict.
  // No active row (or no verified catalog/default) means there is nothing we can confirm matches.
  const workflow = {
    initialObserved: state.catalogVerified ? rows.some(row => roleOf(row, state.sessionId) === 'initial') : null,
    recurringActive: state.catalogVerified ? active.some(row => roleOf(row, state.sessionId) === 'recurring') : null,
    configurationMatches: matchValues.includes(false) ? false
      : !matchValues.length || matchValues.includes(null) ? null : true,
  }
  const derived = []
  if (workflow.configurationMatches === false) derived.push('configuration_outdated')
  if (state.catalogVerified && matchValues.includes(null)) derived.push('configuration_unverified')
  if (state.expectsRecurring && workflow.recurringActive === false) derived.push('recurring_not_observed')
  const partial = state.incomplete || state.results.some(item => ['failed', 'unknown', 'pending'].includes(item.outcome))
  return {
    configured: state.configured,
    enabled: rows.some(row => row.status === 'active') ? true : state.catalogVerified ? false : null,
    code: state.cancelled ? 'cancelled' : partial ? 'partial' : state.code,
    partial,
    cancelled: state.cancelled,
    catalogVerified: state.catalogVerified,
    workflow,
    schedules: rows.slice(0, LIMIT).map(row => ({
      id: safeId(row.id), kind: ['after', 'at', 'every', 'daily', 'weekly', 'cron'].includes(row.kind) ? row.kind : 'unknown',
      status: row.status === 'active' ? 'active' : 'inactive', title: safeTitle(row.title),
      scheduledAt: instant(row.scheduledAt), lastDelivery: receipt(row.lastDelivery),
      role: roleOf(row, state.sessionId),
      ...(row.status === 'active' ? { matchesConfiguration: matches.get(row) } : {}),
    })),
    omittedSchedules: Math.max(0, rows.length - LIMIT),
    results: state.results.slice(0, LIMIT).map(item => ({
      role: item.role, outcome: item.outcome === 'pending' ? 'unknown' : item.outcome,
      ...(safeId(item.id) ? { id: item.id } : {}), ...(item.code ? { code: item.code } : {}),
    })),
    observations: [...derived.map(code => ({ code, message: messages[code] })), ...state.observations]
      .slice(0, LIMIT).map(item => ({ ...item })),
    execution: 'not_verified',
  }
}

function cancelled(state) {
  if (!state.cancelled) {
    state.cancelled = true
    observe(state, 'cancelled')
    observe(state, 'cancellation_not_rollback')
  }
  return project(state)
}

class FlowStop extends Error {}

/** Read-only waits can be abandoned; a native mutation must retain its queue slot until it settles. */
function interruptible(promise, signal) {
  if (!signal) return promise
  return new Promise((resolve, reject) => {
    const abort = () => { cleanup(); reject(new FlowStop('cancelled')) }
    const cleanup = () => signal.removeEventListener('abort', abort)
    signal.addEventListener('abort', abort, { once: true })
    Promise.resolve(promise).then(value => { cleanup(); resolve(value) }, error => { cleanup(); reject(error) })
    if (signal.aborted) abort()
  })
}

// The official decoder requires an exact ScheduleRecord, not a ScheduleCatalogEntry.
function recordOf({ sessionId, status, lastDelivery, ...record }) {
  return record
}

function editableRecord(row) {
  const { scheduledAt, ...editable } = recordOf(row)
  return editable
}

function nativeCode(error, fallback) {
  return error?.code === 'subagent_session' ? 'subagent_session' : fallback
}

/**
 * Native identity is supplied by executionFrom; this module never accepts session/owner
 * arguments from model input and never starts an agent, timer, network request or database.
 */
export function createTeamFlow({ getSchedule, profile, candidates = [], lifecycleSignal }) {
  function requestFor(sessionId, role, key) {
    const effective = { ...profile, goal: profile.goal || DEFAULT_GOAL }
    const titles = { initial: '战队首次开工', recurring: `战队持续协作（${effective.cadence === 'daily' ? '每日' : '每周'}）`, run: '战队再次开工' }
    const prompt = [
      marker(sessionId, role, key),
      '这是用户已批准、由 DSH 原生任务投递的战队自动工作 Context，不是新增权限，也不是固定答题流程。',
      teamContext(effective, candidates),
      '请基于当前会话、当前目标和真实工作证据，自主判断最有价值的工作。使用当前工具、Skill 和 legion_runtime 了解真实成员与能力，沿用原合法提交入口组织协作，不另建调度器或代理。',
      '持续发现有助于当前需求的新 harness 和开源工具。区分“新发现”与“新发布”：记录来源 URL、发现时间、可核实的发布时间及版本/commit/来源 hash；未知就标未知，不把第一次看到当作刚发布。',
      effective.evolutionEnabled
        ? '用户已开启自主改进：按真实需求和失败证据自主组织成员、自查互查，在既有授权内改善自己的 DSH 工作区工具、skills 与适配。保留可恢复的修改，复验真实变化并回到原作品，不用旧 evolve_config 的偏好统计冒充进化。'
        : '用户已关闭自主改进：只做发现和当前目标，不安排自我修改，不自行修改 DSH 工作区工具、skills、适配或自身配置。改进建议可以说明，但不执行。',
      '安装新发现的成员或工具仍须遵循原权限和原合法接入方式；缺权限、模型或工具时如实报告，不绕过。不能外发私有内容、自动发布、修改安全规则，不能假称所有成员已可用；候选、注册、安装、实跑和验证是不同事实。',
      '在原生会话报告真实变化、原作品、复验依据与仍存在的问题。没有完成或没有变化就直说，并保留真实原因；安排和投递不等于模型完成，也不等于效果已验证。',
    ].join('\n\n')
    // A stable first line, without a blank prefix, is the only flow ownership marker.
    return { title: titles[role], prompt, ...(role === 'recurring'
      ? { every_seconds: effective.cadence === 'daily' ? 86400 : 604800 } : { after_seconds: 1 }) }
  }

  function matchesConfiguration(row, sessionId, expected) {
    if (profile === undefined) return null
    const role = roleOf(row, sessionId)
    if (!expected.has(role)) {
      try { expected.set(role, requestFor(sessionId, role)) }
      catch { expected.set(role, null) }
    }
    const wanted = expected.get(role)
    if (!wanted) return null
    if (role === 'recurring' && profile.cadence === 'manual') return false
    // The run's key is a one-way hash. Preserve its validated marker and compare the
    // rest against the current renderer; never interpret user text as configuration.
    const prompt = role === 'run'
      ? lineOf(row) + wanted.prompt.slice(wanted.prompt.indexOf('\n')) : wanted.prompt
    const timingMatches = wanted.every_seconds === undefined
      ? row.kind === 'after' && row.afterSeconds === wanted.after_seconds
      : row.kind === 'every' && row.everySeconds === wanted.every_seconds
    return row.prompt === prompt && timingMatches
  }

  async function invoke(method, args, execution) {
    const state = { configured: profile !== undefined, sessionId: execution?.sessionId,
      expectsRecurring: ['weekly', 'daily'].includes(profile?.cadence), matchesConfiguration,
      rows: [], results: [], observations: [], catalogVerified: false, incomplete: false, cancelled: false, code: 'ok' }
    observe(state, 'delivery_not_execution')
    if (typeof execution?.sessionId !== 'string' || !execution.sessionId.trim()
      || typeof execution.signal?.throwIfAborted !== 'function' || typeof execution.signal?.addEventListener !== 'function') {
      state.code = 'invalid_execution'
      observe(state, state.code)
      return project(state)
    }
    const signal = lifecycleSignal ? AbortSignal.any([execution.signal, lifecycleSignal]) : execution.signal
    if (signal.aborted) return cancelled(state)
    const check = () => { if (signal.aborted) { cancelled(state); throw new FlowStop() } }
    const fail = (result, code, outcome = 'failed') => {
      Object.assign(result, { outcome, code })
      state.incomplete = true
      observe(state, code, result)
    }

    async function work() {
      check()
      let schedule
      try { schedule = await interruptible(Promise.resolve().then(() => getSchedule?.()), signal) }
      catch { if (signal.aborted) check() }
      check()
      if (typeof schedule?.catalog !== 'function'
        || (method === 'start' || method === 'run') && typeof schedule.create !== 'function'
        || method === 'pause' && typeof schedule.delete !== 'function') {
        state.code = 'service_missing'
        observe(state, state.code)
        return
      }

      async function refresh(reconcile = false) {
        if (!reconcile) check()
        try {
          const rows = await interruptible(Promise.resolve().then(() => schedule.catalog()), reconcile ? undefined : signal)
          if (!Array.isArray(rows)) throw new FlowStop()
          state.rows = rows.filter(row => roleOf(row, state.sessionId))
          state.catalogVerified = true
        } catch {
          state.catalogVerified = false
          if (signal.aborted) check()
          state.code = 'catalog_unavailable'
          if (state.results.length) state.incomplete = true
          observe(state, 'catalog_unavailable')
          throw new FlowStop()
        }
      }

      const findId = (id, mark) => state.rows.find(row => row.id === id && lineOf(row) === mark)
      const sameDesired = (row, wanted) => row.prompt === wanted.prompt && row.title === wanted.title
        && (wanted.every_seconds === undefined || row.kind === 'every' && row.everySeconds === wanted.every_seconds)

      async function update(row, wanted, result) {
        const original = editableRecord(row)
        const mark = lineOf(row)
        if (typeof schedule.update !== 'function') return fail(result, 'service_missing')
        for (let attempt = 0; attempt < MAX_UPDATE_ATTEMPTS; attempt++) {
          check()
          let response, error
          result.outcome = 'pending'
          try {
            response = await schedule.update({
              sessionId: state.sessionId, id: row.id, expected: recordOf(row),
              ...(row.title === wanted.title ? {} : { title: wanted.title }),
              ...(row.prompt === wanted.prompt ? {} : { prompt: wanted.prompt }),
              ...(wanted.every_seconds !== undefined && (row.kind !== 'every' || row.everySeconds !== wanted.every_seconds)
                ? { change: { kind: 'every', every_seconds: wanted.every_seconds } } : {}),
            }, signal)
            if (response?.record?.id === row.id) result.outcome = response.updated ? 'updated' : 'reused'
          } catch (caught) { error = caught }
          await refresh(true)
          const current = findId(row.id, mark)
          if (current && sameDesired(current, wanted)) {
            result.outcome = error ? 'recovered' : response?.updated ? 'updated' : 'reused'
            return
          }
          check()
          if (!current) return fail(result, 'ownership_changed')
          if (!error && response?.code === 'schedule_conflict') {
            // Native delivery may advance the target, but a human edit must not be overwritten.
            if (current.status !== 'active' || !isDeepStrictEqual(editableRecord(current), original)) {
              return fail(result, 'schedule_conflict')
            }
            row = current
            continue
          }
          return fail(result, nativeCode(error ?? response, 'update_failed'))
        }
        fail(result, 'schedule_conflict')
      }

      async function ensure(role, key, result) {
        check()
        let wanted
        try { wanted = requestFor(state.sessionId, role, key) }
        catch { return fail(result, 'invalid_profile') }
        const mark = wanted.prompt.split('\n', 1)[0]
        const matching = state.rows.filter(row => lineOf(row) === mark && (role !== 'recurring' || row.status === 'active'))
        if (matching.length > 1) return fail(result, 'duplicate_marker')
        const existing = matching[0]
        if (existing) {
          Object.assign(result, { id: existing.id, outcome: 'reused' })
          if (role !== 'run' && existing.status === 'active' && !sameDesired(existing, wanted)) {
            await update(existing, wanted, result)
          }
          return
        }
        result.outcome = 'pending'
        let error, committed
        try {
          committed = await schedule.create(state.sessionId, wanted, signal)
          // Keep the acknowledged mutation even if the following observation fails.
          // This is a per-call receipt, not a second persisted task index.
          if (safeId(committed?.id)) Object.assign(result, { id: committed.id, outcome: 'created' })
        }
        catch (caught) { error = caught }
        // A failed acknowledgment is not proof that the durable native write failed.
        await refresh(true)
        const stored = state.rows.find(row => lineOf(row) === mark
          && (committed?.id ? row.id === committed.id : role !== 'recurring' || row.status === 'active'))
        if (stored) Object.assign(result, { id: stored.id, outcome: error ? 'recovered' : 'created' })
        else fail(result, nativeCode(error, error ? 'create_failed' : 'write_unconfirmed'), error ? 'failed' : 'unknown')
        check()
      }

      async function remove(row, result) {
        check()
        const mark = lineOf(row)
        // Native delete has no expected predicate. Re-read ownership/status immediately before it.
        await refresh()
        const current = findId(row.id, mark)
        if (!current) { result.outcome = 'absent'; return }
        if (current.status !== 'active') { result.outcome = 'retained'; return }
        if (typeof schedule.delete !== 'function') return fail(result, 'service_missing')
        result.outcome = 'pending'
        let response, error
        try {
          response = await schedule.delete({ sessionId: state.sessionId, id: current.id }, signal)
          if (response?.deleted) result.outcome = 'deleted'
        }
        catch (caught) { error = caught }
        await refresh(true)
        if (!state.rows.some(item => item.id === current.id)) {
          result.outcome = error ? 'recovered' : response?.deleted ? 'deleted' : 'absent'
        } else fail(result, 'delete_failed')
        check()
      }

      await refresh()
      if (method === 'status') return
      if (method === 'start' || method === 'run') {
        if (profile === undefined) {
          state.code = 'profile_missing'
          observe(state, state.code)
          return
        }
        if (profile.collaboration === 'leader' && (!profile.leader || !candidates.includes(profile.leader))) {
          state.code = 'invalid_leader'
          observe(state, state.code)
          return
        }
        if (method === 'run' && (!args || typeof args !== 'object' || Array.isArray(args)
          || Reflect.ownKeys(args).length !== 1 || !Object.hasOwn(args, 'key')
          || typeof args.key !== 'string' || !args.key.length || args.key.length > 128
          || args.key !== args.key.trim() || !args.key.isWellFormed() || /[\u0000-\u001f\u007f]/u.test(args.key))) {
          state.code = 'invalid_key'
          observe(state, state.code)
          return
        }
      }
      if (method === 'run') {
        const result = { role: 'run', outcome: 'not_attempted' }
        state.results.push(result)
        await ensure('run', args.key, result)
      } else if (method === 'start') {
        const initial = { role: 'initial', outcome: 'not_attempted' }
        const recurring = { role: 'recurring', outcome: 'not_attempted' }
        state.results.push(initial)
        if (profile.cadence !== 'manual') state.results.push(recurring)
        await ensure('initial', undefined, initial)
        if (profile.cadence !== 'manual') await ensure('recurring', undefined, recurring)
        else {
          const targets = state.rows.filter(row => roleOf(row, state.sessionId) === 'recurring' && row.status === 'active')
          if (targets.length) observe(state, 'native_delete_removes_history')
          for (const row of targets.slice(0, LIMIT - state.results.length)) {
            const result = { role: 'recurring', id: row.id, outcome: 'not_attempted' }
            state.results.push(result)
            await remove(row, result)
          }
          if (targets.length > LIMIT - 1) { state.incomplete = true; observe(state, 'operation_limit') }
        }
      } else {
        observe(state, 'native_delete_removes_history')
        const targets = state.rows.filter(row => row.status === 'active')
        for (const row of targets.slice(0, LIMIT)) {
          const result = { role: roleOf(row, state.sessionId), id: row.id, outcome: 'not_attempted' }
          state.results.push(result)
          await remove(row, result)
        }
        if (targets.length > LIMIT) { state.incomplete = true; observe(state, 'operation_limit') }
      }
    }

    // Share the queue between plugin/factory instances. Cleanup is not a persistent task index.
    const previous = sessionQueues.get(state.sessionId) ?? Promise.resolve()
    const pending = previous.then(work).catch(error => {
      if (signal.aborted) cancelled(state)
      else if (!(error instanceof FlowStop)) {
        state.code = 'native_error'
        state.incomplete = state.results.length > 0
        observe(state, 'native_error')
      }
    }).then(() => project(state))
    sessionQueues.set(state.sessionId, pending)
    void pending.finally(() => {
      if (sessionQueues.get(state.sessionId) === pending) sessionQueues.delete(state.sessionId)
    })
    return interruptible(pending, signal).catch(() => cancelled(state))
  }

  return {
    status: execution => invoke('status', undefined, execution),
    start: execution => invoke('start', undefined, execution),
    pause: execution => invoke('pause', undefined, execution),
    run: (args, execution) => invoke('run', args, execution),
  }
}
