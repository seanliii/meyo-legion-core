import test from 'node:test'
import assert from 'node:assert/strict'

const api = await import('../lib/team.js').catch(error => {
  if (error.code === 'ERR_MODULE_NOT_FOUND' && error.url === new URL('../lib/team.js', import.meta.url).href) return {}
  throw error
})
const defaults = {
  name: '我的战队',
  goal: '让我的 DSH 和战队在真实工作中持续变强',
  collaboration: 'auto',
  leader: '',
  instructions: '',
  evolutionEnabled: true,
  cadence: 'weekly',
}
function teamApi() {
  assert.equal(typeof api.validateTeamProfile, 'function', 'validateTeamProfile must be exported')
  assert.equal(typeof api.teamContext, 'function', 'teamContext must be exported')
  return api
}

test('omitted team configuration remains absent and adds no context', () => {
  const { validateTeamProfile, teamContext } = teamApi()
  assert.equal(validateTeamProfile(undefined), undefined)
  assert.equal(teamContext(undefined, []), '')
  assert.equal(teamContext(undefined, ['member-a']), '')
})

test('an explicitly configured team has complete defaults and an immutable detached snapshot', () => {
  const { validateTeamProfile } = teamApi()
  assert.deepEqual(validateTeamProfile({}), defaults)
  const input = { name: ' \u3000研究队 🧭\n', goal: ' 做好本次工作\n并检查结果 ', instructions: ' 分工前先理解问题 ' }
  const before = { ...input }
  const profile = validateTeamProfile(input)
  assert.deepEqual(profile, {
    ...defaults, name: '研究队 🧭', goal: '做好本次工作\n并检查结果', instructions: '分工前先理解问题',
  })
  assert.deepEqual(input, before, 'Normalization must not edit caller-owned configuration')
  input.goal = 'Changed after construction'
  assert.equal(profile.goal, '做好本次工作\n并检查结果')
  assert.ok(Object.isFrozen(profile))
  const reordered = Object.fromEntries(Object.entries(profile).reverse())
  assert.equal(JSON.stringify(validateTeamProfile(reordered)), JSON.stringify(profile),
    'Configuration key order must not change a reservation fingerprint')
})

test('team profiles reject non-records, foreign prototypes and unknown fields without echoing input', () => {
  const { validateTeamProfile } = teamApi()
  const secret = 'synthetic-private-config-marker'
  const hidden = Object.defineProperty({}, secret, { value: secret })
  const symbol = { [Symbol(secret)]: secret }
  for (const value of [
    null, [], '', secret, 1, true, () => {}, new Date(), new String(secret),
    Object.create({ name: secret }), new (class Profile {})(),
    { uid: secret }, { authority: secret }, { permissions: secret },
    { [secret]: secret }, JSON.parse(`{"__proto__":{"name":"${secret}"}}`),
    hidden, symbol,
  ]) {
    assert.throws(() => validateTeamProfile(value), error => {
      assert.ok(error instanceof Error)
      assert.match(error.message, /teamProfile/)
      assert.ok(!String(error.stack).includes(secret), 'Errors must not expose configuration names or values')
      assert.ok(!JSON.stringify(error).includes(secret))
      return true
    })
  }
})

test('team validation never evaluates accessors or input serialization hooks', () => {
  const { validateTeamProfile } = teamApi()
  let reads = 0
  const input = Object.defineProperty({}, 'goal', {
    enumerable: true,
    get() { reads++; throw new Error('synthetic-accessor-secret') },
  })
  assert.throws(() => validateTeamProfile(input), /teamProfile/)
  assert.equal(reads, 0)
  assert.throws(() => validateTeamProfile({
    toJSON() { reads++; throw new Error('synthetic-toJSON-secret') },
  }), /teamProfile/)
  assert.equal(reads, 0)
})

test('text limits accept well-formed Unicode and reject oversized, empty or non-text values', () => {
  const { validateTeamProfile } = teamApi()
  for (const [field, max] of [['name', 80], ['goal', 4000], ['instructions', 2000], ['leader', 100]]) {
    const atLimit = '🧭'.repeat(max / 2)
    assert.equal(validateTeamProfile({ [field]: atLimit })[field], atLimit)
    assert.equal(validateTeamProfile({ [field]: ` \u3000${atLimit}\n` })[field], atLimit)
    for (const value of [
      'x'.repeat(max + 1), '\ud800', '\udfff', 'before\ud800after', null, undefined,
      123, true, {}, [], new String('text'),
    ]) {
      assert.throws(() => validateTeamProfile({ [field]: value }), /teamProfile/)
    }
  }
  for (const field of ['name', 'goal']) {
    for (const value of ['', ' \n\t\u3000 ']) {
      assert.throws(() => validateTeamProfile({ [field]: value }), /teamProfile/)
    }
  }
  const empty = validateTeamProfile({ leader: ' \n', instructions: ' \u3000' })
  assert.equal(empty.leader, '')
  assert.equal(empty.instructions, '')
})

