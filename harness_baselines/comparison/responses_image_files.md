# 可选的 Responses 图片 Files 复用

此能力默认关闭。它只改变发往同一提供方的图片传输表示，不改 native rollout、checkpoint、provider_binding、模型、上游 base URL 或授权，不转成 Chat 协议，也不发额外模型请求。

在已有 `LEGION_CODEX_RESPONSES_BINDING` 中增加：

```json
"image_file_cache": {
  "profile": "deepseek-files-v1",
  "ttl_seconds": 3600
}
```

显式启用的是已核对的 Files 协议，不按域名、模型名或报错自动开启。配置仅在 model/base/auth 与本次调用完全匹配时生效。个人实例的 `configure-model` 和 `save-model-profile` 可使用 `--image-file-cache-ttl-seconds 3600`；已有模型配置的其余选项应完整保留，配置保存不是运行验证。

`detail=high/original/auto` 的 inline PNG/JPEG/GIF/WebP 可转换；`low`、远程 URL、已有 file_id、未知 detail 原样传递。省略 detail 时默认不转换；只有操作者已核实上游省略 detail 的语义，才额外声明 `"default_detail":"auto"`（或 high/original），CLI 对应 `--image-file-cache-default-detail auto`。这项声明不推断提供方行为。明确的 low 始终不转换。

范围包括 user/developer 消息的 content，以及 function_call_output/custom_tool_call_output 的 output 列表。文本、工具参数、call_id、顺序和其他字段保留；不递归改写工具参数字符串或 reasoning 项。仅在内存中的发送副本用 file_id 替换 image_url，保留 detail 字段；提供方忽略 file_id 上的 detail，但这三种已声明语义均保留原图。

上传使用原 base URL 下的 `POST /files`、原授权、原图片 bytes，multipart `purpose=user_data`、`expires_after[anchor]=created_at` 和 `expires_after[seconds]`。TTL 必须为 3600–2592000 秒。首次仍需上传，后续用 `GET /files/{id}` 验证缓存引用后发送 Responses。无重定向，无桥内自动重试。

缓存位于原 `CODEX_HOME/meyo-image-files`（未设 CODEX_HOME 时使用原 HOME/.codex），禁止落入本次工程目录。其下按用户 hash、上游身份 hash、原图 bytes SHA-256 分隔，目录 0700、记录和锁文件 0600；不存图片、授权或提示词。锁覆盖查找、验证、上传和原子写入，支持桥进程重启及并发请求复用。TTL 取服务器过期时间、服务器创建时间加配置 TTL、上传开始时间加 TTL 的最早值；更改为较短 TTL 也不会延长旧引用。

本地 TTL 到期会正常淘汰并上传一次。远端 404/410 或已过期时淘汰引用，并回报本次失败，不在同次请求内重传整段或重新上传；下一次明确请求才可重建。网络、HTTP、元数据和缓存错误返回 HTTP 424，包含固定的 `files_decode/files_cache/files_upload/files_validate` 阶段及错误码，可带上游状态码，不包含原错误正文、URL、图片或授权。已有 Responses 上游错误及 SSE 原样回传，已有显式错误兼容策略保持原契约。不制造完成事件，不回退 inline。

配置启用使用 Codex 已有的每次重建 client 路径；先核对原 provider_binding，再启动受管桥，仍运行原 `exec resume`。不会删除或补造旧 checkpoint 的 bridge 字段，也没有放宽 Omnigent 的 `native_bridge_unverified`。

源码依据：

- https://api-docs.deepseek.com/api/create-response/
- https://api-docs.deepseek.com/api/create-file/

本地验证入口：`release/legion-service/tests/test_responses_image_files.py`。其中真实 HTTP/bridge 和原生 CLI 测试只连接 127.0.0.1 假提供方，使用假凭据与合成图片；它们不证明真实提供方识图效果。
