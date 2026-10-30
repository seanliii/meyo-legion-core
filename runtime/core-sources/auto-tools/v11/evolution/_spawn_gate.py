#!/usr/bin/env python3
"""_spawn_gate · v13.88.9f · 全局 claude 子进程闸门 + 统一 spawn 咽喉

历史 (2026-07-28 电脑卡死·hook 雪崩):
    20+ Stop/UserPromptSubmit hook·其中 6+ 各 spawn claude -p·无上限 → 雪崩。
    当时补的 can_spawn() 只给 hook 用 (hook 每轮现跑脚本·能读到新代码)。

本次 (2026-07-31 load 反复爆 100+):
    根因不在 hook·在 52 个 evolution 组件里有 ~17 个会 subprocess spawn claude
    (llm_judge 是主咽喉·被 35 组件调·另有 16 组件绕过咽喉直接 subprocess.run
     (["claude", ...]))。这些在 cron / 被调 时 spawn·完全不受 can_spawn 管。

统一咽喉 spawn_claude_cli():
    所有「真 spawn 一个 claude CLI 一次性进程」的地方·都改走这个函数。它:
      1. 先过 can_spawn() (全局并发闸)·满了 → 有界排队等·等不到 → 降级不 spawn
      2. fail-safe 铁律:闸门/pgrep 任何异常都当「不能 spawn」·绝不 fail-open
         (旧 can_spawn 里 pgrep 失败返 0 是 fail-open·咽喉这层用严格版覆盖)
      3. 全局硬上限 (同时最多 MAX_CLAUDE_PRINT 个)·超了排队·排不进跳过
    返回一个「像 subprocess.CompletedProcess 的对象」·被拦时 returncode=非0 +
    stderr 说明·调用方按普通失败/降级处理即可。

    from _spawn_gate import spawn_claude_cli
    r = spawn_claude_cli(["claude","--print","--model","haiku",prompt],
                         timeout=60)
    if r.returncode != 0: ...降级...
    text = r.stdout
"""
import subprocess
import os
import time
import threading
import fcntl
import glob
import json
import urllib.request
import urllib.error

# 全局同时最多 N 个 claude --print 一次性进程。超了排队/跳过。
# 4 session 各留余量·8 是「够用但不雪崩」经验值 (hook 用)。
MAX_CLAUDE_PRINT = int(os.environ.get("MEYO_HOOK_MAX_CLAUDE", "8"))

# 咽喉硬上限:同时最多 N 个 claude --print (任务要求 4·比 hook 的 8 更紧)。
# 咽喉走的是 cron/被调的重活·比 hook 更该收紧·防 load 爆。
MAX_SPAWN_CONCURRENT = int(os.environ.get("MEYO_SPAWN_MAX_CONCURRENT", "4"))

# 咽喉排队等待:满了先等·每 QUEUE_POLL 秒重探·等到 QUEUE_WAIT 秒还满 → 放弃 (降级)。
QUEUE_WAIT = float(os.environ.get("MEYO_SPAWN_QUEUE_WAIT", "20"))
QUEUE_POLL = float(os.environ.get("MEYO_SPAWN_QUEUE_POLL", "1.5"))

# 急停开关:此文件存在 → 所有 spawn 立即跳过。touch 冻结·rm 恢复。
PAUSE_FLAG = os.path.expanduser("~/.claude/HOOKS_PAUSED")

# 进程内并发计数 (同一 python 进程里多线程调咽喉时·避免大家同时抢过 pgrep 窗口)。
_INPROC_SLOTS = threading.BoundedSemaphore(MAX_SPAWN_CONCURRENT)
_INPROC_LOCK = threading.Lock()
_inproc_active = 0

