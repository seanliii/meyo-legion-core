"""真实临时文件验证；不启动 JCode、不联网、不读用户历史。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


MODEL = "all-MiniLM-L6-v2"
PAYLOADS = {"model.onnx": b"public model fixture\n",
            "tokenizer.json": b'{"version":"public-tokenizer-fixture"}\n'}
RECEIPT = ".legion-public-model-cache.json"


@pytest.fixture
def cache_case(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(service))
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "meyo"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "legion"))
    monkeypatch.delenv("LEGION_JCODE_PUBLIC_MODEL_CACHE", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("public-model preparation must not start a process or use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module = importlib.import_module("external_adapters.jcode_native")
    # Small real files exercise the same pinned-hash copy path without copying
    # the 90 MB production weight for every negative test.
    pins = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in PAYLOADS.items()}
    monkeypatch.setattr(module, "_PUBLIC_MODEL_ASSETS", pins, raising=False)
    source = tmp_path / "public-cache"
    source.mkdir()
    for name, data in PAYLOADS.items():
        (source / name).write_bytes(data)
    (source / "not-a-model-private-history.txt").write_text("must not be copied")
    monkeypatch.setenv("LEGION_JCODE_PUBLIC_MODEL_CACHE", str(source))
    return SimpleNamespace(module=module, source=source, root=tmp_path, mp=monkeypatch)


def prepare(case, name="private-home", **ctx_extra):
    ctx = SimpleNamespace(memory_dir=str(case.root / name), gateway="https://fixture.invalid/v1",
                          model="fixture-model", appid="fixture-key", **ctx_extra)
    return case.module.prepare(ctx, {})


def test_prepare_copies_only_verified_pair_and_records_actual_file_receipt(cache_case):
    case = cache_case
    env, home, sid = prepare(case)
    model = home / "models" / MODEL
    assert sid == "" and env["JCODE_HOME"] == str(home)
    for name, data in PAYLOADS.items():
        copied = model / name
        assert copied.read_bytes() == data
        assert not copied.is_symlink()
        assert (copied.stat().st_dev, copied.stat().st_ino) != (
            (case.source / name).stat().st_dev, (case.source / name).stat().st_ino)
    receipt = json.loads((model / RECEIPT).read_text())
    assert receipt["status"] == "seeded" and receipt["method"] == "copy"
    assert {row["name"]: row["sha256"] for row in receipt["assets"]} == {
        name: hashlib.sha256(data).hexdigest() for name, data in PAYLOADS.items()}
    assert set(p.name for p in model.iterdir()) == {*PAYLOADS, RECEIPT}
    assert json.loads(env["LEGION_JCODE_PUBLIC_MODEL_CACHE_RESULT"]) == receipt


def test_two_private_homes_do_not_share_writable_inodes_or_memory(cache_case):
    case = cache_case
    _, first, _ = prepare(case, "user-a")
    _, second, _ = prepare(case, "user-b")
    one = first / "models" / MODEL / "model.onnx"
    two = second / "models" / MODEL / "model.onnx"
    assert one.read_bytes() == two.read_bytes() == PAYLOADS["model.onnx"]
    assert one.stat().st_ino != two.stat().st_ino
    one.write_bytes(b"private change")
    assert two.read_bytes() == (case.source / "model.onnx").read_bytes() == PAYLOADS["model.onnx"]
    assert not (first / "sessions").exists() and not (second / "sessions").exists()


@pytest.mark.parametrize("existing", ["custom-pair", "partial", "empty-directory"])
def test_existing_native_or_custom_model_is_never_overwritten(cache_case, existing):
    case = cache_case
    target = case.root / "private-home/models" / MODEL
    target.mkdir(parents=True)
    if existing != "empty-directory":
        (target / "model.onnx").write_bytes(b"custom model")
    if existing == "custom-pair":
        (target / "tokenizer.json").write_bytes(b"custom tokenizer")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    inode = target.stat().st_ino
    prepare(case)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    assert target.stat().st_ino == inode


@pytest.mark.parametrize("invalid", ["missing-pair", "wrong-hash", "symlink-file", "relative-source"])
def test_invalid_or_unapproved_cache_leaves_native_fallback_unchanged(cache_case, invalid):
    case = cache_case
    if invalid == "missing-pair":
        (case.source / "tokenizer.json").unlink()
    elif invalid == "wrong-hash":
        (case.source / "model.onnx").write_bytes(b"!" * len(PAYLOADS["model.onnx"]))
    elif invalid == "symlink-file":
        other = case.root / "other-public-file"
        other.write_bytes(PAYLOADS["model.onnx"])
        (case.source / "model.onnx").unlink()
        (case.source / "model.onnx").symlink_to(other)
    else:
        case.mp.setenv("LEGION_JCODE_PUBLIC_MODEL_CACHE", "relative-cache")
    env, home, _ = prepare(case)
    assert env["JCODE_HOME"] == str(home)
    assert not (home / "models" / MODEL).exists()
    assert not list(home.glob("models/.legion-model-*"))


def test_task_text_cannot_choose_a_cache_and_unconfigured_behavior_stays_native(cache_case):
    case = cache_case
    case.mp.delenv("LEGION_JCODE_PUBLIC_MODEL_CACHE")
    env, home, _ = prepare(case, task_facts={"LEGION_JCODE_PUBLIC_MODEL_CACHE": str(case.source)})
    assert env["JCODE_HOME"] == str(home) and not (home / "models").exists()
    assert env["JCODE_OPENAI_COMPAT_DEFAULT_MODEL"] == "fixture-model"
    assert not any(key.startswith("JCODE_MEMORY") for key in env)


def test_symlinked_models_root_is_not_written_through(cache_case):
    case = cache_case
    outside = case.root / "outside"
    outside.mkdir()
    home = case.root / "private-home"
    home.mkdir()
    (home / "models").symlink_to(outside, target_is_directory=True)
    prepare(case)
    assert list(outside.iterdir()) == []


def test_concurrent_prepare_publishes_one_complete_pair_without_replacement(cache_case):
    case = cache_case
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(case), range(2)))
    target = results[0][1] / "models" / MODEL
    assert {name: (target / name).read_bytes() for name in PAYLOADS} == PAYLOADS
    assert json.loads((target / RECEIPT).read_text())["status"] == "seeded"
    assert not list(target.parent.glob(".legion-model-*"))
