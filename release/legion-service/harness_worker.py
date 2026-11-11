#!/usr/bin/env python3
"""单家 harness 隔离执行 worker。

legion 服务的每次尝试都在独立进程里跑这份 worker:
- 子进程每次重新 import run_comparison,因此 MEYO_UNIFIED_MODEL/APPID 覆盖会真生效
- 父进程可按轮次硬墙钟终止整个进程组,不会留下 codex/cc 孤儿进程
- stdout 只输出一行 JSON;harness 自己的 stdout/stderr 由 result 内部收集,不污染协议

调用:
  MEYO_UNIFIED_MODEL=gpt-5.4 MEYO_UNIFIED_APPID=... \
    python3.12 harness_worker.py '<base64-json:{key,task,qhash}>'
  大正文: python3.12 harness_worker.py -，从独立 stdin 读取同一 base64 JSON 到 EOF。
    (实际由 legion_core 用 sys.executable 拉起,保证与服务同一 3.12 解释器)
"""
import base64
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

import artifact_delivery as _artifact_delivery

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
# 与 legion_core 完全对称的 orchestrator 定位(双路径对称律):
#   1) MEYO_ORCH_DIR 显式注入(测试桩/生产外置)
#   2) 精简发布布局 ../meyo-orchestrator
#   3) 完整仓库 ../../harness_baselines/meyo-orchestrator
_CANDIDATES = [
    os.environ.get("MEYO_ORCH_DIR"),
    os.path.abspath(os.path.join(HERE, "..", "meyo-orchestrator")),
    os.path.join(REPO, "harness_baselines", "meyo-orchestrator"),
]
ORCH = next((p for p in _CANDIDATES if p and os.path.isfile(os.path.join(p, "orchestrator.py"))),
            _CANDIDATES[-1])
if ORCH not in sys.path:
    sys.path.insert(0, ORCH)


