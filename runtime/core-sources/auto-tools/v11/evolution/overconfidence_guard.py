#!/usr/bin/env python3
"""
v13.63 · overconfidence_guard · 过度自信校验器
────────────────────────────────────────────────────────────────
用户 2026-07-13 元规则:「过度自信校验器·与搜索 agent 结合·让你能知道自己
不足就去搜索·穷尽搜索」

── 核心机制 ──
  1. 扫最近 LLM 输出 (v11/.what_i_did.jsonl · v11/.llm_calls.jsonl)
  2. 找过度自信信号:
     · 断言词:"一定/肯定/无疑/绝对/必然/毫无疑问/100%/绝无/永远"
     · 未加限定的技术断言:"支持 XX/兼容 YY/性能 Zms"
     · 无引用的具体数字:"CTR 涨 3x / 缩短 40%"
     · 特定 API 姿势断言 (常出错):"用 XX.YY() 就行"
  3. 每条命中 → 生 3 条 verify_query → 触发真跑搜索
  4. 结果落 v11/.overconfidence_events.jsonl · 供 goal_driven_explorer 消费

── 用户之前抓包 (记忆已存) ──
  [[feedback-invented-code-as-evidence]]  · 我编 Java pattern 假装读过 backend
  [[feedback-verify-before-claim]]        · 6 类话下笔前必须 grep
  [[feedback-mars-agent-caught-me-again]] · "已验证"其实没穷尽读
"""
import os, sys, json, re, time
from datetime import datetime, timedelta


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
V11 = os.path.join(BASE, "v11")
EVENTS = os.path.join(V11, ".overconfidence_events.jsonl")
WHAT_I_DID = os.path.join(V11, ".what_i_did.jsonl")

# 过度自信信号词 (加空格避误伤 markdown/code)
CONFIDENCE_PATTERNS = [
    (r"[一必绝][定然对无]会", "断言词·中"),
    (r"肯定[会能是]", "断言词·中"),
    (r"毫无疑问", "断言词·强"),
    (r"100%|百分之百", "断言词·强"),
    (r"绝无[^\s]{0,4}[疑可]", "断言词·强"),
    (r"永远[不能会]", "断言词·中"),
    (r"从未[有失]", "断言词·中"),
    (r"[必只]需?调用\s*[A-Za-z_.]+\(", "API 姿势断言"),
    (r"支持\s*[A-Za-z0-9_.]+\s*(?:格式|接口|协议|参数)", "兼容性断言"),
    (r"性能\s*[<≤>≥]?\s*\d+\s*(?:ms|s|秒|毫秒)", "性能数字断言"),
    (r"(?:涨|提升|缩短|下降|减少)\s*\d+\s*(?:%|倍|x|X)", "无引用数字"),
    (r"CTR\s*(?:是|为|达|涨)\s*\d", "CTR 断言"),
    # v13.88 · 2026-07-24 · 主对话结论断言 (用户抓「都可以整文件删/完全一样/无 import」)
    (r"都可以(?:删|整文件删|重构|重写)", "文件级结论断言"),
    (r"完全(?:一样|相同|等价|覆盖)", "等价断言"),
    (r"(?:无|没有|零)\s*(?:import|依赖|引用|调用)", "依赖排他断言"),
    (r"(?:直接|放心)\s*(?:删|覆盖|重构)", "行动排他断言"),
    (r"(?:全部|所有)\s*(?:可删|可移除|无用)", "全称否定"),
    # v13.88.37 · 2026-07-24 · 用户抓 11346/39579/50925 · 编数字堆
    # 4 类正则 · 见 face_slap category "fabrication"
    # A · 3+ 数字堆 + 中文量词
    (r"(?:[一-龥\w]{0,10}\d{3,}\s*(?:条|次|个|篇|行|字|天|小时|分钟|秒|位|项|处|类|份|张|道|轮|人|台)\s*){3,}", "裸数字堆断言"),
    # B · 单个 4 位以上 + 量词 · 无 wc/stat 证据 (context 里检)
    (r"\d{4,}\s*(?:条|个|次|篇|行)", "四位数量断言"),
    # C · 「N 天前/N 小时前」时间断言
    (r"\d+\s*(?:天前|小时前|周前|月前)(?!(?:.*(?:wc|stat|grep|python3|find|curl)))", "时间断言"),
]

