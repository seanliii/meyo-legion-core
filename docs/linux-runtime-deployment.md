# 个人 Ubuntu 运行主机：部署与恢复

更新时间：2026-10-19。部署方法、已完成的运行证据和未完成的设备验收分别记录。

根据 D 的固定发布回执，2026-10-19 19:42:53（北京时间），现有个人香港云机
已从 `ab6d28c11a0389991605f6d65f1124531b3e7c21` 切换为
`b4b5f886e3d54617a237a2d813c10839a2960c05`。主线程仅本地复核回执，
没有重复部署；同机 HTTPS/API 验收通过，19:49 外部浏览器访问仍超时。
这不是新的国内服务器部署，也不等于 `vortexsean.com` 已上线。
云手机现复用 AgentBay 标准 MCP 后端，已接原手机页面及显式授权的原生成员工具；
`meyo-mobile-reaper.timer` 独立回收到期设备。旧 Redroid 失败保留历史，不再是当前唯一手机后端。
具体使用与验证范围见[云手机接入记录](agentbay-mobile-verification-2026-10-17.md)。
2026-10-19 上午曾恢复本机经既有代理访问公网入口，真浏览器操作云电脑，
并短验手机启动、操作与释放；这段短验本身没有重新部署程序。
晚间程序更新与原网络限流分别记账，不能用同机验收替代外部访问。
详见[访问与设备记录](personal-access-and-devices-2026-10-19.md)。
最新模型、同原生会话GUI返工和备份结果以
[个人云运行验收](personal-cloud-verification-2026-10-16.md) 为准；
下列 `4550f3a`／`474cef3` 保留为部署演进记录，不是当前待重跑的任务。

阿里云新主机已购买并运行。程序仍以个人 GitHub 为主线；本机军团和公司环境不动。
新增远程管理钥匙须明确授权，不能把购买确认或自动续作当成该授权。
模型不得从 Mac 的公司网关配置中默认搬用。

## 首轮部署与设备准备的历史基线

