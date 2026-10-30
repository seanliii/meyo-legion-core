# Prime 原生会话兼容补丁

## 2026-09-20 运行接线整合（候选，未随正式包发布）

- 后续异常回收修订：固定默认 daemon 的 `client_owned` 断连清理已有无模型实测，
  owner 客户端退出后约 30.28 秒，原生 worker 与其休眠 Bash 子进程退出，
  descriptor 清除；这 30 秒是原生重连宽限，不是任意任务的完成时限。
  Meyo 的登记回收不能提前把 supervisor 与普通 CLI 一起杀掉。
  当前源码增加协议优先清理：登记中仅存原生身份、固定路径和 PID namespace，
  复用 scope 锁与原生 shutdown，确认已观察子树、socket、descriptor 收尾后再按
  原记录一致性删账；失败、身份变更及旧恢复记录未核实则留账，不回退强杀。
  真实 Linux “测试 guide 被 SIGKILL→registry 接手”已单列验证：
  原生关闭用时约 0.175 秒，已观察到的 supervisor、worker、Bash 子进程、
  SDK 客户端、socket、descriptor 与登记均收尾；平台未对它们发送强杀信号。
  这不等于 supervisor 自身被外部硬杀、任意逃逸子进程或全部跨 namespace
  场景已经验证，相关未知仍保留。
  不启用 `PRIME_AGENT_INTERNAL_LEGACY_OWNED_WORKER_FRONTEND` 来替换默认能力。
- 正常个人 HTTP 接口已在隔离候选上通过原文件输入及一次只读续作：
  两轮分别 21.15／23.34 秒，Prime 自己完成、无接力，原生 SID 与工程相同；
  原文件 inode／大小／mtime 不变，两次真实下载 27B 逐字节一致，历史为两轮。
  原生 JSONL 中的两条 `ipython` 调用及结果已核对，不以平台摘要的
  `tool_calls=0` 推定没用工具。该摘要缺失和费用默认 0 仍待修，不据此宣传
  零成本或全能力通过；这还是隔离候选验收，不是正式 Prime 已上线。
- 固定源码仍为 `1eee2938`，复用已安装的专属 Node/Python runtime，不重新安装全部成员。
- `prime_cli` 在执行原任务前复用同一组源码、CLI、provider、kernel 和模型配置检查。
  `OwnedPrimeDaemon` 先锁定私有 IPC scope，锁内写配置并直接启动原生 `--mode daemon`；
  新进程立即登记，再由发行物自身 `DaemonClient` 核对 PID、出生身份及 socket。
  发现已有未知 endpoint 时保留它和原 checkpoint，不接管、不删除、不发出新任务。
- registry 模板先写独立 `registry-models.json` 作为配置输入，不再提前覆盖原生的
  `models.json`；只有 guide 取得 scope 锁后才写实际运行配置。否则即使 guide
  拒绝接管，原配置也可能已经被上游改掉。
- 原生 print CLI 结束后，经同一条已核连接发 `shutdown`，由持有 `Popen` 的 Python 回收进程。
  不能先等原生 PID 消失再 `wait`：未被父进程回收的 zombie 会造成关闭误报超时。
  正常运行与错误展开分别由 CLI transport 和 daemon scope 回收自身进程；
  外层不再追加无条件 `kill_owned_by`，避免绕过 scope 的“身份不符，不发信号”结果。
- 成员 final/checkpoint 与 `native_facts.daemon_lifecycle` 分开；清理失败不得擦除已生成的答案。
  daemon 关闭确认不等于全部 detached worker 已回收，SIGKILL 后恢复、残留 worker
  以及已有未知 socket 的自动恢复仍须独立验证，不声称这些能力已经完成。
- 已有私有 Linux 证据证明同版原生 JSONL/SID 与 Python namespace 跨 CLI、新 kernel 进程恢复：
  nonce 摘要保持、count 从 1 到 2；它不是用户任务质量或跨版本兼容证明。
  新生命周期接线已在固定候选 `3d353c5` 续作同一原会话一次：20.008 秒，
  原对象 count 从 2 到 3、nonce 摘要不变、新 kernel 176001，原生工具记录
  证明先读取内存对象再写/读文件；supervisor 原生退出 0、无人工信号，
  已观察进程及私有登记均为空。只证明这一次正常恢复和收尾，不外推异常退出。
  固定证据索引为开发仓 `dev/prime-native-lifecycle-verified-2026-09-20.json`；
  不为重新确认相同结论重复发模型请求。

