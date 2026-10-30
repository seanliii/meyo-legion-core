# builtin JCode 的 Linux 原生依赖配方

**状态：仅准备，未登记共享 catalog、未安装官方运行件、未做 Linux 真验。**
`jcode-runtime` 是 `kind=tool / execution_role=capability` 的依赖，不是第二答题家；
没有任务 transport，没有 `provided_by`。已有 builtin `jcode` 仍是唯一答题入口，接线由 A 负责。

## 固定输入及随包约定

依据 A 的 `jcode-fixed-inputs.json` 和固定源码：

- 官方源码 `1jehuang/jcode`，版本 **0.84.0**，revision `57d58789970f4fe2405ca861c49275519df08147`。
- `Cargo.toml` 版本、源码 Git revision、LICENSE 摘要已只读核对。
- Linux 归档 `jcode-linux-x86_64.tar.gz`，48,783,970 bytes。
- SHA256：`e00eaede1a4f26812e77382bda9d82e2d301affd983d18ada38fddd43dce9571`。
- 源码 LICENSE SHA256：`720443eee2efeda8f9f93a7a6a6f62763c17171106f60df58a35b8ea638fdf60`。

A 将官方归档及以下 `manifest.json` 放入 **`{repo_root}/runtime/jcode-cli/`**：

```json
{
  "archive": "jcode-linux-x86_64.tar.gz",
  "sha256": "e00eaede1a4f26812e77382bda9d82e2d301affd983d18ada38fddd43dce9571",
  "version": "0.84.0",
  "source_revision": "57d58789970f4fe2405ca861c49275519df08147"
}
```

A 同时按既有 source-manifest/`.meyo-source.json` 机制为 `jcode-runtime` 映射固定源码。
不能指向原包的 0.81.4：配方检查 Cargo 包名/版本、完整 revision、LICENSE 摘要。
缺少随包输入就报错，没有下载、cargo 编译、brew 或另一条安装后备路径。

## 原生布局与检查

上游 `scripts/build_linux_compat.sh:151–198` 将官方 wrapper、`.bin` 与动态链接需要的
`libssl.so*`、`libcrypto.so*` 一同打包；wrapper 通过自身目录设置 `LD_LIBRARY_PATH`。
本配方原样保留归档的**全部文件**，不猜 SONAME、不只搬 ELF、不改写 wrapper；
额外复制已核源码 LICENSE 为 `SOURCE-LICENSE`，归档中的许可证也全部保留。

归档中的 SSL 库是否存在取决于上游链接方式，不能凭模板虚构固定库名。
已有库会按原字节保留，并与主程序一起核对 ELF64/little-endian/x86_64 头；
归档整体固定 SHA 约束完整文件集合，后续 verify 对比所有文件摘要及模式。

安装位置：`{runtime_dir}/official-release/`；
入口：`{runtime_dir}/bin/jcode → ../official-release/jcode-linux-x86_64`。
相对链接与上游 `readlink -f` 保留兄弟库关系。既有不同内容、陌生文件或冲突链接拒绝覆盖。
摘要/结构通过后仅执行 `--no-update --no-selfdev version`，校验版本及内嵌 commit；
复用 registry 已有的进程登记、20 秒超时和整组回收。非 Linux x86_64 直接拒绝。

## 复用现有登记/安装入口

本任务仅准备同目录 `jcode-runtime.spec.json`。整合时先核共享 catalog 的当前状态，
由 **`harness_onboard._upsert_catalog("jcode-runtime", spec_path)`** 持锁登记，不能手改 dirty catalog。
登记与安装分开时，后续使用已有 `harness_onboard --key jcode-runtime --build-only`：
它调用 `registry.install` 和本条目的 build_probe，不发模型请求。

完整 onboarding 也已有 capability 分流：本条目声明 `verify` 原生检查，
不跑答题速度题/工具题；不需要改通用平台。日常 probe 只查官方原生版本，
build_probe/verify 则检查归档、源码、全部已安装文件和版本。builtin 接线及 Linux 工具/续作由 A 另验。

## 离线验证与缓存影响

`tests/test_jcode_runtime_recipe.py` 使用临时假归档、假 ELF 头和版本探测 stub；
不执行官方二进制。覆盖摘要/版本/revision/许可证、恶意归档、兄弟库保留、
幂等及冲突保护；临时 catalog 真走持锁 upsert，真实答题池过滤函数排除本依赖。
另调用现有 `registry.install(build_only=True)`，但替换安装执行器和 probe，只验证命令与流程，不实际安装。

安装指纹仅新增本依赖条目和其 `install` 引用的 `jcode_install.py`；
固定 release digest 在配方常量中，改 digest 会改变本 helper 的 SHA。
没有修改 `build.sh`、通用 registry/transport/onboard、harness_registry/core 或缓存算法，
也没有删减指纹字段，因此不会通过改写全家共用安装文件的 SHA 使其他条目失效。
`jcode-runtime` 是新安装身份，本任务不声称它已有缓存或已完成安装。
