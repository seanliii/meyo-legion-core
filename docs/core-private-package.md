# 核心固定源码包：默认私有，可显式声明公开格式

这是一项**源码分发工具**，不是新的运行器、依赖安装器、公开发布流程或成员验收。
只调用本机 Git，使用 Python 标准库；`verify` / `extract` 不需要 Git、旧源码目录或网络。
默认仍生成原来的私有包。新增公开格式只是把发布者明确提供的许可标识、许可文件和
第三方声明与同一份源码固定在一起，**不会替发布者确认权属、选择许可证或上传代码**。
打包工具本身不作许可决定。当前第一方核心的明确许可为根目录 `LICENSE` 中的 MIT，
第三方范围见 `THIRD_PARTY_NOTICES.md`；公开发行入口与边界见
[公开核心发行说明](public-core-release.md)。旧私有包不因此被改写。

## 默认用法：私有包

需要 macOS / Linux、Python 3.11+；构建还需要本机 Git 和已经完整存在的提交对象。
以下变量请指向实际的绝对路径：

```sh
CORE=/绝对路径/私有核心工作树
OUT=/绝对路径/私有包输出目录
COMMIT="$(git -C "$CORE" rev-parse HEAD)"

mkdir -p "$OUT"
python3.12 -I -B "$CORE/scripts/package_core.py" build \
  --repo "$CORE" --commit "$COMMIT" --output "$OUT/core.tar"

python3.12 -I -B "$CORE/scripts/package_core.py" verify "$OUT/core.tar"

# 父目录必须存在，目标目录必须尚不存在，也不能位于任何旧源码 / Git 仓库中。
python3.12 -I -B "$CORE/scripts/package_core.py" extract "$OUT/core.tar" \
  --dest "/private/tmp/meyo-private-core-$COMMIT"
```

构建输出两个文件：

- `core.tar`：不压缩的规范 USTAR 包，程序源码直接位于包根目录。
- `core.tar.sha256`：整个包的 SHA256，仅一行十六进制摘要，不含机器路径或文件名。

校验默认读取相邻 `.sha256` 文件；也可以从**可信渠道**取得摘要后显式传入：

```sh
python3.12 -I -B "$CORE/scripts/package_core.py" verify "$OUT/core.tar" \
  --sha256 "$TRUSTED_SHA256"
python3.12 -I -B "$CORE/scripts/package_core.py" extract "$OUT/core.tar" \
  --sha256 "$TRUSTED_SHA256" --dest /绝对路径/全新核心目录
```

成功时返回 JSON、退出码 `0`；校验 / I/O 拒绝时错误写入 stderr、退出码 `2`，
不会输出“已运行成员”或“安装成功”。包文件、摘要文件、解包目标都不覆盖既有内容。
`--commit` 必须是完整、小写的 40 / 64 位对象 ID，不接受会移动的 `HEAD` / 分支名。
允许选择较早提交，但当前工作树仍须干净。

## 可选用法：显式公开源码格式

只有在发布者已经确定可公开的范围与相应许可后，才应选择这一格式。
许可标识、许可正文和第三方声明都必须显式提供；工具不会补一份 MIT 或沿用连接器的许可。
以下变量应由发布者明确填写，不从另一项目自动继承：

```sh
# CORE、OUT、COMMIT 沿用上面的绝对路径和完整提交。
# LICENSE_ID：本次明确选择的单个许可标识，例如发布者自己的 LicenseRef 标识。
# LICENSE_PATH、NOTICES_PATH：该 COMMIT 中已经存在的、相对于源码根的文件路径。
python3.12 -I -B "$CORE/scripts/package_core.py" build \
  --repo "$CORE" --commit "$COMMIT" --output "$OUT/public-core.tar" \
  --distribution public-source \
  --license-id "$LICENSE_ID" \
  --license-file "$LICENSE_PATH" \
  --notice-file "$NOTICES_PATH"

# 多份第三方声明可重复传入 --notice-file；校验与解包仍使用同一个入口。
python3.12 -I -B "$CORE/scripts/package_core.py" verify "$OUT/public-core.tar"
python3.12 -I -B "$CORE/scripts/package_core.py" extract "$OUT/public-core.tar" \
  --dest "/private/tmp/meyo-public-core-$COMMIT"
```

