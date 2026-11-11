#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Session 交付物存储 —— 每个 session 一个可持续的网页(用户需求)

用户要"每个 session 都可以看到一个网页···交付物是个性化的不只是回复···可以持续爬取
内容更新···可以继续对话"。这个模块负责:
  - 把每次军团出击的交付物(问题+各家答案+编排+模式)存成本机文件(本机原型阶段·
    部署新仓库时换真存储/真链接)
  - 提供 /s/<id> 渲染成一个独立可分享的交付物网页
  - 支持追加(继续对话·持续生成内容),所以 session 是一个 turns 列表

存储:每个 session 一个 JSON,放 SESSIONS_DIR(默认 ~/.legion/sessions)。
"""
from __future__ import annotations
import os
import json
import time
import html
import hashlib
import hmac
import secrets
import threading
import legion_paths as _paths

# save_turn 并发覆盖防护(军团 hermes dogfood 揪出:裸读改写·同 sid 并发保存后写者吃掉前一个
# turn)。单进程 ThreadingHTTPServer 用进程内锁串行化读改写临界区;配合原子写防文件损坏。
_SAVE_LOCK = threading.RLock()
# 列表缓存 stamp = (目录, 目录mtime, JSON数)：目录切换(pytest 各测试文件不同
# LEGION_SESSIONS_DIR)时 stamp 必然不同，旧目录快照自动失效(2026-09-07 组合污染根因)。
_LIST_CACHE: dict[tuple[str, int], tuple[tuple[str, int, int], list[dict]]] = {}
_DETAIL_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_DETAIL_CACHE_MAX = 128
_SUMMARY_CACHE: dict[str, dict] = {}
_SUMMARY_STAMP: tuple[int, int] | None = None
_SUMMARY_LOADED = False
# 摘要投影当前归属的会话目录：目录切换即整体作废，防止旧目录残留记录被
# _write_summary_index() 写进新目录的索引文件(同 sid 累计成脏数据)。
_SUMMARY_DIR: str | None = None
_SUMMARY_PROJECTION_VERSION = 2  # Whole-history artifacts plus legacy nested result support.

# ── 每用户运行中单量(2026-09-23 门二)：dispatch 开跑前登记·结束注销 ──────────
# 排队语义在 server.dispatch(_worker 里 user_slot 等待);这里只做计数与排队位置。
# 崩溃残留(登记了没注销)由 wait_user_slot 的最长等待兜底放行,不依赖严格清理。
_USER_ACTIVE_LOCK = threading.RLock()
_USER_ACTIVE: dict[str, int] = {}
_USER_ACTIVE_TS: dict[str, float] = {}
_WAITING_TOKENS: dict[str, list[dict]] = {}
_USER_SLOT_TIMEOUT_S = float(os.environ.get("LEGION_USER_SLOT_TIMEOUT_S", "1800"))


def _user_slot_limit() -> int:
    try:
        return max(1, int(os.environ.get("LEGION_USER_MAX_ACTIVE", "2")))
    except (TypeError, ValueError):
        return 2


def user_slot_wait(token: dict) -> tuple[bool, int]:
    """单次尝试用户槽；等待、取消检查和排队播报由调用方负责。

    ahead 是当前运行数与本 token 前仍在等待的任务数，不承诺新的调度优先级。
    保持原 limit 与从首次尝试开始计时的 timeout/bypass 语义。
    """
    uid = str(token.get("uid") or "default")
    with _USER_ACTIVE_LOCK:
        if token.get("released"):
            return False, 0
        active = _USER_ACTIVE.get(uid, 0)
        if token.get("held"):
            return True, max(0, active - 1)
        deadline = token.get("deadline") or (time.time() + _USER_SLOT_TIMEOUT_S)
        token["deadline"] = deadline
        ahead = active
        for earlier in _WAITING_TOKENS.get(uid, []):
            if earlier is token:
                break
            if not earlier.get("held") and not earlier.get("released"):
                ahead += 1
        if active < _user_slot_limit():
            _USER_ACTIVE[uid] = active + 1
            _USER_ACTIVE_TS[uid] = time.time()
            token["held"] = True
            return True, ahead
        if time.time() >= deadline:
            _USER_ACTIVE[uid] = active + 1
            token["held"] = True
            token["bypass"] = True
            return True, ahead
        return False, ahead


def user_slot_release(uid: str, token: dict | None = None) -> None:
    """有 token 时精确且幂等释放；旧 uid-only 调用保持原聚合计数语义。"""
    uid = str(uid or "default")
    with _USER_ACTIVE_LOCK:
        waiting = list(_WAITING_TOKENS.get(uid, []))
        release_held = token is None
        if token is not None:
            if (str(token.get("uid") or "default") != uid or token.get("released")
                    or not any(item is token for item in waiting)):
                return
            release_held = bool(token.get("held"))
            token["held"] = False
            token["released"] = True
            remaining = [item for item in waiting if item is not token]
            if remaining:
                _WAITING_TOKENS[uid] = remaining
            else:
                _WAITING_TOKENS.pop(uid, None)
        current = _USER_ACTIVE.get(uid, 0)
        if release_held and current > 0:
            _USER_ACTIVE[uid] = current - 1
    # 不持计数锁获取 Condition，避免与等待方的锁顺序形成死锁。
    # 包含刚撤销的 token，让它正在进行的外层 cond.wait 也能及时醒来。
    for waiting_token in waiting:
        try:
            with waiting_token["cond"]:
                waiting_token["cond"].notify_all()
        except Exception:
            pass


def user_slot_token(uid: str) -> dict:
    """构造一个用户槽等待 token(cond + 等待者登记·release 时被唤醒)。
    release 时统一清理过期 token(wait 完成的留在表里无害,但表不能无限涨)。"""
    cond = threading.Condition()
    token = {"uid": str(uid or "default"), "cond": cond,
             "created_at": time.time(), "held": False, "released": False}
    uid = token["uid"]
    with _USER_ACTIVE_LOCK:
        _WAITING_TOKENS.setdefault(uid, []).append(token)
        # 顺手清理陈旧 token(等待超 1h 的等待者早已超时放行)
        cutoff = time.time() - 3600
        _WAITING_TOKENS[uid] = [
            t for t in _WAITING_TOKENS[uid] if not t.get("released")
            and ((t.get("held") and _USER_ACTIVE.get(uid, 0) > 0)
                 or t.get("deadline", t.get("created_at", 0)) > cutoff or t is token)]
    return token


def user_active_count(uid: str) -> int:
    """该用户当前运行中/排队中的单量(观测用)。"""
    with _USER_ACTIVE_LOCK:
        return _USER_ACTIVE.get(str(uid or "default"), 0)


def sessions_dir() -> str:
    """会话存储根目录(调用时解析 env·非 import 时固化)：pytest 收集阶段多个测试文件
    各自设不同 LEGION_SESSIONS_DIR，固化常量会读写到别的测试目录(2026-09-07 全量失败根因)。"""
    return os.environ.get("LEGION_SESSIONS_DIR") or _paths.home_under("sessions")


def _summary_index_path() -> str:
    return os.path.join(sessions_dir(), ".summary-index.json")


def __getattr__(name):
    # 兼容旧引用(如 tests 直接读 sessions._SUMMARY_INDEX)：转为调用时解析的惰性属性。
    if name == "SESSIONS_DIR":
        return sessions_dir()
    if name == "_SUMMARY_INDEX":
        return _summary_index_path()
    raise AttributeError(f"module 'sessions' has no attribute {name!r}")


def _directory_stamp() -> tuple[str, int, int]:
    """目录路径 + mtime + JSON 数量；目录切换或跨进程写入都能让列表快照失效。"""
    directory = sessions_dir()
    try:
        return directory, os.stat(directory).st_mtime_ns, sum(name.endswith(".json") and not name.startswith(".")
                                                        for name in os.listdir(directory))
    except OSError:
        return directory, 0, 0


def _invalidate_list_cache(user_id: str | None = None) -> None:
    with _SAVE_LOCK:
        if user_id is None:
            _LIST_CACHE.clear()
        else:
            uid = str(user_id or "default")
            for key in [key for key in _LIST_CACHE if key[0] == uid]:
                _LIST_CACHE.pop(key, None)


def _invalidate_views(user_id: str) -> None:
    """写入口统一失效用户快照；模块未加载时不为保存路径增加 import 成本。"""
    import sys
    uid = str(user_id or "default")
    for name in ("performance_views", "user_view"):
        module = sys.modules.get(name)
        invalidate = getattr(module, "invalidate", None) if module else None
        if invalidate:
            try:
                invalidate(uid)
            except Exception:
                pass


def _summary_from_session(sess: dict, fallback_sid: str = "") -> dict:
    turns = sess.get("turns") or []
    agents, artifacts = {}, []
    for turn in turns:
        result_block = turn.get("result") if isinstance(turn.get("result"), dict) else turn
        results = result_block.get("results") or []
        keys = [result.get("key") or result.get("harness") for result in results
                if isinstance(result, dict)]
        winner = result_block.get("winner")
        if isinstance(winner, dict):
            keys.append(winner.get("key") or winner.get("harness"))
        if result_block.get("captain"):
            keys.append(result_block.get("captain"))
        for key in keys:
            if key:
                agents[key] = agents.get(key, 0) + 1
    seen_artifacts = set()
    for turn in reversed(turns):
        block = turn.get("result") if isinstance(turn.get("result"), dict) else turn
        for result in [block, *(block.get("results") or [])]:
            if not isinstance(result, dict):
                continue
            for artifact in result.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                identity = str(artifact.get("url") or artifact.get("path") or artifact.get("name") or "")
                if not identity or identity in seen_artifacts:
                    continue
                seen_artifacts.add(identity)
                row = dict(artifact)
                row.setdefault("source", result.get("key") or result.get("harness") or "")
                artifacts.append(row)
                if len(artifacts) >= 60:
                    break
            if len(artifacts) >= 60:
                break
        if len(artifacts) >= 60:
            break
    sid = sess.get("sid") or fallback_sid
    share_token = str(sess.get("share_token") or "")
    share_url = f"/s/{sid}?share={share_token}" if share_token else f"/s/{sid}"
    # 会话运行态：优先读最后一轮 turn.status（done/error/cancelled——2026-09-19 起
    # save_turn 从 cancelled/outcome 推导落盘）；旧会话 status 缺失时退「有没有交付」：
    # 有交付=done，无交付=running。取消单此前被误判（部分交付→done，全没交付→僵尸
    # running）——现在 cancelled 如实显示。
    _last_turn = turns[-1] if turns else {}
    _last = _last_turn.get("result") if isinstance(_last_turn.get("result"), dict) else _last_turn
    _last_results = _last.get("results") or []
    _has_delivery = bool(_last.get("final_result")) or any(
        (r or {}).get("final") or (r or {}).get("status") == "done" for r in _last_results)
    _turn_status = str(_last_turn.get("status") or _last.get("status") or "")
    if _turn_status == "cancelled":
        _sess_status = "cancelled"
    elif _turn_status == "done":
        _sess_status = "done"
    elif _turn_status == "error":
        _sess_status = "error"
    else:
        _sess_status = "done" if _has_delivery else "running"
    return {"sid": sid, "url": share_url,
            "status": _sess_status,
            "title": (str(turns[0].get("question") or "")[:60] if turns else "(空)"),
            "turns": len(turns), "updated": sess.get("updated", 0),
            "user_id": str(sess.get("user_id") or "default"),
            "agents": agents, "artifacts": artifacts[:60],
            "_projection_version": _SUMMARY_PROJECTION_VERSION}


def _load_summary_index() -> None:
    global _SUMMARY_LOADED, _SUMMARY_STAMP, _SUMMARY_DIR
    directory = sessions_dir()
    if _SUMMARY_DIR != directory:
        # 目录已切换：旧目录的内存投影整体作废(含 stamp=None 的“新目录无索引”场景，
        # 否则残留记录会被 _write_summary_index 带进新目录索引文件)。
        _SUMMARY_CACHE.clear()
        _SUMMARY_LOADED = False
        _SUMMARY_STAMP = None
        _SUMMARY_DIR = directory
    try:
        stat = os.stat(_summary_index_path())
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if _SUMMARY_LOADED and stamp == _SUMMARY_STAMP:
        return
    value = None
    if stamp is not None:
        try:
            with open(_summary_index_path(), encoding="utf-8") as handle:
                candidate = json.load(handle)
            if isinstance(candidate, dict):
                value = candidate
        except Exception:
            pass
    if value is not None:
        _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE.update(value)
    elif stamp is not None:
        # 索引只是 session 文件的投影。损坏时丢弃内存投影，让列表扫描从事实源重建。
        _SUMMARY_CACHE.clear()
    _SUMMARY_STAMP = stamp
    _SUMMARY_LOADED = True


def _write_summary_index() -> None:
    global _SUMMARY_STAMP
    try:
        index_path = _summary_index_path()
        tmp = f"{index_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_SUMMARY_CACHE, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, index_path)
        stat = os.stat(index_path)
        _SUMMARY_STAMP = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass

def _ensure_dir():
    os.makedirs(sessions_dir(), exist_ok=True)


def _path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:60]
    return os.path.join(sessions_dir(), f"{safe}.json")


def new_session_id(question: str, ts: float | None = None) -> str:
    """基于问题+时间生成短 id(可复现·避免 Math.random 之类)。"""
    ts = ts if ts is not None else time.time()
    h = hashlib.sha1(f"{question}|{ts}".encode("utf-8")).hexdigest()[:12]
    return h


def save_turn(sid: str, question: str, result: dict, mode: str = "pk", user_id: str = "default",
              *, source_ref: dict | None = None, input_snapshot: dict | None = None,
              outcome_contract: dict | None = None, version_identity: dict | None = None,
              turn_id: str = "") -> dict:
    """把一次军团出击的交付物追加进 session(支持继续对话·多 turn)。返回 session 概要。

    user_id 写入每个独立 session 文件·list/load 按它隔离(30真实用户浏览器测试要求:用户不能看到
    别人的会话/文件)。旧文件无 user_id 只归 default 用户,向后兼容。
    turn_id 由父层按请求生成，原样用于 running/终态两次保存；不从 result 内容读取。
    显式绑定的终态只替换自己的 running 占位，不匹配时返回错误，绝不降级猜其它请求。
    """
    _ensure_dir()
    p = _path(sid)
    uid = str(user_id or "default")
    # turn.status：dispatch out 顶层没有 status 字段（旧链路落盘恒 None——
    # 取消单因此无法与会话列表区分）。优先级：显式 status > cancelled 标记 >
    # outcome.status（completed/partial→done，cancelled→cancelled，failed→error）。
    _raw_status = result.get("status")
    if not _raw_status:
        if result.get("cancelled"):
            _raw_status = "cancelled"
        else:
            _outcome_status = str((result.get("outcome") or {}).get("status") or "")
            if _outcome_status in ("completed", "partial"):
                _raw_status = "done"
            elif _outcome_status in ("cancelled", "failed"):
                _raw_status = "error" if _outcome_status == "failed" else _outcome_status
    turn = {
        "schema_version": 2,
        "question": question,
        "mode": mode,
        "execution_mode": result.get("execution_mode"),
        "results": result.get("results", []),
        "final_result": result.get("final_result"),
        "pipeline": result.get("pipeline"),
        "hierarchy": result.get("hierarchy"),
        "status": _raw_status,
        "team_id": result.get("team_id"),
        "note": result.get("note"),
        # 2026-09-13 历史回放补充(前端 loadSession 消费):终态推荐/对照/耗时/时间线要素。
        # outcome 里的 delivered/total 也在·新字段缺省时前端按 turns 自身降级渲染。
        "winner": result.get("winner") or "",
        "winner_note": result.get("winner_note") or "",
        "selection": result.get("selection") or {},
        "baseline": result.get("baseline"),
        "outcome": result.get("outcome"),
        "usage": result.get("usage"),
        "source_ref": dict(source_ref or {}),
        "input_snapshot": dict(input_snapshot or {}),
        "outcome_contract": dict(outcome_contract or {}),
        "version_identity": dict(version_identity or {}),
        "ts": time.time(),
    }
    # Preserve delivery observations through refresh/export. A member returning
    # text is not a substitute for the whole request's recorded outcome.
    for field in ("outcome", "delivery_contract", "partial", "contract_incomplete",
                  "success_audit", "verification_reports", "verification_required",
                  "verification_incomplete", "delivered_count", "total",
                  "all_failed", "cancelled", "usage", "pilot", "persistent_rounds",
                  "persistent_diagnostics", "persistent_capped"):
        if field in result:
            turn[field] = result[field]
    _turn_id = str(turn_id or "")
    if _turn_id:
        turn["turn_id"] = _turn_id
    # 读-改-写全流程在锁内串行(防同 sid 并发保存后写者吃掉前一个 turn),原子写防损坏。
    with _SAVE_LOCK:
        if os.path.isfile(p):
            try:
                sess = json.load(open(p, encoding="utf-8"))
            except Exception:
                sess = {"sid": sid, "user_id": uid, "turns": []}
        else:
            sess = {"sid": sid, "user_id": uid, "created": time.time(), "turns": []}
        owner = str(sess.get("user_id") or "default")
        if owner != uid:
            return {"error": "session belongs to another user"}
        sess["schema_version"] = max(2, int(sess.get("schema_version") or 1))
        sess["user_id"] = uid
        sess.setdefault("share_token", secrets.token_urlsafe(24))
        turns = sess.setdefault("turns", [])
        # ★进行中占位 turn 原地替换(2026-09-11 test02 服务器实锤):server._worker 开跑先写
        #   {"status":"running", team_id} 占位 turn(2026-09-08 用户钉死"进行中也得能看")·设计
        #   意图是"单结束由末尾 save_turn 覆盖为完整结果"·但旧实现纯 append→每单产生一条
        #   永远 running 的僵尸 turn0 + 一条完整 turn1·前端同题显示两次(一条"0/0 尚无可用
        #   结论"空转)。修:末尾完成写入时·若最后一条 turn 是【同 team_id 的 running 占位】
        #   则原地替换(保留 append 语义给真正的多轮追问/不同单)。防误替换三条件:team_id
        #   非空且相等·占位 status=running·新 result 非 running(完成/失败/取消都替换)。
        _replaced = False
        _new_tid = str(result.get("team_id") or "")
        if _new_tid and str(result.get("status") or "") != "running":
            for index in range(len(turns) - 1, -1, -1):
                previous = turns[index]
                if (isinstance(previous, dict) and previous.get("status") == "running"
                        and str(previous.get("team_id") or "") == _new_tid
                        # 同锁内核对请求身份；旧无标识调用也不能吃掉新请求的占位。
                        and str(previous.get("turn_id") or "") == _turn_id):
                    turns[index] = turn
                    _replaced = True
                    break
        if _turn_id and str(result.get("status") or "") != "running" and not _replaced:
            return {"error": "running turn unavailable"}
        if not _replaced:
            turns.append(turn)
        sess["updated"] = time.time()
        tmp = f"{p}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sess, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        _DETAIL_CACHE.pop(p, None)
        _load_summary_index()
        summary = _summary_from_session(sess, sid)
        stat = os.stat(p)
        summary["source_stamp"] = [stat.st_mtime_ns, stat.st_size]
        _SUMMARY_CACHE[sid] = summary
        _write_summary_index()
        _invalidate_list_cache(uid)
        n = len(sess["turns"])
        cont = sess.get("continuous", False)
    _invalidate_views(uid)
    return {"sid": sid, "turns": n,
            "url": f"/s/{sid}?share={sess['share_token']}", "continuous": cont}


def set_continuous(sid: str, on: bool = True, interval_hint: str = "", user_id: str = "default") -> dict:
    """标记 session 为【持续生成型】——网页会持续追加新内容/可继续对话(用户需求)。

    本机原型:标记 + 记录节奏提示(interval_hint 如 '每天' '每小时')。真正定时续写靠
    cron 任务(create_job)在部署后触发 session_append。这里先把'持续型'语义落地。
    """
    sess = load_session(sid, user_id=user_id)
    if not sess:
        return {"error": "session not found"}
    sess["continuous"] = bool(on)
    if interval_hint:
        sess["interval_hint"] = interval_hint
    sess["updated"] = time.time()
    path = _path(sid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sess, f, ensure_ascii=False, indent=2)
    with _SAVE_LOCK:
        _DETAIL_CACHE.pop(path, None)
        _load_summary_index()
        summary = _summary_from_session(sess, sid)
        stat = os.stat(path)
        summary["source_stamp"] = [stat.st_mtime_ns, stat.st_size]
        _SUMMARY_CACHE[sid] = summary
        _write_summary_index()
    _invalidate_list_cache(user_id)
    _invalidate_views(user_id)
    return {"sid": sid, "continuous": sess["continuous"],
            "interval_hint": sess.get("interval_hint", "")}


def conversation_context(sid: str, max_turns: int = 6, max_chars: int = 12000, user_id: str = "default") -> str:
    """session 最近多轮→给模型的上下文(持续对话真生效·context not control)。

    学 Hermes sessions/messages 单一事实源:读 canonical turns,保留用户问题+各家独立原答案
    (不融合)+【本轮产出的文件清单】,下一轮模型自己判断怎么沿用/续做。

    军团自审(2026-08-28 cc/codex/hermes/deepseek 一致指出)两个真缺口,这里一并修:
    · A 旧代码末尾 `[-max_chars:]` 从【字符中间硬切】,会切掉用户约束前半句/代码块开头。改为按
      【完整 turn 边界】组装,超预算时【整轮丢弃最老的】,绝不切碎单轮(学 codex token budgeter
      按消息边界压缩·不字符尾切)。
    · B 旧代码只传各家 final 文字,不传"上轮产出的文件在哪"。用户第2轮说"把上轮的PDF改一下",
      新派的 harness 看不到文件路径只能重做。现在保留每轮 artifacts(path/kind)，并区分
      历史引用类型与当前授权，避免把已交付快照误当作可原位修改的工程。
    """
    sess = load_session(sid, user_id=user_id)
    if not sess:
        return ""
    all_turns = sess.get("turns") or []
    if not all_turns:
        return ""
    # 最初意图单独保留，最近修订并列呈现；不让反复“继续”或占位挤掉目标。
    max_chars = max(0, int(max_chars))
    if not max_chars:
        return ""
    def excerpt(text, budget):
        text = str(text)
        if len(text) <= budget:
            return text
        marker = "\n[摘录；完整内容保存在本会话记录]\n"
        if budget <= len(marker):
            return text[:budget]
        head = (budget - len(marker)) * 2 // 3
        tail = budget - len(marker) - head
        return text[:head] + marker + text[-tail:]
    origin = excerpt("【本对话最初用户请求·历史事实，后续修订以最新要求为准】" +
                     str(all_turns[0].get("question") or ""), max_chars // 3)
    completed_teams = {str(t.get("team_id")) for t in all_turns
                       if t.get("team_id") and t.get("status") != "running"}
    effective = [t for t in all_turns if not (t.get("status") == "running"
                 and str(t.get("team_id") or "") in completed_teams)]
    turns = effective[-max_turns:]
    # 从最近往前累加完整 turn·到预算就停(丢最老的整轮·不切碎)——最近的对用户最相关,优先保。
    blocks, used = [], len(origin)
    for i, t in enumerate(reversed(turns)):
        seg = [f"【历史·用户】{str(t.get('question') or '')[:2000]}"]
        result = t.get("result") or t
        seg.append("【运行现场】" + json.dumps({k: result.get(k) for k in
                   ("status", "team_id", "note") if result.get(k)}, ensure_ascii=False))
        for r in (result.get("results") or []):
            if r.get("final"):
                seg.append(f"【{r.get('key','agent')} 的原答案】{str(r['final'])[:3000]}")
            facts = {k: r.get(k) for k in ("status", "error", "workdir") if r.get(k)}
            if facts:
                seg.append("【成员执行事实】" + json.dumps(facts, ensure_ascii=False))
            # 引用不是授权。按已存记录区分交付版本与普通文件，不探测可达性，
            # 不改动旧工件，也不把快照路径提升为下一轮的可写工程目录。
            arts = r.get("artifacts") or []
            if arts:
                ordinary, snapshots = [], []
                for artifact in arts:
                    location = artifact.get("path") or artifact.get("url")
                    if not location:
                        continue
                    snapshot = artifact.get("export_snapshot_id")
                    exported = (isinstance(snapshot, str) and len(snapshot) == 64
                                and all(c in "0123456789abcdef" for c in snapshot))
                    references = snapshots if exported else ordinary
                    references.append(f"{location}({artifact.get('kind','file')})")
                if ordinary:
                    seg.append(f"【{r.get('key','agent')} 历史文件引用·读改须依当前授权】"
                               + "；".join(ordinary))
                if snapshots:
                    seg.append(f"【{r.get('key','agent')} 已交付快照引用·供核对/复用，勿原位改写；"
                               "续作使用当前授权工程】" + "；".join(snapshots))
        block = "\n".join(seg)
        remaining = max_chars - used - 1
        if remaining <= 0:
            break
        if len(block) > remaining:
            if blocks:
                break
            block = excerpt(block, remaining)
        blocks.append(block); used += len(block) + 1
    blocks.reverse()           # 恢复时间正序(老→新)
    return origin + "\n" + "\n".join(blocks)


def _continuation_delivery_result(turn: dict, packet: dict, sid: str,
                                  user_id: str) -> tuple[dict, dict] | None:
    """Select a proven sole delivery, never a winner inferred from time or text.

    Legacy directoryless relay packets kept their graph but not their result
    policy. Only the same owned team's stored specification may fill that gap.
    A unique topological order is required: a parallel fork/join is not a serial
    lane merely because it eventually has one sink.
    """
    if (turn.get("status") == "running"
            or (packet.get("visibility") or {}).get("candidate_isolation") == "strict"):
        return None
    nodes = [row for row in packet.get("tasks") or [] if isinstance(row, dict)]
    results = [row for row in turn.get("results") or [] if isinstance(row, dict)]
    if (len(nodes) != len(packet.get("tasks") or [])
            or len(results) != len(turn.get("results") or [])):
        return None
    by_id = {str(node.get("task_id") or ""): node for node in nodes}
    rows = {str(row.get("task_id") or row.get("key") or ""): row for row in results}
    if (not nodes or "" in by_id or "" in rows or len(by_id) != len(nodes)
            or len(rows) != len(results) or set(by_id) != set(rows)):
        return None
    for task_id, node in by_id.items():
        row = rows[task_id]
        assigned = str(row.get("assigned_harness") or row.get("harness") or row.get("key") or "")
        if not node.get("assigned_harness") or assigned != str(node["assigned_harness"]):
            return None
    if len(nodes) == 1:
        task_id = next(iter(by_id))
        return by_id[task_id], rows[task_id]

    def graph(values):
        entries = values.items() if isinstance(values, dict) else (
            (str(row.get("task_id") or ""), row) for row in values if isinstance(row, dict))
        mapped = {}
        for key, node in entries:
            if not isinstance(node, dict) or not key or key in mapped:
                return None
            if node.get("task_id") and str(node["task_id"]) != str(key):
                return None
            deps = node.get("depends_on", node.get("dependencies"))
            if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
                return None
            mapped[str(key)] = (str(node.get("assigned_harness") or ""), frozenset(deps))
        return mapped

    dependencies = graph(nodes)
    if not dependencies or any(not deps <= set(dependencies)
                               for _, deps in dependencies.values()):
        return None
    spec = {}
    for record in (turn, packet):
        declaration = record.get("orchestration_spec") or {}
        if not isinstance(declaration, dict):
            return None
        for field in ("pattern", "result_policy"):
            value = declaration.get(field) or record.get(field)
            if value:
                if field in spec and spec[field] != value:
                    return None
                spec[field] = value
    if not all(spec.get(field) for field in ("pattern", "result_policy")):
        import team_state
        team_id = str(turn.get("team_id") or "")
        team = team_state.load(team_id) if team_id else None
        if (not isinstance(team, dict) or str(team.get("team_id") or "") != team_id
                or str(team.get("conversation_id") or "") != str(sid)
                or str(team.get("user_id") or "") != str(packet.get("user_id") or "")
                or str(team.get("user_id") or "") not in {str(user_id), "sess:" + str(sid)}
                or graph(team.get("tasks") or {}) != dependencies):
            return None
        declaration = team.get("orchestration_spec") or {}
        if not isinstance(declaration, dict):
            return None
        for field in ("pattern", "result_policy"):
            if spec.get(field) and declaration.get(field) != spec[field]:
                return None
            spec[field] = declaration.get(field)
    if (spec != {"pattern": "relay", "result_policy": "last"}
            or any(record.get("mode") not in (None, "", "auto", "relay")
                   for record in (packet, turn))):
        return None
    remaining, completed, last = dict(dependencies), set(), ""
    while remaining:
        ready = [key for key, (_, deps) in remaining.items() if deps <= completed]
        if len(ready) != 1:
            return None
        last = ready[0]
        completed.add(last)
        remaining.pop(last)
    return by_id[last], rows[last]


def continuation_project_context(sid: str, user_id: str = "default") -> dict:
    """Recover project facts via this user's session references, not a global scan."""
    session = load_session(sid, user_id=user_id)
    if not session:
        return {}
    import project_context
    for turn in reversed(session.get("turns") or []):
        team_id = str(turn.get("team_id") or "")
        if not team_id:
            continue
        packet = project_context.load(team_id)
        if (not packet or str(packet.get("conversation_id") or "") != str(sid)
                or str(packet.get("user_id") or "") not in {str(user_id), "sess:" + str(sid)}):
            continue
        fields = ("objective", "repo_root", "workspace_root", "artifacts", "completed",
                  "decisions", "open_gaps", "verification_findings", "quality_context",
                  "quality_context_revision", "experience_intent", "golden_task", "source_ref",
                  "reference_atlas", "frontier_research_contract")
        facts = {key: packet[key] for key in fields if packet.get(key) not in (None, "", [], {})}
        if facts.get("artifacts"):
            # 续作也复用已落盘轨迹的索引；不把每份产物的完整历史重复塞进规划请求。
            facts["artifacts"] = project_context._snapshot_artifacts(
                team_id, packet, list(facts["artifacts"]))
        if not any(facts.get(key) for key in ("repo_root", "workspace_root")):
            # A new dispatch may let the native executor allocate its workdir.
            # Recover that recorded fact, not a path mentioned in final/artifacts.
            selected = _continuation_delivery_result(turn, packet, sid, user_id)
            if selected:
                _, result = selected
                workdir = str(result.get("workdir") or "")
                if os.path.isabs(workdir):
                    # The execution layer validates availability and ownership.
                    # Read-only restoration never creates directories or edits old packets.
                    facts["workspace_root"] = workdir
        facts["continued_from_team_id"] = team_id
        return facts
    return {}


