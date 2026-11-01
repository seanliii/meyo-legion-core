# 部署：代码、服务和展示分开

先读核心 `README.md`、`docs/public-runtime-operations.md`、`docs/personal-macos-service.md` 和本次适用的 `deploy/linux/` 文件。历史地址、账号、端口、主机和成本不是本次配置；只使用用户当前拥有、已核实且获授权的资源。

## 1. 先认清交付面

| 用户要什么 | 复用什么 | 不代表什么 |
|---|---|---|
| 取得源码／安装包 | [发布指南](publication.md)、原打包器 | GitHub Release 不是在线服务 |
| 本机可用或常驻 | `meyo.py`，按平台使用已有 launchd／systemd 托管 | `init/serve/dsh-config` 不会自动上云；Mac 睡眠／关机时不保证在线 |
| 公网可登录、执行、保存与下载 | 原核心服务 + systemd + Caddy + 独立个人实例 | 首页 200、反代配置保存和开放端口不是动态后端验收 |
| 静态网站／showcase | 已安装的 `$aliyun-external-deploy` | 静态展示不能替代模型、鉴权、任务或数据库后端 |
| 公司 Plus/Hulk 服务 | 已安装的 `$meyo-legion-release` 及该项目当前入口 | 不与个人阿里云或 DSH 公共源码发行混用 |

按技能名定位实际安装位置，不复制其中的私有主机资料。只整理技能或准备本地包时不实际部署、不采购、不扩大公开范围。已有发布许可不等于允许影响其他业务或购买新资源。

## 2. 独立本地核心与 DSH 接线

1. 使用核对过的固定源码，在独立 Python 3.12 venv 中串行安装当前 `requirements-core.txt`；可选依赖按需装。复用已有全局安装锁，不复制旧 `.venv`、开启 `--system-site-packages` 或借旧 editable 安装读取私有工程。
2. 程序、运行依赖、实例数据分离。实例在源码外，使用本人独立身份、模型授权和原生状态，不继承公司密钥、旧 HOME 或历史实例。
3. 沿原 `meyo.py`，不另造启动器或调度器。命令模板中的变量由当前任务核定，端口选本机实际可用值：

```sh
"$CORE/.venv/bin/python" -I "$CORE/meyo.py" init \
  --state-dir "$STATE" --user "$INSTANCE_USER" --port "$BACKEND_PORT"
"$CORE/.venv/bin/python" -I "$CORE/meyo.py" check --state-dir "$STATE"
"$CORE/.venv/bin/python" -I "$CORE/meyo.py" serve --state-dir "$STATE"

# 保持 serve 运行，在另一终端读取接线信息。
"$CORE/.venv/bin/python" -I "$CORE/meyo.py" dsh-config --state-dir "$STATE"
```

4. `dsh-config` 的 `config` 对象给出地址与凭据**引用**。在 DSH 原生“连接与配置”保存并读回；不粘整份回执，更不填令牌内容。`tokenFile` 必须是 **DSH Host 能读取**的私有文件，远端核心的磁盘路径不会自动变成本机可用路径。
5. 模型配置通过现有 `configure-model` 和本人私有密钥文件；确认真实协议与提供方兼容，不固化历史模型或把不同 API 协议当成同一种。配置保存、CLI 可执行、真实模型任务分别核验；未实跑仍为 `not_verified`。
6. `check` 是本地检查；`serve` 是原服务，可能有后台活动，不当成离线探针。重启复用同一实例，不重新 `init` 来“修好”历史。改变模型／身份后保留原恢复绑定检查，不把旧任务私自交给新提供方。

DSH 配置和能力包开关影响同一个 profile，不因切换工作会话就变成会话私有。连接成功后的操作、刷新与重启按[验收指南](acceptance.md)完成。

## 3. 公网动态后端沿原服务部署

