#!/usr/bin/env python3
"""desktop_gui · 系统级桌面 GUI 自动化 (G-gui · 2026-08-08)

用户诉求:「还有gui能力我不能有吗？我也要」——对标 LongHorizon-Harness 的
computer-use 能力(操作任意桌面应用/Office/设计软件，不止浏览器)。核实发现
现有 `interactive_browser_agent.py` 只覆盖浏览器内 DOM 交互(Playwright+CDP)，
系统级"点击任意窗口应用/截图看屏幕/键鼠模拟"这一层确实没有——这是真缺口。

设计取舍(macOS 优先，最小依赖):
  - 不引入 pyautogui/Quartz 等三方库(装依赖 + 需要额外系统权限申请流程)，
    改用 macOS 系统自带的 `osascript`(AppleScript / System Events) +
    `screencapture`，零安装、和 supervisor_of_supervisors.py 里已经在用的
    osascript 告警是同一条已验证可行的技术路线。
  - 权限前提:首次调用会触发 macOS「辅助功能」权限弹窗(System Events 控制
    其他 App 需要用户手动在 系统设置→隐私与安全性→辅助功能 里勾选一次终端
    /Python 解释器)。这是系统级限制，不是本模块的 bug——`is_available()`
    会做能力探测，不可用时工具返回明确指引而不是静默失败。
  - 只做「桌面级」能力，不做浏览器内 DOM 操作(那是 interactive_browser_agent
    的领域，两者互补:浏览器内用已有的，系统级窗口/应用用这个)。

5 类动作(对齐 LongHorizon-Harness "click/type/scroll/browse"的桌面等价物):
  - screenshot(path):          截全屏/指定窗口，返回图片路径(交给 vision 模型看)
  - list_windows():            列当前打开的应用/窗口标题(决策要点哪个窗口前先看清)
  - activate_app(name):        把某个应用带到前台(如"Preview"/"Finder"/"TextEdit")
  - click_at(x, y):            在屏幕绝对坐标点击(配合 screenshot 先看再点)
  - type_text(text):           模拟键盘逐字输入(往当前聚焦的输入框打字)
  - press_key(key):            模拟按键(Return/Tab/Escape/Cmd+S 等组合键)

安全边界(同 hitl_gate.py 的"可控放手"理念，不是"完全禁止"):
  - 不做无差别的「录屏/读取任意窗口内容」——只做用户/agent 主动发起的单次截图
  - 所有动作都记录到 .desktop_gui_actions.jsonl，可追溯
  - click_at/press_key 这类"改变系统状态"的动作，复用 hitl_gate 的审批策略
    (调用方在 qa_auto_agent.py 里接入，本模块只提供能力，不强制审批——审批
    策略集中在 hitl_gate.py 一处，不重复造轮子)

CLI 自测:
  python3 desktop_gui.py --check     · 探测当前环境是否可用(辅助功能权限等)
  python3 desktop_gui.py --smoke     · 截一张屏做完整性验证
"""
import os
import sys
import json
import time
import subprocess
import platform
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.path.join(os.path.expanduser("~/.meyo"), "desktop_gui")
os.makedirs(_STATE_DIR, exist_ok=True)
_ACTION_LOG = os.path.join(_STATE_DIR, ".desktop_gui_actions.jsonl")
_SHOT_DIR = os.path.join(_STATE_DIR, "screenshots")
os.makedirs(_SHOT_DIR, exist_ok=True)

IS_MACOS = platform.system() == "Darwin"


def _log(action: dict):
    action.setdefault("ts", datetime.now().isoformat())
    try:
        with open(_ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(action, ensure_ascii=False) + "\n")
    except Exception:
        pass


