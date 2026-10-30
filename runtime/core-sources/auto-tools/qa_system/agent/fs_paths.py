# -*- coding: utf-8 -*-
"""fs_paths · 每用户文件系统路径基础设施(2026-08-27 从 qa_auto_agent god-file 抽出)。

最底层的路径归属层: ~/.meyo/users/<user_id>/{sandbox,memory,sessions,process}。
qid→user_id 解析、per-user 目录、开关。零业务逻辑·只依赖 os·被 checkpoint/security/
execution 等多处复用。抽出后为后续 checkpoint/execution 模块化铺路(它们都要算路径)。
auto 通过 from fs_paths import * 拿回(调用点零改动)。
"""
import os

SANDBOX_ROOT = os.path.expanduser("~/.meyo/sandbox")

# ── 每用户文件系统(2026-08-20·用户拍板"每用户自己的文件系统·沙盒里划一块")──────
#   ~/.meyo/users/<user_id>/{sandbox,memory,sessions,process}
#   qid 形如 web_<user>_<ts> → 抽出 user_id;抽不出(评测/单用户)→ "default"。
#   跨任务保留同一用户的文件/记忆/会话(不再 per-qid 散掉)。env MEYO_PER_USER_FS 可关。
USERS_ROOT = os.path.expanduser("~/.meyo/users")


def _uid_from_qid(qid):
    """从 qid 抽 user_id: web_<user>_<ts> → <user>; 抽不出 → 'default'。fail-safe。"""
    try:
        q = str(qid or "")
        if q.startswith("web_"):
            rest = q[4:]
            # web_<user>_<ts>: 末段是 ts·去掉末段=user(user 可能含下划线·去最后一段)
            parts = rest.rsplit("_", 1)
            if len(parts) == 2 and parts[0]:
                return parts[0]
        return "default"
    except Exception:
        return "default"


def _user_fs_enabled():
    return os.environ.get("MEYO_PER_USER_FS", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def user_dir(qid_or_uid, sub=""):
    """取某用户的文件系统目录(per-user)。sub∈{sandbox,memory,sessions,process,''}。
    传 qid 自动抽 user_id;传纯 user_id 直接用。fail-safe → SANDBOX_ROOT 兜底。"""
    try:
        uid = qid_or_uid if "_" not in str(qid_or_uid) or not str(qid_or_uid).startswith("web_") \
            else _uid_from_qid(qid_or_uid)
        # 更稳: 只要像 qid(web_ 前缀)就抽·否则当 uid
        if str(qid_or_uid).startswith("web_"):
            uid = _uid_from_qid(qid_or_uid)
        uid = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(uid or "default"))[:60] or "default"
        d = os.path.join(USERS_ROOT, uid, sub) if sub else os.path.join(USERS_ROOT, uid)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        os.makedirs(SANDBOX_ROOT, exist_ok=True)
        return SANDBOX_ROOT
