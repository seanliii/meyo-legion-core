import test from 'node:test'
import assert from 'node:assert/strict'

const ref = 'a'.repeat(32)
const artifactId = 'b'.repeat(32)

async function commands() {
  let module
  try {
    module = await import('../lib/commands.js')
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('/lib/commands.js')) throw error
  }
  assert.equal(typeof module?.parseLegionCommand, 'function', 'The independent human-command parser must exist')
  return module
}

function invocation(rawInput, id = 'current-agent') {
  return { rawInput, agent: { id, session: { id: 'not-the-agent-id' } },
    signal: new AbortController().signal, attachments: [] }
}

test('parses the documented commands without shell parsing or rewriting trailing task/message text', async () => {
  const { parseLegionCommand: parse } = await commands()
  assert.deepEqual(parse(''), { command: 'help' })
  assert.deepEqual(parse(' help '), { command: 'help' })
  assert.deepEqual(parse(' --help'), { command: 'help' })
  assert.deepEqual(parse(' start key-1  Keep  "quotes"\nand newlines '), {
    command: 'start', method: 'submit', args: { key: 'key-1', query: 'Keep  "quotes"\nand newlines ' },
  })
  assert.deepEqual(parse(`continue ${ref} new-key Same project; new feedback`), {
    command: 'continue', method: 'submit',
    args: { previous_task_ref: ref, key: 'new-key', query: 'Same project; new feedback' },
  })
  assert.deepEqual(parse('list'), { command: 'list', method: 'list', args: {} })
  assert.deepEqual(parse(`status ${ref}`), { command: 'status', method: 'status', args: { task_ref: ref } })
  assert.deepEqual(parse(`status ${ref} 0`), { command: 'status', method: 'status', args: { task_ref: ref, after_event_id: 0 } })
  assert.deepEqual(parse(`message ${ref} note-1 author Keep  spaces\nnew line`), {
    command: 'message', method: 'message',
    args: { task_ref: ref, operation_key: 'note-1', recipient: 'author', text: 'Keep  spaces\nnew line' },
  })
  assert.deepEqual(parse(`resume ${ref} recovery-1`), {
    command: 'resume', method: 'resume', args: { task_ref: ref, operation_key: 'recovery-1' },
  })
  assert.deepEqual(parse(`cancel ${ref}`), { command: 'cancel', method: 'cancel', args: { task_ref: ref } })
  assert.deepEqual(parse(`cancel ${ref} User asked to stop`), {
    command: 'cancel', method: 'cancel', args: { task_ref: ref, reason: 'User asked to stop' },
  })
  assert.deepEqual(parse(`download ${ref} ${artifactId}`), {
    command: 'download', method: 'download', args: { task_ref: ref, artifact_id: artifactId },
  })
})

test('rejects missing references/keys/text, extra positional tokens and unsafe event cursors', async () => {
  const { parseLegionCommand: parse } = await commands()
  for (const input of [
    'start', 'start only-key', 'status', 'status latest', 'list unexpected', 'help unexpected',
    'message', `message ${ref} key author`, `resume ${ref}`, `resume ${ref} key unexpected`,
    'cancel', `download ${ref}`, `download ${ref} ${artifactId} extra`,
    `continue ${ref} new-key`, 'continue latest key feedback', 'unknown',
    ...['-1', '1.5', '1e2', 'Infinity', '9007199254740992', '0 extra'].map(cursor => `status ${ref} ${cursor}`),
  ]) assert.throws(() => parse(input), /legion|引用|参数|游标|用法|命令/i, input)
})

test('help explains semantics without touching a client or scheduling an agent', async () => {
  const { executeLegionCommand } = await commands()
  const client = new Proxy({}, { get() { assert.fail('Help must be local') } })
  const result = await executeLegionCommand(client, invocation('help'))
  assert.equal(result.kind, 'success')
  for (const command of ['start', 'status', 'list', 'message', 'resume', 'cancel', 'download', 'continue']) {
    assert.ok(result.text.includes(`/legion ${command}`))
  }
  assert.match(result.text, /done.*≠.*导出.*≠.*验收/)
  assert.match(result.text, /resume.*中断/s)
  assert.match(result.text, /continue.*新轮/s)
  assert.match(result.text, /已读|已处理/)
  assert.match(result.text, /明确.*引用|引用.*明确/)
})

