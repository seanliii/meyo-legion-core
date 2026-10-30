"""traversal_core · 2026-08-13 · 统一遍历骨架(可寻址空间 + 证据引导遍历)

一句话:把"agent 处理任意内容"从 N 个模态问题,降维成 1 个遍历问题 + N 个寻址问题。
  · 寻址是工程(每个空间贡献 indexer+reader,即一个 Space 适配器)
  · 遍历是智能(本模块的循环,建一次,复利到所有模态)

统一抽象:所有模态 = 一块带索引的画布 + 同一套遍历循环。
  循环: observe(看当前位置) → score(离目标多远) → decide(下一跳去哪/做什么)
        → act(执行) → 证据图登记 → 停止判断/熔断 → 收敛or继续

空间分两类(关键扩展——不止信息空间):
  · 信息空间(web/image/pdf/table/av/env): agent 单向读取。地址=URL/bbox/(page)/时间戳/路径。
  · 能动空间(human/agent/model): agent 不"读"它,而是向它委托请求、等它回结果。
        地址="谁能帮我",动作=delegate。这是"像人一样"的核心——人卡住时会求助,
        agent 的 act 合法动作里就该有"问人/让人去找"。复用现成 hitl_gate/clarify_protocol。

本骨架自身零解题逻辑:它只调度(observe/score/decide/act),不知道任何模态细节,
  也不替 agent 解题(decide 用 LLM,骨架只负责把上下文喂进去、把动作路由出来)。

设计红线(项目宪法):
  · 遍历逻辑只写这一次,新模态只加 Space 适配器(indexer+reader),不再长第二套循环。
  · 任何降级/失败路径打标,不静默吞。
"""
from __future__ import annotations
import os
import time
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# ═══════════════════════════════════════════════════════════════
# 地址 / 观测 / 动作 —— 空间无关的通用数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class Observation:
    """在某个地址观测到的内容(空间无关)。"""
    addr: str                       # 当前所在地址(URL/p1:table/时间戳/路径/"human")
    content: Any                    # 读到的内容(文本/表记录/帧描述/…)
    candidates: list = field(default_factory=list)   # 可跳转的下一批地址(indexer 提供)
    kind: str = ""                  # 观测类型(text/table/frame/...)
    note: str = ""                  # 空间给的提示(如 needs_ocr / evidence_type=inferred)
    addr_meta: dict = field(default_factory=dict)  # 可选地址元数据(page/grid/time/cell/...)


@dataclass
class Action:
    """决策产出的一个动作。骨架据 kind 路由。"""
    kind: str                       # "hop"|"read"|"compute"|"delegate"|"finish"
    target: str = ""                # hop 的目标地址 / delegate 的对象("human"/"model"/agent名)
    query: str = ""                 # 本步的意图/问题(喂给 reader 或委托对象)
    answer: str = ""                # finish 时的最终答案
    reason: str = ""                # 决策理由(可诊断)
    compute: dict = field(default_factory=dict)  # compute 动作的结构化聚合意图(见 _run_compute)


# ═══════════════════════════════════════════════════════════════
# Space 协议 —— 每个模态只需实现这个(indexer+reader 的统一门面)
# ═══════════════════════════════════════════════════════════════

class Space(Protocol):
    """一个可寻址空间。信息空间与能动空间都实现它。

    实现者只贡献寻址(indexer+reader),不写任何遍历逻辑:
      · entry_addrs(): 初始候选地址(indexer 的入口)
      · observe(addr, query): 读某地址的内容 + 给出可继续跳的候选(reader + indexer)
      · act(action): 执行 hop/delegate 等副作用(信息空间 hop 通常无副作用,
                     能动空间 delegate 会真去问人/模型)
    """
    name: str
    kind: str  # "info" | "agentic"

    def entry_addrs(self) -> list[str]: ...
    def observe(self, addr: str, query: str = "") -> Observation: ...
    def act(self, action: Action) -> dict: ...


