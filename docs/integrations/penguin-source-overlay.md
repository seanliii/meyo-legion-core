# Penguin：保留原生永久停止事实

本目录配方只针对 **Penguin v0.2.4** 的固定上游提交
`966af59018562657df0a9853671ff41e7b1ea027`，不改模型，也不根据答案内容决定好坏。

## 修的是什么

已核的原生通路在两个地方丢掉明确的“不可再试”事实：

1. 普通模型错误转换成 `failed` 时，没有把真正 HTTP 错误中的布尔停止事实带下去。
2. 原生上下文压缩的结果投影再次丢掉这个字段，后面的重试器只能继续等待。

补丁让已有消费者看到原值。未知错误、临时限流和普通 503 保留原重试行为；
字符串 `"true"`／`"false"` 不当作布尔值，不能仅凭 HTTP 422 就停止。
已经收到的用量和另一条在途成功结果仍保留。

## 这是源码配方，不是安装成功的声明

配方在 [`external_adapters/overlays/penguin-v0.2.4/`](../../release/legion-service/external_adapters/overlays/penguin-v0.2.4/)。
目录包含两文件补丁、原许可证、前后摘要及 `source_patch_hook.py`：

- 核验实际 Git 根、公开来源、固定 revision、两个文件的前后 hash 和补丁 hash；
- 只允许专用构建目录，不改全局安装或已发布的固定运行代；
- 重复 apply／revert 是 no-op，遇到未知修改拒绝覆盖；
- 不 clone、安装依赖、构建、启动模型或挂接运行实例。

在既有声明式 `install` 数组中，可以将下面一项放在固定源码取得后、原构建步骤之前：

```json
["{python}", "{adapter_dir}/overlays/penguin-v0.2.4/source_patch_hook.py", "apply", "--source", "{source}"]
```

经过审查的**完整**构建 spec 使用原入口：

```sh
python3.12 release/legion-service/harness_onboard.py \
  --key <declared-member> --spec /absolute/path/reviewed-spec.json --build-only
```

该组合复用原持锁登记和安装器，不调用模型，也不把仅构建成功冒充运行验收。
安装步骤仍需真正构建被修改的 core 和 CLI；改了源码却再安装另一版官方 npm 包，
运行的就不是这份补丁。

## 已验与未验

固定源码候选与独立实际构建的 JavaScript bundle 均通过 25 个原生协议正反例：
永久拒绝从 6 次请求降为 1 次；压缩路径不再多等 5 轮退避；临时／未知错误、
成功文本、已知用量和在途成功对照保持。制品 source map 中的两个 after hash 与补丁一致。

这是该版本的机制重放，不是完整模型评测、不证明正确率提高，也不代表默认
`penguin-harness` npm 条目或用户全局 Penguin 已被更新。正式挂接还要在独立实例明确
选用新 CLI、读回实际路径与制品身份，再从正常入口实跑；不要借用原冻结矩阵的成绩。

源码回滚沿 hook 的 `revert`；运行回滚则切回保留的旧制品，只对新请求生效。
源码还原不会自动还原已经编译的 bundle，也不应热改正在工作的进程。