test('all commands call only their corresponding client method with the exact invocation identity and signal', async () => {
  const { executeLegionCommand } = await commands()
  const cases = [
    ['start key Task text', 'submit', { key: 'key', query: 'Task text' }],
    [`continue ${ref} new-key Feedback`, 'submit', { previous_task_ref: ref, key: 'new-key', query: 'Feedback' }],
    ['list', 'list', undefined],
    [`status ${ref} 12`, 'status', { task_ref: ref, after_event_id: 12 }],
    [`message ${ref} msg-key author A message`, 'message',
      { task_ref: ref, operation_key: 'msg-key', recipient: 'author', text: 'A message' }],
    [`resume ${ref} recovery-key`, 'resume', { task_ref: ref, operation_key: 'recovery-key' }],
    [`cancel ${ref} Requested`, 'cancel', { task_ref: ref, reason: 'Requested' }],
    [`download ${ref} ${artifactId}`, 'download', { task_ref: ref, artifact_id: artifactId }],
  ]
  for (const [index, [input, method, expectedArgs]] of cases.entries()) {
    const calls = []
    const client = new Proxy({}, { get(_target, name) {
      return async (...args) => {
        calls.push({ name, args })
        return method === 'list' ? { tasks: [] } : { task_ref: ref }
      }
    } })
    const current = invocation(input, `agent-${index}`)
    current.agent.followup = () => assert.fail('Commands must not enter the DSH model loop')
    const result = await executeLegionCommand(client, current)
    assert.equal(result.kind, 'success', result.text)
    assert.equal(calls.length, 1)
    assert.equal(calls[0].name, method)
    if (expectedArgs) assert.deepEqual(calls[0].args[0], expectedArgs)
    const execution = calls[0].args.at(-1)
    assert.equal(execution.sessionId, current.agent.id)
    assert.equal(execution.signal, current.signal)
  }
})

test('invalid input, unsupported attachments, absent scope and caller abort never call the client', async () => {
  const { executeLegionCommand } = await commands()
  const client = new Proxy({}, { get() { assert.fail('Invalid commands must not call the client') } })
  const aborted = invocation('list')
  const controller = new AbortController()
  controller.abort()
  aborted.signal = controller.signal
  for (const input of [
    invocation('status latest'),
    { ...invocation('list'), attachments: [{ type: 'image' }] },
    { ...invocation('list'), agent: undefined },
    { ...invocation('list'), signal: undefined },
    aborted,
  ]) assert.equal((await executeLegionCommand(client, input)).kind, 'error')
})

test('safe client errors remain errors, never optimistic success or an automatic retry', async () => {
  const { executeLegionCommand } = await commands()
  let calls = 0
  const client = { async resume() {
    calls += 1
    throw new Error('Legion HTTP 401: set LEGION_PERSONAL_TOKEN in the host environment')
  } }
  const result = await executeLegionCommand(client, invocation(`resume ${ref} key`))
  assert.deepEqual(result, {
    kind: 'error', text: 'Legion HTTP 401: set LEGION_PERSONAL_TOKEN in the host environment',
  })
  assert.equal(calls, 1)
})

test('human status retains task progress, event cursor and separate export/verification facts', async () => {
  const { formatLegionResult } = await commands()
  const text = formatLegionResult('status', {
    task_ref: ref, team_id: 'team-fixture', connection: 'connected',
    runtime: { tasks: [{ task_id: 'author', status: 'done', result: { final: 'Public final answer' } }] },
    events: [{ event_id: 7, event: 'progress', data: { text: 'Public progress' } }],
    next_event_id: 7, more_events: true,
    artifacts: [{ artifact_id: artifactId, name: 'result.txt', download_eligible: false, download_reason: 'No hash' }],
    experience_verification: 'not_performed_by_plugin',
  })
  for (const expected of [ref, 'author', 'done', 'Public final answer', 'Public progress', '7', artifactId, 'result.txt', 'No hash']) {
    assert.ok(text.includes(expected), expected)
  }
  assert.match(text, /游标/)
  assert.match(text, /更多|后续/)
  assert.match(text, /done.*≠.*导出.*≠.*验收/)
  assert.match(text, /不.*验收|未.*验收/)
})

