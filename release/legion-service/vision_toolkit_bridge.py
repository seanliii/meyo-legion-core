#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vision_toolkit_bridge:ecosystem dsh-vision-toolkit 脚本工具无状态直调桥(阶段2).

用户2026-08-30:各家源码都在本仓库·不止CLI→能像 auto 一样抽工具直调。
dsh-vision-toolkit 的 scripts/*.py 是【纯 argparse 命令行工具】(PIL 实现·IO 是文件路径),
天然无状态·不依赖任何 harness runtime/session→军团任何家可直调(不派家)。

对照 capstack_bridge(auto 走 t_+stub session):这里走 file_per_script + subprocess,
因为脚本本就设计成"输入文件路径·输出结构化文本"的命令行工具·subprocess 是它们的原生契约。

真能力(补军团视觉短板·非空壳·已真跑验证):
- dominant_colors: 取图片区域主色 + 从候选色板定值(设计还原/配色核对)
- pixel_diff: 对比重建图 vs 原图·排序最差区域(视觉验收·找哪块画错了)
- extract_fg: 从截图抠图标/logo 前景(透明 PNG·连通分量分离)
"""
from __future__ import annotations
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 优先用 vendor 下的稳定副本(与 skills 下同源·vendor 是打平后的可直调版)
_SCRIPTS_DIR = os.path.join(
    _REPO, "harness_baselines", "ecosystem", "dsh-vision-toolkit",
    "vendor", "agent-vision-toolkit", "skills", "vision-tools", "scripts")

# 每个脚本 = 一个工具(file_per_script 约定)。value=位置参数 + 描述 + 是否纯无状态。
# offline=True:纯本地 PIL 计算·无外部依赖·真无状态直调。
# offline=False:依赖云端 VISION/glance 配置·直调需先配好凭证(诚实标注·不假装无状态)。
_SCRIPTS = {
    "dominant_colors": {"positional": ["image"], "offline": True,
                        "desc": "取图片区域主色/从候选色板定精确值(设计还原·配色核对)"},
    "pixel_diff": {"positional": ["original", "rebuilt"], "offline": True,
                   "desc": "对比重建图vs原图·排序最差区域(视觉验收·定位画错的块)"},
    "extract_fg": {"positional": ["image"], "offline": True,
                   "desc": "从截图抠图标/logo前景(透明PNG·8邻域连通分量分离)"},
    "html_shot": {"positional": ["html"], "offline": True,
                  "desc": "把HTML渲染成截图PNG(交付网页可视化验收)"},
    "long_screenshot_ocr": {"positional": ["image"], "offline": False,
                            "desc": "长截图分段OCR(需云端VISION/glance配置)"},
}


def available() -> bool:
    return os.path.isdir(_SCRIPTS_DIR) and any(
        os.path.isfile(os.path.join(_SCRIPTS_DIR, f"{n}.py")) for n in _SCRIPTS)


def list_bridged_tools() -> list[str]:
    """列出真实存在脚本文件的工具(不谎报·文件在才算)。"""
    return sorted(n for n in _SCRIPTS
                  if os.path.isfile(os.path.join(_SCRIPTS_DIR, f"{n}.py")))


def tool_meta() -> list[dict]:
    """给军团发现用:工具名+描述+来源。offline=True 标真无状态直调·False 标需云配置(诚实)。"""
    return [{"tool": n, "desc": _SCRIPTS[n]["desc"], "harness": "dsh-vision",
             "call": "direct", "via": "vision_toolkit_bridge",
             "offline": _SCRIPTS[n]["offline"],
             "needs_config": "VISION/glance" if not _SCRIPTS[n]["offline"] else None}
            for n in list_bridged_tools()]


def call(tool_name: str, params: dict | None = None, timeout_s: int = 120) -> dict:
    """无状态直调 dsh-vision 脚本工具(subprocess·脚本原生契约)。

    params:
      - 位置参数按 _SCRIPTS[tool]["positional"] 顺序取(如 image=路径)
      - 其余键→ --key value(如 region="0,0,120,80" → --region 0,0,120,80)
      - bool True → 只加 --key(flag);False/None → 跳过
    返回 {ok, result(脚本stdout), tool, via}。
    """
    params = params or {}
    if tool_name not in _SCRIPTS:
        return {"ok": False, "error": f"dsh-vision无工具 {tool_name}",
                "hint": f"可直调: {list_bridged_tools()}"}
    script = os.path.join(_SCRIPTS_DIR, f"{tool_name}.py")
    if not os.path.isfile(script):
        return {"ok": False, "error": f"脚本文件缺失: {script}", "tool": tool_name}

    spec = _SCRIPTS[tool_name]
    cmd = [sys.executable, script]
    # 1) 位置参数(按声明顺序)
    for key in spec["positional"]:
        val = params.get(key) or params.get("path") or params.get("image")
        if val:
            cmd.append(str(val))
    # 2) 其余 kwargs → --flag(排除已用作位置的键)
    used = set(spec["positional"]) | {"path", "image"}
    for key, val in params.items():
        if key in used or val is None or val is False:
            continue
        flag = "--" + key.replace("_", "-")
        if val is True:
            cmd.append(flag)
        else:
            cmd += [flag, str(val)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        ok = proc.returncode == 0
        return {"ok": ok, "result": (proc.stdout or "").strip(),
                "stderr": (proc.stderr or "").strip() if not ok else "",
                "tool": tool_name, "via": "vision_toolkit_bridge",
                "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{tool_name} 超时({timeout_s}s)", "tool": tool_name}
    except Exception as e:
        return {"ok": False, "error": f"直调 {tool_name} 失败: {type(e).__name__}: {e}",
                "tool": tool_name}


if __name__ == "__main__":
    print("dsh-vision 桥可用:", available())
    print("可直调工具:", list_bridged_tools())
    if len(sys.argv) > 2:
        print(call(sys.argv[1], {"image": sys.argv[2]}))
