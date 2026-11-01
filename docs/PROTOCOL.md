# Protocol / 协议

本页描述 **Legion 0.4.0** 的连接器代码契约，不保证所有 Legion 部署都实现相同 API。DSH 保留主循环和工具决策；连接器不增设调度器或质量裁判。入门见 [English](../README.md)／[中文](../README.zh-CN.md)，独立程序见[核心说明](CORE.md)，实装与发布进度只在[状态表](PLAN.md)维护。**代码契约不是浏览器配置、完整工作台或公开安装验收记录。**

## 1. Runtime and native tools

清单要求 Node `>=24.9.0`、`engines.dsh: "0.2.1-rc.2"`；peers 为 `@deepseek-ai/dsh-llm`、`@deepseek-ai/dsh-tools` 的精确 `0.2.1-rc.2` 及 `@deepseek-ai/schemastery: "~3.18.5"`。`dsh.bundle.patch` 指向 `./cordis.patch.yml`，连接器自身只插入 `id: legion`、`name: dsh-plugin-legion` 一行；它与生成的多能力包是不同安装件。

- `write` 负责工作区写入，`plugin_manager` 负责安装／启停；市场发现使用可选、当前 Agent 可见的 `plugin_market`。
- 有 `skills` 服务才注册 `legion-ecosystem`；有 `commands` 才注册 `/legion` 和 `/legion-panel`。后者是工作台的人类命令，不增加模型工具数量。
- `legion_call` 的原生工具委托使用 `ctx.tools.execute({ callId, rootCallId, parent: exec.token, agent: exec.agent, signal: exec.signal, name, arguments })`，不直调工具执行体。signal 同时响应原生取消和连接器卸载，`deferContext`／`concludeTurn` 绑定原始 execution；这些对象不发给 Legion。人类命令不伪造这类 `ToolExecution` token 或调用链。
- 成功图片内容经本地 user context 转交，`additionalContexts` 和结束回合信息沿原生路径转交，不等于远端文本消息携带媒体。

## 2. Model tools and commands

共 **12 个模型工具**；调用身份由 Host 提供，不由模型参数指定。发现、读公开源和包装无需 Legion token；其余服务查询与任务协作需要个人服务与授权，包含不发 HTTP 的 `legion_call`。

| 工具 | 必填 | 可选 |
|---|---|---|
| `legion_discover` | 无 | `query`, `source`, `limit`, `offset`, `index_url` |
| `legion_source` | `action`, `repository`；`read` 还需 `commit`, `path` | inspect 的 `ref`；read 的 `offset`, `limit` |
| `legion_bundle` | `directory`, `spec`, `source_refs` | 对象内可选项见第 5 节 |
| `legion_runtime` | 无 | `view`: `members`（默认）、`capabilities`、`catalog` |
| `legion_call` | `task_ref`, `recipient`, `operation_key`, `tool_name`, `arguments` | 无 |
| `legion_submit` | `key`, `query` | `context`, `previous_task_ref`, `tool_names` |
| `legion_list` | 无 | 无 |
| `legion_status` | `task_ref` | `after_event_id`, `after_message_id` |
| `legion_message` | `task_ref`, `operation_key`, `recipient`, `text` | 无 |
| `legion_resume` | `task_ref`, `operation_key` | 无 |
| `legion_cancel` | `task_ref` | `reason` |
| `legion_download` | `task_ref`, `artifact_id` | 无 |

discover／source／bundle／call 不是 `/legion` 的位置参数子命令；普通对话中的模型工具保留原入口。工作台另用下文的 `/legion-panel`，不能借它打包或执行原生工具。既有 `/legion` 命令如下：

```text
/legion help
/legion runtime [members|capabilities|catalog]
/legion start <key> <task>
/legion list
/legion status <ref> [cursor]
/legion continue <ref> <new-key> <feedback>
/legion message <ref> <opkey> <taskid> <text>
/legion resume <ref> <opkey>
/legion cancel <ref> [reason]
/legion download <ref> <artifact-id>
```

`status` 命令的 cursor 是事件游标；消息游标用模型工具参数。引用必须明确，不支持 `latest` 或命令附件。命令成功、工具执行和远端任务完成不是同一观察。

<a id="legion-panel"></a>

### Native workbench command / 原生工作台命令

入口为 `executeWorkbenchCommand(ctx, client, sources, invocation)`；复用既有 `LegionClient` 和 `registerEcosystem` 创建的**同一个 `GitHubSources`**，没有另起 Agent、安装器或 HTTP 接口。Host 注册 `/legion-panel` 时设置 `recordInput: false`，不把界面请求记成对话输入；这不是结果、Host 日志或远端任务的零留存承诺。

`invocation` 是 Host 提供的 `CommandInvocation { agent, rawInput, signal, attachments }`。`executionFrom` 只从真实 `agent.id` 和本次 `AbortSignal` 取得会话与取消上下文；无有效身份、无信号或已取消时失败，不借用其他会话。`attachments` 只能省略或为 `[]`。

`rawInput` 是命令名后面的 JSON 文本，最多 **2 MiB（UTF-8）**；根对象只接受 `action`、可选 `args`。省略 `args` 等于 `{}`，显式 `null`、数组、未知字段或未知动作均拒绝，不做类型转换。不能传入 `agentId`、`sessionId`、`agent`、`config`、`token`、`tokenFile`、`baseUrl`、`signal`、`callId` 等权限／环境字段；`context` 也只能是背景文本，不是权限对象。

