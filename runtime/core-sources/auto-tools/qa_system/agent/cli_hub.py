#!/usr/bin/env python3
"""cli_hub · CLI-Anything 生态桥接 (G-cli · 2026-08-08)

用户「是不是有cli anything？还有哪些可以使用到gui的skill和工具可以使用软件」——
调研 https://github.com/HKUDS/CLI-Anything (46.8k star) 后确认:它是比
desktop_gui.py(截图猜坐标点击) 更成熟的 GUI 软件自动化路线——给每个真实软件包一层
**命令行接口**(走软件自带的脚本 API/宏/文件格式)，而不是模拟鼠标键盘。已验证真实
可装可用，注册表(40+ CLI)覆盖 GIMP/Blender/FreeCAD(258命令)/LibreOffice/Krita/
Obsidian/Zoom/QGIS/MuseScore/n8n 等主流软件。

两条 GUI 自动化路线互补，优先级:
  1. 优先走 CLI-Anything(本模块):有对应软件的封装 → 稳定、确定性强、不受界面改版影响
  2. 兜底走 desktop_gui.py(坐标点击):没有现成 CLI 封装的软件 → 截图看+模拟点击

设计(复用 cli-anything-hub 包管理器，不重复造轮子):
  - 依赖: pip install cli-anything-hub (已装进 .venv_autoagent)
  - search(query):  cli-hub search --json，按关键词/软件名找已注册的 CLI 封装
  - can(task):       cli-hub can --json，按"做什么事"(如"转换PDF"/"渲染3D模型")
                      找能完成这个能力的 CLI(比 search 更贴近任务意图)
  - install(name):   cli-hub install <name>，装某个具体 CLI 封装(会拉取对应
                      pip/npm/brew 包，可能需要目标软件本身也已安装)
  - run(name, args):  cli-hub launch <name> <args...>，调用已装 CLI 执行具体操作

安全边界: install/run 都是 subprocess 调用，有超时保护；run 的参数原样透传给
目标 CLI，调用方(qa_auto_agent.py)按需接入 hitl_gate 审批(比如会修改文件/发送
消息的调用)。本模块只负责"调得动、调得安全(超时+输出截断)"，不做业务语义判断。
"""
import os
import sys
import json
import shutil
import subprocess

# 复用运行 qa_auto_agent.py 的同一个虚拟环境(里面已经装了 cli-anything-hub)，
# 不依赖调用方进程的 PATH——直接找 sys.executable 同目录下的 cli-hub 可执行文件。
_BIN_DIR = os.path.dirname(sys.executable)
_CLI_HUB_BIN = os.path.join(_BIN_DIR, "cli-hub")
if not os.path.isfile(_CLI_HUB_BIN):
    _CLI_HUB_BIN = shutil.which("cli-hub") or "cli-hub"  # 退化到 PATH 查找


