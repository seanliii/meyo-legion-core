"""Aider 单次原生调用；不解析控制台文案，不代替原生编辑和重试循环。

仅支持发布声明中的单次任务，不声称等价于交互 CLI 的 watch/load 模式。
stdout 保留给 JSON 协议，原生输出留在本单私有日志。失败保留原生部分结果。
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path


def run_native(params, factory=None):
    if factory is None:
        from aider.main import main as factory
    argv = [
        "--no-auto-commits", "--no-gitignore", "--yes-always",
        "--no-detect-urls", "--no-pretty", "--no-stream", "--no-check-update",
        "--model", "openai/" + params["model"],
        "--openai-api-base", params["gateway"],
        "--openai-api-key", params["appid"],
    ]
    settings = os.environ.get("AIDER_MODEL_SETTINGS_FILE") or params.get("model_settings_file")
    if settings:
        argv += ["--model-settings-file", settings]
    coder = factory(argv=argv, return_coder=True)
    if not callable(getattr(coder, "run", None)):
        return 1, {"error": "aider_initialization_failed", "final": ""}
    coder.show_announcements()
    coder.io.add_to_input_history(params["task"])
    exhausted_before = coder.num_exhausted_context_windows
    malformed_before = coder.num_malformed_responses
    last_send = {"state": "not_started", "exception_type": None}
    native_send = coder.send

    def observed_send(*args, **kwargs):
        # 原生 send_message 会吞 API 异常并重试；只观察最后一次 send，
        # 不把已经恢复的历史异常、io.tool_error 或答案中的报错引用当失败。
        last_send.update(state="running", exception_type=None)
        try:
            yield from native_send(*args, **kwargs)
        except BaseException as exc:
            last_send.update(state="failed", exception_type=type(exc).__name__)
            raise
        else:
            last_send.update(state="completed", exception_type=None)

    coder.send = observed_send
    error = ""
    final = ""
    try:
        final = coder.run(with_message=params["task"]) or ""
    except Exception as exc:
        # CLI --message 同样不接着执行 SwitchCoder；不能另造恢复循环。
        error = "aider_native_exception:" + type(exc).__name__
        # 保留原生部分结果为独立事实，不把未收尾文本冒充干净 final。
    state = {
        "last_send": last_send,
        "exhausted_delta": coder.num_exhausted_context_windows - exhausted_before,
        "malformed_delta": coder.num_malformed_responses - malformed_before,
        "pending_reflection": bool(coder.reflected_message),
        "edited_files": sorted(coder.aider_edited_files),
    }
    if not error:
        if state["exhausted_delta"]:
            error = "aider_context_exhausted"
        elif last_send["state"] != "completed":
            error = "aider_send_incomplete"
        elif state["pending_reflection"]:
            error = "aider_reflection_unfinished"
        elif not final:
            error = "aider_empty_result"
    return (1 if error else 0), {
        "final": str(final), "error": error, "native_state": state,
        "native_partial_response": str(getattr(coder, "partial_response_content", "") or "") if error else "",
    }


def main():
    request = json.loads(sys.stdin.readline())
    log_path = None
    try:
        params = request["params"]
        # 原生控制台包含推理和警告，不是 runtime.error。留在本单私有
        # 日志中，协议仅回传路径；避免 SDK 把 stderr 全部当错误回流。
        state = Path(params["session_dir"]).resolve()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state,
                                         prefix="aider-", suffix=".log", delete=False) as log:
            log_path = log.name
            os.chmod(log.name, 0o600)
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                rc, result = run_native(params)
            result["native_log_path"] = log.name
    except Exception as exc:
        rc, result = 1, {"final": "", "error": "aider_worker_exception:" + type(exc).__name__}
        if log_path:
            result["native_log_path"] = log_path
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
