#!/usr/bin/env python3
"""
tool_forge_loop · 工具自优化/自生成闭环

对齐 Claude Code (claude-code-best/claude-code) 的 skill_synthesizer +
search_skill_forger 理念，但在更高层级闭环:

  监控进化日志 → 识别高频重复模式 → 自动产生 executor_code → forge_new_tool 注册

工作流程:
  1. scan_recurring_patterns: 从 .agent_bus.jsonl / tool_log / tick_reflections
     里扫描反复出现的操作模式 (bash 管道、固定 import 组合、重复的参数模板)
  2. generate_executor: 为高频模式自动生成一段 Python executor 函数代码
  3. forge: 调 evolution_tool_registry.forge_new_tool 固化成命名工具
  4. 验证: 立刻 execute 一次，确认新工具能跑

设计原则:
  - 最小侵入: 不改现有脚本，只读日志/总线
  - 安全第一: executor_code 在受限命名空间执行 (forge_new_tool 已做)
  - 幂等: 同一个 pattern_key 不会被重复锻造
  - 可观测: 每次锻造都上总线 + 落 forge_log
"""
import json
import os
import re
import time
from collections import Counter, defaultdict
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
V11 = os.path.dirname(_DIR)

# 日志源
_BUS_LOG = os.path.join(V11, ".agent_bus.jsonl")
_TOOL_FORGE_LOG = os.path.join(V11, ".tool_forge_log.jsonl")
_SEARCH_SKILLS_LOG = os.path.join(V11, ".search_skills.jsonl")
_TICK_REFLECT_DIR = os.path.join(_DIR, "logs")

# 已锻造 pattern 记录 (防重复)
_FORGED_PATTERNS = os.path.join(V11, ".tool_forge_patterns.json")


# ══════════════════════════════════════════════════════════════════
# 1. 日志扫描 → 识别高频模式
# ══════════════════════════════════════════════════════════════════

def scan_recurring_patterns(min_count: int = 3, days: int = 7) -> list[dict]:
    """从多个日志源扫描反复出现的操作模式。

    返回 [{pattern_key, source_log, count, sample, executor_code, 
           tool_name, description, category, cost_tier, search_hint}]
    """
    patterns = []
    cutoff = time.time() - days * 86400

    # 1a. 从总线 SEARCH 事件里提取高频搜索 query 模式
    patterns.extend(_scan_bus_search_patterns(cutoff, min_count))
    
    # 1b. 从 search_skills.jsonl 提取高质量 query→source 映射
    patterns.extend(_scan_search_skills_patterns(cutoff, min_count))
    
    # 1c. 从 tick_reflections 里提取反复提及的问题
    patterns.extend(_scan_tick_reflections(cutoff, min_count))

    # 1d. v99g: 从成功招法里提反复成功的招法→固化成确定性工具(对偶于1c的失败触发)
    patterns.extend(_scan_win_tactics(cutoff, min_count))

    # 去重 + 排序
    seen = set()
    unique = []
    for p in patterns:
        pk = p["pattern_key"]
        if pk in seen:
            continue
        seen.add(pk)
        unique.append(p)
    unique.sort(key=lambda x: -x["count"])
    return unique