许可标识是 1–128 位的字母、数字、`.`、`+`、`-`，首位须为字母或数字；
不接受 `NONE`、`NOASSERTION`，也不解析 SPDX 组合表达式。
许可文件和至少一份第三方声明必须是**选定提交中**的非空普通源码文件，
路径彼此不同且不能重复。文件大小非零不等于许可正文正确：工具不审核正文的法律含义。
如果打包旧提交，不能借用当前工作树或较晚提交里的许可文件补齐。
私有默认模式不接受许可参数，避免把两种格式混在一起。

公开包使用 `format: "meyo-public-core-v1"`、`distribution: "public-source"`，并保存：

- `license.identifier`、`license.file`、排序后的 `license.third_party_notices`；
- `rights_verified: false`：打包器没有认证公开权属，不是认定发布者没有权利；
- `member_execution_verified: false`：源码包不证明成员已经运行。

公开格式**不包含** `public_license_granted`。私有格式保持原来的字段和语义；
校验器拒绝两种格式互相混用，以及伪造“权属已认证”或“成员已实跑”的标记。
这些是包格式的一致性要求，不是新增模型质量门槛。

旧版校验器不认识新公开格式，应使用可信安装来源的新版本，不执行待审核目录里的脚本
来替自己做校验。新版本仍能校验和解包原有 `meyo-private-core-v1` 包；
仅增加格式支持，不重写、重签或替换已经发布的旧包。

## 内容和固定字节

构建先读取提交的完整 Git 树，再执行 `git archive --format=tar <完整提交>`。
**不是根路径白名单**：已提交的 README、LOCAL-CANDIDATE、启动入口、Makefile、
`.gitignore`、release、harness_baselines、runtime、docs、deploy，以及今后新增的程序目录
都走同一个完整文件集检查。不会叠加工作副本内容、复制旧大仓或附带 Git 历史。

归档中的每个源码文件都与提交中的 Git blob 对照；缺文件、被属性替换过的字节都拒绝。
如果 `export-ignore` / `export-subst` 实际导致遗漏或替换，就明确失败，不偷偷交一个残包。
工作树和索引在构建前、发布文件前各检查一次。原始源码字节变化、暂存变化、
非忽略的 untracked 文件、`assume-unchanged` / `skip-worktree` 隐藏的变化都会拒绝。
因此 Git 换行 / clean filter 使工作副本字节不同于提交时，也视为不满足打包条件；
不会为了检查而运行这些过滤器。

包中 `CORE-PACKAGE.json` 保存：

- 格式版本、完整 `source_commit`；
- 私有格式的 `distribution: "private-local-only"`、`public_license_granted: false`，
  或上节所述的显式公开格式声明；
- `member_execution_verified: false`；
- 完整源码文件集：每个文件的相对路径、Git 模式、字节数、SHA256 和 Git blob ID。

清单自身由包外 SHA256 覆盖。所有成员的 uid / gid / mtime 固定为 0，用户名 / 组名为空，
文件模式来自提交的可执行位，顺序和 JSON 编码固定；不写构建时间、工作树路径、主机名。
同一提交、同一格式版本、同一组许可声明构建两次，包字节和摘要文件字节一致。
许可和第三方声明正文也在完整文件集中，使用相同的大小、Git blob 与 SHA256 校验，
不是包外的可替换附件。

## 精确拒绝数据，不误杀程序模块

这是“整份源码或拒绝”，不是静默过滤已提交文件：

- symlink、gitlink / submodule、非普通文件不允许进入源码包。
- `.git`、venv、node_modules、缓存、`.meyo`、`.legion`、`.qa`、`snapshot_failures`、
  凭据目录、私钥、真实 `.env`、常见身份文件、模型权重、数据库等已提交数据会拒绝。
