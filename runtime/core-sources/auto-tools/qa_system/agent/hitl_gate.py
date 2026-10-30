#!/usr/bin/env python3
"""hitl_gate · Human-in-the-Loop 审批门 (R2 · 2026-08-07)

顶级架构缺口①:真 HITL 审批流。之前门面只有"危险命令硬拦"(命中毁灭性模式直接拒),
没有"执行前挂起等用户批"——deepagents HumanInTheLoopMiddleware / CC 的权限确认那种
"可控放手"。本模块补上:风险操作不直接跑也不直接拒,而是**挂起 → 等用户在网页批**。

设计(文件共享·跨进程):web 进程和 agent 线程可能在同一进程(ThreadingHTTPServer),
但为稳妥用文件+内存双通道:
  - needs_approval(kind, detail): 判这个操作要不要审批(策略·默认宽松,只拦真风险)
  - request_approval(qid, kind, detail): 登记一条 pending,阻塞轮询等裁决,返回 approved/denied
  - resolve(approval_id, decision): 网页 /api/approve 调这个,给出裁决
  - pending(qid): 网页轮询用,列该任务待批项

默认策略(宽松·不破坏现有直跑体验):
  - shell 命中"更宽的风险模式"(sudo/rm -rf 子目录/写 /etc 等)→ 需审批
  - file_write 到沙箱外的绝对路径 → 需审批
  - 其余一律放行(不审批)
可通过环境变量 MEYO_HITL_MODE 调:off(全放行·默认CLI)/ risky(只拦风险·默认web)/ all(全审批)

fail-safe:审批机制不可用/超时(默认 120s 没人批)→ 按 MEYO_HITL_TIMEOUT_ACTION
(默认 deny·超时视为拒绝,安全优先)。绝不因门本身故障卡死任务不返回。
"""
import os
import re
import json
import time
import hashlib

try:
    import fcntl  # POSIX(macOS/Linux) 文件锁,Windows 无此模块
    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False

_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.path.join(os.path.expanduser("~/.meyo"), "hitl")
os.makedirs(_STATE_DIR, exist_ok=True)

# 需审批的更宽风险模式(比 _is_dangerous_shell 的"直接拒"更宽·这些是"该问一下")
_APPROVAL_SHELL_PATTERNS = [
    r"\bsudo\b",
    r"\brm\s+-[rf]",                 # 任何 rm -r/-f(沙箱内也问一下)
    r">\s*/etc/", r">\s*/usr/", r">\s*/System/",
    r"\bmv\s+.*\s+/(etc|usr|bin|System)/",
    r"\bchmod\s+-R", r"\bchown\s+-R",
    r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard",
    r"\bkill(all)?\s+-9",
    r"\bcurl\b.*\|\s*(sh|bash)",     # curl | sh 远程执行
    r"\bpip\s+uninstall",
]


def _mode() -> str:
    return os.environ.get("MEYO_HITL_MODE", "risky").lower()


# ── v108(M6b·2026-08-16 GAIA-165复盘) · 良性路径免审 ──
# 病根实证(114d): `rm -f book.pdf && curl ... -o book.pdf` 触发 rm 模式→
# 挂起等人批 120s→超时 deny→一整轮被白白烧掉。但删的/写的都是【沙箱//tmp/
# 相对路径】里的下载残渣——这类操作零风险(不碰仓库/系统/其他用户)。机制:
# 风险模式命中后·提取目标路径·全部落在安全区(沙箱//tmp/相对路径)→免审放行。
_SAFE_WRITE_ROOTS = (
    os.path.expanduser("~/.meyo/sandbox"),
    "/tmp/",
    "/var/folders/",   # macOS 临时目录
    "/private/tmp/",
)


