"""A Git-free core package can cite its own versioned bytes, not arbitrary roots."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1]
REVISION = "a3" * 20
SOURCE_PATH = "release/legion-service/example.py"
SOURCE = b'# audit fixture\r\nvalue = "unchanged"\r\n'


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(SERVICE_DIR))


def write_manifest(root, *, revision=REVISION, source=SOURCE):
    target = root / SOURCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    algorithm = "sha1" if len(revision) == 40 else "sha256"
    manifest = {
        "format": "meyo-private-core-v1",
        "source_commit": revision,
        "distribution": "private-local-only",
        "public_license_granted": False,
        "member_execution_verified": False,
        "files": [{
            "path": SOURCE_PATH, "mode": "100644", "size": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
            "git_blob": hashlib.new(
                algorithm, f"blob {len(source)}\0".encode() + source
            ).hexdigest(),
        }],
    }
    save_manifest(root, manifest)
    return manifest


def save_manifest(root, manifest):
    # Independent fixture encoding; deliberately do not call the parser under test.
    (root / "CORE-PACKAGE.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def audit(tmp_path, monkeypatch, isolated_environment):
    spec = importlib.util.spec_from_file_location(
        "__ut_core_package_audit", SERVICE_DIR / "source_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "core"
    write_manifest(root)
    monkeypatch.setattr(module, "_REPO", root)
    return module, root


def check(audit, **overrides):
    module, root = audit
    arguments = {
        "source_root": str(root), "file": SOURCE_PATH, "line": 2,
        "quote": 'value = "unchanged"', "revision": REVISION,
    }
    arguments.update(overrides)
    return module.verify_reference(**arguments)


def test_git_free_reference_verifies_exact_bytes_and_saves_original_ledger(audit):
    module, root = audit
    result = module.record({
        "audit_id": "package-check",
        "member": "core", "dimension": "self-source",
        "source_root": str(root / "release/legion-service"),
        "file": "example.py", "line": 2, "quote": 'value = "unchanged"',
        "revision": REVISION, "status": "claimed", "verifier": "unit-test",
    }, uid="__ut_package")

    assert result["ok"] is True
    assert result["entry"]["revision"] == REVISION
    assert result["entry"]["verification"]["content_source"] == f"core-package:{REVISION}"
    assert result["entry"]["verification"]["content_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert result["entry"]["runtime_evidence"] == "runtime_not_tested"
    assert result["learning"]["recorded"] is False
    assert module.show("package-check", uid="__ut_package")["entry"] == result["entry"]
    assert not (root / ".git").exists()


def test_sha256_git_object_format_is_supported_without_reinterpreting_version(audit):
    _, root = audit
    revision = "b4" * 32
    write_manifest(root, revision=revision)

    result = check(audit, revision=revision)

    assert result["ok"] is True
    assert result["revision"] == revision


@pytest.mark.parametrize("revision", ["HEAD", REVISION[:12], "b4" * 20])
def test_requested_revision_must_match_full_package_commit(audit, revision):
    assert check(audit, revision=revision)["ok"] is False


@pytest.mark.parametrize("line,quote", [(0, ""), (3, ""), (2, "different")])
def test_hash_valid_source_still_requires_real_line_and_quote(audit, line, quote):
    assert check(audit, line=line, quote=quote)["ok"] is False


@pytest.mark.parametrize("change", ["same_size", "newline_conversion", "git_blob", "missing_record"])
def test_file_drift_or_missing_manifest_record_is_not_verified(audit, change):
    _, root = audit
    marker = root / "CORE-PACKAGE.json"
    manifest = json.loads(marker.read_text())
    if change == "same_size":
        (root / SOURCE_PATH).write_bytes(SOURCE.replace(b"unchanged", b"rewritten"))
    elif change == "newline_conversion":
        (root / SOURCE_PATH).write_bytes(SOURCE.replace(b"\r\n", b"\n"))
    elif change == "git_blob":
        manifest["files"][0]["git_blob"] = "0" * 40
        save_manifest(root, manifest)
    else:
        manifest["files"][0]["path"] = "other.py"
        save_manifest(root, manifest)

    assert check(audit)["ok"] is False


@pytest.mark.parametrize("damage", ["duplicate_key", "duplicate_path", "extra_field", "invalid_json"])
def test_bad_manifest_returns_refusal_without_falling_back_to_git(audit, monkeypatch, damage):
    module, root = audit
    marker = root / "CORE-PACKAGE.json"
    manifest = json.loads(marker.read_text())
    if damage == "duplicate_key":
        marker.write_text(marker.read_text().replace(
            '"format":', '"format":"meyo-private-core-v1","format":', 1
        ))
    elif damage == "duplicate_path":
        manifest["files"].append(dict(manifest["files"][0]))
        save_manifest(root, manifest)
    elif damage == "extra_field":
        manifest["export_anything"] = True
        save_manifest(root, manifest)
    else:
        marker.write_text("{not-json")

    def no_git_fallback(*a, **kw):
        raise AssertionError("a declared core package must not fall through to Git")

    monkeypatch.setattr(module.subprocess, "run", no_git_fallback)
    assert check(audit)["ok"] is False


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo", "oversized"])
def test_nonregular_or_oversized_manifest_is_refused(audit, tmp_path, kind):
    _, root = audit
    marker = root / "CORE-PACKAGE.json"
    original = marker.read_bytes()
    marker.unlink()
    if kind == "symlink":
        other = tmp_path / "manifest-copy.json"
        other.write_bytes(original)
        marker.symlink_to(other)
    elif kind == "dangling":
        marker.symlink_to(tmp_path / "not-there.json")
    elif kind == "directory":
        marker.mkdir()
    elif kind == "fifo":
        os.mkfifo(marker)
    else:
        with marker.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)

    assert check(audit)["ok"] is False


@pytest.mark.parametrize("kind", ["leaf", "parent"])
def test_packaged_source_symlink_is_not_verified_even_inside_allowed_root(audit, kind):
    _, root = audit
    target = root / SOURCE_PATH
    if kind == "leaf":
        other = root / "original.py"
        target.rename(other)
        target.symlink_to(other)
    else:
        parent = target.parent
        other = root / "original-directory"
        parent.rename(other)
        parent.symlink_to(other, target_is_directory=True)

    assert check(audit)["ok"] is False


def test_manifest_in_another_allowed_root_does_not_grant_core_identity(audit):
    module, _ = audit
    other = Path(module._paths.repos_dir()) / "unrelated"
    write_manifest(other)

    assert check(audit, source_root=str(other))["ok"] is False


def test_validator_is_not_executed_from_the_audited_root(audit):
    _, root = audit
    script = root / "scripts/package_core.py"
    script.parent.mkdir()
    script.write_text('raise RuntimeError("do not execute audit input")\n')

    assert check(audit)["ok"] is True


def test_declared_peer_archive_keeps_its_own_revision(audit):
    _, root = audit
    peer = root / "runtime/core-sources/example-peer"
    peer.mkdir(parents=True)
    (peer / "example.py").write_bytes(SOURCE)
    (peer / ".meyo-source.json").write_text(json.dumps({
        "revision": REVISION,
        "files": {"example.py": {"sha256": hashlib.sha256(SOURCE).hexdigest()}},
    }))

    result = check(audit, source_root=str(peer), file="example.py")

    assert result["ok"] is True
    assert result["content_source"] == f"archive:{REVISION}"
    assert result["content_sha256"] == hashlib.sha256(SOURCE).hexdigest()


def test_nested_git_peer_is_not_misidentified_as_part_of_the_core_package(audit):
    _, root = audit
    peer = root / "harness_baselines/__ut_peer"
    peer.mkdir(parents=True)
    (peer / "example.py").write_bytes(SOURCE)
    for command in [
        ["init", "-q"],
        ["add", "example.py"],
        ["-c", "user.name=Unit Test", "-c", "user.email=unit@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "peer fixture"],
    ]:
        subprocess.run(["git", *command], cwd=peer, check=True,
                       capture_output=True, timeout=10)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=peer, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()

    result = check(audit, source_root=str(peer), file="example.py", revision=revision)

    assert result["ok"] is True
    assert result["content_source"] == f"git:{revision}"


def test_git_worktree_keeps_revision_based_behavior(audit):
    _, root = audit
    (root / "CORE-PACKAGE.json").unlink()
    for command in [
        ["init", "-q"],
        ["add", SOURCE_PATH],
        ["-c", "user.name=Unit Test", "-c", "user.email=unit@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
    ]:
        subprocess.run(["git", *command], cwd=root, check=True,
                       capture_output=True, timeout=10)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    (root / SOURCE_PATH).write_text("modified worktree\n")

    result = check(audit, revision=revision)

    assert result["ok"] is True
    assert result["content_source"] == f"git:{revision}"
    assert result["actual"] == 'value = "unchanged"'


def add_manifest_file(root, path, source):
    manifest = json.loads((root / "CORE-PACKAGE.json").read_bytes())
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    manifest["files"].append({
        "path": path, "mode": "100644", "size": len(source),
        "sha256": hashlib.sha256(source).hexdigest(),
        "git_blob": hashlib.sha1(f"blob {len(source)}\0".encode() + source).hexdigest(),
    })
    manifest["files"].sort(key=lambda item: item["path"])
    save_manifest(root, manifest)


def test_prior_art_reads_versioned_package_without_git_or_rg(audit, monkeypatch):
    module, root = audit
    # Unindexed local files must not acquire the package's identity.
    (root / "private-local.txt").write_text("unchanged")
    (root / "scripts").mkdir()
    (root / "scripts/package_core.py").write_text('raise RuntimeError("audit input")')

    def no_external_index(*args, **kwargs):
        raise AssertionError("a source package needs neither Git nor ripgrep")

    monkeypatch.setattr(module.subprocess, "run", no_external_index)
    result = module.discover_prior_art(["UNCHANGED"])

    assert result["ok"] is True
    assert result["current_source_and_entrypoints"] == [{
        "path": SOURCE_PATH, "queries": ["UNCHANGED"], "revision": REVISION,
        "content_source": f"core-package:{REVISION}",
        "content_sha256": hashlib.sha256(SOURCE).hexdigest(),
    }]
    search = result["observations"]["source_search"]
    assert search["backend"] == "core_package_manifest"
    assert search["status"] == "complete"
    assert search["scanned_files"] == 1
    assert search["bytes_read_upper_bound"] == len(SOURCE) + 1
    assert result["observations"]["git_history"]["status"] == "not_in_package"
    assert result["git_history"] == []
    assert result["decision_contract"]["choices"] == ["reuse", "extend", "replace"]


def test_prior_art_uses_whole_package_index_not_old_developer_roots(audit):
    module, root = audit
    other = "harness_baselines/meyo-orchestrator/example.py"
    add_manifest_file(root, other, b"# unique shared mechanism\n")
    add_manifest_file(root, "docs/evidence-notes.md", b"# Saved filename only\n")
    add_manifest_file(root, "docs/handoff-notes.md", b"# Saved filename only\n")

    result = module.discover_prior_art("unique shared mechanism")

    assert [row["path"] for row in result["current_source_and_entrypoints"]] == [other]
    assert result["existing_results_and_evidence"] == [{
        "path": "docs/evidence-notes.md", "source": "package_filename_index",
    }]
    assert result["checkpoint_candidates"] == ["docs/handoff-notes.md"]
    assert result["observations"]["source_search"]["scanned_files"] == 4


def test_prior_art_recognizes_plan_directory_without_leaf_name_keywords(audit):
    module, root = audit
    plan_path = "docs/plans/2026-10-19-member-owned-experience-loop.md"
    add_manifest_file(root, plan_path, b"# Existing implementation decision\n")
    add_manifest_file(root, "docs/examples/member-owned-experience-loop.md", b"# Example\n")

    result = module.discover_prior_art("unchanged")

    assert result["checkpoint_candidates"] == [plan_path]
    assert result["observations"]["checkpoint_candidates"]["status"] == "filename_index_only"
    assert result["observations"]["checkpoint_candidates"]["truncated"] is False


@pytest.mark.parametrize("damage", [
    "invalid_json", "duplicate_key", "unsafe_path", "symlink", "fifo", "oversized",
])
def test_prior_art_invalid_manifest_never_falls_back_to_unindexed_files(
    audit, monkeypatch, tmp_path, damage
):
    module, root = audit
    marker = root / "CORE-PACKAGE.json"
    encoded = marker.read_bytes()
    if damage == "invalid_json":
        marker.write_text("{invalid")
    elif damage == "duplicate_key":
        marker.write_bytes(encoded.replace(b'"format":', b'"format":"other","format":', 1))
    elif damage == "unsafe_path":
        manifest = json.loads(encoded)
        manifest["files"][0]["path"] = "../private.txt"
        save_manifest(root, manifest)
    elif damage == "oversized":
        with marker.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    else:
        marker.unlink()
        if damage == "symlink":
            other = tmp_path / "manifest-copy.json"
            other.write_bytes(encoded)
            marker.symlink_to(other)
        else:
            os.mkfifo(marker)

    def no_fallback(*args, **kwargs):
        raise AssertionError("invalid package metadata is not a Git search")

    monkeypatch.setattr(module.subprocess, "run", no_fallback)
    result = module.discover_prior_art("unchanged")

    assert result["ok"] is False
    assert result["current_source_and_entrypoints"] == []
    assert result["observations"]["source_search"]["status"] == "failed"
    assert result["error_code"].startswith("core_package_manifest_")
    assert result["error"]
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("damage", ["bytes", "git_blob", "leaf_symlink", "parent_symlink", "fifo"])
def test_prior_art_reports_unreadable_or_drifted_files_without_false_matches(audit, damage):
    module, root = audit
    target = root / SOURCE_PATH
    if damage == "bytes":
        target.write_bytes(SOURCE.replace(b"unchanged", b"rewritten"))
    elif damage == "git_blob":
        manifest = json.loads((root / "CORE-PACKAGE.json").read_bytes())
        manifest["files"][0]["git_blob"] = "0" * 40
        save_manifest(root, manifest)
    elif damage == "leaf_symlink":
        other = root / "copy.py"
        target.rename(other)
        target.symlink_to(other)
    elif damage == "parent_symlink":
        other = root / "copy"
        target.parent.rename(other)
        target.parent.symlink_to(other, target_is_directory=True)
    else:
        target.unlink()
        os.mkfifo(target)

    result = module.discover_prior_art(["unchanged", "rewritten"])

    assert result["ok"] is False
    assert result["current_source_and_entrypoints"] == []
    search = result["observations"]["source_search"]
    assert search["status"] == "partial"
    assert search["failed_files"] == 1
    assert search["issues"][0]["path"] == SOURCE_PATH
    assert search["issues"][0]["reason"]


def test_prior_art_keeps_healthy_matches_when_another_file_fails(audit):
    module, root = audit
    add_manifest_file(root, "scripts/missing.py", b"unchanged")
    (root / "scripts/missing.py").unlink()

    result = module.discover_prior_art("unchanged")

    assert result["ok"] is False
    assert [row["path"] for row in result["current_source_and_entrypoints"]] == [SOURCE_PATH]
    assert result["observations"]["source_search"]["failed_files"] == 1


def test_prior_art_result_limit_is_not_confused_with_complete_content_scan(audit):
    module, root = audit
    add_manifest_file(root, "scripts/second.py", b"unchanged")

    result = module.discover_prior_art("unchanged", limit=1)

    assert len(result["current_source_and_entrypoints"]) == 1
    search = result["observations"]["source_search"]
    assert search["status"] == "complete"
    assert search["scanned_files"] == 2
    assert search["matched_files"] == 2
    assert search["result_truncated"] is True


@pytest.mark.parametrize("bound", ["file", "total", "time"])
def test_prior_art_scan_limits_are_reported_not_disguised_as_no_matches(audit, monkeypatch, bound):
    module, _ = audit
    setting, value = {
        "file": ("_PRIOR_ART_FILE_BYTES", len(SOURCE) - 1),
        "total": ("_PRIOR_ART_TOTAL_BYTES", len(SOURCE) - 1),
        "time": ("_PRIOR_ART_SECONDS", 0),
    }[bound]
    monkeypatch.setattr(module, setting, value, raising=False)

    result = module.discover_prior_art("unchanged")

    assert result["ok"] is True
    assert result["current_source_and_entrypoints"] == []
    search = result["observations"]["source_search"]
    assert search["status"] == "partial"
    assert search["scanned_files"] == 0
    assert search["skipped_files"] == 1
    assert search["bytes_read_upper_bound"] == 0
    assert search["issues"][0]["reason"]


def test_prior_art_true_no_match_is_distinct_from_unavailable_history(audit):
    module, _ = audit
    result = module.discover_prior_art("not in this file")

    assert result["ok"] is True
    assert result["current_source_and_entrypoints"] == []
    assert result["observations"]["source_search"]["status"] == "complete"
    assert result["observations"]["source_search"]["matched_files"] == 0
    assert result["observations"]["git_history"]["status"] == "not_in_package"


def test_checkout_missing_optional_roots_do_not_discard_real_rg_matches(audit, monkeypatch):
    if not shutil.which("rg"):
        pytest.skip("checkout ripgrep regression requires ripgrep")
    module, root = audit
    (root / "CORE-PACKAGE.json").unlink()

    def no_masking_git_fallback(*args, **kwargs):
        raise AssertionError("valid ripgrep matches must not be discarded")

    monkeypatch.setattr(module, "_git_lines", no_masking_git_fallback)
    assert module._current_asset_paths(["unchanged"], 40) == [SOURCE_PATH]


def test_checkout_evidence_index_ignores_absent_optional_roots(audit):
    if not shutil.which("rg"):
        pytest.skip("checkout ripgrep regression requires ripgrep")
    module, root = audit
    path = root / "harness_baselines/comparison/result.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    assert module._evidence_paths(40) == ["harness_baselines/comparison/result.json"]


@pytest.fixture
def public_audit(audit):
    """Independent public-format fixture; no license is assigned to this product."""
    _, root = audit
    add_manifest_file(root, "LICENSE", b"Fictional QA terms; not a real product license.\n")
    add_manifest_file(root, "THIRD_PARTY_NOTICES.md", b"Fictional fixture notices only.\n")
    manifest = json.loads((root / "CORE-PACKAGE.json").read_bytes())
    del manifest["public_license_granted"]
    manifest.update(
        format="meyo-public-core-v1", distribution="public-source", rights_verified=False,
        license={
            "identifier": "LicenseRef-QA-Fixture", "file": "LICENSE",
            "third_party_notices": ["THIRD_PARTY_NOTICES.md"],
        },
    )
    save_manifest(root, manifest)
    return audit


def test_public_package_reference_keeps_the_original_ledger_and_version(public_audit):
    module, root = public_audit
    before = (root / "CORE-PACKAGE.json").read_bytes()
    result = module.record({
        "audit_id": "public-package-check",
        "member": "core", "dimension": "self-source",
        "source_root": str(root), "file": SOURCE_PATH, "line": 2,
        "quote": 'value = "unchanged"', "revision": REVISION,
        "status": "claimed", "verifier": "unit-test",
    }, uid="__ut_public_package")

    assert result["ok"] is True
    verification = result["entry"]["verification"]
    assert verification["content_source"] == f"core-package:{REVISION}"
    assert verification["content_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert result["entry"]["runtime_evidence"] == "runtime_not_tested"
    assert result["learning"]["recorded"] is False
    assert module.show("public-package-check", uid="__ut_public_package")["entry"] == result["entry"]
    assert (root / "CORE-PACKAGE.json").read_bytes() == before
    assert not (root / ".git").exists()


def test_public_package_prior_art_uses_the_same_trusted_validator(public_audit, monkeypatch):
    module, root = public_audit
    (root / "scripts").mkdir()
    (root / "scripts/package_core.py").write_text('raise RuntimeError("audit input")\n')
    (root / "private-local.txt").write_text("unchanged but not in the manifest\n")

    def no_external_lookup(*args, **kwargs):
        raise AssertionError("public package source lookup must not require Git or a runner")

    monkeypatch.setattr(module.subprocess, "run", no_external_lookup)
    result = module.discover_prior_art("unchanged")

    assert result["ok"] is True
    assert result["observations"]["source_search"]["backend"] == "core_package_manifest"
    assert result["observations"]["source_search"]["status"] == "complete"
    assert result["observations"]["git_history"]["status"] == "not_in_package"
    assert result["current_source_and_entrypoints"] == [{
        "path": SOURCE_PATH, "queries": ["unchanged"], "revision": REVISION,
        "content_source": f"core-package:{REVISION}",
        "content_sha256": hashlib.sha256(SOURCE).hexdigest(),
    }]


@pytest.mark.parametrize("damage", [
    "rights-claim", "legacy-grant", "old-format", "missing-license-reference",
    "missing-notice-reference", "source-bytes",
])
def test_public_package_invalid_facts_do_not_become_verified_citations(
    public_audit, monkeypatch, damage,
):
    module, root = public_audit
    manifest = json.loads((root / "CORE-PACKAGE.json").read_bytes())
    if damage == "rights-claim":
        manifest["rights_verified"] = True
    elif damage == "legacy-grant":
        manifest["public_license_granted"] = True
    elif damage == "old-format":
        manifest["format"] = "meyo-private-core-v1"
    elif damage == "missing-license-reference":
        manifest["license"]["file"] = "not-in-package"
    elif damage == "missing-notice-reference":
        manifest["license"]["third_party_notices"] = ["not-in-package"]
    else:
        (root / SOURCE_PATH).write_bytes(SOURCE.replace(b"unchanged", b"rewritten"))
    save_manifest(root, manifest)

    def no_fallback(*args, **kwargs):
        raise AssertionError("invalid public package must not fall through to Git")

    monkeypatch.setattr(module.subprocess, "run", no_fallback)
    assert check(public_audit)["ok"] is False
    result = module.discover_prior_art("unchanged")
    assert result["ok"] is False
    assert result["current_source_and_entrypoints"] == []


def test_public_archive_roundtrip_is_consumable_by_original_source_audit(audit, tmp_path, monkeypatch):
    module, _ = audit
    repository = tmp_path / "fictional-repository"
    target = repository / SOURCE_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE)
    (repository / "LICENSE").write_bytes(b"Fictional QA terms; not a real product license.\n")
    (repository / "THIRD_PARTY_NOTICES.md").write_bytes(b"Fictional fixture notices only.\n")
    for command in [
        ["init", "-q"], ["add", "."],
        ["-c", "user.name=Unit Test", "-c", "user.email=unit@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "fictional public-source fixture"],
    ]:
        subprocess.run(["git", *command], cwd=repository, check=True,
                       capture_output=True, timeout=10)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    validator = module._core_package_validator()
    archive, extracted = tmp_path / "public-source.tar", tmp_path / "public-source"
    validator.build_package(
        repository, revision, archive, distribution="public-source",
        license_id="LicenseRef-QA-Fixture", license_file="LICENSE",
        notice_files=["THIRD_PARTY_NOTICES.md"],
    )
    manifest = validator.extract_package(archive, extracted)
    monkeypatch.setattr(module, "_REPO", extracted)
    repository.rename(tmp_path / "old-repository")

    def no_git(*args, **kwargs):
        raise AssertionError("an extracted source package must not consult its old repository")

    monkeypatch.setattr(module.subprocess, "run", no_git)
    result = check((module, extracted), revision=revision)
    assert result["ok"] is True
    assert result["content_source"] == f"core-package:{revision}"
    assert result["content_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert manifest["rights_verified"] is False
    assert manifest["member_execution_verified"] is False
    assert "public_license_granted" not in manifest
    assert not (extracted / ".git").exists()
