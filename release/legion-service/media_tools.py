#!/usr/bin/env python3
"""
media_tools · 军团多媒体工具箱(第②批)

把散落各 harness 的多媒体能力,收成一组军团任意一家都能调的统一工具:
  - gen_image   文生图    → image_generation 方向配置模型· 真出图片
  - see_image   识图/视觉  → vision_understanding 方向(gpt-5/opus/gemini)· 读用户上传的图
  - read_pdf    PDF 解析   → 本地(pdftotext / pdfplumber / pypdf)· 长 PDF 分块 + 可选摘要
  - gen_video   视频生成   → 文生视频优先Seedance方舟网关(真AI·异步任务约3-5分钟·限流冷却换appid);
                  图生视频走 OpenMontage 动态选 provider，云不可用回退本地 FFmpeg motion(降级产物带degraded标记)

设计(对齐纲领):
  - 生图/识图走随包 media_model_cli.py，模型与授权复用 model_registry
  - 如实报告:能做就做,做不了(视频)明说原因,不糊弄
  - 已绑定任务的图片落本单工作区；仅旧服务媒体根返回真实映射的 /media URL
"""
import os
import subprocess
import sys
import time
import uuid
import re
import html
import json
import threading
import math
import stat
from pathlib import Path
import urllib.request
import urllib.error

import legion_paths as _paths

HERE = os.path.dirname(os.path.abspath(__file__))
_CALL_MODEL = os.path.join(HERE, "media_model_cli.py")
_MODEL_ENV_FIELDS = (
    "MEYO_MODEL_CATALOG_PATH", "MEYO_MODEL_MATRIX_PATH", "MEYO_APPID_ALIASES",
    "MEYO_MEDIA_GATEWAY", "MEYO_UNIFIED_GATEWAY", "MEYO_TASK_DEADLINE",
    "MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER", "MEYO_USER_ID",
    "MEYO_WORKDIR", "MEYO_MODEL_WORKSPACE_ID", "LEGION_PROC_REGISTRY_DIR",
    "MEYO_CAPABILITY_POLICY_JSON",
    "MEYO_SESSION_ID", "MEYO_QHASH", "MEYO_TASK_IDEMPOTENCY_KEY",
    "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
)


def _call_model_path() -> str:
    """明确覆盖优先，默认随包入口；不搜索个人 HOME 中的 skill。"""
    return os.environ.get("MEYO_MEDIA_MODEL_CLI") or _CALL_MODEL


_MEDIA_SAFE_ERRORS = frozenset({
    "media_direction_unavailable", "media_authorized_model_unavailable",
    "media_model_catalog_unavailable",
    "media_output_limit", "media_finish_reason_invalid",
    "media_gateway_invalid", "media_gateway_error", "media_image_url_invalid",
    "media_image_missing", "media_image_empty", "media_image_invalid_png",
    "media_image_path_invalid", "media_image_unavailable", "media_image_workspace_invalid",
    "media_image_read_denied", "media_image_policy_invalid",
    "media_image_requires_vision_direction", "media_chat_direction_invalid",
    "media_image_type_unsupported", "media_chat_empty_response", "media_timeout_invalid",
    "media_image_billable_denied", "media_image_network_denied", "media_image_write_denied",
})
_MEDIA_SAFE_EXCEPTION_TYPES = frozenset({
    "TimeoutError", "URLError", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "RemoteDisconnected", "IncompleteRead", "SSLError",
    "FileNotFoundError", "PermissionError", "IsADirectoryError", "OSError",
    "JSONDecodeError", "UnicodeDecodeError", "ValueError", "TypeError",
    "KeyError", "IndexError", "AttributeError", "Error",
})


def _safe_media_error(stderr: str) -> str:
    """仅接受完整行的已知错误码；不复制正文、未知后缀或异常消息。"""
    if not isinstance(stderr, str):
        return ""
    for line in reversed((stderr or "").splitlines()):
        code = line.strip()
        if code in _MEDIA_SAFE_ERRORS:
            return code
        if re.fullmatch(r"media_http_error:[345][0-9]{2}", code):
            return code
        if code.startswith("media_call_error:"):
            kind = code.partition(":")[2]
            return code if kind in _MEDIA_SAFE_EXCEPTION_TYPES else "media_call_error"
    return ""


def _gw_env(environment=None) -> dict:
    """网关调用(生图/识图/摘要)的子进程环境:让 aigc.sankuai.com 直连·不走 HTTP(S)_PROXY。
    持久化根治(用户2026-09-01"保证持久化运行不错"):部署机若设了死代理 env(如 HTTP_PROXY 指向
    没起的本地代理),urllib 会把网关请求也发给死代理→Connection refused→生图整个fail。网关是
    内网服务本就该直连,把它加进 NO_PROXY(保留原代理供 github 等外网用·不粗暴清全部代理)。"""
    env = dict(os.environ if environment is None else environment)
    if "MEYO_MODEL_CATALOG_PATH" in env:
        import model_registry
        try:
            env["MEYO_MODEL_CATALOG_PATH"] = model_registry.model_catalog_path(env)
        except model_registry.ModelCatalogError:
            # 保留无效显式声明，让 CLI 报固定错误；不能删掉它后静默回落 HOME。
            pass
    gw = "aigc.sankuai.com"
    for key in ("NO_PROXY", "no_proxy"):
        cur = env.get(key, "")
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        if gw not in parts:
            parts.append(gw)
        env[key] = ",".join(parts)
    return env


def _model_tool_env():
    """Both model wrappers consume the same trusted thread binding, never params."""
    registry = sys.modules.get("harness_registry")
    local = getattr(registry, "_INPROC_RUNTIME_ENV", None)
    bound = dict(getattr(local, "values", {}))
    inherit = getattr(local, "inherit_process_context", True)
    env = dict(os.environ)
    if not inherit:
        for field in _MODEL_ENV_FIELDS:
            env.pop(field, None)
        # An unbound task must not borrow the host's model catalog.
        env["MEYO_MODEL_CATALOG_PATH"] = ""
    env.update({key: bound[key] for key in _MODEL_ENV_FIELDS if key in bound})
    return _gw_env(env), bool(bound) or not inherit


_MEDIA_DIR = _paths.media_dir()


def _ensure_dir():
    os.makedirs(_MEDIA_DIR, exist_ok=True)


def _new_path(ext: str) -> str:
    _ensure_dir()
    return os.path.join(_MEDIA_DIR, f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}")


def _rel_url(path: str) -> str:
    """产物本机路径 → 服务可暴露的 /media/<name> url(第③批 server 挂静态)。"""
    return "/media/" + os.path.basename(path)


# ── 文生图:使用 image_generation 方向配置 ──
def gen_image(prompt: str, timeout_s: int = 180) -> dict:
    """直接调用与工具箱调用共用工作区、真实文件事实及既有副作用账本。"""
    params = {"prompt": prompt}
    if timeout_s != 180:
        params["timeout_s"] = timeout_s
    return _generate_image(params)


def _image_workspace_directories(workspace, environment):
    """只在已绑定目录内建产物/账本目录，不改变整个运行状态根或沙箱权限。"""
    from media_model_cli import MediaError
    from personal_runtime import model_tool_workspace
    root = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root)
        if [info.st_dev, info.st_ino] != json.loads(environment["MEYO_MODEL_WORKSPACE_ID"]):
            raise MediaError("media_image_workspace_invalid")
        for name in ("media", ".meyo-media"):
            try:
                os.mkdir(name, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                info = os.fstat(child)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise MediaError("media_image_workspace_invalid")
            finally:
                os.close(child)
        model_tool_workspace(environment, require_bound=True)
    finally:
        os.close(root)
    return workspace / "media", workspace / ".meyo-media" / "side_effect_ledger.json"


def _verified_image_result(result, environment, *, cached=False, scoped=True):
    """命中缓存也重新核本次读权限和文件字节，不把旧路径/URL当成当前事实。"""
    from media_model_cli import MediaError, image_file_facts
    try:
        # 旧 operator 缓存只复用原媒体根的文件，不继承显式绝对路径读取权限。
        # 不 resolve：后续 helper 仍须逐层拒绝符号链接和路径替换。
        if not scoped and Path(result.get("path", "")).parent != Path(_MEDIA_DIR):
            raise MediaError("media_image_path_invalid")
        facts = image_file_facts(result.get("path", ""), environment)
        for field in ("sha256", "bytes"):
            if field in result and result[field] != facts[field]:
                raise MediaError("media_image_unavailable")
    except (MediaError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc) if isinstance(exc, MediaError)
                else "media_image_unavailable", "requires_reconciliation": True}
    out = {"ok": True, "model": "image_generation", **facts}
    if not scoped and Path(facts["path"]).parent == Path(_MEDIA_DIR):
        out["url"] = _rel_url(facts["path"])
    if cached:
        out.update(idempotent_hit=True,
                   idempotent_note="复用同一请求的已生成图片；本次已重新验证文件与读权限。")
    return out


