#!/usr/bin/env python3
"""Prepare an offline two-package identity and collect matching acceptance receipts.

Does not build, install, run tests/models, fetch, commit or publish. Supply the
existing trusted core packager, original archives and original npm pack --json
output. A prepared pair is not a runtime, rights or public-history certification.
Optional source provenance compares pinned Git trees; it does not migrate or sync.
"""

import argparse
import base64
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_packager(path):
    # This is an explicitly trusted LOCAL tool, never code taken out of an archive.
    spec = importlib.util.spec_from_file_location("trusted_core_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_bytes(packager, path, limit):
    with packager._regular(path, limit) as source:
        data = source.read(limit + 1)
    require(len(data) <= limit, "input exceeds size bound")
    return data


def read_json(packager, path):
    data = read_bytes(packager, path, packager.MAX_METADATA_BYTES)
    return json.loads(data, object_pairs_hook=packager._unique_json), data


def source_identity(packager, repo, commit):
    # Reuse the original offline Git/path/blob handling. No live checkout overlay.
    actual_root = packager._git(repo, ["rev-parse", "--show-toplevel"]).decode().strip()
    require(Path(actual_root).resolve() == repo.resolve(), "source must be the Git repository root")
    entries = packager._read_tree(repo, commit)
    tree = packager._git(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"]).decode().strip()
    tags = packager._git(repo, ["tag", "--points-at", commit]).decode().splitlines()
    return entries, tree, sorted(tags)


def verify_connector_source(packager, args, core_entries, connector_entries, connector_tree):
    path = getattr(args, "connector_source_path", None)
    if path is None:
        return {"status": "not_verified", "reason": "connector_source_path_not_supplied"}
    # Keep the raw POSIX-relative path: Path.resolve/normpath would hide traversal
    # or aliases. The original packager rejects unsafe/noncanonical source paths.
    packager.validate_source_path(path)
    prefix = path + "/"
    subtree = {
        name[len(prefix):]: {**entry, "path": name[len(prefix):]}
        for name, entry in core_entries.items() if name.startswith(prefix)
    }
    require(bool(subtree), "connector source path must identify a nonempty source subtree")
    require(subtree == connector_entries,
            "connector source subtree paths/modes/blobs/sizes do not match the connector commit")
    tree = packager._git(args.core_repo, [
        "rev-parse", "--verify", f"{args.core_commit}:{path}",
    ]).decode().strip()
    require(packager._git(args.core_repo, ["cat-file", "-t", tree]).strip() == b"tree",
            "connector source path is not a Git tree")
    require(tree == connector_tree, "connector source subtree tree does not match the connector root tree")
    return {
        "status": "verified", "method": "core_subtree_equals_connector_root",
        "core_commit": args.core_commit, "connector_source_path": path, "core_subtree_tree": tree,
        "connector_commit": args.connector_commit, "connector_tree": connector_tree, "files": len(subtree),
    }


def verify_connector(packager, args, entries):
    archive = read_bytes(packager, args.connector_archive, packager.MAX_FILE_BYTES)
    require(sha256(archive) == args.connector_sha256, "connector archive SHA256 mismatch")
    report, report_bytes = read_json(packager, args.connector_pack_report)
    require(isinstance(report, list) and len(report) == 1 and isinstance(report[0], dict),
            "expected the original one-package npm pack --json report")
    report = report[0]
    require(report.get("size") == len(archive)
            and report.get("shasum") == hashlib.sha1(archive).hexdigest()
            and report.get("integrity") == "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode(),
            "npm pack size/integrity does not identify this archive")
    files, native_files, manifest = {}, [], None
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            require(member.isfile() and member.name.startswith("package/"), "connector contains a non-file or non-package entry")
            name = member.name[len("package/"):]
            packager.validate_source_path(name)
            require(name not in files, "duplicate connector file")
            total += member.size
            require(0 <= member.size <= packager.MAX_FILE_BYTES and total <= packager.MAX_TOTAL_BYTES
                    and len(files) < packager.MAX_FILES, "connector exceeds file/count/size bounds")
            require(name != "meyo.py" and not name.startswith(("release/legion-service/", "runtime/core-sources/", "harness_baselines/")),
                    "core source must not be copied into the connector")
            data = tar.extractfile(member).read()
            require(len(data) == member.size, "truncated connector file")
            mode = "100755" if member.mode & 0o111 else "100644"
            blob = hashlib.new(packager._algorithm(args.connector_commit),
                               f"blob {len(data)}\0".encode() + data).hexdigest()
            require(entries.get(name) == {"path": name, "mode": mode, "git_blob": blob, "size": len(data)},
                    f"connector source/blob mismatch: {name}")
            files[name] = {"path": name, "sha256": sha256(data), "bytes": len(data), "mode": mode}
            native_files.append({"path": name, "size": len(data), "mode": member.mode & 0o777})
            if name == "package.json":
                manifest = json.loads(data, object_pairs_hook=packager._unique_json)
    packager._validate_path_set(files)
    # npm's display order need not be Python's code-point order (e.g. docs/media vs docs/PLAN).
    require(sorted(report.get("files", []), key=lambda row: row["path"]) == sorted(native_files, key=lambda row: row["path"])
            and report.get("entryCount") == len(files) and report.get("unpackedSize") == total
            and report.get("bundled") == [], "npm pack file set/metadata mismatch (bundled dependencies are not supported)")
    require(isinstance(manifest, dict), "missing connector package.json")
    require((manifest.get("name"), manifest.get("version")) == (report.get("name"), report.get("version")),
            "npm pack package identity mismatch")
    patch = manifest.get("dsh", {}).get("bundle", {}).get("patch")
    require(isinstance(patch, str) and isinstance(manifest.get("main"), str), "missing native main/bundle patch")

    def references(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [ref for child in value.values() for ref in references(child)]
        if isinstance(value, list):
            return [ref for child in value for ref in references(child)]
        return []

    for target in [manifest["main"], patch, *references(manifest.get("exports"))]:
        relative = target[2:] if target.startswith("./") else target
        require(any(fnmatch.fnmatchcase(name, relative) for name in files), f"native package target missing: {target}")
    repository = manifest.get("repository", {})
    url = repository.get("url", "") if isinstance(repository, dict) else repository
    require(isinstance(url, str), "missing connector repository URL")
    url = url.removeprefix("git+")
    parsed = urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment),
            "connector source must be an explicit HTTPS Git URL without credentials/query/ref")
    native = {
        "main": manifest["main"], "exports": manifest.get("exports"), "bundle_patch": patch,
        "engines": manifest.get("engines", {}), "peer_dependencies": manifest.get("peerDependencies", {}),
        "source": {"type": "git", "url": url, "commit": args.connector_commit},
    }
    return manifest, native, sorted(files.values(), key=lambda row: row["path"]), sha256(report_bytes)


def prepare(args):
    packager = load_packager(args.core_packager)
    for digest in (args.core_sha256, args.connector_sha256):
        require(re.fullmatch("[0-9a-f]{64}", digest), "expected a full lowercase SHA256 from a trusted source")
    core_entries, core_tree, core_tags = source_identity(packager, args.core_repo, args.core_commit)
    connector_entries, connector_tree, connector_tags = source_identity(packager, args.connector_repo, args.connector_commit)
    core_manifest = packager.verify_package(args.core_archive, expected_sha256=args.core_sha256)
    require(core_manifest["source_commit"] == args.core_commit, "core source commit does not match the requested commit")
    require({row["path"]: {key: row[key] for key in ("path", "mode", "git_blob", "size")} for row in core_manifest["files"]}
            == core_entries, "core archive files do not match the pinned source tree")
    manifest, native, files, report_hash = verify_connector(packager, args, connector_entries)
    provenance = verify_connector_source(packager, args, core_entries, connector_entries, connector_tree)
    versions = [tag[1:] for tag in core_tags if re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag)]

    def archive(path, digest, count):
        return {"path": str(path.resolve()), "sha256": digest, "bytes": path.stat().st_size, "files": count}

    core = {
        "source_commit": args.core_commit, "source_tree": core_tree,
        "version": versions[0] if len(versions) == 1 else None, "release_tags": core_tags,
        "archive": archive(args.core_archive, args.core_sha256, len(core_entries)),
        **{key: value for key, value in core_manifest.items() if key not in ("files", "source_commit")},
    }
    connector = {
        "name": manifest["name"], "version": manifest["version"],
        "source_commit": args.connector_commit, "source_tree": connector_tree, "release_tags": connector_tags,
        "archive": archive(args.connector_archive, args.connector_sha256, len(files)), "files": files, "native": native,
    }
    # Only immutable source/package identities bind receipts; output paths and local tags do not.
    identity = {name: {"commit": side["source_commit"], "tree": side["source_tree"], "sha256": side["archive"]["sha256"]}
                for name, side in (("core", core), ("connector", connector))}
    if provenance["status"] == "verified":
        identity["source_provenance"] = provenance
    pair_id = "sha256:" + sha256(canonical(identity))
    receipts = []
    for path in args.evidence:
        receipt, encoded = read_json(packager, path)
        require(isinstance(receipt, dict) and receipt.get("format") == "meyo-release-pair-evidence-v1"
                and receipt.get("pair_id") == pair_id, "evidence belongs to another pair or has an invalid format")
        observations = receipt.get("observations")
        require(isinstance(observations, list) and all(isinstance(row, dict) and isinstance(row.get("kind"), str)
                and row.get("status") in ("passed", "failed", "not_verified") for row in observations), "invalid evidence observations")
        receipts.append({"path": str(path.resolve()), "sha256": sha256(encoded), "origin": "supplied_receipt_not_rerun",
                         "environment": receipt.get("environment"), "observations": observations})
    return {
        "format": "meyo-release-pair-v1", "operation": "prepare-only", "pair_id": pair_id,
        "core": core, "connector": connector, "source_provenance": provenance,
        "tools": {"core_packager": {"path": str(args.core_packager.resolve()), "sha256": sha256(args.core_packager.read_bytes())},
                  "npm_pack_report": {"path": str(args.connector_pack_report.resolve()), "sha256": report_hash}},
        "acceptance": {"performed_by_preparer": False, "missing_receipt_status": "not_verified", "receipts": receipts},
        "publication": {"performed": False, "history_review": "not_performed"},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-packager", required=True, type=Path, help="Existing trusted LOCAL scripts/package_core.py, not code from the input archive")
    for side in ("core", "connector"):
        parser.add_argument(f"--{side}-repo", required=True, type=Path)
        parser.add_argument(f"--{side}-commit", required=True, help="Full immutable commit; never a branch or HEAD")
        parser.add_argument(f"--{side}-archive", required=True, type=Path)
        parser.add_argument(f"--{side}-sha256", required=True)
    parser.add_argument("--connector-pack-report", required=True, type=Path, help="Original npm pack --json --ignore-scripts --offline output")
    parser.add_argument("--connector-source-path",
                        help="Optional exact relative directory in the core commit; compare its full Git subtree with the connector root")
    parser.add_argument("--evidence", action="append", default=[], type=Path, help="Optional matching-pair receipt; records facts without rerunning or judging tests")
    parser.add_argument("--output", required=True, type=Path, help="New private JSON file outside both source repositories; never overwritten")
    args = parser.parse_args(argv)
    try:
        require(not os.path.lexists(args.output), "output already exists; refusing to overwrite")
        output = args.output.resolve()
        require(not any(output.is_relative_to(repo.resolve()) for repo in (args.core_repo, args.connector_repo)),
                "output must be outside both source repositories")
        document = prepare(args)
        # Link a complete file atomically; a concurrent creator wins rather than being overwritten.
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".release-pair-") as temporary:
            temporary.write(canonical(document))
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(temporary.name, output)
        print(json.dumps({"operation": "prepare-only", "pair_id": document["pair_id"], "output": str(output)}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, RecursionError) as error:
        print(f"release-pair: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
