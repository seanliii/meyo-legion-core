# 个人实例的命名模型配置

沿用已有 `meyo.py` 和原生 Responses 绑定，不新增网关或协议桥。
Kimi、DeepSeek、OpenRouter 都通过同一组参数保存；模型 ID、账户区域对应的
Responses 地址及授权由本人或认证主线确认，此功能不探测提供方、不验证可用性。

## 保存、列出、明确激活

先按已有方式初始化个人实例。实例必须在源码目录外、属于当前用户且权限为0700。
密钥只从本人预先准备的0600普通文件读取；不要把密钥写进命令参数、终端输出、
Prompt、日志或 Git。以下变量中只有目录、模型 ID 和地址，没有密钥值：

```sh
STATE="$HOME/.local/share/meyo-legion"
PYTHON="/绝对路径/个人版Python解释器"
MEYO="/绝对路径/个人版源码/meyo.py"

# 为三家分别执行本命令，ID 可取 kimi / deepseek / openrouter。
# 下列模型、地址和文件路径都是占位符，请使用认证主线确认的实际值。
"$PYTHON" -I "$MEYO" save-model-profile --state-dir "$STATE" \
  --id kimi --model "已确认的模型ID" \
  --base-url "https://已确认的原生Responses地址/v1" \
  --api-key-file "/绝对路径/本人独占的密钥文件"

"$PYTHON" -I "$MEYO" list-model-profiles --state-dir "$STATE"

# 只在确认无活任务的维护窗口切换；本命令不会自动重启。
"$PYTHON" -I "$MEYO" activate-model-profile --state-dir "$STATE" --id kimi
```

- `save-model-profile` **只保存候选，不切换**；即使尚无模型配置也不会自动激活。
  ID 为1–64位 ASCII 字母、数字、`_`、`.`、`-`，以字母或数字开头。
  已有 ID 拒绝覆盖；换密钥时另起名称（例如 `kimi-next`）。
- 候选库位于实例的 `secrets/model-profiles.json`，每项保留
  `model/base_url/api_key` 三个必需字段，可选保存 `input_modalities`、
  `context_window`、`model_auto_compact_token_limit`，文件权限0600。
  候选库没有 active 指针。
- **`secrets/model.json` 仍是唯一实际运行绑定。** 列表只输出 ID、model、
  base_url、current；current 根据该文件的完整绑定（含密钥）比较得出，
  不是已运行服务的实时状态。多项绑定相同时可同时显示 current。
- 激活前先保存旧配置：若已有完全相同的候选就复用，否则生成不冲突的
  `previous-…` 候选。返回的 `rollback_profile` 是回退 ID；首次激活没有旧绑定、
  或重复激活相同绑定时为 null。已有候选不会被覆盖。
- 回退仍使用同一个命令：
  `"$PYTHON" -I "$MEYO" activate-model-profile --state-dir "$STATE" --id "返回的回退ID"`。
  回退也需要在维护窗口按原方式重启；不修改或删除会话历史。
- 候选库和绑定都沿用私有文件检查、实例锁和原子写。先落盘回退配置，再替换绑定；
  若后一步失败或进程中断，可能多留一份回退候选，旧绑定不会因此丢失。
  候选库沿用32 KiB读取上限，超限拒绝写入，不截断或丢弃旧候选。
- `configure-model` 和旧三字段 `model.json` **原样兼容**。
  旧命令仍是直接替换，不提供新增的自动保存回退；需要保留回退时使用命名候选激活。
  旧命令改动绑定后，列表会重新比较 current，不依赖陈旧标记。

保存或激活不等于提供方可用，不调用模型、不探测云端、不热切换，
也不改变公司配置或当前开发任务使用的模型。配置变更后，须确认无活任务，
按个人实例原有方式正常停止并重新启动服务（原 `serve` 入口）；不要并行启动第二个实例。
旧原生断点的提供方绑定限制仍保留，不保证历史能跨提供方继续。

## 给其他成员各绑一个候选

`secrets/model.json` 只属于 Codex。其他成员（例如 `cc`、`pi`）要进入派单池，
需要各自绑定一个已保存的候选，并声明该地址实际提供的协议；不猜测、不转换协议。

```sh
# 模型接口协议三选一：responses / anthropic-messages / openai-completions
# （Cursor 密钥不是模型接口，用 cursor-agent，见下文“用 Cursor 的模型当成员”）
"$PYTHON" -I "$MEYO" bind-member-model --state-dir "$STATE" \
  --member cc --id "已保存的候选ID" --protocol anthropic-messages

"$PYTHON" -I "$MEYO" list-member-models --state-dir "$STATE"
"$PYTHON" -I "$MEYO" unbind-member-model --state-dir "$STATE" --member cc
```

- 绑定保存在实例的 `secrets/member-models.json`，只记成员、候选 ID 和协议，
  不含密钥；列表只输出成员、候选 ID、协议、model、base_url。
- 每个成员只拿到自己的那份授权：不与 Codex 或其他成员共用，失败后也不换用别家的密钥。
- 部署实例里，已安装但没有绑定的成员不进派单池；绑定后在无活任务的维护窗口
  按原方式重启即生效。成员 ID 为小写字母、数字、`_`、`.`、`-`；`codex` 不在此绑定。