# 需搜索校验的话题分类 → 生 query 模板
CATEGORIES = {
    "API 姿势断言": lambda phrase: [
        f'"{phrase}" official documentation example',
        f'{phrase} usage python 2026',
        f'is {phrase} correct syntax',
    ],
    "兼容性断言": lambda phrase: [
        f'"{phrase}" compatibility list',
        f'{phrase} does it support',
    ],
    "性能数字断言": lambda phrase: [
        f'{phrase} benchmark 2026',
        f'{phrase} real measurement',
    ],
    "无引用数字": lambda phrase: [
        f'{phrase} case study evidence',
        f'{phrase} real user data',
    ],
}


def scan_text(text: str, source_tag: str = "") -> list:
    """扫一段文本 · 返回命中的自信事件列表
    v13.88.37 · 加载 face_slap_rules · 若命中 fabrication/count_over_semantics 加规则提示
    """
    events = []
    # 加载 face_slap 规则 (若存在)
    _face_slap_rules = _load_face_slap_rules_safe()

    # 若文本已含证据 · 跳过部分误伤
    _has_evidence = any(x in text for x in
                        ["wc -l", "stat -f", "grep -c", "python3 -c",
                         "ls -la", "find ", "curl ", "```bash", "```sh"])

    for pat, kind in CONFIDENCE_PATTERNS:
        for m in re.finditer(pat, text):
            # 有证据 · 跳裸数字类
            if _has_evidence and kind in ("裸数字堆断言", "四位数量断言", "时间断言"):
                continue
            span = text[max(0, m.start()-40): m.end()+40]
            events.append({
                "kind": kind,
                "phrase": m.group(0),
                "context": span.strip()[:200],
                "source": source_tag,
                "ts": datetime.now().isoformat(),
                # v13.88.37 · 附加 face_slap 规则 (若匹配)
                "face_slap_rule": _lookup_face_slap(_face_slap_rules, kind),
            })
    return events


def _load_face_slap_rules_safe():
    """load face_slap_rules · 失败返 [] · 不 raise"""
    try:
        import json as _j
        p = os.path.join(V11, ".face_slap_rules.json")
        if not os.path.exists(p):
            return []
        return _j.load(open(p)).get("rules", [])
    except Exception:
        return []


def _lookup_face_slap(rules, kind):
    """kind → face_slap category 映射"""
    _map = {
        "裸数字堆断言": "fabrication",
        "四位数量断言": "fabrication",
        "时间断言": "fabrication",
        "文件级结论断言": "harness_blind",
        "等价断言": "premature_generalization",
    }
    cat = _map.get(kind)
    if not cat:
        return None
    for r in rules:
        if r.get("category") == cat:
            return r.get("rule", "")[:100]
    return None


def get_rules_for_hook() -> list:
    """v13.88.37 · 给下游 hook 消费 · 用户 07-24 抓「rule 存着没人消费」
    调用者:
      · output_gate (Stop hook)
      · pre_edit_impact_check (PreToolUse hook)
      · self_check_hook
    返:[{"category","rule","hook","pattern"}]
    """
    rules = _load_face_slap_rules_safe()
    if not rules:
        return []
    # 展开 · 每 rule + 对应 CONFIDENCE_PATTERN
    _rev_map = {
        "fabrication": ["裸数字堆断言", "四位数量断言", "时间断言"],
        "harness_blind": ["文件级结论断言"],
        "premature_generalization": ["等价断言"],
    }
    out = []
    for r in rules:
        cat = r.get("category")
        kinds = _rev_map.get(cat, [])
        out.append({
            "category": cat,
            "rule": r.get("rule", ""),
            "prevention_hook": r.get("prevention_hook", ""),
            "kinds": kinds,
            "n_slaps": r.get("n_slaps", 0),
        })
    return out


def scan_recent(hours: int = 1) -> list:
    """扫最近 N 小时 what_i_did 里的 LLM 输出"""
    if not os.path.exists(WHAT_I_DID):
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).timestamp()
    events = []
    try:
        with open(WHAT_I_DID) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    ts = r.get("ts", "")
                    if not ts: continue
                    try:
                        rec_ts = datetime.fromisoformat(ts).timestamp()
                    except Exception:
                        continue
                    if rec_ts < cutoff: continue
                    # 抽任何 text-like 字段
                    for k in ("text", "output", "response", "content", "answer"):
                        v = r.get("payload", {}).get(k) if isinstance(r.get("payload"), dict) else r.get(k)
                        if isinstance(v, str) and len(v) > 20:
                            hits = scan_text(v, source_tag=f"{r.get('action','')}·{k}")
                            events.extend(hits)
                except Exception:
                    continue
    except Exception:
        pass
    return events


