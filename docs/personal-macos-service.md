# 个人 macOS 常驻服务

更新：2026-10-14（北京时间）。这是本机已经部署的单人私有实例说明，
不是公网服务、开源发行版或全部成员兼容声明。

## 现在怎么用

在这台 Mac 上打开 **http://127.0.0.1:8000/**。
正常页面保留原身份、历史会话、文件预览和下载，可以沿原会话继续修改成果。
现有浏览器已经登录，不需要重新初始化身份。

新浏览器需要通过页面的“个人授权”登录；凭证只在本机实例的
`login-token.txt` 内，不能贴到公共消息、命令参数或 Git。

目前实际验证的是 Codex 原生执行，以及用户指定的 OhMyGame 源码复核协作。
这不代表每家成员都已可用，也不代表原游戏的玩法或美术通过。
当前模型仍使用本人已有的授权网关，并非已经完全独立于该网关。

## 固定了什么

| 项目 | 本机配置 |
| --- | --- |
| 程序快照 | `9685cd7babf917d529a5e51d1026a4dd121a9531` |
| 程序位置 | `$HOME/.local/share/meyo-legion/apps/9685cd7babf917d529a5e51d1026a4dd121a9531` |
| Python 环境 | `$HOME/.local/share/meyo-legion/runtimes/core/venv`，Python 3.12.11 |
| 实例目录 | `$HOME/.local/share/meyo-legion` |
| 监听地址 | `127.0.0.1:8000`，不监听局域网或公网地址 |
| 系统启动项 | `$HOME/Library/LaunchAgents/com.seanliii.meyo-legion.plist` |
| 系统服务名 | `com.seanliii.meyo-legion` |

程序是从固定 Git 提交导出的副本；后续在开发仓库编辑或切换分支，不会偷偷改变
正在运行的程序。Python 环境独立安装，没有复制旧虚拟环境，也没有开启
`system-site-packages`。本次核心环境的 36 项依赖版本已核对，`pip check` 通过。

启动项复用原有 `meyo.py serve`，没有增加新的模型调度器。
它由 macOS 的 `launchd` 管理：已配置在登录此系统用户后自动启动；异常退出后按配置重启，
正常退出不自动拉起。电脑关机、睡眠或用户退出登录时，不承诺继续提供服务。
本次没有注销用户或重启整台 Mac 来验证系统登录过程。
这不是一台可供他人随时访问的云服务器。

## 查看状态和日志

```sh
launchctl print "gui/$(id -u)/com.seanliii.meyo-legion"
```

日志位于：

```text
$HOME/.local/share/meyo-legion/logs/service.stdout.log
$HOME/.local/share/meyo-legion/logs/service.stderr.log
```

页面与 `/health` 能打开只证明服务在响应；模型是否能执行、文件是否可用，
仍要看真实任务结果，不能拿健康检查替代业务验收。

## 正常停止、启动和升级

**先让正在执行的任务结束。** 不要为升级强杀原生成员，不要使用全局 `pkill`，
也不要重新运行 `init` 来“修复”历史。

正常停止这个指定服务：

```sh
launchctl bootout "gui/$(id -u)/com.seanliii.meyo-legion"
```

确认旧进程退出后，再启动已有配置：

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.seanliii.meyo-legion.plist"
```

这里使用“正常卸载后再加载”，不建议用强制杀进程的方式重启。
若停止失败、仍有活任务或端口被其他进程占用，先核实际状态，不重复启动第二个实例。

升级时先验证新代码，再导出到新的固定版本目录、正常停止旧服务、修改本启动项
指向的新程序路径并重新加载。保持实例目录不变；不要原地改已发布程序副本，
不要连同数据目录一起覆盖。升级后核原身份、原会话、成果和原生接续。

## 数据与授权放在哪里

- `config.json`：本机用户、端口和明确授权的能力动作。
- `login-token.txt`：个人页面登录凭证。
- `secrets/model.json`：模型提供方配置与密钥。
- `data/users`、`data/teams`、`data/project_context`、`data/task_memory`：
  本实例身份、会话、工程和执行记录。
- `data/outputs`：军团真正生成的成果。
- `codex-home`：本实例的原生 Codex 状态，不混用用户其他原生配置。
- `repos`、`runtimes`、`projects`：按需接入的成员源码、运行依赖和工程。

**整个实例目录都不应提交到 Git。** 备份也应按含敏感数据处理。
同一实例重启不会主动更换凭证；换模型或提供方后，旧原生断点的绑定检查仍然生效。
工作室能力只按列出的动作授权，不因安装就自动获得所有未来权限。

## 尚未完成的独立阶段

- 尚未核定用于外网服务的个人主机／平台，本机端口没有暴露给他人。
- OhMyGame 是可信个人桌面工作室，不可直接当成不可信多用户云执行端。
- 第一方公开许可证、第三方分发说明和公开范围，仍需在真正开源前确定。
- 原游戏制作与试玩属于后续真实需求，不在本次常驻服务验收里补写为成功。

已有真实任务、反馈采用和恢复证据的范围，见
[个人运行验收说明](personal-runtime-verification-2026-10-14.md)。
