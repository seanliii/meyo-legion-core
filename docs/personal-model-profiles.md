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
  `model/base_url/api_key` 三个必需字段，可选保存 `input_modalities`，文件权限0600。
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