def is_screenshot_available() -> dict:
    """探测「屏幕录制」权限(screenshot 只需要这一个，独立于下面的辅助功能权限)。
    macOS 两套权限体系是分开的:截图归屏幕录制管，键鼠模拟/读窗口归辅助功能管。

    2026-08-09 修复一个和权限完全无关的假阴性 bug:探测用的临时文件名之前是
    `.probe.png`(点开头的隐藏文件)。实测发现 macOS 的 `screencapture` 命令
    对"以 . 开头的隐藏文件名"作为输出目标会拒绝写入——命令本身 returncode
    仍是 0，但 stderr 会打印 `cannot write file to intended destination`
    且文件根本不会生成，这和有没有「屏幕录制」权限无关(同一权限状态下,
    换成非隐藏文件名如 `probe.png` 立即截图成功)。之前的实现只看
    returncode==0 and 文件存在 来判定 ok,文件确实没生成所以正确地判了
    ok=False,但报错文案把用户导向"去系统设置开权限"，而真正权限早就是通的，
    用户会被误导去做无用功、或者怀疑自己设置错了。这里换成非隐藏文件名
    避免触发这个系统行为，探测结果才能真实反映权限状态。"""
    if not IS_MACOS:
        return {"ok": False, "reason": "仅支持 macOS，当前平台: " + platform.system()}
    try:
        # 用非隐藏文件名(不以 . 开头)，规避 screencapture 拒绝写隐藏文件的系统行为
        test_path = os.path.join(_STATE_DIR, f"_probe_{os.getpid()}.png")
        r = subprocess.run(["screencapture", "-x", test_path], capture_output=True, text=True, timeout=6)
        ok = r.returncode == 0 and os.path.isfile(test_path)
        stderr = (r.stderr or "").strip()
        try:
            if ok:
                os.remove(test_path)
        except Exception:
            pass
        if not ok:
            if "cannot write file" in stderr.lower():
                # 明确是写入目标问题(如隐藏文件/路径不存在)，不是权限问题，
                # 报更准确的原因,不要误导用户去改系统权限设置。
                return {"ok": False, "reason": f"screencapture 无法写入目标文件(非权限问题): {stderr}"}
            return {"ok": False, "reason": "screencapture 失败，请到 系统设置→隐私与安全性→"
                                            "屏幕录制 里勾选运行本程序的终端/Python，然后重试。"
                                            f"{('(' + stderr + ')') if stderr else ''}"}
        return {"ok": True, "reason": ""}
    except Exception as e:
        return {"ok": False, "reason": f"探测异常: {type(e).__name__}: {e}"}


def is_available() -> dict:
    """探测「辅助功能」权限(click_at/type_text/press_key/list_windows/activate_app
    这些需要 System Events 控制其他 App 的动作都靠这个)。fail-safe:任何异常/超时
    都返回不可用而不是抛出或卡死，调用方据此决定要不要提示用户去开权限。
    超时给到很短(3s):首次未授权时 osascript 可能弹权限对话框，不能让调用方
    的工具函数被这里的探测卡死——宁可误判'不可用'让用户重试，也不能挂起。"""
    if not IS_MACOS:
        return {"ok": False, "reason": "仅支持 macOS(用 osascript/System Events)，"
                                        "当前平台: " + platform.system()}
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of first process'],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "not allowed" in err.lower() or "1002" in err:
                return {"ok": False, "reason": "缺少「辅助功能」权限，请到 系统设置→隐私与安全性→"
                                                "辅助功能 里勾选运行本程序的终端/Python，然后重试。"}
            return {"ok": False, "reason": f"System Events 调用失败: {err[:200]}"}
        return {"ok": True, "reason": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "探测超时(可能是首次调用弹出了权限对话框未处理)，请到 "
                                        "系统设置→隐私与安全性→辅助功能 里手动勾选一次后重试。"}
    except Exception as e:
        return {"ok": False, "reason": f"探测异常: {type(e).__name__}: {e}"}


def screenshot(path: str = "", window_name: str = "") -> dict:
    """截屏。window_name 非空时尝试只截该窗口(先 activate 再截前台窗口)，
    否则截全屏。返回 {ok, path, reason}。只需要「屏幕录制」权限，不依赖
    辅助功能权限(所以比 click_at/type_text 更容易可用)。"""
    avail = is_screenshot_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"]}

    if not path:
        path = os.path.join(_SHOT_DIR, f"shot_{int(time.time()*1000)}.png")
    try:
        if window_name:
            act = activate_app(window_name)
            if not act.get("ok"):
                return act
            time.sleep(0.3)
            # -l 后接窗口ID比较复杂，退化为激活后截全屏(前台即目标窗口，
            # 对大多数单窗口场景足够；多窗口精确定位留作按需增强)。
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0 and os.path.isfile(path)
        result = {"ok": ok, "path": path if ok else "",
                  "reason": "" if ok else (r.stderr or "截图失败").strip()}
        _log({"action": "screenshot", "window_name": window_name, **result})
        return result
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        _log({"action": "screenshot", "window_name": window_name, **result})
        return result


def list_windows() -> dict:
    """列当前所有可见应用进程名(决策"该操作哪个窗口"前先看清现状)。"""
    avail = is_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"], "windows": []}
    try:
        script = ('tell application "System Events" to get name of every process '
                   'whose background only is false')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return {"ok": False, "reason": (r.stderr or "").strip(), "windows": []}
        names = [n.strip() for n in (r.stdout or "").split(",") if n.strip()]
        result = {"ok": True, "reason": "", "windows": names}
        _log({"action": "list_windows", "n": len(names)})
        return result
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "windows": []}


def activate_app(name: str) -> dict:
    """把某个应用带到前台(如 Preview/Finder/TextEdit/Microsoft Excel)。
    应用未打开时 macOS 会尝试启动它。"""
    avail = is_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"]}
    try:
        script = f'tell application "{name}" to activate'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        result = {"ok": ok, "reason": "" if ok else (r.stderr or "").strip()}
        _log({"action": "activate_app", "name": name, **result})
        return result
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        _log({"action": "activate_app", "name": name, **result})
        return result


