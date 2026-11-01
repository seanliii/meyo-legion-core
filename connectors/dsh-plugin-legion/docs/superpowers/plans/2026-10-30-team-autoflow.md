# 战队自动工作流程实施计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development 或 executing-plans。
> 本计划已由用户确认产品顺序，并明确要求建立流程后自动开启；无需再问是否开始。

**Goal:** 在 DSH 原生工作台完成组队后，自动启动并持久安排协作、发现与自我改进任务。

**Architecture:** 扩展已有连接器配置、工作台命令和原生 DSH schedule，不改造新的 Agent。
成员协作要求进入原核心的用户上下文；任务投递和会话由 DSH 管理，结果可回到原会话。

**Tech Stack:** 当前固定 DSH `0.2.1-rc.2`、Node 24、ESM、Host 提供的 React、原 Python 核心。

**Spec:** `docs/superpowers/specs/2026-10-30-team-autoflow.md`

## Global Constraints

- 使用现有独立连接器的 `codex/legion-team-autoflow` 分支；不改 mars-data 的脏工作区。
- 新 `teamProfile` 不改变未配置旧用户行为；完成向导才安排首次任务，不在 import 时派单。
- 首次与定期任务只走 DSH `schedule`；不增加定时守护进程、独立模型调用或新运行数据库。
- 候选、协作偏好与进化指令是 Context，身份／授权／取消仍由原 Host 和核心处理。
- 所有示例为当次配置，不宣称已具备所有模型或成员；结果、计划和效果分开显示。
- 测试使用专用 HOME、MEYO_LEGION_DIR、LEGION_HOME_DIR、TMPDIR，不复制真实凭据。

## 1. 可持久保存的战队配置与原提交接线

**Files:** 新增 `lib/team.js`、`test/team.test.js`；修改 `index.js`、`lib/client.js`；
关联测试 `test/plugin.test.js`、`test/settings.test.js`、`test/client.test.js`。

**接口：**

```js
validateTeamProfile(value) // undefined | 规范化的 teamProfile；拒绝未知字段
teamContext(profile, candidates) // 明确标注为用户配置的有界说明
```

- [x] 红测：配置默认进化为 true、枚举和文本校验、旧 config 无变化、leader 与候选关系。
- [x] 红测：真实 HTTP submit 收到队伍上下文；原始 query、项目绑定、候选和恢复身份不丢。
- [x] 实现上述接口；已配置团队进入 bindingDigest，旧版无团队的 digest 不变。
- [x] 跑定向测试并提交该阶段（`f1e684c`）。

## 2. 原生自动流程

**Files:** 新增 `lib/team-flow.js`、`test/team-flow.test.js`；
修改 `index.js`、`lib/workbench.js` 和对应命令测试。

**接口：**

```js
createTeamFlow({ getSchedule, profile, candidates, lifecycleSignal })
// { status(execution), start(execution), pause(execution), run({key}, execution) }
```

结果包含 `configured`、`enabled`、`schedules`、`observations`；每项只声明
原生安排／投递事实，保留 schedule ID、next time 和 lastDelivery，不声称模型完成。

- [x] 红测：缺 schedule 真实报错；默认建立首次及每周任务；manual 只有首次。
- [x] 红测：重复启动、同 key 再跑、并发调用和重载后恢复不重复；不同会话互不影响。
- [x] 红测：暂停只处理自有标记的活动任务（包括逾期未投递）；投递后不声称撤回；取消与部分失败读回。
- [x] 接线 `flow_status/start/pause/run`，保持附件、字段、会话身份及输出边界。
- [x] 用官方 `ScheduleService` 在隔离环境测试真实创建／投递／持久化，不只模拟服务（后端阶段 `e6ec9bb`）。

## 3. 组队首页与自动开启交互

**Files:** 修改 `client/index.js`，添加 `test/team-browser.test.js`；
复用 `test/browser-fixture.js` 和既有前端 controller。

**Controller 新接口：**

```js
loadFlow()              // 原生 flow_status，随后只恢复已持久保存的未完成用户操作
startFlow()             // 已保存配置的 flow_start
pauseFlow()             // flow_pause
runFlow(key)            // 同 key 可恢复的 flow_run
// state.flow、busy/errors.flowStatus/flowStart/flowPause/flowRun
```

- [x] 红测：`configOperations` 支持 `teamProfile`；flow 操作使用当前会话，迟到返回不串会话。
- [x] 组队首页三步：战友候选 → 协作方式 → 默认进化／周期与目标。
- [x] 最终按钮保存队伍配置和成员，再自动安排；失败保留输入、可核对原记录，不重复创建。
- [x] 已组队展示原生下次任务、实际投递、刷新、再跑、暂停及打开原会话。
- [x] 添加战友／装备继续使用已有来源检查与接入界面；配置入口辅助而非挡住主线。
- [x] 保留五个旧面板功能、原生主题／无障碍与窄屏；不新增花哨重动画。

## 4. 真实 Host 验证与收尾

- [x] 组合回归与语法检查；693/693 通过，package 不含私有运行数据。
- [x] 在专用 DSH Host 安装同一新包，完成向导并核对原生 schedule 持久记录／实际投递；最终安装文件 32/32 与包一致。
- [x] 真浏览器操作与逐张截图；真实缺少模型凭据不显示成执行成功；390px CSS 窄视口不冒充手机真机。
- [x] 读回真实模型可用条件：独立实例未配置 API Key，原生记录 `MISSING_CREDENTIAL`；没有成功模型改进，不借用其他账号补绿。
- [x] 重启后读取同一个队伍、schedule 和会话；配置／调度存储字节一致，原首次任务不重复。
- [x] 独立审查的具体反例已修并重放；复用既有发布验收技能，新增 `docs/TEAM.md` 使用说明，并分阶段中文提交。
- [x] 交付新版界面及事实账本；只对本次流程是否建立作结论，不外推全生态自治效果。

## 收尾事实与范围

- 用户原话将本次收敛为“建立流程即可”。0.5.0 是本地开发安装包，未推送 GitHub、
  未修改旧发行标签或 Discover 条目。
- 最终包的源码语法、693 项组合测试、原生安装，以及真实保存／投递／暂停／
  关闭页面恢复／Host 重启均已核对。已有包 `0.4.2` 的事实保持独立。
- 真浏览器故障注入仅丢弃实际 HTTP 成功后的回执，不伪造成功数据：
  一个发生在配置落盘后，一个发生在原生任务创建后。重开恢复同一操作，
  没有重复首次或重试任务；主动暂停后刷新不会复活。
- 额外审查修复：旧会话计划与全局默认配置分开显示；周期缺失不被刷新洗成成功；
  逾期未投递计划可暂停；新鲜 settings 读回不受旧缓存错误否决，并保留文档级可写事实。
- 原始运行资料留在忽略的 `.qa/team-autoflow/`，总核验为
  `FINAL-VERIFICATION.json`。它不进入公开安装包，不将测试次数当能力分数。
- 尚未证明／不在本次范围：真实成功的模型改进、全成员协作与改进传播、普遍行业胜出、
  模型权重训练和新的公开发行。定时工作仍要求原生 DSH Host 运行及本人模型可用。