# ── 跨进程原子闸门 (spawn 雪崩第 8 次根因·TOCTOU 竞态修) ──────────────────────
# 病根:curator_agent / research_cron / 各 hook 是「多个独立 python 进程」·各自
#   持一个进程内信号量 (各限 4)·进程内信号量管不到跨进程。全局保险 _count_or_raise
#   只在 spawn 前查一次 → 多个独立进程同时通过 count<4 → 同时 spawn → 全局爆到 9~22。
# 修:用 fcntl.flock 对一个锁文件做「数进程 → 判 <MAX → 持锁 spawn 拿到 pid → 释放锁」
#   的原子操作·让所有独立 python 进程串行通过这道闸。新进程在释放锁前已能被下一个
#   竞争者的 pgrep 数到·消除 TOCTOU 窗口。全局并发绝不超 MAX_SPAWN_CONCURRENT。
SPAWN_LOCK_PATH = os.environ.get("MEYO_SPAWN_LOCK", "/tmp/meyo_spawn_gate.lock")
# 拿跨进程锁最多等这么久·拿不到就跳过 spawn (别死等阻塞调用方)。
SPAWN_LOCK_WAIT = float(os.environ.get("MEYO_SPAWN_LOCK_WAIT", "3.0"))
SPAWN_LOCK_POLL = float(os.environ.get("MEYO_SPAWN_LOCK_POLL", "0.05"))
# 额外急停开关 (和记忆里的止血哨兵对齐)·任一存在 → 一律不 spawn。
_EXTRA_STOP_FLAGS = ("/tmp/MEYO_SPAWN_STOP", "/tmp/MEYO_HOOKS_DISABLED")
# 止血哨兵自过期 TTL (秒·默认 2h)。根治「止血后忘删·一整天禁掉所有 spawn」劣根性:
#   哨兵超过 TTL 且当前 load 已健康 → 自动删除并忽略·不再靠人记得删。
#   (若 load 仍高·保留哨兵继续止血·由 _load_too_high 接力拦。)
_STOP_FLAG_TTL = float(os.environ.get("MEYO_STOP_FLAG_TTL", "7200"))


# ── 整机 load 总闸 (2026-08-09·雪崩根治·load 143 事件) ──────────────────────
# 病根:can_spawn / 咽喉只数「自己那条线的 claude 进程数」(<MAX_SPAWN_CONCURRENT)。
#   多源 (auto 蜂群 + GAIA 重跑 + 多 session + cron) 各自都没满自己的槽·但叠加把
#   整机 load 压到 143·闸门对整机压力完全无感 → 继续放行 → 火上浇油。
# 修:在所有 spawn 判定的公共前置 _stop_requested() 里加整机 load 检查。load 超
#   (核数 × MEYO_LOAD_MULT·默认 2) → 当「急停」·一切 spawn 降级。一处生效·
#   can_spawn / can_spawn_strict / 咽喉三处临界区全部自动覆盖。
# fail-safe:读 load / cpu_count 任何异常 → 返 False (不因 load 闸自身故障拦死正常
#   spawn·load 闸是「额外收紧」·底线仍是原有并发闸)。
# v13.90(2026-08-09 用户「你的目标是降低我 CPU 卡顿」)收紧:2.0→1.25(8核→阈值10)。
#   旧阈值16对8核太松(load 到16=每核排队2任务)·用户 load 11 就卡·却要 16 才拦。
_LOAD_MULT = float(os.environ.get("MEYO_LOAD_MULT", "1.25"))
# 关掉 load 闸的开关 (MEYO_LOAD_GATE_OFF=1)·仅调试/特殊场景用·默认开。
_LOAD_GATE_OFF = os.environ.get("MEYO_LOAD_GATE_OFF") == "1"


def _load_too_high() -> bool:
    """整机 1 分钟 load 是否超过 核数 × _LOAD_MULT。
    True → 整机过载·应拒绝新 spawn。读取失败 → False (fail-safe·不误拦)。"""
    if _LOAD_GATE_OFF:
        return False
    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or 8
        return load1 > ncpu * _LOAD_MULT
    except Exception:
        return False