**2026-10-19 晚间更新**：用户已支付的杭州固定带宽 ECS 上，固定 `b4b5f886` 程序、
原个人身份／会话／交付文件恢复、DeepSeek Flash 原生文件任务和云电脑实际
输入／保存／重开已经通过。新机只监听回环，公网配置仅完成离线解析；
域名短信绑定、ICP 资料及审核、DNS／HTTPS 启用和外部浏览器验收未完成。
手机原授权余额为 0 秒，不重新发起计费短测。详细分层结果见
[当天访问与设备记录](personal-access-and-devices-2026-10-19.md#杭州新机实际完成项2325-核对)。
下面首轮日期、旧机能力和旧失败均保留为历史，不能替代新机当前状态。

后续已在杭州运行 `63ae0db`，修通失败尝试文件被隐藏及同任务多轮历史卡被覆盖的
两个前端断点。原身份、8 张独立历史卡、原文件实际点击下载和刷新恢复的
同机真实浏览器旅程已经通过；原失败状态保留，未新发模型任务。
公网仍未启用，不能用这次同机验收替代域名／备案／外网验收。

个人云的固定程序 `4550f3a56701eae85b2d38f529265fa4aaeed199` 已完成首轮运行；
之后 `474cef37a518e79d486ca5b9eebd3b085f4220a2` 已切换运行并接通原生桌面工具：
DeepSeek 原生文件工具、同原生会话继续修改、真实 HTTPS/Cookie API 读取与下载，
以及加密备份、独立目录恢复和加密异机副本已验证。
这些是窄运行证据，不等于外部浏览器、所有模型或设备已验收。

2026-10-16 用户追加要求云电脑、云手机和模型实际可用，当前分开记账：

| 项目 | 已确认 | 尚未完成 |
| --- | --- | --- |
| 设备运行层 | 当前服务用户的 rootless Docker 实际运行；普通原生命令与可信设备工具的 Linux 权限分离已验证 | 不据此外推不可信多租户安全 |
| 云电脑 | 原生 MCP 截图、普通编辑器键盘／剪贴板输入、GUI保存和重开已执行；公网浏览器画面、鼠标、键盘及真实下载通过 | 其他办公软件与完整复杂业务流程未验证，不从便笺用例外推 |
| 云手机 | ADB server与binderfs可用，官方Android镜像已取到 | rootless和TTY样本均立即退出；尚无可用手机，不启用宿主特权容器掩盖失败 |
| 模型 | DeepSeek父级和原生子代理真实模型一致；Kimi原生看图、工具、GUI返工和公网默认入口同会话续作已验证 | 其他成员与模型未逐一通过；OpenRouter指定GPT模型明确返回地域不可用，不绕地域限制 |

云电脑来自图形容器，云手机采用 Android 容器，不冒称购买或接入了实体手机。
云端 API 与用户浏览器分别留存运行证据，不相互代替；旧代理访问失败保留为历史。

### 显式接通已准备好的 rootless 桌面

此配置不安装 Docker、不创建容器、不更换模型或登录身份。操作员先核真实 daemon 的
`SecurityOptions` 含 `name=rootless`，数据目录与本实例的
`home/.local/share/docker` 一致，再保存当前用户专用 socket 与实际 noVNC 端口：

```sh
python meyo.py configure-devices --state-dir /var/lib/meyo-legion \
  --docker-host unix:///run/user/运行UID/docker.sock \
  --desktop-port 实际回环端口
```

正式命令由操作者替换真实 UID／端口并执行，不交给普通用户猜数值。
无设备配置时保留原本机行为；有明确配置时不回退宿主 rootful Docker socket。
保存后仍须在无活任务的维护窗口重启军团，不把保存成功标成设备已通过。

对外画面复用 Caddy：仅在桌面就绪后，将 `deploy/linux/desktop.caddy` 安装到
`/etc/meyo-legion/routes/desktop.caddy`，并设置：

```text
MEYO_DESKTOP_PUBLIC_PATH=/cloud-desktop
MEYO_DESKTOP_UPSTREAM=127.0.0.1:实际回环端口
```

主 Caddyfile 的可选目录导入在未启用设备时不产生新路由。
该同源路径的静态资源与 WebSocket 握手均通过现有身份检查和桌面 owner 检查，
认证查询不继承 noVNC 的查询参数；转入容器前移除 Cookie 和授权头。
内部探测继续访问回环地址，用户收到的是受保护的同源路径，不再收到服务器的 localhost。
必须验证匿名／错误主体被拒绝、正确主体能看到并操作画面，不能只验证端口。

原生模型的命令工具不能直接取得 Docker／用户总线管理口；设备操作经明确授权的
已有宿主工具进行。rootless 只表示没有宿主 root 权限，不等于已经完成这项工具隔离。
在实际权限验证通过前，不把新设备能力登记为军团已可用。

### 已安装实例的服务挂载与应用补齐

本次 systemd 主服务原有 `ProtectHome=true` 会隐藏自己的 `/run/user/UID`，
导致同一个 UID 在服务外能连接 rootless Docker、服务内却看不见 socket。
不能据此关闭整个原生沙箱，也不能直接开放 rootful Docker。

在本次 Ubuntu 24.04／systemd 255 实例中，已实际验证以下**主服务**覆盖配置。
`运行UID` 必须由当前服务账号解析，不能把别的机器的数字照搬：

```ini
[Unit]
Wants=user@运行UID.service
After=user@运行UID.service

[Service]
ProtectHome=tmpfs
TemporaryFileSystem=/home:ro /root:ro /run/user:ro
BindReadOnlyPaths=/run/user/运行UID
```

三个遮蔽目录的只读挂载与指定 socket 可达性均须在实际主服务命名空间核对；
只看配置文字不够。本次明确写 `TemporaryFileSystem=…:ro`，不只依赖
`ProtectHome=tmpfs` 的推定行为。原生命令另有设备管理口遮蔽，
可信 MCP 宿主接通设备不等于任意模型 shell 都获得 Docker 管理能力。

桌面镜像没有普通文本编辑器时，已沿现有容器安装串行锁补装
`mousepad` 和 `fonts-noto-cjk`。这是软件准备，不是 GUI 体验验收。
本版本工具说明保留已核 Linux 按键格式：`enter`／`esc`、
`hotkey` 的 `keys` 数组；Linux 占位 accessibility tree 不是实际组件树。
截图、真实操作和保存后的原文件仍分别验收。

部署写配置时还要考虑 `umask`：创建文件的 mode 参数不能保证最终权限。
公开给代理读取的配置须显式设置权限并以真实代理 UID 检查；
凭据继续0600，不能为修复代理启动而放宽秘密文件。

## 当前范围与依赖

- 本脚本用于新建的 Linux x86_64 主机（本次 Ubuntu 24.04），不是运行中服务的升级器。
- 程序固定到 Git 提交，由独立 Python 3.12 venv 运行原 `meyo.py serve`。
- 原已声明的 Codex CLI 固定到本机实际验过的 **0.153.4**，使用官方 Linux 独立二进制，
  不复制 Mac 二进制、HOME、登录态或模型密钥。这是恢复已有内建成员的运行依赖，
  不是绕过接入入口注册新成员。以后接入其他成员仍用既有 `harness_onboard.py`。
- Caddy 固定 **2.11.7**，不用会截断部分进度流的 2.11.6；其源码和发布说明均已核对。
- 首条 core/native 旅程不要求先安装 Docker、云手机、全部成员或 Node 工具链；
  这些能力未因此被算成已可用，后续按真实使用需求单独接通。

## 部署包及执行

私有目录包含 `app.tar.gz`（固定 Git archive）、`codex.tar.gz`、`caddy.tar.gz`，
以及 `SHA256SUMS`、`SOURCE_REVISION` 和 `PUBLIC_ORIGIN`。
SHA256SUMS 只允许这三个固定文件名；官方二进制须先与对应 GitHub Release 的 SHA256 核对。
只打包被 Git 跟踪的程序文件，不打包 Git 历史或本机实例资料。

```sh
bash deploy/linux/bootstrap.sh --check /私有部署包目录
# 仅在目标主机与必要授权核定后：
sudo bash deploy/linux/bootstrap.sh --install /私有部署包目录 实例用户名
```

命令由部署操作者执行，不要求普通用户操作终端。`--check` 仅验输入，不安装、不联网发模型。
安装串行进行；已有服务正在运行、同名账号归属不符、固定程序被改或已有其他版本时拒绝覆盖。
程序和依赖为 root 所有；业务数据仅 `meyo-legion` 用户可读写。
同一用户名重跑原 `init` 不会重置现有凭据。登录凭据仅保存在实例的 `login-token.txt` 中。

| 内容 | 路径 |
| --- | --- |
| 固定程序 | `/opt/meyo-legion/releases/<Git提交>` |
| 当前程序入口 | `/opt/meyo-legion/current` |
| Python 环境及实际依赖版本 | `/opt/meyo-legion/venv`、`/opt/meyo-legion/core-resolved.txt` |
| 业务数据、凭据及原生断点 | `/var/lib/meyo-legion` |
| HTTPS 证书与账户状态 | `/var/lib/meyo-proxy`，与业务进程分属不同 UID |
| 系统服务 | `meyo-legion.service`、`meyo-proxy.service` |
| 脱敏访问日志 | `/var/log/meyo-proxy/access.json`，轮换文件总数和单文件体积有界 |

公网只有 HTTPS 业务入口；80 供证书验证及 HTTPS 跳转。原服务继续监听 `127.0.0.1:8000`。
代理不改 Host/Origin、不代填用户凭据；拒绝访客伪造 localhost Host。
代理管理 API 关闭，不能让执行任务的进程通过它修改网站路由。
访问日志删除请求头、响应头和整段查询串，不采集请求正文。
分享地址只记录 `/shared/`，不记录路径中的分享凭据；原请求地址仍完整转发给后台。
业务与原生任务日志另属私有数据，不把它们冒称为已经脱敏的访问日志。

本次可采用公网 IPv4 的短期证书，不强迫购买新域名；使用 `shortlived` 证书配置。
Caddy 负责续期。**有效证书、自动续期条件及公网连接仍需云上实证，不能拿配置解析通过替代。**
没有关闭 TLS 验证、使用假证书或要求访客绕过浏览器安全提示。

## 运行、更新与备份

1. `systemctl status meyo-legion meyo-proxy` 与 `/health` 只证明进程/入口状态。
   必须另验真实任务、文件下载、反馈继续修改和刷新恢复。
2. 用 `journalctl -u meyo-legion -u meyo-proxy` 看私有服务日志。
   不把含任务资料的日志贴到公开页面或 Git。
3. 更新前先从实际会话和进程核对原任务已经结束。**systemd 停服会结束子进程，不会代等任务。**
   核定无活任务后才停指定服务；禁止全局 pkill 或改动其他服务。
4. `/opt/meyo-legion/backup.sh` 只在服务已停止时生成加密维护备份，
   不自行停服、不清旧备份。新增设备后，操作员还须先确认设备、容器存储与应用数据静止，
   不能只停军团就宣称全实例一致。密码独立存于 root 私有目录，并已有独立私有副本。
5. 云主机上的备份库为 `/var/backups/meyo-legion`。2026-10-17 已将云手机接入后的
   `941a57e136de` 快照密文取回核定的个人电脑私有目录，核大小和 SHA256 一致；
   云端独立恢复目录中的18个关键文件及权限也已核验，包含本次手机配置、额度与成果。
   密码与密文分存，旧副本保留。**这不代表全天自动备份或异机灾难接管已完成**；
   后续设备与程序发布须按新输入另核备份边界。
6. 恢复先到新的私有目录，核原身份、文件 SHA、会话与原生断点，不能覆盖运行中实例。
   真正接管时保持原实例路径；有些原生断点保留绝对路径，换目录读得开不等于能继续执行。
7. 新源码先放新的不可变目录；停服后切换入口，保持实例 UID 和数据目录不变。
   venv/CLI 变化也要作为本次固定输入记录，禁止在活任务中原地更新。

## 不能从这份部署推导的结论

当前个人入口按可信实例设计。不同 token 可以绑定不同应用用户，
但共享运行 UID、模型配置和部分实例级资料，不等于不可信多租户隔离。
不能直接把共享账号或全局手机发给所有访客。

### Ubuntu 24.04 的原生沙箱前提

`unprivileged_userns_clone=1` 并不代表 AppArmor 允许完整建立沙箱。
2026-10-16 的云端文件验证实际遇到 `bwrap: setting up uid map: Permission denied`；
内核记录显示它从 `unconfined` 转入 `unprivileged_userns` 后拒绝写入自己的 UID 映射。
这不是模型认证失败，也不能靠模型换命令反复重试解决。

安装器在该限制启用时加载 `meyo-codex.apparmor`。它只匹配 root 所有的
`/opt/meyo-legion/native/codex`，采用 Ubuntu 的具名 unconfined 应用例外并显式允许
`userns`。例外适用于该标签及继承的进程链，不是给整个服务 UID 或系统 bwrap 放行。
全局 sysctl、Codex 的 `workspace-write`、bwrap 挂载与 seccomp 都保留。
遇到同名但内容不同的既有策略会拒绝覆盖。

已安装主机只部署这份经核定的策略，不重跑初始化安装器或重置实例。
策略加载后，应以同一服务 UID 无模型验证：工作目录内写入、读回成功，
允许写根之外的同 UID 自有测试目录仍拒绝写入；核实际 bwrap 标签及未入该标签的
负对照。最后才重放原任务。CLI `codex sandbox` 按宿主系统选后端，
0.153.4 没有 `sandbox linux` 子命令。

新主机部署必须分别验独立身份、实际执行与原生沙箱、模型授权、HTTPS/SSE、
下载、重启接续、备份及恢复；本实例的实际通过范围以运行验收为准。
云手机已接入当前个人身份及原生工具，最新加密备份的异机副本已取回。
不可信多租户执行、跨用户设备隔离的完整验收及任意应用任务仍未完成，
不以首页可打开或一个可信个人身份的成功代替。

参考：Caddy 官方 2.11.7 发布说明；Let's Encrypt 2026-01-15 公网 IP／短期证书公告。
