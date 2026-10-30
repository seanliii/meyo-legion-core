#!/usr/bin/env python3
"""
context_store · 上下文工程地基层 (v1)

用户抓正 (2026-08-06):
「除了tool，其他他们的核心优势你也得学习啊」——深挖 deepagents 实际源码
(middleware/_message_eviction.py + _overflow_clip.py) 发现: DeepAgents/Claude
Code 真正的护城河不是"工具搜索"，而是一整套**上下文工程**——任何工具产出超过
阈值，不是截断/硬塞进对话，而是:
    1. 全量落盘到虚拟文件系统 (backend.write)
    2. 回给模型的只是 head+tail 预览 + 落盘路径指针
    3. 模型需要细节时，自己 read_file(path, offset, limit) 分页读回来

现状问题: evolution 280+ 模块互相调用/上总线时，大产出(比如 deep_research 抓回
几万字、search 命中几十条证据)直接原样怼给下游/LLM，没有这层"卸载-预览-按需读回"
的抽象，是长任务爆 context、爆 token 的根因之一。

本模块提供跟 backend 无关的最小实现 (用本地文件系统当 backend，理念对齐但不
依赖 langgraph/deepagents，任何 evolution 脚本、agent_comm 总线、qa_auto_agent
都能直接 import 使用)：

    from context_store import offload, read_back, maybe_offload

    # 1. 主动落盘 (明确知道内容大，直接落)
    ref = offload("deep_research", "研究结果全文...", tag="dr_result")
    # ref = {"ref_id": "...", "path": "...", "preview": "...(head+tail)...", "size": N}

    # 2. 自动判断 (超过阈值才落盘，否则原样返回)
    summary_or_full = maybe_offload("search_agent", hits_text)

    # 3. 下游/LLM 需要细节时按需读回 (支持分页，不会一次性又撑爆)
    chunk = read_back(ref_id, offset=0, limit=200)

设计原则 (对齐 deepagents 但去 langgraph 依赖):
- 落盘目录: v11/.context_store/{ref_id}.txt (ref_id = 短 hash，防冲突)
- 预览: head N 行 + tail N 行 + 中间省略提示 (跟 deepagents 的
  _create_content_preview 同构)
- 元数据index: v11/.context_store_index.jsonl (谁存的/多大/何时/tag)，供
  cleanup 和 dashboard 用
- 自动过期清理: 默认 7 天，防止无限堆积磁盘
"""
import hashlib
import json
import os
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
V11 = os.path.dirname(_DIR)
_STORE_DIR = os.path.join(V11, ".context_store")
_INDEX_LOG = os.path.join(V11, ".context_store_index.jsonl")

# 超过这个字符数才自动落盘 (对齐 deepagents 20_000 chars ≈ 5000 token 的量级，
# 但我们全系统很多输出偏长文本，阈值放宽到 8000 字符 ≈ 2000 token)
AUTO_OFFLOAD_THRESHOLD_CHARS = 8000

HEAD_LINES = 8
TAIL_LINES = 8

TOO_LARGE_NOTICE = """[内容过大已落盘 · ref_id={ref_id} · 原始大小={size}字 · 来源={source}]
可用 context_store.read_back('{ref_id}', offset=0, limit=200) 分页读取完整内容。
预览(头{head_n}行+尾{tail_n}行，省略中间):
{preview}
"""


def _ensure_dir():
    os.makedirs(_STORE_DIR, exist_ok=True)


def _make_ref_id(source: str, content: str) -> str:
    h = hashlib.sha1(f"{source}:{time.time()}:{len(content)}".encode()).hexdigest()[:12]
    return f"{source[:20]}_{h}"


def _create_preview(content: str, head_lines: int = HEAD_LINES, tail_lines: int = TAIL_LINES) -> str:
    """head+tail 预览，跟 deepagents._create_content_preview 同构。"""
    lines = content.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return "\n".join(l[:500] for l in lines)
    head = "\n".join(l[:500] for l in lines[:head_lines])
    tail = "\n".join(l[:500] for l in lines[-tail_lines:])
    omitted = len(lines) - head_lines - tail_lines
    return f"{head}\n... [{omitted} 行省略] ...\n{tail}"