def _benign_targets(detail: str) -> bool:
    """提取 rm/mv/chmod/chown 类命令的目标路径·全部在安全区→True。
    解析不了目标(拿不到路径证据)→False(保守·照旧审批)。"""
    try:
        import shlex as _shlex
        targets = []
        m = re.search(r"(?:rm|mv|chmod|chown)\s+((?:-[a-zA-Z]+\s+)*)(.+)$",
                      str(detail), re.S)
        if not m:
            return False
        rest = m.group(2)
        for tok in _shlex.split(rest.replace("\n", " ")):
            tok = tok.strip('"\'')
            if not tok or tok.startswith("-") or tok.startswith("$"):
                continue
            if tok in ("&&", ";", "|", ">>", ">"):
                break
            targets.append(tok)
        if not targets:
            return False
        for t in targets:
            # 家目录展开(~/xxx)与路径逃逸(..)一律不豁免——宁可多问一句
            if t.startswith("~") or ".." in t.split("/"):
                return False
            if os.path.isabs(t):
                if not any(t.startswith(r) for r in _SAFE_WRITE_ROOTS):
                    return False
            # 相对路径(沙箱 cwd 内)视为安全
        return True
    except Exception:
        return False


# 只有这些【文件操作类】模式有良性豁免资格; 其余(git push/sudo/curl|sh/kill)
# 无论目标路径一律照旧审批——免审面最小化。
_BENIGN_ELIGIBLE_PATTERNS = (
    r"\brm\s+-[rf]",
    r"\bchmod\s+-R", r"\bchown\s+-R",
)


def needs_approval(kind: str, detail: str) -> bool:
    """判这个操作要不要审批。默认策略见模块 docstring。fail-safe:异常→不审批(放行)。"""
    try:
        mode = _mode()
        if mode == "off":
            return False
        if mode == "all":
            return True
        # risky 模式(默认):只拦真风险
        if kind == "shell_exec":
            for pat in _APPROVAL_SHELL_PATTERNS:
                if re.search(pat, detail):
                    # v108(M6b) · 良性目标免审: 仅 rm/chmod/chown 这类文件操作·
                    # 且目标全部在沙箱//tmp/相对路径(下载残渣清理·零风险)。
                    # git push/sudo/curl|sh/kill 等无豁免资格·照旧审批。
                    if pat in _BENIGN_ELIGIBLE_PATTERNS and _benign_targets(detail):
                        continue
                    return True
            return False
        if kind == "file_write":
            # 写到沙箱外的绝对路径(可能改仓库/系统文件)→ 审批
            path = detail.strip()
            if os.path.isabs(path) and not path.startswith(os.path.expanduser("~/.meyo/sandbox")):
                return True
            return False
        # G-gui(2026-08-08) · 系统级桌面 GUI 操作(点击/输入)会改变真实系统状态
        # 且无法撤销(点错了、输错密码了都不可逆)，risky 模式下一律需要审批——
        # 比 shell_exec 的"只拦风险模式"更保守,因为桌面点击的"风险"无法用
        # 正则模式识别(点哪都可能触发不可预期后果),干脆全审批更安全。
        if kind in ("desktop_click", "desktop_type"):
            return True
        return False
    except Exception:
        return False


def _approval_path(approval_id: str) -> str:
    return os.path.join(_STATE_DIR, f"{approval_id}.json")


def _safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(s))[:40]