def click_at(x: int, y: int) -> dict:
    """在屏幕绝对坐标(左上角为原点)模拟一次鼠标左键点击。建议调用前先
    screenshot() 让模型看清当前屏幕状态再决定坐标，不要盲点。"""
    avail = is_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"]}
    try:
        # System Events 的 click 需要坐标以 {x, y} 列表形式传入
        script = (f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        ok = r.returncode == 0
        result = {"ok": ok, "reason": "" if ok else (r.stderr or "").strip()}
        _log({"action": "click_at", "x": x, "y": y, **result})
        return result
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        _log({"action": "click_at", "x": x, "y": y, **result})
        return result


def type_text(text: str) -> dict:
    """往当前聚焦的输入框逐字输入文本(先用 activate_app/click_at 把焦点
    定位到目标输入框，再调用这个)。"""
    avail = is_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"]}
    try:
        # 转义双引号和反斜杠，防止破坏 AppleScript 字符串字面量
        safe = str(text).replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{safe}"'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        result = {"ok": ok, "reason": "" if ok else (r.stderr or "").strip()}
        _log({"action": "type_text", "len": len(text), **result})
        return result
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        _log({"action": "type_text", "len": len(text), **result})
        return result


# 常用按键/组合键名 → AppleScript key code 或 keystroke modifiers 的映射
_KEY_ALIASES = {
    "enter": ("key code", "36"), "return": ("key code", "36"),
    "tab": ("key code", "48"), "escape": ("key code", "53"), "esc": ("key code", "53"),
    "space": ("key code", "49"), "backspace": ("key code", "51"), "delete": ("key code", "51"),
    "up": ("key code", "126"), "down": ("key code", "125"),
    "left": ("key code", "123"), "right": ("key code", "124"),
}


def press_key(key: str) -> dict:
    """模拟按键或组合键。支持: enter/tab/escape/space/backspace/上下左右，
    以及形如 "cmd+s"/"cmd+shift+z" 的组合键(修饰键: cmd/shift/option/ctrl)。"""
    avail = is_available()
    if not avail["ok"]:
        return {"ok": False, "reason": avail["reason"]}
    try:
        key_l = str(key).strip().lower()
        if "+" in key_l:
            parts = key_l.split("+")
            main_key = parts[-1]
            mods = parts[:-1]
            mod_map = {"cmd": "command down", "command": "command down",
                       "shift": "shift down", "option": "option down", "alt": "option down",
                       "ctrl": "control down", "control": "control down"}
            using = ", ".join(mod_map[m] for m in mods if m in mod_map)
            if main_key in _KEY_ALIASES:
                _, code = _KEY_ALIASES[main_key]
                script = f'tell application "System Events" to key code {code} using {{{using}}}'
            else:
                safe = main_key.replace("\\", "\\\\").replace('"', '\\"')
                script = f'tell application "System Events" to keystroke "{safe}" using {{{using}}}'
        elif key_l in _KEY_ALIASES:
            _, code = _KEY_ALIASES[key_l]
            script = f'tell application "System Events" to key code {code}'
        else:
            safe = key_l.replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{safe}"'

        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        ok = r.returncode == 0
        result = {"ok": ok, "reason": "" if ok else (r.stderr or "").strip()}
        _log({"action": "press_key", "key": key, **result})
        return result
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        _log({"action": "press_key", "key": key, **result})
        return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="探测当前环境是否可用")
    ap.add_argument("--smoke", action="store_true", help="截一张屏做完整性验证")
    args = ap.parse_args()

    if args.check:
        r_shot = is_screenshot_available()
        r_a11y = is_available()
        print(json.dumps({"screenshot(屏幕录制权限)": r_shot,
                          "键鼠/窗口(辅助功能权限)": r_a11y}, ensure_ascii=False, indent=2))
        sys.exit(0 if (r_shot["ok"] or r_a11y["ok"]) else 1)

    if args.smoke:
        print("=== desktop_gui smoke ===")
        r_shot = is_screenshot_available()
        print(f"  is_screenshot_available: {r_shot}")
        if r_shot["ok"]:
            r2 = screenshot()
            print(f"  screenshot: ok={r2['ok']}, path={r2.get('path')}")
            assert r2["ok"] and os.path.isfile(r2["path"]), "截图应成功且文件存在"
            print("  ✅ screenshot 通过")
        else:
            print("  ⚠️ 截图权限不可用，跳过(不是代码 bug，是系统权限未授予)")

        r_a11y = is_available()
        print(f"  is_available(辅助功能): {r_a11y}")
        if r_a11y["ok"]:
            r1 = list_windows()
            print(f"  list_windows: ok={r1['ok']}, n={len(r1.get('windows', []))}")
        else:
            print("  ⚠️ 辅助功能权限不可用，跳过 list_windows/click_at 等(需手动到系统设置授权)")
        print("✅ desktop_gui smoke 完成(权限相关跳过属预期,非代码错误)")