# ═══════════════════════════════════════════════════════════════
# 证据图 —— 空间无关的共享机制(登记看过哪、读出什么、避免重复、支撑停止)
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvidenceMap:
    """证据图:遍历的记忆。哪些地址已 examined、各读出什么、置信如何。
    · 防重复:examined 里的地址不再重复深读
    · 支撑停止:连续 N 步无新增证据 → stall 熔断
    · 支撑归因:失败时能看到走过哪、读到啥
    """
    goal: str = ""
    examined: set = field(default_factory=set)
    evidence: list = field(default_factory=list)  # [{addr, content, kind, meta, ts}]
    known_addrs: set = field(default_factory=set)  # indexer 暴露过的地址（含尚未读）
    addr_meta: dict = field(default_factory=dict)  # addr -> 空间提供或核心推断的结构化坐标
    stall_rounds: int = 0

    def seen(self, addr: str) -> bool:
        return addr in self.examined

    def register_candidates(self, addrs: list):
        """登记 indexer 暴露的可寻址位置；不等于已观察。"""
        for addr in addrs or []:
            if isinstance(addr, str) and addr:
                self.known_addrs.add(addr)

    @staticmethod
    def _infer_addr_meta(addr: str) -> dict:
        """兼容旧 Space 的轻量地址解析；新 Space 可通过 Observation.addr_meta 覆盖。"""
        try:
            import re
            text = str(addr or "")
            match = re.fullmatch(r"grid:(\d+),(\d+)", text)
            if match:
                return {"axis": "space", "row": int(match.group(1)), "col": int(match.group(2))}
            match = re.fullmatch(r"t:(\d+(?:\.\d+)?)", text)
            if match:
                return {"axis": "time", "seconds": float(match.group(1))}
            match = re.fullmatch(r"p(\d+):.*", text)
            if match:
                return {"axis": "page", "page": int(match.group(1))}
            if text.startswith("sheet:"):
                return {"axis": "sheet", "sheet": text[len("sheet:"):]}
        except Exception:
            pass
        return {}

    def _relation_digest(self) -> str:
        """输出已读/未读地址的邻接关系，让决策器按空间或时间缺口取证。"""
        try:
            metas = {addr: self.addr_meta.get(addr) or self._infer_addr_meta(addr)
                     for addr in self.known_addrs | self.examined}
            lines = []
            grids = [(addr, meta) for addr, meta in metas.items() if meta.get("axis") == "space"
                     and "row" in meta and "col" in meta]
            if grids:
                seen_grid = {(m["row"], m["col"]) for a, m in grids if a in self.examined}
                missing = [a for a, m in grids if a not in self.examined]
                adjacent = []
                for addr, meta in grids:
                    if addr not in self.examined:
                        continue
                    neighbors = [f"grid:{r},{c}" for r, c in (
                        (meta["row"], meta["col"] + 1), (meta["row"] + 1, meta["col"]))
                        if (r, c) in seen_grid]
                    if neighbors:
                        adjacent.append(f"{addr}邻接{','.join(neighbors)}")
                lines.append("空间网格：已读%d块，未读%s%s" % (
                    len(seen_grid), ",".join(missing[:6]) or "无",
                    "; " + "；".join(adjacent[:4]) if adjacent else ""))
            times = sorted((meta["seconds"], addr) for addr, meta in metas.items()
                           if meta.get("axis") == "time" and "seconds" in meta)
            if times:
                read = [addr for _, addr in times if addr in self.examined]
                unread = [addr for _, addr in times if addr not in self.examined]
                lines.append("时间轴：已读%s；待读%s" % (
                    ",".join(read[:6]) or "无", ",".join(unread[:6]) or "无"))
            pages = sorted((meta["page"], addr) for addr, meta in metas.items()
                           if meta.get("axis") == "page" and "page" in meta)
            if pages:
                unread = [addr for _, addr in pages if addr not in self.examined]
                lines.append("分页：已读%d页块，待读%s" % (
                    sum(1 for _, addr in pages if addr in self.examined), ",".join(unread[:6]) or "无"))
            return " | ".join(lines)
        except Exception:
            return ""

    def record(self, obs: Observation) -> bool:
        """登记一次观测。返回 True 表示带来了新证据(用于 stall 判断)。

        关键(截断根因·记忆教训"修全所有消费点"):结构化数据集(表格记录)
        不能字符截断——否则 decide/finalize 只看到前几行,把"59条里筛7条"
        错算成"3条里筛2条"。证据图对结构化内容【完整保留】,只有自由文本才截断。
        """
        is_new = obs.addr not in self.examined
        self.known_addrs.add(obs.addr)
        self.addr_meta[obs.addr] = dict(obs.addr_meta or self._infer_addr_meta(obs.addr))
        self.register_candidates(obs.candidates)
        self.examined.add(obs.addr)
        # 结构化内容(dict/list)完整保留;纯文本才截断
        if isinstance(obs.content, (dict, list)):
            payload = obs.content
            got_info = bool(payload)
        else:
            payload = str(obs.content) if obs.content is not None else ""
            got_info = bool(payload.strip())
        self.evidence.append({
            "addr": obs.addr, "kind": obs.kind,
            "content": payload, "note": obs.note,
            "addr_meta": self.addr_meta.get(obs.addr, {}),
            "structured": isinstance(obs.content, (dict, list)),
        })
        if is_new and got_info:
            self.stall_rounds = 0
            return True
        self.stall_rounds += 1
        return False

    def digest(self, limit: int = 12) -> str:
        """给 decide/score 的证据摘要(空间无关)。
        结构化证据(表格)完整序列化不截断;长文本截断到 800 字。"""
        lines = []
        for e in self.evidence[-limit:]:
            if e.get("structured"):
                c = json.dumps(e["content"], ensure_ascii=False)  # 完整,不截断
            else:
                c = str(e["content"])[:800]
            lines.append(f"- @{e['addr']} [{e['kind']}] {c}"
                         + (f"  ⟨{e['note']}⟩" if e.get("note") else ""))
        relation = self._relation_digest()
        if relation:
            lines.append(f"[地址关系/探索缺口] {relation}")
        return "\n".join(lines) if lines else "(还没有证据)"


def _short(x, n=600):
    try:
        s = json.dumps(x, ensure_ascii=False)
    except Exception:
        s = str(x)
    return s[:n]


# ═══════════════════════════════════════════════════════════════
# 决策/评分 —— 用 LLM,但骨架不含任何模态/解题知识
# ═══════════════════════════════════════════════════════════════