function planningFailureStatus() {
  return {
    task_ref: ref, team_id: 'team-fixture', connection: 'connected',
    runtime: { tasks: [] },
    events: [{ event_id: 7, event: 'final', data: {
      ok: false, status: 'error', error: 'planner_tool_unavailable',
      results: [], all_failed: true, delivered_count: 0, total: 0,
      outcome: {
        status: 'failed', failure_stage: 'planning',
        planner_error: 'planner_tool_unavailable', planner_attempts: [],
      },
    } }],
    next_event_id: 7, more_events: false, artifacts: [],
    artifact_source: 'authenticated_service_exports_observed_for_this_task',
    experience_verification: 'not_performed_by_plugin',
    projection: { omitted_fields: [], truncated_fields: [] },
  }
}

test('human status displays the final error even when planning created no runtime tasks', async () => {
  const { formatLegionResult } = await commands()
  const text = formatLegionResult('status', planningFailureStatus())
  assert.match(text, /error: planner_tool_unavailable/)
  assert.match(text, /未返回具体执行项状态/)
  assert.match(text, /导出记录：0/)
  assert.match(text, /done.*≠.*导出.*≠.*验收/)
})

test('human status displays the reported outcome failure stage without runtime tasks', async () => {
  const { formatLegionResult } = await commands()
  const text = formatLegionResult('status', planningFailureStatus())
  assert.match(text, /outcome\.failure_stage: planning/)
})

test('human status retains generic errors and distinct direct and nested failure stages', async () => {
  const { formatLegionResult } = await commands()
  const state = planningFailureStatus()
  state.events = [{ event_id: 8, event: 'progress', data: {
    status: 'partial', stage: 'checkpointed', error: 'operation_unavailable', failure_stage: 'execution',
    outcome: { status: 'failed', failure_stage: 'transfer' },
  } }]
  const original = structuredClone(state)
  const text = formatLegionResult('status', state)
  assert.match(text, /error: operation_unavailable/)
  assert.match(text, /(?:^|；)failure_stage: execution/m)
  assert.match(text, /outcome\.failure_stage: transfer/)
  assert.match(text, /status: partial/)
  assert.match(text, /stage: checkpointed/)
  assert.deepEqual(state, original)
})

test('status command exposes planning failure after one read without changing facts or redispatching', async () => {
  const { executeLegionCommand } = await commands()
  const state = planningFailureStatus()
  const original = structuredClone(state)
  const calls = []
  const client = new Proxy({}, { get(_target, method) {
    return async (...args) => { calls.push({ method, args }); return state }
  } })
  const current = invocation(`status ${ref}`)
  current.agent.followup = () => assert.fail('Status must not enter the model loop')
  const result = await executeLegionCommand(client, current)
  assert.equal(result.kind, 'success', 'Reading a failed task is still a successful status query')
  assert.match(result.text, /error: planner_tool_unavailable/)
  assert.match(result.text, /outcome\.failure_stage: planning/)
  assert.deepEqual(calls, [{ method: 'status', args: [
    { task_ref: ref }, { sessionId: current.agent.id, signal: current.signal },
  ] }])
  assert.deepEqual(state, original)
})

test('human status does not invent a missing error or failure stage from a failed status', async () => {
  const { formatLegionResult } = await commands()
  const state = planningFailureStatus()
  state.events = [{ event_id: 7, event: 'final', data: {
    ok: false, status: 'error', outcome: { status: 'failed' },
  } }]
  const text = formatLegionResult('status', state)
  assert.match(text, /status: error/)
  assert.doesNotMatch(text, /error:|failure_stage:|planner_tool_unavailable/)
})

