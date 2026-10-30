"""CPU-only fixtures for the existing host's local-model transport.

The child below is deliberately not a model; no weights, GPU or provider is used.
"""
from __future__ import annotations

import hashlib
import importlib
import base64
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace
import zlib

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
PREFIX = "local_mcp:meyo-local-model:"
IDS = [PREFIX + name for name in ("preflight", "coding_text", "shape_3d")]

FAKE_WRAPPER = r'''
import hashlib, json, os
from pathlib import Path
import struct, sys
args = sys.argv[1:]
def arg(key):
    return args[args.index(key) + 1]
if args[0] == "doctor":
    # Mirror the real CPU doctors, not the unrelated generation receipt schema.
    doctor = {"gpu_tested": False, "cpu_fixture": True}
    if "--backend" in args:
        doctor.update(runtime={"status": "cli_ready", "backend": "python"},
                      model_inference=False, weights_verified=False)
    else:
        doctor.update(runtime={"versions": {"mlx-lm": "cpu-fixture"}},
                      model={"status": "verified"})
    print(json.dumps(doctor))
    raise SystemExit(0)
output = Path(arg("--output"))
if args[0] == "text":
    raw = Path(arg("--prompt-file")).read_bytes()
    receipt_file = Path(str(output) + ".receipt.json")
    kind = "coding"
else:
    raw_input = Path(arg("--image")).read_bytes()
    payload = json.dumps({"asset": {"version": "2.0"}}).encode()
    payload += b" " * (-len(payload) % 4)
    raw = b"glTF" + struct.pack("<II", 2, 20 + len(payload))
    raw += struct.pack("<II", len(payload), 0x4E4F534A) + payload
    receipt_file = Path.home() / ".local/share/local-media/runs/3d/cpu-fixture/receipt.json"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    kind = "shape"
    assert arg("--backend") == "python" and arg("--texture") == "none"
output.write_bytes(raw)
receipt = {"status": "success", "output": str(output), "cpu_fixture": True,
           "mode": "text" if kind == "coding" else "shape",
           "finish_reason": "stop", "quality_review": "pending",
           "input_sha256": hashlib.sha256(raw if kind == "coding" else raw_input).hexdigest(),
           "seen_env": {key: os.environ.get(key) for key in (
                "HOME", "MEYO_USER_ID", "MEYO_SESSION_ID", "MEYO_WORKDIR",
                "LOCAL_MEDIA_LEGION_SERVICE_DIR", "LEGION_PROC_REGISTRY_DIR")},
           "has_secret": "OPENAI_API_KEY" in os.environ or "PYTHONPATH" in os.environ}
receipt_file.write_text(json.dumps(receipt))
print("CPU fixture log, not a generated model result")
print(json.dumps({"status": "success", "output": str(output),
                  "receipt" if kind == "coding" else "receipt_path": str(receipt_file)}, indent=2))
'''


@pytest.fixture(autouse=True)
def _pin_isolation_env(tmp_path, monkeypatch):
    for name in ("MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR", "HOME"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.delenv("MEYO_LOCAL_MODEL_CONFIG", raising=False)
    monkeypatch.delenv("MEYO_NATIVE_CAPABILITY_IDS", raising=False)


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(ORCH))
    module = importlib.import_module("native_capability_host")
    import external_agent_registry
    monkeypatch.setattr(external_agent_registry, "capability_actions",
                        lambda: {"actions": []})
    work, trusted = tmp_path / "work", tmp_path / "trusted"
    work.mkdir()
    trusted.mkdir()
    script = trusted / "cpu_fixture.py"
    script.write_text(FAKE_WRAPPER)
    config = {
        "version": 1, "python": str(Path(sys.executable).resolve()),
        "coding_script": str(script), "shape_script": str(script),
        "home": str(tmp_path / "HOME"), "state_dir": str(tmp_path / "MEYO_LEGION_DIR"),
        "registry_dir": str(tmp_path / "LEGION_PROC_REGISTRY_DIR"),
        "timeout_s": 10, "wait_s": 0, "term_grace_s": 1,
    }
    monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps(config))
    monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", json.dumps(IDS))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-local-wrapper")
    monkeypatch.setenv("PYTHONPATH", "/must-not-reach-local-wrapper")
    context = {
        "user_id": "alice", "session_id": "same-original-session",
        "workdir": str(work), "task_facts": {"team_id": "same-team"},
        "project_context": {"workspace_root": str(work)},
        "capability_policy": {"execute": True, "read": True, "write": True, "publish": False},
        "allowed_ids": IDS,
    }
    return SimpleNamespace(module=module, work=work, script=script, config=config,
                           context=context, monkeypatch=monkeypatch)


def session(local):
    return local.module.CapabilitySession(local.context)


def invoke(local, name, params, current=None):
    return (current or session(local)).call_capability({
        "tool_id": PREFIX + name, "params": params,
    })


def coding_params(local):
    source = local.work / "prompt.txt"
    source.write_text("CPU fixture prompt，不是游戏代码。")
    return {"prompt_file": str(source), "output_file": str(local.work / "result.txt"),
            "max_tokens": 32, "context_tokens": 1024, "thinking": "off"}


def _postlaunch_failure_context(local, params, result):
    assert result["ok"] is False and result["status"] == "failed"
    assert result["session_id"] == local.context["session_id"]
    assert result["error"] and len(result["error"]) <= 8192
    context = result["failure_context"]
    staging = Path(context["staging_dir"])
    assert staging.is_dir() and staging.stat().st_mode & 0o777 == 0o700
    assert (staging / "prompt.txt").read_bytes() == Path(params["prompt_file"]).read_bytes()
    assert not Path(params["output_file"]).exists()
    assert not Path(params["output_file"] + ".receipt.json").exists()
    assert not list(Path(local.config["registry_dir"]).glob("*.ns-*.json"))
    return context


def bound_environment(local):
    return {
        "MEYO_USER_ID": "alice", "MEYO_SESSION_ID": "same-original-session",
        "MEYO_WORKDIR": str(local.work), "MEYO_TASK_FACTS_JSON": "{}",
        "MEYO_PROJECT_CONTEXT_JSON": "{}",
        "MEYO_CAPABILITY_POLICY_JSON": json.dumps(local.context["capability_policy"]),
        "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(IDS),
        "MEYO_LOCAL_MODEL_CONFIG": json.dumps(local.config),
    }


def shared_coding_params(local):
    return {"server": "meyo_capabilities", "name": "call_capability",
            "arguments": {"tool_id": PREFIX + "coding_text",
                          "params_json": json.dumps(coding_params(local))}}