def _default_llm(system: str, user: str, timeout: float = 90) -> str:
    """默认 LLM 通道:走 qa_llm_gateway。可被注入替换(测试/离线)。"""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import qa_llm_gateway as g
        prompt = (system + "\n\n" + user) if system else user
        return (g.text("opus", prompt, timeout=timeout) or "").strip()
    except Exception as e:
        return f"[LLM_UNAVAILABLE] {type(e).__name__}: {e}"


_DECIDE_SYSTEM = (
    "你是遍历决策器。给定目标、当前证据图、可跳转候选地址,决定下一步。\n"
    "动作(只输出JSON):\n"
    '  {"kind":"read","query":"要在当前内容里找/算什么"}  — 深读当前位置\n'
    '  {"kind":"hop","target":"<候选地址>","query":"去那里找什么"}  — 跳到某候选\n'
    '  {"kind":"compute","compute":{...}}  — 对当前表格【用代码精确聚合】(下详),别自己心算\n'
    '  {"kind":"delegate","target":"human|model","query":"要委托对方做/找什么"}  — 卡住时求助(像人一样)\n'
    '  {"kind":"finish","answer":"<最终答案>"}  — 证据已足以回答目标\n'
    "\ncompute 的结构化聚合(表格空间的遍历语言·比心算可靠·数值题优先用它):\n"
    '  {"kind":"compute","compute":{"op":"sum|mean|count|max|min|argmax|argmin",'
    '"column":"要聚合的列名","columns":["多列求和时列出"],"group_by":"分组列(可选)",'
    '"filter":{"列名":"等于的值"}(可选·支持!=前缀取反),"label_col":"argmax/argmin返回哪列的值"}}\n'
    "  例:食物总额(不含Soda)= {op:sum, columns:[Burgers,Hot Dogs,Salads,Fries,Ice Cream]}\n"
    "  例:哪城销量最高 = {op:argmax, columns:[所有数值列], group_by:Location, label_col:Location}\n"
    "\n原则:证据足够就 finish,别过度遍历。若证据图含【完整表格】且题目要数值聚合/求和/均值/"
    "计数/最值——【必须用 compute 动作让代码算,禁止自己心算大数】(心算会错)。compute 结果会并入证据,"
    "下一步再 finish。非数值题(筛选/找某条目)证据够就直接 finish。只有信息真不足且候选已尽才 delegate。"
    "\n【穷举/列表题·读全优先】(v99m):若目标要【列出全部/所有/every/all/逐个】某类条目,"
    "别只看一个区域就 finish——必须 hop 遍历完【所有候选区域】(还有候选没看=没读全)。"
    "finish 前回头核对:题目要几个/多长,我读到的数量对得上吗?少了就继续 hop 扫剩下区域。"
    "\n【读出题目要算答案】:若读到的是【题目/算式】而目标要【答案】(如约分/化简/求解),"
    "用 compute 或提示下游对读到的值做精确计算,别把题目原样当答案交。"
    "\n证据摘要中的【地址关系/探索缺口】描述了已读/未读的空间块、时间点或页面。"
    "当问题涉及位置、顺序、变化、比较或穷举时，优先按这些关系选择相邻或尚未读的位置，"
    "并把跨位置/跨时间观察明确比较后再 finish；不要把单个帧、单页或单块当成整体。"
    "只输出一个 JSON,不要解释。"
)


# ═══════════════════════════════════════════════════════════════
# 意图回看 —— 在遍历过程中定期回到题目原文·检查当前理解是否偏离
# 用户洞察:「需要理解和回答问题·中间回头看看题和对比中间步骤」
#   「在不同阶段回头看·有没有不理解的地方和误解的地方自己没理解」
#   这是遍历骨架缺失的内核:observe→decide 只看证据和目标·但
#   从不核对"我对目标的理解本身对不对"。agent 可能从第2步开始
#   就按偏离的意图在跑·后面越跑越远·到交卷才发现理解错了。
#   解法:不是软话术("方向对吗")·而是结构化回看:对比
#   【题目原始字面意图】vs【当前正在做的事】·有偏就校正。
# ═══════════════════════════════════════════════════════════════

_INTENT_CHECK_SYSTEM = (
    "你是意图审查器。遍历进行到一半了·回头核对 agent 当前走的路线是否还忠于题目原始意图。\n"
    "这不是看证据够不够(那是 decide 的事)·是看【理解本身有没有偏】。\n"
    "\n"
    "你的工作:对比【题目原始字面意图】和【agent 当前正在做的事】·判断有没有偏移。\n"
    "偏移可以是任何形式的——方向读反、约束漏读、限定词误解、粒度偏移、\n"
    "把穷举做成单值、把实体搞混、自我说服式论证偏离字面意思·等等。\n"
    "不预设偏移类型·你根据这道题的具体情况自己判断。\n"
    "\n"
    "关键原则:\n"
    "  · 只看「题目原文说了什么」vs「agent 当前在做什么」——不对齐就报偏移\n"
    "  · 不给解题步骤·不给答案·只纠正理解方向\n"
    "  · aligned=true 意味着当前路线和题目意图一致·不需要任何废话\n"
    "  · aligned=false 时要说清:偏在哪、题目原始意图的正确理解是什么\n"
    "\n"
    "只输出JSON:\n"
    '  {"aligned":true}\n'
    '  {"aligned":false,"misread":"一句话说清偏在哪","correct_intent":"题目原始意图的正确理解(不给答案·只纠正理解方向)"}\n'
    "只输出一个JSON。"
)