def _scan_bus_search_patterns(cutoff: float, min_count: int) -> list[dict]:
    """从总线 SEARCH 事件提取高频搜索模式。"""
    if not os.path.exists(_BUS_LOG):
        return []
    query_counter = Counter()
    query_samples = {}
    try:
        with open(_BUS_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get("ts", 0) < cutoff:
                        continue
                    if rec.get("kind") != "SEARCH":
                        continue
                    extra = rec.get("extra") or {}
                    q = (extra.get("query") or rec.get("msg", ""))[:100]
                    if not q:
                        continue
                    # 归一化: 去掉具体数字/URL，保留模式
                    pattern = _normalize_pattern(q)
                    query_counter[pattern] += 1
                    if pattern not in query_samples:
                        query_samples[pattern] = q
                except Exception:
                    continue
    except Exception:
        return []

    out = []
    for pattern, count in query_counter.most_common(20):
        if count < min_count:
            continue
        sample = query_samples[pattern]
        tool_name = _safe_name("search_" + pattern[:30])
        out.append({
            "pattern_key": f"bus_search:{pattern}",
            "source_log": "agent_bus",
            "count": count,
            "sample": sample,
            "tool_name": tool_name,
            "description": f"高频搜索模式自动固化: {pattern[:50]}",
            "category": "search",
            "cost_tier": "medium",
            "search_hint": pattern[:60],
            "executor_code": _gen_search_executor(sample, tool_name),
        })
    return out


def _scan_search_skills_patterns(cutoff: float, min_count: int) -> list[dict]:
    """从 search_skills.jsonl 提取高质量 query→source 映射，固化成快捷搜索工具。"""
    if not os.path.exists(_SEARCH_SKILLS_LOG):
        return []
    
    by_pattern = defaultdict(list)
    try:
        with open(_SEARCH_SKILLS_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get("unix_ts", 0) < cutoff:
                        continue
                    pk = rec.get("pattern_key", "")
                    if not pk:
                        continue
                    by_pattern[pk].append(rec)
                except Exception:
                    continue
    except Exception:
        return []

    out = []
    for pk, records in by_pattern.items():
        if len(records) < min_count:
            continue
        # 找最优 source
        src_quality = defaultdict(list)
        for r in records:
            src_quality[r.get("source", "?")].append(r.get("quality", 0))
        best_src = max(src_quality.items(),
                       key=lambda kv: sum(kv[1]) / len(kv[1]))
        best_source = best_src[0]
        avg_quality = sum(best_src[1]) / len(best_src[1])
        if avg_quality < 0.3:
            continue
        
        sample_query = records[0].get("query", "")[:80]
        tool_name = _safe_name("quick_search_" + pk[:20])
        out.append({
            "pattern_key": f"search_skills:{pk}",
            "source_log": "search_skills",
            "count": len(records),
            "sample": f"query='{sample_query}' → source={best_source} (avg_quality={avg_quality:.2f})",
            "tool_name": tool_name,
            "description": f"从历史搜索经验固化的快捷搜索(pattern={pk}, best_source={best_source})",
            "category": "search",
            "cost_tier": "medium",
            "search_hint": f"quick search {best_source} {pk}",
            "executor_code": _gen_quick_search_executor(best_source, sample_query, tool_name),
        })
    return out


def _scan_tick_reflections(cutoff: float, min_count: int) -> list[dict]:
    """从 tick_reflections 日志里提取反复出现的问题/改进点。"""
    if not os.path.exists(_TICK_REFLECT_DIR):
        return []
    
    # 扫最近 days 天的目录
    import glob
    issue_counter = Counter()
    issue_samples = {}
    for date_dir in sorted(glob.glob(os.path.join(_TICK_REFLECT_DIR, "*")), reverse=True):
        # 解析日期目录名
        dirname = os.path.basename(date_dir)
        try:
            dir_time = time.mktime(time.strptime(dirname, "%Y-%m-%d"))
            if dir_time < cutoff:
                break
        except Exception:
            continue
        
        for jsonf in glob.glob(os.path.join(date_dir, "**/*.json"), recursive=True):
            try:
                data = json.load(open(jsonf, encoding="utf-8"))
                # 提取 issue/pain_point/improvement 等关键词
                text = json.dumps(data, ensure_ascii=False)
                for keyword in ["超时", "失败", "降级", "漂移", "漏", "黑洞", "空转"]:
                    if keyword in text:
                        issue_counter[keyword] += 1
                        if keyword not in issue_samples:
                            issue_samples[keyword] = dirname
            except Exception:
                continue
    
    out = []
    for keyword, count in issue_counter.most_common(10):
        if count < min_count:
            continue
        tool_name = _safe_name(f"check_{keyword}")
        out.append({
            "pattern_key": f"tick_reflect:{keyword}",
            "source_log": "tick_reflections",
            "count": count,
            "sample": f"反复出现的问题关键词: '{keyword}' (在 {issue_samples[keyword]} 等日期)",
            "tool_name": tool_name,
            "description": f"自动固化: 检查反复出现的'{keyword}'问题",
            "category": "monitor",
            "cost_tier": "cheap",
            "search_hint": f"check {keyword} recurring issue monitor",
            "executor_code": _gen_check_executor(keyword, tool_name),
        })
    return out


def _scan_win_tactics(cutoff: float, min_count: int) -> list[dict]:
    """v99g(2026-08-13 · 用户"让他自己想到该造确定性切行工具")补的缺环:
    从成功招法(win_tactic_capture 捕获)里·某招法在【≥min_count 个不同成功题】复现·
    就自动提工具需求→走 forge 自动生产。这是"反复用某招法成功→该固化成工具"的触发·
    对偶于 _scan_tick_reflections 的"反复失败→造检查工具"。他自己攒够成功·自己造工具。"""
    out = []
    try:
        import win_tactic_capture as _wtc
        counts = _wtc.signature_counts()   # {招法签名: 不同题数}
        # N>1 硬底线(用户要求):即使调用方传 min_count=1·招法也必须在【≥2个不同成功题】
        #   复现才升工具·防单样本过拟合(切行坐标因图而异·1个样本造的工具可能只对那张图)。
        _floor = max(2, min_count)
        for sig, n in counts.items():
            if n < _floor or sig in ("generic", ""):
                continue
            gen = _TACTIC_EXECUTORS.get(sig)
            if not gen:
                continue  # 只对有确定性实现模板的招法造工具(如 row_slice_ocr)
            tool_name = _safe_name(f"forged_{sig}")
            out.append({
                "pattern_key": f"win_tactic:{sig}",
                "source_log": "win_traces",
                "count": n,
                "sample": f"成功招法 '{sig}' 在 {n} 个不同题里复现成功·固化成确定性工具",
                "tool_name": tool_name,
                "description": gen["desc"],
                "category": "vision",
                "cost_tier": "medium",
                "search_hint": gen["hint"],
                "executor_code": gen["code"](tool_name),
            })
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════
# 2. Executor 代码生成 (模板化·不依赖 LLM)
# ══════════════════════════════════════════════════════════════════

def _gen_row_slice_executor(tool_name: str) -> str:
    """确定性切行工具真身:PIL按行坐标切→逐行OCR→按序拼。把"随机整读"变"机械逐行"。
    这是从 row_slice_ocr 成功招法固化出来的·不依赖VLM即兴读多行。"""
    return '''def _fn(params):
    """确定性切行读图:把混排/多行图按行切开·逐行OCR·避免整读时行间串读/重复读。
    params: {src: 图片路径, rows: 行数(可选·默认自动按投影分行), lang: OCR语言(可选)}"""
    import os, subprocess, tempfile
    src = (params or {}).get("src", "")
    if not src or not os.path.exists(src):
        return {"ok": False, "error": f"图片不存在: {src}"}
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "error": "缺PIL·pip install pillow"}
    try:
        im = Image.open(src).convert("L")
        W, H = im.size
        rows = int((params or {}).get("rows", 0))
        # 自动分行:按水平投影找空白带切行(无rows时)
        if rows <= 0:
            import numpy as np
            arr = 255 - np.asarray(im)              # 反相:文字=高值
            rowsum = arr.sum(axis=1)
            thr = rowsum.max() * 0.04           # v99h:降阈值·更多行被检出
            raw_bands, in_band, start = [], False, 0
            for y, v in enumerate(rowsum):
                if v > thr and not in_band:
                    in_band, start = True, y
                elif v <= thr and in_band:
                    in_band = False
                    if y - start > 6:               # 忽略太薄的噪声带
                        raw_bands.append((start, y))
            if in_band:
                raw_bands.append((start, H))
            # v99h(2026-08-13 · 分数图下半区漏读根治):
            #   ①大band(如揉成一块的多行)按固定步长细分·别整块读 ②确保覆盖到图底H
            _MAX_BAND = 55                          # 单行最大高度·超了细分
            bands = []
            for (y0, y1) in raw_bands:
                if y1 - y0 > _MAX_BAND:
                    n = (y1 - y0 + _MAX_BAND - 1) // _MAX_BAND
                    step = (y1 - y0) // n
                    for k in range(n):
                        bands.append((y0 + k*step, y1 if k == n-1 else y0 + (k+1)*step))
                else:
                    bands.append((y0, y1))
            # 底部兜底:若最后一个band没到图底·补一段(sample problems区常在底部被漏)
            if bands and bands[-1][1] < H - 10:
                _ly = bands[-1][1]
                while _ly < H:
                    bands.append((_ly, min(H, _ly + _MAX_BAND)))
                    _ly += _MAX_BAND
        else:
            step = H // rows
            bands = [(i*step, (i+1)*step if i < rows-1 else H) for i in range(rows)]
        # 逐行裁剪+OCR
        lines = []
        for i, (y0, y1) in enumerate(bands):
            crop = im.crop((0, max(0, y0-2), W, min(H, y1+2)))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                crop.save(tf.name)
                tmp = tf.name
            txt = ""
            try:
                r = subprocess.run(["tesseract", tmp, "stdout"],
                                   capture_output=True, text=True, timeout=30)
                txt = (r.stdout or "").strip()
            except Exception as e:
                txt = f"[OCR失败:{type(e).__name__}]"
            finally:
                try: os.unlink(tmp)
                except Exception: pass
            lines.append({"row": i, "y": [y0, y1], "text": txt})
        return {"ok": True, "n_rows": len(lines), "lines": lines,
                "note": "确定性逐行读·按row顺序拼·区分样例行/答案行由调用方按题意判"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
'''


# 招法签名 → 确定性工具生成器(有模板的招法才自动造工具)
_TACTIC_EXECUTORS = {
    "row_slice_ocr": {
        "desc": "确定性切行读图(混排/多行/worksheet图·PIL按行切→逐行OCR→按序拼·避免整读串行)",
        "hint": "row slice ocr worksheet 切行 逐行 多行读图 混排 表格",
        "code": _gen_row_slice_executor,
    },
}


def _normalize_pattern(text: str) -> str:
    """归一化文本为模式 key: 去数字、去 URL、小写化。"""
    t = re.sub(r'https?://\S+', 'URL', text)
    t = re.sub(r'\d+', 'N', t)
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t[:80]


def _safe_name(raw: str) -> str:
    """把任意文本变成合法的 tool name (snake_case)。"""
    name = re.sub(r'[^a-zA-Z0-9_]', '_', raw)
    name = re.sub(r'_+', '_', name).strip('_').lower()
    return name[:40] if name else "forged_tool"


def _gen_search_executor(sample_query: str, tool_name: str) -> str:
    """为高频搜索模式生成 executor 代码。"""
    # 转义引号
    safe_query = sample_query.replace("'", "\\'")[:100]
    return f"""def _fn(params):
    # 自动锻造: 高频搜索模式固化
    # 原始 query: {safe_query}
    import sys, os
    _dir = os.path.dirname(os.path.abspath("{tool_name}"))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    try:
        from search_agent import search
        query = params.get('query', '{safe_query}')
        source = params.get('source', 'friday_bing')
        result = search(query, mode='balanced', top_k=params.get('max_results', 5))
        hits = result if isinstance(result, list) else result.get('hits', result.get('results', []))
        return {{'ok': True, 'hits': hits, 'count': len(hits) if isinstance(hits, list) else 0, 'forged_from': 'bus_search_pattern'}}
    except Exception as e:
        return {{'ok': False, 'error': f'search failed: {{type(e).__name__}}: {{e}}'}}
"""


def _gen_quick_search_executor(best_source: str, sample_query: str, tool_name: str) -> str:
    """为高质量 query→source 映射生成快捷搜索 executor。"""
    safe_query = sample_query.replace("'", "\\'")[:80]
    return f"""def _fn(params):
    # 自动锻造: 历史最优 source 快捷搜索
    # pattern: query='{safe_query}' → source={best_source}
    import sys, os
    _dir = os.path.dirname(os.path.abspath("{tool_name}"))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    try:
        from search_agent import search
        query = params.get('query', '{safe_query}')
        result = search(query, mode='balanced', top_k=params.get('max_results', 5))
        hits = result if isinstance(result, list) else result.get('hits', result.get('results', []))
        return {{'ok': True, 'hits': hits, 'count': len(hits) if isinstance(hits, list) else 0, 'best_source': '{best_source}', 'forged_from': 'search_skills'}}
    except Exception as e:
        return {{'ok': False, 'error': f'quick search failed: {{type(e).__name__}}: {{e}}'}}
"""


def _gen_check_executor(keyword: str, tool_name: str) -> str:
    """为反复出现的问题关键词生成检查 executor。"""
    return f"""def _fn(params):
    # 自动锻造: 检查反复出现的'{keyword}'问题
    import sys, os, json, glob
    _dir = os.path.dirname(os.path.abspath("{tool_name}"))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    try:
        # 扫最近日志里包含 '{keyword}' 的记录
        bus_log = os.path.join(_dir, '..', '.agent_bus.jsonl')
        hits = []
        if os.path.exists(bus_log):
            lines = open(bus_log, encoding='utf-8').read().strip().splitlines()
            for line in lines[-200:]:
                if '{keyword}' in line:
                    try:
                        rec = json.loads(line)
                        hits.append({{'ts': rec.get('ts'), 'who': rec.get('who'), 'msg': rec.get('msg', '')[:80]}})
                    except Exception:
                        pass
        return {{'ok': True, 'keyword': '{keyword}', 'recent_hits': hits[-10:], 'count': len(hits), 'forged_from': 'tick_reflection'}}
    except Exception as e:
        return {{'ok': False, 'error': f'check failed: {{type(e).__name__}}: {{e}}'}}
"""


# ══════════════════════════════════════════════════════════════════
# 3. Forge 闭环: 识别 → 生成 → 注册 → 验证
# ══════════════════════════════════════════════════════════════════

def _load_forged_patterns() -> set:
    """加载已锻造的 pattern keys (防重复)。"""
    if not os.path.exists(_FORGED_PATTERNS):
        return set()
    try:
        data = json.load(open(_FORGED_PATTERNS, encoding="utf-8"))
        return set(data.get("patterns", []))
    except Exception:
        return set()


def _save_forged_pattern(pattern_key: str, tool_name: str):
    """记录已锻造的 pattern。"""
    existing = _load_forged_patterns()
    existing.add(pattern_key)
    try:
        os.makedirs(os.path.dirname(_FORGED_PATTERNS), exist_ok=True)
        with open(_FORGED_PATTERNS, "w", encoding="utf-8") as f:
            json.dump({"patterns": list(existing)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def run_forge_loop(min_count: int = 3, days: int = 7, max_forge: int = 3) -> dict:
    """主入口: 完整一轮工具自生成闭环。
    
    流程:
      1. scan_recurring_patterns → 识别高频模式
      2. 过滤已锻造的 → 只留新的
      3. 对每个新模式: generate → forge_new_tool → 验证执行
      4. 上总线报告
    
    Args:
        min_count: 最少出现次数才考虑锻造
        days: 扫描最近 N 天的日志
        max_forge: 一轮最多锻造几个工具 (防一次性炸)
    
    Returns:
        {scanned, forged, skipped, errors, details}
    """
    # v13.90 (2026-08-09) 省电门 · 用户「进化只在 load 低时启动·降 CPU 卡顿」。
    # 造工具+验证执行是吃 CPU 的真进化·load 高时本轮不锻造(下次 cron 4:17 再来)。
    try:
        import sys as _fsys, os as _fos
        _fsys.path.insert(0, _fos.path.dirname(_fos.path.abspath(__file__)))
        from power_gate import should_run_evolution, gate_status
        if not should_run_evolution():
            return {"scanned": 0, "forged": 0, "skipped": 0, "errors": 0,
                    "details": [], "power_skipped": True, "gate": gate_status()}
    except Exception:
        pass  # 拿不到闸不阻断(fail-open·别因闸故障停掉进化)

    from evolution_tool_registry import forge_new_tool, execute, _ensure_core_registered
    _ensure_core_registered()

    # 上总线
    try:
        from agent_comm import emit
        emit("INTERNAL", "tool_forge_loop", "工具自优化闭环启动", {"min_count": min_count, "days": days})
    except Exception:
        pass

    patterns = scan_recurring_patterns(min_count=min_count, days=days)
    forged_patterns = _load_forged_patterns()
    
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scanned_patterns": len(patterns),
        "forged": [],
        "skipped": [],
        "errors": [],
    }

    # v99g: win_tactic(成功招法固化)优先于高频search模式——招法count天然低(1个成功)·
    #   不该被count几千的搜索模式挤出max_forge名额。成功招法=更该固化成确定性工具。
    patterns = sorted(patterns, key=lambda p: 0 if p.get("pattern_key", "").startswith("win_tactic") else 1)

    new_count = 0
    for p in patterns:
        if new_count >= max_forge:
            break
        pk = p["pattern_key"]
        if pk in forged_patterns:
            result["skipped"].append({"pattern": pk, "reason": "already forged"})
            continue

        # 调 forge_new_tool
        forge_result = forge_new_tool(
            name=p["tool_name"],
            description=p["description"],
            category=p["category"],
            cost_tier=p["cost_tier"],
            executor_code=p["executor_code"],
            search_hint=p["search_hint"],
            source_pattern=p["sample"][:200],
        )

        if not forge_result.get("ok"):
            result["errors"].append({
                "pattern": pk,
                "tool_name": p["tool_name"],
                "error": forge_result.get("error", "unknown"),
            })
            continue

        # 验证: 立刻执行一次 (空参数)
        verify_result = execute(p["tool_name"], {})
        
        result["forged"].append({
            "pattern": pk,
            "tool_name": p["tool_name"],
            "count": p["count"],
            "verify_ok": verify_result.get("ok", False),
            "verify_error": verify_result.get("error", "") if not verify_result.get("ok") else "",
        })
        _save_forged_pattern(pk, p["tool_name"])
        new_count += 1

        # 上总线
        try:
            from agent_comm import emit
            emit("INTERNAL", "tool_forge_loop",
                 f"锻造新工具: {p['tool_name']} (pattern出现{p['count']}次)",
                 {"tool_name": p["tool_name"], "pattern": pk, "count": p["count"],
                  "verify_ok": verify_result.get("ok", False)})
        except Exception:
            pass

    result["forged_count"] = new_count
    result["skipped_count"] = len(result["skipped"])
    
    return result


def forge_stats() -> dict:
    """查看工具自生成统计。"""
    forged = _load_forged_patterns()
    # 读 forge log
    forge_log = []
    if os.path.exists(_TOOL_FORGE_LOG):
        try:
            lines = open(_TOOL_FORGE_LOG, encoding="utf-8").read().strip().splitlines()
            forge_log = [json.loads(l) for l in lines[-20:] if l.strip()]
        except Exception:
            pass
    return {
        "total_forged_patterns": len(forged),
        "forged_patterns": list(forged)[:20],
        "recent_forge_log": forge_log,
    }


# ══════════════════════════════════════════════════════════════════
# 4. 注册为 evolution_tool_registry 的工具 (自我递归)
# ══════════════════════════════════════════════════════════════════

def _register_self():
    """把 forge_loop 自身注册成一个可被其他 agent 调用的工具。"""
    try:
        from evolution_tool_registry import register_evolution_tool
        
        @register_evolution_tool(
            name="tool_forge",
            description="扫描近期日志，自动识别高频模式并锻造为新工具",
            category="meta",
            cost_tier="expensive",
            search_hint="forge tool auto generate pattern recurring optimize",
            module="tool_forge_loop",
            is_deferred=True,
            always_load=False,
            max_retries=0,
            timeout_sec=120,
        )
        def _tool_forge(params):
            result = run_forge_loop(
                min_count=params.get("min_count", 3),
                days=params.get("days", 7),
                max_forge=params.get("max_forge", 3),
            )
            return {"ok": True, "result": result}
        
        @register_evolution_tool(
            name="forge_stats",
            description="查看工具自生成统计(已锻造哪些模式)",
            category="meta",
            cost_tier="cheap",
            search_hint="forge stats tool generated count pattern",
            module="tool_forge_loop",
            is_deferred=True,
            always_load=False,
        )
        def _forge_stats(params):
            return {"ok": True, "result": forge_stats()}
            
    except Exception:
        pass


# 模块级触发注册：之前只在 __main__ 里调用 _register_self()，意味着被
# 其他模块 import(比如未来 evolution_tool_impls 接入)时不会自动注册。
_register_self()


# ══════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    # 确保 evolution_tool_registry 可 import
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)

    if "--scan" in sys.argv:
        patterns = scan_recurring_patterns(min_count=2, days=14)
        print(f"=== 扫描到 {len(patterns)} 个高频模式 ===")
        for p in patterns:
            print(f"  [{p['count']}x] {p['pattern_key']:40} → {p['tool_name']:30} ({p['source_log']})")
            print(f"         sample: {p['sample'][:80]}")
        sys.exit(0)

    if "--stats" in sys.argv:
        print(json.dumps(forge_stats(), ensure_ascii=False, indent=2))
        sys.exit(0)

    if "--run" in sys.argv:
        # 完整闭环
        result = run_forge_loop(min_count=3, days=7, max_forge=3)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n=== 锻造 {result['forged_count']} 个新工具 · 跳过 {result['skipped_count']} 个已存在 ===")
        sys.exit(0)

    # 默认: scan + dry-run
    print("tool_forge_loop · 工具自优化闭环")
    print("用法: --scan (扫描模式) | --run (执行锻造) | --stats (统计)")
    patterns = scan_recurring_patterns(min_count=2, days=7)
    print(f"\n扫描到 {len(patterns)} 个高频模式 (--scan 查看详情):")
    for p in patterns[:5]:
        print(f"  [{p['count']}x] {p['tool_name']}: {p['description'][:60]}")