def _image_png(rows, *, color=6, depth=8, filters=(0,), interlaced=False, transparency=None):
    """Tiny lossless CPU fixture, including PNG filters/Adam7; never a model input from HOME."""
    width, height = len(rows[0]), len(rows)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bpp = max(1, (channels * depth + 7) // 8)
    passes = ((0, 0, 1, 1),) if not interlaced else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    scanlines = bytearray()
    for x0, y0, dx, dy in passes:
        if x0 >= width or y0 >= height:
            continue
        previous = None
        for y in range(y0, height, dy):
            samples = [v for x in range(x0, width, dx) for v in rows[y][x]]
            if depth < 8:
                count = 8 // depth
                row = bytes(sum(v << (8 - depth * (i + 1)) for i, v in enumerate(samples[j:j + count]))
                            for j in range(0, len(samples), count))
            else:
                row = b"".join(v.to_bytes(depth // 8, "big") for v in samples)
            previous = previous if previous is not None else bytes(len(row))
            kind = filters[y % len(filters)]
            scanlines.append(kind)
            for i, value in enumerate(row):
                left, above = row[i - bpp] if i >= bpp else 0, previous[i]
                corner = previous[i - bpp] if i >= bpp else 0
                paeth = min((left, above, corner), key=lambda v: abs(left + above - corner - v))
                scanlines.append((value - (0, left, above, (left + above) // 2, paeth)[kind]) % 256)
            previous = row

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, int(interlaced)))
    if color == 3:
        png += chunk(b"PLTE", bytes((20, 30, 40, 60, 70, 80)))
    if transparency is not None:
        png += chunk(b"tRNS", transparency)
    return png + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")


def _bbox(left, top, right, bottom):
    return {"left": left, "top": top, "right_exclusive": right, "bottom_exclusive": bottom}


def test_local_actions_are_explicit_declarations_not_gpu_probes(local, monkeypatch):
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("listing launched a process"))
    actions = session(local).list_capabilities({})["actions"]
    assert {row["id"] for row in actions} == set(IDS)
    assert all(row["input_schema"]["additionalProperties"] is False for row in actions)
    assert "run_harness" not in repr(actions)


def test_shape_declaration_exposes_input_facts_through_native_and_shared_entry(local, monkeypatch):
    import agent_tool_provider
    import legion_tool
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("declaration launched a process"))
    native = session(local).list_capabilities({})["actions"]
    with agent_tool_provider._runtime_scope(bound_environment(local), None, inherit_process_context=False):
        shared = legion_tool._run_mcp_call("meyo_capabilities", "list_capabilities", {})
    assert shared["ok"] is True
    declared = local.module.declared_capability_actions()["actions"]
    shapes = [next(row for row in rows if row["id"] == PREFIX + "shape_3d")
              for rows in (native, shared["result"]["actions"], declared)]
    assert shapes[0] == shapes[1] == shapes[2]
    description = shapes[0]["description"]
    for fact in ("非零 alpha", "不自动分离背景", "原 PNG", "SHA-256", "全透明像素", "包围框"):
        assert fact in description
    assert "不代表 PBR、骨骼、动画或视觉质量通过" in description


def test_coding_runs_real_cpu_child_and_publishes_bound_output_and_receipt(local, monkeypatch):
    monkeypatch.setattr(local.module, "_validated_screenshot",
                        lambda *a, **k: pytest.fail("coding decoded an image"))
    params = coding_params(local)
    result = invoke(local, "coding_text", params)
    assert result["ok"] is True and result["status"] == "success", result
    raw = Path(result["path"]).read_bytes()
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert raw == Path(params["prompt_file"]).read_bytes()
    assert result["sha256"] == hashlib.sha256(raw).hexdigest() == receipt["output"]["sha256"]
    assert receipt["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert set(receipt["input"]) == {"path", "sha256", "bytes"}
    assert "input" not in result
    assert receipt["user_id"] == "alice"
    assert receipt["session_id"] == "same-original-session"
    assert receipt["wrapper_receipt"]["cpu_fixture"] is True
    assert receipt["wrapper_receipt"]["has_secret"] is False
    env = receipt["wrapper_receipt"]["seen_env"]
    assert env["HOME"] == local.config["home"]
    assert env["MEYO_USER_ID"] == "alice"
    assert env["MEYO_SESSION_ID"] == "same-original-session"
    assert env["LOCAL_MEDIA_LEGION_SERVICE_DIR"] == str(SERVICE)
    assert env["LEGION_PROC_REGISTRY_DIR"] == local.config["registry_dir"]
    assert result["quality_review"] == "not_performed"


@pytest.mark.parametrize("entry", ["native", "shared"])
def test_parent_toolchain_survives_resolved_interpreter_without_inheriting_path(local, entry):
    """A package-manager Python symlink need not share its real binary's directory."""
    toolchain = local.script.parent / "bin"
    toolchain.mkdir()
    interpreter = toolchain / "python"
    interpreter.symlink_to(local.config["python"])
    companion = toolchain / "local-capability-companion"
    companion.write_text("#!/bin/sh\nprintf '%s\\n' trusted-parent-toolchain\n")
    companion.chmod(0o755)
    local.config.update(python=str(interpreter), executable_dirs=[str(toolchain)])
    local.monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps(local.config))
    # The service/author PATH is not an authority for this privileged child.
    untrusted = local.work / "bin"
    untrusted.mkdir()
    poison = untrusted / companion.name
    poison.write_text("#!/bin/sh\nprintf '%s\\n' borrowed-author-path\n")
    poison.chmod(0o755)
    local.monkeypatch.setenv("PATH", str(untrusted) + os.pathsep + os.environ.get("PATH", ""))
    local.script.write_text(FAKE_WRAPPER.replace(
        "import hashlib, json, os",
        "import hashlib, json, os, subprocess\n"
        "assert subprocess.check_output(['local-capability-companion'], text=True).strip() "
        "== 'trusted-parent-toolchain'\n"
        f"assert {str(untrusted)!r} not in os.environ['PATH'].split(os.pathsep)",
    ))
    if entry == "native":
        result = invoke(local, "coding_text", coding_params(local))
    else:
        import agent_tool_provider
        import legion_tool
        with agent_tool_provider._runtime_scope(
                bound_environment(local), None, inherit_process_context=False):
            result = legion_tool._run_mcp_call("meyo_capabilities", "call_capability", {
                "tool_id": PREFIX + "coding_text",
                "params_json": json.dumps(coding_params(local)),
            })["result"]
    assert result["ok"] is True, result
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["wrapper_receipt"]["has_secret"] is False
    assert receipt["wrapper_receipt"]["seen_env"]["HOME"] == local.config["home"]
    assert receipt["session_id"] == local.context["session_id"]
    assert receipt["quality_review"] == "not_performed"


@pytest.mark.parametrize("invalid", [
    "not_list", "relative", "root", "workspace", "workspace_link", "file",
    "path_separator", "world_writable",
])
def test_parent_toolchain_rejects_non_directory_or_task_controlled_search_paths(local, invalid):
    directory = local.script.parent / "toolchain"
    directory.mkdir()
    value = [str(directory)]
    if invalid == "not_list":
        value = str(directory)
    elif invalid == "relative":
        value = ["bin"]
    elif invalid == "root":
        value = [str(directory.anchor)]
    elif invalid == "workspace":
        value = [str(local.work)]
    elif invalid == "workspace_link":
        directory.rmdir()
        directory.symlink_to(local.work, target_is_directory=True)
    elif invalid == "file":
        value = [str(local.script)]
    elif invalid == "path_separator":
        separated = local.script.parent / ("bin" + os.pathsep + "other")
        separated.mkdir()
        value = [str(separated)]
    elif invalid == "world_writable":
        directory.chmod(0o777)
    local.monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps({
        **local.config, "executable_dirs": value,
    }))
    with pytest.raises(ValueError):
        session(local)


def test_parent_toolchain_rejects_separator_in_resolved_symlink_target(local):
    target = local.script.parent / ("bin" + os.pathsep + "other")
    target.mkdir()
    link = local.script.parent / "toolchain"
    link.symlink_to(target, target_is_directory=True)
    assert os.pathsep not in str(link)
    assert os.pathsep in str(link.resolve(strict=True))
    local.monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps({
        **local.config, "executable_dirs": [str(link)],
    }))
    with pytest.raises(ValueError, match="trusted host directory"):
        session(local)


