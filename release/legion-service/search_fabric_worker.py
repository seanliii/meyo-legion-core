"""One retained search invocation with fixed code and persistent service data.

Only this short-lived child binds the legacy module globals. It inherits the
caller's existing OS/network boundary; it is not an agent or a new sandbox.
SearchFabric and Friday/Google keep their original provider semantics.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.dont_write_bytecode = True


def _load(name, evolution):
    expected = (evolution / (name + ".py")).resolve()
    if not expected.is_relative_to(evolution) or not expected.is_file():
        raise FileNotFoundError(f"retained search module missing: {name}")
    module = importlib.import_module(name)
    if Path(module.__file__).resolve() != expected:
        raise ImportError(f"retained search module came from another source: {name}")
    return module


def _search_identity(resolve, default_alias=""):
    """Resolve only an explicitly configured search identity, never a model key."""
    if default_alias and (not isinstance(default_alias, str) or not default_alias.startswith("appid-")):
        raise ValueError("Friday 搜索默认身份必须是计费别名，不能是密钥")
    def selected(sources):
        # The native resolver (including its env_loader) keeps first priority.
        value = str(resolve(sources) or default_alias or "").strip()
        if not value:
            raise ValueError("Friday 搜索未配置专用计费身份；不会借用其他模型的密钥")
        if value.startswith("appid-"):
            pool = os.environ.get("MEYO_SHARED_MODEL_POOL_ENABLED", "1").strip().lower()
            if pool not in ("1", "true", "yes", "on"):
                raise ValueError("Friday 搜索使用的共享授权池已关闭或开关无效")
            from model_registry import _resolve_alias
            value = _resolve_alias(value)
            if not value:
                raise ValueError("Friday 搜索计费别名在现有授权池中未解析")
        return value
    return selected


def _private_header_runner(run, state):
    """Keep the fixed curl protocol but pass its secret header through an anonymous fd."""
    def request(command, *args, **kwargs):
        command = list(command)
        descriptors = []
        with contextlib.ExitStack() as stack:
            for index, argument in enumerate(command[:-1]):
                if argument not in ("--header", "-H"):
                    continue
                header = command[index + 1]
                if not isinstance(header, str) or not header.lower().startswith("authorization:"):
                    continue
                if any(character in header for character in ("\r", "\n", "\0")):
                    raise ValueError("Friday 搜索认证头包含无效控制字符")
                stream = stack.enter_context(tempfile.TemporaryFile(
                    mode="w+b", prefix=".friday-header-", dir=state))
                stream.write((header + "\n").encode("utf-8"))
                stream.flush()
                stream.seek(0)
                descriptors.append(stream.fileno())
                command[index + 1] = "@/dev/fd/" + str(stream.fileno())
            if descriptors:
                # An authenticated API call must not redirect its billing identity.
                command = [argument for argument in command
                           if argument not in ("--location", "-L", "--location-trusted")]
                # curl reads its default configuration before ordinary options.
                # A curlrc may otherwise restore redirects or log the actual
                # header even though the command line contains only an fd.
                command[1:1] = ["--disable"]
                command.extend(("--no-location", "--no-location-trusted"))
                kwargs["pass_fds"] = tuple(dict.fromkeys(
                    (*kwargs.get("pass_fds", ()), *descriptors)))
            return run(command, *args, **kwargs)
    return request


def _search_google(request, evolution, state, rf):
    """Bind the fixed Friday client to explicit identity, private transport and stores."""
    friday = _load("friday_universal", evolution)
    cache = _load("search_cache", evolution)
    health = _load("fetcher_health", evolution)
    # This same production origin/path has been verified with TLS and the
    # existing account. Do not alter an independently configured test endpoint.
    if friday._ENDPOINT_PROD == "http://agi.sankuai.com/tools/universal-search/api/v1":
        friday._ENDPOINT_PROD = "https://agi.sankuai.com/tools/universal-search/api/v1"
    friday._get_appid_for_sources = _search_identity(
        friday._get_appid_for_sources, request.get("friday_appid_alias", ""))
    friday.subprocess = SimpleNamespace(run=_private_header_runner(friday.subprocess.run, state))
    friday._FRIDAY_RATE_LIMITER = rf.RateLimiter(rate_root=state / ".friday_ratelimit")
    cache.CACHE_DB = str(state / ".search_cache.sqlite")
    health.HEALTH_DIR = str(state)
    trace = ["provider:friday/google-search"]
    health_log = health.health_log

    def observe_health(fetcher, record):
        health_log(fetcher, record)
        if isinstance(record, dict) and record.get("err"):
            trace.append(f"source_error:{fetcher}:{record['err']}")

    health.health_log = observe_health
    search_fn = friday.search_google_deep if request["mode"] == "deep" else friday.search_google
    hits = search_fn(request["query"], top_k=request["top_k"])
    return {"hits": hits, "trace": trace}


def search(request):
    from capstack_bridge import source_root
    import legion_paths
    import fcntl

    source = Path(source_root()).resolve(strict=True)
    evolution = (source / "v11/evolution").resolve(strict=True)
    if not evolution.is_relative_to(source):
        raise ValueError("retained search code escapes its source root")
    app = Path(__file__).resolve().parents[2]
    protected = [source, app]
    if os.environ.get("MEYO_RUNTIME_ROOT"):
        runtime = Path(os.environ["MEYO_RUNTIME_ROOT"])
        if not runtime.is_absolute():
            raise ValueError("MEYO_RUNTIME_ROOT must be absolute")
        protected.append(runtime.resolve())
    data = Path(legion_paths.legion_dir()).expanduser()
    if not data.is_absolute() or data.resolve() == Path(data.anchor):
        raise ValueError("retained search data root must be absolute and non-root")
    state = (data / "retained-auto-tools/search").resolve()
    if any(state.is_relative_to(root) or root.is_relative_to(state) for root in protected):
        raise ValueError("retained search state overlaps protected code")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    sys.path.insert(0, str(evolution))
    rf = _load("resilient_fetch", evolution)
    rf.CACHE_ROOT = state / ".resilient_cache"
    rf.RATE_ROOT = state / ".resilient_ratelimit"
    if request["source"] in ("google", "google-search"):
        # Friday's SQLite/file limiter synchronize their own writes and do not
        # consume SearchFabric's budget. Do not serialize unrelated providers.
        return _search_google(request, evolution, state, rf)
    # The legacy budget and health writers only have process-local locks. Keep
    # one persistent-root lock, not a fresh quota namespace on every request.
    with (state / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        health = _load("source_health", evolution)
        fabric_module = _load("search_fabric", evolution)
        if fabric_module._BUDGET_INSTANCE is not None or rf._DEFAULT_LIMITER is not None:
            raise RuntimeError("retained search state was used before worker binding")
        fabric_module._BUDGET_FILE = state / ".search_fabric_budget.json"
        fabric_module._CACHE_DIR = state / ".search_fabric_cache"
        fabric_module._RATE_LIMITER = rf.RateLimiter(rate_root=state / ".search_fabric_ratelimit")
        # _BANDIT_FILE is unused in the fixed library; no fictitious migration.
        health.LOG = state / ".source_health.jsonl"
        health.STATE = state / ".source_health_state.json"
        health._DISABLED_SOURCES = state / ".search_disabled_sources.json"
        notices = []
        source_errors = []
        record_fetch = health.record_fetch

        def observe_fetch(*args, **kwargs):
            record = record_fetch(*args, **kwargs)
            # SearchFabric filters failed hits before returning. Its original
            # health record still carries the source and error; forward that
            # return value without repeating the write or changing its policy.
            if isinstance(record, dict) and record.get("err"):
                source_errors.append(f"source_error:{record['source']}:{record['err']}")
            return record

        def no_reflection(source, reason, streak):
            # Keep record_fetch's real counters/health persistence. This trace
            # distinguishes its reflection *attempt* from an executed workflow.
            notices.append(f"legacy_reflection_not_started:{source}:{reason}:{streak}")

        def no_legacy_alert(severity, message):
            notices.append(f"legacy_alert_not_started:{severity}")

        health._fire_reflection = no_reflection
        health._alert = no_legacy_alert
        health.record_fetch = observe_fetch
        fabric = fabric_module.SearchFabric(use_router=True, fanout=2, fallback_to_anysearch=True)
        hits = fabric.search(request["query"], top_k=request["top_k"], mode=request["mode"],
                             force_sources=[request["source"]] if request["source"] else None)
        trace = list(fabric._trace)
        # The library returns some provider exceptions as error hits. Retain the
        # actual type/message rather than silently discarding them in the parent.
        trace.extend(str(hit["_error"]) for hit in hits if isinstance(hit, dict) and hit.get("_error"))
        trace.extend(source_errors)
        trace.extend(notices)
        return {"hits": hits, "trace": trace}


def main():
    from external_agent_transports import redact_native_diagnostic
    try:
        request = json.load(sys.stdin)
        if (not isinstance(request, dict) or not isinstance(request.get("query"), str)
                or not isinstance(request.get("source"), str)
                or not isinstance(request.get("friday_appid_alias", ""), str)
                or request.get("mode") not in ("fast", "balanced", "deep")
                or not isinstance(request.get("top_k"), int) or not 1 <= request["top_k"] <= 20):
            raise ValueError("invalid retained search request")
        # Legacy diagnostics belong on stderr; stdout is the one JSON reply.
        with contextlib.redirect_stdout(sys.stderr):
            result = search(request)
        # The child knows its own invocation environment. Sanitize diagnostic
        # views only, never the provider's document content or source metadata.
        if "trace" in result:
            result["trace"] = redact_native_diagnostic(result["trace"])
        if isinstance(result.get("hits"), list):
            result["hits"] = [
                redact_native_diagnostic(hit)
                if isinstance(hit, dict) and (hit.get("_error") or hit.get("_source_health"))
                else hit for hit in result["hits"]
            ]
    except Exception as exc:
        result = {"hits": [], "trace": [], "error_type": type(exc).__name__,
                  "error": redact_native_diagnostic(str(exc))[:160]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