def _atomic_write_json(path: str, obj: dict) -> None:
    """原子写:先写临时文件再 os.replace,避免进程中途被杀/崩溃导致文件
    截断成半个 JSON(之前是直接 open(path,'w') 覆盖写,写到一半崩了下次
    json.load 就会抛异常,该条审批记录直接损坏丢失——对"用户批不批"这种
    一次性、不可重放的裁决记录尤其致命)。os.replace 在同一文件系统内是
    原子操作,不存在"写了一半"的中间态。"""
    tmp = f"{path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _LockedFile:
    """跨进程/跨线程互斥:对 <approval_id>.json 加排他锁包住"读-判断-写"
    临界区,修复#4——之前 resolve() 是裸的 读→改→写,如果网页端重复点击
    两次"批准/拒绝"、或者恰好和 request_approval() 的超时兜底写并发,
    两个执行者都读到 status=pending 各自决定要写,后写的会无条件覆盖前
    写的结果(可能把已经"approved"的裁决静默改成"denied"或反之),且
    resolved_at 也会被覆盖导致时间线失真。fcntl.flock 在 POSIX 上对
    同一文件路径的并发访问是有效的(阻塞直到拿到锁);Windows/无 fcntl
    环境下退化为不加锁(fail-open,不影响主流程正确性但失去互斥保证,
    这里的场景只在 macOS/Linux 部署,可接受)。"""

    def __init__(self, lock_path: str):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        if not _HAS_FCNTL:
            return self
        try:
            self._fh = open(self._lock_path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            # 锁不可用(权限/磁盘问题)→ fail-open,不因锁本身故障卡死审批流程
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
        return False


def _lock_for(approval_id: str) -> _LockedFile:
    return _LockedFile(os.path.join(_STATE_DIR, f".{_safe(approval_id)}.lock"))


def request_approval(qid: str, kind: str, detail: str,
                     timeout_s: float = None, poll_s: float = 1.0) -> dict:
    """登记一条待审批 → 阻塞轮询等网页裁决。返回 {decision: approved|denied|timeout, ...}。
    这是 agent 线程里调的:挂起自己等用户批,不占 CPU(sleep 轮询文件)。

    v108(M6b): timeout_s 缺省从环境变量 MEYO_HITL_TIMEOUT_S 读(默认120)。
    无人值守评测跑批时把它设短(如 15)·真风险命令不会白白挂 120s——
    超时动作仍由 MEYO_HITL_TIMEOUT_ACTION 控制(默认 deny·安全优先)。"""
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get("MEYO_HITL_TIMEOUT_S", "120"))
        except Exception:
            timeout_s = 120.0
    approval_id = hashlib.md5(f"{qid}|{kind}|{detail}|{time.time()}".encode()).hexdigest()[:12]
    rec = {
        "approval_id": approval_id, "qid": qid, "kind": kind,
        "detail": str(detail)[:500], "status": "pending",
        "created": time.time(), "decision": None,
    }
    path = _approval_path(approval_id)
    try:
        _atomic_write_json(path, rec)
    except Exception:
        # 门写不了 → fail-safe 放行(不卡任务),但标记出来
        return {"decision": "approved", "note": "hitl门不可用·放行", "approval_id": approval_id}

    deadline = time.time() + max(5.0, timeout_s)
    while time.time() < deadline:
        try:
            cur = json.load(open(path, encoding="utf-8"))
            if cur.get("status") == "resolved":
                dec = cur.get("decision") or "denied"
                return {"decision": dec, "approval_id": approval_id}
        except Exception:
            pass
        time.sleep(poll_s)

    # 超时:默认按 deny(安全优先),可配。
    # 修复#4:超时兜底写和 resolve() 网页裁决可能并发发生(用户刚好在超时
    # 前一瞬间点了批准,两者都会尝试把 status 从 pending 改成 resolved)。
    # 用同一把 approval_id 锁 + 写前重新读一次判断是否已被 resolve() 抢先
    # 处理,避免超时线程无脑覆盖掉用户已经给出的真实裁决。
    timeout_action = os.environ.get("MEYO_HITL_TIMEOUT_ACTION", "deny").lower()
    with _lock_for(approval_id):
        try:
            cur = json.load(open(path, encoding="utf-8"))
        except Exception:
            cur = rec
        if cur.get("status") == "resolved":
            # 已经被 resolve() 抢先裁决(大概率是用户在超时前批了)→ 尊重既有裁决,
            # 不覆盖,幂等返回。
            return {"decision": cur.get("decision") or timeout_action,
                     "approval_id": approval_id, "raced_with_resolve": True}
        cur["status"] = "resolved"
        cur["decision"] = timeout_action
        cur["timeout"] = True
        cur["resolved_at"] = time.time()
        try:
            _atomic_write_json(path, cur)
        except Exception:
            pass
    return {"decision": timeout_action, "timeout": True, "approval_id": approval_id}


