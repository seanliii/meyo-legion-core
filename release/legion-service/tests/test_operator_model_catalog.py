"""操作者只读模型目录引用：全部使用临时假配置，不读取个人凭证或调用模型。"""
from __future__ import annotations

import importlib
import builtins
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ENV = "MEYO_MODEL_CATALOG_PATH"


def catalog(model="fixture-vision", credential="fixture-only-not-a-real-key", inputs=None):
    row = {"model": model, "status": "PASS", "appids": [credential]}
    if inputs is not None:
        row["input"] = inputs
    return {"vision_understanding": {"candidates": [row]}}


def write_catalog(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_")):
            monkeypatch.delenv(key)
    for key, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "state"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "processes"),
    ):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("该回归不得连接网络或模型")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    mr = importlib.import_module("model_registry")
    cli = importlib.import_module("media_model_cli")
    media = importlib.import_module("media_tools")
    legacy = tmp_path / "home/.claude/skills/aigc-model-router/models.json"
    # 保留历史测试可 monkeypatch _MODELS_PATH 的接口，也隔离组合运行的 import 缓存。
    monkeypatch.setattr(mr, "_MODELS_PATH", str(legacy))
    monkeypatch.setattr(mr, "_APPID_MATRIX_CACHE", {"data": None})
    monkeypatch.setattr(media, "_MEDIA_DIR", str(tmp_path / "media"))
    return SimpleNamespace(root=tmp_path, mr=mr, cli=cli, media=media,
                           legacy=legacy, patch=monkeypatch)


def test_legacy_path_and_load_models_monkeypatch_remain_compatible(case):
    value = catalog("legacy-fixture")
    write_catalog(case.legacy, value)
    assert case.mr._load_models() == value
    case.patch.setattr(case.mr, "_load_models", lambda: catalog("patched-fixture"))
    assert case.cli._select("vision_understanding")[0] == "patched-fixture"


@pytest.mark.parametrize("reference_kind", ["file", "directory"])
def test_operator_reference_overrides_import_time_home_readonly(case, reference_kind):
    write_catalog(case.legacy, catalog("must-not-borrow"))
    path = write_catalog(case.root / "authorized/models.json",
                         catalog(inputs=["text", "image"]))
    before = path.read_bytes()
    path.chmod(0o400)
    reference = path if reference_kind == "file" else path.parent
    case.patch.setenv(ENV, str(reference))
    assert case.mr._load_models() == json.loads(before)
    assert case.cli._select("vision_understanding")[0] == "fixture-vision"
    assert case.mr.input_modalities("fixture-vision") == ["image", "text"]
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path], "只能读引用，不复制或生成配置"


def test_operator_reference_is_resolved_at_call_time(case):
    first = write_catalog(case.root / "first.json", catalog("first"))
    second = write_catalog(case.root / "second.json", catalog("second"))
    for path, expected in ((first, "first"), (second, "second"), (first, "first")):
        case.patch.setenv(ENV, str(path))
        assert case.cli._select("vision_understanding")[0] == expected


def test_default_does_not_search_another_personal_catalog(case):
    write_catalog(case.root / "home/.agents/skills/aigc-model-router/models.json",
                  catalog("not-explicitly-authorized"))
    assert case.mr._load_models() == {}
    with pytest.raises(case.cli.MediaError, match="^media_direction_unavailable$"):
        case.cli._select("vision_understanding")


@pytest.mark.parametrize("bad", [
    "missing", "empty", "whitespace", "malformed", "array", "null", "empty-directory",
])
def test_unavailable_explicit_reference_fails_closed_without_home_fallback(case, bad):
    write_catalog(case.legacy, catalog("must-not-borrow"))
    path = case.root / "explicit.json"
    if bad == "malformed":
        path.write_text('{"private_fixture": "never-echo-this", invalid')
    elif bad == "array":
        path.write_text("[]")
    elif bad == "null":
        path.write_text("null")
    elif bad == "empty-directory":
        path.mkdir()
    reference = "" if bad == "empty" else "   " if bad == "whitespace" else str(path)
    case.patch.setenv(ENV, reference)
    with pytest.raises(ValueError, match="^model_catalog_unavailable$"):
        case.mr._load_models()
    with pytest.raises(case.cli.MediaError, match="^media_model_catalog_unavailable$"):
        case.cli._select("vision_understanding")


