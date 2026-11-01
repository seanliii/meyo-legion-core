# 一个产品、两个包、一份配对记录

军团核心与 DSH 连接器作为**同一个 Harness 协同进化产品**维护。核心承载战队协作、共享能力与持续改进，连接器保留 DSH 的原生入口、身份、工程接续和正式成果下载。分成两个包是分发边界，不是把产品缩成工具目录。

## 本次决定：暂不合并仓库

| 已读源码 | 对决定的影响 |
|---|---|
| 连接器 `package.json` 的 `main`、`exports`、`files`、`dsh.bundle.patch`；根目录 `cordis.patch.yml` | 当前固定 Git 安装认现有包根。`npm pack` 已负责文件选择，没有必要改安装目标或把 Python 核心塞进 TGZ。 |
| 已安装的 DSH rc.2：`@deepseek-ai/dsh-plugin-manager` 的 `install-spec.js` / `operations.js`，`@deepseek-ai/dsh-app-boot` 的 `evaluatePluginCompatibility` | 原生管理器接受 Git/TGZ，检查 DSH 版本并处理启用、构建许可和恢复。配对记录不替代这些机制，也不提供第二个安装器。 |
| 核心 `scripts/package_core.py` 的 `build_package`、`verify_package`、`_read_tree`；`docs/core-private-package.md` | 原打包器检查**整个固定 Git 树**，不是随意过滤目录的 exporter。直接合仓会改变核心归档的范围，或要求另改打包契约，不属于这次最小接线。 |
| 核心 `docs/public-core-release.md`；维护树实际提交历史 | 公开初始历史来自纯源码 snapshot。维护历史曾提交后删除 QA；在新 HEAD 删除文件不等于清理了父历史，不能直接推到公开 `main`。 |
| 两边现有 scripts / Makefile / package scripts / CI | 没有联合发行入口。连接器已有 `node --test test/*.test.js`，因此新增测试直接被原 runner 收录，不新增工作流平台。 |

合仓以后可以单独评估，例如明确新的包子目录和纯净历史迁移；并非 DSH 永远不能用 monorepo。但本次不需要这些迁移，**配对交付先解决“两个包各自更新、证据却串版本”的实际问题**。

## 薄入口做什么、不做什么

[`scripts/release_pair.py`](../scripts/release_pair.py) 只接收已经由原入口生成的两份包、完整提交、可信 SHA-256 和原 `npm pack --json` 回执：

1. 用调用者明确指定的**本地可信核心打包器**核验核心 TAR；复用它的有界、禁网络 Git 查询，逐文件对照指定提交。不会从待审核归档中执行打包器。
2. 核 TGZ 摘要、原 npm 回执的摘要／大小／完整文件集，逐文件核 Git blob、模式和字节；检查原生 main / exports / bundle patch 的目标仍在。不复制核心、不重做 npm 的文件选择。
3. 生成一份私有 JSON：两边各自的 commit、Git tree、归档 hash、版本来源、安装格式、工具身份，以及共同的 `pair_id`。**同一批次不是两个包必须同版本号或同 SHA。**
4. 可收录绑定同一 `pair_id` 的验收回执及其 hash。错包、错提交、旧配对回执被拒绝；回执中的 `failed` 和 `not_verified` 原样保留，不变成“整体通过”。

**退出 0 只表示配对记录已完整写出。** 入口不打包、不联网、不安装、不运行测试或模型、不更新版本、不推送／发 PR／建 release／合并，也不评判作品质量。没有验收回执仍可准备，但验收为 `not_verified`；没有“准备成功就准予发布”的总开关。

核心没有独立的包版本字段：只有所选提交恰有唯一 `vX.Y.Z` 标签时才记录对应版本，否则 `version: null`，保留完整提交，不借旧 `v0.1.0` 给新维护提交命名。连接器版本从包内 `package.json` 读取，同时列出所选提交实际拥有的标签。**同版本号但不同源码／归档的候选不能覆盖旧发行附件或复用旧回执。**

`pair_id` 只取两份不可变源码／归档身份；本地目录、标签别名和后来追加的回执不参与计算。Git tree 可用于后续对照“维护提交”和“纯净公开提交”的文件身份；即使 tree 相同，也不能把二者 commit 混写或把私有历史推过去。

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

本次只准备。以后获准发行时，先完成纯净公开提交、两个包的版本选择和实际联合验收，再使用各自原渠道发布原受审字节；最后读回两个固定 tag／commit、实际公开下载 hash、DSH 原安装入口与正式成果下载。两份远端 release 不是原子事务：一边未完成就如实保留“配对未完成”，不要把另一边更新了说成整体已交付。

撤回这次接线只需按逆序回退本次连接器的配对相关提交。核心源码、旧 tag、旧包、原安装器、用户配置与运行接口均未迁移，也没有新增后台服务或持续任务。