| `action` | `args` 与边界 | 复用入口 |
|---|---|---|
| `runtime` | 必填 `view: "members"`、`"capabilities"` 或 `"catalog"`；本命令不设默认 view。仅 members 支持 `offset`（默认 0）和 `limit`（默认 12，1–30）。 | `client.runtime`；分页仅在工作台投影处理，不向核心虚构筛选参数 |
| `tools` | 可选 `query`（最多 500 字符，允许空）、`offset`（默认 0）、`limit`（1–30，默认 10）；数值必须是非负／范围内安全整数。 | `ctx.tools.schemas(invocation.agent)`，只读当前 Agent 元数据 |
| `github` | 非空 `query`，最多 2048 字符；可选 `limit` 为 1–100，默认 5。仅第一页，不接受 offset。 | `sources.search` |
| `inspect` | 非空 `repository`，最多 256 字符；可选非空 `ref`，最多 1024 字符。仓库／ref 语法和公开来源规则由 `GitHubSources` 继续校验。 | `sources.inspect`，再作窄投影 |
| `tasks` | 可选 `offset`（默认 0）、`limit`（默认 30，1–30）、`snapshot`。后页必须带首读返回的快照。 | `client.list`，仅恢复索引；不查询／派发模型任务 |
| `status` | 必填 `task_ref`，恰为 32 位小写十六进制；可选非负 `after_event_id`、`after_message_id`、`artifact_offset`；不支持省略 task_ref 或 `latest`。 | `client.status`，成果分页在工作台投影处理 |
| `submit` | 非空 `key`（最多 160 字符）、`query`（最多 100000 字符）；可选文本 `context`（最多 200000 字符，允许空）和原生索引返回的 `connection_id`。 | `client.submit`，保留原 key 去重／恢复；连接比较不是身份授权 |
| `flow_status`／`flow_start`／`flow_pause`（0.5.0） | 无附加字段；身份仅来自原生会话。 | 原生计划查询、安排／核对与暂停，不调用外部模型或核心。 |
| `flow_run`（0.5.0） | 非空 `key`（最多 128 字符）；重试同一操作保留原 key。 | 通过原生调度安排明确的新一轮，不是重建任务执行器。 |

字符上限按 JavaScript 字符串长度校验；不接受含孤立代理项的字符串。`submit` 不接受 `previous_task_ref`、`tool_names`、候选成员或安装配置；这些不因存在于其他工具契约就自动开放给工作台命令。

五页不是五条新命令，也不是所有按钮都经此适配器：

| 页面 | 实际通道 |
|---|---|
| 我的军团 / My Legion | `runtime` 的 members 观察；原生配置保存 `candidateHarnesses`／`teamProfile`，随后 `flow_start` 安排工作。`flow_status/pause/run` 查询、暂停和再跑；配置、投递和模型执行分开。 |
| 找能力 / Discover | `tools`／`github`；Skill 列表走原生 `skills`，市场走原生 `pluginMarket` 的已配置索引。Skill 是指导，不是新增工具。 |
| 添加能力 / Add capabilities | `inspect` 准备来源事实，明确委托才 `submit`；检查、安装和开关走官方 `pluginManager`，不是新增 panel 动作。 |
| 战队任务 / Team tasks | `tasks` 读本地索引，`status` 主动读实时观察；不自动轮询或执行来信。 |
| 连接与配置 / Connection | 原生配置表单读写地址与凭据引用，不经命令 JSON 传凭据；成员候选在我的军团页保存。 |