@pytest.mark.parametrize("extra", [
    {"env": {"HOME": "/other"}}, {"command": ["echo", "bad"]},
    {"model": "/other/weights"}, {"uid": "mallory"}, {"owner": "mallory"},
    {"backend": "swift"}, {"texture": "pbr"}, {"timeout": 999999},
    {"executable_dirs": ["/other/bin"]},
    {"_capability_policy": {"execute": True}},
])
def test_local_parameters_cannot_override_parent_configuration(local, extra):
    with pytest.raises(ValueError):
        invoke(local, "coding_text", {**coding_params(local), **extra})


@pytest.mark.parametrize("field,value", [
    ("max_tokens", 0), ("max_tokens", 1025), ("max_tokens", True),
    ("context_tokens", 32769), ("context_tokens", 0), ("thinking", "maybe"),
])
def test_coding_parameter_bounds_are_checked_before_launch(local, field, value, monkeypatch):
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("invalid request launched a child"))
    params = {**coding_params(local), field: value}
    with pytest.raises(ValueError):
        invoke(local, "coding_text", params)


@pytest.mark.parametrize("denied", ["execute", "read", "write"])
def test_each_parent_permission_denies_without_launch(local, denied, monkeypatch):
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("denied request launched a child"))
    local.context["capability_policy"][denied] = False
    result = invoke(local, "coding_text", coding_params(local))
    assert result["ok"] is False and result["authorization"]["allowed"] is False


@pytest.mark.parametrize("attack", ["input_symlink", "output_symlink", "existing", "sidecar"])
def test_paths_and_existing_files_are_rejected_before_launch(local, attack, monkeypatch):
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("unsafe path launched a child"))
    params = coding_params(local)
    outside = local.work.parent / "outside"
    outside.mkdir()
    if attack == "input_symlink":
        Path(params["prompt_file"]).unlink()
        (outside / "secret.txt").write_text("not authorized")
        Path(params["prompt_file"]).symlink_to(outside / "secret.txt")
    elif attack == "output_symlink":
        (local.work / "linked").symlink_to(outside)
        params["output_file"] = str(local.work / "linked/result.txt")
    elif attack == "existing":
        Path(params["output_file"]).write_text("preserve me")
    else:
        Path(params["output_file"] + ".receipt.json").write_text("preserve receipt")
    with pytest.raises((ValueError, OSError)):
        invoke(local, "coding_text", params)
    assert list(outside.iterdir()) == ([outside / "secret.txt"] if attack == "input_symlink" else [])


