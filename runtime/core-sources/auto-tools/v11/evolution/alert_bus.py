#!/usr/bin/env python3
"""alert_bus · v13.88.37 · 2026-07-24

用户 07-24: 「告警很全吗?都覆盖了吗?我说的是告警」

问题 · 磁盘查:
  22 target supervisor · 15 有 osascript 告警
  9 大组件 · 0 告警 (监督到问题不通知我)

统一告警 bus:
  · 一个 API · notify(component, severity, msg, sound)
  · 4 级严重: INFO / WARN / ERROR / CRITICAL
  · 4 声音: Ping (info) / Funk (warn) / Sosumi (error) / Basso (critical)
  · 落日志 .alerts.jsonl (可追溯 · 不刷屏)
  · 节流 · 同 (component, msg_hash) 30min 内只告 1 次

用法 (让 9 缺口组件都调):
  from alert_bus import notify
  notify("face_slap_evolver", "WARN", "新增 5 条打脸 sev=high")
"""
import os, sys, json, subprocess, hashlib, shutil
from pathlib import Path
from datetime import datetime, timedelta


def _which(bin_name: str):
    """探测二进制 · 有则返回绝对路径 · 无则 None"""
    try:
        return shutil.which(bin_name)
    except Exception:
        return None


def _build_md_content(component: str, severity: str, msg: str,
                      now, key: str, icon: str,
                      upstream: str = None, trigger_ctx: str = None,
                      root_cause: str = None, todo: str = None) -> str:
    """生 alert 详情 markdown · 独立函数便于 by_hash 先写再弹
    v13.88.9f · 重排成人能看懂的决策卡片 (6 段):
      【问题一句话】→【影响谁】→【怎么触发】→【为什么】→【该做什么】→【修了没】
    """
    # ── 修复状态 · v13.88.9f 优先读真状态 (mark_resolved 落的 .alerts_resolved.json) ──
    fix_status = _read_resolved_status(key)
    # 真状态没有 · 再退回老 fix_pool 子串匹配 (向后兼容)
    if fix_status is None:
        fix_status = "❌ 未修 (待处理·无组件主动标记)"
        try:
            fp = V11 / ".fix_pool.jsonl"
            if fp.exists():
                for line in fp.read_text(encoding='utf-8').splitlines():
                    try:
                        fx = json.loads(line)
                        if component in fx.get("bug", "") or component in fx.get("fix", ""):
                            fix_status = f"✅ 已修 · {fx['ts'][:16]} · {fx['fix'][:80]}"
                            break
                    except Exception:
                        continue
        except Exception:
            pass

    # v13.88.37 · 加实时验证 · pytest 类告警若现在能跑通 → 标已过期
    try:
        if component.startswith("pytest:"):
            # ① SyntaxError · py_compile 验
            if "SyntaxError" in msg:
                import re as _re
                candidates = _re.findall(r"(/[\w/.\-一-鿿]+\.py)", msg)
                for target in candidates:
                    if os.path.exists(target):
                        import subprocess as _sp
                        r = _sp.run(["python3", "-m", "py_compile", target],
                                     capture_output=True, timeout=5)
                        if r.returncode == 0:
                            fix_status = f"✅ 已过期 · {target} 现语法通"
                            break
                        else:
                            fix_status = f"❌ 未修 · {target} 仍有语法错"
                            break

            # ② 一般 assert 错 · 从 nodeid 抽 test · 重跑 · 现在通就标已过期
            # nodeid 格式: tests/test_x.py::Test_Y::test_z
            elif "::test_" in msg or "assert" in msg.lower():
                import re as _re
                m = _re.search(r"(tests?/[^\s:]+\.py::[\w:]+)", msg)
                if m:
                    nodeid = m.group(1)
                    # 补绝对路径
                    if not nodeid.startswith("/"):
                        nodeid_full = f"v11/evolution/{nodeid}"
                    else:
                        nodeid_full = nodeid
                    try:
                        import subprocess as _sp
                        r = _sp.run(
                            ["python3", "-m", "pytest", nodeid_full,
                             "--tb=no", "-q", "--no-header"],
                            capture_output=True, text=True, timeout=15,
                            cwd=str(V11.parent),
                        )
                        if r.returncode == 0 and "passed" in r.stdout:
                            fix_status = f"✅ 已过期 · 现在真跑 passed"
                        elif r.returncode != 0 and "failed" in r.stdout:
                            fix_status = f"❌ 未修 · 现在仍 fail"
                    except Exception:
                        pass
    except Exception:
        pass
    # v13.88.65 · 2026-07-28 用户: 8 环闭环加搜索
    # 报警后自动查历史类似 alert · 有没有相同 hash / 相同组件历史修法
    similar_section = _find_similar_alerts_section(component, msg, key)

    # ── v13.88.9f · 人可读决策卡片 · 用户反复骂「报警一坨狗屎·人看不懂」──
    # 6 段顺序: 问题一句话 → 影响谁 → 怎么触发 → 为什么 → 该做什么 → 修了没
    # 缺的字段显「未知/待补」而非空 · 上下游没传则自动推
    _NA = "_（待补·未知）_"

    # ① 问题一句话 · 从 msg 抽第一行 / 前 120 字当摘要
    one_liner = (msg.strip().splitlines() or ["(空)"])[0].strip()[:160] or "(空)"

    # ② 影响谁 (上下游) · 显式传优先 · 否则从 depends_on 自动推 (guard)
    if upstream:
        upstream_txt = upstream
    else:
        upstream_txt = _auto_upstream(component)

    trigger_txt = trigger_ctx if trigger_ctx else _NA
    root_txt = root_cause if root_cause else _NA
    todo_txt = todo if todo else _NA

    return (
        f"# 🃏 告警卡片 · {component}\n\n"
        f"> {icon} **{severity}** · {now.strftime('%Y-%m-%d %H:%M:%S')} · hash `{key}`\n\n"
        f"## 1️⃣ 问题一句话\n\n{one_liner}\n\n"
        f"## 2️⃣ 影响谁（上下游）\n\n{upstream_txt}\n\n"
        f"## 3️⃣ 怎么触发的\n\n{trigger_txt}\n\n"
        f"## 4️⃣ 为什么（原因分析）\n\n{root_txt}\n\n"
        f"## 5️⃣ 该做什么（todo）\n\n{todo_txt}\n\n"
        f"## 6️⃣ 修了没\n\n{fix_status}\n\n"
        f"---\n\n"
        f"<details><summary>原始报警内容（机器串·调试用）</summary>\n\n"
        f"```\n{msg[:1200]}\n```\n\n</details>\n\n"
        f"{similar_section}\n\n"
        f"---\n\n"
        f"## 快查命令\n\n"
        f"- 标记已修: `python3 v11/evolution/alert_bus.py --resolve {key} --how '修法'`\n"
        f"- 查历史 24h: `python3 v11/evolution/alert_bus.py --recent 24`\n"
        f"- 生 dashboard: `python3 v11/evolution/alert_bus.py --dashboard 24`\n"
        f"- 挖底层根因: `python3 v11/evolution/fix_pool.py --daily-analyze`\n"
    )


