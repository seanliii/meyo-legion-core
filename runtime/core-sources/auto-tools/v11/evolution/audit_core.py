#!/usr/bin/env python3
"""
v13.74 · audit_core · 所有 audit agent 的共同基座
────────────────────────────────────────────────────────────────
用户 2026-07-14 抓 R8:
「这次做的跟之前的可以融合吗?一起升级?让架构优雅?」

── 融合前问题 ──
llm_judge / path_monitor / turn_hook / correction_evolver
- subprocess claude 各自实现 (200+ 行重复)
- 落盘各自 .jsonl (dashboard 拼不齐)
- verdict 结构不同 (pass/fail/warn vs verdict/reason)
- push_correction 各自写 (correction_evolver 消费口不统一)

── 融合后架构 ──
                    audit_core
                   ┌────────────┐
                   │ LLM adapter│ ← claude subprocess 只这里
                   │ EventLog   │ ← .audit_events.jsonl 统一落
                   │ Pending    │ ← correction 队列统一入口
                   └────┬───────┘
                        │
      ┌─────────────────┼─────────────────┐
      ↓                 ↓                 ↓
  LLMJudge          PathMonitor       IntentAudit (future)
  (判文本)          (判路径)          (判意图)

── 使用 ──
子类只声明:
- name (unique id)
- tier (routine/critical/deep → haiku/sonnet/opus)
- system_prompt (LLM 独立上下文的角色)
- build_user_msg(inputs) → str
- parse_verdict(raw) → dict (含 verdict/reason)

基类负责:
- run subprocess claude (with retry/timeout)
- 落 .audit_events.jsonl (统一 schema)
- fail → correction_pending 队列
- stats/dashboard 一处查
"""
import os, sys, json, subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Any


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = Path(__file__).resolve().parent.parent.parent
V11 = BASE / "v11"
EVO = V11 / "evolution"
EVENT_LOG = V11 / ".audit_events.jsonl"
PENDING_LOG = EVO / ".correction_pending.jsonl"

# LLM 分层配置
TIER_MODEL = {
    "routine": "haiku",   # turn_hook 每次调
    "critical": "sonnet", # publisher gate / path_monitor 主要用这个
    "deep": "opus",       # 元规则拆 / 复杂纠正分类
}
CLAUDE_CLI = os.environ.get("MEYO_CLAUDE_CLI", "claude")
DEFAULT_TIMEOUT = int(os.environ.get("MEYO_AUDIT_TIMEOUT", "60"))
ESCALATE_ON_WARN = os.environ.get("MEYO_AUDIT_ESCALATE", "1") == "1"


# ────────────── 统一 LLM adapter ──────────────

def call_llm(system_prompt: str, user_msg: str,
             tier: str = "routine", timeout: int = None,
             model_override: str = None) -> dict:
    """独立干净上下文调 claude · 所有 audit agent 都过这里。

    返回: {ok: bool, raw: str, model: str, elapsed: float, error: str}
    """
    import time
    model = model_override or TIER_MODEL.get(tier, "haiku")
    timeout = timeout or DEFAULT_TIMEOUT
    t0 = time.time()
    try:
        # v13.88.9j · 走统一 spawn 闸门 (防 load 雪崩)·闸门拒绝时 returncode=199 走下面降级
        from _spawn_gate import spawn_claude_cli
        r = spawn_claude_cli(
            [CLAUDE_CLI, "--print", "--model", model,
             "--append-system-prompt", system_prompt],
            input=user_msg,
            timeout=timeout,
        )
        elapsed = time.time() - t0
        if r.returncode != 0:
            return {"ok": False, "raw": "", "model": model,
                   "elapsed": elapsed, "error": r.stderr[:200]}
        return {"ok": True, "raw": r.stdout.strip(), "model": model,
                "elapsed": elapsed, "error": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "raw": "", "model": model,
                "elapsed": time.time() - t0, "error": f"timeout {timeout}s"}
    except Exception as e:
        return {"ok": False, "raw": "", "model": model,
                "elapsed": time.time() - t0, "error": str(e)[:200]}


def try_parse_json(raw: str, default: Optional[dict] = None) -> dict:
    """从 LLM 输出抽 JSON (常见 pre/post text 包裹的情况)"""
    if not raw:
        return default or {}
    # 抽第一个 { ... }
    if "{" in raw and "}" in raw:
        try:
            js = raw[raw.index("{"):raw.rindex("}") + 1]
            return json.loads(js)
        except Exception:
            pass
    # 抽第一个 [ ... ]
    if "[" in raw and "]" in raw:
        try:
            js = raw[raw.index("["):raw.rindex("]") + 1]
            parsed = json.loads(js)
            return {"items": parsed} if isinstance(parsed, list) else parsed
        except Exception:
            pass
    return default or {}


# ────────────── 统一事件日志 ──────────────