test('human status skips non-scalar failure details instead of dumping event objects', async () => {
  const { formatLegionResult } = await commands()
  for (const value of [undefined, null, {}, [], { detail: 'not_displayed_detail' }, ['not_displayed_detail']]) {
    const state = planningFailureStatus()
    state.events = [{ event_id: 7, event: 'final', data: {
      status: 'error', error: value, failure_stage: value, outcome: { failure_stage: value },
    } }]
    const text = formatLegionResult('status', state)
    assert.match(text, /status: error/)
    assert.doesNotMatch(text, /error:|failure_stage:|not_displayed_detail|\[object Object\]/)
  }
})

test('human status keeps scalar event bodies and tolerates absent outcome details', async () => {
  const { formatLegionResult } = await commands()
  const state = planningFailureStatus()
  state.events = ['Public notice', 0, false, null, { outcome: null }].map((data, index) => ({
    event_id: index + 1, event: 'progress', data,
  }))
  const text = formatLegionResult('status', state)
  assert.match(text, /\[1\] progress — Public notice/)
  assert.match(text, /\[2\] progress — 0/)
  assert.match(text, /\[3\] progress — false/)
  assert.doesNotMatch(text, /error:|failure_stage:|\[object Object\]/)
})

test('human status displays a task result error even with an empty final and no events', async () => {
  const { formatLegionResult } = await commands()
  const state = planningFailureStatus()
  state.events = []
  state.runtime.tasks = [{ task_id: 'author', status: 'error', result: {
    final: '', error: 'Missing optional dependency: runtime-helper',
  } }]
  const original = structuredClone(state)
  const text = formatLegionResult('status', state)
  assert.match(text, /执行错误：Missing optional dependency: runtime-helper/)
  assert.match(text, /执行项 author：error/)
  assert.match(text, /导出记录：0/)
  assert.match(text, /done.*≠.*导出.*≠.*验收/)
  assert.deepEqual(state, original)
})

test('human status preserves scalar task result errors alongside the public final', async () => {
  const { formatLegionResult } = await commands()
  for (const [error, expected] of [[0, '0'], [false, 'false'], [503, '503'], ['', '']]) {
    const state = planningFailureStatus()
    state.events = []
    state.runtime.tasks = [{ task_id: 'author', status: 'partial', result: {
      final: 'Partial public answer', error,
    } }]
    const text = formatLegionResult('status', state)
    assert.ok(text.split('\n').includes(`执行错误：${expected}`))
    assert.match(text, /公开结果：Partial public answer/)
  }
})

test('human status omits missing or non-scalar task result errors without dumping objects', async () => {
  const { formatLegionResult } = await commands()
  for (const result of [
    undefined, null, {}, { error: undefined }, { error: null },
    { error: { detail: 'not_displayed_detail' } }, { error: ['not_displayed_detail'] },
  ]) {
    const state = planningFailureStatus()
    state.events = []
    state.runtime.tasks = [{ task_id: 'author', status: 'error', result }]
    const text = formatLegionResult('status', state)
    assert.match(text, /执行项 author：error/)
    assert.doesNotMatch(text, /执行错误：|not_displayed_detail|\[object Object\]/)
  }
})

test('list is a local recovery index, and missing exports or observations are not invented', async () => {
  const { formatLegionResult } = await commands()
  const list = formatLegionResult('list', {
    tasks: [{ task_ref: ref, key: 'first-task', team_id: 'team-fixture' }],
    source: 'local_recovery_index_not_live_status',
  })
  assert.ok(list.includes(ref))
  assert.ok(list.includes('first-task'))
  assert.match(list, /本地.*索引/)
  assert.match(list, /不是实时|非实时/)
  const state = formatLegionResult('status', { task_ref: ref, runtime: { tasks: [] }, artifacts: [] })
  assert.match(state, /导出.*0|0.*导出/)
  assert.match(state, /done.*≠.*导出.*≠.*验收/)
})