def _stop_requested() -> bool:
    """急停:PAUSE_FLAG / 止血哨兵存在·或整机 load 过载 → True (不能 spawn)。
    检查本身出异常也当「急停」(fail-safe)。"""
    try:
        if os.path.exists(PAUSE_FLAG):
            return True
        overloaded = _load_too_high()
        for f in _EXTRA_STOP_FLAGS:
            if not os.path.exists(f):
                continue
            # 止血哨兵 TTL 自过期:超时且 load 已健康 → 删除并忽略 (根治忘删)。
            try:
                age = time.time() - os.path.getmtime(f)
                if age > _STOP_FLAG_TTL and not overloaded:
                    os.unlink(f)
                    continue  # 已过期作废·当此哨兵不存在
            except Exception:
                pass  # 取 mtime/删除失败 → 保守当哨兵仍有效
            return True
        if overloaded:
            return True
        return False
    except Exception:
        return True


class _CrossProcLock:
    """跨进程文件锁 (fcntl.flock 独占)·带获取超时。
    用法:
        with _CrossProcLock() as lk:
            if lk is None: ...没拿到锁·fail-safe 跳过...
            else: ...持锁临界区 (数进程+判 MAX+spawn)...
    fail-safe:开文件/加锁任何异常 → 进入块时 lk is None (当「拿不到锁·不 spawn」)。"""
    def __init__(self, path=None, wait=None, poll=None):
        self.path = path or SPAWN_LOCK_PATH
        self.wait = SPAWN_LOCK_WAIT if wait is None else wait
        self.poll = SPAWN_LOCK_POLL if poll is None else poll
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except Exception:
            self._fd = None
            return None  # fail-safe:锁文件都开不了 → 当拿不到锁
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self  # 拿到独占锁
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    # 超时没拿到 → 关 fd·返 None (fail-safe·别死等)
                    try:
                        os.close(self._fd)
                    except Exception:
                        pass
                    self._fd = None
                    return None
                time.sleep(self.poll)
            except Exception:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
                return None

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
        return False  # 不吞异常


def count_claude_print() -> int:
    """当前有多少个 claude --print / claude -p 一次性子进程。
    fail-open 版:pgrep 失败返 0 (给旧 hook 逻辑用·保持历史行为不破坏)。
    咽喉需要 fail-safe·用下面 _count_or_raise。
    """
    try:
        return _count_or_raise()
    except Exception:
        return 0


def _count_or_raise() -> int:
    """数 claude 一次性进程·pgrep 失败抛异常 (给 fail-safe 咽喉判断)。
    同时认 `claude --print` 和 `claude -p` (沙箱 skill 形式·和 reaper 对齐)。

    MEYO_SPAWN_COUNT_MARKER (可选·仅测试用):设了则改数命令行含此 marker 的进程·
      默认 None → 完全保持历史「claude --print/-p」计数行为不变。用于多进程压测
      隔离到自己的假命令·不受系统真 claude 进程波动干扰。"""
    marker = os.environ.get("MEYO_SPAWN_COUNT_MARKER")
    pat = marker if marker else "claude"
    r = subprocess.run(["pgrep", "-fl", pat],
                       capture_output=True, text=True, timeout=3)
    # returncode 1 = 没匹配到 (正常·0 个)·>1 = pgrep 真出错
    if r.returncode not in (0, 1):
        raise RuntimeError(f"pgrep rc={r.returncode}")
    n = 0
    for line in r.stdout.splitlines():
        low = " " + line.lower() + " "
        if marker:
            if marker.lower() in low:
                n += 1
        elif "claude" in low and ("--print" in low or " -p " in low):
            n += 1
    return n


def can_spawn(max_n: int = None) -> bool:
    """现在能不能再 spawn 一个 claude (hook 用·保持历史 fail-open 行为)。
    急停/超阈值/整机 load 过载返 False。pgrep 失败时 count_claude_print 返 0 → 放行。
    """
    if _stop_requested():  # PAUSE_FLAG / 止血哨兵 / 整机 load 过载
        return False
    limit = max_n if max_n is not None else MAX_CLAUDE_PRINT
    return count_claude_print() < limit