def continuation_native_slot(sid: str, harness: str, user_id: str,
                             context: dict) -> str:
    """Reuse one proven native lane; task labels are not conversation identity.

    Read only this owner's canonical turns and their full persisted plans.
    Ambiguous candidates keep their separate slots. A known but inconsistent
    checkpoint is an explicit recovery error, not permission to invent a new one.
    """
    if str(context.get("conversation_id") or "") != str(sid):
        return ""
    workspace = str(context.get("repo_root") or context.get("workspace_root") or "")
    if workspace and not os.path.isabs(workspace):
        return ""
    if str(context.get("user_id") or user_id) != str(user_id):
        return ""
    session = load_session(sid, user_id=user_id)
    if not session:
        return ""
    import project_context
    import task_memory

    def repair_source(source: dict, source_turn: dict) -> bool:
        repair_for = source.get("repair_for")
        return (
            source.get("native_conversation_id") == f"{sid}:contract-repair:{harness}"
            and source.get("contract_repair") is True
            and isinstance(repair_for, str) and bool(repair_for)
            and sum(1 for other in source_turn.get("results") or []
                    if isinstance(other, dict) and other is not source
                    and other.get("key") == repair_for) == 1
        )

    current_team = str(context.get("team_id") or "")
    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, dict)]
    finished_teams = set()
    for turn in reversed(turns):
        team_id = str(turn.get("team_id") or "")
        if turn.get("status") == "running":
            if team_id not in finished_teams and team_id != current_team:
                raise ValueError("native continuation has another active request")
        elif team_id:
            finished_teams.add(team_id)  # Only a later terminal supersedes a running placeholder.
    for turn_index in range(len(turns) - 1, -1, -1):
        turn = turns[turn_index]
        if turn.get("status") == "running":
            continue
        team_id = str(turn.get("team_id") or "")
        packet = project_context.load(team_id) if team_id else {}
        if (str(packet.get("conversation_id") or "") != str(sid)
                or str(packet.get("user_id") or "") != str(user_id)):
            continue
        selected = _continuation_delivery_result(turn, packet, sid, user_id)
        rows = [row for row in (turn.get("results") or [])
                if isinstance(row, dict)
                and str(row.get("harness") or row.get("assigned_harness") or row.get("key")) == harness]
        if selected:
            selected_row = selected[1]
            if str(selected_row.get("harness") or selected_row.get("assigned_harness")
                   or selected_row.get("key")) != harness:
                return ""
            rows = [selected_row]
        if not rows:
            continue
        # The snapshot may hide other PK candidates, so count the full old plan.
        old_lanes = [row for row in (packet.get("tasks") or [])
                     if isinstance(row, dict) and str(row.get("assigned_harness") or "") == harness]
        if len(rows) != 1 or (not selected and (len(old_lanes) > 1 or not workspace)):
            return ""
        row = rows[0]
        if row.get("native_session_scope") == "request":
            # Keep this delivery/project context, but do not promote history
            # explicitly scoped to one team into a later conversation.
            continue
        envelope = row.get("error_envelope")
        if (row.get("fail_kind") == "native_session_unavailable" and row.get("attempts") == 0
                and isinstance(envelope, dict) and envelope.get("phase") == "native_resume_selection"):
            # No native execution happened. Revalidate the preceding real lane
            # after storage repair instead of treating this error as a new lane.
            continue
        if row.get("native_resume_error"):
            raise ValueError("native continuation checkpoint was previously rejected")
        checkpoint = row.get("native_checkpoint") or {}
        if not isinstance(checkpoint, dict):
            raise ValueError("native continuation checkpoint is malformed")
        if not checkpoint.get("session_id"):
            return ""
        cwd = str(row.get("workdir") or "")
        target_workspace = workspace or (cwd if selected else "")
        if (not cwd or not os.path.isabs(cwd)
                or os.path.realpath(cwd) != os.path.realpath(target_workspace)):
            return ""
        if not os.path.isdir(cwd):
            raise ValueError("native continuation workspace is unavailable")
        slot = str(row.get("native_conversation_id") or "")
        if not slot:
            # Fixed legacy encodings, not a planner-selected new label.
            task_id = str(row.get("task_id") or row.get("key") or "")
            if turn.get("mode") == "single":
                slot = str(sid)
            elif (turn.get("mode") in {"pk", "orchestrate"} or
                  (selected and len(packet.get("tasks") or []) > 1)) and task_id:
                slot = f"{sid}:task:{task_id}"
        # A contract-repair call is a real native lane, not a planner label.
        # Its host-written provenance must point to another result in this
        # same owned turn; arbitrary internal-looking prefixes are not proof.
        qualified_repair = repair_source(row, turn)
        if slot == f"{sid}:contract-repair:{harness}" and not qualified_repair:
            # An ordinary role need not pretend to be a repair. Earlier owned
            # terminal provenance authorizes only this unchanged native lane;
            # the latest row/cache below remain the checkpoint authority.
            terminal = {"done", "error", "cancelled", "completed", "partial", "failed"}
            for prior in reversed(turns[:turn_index]):
                prior_status = prior.get("status")
                if prior_status is None:
                    # Legacy turns omitted top-level status. Use the same
                    # authoritative outcome terminals as save_turn, never
                    # overriding an explicit running/unknown status.
                    outcome = prior.get("outcome")
                    if (isinstance(outcome, dict) and outcome.get("status") in
                            {"completed", "partial", "failed", "cancelled"}):
                        prior_status = outcome["status"]
                if prior_status not in terminal:
                    continue
                prior_team = str(prior.get("team_id") or "")
                prior_packet = project_context.load(prior_team) if prior_team else {}
                if (str(prior_packet.get("conversation_id") or "") != str(sid)
                        or str(prior_packet.get("user_id") or "") != str(user_id)):
                    continue
                sources = [item for item in prior.get("results") or []
                           if isinstance(item, dict) and str(item.get("harness")
                           or item.get("assigned_harness") or item.get("key")) == harness]
                if (len(sources) != 1 or sum(1 for item in prior_packet.get("tasks") or []
                        if isinstance(item, dict)
                        and str(item.get("assigned_harness") or "") == harness) > 1):
                    continue
                source = sources[0]
                source_cp = source.get("native_checkpoint")
                source_cwd = str(source.get("workdir") or "")
                if (source.get("status") in terminal and not source.get("native_resume_error")
                        and repair_source(source, prior) and isinstance(source_cp, dict)
                        and all(source_cp.get(name) == checkpoint.get(name)
                                for name in ("kind", "session_id", "path", "request_binding"))
                        and os.path.isabs(source_cwd)
                        and os.path.realpath(source_cwd) == os.path.realpath(cwd)):
                    qualified_repair = True
                    break
        if not slot or (slot != sid and not slot.startswith(str(sid) + ":task:")
                        and not qualified_repair):
            raise ValueError("native continuation lane identity is unavailable")
        cached = task_memory.load(f"conv:{harness}:{slot}", harness)
        if not isinstance(cached, dict):
            raise ValueError("native continuation checkpoint storage is malformed")
        if qualified_repair and (
                cached.get("team_id") != f"conv:{harness}:{slot}"
                or cached.get("key") != harness
                or cached.get("native_conversation_id") != slot):
            raise ValueError("native continuation checkpoint storage identity is inconsistent")
        native = cached.get("native_checkpoint") or {}
        if (not isinstance(native, dict)
                or any(native.get(name) != checkpoint.get(name)
                       for name in ("kind", "session_id", "path", "request_binding"))
                or os.path.realpath(str(cached.get("workdir") or "")) != os.path.realpath(cwd)):
            raise ValueError("native continuation checkpoint does not match the latest owned result")
        return slot
    return ""