- sessions / logs / history / results 等位置的 JSON、日志、表格等运行数据会拒绝。
- **不会按 `token`、`state`、`model`、`runtime` 字符串模糊删除**：
  `token_telemetry.py`、`qa_system/state/` 中的程序和 schema、
  `runtime/core-sources/` 等保留；`.env.example` / `.env.sample` / `.env.template` 可以保留。
- 忽略的本地数据不在 Git 提交中，不读取其内容，也不进入包。
  非忽略的 untracked 内容使工作树不满足条件；例如本地 `snapshot_failures` 未被忽略时会拒绝。
  工具不会自动删除、移动、暂存这些内容，也不改 `.gitignore`。

路径策略不是秘密扫描器：不能识别任意 `.py` / `.md` / 示例文件里硬编码的密钥，
也不通过读取真实凭据来检测。打包前仍须由源码所有者确认提交内容可以按所选方式交付。
现有第三方许可证随源码保留；这些本地事实**不新增公开许可，也不撤销既有许可证**。

## 校验与提取边界

先核对包外摘要，再在一份私有临时副本上验证清单、完整文件集、SHA256、Git blob、
大小、模式和归档结构；提取使用同一份已验证字节，避免验证后路径被替换。
不调用 `tar.extractall`，不支持压缩包、PAX、GNU 扩展或目录 / 链接 / 设备成员。
重复路径 / 重复 JSON 键、穿越、绝对路径、反斜杠 / 驱动器路径、大小写冲突、
文件与目录冲突、非规范 Unicode 路径、非零填充、截断、尾随 / 拼接包都会拒绝。

提取先完成所有校验，再原子创建一个全新根目录；使用目录描述符和 `O_NOFOLLOW` /
`O_EXCL` 写入。目标的父路径不能经过 symlink，任何祖先存在 `.git`、
`CORE-PACKAGE.json` 或 `meyo.py` 时均拒绝，避免把新包叠进旧工作树 / 旧源码。
根目录权限为 `0700`；源码文件读回验证，普通文件 / 可执行文件分别恢复为 `0644` / `0755`。
普通写入异常会清理本次创建的目录，不删除既有目录。

固定上限：单文件 64 MiB、源码总量 512 MiB、20,000 个文件、清单 / Git 元数据 16 MiB、
包 592 MiB；大文件按最多 1 MiB 的块处理，单次 Git 命令最多 60 秒并回收其进程组。
USTAR 路径须能无损表示，UTF-8 路径最多 255 字节；不截短文件名。
构建需预留最多约三份包大小的临时空间，解包还需额外源码空间。
这些是本格式的有界资源限制，不会自动安装依赖、下载缺失对象或偷偷放宽上限。

## 验证和不保证的事情

对口测试只使用自建微型 Git 仓库，逐测试设置临时 HOME / MEYO / LEGION，
不导入成员运行时、不调用模型、不触碰个人凭据。

```sh
# 在核心根运行；临时测试目录不要放在任意 Git 仓库内部。
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3.12 -I -B -m pytest -q \
  -p no:cacheprovider --basetemp=/private/tmp/core-distribution-tests \
  release/legion-service/tests/test_core_distribution.py \
  release/legion-service/tests/test_source_audit_core_package.py
```

覆盖重复构建字节一致、所有新目录源码纳入、脏树与数据拒绝、篡改 / 缺失 / 额外文件、
路径和链接攻击、损坏清单 / 超大声明、真实 CLI 与解包读回、无 Git 解包、异常清理。
公开格式另测：许可文件必须来自指定提交、声明参数缺失或混用、正文篡改、
原私有包兼容，以及解包后沿用原源码检索与审核账本。许可相关测试使用虚构的小型仓库，
不替实际军团核心指定许可证。
不重复运行成员、模型或全量产品测试。

局限：

1. hash 保证与**可信摘要**一致，不是签名；攻击者若能同时替换包和摘要，就不能用它认证作者。
   离线校验不向原 Git 仓库确认提交身份；提交与源码的对应在构建时核对。
2. 这是源码可搬运性，不代表依赖齐全、服务已启动、任意成员已运行、所有平台已兼容。
   安装 / 配置 / 启动仍由既有核心入口负责；本工具不提供替代路径。
