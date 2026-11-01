const FACT_BOUNDARY = 'done ≠ 已导出 ≠ 已验收；插件不进行作品体验验收。'

export const LEGION_HELP = [
  '军团任务：DSH 保留当前会话和原生执行循环。',
  '/legion start <key> <task> — 提交任务；key 是稳定去重键。',
  '/legion list — 当前 DSH 会话的本地恢复索引，不是实时状态。',
  '/legion runtime [members|capabilities|catalog] — 查看军团实际成员与能力状态；不安装、不探活。',
  '/legion status <ref> [cursor] — 查询原任务和进度，不重新派单。',
  '/legion message <ref> <operation-key> <recipient> <text> — 消息入邮箱不代表已读或已处理。',
  '/legion resume <ref> <operation-key> — 只恢复中断任务，不创建新轮次。',
  '/legion cancel <ref> [reason] — 请求取消，不代表进程已经停止。',
  '/legion download <ref> <artifact-id> — 下载服务列出的文件并校验 SHA-256。',
  '/legion continue <ref> <new-key> <feedback> — 同一军团会话的新轮次，不是 resume。',
  'ref 必须是明确的任务引用（task_ref），可从 start 或 list 取得；不支持省略或 latest。',
  '网络结果未确认时先查原任务，不用新键盲目重发。令牌放在用户配置的私有文件或环境变量中，不贴进聊天。',
  FACT_BOUNDARY,
].join('\n')

/** The host is the sole authority for session ownership and call cancellation. */
export function executionFrom(invocation) {
  if (typeof invocation?.agent?.id !== 'string' || !invocation.agent.id.trim()) {
    throw new Error('无法确认当前 DSH 会话；请在明确的会话中调用军团。')
  }
  const signal = invocation.signal
  if (!signal || typeof signal.addEventListener !== 'function' || typeof signal.throwIfAborted !== 'function'
    || typeof signal.aborted !== 'boolean') {
    throw new Error('缺少 DSH 本次调用的取消信号（AbortSignal）。')
  }
  signal.throwIfAborted()
  return { sessionId: invocation.agent.id, signal }
}

/** Parse only our positional grammar; the final free-text argument is not a shell command. */
export function parseLegionCommand(rawInput) {
  if (typeof rawInput !== 'string') throw new Error('无效的 /legion 命令文本。')
  let rest = rawInput.trimStart()
  const token = () => {
    const match = /^(\S+)([\s\S]*)$/u.exec(rest)
    if (!match) return undefined
    rest = match[2].trimStart()
    return match[1]
  }
  const required = label => {
    const value = token()
    if (!value) throw new Error(`缺少 ${label} 参数。用法见 /legion help。`)
    return value
  }
  const reference = () => {
    const value = required('任务引用 ref')
    if (value.toLowerCase() === 'latest') {
      throw new Error('请提供明确的任务引用；/legion 不支持 latest。')
    }
    return value
  }
  const body = label => {
    if (!rest.trim()) throw new Error(`缺少 ${label} 参数。用法见 /legion help。`)
    const value = rest
    rest = ''
    return value
  }
  const end = () => {
    if (rest.trim()) throw new Error('存在多余的位置参数。用法见 /legion help。')
  }
  const command = token() ?? 'help'
  if (['help', '--help', '-h'].includes(command)) {
    end()
    return { command: 'help' }
  }
  let args
  let method = command
  switch (command) {
    case 'start':
      method = 'submit'
      args = { key: required('key'), query: body('task') }
      break
    case 'continue':
      method = 'submit'
      args = { previous_task_ref: reference(), key: required('new-key'), query: body('feedback') }
      break
    case 'list':
      args = {}
      break
    case 'runtime': {
      const view = token() ?? 'members'
      if (!['members', 'capabilities', 'catalog'].includes(view)) throw new Error('runtime 只支持 members、capabilities 或 catalog。')
      args = { view }
      break
    }
    case 'status': {
      args = { task_ref: reference() }
      const cursor = token()
      if (cursor !== undefined) {
        if (!/^\d+$/u.test(cursor) || !Number.isSafeInteger(Number(cursor))) {
          throw new Error('事件游标必须是非负安全整数。用法：/legion status <ref> [cursor]')
        }
        args.after_event_id = Number(cursor)
      }
      break
    }
    case 'message':
      args = { task_ref: reference(), operation_key: required('operation-key'),
        recipient: required('recipient'), text: body('text') }
      break
    case 'resume':
      args = { task_ref: reference(), operation_key: required('operation-key') }
      break
    case 'cancel':
      args = { task_ref: reference() }
      if (rest.trim()) args.reason = body('reason')
      break
    case 'download':
      args = { task_ref: reference(), artifact_id: required('artifact-id') }
      break
    default:
      throw new Error('未知的 /legion 命令。用法见 /legion help。')
  }
  end()
  return { command, method, args }
}

const scalar = value => typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
const rows = value => Array.isArray(value) ? value : []