def _find_similar_alerts_section(component: str, msg: str, key: str) -> str:
    """8 环闭环: 报警 → 外网搜索 SearchAgent
    v13.88.65 · 2026-07-28 用户抓「搜索是外部搜索·使用搜索 agent」
    · SearchAgent fast 真调外网但 ~34s · 不能同步阻塞 notify
    · 策略: 报警时把 query 入队 (.alert_search_queue.jsonl)
            独立 cron (alert_search_worker) 异步搜完·回填 by_hash md
    · 本函数只写「搜索待补」占位·秒返
    """
    try:
        import json as _json
        # v13.88.9 · query 净化闸 · 用户抓「拿文件去搜·不得搜问题吗」
        # 告警文本先判「是不是可搜的真外部问题」·内部状态/fid/.py 不入队·真问题抽干净 query
        raw_query = f"{component} {msg[:80]}"
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from search_query_purifier import purify
            _pure = purify(component, raw_query, use_llm=True)
        except Exception:
            # 净化挂·保守不入队 (宁可不搜·不搜垃圾)
            _pure = {"searchable": False, "query": "", "reason": "purify_err"}

        if not _pure.get("searchable"):
            return (f"## 外网参考 (8 环闭环)\n\n"
                    f"🚫 内部状态·不搜外网 ({_pure.get('reason', '')[:30]})\n")

        query = _pure.get("query") or raw_query[:60]
        # 入队 · 待异步搜 (去重: 同 query 已在队列则跳过)
        queue_p = V11 / ".alert_search_queue.jsonl"
        try:
            _existing = set()
            if queue_p.exists():
                for _ln in queue_p.read_text(encoding="utf-8").splitlines()[-300:]:
                    try:
                        _existing.add(_json.loads(_ln).get("query", ""))
                    except Exception:
                        continue
            if query not in _existing:
                with open(queue_p, "a", encoding="utf-8") as f:
                    f.write(_json.dumps({
                        "hash": key,
                        "component": component,
                        "query": query,
                        "query_original": raw_query[:120],
                        "purify_via": _pure.get("via", "?"),
                        "ts": datetime.now().isoformat(),
                        "searched": False,
                    }, ensure_ascii=False) + "\n")
                # v13.88.9 · 神经链 v0 · 敲搜索环的门铃(有新告警待搜)·
                # worker 被信号唤醒·不等 10min 轮询窗口。只标记不 spawn(不影响性能)。
                try:
                    import neuro_signal
                    neuro_signal.mark("alert_search", source=component)
                except Exception:
                    pass
        except Exception:
            pass
        return (f"## 外网参考 (8 环闭环·报警→SearchAgent)\n\n"
                f"⏳ 搜索已入队 · query=`{query[:60]}`\n"
                f"异步搜完回填 · 或手动: "
                f"`python3 v11/evolution/alert_search_worker.py --once`")
    except Exception as e:
        return f"## 外网参考\n\n(入队挂: {str(e)[:80]})"


def _legacy_sync_search(component: str, msg: str, key: str) -> str:
    """保留同步搜索实现 · worker 复用 · 不在 notify 路径调"""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        query = f"{component} {msg[:80]}"
        from search_agent import SearchAgent
        sa = SearchAgent(use_cache=True, use_forger=True, use_supervisor=False)
        hits = sa.search(query, mode="fast", top_k=3)

        lines = ["## 外网参考 (8 环闭环·报警→SearchAgent)\n"]
        if not hits:
            lines.append(f"(SearchAgent fast 无命中·query='{query[:60]}')")
        else:
            lines.append(f"query: `{query[:80]}`\n")
            for i, h in enumerate(hits[:3], 1):
                title = (h.get("title") or "?")[:80]
                url = h.get("url", "?")
                source = h.get("source", "?")
                lines.append(f"{i}. [{title}]({url}) · {source}")
        return "\n".join(lines)
    except Exception as e:
        return f"## 外网参考\n\n(SearchAgent 挂: {str(e)[:100]})"

V11 = Path(__file__).parent.parent
ALERTS_LOG = V11 / ".alerts.jsonl"
DEDUP_CACHE = V11 / ".alerts_dedup.json"
# v13.90 · P0 · import 时快照生产路径 · 测试 monkeypatch 改 V11/DEDUP_CACHE 不影响此值
_PROD_V11_STR = str(V11)
RESOLVED_STORE = V11 / ".alerts_resolved.json"  # v13.88.9f · mark_resolved 真状态