def _load_session_record(sid: str) -> dict | None:
    """Read the canonical session without applying an authorization policy."""
    p = _path(sid)
    try:
        stat = os.stat(p)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    with _SAVE_LOCK:
        cached = _DETAIL_CACHE.get(p)
        if cached and cached[0] == stamp:
            sess = cached[1]
        else:
            try:
                with open(p, encoding="utf-8") as handle:
                    sess = json.load(handle)
            except Exception:
                return None
            _DETAIL_CACHE[p] = (stamp, sess)
            if len(_DETAIL_CACHE) > _DETAIL_CACHE_MAX:
                _DETAIL_CACHE.pop(next(iter(_DETAIL_CACHE)))
    return sess


def load_session(sid: str, user_id: str = "default") -> dict | None:
    sess = _load_session_record(sid)
    if not sess:
        return None
    # 旧session无user_id只归default·其他用户不可读(防跨用户会话/文件泄漏)
    if str(sess.get("user_id") or "default") != str(user_id or "default"):
        return None
    # 旧会话曾只保存成员用量、丢了整单汇总。复用原生用量投影，既不重跑模型，
    # 也不改磁盘或详情缓存；未留存的审计/补位开销不能补成零或声称已恢复。
    turns = []
    for turn in sess.get("turns") or []:
        if isinstance(turn, dict):
            nested = isinstance(turn.get("result"), dict)
            result = turn["result"] if nested else turn
            if not result.get("usage"):
                import token_telemetry
                usage = token_telemetry.summarize(
                    [row for row in result.get("results") or [] if isinstance(row, dict)])
                if usage["available_members"]:
                    projected = {**result, "usage": {**usage, "scope": "recorded_members_only"}}
                    turn = {**turn, "result": projected} if nested else projected
        turns.append(turn)
    return {**sess, "turns": turns}


