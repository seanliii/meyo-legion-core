import { isAbsolute } from 'node:path'
import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { LegionClient, validateCandidateHarnesses } from './lib/client.js'
import { executeLegionCommand, executionFrom } from './lib/commands.js'
import { capabilityContext } from './lib/ecosystem.js'
import { registerEcosystem } from './lib/extension.js'
import { credentialFilePath } from './lib/credentials.js'
import { executeWorkbenchCommand } from './lib/workbench.js'
import { validateTeamProfile } from './lib/team.js'
import { createTeamFlow } from './lib/team-flow.js'

export const name = 'legion'
export const inject = ['tools']

function validateBaseUrl(value) {
  let url
  try { url = new URL(value) } catch { throw new Error('Legion baseUrl must be a valid HTTP(S) URL') }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) {
    throw new Error('Legion baseUrl requires HTTPS; HTTP is allowed only on loopback')
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('Legion baseUrl must not contain credentials, a query or a fragment')
  }
  return value
}

// Declaration-only fields for native Settings. The host validator below reads
// raw user data first, before Schemastery can omit nulls or echo invalid values.
const teamDefaults = validateTeamProfile({})
const teamProfileForm = Schema.object({
  name: Schema.string().pattern(/\S/).max(80).default(teamDefaults.name).description('战队名称。'),
  goal: Schema.string().pattern(/\S/).max(4000).role('textarea').default(teamDefaults.goal)
    .description('长期目标；仅作为用户上下文，不替换当前任务或扩大权限。'),
  collaboration: Schema.union(['auto', 'leader', 'custom']).default(teamDefaults.collaboration)
    .description('自主组织、指定带队者，或按用户要求协作。'),
  leader: Schema.string().max(100).default(teamDefaults.leader)
    .description('指定带队者时必填，且须属于 candidateHarnesses。'),
  instructions: Schema.string().max(2000).role('textarea').default(teamDefaults.instructions)
    .description('用户的协作要求；自定义协作时必填。'),
  evolutionEnabled: Schema.boolean().default(teamDefaults.evolutionEnabled)
    .description('是否允许在既有权限内按真实工作需求持续改进；不代表已执行或已验证效果。'),
  cadence: Schema.union(['weekly', 'daily', 'manual']).default(teamDefaults.cadence)
    .description('原生战队流程的发现频率：每周、每日或仅手动。'),
})

const fields = {
  baseUrl: Schema.transform(Schema.string(), validateBaseUrl, true)
    .description('Legion 服务地址；省略时使用 LEGION_BASE_URL，未设置时使用 http://127.0.0.1:8000。保存配置会重载军团插件并中止本地在途请求，不取消远端任务。'),
  candidateHarnesses: Schema.transform(Schema.array(Schema.string()), validateCandidateHarnesses, true)
    .description('可选的用户指定成员候选，按原顺序传给服务；省略或空列表不发送 plan。不由模型参数提供，不增加权限或兜底。'),
  teamProfile: Schema.transform(Schema.any(), validateTeamProfile, true)
    .description('可选的用户战队配置；省略保留旧行为。组队向导保存后通过原生工作台启动，读取配置或加载插件本身不派单。'),
  tokenEnv: Schema.string().pattern(/^[A-Za-z_][A-Za-z0-9_]*$/).default('LEGION_PERSONAL_TOKEN')
    .description('保存个人令牌的环境变量名；不填写令牌本身。'),
  tokenFile: Schema.transform(Schema.string(), credentialFilePath, true)
    .description('可选的个人令牌文件绝对路径（本人所有的0600普通文件）。设置后优先于tokenEnv，读取失败不回退；不填写令牌本身。'),
  stateDirectory: Schema.string().pattern(/\S/).description('本机任务恢复索引目录（可选）。'),
  requestTimeoutMs: Schema.natural().min(100).max(300000).default(30000)
    .description('单次 HTTP 等待上限，单位毫秒；超时不取消远端任务。'),
  maxResponseBytes: Schema.natural().min(1024).max(32 * 1024 * 1024).default(4 * 1024 * 1024)
    .description('单次响应的字节上限。'),
  maxArtifactBytes: Schema.natural().min(1).max(1024 * 1024 * 1024).default(50 * 1024 * 1024)
    .description('单个下载文件的字节上限。'),
  maxEvents: Schema.natural().min(1).max(500).default(100)
    .description('每次状态查询返回的事件数量上限。'),
  projectBindingFile: Schema.transform(Schema.string(), value => {
    if (!isAbsolute(value) || value.includes('\0')) {
      throw new Error('projectBindingFile must be an absolute user-configured file path')
    }
    return value
  }, true).description('用户配置的项目授权绑定文件路径；不由模型参数提供。'),
}