def _auto_upstream(component: str) -> str:
    """v13.88.9f · 没显式传 upstream 时·从 depends_on 自动推「谁牵连」
    · guard: code_map_query 挂 / 超时 → 返回「待补」而非崩
    · depends_on 做全仓 grep 可能慢 · 用短超时子进程隔离 · 失败静默
    """
    _NA = "_（待补·未知·可传 upstream= 或跑 depends_on）_"
    try:
        # component 名未必是模块名 (如 pytest:xxx / cdp_dead) · 取合理候选
        cand = component.split(":")[0].strip()
        if not cand or len(cand) < 3:
            return _NA
        # 子进程隔离 + 超时 · 不阻塞 notify 主路径
        code = (
            "import sys,json;sys.path.insert(0,'v11/evolution');"
            "from code_map_query import depends_on;"
            f"r=depends_on({cand!r});"
            "print(json.dumps({'n':r.get('total_hits',0),'k':r.get('by_kind',{})}))"
        )
        r = subprocess.run(["python3", "-c", code],
                           capture_output=True, text=True, timeout=6,
                           cwd=str(V11.parent))
        if r.returncode != 0 or not r.stdout.strip():
            return _NA
        d = json.loads(r.stdout.strip().splitlines()[-1])
        n = d.get("n", 0)
        k = d.get("k", {})
        if n == 0:
            return f"（{cand} 无被 import/shell/cron 引用·影响面小或名字非模块）"
        parts = []
        for lbl, key in [("py import", "py_import"), ("shell", "shell_call"),
                         ("hook", "hook_ref"), ("cron", "cron_ref")]:
            v = k.get(key, 0)
            if v:
                parts.append(f"{lbl} {v}")
        detail = " · ".join(parts) if parts else f"{n} 处引用"
        return f"**{cand}** 被 {n} 处依赖（{detail}）· 挂了会牵连这些调用方（depends_on 自动推）"
    except Exception:
        return _NA


def _read_resolved_status(key: str):
    """v13.88.9f · 读 mark_resolved 落的真状态 · 无则 None (让调用方走老逻辑)"""
    try:
        if not RESOLVED_STORE.exists():
            return None
        store = json.load(open(RESOLVED_STORE))
        rec = store.get(key)
        if not rec:
            return None
        return (f"✅ 已修 · {rec.get('ts','')[:16]} · "
                f"by {rec.get('by','?')} · {rec.get('how','')[:120]}")
    except Exception:
        return None


