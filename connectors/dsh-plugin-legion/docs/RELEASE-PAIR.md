# 一个产品、两个包、一份配对记录

军团核心与 DSH 连接器作为**同一个 Harness 协同进化产品**维护。核心承载战队协作、共享能力与持续改进，连接器保留 DSH 的原生入口、身份、工程接续和正式成果下载。分成两个包是分发边界，不是把产品缩成工具目录。

<a id="v060"></a>

## 0.6.0：同一主库来源的下一对包

连接器源码现在位于 `meyo-legion-core/connectors/dsh-plugin-legion/`。
主库以 Git subtree 导出原插件包根；初次导出已精确回到原连接器提交，文件树与历史均保留。
后续更新只在主库编写，发行镜像不独立实现另一套功能。

连接器源码版本为 **0.6.0**，配套核心拟定为 **0.2.0**；本节不宣称 tag 或 Release 已创建。
最终固定提交与两份原包通过下方 `--connector-source-path` 核验，并形成新的 `pair_id`。
本版的安装、真实任务、正式下载和公开渠道回执，必须与这份新配对对应。
下方 0.5.2 的 34/34、42 文件包及回归数量只保留其原身份，不能移作本版证据。

原冻结 **676 格**仍属于其明确记录的被测核心、原生成员和运行条件。
后续停止事实与超时数据修复、维护源迁移都不改写该矩阵；没有本版实跑证明的效果不填分数。
同源打包与 CI 协议测试不等于无人自治、全成员就绪或普遍超越单家。

<a id="v052"></a>

## 0.5.2：本地验收与发行身份分别核对

**本地验收快照：2026-11-01。** 连接器 **0.5.2** 的源码基线为
`124d1d4379f9c56af61014b1cba0f13e03879e7e`。用于公开发行的纯净核心提交为
`52f2e9628cceee364b4fef05d60af126b11a4c68`，运行时文件树与被测 `1741b92` 一致。
这几个提交保留各自身份，不混作同一个 commit。纯净核心只继承原公开快照，
不把包含私有验收材料的维护历史推到公开仓库。核心与连接器仍各自打包，
由本页的配对记录绑定具体提交、归档和验收回执。

**本版主题：实际成员归属、预算／失败事实和正式交付贯通。** 实际派到谁，就同步谁的项目记录；
首脑提前完成也保留原生结果、文件、用量和续作位置；预算已耗尽、暂被占用、
成员失败与用户取消分别记录。把这些现场交还原生成员，让顶尖 Harness 继续协作和改进，
不是在上方另造答题器或质量裁判。

| 本地已验项 | 本次实际范围 |
|---|---|
| 回归 | 核心 **4078 项通过、6 项跳过**；连接器 **756 项通过**。跳过项不计作通过。 |
| 真实执行与出口 | **9 个真实单体文件预检**；`pk`、`relay`、`orchestrate`、`auto` **4 种 HTTP 模式**走正式交付出口。 |
| 原生安装与旧现场 | 0.5.2 安装文件 **34/34** 已核对并启用；原 **6 个任务、身份与配置**保留，**1263 字节旧成果**仍可正式下载。 |

**以上是 0.5.2 的历史基线，不是维护源合一候选的验收。** 后续连接器提交
`8a6ffcd0b20be2bca6702f685c7d423a71fe4af0` 已由原 npm 打出 **42 文件** TGZ，
复用上述 `52f2e96` 核心包，配对身份为
`sha256:6d6861d5fb88eba0b4b27146965ac8e57bd5b5cebcb39d7c52709e000fbfc189`。
该基线的连接器全量为 **757 项通过、零 skip**，绑定原生下载 fixture 已通过；
**尚未进行该新包的安装或发布**，不能沿用 34/34 安装回执。