def _last_line_hash(path) -> str:
    """取文件最后一条非空行的 hash 字段(没有则返回创世哈希)。"""
    try:
        if not os.path.exists(path):
            return "genesis"
        with open(path, "rb") as f:
            # 从尾部读最后一行(小文件直接读末尾即可)
            _tail = f.read()[-8192:].decode("utf-8", "replace").strip().splitlines()
        for _l in reversed(_tail):
            if _l.strip():
                try:
                    return json.loads(_l).get("hash") or "genesis"
                except Exception:
                    return "genesis"
        return "genesis"
    except Exception:
        return "genesis"


def append_event(event: dict):
    """所有 audit agent 的判决都落这里。
    (2026-08-15·审计哈希链#52) 每条事件挂 prev_hash(上一条的 hash)+ 自己的 hash
    = sha256(prev_hash + 本行规范化内容)·形成 append-only 防篡改链。self-modifying
    系统生存要件:自愈逻辑出 bug 也能事后证明"它当初改了什么"·任何一行被篡改/删除→
    verify_chain() 立刻发现断链。"""
    import hashlib
    event.setdefault("ts", datetime.now().isoformat())
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        # (2026-08-15·内核自检抓到并发分叉bug修) 读prev_hash+写必须【串行化】·否则多写者
        #   同时从同一前行算prev_hash→分叉断链(kernel_integrity实证断链就是这个)。用文件锁
        #   把"读上一行hash→算本行hash→写"整段锁起来·并发追加不再分叉。锁不可用时降级(不阻断)。
        _lock_f = None
        try:
            import fcntl as _fcntl
            _lock_f = open(str(EVENT_LOG) + ".lock", "w")
            _fcntl.flock(_lock_f.fileno(), _fcntl.LOCK_EX)
        except Exception:
            _lock_f = None
        try:
            _prev = _last_line_hash(EVENT_LOG)
            event["prev_hash"] = _prev
            _payload = {k: event[k] for k in event if k != "hash"}
            _norm = json.dumps(_payload, ensure_ascii=False, sort_keys=True)
            event["hash"] = hashlib.sha256((_prev + _norm).encode("utf-8")).hexdigest()[:16]
            with open(EVENT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        finally:
            if _lock_f is not None:
                try:
                    import fcntl as _fc2
                    _fc2.flock(_lock_f.fileno(), _fc2.LOCK_UN)
                    _lock_f.close()
                except Exception:
                    pass
    except Exception:
        pass


def verify_chain(path=None) -> dict:
    """校验审计哈希链完整性(#52)。返回 {ok, total, broken_at, reason}。
    每行重算 sha256(prev_hash + 规范化内容)·与存的 hash 比·且 prev_hash 必须等于上一行 hash。
    断链=有人篡改/删除/插入了事件·对 self-modifying 系统是最高级别信号。"""
    import hashlib
    _p = path or EVENT_LOG
    if not os.path.exists(_p):
        return {"ok": True, "total": 0, "broken_at": None, "reason": "empty"}
    _prev_expect = "genesis"
    n = 0
    try:
        with open(_p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                n += 1
                rec = json.loads(line)
                _stored = rec.get("hash")
                _prev = rec.get("prev_hash")
                # 旧数据(无 hash 字段)容忍跳过·只校验有链的部分
                if _stored is None or _prev is None:
                    _prev_expect = _stored or _prev_expect
                    continue
                if _prev != _prev_expect:
                    return {"ok": False, "total": n, "broken_at": i,
                            "reason": f"prev_hash断链: 期望{_prev_expect} 实际{_prev}"}
                _payload = {k: rec[k] for k in rec if k != "hash"}
                _norm = json.dumps(_payload, ensure_ascii=False, sort_keys=True)
                _recalc = hashlib.sha256((_prev + _norm).encode("utf-8")).hexdigest()[:16]
                if _recalc != _stored:
                    return {"ok": False, "total": n, "broken_at": i,
                            "reason": f"内容被篡改: 存{_stored} 重算{_recalc}"}
                _prev_expect = _stored
        return {"ok": True, "total": n, "broken_at": None, "reason": "chain intact"}
    except Exception as e:
        return {"ok": False, "total": n, "broken_at": None, "reason": f"{type(e).__name__}: {e}"}


def read_recent_events(last_n: int = 50, agent_filter: str = None) -> list:
    """读最近 N 条 audit 事件"""
    if not EVENT_LOG.exists():
        return []
    try:
        with open(EVENT_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        events = [json.loads(l) for l in lines[-last_n:] if l.strip()]
        if agent_filter:
            events = [e for e in events if e.get("agent") == agent_filter]
        return events
    except Exception:
        return []


# ────────────── 统一 correction 队列 ──────────────

def push_correction(source: str, text: str, markers: list = None,
                    severity: str = "medium"):
    """所有 audit fail → 落这里 · correction_evolver 唯一消费口"""
    try:
        PENDING_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(),
            "source": source,
            "text": text[:1000],
            "markers": markers or [],
            "severity": severity,
            "processed": False,
        }
        with open(PENDING_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


# ────────────── AuditAgent 基类 ──────────────

class AuditAgent:
    """所有 audit agent 的基类。

    子类必须:
    - name: str            (unique id · 落 event log)
    - tier: str            (routine/critical/deep)
    - system_prompt: str   (LLM 独立上下文角色)

    子类可选覆盖:
    - build_user_msg(inputs: dict) -> str
    - parse_verdict(raw: str) -> dict
    - on_fail(inputs: dict, verdict: dict)
    - severity_of(verdict: dict) -> str
    """
    name: str = "unnamed"
    tier: str = "routine"
    system_prompt: str = "你是审核员。判决 JSON: {verdict: pass|warn|fail, reason: str}"

    def build_user_msg(self, inputs: dict) -> str:
        """默认: JSON 序列化 · 子类通常重写"""
        return json.dumps(inputs, ensure_ascii=False, indent=2)[:3000]

    def parse_verdict(self, raw: str) -> dict:
        """默认: 抽 JSON"""
        return try_parse_json(raw, default={"verdict": "warn", "reason": "no_json"})

    def severity_of(self, verdict: dict) -> str:
        v = (verdict.get("verdict") or "").lower()
        return {"fail": "high", "warn": "medium"}.get(v, "low")

    def on_fail(self, inputs: dict, verdict: dict):
        """fail 时落 correction pending (子类可加 markers)"""
        text = (f"[{self.name}] {verdict.get('reason','')[:200]} · "
                f"inputs={json.dumps(inputs, ensure_ascii=False)[:300]}")
        push_correction(
            source=self.name,
            text=text,
            markers=[self.name] + (verdict.get("markers") or []),
            severity=self.severity_of(verdict),
        )

    def run(self, inputs: dict, meta: dict = None,
            model_override: str = None, timeout: int = None) -> dict:
        """跑一次判决 · 统一入口"""
        import time
        t0 = time.time()
        user_msg = self.build_user_msg(inputs)
        r = call_llm(
            system_prompt=self.system_prompt,
            user_msg=user_msg,
            tier=self.tier,
            timeout=timeout,
            model_override=model_override,
        )
        # 首判
        if r["ok"]:
            verdict = self.parse_verdict(r["raw"])
        else:
            verdict = {"verdict": "warn", "reason": f"llm_err:{r.get('error','')[:80]}"}

        # 升级 (haiku warn/fail → sonnet/opus 复审)
        escalated_from = None
        if (ESCALATE_ON_WARN and self.tier == "routine"
                and verdict.get("verdict") in ("warn", "fail")):
            r2 = call_llm(self.system_prompt, user_msg, tier="deep",
                          timeout=timeout)
            if r2["ok"]:
                verdict2 = self.parse_verdict(r2["raw"])
                escalated_from = verdict.get("verdict")
                verdict = verdict2
                verdict["escalated_from"] = escalated_from
                verdict["escalated_to"] = "opus"

        # 落 event
        event = {
            "agent": self.name,
            "tier": self.tier,
            "verdict": verdict.get("verdict"),
            "reason": (verdict.get("reason") or "")[:300],
            "model": r.get("model"),
            "elapsed_s": round(time.time() - t0, 2),
            "meta": meta or {},
            "escalated_from": escalated_from,
        }
        # 保留 verdict 里其他字段 (missed_steps/fake_steps/...)
        for k, v in verdict.items():
            if k not in ("verdict", "reason", "escalated_from", "escalated_to"):
                event[k] = v
        append_event(event)

        # fail 塞 correction
        if verdict.get("verdict") == "fail":
            self.on_fail(inputs, verdict)

        return verdict


# ────────────── stats ──────────────

def stats(agent_filter: str = None, hours: int = 24) -> dict:
    """统一 dashboard · 一次查所有 audit"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    events = read_recent_events(last_n=1000, agent_filter=agent_filter)
    events = [e for e in events if e.get("ts", "") >= cutoff]
    by_agent = {}
    for e in events:
        a = e.get("agent", "?")
        if a not in by_agent:
            by_agent[a] = {"total": 0, "pass": 0, "warn": 0, "fail": 0}
        by_agent[a]["total"] += 1
        v = e.get("verdict")
        if v in ("pass", "warn", "fail"):
            by_agent[a][v] += 1
    return {"hours": hours, "by_agent": by_agent, "total_events": len(events)}


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["stats", "recent", "verify"])
        p.add_argument("--agent", default=None)
        p.add_argument("--hours", type=int, default=24)
        p.add_argument("--last-n", type=int, default=20)
        args = p.parse_args()
        if args.cmd == "stats":
            print(json.dumps(stats(args.agent, args.hours),
                            ensure_ascii=False, indent=2))
        elif args.cmd == "recent":
            for e in read_recent_events(args.last_n, args.agent):
                print(json.dumps(e, ensure_ascii=False))
        elif args.cmd == "verify":
            print(json.dumps(verify_chain(), ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