def is_available() -> dict:
    """探测 cli-hub 是否已安装可用。fail-safe:异常/超时都返回不可用。"""
    try:
        r = subprocess.run([_CLI_HUB_BIN, "--version"], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return {"ok": False, "reason": (r.stderr or "cli-hub --version 失败").strip()[:200]}
        return {"ok": True, "reason": "", "version": (r.stdout or "").strip()}
    except FileNotFoundError:
        return {"ok": False, "reason": "cli-hub 未安装。请先: pip install cli-anything-hub"}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def search(query: str, timeout_s: float = 15.0) -> dict:
    """按关键词/软件名搜索已注册的 CLI 封装(名称/描述/分类里模糊匹配)。
    返回 {ok, items:[{name, description, category, install_cmd, ...}], reason}。"""
    try:
        r = subprocess.run([_CLI_HUB_BIN, "search", query, "--json"],
                          capture_output=True, text=True, timeout=timeout_s)
        if r.returncode != 0:
            return {"ok": False, "items": [], "reason": (r.stderr or "search 失败").strip()[:300]}
        items = json.loads(r.stdout or "[]")
        return {"ok": True, "items": items if isinstance(items, list) else [], "reason": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "items": [], "reason": f"search 超时(>{timeout_s}s)"}
    except json.JSONDecodeError as e:
        return {"ok": False, "items": [], "reason": f"json解析失败: {e}"}
    except Exception as e:
        return {"ok": False, "items": [], "reason": f"{type(e).__name__}: {e}"}


def can(task_desc: str, timeout_s: float = 15.0) -> dict:
    """按"要完成的任务"(自然语言,如'转换PDF为图片'/'渲染3D模型')找能做这事的 CLI，
    比 search(关键词匹配)更贴近任务意图。返回 {ok, items, reason}——注意 cli-hub can
    在"没匹配到任何能力"时会返回非零退出码(不代表命令本身失败),这里按 stdout 是否
    能正常解析出 JSON 结构来判断"调用成功"，退出码只影响 reason 提示，不影响 ok。"""
    try:
        r = subprocess.run([_CLI_HUB_BIN, "can", task_desc, "--json"],
                          capture_output=True, text=True, timeout=timeout_s)
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "items": [], "reason": (r.stderr or "can 输出非json").strip()[:300]}
        items = data.get("matched_capabilities", []) if isinstance(data, dict) else []
        # 调用本身(能正常解析出结构)算成功；items 为空只是"没找到匹配的能力"，
        # 不是调用失败——reason 留空,调用方看 items 长度自己判断有没有找到。
        return {"ok": True, "items": items if isinstance(items, list) else [],
                "reason": "" if items else "没有匹配的能力(可换个描述或用 search 按关键词试试)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "items": [], "reason": f"can 超时(>{timeout_s}s)"}
    except Exception as e:
        return {"ok": False, "items": [], "reason": f"{type(e).__name__}: {e}"}


def install(name: str, timeout_s: float = 180.0) -> dict:
    """安装某个具体 CLI 封装(拉取对应 pip/npm/brew 包)。这一步可能耗时较长
    (装依赖包)，超时给到 180s。注意:大部分 CLI 封装还要求目标软件本身已经
    装在本机(比如 gimp 封装需要真的装了 GIMP.app)，装 CLI 封装本身不等于
    装好了底层软件——安装失败/运行报错时提示信息通常会说明缺什么。"""
    try:
        r = subprocess.run([_CLI_HUB_BIN, "install", name],
                          capture_output=True, text=True, timeout=timeout_s)
        stdout = (r.stdout or "")[:3000]
        stderr = (r.stderr or "")[:1500]
        ok = r.returncode == 0
        return {"ok": ok, "stdout": stdout, "stderr": stderr,
                "reason": "" if ok else "安装失败，见 stderr"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "",
                "reason": f"安装超时(>{timeout_s}s)，该CLI可能依赖较大或网络较慢"}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": "", "reason": f"{type(e).__name__}: {e}"}


def run(name: str, args: list = None, timeout_s: float = 120.0) -> dict:
    """执行已安装的 CLI，args 是要透传给目标 CLI 的参数列表(比如
    ["--help"] 或 ["export", "--format", "pdf", "in.odt"])。
    返回 {ok, stdout, stderr, returncode, reason}，stdout/stderr 各截断到合理长度
    防止刷屏撑爆上下文。"""
    args = args or []
    try:
        r = subprocess.run([_CLI_HUB_BIN, "launch", name, *args],
                          capture_output=True, text=True, timeout=timeout_s)
        stdout = (r.stdout or "")[:4000]
        stderr = (r.stderr or "")[:2000]
        ok = r.returncode == 0
        return {"ok": ok, "stdout": stdout, "stderr": stderr, "returncode": r.returncode,
                "reason": "" if ok else f"exit_code={r.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "", "returncode": -1,
                "reason": f"执行超时(>{timeout_s}s)，请拆分成更小的步骤"}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": "", "returncode": -1,
                "reason": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    print("=== cli_hub smoke ===")
    avail = is_available()
    print(f"  is_available: {avail}")
    assert avail["ok"], "cli-hub 应已安装(pip install cli-anything-hub)"
    print("  ✅ is_available 通过")

    r1 = search("pdf")
    print(f"  search('pdf'): ok={r1['ok']}, n_items={len(r1.get('items', []))}")
    assert r1["ok"] and len(r1["items"]) > 0, "搜索pdf相关CLI应有结果(如libreoffice)"
    print("  ✅ search 通过")

    r2 = can("edit image")
    print(f"  can('edit image'): ok={r2['ok']}, n_items={len(r2.get('items', []))}")
    print("  ✅ can 调用完成(结果条数视注册表匹配度而定)")

    print("✅ cli_hub smoke 全部通过")
