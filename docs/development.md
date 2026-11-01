# 一支战队，一个维护源

军团核心与 DSH 连接器现在在本仓库一起维护。用户仍然可以从原来的
`seanliii/dsh-plugin-legion` 安装插件；那个仓库是连接器的发行镜像，
不再作为另一份独立改功能的源码。

```text
这里改代码：meyo-legion-core
  ├─ release/legion-service/           军团核心、协作、工具与交付
  ├─ harness_baselines/                原生成员的接线与保留源码
  └─ connectors/dsh-plugin-legion/     DSH 原生入口、界面与连接器
                  │
                  └─ Git subtree 导出 → 原 dsh-plugin-legion 安装仓库
```

这统一的是维护与交付，不是把所有成员改写成一个 Agent。成员仍保留各自
原生工具、上下文与恢复方式；连接器不自带或自动启动核心，也不提供任何共享账号。

## 为什么保留两个安装包

- Python 核心和 DSH 插件的安装方式、依赖与运行环境不同。
- 原插件仓库根目录仍有 `package.json` 和 `cordis.patch.yml`，已有安装地址与旧标签不变。
- 核心 TAR 保持原打包器的**完整源码树**契约，现在也包含连接器子目录。
  独立 TGZ 仍只含连接器声明的文件，不把核心运行时塞进插件。
- 核心变化但连接器没变时，可以复用同一连接器提交；不能只因为版本号相同，
  就认为两份程序或两个运行实例相同。

## 日常修改与检查

所有产品改动在本仓库提出并审查。若收到针对发行镜像的修改，先带回本仓库，
通过验证后再单向导出；不要让两个目录各改一套，也不要自动强推覆盖未知提交。

在已安装项目声明依赖的开发环境中，复用原测试入口：

```sh
CORE="$(git rev-parse --show-toplevel)"
TEST_STATE="$(mktemp -d)"
MEYO_LEGION_DIR="$TEST_STATE/meyo" LEGION_HOME_DIR="$TEST_STATE/legion" \
  python3.12 -m pytest -q -ra "$CORE/release/legion-service/tests"

cd "$CORE/connectors/dsh-plugin-legion"
npm run check
LEGION_CORE_PACKAGER="$CORE/scripts/package_core.py" \
  node --test --test-concurrency=1 test/*.test.js
```

实际原生 CLI 未安装时，相关测试会注明跳过；跳过不算该成员已验。
跨包配对测试需要显式的可信核心打包器，不能漏掉环境变量后把跳过算通过。
网页、真实模型任务、恢复和正式成果下载，仍须按当次改动回到实际入口验收。

## 同源导出，不另造同步器

先提交本仓库的修改。以下命令只导出 Git 身份，不进行任何公开推送：

```sh
CORE="$(git rev-parse --show-toplevel)"
CORE_COMMIT="$(git rev-parse HEAD)"
CONNECTOR_COMMIT="$(git subtree split --prefix=connectors/dsh-plugin-legion "$CORE_COMMIT")"

test "$(git rev-parse "${CORE_COMMIT}:connectors/dsh-plugin-legion")" = \
  "$(git rev-parse "${CONNECTOR_COMMIT}^{tree}")"
printf 'core=%s\nconnector=%s\n' "$CORE_COMMIT" "$CONNECTOR_COMMIT"
```

最初导入的连接器历史需保持可达。本库使用标准 `git subtree split --rejoin`
把已审查的连接器导出历史接回主库，根目录文件树不变；干净 clone 后也有 split 需要的对象。
不要仅凭 squash 提交消息中的 SHA 就假设 CI 一定取得了那个对象，也不要借这个步骤
接入任何未审查的私有分支。日常只读导出不需要每次 rejoin。

在**已有发行镜像的干净功能分支**里，先从本机主库 fetch 这个导出提交，再以
`git merge --ff-only` 更新。不能快进时，先检查是否存在镜像侧独立修改，不用 force
抹掉它。不要将主库的核心 commit 误当成 DSH 连接器安装 commit。

沿原 `package_core.py` 与 `npm pack --ignore-scripts` 生成固定包，再调用
[原配对入口](../connectors/dsh-plugin-legion/docs/RELEASE-PAIR.md)，增加：

```sh
--connector-source-path connectors/dsh-plugin-legion
```

配对器会核对主库指定提交中的完整子树与连接器指定提交根树，包括没有进入 TGZ
的源码文件；内容、路径、权限模式或版本对象不匹配会拒绝生成同源证明。
它不会联网、安装、选择成员、调用模型、判作品质量或替你公开发布。

## CI 自动做什么

[主库工作流](../.github/workflows/ci.yml) 使用固定的 Actions 提交和只读仓库权限：

1. 串行安装声明的开发依赖，运行原核心与连接器测试。
2. 用 Git subtree 导出保持原包根的连接器，核对两边 tree。
3. 用原打包器和 npm 生成包，用原配对器验证同一主库来源。
4. 对这对包运行已有 DSH 原生兼容检查与下载协议 fixture，保存回执。

这些是源码、包、协议与原生服务事实，**不是完整模型评测或真实用户作品验收**。
工作流不使用个人或公司模型密钥，不推送镜像、不建 Release、不更新市场。
CI 导出成功、远端已同步、用户实例已升级必须分别报告；不能画一个总绿灯吞掉差异。

## 发布与历史

公开前明确批准的仓库、包、索引和网站范围。只推审查后的分支／标签，不推全部 refs，
不将旧私有维护分支的父历史带入公开线。QA、会话、账号、原始评测答案与模型凭据
留在源码仓库外；第一方与第三方许可各自保留。

两个远端不是原子事务。用同一份配对记录跟踪两个固定提交、原包 hash、渠道回执和
实际安装，缺一边就保留缺口，不移动旧标签或覆盖旧附件来制造同步。
配对记录可能含本机路径与私有验收字段，不直接上传；公开材料仅输出审查过的必要事实。

已经冻结的模型评测不因为后续修复或目录统一而改写身份。新修复需要自己的回归和
实跑证据；修复后的候选不能借用旧版本的分数、安装数量或录屏。