def test_parent_output_directory_replacement_never_writes_outside(local, monkeypatch):
    import media_tools
    original = media_tools._bounded_media_process
    folder = local.work / "assets"
    folder.mkdir()
    outside = local.work.parent / "outside"
    outside.mkdir()
    params = coding_params(local)
    params["output_file"] = str(folder / "result.txt")

    def replaced(*args, **kwargs):
        folder.rename(local.work / "old-assets")
        folder.symlink_to(outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(media_tools, "_bounded_media_process", replaced)
    result = invoke(local, "coding_text", params)
    _postlaunch_failure_context(local, params, result)
    assert not list(outside.iterdir())
    assert not list((local.work / "old-assets").iterdir())


def test_input_bytes_are_snapshotted_before_child_can_observe_changes(local, monkeypatch):
    import media_tools
    original = media_tools._bounded_media_process
    params = coding_params(local)
    initial = Path(params["prompt_file"]).read_bytes()

    def mutate(*args, **kwargs):
        Path(params["prompt_file"]).write_text("replacement after validation")
        return original(*args, **kwargs)

    monkeypatch.setattr(media_tools, "_bounded_media_process", mutate)
    result = invoke(local, "coding_text", params)
    assert result["ok"] is True
    assert Path(result["path"]).read_bytes() == initial


def test_shape_uses_snapshot_python_backend_and_wrapper_receipt(local):
    # A valid PNG is generated by CPU from the test fixture; not a private photo.
    import base64
    image = local.work / "input.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
    result = invoke(local, "shape_3d", {
        "image_file": str(image), "output_file": str(local.work / "shape.glb"), "seed": 42,
    })
    assert result["ok"] is True, result
    assert Path(result["path"]).read_bytes()[:4] == b"glTF"
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["input"]["sha256"] == receipt["wrapper_receipt"]["input_sha256"]
    assert receipt["wrapper_receipt"]["quality_review"] == "pending"
    assert "receipt.json" in receipt["wrapper_receipt_source"]["path"]


@pytest.mark.parametrize("entry", ["native", "shared"])
@pytest.mark.parametrize("case", ["transparent", "opaque", "nonzero_190", "all_zero", "rgb"])
def test_shape_input_facts_match_frozen_png_return_and_receipt(local, monkeypatch, entry, case):
    import agent_tool_provider
    import legion_tool
    import media_tools
    alpha = {"transparent": (0, 0, 0, 0, 190, 255), "opaque": (255,) * 6,
             "nonzero_190": (190, 255) * 3, "all_zero": (0,) * 6, "rgb": (255,) * 6}[case]
    color = 2 if case == "rgb" else 6
    rows = [[(17, 42, 63, alpha[y * 3 + x])[:3 if color == 2 else 4] for x in range(3)]
            for y in range(2)]
    raw = _image_png(rows, color=color, filters=(1, 4))
    image = local.work / "source.png"
    image.write_bytes(raw)
    params = {"image_file": str(image), "output_file": str(local.work / "shape.glb"), "seed": 20261025}
    original = media_tools._bounded_media_process
    original_decompress = local.module.zlib.decompressobj
    inflations = []
    commands = []

    def count_inflate(*a, **k):
        inflations.append(True)
        return original_decompress(*a, **k)

    def cpu_wrapper(command, *a, **k):
        commands.append(command)
        assert command[3] == str(local.script), "only the isolated CPU stub may run"
        assert Path(command[command.index("--image") + 1]).read_bytes() == raw
        assert command[command.index("--seed") + 1] == "20261025"
        assert command[command.index("--backend") + 1] == "python"
        assert command[command.index("--texture") + 1] == "none"
        # Facts must not reopen a mutable workspace image after the wrapper starts.
        image.write_bytes(b"changed after the trusted snapshot")
        return original(command, *a, **k)

    monkeypatch.setattr(local.module.zlib, "decompressobj", count_inflate)
    monkeypatch.setattr(media_tools, "_bounded_media_process", cpu_wrapper)
    args = {"tool_id": PREFIX + "shape_3d", "params_json": json.dumps(params)}
    if entry == "native":
        result = session(local).call_capability_json(args)
    else:
        with agent_tool_provider._runtime_scope(bound_environment(local), None, inherit_process_context=False):
            envelope = legion_tool._run_mcp_call("meyo_capabilities", "call_capability", args)
        assert envelope["ok"] is True
        result = envelope["result"]
    assert result["ok"] is True and result["status"] == "success", result
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    expected_bbox = (None if case == "all_zero" else
                     _bbox(1, 1, 3, 2) if case == "transparent" else _bbox(0, 0, 3, 2))
    expected = {
        "path": str(image), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "image": {"width": 3, "height": 2, "bit_depth": 8, "color_type": color,
                  "alpha_source": "implicit_opaque" if case == "rgb" else "channel",
                  "has_fully_transparent_pixels": case in ("transparent", "all_zero"),
                  "nonzero_alpha_bbox": expected_bbox},
    }
    assert result.get("input") == receipt["input"] == expected
    assert receipt["wrapper_receipt"]["input_sha256"] == expected["sha256"]
    assert result["quality_review"] == receipt["quality_review"] == "not_performed"
    assert receipt["wrapper_receipt"]["quality_review"] == "pending"
    assert result["session_id"] == receipt["session_id"] == "same-original-session"
    assert receipt["user_id"] == "alice"
    assert len(commands) == len(inflations) == 1


@pytest.mark.parametrize("color,depth,transparency,alpha_source", [
    (6, 8, None, "channel"), (6, 16, None, "channel"), (4, 8, None, "channel"),
    (4, 16, None, "channel"), (2, 8, struct.pack(">HHH", 0, 0, 0), "tRNS"),
    (2, 16, struct.pack(">HHH", 0, 0, 0), "tRNS"),
    (0, 1, b"\0\0", "tRNS"), (0, 2, b"\0\0", "tRNS"), (0, 4, b"\0\0", "tRNS"),
    (0, 8, b"\0\0", "tRNS"), (0, 16, b"\0\0", "tRNS"),
    (3, 1, bytes((0, 190)), "tRNS"), (3, 2, bytes((0, 190)), "tRNS"),
    (3, 4, bytes((0, 190)), "tRNS"), (3, 8, bytes((0, 190)), "tRNS"),
])
@pytest.mark.parametrize("interlaced", [False, True])
def test_shape_alpha_observation_reuses_validated_scanlines(local, color, depth, transparency,
                                                          alpha_source, interlaced):
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    # 1 stays nonzero even for 16-bit PNGs: these are original sample facts,
    # not a prediction of later RGBA conversion, resizing or output quality.
    rows = [[((1,) * channels if 2 <= x < 7 and 1 <= y < 6 else (0,) * channels)
             for x in range(9)] for y in range(8)]
    png = _image_png(rows, color=color, depth=depth, transparency=transparency,
                     interlaced=interlaced, filters=(0, 1, 2, 3, 4))
    facts = {}
    assert local.module._validated_screenshot(
        base64.b64encode(png).decode(), "png", image_info=facts) == png
    assert facts == {"width": 9, "height": 8, "bit_depth": depth, "color_type": color,
                     "alpha_source": alpha_source, "has_fully_transparent_pixels": True,
                     "nonzero_alpha_bbox": _bbox(2, 1, 7, 6)}


def test_shape_input_facts_are_retained_on_wrapper_failure_without_success_receipt(local):
    image = local.work / "input.png"
    raw = _image_png([[(1, 2, 3, 190), (1, 2, 3, 255)]])
    image.write_bytes(raw)
    local.script.write_text('import json\nprint(json.dumps({"status": "failed", "error": "CPU_ONLY"}))\n'
                            'raise SystemExit(2)\n')
    target = local.work / "shape.glb"
    result = invoke(local, "shape_3d", {"image_file": str(image), "output_file": str(target)})
    assert result["ok"] is False and result["status"] == "failed"
    assert result.get("input", {}).get("sha256") == hashlib.sha256(raw).hexdigest()
    assert result["input"]["image"]["has_fully_transparent_pixels"] is False
    assert not target.exists() and not Path(str(target) + ".receipt.json").exists()


@pytest.mark.parametrize("invalid", ["bad_crc", "truncated", "symlink"])
def test_shape_input_observation_keeps_existing_png_and_path_rejections(local, monkeypatch, invalid):
    import media_tools
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("invalid image reached the wrapper"))
    image = local.work / "input.png"
    raw = _image_png([[(1, 2, 3, 190), (1, 2, 3, 0)]])
    if invalid == "bad_crc":
        raw = raw[:-1] + bytes((raw[-1] ^ 1,))
    if invalid == "truncated":
        raw = raw[:-12]
    if invalid == "symlink":
        outside = local.work.parent / "outside.png"
        outside.write_bytes(raw)
        image.symlink_to(outside)
    else:
        image.write_bytes(raw)
    with pytest.raises((ValueError, OSError)):
        invoke(local, "shape_3d", {"image_file": str(image), "output_file": str(local.work / "out.glb")})
    assert not list(local.work.glob("*.glb*"))