def _intent_recheck(goal: str, emap: EvidenceMap, trace: list,
                     llm: Callable) -> dict:
    """意图回看:对比题目原始意图 vs 当前走的路线。返回 {aligned, misread?, correct_intent?}。
    不是软话术·是结构化核对——LLM 拿到题目原文+走过的路线+当前证据摘要·
    判定 aligned=true/false。fail-safe:LLM 异常返回 aligned=True(不误拦)。"""
    try:
        # 只看最近几步的轨迹(早期步可能还在探索·不判)
        recent = trace[-5:] if len(trace) > 5 else trace
        trace_txt = "\n".join(
            f"  step{t.get('step','?')}: {t.get('action','?')} "
            f"target={t.get('target','')[:40]} query={t.get('query','')[:60]}"
            for t in recent) or "  (还没有行动轨迹)"
        user = (f"题目原始意图: {goal}\n\n"
                f"agent 到目前为止走过的路线:\n{trace_txt}\n\n"
                f"当前证据摘要:\n{emap.digest(limit=6)}\n\n"
                f"核对:agent 当前走的路线是否还忠于题目原始意图?\n输出JSON:")
        raw = llm(_INTENT_CHECK_SYSTEM, user)
        s = (raw or "").strip()
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return {"aligned": True}
        d = json.loads(s[i:j+1])
        # 必须有 aligned 字段·否则当 aligned
        if "aligned" not in d:
            return {"aligned": True}
        return d
    except Exception:
        return {"aligned": True}  # fail-safe:回看失败不误拦


def decide(goal: str, emap: EvidenceMap, candidates: list, cur_addr: str,
           llm: Callable, *, budget_left: int) -> Action:
    # v99m·穷举/列表题:证据摘要给【全部】(不截最近12条)——否则各区域读到的条目
    #   会因 limit 丢失·LLM 无法合并去重成完整列表(用户点破"两块合起来才是完整答案·
    #   系统没合并")。单值题仍用默认 limit(省 token)。
    _gl = (goal or "").lower()
    _exhaustive = any(k in _gl for k in (
        "every", "all ", "list", "each", "全部", "所有", "列出", "逐个", "每个", "每一个"))
    _dig = emap.digest(limit=200) if _exhaustive else emap.digest()
    _hint = ("\n【穷举题·合并去重】上面是你走过所有区域的读数·finish 前把它们【全部合并、"
             "去重、按出现顺序排列】成完整列表·核对数量是否对得上题目要求。" if _exhaustive else "")
    user = (f"目标: {goal}\n当前地址: {cur_addr}\n剩余步数: {budget_left}\n"
            f"可跳转候选地址: {candidates[:20]}\n\n证据图:\n{_dig}{_hint}\n\n"
            f"输出下一步动作 JSON:")
    raw = llm(_DECIDE_SYSTEM, user)
    act = _parse_action(raw)
    if act is None:
        # 解析失败不静默:降级为 read 当前(打标),让下一轮再试
        return Action(kind="read", query=goal, reason=f"[DEGRADED]decide解析失败:{raw[:80]}")
    return act


def _parse_action(raw: str) -> Action | None:
    s = (raw or "").strip()
    # 抠出第一个 JSON 对象
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except Exception:
        return None
    k = d.get("kind", "")
    if k not in ("read", "hop", "compute", "delegate", "finish"):
        return None
    return Action(kind=k, target=d.get("target", ""), query=d.get("query", ""),
                  answer=d.get("answer", ""), reason=d.get("reason", ""),
                  compute=d.get("compute", {}) or {})


# ═══════════════════════════════════════════════════════════════
# compute —— 表格空间的遍历语言:用 pandas 精确聚合(不 eval 任意代码·受限结构化)
# ═══════════════════════════════════════════════════════════════

def _latest_records(emap: EvidenceMap) -> tuple[list, list]:
    """从证据图里取最近一份结构化表(columns + records)。"""
    for e in reversed(emap.evidence):
        c = e.get("content")
        if isinstance(c, dict) and "records" in c:
            return c.get("columns", []) or [], c.get("records", []) or []
    return [], []


