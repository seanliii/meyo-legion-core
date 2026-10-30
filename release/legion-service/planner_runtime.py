"""Resolve existing planner CLI and per-model credentials without source secrets."""
import os
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace


def run_cli(command, *, input, env, cancel_check=None):
    """Use the native CLI lifecycle, without publishing planner text/events."""
    import harness_registry as hr
    sink = getattr(hr._INPROC_EVENT_SINK, "cb", None)
    check = getattr(hr._INPROC_CANCEL_CHECK, "check", None)
    cancelled = getattr(hr._INPROC_CANCEL_CHECK, "cancelled", False)

    def stopped():
        return bool((cancel_check and cancel_check()) or (check and check()))

    try:
        # _run_live snapshots this sink into both reader threads. Do not globally
        # replace emitters or redirect stdout: other native tasks may be active.
        hr.set_inproc_event_sink(lambda event: None)
        hr.set_inproc_cancel_check(stopped)
        if stopped():
            return SimpleNamespace(returncode=130, stdout="", stderr="", cancelled=True)
        # The helper owns first-progress/idle waiting and the native token cap.
        # This runner retains process registration and scoped cancellation.
        return hr._run_live(command, 0, input=input, env=env, tag="planner")
    finally:
        hr.set_inproc_event_sink(sink)
        hr.set_inproc_cancel_check(check)
        hr._INPROC_CANCEL_CHECK.cancelled = cancelled


def diagnostic(stderr, authorization):
    text = (stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes)
            else str(stderr or ""))
    facts, code = {}, ""
    pattern = r"model_http_error:[1-5][0-9]{2}|model_call_error:[A-Za-z]+|model_(?:empty|incomplete)_response|model_stream_error"
    for line in text.splitlines():
        if re.fullmatch(pattern, line.strip()):
            code = line.strip()
        if not line.startswith("planner_diagnostic:") or len(line) > 8192:
            continue
        try:
            value = json.loads(line[len("planner_diagnostic:"):])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if (isinstance(value.get("phase"), str)
                and value["phase"] in {"request_open", "response_stream", "response_read",
                                      "response_parse", "content_validation"}):
            facts["phase"] = value["phase"]
        if value.get("timeout_kind") in ("total", "first_progress", "idle_stream"):
            facts["timeout_kind"] = value["timeout_kind"]
        for key in ("content_chars", "reasoning_chars", "http_status"):
            v = value.get(key)
            if type(v) is int and v >= 0:
                facts[key] = v
        for key in ("elapsed_s", "timeout_s", "first_progress_s", "last_progress_age_s"):
            v = value.get(key)
            if type(v) in (int, float) and math.isfinite(v) and v >= 0:
                facts["transport_elapsed_s" if key == "elapsed_s" else key] = v
        for key in ("request_id", "finish_reason"):
            v = value.get(key)
            if (isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", v)
                    and str(authorization) not in v):
                facts[key] = v
        usage = value.get("usage")
        if isinstance(usage, dict):
            clean = {k: v for k, v in usage.items()
                     if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and type(v) is int and v >= 0}
            for key, names in (("prompt_tokens_details", {"cached_tokens"}),
                               ("completion_tokens_details", {"reasoning_tokens"})):
                if isinstance(usage.get(key), dict):
                    details = {k: v for k, v in usage[key].items()
                               if k in names and type(v) is int and v >= 0}
                    if details:
                        clean[key] = details
            if clean:
                facts["usage"] = clean
    return facts, code


def tool_path():
    here = Path(__file__).resolve().parent
    return next((str(p) for p in (
        here.parents[1] / "scripts/export-model-tool.py",
        here.parent / "config/aigc-model-router/scripts/call_model.py",
        Path.home() / ".claude/skills/aigc-model-router/scripts/planner_call_model.py",
    ) if p.is_file()), "")


def model_env(model, base_env):
    import model_registry
    appid = model_registry._pick_appid("planner", model)
    if not appid:
        raise ValueError("planner_model_authorization_unavailable")
    return {**base_env, "LEGION_PLANNER_MODEL": model, "LEGION_PLANNER_APPID": appid,
            "LEGION_PLANNER_GATEWAY": base_env.get("MEYO_UNIFIED_GATEWAY")
            or "https://aigc.sankuai.com/v1/openai/native"}
