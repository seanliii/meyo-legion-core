"""代码追平独立回归：真实临时 Git，不模拟 add/commit 成功。

只执行当前 legion_core.py 的原样 helper 及其同文件函数依赖；不导入服务、
编排器、模型配置或设备模块。AST 装载仅隔离模块初始化副作用，不改函数体。
每例显式允许自有 tmp_path 内的 Git subprocess，并仅在调用 helper 时解除
PYTEST_CURRENT_TEST 的统一跳过。正常测试仍保留该保护。

断言检查真实 HEAD、commit 文件列表、blob 和用户 index 的原始字节。
提交失败或 helper 尚未修复就真实失败；不使用 xfail、假 dispatch 或假 done。
状态 done/partial 只是输入样例，不作为实际提交成功的证据。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


CORE = Path(__file__).resolve().parents[1] / "legion_core.py"


@pytest.fixture(scope="module")
def autocommit():
    """运行磁盘真实实现；避免 import legion_core 顺带启动别的系统。"""
    source = CORE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CORE))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert "_autocommit_delivery" in functions, "候选尚未导入 helper，不能当作已通过"
    selected = {"_autocommit_delivery"}
    pending = list(selected)
    while pending:
        name = pending.pop()
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Name) and node.id in functions:
                if node.id not in selected:
                    selected.add(node.id)
                    pending.append(node.id)
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            if all(alias.name.split(".")[0] in sys.stdlib_module_names
                   for alias in node.names):
                imports.append(node)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if (node.module or "").split(".")[0] in sys.stdlib_module_names:
                imports.append(node)
    body = [
        ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0),
        *[node for node in imports
          if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")],
        *[node for node in tree.body
          if isinstance(node, ast.FunctionDef) and node.name in selected],
    ]
    namespace = {"__file__": str(CORE), "__name__": "_code_parity_test_core"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 str(CORE), "exec"), namespace)
    digest = hashlib.sha256(
        ast.dump(functions["_autocommit_delivery"]).encode()
    ).hexdigest()
    print(f"\n真实 helper: {CORE}:{functions['_autocommit_delivery'].lineno}"
          f"\nhelper AST sha256: {digest}")
    return namespace["_autocommit_delivery"]


class OwnedTmpGit:
    """所有可写仓库均由该测试创建；没有 remote，检查命令不继承投毒环境。"""

    def __init__(self, tmp_path, monkeypatch, helper):
        self.root = tmp_path.resolve()
        self.monkeypatch = monkeypatch
        self.helper = helper
        self.env = dict(os.environ)
        self.env.pop("PYTEST_CURRENT_TEST", None)

    def git(self, repo, *args, check=True):
        repo = Path(repo).resolve()
        assert repo.is_relative_to(self.root), "只允许测试自有临时仓库"
        return subprocess.run(
            ["git", "-C", str(repo), *args], env=self.env,
            capture_output=True, text=True, check=check, timeout=15,
        ).stdout.strip()

    def repo(self, name="repo", files=None):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "代码追平回归")
        self.git(repo, "config", "user.email", "parity@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        self.git(repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        for path, text in (files or {"base.txt": "base\n"}).items():
            self.write(repo, path, text)
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "-m", "独立测试基线")
        return repo

    @staticmethod
    def write(repo, path, text):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def index(self, repo):
        # GIT_OPTIONAL_LOCKS=0，观察命令本身不刷新用户 index。
        entries = self.git(repo, "ls-files", "--stage", "-z")
        path = repo / ".git" / "index"
        return {"entries": entries, "bytes": path.read_bytes() if path.exists() else None}

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")

    def committed_files(self, repo):
        raw = self.git(repo, "diff-tree", "--no-commit-id", "--name-only",
                       "-r", "-z", "HEAD")
        return set(filter(None, raw.split("\0")))

    def invoke(self, repo, rows):
        assert repo.resolve().is_relative_to(self.root)
        out = {"results": rows}
        before = copy.deepcopy(out)
        with self.monkeypatch.context() as patch:
            # 显式允许这一次自有 tmp Git 真跑；不替换任何 subprocess/Git 结果。
            patch.delenv("PYTEST_CURRENT_TEST", raising=False)
            result = self.helper(
                {"project_context": {"repo_root": str(repo)}},
                "code-parity-test", 1, out,
            )
        assert out == before, "自动提交不得把失败/partial 改写成 done"
        return result


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """不读取宿主 Git 配置/凭据；所有环境改动在每例结束时恢复。"""
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "MEYO_LEGION_DIR": tmp_path / "meyo",
        "LEGION_HOME_DIR": tmp_path / "legion",
    }.items():
        monkeypatch.setenv(key, str(value))


@pytest.fixture
def allow_owned_tmp_git_subprocess(tmp_path, monkeypatch, autocommit):
    """显式 opt-in：仅本文件、每例自有 tmp 仓库允许真实子进程。"""
    return OwnedTmpGit(tmp_path, monkeypatch, autocommit)


def delivered(*paths, status="done"):
    return {"status": status, "artifacts": [{"path": str(path)} for path in paths]}


def test_commits_real_file_not_unlisted_neighbor(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "product.txt", "本轮真实文件\n")
    g.write(repo, "runtime.log", "不属于本轮\n")
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}, result
    assert g.git(repo, "show", "HEAD:product.txt") == "本轮真实文件"
    assert result["files"] == 1


def test_success_preserves_user_index_bytes(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "product.txt", "本轮文件\n")
    before = g.index(repo)
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}
    assert g.index(repo) == before, "提交必须使用独立 index，不能改用户 index"


def test_does_not_commit_unrelated_staged_work(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo(files={"user.txt": "原版\n"})
    g.write(repo, "user.txt", "用户已经暂存的版本\n")
    g.git(repo, "add", "--", "user.txt")
    g.write(repo, "user.txt", "用户尚未暂存的版本\n")
    g.write(repo, "product.txt", "本轮交付\n")
    before = g.index(repo)
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}, result
    assert g.git(repo, "show", "HEAD:user.txt") == "原版"
    assert g.index(repo) == before
    assert (repo / "user.txt").read_text() == "用户尚未暂存的版本\n"


def test_delivery_file_keeps_its_preexisting_staged_version(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo(files={"product.txt": "原版\n"})
    g.write(repo, "product.txt", "用户暂存版\n")
    g.git(repo, "add", "--", "product.txt")
    g.write(repo, "product.txt", "本轮磁盘版\n")
    before = g.index(repo)
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] == "committed", result
    assert g.git(repo, "show", "HEAD:product.txt") == "本轮磁盘版"
    assert g.index(repo) == before, "同一文件的用户暂存版本也不能被覆盖"


@pytest.mark.parametrize("directory", [".", "bundle"])
def test_directory_artifact_is_not_recursive_add(
    allow_owned_tmp_git_subprocess, directory,
):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "bundle/product.txt", "本轮明确列出的正规文件\n")
    g.write(repo, "bundle/unlisted.log", "目录里不属于交付的文件\n")
    g.write(repo, "outside-bundle.log", "根目录里的无关文件\n")
    before = g.index(repo)
    result = g.invoke(repo, [delivered(directory, "bundle/product.txt")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"bundle/product.txt"}, result
    assert g.index(repo) == before


def test_directory_only_is_not_a_file_delivery(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "bundle/unlisted.txt", "没有逐文件交付证据\n")
    before = (g.head(repo), g.index(repo))
    result = g.invoke(repo, [delivered("bundle")])
    assert result["status"] != "committed", result
    assert (g.head(repo), g.index(repo)) == before


@pytest.mark.parametrize("invalid", ["missing", "deleted", "symlink"])
def test_only_existing_regular_files_are_added(allow_owned_tmp_git_subprocess, invalid):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo(files={"deleted.txt": "不可把缺文件伪装成本轮删除交付\n"})
    g.write(repo, "product.txt", "本轮正规文件\n")
    if invalid == "deleted":
        (repo / "deleted.txt").unlink()
        path = "deleted.txt"
    elif invalid == "symlink":
        g.write(repo, "unlisted.txt", "符号链接不能借壳提交这个文件\n")
        (repo / "alias.txt").symlink_to("unlisted.txt")
        path = "alias.txt"
    else:
        path = "never-created.txt"
    before = g.index(repo)
    result = g.invoke(repo, [delivered("product.txt", path)])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}, result
    assert g.index(repo) == before


def test_git_pathspec_characters_are_literal(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "product*.txt", "文件名里的星号是字面量\n")
    g.write(repo, "product-other.txt", "不能被通配符卷进去\n")
    result = g.invoke(repo, [delivered("product*.txt")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product*.txt"}, result


def test_failed_row_cannot_borrow_another_rows_done(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "product.txt", "成功任务的文件\n")
    g.write(repo, "failed-draft.txt", "失败任务留下的草稿\n")
    result = g.invoke(repo, [
        delivered("product.txt"),
        delivered("failed-draft.txt", status="error"),
    ])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}, result


def test_all_failed_never_creates_commit(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "failed-draft.txt", "真实残留，但任务失败\n")
    before = (g.head(repo), g.index(repo))
    result = g.invoke(repo, [delivered("failed-draft.txt", status="error")])
    assert result["status"] == "skipped", result
    assert (g.head(repo), g.index(repo)) == before


def test_partial_file_is_committed_without_rewriting_outcome(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    path = g.write(repo, "partial.txt", "确实落盘的阶段产物\n")
    result = g.invoke(repo, [delivered(path, status="partial")])
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"partial.txt"}
    assert g.git(repo, "show", "HEAD:partial.txt") == "确实落盘的阶段产物"


def test_unchanged_artifact_cannot_commit_unrelated_index(allow_owned_tmp_git_subprocess):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo(files={"product.txt": "本轮没有变化\n", "user.txt": "原版\n"})
    g.write(repo, "user.txt", "已暂存但不是本轮产物\n")
    g.git(repo, "add", "--", "user.txt")
    before = (g.head(repo), g.index(repo))
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] != "committed", result
    assert (g.head(repo), g.index(repo)) == before


@pytest.mark.parametrize("poison", ["repository", "index"])
def test_inherited_git_environment_cannot_redirect_commit(
    allow_owned_tmp_git_subprocess, monkeypatch, poison,
):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    victim = g.repo("other-repo")
    g.write(repo, "product.txt", "应该提交到目标仓库\n")
    g.write(victim, "product.txt", "不能提交或暂存到另一个仓库\n")
    before = (g.head(victim), g.index(victim))
    if poison == "repository":
        monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(victim))
        monkeypatch.setenv("GIT_COMMON_DIR", str(victim / ".git"))
    else:
        monkeypatch.setenv("GIT_INDEX_FILE", str(victim / ".git" / "index"))
    result = g.invoke(repo, [delivered("product.txt")])
    # 先检查受害仓库，避免错误 status 掩盖环境串写的证据。
    assert (g.head(victim), g.index(victim)) == before, result
    assert result["status"] == "committed", result
    assert g.committed_files(repo) == {"product.txt"}, result
    assert g.git(repo, "show", "HEAD:product.txt") == "应该提交到目标仓库"


def test_real_commit_failure_leaves_user_index_untouched(
    allow_owned_tmp_git_subprocess,
):
    g = allow_owned_tmp_git_subprocess
    repo = g.repo()
    g.write(repo, "product.txt", "准备提交的产物\n")
    before = (g.head(repo), g.index(repo))
    # 真实 Git ref 锁冲突；不是 mock 返回失败，也不是执行宿主 hook。
    lock = repo / ".git" / "refs" / "heads" / "main.lock"
    lock.write_text("本测试模拟另一个进程持锁\n", encoding="utf-8")
    result = g.invoke(repo, [delivered("product.txt")])
    assert result["status"] == "error", result
    assert (g.head(repo), g.index(repo)) == before, result
    assert lock.read_text() == "本测试模拟另一个进程持锁\n"