def _to_num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def _run_compute(spec: dict, emap: EvidenceMap) -> dict:
    """受限结构化聚合:sum/mean/count/max/min/argmax/argmin·可 filter/group_by。
    只用 pandas 数值运算,不 eval 任何代码(安全)。返回 {ok, result, detail}。"""
    cols, recs = _latest_records(emap)
    if not recs:
        return {"ok": False, "detail": "compute: 证据里没有结构化表"}
    op = (spec.get("op") or "").lower()
    column = spec.get("column") or ""
    columns = spec.get("columns") or ([column] if column else [])
    group_by = spec.get("group_by") or ""
    label_col = spec.get("label_col") or group_by or (cols[0] if cols else "")
    filt = spec.get("filter") or {}

    try:
        import pandas as pd
        df = pd.DataFrame(recs)
    except Exception as e:
        return {"ok": False, "detail": f"compute: pandas 失败 {e}"}

    # filter(支持 "!=值" 取反·纯等值/不等值·不 eval)
    for col, val in (filt.items() if isinstance(filt, dict) else []):
        if col not in df.columns:
            continue
        sval = str(val)
        if sval.startswith("!="):
            df = df[df[col].astype(str) != sval[2:]]
        else:
            df = df[df[col].astype(str) == sval]
    if df.empty:
        return {"ok": False, "detail": f"compute: filter 后无数据 filter={filt}"}

    # 数值列转换
    use_cols = [c for c in columns if c in df.columns]
    if op in ("sum", "mean") and not use_cols:
        return {"ok": False, "detail": f"compute: 列不存在 {columns} (可用列={list(df.columns)})"}

    def _rowsum(row):
        s = 0.0
        for c in use_cols:
            n = _to_num(row.get(c))
            if n is not None:
                s += n
        return s

    try:
        if op == "count":
            res = int(len(df))
            return {"ok": True, "result": res, "detail": f"count(filter={filt})={res}"}
        if op in ("sum", "mean"):
            if group_by and group_by in df.columns:
                sums = {}
                for _, row in df.iterrows():
                    sums.setdefault(row[group_by], []).append(_rowsum(row))
                agg = {k: (sum(v) if op == "sum" else sum(v) / len(v)) for k, v in sums.items()}
                return {"ok": True, "result": agg,
                        "detail": f"{op} by {group_by} over {use_cols}: {agg}"}
            vals = [_rowsum(row) for _, row in df.iterrows()]
            res = sum(vals) if op == "sum" else (sum(vals) / len(vals) if vals else 0)
            return {"ok": True, "result": round(res, 4),
                    "detail": f"{op}({use_cols}) over {len(vals)}行 = {round(res,4)}"}
        if op in ("max", "min", "argmax", "argmin"):
            # 按 group_by 汇总每组的行和(或单列),再取极值
            scored = {}
            key_col = group_by if (group_by and group_by in df.columns) else None
            for _, row in df.iterrows():
                val = _rowsum(row) if use_cols else _to_num(row.get(column))
                if val is None:
                    continue
                key = row[key_col] if key_col else row.get(label_col, "")
                scored[key] = scored.get(key, 0) + val if key_col else val
            if not scored:
                return {"ok": False, "detail": "compute: 无可比较数值"}
            if op in ("max", "argmax"):
                best = max(scored, key=scored.get)
            else:
                best = min(scored, key=scored.get)
            res = best if op in ("argmax", "argmin") else scored[best]
            return {"ok": True, "result": res,
                    "detail": f"{op}: {best}={scored[best]} (共{len(scored)}组·{dict(list(scored.items())[:8])})"}
        return {"ok": False, "detail": f"compute: 不支持的 op={op}"}
    except Exception as e:
        return {"ok": False, "detail": f"compute 异常: {type(e).__name__}: {e}"}


# ═══════════════════════════════════════════════════════════════
# 遍历骨架 —— 唯一的循环(observe→score→decide→act),空间无关
# ═══════════════════════════════════════════════════════════════

@dataclass
class TraversalResult:
    ok: bool
    answer: str
    steps: int
    stop_reason: str
    evidence: list
    trace: list