def offload(source: str, content: str, tag: str = "") -> dict:
    """强制落盘一段内容，返回引用信息 (ref_id/path/preview/size)。

    Args:
        source: 谁产生的这段内容 (模块名/agent名)，用于 ref_id 前缀和审计
        content: 完整内容 (字符串)
        tag: 可选分类标签 (如 "search_hits" / "deep_research" / "narrative")

    Returns:
        {ref_id, path, preview, size, notice} · notice 是可以直接塞给 LLM/
        下游的完整提示文本 (含预览 + 读回方法)
    """
    _ensure_dir()
    content = str(content)
    ref_id = _make_ref_id(source, content)
    path = os.path.join(_STORE_DIR, f"{ref_id}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        # fail-safe: 落盘失败就不省略，直接把原内容返回 (调用方自己截断)
        return {"ref_id": "", "path": "", "preview": content[:2000],
                "size": len(content), "notice": content[:2000],
                "offload_failed": str(e)}

    preview = _create_preview(content)
    notice = TOO_LARGE_NOTICE.format(
        ref_id=ref_id, size=len(content), source=source,
        head_n=HEAD_LINES, tail_n=TAIL_LINES, preview=preview,
    )

    # 落审计索引 (append-only，供 dashboard/cleanup 用)
    try:
        rec = {"ts": time.time(), "ref_id": ref_id, "source": source, "tag": tag,
               "size": len(content), "path": path}
        with open(_INDEX_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return {"ref_id": ref_id, "path": path, "preview": preview,
            "size": len(content), "notice": notice}


def maybe_offload(source: str, content: str, tag: str = "",
                   threshold: int = AUTO_OFFLOAD_THRESHOLD_CHARS) -> str:
    """自动判断: 超过阈值才落盘返回摘要通知，否则原样返回。

    这是最常用的入口——任何模块产出一段文本，想传给下游/总线/LLM之前，
    过一遍这个函数就自动获得"大结果保护"，不用每处手写判断逻辑。

    Returns:
        str: 原文 (未超阈值) 或者落盘提示 notice (超阈值)
    """
    content = str(content)
    if len(content) <= threshold:
        return content
    ref = offload(source, content, tag=tag)
    return ref.get("notice", content[:2000])


def read_back(ref_id: str, offset: int = 0, limit: int = 200) -> dict:
    """按需分页读回落盘内容 (行级分页，对齐 deepagents read_file 语义)。

    Args:
        ref_id: offload() 返回的 ref_id
        offset: 起始行号 (0-based)
        limit: 读取行数

    Returns:
        {ok, content, total_lines, has_more, next_offset}
    """
    path = os.path.join(_STORE_DIR, f"{ref_id}.txt")
    if not os.path.exists(path):
        return {"ok": False, "error": f"ref_id '{ref_id}' 不存在或已过期清理",
                "content": "", "total_lines": 0, "has_more": False}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "content": "", "total_lines": 0, "has_more": False}

    total = len(lines)
    chunk = lines[offset:offset + limit]
    has_more = offset + limit < total
    return {
        "ok": True,
        "content": "\n".join(chunk),
        "total_lines": total,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


def read_back_full(ref_id: str) -> str:
    """一次性读回全文 (谨慎用——正是为了避免这种用法才做的分页，仅供内部
    需要完整重建原文的场景，比如离线批处理)。"""
    path = os.path.join(_STORE_DIR, f"{ref_id}.txt")
    if not os.path.exists(path):
        return ""
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def cleanup_expired(max_age_days: int = 7) -> dict:
    """清理超过 max_age_days 的落盘文件，防止磁盘无限堆积。
    供 cron / evolution_tool_registry 定时调用。"""
    if not os.path.exists(_STORE_DIR):
        return {"removed": 0, "kept": 0}
    cutoff = time.time() - max_age_days * 86400
    removed, kept = 0, 0
    for fn in os.listdir(_STORE_DIR):
        p = os.path.join(_STORE_DIR, fn)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
            else:
                kept += 1
        except Exception:
            continue
    return {"removed": removed, "kept": kept}


def store_stats() -> dict:
    """查看当前落盘统计 (总文件数/总大小/最近几条)。"""
    if not os.path.exists(_STORE_DIR):
        return {"n_files": 0, "total_size": 0, "recent": []}
    files = os.listdir(_STORE_DIR)
    total_size = 0
    for fn in files:
        try:
            total_size += os.path.getsize(os.path.join(_STORE_DIR, fn))
        except Exception:
            pass
    recent = []
    if os.path.exists(_INDEX_LOG):
        try:
            lines = open(_INDEX_LOG, encoding="utf-8").read().strip().splitlines()
            recent = [json.loads(l) for l in lines[-10:] if l.strip()]
        except Exception:
            pass
    return {"n_files": len(files), "total_size": total_size, "recent": recent}


# ══════════════════════════════════════════════════════════════════
# 注册为 evolution_tool_registry 工具 (供 auto agent 发现 + 调用)
# ══════════════════════════════════════════════════════════════════

def _register_self():
    try:
        from evolution_tool_registry import register_evolution_tool

        @register_evolution_tool(
            name="context_read_back",
            description="分页读回之前被落盘卸载的大结果全文(超过8000字的产出会自动落盘)",
            category="meta",
            cost_tier="cheap",
            search_hint="read back large result offload context store paginate",
            module="context_store",
            is_deferred=True,
        )
        def _read_back_tool(params):
            ref_id = params.get("ref_id", "")
            if not ref_id:
                return {"ok": False, "error": "缺少 ref_id 参数"}
            r = read_back(ref_id, offset=params.get("offset", 0), limit=params.get("limit", 200))
            return {"ok": r.get("ok", False), "result": r}

        @register_evolution_tool(
            name="context_store_stats",
            description="查看上下文卸载存储统计(多少大结果被落盘/总大小)",
            category="monitor",
            cost_tier="cheap",
            search_hint="context store stats disk usage offload",
            module="context_store",
            is_deferred=True,
        )
        def _stats_tool(params):
            return {"ok": True, "result": store_stats()}
    except Exception:
        pass


# (2026-08-27 生产/测评分界) 纯 context_store 被 agent_comm 导入时不再自动注册
# evolution 工具——避免通讯/大结果卸载顺带拉入整套测评进化系统。
# evolution_tool_impls 作为明确插件入口会调用 _register_self()；存储层本身保持零副作用。


if __name__ == "__main__":
    import sys
    if "--stats" in sys.argv:
        print(json.dumps(store_stats(), ensure_ascii=False, indent=2))
    elif "--cleanup" in sys.argv:
        print(json.dumps(cleanup_expired(), ensure_ascii=False, indent=2))
    elif "--test" in sys.argv:
        big_content = "\n".join(f"line {i}: some content here that is reasonably long to pad size" for i in range(500))
        print(f"原始大小: {len(big_content)} 字符")
        ref = offload("test_source", big_content, tag="smoke_test")
        print(f"落盘 ref_id: {ref['ref_id']}")
        print(f"预览:\n{ref['preview'][:300]}...")
        back = read_back(ref["ref_id"], offset=0, limit=10)
        print(f"读回前10行 ok={back['ok']}, total_lines={back['total_lines']}, has_more={back['has_more']}")
        # maybe_offload 测试
        small = maybe_offload("test", "短内容不会被落盘")
        assert small == "短内容不会被落盘", "小内容不该被落盘"
        big_notice = maybe_offload("test", big_content)
        assert "已落盘" in big_notice, "大内容应该触发落盘通知"
        print("✅ context_store 自测通过")
    else:
        print("用法: --test | --stats | --cleanup")