@pytest.mark.parametrize("case,color,transparency", [
    ("gray_overlong_83_bytes", 0, b"\0\0\0\1"),
    ("gray_empty", 0, b""),
    ("gray_short", 0, b"\0"),
    ("rgb_empty", 2, b""),
    ("rgb_short", 2, b"\0" * 4),
    ("rgb_long", 2, b"\0" * 8),
    ("palette_empty", 3, b""),
    ("palette_too_long", 3, b"\0" * 3),
    ("gray_alpha_forbidden", 4, b"\0\0"),
    ("rgba_forbidden", 6, b"\0\0"),
    ("rgba_megabyte", 6, None),
    ("duplicate", 0, b"\0\0"),
    ("after_idat", 0, b"\0\0"),
    ("indexed_before_plte", 3, b"\0"),
    ("indexed_without_plte", 3, b"\0"),
    ("rgb_before_plte", 2, b"\0" * 6),
])
def test_shape_trns_rejected_before_inflation(local, monkeypatch, case, color, transparency):
    """Untrusted tRNS must not become alpha facts or an unrelated large integer tuple."""
    if case == "rgba_megabyte":
        transparency = b"\x80\x01" * (512 * 1024)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = _image_png([[(0,) * channels]], color=color, transparency=transparency)
    if case == "gray_overlong_83_bytes":
        assert len(raw) == 83  # The independent review's exact malformed gray example.
    chunks, offset = [], 8
    while offset < len(raw):
        end = offset + int.from_bytes(raw[offset:offset + 4], "big") + 12
        chunks.append(raw[offset:end])
        offset = end
    trns = next(chunk for chunk in chunks if chunk[4:8] == b"tRNS")
    if case == "duplicate":
        chunks.insert(chunks.index(trns), trns)
    elif case == "after_idat":
        chunks.remove(trns)
        chunks.insert(-1, trns)
    elif case == "indexed_before_plte":
        chunks.remove(trns)
        chunks.insert(1, trns)
    elif case == "indexed_without_plte":
        chunks = [chunk for chunk in chunks if chunk[4:8] != b"PLTE"]
    elif case == "rgb_before_plte":
        payload = b"PLTE" + bytes((20, 30, 40))
        chunks.insert(2, struct.pack(">I", 3) + payload + struct.pack(">I", zlib.crc32(payload)))
    raw = raw[:8] + b"".join(chunks)
    # pytest.fail is not swallowed by the validator's generic Exception handler.
    monkeypatch.setattr(local.module.zlib, "decompressobj",
                        lambda: pytest.fail("invalid tRNS reached pixel inflation"))
    facts = {}
    with pytest.raises(ValueError, match="complete bounded PNG"):
        local.module._validated_screenshot(base64.b64encode(raw).decode(), "png", image_info=facts)
    assert facts == {}


@pytest.mark.parametrize("color,depth,transparency,rows", [
    # PNG decoders must ignore unused high bits in a low-depth tRNS color key.
    (0, 1, struct.pack(">H", 2), [[(0,), (1,), (0,)]]),
    (0, 2, struct.pack(">H", 4), [[(0,), (1,), (0,)]]),
    (0, 4, struct.pack(">H", 16), [[(0,), (1,), (0,)]]),
    (0, 8, struct.pack(">H", 256), [[(0,), (1,), (0,)]]),
    (2, 8, struct.pack(">HHH", 256, 257, 65535),
     [[(0, 1, 255), (0, 2, 255), (0, 1, 255)]]),
    (0, 16, struct.pack(">H", 257), [[(257,), (258,), (257,)]]),
    (2, 16, struct.pack(">HHH", 1, 257, 65535),
     [[(1, 257, 65535), (1, 258, 65535), (1, 257, 65535)]]),
    (3, 2, b"\0", [[(0,), (1,), (0,)]]),
])
@pytest.mark.parametrize("interlaced", [False, True])
def test_shape_trns_valid_sample_keys_and_omitted_palette_alpha(local, color, depth, transparency,
                                                              rows, interlaced):
    raw = _image_png(rows, color=color, depth=depth, transparency=transparency,
                     interlaced=interlaced, filters=(4,))
    facts = {}
    assert local.module._validated_screenshot(
        base64.b64encode(raw).decode(), "png", image_info=facts) == raw
    assert facts["alpha_source"] == "tRNS"
    assert facts["has_fully_transparent_pixels"] is True
    assert facts["nonzero_alpha_bbox"] == _bbox(1, 0, 2, 1)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_shape_scan_control_exceptions_are_not_png_errors(local, monkeypatch, stop):
    error = (InterruptedError("CPU_ONLY_SCAN_STOP") if stop == "cancel" else
             local.module.subprocess.TimeoutExpired(["cpu-fixture"], 0.05))

    def interrupted_scan(*args, **kwargs):
        raise error

    monkeypatch.setattr(local.module, "_png_image_info", interrupted_scan)
    raw = _image_png([[(0, 0, 0, 0)]])
    facts = {}
    with pytest.raises(type(error)) as caught:
        local.module._validated_screenshot(base64.b64encode(raw).decode(), "png", image_info=facts)
    assert caught.value is error
    assert facts == {}


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("width,height", [(512, 512), (16384, 1)], ids=["review_image", "long_row"])
def test_shape_scan_checks_parent_stop_before_finishing_pixels(local, monkeypatch, stop, width, height):
    """Stop during real Paeth decoding, not merely at the later process boundary."""
    import media_tools
    raw = _image_png([[(0, 0, 0, 0)] * width] * height, depth=16, filters=(4,))
    image = local.work / "input.png"
    image.write_bytes(raw)
    target = local.work / "shape.glb"
    progress = {"bytes": 0}

    class ScanRow(bytearray):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            progress["bytes"] += 1

    # Advance only this host's clock after real CPU work; no timing/thread race.
    monkeypatch.setattr(local.module, "bytearray", ScanRow, raising=False)
    monkeypatch.setattr(local.module, "time", SimpleNamespace(monotonic=lambda:
        100 + local.config["timeout_s"] + 1 if stop == "timeout" and progress["bytes"] >= 4096 else 100))
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **k: pytest.fail("cancelled scan launched a wrapper"))
    current = session(local)
    with current.cancellation_scope(lambda: stop == "cancel" and progress["bytes"] >= 4096):
        result = invoke(local, "shape_3d", {"image_file": str(image), "output_file": str(target)}, current)
    assert result["ok"] is False and result["status"] == ("cancelled" if stop == "cancel" else "timeout")
    assert 0 < progress["bytes"] < width * height * 8, "stop was not checked until the whole PNG was scanned"
    assert result["input"] == {"path": str(image), "sha256": hashlib.sha256(raw).hexdigest(),
                               "bytes": len(raw)}  # No partial image facts may escape.
    assert result["session_id"] == "same-original-session"
    assert not target.exists() and not Path(str(target) + ".receipt.json").exists()
    control = Path(local.config["state_dir"]) / "run/native-local-model-calls"
    assert not list(control.iterdir())
    assert not list(Path(local.config["registry_dir"]).iterdir())


