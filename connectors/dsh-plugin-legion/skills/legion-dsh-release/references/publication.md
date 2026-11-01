# 固定源码、发行包与投稿

按需对照连接器 `docs/MARKET.md`、`docs/SECURITY.md`、`docs/PLAN.md`，以及核心 `docs/core-private-package.md`、`docs/public-core-release.md`、`scripts/package_core.py`。版本、渠道规则和审核状态从当前现场读取，不用历史记录冒充现状。

## 1. 先确定能公开什么

- 连接器、独立核心、运行实例、生成成果分别处理。只公开本次明确获授权的第一方代码和文档；选定共享工具也须核清权属，不因位于同一仓库就一并授权。
- 第一方许可不覆盖第三方。保留适用的 LICENSE、NOTICE、版权和其他分发条件原文；连接器采用 MIT 不会把外部依赖、原生成员或可选媒体项目变成 MIT。
- **在建立公开初始 snapshot 之前剥离私有内容**：私有历史、实例、凭据、默认密钥及回退配置、个人实测内嵌数据、会话／日志、截图里的私人信息与无关业务代码。检查程序、测试、示例和 Markdown 内嵌内容，不能只靠文件名过滤。普通路径、公开域名、版本与源码 hash 不自动等同于秘密；有用的来源标识和程序接口按真实上下文保留。
- 公开初始提交应来自已审查的纯源码快照，不携带私有仓库的 `.git` 或私有父历史。检查将推出的分支、标签及全部可达历史；只在新 HEAD 删除秘密、却仍推出旧提交，并没有清理干净。不要通过推送全部 refs 夹带私有历史。
- 保留有用的程序模块，不按 `token`、`state`、`runtime` 等名字批量删源码。打包器的路径检查不是万能秘密扫描器，许可字段也不是权属认证。
- 若敏感材料已经公开，准确报告暴露面，按授权处理撤销／轮换和历史清理；不擅自强推或把删文件说成秘密已撤回。

## 2. 冻结一个发行身份

记录完整 commit、版本／tag、原 TGZ 或 TAR、SHA-256、大小、文件清单与实际兼容环境。tag 要解析到 commit；安装目标不得悄悄从固定提交改成 `main`。

连接器沿现有构建和打包入口，核心沿 `scripts/package_core.py`，不另造 exporter。包中程序、许可、文档及清单均属于发行字节。源树、包、匿名下载和安装后的源文件要逐一对应：

- 按本次真实文件集核对路径和内容，报告 `N/N`。历史的 **29/29** 只是数量示例，不是永久要求；文件数量相同也不能代替逐文件 hash 一致。
- 安装器生成的元数据或依赖目录单独说明，不能混入源文件数量，也不能忽略应随包文件的差异。
- 发布原受审附件和对应摘要。保留 old tag、旧 TGZ／TAR 及原回执；不能重新打包覆盖旧附件或沿用旧 hash。
- 发行后 `main` 的后验文档单独记账，不移动旧 tag。即使程序没变，新文档也会改变包字节；不能把重包伪装成原发行。
- 连接器与核心各有自己的版本和证据；一次联合测试不能消除两者的身份区别。

## 3. 核心包：默认私有，公开需显式声明

| 格式 | 必须保留的语义 |
|---|---|
| `meyo-private-core-v1` | 默认 `distribution: private-local-only`、`public_license_granted: false`、`member_execution_verified: false`；不附公开许可参数 |
| `meyo-public-core-v1` | 显式 `distribution: public-source`，包含许可标识、许可文件和第三方声明；`rights_verified: false`、`member_execution_verified: false`；**不包含** `public_license_granted` |

`rights_verified: false` 表示打包器没有认证权属，不是否定发布者已有权利。公开许可不会改写历史私有包，也不能把成员实跑标记改成 true。

以下均为命令模板。`CORE` 是已审查的源码仓库根，`FULL_COMMIT` 是已核定的完整提交；`OUT` 是源码仓库外已存在的输出目录，目标归档和摘要不得覆盖既有文件。工作树须满足原打包器的干净状态检查，不清除他人的改动来凑条件。

```sh
# 默认私有；不自动取得公开授权。
python3.12 -I -B "$CORE/scripts/package_core.py" build \
  --repo "$CORE" --commit "$FULL_COMMIT" --output "$OUT/core.tar"

# 仅在本次公开范围与许可已明确后使用。
python3.12 -I -B "$CORE/scripts/package_core.py" build \
  --repo "$CORE" --commit "$FULL_COMMIT" --output "$OUT/public-core.tar" \
  --distribution public-source --license-id "$LICENSE_ID" \
  --license-file "$LICENSE_PATH" --notice-file "$NOTICES_PATH"
```