def test_revoked_reference_never_reuses_previous_credentials(case):
    path = write_catalog(case.root / "authorized.json", catalog())
    case.patch.setenv(ENV, str(path))
    assert case.cli._select("vision_understanding")[0] == "fixture-vision"
    path.unlink()
    with pytest.raises(case.cli.MediaError, match="^media_model_catalog_unavailable$"):
        case.cli._select("vision_understanding")


def test_explicit_read_permission_error_never_falls_back_or_leaks(case):
    write_catalog(case.legacy, catalog("must-not-borrow"))
    path = write_catalog(case.root / "authorized.json", catalog())
    case.patch.setenv(ENV, str(path))
    original_open = builtins.open

    def denied(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise PermissionError("private-path-and-content-must-not-leak")
        return original_open(file, *args, **kwargs)

    case.patch.setattr(builtins, "open", denied)
    with pytest.raises(case.cli.MediaError, match="^media_model_catalog_unavailable$"):
        case.cli._select("vision_understanding")


def test_explicit_catalog_does_not_borrow_legacy_matrix_or_cached_credentials(case):
    value = catalog()
    write_catalog(case.legacy, value)
    explicit = write_catalog(case.root / "authorized.json", value)
    legacy_matrix = {"by_model": {"fixture-vision": {
        "pass_appids": ["different-home-credential"]}}}
    write_catalog(case.legacy.with_name("appid_matrix.json"), legacy_matrix)
    # 先证明默认兼容路径仍可加载矩阵，再切入显式授权作用域。
    assert case.mr._load_appid_matrix() == legacy_matrix
    case.patch.setenv(ENV, str(explicit))
    assert case.mr._load_appid_matrix() == {}
    assert case.mr._appids_of("fixture-vision") == ["fixture-only-not-a-real-key"]


def test_catalog_does_not_invent_unknown_modalities(case):
    path = write_catalog(case.root / "authorized.json", catalog())
    case.patch.setenv(ENV, str(path))
    assert case.cli._select("vision_understanding")[0] == "fixture-vision"
    assert case.mr.input_modalities("fixture-vision") is None
    assert "input" not in case.mr._load_models()["vision_understanding"]["candidates"][0]


@pytest.mark.parametrize("server_inputs,request_inputs", [
    (["text", "image"], ["text"]),
    (["text"], ["text", "image"]),
])
@pytest.mark.parametrize("reference_kind", ["file", "directory", "tilde", "home"])
def test_input_modalities_reads_request_catalog_not_server(
        case, server_inputs, request_inputs, reference_kind):
    server_data = catalog("same-model", inputs=server_inputs)
    server_data["vision_understanding"]["candidates"].append(
        {"model": "server-only", "input": ["text", "image"]})
    server = write_catalog(case.root / "server/models.json", server_data)
    request_home = case.root / "request-home"
    relative = (".claude/skills/aigc-model-router/models.json"
                if reference_kind == "home" else "catalog/models.json")
    request = write_catalog(request_home / relative, catalog("same-model", inputs=request_inputs))
    case.patch.setenv(ENV, str(server))
    environment = {"HOME": str(request_home)}
    if reference_kind != "home":
        environment[ENV] = {
            "file": str(request), "directory": str(request.parent), "tilde": "~/" + relative,
        }[reference_kind]
    before_env, before_files = dict(environment), (server.read_bytes(), request.read_bytes())
    assert case.mr.input_modalities("same-model") == sorted(server_inputs)
    assert case.mr.input_modalities("same-model", environment=environment) == sorted(request_inputs)
    assert case.mr.input_modalities("server-only", environment=environment) is None
    assert case.mr.input_modalities("same-model") == sorted(server_inputs)
    assert environment == before_env
    assert (server.read_bytes(), request.read_bytes()) == before_files
    assert os.environ[ENV] == str(server)


@pytest.mark.parametrize("bad", [
    "missing", "empty", "whitespace", "malformed", "array", "null", "empty-directory",
])
def test_input_modalities_bad_request_catalog_does_not_fall_back_to_server(case, bad):
    server = write_catalog(case.root / "server.json", catalog("same-model", inputs=["text", "image"]))
    case.patch.setenv(ENV, str(server))
    request = case.root / "request.json"
    if bad == "malformed":
        request.write_text('{"private_fixture": "never-echo-this", invalid')
    elif bad == "array":
        request.write_text("[]")
    elif bad == "null":
        request.write_text("null")
    elif bad == "empty-directory":
        request.mkdir()
    environment = {ENV: "" if bad == "empty" else "  " if bad == "whitespace" else str(request)}
    with pytest.raises(ValueError, match="^model_catalog_unavailable$"):
        case.mr.input_modalities("same-model", environment=environment)
    assert case.mr.input_modalities("same-model") == ["image", "text"]


@pytest.mark.parametrize("reference", ["default-home", "tilde"])
def test_input_modalities_missing_request_home_never_borrows_server_home(case, reference):
    write_catalog(case.legacy, catalog("same-model", inputs=["text", "image"]))
    assert case.mr.input_modalities("same-model") == ["image", "text"]
    environment = {} if reference == "default-home" else {ENV: "~/.claude/skills/aigc-model-router/models.json"}
    if reference == "default-home":
        assert case.mr.input_modalities("same-model", environment=environment) is None
    else:
        with pytest.raises(ValueError, match="^model_catalog_unavailable$"):
            case.mr.input_modalities("same-model", environment=environment)


def test_input_modalities_request_directions_do_not_use_global_all_directions(case):
    server = write_catalog(case.root / "server.json", {
        "programming": {"candidates": [{"model": "same-model", "input": ["text"]}]}})
    request = write_catalog(case.root / "request.json", catalog("same-model", inputs=["text", "image"]))
    case.patch.setenv(ENV, str(server))
    assert case.mr.all_directions() == ["programming"]
    assert case.mr.input_modalities("same-model", environment={ENV: str(request)}) == ["image", "text"]
    assert case.mr.all_directions() == ["programming"]
    assert case.mr.input_modalities("same-model") == ["text"]


def test_catalog_does_not_change_provider_binding_or_default_model(case):
    binding = json.dumps({"model": "operator-text-model",
                         "base_url": "https://provider.example.invalid/v1",
                         "api_key_env": "FIXTURE_OPERATOR_KEY"})
    case.patch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    case.patch.setenv("LEGION_CODEX_RESPONSES_BINDING", binding)
    case.patch.setenv("FIXTURE_OPERATOR_KEY", "fixture-not-real")
    case.patch.setenv("MEYO_UNIFIED_MODEL", "operator-text-model")
    case.patch.setenv("MEYO_UNIFIED_GATEWAY", "https://provider.example.invalid/v1")
    before = case.mr.explicit_responses_binding()
    path = write_catalog(case.root / "authorized.json", catalog())
    case.patch.setenv(ENV, str(path))
    assert case.cli._select("vision_understanding")[0] == "fixture-vision"
    assert case.mr.explicit_responses_binding() == before
    assert case.mr.input_modalities("operator-text-model") is None
    env = case.media._gw_env()
    for key in ("MEYO_UNIFIED_MODEL", "MEYO_UNIFIED_GATEWAY",
                "LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING"):
        assert env[key] == os.environ[key]


CHILD = r"""
import json, os, sys
def offline(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
        raise AssertionError("child may only read the synthetic catalog")
sys.addaudithook(offline)
sys.path.insert(0, sys.argv[1])
import model_registry, media_model_cli
try:
    model, credential = media_model_cli._select("vision_understanding")
    print(json.dumps({"model": model,
                      "authorization_matches_fixture": credential == "fixture-only-not-a-real-key",
                      "legacy_exists": os.path.exists(model_registry._MODELS_PATH),
                      "reference": os.environ.get("MEYO_MODEL_CATALOG_PATH")}))
except media_model_cli.MediaError as exc:
    print(json.dumps({"error": str(exc)}))
"""


@pytest.mark.parametrize("reference_kind", ["absolute-file", "relative-directory", "tilde-file"])
def test_gw_env_reaches_fresh_child_with_different_home_and_cwd(case, reference_kind):
    path = write_catalog(case.root / "home/authorized/models.json", catalog())
    case.patch.chdir(case.root)
    reference = {
        "absolute-file": str(path),
        "relative-directory": "home/authorized",
        "tilde-file": "~/authorized/models.json",
    }[reference_kind]
    case.patch.setenv(ENV, reference)
    parent_env = case.media._gw_env()
    child_home = case.root / "different-home"
    child_home.mkdir()
    child_env = dict(parent_env, HOME=str(child_home))
    # 真实新 Python 进程重新 import；它的默认 HOME 没有目录，且从不请求模型。
    process = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, str(SERVICE)],
        cwd=child_home, env=child_env, capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    assert observed.get("model") == "fixture-vision", observed
    assert observed["authorization_matches_fixture"] is True
    assert observed["legacy_exists"] is False
    assert observed["reference"] == str(path)
    assert os.environ[ENV] == reference, "传给子进程时归一化，不能改调用者环境"
    assert not (child_home / ".claude").exists(), "不得向隔离 HOME 复制个人配置"