def gen_verify_queries(event: dict) -> list:
    """一个事件 → 3 条真跑校验 query"""
    tpl = CATEGORIES.get(event["kind"])
    if tpl:
        return tpl(event["phrase"])[:3]
    # 默认:直接搜原短语
    return [
        f'{event["phrase"]} verification',
        f'{event["phrase"]} counter-example',
        f'{event["phrase"]} official',
    ]


def verify_with_search(event: dict, use_bridge: bool = True) -> dict:
    """真跑 · Firecrawl agent 或 fallback WebAccess"""
    queries = gen_verify_queries(event)
    verified_hits = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        # v13.88.9 · 手拼 google 死磕→改走 SearchAgent (换源/去重/HTTP优先) + loop_guard
        from search_agent import search
        from search_loop_guard import should_search, record_search
        for q in queries[:2]:  # 只跑前 2 · 省额度
            allowed, why = should_search(q)
            if not allowed:
                continue
            shits = search(q, mode="fast", top_k=5)
            record_search(q)
            for h in shits:
                content = h.get("content", "") or ""
                if len(content) > 300:
                    verified_hits.append({
                        "query": q, "via": h.get("source"),
                        "excerpt": content[:800],
                        "url": h.get("url"), "title": h.get("title"),
                    })
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}
    return {"ok": bool(verified_hits), "hits": verified_hits, "n_queries": len(queries)}


def tick(hours: int = 1, do_verify: bool = False, max_events: int = 5) -> dict:
    """cron 每 10min 触发"""
    events = scan_recent(hours=hours)
    # 去重 · 按 phrase 保留最新一个
    seen = {}
    for e in events:
        seen[e["phrase"]] = e
    events = list(seen.values())[:max_events]

    for e in events:
        e["verify_queries"] = gen_verify_queries(e)
        if do_verify:
            e["verify_result"] = verify_with_search(e)

    # 落日志
    with open(EVENTS, "a") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # 触发 goal (即使不 verify)
    if events:
        try:
            _push_verify_goal(events)
        except Exception:
            pass

    return {"n_events": len(events), "verified": do_verify,
            "sample": events[:3]}


def _push_verify_goal(events: list):
    """把 verify_queries 塞进 goal_list.jsonl · 让 goal_driven_explorer 消费"""
    goal_path = os.path.join(V11, ".goal_list.jsonl")
    now = datetime.now().isoformat()
    with open(goal_path, "a") as f:
        for e in events:
            goal = {
                "type": "overconfidence_verify",
                "topic": e["phrase"][:80],
                "action": "search_and_read",
                "queries": e["verify_queries"],
                "context": e["context"],
                "kind": e["kind"],
                "goal": f"校验「{e['phrase']}」是否真成立 · 搜 → 点详情 → 抽 3 条证据",
                "id": f"conf_{int(time.time())}_{hash(e['phrase']) % 10000}",
                "status": "pending",
                "created_at": now,
                "source": "overconfidence_guard",
            }
            f.write(json.dumps(goal, ensure_ascii=False) + "\n")


def stats() -> dict:
    if not os.path.exists(EVENTS):
        return {"n_events": 0}
    n = 0
    kinds = {}
    with open(EVENTS) as f:
        for line in f:
            n += 1
            try:
                r = json.loads(line)
                k = r.get("kind", "?")
                kinds[k] = kinds.get(k, 0) + 1
            except:
                pass
    return {"n_events_total": n, "by_kind": kinds}


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd", choices=["scan", "tick", "verify", "stats", "test"])
        ap.add_argument("--hours", type=int, default=1)
        ap.add_argument("--verify", action="store_true")
        ap.add_argument("--text", default="")
        args = ap.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "scan":
        events = scan_recent(hours=args.hours)
        for e in events[:20]:
            print(f"  [{e['kind']}] {e['phrase']}  ·  {e['context'][:80]}")
        print(f"total: {len(events)}")
    elif args.cmd == "tick":
        r = tick(hours=args.hours, do_verify=args.verify)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "verify":
        # 试跑校验 · 用 --text 输入一句话
        events = scan_text(args.text or "这个 API 一定支持 v2.0 格式·性能 5ms")
        for e in events:
            print(f"[{e['kind']}] {e['phrase']}")
            r = verify_with_search(e)
            print(f"  verify: {r.get('ok')} · {r.get('n_queries', 0)} queries")
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "test":
        # 自测
        sample = "这个 API 一定支持 markdown 格式·性能 5ms·CTR 提升 30%·永远不会挂"
        events = scan_text(sample, source_tag="test")
        for e in events:
            e["verify_queries"] = gen_verify_queries(e)
        print(json.dumps(events, ensure_ascii=False, indent=2))