**未完成的部分照实保留。** 完整固定样本 **52 × 13 = 676** 仍在跑，原 GAIA、BrowseComp、
搜索与编程样本没有被后加十类替代；不是官方题库全量，本节不填新分数或提分幅度。
Superagent 仅 **3 个原生只读管理动作**实跑，真实接单账号未接，不作为第十个答题模型；
Trinity 仅为待测候选。这些验收不证明所有能力就绪、无人自治或世界第一。
0.5.1 录屏与协作案例继续归属[原版本记录](PLAN.md#v051)，不挪作本版新证据。

**公开发行看渠道回执，不从本地验收推断。** 本节不是远端状态查询：是否已 push、标签与 Release
下载是否可用、npm 是否发布、Discover 目标与市场审核是否完成，均以对应渠道实际回执为准；
缺回执的项目标“待验证”。这不改变源码的公开分发形态或已有本地验收事实，也不覆盖旧标签与原包。
安装时按[0.5.2 渠道核验指引](MARKET.md#v052)确认最终目标；文档或封包字节变化后，须重新绑定最终包身份。

## 新维护方案：一个主库维护，两个入口发行

已采纳：公开 **`meyo-legion-core` 是唯一维护源**，连接器源码进入
`connectors/dsh-plugin-legion/`；现有 `dsh-plugin-legion` 仓库保留为单向同步的发行入口，
继续提供原包根、Git 安装地址和历史版本，不再两边独立改代码。

- 复用 Git subtree、原核心打包器、npm 和 DSH 原生安装器，不另造 exporter、runner 或质量裁判。核心归档仍对应**完整主库树**；连接器子树导出到独立 Git 根后，仍由原 npm `files` 清单决定 TGZ。
- 主库子树、单向同步和 CI 由维护主线程接线；下方新增参数只验证传入的固定提交，不执行迁移、同步或 CI，也不证明远端发行已完成。
- 新维护候选拟定**连接器 0.6.0／核心 0.2.0**，尚未打 tag；本次配对参数实现不修改包版本。最终版本、提交、包与安装验收另行固定，不继承上方 0.5.2 的回执。
- 公开历史仍只接审查过的纯净提交，不因工作树相同就合入含私有 QA 的父历史。本机共享 skill 的目录软链接应随迁移指向主库的 `connectors/dsh-plugin-legion/skills/legion-dsh-release/`；由维护主线程更新并读回，不另改一份技能副本。

## 薄入口做什么、不做什么

[`scripts/release_pair.py`](../scripts/release_pair.py) 只接收已经由原入口生成的两份包、完整提交、可信 SHA-256 和原 `npm pack --json` 回执：

1. 用调用者明确指定的**本地可信核心打包器**核验核心 TAR；复用它的有界、禁网络 Git 查询，逐文件对照指定提交。不会从待审核归档中执行打包器。
2. 核 TGZ 摘要、原 npm 回执的摘要／大小／完整文件集，逐文件核 Git blob、模式和字节；检查原生 main / exports / bundle patch 的目标仍在。不复制核心、不重做 npm 的文件选择。
3. 生成一份私有 JSON：两边各自的 commit、Git tree、归档 hash、版本来源、安装格式、工具身份，以及共同的 `pair_id`。**同一批次不是两个包必须同版本号或同 SHA。**
4. 可收录绑定同一 `pair_id` 的验收回执及其 hash。错包、错提交、旧配对回执被拒绝；回执中的 `failed` 和 `not_verified` 原样保留，不变成“整体通过”。

<a id="source-provenance"></a>

### 可选的主库来源校验

提供 **`--connector-source-path connectors/dsh-plugin-legion`** 时，使用原 packager 的安全 Git/path 入口：
读取 `--core-commit` 中该目录的完整子树，去掉这一明确目录前缀后，与 `--connector-commit` 的仓库根逐项核对
**完整相对路径、mode、blob、size 和 tree ID**。包括未进入 npm 包的源码文件，不只比版本号或 TGZ 文件。
绝对路径、`..`、`.`、空路径、非规范分隔和非目录目标会被拒绝；失败不降级为“未请求校验”。
读取的是指定 commit，不是当前 HEAD 或工作区文件；原有两份归档、摘要与 npm 回执核验全部保留。

结果写入原 pair JSON 的 `source_provenance`：

- 已提供并通过：`status: verified`，记录 `core_commit`、`connector_source_path`、
  `core_subtree_tree`、`connector_commit`、`connector_tree`、`files`，方法为 `core_subtree_equals_connector_root`。
- 未提供：`status: not_verified`，原因 `connector_source_path_not_supplied`；保留旧独立仓库用法，不能据此宣称来自唯一维护源。

这些来源事实只含不可变身份和仓库内相对路径，不加入本机 checkout 绝对路径；它们不证明安装、模型能力、自主进化或公开渠道已同步。

**退出 0 只表示配对记录已完整写出。** 入口不打包、不联网、不安装、不运行测试或模型、不更新版本、不推送／发 PR／建 release／合并，也不评判作品质量。没有验收回执仍可准备，但验收为 `not_verified`；没有“准备成功就准予发布”的总开关。

核心没有独立的包版本字段：只有所选提交恰有唯一 `vX.Y.Z` 标签时才记录对应版本，否则 `version: null`，保留完整提交，不借旧 `v0.1.0` 给新维护提交命名。连接器版本从包内 `package.json` 读取，同时列出所选提交实际拥有的标签。**同版本号但不同源码／归档的候选不能覆盖旧发行附件或复用旧回执。**

不提供来源参数时，`pair_id` 保留原来的两份不可变源码／归档身份算法；提供并验证来源时，将上述 `source_provenance` 一起纳入身份。
启用来源校验或换相对来源路径后，不能借旧 pair 的验收回执。绝对本地目录、标签别名和后来追加的回执仍不参与计算。
即使 tree 相同，也不能把不同 commit 混写或把私有历史推过去。

入口不替代安全审查：hash 不是作者签名，路径检查不是内嵌秘密扫描，许可字段不是权属认证，版本数字也不证明协议兼容。供应的 npm 回执和核心校验器必须来自可信准备过程。JSON 含本机绝对路径和私有验收信息，**不是可直接上传的宣传／市场文件**。

## 沿原入口准备

以下是维护命令模板。`CORE`、`CONNECTOR`、`OUT` 都应是本次确认的绝对路径，输出目录在两个源码仓库之外；使用已经安装、符合包要求的 Node 和 Python 3.12，不在这一步安装依赖。源码先提交，再冻结完整 SHA，打包期间不并发修改源码。

```sh
CORE_COMMIT="$(git -C "$CORE" rev-parse HEAD)"
CONNECTOR_COMMIT="$(git -C "$CONNECTOR" rev-parse HEAD)"
mkdir -p "$OUT"

# 原核心打包器：默认私有，不因配对取得公开许可。
python3.12 -I -B "$CORE/scripts/package_core.py" build \
  --repo "$CORE" --commit "$CORE_COMMIT" --output "$OUT/core.tar"

# 原 npm 打包器：禁 lifecycle scripts、禁联网；不是 npm publish。
(cd "$CONNECTOR" && npm pack --json --ignore-scripts --offline \
  --pack-destination "$OUT") > "$OUT/npm-pack.json"

CONNECTOR_ARCHIVE="$OUT/$(node -e \
  'console.log(JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"))[0].filename)' \
  "$OUT/npm-pack.json")"
CORE_SHA256="$(cat "$OUT/core.tar.sha256")"
CONNECTOR_SHA256="$(shasum -a 256 "$CONNECTOR_ARCHIVE" | cut -d ' ' -f 1)"

python3.12 -I -B "$CONNECTOR/scripts/release_pair.py" \
  --core-packager "$CORE/scripts/package_core.py" \
  --core-repo "$CORE" --core-commit "$CORE_COMMIT" \
  --core-archive "$OUT/core.tar" --core-sha256 "$CORE_SHA256" \
  --connector-repo "$CONNECTOR" --connector-commit "$CONNECTOR_COMMIT" \
  --connector-archive "$CONNECTOR_ARCHIVE" --connector-sha256 "$CONNECTOR_SHA256" \
  --connector-pack-report "$OUT/npm-pack.json" --output "$OUT/pair.prepared.json"
```

上面保留旧独立仓库命令。主库已包含连接器子树、连接器发行根已由该子树生成时，在同一配对命令中增加
`--connector-source-path connectors/dsh-plugin-legion`。`--core-repo` 与 `--connector-repo` 仍必须各是 Git 仓库根，
不要把主库子目录冒充 `--connector-repo`。这只核来源事实；主库与 CI 是否接好、两份最终包是否完成验收，仍看各自回执。

已有包、摘要和配对记录不覆盖；重试选择新输出目录。原核心打包器拒绝脏树时不替别人清文件。核验入口只读显式提交的 Git 对象，不会把随后工作区里的未提交修复混进已冻结的归档。

显式公开核心仍使用原打包器的 `--distribution public-source` 及许可参数，并从**审查过的纯净公开提交**构建；它的许可、`rights_verified`、`member_execution_verified` 原样进入配对记录。不因当前树不含 QA，就认定维护历史可以直接公开。

## 离线测试与原入口下载回执

```sh
cd "$CONNECTOR"
LEGION_CORE_PACKAGER="$CORE/scripts/package_core.py" \
  node --test --test-concurrency=1 test/release-pair*.test.js

# 原 runner 的全量测试，同时为这对真实归档生成限定范围的原生回执。
LEGION_CORE_PACKAGER="$CORE/scripts/package_core.py" \
LEGION_RELEASE_PAIR_FILE="$OUT/pair.prepared.json" \
LEGION_RELEASE_PAIR_RECEIPT="$OUT/native-offline-receipt.json" \
  node --test --test-concurrency=1 test/*.test.js
```

- 配对反例使用临时 HOME／MEYO／LEGION、微型 Git 仓库、原核心打包器与原 npm pack；禁 lifecycle scripts，不接触个人实例。未设置 `LEGION_CORE_PACKAGER` 时，跨包集成测试明确 **skip**，不假装已经核验核心。
- 原生测试使用**已安装的真实 DSH 兼容性检查器、Cordis、ToolRuntime 和 CommandRuntime**，不是自己写一个兼容版本裁判。
- 下载使用现有 `test/fixture.js` 的本地 HTTP 协议替身，真正走 `legion_download` 和原会话 `/legion download`，检查根路径／反向代理前缀、`?download=1` 原始字节、落盘读回、损坏摘要拒绝、跨会话拒绝与临时文件清理。
- 绑定配对时，在测试前后核两份归档摘要和当前连接器的全部随包文件；修改过文档也不能默认为同一包。只有这些断言成功才创建新回执。
- **该 HTTP 替身不是活核心。** 回执明确把活核心联合运行、新 profile 安装、浏览器实操、模型任务和战队协同进化留为 `not_verified`；不能拿这份绿灯替代它们。

要把回执装进同一份配对记录，重跑上面的**同一组身份参数**，增加
`--evidence "$OUT/native-offline-receipt.json"`，并将输出换成新的
`--output "$OUT/pair.with-evidence.json"`。可重复 `--evidence` 收录其他同题、同包回执；不会自动运行其中的命令。旧配对单保持不变。

## 后续发布与撤回边界

本地已验范围见 [0.5.2 快照](#v052)。获准发行时仍须把最终纯净提交、两份原受审包与匹配的联合验收回执对齐，再沿各自原渠道发布；之后读回两个固定 tag／commit、实际公开下载 hash、DSH 原安装入口与正式成果下载。两份远端 release 不是原子事务：一边未完成就如实保留“配对未完成”，不要把另一边更新了说成整体已交付。

最初薄配对工具的撤回，可按逆序回退它的连接器相关提交；这不是 0.6.0 整版回滚方案。
维护源已统一后的源码回退应在主库完成，并重新按 Git subtree 单向导出、核验和发布；
运行实例则切回保留的原核心／连接器制品配对，另核身份、会话和正式成果。
不移动旧 tag，不覆盖旧包，不强推镜像，不把源码回退说成正在运行的实例已回退。
