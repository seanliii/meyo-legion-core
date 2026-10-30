#!/usr/bin/env python3
"""power_gate · 2026-08-09 · 进化执行层省电总闸(降 CPU 卡顿)

用户 goal(降 CPU 卡顿·不是加进化)明确三条:
  1. 后台进化一律只在 load 低时启动·load 高就不启动。
  2. 已经在跑的进化·中途 load 升高不用管(让它跑完·别中途杀加剧抖动)。
  3. 进化时【绝不用 CDP 浏览器搜索】(常驻 chrome 最吃性能)·一律走 http_only。

和已有闸门的分工(不重造):
  - cron_load_gate.sh: cron 层·决定「这条 cron 命令这轮跑不跑」(进程启动前)。
  - _spawn_gate.py:    spawn 层·决定「能不能再 spawn 一个 claude 子进程」。
  - power_gate.py(本模块): 进化执行层·决定「进化逻辑现在该不该干活 / 能不能开浏览器」。
    给所有进化模块一行接入·统一「低负载才进化 + 进化不碰 CDP」策略。

用法(进化模块入口第一件事):
    from power_gate import should_run_evolution, browser_allowed
    if not should_run_evolution():
        return  # load 高·本轮不进化(不是错误·是省电降级)
    hits = agent.search(q, http_only=not browser_allowed())  # 进化默认禁 CDP

环境变量(用户可在入口自定义):
  MEYO_POWER_LOAD_MAX   进化启动的 load 上限(默认 15)。用户 2026-08-09 定:本机 8 核·
                        历史 load 中位数 23.7/均值 30.8/最大 216·日常跑 20-40。15 介于
                        "真闲(12)"和"日常下沿(20)"之间——比日常更闲时才进化·既降卡顿
                        (忙时不进化)又不至于完全不跑(真闲时能追上)。想更省电调小(如
                        12)·想进化更勤调大(如 20)。
  MEYO_POWER_LOAD_MULT  另一种设法: 按核数倍数(核数×此值)。仅在没设 MEYO_POWER_LOAD_MAX
                        时生效。默认不用(直接用绝对值 15 更直观)。
  MEYO_LOW_POWER=1      强制省电模式: 一切进化不启动(手动急停·比 load 判断更硬)。
  MEYO_EVOLUTION_ALLOW_CDP=1  例外放行进化用 CDP(默认禁·只有你确认低负载想用时开)。
"""
import os

# 进化启动的默认 load 上限(可被 MEYO_POWER_LOAD_MAX / power.conf 覆盖)。
# v13.90(2026-08-09 用户「你的目标是降低我 CPU 卡顿·思路不对」二次收紧):15→10。
#   病根同 spawn_gate:15 对 8 核太松——用户 load 11 就明显卡·进化却要 load 15
#   才不启动=省电闸形同虚设。10 与 spawn_gate(核×1.25=10)统一·进化只在真正
#   低峰(load<10)启动·给交互留足 CPU。日常 load 20-40 时进化自然全部跳过。
_DEFAULT_LOAD_MAX = 10.0


def _cpu() -> int:
    try:
        return os.cpu_count() or 8
    except Exception:
        return 8


def current_load() -> float:
    """1 分钟 load。读取失败返回一个高值(保守·当作过载→不进化)。"""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 999.0


# 用户配置文件入口(最友好·不用改代码/环境变量·直接编辑这个文件)。
# 格式: 每行 KEY=VALUE·如 MEYO_POWER_LOAD_MAX=15。# 开头为注释。
_CONF_PATH = os.path.expanduser("~/.meyo/power.conf")


def _conf_get(key: str):
    """从 ~/.meyo/power.conf 读一个配置项(用户入口·环境变量没设时的兜底)。"""
    try:
        if not os.path.exists(_CONF_PATH):
            return None
        for line in open(_CONF_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _load_max() -> float:
    """进化启动的 load 上限。优先级: 环境变量绝对值 → 配置文件绝对值 → 倍数 → 默认 15。
    用户 2026-08-09 定 15(本机 8 核实测日常 20-40)·可在 ~/.meyo/power.conf 或
    环境变量 MEYO_POWER_LOAD_MAX 自定义。"""
    # 1) 环境变量绝对值(最高优先)
    hard = os.environ.get("MEYO_POWER_LOAD_MAX")
    # 1b) 配置文件绝对值(用户最友好的入口·不用改代码)
    if not hard:
        hard = _conf_get("MEYO_POWER_LOAD_MAX")
    if hard:
        try:
            return float(hard)
        except Exception:
            pass
    # 2) 按核数倍数(可选·没设绝对值时才看)
    mult = os.environ.get("MEYO_POWER_LOAD_MULT") or _conf_get("MEYO_POWER_LOAD_MULT")
    if mult:
        try:
            return _cpu() * float(mult)
        except Exception:
            pass
    # 3) 默认固定值
    return _DEFAULT_LOAD_MAX


def should_run_evolution() -> bool:
    """现在该不该【启动】一轮后台进化。

    True  = load 低于阈值·可以进化。
    False = 强制省电模式 / load 过载 → 本轮不启动(省电降级·非错误)。

    注意(对应用户第 2 条): 本函数只在【启动前】判断。已经在跑的进化不受影响——
    调用方应在进入进化循环【前】调一次·而不是每步都调(别中途因 load 抖动自杀)。
    """
    if os.environ.get("MEYO_LOW_POWER") == "1" or _conf_get("MEYO_LOW_POWER") == "1":
        return False
    return current_load() <= _load_max()


def browser_allowed() -> bool:
    """进化过程中能不能用 CDP 浏览器(默认禁·对应用户第 3 条)。

    默认 False: 进化一律 http_only 搜索(friday_bing 等·又快又好·不拉常驻 chrome)。
    只有显式 MEYO_EVOLUTION_ALLOW_CDP=1 且当前 load 低·才放行(你确认想用时)。
    """
    if os.environ.get("MEYO_EVOLUTION_ALLOW_CDP") != "1":
        return False
    # 即便显式放行·load 高也不给开浏览器(CDP 最吃性能)
    return current_load() <= _load_max()


def gate_status() -> dict:
    """诊断用: 一次看清当前闸门状态。"""
    return {
        "load": round(current_load(), 2),
        "load_max": _load_max(),
        "cpu": _cpu(),
        "low_power_forced": os.environ.get("MEYO_LOW_POWER") == "1",
        "should_run_evolution": should_run_evolution(),
        "browser_allowed": browser_allowed(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(gate_status(), ensure_ascii=False, indent=2))