@pytest.mark.parametrize("kind", ["coding", "shape"])
def test_preflight_is_doctor_only_and_does_not_publish_generated_artifact(local, kind, monkeypatch):
    monkeypatch.setattr(local.module, "_validated_screenshot",
                        lambda *a, **k: pytest.fail("preflight decoded an image"))
    result = invoke(local, "preflight", {"kind": kind})
    assert result["ok"] is True and result["generation_tested"] is False
    assert result["observation"]["cpu_fixture"] is True
    assert result["observation"]["gpu_tested"] is False
    assert "status" not in result["observation"]
    assert "input" not in result
    if kind == "shape":
        assert result["observation"]["weights_verified"] is False
        assert result["observation"]["model_inference"] is False
    assert not list(local.work.iterdir())


@pytest.mark.parametrize("kind", ["coding", "shape"])
def test_preflight_preserves_doctor_nonzero_and_failure_observation(local, kind):
    observation = {"runtime": {"status": "cli_ready"}, "gpu_tested": False,
                   "model": {"status": "not_ready", "error": "CPU fixture missing receipt"}}
    local.script.write_text(
        "import json\nprint(json.dumps(" + repr(observation) + "))\nraise SystemExit(1)\n")
    result = invoke(local, "preflight", {"kind": kind})
    assert result["ok"] is False and result["returncode"] == 1
    assert result["observation"] == observation
    assert result["generation_tested"] is False
    assert not list(local.work.iterdir())


@pytest.mark.parametrize("write_receipt,error_type", [
    ("pass", "FileNotFoundError"),
    ('receipt_file.write_text("CPU_ONLY_INVALID_RECEIPT")', "JSONDecodeError"),
    ('receipt["output"] = "/different/output"; receipt_file.write_text(json.dumps(receipt))',
     "ValueError"),
    ('receipt["input_sha256"] = "wrong-input"; receipt_file.write_text(json.dumps(receipt))',
     "ValueError"),
], ids=["missing", "malformed", "mismatched-output", "mismatched-input"])
def test_success_stdout_cannot_replace_missing_or_mismatched_receipt(local, write_receipt, error_type):
    params = coding_params(local)
    local.script.write_text(FAKE_WRAPPER.replace(
        "args = sys.argv[1:]",
        'args = sys.argv[1:]\n'
        'Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))\n'
        'print("CPU_ONLY_RECEIPT_DIAGNOSTIC", file=sys.stderr, flush=True)',
    ).replace('receipt_file.write_text(json.dumps(receipt))', write_receipt))
    result = invoke(local, "coding_text", params)
    context = _postlaunch_failure_context(local, params, result)
    assert result["error"].startswith(error_type + ":")
    assert "CPU_ONLY_RECEIPT_DIAGNOSTIC" in context["stderr_tail"]
    assert "CPU fixture log" in context["stdout_tail"]
    assert (Path(context["staging_dir"]) / "result.txt").is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(local.script.with_suffix(".pid").read_text()), 0)


def test_malformed_rc0_returns_postlaunch_context_without_publishing(local):
    params = coding_params(local)
    local.script.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        'Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))\n'
        'Path(args[args.index("--output") + 1]).write_text("CPU_ONLY_UNVERIFIED_OUTPUT")\n'
        'print("CPU_ONLY_PARSE_DIAGNOSTIC", file=sys.stderr, flush=True)\n'
        'print("CPU_ONLY_NOT_JSON", flush=True)\n'
    )
    result = invoke(local, "coding_text", params)
    context = _postlaunch_failure_context(local, params, result)
    assert result["error"] == "ValueError: local wrapper returned no final structured observation"
    assert context["stderr_tail"].endswith("CPU_ONLY_PARSE_DIAGNOSTIC\n")
    assert context["stdout_tail"].endswith("CPU_ONLY_NOT_JSON\n")
    assert (Path(context["staging_dir"]) / "result.txt").read_text() == "CPU_ONLY_UNVERIFIED_OUTPUT"
    with pytest.raises(ProcessLookupError):
        os.kill(int(local.script.with_suffix(".pid").read_text()), 0)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_postlaunch_base_exceptions_are_not_failure_receipts(local, monkeypatch, exception):
    import media_tools

    def interrupted(*args, **kwargs):
        raise exception("CPU_ONLY_PROCESS_CONTROL")

    monkeypatch.setattr(media_tools, "_bounded_media_process", interrupted)
    with pytest.raises(exception, match="CPU_ONLY_PROCESS_CONTROL"):
        invoke(local, "coding_text", coding_params(local))


def test_postpublication_cleanup_error_is_not_relabelled_as_generation_failure(local, monkeypatch):
    params = coding_params(local)
    remove = local.module.shutil.rmtree

    def cleanup(path, *args, **kwargs):
        if Path(path).parent.name == "native-local-model-calls":
            raise OSError("CPU_ONLY_POSTPUBLICATION_CLEANUP")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(local.module.shutil, "rmtree", cleanup)
    with pytest.raises(OSError, match="CPU_ONLY_POSTPUBLICATION_CLEANUP"):
        invoke(local, "coding_text", params)
    assert Path(params["output_file"]).read_bytes() == Path(params["prompt_file"]).read_bytes()
    assert json.loads(Path(params["output_file"] + ".receipt.json").read_text())["status"] == "success"


def test_cancellation_scope_is_thread_local_and_restores(local):
    current = session(local)
    assert current._cancel_check() is None
    seen = []
    with current.cancellation_scope(lambda: True):
        assert current._cancel_check()() is True
        thread = threading.Thread(target=lambda: seen.append(current._cancel_check()))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and seen == [None]
        result = invoke(local, "coding_text", coding_params(local), current)
        assert result["ok"] is False and result["status"] == "cancelled"
    assert current._cancel_check() is None