def latest_source_ref(sid: str, user_id: str = "default") -> dict:
    """Return the newest durable external source attached to a session.

    Follow-up turns may identify themselves as ``session_continue`` without repeating the
    original URL.  The original source remains the provenance anchor; the new question is
    already preserved separately on the new turn.
    """
    sess = load_session(sid, user_id=user_id)
    if not sess:
        return {}
    for turn in reversed(sess.get("turns") or []):
        source = turn.get("source_ref") if isinstance(turn, dict) else None
        if isinstance(source, dict) and str(source.get("url") or "").strip():
            return dict(source)
    return {}


def load_shared_session(sid: str, share_token: str) -> dict | None:
    """Authorize a public delivery page with an unguessable per-session capability token."""
    sess = _load_session_record(sid)
    expected = str((sess or {}).get("share_token") or "")
    supplied = str(share_token or "")
    return sess if expected and supplied and hmac.compare_digest(expected, supplied) else None


def list_sessions(limit: int = 50, user_id: str = "default") -> list[dict]:
    _ensure_dir()
    uid = str(user_id or "default")
    cache_key = (uid, int(limit))
    stamp = _directory_stamp()
    with _SAVE_LOCK:
        cached = _LIST_CACHE.get(cache_key)
        if cached and cached[0] == stamp:
            return [dict(row) for row in cached[1]]
    out, changed = [], False
    with _SAVE_LOCK:
        _load_summary_index()
        directory = sessions_dir()
        filenames = [name for name in os.listdir(directory)
                     if name.endswith(".json") and not name.startswith(".")]
        live = {name[:-5] for name in filenames}
        for sid in [sid for sid in _SUMMARY_CACHE if sid not in live]:
            _SUMMARY_CACHE.pop(sid, None)
            changed = True
        for fn in filenames:
            sid = fn[:-5]
            try:
                file_stat = os.stat(os.path.join(directory, fn))
                source_stamp = [file_stat.st_mtime_ns, file_stat.st_size]
            except OSError:
                continue
            row = _SUMMARY_CACHE.get(sid)
            if (row is None or row.get("source_stamp") != source_stamp
                    or row.get("_projection_version") != _SUMMARY_PROJECTION_VERSION):
                try:
                    with open(os.path.join(directory, fn), encoding="utf-8") as handle:
                        row = _summary_from_session(json.load(handle), sid)
                    row["source_stamp"] = source_stamp
                    _SUMMARY_CACHE[sid] = row
                    changed = True
                except Exception:
                    continue
            if str(row.get("user_id") or "default") == uid:
                public = dict(row)
                public.pop("user_id", None)
                public.pop("source_stamp", None)
                public.pop("_projection_version", None)
                out.append(public)
        if changed:
            _write_summary_index()
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    result = out[:limit]
    with _SAVE_LOCK:
        _LIST_CACHE[cache_key] = (_directory_stamp(), [dict(row) for row in result])
    return result


