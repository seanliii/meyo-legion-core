#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legion_memory · 军团跨家经验共享记忆层(军团独有价值·不碰各家自有记忆)

设计依据(扒四家harness真源码·见 dev/legion-memory-harness-study.md):
  - 可插拔抽象接口 ← hermes/agent/memory_provider.py:104 MemoryProvider(ABC)
    (name/is_available/add/search·one-external限制·trivial跳过)
  - jsonl 文件落盘 ← deepseek examples/mcp-memory(MEMORY_FILE_PATH jsonl·无向量库无grpc)
  - 人可读/git版本化 ← cc CLAUDE.md/MEMORY.md markdown 思路
  - 不外挂重服务(放弃Mem0 grpc·teamai需建仓登录)——纯文件·零依赖·任何用户开箱即用

为什么军团要这层(而不是各家自己记):
  四家记忆哲学不同(hermes可插拔/deepseek压缩/codex history/cc markdown)·各家记忆归各家不动。
  军团独有价值 = 【跨家经验流动】:codex 这次学到的·cc 下次能看到。这层只做这件事·不和任何家冲突。

存储:~/.meyo/legion/experience.jsonl(每行一条经验·可读·可git)。
接口(仿 hermes MemoryProvider·可换后端):add / search / recall / is_available。
"""
import fcntl
from functools import wraps
import json
import os
import re
import time
import threading
import uuid
from typing import Optional

import legion_paths as _paths

_MEM_DIR = _paths.legion_dir()
_MEM_FILE = os.environ.get("LEGION_MEMORY_FILE",
                           os.path.join(_MEM_DIR, "experience.jsonl"))
_MAX_KEEP = 2000  # 经验条数上限·超了裁最旧(防无限膨胀·学deepseek有界)
_WRITE_LOCK = threading.RLock()


def _serialized(function):
    """Keep receipt identities stable across CLI and in-process read/modify/write.

    Atomic replacement alone cannot stop another search/add from overwriting a
    newly assigned legacy ID. All writers use the same short storage lock.
    """
    @wraps(function)
    def locked(*args, **kwargs):
        with _WRITE_LOCK:
            os.makedirs(os.path.dirname(os.path.abspath(_MEM_FILE)), exist_ok=True)
            with open(f"{_MEM_FILE}.lock", "a", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    return function(*args, **kwargs)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return locked


def is_available() -> bool:
    """记忆层是否就绪(仿hermes:115·纯文件后端总是可用·无需凭证)。"""
    try:
        os.makedirs(_MEM_DIR, exist_ok=True)
        return True
    except Exception:
        return False


def _is_trivial(text: str) -> bool:
    """琐碎内容不值得记(仿hermes:84 is_trivial_prompt·省噪省空间)。"""
    t = (text or "").strip()
    if len(t) < 8:
        return True
    trivial = ("你好", "hi", "hello", "谢谢", "thanks", "ok", "好的", "test")
    return t.lower() in trivial


# 记忆分层(用户 2026-08-28 goal·依据 hermes memory_provider.py 生命周期钩子 + 军团自查收敛):
#   working   =工作记忆·本次执行过程(怎么抓/怎么解)·【可跨用户】通用无隐私
#   user      =用户记忆·某用户偏好/身份/已知事实·【不跨用户】隐私隔离
#   long      =长期记忆·沉淀的问答/结论(现有默认层)·带 user 归属·跨用户需脱敏
#   procedural=程序化记忆·抽象方法/套路(如"BP=财务推演+竞品+PDF")·【可跨用户】方法论无隐私
# 哪些能跨用户:working/procedural 天然可共享(通用·快);user 绝不跨;long 默认按 user 隔离。
_SHAREABLE_TYPES = {"working", "procedural"}   # 跨用户可召回的类型(其余按 user 隔离)
_VALID_MTYPES = {"working", "user", "long", "procedural"}


@_serialized
def add(question: str, answer: str, source: str = "legion",
        tags: Optional[list] = None, mtype: str = "long", user_id: str = "") -> dict:
    """记一条跨家经验:某问题·某家怎么答的。别家下次 search 能捞到。

    context not control:只记事实(谁答了什么)·不评判对错·让检索方自己判断。
    mtype=记忆类型(working/user/long/procedural·见上)·决定能否跨用户召回。
    user_id=归属用户(user 类型必带·long 可带做隔离;working/procedural 可空=全局共享)。
    """
    if not is_available():
        return {"ok": False, "error": "记忆目录不可用"}
    q, a = str(question or "").strip(), str(answer or "").strip()
    if _is_trivial(q) or not a:
        return {"ok": False, "skipped": "trivial", "reason": "问题太琐碎或无答案·不记"}
    mt = mtype if mtype in _VALID_MTYPES else "long"
    rec = {
        "entry_id": uuid.uuid4().hex,     # 单条持久身份，不由问题/来源或调用者猜测
        "q": q[:500], "a": a[:2000], "source": source,
        "tags": tags or [], "ts": int(time.time()),
        "mtype": mt,                     # 记忆类型(分层召回/跨用户判定用)
        "user_id": str(user_id or ""),   # 归属用户(隔离用·空=全局)
        # 使用信号(学 teamai-cli 置信度飞轮·借鉴见 dev/legion-memory-gap-vs-4families.md #3)
        # recalled=被检索命中次数·adopted=某家真采纳这条经验次数。只记事实·不否决·让检索方自己判断。
        "recalled": 0, "adopted": 0,
    }
    try:
        with open(_MEM_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim_if_needed()
        return {"ok": True, "stored": rec}
    except Exception as e:
        return {"ok": False, "error": f"写入失败:{e}"}


def _load() -> list:
    if not os.path.isfile(_MEM_FILE):
        return []
    out = []
    try:
        for line in open(_MEM_FILE, "r", encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _trim_if_needed():
    recs = _load()
    if len(recs) <= _MAX_KEEP:
        return
    recs = recs[-_MAX_KEEP:]  # 留最新
    try:
        with open(_MEM_FILE, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass


@_serialized
def delete(ts: int, q: str, user_id: str = "") -> dict:
    """删除用户自己的某条记忆(配置md·用户诉求)。按 ts+q 定位·【只删属于该 user_id 的】·
    绝不删别人的(隔离)。ts+q 组合足够唯一(同用户同秒同问题极罕见)。"""
    recs = _load()
    if not recs:
        return {"ok": False, "error": "无记忆可删"}
    uid = str(user_id or "")
    q_head = str(q or "")[:60]
    kept, removed = [], 0
    for r in recs:
        # 只有归属匹配 + ts匹配 + q前缀匹配 才删(三重定位·防误删/删别人的)
        r_uid = str(r.get("user_id") or "")
        owner_ok = (r_uid == uid) or (uid == "default" and not r_uid)
        if owner_ok and int(r.get("ts", 0)) == int(ts) and str(r.get("q", ""))[:60] == q_head:
            removed += 1
            continue
        kept.append(r)
    if removed == 0:
        return {"ok": False, "error": "没找到匹配且属于你的记忆(不能删别人的)"}
    try:
        tmp = f"{_MEM_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, _MEM_FILE)
        return {"ok": True, "removed": removed, "remaining": len(kept)}
    except Exception as e:
        return {"ok": False, "error": f"删除写回失败: {e}"}


def _confidence(rec: dict) -> float:
    """经验置信度(学 teamai-cli:基础×0.4 + 时近×0.3 + 采纳率×0.3)。

    只用于排序/淘汰的软信号·不做门控否决(context not control·纲领铁律10)。
    """
    recalled = max(0, int(rec.get("recalled", 0)))
    adopted = max(0, int(rec.get("adopted", 0)))
    base = 0.5  # 存在即有基础分
    # 时近性:30 天内接近 1·越旧越衰减(不硬删旧的·只是排序沉底)
    age_days = max(0.0, (time.time() - float(rec.get("ts", 0))) / 86400.0)
    recency = 1.0 / (1.0 + age_days / 30.0)
    # 采纳率:被真用过的经验更可信;从没被召回的按 0 算(不惩罚·只是不加分)
    adopt_rate = (adopted / recalled) if recalled > 0 else 0.0
    return round(base * 0.4 + recency * 0.3 + adopt_rate * 0.3, 4)


def _score(rec: dict, terms: list, raw_q: str = "") -> float:
    blob = f"{rec.get('q','')} {rec.get('a','')} {' '.join(rec.get('tags') or [])}".lower()
    s = sum(3 for t in terms if t in blob)
    # 中文无空格分词:整句/2-gram 子串重叠(补分词匹配不到的·如"法国首都"vs"法国的首都")
    rq = (raw_q or "").lower()
    if rq:
        # 2-gram 重叠计数(中文按字滑窗)
        grams = {rq[i:i+2] for i in range(len(rq) - 1) if rq[i:i+2].strip()}
        s += sum(1 for g in grams if len(g) == 2 and g in blob)
    if s <= 0:
        return 0.0
    # 相关性为主·置信度做微调(高采纳/新鲜的同分经验排前·学 teamai 使用信号驱动演化)
    return s + _confidence(rec)


def _matched_missing(terms: list, hits: list) -> dict:
    """检索透明:告诉调用方哪些查询词命中了·哪些没覆盖(学 teamai Matched:…|Missing:…)。

    这是"给 context 不加 control"的正解:暴露检索盲区·让模型自己决定要不要换词再搜。
    """
    blob = " ".join(
        f"{h.get('q','')} {h.get('a','')} {' '.join(h.get('tags') or [])}"
        for h in hits
    ).lower()
    matched = [t for t in terms if t in blob]
    missing = [t for t in terms if t not in blob]
    return {"matched": matched, "missing": missing}


def _visible_to(rec: dict, user_id: str) -> bool:
    """记忆隔离(隐私铁律):working/procedural【可跨用户】任何人可召回;user/long【按归属隔离】——
    只有本人(rec.user_id==user_id)可召回。旧记录无 user_id 时视为归属 default(仅 default 可见·
    不泄露给别的用户)。user_id 为空(未登录/单机)时按 default 处理。
    """
    mt = rec.get("mtype", "long")
    if mt in _SHAREABLE_TYPES:
        return True                       # 工作/程序记忆·通用无隐私·跨用户共享
    owner = rec.get("user_id", "") or "default"
    return owner == (user_id or "default")   # 用户/长期记忆·只对归属用户可见


def _owned_by(rec: dict, user_id: str) -> bool:
    # Legacy memory keeps its existing default-account ownership semantics.
    # Public working/procedural visibility is not permission to edit its owner.
    return bool(user_id) and (rec.get("user_id") or "default") == user_id


@_serialized
def search(query: str, top: int = 3, user_id: str = "", mtypes: Optional[list] = None) -> dict:
    """检索相关的历史经验(recall)。返回 top-N + 来源家 + 时间。

    仿 hermes RecallStatus:透明告知捞到什么·让调用方(军团/模型)自己决定用不用(context not control)。
    user_id=当前用户(隔离 user/long·working/procedural 不受限跨用户共享)。
    mtypes=只召回这些类型(None=全部)·比如只要程序化方法可传 ["procedural"]。
    """
    q = str(query or "").lower().strip()
    terms = [t for t in re.split(r"[\s/,]+", q) if t]
    if not terms:
        return {"query": query, "recalled": 0, "experiences": []}
    recs = _load()
    # 隔离过滤:先按用户可见性 + 类型筛(隐私铁律·跨用户学习只发生在 working/procedural)
    _allow = set(mtypes) if mtypes else None
    visible = [r for r in recs
               if _visible_to(r, user_id) and (_allow is None or r.get("mtype", "long") in _allow)]
    scored = sorted(((_score(r, terms, q), r) for r in visible), key=lambda x: -x[0])
    hits = [r for sc, r in scored if sc > 0][:top]
    # 使用信号回流:被检索命中的经验 recalled+1(学 teamai 双票之"召回票"·驱动 confidence 演化)
    usage_persisted = True
    if hits:
        previous = [(h, h.get("recalled", 0), h.get("entry_id")) for h in hits]
        for h in hits:
            if not h.get("entry_id"):
                h["entry_id"] = uuid.uuid4().hex
            h["recalled"] = int(h.get("recalled", 0)) + 1
        usage_persisted = _rewrite(recs)
        if not usage_persisted:
            # Continue to offer the readable facts, not an ID that never reached
            # disk or a fabricated persisted recall/adoption counter.
            for row, recalled, entry_id in previous:
                row["recalled"] = recalled
                if entry_id:
                    row["entry_id"] = entry_id
                else:
                    row.pop("entry_id", None)
    mm = _matched_missing(terms, hits)
    return {
        "query": query,
        "total_memory": len(recs),
        "recalled": len(hits),
        "usage_persisted": usage_persisted,
        "matched": mm["matched"],   # 命中了哪些查询词
        "missing": mm["missing"],   # 哪些词没覆盖(暴露盲区·让调用方决定要不要换词再搜)
        "experiences": [
            {"entry_id": h.get("entry_id", ""),
             "adoptable": bool(h.get("entry_id")) and _owned_by(h, str(user_id or "").strip()),
             "q": h["q"], "a": h["a"][:400], "from": h.get("source"),
             "when": h.get("ts"), "confidence": _confidence(h),
             "mtype": h.get("mtype", "long"),   # 记忆类型(工作/程序/长期)·不含 user_id 保隐私
             "used": {"recalled": h.get("recalled", 0), "adopted": h.get("adopted", 0)}}
            for h in hits
        ],
        "hint": "这是军团历史上别家/自己答过的相似问题·供参考·不一定对·自己判断;"
                "确实采用且 adoptable 的条目可用 entry_id 回报；检索投递不等于采用。",
    }


def _rewrite(recs: list) -> bool:
    """整体回写(用于更新使用信号后落盘)。原子替换:防并发写留半行/损坏 JSONL。

    军团 hermes dogfood 揪出:直接 open(w) 覆盖·并发 add/search 会互相踩→半行→_load 静默吞
    坏行→经验凭空消失(飞轮空转)。tmp+os.replace 保证要么旧全量要么新全量·永不半截。
    """
    try:
        import os as _os
        tmp = f"{_MEM_FILE}.tmp.{_os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, _MEM_FILE)
        return True
    except Exception:
        return False


@_serialized
def mark_adopted(entry_id: str, *, user_id: str = "") -> dict:
    """某家真采纳了某条经验时调用:adopted+1(学 teamai 双票之"采纳票")。

    采纳票权重高于召回票:被真用过的经验 confidence 升·排序更靠前(使用信号事后收敛)。
    """
    uid, identity = str(user_id or "").strip(), str(entry_id or "").strip()
    if not uid or not identity:
        return {"ok": False, "marked": 0, "error": "需要绑定用户和本次召回的 entry_id"}
    recs = _load()
    matches = [row for row in recs
               if row.get("entry_id") == identity and _owned_by(row, uid)]
    if len(matches) != 1:
        return {"ok": False, "marked": 0, "error": "未找到唯一且属于当前用户的经验"}
    matches[0]["adopted"] = int(matches[0].get("adopted", 0)) + 1
    if not _rewrite(recs):
        return {"ok": False, "marked": 0, "error": "经验采纳回执写入失败"}
    return {"ok": True, "marked": 1, "entry_id": identity}


@_serialized
def prune(min_conf: float = 0.15) -> dict:
    """淘汰低置信度经验(学 teamai:confidence <0.15 prune)。

    仅在被检索多次仍没人采纳、且陈旧时才会跌破阈值——事后治理·非入口门控。
    """
    recs = _load()
    kept = [r for r in recs if _confidence(r) >= min_conf or int(r.get("recalled", 0)) == 0]
    removed = len(recs) - len(kept)
    if removed:
        _rewrite(kept)
    return {"pruned": removed, "kept": len(kept), "min_conf": min_conf}


def stats() -> dict:
    recs = _load()
    from collections import Counter
    by_src = Counter(r.get("source", "?") for r in recs)
    return {"total": len(recs), "by_source": dict(by_src),
            "file": _MEM_FILE, "available": is_available()}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "search":
        print(json.dumps(search(" ".join(sys.argv[2:])), ensure_ascii=False, indent=1))
    elif len(sys.argv) >= 4 and sys.argv[1] == "add":
        print(json.dumps(add(sys.argv[2], sys.argv[3]), ensure_ascii=False))
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