def test_native_and_shared_tool_entry_use_same_grants_without_process_env_borrow(local):
    import agent_tool_provider
    import legion_tool
    bound = {
        "MEYO_USER_ID": "alice", "MEYO_SESSION_ID": "same-original-session",
        "MEYO_WORKDIR": str(local.work), "MEYO_TASK_FACTS_JSON": "{}",
        "MEYO_PROJECT_CONTEXT_JSON": "{}",
        "MEYO_CAPABILITY_POLICY_JSON": json.dumps(local.context["capability_policy"]),
        "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(IDS),
        "MEYO_LOCAL_MODEL_CONFIG": json.dumps(local.config),
    }
    with agent_tool_provider._runtime_scope(bound, None, inherit_process_context=False):
        result = legion_tool._run_mcp_call("meyo_capabilities", "list_capabilities", {})
        assert result["ok"] is True, result
        assert {row["id"] for row in result["result"]["actions"]} == set(IDS)
        called = legion_tool._run_mcp_call("meyo_capabilities", "call_capability", {
            "tool_id": PREFIX + "coding_text", "params_json": json.dumps(coding_params(local)),
        })
        assert called["ok"] is True and called["result"]["session_id"] == "same-original-session"
    for key in ("MEYO_NATIVE_CAPABILITY_IDS", "MEYO_LOCAL_MODEL_CONFIG"):
        bound.pop(key)
    with agent_tool_provider._runtime_scope(bound, None, inherit_process_context=False):
        denied = legion_tool._run_mcp_call("meyo_capabilities", "list_capabilities", {})
    assert denied["ok"] is False, "HTTP request borrowed process-level capability grants"


def test_native_spec_keeps_original_identity_and_matches_local_call_budget(local):
    spec = local.module.native_stdio_spec(
        SimpleNamespace(**local.context, execution_depth="standard"),
        dict(os.environ), ORCH / "meyo_mcp_servers/orchestrator_server.py")
    assert spec["env"]["MEYO_LOCAL_MODEL_CONFIG"] == json.dumps(local.config)
    assert "OPENAI_API_KEY" not in spec["env"]
    assert spec["tool_timeout_sec"] >= local.config["timeout_s"] + local.config["term_grace_s"]
    assert json.loads(spec["env"]["MEYO_NATIVE_HOST_CONTEXT"])["session_id"] == "same-original-session"


def test_shared_discovery_includes_only_explicitly_bound_facade(local, monkeypatch):
    import agent_tool_provider
    import legion_tool
    monkeypatch.setattr(legion_tool, "_mcp_bridge_module",
                        lambda: SimpleNamespace(list_mcp_tools=lambda: []))
    with agent_tool_provider._runtime_scope(
            bound_environment(local), None, inherit_process_context=False):
        result = legion_tool._run_mcp_list()
    assert result["ok"] is True
    assert {row["name"] for row in result["tools"]} == {"list_capabilities", "call_capability"}
    assert all(row["server"] == "meyo_capabilities" for row in result["tools"])
    call = next(row for row in result["tools"] if row["name"] == "call_capability")
    assert set(call["input_schema"]["properties"]["tool_id"]["enum"]) == set(IDS)
    with agent_tool_provider._runtime_scope({}, None, inherit_process_context=False):
        denied = legion_tool._run_mcp_list()
    assert denied["tools"] == [], "discovery borrowed the process's local-model grants"


@pytest.mark.parametrize("deadline", ["0", "future", "expired"])
def test_native_local_deadline_uses_parent_monotonic_clock(local, monkeypatch, deadline):
    # legion_core._runtime_deadline uses monotonic seconds, not wall-clock epoch.
    value = ("0" if deadline == "0" else
             str(time.monotonic() + (5 if deadline == "future" else -1)))
    monkeypatch.setenv("MEYO_TASK_DEADLINE", value)
    params = coding_params(local)
    result = invoke(local, "coding_text", params)
    if deadline == "expired":
        assert result["status"] == "timeout" and result["ok"] is False
        assert not Path(params["output_file"]).exists()
    else:
        assert result["ok"] is True, result


def test_shared_run_uses_parent_local_budget_not_generic_future_timeout(local, monkeypatch):
    import agent_tool_provider
    import legion_tool
    monkeypatch.setattr(legion_tool, "_TOOL_DEADLINE_S", 0.001)
    monkeypatch.setattr(legion_tool, "_record_tool_usage", lambda *a, **kw: {})
    params = shared_coding_params(local)
    with agent_tool_provider._runtime_scope(
            bound_environment(local), None, inherit_process_context=False):
        result = legion_tool.run_tool("runnable:mcp_call", params)
    assert result["ok"] is True, result
    assert result["result"]["session_id"] == "same-original-session"


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_shared_run_stops_real_cpu_child_before_return(local, monkeypatch, stop):
    import agent_tool_provider
    import legion_tool
    monkeypatch.setattr(legion_tool, "_record_tool_usage", lambda *a, **kw: {})
    local.script.write_text(FAKE_WRAPPER.replace(
        'output = Path(arg("--output"))',
        'Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))\n'
        'import time; time.sleep(4)\noutput = Path(arg("--output"))'))
    params = shared_coding_params(local)
    cancelled = threading.Event()
    timer = threading.Timer(0.25, cancelled.set)
    events = []
    started = time.monotonic()
    if stop == "cancel":
        timer.start()
    try:
        with agent_tool_provider._runtime_scope(
                bound_environment(local), cancelled.is_set, inherit_process_context=False):
            result = legion_tool.run_tool("runnable:mcp_call", params,
                                          timeout_s=0.25 if stop == "timeout" else None,
                                          event_cb=events.append)
    finally:
        timer.cancel()
        if stop == "cancel":
            timer.join(timeout=2)
    assert time.monotonic() - started < 2.5, "thread-pool shutdown waited for the un-cancelled worker"
    assert result["ok"] is False, result
    if stop == "timeout":
        assert result.get("tool_timeout") is True or result.get("result", {}).get("status") == "timeout"
        timeout_events = [event for event in events if event.get("t") == "tool_timeout"]
        assert len(timeout_events) == 1
        assert timeout_events[0]["x"] == "runnable:mcp_call 已超时"
    else:
        assert result.get("result", {}).get("status") == "cancelled", result
    pid_file = local.script.with_suffix(".pid")
    assert pid_file.exists(), "the test did not launch its CPU child"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not (local.work / "result.txt").exists()


def test_shared_run_does_not_borrow_grants_from_process_in_empty_scope(local, monkeypatch):
    import agent_tool_provider
    import legion_tool
    import media_tools
    monkeypatch.setattr(legion_tool, "_record_tool_usage", lambda *a, **kw: {})
    monkeypatch.setattr(media_tools, "_bounded_media_process",
                        lambda *a, **kw: pytest.fail("missing parent grant launched a child"))
    with agent_tool_provider._runtime_scope({}, None, inherit_process_context=False):
        result = legion_tool.run_tool("runnable:mcp_call", shared_coding_params(local))
    assert result["ok"] is False