def _render_deliverable(text: str) -> str:
    """按内容类型渲染交付物(不只是纯文本):
    - ```lang 代码块 → <pre><code>（保留缩进）
    - Markdown 链接 / 裸 URL → 可点链接(target=_blank)
    - Markdown 表格 → 真 <table>
    - # 标题 / **粗体** / 换行 → 轻量富文本
    所有原文先 HTML escape,防 XSS；只放行我们自己生成的安全标签。
    """
    import re
    raw = str(text or "")
    # 先提取代码块,用占位符保护(代码不做 markdown 处理)
    blocks = []
    def _code(m):
        lang = html.escape(m.group(1) or "")
        code = html.escape(m.group(2).strip("\n"))
        blocks.append(f'<pre class="code"><div class="code-lang">{lang or "code"}</div><code>{code}</code></pre>')
        return f"\x00CODE{len(blocks)-1}\x00"
    s = re.sub(r"```([\w+-]*)\n(.*?)```", _code, raw, flags=re.S)

    # 多媒体渲染:图片/音频/视频 URL → 真媒体标签(军团出的图/上传的图都能显示)。
    #   在 html.escape 前用占位符保护(和代码块同机制·复用 blocks 还原循环),
    #   避免后续裸URL转链接把媒体地址错当成 <a>。只放行自家 /media/ 与常见图片后缀。
    def _media_from_url(url):
        low = url.lower()
        safe = html.escape(url, quote=True)
        if any(low.split("?")[0].endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp")) \
                or ("/media/" in low and not any(low.endswith(e) for e in (".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a"))):
            tag = f'<img class="deliv-media" src="{safe}" alt="军团产出图片" loading="lazy">'
        elif any(low.split("?")[0].endswith(e) for e in (".mp4", ".mov", ".webm")):
            tag = f'<video class="deliv-media" src="{safe}" controls preload="metadata"></video>'
        elif any(low.split("?")[0].endswith(e) for e in (".mp3", ".wav", ".m4a", ".ogg")):
            tag = f'<audio src="{safe}" controls preload="none"></audio>'
        else:
            return url  # 不是媒体·交给后续链接处理
        blocks.append(tag)
        return f"\x00CODE{len(blocks)-1}\x00"
    # 匹配 markdown 图片 ![alt](url)、/media/ 相对路径、以及带图片/媒体后缀的 http url
    s = re.sub(r"!\[[^\]]*\]\((/media/[^\s)]+|https?://[^\s)]+)\)",
               lambda m: _media_from_url(m.group(1)), s)
    s = re.sub(r"(?<![\(\"'>])(/media/[\w./-]+)",
               lambda m: _media_from_url(m.group(1)), s)
    s = re.sub(r"(?<![\(\"'>])(https?://[^\s<)]+\.(?:png|jpg|jpeg|gif|webp|mp4|mov|webm|mp3|wav|m4a|ogg))(?![\w])",
               lambda m: _media_from_url(m.group(1)), s, flags=re.I)

    s = html.escape(s)

    # Markdown 表格:连续至少2行包含 |,第二行是 --- 分隔
    lines = s.splitlines()
    out, i = [], 0
    while i < len(lines):
        if (i + 1 < len(lines) and "|" in lines[i] and
                re.match(r"^\s*\|?\s*:?-+", html.unescape(lines[i + 1]))):
            headers = [x.strip() for x in lines[i].strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i]:
                rows.append([x.strip() for x in lines[i].strip("|").split("|")])
                i += 1
            tbl = '<table><thead><tr>' + ''.join(f'<th>{x}</th>' for x in headers) + '</tr></thead><tbody>'
            tbl += ''.join('<tr>' + ''.join(f'<td>{x}</td>' for x in row) + '</tr>' for row in rows)
            tbl += '</tbody></table>'
            out.append(tbl); continue
        out.append(lines[i]); i += 1
    s = "\n".join(out)

    # 标题 / 粗体 / Markdown链接 / 裸URL / 换行
    s = re.sub(r"(?m)^###\s+(.+)$", r"<h4>\1</h4>", s)
    s = re.sub(r"(?m)^##\s+(.+)$", r"<h3>\1</h3>", s)
    s = re.sub(r"(?m)^#\s+(.+)$", r"<h2>\1</h2>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    # 裸 URL:前面不是 "(属性值) 或 >(标签内文本已被链接包裹) 才转,避免破坏上面已生成的 <a>
    s = re.sub(r'(?<![">])(https?://[^\s<)]+)',
               r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    s = s.replace("\n", "<br>")
    # 还原代码块占位符(html.escape 会保留 NUL)
    for idx, block in enumerate(blocks):
        s = s.replace(f"\x00CODE{idx}\x00", block)
    return s


def render_session_html(sid: str, user_id: str = "default", share_token: str = "") -> str | None:
    """把 session 渲染成独立交付页；所属用户或分享令牌二者满足其一即可。"""
    sess = load_session(sid, user_id=user_id) or load_shared_session(sid, share_token)
    if not sess:
        return None
    turns = sess.get("turns", [])
    continuous = sess.get("continuous", False)
    interval = sess.get("interval_hint", "")
    e = html.escape
    # 与主对话页的模式名称一致；这里展示保存记录，不按结果数量猜测执行或协作。
    mode_labels = {
        "single": "单家", "solo": "单家快路", "direct_model": "模型直答",
        "pk": "PK", "relay": "接力", "orchestrate": "首脑", "auto": "智能选择",
    }

    def _fmt_ts(ts):
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except Exception:
            return ""

    turn_html = []
    for i, t in enumerate(turns):
        results = t.get("results", [])
        mode = t.get("mode")
        mode = mode.strip() if isinstance(mode, str) else ""
        cards = []
        for r in results:
            ok = r.get("status") == "done"
            badge = "成员已返回" if ok else ("⏱ 超时" if r.get("status") == "timeout" else "✗ 未跑通")
            body = _render_deliverable(r.get('final', '')) if ok else e(str(r.get('error', '')))
            artifact_html = []
            for a in (r.get("artifacts") or []):
                name, url, kind = e(str(a.get("name", "artifact"))), e(str(a.get("url", ""))), a.get("kind")
                open_link = ""
                if str(a.get("name", "")).lower().endswith(".html"):
                    # HTML 也可能是供其他页面使用的片段；默认只列文件，不自动执行它。
                    # template 内容保持惰性，用户展开后才创建 iframe，收起时卸载。
                    open_link = (f' <a href="{url}" data-html-open="{name}" target="_blank" '
                                 f'rel="noopener noreferrer">打开 {name} ↗</a>')
                    preview = (f'<details class="html-preview"><summary>预览 {name}</summary>'
                               '<div class="html-preview-host"></div><template>'
                               f'<iframe src="{url}" title="{name} 预览" sandbox="allow-scripts allow-forms" '
                               'style="width:100%;height:360px;border:1px solid #26344a;border-radius:8px;background:white"></iframe>'
                               '</template></details>')
                else:
                    preview = f'<video src="{url}" controls style="max-width:100%"></video>' if kind == "video" else ""
                artifact_html.append(f'<div style="margin-top:10px"><a href="{url}" download>📦 {name} · 打开/下载</a>{open_link}{preview}</div>')
            cards.append(f"""
              <div class="deliv-card {'ok' if ok else 'bad'}">
                <div class="dc-head"><b>{e(str(r.get('key','')))}</b>
                  <span class="dc-badge">{badge}</span>
                  <span class="dc-meta">{e(str(r.get('elapsed','')))}s</span></div>
                <div class="dc-final">{body}{''.join(artifact_html)}</div>
              </div>""")
        mode_label = ("记录模式：" + mode_labels.get(mode, f"未识别（{mode}）")
                      if mode else "记录模式未上报")
        outcome = t.get("outcome") or {}
        if t.get("cancelled") or outcome.get("status") == "cancelled":
            outcome_note = "本轮已停止；已保留实际完成的工作，停止不代表交付完成。"
        elif outcome.get("contract_incomplete") or t.get("contract_incomplete"):
            outcome_note = "要求的成品尚未齐全；以下包括过程文件和成员回复。"
        elif outcome.get("verification_incomplete") or t.get("verification_incomplete"):
            outcome_note = "成品仍待验证；已有候选结果，但验证尚未完成。"
        elif outcome.get("status") == "completed":
            outcome_note = "本轮已完成；具体交付及验证范围以记录为准。"
        elif outcome.get("status"):
            outcome_note = "本轮状态：" + str(outcome["status"])
        else:
            outcome_note = "旧记录未保留整单验证结果；成员返回不代表成品已验收。"
        final_note = ""
        if mode == "relay" and t.get("final_result"):
            final_note = f'<div class="deliv-final">最终成果：{_render_deliverable(t["final_result"].get("final",""))}</div>'
        turn_html.append(f"""
          <div class="turn">
            <div class="turn-q"><span class="turn-idx">#{i+1}</span> {e(t.get('question',''))}</div>
            <div class="turn-mode">{e(mode_label)} · {_fmt_ts(t.get('ts'))}</div>
            <div class="turn-outcome">{e(outcome_note)}</div>
            <div class="deliv-grid">{''.join(cards)}</div>
            {final_note}
          </div>""")

    title = e(turns[0]["question"][:50]) if turns else "军团交付物"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 军团交付物</title>
{'<meta http-equiv="refresh" content="30">' if continuous else ''}
<style>
:root{{--bg:#0a0f1a;--panel:#111a2e;--line:#233149;--text:#e8eef7;--muted:#93a4bd;--dim:#5f7290;--ok:#43d99a;--err:#ff6b6b;--accent:#5b8cff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--muted);font-size:12.5px;margin-bottom:22px}}
.turn{{border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:16px;background:var(--panel)}}
.turn-q{{font-size:16px;font-weight:800}}.turn-idx{{color:var(--accent);margin-right:6px}}
.turn-mode{{color:var(--muted);font-size:11.5px;margin:3px 0 12px}}
.deliv-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}}
.deliv-card{{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#0b1322}}
.deliv-card.ok{{border-color:#2b6b52}}.deliv-card.bad{{border-color:#5a3838}}
.dc-head{{display:flex;align-items:center;gap:8px;font-size:13px}}.dc-badge{{font-size:10px;color:var(--muted)}}.dc-meta{{margin-left:auto;color:var(--dim);font-size:10px}}
.dc-final{{margin-top:6px;font-size:13px;color:var(--text);word-break:break-word}}
.dc-final a{{color:#6eb6ff}}.dc-final h2,.dc-final h3,.dc-final h4{{margin:8px 0 4px}}
.dc-final pre.code{{background:#05080f;border:1px solid var(--line);border-radius:8px;padding:0;margin:8px 0;overflow-x:auto}}
.dc-final pre.code .code-lang{{color:var(--dim);font-size:10px;padding:4px 10px;border-bottom:1px solid var(--line)}}
.dc-final pre.code code{{display:block;padding:10px;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#c8d6ee;white-space:pre}}
.dc-final table{{border-collapse:collapse;margin:8px 0;font-size:12px;width:100%}}
.dc-final th,.dc-final td{{border:1px solid var(--line);padding:5px 8px;text-align:left}}
.dc-final th{{background:#0d1830;color:var(--muted)}}
.html-preview{{margin-top:8px}}.html-preview summary{{cursor:pointer;color:#6eb6ff;overflow-wrap:anywhere}}
.html-preview summary:focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}
.html-preview-host:not(:empty){{margin-top:8px}}
.deliv-final{{margin-top:10px;padding:9px 11px;border-left:3px solid var(--ok);background:#0b1c14;border-radius:3px 8px 8px 3px;font-size:13.5px}}
.foot{{margin-top:22px;color:var(--dim);font-size:11.5px;text-align:center}}
</style></head><body><div class="wrap">
<h1>🛡️ 军团交付物{' · 🔄 持续生成型' if continuous else ''}</h1>
<div class="sub">session {e(sid)} · {len(turns)} 轮 · {'持续生成中(每30秒自动刷新看最新)' + (' · 节奏:' + e(interval) if interval else '') if continuous else '可持续更新/继续对话'}</div>
{''.join(turn_html) or '<div class="turn">(暂无交付)</div>'}
<div class="foot">觅游 Agent 军团 · 每个 session 一个可分享的交付物网页</div>
</div><script>
// 单文件导出会将 HTML 内联为 data URL；顶层导航不可用，改为明确的本页沙箱预览。
document.querySelectorAll('a[data-html-open]').forEach(function(link, index) {{
  const url = (link.getAttribute('href') || '').toLowerCase();
  if (!url.startsWith('data:text/html;') && !url.startsWith('data:text/html,')) return;
  const preview = link.parentElement.querySelector('details.html-preview');
  if (!preview) return;
  preview.id = 'html-preview-' + index;
  link.setAttribute('href', '#' + preview.id);
  link.removeAttribute('target');
  link.textContent = '在本页打开 ' + link.dataset.htmlOpen;
  link.addEventListener('click', function(event) {{
    event.preventDefault();
    preview.open = true;
    preview.querySelector('summary').focus();
  }});
}});
document.addEventListener('toggle', function(event) {{
  const preview = event.target;
  if (!preview.matches || !preview.matches('details.html-preview')) return;
  const host = preview.querySelector('.html-preview-host');
  if (!preview.open) {{ host.replaceChildren(); return; }}
  if (!host.firstChild) host.appendChild(preview.querySelector('template').content.cloneNode(true));
}}, true);
</script></body></html>"""


def _inline_media(page: str) -> tuple[str, int]:
    """把交付页里的 /media/<file> 内联成 data URI,确保导出单文件离开服务也能显示。

    只读 ~/.meyo/legion/media 和 uploads,只按 basename(防路径穿越)。返回(新页面,内联文件数)。
    """
    import base64
    import mimetypes
    import re
    dirs = [_paths.media_dir(), _paths.uploads_dir()]
    count = 0

    def repl(m):
        nonlocal count
        name = os.path.basename(m.group(1))
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    blob = open(p, "rb").read()
                    mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
                    count += 1
                    return "data:" + mime + ";base64," + base64.b64encode(blob).decode()
                except Exception:
                    pass
        return m.group(0)  # 找不到就保留原 URL,不谎称内联成功
    return re.sub(r"/media/([\w.-]+)", repl, page), count


def export_session(sid: str, out_dir: str | None = None) -> dict:
    """导出为【单文件、自包含、可直接部署】的 HTML 交付物。

    默认写到 ~/.legion/exports/<sid>/index.html。文件内联全部 CSS、无外部依赖,
    可直接上传 GitHub Pages/S3/任意静态服务器(部署新仓库时换真域名)。
    """
    page = render_session_html(sid)
    if page is None:
        return {"error": "session not found"}
    page, inlined = _inline_media(page)  # 媒体内联,离线也能显示图片
    if out_dir is None:
        out_dir = _paths.home_under("exports", sid)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return {"sid": sid, "path": path, "filename": f"legion-{sid}.html",
            "bytes": os.path.getsize(path), "deployable": True, "media_inlined": inlined,
            "note": f"单文件自包含·媒体已内联{inlined}个·可直接上传任意静态托管"}


if __name__ == "__main__":
    sid = new_session_id("test")
    save_turn(sid, "2的10次方?", {"results": [{"key": "codex", "status": "done",
              "final": "1024", "elapsed": 8}], "mode": "pk"})
    print("saved", sid, "-> /s/" + sid)
    print("list:", list_sessions())