def _generate_image(params):
    """原有生图执行器；工作区内复用强账本，旧成功缓存只读兼容。"""
    from media_model_cli import MediaError, validate_image_generation
    import legion_idempotency as idem
    import side_effect_ledger as ledger
    env, scoped = _model_tool_env()
    cli = _call_model_path()
    if not os.path.isfile(cli):
        return {"ok": False, "error": "媒体模型 CLI 不存在", "request_started": False}
    effect, ledger_path = {}, None
    try:
        timeout = params.get("timeout_s", 180)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise MediaError("media_timeout_invalid")
        if not isinstance(params.get("prompt"), str) or not params["prompt"].strip():
            return {"ok": False, "error": "生图需要非空 prompt", "request_started": False}
        if scoped and not env.get("MEYO_WORKDIR"):
            raise MediaError("media_image_workspace_invalid")
        workspace = validate_image_generation(env)
        scoped = scoped or workspace is not None
        if workspace is not None:
            info = workspace.stat()
            env.setdefault("MEYO_MODEL_WORKSPACE_ID", json.dumps([info.st_dev, info.st_ino]))
            env["MEYO_WORKDIR"] = str(workspace)
        # Original normalized business arguments remain the cache key. Neither
        # a random output filename nor an implicit timeout is added to it.
        hit = idem.check_bound("gen_image", params, environment=env)
        if hit is not None:
            return _verified_image_result(hit, env, cached=True, scoped=scoped)
        if workspace is not None:
            media_dir, ledger_path = _image_workspace_directories(workspace, env)
            target = str(media_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png")
        else:
            target = _new_path("png")
        key = (env.get("MEYO_TASK_IDEMPOTENCY_KEY") or idem._qhash_from_environment(env)
               or (f"workspace:{env['MEYO_MODEL_WORKSPACE_ID']}" if workspace is not None else ""))
        effect = ledger.begin("gen_image", params, key, path=ledger_path,
                              lease_s=max(60, int(timeout) + 30), safe_retry=False)
        decision = effect.get("decision")
        if decision == "reuse":
            return {**_verified_image_result(effect.get("result") or {}, env,
                                             cached=True, scoped=scoped),
                    "effect_id": effect["effect_id"]}
        if decision in ("in_progress", "requires_reconciliation"):
            return {"ok": False, "error": "SIDE_EFFECT_IN_PROGRESS" if decision == "in_progress"
                    else "SIDE_EFFECT_UNKNOWN", "effect_id": effect["effect_id"],
                    "effect_in_progress": decision == "in_progress",
                    "requires_reconciliation": decision == "requires_reconciliation"}
    except (MediaError, idem.BoundCacheError, ledger.LedgerError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, (MediaError, idem.BoundCacheError, ledger.LedgerError)) \
            else "media_image_workspace_invalid"
        return {"ok": False, "error": code, "request_started": False}

    bundled = os.path.realpath(cli) == os.path.realpath(_CALL_MODEL)
    command = [sys.executable, cli, "image", params["prompt"], "--out", target,
               "--timeout", str(timeout)]
    if bundled:
        command.append("--json")
    try:
        r = subprocess.run(command, capture_output=True, text=True,
                           timeout=timeout + 15, env=env,
                           **({"cwd": workspace} if workspace is not None else {}))
        try:
            data = json.loads(r.stdout) if bundled else None
        except (ValueError, TypeError):
            data = None
        valid = (isinstance(data, dict) and data.get("ok") is (r.returncode == 0)
                 and type(data.get("request_started")) is bool)
        if bundled and (not valid or (r.returncode == 0 and data.get("path") != target)):
            out = {"ok": False, "error": _safe_media_error(r.stderr) or "media_tool_invalid_response",
                   "request_started": None}
        elif r.returncode != 0:
            error = _safe_media_error(data.get("error")) if valid else ""
            out = {"ok": False, "error": error or _safe_media_error(r.stderr)
                   or "media_tool_invalid_response",
                   "request_started": data["request_started"] if valid else None}
        else:
            out = _verified_image_result(data if bundled else {"path": target}, env, scoped=scoped)
            out["request_started"] = True
    except subprocess.TimeoutExpired:
        out = {"ok": False, "error": "media_image_timeout", "request_started": None}
    except OSError:
        # run 也可能在 Popen 成功后的管道读取阶段报错，不能据此安全重试付费请求。
        out = {"ok": False, "error": "media_image_process_unavailable", "request_started": None}
    uncertain = not out.get("ok") and out.get("request_started") is not False
    if uncertain:
        out["requires_reconciliation"] = True
    if effect.get("effect_id"):
        out["effect_id"] = effect["effect_id"]
        try:
            saved = ledger.finish(effect["effect_id"], out, path=ledger_path,
                                  attempt=effect["attempt"], uncertain=uncertain)
            if not saved.get("ok"):
                raise ledger.LedgerError("ledger_finish_failed")
        except (ledger.LedgerError, OSError, ValueError):
            # The image can exist even when final accounting failed. Preserve
            # the useful artifact, but do not claim a safe automatic retry.
            out.update(effect_ledger_warning="finish_failed", requires_reconciliation=True)
    return out


# ── 识图/视觉理解:读用户上传的图,回答问题 ──
def see_image(image_path: str, question: str = "详细描述这张图里的内容", timeout_s: int = 90) -> dict:
    """识图；保留完整答案和随包 CLI 的本次元数据，model 仍是兼容方向标签。"""
    cli = _call_model_path()
    if not os.path.isfile(cli):
        return {"ok": False, "error": "媒体模型 CLI 不存在"}
    env, scoped = _model_tool_env()
    cwd = None
    if scoped or "MEYO_WORKDIR" in env:
        from personal_runtime import model_tool_workspace
        try:
            cwd = model_tool_workspace(env, require_bound=True)
            info = cwd.stat()
            env.setdefault("MEYO_MODEL_WORKSPACE_ID", json.dumps([info.st_dev, info.st_ino]))
            env["MEYO_WORKDIR"] = str(cwd)
            if not os.path.isabs(image_path):
                image_path = os.path.join(cwd, image_path)
        except (OSError, ValueError, TypeError):
            return {"ok": False, "error": "media_image_workspace_invalid"}
    # Bound calls let the CLI do the only image read and return fixed errors;
    # keep the historical operator-only existence check for an unbound CLI.
    if cwd is None and not os.path.isfile(image_path):
        return {"ok": False, "error": f"图片不存在: {image_path}"}
    # Only the known bundled implementation has this JSON contract. An
    # overridden legacy CLI is called once with its original text arguments.
    bundled = os.path.realpath(cli) == os.path.realpath(_CALL_MODEL)
    command = [sys.executable, cli, "chat", question,
               "--domain", "vision_understanding", "--image", image_path,
               "--timeout", str(int(timeout_s))]
    if bundled:
        command.append("--json")
    try:
        r = subprocess.run(command,
                           capture_output=True, text=True, timeout=timeout_s + 15, env=env,
                           **({"cwd": cwd} if cwd is not None else {}))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"识图超时({timeout_s}s)"}
    ans = (r.stdout or "").strip()
    if ans and (r.returncode == 0 or bundled):
        metadata = {"configured_model": None, "reported_model": None, "provider": None,
                    "request_id": None, "cost": None, "cost_source": None, "usage": None,
                    "usage_scope": None, "finish_reason": None, "max_tokens": None,
                    "input_image": None,
                    "metadata_source": "legacy_cli_text"}
        if bundled:
            try:
                data = json.loads(ans)
            except (ValueError, TypeError):
                data = None
            if (not isinstance(data, dict) or data.get("ok") is not (r.returncode == 0)
                    or (r.returncode == 0 and (
                        not isinstance(data.get("answer"), str) or not data["answer"].strip()))):
                return {"ok": False, "error": (_safe_media_error(r.stderr) if r.returncode != 0
                                              else "") or "media_tool_invalid_response"}
            from media_model_cli import _usage, _public_text, _amount
            if r.returncode == 0:
                ans = data["answer"]
            # Project only the existing filtered result, not arbitrary response
            # fields or a second catalog lookup that could guess another model.
            for field in ("configured_model", "reported_model", "provider", "request_id"):
                metadata[field] = _public_text(data.get(field))
            image_receipt = data.get("input_image")
            if (isinstance(image_receipt, dict)
                    and isinstance(image_receipt.get("sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", image_receipt["sha256"])
                    and type(image_receipt.get("bytes")) is int and image_receipt["bytes"] > 0
                    and isinstance(image_receipt.get("mime_type"), str)
                    and image_receipt["mime_type"] in
                    ("image/png", "image/jpeg", "image/gif", "image/webp")):
                metadata["input_image"] = {key: image_receipt[key]
                                           for key in ("sha256", "bytes", "mime_type")}
            metadata["cost"] = _amount(data.get("cost"))
            metadata["cost_source"] = (data.get("cost_source") if data.get("cost_source")
                                       in ("response.cost", "usage.cost") else None)
            reason = data.get("finish_reason")
            if reason is not None and (
                    not isinstance(reason, str) or reason not in
                    ("stop", "length", "tool_calls", "function_call", "content_filter")):
                return {"ok": False, "error": "media_tool_invalid_response"}
            metadata.update(
                usage=_usage(data.get("usage")), finish_reason=reason,
                usage_scope=("single_model_tool_request"
                             if data.get("usage_scope") == "single_model_tool_request" else None),
                max_tokens=(data["max_tokens"] if type(data.get("max_tokens")) is int
                            and data["max_tokens"] > 0 else None),
                metadata_source="bundled_cli_json")
            if r.returncode != 0:
                # A failed bundled call can still report billed usage. Keep
                # only the same safe facts, never its partial answer/stdout.
                return {"ok": False, "error": _safe_media_error(r.stderr) or "media_tool_invalid_response",
                        "model": "vision_understanding", **metadata}
        return {"ok": True, "answer": ans, "model": "vision_understanding", **metadata}
    return {"ok": False, "error": _safe_media_error(r.stderr) or f"识图未产出有效答案(CLI退出码={r.returncode})"}


# ── PDF 解析:本地抽文本(长 PDF 分块) ──
def _pdf_text_via_pdftotext(path: str) -> str:
    try:
        r = subprocess.run(["pdftotext", "-layout", path, "-"],
                           capture_output=True, text=True, timeout=120)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _pdf_text_via_lib(path: str) -> str:
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                parts.append(pg.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        pass
    try:
        import pypdf
        parts = []
        for pg in pypdf.PdfReader(path).pages:
            parts.append(pg.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


# ── 确定性网页抓取(全军团共享)──────────────────────────────────────────
# 由 dogfood 真实发现驱动:各家 CLI(cc/codex…)自带的 WebFetch 抓 mp.weixin.qq.com 会
# 撞上企业网限制 / 微信反爬(返回验证页/空页),各家又不一定想到"换移动端 UA + 直连"这招,
# 就摆烂说"我会去做/已完成"却没真内容。给全军团一个确定性抓取工具:不走 AIGC 网关、不靠
# 浏览器桥,纯 stdlib 直连 + 智能 UA(微信自动切移动端微信 UA),正文提取。任何一家需要抓
# 某个具体 URL 时都能 `media_tools.py fetch_url "URL"` 真拿到内容,而不是各自碰运气。
# 关键:抓到验证页/空页时如实返回 error(不假装成功),模型看到失败会换招——给 context 不加 control。

_UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UA_WECHAT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.32")


def _pick_ua(url: str) -> str:
    """微信公众号文章 → 移动端微信 UA(实测能绕反爬拿到 js_content);其余 → 桌面 Chrome UA。"""
    return _UA_WECHAT if "mp.weixin.qq.com" in url else _UA_DESKTOP


def _extract_main_text(url: str, raw: str) -> tuple[str, str]:
    """从 HTML 提取 (标题, 正文纯文本)。微信优先 js_content,其余退 article/main/body。"""
    m = (re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', raw)
         or re.search(r"<title[^>]*>(.*?)</title>", raw, re.S))
    title = html.unescape(m.group(1)).strip() if m else ""
    body_html = ""
    if "mp.weixin.qq.com" in url:
        b = re.search(r'id=["\']js_content["\'][^>]*>(.*?)</div>\s*<script', raw, re.S)
        if b:
            body_html = b.group(1)
    if not body_html:
        for pat in (r"<article[^>]*>(.*?)</article>", r"<main[^>]*>(.*?)</main>",
                    r"<body[^>]*>(.*?)</body>"):
            b = re.search(pat, raw, re.S)
            if b:
                body_html = b.group(1)
                break
    body_html = body_html or raw
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body_html, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(re.sub(r"[ \t]+", " ", txt))
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt).strip()
    return title, txt


def fetch_url(url: str, timeout_s: int = 25) -> dict:
    """抓任意网页 URL 的正文(确定性·不走 AIGC 网关·微信自动切移动端 UA)。

    返回 {ok, url, title, text, chars, method, status_code, error}。
    抓到验证页/空正文时 ok=False 且带明确 error(如实报失败,不假装成功)。
    """
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "url": url, "error": f"非法 URL(需 http/https): {url!r}"}
    ua = _pick_ua(url)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", 200)
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, "ignore")
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "status_code": e.code,
                "error": f"HTTP {e.code}:{e.reason}"}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    # 微信反爬/验证页识别:如实返回失败,让调用方(模型)知道要换招,不假装抓到了。
    if any(sig in raw for sig in ("环境异常", "去验证", "weixin110", "请在微信客户端打开")):
        return {"ok": False, "url": url, "status_code": code,
                "error": "命中微信验证/反爬页(未拿到正文)·可重试或换来源"}
    title, text = _extract_main_text(url, raw)
    if len(text) < 80:
        return {"ok": False, "url": url, "status_code": code, "title": title,
                "text": text, "chars": len(text),
                "error": "正文过短(可能是空页/需登录/JS 渲染页)·换方式或来源"}
    return {"ok": True, "url": url, "title": title, "text": text[:200000],
            "chars": len(text), "method": f"stdlib+{'wechat-ua' if 'mp.weixin.qq.com' in url else 'desktop-ua'}",
            "status_code": code}