def traverse(space: Space, goal: str, *, max_steps: int = 8,
             stall_limit: int = 3, llm: Callable | None = None,
             log: Callable[[dict], None] | None = None,
             emap: "EvidenceMap | None" = None,
             intent_recheck_every: int = 3) -> TraversalResult:
    """带目标遍历一个空间,直到收敛/耗尽/求助。这是全系统唯一的遍历循环。

    停止条件(共享机制层):
      · decide 输出 finish 且有答案 → 收敛
      · stall_rounds >= stall_limit → 熔断(连续无新证据·避免空转)
      · step >= max_steps → 预算耗尽
      · delegate 成功拿到外部结果 → 并入证据继续

    intent_recheck_every: 每 N 步做一次意图回看(回题目原文核对当前理解是否偏移)。
      默认3步:第3/6/...步各回看一次。这是用户说的"在不同阶段回头看·有没有
      不理解的地方和误解的地方"的结构化落地——不是软话术·是 LLM 判 aligned=true/false。
      偏了就注入校正提示(只纠正理解方向·不给答案)·让 decide 在正确意图下决策。
      intent_recheck_every=0 关闭此机制(向后兼容)。

    emap: 可选·外部传入的证据图(跨空间遍历时共享·上个空间的证据带到下个空间)。
          不传则新建——现有单空间调用零影响。
    """
    llm = llm or _default_llm
    log = log or (lambda r: None)
    if emap is None:
        emap = EvidenceMap(goal=goal)
    trace: list = []

    # 入口地址(indexer 提供)。空间可给多个入口,先取第一个,其余作候选。
    entries = space.entry_addrs()
    emap.register_candidates(entries)
    cur = entries[0] if entries else ""
    pending_candidates = list(entries[1:])

    step = 0
    stop_reason = "max_steps"
    answer = ""
    _intent_corrections: list = []   # 意图回看产出的校正提示(注入给 decide)
    while step < max_steps:
        step += 1

        # 0. 意图回看(每 intent_recheck_every 步·回题目原文核对理解是否偏移)
        #    用户洞察:"在不同阶段回头看·有没有不理解的地方和误解的地方自己没理解"
        #    这不是软话术·是结构化核对:LLM 对比题目原始意图 vs 当前路线·aligned=false 就注入校正。
        if intent_recheck_every > 0 and step % intent_recheck_every == 0:
            _irc = _intent_recheck(goal, emap, trace, llm)
            if not _irc.get("aligned", True):
                _corr = _irc.get("correct_intent", "")
                _mis = _irc.get("misread", "")
                _intent_corrections.append(
                    f"[意图回看·第{step}步] 检测到理解偏移!"
                    f"偏移:{_mis}。正确理解:{_corr}。"
                    f"请按这个正确理解继续·别沿偏移方向跑。")
                log({"step": step, "phase": "intent_recheck",
                     "aligned": False, "misread": _mis, "correct_intent": _corr})
            else:
                log({"step": step, "phase": "intent_recheck", "aligned": True})

        # 1. observe(reader + indexer)
        obs = space.observe(cur, query=goal)
        new_info = emap.record(obs)
        # 合并候选(去重、剔已看)
        for c in (obs.candidates or []):
            if c not in pending_candidates and not emap.seen(c):
                pending_candidates.append(c)
        log({"step": step, "phase": "observe", "addr": cur, "new_info": new_info,
             "kind": obs.kind, "note": obs.note})

        # 2. 停止:stall 熔断(连续无新证据)
        # v99m(2026-08-13·分数图实证·用户点破"熔断把还在扫新区域误杀"):
        #   熔断只在【没有未探索候选】时才生效。还有没看过的区域(grid块/页/时间段)时·
        #   连续几块读到重复≠探索尽了·不该熔断——继续扫剩下的区域。这是通用机制:
        #   图像grid/表格分节/视频时间段都受益(还有块没扫就别停)。
        _has_unexplored = any(not emap.seen(c) for c in pending_candidates)
        if emap.stall_rounds >= stall_limit and not _has_unexplored:
            stop_reason = "stall_circuit_breaker"
            trace.append({"step": step, "stop": stop_reason,
                          "stall": emap.stall_rounds})
            break
        # 还有未探索候选时·stall 到上限就【强制去探索下一个未看区域】(而非熔断/让LLM再决策)·
        # 保证"读全"优先于"早收敛"。这治穷举题(要遍历完所有区域)。
        if emap.stall_rounds >= stall_limit and _has_unexplored:
            _next = next((c for c in pending_candidates if not emap.seen(c)), None)
            if _next:
                trace.append({"step": step, "action": "hop", "target": _next,
                              "reason": "stall但有未探索区域·强制续扫(读全优先)"})
                log({"step": step, "phase": "force_explore", "target": _next})
                cur = _next
                emap.stall_rounds = 0  # 给新区域重新计数
                continue

        # 3. decide(LLM,骨架不含解题知识)
        # 若意图回看产出了校正·拼进 decide 的上下文(让 LLM 在正确意图下决策)
        _goal_for_decide = goal
        if _intent_corrections:
            _goal_for_decide = goal + "\n\n" + "\n".join(_intent_corrections[-2:])  # 最近2条校正
        act = decide(_goal_for_decide, emap, pending_candidates, cur, llm,
                     budget_left=max_steps - step)
        trace.append({"step": step, "action": act.kind, "target": act.target,
                      "query": act.query[:80], "reason": act.reason[:80]})
        log({"step": step, "phase": "decide", "action": act.kind,
             "target": act.target})

        # 4. act(骨架据 kind 路由)
        if act.kind == "finish":
            # v99m·回头自检(用户点破"做完自己回头看题目反复思考"):想 finish 但【还有
            #   未探索区域】时·先别收——去把没看的区域逐个扫完再收(读全优先于早收敛)。
            #   通用机制:任何空间"还有候选没看就想交卷"都续扫。防死循环:hop过的会进 examined·
            #   未探索候选只减不增(每扫一个少一个)·扫尽自然放行 finish。仅对【穷举/列表题】
            #   强制续扫(目标含"全部/所有/every/all/list/逐个/each");单值题证据够就放行。
            _gl = (goal or "").lower()
            _is_exhaustive = any(k in _gl for k in (
                "every", "all ", "list", "each", "全部", "所有", "列出", "逐个", "每个", "每一个"))
            _unseen = [c for c in pending_candidates if not emap.seen(c)]
            if _is_exhaustive and _unseen:
                _nx = _unseen[0]
                trace.append({"step": step, "action": "hop", "target": _nx,
                              "reason": f"穷举题想finish但还有{len(_unseen)}个区域没看·回头扫全再收"})
                log({"step": step, "phase": "finish_reflect_continue",
                     "unseen": len(_unseen), "target": _nx})
                cur = _nx
                continue
            answer = act.answer
            stop_reason = "converged"
            break
        if act.kind == "delegate":
            # 能动空间:委托人/模型/其他 agent,拿回结果并入证据
            r = space.act(act)
            # v99p · 子任务结果地址化:给 delegate 结果一个可寻址地址,
            # 后续可 hop 回来精读/引用子结果。不再只是一坨 _short(r) 不可寻址。
            _del_addr = f"delegate:{act.target}:{step}"
            _del_content = r.get("detail") or r.get("content") or str(r)
            _is_structured = isinstance(_del_content, (dict, list))
            emap.evidence.append({
                "addr": _del_addr, "kind": "delegated",
                "content": _short(r) if not _is_structured else _del_content,
                "note": act.query[:80],
                "structured": _is_structured,
                "addr_meta": {"axis": "delegate", "target": act.target, "step": step},
            })
            # 把 delegate 结果的子部分暴露为候选地址(让决策器能定向精读)
            _sub_cands = []
            if _is_structured and isinstance(_del_content, list):
                _sub_cands = [f"{_del_addr}:{i}" for i in range(min(len(_del_content), 10))]
            elif _is_structured and isinstance(_del_content, dict):
                _sub_cands = [f"{_del_addr}:{k}" for k in list(_del_content.keys())[:10]]
            emap.register_candidates(_sub_cands)
            log({"step": step, "phase": "delegate", "target": act.target,
                 "ok": bool(r and r.get("ok")), "addr": _del_addr,
                 "sub_addrs": len(_sub_cands)})
            # 委托后回到入口重新观测(拿到新信息)
            continue
        if act.kind == "compute":
            # 表格空间遍历语言:用 pandas 精确算,结果并入证据(治 LLM 心算漂移)
            cr = _run_compute(act.compute, emap)
            emap.evidence.append({
                "addr": f"compute:{act.compute.get('op','?')}", "kind": "computed",
                "content": cr.get("detail", ""), "note": "code-computed·精确值",
                "structured": False,
            })
            emap.stall_rounds = 0  # 算出结果算进展,不触发熔断
            log({"step": step, "phase": "compute", "op": act.compute.get("op"),
                 "ok": cr.get("ok"), "result": _short(cr.get("result"), 80)})
            continue
        if act.kind == "hop" and act.target:
            cur = act.target
            continue
        # read: 深读当前(下一轮 observe 同址会带更细 query)——或换未看候选
        if act.kind == "read":
            # 若当前已看过且无新增,尝试跳到一个未看候选(推进,不原地打转)
            if emap.seen(cur) and pending_candidates:
                nxt = pending_candidates.pop(0)
                cur = nxt
            continue

    # 收尾:若循环结束仍无答案,让 LLM 基于证据图给最终答(不额外遍历)
    if not answer and stop_reason != "converged":
        answer = _finalize(goal, emap, llm)

    return TraversalResult(
        ok=bool(answer and not answer.startswith("[")),
        answer=answer, steps=step, stop_reason=stop_reason,
        evidence=emap.evidence, trace=trace,
    )