test('enums and evolution flags are strict rather than truthy or nullish coercions', () => {
  const { validateTeamProfile } = teamApi()
  for (const value of [null, undefined, 0, 1, 'true', 'false', [], {}]) {
    assert.throws(() => validateTeamProfile({ evolutionEnabled: value }), /teamProfile/)
  }
  assert.equal(validateTeamProfile({ evolutionEnabled: false }).evolutionEnabled, false)
  for (const value of [null, undefined, '', 'AUTO', 'leader ', 'system', 1, {}, []]) {
    assert.throws(() => validateTeamProfile({ collaboration: value }), /teamProfile/)
  }
  for (const value of [null, undefined, '', 'hourly', ' weekly', 1, {}, []]) {
    assert.throws(() => validateTeamProfile({ cadence: value }), /teamProfile/)
  }
  for (const cadence of ['weekly', 'daily', 'manual']) {
    assert.equal(validateTeamProfile({ cadence }).cadence, cadence)
  }
})

test('leader and custom modes require their explicit text, not a silently invented policy', () => {
  const { validateTeamProfile } = teamApi()
  for (const leader of [undefined, '', ' \n']) {
    assert.throws(() => validateTeamProfile({ collaboration: 'leader', leader }), /leader/)
  }
  for (const instructions of [undefined, '', ' \n']) {
    assert.throws(() => validateTeamProfile({ collaboration: 'custom', instructions }), /instructions/)
  }
  const led = validateTeamProfile({ collaboration: 'leader', leader: ' member-a ' })
  assert.equal(led.leader, 'member-a')
  const custom = validateTeamProfile({ collaboration: 'custom', instructions: ' 先一起理解本次需求 ' })
  assert.equal(custom.instructions, '先一起理解本次需求')
})

test('team context is explicit user content, not a system instruction or a substitute for the current task', () => {
  const { teamContext } = teamApi()
  const goal = '理解真实失败再改进，不替换本次问题'
  const instructions = '讨论 system、authority、token 这些术语；\n保留这行用户要求。'
  const context = teamContext({
    name: '共同工作队', goal, collaboration: 'custom', instructions,
    evolutionEnabled: false, cadence: 'manual',
  }, [])
  assert.equal(typeof context, 'string')
  assert.ok(context.includes('共同工作队'))
  assert.ok(context.includes(goal))
  assert.ok(context.includes(instructions), 'User-authored requirements must not be keyword filtered')
  assert.match(context, /用户(?:明确)?配置/)
  assert.match(context, /不是系统指令|not a system instruction/i)
  assert.match(context, /不(?:增加|扩大|提升|改变)[^\n]*(?:权限|授权)/)
  assert.match(context, /(?:本次|当前)(?:问题|任务)/)
  assert.match(context, /关闭|false/)
  assert.match(context, /仅手动|manual/)
})

test('only an explicitly selected leader may appear as the designated leader in context', () => {
  const { teamContext } = teamApi()
  const profile = { collaboration: 'leader', leader: 'member-a' }
  const context = teamContext(profile, ['member-b', 'member-a'])
  assert.ok(context.includes('member-a'))
  assert.match(context, /带队|leader/)
  for (const candidates of [undefined, [], ['member-b']]) {
    assert.throws(() => teamContext(profile, candidates), error => {
      assert.match(error.message, /leader|带队/)
      assert.match(error.message, /候选|candidate/)
      assert.ok(!error.message.includes('member-a'))
      assert.ok(!error.message.includes('member-b'))
      return true
    })
  }
})

test('the native author can see which companions the user selected before delegating work', () => {
  const { teamContext } = teamApi()
  const context = teamContext({}, ['member-b', 'member-a'])
  assert.ok(context.includes('["member-b","member-a"]'), 'Do not hide the selected team behind an unexplained reference')
  assert.match(context, /不代表.*(?:安装|就绪)/)
})

test('empty candidates allow autonomous or custom collaboration and context stays bounded', () => {
  const { teamContext } = teamApi()
  for (const candidates of [undefined, []]) {
    assert.match(teamContext({}, candidates), /自主/)
    assert.ok(teamContext({ collaboration: 'custom', instructions: '按当前需求共同协作' }, candidates)
      .includes('按当前需求共同协作'))
  }
  const context = teamContext({
    name: '名'.repeat(80), goal: '目'.repeat(4000), instructions: '求'.repeat(2000),
    collaboration: 'leader', leader: 'a'.repeat(100), cadence: 'daily',
  }, ['a'.repeat(100), ...Array.from({ length: 1500 }, (_, index) => `member-${index}`)])
  assert.ok(context.includes('目'.repeat(4000)))
  assert.ok(context.includes('求'.repeat(2000)))
  assert.ok(context.length < 8000, 'Team context must not expand into a large roster or repeated tutorial')
  assert.ok(context.isWellFormed())
  assert.match(context, /每日|daily/)
})