@pytest.mark.parametrize("reference", ["", "   ", "missing.json"])
def test_gw_env_keeps_invalid_explicit_declaration_fail_closed(case, reference):
    case.patch.setenv(ENV, reference)
    write_catalog(case.legacy, catalog("must-not-borrow"))
    env = case.media._gw_env()
    assert ENV in env, "不能删除无效显式声明然后静默回落 HOME"
    process = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, str(SERVICE)],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"error": "media_model_catalog_unavailable"}


def test_media_cli_and_parent_return_only_safe_catalog_error(case, capsys):
    write_catalog(case.legacy, catalog("must-not-borrow"))
    case.patch.setenv(ENV, str(case.root / "private-path-not-for-response.json"))
    assert case.cli.main(["chat", "fixture", "--domain", "vision_understanding"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "media_model_catalog_unavailable\n"
    assert case.media._safe_media_error(captured.err) == "media_model_catalog_unavailable"


def test_see_image_real_child_reports_bad_reference_before_network(case):
    """真走 see_image -> _gw_env -> CLI；只验证失败回流，不假装识图成功。"""
    write_catalog(case.legacy, catalog("must-not-borrow"))
    case.patch.setenv(ENV, str(case.root / "missing-authorized.json"))
    bootstrap = case.root / "offline_media_cli.py"
    bootstrap.write_text(
        "import sys\n"
        "def offline(event, args):\n"
        "    if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):\n"
        "        raise AssertionError('media test forbids network and model calls')\n"
        "sys.addaudithook(offline)\n"
        f"sys.path.insert(0, {str(SERVICE)!r})\n"
        "from media_model_cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(bootstrap))
    image = case.root / "fixture.png"
    image.write_bytes(b"fixture image bytes, never sent")
    result = case.media.see_image(str(image), "offline failure contract", timeout_s=5)
    assert result == {"ok": False, "error": "media_model_catalog_unavailable"}


def test_task_payload_cannot_replace_operator_catalog_reference(case):
    path = write_catalog(case.root / "authorized.json", catalog())
    case.patch.setenv(ENV, str(path))
    result = case.media.run_media_tool("see_image", {
        "image_path": str(case.root / "not-created.png"),
        ENV: str(case.root / "task-supplied.json"),
    })
    assert result["ok"] is False
    assert os.environ[ENV] == str(path)
    assert case.cli._select("vision_understanding")[0] == "fixture-vision"


def _capture_media_request(case):
    path = write_catalog(case.root / "gateway-test.json", catalog())
    case.patch.setenv(ENV, str(path))
    requests = []

    def capture(request, timeout):
        requests.append(request)
        return io.StringIO("{}")

    case.patch.setattr(case.cli.urllib.request, "build_opener",
                       lambda *args: SimpleNamespace(open=capture))
    return requests


@pytest.mark.parametrize(("media_gateway", "primary_gateway", "expected"), [
    (None, None, "https://aigc.sankuai.com/v1/openai/native"),
    (None, "", "https://aigc.sankuai.com/v1/openai/native"),
    (None, "https://primary.example.invalid/agentic/v1",
     "https://primary.example.invalid/agentic/v1"),
    ("https://media.example.invalid/v1", "https://primary.example.invalid/agentic/v1",
     "https://media.example.invalid/v1"),
    ("https://media.example.invalid/v1/", "https://primary.example.invalid/agentic/v1",
     "https://media.example.invalid/v1"),
])
def test_media_gateway_precedence_preserves_binding_and_child_environment(
    case, media_gateway, primary_gateway, expected,
):
    requests = _capture_media_request(case)
    if primary_gateway is not None:
        case.patch.setenv("MEYO_UNIFIED_GATEWAY", primary_gateway)
    if media_gateway is not None:
        case.patch.setenv("MEYO_MEDIA_GATEWAY", media_gateway)
    case.patch.setenv("MEYO_UNIFIED_MODEL", "unchanged-primary-model")
    case.patch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    case.patch.setenv("LEGION_CODEX_RESPONSES_BINDING", '{"fixture":"unchanged"}')
    before = dict(os.environ)
    case.cli._request("vision_understanding", "/chat/completions", {"messages": []}, 1)
    assert len(requests) == 1
    assert requests[0].full_url == expected + "/chat/completions"
    assert json.loads(requests[0].data)["model"] == "fixture-vision"
    assert requests[0].get_header("Authorization") == "Bearer fixture-only-not-a-real-key"
    assert dict(os.environ) == before
    child = case.media._gw_env()
    for key in ("MEYO_MEDIA_GATEWAY", "MEYO_UNIFIED_GATEWAY", "MEYO_UNIFIED_MODEL",
                "LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING"):
        assert child.get(key) == before.get(key)
    assert "input" not in case.mr._load_models()["vision_understanding"]["candidates"][0]


@pytest.mark.parametrize("gateway", [
    "", "   ", "not-a-url", "ftp://media.example.invalid/v1", "https:///v1",
    "https://user:fixture-password@media.example.invalid/v1",
    "https://[invalid", "https://media.example.invalid:bad/v1",
    "https://media.example.invalid:70000/v1",
    "https://media.example.invalid/v1?token=fixture",
    "https://media.example.invalid/v1#fragment",
    "https://media.example.invalid/v1\n",
    "https://media.example.invalid\\other/v1",
])
def test_invalid_explicit_media_gateway_never_falls_back_or_opens_request(case, gateway):
    requests = _capture_media_request(case)
    case.patch.setenv("MEYO_UNIFIED_GATEWAY", "https://primary.example.invalid/agentic/v1")
    case.patch.setenv("MEYO_MEDIA_GATEWAY", gateway)
    assert case.media._gw_env()["MEYO_MEDIA_GATEWAY"] == gateway
    with pytest.raises(case.cli.MediaError, match="^media_gateway_invalid$"):
        case.cli._request("vision_understanding", "/chat/completions", {"messages": []}, 1)
    assert requests == []


def test_invalid_media_gateway_cli_returns_existing_safe_error(case, capsys):
    requests = _capture_media_request(case)
    case.patch.setenv("MEYO_MEDIA_GATEWAY", "")
    assert case.cli.main(["chat", "fixture", "--domain", "vision_understanding"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "media_gateway_invalid\n"
    assert case.media._safe_media_error(captured.err) == "media_gateway_invalid"
    assert requests == []