## 初始兼容阶段的历史范围与依据

范围：旧 `prime` key 薄接已有 `prime-intellect` registry CLI transport；不安装、不运行模型、不部署、不修改共享 catalog。不改 JCode、通用别名、通用文本解析器或 `_safe_key`。

## 安装缓存边界（修订）

第一版改过通用 registry；本版已将其逐字恢复为分支起点 `dd6f255c` 的内容，
SHA256 为 `5250fcb3d12397047cf4ce3d4c9663147d237afa13f7d40274e24bf3a8de74ac`。
通用 transport、harness_onboard、安装器及缓存算法也没有修改。

已核 mars-wall `bin/build-external-harnesses.py:_legacy_identity`：旧身份包含整个 registry SHA 和整个 catalog item；
缓存实现另有按安装输入分析的分支。本补丁不依赖某版缓存恰好忽略运行时代码，也不删除或放宽 fingerprint 字段。
Prime 自己的 transport/spec 会变化，旧整条目指纹下仍可能只令 Prime 自身需要重新判定；
不能承诺其原 `.build-ok` 必然命中。其他条目和共用文件的安装输入不因本补丁变化。

`prime_cli.py` 是普通 CLI 引导器，不是新 Agent：
原 registry → 普通 CLI transport → 引导器读取 `runtime_context_json` →
调用同一个现有 CLI transport 启动原生 Prime。新增子进程仍由既有进程登记/回收逻辑管理；
引导器收到 SIGTERM/SIGINT 时调用已有 `kill_owned_by` 清理自己的 CLI 子进程。

## 已查证的源码事实

- 本机 `prime-agent@0.8.0` 是 PrimeIntellect-ai/prime-agent 的官方发行包，不是 AISA。
- 目标声明来自 mars-wall **b93c72c** 的 `legion/legion-service/external_agents.json`。官方源码 revision 为 `1eee2938b4eeb7a4d72e17035adda669a89b63de`，归档 `prime-intellect-1eee2938b4ee.tar.gz` 的 SHA256 为 `3149b9df41b3a56313bfcc51acbbb1afe8c0a19560876afeb4d4bd5ed59a31b2`，与 source-manifest 一致。
- 0.8 `dist/core/tools/ipython.js:372–425`：启动新 kernel 进程，但持久会话的 `snapshotDir` 会用于 `restoreState()`，不是每次清空状态。`dist/core/kernel/state-snapshot.js:1–28` 定义 `kernel-state.dill`/`.json`，`dist/core/session-manager.js:69–76,990` 定义目录为 `session-dir` 父目录下的 `session-artifacts/<native-id>`。
- 新版 `src/core/session-manager.ts:1473–1507`：坏文件可能被重建，缺文件可能变新会话；`:1676` 保留相同 artifact 目录算法。`:40` 与旧包均为 v3，但版本号一致不证明 kernel 快照兼容。
- 新版 `src/core/agent-session.ts:9557`、`src/modes/print-mode.ts:98–106` 分别绑定持久快照目录、输出原生 session header 与 JSONL 事件。
- 0.8 bootstrap 需要 `rlm.run`；目标 bootstrap.ts:885–919 需要 `rlm.spawn/create_session`。不能把新版 CLI 配旧 Python runtime。

## 数据与失败边界

新 checkpoint：

```json
{
  "kind": "prime_session",
  "session_id": "<原生 header 的 UUID>",
  "session_path": "<完整绝对 JSONL 路径>",
  "logical_session_id": "<经现有 _safe_key 处理的业务会话标识>",
  "daemon_socket": "<同一个原生 prime.sock 绝对路径>",
  "config_dir": "<原配置目录>",
  "runtime_revision": "1eee2938b4eeb7a4d72e17035adda669a89b63de"
}
```

旧 `session_id=<JSONL路径>` 只在 Prime 专属代码中识别。业务标识用于配置/IPC；原生文件路径独立传递。恢复时 `--session-dir` 指回原 JSONL 的父目录，避免“历史找到了，kernel 快照却换了目录”。