def test_missing_absolute_workspace_is_not_replaced_by_host_cwd(local):
    for workdir in ("", ".", "relative"):
        with pytest.raises(ValueError):
            local.module.CapabilitySession({**local.context, "workdir": workdir})


def test_shared_discovery_thread_keeps_explicit_empty_binding(local, monkeypatch):
    import agent_tool_provider
    import legion_tool
    monkeypatch.setattr(legion_tool, "_mcp_bridge_module",
                        lambda: SimpleNamespace(list_mcp_tools=lambda: []))
    monkeypatch.setattr(legion_tool, "_record_tool_usage", lambda *a, **kw: {})
    with agent_tool_provider._runtime_scope({}, None, inherit_process_context=False):
        result = legion_tool.run_tool("runnable:mcp_list", {})
    assert result["ok"] is True and result["tools"] == [], result


def test_coding_prompt_larger_than_argv_budget_uses_frozen_file(local):
    params = coding_params(local)
    raw = b"a" * min(os.sysconf("SC_ARG_MAX") + 4096, 1_900_000)
    Path(params["prompt_file"]).write_bytes(raw)
    result = invoke(local, "coding_text", params)
    assert result["ok"] is True, result
    assert Path(result["path"]).read_bytes() == raw


def test_failed_wrapper_returns_stderr_to_author_without_publishing(local):
    params = coding_params(local)
    local.script.write_text(
        'import sys\nsys.stderr.write("x" * 10000 + "\\nCPU_ONLY_MISSING_RUNTIME\\n")\n'
        'raise SystemExit(7)\n')
    result = invoke(local, "coding_text", params)
    assert result["ok"] is False and result["returncode"] == 7
    context = result["failure_context"]
    assert context["stderr_tail"].endswith("CPU_ONLY_MISSING_RUNTIME\n")
    assert len(context["stderr_tail"]) <= 8192 and context["stderr_truncated"] is True
    staging = Path(context["staging_dir"])
    assert staging.is_dir() and staging.stat().st_mode & 0o777 == 0o700
    assert (staging / "prompt.txt").read_bytes() == Path(params["prompt_file"]).read_bytes()
    assert not Path(params["output_file"]).exists()
    assert not Path(params["output_file"] + ".receipt.json").exists()


def test_failed_wrapper_retains_receipt_and_bounded_log_in_tool_result(local):
    params = coding_params(local)
    local.script.write_text(r'''
import json, sys
from pathlib import Path
args = sys.argv[1:]
output = Path(args[args.index("--output") + 1])
output.write_text("partial CPU output, not an artifact")
receipt = Path(str(output) + ".receipt.json")
receipt.write_text(json.dumps({"status": "failed", "error": "CPU_ONLY_WORKER_FAILED",
                              "output": str(output)}))
Path(str(output) + ".log").write_text("x" * 2_100_000 + "\nCPU_ONLY_LOG_END\n")
print(json.dumps({"status": "failed", "receipt": str(receipt),
                  "output": str(output), "error": "CPU_ONLY_WORKER_FAILED"}))
raise SystemExit(1)
''')
    result = invoke(local, "coding_text", params)
    assert result["ok"] is False and result["status"] == "failed"
    context = result["failure_context"]
    assert "CPU_ONLY_WORKER_FAILED" in context["files"]["receipt"]["tail"]
    log = context["files"]["log"]
    assert log["tail"].endswith("CPU_ONLY_LOG_END\n")
    assert len(log["tail"].encode()) <= 8192 and log["truncated"] is True
    assert Path(log["path"]).stat().st_size > 2_000_000
    assert Path(context["files"]["receipt"]["path"]).is_file()
    assert (Path(context["staging_dir"]) / "result.txt").is_file()
    assert not Path(params["output_file"]).exists()
    assert not Path(params["output_file"] + ".receipt.json").exists()


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_stopped_wrapper_returns_cleanup_diagnostics_after_process_exit(local, stop):
    params = coding_params(local)
    local.script.write_text(r'''
import os, signal, sys, time
from pathlib import Path
Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))
def cleanup(signum, frame):
    sys.stderr.write("CPU_ONLY_WRAPPER_CLEANED\n")
    sys.stderr.flush()
    raise SystemExit(1)
signal.signal(signal.SIGTERM, cleanup)
print("CPU_ONLY_WRAPPER_STARTED", file=sys.stderr, flush=True)
time.sleep(8)
''')
    if stop == "timeout":
        local.monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps(
            {**local.config, "timeout_s": 0.4}))
    current = session(local)
    cancel = lambda: stop == "cancel" and local.script.with_suffix(".pid").exists()
    with current.cancellation_scope(cancel):
        result = invoke(local, "coding_text", params, current)
    assert result["ok"] is False and result["status"] == (
        "cancelled" if stop == "cancel" else "timeout")
    context = result["failure_context"]
    assert "CPU_ONLY_WRAPPER_CLEANED" in context["stderr_tail"]
    assert Path(context["staging_dir"]).is_dir()
    with pytest.raises(ProcessLookupError):
        os.kill(int(local.script.with_suffix(".pid").read_text()), 0)
    assert not Path(params["output_file"]).exists()
    assert not list(Path(local.config["registry_dir"]).glob("*.ns-*.json"))


@pytest.mark.parametrize("outcome", ["success", "rejected"])
def test_local_staging_does_not_accumulate_success_or_prelaunch_rejection(local, outcome):
    params = coding_params(local)
    if outcome == "success":
        assert invoke(local, "coding_text", params)["ok"] is True
    else:
        Path(params["output_file"]).write_text("existing output must stay")
        with pytest.raises(ValueError):
            invoke(local, "coding_text", params)
        assert Path(params["output_file"]).read_text() == "existing output must stay"
    control = Path(local.config["state_dir"]) / "run/native-local-model-calls"
    assert not control.exists() or not list(control.iterdir())


def test_failed_wrapper_diagnostics_do_not_follow_receipt_or_log_symlinks(local):
    params = coding_params(local)
    secret = local.script.with_suffix(".private")
    secret.write_text("MUST_NOT_BE_READ_AS_DIAGNOSTICS")
    local.script.write_text(r'''
import sys
from pathlib import Path
args = sys.argv[1:]
output = Path(args[args.index("--output") + 1])
for suffix in (".receipt.json", ".log"):
    Path(str(output) + suffix).symlink_to(Path(__file__).with_suffix(".private"))
raise SystemExit(1)
''')
    result = invoke(local, "coding_text", params)
    assert result["ok"] is False
    assert "MUST_NOT_BE_READ_AS_DIAGNOSTICS" not in json.dumps(result)
    assert result["failure_context"]["files"]["receipt"]["read_error"]
    assert result["failure_context"]["files"]["log"]["read_error"]