_FINALIZE_SYSTEM = (
    "你是基于证据作答的收尾器。原则(通用·非针对某道题):\n"
    "· 涉及数值聚合(求和/平均/计数/最值)时【精确计算·不估算·不心算大数】,"
    "  逐项列出再相加,确保结果准确。\n"
    "· 答案就是提取值/计算值本身,按题目要求的格式(小数位/单位)给出。\n"
    "· 只给最终答案,无解释;证据确实不足才回复 '[INSUFFICIENT] 还缺:...'。"
)


def _finalize(goal: str, emap: EvidenceMap, llm: Callable) -> str:
    user = (f"目标: {goal}\n\n已收集的证据图:\n{emap.digest(limit=30)}\n\n"
            f"基于以上证据,给出目标的最终答案。")
    return llm(_FINALIZE_SYSTEM, user)


# ═══════════════════════════════════════════════════════════════
# 跨空间遍历 —— 一个任务多空间接力(像人:看图认品牌→上网查故事)
# ═══════════════════════════════════════════════════════════════

_NEXT_SPACE_SYSTEM = (
    "你是跨空间遍历调度器。一个任务可能需要多个信息空间接力完成"
    "(如:先看图认出品牌 → 再上网查该品牌的信息)。给定目标、已收集证据、"
    "以及【尚未用过的空间】,判断:证据是否已足以回答目标?若不够,下一个该用哪个空间、去找什么。\n"
    "只输出JSON:\n"
    '  {"done":true,"answer":"<最终答案>"}  — 证据已够,直接给答案\n'
    '  {"done":false,"next_space":"<空间名>","subgoal":"在那个空间要找的具体子问题"}  — 换空间接力\n'
    '  {"done":false,"next_space":"","answer":"[INSUFFICIENT] 还缺:..."}  — 没有可用空间了\n'
    "subgoal 要具体(带上已知线索,如'查 Ruffwear 官网 2022-12-8 的故事提到什么肉')。只输出一个JSON。"
)


