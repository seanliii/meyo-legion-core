# Cline：已安装原生 CLI 的接线

固定输入为 npm `cline@2.5.0`、官方 `v2.5.0-cli` 对应提交
`452733c3fddb6fffa9b06fbd4c96a0f5e88400d8`。不是桌面版本号，也不是另建答题 agent。

- 入口仍为现有 external registry / CLI transport。`cline_cli.py` 只准备当前请求的
  OpenAI-compatible provider 配置，调用原生 CLI，回收原生事实。
- 配置位于当前工程私有 `.sessions/cline/<owner+logical-slot>/`，不写共享运行层。
  原生 API 凭证仅写私有 `data/secrets.json`（0600），不进 argv、公共日志或源码。
  只合并当前 provider/model/baseURL，不覆盖 Cline 自己维护的 history、MCP 和用户设置。
- 新任务只接受 Cline 真正返回的 `task_started.taskId`；续作传同一 `--taskId`、
  配置目录与工程，并保留原生 `api_conversation_history` / `context_history` / UI 状态。
  同家并行逻辑槽分开；已验证 checkpoint 可以跨同工程的步骤改名恢复。
  收到 taskId 后立即发显式 checkpoint 事件，外层超时也保留它；这不提前宣称原生
  history 已成功落盘。正常返回时再核原生任务索引与实际文件。
- Cline JSONL 会重放旧消息，且最终可能附带空 completion ask。平台仅提取本轮非 partial
  completion 正文，不把旧答案、普通输出、原始请求、reasoning 或退出 0 当完成。
  原生失败后已收到的 ID 仍保留；缺 history 时如实报不可恢复，不猜最新目录。
- 当前请求的模型绑定和原生 metadata 分开记录。usage 取本轮持久 UI 请求记录；
  原生未提供的字段保持未知，不用任务累计数冒充本轮消费。
- 原生自动升级用官方 `CLINE_NO_AUTO_UPDATE=1` 关闭。目录更新检查只跟随固定
  `v2.5.0-cli` tag，不再把桌面 latest 当 CLI 版本。
- 执行复用 `native_process_sandbox.prepare_native_process` 的 Codex 写隔离，以及现有
  `run_cli` / `legion_proc_registry` 生命周期。缺少真实可用的后端直接报环境错误，
  不关 sandbox 重试。`--yolo` 是边界内工具自动批准，不是关闭内核隔离。
  外层 CLI 超时先结束包装进程并让原生 scope 锁释放，再按其 PID 调现有
  `kill_owned_by` 回收已登记子组；不扩大成扫描/杀掉同一用户的全部进程。
- 机器输出声明 `error_message_field`，在现有预算内优先保留原生诊断，再放 stderr。
  进程创建失败，或原生命令在任何 Cline 事件前退出，沿现有 `error_envelope`
  回传不可通过换模型修复的运行失败；已进入原生 API 的错误仍保留自己的分类。
  未声明机器输出的普通业务 JSON 不能据同名字段变成运行控制。

## 部署复用

基础 spec 不含机器目录。发布时沿既有 `runtime.deployment_directory` 为 Cline 指向
一个提前校验的固定 `source` / `runtime` 配对目录；只在发布副本持锁 upsert 该声明。
保留旧 p75097875，不原地 npm install、不改它的标记。

准确主源码 1888 个 Git blob 可从固定官方归档按清单验证后复用。独立 gitlink
`evals/cline-bench@d1085569fb0ae3f9613957e6fc2706c6e2f7da9b` 的许可尚未确认，
只保留上游引用，不将父项目 Apache-2.0 当成该独立仓库的许可，也不称已迁移该测试数据集。

## 当前证据边界

已完成原生假凭证配置检查、Linux 隔离原生 CLI 写文件/原 taskId 续作读回、
续作换模型 wire 与 metadata 一致；上述协议检查没有调用真实模型。
另已用真实 CLI 的受控超时验证：已观察进程在返回超时后不再运行，原生 ID 保留，
下一轮在该 ID 上恢复。这不代表所有未登记后台工具或任意故障都已覆盖。

私有真实服务入口首单已由 Cline 交出原始要求的 27 字节文件并真实下载；首选 GLM
超过本次 150 秒验收预算后，同一家改用 DeepSeek 收尾，总计约 168 秒，不算速度通过。
之后 DeepSeek 在同一成功原生 ID 上续作约 12.6 秒完成；旧交付器漏收未加反引号的
`.txt` 路径，已修正为按真实文件与既有安全范围识别，不再使用媒体扩展名白名单。
已保存原生结果通过修复后的收集器和真实授权下载再验一致，没有修改文件或伪造新模型轮次。

上述是私有候选/API 与明确边界的原生证据；历史结果未被手工重写。真实页面呈现、
Cline 正式激活、全部工具/MCP/复杂任务和 GLM 性能仍未验收，不代表全家完整迁移完成。
阶段验收记录位于开发 evidence，不随正式包携带测试产物。