test('uncertain submit/message/resume and cancellation do not claim processing, new dispatch or stopped processes', async () => {
  const { formatLegionResult: render } = await commands()
  const submit = render('start', { task_ref: ref, submission: 'unconfirmed', reason: 'Connection lost', reused: true })
  assert.match(submit, /未确认|尚未确认/)
  assert.match(submit, /Connection lost/)
  assert.match(submit, /复用/)
  const message = render('message', { task_ref: ref, acknowledgement: 'received', consumed_by_model_confirmed: false })
  assert.match(message, /邮箱/)
  assert.match(message, /不代表.*已读|不代表.*处理/)
  const pending = render('message', { task_ref: ref, acknowledgement: 'unconfirmed' })
  assert.match(pending, /未确认|尚未确认/)
  assert.ok(!pending.includes('已进入邮箱'))
  const resume = render('resume', {
    task_ref: ref, acknowledgement: 'received', server_response: { status: 'unchanged', reason: 'no recoverable tasks' },
  })
  assert.match(resume, /unchanged/)
  assert.match(resume, /no recoverable tasks/)
  assert.match(resume, /中断/)
  assert.match(resume, /不.*新轮|不.*新任务/)
  const cancel = render('cancel', { task_ref: ref, cancellation: 'requested', stopped_confirmed: false })
  assert.match(cancel, /取消.*请求|请求.*取消/)
  assert.match(cancel, /停止.*未确认|未确认.*停止|不代表.*停止/)
})

test('explicitly rejected acknowledgements remain rejections, including a reused receipt', async () => {
  const { formatLegionResult } = await commands()
  for (const command of ['message', 'resume']) {
    const text = formatLegionResult(command, {
      task_ref: ref, acknowledgement: 'rejected', http_status: 403, reused: true,
      reason: 'The service refused this operation',
    })
    assert.match(text, /服务.*拒绝/)
    assert.match(text, /403/)
    assert.ok(text.includes(ref))
    assert.match(text, /复用/)
    assert.match(text, /The service refused this operation/)
    assert.doesNotMatch(text, /尚未确认|已进入邮箱|恢复请求已收到/)
  }
})

test('a rejected cancellation shows the original refusal, not an accepted request or stopped task', async () => {
  const { formatLegionResult } = await commands()
  const text = formatLegionResult('cancel', {
    task_ref: ref, cancellation: 'rejected', stopped_confirmed: false,
    server_response: { ok: false, error: 'synthetic refusal', reason: 'cancellation not registered' },
  })
  assert.match(text, /服务.*拒绝.*取消/)
  assert.match(text, /服务停止确认：未确认/)
  assert.match(text, /false/)
  assert.match(text, /synthetic refusal/)
  assert.match(text, /cancellation not registered/)
  assert.doesNotMatch(text, /已请求|已发送取消|收到取消请求回执/)
})

test('commands mark explicit refusal as an error without turning uncertain receipts into failures', async () => {
  const { executeLegionCommand } = await commands()
  const cases = [
    ['start key Task', 'submit', 'submission'],
    [`continue ${ref} next-key Feedback`, 'submit', 'submission'],
    [`message ${ref} note author Feedback`, 'message', 'acknowledgement'],
    [`resume ${ref} recovery`, 'resume', 'acknowledgement'],
    [`cancel ${ref}`, 'cancel', 'cancellation'],
  ]
  for (const [input, method, field] of cases) {
    let calls = 0
    let receipt = 'rejected'
    const client = { async [method]() { calls++; return { task_ref: ref, [field]: receipt, http_status: 403 } } }
    const rejected = await executeLegionCommand(client, invocation(input))
    assert.equal(rejected.kind, 'error', `${method}: explicit rejection is not command success`)
    receipt = 'unconfirmed'
    const uncertain = await executeLegionCommand(client, invocation(input))
    assert.equal(uncertain.kind, 'success', 'An uncertain remote result is not proof of rejection')
    assert.match(uncertain.text, /尚未确认/)
    assert.equal(calls, 2, 'Only the two explicit invocations may call the client')
  }
})

test('download reports saved bytes/hash/path, not experience acceptance', async () => {
  const { formatLegionResult } = await commands()
  const text = formatLegionResult('download', {
    task_ref: ref, artifact_id: artifactId, local_path: '/fixture/result.txt', bytes: 42,
    sha256: 'c'.repeat(64), integrity: 'sha256_verified', experience_verification: 'not_performed_by_plugin',
  })
  for (const expected of ['/fixture/result.txt', '42', 'c'.repeat(64), 'sha256_verified']) assert.ok(text.includes(expected))
  assert.match(text, /done.*≠.*导出.*≠.*验收/)
})