通用 registry 不必学会新字段：它仍只保存 `session_id=<JSONL路径>` 引用。
引导器在 `session-artifacts/<native-id>/meyo-prime-runtime.json` 原子保存完整身份，
恢复时校验文件/ID并找回同一逻辑标识、配置目录和 daemon/socket，不采用被通用层转义的路径作为新业务标识。
没有历史 socket 记录时，仅按原有 `_session_socket_dir` 和调用方逻辑身份推导；
不声称能够从旧 0.8 JSONL 推知其未记录的活 daemon PID。
core 薄入口通过 `unwrap_result` 读取 wrapper 的完整结果；失败时读取 registry 已有的私有失败证据，
不让通用 parser 丢掉的字段变成“已恢复”。外部 registry 的原始返回仍是其既有引用格式，完整恢复事实在 wrapper envelope 中。

校验包括：真实文件、非符号链接、可完整解析的 v3 header/UUID、消息树父子引用、重复 ID、原生输出 header 与退出后文件 ID 一致。坏 checkpoint 在 CLI 前返回，原文件和原 checkpoint 不改；失败不退新会话。未收到可信原生 header 时不报告恢复。超时保留已有 checkpoint。

旧/来源未知的 checkpoint 若存在 kernel 快照或 ipython 调用历史，返回 `prime_legacy_kernel_resume_unverified`，不反序列化、不删快照。即使同 revision，历史用过 ipython 而快照已丢，也不以空 kernel 顶替。

同 revision 的快照只保持位置并交原生实现处理；**离线测试没有证明 Python 变量恢复成功**，结果明确带 `native_facts.kernel_snapshot_restore=not_verified`。真实恢复、daemon 取消与跨版本迁移由 A 的窗口另验。检查不能替代原生进程内的文件锁；本补丁不声称防御其他进程在校验后恶意删除/改写会话文件。

文本/usage 复用通用读取函数，但只消费 assistant `message_end`；`turn_end` 不重复计数。读取现有完整私有终端日志，不能只读 transport 返回的最后 100 条。错误终态/仅工具输出不能冒充 final。

## 应用 spec（由整合方执行；本任务只在临时 catalog 验证过）

同目录 `prime-intellect.spec.json` 是完整声明，保留固定版的源码、安装步骤和 kernel/env 配置。**不要直接覆盖共享 dirty external_agents.json**。

只登记、不安装/跑模型时，在目标 service 目录调用现有持锁入口：

```python
import harness_onboard
harness_onboard._upsert_catalog("prime-intellect", "/absolute/path/prime-intellect.spec.json")
```

该函数内部验证声明并持 `catalog_lock()` 读改写。整合方应在应用前核对共享 catalog 中 Prime 条目的新改动，再合并到 spec；全条目 upsert 不能自动合并同一条目的并发语义修改。本补丁未执行真实 upsert；本地 shell 不应运行完整 `harness_onboard --spec`，因为后续会安装/探测/调用模型。

已核当前 `harness_onboard.main`：有 `--key`、`--spec`、`--build-only`，没有独立 `--core` 参数。
`--build-only` 仅接受已登记 key，且不能与 `--spec` 混用；完整 `--key ... --spec ...` 会继续运行后续阶段。
core 的 `prime` 入口由 `_prime_adapter` 保留，spec.key 必须是 `prime-intellect`；
将本 spec 用 `--key prime` 登记会明确 key 不匹配，不应另加 alias 或复制一份安装身份。

## 写面之外的已知边界

`legion_core.py:8502` 的旧 `prime` 可用性探测仍查 PATH/家目录。该文件不在本任务授权写面，未修改：无旧二进制的机器可能仍把旧 key 显示为不可用；`prime-intellect` 的 registry 入口不依赖这段旧探测。整合方若要求旧 key 的目录状态也反映包内 runtime，需另授权该探测分支；不能用假二进制或 AISA 填过去。

## 离线验证

`tests/test_prime_native_compat.py` 使用临时目录、fixture Python CLI、假的 kernel 文件；
集成测试真实起 wrapper 并经过未修改的 registry/CLI transport，内层 fixture 不含网络或模型代码。
故障注入单测在进程内调用同一引导器并禁网。包含临时 catalog 持锁 upsert，以及通用引用跨回合恢复同一 socket/config/原生 ID 的测试。

运行时设 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，避免系统无关 Phoenix 插件因缺 `pytz` 在收集前失败；无需安装依赖。配合已有 usage、registry 异常、原生输出测试组合验证。本任务未运行模型监督 skill；改前快照、diff、AST 和无网回归代替外发模型审核。change-log skill 指向的模块在本工作树和主工作树均不存在，故变更记录由本说明与 Git commit 保留，不伪报已调用。