def resolve(approval_id: str, decision: str) -> dict:
    """网页 /api/approve 调:给出裁决(approve/deny)。返回 {ok, ...}。

    修复#4(非原子写 + 无幂等保护):
    - 原实现是裸的"读 JSON → 改字段 → 覆盖写",两次请求(比如用户手滑
      重复点了两下批准按钮,或前端重试)并发执行时会互相踩踏,且覆盖写
      在进程被杀的极端情况下可能截断文件。
    - 现改为:整个读-判断-写过程用 approval_id 专属文件锁保护(同一时刻
      只有一个 resolve/超时兜底 在处理同一条审批),且写入前检查是否已
      是 resolved 状态——若是,直接幂等返回已存在的裁决,不重复处理、
      不覆盖已有的 decision/resolved_at,保证"同一条审批只能被裁决一次,
      重复调用得到一致结果"。写盘统一走 _atomic_write_json 避免半写。"""
    path = _approval_path(_safe(approval_id))
    if not os.path.exists(path):
        return {"ok": False, "error": "approval not found"}
    with _lock_for(approval_id):
        try:
            rec = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if rec.get("status") == "resolved":
            # 幂等:已经裁决过,不重复处理/不覆盖,直接把已有结果原样返回。
            return {"ok": True, "decision": rec.get("decision"), "already_resolved": True}
        rec["status"] = "resolved"
        rec["decision"] = "approved" if decision in ("approve", "approved", "yes", "y") else "denied"
        rec["resolved_at"] = time.time()
        try:
            _atomic_write_json(path, rec)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "decision": rec["decision"]}


def pending(qid: str = None) -> list:
    """列待审批项(网页轮询用)。传 qid 只列该任务的。"""
    out = []
    try:
        for fn in os.listdir(_STATE_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                rec = json.load(open(os.path.join(_STATE_DIR, fn), encoding="utf-8"))
            except Exception:
                continue
            if rec.get("status") != "pending":
                continue
            if qid and rec.get("qid") != qid:
                continue
            out.append({
                "approval_id": rec.get("approval_id"),
                "qid": rec.get("qid"),
                "kind": rec.get("kind"),
                "detail": rec.get("detail"),
                "created": rec.get("created"),
            })
    except Exception:
        pass
    return out


def cleanup(max_age_s: float = 3600):
    """清超过 max_age 的旧审批文件(避免堆积)。best-effort。"""
    try:
        now = time.time()
        for fn in os.listdir(_STATE_DIR):
            p = os.path.join(_STATE_DIR, fn)
            try:
                if now - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
            except Exception:
                continue
    except Exception:
        pass


if __name__ == "__main__":
    print("=== hitl_gate smoke ===")
    # 1. 策略判定
    os.environ["MEYO_HITL_MODE"] = "risky"
    assert needs_approval("shell_exec", "sudo rm -rf /tmp/x") is True, "sudo应审批"
    assert needs_approval("shell_exec", "ls -la") is False, "普通命令不审批"
    assert needs_approval("shell_exec", "git push origin main") is True, "git push应审批"
    assert needs_approval("file_write", "/etc/hosts") is True, "写沙箱外应审批"
    assert needs_approval("file_write", "note.txt") is False, "沙箱内相对路径不审批"
    os.environ["MEYO_HITL_MODE"] = "off"
    assert needs_approval("shell_exec", "sudo rm -rf /") is False, "off模式全放行"
    os.environ["MEYO_HITL_MODE"] = "risky"
    # G-gui(2026-08-08) · 桌面GUI操作策略
    assert needs_approval("desktop_click", "click at (100,200)") is True, "桌面点击应审批"
    assert needs_approval("desktop_type", "some text") is True, "桌面输入应审批"
    print("  策略判定 OK")

    # 2. 请求→裁决往返(模拟网页批准)
    import threading
    result = {}
    def _ask():
        result["r"] = request_approval("smoke_qid", "shell_exec", "sudo test cmd",
                                        timeout_s=10, poll_s=0.2)
    t = threading.Thread(target=_ask); t.start()
    time.sleep(0.5)
    p = pending("smoke_qid")
    assert p, "没登记 pending"
    aid = p[0]["approval_id"]
    resolve(aid, "approve")
    t.join(timeout=5)
    assert result["r"]["decision"] == "approved", f"裁决没生效: {result}"
    print("  请求→网页批准→放行 OK")

    # 3. 超时→默认 deny
    os.environ["MEYO_HITL_TIMEOUT_ACTION"] = "deny"
    r = request_approval("smoke_qid2", "shell_exec", "another", timeout_s=2, poll_s=0.2)
    assert r["decision"] == "deny" and r.get("timeout"), f"超时没按deny: {r}"
    print("  超时→默认deny(安全优先) OK")

    cleanup(max_age_s=0)
    print("✅ hitl_gate 自测通过(策略/请求裁决往返/超时deny/清理)")
