# 军团核心：承载顶尖 Harness 的协同进化战队

**军团的主体是 Harness 战队，不是工具菜单。** 目标是让任何顶尖 Harness 保留自己的能力加入团队，共同为用户工作，开放工具与接口，互相学习和改进。DSH 是工作入口，核心负责运行、连接、恢复与事实传递；成员自己组织、实施与进化。[完整产品定义](ECOSYSTEM.md#co-evolving-team)不应被当前已验证的单成员安装路径缩小。

下表的三层是分发与运行结构，不是“只接上核心就实现战队”的完成判据。实际协作、开放工具互用以及改进被其他成员采用，仍要分别有真实任务证据。

**新用户先做：** [下载公开核心 v0.1.0 并建立实例](#public-core-install)，启动后将 `dsh-config` 输出的地址与凭据引用保存到 DSH 插件。v0.1.0 是已发布的技术预览，核心不随连接器打包。

当前交付分三层：

| 层 | 放什么 | 不混淆什么 |
|---|---|---|
| 本公开插件 | 原生侧栏五页工作台、工具与配置、开源发现、读源、适配包装、任务接续、正式文件下载 | 不捆绑核心、用户数据／日志／凭据、第三方上游全量、模型权重或公司大仓及其历史。 |
| 独立军团核心 | 原服务、编排与协作、持久会话、研究与知识入口、正式成果出口及选定共享工具源码 | v0.1.0 公开源码技术预览，独立安装；第一方 MIT，第三方原许可保留。 |
| 按需成员和能力 | 原生 Harness、CLI、MCP、skill、媒体模型、浏览器与设备 | 各自安装、配置、授权和验证；目录有名字不表示已经可用。 |

本页对应已公开的连接器 **0.4.2**。公开核心已从匿名下载包建立全新 Python 3.12 环境，并在 DSH 中完成保存连接、读取成员、页面刷新及双方服务重启后的恢复，见 [0.4.2 新装记录](PLAN.md#v042)。本轮模型未配置，未执行新的模型委托；[0.4.1 无核心 UI 检查](PLAN.md#public-r-acceptance)与[旧私有核心 548d148](PLAN.md#core-548d148)仍只属于原版本。

## 军团的核心没有变成一句宣传语

以下是独立核心中的实际程序入口，不是本插件重新实现的一套指挥系统。模块路径相对于**核心包根目录**，不是本连接器仓库。

| 用户的理念 | 程序承载 | 在工作中意味着什么 |
|---|---|---|
| 先理解人想得到什么 | `release/legion-service/quality_context.py`、`legion_core.py` | 保留原始需求、意图与作品上下文，不把一句话缩成工具关键字。 |
| 看世界上的好东西，再决定怎么做 | `research_pipeline.py`、`quality_context.py` | 研究入口与来源事实可进入当单上下文；URL、正文与失败状态各有意义，不是搜索返回成功就算做过研究。 |
| 小而完整、真正有用的首个作品 | `quality_context.py`、`legion_core.py` | 保留完整纵切的上下文；范围、媒介和验收由当单需求决定，不硬编码游戏、PPT或网站答案。 |
| 顶尖 Harness 作为战队成员共同工作 | `legion_core.py`、`collaboration_runtime.py` | 复用原有派单、协作和成员身份。新个人实例已验的 Codex 配置路径只是当前入口，不是产品固定队长、唯一成员或完整战队验收。 |
| 接力不能失忆 | `team_state.py`、`task_memory.py`、原生成员 checkpoint | 原任务、工程和已知事实随合法续作保留；不复制旧 UUID 或权限冒充新身份。 |
| 出问题自己看到并返工 | 原执行器、协作事件与 `legion_core.py` | 工具结果、运行错误、当前产物和反馈回到工作现场。保留续作能力不保证每次模型都能自主修好。 |
| 共同进化，不止保存经验 | `source_audit.py`、`delivery_knowledge.py` | 原源码审核和经验入口可承接部分工作；目标是实际改进、复验、共享并被其他成员采用，不能把索引存在或一份审核记录说成整条循环已完成。 |
| 做出来还必须交得出来 | `project_context.py`、`artifact_delivery.py`、`artifact_store.py` | 合法工程身份、允许导出范围、实际文件与正式清单相接。运行 `done` 不能替代正式导出。 |
| 生态按需扩展 | `runtime_manifest.py`、`external_agent_registry.py`、`harness_onboard.py` | 原成员目录、安装和探测入口保留，实际安装与运行各自核对。 |
| 不止写文字，也能操作工具和设备 | `capstack_bridge.py`、共享工具总线、`cloud_desktop.py`、`agentbay_mobile.py` | 媒体和设备沿原能力入口接入；仍需要真实软件、模型、账号与明确授权。 |
| 能长期工作，也能准确停下 | 原会话持久化、取消及进程登记机制 | 工作与恢复状态保存在独立私有实例，不通过杀掉未知进程或丢失历史“重置问题”。 |
| 修传递，不另造质量裁判 | DSH 原生运行时 ↔ 本插件 ↔ 原军团服务 | 让执行者看到情况与反馈，而不是在上层加一个固定评分器替它答题。 |

表中除首行明确写出的前缀外，核心模块位于 `release/legion-service/`。共享工具的选定源码在 `runtime/core-sources/auto-tools`，`retained-tools.json` 记录文件版本、hash与调用范围。此前独立包的清单快照保留 155 份共享工具源码；其中 `duckdb_query`、`ijson_stream`、`sympy_calc` 是三个明确的 **bridge 调用绑定，不是军团的全部工具**。这份清单不是所有保留工具已可执行的证明。

其它能力沿既有入口工作：`legion_tool.py` 的搜索和网页／GitHub读取、`research_pipeline.py` 的资料研究与图谱、`func_search`／`func_call` 的源码函数发现和调用，以及获准后的原生能力宿主。桥接绑定限制不能套到这些独立入口上。每条入口仍有自己的依赖、权限和失败事实，不能将“源码函数可发现”说成“所有函数已可运行”。

`capstack_bridge.py` 是共享工具调用桥，不是经验学习引擎。源码数量不是工具数量，保留实现也不自动启用旧 Auto 引擎。

### 搬程序之外，还要把工作现场接上

历史接线修复保留了三类原本容易丢失的信息，均复用原机制；以下是接线说明，不代替指定版本的运行回执：

- **工具授权现场：** 授权检查和实际调用使用同一可信身份／范围快照；父级策略收窄、空范围和取消仍生效。不是看到一把工具就自动得到使用权。
- **已选择的历史经验：** 当单明确选中历史类别时，除了旧条目，还能看到最近收集的观察、各自来源和审核状态，并按需读取完整历史。`pending_review` 不会借旧条目的等级变成“已验证成功”；未选中不自动灌入正文。
- **操作者确认的长期方向：** 独立实例有 `configure-goals --file`／`--clear` 接口，示例可保留世界研究、完整纵切、动态协作、看图返工和真实交付等理念。默认不采用；不从旧仓或 Shell 偷带，也不伪装成本轮用户发言。

长期方向仍走原续作入口：既有评价的继续分支只选一条相关方向、最多 500 字；
不是每个成员或首轮都自动收到整份理念，首脑直接继续分支也不保证经过它。
这些修复的版本、正式包和真实调用证据分别记录在[状态表](PLAN.md)，不能互相替代。

## “吸星大法”吸收的是什么

更准确地说，是**发现别人的专长，读懂真实接口，再让它成为团队能用的一员**，
而不是把一个 GitHub 地址登记进去就自动掌握整份代码：

```text
发现项目或插件 → 读源码、接口和许可 → 复用或编写适配
              → 安装必要依赖 → 实际调用 → 留下可复用的入口和证据
```

这是一条能力接入的说明，不是要求每个任务都走一遍的固定流程。
已经可用的 DSH 插件可以直接复用；CLI、MCP、skill 和完整 Harness 各自保留原生形态。
军团可以组织它们协作，DSH 仍负责自己的会话、工具执行和权限；双方不是互相替换。

### 哪些成员和工具的代码已保留

下表保留此前独立核心清单的真实声明快照，不代替具体包的运行验收，也不是“已装好全部生态”的广告。

| 类型 | 历史清单中的内容 | 尚不能据此认定 |
|---|---|---|
| 原生成员适配 | `harness_manifest.json` 保留 `codex`、`cc`、`deepseek`、`deepagents`、`hermes`、`auto`、`jcode`、`omnigent`、`penguin`、`prime` 10 个适配入口。 | 不是打包了所有成员的完整上游仓库、CLI、依赖与账号；入口存在不表示该成员已经运行。 |
| 外部项目目录 | `external_agents.json` 有 30 项声明，包含 Kimi Code、Qwen Code、Aider、OpenCode、Goose、Cline、OpenHands 等成员，也包含 Paperclip、Browser Use、OhMyGame、OpenMontage 等平台或能力组件。 | 不是 30 家答题成员，更不是 30 家实测通过；平台和能力角色不能冒充可答题 Harness。 |
| 原共享工具源码 | `retained-tools.json` 列出 155 份保留源码及原版本／文件摘要；三个 bridge 绑定之外，保留原工具 CLI、研究、图谱和源码函数入口。 | 不是 155 个已可调用工具；`engine_enabled=false`，旧 `qa_auto_agent.py` 仍为 reference-only。 |
| 工具、媒体与设备接线 | 原工具总线、安装中心、媒体入口、浏览器／设备模块随核心保留，按实际依赖和授权启用。 | 模型权重、GPU、浏览器登录、设备服务和第三方账号不会因复制源码而自动获得。 |

因此，目前不能说“我们所有代码、所有工具都已启用”。应保留核心机制的真实实现，
把尚未接通的部分明确列出来并继续接线，而不是复制无关业务、密钥和历史来凑一个
“完整仓库”。实际成员状态从 `/legion runtime members` 和
`/legion runtime capabilities` 查询；保留返回的未安装、未配置、过期和失败事实。

**普通任务续作与旧验收中心后台自动修复不是同一条入口。** 历史包中的个人 Responses 修复入口 `verification_workflow.py` 需要上游鉴权后传入 `user_id` 与 `conversation_id`。已记录的旧调用只传 `actor`、缺少身份字段时，会在派单前返回 `planner_identity_required`，不借服务所有者身份，也不回退默认 `cc`。`actor` 只是显示／审计信息，非空检查不代替鉴权。这不表示普通任务都不能续作，也不证明个人模式无人自动修复已闭环；后续版本的接线与运行结论须以该包自己的回执为准。

**“世界最强团队”的定位是产品目标，不是已证明的排名。** 单成员成功不能证明动态协作胜出；代码保留不能证明无人自治。是否完成，要回到同一用户需求的真作品、操作、继续修改及文件读回。

## 为什么不是把全部仓库复制进去

核心应保留的是研究、执行、协作、记忆、恢复、工具和交付能力，而不是无差别打包所有历史。独立包保存真实程序字节，不靠一个指向旧开发目录的链接运行；第三方 Harness 的原生 CLI 与依赖仍沿各家入口安装。

以下内容不应随公开连接器搬走：

- 公司来源项目的完整 Git 历史、无关业务代码和内部资料。
- 用户会话、产物、工作区、令牌、设备凭据与运行数据库。
- 未取得分发依据的第三方源码或模型权重。
- 机器专用 `.venv`、`node_modules` 和指向旧开发树的快捷链接。

私有包的文件 hash 解决“这一包到底是什么”；不是安全认证或公开分发许可证。历史固定核心 548d148 的 **606 项本地来源清单与逐文件字节对照已完成**，不是公开发行、权属认证或运行验收。新公开核心 v0.1.0 的身份与源码校验另行记录，不借这份旧包清单背书。

已确认随包的 Codex 资源内嵌了 Apache-2.0 全文和 NOTICE，应保留其来源与许可条件，不统一改标为连接器的 MIT。OpenMontage 在此包中只有项目接线／测试和 source-only 声明，不含其上游实现树，不能据该声明判定整个核心不可公开。

<a id="public-core-next-steps"></a>

### 公开核心 v0.1.0 与许可

第一方军团核心、选定 auto-tools、打包器与文档已获明确授权，以 **MIT** 公开。第三方源码、资源、依赖的许可、署名与 NOTICE 各自保留，不统一改标 MIT。

[`seanliii/meyo-legion-core`](https://github.com/seanliii/meyo-legion-core) 的初始公开历史只有源码快照提交 **`98f1c107c12b8e70c90658dad1ecdf53a3f371e5`**，不含私有历史；`v0.1.0` 固定到该提交，GitHub Release 标为 **prerelease／技术预览**。公开树的 **610 个 Git blob** 与受审快照一致；源码包匿名下载后，现有可信校验器的 `verify`／`extract` 已通过。

公开包格式如下：

```json
{
  "format": "meyo-public-core-v1",
  "distribution": "public-source",
  "license": "MIT",
  "rights_verified": false,
  "member_execution_verified": false
}
```

新格式**不含 `public_license_granted` 字段**。两个 `false` 不否认第一方授权，也不表示全量权属审计或全成员执行已完成。全新 Python 3.12 环境的依赖安装及后续服务／DSH 连接验收另见 [GitHub main 回执](https://github.com/seanliii/dsh-plugin-legion/blob/main/docs/PLAN.md#v042)，不由源码校验推导运行成功。

**旧私有包原样保留。** 以下是既有 `private-local-only` 包的历史字段，不是新公开包模板，也不改写旧归档：

```json
{
  "distribution": "private-local-only",
  "public_license_granted": false,
  "member_execution_verified": false
}
```

这些是旧打包器写入的**固定元数据**，不是用户未授权公开的证据，也不是一次运行检测的结果。第一方核心的 MIT 来自独立确认，不由连接器许可或旧包字段推导；具体成员的实际运行范围看各版本回执，不因单次成员成功就把清单改为“全员验证”。

历史的[首次互动网页](PLAN.md#真实作品验收到哪里)、[60872ced 续作与重启](PLAN.md#historical-60872ced)以及 [c8f10c6 正式 TGZ 出口与调用](PLAN.md#formal-marked-acceptance)分别保留在状态表，包含原失败、监督者反馈、完整包身份与实际操作范围，不把其中一次成功移给另一版本。

这些是限定实例、成员和作品的证据，不是全成员、无人自治或所有功能质量的认证。包校验、随包回归与真实作品验收仍各自记录，互不代替。

## 安装自己的实例

<a id="public-core-install"></a>

### 0. 下载并核对公开源码

从以下固定发行取得源码，无需旧私有包或原公司大仓：

| 项目 | 固定公开版本 |
|---|---|
| 源码仓库 | [`seanliii/meyo-legion-core`](https://github.com/seanliii/meyo-legion-core)，公开初始提交 `98f1c107c12b8e70c90658dad1ecdf53a3f371e5`。 |
| 固定发行 | [`v0.1.0`](https://github.com/seanliii/meyo-legion-core/releases/tag/v0.1.0)，已发布的 prerelease 技术预览。 |
| 源码包 | [`meyo-legion-core-0.1.0.tar`](https://github.com/seanliii/meyo-legion-core/releases/download/v0.1.0/meyo-legion-core-0.1.0.tar)，**610 份源码文件，13,762,560 字节**。 |
| 相邻校验文件 | [`meyo-legion-core-0.1.0.tar.sha256`](https://github.com/seanliii/meyo-legion-core/releases/download/v0.1.0/meyo-legion-core-0.1.0.tar.sha256)，内容为下方 SHA-256。 |

源码包 SHA-256：

```text
c5f1d5392be92b858525ff43736caf6d6ed0fc1b07ac66df72f2acbaa7c00c64
```

在本机下载目录执行；`.sha256` 是纯摘要，不是带文件名的校验清单：

```sh
curl --fail --location --output meyo-legion-core-0.1.0.tar \
  'https://github.com/seanliii/meyo-legion-core/releases/download/v0.1.0/meyo-legion-core-0.1.0.tar'
curl --fail --location --output meyo-legion-core-0.1.0.tar.sha256 \
  'https://github.com/seanliii/meyo-legion-core/releases/download/v0.1.0/meyo-legion-core-0.1.0.tar.sha256'
cat meyo-legion-core-0.1.0.tar.sha256
printf '%s  %s\n' \
  'c5f1d5392be92b858525ff43736caf6d6ed0fc1b07ac66df72f2acbaa7c00c64' \
  'meyo-legion-core-0.1.0.tar' | shasum -a 256 -c -
```

相邻摘要应与上方一致，校验须返回 `OK`；失败时不要解包。成功后，使用不存在的新目录，源码文件位于归档根部：

```sh
mkdir meyo-legion-core-0.1.0 &&
  tar -xf meyo-legion-core-0.1.0.tar -C meyo-legion-core-0.1.0 &&
  cd meyo-legion-core-0.1.0
```

需要逐文件核验时，按[固定版本的公开包指南](https://github.com/seanliii/meyo-legion-core/blob/98f1c107c12b8e70c90658dad1ecdf53a3f371e5/docs/public-core-release.md)使用已审查、可信的校验器；不要执行未知包来给它自己背书。

接下来按 **Python 3.12 全新环境 → `init` → `serve` → 另一终端 `dsh-config` → 插件保存连接**操作。核心仍另包安装，成员、权重、账号与设备按需配置；本次公开版本的新环境运行及连接回执在 [GitHub main](https://github.com/seanliii/dsh-plugin-legion/blob/main/docs/PLAN.md#v042)单独记录。

<a id="当前可核对的包"></a>

#### 历史私有包：只供旧版本复现

**既有私有核心 `548d14893ca62e776f60c50761cabac4e19585ba` 仍是 `private-local-only`，没有该旧包的公共下载地址。** 固定包身份与校验、原实例保留、首次及续作默认正式导出、下载读回和随包关键回归的实际范围见 [548d148 记录](PLAN.md#core-548d148)。这些是限定实例与作品的证据，不代表全成员实跑或完全无人值守；[c8f10c6 历史包](PLAN.md#historical-c8f10c6)及更早版本的 SHA、失败和验收仍分别保留。

已合法取得旧私有包的操作者，可继续按该包的 `scripts/package_core.py --help` 和 `docs/core-private-package.md` 核对、解包到新目录；不改旧包字段或 hash。**这条历史路线不是新用户安装的前提**，也不要把旧开发环境当成公开版本的“全新安装”。

### 1. 独立环境与基础依赖

需要 Python 3.12；以下启动入口面向 macOS/Linux。依赖安装串行执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple \
  -r release/legion-service/requirements-core.txt
```

不使用 `--system-site-packages`，不复制旧虚拟环境。基础依赖不会安装所有 Harness、GPU模型或设备服务。需要 Auto/DeepAgents Python 依赖时，才另装 `release/legion-service/requirements-auto.txt`，并验证相应能力；不是默认全部启用。

### 2. 新建私有实例

选择源码目录之外、仅本人访问的目录，端口不能占用已有服务：

```sh
.venv/bin/python -I meyo.py init \
  --state-dir "/绝对路径/我的军团实例" --user my-user --port 8015
```

初始化只在本机建立身份，不创建云端账号；重复初始化不会重置历史。登录凭据在实例的 `login-token.txt`，不要发到聊天、版本库或公共文件服务器。

### 3. 配置本人模型

只想先启动并连接实例时，可先跳到第 4 步；实际委托任务前再配置本人模型，配置后按原入口重新启动服务。

先确认自己的 `codex --version` 可以正常执行，再使用本人提供方实际支持的模型与 **Responses** API；不能把 Chat Completions 地址当成同一协议。

```sh
.venv/bin/python -I meyo.py configure-model \
  --state-dir "/绝对路径/我的军团实例" \
  --model "提供方实际支持的模型ID" \
  --base-url "https://本人提供方的API基础地址/v1" \
  --api-key-file "/绝对路径/本人所有的0600密钥文件"
```

这条个人配置只显式绑定 Codex，不授权所有成员，不借用其他登录或环境密钥。配置保存不等于真实请求验证；换提供方也不自动把旧会话发给它。

#### 启动脚本依赖 HOME 时

`check` 中的 `codex_executable_found: true` 只表示能找到程序路径，不代表它在隔离实例里能启动。
个人包装脚本可能从 `$HOME` 拼出另一个程序位置；军团保留独立 `HOME`／`CODEX_HOME` 后，
这个位置可能不存在，导致 `rc127`。这发生在模型调用之前，不能归因于模型回答能力。

先找到本人已安装、已审查的**原生 Codex 程序**，在同样的隔离目录下只查版本：

```sh
env HOME="/绝对路径/我的军团实例/home" \
  CODEX_HOME="/绝对路径/我的军团实例/codex-home" \
  "/本人实际安装目录/codex" --version
```

确认退出 0 后，让这个实例的原有 `serve` 启动环境优先使用该程序目录：

```sh
PATH="/本人实际安装目录:$PATH" .venv/bin/python -I meyo.py serve \
  --state-dir "/绝对路径/我的军团实例"
```

路径是模板，不要求所有用户安装同一应用。不复制登录 token，不把 HOME 改回日常账号，
也不在找不到程序时静默换模型；服务重启和后续实际任务分别验证。

若任务在启动前已明确失败、没有原生 checkpoint，`legion_resume` 可能如实返回
`no recoverable tasks`。这不代表恢复成功，也不是让操作者改写旧任务状态。
确认原失败和副作用后，可用既有 `legion_submit.previous_task_ref` 发起有原任务来源的接续，
使用新的稳定接续 key，并保留首次失败。接续是新运行，不冒充原生 checkpoint 恢复，
更不能用于盲目重发结果未知的操作。

#### 可选：明确采用自己的长期方向

核心包中的 `docs/examples/pilot-goals.md` 是可编辑示例，不会默认生效。
确认内容后，使用核心的原配置入口：

```sh
.venv/bin/python -I meyo.py configure-goals \
  --state-dir "/绝对路径/我的军团实例" \
  --file "/绝对路径/确认过的产品方向.md"
```

只保存唯一、非空且不嵌在 AUTO 中的 MANUAL 区，存为本实例私有文件；正文不会自动脱敏，
不要放入凭据。`check` 回报来源状态与 hash，不打印正文。
撤回使用同一命令的 `--clear`（不与 `--file` 并用）。
下次读取生效，不撤回已发上下文、不取消当前任务、不增加执行权限。
实际消费范围见上文，不把配置成功当作无人自治或模型已采用的证据。

#### 图片输入需要真实的能力声明

需要成员看图时，先核实**同一提供方、同一模型和同一授权**确实接受图片请求，再在上面的 `configure-model` 命令中显式增加：

```sh
--input-modality text --input-modality image
```

这只是随包原生配置入口的选项，不是单独执行的命令。不要猜模型支持图片，也不要为了消除报错把不支持的能力标成支持。已运行的服务需重新加载该配置。

旧互动网页验收中的真实故障是图片声明缺失：提供方图片请求已成功，但原生执行环境尚未声明图片输入。补齐声明后，同会话作者的 `view_image` 调用返回了 `input_image`，作者据截图修正了作品。配置保存回执仍为 `provider_verified: false`，真实请求与工具 trace 另行保存，不把“写了配置”变成“调用已验证”，也不声称此视觉验收已在当前包重跑。

### 4. 先启动原服务，再接到 DSH

```sh
.venv/bin/python -I meyo.py check --state-dir "/绝对路径/我的军团实例"
.venv/bin/python -I meyo.py serve --state-dir "/绝对路径/我的军团实例"
```

保持服务运行；在另一终端进入同一源码根目录，再执行：

```sh
.venv/bin/python -I meyo.py dsh-config --state-dir "/绝对路径/我的军团实例"
```

`dsh-config` 只输出 `baseUrl` 与 `tokenFile` 等非秘密接线信息。按其 **`config` 对象**填写 DSH 插件的 **连接与配置 → 核心地址／私有令牌文件路径 → 保存连接配置**，随后刷新成员。不要粘贴整份回执，也不把令牌值放进配置、命令或聊天；模型密钥也只通过本人私有文件提供。`check` 不调用模型；`serve` 启动原 `server.py`，不是另造调度器或自动公网部署。保存和刷新不等于模型已完成任务。

DSH 与核心不在同一机器时，核心文件路径不可直接使用；保留 HTTPS 服务地址及 Host 私下配置的 `tokenEnv` 方式，见[协议](PROTOCOL.md#7-configuration)。

#### 随包自检的范围

以下保留历史私有包已经使用的自检入口与范围；新公开包的自检结论须以该固定版本自己的回执为准。需要复现这些随包关键回归时，在核心包根目录使用：

```sh
.venv/bin/python release/legion-service/harness_selfcheck.py \
  --distribution private-core --run-tests
```

这是显式的独立包模式，不依赖解包目录有 `.git`，也不做真实成员体检。范围仅为 `release/legion-service/tests/` 下的 `test_core_distribution.py`、`test_personal_runtime_bootstrap.py`、`test_harness_selfcheck.py`；应随包测试缺失仍硬失败，不按现存文件缩小范围。

大仓历史测试缺失项列入 `historical_tests.not_included`，未验证项保留在 `historical_tests.not_verified`；真实成员的 `live_harnesses.status` 仍为 `not_verified`。不加 `--run-tests` 只列范围，不能算通过；退出码 0 也仅表示随包关键回归通过，不代表大仓全量、真实成员或用户作品已验收。这里的 `private-core` 是历史自检模式名，不改变旧包的 `private-local-only` 分发标记，也不决定新公开包的分发或许可状态。

548d148 的随包关键回归已有[实际通过回执](PLAN.md#core-548d148)，仅限上述三个测试文件。旧自检失败、历史缺失／未测项与真实成员未验证状态仍分别保留，不能把独立包模式的通过说成全仓或全家通过。

### 5. 真正走一遍，而不是只看绿色灯

从 DSH：

```text
/legion runtime members
/legion runtime capabilities
/legion start first-work 制作一个我能实际打开和使用的小作品，保留可继续修改的工程。
```

任务引用来自真实回执，接着查状态、下载正式成果，并用 `continue` 回到原会话修改。重新启动核心后，再查看同一任务与文件。分别记录：

1. 服务能启动、目录能读取。
2. 具体成员实际完成请求。
3. 真实文件出现在正式成果清单并成功下载。
4. 下载 hash 匹配且作品实际能打开、操作。
5. 同会话续作与服务重启后，原任务和文件仍可恢复。

其中任何一项都不能替代其他项；演示任务也不能证明所有顶尖 Harness、所有专业场景或整套自治能力已验收。`legion_runtime` 返回的过期、预热、错误与未安装事实应保留，不能包装成“全生态已就绪”。