def mark_resolved(alert_hash: str, how: str, by: str = "unknown") -> dict:
    """v13.88.9f · 修复组件修完主动标记 · 闭环报警「修没修」
    根因: grep mark_resolved 全仓 0 命中 → 674/770 死标未修 (修了也不更新)
    Args:
      alert_hash: 报警 hash (卡片里的 hash / 文件名)
      how: 怎么修的 · 一句话
      by: 谁修的 · fix_pool / code_evolver / proposal_applier / 你
    """
    now = datetime.now()
    rec = {"ts": now.isoformat(), "how": how[:300], "by": by}
    try:
        store = {}
        if RESOLVED_STORE.exists():
            try:
                store = json.load(open(RESOLVED_STORE))
            except Exception:
                store = {}
        store[alert_hash] = rec
        tmp = str(RESOLVED_STORE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
        os.replace(tmp, RESOLVED_STORE)
    except Exception as e:
        return {"ok": False, "err": str(e)[:100]}

    # 回写卡片 md 的「修了没」段 (若存在) · 让文件即时反映真状态
    try:
        hf = V11 / ".alerts_by_hash" / f"{alert_hash}.md"
        if hf.exists():
            txt = hf.read_text(encoding="utf-8")
            new_status = (f"✅ 已修 · {now.strftime('%Y-%m-%d %H:%M')} · "
                          f"by {by} · {how[:120]}")
            import re as _re
            # 匹配 "## 6️⃣ 修了没\n\n<...>\n\n" 段的正文行
            m = _re.search(r"(## 6️⃣ 修了没\n\n)(.*?)(\n\n)", txt, _re.S)
            if m:
                txt = txt[:m.start(2)] + new_status + txt[m.end(2):]
                hf.write_text(txt, encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "hash": alert_hash, **rec}

# 严重度 → osascript sound
SOUND_MAP = {
    "INFO": None,        # 静默 · 只落 log
    "WARN": "Ping",
    "ERROR": "Funk",
    "CRITICAL": "Sosumi",
}

TITLE_MAP = {
    "INFO": "ℹ️",
    "WARN": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🚨",
}


def _load_dedup() -> dict:
    if not DEDUP_CACHE.exists():
        return {}
    try:
        return json.load(open(DEDUP_CACHE))
    except Exception:
        return {}


def _save_dedup(d: dict):
    """v13.88.37 · 加 atomic write · 防 pytest 多进程并发 race"""
    try:
        # atomic · 先写 tmp 再 rename
        tmp = str(DEDUP_CACHE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DEDUP_CACHE)
    except Exception:
        pass


def _hash_msg(component: str, msg: str) -> str:
    """v13.88.37 · 用户 07-24 抓「疯狂重复弹 577 条」
    dedup key 只用 component + msg 前 60 字 (test nodeid 稳定部分)
    · 剥离 traceback / 变量 · 让同 test 反复挂只算 1 条
    v13.88.9i · 2026-07-31 · 用户「backfill_sentinel + watchdog 无数通知·修 hash 也没用」:
      age=90.5h → 第一条正剥到 age=N·但 90.5h 的 Nh 落在 (h|...) 前是 N 非数字·第二条不匹配
      → hash 因残留 Nh/N% 等每次不同·30min 节流全绕过·.alerts_throttled.jsonl 12999 行。
      根治: 第一刀先把整段括号内容 (age=Xh / coverage=80% / rc=0) 整体剥掉·再走数字剥离。
      例: 「ua_kg stale(age=90.5h)·wire发现但」→ 剥括号 → 「ua_kg stale·wire发现但」→ 稳定。
    """
    import re as _re
    # 抽 test nodeid (稳定部分) · 剥离后面变量
    m = _re.search(r"(test_\w+(?:\[[^\]]*\])?)", msg[:200])
    if m:
        stable = m.group(1)
        return hashlib.md5(f"{component}::{stable}".encode()).hexdigest()[:12]
    # ★ 关键一刀: 先把 (key=value) / (rc=0) / (age=90.5h) 整段括号整体剥掉
    #   这样 age / load / coverage / rc 全消失·不再参与 hash
    # v13.90 · P0 语义归一 · 治「{'编排'} vs {'赋能'} 不同 hash → 同类问题各自累积 2025 条」
    #   {…} 集合 / 「…」引用 / fid=ULID / 长 hex → 归一占位 · 同模式同 hash
    msg = _re.sub(r"\{[^{}]{0,80}\}", "SET", msg)
    msg = _re.sub(r"「[^」]{1,40}」", "Q", msg)
    msg = _re.sub(r"fid=[0-9A-Za-z]{6,}", "fid=X", msg)
    msg = _re.sub(r"\b[0-9a-f]{8,}\b", "HEX", msg)
    _norm = _re.sub(r"\([^)]{0,40}\)", "", msg)
    # 再走老数字剥离 (兜底剥残留)
    _norm = _re.sub(r"(age|load|view|cpu|mem|count|n|coverage|rc|score|len)\s*=\s*[\d.]+", r"\1=N", _norm, flags=_re.I)
    _norm = _re.sub(r"[\d.]+\s*(h|s|ms|min|%|条|个)\b", r"N\1", _norm)
    _norm = _re.sub(r"\d+\s*/\s*\d+", "N/N", _norm)
    _norm = _re.sub(r"\d{4}-\d\d-\d\d[T ][\d:]+", "TS", _norm)  # 时间戳
    _norm = _re.sub(r"[\d.]+%", "N%", _norm)
    _norm = _re.sub(r"\b\d+\b", "N", _norm)
    stable = _norm[:60]
    return hashlib.md5(f"{component}::{stable}".encode()).hexdigest()[:12]


# v13.88.37 · 用户 07-24: 「标题上区分 · 谁做的 · 谁修 · 啥时候修 · 让我做的」
# 责任方 4 类:
#   AUTO      - 系统自动修 (未来 code_evolver 消费 · 你不用管)
#   USER      - 需要你手动决策 (架构/删除 · 你必须审)
#   MONITOR   - 只监督 · 不修 (状态断链 · 长期告警)
#   OPUS_DIG  - opus 主动挖的可能坑 · 需你或 code_evolver 判
OWNER_ICONS = {
    "AUTO": "🤖",       # 系统自愈中
    "USER": "👤",       # 你必须做
    "MONITOR": "👀",    # 只观察
    "OPUS_DIG": "🔍",   # opus 挖的
}


# ─── v13.90 · 真家伙 · 全局 LLM 弹窗语义门 ───
# memory 铁则「规则派必移 LLM judge」· 治弹窗风暴断根:
#   机器自检信号(output_reflect/source_health/self_harness/escape_scan 等)直连
#   notify → 白名单打地鼠永远追不上新组件 → 用户被机器自言自语轰炸。
# 门: 每条新指纹(WARN/ERROR)第一次过 haiku 判「值不值得打断用户」·
#   缓存按 hash 落盘·同指纹后续零成本。CRITICAL 永远直弹(真要命不过门)。
# fail-open: LLM 挂/超时 → 弹(宁多弹一条真警·不漏真警)。
_GATE_CACHE = V11 / ".popup_gate_cache.json"


def _popup_gate_llm(component: str, severity: str, msg: str, key: str) -> bool:
    """语义判「这条值不值得弹窗打断用户」· True=弹。缓存+fail-open。"""
    try:
        cache = json.loads(_GATE_CACHE.read_text(encoding="utf-8")) \
            if _GATE_CACHE.exists() else {}
    except Exception:
        cache = {}
    if key in cache:
        return bool(cache[key].get("popup", True))
    verdict = True  # fail-open
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from llm_judge import _judge_via_api
        sys_p = "你是告警弹窗守门员。判断告警是否值得立即打断用户。只回 JSON。"
        prompt = (
            f"组件: {component}\n严重度: {severity}\n内容: {msg[:300]}\n\n"
            "值得弹窗(popup=true)仅当: 需要用户拍板/决策、真线上功能坏"
            "(发帖/登录/API/CDP 挂)、数据安全风险、用户明确让盯的事。\n"
            "不值得(popup=false)当: 机器自检/元分析(审议/反思/演绎/统计汇报)、"
            "自动修复回执、测试/基准信息、数据源健康波动(自愈机制在处理)、长期观察类。\n"
            '只回 {"popup": true} 或 {"popup": false}。')
        raw = _judge_via_api(sys_p, prompt, "haiku")
        if raw:
            low = raw.lower()
            if '"popup": false' in low or '"popup":false' in low:
                verdict = False
            elif '"popup": true' in low or '"popup":true' in low:
                verdict = True
    except Exception:
        verdict = True
    try:
        cache[key] = {"popup": verdict, "ts": datetime.now().isoformat(),
                      "component": component}
        # 容量帽 800 · 超了丢最老
        if len(cache) > 800:
            for k in sorted(cache, key=lambda x: cache[x].get("ts", ""))[:len(cache) - 800]:
                cache.pop(k, None)
        _GATE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception:
        pass
    return verdict


def notify(component: str, severity: str, msg: str,
            throttle_min: int = 30, sound: str = None,
            owner: str = "MONITOR", eta: str = "",
            upstream: str = None, trigger_ctx: str = None,
            root_cause: str = None, todo: str = None) -> dict:
    """统一告警入口 · v13.88.37 加 owner + eta · v13.88.9f 加人卡片 4 字段
    Args:
      component: 组件名
      severity: INFO / WARN / ERROR / CRITICAL
      msg: 消息主体
      owner: AUTO / USER / MONITOR / OPUS_DIG · 责任方 · 标题显示
      eta: 预计修的时机 · 「code_evolver 06:00」/「你审 · 待定」等
      throttle_min: 节流 · 30min 同 hash 只告 1 次
      sound: 覆盖默认声音
      ── v13.88.9f · 人可读卡片 4 字段 (全可选·老调用方不传也跑) ──
      upstream: 上下游/影响面 · 「这报警牵连谁、影响啥」· 不传则从
                depends_on(component) 自动推 (guard·失败静默)
      trigger_ctx: 原始触发 · 「因为哪句用户话/哪个事件触发」
      root_cause: 原因分析 · 「为什么会这样」
      todo: 该做什么 · 修复动作清单
    """
    severity = severity.upper()
    if severity not in SOUND_MAP:
        severity = "WARN"

    # v13.90 · P0 架构止血 · 测试流量物理隔离 (治根因B-1)
    # 根因: pytest 跑测试时 conftest/组件内部 _alert 直连本函数 → fixture 假数据
    #   (如 deduce_hub「演绎真错 [pitfall] some content」) 写进生产 .alerts_by_hash/
    #   .alerts_dedup.json → 测试噪音当真告警弹窗。
    # 隔离条件3条全满足才隔离: ①pytest 环境 ②无 ALERT_BUS_FORCE_PROD 逃生门
    #   ③dedup 路径仍指生产 (测试若已 monkeypatch 到 tmpdir → 放行·不算污染)
    if ("PYTEST_CURRENT_TEST" in os.environ
            and not os.environ.get("ALERT_BUS_FORCE_PROD")
            and str(DEDUP_CACHE).startswith(_PROD_V11_STR)):
        # v13.90 · 隔离但要真语义: 测试期望 hash/throttled 真行为(test_02/05)。
        # 用独立测试 dedup 文件跑真节流·不碰生产·返回形状与生产一致。
        _t_key = _hash_msg(component, msg)
        _t_dedup_p = V11 / ".test_alerts_dedup.json"
        _t_now = datetime.now()
        _t_throttled = False
        try:
            _td = json.loads(_t_dedup_p.read_text(encoding="utf-8")) \
                if _t_dedup_p.exists() else {}
            _t_last = _try_parse_dt((_td.get(_t_key) or {}).get("last", "") \
                                    if isinstance(_td.get(_t_key), dict) else str(_td.get(_t_key, "")))
            if _t_last and _t_now - _t_last < timedelta(minutes=throttle_min):
                _t_throttled = True
            _td[_t_key] = {"last": _t_now.isoformat()}
            _t_dedup_p.write_text(json.dumps(_td, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        try:
            with open(V11 / ".test_alerts.jsonl", "a", encoding="utf-8") as _tf:
                _tf.write(json.dumps({
                    "ts": _t_now.isoformat(), "component": component,
                    "severity": severity, "msg": msg[:300],
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return {"ts": _t_now.isoformat(), "component": component,
                "severity": severity, "msg": msg[:500], "hash": _t_key,
                "throttled": _t_throttled,
                "notified": False, "test_env_isolated": True}

    now = datetime.now()
    key = _hash_msg(component, msg)

    # 节流 · 层 1 · 同 (component, msg) hash 严格 throttle
    dedup = _load_dedup()
    # v13.90 · P0 聚合熔断 · 治「历史 2025 条类似仍每 30min 弹一次」(根因B-3)
    # dedup[key] 新格式 {"last","count","first"} · 兼容旧格式(裸 iso 字符串)
    # 策略: 同 fingerprint 24h 内最多弹 _POPUP_MAX 次
    #   第 _POPUP_MAX 次 · 升级 WARN→ERROR + 落 .alerts_todo.jsonl · 之后熔断只落档案
    _POPUP_MAX = 3
    raw = dedup.get(key)
    last_dt, cnt, first_dt = None, 0, None
    if isinstance(raw, dict):
        last_dt = _try_parse_dt(raw.get("last", ""))
        try:
            cnt = int(raw.get("count", 1))
        except Exception:
            cnt = 1
        first_dt = _try_parse_dt(raw.get("first", ""))
    elif raw:
        last_dt = _try_parse_dt(raw)
        cnt, first_dt = 1, last_dt
    should_alert = True
    if last_dt and now - last_dt < timedelta(minutes=throttle_min):
        should_alert = False
    # 节流窗口过后的重复 → 计数+熔断判定
    if should_alert and raw:
        cnt += 1
        if cnt > _POPUP_MAX:
            should_alert = False  # 熔断 · 只落 throttled 档案·不再弹
        elif cnt == _POPUP_MAX:
            if severity == "WARN":
                severity = "ERROR"  # 持续未解决 · 自动升级
            msg = f"{msg} · [已第{cnt}次·转待办·后续聚合不再弹]"
            try:
                with open(V11 / ".alerts_todo.jsonl", "a", encoding="utf-8") as _tf:
                    _tf.write(json.dumps({
                        "ts": now.isoformat(), "hash": key, "component": component,
                        "count": cnt, "msg": msg[:300],
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass

    # v13.88.66 · 节流层 2 · 组件级速率限制 · 治「287 条 evolver_pending:fix_pool 刷屏」
    # 用户 07-28 抓·同一 component 每个 msg 不同都过·加组件 cap: 15 min 最多 3 条
    # 除 CRITICAL/ERROR 外都限
    if should_alert and severity not in ("CRITICAL", "ERROR"):
        comp_key = f"__comp__:{component}"
        recent = dedup.get(comp_key, [])
        if isinstance(recent, list):
            cutoff = now - timedelta(minutes=15)
            # 保留 15min 内的
            recent = [ts for ts in recent
                      if isinstance(ts, str) and datetime.fromisoformat(ts) > cutoff]
            if len(recent) >= 3:  # 15min 内已 3 条同组件·压
                should_alert = False
        else:
            recent = []

    # v13.88.9 (07-30 修) · 被节流的不写主日志 · 治「.alerts.jsonl 83% 是 throttled 记录」
    #   老逻辑:无论节流与否都写主日志(注释「不受节流影响」)· 导致 6773 条里 5689 条是
    #   throttled 噪声 · 主日志巨大难查 · 还让人误判为「告警刷屏」。
    #   修:真弹(should_alert)才写主 .alerts.jsonl · 被节流的写独立 .alerts_throttled.jsonl
    #   (throttled 仍留档可审计 · 但不污染主告警流)。
    entry = {
        "ts": now.isoformat(),
        "component": component,
        "severity": severity,
        "msg": msg[:500],
        "throttled": not should_alert,
        "hash": key,
    }
    _log_target = ALERTS_LOG if should_alert else (V11 / ".alerts_throttled.jsonl")
    try:
        with open(_log_target, "a", encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if not should_alert:
        return {**entry, "notified": False}

    # ── v13.90 · 真家伙 · LLM 弹窗语义门 (规则派必移 LLM judge) ──
    # 过了节流/熔断/限速的 WARN/ERROR·弹窗前过语义判: 机器自言自语 → 只落档案不弹。
    # CRITICAL 直弹不过门 · INFO 本来就不弹。
    if severity in ("WARN", "ERROR") and not _popup_gate_llm(component, severity, msg, key):
        entry["popup_gate"] = "llm_blocked"
        return {**entry, "notified": False, "popup_gate": "llm_blocked"}

    # osascript 弹窗 · INFO 不弹只 log
    # v13.88.37 · 用户: 「点击后内容空」· 加 subtitle + 落可查文件
    # 2026-07-28 用户: 「时长变常驻·点击后能看到具体消息」
    #   优先 terminal-notifier (-timeout 0 = 常驻 · -execute = 点击打开 latest.md)
    #   兜底 osascript display notification (banner 会消失但 subtitle 带路径提示)
    sound_name = sound or SOUND_MAP.get(severity)
    if severity != "INFO":
        icon = TITLE_MAP.get(severity, "⚠️")
        # v13.88.37 · 加 owner 图标 · 让你一眼知道「是让你做还是自动修」
        _owner_icon = OWNER_ICONS.get(owner, "❓")
        title = f"{_owner_icon} {icon} {component}"
        safe_msg = msg.replace('"', "'").replace("\\", "\\\\")[:400]
        ts_short = now.strftime("%H:%M:%S")
        # v13.88.37 · subtitle 显示 owner + eta · 让你一眼知道谁修啥时候
        _eta_str = f" · {eta}" if eta else ""
        subtitle = f"[{owner}] {severity} · {ts_short}{_eta_str}"

        # v13.88.37 · 2026-07-24 用户抓 3 次 「点击后是空的」
        # 根因: 之前指向 .alerts_latest.md · 后续 alert 覆盖 · 老弹窗点击看到新内容
        # 修: 每条 alert 独立文件 .alerts_by_hash/<hash>.md
        _hash_file = V11 / ".alerts_by_hash" / f"{key}.md"
        # 若尚未生成 · 提前建目录 (下面 md_content 写入会覆盖)
        try:
            _hash_file.parent.mkdir(exist_ok=True)
        except Exception:
            pass

        # ── 路径 A · terminal-notifier (常驻 + 点击对应 hash.md) ──
        # 2026-07-28 v13.88.65 用户再抓「点击又没内容」
        # 病根: -execute 只被「显示」按钮触发·点击通知主体走 -activate/-open
        # 修: 主用 -open file:// (点主体直接打开) + 预先写好 hash 文件
        #      不再用 [ -f ... ] && ... shell 表达式 (terminal-notifier 传参不稳)
        # v13.88.70 · 用户 07-28 抓「区分我要知道的/你要做的/未来要做的」
        # 3 级 audience:
        #   1. user_action_required (WARN/CRITICAL 且 component 在白名单) → 弹通知
        #   2. system_todo (system 自己该修的·evolution 消费) → 只落 KM 日报
        #   3. future_backlog (堆待处理·非紧急) → 只落 .alerts_by_hash
        USER_MUST_KNOW = {
            "production_watchdog",   # goal_driver / publisher 挂 · 影响真发帖
            "publisher",              # 发帖失败
            "meyo_api",              # 上游服务挂
            "cdp_dead",              # Chrome 9224 挂
        }
        # pytest fail / auto_verify / findings 归 system_todo · 不弹
        _user_must_know = (component in USER_MUST_KNOW
                           and severity in ("WARN", "CRITICAL"))
        # v13.88.37 · 2026-07-24 用户抓「还是得弹 · 不能不弹 · 但要区分」
        # 决策 · 全弹 · title 里 owner 图标区分谁修
        #   🤖 AUTO 系统自动修 · 你不用管
        #   👤 USER 你必须审
        #   👀 MONITOR 只看 · 长期观察
        #   🔍 OPUS_DIG opus 挖的可能坑
        # (只有 INFO 级不弹 · WARN 及以上全弹)
        _enable_notifier = True
        tn = _which("terminal-notifier") if _enable_notifier else None
        delivered = False
        # 先把 hash 文件写好·再发通知 (顺序颠倒: 之前是先弹再写·点击竞态可能空)
        try:
            _hash_file.parent.mkdir(parents=True, exist_ok=True)
            _hash_file.write_text(_build_md_content(component, severity, msg, now, key, icon,
                                                     upstream=upstream, trigger_ctx=trigger_ctx,
                                                     root_cause=root_cause, todo=todo), encoding='utf-8')
        except Exception:
            pass
        if tn:
            # v13.88.37 · 2026-07-24 修 3 事
            # v13.88.72 · 用户 07-28 4 次抓「点击又没反应·彻底修好」
            # 根因: terminal-notifier 有 3 个点击行为·-execute 只覆盖 1 个:
            #   · 主体点击 → -open URL (打开 URL)
            #   · 「显示」按钮 → -execute shell (跑命令)
            #   · 单击悬浮气泡 → -activate bundle (激活 app)
            # 修: 3 个都设·任一路径点击都能打开
            #   -open file://path       → 主体点击直接 Preview / macOS 默认打开 md
            #   -execute /usr/bin/open -e path → 「显示」按钮·-e 强制 TextEdit
            #   -activate com.apple.TextEdit → 单击气泡·激活 TextEdit
            file_url = f"file://{_hash_file}"
            open_cmd = f"/usr/bin/open -e '{_hash_file}'"
            # 2026-07-28 用户抓「有的通知点不开」
            # 根因: 之前 -group 用 component 名 · 同组新通知替代旧的 → 旧通知丢 -open action
            # 修: 每条 alert 用 hash 独立 group · 保证每条都能点开
            # 且清理特殊字符 (":" "/" 虽然不崩·避免语义混乱)
            _safe_group = f"alert_bus_{key}"  # key = md5 hash · 天然安全
            cmd = [
                tn,
                "-title", title,
                "-subtitle", subtitle,
                "-message", safe_msg,
                "-open", file_url,           # 主体点击 → 直接打开 md
                "-execute", open_cmd,        # 「显示」按钮 → open -e TextEdit
                "-activate", "com.apple.TextEdit",  # 单击 → 激活 TextEdit
                "-group", _safe_group,
                "-ignoreDnD",
            ]
            if sound_name:
                cmd += ["-sound", sound_name]
            try:
                r = subprocess.run(cmd, timeout=8, capture_output=True, text=True)
                if r.returncode == 0:
                    delivered = True
            except Exception:
                delivered = False

        # ── 路径 B · v13.88.37 · 用户抓「通知形式不一样 · 能不能统一」
        # 兜底 osascript 会产矩形通知 · 破坏统一 · 改为 · 挂了不弹 · 只 log
        # (通知已落 .alerts_latest.md + .alerts_by_hash/<hash>.md · 用户可查)
        if not delivered:
            # 只写 log · 不弹矩形通知
            try:
                _bad_log = V11 / ".alert_bus_delivery_fail.log"
                with open(_bad_log, "a", encoding='utf-8') as _f:
                    _f.write(f"{now.isoformat()} · {component} · {severity} · terminal-notifier 未送达 · content 已落 .alerts_by_hash/{key}.md\n")
            except Exception:
                pass
        # 保留 script 变量避免未定义
        if False:
            hint_msg = f"{safe_msg[:340]}\n→ cat v11/.alerts_latest.md"
            sound_arg = f' sound name "{sound_name}"' if sound_name else ""
            script = (
                f'display notification "{hint_msg}" '
                f'with title "{title}" '
                f'subtitle "{subtitle}"'
                f'{sound_arg}'
            )
            try:
                subprocess.run(["osascript", "-e", script], timeout=3)
            except Exception:
                pass

        # 落 · latest.md (dashboard 用) · by_hash 已在通知前写好 (顺序颠倒防竞态)
        # 2026-07-28 v13.88.65 · 用户抓「点击又没内容」· 病根 = 弹通知先于写文件
        md_content = _build_md_content(component, severity, msg, now, key, icon,
                                       upstream=upstream, trigger_ctx=trigger_ctx,
                                       root_cause=root_cause, todo=todo)
        _latest = V11 / ".alerts_latest.md"
        try:
            _latest.write_text(md_content, encoding='utf-8')
        except Exception:
            pass

    # 更新 dedup · v13.90 · dict 格式 (last/count/first) 支撑聚合熔断
    dedup[key] = {
        "last": now.isoformat(),
        "count": max(cnt, 1),
        "first": (first_dt or now).isoformat(),
    }

    # v13.88.66 · 组件级速率历史 · 用于层 2 节流
    if severity not in ("CRITICAL", "ERROR"):
        comp_key = f"__comp__:{component}"
        comp_recent = dedup.get(comp_key, [])
        if not isinstance(comp_recent, list):
            comp_recent = []
        comp_recent.append(now.isoformat())
        # 只保留 15min 内的
        _cutoff_15 = now - timedelta(minutes=15)
        comp_recent = [ts for ts in comp_recent
                       if isinstance(ts, str) and _try_parse_dt(ts)
                       and _try_parse_dt(ts) > _cutoff_15]
        dedup[comp_key] = comp_recent[-10:]  # 最多存 10 条

    # 清理老 dedup (>24h · 但 __comp__ list 保留)
    cutoff = now - timedelta(hours=24)
    new_dedup = {}
    for k, v in dedup.items():
        if k.startswith("__comp__:"):
            new_dedup[k] = v  # 保留 (已在上面 trim)
        elif isinstance(v, dict):  # v13.90 新格式
            _dt = _try_parse_dt(v.get("last", ""))
            if _dt and _dt > cutoff:
                new_dedup[k] = v
        elif _try_parse_dt(v) and _try_parse_dt(v) > cutoff:
            new_dedup[k] = v
    dedup = new_dedup
    _save_dedup(dedup)

    return {**entry, "notified": True}


def _try_parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def recent(hours: int = 24, severity: str = None) -> list:
    """查最近告警"""
    if not ALERTS_LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    for line in ALERTS_LOG.read_text(encoding='utf-8').splitlines():
        try:
            e = json.loads(line)
            dt = datetime.fromisoformat(e["ts"])
            if dt < cutoff:
                continue
            if severity and e.get("severity") != severity.upper():
                continue
            out.append(e)
        except Exception:
            continue
    return out


def dashboard(hours: int = 24) -> str:
    """v13.88.37 · 用户: 「点击后内容是空的 · 我需要显示内容 · 有没有修复 · 时间 · 内容」
    生 markdown dashboard · 可 cat / open
    """
    alerts = recent(hours=hours)
    # 按组件聚合
    from collections import Counter, defaultdict
    by_component = defaultdict(list)
    by_severity = Counter()
    for a in alerts:
        by_component[a.get("component", "?")].append(a)
        by_severity[a.get("severity", "?")] += 1

    # 查 fix_pool · 已修的
    fixed_bugs = set()
    try:
        fix_pool_path = V11 / ".fix_pool.jsonl"
        if fix_pool_path.exists():
            for line in fix_pool_path.read_text(encoding='utf-8').splitlines():
                try:
                    fx = json.loads(line)
                    # 若 fix 里提到某组件 · 认为该组件相关告警可能已修
                    for f_path in fx.get("files", []):
                        for comp in by_component.keys():
                            if comp in f_path:
                                fixed_bugs.add((comp, fx["ts"], fx["bug"][:80]))
                except Exception:
                    continue
    except Exception:
        pass

    lines = [
        f"# 告警 dashboard · {datetime.now().isoformat()}",
        "",
        f"## 近 {hours}h 概览",
        f"- 总 {len(alerts)} 条",
        f"- 严重度: {dict(by_severity)}",
        f"- 涉及组件: {len(by_component)}",
        "",
        "## 按组件分",
    ]
    for comp, alist in sorted(by_component.items(), key=lambda x: -len(x[1])):
        latest = alist[-1]
        lines.append(f"\n### {comp} · {len(alist)} 次")
        lines.append(f"- 最新 [{latest['ts'][11:19]}] {latest.get('severity','?')} · {latest.get('msg','')[:150]}")
        # 已修?
        related_fix = [(t, b) for c, t, b in fixed_bugs if c == comp]
        if related_fix:
            lines.append(f"- ✅ 相关 fix: {related_fix[-1][1]}")

    lines.append("\n## 未消化 (未在 fix_pool 里)")
    for comp, alist in by_component.items():
        if not any(c == comp for c, _, _ in fixed_bugs):
            latest = alist[-1]
            lines.append(f"- ⚠️ {comp} × {len(alist)} · {latest.get('msg','')[:100]}")

    lines.append(f"\n---\n查历史: `python3 v11/evolution/alert_bus.py --recent {hours}`")
    lines.append(f"最新详情: `cat v11/.alerts_latest.md`")

    dash = "\n".join(lines)
    dash_path = V11 / ".alerts_dashboard.md"
    dash_path.write_text(dash, encoding='utf-8')
    return dash


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--recent", type=int, default=0, help="打印最近 N 小时告警")
    ap.add_argument("--severity", type=str, help="过滤 severity")
    ap.add_argument("--dashboard", type=int, default=0, help="生 markdown dashboard")
    ap.add_argument("--resolve", type=str, default="", help="标记某 hash 已修")
    ap.add_argument("--how", type=str, default="", help="配 --resolve · 怎么修的")
    ap.add_argument("--by", type=str, default="manual", help="配 --resolve · 谁修的")
    ap.add_argument("--demo-card", action="store_true", help="生一张示例人卡片")
    args = ap.parse_args()

    if args.resolve:
        r = mark_resolved(args.resolve, args.how or "(未填修法)", by=args.by)
        print(f"mark_resolved: {r}")
    elif args.demo_card:
        _now = datetime.now()
        _md = _build_md_content(
            "publisher", "CRITICAL",
            "低效帖 fid=01KYRW8Z view=0 title='' persona= 发帖链断 Not logged in",
            _now, _hash_msg("publisher", "低效帖发帖链断"), "🚨",
            upstream="publisher 挂 → goal_driver 真发帖链断 → 帖子发不出去（下游: dashboard 帖数不涨）",
            trigger_ctx="cron publisher_loop 06:00 触发 · 采集到 fid=01KYRW8Z 时 Chrome 未登录",
            root_cause="Chrome 9224 profile 登录态失效 · 采集报错硬门拦下 Not logged in 烂帖",
            todo="1) 重登 Chrome-Agent profile  2) 跑 cdp health 探针  3) --force 兜底补发",
        )
        print(_md)
    elif args.dashboard:
        d = dashboard(hours=args.dashboard)
        print(d)
        print(f"\n落盘: v11/.alerts_dashboard.md")
    elif args.test:
        # 4 级都试
        for sev in ["INFO", "WARN", "ERROR", "CRITICAL"]:
            r = notify("alert_bus_selftest", sev,
                        f"这是 {sev} 级测试消息",
                        throttle_min=0)
            print(f"  {sev}: notified={r['notified']} · hash={r['hash']}")
    elif args.recent:
        recs = recent(hours=args.recent, severity=args.severity)
        print(f"最近 {args.recent}h 告警: {len(recs)} 条")
        for r in recs[-20:]:
            print(f"  [{r['ts'][11:16]}] {r['severity']:8s} · {r['component']:<25s} · {r['msg'][:80]}")
    else:
        ap.print_help()
