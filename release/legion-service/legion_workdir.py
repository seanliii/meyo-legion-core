#!/usr/bin/env python3.12
"""军团 workdir/state 持续化恢复包(军团自己设计·ROI #2·2026-08-28)。

军团反思共识(dev/legion-self-designed-persistence.md):失败回流现在只给 prior_attempts 文本
(attempt/model/status/error/final[:500]),接力家/续跑轮看不到上一家真做了什么——中间产物路径、
已抓数据、已建文件夹、踩过的坑全丢。修:每家每单一个专属 workdir + state.json,失败后保留,
路径 + 摘要随回流传给接力家(它据此在已有成果上接着干·不失忆重跑)。

依据(harness 源码):
- codex compact.rs record_items:失败整段进 history 喂回下一轮(不是只给摘要)。
- deepseek timeout-policy:结构化错误原样喂回·模型据完整现场自换招。

守边界(context not control):只给 harness 一个"这是你的工作目录·上一家成果在这"的 context,
不替它读文件挑内容写答案。harness 写不写 state.json 由它自己定(不写就退回 prior_attempts
文本回流·零回归)。所以本模块只做:①给出/建目录 ②原子读写 state.json ③给接力家生成摘要。

独立模块(零改现有文件):等并发 session 的 cancel/P0 改动提交后,再在 run_one 加最小接线。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import legion_paths as _paths

_ROOT = "workdir"                       # ~/.meyo/legion/workdir/<qhash>/<key>/
_STATE_FILE = "state.json"
_MAX_LIST = 40                          # steps/artifacts/pitfalls 各自回流上限(防撑爆 context)


def _safe(seg: str) -> str:
    """把 qhash/key 清洗成安全路径段(和 team_state._safe 同口径·不让 ../ 逃逸)。"""
    seg = str(seg or "").strip() or "default"
    return re.sub(r"[^A-Za-z0-9._:-]", "_", seg)[:80]


def under(qhash: str, key: str) -> str:
    """返回(并确保存在)这一单这一家的专属 workdir 绝对路径。

    归一(2026-08-28·和并发 session 的"统一持久工作目录"合并·不重复造):优先复用父层已下放的
    MEYO_WORKDIR(~/.meyo/legion/outputs/<key>_<qhash>/·harness 产物就写这里·被 artifact scan
    自动收集)。让 state.json 和交付产物落在【同一个目录】——harness 只认一个工作目录,接力家读
    同一处既看到产物又看到 state。MEYO_WORKDIR 未设时(测试/父层直调)才退回自建目录(兼容)。
    """
    wd = os.environ.get("MEYO_WORKDIR", "").strip()
    if wd:
        try:
            os.makedirs(wd, exist_ok=True)
            return wd
        except OSError:
            pass
    d = _paths.under(_ROOT, _safe(qhash), _safe(key))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _state_path(qhash: str, key: str) -> str:
    return os.path.join(under(qhash, key), _STATE_FILE)


def write_state(qhash: str, key: str, state: dict) -> bool:
    """原子写 state.json(tmp + os.replace·仿 team_state._write)。绝不抛(落盘失败不影响主流程)。"""
    try:
        path = _state_path(qhash, key)
        payload = dict(state or {})
        payload["updated_ts"] = time.time()
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_state(qhash: str, key: str) -> dict | None:
    try:
        with open(_state_path(qhash, key), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _list_files(qhash: str, key: str, limit: int = _MAX_LIST) -> list[dict]:
    """扫这家 workdir 里的真文件(不含 state.json 自身·空文件不算)。"""
    root = under(qhash, key)
    out: list[dict] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", "node_modules", ".venv", "venv")]
            for fn in filenames:
                if fn == _STATE_FILE or fn.startswith(".") or fn.endswith(".tmp"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size == 0:
                    continue
                out.append({"path": p, "rel": os.path.relpath(p, root), "bytes": size})
                if len(out) >= limit:
                    return out
    except OSError:
        pass
    return out


def summarize_for_relay(qhash: str, failed_key: str) -> str:
    """给接力家的 context:上一家的 workdir 路径 + state 里的计划/已做步骤/产物/坑 + 真文件清单。

    返回一段可拼进 relay_task 的中文文本。没有 state 也没有文件→返回 ""(调用方据此不追加·零回归)。
    这只是【把上一家的真实现场如实交给接力家】(context),接力家自己决定怎么用(not control)。
    """
    st = read_state(qhash, failed_key) or {}
    files = _list_files(qhash, failed_key)
    if not st and not files:
        return ""
    lines = [f"\n\n【接力现场·{failed_key} 的工作目录已保留,你可以直接读它续做,别从零重来】",
             f"工作目录:{under(qhash, failed_key)}"]

    def _bullets(title, items):
        items = [str(x).strip() for x in (items or []) if str(x).strip()][:_MAX_LIST]
        if items:
            lines.append(title)
            lines.extend(f"  - {x}" for x in items)

    if st.get("plan"):
        lines.append(f"上一家的计划:{str(st['plan'])[:800]}")
    _bullets("已完成的步骤:", st.get("steps_done"))
    _bullets("踩过的坑(别重复):", st.get("pitfalls"))
    # 产物:优先 state 里 harness 自己登记的·再补真文件扫描(harness 没登记也能看到)
    art = st.get("artifacts") or []
    art_lines = []
    for a in art[:_MAX_LIST]:
        if isinstance(a, dict) and a.get("path"):
            art_lines.append(f"{a['path']}" + (f" ({a.get('kind')})" if a.get('kind') else ""))
        elif isinstance(a, str):
            art_lines.append(a)
    seen = {a.get("path") if isinstance(a, dict) else a for a in art}
    for f in files:
        if f["path"] not in seen:
            art_lines.append(f"{f['path']} ({f['bytes']}B)")
    if art_lines:
        lines.append("已产出的文件(可直接读/续写·别重造):")
        lines.extend(f"  - {x}" for x in art_lines[:_MAX_LIST])
    if st.get("last_action"):
        lines.append(f"上一家最后在做:{str(st['last_action'])[:300]}")
    return "\n".join(lines)


def cleanup(qhash: str) -> None:
    """整单结束后清这一单所有家的 workdir(可选·避免磁盘增长)。绝不抛。"""
    import shutil
    try:
        d = _paths.under(_ROOT, _safe(qhash))
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