@contextlib.contextmanager
def _cooperative_native_stop():
    """Let the adapter drain its owned CLI before the parent's kill deadline."""
    try:
        import harness_registry as registry
    except ImportError:
        yield  # Legacy external workers retain the normal SIGTERM behavior.
        return
    cancel_state = getattr(registry, "_INPROC_CANCEL_CHECK", None)
    set_check = getattr(registry, "set_inproc_cancel_check", None)
    if (cancel_state is None or not callable(set_check) or os.name == "nt"
            or threading.current_thread() is not threading.main_thread()):
        yield
        return
    stopped = threading.Event()
    previous_handler = signal.getsignal(signal.SIGTERM)
    previous_check = getattr(cancel_state, "check", None)
    set_check(
        lambda: stopped.is_set() or (callable(previous_check) and previous_check()))
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        yield
    finally:
        set_check(previous_check)
        signal.signal(signal.SIGTERM, previous_handler)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("缺 worker payload")
        encoded = sys.stdin.read() if sys.argv[1] == "-" else sys.argv[1]
        payload = json.loads(base64.b64decode(
            encoded.strip().encode("ascii"), altchars=b"-_", validate=True).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be an object")
        key, task = payload.get("key"), payload.get("task")
        qhash = payload.get("qhash") or "adhoc"
        if not isinstance(key, str) or not key:
            raise ValueError("worker payload key must be a non-empty string")
        if not isinstance(task, str) or not isinstance(qhash, str):
            raise ValueError("worker payload task and qhash must be strings")

        # 项①(军团持续化·workdir/state 恢复包·ROI#2)· 归一复用父层"统一持久工作目录"MEYO_WORKDIR:
        # legion_core 已给每家下放 MEYO_WORKDIR(~/.meyo/legion/outputs/<key>_<qhash>/·harness 产物
        # 写这里被 scan 自动收集)。这里【不再另建目录】,只解析出它(legion_workdir.under 优先返回
        # MEYO_WORKDIR)·让 state.json 和交付产物落在同一处。run 完 worker 把现场收进 result 冒泡回父层→
        # 失败时接力家能读到上一家真做了什么(不失忆重跑)。harness 写不写 state 自主·不写=零回归。
        _attempt_workdir = ""
        try:
            import legion_workdir as _wd
            _attempt_workdir = _wd.under(qhash, key)   # 优先 MEYO_WORKDIR·未设才自建(归一·不重复造)
        except Exception:
            _attempt_workdir = ""

        # 某些 harness 模块 import/执行时会 print;协议 stdout 必须只留最终 JSON。
        # 但父层靠"子进程有无输出"判卡死——若把中间输出全吞,正常长任务会被误判卡死。
        # 解法:用能感知写入的 StringIO 记录 harness 最后活动时间;心跳线程只在 harness
        #   【最近确实写过东西(在推进)】时才往真 stderr 发心跳。真卡死(长时间零写入)→
        #   心跳停→父层 idle 检测生效。这是真实进展信号,不是无脑跳(学 codex/deepseek 事件回流)。
        _real_err = sys.stderr

        # 事件回流前缀(和 harness_registry._LEGION_EV_PREFIX 约定一致):adapter 提炼的过程事件
        # 带此前缀写 sys.stderr → 被 redirect 进 _ActiveIO → 这里【透传】给真 stderr(不丢),
        # legion_core._pump 再解析成 cb → SSE 到前端。这是"让各家过程像 CC 一样被看到"的关键管道。
        _EV_PREFIX = "\x1e[legion-ev]"

        class _ActiveIO(io.StringIO):
            """记'最后写入时间'做活动信号 + 透传结构化过程事件;其余噪声丢弃(bounded·防 OOM)。
            worker 只需要 stdout 最后一行 JSON(见下)当结果,但过程事件必须透传给前端。

            隐患修复(2026-08-29·事件丢失):旧版用 `_EV_PREFIX in s` 对【单次 write】做子串判断。
            但 print/logging 常把一条事件拆成多次 write(如 `\x1e[legion-` + `ev]{...}` + `\n`),
            前缀被拆到两次调用中间就漏判→该过程事件静默降级成噪声被丢。改为跨 write 边界的行缓冲:
            拼进 self._buf,按 \n 切出完整行再判前缀透传,残行留到下次;无前缀行做 rsplit 截断防无界增长。"""
            def __init__(self):
                super().__init__()
                self.last = time.time()
                self._buf = ""   # 跨 write 边界拼接·防事件前缀被 I/O 缓冲拆断

            def write(self, s):
                self.last = time.time()
                if not s:
                    return 0
                self._buf += s
                if _EV_PREFIX in self._buf:
                    # 按行切:完整行判前缀→透传真 stderr(legion_core 按行解析);末尾残行留到下次拼接。
                    *lines, self._buf = self._buf.split("\n")
                    for ln in lines:
                        if _EV_PREFIX in ln:
                            try:
                                event = json.loads(ln.split(_EV_PREFIX, 1)[1])
                                if _artifact_delivery._is_private_artifact_event(event):
                                    continue
                                _real_err.write(ln + "\n")
                                _real_err.flush()
                            except Exception:
                                pass
                elif "\n" in self._buf:
                    # 无事件前缀的整行是纯噪声·不必再等前缀·只保留最后残段·防 _buf 无界增长(bounded)。
                    self._buf = self._buf.rsplit("\n", 1)[-1]
                if len(self._buf) > 4096:   # 无换行的超长噪声兜底截断·仍防 OOM
                    self._buf = self._buf[-4096:]
                return len(s)

        noise_out, noise_err = _ActiveIO(), _ActiveIO()
        _stop = threading.Event()

        def _heartbeat():
            # 军团6家一致反思(2026-08-28·codex/deepseek/hermes/cc/penguin/jcode):父层靠
            # "无输出就猜卡死杀进程"会误杀正在【纯推理/深度思考】的 agent(codex reasoning 阶段
            # 可能 >60s 不写任何东西·但它在正常干活)。学 codex(工具级budget不进程级kill)+
            # deepseek(工具级deadline):进程还活着本身=没失联,就该持续报心跳;真正的"卡死"
            # 由【工具级超时】(单个工具调用deadline·adapter层)或【进程真退出/崩溃】(父层poll到)
            # 判定,不由父层拿"静默"猜整个 agent 死了。所以这里只要 worker 主逻辑线程还在跑就发心跳,
            # 让父层 idle 只在【进程真失联(连心跳线程都停了=进程没了)】时才可能触发。
            while not _stop.wait(15.0):
                try:
                    recent = max(noise_out.last, noise_err.last)
                    busy = "active" if time.time() - recent < 60.0 else "thinking"
                    _real_err.write(f"[hb] {key} running t={int(time.time())} state={busy}\n")
                    _real_err.flush()
                except Exception:
                    pass

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

        # 父死自杀守护(军团 dogfood 揪出:父进程被 SIGKILL 时,worker 因 start_new_session 自成
        # 进程组变孤儿,继续跑 codex/CLI 子进程→泄漏)。worker 自成进程组后【父死不会级联杀子】,
        # macOS 无 PR_SET_PDEATHSIG,故 worker 自己盯梢:父进程一没(getppid 变 1=被 init 收养),
        # 就杀掉自己进程组内的全部 harness 子进程再退出。这是"父层杀 run_one 应级联清理"的兜底,
        # 复用 _stop 事件·任务正常结束时 _stop 置位守护自然退出·零副作用。
        def _orphan_guard():
            import signal as _sig
            orig_ppid = os.getppid()
            while not _stop.wait(3.0):
                try:
                    if os.getppid() != orig_ppid:   # 父换了(原父死→被 init(1) 收养)= 孤儿
                        # CLI 自立进程组，不在 worker 组里；必须先收子组再终止自己。
                        # 仅在父进程离开后回收，正常巡检绝不能杀仍在工作的 CLI。
                        try:
                            import legion_proc_registry as _preg
                            _preg.kill_owned_by(os.getpid())
                        except Exception:
                            pass
                        try:
                            os.killpg(os.getpgid(0), _sig.SIGTERM)  # 杀自己进程组(含所有 harness 子进程)
                        except Exception:
                            pass
                        time.sleep(2)
                        try:
                            os.killpg(os.getpgid(0), _sig.SIGKILL)
                        except Exception:
                            pass
                        os._exit(137)
                except Exception:
                    pass
        if os.name != "nt":
            threading.Thread(target=_orphan_guard, daemon=True).start()

        # 运行中产物观察：文件一落到本家持久 workdir 就立即推 SSE，不等 harness 结束。
        # 只陈述可观测事实；description/next_action 仅从 agent 自己写的 sidecar 读取，父层不猜。
        _artifact_stop = threading.Event()
        _artifact_seen = set()
        _artifact_state = {}
        _artifact_safety_seen = set()
        _artifact_started_at = time.time()

        def _emit_event(event):
            try:
                _real_err.write(_EV_PREFIX + json.dumps(event, ensure_ascii=False) + "\n")
                _real_err.flush()
            except Exception:
                pass

        def _artifact_meta(path: Path):
            sidecar = path.with_name(path.name + ".progress.json")
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
            except (OSError, ValueError, TypeError):
                data = {}
            return {
                "description": str(data.get("description") or ""),
                "next_action": str(data.get("next_action") or ""),
            }

        def _art_url(path: Path) -> str:
            """统一产物 url(重发根治·用户2026-09-01):用 artifact_delivery._url_for 按【文件真实
            归属根】定前缀——与 legion_core 最终 emit 同一逻辑同一前缀,同一文件两条事件
            (worker in_progress + final)url 一致,前端按 url 去重才可靠。import 失败退回
            旧行为(workdir 都在家目录 outputs 下·等价 /home-outputs/)。"""
            try:
                import artifact_delivery as _ad
                return _ad._url_for(str(path))
            except Exception:
                try:
                    rel = path.relative_to(Path(_attempt_workdir).parent)
                    return "/home-outputs/" + "/".join(rel.parts)
                except Exception:
                    return "/home-outputs/" + path.name

        def _emit_new_artifacts():
            if not _attempt_workdir or not Path(_attempt_workdir).is_dir():
                return
            safety_observations = []
            items = _artifact_delivery.scan_new_files(
                _artifact_started_at - 1, workdir=_attempt_workdir, limit=60,
                safety_observations=safety_observations)
            for note in safety_observations:
                signature = (note["name"], note["reason"])
                if signature not in _artifact_safety_seen:
                    _artifact_safety_seen.add(signature)
                    _emit_event({"t": "artifact_not_exported", "agent": key, **note,
                                 "x": f"文件仍保留，未公开下载：{note['name']}（{note['reason']}）"})
            for item in items:
                path = Path(item["path"])
                try:
                    path.stat()
                except OSError:
                    continue
                sidecar = path.with_name(path.name + ".progress.json")
                if not sidecar.exists() and str(path) not in _artifact_state:
                    time.sleep(0.15)
                meta = _artifact_meta(path)
                if _artifact_delivery._is_private_artifact({**item, **meta}):
                    continue  # Recheck after sidecar wait; metadata must not leak credentials either.
                provenance = item.get("provenance")
                if isinstance(provenance, dict) and provenance.get("sha256"):
                    # scanner 的 size/preview/provenance 也可能跨过一次改写。
                    # 复用原安全扫描核对，混合/过期记录留待下一轮；不补造 hash。
                    checked = _artifact_delivery._content_safety.check_file(str(path))
                    if (not checked["allowed"] or checked.get("sha256") != provenance["sha256"]
                            or checked.get("bytes") != item.get("bytes")):
                        continue
                # 同一次扫描的 bytes/provenance 必须一起传；扫描后文件可能已被改写，
                # 不能拿新 stat 配旧 hash，也不能用新 stat 吞掉下一次扫描的版本。
                event = {"t": "artifact_ready", "agent": key, "phase": "产出文件",
                         "path": str(path), "name": path.name,
                         "url": item.get("url") or _art_url(path),
                         "ext": path.suffix.lstrip(".").lower(), "bytes": item.get("bytes"),
                         "provenance": provenance,
                         "description": meta["description"], "next_action": meta["next_action"],
                         "in_progress": True}
                signature = json.dumps(event, sort_keys=True, ensure_ascii=False)
                if _artifact_state.get(str(path)) == signature:
                    continue
                _artifact_state[str(path)] = signature
                _emit_event(event)
                _artifact_seen.add(str(path))

        def _watch_artifacts():
            while not _artifact_stop.wait(0.5):
                try:
                    _emit_new_artifacts()
                except (OSError, ValueError):
                    continue

        artifact_thread = threading.Thread(target=_watch_artifacts, daemon=True)
        artifact_thread.start()
        try:
            with contextlib.redirect_stdout(noise_out), contextlib.redirect_stderr(noise_err):
                import orchestrator
                # per-call ctx(2026-08-28):从 env 组装本轮 model/appid/方向,让已 ctx 化的
                # adapter(penguin 起)拿到 per-call model,不再依赖模块级 _UNIFIED_MODEL 全局。
                # legion_core._run_harness_isolated 已把 model/appid 塞进子进程 env,这里 from_env()
                # 即可拿到。组装失败也不阻断(退回不传 ctx·老家路径),保证 worker 永不因此崩。
                ctx = None
                try:
                    from harness_registry import HarnessCtx
                    ctx = HarnessCtx.from_env()
                except Exception:
                    ctx = None
                with _cooperative_native_stop():
                    result = orchestrator.run_harness(key, task, qhash=qhash, ctx=ctx)
        finally:
            _stop.set()
            # 最后同步扫一次：极短任务可能在 0.5s 首轮 watcher tick 前已经完成，若先 stop
            # 就永远漏掉“运行中已产出”事件。先扫、再停，保证短/长路径对称。
            try:
                _emit_new_artifacts()
            except (OSError, ValueError):
                pass
            _artifact_stop.set()
            artifact_thread.join(timeout=1.0)
        if not isinstance(result, dict):
            result = {"status": "ok", "final": str(result)}
        if ctx is not None and getattr(ctx, "context_budget_report", None):
            result["context_budget"] = dict(ctx.context_budget_report)
        # 编排请求解析(全家统一出口·编排自主权):harness 若在 final 末尾写了
        # <<LEGION_ORCH>>{json}<<END>> 请求 fork/handoff/need_peer,在此解析成结构化
        # orchestration_request 并从 final 剥掉标记(不污染给用户的交付)。没写=零变化。
        if not result.get("orchestration_request"):
            try:
                import harness_registry as _HR
                _cleaned, _orch = _HR._parse_orch_request(str(result.get("final") or ""))
                if _orch:
                    result["final"] = _cleaned
                    result["orchestration_request"] = _orch
            except Exception:
                pass
        # 项①:把 workdir 现场收进 result 冒泡回父层。成功→带 workdir 路径(供 legion_core 收产物);
        # 失败→把 workdir 路径写进 result·让接力家至少知道"上一家的半成品在这·去读别重来"
        # (完整版:等并发 session 提交后·dispatch 接力再调 summarize_for_relay 注入更详的现场)。
        if _attempt_workdir:
            try:
                result["workdir"] = _attempt_workdir
                _st = str(result.get("status") or "")
                # adapter 成功态是 ok，父层稍后才归一为 done；两者都不应被误写“未交付”。
                # 排除进程级失败前缀 err:；普通 error/timeout 才附现场给接力家。
                if _st and _st not in ("ok", "done") and not _st.startswith("err:"):
                    # 失败/未交付:在 error 尾部附上 workdir 路径(现有 prev_err 提取会带上·接力家可读)
                    _e = str(result.get("error") or "")
                    if _attempt_workdir not in _e:
                        # 路径放【开头】而非尾部:dispatch 现有 relay_task 只取 prev_err[:500],
                        # 长错误若把路径放尾部会被截掉→接力家仍看不见现场。开头保证一定带到。
                        result["error"] = (f"工作目录已保留:{_attempt_workdir}·接力家可读它续做。{_e}").strip()
            except Exception:
                pass
        noise = (noise_out.getvalue() + "\n" + noise_err.getvalue()).strip()
        if noise:
            result.setdefault("worker_log", noise[-2000:])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"status": f"err:{type(e).__name__}:{str(e)[:200]}",
                          "final": "", "worker_trace": traceback.format_exc()[-1200:]},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