def read_pdf(pdf_path: str, summarize: bool = False, timeout_s: int = 120) -> dict:
    """解析 PDF。summarize=True 时对长 PDF 分块喂模型出一页纸摘要。

    返回 {ok, text, chars, pages_est, summary?, error}。
    """
    if not os.path.isfile(pdf_path):
        return {"ok": False, "error": f"PDF 不存在: {pdf_path}"}
    text = _pdf_text_via_pdftotext(pdf_path) or _pdf_text_via_lib(pdf_path)
    if not text:
        return {"ok": False, "error": "PDF 无可抽取文本(可能是扫描件·需 OCR)"}
    pages_est = max(1, text.count("\f") + 1)
    result = {"ok": True, "text": text[:200000], "chars": len(text), "pages_est": pages_est}
    cli = _call_model_path()
    if summarize and os.path.isfile(cli):
        # 超长 PDF:分块摘要 → 汇总(map-reduce),避免一次撑爆上下文
        chunks = [text[i:i + 12000] for i in range(0, min(len(text), 120000), 12000)]
        partials = []
        for idx, ck in enumerate(chunks[:10]):
            try:
                r = subprocess.run([sys.executable, cli, "chat",
                                    f"这是长文档第{idx+1}块,提炼要点(中文,≤6条):\n\n{ck}",
                                    "--domain", "programming", "--timeout", str(int(timeout_s))],
                                   capture_output=True, text=True, timeout=timeout_s, env=_gw_env())
                if r.returncode == 0 and r.stdout.strip():
                    partials.append(r.stdout.strip())
            except Exception:
                continue
        if partials:
            joined = "\n".join(partials)
            try:
                r = subprocess.run([sys.executable, cli, "chat",
                                    f"把这些分块要点汇总成一页纸摘要(中文·结构化):\n\n{joined[:20000]}",
                                    "--domain", "programming", "--timeout", str(int(timeout_s))],
                                   capture_output=True, text=True, timeout=timeout_s, env=_gw_env())
                result["summary"] = ((r.stdout or "").strip() if r.returncode == 0 else joined)[:8000]
            except Exception:
                result["summary"] = joined[:8000]
    return result


