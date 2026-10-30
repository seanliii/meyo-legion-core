#!/usr/bin/env python3
"""shell_error_handler · 2026-08-13 · 独立可进化模块(自进化闭环的安全出口)

用户大目标:测自进化能否优化错题。查实的核心障碍:进化诊断反复指出"shell_exec 无
错误捕获/无重试/无降级"·但修法都要改核心文件 qa_auto_agent.py(7600行·自动改是宪法红线)
→进化永远只能记提案落不了地。

正路(架构重构):把 shell 错误处理【抽成这个独立小模块】——白名单内、带 pytest、
进化能安全改它(safe_patch 编译+pytest+回退)·不碰 7600 行核心。进化诊断"shell 无重试"
→target 指向本文件→真改→回测→错了再进化。这就是给核心逻辑的【安全可进化出口】。

职责:输入一次 shell 执行结果·输出决策(retry/downgrade/salvage/give + 教学 hint)。
这是【纯函数·无副作用】·好测好进化。shell_exec 调 classify_and_advise 拿决策拼进 summary。

进化改本模块时可优化:①错误分类更细 ②重试退避策略 ③降级传输阶梯(curl→requests→浏览器)
④脚本错的修复引导。改完 safe_patch 编译+pytest 过才留·回测不对自动回退。
"""
import re


# 错误签名 → 类别(进化可扩充这张表)
_NET_MARKERS = ("timeout", "timed out", "connection", "connect", "network",
                "ssl", "reset", "refused", "temporarily unavailable" ,
                "could not resolve", "429", "502", "503", "504", "read timed out")
_ENV_MARKERS = ("resource temporarily unavailable", "fork:", "cannot allocate memory",
                "too many open files", "no space left", "cannot fork")
_SCRIPT_MARKERS = ("syntaxerror", "indentationerror", "modulenotfounderror",
                   "importerror", "nameerror", "traceback (most recent call last)",
                   "no such file", "command not found", "not found")


def classify_error(exit_code, stderr):
    """把一次 shell 失败分类。返回 'ok'|'net'|'env'|'script'|'unknown'。纯函数。"""
    try:
        if exit_code == 0:
            return "ok"
        low = (stderr or "").lower()
        # env 最先判(资源耗尽·重试无意义)
        if any(m in low for m in _ENV_MARKERS):
            return "env"
        if any(m in low for m in _NET_MARKERS):
            return "net"
        if any(m in low for m in _SCRIPT_MARKERS):
            return "script"
        return "unknown"
    except Exception:
        return "unknown"


def classify_and_advise(command, exit_code, stdout, stderr, attempt=1):
    """★核心决策★:输入一次 shell 执行结果·输出该怎么办。纯函数·fail-safe。
    返回 {category, action, hint, should_retry}。
      action: pass|retry|downgrade|salvage|give
    进化改的就是这里的策略(分类/重试/降级阶梯/修复引导)。"""
    try:
        cat = classify_error(exit_code, stderr)
        if cat == "ok":
            return {"category": "ok", "action": "pass", "hint": "", "should_retry": False}
        if cat == "env":
            # 环境资源耗尽:别重试(会更糟)·已有熔断处理·这里只标不建议重试
            return {"category": "env", "action": "give", "should_retry": False,
                    "hint": "[shell错误·环境资源耗尽] fork/内存/文件句柄耗尽——重试同类命令没意义·"
                            "先降低并发/等资源释放·或换更轻的做法。"}
        if cat == "net":
            # (2026-08-15·A) 先判是不是【真断网】(vs 单个端点抖动):真断网→别重试烧预算·
            #   提示挂起等网络恢复(任务上下文/进度保留·网络回来自动续)。
            try:
                import net_watchdog as _nw
                if not _nw.net_ok():
                    return {"category": "net", "action": "pause_offline", "should_retry": False,
                            "hint": "[⚠️检测到真·断网(非端点抖动)] 别重试烧预算。任务上下文/已做进度"
                                    "都保留着(不丢)。调 network_status(wait_if_offline=True) 挂起等网络"
                                    "恢复(最多1小时·恢复后自动续)·或先做不依赖网络的部分。"}
            except Exception:
                pass  # 探测本身故障不阻断原有重试逻辑
            # 网络/超时/限流(端点抖动·非全断网):分级重试→降级换传输
            if attempt <= 2:
                return {"category": "net", "action": "retry", "should_retry": True,
                        "hint": f"[shell错误·网络/超时类·第{attempt}次] 退避后重试(sleep 指数退避)·"
                                "别短间隔连试(429越试越封)。"}
            return {"category": "net", "action": "downgrade", "should_retry": True,
                    "hint": "[shell错误·网络类重试多次仍失败] 降级换传输阶梯:"
                            "curl → requests 直连 → 浏览器渲染抓取(fetch_url_smart)·"
                            "或换 API 端点/换 UA。别用同一姿势死磕。"}
        if cat == "script":
            # 脚本逻辑错:不是重试能解·要修脚本本身
            return {"category": "script", "action": "salvage", "should_retry": False,
                    "hint": "[shell错误·脚本自身问题] SyntaxError/ModuleNotFound/文件不存在——"
                            "这不是重试能解的·先修脚本:缺库 pip_install·语法错改代码·"
                            "路径错核对沙箱路径。别原样重跑。"}
        # unknown:给一次重试机会
        return {"category": "unknown", "action": "retry" if attempt <= 1 else "salvage",
                "should_retry": attempt <= 1,
                "hint": f"[shell错误·未分类·exit_code={exit_code}] 看 stderr 判断:"
                        "网络类退避重试·脚本类修代码·环境类别硬磕。"}
    except Exception:
        # fail-safe:决策出错绝不影响 shell_exec 主流程
        return {"category": "error", "action": "pass", "hint": "", "should_retry": False}


def advise_hint(command, exit_code, stdout, stderr, attempt=1):
    """便捷:只返回给 agent 的 hint 串(空=无建议)。shell_exec 拼进 summary 用。"""
    try:
        d = classify_and_advise(command, exit_code, stdout, stderr, attempt=attempt)
        return d.get("hint", "") or ""
    except Exception:
        return ""
