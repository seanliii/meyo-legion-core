# 军团与 DSH：开放工具、共同工作的协同进化战队

**定位：让任何顶尖 Harness 都能组成战队，协同为用户干活，开放工具与能力，在真实工作中互相进化。** DSH 插件是入口，独立核心是运行底座，Harness 战队才是研究、组织、实现、检查和改进的主体。

从[产品定位与现状](PRODUCT.md)开始看：同一个目标、成员互用工具、有效改进在团队中传播。以下保留机制、接口与历史细节；不是要求用户先理解全部工具菜单。

<a id="co-evolving-team"></a>

## 产品定义：不是工具箱，是共同进化的战队

三个不能缩掉的重心：

1. **顶尖 Harness 共同为用户工作。** 产品不能锁定成一家主力加一列备用工具。现有和后续出现的优秀 Harness 都应有加入战队的路径，保留各自的执行、上下文、工具和恢复长处。具体任务由战队自主组织，不把角色永久写死在平台里。
2. **成员与开放工具相互增强。** 工具、接口函数、Skill 和可复用能力不只供一个中心调用，也应能在成员之间发现、调用和改进。DSH、军团及外部生态之间是双向连接；一名成员补出的能力，经过真实验证后可被其他成员接着使用和改进。开放的是合法接口与可共享能力，不是取消权限或复制所有私有代码。
3. **整个战队在工作中进化。** 不止存一段经验笔记：成员研究彼此可访问的源码和运行记录，发现问题、提出并实施改进、在原任务复验，再把有效改进回到团队可用的工具、Skill、适配或协作方法中。下次任务能真正用上，才是进化的证据；用户应感受到整个战队的能力，而不只是多个名字。

下面是目标体验的示意，**不是已完成的运行回执，也不是固定流程**：

```text
用户给一个目标
      ↓
顶尖 Harness 战队共同研究与组织
  成员 A ↔ 成员 B ↔ 成员 C ↔ 新加入的成员
      ↕ 开放工具、接口、Skill、资源与已验证经验
      ↓
共同完成一个真实作品，互相检查并继续改进
      ↓
有效改进回到团队：别的成员实际采用，后续任务继续受益
```

“任何／全部”表达的是开放成员范围和共同协作、进化的产品目标，不应被“默认只配置了一家”缩小。它也不是让每个简单问题机械启动所有成员、复制同一份劳动；怎样组成战队由当前目标与资源决定。

**安装与连接只是基础条件。** 要证明这套理念落地，必须能看到同一真实任务中成员互相补位、实际使用对方的能力、共同交付，以及改进被另一个成员或后续任务实际采用。成员数量、工具数量、消息数量、绿测和新装录屏都不能代替这些事实。

## 当前版本与历史范围