# ── 视频/音频剪辑:本地 ffmpeg(不依赖网关·真能做) ──
#   命令模式抄 OpenMontage tools/video/video_trimmer.py:117-130 的真实现(-ss/-to/-c copy)
import shutil as _shutil


def _has_ffmpeg() -> bool:
    return bool(_shutil.which("ffmpeg"))


def trim_media(input_path: str, start_s: float = 0, end_s: float = 0,
               timeout_s: int = 300) -> dict:
    """剪辑视频/音频的一段(start_s → end_s 秒)。end_s<=0 表示到结尾。返回 {ok,path,url}。"""
    if not _has_ffmpeg():
        return {"ok": False, "error": "本机无 ffmpeg(brew install ffmpeg)"}
    if not os.path.isfile(input_path):
        return {"ok": False, "error": f"输入文件不存在: {input_path}"}
    ext = os.path.splitext(input_path)[1].lstrip(".") or "mp4"
    out = _new_path(ext)
    # -ss 放 -i 前(input seeking):快、且能保住全部流(视频+音频)。
    #   OpenMontage 把 -ss 放 -i 后 + -c copy,对某些素材会丢视频轨(实测合成视频复现),这里更鲁棒。
    dur = None
    if end_s and end_s > start_s:
        dur = end_s - start_s
    cmd = ["ffmpeg", "-y", "-ss", str(start_s), "-i", input_path]
    if dur:
        cmd += ["-t", str(dur)]
    cmd += ["-c", "copy", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        # -c copy 在无关键帧对齐时可能产出坏流:透明回退到重编码(慢但一定对)。
        need_reencode = not (os.path.isfile(out) and os.path.getsize(out) > 100)
        if not need_reencode:
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                                    "-show_entries", "stream=codec_type", "-of", "csv=p=0", out],
                                   capture_output=True, text=True, timeout=30)
            if "video" not in (probe.stdout or ""):  # 视频轨丢了 → 重编码
                need_reencode = True
        if need_reencode:
            cmd2 = ["ffmpeg", "-y", "-ss", str(start_s), "-i", input_path]
            if dur:
                cmd2 += ["-t", str(dur)]
            cmd2 += ["-c:v", "libx264", "-c:a", "aac", out]
            r = subprocess.run(cmd2, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"剪辑超时({timeout_s}s)"}
    if os.path.isfile(out) and os.path.getsize(out) > 100:
        return {"ok": True, "path": out, "url": _rel_url(out), "bytes": os.path.getsize(out)}
    return {"ok": False, "error": (r.stderr or "剪辑失败")[-300:]}


def extract_audio(video_path: str, timeout_s: int = 300) -> dict:
    """从视频抽出音轨(→ mp3)。返回 {ok,path,url}。

    静默视频不是"ffmpeg坏了":先用 ffprobe 看有没有 audio stream,没有就给模型
    清晰的结构化现场"视频无音轨"——它据此自己换招(配音/换素材),而不是看到300字符
    ffmpeg rc1日志一头雾水。给 context 不加 control。
    """
    if not _has_ffmpeg():
        return {"ok": False, "error": "本机无 ffmpeg"}
    if not os.path.isfile(video_path):
        return {"ok": False, "error": f"视频不存在: {video_path}"}
    # ffprobe 与 ffmpeg 同套安装;若可用就先确定性检查音轨(无则不跑注定失败的ffmpeg)。
    if _shutil.which("ffprobe"):
        try:
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path,
            ], capture_output=True, text=True, timeout=min(int(timeout_s), 30))
            if not (probe.stdout or "").strip():
                return {"ok": False, "error": "视频没有音轨(静默视频)·无法提取音频;请换有声音的视频或先生成/添加配音"}
        except (subprocess.TimeoutExpired, OSError):
            pass  # probe本身失败就退原ffmpeg流程·不误判
    out = _new_path("mp3")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn",
                            "-acodec", "libmp3lame", "-q:a", "2", out],
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"抽音频超时({timeout_s}s)"}
    if os.path.isfile(out) and os.path.getsize(out) > 100:
        return {"ok": True, "path": out, "url": _rel_url(out), "bytes": os.path.getsize(out)}
    return {"ok": False, "error": (r.stderr or "抽音频失败")[-300:]}


def video_frame(video_path: str, at_s: float = 1.0, timeout_s: int = 120) -> dict:
    """抽视频某一帧成图片(→ png),可再喂 see_image 让军团"看视频"。返回 {ok,path,url}。"""
    if not _has_ffmpeg():
        return {"ok": False, "error": "本机无 ffmpeg"}
    if not os.path.isfile(video_path):
        return {"ok": False, "error": f"视频不存在: {video_path}"}
    out = _new_path("png")
    fallback = False
    try:
        r = subprocess.run(["ffmpeg", "-y", "-ss", str(at_s), "-i", video_path,
                            "-frames:v", "1", out],
                           capture_output=True, text=True, timeout=timeout_s)
        # 短片段/流复制片段在指定时间可能无关键帧:透明回退到首帧,别静默失败。
        if not (os.path.isfile(out) and os.path.getsize(out) > 100) and float(at_s or 0) > 0:
            fallback = True
            r = subprocess.run(["ffmpeg", "-y", "-i", video_path, "-frames:v", "1", out],
                               capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"抽帧超时({timeout_s}s)"}
    if os.path.isfile(out) and os.path.getsize(out) > 100:
        return {"ok": True, "path": out, "url": _rel_url(out), "bytes": os.path.getsize(out),
                "at_s": 0 if fallback else at_s,
                "note": "指定时间无帧·已透明回退首帧" if fallback else ""}
    return {"ok": False, "error": (r.stderr or "抽帧失败")[-300:]}


# ── 视频生成:接 OpenMontage 自动发现/selector(不手选provider·军团自己看现场选) ──
_OPENMONTAGE_SOURCE_IDENTITY = None
_OPENMONTAGE_SOURCE_LOCK = threading.RLock()


def _openmontage_dir() -> str:
    from install_registry import openmontage_source_status
    state = openmontage_source_status()
    if not state["ok"]:
        raise RuntimeError(state["error"])
    return state["source"]


def _load_video_registry(om: str):
    """加载OpenMontage registry，并注册Legion随包本地fallback provider。

    OpenMontage自有provider仍由registry.ensure_discovered()自动发现；本地fallback随legion发布，
    云provider全不可用时selector仍能真产image_to_video。
    """
    global _OPENMONTAGE_SOURCE_IDENTITY
    from install_registry import openmontage_source_status
    with _OPENMONTAGE_SOURCE_LOCK:
        fact = openmontage_source_status()
        if not fact["ok"]:
            raise RuntimeError(fact["error"])
        source = Path(om).resolve()
        if str(source) != fact["source"]:
            raise RuntimeError("OpenMontage Python source 已变化；请重启使用当前源码的进程")
        identity = (str(source), fact["revision"], fact["source_digest"])
        cached = False
        # sys.path 不会替换已导入的 tools/lib；不同根、版本或关键内容均拒绝，不删除模块。
        for name, module in list(sys.modules.items()):
            if name not in ("tools", "lib") and not name.startswith(("tools.", "lib.")):
                continue
            cached = True
            locations = [getattr(module, "__file__", None), *getattr(module, "__path__", [])]
            locations = [Path(p).resolve() for p in locations if p]
            expected = source / name.split(".")[0]
            if not locations or any(not p.is_relative_to(expected) for p in locations):
                raise RuntimeError(f"OpenMontage Python source 冲突({name})；请重启使用当前源码的进程")
        if (_OPENMONTAGE_SOURCE_IDENTITY is not None or cached) and _OPENMONTAGE_SOURCE_IDENTITY != identity:
            raise RuntimeError("OpenMontage Python source 身份变化或缓存身份未知；请重启使用当前源码的进程")
        # 导入失败也可能留下 tools/lib 的部分模块，不能在下次调用把它们认成另一个版本。
        _OPENMONTAGE_SOURCE_IDENTITY = identity
        # 当前源码必须先于其他搜索路径；调用方在 finally 中恢复 sys.path。
        sys.path.insert(0, str(source))
        from tools.tool_registry import registry
        registry.ensure_discovered()
        # 插件式注册(不要求修改/发布嵌套OpenMontage仓库)
        from openmontage_local_provider import LegionStillMotionVideo
        if registry.get(LegionStillMotionVideo.name) is None:
            registry.register(LegionStillMotionVideo())
        return registry