3. 当前实现使用 POSIX 安全文件接口，不支持 Windows 原生运行；不承诺字节格式升级后的
   包仍与旧版本逐字节一致。
4. 源码应在无并发写入时打包。进程被强杀、断电、系统崩溃可能留下临时目录、
   不完整的新目录或只有 tar 的输出；不会假称成功或自动覆盖，处理后选择新路径重试。
5. 只验证本子项。共享工作树里其他线程尚未提交的功能不会被纳入，也不替它们提交。

## 安装后仍沿用原来的接入与自审入口

独立包没有旧大仓的全部开发检查，也没有 `.git`。这两件事不能被混同为
“成员做错了”或“源码不能审核”，但也不能被抹掉并宣布完整通过。

### 成员接入报告

`release/legion-service/harness_onboard.py` 继续承担登记、安装和接入检查。
报告并列保存每环的 `status`、`reason`、说明和耗时：

- `PASS`／退出码 `0`：本次适用检查全部执行并通过。
- `FAIL`／退出码 `1`：存在真实失败，保留失败环节。
- `PARTIAL`／退出码 `2`：没有失败，但仍有适用检查未运行。

例如，独立包没有历史 `dev/benchmark_execution_modes.py` 时，该环是
`SKIP`／`not_included`，前面执行过的结果仍在，总报告为 `PARTIAL`。
使用者主动跳过记为 `requested_skip`；不适用于该成员角色的检查记为
`not_applicable`，后者本身不让结果变为部分完成。
历史模式检查存在时，仍走原有带进程登记和超时回收的执行入口；每次使用新的
输出目录，不能拿上次留下的报告当本次结果。

这只是准确传递检查事实，不是新的模型质量裁判。用替身检查报告逻辑不等于
实际安装或调用过成员，`--build-only` 也仍只报告安装，不冒充运行验收。

### 无 Git 的核心源码审核

继续用 `release/legion-service/legion_tool.py audit-source` 的 `record`／`show`
入口和原审核账本。记录应携带实际解包根目录、完整 `source_commit`、相对源码路径、
真实行号和引用；不得凭描述编造引用。

当前运行包中的可信校验器核对 `CORE-PACKAGE.json`、固定提交、大小、SHA256、
Git blob 和引用行。摘要与行内容来自同一份原始字节，结果标明
`content_source: "core-package:<完整提交>"`，不伪造 Git 仓库。
被审目录内的脚本不作为校验器执行，坏清单不回退为未经核实的本地文件。
另装的同伴归档或嵌套 Git 项目继续保留自己的版本身份。

这证明引用与当前包清单一致；仍不是远端作者认证，也不把账本里的自述变成
已经验证的学习经验。包的公开许可与成员实跑标记保持不变。
私有和公开格式均通过当前可信安装中的同一个校验器读取；不另建审核账本。
`harness_selfcheck.py --distribution private-core` 是独立核心的检查模式名，
不是授权声明；不根据包的许可格式改名或让未测成员变成通过。

### 可选视频源码与真正的媒体能力

OpenMontage 是按需依赖，不是核心包中已启用的视频服务。安装、源码检查和媒体发现
共用现有 `external_agent_registry` 的固定版本与源码位置；个人源码根或显式 source-map
有误时如实报错，不悄悄借用旧开发目录。

安装中心用一个 `repo:openmontage` 条目表示这份源码，旧别名仍委托同一个入口，
不重复安装。只读源码检查由当前可信核心在隔离 Python 中执行，不导入被检目录的
同名标准库模块，不沿源码链接读取根外文件，也不把语法错误里的源码正文输出。
媒体端已加载的源码身份包含目录、版本与关键文件摘要；身份改变时明确要求重启，
不删除其他组件的模块缓存或假装使用了新版本。

登记角色为 `capability`，不进答题池；`install_scope=source_only`，不会顺带安装
requirements、启动 provider 或生成视频。上游许可仍为 AGPL-3.0，不改成连接器的 MIT。
源码检查通过、provider 依赖就绪、账号获准、真实生成并验收是四种不同事实。
