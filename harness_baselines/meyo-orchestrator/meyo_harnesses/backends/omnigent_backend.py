# -*- coding: utf-8 -*-
"""omnigent 后端 adapter —— 军团的「编排放大器」(用户 2026-08-27:auto很薄·直接用omnigent)。

军团骨架本就 fork 自 omnigent(见 meyo_harnesses ADR-001)。本 adapter 更进一步:
把 omnigent 本体当军团一个成员接入——它一行 `omnigent run --harness <x>` 就能调
claude-sdk/codex/cursor/kimi/goose/qwen/pi/copilot/antigravity 一堆家 + `--from-openclaw`
拉任意 agent。**纳入它=军团瞬间多一堆现在没有的家(量大管饱)+多层嵌套**
(军团 harness omnigent · omnigent harness 一堆家)。

设计(对齐纲领·非侵入 backends 扩展点):
  - probe(): 诚实探测 omnigent CLI + runner 依赖(python-socks)在不在·不假跑
  - run(qhash, task, gold): 配军团 AIGC 网关 env 后调 omnigent·把结果如实返回
  - 显式请求通过原生 executor.auth 绑定授权/网关；必要头由既有桥的透明 Responses 模式补齐。
    无 ctx 的老式调用保留目录选型与 Responses→Chat bridge。
  - 子家可选:MEYO_OMNIGENT_HARNESS 指定 omnigent 内部用哪家(默认 codex·军团已验证能跑)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

_UNIFIED_GATEWAY = os.environ.get("MEYO_UNIFIED_GATEWAY",
                                  "https://aigc.sankuai.com/v1/openai/native")
# 默认用 openai-agents:它走 OpenAI Responses API·可复用军团已验证的 Responses→Chat bridge。
# codex native 在 omnigent 内默认路由 localhost:11434(ollama口)·不适配军团AIGC网关。
_DEFAULT_SUB = os.environ.get("MEYO_OMNIGENT_HARNESS", "openai-agents")


def _omnigent_bin() -> str | None:
    return shutil.which("omnigent")


def _runtime_registry():
    """Use the existing source/release sibling; never import native SDK packages."""
    import importlib
    import sys

    if __package__:
        from .omnigent_request_binding import _service_import_path
    else:
        from omnigent_request_binding import _service_import_path
    _service_import_path()
    root = Path(__file__).resolve().parents[2]
    if not (root / "harness_registry.py").is_file():
        raise RuntimeError("Omnigent shared task runtime is unavailable")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module("harness_registry")


# 依 omnigent 哲学(inner/*_executor.py:lazy import·缺extra给安装命令·不预探测):
#   军团不假装18家都能跑·诚实分层。openai-agents 已真跑验证(经bridge出答案)。
#   其余子家=可发现·首次用时按 omnigent 的 extra_install 提示装(omnigent自己也是lazy)。
_VERIFIED_SUBS = {"openai-agents"}  # 已端到端验证经军团网关bridge真出答案的子家


def _native_info(cli=None):
    if __package__:
        from .omnigent_request_binding import _run_native_helper
    else:
        from omnigent_request_binding import _run_native_helper
    entry = cli if cli is not None else _omnigent_bin()
    if not entry:
        return {}
    return _run_native_helper(entry, dict(os.environ), {"operation": "inspect"})


def list_sub_harnesses() -> list[str]:
    """动态读 omnigent registry 的子harness清单(不硬编码·量大管饱的来源)。

    军团纳入omnigent后·可经它代跑这些子家(cursor/kimi/goose/qwen…军团本来没有的)。
    过滤 native/别名变体·只留干净可选名。依据:omnigent runtime/harnesses/__init__.py 动态registry。"""
    try:
        names = _native_info().get("sub_harnesses", [])
    except Exception:
        return []
    return _clean_sub_harnesses(names)


def _clean_sub_harnesses(names):
    clean, seen = [], set()
    for n in sorted(names):
        if "native" in n:
            continue
        base = n.replace("-sdk", "").replace("-code", "").replace("-build", "")
        if base in seen:
            continue
        seen.add(base)
        clean.append(n)
    return clean


def sub_harness_status(subs=None) -> list[dict]:
    """诚实分层子家状态(依omnigent lazy哲学·不预探18家假可用)。

    verified=已端到端真出答案·其余=可发现·首次用按omnigent extra_install装。
    依据:omnigent/inner/*_executor.py(cursor/copilot等缺extra时报extra_install_display)。"""
    if subs is None:
        subs = list_sub_harnesses()
    out = []
    for s in subs:
        out.append({
            "name": s,
            "status": "verified" if s in _VERIFIED_SUBS else "discoverable",
            "note": ("已验证:经军团bridge真出答案" if s in _VERIFIED_SUBS
                     else "可发现·首次用时omnigent会提示装extra(omnigent lazy机制)"),
        })
    return out


# 军团网关支持的模型(models.json programming方向)·omnigent默认gpt-5.6网关不认→用这个
_SUB_MODEL = os.environ.get("MEYO_OMNIGENT_MODEL", "gpt-5.4")


def _aigc_model_appid(prefer_model: str = "") -> tuple[str | None, str | None]:
    """仅供无显式授权的旧调用选(模型名, appid)。优先 prefer_model·
    其次 _SUB_MODEL·appid必须和模型配对(否则网关拒)。prefer_model 不在军团网关清单里则
    自然落回下面的兜底(不硬塞·避免走错协议)。"""
    want = prefer_model or _SUB_MODEL
    try:
        mp = Path.home() / ".claude/skills/aigc-model-router/models.json"
        d = json.loads(mp.read_text(encoding="utf-8"))
        cands = []
        for direction in ("programming", "general_chat_high_coverage"):
            cands += (d.get(direction) or {}).get("candidates", [])
        # 先找指定模型(prefer_model 优先·让父层 ctx.model 真生效)
        for c in cands:
            if c.get("model") == want and c.get("appids"):
                return c["model"], c["appids"][0]
        # 退回第一个有appid的
        for c in cands:
            if c.get("appids"):
                return c.get("model"), c["appids"][0]
    except Exception:
        pass
    return None, None


def probe() -> dict:
    """诚实探测:omnigent CLI 在不在 + runner 依赖(python-socks·SOCKS代理需要)可导入。"""
    b = _omnigent_bin()
    if not b:
        return {"backend": "omnigent", "available": False, "cli": None,
                "reason": "omnigent_not_installed",
                "hint": "uv tool install omnigent (或 pip install omnigent)"}
    # 不能只看 CLI 文件存在：服务端 import 链坏了时每次 run 都会先起 daemon 后 rc1。
    # 这里做无网络 runtime 探针，让本地依赖故障在派活前就被看见，不误报 available。
    info = {}
    try:
        info = _native_info(b)
        missing = info.get("runtime_errors", [])
    except Exception as exc:
        missing = [f"native_runtime:{type(exc).__name__}:{str(exc)[:180]}"]
    subs = _clean_sub_harnesses(info.get("sub_harnesses", []))
    available = not missing
    return {"backend": "omnigent", "available": available, "cli": b,
            "socks_ready": not any(x.startswith("python_socks:") for x in missing),
            "runtime_ready": available, "runtime_errors": missing,
            "default_sub_harness": _DEFAULT_SUB,
            "sub_harnesses": subs, "sub_status": sub_harness_status(subs), "sub_count": len(subs),
            "runtime_python": info.get("python"), "runtime_prefix": info.get("prefix"),
            "native_source": info.get("native_source"),
            "note": (f"omnigent 可用·经它可调 {len(subs)} 个子家(军团没有的:cursor/kimi/goose/qwen/grok/opencode…)"
                     if available else "omnigent CLI 存在，但本地 runtime 依赖不完整，暂不可派活"),
            "gateway": _UNIFIED_GATEWAY,
            "hint": "军团派活给 omnigent·设 MEYO_OMNIGENT_HARNESS=<子家> 指定用哪个(默认openai-agents走网关bridge已验证)"}


def run(qhash: str, task: str, gold: str = "", prefer_model: str | None = None,
        resume_id: str = "", runtime_env: dict | None = None, *,
        appid: str | None = None, gateway: str | None = None,
        native_checkpoint: dict | None = None) -> dict:
    """经 omnigent 跑一个子家·配军团 AIGC 网关。返回统一 {final,status,elapsed}。

    context not control:只把任务给 omnigent+它选的子家·不替它改写/门控答案。
    显式 appid/gateway（含 runtime_env 中的请求值）走原生 spec auth，不重新选模型/key。
    原生 resume 无授权更新接口：校验 checkpoint 绑定，不一致则保留原会话并明确失败。
    老式无授权参数调用保留目录选型/bridge 接口。所有 env 都是请求副本，不写 os.environ。
    """
    if __package__:
        from .omnigent_request_binding import (
            NativeBindingError, binding_failure, checkpoint_for, native_launcher, resolve_binding, _cli_runtime)
        from .omnigent_responses_bridge import transparent_bridge
    else:  # 保留直接执行此 backend 文件的旧入口。
        from omnigent_request_binding import (
            NativeBindingError, binding_failure, checkpoint_for, native_launcher, resolve_binding, _cli_runtime)
        from omnigent_responses_bridge import transparent_bridge

    t0 = time.time()
    b = _omnigent_bin()
    if not b:
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "omnigent CLI 未安装", "resumed_native": False,
                "native_checkpoint": native_checkpoint or checkpoint_for(resume_id, None)}
    model = None
    sub = _DEFAULT_SUB
    request_env = {str(k): str(v) for k, v in (runtime_env or {}).items()
                   if k and v is not None}
    key_fields = ("MEYO_UNIFIED_APPID", "AIGC_APPID", "OPENAI_API_KEY")
    explicit = (appid is not None or gateway is not None
                or any(k in request_env for k in (*key_fields, "MEYO_UNIFIED_GATEWAY", "OPENAI_BASE_URL")))
    use_bridge = not explicit and sub.startswith("openai")
    _bridge_cm = None
    _native_cm = None
    _transparent_cm = None
    binding = None
    runtime = None
    try:
        # The bridge belongs to Meyo's Python/vendor, while CLI/daemon/runner
        # belong to the native venv. Their dependency environments are distinct.
        runtime = _runtime_registry()
        caller_env = dict(getattr(runtime._INPROC_RUNTIME_ENV, "values", {}) or {})
        service_env = {**os.environ, **caller_env, **request_env}
        env = _cli_runtime(b, service_env)[1]
        if explicit:
            # 显式空值同样有语义，不能被 ambient key 替代。
            selected_key = appid if appid is not None else next(
                (request_env[k] for k in key_fields if k in request_env),
                env.get("MEYO_UNIFIED_APPID") or env.get("AIGC_APPID") or env.get("OPENAI_API_KEY", ""))
            selected_gateway = gateway if gateway is not None else next(
                (request_env[k] for k in ("MEYO_UNIFIED_GATEWAY", "OPENAI_BASE_URL") if k in request_env),
                env.get("MEYO_UNIFIED_GATEWAY") or env.get("OPENAI_BASE_URL", ""))
            binding = resolve_binding(
                prefer_model if prefer_model is not None else env.get("MEYO_UNIFIED_MODEL", ""),
                selected_key, selected_gateway,
                sub, env, resume_id, native_checkpoint)
            model = binding["model"]
            _transparent_cm = transparent_bridge(binding, service_env, native_checkpoint)
            binding = _transparent_cm.__enter__()
            env.update(AIGC_APPID=binding["api_key"], MEYO_UNIFIED_APPID=binding["api_key"],
                       OPENAI_API_KEY=binding.get("client_api_key", binding["api_key"]),
                       OPENAI_BASE_URL=binding.get("client_base_url", binding["base_url"]),
                       MEYO_UNIFIED_GATEWAY=selected_gateway, MEYO_UNIFIED_MODEL=model)
            _native_cm = native_launcher(binding, sub, cli=b, environment=env)
            native_path = _native_cm.__enter__()
        else:
            model, legacy_appid = _aigc_model_appid(prefer_model or "")
            if legacy_appid:
                env["AIGC_APPID"] = legacy_appid
                env.setdefault("OPENAI_API_KEY", legacy_appid)
        if use_bridge:
            try:
                import harness_registry as _hr  # 军团 codex 同款 bridge(同package父路径可import)
                # ★ 根因修(2026-08-28 逐家测:omnigent 964s error):固定端口 18756 会和 codex 冲突
                #   (codex 早改成 PID 派生唯一端口·omnigent 还用固定 18756→并发/串行残留时撞车·
                #   A 结束 terminate 杀掉 B 正用的 bridge)。改成 PID 派生唯一端口·各起各的·不撞。
                _bport = 18756 + (os.getpid() % 4000)
                _bridge_cm = _hr._codex_bridge_ctx(_bport)
                ok = _bridge_cm.__enter__()
                if not ok:
                    return {"final": "", "status": "unavailable",
                            "elapsed": round(time.time() - t0, 1),
                            "error": f"responses bridge 起不来({_bport})·omnigent openai系家需要它"}
                env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{_bport}/v1"
            except Exception as e:
                return {"final": "", "status": "unavailable",
                        "elapsed": round(time.time() - t0, 1),
                        "error": f"bridge 不可用:{type(e).__name__}:{str(e)[:120]}"}
        elif not explicit:
            env.setdefault("OPENAI_BASE_URL", _UNIFIED_GATEWAY)
        # Omnigent no-AGENT 模式会把 --tools 对应 schema 与本地 callable 一起交给 SessionsChat。
        # 闭环由 omnigent cli `_materialize_harness_launcher_file(..., client_tools=tools)` 实现；
        # 不设置无人读取的额外 env，避免把“写了配置”误当成“运行时已接通”。
        cmd = [b, "run"]
        if explicit:
            cmd.append(str(native_path))
        cmd += ["--harness", sub, "--tools", "coding"]
        if resume_id:
            cmd += ["--resume", resume_id]
        if model:
            cmd += ["--model", model]
        cmd += ["-p", task]
        configured_timeout = service_env.get("LEGION_HARNESS_TIMEOUT")
        timeout = (float(configured_timeout) if configured_timeout
                   else runtime._WALL_S)
        # Request context was merged before _cli_runtime cleaned the native env.
        # Do not let _run_live reintroduce service PYTHONPATH/PYTHONHOME afterward.
        runtime.set_inproc_runtime_env(None)
        try:
            r = runtime._run_task(
                cmd, timeout=timeout, input="", env=env,
                cwd=env.get("MEYO_WORKDIR") or None, tag="omnigent",
            )
        finally:
            runtime.set_inproc_runtime_env(caller_env)
        out = (r.stdout or "").strip()
        final = out[-4000:] if out else ""
        cancelled = bool(getattr(r, "cancelled", False))
        status = ("cancelled" if cancelled else
                  "ok" if r.returncode == 0 and final else f"rc{r.returncode}")
        if cancelled:
            final = ""
        error = (r.stderr or "").strip()[-4000:] if r.returncode != 0 else ""
        if binding:
            error = error.replace(binding["api_key"], "[redacted]")
        local_markers = ("local daemon exited", "server became ready", "modulenotfounderror",
                         "no module named", "tool ", "not found in agent", "failed to start")
        local_runtime_failure = bool(error) and any(x in error.lower() for x in local_markers)
        # CLI 会在输出/提示中给出 conv_*；保存 opaque id，下一进程用 --resume 原生接回。
        import re
        match = re.search(r"OMNIGENT_CONVERSATION_ID=([A-Za-z0-9_-]+)",
                          (r.stdout or "") + "\n" + (r.stderr or ""))
        native_id = match.group(1) if match else resume_id
        result = {"final": final, "status": status,
                  "elapsed": round(time.time() - t0, 1), "via": f"omnigent/{sub}",
                  "bridge": bool(use_bridge or (binding and binding.get("bridge"))),
                  "transport": ("responses_passthrough" if binding and binding.get("bridge")
                                else "responses_chat" if use_bridge else "native"),
                  "model_used": model or "",
                  "native_checkpoint": checkpoint_for(native_id, binding),
                  "resumed_native": bool(resume_id),
                  "stderr_tail": error[-1000:] if error else ""}
        if error:
            result["error"] = error
        if cancelled:
            result.update(
                cancelled=True,
                resumed_native=False,
                native_facts={"cli_cancelled": True, "cli_returncode": r.returncode,
                              "resume_requested": bool(resume_id),
                              "native_turn_terminal": "unobserved"},
                error_envelope={"category": "cancelled", "retryable": False,
                                "scope": "harness", "code": "caller_cancelled"},
            )
        elif local_runtime_failure:
            result["fail_kind"] = "harness_runtime"
            result["error_envelope"] = {
                "category": "harness_runtime", "retryable": False, "scope": "harness",
                "tool": "omnigent", "code": status, "message": error,
            }
        return result
    except NativeBindingError as exc:
        return binding_failure(exc, round(time.time() - t0, 1), resume_id, native_checkpoint)
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired 会携带已收集 stdout/stderr；若首轮已打印 conversation id，也必须保住，
        # 不能只在 resume_id 本来存在时才回 checkpoint。
        partial = "\n".join(
            value.decode("utf-8", errors="replace") if isinstance(value, bytes)
            else str(value or "")
            for value in (
                getattr(exc, "legion_stdout", ""), getattr(exc, "legion_stderr", ""),
                getattr(exc, "stdout", ""), getattr(exc, "stderr", ""),
            )
        )
        import re
        match = re.search(r"OMNIGENT_CONVERSATION_ID=([A-Za-z0-9_-]+)", partial)
        native_id = match.group(1) if match else resume_id
        runtime_failure = runtime._runtime_timeout(exc) if runtime is not None else {}
        return {"final": "", "status": "timeout",
                "elapsed": round(time.time() - t0, 1),
                "error": f"omnigent/{sub} CLI等待超时",
                **runtime_failure,
                "native_checkpoint": checkpoint_for(native_id, binding),
                "resumed_native": False,
                "native_facts": {"cli_wait_timed_out": True,
                                 "resume_requested": bool(resume_id),
                                 "native_turn_terminal": "unobserved"}}
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)}"
        if binding:
            detail = detail.replace(binding["api_key"], "[redacted]")
        return {"final": "", "status": "error", "elapsed": round(time.time() - t0, 1),
                "error": detail[:200], "native_checkpoint": native_checkpoint or checkpoint_for(resume_id, None),
                "resumed_native": False}
    finally:
        if _native_cm is not None:
            with __import__("contextlib").suppress(Exception):
                _native_cm.__exit__(None, None, None)
        if _transparent_cm is not None:
            _transparent_cm.__exit__(None, None, None)
        if _bridge_cm is not None:
            with __import__("contextlib").suppress(Exception):
                _bridge_cm.__exit__(None, None, None)


if __name__ == "__main__":
    import sys
    print(json.dumps(probe(), ensure_ascii=False, indent=1))
    if len(sys.argv) > 1:
        print(json.dumps(run("adhoc", sys.argv[1]), ensure_ascii=False, indent=1))