def can_spawn_strict(max_n: int = None) -> bool:
    """fail-safe 版:急停/超阈值/整机 load 过载/闸门任何异常 → False。咽喉用。"""
    try:
        if _stop_requested():  # PAUSE_FLAG / 止血哨兵 / 整机 load 过载
            return False
        limit = max_n if max_n is not None else MAX_SPAWN_CONCURRENT
        return _count_or_raise() < limit
    except Exception:
        # fail-safe 铁律:闸门自身故障 → 当「不能 spawn」·绝不 fail-open
        return False


class _Blocked:
    """被闸门拦下时返回·长得像 subprocess.CompletedProcess·调用方按失败处理。"""
    def __init__(self, reason: str, args=None):
        self.returncode = 199  # 非 0·专用码「被 spawn 闸门拦」
        self.stdout = ""
        self.stderr = f"[spawn_gate blocked] {reason}"
        self.args = args
        self.blocked = True
        self.block_reason = reason


# ── 网关翻译分支 (v13.88.9h·2026-08-05·闸门收口第二阶段) ────────────────────
# 背景 (8.5日报「三套并行LLM调用路径」诊断): claude code CLI(个人登录态)、
#   CCR代理、qa_llm_gateway(美团网关直连) 三条路并行·全部挤在同一批 claude
#   进程闸门上。qa_agent 一条链已迁移到 qa_llm_gateway 直连 HTTP·但
#   v11/evolution 全局仍是「真 spawn 一个 claude CLI 进程」，占用本地并发槽位、
#   依赖个人登录态。
# 本分支: MEYO_USE_GATEWAY=1 时·spawn_claude_cli 对 claude CLI 调用不再真
#   spawn 进程·而是解析出 model/system/prompt 直接打 HTTP 到美团网关·返回一
#   个「长得像 subprocess.CompletedProcess」的对象。调用方 28 个模块的调用代
#   码不用改一行。默认关闭 (环境变量未设置时) ·不改变任何现有调用方行为。
# fail-safe 相反方向:翻译/网关调用出任何异常 → 返回 None (由调用方退回原有
#   「真 spawn CLI」路径)·绝不因为网关故障而彻底拦死。
_GATEWAY_DEFAULT_MODEL = os.environ.get(
    "MEYO_QA_GATEWAY_MODEL", "sankuai,aws.claude-opus-4.7")
_GATEWAY_HAIKU_MODEL = os.environ.get(
    "MEYO_QA_HAIKU_MODEL", "sankuai,aws.claude-haiku-4.5")


def _gateway_enabled() -> bool:
    return os.environ.get("MEYO_USE_GATEWAY", "") == "1"


def _is_claude_cli_call(args) -> bool:
    """判断这次 spawn 是不是在调 claude CLI (而不是 echo/sleep/grep 等其他命令)。"""
    if not args:
        return False
    head = os.path.basename(str(args[0])).lower()
    return head == "claude"


def _gateway_config():
    """和 qa_llm_gateway._config() 同款读取逻辑 (独立维护·不跨目录 import·
    避免 v11/evolution 与 qa_system/agent 产生包依赖耦合)。"""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:3456")
    auth = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    for path in glob.glob(
            "/var/folders/*/*/T/claude-code-router/ccr-settings-*.json"):
        try:
            env = json.loads(open(path, encoding="utf-8").read()).get("env", {})
            base_url = env.get("ANTHROPIC_BASE_URL") or base_url
            auth = env.get("ANTHROPIC_AUTH_TOKEN") or auth
            if auth:
                break
        except Exception:
            continue
    return base_url, auth