`tools`、`github`、`inspect` 不要求 Legion 核心或 token；Skill、市场和安装使用主机实际提供的服务，缺少服务不绕过。`runtime`／`status`／`submit` 使用已配置核心与身份；`tasks` 虽不发 HTTP，也要通过当前凭据、身份与生命周期检查。切换工作会话改变工具和任务上下文，不把同 profile 的配置／能力包开关变成会话私有。下载、反馈、续作、恢复和远端取消仍用原对话的 `/legion` 或相应模型工具，工作台不另增这些动作。操作说明见[五页工作台](ECOSYSTEM.md#native-workbench)。

### CommandResult and value shapes / 回执形状

官方 `CommandResult` 外层只有这里使用的 `kind` 和 `text`：成功为 `kind: "success"`，`text` 是 `{ ok: true, action, value }` 的 JSON 字符串；错误为 `kind: "error"`，`text` 是 `{ ok: false, action, error }`，可另有 `diagnostics`。解析／未知动作错误的 `action` 为 `null`；已识别动作的参数或执行错误保留动作名。不回显未知字段名、JSON 原文片段或任意异常附件。

下表为投影前的主要字段，不是承诺任意大小的字段都会完整保留；成功的 `value` 另有 `projection`、`truncated`：

| 动作 | `value` 的主要形状 |
|---|---|
| `runtime` | `{ view, source, observed_at, runtime, verification }`；`verification` 为 `service_observation_only_no_execution`。 |
| `tools` | `{ tools: [{ name, description }], total, offset, limit, next_offset, scope, source, metadata_only, execution_performed, grants_remote_access }`；不是顶层工具数组，也不返回参数 schema。 |
| `github` | `{ trust, status, query, repositories, total_count, incomplete_results, source, fetched_at }`；保留来源和不完整事实，不推断安装或可用。 |
| `inspect` | `{ repository, commit, url, metadata, license, readme, readme_status, manifests, tree, errors, continuation, ... }` 的有限事实，保留来源与实际状态；细节见下文。 |
| `tasks` | `{ tasks: [{ task_ref, key, team_id, created_at, previous_task_ref? }], source: "local_recovery_index_not_live_status" }`；不是服务实时任务全表。 |
| `status` | `{ task_ref, team_id, connection, runtime, events, next_event_id, more_events, artifacts, mailbox, artifact_source, experience_verification }`。本入口默认从事件／消息游标 0 读取；后续分页用 `legion_status` 的游标参数。 |
| `submit` | `{ task_ref, team_id, session_id, submission, next, ... }`；依回执可有 `reason`、`http_status`、`reused`、`request_sent`、`remote_task_cancelled`。 |
| `flow_*`（0.5.0） | `{ configured, enabled, code, partial, cancelled, catalogVerified, workflow, schedules, observations, results, execution: "not_verified" }`。`workflow` 独立报告首次／周期是否观察到及当前配置是否匹配；每条计划可带 `lastDelivery`，不返回原指令或凭据。 |

`ok: true` 只表示命令交回了观察。`submission: "accepted"` 是接单，`"unconfirmed"` 仍是不确定，二者都不是任务完成。重读到已保存的 `"rejected"` 时返回错误，并在 `diagnostics` 保留有限回执和原引用；首次异常不保证附带引用，可用原会话 `tasks` 找记录。网络错误、取消或输出过大都不能推断“没有派单”，不要换 key 盲目重发。仅客户端已有“未进入 HTTP 发送”的持久化证据时，允许修复后用原 key 重试；工作台不自行判断未发送。

来源异常通过 `sourceErrorDiagnostics` 保留类型化的安全原因和限流等白名单事实，仍为错误通道；不透传响应正文、原始 `cause`、堆栈或任意 diagnostics。可用的 `partial` 来源观察会保留 `status`、`errors`，不伪装完整成功。

<a id="workbench-projection"></a>

### Bounded workbench output / 工作台有限投影

- `client.clean`／`redactText` 先清理，再按 UTF-8 边界截短。整个 `text` JSON 上限 **64 KiB**；普通数据投影预算 40 KiB，单段文本 4096 字节，每个数组至多 100 项、对象至多 100 字段、深度上限 8。最终仍超限时返回 `kind: "error"`，带 `diagnostics.truncated: true`，不截破 JSON 或换成看似完整的空成功。
- **0.5.1 成员专用投影**：工作台从完整脱敏成员响应中按调用开始时的候选快照提取 `selected`，
  不先截目录再找人；成员页和候选摘要各保留 16 KiB 独立预算。`runtime.harnesses` 仍是原嵌套行形状的
  精简数据，优先保留 `model.configured/configuration_error/provider_verified`、
  `readiness.verified/historical_verified` 和 `entry_points.execution`；预热、缓存与公开目录标记单独保留。
  `selected` 包含 `mode/items/total/shown_count/complete`，未观察到的 key 返回 `observed:false`，
  不填假的未安装／失败状态。`member_page` 包含 `offset/limit/total/shown_count/next_offset/complete`；
  字节预算不足时整行缩页，`next_offset` 只越过实际返回的记录，身份 key 不截短。
  `complete` 表示投影完整性，不表示可执行；是否有后页看 `next_offset`。元数据与省略路径仍受总输出上限约束。
  仅工作台命令使用该投影，模型工具 `legion_runtime` 与原 `/legion runtime` 的既有契约不变。
- **0.5.1 任务列表**：`tasks` 返回最多 30 条、16 KiB 整行预算的当前页，以及
  `task_page.{available,total,shown_count,offset,limit,next_offset,snapshot,source_complete,complete}`。
  快照来自完整脱敏索引与连接身份；后页快照不匹配时明确要求刷新第一页，不把不同版本的列表拼起来。
  非法引用保留为不可用记录，缺失索引、读取失败、截断与已确认的空列表分别显示。
  这是本会话的恢复索引，不是实时成员活动流。
- **提交回执归属**：浏览器按原生连接标识与会话保存未确认操作；切换连接仅隐藏旧操作，不删除原 key。
  `connection_id` 是既有服务／认证凭据／索引绑定的不可读比较值，不是令牌、授权或远端稳定用户 ID。
  工作台在真正提交前核对当前绑定；该字段不会转交给核心当授权。
  新客户端需要同版 Host 配套；旧模型工具与不带 `connection_id` 的既有命令契约不变。
- `projection` 保留 `omitted_fields`、`truncated_fields` 及去重后的 `omitted_fields_count`、`truncated_fields_count`，附 `limits`。每类至多展示 64 条路径、每条至多 160 字节，路径元数据总预算 16 KiB；有省略时标 `paths_truncated`。路径本身也脱敏。
- `tools` 只保留 `name`、最多 1024 字节的 `description`；名称超过 1024 字节直接报错，不造一个截短的可调用名称。按名字／描述匹配查询词并分页；`total` 是匹配总数，`next_offset` 按实际返回行数前进。工具页数据预算为 38 KiB，未显示的字段／行明确记录；只读元数据，`execution_performed: false`、`grants_remote_access: false`。
- `inspect` 保留固定 repository／commit／URL、真实 metadata／license 字段、README 事实及最多 **1200 字节**正文、manifest 路径与文件事实、目录摘要、errors 和来源。不返回完整目录 `entries`，不复制 manifest 或许可证正文；README 标 `content_preview: true`。`source_truncated` 保留来源原始截断值，`source_preview` 内的 offset／字节数属于来源读取页，**不是清理后的 UI 正文字节数**。
- 每个文件的 `next` 指向 `legion_source` 的 `read`，从 `offset: 0` 开始，避免跳过 UI 未展示的正文；顶层 `continuation` 保留固定 commit，并要求补 `path`。它们是后续入口，不会自动执行。模型工具的大 inspect／按字节续页机制仍见[公开源读取](#4-public-source-reading)。

任意字段被省略或截短，成功 `value.truncated` 也会为真；这可能只是参数 schema 被主动省略，不等于来源失败。反过来，缺失字段、短 README、清单数量或任务 `done` 都不能当成“不存在”“已安装”“已可用”或质量判断。

### JSON examples / 格式示例

下列只说明语法，不是已经执行的公开验收。仓库名与全零任务引用是占位符，需换成公开来源和当前会话的真实引用；**实际执行 `submit` 会派单，可能产生模型费用**。

```text
/legion-panel {"action":"runtime","args":{"view":"members"}}
/legion-panel {"action":"tools","args":{"query":"read","offset":0,"limit":10}}
/legion-panel {"action":"github","args":{"query":"markdown parser","limit":5}}
/legion-panel {"action":"inspect","args":{"repository":"owner/repository","ref":"main"}}
/legion-panel {"action":"tasks"}
/legion-panel {"action":"status","args":{"task_ref":"00000000000000000000000000000000"}}
/legion-panel {"action":"submit","args":{"key":"example-integration-1","query":"将选定的公开项目接入 DSH，并报告实际可用范围。","context":"这是文档格式示例，不包含私有材料。"}}
```

假设 tools 查询没有匹配项：外层 `kind` 为 `"success"`，下面是 **`JSON.parse(result.text)`**，不是 CommandResult 根对象：

```json
{
  "ok": true,
  "action": "tools",
  "value": {
    "scope": "current_dsh_agent",
    "source": "dsh.tools.schemas",
    "metadata_only": true,
    "execution_performed": false,
    "grants_remote_access": false,
    "tools": [],
    "total": 0,
    "offset": 0,
    "limit": 10,
    "next_offset": null,
    "truncated": false,
    "projection": {
      "omitted_fields": [],
      "truncated_fields": [],
      "limits": {
        "output_bytes": 65536,
        "text_bytes": 4096,
        "array_items": 100,
        "object_fields": 100,
        "depth": 8,
        "tool_description_bytes": 1024,
        "readme_bytes": 1200
      },
      "omitted_fields_count": 0,
      "truncated_fields_count": 0
    }
  }
}
```

例如输入 `{"action":"install"}` 不会转给安装器，外层返回 `"error"`；解开 `text` 后为：

```json
{
  "ok": false,
  "action": null,
  "error": "未知工作台动作；仅支持 runtime、tools、github、inspect、tasks、status、submit。"
}
```

### Service runtime observations

`legion_runtime`／`/legion runtime` 只读现有 `/api/legion/harnesses`、`/api/legion/capabilities` 或 `/api/legion/catalog`。返回 `view`、`source`、`observed_at`、脱敏且有界的 `runtime`、`projection`，以及 `verification: "service_observation_only_no_execution"`。

保留服务提供的 `warming`、`stale`、`errors`、`provider_verified` 等事实，不把注册、可安装、已配置改成已验证。`observed_at` 是本次读取时间，不刷新服务内旧记录的时间。查询失败走失败通道，不生成“成功但空”的清单。此工具不安装、不主动强制探活、不发模型任务；服务自身端点的正常缓存行为仍由服务实现。

## 3. Capability discovery

`source: all | installed | market | github`，默认 `all`；`query` 至多 500 字符。`limit` 为 1–30，默认 10；`offset` 为非负整数，默认 0。返回 `fetched_at`、`untrusted_source: true`，不安装或执行结果。

| 来源 | 返回边界 |
|---|---|
| `installed` | 当前 Agent 可见 schema；`scope: "current_dsh_agent"`、`entries`、`total`、`next_offset`、`grants_remote_access: false`。不是全机器安装清单。 |
| `market` | 原生 `plugin_market` 的 `list_indexes` → `list_plugins`；`index_url` 只能选择已配置索引。一次至多 5 个，余项见 `remaining_indexes`。 |
| `github` | 公开仓库搜索，至多 10 条、仅第一页，`offset_supported: false`；没有搜索词时为 `query_required`。 |

市场区分 `unavailable`、`not_configured`、`failed`、`partial`、`available`。GitHub 失败为 `github.state: "failed"`，带 `error`；类型化来源错误另有 `diagnostics`。一个来源失败不抹掉其他来源结果；失败不是空结果，取消等待也不证明官方市场的共享抓取已停止。

## 4. Public source reading

`repository` 只接受公开 `owner/repo` 或规范 `https://github.com/owner/repo`，不是任意 URL 抓取器。

| 动作 | 行为 |
|---|---|
| `inspect` | 可选 `ref`；省略用默认分支，将其解析到固定 `commit`，返回元数据、目录、README、清单和许可证观察。 |
| `read` | 必须用完整 40 位 `commit` 与仓库相对 `path`；读取非空普通 UTF-8 文本。默认 `offset: 0`、`limit: 12000`，limit 为 1–65536 **字节**。 |

- `inspect` 至多 2,000 个目录项、1 个 README、8 个清单及实际许可证文件预览，各至多 12,000 字节。单个文本文件上限 1 MiB，或更小的配置响应上限；分页不绕过整文件限制。
- `read` 核对固定 commit／tree／blob 和整文件字节，返回 `trust`, `status`, `repository`, `commit`, `source_sha`, `path`, `url`, `fetched_at`, `source`, `content`, `encoding`, `sha256`, `blob_sha`, `offset`, `limit`, `end_offset`, `returned_bytes`, `total_bytes`, `offset_unit`, `truncated`, `next_offset`, `next`。**SHA-256 对应整文件，不是当前页**；续页用返回的 `next_offset`，不要用字符位置计算。
- 保留 `partial`、`errors`、截断与续页信息。目录有限或许可证 `not_detected` 不等于不存在；`assessment: "not_performed"` 和 `NOASSERTION` 都不是分发权判断。
- 请求使用固定 GitHub API，不跟随重定向，不把私有仓库、符号链接、子模块或二进制当作可读源文件；不执行读到的代码。

### Large output and failures

模型工具 `legion_source` 的大 `inspect` JSON 仍交给 Host 的原生输出／转存机制；显示片段可能不是完整 JSON。依据本次工具结果中的转存位置读取完整输出，或对所需源码使用 `read` 续页。连接器没有用 compact `additionalContexts` 重建全文，也不把 repo 正文升级为 instruction。**`/legion-panel` 不返回这份完整 inspect**，而是上述[工作台窄投影](#workbench-projection)，保留后续 `legion_source` 入口。

类型化来源错误只保留本地定义的 `name`, `message`, `code`, `trust`，以及可用的 `status`, `rate_limit`, `retry_after_seconds`, `retryable`。`rate_limit` 含 `limit`, `remaining`, `used`, `reset_epoch_seconds`, `resource`；未知／不合法值保留 `null`，不编造重试时刻。

`legion_source` 和 `legion_bundle` 的来源获取异常仍走**失败通道**：非取消错误通过原生 `HarnessError` 文本附 `Source diagnostics: <JSON>`；不假定 Host 提供独立结构化字段。取消及非来源异常保留原身份；不透传响应正文、重定向地址或任意原始异常。`discover` 可用相同事实表达单个来源失败，不自动绕过限流。

## 5. Package authored adapters

`legion_bundle` 必须提供普通工作区的绝对 `directory`（**不加 `file:`**）、`spec`、1–4 个 `source_refs`。每个来源为 `{ repository, ref?, paths? }`，最多 4 个额外文件路径。来源事实来自实际读取或合格的固定观察复用，不能由调用方编造 hash／许可。结构示例见[生态用法](ECOSYSTEM.md#作者适配与包装)。

| `spec` 字段 | 契约 |
|---|---|
| `package_name`, `version`, `description` | 必填；版本为完整固定 semver。 |
| `plugins` | 非空数组，每项 `{ id, title, description, entry }`；entry 必须是 files 中实际提供的非空相对 `.js`／`.mjs` 文件。 |
| `files` | 非空数组，每项 `{ path, content }`；内容是作者实际编写的 ESM 适配及所需文本。 |
| `dependencies` | 可选 `{ name, version }[]`，版本只接受完整 semver，不接受范围、URL、Git spec 或别名。 |
| `license` | 可选、仅为有权声明的代码给出的声明；`files` 中的 `LICENSE` 原文保留，没有原文不生成，没有字段不添加许可声明。 |

拒绝越界／绝对／反斜线／NUL 路径、大小写或 Unicode 别名重复、文件目录冲突、缺入口、空内容、保留元数据／控制文件替换与未知 spec 字段。这些是协议保护，不是代码审查。最多 **256 文件、64 插件、128 直接依赖**；单个输出文件 1 MiB，含元数据总输出 8 MiB，provenance 256 KiB，路径 512 UTF-8 字节。

### Pinned facts on retry

复用限**同原生会话、同连接器实例／生命周期**，key 包含仓库输入、完整 40 位 commit 和有序路径列表。分支、标签、短 SHA 或缺省 ref 重新读取；`partial`、任何错误及 `license.error` 不复用。成功但截断的观察可复用，仍明确截断。

内存至多 **16 组／1 MiB**，卸载或生命周期 abort 清除；单次调用取消不抹掉之前成功观察。只保留来源事实，不保留适配代码、目录、原生执行结果或权限。保留原 `fetched_at`／hash，工具 `source_retrieval` 及各 provenance 项 `retrieval` 标记 `fetched` 或 `reused_pinned_facts`；不冒充新抓取或许可评估。每次仍核当前 spec／目录并逐文件走当前原生写权限。它不是 `legion_source` 的全局缓存。

### Outputs and installation

生成 ESM、`private: true` 的包；根 export 指向首入口，每项另有 `./<id>`，patch 各行的 `id`／`name` 为 `<package>/<id>`。生成清单、patch、locale、原创图标、README、`provenance.json`、`SOURCE-NOTICE.md`；locale 保留输入文字，不声称自动翻译。

没有 `prepare`／`postinstall` 等 scripts；不会扫描 imports、生成 `peerDependencies`／`engines`、补 inject 或安装外部环境。依赖自身仍可能有脚本。一个 bundle 是一个安装单位，不是每个能力一个 npm 包。

内部同步 `buildBundle(spec, provenance = [])` 是纯函数，不读写文件、不联网、不执行输入代码；它保留传入 provenance，返回 `files: [{ path, content, sha256 }]`、`packageName`、`version`、`plugins`、`totalBytes` 和未验证的 `verification`。有来源读取和原生写入的是模型工具，不是此纯函数。

模型工具成功返回：

```text
{
  state: "written_not_installed", directory, package_name, version,
  plugins: [{ id, rowId, name, title, description, entry }],
  files: [{ path, sha256, bytes }],
  hash_kind: "expected_written_content_not_independent_readback",
  source_retrieval: "fetched" | "reused_pinned_facts",
  install: {
    tool: "plugin_manager",
    arguments: { action: "install_bundle", target: "file:" + directory, enabled: false }
  },
  execution_verified: false
}
```

原生 `write` 可覆盖同名文件；失败报告已写部分，不自动回滚。hash 是预期值，仍需独立读回。用返回的安装参数走官方管理器，保留 `application`／`changed`／警告／重启要求。`set_bundle` 管整包，`list_plugins` 读真实 `entryId` 后用 `set_plugin` 管单项；`rowId` 不能代替运行 ID，加载失败也不证明选择已回滚。细节见[原生安装](ECOSYSTEM.md#native-installation)。

开发入口是 Host `skill` 的 `{ "name": "cordis-plugin-development" }` 及其返回基目录下的 `references/host-plugin.md`、`references/practices.md`。`export const inject = ['tools']` 声明服务依赖，**不授予调用权限**。import、激活、调用、卸载各需自己的事实。

## 6. Two-step collaboration

`legion_submit.tool_names` 只分享最多 **30 个明确选定、当前 Agent 可见**的 schema；省略／`[]` 不分享，不给远端 token 或本机执行权。

### Execute first

`legion_call` 需要原会话 `task_ref`、可用个人令牌、明确收件任务 ID、稳定 `operation_key`、非 `legion_*` 的工具名和 JSON 对象参数。新操作检查当前可见性，经原生运行时执行；先预约，防止同一操作重复发起。桥接器不发 HTTP／消息，所选工具本身仍可能联网。

首次成功子调用只返回 pending 观察及其**经过原生策略的最终 `content` 投影**，不用隐藏的 `value`／`error`／`additionalContexts` 补正文。父层仍可能改写或拒绝。首次子调用失败、未知或取消仍报工具错误；元数据写入失败另外标明，不转换成成功或自动投递。

### Observe the enclosing call, then decide to send

官方 `tools/result` 从当前 token 同步跟随 parent 到根；根终态后只写一次元数据，不保存子／父正文、不回改首回执、不发网络。终态按官方结果而非事后单看 signal 是否 aborted；缺根证据不能确认成功。观察器写盘失败不能改 Host 结果，恢复可能仍 pending／unknown。

| 回执字段 | 含义 |
|---|---|
| `version`, `task_ref`, `operation_id`, `recipient`, `tool_name` | v2 与操作身份；不是新的访问凭据。 |
| `native.state` | 初始 `unknown`；子调用及全部祖先成功才可确认 `succeeded`；明确子失败保留 `failed`。 |
| `native.observed.state` | 子调用观察：`succeeded`／`failed`／`unknown`；不等于祖先提交。 |
| `native.result`, `projection` | 只在首次返回的有限正文投影及省略／截断事实；不存入 v2 记录、不随恢复回放。 |
| `transport.state`, `transport.outcomes` | `pending`／`succeeded`／`failed`／`unknown`；逐层 `{ call_id, name, state }`。这里是 **DSH 父调用链，不是 HTTP 投递**。 |
| 新 v2 的 `delivery` | `{ acknowledgement: "not_attempted", automatic: false, method: "legion_message" }`；后续独立消息不会更新此字段。 |
| `persistence`, `persistence_error` | 初次元数据保存失败时单列 `failed` 和脱敏原因，不代表工具未执行。 |
| `consumed_by_model_confirmed` | 保持 `false`，不把任何回执当模型消费。 |

**等 Host 展示外层调用的最终结果后**，DSH 下一次独立决定是否调用 `legion_message`。用消息自己的稳定 key，只发送实际可见且允许分享的内容；失败、未知亦如实说明。不要在同一个仍运行的 `run_code` 或其他父调用内执行并发送成功消息。这是使用协议，不是拦截任意程序的控制器。

### Recovery and media

同任务、同 key、同 recipient／tool／arguments 只读元数据；改输入会拒绝。已预约记录先于目标可见性检查，因此目标能力停用／卸载后仍可恢复，前提是连接器、原会话／状态及授权可用。只有预约或终态不完整就保持未知，不重做副作用；本地去重不等于跨机器 exactly-once。取消／卸载后不继续发请求，恢复异步读取后仍检查生命周期。

v1 没有祖先证据，旧成功不能确认原生成功，旧正文不回放；明确旧失败可保留。历史 `native-result:<operation_id>` ACK 只读为 `received`／`rejected`／`unconfirmed`，并列 `historical: true`、`attempted_now: false`。旧原生 `failed` 与历史 `received` 可以同时成立。缺失／不可读／畸形 ACK 保持 `unconfirmed`，不抹成“未发送”，不重发、不改盘。

正文投影预算 **12,288 字节**，另有限长／深度／形状限制；明确媒体、data URL、敏感及标记编码字段省略。普通长文本或 hash 不按外形猜成 base64，仍按预算截断。`media_return` 明示仅文本／元数据；本地图片转交不是远端完整媒体或视觉验证。脱敏不是万能秘密检测。

## 7. Configuration

这些是用户／运营者配置，不是模型参数；未知配置字段（含内联 token）会拒绝。JSON Environment 编辑器用带引号的 JSON 字符串值，普通环境变量用普通字符串。

| 字段 | 默认与边界 |
|---|---|
| `baseUrl` | `LEGION_BASE_URL` → 回环 8000 端口；不启动服务。远端 HTTPS，HTTP 仅回环；拒绝 URL 凭据／query／fragment。 |
| `tokenEnv` | 默认 `LEGION_PERSONAL_TOKEN`，仅为 Host 环境变量**名称**。 |
| `tokenFile` | 可选本机绝对路径；设置后优先于 `tokenEnv`，失败不回退。每次凭据读取重读，最多 4096 字节；本人所有、无 group/world 权限、单硬链接的普通文件。检测到已绑定的实际身份变化后停止旧连接器，须重载。 |
| `stateDirectory` | `DSH_LEGION_STATE_DIR` → `DSH_HOME` 或用户 `.dsh` 下的 `legion-state`；相对路径基于 Host 工作目录。选私有目录并保留恢复记录。 |
| `candidateHarnesses` | 用户选定有序数组；非空传 `plan.team_policy.candidate_harnesses`，省略／`[]` 不传 plan，`null` 拒绝。无连接器默认成员或兜底。 |
| `projectBindingFile` | 绝对路径 JSON，至多 16 KiB，仅含 `workspace_root`：运营者已授权的绝对目录。不上传工作区、不授予新权限。 |
| `requestTimeoutMs` | 默认 30,000，范围 100–300,000；只限制本次 HTTP 等待，不限制远端任务运行。 |
| `maxResponseBytes` | 默认 4 MiB，范围 1 KiB–32 MiB；JSON 响应／接单 SSE 事件上限。 |
| `maxArtifactBytes` | 默认 50 MiB，范围 1 字节–1 GiB；单次下载上限。 |
| `maxEvents` | 默认 100，范围 1–500；状态事件和来信分页上限。 |

API 保留配置的服务路径前缀；实际反代仍需部署者验证。project binding 成为 `project_context`，不让模型另选授权目录。

令牌文件拒绝末级文件及其直接父目录的符号链接、目录或其它非普通文件、空值、多行／非法 HTTP 头字符以及读取期间变化；这不是对任意祖先目录或整台主机的安全认证。实际使用过的新旧值仅在进程内加入脱敏，不保存到恢复索引。提交前读取配置，缺失凭据不占用新的派单键。

<a id="identity-reload"></a>

### Credential rotation, reload and identity / 轮换、重载与身份

1. 凭据文件不是启动时读一次，也没有后台自动换身份的保证。每次使用重新读取，受身份约束的操作按**规范化服务地址＋实际 token 值**校验身份；不是按 token 文件名或环境变量名识别用户。环境变量中的实际 token 改变也适用。
2. 连接器一旦绑定身份，再检测到身份不同，会 `dispose` 旧客户端并明确要求**重载插件**；不把旧任务带到新身份继续请求。重载后新实例才可使用新身份，是否被核心接受仍需服务验证。文件失效直接报错、不回退环境变量；修复为同一有效凭据不意味着换了身份。
3. 原生配置使用可重载的完整快照。保存提交后的 `loader/volatile-update` 先停止旧生命周期，再由原生 fiber 重启；重启失败时旧客户端也不会继续服务。工作台的 **重新读取配置 / Reload configuration** 只重读表单，不等于重载连接器。保存须通过可写原生配置和 revision 校验，不能借 `/legion-panel` 写 profile 或凭据。
4. 恢复索引按服务／实际 token 身份及 DSH 会话隔离。新地址或新 token 不继承、迁移或删除旧引用；只改凭据文件路径／变量名且实际身份不变，不会创建新身份。保留原 stateDirectory、同一身份和同一 DSH 会话才可恢复原记录；历史上仅按服务隔离的旧索引不自动并入。中断操作的回执仍落在原索引，不写进新身份。
5. **保存配置、重载、检测到轮换、卸载或取消本地等待，都不发送远端任务取消请求。** 已接单或结果不明的远端工作可能继续；只有另行明确使用 `legion_cancel`／`/legion cancel` 请求取消，再核对实际状态。超时和取消 ACK 也不证明远端进程已停。

独立核心 `meyo.py dsh-config --state-dir <实例>` 复用实例配置与登录身份检查，返回 `{plugin, config:{baseUrl,tokenFile}, verification:"configuration_only_no_model_request"}`。不打印令牌、不改环境／Host profile、不联网；返回配置不等于服务启动或模型可用。路径必须能由 DSH 所在的主机和 OS 用户读取。

## 8. Task lifecycle and mailbox

### Submit and observe

- `task_ref` 为配置服务、实际凭据身份与 DSH 会话范围内的本地 32 位十六进制引用；保存的 `team_id`／`session_id` 指向 Legion team／conversation，不是凭据。`legion_list`／panel `tasks` 读本地索引，不查实时进度；换 DSH 会话或身份不自动继承引用。
- 提交前原子预约引用和输入 digest；同 key 同输入通常只恢复回执、不再 POST，不同输入拒绝。唯一重试例外是客户端保存了明确未进入 HTTP 发送的证据，修复后原子领取原 key 的重试机会；缺失／不确定 ACK 不构成该证据。候选顺序计入 digest；丢记录或换 key 不让未知副作用变得可安全重投。
- POST `/api/legion/dispatch`，含 `question`、team／session 身份、`mode: "auto"`、`allow_direct_model: false` 及可选项目绑定／候选。只有相符 SSE `meta` 的 `team_id`／`sid` 确认接单，随后关闭本地读取；关闭不取消远端任务。
- `submission: "accepted"` 不是完成。HTTP 4xx（408 除外）为 `rejected`；5xx、408、超时、断线或缺失／不符 ACK 为 `unconfirmed`，不等于未执行。保留原 key／ref，用 list／status 恢复观察。
- status GET `/api/legion/team` 及 `/api/legion/events`，检查保存的 team 和 `conversation_id`；缺失／不符会失败。task ID 来自 task-map 的键，不信任正文内自报 ID。
- `after_event_id` 为非负安全整数，省略从 0 起；返回 `event_id`、`next_event_id`、`more_events`。status／events 非原子快照，空可见页不等于历史结束。输出有限且脱敏，不是完整 trace；运行、产物及服务自评是并列事实。

### Commander messages

`status.mailbox` 只投影同一已核身份 `/team` 响应的 `mailboxes.commander`，标记 `source: "team.mailboxes.commander"`、`trust: "untrusted_source"`；不调用默认会标读的 `/team/messages`。

`after_message_id` 为独立非负安全整数，默认 0。按 ID 升序返回大于游标的消息，每页至多 maxEvents；`more_messages` 为真时用 `next_message_id` 继续。`total_messages` 计整个返回 mailbox，不是当前页。分页是有大小限制的 team 快照的本地投影，不减少 HTTP 体积、不保存消费位置，也不是原子历史。

消息保留 `id`, `from`, `to`, `text`, `ts` 及存在时的 `read_at`；时间为服务秒数，缺失 read_at 不补造。`read_receipts_modified: false`、`model_consumption: "not_observed"` 表示不标读、不确认模型消费、不执行文字或恢复工作。

`available` 且 `messages: []` 才是观察到空页；未返回／畸形分别为 `not_returned`／`invalid`，`messages: null` 并附原因，未知数量／续页保留 null。保留 `truncated`、`text_truncated`、`artifact_refs_truncated`、`projection`；`text_source: "inline_preview"` 是长正文预览，不是已取全文，产物引用仅含有限非路径元数据。

### Follow-up actions and acknowledgements

| 动作 | 行为 |
|---|---|
| `continue`／带 `previous_task_ref` 的 submit | 新 key、同 Legion session、**新 team**；明确新工作，不是重试或重连。 |
| `message` | POST `/api/legion/team/message`，保存的 team、明确 recipient／text、固定 `sender: "commander"`。仅投递，不自动唤醒终态任务。 |
| `resume` | POST `/api/legion/team/resume`，请求中断工作的恢复；不是重跑完成任务，`no recoverable tasks` 不是恢复成功。 |
| `cancel` | POST `/api/legion/cancel`；明确 `ok: true` 为 `requested`，false 为 `rejected`，无 ok 为 `unconfirmed`；`stopped_confirmed: false`。 |

message／resume 发送前预约操作 key 和 digest；首次返回可含清理后的 `server_response`，持久化及同键恢复只保留 ACK 元数据，不再发送或回旧响应。恢复的合法 acknowledgement 为 `received`／`rejected`／`unconfirmed`；仅允许整数 HTTP 状态 100–599、已脱敏至多 500 字符 reason。缺失／不可读／坏形状为可见的 `unconfirmed`；坏可选字段省略并带 `record_warning`，不改旧记录。

JSON `ok:false` 表示拒绝；HTTP／网络未知按提交边界处理。cancel 没有操作 key 账本，其 HTTP／传输错误直接报错。ACK、`read_at`、`cancel_requests` 都不证明模型消费或进程停止；本地 abort／卸载也不会自动取消远端工作。

## 9. Formal exports and retention

正式导出来自已授权任务结果／事件的 `artifacts`、`discovered_artifacts`、`artifact_ready`。download 只接受已观察 `artifact_id`，重新检查实时任务身份／权限及元数据；不能由调用方选 URL 或目标路径。仅同源正式导出路由，保留服务路径前缀，拒绝凭据／query／fragment／重定向。

必须有合法 `sha256` 或 `provenance.sha256`；两者都存在则须各自合法且一致。声明大小也须合法；下载有限字节到私有临时文件，hash 与声明大小匹配后才重命名。`integrity: "sha256_verified"` 只说明字节与服务声明一致，`experience_verification: "not_performed_by_plugin"` 不作安全、正确性或体验保证。

来源 hash 是整文件观察，包装 hash 是预期写入，下载 hash 是实际收取字节比对，三者不能互当验收。恢复索引留引用、key／digest、最小回执、游标和导出元数据；v2 原生记录不留正文。旧 v1、两端会话、工程及下载文件仍可能有正文或媒体，卸载不删除。产物观察按 artifact_id 原子保存，兼容旧 `artifacts.json`，新 per-ID 记录优先；本地记录不是实时授权。详见[安全说明](SECURITY.md)。
