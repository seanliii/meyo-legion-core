"""v13.87 · dotenv 轻量加载器 · 纯 stdlib
搜索 agent / cron / hook 各处都要读 secret · 加个统一入口

用法:
  from env_loader import load_env
  load_env()  # 会读 ./seed-users-system/.env 和 ~/.env

行为:
- 不覆盖已在 os.environ 里的值 (shell export 优先)
- KEY=VALUE 行 · # 开头注释 · 空行跳过
- 双/单引号自动去
- 找不到 .env 不报错 · fail-safe

自动搜索的 .env 路径 (按顺序):
  1. $MEYO_ENV_FILE (显式指定 · 最高优先)
  2. <repo>/seed-users-system/.env  (跟当前项目结构对齐)
  3. ~/.env                          (用户级 fallback)
"""
import os
from typing import Optional


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_DEFAULT_PATHS = [
    os.environ.get("MEYO_ENV_FILE", ""),
    os.path.join(BASE, "seed-users-system", ".env"),
    os.path.join(BASE, ".env"),
    os.path.expanduser("~/.env"),
]


def load_env(paths: Optional[list] = None, override: bool = False) -> dict:
    """加载 .env 到 os.environ
    - paths: 显式路径 list · 默认按 _DEFAULT_PATHS 依次找
    - override: 是否覆盖已存在的 env (默认 False · shell export 优先)
    返回 {已加载的 key: 来源路径}
    """
    loaded = {}
    scan = paths or [p for p in _DEFAULT_PATHS if p]
    for path in scan:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip()
                    if not k:
                        continue
                    # 去引号
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    if not override and k in os.environ:
                        continue
                    os.environ[k] = v
                    loaded[k] = path
        except Exception:
            continue
    return loaded


if __name__ == "__main__":
    try:
        r = load_env()
        if r:
            print(f"loaded {len(r)} keys:")
            for k, path in r.items():
                # 脱敏 · 只显示 key 和来源
                print(f"  {k}  <- {path}")
        else:
            print("no .env found or all keys already in environ")
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