/** Human-only projection of the client's already-sanitized facts, never a quality verdict. */
export function formatLegionResult(command, result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error('军团返回了无效结果；未能确认操作状态。')
  }
  const lines = []
  const fact = (label, value) => { if (scalar(value)) lines.push(`${label}：${value}`) }
  fact('任务引用', result.task_ref)
  fact('军团队伍', result.team_id)
  if (result.reused === true) lines.push('复用已有记录；未再次发送本操作。')

  switch (command) {
    case 'runtime':
      fact('查看范围', result.view)
      fact('事实来源', result.source)
      fact('查询时间', result.observed_at)
      lines.push('已注册 ≠ 已安装 ≠ 当前可用；以下是服务原始事实，不是执行验收：')
      lines.push(JSON.stringify(result.runtime, null, 2))
      if (result.projection?.omitted_fields?.length || result.projection?.truncated_fields?.length) {
        lines.push(`脱敏/截断记录：${JSON.stringify(result.projection)}`)
      }
      break
    case 'start':
    case 'continue': {
      const labels = { accepted: '已收到提交回执（不代表完成）', unconfirmed: '尚未确认，请查询原任务',
        rejected: '服务已拒绝提交' }
      fact('提交状态', labels[result.submission] ?? result.submission ?? '未确认')
      if (command === 'continue') lines.push('这是同一军团会话的新轮次；不是恢复中断任务。')
      break
    }
    case 'list':
      lines.push('当前 DSH 会话的本地恢复索引（不是实时状态）：')
      if (!rows(result.tasks).length) lines.push('尚无本地记录；不能据此判断远端运行状态。')
      for (const task of rows(result.tasks)) {
        lines.push(`- ${task.task_ref} | key: ${task.key} | 队伍: ${task.team_id}`)
      }
      break
    case 'status': {
      fact('连接', result.connection)
      const tasks = rows(result.runtime?.tasks)
      if (!tasks.length) lines.push('未返回具体执行项状态。')
      for (const task of tasks) {
        lines.push(`- 执行项 ${task.task_id ?? '未标识'}：${task.status ?? '未知'}`)
        fact('进度', task.progress)
        fact('公开结果', task.result?.final)
        fact('执行错误', task.result?.error)
      }
      fact('服务取消标记（非进程核验）', result.runtime?.cancelled)
      lines.push(`本页进度事件：${rows(result.events).length} 条`)
      for (const event of rows(result.events)) {
        const detail = scalar(event.data) ? String(event.data) : [
          ...['text', 'message', 'stage', 'status', 'final', 'reason', 'error', 'failure_stage']
            .map(key => [key, event.data?.[key]]),
          ['outcome.failure_stage', event.data?.outcome?.failure_stage],
        ].filter(([, value]) => scalar(value)).map(([key, value]) => `${key}: ${value}`).join('；')
        lines.push(`- [${event.event_id ?? '?'}] ${event.event ?? 'event'}${detail ? ` — ${detail}` : ''}`)
      }
      fact('下次查询游标', result.next_event_id)
      if (result.more_events === true) lines.push('还有更多事件；使用本次游标查询后续。')
      lines.push(`服务列出的导出记录：${rows(result.artifacts).length}；本次查询不代表已下载。`)
      for (const artifact of rows(result.artifacts)) {
        lines.push(`- ${artifact.artifact_id} | ${artifact.name} | ${artifact.download_eligible === true ? '可下载' : '不可下载'}`)
        fact('下载限制', artifact.download_reason)
      }
      break
    }
    case 'message': {
      const labels = {
        received: '消息已进入邮箱；不代表作者已读或已处理。',
        rejected: '服务已拒绝消息请求；没有入箱确认，请核对拒绝原因。',
      }
      lines.push(labels[result.acknowledgement]
        ?? '消息入箱尚未确认；请查询原任务，不用新键盲目重发。')
      break
    }
    case 'resume': {
      const labels = { received: '恢复请求已收到。', rejected: '服务已拒绝恢复请求。' }
      lines.push(labels[result.acknowledgement] ?? '恢复请求尚未确认。')
      lines.push('仅请求恢复中断任务，不创建新轮次；是否恢复以服务状态为准。')
      break
    }
    case 'cancel': {
      const labels = {
        requested: '已发送取消请求（不代表已停止）',
        rejected: '服务已拒绝取消请求',
        unconfirmed: '取消结果尚未确认',
      }
      fact('取消请求', labels[result.cancellation] ?? result.cancellation ?? '未确认')
      lines.push(result.cancellation === 'rejected'
        ? '当前运行情况以原任务状态为准。'
        : '收到取消请求回执不代表任务或进程已经停止。')
      fact('服务停止确认', result.stopped_confirmed === true ? '服务标记已确认（非本地进程核验）' : '未确认')
      break
    }
    case 'download':
      fact('导出文件 id', result.artifact_id)
      fact('本地文件', result.local_path)
      fact('字节数', result.bytes)
      fact('SHA-256', result.sha256)
      fact('完整性检查', result.integrity)
      break
    default:
      throw new Error('未知的 /legion 结果类型。')
  }
  fact('HTTP 状态', result.http_status)
  fact('服务返回成功标记', result.server_response?.ok)
  fact('服务回执状态', result.server_response?.status)
  fact('服务回执错误', result.server_response?.error)
  fact('服务回执说明', result.server_response?.reason)
  fact('说明', result.reason)
  fact('后续操作', result.next)
  lines.push(FACT_BOUNDARY)
  return lines.join('\n')
}

/** One direct human action, not a model message, scheduler, retry loop or implicit resume. */
export async function executeLegionCommand(client, invocation) {
  try {
    if (invocation.attachments?.length) throw new Error('/legion 不接收附件；请提供明确的任务或消息文本。')
    const parsed = parseLegionCommand(invocation.rawInput)
    if (parsed.command === 'help') return { kind: 'success', text: LEGION_HELP }
    const execution = executionFrom(invocation)
    const result = parsed.method === 'list' ? await client.list(execution) : await client[parsed.method](parsed.args, execution)
    const text = formatLegionResult(parsed.command, result)
    const rejected = result.submission === 'rejected' || result.acknowledgement === 'rejected'
      || result.cancellation === 'rejected'
    return { kind: rejected ? 'error' : 'success', text }
  } catch (error) {
    // LegionClient's public contract supplies safe, actionable errors.
    return { kind: 'error', text: error instanceof Error ? error.message : '军团操作失败；请查询原任务状态。' }
  }
}
