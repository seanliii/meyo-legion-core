# 私有核心的随包自检

复用 `harness_selfcheck.py` 及其既有 pytest 执行器，**必须显式选择分发模式**。
默认的大仓 diff、历史测试选择、缓存及验收入口不切换到这个模式。

## 用法

将 `CORE` 替换为已解包源码的绝对路径，`REPORT` 指向已存在的 QA 目录：

```sh
CORE=/absolute/path/to/extracted-core
REPORT=/absolute/path/to/qa/core-selfcheck.json
python3.12 -I -B "$CORE/release/legion-service/harness_selfcheck.py" \
  --distribution private-core --run-tests --output "$REPORT"
```

- 解包目录不需要 `.git`；本模式不读取 diff、不使用 Git 自检缓存，也不导入大仓环境诊断/发布验收栈。
- 需要该 Python 中已有 pytest，以及 PATH 中可用的 Git。打包回归会在临时目录自建微型 Git 仓库；这不等于依赖旧源码仓库。
- 不自动安装依赖、联网、调用模型或启动服务。
- 子测试使用新的 HOME、CODEX_HOME、MEYO_LEGION_DIR、LEGION_HOME_DIR 和临时目录，不继承提供方凭据、代理、配置路径、PYTHONPATH 或 pytest 环境参数；禁用 pytest 插件自动加载、缓存和 Python 字节码写入。
- 若自设 TMPDIR，必须放在源码仓库/已解包源码之外。打包回归会真实提取，不能通过把测试产物嵌入旧源码来绕过打包器的安全边界。
- `--no-cache` 可带可不带，分发模式始终真实运行、不复用缓存。
- 本模式拒绝 `--files`、非默认 `--base`、`--environment`、`--quality-levels`、`--live-harnesses` 和 `--product-target`，不能借这些参数缩减必测范围或悄悄变成在线验收。

## 固定范围和结果含义

以下是服务目录内的固定随包测试名，不按“目录里恰好有什么”动态缩减：

```text
tests/test_core_distribution.py
tests/test_personal_runtime_bootstrap.py
tests/test_harness_selfcheck.py
```

- `tests.scope=private-core-regressions`：只代表以上源码包、个人配置/身份、自检分发回归。
- 任一必需文件缺失：`tests.status=failed`，列在 `tests.missing`，退出码 **1**；不跳过、不启动剩余测试。
- 未给 `--run-tests`：只报告范围，`tests.status=not_verified`，退出码 **1**。
- 已运行：pytest 非零就失败；退出码 **0** 只表示随包回归通过。
- `historical_tests.not_included`：历史大仓映射中未随包提供的测试文件。
- `historical_tests.not_verified`：本轮没有通过验证的历史测试，包含缺失项；即使文件存在，也不能靠“存在”算绿。只有本轮实际通过的随包清单与历史映射重叠项会被排除。
- `historical_tests` 和 `live_harnesses` 保持 `status=not_verified`、`ok=false`，与随包回归结果分开，不合成为“全仓/全军团已通过”。

本入口不证明任何真实成员已运行、planner/提供方连通、产品交付或新环境安装验收完成。
它只填补独立源码包的离线回归入口，不恢复整个大仓历史测试。

## Superagent：三个只读 capability 动作

`dsh-superagent` 是 DSH 原生能力包，不是另一名答题成员。
声明维护在 `release/legion-service/external_adapters/dsh-superagent.spec.json`；
登记继续调用 `harness_onboard._upsert_catalog` 的原校验/持锁入口，不手改 catalog，
也不为更新三个动作运行安装、模型或完整 onboarding。

| 统一工具 ID | 原生 GET 路径 | 保留的只读观察 |
|---|---|---|
| `external_capability:dsh-superagent:status` | `/orchestrator/api/state` | pause、队列数量、setup 问题代码、健康状态 |
| `external_capability:dsh-superagent:settings` | `/orchestrator/api/settings` | revision、并发/轮询/observe 开关、仓库等非凭据配置 |
| `external_capability:dsh-superagent:sources` | `/orchestrator/api/sources` | 来源标识、启用/安装状态、健康状态及数量统计 |