**0.5.0（已公开）：原生组队与单作者交付。** 原生组队、自动工作、恢复以及单 DSH 作者工具交付保留在该版的[历史记录](PLAN.md#v050)。那轮没有成员评审或改进传播，不代表后来没有发生协作。核心仍是独立安装的公开 v0.1.0，产品总体保持技术预览。

**0.5.1：成员事实、战队概览与成员分页。** DSH 作者模型与核心成员分别呈现。本轮已跑通 DSH 委托核心 Codex 独立源码评审、再由 DSH 逐项独立复现并采纳三个 P2 的限定双 Harness 协作；随后由 DSH 在隔离副本实施修复并回归，操作者按基准 hash 合回且独立复跑。不是无需监督的自动进化。详见[协作证据与发行状态](PLAN.md#v051)。

这轮有维护者修正启动通路和授权接续，不是无人自治；核心只交审查文字，DSH 的可见抄录未匹配原 `final_ref` hash，不是正式文件导出或 hash 核验通过。反馈采用已经发生，全团工具互用、改进自动传播与普遍行业领先仍未证明。[完整范围](PRODUCT.md)

<details>
<summary>保留的 0.4.2 分发与验收记录（历史，不是当前发行状态）</summary>

以下为 0.4.2 当时的范围和目录审核记录；原提交、标签、许可与验收归属保持不变。

**本版 0.4.2 的分发结构：DSH 插件 → 独立军团核心 → 按需成员与能力。** 这描述怎么安装，不替代上面的产品目标。先按[Git 安装说明](MARKET.md#v042)启用入口，需要委托工作时再用[公开核心 v0.1.0](CORE.md#public-core-install)建立实例；当前实现与缺口按版本核对。

**Edition 0.4.2 connects DSH to the separately installed, public core v0.1.0 technical preview.** The first-party core, selected auto-tools, packager and documentation are MIT; third-party licenses remain intact. The initial public source snapshot contains no private history. Post-release installation and runtime receipts belong on [GitHub main](https://github.com/seanliii/dsh-plugin-legion/blob/main/docs/PLAN.md#v042), not retroactively in frozen tag documentation.

**0.4.2 已公开，固定 Git 索引也已发布并通过 DSH“发现”页真实安装。** 新 profile 已启用工作台、保存独立核心连接，页面刷新与双方服务重启后配置仍保留；具体范围见[版本记录](PLAN.md#v042)。社区目录 PR #748 的文件已更新，仍待维护者合入；这与原生 Discover 安装是两回事。未做 npm publish，也不声称 1024Store 已有安装按钮。[0.4.1](PLAN.md#v041)、[0.4.0](PLAN.md#v040)与旧核心记录保持原版本和 hash。

</details>

## 先回答最容易混淆的三个问题

| 问题 | 准确回答 |
|---|---|
| 注册一个开源仓库，就能把全部代码变成工具吗？ | **不能。** 注册只是让它被发现；模型还需理解接口、选择可复用能力、写适配、安装依赖并验证真实调用。没有稳定接口的应用可能需要较多改造。 |
| 各种顶尖 Harness 全部装在插件里了吗？ | **没有。** 核心保留原成员适配、目录和接入机制；各家原生程序、账号、模型与设备按需安装授权。查看 `legion_runtime` 的实际事实，不能把目录当已安装清单。 |
| 是不是把我们全部代码公开了？ | **不是。** 连接器与公开核心 v0.1.0 分包；只公开选定源码快照，不含私有历史、实例、凭据或无关业务。第一方 MIT、第三方原许可各自保留；新公开格式不改写旧 `private-local-only` 包，见[分发说明](CORE.md#public-core-next-steps)。 |

<a id="native-workbench"></a>

## 原生工作台 / Native workbench

安装并启用连接器后，从 DSH 侧栏点 **军团 / Legion**，右上角选择 **当前工作会话 / Working session**。只有一个会话时自动选择；没有会话时先新建 DSH 对话，再 **刷新会话 / Refresh sessions**。工作台不另起网页服务、安装器或 agent；内部 `/legion-panel` 是 Host 数据命令（`recordInput:false`），不是需要手工输入的开台命令。

After enabling the connector in DSH Plugins, open **Legion** in the sidebar and select a **Working session**. Create a conversation first if the list is empty. The workbench uses native DSH services; it is not a separate site or agent, and users do not need to type its internal `/legion-panel` command.

| 页面 / Page | 操作与边界 / Actions and limits |
|---|---|
| **我的军团 / My Legion（0.5.0）** | **选战友 → 定协作 → 自动变强 → 组建战队并自动开工**。通过原生配置保存默认偏好，并通过 DSH schedule 安排首次与默认每周任务；状态、原会话入口、暂停和恢复见[自动流程](TEAM.md)。Uses native configuration and scheduling, not another agent loop. Selection is not installation; delivery is not completed improvement. |
| **找能力 / Discover** | 查 **本会话工具 / Skill**、**GitHub**、**DSH 插件市场**。Search current session tools/Skills, public GitHub or configured DSH market indexes. No Legion core or token is required; discovery does not execute tools. |
| **添加能力 / Add capabilities** | **安装现成 DSH 包 / Install an existing DSH bundle** 与 **交给军团接入 / Delegate integration** 分开。Install a reviewed bundle through the official manager, or send an editable goal to the configured core/models. Reading a repository does not install or submit it. |
| **战队任务 / Team tasks** | **刷新任务列表 / Refresh tasks** 读本地恢复索引；**读取实时进度 / Read live progress** 才向核心查状态。The list is scoped to the selected session and authenticated identity, not a live feed. Formal downloads reuse `/legion download` and save on the DSH Host, not automatically in the browser's Downloads folder. Continuation, feedback and cancellation use `/legion` in the original conversation. |
| **连接与配置 / Connection** | 修改地址、环境变量名或令牌文件路径，点 **保存连接配置 / Save connection**。Native configuration saves reload the connector and end local waits, not remote tasks. The page never reads or shows token-file contents. |

成员选择与连接配置、整包与子插件开关都作用于当前 **DSH profile**，不是仅影响所选会话。配置保存使用原生 revision；只读或冲突时重新读取，不强行覆盖。保存成功只说明配置被接受，不说明模型已能工作。

Member preferences, connection settings and plugin switches affect the current **DSH profile**, not just the working session. Saves require writable native configuration and the current revision. Reload after a conflict; configuration acceptance is not a connectivity or model test.

### 搜索的范围 / Discovery scope

- **工具和 Skill：** 工具来自当前 Agent 实际可见的 `ctx.tools.schemas`，首批最多 30 项；Skill 来自原生会话 Skill 服务，按名称与说明筛选。可以留空查看；要更多工具，缩小查询或在对话用 `legion_discover` 分页。两路错误分别显示，不把服务失败当“没有结果”。Skill 是使用指导，不是新增执行工具。
- **GitHub：** 首批最多 8 个公开仓库，不携带 Legion 凭据。**检查并交给军团 / Inspect & delegate** 会切到添加页、读取来源并填入可编辑目标，**不会立即派单或安装**。
- **市场：** 只搜索用户已配置索引，一次一个，首批最多 20 项；未配置时到 DSH 原生插件页添加可信来源，不自动加入中央市场。更多结果需缩小查询或到原生插件页浏览。**查看安装来源 / Review install source** 只检查安装来源，不代表安装完成。

Local discovery reads metadata, not tools: up to 30 visible tools in the first page plus native session Skills filtered by name/description. Tool and Skill failures are reported independently. GitHub returns up to 8 public repositories without Legion credentials; configured markets return up to 20 entries from one selected index. Refine the query or use the indicated native/tool entry for more. **Inspect & delegate** prepares an editable task; **Review install source** inspects a package source. Neither button installs or delegates.

### 安装和委托的两个按钮 / Installation and delegation

已有包：填包名、Git 来源或本地包 → **检查来源 / Inspect source** → 核对代码和依赖 → 明确点 **安装 / 更新（Install / update）**。工作台通过官方 `remote.pluginManager.inspect`／`installBundle` 操作，安装请求使用 `enabled:false`：新包不会自动选择启用；更新保留已有开关，已启用包可能继续运行。**若不希望更新后的代码运行，请先停用对应能力包，再安装更新。** 检查 `accepted` 只说明格式或元数据，不是可信认证。依赖构建批准独立于启用开关，仍在 DSH 原生插件页处理 `pendingBuilds`；停用并不禁止依赖构建。

An existing bundle follows **Inspect source → review → Install / update**, using the official `remote.pluginManager`, not a second installer. The request uses `enabled:false`: a new bundle is not automatically selected for activation, while an update preserves existing switches and an enabled bundle may keep running. **Disable the corresponding bundle before updating if you do not want the updated code to run.** An accepted source is not a safety endorsement. Dependency-build approval is separate from activation: disabled does not prohibit dependency builds; handle `pendingBuilds` in DSH Plugins.

安装后可在 **已安装的 DSH 能力包 / Installed DSH capability bundles** 中开关整包，展开 **查看子插件与加载状态 / Individual plugins and loading state** 后开关已有运行条目。界面使用真实包名／`entryId`，尊重只读状态；不允许在自己的工作台停用 Legion 连接器，需去 DSH 原生插件页。`applied`、`restart-required`、`overridden`、`failed`、`cancelled` 分别展示，不能把“已选启用”当“已实际调用”。

Bundle switches and individual plugin switches are separate and use actual native IDs. Read-only entries and this workbench's own Legion connector cannot be toggled here; manage Legion in DSH Plugins. Applied, restart-required, overridden, failed and cancelled outcomes are distinct. Refresh session tools after activation, then verify a real use.

回执不确定时保留本次安装编号，点 **读取本次安装结果 / Recover install result**，走原生 `waitForInstall`；不要换编号重装。**请求取消安装 / Request cancellation** 走 `cancelInstall`，不是已取消的证明。页面不再保留编号或主机找不到记录时，先到 DSH 原生插件页核对。

Recover an uncertain installation by its original request ID; a cancellation request is not a confirmed cancellation. If the page or Host no longer has the request, inspect native plugin state rather than blindly reinstalling.

还不是插件：填 `owner/repository` 和可选版本，点 **读取项目与许可 / Inspect project & license** 固定 commit；改好任务，再点 **交给军团接入 / Delegate integration**。也可不指定仓库，让军团自主研究。提交会使用已配置的核心与模型，可能计费；只附来源、commit、许可、抓取时间和错误等短事实，不把全部 README 塞进任务。页面保留本单时，未知回执用原去重编号恢复；不得靠换 key 重做未知副作用。

For a source project, pin its revision, edit the desired outcome and explicitly **Delegate integration**. A repository is optional. Delegation uses the configured core/models and may incur costs; it attaches short, untrusted source observations, not the entire README. Legion decides what to adapt and how to verify it. While the operation is retained, recover uncertain submission with its original key rather than inventing another task.

### 凭据轮换 / Credential rotation

`tokenFile` 只填 DSH Host 上的本机绝对路径，`tokenEnv` 只填环境变量名；都不填 token 内容。每次读取凭据时检测变化，已绑定连接器发现实际 token 改变后会停止旧连接器，**必须重载才能使用新身份**，不是无缝热切换。新服务地址／token 不继承旧身份任务索引，也不删除旧记录；保存、重载和轮换不会取消远端任务。文件无效时明确失败，不回退环境变量。

Enter only a Host-local absolute path in `tokenFile`, or a variable name in `tokenEnv`, never the token itself. Every credential read checks for changes. A changed token stops an already-bound connector and **requires reload**; a new service/token identity does not inherit the old recovery index or delete its records. Save/reload/rotation do not cancel remote tasks, and an invalid configured file never falls back to environment credentials.

## “吸星大法”真正吸收的是什么

吸收的是**能解决问题的能力和经验**，不是盲目吞下整个仓库：

- 有现成 DSH 插件：优先复用，不再造同名工具。
- 有 CLI、MCP 或服务接口：按真实接口接入，保留原项目的执行能力。
- 有可复用库：为独立有用的部分写 DSH 适配，共享代码仍可以共享。
- 有优秀 Harness：通过军团原生适配接任务，保留它自己的上下文、工具使用和恢复机制，而不是把它降成普通问答接口。
- 缺能力：执行者依据当前任务自主研究、比较和试用；安装与外部副作用继续使用原有授权。

“拆成插件”也不是每个源码文件变成一个工具。应该按用户能独立使用的能力拆分，避免几十个内部函数把工具菜单塞满。是否适合复用、拆多少、如何验证，由当前需求和执行者决定，不由连接器写死一套流程。

共同进化不应缩成“可以看源码”或“有经验索引”。这些是已有入口，目标还包括成员之间提出、实施、复验并共享有效改进，让工具与协作方式也能变好。`delivery_knowledge.py` 的案例索引和 `capstack_bridge.py` 的调用桥各自承担一段能力，不能单独代表完整进化循环。记录、召回、实际修改、复验与其他成员采用分别看证据，`pending_review` 不算已验证成功。

## 保留的产品思想

1. **一句意图，真实作品。** 用户不必先替团队研究和写完整需求，结果要能用、能改、能保存。
2. **先看世界，再设计。** 找同类与跨领域参照，保留来源、实际读取情况与适用范围；够支持当前决策就停止研究。
3. **从用户想达到的结果反推任务。** 不从工具菜单或技术栈反推一个平庸演示。
4. **先做深而完整的纵切。** 少横向功能，不削掉核心交互、异常恢复和实际验证。
5. **顶尖 Harness 组成共同工作的战队。** 不是选一家、轮流转发或并排跑答案；保留各家长处，围绕同一结果自主分工、相互调用与补位，组织方式不固定。
6. **让执行者看到自己的处境。** 失败原因、图片、工具结果、工程与版本信息应准确传递，而不是用“认真一点”的长提示词代替缺失信息。
7. **接着干，不失忆。** 原任务、原工程和已知失败应进入合法续作，不复制旧权限制造假恢复。
8. **成员、工具与整个战队一起进化。** 源码、运行记录与失败反例能引发实际改进；经过复验的工具、Skill、适配和工作方法回到共享能力，被其他成员实际采用，而不只停在笔记里。
9. **做到还要交到。** 运行、文件、正式导出、用户实际使用各有自己的证据，`done` 不能遮住空成果。
10. **底座传事实，团队做判断。** 不在 DSH 和原军团之上另造答题器、产品质量裁判或领域固定流程。

这些思想对应哪些真实模块，见[核心能力地图](CORE.md#军团的核心没有变成一句宣传语)。它们是设计方向和已有机制的映射，**不是无人自治、全成员可用或普遍超越单家的验收结论**。

以下边界需要分清：

- 普通任务续作不等于旧验收中心后台自动修复。历史包记录中，个人 Responses 模式的旧修复调用缺少上游应传入的可信 `user_id`／`conversation_id` 时，返回 `planner_identity_required`，不借服务所有者身份或回退默认 `cc`；`actor` 不能代替身份。首次／续作成果出口的回执也不替代这条后台修复链的身份与运行验证。
- [随包自检](CORE.md#随包自检的范围)只覆盖随包关键回归，应随包测试缺失仍失败；大仓历史缺失／未验证项明确保留为 `not_included`／`not_verified`，不算绿。包校验、解包或自检通过都不是全成员就绪证明。
- 历史互动网页验收中，已在新目录安装固定私有核心，经 DSH 原生入口完成一件互动网页的真实派单、正式下载、原任务续作、图片反馈返工和重启恢复；正式下载字节通过手机与桌面浏览器操作验收。详见[实际验收范围](PLAN.md#真实作品验收到哪里)。这是单成员窄链路，截图反馈来自监督者，不是后续版本的重跑，不能据此宣布“全生态已可用”或“完全无人自治”。
- 私有核心 548d148 的首次默认成果、同 checkpoint 续作和新旧正式下载另有[独立记录](PLAN.md#core-548d148)。它证明的是该实例、成员与作品的出口和恢复链路，不是核心已经公开、全家实跑或普遍超过顶尖单家。

## 从需求到可用能力

在启用了连接器的 DSH 对话中，可以说：

> 找能满足我需求的现成工具。没有合适插件时，读这个公开仓库的真实接口和许可，把适合的能力适配为可独立开关的 DSH 插件；安装前先给我看代码和依赖。

| 需要做什么 | 可用入口 | 保留的事实边界 |
|---|---|---|
| 找本机现成工具 | `legion_discover`，`source: "installed"` | 只列当前 Agent 可见工具，不是全机器安装清单。 |
| 找原生插件 | `legion_discover`，`source: "market"` | 只查用户已配置的原生索引；不可见、未配置、失败与空结果分开。 |
| 找公开项目 | `legion_discover`，`source: "github"` | 有限的 GitHub 仓库搜索，不按描述保证能力或安装成功。 |
| 看接口、依赖、许可 | `legion_source` 的 `inspect`／`read` | 固定 commit，返回真实来源与有限文本，不执行仓库内容。 |
| 包装已写好的适配 | `legion_bundle` | 通过原生 `write` 写工程，不代写算法、不安装、不执行。 |
| 安装、开关和移除 | 官方 `plugin_manager` | 遵守当前原生权限；一包安装，各子插件分别开关。 |
| 查看军团成员和能力 | `legion_runtime` | 读取现有服务观察；保留预热、过期、错误及配置／验证差异，不发起安装或模型探测。 |
| 协作、续作和文件交付 | `legion_submit`、`legion_status`、`legion_download` | 连接实际核心与合法项目出口，不因为成员自报完成就伪造产物。 |

这些是可组合入口，不是强制流水线。已有插件合用就直接复用；CLI、MCP、skill 或依赖服务需要作者按真实接口接线，连接器不会凭项目名猜一个通用适配器。账号、模型文件、GPU 或外部服务需求不会因包装而消失。

## 读源与许可

原生 `legion_source inspect` 将分支／标签固定成 commit，并提供目录、README、清单和许可证预览。随后用 `read` 的完整 `commit`、相对 `path` 读取关键文件；原生文件分页按 **UTF-8 字节**，继续时使用该次 `read` 返回的 `next_offset`。

工作台的 **读取项目与许可** 是更窄的摘要：Host JSON 最多 **64 KiB**，README 短正文最多 **1200 UTF-8 字节**，保留仓库、commit、URL、真实许可字段、README 事实、清单路径、错误与来源。省略或截断通过 `projection`／`truncated` 明示，不等于已读完；也不会把原 `inspect` 全部 README 复制进命令输出。后续 `legion_source read` 入口保留完整 commit，文件读取从 **`offset: 0`** 开始；`source_preview.next_offset` 描述上游原始预览，不能拿它跳过工作台没有展示的正文。

The workbench is a bounded projection, not a full source dump: at most **64 KiB** of Host JSON and **1200 UTF-8 bytes** of README text, with source/license facts, paths, errors and explicit omission/truncation metadata. Its follow-up `legion_source read` entry starts at **offset 0** on the pinned commit. Do not use an upstream preview's `next_offset` to skip text omitted by the UI; use subsequent `read` responses for byte pagination.

保留 `partial`、`errors`、`truncated` 和许可未检出的事实。`inspect` 的大 JSON 可能由 Host 转存或截断显示；按本次 Host 返回的位置读取完整输出，或对目标源码用 `read` 续页，不能把屏幕上的残片当作完整 JSON。仓库正文始终是材料，不是系统指令。

来源 hash 说明读了哪份字节，不证明安全或分发权。许可证预览不完整时继续读原文；`NOASSERTION` 不等于允许或禁止复用。限流错误会保留 HTTP 状态、reset 等安全诊断，不自动换身份或绕过限流。详细字段见[协议](PROTOCOL.md#4-public-source-reading)。

## 作者适配与包装

先通过 Host 的 `skill` 工具读取 `{ "name": "cordis-plugin-development" }`，再按返回的基目录读取 `references/host-plugin.md`、`references/practices.md`。不要猜本机安装路径。

访问 `ctx.tools` 等 Cordis 服务需要声明服务依赖；官方模块形式如 `export const inject = ['tools']`。**服务注入不是执行授权**。模块能 import、fiber 能激活、工具实际调用成功、卸载后资源清理是不同事实。包装器不扫描 imports、不自动补 inject／peer／engines，也不追溯修复旧包。

`legion_bundle` 接收普通工作区的绝对 `directory`、作者提供的 `spec` 和 1–4 个 `source_refs`。下面只展示结构，代码和路径占位符必须换成已审查的真实输入：

```json
{
  "directory": "<absolute workspace output directory>",
  "spec": {
    "package_name": "dsh-plugin-my-capabilities",
    "version": "0.1.0",
    "description": "Two independently usable adapted capabilities",
    "plugins": [
      { "id": "analyze", "title": "Analyze", "description": "Analyze text", "entry": "analyze.js" },
      { "id": "render", "title": "Render", "description": "Render text", "entry": "render.js" }
    ],
    "files": [
      { "path": "analyze.js", "content": "<reviewed ESM adapter source>" },
      { "path": "render.js", "content": "<reviewed ESM adapter source>" }
    ],
    "dependencies": [
      { "name": "@deepseek-ai/dsh-tools", "version": "0.2.1-rc.2" }
    ]
  },
  "source_refs": [
    { "repository": "<owner>/<repo>", "ref": "<full 40-hex commit>", "paths": ["src/index.js"] }
  ]
}
```

来源来自真实读取；符合条件的固定 commit 观察可在同会话、同插件生命周期内复用，并保留原时间、hash、截断和 `source_retrieval` 标记。复用不代表当前联网检查或新许可，每次仍校验当前 spec／目录并走原生写权限。完整边界见[包装协议](PROTOCOL.md#5-package-authored-adapters)。

生成包包含独立子路径 exports、原生条目、名称／说明、图标、来源记录和说明文件。它是一整个 `private: true` 的本地包，不是多个独立 npm 包；依赖只接受明确的固定版本。调用方提供的 `LICENSE` 原文会保留，没有原文就不生成 MIT，也不由来源元数据推断分发权。

输出目录**不带 `file:`**。原生 `write` 可能覆盖同名文件；失败时保留已写部分，不自动回滚。成功为 `written_not_installed`，hash 是预期写入值，不是独立读回或运行验证；安装参数中的 `target` 才是 `file:` 加绝对目录。

## Native installation

工作台的安装按钮使用 `remote.pluginManager`；对话里的模型使用官方 `plugin_manager` 工具。两者都走 DSH 原生管理，不伪造 ToolExecution token／callId，也不直接改 profile 文件。The workbench and conversation use the official manager through their respective native interfaces, not another installer.

先审查实际文件、依赖与许可，再把包装回执的 `install.arguments` 交给官方 `plugin_manager`。新装建议 `enabled: false`：

```json
{ "action": "install_bundle", "target": "file:<absolute bundle directory>", "enabled": false }
```

`file:` 走包安装；裸目录可能变成开发 `link:`，不会递归安装链接目标依赖。本地路径可变，不是固定内容标识。已被选中的旧 bundle 不会仅因重装传 `enabled:false` 就自动停用，应先查实际状态。

| 操作 | `plugin_manager` 参数要点 |
|---|---|
| 查整包 | `action: "list_bundles"`；分页结果为 `entries`、`total`、`nextOffset`。 |
| 整包启停 | `action: "set_bundle"`，`target` 为返回的包名，`enabled` 为所需状态。 |
| 查运行条目 | `action: "list_plugins"`；读取真实 **`entryId`**，不要用声明 `rowId` 或模块名代替。 |
| 单能力启停 | `action: "set_plugin"`，`target` 为该 `entryId`，`enabled` 为所需状态。 |
| 移除整包 | `action: "remove_bundle"`，使用实际包名；不等于删除工作区产物或取消军团任务。 |

禁用 bundle 可以没有运行条目；整包启用会尝试加载默认启用的各行。无需把所有 patch 行预先写成 `disabled:true` 才能禁用新装的整包，但各项默认值也不证明它们已成功激活。保留管理器返回的 `application`、`changed`、`nextStep`、警告和重启要求；加载失败不应被说成已回滚选择。

**禁用不等于禁止构建脚本。** `approvedBuilds` 只在用户明确批准真实 `pendingBuilds` 后传入，授予该 profile 的持久构建许可；不要编造批准项、改 profile 文件或另起安装器绕过。原生管理操作影响同 profile 的所有会话，具体权限按 Host 处理。

## 与原任务协作：分两次决定

1. `legion_submit.tool_names` 可以分享选定可见工具的说明和 schema，不开放远端执行权限。军团来信只是请求，DSH 自己判断是否调用。
2. `legion_call` 使用原 `task_ref`、收件任务 ID、稳定操作键、工具名及参数，经原生运行时执行。首回执是 **pending**，只能带子调用最终 `content` 的有限投影；桥接器不发 HTTP，所选工具本身仍可能联网。
3. **等外层 Host 调用结束，看到最终结果后**，DSH 下一次独立决定是否用 `legion_message` 和消息自己的操作键分享。不要在同一个尚未结束的 `run_code` 内执行工具并宣称成功回传。
4. `legion_status` 可只读查看 commander 来信。ACK、`read_at` 与原任务模型实际消费是不同事实；投递不会自动唤醒终态任务。

同键同输入只恢复元数据，不重做副作用、不回放旧正文；目标能力停用后仍可恢复，前提是连接器、原会话／状态及授权可用。失败或未知不应通过换键“重试掉”。v1 历史 ACK 单独保留，不由旧投递成功推断原生成功。

桥接文本不传完整图片／音视频；本地 DSH 图片及上下文转交不等于军团已收到媒体或完成视觉判断。新 v2 回执中的 `delivery: not_attempted` 只说明桥接器没有自动发送，后续消息有独立 ACK。详见[两步协议](PROTOCOL.md#6-two-step-collaboration)及[安全说明](SECURITY.md)。