def video_provider_status() -> dict:
    """自动发现 OpenMontage 所有 video_generation providers 的实时状态/能力。"""
    old = list(sys.path)
    try:
        om = _openmontage_dir()
        registry = _load_video_registry(om)
        providers = []
        for t in registry.get_by_capability("video_generation"):
            if t.name == "video_selector":
                continue
            try:
                status = t.get_status().value
            except Exception as e:
                status = "error"
            providers.append({"name": t.name, "provider": getattr(t, "provider", ""),
                              "status": status, "capabilities": list(getattr(t, "capabilities", []) or []),
                              "dependencies": list(getattr(t, "dependencies", []) or [])})
        return {"ok": True, "providers": providers,
                "available": [p for p in providers if p["status"] == "available"]}
    except Exception as e:
        return {"ok": False, "error": f"OpenMontage发现失败:{type(e).__name__}:{e}", "providers": []}
    finally:
        sys.path[:] = old


# ── Seedance 方舟网关通道(2026-09-07 沙丘假视频根修·真AI视频) ──
# 实测铁律(2026-09-07 全24 appid扫描+成功出片):
# ① chat端点(/v1/openai/native/chat/completions)对视频模型必报“bound must be positive”——
#    视频生成必须走方舟异步任务端点(创建→轮询→video_url→下载);
# ② 扫描中仅两个优先授权有seedance配额(其余首请求即429,等65s也429=配额≈0);
# ③ 全程约3-5分钟出片,5秒720p;④429=该appid此模型分钟配额0,冷却换appid。
_FANGZHOU_TASKS = "https://aigc.sankuai.com/v1/fangzhou/v3/contents/generations/tasks"
_SEEDANCE_MODEL = "doubao-seedance-2-0-260128"
# 实测有配额的appid优先;其余从models.json trusted_appids里动态读(未来配额变化自动适配)
_SEEDANCE_PREF_APPIDS = ['appid-fbd7ed06af', 'appid-710d24d372']
_SEEDANCE_COOLDOWN: dict = {}          # appid -> 冷却到期时间戳(限流控制)
_SEEDANCE_CREATE_LOCK = threading.Lock()  # 同一进程内串行创建任务,防连发撞429


def _seedance_appids() -> list:
    """显式env覆盖优先池;默认实测顺序+trusted,仅返回已解析且未耗尽的授权。"""
    import model_registry

    db = model_registry._load_models()
    exhausted = {str(a).strip() for a in
                 (db.get("quota_exhausted_appids") or {}).get("appids", [])}
    exhausted.update(r for a in tuple(exhausted)
                     if (r := model_registry._resolve_alias(a)))
    explicit = os.environ.get("SEEDANCE_APPIDS")
    if explicit is not None:
        candidates = explicit.split(",")
    else:
        candidates = list(_SEEDANCE_PREF_APPIDS)
        candidates.extend(x.get("appid") for x in
                          (db.get("trusted_appids") or {}).get("list", []))
    ids = []
    for candidate in candidates:
        alias = str(candidate or "").strip()
        if not alias or alias in exhausted:
            continue
        appid = model_registry._resolve_alias(alias)
        if appid and not appid.startswith("appid-") and appid not in exhausted and appid not in ids:
            ids.append(appid)
    return ids


def _seedance_pick_appid() -> str:
    """选一个不在冷却期的appid;全冷却则选最早到期的。"""
    ids = _seedance_appids()
    if not ids:
        raise ValueError("Seedance无可用授权:请检查MEYO_APPID_ALIASES、SEEDANCE_APPIDS及配额配置")
    now = time.time()
    for a in ids:
        if _SEEDANCE_COOLDOWN.get(a, 0) <= now:
            return a
    return min(ids, key=lambda a: _SEEDANCE_COOLDOWN[a])


def _seedance_create(prompt: str, duration: str, ratio: str, timeout_s: int) -> dict:
    """创建方舟视频任务;429→冷却65s换appid重试(限流控制)。"""
    deadline = time.time() + max(90, min(timeout_s, 600))
    last_err = ""
    for _ in range(8):
        try:
            appid = _seedance_pick_appid()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        body = json.dumps({
            "model": _SEEDANCE_MODEL,
            "content": [{"type": "text", "text": str(prompt or "").strip()[:4000]}],
            "duration": int(float(duration or 5)), "ratio": ratio or "16:9",
            "watermark": False}).encode()
        req = urllib.request.Request(_FANGZHOU_TASKS, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {appid}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read())
                if d.get("id"):
                    return {"ok": True, "task_id": d["id"], "appid": appid}
                last_err = f"创建无id:{str(d)[:200]}"
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            last_err = f"HTTP{e.code}:{detail}"
            if e.code == 429:
                # 限流控制:该appid冷却65s(一个完整分钟窗口),下轮自动换下一个
                _SEEDANCE_COOLDOWN[appid] = time.time() + 65
        except Exception as e:
            last_err = f"{type(e).__name__}:{e}"
        if time.time() >= deadline:
            break
        time.sleep(min(30, max(3, deadline - time.time())))
    return {"ok": False, "error": f"seedance任务创建失败:{last_err}"}


