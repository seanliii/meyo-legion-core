# 按次调用 Decisions 模型

`runnable:model_decisions` 是既有共享模型工具的同族协议入口，不是新成员、
runner、裁判或自动工作流。调用者自行决定问题和候选 profile；不改变主模型或默认配置。

## 现成入口

在可信父层已经绑定的任务进程内，从服务目录调用：

```sh
python3.12 legion_tool.py run runnable:model_decisions \
  '{"profile_id":"<已配置的Decisions候选ID>","state":"调用者提供的上下文","questions":{"q":{"type":"noul","instructions":"调用者定义的判断题"}}}'
```

同一 JSON 也可从 stdin 传给 `media_model_cli.py tool model_decisions`。
共享发现由 `model_tool_specs()` 提供；native 的 `list_capabilities` / `call_capability`
复用同一入口，但父层必须单独授权 `runnable:model_decisions`。
MCP 调用时使用 `tool_id` 和 `params_json`（上述 JSON 字符串），不能由参数自行授权。

## 绑定与协议

- 必需 `profile_id`、`state`、`questions`；仅额外接受 `image`、`timeout_s`。
- 父层绑定私有实例、业务 owner、工作区及原 workspace identity；
  复用 `personal_runtime` 的 configuration、宿主 owner、0700/0600、no-follow 检查。
  `profile_id` 不是授权。未知、越权、绑定缺失固定失败，不借公司目录、key 或另一 profile。
- model、base URL、key 从同一个私有 snapshot 读取。工具参数不能提供
  model/base_url/api_key/env、私有配置路径或信任绑定；不调用 activate/save。
- profile 的端点路径必须明确为 `/api/alpha`，只追加 `/decisions`；
  OpenRouter 请求地址即 `https://openrouter.ai/api/alpha/decisions`。
  不把 `/api/v1` 偷换成 `/api/alpha`，也不自动选择模型版本。
- 历史 `openrouter-jev-router-20261018` / `typesafe/jev-router` 保留原值，
  仍由 `runnable:model_chat` 使用 chat 协议；不能据此宣称已配置 Jev 本体 Decisions。
- 沿用 billable/read 权限、总 deadline、可取消注册子进程与进程组回收。
  `read=false` 拒绝图片，仍可按 billable 授权处理纯文字。

## 文字、问题与图片

`state` 和每题 `instructions` 接受 JSON 字符串、对象或数组。
每题必需 `type`、`instructions`：

| type | criteria | 对应答案字段 |
| --- | --- | --- |
| choice | 非空对象；各值为字符串/对象/数组/null | `choice`：调用者候选中的字符串 |
| noul | 可省；提供时必须有 true/false，两值不可为 null | `noul`：有限数字，非布尔值 |
| score | 非空数组；各项为字符串/对象/数组 | `score`：有限数字 |
| box | 不接受 | `box`：top/bottom/left/right 四个有限数值 |

图片只接受 `image` 指定的一张工作区内 PNG/JPEG。普通目录、`.playwright-mcp`、
`.local-run` 可用；越界、私有/secret 目录、symlink、硬链接和 workspace 替换仍拒绝。
校验与发送使用同一次安全读取的 bytes，路径本身不发送。
图片变为 `state` 数组中的 `image_url` data URL；文字仍是普通字符串。
state/instructions/criteria 任意深度的 raw 媒体、`assets`、远程图片旁路均拒绝。

保留原 8MiB 文件上限；边长 256–1568，总像素 78400–2458624；拒绝 APNG。
PNG 复用已有像素/CRC 检查；JPEG 校验段头、尺寸及完整首扫描头，
不冒称完成熵解码，不自动缩放或旋转 EXIF。文字参数≤2MiB，请求体≤32MiB。

## 回执与验证边界

仅返回校验后的 typed answers、`configured_model`、`reported_model`、`provider`、
`request_id`、白名单 `usage` 与上游实际报告的 `cost/cost_source`；
缺少的可选事实为未知，不按 token 猜费用。原始错误正文、凭据及调试字段不输出。
缺失必需 model/usage、空答案或答案类型不符仍失败，保留已有安全回执；
CLI 退出非零，不把截断/失败视为成功。trace、broadcast、user/session 标识不自动传出。

离线验证仅证明 fake profiles + loopback HTTP 下的协议、图片 bytes、权限、
取消回收和新旧入口兼容。云 profile 核查、真实调用、作者任务采用及提速均未证明；
本阶段不触碰真实 key、配置或运行服务。官方协议依据为本次已保存的
Decisions API 与 multimodal decisions 正文，不以历史 Router 的结果替代本体验证。