def _parse_claude_cli_args(args):
    """把 claude CLI 命令行参数解析成 (model, system, prompt)。
    覆盖当前所有真实调用方用到的两种参数形态:
      [CLAUDE_CLI, "--print", "--model", X, "--append-system-prompt", S, PROMPT]
      [CLAUDE_CLI, "-p", PROMPT, "--model", X]
    未识别的 flag 一律跳过 (兼容未来新增 flag 不崩)。"""
    model = "haiku"
    system = ""
    prompt = ""
    rest = list(args[1:])
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--model" and i + 1 < len(rest):
            model = rest[i + 1]; i += 2
        elif a == "--append-system-prompt" and i + 1 < len(rest):
            system = rest[i + 1]; i += 2
        elif a == "-p" and i + 1 < len(rest):
            prompt = rest[i + 1]; i += 2
        elif a in ("--print", "--verbose", "--include-partial-messages",
                    "--no-input", "--output-format"):
            # --output-format 后面通常跟一个值 (如 stream-json)·一并跳过
            if a == "--output-format" and i + 1 < len(rest):
                i += 2
            else:
                i += 1
        else:
            # 非 flag 的裸参数 → 视为 prompt (兼容 --print ... PROMPT 结尾形态)
            if not prompt:
                prompt = a
            i += 1
    return model, system, prompt


def _translate_to_gateway_call(args, timeout, stdin_data=None):
    """真调美团网关一次·返回 subprocess.CompletedProcess 或 None (调用方回退)。
    绝不抛异常给调用方 (fail-safe:任何环节出错都返回 None)。"""
    try:
        model, system, prompt = _parse_claude_cli_args(args)
        if not prompt and stdin_data:
            prompt = stdin_data
        if not prompt:
            return None  # 解析不出 prompt·交回原有路径处理
        model_l = str(model).lower()
        model_full = _GATEWAY_HAIKU_MODEL if "haiku" in model_l else _GATEWAY_DEFAULT_MODEL
        base_url, auth = _gateway_config()
        if not auth:
            return None  # 没有认证信息·不硬凑·回退真 spawn
        payload = {
            "model": model_full,
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth}",
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        content = data.get("content", [])
        text_out = "".join(
            b.get("text", "") for b in content if isinstance(b, dict)).strip()
        return subprocess.CompletedProcess(args, 0, stdout=text_out, stderr="")
    except Exception:
        # fail-safe:网关翻译任何异常都不往上抛·让调用方走原有 CLI spawn 路径
        return None


def _acquire_slot() -> bool:
    """有界排队抢一个 spawn 槽。抢到返 True·超时返 False (降级)。
    双闸:进程内信号量 (同进程多线程) + 全局 pgrep 计数 (跨进程)。"""
    deadline = time.time() + QUEUE_WAIT
    got_sema = False
    try:
        while True:
            # 1) 进程内信号量 (非阻塞 try·避免死等)
            if not got_sema:
                got_sema = _INPROC_SLOTS.acquire(blocking=False)
            if got_sema:
                # 2) 全局 pgrep 严格闸 (fail-safe)
                if can_spawn_strict():
                    with _INPROC_LOCK:
                        global _inproc_active
                        _inproc_active += 1
                    return True
                # 全局满·先把进程内信号量还回去·等下轮 (别占着不放堵别的线程)
                _INPROC_SLOTS.release()
                got_sema = False
            if time.time() >= deadline:
                if got_sema:
                    _INPROC_SLOTS.release()
                return False
            time.sleep(QUEUE_POLL)
    except Exception:
        # fail-safe:抢槽逻辑异常 → 释放已占资源·当「抢不到」
        if got_sema:
            try:
                _INPROC_SLOTS.release()
            except Exception:
                pass
        return False


def _release_slot():
    try:
        with _INPROC_LOCK:
            global _inproc_active
            if _inproc_active > 0:
                _inproc_active -= 1
        _INPROC_SLOTS.release()
    except Exception:
        pass