发现时的 `runnable=true` 只表示已有调用实现，`installed/health` 保持未知；
调用成功的 `ok=true` 只表示读成功，不表示 setup 完成或已经接单。
例如 `repositoryUnset` 与来源 `missing/off` 都可能是一次成功读取到的真实状态。

### 显式私有实例绑定

调用方必须有绝对路径的 `MEYO_LEGION_DIR`。仅从该目录下
`external_agents/instances.json` 读取当前 capability 对应条目：

```json
{
  "dsh-superagent": {
    "url": "http://127.0.0.1:<操作者明确确认的端口>"
  }
}
```

替换示例占位符后再保存；文件需为当前 OS 用户所有、权限 `0600`，
且文件及固定 `external_agents` 子目录不能是符号链接。该条目只允许 `url`，
不要填 token、cookie、header 或代理，也不要把文件放进公开源码包。

- 只接受 `http://127.0.0.1:显式端口` 或 `http://localhost:显式端口` 的纯 origin；
  不接受凭据、路径、query、fragment、其他主机或默认端口推断。
- `localhost` 的连接固定到同端口的 IPv4 回环，HTTP Host/Origin 仍使用配置的 origin。
  不经过环境代理、不跟随任何重定向，不读取浏览器、GitHub 或其他账号的凭据。
- 当前调用方的显式环境优先；空绑定也保留为空，不借用进程中另一个实例的私有目录。
  这是已有调用方上下文的传递，不是替不可信多租户提供 OS 沙箱。
- 未配置返回 `instance_unconfigured`；配置有误、端点关闭、HTTP 401/403/5xx、
  非 JSON 或超过 1 MiB 都如实失败。动作不启动 Host、不启动 watcher、不重试接单。

原有 command 动作不变。新声明在原 `capability_actions` 内复用 HTTP transport 结构：
`type=http`、`method=GET`、`read_only_loopback=true`、固定 `url` 模板及必要的固定头部。
三个动作无调用参数，不能由任务参数覆盖实例、方法、身份或 headers。

### 不能把原始管理 JSON 当公开日志

固定上游原型的 settings 序列化直接返回 executor 与 Source config；
state 的错误详情、sources 的过滤/丢弃消息还可能包含自由文本。上游并未提供
“任意内容都已脱敏”的保证。

因此三个动作声明 `response_fields` 白名单，在进入统一工具结果及事件日志前裁剪，
再复用现有诊断脱敏函数；返回 `data_view=redacted_observation`。
不返回 `prepare`、CLI/目录/阻断命令、环境变量、账号原始对象、模型凭据、
规则正文、错误 detail/output/values、消息 preview 或其他未声明字段。
隐藏不等于未配置；需要私有完整配置时，应由操作者在原本地管理界面查看，
不能把本入口扩成任意管理代理。HTTP 失败也不回显原始响应体或重定向位置。

### 调用与本次测试范围

```sh
CORE=/absolute/path/to/core
STATE=/absolute/path/to/this-caller-private-state
MEYO_LEGION_DIR="$STATE" python3.12 -B "$CORE/release/legion-service/legion_tool.py" \
  run external_capability:dsh-superagent:status '{}'
```

同样调用 `settings`、`sources`。已有 Host 的启动/停止仍由原操作入口负责，
不重建 profile，不修改它的 `cordis.patch` 或数据库。

专项测试为 `tests/test_external_capability_http_readonly.py`：
真实回环 HTTP、未配置、上下文/私有目录隔离、GET/同源、无凭据转发、无重定向、
错误/体积/超时边界、含 token 与私有命令的输出/日志负例，以及旧 command 回归。
NativeCapabilitySession 的单测父上下文不是 GitHub 身份、Trigger 或真实订单。
真实独立 Host 的三次读取只证明管理能力接线，不替代
真实账号→Source→Eval→Ticket→产物→反馈恢复的接单验收。