def traverse_multi(spaces: dict, goal: str, *, max_steps_each: int = 5,
                   max_hops: int = 4, llm: Callable | None = None,
                   log: Callable[[dict], None] | None = None) -> TraversalResult:
    """跨空间遍历:多个 Space 接力完成一个任务,共享同一份证据图。

    spaces: {空间名: Space实例}。调度器(LLM)决定先用哪个、卡住后换哪个、带什么子目标。
    像人一样:图像空间认出品牌 → 证据带给 web 空间查故事 → 汇总回答原目标。

    每个空间内部仍是同一个单空间 traverse(零新增遍历逻辑),这里只做【空间间调度】。
    """
    llm = llm or _default_llm
    log = log or (lambda r: None)
    emap = EvidenceMap(goal=goal)      # 跨空间共享的证据图(关键:上个空间证据带到下个)
    used: set = set()
    trace: list = []
    hop = 0
    cur_goal = goal

    while hop < max_hops:
        hop += 1
        # 1. 调度:选下一个空间(或判定已够)
        avail = [n for n in spaces if n not in used]
        digest = emap.digest(limit=20)
        raw = llm(_NEXT_SPACE_SYSTEM,
                  f"原目标: {goal}\n当前子目标: {cur_goal}\n已用空间: {sorted(used)}\n"
                  f"未用空间: {avail}\n\n证据:\n{digest}\n\n输出JSON:")
        dec = _parse_next(raw)
        trace.append({"hop": hop, "decision": dec})
        log({"hop": hop, "phase": "schedule", **dec})

        if dec.get("done"):
            return TraversalResult(ok=True, answer=dec.get("answer", ""),
                                   steps=hop, stop_reason="cross_space_converged",
                                   evidence=emap.evidence, trace=trace)
        nxt = dec.get("next_space", "")
        if not nxt or nxt not in spaces:
            # 没有可用空间 → 用现有证据收尾
            ans = dec.get("answer") or _finalize(goal, emap, llm)
            return TraversalResult(ok=bool(ans and not ans.startswith("[")),
                                   answer=ans, steps=hop,
                                   stop_reason="no_more_space",
                                   evidence=emap.evidence, trace=trace)
        # 2. 在选定空间遍历(共享 emap·带上子目标)
        used.add(nxt)
        cur_goal = dec.get("subgoal") or goal
        sub = traverse(spaces[nxt], cur_goal, max_steps=max_steps_each,
                       llm=llm, log=log, emap=emap)
        trace.append({"hop": hop, "space": nxt, "sub_stop": sub.stop_reason,
                      "sub_ans": (sub.answer or "")[:80]})

    # hops 耗尽 → 基于全部跨空间证据收尾
    ans = _finalize(goal, emap, llm)
    return TraversalResult(ok=bool(ans and not ans.startswith("[")),
                           answer=ans, steps=hop, stop_reason="max_hops",
                           evidence=emap.evidence, trace=trace)


def _parse_next(raw: str) -> dict:
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {"done": False, "next_space": "", "answer": f"[DEGRADED]调度解析失败:{s[:60]}"}
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return {"done": False, "next_space": "", "answer": f"[DEGRADED]调度JSON坏:{s[:60]}"}


# ═══════════════════════════════════════════════════════════════
# 自测(dry-run:用一个假 Space 验证循环逻辑,不依赖任何模态/网络)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 假信息空间:3个地址,目标在第2个
    class _FakeSpace:
        name, kind = "fake", "info"
        def entry_addrs(self): return ["a", "b", "c"]
        def observe(self, addr, query=""):
            data = {"a": "无关", "b": "答案是42", "c": "也无关"}
            return Observation(addr=addr, content=data.get(addr, ""),
                               candidates=["a", "b", "c"], kind="text")
        def act(self, action): return {"ok": True}

    # 假 LLM:看到证据里有"42"就 finish,否则 hop 到 b
    def _fake_llm(system, user, timeout=90):
        if "42" in user:
            return '{"kind":"finish","answer":"42"}'
        return '{"kind":"hop","target":"b","query":"找答案"}'

    r = traverse(_FakeSpace(), "找出答案数字", llm=_fake_llm, max_steps=5)
    assert r.ok and r.answer == "42", f"骨架循环 fail: {r}"
    assert r.stop_reason == "converged", r.stop_reason
    print(f"dry-run self-test ✓ · steps={r.steps} answer={r.answer!r} "
          f"stop={r.stop_reason} evidence={len(r.evidence)}")

    # ---- 跨空间接力 dry-run:空间A认出"品牌X",空间B据此查到"bacon" ----
    class _SpaceA:  # 图像类:读出品牌
        name, kind = "img", "info"
        def entry_addrs(self): return ["full"]
        def observe(self, addr, query=""):
            return Observation(addr="full", content="品牌是 BrandX", kind="vision")
        def act(self, a): return {"ok": True}

    class _SpaceB:  # web 类:据品牌查到答案
        name, kind = "web", "info"
        def entry_addrs(self): return ["search"]
        def observe(self, addr, query=""):
            # 只有当子目标带了 BrandX 才"查到"答案(验证证据跨空间传递)
            hit = "brandx" in query.lower()
            return Observation(addr="search",
                               content="故事提到 bacon" if hit else "没头绪",
                               kind="text")
        def act(self, a): return {"ok": True}

    def _multi_llm(system, user, timeout=90):
        if "跨空间遍历调度器" in system:            # 调度决策
            if "bacon" in user:
                return '{"done":true,"answer":"bacon"}'
            if "BrandX" in user or "品牌" in user:   # 认出品牌了 → 切 web 查
                return '{"done":false,"next_space":"web","subgoal":"查 BrandX 的故事提到什么肉"}'
            return '{"done":false,"next_space":"img","subgoal":"认出图里的品牌"}'
        # 空间内决策:读到内容就 finish
        if "bacon" in user: return '{"kind":"finish","answer":"bacon"}'
        if "BrandX" in user or "品牌" in user: return '{"kind":"finish","answer":"BrandX"}'
        return '{"kind":"read","query":"读"}'

    rm = traverse_multi({"img": _SpaceA(), "web": _SpaceB()},
                        "图里品牌的故事提到什么肉", llm=_multi_llm, max_hops=4)
    assert rm.ok and "bacon" in rm.answer, f"跨空间接力 fail: {rm}"
    print(f"cross-space self-test ✓ · hops={rm.steps} answer={rm.answer!r} "
          f"stop={rm.stop_reason} evidence={len(rm.evidence)}")