def _bounded_media_process(command, timeout_s, input_text=None, *, cancel_check=None, env=None,
                           cwd=None, term_grace_s=3.0, diagnostics=None):
    """有限操作；可选单次调用字典保留 stdout/stderr 尾部，不改变二元返回。"""
    import math
    import signal
    import legion_proc_registry as _preg
    term_grace_s = float(term_grace_s)
    if not math.isfinite(term_grace_s) or term_grace_s < 0:
        raise ValueError("media_term_grace_invalid")
    if diagnostics is not None:
        if type(diagnostics) is not dict:
            raise TypeError("media_diagnostics_must_be_dict")
        diagnostics.update(stdout_tail="", stdout_truncated=False,
                           stderr_tail="", stderr_truncated=False)

    def remember_output(stdout, stderr):
        if diagnostics is not None:
            for stream, value in (("stdout", stdout), ("stderr", stderr)):
                if value is not None:
                    if isinstance(value, bytes):  # TimeoutExpired 可携带尚未解码的输出。
                        value = value.decode("utf-8", errors="replace")
                    diagnostics[f"{stream}_tail"] = value[-8192:]
                    diagnostics[f"{stream}_truncated"] = len(value) > 8192

    if input_text is not None:
        input_text.encode("utf-8")  # 编码失败必须发生在启动子进程之前。
    if callable(cancel_check) and cancel_check():
        raise InterruptedError("media_call_cancelled")
    child_env = dict(env) if env is not None else _gw_env()
    with _preg.registry_directory_scope(child_env.get("LEGION_PROC_REGISTRY_DIR")) as directory:
        # 仅传给本次子进程，也让未显式指定目录的嵌套调用继承同一本账。
        child_env["LEGION_PROC_REGISTRY_DIR"] = directory
        failed = False
        proc = subprocess.Popen(command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, env=child_env, cwd=cwd)
        try:
            if not _preg.register(proc.pid, owner=os.getpid(), comm=command[0],
                                  meta={"kind": "media_operation"}):
                raise RuntimeError("media_process_registration_failed")
            deadline = time.monotonic() + timeout_s
            first = True
            while True:
                if callable(cancel_check) and cancel_check():
                    raise InterruptedError("media_call_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_s)
                try:
                    stdout, stderr = proc.communicate(
                        input=input_text if first else None,
                        timeout=min(0.25, remaining) if callable(cancel_check) else remaining)
                    remember_output(stdout, stderr)
                    return proc.returncode, stdout
                except subprocess.TimeoutExpired as exc:
                    remember_output(exc.output, exc.stderr)
                    first = False
                    if not callable(cancel_check):
                        raise
        except BaseException:
            failed = True
            # 先让 wrapper 回收自己另建的进程组；直接 KILL 外层会跳过它的 finally。
            grace_deadline = time.monotonic() + term_grace_s
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except BaseException:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=term_grace_s)
                remember_output(stdout, stderr)
            except BaseException as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    remember_output(exc.output, exc.stderr)
                # communicate 本身可能解码/读管道失败，仍留足剩余清理宽限。
                try:
                    proc.wait(timeout=max(0, grace_deadline - time.monotonic()))
                except BaseException:
                    pass
            # 即使 wrapper 已退出，也回收同组中可能仍存活的后代。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except BaseException:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
                remember_output(stdout, stderr)
            except BaseException as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    remember_output(exc.output, exc.stderr)
                try:
                    proc.wait(timeout=2)
                except BaseException:
                    pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except BaseException:
                    pass
            raise
        finally:
            try:
                try:
                    # wrapper 正常退出或被硬杀都要收其登记的独立组，且必须用原目录。
                    try:
                        _preg.kill_owned_by(proc.pid, strict=True)
                    except Exception as exc:
                        raise RuntimeError("media_process_cleanup_failed") from exc
                finally:
                    if proc.poll() is not None:
                        _preg.unregister(proc.pid)
            except BaseException:
                if not failed:
                    raise  # 不掩盖原始工具异常；回收失败的活子组保留登记。


def run_shared_model_tool(tool_id, params):
    """复用模型 CLI 和既有进程回收；目录不联网，生成可取消且有总等待上限。"""
    import math
    import media_model_cli
    try:
        params = media_model_cli.validate_model_tool(tool_id, params)
        registry = sys.modules.get("harness_registry")
        local = getattr(registry, "_INPROC_RUNTIME_ENV", None)
        bound = dict(getattr(local, "values", {}))
        if (tool_id == "model_catalog" and "profile_id" not in params
                and getattr(local, "inherit_process_context", True)
                and not any(field in bound for field in _MODEL_ENV_FIELDS)):
            return media_model_cli.run_model_tool(tool_id, params)
        env, scoped = _model_tool_env()
        cwd = None
        if "profile_id" in params:
            from personal_runtime import model_profile_context, ModelProfileError
            try:
                _, cwd = model_profile_context(env)
            except ModelProfileError as exc:
                raise media_model_cli.MediaError(str(exc)) from None
            # The child resolves the selected key from its private source. It
            # neither needs nor may borrow another provider's environment.
            for key in ("MEYO_MODEL_CATALOG_PATH", "MEYO_MODEL_MATRIX_PATH", "MEYO_APPID_ALIASES",
                        "MEYO_MEDIA_GATEWAY", "MEYO_UNIFIED_GATEWAY", "MEYO_UNIFIED_APPID",
                        "AIGC_APPID", "MEYO_PERSONAL_PROVIDER_KEY", "LEGION_CODEX_RESPONSES_BINDING",
                        "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(key, None)
        elif params.get("image") or "MEYO_WORKDIR" in env:
            from personal_runtime import model_tool_workspace
            try:
                cwd = model_tool_workspace(env, require_bound=scoped)
            except (OSError, ValueError, TypeError):
                raise media_model_cli.MediaError("media_image_workspace_invalid") from None
        if cwd is not None:
            info = cwd.stat()
            env.setdefault("MEYO_MODEL_WORKSPACE_ID", json.dumps([info.st_dev, info.st_ino]))
        timeout = params.get("timeout_s", 90)
        deadline = float(env.get("MEYO_TASK_DEADLINE") or 0)
        if not math.isfinite(deadline) or deadline < 0:
            raise media_model_cli.MediaError("media_timeout_invalid")
        if deadline:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired([], timeout)
        if tool_id in ("model_chat", "model_decisions"):
            params["timeout_s"] = timeout
        check = getattr(getattr(registry, "_INPROC_CANCEL_CHECK", None), "check", None)
        # prompt 经 stdin 传输，不将请求正文或秘密放进进程命令行。
        code, text = _bounded_media_process(
            [sys.executable, _CALL_MODEL, "tool", tool_id], timeout,
            json.dumps(params, ensure_ascii=False), cancel_check=check, env=env, cwd=cwd)
        result = json.loads(text)
        if not isinstance(result, dict) or (code != 0 and result.get("ok")):
            raise media_model_cli.MediaError("media_tool_invalid_response")
        return result
    except media_model_cli.MediaError as exc:
        error = str(exc)
    except subprocess.TimeoutExpired:
        error = "media_call_timeout"
    except InterruptedError:
        error = "media_call_cancelled"
    except Exception as exc:
        error = f"media_call_error:{type(exc).__name__}"
    return {"ok": False, "isError": True, "error": error}


def _download_generated_video(url: str, timeout_s: float) -> dict:
    """只报告下载与媒体协议事实；云端 succeeded 不等于已下载到可读的视频。"""
    import hashlib
    import tempfile
    partial = ""
    observation = {}
    try:
        _ensure_dir()
        with tempfile.NamedTemporaryFile(dir=_MEDIA_DIR, prefix=".download-",
                                         suffix=".partial", delete=False) as output:
            partial = output.name
        # URL经stdin传递，不进入进程参数或错误文本。stdlib独立进程避免线程内无法
        # 打断的read/readline：持续滴字节也不能绕过此工具的总下载预算。
        download = r'''
import json,sys,urllib.request,urllib.error
r=json.load(sys.stdin); observation={}
try:
    with urllib.request.urlopen(r["url"],timeout=min(60,r["timeout"])) as response:
        content_type=response.headers.get("Content-Type","").split(";",1)[0].lower()
        observation={"http_status":response.getcode(),"content_type":content_type}
        if content_type.startswith("text/") or content_type in {
            "application/json","application/xml","application/xhtml+xml"}:
            result={"ok":False,"error":"video_download_unexpected_content"}
        else:
            size=0
            with open(r["partial"],"wb") as output:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    output.write(chunk);size+=len(chunk)
            observation["bytes_received"]=size
            expected=response.headers.get("Content-Length")
            result=({"ok":False,"error":"video_download_incomplete"} if
                    expected and size!=int(expected) else {"ok":True})
except urllib.error.HTTPError as exc:
    result={"ok":False,"error":f"video_download_http_{exc.code}"}
except Exception as exc:
    result={"ok":False,"error":"video_download_failed","exception_type":type(exc).__name__}
print(json.dumps({**result,"download_observation":observation}))
'''
        try:
            rc, stdout = _bounded_media_process(
                [sys.executable, "-I", "-c", download], max(0.1, timeout_s),
                json.dumps({"url": url, "partial": partial, "timeout": max(0.1, timeout_s)}))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "video_download_timeout",
                    "download_observation": observation}
        result = json.loads(stdout) if rc == 0 else {"ok": False, "error": "video_download_worker_failed"}
        observation.update(result.get("download_observation") or {})
        if not result.get("ok"):
            return {**result, "download_observation": observation}
        size = os.path.getsize(partial)
        probe = _shutil.which("ffprobe")
        decoder = _shutil.which("ffmpeg") if not probe else None
        if not probe and not decoder:
            return {"ok": False, "error": "video_probe_unavailable",
                    "download_observation": observation}
        # 不相信扩展名或MIME；优先容器探测，没有ffprobe时用现有ffmpeg真解码视频帧。
        # 后者不是省略检查，也不把技术可读性当内容/创作/完整播放验收。
        try:
            if probe:
                rc, stdout = _bounded_media_process(
                    [probe, "-v", "error", "-protocol_whitelist", "file,pipe",
                     "-show_entries", "stream=codec_type,codec_name,width,height:format=duration,format_name",
                     "-of", "json", partial], 15)
                facts = json.loads(stdout or "{}") if rc == 0 else {}
                videos = [s for s in facts.get("streams", []) if s.get("codec_type") == "video"]
                formats = str(facts.get("format", {}).get("format_name", "")).split(",")
                readable = bool(videos and "mp4" in formats)
                validation = {"tool": "ffprobe", "video_stream_observed": bool(videos)}
            else:
                rc, stdout = _bounded_media_process(
                    [decoder, "-v", "error", "-xerror", "-err_detect", "explode",
                     "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", partial,
                     "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
                     "-progress", "pipe:1", "-nostats"], 15)
                frames = max((int(value.strip()) for line in stdout.splitlines()
                              for name, sep, value in [line.partition("=")]
                              if sep and name.strip() == "frame" and value.strip().isdigit()), default=0)
                readable = rc == 0 and frames >= 1
                videos = [{"codec_type": "video", "decoded_frames": frames}] if readable else []
                facts = {"format": {"decoder_input_format": "mov"}}
                validation = {"tool": "ffmpeg", "decoded_video_frames": frames,
                              "full_playback_checked": False}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "video_probe_timeout",
                    "download_observation": observation}
        if not readable:
            return {"ok": False, "error": "video_download_invalid_media",
                    "download_observation": observation}
        with open(partial, "rb") as content:
            digest = hashlib.file_digest(content, "sha256")
        out = _new_path("mp4")
        os.replace(partial, out)
        partial = ""
        return {"ok": True, "path": out, "url": _rel_url(out), "bytes": size,
                "sha256": digest.hexdigest(), "download_observation": observation,
                "media_observation": {"streams": videos, "format": facts.get("format", {}),
                                      "validation": validation,
                                      "playback_reviewed": False}}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"video_download_http_{exc.code}",
                "download_observation": observation}
    except Exception as exc:
        return {"ok": False, "error": "video_download_failed",
                "exception_type": type(exc).__name__,
                "download_observation": observation}
    finally:
        if partial:
            try:
                os.unlink(partial)
            except OSError as exc:
                # 不让清理失败盖掉下载事实和原task_id；临时文件从未被发布。
                observation["partial_cleanup_error"] = type(exc).__name__