- Claude Code 绑定 `anthropic-messages` 后由原生命令行直连该地址；
  同一实例若另行配置了本机路由（`LEGION_CCR_BASE_URL`），以本机路由为准。
- 绑定不等于提供方可用：不调用模型、不探测云端。启动时若绑定引用的候选不存在，
  服务拒绝启动并说明原因。

### 先把成员装进本实例

成员目录里的外部成员（例如 `pi`、`qwen-code`）用同一个入口安装到本实例自己的
`repos/` 与 `runtimes/`，不调用模型，也不替它绑定模型：

```sh
"$PYTHON" -I "$MEYO" install-member --state-dir "$STATE" --member pi
```

安装需要该成员声明的运行环境（多数为 Node 20 以上）并会联网下载；失败时返回码非零并给出原因。
装好后仍需 `bind-member-model` 并重启，才进入派单池。

### 用 Cursor 的模型当成员

Cursor 的 API 密钥不是模型接口，不能给 Codex、Claude Code、pi 这些成员当模型地址用。
它驱动的是 Cursor 自己的代理（官方 `@cursor/sdk`，本地运行时：代理循环和文件读写在本机任务目录里，
模型推理走 Cursor 托管的模型）。目录里为此提供三个成员位：`cursor-claude`、`cursor-gpt`、`cursor-gemini`，
各绑一个不同的模型即可参与并集。

```sh
# 地址固定为 https://api.cursor.com（只作“这是 Cursor 密钥”的声明），model 填 Cursor 的模型 ID。
"$PYTHON" -I "$MEYO" save-model-profile --state-dir "$STATE" --id cursor-claude \
  --model "Cursor 模型 ID" --base-url https://api.cursor.com --api-key-file "私有密钥文件"
"$PYTHON" -I "$MEYO" install-member --state-dir "$STATE" --member cursor-claude
"$PYTHON" -I "$MEYO" bind-member-model --state-dir "$STATE" \
  --member cursor-claude --id cursor-claude --protocol cursor-agent
```

- `cursor-agent` 只能绑定地址为 `https://api.cursor.com` 的候选，该地址的候选也只能用 `cursor-agent`。
- 需要 Node 22.13 以上。可用的模型 ID 因账号而异；费用按 Cursor 的计费规则记在密钥所属账号上。
- 密钥只经环境变量交给成员进程，进程取出后即从自身环境删除：不进命令行、不写入任务目录、不出现在输出里。
- 同一会话再次派给同一成员时接着上次的 Cursor 对话做；对话状态存在任务目录的 `.sessions/` 下，不随成品导出。
- 无人值守时代理直接执行命令和改文件，不逐次询问。主机装有 `bubblewrap` 时可设
  `LEGION_CURSOR_SANDBOX=1` 启用 SDK 沙箱（写入限于任务目录，默认禁止联网）。

### 绑了不同模型的成员会一起做同一个任务

自动模式下，需要实做的任务不再只交给一位主创：实例里至少有两家可跑成员、
且它们绑定的模型互不相同时，默认由两家各用自己的模型独立完成同一目标；
还有第三家可跑成员时，由它在收口时匿名比选。同一个模型绑给两家不算异构，仍按单主创执行。

- `LEGION_AUTO_UNION_AUTHORS` 控制同时创作的家数：默认 2，上限 3，设为 1 即恢复单主创。
- 请求里点名成员、限定 `max_agents` 为 1 或声明 `solo` 时，以请求为准。
- 多一家就多一份模型费用与并发占用；小内存主机建议保持默认的 2。

## 已确认的看图能力

支持 Responses 协议不代表模型能看图。操作员在核实具体模型与提供方后，
可以在 `configure-model` 或 `save-model-profile` 中增加：

```sh
--input-modality text --input-modality image
```

声明随同一份模型配置保存、激活、传入原生执行和回滚，不另建能力开关或活动指针。
省略时仍是未知，Codex 原生目录保持仅文本；不得按模型名字、协议名称或另一家的能力推断。
目前该原生入口只声明 `text`／`image`，且必须含 `text`。无效声明拒绝保存。

目录只消费当前调用快照中与实际模型、地址和授权一致的声明。
它不会读取另一个并发请求的声明，也不会把公司目录的能力借给个人模型。
配置与页面状态中的 `verified` 仍为 false；保存声明不是一次模型实测。
供应方切换后的设备验证应使用明确的新会话，不能悄悄把旧供应方的原生历史发送给另一家。

## 已确认的上下文容量

同一个保存入口支持 `--context-window` 与 `--model-auto-compact-token-limit`。
前者声明该提供方、该模型实际支持的 token 容量；后者是操作员明确选择的原生整理阈值。
两项只接受 1 到 9223372036854775807 的整数，省略时保持原来未知／未覆盖的行为，
不按模型名字猜值，也不在保存时发起模型或网络请求。

容量仅从与本次模型、地址和授权一致的绑定写进 Codex 原生目录，正常启动与原断点续作
共用此入口。阈值沿原生 `model_auto_compact_token_limit` 配置传递；不另造摘要器，
不改变原会话身份或擅自切换到传输桥。声明容量不证明当前请求一定能成功，整理本身也可能
产生模型请求；仍须查看原执行器的实际回执。命名候选、激活、原子保存和回退均保留这两项。
