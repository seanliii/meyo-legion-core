# OhMyGame 游戏协作工作室

## 接入的是什么

官方项目：<https://github.com/WhiteTowerAI/ohmygame>。
本接入固定为 `0.0.0-beta.4.7`，源码提交
`29bd5d4747064cf30432c09d64510e00629bced5`。

使用官方 macOS Apple silicon 完整桌面包，不从源码重建、不升级全局 Bun，
不裁剪原生 Pi Agent、画布、资产工具、插件或 `game_use` 试玩窗口。
Windows、Linux 和其它架构不在本次已验证范围内。

它以**可调用的游戏协作工作室**接入，而不是把一个桌面应用伪装成普通 CLI 答题成员。
因此目录角色为 `ui-platform`，不占普通问答成员池；其他成员通过现有共享工具总线
把任务交给其原生 Agent，并读取它的状态与结果。

用户希望游戏需求优先考虑这项协作。这个偏好与真实能力描述放在成员目录里，
供军团按当单需求判断；没有增加游戏关键词路由、固定引擎选择或质量裁判。
首次检索和缓存目录会补入当前已声明的协作动作，不用等完整源码索引构建完成。
查询返回的 `installed: null / health: unknown` 表示这一步没有探测后台，
不是“已经安装/已能制作”的结论；实际状态由 `status` 或动作本身返回。

## 安装与持久状态

声明在 `release/legion-service/external_adapters/ohmygame.spec.json`，
通过现有 `harness_onboard.py` 的注册、安装、构建检查及能力动作验证进入目录。
调用安装入口时必须使用已初始化的**个人实例环境**，不能落回开发机的旧公司状态目录。

- 应用：实例的 `runtimes/ohmygame/app/OhMyGame.app`。
- 下载缓存：同一 runtime 的 `.ohmygame-cache`，用于明确的完整性复核。
- 专属数据：同一 runtime 的 `operator/data`。
- 原生模型和会话配置：`operator/pi-agent`。
- Chromium 用户数据：`operator/chromium`。

完整安装检查核固定下载摘要、包内路径、应用版本、内容和代码签名；
普通发现只读目录声明，调用时再检查实际运行条件，不为每次检索重扫完整应用包。
不覆盖全局 Applications，也不读取或迁走其他 OhMyGame 实例的账号。

## 模型连接

安装和打开工作室不需要模型密钥。**真正制作游戏需要先在 OhMyGame 原生
Settings → Providers & Models 中连接语言模型**。图像、视频和 3D 生成还需要对应连接。

军团不会把公司默认 key 或其它成员的授权偷偷转交给它。
`status` 返回实际 `model_count` / `needs_model_setup`；
没有语言模型时，`delegate` 不创建或提交模型任务。

## 原生成员如何调用本机工作室

macOS 上，工作室的 `ps/lsof` 身份检查必须由受管宿主执行，不能要求成员在
命令沙箱里绕过失败的检查。个人实例可以明确授权已有动作，原生 Codex 的
standard/deep 执行与恢复随后通过 `meyo_capabilities` MCP 调用同一工具总线。
quick 不额外挂载这组工具；原生文件工具与命令沙箱保持不变。

例如，实例所有者只授权状态读取时：

```sh
.venv/bin/python -I meyo.py configure-capabilities \
  --state-dir "$HOME/.local/share/meyo-legion" \
  --tool external_capability:ohmygame:status
```

要授权制作协作，可重复 `--tool` 明确加入 `create/delegate/continue/result/inspect`
等所需动作的完整 ID。配置只保存当时列明的动作，不自动授权未来新增动作；
服务在下一次正常启动读取配置。命令不执行任何动作或发模型请求。

成员通过 `list_capabilities` 读取真实声明，再用 `call_capability` 选择已授权动作。
业务参数放在明确的 `params_json` 字符串中，例如
`{"request_file":"/absolute/task/workspace/request.json"}`；无参数动作用 `{}`。
不把动态字段藏在没有属性说明的空对象 schema 中；解码后仍执行相同的字段与范围检查。
宿主固定调用者身份、原会话、工作目录和权限；工具参数不能覆盖这些字段。
`request_file` 必须是工作目录内的常规文件，先保存该次调用的临时字节快照再执行；
已有 `workspacePath/output_file` 参数也须留在工作目录范围内。
这不是对提示词、原生 Agent 行为或所有嵌套业务字段的操作系统级隔离承诺。