def _seedance_wait(task_id: str, appid: str, timeout_s: int) -> dict:
    """轮询任务到succeeded,下载mp4到军团media目录。约3-5分钟出片。"""
    deadline = time.time() + max(180, timeout_s)
    url = f"{_FANGZHOU_TASKS}/{task_id}"
    while time.time() < deadline:
        time.sleep(6)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {appid}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):  # 查询瞬时限流/抖动→退避重试
                time.sleep(10)
                continue
            return {"ok": False, "error": f"轮询HTTP{e.code}"}
        except Exception:
            time.sleep(8)
            continue
        st = d.get("status")
        if st == "succeeded":
            vurl = (d.get("content") or {}).get("video_url") or ""
            if not vurl:
                return {"ok": False, "error": f"succeeded但无video_url:{str(d)[:300]}"}
            downloaded = _download_generated_video(vurl, max(1, deadline - time.time()))
            return {**downloaded, "provider": "seedance_gateway",
                    "model": _SEEDANCE_MODEL, "task_id": task_id, "remote_status": st,
                    "duration_s": d.get("duration"), "resolution": d.get("resolution")}
        if st in ("failed", "cancelled"):
            return {"ok": False, "error": f"seedance任务{st}:{str(d)[:300]}"}
    return {"ok": False, "error": "seedance任务轮询超时(任务仍在云端跑,可稍后查询)"}


def seedance_gateway_video(prompt: str, duration: str = "5", aspect_ratio: str = "16:9",
                           timeout_s: int = 420) -> dict:
    """真AI文生视频:方舟异步任务通道(创建→轮询→下载)。串行创建+429冷却限流。"""
    if not str(prompt or "").strip():
        return {"ok": False, "error": "prompt为空"}
    with _SEEDANCE_CREATE_LOCK:  # 进程内串行:同一时刻最多创建一个任务
        r = _seedance_create(prompt, duration, aspect_ratio, timeout_s)
    if not r.get("ok"):
        return r
    return _seedance_wait(r["task_id"], r["appid"], timeout_s)


def gen_video(prompt: str, image_path: str = "", duration: str = "5",
              aspect_ratio: str = "16:9", timeout_s: int = 240) -> dict:
    """自动生成视频:文生视频优先走Seedance方舟网关(真AI视频·2026-09-07打通)。

    - 无 image_path → 文生视频:优先 seedance_gateway(真AI·约3-5分钟);失败如实报错并
      给出能力状态,不再用静态图羼充。
    - 有 image_path → image_to_video(方舟参考图需公网URL,本地图暂不支持)→
      OpenMontage VideoSelector:云provider不可用时本地FFmpeg motion兜底
      (降级产物带degraded标记,交付必须如实说明)。
    """
    try:
        om = _openmontage_dir()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    if image_path and not os.path.isfile(image_path):
        return {"ok": False, "error": f"输入图片不存在:{image_path}"}
    # ① 文生视频:真AI通道优先(沙丘假视频根修——不再让“静态图运镜”冒充文生视频)
    if not image_path:
        r = seedance_gateway_video(prompt, duration, aspect_ratio, timeout_s=timeout_s)
        if r.get("ok"):
            return r
        _st = video_provider_status()
        return {"ok": False, "error": f"seedance网关失败:{r.get('error')}",
                "hint": ("真AI文生视频(Seedance方舟通道)本次失败;不要用静态图/运镜动画冒充视频交付,"
                         "如实告知用户并建议重试。"),
                "provider_status": _st}
    old = list(sys.path)
    try:
        _load_video_registry(om)  # 自动发现云provider+注册随包本地fallback
        from tools.video.video_selector import VideoSelector
        output = _new_path("mp4")
        inputs = {"operation": "image_to_video" if image_path else "text_to_video",
                  "prompt": prompt, "duration": str(duration), "aspect_ratio": aspect_ratio,
                  "output_path": output}
        if image_path:
            inputs["reference_image_path"] = image_path
        # 注:纯文生视频已在上方 seedance_gateway 分支提前返回;这里只剩 image_to_video路径。
        start = time.time()
        result = VideoSelector().execute(inputs)
        if result.success:
            data = result.data or {}
            path = data.get("output") or (result.artifacts[0] if result.artifacts else output)
            if path and os.path.isfile(path):
                provider = data.get("selected_provider") or data.get("provider")
                out = {"ok": True, "path": path, "url": _rel_url(path),
                       "bytes": os.path.getsize(path), "duration_s": data.get("duration"),
                       "provider": provider,
                       "selected_tool": data.get("selected_tool"),
                       "selection_reason": data.get("selection_reason"),
                       "elapsed": round(time.time()-start, 2)}
                # 诚实降级标记(2026-09-07 沙丘假视频根修):provider=local_ffmpeg_motion 时
                # 产出只是单帧关键帧运镜动画,不是AI生成视频。不标记会让harness把它当真视频
                # 交付给用户(实测:cc交付"动态样片"→用户看到图片播5秒)。
                # context not control:如实告知降级事实与含义,交付话术由harness自己决定。
                if provider == "local_ffmpeg_motion":
                    out["degraded"] = True
                    out["degraded_note"] = ("云端AI视频通道(Seedance方舟)本次不可用或未走通,"
                                             "本mp4只是输入关键帧的本地运镜动画(缩放/平移),不是AI生成的动态视频。"
                                             "向用户交付时必须如实说明这是动态分镜预览,不可冒充AI视频成品。")
                return out
            return {"ok": False, "error": "provider报告成功但视频文件不存在", "raw": data}
        # 图生视频失败(本地 FFmpeg fallback 也没成)——如实带 provider 状态,不糊弄
        return {"ok": False, "error": result.error or "没有可用视频provider",
                "provider_status": video_provider_status()}
    except Exception as e:
        return {"ok": False, "error": f"OpenMontage执行失败:{type(e).__name__}:{e}",
                "provider_status": video_provider_status()}
    finally:
        sys.path[:] = old


