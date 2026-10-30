"""Bounded GitHub facts, not a clone, installer, license verdict or task selector."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import github_access

MAX_REQUESTS = 10
MAX_RESPONSE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_TEXT_CHARS = 16000
_SHA = re.compile(r"[0-9a-f]{40}")
_LICENSE = re.compile(r"^(?:licen[sc]e|copying|notice|copyright|unlicense)(?:[._-].*)?$", re.I)
_RUN_FILES = ("INSTALL.md", "INSTALL", "BUILD.md", "BUILD", "package.json",
              "pyproject.toml", "Cargo.toml", "go.mod", "requirements.txt",
              "Makefile", "CMakeLists.txt", "Dockerfile", "setup.cfg", "setup.py")


def _now():
    return datetime.now(timezone.utc).isoformat()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send shared Authorization to redirects, including renamed repos.
        # Report the original request/status; never echo a credential-bearing Location.
        return None


class _Reader:
    def __init__(self, timeout_s):
        self.started = time.monotonic()
        self.deadline = self.started + timeout_s
        self.timeout_s = timeout_s
        self.sources = []
        self.requests = self.bytes_read = 0
        self.stopped = ""
        self.auth = github_access.status()
        self.headers = github_access.request_headers()
        authorization = self.headers.get("Authorization", "")
        self.secret = authorization.partition(" ")[2] if authorization else ""
        proxy = github_access.proxy_url()
        handlers = [_NoRedirect()]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)

    def safe_text(self, text):
        return text.replace(self.secret, "[REDACTED]") if self.secret else text

    def get(self, url, kind, revision=None):
        row = {"source_id": "github-" + hashlib.sha256(url.encode()).hexdigest()[:20],
               "kind": kind, "url": url, "visited_at": None, "captured_at": None,
               "revision": revision, "content_sha256": None, "snapshot_ref": None,
               "supports_claims": [], "status": "failed", "http_status": None}
        self.sources.append(row)
        remaining = self.deadline - time.monotonic()
        if self.stopped or remaining <= 0 or self.requests >= MAX_REQUESTS:
            row.update(status="not_fetched", error=self.stopped or "inspection_budget_exhausted")
            return None, row
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "api.github.com"
                or parsed.username is not None or parsed.fragment):
            row.update(url="<refused>", error="unsafe_api_url")
            return None, row
        self.requests += 1
        try:
            req = urllib.request.Request(url, headers=self.headers, method="GET")
            row["visited_at"] = _now()
            with self.opener.open(req, timeout=min(5.0, remaining)) as response:
                row["http_status"] = response.status
                chunks, size = [], 0
                while True:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("inspection deadline")
                    # read1 does not wait to fill a large buffer from a slow stream.
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(min(5.0, remaining))
                    block = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - size))
                    if not block:
                        break
                    size += len(block)
                    self.bytes_read += len(block)
                    if size > MAX_RESPONSE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
                        self.stopped = "response_byte_budget_exhausted"
                        raise ValueError(self.stopped)
                    chunks.append(block)
                raw = b"".join(chunks)
                digest = hashlib.sha256(raw).hexdigest()
                row.update(captured_at=_now(), response_bytes=len(raw), response_sha256=digest,
                           content_sha256=digest, hash_scope="github_api_response_bytes")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("expected JSON object")
                row["status"] = "fetched"
                return data, row
        except urllib.error.HTTPError as exc:
            row.update(http_status=exc.code,
                       error="redirect_refused" if 300 <= exc.code < 400 else "github_http_error")
            for header in ("X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"):
                value = str(exc.headers.get(header, "")) if exc.headers else ""
                if value.isdigit():
                    row.setdefault("rate_limit", {})[header] = value
            if exc.code in (401, 403, 429):
                self.stopped = "github_auth_or_rate_limit"
            exc.close()
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
            # Exception messages can contain proxy passwords or Authorization.
            row["error"] = self.stopped or type(exc).__name__
        return None, row

    def document(self, api, repository, revision, entry, kind):
        path, sha = entry["path"], entry["sha"]
        url = f"https://github.com/{repository}/blob/{revision}/" + urllib.parse.quote(path, safe="")
        result = {"path": path, "url": url, "revision": revision, "git_blob_sha": sha,
                  "status": "not_fetched", "kind": kind, "visited_at": None, "captured_at": None}
        if entry.get("mode") not in ("100644", "100755"):
            return {**result, "error": "not_a_regular_file"}
        if not isinstance(entry.get("size"), int) or not 0 <= entry["size"] <= MAX_DOCUMENT_BYTES:
            return {**result, "error": "document_size_limit_or_unknown"}
        data, source = self.get(f"{api}/git/blobs/{sha}", kind, revision)
        source["source_id"] = "github-" + hashlib.sha256((kind + "\0" + url).encode()).hexdigest()[:20]
        source["web_url"] = url  # Canonical pinned link; the fetched URL remains the API URL.
        result["source"] = source
        result.update(visited_at=source["visited_at"], captured_at=source["captured_at"])
        if data is None:
            return {**result, "error": source["error"]}
        try:
            if (data.get("encoding") != "base64" or data.get("sha") != sha
                    or not isinstance(data.get("content"), str)):
                raise ValueError("invalid blob identity/encoding")
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
            actual_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if len(raw) != entry["size"] or len(raw) > MAX_DOCUMENT_BYTES or actual_sha != sha:
                raise ValueError("blob integrity mismatch")
            text = raw.decode("utf-8")
            safe = self.safe_text(text)
            digest = hashlib.sha256(raw).hexdigest()
            source.update(content_sha256=digest, hash_scope="complete_original_document_bytes")
            return {**result, "status": "fetched", "content_sha256": digest,
                    "bytes": len(raw), "text": safe[:MAX_TEXT_CHARS],
                    "text_truncated": len(safe) > MAX_TEXT_CHARS,
                    "text_redacted": safe != text, "hash_scope": "complete_original_document_bytes"}
        except (ValueError, KeyError, TypeError, UnicodeError):
            source.update(status="failed", error="invalid_or_nontext_blob")
            return {**result, "error": "invalid_or_nontext_blob"}


def inspect(repository, revision="", timeout_s=20):
    """Read live metadata, then pin all source documents to one verified commit."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return {"ok": False, "status": "failed", "error": "invalid_repository"}
    if not isinstance(revision, str) or (revision and not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}", revision)):
        return {"ok": False, "status": "failed", "error": "invalid_revision"}
    try:
        budget = float(timeout_s)
        if not 0 < budget <= 30:
            raise ValueError()
    except (ValueError, TypeError):
        return {"ok": False, "status": "failed", "error": "timeout_s must be within (0, 30]"}
    try:
        reader = _Reader(budget)
    except Exception as exc:
        return {"ok": False, "status": "failed", "error": "github_access:" + type(exc).__name__}
    api = f"https://api.github.com/repos/{repository}"
    metadata, metadata_source = reader.get(api, "repository_metadata")
    if metadata is not None and (not isinstance(metadata.get("full_name"), str)
                                 or not isinstance(metadata.get("default_branch"), str)):
        metadata_source.update(status="failed", error="invalid_repository_metadata")
        metadata = None
    requested = revision or (metadata or {}).get("default_branch") or "HEAD"
    commit, commit_source = reader.get(api + "/commits/" + urllib.parse.quote(requested, safe=""), "revision")
    result = {"repository": repository, "requested_revision": requested, "revision": None,
              "metadata": {}, "readme": None, "root_licenses": [], "runtime_documents": [],
              "root_directory": None, "github_auth": reader.auth, "sources": reader.sources,
              "runtime_verified": False, "cloned": False, "commands_executed": False,
              "license_scope": "Root notices and repository-declared label only; dependencies/assets are not audited.",
              "complete_package_license_verified": False}
    if metadata is not None:
        result["metadata"] = {key: metadata.get(key) for key in (
            "full_name", "description", "default_branch", "archived", "disabled", "visibility",
            "language", "stargazers_count", "pushed_at", "updated_at", "homepage")}
        license_info = metadata.get("license")
        license_info = license_info if isinstance(license_info, dict) else {}
        result["declared_license"] = {key: license_info.get(key) for key in ("name", "spdx_id")}
    pinned = (commit or {}).get("sha", "")
    commit_data = (commit or {}).get("commit")
    commit_data = commit_data if isinstance(commit_data, dict) else {}
    tree_data = commit_data.get("tree")
    tree_sha = tree_data.get("sha", "") if isinstance(tree_data, dict) else ""
    if (not _SHA.fullmatch(str(pinned)) or not _SHA.fullmatch(str(tree_sha))
            or (_SHA.fullmatch(revision) and pinned != revision)):
        if commit is not None:
            commit_source.update(status="failed", error="invalid_commit_identity")
    else:
        result["revision"] = pinned
        commit_source.update(revision=pinned, snapshot_ref="inline:revision")
        tree, tree_source = reader.get(api + "/git/trees/" + tree_sha, "root_directory", pinned)
        if tree is not None and tree.get("sha") == tree_sha and isinstance(tree.get("tree"), list):
            entries = tree["tree"]
            valid = all(isinstance(e, dict) and isinstance(e.get("path"), str)
                        and e["path"] not in ("", ".", "..") and "/" not in e["path"]
                        and _SHA.fullmatch(str(e.get("sha", ""))) for e in entries)
            if valid:
                result["root_directory"] = {
                    "revision": pinned, "git_tree_sha": tree_sha, "entry_count": len(entries),
                    "truncated": bool(tree.get("truncated")) or len(entries) > 200,
                    "entries": [{k: e.get(k) for k in ("path", "type", "mode", "sha", "size")}
                                for e in entries[:200]]}
                tree_source["snapshot_ref"] = "inline:root_directory"
                readmes = sorted((e for e in entries if re.fullmatch(r"readme(?:\..*)?", e["path"], re.I)),
                                 key=lambda e: (e["path"].lower() != "readme.md", e["path"]))
                if readmes:
                    result["readme"] = reader.document(api, repository, pinned, readmes[0], "readme")
                licenses = [e for e in entries if _LICENSE.fullmatch(e["path"])]
                result["root_licenses"] = [reader.document(api, repository, pinned, e, "root_license")
                                           for e in licenses]
                run_files = sorted((e for e in entries if e["path"] in _RUN_FILES),
                                   key=lambda e: _RUN_FILES.index(e["path"]))
                result["runtime_documents"] = [reader.document(api, repository, pinned, e, "runtime_document")
                                               for e in run_files[:2]]
                result["runtime_document_index"] = [e["path"] for e in run_files]
                result["readme_scope"] = "root only"
                result["root_license_status"] = "present" if licenses else "not_found_in_root"
            else:
                tree_source.update(status="failed", error="invalid_root_entries")
        elif tree is not None:
            tree_source.update(status="failed", error="invalid_tree_identity")
    docs = ([result["readme"]] if result["readme"] else []) + result["root_licenses"] + result["runtime_documents"]
    if metadata is not None:
        metadata_source["snapshot_ref"] = "inline:metadata"
    for field in ("readme", "root_licenses", "runtime_documents"):
        documents = [result[field]] if field == "readme" and result[field] else result[field] or []
        for index, document in enumerate(documents):
            if document.get("source") and document["status"] == "fetched":
                document["source"]["snapshot_ref"] = (
                    f"inline:{field}" if field == "readme" else f"inline:{field}/{index}")
    for source in reader.sources:
        source["fetch_status"] = source["status"]
        if source["status"] == "fetched":
            source["supports_claims"] = [source["kind"] + (
                "_at_revision" if source["revision"] else "_at_fetch_time")]
    partial = (any(s["status"] != "fetched" for s in reader.sources)
               or any(d["status"] != "fetched" for d in docs)
               or bool((result["root_directory"] or {}).get("truncated")))
    usable = bool(metadata is not None or result["revision"])
    result.update(ok=usable, status=("partial" if partial else "inspected") if usable else "failed",
                  network_budget={"timeout_s": budget, "max_requests": MAX_REQUESTS,
                                  "requests": reader.requests, "bytes_read": reader.bytes_read,
                                  "elapsed_s": round(time.monotonic() - reader.started, 3)},
                  runtime_instructions={"status": "documents_only", "executed": False,
                                        "paths": [d["path"] for d in ([result["readme"]] if result["readme"] else [])
                                                  + result["runtime_documents"]]},
                  next="Read the pinned documents and assess reuse yourself; inspection is not installation or runtime validation.")
    # Never expose the shared credential, even if a response echoes its exact value.
    def redact(value):
        if isinstance(value, str):
            return reader.safe_text(value)
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {redact(k): redact(v) for k, v in value.items()}
        return value
    return redact(result)