// Schemastery object schemas preserve unknown keys by default. Check raw keys
// before normalization so credentials and lifecycleSignal cannot enter config.
const knownFields = Schema.transform(Schema.any(), value => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Legion configuration must be an object')
  }
  if (Reflect.ownKeys(value).some(key => !Object.hasOwn(fields, key))) {
    throw new Error('Unknown Legion configuration field; use tokenEnv or tokenFile, never an inline token or lifecycleSignal')
  }
  // Validate before Schemastery can normalize an explicit null to "omitted".
  if (Object.hasOwn(value, 'candidateHarnesses')) validateCandidateHarnesses(value.candidateHarnesses)
  if (Object.hasOwn(value, 'teamProfile')) validateTeamProfile(value.teamProfile)
  if (Object.hasOwn(value, 'tokenFile')) credentialFilePath(value.tokenFile)
  return {}
}, true).hidden()

// One atomic snapshot: Settings exposes the existing fields, and the owning
// fiber reloads them together rather than mixing a new URL with an old identity.
export const Config = Schema.intersect([knownFields, Schema.object(fields)]).default({}).volatile()

// Settings clones and serializes the schema twice. Its current Schemastery
// loses transform callbacks on that second pass. Publish a declaration-only
// form, not executable host callbacks. Config / ~standard validation stays
// intact: ConfigEditor still validates every raw write before persisting it.
const form = Schema.object(Object.fromEntries(Object.entries(fields).map(([key, node]) => {
  const field = new Schema(key === 'teamProfile' ? teamProfileForm : node.type === 'transform' ? node.inner : node)
  field.meta = { ...field.meta, ...node.meta }
  if (node.type === 'transform' && !Object.hasOwn(node.meta, 'default')) delete field.meta.default
  return [key, field]
}))).default({}).volatile()
Config.toJSON = () => form.toJSON()

const text = description => ({ type: 'string', required: true, description })
const taskRef = text('当前 DSH 会话中已有的 task_ref，来自 legion_submit 或 legion_list；必须明确指定。')
const operationKey = text('本次操作的稳定去重键；查询或重试同一操作时保留原键。')
const tools = [
  {
    method: 'runtime',
    description: '只读军团当前成员、能力或目录事实；保留未安装、预热、过期和错误，不把注册当可用。不安装、不强制探活、不执行模型任务。',
    parameters: {
      view: { type: 'string', enum: ['members', 'capabilities', 'catalog'], description: '默认 members；仅查询已有服务端点。' },
    },
  },
  {
    method: 'submit',
    description: '向军团提交任务并返回可恢复的引用；同一 key 不重复派单。previous_task_ref 在同一军团会话开启新轮次。',
    parameters: {
      key: text('本次任务的稳定去重键。'),
      query: text('原始任务或新轮次反馈。'),
      context: { type: 'string', description: '调用方明确提供的补充背景，不是权限或系统指令。' },
      previous_task_ref: { type: 'string', description: '可选：同会话上一轮的任务引用；不是恢复中断。' },
      tool_names: { type: 'array', items: { type: 'string' },
        description: '可选：把当前 DSH Agent 明确选中的可见工具 schema 作为上下文传给军团，不授予远程工具权限。' },
    },
  },
  {
    method: 'list',
    description: '列出当前 DSH 会话的本地任务恢复索引；不是实时进度查询。',
    parameters: {},
  },
  {
    method: 'status',
    description: '查询原任务状态、进度事件与导出记录，不重新派单。done 不等于已导出或已验收。',
    parameters: {
      task_ref: taskRef,
      after_event_id: { type: 'integer', description: '只读取此事件序号之后的进度；省略时从 0 开始。' },
      after_message_id: { type: 'integer',
        description: '只显示此消息 ID 之后的 commander 来信；省略从 0 开始。仅分页，不标读或确认模型消费。' },
    },
  },
  {
    method: 'message',
    description: '向原任务指定收件人发送消息；回执只证明进入邮箱，不证明已读或已处理，不自动恢复任务。',
    parameters: {
      task_ref: taskRef, operation_key: operationKey,
      recipient: text('明确的收件人任务 id。'), text: text('消息或反馈原文。'),
    },
  },
  {
    method: 'resume',
    description: '请求恢复原任务的中断执行；不创建新轮次。新轮次请用 legion_submit 的 previous_task_ref。',
    parameters: { task_ref: taskRef, operation_key: operationKey },
  },
  {
    method: 'cancel',
    description: '请求取消明确的远端任务；收到请求回执不等于任务或进程已经停止。',
    parameters: { task_ref: taskRef, reason: { type: 'string', description: '用户给出的取消原因（可选）。' } },
  },
  {
    method: 'download',
    description: '下载 legion_status 列出的可下载导出文件并核对字节与 SHA-256；下载不是作品验收。',
    parameters: { task_ref: taskRef, artifact_id: text('legion_status 返回的 artifact_id；不是任意 URL 或路径。') },
  },
]