# ── 工具清单(供 legion run_tool 分发 + 灵感墙) ──
MEDIA_TOOLS = {
    "gen_image": {"fn": gen_image, "label": "🎨 文生图",
                  # 描述用"生成/创作/画"这类产出词·不泛用"图片"避免抢"识别图片"这类理解类query
                  # (2026-08-28 L2检索偏差修:让工具如实描述自己是"生成"还是"理解")
                  "desc": "文生图·根据文字提示生成/创作/画一张新图片(gpt-image-2)", "cap": "image_gen",
                  "params": ["prompt", "timeout_s"]},
    "see_image": {"fn": see_image, "label": "👁 识图",
                  # 补齐它真能做的语义词(识别/图片/内容/看图/理解),让"识别图片内容"这类中文query
                  # 经 BM25 正确命中它而非 gen_image。给context(如实描述能力)不加意图词表(control)。
                  "desc": "识别/理解图片内容·看图并回答关于这张图片的问题(视觉理解·识图看图)", "cap": "vision",
                  "params": ["image_path", "question", "timeout_s"]},
    "read_pdf": {"fn": read_pdf, "label": "📄 读PDF",
                 "desc": "解析PDF·长文档分块出摘要", "cap": "pdf_doc",
                 "params": ["pdf_path", "summarize", "timeout_s"]},
    "fetch_url": {"fn": fetch_url, "label": "🌐 抓网页",
                  "desc": "抓任意网页/微信文章正文(确定性·微信自动切移动UA·绕反爬)",
                  "cap": "web_fetch", "params": ["url", "timeout_s"]},
    "gen_video": {"fn": gen_video, "label": "🎬 生成视频",
                  "desc": "文生视频走Seedance方舟真AI通道(约3-5分钟·限流自动冷却重试);图生视频云不可用回退本地运镜动画(降级产物带degraded标记,交付须如实说明)",
                  "cap": "video_gen",
                  "params": ["prompt", "image_path", "duration", "aspect_ratio", "timeout_s"]},
    "trim_media": {"fn": trim_media, "label": "✂️ 剪辑视频/音频",
                   "desc": "剪出一段(本地ffmpeg·快)", "cap": "video_edit",
                   "params": ["input_path", "start_s", "end_s"]},
    "extract_audio": {"fn": extract_audio, "label": "🎵 视频抽音轨",
                      "desc": "从视频提取音频→mp3", "cap": "audio",
                      "params": ["video_path"]},
    "video_frame": {"fn": video_frame, "label": "🖼 视频抽帧",
                    "desc": "抽某一帧成图·可再识图看视频", "cap": "video_edit",
                    "params": ["video_path", "at_s"]},
}


def run_media_tool(tool_id: str, params: dict) -> dict:
    """军团任意一家把多媒体工具当工具箱调用的统一入口。

    幂等(军团反思⑤·防重复付费):生图/生视频/TTS 等【花钱操作】调用前先查这单(qhash)账本——
    接力家重跑时若上一家已用完全相同参数做过·直接返回上次结果(不重复扣费),否则真调后记账。
    只对花钱操作生效·读类(识图/读PDF/搜索)不记(重做无害)。context not control:模型自主决定调不调·
    这里只在"这单里完全相同的花钱操作刚做过"时把上次结果还给它(事实)·参数变了就是新操作正常花钱。
    """
    spec = MEDIA_TOOLS.get(tool_id)
    if not spec:
        return {"ok": False, "error": f"未知媒体工具: {tool_id}",
                "available": list(MEDIA_TOOLS)}
    kwargs = {k: params[k] for k in spec["params"] if k in params}
    # 参数类型安全强转(用户2026-09-01"查清所有fail原因"·真bug根治):军团/前端/CLI 传来的
    # params 常是 JSON 字符串(如 timeout_s="100"),但 gen_image(timeout_s:int)内部 subprocess
    # timeout=timeout_s 会 float+str 崩→整个生图fail。按函数签名默认值类型把 str 数值参数强转到
    # int/float(所有媒体工具的 timeout_s/duration/at_s/start_s/end_s… 一次性通用生效·不逐个特判)。
    try:
        import inspect as _insp
        _sig = _insp.signature(spec["fn"])
        for _k, _v in list(kwargs.items()):
            _p = _sig.parameters.get(_k)
            if _p is None or not isinstance(_v, str):
                continue
            _d = _p.default
            try:
                if isinstance(_d, bool):
                    kwargs[_k] = _v.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(_d, int):
                    kwargs[_k] = int(float(_v))
                elif isinstance(_d, float):
                    kwargs[_k] = float(_v)
            except (ValueError, TypeError):
                pass  # 转不了(如非数字字符串)保留原值·让 fn 自己报清晰错
    except Exception:
        pass  # 强转任何异常绝不阻断真执行(上游断链铁律)
    if tool_id == "gen_image":
        # This implementation owns the same strong ledger for CLI and runtime
        # callers. A weak cache hit must not bypass its current file checks.
        return _generate_image(kwargs)
    # 幂等命中→直接返回上次结果(省重复付费)。整段兜底:账本任何异常绝不影响真执行(上游断链铁律)。
    try:
        import legion_idempotency as _idem
        _hit = _idem.check(tool_id, kwargs)
        if _hit is not None:
            return _hit
    except Exception:
        _idem = None
    try:
        out = spec["fn"](**kwargs)
    except TypeError as e:
        return {"ok": False, "error": f"参数错误: {e}·需 {spec['params']}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    # 成功的花钱操作记账(下次相同参数命中·不重复扣费)。
    try:
        if _idem is not None:
            _idem.record(tool_id, kwargs, out)
    except Exception:
        pass
    return out


def list_media_tools() -> list:
    return [{"id": tid, "label": s["label"], "desc": s["desc"],
             "cap": s["cap"], "params": s["params"]}
            for tid, s in MEDIA_TOOLS.items()]


if __name__ == "__main__":
    import json as _j
    import sys as _sys
    # CLI 入口:军团各家 harness 直接命令行调共享媒体工具(context-not-control·模型自己决定调)
    #   python media_tools.py gen_image "prompt"
    #   python media_tools.py read_pdf "path"   /  see_image "path" "question"
    #   python media_tools.py --json see_image "path" "question"
    argv = _sys.argv[1:]
    as_json = bool(argv and argv[0] == "--json")
    if as_json:
        argv = argv[1:]
    if argv and argv[0] in MEDIA_TOOLS:
        tool = argv[0]
        args = argv[1:]
        pnames = MEDIA_TOOLS[tool]["params"]
        if any(arg in ("--help", "-h") for arg in args):
            print(f"用法: media_tools.py [--json] {tool} " + " ".join(pnames))
            print(MEDIA_TOOLS[tool]["desc"])
            _sys.exit(0)
        params = {pnames[i]: args[i] for i in range(min(len(args), len(pnames)))}
        out = run_media_tool(tool, params)
        # 给模型看的:按工具语义回它真正要的东西,失败回错误
        if as_json:
            print(_j.dumps(out, ensure_ascii=False))
        elif out.get("ok"):
            if out.get("text"):
                # 抓网页/读PDF:回正文(fetch_url 带标题一起回,模型才知道抓到了啥)
                head = (f"[标题] {out['title']}\n" if out.get("title") else "")
                print(head + out["text"][:12000])
            elif out.get("answer"):
                print(out["answer"])       # 识图:回答案
            elif out.get("url"):
                # 原生成员既要网页引用，也要继续加工/识图的本地 path。
                # 保留工具已经返回的事实，不把现成产物压成不可读的裸 URL。
                print(_j.dumps(out, ensure_ascii=False))
            else:
                print(_j.dumps(out, ensure_ascii=False))
        else:
            print("ERROR: " + str(out.get("error", "failed")))
        _sys.exit(0 if out.get("ok") else 1)
    if as_json and argv:
        print(_j.dumps({"ok": False, "error": "unknown_media_tool"}, ensure_ascii=False))
        _sys.exit(1)
    print(_j.dumps(list_media_tools(), ensure_ascii=False, indent=1))