```text
外部浏览器 HTTPS
  → 当前运营者控制的域名与 TLS
  → 同机 Caddy（保留真实 Host / Origin、及时转发 SSE）
  → 回环地址上的原 meyo.py serve
  → 个人身份、原工程、原生执行与正式成果
```

- 先核已有主机、服务、路由、版本、实例和回滚条件。系统服务以独立非 root 身份运行，程序固定版本，实例和凭据保持私有；HTTPS 登录不等于不可信多租户执行隔离。
- 复用 `deploy/linux/meyo-legion.service`、`Caddyfile` 和已有运维入口；不要另搭一层答题服务。`bootstrap.sh` **仅用于初装**，不能拿来重建活跃部署。
- `bootstrap.sh --check/--install` 接收的是它规定的部署包目录，**不是** `package_core.py` 的公开源码 TAR。先核现有输入契约、平台和包清单，复用原准备过程；不为此写新 exporter 或伪装两种包等价。
- Linux 初装器已有 `flock` 全局安装锁；其他重安装也沿对应平台的既有串行入口，不另用一把互不协调的锁，不并发叠加 apt／pip／npm 等重活。
- 使用原入口声明当前受控的单一 HTTPS 来源；命令只保存配置，不取得证书或完成部署：

```sh
"$CORE/.venv/bin/python" -I "$CORE/meyo.py" configure-public-origin \
  --state-dir "$STATE" --origin "$PUBLIC_HTTPS_ORIGIN"
```

- 后端仍绑定回环，不改为任意公网监听。Caddy 保留真实 Host／Origin，拒绝伪造 localhost Host，不注入个人 Bearer、不开放通配来源；不暴露 Docker、ADB、VNC 或私有文件。
- 沿已有代理配置验证证书、鉴权、用户归属、正常 GET／POST、下载和 SSE。SSE 不缓存整段、不因短代理超时截断长任务；实际代理版本上的配置检查不替代外部浏览器实测。
- 访问记录限必要诊断，不能包含 Cookie、Authorization、请求正文或带凭据查询串；不把原始调试日志公开。

完成标准是从外部浏览器走原请求：登录 → 实际任务／流式进度 → 正式下载与 hash → 反馈续作 → 保存／刷新 → 服务正常重启后原状态仍在。没跑模型或设备，就保留对应未验证项，不能用公网绿灯替代。

## 4. 更新、备份和回滚保留原现场

- 固定候选版本并做相关验证，等待本实例活任务结束或按用户授权准确取消，再正常停止指定服务。核 PID、任务和端口，不全局 `pkill`，不因超时就假定进程已停。
- 新程序放入独立固定版本目录，再沿现有服务配置切换；不原地覆盖正在运行的代码或连同实例一起替换。macOS 复用当前 launchd 配置，Linux 复用当前 systemd 配置，不照搬历史 label 或账户。
- 备份使用已有机制。`deploy/linux/backup.sh` 要求服务已静止，它不会替操作者停服；活数据库直接 tar 不等于一致性备份。本地密文副本也不等于异机备份或恢复演练。
- 切换后核原身份、配置、会话、工程、正式文件和原生断点，重放原旅程；回滚同时核程序、依赖和数据兼容，不能以丢历史换“恢复成功”。
- 机器负载高先减少重活并诊断，不杀用户有窗口的应用；详见[测试隔离与资源边界](acceptance.md)。

## 5. 静态展示单独处理

静态站调用现有 `$aliyun-external-deploy`，按需读取它的部署流程和 showcase helper，不复制私人目标到本技能。

保留现役 showcase 路由和其他应用入口；读取**本次真实父版本**、精确文件清单与现役配置。新版独立暂存、核文件和配置后原子切换本应用入口，失败回滚到该父版本。不能只换一个 commit 就重复使用旧 helper 中固定的父版本、身份或路由名单；不重启共享网关来更新一个静态页面。

外部打开新页面并检查旧入口、资源、交互与下载。若用户要动态后端，静态壳只能算展示部分，不能删掉核心能力后宣称已部署完整军团。