该 MCP 不需要个人 HTTP Token，不接收模型密钥；外部能力不会在此模式自动回填
旧模型目录里的凭据。OhMyGame 仍使用自己已明确配置的模型。
宿主进程独立登记在已有进程注册表，结束后注销；不会因一次工具调用结束关闭桌面。
原生父回合退出不代表外部异步任务已取消，仍需按真实项目、会话、turn ID 查结果或取消。

## 现有共享工具入口

工具 ID 以 `external_capability:ohmygame:` 开头。

| 动作 | 用途 |
|---|---|
| `open` | 打开或复用本实例专属桌面，不提交模型任务 |
| `status` | 读连接和模型状态，不启动第二个工作室 |
| `models` | 读原生可用模型，不读密钥 |
| `projects` / `inspect` | 读工程及原生会话 ID，便于接手已有项目 |
| `create` | 创建工程或接入已有目录；不替成员选择制作方案 |
| `delegate` | 原样传递需求，交给原生 Pi Agent |
| `continue` | 沿已有 `projectId` / `conversationId` 传递反馈和返工要求 |
| `result` | 读原生状态、失败和会话结果 |
| `cancel` | 仅按明确取消意图取消指定原生 turn，不关闭其它任务或工作室 |

有参数的操作接受 `request_file`：当前任务内的 JSON 文件路径。
需求正文不塞入进程命令行。

委托已有工程的示例文件内容：

```json
{
  "workspacePath": "/absolute/path/to/your/game",
  "name": "原有游戏",
  "prompt": "用户完整需求、已知现场及当前授权范围。保留已有有效成果。"
}
```

接续使用委托回执中的真实 `projectId`、`conversationId`，不要猜 ID 或复制一份新会话。
可以传递原生 `references`、`images`、`attachments`、`mode`、`mentions` 和 `contexts`；
接收、设计、实现和试玩仍由 OhMyGame 自己完成。

**`accepted` 只表示请求已受理，不表示游戏完成。**
下一次判断须读取 `result` 中的原生状态和真实成果。
传输中断后的提交结果不明时，先查同一会话，不自动重复 POST。
连接器会在提交前保存已知原生 ID 和阶段；即使外层等待超时，也返回这份操作回执。
HTTP 5xx 不会被当成“肯定没受理”，后台原任务不会因连接器超时被取消。
公开发布、付费或其它外部副作用仍须符合用户明确授权，不因委托而扩大权限。

大结果用当前任务内新的 `output_file` 路径保存完整 JSON；文件权限为0600，
已有文件不覆盖。没有提供输出文件且响应超出共享工具返回预算时，会明确要求文件，
不会截掉一半后冒称返回完整结果。

## 本机信任边界

这是上游定义的**单一可信用户本机工作室**，不是多租户或宿主机级安全沙箱。
不要把它用作不可信用户共享的远程执行端。

连接器只管理自己的应用路径、私有 profile 和已核 PID 身份；
仅连接该进程的回环调试端口，选取该安装包的 renderer。
通过原生 `runtimeService.state()` 读取当前后台连接，
原生 token 只在连接器内存中使用，不落到参数、日志、工作目录或返回值中。
不得把调试端口或后台端口代理到公网。

桌面应用有意留给用户持续使用，由它自己管理 Pi、预览和试玩进程。
短工具调用结束不关闭用户窗口；窗口就绪前只等待连接，不重新启动实例。
PID 或应用身份不符时拒绝接管，不按陈旧数字杀进程。

## 验证范围

已验证的安装、原生连接和工程元数据交接，不等于语言模型已经配置、
真实游戏制作已经成功，或所有媒体连接都能用。
协议回归保留“不把受理当完成、不重复未知提交、继续使用原生 ID、失败原样返回”等边界。
真实游戏质量仍需在完成模型连接后，由军团通过实际任务与试玩验收。