def _atomic_spawn_popen(args, popen_kwargs, queue_wait: float):
    """跨进程原子 spawn:持文件锁做「pgrep 数全局 claude → 判 <MAX → 启 Popen →
    拿 pid → 释放锁」。返回 (proc, None) 成功·(None, reason) 被拦/降级。

    关键:Popen 在持锁状态下启动·新进程在释放锁前已能被下一个竞争者的 pgrep 数到·
    消除 TOCTOU 窗口。锁只持「启动」这一瞬·不持整个子进程运行期 (否则退化成串行 1 个)。
    queue_wait: 全局满时·在锁外有界重试等待多久 (拿锁+复检)·0 → 不等。
    fail-safe:急停/拿不到锁/pgrep 异常/Popen 异常 → (None, reason)·绝不 fail-open。"""
    deadline = time.time() + max(0.0, queue_wait)
    while True:
        if _stop_requested():
            return None, "急停 (PAUSE/STOP flag)"
        with _CrossProcLock() as lk:
            if lk is None:
                reason = "拿不到跨进程锁 (fail-safe 跳过)"
            else:
                # 持锁临界区:数全局 claude --print → 判 <MAX → 启 Popen
                try:
                    cur = _count_or_raise()
                except Exception:
                    return None, "pgrep 计数失败 (fail-safe 不 spawn)"
                if cur >= MAX_SPAWN_CONCURRENT:
                    reason = f"全局并发 {cur} 已达上限 {MAX_SPAWN_CONCURRENT}"
                else:
                    try:
                        proc = subprocess.Popen(args, **popen_kwargs)
                    except Exception as e:
                        return None, f"Popen 失败: {e}"
                    # 持锁期间「探测」新进程已可被 pgrep 数到 (消除 TOCTOU)。
                    # 等 count 超过 spawn 前的 cur (确认我们这个新进程已可见给下一个
                    # 竞争者)·数不到也不阻断 (启动本身已成功)·仅尽力等一小会。
                    _wait_visible_to_pgrep(baseline=cur)
                    return proc, None
        # 没拿到锁 / 全局满 → 看要不要等
        if time.time() >= deadline:
            return None, reason
        time.sleep(QUEUE_POLL if QUEUE_POLL < queue_wait else SPAWN_LOCK_POLL)


def _wait_visible_to_pgrep(baseline: int = 0, max_wait: float = 0.5):
    """尽力等新 spawn 的进程出现在 pgrep 计数里 (确保释放锁前它已被数到)。
    baseline: spawn 前数到的计数·等到 count > baseline (我们这个新进程已可见)。
    数不到也不报错·最多等 max_wait 秒。fail-safe:异常直接返回。"""
    deadline = time.time() + max_wait
    try:
        while time.time() < deadline:
            n = _count_or_raise()
            # count 超过 spawn 前基线 → 新进程已可见给下一个竞争者·TOCTOU 关窗
            if n > baseline:
                return
            time.sleep(0.02)
    except Exception:
        return


