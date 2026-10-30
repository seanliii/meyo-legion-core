#!/usr/bin/env python3
"""重型 HTTP-server 型外部家的后台守护启动器(通用·非 paperclip 专属)。

usage: http_server_daemon.py <pidfile> <logfile> <cwd> -- <command...>

行为:
  - 已有健康进程(pidfile 活)→直接退出 0(幂等)
  - 起子进程(nohup 语义·setsid 自立进程组·stdout/stderr 落 logfile)
  - 写 pidfile·等最多 <wait_s>(默认 20s·env HTTP_DAEMON_WAIT_S)轮询探活端口/进程
  - 进程秒死(安装坏)→非零退出,日志尾部打印到 stderr 供 install 错误回流

设计依据:OpenHands worker 懒拉起同款哲学(server 不死·按需拉起),但 control-plane
型家没有 run worker 环节——install 最后一步直接把 server 守护起来,probe 才能绿。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 4 or "--" not in argv:
        print("usage: http_server_daemon.py <pidfile> <logfile> <cwd> -- <command...>", file=sys.stderr)
        return 2
    sep = argv.index("--")
    pidfile, logfile, cwd = map(pathlib.Path, argv[:3])
    # 可选第4位置参数:该 server 的就绪等待秒数(heavy 家如 paperclip 要跑 embedded
    # postgres 初始化+迁移,20s 默认不够;per-server 声明,不用全局调大)。
    wait_override = ""
    if sep > 3 and str(argv[3]).strip().isdigit():
        wait_override = str(argv[3]).strip()
    cmd = argv[sep + 1:]
    if not cmd:
        print("empty command", file=sys.stderr)
        return 2
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    # 幂等:已有活进程直接成功
    try:
        old = int(pidfile.read_text().strip())
        if _alive(old):
            print(f"already running pid={old}")
            return 0
    except Exception:
        pass
    log = open(logfile, "ab")
    env = dict(os.environ)
    # 死代理坑(CLAUDE.md 血泪):localhost 探测被 HTTP_PROXY 劫持→拒连。server 是本机回环,
    # 明确不走代理。
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
    pidfile.write_text(str(proc.pid))
    wait_s = float(wait_override or os.environ.get("HTTP_DAEMON_WAIT_S", "20"))
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if proc.poll() is not None:
            # 秒死:安装/启动坏了,把日志尾部给 install 错误回流
            tail = ""
            try:
                tail = pathlib.Path(logfile).read_text(errors="replace")[-800:]
            except Exception:
                pass
            print(f"server exited rc={proc.returncode}\n{tail}", file=sys.stderr)
            return proc.returncode or 1
        time.sleep(0.5)
    print(f"daemon started pid={proc.pid} (waited {wait_s}s·still running)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
