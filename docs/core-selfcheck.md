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