def spawn_claude_cli(args, timeout=60, wait_in_queue: bool = True,
                     **run_kwargs):
    """统一咽喉:spawn 一个 claude CLI 一次性进程·过闸门 + 全局硬上限。

    args: subprocess 参数列表·如 ["claude","--print","--model","haiku",prompt]
    timeout: subprocess.run timeout (秒)
    wait_in_queue: True → 满了有界排队 (QUEUE_WAIT 秒)·False → 满即降级不等
    run_kwargs: 透传给 subprocess.run (如 cwd·input)·capture_output/text 默认开

    返回: subprocess.CompletedProcess (真跑了)·或 _Blocked (被拦·returncode=199)。
    fail-safe:任何闸门异常都不 spawn·返回 _Blocked。

    并发闸 (spawn 雪崩第 8 次修):真正的全局并发上限由跨进程文件锁保证——
      持 flock 原子做「pgrep 数全局 claude → 判 <MAX → 启 Popen」·独立 python 进程
      也串行通过·全局绝不超 MAX_SPAWN_CONCURRENT。锁只持「启动」瞬·不持整段运行。
    """
    # 急停优先 (含止血哨兵)
    if _stop_requested():
        return _Blocked("急停 (PAUSE/STOP flag)", args)

    # v13.88.9h · 网关翻译分支 (闸门收口第二阶段):MEYO_USE_GATEWAY=1 且这次
    # spawn 确实是调 claude CLI 时·不占并发槽位、不真起进程·直接 HTTP 打
    # 美团网关。翻译失败 (解析不出 prompt / 无认证 / 网关异常) → 静默回退
    # 到下面原有「真 spawn CLI」路径·调用方完全无感知、行为不变。
    if _gateway_enabled() and _is_claude_cli_call(args):
        stdin_data = run_kwargs.get("input")
        gw = _translate_to_gateway_call(args, timeout, stdin_data=stdin_data)
        if gw is not None:
            return gw
        # gw is None → 网关翻译放弃·继续走下面原有真 spawn 逻辑 (fail-safe)

    # 进程内信号量:同一 python 进程内多线程时先收一道 (次要层·真闸是跨进程锁)。
    got_sema = False
    if wait_in_queue:
        # 有界等一个进程内槽位 (跨进程等待由 _atomic_spawn_popen 内部 queue_wait 处理)
        deadline = time.time() + QUEUE_WAIT
        while time.time() < deadline:
            if _INPROC_SLOTS.acquire(blocking=False):
                got_sema = True
                break
            time.sleep(QUEUE_POLL)
    else:
        got_sema = _INPROC_SLOTS.acquire(blocking=False)
    if not got_sema:
        return _Blocked(
            f"进程内并发已达上限 {MAX_SPAWN_CONCURRENT}·本次降级不 spawn", args)

    with _INPROC_LOCK:
        global _inproc_active
        _inproc_active += 1

    proc = None
    try:
        # 组装 Popen kwargs:把 subprocess.run 的 capture_output/text/input 语义映射过来
        pk = dict(run_kwargs)
        stdin_data = pk.pop("input", None)
        capture = pk.pop("capture_output", True)
        text = pk.pop("text", True)
        if capture:
            pk.setdefault("stdout", subprocess.PIPE)
            pk.setdefault("stderr", subprocess.PIPE)
        if stdin_data is not None:
            pk.setdefault("stdin", subprocess.PIPE)
        pk.setdefault("text", text)

        queue_wait = QUEUE_WAIT if wait_in_queue else 0.0
        proc, reason = _atomic_spawn_popen(args, pk, queue_wait)
        if proc is None:
            return _Blocked(
                f"claude 并发闸拦下: {reason} (上限 {MAX_SPAWN_CONCURRENT})", args)

        # 锁已释放·在锁外等子进程跑完 (不占并发闸·别退化成串行)
        try:
            out, err = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(args, timeout, output=out,
                                            stderr=err)
        return subprocess.CompletedProcess(args, proc.returncode,
                                           stdout=out, stderr=err)
    finally:
        _release_slot()


def spawn_popen(args, **popen_kwargs):
    """给 daemon 那种长驻 Popen 用:过一次闸门·满了返 None (不启)。
    daemon 是长驻复用进程·只在「启新的」时过闸·不排队 (启不了就降级到别的路)。
    全局并发由跨进程文件锁原子保证 (和 spawn_claude_cli 同一道闸)。
    fail-safe:急停/拿不到锁/pgrep 异常/Popen 异常 → 返 None (绝不 fail-open)。"""
    if _stop_requested():
        return None
    # daemon 不排队:queue_wait=0·满即返 None
    proc, _reason = _atomic_spawn_popen(args, popen_kwargs, 0.0)
    return proc


if __name__ == "__main__":
    n = count_claude_print()
    print(f"claude 一次性进程数: {n} · hook上限 {MAX_CLAUDE_PRINT} · "
          f"咽喉上限 {MAX_SPAWN_CONCURRENT} · "
          f"{'可 spawn' if n < MAX_SPAWN_CONCURRENT else '咽喉已满·会排队/降级'}")