export function apply(ctx, liveConfig) {
  const config = liveConfig.get()
  const lifecycle = new AbortController()
  const client = new LegionClient({ ...config, lifecycleSignal: lifecycle.signal })
  const flow = createTeamFlow({
    getSchedule: () => ctx.get('schedule'),
    profile: config.teamProfile,
    candidates: config.candidateHarnesses,
    lifecycleSignal: lifecycle.signal,
  })
  const dispose = () => {
    if (lifecycle.signal.aborted) return
    lifecycle.abort(new DOMException('Legion plugin unloaded; remote tasks were not cancelled', 'AbortError'))
    return client.dispose()
  }
  ctx.effect(() => dispose, 'legion HTTP lifetime')
  ctx.on('loader/volatile-update', () => {
    // Loader has committed the new raw config before this owning-fiber event.
    // Stop old closures synchronously; official reload cleans every registration.
    dispose()
    void ctx.fiber.restart().catch(() => {
      // ConfigEditor observes inactive fibers and rolls back failed reconciliation.
      ctx.logger.warn('Legion configuration reload failed; the old client remains closed')
    })
  })

  for (const { method, description, parameters } of tools) {
    ctx.tools.register(defineTool({
      name: `legion_${method}`,
      description,
      parameters,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      async execute(args, exec) {
        const execution = executionFrom(exec)
        // defineTool has an open parameter root. Never pass undeclared authority
        // fields, even when a caller includes them in an otherwise valid call.
        const input = Object.fromEntries(Object.keys(parameters)
          .filter(key => Object.hasOwn(args, key))
          .map(key => [key, args[key]]))
        if (method === 'submit') {
          const context = capabilityContext(ctx, exec, input.tool_names)
          delete input.tool_names
          if (context) input.context = [input.context, context].filter(Boolean).join('\n\n')
        }
        return method === 'list' ? client.list(execution) : client[method](input, execution)
      },
    }))
  }

  const { sources } = registerEcosystem(ctx, client, config, lifecycle.signal)

  // CLI/Web adapters may provide commands; headless hosts need only the tools.
  // Own registration through the injected child, so service replacement cleans up.
  ctx.inject(['commands'], commandCtx => {
    commandCtx.commands.register({
      name: 'legion',
      description: '提交或跟进军团任务，查询进度、发送消息、恢复、取消与下载。',
      input: { hint: 'help | runtime | start | list | status | message | resume | cancel | download | continue' },
      handler: invocation => executeLegionCommand(client, invocation),
    })
    commandCtx.commands.register({
      name: 'legion-panel',
      description: '军团工作台的原生数据接口；使用当前会话身份，不把界面查询记入对话输入。',
      recordInput: false,
      input: { hint: 'JSON: action, args' },
      handler: invocation => executeWorkbenchCommand(commandCtx, client, sources, {
        ...invocation,
        // Public reads bypass LegionClient but still belong to this plugin lifetime.
        signal: AbortSignal.any([invocation.signal, lifecycle.signal]),
      }, flow),
    })
  })
}