许可与声明文件必须已存在于所选提交中；多份声明可重复传 `--notice-file`。不能用新 HEAD 的许可证给旧提交补授权。

从可信来源取得校验器和期望摘要，再校验匿名下载到的同一份字节；不要执行待审核包中的脚本来给它自己背书。

```sh
python3.12 -I -B "$TRUSTED_CORE/scripts/package_core.py" verify \
  "$ARCHIVE" --sha256 "$EXPECTED_SHA256"
python3.12 -I -B "$TRUSTED_CORE/scripts/package_core.py" extract \
  "$ARCHIVE" --sha256 "$EXPECTED_SHA256" --dest "$NEW_CORE"
```

`NEW_CORE` 的父目录须存在，目标须全新且不在 Git／旧核心树内；按原校验器处理路径和归档结构，不绕过拒绝叠加解包。核心产物是未压缩 TAR，邻接 `.sha256` 是纯摘要，不是 `SHA256SUMS` 那样带文件名的清单。`CORE-PACKAGE.json` 固定源码提交、路径、模式、大小、SHA-256 和 Git blob；校验通过不代表安装或运行通过。

## 4. npm、Git、Discover 与社区目录不是一件事

| 渠道 | 操作与真实边界 |
|---|---|
| 固定 Git | 不要求本插件先发布到 npm；安装依赖仍可能联网。保持原生安装、明确启用、警告和重启回执 |
| 官方 Discover | 使用已配置的可信索引与固定 Git 来源；原生页面安装和实际安装文件需验证 |
| npm | 仅在实际有登录、包名权限和本次发布授权时执行；未登录不阻塞 Git／Discover |
| 社区目录／1024Store | 各自的 schema、安装方式和维护者规则独立；browse-only、安装按钮、投稿、CI、合并、网站收录分别核实 |

固定 Git 安装模板；Host／Node 兼容版本读当前包清单，profile 使用本次独立验收实例：

```sh
dsh plugin --profile "$PROFILE" -w add \
  "github:${OWNER}/${REPOSITORY}#${FULL_CONNECTOR_COMMIT}"
```

原生安装后还须核对并明确启用。`enabled:false` 不代表禁止依赖构建脚本，也不保证更新时停用原已启用包；构建许可和 bundle／子插件开关分别处理，不绕过原生审批。

Discover 索引按当前官方格式核对：根 `format/title/plugins`，版本中的 `publishTimestamp` 与 `source`，Git 来源中的 `type/url/commit` 及可选子目录 `path`。索引应可匿名直接读取；安装来源钉完整 commit，即使索引文档位于 `main`，也不能让代码跟随 `main`。以当前 UI 找到“插件／发现／添加索引源”，缺可选市场时沿原生开关启用，不假设用户已配置。

社区投稿先读目标仓库的 schema、贡献规则和已有条目。更新已有条目而非重复新增，保留身份和首次 `added`；已知七字段为 `$schema/id/name/repository/category/description/added`，以后以该仓库实际规则为准。不要把官方 Discover 的 `source` 或自创的 npm、SHA、rights 字段塞进目录 JSON；不要连带改生成页、workflow 或审查脚本。目录自身的元数据／代码许可不重新许可插件。

无 npm 包时，1024Store 可能仅提供 browse-only。以当前机器人回执和用户可见页面确认，不概括成所有 DSH 安装都需要 npm。更新条目需要维护者审核时如实保留 pending，不能把 CI 绿、merge skipped 或 PR open 写成合入。

隔离 npm 操作时，`NPM_CONFIG_USERCONFIG` 与 `NPM_CONFIG_GLOBALCONFIG` 指向测试根内**两个不同的临时普通文件**，不能同时设为 `/dev/null`，否则可能 double-loading。不要修改用户真实 npm 配置；空白配置不继承登录，不能据此推断用户原账号已失效。

## 5. GitHub 编辑必须保存并读回

- 行动前确认当前工具策略、授权的 PR 与可修改字段。优先适用的原生工具；若 `update_pull_request` 只渲染确认表单，结果是“待提交”，不是已更新。
- 看用户实际能看到的 UI，不能让人寻找工具并未展示的 Submit，也不能把表单预填内容算成远端正文。
- 在已有授权和工具策略允许时，可改走真实原生 GitHub 编辑界面完成同一修改；保存后重新读取标题、正文或文件 diff，确认用户实际可见的结果。不要顺带合并或改其他设置。
- 浏览器 stale、连接重置与用户接管按[验收指南](acceptance.md)处理。一次工具错误不等于必须永远等待表单；恢复不成就说明具体阻塞，同时继续不依赖它的已授权工作。

交接时分别列发行附件、匿名验证、安装、Discover、目录投稿、PR 正文和维护者处理状态；未完成的那一项不能掩盖或冒充其他项。
