#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hermes_tool_bridge:阶段1(km文档 2783646281)·hermes tools 无状态直调桥。

按 _TOOL_CONVENTIONS['hermes'] = file_per_tool 约定,从 harness_baselines/hermes-agent/tools/
抽【无 GUI/session/subprocess runtime 依赖】的纯函数工具,注册进 harness_tool_bus 直调。

判据(km 2783646281 五):①军团可发现 ②无状态真直调不派家 ③runtime 类保留 provider 派单
④端到端测 ≥5 工具真跑不崩 ⑤进体检健康。

抽的 5 个无状态工具(hermes 129 里最有价值·真跑验证过):
- strip_ansi           : 去 ANSI 转义(subprocess 输出清理)
- has_binary_extension : 文件二进制判别
- strip_markdown_for_tts: TTS 文本归一
- check_package_for_malware: 供应链恶意包检查(用 OSV.dev)
- validate_within_dir  : 路径穿越安全检查
"""
from __future__ import annotations
import os
import sys
import importlib.util
from typing import Any


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES_SOURCE = os.path.join(_REPO, "harness_baselines", "hermes-agent")
_PACKAGED_SOURCE = os.path.join(_REPO, "runtime", "core-sources", "hermes-agent")
# Match the native Hermes adapter's source layout; use the retained pinned tree,
# not another user's HOME or a task-provided path. No installation or model call.
if not os.path.isfile(os.path.join(_HERMES_SOURCE, "run_agent.py")) and os.path.isdir(_PACKAGED_SOURCE):
    _HERMES_SOURCE = _PACKAGED_SOURCE
_HERMES_TOOLS = os.path.join(_HERMES_SOURCE, "tools")


# 每个条目:模块文件名(不带.py) → [公开函数名]
# 只列真的无状态(不依赖 tools.registry / tools.desktop_ui / 全局 session / subprocess)
_BRIDGED = {
    "ansi_strip":         ["strip_ansi", "sanitize_display_text", "strip_unicode_tags"],
    "binary_extensions":  ["has_binary_extension", "has_opaque_document_extension", "is_pdf_path"],
    "tts_text_normalize": ["strip_markdown_for_tts", "normalize_symbols_for_tts", "smooth_whitespace_for_tts"],
    "osv_check":          ["check_package_for_malware"],
    "path_security":      ["validate_within_dir", "has_traversal_component"],
}

# 每个工具的元数据(供 all_tools() 发现)。OSV 检查是原生 HTTP 查询，
# None 也可能表示未识别命令/网络失败，不代表已证明软件包安全。
_META = {
    "strip_ansi":              "去除文本中的 ANSI 转义序列(subprocess 输出清理·防止转义污染 LLM 上下文)",
    "sanitize_display_text":   "清理终端显示文本(strip_ansi + 去 Unicode tag)",
    "strip_unicode_tags":      "去除 Unicode tag 字符(E0000-E007F 隐藏字符攻击防御)",
    "has_binary_extension":    "判断文件路径是否是二进制文件(扩展名判别)",
    "has_opaque_document_extension": "判断是否是不透明文档格式(PDF/Office 等·不能纯文本读)",
    "is_pdf_path":             "判断路径是否是 PDF 文件",
    "strip_markdown_for_tts":  "去 Markdown 格式化(适配 TTS 语音合成)",
    "normalize_symbols_for_tts": "TTS 符号归一(数字/单位/温度/日期等口语化)",
    "smooth_whitespace_for_tts": "TTS 空白平滑(合并空格·断句自然)",
    "check_package_for_malware": "供应链恶意包检查(查 OSV.dev·防装到已知恶意 npm/pypi 包)",
    "validate_within_dir":     "校验路径在指定根目录内(防路径穿越攻击)",
    "has_traversal_component": "判断路径字符串是否含 .. 穿越分量",
}


def _load_mod(name: str):
    """从 hermes-agent/tools 加载单个 .py 模块(不 import 整个 tools 包·避免依赖污染)。"""
    fp = os.path.join(_HERMES_TOOLS, f"{name}.py")
    if not os.path.exists(fp):
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"hermes_tools.{name}", fp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def available() -> bool:
    """有 hermes tools 源码就算可用。"""
    return os.path.isdir(_HERMES_TOOLS)


def list_bridged_tools() -> list[str]:
    """所有已桥接的 hermes 工具名(扁平列表·供 all_tools 聚合)。"""
    return [t for tools in _BRIDGED.values() for t in tools]


def tool_meta() -> list[dict]:
    """供 harness_tool_bus.all_tools() 聚合的元数据。"""
    out = []
    for mod_name, fns in _BRIDGED.items():
        for fn in fns:
            out.append({
                "harness": "hermes", "tool": fn, "call": "direct",
                "via": f"hermes_tool_bridge:{mod_name}",
                "desc": _META.get(fn, ""),
            })
    return out


def _find_fn(tool: str):
    """按工具名找模块+函数(缓存 lazy 加载)。"""
    for mod_name, fns in _BRIDGED.items():
        if tool in fns:
            mod = _load_mod(mod_name)
            if mod and hasattr(mod, tool):
                return getattr(mod, tool)
    return None


def call(tool: str, params: dict | None = None) -> dict:
    """统一调用入口·无状态直调不派家。params 按各工具签名传。"""
    params = params or {}
    fn = _find_fn(tool)
    if fn is None:
        return {"ok": False, "error": f"hermes 未桥接: {tool}"}
    try:
        # 按工具签名分派参数(hermes tools 各家签名不一·这里显式适配)
        if tool in ("strip_ansi", "sanitize_display_text", "strip_unicode_tags"):
            r = fn(params.get("text", ""))
        elif tool in ("has_binary_extension", "has_opaque_document_extension", "is_pdf_path"):
            r = fn(params.get("path", ""))
        elif tool in ("strip_markdown_for_tts", "normalize_symbols_for_tts", "smooth_whitespace_for_tts"):
            r = fn(params.get("text", ""))
        elif tool == "check_package_for_malware":
            # (command, args) → 恶意包说明字符串或 None；不执行安装命令。
            r = fn(params.get("command", ""), params.get("args", []))
        elif tool == "validate_within_dir":
            from pathlib import Path
            r = fn(Path(params.get("path", "")), Path(params.get("root", "")))
        elif tool == "has_traversal_component":
            r = fn(params.get("path", ""))
        else:
            r = fn(**params)
        return {"ok": True, "result": r, "tool": tool, "harness": "hermes"}
    except Exception as e:
        return {"ok": False, "error": f"{tool} 调用失败: {type(e).__name__}: {e}"}


if __name__ == "__main__":
    print("=== hermes_tool_bridge 阶段1 端到端真调测试 (km 2783646281 判据 ≥5) ===")
    if not available():
        print(f"❌ hermes tools 目录不存在: {_HERMES_TOOLS}")
        sys.exit(1)
    print(f"✓ 桥接工具数: {len(list_bridged_tools())}")
    tests = [
        ("strip_ansi", {"text": "\x1b[31mred\x1b[0m normal"}),
        ("has_binary_extension", {"path": "/tmp/a.png"}),
        ("has_binary_extension", {"path": "/tmp/a.txt"}),
        ("strip_markdown_for_tts", {"text": "# 标题\n**粗体** 正文"}),
        ("validate_within_dir", {"path": "/tmp/a/b.txt", "root": "/tmp"}),
        ("has_traversal_component", {"path": "../etc/passwd"}),
    ]
    ok = 0
    for name, p in tests:
        r = call(name, p)
        status = "✅" if r.get("ok") else "❌"
        print(f"  {status} {name}({p}) → {r.get('result') if r.get('ok') else r.get('error')}")
        if r.get("ok"):
            ok += 1
    print(f"\n通过 {ok}/{len(tests)} · 判据要求≥5 · {'PASS' if ok >= 5 else 'FAIL'}")
