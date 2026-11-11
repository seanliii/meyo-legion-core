#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团内核 (legion_core) —— 薄封装 · 给模型 context 不加 control

设计哲学(承接 meyo-orchestrator·2026-08-27 上线版):
  - 军团 = 一群可并行调度的 harness(auto/cc/codex/deepseek/hermes/penguin/jcode/prime/
    deepagents/omnigent 共10家真harness)。cua/supervision/herdr/orca 是【能力后端】不是
    harness(桌面/CV/终端/并行编排)·归工具能力·不进派单池(2026-08-28 定位纠正)。
  - 用户一句话派活 → 解析出"谁干什么" → 并行真调各家 → 实时推每家过程。
  - 不做已证伪死路:不强制融合各家答案、不投票门控、不 topic 路由。
    只把各家原始结果并列呈现,控制权交用户(选谁最强/继续谁)。
  - auto 尽量简洁:默认经 orchestrator.run_harness 调,不直接背巨型 qa_auto_agent;
    某家跑不通就如实标 error 并跳过,绝不让服务崩(先上线为主·保证无 bug)。

对外只暴露三个函数,供 server.py 的 /api/legion/* 调用:
  - list_harnesses()               列出军团全部成员(客观,无排序/推荐)
  - dispatch(question, plan, cb)   一句话派活并行执行,cb 收到每家每一步事件
  - run_one(key, task, cb)         单独派一家(供 fork 子 agent 用)
"""
from __future__ import annotations
import os
import sys
import time
import copy
import json
import base64
import re
import subprocess
import threading
import traceback
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Optional

try:
    import fcntl as _fcntl
except ImportError:  # Windows 退回进程内锁
    _fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
# 定位 meyo-orchestrator(军团编排层)。按优先级找,兼容"仓库内"和"打包分发"两种布局:
#   1) 环境变量 MEYO_ORCH_DIR 显式指定
#   2) 打包发布布局: ../meyo-orchestrator (make-clean-release.sh 产物)
#   3) 仓库内布局:   ../../harness_baselines/meyo-orchestrator
_ORCH_CANDIDATES = [
    os.environ.get("MEYO_ORCH_DIR"),
    os.path.join(HERE, "..", "meyo-orchestrator"),
    os.path.join(HERE, "..", "..", "harness_baselines", "meyo-orchestrator"),
]
_ORCH_DIR = ""
for _c in _ORCH_CANDIDATES:
    if _c and os.path.isfile(os.path.join(_c, "orchestrator.py")):
        _ORCH_DIR = os.path.abspath(_c)
        break
if not _ORCH_DIR:  # 都没找到,退回仓库内路径(让报错信息指向它)
    _ORCH_DIR = os.path.abspath(_ORCH_CANDIDATES[-1])
if _ORCH_DIR not in sys.path:
    sys.path.insert(0, _ORCH_DIR)

try:
    import orchestrator as _ORCH  # noqa: E402
    _ORCH_OK = True
    _ORCH_ERR = ""
except Exception as e:  # 编排层缺失也不让整个服务起不来
    _ORCH = None
    _ORCH_OK = False
    _ORCH_ERR = f"{type(e).__name__}: {e}"


# ── 军团成员 ───────────────────────────────────────────────────────────
# list_harnesses() 每次实时探活21家CLI+外部agent probe·真实测 8s(前端"军团各家"面板每次点开
# 用户白等·dispatch 前置最大头)。注册了哪些 adapter/available 状态变化极慢(装卸家才变),给完整
# 结果加 60s TTL 缓存→8s→~0s。同"限时+TTL+预热"模式(见 _AIGC_READY / _harness_keys)。
# 装/卸家后 invalidate_harness_keys_cache() 一并失效。
_HARNESS_LIST_TTL = 60.0
_HARNESS_LIST_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_HARNESS_LIST_LOCK = threading.RLock()

# stale-while-revalidate:探针缓存过期时,不让当前请求同步等 7-8.8s 重填,而是【先返回旧值
# 让用户立即起跑·后台线程异步重填】(用户 2026-09-01 loop:保证任何时刻 dispatch 都 1-2s 起跑,
# 即使周期刷新守护恰好没跑到/异常,过期缓存也不阻塞用户)。只有【完全无旧值】(进程首次)才同步填。
# _refreshing 去重:同一缓存同时只起一个后台刷新线程,不因高并发派活起一堆重复探活。
_SWR_REFRESHING: set = set()
_SWR_LOCK = threading.Lock()

# single-flight:进程刚起、缓存全空时,多个并发请求(前端首屏并发打 harnesses+capabilities+health,
# 或多用户同时新开页面)会各自触发一次首次同步冷填→8 并发实测每个都卡 13s(惊群 thundering herd·
# 用户 2026-09-01 loop 揪出)。修:首次冷填只让【第一个】线程真探活,其余线程等它填完直接读缓存
# (总探活=1·所有请求拿真实结果·总耗时=1次探活而非每个13s)。按 name 分组各一把填充锁。
_SINGLEFLIGHT_LOCKS: dict = {}
_SINGLEFLIGHT_META_LOCK = threading.Lock()


def _singleflight_lock(name: str) -> threading.Lock:
    with _SINGLEFLIGHT_META_LOCK:
        lk = _SINGLEFLIGHT_LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            _SINGLEFLIGHT_LOCKS[name] = lk
        return lk


def _gateway_env() -> dict:
    """给 call_model 子进程一份干净环境:内网 AIGC 网关绕开公网/socks 死代理。

    血泪根因([[codex-rc1-real-cause-dead-proxy.md]]):用户机上常有 socks5 死代理
    (ALL_PROXY=socks5h://127.0.0.1:15235),规划器/commander 子进程继承后连不上内网网关
    aigc.sankuai.com,一律超时/no JSON,退默认家、丢智能编排。把网关 host 加进 NO_PROXY
    并清掉 socks 代理,子进程直连内网,不依赖那个可能死掉的公网代理。稳定性底线。
    """
    env = dict(os.environ)
    gateway_hosts = "aigc.sankuai.com,127.0.0.1,localhost"
    existing = env.get("NO_PROXY", "") or env.get("no_proxy", "")
    merged = (existing + "," + gateway_hosts).strip(",") if existing else gateway_hosts
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    for key in ("ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    return env


def _swr_bg_refresh(name: str, refill: Callable[[], object]) -> None:
    """后台异步重填一个探针缓存;同名去重,失败静默(纯加速)。"""
    with _SWR_LOCK:
        if name in _SWR_REFRESHING:
            return
        _SWR_REFRESHING.add(name)

    def _run():
        try:
            refill()
        except Exception:
            pass
        finally:
            with _SWR_LOCK:
                _SWR_REFRESHING.discard(name)
    threading.Thread(target=_run, name=f"swr-{name}", daemon=True).start()


def _list_harnesses_raw() -> list[dict]:
    if _ORCH_OK and _ORCH is not None:
        try:
            return _ORCH.list_harnesses()
        except Exception:
            pass
    # 编排层不可用时的最小回退清单,保证前端仍能渲染
    return [
        {"key": "cc", "display_name": "Claude Code", "cli_hint": "claude -p"},
        {"key": "codex", "display_name": "Codex", "cli_hint": "codex exec"},
    ]


def list_harnesses(force: bool = False) -> list[dict]:
    """列出军团全部成员(key/display_name/cli_hint)。无能力评分/排序/推荐。

    带 60s TTL 缓存:前端"军团各家"面板/首脑候选/health 都受益,不再每次白等 8s 探活。
    """
    def _refill():
        data = _list_harnesses_raw()
        with _HARNESS_LIST_LOCK:
            _HARNESS_LIST_CACHE["data"] = [dict(h) for h in data]
            _HARNESS_LIST_CACHE["ts"] = time.time()
    with _HARNESS_LIST_LOCK:
        cached = _HARNESS_LIST_CACHE["data"]
        fresh = cached is not None and (time.time() - _HARNESS_LIST_CACHE["ts"] < _HARNESS_LIST_TTL)
    if not force and fresh and isinstance(cached, list):
        return [dict(h) for h in cached]
    # 过期但有旧值→先返回旧值让用户立即起跑·后台异步重填(SWR);完全无值或 force 才同步填。
    if not force and cached is not None:
        _swr_bg_refresh("harness_list", _refill)
        return [dict(h) for h in cached]
    # 首次冷填:single-flight——只第一个线程真探活,其余等它填完直接读缓存(治并发惊群)。
    with _singleflight_lock("harness_list"):
        with _HARNESS_LIST_LOCK:  # 二次检查:等锁期间别的线程可能已填好
            _c = _HARNESS_LIST_CACHE["data"]
            _f = _c is not None and (time.time() - _HARNESS_LIST_CACHE["ts"] < _HARNESS_LIST_TTL)
        if force or not _f:
            _refill()
    with _HARNESS_LIST_LOCK:
        return [dict(h) for h in (_HARNESS_LIST_CACHE["data"] or [])]


def omnigent_sub_harnesses() -> dict:
    """列出军团经 omnigent 可代跑的子家(cursor/kimi/goose/qwen…军团本来没有的·量大管饱)。

    让用户/UI 能发现并选择"经 omnigent 派哪个子家"(D类'能发现吗'对omnigent的落地)。
    动态读 omnigent registry·不硬编码。omnigent 未装则空。
    """
    try:
        import sys as _sys
        if _ORCH_DIR not in _sys.path:
            _sys.path.insert(0, _ORCH_DIR)
        _mh = os.path.join(_ORCH_DIR, "meyo_harnesses")
        if _mh not in _sys.path:
            _sys.path.insert(0, _mh)
        from backends import omnigent_backend as ob
        p = ob.probe()
        if not p.get("available"):
            return {"available": False, "reason": p.get("reason"), "subs": []}
        status = p.get("sub_status", [])
        verified = [s["name"] for s in status if s.get("status") == "verified"]
        return {"available": True, "count": p.get("sub_count", 0),
                "subs": p.get("sub_harnesses", []),
                "sub_status": status,  # 诚实分层:verified(真出答案)/discoverable(按需装extra·依omnigent lazy)
                "verified": verified,
                "how": "派活给 omnigent·设 MEYO_OMNIGENT_HARNESS=<子家>·默认openai-agents(已验证)·其余首次用omnigent提示装extra"}
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {str(e)[:100]}", "subs": []}


# _harness_keys 只要"有哪些合法家"的 key 集合(校验/候选池/pool),不需要"这一秒某家CLI在不在线"
# 的实时性——available 状态由 list_harnesses() 完整结果承载(前端 harnesses 端点走那条·实时性不变)。
# 但 list_harnesses() 每次实时探活21家CLI+外部agent probe·真实测 8.8s(dispatch前置最大头·每次派活
# 用户白等)。给 key 集合单独加 60s TTL 缓存(注册了哪些adapter变化极慢)→dispatch前置 8.8s→~0s。
# 同"限时+TTL+预热"模式(见 _AIGC_READY / _ECO_PROBE / external updates)。装/卸新家后 invalidate 失效。
_HARNESS_KEYS_TTL = 60.0
_HARNESS_KEYS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_HARNESS_KEYS_LOCK = threading.RLock()


def invalidate_harness_keys_cache() -> None:
    """装/卸家、外部 agent 变更后调用,让下次 _harness_keys()/list_harnesses() 重新探活。"""
    with _HARNESS_KEYS_LOCK:
        _HARNESS_KEYS_CACHE["data"] = None
        _HARNESS_KEYS_CACHE["ts"] = 0.0
    with _HARNESS_LIST_LOCK:
        _HARNESS_LIST_CACHE["data"] = None
        _HARNESS_LIST_CACHE["ts"] = 0.0


def _harness_keys(force: bool = False) -> set:
    def _refill():
        keys = {h["key"] for h in list_harnesses(force=True)}
        # 派单池纯净性:refill 路径同样必须滤掉 capability/平台面家(2026-09-07 军团成员
        # 自选 UI 暴露实锤:refill 后本函数返回 36 家含 browser-use/paperclip/aeon——
        # 冷占位才是过滤后的 27 家,同一函数两种结果=血泪复发。list_harnesses 是
        # "军团各家"面板的全量源,绝不能直接当答题池;_aigc_ready 探测路径也以本函数
        # 为 key 源(:6323),这里滤干净全链路自动纯净。
        try:
            import external_agent_registry as _er_refill
            non_answer = {str(a.get("key")) for a in _er_refill.load_catalog()
                          if (a.get("execution_role") or "agent") in _NON_ANSWER_ROLES}
            # 内置manifest家同源过滤(2026-09-08用户钉死'auto剔除军团':外部catalog早有此链,
            # builtin家(auto=qa_system本体·多次UNKNOWN占位实锤)现在同接入——capability/
            # control-plane/ui-platform永不进答题池,单一条词表三处生效)。
            try:
                import runtime_manifest as _rm_refill
                _defs = _rm_refill.load_definitions(force=True)
                non_answer |= {str(k) for k, dfn in _defs.items()
                               if str((dfn.get("runtime") or {}).get("execution_role")
                                      or dfn.get("execution_role") or "agent") in _NON_ANSWER_ROLES}
            except Exception:
                pass
            keys -= {k for k in non_answer if k}
        except Exception:
            pass
        with _HARNESS_KEYS_LOCK:
            _HARNESS_KEYS_CACHE["data"] = set(keys)
            _HARNESS_KEYS_CACHE["ts"] = time.time()
    with _HARNESS_KEYS_LOCK:
        cached = _HARNESS_KEYS_CACHE["data"]
        fresh = cached is not None and (time.time() - _HARNESS_KEYS_CACHE["ts"] < _HARNESS_KEYS_TTL)
    if not force and fresh and isinstance(cached, set):
        return set(cached)
    # SWR:过期有旧值→立即返回旧值+后台异步重填;首次/force 同步填。
    if not force and isinstance(cached, set):
        _swr_bg_refresh("harness_keys", _refill)
        return set(cached)
    # 首次冷填:single-flight(治并发惊群·只第一个探活)。★并发拖累根治(用户 2026-09-01 loop·
    # py-spy揪出):旧版拿不到锁的线程 with _singleflight_lock 会【阻塞等】第一个线程探活完(21家
    # ~23s)→N家并发全串行化(单跑4s·并发15s)。改:拿到锁的同步探活填缓存;拿不到锁的线程【不等】·
    # 立即返回"全部已注册adapter名"乐观占位(不探活·秒回)·并触发后台填真值。key 校验用全集占位
    # 完全够(run_one 只判 key 合法·真不可跑的家 run 时自然失败→补位接力兜底)。
    if not force:
        # 冷/过期:任何线程都【不在热路径同步探活】——触发一次后台 force 填(_swr_bg_refresh 去重·
        # 只起一个后台任务)·自己立即返回占位(有旧值用旧值·否则全集占位)。派活热路径永不付探活成本
        # (用户 2026-09-01 loop:抢到锁的线程也不该阻塞·和 _aigc_ready 对称)。
        _swr_bg_refresh("harness_keys", _refill)
        with _HARNESS_KEYS_LOCK:
            _c = _HARNESS_KEYS_CACHE["data"]
        if _c:
            return set(_c)
        # 全集占位·秒回不探活:core家(_STANDALONE_ADAPTERS常量)+ external catalog(读json快)
        # auto已彻底剔除军团(2026-09-08用户钉死·不参与任何环节)
        _keys = {"codex", "cc", "hermes", "penguin", "jcode", "deepseek", "prime",
                 "deepagents", "omnigent"}
        try:
            import harness_registry as _hr
            _keys = set(_hr._STANDALONE_ADAPTERS.keys())
        except Exception:
            pass
        try:
            import external_agent_registry as _er
            _keys |= _answer_keys_from_catalog(_er.load_catalog())
        except Exception:
            pass
        try:
            import runtime_manifest as _rm
            definitions = _rm.load_definitions()
            _keys -= set(definitions) - _answer_keys_from_catalog(definitions.values())
        except Exception:
            pass
        return _keys
    # force=True(装卸后强刷):同步 single-flight 探活(force 路径不追求并发·要真值)
    with _singleflight_lock("harness_keys"):
        _refill()
    with _HARNESS_KEYS_LOCK:
        return set(_HARNESS_KEYS_CACHE["data"] or set())


# capability 角色过滤·单一事实源(2026-09-02 用户钉死[代码固化·修一类不是修一个]):
# browser-use 血泪——三跑实锤冷启动 planner 把 capability 家(9家)当答题家派·run 才撞
# unavailable。capability/control-plane/ui-platform 是能力后端/平台面·永不进答题池(现在+未来新家自动覆盖)。
# 词表与 runtime_manifest._validate/agent_adapters._is_answer_agent 同源。行为回归锁+AST 静态守卫:tests/test_dispatch_pool_guards.py。
_NON_ANSWER_ROLES = ("capability", "control-plane", "ui-platform")

def _answer_keys_from_catalog(items):
    # catalog 条目里只取答题家 key。单一词表·两处派单消费点(_harness_keys 占位/_aigc_ready warming 占位)统一走这里。
    return {a.get("key") for a in items
            if a.get("key")
            and ((a.get("runtime") or {}).get("execution_role")
                 or a.get("execution_role") or "agent") not in _NON_ANSWER_ROLES}
_CAPTAIN_MARK_RE = re.compile(r"<<LEGION_CAPTAIN>>\s*(\{.*?\})\s*<<END>>", re.DOTALL)


def _parse_captain_bid(final: str) -> tuple[str, dict | None]:
    """解析成员自愿提交的首脑竞选提案；无/坏标记不影响原回答。"""
    text = str(final or "")
    match = _CAPTAIN_MARK_RE.search(text)
    if not match:
        return text, None
    try:
        bid, json_end = json.JSONDecoder().raw_decode(text, match.start(1))
        frame_end = json_end
        while frame_end < len(text) and text[frame_end].isspace():
            frame_end += 1
        if not text.startswith("<<END>>", frame_end):
            return text, None
        frame_end += len("<<END>>")
    except (TypeError, ValueError):
        return text, None
    if not isinstance(bid, dict):
        return text, None
    clean = (text[:match.start()] + text[frame_end:]).strip()
    return clean, bid


def _captain_candidates(direction: str = "") -> list[str]:
    """所有当前可执行 harness 都可竞选首脑；没有固定 Auto/CC/Kimi。

    """
    valid = _harness_keys()
    try:
        runnable = set(_aigc_ready().get("runnable_clis", []))
    except Exception:
        runnable = set(valid)
    # 候选顺序=快家优先(用户 2026-08-29 loop 揪出:"吃什么"简单题第一个候选是 auto(最重最慢)
    # →solo 直答也要等它 98s)。第一个候选最先竞选·简单题它 solo 直答就结束→顺序直接决定简单题
    # 响应速度。codex/cc/hermes 等快家排前面·auto/omnigent 强但慢排后(复杂编排时它们仍会被选为
    # 首脑·_bid_strength 按方案完整度选·不受此顺序影响·顺序只决定"谁先竞选"即简单题谁先抢答)。
    # 候选顺序按【真实 cost_class】分层(2026-09-01 简单题3分钟不收敛根修):
    # 旧序把 heavy 的 codex 排第一·简单题它 solo 抢答·冷启动+深推理 3min 不收敛。
    # standard/light 先抢答(秒级)·heavy(codex/auto/deepagents)排后;
    # 首脑竞选按 _bid_strength 方案完整度选·不受此顺序影响(复杂任务仍可选 codex)。
    # _pref_order 只是【已验证快家】的排序提示·不再当过滤器(用户 2026-09-01 "为啥体验不是20多家"
    # 根因:旧代码 base=[k for k in _pref_order ...] 把不在这10个名字里的新外部agent(cline/goose/
    # openclaw/pi/cowagent/deer-flow/genericagent/nanobot/omniagent/ironclaw)全结构性排除→它们进了
    # /harnesses(20家)却永远不能竞选首脑/solo直答→主路径体验只有10家)。修:候选池=【全部可执行家】,
    # _pref_order 内的按其顺序打头(已知快·排前抢答),池外的新家按 cost_class 排在后面(仍能参选)。
    _pref_order = ["cc", "hermes", "jcode", "deepseek", "penguin", "prime", "codex", "deepagents", "omnigent"]
    try:
        _am_cc = {k: str(v.get("cost_class") or "standard") for k, v in _autonomy_matrix().items()}
    except Exception:
        _am_cc = {}
    # 全部可执行家都入候选(不再被 _pref_order 过滤掉)
    all_runnable = [k for k in sorted(valid) if k in runnable]
    _pref_idx = {k: i for i, k in enumerate(_pref_order)}
    # 排序键:①light/standard 先于 heavy(简单题快家先抢答)②_pref_order 内的按其固定顺序打头·
    # 池外新家排在同档 _pref 家之后(用一个大 index 兜底)③名字兜底稳定
    def _cand_key(k: str):
        heavy = 1 if _am_cc.get(k) not in ("light", "standard") else 0
        pref = _pref_idx.get(k, len(_pref_order) + 1)
        return (heavy, pref, k)
    base = sorted(all_runnable, key=_cand_key)
    # 按首脑适任度(竞技场控制变量实验真数据)重排:证明首脑是关键变量(极差53分·auto当首脑仅36),
    # 让历史群体分高的家优先竞选/当首脑;无数据的家用中性分插中间(给机会积累数据·不歧视)。
    # 这是"按测试经验判断谁适合当首脑·默认选最强"的落地;过程不行再交接走已有 handoff_captain。
    try:
        import captain_aptitude as _apt
        # 方向实测优先(7b):给了 direction 就按该方向场景排名重排——这是 by_direction 的
        # 第一个生产消费点(此前实现完整但全仓零调用·账本只写不读空转)。没给退全局适任度。
        if direction:
            return _apt.by_direction(base, direction)
        return _apt.rank_candidates(base)
    except Exception:
        return base


def _appoint_runtime_captain(plan: dict | None = None,
                             candidate_harnesses: list[str] | None = None) -> str:
    """低延迟指定一个真实 Harness 持续负责协调；不再用 legion/planner 伪装首脑。"""
    source = plan if isinstance(plan, dict) else {}
    explicit = str(source.get("captain") or "").strip()
    valid = _harness_keys()
    if explicit in valid:
        return explicit
    direction = str(source.get("direction") or "")
    if not direction:
        directions = source.get("directions") if isinstance(source.get("directions"), dict) else {}
        direction = next((str(value) for value in directions.values() if str(value).strip()), "")
    ranked = _captain_candidates(direction)
    if candidate_harnesses:
        allowed = {str(key) for key in candidate_harnesses if str(key)}
        ranked = [key for key in ranked if key in allowed]
    return ranked[0] if ranked else ""


_TASK_GRAPH_INPUT_CONTEXT = (
    "任务图字段契约：hierarchy 是旧式 child→parent 依赖映射，值是 "
    "tasks 中其他任务ID的单个字符串，不是职责说明或列表；无父依赖的节点省略，"
    "整个映射可为 {}。任务对象的 depends_on 是已有任务ID列表；职责写进任务文本。"
    "任务ID与任务正文不是执行者绑定，名字中写某个成员也不会指定它。"
    "选择执行者写 assigned_harness；需要固定已选成员时写 assignment_locked=true，"
    "未锁定的自动规划建议可能按现有候选条件重绑。"
    "capability_requirements 可表达本任务必需的运行能力；能力声明不是本次已执行的证据。"
    "具体任务、依赖关系和分工仍由你根据当前目标决定。\n"
)


def _selection_resource_context(plan: dict | None, user_id: str) -> dict:
    """One observational index for native planning, captain bids and execution."""
    try:
        import harness_view
        return harness_view.selection_context(user_id=user_id, plan=plan)
    except Exception as exc:
        return {"candidate_keys_complete": False, "details_complete": False,
                "observations": ["selection_resource_index_unavailable:" + type(exc).__name__],
                "meaning": "目录暂不可读不代表未安装；沿既有运行目录检查，调用仍需原授权。"}


def _request_budget_pause(provider_results, result: dict | None = None) -> dict | None:
    """Observe a private caller-owned budget; never infer the cause of a native failure.

    The callback is trusted runtime input, not a plan/answer or a cancellation.
    Already successful/in-flight work remains successful. Keep the original
    failure and the independent observation in existing durable native_facts.
    """
    from copy import deepcopy
    from error_envelope import requires_request_budget_pause, request_budget_hold
    if provider_results is None:
        return None
    message_absent = bool(result is not None and _native_terminal_message_absent(result))
    if result is not None:
        if (result.get("cancelled") or result.get("status") == "cancelled"
                or (not result.get("status") and result.get("ok") is True)
                or (result.get("status") in ("ok", "done", "completed", "success")
                    and result.get("ok") is not False and not message_absent)
                or requires_request_budget_pause(result)):
            return None
    observed = next((row for row in provider_results()
                     if requires_request_budget_pause(row)), None)
    if observed is None:
        return None
    envelope = request_budget_hold([observed])
    paused = dict(result or {})
    facts = dict(paused.get("native_facts") or {})
    if result is not None and not result.get("blocked_before_native"):
        facts.setdefault("execution_observation" if message_absent else "execution_failure", deepcopy({
            name: result[name] for name in ("status", "error", "fail_kind", "error_envelope")
            if name in result}))
    facts["runtime_budget_observation"] = {
        name: deepcopy(observed[name])
        for name in ("status", "source", "error_envelope") if name in observed}
    error = str(paused.get("error") or "")
    paused.update(
        status="error", ok=False, fail_kind="runtime_budget", error_envelope=envelope,
        error=(error + "\n原请求预算另已耗尽，暂停后续调用；原执行记录保留，未作因果归因。"
               if result is not None else envelope["message"]),
        native_facts=facts)
    if result is None:
        paused.update(final="", blocked_before_native=True, execution_calls=0)
    return paused


def elect_captain(question: str, plan: dict | None = None,
                  cb: Optional[Callable[[dict], None]] = None,
                  qhash: str = "adhoc", user_id: str = "default",
                  conversation_id: str = "",
                  _provider_results: Optional[Callable[[], list[dict]]] = None) -> dict:
    """让平等成员基于同一现场自主竞选首脑，不固定某一家、不用父层关键词路由。

    用户显式 captain 时直接尊重；否则候选各自返回结构化提案。选择器只比较提案的客观
    完整度（是否愿意、是否给出可执行 tasks），不评判业务答案、不融合答案。首位完整提案
    当选；后续可用 handoff_captain 换届。候选调用走 run_one，因此模型、权限、记忆、失败
    回流与普通成员完全一致。
    """
    from copy import deepcopy
    # 与 dispatch 同一请求隔离语义；plan 是任务内容，不是调用者身份。
    if not qhash or qhash == "adhoc":
        qhash = f"adhoc_{uuid.uuid4().hex[:12]}"
    user_id = str(user_id or "").strip()
    if not user_id or user_id == "default":
        user_id = f"anon:{qhash}"
    # 初始化、绑定和原生调用必须使用同一个有效会话，不从旧项目借身份。
    conversation_id = str(conversation_id or qhash)
    plan = dict(plan or {})
    _election_results = []
    bids = []

    def _provider_facts():
        # 私有运行事实，不塞进竞选提示或公开 bids；后续候选与备用规划共用。
        inherited = list(_provider_results() if _provider_results is not None else [])
        seen = {id(row) for row in inherited}
        return inherited + [row for row in _election_results if id(row) not in seen]

    def _cancelled_election():
        # 不再触发延迟的历史读取；本次已返回的原生结果/游标仍原样保留。
        return {"ok": False, "cancelled": True, "error": "cancelled",
                "bids": bids, "provider_results": list(_election_results)}

    def _candidate_cancelled(key):
        return (_request_cancelled(qhash, harness=key)
                or is_cancelled(f"{qhash}_captain_{key}", key))

    if is_cancelled(qhash):
        return _cancelled_election()

    budget_pause = _request_budget_pause(_provider_facts)
    if budget_pause:
        return {**budget_pause, "bids": bids, "provider_results": _provider_facts()}
    valid = _harness_keys()
    policy = plan.get("team_policy") if isinstance(plan.get("team_policy"), dict) else {}
    scope = policy.get("candidate_harnesses")
    if isinstance(scope, (list, tuple, set)):
        valid &= {str(key) for key in scope if str(key)}
    explicit = str(plan.get("captain") or "").strip()
    try:
        import captain_preferences as _cp
        _pref = _cp.load(user_id)
    except Exception:
        _pref = {"mode": "auto", "preferred_captain": "", "candidates": []}
    if not explicit and _pref.get("mode") == "fixed":
        explicit = str(_pref.get("preferred_captain") or "")
    # 候选顺序按本单方向实测重排(7b·2026-09-02):direction 优先级=规划器 directions
    # (captain 视角)→plan.direction→无(全局 rank)。第一个候选最先竞选·方向对的人排前
    # →简单题 solo 直答更快更准。
    _elect_dir = str((plan.get("directions") or {}).get(str(plan.get("captain") or ""))
                     or plan.get("direction") or "")
    candidates = [explicit] if explicit in valid else _captain_candidates(_elect_dir)
    # Election, execution and fallback share the caller's candidate scope.
    # An empty explicit scope must not become an unrestricted audition.
    candidates = [key for key in candidates if key in valid]
    if not explicit and _pref.get("candidates"):
        allowed_pref = set(_pref["candidates"])
        candidates = [key for key in candidates if key in allowed_pref]
    # 用户/测试可限制竞选范围；默认仍是全部真实 runnable 家。限制只决定谁参加本次竞选，
    # 不替候选判断谁该赢。用于成本可控的首脑擂台与显式小队。
    requested = plan.get("captain_candidates") or os.environ.get("LEGION_CAPTAIN_CANDIDATES", "") or []
    if isinstance(requested, str):
        requested = [x.strip() for x in requested.split(",") if x.strip()]
    if not explicit and requested:
        allow = {str(x) for x in requested}
        candidates = [k for k in candidates if k in allow]
    # 首脑竞选是控制面，不是正式执行：默认只让实测排序最前的 3 家短竞选。
    # 旧版串行遍历全部 runnable（真测 12 家仍未结束、830s 还在竞选），用户在任何实际工作
    # 开始前就支付候选数×完整 Harness 墙钟。显式候选/固定首脑仍完整尊重；环境变量可调大做擂台。
    if not explicit and not requested:
        _candidate_cap = max(1, int(os.environ.get("LEGION_CAPTAIN_CANDIDATE_CAP", "3")))
        candidates = candidates[:_candidate_cap]
    if not candidates:
        return {"ok": False, "error": "没有可运行的首脑候选", "bids": [],
                "provider_results": _provider_facts()}
    profiles = {p.get("key"): p for p in __import__("harness_profiles").get_profiles()}
    # 竞选现场瘦身(用户 2026-08-29 时效优化:竞选prompt太重·codex单跑9.8s但竞选28s)。
    # roster 只给 key+一句 best_for(供判断编排时选谁)·不塞 strengths数组/historical_results
    # (那些对"这题要不要编排"的第一轮判断是无用重输入·徒增token拖慢首答)。
    # P0-B 阶段3(2026-09-02):roster 每家补 readiness 三态 + 本单方向的实测排名摘要——
    # 竞选模型看得见"谁真跑过这个方向、谁只是画像声明",重要交付自己选实测过的家
    # (context not control·摆事实不加门)。瘦身原则不变:每家仍是一行,只多两个短字段。
    try:
        import harness_performance as _hp
        _hp_rows = _hp.table()["rows"]
    except Exception:
        _hp_rows = []
    try:
        import captain_aptitude as _apt
        _ranked = [k for k in _apt.by_direction(list(candidates), _elect_dir) if k in set(candidates)]
    except Exception:
        _ranked = list(candidates)
    _rank_txt = " > ".join(_ranked[:5]) if _ranked and _elect_dir else ""
    def _readiness_of(k: str) -> str:
        mine = [r for r in _hp_rows if r.get("harness") == k]
        good_any = any(r.get("good", 0) > 0 for r in mine)
        n_any = sum(int(r.get("n") or 0) for r in mine)
        lb_max = max((float(r.get("success_lb") or 0) for r in mine), default=0.0)
        if n_any >= 3 and lb_max > 0 and good_any:
            return "experienced"
        return "verified" if good_any else "declared"
    roster = [{"key": k, "best_for": (profiles.get(k, {}).get("best_for", "") or "")[:60],
               "readiness": _readiness_of(k)}
              for k in candidates]
    shared = {"goal": question, "members": roster,
              "selection_resources": _selection_resource_context(plan, user_id)}
    shared["conversation_context"] = str(plan.get("conversation_context") or "")
    shared["project_context"] = plan.get("project_context") or {}
    _experience = plan.get("experience_intent") if isinstance(plan.get("experience_intent"), dict) else {}
    _high_value = bool(str(plan.get("quality_profile") or "").lower() == "frontier" or
                       str(_experience.get("ambition") or "").lower() in {"world_class", "frontier"})
    if _high_value:
        shared.update({
            "north_star": plan.get("north_star") or {},
            "critical_dimensions": plan.get("critical_dimensions") or
                                   _experience.get("experience_dimensions") or [],
            "claim_ceiling": plan.get("claim_ceiling") or "PARITY_OR_INCONCLUSIVE",
            "golden_task": plan.get("golden_task") or {},
        })
    if _rank_txt:
        shared["measured_rank_" + (_elect_dir or "global")] = _rank_txt
    if plan.get("attachments"): shared["attachments"] = plan["attachments"]
    if plan.get("acceptance"): shared["acceptance"] = plan["acceptance"]
    if plan.get("user_profile"): shared["user_profile"] = str(plan["user_profile"])[:200]
    _election_started = time.monotonic()
    _explicit_election_timeout = (bool(plan.get("captain_election_timeout_s"))
                                  or "LEGION_CAPTAIN_ELECTION_TIMEOUT_S" in os.environ)
    _election_budget = max(5.0, float(plan.get("captain_election_timeout_s")
                                      or os.environ.get("LEGION_CAPTAIN_ELECTION_TIMEOUT_S", "30")))
    # 简单/日常题快速直答(用户 2026-08-29 loop 揪出:"吃什么"卡在多家竞选首脑·空烧网关)。
    # 让第一个候选先判断:这题是否根本【不需要首脑编排】(开放式日常/简单问答/单点请求)?
    # 是→它直接把答案写出来·父层立刻采纳作单家交付·不再遍历其他候选(context not control:
    # 让最快看到现场的成员自己判断要不要编排·不是父层写关键词判断"这是简单题")。
    # 任务复杂度现场事实(2026-09-02 roundC/D 复盘⑤⑥根修·context not control):wallpaper 三步(生成+设置+截图)被第一家误判 solo 干 485s;fractal 单家能干被 orchestrate 816s。两边都是判断者对步骤数/单步耗时没概念→把多步识别+耗时量级摆到眼前(不加规则不强制·判断权仍在模型):云真机 GUI 单步真实耗时 30-120s·多步预算按步骤数x单步估。
    _step_facts = ("【任务复杂度现场事实(自判参考·非规则)】任务文本含多个连续动作(如生成X并设置Y再截图/"
                  "安装A用它做B/先建再改再读)=多步任务:solo 也能做,选 solo 前掂量能否完成全部步骤;"
                  "单点请求(一问一答/翻译/改一段)=solo 最快。云真机 GUI 每步真实耗时 30-120s,"
                  "多步任务预算按步骤数×单步估,别把多步任务当一问一答秒回。\n")
    _audition_contract = ""
    if _high_value:
        _audition_contract = (
            "【高价值主创试镜】这不是比谁拆的任务多。你的 captain_bid 必须额外包含 captain_thesis："
            "{goal_restatement,critical_dimensions,largest_failure_risk,smallest_risky_sample,"
            "stop_expansion_condition,personal_ownership,claim_ceiling,evidence_plan}。"
            "先提出能证伪方向的最小样片与停止扩展条件，再设计后续 DAG；不得从研究直接铺满完整产品。"
            "若你不具备最高关键维度所需能力，应主动建议更合适 owner。\n")
    prompt = (_step_facts + _audition_contract +
              "你正在竞选本单军团首脑。先读真实现场判断:这题需要多成员协作/多轮检索/代码执行/"
              "复杂编排吗?\n"
              "① 如果【不需要】(开放式日常题、简单问答、单点请求,如'吃什么'/'讲个笑话'/'解释X')——"
              "别选首脑,直接把【完整答案】写给用户(就像你在直接回答),最后追加一次:"
              "<<LEGION_CAPTAIN>>{\"willing\":false,\"solo\":true,\"reason\":\"简单题直接答\"}<<END>>。\n"
              "② 如果【需要】编排——提出可执行编排,最后追加一次:"
              "<<LEGION_CAPTAIN>>{\"willing\":true,\"reason\":\"...\",\"captain_thesis\":{},"
              "\"plan\":{\"tasks\":{\"成员key\":\"任务\"},\"hierarchy\":{},\"directions\":{}}}<<END>>。\n"
              + _TASK_GRAPH_INPUT_CONTEXT
              + "③ 你不适合领导但题需编排→willing=false 且不加 solo(让别家竞选)。"
              "现场 members 带 readiness(declared=画像声明未实测/verified=实测成功过/"
              "experienced=样本充分可信)与本方向实测排名(measured_rank_*)——重要交付优先选"
              "实测过的家,探索性任务可给 declared 家机会(事实参考·非硬规则)。现场："
              + json.dumps(shared, ensure_ascii=False))
    for key in candidates:
        if is_cancelled(qhash):
            return _cancelled_election()
        budget_pause = _request_budget_pause(_provider_facts)
        if budget_pause:
            return {**budget_pause, "bids": bids, "provider_results": _provider_facts()}
        remaining = _election_budget - (time.monotonic() - _election_started)
        if remaining <= 0:
            if cb:
                cb({"agent": "legion", "t": "captain_election_budget", "status": "timeout",
                    "elapsed_s": round(time.monotonic() - _election_started, 1),
                    "x": f"首脑竞选已达 {_election_budget:g}s 预算·用已收到的方案收敛"})
            break
        if cb:
            # 措辞如实(用户2026-08-30:简单题不该显示"评估首脑"·首脑非必须)。第一家其实是在
            # 读现场·简单题直接答/复杂题才提编排。第一候选显示"正在理解并回答"(简单题它就是直接答);
            # 后续候选(仅复杂题才轮到)才是"评估是否接手编排"。
            _msg = ("正在理解你的问题并回答(简单题直接答·复杂才编排)" if key == candidates[0]
                    else "上一家判断需要编排·评估是否接手")
            cb({"agent": key, "t": "captain_bid", "status": "running", "x": _msg})
        # 竞选 cb 透传(2026-09-02 端到端揪出真根因):执行类题会被竞选家【直接干完】
        # (prompt 带全任务+云真机现场·判断 solo 后返回)——旧链不传 cb→run_one 内产物事件
        # (artifact_ready)和过程事件全丢(真跑实锤:trending_e2e.json 真落盘但用户 arts=[])。
        # 过滤版透传:artifact/工具过程事件放行(过程透明),拦 result(竞选答卷不冒充交付·防双卡)。
        def _captain_cb(ev, _key=key):
            if not cb:
                return
            if ev.get("t") == "result":
                return
            try:
                cb(ev)
            except Exception:
                pass
        candidate_project = dict(plan.get("project_context") or {})
        candidate_conversation = conversation_id
        selection_error = None
        candidate_scope = {}
        candidate_attempt = None
        if candidate_project:
            # 竞选可沿用已核实的工程/原生会话，但它不是团队中已登记的业务任务。
            # 用外层请求身份查旧槽；不要把旧 packet.team_id 当成本轮活动请求。
            candidate_project["team_id"] = qhash
            candidate_project.setdefault("conversation_id", conversation_id)
            candidate_project.setdefault("user_id", user_id)
            if conversation_id and _native_scope_facts(plan).get("native_session_scope") != "request":
                try:
                    import sessions as _election_sessions
                    import task_memory as _election_memory
                    previous = _election_memory.load(f"{qhash}_captain_{key}", key)
                    if not isinstance(previous, dict):
                        raise ValueError("native continuation request storage is malformed")
                    checkpoint = previous.get("native_checkpoint") or {}
                    if not isinstance(checkpoint, dict):
                        raise ValueError("native continuation request checkpoint is malformed")
                    if checkpoint.get("session_id"):
                        # 与 run_one 的请求级优先级一致：checkpoint 和逻辑槽必须成对，
                        # 同 qhash 重入不能因后来完成的其他任务而写进另一条会话槽。
                        candidate_conversation = str(previous.get("native_conversation_id")
                                                     or conversation_id)
                        own_repair_request = (
                            candidate_conversation == f"{conversation_id}:contract-repair:{key}"
                            and previous.get("team_id") == f"{qhash}_captain_{key}"
                            and previous.get("key") == key)
                        if (candidate_conversation != conversation_id
                                and not candidate_conversation.startswith(conversation_id + ":task:")
                                and not own_repair_request):
                            raise ValueError("native continuation lane identity is unavailable")
                    else:
                        candidate_conversation = (_election_sessions.continuation_native_slot(
                            conversation_id, key, user_id, candidate_project) or conversation_id)
                except (ValueError, PermissionError) as exc:
                    selection_error = str(exc)
            # 完整原始现场仍在 shared 提示中；执行参数只声明项目现场，不能冒充
            # maker/commander 的 live mailbox 绑定，也不让 run_one 再次猜选旧槽。
            candidate_project = {k: v for k, v in candidate_project.items()
                                 if k not in {"team_id", "task_id", "conversation_id"}}
        if selection_error is None:
            try:
                import project_context as _candidate_projects
                import task_memory as _candidate_memory
                import team_state as _candidate_teams
                candidate_request = f"{qhash}_captain_{key}"
                packet = _candidate_projects.load(qhash)
                if not packet:
                    packet = _candidate_projects.initialize(
                        qhash, question, {**plan, "tasks": {}}, mode="orchestrate",
                        user_id=user_id, conversation_id=conversation_id)
                _candidate_projects.require_owner(packet, user_id, conversation_id)
                nodes = {row["task_id"]: row for row in packet.get("tasks", [])
                         if isinstance(row, dict) and row.get("task_id")}
                remembered = (_candidate_memory.load(candidate_request, key).get("candidate_scope") or {})
                task_id = str(remembered.get("task_id") or "")
                if (remembered.get("team_id") != qhash or remembered.get("request_id") != candidate_request
                        or remembered.get("harness") != key
                        or (nodes.get(task_id) or {}).get("assigned_harness") != key):
                    task_id = f"candidate_{key}_{uuid.uuid4().hex[:12]}"
                    while task_id in nodes:
                        task_id = f"candidate_{key}_{uuid.uuid4().hex[:12]}"
                candidate_scope = {"team_id": qhash, "task_id": task_id, "harness": key,
                                   "request_id": candidate_request,
                                   "native_conversation_id": candidate_conversation}
                node = {"task": question, "assigned_harness": key, "depends_on": []}
                packet = _candidate_projects.record_task_bindings(
                    qhash, {task_id: node}, user_id=user_id, conversation_id=conversation_id)
                candidate_project = _candidate_projects.snapshot(qhash, task_id, packet=packet)
                _candidate_memory.update(candidate_request, key, candidate_scope=candidate_scope)
                _candidate_teams.ensure(
                    qhash, question, {task_id: node}, {task_id: []},
                    user_id=user_id, conversation_id=conversation_id, registered=valid)
                candidate_attempt = _candidate_teams.begin_attempt(
                    qhash, task_id, owner=f"captain-candidate:{os.getpid()}")
            except Exception as exc:
                selection_error = "candidate project binding unavailable: " + str(exc)
        if selection_error is not None:
            result = {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                      "error": selection_error, "attempts": 0,
                      "fail_kind": "native_session_unavailable",
                      "error_envelope": {"category": "native_session_unavailable",
                                         "scope": "request", "retryable": False,
                                         "phase": "native_resume_selection",
                                         "message": selection_error}}
        else:
            try:
                result = run_one(key, prompt, qhash=candidate_scope["request_id"], user_id=user_id,
                                 conversation_id=candidate_conversation,
                                 task_facts={**_native_scope_facts(plan, request_id=qhash),
                                             "team_id": qhash, "task_id": candidate_scope["task_id"],
                                             "attempt_id": candidate_attempt,
                                             "project_context": candidate_project},
                                 runtime_brief=_build_runtime_brief(question, plan, user_id=user_id),
                                 # Keep the existing native context and caller budget.
                                 attempt_timeout=(min(remaining, 12.0)
                                                  if _explicit_election_timeout else None),
                                 execution_depth="quick",
                                 cancel_check=lambda _key=key, _task=candidate_scope["task_id"]: (
                                     _candidate_cancelled(_key) or _request_cancelled(qhash, _task, _key)),
                                 cb=_captain_cb, _provider_results=_provider_facts)
            except BaseException as exc:
                interrupted = {"status": "cancelled" if _candidate_cancelled(key) else "error",
                               "final": "", "error": f"candidate_call:{type(exc).__name__}"}
                _candidate_teams.finish_attempt(
                    qhash, candidate_scope["task_id"], candidate_attempt, interrupted)
                _candidate_projects.record_result(
                    qhash, candidate_scope["task_id"], interrupted, assigned_harness=key,
                    continuation={"action": "captain_candidate_interrupted", **candidate_scope})
                raise
            result = {**result, "task_id": candidate_scope["task_id"], "assigned_harness": key}
            result["native_facts"] = {
                **dict(result.get("native_facts") or {}), "candidate_scope": dict(candidate_scope)}
            _candidate_teams.finish_attempt(qhash, candidate_scope["task_id"], candidate_attempt, result)
            _candidate_projects.record_result(
                qhash, candidate_scope["task_id"], result, assigned_harness=key,
                continuation={"action": "captain_candidate_execution", **candidate_scope})
        _election_results.append(result)
        if is_cancelled(qhash):
            return _cancelled_election()
        from error_envelope import requires_request_budget_pause
        if requires_request_budget_pause(result):
            return {**result, "ok": False, "bids": bids, "provider_results": _provider_facts()}
        clean, bid = _parse_captain_bid(result.get("final") or "")
        if bid is None:
            req = result.get("orchestration_request") or {}
            bid = req.get("captain_bid") if isinstance(req.get("captain_bid"), dict) else None
        # ★简单题快速直答(fast-path):候选判断"这题不需要编排·我直接答"(solo=true)且真给了答案·
        # 立刻采纳作单家交付·不再遍历其他候选(治"吃什么"卡在多家竞选空烧网关)。clean=剥掉标记的答案。
        if ((bid or {}).get("solo") and result.get("status") == "done" and clean.strip()
                and not _candidate_cancelled(key)):
            if cb:
                cb({"agent": key, "t": "captain_selected", "status": "solo",
                    "captain": key, "x": f"{key} 判断这是简单题·直接答(无需首脑编排)"})
            if is_cancelled(qhash):
                return _cancelled_election()
            if _candidate_cancelled(key):
                continue
            return {"ok": True, "captain": key, "solo": True,
                    "plan": {**plan, "captain": key, "tasks": {
                                candidate_scope["task_id"]: {"task": question, "assigned_harness": key,
                                                              "depends_on": []}},
                             "task_assignments": {candidate_scope["task_id"]: key},
                             "_solo_answer": clean},
                    "original_result": deepcopy(result), "candidate_scope": dict(candidate_scope),
                    "reason": str((bid or {}).get("reason") or "简单题直接答"),
                    "bids": bids + [{"key": key, "solo": True}], "via": "captain_solo_fastpath"}
        _bid_plan = (bid or {}).get("plan") if isinstance((bid or {}).get("plan"), dict) else {}
        _thesis = (bid or {}).get("captain_thesis") if isinstance((bid or {}).get("captain_thesis"), dict) else {}
        if not _thesis and isinstance(_bid_plan.get("captain_thesis"), dict):
            _thesis = dict(_bid_plan["captain_thesis"])
        row = {"key": key, "status": result.get("status"), "willing": bool((bid or {}).get("willing")),
               "reason": str((bid or {}).get("reason") or clean)[:1000],
               "plan": _bid_plan, "captain_thesis": _thesis}
        bids.append(row)
        if explicit and row["status"] == "done":
            row["willing"] = True
        if cb:
            cb({"agent": key, "t": "captain_bid", "status": "done", "willing": row["willing"],
                "x": row["reason"][:240]})
        # 普通任务仍在首个可执行方案收敛；高价值创作至少比较两份主创论证，避免“先到且拆得多”
        # 自动成为明星。仍受候选上限和总竞选预算约束，不扩大成无界评测平台。
        if (not requested and row["status"] == "done" and row["willing"] and row["plan"].get("tasks")
                and not _candidate_cancelled(key)
                and (not _high_value or len([item for item in bids if item.get("willing")]) >= 2)):
            break
    if is_cancelled(qhash):
        return _cancelled_election()
    eligible = [b for b in bids if b["status"] == "done" and b["willing"] and (b["plan"].get("tasks"))]
    if not eligible:
        return {"ok": False, "error": "候选均未提交可执行首脑方案", "bids": bids,
                "provider_results": _provider_facts()}
    # 谁强谁当本单首脑：普通任务比较可执行结构；高价值创作先比较目标保真、关键风险、最小样片、
    # 停止扩展和证据诚实性这些结构事实，再参考现场战绩。平台不评业务答案，只检查候选是否给出了
    # 本题需要的完整产品论证；任务数量降为弱信号，避免奖励把弱方向拆得很大。
    def _bid_strength(row):
        p = row["plan"]
        tasks = p.get("tasks") or {}
        dirs = p.get("directions") or {}
        graph = p.get("hierarchy") or p.get("dependencies") or {}
        thesis = row.get("captain_thesis") if isinstance(row.get("captain_thesis"), dict) else {}
        thesis_fields = ("goal_restatement", "critical_dimensions", "largest_failure_risk",
                         "smallest_risky_sample", "stop_expansion_condition", "personal_ownership",
                         "claim_ceiling", "evidence_plan")
        thesis_completeness = sum(bool(thesis.get(field)) for field in thesis_fields)
        critical_count = len(thesis.get("critical_dimensions") or []) if isinstance(
            thesis.get("critical_dimensions"), list) else int(bool(thesis.get("critical_dimensions")))
        sample_first = int(bool(thesis.get("smallest_risky_sample") and
                                thesis.get("stop_expansion_condition")))
        evidence_honesty = int(bool(thesis.get("claim_ceiling") and thesis.get("evidence_plan")))
        ev = profiles.get(row["key"], {}).get("eval", {}) or {}
        # 场景实测 tie-break(2026-09-02 P0-B·7c 收口):方案完整度打平时,【本单方向】的
        # 实测质量(harness_performance·运行时+盲评都在写)强的先赢——这是今天的真战绩。
        # 依据:样本 n<3 时返回中性 0.5(不拿偶然压人);纯排序不硬过滤——实测差不等于禁选,
        # 只输给同分且实测更强的。
        scene_quality = _scene_measured_quality(row["key"], plan)
        # 静态 eval 声明(历史 benchmark 快照·n>=10 才算)降为场景实测【之后】的兜底:
        # 静态分是过去的全局印象(codex 旧benchmark 0.0625 不代表它今天在"深度推理
        # orchestrate"的实力)——今天该方向的实测战绩比过去快照可信,排序在前。
        n = int(ev.get("n") or 0)
        accuracy = float(ev.get("accuracy") or 0) if n >= 10 else 0.0
        elapsed = float(ev.get("avg_elapsed") or 1e9) if n >= 10 else 1e9
        if _high_value:
            return (thesis_completeness, critical_count, sample_first, evidence_honesty,
                    scene_quality, accuracy, len(graph), len(dirs), min(len(tasks), 4), -elapsed)
        return (len(tasks), len(dirs), len(graph), scene_quality, accuracy, -elapsed,
                len(row.get("reason") or ""))
    # 只在已有答卷中选择，不重试候选；排序期间的取消也不能被当选覆盖。
    for winner in sorted(eligible, key=_bid_strength, reverse=True):
        if is_cancelled(qhash):
            return _cancelled_election()
        if _candidate_cancelled(winner["key"]):
            continue
        chosen_plan = dict(plan)
        chosen_plan.update(winner["plan"])
        chosen_plan["captain"] = winner["key"]
        if winner.get("captain_thesis"):
            chosen_plan["captain_thesis"] = dict(winner["captain_thesis"])
        chosen_plan["captain_bids"] = bids
        if is_cancelled(qhash):
            return _cancelled_election()
        if _candidate_cancelled(winner["key"]):
            continue
        return {"ok": True, "captain": winner["key"], "plan": chosen_plan,
                "reason": winner["reason"], "bids": bids, "via": "harness_captain_election"}
    if is_cancelled(qhash):
        return _cancelled_election()
    return {"ok": False, "error": "可执行候选均已取消", "bids": bids,
            "provider_results": _provider_facts()}


# ── 自然语言派活:兼容旧规划器；产品默认入口由 harness 动态竞选首脑 ───────────
# 既有显式重规划 helper 的兼容模型偏好；不是高目标任务的默认必经阶段。
_DEEP_PLANNER_MODELS = {"MiniMax-M3"}


def _deep_replan(prompt: str, cm: str, request_context: dict | None = None) -> dict:
    """保留既有显式重规划接口及运行保护；默认规划不按 ambition 自动调用。"""
    import planner_runtime as _planner_runtime
    import time as _clock
    _team_id = str((request_context if isinstance(request_context, dict) else {}).get("team_id") or "")
    _cancelled = lambda: bool(_team_id and is_cancelled(_team_id))
    attempts = []

    def finish(value):
        if attempts and value.get("error"):
            attempts[-1]["error"] = value["error"]
        return {**value, "planner_attempts": attempts}

    if _cancelled():
        return finish({"ok": False, "error": "planner_cancelled", "cancelled": True})
    try:
        # 保留原深规划模型偏好，但授权必须来自当前 registry，不能写死 appid。
        import model_registry
        pick = select_usable_model("programming", exclude_models={
            c["model"] for c in (model_registry._load_models().get("programming") or {}).get("candidates", [])
            if c.get("model") and c["model"] not in _DEEP_PLANNER_MODELS})
        if pick.get("model") not in _DEEP_PLANNER_MODELS or not pick.get("appid"):
            return finish({"ok": False, "error": "deep_replan_model_config_unavailable"})
        env = _gateway_env()
        env.update(LEGION_PLANNER_MODEL=pick["model"], LEGION_PLANNER_APPID=pick["appid"])
        started = _clock.monotonic()
        attempt = {"attempt": 1, "scope": "deep_replan", "model": pick["model"],
                   "timeout_s": 85.0, "phase": "process_start", "error": ""}
        attempts.append(attempt)
        r = _planner_runtime.run_cli(
            [sys.executable, cm, "chat", "--timeout", "85", "--stream-idle"],
            input=prompt, env=env, cancel_check=_cancelled)
    except Exception as exc:
        if attempts:
            facts, _ = _planner_runtime.diagnostic(
                getattr(exc, "legion_stderr", getattr(exc, "stderr", "")), pick["appid"])
            attempts[-1].update(facts, elapsed_s=round(_clock.monotonic() - started, 4))
        return finish({"ok": False, "error": "planner_cancelled" if _cancelled() else
                       f"deep_replan_call:{type(exc).__name__}", "cancelled": _cancelled()})
    facts, code = _planner_runtime.diagnostic(r.stderr, pick["appid"])
    attempt.update(facts, elapsed_s=round(_clock.monotonic() - started, 4),
                   returncode=r.returncode)
    if getattr(r, "cancelled", False) or _cancelled():
        return finish({"ok": False, "error": "planner_cancelled", "cancelled": True})
    if r.returncode:
        return finish({"ok": False, "error": f"deep_replan_cli_exit:{r.returncode}" +
                       (":" + code if code else "")})
    body = _strip_reasoning(r.stdout or "")
    m = re.search(r"\{[\s\S]*\}", body)
    if not m:
        return finish({"ok": False, "error": "deep_replan_no_json"})
    try:
        parsed = _parse_planner_json(m.group(0))
    except Exception:
        return finish({"ok": False, "error": "deep_replan_parse"})
    return finish(parsed if parsed.get("ok") else {"ok": False, "error": "deep_replan_invalid"})


def _personal_responses_planner_enabled() -> bool:
    """Deployment mode only; the service owner is never a caller identity."""
    import personal_accounts
    return (personal_accounts.enabled()
            and os.environ.get("LEGION_CODEX_TRANSPORT", "").strip().lower() == "responses")


def _planner_identity_valid(user_id, conversation_id) -> bool:
    return all(isinstance(value, str) and bool(value.strip())
               for value in (user_id, conversation_id))


def _plan_with_native_codex(prompt: str, *, user_id: str, conversation_id: str,
                            cancel_check, provider_results=None) -> dict:
    """Use the existing runner once, with a fresh, read-only native request.

    The original project is already in the prompt. It is intentionally NOT an
    execution context, grant, resume cursor or writable planning directory.
    """
    request_id = "planner_" + uuid.uuid4().hex
    facts = {"planner_transport": "native_codex", "planner_request_id": request_id,
             "planner_attempts": []}
    # Native adapters scrub their actual binding. Also scrub request-local
    # provider holds (which can stop before the adapter) and nested diagnostics.
    try:
        binding = _explicit_provider_binding("codex") or {}
    except (ImportError, ValueError):
        binding = {}
    secrets = [str(binding["api_key"])] if binding.get("api_key") else []
    private_fields = {
        "api_key", "apikey", "appid", "authorization", "proxy_authorization",
        "access_token", "refresh_token", "token", "password", "secret", "credentials",
        "cookie", "set_cookie", "headers", "env", "argv", "command", "prompt",
        "stdout", "stderr", "body", "response_body",
    }

    def diagnostic(value, *, content=False):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            if content:
                return value
            value = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"']+",
                           r"\1 [REDACTED]", value)
            value = re.sub(
                r"""(?ix)(\b(?:api[_-]?key|appid|access[_-]?token|refresh[_-]?token|
                    password|secret|authorization)\b["']?\s*[:=]\s*)[^\s,;&"']+""",
                r"\1[REDACTED]", value)
            return value
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if not content and
                          str(key).lower().replace("-", "_") in private_fields
                          else diagnostic(item, content=content)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [diagnostic(item, content=content) for item in value]
        return value

    def cancelled():
        return {**facts, "ok": False, "status": "cancelled",
                "error": "planner_cancelled", "cancelled": True}

    def failed(value):
        if cancel_check() or value.get("cancelled"):
            return cancelled()
        paused = _request_budget_pause(provider_results, value)
        return diagnostic(paused) if paused else value

    if cancel_check():
        return cancelled()
    budget_pause = _request_budget_pause(provider_results)
    if budget_pause:
        return {**facts, **diagnostic(budget_pause)}
    started = time.monotonic()
    try:
        result = _run_one_with_gateway(
            "codex", prompt, qhash=request_id, user_id=user_id,
            conversation_id=conversation_id, cancel_check=cancel_check,
            task_facts={"native_session_scope": "request",
                        "native_session_request_id": request_id, "native_read_only": True},
            _provider_results=provider_results)
    except Exception as exc:
        if cancel_check():
            return cancelled()
        # Exception text may contain argv, a private prompt or credentials.
        return failed({**facts, "ok": False, "status": "error", "execution_calls": None,
                       "error": "planner_call:" + type(exc).__name__})
    if not isinstance(result, dict):
        return failed({**facts, "ok": False, "status": "error", "error": "planner_native_no_result"})
    for name in ("status", "error_envelope", "native_facts", "blocked_before_native"):
        if name in result:
            facts[name] = diagnostic(result[name])
    facts["execution_calls"] = result.get("execution_calls", result.get("attempts", 0))
    facts["planner_attempts"] = [{
        "attempt": 1, "scope": "initial_plan", "transport": "native_codex",
        "request_id": request_id, "status": facts.get("status", "error"),
        "model": diagnostic(str(result.get("model_used") or result.get("model") or "")),
        "elapsed_s": round(time.monotonic() - started, 4),
        "native_attempts": result.get("attempts", 0),
    }] if facts["execution_calls"] else []
    if isinstance(result.get("usage"), dict):
        facts["planner_usage"] = diagnostic(result["usage"])
    if cancel_check() or result.get("cancelled") or result.get("status") == "cancelled":
        return cancelled()
    from error_envelope import requires_execution_pause
    native = result.get("native_facts") or {}
    if (result.get("status") not in ("ok", "done", "success") or result.get("error")
            or requires_execution_pause(result) or native.get("failed") or native.get("interrupted")):
        return failed({**facts, "ok": False,
                       "error": diagnostic(str(result.get("error") or "planner_native_failed"))})
    match = re.search(r"\{[\s\S]*\}", str(result.get("final") or ""))
    if not match:
        return failed({**facts, "ok": False, "status": "error", "error": "planner_no_json"})
    try:
        parsed = _parse_planner_json(match.group(0))
    except Exception as exc:
        return failed({**facts, "ok": False, "status": "error", "error": "parse:" + type(exc).__name__})
    if cancel_check():
        return cancelled()
    # Reuse the same parser, including its real structural errors; no second
    # quality judge, helper fallback or outer three-model retry loop.
    if not parsed.get("ok"):
        facts["status"] = "error"  # Native completion remains in planner_attempts.
    return failed({**diagnostic(parsed, content=True), **facts})


def plan_request_with_model(question: str, attachments: list | None = None,
                            candidate_harnesses: list[str] | None = None,
                            request_context: dict | None = None,
                            cancel_check: "Callable[[], bool] | None" = None,
                            _provider_results: Optional[Callable[[], list[dict]]] = None, *,
                            user_id: str = "", conversation_id: str = "") -> dict:
    """总指挥模型读【用户目标+附件+军团成员+能力状态】后自主选择1-3家及任务。

    context not control:默认不再盲派5家/父进程关键词路由。模型自己判断这是媒体/代码/研究/简单问答,
    选择合适harness并派活。失败才退现有默认快家。
    """
    import subprocess, json as _j, time as _clock
    import planner_runtime as _planner_runtime
    _team_id = str((request_context if isinstance(request_context, dict) else {}).get("team_id") or "")
    _cancelled = lambda: bool((cancel_check and cancel_check())
                             or (_team_id and is_cancelled(_team_id)))
    _personal_native = _personal_responses_planner_enabled()
    if _personal_native:
        if _cancelled():
            return {"ok": False, "status": "cancelled", "error": "planner_cancelled",
                    "cancelled": True, "planner_transport": "native_codex", "planner_attempts": []}
        if not _planner_identity_valid(user_id, conversation_id):
            return {"ok": False, "status": "blocked", "error": "planner_identity_required",
                    "planner_transport": "native_codex", "planner_attempts": [],
                    "blocked_before_native": True, "execution_calls": 0,
                    "error_envelope": {"category": "authorization_required", "scope": "request",
                                       "retryable": False, "code": "planner_identity_required",
                                       "message": "辅助规划缺少已鉴权的用户和会话身份；未调用模型"}}
    if _cancelled():
        return {"ok": False, "status": "cancelled", "error": "planner_cancelled",
                "cancelled": True, "planner_attempts": []}
    budget_pause = _request_budget_pause(_provider_results)
    if budget_pause:
        return {**budget_pause, "planner_attempts": [],
                "planner_transport": "native_codex" if _personal_native else "cli"}
    resource_plan = dict(request_context or {})
    if candidate_harnesses is not None:
        resource_plan["team_policy"] = {
            **dict(resource_plan.get("team_policy") or {}),
            "candidate_harnesses": list(candidate_harnesses)}
    resources = _selection_resource_context(resource_plan, user_id)
    resource_members = {row["key"]: row for row in resources.get("members", [])}
    try:
        import harness_profiles
        # cost_class(light/standard/heavy)作为【响应速度画像】给总指挥(context not control):
        # heavy 家(codex/auto/deepagents…冷启动+推理慢·实测单任务>5min)适合复杂交付;
        # light/standard 适合交互/简单任务。总指挥据此自己判断该给谁——父层不写死"慢家不用"路由。
        _cc_map = {k: v.get("cost_class") for k, v in _autonomy_matrix().items()}
        _speed = lambda k: {"light": "快", "standard": "中", "heavy": "慢(冷启动+深推理·适合复杂交付非交互)"}.get(str(_cc_map.get(k) or "standard"), "中")
        pool = _harness_keys()
        if candidate_harnesses:
            requested_pool = {str(key) for key in candidate_harnesses if str(key)}
            pool &= requested_pool
        prof_by_key = {p["key"]: p for p in harness_profiles.get_profiles()}
        try:
            import external_agent_registry as _external_registry
            external_by_key = {str(item.get("key") or ""): item
                               for item in _external_registry.load_catalog()}
        except Exception:
            external_by_key = {}
        # 覆盖【编排池全部成员】——不只有内置 profile 的家。发现并探活通过的外部 agent(cline/goose/
        # openclaw…)已进池但没详细 profile,旧代码把它们过滤掉→总指挥永远看不见→"发现agent进编排"
        # 最后一公里断掉(军团 dogfood 揪出链路)。没 profile 的用发现信息兜底,让总指挥知道有它可选。
        profiles = []
        try:
            import harness_performance as _perf
        except Exception:
            _perf = None
        for k in sorted(pool):
            p = prof_by_key.get(k) or {}
            row = {"key": k, "best_for": p.get("best_for") or "外部接入 agent(发现并探活通过)",
                   "strengths": p.get("strengths") or ["外部成员·可派单"], "speed": _speed(k)}
            if (external_by_key.get(k) or {}).get("requires_existing_workspace"):
                row["requires_existing_workspace"] = True
            if _perf is not None:
                advantages = _perf.strengths(k)
                if advantages:
                    row["measured_advantages"] = [{field: item.get(field) for field in
                        ("scene", "mode", "n", "success_lb", "p95_s", "quality_avg", "artifacts")}
                        for item in advantages]
            # 运行资格三态(S3 根因④·2026-09-02):总指挥要能分清"声明的能力"和"实测验证过的能力"——
            # declared=只在 profile/catalog 里声明(best_for 是画像/README 语义·没真跑过);
            # verified=实测账本 n>=1(真跑过至少一次·有成功记录);experienced=n>=3 且
            # success_lb 达到正分(样本充分·优势画像可信)。规划器自己权衡:重要任务选
            # verified/experienced 的家,探索性任务可以给 declared 家机会。不加硬门。
            try:
                _rows = (_perf.table()["rows"] if _perf is not None else [])
            except Exception:
                _rows = []
            _mine = [r for r in _rows if r.get("harness") == k]
            _good_any = any(r.get("good", 0) > 0 for r in _mine)
            _n_any = sum(int(r.get("n") or 0) for r in _mine)
            _lb_max = max((float(r.get("success_lb") or 0) for r in _mine), default=0.0)
            if _n_any >= 3 and _lb_max > 0 and _good_any:
                row["readiness"] = "experienced"
            elif _good_any:
                row["readiness"] = "verified"
            else:
                row["readiness"] = "declared"
            # 最近运行事实单列给总指挥：声明/历史成功不代表此刻 runtime 可启动。只提供事实，
            # 不由父层硬过滤；总指挥可在探索任务仍试 unavailable 家，重要交付应自行权衡。
            facts = resource_members.get(k) or {}
            row["runtime_available"] = facts.get("runtime_available")
            row.update({field: facts[field] for field in ("runtime_reason", "model", "native")
                        if field in facts})
            profiles.append(row)
    except Exception:
        profiles = [{"key": k} for k in sorted(_harness_keys())]
    # 归一附件(前端传 dict·旧调用传字符串),规划器才能看到真实路径/类型判断该派谁
    _paths = [pp for pp in (_attachment_path(a) for a in (attachments or [])) if pp]
    try:
        import file_hub
        att = [{"path": p, "kind": file_hub.kind_of(p)} for p in _paths]
    except Exception:
        att = [{"path": p} for p in _paths]
    # 源码/发布包优先使用同一受版本管理实现，不误用个人目录的不兼容 CLI。
    cm = "" if _personal_native else next((p for p in (
        os.path.join(HERE, "..", "..", "scripts", "export-model-tool.py"),
        os.path.join(HERE, "..", "config", "aigc-model-router", "scripts", "call_model.py"),
        os.path.expanduser("~/.claude/skills/aigc-model-router/scripts/planner_call_model.py"),
    ) if os.path.isfile(p)), "")
    if not _personal_native and not cm:
        failed = {"ok": False, "error": "planner_tool_unavailable"}
        return _request_budget_pause(_provider_results, {
            **failed, "status": "error", "blocked_before_native": True,
            "execution_calls": 0, "planner_attempts": []}) or failed
    supplied_context = request_context if isinstance(request_context, dict) else {}
    source_ref = supplied_context.get("source_ref") if isinstance(supplied_context.get("source_ref"), dict) else {}
    quality_context = (supplied_context.get("quality_context")
                       if isinstance(supplied_context.get("quality_context"), dict) else {})
    project_context = (supplied_context.get("project_context")
                       if isinstance(supplied_context.get("project_context"), dict) else {})
    repo_root = str(project_context.get("repo_root") or project_context.get("workspace_root") or "")
    # The original request is the planner's input, not a display preview.
    # Silently cutting its tail loses requirements before any member can
    # decide what to research or deliver. Provider limits remain explicit.
    context = {"goal": str(question), "attachments": att,
               "conversation_context": str(supplied_context.get("conversation_context") or ""),
               "project_context": project_context,
               "input_snapshot": supplied_context.get("input_snapshot") or {},
               "user_profile": supplied_context.get("user_profile") or {},
               "members": profiles,
               "workspace": {"repo_root": repo_root[:2000],
                             "existing_project": bool(repo_root and os.path.isdir(repo_root))},
               "request_origin": {
                   "type": str(source_ref.get("type") or "user_direct"),
                   "url": str(source_ref.get("url") or "")[:2000],
                   "title": str(source_ref.get("title") or "")[:500],
                   "supports_claims": [str(item)[:500] for item in
                                       (source_ref.get("supports_claims") or [])[:12]],
                   "instruction": ("这是待满足的真实用户需求来源；理解其中用户想获得的结果，"
                                   "不要把它误当成只需摘要、润色或回复的参考材料。"),
               },
               "quality_context": {
                   "original_query": str(quality_context.get("original_query") or question),
                   "experience_intent": supplied_context.get("experience_intent") or {},
                   "golden_task": supplied_context.get("golden_task") or {},
               },
               "selection_resources": {key: value for key, value in resources.items()
                                       if key != "members"},
               "planning_invocation": {
                   "role": "plan_proposal",
                   "permissions_apply_to": "current_planner_invocation_only",
                   "user_request": question,
                   "delegated_permissions": "resolved_by_original_execution_path",
                   "meaning": (
                       "当前辅助调用只生成计划，不执行计划；本次规划进程的只读限制不是用户对后续工作的限制。"
                       "后续执行任务不继承规划进程的只读限制，也不因收到计划就获得额外权限；"
                       "保留用户原始目标及明确授权，各执行者的实际操作范围仍由原运行环境与权限检查决定。"
                       "规划器只读不撤销用户已授权的交付；宿主记录规划响应与checkpoint，"
                       "不代表宿主会代替执行者制作用户要求的成果。"),
               }}
    prompt = ("你是Agent军团总指挥。读完整现场后自主编排,不要按关键词机械路由。"
              "先把用户真正要获得的最终交付物和验收标准编译成机器可读契约；没要求的无关类型不要猜。"
              "如果你判断目标是实现或修改软件，required_artifacts 必须包含可运行或可审查的代码/网页成品，"
              "不能用一段完成声明代替实现；公开 Feature Request 是待满足的需求，不默认等于帮它润色 Issue。"
              "required_artifacts 只表示用户要求落盘/下载/打开的真实文件或媒体；普通聊天文本答案、"
              "一句话解释、翻译和短答只能 require_final=true，required_artifacts 必须为空，不能把聊天答复伪造成 text 文件。"
              "再为用户目标选择最合适成员(简单题只选1个；复杂度或新发现确有净收益时可动态扩军),给每家具体任务;"
              "从当单允许的完整候选中，结合原生能力、当前可用性、授权与实际工作经验自主选队；"
              "协作有助于目标时选择能力互补的成员，不固定某一家，也不为凑人数让全员重复答题。"
              "selection_resources 是当前资源索引，缓存过期或预热中的可用性是未知，不代表能执行。"
              "有图片/PDF/表格时选能调工具的成员；工具目录只是能力声明，"
              "不代表已安装、已配置或获授权，本次可用性以实际工具结果为准。"
              # 速度画像(members[].speed):慢家(heavy)冷启动+深推理适合复杂交付;简单/交互任务
              # 优先选快家。这是给你判断的现场信息·不是硬规则——你自己权衡任务复杂度 vs 速度。
              "每个成员带 speed(快/中/慢):简单或需要快速响应的任务优先选快家;"
              "只有复杂交付(BP/架构/深研)才值得用慢家等它深推理。"
              # 运行资格三态(S3 根因④):让总指挥分清声明与实测·自己权衡·不加硬门。
              "每个成员带 readiness:declared=只在画像里声明(没实测过)/verified=真跑过至少一次"
              "且成功/experienced=实测样本充分且成功率可信；runtime_available 则表示当前运行时探活。"
              "重要交付优先 runtime_available 且 verified/experienced；当前不可用的成员可留作探索，"
              "但不要让它成为有明确文件契约任务的唯一执行者。成员若标 requires_existing_workspace=true，"
              "只有 workspace.existing_project=true 时才适合直接承担编辑实现；否则选择能自主取得源码并产出完整项目的成员。"
              "这是给你的现场信息,不是硬编码路由。"
              # context not control:不用词表替模型硬分类·如实告诉它每个方向的模型【能力特征】,
              # 让它根据"这个任务需要什么能力"自己判断。关键区别:任务要不要【联网深搜/调工具/多步
              # 推理】——要就选会调工具的强模型方向,不要(纯写作/闲聊)才用覆盖稳的通用方向。
              # 用户2026-09-01:让模型选择真正对应【这活是什么性质】。给你7类活的性质·你按
              # "这活到底是什么"如实判断(不是按题面关键词·是按真正需要什么能力),父层据此配
              # 对口档位的模型(强/性价比/便宜自动分配·你只管判性质)。context not control。
              "同时为每家判断【这活是什么性质 directions】,7选1(按活的真实性质·不是题面词):\n"
              "- 写代码:写/改/调试代码、算法、脚本、建站建APP。\n"
              "- 深度推理:多跳推理/调研求证/复杂分析/BP架构/合规深研/难题(需要联网深搜+强推理·同写代码用最强模型)。\n"
              "- 长文:长篇报告/文档/方案/长文写作(篇幅大·同写代码用强模型)。\n"
              "- 中文:中文写作/翻译润色/中文问答(用中文强的模型如GLM/kimi)。\n"
              "- 快答:简单问答/一句话解释/闲聊/短答·不需要查证或深推理的(仍用正式主力，速度来自少编排)。\n"
              "- 看图:看图/识图/OCR;- 生图:文生图/配图/海报。\n"
              "判据:这活到底在干什么?简单直答=快答(省钱)·写代码=写代码·又难又要查=深度推理·中文表达=中文。"
              "不要设总墙钟:成熟harness靠自身收敛,复杂任务需要时间;把timeout_s恒设为0(不限时,由harness自主收尾)。"
              # 军团模式自动决定(用户2026-08-31:不用用户点·按场景自动选)。给你现场·你自己判断该用哪种队形:
              # - pk:多家平等并行各答各的、最后比最佳(适合开放问题/要多个视角/单家直答)。默认。
              # - relay:有明确先后接力(先让A产出、再把A的成果交给B继续加工·如"先写初稿再润色""先调研再据此设计")。
              # - orchestrate:复杂多阶段、需要一个首脑统筹分工再汇总(如完整方案/BP/大型架构·分几块给不同家最后合并)。
              "同时判断【本单最合适的军团队形 suggested_mode】=pk|relay|orchestrate(按上面语义·单家或简单题用pk)。"
              # 组织形态(2026-09-07用户:不写死·每单自适应):研究并行/持续回灌·接力深化·同步互补协作·多轮版本迭代,
              # 全部是总指挥可自由混用的可能性,不是固定工作流;拆分标准=每节点单一可验收产出+依赖清晰。
              "只有复杂、长周期、强事实/安全要求的任务才启用独立验证；验证角色负责找缺陷和给证据，不做答案投票。"
              "协作逻辑和成员必须分开：tasks 的 key 是稳定 task_id（如 research/implement/audit），"
              "节点写 task、depends_on、direction、capability_requirements；只有用户点名或你有充分现场依据时才写 assigned_harness。"
              "理解用户要得到的可使用成果，保留原始目标、最新反馈、媒介与不可妥协项，不按实现方便程度降低标准。"
              "输出 experience_intent 表达核心需求、受众、期望结果、必须感受到/做到的事、体验维度和 ambition；"
              "golden_task 保留本题 input、constraints、expected_user_outcome。高质量目标不会要求固定任务名、"
              "工序、成员数量或前后顺序，也不要为通过规划校验而降低 ambition。"
              "研究、制作可运行片段、同行评审、形成执行说明、最终实现都是可用能力，不是每单必建的四类节点。"
              "由当前证据、未知问题、成员优势和真实依赖决定是否拆分，允许一个成员研究/制作/自验，也允许"
              "先交可使用片段再据体验补研究、并行补位或调整方案。只有真实依赖才写 depends_on。"
              "参照、搜索源与实现路线由你围绕用户结果选择；优先核实来源、许可和运行条件后合法复用/扩展/组合。"
              "既有工程归属不等于旧技术路线不可替换；历史成员对媒介上限的判断不是用户约束，应据事实核实。"
              "只围绕会影响当前决定的缺口研究，足以支持决定就回到制作，不为标签凑参照数量或仪式步骤。"
              "如果需要独立评审或真人验证，在本题计划中明确提出；没有这些证据不能声称已验证，也不因此"
              "自动阻止其它可执行的工作。普通短答和修复不要无故扩张研究或协作。"
              "计划应包含用户真正需要的交付，不能以研究文档、截图、自评或测试数量代替成品。视觉/交互"
              "结果依据真实画面和正常操作检查；工具受阻则保留具体失败和已有现场，寻找其它合法可用路径。"
              "最终入口、所需源文件和使用方式应清楚完整，具体组织与交付形态服从用户目标。"
              "比较优越性的结论需要同输入、约束、预算和独立证据；没有证明就明确未证明。"
              + _TASK_GRAPH_INPUT_CONTEXT
              + "只输出JSON:{\"tasks\":{\"稳定task_id\":{\"task\":\"具体任务\",\"depends_on\":[],\"task_kind\":\"可选结构角色\",\"direction\":\"活的性质\",\"capability_requirements\":[\"需要的能力\"],\"assigned_harness\":\"可选成员key\"}},"
              "\"suggested_mode\":\"pk|relay|orchestrate\",\"pipeline\":[\"relay时的task_id顺序\"],\"hierarchy\":{},"
              "\"delivery_contract\":{\"required_artifacts\":[{\"kind\":\"image|pdf|spreadsheet|web|code|file\",\"min_count\":1,\"formats\":[],\"description\":\"用户明确要求\"}],\"acceptance\":[\"可客观验收项\"],\"require_final\":true},"
              "\"experience_intent\":{\"user_context\":\"\",\"core_need\":\"\",\"desired_outcome\":\"\",\"audience\":\"\",\"medium\":\"\",\"ambition\":\"professional|world_class|frontier\",\"must_feel\":[],\"must_achieve\":[],\"non_negotiables\":[],\"experience_dimensions\":[{\"key\":\"\",\"user_outcome\":\"\",\"minimum_standard\":\"\",\"breakthrough_question\":\"\"}],\"benchmark_queries\":[]},"
              "\"golden_task\":{\"input\":\"\",\"constraints\":\"\",\"expected_user_outcome\":\"\"},"
              "\"verification\":{\"enabled\":false,\"roles\":[\"critic|challenger|auditor\"],\"reviewers\":[{\"key\":\"另一个独立成员\",\"role\":\"critic\",\"targets\":[\"被审逻辑task_id\"]}],\"criteria\":[],\"max_rounds\":1},"
              "\"team_policy\":{\"adaptive\":true,\"min_agents\":1,\"max_agents\":4},"
              "\"timeout_s\":0,\"reason\":\"一句话\"}。"
              "现场:" + _j.dumps(context, ensure_ascii=False))
    if _personal_native:
        return _plan_with_native_codex(
            prompt, user_id=user_id, conversation_id=conversation_id,
            cancel_check=_cancelled, provider_results=_provider_results)
    # 根因(2026-08-28 dogfood 实测):30s 不够——kimi 生成完整编排 JSON(含 tasks/
    # directions/reason)对"数据诊断/方案/BP"这类复杂目标常需 40-60s,30s 超时 → no JSON
    # → 规划器整个挂掉 → 用户派活退到默认快家、拿不到"总指挥读现场选最合适成员"的智能编排。
    # 规划是关键前置步骤,值得等;放宽到 90s(仍远小于 harness 执行时间)。
    #
    # 可靠性(2026-08-28 核查⑥ dogfood 实测:kimi 偶发返回非 JSON·2次里挂1次):no JSON
    # 不能一次就放弃(那会丢掉智能编排退默认家)。换一个模型/appid 再试一次——偶发抖动
    # 靠重试消化,仍失败才返回。这是"从query到交付100%可靠·丝滑"在【规划环节】的落地。
    # 使用现有 selector 的模型、授权、耗尽/坏模型过滤；保留最多三次调用。
    # First effective delta / idle waiting retain 85s; active SSE generation
    # is bounded by native completion/token limits and request cancellation.
    _helper_timeout = 85.0
    _attempts = []

    _diagnostic = _planner_runtime.diagnostic

    _tried_models = set()
    _last_err = "planner no JSON"

    def planner_budget_pause():
        failed = ({"ok": False, "status": "error", "error": _last_err,
                   "planner_attempts": _attempts, "execution_calls": len(_attempts)}
                  if _attempts else None)
        paused = _request_budget_pause(_provider_results, failed)
        return {**paused, "planner_attempts": _attempts, "planner_transport": "cli"} if paused else None

    for _mi in range(3):
        if _cancelled():
            return {"ok": False, "error": "planner_cancelled", "cancelled": True, "planner_attempts": _attempts}
        budget_pause = planner_budget_pause()
        if budget_pause:
            return budget_pause
        _attempt = None
        try:
            _pick = select_usable_model("programming", exclude_models=_tried_models)
            _pm, _pa = _pick.get("model"), _pick.get("appid")
            if not _pm or not _pa:
                if not _tried_models:
                    _last_err = "planner_model_config_unavailable"
                break
            _tried_models.add(_pm)
            _env = _gateway_env()
            _env.update(LEGION_PLANNER_MODEL=_pm, LEGION_PLANNER_APPID=_pa)
            budget_pause = planner_budget_pause()
            if budget_pause:
                return budget_pause
            if _provider_results is not None:
                from error_envelope import request_provider_authorization_hold
                # 与实际 CLI 的出口一致；显式空值仍是未知绑定，不能改用另一网关猜。
                _planner_gateway = _env.get(
                    "LEGION_PLANNER_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")
                _held = request_provider_authorization_hold(
                    _provider_results(), _pa, _planner_gateway)
                if _cancelled():
                    return {"ok": False, "error": "planner_cancelled", "cancelled": True,
                            "planner_attempts": _attempts}
                if _held:
                    return {"ok": False, "status": "blocked",
                            "error": _held["message"], "error_envelope": _held,
                            "blocked_before_native": True, "execution_calls": 0,
                            "planner_attempts": _attempts}
            _started = _clock.monotonic()
            _attempt = {"attempt": len(_attempts) + 1, "model": _pm, "scope": "initial_plan",
                        "timeout_s": _helper_timeout, "phase": "process_start"}
            _attempts.append(_attempt)
            r = _planner_runtime.run_cli(
                [sys.executable, cm, "chat", "--timeout", str(_helper_timeout), "--stream-idle"],
                input=prompt, env=_env, cancel_check=_cancelled)
        except Exception as e:
            # TimeoutExpired/CalledProcessError 的文本包含 argv；不得回显授权或原始 prompt。
            _last_err = f"planner_call:{type(e).__name__}"
            if _attempt is not None:
                _facts, _ = _diagnostic(getattr(e, "legion_stderr", getattr(e, "stderr", "")), _pa)
                _attempt.update(_facts, elapsed_s=round(_clock.monotonic() - _started, 4),
                                error=_last_err)
            continue
        _facts, _code = _diagnostic(r.stderr, _pa)
        _attempt.update(_facts, elapsed_s=round(_clock.monotonic() - _started, 4),
                        returncode=r.returncode, error="")
        if getattr(r, "cancelled", False) or _cancelled():
            _attempt["error"] = "planner_cancelled"
            return {"ok": False, "error": "planner_cancelled", "cancelled": True, "planner_attempts": _attempts}
        if r.returncode:
            _last_err = f"planner_cli_exit:{r.returncode}"
            if _code:
                _last_err += ":" + _code
            _attempt["error"] = _code or f"planner_cli_exit:{r.returncode}"
            continue
        m = re.search(r"\{[\s\S]*\}", r.stdout or "")
        if not m:
            _last_err = "planner no JSON"
            _attempt["error"] = "planner_no_json"
            continue  # 换下一个模型重试
        try:
            _parsed = _parse_planner_json(m.group(0))
        except Exception as e:
            _last_err = f"parse:{type(e).__name__}"
            _attempt["error"] = _last_err
            continue
        if _parsed.get("ok"):
            _parsed["planner_attempts"] = _attempts
            if _mi > 0:
                _parsed["retried"] = _mi  # 透明:第几次重试才成功(用户看得见自愈)
            # 目标高度不是额外模型调用许可；合法计划交回成员，后续研究/复核由其决定。
            # 取消仍优先于交出计划，不把移除质量工序变成绕过运行边界。
            if _cancelled():
                return {"ok": False, "error": "planner_cancelled", "cancelled": True, "planner_attempts": _attempts}
            return _parsed
        # 保留真实解析/结构错误；非空计划被拒绝不等于模型没给任务。
        _parse_error = str(_parsed.get("error") or "")
        _last_err = _parse_error or "planner empty tasks"
        _attempt["error"] = _parse_error or "planner_empty_tasks"
        _details = {key: _parsed[key] for key in
                    ("missing_task_kinds", "reversed_dependencies") if key in _parsed}
        if _details:
            _attempt.update(_details)
    return planner_budget_pause() or {"ok": False, "error": _last_err, "planner_attempts": _attempts}


def _parse_planner_json(raw: str) -> dict:
    """解析总指挥模型返回的编排 JSON → 校验/过滤到可派家 + 合法方向。抽成独立函数供重试复用。"""
    import json as _j
    try:
        d = _j.loads(raw); valid = _harness_keys()
        tasks = {}
        for key, value in (d.get("tasks") or {}).items():
            task_id = str(key).strip()[:120]
            if not task_id:
                continue
            if isinstance(value, dict):
                node = dict(value)
                node["task"] = str(node.get("task") or node.get("prompt") or node.get("role") or "回答问题")
                assigned = str(node.get("assigned_harness") or node.get("assign_to") or
                               node.get("harness") or "").strip()
                if assigned and assigned not in valid:
                    node.pop("assigned_harness", None)
                    node.pop("assign_to", None)
                    node.pop("harness", None)
                tasks[task_id] = node
            elif task_id in valid:
                tasks[task_id] = {"task": str(value), "assigned_harness": task_id}
            else:
                tasks[task_id] = {"task": str(value)}
        # 方向=【活的性质 scene】(用户2026-09-01):规划器现在输出写代码/深度推理/中文/长文/
        # 快答/看图/生图这7类活性质,registry.resolve_assignments 按 scene 选对口档位模型
        # (scene_models 认这些名·快答→便宜·中文→GLM·写代码→sol)。这里【保留规划器原始 scene 名】
        # 传给 registry——绝不能再过 _normalize_direction(那会把"快答/中文"抹成 programming→
        # 场景分类静默失效·上一轮真跑暴露的断点)。只做非空校验;registry 端 _SCENE_ALIASES 兜底
        # 未知名(退 code)。物理方向名(video_generation 等)registry 也识别·向后兼容。
        directions = {str(k): str(v).strip() for k, v in (d.get("directions") or {}).items()
                      if (str(k) in tasks or str(k) in valid) and str(v).strip()}
        for task_id, node in tasks.items():
            if isinstance(node, dict) and node.get("direction"):
                directions.setdefault(task_id, str(node["direction"]).strip())
        hierarchy = {}
        for c, p in (d.get("hierarchy") or {}).items():
            # 模型偶尔把 parent 返回单元素list;宽容归一,别因格式细节让规划全失败
            if isinstance(p, list):
                p = p[0] if p else None
            if str(c) in tasks and isinstance(p, str) and p in tasks and str(c) != p:
                hierarchy[str(c)] = p
        # 去父层硬墙钟(用户2026-08-28钉死·成熟harness不靠父层总墙钟):timeout_s=0/缺省=不限时,
        # 由各家自身收敛。仅当模型/用户显式给正数上限时才尊重(极少数需要卡时的场景)。
        try:
            timeout_s = int(d.get("timeout_s") or 0)
            timeout_s = timeout_s if timeout_s > 0 else 0
        except (TypeError, ValueError):
            timeout_s = 0
        # 军团队形自动决定(用户2026-08-31:不用用户点·按场景自动选)。规划器判断的 suggested_mode
        # 归一到合法三态;单家一律 pk(solo);relay 需要 pipeline 顺序(缺则用 tasks 顺序兜底)。
        _sm = str(d.get("suggested_mode") or "").strip().lower()
        if _sm not in ("pk", "relay", "orchestrate"):
            _sm = ""
        if len(tasks) <= 1:
            _sm = "pk"   # 单家没有"接力/统筹"可言
        _pipeline = [k for k in (d.get("pipeline") or []) if k in tasks]
        if _sm == "relay" and len(_pipeline) < 2:
            _pipeline = list(tasks.keys())   # relay 缺顺序→按 tasks 出现顺序接力
        from legion_contracts import DeliveryContract, VerificationSpec
        _delivery = DeliveryContract.from_value(d.get("delivery_contract") or {
            "required_artifacts": d.get("required_artifacts") or [],
            "acceptance": d.get("acceptance") or [],
            "source": "commander_model",
        }).to_dict()
        # 规划器自己已把媒介判为纯聊天/文本答复且任务只有一个 quick 节点时，文件契约与它的
        # intent 自相矛盾。保留 acceptance 与 final，只去掉模型凭空发明的文件要求。
        _medium = str((d.get("experience_intent") or {}).get("medium") or "").strip().lower()
        _quick_single = len(tasks) == 1 and any(str(value).strip().lower() in {"快答", "fast", "quick"}
                                                for value in directions.values())
        if _quick_single and _medium in {"纯文本", "文本", "chat", "text", "conversation", "聊天"}:
            _delivery["required_artifacts"] = []
        _verification = VerificationSpec.from_value(d.get("verification")).to_dict()
        _team_policy = d.get("team_policy") if isinstance(d.get("team_policy"), dict) else {}
        _experience = d.get("experience_intent") if isinstance(d.get("experience_intent"), dict) else {}
        _golden = d.get("golden_task") if isinstance(d.get("golden_task"), dict) else {}
        # ambition 是用户目标，不是平台固定质量工序的开关。
        # 保留模型任务和依赖；通用依赖解析/调度继续走同一运行链。
        _compiled_world = {}
        if _experience:
            try:
                import frontier_benchmarks as _frontier
                _compiled_world = _frontier.compile_experience_contract(d)
            except Exception:
                _compiled_world = {}
        return {"ok": bool(tasks), "tasks": tasks, "directions": directions,
                "hierarchy": hierarchy, "timeout_s": timeout_s,
                "suggested_mode": _sm, "pipeline": _pipeline,
                "delivery_contract": _delivery, "verification": _verification,
                "team_policy": _team_policy, "experience_intent": _experience,
                "golden_task": _golden,
                **{key: value for key, value in _compiled_world.items()
                   if key not in {"experience_intent", "golden_task"}},
                # 来源与显式质量事实独立于可选体验投影；无 experience_intent 时也不丢失。
                # 仅保留已有上下文字段，不透传身份、执行目录或其他调用配置。
                **{key: ({**_compiled_world[key], **d[key]}
                         if isinstance(d[key], dict) and isinstance(_compiled_world.get(key), dict)
                         else d[key])
                   for key, value_type in (
                    ("quality_context", dict), ("source_ref", dict), ("north_star", dict),
                    ("critical_dimensions", list), ("claim_ceiling", str),
                    ("frontier_research_contract", dict),
                ) if isinstance(d.get(key), value_type)},
                "reason": str(d.get("reason") or "")[:200], "via": "commander_model"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


def parse_dispatch_with_model(instruction: str) -> dict:
    """让总指挥模型把"codex干X,让hermes挂它下"解析成 tasks/hierarchy。

    context not control:不靠前端正则猜语义;把可用成员+原话给模型,它读完返回结构。
    失败返回ok=False,前端才退离线正则。
    """
    import subprocess, json as _j
    text = str(instruction or "").strip()
    if not text:
        # 空输入不静默返回"成功且空"(旧行为让前端误以为规划成功但啥也没派·用户对着
        # 空面板发呆无从排查)。给 context 不加 control:仍 ok=True 不阻断,但带 reason
        # code + 引导语,前端可据此提示用户重新表达。
        # 依据:deepseek schedule_delete 空id返回 {deleted:false, code:"schedule_not_found"}
        # (packages/schedule/schedule/README.md:31)、normalizeApiKey 空值返回
        # ApiKeyRejection('empty')(packages/llm/llm/README.zh.md:76)——空/无效输入
        # 一律返回带原因码的可理解结果,绝不静默"成功空"。
        return {"ok": True, "tasks": {}, "hierarchy": {},
                "reason": "empty_instruction",
                "hint": "没收到派活内容·请说明让哪几家干什么(如「codex调研·cc做数据分析挂codex下」)"}
    keys = sorted(_harness_keys())
    from planner_runtime import tool_path, model_env
    cm = tool_path()
    if not os.path.isfile(cm):
        return {"ok": False, "error": "模型解析器不可用·前端可用离线fallback"}
    prompt = (
        "你是军团总指挥。把用户的自然语言派活解析成JSON,只输出JSON不要解释。"
        f"可用成员:{keys}。格式:{{\"tasks\":{{\"成员key\":\"具体任务\"}},"
        "\"hierarchy\":{\"子成员key\":\"父成员key\"}}}。"
        "只包含用户明确点名的成员;没说层级则hierarchy为空。用户原话:" + text[:1000])
    try:
        r = subprocess.run([sys.executable, cm, "chat", prompt, "--timeout", "30"],
                            capture_output=True, text=True, timeout=35,
                            env=model_env("kimi-k2.7-code-highspeed", _gateway_env()))
        m = re.search(r"\{[\s\S]*\}", r.stdout or "")
        if not m:
            return {"ok": False, "error": "模型未返回JSON"}
        d = _j.loads(m.group(0))
        valid = set(keys)
        tasks = {k: str(v) for k, v in (d.get("tasks") or {}).items() if k in valid}
        hierarchy = {c: p for c, p in (d.get("hierarchy") or {}).items()
                     if c in valid and p in valid and c != p}
        out = {"ok": True, "tasks": tasks, "hierarchy": hierarchy, "via": "commander_model"}
        if not tasks:
            # 说了话但没解析出任何有效派活对象(没点名成员/点了不存在的家)。同空输入:
            # 给可理解 context 让前端引导,不静默返回空面板(依据同上·deepseek 带 code 反馈)。
            out["reason"] = "no_member_matched"
            out["hint"] = f"没识别到要派给谁·可用成员:{keys}(如「让codex调研·cc做数据分析」)"
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


# ── 订阅追踪建议:由执行模型读完整context后自主决定(context not control) ─────────
def suggest_subscription(question: str) -> dict:
    """兼容旧API。父进程不再用关键词判断;LEGION_PROTOCOL 已把"时效问题可建议订阅"作为
    context 给每家模型,由它在答案里自然决定。旧前端收到 suggest=False 不会额外弹父层提示。
    """
    return {"suggest": False, "model_decides": True,
            "reason": "由执行模型基于完整上下文自主判断是否建议订阅(无关键词路由)"}


# ── 隔离执行 + 网关自愈重试 ────────────────────────────────────────────
_WORKER = os.path.join(HERE, "harness_worker.py")
# 未指定调用方总预算时只监测真实活动/原生调用租约，不强加总墙钟。
_ATTEMPT_TIMEOUT = float(os.environ.get("LEGION_ATTEMPT_TIMEOUT", "0"))
# verify/audit 硬超时(2026-09-07):验证是支撑角色不是交付主体——挂死 running 比失败更伤信心。
# 超时如实标"验证未完成",Champion final 照常交付,绝不阻塞。
_VERIFY_TIMEOUT = float(os.environ.get("LEGION_VERIFY_TIMEOUT", "600"))  # 重放#2实锤:prime静态检查已抓到伪3D,但真浏览器桌面+390×844移动全流程300s不够,超时切在半截→结构化JSON没输出→verdict=unverified→REVISE没触发。600s=给真实验证完整旅程的时间;仍不阻塞final(超时如实标注)
# 研究角色默认不额外添加父层墙钟。显式正值仍可覆盖通用 attempt 预算；
# 未配置或非正值交给既有通用预算、停滞检测、取消和进程回收处理。
_RESEARCH_TIMEOUT = float(os.environ.get("LEGION_RESEARCH_TIMEOUT", "0"))
# 中间产物看门狗(2026-09-07):长任务跑超过这个秒数仍无任何 artifact_ready 时,扫本单
# workdir 把可打开的中间文件如实推给用户(标 intermediate·不冒充最终交付)。0=关。
_INTERMEDIATE_AFTER_S = float(os.environ.get("LEGION_INTERMEDIATE_AFTER_S", "300"))
_INTERMEDIATE_SCAN_S = float(os.environ.get("LEGION_INTERMEDIATE_SCAN_S", "60"))
# 自救轮数上限(用户 2026-08-28:"尽量多次自救直到完成"·不要父层固定3次墙)。
# 语义:0 或未设 = 不设固定上限,自救到【所有未试过的可用模型池耗尽】自然收敛(run_one 里
#   "没有可换的未试模型就 break"是天然刹车·候选有限·绝不无限)。这才是"能救就继续直到完成"。
#   正数 = 用户显式要的硬上限(省成本时用·如设 3)。默认 0=不限,交给模型池自然耗尽收敛。
# 另有 _HARD_ATTEMPT_CEIL 极大值防御(极端情况下候选估算异常时兜底·不是业务墙)。
_MAX_ATTEMPTS = int(os.environ.get("LEGION_GATEWAY_RETRIES", "0"))
_HARD_ATTEMPT_CEIL = int(os.environ.get("LEGION_HARD_ATTEMPT_CEIL", "40"))  # >26模型池·纯防御

# ── 动态 appid 轮换 + 失败率驱动重试(用户 2026-08-29 定·治并发撞单appid限流)──────────
# 根因:select_usable_model 挑 appid 时从 trusted 白名单第一个开始 → 并发 N 个 worker 全撞
# 同一个 appid(CatPaw主力)→ 限流误判失败。修:①appid 按轮换游标错开选(并发 worker 分到
# 不同 appid,依据 model router 的 trusted 白名单 + appid_matrix 交叉矩阵——每个 appid 都实测
# 过能跑哪些模型)②失败率高时自动提高重试上限(不再死守 LEGION_GATEWAY_RETRIES=2·2个都限流
# 就误判)③429/限流 appid 短暂冷却换 appid 重试。全是调度层·不改 harness 答题·依据真实数据。
_APPID_CURSOR = _mp_count = None
try:
    _APPID_CURSOR = __import__("itertools").count()  # 全进程单调游标·每次取 appid +1 错开
except Exception:
    pass
_APPID_COOLDOWN: dict = {}          # appid -> 冷却到期时间戳(429/限流后短暂跳过)
_APPID_COOLDOWN_LOCK = threading.Lock()
_APPID_COOLDOWN_S = int(os.environ.get("LEGION_APPID_COOLDOWN_S", "45"))
_GW_STATS = {"ok": 0, "fail": 0}    # 最近网关调用成败(滑动·算失败率驱动动态重试)
_GW_STATS_LOCK = threading.Lock()
_GW_STATS_WINDOW = 20               # 只看最近 N 次(滑动窗口·反映当前限流态而非历史)


def _record_gateway_call(ok: bool) -> None:
    """记一次网关调用成败(滑动窗口·给动态重试次数用)。"""
    with _GW_STATS_LOCK:
        _GW_STATS["ok" if ok else "fail"] += 1
        total = _GW_STATS["ok"] + _GW_STATS["fail"]
        if total > _GW_STATS_WINDOW:   # 滑动:超窗就按比例衰减(保留近况权重)
            _GW_STATS["ok"] = int(_GW_STATS["ok"] * 0.5)
            _GW_STATS["fail"] = int(_GW_STATS["fail"] * 0.5)


def _gateway_fail_rate() -> float:
    with _GW_STATS_LOCK:
        total = _GW_STATS["ok"] + _GW_STATS["fail"]
        return (_GW_STATS["fail"] / total) if total >= 3 else 0.0  # 样本太少不判


def _dynamic_max_attempts(pool_size: int) -> int:
    """失败率驱动的动态重试上限。
    - 用户【没设】LEGION_GATEWAY_RETRIES(默认0):保持原有"不设墙·换未试模型直到模型池自然
      耗尽"行为(_HARD_ATTEMPT_CEIL 给足空间),不破坏"本方向耗尽→跨方向兜底"链路。
    - 用户【显式设了】LEGION_GATEWAY_RETRIES(如loop场景设2):以它为基线,失败率高时自动【提高】
      (避免'2个模型恰好都限流就误判失败'),失败率低保持;永远不低于用户设的值、不砍兜底空间。
    只【提高】不【降低】——动态化是为解决限流误判,绝不缩短原有的自愈/跨方向兜底空间。"""
    ceil = _HARD_ATTEMPT_CEIL if _HARD_ATTEMPT_CEIL > 0 else 40
    if _MAX_ATTEMPTS <= 0:
        return ceil   # 默认:不设墙·耗尽模型池自然收敛(保跨方向兜底·零回归)
    base = _MAX_ATTEMPTS
    rate = _gateway_fail_rate()
    if rate >= 0.5:                    # 网关明显在限流:在用户基线上多给几次机会换 appid/模型
        base = max(base, min(pool_size or 5, 6))
    elif rate >= 0.25:
        base = max(base, min(pool_size or 4, 4))
    return max(1, min(base, ceil))


def _appid_in_cooldown(appid: str) -> bool:
    if not appid:
        return False
    with _APPID_COOLDOWN_LOCK:
        until = _APPID_COOLDOWN.get(appid, 0)
        if until and time.time() < until:
            return True
        if until:
            _APPID_COOLDOWN.pop(appid, None)   # 到期清除
    return False


def _cooldown_appid(appid: str) -> None:
    """把限流的 appid 短暂标冷却(并发下别的 worker 就跳过它·换未冷却的 appid)。"""
    if not appid:
        return
    with _APPID_COOLDOWN_LOCK:
        _APPID_COOLDOWN[appid] = time.time() + _APPID_COOLDOWN_S


def _fallback_candidates(direction: str, exclude: set) -> list:
    """给 harness 下放的 fallback 候选模型:当前方向可用、未试过的几个,交给 harness 自带的
    fallback 机制(如 hermes fallback_model),主模型退化/失败时它【自己】换,不靠父层重试。
    这是把'换模型自愈'的自主权也下放给 harness(用户 2026-08-28:harness 自己改自己那份)。"""
    out, seen = [], set(exclude or [])
    for _ in range(6):
        pick = select_usable_model(direction, exclude_models=seen)
        m = pick.get("model")
        if not m or m in seen:
            break
        seen.add(m)
        out.append(m)
    return out


# 完成契约 context(给 harness·非父层判定):告诉每家'你产出的是给用户看的最终成品',让它
# 自己拿到结果后复检'这符合体验吗、是不是残渣/半截/托词',不符合就自己重调(换模型/多写/收尾)。
# 这不是规则门(不在父层判'。'算不算交付),是把'完成的标准'作为 context 交给 harness 自判——
# 它能拿到最终结果,最清楚符不符合体验(用户 2026-08-28:harness 自己复检交付再决定完没完)。
_DELIVERY_CONTRACT = (
    "【完成契约·按本次职责把关】本次输出由实际调用方按已经明确的职责和交付要求使用，"
    "也可能作为过程向用户展示；并非每次 final 都是用户最终成品。没有明确内部职责时，"
    "应按用户最终请求交付完整可用的结果，不能自行降成未约定的阶段回复。"
    "明确的内部协调、研究、审阅或分工任务应完整完成本次职责，交出可被下一步直接使用的"
    "真实结果或既定协议，并保留整体目标、证据和未完成项。内部回合不必独自完成整单才返回，"
    "但阶段完成也不等于整单已经达标。"
    "交卷前自检：①本次职责要求的结果是否完整？不能以空话或计划冒充已经执行；"
    "本来要求可执行计划、协调协议或短答的任务，可以交相应的完整结果。"
    "②实际接收方能否使用它或据此继续？③本次输出缺失或不合要求，应修正后再返回；"
    "确有阻断或需要协作时，如实交接现场、已有成果和未完成项，不硬报完整。"
    "④研究、制作类的用户交付仍以原始意图和该品类人类顶尖成果为参照，而不是舒适线或"
    "最低字面合规。围绕当前决策缺口按需研究、查证和利用合法可复用资源；已有可靠资料"
    "足以支持下一步就实做，不按固定关键词数量或搜索次数重复，也不保证所有资料一定可得。"
    "视觉/3D等可视化交付应看真实画面、对照实际参照并验证所需操作；状态断言、文件数量"
    "和自评分不能替代体验。依据证据决定是否继续改进、换路线或交接。整体目标尚未达成时"
    "保留真实差距与下一步；只有实际满足目标，才能把整单表述为完成。达到某个参照或"
    "维度的声明应另按对应证据范围表述，不用过程状态代替。"
)

# 网关加权并发闸：轻请求占1格，标准2格，重任务3格；总容量按槽位计，而不是所有任务一刀切。
# 用户 2026-08-29 时效瓶颈实测钉死:capacity=6 时两个 heavy 家(codex/hermes 各占3)就占满·
# 第3家排队等到前家结束→并发退化成串行(3家并发单家13s却总54.7s)。根治:capacity 按"支持
# MAX_CONCURRENT 个 heavy 家真并发"算(*3 而非 *2)·默认 MAX_CONCURRENT 提到 6(用户机器实测
# 内存62%空闲能扛·并发压爆网关由 appid 轮换+限流冷却机制分散兜住·见 select_usable_model)。
_GATEWAY_MAX_CONCURRENT = int(os.environ.get("LEGION_MAX_CONCURRENT", "6"))
_GATEWAY_CAPACITY = int(os.environ.get("LEGION_GATEWAY_CAPACITY", str(max(9, _GATEWAY_MAX_CONCURRENT * 3))))
_GATEWAY_COND = threading.Condition()
_GATEWAY_USED = 0
_GATEWAY_ACTIVE = 0
# 高优先级等待者数(当前用户订单)>0时低优先级(auto_resume后台恢复)让位(2026-09-07单3实锤)
_GATEWAY_HIGH_WAITERS = 0
_COST_WEIGHT = {"light": 1, "standard": 2, "heavy": 3}
# 容量槽控制 provider/appid 压力；进程数还要单独封顶。只用加权槽会允许 18 个 light Harness
# 同时起进程，在 16GB 机器上瞬时把 server OOM-kill（真实全家体检退出137）。默认最多4个进程，
# 可按部署机内存显式调大；正常3家并行不受影响。
_PROCESS_MAX_CONCURRENT = max(1, int(os.environ.get("LEGION_PROCESS_MAX_CONCURRENT", "4")))

# ── 内存压力感知闸(2026-09-02 机器重启复盘根治)────────────────────────────
# 真实血泪:16GB Mac·6并发x多家自愈轮换→harness 子进程内存峰值→server 两次被系统
# SIGKILL(退出码137)·guard 2s救活但窗口内整批题0秒挂·最终疑似内存压力引发整机重启。
# 机制=空闲内存低于 floor 时拦【新任务启动】:已跑的不动(不加新的也不杀旧的)·水位
# 回落自动放行·正常水位完全满并发不减速——同时守住稳定底线和速度底线(铁律0)。
# env LEGION_MEM_PRESSURE_FLOOR 可调·设 0 关闭。
_MEM_PRESSURE_FLOOR = float(os.environ.get("LEGION_MEM_PRESSURE_FLOOR", "10"))
_MEM_PCT_LOCK = threading.Lock()
_MEM_PCT_TS = 0.0
_MEM_PCT_VAL = 100.0


def _mem_free_pct() -> float:
    # 系统空闲内存百分比(0-100)·30s 缓存·任何失败返 100(fail-open 绝不误拦)。
    # 线程安全:并发时只有一个真跑探测(其余拿缓存旧值·宁旧勿卡);探测跑在军团
    # 条件锁外,不阻塞其他 lease 的释放/唤醒。无 memory_pressure 命令(非 macOS)也返 100。
    global _MEM_PCT_TS, _MEM_PCT_VAL
    now = time.monotonic()
    if now - _MEM_PCT_TS < 30.0:
        return _MEM_PCT_VAL
    if not _MEM_PCT_LOCK.acquire(blocking=False):
        return _MEM_PCT_VAL  # 别人正在探·先用旧值
    try:
        if time.monotonic() - _MEM_PCT_TS < 30.0:
            return _MEM_PCT_VAL  # 双检:拿到锁时别人已刷新
        try:
            out = subprocess.run(
                ["/usr/bin/memory_pressure", "-Q"],
                capture_output=True, text=True, timeout=0.35,
            ).stdout or ""
            m = re.search(r"free percentage:\s*([0-9]+(?:\.[0-9]+)?)", out)
            val = float(m.group(1)) if m else 100.0
        except Exception:
            val = 100.0  # 探测失败绝不拦(守住速度底线)
        _MEM_PCT_TS = time.monotonic()
        _MEM_PCT_VAL = val
        return val
    finally:
        _MEM_PCT_LOCK.release()


class _GatewayLeaseCancelled(RuntimeError):
    """A queued dispatch was cancelled before it consumed a gateway/process slot."""


class _GatewayLease:
    def __init__(self, cost_class: str = "standard", *,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 on_wait: Optional[Callable[[dict], None]] = None,
                 poll_seconds: float = 0.25,
                 priority: int = 0):
        self.weight = min(_GATEWAY_CAPACITY, _COST_WEIGHT.get(cost_class, 2))
        self.wait_seconds = 0.0
        self.cancel_check = cancel_check
        self.on_wait = on_wait
        self.poll_seconds = max(0.1, min(float(poll_seconds or 0.25), 0.25))
        self.acquired = False
        self._last_wait_event = 0.0
        # 优先级(2026-09-07单3实锤:23个auto_resume复活团队247任务把执行槽占满,
        # 当前用户订单的修订审计排队85分钟无人执行→final卡死→用户感知"2小时啥也没交"):
        # priority>0=当前用户订单(高)·0=普通·<0=后台恢复团队(低,让位)。在跑的不抢占,
        # 只影响新拿槽顺序。
        self.priority = int(priority)

    def _cancelled(self) -> bool:
        try:
            return bool(self.cancel_check and self.cancel_check())
        except Exception:
            return False

    def _emit_wait(self, reason: str, started: float, *, force: bool = False) -> None:
        self.wait_seconds = time.monotonic() - started
        if reason == "cancelled":
            return
        now = time.monotonic()
        if not self.on_wait or (not force and now - self._last_wait_event < 2.5):
            return
        self._last_wait_event = now
        try:
            self.on_wait({"reason": reason, "wait_seconds": round(self.wait_seconds, 3),
                          "active": _GATEWAY_ACTIVE, "process_limit": _PROCESS_MAX_CONCURRENT,
                          "used": _GATEWAY_USED, "capacity": _GATEWAY_CAPACITY,
                          "weight": self.weight})
        except Exception:
            pass

    def __enter__(self):
        global _GATEWAY_USED, _GATEWAY_ACTIVE, _GATEWAY_HIGH_WAITERS
        started = time.monotonic()
        if self.priority > 0:
            with _GATEWAY_COND:
                _GATEWAY_HIGH_WAITERS += 1
        try:
            while True:
                if self._cancelled():
                    self._emit_wait("cancelled", started, force=True)
                    raise _GatewayLeaseCancelled("queued dispatch cancelled before gateway lease")
                if _MEM_PRESSURE_FLOOR > 0 and _mem_free_pct() < _MEM_PRESSURE_FLOOR:
                    if self._cancelled():
                        self._emit_wait("cancelled", started, force=True)
                        raise _GatewayLeaseCancelled("queued dispatch cancelled during memory probe")
                    self._emit_wait("memory_pressure", started)
                    with _GATEWAY_COND:
                        _GATEWAY_COND.wait(timeout=self.poll_seconds)
                    continue
                reason = "capacity"
                with _GATEWAY_COND:
                    if (_GATEWAY_ACTIVE < _PROCESS_MAX_CONCURRENT
                            and _GATEWAY_USED + self.weight <= _GATEWAY_CAPACITY
                            and (self.priority > 0 or _GATEWAY_HIGH_WAITERS == 0)):
                        _GATEWAY_USED += self.weight
                        _GATEWAY_ACTIVE += 1
                        self.acquired = True
                        break
                    _GATEWAY_COND.wait(timeout=self.poll_seconds)
                self._emit_wait(reason, started)
            self.wait_seconds = time.monotonic() - started
            return self
        finally:
            if self.priority > 0:
                with _GATEWAY_COND:
                    _GATEWAY_HIGH_WAITERS = max(0, _GATEWAY_HIGH_WAITERS - 1)
                    _GATEWAY_COND.notify_all()

    def __exit__(self, *_):
        global _GATEWAY_USED, _GATEWAY_ACTIVE
        if not self.acquired:
            return
        with _GATEWAY_COND:
            _GATEWAY_USED = max(0, _GATEWAY_USED - self.weight)
            _GATEWAY_ACTIVE = max(0, _GATEWAY_ACTIVE - 1)
            self.acquired = False
            _GATEWAY_COND.notify_all()


def _native_scope_facts(plan: dict | None, node: dict | None = None, *,
                        request_id: str = "") -> dict:
    """Explicit execution context selection, never inferred from task quality."""
    plan, node = plan or {}, node or {}
    policy = plan.get("team_policy") or {}
    scope = node.get("native_session_scope")
    if scope is None:
        scope = policy.get("native_session_scope")
    if scope is None:
        return {}
    return {"native_session_scope": scope,
            "native_session_request_id": str(plan.get("team_id") or request_id)}


def _run_one_with_gateway(key: str, task: str, *, lease_plan: Optional[dict] = None,
                          cancel_check: Optional[Callable[[], bool]] = None,
                          priority: int = 0, **kwargs) -> dict:
    """辅助执行统一入口：排队可观察、原请求取消可中断、不会把取消包装成异常。"""
    callback = kwargs.get("cb")
    scope_facts = _native_scope_facts(lease_plan)
    if scope_facts:
        kwargs["task_facts"] = {**scope_facts, **dict(kwargs.get("task_facts") or {})}

    def _wait(facts: dict) -> None:
        if callback:
            callback({"agent": key, "t": "task_queued", "status": "queued", "stage": "queued",
                      "x": f"等待可用执行槽…(已 {float(facts.get('wait_seconds') or 0):.1f}s)",
                      "gateway_queue_wait": float(facts.get("wait_seconds") or 0), "queue": facts})

    lease = _GatewayLease(_cost_class(key, lease_plan), cancel_check=cancel_check, on_wait=_wait,
                          priority=priority)
    try:
        with lease:
            if cancel_check and cancel_check():
                raise _GatewayLeaseCancelled("cancelled immediately after gateway lease")
            kwargs.setdefault("cancel_check", cancel_check)
            result = run_one(key, task, **kwargs)
    except _GatewayLeaseCancelled:
        result = {"key": key, "harness": key, "status": "cancelled", "final": "",
                  "error": "用户停止", "cancelled": True, "attempts": 0}
        if callback:
            callback({"agent": key, "t": "task_status", "status": "cancelled",
                      "x": "用户停止·未启动辅助 Harness"})
    result = dict(result)
    result["gateway_queue_wait"] = round(lease.wait_seconds, 3)
    return result


def _autonomy_matrix() -> dict:
    """拿各家能力/成本矩阵；派活热路径只读静态画像，不同步探外部 agent。

    harness_registry.autonomy_matrix() 的首次调用会 registry.probe 全部外部 agent（实测 20–34s）。
    cost_class 等派活字段来自 manifest，是静态事实；冷态先直接读 manifest 内置家并后台预热完整
    矩阵，不能让用户每次首问替控制面付全量探活墙钟。
    """
    try:
        import harness_registry as _HR
        cache = getattr(_HR, "_AUTONOMY_MATRIX_CACHE", {})
        if cache.get("data") is not None:
            return cache["data"]
        try:
            import runtime_manifest as _rm
            specs = _rm.autonomy_specs()
            static = {key: dict(value) for key, value in specs.items()
                      if isinstance(value, dict) and value.get("integration")}
        except Exception:
            static = {}
        _swr_bg_refresh("autonomy_matrix", lambda: _HR.autonomy_matrix(force=True))
        return static
    except Exception:
        return {}


def _cost_class(key: str, plan: Optional[dict] = None) -> str:
    explicit = ((plan or {}).get("cost_class") or {})
    if isinstance(explicit, str):
        return explicit if explicit in _COST_WEIGHT else "standard"
    if isinstance(explicit, dict) and explicit.get(key) in _COST_WEIGHT:
        return explicit[key]
    try:
        return str(_autonomy_matrix().get(key, {}).get("cost_class") or "standard")
    except Exception:
        return "standard"


# ── 真取消注册表(军团自审 cc/penguin/codex 一致 P0·2026-08-28)────────────────────
# 现状病:team_state.cancel_attempt 只改持久状态·server 没取消 API·前端也没停止入口。用户
# 点断开 SSE→后台线程/子进程照跑烧网关(server.py 注释明说"客户端断开不动后台线程")。
# 修:一个请求级取消令牌注册表(qhash=整单键·(qhash,key)=成员键),贯穿 dispatch→run_one→
# _run_harness_isolated 的监控循环。取消 API 只【设令牌】·拥有 Popen 的那个线程自己安全
# SIGTERM/SIGKILL 进程组(不跨线程直接动 Popen·避免竞态和误杀服务进程组)。这是给执行链
# "用户喊停了"这个真实事实(context)·不是父层替 harness 判断答案(control)。
_CANCEL_LOCK = threading.RLock()
# {qhash: {"__all__": {"reason","ts"}, "<key>": {"reason","ts"}}}
_CANCEL_REGISTRY: dict[str, dict] = {}
_CANCEL_ALL = "__all__"


def _cancel_slot(key: str, scope: str) -> str:
    return f"{scope}:{key}" if scope in {"task", "harness"} else str(key)


def request_cancel(qhash: str, key: Optional[str] = None, reason: str = "用户停止",
                   scope: str = "") -> dict:
    """登记取消请求；scope 明确区分逻辑 task 与 Harness，旧无 scope 调用仍兼容。"""
    qhash = str(qhash or "").strip()
    if not qhash:
        return {"ok": False, "error": "缺 qhash/team_id"}
    effective_scope = scope if key and scope in {"task", "harness"} else ("legacy" if key else "all")
    slot = _cancel_slot(str(key), effective_scope) if key else _CANCEL_ALL
    with _CANCEL_LOCK:
        reg = _CANCEL_REGISTRY.setdefault(qhash, {})
        reg[slot] = {"reason": str(reason or "用户停止")[:200], "ts": time.time(),
                     "scope": effective_scope, "key": str(key or "")}
    # Wake capacity and memory-pressure waiters immediately.
    with _GATEWAY_COND:
        _GATEWAY_COND.notify_all()
    return {"ok": True, "qhash": qhash, "scope": ("member:" + str(key)) if key else "whole_request",
            "target_scope": effective_scope, "reason": str(reason or "用户停止")[:200]}


def is_cancelled(qhash: str, key: Optional[str] = None, scope: str = "") -> bool:
    """整单取消始终生效；task/harness 作用域互不混淆，legacy token 向后兼容。"""
    qhash = str(qhash or "").strip()
    if not qhash:
        return False
    with _CANCEL_LOCK:
        reg = _CANCEL_REGISTRY.get(qhash)
        if not reg:
            return False
        if _CANCEL_ALL in reg:
            return True
        if not key:
            return False
        raw = str(key)
        if scope in {"task", "harness"}:
            return _cancel_slot(raw, scope) in reg or raw in reg
        return raw in reg or _cancel_slot(raw, "task") in reg or _cancel_slot(raw, "harness") in reg


def cancel_reason(qhash: str, key: Optional[str] = None, scope: str = "") -> str:
    qhash = str(qhash or "").strip()
    with _CANCEL_LOCK:
        reg = _CANCEL_REGISTRY.get(qhash) or {}
        if _CANCEL_ALL in reg:
            return reg[_CANCEL_ALL].get("reason") or "用户停止"
        if key:
            raw = str(key)
            slots = ([_cancel_slot(raw, scope), raw] if scope in {"task", "harness"}
                     else [raw, _cancel_slot(raw, "task"), _cancel_slot(raw, "harness")])
            for slot in slots:
                if slot in reg:
                    return reg[slot].get("reason") or "用户停止"
    return "用户停止"


def _request_cancelled(qhash: str, task_id: str = "", harness: str = "") -> bool:
    return (is_cancelled(qhash)
            or bool(task_id and is_cancelled(qhash, task_id, scope="task"))
            or bool(harness and is_cancelled(qhash, harness, scope="harness")))


# ── reasoning 模型不兼容名单(底线:稳定+速度·想100步) ─────────────────────
# 真跑坐实(2026-09-01):gpt-5.6 系是 reasoning 模型(sol/terra/luna)。带 function tools 走网关
# chat completions 时:①cline 直接 400"Function tools with reasoning_effort not supported...use
# /v1/responses or reasoning_effort=none" ②goose/openclaw 极慢且 reasoning 输出被 _strip_reasoning
# 剥成残片(final 只剩"_OK")。→ 用 function tools + chat completions 的 external agent(cline/goose/
# openclaw…)首轮就该避开 reasoning 模型·选 gpt-5.4/GLM/kimi/qwen(chat 原生·带工具稳)。
# 这不是"禁用模型"(control)·是"把该家与这类模型不兼容这个真实现场告诉选择器"(context)·让它首轮
# 就选对·不靠先撞一次 400/残片再靠 model_health 学(那违反速度底线:第一次就慢/错)。
_REASONING_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
# 用 function tools + chat completions 的 external agent(reasoning 模型不兼容·首轮排除)。
# 可随新家扩充;不在名单的家零影响(reasoning 模型对纯对话/responses 家仍可用·如 pi 走 responses)。
_NO_REASONING_HARNESSES = {"cline", "goose", "openclaw"}


def clear_cancel(qhash: str) -> None:
    """新 dispatch 开始/结束时清理同 qhash 的取消状态(避免内存增长+旧取消误伤新单)。"""
    qhash = str(qhash or "").strip()
    if not qhash:
        return
    with _CANCEL_LOCK:
        _CANCEL_REGISTRY.pop(qhash, None)

# 判定"这是网关/模型抖动,值得换个模型重试"的信号(不对任务级/配置级错误瞎重试)。
# 依据(harness 源码):hermes `cron/scheduler_provider.py:56-64 _note_tick_failure`——
# "任何其它失败【重置】计数,backoff/降级只留给自作自受的特定故障,不给瞬时错误"。核心洞察:
# 失败要【分类】,不是所有失败都触发降级。强/弱信号分级正是这一分类:强信号=确定的基础设施
# 故障(该换),弱信号只在短文本里作数(长交付里的同名词不误判)——避免把真交付误当故障丢弃。
# 强信号:明确的基础设施报错串,正常答案几乎不会出现 → 出现即判网关问题(不看长度)。
_GATEWAY_STRONG = ("api call failed", "http 429", "http 502", "http 503", "http 504",
                   "http 500", "rate limit", "每分钟请求次数超过限制", "timed out",
                   "bridge_down", "publisher model", "econnreset", "no choices",
                   "quota exceeded", "model overloaded", "服务暂时不可用",
                   # 本地 cc-router 死(2026-09-02 真跑揪出):cc 的 claude 依赖本机 router(ANTHROPIC_
                   # BASE_URL=127.0.0.1:3456)·router 半死→claude 53s 纯等。adapter 已做探测降级
                   # (返回"cc 本地 router 不可用…")·这里认这个信号→走自愈链。
                   "router 不可用", "cc-router-down",
                   # 模型选错/该家CLI不认这个模型名→换一个它认的模型就行(明确的自愈信号·
                   # 用户2026-09-01真跑kimi-code揪出:选到gpt-5.6-terra·kimi-code CLI说
                   # "Model gpt-5.6-terra is not configured"→旧信号不认→attempts=1不自愈交白卷。
                   # external家各有自己的model白名单·父层给的网关模型名它未必认·失败即换模型自愈)。
                   "is not configured", "not configured in", "model not found",
                   "unknown model", "unsupported model", "invalid model", "model is not supported")
# 弱信号:日常词(empty/connection/429/404…)——可能出现在正常长答案里,只在 final 很短
# (基础设施报错通常几十字)时才判,避免误伤长交付。
_GATEWAY_WEAK = ("timeout", "504", "502", "503", "429", "404", "gateway",
                 "connection", "empty", "overload")
# 注:401/403假交付识别不在全局final词表/前缀做(监督三负例实锤:'HTTP 401: 表示未认证…'
# 也是合法回答)。识别下沉到 hermes adapter 层按原始失败事件(rc/stderr)·见 harness_registry。
_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded", "exceeds the context window", "maximum context length",
    "input is too long", "prompt is too long",
)


def _is_context_overflow(status: str, text: str) -> bool:
    """Legacy text markers only; new vendor evidence is normalized from error/envelope."""
    blob = f"{status} {text}".lower()
    if any(marker in blob for marker in _CONTEXT_ERROR_MARKERS):
        return True
    return "too many tokens" in blob and str(status or "").lower() not in {"ok", "done", "success"}


def _looks_like_gateway_issue(status: str, final: str) -> bool:
    # timeout / rcN 进程级失败、adapter 明确的 incomplete(没跑完)、reasoning 被截断只剩残句
    # → 都是"没真交付",换模型重做(真实 E2E:hermes 返 incomplete·必须自愈)。
    #
    # ★ 根因修(2026-08-28 真派 cc 揪出):进程级失败的 status 不只 "rc" 开头——真实值是
    #   "err:worker_no_json:rc-9"(CC headless 被 SIGKILL·rc-9)、"err:worker_...:rc1" 等,
    #   前缀是 err:worker_...。旧 `startswith("rc")` 匹配不到里面的 rcN → 被判"非网关不重试"→
    #   一次就交白卷(违反"任何报错都揪根+丝滑可靠尽力100%"·该换模型/补位接力)。改:status 里
    #   【出现 rc<数字>(含负=被信号杀) 或 worker_no_json/rc- 这类进程级失败标记】都算基础设施
    #   失败→自愈重试。学 deepseek timeout-policy:基础设施失败原样回流+换招,不当任务级永久错。
    if status in ("runtime_budget", "idle_stall", "cancelled"):
        return False
    if status in ("timeout", "truncated_reasoning", "incomplete"):
        return True
    if re.search(r"\brc-?\d+\b|worker_no_json|rc[<-]", status or ""):
        return True
    if _is_context_overflow(status, final):
        return True
    blob = f"{status} {final}".lower()
    if any(sig in blob for sig in _GATEWAY_STRONG):
        return True
    # 弱信号只在短文本里作数(报错串短·长交付里的同名词不误判)
    if len((final or "").strip()) <= 200 and any(sig in blob for sig in _GATEWAY_WEAK):
        return True
    return False


def _classify_failure(status: str, error: str, fail_kind: str = "") -> str:
    """失败原因分类(军团反思⑥"别混成一句")。优先信 _run_harness_isolated 已给的结构化 fail_kind,
    没有才从 status/error 归类。让接力家/前端能程序化区分,不用解析一句混合文本。取值:
      idle_stall(卡死) / wall_timeout(跑太久) / signal_killed(被外部信号杀·含OOM rc-9) /
      user_cancel(用户停) / tool_timeout(工具级超时) / truncated(思考链截断) /
      gateway(网关/模型抖动) / empty(空交付) / stalled(只给托词) / task_error(任务级其它错)。
    """
    if fail_kind:
        return fail_kind
    s = str(status or ""); e = str(error or ""); blob = f"{s} {e}".lower()
    if s == "cancelled":
        return "user_cancel"
    # 环境资源耗尽(2026-09-23 修沙盒死循环):bwrap/namespace 配额打满时 CLI 把故障写进
    # final/error 自述(不是 rc 级失败),普通 failover 换到的下一家仍撞同一堵墙→死循环。
    # 单独分类让接力选家能识别"环境坏了"换赛道,而不是反复派同型 CLI 撞墙。
    if any(marker in blob for marker in (
            "bwrap", "enospc", "max_user_namespaces", "max_*_namespaces",
            "creating new namespace failed", "nesting depth")):
        return "environment_exhausted"
    if s == "truncated_reasoning" or "截断" in e:
        return "truncated"
    if "tool_timeout" in blob or "工具超时" in e:
        return "tool_timeout"
    # SIGTERM(-15) 是父层停止/重启/切换 attempt 的可恢复中断，不是 provider 或模型故障；
    # SIGKILL(-9) 等其它信号才归 signal_killed。上游有用户取消令牌时已先命中 user_cancel。
    if re.search(r"\brc-15\b", s):
        return "interrupted"
    if re.search(r"\brc-\d+\b|rc-9\b", s):
        return "signal_killed"
    if s == "timeout":
        return "wall_timeout"       # run_one 层看到的笼统 timeout(_run_harness_isolated 未细分时)
    if "只给计划句" in e or "托词" in e:
        return "stalled"
    if "空交付" in e or (not e and not s):
        return "empty"
    if _is_context_overflow(s, e):
        return "context_overflow"
    if _looks_like_gateway_issue(s, e):
        return "gateway"
    return "task_error"


# "只给计划句没真交付"的托词信号(学 deepseek repeat-reminder:只识别摆烂·不否决答案)。
# 典型:cc 抓不到网页时说"我会改用军团抓取链路"就交差——说要做但没做。判定极保守:
# 必须又短(没实质内容)又含明显的"我将去做"式措辞,避免误伤正常的简短真答案。
_STALL_PHRASES = (
    "我会改用", "我将改用", "我会使用", "我将使用", "我会去", "我将去", "我会尝试", "我将尝试",
    "接下来我会", "接下来我将", "让我来", "我来抓", "我来读", "我来获取", "稍后", "马上去",
    "正在提取", "正在查找", "正在搜索", "正在检索", "我先找", "我先查", "还需进一步", "接着提取",
    "i will now", "i'll now", "let me", "i will use", "i'll use", "i am going to",
    "i will try", "i'll try", "i will fetch", "i will read", "i will access",
    # 英文"我正在做X但没给结果"的进行态托词(2026-08-28 BC最难题实测:codex 返回
    # "I've identified the book; now extracting its acknowledgments directly." status=done
    # 却没最终答案——被墙钟切在中间步·必须判托词触发续跑,别把"正在提取"当成品交付)。
    "now extracting", "now searching", "now checking", "now looking", "now retrieving",
    "i've identified", "i have identified", "i'll extract", "i will extract",
    "let me now", "i'm now", "i am now", "currently extracting", "next, i", "next i'll",
)


# 各家走 AIGC 网关时,reasoning 类模型会把内部思考链用 <think>…</think>(或
# <thinking>/<reasoning>)包住,连同正文一起吐回来。军团历史上把这坨原样当交付给用户
# (100场景 dogfood 实测 16/100 泄漏,其中 3 条正文被截断、只剩思考草稿)。这不是替 agent
# 答题——是修"内部草稿漏进给用户看的成品"这条链路 bug:把思考剥掉,只留真正的交付。
# 剥完若为空(模型只输出了思考、正文没写出来),交给上层空 final 自愈重试,让它自己重做。
#
# 依据(harness 源码·三证):hermes `agent/transports/bedrock.py:108-116`——transport 层把
# reasoning 和 content 分成 NormalizedResponse 的两个独立字段(`reasoning=... , content=...`),
# 用户交付只用 content,思考单独存、绝不进正文。这是 harness 处理 reasoning 的标准做法:分离。
# 我们这里是同一原则的补丁层:当模型/网关【没走结构化分字段】、把 think 内联进了 content 文本
# 时(<think> 标签),在军团统一出口把它剥回去——等价于 hermes 的 content/reasoning 分离。
_THINK_BLOCK_RE = re.compile(
    r"<\s*(think|thinking|reasoning|thought)\s*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 未闭合的思考块(正文被 token 上限截断,只剩 "<think>…" 没有 </think>):从起始标签一路删到结尾。
_THINK_OPEN_RE = re.compile(
    r"<\s*(think|thinking|reasoning|thought)\s*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_reasoning(final: str) -> str:
    """从最终交付里剥掉模型的内部思考链 <think>…</think>,只保留给用户看的成品。

    给 context 不加 control:这不改模型答案的内容,只把它明确标注为"思考"的草稿段拿掉,
    等价于用户永远只看成品、看不到模型的内心独白——和主流 chat 产品行为一致。
    """
    f = final or ""
    if "<" not in f:
        return f
    low = f.lower()
    if not any(tag in low for tag in ("<think", "<thinking", "<reasoning", "<thought")):
        return f
    f = _THINK_BLOCK_RE.sub("", f)          # 先剥所有闭合思考块
    f = _THINK_OPEN_RE.sub("", f)           # 再剥被截断的未闭合思考块(整篇只有思考时→清空)
    return f.strip()


# 将来时开局意图(q30d 2026-09-02 补强:qwen-code 三连托词"我将通过云真机 MCP 操作桌面…
# 然后截图核验"式不在 _STALL_PHRASES 词表而漏判·d25/d26/d27 三题全中)。开局"我(将/会/
# 准备/打算)+(通过/依次/直接/马上/立即/先/逐步)?+动作动词"且全文无完成证据=计划书非交付。
# 说话动词(建议/推荐/认为)不触发防误伤;含完成证据(已/如下/如图/保存在…)不判;短答案安全。
_FUTURE_INTENT_RE = re.compile(
    r"^我(将|会|准备|打算|想要)[^，。；、！\n]{0,28}?"
    r"(操作|控制|打开|创建|执行|获取|截图|抓取|访问|调用|运行|安装|写入|生成|切换|调整|新建|右键|拖拽|拖动|输入|连接|启动|使用)",
)
_DONE_MARKERS = ("已", "如下", "如图", "结果是", "保存在", "输出为", "产生了", "saved", "created", "generated", "completed")


def _looks_like_stall(final: str) -> bool:
    """final 像"我准备去做"而非真结果 → True。保守:短 + 含托词措辞才算。"""
    f = (final or "").strip()
    if not f:
        return False              # 空 final 由 bool(final) 那条管,不归托词
    if len(f) > 400:
        return False              # 有实质长度内容,大概率真干了,不判托词
    low = f.lower()
    if any(p in low for p in _STALL_PHRASES):
        return True
    _head = f.split("，", 1)[0]
    if (_FUTURE_INTENT_RE.match(f) and not any(m in f for m in _DONE_MARKERS)
            and not any(v in _head for v in ("建议", "推荐", "认为", "觉得"))):
        return True
    return False


# 上下文预算(军团四首脑复核共识2:失败回流从"字符硬截"升级为"token 保近弃远")。
# 依据:codex `codex-rs/core/src/compact.rs:665-683`——从最新消息倒序按 token 预算累加,
# 放不下就整条丢最旧的、单条超预算才对边界那条按 token 截断,恢复顺序;token 估算
# `realtime_context.rs:45,577-579` 用 4 字节/token 的保守近似。军团这里做等价的最小实现:
# note(最新一轮)整条永保,prev(历史)按剩余 token 预算从尾部保留、超预算整块让位不从中间切。
_APPROX_BYTES_PER_TOKEN = 4
_FAILCTX_BUDGET_TOKENS = int(os.environ.get("LEGION_FAILCTX_BUDGET_TOKENS", "8000"))  # ≈旧 32000 字节等价


def _approx_tokens(s: str) -> int:
    """保守近似 token 数(同 codex realtime_context.rs:45 的 4 字节/token)。CJK 偏保守多算。"""
    try:
        return (len(str(s or "").encode("utf-8")) + _APPROX_BYTES_PER_TOKEN - 1) // _APPROX_BYTES_PER_TOKEN
    except Exception:
        return len(str(s or "")) // 3


def _truncate_by_tokens(s: str, max_tokens: int, keep_head_ratio: float = 0.0) -> str:
    """把单条文本按 token 预算截断(超预算才截)。keep_head_ratio>0 时头尾都保、中间省。
    对应 codex compact.rs:668-673 的"单条超预算按 token 截边界那条"。"""
    s = str(s or "")
    if max_tokens <= 0:
        return ""
    if _approx_tokens(s) <= max_tokens:
        return s
    max_bytes = max_tokens * _APPROX_BYTES_PER_TOKEN
    b = s.encode("utf-8")
    if keep_head_ratio > 0:
        # 省略标记本身占字节·必须从预算预扣(codex 首脑复核 R2 揪出:旧版 head_b+tail_b=max_bytes
        # 再拼标记→总量超预算一个标记的长度·极端 note 分支 8000→8009)。预扣后严格 ≤ max_bytes。
        _marker = "\n…(token 预算·中间省略)…\n"
        _marker_b = len(_marker.encode("utf-8"))
        avail = max(0, max_bytes - _marker_b)
        head_b = int(avail * keep_head_ratio)
        tail_b = avail - head_b
        head = b[:head_b].decode("utf-8", "ignore")
        tail = b[-tail_b:].decode("utf-8", "ignore") if tail_b > 0 else ""
        return head + _marker + tail
    return b[-max_bytes:].decode("utf-8", "ignore")


def _build_failure_context(prev: str, attempt: int, key: str, model: Optional[str],
                           status: str, final: str, stalled: bool, error: str = "",
                           actions: Optional[list] = None) -> str:
    """把上一轮的真实结果原样回流给下一轮(只给事实·不教它怎么做)。

    依据(三证合一·file:line):
      · deepseek timeout-policy `packages/guard/timeout-policy/src/index.ts:41-47`——超时后把
        结构化 {isError:true, content:"Error: tool call timed out after Nms"} 作为 tool_result
        原样喂回,不加"你该换XX"的指导。
      · deepseek repeat-tool-reminder `.../repeat-tool-reminder/src/index.ts:64-78`——提醒只说
        "分析上一次结果,若没完成就换个方法或换参数,或收尾",【绝不列具体手段】(不写换UA/换工具)。
        且 enrich-never-veto(:209),只追加 context 不否决。
      · codex `codex-rs/core/src/mcp_tool_call.rs`——失败结果 is_error 也原样进 ConversationHistory。
    结论:回流=如实呈现上一轮的 status/输出片段(事实)+ 一句最克制的"分析后换个方法或收尾",
    绝不替它写"换UA/换工具/换来源"——那是"傻都你告诉",违反"让军团自己看得见处境自己想"。
    """
    # 给模型看【完整的上一轮真实输出】,不是片段——它要看清自己上轮到底做了啥/错在哪,
    # 才能真换招(否则是盲换=一会儿好一会儿坏)。依据:codex `mcp_tool_call.rs`+`compact.rs
    # record_items`——失败结果整段进 ConversationHistory 喂回下轮·靠 harness 自己的 compaction
    # 管窗口·不由父层预先截片段。军团最多3轮且各家都有上下文管理,这里不替它砍信息。
    # timeout 也可能已经产出半成品/真实报错；不能用一句“无进展”覆盖掉现场。所有失败类型统一
    # 保留 status + 完整输出 + 完整 error，模型才看得见自己上轮到底做到哪、为什么停。
    # 字段先各自限窗(军团 deepseek dogfood 揪出:大输出失败时,note 本身超长→末尾 [-32000:]
    # 会切掉 note 头标(第几轮/哪家/动作清单在开头)只留最没信息量的文档尾巴→模型盲换)。
    # 头+尾保留(中间省略):失败诊断信息通常在输出的头(做了啥)和尾(错在哪),中间可省。
    def _window(s: str, head: int, tail_n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= head + tail_n else s[:head] + "\n…(中间省略)…\n" + s[-tail_n:]
    tail = _window(final, 4000, 3000)
    err = _window(error, 1500, 500)
    if tail and err and err not in tail:
        body = f"输出:\n{tail}\n真实报错:\n{err}"
    elif tail:
        body = f"真实输出:\n{tail}"
    elif err:
        body = f"真实报错:\n{err}"
    else:
        body = "(无输出无报错·仅 status)"
    if stalled:
        what = f"上一轮只回了计划而未交付。status={status};{body}"
    else:
        what = f"上一轮未成功。status={status};{body}"
    # 学 deepseek 的最克制提醒:只说"分析结果·换方法或收尾",不列任何具体手段(让它自己判断)。
    # 动作清单(军团③):如实列出上一轮真调了哪些工具、成没成——让下一轮/接力家看到具体动作,
    # 而不只是 final 摘要(它据此避开上次失败的工具/参数·自己换招·仍是给事实不教手段)。
    acts_block = ""
    if actions:
        _lines = []
        for a in actions[-12:]:
            if a.get("t") == "tool_started":
                _lines.append(f"  · 调用 {a.get('tool')}({str(a.get('brief') or '')[:50]})")
            elif a.get("t") == "tool_end":
                _lines.append(f"    → {a.get('tool')} {'成功' if a.get('ok') else '未成功'}"
                               f"{('·'+str(a.get('brief'))[:50]) if a.get('brief') else ''}")
            elif a.get("t") == "tool_timeout":
                _lines.append(f"    → {a.get('tool')} 超时")
            elif a.get("t") == "harness_step":
                # deepseek 首脑复核(轻):各家 adapter 实际只发 harness_step·acts_block 旧版不渲染它
                # →失败回流的"动作清单"对真实 adapter 恒为空。补上·让接力家看到上轮真实走了哪几步。
                _b = str(a.get("brief") or "")[:50]
                _u = a.get("urls") or []
                _lines.append(f"  · 步骤 {_b}" + (f"(访问 {len(_u)} 个网页)" if _u else ""))
        if _lines:
            acts_block = "\n上一轮的工具动作(供你避开已失败的工具/参数):\n" + "\n".join(_lines)
    note = (f"\n\n———\n【上一轮回流·第{attempt}轮·{key}{('·模型'+model) if model else ''}】\n"
            f"{what}{acts_block}\n"
            f"请先分析上面这个真实结果,再决定下一步:如果任务未完成,换一个不同的方法或角度,"
            f"或在证据足够时直接收尾并交付真实结果。")
    # token 保近弃远(四首脑复核共识2·抄 codex compact.rs:665-683):
    # note(最新一轮·含头标+动作清单+真实报错)整条永保——它是模型最该看的当下处境;
    # prev(历史)用【剩余】token 预算从尾部保留,超预算只丢最旧的字符、绝不切掉 note 头标。
    # 相比旧 combined[-32000:] 字符硬截:预算以 token 计(跨语言更准)、最新一轮结构永不被逐出、
    # 单条 note 若自身超预算才按 token 截边界(保头尾省中间)。收编旧 24000/32000 字符散点魔数。
    note_toks = _approx_tokens(note)
    if note_toks >= _FAILCTX_BUDGET_TOKENS:
        # 极端:单条 note 就撑满预算——按 token 截 note 本身(保头标+尾报错,省中间),不带 prev。
        return _truncate_by_tokens(note, _FAILCTX_BUDGET_TOKENS, keep_head_ratio=0.5)
    prev_budget = _FAILCTX_BUDGET_TOKENS - note_toks
    prev_kept = _truncate_by_tokens(prev, prev_budget)  # 历史只从尾部保、超预算丢最旧
    out = prev_kept + note
    # 严格上限(codex 首脑复核揪出:近似估算下 note+prev 相加可能微超预算·8000→8009)。
    # 拼接后再兜一次:若总量仍超,只收紧 prev 头部(丢最旧)、绝不动 note——保证严格 ≤ 预算,
    # 且最新一轮结构不受影响。近似误差就此吸收,不再用"budget+50 容差"掩盖越界。
    if _approx_tokens(out) > _FAILCTX_BUDGET_TOKENS and prev_kept:
        shrink = _FAILCTX_BUDGET_TOKENS - note_toks
        prev_kept = _truncate_by_tokens(prev_kept, max(0, shrink - 1))  # 再紧 1 token 边际吸收误差
        out = prev_kept + note
    return out


# 无进展心跳超时(idle timeout):不是"跑久=失败"的总墙钟,而是"卡死=失败"的检测。
# 用户"人家都没有墙钟"——成熟harness没有总墙钟,但正常跑的复杂任务会【持续】推进;
# 真卡死(codex等网关不返回)的特征是【既无输出、又不烧CPU】。所以判据从"跑了多久"改成
# "多久没动静了"。两个真实"在推进"信号(满足任一就不判卡死):
#   ① 子进程有 stdout/stderr 输出(harness打印/心跳)
#   ② 进程组还在烧CPU(auto多轮检索/codex推理时CPU忙·只是不打印)——治auto被误杀。
# 只有【连续idle秒·既无输出又CPU几乎不动】才判真卡死→杀→回流(学deepseek timeout-policy)。
# 150s对"整段静默等网关慢响应"的家(hermes源码入口AIAgent中间不print·CPU也低)太激进——
# 实测hermes换电调研282s/deepseek融资BP285s都是真成功·静默等250-300s是常态不是卡死。
# 按真实成功数据放到330s:仍能杀真正永久卡死(网关永不返回·会拖到330s但最终释放)·
# 又给慢网关足够空间不误杀。配合流式心跳(有输出的家更早刷新)·双保险。
_IDLE_TIMEOUT = float(os.environ.get("LEGION_IDLE_TIMEOUT", "330"))
# 兼容显式设置的总预算；未设置时不另加总时长限制。
_TOTAL_CAP = float(os.environ.get("LEGION_TOTAL_CAP", "0"))
# CPU 活动阈值:两次采样间进程组累计CPU时间增量 > 此值(秒)= 在烧CPU = 在推进。
_CPU_ACTIVE_DELTA = float(os.environ.get("LEGION_CPU_ACTIVE_DELTA", "0.3"))


def _pgroup_cpu_seconds(pgid: int) -> float:
    from harness_registry import _pgroup_cpu_seconds as sample
    return sample(pgid)


# 可进程内直调的 core 家(已 ctx 化·用 ctx.model 参数传模型·不依赖 run_comparison 全局常量)。
# ★方案已验证可行(codex 19→7s / hermes 13→6s·事件回流经 thread-local sink 打通)·但它是新的执行
# 路径·必须先完整对齐 run_one 的全部语义(cancel_check 取消·失败 context 回流·native_checkpoint·
# model_used 上报·user/memory 传递)+ 让语义测试覆盖这条路径·才能启用(否则绕过 _run_harness_isolated
# 会丢这些能力·14 个语义测试已暴露)。开关默认【空=不启用】·经 env 显式开(试点/验证用)·
# 全部语义对齐+测试覆盖后再默认开。这是伤筋动骨改动·守铁律:验证充分再上·不带红灯 commit。
# 默认开的已 ctx 化 core 家(codex/hermes/penguin/jcode/prime/deepagents/omnigent)——实测进程内
# 直调 codex 19→7s/hermes 13→6s·语义已对齐(model上报/失败回流/checkpoint 共用 run_one 后续处理·
# 取消跑前查·14 语义测试经 patch_exec 覆盖三条执行路都过)。env LEGION_CORE_INPROC 可覆盖(调优/回退)。
# cc 也可 inproc(2026-09-01 loop 验证:它 adapter 接 ctx·走 claude CLI 自己配置不依赖全局常量·
# 隔离子进程对它同样多余·实测 15→7.6s)。auto(native引擎)/deepseek(dsh bridge)启动/常量机制不同·
# 暂留隔离子进程需单独验证。
# ★codex 移出 inproc(2026-09-01 loop 真跑锁定):codex inproc 在常驻 server 进程里【全 rc1】
#   (换模型27→3轮收敛靠 cc 补位接力兜住·26s)·但直调 adapter/全新进程完全 ok(final="好")→
#   差异只在"常驻 server 进程内 inproc 调 codex"·很可能 codex CLI 子进程继承了 inproc 特有的
#   fd/信号/bridge 状态。真跑对比:codex【子进程模式 rc1=0·自己交付·13.9s】完胜【inproc 26s+接力】。
#   codex bridge(responses wire_api)机制与常驻进程 inproc 不兼容·让它走稳定子进程(_run_harness_isolated)。
#   codex 是首选快家·子进程 13.9s 够快·稳定压倒微小加速。inproc bridge 兼容性是后续单独课题。
_CORE_INPROC_DEFAULT = "hermes,penguin,jcode,prime,deepagents,cc"
_CORE_INPROC_KEYS = set(
    (os.environ.get("LEGION_CORE_INPROC", _CORE_INPROC_DEFAULT)).replace(" ", "").split(",")) - {""}


def _hb_act_from_event(ev: dict) -> str:
    """事件→人话动作摘要(2026-09-07用户:心跳越人话越透明越好·不要'思考中'废话)。"""
    t = str((ev or {}).get("t") or "")
    x = str((ev or {}).get("x") or "").strip()
    tool = str((ev or {}).get("tool") or "").strip()
    args = str((ev or {}).get("args_brief") or "").strip()
    if t == "tool_started":
        return (f"正在跑工具 {tool}" + (f"：{args[:70]}" if args else "")).strip()
    if t == "tool_end" and tool:
        return f"工具 {tool} 返回结果中"
    if t == "tool_timeout" and tool:
        return f"工具 {tool} 超时·换招中"
    if x:
        return x[:90]
    return ""


def _hb_x(act: dict, elapsed: float, fresh_s: float = 20.0) -> str:
    """成员心跳文案:20s内有真实动作→显示动作本身;否则如实说'暂无新动作'。"""
    try:
        if isinstance(act, dict) and act.get("x") and time.time() - float(act.get("ts") or 0) <= fresh_s:
            return f"{str(act['x']).strip()[:90]}（已 {elapsed:g}s）"
    except Exception:
        pass
    return f"正在调模型/跑工具·暂无新动作（已 {elapsed:g}s）"


def _runtime_deadline(timeout_s: float) -> float:
    """继承调用者的绝对截止点；换家/重试只能收窄，不能重新获得一轮预算。"""
    import math
    registry = sys.modules.get("harness_registry")
    local = getattr(getattr(registry, "_INPROC_RUNTIME_ENV", None), "values", {})
    inherited = float(local.get("MEYO_TASK_DEADLINE", os.environ.get("MEYO_TASK_DEADLINE", 0)))
    if not math.isfinite(inherited) or inherited < 0:
        raise ValueError("MEYO_TASK_DEADLINE must be finite and non-negative")
    now = time.monotonic()
    limits = [now + value for value in (timeout_s, _TOTAL_CAP) if value > 0]
    if inherited:
        limits.append(inherited)
    return min(limits) if limits else 0


def _runtime_context_env(runtime_brief, task_facts, capability_policy,
                         task_memory, project_context, prior_attempts, *, key, qhash) -> dict:
    """三入口使用现有 JSON 字段；空记录也必须覆盖旧 env，尤其不能继承父 native 槽。"""
    import hashlib
    import task_memory as memory_store
    from harness_registry import (_CONTEXT_BINDINGS, _INLINE_CONTEXT_BYTES,
                                  _context_binding_view, _context_preview_json)
    memory_request_id = ((task_facts or {}).get("native_request_memory_id")
                         if (task_facts or {}).get("native_session_scope") == "request" else None) or qhash
    values = {
        "MEYO_TASK_FACTS_JSON": task_facts or {},
        "MEYO_CAPABILITY_POLICY_JSON": capability_policy or {},
        "MEYO_TASK_MEMORY_JSON": task_memory or {},
        "MEYO_PROJECT_CONTEXT_JSON": project_context or {},
        "MEYO_PRIOR_ATTEMPTS": prior_attempts or [],
        "MEYO_RUNTIME_BRIEF": runtime_brief or {},
    }
    env = {key: json.dumps(value, ensure_ascii=False) for key, value in values.items()}
    limits = {name: 4000 if name == "MEYO_RUNTIME_BRIEF" else 32000 for name in values}
    large = {name for name, text in env.items() if len(text.encode("utf-8")) > limits[name]}
    if "MEYO_CAPABILITY_POLICY_JSON" in large:
        raise ValueError("capability policy exceeds transport budget; permissions were not truncated")
    # Even a value below the env limit may exceed its final prompt allocation.
    # Retain its original in the same request store, keyed by content hash. The
    # renderer only reads these references; it does not write files or replace
    # the original ctx used by adapters for runtime decisions.
    referenced = {name for name, value in values.items()
                  if value and len(env[name].encode("utf-8")) > _INLINE_CONTEXT_BYTES
                  and name != "MEYO_CAPABILITY_POLICY_JSON"}
    refs = {}
    if referenced:
        # 只用本 request 已有 task_memory 文件；哈希键保留重试前版本，不另建 context 文件系统。
        digests = {name: hashlib.sha256(env[name].encode("utf-8")).hexdigest() for name in referenced}
        # Reuse the store's lock across this in-process read/merge/write too.
        with memory_store._LOCK:
            saved = dict(memory_store.load(memory_request_id, key).get("runtime_context") or {})
            additions = {digests[name]: values[name] for name in referenced if digests[name] not in saved}
            if additions:
                saved.update(additions)
                memory_store.update(memory_request_id, key, runtime_context=saved)
        path = str(memory_store._path(memory_request_id, key).resolve())
        refs = {name: {"path": path, "json_pointer": f"/runtime_context/{digests[name]}",
                       "sha256": digests[name], "bytes": len(env[name].encode("utf-8")),
                       "encoding": "json-utf8"} for name in referenced}
    env["MEYO_CONTEXT_REFS_JSON"] = json.dumps(refs, ensure_ascii=False)
    if large:
        # 这些字段参与身份、目录、恢复、重试或权限判断，必须原样保留，不能变成摘要。
        keep = _CONTEXT_BINDINGS
        for name in large:
            value, text, limit = values[name], env[name], limits[name]
            marker = {"context_ref": refs[name]}
            if isinstance(value, list):
                projected = [{k: row[k] for k in keep.get(name, ()) if k in row}
                             for row in value if isinstance(row, dict)] + [{"actions": [marker]}]
            else:
                projected = _context_binding_view(value, name) if isinstance(value, dict) else {}
                projected.update(marker)
                marker = projected
            # 复用原上下文文本预算；只截 preview 字符串，不截序列化后的 JSON。
            env[name] = _context_preview_json(projected, marker, text, limit)
    if isinstance(runtime_brief, str) and "MEYO_RUNTIME_BRIEF" not in large:
        env["MEYO_RUNTIME_BRIEF"] = runtime_brief or "{}"  # 兼容已有纯文本 brief。
    return env


def _explicit_provider_binding(key: str, binding: Optional[dict] = None) -> Optional[dict]:
    """Private execution configuration; never put credentials in task context."""
    if binding is not None:
        return dict(binding)
    if key == "codex" and os.environ.get("LEGION_CODEX_TRANSPORT", "").strip().lower() == "responses":
        import model_registry
        return model_registry.explicit_responses_binding()
    return None


def _provider_configuration_error(key: str) -> dict:
    message = "显式 Responses 配置缺失、无效或与请求冲突；未调用模型"
    return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
            "error": message, "attempts": 0, "blocked_before_native": True,
            "fail_kind": "model_config",
            "error_envelope": {"category": "model_config", "scope": "request",
                               "retryable": False, "message": message}}


def _run_core_inprocess(key: str, task: str, qhash: str,
                        model_override: Optional[str] = None,
                        appid_override: Optional[str] = None,
                        session_id: str = "", user_id: str = "default",
                        memory_dir: str = "", tried_models: Optional[list] = None,
                        prior_attempts: Optional[list] = None, runtime_brief: str = "",
                        task_facts: Optional[dict] = None, capability_policy: Optional[dict] = None,
                        task_memory: Optional[dict] = None, project_context: Optional[dict] = None,
                        direction: str = "programming",
                        timeout_s: float = 0.0,
                        progress_cb: Optional[Callable[[dict], None]] = None,
                        cancel_check: Optional[Callable[[], bool]] = None,
                        execution_depth: str = "standard",
                        provider_binding: Optional[dict] = None) -> dict:
    """已 ctx 化的 core 家进程内直调 standalone adapter(跳过多余 _run_harness_isolated 隔离子进程·
    速度底线·用户 2026-09-01 确认"core家也跳隔离")。adapter 内部 _run_live 起 CLI 子进程仍隔离;
    省掉的是"重新 import legion_core/run_comparison 的中间隔离子进程"(实测 codex 19s→11s)。
    事件回流:设 harness_registry 的进程内 sink(thread-local)·adapter 的 _emit_harness_event
    直接回调 progress_cb·不写 stderr(过程透明不丢)。跑完置回 sink。
    """
    t0 = time.time()
    try:
        provider_binding = _explicit_provider_binding(key, provider_binding)
    except (ImportError, ValueError):
        return _provider_configuration_error(key)
    try:
        import harness_registry as _hr
    except Exception as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0, "error": f"harness_registry 不可用:{exc}"}
    adapter = _hr._STANDALONE_ADAPTERS.get(key)
    if adapter is None:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0, "error": f"无 standalone adapter:{key}"}
    # 跑前先查取消；运行中由 _run_live 持续检查同一请求令牌并回收其独立进程组。
    if cancel_check and cancel_check():
        return {"key": key, "status": "cancelled", "final": "", "elapsed": 0.0, "error": "用户停止", "cancelled": True}
    try:
        import legion_sandbox as _sandbox
        workdir = _sandbox.workdir(key, qhash, project_context, user_id)
    except (OSError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"project workspace unavailable:{type(exc).__name__}:{exc}"}
    appid = appid_override or os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID") or ""
    gateway = os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    if provider_binding:
        model_override, appid, gateway = (
            provider_binding["model"], provider_binding["api_key"], provider_binding["base_url"])
    try:
        ctx = _hr.HarnessCtx(
            model=model_override or "", appid=appid, gateway=gateway, direction=direction,
            execution_depth=execution_depth,
            session_id=session_id or f"{key}:{qhash}", user_id=user_id, memory_dir=memory_dir,
            tried_models=list(tried_models or []), prior_attempts=list(prior_attempts or []),
            runtime_brief=runtime_brief, workdir=workdir, task_facts=task_facts or {},
            capability_policy=capability_policy or {}, task_memory=task_memory or {},
            project_context=project_context or {}, peer_source_context="",
            stable_context=_stable_prelude(execution_depth),
            prompt_cache_key=_hr.prompt_cache_key(user_id, session_id or f"{key}:{qhash}", key),
            model_protocol="responses" if provider_binding else "")
        runtime_env = _runtime_context_env(runtime_brief, task_facts, capability_policy,
                                           task_memory, project_context, prior_attempts, key=key, qhash=qhash)
        ctx.context_refs = json.loads(runtime_env["MEYO_CONTEXT_REFS_JSON"])
        runtime_env.update({"MEYO_RUNTIME_INPROC": "1", "MEYO_TASK_DEADLINE": _runtime_deadline(timeout_s),
                            "MEYO_SESSION_ID": ctx.session_id, "MEYO_USER_ID": ctx.user_id,
                            "MEYO_WORKDIR": ctx.workdir, "MEYO_DIRECTION": direction,
                            "MEYO_EXECUTION_DEPTH": execution_depth,
                            "MEYO_TASK_IDEMPOTENCY_KEY": str((task_facts or {}).get("idempotency_key") or "")})
        if provider_binding:
            runtime_env.update(MEYO_UNIFIED_MODEL=model_override, MEYO_UNIFIED_APPID=appid,
                               AIGC_APPID=appid, MEYO_UNIFIED_GATEWAY=gateway,
                               MEYO_MODEL_PROTOCOL="responses", LEGION_CODEX_TRANSPORT="responses",
                               MEYO_FALLBACK_MODELS="[]")
    except Exception as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": round(time.time()-t0, 2), "error": f"HarnessCtx:{exc}"}
    # 设进程内事件 sink + per-call runtime env。后者由 harness_registry._run_live 合并到
    # adapter 启动的 native CLI，不改 server 全局 os.environ，因此并发用户/成员不会串 scope。
    _parent_env = dict(getattr(getattr(_hr, "_INPROC_RUNTIME_ENV", None), "values", {}))
    _parent_check = getattr(getattr(_hr, "_INPROC_CANCEL_CHECK", None), "check", None)
    _parent_cancelled = getattr(getattr(_hr, "_INPROC_CANCEL_CHECK", None), "cancelled", False)
    _parent_sink = getattr(getattr(_hr, "_INPROC_EVENT_SINK", None), "cb", None)
    _hb_act = {"x": "", "ts": 0.0}   # 最近真实动作(心跳带真内容·2026-09-07用户:越人话越透明)
    if progress_cb:
        def _hb_sink(ev):
            _a = _hb_act_from_event(ev)
            if _a:
                _hb_act.update(x=_a, ts=time.time())
            progress_cb(ev)
        try: _hr.set_inproc_event_sink(_hb_sink)
        except Exception: pass
    try:
        _hr.set_inproc_runtime_env(runtime_env)
        _hr.set_inproc_cancel_check(cancel_check)
    except Exception:
        pass
    # ★inproc 心跳(2026-09-02 SSE 丝滑度实测揪出:静默段 4s/7s/7.8s——inproc 家同步调 adapter
    #   期间没有任何事件·不像 _run_harness_isolated 有监视线程发"思考中"心跳)。补:inproc 执行期
    #   起轻量心跳线程·每 3s 发一个"思考中"心跳(纯 UX·不干预 harness)。adapter 有真事件时
    #   sink 会推 event·心跳照发也无妨(前端按时间线排·不覆盖)。daemon=True 随主流程结束。
    _hb_stop = threading.Event()
    if progress_cb is not None:
        def _hb_thread():
            _i = 0
            while not _hb_stop.wait(2.5):
                _i += 1
                try:
                    progress_cb({"agent": key, "t": "reason", "status": "running", "hb": True,
                                 "x": _hb_x(_hb_act, _i * 2.5)})
                except Exception:
                    pass
        threading.Thread(target=_hb_thread, daemon=True).start()
    try:
        result = adapter(qhash, task, ctx=ctx)
    except Exception as exc:
        result = {"final": "", "status": "error", "error": f"{key} 直调:{type(exc).__name__}:{str(exc)[:200]}"}
    finally:
        _hb_stop.set()
        try: _hr.set_inproc_event_sink(_parent_sink)
        except Exception: pass
        try: _hr.set_inproc_runtime_env(_parent_env)
        except Exception: pass
    try:
        _cancelled_during_run = bool(_hr.consume_inproc_cancelled())
        _hr.set_inproc_cancel_check(_parent_check)
        if hasattr(_hr, "_INPROC_CANCEL_CHECK"):
            _hr._INPROC_CANCEL_CHECK.cancelled = _parent_cancelled
    except Exception:
        _cancelled_during_run = False
    if _cancelled_during_run:
        result = {**(result if isinstance(result, dict) else {}), "status": "cancelled",
                  "error": "用户停止", "cancelled": True}
    if not isinstance(result, dict):
        result = {"final": str(result or ""), "status": "ok"}
    normalized_result: dict[str, Any] = dict(result)
    if getattr(ctx, "context_budget_report", None):
        normalized_result["context_budget"] = dict(ctx.context_budget_report)
    normalized_result.setdefault("workdir", workdir)
    normalized_result["elapsed"] = float(normalized_result.get("elapsed") or round(time.time()-t0, 2))
    return normalized_result


def _private_transport_output(event: dict) -> bool:
    """CLI transport 的未解析行只是私有日志，不能伪装成公开过程。"""
    return (event.get("t") == "harness_event" and event.get("protocol") == "cli"
            and event.get("kind") == "output")


def _run_external_inprocess(key: str, task: str, qhash: str,
                            model_override: Optional[str] = None,
                            appid_override: Optional[str] = None,
                            session_id: str = "", user_id: str = "default",
                            memory_dir: str = "", tried_models: Optional[list] = None,
                            prior_attempts: Optional[list] = None, runtime_brief: str = "",
                            task_facts: Optional[dict] = None, capability_policy: Optional[dict] = None,
                            task_memory: Optional[dict] = None, project_context: Optional[dict] = None,
                            direction: str = "programming",
                            timeout_s: float = 0.0,
                            progress_cb: Optional[Callable[[dict], None]] = None,
                            cancel_check: Optional[Callable[[], bool]] = None,
                            execution_depth: str = "standard",
                            provider_binding: Optional[dict] = None) -> dict:
    """external agent 进程内直调(跳过多余的 _run_harness_isolated 子进程·速度底线)。

    external_agent_registry.run 内部本就起自己的 worker/CLI 子进程(天生隔离)·不需要父层再套一层
    "重新 import 的隔离子进程"(那是老家 core 走 run_comparison 常量时才需要的换进程手段)。
    这里进程内构造 HarnessCtx(带全部下放:model/session/prior_attempts/brief/task_memory…)·
    直接调 harness_registry 的 external adapter·把它的事件转成 progress_cb。省一整层 Python 冷启动。
    workdir 统一到本单持久目录(和隔离层一致·产物落盘被收集)。
    """
    _parent_check = getattr(getattr(sys.modules.get("harness_registry"),
                                   "_INPROC_CANCEL_CHECK", None), "check", None)
    if callable(_parent_check) and cancel_check is not _parent_check:
        _child_check = cancel_check
        cancel_check = lambda: _parent_check() or bool(_child_check and _child_check())
    if timeout_s <= 0 or callable(_parent_check):
        # 外部 transport 没有空闲租约/中途取消接口；取消默认总墙钟后必须用已有 worker
        # 监护，不能把无限等待留在服务线程；借家继承的取消也必须经过同一监护。
        # 无继承取消的显式限时直调保持兼容。
        return _run_harness_isolated(
            key, task, qhash, timeout_s, model_override, appid_override,
            session_id=session_id, user_id=user_id, memory_dir=memory_dir,
            tried_models=tried_models, prior_attempts=prior_attempts,
            runtime_brief=runtime_brief, task_facts=task_facts,
            capability_policy=capability_policy, task_memory=task_memory,
            project_context=project_context, direction=direction,
            progress_cb=progress_cb, cancel_check=cancel_check,
            execution_depth=execution_depth,
            **({"provider_binding": provider_binding} if provider_binding is not None else {}))
    t0 = time.time()
    try:
        provider_binding = _explicit_provider_binding(key, provider_binding)
    except (ImportError, ValueError):
        return _provider_configuration_error(key)
    try:
        import harness_registry as _hr
    except Exception as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"harness_registry 不可用:{exc}"}
    # 编辑已有项目时优先使用 Project Context 明示的真实源码根；否则使用本单持久交付目录。
    # 这让 Aider 等编辑器型 Harness 真正在目标仓库工作，同时保持绿地交付隔离。
    try:
        import legion_sandbox as _sandbox
        workdir = _sandbox.workdir(key, qhash, project_context, user_id)
    except (OSError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"project workspace unavailable:{type(exc).__name__}:{exc}"}
    appid = appid_override or os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID") or ""
    gateway = os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    if provider_binding:
        model_override, appid, gateway = (
            provider_binding["model"], provider_binding["api_key"], provider_binding["base_url"])
    try:
        ctx = _hr.HarnessCtx(
            model=model_override or "", appid=appid, gateway=gateway, direction=direction,
            execution_depth=execution_depth,
            session_id=session_id or f"{key}:{qhash}", user_id=user_id, memory_dir=memory_dir,
            tried_models=list(tried_models or []), prior_attempts=list(prior_attempts or []),
            runtime_brief=runtime_brief, workdir=workdir, task_facts=task_facts or {},
            capability_policy=capability_policy or {}, task_memory=task_memory or {},
            project_context=project_context or {}, peer_source_context="",
            stable_context=_stable_prelude(execution_depth),
            prompt_cache_key=_hr.prompt_cache_key(user_id, session_id or f"{key}:{qhash}", key),
            model_protocol="responses" if provider_binding else "")
        deadline = _runtime_deadline(timeout_s)
    except Exception as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": round(time.time()-t0, 2),
                "error": f"HarnessCtx 构造失败:{exc}"}
    # external adapter 的事件经 _relay_external_event 写 stderr 前缀·进程内直调时改成直接转 progress_cb。
    # adapter 内部用 event_cb=_relay_external_event(写 stderr)·我们这里拿不到它——改为直接调
    # external_agent_registry.run 并传 event_cb=progress_cb(进程内·事件直达·无需跨进程 stderr 中转)。
    try:
        import external_agent_registry as _ext
    except Exception as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": round(time.time()-t0, 2),
                "error": f"external_agent_registry 不可用:{exc}"}
    env = {"AIGC_APPID": appid, "MEYO_UNIFIED_APPID": appid, "MEYO_UNIFIED_GATEWAY": gateway,
           # external transport 由 registry 自己起进程，不经过 harness_registry._run_live；
           # 因此直接显式下放同一可靠作用域，保证工具学习与 core 家一致且不读 server 全局 env。
           "MEYO_SESSION_ID": ctx.session_id, "MEYO_USER_ID": ctx.user_id,
           "MEYO_WORKDIR": ctx.workdir, "MEYO_TASK_DEADLINE": str(deadline),
           "MEYO_DIRECTION": direction, "MEYO_EXECUTION_DEPTH": execution_depth,
           "MEYO_TASK_IDEMPOTENCY_KEY": str((task_facts or {}).get("idempotency_key") or "")}
    if provider_binding:
        env.update(MEYO_UNIFIED_MODEL=model_override, MEYO_MODEL_PROTOCOL="responses",
                   LEGION_CODEX_TRANSPORT="responses", MEYO_FALLBACK_MODELS="[]")
    try:
        env.update(_runtime_context_env(runtime_brief, task_facts, capability_policy,
                                        task_memory, project_context, prior_attempts, key=key, qhash=qhash))
        ctx.context_refs = json.loads(env["MEYO_CONTEXT_REFS_JSON"])
    except (OSError, TypeError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"runtime context unavailable:{type(exc).__name__}:{exc}"}
    runtime_context = {
        "user_id": user_id, "session_id": session_id or qhash, "memory_dir": memory_dir,
        "tried_models": list(tried_models or []), "prior_attempts": list(prior_attempts or []),
        "runtime_brief": runtime_brief, "task_facts": task_facts,
        "capability_policy": capability_policy, "task_memory": task_memory,
        "project_context": project_context,
        "stable_context": ctx.stable_context, "prompt_cache_key": ctx.prompt_cache_key,
        "tool_cli": os.path.join(HERE, "legion_tool.py"), "workdir": workdir,
    }
    _hb_act2 = {"x": "", "ts": 0.0}
    def _ev(event):
        if _private_transport_output(event):
            return
        if progress_cb:
            _a = _hb_act_from_event(event)
            if _a:
                _hb_act2.update(x=_a, ts=time.time())
            try: progress_cb(event)
            except Exception: pass
    # ★external inproc 心跳(同 core inproc·2026-09-02 SSE 丝滑度:worker 静默期无事件→界面卡住感)
    # 2026-09-07:文案带最近真实动作(工具/输出)·hb标记让前端稳定单行复用。
    _hb_stop = threading.Event()
    if progress_cb is not None:
        def _hb_thread():
            _i = 0
            while not _hb_stop.wait(2.5):
                _i += 1
                try:
                    progress_cb({"agent": key, "t": "reason", "status": "running", "hb": True,
                                 "x": _hb_x(_hb_act2, _i * 2.5)})
                except Exception:
                    pass
        threading.Thread(target=_hb_thread, daemon=True).start()
    # 失败历史文本回流(external adapter 走 _legion_prompt·同 harness_registry 里的拼法)。
    # external 直调绕过 core adapter 的 sink 生命周期，因此在构造 prompt 时临时接上同一回调，
    # 让发送前压缩事件进入 SSE，而不是掉到 server stderr。
    try:
        adapter_capabilities = _hr._external_adapter_capabilities(key)
    except Exception as exc:
        # 清单读取失败不同于未声明能力；不能回退成一段默认“工具已就绪”的任务。
        _hb_stop.set()
        return {"key": key, "status": "error", "final": "",
                "elapsed": round(time.time() - t0, 2),
                "error": f"adapter capability context unavailable:{type(exc).__name__}",
                "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                   "retryable": False,
                                   "code": "adapter_capabilities_unavailable"}}
    runtime_context["adapter_capabilities"] = adapter_capabilities
    failure_preface = ""
    try:
        if progress_cb:
            _hr.set_inproc_event_sink(_ev)
        failure_preface = _hr._prior_attempts_prelude(list(prior_attempts or []))
        prompt = _hr._legion_prompt(
            failure_preface + task, ctx, adapter_capabilities=adapter_capabilities)
    except ValueError as exc:
        _hb_stop.set()
        return {"key": key, "status": "error", "final": "",
                "elapsed": round(time.time() - t0, 2),
                "error": f"native context cannot be represented within budget:{exc}",
                "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                   "retryable": False, "code": "context_budget_unavailable"}}
    except Exception as exc:
        # A formatting failure is not permission to silently discard all
        # capability context. Structured project facts still reach ext.run.
        adapter_note = ("ADAPTER_CAPABILITIES_JSON\n"
                        + json.dumps(adapter_capabilities, ensure_ascii=False)
                        if adapter_capabilities else "")
        text_note = (_hr._LEGION_TEXT_ACCESS.format()
                     if adapter_capabilities.get("tools") is False else "")
        prompt = "\n\n".join(part for part in (
            adapter_note, text_note, ctx.stable_context, failure_preface, task) if part)
        runtime_context["context_render_error"] = {"type": type(exc).__name__}
        _ev({"agent": key, "t": "context_warning", "error_type": type(exc).__name__,
             "x": "上下文格式化异常；已保留稳定能力与原任务，结构化现场仍随请求传递。"})
    finally:
        try:
            _hr.set_inproc_event_sink(None)
        except Exception:
            pass
    try:
        remaining = min(timeout_s, deadline - time.monotonic()) if deadline else timeout_s
        if remaining <= 0:
            raise TimeoutError("caller runtime budget exhausted")
        result = _ext.run(key, prompt, workdir=workdir, env=env, model=model_override or "",
                          session_id=session_id or qhash, runtime_context=runtime_context,
                          timeout_s=remaining, event_cb=_ev)
    except Exception as exc:
        _hb_stop.set()
        return {"key": key, "status": "error", "final": "", "elapsed": round(time.time()-t0, 2),
                "error": f"external {key} 直调失败:{type(exc).__name__}:{str(exc)[:200]}", "workdir": workdir}
    finally:
        _hb_stop.set()
    result.setdefault("workdir", workdir)
    if getattr(ctx, "context_budget_report", None):
        result["context_budget"] = dict(ctx.context_budget_report)
    result["elapsed"] = float(result.get("elapsed") or round(time.time()-t0, 2))
    return result


def _run_harness_isolated(key: str, task: str, qhash: str, timeout_s: float,
                          model_override: Optional[str] = None,
                          appid_override: Optional[str] = None,
                          session_id: str = "",
                          user_id: str = "default",
                          memory_dir: str = "",
                          tried_models: Optional[list[str]] = None,
                          prior_attempts: Optional[list[dict]] = None,
                          runtime_brief: str = "",
                          task_facts: Optional[dict] = None,
                          capability_policy: Optional[dict] = None,
                          task_memory: Optional[dict] = None,
                          project_context: Optional[dict] = None,
                          direction: str = "programming",
                          cancel_check: Optional[Callable[[], bool]] = None,
                          progress_cb: Optional[Callable[[dict], None]] = None,
                          execution_depth: str = "standard",
                          provider_binding: Optional[dict] = None) -> dict:
    """在独立进程组里跑一家,用【无进展心跳】而非总墙钟判卡死(不留孤儿 CLI)。

    cancel_check(军团自审 P0):无参可调对象·返 True 表示用户已喊停这一单/这家。监控循环每 tick
    检查它·一旦为真就走和超时同款的安全进程组回收(SIGTERM 宽限→SIGKILL),但状态标 cancelled
    (不是 timeout)·保留宽限期 partial。取消 API 只设令牌·这里的拥有者线程才真动进程(避免竞态)。

    model/appid override 通过 env 传下去 —— 子进程重新 import run_comparison 时,
    UNIFIED_MODEL/APPID 的 os.environ 覆盖才会真生效(常量在父进程已定死,故必须换进程)。

    卡死检测:后台线程流式读 stdout/stderr,每有一行就刷新 last_activity。主循环发现
    连续 _IDLE_TIMEOUT 秒零输出即判卡死,杀进程组并把"卡在无输出状态"回流(不是判"跑太久")。
    """
    import subprocess
    import signal
    try:
        provider_binding = _explicit_provider_binding(key, provider_binding)
    except (ImportError, ValueError):
        return _provider_configuration_error(key)
    payload = base64.urlsafe_b64encode(
        json.dumps({"key": key, "task": task, "qhash": qhash}).encode()).decode()
    # This is a transport cutoff, not a task/context budget. Keep legacy small
    # argv calls; large payloads use the same complete envelope on owned stdin.
    payload_on_stdin = len(payload) > 32000
    env = dict(os.environ)
    # 从启动前计时，传绝对 monotonic 截止点；子进程重试不能重置调用方预算。
    try:
        deadline = _runtime_deadline(timeout_s)
    except (TypeError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"runtime context unavailable:{type(exc).__name__}:{exc}"}
    env["MEYO_TASK_DEADLINE"] = str(deadline)
    env["MEYO_RUNTIME_INPROC"] = "0"
    # 全家统一拿到 GitHub 能力现场:只下放授权状态和代理地址，不把 token 塞进子进程/prompt。
    # 真搜索/下载统一走 legion_tool，由它在父层读取当前用户 gh keyring；这样 cc/codex/auto 等
    # 不论是否原生支持 MCP 都能用同一入口，且未授权时能如实告诉用户运行 gh auth login。
    try:
        import github_access as _github
        env = _github.shared_environment(env)
    except Exception:
        pass
    if model_override:
        env["MEYO_UNIFIED_MODEL"] = model_override
    if appid_override:
        env["MEYO_UNIFIED_APPID"] = appid_override
    if provider_binding:
        env.update(MEYO_UNIFIED_MODEL=provider_binding["model"],
                   MEYO_UNIFIED_APPID=provider_binding["api_key"],
                   AIGC_APPID=provider_binding["api_key"],
                   MEYO_UNIFIED_GATEWAY=provider_binding["base_url"],
                   MEYO_MODEL_PROTOCOL="responses", LEGION_CODEX_TRANSPORT="responses")
    # 权限下放(2026-08-28·先 penguin 一家):把 session/记忆单元 + 本单已试模型 + 前几轮
    # 结构化结果通过 env 下放给 harness,让已 ctx 化的 adapter(HarnessCtx.from_env)拿到,
    # 自己决定续 session/避开坏模型/看着失败换招。老家(未 ctx 化)不读这些 env,行为零变。
    # 任务方向下放(军团自审 penguin/auto P0):旧版 direction 只在父层选模型用,没设进子进程
    # env→HarnessCtx.direction 恒为 programming→harness 的工具提示/fallback 候选按错误方向生成
    # (视觉/长调研任务首轮选对·失败后又退回编程模型)。设 MEYO_DIRECTION 让 harness 知道自己
    # 在做什么方向、选对口 fallback。
    if direction:
        env["MEYO_DIRECTION"] = direction
    env["MEYO_EXECUTION_DEPTH"] = execution_depth
    env["MEYO_SESSION_ID"] = session_id or f"{key}:{qhash}"
    # user 单元 + 记忆目录下放:harness 用 user_id/memory_dir 自己续跨会话长期记忆
    # (依据 hermes run_agent.py:464,492 AIAgent 真接 session_id/user_id·记忆由 harness 自管)。
    # 缺省 "default"/空目录 → 未接记忆的家零变。
    if user_id:
        env["MEYO_USER_ID"] = user_id
    if memory_dir:
        env["MEYO_MEMORY_DIR"] = memory_dir
    if tried_models:
        env["MEYO_TRIED_MODELS"] = json.dumps(tried_models, ensure_ascii=False)
    # 自主权下放(2026-08-28·全家通用):把 token/迭代/fallback/完成契约作为 context 交给 harness
    # 自己按任务调(不写死·harness 拿到自己那份自己改)。默认值是"给足/不限"(治 hermes adapter
    # 曾写死 max_tokens=8192 太小·reasoning 思考链吃光正文截成残渣),不是父层规则:
    #   · MAX_TOKENS 默认 0=不写死上限,让模型自己用足(可 env LEGION_HARNESS_MAX_TOKENS 调)
    #   · FALLBACK_MODELS = 本方向候选池(harness 主模型退化时自己换·接各家自带 fallback 自愈)
    #   · DELIVERY_CONTRACT = 完成契约 context,让 harness 产出后自己复检"是不是给用户看的成品"
    _mx = os.environ.get("LEGION_HARNESS_MAX_TOKENS", "").strip()
    if _mx:
        env["MEYO_MAX_TOKENS"] = _mx
    if provider_binding:
        env["MEYO_FALLBACK_MODELS"] = "[]"
    else:
        try:
            _fb = _fallback_candidates(env.get("MEYO_DIRECTION") or "programming",
                                       exclude={model_override} | set(tried_models or []))
            if _fb:
                env["MEYO_FALLBACK_MODELS"] = json.dumps(_fb[:4], ensure_ascii=False)
        except Exception:
            pass
    env["MEYO_DELIVERY_CONTRACT"] = _DELIVERY_CONTRACT
    # 稳定能力前置 + 会话缓存槽：两条都由父层单一事实源生成，隔离子进程只消费。
    env["MEYO_STABLE_CONTEXT"] = _stable_prelude(execution_depth)
    try:
        import harness_registry as _hr_cache
        _cache_key = _hr_cache.prompt_cache_key(user_id, session_id, key)
        if _cache_key:
            env["MEYO_PROMPT_CACHE_KEY"] = _cache_key
    except Exception:
        pass
    try:
        env.update(_runtime_context_env(runtime_brief, task_facts, capability_policy,
                                        task_memory, project_context, prior_attempts, key=key, qhash=qhash))
    except (OSError, TypeError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"runtime context unavailable:{type(exc).__name__}:{exc}"}
    env["MEYO_TASK_IDEMPOTENCY_KEY"] = str((task_facts or {}).get("idempotency_key") or "")
    # 统一准备每家真实可写 workdir + 真实可读 peer 源码入口。隔离 sandbox 家拿 workdir 内镜像，
    # 其余家拿已核验的仓库绝对路径；不再给一张在该家环境里打不开的“纸地图”。路径根统一走
    # legion_paths，测试重定向后绝不碰用户真目录。
    try:
        import legion_sandbox as _sandbox
        _wd = _sandbox.workdir(key, qhash, project_context, user_id)
        env["MEYO_WORKDIR"] = _wd
        env["MEYO_PEER_SOURCE_CONTEXT"] = _sandbox.prepare_peer_sources(
            key, _wd, preserve_project=bool((project_context or {}).get("repo_root") or
                                           (project_context or {}).get("workspace_root")))
    except (OSError, ValueError) as exc:
        return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                "error": f"project workspace unavailable:{type(exc).__name__}:{exc}"}
    kw = {}
    if os.name != "nt":
        kw["start_new_session"] = True  # 自成进程组,便于超时 killpg 整组
    # 用当前运行服务的同一个解释器(sys.executable)——服务是 python3.12 起的,子进程也 3.12。
    # 硬约束:别写死 "python3"(别人机器上可能是 3.9/3.14,会触发 auto 启动自检 SystemExit(97))。
    # Never inherit the service's stdin. The large-payload pipe belongs only to
    # this worker and is closed at EOF; downstream native CLI stdin stays native.
    proc = subprocess.Popen([sys.executable, _WORKER, "-" if payload_on_stdin else payload], env=env,
                            stdin=subprocess.PIPE if payload_on_stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, **kw)
    # 崩溃安全孤儿回收(2026-09-02 根修):worker start_new_session 自立进程组,server 被
    # SIGKILL(137)时守护的 killpg(server pgid) 够不到它 → 磁盘注册表登记,sweep 按代回收。
    try:
        import legion_proc_registry as _preg
        _preg.register(proc.pid, pgid=proc.pid, owner=os.getpid(),
                       comm=os.path.basename(sys.executable),
                       meta={"kind": "harness-worker", "key": key})
    except Exception:
        pass

    from harness_registry import RuntimeLease
    idle_limit = _IDLE_TIMEOUT
    lease = RuntimeLease(idle_limit, deadline)

    out_lines: list[str] = []
    err_buf: list[str] = []
    state = {"last": time.time()}
    lock = threading.Lock()
    # 事件回流:worker 透传上来的过程事件带此前缀(和 harness_registry._LEGION_EV_PREFIX 一致)。
    _EV_PREFIX = "\x1e[legion-ev]"

    def _pump(stream, sink, is_err=False):
        try:
            for ln in iter(stream.readline, ""):
                # worker 定时 [hb] 只能证明线程还活着，不能给原生任务无限续租。
                if ln.startswith("[hb] "):
                    continue
                if is_err and _EV_PREFIX in ln:
                    try:
                        ev = json.loads(ln.split(_EV_PREFIX, 1)[1].strip())
                        if not isinstance(ev, dict):
                            continue
                        if ev.get("t") == "runtime_activity" or _private_transport_output(ev):
                            lease.observe(ev if ev.get("t") == "runtime_activity" else None)
                            with lock:
                                state["last"] = time.time()
                            continue  # 私有活动通道，禁止透传 UI/公开 tool_trace。
                        if not ev.get("hb"):
                            lease.observe()
                            with lock:
                                state["last"] = time.time()
                        _a = _hb_act_from_event(ev)
                        if _a:
                            with lock:
                                state["act"] = {"x": _a, "ts": time.time()}
                        if progress_cb:
                            progress_cb(ev)
                    except Exception:
                        pass
                    continue
                # 原文供结果解析，不再写进公开心跳摘要。
                lease.observe()
                with lock:
                    state["last"] = time.time()
                with lock:
                    sink.append(ln)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_lines), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_buf, True), daemon=True)
    t_out.start(); t_err.start()

    input_thread = None
    input_state = {"complete": not payload_on_stdin, "error": ""}
    if payload_on_stdin:
        def _send_payload():
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
                input_state["complete"] = True
            except Exception as exc:
                input_state["error"] = type(exc).__name__
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        # A worker can stall before it reads stdin. Keep the existing parent
        # cancellation/lease loop running rather than blocking it on pipe.write.
        input_thread = threading.Thread(
            target=_send_payload, name=f"worker-input-{proc.pid}", daemon=True)
        input_thread.start()

    started = time.time()
    kill_reason = None
    # 失败原因分类(军团反思⑥"别混成一句"):把"为什么被切"作为结构化字段带出去,让接力家/前端
    # 能程序化区分【卡死 vs 跑太久 vs 被信号杀 vs 用户取消】,而不是都归成一句 status:timeout。
    #   idle_stall=既无输出又无CPU疑似卡死 · wall_timeout=总时长兜底 · user_cancel=用户主动停 ·
    #   signal_killed=进程被外部信号杀(rc<0·见下方进程退出分支)。
    _fail_kind = None
    try:
        _pgid = os.getpgid(proc.pid)
    except Exception:
        _pgid = None
    # 每 _CPU_SAMPLE 秒采一次进程组CPU;若这段内CPU增量够大=在烧CPU=在推进→刷新last_activity。
    # 这样"无stdout但CPU忙"的auto/codex多轮检索不会被误判卡死;只有既无输出又不烧CPU才是真卡死。
    _CPU_SAMPLE = min(20.0, max(0.1, idle_limit / 3))
    _cpu_last = (_pgroup_cpu_seconds(_pgid) if _pgid else -1.0)
    _cpu_last_ts = time.time()
    cancelled = False                                # 用户主动取消(区别于卡死超时·状态标 cancelled)
    while True:
        rc = proc.poll()
        if rc is not None:
            break                                   # 进程正常退出
        # 用户取消(军团自审 P0):每 tick 查取消令牌·喊停就走和超时同款的安全进程组回收,
        # 但标 cancelled 而非 timeout。放在最前——用户意图优先于"跑得久/卡死"判断。
        if cancel_check is not None:
            try:
                if cancel_check():
                    cancelled = True
                    kill_reason = "用户主动停止"
                    break
            except Exception:
                pass
        now = time.time()
        # 周期性CPU采样:在动就刷新活动时间(独立于stdout)
        if _pgid is not None and (now - _cpu_last_ts) >= _CPU_SAMPLE:
            cpu_now = _pgroup_cpu_seconds(_pgid)
            if cpu_now >= 0 and _cpu_last >= 0 and (cpu_now - _cpu_last) > _CPU_ACTIVE_DELTA:
                with lock:
                    state["last"] = now          # CPU在动=在推进
                lease.observe()
            if cpu_now >= 0:
                _cpu_last = cpu_now
            _cpu_last_ts = now
        with lock:
            idle = now - state["last"]
        # ★ UX 心跳(用户 2026-09-01 loop 真测揪出:SSE 过程流有 11.86s 静默空档——harness 调模型
        #   思考期没有任何事件·界面卡住近12s·用户以为死了·违反"过程实时可感·即使慢也不让干等")。
        #   静默>3s 时每~3s 发一个"思考中"心跳给前端(纯 UX 事件·不进 out/err·不干预 harness 答题·
        #   context not control)。让用户看到"它在动"。有真实输出时 state[last] 会刷新→idle 归零→不发。
        if progress_cb is not None and idle >= 2.5:
            _last_hb = state.get("hb", 0.0)
            if now - _last_hb >= 2.5:
                state["hb"] = now
                try:
                    with lock:
                        _act = dict(state.get("act") or {})
                    progress_cb({"agent": key, "t": "reason", "status": "running", "hb": True,
                                 "x": _hb_x(_act, int(now - started))})
                except Exception:
                    pass
        _expired = lease.expired()
        if _expired:
            _fail_kind = _expired
            kill_reason = ("调用方显式执行预算已用完" if _expired == "runtime_budget" else
                           f"连续 {idle_limit:g}s 无真实活动且原生调用预算已结束")
            break
        time.sleep(0.1)

    if kill_reason is not None:
        # 军团6家共识(2026-08-28·deepseek点名):别直接 SIGKILL 丢弃 partial output——先 SIGTERM
        # 给 worker 15s 宽限落盘中间产物/吐出已有 final,只有宽限后仍不退才 SIGKILL。这样"被切"
        # 也能保住已跑出来的东西(worker 若捕获 SIGTERM 会 finalize)·不是从零白卷。
        # 安全防护:killpg 前必须确认目标进程组 ≠ 服务自己的进程组,否则会杀掉整个军团服务。
        try:
            child_pgid = os.getpgid(proc.pid) if os.name != "nt" else None
            my_pgid = os.getpgid(0) if os.name != "nt" else None
            _own_group = (os.name != "nt" and child_pgid and child_pgid != my_pgid
                          and child_pgid == proc.pid)
            # 1) 先 SIGTERM(宽限)
            try:
                if _own_group and child_pgid is not None:
                    os.killpg(child_pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                pass
            # 2) 等最多 15s 让它自己落盘/退出(期间 pump 线程继续收它吐的 partial/final)
            _grace_deadline = time.time() + 15.0
            while time.time() < _grace_deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
            # 3) 仍没退 → SIGKILL 最后保险
            if proc.poll() is None:
                if _own_group and child_pgid is not None:
                    os.killpg(child_pgid, signal.SIGKILL)
                else:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    t_out.join(timeout=3); t_err.join(timeout=3)

    # 崩溃安全扫尾(2026-09-02 根修):worker 已退出,但①同组可能还有原生 CLI 残留(worker
    # 被单独 SIGKILL(rc-9)时同组孩子不死)②它名下自立进程组的 external CLI 无人收。
    # 三步兜底:killpg 同组扫尾 → kill_owned_by 收登记组 → 注销自己的登记。全 try·不影响返回值。
    try:
        if _pgid is not None and os.name != "nt":
            try:
                os.killpg(_pgid, signal.SIGKILL)
            except Exception:
                pass
        try:
            import legion_proc_registry as _preg
            _preg.kill_owned_by(proc.pid)
            _preg.unregister(proc.pid)
        except Exception:
            pass
    except Exception:
        pass
    if input_thread is not None:
        input_thread.join(timeout=3)
    with lock:
        out = "".join(out_lines)
        errout = "".join(err_buf)

    if kill_reason is not None:
        # 军团共识:被切也别白卷——SIGTERM 宽限期 worker 若吐了 partial JSON/final,保留它。
        # 先扫 out 末尾有没有 worker 协议 JSON(它自己 finalize 落的);有 final 就当"部分交付"
        # 回传(status=timeout 仍触发换招重试·但带上已跑出的 partial 作 context·不是从零)。
        # 协议校验(2026-09-20 修乱码气泡):只认含 "final" 或 "status" 键的 worker 协议行——
        # 被杀的 cc/codex CLI 半路死的 stdout 末行是 stream-json 噪声帧({"type":"system",
        # "subtype":"thinking_tokens"...}·没有 final 键),旧逻辑见 { 就当 partial 整坨收,
        # 11378 字符 CLI 内部帧被当"成员答案"落账 → 前端渲染出乱码气泡。
        _partial = ""
        _partial_result = {}
        # worker 只用 LF 分帧；U+0085/U+2028/U+2029 可以是 JSON 字符串正文。
        for _ln in reversed(out.strip().split("\n")):
            _ln = _ln.strip()
            if _ln.startswith("{"):
                try:
                    _d = json.loads(_ln)
                    if not isinstance(_d, dict):
                        continue
                    # 协议校验（2026-09-20 修乱码气泡）：只认含 final/status 的 worker 协议行——
                    # 被杀 CLI 的 stdout 末行可能是 stream-json 噪声帧（无这些键），不误当 partial。
                    if "final" not in _d and "status" not in _d:
                        continue
                    _partial_result = _d
                    _partial = str(_d.get("final") or "")
                    break
                except Exception:
                    continue
        # 用户取消 vs 卡死超时:状态分开(军团自审 P0)。取消是用户意图·不是失败·绝不触发换模型
        # 自愈/接力(run_one/dispatch 会据 status==cancelled 停手·不把"用户喊停"当"该重试的故障")。
        _parent_error = ("用户停止" if cancelled else f"已切进程:{kill_reason}")
        _parent_error += "(保留了部分产出)" if _partial else ""
        if _partial_result.get("error"):
            _parent_error += "\n" + str(_partial_result["error"])
        # 子信封是原始证据，不能让它覆盖父进程已经发生的取消/超时。
        if _partial_result.get("error_envelope"):
            _native_facts = (_partial_result.get("native_facts")
                             if isinstance(_partial_result.get("native_facts"), dict) else {})
            _partial_result["native_facts"] = {
                **_native_facts, "worker_error_envelope": _partial_result["error_envelope"]}
        import error_envelope as _stop_errors
        _parent_status = "cancelled" if cancelled else "timeout"
        _parent_envelope = _stop_errors.normalize(
            _parent_status, _parent_error,
            fail_kind="user_cancel" if cancelled else (_fail_kind or "timeout")).to_dict()
        if not cancelled:
            _parent_envelope["scope"] = "harness"
        # 保留 worker 已落出的结构化现场；父进程的取消/超时最后覆盖，绝不继承子结果的 ok。
        return {**_partial_result,
                "workdir": str(_partial_result.get("workdir") or _partial_result.get("cwd")
                               or env.get("MEYO_WORKDIR") or ""),
                "status": _parent_status, "final": _partial,
                "ok": False, "cancelled": cancelled,
                "error": _parent_error,
                "error_envelope": _parent_envelope,
                "fail_kind": "user_cancel" if cancelled else (_fail_kind or "timeout"),
                "partial": bool(_partial) or bool(_partial_result.get("partial"))}

    if not input_state["complete"]:
        return {"status": "err:worker_input_failed", "final": "",
                "error": "worker payload delivery failed: " + (input_state["error"] or "incomplete")}
    for ln in reversed(out.strip().split("\n")):  # 与 partial 对称，不能按 Unicode 分隔符拆帧
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                return json.loads(ln)
            except Exception:
                continue
    return {"status": f"err:worker_no_json:rc{proc.returncode}", "final": "",
            "error": (errout or "")[-300:]}


def _appid_for_model(model: str) -> Optional[str]:
    """从 models.json 找某模型可用的第一个 appid(用户固定模型时配对 appid)。
    2026-09-10 服务器别名制:models.json 里存的是稳定别名(appid-xxxx),必须过
    model_registry._resolve_alias 解析回真实值,否则会把别名当 Bearer 发出去。
    缺授权(未注入 env)→ None,该配对如实不可用。"""
    if not model:
        return None
    d = _load_model_router()
    try:
        import model_registry as _mr
        for direction in ("programming", "vision_understanding", "general_chat_high_coverage"):
            for c in (d.get(direction) or {}).get("candidates", []):
                if c.get("model") == model and c.get("appids"):
                    for _a in c["appids"]:
                        _r = _mr._resolve_alias(str(_a))
                        if _r:
                            return _r
        return None
    except ImportError:
        # model_registry 不可导入(极端隔离环境):宁缺勿假——返回 None,
        # 绝不返回未解析的别名(发别名当 Bearer 比没 appid 更糟)。
        return None


def _build_process_summary(actions: list, elapsed: float, model: str,
                           artifacts: list | None = None) -> dict:
    """把已收集的工具动作(_actions)提炼成"我做了什么"过程卡·沉淀进交付。

    军团自评暴露:评委一致给 process_transparency 低分(hermes 4.5)——只给成品不展示过程;
    codex 甚至 final 截断在"进入运行态验证..."。根病=过程事件是实时流·任务结束就没了·
    用户回看交付看不到过程=黑盒。这里把已有的 actions(工具名/成败/耗时/摘要)沉淀成
    结构化过程卡随交付返回,让"军团干了什么"可回看。纯 context 沉淀·不改答题逻辑·不加门。
    """
    steps = []
    tool_count = 0          # 真正的工具调用(tool_end/timeout)
    ok_count = 0
    step_count = 0          # 过程步(harness_step:各家 adapter 真实吐的步骤)
    urls: list = []         # 访问过的网页(可点·头号纲领要列出来)
    for a in (actions or []):
        _at = a.get("t")
        if _at == "tool_started":
            continue  # 用 tool_end/timeout 作为一步落点·避免与 started 重复计
        if _at == "harness_step":
            # 军团 deepseek 首脑复核:harness_step 是各家真实吐的过程步(带工具文本+URL)。
            step_count += 1
            for u in (a.get("urls") or []):
                if u and u not in urls:
                    urls.append(u)
            steps.append({"tool": "步骤", "ok": None, "ms": None,
                          "brief": (a.get("brief") or "")[:80]})
            continue
        tool = a.get("tool") or "工具"
        ok = a.get("ok")
        tool_count += 1
        if ok:
            ok_count += 1
        steps.append({"tool": tool, "ok": ok, "ms": a.get("ms"),
                      "brief": (a.get("brief") or "")[:80]})
    arts = artifacts or []
    # headline 说真话(deepseek D1):有工具调用就报工具数·没有就报过程步数·别机械拼"调用0次工具"。
    if tool_count > 0:
        _act_str = f"调用 {tool_count} 次工具({ok_count} 成功)"
    elif step_count > 0:
        _act_str = f"{step_count} 步过程"
    else:
        _act_str = "直接推理产出"
    if urls:
        _act_str += f"·访问 {len(urls)} 个网页"
    return {
        "steps": steps[-30:],
        "tool_calls": tool_count,
        "tool_ok": ok_count,
        "process_steps": step_count,
        "urls": urls[:20],
        "artifacts_count": len(arts),
        "elapsed": elapsed,
        "model": model,
        "headline": (f"{model} 用时 {round(elapsed)}s·{_act_str}"
                     f"·产出 {len(arts)} 个文件"),
    }


# ── 单家执行(含全过程事件回调 + 网关自愈换模型重试 + 用户指定模型) ──────────
def _native_budget_exit(result):
    """只识别原生机器原因；不从 final、计数或普通 incomplete 猜测。"""
    result = result.get("native_facts") or {}
    return (re.fullmatch(r'max_iterations_reached(?:\(\d+/\d+\))?',
                        str(result.get('turn_exit_reason') or '')) is not None
            and not any(result.get(k) for k in
                        ('failed', 'interrupted', 'error', 'failure_reason')))


def _native_completed_cleanly(result: dict, *, allow_partial: bool = False) -> bool:
    """Native lifecycle evidence, not a judgment about the delivered content."""
    facts = result.get("native_facts")
    if not isinstance(facts, dict):
        return False
    terminal = facts.get("turn_result")
    terminal_type = terminal.get("type") if isinstance(terminal, dict) else None
    rc = facts.get("process_returncode")
    statuses = {"ok", "done", "completed", "success"}
    if allow_partial:
        statuses.add("partial")
    return (
        result.get("status") in statuses
        and type(rc) is int and rc == 0
        and not result.get("error")
        and not (isinstance(result.get("error_envelope"), dict) and result["error_envelope"])
        and not any(facts.get(k) for k in ("failed", "error", "failure_reason", "interrupted"))
        and not _native_budget_exit(result)
        and facts.get("completed") is not False
        and facts.get("turn_status") in (None, "completed")
        and terminal_type in (None, "turn.completed")
        and ((facts.get("turn_status") == "completed" and terminal_type == "turn.completed")
             or facts.get("completed") is True))


def _native_terminal_message_absent(result: dict | None) -> dict:
    """Project a bound missing message separately from failure and author intent."""
    if (not isinstance(result, dict) or str(result.get("final") or "").strip()
            or result.get("fail_kind")
            or not _native_completed_cleanly(result, allow_partial=True)):
        return {}
    facts = result["native_facts"]
    message = facts.get("completion_message")
    if (not isinstance(message, dict) or message.get("state") != "absent"
            or message.get("source") != "codex_task_complete_event"
            or not all(isinstance(message.get(k), str) and message[k].strip()
                       for k in ("thread_id", "turn_id"))
            or "last_agent_message" not in message
            or message["last_agent_message"] not in (None, "")):
        return {}
    checkpoint = result.get("native_checkpoint")
    if checkpoint is not None and (
            not isinstance(checkpoint, dict) or checkpoint.get("kind") != "codex_thread"
            or checkpoint.get("session_id") != message["thread_id"]):
        return {}
    writes = result.get("checkpoint_writes")
    saved = bool(checkpoint and isinstance(writes, dict) and writes.get("request") is True
                 and not result.get("checkpoint_storage_error")
                 and not result.get("native_resume_error"))
    observation = {**message, "checkpoint_saved": saved}
    last_output = facts.get("last_message_output")
    if isinstance(last_output, dict):
        observation["last_message_output"] = dict(last_output)
    return observation


def _native_terminal_message_context(observations: dict) -> str:
    """Only public runtime facts; the original author decides what work remains."""
    view = {}
    for task_id, observation in observations.items():
        row = dict(observation)
        output = row.get("last_message_output")
        if isinstance(output, dict):
            output = dict(output)
            text = str(output.get("text") or "")
            output["text"] = text[:1500]
            output.setdefault("text_char_count", len(text))
            output["text_truncated"] = bool(output.get("text_truncated") or len(text) > 1500)
            row["last_message_output"] = output
        view[task_id] = row
    return (
        "\n【本轮原生终态事实，不是作者的续作请求】"
        "原生进程正常退出，但本轮公开 task_complete 的 last_agent_message 为空。"
        "last_message_output 只保留 CLI 输出文件中的过程记录，不是本轮最终答复。"
        "这不证明作品完成，也不表示模型或工具全部失败；原任务、原生会话和已落盘产物仍保留。"
        "请结合原目标、已有作品和真实执行记录，自主判断剩余工作及如何继续。\n"
        + json.dumps(view, ensure_ascii=False))


def _native_continuation_assignment(key: str, previous: dict | None,
                                    memory: dict, direction: str) -> dict:
    """Reuse an actual node model only for its already selected native cursor."""
    if not isinstance(previous, dict):
        return {}
    checkpoint = previous.get("native_checkpoint")
    model = previous.get("model_used")
    writes = previous.get("checkpoint_writes")
    if (previous.get("assigned_harness") != key
            or not isinstance(model, str) or not model.strip()
            or not isinstance(checkpoint, dict)
            or not all(isinstance(checkpoint.get(field), str) and checkpoint[field].strip()
                       for field in ("kind", "session_id"))
            or not isinstance(writes, dict) or writes.get("request") is not True
            or previous.get("checkpoint_storage_error") or previous.get("native_resume_error")
            or checkpoint != memory.get("native_checkpoint")
            or not all(isinstance(previous.get(field), str) and previous[field].strip()
                       for field in ("workdir", "native_conversation_id"))
            or not isinstance(memory.get("workdir"), str) or not memory["workdir"].strip()
            or os.path.realpath(previous["workdir"]) != os.path.realpath(memory["workdir"])
            or previous.get("native_conversation_id") != memory.get("native_conversation_id")
            or (previous.get("native_session_scope") or "conversation")
            != (memory.get("native_session_scope") or "conversation")
            or previous.get("native_context_origin") != memory.get("native_context_origin")):
        return {}
    try:
        import model_registry
        # Resolve current authorization/compatibility; never reuse the previous
        # key or mutate the shared harness assignment or the persisted plan.
        assignment = model_registry.resolve_assignments(
            {"model_overrides": {key: model}, "directions": {key: direction}}, [key]).get(key, {})
    except Exception:
        return {}
    if (assignment.get("source") != "request_override" or assignment.get("model") != model
            or not assignment.get("appid") or assignment.get("configuration_error")):
        return {}
    return assignment


def run_one(key: str, task: str, qhash: str = "adhoc",
            cb: Optional[Callable[[dict], None]] = None,
            preferred_model: Optional[str] = None,
            preferred_appid: Optional[str] = None,
            attempt_timeout: Optional[float] = None,
            direction: str = "programming",
            user_id: str = "default",
            conversation_id: str = "",
            runtime_brief: str = "",
            task_facts: Optional[dict] = None,
            capability_policy: Optional[dict] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
            execution_depth: str = "standard",
            _provider_results: Optional[Callable[[], list[dict]]] = None,
            _model_selection_source: str = "",
            _continuation_result: Optional[dict] = None) -> dict:
    """真调一家 harness 干一个活。cb 收到该家的过程事件(全透明:reason/tool/result)。

    网关自愈(用户"不是能切模型吗·怎么等这么久"):所有家共用一条 AIGC 网关,
    某模型抖动时会 timeout/404/429/空答案。这里检测到这类信号,就调 select_usable_model
    换一个"未坏未耗尽"的模型+appid,在新进程里重试(最多 _MAX_ATTEMPTS 轮·每轮硬墙钟)。
    每次换模型都发透明事件——用户看得见它在自愈,不是干等。

    返回统一结构:{key, status, final, elapsed, error}。绝不抛异常,保证军团不因单家崩。
    """
    task_facts = dict(task_facts or {})
    project_context = dict(task_facts.pop("project_context", None) or {})
    try:
        import runtime_manifest as _runtime_manifest
        if execution_depth not in ("quick", "standard", "deep") or not _runtime_manifest.supports_depth(key, execution_depth):
            execution_depth = "standard"
    except Exception:
        execution_depth = "standard"

    _emitted_artifacts: set[tuple[str, str]] = set()

    def emit(ev):
        if ev.get("t") == "artifact_ready":
            # URL 是文件入口，不是版本。只压掉同一入口的相同事实，保留扫描补齐、
            # 内容改写和正式导出元数据；运输时间/phase 不制造新版本。名称不是身份。
            _artifact_id = str(ev.get("url") or ev.get("path") or "")
            if _artifact_id:
                _facts = {field: ev.get(field) for field in (
                    "path", "name", "ext", "kind", "bytes", "sha256", "provenance",
                    "lineage", "version", "export_snapshot_id", "description",
                    "next_action", "in_progress", "preview", "visibility")}
                _signature = (_artifact_id, json.dumps(
                    _facts, sort_keys=True, ensure_ascii=False, default=str))
                if _signature in _emitted_artifacts:
                    return
                _emitted_artifacts.add(_signature)
        if cb:
            try:
                cb({"agent": key, **ev})
            except Exception:
                pass

    def collect_artifact_facts(out, previous_artifacts=None):
        """Keep collection, runtime outcome and goal decisions independent."""
        import legion_paths

        workdir = str(out.get("workdir") or os.path.join(
            legion_paths.under("outputs"), f"{key}_{qhash}"))
        phase = "artifact_collection"
        try:
            import artifact_delivery as artifacts
            extra_roots = []
            try:
                import harness_registry
                if harness_registry._is_cloud_desktop_task(task):
                    import desktop_fs
                    extra_roots = [desktop_fs.user_meyo_root(user_id),
                                   desktop_fs.user_meyo_root("default")]
            except Exception:
                pass
            facts = artifacts.collect_run_artifacts(
                since_ts=t0, workdir=workdir, project=project_context,
                team_id=str(task_facts.get("team_id") or qhash),
                task_id=str(task_facts.get("task_id") or key),
                user_id=user_id, harness_key=key,
                native_conversation_id=conversation_id, request_id=qhash,
                final=str(out.get("final") or ""), extra_roots=extra_roots,
                previous_artifacts=previous_artifacts,
                require_project_authorization=_require_project_authorization)
        except Exception as exc:
            # The native result survives an exporter fault, but the fault must
            # not vanish. Never expose exception text containing private paths.
            notes = [{"name": "project", "reason": "artifact_export_unavailable",
                      "phase": phase, "error_type": type(exc).__name__,
                      "scan_status": "not_exported", "downloadable": False,
                      "file_preserved": True,
                      "message": "成果收集未完成，原文件未删除。"}]
            facts = {"artifacts": [], "artifact_safety_observations": notes,
                     "artifact_export": {"exported_files": 0, "observations": notes,
                                         "workdir": workdir}}
        return record_artifact_facts(out, facts, workdir)

    def record_artifact_facts(out, facts, workdir):
        """Keep the same export/version facts in responses and both memory slots."""
        out.update(facts)
        out["workdir"] = workdir
        arts = out["artifacts"]
        # Keep the returned and persisted descriptors identical. Assigning
        # versions after persistence left the next native request a stale view.
        version_seen = {}
        for artifact in arts:
            stem = str(artifact.get("name") or "")
            match = _VERSION_RE.search(stem)
            version = int(match.group(1)) if match else version_seen.get(stem, 0) + 1
            version_seen[stem] = version
            artifact["version"] = version
        try:
            import artifact_provenance
            artifact_provenance.attach_lineage(
                arts, objective=task, task=task,
                task_id=str(task_facts.get("task_id") or qhash),
                agent=key, actions=out.get("tool_trace") or [])
        except Exception as exc:
            out["artifact_lineage_error"] = {
                "phase": "artifact_lineage", "error_type": type(exc).__name__}
            emit({"t": "artifact_lineage_error", **out["artifact_lineage_error"]})
        for note in out.get("artifact_safety_observations") or []:
            emit({"t": "artifact_not_exported", **note})
        try:
            write_memory_facts(
                artifacts=arts,
                artifact_export=out["artifact_export"],
                artifact_safety_observations=out["artifact_safety_observations"],
                completed_steps=out.get("tool_trace") or [], workdir=workdir,
                error_envelope=out.get("error_envelope") or {},
                native_facts=out.get("native_facts") or {},
                phase="交付记录" if out.get("status") == "done" else "失败现场保留")
        except Exception as exc:
            out["artifact_state_error"] = {
                "phase": "artifact_memory_persistence", "error_type": type(exc).__name__,
                "message": "成果记录未写入任务记忆；当前响应保留文件与导出事实。"}
            emit({"t": "artifact_state_error", **out["artifact_state_error"]})
        return arts

    # 请求级隔离:直接调 run_one(如手动 fork 子 agent)时也给唯一 qhash,防串台。
    # dispatch 已生成唯一 qhash 传入,此处不会覆盖(仅兜住默认 "adhoc")。
    if not qhash or qhash == "adhoc":
        qhash = f"adhoc_{uuid.uuid4().hex[:12]}"

    # Remember only that an operator declaration existed before native work.
    # This is not cached permission: collection must reauthorize the project.
    # A revoked/invalid declaration may never fall back to a default-root scan.
    _require_project_authorization = False
    _declared_project_root = str(project_context.get("repo_root")
                                 or project_context.get("workspace_root") or "")
    if _declared_project_root:
        try:
            import personal_identity as _identity
            _require_project_authorization = bool(
                _identity.project_grant_declared(user_id, _declared_project_root))
        except Exception:
            _require_project_authorization = True

    # 取消检查(军团自审 P0):没显式传就用注册表(qhash=整单·key=本成员·任一被喊停即真)。
    # 显式传(测试/特殊调用)优先。cancel_check 会向下传给 _run_harness_isolated 的监控循环。
    if cancel_check is None:
        cancel_check = lambda: is_cancelled(qhash, key)

    t0 = time.time()
    # 领到任务事件（2026-09-16 瘦身）：task 含注入的执行任务书（续跑指引/历史经验/上下文等
    # 【】块），那是给 Agent 的指令不是给用户的过程事实——只展示用户原话部分（首个注入块前）。
    _task_head = str(task).split("【", 1)[0].strip() or str(task)[:80]
    emit({"t": "reason", "x": f"领到任务：{_task_head}", "execution_depth": execution_depth})

    if not _ORCH_OK:
        return {"key": key, "status": "error", "final": "",
                "elapsed": 0.0, "error": f"编排层不可用: {_ORCH_ERR}"}
    if key not in _harness_keys():
        return {"key": key, "status": "error", "final": "",
                "elapsed": 0.0, "error": f"未知 harness: {key}"}

    try:
        provider_binding = _explicit_provider_binding(key)
        provider_binding_id = None
        if provider_binding:
            import model_registry as _bound_model
            provider_binding_id = _bound_model.ccr_binding_key(
                provider_binding["model"], provider_binding["api_key"], provider_binding["base_url"])
        if provider_binding and (
                (preferred_model and preferred_model != provider_binding["model"])
                or (preferred_appid and preferred_appid != provider_binding["api_key"])):
            return _provider_configuration_error(key)
    except (ImportError, ValueError):
        return _provider_configuration_error(key)

    tried_models = []       # 已试过的模型,自愈时避开同一个坏模型
    # reasoning 模型不兼容的 external 家(cline/goose/openclaw·用 function tools+chat completions):
    # 每轮 select 都额外排除 reasoning 模型(首轮+自愈轮都不选它)·选 chat 原生模型(gpt-5.4/GLM/kimi/
    # qwen)。用单独 _base_exclude 不污染 tried_models 的 provenance(tried_models 只记真试过的)。
    # 给选择器"该家与 reasoning 模型不兼容"的现场·不是禁用(context not control)。
    # 家×模型兼容矩阵(2026-09-02 error治未病根修):静态表(cline/goose/openclaw×reasoning
    # 400/残片)+账本连败(该家×该模型零胜连败≥5·首轮就跳过不白烧)。统一走
    # harness_model_compat 单一事实源——旧版是两组写死集合,新家/新坑都要改两处。
    # 账本排除是【首轮跳过】不是硬禁:池耗尽仍可选(见 select_usable_model 排序),
    # 环境病污染的组合真跑一次成功(good≥1)即自动出清。
    try:
        import harness_model_compat as _hmc
        _base_exclude = _hmc.incompatible_models(key)
    except Exception:
        _base_exclude = set(_REASONING_MODELS) if key in _NO_REASONING_HARNESSES else set()
    # qhash 是本轮执行流水号(取消/事件/工作目录)，conversation_id 才是跨轮逻辑会话。
    # 两者不能混：HTTP team_id 带时间戳，每轮都变；拿它做缓存键就永远命不中。每家仍加 key
    # 隔离自己的原生会话，防不同 harness 的协议/工具历史混进同一槽。无 conversation_id 的老调用
    # 退回 qhash，保持请求级隔离，不会聚到一个全局 default。
    logical_session_id = str(conversation_id or qhash)
    _resume_conversation = str(task_facts.pop("_native_resume_conversation", "") or "")
    _project_conversation = str(project_context.get("conversation_id") or "")
    _may_bridge_lane = bool(_project_conversation and (
        logical_session_id == _project_conversation or _resume_conversation == _project_conversation))
    _native_scope = task_facts.get("native_session_scope")
    if _native_scope is None:
        _native_scope = "conversation"
    _scope_metadata = {}
    _memory_request_id = qhash
    _selection_context = {
        "memory_source": None, "memory_storage_id": None,
        "native_conversation_id": logical_session_id, "workspace_validated": False,
    }

    def remember_selection(memory, source, storage_id):
        """Parallel facts for the next decision; never export keys/binding digests.

        This is a reference to the rejected original context, not permission to
        resume it, a successful resume claim, or a replacement checkpoint.
        """
        checkpoint = memory.get("native_checkpoint") or {}
        _selection_context.update({
            "memory_source": source, "memory_storage_id": storage_id,
            "native_conversation_id": str(memory.get("native_conversation_id") or logical_session_id),
            "native_thread_id": str(checkpoint.get("session_id") or ""),
            "native_session_scope": str(memory.get("native_session_scope") or _native_scope),
            "workdir": str(memory.get("workdir") or ""),
            "workspace_validated": False,
        })

    _owner_conversation = logical_session_id
    if (_project_conversation and (
            _may_bridge_lane
            or logical_session_id.startswith(_project_conversation + ":task:")
            or logical_session_id == _project_conversation + ":captain-control"
            or logical_session_id.startswith(_project_conversation + ":contract-repair:"))):
        _owner_conversation = _project_conversation

    def scope_memory_matches(memory):
        if not memory:
            return
        checkpoint = memory.get("native_checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        if not checkpoint.get("session_id"):
            return  # 没有原生线程锚点：本轮不是 resume，不做 provider 恢复绑定判断。
        previous_binding = checkpoint.get("provider_binding")
        previous_descriptor = checkpoint.get("provider_descriptor")
        descriptor_known = (
            isinstance(previous_descriptor, dict)
            and set(previous_descriptor) == {"model", "base_url"}
            and all(isinstance(previous_descriptor[field], str) and previous_descriptor[field]
                    for field in ("model", "base_url")))
        mismatch_fields = None
        if descriptor_known and provider_binding and previous_binding and provider_binding_id:
            mismatch_fields = [field for field in ("model", "base_url")
                               if (str(previous_descriptor[field]).rstrip("/") if field == "base_url"
                                   else previous_descriptor[field])
                               != (str(provider_binding[field]).rstrip("/") if field == "base_url"
                                   else provider_binding[field])]
            if not mismatch_fields and previous_binding != provider_binding_id:
                # The persisted descriptor covers the other two exact hash inputs.
                mismatch_fields = ["credential"]
        _selection_context["binding"] = {
            "expected_present": bool(previous_binding),
            "actual_present": bool(provider_binding_id),
            "matches": bool(previous_binding and previous_binding == provider_binding_id),
            "expected_descriptor_known": descriptor_known,
            "mismatch_fields": mismatch_fields,
        }
        if (previous_binding or provider_binding_id) and previous_binding != provider_binding_id:
            raise ValueError("native checkpoint provider binding is missing or differs from this request")
        if _native_scope == "request":
            if (memory.get("native_context_origin") != _scope_metadata["native_context_origin"]
                    or memory.get("native_session_scope") != "request"
                    or memory.get("native_conversation_id") != logical_session_id):
                raise ValueError("native request checkpoint belongs to another execution context")
        elif memory.get("native_session_scope") == "request":
            raise ValueError("request-scoped checkpoint cannot become conversation history")
        elif memory.get("native_context_origin"):
            origin = memory["native_context_origin"]
            if not isinstance(origin, dict):
                raise ValueError("native continuation origin is malformed")
            import project_context as _memory_project
            _memory_project.require_owner(
                {"user_id": origin.get("user_id"),
                 "conversation_id": origin.get("conversation_id")},
                user_id, _owner_conversation)
            if (origin.get("harness") != key
                    or origin.get("conversation_id") != _owner_conversation):
                raise ValueError("native continuation facts belong to another conversation")

    def memory_context(memory_id):
        # Same public fact fields as task_memory.context, but retain explicit
        # empty values: a new empty list must clear, not resurrect, old files.
        import task_memory
        state = task_memory.load(memory_id, key)
        if not isinstance(state, dict):
            raise ValueError("native continuation storage is malformed")
        scope_memory_matches(state)
        return {name: state[name] for name in (
            "phase", "completed_steps", "artifacts", "artifact_export",
            "artifact_safety_observations", "error_envelope", "native_checkpoint",
            "workdir", "native_facts", "native_resume_error", "checkpoint_writes",
            "checkpoint_storage_error", "native_conversation_id", "native_session_scope",
            "native_context_origin", "native_request_memory_id")
            if name in state and state[name] is not None}

    def write_memory_facts(*, writes=None, **facts):
        """Mirror current facts, not permissions, to the two existing slots."""
        import task_memory
        writes = writes if writes is not None else {}
        targets = {"request": _memory_request_id, "conversation": f"conv:{session_id}"}
        failures = []
        for slot, memory_id in targets.items():
            try:
                scope_memory_matches(task_memory.load(memory_id, key))
                task_memory.update(memory_id, key, **{**facts, **_memory_scope_metadata})
                writes[slot] = True
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]
        return writes

    def unavailable_native_context(exc, *, executed=0, phase="native_resume_selection",
                                   execution_result=None, depth_upgrade=False):
        from copy import deepcopy
        recovery = deepcopy(_selection_context)
        comparison = recovery.get("binding") or {}
        labels = {"model": "模型", "base_url": "接口地址", "credential": "凭据"}
        differences = comparison.get("mismatch_fields")
        difference_text = ("差异字段未知（旧记录没有足够描述）" if differences is None else
                           "、".join(labels[field] for field in differences) + "不一致"
                           if differences else "已记录的原绑定一致")
        source_text = {"request": "本轮执行记忆", "conversation": "原会话记忆"}.get(
            recovery.get("memory_source"), "尚未选中记忆")
        summary = (f"恢复来源：{source_text}；{difference_text}；"
                   + ("原目录匹配已核验" if recovery.get("workspace_validated") else "原目录尚未核验")
                   + "。未自动切换提供方或新建原生线程。")
        if phase == "native_result_binding":
            summary = "原生执行已发生，但返回的绑定不一致；未采纳该恢复点。" + summary
        result = {"key": key, "status": "error", "final": "",
                "elapsed": round(time.time() - t0, 3) if executed else 0.0,
                "error": str(exc) + "；" + summary, "fail_kind": "native_session_unavailable",
                "attempts": max(0, executed - int(depth_upgrade)), "execution_calls": executed,
                "blocked_before_native": executed == 0,
                "error_envelope": {"category": "native_session_unavailable",
                                   "scope": "request", "retryable": False,
                                   "phase": phase,
                                   "source_ref": {"session_id": _project_conversation,
                                                  "request_id": qhash},
                                   "recovery_context": recovery,
                                   "message": str(exc)}}
        if executed and isinstance(execution_result, dict):
            # Keep accounting, not the rejected thread/checkpoint/answer/artifact.
            for field in ("usage", "model", "model_used", "execution_depth", "execution_depth_history"):
                if execution_result.get(field) is not None:
                    result[field] = deepcopy(execution_result[field])
            facts = execution_result.get("native_facts") or {}
            result["native_facts"] = {
                field: value for field, value in facts.items()
                if field in ("returncode", "process_returncode", "exit_code", "completed",
                             "interrupted", "budget_exhausted", "iteration_count", "tool_calls")
                and isinstance(value, (bool, int, float))
            } if isinstance(facts, dict) else {}
        emit({"t": "native_resume_blocked", "status": "error",
              "x": summary,
              "error_envelope": result["error_envelope"]})
        return result

    try:
        import task_memory as _lane_memory
        if _native_scope not in ("conversation", "request"):
            raise ValueError("native_session_scope must be conversation or request")
        if _native_scope == "request":
            import project_context as _scope_project
            _scope_project.require_owner(project_context, user_id, _owner_conversation)
            import hashlib
            origin = {"user_id": str(project_context.get("user_id") or user_id), "harness": key,
                      "request_id": str(task_facts.get("native_session_request_id")
                                        or task_facts.get("team_id") or qhash),
                      "logical_session_id": logical_session_id}
            _memory_request_id = _lane_memory.request_storage_id(qhash, origin)
            legacy_request = _lane_memory.load(qhash, key)
            if (isinstance(legacy_request, dict) and legacy_request.get("team_id") == qhash
                    and (legacy_request.get("native_checkpoint") or {}).get("session_id")):
                raise ValueError("native request already started with conversation history")
            _scope_metadata = {"native_session_scope": "request",
                               "native_context_origin": origin,
                               "native_request_memory_id": _memory_request_id}
            task_facts.update(_scope_metadata)
            # The storage layer truncates names. Hash the full identity before
            # constructing its short key; preserve original fields as context.
            logical_session_id = "request_" + hashlib.sha256(
                json.dumps(origin, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            _may_bridge_lane = False
        _request_memory = _lane_memory.load(_memory_request_id, key)
        if not isinstance(_request_memory, dict):
            raise ValueError("native continuation request storage is malformed")
        _request_checkpoint = _request_memory.get("native_checkpoint") or {}
        if not isinstance(_request_checkpoint, dict):
            raise ValueError("native continuation request checkpoint is malformed")
        remember_selection(_request_memory, "request", _memory_request_id)
        scope_memory_matches(_request_memory)
        if _request_checkpoint.get("session_id"):
            if _may_bridge_lane:
                logical_session_id = str(_request_memory.get("native_conversation_id")
                                         or logical_session_id)
            _selected_memory = _request_memory
        else:
            if _may_bridge_lane:
                import sessions as _lane_sessions
                logical_session_id = (_lane_sessions.continuation_native_slot(
                    _project_conversation, key, user_id, project_context) or logical_session_id)
            _selected_storage_id = f"conv:{key}:{logical_session_id}"
            _selected_memory = _lane_memory.load(_selected_storage_id, key)
        # These checks apply to every native slot, including parallel lanes.
        # Only cross-label selection above requires a unique harness lane.
        if not isinstance(_selected_memory, dict):
            raise ValueError("native continuation checkpoint storage is malformed")
        _selected_checkpoint = _selected_memory.get("native_checkpoint") or {}
        if not isinstance(_selected_checkpoint, dict):
            raise ValueError("native continuation checkpoint is malformed")
        if not _request_checkpoint.get("session_id"):
            remember_selection(_selected_memory, "conversation", _selected_storage_id)
        scope_memory_matches(_selected_memory)
        _requested_workspace = str(project_context.get("repo_root")
                                   or project_context.get("workspace_root") or "")
        if (_selected_checkpoint.get("session_id") and _requested_workspace
                and os.path.realpath(str(_selected_memory.get("workdir") or ""))
                != os.path.realpath(_requested_workspace)):
            raise ValueError("native continuation checkpoint belongs to a different workspace")
        _selection_context["workspace_validated"] = bool(
            _selected_checkpoint.get("session_id") and _requested_workspace)
    except (ValueError, PermissionError) as exc:
        return unavailable_native_context(exc)
    # Only an automatic worker assignment may inherit a node's actual model.
    # Identity/provider/workspace selection above stays authoritative; the
    # private result is not accepted as a replacement checkpoint or permission.
    if _model_selection_source == "task_auto" and not provider_binding:
        _resume_assignment = _native_continuation_assignment(
            key, _continuation_result, _selected_memory, direction)
        if _resume_assignment:
            preferred_model = _resume_assignment["model"]
            preferred_appid = _resume_assignment["appid"]
    session_id = f"{key}:{logical_session_id}"
    _memory_scope_metadata = {
        "native_conversation_id": logical_session_id,
        **(_scope_metadata or {
            "native_session_scope": "conversation",
            "native_context_origin": {
                "user_id": str(user_id or "default"), "harness": key,
                "conversation_id": _owner_conversation,
                "logical_session_id": logical_session_id},
        }),
    }
    # cache_key 由下游 HarnessCtx/环境按同一函数生成；这里保留会话锚点，不在 task 文本泄漏身份。
    # user 单元记忆目录:归一到 legion_paths(~/.meyo/legion/memory/<uid>/)·harness 自管读写。
    # 记忆下放依据 hermes run_agent.py:464,492(AIAgent 接 session_id/user_id 自续长期记忆)。
    try:
        import legion_paths as _lpaths
        memory_dir = _lpaths.under("memory", user_id or "default")
    except Exception:
        memory_dir = ""
    prior_attempts: list[dict] = []
    _attempt_usages: list[dict] = []
    task_facts.update(_memory_scope_metadata)
    _last_native: dict = dict(_memory_scope_metadata)

    # A cancelled attempt may never reach the native process. Keep only the
    # already-selected/actually-returned resume reference, not old execution facts.
    from copy import deepcopy as _copy_native_reference
    _cancel_native_reference: dict = {}

    def _remember_cancel_native_reference(value: dict) -> None:
        nonlocal _cancel_native_reference
        envelope = value.get("error_envelope")
        if (value.get("native_resume_error")
                or value.get("fail_kind") == "native_session_unavailable"
                or (isinstance(envelope, dict)
                    and envelope.get("category") == "native_session_unavailable")
                or (value.get("native_conversation_id")
                    and value["native_conversation_id"] != logical_session_id)):
            _cancel_native_reference = {}
            return
        checkpoint = value.get("native_checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("session_id"):
            _cancel_native_reference = {
                "native_checkpoint": _copy_native_reference(checkpoint),
                "native_conversation_id": logical_session_id,
            }
            workdir = value.get("workdir") or value.get("cwd")
            if workdir:
                _cancel_native_reference["workdir"] = str(workdir)

    def _with_cancel_native_reference(target: dict) -> dict:
        if target.get("native_checkpoint") or not _cancel_native_reference:
            return target
        workdir = target.get("workdir") or target.get("cwd")
        known_workdir = _cancel_native_reference.get("workdir")
        if workdir and known_workdir and os.path.realpath(str(workdir)) != os.path.realpath(known_workdir):
            return target
        for name, value in _cancel_native_reference.items():
            if not target.get(name):
                target[name] = _copy_native_reference(value)
        return target

    _remember_cancel_native_reference(_selected_memory)

    def _attach_task_usage(target: dict, *, collect: bool = True) -> dict:
        """汇总本次 run_one 的全部原生 usage；每轮只收一次，失败重试也计入。"""
        try:
            import token_telemetry as _telemetry
            if collect:
                current = target.get("usage")
                records = target.get("usage_records") if isinstance(target.get("usage_records"), list) else []
                if records:
                    _attempt_usages.extend(row if isinstance(row, dict) else {"available": False}
                                           for row in records)
                else:
                    # Every executed native attempt contributes a record, even
                    # when its final event omitted usage. Unknown is not zero.
                    _attempt_usages.append(current if isinstance(current, dict) and current
                                          else {"available": False})
            target["usage"] = _telemetry.aggregate(
                _attempt_usages,
                model=str(target.get("model_used") or target.get("model") or ""),
                elapsed=target.get("elapsed"))
        except Exception:
            target.setdefault("usage", {"available": False, "source": "unavailable",
                                        "model": str(target.get("model_used") or target.get("model") or ""),
                                        "elapsed_s": target.get("elapsed")})
        target.pop("usage_records", None)
        return target

    _spin_fps: list[str] = []        # 完整指纹(final+status+err·不含 model·hermes R3)·非基础设施失败用
    _spin_infra_fps: list[str] = []  # infra 指纹(只 status+error)·gateway/process 基础设施失败用(hermes R3)
    last = {"status": "error", "final": "", "error": "未执行"}
    result: dict = {}

    def pause_for_budget(current=None, *, provider_results=_provider_results):
        nonlocal last, result
        paused = _request_budget_pause(provider_results, current)
        if paused is None:
            return False
        result = paused
        last = {name: paused.get(name, "") for name in
                ("status", "final", "error", "fail_kind", "error_envelope")}
        _last_native.update(error_envelope=paused["error_envelope"],
                            native_facts=paused["native_facts"])
        try:
            import task_memory as _tm
            _tm.update(_memory_request_id, key, phase="请求预算已用尽",
                       error_envelope=paused["error_envelope"], native_facts=paused["native_facts"])
        except Exception as exc:
            paused["native_facts"]["runtime_budget_persistence_error"] = type(exc).__name__
        return True

    preserve_native_state = False
    native_reported_artifacts: list[dict] = []
    # 学 codex history 回流(mcp_tool_call.rs:875 失败也进 history)+ deepseek timeout-policy
    # (失败结果原样喂回模型):重试不再拿原 task 重跑(那样每轮失忆·永远重复同样失败),
    # 而是把"上一轮我怎么干、失败成什么样"作为 context 拼进新 task,让新进程的模型看到自己
    # 的失败自主换招(换 UA/换工具/换角度)。这是"给 context 不加 control"的链路实现。
    failure_context = ""
    # 质量门重做上下文（2026-09-22 修 incomplete 死锁）：begin_attempt 对被判 incomplete 的
    # 任务留档 quality_gate_retry_of（上次降级原因，如「研究协议缺桂冠锚/URL」）——重领时把
    # 它拼进任务文本，成员第一次就知道该按什么标准交卷，而不是盲跑后再次被判不合格。
    _gate_retry_reason = str((task_facts or {}).get("quality_gate_retry_of") or "")
    if _gate_retry_reason:
        failure_context += (
            "\n\n【上次交付被质量门退回·本次重做必须修正】\n退回原因："
            + _gate_retry_reason[:400]
            + "\n请对照修正后交付；研究类交付记得按任务书「锚点表交卷格式」补齐结构化字段。")
    context_retry_target: tuple[Optional[str], Optional[str]] | None = None
    output_retry_target: tuple[Optional[str], Optional[str]] | None = None
    output_recovery_applied = False
    # Quick 发现任务实际需要工具/文件/联网时，不把升级请求算作一次失败或模型重试。
    # 下一次执行保持同一 Harness/任务/qhash/逻辑 session/模型/appid/任务记忆/Project Context，
    # 唯一变化是 execution_depth=standard。局部布尔值保证每个 run_one 最多自动升级一次。
    depth_upgrade_target: tuple[Optional[str], Optional[str]] | None = None
    depth_upgraded = False
    execution_depth_history = [execution_depth]
    # 默认不设固定3轮墙:一直换【未试过】模型直到成功或模型池自然耗尽。候选池有限(当前总26),
    # exclude_models 保证不重复,天然收敛;_HARD_ATTEMPT_CEIL 仅防异常provider无限造候选。
    attempt = 0
    executed_attempts = 0
    native_attempts = []
    # Actual executions own their action lists. Failure-history rows are not
    # exhaustive: cancellation and native depth upgrades need not append one.
    # Keeping the lists here retains those facts without counting retries twice.
    action_batches: list[list[dict]] = []
    # 动态重试上限(用户 2026-08-29):失败率高(网关限流)→自动多试几个 appid/模型·不再死守
    # LEGION_GATEWAY_RETRIES=2(2个恰好都限流就误判失败);失败率低→精简省成本。永远 ≤ 模型池
    # (exclude_models 天然收敛)。每轮循环内重算(失败率是滑动窗口·实时反映当下限流态)。
    try:
        # A single authorized provider is not a one-attempt native-work budget.
        # Retain the existing caller ceiling and dynamic calculation, without
        # reading a different provider pool solely to count continuation slots.
        _pool = ((_HARD_ATTEMPT_CEIL if _HARD_ATTEMPT_CEIL > 0 else 40)
                 if provider_binding else len(_fallback_candidates(direction, set())))
    except Exception:
        _pool = 6
    native_attempt_limit = _dynamic_max_attempts(_pool)
    native_recovery_active = False

    def _cancel_before_execution() -> dict:
        emit({"t": "task_status", "status": "cancelled", "x": "用户停止·不再继续"})
        result = {
            "key": key, "harness": key, "status": "cancelled",
            "final": str(last.get("final") or ""), "elapsed": round(time.time() - t0, 2),
            "error": "用户停止", "attempts": max(0, executed_attempts - int(depth_upgraded)),
            "execution_calls": executed_attempts, "cancelled": True,
            "execution_depth": execution_depth,
            "execution_depth_history": list(execution_depth_history),
            "execution_depth_upgraded": depth_upgraded,
        }
        result.update(_last_native)
        _attach_task_usage(result, collect=False)
        return _with_cancel_native_reference(result)

    # Context classification may change the next projection, not the attempt budget.
    while attempt < _dynamic_max_attempts(_pool):
        attempt += 1
        # 动作清单(军团反思③):收本 attempt 的工具动作(tool_started/end/timeout·来自项③事件回流),
        # 存进 prior_attempts→回流下一轮/接力家,让它看到"上次具体调了哪些工具、成没成",不只 final[:500]。
        _actions: list[dict] = []
        # 用户取消(军团自审 P0):每轮换模型自救前先查——用户喊停就立即停·不再启新 attempt/新模型。
        # 取消不是"该自愈的故障"·绝不算失败换招(那会变成"用户喊停我却偷偷换家又跑")。
        if cancel_check():
            return _cancel_before_execution()
        if pause_for_budget(
                {**result, **last, **_last_native, "execution_calls": executed_attempts}
                if executed_attempts else None):
            break
        if native_recovery_active and executed_attempts >= native_attempt_limit:
            break
        model_override = appid_override = None
        _selection_hold = None
        # 失败历史通道(codex/deepseek 首脑复核 D1:不双写):历史失败要么走【结构化 prior_attempts
        # env 通道】(ctx 化 adapter·下方探测到就用),要么走【文本 failure_context 拼进 task】
        # (老 6 参 adapter 的唯一通道)。旧版两条都塞→ctx 化家把同一失败吃两遍·白占上下文。
        # 这里先只给纯 task,等下方探测出 adapter 是否吃 prior_attempts 后再决定文本通道拼不拼。
        task_this = task
        if depth_upgrade_target is not None:
            # 这是同一执行的深度升级，不进入模型选择器，确保 model/appid 与 Quick 完全一致。
            model_override, appid_override = depth_upgrade_target
            depth_upgrade_target = None
        elif output_retry_target is not None:
            model_override, appid_override = output_retry_target
            output_retry_target = None
        elif provider_binding:
            # Only this declared provider is authorized. Existing context/native
            # continuation may reuse it; an ordinary failure may not select a
            # different key from the company pool.
            if attempt > 1 and context_retry_target is None:
                break
            model_override, appid_override = provider_binding["model"], provider_binding["api_key"]
            context_retry_target = None
        elif attempt == 1:
            if preferred_model:
                model_override = preferred_model
                appid_override = preferred_appid or _appid_for_model(preferred_model)
                emit({"t": "tool", "x": f"run_harness({key}) · 用户/偏好模型 {model_override}"})
            else:
                # 根因修(2026-08-28):attempt1 曾不带 model/appid → codex 等家拿到空
                # AIGC_APPID 必 rc1("Missing environment variable: AIGC_APPID"),白白浪费
                # 首轮、还常停在 attempts=1。现在首轮就按任务方向选一个"未坏未耗尽"的默认
                # model+appid(select_usable_model 已归一方向+过滤 broken/exhausted),让首轮
                # 就带对 appid 真跑,而不是必挂后才在 attempt2 补。
                # 传 harness_key:按这家的历史健康重排候选(治忽好忽坏·上次成过的模型首轮就用)。
                # reasoning 不兼容家(cline/goose/openclaw):首轮就 exclude reasoning 模型·避免第一次
                # 就撞 400/reasoning残片(速度底线:第一次就选对·不靠撞一次再学)。
                pick = select_usable_model(
                    direction, exclude_models=(_base_exclude or None), harness_key=key,
                    **({"_provider_results": _provider_results}
                       if _provider_results is not None and not preferred_appid else {}))
                model_override = pick.get("model")
                # An explicit credential is still explicit when the caller leaves
                # model selection automatic. Never replace it with the pool's pick.
                appid_override = preferred_appid or pick.get("appid")
                _selection_hold = pick.get("authorization_hold")
                emit({"t": "tool", "x": (
                    f"run_harness({key}) · 候选授权不可用，未发起原生调用"
                    if _selection_hold else
                    f"run_harness({key}) · 智能默认模型 {model_override or '(网关兜底)'}")})
        else:
            if context_retry_target:
                # 上一轮是请求投影超窗，不是模型质量失败。保持同一模型/appid，只让统一 prompt
                # 预算器按结构化 prior_attempts 收紧到 62%/38%，避免污染健康榜或无意义换 provider。
                model_override, appid_override = context_retry_target
                context_retry_target = None
                emit({"t": "tool", "x": f"保持 {model_override or '当前模型'} 与 appid，沿已有现场重试"})
            else:
                # 换一个当下可用【且本请求没试过】的模型(按任务方向选·不再死磕 programming):
                # 用户2026-08-28"给不同appid拿最适合任务的模型就解决了"——视觉/生图/长调研各走对口方向。
                # 关键修(100场景实测):必须把 tried_models 传进去排除,否则 select 永远返回同一个
                # "最优"(如 MiniMax-M3),自愈=原地重试同一坏模型,cx023/cx091 就栽在这。
                excluded = set(tried_models) | _base_exclude   # reasoning 不兼容家:自愈轮也不选 reasoning 模型
                # 最后一轮优先跨方向:同方向前两种模型都失败后,很可能不是"某一个模型坏",
                # 而是该 harness 与这一 provider 族不兼容(真实E2E:penguin 对 general_chat 的
                # MiniMax/deepseek 均 rc1,但 programming 的 gpt-5.6-sol 正常)。若第三轮还留在
                # 原方向,重试预算耗尽就永远走不到可用模型。前两轮给本方向机会,最后一轮跨出去。
                # 先在本方向找未试过的可用模型;本方向耗尽自动跨方向兜底(不再依赖固定"第3轮"·
                # 无固定墙后:本方向所有候选试完就跨方向,跨方向也耗尽才真收尾)。
                pick = select_usable_model(direction, exclude_models=excluded, harness_key=key)
                model_override, appid_override = pick.get("model"), pick.get("appid")
                if not model_override:
                    pick = _select_cross_direction(direction, excluded)
                    model_override, appid_override = pick.get("model"), pick.get("appid")
                    if not model_override:
                        emit({"t": "reason", "x": f"全方向都无未试过的可用模型（{pick.get('reason')}）→ 收尾(自救到模型池耗尽)"})
                        break  # 真的没有可换的模型了,停(天然收敛·非固定次数墙)
                    emit({"t": "tool", "x": f"⚠️ 本方向候选耗尽 → 跨方向兜底换 {model_override}"})
                _lim_txt = str(_MAX_ATTEMPTS) if _MAX_ATTEMPTS > 0 else "池耗尽"
                emit({"t": "tool", "x": f"⚠️ 上一轮没通 → 换模型 {model_override}(未试过)自救({attempt}/{_lim_txt})"})
        # 向后兼容:老的 _run_harness_isolated(或测试/插件的 monkeypatch 替身)只有 6 参签名,
        # 不认 session_id/tried_models/prior_attempts。先探被调方是否接受这些下放参数,不接受
        # 就走老 6 参路径(权限下放只对已升级的实现生效·老替身零回归)。
        import inspect as _insp
        _iso_params = _insp.signature(_run_harness_isolated).parameters
        _iso_kw = {}
        if "session_id" in _iso_params:
            _iso_kw = {"session_id": session_id, "tried_models": tried_models,
                       "prior_attempts": prior_attempts}
            if "direction" in _iso_params:
                _iso_kw["direction"] = direction
            # user 单元/记忆目录:同样探测被调方是否支持(老替身不认则不传·零回归)。
            if "user_id" in _iso_params:
                _iso_kw["user_id"] = user_id
                _iso_kw["memory_dir"] = memory_dir
            # 运行时现场简报(军团点名缺的·探测支持则透传·老替身不认则不传·零回归)
            if "runtime_brief" in _iso_params and runtime_brief:
                _iso_kw["runtime_brief"] = runtime_brief
            if "task_facts" in _iso_params and task_facts:
                _iso_kw["task_facts"] = task_facts
            if "capability_policy" in _iso_params and capability_policy:
                _iso_kw["capability_policy"] = capability_policy
            if "project_context" in _iso_params and project_context:
                _iso_kw["project_context"] = project_context
            if "execution_depth" in _iso_params:
                _iso_kw["execution_depth"] = execution_depth
            if provider_binding:
                if "provider_binding" not in _iso_params:
                    return _provider_configuration_error(key)
                _iso_kw["provider_binding"] = dict(provider_binding)
            if "task_memory" in _iso_params:
                try:
                    _ctx_tm = memory_context(_memory_request_id)
                    # ★checkpoint 读回兜底(双键的另一端):本轮 qhash 无记录时·退逻辑会话键
                    # (conv:<session_id>)——第一轮的 native_checkpoint 在那(Goose/OpenClaw
                    # 原生 --resume 由此真触发)。
                    if not ((_ctx_tm or {}).get("native_checkpoint") or {}).get("session_id") and session_id:
                        _alt = memory_context(f"conv:{session_id}")
                        _ctx_tm = {**_alt, **_ctx_tm}
                    _iso_kw["task_memory"] = _ctx_tm
                except (ValueError, PermissionError) as exc:
                    return unavailable_native_context(exc)
                except Exception:
                    pass
        # 不双写(codex/deepseek D1):只有 adapter【不吃结构化 prior_attempts】时,才把失败历史
        # 以文本形式拼进 task(老 6 参路径的唯一通道)。ctx 化 adapter 已从 env 拿到结构化历史,
        # 这里不再重复拼文本·省掉"同一失败吃两遍"的上下文浪费。
        if "prior_attempts" not in _iso_kw and failure_context:
            task_this = task + failure_context
        # 事件回流:把这家的过程事件(工具/URL/状态)转成军团 cb → SSE 全过程流水。
        # 同样探测被调方支持(老替身不认则不传·零回归)。emit 已带 agent=key。
        if "progress_cb" in _iso_params:
            _last_phase = {"value": ""}
            def _prog(ev, _acts=_actions):
                # 捕获完整工具 provenance。候选、执行、结束/超时共享 call_id/selection/source_ref，
                # 下一轮与接力家因此能知道实际用了哪个 Skill/GitHub 工具/源码函数，而非只看笼统步骤。
                _t = ev.get("t")
                if _t == "weapon_candidates":
                    _acts.append({"t": _t, "query_hash": ev.get("query_hash"),
                                  "pool": ev.get("pool"), "returned": ev.get("returned"),
                                  "candidates": list(ev.get("candidates") or [])[:30],
                                  "ranking_context": dict(ev.get("ranking_context") or {})})
                elif _t in ("tool_started", "tool_end", "tool_timeout"):
                    _acts.append({"t": _t, "tool": ev.get("tool"), "call_id": ev.get("call_id"),
                                  "ok": ev.get("ok"), "status": ev.get("status"), "ms": ev.get("ms"),
                                  "selection": ev.get("selection"), "source_ref": ev.get("source_ref"),
                                  "brief": ev.get("args_brief") or ev.get("result_brief")})
                # 军团 deepseek 首脑复核揪出:各家 adapter 实际只发 harness_step(带工具文本 x + 访问 urls),
                # 从不发 tool_*·导致过程卡永远"调用0次工具"=说假话。把 harness_step 也收进动作清单,
                # 让过程卡如实反映"军团跑了哪几步·访问了哪些URL"。纯 context 沉淀·不加门。
                elif _t == "harness_step":
                    _x = str(ev.get("x") or "")
                    _urls = ev.get("urls") or []
                    if _x or _urls:
                        _acts.append({"t": "harness_step", "brief": _x[:120],
                                      "urls": _urls[:5] if isinstance(_urls, list) else []})
                phase = str(ev.get("phase") or "")
                if phase and phase != _last_phase["value"]:
                    _last_phase["value"] = phase
                    emit({"t": "phase", "phase": phase, "x": phase})
                    try:
                        write_memory_facts(
                            phase=phase, completed_steps=list(_acts),
                            workdir=(_iso_kw.get("task_memory") or {}).get("workdir") or None)
                    except Exception:
                        pass
                emit({k2: v2 for k2, v2 in ev.items() if k2 != "agent"})
            _iso_kw["progress_cb"] = _prog
        # 取消检查下放给监控循环(探测支持·老替身不认则不传·零回归):跑到一半用户喊停也能真切进程。
        if "cancel_check" in _iso_params:
            _iso_kw["cancel_check"] = cancel_check
        # ★速度底线(用户 2026-09-01 loop 揪出根因):external agent 本身已是隔离 worker 子进程
        # (external_agent_registry.run 起自己的 CLI/worker 进程)。再套 _run_harness_isolated
        # 又启一个"重新 import legion_core/run_comparison 的隔离子进程"= 三层子进程·每层 Python
        # 冷启动。实测:registry.run 直调 4.7s·套隔离层变 18s(差 13s 全是多余子进程冷启动)。
        # 隔离层存在的唯一理由是"换进程让 UNIFIED_MODEL/APPID 常量生效"(老家直调 run_comparison
        # 用常量)·但 external adapter 走 ctx.model 参数传模型(不依赖进程级常量)·根本不需要这层。
        # → external 家进程内直调 harness_registry 的 adapter(它内部再起自己的 worker·仍隔离)·
        # 跳过多余的中间隔离子进程。core 家(_STANDALONE_ADAPTERS)仍走 _run_harness_isolated 不变。
        _is_external = False
        try:
            import external_agent_registry as _ext_reg
            _is_external = any(a.get("key") == key for a in _ext_reg.load_catalog())
        except Exception:
            _is_external = False
        if prior_attempts and prior_attempts[-1].get("fail_kind") == "native_iteration_budget":
            expected_checkpoint = prior_attempts[-1].get("native_checkpoint")
            loaded_checkpoint = (_iso_kw.get("task_memory") or {}).get("native_checkpoint")
            if loaded_checkpoint != expected_checkpoint:
                _last_native["checkpoint_storage_error"] = "native checkpoint not read back"
                break
        # 跨进程全局槽位闸(2026-09-23 接线·治多用户并发挤爆):整台机器所有 legion 进程共享
        # LEGION_GLOBAL_SLOTS(默认 4)个 Agent 进程名额——几个用户同时发任务时各单内部的
        # 进程闸(_PROCESS_MAX_CONCURRENT)只管本进程,加总远超机器承载 → OOM 随机杀单。
        # 每个 attempt 持一个槽(换模型自愈轮自然释放重抢);排队等待事件透传给用户。
        # 闸内部已含保险:内存≥88% 让道 + 最长等 240s 放行 + LEGION_CONCURRENCY_OFF=1 一键回退。
        try:
            import legion_concurrency as _conc
            _conc_ctx = _conc.slot(key, cancel_check=cancel_check, on_wait=lambda facts: emit({
                **facts,
                "t": "task_queued", "status": "queued", "stage": "execution_queue",
                "x": "等待可用执行名额；可随时停止"}))
        except Exception:
            import contextlib as _ctxlib
            _conc_ctx = _ctxlib.nullcontext()
        with _conc_ctx as _slot_got:
            if cancel_check():
                return _cancel_before_execution()
            if _provider_results is not None:
                from error_envelope import request_provider_authorization_hold
                request_results = _provider_results()
                if pause_for_budget(
                        {**result, **last, **_last_native, "execution_calls": executed_attempts}
                        if executed_attempts else None, provider_results=lambda: request_results):
                    break
                held = _selection_hold or request_provider_authorization_hold(
                    request_results, appid_override or "",
                    provider_binding["base_url"] if provider_binding else
                    os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native")
                if held:
                    last = {"status": "error", "final": str(last.get("final") or ""),
                            "error": held["message"], "fail_kind": held["category"],
                            "error_envelope": held}
                    result = {**last, "blocked_before_native": True,
                              "workdir": str((_iso_kw.get("task_memory") or {}).get("workdir") or "")}
                    _last_native["error_envelope"] = held
                    try:
                        write_memory_facts(phase="等待管理员确认授权", error_envelope=held)
                    except Exception:
                        pass
                    break
            # First default workspaces must be bound at actual allocation, not
            # inferred later from a packet/path or a fabricated native result.
            # All active core, external and isolated routes share this admission;
            # capability-only/disabled engines are not enabled by this binding.
            if (not (project_context.get("repo_root") or project_context.get("workspace_root"))
                    and task_facts.get("team_id") and task_facts.get("task_id")
                    and task_facts.get("attempt_id")):
                try:
                    import legion_sandbox as _allocation
                    import project_context as _allocation_context
                    _allocated_workdir = _allocation.allocate_task_workdir(
                        key, qhash, team_id=task_facts["team_id"], task_id=task_facts["task_id"],
                        attempt_id=task_facts["attempt_id"], user_id=user_id,
                        native_conversation_id=conversation_id)
                    if _allocated_workdir:
                        project_context = _allocation_context.snapshot(
                            task_facts["team_id"], task_facts["task_id"],
                            execution_depth=execution_depth)
                        if project_context.get("workspace_root") != _allocated_workdir:
                            raise PermissionError("default workspace binding was not read back")
                        _iso_kw["project_context"] = project_context
                except (OSError, ValueError, RuntimeError) as exc:
                    if cancel_check():
                        return _cancel_before_execution()
                    return {"key": key, "status": "error", "final": "", "elapsed": 0.0,
                            "error": f"project workspace unavailable:{type(exc).__name__}:{exc}",
                            "blocked_before_native": True, "execution_calls": executed_attempts,
                            "fail_kind": "project_workspace"}
            # Read at the actual input boundary, after queueing and provider/
            # cancellation checks. Use exactly the memory passed below, not an
            # earlier selection or a new "latest team" lookup.
            task_this += _native_task_mail_context(
                key, task_facts, project_context, _iso_kw.get("task_memory") or {},
                user_id=user_id, native_conversation_id=logical_session_id)
            if model_override and model_override not in tried_models:
                tried_models.append(model_override)
            executed_attempts += 1
            action_batches.append(_actions)
            if _is_external:
                result = _run_external_inprocess(
                    key, task_this, qhash, model_override, appid_override,
                    session_id=session_id, user_id=user_id, memory_dir=memory_dir,
                    tried_models=tried_models, prior_attempts=prior_attempts,
                    runtime_brief=runtime_brief, task_facts=task_facts,
                    capability_policy=capability_policy,
                    task_memory=_iso_kw.get("task_memory"), project_context=project_context,
                    direction=direction,
                    timeout_s=float(_ATTEMPT_TIMEOUT if attempt_timeout is None else attempt_timeout),
                    progress_cb=_iso_kw.get("progress_cb"), cancel_check=cancel_check,
                    execution_depth=execution_depth,
                    **({"provider_binding": provider_binding} if provider_binding else {}))
            elif key in _CORE_INPROC_KEYS:
                # 已 ctx 化的 core 家:进程内直调·跳过多余隔离子进程(用户 2026-09-01 确认彻底根治)
                result = _run_core_inprocess(
                    key, task_this, qhash, model_override, appid_override,
                    session_id=session_id, user_id=user_id, memory_dir=memory_dir,
                    tried_models=tried_models, prior_attempts=prior_attempts,
                    runtime_brief=runtime_brief, task_facts=task_facts,
                    capability_policy=capability_policy,
                    task_memory=_iso_kw.get("task_memory"), project_context=project_context,
                    direction=direction,
                    timeout_s=float(_ATTEMPT_TIMEOUT if attempt_timeout is None else attempt_timeout),
                    progress_cb=_iso_kw.get("progress_cb"), cancel_check=cancel_check,
                    execution_depth=execution_depth,
                    **({"provider_binding": provider_binding} if provider_binding else {}))
            else:
                result = _run_harness_isolated(key, task_this, qhash,
                                               float(_ATTEMPT_TIMEOUT if attempt_timeout is None else attempt_timeout),
                                               model_override, appid_override, **_iso_kw)
        # Capture this native invocation before any legacy memory recovery can
        # replace result.artifacts with an older export snapshot.
        native_reported_artifacts = [dict(item) for item in
            [*(result.get("artifacts") or []), *(result.get("discovered_artifacts") or [])]
            if isinstance(item, dict)]
        # The external transport redacts its launch environment. Cover legacy
        # adapters too, using this invocation's actual binding before recording
        # native attempts or exposing a diagnostic through a result/event.
        from external_agent_transports import redact_native_diagnostic
        _diagnostic_env = dict(os.environ)
        if appid_override:
            _diagnostic_env["LEGION_REQUEST_APPID"] = appid_override
        if provider_binding:
            _diagnostic_env["LEGION_REQUEST_API_KEY"] = provider_binding["api_key"]
        for _field in ("stderr", "error", "error_envelope"):
            if result.get(_field):
                result[_field] = redact_native_diagnostic(result[_field], _diagnostic_env)
        native_facts = dict(result["native_facts"]) if isinstance(result.get("native_facts"), dict) else {}
        for _field in ("stderr", "error", "failure_reason"):
            if native_facts.get(_field):
                native_facts[_field] = redact_native_diagnostic(native_facts[_field], _diagnostic_env)
        if isinstance(result.get("native_facts"), dict):
            result["native_facts"] = native_facts
        if native_facts:
            native_attempts.append({"attempt": executed_attempts, "facts": native_facts.copy()})
        result = _attach_task_usage(result)
        checkpoint = result.get("native_checkpoint")
        if provider_binding_id and isinstance(checkpoint, dict) and checkpoint.get("session_id"):
            reported_binding = checkpoint.get("provider_binding")
            if reported_binding and reported_binding != provider_binding_id:
                _selection_context["result_binding_matches"] = False
                blocked = unavailable_native_context(ValueError("native result provider binding differs"),
                                                     executed=executed_attempts, phase="native_result_binding",
                                                     execution_result=result, depth_upgrade=depth_upgraded)
                # Observe the caller budget without restoring any rejected
                # checkpoint, content or artifact from the foreign binding.
                return result if pause_for_budget(blocked) else blocked
            result["native_checkpoint"] = {
                **checkpoint, "provider_binding": provider_binding_id,
                "provider_descriptor": {"model": provider_binding["model"],
                                        "base_url": provider_binding["base_url"]},
            }
        _remember_cancel_native_reference(result)
        _last_native = {name: result.get(name) for name in
                        ("native_checkpoint", "resumed_native", "native_events", "transport_failures",
                         "responses_transport", "workdir", "cwd")
                        if result.get(name) is not None}
        _last_native.update(_memory_scope_metadata)
        if result.get("native_resume_error"):
            _last_native["native_resume_error"] = result["native_resume_error"]
        _last_native["native_facts"] = native_facts
        _last_native["native_attempts"] = list(native_attempts)
        checkpoint_saved = False
        checkpoint_writes = {"request": False, "conversation": False}
        if result.get("native_checkpoint"):
            _last_native["native_conversation_id"] = logical_session_id
            try:
                write_memory_facts(writes=checkpoint_writes,
                                   native_checkpoint=result["native_checkpoint"],
                                   workdir=str(result.get("workdir") or ""))
                checkpoint_saved = all(checkpoint_writes.values())
            except Exception as exc:
                _last_native["checkpoint_storage_error"] = str(exc)
        _last_native["checkpoint_writes"] = dict(checkpoint_writes)
        if native_facts or result.get("native_resume_error"):
            try:
                write_memory_facts(
                    native_facts=native_facts,
                    native_resume_error=result.get("native_resume_error") or {},
                    checkpoint_writes=checkpoint_writes,
                    checkpoint_storage_error=_last_native.get("checkpoint_storage_error") or "")
            except Exception:
                pass
        if result.get("resumed_native") and native_facts:
            emit({"t": "reason", "phase": "继续工作", "status": "resumed",
                  "qhash": qhash, "x": "已读取原生会话历史，本次执行沿原现场继续",
                  "native_session_id": (result.get("native_checkpoint") or {}).get("session_id")})
        raw_final = str(result.get("final", "") or "")
        final = _strip_reasoning(raw_final)
        # harness_worker 会解析成员编排标记，但进程内 core/external 路径绕过 worker；统一出口再解析一次，
        # 保证两条路径对称。否则 DeerFlow 等真写 need_peer 仍会把协议原文当 final、请求永不执行。
        if "<<LEGION_ORCH>>" in final:
            try:
                import harness_registry as _hr_orch
                final, orch_request = _hr_orch._parse_orch_request(final)
                if orch_request:
                    result["orchestration_request"] = orch_request
            except Exception:
                pass
        status = str(result.get("status", "ok") or "ok")
        # This status belongs to the trusted adapter receipt, not JSON fields
        # inside its answer. It identifies a content channel; it proves neither
        # a process exit code nor native completion nor the quality of the work.
        _success_statuses = {"ok", "done", "completed", "success"}
        _noncompletion_statuses = {"partial", "incomplete", "blocked", "unavailable", "unknown"}
        _adapter_reported_success = str(result.get("status") or "") in _success_statuses
        _native_rc = native_facts.get("process_returncode")
        _native_failed = bool(
            native_facts.get("failed") or native_facts.get("error") or native_facts.get("failure_reason")
            or native_facts.get("turn_status") == "failed"
            or (type(_native_rc) is int and _native_rc != 0))
        if status in _success_statuses or status == "incomplete":
            if _native_failed:
                status = f"rc{_native_rc}" if type(_native_rc) is int and _native_rc != 0 else "error"
            elif (_native_budget_exit(result) or native_facts.get("completed") is False
                  or native_facts.get("turn_status") == "incomplete"):
                status = "incomplete"
        # 原始 native_facts 不展开覆盖公共字段；标准错误同时保留 worker 和原生终态原因。
        # Worker 的工作目录说明不是错误源，不能抢先遮住 stderr-only adapter 的真原因。
        # 已明确正常结束的原生轮次仍保留 stderr 观察，不把非致命诊断升级成任务失败。
        _stderr = str(result.get("stderr") or "")
        if _stderr:
            native_facts.setdefault("stderr", _stderr)
        _stderr_error = (_stderr if native_facts.get("stderr_is_error") is True
                         or (native_facts.get("stderr_is_error") is not False
                             and not (_adapter_reported_success and status in _success_statuses)
                             and not _native_completed_cleanly({**result, "status": status})) else "")
        _errors = []
        for _error in (_stderr_error, result.get("error"), native_facts.get("error"),
                       native_facts.get("failure_reason")):
            if _error and str(_error) not in _errors:
                _errors.append(str(_error))
        if _errors:
            result["error"] = "\n".join(_errors)
        if status in _success_statuses and (
                result.get("error") or
                (isinstance(result.get("error_envelope"), dict) and result["error_envelope"])):
            status = "error"  # Current runtime diagnostics outrank the success receipt.
        elapsed = round(time.time() - t0, 2)
        _orch_request = (result.get("orchestration_request")
                         if isinstance(result.get("orchestration_request"), dict) else {})
        _upgrade_request = (_orch_request.get("upgrade")
                            if isinstance(_orch_request.get("upgrade"), dict) else None)
        _requested_depth = str((_upgrade_request or {}).get("depth") or "").strip().lower()
        import error_envelope as _native_ee
        boundary_envelope = _native_ee.normalize(status, str(result.get("error") or ""),
            fail_kind=str(result.get("fail_kind") or ""),
            supplied=result.get("error_envelope"), native_facts=native_facts).to_dict()
        boundary_category = boundary_envelope.get("category")
        if (status == "cancelled" or cancel_check()
                or (status != "timeout" and
                    (native_facts.get("interrupted") or boundary_category == "user_cancel"))):
            status = "cancelled"
            _last_native["error_envelope"] = boundary_envelope
        elif pause_for_budget({**result, "status": status, "error_envelope": boundary_envelope,
                               "execution_calls": executed_attempts}):
            break
        elif boundary_category == "output_limit":
            # A complete native checkpoint is required. Never replay a partial
            # tool invocation, silently switch providers, or claim an empty final
            # means no work/cost occurred. Feed the failure into the same native
            # context once; a repeated identical limit needs a changed condition.
            # A failed response does not establish whether its tools executed.
            from copy import deepcopy as _copy_limit
            envelope = _copy_limit(boundary_envelope)
            details = envelope.setdefault("details", {})
            checkpoint = result.get("native_checkpoint") or {}
            recoverable = bool(checkpoint_saved and isinstance(checkpoint, dict)
                               and checkpoint.get("session_id")
                               and not result.get("native_resume_error"))
            budget_available = attempt < _dynamic_max_attempts(_pool)
            retry = recoverable and budget_available and not output_recovery_applied
            details.update({
                "recovery_status": ("context_retry" if retry else
                                    "checkpoint_unavailable" if not recoverable else
                                    "unchanged_limit_after_recovery" if output_recovery_applied else
                                    "caller_attempt_budget_exhausted"),
                "same_provider": True, "original_workspace_retained": bool(result.get("workdir")),
                "recovery_context": (
                    "原生提供方报告 max_output_tokens；这是本次输出被截断，不是输入超窗。"
                    "工具是否执行、执行到哪里，以原生工具结果及现场读回为准；"
                    "不原样重放未确认的操作，也不能把尚未落盘的内容当成已有文件。"
                    "原线程、已完成工具结果、研究资料和工作目录仍保留；"
                    "本次续接应先识别最后确实完成的边界，再按真实输出预算调整制作方式，"
                    "避免再次生成同一大段未完成输出。预算数值未返回时保持未知，不猜数值或换模型。"),
            })
            envelope["retryable"] = retry
            last = {"status": "error", "final": final, "error": envelope["message"],
                    "fail_kind": "output_limit", "error_envelope": envelope}
            prior_attempts.append({
                "attempt": executed_attempts, "model": model_override or "", **last,
                "native_facts": native_facts, "native_checkpoint": checkpoint,
                "workdir": str(result.get("workdir") or ""), "actions": list(_actions),
                "usage": result.get("usage"),
            })
            _last_native["error_envelope"] = envelope
            _last_native["output_limit_recovery"] = {
                "applied": bool(retry or output_recovery_applied), "status": details["recovery_status"]}
            try:
                import task_memory as _limit_memory
                _limit_memory.update(_memory_request_id, key, phase="输出截断后恢复" if retry else "输出恢复待处理",
                                     error_envelope=envelope,
                                     output_limit_recovery=_last_native["output_limit_recovery"])
            except Exception as error:
                retry = False
                envelope["retryable"] = False
                details["recovery_status"] = "recovery_context_storage_failed"
                _last_native["checkpoint_storage_error"] = type(error).__name__
            emit({"t": "output_limit_recovery", "status": "resuming" if retry else "blocked",
                  "x": ("输出被截断：已保留原线程和成果，携带原因调整输出方式后继续。"
                        if retry else "相同输出限制仍未解决，已保留现场；不再原样重放或自动换模型。"),
                  "error_envelope": envelope})
            if retry:
                output_recovery_applied = True
                output_retry_target = (model_override, appid_override)
                continue
            break
        elif (not _native_ee.is_provider_authorization_denial(boundary_envelope)
              and (boundary_category in ("authorization_required", "credential_required",
                                    "native_session_unavailable")
              or ((result.get("error_envelope") or _native_failed
                   or boundary_category == "context_overflow")
                  and not boundary_envelope.get("retryable")))):
            last = {"status": "timeout" if status == "timeout" else "error", "final": final,
                    "error": str(boundary_envelope.get("message") or result.get("error") or status),
                    "fail_kind": boundary_category, "error_envelope": boundary_envelope}
            prior_attempts.append({"attempt": executed_attempts, "model": model_override or "",
                **last, "native_facts": native_facts, "actions": _actions,
                "usage": result.get("usage"), "workdir": str(result.get("workdir") or "")})
            break
        if (status != "cancelled" and execution_depth == "quick" and not depth_upgraded
                and _upgrade_request is not None and _requested_depth == "standard"
                and not _native_ee.requires_provider_pause(
                    {"status": status, "error_envelope": boundary_envelope})):
            # 必须早于 ok/empty/gateway/health 分类：Quick 的输出是执行深度请求，不是用户答案，
            # 更不是模型失败。原生 checkpoint 已在上方落盘；下一轮会从同 qhash/task_memory 读回。
            depth_upgraded = True
            reason = str(_upgrade_request.get("reason") or "任务需要完整工具与多步骤执行")[:300]
            emit({"t": "execution_depth_upgraded", "status": "running",
                  "from_depth": "quick", "to_depth": "standard", "reason": reason,
                  "qhash": qhash, "session_id": session_id,
                  "conversation_id": str(conversation_id or qhash),
                  "x": f"{key} 发现任务需要完整能力，已从 Quick 原地升级到 Standard：{reason}"})
            execution_depth = "standard"
            execution_depth_history.append("standard")
            depth_upgrade_target = (model_override, appid_override)
            # 深度升级不消耗模型重试次数；回到同一 attempt 编号，仅重建完整 HarnessCtx。
            attempt -= 1
            continue
        # Harness 超时/异常但 task memory 已处于复检且产物齐全时，先把真实文件带回结果。
        # 这里只恢复 artifacts，不自封 done：dispatch 的显式 delivery contract 会客观验文件；
        # require_final=true 时仍保持失败，require_final=false 的文件型任务可由契约事实完成。
        if status in ("timeout", "error") and not result.get("artifacts"):
            try:
                import task_memory as _tm
                _saved = _tm.context(_memory_request_id, key)
                _saved_arts = list((_saved or {}).get("artifacts") or [])
                if _saved_arts:
                    result["artifacts"] = _saved_arts
                    result["workdir"] = result.get("workdir") or (_saved or {}).get("workdir") or ""
                    result["partial"] = True
                    emit({"t": "artifact_recovered", "phase": "复检恢复",
                          "x": f"Harness 未收口，但已恢复 {len(_saved_arts)} 个真实产物供契约验收",
                          "artifact_count": len(_saved_arts)})
            except Exception:
                pass
        # 跑到一半被取消:_run_harness_isolated 已安全切进程并标 cancelled·run_one 不当"该自愈的
        # 故障"重试(那会变成用户喊停却偷偷换家再跑)。直接返回 cancelled·保留宽限期 partial。
        if status == "cancelled":
            emit({"t": "task_status", "status": "cancelled", "x": "用户停止·已切进程"})
            # Return to the shared terminal collector instead of bypassing it.
            # Only an attempt that actually ran can report artifact selections;
            # cancellation itself remains authoritative and never retries.
            last = {"status": "cancelled", "final": final,
                    "error": str(result.get("error") or "用户停止")}
            break
        if (not result.get("error_envelope") and native_facts
                and _native_budget_exit(result) and status == "incomplete"):
            checkpoint = result.get("native_checkpoint")
            checkpoint_ok = (isinstance(checkpoint, dict)
                             and bool(checkpoint.get("session_id")) and checkpoint_saved)
            can_resume = checkpoint_ok and executed_attempts < native_attempt_limit
            envelope = {"category": "native_iteration_budget", "retryable": False,
                        "scope": "task", "code": status,
                        "message": str(native_facts["turn_exit_reason"]),
                        "details": {"native_facts": native_facts,
                                    "checkpoint_saved": checkpoint_saved,
                                    "same_parent_request_parameters": can_resume}}
            last = {"status": status, "final": final,
                    "error": str(native_facts["turn_exit_reason"]),
                    "fail_kind": "native_iteration_budget", "error_envelope": envelope}
            prior_attempts.append({"attempt": executed_attempts, "model": model_override or "",
                "status": status, "final": raw_final, "error": result.get("error"),
                "fail_kind": "native_iteration_budget", "error_envelope": envelope,
                "native_facts": native_facts, "native_checkpoint": checkpoint,
                "usage": result.get("usage"), "actions": _actions,
                "workdir": str(result.get("workdir") or "")})
            try:
                write_memory_facts(error_envelope=envelope,
                                   phase="准备续作" if can_resume else "本轮预算已用完")
            except Exception:
                pass
            if can_resume:
                native_recovery_active = True
                context_retry_target = (model_override, appid_override)
                emit({"t": "native_resume", "attempt": executed_attempts,
                      "phase": "继续工作", "status": "retrying", "qhash": qhash,
                      "x": "本轮迭代已用完，正在尝试从已保存现场继续",
                      "remaining_attempts": native_attempt_limit - executed_attempts,
                      "native_facts": native_facts, "session_id": session_id})
                continue
            break
        # 只把【未闭合】思考块视为 token 截断。闭合 <think> 后的正文无论多短都可能是合法交付
        # （如用户明确要求只回“好”）；长度不是完成语义，不能因 <80 字触发无谓换模型。
        truncated_think = bool(_THINK_OPEN_RE.search(_THINK_BLOCK_RE.sub("", raw_final or "")))
        if truncated_think:
            status = "truncated_reasoning"   # 归一为可自愈信号(会被 gateway_issue 判为可重试)

        # "看起来没真干活"检测(学 deepseek repeat-reminder:仅识别循环/摆烂·不否决):
        # 有些家会返回一句"我会去做/我将改用XX"这种计划句就交差,status=ok 但没交付。
        # 把这类"托词 final"也判为未完成,触发重试并回流提醒——不是加门丢答案,是让它看到
        # "上次你只说没做"再给一次机会自己做出来。
        stalled = _looks_like_stall(final)
        # Adapter status/error/envelope are runtime channels. Words such as
        # "Error:" or "HTTP 429" in a reported successful answer are not errors.
        _raw_error = str(result.get("error") or "")
        _raw_envelope = result.get("error_envelope")
        _supplied_envelope: dict = _raw_envelope if isinstance(_raw_envelope, dict) else {}
        if not _raw_error and _supplied_envelope:
            _raw_error = str(_supplied_envelope.get("message") or _supplied_envelope.get("code")
                             or _supplied_envelope.get("category") or "native runtime error envelope")
        protocol_output_ok = (bool(_orch_request)
                              and status in {"ok", "done", "completed", "success"}
                              and not _native_ee.is_provider_authorization_denial(boundary_envelope))
        # A confirmed native completion can discuss failures or return a file
        # named "connection". That content is not a transport diagnostic.
        # This only selects the diagnostic channel; it does not declare delivery.
        native_final_is_content = (_adapter_reported_success or status in _noncompletion_statuses
                                   or _native_completed_cleanly({**result, "status": status}))
        # The transport owns this provenance. A text CLI has no native turn
        # terminal, but its stdout is still content rather than a diagnostic.
        # Exit/terminal/error checks below remain independent of that content.
        native_final_is_content = native_final_is_content or (
            type(native_facts.get("process_returncode")) is int
            and native_facts.get("final_source") in
            {"cli_stdout_text", "cli_assistant_stream", "cli_native_output"})
        if native_final_is_content:
            stalled = False  # A phrase in a reported answer is not a runtime event.
        terminal_message_absent = _native_terminal_message_absent(
            {**result, **_last_native, "status": status, "final": final})
        # Explicit errors/envelopes still decide failure below. Only an
        # unrecognized/non-success legacy receipt falls back to its final text.
        failure_text = (_raw_error if native_final_is_content or
                        (protocol_output_ok and isinstance(_orch_request.get("coordinate"), dict))
                        else f"{final}\n{_raw_error}")
        context_overflow = (boundary_category == "context_overflow"
                            or str(result.get("fail_kind") or "") == "context_overflow"
                            or str(_supplied_envelope.get("category") or "") == "context_overflow"
                            or _is_context_overflow(status, failure_text))
        runtime_stop = (str(result.get("fail_kind") or "") in {"runtime_budget", "idle_stall"}
                        or str(_supplied_envelope.get("category") or "") in {"runtime_budget", "idle_stall"})
        gateway_issue = not runtime_stop and (
            context_overflow or _looks_like_gateway_issue(status, failure_text))
        # A native turn may return only a structured orchestration request.
        # Preserve that response for its existing consumer; it is not an empty
        # model answer, and _is_delivered still requires actual user-facing text.
        has_native_output = bool(final.strip()) or protocol_output_ok
        ok = (status in _success_statuses
              and (not _orch_request or protocol_output_ok)
              and has_native_output and not _raw_error and not _supplied_envelope
              and not stalled and not gateway_issue)
        if ok or terminal_message_absent:
            # 记这家跑这模型【成功】(治忽好忽坏:下次首轮优先选这家真跑通过的模型)。
            if ok:
                record_model_health(key, model_override or "", ok=True)
                _record_gateway_call(True)   # 网关成功(滑动窗口·失败率下降→动态重试回落省成本)
            emit({"t": "result", "x": final[:300]})
            # provenance 诚实(完成契约·2026-08-28 ctx 化后闭环):优先信 adapter 回传的
            # model_used(harness 实际用的模型),没有才退父层 tried_models[-1](父层选的)。
            # 当前两者一致;但 ctx 下放控制权后 harness 可自己换 model,此时只有 adapter 回传
            # 的才是真相——父层 tried_models 会说谎。先信真实值,给后续下放留对的口。
            _model = str(result.get("model_used") or "") or (tried_models[-1] if tried_models else "默认")
            # 用户中心:交付里带上"哪家干的·什么模型"·让用户能判断可信度/决定换最强家再试
            # (CLAUDE.md 过程透明可感)。harness/model 是前端友好别名·保留 key/model_used 兼容。
            _tool_trace = [action for batch in action_batches for action in batch]
            _ret = {"key": key, "harness": key,
                    "status": "partial" if terminal_message_absent else "done", "final": final,
                    "elapsed": elapsed, "error": "",
                    "attempts": max(0, executed_attempts - int(depth_upgraded)),
                                "execution_calls": executed_attempts, "model_used": _model, "model": _model,
                    "execution_depth": execution_depth,
                    "execution_depth_history": list(execution_depth_history),
                    "execution_depth_upgraded": depth_upgraded,
                    "usage": result.get("usage"),
                    "workdir": str(result.get("workdir") or ""),
                    "context_budget": dict(result.get("context_budget") or {}),
                    "tool_trace": _tool_trace[-100:],
                    **_last_native,
                    "raw": {k: v for k, v in result.items()
                            if k in ("search_count", "trace_path")}}
            if cb:
                try:
                    import token_telemetry as _telemetry
                    _usage_text = _telemetry.summary_text(_ret.get("usage"))
                except Exception:
                    _usage_text = "该 Harness 未提供原生 Token 数据"
                emit({"t": "usage", "phase": "资源用量", "usage": _ret.get("usage"),
                      "x": _usage_text})
            # 成功与失败共用正规出口。导出失败是并列事实，不改写原生运行结果，
            # 也不能因 final 非空吞掉错误、退回共享扫描或把源文件当导出快照。
            arts = collect_artifact_facts(_ret)
            for a in arts:
                emit({"t": "artifact_ready", "phase": "产出文件", "url": a.get("url"),
                      "name": a.get("name"), "ext": a.get("kind"),
                      "bytes": a.get("bytes"), "provenance": a.get("provenance"),
                      "lineage": a.get("lineage"), "version": a.get("version"),
                      "sha256": a.get("sha256"),
                      "export_snapshot_id": a.get("export_snapshot_id")})
            try:
                # Collection, versioning and memory above already describe this
                # exact export. Never publish a second snapshot at success only.
                # 过程沉淀(军团自评产品级优化):把本轮工具动作提炼成"我做了什么"过程卡随交付
                # 返回·治评委一致指出的 process_transparency 弱(黑盒)。emit 给前端即时渲染。
                _psum = _build_process_summary(_actions, elapsed, _model, arts)
                _ret["process_summary"] = _psum
                emit({"t": "process_summary", "phase": "过程回看", **_psum})
                emit({"t": "phase", "phase": "复检", "x": "复检交付内容与产物"})
                try:
                    import task_memory as _tm
                    _tm.update(_memory_request_id, key, phase="复检", artifacts=arts,
                               completed_steps=_actions, workdir=str(result.get("workdir") or ""))
                except Exception:
                    pass
            except Exception:
                pass
            # 所有 Harness 共用同一协作协议解码；旧 operation-map 继续兼容。
            # v1 envelope 由运行层绑定任务身份，任何结构错误都原子拒绝而非执行部分操作。
            if isinstance(result.get("orchestration_request"), dict):
                import collaboration_runtime as _collaboration_protocol
                _request, _protocol = _collaboration_protocol.decode_orchestration_request(
                    result["orchestration_request"],
                    team_id=str(task_facts.get("team_id") or qhash),
                    task_id=str(task_facts.get("task_id") or key),
                    correlation_id=qhash, harness=key)
                _ret["orchestration_request"] = _request
                _ret["collaboration_protocol"] = _protocol
                if _protocol.get("encoding") == "collaboration-envelope/v1" and _protocol.get("errors"):
                    # A native answer may be useful evidence, but an invalid atomic envelope is not
                    # a completed collaboration. Preserve the raw request for same-session repair.
                    _ret["raw_orchestration_request"] = copy.deepcopy(result["orchestration_request"])
                    _ret.update(status="error", error="collaboration_protocol_invalid",
                                protocol_code="collaboration_protocol_invalid")
            # This outlet also carries a completed native turn with no final
            # message (partial). Observe a newly arrived budget after export;
            # the shared helper leaves genuinely successful results unchanged.
            return result if pause_for_budget(_ret) else _ret

        # Current error envelopes were honored above. Recovered historical
        # failures belong in native_facts, not the current error channel.
        if _native_ee.requires_provider_pause(
                {"status": status, "error_envelope": boundary_envelope}):
            _authorization_denied = _native_ee.is_provider_authorization_denial(boundary_envelope)
            last = {"status": "error", "final": final,
                    "error": str(result.get("error") or boundary_envelope.get("message") or status),
                    "fail_kind": boundary_category, "error_envelope": boundary_envelope}
            if result.get("transport_failures"):
                _actions.append({"t": "transport_failure_evidence",
                                 "records": result["transport_failures"]})
            try:
                write_memory_facts(phase=("等待管理员处理授权" if _authorization_denied
                                         else "等待提供方退避"), error_envelope=boundary_envelope,
                                   completed_steps=_actions)
            except Exception:
                pass
            prior_attempts.append({
                "attempt": executed_attempts, "model": model_override or "", **last,
                "native_facts": native_facts, "actions": _actions,
                "usage": result.get("usage"), "workdir": str(result.get("workdir") or "")})
            emit({"t": "result", "x": ("提供方已拒绝本次授权；需管理员处理，本请求不自动重试。"
                                     if _authorization_denied else
                                     "提供方要求退避；本请求不立即换模型或凭据重试"),
                  "error_envelope": boundary_envelope})
            break

        if (status in _noncompletion_statuses and not _raw_error and not _supplied_envelope
                and not runtime_stop and not context_overflow):
            # A native non-completion is a runtime fact, not a failed model or
            # permission to promote a nonempty draft to done. Return it to the
            # existing coordinator with the same files/checkpoint and content.
            preserve_native_state = True
            last = {"status": status, "final": final, "error": "", "fail_kind": "",
                    "error_envelope": None}
            _last_native["reported_status"] = status
            prior_attempts.append({
                "attempt": attempt, "model": model_override or "", **last,
                "native_facts": native_facts, "actions": _actions,
                "usage": result.get("usage"), "workdir": str(result.get("workdir") or "")})
            try:
                import task_memory as _tm
                _tm.update(_memory_request_id, key, phase="成员尚未报告完成",
                           reported_status=status, completed_steps=_actions)
            except Exception:
                pass
            break

        # error 落一句人能看懂的原因(别把底层 status="ok" 原样当 error·100场景 cx023 就显示成
        # 误导性的 error='ok')。stall/截断这类"status ok 但没真交付"要说清到底哪没通。
        _err = result.get("error") or (_raw_error if _supplied_envelope else "")
        if not _err and result.get("stderr"):
            # adapter(如 hermes dist fallback)失败时只带 stderr 不带 error——不透传的话
            # 上层只会显示"空交付",真实 CLI 错误(缺登录/参数不支持/网络)全部丢失,无法归因。
            _err = f"CLI stderr: {str(result['stderr'])[-300:]}"
        if not _err:
            _err = ("只给计划句未真交付" if stalled
                    else "思考链截断·正文未写完" if status == "truncated_reasoning"
                    else "空交付" if not final.strip()
                    # Keep legacy text markers, but label this derived diagnostic:
                    # final text is not a native provider error JSON envelope.
                    else f"CLI final output: {final}" if gateway_issue
                    else status)
        # 先归一失败，再决定是否能归因给模型；环境/进程/Harness/工具/用户失败不能污染模型排序。
        # 网关失败统计(失败率↑→动态重试次数↑·治"2个模型恰好都限流就误判失败")+ 限流 appid 冷却
        # (429/high demand/限流):把这个 appid 短暂标冷却,并发下别的 worker 换未冷却 appid,
        # 不再全撞同一个(用户 2026-08-29 核心诉求·依据 model router 多 appid 池)。
        if gateway_issue:
            # 请求投影超窗是本地预算适配问题，不是 provider 健康下降；不能污染动态限流状态。
            if not context_overflow:
                _record_gateway_call(False)
            _low = (str(_err or "") + " " + final).lower()
            if appid_override and not context_overflow and any(
                    s in _low for s in ("429", "high demand", "限流", "rate limit", "quota", "每分钟")):
                _cooldown_appid(appid_override)
                emit({"t": "reason", "x": f"⏳ appid {str(appid_override)[:8]}… 触发限流·冷却{_APPID_COOLDOWN_S}s·下轮换 appid"})
        # 结构化失败信封优先；文本分类仅给尚未升级的第三方 adapter 兜底。
        try:
            import error_envelope as _ee
            if context_overflow:
                _envelope = _ee.normalize(
                    status, str(_err or final), "context_overflow",
                    boundary_envelope if boundary_category == "context_overflow"
                    else result.get("error_envelope")).to_dict()
                _fk = "context_overflow"
            else:
                _env = _ee.normalize(status, str(_err or ""), str(result.get("fail_kind") or ""),
                                     result.get("error_envelope"))
                _envelope = _env.to_dict()
                _fk = _envelope["category"]
        except Exception:
            _fk = _classify_failure(status, str(_err or ""), str(result.get("fail_kind") or ""))
            _envelope = {"category": _fk, "retryable": _looks_like_gateway_issue(status, final),
                         "scope": "task", "tool": "", "code": status, "message": str(_err or "")}
        if pause_for_budget({**result, "status": "error", "final": final,
                             "error": str(_err or ""), "error_envelope": _envelope,
                             "execution_calls": executed_attempts}):
            break
        # context_overflow 反映请求投影，不代表该模型质量差；record_model_health 会按类别忽略它。
        record_model_health(key, model_override or "", ok=False, failure_category=_fk)
        last = {"status": status, "final": final, "error": _err, "fail_kind": _fk,
                "error_envelope": _envelope}
        if result.get("transport_failures"):
            _actions.append({"t": "transport_failure_evidence",
                             "records": result["transport_failures"]})
        try:
            write_memory_facts(phase="自救处理中", error_envelope=_envelope,
                               completed_steps=_actions)
        except Exception:
            pass
        prior_attempts.append({
            "attempt": attempt,
            "model": model_override or "",
            "status": status,
            "fail_kind": _fk,
            "error_envelope": _envelope,
            "error": str(_err)[:300],
            "final": final[:500],
            "usage": result.get("usage"),
            "context_budget": dict(result.get("context_budget") or {}),
            "actions": _actions[-50:],           # 结构化候选/工具/验证链；有界保留最近50条供自救与接力
            "workdir": str(result.get("workdir") or ""),
        })
        # 把这一轮的真实失败/半成品回流进下一轮 task(codex/deepseek 式:失败进 history 喂回)。
        # 模型下一轮会读到"我上次怎么干、结果怎样",从而自己换 UA/换工具/换角度,而不是重复。
        failure_context = _build_failure_context(failure_context, attempt, key,
                                                 model_override, status, final, stalled,
                                                 error=str(_err or ""), actions=_actions)
        # 转圈检测(军团 cc 首脑复核 D1 揪出:run_one 重试循环缺 _no_progress·某家每轮吐
        # 同样的半截/报错会把整个模型池空烧一遍才停)。学 deepseek repeat-tool-reminder:
        # 只【提前收敛】不否决——连续 2 轮【结构化指纹】完全相同=真转圈,不再换模型白烧,
        # 直接收尾返回最后结果(仍是失败态·但省掉后续无意义 attempt 的 token/时间)。
        # 指纹=final+status+error 规范化(codex R1 揪出:旧版只比 final·_no_progress 要两边非空
        # →【连续空 final 且同错】的转圈漏检)。codex R2 又揪出:指纹不能用 prior_attempts 的截断
        # 展示字段(final[:500]/error[:300])·否则两次输出前 500 字相同、后文不同会误判转圈——
        # 这里用【本轮完整 final/error】建指纹存进 _spin_fps,与截断展示字段解耦。
        n = lambda s: " ".join(str(s or "").lower().split())
        # 历史(已废):曾试"指纹纳入 model"(想让换模型不算转圈·护跨方向兜底),但 hermes R3 揪出
        # 核心矛盾——重试总换模型→含 model 指纹永不相同→转圈检测形同虚设。当前正确做法见下:
        # 转圈指纹【不含 model】(hermes 首脑复核 R3):重试总是换模型·若把 model
        # 混进指纹→换模型永远=不同指纹=永不收敛·转圈检测形同虚设(gateway≥3轮跑满5轮·非gateway跑满池)。
        # 正确语义:真转圈=换了模型但【输出/错误完全一样】(换模型没用·白烧)→该收敛;有意义推进=
        # 换模型后输出【不同】(在迭代)→不收敛。所以指纹看输出不看模型。
        #  ① 完整指纹(final+status+err):非基础设施失败用·连续 2 轮同输出即收敛(同样半截答案换模型还是半截)。
        #  ② infra 指纹(只 status+err):gateway/process 基础设施失败用·连续 ≥3 轮同基础设施错才收敛——
        #     给 appid 冷却+换 appid 前 2 轮机会(deepseek R2·不 2 轮掐断)·3 轮都同 503=网关真挂了才停(hermes R3)。
        _spin_fps.append("\x1f".join((n(final), n(status), n(_err))))
        # infra 指纹【只看 status·不看 error 文本】(用户 2026-09-01 真跑揪出:codex 环境失败 rc1
        # 换 27 个模型空烧整池·根因=infra 指纹含 error 文本·而 error 每轮带不同模型名/细节→指纹永
        # 不相同→3 轮 infra 收敛形同虚设→跑满整个模型池)。铁律0:环境/进程级失败(rc1/rc-9/process)
        # 换模型救不了·status 稳定(err:worker_no_json:rc1)·连续 3 轮同 status 就该收敛·不被 error
        # 文本的变化细节骗过。error 细节仍进完整指纹(_spin_fps)供非 infra 路径用。
        _spin_infra_fps.append(n(status))
        # rate_limit(429/限流)也是基础设施抖动·最需要 appid 冷却+换 appid 的 ≥3 轮机会
        # (codex 首脑复核 R4:429 归 rate_limit·旧版只认 gateway/process→429 走 2 轮收敛·绕过 infra 保护)。
        _is_infra = str(_envelope.get("category") or "") in (
            "gateway", "process", "rate_limit", "harness_runtime", "context_overflow")
        if _is_infra:
            _need, _fps = 3, _spin_infra_fps        # 基础设施失败:infra 指纹·≥3 轮
        else:
            _need, _fps = 2, _spin_fps              # 其它:完整指纹(不含 model)·≥2 轮
        if len(_fps) >= _need:
            _recent = _fps[-_need:]
            # 最近 _need 轮指纹全相同 = 真转圈。final 与 error 都空(全无信息·只 status)不算转圈,
            # 交由预算自然收敛(deepseek R2:旧 strip("\x1f ") 恒真是死代码·status 恒非空)。
            _has_info = bool(n(final) or n(_err))
            _spin = _has_info and all(fp == _recent[0] for fp in _recent)
            # codex R4:infra 指纹忽略 final·若 gateway/process 每轮吐【变化的 partial output】其实在推进,
            # 不该按 infra 3 轮停。加一层:infra 收敛时,若最近 _need 轮 final 有实质变化(非空且不全同)→
            # 判为推进·不收敛(与"输出不同即推进"的全局语义一致)。空/噪声 final 不受影响(仍按 infra 收敛)。
            if _spin and _is_infra:
                _recent_finals = [n(f) for f in ([x.split("\x1f")[0] for x in _spin_fps[-_need:]])]
                if any(_recent_finals) and len(set(_recent_finals)) > 1:
                    _spin = False   # final 在变=推进·不收敛
            if _spin:
                # ★ 真转圈=换了模型但【输出/错误完全一样】(连续 _need 轮指纹相同·指纹不含 model)。
                # 这说明【换模型对这个任务没用】——同样的"半截答案"/同样的 rc1·再换第4/5个模型
                # 极大概率还是一模一样(病在任务本身/该 harness·不在模型)。此时继续换纯烧时间·
                # 直接【挡速度底线】(用户 2026-09-01 钉死:速度快是不可触碰的底线·谁挡降级谁)。
                # 故:真转圈即收敛·不再"因为还有未试模型就继续"(那会让"半截答案×N"跑满整个模型池·
                # 违反速度底线·且对交付率零帮助——换模型输出还相同=模型不是瓶颈)。
                # 注意:输出【有变化】(在迭代推进)本就进不到这里(_spin 要求 _need 轮指纹全同·line 1840),
                # 所以这个收敛只掐"真原地打转"·不误伤"换模型后有新进展"的正常自救。
                emit({"t": "reason", "x": f"连续 {attempt} 轮换模型输出/错误完全相同=换模型无效→ 收敛(不空烧·保速度底线)"})
                break
        if context_overflow:
            context_retry_target = (model_override, appid_override)
            _budget = dict(result.get("context_budget") or {})
            emit({"t": "context_compacted", "phase": "压缩现场", "status": "retrying",
                  "context_budget": _budget,
                  "x": "输入超过当前模型窗口；完整现场仍在任务存储中，下轮将用更紧的分层投影自动续跑"})
        # 本地 runtime/router/bridge 故障和模型无关，换模型救不了；立即交给上层换 transport/换家。
        if str(_envelope.get("category") or "") == "harness_runtime":
            emit({"t": "result", "x": f"本地运行链路故障·换模型无效:{str(_err)[:160]}",
                  "error_envelope": _envelope})
            break
        # 托词/半成品 → 值得再给一轮(带回流)让它真做出来,即使 status=ok。
        # 纯任务级错(未知家等)且非托词 → 不浪费重试。
        if runtime_stop or (not stalled and not bool(_envelope.get("retryable"))):
            emit({"t": "result", "x": f"未跑通({_envelope.get('category')}·不可自动重试):{status}",
                  "error_envelope": _envelope})
            break
        why = "只给了计划句没真交付" if stalled else f"网关/模型不通:{status[:80]}"
        emit({"t": "reason", "x": f"第 {attempt} 轮{why} → 把失败回流给下一轮让它换招"})

    elapsed = round(time.time() - t0, 2)
    tried = "、".join(tried_models) or "默认模型"
    emit({"t": "result", "x": (
        "用户停止；已停止后续执行，正在核对可保留的成果"
        if last.get("status") == "cancelled" else
        f"成员报告 {last['status']}；现有内容与现场已保留，尚未报告完成"
        if preserve_native_state else last["error"] if result.get("blocked_before_native")
        else f"已试 {tried} 仍未通,最后:{last['status'][:80]}" )})
    failed_result = {"key": key, "harness": key,
                     "status": last["status"] if preserve_native_state else
                               "timeout" if last["status"] == "timeout" else "error",
                     "final": last["final"], "elapsed": elapsed,
                     "error": ("" if preserve_native_state else last["error"] if not executed_attempts
                               else f"{last['error']}(已试模型:{tried})"),
                     # 军团⑥失败分类 + ③动作清单:让前端区分卡死/跑太久/信号杀·让接力家看到历次工具动作。
                     "fail_kind": "" if preserve_native_state else last.get("fail_kind") or "task_error",
                     "error_envelope": last.get("error_envelope"),
                     "prior_attempts": prior_attempts,
                     "context_budget": dict(result.get("context_budget") or {}),
                     "execution_depth": execution_depth,
                     "execution_depth_history": list(execution_depth_history),
                     "execution_depth_upgraded": depth_upgraded,
                     "tool_trace": [action for batch in action_batches for action in batch][-100:],
                     "workdir": str(result.get("workdir") or ""),
                     "model": (tried_models[-1] if tried_models else "默认"),
                     "attempts": max(0, executed_attempts - int(depth_upgraded)),
                                "execution_calls": executed_attempts, "tried_models": tried_models,
                     **_last_native}
    if result.get("blocked_before_native"):
        failed_result["blocked_before_native"] = True
    if preserve_native_state:
        if _orch_request:
            failed_result["orchestration_request"] = dict(_orch_request)
        for field in ("model_used", "requested_model"):
            if field in result:
                failed_result[field] = result[field]
    if last.get("status") == "cancelled":
        failed_result.update(status="cancelled", cancelled=True,
                             error=str(last.get("error") or "用户停止"),
                             partial=bool(result.get("partial")))
        failed_result = _with_cancel_native_reference(failed_result)
    _attach_task_usage(failed_result, collect=False)
    checked_reported_artifacts = []
    references = list(native_reported_artifacts)
    if preserve_native_state or references:
        # A runtime pause must not drop explicitly reported files just because
        # they predate this attempt. Reuse the existing authorized exporter,
        # never promote an unchecked native path/URL directly.
        if references or project_context.get("workspace_root") or project_context.get("repo_root"):
            # Reported identities are observations, never a new download root.
            # Do not forward preview/content or unvalidated active URLs here.
            import artifact_delivery as _partial_delivery
            # Private-marked references must not become public merely because
            # the exporter was given only their path. A mixed selection fails as
            # one unit, and never falls back to a broader project scan. The bound
            # root is only for path classification, never an export grant.
            project_root = str(project_context.get("repo_root") or project_context.get("workspace_root") or "")
            classified = [(item, _partial_delivery._is_private_artifact(item, project_root=project_root))
                          for item in references]
            private_selection = any(private for _, private in classified)
            failed_result["reported_artifacts"] = [
                {field: item[field] for field in ("name", "kind", "bytes", "sha256")
                 if field in item and isinstance(item[field], (str, int, float, bool))
                 and not _partial_delivery._content_safety.sensitive_text(str(item[field]))}
                for item, private in classified if not private]
            paths = [item["path"] for item in references if isinstance(item.get("path"), str)]
            workdir = project_root or str(result.get("workdir") or "")
            if project_context.get("workspace_root") or project_context.get("repo_root"):
                # Reuse the exact authenticated exporter. It rereads ownership,
                # selection and revocation before touching reported file bytes.
                team = str(task_facts.get("team_id") or qhash)
                node = str(task_facts.get("task_id") or key)
                try:
                    if private_selection:
                        raise _partial_delivery._content_safety.ArtifactUnsafe("artifact_private_reference")
                    export_conversation = _partial_delivery.project_export_conversation(
                        project_context, team_id=team, task_id=node, user_id=user_id,
                        harness_key=key, native_conversation_id=conversation_id, workdir=workdir)
                    exported = _partial_delivery.export_project_artifacts(
                        paths, team_id=team, task_id=node, user_id=user_id,
                        conversation_id=export_conversation,
                        requires_operator_grant=_require_project_authorization)
                except _partial_delivery._content_safety.ArtifactUnsafe as exc:
                    exported = {"artifacts": [], "artifact_safety_observations": [
                        _partial_delivery._not_exported(str(exc))]}
                except Exception as exc:
                    observation = _partial_delivery._not_exported("artifact_export_unavailable")
                    observation.update(
                        stage="project_export", exception_type=type(exc).__name__,
                        diagnostic=redact_native_diagnostic(str(exc), _diagnostic_env)[:400])
                    exported = {"artifacts": [], "artifact_safety_observations": [observation]}
                notes = list(exported.get("artifact_safety_observations") or [])
                arts = list(exported.get("artifacts") or [])
                arts = record_artifact_facts(
                    failed_result,
                    {"artifacts": arts, "artifact_safety_observations": notes,
                     "artifact_export": {"source": "authorized-project-export",
                                         "exported_files": len(arts), "observations": notes,
                                         "workdir": workdir}}, workdir)
                for item in arts:
                    emit({"t": "artifact_ready", "phase": "未完成现场保留",
                          **{field: item.get(field) for field in
                             ("name", "url", "bytes", "sha256", "export_snapshot_id",
                              "version", "provenance", "lineage")}})
                # An accepted/refused project selection must not be widened
                # afterwards by legacy memory, scan or native reference paths.
                return (result if last.get("status") != "cancelled" and pause_for_budget(failed_result)
                        else failed_result)
            if workdir and not private_selection:
                checked_reported_artifacts = _partial_delivery.extract(
                    "\n".join("`" + path + "`" for path in paths), roots=[workdir])
    # 失败时仍保留这次真实生成的产物，供接力家和最终恢复层验证/续做。状态仍是 error，
    # 绝不因“有任意文件”改成成功；只有下游按任务 delivery contract 真验过才可完成。
    saved_artifacts = list(checked_reported_artifacts)
    try:
        import task_memory as _tm
        saved = _tm.context(_memory_request_id, key)
        saved_artifacts.extend((saved or {}).get("artifacts") or [])
    except Exception:
        pass
    # A native failure can still leave useful files. Apply the same project
    # authority and refusal semantics as success, never revive stale sources
    # from task memory after an export was denied.
    saved_artifacts = collect_artifact_facts(failed_result, saved_artifacts)
    if saved_artifacts:
        failed_result["artifacts"] = saved_artifacts
        failed_result["partial"] = True
        for artifact in saved_artifacts:
            emit({"t": "artifact_recovered", "stage": "artifact_ready",
                  "phase": "未完成现场保留" if preserve_native_state else "失败现场恢复",
                  "status": "recovered", "name": artifact.get("name"), "url": artifact.get("url"),
                  "path": artifact.get("path"), "ext": artifact.get("kind"),
                  "bytes": artifact.get("bytes"), "provenance": artifact.get("provenance"),
                  "lineage": artifact.get("lineage"), "sha256": artifact.get("sha256"),
                  "export_snapshot_id": artifact.get("export_snapshot_id"),
                  "x": f"{key} 遇阻，但已保留产物 {artifact.get('name') or artifact.get('path')} 供接力复验"})
    if last.get("status") != "cancelled" and pause_for_budget(failed_result):
        return result
    return failed_result


def _dispatch_relay(question, plan, cb, qhash, user_id: str = "default",
                    conversation_id: str = ""):
    """军团协作(串行流水线):A 干完把产出交给 B 接着干,顺序传递,最后一家输出即成果。

    编排顺序:plan.pipeline 显式指定;没有则用 tasks 的键顺序。
    每一步都发 task 状态事件(pending→running→done),任务显式可见。
    """
    tasks = dict(plan.get("tasks") or {})
    media_ctx = _build_prelude(question, plan, cb, user_id=user_id)
    _rt_brief = _build_runtime_brief(question, plan, user_id=user_id)   # 同一真实用户的运行时简报
    valid = _harness_keys()
    order = [k for k in (plan.get("pipeline") or list(tasks.keys())) if k in valid]
    if not order:
        # 没指定就用默认快家串一条(codex 起草 → hermes 复核)
        runnable = set(_aigc_ready().get("runnable_clis", []))
        order = [k for k in ["codex", "hermes", "cc"] if k in valid and k in runnable][:2] or ["codex"]

    if cb:
        cb({"agent": "commander", "t": "dispatch", "mode": "relay",
            "x": f"军团协作(串行)：{' → '.join(order)}", "keys": order, "hierarchy": {}})
        cb({"agent": "commander", "t": "tasklist", "mode": "relay",
            "tasks": [{"key": k, "task": tasks.get(k) or "接力处理",
                       "step": i + 1, "status": "pending"} for i, k in enumerate(order)]})

    # 不设协作流水线父层总墙钟(去墙钟铁律):每步由该家自主收敛,run_one 单进程上限兜死进程。
    _dirs = dict(plan.get("directions") or {})
    _depths = dict(plan.get("execution_depths") or {})
    try:
        import model_registry
        _assignments = model_registry.resolve_assignments(plan, order)
    except Exception:
        _assignments = {}
    results, prev_final = [], ""
    for i, k in enumerate(order):
        # 用户取消(军团自审 P0):流水线每步前查·整单喊停就停在这·不再往下接力(保留已完成步)。
        if is_cancelled(qhash):
            if cb:
                cb({"agent": "commander", "t": "reason", "x": "🛑 用户停止·协作流水线在此中止"})
            break
        if cb:
            cb({"agent": k, "t": "task_status", "status": "running"})
        role = tasks.get(k) or ("起草初稿" if i == 0 else "在上一步成果上继续加工")
        # 上一步成果完整传给下一家(不截断·复杂交付物接力时半截会毁掉后续加工)
        upstream = f"\n上一步（{order[i-1]}）的成果：{prev_final}" if i > 0 and prev_final else ""
        task = f"用户问题：{question}\n你负责（协作第{i+1}步）：{role}{upstream}{media_ctx}"
        _as = _assignments.get(k, {})
        _upstream_refs = []
        if results:
            _upstream_refs.extend(results[-1].get("artifacts") or [])
            _upstream_refs.extend(results[-1].get("discovered_artifacts") or [])
        try:
            from legion_contracts import CapabilityPolicy, TaskFacts, compile_orchestration_plan
            _compiled_plan, _relay_spec = compile_orchestration_plan(plan, "relay")
            _node_spec = (_compiled_plan.get("tasks") or {}).get(k) or {}
            _task_facts = TaskFacts(
                goal=question, role=role, task_id=k, team_id=qhash,
                input_hash=str(_node_spec.get("input_hash") or ""),
                idempotency_key=str(_node_spec.get("idempotency_key") or ""),
                delivery_contract=dict(_compiled_plan.get("delivery_contract") or {}),
                orchestration=_relay_spec.to_dict(),
                deliverables=list(plan.get("deliverables") or []),
                acceptance=list(plan.get("acceptance") or []) if isinstance(plan.get("acceptance"), list)
                           else ([str(plan.get("acceptance"))] if plan.get("acceptance") else []),
                upstream_artifacts=_upstream_refs,
                upstream_results=([{"key": results[-1].get("key"),
                                    "status": results[-1].get("status"),
                                    "error_envelope": results[-1].get("error_envelope")}]
                                  if results else []),
            ).to_dict()
            _cap_policy = CapabilityPolicy.from_value(plan.get("capability_policy") or {}).to_dict()
        except Exception:
            _task_facts, _cap_policy = {}, {}
        r = run_one(k, task, qhash=qhash, cb=cb,
                    preferred_model=_as.get("model"), preferred_appid=_as.get("appid"),
                    attempt_timeout=None, direction=_resolve_direction(plan, k), user_id=user_id,
                    conversation_id=conversation_id,
                    runtime_brief=_rt_brief, task_facts=_task_facts,
                    capability_policy=_cap_policy,
                    execution_depth=str(_depths.get(k) or "standard"))
        results.append(r)
        if r["status"] == "done":
            prev_final = r["final"]  # 传递给下一家
        if cb:
            cb({"agent": k, "t": "task_status",
                "status": "done" if r["status"] == "done"
                          else ("cancelled" if r["status"] == "cancelled" else "error")})
        if r["status"] == "cancelled":
            break  # 这一步被取消(跑到一半喊停)→ 停止流水线·不再接后续步

    delivered = sum(1 for r in results if _is_delivered(r))
    cancelled_count = sum(1 for r in results if r.get("status") == "cancelled")
    whole_cancelled = is_cancelled(qhash)
    delivered_results = [r for r in results if _is_delivered(r)]
    tail_result = results[-1] if results else None
    # 正常接力仍以末端产出为最终成果；末端家若失败，不能把上游已经交付的真成果也抹掉并谎称“全失败”。
    # 像流水线最后一道包装机坏了：保留前一道已经做好的产品给用户，并诚实标 partial。
    final_result = tail_result if _is_delivered(tail_result) else (delivered_results[-1] if delivered_results else tail_result)
    partial = bool(delivered and delivered < len(results))
    if cb:
        if whole_cancelled:
            _msg = f"协作流水线已停止：完成 {delivered} 步，用户在中途停止"
        elif partial:
            _msg = (f"协作流水线部分交付：{delivered}/{len(results)} 步成功，"
                    f"末端遇阻，已保留 {final_result.get('key') if final_result else '-'} 的有效成果")
        else:
            _msg = (f"协作流水线完成：{delivered}/{len(results)} 步交付，"
                    f"最终成果来自 {final_result.get('key') if final_result else '-'}")
        cb({"agent": "commander", "t": "summary", "x": _msg,
            "done": delivered, "total": len(results), "partial": partial,
            "cancelled": whole_cancelled, "cancelled_count": cancelled_count})
    _record_experience(question, results, user_id=user_id)  # 跨家经验落盘(双路径对称·和pk一致·按user隔离)
    try:
        from legion_contracts import compile_orchestration_plan
        _compiled, _spec = compile_orchestration_plan(plan, "relay")
        _delivery = _compiled.get("delivery_contract") or {}
        _outcome = _typed_outcome(results, whole_cancelled, _delivery)
    except Exception:
        _spec, _delivery = None, {}
        _outcome = _typed_outcome(results, whole_cancelled)
    return {"results": results, "mode": "relay", "final_result": final_result,
            "pipeline": order, "gold_unknown": True,
            "delivered_count": delivered, "total": len(results),
            "partial": _outcome.get("status") == "partial",
            "cancelled": whole_cancelled, "cancelled_count": cancelled_count,
            "all_failed": delivered == 0 and not whole_cancelled,
            "orchestration_spec": _spec.to_dict() if _spec else {},
            "delivery_contract": _delivery, "outcome": _outcome,
            "contract_incomplete": bool(_outcome.get("contract_incomplete"))}


# ── 派军团:两种模式 ──────────────────────────────────────────────────
#   mode="pk"    军团 PK(并行):多家同时干活,并列结果,用户/裁判比最佳(默认)
#   mode="relay" 军团协作(串行):一条流水线,A 干完把产出交给 B 接着干,顺序传递
def _attachment_path(attachment) -> Optional[str]:
    """归一前端上传对象与旧纯路径格式,供规划器/派活prelude共用(单一事实源)。"""
    if isinstance(attachment, str):
        return attachment
    if isinstance(attachment, dict):
        for key in ("path", "file_path", "filepath", "local_path", "abspath"):
            value = attachment.get(key)
            if value:
                return str(value)
    return None


def _media_prelude(question: str, attachments: list, cb=None) -> str:
    """附件只作为【上下文+工具入口】给模型,不在父进程预跑/代选工具(context not control)。

    以前按后缀自动 read_pdf/see_image/video_frame=父进程替模型选路,已删除。
    现在告诉模型:有什么文件、类型、可用命令;模型读完自己决定调哪个、调几次。
    """
    import file_hub
    # 归一附件格式(修上游断链):前端 /api/legion/upload 返回的是 dict {path,url,kind,...},
    # 用户对话时原样把这些 dict 数组当 attachments 传回。旧代码直接 os.path.isfile(dict) → 崩
    # TypeError,任何带附件的 query 还没进军团就 500。用 _attachment_path 归一,取不出的静默跳过。
    atts = []
    for a in (attachments or []):
        p = _attachment_path(a)
        if p and os.path.isfile(p):
            atts.append(p)
    if not atts:
        return ""
    media_cli = os.path.join(HERE, "media_tools.py")
    file_cli = os.path.join(HERE, "file_hub.py")
    lines = ["\n\n【用户上传的文件·请根据任务自主选择工具读取/分析】"]
    for p in atts[:10]:
        kind = file_hub.kind_of(p)
        lines.append(f"- {p}  (类型:{kind})")
    lines += [
        "工具入口不是授权或可用性证明；配置与可用性以本次工具结果为准:",
        f"  PDF: python3.12 {media_cli!r} read_pdf 'PATH'",
        f"  图片: python3.12 {media_cli!r} --json see_image 'PATH' 'QUESTION'",
        f"  图片转视频: python3.12 {media_cli!r} gen_video 'PROMPT' 'PATH' '5' '16:9'  # 文生视频(无PATH)走Seedance方舟真AI通道约3-5分钟;带PATH图生视频云不可用回退本地运镜(degraded=true,交付须如实说明)",
        f"  查看视频某帧: python3.12 {media_cli!r} video_frame 'PATH' 'SECONDS'",
        f"  Excel/代码/其它: python3.12 {file_cli!r} 'PATH'",
        "你自己判断是否需要读取、读哪个、如何结合用户目标;父进程没有替你做判断。",
    ]
    if cb:
        cb({"agent": "commander", "t": "reason", "x": f"已把 {len(atts)} 个附件路径+工具入口交给各家自主处理"})
    return "\n".join(lines)


_TOOL_HINT_CACHE: dict[str, Optional[str]] = {"txt": None}


def _toolbox_hint() -> str:
    """Stable, lazy capability pointers, not runtime discovery or a task recipe.

    Rendering a prefix must not enumerate every tool or resolve an unused
    retained runtime. Actual availability and source paths come from discovery
    results when the native harness chooses to request them.
    """
    cached = _TOOL_HINT_CACHE["txt"]
    if cached is not None:
        return cached
    tool_cli = os.path.join(HERE, "legion_tool.py")
    txt = (f"\n\n【军团共享能力·按需发现】\n"
           f"  能力与实际状态：python3.12 {tool_cli!r} run runnable:capability_catalog '{{}}'。"
           f"可按当前任务检索工具；中文未命中时可换中英能力短语。"
           f"工具登记、安装、配置和当次可用性分开，以真实返回为准；"
           f"这些资料不为只读或无工具接口增加执行权限。\n"
           f"  共享 Skill：python3.12 {tool_cli!r} run runnable:skill_finder "
           f"'{{\"query\":\"所需能力\",\"scope\":\"local\"}}' 查各家源码和本包已安装的 Skill，"
           f"按返回的 skill_md 读取。action:list 仅列个人已启用项，空列表不代表共享库为空；"
           f"外部线索可用 scope:external/all。func_search 可查询同伴源码与可借用函数；"
           f"是否搜索、复用、安装或自行实现，由你按目标、证据、成本和授权判断，不是固定顺序。\n"
           f"  部署交付:run runnable:deploy_site "
           f"'{{\"path\":\"产物\",\"mode\":\"local\"}}' 仅做静态文件地址映射并返回URL；"
           f"未托管应用进程，HTTP/业务可用性尚未测试，访问仍遵循既有服务授权"
           f"(公网发布需 mode:public+confirm)。")
    _TOOL_HINT_CACHE["txt"] = txt
    return txt


# 云真机状态探测走多条 docker 命令(_desktop_snapshot+_cua_control)·真实测 2.6s·每次 dispatch 都在
# prelude 里被调→用户每次派活白等 2.6s(真实 server 探针揪出:prelude 2.24s 大头全在这)。prelude 只需
# 一句"云真机存在+当前状态"给军团当现场事实·30s 陈旧完全可接受(启停是慢事件)。加 30s TTL 缓存·
# 前端云真机面板走 cloud_desktop.status() 那条实时路径不受影响(实时性不变)。
_DESKTOP_STATUS_TTL = 30.0
_DESKTOP_STATUS_CACHE = {"txt": None, "ts": 0.0}
_DESKTOP_STATUS_LOCK = threading.RLock()


def _desktop_status_txt() -> str:
    def _refill():
        status_txt = "状态未知"
        try:
            import cloud_desktop
            snap = cloud_desktop.status("default")
            desktop = snap.get("desktop") or {}
            if snap.get("desktop_ready"):
                status_txt = "已就绪(可直接执行)"
            elif (snap.get("docker") or {}).get("daemon_ready") is False and not snap.get("warming"):
                status_txt = "不可用：Docker 执行服务未就绪；安装客户端不等于已有可运行的云桌面"
            elif desktop.get("status") in ("stopped", "image_ready", "image_pending", "image_available", "display_starting"):
                status_txt = "未运行(可先 desktop_start 启动)"
            else:
                status_txt = str(desktop.get("status") or "不可用")
        except Exception:
            status_txt = "探测失败：设备是否存在及可用均未确认"
        with _DESKTOP_STATUS_LOCK:
            _DESKTOP_STATUS_CACHE["txt"] = status_txt
            _DESKTOP_STATUS_CACHE["ts"] = time.time()
    with _DESKTOP_STATUS_LOCK:
        cached = _DESKTOP_STATUS_CACHE["txt"]
        fresh = cached is not None and (time.time() - _DESKTOP_STATUS_CACHE["ts"] < _DESKTOP_STATUS_TTL)
    if fresh and isinstance(cached, str):
        return cached
    # SWR:过期有旧值→立即返旧值+后台异步重填。
    # 速度底线(用户 2026-09-01 骂醒:9.3s 前置静默里 desktop 探测占 3.5s):首次(cached is None)
    # 也【不同步阻塞】——立即返"探测中"占位 + 后台探 Docker。这个状态只是给 harness 的【存在性
    # 提示 context】(用不用它自己判断·真用时会 mcp_list 查实时),占位完全够;绝不让每单为它等 3.5s。
    _swr_bg_refresh("desktop_status", _refill)
    return cached if cached is not None else "探测中：尚未确认有可用桌面"


def _desktop_context() -> str:
    """云真机现场简报：入口存在不等于已配置可用设备。
    这里只给当前状态与已有接口,
    用不用、怎么用,军团各家模型自己判断。
    """
    status_txt = _desktop_status_txt()
    cli = os.path.join(HERE, "legion_tool.py")
    return (
        chr(10)*2 + "【用户云真机现场(执行环境·按需自用)】" + chr(10) +
        "平台提供 CUA 云桌面接入能力；实际设备状态：" + status_txt + "." + chr(10) +
        "通过 MCP 服务器 cua-local-desktop 的工具控制它(先 mcp_list 查 schema):" + chr(10) +
        "  python3.12 " + repr(cli) + " run runnable:mcp_list {}" + chr(10) +
        "  · desktop_exec: 在真机内执行 shell 命令(工作目录=/root/Meyo)" + chr(10) +
        "  · desktop_app: 列出/打开/关闭真机桌面软件(list/open/close)" + chr(10) +
        "  · computer_command: 官方 CUA computer-server 命令(screenshot/left_click/type_text/press_key/run_command/open)" + chr(10) +
        "  · desktop_open_file: 把 ~/Meyo 交付物在真机桌面真实打开(按扩展名选 GUI 应用)" + chr(10) +
        "  · desktop_fs_list/read/write/mkdir: 读写用户 ~/Meyo 持久卷文件" + chr(10) +
        "  · desktop_screenshot: 截取真机屏幕(保存进 ~/Meyo/personal/screenshots)" + chr(10) +
        "以上是接口说明，不是设备已就绪的证明；文件落盘与本地代码校验不依赖云桌面。" + chr(10) +
        "设备失败信息和已有产物属于当前现场；无法执行的 GUI 验证需如实标明，不能冒充已验证。")


def _delivery_context() -> str:
    """提供交付入口与自验能力，不把一种历史文件用法说成所有用户的限制。

    历史图表案例的 file:// CORS 问题仍需验证，但只适用于承诺本地双击的交付；
    不能由此替用户规定所有网页、联网服务或工程都必须采用同一种打开方式。
    """
    # 只提供当前确实存在的文件校验入口，不把它变成所有交付的固定阶段。
    verify_cli = os.path.normpath(os.path.join(HERE, "..", "..", "dev", "verify_web_delivery.py"))
    verify_hint = ""
    if os.path.exists(verify_cli):
        verify_hint = (
            "- 若本单选择本地 HTML 下载/双击入口，可用已有工具真开 file:// 自验"
            "（不是 grep 关键字）：\n"
            f'    python3.12 {verify_cli!r} <你产出的html绝对路径>   # 退出码0=双击真能渲染·非0=空白/报错\n'
            "  若该入口打不开，应修复与其承诺不符的问题；若本单采用其他入口，"
            "此文件校验不替代对那个入口的真实使用验证。\n")
    return ("\n\n【交付入口与实际使用方式】\n"
            "- 以本单明确的用户要求、真实使用场景和实际可用入口决定交付方式，"
            "不要把已有文件名或上一轮制作选择自动当成用户不可改变的限制。\n"
            "- 交付目标仍是【用户拿到就能用】：本地文件、联网服务、可运行工程"
            "各自需要真实可用的入口和必要配套，不以代码、地址或说明的存在冒充已经可用。\n"
            "- 对承诺本地双击的 HTML，应验证 file:// 下的资源加载与交互；"
            "这不自动要求所有项目都采用单文件或零依赖。"
            "网络和工程交付也不意味着平台已经提供托管；尚未运行、部署或需授权之处应如实说明。\n"
            + verify_hint +
            "具体实现与验证方式由你依据原目标和当前事实选择；用户明示要求及既有权限边界保持不变。")


def _build_runtime_brief(question: str, plan: dict, user_id: str = "") -> str:
    """运行时现场简报(军团亲口点名缺的 context·context not control:只给现场事实让它自判):
      · 本单派了哪些家、谁挂谁下面(静态分工·cc 已纠正不做横向实时消息)
      · 机器/网络现场(平台·AIGC 网关是否可达——只说状态不吐 key)
      · 用户画像 / 验收标准(plan 里带则透传·没有则不编)
    每条都是"给信息不设限":harness 读完自己决定交付深浅/是否重复别家/按验收自检。空信息不拼。
    """
    lines = []
    tasks = plan.get("tasks") or {}
    hier = plan.get("hierarchy") or {}
    if tasks:
        who = []
        for k in tasks:
            parent = hier.get(k)
            who.append(f"{k}（挂在 {parent} 下）" if parent else k)
        lines.append(f"- 本单军团分工：{', '.join(who)}。你是其中一员，别重复别家已明确负责的部分。")
    import platform as _plat
    lines.append(f"- 运行机器：{_plat.system()} {_plat.machine()}；模型配置、提供方验证和工具授权是不同事实，"
                 "以本请求的实际绑定与调用结果为准。")
    lines.append("- 当前成员与共享能力索引（目录不是授权；能力互补由成员自主判断）："
                 + json.dumps(_selection_resource_context(plan, user_id), ensure_ascii=False))
    import legion_paths as _brief_paths
    browser_profile = (os.environ.get("MEYO_PLAYWRIGHT_RUNTIME_PROFILE")
                       or _brief_paths.under("config", "playwright-runtime.json"))
    browser_entry = os.path.join(_ORCH_DIR, "meyo_harnesses", "playwright_runtime.cjs")
    if os.path.isfile(browser_profile) and os.path.isfile(browser_entry):
        lines.append(f"- 共享浏览器已有运行材料配置；原生 Playwright MCP 通过现有 mcp_list/mcp_call 使用。"
                     f"确切入口/材料核对可读：node {browser_entry!r} --describe。"
                     "入口复用现有包与固定库/字体，显式保留沙箱；缺件会报错，不会自动安装。"
                     "这是已配置能力，不是本题已验；独立 MCP call 不承诺跨调用保留页面。")
    if plan.get("user_profile"):
        lines.append(f"- 用户画像（决定交付深浅）：{str(plan['user_profile'])[:300]}")
    # 验收标尺(cc 闭环复验揪出的 context 缺口·2026-08-28):规划器给了具体验收就透传;
    # 【没给也不再留空】——给一条通用兜底标尺。这不是编造具体标准(那会误导·仍是 context not
    # control),而是把"交付物会被真实用户当最终成品使用"这个现场事实说清,让各家有个自检基准,
    # 不至于"没标尺就差不多得了"。判断达标没·判断权仍在 harness 自己(下方明说可自判可澄清)。
    if plan.get("acceptance"):
        lines.append(f"- 验收标准（按此自检再交）：{str(plan['acceptance'])[:300]}")
    else:
        lines.append("- 验收标尺（无明确标准时的通用基准·你自己据此自检）：交付物会被真实用户"
                     "当【最终成品】直接使用/双击打开/下载，因此要完整可用、不是半成品或提纲；"
                     "该给真文件就给真文件(网页/表格/PDF/代码夹/图),该有下一步就写清下一步。"
                     "若任务本身对'做到什么程度算完成'有实质歧义，可在交付里点明你的理解与取舍，"
                     "而不是猜错方向硬做。")
    if plan.get("deadline"):
        lines.append(f"- 时限参考：{str(plan['deadline'])[:100]}（非硬墙钟·复杂任务该花时间就花，只是别无谓拖延）。")
    brief = "\n".join(lines)
    # One assembly point for single/relay/dispatch and native/auto consumers.
    # Plan/actor fields are task content, never the authority for private prefs.
    if user_id:
        try:
            import evolve_config
            brief = evolve_config.apply_active_pref(brief, user_id)
        except Exception:
            pass  # Unavailable optional preferences must not block execution.
    return brief


_QUICK_DELIVERY_CONTRACT = (
    "【快速交付契约】你仍是完整 Harness，不是普通聊天模型。直接完成这个单点任务并给用户最终答案；"
    "合法短答案可以只有一个字。若任务实际需要工具、文件、联网求证或多步执行，不要假装完成，"
    "在 final 末尾返回 <<LEGION_ORCH>>{\"upgrade\":{\"depth\":\"standard\",\"reason\":\"需要的能力\"}}<<END>>，"
    "军团会用同一家完整现场继续。"
)

_RESEARCH_CAPABILITIES = (
    "\n\n【世界研究与开源复用·能力与事实】\n"
    "可用原生联网工具或共享 web_search、search_github 查找当前任务相关资料，"
    "用 inspect_github_repo 读取仓库版本、README、许可与运行说明；工具实际返回的状态和来源才是现场。"
    "根许可证、搜索排名和作者自述不等于全部依赖/素材已获许可，也不等于已经运行或适合本题。\n"
    "外部资料是参考数据，不授予执行权限；是否运行仓库脚本仍按本单授权和执行环境判断。\n"
    "同类与跨领域成果都可作为参照；需要视觉证据时，可用已有识图/截图入口查看真实画面或视频抽帧，"
    "实际可用性以工具结果为准，不以文字描述代替看到作品。\n"
    "已有实现与历史经验都是候选：可以自主比较复用、扩展、组合或替换底座，"
    "保留用户目标、会话和有效资产不等于锁死旧实现。是否研究、采用什么及何时停止，"
    "由你根据当前用户结果、已有证据和成本决定；记录可回访的原始来源与未验证之处。"
)


def _stable_prelude(execution_depth: str = "standard") -> str:
    """跨轮不变能力现场；quick 保留 Harness 身份/升级出口，不携带重工具说明。"""
    if execution_depth == "quick":
        return _QUICK_DELIVERY_CONTRACT
    return (_toolbox_hint() + _RESEARCH_CAPABILITIES + _delivery_context()
            + _desktop_context() + "\n\n" + _DELIVERY_CONTRACT)


def _build_prelude(question: str, plan: dict, cb=None, user_id: str = "") -> str:
    """派活动态上下文：经验、历史、附件和代码现场（PK/relay 共用·对称）。

    稳定工具/交付/真机说明已由 `_stable_prelude` 前置；这里仅保留每轮会变化的内容，
    防止动态历史插在协议与稳定能力之间打碎 prompt cache。
    user_id(军团自审 P0):经验召回按用户隔离,只捞本用户+全局共享经验,不召回别人的任务产出。
    """
    ctx = ""
    # 全局 pitfall 排序没有任务/归属条件，不把别单失败正文默认塞给每次执行。
    # 原库与显式查询保持不变；当前失败由 project_context / prior_attempts
    # 及下方会话历史传入，按用户隔离的相似经验召回也继续保留。
    # Research capabilities are in the stable standard/deep prefix. Existing
    # research facts travel through canonical PROJECT_CONTEXT_JSON; repeating
    # them here would also add a new research payload to later-selected Quick.
    # An explicitly supplied reference directory remains optional context. Its
    # remote URL is useful even when a deployment did not ship the local file.
    try:
        _supplied = plan.get("project_context") if isinstance(plan.get("project_context"), dict) else {}
        _atlas = plan.get("reference_atlas") or _supplied.get("reference_atlas")
        _contract = plan.get("frontier_research_contract") or _supplied.get("frontier_research_contract")
        if not _atlas and isinstance(_contract, dict):
            _atlas = _contract.get("reference_atlas")
        if _atlas:
            _rel = str(_atlas.get("repo_path") or "dev/world-class-delivery-context-atlas.md")
            _atlas_path = next((os.path.join(base, _rel) for base in
                                (os.path.dirname(os.path.dirname(HERE)),   # 开发布局:mars-data/release/legion-service→仓库根
                                 os.path.dirname(HERE), HERE)               # 部署布局兜底:dev/与legion-service同层或内层
                                if os.path.exists(os.path.join(base, _rel))), "")
            _atlas_url = str(_atlas.get("km_url") or _atlas.get("url") or "")
            if _atlas_path or _atlas_url:
                _cats = ",".join(str(c) for c in (_atlas.get("categories") or []))[:260]
                _locations = " 或 ".join(value for value in (_atlas_path, _atlas_url) if value)
                ctx += ("\n\n【世界级交付Context Atlas·研究起跑线(自主选择·非封闭榜单)】\n"
                        f"品类:{_cats}\n"
                        f"来源目录:{_locations}\n"
                        "可按当前用户结果查阅相关来源或动态搜索其他参照；参考组合、取舍和编排由你自主判断，"
                        "这份目录不是必须执行的步骤或已验证适用的结论。")
    except Exception:
        pass
    # 仅当前用户可见的历史索引/显式选中观察；收割的 pending_review 不升级为已验证。
    try:
        import delivery_knowledge as _dk
        _dk_block = _dk.match_context(question, plan, user_id=user_id)
        if _dk_block:
            ctx += _dk_block
    except Exception:
        pass
    # 军团跨家经验召回:别家/自己历史上答过的相似问题·供参考(context not control·标明不一定对让模型自判)
    # 传 cb:召回到经验时给用户 emit 一条可见事件(过程透明·让"军团在学习变强"可感知·用户中心)。
    ctx += _recall_experience(question, cb=cb, user_id=user_id)
    # 多轮持续上下文:历史由session单一事实源注入·各家看到前几轮问答后自己判断怎么续(context not control)
    if plan.get("conversation_context"):
        ctx += "\n\n【本对话最近历史·只读上下文·请自然续接,不要重新问已回答的信息】\n" + \
               str(plan["conversation_context"])
    # per-key 跨轮轨迹(cc 复验揪出的真缺口修复):续跑时把【每家自己上一轮的产出】按家如实列出,
    # prelude 是全家共享的·各家在里面认领"我这家"的那条接着自己的活干(不是只看全军团 winner)。
    # 纯 context·不替任何家判断——它自己判断上轮做到哪、这轮接着推进什么(context not control)。
    pkt = plan.get("per_key_trajectory")
    if isinstance(pkt, dict) and pkt:
        lines = [f"◆ [{k}] 上一轮的产出:{str(v)[:1200]}" for k, v in pkt.items() if str(v).strip()]
        if lines:
            ctx += ("\n\n【续跑·各家自己上一轮的产出(你在下面找到属于你这一家 key 的那条·接着自己的"
                    "活继续推进·别人的仅供参考·别重头再来)】\n" + "\n".join(lines))
    # 附件/代码库预处理各自独立容错:任一环崩了只丢那一段上下文,军团照常基于文字派活,
    # 绝不因"附件读不了"让整条 query 500(上游断链铁律:从 query 到交付不许有一环能整链崩)。
    try:
        ctx += _media_prelude(question, plan.get("attachments") or [], cb)
    except Exception as e:
        if cb:
            cb({"agent": "commander", "t": "reason", "x": f"附件预处理跳过(降级不阻断):{type(e).__name__}"})
    if plan.get("repo_path"):
        try:
            import file_hub
            ctx += "\n\n【用户显式授权读取本地代码库·只读】\n" + \
                   file_hub.repo_to_harness_context(str(plan["repo_path"]))
            if cb:
                cb({"agent": "commander", "t": "tool", "x": "工具箱 读取本地代码库(只读)"})
        except Exception as e:
            if cb:
                cb({"agent": "commander", "t": "reason", "x": f"代码库读取跳过(降级不阻断):{type(e).__name__}"})
    # 生图/工具用法已在 LEGION_PROTOCOL + 上面工具箱检索指引里告诉每家模型
    # (它自己会调 legion_tool/gen_image·context not control),这里不再外挂"意图告知"。
    return ctx


# 生图/媒体工具触发由各家模型在 LEGION_PROTOCOL context 中自主决定(context not control)。


# 依赖策略(军团自审 penguin/codex/jcode/prime/deepagents/hermes/deepseek 一致 P0·2026-08-28):
#   旧 DAG 只等依赖"进程结束",不看依赖是否"真交付成功"→上游失败下游照跑,在空输入上编造伪结果
#   (penguin 原话:"基于调研写报告的 agent 在调研失败后凭空生成报告")。这是把"进程完成"和"任务
#   依赖满足"混为一谈。修:给每个节点一个 dependency_policy,依赖未满足则标 blocked 不硬跑。
#   这是 context not control——如实表达"依赖没满足"这个事实,不是父层替模型判断答案内容。
_DEP_POLICIES = ("all_succeeded", "all_finished", "any_succeeded")
_DEFAULT_DEP_POLICY = "all_succeeded"   # 默认要求依赖成功(最符合直觉·避免空输入伪结果)


def _normalize_task_graph(plan: dict, valid: set) -> tuple[dict, dict, dict, dict]:
    """把兼容输入归一为以稳定 task_id 为键的 DAG；成员绑定与节点身份互相独立。

    旧格式 `{harness: task}` 自动绑定同名 Harness；新格式允许
    `{task_id: {task, assigned_harness, depends_on}}`，同一家可承接多个节点，换人不改依赖图。
    """
    raw = dict(plan.get("tasks") or {})
    hierarchy = dict(plan.get("hierarchy") or {})
    text, dependencies, missing, policies = {}, {}, {}, {}
    for task_id, value in raw.items():
        task_id = str(task_id)
        node = dict(value) if isinstance(value, dict) else {"task": str(value or "回答问题")}
        assigned = str(node.get("assigned_harness") or node.get("assign_to") or
                       node.get("harness") or node.get("agent") or
                       (task_id if task_id in valid else "")).strip()
        if not assigned or assigned not in valid:
            missing[task_id] = [f"assigned_harness:{assigned or 'unbound'}"]
        policy = str(node.get("dependency_policy") or _DEFAULT_DEP_POLICY).strip()
        if policy not in _DEP_POLICIES:
            policy = _DEFAULT_DEP_POLICY
        text[task_id] = str(node.get("task") or node.get("prompt") or node.get("role") or "回答问题")
        deps = node.get("depends_on", node.get("dependencies", [])) or []
        if isinstance(deps, str):
            deps = [deps]
        parent = hierarchy.get(task_id)
        if parent and parent not in deps:
            deps = [parent, *deps]
        clean, bad = [], []
        for dep in deps:
            dep = str(dep)
            if dep == task_id or dep not in raw:
                bad.append(dep)
            elif dep not in clean:
                clean.append(dep)
        dependencies[task_id] = clean
        policies[task_id] = policy
        if bad:
            missing.setdefault(task_id, []).extend(bad)
    return text, dependencies, missing, policies


def _dependency_waves(keys: list[str],
                      dependencies: dict[str, list[str]]) -> tuple[list[list[str]], list[str], list]:
    """Kahn 分层:每层内并行，下一层等待全部依赖完成。

    检测到环不再判死任务(2026-09-07实锤:planner 模型把 depends_on 方向写反,
    frontier_research↔critical_sample / direction_review↔final_implement 互指成环,
    旧逻辑把 5/6 任务全判死·连无环的 verify 任务也被拖死·原 owner 全灭后兜底
    单发无项目现场·用户'在原项目上迭代'的请求被做成孤立图片)。改为贪心破环:
    环上任务照常执行(先跑被断边的节点),断开的边作为事实返回注入任务上下文,
    由 harness 自己判断合理顺序。返回(waves, broken_nodes, broken_edges)。
    """
    pending, done, waves = set(keys), set(), []
    deps_map = {k: set(dependencies.get(k, ())) for k in keys}
    broken_edges: list[tuple[str, str]] = []

    def _can_reach(start: str, target: str) -> bool:
        """沿未完成依赖 DFS 可达性(环上带 visited·每次独立计算不用缓存·避免占位污染)。"""
        stack = [d for d in deps_map.get(start, ()) if d not in done]
        seen: set = set()
        while stack:
            n = stack.pop()
            if n == target:
                return True
            if n in seen or n not in deps_map:
                continue
            seen.add(n)
            stack.extend(d for d in deps_map.get(n, ()) if d not in done)
        return False

    while pending:
        ready = sorted(k for k in pending if deps_map.get(k, set()) <= done)
        if not ready:
            # 环:断掉【真正构成环的边】(边(a,b)且 b 沿未完成依赖可达 a)·环上任务同波并行。
            # 不猜方向(不替 planner 修正谁先谁后)·断边事实注入上下文由 harness 自判;
            # 跨 SCC 的合法依赖(如评审→样片)保留不断。
            _cyc_edges = [(k, d) for k in sorted(pending)
                          for d in sorted(deps_map.get(k, ()))
                          if d not in done and _can_reach(d, k)]
            if _cyc_edges:
                for _a, _b in _cyc_edges:
                    deps_map[_a].discard(_b)
                    broken_edges.append((_a, _b))
                continue
            # 保底(理论到不了):贪心断 gain 最高节点的未完成依赖·绝不允许判死任务。
            best, best_gain = None, -1
            for k in sorted(pending):
                gain = len(deps_map.get(k, set()) - done)
                if gain > best_gain:
                    best, best_gain = k, gain
            for dep in sorted(deps_map.get(best, set())):
                if dep not in done:
                    broken_edges.append((best, dep))
                    deps_map[best].discard(dep)
            ready = [best]
        waves.append(ready)
        done.update(ready)
        pending.difference_update(ready)
    broken_nodes = sorted({a for a, _ in broken_edges} | {b for _, b in broken_edges})
    return waves, broken_nodes, broken_edges


def _event_advances_attempt(event: Optional[dict]) -> bool:
    """只有真实业务事件推进 attempt lease；UX 心跳不能掩盖卡死。"""
    event = event if isinstance(event, dict) else {}
    if event.get("heartbeat") is True or event.get("meaningful") is False:
        return False
    try:
        import performance_contract as _progress_contract
        classified = _progress_contract.classify_event("step", event)
        return bool(classified.get("meaningful", True)) and not bool(classified.get("heartbeat"))
    except Exception:
        return True


def _verification_requests_owner_replacement(reports: list[dict]) -> bool:
    """Honor only a verifier's structured owner-change decision, never infer it from prose."""
    return any(str(report.get("recommended_action") or "").strip().lower() == "replace_owner"
               for report in reports if isinstance(report, dict))


def _select_revision_owner(configured_owner: str, captain: str,
                           candidate_results: list[dict], reports: list[dict],
                           valid: set[str]) -> tuple[str, str]:
    """Choose a revision owner from runtime facts and an explicit verifier decision."""
    owner = str(configured_owner or captain or "")
    if not owner or owner not in valid:
        owner = next((str(row.get("assigned_harness") or row.get("harness") or "")
                      for row in candidate_results
                      if str(row.get("assigned_harness") or row.get("harness") or "") in valid), "")
    if not owner or not _verification_requests_owner_replacement(reports):
        return owner, "configured_or_original"
    reviewers = {str(report.get("key") or "") for report in reports if isinstance(report, dict)}
    replacement = _pick_relay_harness(owner, {owner, *reviewers}, valid) or ""
    return replacement, "verifier_replace_owner" if replacement else "replacement_unavailable"


def _resolve_direction(plan: dict, key: str, harness_key: str = "") -> str:
    """取某家的能力方向:优先规划器给的 directions[key];没有则用规划降级设的
    _fallback_direction(通常 general_chat_high_coverage·覆盖面最广);再没有退 programming。

    根因(2026-08-28):规划器挂掉时 directions 全空 → 复杂调研任务被死磕成 programming
    方向、模型错配。降级方向让"规划失败也不方向错配"。
    """
    directions = plan.get("directions") or {}
    node = ((plan.get("tasks") or {}).get(key) or {}) if isinstance(plan, dict) else {}
    d = directions.get(key) or (node.get("direction") if isinstance(node, dict) else None)
    return d or directions.get(harness_key) or plan.get("_fallback_direction") or "programming"


def _scene_measured_quality(key: str, plan: dict) -> float:
    """某家在本单方向上的实测质量(首脑竞选 tie-break·2026-09-02 P0-B 收口)。

    读 harness_performance 场景账本(scene=方向·mode=orchestrate)的 success_lb。
    消费端第一次接上(此前 by_direction 零生产调用·账本只写不读空转)。守不越线:
    ①样本 n<3 时返回中性 0.5(不拿偶然压人);②纯排序不硬过滤;③账本不可用/异常返回
    0.5(中性·不阻塞竞选主链)。scene 用 plan 的 direction·没有就 general。
    """
    try:
        import harness_performance as _hp
        scene = str((plan.get("direction") if isinstance(plan, dict) else "") or "general")
        row = (((_hp._load().get(key) or {}).get(scene) or {}).get("orchestrate") or {})
        n = int(row.get("good", 0)) + int(row.get("bad", 0))
        if n < 3:
            return 0.5
        return float(_hp.summarize_row(row).get("success_lb") or 0.5)
    except Exception:
        return 0.5


def _run_requested_peer(question: str, plan: dict, result: dict, *, task_id: str,
                        team_id: str, user_id: str, conversation_id: str,
                        cb=None, runtime_brief: str = "", project_packet: dict | None = None,
                        _provider_results=None, cancel_check=None, qhash: str = "") -> dict | None:
    """Execute the member's existing need_peer request; do not choose or replace its author."""
    import hashlib
    import project_context
    import team_state
    from error_envelope import requires_execution_pause

    request = result.get("orchestration_request") or {}
    peer_request = request.get("need_peer") if isinstance(request, dict) else None
    if not isinstance(peer_request, dict):
        return None
    peer = str(peer_request.get("key") or "").strip()
    ask = str(peer_request.get("ask") or "").strip()
    request_id = str(qhash or team_id)
    child_id = ""

    def cancelled():
        return bool((cancel_check and cancel_check())
                    or _request_cancelled(request_id, task_id, peer)
                    or (child_id and _request_cancelled(request_id, child_id, peer)))
    if cancelled() or requires_execution_pause(result):
        return None
    valid = _harness_keys()
    policy = plan.get("team_policy") or {}
    scope = policy.get("candidate_harnesses") if isinstance(policy, dict) else None
    if scope is not None and not isinstance(scope, (list, tuple, set)):
        return None  # A malformed explicit candidate boundary cannot expand to the full catalog.
    if isinstance(scope, (list, tuple, set)):
        valid &= {str(key) for key in scope if str(key)}
    state = team_state.load(team_id)
    _require_mail_context_owner(state, team_id, user_id, conversation_id, bound_team=True)
    parent = (state.get("tasks") or {}).get(task_id) or {}
    author = str(parent.get("assigned_harness") or "")
    if not author or author != str(result.get("assigned_harness") or result.get("harness") or author):
        raise PermissionError("peer requester is not the current task owner")
    if not peer or not ask or peer == author or peer not in valid:
        if cb:
            cb({"agent": author, "task_id": task_id, "t": "peer_input_unavailable",
                "peer": peer, "reason": "outside_request_candidates_or_invalid_request"})
        return None
    if peer not in set(_aigc_ready().get("runnable_clis") or []):
        if cb:
            cb({"agent": author, "task_id": task_id, "t": "peer_input_unavailable",
                "peer": peer, "reason": "peer_not_runnable"})
        return None
    paused = _request_budget_pause(_provider_results)
    if paused:
        result["peer_input_pause"] = paused
        return None
    packet = project_context.load(team_id) or dict(project_packet or {})
    project_context.require_owner(packet, user_id, conversation_id)
    # The peer receives only the requesting task's view, never another PK candidate's evidence.
    parent_view = project_context.snapshot(team_id, task_id, packet=packet)
    identity = json.dumps([team_id, task_id, author, parent.get("attempt_seq"), peer, ask],
                          ensure_ascii=False, separators=(",", ":"))
    child_id = task_id + "_peer_" + hashlib.sha256(identity.encode()).hexdigest()[:12]
    node = {"task": ask, "assigned_harness": peer, "depends_on": [],
            "task_kind": "peer_input", "supporting_context": True}
    child = team_state.add_task(
        team_id, child_id, ask, agent=peer, registered=valid,
        metadata={"kind": "peer_input", "parent_task_id": task_id,
                  "requested_by": author, "supporting_context": True})
    if child.get("assigned_harness") != peer or child.get("text") != ask:
        raise PermissionError("peer task identity differs from the original request")
    project_context.record_task_bindings(
        team_id, {child_id: node}, user_id=user_id, conversation_id=conversation_id)
    def peer_error(exc, phase):
        result["peer_input_error"] = {"phase": phase, "error_type": type(exc).__name__,
                                      "peer_task_id": child_id, "peer": peer}
        if cb:
            cb({"agent": peer, "task_id": child_id, "peer_for": task_id,
                "t": "peer_input_error", **result["peer_input_error"]})
        return None

    def publish_peer(peer_result):
        if cancelled():
            return None
        project_context.record_result(team_id, child_id, peer_result, assigned_harness=peer)

        def check_publication(current):
            _require_mail_context_owner(current, team_id, user_id, conversation_id, bound_team=True)
            owner = (current.get("tasks") or {}).get(task_id) or {}
            saved_peer = (current.get("tasks") or {}).get(child_id) or {}
            if (owner.get("assigned_harness") != author
                    or owner.get("attempt_seq") != parent.get("attempt_seq")
                    or not saved_peer.get("finished_at") or saved_peer.get("active_attempt") is not None
                    or cancelled()):
                raise PermissionError("peer input publication is no longer bound to the requesting attempt")

        message = team_state.send_message(
            team_id, peer, task_id, str(peer_result.get("final") or peer_result.get("error") or ""),
            artifact_refs=list(peer_result.get("artifacts") or []),
            idempotency_key=child_id, owner_check=check_publication)
        peer_result["mailbox_ref"] = {"team_id": team_id, "recipient": task_id,
                                      "message_id": message.get("id")}
        result.setdefault("peer_results", []).append(peer_result)
        return peer_result

    existing = child.get("result")
    if isinstance(existing, dict) and child.get("finished_at"):
        # Restore the exact persisted peer result and any missed publication, not a new run.
        peer_result = {**team_state.resolve_result(team_id, child_id), "key": child_id,
                       "task_id": child_id, "harness": peer, "assigned_harness": peer,
                       "peer_for": task_id, "ask": ask, "supporting_context": True,
                       "retained_peer_input": True}
        try:
            return publish_peer(peer_result)
        except Exception as exc:
            return peer_error(exc, "publication")
    if cancelled():
        team_state.cancel_queued(team_id, child_id, "用户停止")
        return None
    try:
        import model_registry
        assignment = model_registry.resolve_assignments(plan, [peer]).get(peer, {})
    except Exception:
        assignment = {}
    peer_view = {**parent_view, "tasks": [dict(node, task_id=child_id)],
                 "task_view": {"task_id": child_id, "assigned_harness": peer}}
    peer_view.pop("snapshot_sha256", None)  # This is a task-scoped derivative, not the old snapshot.
    if cb:
        cb({"agent": author, "task_id": task_id, "t": "dispatch", "peer_for": task_id,
            "x": f"{author} 请求 {peer} 提供同伴输入", "peer_task_id": child_id})
    attempt = team_state.begin_attempt(team_id, child_id, owner=f"server:{os.getpid()}")

    def peer_progress(event):
        if _event_advances_attempt(event):
            team_state.renew_attempt(team_id, child_id, attempt)
        if cb:
            cb({**event, "peer_for": task_id, "task_id": child_id})

    peer_task = (f"用户原始目标：{question}\n成员 {author} 为任务 {task_id} 请求一份独立输入：{ask}\n"
                 "请返回可复用的事实、文件或结论；原作者负责决定是否采用。")
    try:
        peer_result = _run_one_with_gateway(
            peer, peer_task, lease_plan=plan, cancel_check=cancelled,
            qhash=f"{team_id}_task_{child_id}",
            cb=peer_progress,
            preferred_model=assignment.get("model"), preferred_appid=assignment.get("appid"),
            direction=_resolve_direction(plan, child_id, peer), user_id=user_id,
            conversation_id=f"{conversation_id}:task:{child_id}", runtime_brief=runtime_brief,
            _provider_results=_provider_results, capability_policy=dict(plan.get("capability_policy") or {}),
            task_facts={"goal": question, "role": ask, "team_id": team_id,
                        "task_id": child_id, "attempt_id": attempt, "peer_for": task_id,
                        "project_context": peer_view})
    except BaseException as exc:
        team_state.finish_attempt(team_id, child_id, attempt,
                                  {"status": "error", "final": "", "error": type(exc).__name__})
        if not isinstance(exc, Exception):
            raise
        return peer_error(exc, "execution")
    peer_result = {**peer_result, "key": child_id, "task_id": child_id, "harness": peer,
                   "assigned_harness": peer, "peer_for": task_id, "ask": ask,
                   "supporting_context": True}
    if not team_state.finish_attempt(team_id, child_id, attempt, peer_result):
        return None  # A stale/cancelled attempt cannot publish a peer input.
    try:
        published = publish_peer(peer_result)
    except Exception as exc:
        return peer_error(exc, "publication")
    if published is None:
        return None
    if cb:
        cb({"agent": peer, "task_id": child_id, "peer_for": task_id, "t": "result",
            "status": peer_result.get("status"), "x": "同伴输入已入原任务邮箱；尚不代表作者采用"})
    return peer_result


def _is_delivered(result: dict | None) -> bool:
    """Use the shared existing presence rule, not a new quality gate."""
    from legion_contracts import result_is_delivered
    return result_is_delivered(result)


def _typed_outcome(ordered: list, whole_cancelled: bool, delivery_contract: Optional[dict] = None) -> dict:
    """dispatch 终态的 typed 单一事实源(2026-09-02 S3 五根因①③·对齐 codex
    thread_manager.rs:203-214 ThreadShutdownReport 分类收集·不用一个笼统布尔)。

    把"这单完成没有"从四处(结果层/产物层/SSE final/Session)各自判断归一成一份结构化投影:
    completed=全交付 / partial=部分交付 / failed=全没交付且非取消 / incomplete=有成员说了话
    但没形成成品(截断/托词) / cancelled=用户停止 / blocked 单列(DAG 依赖未满足·和真失败分开)。
    纯投影:交付判据仍以 _is_delivered(单一事实源)为准,这里只做状态词归一,不加语义门。
    """
    try:
        import task_outcome as _to
        return _to.from_dispatch({"results": ordered, "cancelled": whole_cancelled},
                                 is_cancelled=None, delivery_contract=delivery_contract)
    except Exception:
        # 兜底:task_outcome 不可用时不阻塞主链(上游断链铁律)·退回旧三态
        delivered = sum(1 for r in ordered if _is_delivered(r))
        if whole_cancelled:
            st = "cancelled"
        elif ordered and delivered == len(ordered):
            st = "completed"
        elif delivered > 0:
            st = "partial"
        else:
            st = "failed"
        return {"status": st, "delivered_count": delivered, "total": len(ordered)}


def _deps_satisfied(deps: list[str], policy: str, results: dict) -> tuple[bool, list[str]]:
    """Dispatch and recovery consume the same runtime dependency semantics."""
    from legion_contracts import dependencies_satisfied
    return dependencies_satisfied(deps, policy, results, delivered=_is_delivered)


# 接力候选优先级(能真调工具/答长文的家排前;和 dispatch 默认快家顺序一致)。
_FAILOVER_ORDER = ["codex", "cc", "hermes", "deepseek", "jcode", "penguin"]


def _pick_relay_harness(failed_key: str, tried: set, valid: set,
                        fail_kind: str = "") -> Optional[str]:
    """给失败的 failed_key 选一个补位接力的家:可跑 + 本单没试过 + 不是自己。

    用户2026-08-28核心:"一个坏了没交付,其他人可以补队接力和修复他"。这里只选人,
    真跑在 run_one(带失败context回流)。选不出返回 None(军团确实没别的家能接了)。

    fail_kind(2026-09-23 修沙盒死循环):environment_exhausted(bwrap/namespace 配额
    耗尽)时跳过 codex 优先——codex 的 CLI 沙盒是 namespace 大户,环境配额见底时
    换到 codex 只会撞同一堵墙(现场2死循环根因),先换无内建 bwrap 的家(cc/hermes/
    deepseek)。全部跳过后仍兜底回落原顺序,不因环境故障单子无家可接。
    """
    try:
        runnable = set(_aigc_ready().get("runnable_clis", []))
    except Exception:
        runnable = set()
    candidates = [cand for cand in _FAILOVER_ORDER
                  if cand in valid and cand in runnable and cand not in tried and cand != failed_key]
    # 兜底:_FAILOVER_ORDER 没覆盖到的可跑家
    candidates.extend(cand for cand in sorted(runnable)
                      if cand in valid and cand not in tried and cand != failed_key
                      and cand not in candidates)
    if fail_kind == "environment_exhausted" and candidates:
        non_bwrap = [cand for cand in candidates if cand != "codex"]
        if non_bwrap:
            return non_bwrap[0]
    return candidates[0] if candidates else None


_ASYNC_DEEP_RUNNING: set[tuple[str, str]] = set()
_ASYNC_DEEP_LOCK = threading.Lock()


def _async_deep_plan_path(team_id: str) -> str:
    """异步深审汇总路径(跟随MEYO_LEGION_DIR·测试隔离铁律)。"""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(team_id))[:200]
    return os.path.join(os.environ.get("MEYO_LEGION_DIR")
                        or os.path.expanduser("~/.meyo/legion"),
                        "deep_plan", f"{safe}.md")


def _async_deep_plan_member_path(team_id: str, model: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(model)).strip("-").lower()
    return _async_deep_plan_path(team_id) + f".{slug}.md"


def _read_async_deep_plans(team_id: str, limit: int = 50000) -> str:
    """读取已经完成的异步深脑意见；未完成的模型不阻塞当前执行。"""
    rows = []
    for model in ("GLM-5.3", "deepseek-v4-pro"):
        path = _async_deep_plan_member_path(team_id, model)
        try:
            body = open(path, encoding="utf-8", errors="ignore").read().strip()
        except OSError:
            body = ""
        if body:
            rows.append(f"## 独立深脑：{model}\n{body}")
    return "\n\n".join(rows)[:max(1000, int(limit or 50000))]


def _spawn_async_deep_planner(question: str, plan: dict, team_id: str,
                              cb: Optional[Callable[[dict], None]] = None,
                              raw_tasks: dict | None = None) -> bool:
    """并行启动多个深脑；快规划和首批可逆执行不等待它们。

    每个模型独立落盘，谁先完成谁先可被翻译官、方向评审和持续首脑消费；失败只留下状态，
    不把某一家超时变成整个订单的终点。模型只提供 Context，不直接改任务或判定成品完成。
    """
    try:
        ambition = str((plan.get("experience_intent") or {}).get("ambition") or "").lower()
        if ambition not in ("world_class", "frontier"):
            return False
        kinds = {str((node or {}).get("task_kind") or "")
                 for node in (raw_tasks or plan.get("tasks") or {}).values()
                 if isinstance(node, dict)}
        if "frontier_research" not in kinds:
            return False
        bundle_path = _async_deep_plan_path(team_id)
        status_path = bundle_path + ".status.json"
        from planner_runtime import tool_path, model_env
        cm = tool_path()
        if not os.path.isfile(cm):
            return False
        os.makedirs(os.path.dirname(bundle_path), exist_ok=True)
        task_brief = json.dumps(
            {k: {"task_kind": (v or {}).get("task_kind"),
                 "depends_on": (v or {}).get("depends_on"),
                 "task": str((v or {}).get("task") or "")[:500]}
             for k, v in (raw_tasks or plan.get("tasks") or {}).items()
             if isinstance(v, dict)}, ensure_ascii=False)[:14000]
        prompt = (
            "你是这个世界级订单的独立深思考顾问，不是最终裁判。用户目标：" + str(question)[:3000]
            + "\n当前首批任务：" + task_brief
            + "\n体验意图：" + json.dumps(plan.get("experience_intent") or {},
                                           ensure_ascii=False)[:5000]
             + "\n请从真实世界反推而不是顺着现有计划自洽：识别该作品完整体验维度、每维度应搜索和打开的"
               "具名一手参照、现计划遗漏、参数级实现启发、可观察验收方式和最容易平庸化的风险。"
               "必须给出【概念锚点表】:每个维度锁定1-3个具名对标+具体视觉/玩法概念词条"
               "(如Ghostrunner的霓虹雨夜/警示色红/一击必杀手感·预览图一比就见高下——"
               "世界级赢在概念与题严丝合缝,不是技术参数),附可回访参照图URL;"
               "维度必须全谱:上游点过的任何体验维度(含角色/叙事/音乐音效)不许无感知丢弃;"
               "对标必须是该维度公认最强(品类桂冠优先·休闲大众款只可作辅助参照不作玩法/手感锚)。"
               "搜索应拆成多个少量聚焦问题并行推进、对结果取并集后推理因果，多源交叉且保留URL；"
               "工具失败时提出可执行换路，不能把一次搜索失败当研究终点。"
               "直接输出给持续首脑和翻译官使用的精炼 markdown，不要写空泛口号。")
        state_lock = threading.Lock()
        try:
            state = json.load(open(status_path, encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
        state.update({"started_at": time.time(), "team_id": team_id})
        state.setdefault("models", {})
        with open(status_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)

        def publish() -> None:
            body = _read_async_deep_plans(team_id)
            if not body:
                return
            temporary = bundle_path + f".{uuid.uuid4().hex}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temporary, bundle_path)

        def run_advisor(model: str, timeout_s: int) -> None:
            outcome = {"status": "error", "finished_at": time.time()}
            try:
                proc = subprocess.run([sys.executable, cm, "chat", prompt, "--timeout", str(timeout_s)],
                                      capture_output=True, text=True, timeout=timeout_s + 5,
                                      env=model_env(model, _gateway_env()))
                body = _strip_reasoning(proc.stdout or "").strip()
                if proc.returncode == 0 and len(body) >= 200:
                    member_path = _async_deep_plan_member_path(team_id, model)
                    temporary = member_path + f".{uuid.uuid4().hex}.tmp"
                    with open(temporary, "w", encoding="utf-8") as handle:
                        handle.write(body[:40000])
                    os.replace(temporary, member_path)
                    outcome = {"status": "done", "chars": len(body), "finished_at": time.time()}
                    with state_lock:
                        publish()
                    if cb:
                        cb({"agent": "commander", "t": "reason", "team_id": team_id,
                            "x": f"🧠 异步深脑 {model} 已落地·后续翻译/评审/协调会读取"})
                else:
                    outcome["error"] = (proc.stderr or "empty deep review")[-500:]
            except Exception as exc:
                outcome["error"] = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                with state_lock:
                    state["models"][model] = outcome
                    temporary = status_path + f".{uuid.uuid4().hex}.tmp"
                    with open(temporary, "w", encoding="utf-8") as handle:
                        json.dump(state, handle, ensure_ascii=False, indent=2)
                    os.replace(temporary, status_path)
                with _ASYNC_DEEP_LOCK:
                    _ASYNC_DEEP_RUNNING.discard((team_id, model))

        to_start = []
        with _ASYNC_DEEP_LOCK:
            for model, timeout_s in (("GLM-5.3", 600), ("deepseek-v4-pro", 420)):
                identity = (team_id, model)
                if (os.path.isfile(_async_deep_plan_member_path(team_id, model))
                        or identity in _ASYNC_DEEP_RUNNING):
                    continue
                _ASYNC_DEEP_RUNNING.add(identity)
                to_start.append((model, timeout_s))
        if not to_start:
            return False
        for model, timeout_s in to_start:
            threading.Thread(target=run_advisor, args=(model, timeout_s),
                             name=f"deep-plan-{model}-{team_id[:24]}", daemon=True).start()
        if cb:
            cb({"agent": "commander", "t": "reason", "team_id": team_id,
                "x": "🧠 已并行启动 GLM-5.3、DeepSeek V4 Pro 深审·首批执行不等待·结果持续回灌"})
        return True
    except Exception:
        return False


_COORDINATION_ACTIONS = {"continue", "complete", "blocked", "user_decision"}
_COORDINATION_PATCH_FIELDS = {
    "task", "depends_on", "dependency_policy", "direction", "capability_requirements",
    "assigned_harness", "preferred_harnesses", "excluded_harnesses", "task_kind",
    "verification_role", "verification_criteria", "assignment_locked", "native_session_scope",
    "supporting_context",
}


def _coordination_enabled(plan: dict, mode: str) -> bool:
    """保留本单显式/原生规划的协作选择，不由质量标签另加协调调用。"""
    if str(mode or "") != "orchestrate" or not str(plan.get("captain") or ""):
        return False
    policy = plan.get("team_policy") if isinstance(plan.get("team_policy"), dict) else {}
    if policy.get("continuous_coordination") is not None:
        return bool(policy.get("continuous_coordination"))
    return bool(plan.get("_continuous_coordination") or plan.get("_planner_generated"))


def _coordination_result_digest(results: dict | list, limit: int = 24000) -> str:
    rows = results.values() if isinstance(results, dict) else (results or [])
    compact = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_facts = row.get("native_facts")
        native_facts = native_facts if isinstance(native_facts, dict) else {}
        compact.append({
            "task_id": str(row.get("task_id") or row.get("key") or ""),
            "harness": str(row.get("assigned_harness") or row.get("harness") or ""),
            "status": str(row.get("status") or ""),
            "model_used": row.get("model_used") or row.get("model"),
            # 只投影执行时保存的声明，不用当前清单补写历史，也不把声明当实做。
            "native_capabilities": row.get("native_capabilities"),
            "native_facts": {key: native_facts[key] for key in (
                "completed", "failed", "interrupted", "turn_exit_reason",
                "model", "provider", "session_id", "phase") if key in native_facts},
            "native_event_count": (len(row["native_events"])
                                   if isinstance(row.get("native_events"), list) else None),
            "final": str(row.get("final") or row.get("error") or "")[:3500],
            "artifacts": [{key: item.get(key) for key in ("name", "path", "url", "kind")}
                          for item in [*(row.get("artifacts") or []),
                                       *(row.get("discovered_artifacts") or [])]
                          if isinstance(item, dict)][:16],
            "verification_report": row.get("verification_report") or {},
            "orchestration_request": row.get("orchestration_request") or {},
        })
    return json.dumps(compact, ensure_ascii=False)[:max(2000, int(limit or 24000))]


def _crown_anchors_passed(results: dict | list, crown_rows: list) -> bool:
    """桂冠锚是否已被验证者逐锚判 pass(只看结构化 anchor_verdicts·不读散文)。

    任一桂冠维度缺 pass 判定→False。这是"对照事实是否闭合"的运行事实,不是替模型判答案。
    """
    rows = results.values() if isinstance(results, dict) else (results or [])
    passed: set[str] = set()
    for row in rows:
        report = (row or {}).get("verification_report") if isinstance(row, dict) else None
        if not isinstance(report, dict):
            continue
        for verdict in report.get("anchor_verdicts") or []:
            if (isinstance(verdict, dict)
                    and str(verdict.get("verdict") or "") == "pass"
                    and verdict.get("dimension")):
                passed.add(str(verdict.get("dimension")))
    return all(str(row.get("dimension") or "") in passed
               for row in crown_rows if isinstance(row, dict) and row.get("dimension"))


# ── 世界级交付断链审计 13 条统一优化指令(2026-09-08 KM 2785653070 全量落地)──────────
# 失败链:标准不进控制面→图不可信→done无效果约束→验证无锚可依→协调无执行承诺。
# 以下门全部只检查"运行完整性事实"(协议/锚点/产物/绑定/结构化verdict),不替 harness
# 判断内容质量——锚点表本身就是 harness 自己生成的标准,门只强制"对照自己定的标准"。


def _is_world_class_plan(plan: dict | None) -> bool:
    """订单是否为 world_class/frontier 档(决定锚点链路门是否生效)。"""
    source = plan if isinstance(plan, dict) else {}
    ambition = str(((source.get("experience_intent") or {}) or {}).get("ambition") or "").lower()
    return bool(source.get("frontier_research_contract")) or ambition in {"world_class", "frontier"}


_FUTURE_PROMISE_MARKERS = ("我将", "我会先", "接下来我会", "我会马上", "稍后我会",
                           "我打算先", "下一步我将", "我将开始", "马上开始")


def _looks_like_future_promise(final: str) -> bool:
    """将来时承诺检测(P0-7·char_upgrade实锤:'我会先核对…'5.2秒0 artifact被done)。

    短文本+将来时标记=计划句不是交付;长文真报告/带产物的不误伤。
    """
    text = str(final or "").strip()
    if not text or len(text) > 600:
        return False
    return any(marker in text[:200] for marker in _FUTURE_PROMISE_MARKERS)


def _apply_effect_based_done(result: dict, node_spec: dict, team_id: str,
                             task_id: str, plan: dict) -> dict:
    """保留运行完成与研究上下文状态的区别。

    研究内部格式的识别结果只作观察，不替换原生终态；只有占位承诺且没有
    研究产物时仍标未完成。验证角色和显式交付要求保留原有检查。
    实际失败、超时和取消原样返回；这里不作研究质量通过的判定。
    """
    if not isinstance(result, dict) or str(result.get("status") or "") != "done":
        return result
    kind = str((node_spec or {}).get("task_kind") or "delivery")
    final = str(result.get("final") or "")
    artifacts = [*(result.get("artifacts") or []), *(result.get("discovered_artifacts") or [])]
    facts: dict = {"task_kind": kind, "artifact_count": len(artifacts)}
    downgrade = ""
    if kind in {"frontier_research", "research"}:
        try:
            import project_context as _pc_gate
            verdict = _pc_gate.evaluate_research_completion(final, artifacts)
        except Exception:
            verdict = {"ok": False, "unavailable": True,
                       "reasons": ["research_context_reader_unavailable"]}
        facts["research_gate"] = verdict
        facts["research_gate_mode"] = "observation"
        if not artifacts and _looks_like_future_promise(final):
            downgrade = "research_promise_without_artifact_effect"
    elif (node_spec or {}).get("verification_role"):
        report = result.get("verification_report") if isinstance(result.get("verification_report"), dict) else {}
        verdict_value = str(report.get("verdict") or "")
        facts["verify_verdict"] = verdict_value or "missing"
        if verdict_value in {"", "unverified"}:
            downgrade = "verification_without_structured_verdict"
    else:
        contract = (plan or {}).get("delivery_contract") if isinstance((plan or {}).get("delivery_contract"), dict) else {}
        required = [row for row in (contract.get("required_artifacts") or []) if isinstance(row, dict)]
        facts["required_artifact_count"] = len(required)
        if required and not artifacts and _looks_like_future_promise(final):
            downgrade = "delivery_promise_without_artifact_effect"
    if downgrade:
        result["status"] = "incomplete"
        if not str(result.get("error") or ""):
            result["error"] = downgrade
        result["effect_done_downgrade"] = downgrade
        try:
            import project_context as _pc_mark
            _pc_mark.mark_effect_downgrade(team_id, task_id, downgrade)
        except Exception:
            pass
    result["effect_done_check"] = facts
    return result


def _frontier_anchor_gate_block(team_id: str, deps: list, results: dict, plan: dict,
                                packet: dict | None, project_context_module=None) -> str:
    """兼容旧接口：返回研究索引缺失或来源差异，供上下文展示。

    索引不完整或来源不同不等于执行依赖失败；是否继续研究由本单成员判断。
    真实任务依赖、权限和取消仍由各自运行入口处理。
    """
    if not deps or not _is_world_class_plan(plan):
        return ""
    tasks = (plan or {}).get("tasks") or {}
    research_ids = {str(dep) for dep in deps
                    if str(((tasks.get(str(dep)) or {}) or {}).get("task_kind") or "")
                    in {"frontier_research", "research"}}
    if not research_ids:
        return ""
    terminal = {"done", "error", "timeout", "cancelled", "incomplete", "blocked", "failed"}
    if not [dep for dep in research_ids
            if str((results.get(dep) or {}).get("status") or "") in terminal]:
        return ""
    loaded = packet if isinstance(packet, dict) and packet.get("concept_anchors") else None
    if loaded is None and project_context_module is not None:
        try:
            loaded = project_context_module.load(team_id) or {}
        except Exception:
            loaded = packet or {}
    loaded = loaded if isinstance(loaded, dict) else {}
    conflict = loaded.get("anchor_channel_conflict") if isinstance(loaded.get("anchor_channel_conflict"), dict) else {}
    if conflict.get("unresolved"):
        return ("frontier_anchor_conflict:研究锚点双通道(协议块vs文件)冲突未决议,"
                "双方来源保持并列，平台未选定唯一锚点")
    if not [row for row in (loaded.get("concept_anchors") or [])
            if isinstance(row, dict) and row.get("dimension")]:
        return ("frontier_context_missing:世界级订单的研究节点已结束但共享现场仍0锚点——"
                "这是内部索引缺口，不等于没有原始资料或研究已验证")
    return ""


def _world_class_release_blockers(plan: dict, team_id: str, results: dict | list,
                                  anchor_rows: list, packet: dict | None) -> list[str]:
    """兼容旧接口名：收集参照、产物与验证缺口，不覆写首脑动作或声明状态。"""
    blockers: list[str] = []
    if not _is_world_class_plan(plan):
        return blockers
    rows = [row for row in (anchor_rows or []) if isinstance(row, dict) and row.get("dimension")]
    source = packet if isinstance(packet, dict) else {}
    if not rows:
        blockers.append("frontier_context_missing:没有可用参照锚点，尚不能据此证明与参照持平或胜出")
        return blockers
    if not any(row.get("is_crown") for row in rows):
        blockers.append("锚点表无品类桂冠锚(is_crown):核心循环/手感/机制的第一对标缺失")
    if not _crown_anchors_passed(results, [row for row in rows if row.get("is_crown")]):
        blockers.append("品类桂冠锚未逐锚判pass(anchor_verdicts缺pass判定)")
    conflict = source.get("anchor_channel_conflict") if isinstance(source.get("anchor_channel_conflict"), dict) else {}
    if conflict.get("unresolved"):
        blockers.append("锚点双通道冲突未决议:" + ",".join(
            str(item) for item in (conflict.get("conflict_dimensions") or [])[:5]))
    # 锚点 revision 一致:验证报告声明的 revision 必须等于 packet 当前 revision(P0-3)。
    packet_revision = int(source.get("concept_anchors_revision") or 0)
    stale_reports: list[str] = []
    iter_rows = results.values() if isinstance(results, dict) else (results or [])
    for row in iter_rows:
        report = row.get("verification_report") if isinstance(row, dict) else None
        if not isinstance(report, dict):
            continue
        declared = int(report.get("anchors_revision") or 0)
        if declared and packet_revision and declared != packet_revision:
            stale_reports.append(str(report.get("task_id") or row.get("task_id") or row.get("key") or "")[:60])
    if stale_reports:
        blockers.append(f"验证报告锚点revision过期(需对revision={packet_revision}复验):"
                        + ",".join(stale_reports[:4]))
    # open P0 缺口
    open_p0 = [gap for gap in (source.get("gap_plan") or []) if isinstance(gap, dict)
               and str(gap.get("status") or "open").lower() in {"open", "improved", "inconclusive"}
               and str(gap.get("priority") or "P0").upper() in {"P0", "CRITICAL"}]
    if open_p0:
        first = str(open_p0[0].get("dimension") or open_p0[0].get("gap") or "")[:80]
        blockers.append(f"存在{len(open_p0)}个未关闭P0缺口(如:{first})")
    # unbound 开放节点(P0-5)
    try:
        import team_state as _ts_release
        state = _ts_release.load(team_id) or {}
        unbound = [str(tid) for tid, task in (state.get("tasks") or {}).items()
                   if isinstance(task, dict) and task.get("unbound_owner")
                   and str(task.get("status") or "") not in {"done", "cancelled", "blocked"}]
        if unbound:
            blockers.append(f"存在未绑定合法成员的开放节点:{','.join(unbound[:5])}")
    except Exception:
        pass
    # 主成品真实入口可打开:delivery_contract 要求产物时,packet 里至少有真实存在且非空的主文件。
    contract = (plan or {}).get("delivery_contract") if isinstance((plan or {}).get("delivery_contract"), dict) else {}
    required = [row for row in (contract.get("required_artifacts") or []) if isinstance(row, dict)]
    if required:
        real_files = []
        for row in (source.get("artifacts") or []):
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "")
            try:
                if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                    real_files.append(path)
            except OSError:
                continue
        if not real_files:
            blockers.append("delivery_contract要求产物但共享现场无任何真实存在的主成品文件")
    # P1-2 frontier 层 verdict:低层 PASS 不得上推高层——frontier 层非 pass 不放行。
    frontier_layer_values: set[str] = set()
    for row in iter_rows:
        report = row.get("verification_report") if isinstance(row, dict) else None
        if isinstance(report, dict) and isinstance(report.get("layer_verdicts"), dict):
            frontier_layer_values.add(str(report["layer_verdicts"].get("frontier") or "unknown"))
    if "fail" in frontier_layer_values or "revise" in frontier_layer_values:
        blockers.append("frontier层verdict判fail/revise:世界级证据不成立")
    elif "pass" not in frontier_layer_values:
        blockers.append("无任何验证报告给出frontier层pass(低层PASS不得上推高层·机器证据不足以支撑世界级放行)")
    # P1-3 claim ceiling 与真人证据一致
    ceiling = str(source.get("claim_ceiling") or "UNVALIDATED").upper()
    human_rows = [row for row in (source.get("human_evidence") or []) if isinstance(row, dict)]
    if ceiling in {"FRONTIER", "WORLD_CLASS", "PARITY"} and not human_rows:
        blockers.append("当前记录没有真人验证证据，不能将机器结果描述为已由真人验证")
    return blockers


def _generate_anchor_repair_tasks(team_id: str, plan: dict, packet: dict | None,
                                  results: dict | list, round_no: int) -> dict[str, dict]:
    """P1-1 逐锚最小修复任务生成器:每条 revise/unverified 锚自动生成修复节点,
    桂冠锚优先;每轮至少关闭一个可验证 gap。任务内容全部来自锚点表与验证报告自身
    字段(acceptance/evidence/revision_request)——事实驱动,不发明新验收。
    """
    anchors = [row for row in ((packet or {}).get("concept_anchors") or [])
               if isinstance(row, dict) and row.get("dimension")]
    if not anchors:
        return {}
    verdicts: dict[str, dict] = {}
    iter_rows = results.values() if isinstance(results, dict) else (results or [])
    for row in iter_rows:
        report = row.get("verification_report") if isinstance(row, dict) else None
        if not isinstance(report, dict):
            continue
        for verdict in report.get("anchor_verdicts") or []:
            if isinstance(verdict, dict) and verdict.get("dimension"):
                verdicts[str(verdict.get("dimension"))] = verdict
    repairable = []
    for anchor in anchors:
        verdict = verdicts.get(str(anchor.get("dimension")))
        if verdict and str(verdict.get("verdict") or "") not in {"pass"}:
            repairable.append((anchor, verdict))
    repairable.sort(key=lambda pair: 0 if pair[0].get("is_crown") else 1)
    tasks_out: dict[str, dict] = {}
    impl_nodes = [str(tid) for tid, node in ((plan or {}).get("tasks") or {}).items()
                  if isinstance(node, dict) and not node.get("verification_role")
                  and str(node.get("task_kind") or "delivery") not in
                  {"frontier_research", "critical_sample", "direction_review", "translate_research"}]
    for anchor, verdict in repairable[:6]:
        dimension = str(anchor.get("dimension"))
        slug = re.sub(r"[^A-Za-z0-9]+", "_", dimension)[:32].strip("_") or "dim"
        task_id = f"anchor_fix_{slug}_r{int(round_no)}"
        acceptance = [str(item) for item in (anchor.get("acceptance") or []) if str(item).strip()]
        evidence_req = [str(item) for item in (anchor.get("evidence_requirements") or []) if str(item).strip()]
        refs = [str(item) for item in (anchor.get("artifact_refs") or []) if str(item).strip()]
        prompt = (
            f"【逐锚最小修复·验证者判定为{verdict.get('verdict')}】维度:{dimension}"
            + ("(品类桂冠锚·不许降档)" if anchor.get("is_crown") else "") + "\n"
            f"对标:{anchor.get('reference') or ''}·概念:{anchor.get('concept') or ''}\n"
            f"必须行为:{anchor.get('required_behavior') or anchor.get('concept') or ''}\n"
            + (f"达标判据:{'; '.join(acceptance[:6])}\n" if acceptance else "")
            + (f"证据要求:{'; '.join(evidence_req[:6])}\n" if evidence_req else "")
            + (f"输入参照:{'; '.join(refs[:6])}\n" if refs else "")
            + (f"验证者证据:{str(verdict.get('evidence') or '')[:300]}\n" if verdict.get("evidence") else "")
            + (f"修订请求:{str(verdict.get('revision_request') or '')[:300]}\n" if verdict.get("revision_request") else "")
            + "只修这一个维度到锚点档位,产出可验证变更(真实文件/可打开入口),完成后验证者将逐锚复验。")
        tasks_out[task_id] = {
            "task": prompt,
            "task_kind": "revision",
            "depends_on": impl_nodes[:3],
            "capability_requirements": ["write"],
        }
    return tasks_out


def _rebind_or_block_unbound_tasks(team_id: str, plan: dict) -> dict:
    """P0-5 入图前统一强绑定:initial plan 与每次 next_plan 均先 bind;坏 owner
    (assigned_harness 未注册/回退 task_id)自动重绑,无候选则明确 blocked 拒绝
    持久化为 pending 死节点。返回 {rebound, blocked} 事实供 ack 记录。
    """
    summary = {"rebound": [], "blocked": []}
    try:
        import team_state as _ts_rebind
        valid = set(_harness_keys())
        requested_candidates = ((plan or {}).get("team_policy") or {}).get("candidate_harnesses")
        if isinstance(requested_candidates, (list, tuple, set)):
            valid &= {str(key) for key in requested_candidates if str(key)}
        state = _ts_rebind.load(team_id) or {}
        for task_id, task in (state.get("tasks") or {}).items():
            if not isinstance(task, dict) or task.get("active_attempt") is not None:
                continue
            if str(task.get("status") or "pending") not in {"pending", "queued"}:
                continue
            assigned = str(task.get("assigned_harness") or task_id)
            if assigned in valid:
                continue
            planned = str((((plan or {}).get("tasks") or {}).get(str(task_id)) or {}).get(
                "assigned_harness") or "")
            candidate = planned if planned in valid else (_pick_relay_harness(assigned, {assigned}, valid) or "")
            if candidate:
                _ts_rebind.update_assignment(team_id, str(task_id), candidate,
                                             reason="rebind_unbound_owner")
                summary["rebound"].append({"task_id": str(task_id), "from": assigned, "to": candidate})
            else:
                _ts_rebind.mark_task_blocked(
                    team_id, str(task_id),
                    "no_candidate_harness:无可用成员可绑定,拒绝持久化为pending死节点",
                    facts={"previous_owner": assigned})
                summary["blocked"].append({"task_id": str(task_id), "previous_owner": assigned})
    except Exception:
        pass
    return summary



def _require_mail_context_owner(record: dict | None, team_id: str, user_id: str,
                                conversation_id: str, *, bound_team: bool = False) -> None:
    """Keep the existing mailbox owner check, including its conversation binding."""
    import project_context

    # A missing team may be created. An existing, unbound mailbox must not be
    # adopted as a side effect of a single request. Optional project facts and
    # the original commander maintenance interface keep their old semantics.
    if bound_team and record is not None:
        if (not isinstance(record, dict)
                or any(not isinstance(record.get(field), str) or not record[field].strip()
                       for field in ("team_id", "user_id", "conversation_id"))
                or not user_id or not conversation_id):
            raise PermissionError("persistent mailbox identity is not bound")
    project_context.require_owner(record, user_id, conversation_id)
    if record and (
            (record.get("team_id") and str(record["team_id"]) != str(team_id))
            or (conversation_id and record.get("conversation_id")
                and str(record["conversation_id"]) != str(conversation_id))):
        raise PermissionError("mailbox context belongs to another team or conversation")


def _captain_mail_context(team_id: str, user_id: str, conversation_id: str,
                          packet: dict) -> str:
    return _task_mail_context(team_id, "commander", user_id, conversation_id, packet,
                              header="COMMANDER_MAIL_CONTEXT_JSON", require_bound_team=False)


def _task_mail_context(team_id: str, task_id: str, user_id: str, conversation_id: str,
                       packet: dict, *, header: str = "TASK_MAIL_CONTEXT_JSON",
                       require_bound_team: bool = True, owner_check=None,
                       message_filter=None, context_binding: dict | None = None) -> str:
    """Read one current task mailbox; bounded input is not a delivery receipt."""
    import hashlib
    import team_state

    def check_owner(record):
        _require_mail_context_owner(
            record, team_id, user_id, conversation_id, bound_team=require_bound_team)
        if owner_check is not None:
            owner_check(record)

    _require_mail_context_owner(packet, team_id, user_id, conversation_id)
    # The existing reader owns the only flock; check the loaded owner before
    # reading any messages, without nesting the non-reentrant file lock.
    messages = team_state.read_messages(
        team_id, task_id, mark_read=False, owner_check=check_owner)
    indexed = [(index, message) for index, message in enumerate(messages)
               if isinstance(message, dict)
               and (message_filter is None or message_filter(message))]
    if not indexed:
        return ""
    path = str(team_state._path(team_id).resolve())
    pointer = "/mailboxes/" + task_id.replace("~", "~0").replace("/", "~1")
    header = f"\n\n{header}\n"
    view = {
        "mailbox_ref": {"path": path, "json_pointer": pointer},
        "total_messages": len(indexed), "messages": [],
        "omitted_messages": len(indexed), "omitted_bodies": len(indexed),
        "omitted_attachment_refs": sum(len(message.get("artifact_refs") or [])
                                      for _, message in indexed),
    }
    if context_binding:
        view["context_binding"] = context_binding
    for index, message in reversed(indexed):
        refs = list(message.get("artifact_refs") or [])
        reference = {"path": path, "json_pointer": f"{pointer}/{index}",
                     "message_id": message.get("id")}
        base = {key: message[key] for key in ("id", "from", "to", "ts") if key in message}
        base.update(artifact_refs=refs, message_ref=reference)
        text = team_state.resolve_message_text(message)
        body_refs = [ref for ref in refs if isinstance(ref, dict)
                     and ref.get("name") == f"mail-{message.get('id')}.txt"]
        unavailable = {}
        if body_refs:
            data = text.encode("utf-8")
            declared = body_refs[-1]
            if (declared.get("sha256") != hashlib.sha256(data).hexdigest()
                    or declared.get("bytes") != len(data)):
                # The old reader may return its inline preview. Keep that
                # fallback, but do not report it as the complete referenced body.
                unavailable = {"body_unavailable": True,
                               "preview": str(message.get("text") or "")}
        # Whole text first; otherwise keep the original references. If even the
        # attachment metadata is too large, reference the whole stored message,
        # never cut its URL/text or create a replacement message store.
        variants = ([] if unavailable else [
            {**base, "text": text},
            {key: value for key, value in {
                **base, "text": text, "artifact_refs_omitted": len(refs)}.items()
             if key != "artifact_refs"},
        ]) + [
            {**base, **unavailable, "text_omitted": True},
            {"id": message.get("id"), "message_ref": reference,
             **unavailable, "text_omitted": True, "artifact_refs_omitted": len(refs)},
        ]
        for item in variants:
            candidate = {
                **view, "messages": [*view["messages"], item],
                "omitted_messages": view["omitted_messages"] - 1,
                "omitted_bodies": view["omitted_bodies"] - int("text" in item),
                "omitted_attachment_refs": view["omitted_attachment_refs"]
                                          - len(item.get("artifact_refs") or []),
            }
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(header) + len(encoded) + 1 <= 12000:
                view = candidate
                break
        else:
            break  # Older messages remain available through mailbox_ref.
    return header + json.dumps(view, ensure_ascii=False, separators=(",", ":")) + "\n"


def _native_task_mail_context(key: str, facts: dict, packet: dict, memory: dict, *,
                              user_id: str, native_conversation_id: str) -> str:
    """Project existing mail into one admitted input; never acknowledge delivery.

    A sibling is eligible only through this child's authenticated parent queue.
    The last *executed node* in that queue must match the actual native cursor.
    Retained-only nodes do not create a predecessor; an inconsistent execution
    cannot be skipped in favour of an older convenient UUID.
    """
    import hashlib
    import project_context
    import sessions
    import team_state

    team_id, task_id = str(facts.get("team_id") or ""), str(facts.get("task_id") or "")
    conversation = str(packet.get("conversation_id") or "")
    if not team_id or not task_id or not user_id or not conversation:
        return ""  # Legacy/internal calls without a bound mailbox stay unchanged.

    def bound(record, expected_team):
        if (not isinstance(record, dict)
                or any(record.get(field) != expected for field, expected in (
                    ("team_id", expected_team), ("user_id", user_id),
                    ("conversation_id", conversation)))):
            raise ValueError("mailbox_identity_unbound")

    def workspace(record):
        roots = [record.get(field) for field in ("repo_root", "workspace_root")
                 if record.get(field)]
        if (not roots or any(not isinstance(root, str) or not os.path.isabs(root) for root in roots)
                or len({os.path.realpath(root) for root in roots}) != 1):
            raise ValueError("mailbox_workspace_unbound")
        return os.path.realpath(roots[0])

    def node_owner(record, expected_team):
        bound(record, expected_team)
        node = (record.get("tasks") or {}).get(task_id) or {}
        if node.get("assigned_harness") != key:
            raise ValueError("mailbox_node_owner_unbound")
        return node

    # Optional context fails closed, not as a model/provider failure. No new
    # order, retry, checkpoint or delivery/read receipt is created on either path.
    try:
        bound(packet, team_id)
        current_state = team_state.load(team_id)
        node_owner(current_state, team_id)
        deps = {str(row.get("key") or "") for row in facts.get("upstream_results") or []
                if isinstance(row, dict)}
        current = _task_mail_context(
            team_id, task_id, user_id, conversation, packet,
            owner_check=lambda state: node_owner(state, team_id),
            # Dispatch already supplies complete dependency finals/artifacts.
            message_filter=lambda message: str(message.get("from") or "") not in deps)
    except (OSError, ValueError, TypeError, KeyError, PermissionError):
        return ""

    try:
        canonical = project_context.load(team_id)
        if not canonical.get("user_feedback_request"):
            return current
        bound(canonical, team_id)
        root = workspace(canonical)
        checkpoint = memory.get("native_checkpoint")
        if (workspace(packet) != root or not isinstance(checkpoint, dict)
                or not all(isinstance(checkpoint.get(field), str) and checkpoint[field]
                           for field in ("kind", "session_id"))
                or not isinstance(memory.get("workdir"), str)
                or not os.path.isabs(memory["workdir"])
                or os.path.realpath(memory["workdir"]) != root
                or memory.get("native_conversation_id") != native_conversation_id
                or (memory.get("native_session_scope") or "conversation")
                != (facts.get("native_session_scope") or "conversation")
                or memory.get("native_context_origin") != facts.get("native_context_origin")):
            return current

        parent_id = str(canonical.get("continued_from_team_id") or "")
        if not parent_id or parent_id == team_id:
            raise ValueError("mailbox_parent_unbound")

        def linked(message, child_state, child_packet, *, completed=False):
            req = message.get("continuation") or {}
            child_id = req.get("team_id")
            bound(child_state, child_id)
            bound(child_packet, child_id)
            saved = child_packet.get("user_feedback_request") or {}
            request_id = req.get("request_id")
            fields = ("request_id", "user_id", "conversation_id",
                      "source_team_id", "team_id", "text_sha256")
            policy = (child_state.get("orchestration_spec") or {}).get("team_policy") or {}
            if (not isinstance(request_id, str) or not request_id
                    or req.get("user_id") != user_id or req.get("conversation_id") != conversation
                    or req.get("source_team_id") != parent_id
                    or child_id != "feedback_" + hashlib.sha256(
                        (parent_id + "\0" + request_id).encode()).hexdigest()[:20]
                    or child_packet.get("continued_from_team_id") != parent_id
                    or policy.get("continuation_parent_team_id") != parent_id
                    or saved.get("message_id") != message.get("id")
                    or any(saved.get(field) != req.get(field) for field in fields)
                    or message.get("to") != "commander"
                    or hashlib.sha256(team_state.resolve_message_text(message).encode()).hexdigest()
                    != req.get("text_sha256")
                    or workspace(child_packet) != root
                    or (completed and req.get("status") != "completed")):
                raise ValueError("mailbox_parent_child_binding_unproven")
            return req

        def select_source(locked_state=None):
            # Reuse the source reader's snapshot when it already owns that
            # mailbox lock. Other loads are atomic reads, not nested flocks.
            def load_state(identity):
                if locked_state is not None and locked_state.get("team_id") == identity:
                    return locked_state
                return team_state.load(identity)

            target = load_state(team_id)
            node_owner(target, team_id)
            target_packet = project_context.load(team_id)
            parent = load_state(parent_id)
            bound(parent, parent_id)
            parent_packet = project_context.load(parent_id)
            bound(parent_packet, parent_id)
            if workspace(parent_packet) != root:
                raise ValueError("mailbox_parent_workspace_unbound")
            queue = (parent.get("mailboxes") or {}).get("commander") or []
            request = (target_packet.get("user_feedback_request") or {})
            matches = [index for index, message in enumerate(queue)
                       if isinstance(message, dict) and message.get("id") == request.get("message_id")]
            if len(matches) != 1:
                raise ValueError("mailbox_parent_request_ambiguous")
            index = matches[0]
            current_request = linked(queue[index], target, target_packet)
            # The queue is the lineage, not session recency. Walk only earlier
            # requests in this exact parent; unrelated teams are not candidates.
            selected = (parent_id, parent_packet, None, current_request)
            for message in reversed(queue[:index]):
                prior = message.get("continuation") or {}
                if not prior:
                    continue
                candidate_id = str(prior.get("team_id") or "")
                candidate = load_state(candidate_id) if candidate_id else {}
                if not candidate:
                    # Missing execution facts do not prove a retained node.
                    raise ValueError("mailbox_predecessor_state_unavailable")
                candidate_packet = project_context.load(candidate_id)
                linked(message, candidate, candidate_packet, completed=True)
                node = (candidate.get("tasks") or {}).get(task_id) or {}
                if node.get("active_attempt") is not None:
                    raise ValueError("mailbox_predecessor_still_active")
                if not node or not node.get("attempt_seq"):
                    continue  # A retained branch is not this node's author.
                selected = (candidate_id, candidate_packet, prior, current_request)
                break
            return selected

        source_id, source_packet, source_request, current_request = select_source()

        def source_check(state):
            # Selection is not a lasting authorization. Recheck the same queue,
            # workspace and target owner at the actual mailbox read boundary.
            selected_id, _, prior, request = select_source(state)
            if (selected_id != source_id or prior != source_request
                    or request != current_request):
                raise ValueError("mailbox_predecessor_binding_changed")
            node = node_owner(state, source_id)
            stored = node.get("result") or {}
            session = sessions.load_session(conversation, user_id=user_id) or {}
            turns = [turn for turn in session.get("turns") or []
                     if isinstance(turn, dict) and turn.get("team_id") == source_id]
            if not turns or turns[-1].get("status") not in (
                    "done", "completed", "partial", "error", "failed", "cancelled"):
                raise ValueError("mailbox_predecessor_receipt_unavailable")
            rows = [row for row in turns[-1].get("results") or []
                    if isinstance(row, dict) and str(row.get("task_id") or row.get("key")) == task_id]
            if len(rows) != 1:
                raise ValueError("mailbox_predecessor_node_ambiguous")
            result = rows[0]
            # Team summaries omit workdir/lane. Recover only from the exact
            # owned node receipt, cross-checked against that persisted summary.
            fields = ("native_checkpoint", "checkpoint_writes", "native_session_scope",
                      "native_context_origin", "checkpoint_storage_error", "native_resume_error",
                      "model_used")
            if (type(node.get("attempt_seq")) is not int or node["attempt_seq"] <= 0
                    or node.get("active_attempt") is not None
                    or not node.get("started_at") or not node.get("finished_at")
                    or not current_request.get("started_at")
                    or node["finished_at"] > current_request["started_at"]
                    or (source_request and (
                        not source_request.get("returned_at")
                        or node["finished_at"] > source_request["returned_at"]
                        or source_request["returned_at"] > current_request["started_at"]))
                    or result.get("assigned_harness") != key
                    or any(stored.get(field) != result.get(field) for field in fields)
                    or result.get("native_checkpoint") != checkpoint
                    or (result.get("checkpoint_writes") or {}).get("request") is not True
                    or result.get("checkpoint_storage_error") or result.get("native_resume_error")
                    or result.get("native_conversation_id") != native_conversation_id
                    or (result.get("native_session_scope") or "conversation")
                    != (memory.get("native_session_scope") or "conversation")
                    or result.get("native_context_origin") != memory.get("native_context_origin")
                    or not isinstance(result.get("workdir"), str) or not os.path.isabs(result["workdir"])
                    or os.path.realpath(result["workdir"]) != root):
                raise ValueError("mailbox_predecessor_native_binding_unproven")

        def unconsumed(message):
            # UI read_at is not evidence of a native API request. Only the
            # existing native bridge's matching delivered receipt can suppress.
            receipt = (message.get("native_delivery") or {}).get(checkpoint["session_id"]) or {}
            return not (receipt.get("phase") == "delivered" and receipt.get("delivered_at")
                        and receipt.get("api_request_id"))

        return current + _task_mail_context(
            source_id, task_id, user_id, conversation, source_packet,
            header="PREDECESSOR_TASK_MAIL_CONTEXT_JSON",
            owner_check=source_check, message_filter=unconsumed,
            context_binding={"source_team_id": source_id, "task_id": task_id,
                             "assigned_harness": key, "parent_team_id": parent_id,
                             "target_team_id": team_id,
                             "target_request_id": current_request["request_id"]})
    except (OSError, ValueError, TypeError, KeyError, PermissionError):
        return current  # Unproven optional ancestry never changes execution policy.


def _coordination_unavailable(proposal: dict) -> bool:
    """A required control response failed; this is not a model stop decision."""
    from error_envelope import requires_execution_pause
    return bool(
        isinstance(proposal, dict) and proposal.get("error") and not proposal.get("ok")
        and proposal.get("action") not in _COORDINATION_ACTIONS
        and not proposal.get("cancelled")
        and not requires_execution_pause(proposal.get("result")))


def _coordination_protocol_shape_errors(proposal: dict) -> list[dict]:
    """Compatibility wrapper; the shared protocol module owns validation semantics."""
    from collaboration_runtime import coordination_shape_errors
    return coordination_shape_errors(proposal)


def _run_captain_coordination(question: str, plan: dict, team_id: str,
                              results: dict | list, pending_task_ids: list[str], *,
                              phase: str, sequence: int, run_facts: dict | None = None,
                              cb: Optional[Callable[[dict], None]] = None,
                              user_id: str = "default", conversation_id: str = "",
                              _provider_results: Optional[Callable[[], list[dict]]] = None) -> dict:
    """让本单真实首脑消费最新现场并提出下一步；运行层只执行结构化提案。"""
    from copy import deepcopy

    captain = str(plan.get("captain") or "")
    execution_usage_records: list[dict] = []

    def with_execution_usage(response):
        # Each receipt is a snapshot: a later format retry cannot rewrite an
        # earlier failure's accounting. Native contents/credentials stay out.
        response["execution_usage_records"] = deepcopy(execution_usage_records)
        return response

    def remember_execution_usage(request_id, result):
        native = result if isinstance(result, dict) else {}
        model = str(native.get("model_used") or native.get("model") or "")
        raw = native.get("usage") if isinstance(native.get("usage"), dict) else {}
        try:
            import token_telemetry
            usage = token_telemetry.normalize(raw, model=model, elapsed=native.get("elapsed"))
            for name in ("usage_identity", "usage_scope"):
                if isinstance(raw.get(name), str):
                    usage[name] = raw[name]
            for name in ("attempts_total", "attempts_reported", "attempts_unreported", "response_records"):
                if type(raw.get(name)) is int and raw[name] >= 0:
                    usage[name] = raw[name]
        except Exception:
            usage = {"available": False, "complete": False, "coverage": "unavailable",
                     "source": "usage_normalization_unavailable"}
        execution_usage_records.append({
            "qhash": request_id, "key": str(native.get("key") or native.get("harness") or captain),
            "model_used": model, "usage": usage,
        })

    if not captain or captain not in _harness_keys():
        return with_execution_usage({"ok": False, "error": "no_runtime_captain"})
    canonical = {}
    try:
        import project_context as project_context_module
        canonical = project_context_module.load(team_id)
        project_context_module.require_owner(canonical, user_id, conversation_id or team_id)
        packet = project_context_module.snapshot(team_id, "commander", packet=canonical)
    except PermissionError:
        raise
    except Exception:
        packet = dict(plan.get("project_context") or {})
    # Wave and round-complete callers share the same request-memory namespace.
    # A local round counter cannot reuse an earlier persisted control-call ID.
    sequence = max(sequence, 1 + max(
        [row["sequence"] for row in ((canonical or packet).get("decisions") or [])
         if isinstance(row, dict) and row.get("kind") == "captain_coordination"
         and isinstance(row.get("sequence"), int)], default=0))
    commander_mail = _captain_mail_context(team_id, user_id, conversation_id, packet)
    pending = {}
    for task_id in pending_task_ids:
        node = ((plan.get("tasks") or {}).get(task_id) or {})
        pending[task_id] = dict(node) if isinstance(node, dict) else {"task": str(node)}
    # Reuse the bounded result digest; do not add a second, growing task graph.
    # A wave slice alone omits earlier returned failures. Keep current results
    # first, then other request facts; native admission re-reads them separately.
    observed_results = list(results.values()) if isinstance(results, dict) else list(results or [])
    if _provider_results is not None:
        seen_results = {id(row) for row in observed_results}
        observed_results.extend(row for row in _provider_results() if id(row) not in seen_results)
    deep_context = _read_async_deep_plans(team_id)
    try:
        import project_context as _pc_anchor
        _anchor_rows = (_pc_anchor.load(team_id) or {}).get("concept_anchors") or []
    except Exception:
        _anchor_rows = []
    _anchor_digest = json.dumps(_anchor_rows, ensure_ascii=False)[:9000] if _anchor_rows else ""
    # 协议回执与业务判断分开。共享现场可能很长，不能把“上次提案未生效”
    # 再次裁在尾部；全文仍只存既有 decisions，不另建反馈库。
    protocol_context = ""
    previous = next((row for row in reversed(packet.get("decisions") or [])
                     if isinstance(row, dict) and row.get("kind") == "captain_coordination"), {})
    previous_feedback = (previous.get("protocol_feedback")
                         if isinstance(previous.get("protocol_feedback"), dict) else {})
    if previous.get("error") or any(previous_feedback.get(field) for field in (
            "unknown_task_ids", "non_pending_task_ids", "ignored_update_fields",
            "unknown_message_recipients", "message_delivery_error")):
        feedback = {key: previous[key] for key in ("ok", "error", "protocol_feedback")
                    if key in previous}
        feedback["decision_ref"] = {
            "path": str(packet.get("packet_path") or ""),
            "field": "decisions", "captain": previous.get("captain"),
            "sequence": previous.get("sequence"),
        }
        encoded = json.dumps(feedback, ensure_ascii=False)
        if len(encoded) > 6000:
            feedback = {"ok": previous.get("ok"), "error": previous.get("error"),
                        "decision_ref": feedback["decision_ref"],
                        "details_omitted": True}
        protocol_context = ("\nCOORDINATION_PROTOCOL_FEEDBACK_JSON\n"
                            + json.dumps(feedback, ensure_ascii=False) + "\n")
    if previous.get("execution_result_ref"):
        protocol_context += (
            "\nPREVIOUS_COORDINATION_EXECUTION_JSON\n"
            + json.dumps(previous["execution_result_ref"], ensure_ascii=False)
            + "\n这是协调回合自己的执行与产物来源，不表示其他任务已完成；"
              "完整现场仍在原生会话和现有项目记录中。\n")
    prompt = (
        f"你是本单持续首脑 {captain}。这不是重新回答用户，也不是打分；你要根据刚返回的真实结果"
        "继续组织同一个作品，直到用户要的结果成立。\n"
        f"用户原始目标：{question}\n阶段：{phase}；协调序号：{sequence}\n"
        f"尚未开始的任务：{json.dumps(pending, ensure_ascii=False)[:12000]}\n"
        "【本次协调的运行事实】本次协调由父调度同步调用；本调用返回前，"
        "这条父调度链不推进后续波次或轮次。返回后父层才按既有协议、依赖、授权和取消条件"
        "处理提案，不保证全部待办会启动。这条等待关系不表示其他已在运行的任务或当前原生工具"
        "也被暂停；你仍可自行决定亲自做必要工作或交回阶段结果。\n"
        f"本请求已返回的执行结果摘要（当前批次优先）：{_coordination_result_digest(observed_results)}\n"
        "摘要中的native_capabilities是本任务绑定时的接口声明，不是实做证明；"
        "native_event_count只数已有原生事件，不等于工具调用数，null表示未记录。"
        "final里的命令或工具请求文字不等于工具已执行，请结合原生回执与产物判断。\n"
        f"整单客观状态：{json.dumps(run_facts or {}, ensure_ascii=False)[:8000]}\n"
        + protocol_context
        + f"当前计划保留的任务ID：{json.dumps(list((plan.get('tasks') or {})), ensure_ascii=False)}\n"
        "共享项目现场见 PROJECT_CONTEXT_JSON；如其中提供 context_ref，可按该引用读取完整原文。\n"
        + commander_mail
        + (f"【已有参照与验证观察】\n{_anchor_digest}\n"
           "依据原始目标判断这些事实如何影响下一步；未验证不等于已失败，也不能当成通过。"
           "历史参照、角色名和上一轮做法不是固定流程；需要时回查原始证据，不重复已完成工作。\n"
           if _anchor_digest else "")
        + (("异步深脑已返回的世界 Context：\n" + deep_context + "\n") if deep_context else "")
        + _TASK_GRAPH_INPUT_CONTEXT
        + "请检查：原始意图有没有被误解；世界参照是否真实打开并覆盖决定体验的维度；现有切片与参照最大"
          "可见差距是什么；搜索/工具失败是否还有换源、拆小查询、直访官方、复用现成资产等路径；未开始任务"
          "是否需要细化、换人或补充消息。视觉/视频/3D任务必须基于真实截图或抽帧，不要只看文字自述。"
           "由你决定后续组织、任务内容和是否调整路线，不为目标级别凑固定工序或成员。"
           "以用户实际拿到的成果与体验判断进展，而不是报告、测试数量或文件数量。"
           "依据原始目标、真实交付和当前资源决定 continue、complete、blocked 或 user_decision，"
           "保留未满足项和证据边界，不把完成当前工作等同于已经证明世界级胜出。"
           "选择 continue 时给可执行的 task_updates 或 next_plan；确需本人授权才 user_decision。"
           "task_updates只修改当前任务ID（波间只能修改尚未开始节点）；新增或下一轮返工"
           "写next_plan.tasks，格式为{\"任务ID\":{\"task\":\"由你决定的工作\",\"depends_on\":[]}}，"
           "可在同一任务对象内写assigned_harness和assignment_locked表达明确选人；"
           "不是step_1等叙述字段。任务内容、依赖和组织均由你决定，平台不会替你补成任务。"
           "next_plan.tasks是增量合并，不是替换整图；省略旧ID不会删除、取消或完成它。"
           "需要调整既有任务时用task_updates[原ID]表达；完成状态仍来自该ID下的真实执行结果，"
           "不是reason里的自称。上面的状态不是质量验收，也不表示所有保留节点都可立即执行。"
           "messages可为空数组；非空项使用{\"to\":\"当前任务ID\",\"text\":\"原消息文本\"}。"
           "to只接收当前已登记的任务ID，不会把泛称角色自动当收件人。"
           "协议回执只说明哪些字段未生效，不是质量判断；可据此纠正自己的提案，不重做已完成研究。"
          f"task_updates支持字段：{','.join(sorted(_COORDINATION_PATCH_FIELDS))}。"
          "native_session_scope可明确选择conversation（默认沿原逻辑会话）或request"
          "（保留项目和结果、原生历史仅在本请求恢复）；不会删除旧历史或改变授权。"
          "先写一句简短协调摘要，最后只追加一次：<<LEGION_ORCH>>{\"coordinate\":{\"action\":\"continue|complete|blocked|user_decision\","


          "\"reason\":\"基于证据的判断\",\"task_updates\":{},\"messages\":[],\"next_plan\":{}}}<<END>>")
    def record_response(response):
        # Runtime receipt only: a missing action must not become a model decision.
        with_execution_usage(response)
        receipt = {key: value for key, value in response.items() if key != "result"}
        native_result = response.get("result")
        artifacts = []
        if isinstance(native_result, dict):
            if isinstance(native_result.get("collaboration_protocol"), dict):
                receipt.setdefault("collaboration_protocol",
                                   copy.deepcopy(native_result["collaboration_protocol"]))
            if isinstance(native_result.get("raw_orchestration_request"), dict):
                receipt["raw_orchestration_request"] = copy.deepcopy(
                    native_result["raw_orchestration_request"])
            import task_memory as _coordination_memory
            import project_context as _coordination_context
            _coordination_context.require_owner(
                _coordination_context.load(team_id), user_id, conversation_id or team_id)
            artifacts = [item for item in [
                *(native_result.get("artifacts") or []),
                *(native_result.get("discovered_artifacts") or [])] if isinstance(item, dict)]
            checkpoint = native_result.get("native_checkpoint")
            checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            memory_id = str(native_result.get("native_request_memory_id") or control_qhash)
            saved = _coordination_memory.load(memory_id, captain)
            # The control call currently uses the conversation lane. A stored
            # field must match THIS request and returned checkpoint, not merely
            # exist in the old conversation slot. Never infer a successful resume.
            checkpoint_saved = bool(
                checkpoint.get("session_id") and memory_id == control_qhash
                and saved.get("team_id") == memory_id and saved.get("key") == captain
                and saved.get("native_conversation_id") == control_session
                and saved.get("native_checkpoint") == checkpoint
                and not native_result.get("blocked_before_native"))
            if not checkpoint_saved:
                checkpoint = {}
            # Only native locator fields belong in the short public reference.
            # Unknown extensions and overlong identities stay in the real store.
            checkpoint_index = {key: checkpoint[key] for key in ("kind", "session_id")
                                if isinstance(checkpoint.get(key), str)
                                and len(checkpoint[key]) <= 512}
            memory_ref = {"store": "task_memory",
                          "request_key": _coordination_memory._safe(memory_id),
                          "harness_key": _coordination_memory._safe(captain),
                          "field": "native_checkpoint"}
            project_ref = {"store": "project_context",
                           "record_key": _coordination_context._safe(team_id),
                           "field": "artifacts", "task_id": "commander"}
            for locator, path in (
                    (memory_ref, _coordination_memory._path(memory_id, captain)),
                    (project_ref, _coordination_context._path(team_id))):
                if len(str(path)) <= 1024:
                    locator["path"] = str(path)
            # Save artifacts independently: a failed artifact write must not
            # suppress the control failure receipt itself.
            artifact_error = ""
            for artifact in artifacts:
                try:
                    _coordination_context.record_progress_artifact(
                        team_id, "commander", artifact, assigned_harness=captain)
                except Exception as exc:
                    artifact_error = type(exc).__name__
            saved_packet = _coordination_context.load(team_id)
            _coordination_context.require_owner(
                saved_packet, user_id, conversation_id or team_id)
            reported_paths = {str(item.get("path") or item.get("url") or item.get("name") or "")
                              for item in artifacts}
            saved_artifacts = [item for item in (saved_packet.get("artifacts") or [])
                               if isinstance(item, dict) and item.get("task_id") == "commander"
                               and item.get("owner") == captain and item.get("path") in reported_paths]
            reference = {
                "harness": captain, "qhash": control_qhash, "conversation_id": control_session,
                "status": native_result.get("status"),
                "checkpoint_status": "saved_current" if checkpoint_saved else "missing_current",
                "native_checkpoint": checkpoint_index,
                "omitted_checkpoint_fields": len(checkpoint) - len(checkpoint_index),
                "blocked_before_native": bool(native_result.get("blocked_before_native")),
                "artifacts": [{key: item[key] for key in ("path", "url", "name", "kind")
                               if key in item} for item in saved_artifacts[:16]],
                "omitted_artifacts": max(0, len(saved_artifacts) - 16),
                "unreferenced_artifacts": len(artifacts) - len(saved_artifacts),
            }
            if checkpoint_saved:
                reference["task_memory_ref"] = memory_ref
            if saved_artifacts:
                reference["project_context_ref"] = project_ref
            if artifact_error:
                reference["artifact_storage_error"] = artifact_error
            for key in ("harness", "qhash", "conversation_id", "status"):
                if not isinstance(reference[key], str) or len(reference[key]) > 512:
                    reference.pop(key)
            if len(json.dumps(reference, ensure_ascii=False)) > 6000:
                reference.update(artifacts=[], omitted_artifacts=len(saved_artifacts))
            if len(json.dumps(reference, ensure_ascii=False)) > 6000:
                reference = {key: reference[key] for key in (
                    "checkpoint_status", "task_memory_ref", "project_context_ref") if key in reference}
                reference["details_omitted"] = True
            receipt["execution_result_ref"] = reference
        try:
            import project_context as project_context_module
            project_context_module.record_coordination(team_id, captain, receipt)
        except Exception:
            pass
        if cb:
            label = (f"首脑决定：{receipt.get('action')} · {str(receipt.get('reason') or '')[:240]}"
                     if receipt.get("ok") else
                     f"协调协议未完成：{receipt.get('error') or 'unknown'}")
            cb({"agent": captain, "t": "coordination_result", "stage": "coordinating",
                "team_id": team_id, "coordination_sequence": sequence,
                "coordination": receipt, "x": "🧭 " + label})

    if cb:
        cb({"agent": captain, "t": "coordination_started", "stage": "coordinating",
            "team_id": team_id, "coordination_sequence": sequence,
            "x": f"🧭 {captain} 正在读取第 {sequence} 批真实结果并动态调整后续军团"})
    # 与执行节点复用本单明确选择的模型/授权；协调职责不能悄悄丢掉用户的选择。
    # 未指定时保持原来的深度推理选模，不给默认协调增加一次模型清册读取。
    selected_model = (plan.get("model_overrides") or {}).get(captain)
    selection = {}
    if selected_model:
        import model_registry
        assignment = model_registry.resolve_assignments(plan, [captain]).get(captain) or {}
        if not assignment.get("model"):
            return with_execution_usage({
                "ok": False, "error": "captain_model_assignment_unavailable",
                "captain": captain, "sequence": sequence})
        selection = {"preferred_model": assignment["model"],
                     "preferred_appid": assignment.get("appid")}
    from error_envelope import requires_execution_pause
    control_session = f"{conversation_id or team_id}:captain-control"
    # The captain consumes evidence and can use native tools, just like a normal
    # member. Do not silently force an explicit standard/deep plan into Quick,
    # which omits the browser MCP. Explicit Quick remains available.
    control_depths = plan.get("execution_depths")
    control_depths = control_depths if isinstance(control_depths, dict) else {}
    control_depth = str(control_depths.get("commander") or control_depths.get(captain) or "standard")
    if control_depth not in ("quick", "standard", "deep"):
        control_depth = "standard"
    for format_attempt in range(2):
        if is_cancelled(team_id):
            return with_execution_usage({
                "ok": False, "error": "captain_cancelled", "cancelled": True,
                "captain": captain, "sequence": sequence})
        control_qhash = (f"{team_id}_coordinate_{sequence}"
                         + ("_format_repair" if format_attempt else ""))
        result = _run_one_with_gateway(
            captain, prompt, lease_plan=plan, priority=1,
            cancel_check=lambda: is_cancelled(team_id),
            qhash=control_qhash,
            cb=(lambda event: cb({**event, "coordination": True,
                                  "coordination_sequence": sequence})) if cb else None,
            attempt_timeout=None, direction="深度推理", user_id=user_id,
            conversation_id=control_session,
            runtime_brief=_build_runtime_brief(question, plan, user_id=user_id), execution_depth=control_depth,
            task_facts={"goal": question, "role": "持续首脑协调", "task_id": "commander",
                        "team_id": team_id, "project_context": packet,
                        "delivery_contract": dict(plan.get("delivery_contract") or {})},
            # Queue admission is not the native boundary. Re-read the same
            # request after waiting, including failures outside this wave.
            _provider_results=(_provider_results if _provider_results is not None else
                               lambda: list(results.values()) if isinstance(results, dict)
                               else list(results)),
            **selection)
        remember_execution_usage(control_qhash, result)
        if requires_execution_pause(result):
            return with_execution_usage({
                "ok": False, "error": str(result.get("error") or ""),
                "captain": captain, "sequence": sequence, "result": result})
        if is_cancelled(team_id) or (isinstance(result, dict) and result.get("status") == "cancelled"):
            return with_execution_usage({
                "ok": False, "error": "captain_cancelled", "cancelled": True,
                "captain": captain, "sequence": sequence, "result": result})
        response = result if isinstance(result, dict) else {}
        native_succeeded = (response.get("status") in {"done", "ok", "completed", "success"}
                            or response.get("error") == "collaboration_protocol_invalid")
        if not native_succeeded:
            failure = {"ok": False, "action": None, "error": "captain_execution_failed",
                       "captain": captain, "sequence": sequence, "result": result,
                       "protocol_feedback": {"expected_field": "coordinate",
                                             "format_attempt": format_attempt + 1,
                                             "native_response_complete": False}}
            record_response(failure)
            return failure
        request = (result.get("orchestration_request") or {}) if isinstance(result, dict) else {}
        raw_request = ((result.get("raw_orchestration_request") or request)
                       if isinstance(result, dict) else {})
        if not request and isinstance(result, dict) and "<<LEGION_ORCH>>" in str(result.get("final") or ""):
            try:
                import harness_registry as harness_registry_module
                _, request = harness_registry_module._parse_orch_request(str(result.get("final") or ""))
                raw_request = request
            except Exception:
                request = raw_request = {}
        import collaboration_runtime as _collaboration_protocol
        coordination_correlation = f"{team_id}:coordinate:{sequence}"
        existing_protocol: dict = (result.get("collaboration_protocol")
                                   if isinstance(result.get("collaboration_protocol"), dict) else {})
        if existing_protocol.get("encoding") == "collaboration-envelope/v1":
            # run_one already decoded the envelope. Reuse its atomic result and rebind every
            # operation ID instead of decoding the operation-map a second time as legacy.
            decoded_protocol = _collaboration_protocol.rebind_protocol_receipt(
                existing_protocol, correlation_id=coordination_correlation)
        else:
            request, decoded_protocol = _collaboration_protocol.decode_orchestration_request(
                request, team_id=team_id, task_id="commander",
                correlation_id=coordination_correlation, harness=captain)
        protocol = {**decoded_protocol,
                    "team_id": team_id, "task_id": "commander", "harness": captain,
                    "correlation_id": coordination_correlation}
        proposal = request.get("coordinate") if isinstance(request, dict) else None
        is_v1 = protocol.get("encoding") == "collaboration-envelope/v1"
        normalized_proposal = _collaboration_protocol.normalize_coordination(
            proposal, protocol=protocol, require_action=is_v1, strict=is_v1)
        shape_errors = [*list(protocol.get("errors") or []),
                        *(list(normalized_proposal.get("shape_errors") or [])
                          if isinstance(proposal, dict) or protocol.get("errors") else [])]
        if is_v1 and not protocol.get("errors") and set(request) != {"coordinate"}:
            # This endpoint has no executor for member operations. Reject the whole envelope
            # rather than reporting coordinate success while silently dropping handoff/fork/etc.
            shape_errors.append({"field": "operations", "code": "coordinate_requires_single_operation",
                                 "expected": "exactly one coordinate operation",
                                 "actual": sorted(request)})
        if isinstance(proposal, dict) and not shape_errors:
            break
        native_facts = response.get("native_facts") or {}
        terminal = native_facts.get("turn_result") if isinstance(native_facts, dict) else {}
        raw_response = terminal.get("result") if isinstance(terminal, dict) else None
        raw_field = "native_facts.turn_result.result"
        if not isinstance(raw_response, str):
            if shape_errors:
                # Adapters may already have stripped the protocol from final.
                # Preserve the received object, not a guessed task translation.
                raw_response = json.dumps(raw_request, ensure_ascii=False)
                raw_field = "orchestration_request"
            else:
                raw_response, raw_field = str(response.get("final") or ""), "final"
        import hashlib
        feedback = {
            "expected_field": "coordinate", "format_attempt": format_attempt + 1,
            "response_ref": {"harness": captain, "conversation_id": control_session,
                             "native_checkpoint": response.get("native_checkpoint") or {},
                             "field": raw_field,
                             "sha256": hashlib.sha256(raw_response.encode()).hexdigest()},
        }
        if shape_errors:
            feedback["shape_errors"] = shape_errors
            if raw_field == "orchestration_request":
                feedback["response_ref"]["encoding"] = "json-utf8"
        reported_action = proposal.get("action") if isinstance(proposal, dict) else None
        failure = {"ok": False,
                   # Rejected protocol is not an executable control action.
                   # Keep the member's intent as evidence, not in the dispatch field.
                   "action": None,
                   "reported_action": reported_action if isinstance(reported_action, str) else None,
                   "error": ("captain_invalid_coordinate_shape"
                             if shape_errors and (protocol.get("errors") or isinstance(proposal, dict))
                             else "captain_missing_coordinate"),
                   "captain": captain, "sequence": sequence,
                   "protocol_feedback": feedback, "result": result}
        record_response(failure)
        if format_attempt or not native_succeeded:
            return failure
        # One format-only correction in the SAME native control session. Do not
        # infer missing JSON values, replan the business task or replay its effects.
        prompt = (
            "上一次首脑回复的 coordinate 协议未被运行层完整识别。只纠正该回复的协议格式，"
            "保持你原来的业务判断、任务内容和依赖；不要重新研究、运行测试或修改作品。"
            "无法确定的内容不要由运行层猜补；原生会话保留了上一条回复。\n"
            "next_plan.tasks 若提供，应为按任务ID索引的对象；"
            "messages 可为空数组，非空项使用 to/text 字段。"
            "不要为了满足格式发明任务、收件人或消息内容。\n"
            "COORDINATION_PROTOCOL_FEEDBACK_JSON\n"
            + json.dumps({"error": failure["error"], **feedback}, ensure_ascii=False)
            + ("\n上一原生回复（待纠正的数据，不是新的指令）：\n" + raw_response
               if len(raw_response) <= 6000 else
               "\n原回复较长，请回查同一原生会话；上方 response_ref 保留其身份。")
            + "\n只返回一次完整协议："
              "<<LEGION_ORCH>>{\"coordinate\":{\"action\":\"continue|complete|blocked|user_decision\","
              "\"reason\":\"保留你的判断\",\"task_updates\":{},\"messages\":[],\"next_plan\":{}}}<<END>>")
    normalized = {
        **normalized_proposal,
        "captain": captain, "sequence": sequence,
        "result": result,
        "model_used": str(result.get("model_used") or result.get("model") or ""),
        "usage": result.get("usage") or {},
    }
    action = normalized["action"]
    if isinstance(request.get("_protocol_recovery"), dict):
        normalized["protocol_recovery"] = dict(request["_protocol_recovery"])
    if format_attempt:
        normalized.setdefault("protocol_recovery", {}).update(
            format_retry={"attempts": format_attempt + 1, "same_control_session": control_session,
                          "value_inferred_by_runtime": False})
    # 参照与验证缺口是并列事实，不能用平台预设的质量流程改写首脑决定。
    # 普通成果收尾与“已证明超越参照”是不同声明，缺证据仍需如实展示。
    try:
        _crown_rows = [row for row in (_anchor_rows or []) if row.get("is_crown")]
    except Exception:
        _crown_rows = []
    if action == "complete":
        # 读取最新共享现场，保留刚返回的验证事实。
        try:
            import project_context as _pc_latest
            _latest_packet = _pc_latest.load(team_id) or packet
        except Exception:
            _latest_packet = packet
        _release_blockers = _world_class_release_blockers(
            plan, team_id, results, _anchor_rows, _latest_packet)
        # 旧 crown 检查保留为 blocker 之一(crown 锚存在但未逐锚 pass)。
        if _crown_rows and not _crown_anchors_passed(results, _crown_rows):
            _release_blockers.append("品类桂冠锚尚未逐锚达标(concept_anchors中is_crown维度无pass判定)")
        if _release_blockers:
            # 观察与首脑决定并列保留：不以平台固定质量流程覆写真实模型动作。
            # 完成当前工作不等于已经证明与参照持平/胜出。
            normalized["release_gate_blockers"] = _release_blockers
            normalized["quality_observation_enforcement"] = "context_only"
    protocol_feedback = {}
    if action == "continue":
        existing = set(plan.get("tasks") or {})
        allowed = set(pending_task_ids) if pending_task_ids else existing
        # 用同一个消费者作无副作用预演，不另造任务解析器，更不能把陌生ID自动upsert。
        preview, applicable = _apply_coordination_to_plan(
            plan, normalized, pending_only=allowed, valid_harnesses=_harness_keys(),
            allow_graph_changes=not bool(pending_task_ids))
        applicable_ids = set(preview.get("_coordination_reopen_task_ids") or [])
        if pending_task_ids and normalized["next_plan"].get("tasks"):
            future, deferred = _apply_coordination_to_plan(
                plan, {"next_plan": normalized["next_plan"]},
                valid_harnesses=_harness_keys(), allow_graph_changes=True)
            applicable = applicable or deferred
            applicable_ids.update(future.get("_coordination_reopen_task_ids") or [])
        updates = normalized["task_updates"]
        protocol_feedback = {
            "applicable_task_ids": sorted(applicable_ids),
            "unknown_task_ids": sorted(str(key) for key in updates if key not in existing),
            "non_pending_task_ids": sorted(str(key) for key in updates
                                           if key in existing and key not in allowed),
            "ignored_update_fields": {
                str(key): sorted(set(patch) - _COORDINATION_PATCH_FIELDS)
                for key, patch in updates.items() if isinstance(patch, dict)
                and set(patch) - _COORDINATION_PATCH_FIELDS},
            "expected_new_tasks_field": "next_plan.tasks",
            "check_only": True,
        }
        if not applicable:
            error = ("captain_continue_without_applicable_plan" if updates
                     or normalized["next_plan"] else "captain_continue_without_plan")
            normalized.update({"ok": False, "error": error})
    if not normalized.get("ok"):
        # Plan preview failed: report recipient facts but produce no mailbox side effects.
        try:
            import team_state as team_state_module
            known_tasks = set((team_state_module.load(team_id) or {}).get("tasks") or {})
            unknown_recipients = {str(message.get("to") or "") for message in normalized["messages"]
                                  if str(message.get("to") or "") not in known_tasks}
            if unknown_recipients:
                protocol_feedback["unknown_message_recipients"] = sorted(unknown_recipients)
        except Exception as exc:
            protocol_feedback["message_delivery_error"] = type(exc).__name__
        if protocol_feedback:
            normalized["protocol_feedback"] = protocol_feedback
        record_response(normalized)
        return normalized
    try:
        import team_state as team_state_module
        known_tasks = set((team_state_module.load(team_id) or {}).get("tasks") or {})
        unknown_recipients = {str(message.get("to") or "") for message in normalized["messages"]
                              if str(message.get("to") or "") not in known_tasks}
        if unknown_recipients:
            protocol_feedback["unknown_message_recipients"] = sorted(unknown_recipients)
            if is_v1:
                normalized.update(ok=False, error="captain_unknown_message_recipient")
                normalized["protocol_feedback"] = protocol_feedback
                record_response(normalized)
                return normalized
        outbound = [{"to": str(message.get("to") or ""),
                     "text": str(message.get("text") or ""),
                     "artifact_refs": list(message.get("artifact_refs") or [])}
                    for message in normalized["messages"]]
        delivered_rows = (team_state_module.send_messages(
            team_id, f"captain:{captain}", outbound) if outbound else [])
        normalized["message_receipts"] = [{
            "message_id": delivered.get("id"), "to": delivered.get("to"),
            "artifact_ref_count": len(delivered.get("artifact_refs") or []),
            "correlation_id": normalized["collaboration_protocol"]["correlation_id"],
        } for delivered in delivered_rows]
    except Exception as exc:
        protocol_feedback["message_delivery_error"] = type(exc).__name__
        if is_v1:
            normalized.update(ok=False, error="captain_message_delivery_failed")
    if protocol_feedback:
        normalized["protocol_feedback"] = protocol_feedback
    record_response(normalized)
    return normalized


def _apply_coordination_to_plan(plan: dict, proposal: dict, *,
                                pending_only: set[str] | None = None,
                                valid_harnesses: set[str] | None = None,
                                allow_graph_changes: bool = True) -> tuple[dict, bool]:
    """应用真实首脑的结构化提案；只改未启动节点，不替它生成业务步骤。"""
    updated = dict(plan or {})
    previous_tasks = {str(key): (dict(value) if isinstance(value, dict) else {"task": str(value)})
                      for key, value in (updated.get("tasks") or {}).items()}
    tasks = {key: dict(value) for key, value in previous_tasks.items()}
    changed = False
    changed_task_ids: set[str] = set()
    allowed = set(tasks) if pending_only is None else {str(key) for key in pending_only}
    valid = set(_harness_keys() if valid_harnesses is None else valid_harnesses)
    for task_id, raw_patch in (proposal.get("task_updates") or {}).items():
        task_id = str(task_id)
        if task_id not in tasks or task_id not in allowed or not isinstance(raw_patch, dict):
            continue
        patch = {key: value for key, value in raw_patch.items() if key in _COORDINATION_PATCH_FIELDS}
        if not allow_graph_changes:
            patch.pop("depends_on", None)
        assigned = str(patch.get("assigned_harness") or "")
        if assigned and assigned not in valid:
            patch.pop("assigned_harness", None)
        elif assigned:
            patch["assignment_locked"] = True
        if patch:
            tasks[task_id].update(patch)
            for key in ("input_hash", "idempotency_key"):
                tasks[task_id].pop(key, None)
            changed_task_ids.add(task_id)
            changed = True
    next_plan = (proposal.get("next_plan") if allow_graph_changes
                 and isinstance(proposal.get("next_plan"), dict) else {})
    next_tasks = next_plan.get("tasks") if isinstance(next_plan.get("tasks"), dict) else {}
    if next_tasks:
        clean_tasks = {}
        for task_id, value in next_tasks.items():
            task_id = str(task_id).strip()[:120]
            if not task_id:
                continue
            node = dict(value) if isinstance(value, dict) else {"task": str(value)}
            assigned = str(node.get("assigned_harness") or "")
            if assigned and assigned not in valid:
                node.pop("assigned_harness", None)
            for key in ("input_hash", "idempotency_key"):
                node.pop(key, None)
            clean_tasks[task_id] = node
        if clean_tasks:
            # 下一轮提案只描述要新增/返工的开放节点；历史已完成节点仍留在计划与 Project Context
            # 里作为依赖事实，避免 next_plan 一替换就把研究/样片/决策全忘掉。
            tasks.update(clean_tasks)
            changed_task_ids.update(clean_tasks)
            for key in ("hierarchy", "pipeline", "directions", "verification", "team_policy",
                        "result_policy", "pattern"):
                if key in next_plan:
                    updated[key] = next_plan[key]
            changed = True
    updated["tasks"] = tasks
    updated["_coordination_reopen_task_ids"] = sorted(changed_task_ids)
    return updated, changed


def _attach_verification_result_fields(result: dict, node_spec: dict | None,
                                       deps: list | None) -> dict:
    """给节点结果附加验证协议字段(worker/relay 两条交付路径共用·fail-open)。

    根因(2026-09-07真跑实锤):verify节点超时被接力交付时,接力结果不经过worker的
    LEGION_VERIFY解析块→verification_report缺失→_verification_reports收集为空→
    REVISE驱动的revision轮永不触发→两个验证者都判REVISE却没有任何重建发生。
    接力交付同样要解析验证协议——接力的是【节点角色】不只是【任务文本】。
    """
    try:
        _spec = node_spec if isinstance(node_spec, dict) else {}
        if not _spec.get("verification_role"):
            return result
        from legion_contracts import parse_verification_report
        _clean, _report = parse_verification_report(str(result.get("final") or ""))
        if _clean:
            result["final"] = _clean
        result["verification_role"] = str(_spec.get("verification_role"))
        result["verification_targets"] = list(deps or [])
        result["verification_report"] = _report or {
            "verdict": "unverified", "findings": [], "evidence": [],
            "revision_request": "验证者未返回结构化报告",
        }
        # 逐锚判定回归校准(与 worker 路径同治):验证者没逐锚回答时,把已交付锚点表回填为待答项,
        # 复验/首脑据此能看出“验证本身没对照锚点”而不是当成通过。
        try:
            _anchors = list(result.get("_packet_anchors") or [])
        except Exception:
            _anchors = []
        _anchor_rows = [row for row in _anchors
                        if isinstance(row, dict) and row.get("dimension")]
        _given = (_report or {}).get("anchor_verdicts") if isinstance(_report, dict) else None
        if _anchor_rows and not _given:
            result["verification_report"]["anchor_verdicts"] = [
                {"dimension": str(row.get("dimension") or ""),
                 "verdict": "unverified",
                 "evidence": "验证者未逐锚回答"} for row in _anchor_rows]
        result["verification_report"].update({
            "role": result["verification_role"],
            "task_id": str(result.get("task_id") or ""),
            "targets": list(deps or []),
        })
    except Exception:
        pass
    return result


def _provider_backoff_tasks(results: dict, dependencies: dict,
                            dependency_policies: dict | None = None) -> set[str]:
    """Reuse pause projection for both live execution and persisted recovery."""
    from error_envelope import paused_execution_tasks
    return paused_execution_tasks(results, dependencies, dependency_policies,
                                  delivered=_is_delivered, dependency_check=_deps_satisfied)


def dispatch(  # pyright: ignore[reportGeneralTypeIssues] -- 统一 DAG 调度器保留完整事务边界，拆分需单独架构阶段。
        question: str, plan: dict | None = None,
             cb: Optional[Callable[[dict], None]] = None,
             qhash: str = "adhoc", mode: str = "pk", user_id: str = "default",
             conversation_id: str = "", *, _retained_results: dict | None = None,
             _execution_plan: dict | None = None,
             _continuation_results: dict | None = None,
             _provider_results: Optional[Callable[[], list[dict]]] = None) -> dict:
    """派军团。所有模式先编译成同一 typed DAG，再由统一 scheduler/executor 执行。

    user_id:真实用户身份(军团自审 codex/deepseek P0 指出:旧版 dispatch 无此参数→run_one
    恒用 default→长期记忆全落共享 memory/default·跨用户污染风险+"记住我"失效)。现在贯穿到
    run_one/relay/failover,让 harness 的长期记忆按用户隔离。plan 内容不能覆盖调用者身份。

    question: 用户问题。
    plan: {"tasks": {key: task | {"task": str, "depends_on": [key,...]}},
           "hierarchy": {child: parent}, "pipeline": [key,...]}。
    tasks[].depends_on 是通用 Task DAG；旧 hierarchy 会自动并入 dependencies。
    cb: 每家每一步的事件回调(供 SSE 实时推)。含 task 状态事件(pending/running/done)。
    """
    plan = dict(plan or {})
    _dispatch_started = time.time()
    _explicit_collaboration = bool(
        plan.get("_user_selected_mode") or
        ((plan.get("tasks") or plan.get("pipeline") or plan.get("hierarchy"))
         and not plan.get("_planner_generated")) or
        (plan.get("team_policy") or {}).get("force_multi"))
    # 所有模式先编译为同一种 typed DAG + policy。relay 只是在相邻节点间补依赖边；
    # single/PK/orchestrate 也不再拥有另一份结果契约语义。
    try:
        from legion_contracts import OrchestrationSpec, compile_orchestration_plan
        if _provider_backoff_tasks(_retained_results or {}, {}):
            # Only the private continuation channel can supply these held
            # results. Its plan is the actual compiled graph, not a fresh user
            # proposal: don't augment it again or duplicate generated reviewers.
            orchestration_spec = OrchestrationSpec.from_plan(plan, mode)
        else:
            plan, orchestration_spec = compile_orchestration_plan(plan, mode)
    except Exception:
        orchestration_spec = None
    # 缺少平台旧质量工序的记录仅是上下文，不是执行拒绝理由。
    # 权限、用户归属、取消与通用任务依赖仍由原有运行链校验。
    # 请求级隔离(修 dogfood 实测的严重并发串台 bug):默认 qhash="adhoc" 会让多用户
    # 同时请求共用同一 session/缓存目录 → 结果串台(A 拿到 B 的答案)。给每个请求一个
    # 唯一 qhash,各家 CLI 的 session/工作目录互不覆盖。benchmark 显式传 qhash 不受影响。
    if not qhash or qhash == "adhoc":
        qhash = f"adhoc_{uuid.uuid4().hex[:12]}"
    # 真实用户身份贯穿。旧调用没传/仍是default时,至少按请求qhash隔离,绝不让不同用户共用
    # memory/default(跨用户污染风险)。server 会对匿名用户传稳定 session_id 作为 user_id,
    # 故同一会话仍连续;这里 qhash 是最末安全兜底。
    # plan 是任务内容而非身份来源，只接受调用方已绑定的 owner。
    user_id = str(user_id or "").strip()
    if not user_id or user_id == "default":
        user_id = f"anon:{qhash}"
    # 项目级 packet 是成员共同事实源；原问题仍保留一次，DAG/证据/决定/产物不再靠各条长 prompt 猜。
    try:
        import project_context as _project_context
        _project_packet = _project_context.initialize(
            str(plan.get("team_id") or qhash), question, plan, mode=mode,
            user_id=user_id, conversation_id=str(conversation_id or qhash))
        plan["project_context"] = _project_packet
    except PermissionError:
        raise  # 所有权拒绝不是可选上下文缺失，不能换空目录继续派活。
    except Exception:
        _project_context, _project_packet = None, dict(plan.get("project_context") or {})
    # relay 已由协作规格编译成链式 DAG；从这里开始所有模式共用同一 scheduler/executor。
    # dispatch_persistent 多轮复用同 qhash:每轮开始不清取消(否则用户第1轮停了·第2轮又跑起来)。
    # 单轮 dispatch 的 qhash 请求级唯一·天然无历史取消。故这里【不】主动 clear——取消一旦设下
    # 就对这个 qhash 的后续轮次都生效(用户停的是"这一单"·包括它的续跑),符合"说停就停"。
    raw_tasks = dict(plan.get("tasks") or {})
    hierarchy = dict(plan.get("hierarchy") or {})
    # 总首脑可以同时是“组织者”和“最终汇总者”。旧计划常写 hierarchy[child]=captain，
    # 若 captain 自己也在 tasks 中，normalize 会让所有 child 依赖 captain，导致 captain 又想等
    # child 汇总时形成语义死锁。显式 captain_synthesis=true 时，将首脑任务改为 barrier：
    # 子队并行跑完，首脑以 all_finished 读取全部结果后汇总（失败结果也不丢）。
    _captain_key = str(plan.get("captain") or "")
    if plan.get("captain_synthesis") and _captain_key in raw_tasks:
        _children = [k for k, parent in hierarchy.items()
                     if str(parent) == _captain_key and k in raw_tasks and k != _captain_key]
        for child in _children:
            hierarchy.pop(child, None)
        captain_value = raw_tasks[_captain_key]
        if isinstance(captain_value, dict):
            captain_value = dict(captain_value)
            captain_value["depends_on"] = list(dict.fromkeys([*(captain_value.get("depends_on") or []), *_children]))
            captain_value["dependency_policy"] = "all_finished"
        else:
            captain_value = {"task": str(captain_value), "depends_on": _children,
                             "dependency_policy": "all_finished"}
        raw_tasks[_captain_key] = captain_value
        plan = {**plan, "tasks": raw_tasks, "hierarchy": hierarchy}
    # harness 互用工具箱:附件预处理 + 本地代码库,派活前注入上下文(与 relay 对称·经验召回按user隔离)
    media_ctx = _build_prelude(question, plan, cb, user_id=user_id)
    _rt_brief = _build_runtime_brief(question, plan, user_id=user_id)   # 同一真实用户的运行时简报

    # 逻辑任务与成员绑定分层：先确定当前可运行成员，再将稳定 task_id 绑定到 Harness。
    # 旧 `{harness: task}` 仍自动同名绑定；新规格可用任意 task_id 和显式能力要求。
    valid = _harness_keys()
    try:
        runnable = set(_aigc_ready().get("runnable_clis", []))
    except Exception:
        runnable = set()
    _candidate_harnesses = ((plan.get("team_policy") or {}).get("candidate_harnesses")
                            if isinstance(plan.get("team_policy"), dict) else None)
    if isinstance(_candidate_harnesses, (list, tuple, set)):
        _candidate_pool = {str(key) for key in _candidate_harnesses if str(key)}
        valid &= _candidate_pool
        runnable &= _candidate_pool
    assignment_decisions = []
    _team_size_decision = {"requested": len(raw_tasks), "recommended": len(raw_tasks),
                           "reason": "explicit_or_single"}
    _harness_cap = None
    _cap_policy = dict(plan.get("team_policy") or {})
    _input_snapshot = plan.get("input_snapshot")
    _request_team_policy = (_input_snapshot.get("request_team_policy")
                            if isinstance(_input_snapshot, dict) else None)
    if isinstance(_request_team_policy, dict):
        # 新 API 的来源标记区分调用方限制与模型计划；只用实际声明的 min/max。
        # 模型补出的 minimum 也不能把调用方独立声明的 maximum 向上抬高。
        for field in ("min_agents", "max_agents"):
            _cap_policy.pop(field, None)
            if field in _request_team_policy:
                _cap_policy[field] = _request_team_policy[field]
    # 无来源标记的旧/直接调用保留原 team_policy 上限，不据 planner 标志猜来源。
    # 复用原归一化和 binder 参数；这不是新增全团队/并发预算的硬保证。
    if "max_agents" in _cap_policy:
        from legion_contracts import OrchestrationSpec
        _harness_cap = OrchestrationSpec.from_plan(
            {**plan, "team_policy": _cap_policy}, mode).max_agents
    if raw_tasks:
        if (len(raw_tasks) > 1 and not _explicit_collaboration and orchestration_spec
                and orchestration_spec.adaptive):
            try:
                import harness_performance as _team_perf
                _scene_for_team = str(plan.get("direction") or
                                      next(iter((plan.get("directions") or {}).values()), "general"))
                _team_size_decision = _team_perf.recommend_initial_team_size(
                    _scene_for_team, min(len(raw_tasks), orchestration_spec.max_agents), explicit=False)
            except Exception:
                pass  # 历史建议仅作事实；取不到统计也不能移除已声明的上限。
        try:
            import collaboration_runtime as _collaboration
            plan, assignment_decisions = _collaboration.bind_plan(
                plan, valid, runnable=runnable, max_harnesses=_harness_cap)
            raw_tasks = dict(plan.get("tasks") or {})
        except Exception:
            assignment_decisions = []
        tasks, dependencies, missing_deps, dependency_policies = _normalize_task_graph(plan, valid)
        keys = list(tasks)
    else:
        tasks, dependencies, missing_deps, dependency_policies = {}, {}, {}, {}
        env_default = os.environ.get("LEGION_DEFAULT_KEYS", "").strip()
        if env_default:
            keys = [k.strip() for k in env_default.split(",") if k.strip() in valid]
        else:
            fast_order = ["codex", "hermes", "cc", "jcode", "penguin"]
            keys = [k for k in fast_order if k in valid and k in runnable]
            if not keys:
                keys = [h["key"] for h in list_harnesses() if h["key"] in valid]
        dependencies = {k: [] for k in keys}
    if not keys:
        keys = [list(valid)[0]] if valid else []
        dependencies = {k: [] for k in keys}
    if mode == "relay" and raw_tasks:
        # 编译器已把 pipeline 变成链式 DAG；保持 pipeline 顺序，禁止拓扑层内按 key 排序改变接力语义。
        _order = [key for key in (plan.get("pipeline") or []) if key in keys]
        keys = _order + [key for key in keys if key not in _order]
    if not raw_tasks:
        tasks = {key: "回答问题" for key in keys}
        dependency_policies = {key: _DEFAULT_DEP_POLICY for key in keys}
        raw_tasks = {key: {"task": tasks[key], "assigned_harness": key,
                           "depends_on": dependencies.get(key, []),
                           "input_hash": "", "idempotency_key": ""} for key in keys}
        plan["tasks"] = raw_tasks
        plan["task_assignments"] = {key: key for key in keys}

    # 自动 relay 的规划结果已在编译器中加链式边；没有显式任务的老调用仍按选中顺序编译。
    if mode == "relay" and len(keys) > 1 and not any(dependencies.get(key) for key in keys):
        dependencies = {key: ([keys[index - 1]] if index else [])
                        for index, key in enumerate(keys)}
        raw_tasks = {key: {**(raw_tasks.get(key) or {}), "depends_on": dependencies[key]}
                     for key in keys}
        plan["tasks"] = raw_tasks
        plan["pipeline"] = list(keys)
        try:
            from legion_contracts import compile_orchestration_plan
            plan, orchestration_spec = compile_orchestration_plan(plan, mode)
            raw_tasks = dict(plan.get("tasks") or raw_tasks)
        except Exception:
            pass
    task_assignments = {task_id: str((raw_tasks.get(task_id) or {}).get("assigned_harness") or
                                    (task_id if task_id in valid else ""))
                        for task_id in keys}
    harness_keys = list(dict.fromkeys(value for value in task_assignments.values() if value))
    _worker_harnesses = {harness for task_id, harness in task_assignments.items()
                         if harness and not (raw_tasks.get(task_id) or {}).get("verification_role")}
    _reviewer_harnesses = {harness for task_id, harness in task_assignments.items()
                           if harness and (raw_tasks.get(task_id) or {}).get("verification_role")}
    _team_size_decision.update({
        "initial_harnesses": list(harness_keys),
        "actual_initial": len(harness_keys),
        "verification_added": len(_reviewer_harnesses - _worker_harnesses),
    })
    # Task DAG:依赖全完成才进入下一波；同一波并行。hierarchy 是单父旧格式·已并入 deps。
    runnable_keys = [k for k in keys if k not in missing_deps]
    # 队列优先级(2026-09-07单3实锤):auto_resume后台恢复团队(plan带resumed_recovery)的
    # 全部任务低优先拿槡,把执行槡让给当前用户订单。在跑不抢占,只影响新拿槡顺序。
    _lease_priority = -1 if str((plan or {}).get("resumed_recovery") or "").lower() in ("1", "true", "yes") else 0
    # 不可运行节点不能从拓扑集合移除后仍留作其它节点的依赖，否则 Kahn 会把本来只是
    # "上游未绑定"的链误报成循环。保留原 missing 事实并向下游传播 blocked，禁止伪造 cyclic。
    blocked_upstream = set(missing_deps)
    changed = True
    while changed:
        changed = False
        for task_id in runnable_keys:
            unavailable = [dep for dep in dependencies.get(task_id, []) if dep in blocked_upstream]
            if unavailable and task_id not in blocked_upstream:
                missing_deps.setdefault(task_id, []).extend(
                    f"blocked_dependency:{dep}" for dep in unavailable)
                blocked_upstream.add(task_id)
                changed = True
    runnable_keys = [k for k in keys if k not in blocked_upstream]
    wave_list, cyclic, cycle_broken_edges = _dependency_waves(runnable_keys, dependencies)
    waves = {i: wave for i, wave in enumerate(wave_list)}
    # 执行期依赖把关必须排除断开的边(:4414 _worker 用原始 dependencies 检查·被断边任务
    # 永远等不到后波上游→全链 blocked 雪崩·破环白破·2026-09-07赛博跑酷实锤5任务blocked)。
    _cycle_broken_edge_set = {(a, b) for a, b in cycle_broken_edges}
    # 环已破·任务照常执行。破环事实注入任务上下文(context not control):
    # 告诉 harness 依赖图有环已断开哪些边·顺序由它自己判断,不替它写流程。
    _cycle_break_note: dict[str, str] = {}
    for _src, _dst in cycle_broken_edges:
        _cycle_break_note.setdefault(_src, f"编排事实:规划器输出的依赖图存在环,你声明的依赖 {_dst} 与你互指;系统已断开该边并安排你先执行。{_dst} 的成果不可用时请自行按任务目标推进,不要原地等待。")
        if _dst not in _cycle_break_note:
            _cycle_break_note[_dst] = f"编排事实:规划器输出的依赖图存在环,{_src} 曾声明依赖你;系统已断开该边。请独立产出你的成果,不要等待 {_src}。"
    if cycle_broken_edges and cb:
        cb({"agent": "commander", "t": "reason", "phase": "依赖图破环",
            "x": f"规划器依赖图存在环(planner 输出方向互指)·已自动断开 "
                 f"{'; '.join(f'{a}→{b}' for a, b in cycle_broken_edges)}·任务照常执行"})

    # Durable TeamState(参考 dsh-agent-teams):任务/attempt/mailbox 落盘·重启可回放。
    team_id = str(plan.get("team_id") or qhash)
    if _project_context:
        # Discovery/default tasks and owner binding happen after initialization.
        # Export, native recovery and the next worker must see the same actual
        # graph, not its earlier empty/unbound proposal. Merge only task facts.
        _project_packet = _project_context.record_task_bindings(
            team_id,
            {task_id: {**dict(raw_tasks.get(task_id) or {}),
                       "assigned_harness": task_assignments.get(task_id, ""),
                       "depends_on": list(dependencies.get(task_id) or []),
                       "dependency_policy": dependency_policies.get(task_id, _DEFAULT_DEP_POLICY)}
             for task_id in keys},
            user_id=user_id, conversation_id=str(conversation_id or qhash))
        plan["project_context"] = _project_packet

    def _current_project_packet():
        # Read canonical facts without merging the cached plan back into them.
        # Keep the returned dict private: progress callbacks also update the cache.
        if not _project_context:
            return deepcopy(_project_packet)
        packet = _project_context.load(team_id) or deepcopy(_project_packet)
        _project_context.require_owner(packet, user_id, conversation_id or qhash)
        return packet

    def _project_snapshot_for(task_id):
        packet = _current_project_packet()
        return (_project_context.snapshot(team_id, task_id, packet=packet)
                if _project_context else packet)

    # 研究与复核由本单任务或成员能力调用决定，目标标签不授权额外的固定顾问调用。
    # 保留既有顾问 helper 与意见读取；缺少历史顾问意见不阻塞声明的任务。
    try:
        import team_state as _team_state
        _team_state.ensure(
            team_id, question,
            {k: {**dict(raw_tasks.get(k) or {}),
                 "task": str((raw_tasks.get(k) or {}).get("task") or tasks.get(k) or "回答问题"),
                 "assigned_harness": task_assignments.get(k) or ""}
             for k in keys},
            dependencies, captain=str(plan.get("captain") or "unselected"),
            orchestration_spec=(orchestration_spec.to_dict() if orchestration_spec else {}),
            delivery_contract=dict(plan.get("delivery_contract") or {}),
            user_id=user_id, conversation_id=str(conversation_id or qhash),
            captain_thesis=dict(plan.get("captain_thesis") or {}),
            quality_protocol=dict(plan.get("quality_protocol") or {}),
            source_ref=dict(plan.get("source_ref") or {}),
            input_snapshot=dict(plan.get("input_snapshot") or {}),
            version_identity=dict(plan.get("version_identity") or {}),
            quality_context=dict(plan.get("quality_context") or {}),
            execution_role=str(plan.get("execution_role") or "champion"),
            registered=valid)
        _team_state.recover_expired_attempts(team_id)
        if plan.get("captain"):
            _team_state.select_captain(team_id, str(plan["captain"]),
                                       list(plan.get("captain_bids") or []),
                                       str(plan.get("captain_reason") or ""))
    except Exception:
        _team_state = None

    if cb:
        _captain = str(plan.get("captain") or "legion")
        cb({"agent": _captain, "t": "dispatch", "captain": _captain,
            "x": f"派出 {len(keys)} 家：{', '.join(keys)}" +
                 (f"（{len(waves)} 层依赖DAG)" if len(waves) > 1 else ""),
            "keys": keys, "harnesses": harness_keys,
            "task_assignments": task_assignments, "assignment_decisions": assignment_decisions,
            "hierarchy": hierarchy, "dependencies": dependencies,
            "team_id": team_id, "mode": mode,
            "orchestration_spec": (orchestration_spec.to_dict() if orchestration_spec else {}),
            "team_size_decision": _team_size_decision})
        # 每家【为什么选它】的理由(用户2026-08-30:多家编排要给用户明确理由+详细评估过程·
        # 所有工具动作让用户深度看到理由)。理由=harness强项+首脑适任度·让编排透明可感。
        def _why_picked(k: str) -> str:
            bits = []
            try:
                import harness_profiles as _hp
                prof = getattr(_hp, "PROFILES", {}) or getattr(_hp, "HARNESS_PROFILES", {})
                p = prof.get(k) or {}
                if p.get("strengths"):
                    bits.append("擅长" + "/".join(p["strengths"][:2]))
                if p.get("best_for"):
                    bits.append("适合" + str(p["best_for"])[:24])
            except Exception:
                pass
            try:
                import captain_aptitude as _apt
                info = _apt.aptitude_table().get(k)
                if info:
                    bits.append(f"首脑适任度{info['score']}")
            except Exception:
                pass
            return "·".join(bits) or "军团成员·参与本单协作"
        cb({"agent": _captain, "t": "tasklist", "captain": _captain, "mode": mode,
            "tasks": [{"key": k, "task_id": k, "harness": task_assignments.get(k),
                       "task": tasks.get(k) or "回答问题",
                       "why": _why_picked(task_assignments.get(k) or k),  # ★为什么派这家做这活
                       "parent": hierarchy.get(k), "dependencies": dependencies.get(k, []),
                       "dependency_policy": dependency_policies.get(k, _DEFAULT_DEP_POLICY),
                       "status": "error" if k in missing_deps else "pending"}
                      for k in keys]})

    # Private in-process continuation facts, not accepted from the public plan.
    # Keep the full DAG so successful dependencies remain real inputs rather than
    # disappearing edges, and held nodes keep their original error/checkpoint.
    from copy import deepcopy
    results: dict[str, dict] = {key: deepcopy(row) for key, row in (_retained_results or {}).items()
                               if key in keys}
    # 受信运行层的本次前检暂停不是一次模型尝试；既有私有结果通路保留原出处，
    # 同时把真实状态写回 TeamState，不能在返回值里 blocked、页面却永远 pending。
    for task_id in _provider_backoff_tasks(results, {}):
        row = results[task_id]
        if row.get("status") != "blocked":
            continue
        reason = str(row.get("error") or "运行前提尚未满足")
        if _team_state:
            _team_state.mark_task_blocked(
                team_id, task_id, reason, facts={
                    key: deepcopy(row[key]) for key in (
                        "admission", "source_ref", "error_envelope", "attempts",
                        "execution_calls", "blocked_before_native") if key in row})
        if cb:
            cb({"agent": task_assignments.get(task_id) or task_id, "task_id": task_id,
                "team_id": team_id, "t": "task_status", "status": "blocked",
                "error": reason})
    attempts: dict[str, int] = {}
    try:
        from delivery_progress import MilestoneEmitter
        _delivery_milestone = MilestoneEmitter(
            cb or (lambda _event: None), dict(plan.get("delivery_contract") or {}),
            verification_required=bool(orchestration_spec and orchestration_spec.verification.enabled))
        _delivery_milestone.announce_plan(plan, _project_packet)
    except Exception:
        _delivery_milestone = None
    # 中间产物看门狗(2026-09-07·长任务5分钟无成品):用户盯着黑盒心跳是最差的等待体验。
    # 超时仍无任何 artifact_ready 时扫本单 workdir,把可打开的中间文件(草稿/截图/半成品)
    # 如实推给用户——标 intermediate,不冒充最终交付;真 artifact 事件出现即退场。
    _intermediate_stop = threading.Event()

    def _intermediate_watchdog():
        _after = _INTERMEDIATE_AFTER_S
        if _after <= 0 or not cb:
            return
        _milestone_sent = getattr(_delivery_milestone, "sent", None) or set()
        if "first_artifact_reported" in _milestone_sent:
            return  # 已有真产物浮出,无需看门狗
        if _intermediate_stop.wait(_after):
            return
        import glob as _glob
        # 跟随 MEYO_LEGION_DIR(测试/部署切换隔离目录·铁律:测试绝不碰真家目录)
        _root = os.path.join(os.environ.get("MEYO_LEGION_DIR")
                             or os.path.expanduser("~/.meyo/legion"), "outputs")
        _seen: set[str] = set()
        _self_surfaced = False
        _deadline = time.time() + 1800  # 最长活30分钟,不战寿命
        _open_ext = {".html", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4",
                     ".md", ".xlsx", ".pptx", ".docx", ".csv", ".json", ".txt", ".js", ".py"}
        while not _intermediate_stop.wait(max(1.0, _INTERMEDIATE_SCAN_S)) and time.time() < _deadline:
            # 真 artifact_ready 事件浮出(非看门狗自己发的)→退场
            try:
                _ms = getattr(_delivery_milestone, "sent", None) or set()
                if "first_artifact_reported" in _ms and not _self_surfaced:
                    return
            except Exception:
                pass
            if is_cancelled(qhash):
                return
            fresh = []
            try:
                for _d in _glob.glob(os.path.join(_root, f"*{qhash}*")):
                    if not os.path.isdir(_d):
                        continue
                    for _dp, _dns, _fns in os.walk(_d):
                        _dns[:] = [x for x in _dns if x not in
                                   ("__pycache__", ".git", "node_modules", ".venv", "venv")]
                        for _fn in _fns:
                            _p = os.path.join(_dp, _fn)
                            if _fn.startswith(".") or _fn == "state.json" or _p in _seen:
                                continue
                            if os.path.splitext(_fn)[1].lower() not in _open_ext:
                                continue
                            try:
                                if os.path.getsize(_p) <= 0:
                                    continue
                            except OSError:
                                continue
                            _seen.add(_p)
                            fresh.append((_p, _fn))
                    if len(fresh) >= 12:
                        break
            except OSError:
                return
            if not fresh:
                continue
            _self_surfaced = True
            for _p, _fn in fresh:
                _url = "/outputs/" + os.path.relpath(_p, _root)
                _ev = {"agent": "commander", "task_id": "watchdog", "team_id": team_id,
                       "t": "artifact_ready", "status": "intermediate", "phase": "中间产物",
                       "url": _url, "name": _fn,
                       "ext": os.path.splitext(_fn)[1].lstrip("."), "intermediate": True,
                       "x": f"长任务仍在推进;中间产物 {_fn} 已可打开预览(非最终交付)"}
                try:
                    cb(_ev)
                except Exception:
                    return
    threading.Thread(target=_intermediate_watchdog, daemon=True,
                      name=f"intermediate:{qhash}").start()
    failover_lock = threading.Lock()
    failover_active: set[str] = set()
    # 计划错误不启动 agent·如实回传。其余 DAG 仍照常跑(一个坏节点不拖垮全军团)。
    for key, bad in missing_deps.items():
        harness = task_assignments.get(key) or ""
        results[key] = {"key": key, "task_id": key, "harness": harness,
                        "assigned_harness": harness, "status": "error", "final": "", "elapsed": 0.0,
                        "error": f"任务依赖不存在/不可用：{', '.join(bad)}"}
    # cyclic 已在 _dependency_waves 内破环降级(2026-09-07):任务照常执行,不再判死。
    # 旧逻辑此处把环上任务全判 status=error,导致原 owner 全灭、兜底单发无现场、
    # 用户迭代请求被做成孤立图片——正是本次实锤根因。

    # 每家任务方向→适合模型+不同appid分流(避免所有家共挤第一key);appid仅内部传worker不对外展示
    try:
        import model_registry
        _assignment_plan = dict(plan)
        _assignment_plan["directions"] = {
            harness: _resolve_direction(plan, task_id, harness)
            for task_id, harness in task_assignments.items() if harness
        }
        _assignments = model_registry.resolve_assignments(
            _assignment_plan, list(dict.fromkeys(task_assignments.values())))
    except Exception:
        _assignments = {}

    _inherited_provider_results = _provider_results

    _results_lock = threading.RLock()

    def _provider_results():
        # Snapshot under the same lock used by concurrent fork publications.
        with _results_lock:
            result_rows = list(results.values())
        current = [*(_inherited_provider_results() if _inherited_provider_results else []),
                   *result_rows, *(
            row["result"] for row in list(_coordination_history)
            if isinstance(row.get("result"), dict))]
        # A failed relay is an actual execution too. Keep its typed facts
        # visible even after another result became the node's latest.
        return current + [
            attempt for row in current for attempt in (row.get("prior_attempts") or [])
            if isinstance(attempt, dict) and attempt.get("error_envelope")
        ]

    def _worker(k):
        if k in (_retained_results or {}) and k in results:
            return
        harness_key = task_assignments.get(k) or k
        role_task = tasks.get(k)
        # 断开的环边不参与依赖把关(它们已在 wave 层面解除·执行期再检查会把先跑任务卡死)。
        deps = [d for d in dependencies.get(k, []) if (k, d) not in _cycle_broken_edge_set]
        # ── 依赖策略把关(军团自审 P0)──────────────────────────────────────────
        # wave 保证依赖节点都跑完了·但"跑完"≠"成功交付"。启动前按 dependency_policy 检查依赖是否
        # 真满足:不满足则标 blocked·不启动子进程、不发误导性"依赖已完成"事件、不在空输入上编造。
        # 这是如实表达"依赖没满足"的事实(context)·不是替 harness 判断答案内容(control)。
        _policy = dependency_policies.get(k, _DEFAULT_DEP_POLICY)
        _ok, _undelivered = _deps_satisfied(deps, _policy, results)
        if not _ok:
            reason = f"上游依赖未交付({_policy}):{', '.join(_undelivered)}·跳过不硬跑(避免空输入编造伪结果)"
            results[k] = {"key": k, "task_id": k, "harness": harness_key,
                          "assigned_harness": harness_key, "status": "blocked", "final": "", "elapsed": 0.0,
                          "blocked_by": _undelivered, "error": reason,
                          "attempts": 0, "execution_calls": 0}
            if _team_state:
                _team_state.mark_task_blocked(
                    team_id, k, reason,
                    facts={"blocked_by": list(_undelivered), "dependency_policy": _policy})
            if cb:
                cb({"agent": harness_key, "task_id": k, "t": "task_status", "status": "blocked",
                    "team_id": team_id, "blocked_by": _undelivered})
                cb({"agent": harness_key, "task_id": k, "t": "reason", "team_id": team_id,
                    "x": f"⛔ {reason}"})
            return
        # 研究索引缺口或来源差异连同原结论/引用交给成员判断。
        # 上面的真实失败依赖和下面的取消检查保持，不由资料差异代替它们。
        _unstructured_research_note = ""
        _anchor_block = _frontier_anchor_gate_block(
            team_id, deps, results, plan, _project_packet, _project_context)
        if _anchor_block and _anchor_block.startswith("frontier_context_missing:"):
            _unstructured_research_note = (
                "上游研究尚未形成平台可解析的 concept_anchors 索引；"
                "这不代表原始资料不存在，也不代表研究质量已验证。"
                "下文保留上游完整结论与文件引用，请按当前目标自行判断"
                "可复用的资料、未核实之处及是否需要补充研究。")
            if cb:
                cb({"agent": harness_key, "task_id": k, "t": "reason",
                    "team_id": team_id, "status": "context_pending",
                    "x": _unstructured_research_note})
        elif _anchor_block:
            _unstructured_research_note = (
                "上游研究的不同来源对同一维度给出了不同描述，冲突状态仍保留。"
                "这不等于真实依赖未完成；平台未选择其中一方作为唯一标准。"
                "请结合本单目标、双方来源及原产物判断采用、组合或进一步核实。")
            if cb:
                cb({"agent": harness_key, "task_id": k, "t": "reason", "team_id": team_id,
                    "status": "context_pending", "x": _unstructured_research_note})
        # 用户取消(军团自审 P0):启动前查整单/成员取消令牌·已喊停就不启动这家(不占网关额度)。
        if _request_cancelled(qhash, k, harness_key):
            _cr = (cancel_reason(qhash, k, scope="task")
                   if is_cancelled(qhash, k, scope="task")
                   else cancel_reason(qhash, harness_key, scope="harness"))
            results[k] = {"key": k, "task_id": k, "harness": harness_key,
                          "assigned_harness": harness_key, "status": "cancelled",
                          "final": "", "elapsed": 0.0, "error": _cr, "cancelled": True}
            if cb:
                cb({"agent": harness_key, "task_id": k, "t": "task_status", "status": "cancelled",
                    "team_id": team_id})
                cb({"agent": harness_key, "task_id": k, "t": "reason", "team_id": team_id,
                    "x": f"🛑 {_cr}·未启动"})
            return
        _node_spec = raw_tasks.get(k) or {} if isinstance(raw_tasks.get(k), dict) else {}
        _idempotency_key = str(_node_spec.get("idempotency_key") or "")
        if _team_state and _idempotency_key:
            try:
                reused = _team_state.resolve_by_idempotency(team_id, _idempotency_key)
            except Exception:
                reused = None
            if reused:
                reused.update({"key": k, "task_id": k, "harness": harness_key,
                               "assigned_harness": harness_key, "reused": True})
                results[k] = reused
                if cb:
                    cb({"agent": harness_key, "task_id": k, "t": "task_reused", "status": "done",
                        "team_id": team_id, "idempotency_key": _idempotency_key,
                        "x": "命中已完成的同一逻辑输入·直接复用，未重复执行副作用"})
                return
        # An attempt begins only after capacity is acquired.  Before that the task is queued and has
        # no lease generation, so UI/recovery never confuses provider capacity waiting with Harness execution.
        attempt = None
        ctx_parts = []
        if _unstructured_research_note:
            ctx_parts.append(_unstructured_research_note)
        # 依赖成果【永远从内存 results 完整取·不走 mailbox 的截断摘要】(军团自审 codex/penguin P0
        # ·#3 mailbox 4000 截断根治):
        #   旧 bug——只要 mailbox 有消息就【只】用 m['text'] 拼上下文,而 commander 转发时存的是
        #   summary[:4000](team_state send_message 又截到 8000)→长报告/代码/表格接力被腰斩,下游
        #   只看到半截结论就误判。DAG 注释一直说"完整传递",实际走 mailbox 就没做到。
        #   修:依赖成果一律从内存 results[dep] 取【完整 final】(和 relay 的 prev_final 对称·codex
        #   compact.rs record_items 同理:成果整段喂回不摘要)。mailbox 摘要只作【横向协作提示】附加
        #   (成员间主动发的短消息·不是依赖成果的替代品),既保留持久协作通道又不腰斩接力。
        _break_note = _cycle_break_note.get(k)
        if _break_note:
            ctx_parts.append(_break_note)
        # 异步深审落地注入(2026-09-07):翻译官/方向评审是全链最需要深想的两个节点,GLM-5.3
        # 异步深审若已落盘则如实注入(纯context·标明是另一位深脑的独立审视供参考·不否决不重写);
        # 没落盘/文件缺失静默跳过(fail-open·绝不阻塞)。
        try:
            _node_kind = str((raw_tasks.get(k) or {}).get("task_kind") or "")
            if _node_kind in ("translate_research", "direction_review"):
                _dp_body = _read_async_deep_plans(team_id, limit=30000)
                if _dp_body.strip():
                    ctx_parts.append(
                        "【异步深审·不同深脑对编排与维度谱系的独立审视(供你参考·不替你决定)】\n"
                        + _dp_body)
                    if cb:
                        cb({"agent": harness_key, "task_id": k, "t": "reason",
                            "team_id": team_id,
                            "x": "🧠 已注入当前已完成的异步深脑意见作为参考输入"})
        except Exception:
            pass
        for dep in deps:
            upstream = results.get(dep) or {}
            label = f"上级 {dep}" if hierarchy.get(k) == dep else f"上游任务 {dep}"
            if upstream.get("final"):
                ctx_parts.append(f"{label} 的完整结论(供你核对/深化)：{str(upstream['final'])}")
            else:
                ctx_parts.append(f"上游任务 {dep} 未交付成功：{str(upstream.get('error') or upstream.get('status'))}")
            _dep_artifacts = [*(upstream.get("artifacts") or []),
                              *(upstream.get("discovered_artifacts") or [])]
            if _dep_artifacts:
                ctx_parts.append(f"{label} 的真实产物引用(请直接读取、复用并验证)："
                                 + json.dumps(_dep_artifacts, ensure_ascii=False)[:16000])
        # Current and proven predecessor mail is projected by run_one after
        # admission, so messages arriving during queueing reach the real input.
        # Complete dependency results above retain their existing path.
        ctx = ("\n" + "\n".join(ctx_parts)) if ctx_parts else ""
        if deps and cb:
            # 如实措辞(军团自审 P0):到这里依赖已满足策略。区分"依赖真交付"(all_succeeded/
            # any_succeeded 走到这=有成功依赖)和"允许基于失败继续"(all_finished)——不再无条件
            # 说"依赖已完成"(那会在依赖其实失败时误导用户)。
            _bad = [d for d in deps if not _is_delivered(results.get(d))]
            if _bad and _policy == "all_finished":
                _msg = f"依赖 {', '.join(deps)} 已结束(其中 {', '.join(_bad)} 未成功·按策略继续复盘/审计)"
            else:
                _msg = f"依赖 {', '.join(deps)} 已交付，在它们的成果上继续"
            cb({"agent": k, "t": "reason", "team_id": team_id, "attempt": attempt, "x": _msg})
        _upstream_refs = []
        for dep in deps:
            _dr = results.get(dep) or {}
            _upstream_refs.extend(_dr.get("artifacts") or [])
            _upstream_refs.extend(_dr.get("discovered_artifacts") or [])
            try:
                _stored = _team_state.resolve_result(team_id, dep) if _team_state else {}
                if _stored.get("final_ref"):
                    _upstream_refs.append(_stored["final_ref"])
            except Exception:
                pass
        try:
            from legion_contracts import CapabilityPolicy, TaskFacts
            import hashlib
            _node_spec = raw_tasks.get(k) or {} if isinstance(raw_tasks.get(k), dict) else {}
            _verification_role = str(_node_spec.get("verification_role") or "")
            _verification_criteria = list(_node_spec.get("verification_criteria") or [])
            _project_snapshot = _project_snapshot_for(k)
            _task_facts = TaskFacts(
                goal=question, role=role_task or "回答问题", task_id=k, team_id=team_id,
                attempt_id=int(attempt or 0), input_hash=str(_node_spec.get("input_hash") or ""),
                idempotency_key=str(_node_spec.get("idempotency_key") or ""),
                delivery_contract=dict(plan.get("delivery_contract") or {}),
                orchestration={**(orchestration_spec.to_dict() if orchestration_spec else {}),
                               "assignment": {"task_id": k, "harness": harness_key,
                                              "source": _node_spec.get("assignment_source"),
                                              "native_capabilities": _node_spec.get("native_capabilities") or {}}},
                deliverables=list((plan.get("deliverables") or [])),
                acceptance=list((plan.get("acceptance") or [])) if isinstance(plan.get("acceptance"), list)
                           else ([str(plan.get("acceptance"))] if plan.get("acceptance") else []),
                upstream_artifacts=_upstream_refs,
                upstream_results=[{
                    "key": d, "status": (results.get(d) or {}).get("status"),
                    "error_envelope": (results.get(d) or {}).get("error_envelope"),
                    "harness": (results.get(d) or {}).get("assigned_harness")
                               or (results.get(d) or {}).get("harness"),
                    "model": (results.get(d) or {}).get("model_used") or (results.get(d) or {}).get("model"),
                    "final_sha256": hashlib.sha256(
                        str((results.get(d) or {}).get("final") or "").encode()).hexdigest(),
                    "artifacts": [{field: item.get(field) for field in
                                   ("path", "sha256", "export_snapshot_id")}
                                  for item in (results.get(d) or {}).get("artifacts") or []
                                  if isinstance(item, dict)],
                    "observation": "included_upstream_input; adoption unverified",
                } for d in deps],
                project_context=_project_snapshot,
            ).to_dict()
            _task_facts.update(_native_scope_facts(
                plan, _node_spec, request_id=team_id))
            _author_origin = plan.get("initial_author") or {}
            if isinstance(_author_origin, dict) and _author_origin.get("task_id") == k:
                _task_facts["orchestration"]["author_role"] = {
                    "task_id": k, "current_harness": harness_key,
                    "initial_harness": _author_origin.get("harness"),
                    "source": "current_delivery_task", "may_propose_task_graph": True,
                    "meaning": "任务组织提案可沿既有协议执行；目录、历史成绩和主创身份不授予额外权限",
                }
            _cap_policy = CapabilityPolicy.from_value(plan.get("capability_policy") or {}).to_dict()
        except PermissionError:
            raise
        except Exception:
            _node_spec, _verification_role, _verification_criteria = {}, "", []
            _project_snapshot = dict(_project_packet or {})
            _task_facts, _cap_policy = {}, {}
        # 概念锚点表下放(重放#3根修):研究的 concept_anchors 是全军团共同对照事实。
        # 任务书给翻译官/实现/验证时把锚点表注入节点任务文本——他们看到的世界级标准不再是
        # 抽象形容词而是具名锚点·不再依赖各自读包。fail-open:没锚点表时零改动(简单题不受影响)。
        try:
            _anchor_packet = (_project_context.load(team_id) or {}) if _project_context else {}
            _anchor_rows = [row for row in (_anchor_packet.get("concept_anchors") or [])
                            if isinstance(row, dict) and row.get("dimension")]
        except Exception:
            _anchor_rows = []
        _anchor_conflict = (_project_snapshot.get("anchor_channel_conflict") or {})
        _anchor_conflicted = bool(isinstance(_anchor_conflict, dict)
                                  and _anchor_conflict.get("unresolved"))
        if _anchor_conflicted:
            role_task = (
                f"{role_task}\n\n【研究来源存在差异，尚未由平台选定】\n"
                "当前单张 concept_anchors 索引不代表来源已经一致，也不代表其中一方已验证或必须采用。"
                "双方原始锚行和来源在 PROJECT_CONTEXT.concept_anchors_sources，"
                "差异记录在 anchor_channel_conflict，原文入口保留在上游产物引用中。"
                "保留用户明示要求，由你按来源和当前证据判断后续工作。")
        elif _anchor_rows and str(_node_spec.get("task_kind") or "") not in {"frontier_research"}:
            _anchor_lines = "\n".join(
                f"- 维度[{row.get('dimension')}] 对标[{row.get('reference')}]"
                f"{'(桂冠)' if row.get('is_crown') else ''}: {row.get('concept')}"
                + (f" (参照:{row.get('evidence_url')})" if row.get("evidence_url") else "")
                + (f" [必做行为:{row.get('required_behavior')}]" if row.get("required_behavior") else "")
                for row in _anchor_rows)
            # 资产必做条款(2026-09-08用户钉死'不是验收而是必做'+'必做且发挥更多'+二次钉死'彻查:领域词
            # 硬编码=不是通用'):不能枚举车/角色/gltf这类领域词——通用法:锚点表里任何带【可获取
            # 资产/资源URL】的行,其URL+用途都是【必做基线】直接写进任务文本;禁止行为由该行自己
            # 描述(锚点表自己写'禁止几何体拼凑'时才会出现·平台不发明领域惩罚词)。拿到基线后
            # 继续发挥到参照密度——锚点表参数级配方就是发挥方向。
            _asset_lines = []
            for row in _anchor_rows:
                _ref = str(row.get("reference") or "").strip()
                _url = str(row.get("evidence_url") or "").strip()
                if _url.startswith("http"):
                    _asset_lines.append((_ref or "资源", _url))
            role_task = (
                f"{role_task}\n\n【概念锚点表·你交付的世界级对照标准·逐锚对照不许跳过】"
                f"\n{_anchor_lines}\n"
                "你负责的交付必须达到锚点表对应维度的标准;桂冠锚是品类顶级标准不许降档。"
                "锚点表是底线不是终点:达标后继续向你判断的该品类最强形态推进——'能过验收'不是交差标准,"
                "'我能做出的最强版本'才是;禁止最小合规(满足字面但显然是最弱实现形态=未完成)。"
                + ("\n\n【资源必做·且基于此发挥更多】锚点表里带URL的每一条都是【必做基线】而非参考:直接获取并使用("
                   + "; ".join(f"{n}:{u}" for n, u in _asset_lines[:5])
                   + "等);锚点表对应维度若写明'禁止某做法',该做法即不允许;拿到基线后继续发挥到参照密度"
                   + "(按锚点表参数级配方强化),不是拿到资源摆上就完——基于基线发挥出更多更多。\n"
                   if _asset_lines else "")
                + "完成时在 final 里逐锚说明你的交付对应锚点做到了什么(对照到具体画面/参数/机制),"
                "没做到的如实说明并留下下一步。")
        _is_captain_synthesis = bool(plan.get("captain_synthesis") and k == _captain_key and deps)
        _synthesis_contract = ""
        if _is_captain_synthesis:
            _synthesis_contract = (
                "\n\n【总首脑最终汇总契约】\n"
                "你现在处于 barrier 汇总阶段，全部子队已结束。必须逐份读取上游完整结论与产物，"
                "消除冲突并给出一个可直接交付的最终结果；不得只说‘已收到/待汇总’。"
                "如果部分子队失败，明确列出已完成、失败、证据缺口和可执行下一步，但仍要完成当前可交付部分。"
                "最终答案必须包含：结论、证据、决策、实施顺序、验收标准、剩余风险。\n"
                "★这是给用户的最终交付物，不是转述目录或一句话概述：要把各子队的实质内容真正整合进来"
                "（关键数据、方案要点、取舍理由都要落到正文），篇幅与深度应与各子队产出相称——"
                "子队各交了数百字的专业内容，你的整合方案不应比任何单个子队更单薄。"
                "如果子队产出了文件/表格/图（见上游产物路径），在正文中引用并说明它们如何支撑最终方案。"
            )
        _verification_contract = ""
        if _verification_role:
            _anchor_prompt_rows = []
            _anchor_prompt_revision = 0
            try:
                _anchor_packet = (_project_context.load(team_id) or {}) if _project_context else {}
                _anchor_prompt_rows = [row for row in (_anchor_packet.get("concept_anchors") or [])
                                       if isinstance(row, dict) and row.get("dimension")]
                _anchor_prompt_revision = int(_anchor_packet.get("concept_anchors_revision") or 0)
            except Exception:
                _anchor_prompt_rows = []
            if _anchor_conflicted:
                # The explicitly chosen verifier receives the same unresolved
                # sources as the implementer, not a newly mandated single table.
                _anchor_prompt_rows = []
            _verification_contract = (
                f"\n\n【独立验证角色：{_verification_role}】\n"
                f"验收标准：{json.dumps(_verification_criteria, ensure_ascii=False)}\n"
                + (f"概念锚点表（逐锚判定·每个维度都必须有一行；当前锚点表 revision={_anchor_prompt_revision}）：{json.dumps(_anchor_prompt_rows, ensure_ascii=False)[:9000]}\n"
                   if _anchor_prompt_rows else "")
                 + "你必须独立检查上游真实输出、产物和可执行证据；不要因其他成员一致就放行。"
                 + "警惕最小合规:交付满足条款字面但显然是该要求的最弱实现形态(如'有头/躯干/四肢'交胶囊+球"
                   "两个几何体、'有音乐'交单次蜂鸣)时,verdict=revise并写明'这是最小合规交付,要求该形态的"
                   "合理最强实现'——条款是底线,验收的是差距是否真的关闭而不是字面是否勾选。"
                "先给人可读结论，末尾必须严格追加且只追加一次："
                "<<LEGION_VERIFY>>{\"verdict\":\"pass|revise|fail|blocked\","
                "\"findings\":[\"...\"],\"evidence\":[\"...\"],"
                "\"revision_request\":\"...\","
                "\"recommended_action\":\"keep_owner|replace_owner|replace_direction|inconclusive\","
                "\"replacement_requirements\":[\"...\"]"
                # P1-2 验证五层 verdict:runtime/flow/outcome/frontier/release 各层独立判定,
                # 低层 PASS 不得上推高层——机器能跑≠世界级达标。
                ",\"layer_verdicts\":{\"runtime\":\"pass|fail|unknown\",\"flow\":\"pass|fail|unknown\","
                "\"outcome\":\"pass|fail|unknown\",\"frontier\":\"pass|fail|unknown\",\"release\":\"pass|fail|unknown\"}"
                # P1-3 真人证据:有真人测试/审校事实时如实上报,没有就不填(不得编造)。
                ",\"human_evidence\":[{\"mode\":\"expert_review|novice_test|blind_evaluation\","
                "\"recruit_source\":\"...\",\"sample_size\":0,\"scale\":\"...\",\"evidence_artifact\":\"路径\"}]"
                + (f",\"anchors_revision\":{_anchor_prompt_revision},"
                   "\"anchor_verdicts\":[{\"dimension\":\"锚点表里的维度\",\"verdict\":\"pass|revise|fail\",\"evidence\":\"截图/路径/帧号\"}]"
                   "——锚点表每个维度一行；桂冠锚(is_crown)是品类顶级标准，达不到就 revise 不许放行"
                   if _anchor_prompt_rows else "")
                + "}<<END>>。\n"
                "recommended_action 必须基于可复现证据选择枚举值；不要把自然语言建议塞进该字段。"
                "direction_reviewer 还应在同一 JSON 中如实提供 current_hypothesis、sample_evidence、"
                "rendered_observations、gap_plan、claim_ceiling；不要为凑字段编造。"
                "没有这个完整 JSON 标记只会记为 unverified。"
            )
        task = (f"用户问题：{question}\n"
                "【分工来源】分工文本可能包含用户要求和规划者的诊断/实现选择。"
                "请保留用户明示的任务范围、交付约束及授权边界；"
                "规划选择不自动成为用户新增硬约束，需按原始来源理解，可据新证据提出修订。\n"
                f"你负责：{role_task}{ctx}{_synthesis_contract}{_verification_contract}{media_ctx}"
                if role_task else f"{question}{ctx}{_synthesis_contract}{_verification_contract}{media_ctx}")

        def _attempt_cb(event):
            # UX 心跳只证明父执行器还活着，不证明 Harness/工具仍有业务进展。若让
            # heartbeat/meaningful=false 无条件续租，本地 CLI 卡死会永久挡住恢复与接力。
            if _team_state and attempt is not None and _event_advances_attempt(event):
                try:
                    _team_state.renew_attempt(team_id, k, attempt)
                except Exception:
                    pass
            enriched = {**event, "agent": harness_key, "task_id": k,
                        "team_id": team_id, "attempt": attempt}
            if event.get("t") == "artifact_ready":
                if _project_context:
                    try:
                        _project_packet.clear()
                        _project_packet.update(_project_context.record_progress_artifact(
                            team_id, k, enriched, assigned_harness=harness_key))
                    except Exception:
                        pass
                if _delivery_milestone:
                    try:
                        _delivery_milestone.observe_artifact(enriched)
                    except Exception:
                        pass
            if cb:
                cb(enriched)

        # 网关并发闸:限同时真调网关的家数·复杂任务不互相挤爆(超出的在此排队·不丢)
        # attempt_timeout=None:不另加角色墙钟，沿用 run_one 的显式通用预算与运行管理。
        # direction 决定给它配对口模型；角色名称本身不是停止正在推进的工作的理由。
        _dir = _resolve_direction(plan, k, harness_key)
        _as = _assignments.get(harness_key, {})
        _explicit_depth = str((plan.get("execution_depths") or {}).get(k)
                              or (plan.get("execution_depths") or {}).get(harness_key) or "")
        _depth_scene = "verify" if _verification_role else str(_dir or "general")
        try:
            import runtime_manifest as _depth_manifest
            _supported_depths = list((_depth_manifest.get(harness_key).get("execution") or {}).get("depths")
                                     or ["standard", "deep"])
            if _explicit_depth:
                _execution_depth, _depth_reason = _explicit_depth, "explicit_plan"
            else:
                import harness_performance as _depth_perf
                _depth_pick = _depth_perf.recommend_depth(
                    harness_key, _depth_scene, _supported_depths, default="standard")
                _execution_depth = str(_depth_pick.get("depth") or "standard")
                _depth_reason = str(_depth_pick.get("reason") or "insufficient_samples")
        except Exception:
            _execution_depth, _depth_reason = (_explicit_depth or "standard"), "fallback"
        if cb:
            cb({"agent": harness_key, "task_id": k, "t": "execution_depth_selected",
                "stage": "preparing", "execution_depth": _execution_depth,
                "scene": _depth_scene, "reason": _depth_reason,
                "x": f"执行档位：{_execution_depth}（{_depth_reason}）"})
        _queue_cancel_check = lambda: _request_cancelled(qhash, k, harness_key)

        def _queue_wait(facts: dict) -> None:
            reason = str(facts.get("reason") or "capacity")
            wait_s = float(facts.get("wait_seconds") or 0.0)
            if _team_state:
                try:
                    _team_state.mark_queued(team_id, k, reason=reason,
                                            wait_seconds=wait_s, queue_facts=facts,
                                            owner=f"server:{os.getpid()}")
                except Exception:
                    pass
            if cb:
                label = "等待内存压力回落" if reason == "memory_pressure" else "等待可用执行槽"
                cb({"agent": harness_key, "task_id": k, "t": "task_queued", "status": "queued",
                    "stage": "queued", "phase": label, "team_id": team_id,
                    "x": f"{label}…(已 {wait_s:.1f}s·在役 {facts.get('active')}/{facts.get('process_limit')})",
                    "gateway_queue_wait": round(wait_s, 3), "queue": facts})

        _lease = _GatewayLease(_cost_class(harness_key, plan),
                               cancel_check=_queue_cancel_check, on_wait=_queue_wait,
                               priority=_lease_priority)
        try:
            with _lease:
                if _queue_cancel_check():
                    raise _GatewayLeaseCancelled("cancelled immediately after gateway lease")
                if _team_state:
                    try:
                        attempt = _team_state.begin_attempt(
                            team_id, k, owner=f"server:{os.getpid()}", dependency_ready=True)
                        attempts[k] = attempt
                        if isinstance(_task_facts, dict):
                            _task_facts["attempt_id"] = int(attempt or 0)
                            # 质量门重做原因（begin_attempt 留档）：供 run_one 拼进重做任务文本
                            _gate_reason = str(
                                ((_team_state.load(team_id) or {}).get("tasks") or {}).get(k, {}).get(
                                    "quality_gate_retry_of") or "")
                            if _gate_reason:
                                _task_facts["quality_gate_retry_of"] = _gate_reason
                    except RuntimeError as exc:
                        results[k] = {"key": k, "task_id": k, "harness": harness_key,
                                      "assigned_harness": harness_key, "status": "blocked",
                                      "final": "", "elapsed": 0.0,
                                      "error": str(exc), "blocked_by": ["active_lease"]}
                        return
                    except Exception:
                        attempt = None
                if cb:
                    if _lease.wait_seconds >= 0.05:
                        cb({"agent": harness_key, "task_id": k, "t": "queue_released",
                            "status": "running", "stage": "executing", "team_id": team_id,
                            "x": f"执行槽已就绪·排队 {_lease.wait_seconds:.2f}s 后开始",
                            "gateway_queue_wait": round(_lease.wait_seconds, 3)})
                    cb({"agent": harness_key, "task_id": k, "t": "task_started", "status": "running",
                        "phase": "理解任务", "team_id": team_id, "attempt": attempt,
                        "recoverable": bool(_team_state)})
                    cb({"agent": harness_key, "task_id": k, "t": "step_started", "status": "running",
                        "phase": "执行任务", "team_id": team_id, "attempt": attempt,
                        "x": "执行槽已获得·开始调用 Harness"})
                    cb({"agent": harness_key, "task_id": k, "t": "task_status", "status": "running",
                        "team_id": team_id, "attempt": attempt})
                # 同一 Harness 可被动态绑定到多个并行逻辑节点；项目级 conversation 保持共享，
                # 但原生 session 必须按 task_id 分槽，否则 Claude Code 等会报 session already in use。
                _task_conversation_id = f"{conversation_id or qhash}:task:{k}"
                # Only one lane for this harness in the entire plan may bridge
                # to its previous owned slot. Parallel nodes retain task slots.
                if (conversation_id and isinstance(_task_facts, dict)
                        and sum(value == harness_key for value in task_assignments.values()) == 1):
                    _task_facts["_native_resume_conversation"] = str(conversation_id)
                # Queue waiting may outlive another member's persisted update.
                # Refresh facts at admission too; do not change the assigned task.
                _task_facts["project_context"] = _project_snapshot_for(k)
                result = run_one(harness_key, task, qhash=f"{qhash}_task_{k}", cb=_attempt_cb if cb else None,
                                 preferred_model=_as.get("model"), preferred_appid=_as.get("appid"),
                                 _model_selection_source=str(_as.get("source") or ""),
                                 _continuation_result=(_continuation_results or {}).get(k),
                                 _provider_results=_provider_results,
                                 # 验证角色保留原有预算。研究只有显式正配置才覆盖通用预算；
                                 # 必须传 None 而非 0，避免抹掉调用方已配置的通用 attempt 上限。
                                 attempt_timeout=(_VERIFY_TIMEOUT if _verification_role else
                                                  (_RESEARCH_TIMEOUT if _node_spec.get("task_kind") == "frontier_research"
                                                   and _RESEARCH_TIMEOUT > 0
                                                   else None)),
                                 direction=_dir, user_id=user_id,
                                 conversation_id=_task_conversation_id,
                                 runtime_brief=_rt_brief, task_facts=_task_facts,
                                 capability_policy=_cap_policy,
                                 cancel_check=_queue_cancel_check,
                                 execution_depth=_execution_depth)
        except _GatewayLeaseCancelled:
            reason = (cancel_reason(qhash, k, scope="task")
                      if is_cancelled(qhash, k, scope="task")
                      else cancel_reason(qhash, harness_key, scope="harness"))
            if _team_state:
                try:
                    _team_state.cancel_queued(team_id, k, reason)
                except Exception:
                    pass
            result = {"key": k, "task_id": k, "harness": harness_key,
                      "assigned_harness": harness_key, "status": "cancelled", "final": "",
                      "elapsed": round(_lease.wait_seconds, 3), "error": reason,
                      "cancelled": True, "gateway_queue_wait": round(_lease.wait_seconds, 3)}
            results[k] = result
            if cb:
                cb({"agent": harness_key, "task_id": k, "t": "task_queue_cancelled",
                    "status": "cancelled", "stage": "cancelled", "team_id": team_id,
                    "x": f"排队期间已取消·未启动 Harness（等待 {_lease.wait_seconds:.1f}s）",
                    "gateway_queue_wait": round(_lease.wait_seconds, 3)})
                cb({"agent": harness_key, "task_id": k, "t": "task_status",
                    "status": "cancelled", "team_id": team_id})
            return
        result = dict(result)
        _effective_depth = str(result.get("execution_depth") or _execution_depth)
        result.update({"key": k, "task_id": k, "harness": harness_key,
                       "assigned_harness": harness_key, "execution_depth": _effective_depth,
                       "requested_execution_depth": _execution_depth,
                       "depth_scene": _depth_scene, "depth_reason": _depth_reason,
                       "task_kind": str(_node_spec.get("task_kind") or "delivery"),
                       "supporting_context": bool(_node_spec.get("supporting_context")),
                       "contribution_input_refs": deepcopy(_task_facts.get("upstream_results") or [])})
        if isinstance(_node_spec.get("native_capabilities"), dict):
            result["native_capabilities"] = deepcopy(_node_spec["native_capabilities"])
        try:
            import token_telemetry as _token_telemetry
            _token_telemetry.attach(result)
        except Exception:
            result.setdefault("usage", {"available": False, "source": "unavailable",
                                        "model": str(result.get("model_used") or result.get("model") or ""),
                                        "elapsed_s": result.get("elapsed")})
        result["gateway_queue_wait"] = round(_lease.wait_seconds, 3)
        try:
            import harness_performance as _depth_perf
            _depth_perf.record_depth(
                harness_key, _depth_scene,
                str(result.get("model_used") or result.get("model") or "unknown"),
                _effective_depth, ok=_is_delivered(result),
                wall_s=float(result.get("elapsed") or 0), source="runtime")
        except Exception:
            pass
        if _node_spec.get("verification_role"):
            try:
                from legion_contracts import parse_verification_report
                _clean_final, _report = parse_verification_report(str(result.get("final") or ""))
                result["final"] = _clean_final
                result["verification_role"] = str(_node_spec.get("verification_role"))
                result["verification_targets"] = list(deps)
                result["verification_report"] = _report or {
                    "verdict": "unverified", "findings": [], "evidence": [],
                    "revision_request": "验证者未返回结构化报告",
                }
                # 逐锚对照(重放#3实锤:研究引用了Ghostrunner但交付物锚在Subway Surfers三车道
                # ——验证若不逐条对照锚点表,品类桂冠只在视觉维生效,玩法/手感锚全是平盾句)。验证
                # 者必须沿共享现场的concept_anchors逐锚给"达标/未达标"的逐项判定,不许一句“整体还行”糊过去。
                _anchor_rows = []
                try:
                    _anchor_source = ((_project_packet or {}).get("concept_anchors")) if isinstance(_project_packet, dict) else None
                    if not _anchor_source and _project_context:
                        _anchor_source = (_project_context.load(team_id) or {}).get("concept_anchors")
                    _anchor_rows = [row for row in (_anchor_source or [])
                                    if isinstance(row, dict) and row.get("dimension")]
                except Exception:
                    _anchor_rows = []
                if _anchor_rows and not _anchor_conflicted:
                    result["verification_report"]["anchor_verdicts"] = [
                        {"dimension": str(row.get("dimension") or ""),
                         "reference": str(row.get("reference") or ""),
                         "concept": str(row.get("concept") or ""),
                         "verdict": "unverified"} for row in _anchor_rows]
                    _need_anchor_answer = not any(
                        isinstance(_v, dict) and _v.get("dimension")
                        for _v in (_report or {}).get("anchor_verdicts") or [])
                    if _need_anchor_answer:
                        result["verification_report"]["revision_request"] = (
                            str(result["verification_report"].get("revision_request") or "")
                            or "请逐条回答 concept_anchors 里每个维度的达标判定与证据;任一维度无判定都算未通过")
                # P1-2 五层 verdict 归一:验证者没分层回答时如实标 unknown(不是 pass),
                # 低层 PASS 不得上推高层——release 门看 frontier 层。
                _layers_given = (_report or {}).get("layer_verdicts") \
                    if isinstance((_report or {}).get("layer_verdicts"), dict) else {}
                result["verification_report"]["layer_verdicts"] = {
                    layer: (str(_layers_given.get(layer) or "unknown").strip().lower()
                            if str(_layers_given.get(layer) or "unknown").strip().lower()
                            in {"pass", "fail", "revise", "unknown"} else "unknown")
                    for layer in ("runtime", "flow", "outcome", "frontier", "release")}
                # P0-3:声明本报告消费的锚点 revision(缺声明时回填 packet 当前值,
                # 让 stale 检测只对真过期生效;首锚时 packet 还没写盘则从 load 读)。
                if not result["verification_report"].get("anchors_revision") and not _anchor_conflicted:
                    try:
                        _vr_packet = (_project_context.load(team_id) or {}) if _project_context else {}
                        result["verification_report"]["anchors_revision"] = int(
                            _vr_packet.get("concept_anchors_revision") or 0)
                    except Exception:
                        pass
                # verify/audit 硬超时如实标注(2026-09-07):超时/失败/取消不挂 running不阻塞交付,
                # 验证状态如实回流给用户与后续轮次。
                # 半截发现不得丢(重放#2实锤:prime超时前已抓到伪3D硬事实,但整个final被当unverified
                # 丢弃→牙齿咬到了猎物哨声却没吹响→REVISE永不触发)。超时/失败但final里有实质发现
                # (含阻断关键词)时:保留已有发现升级为revise而非unverified——这些是验证者用真
                # 实检查换来的事实,丢弃它们=验证白做。
                _v_status = str(result.get("status") or "")
                if not _report and _v_status and _v_status not in ("done", "delivered"):
                    _halfway = str(result.get("final") or "")
                    _blocking_hits = [kw for kw in (
                        "getContext('2d')", 'getContext("2d")', "伪3D", "伪 3D", "不是真实 3D", "不是真 3D",
                        "127.0.0.1", "localhost", "未部署", "没有公网", "无公网",
                        "CanvasRenderingContext2D", "手写透视", "手工透视",
                    ) if kw in _halfway]
                    if _blocking_hits and len(_halfway) >= 120:
                        result["verification_report"] = {
                            "verdict": "revise",
                            "findings": [f"验证者超时前已发现的阻断事实:{kw}" for kw in _blocking_hits[:4]]
                                        + [f"验证未完成(状态:{_v_status}·硬超时{_VERIFY_TIMEOUT:.0f}s·以下为已验证部分发现)"],
                            "evidence": ["超时截断的final前文·发现已人工核对在交付物中"],
                            "revision_request": ("基于超时前已确认的阻断事实要求返工:" + "、".join(_blocking_hits[:4])
                                                 + ";完整验证未跑完,返工后需重跑完整验证"),
                        }
                    else:
                        result["verification_report"]["findings"] = [
                            f"验证未完成(执行状态:{_v_status}·硬超时{_VERIFY_TIMEOUT:.0f}s),不阻塞交付,如实标注"]
                result["verification_report"].update({
                    "role": result["verification_role"], "task_id": k,
                    "targets": list(deps),
                })
            except Exception:
                pass
        # P0-7 effect-based done(治R7):done 必须有运行效果证据——research=有效协议,
        # delivery/revision=非空 artifact 且非将来时承诺,verify=结构化 verdict。
        # 将来时承诺、0 artifact、无结构化verdict一律降 incomplete(接力/failover照常生效)。
        try:
            result = _apply_effect_based_done(result, _node_spec, team_id, k, plan)
        except Exception:
            pass
        accepted = True
        if _team_state and attempt is not None:
            accepted = _team_state.finish_attempt(team_id, k, attempt, result)
        if not accepted:
            return  # 旧 attempt 迟到·不能覆盖超时/重派后的状态
        results[k] = result
        if _project_context:
            try:
                _project_packet.clear()
                _project_packet.update(_project_context.record_result(
                    team_id, k, result, assigned_harness=harness_key))
                # 评审事实必须在节点结束时立刻进入共享 Context；若等所有节点结束后才记录，
                # 已依赖 direction_review 的实现节点会拿到旧快照，样片评审形同虚设。
                if result.get("verification_role") and isinstance(result.get("verification_report"), dict):
                    _project_packet.clear()
                    _project_packet.update(_project_context.record_verification(
                        team_id, dict(result["verification_report"])))
            except Exception:
                pass
        if _delivery_milestone:
            try:
                _report = result.get("verification_report") \
                    if isinstance(result.get("verification_report"), dict) else {}
                _delivery_milestone.observe_context(
                    _project_packet,
                    include_gaps=bool(result.get("verification_role") == "direction_reviewer"
                                      and _report.get("gap_plan")),
                    include_journey=bool(result.get("journey_observations") or
                                         _report.get("journey_observations")))
                _delivery_milestone.update(list(results.values()))
            except Exception:
                pass
        if _team_state:
            # 存完整成果进 mailbox(军团自审 #3:不再预截 4000·team_state 有 200000 上限兜底防膨胀)。
            # 这条主要供 durable 重启回放;下游接力的依赖成果走内存 results 完整 final(见 _worker 上下文
            # 拼接·不读这条截断摘要)。
            summary = str(result.get("final") or result.get("error") or result.get("status"))
            try:
                _team_state.send_message(team_id, k, "commander", summary)
                for child in keys:
                    if k in dependencies.get(child, []):
                        _team_state.send_message(team_id, k, child, summary)
            except Exception:
                pass
        if cb:
            _st = result.get("status")
            _visible_status = "done" if _st == "done" else ("cancelled" if _st == "cancelled" else "error")
            _life = "task_completed" if _visible_status == "done" else "task_failed"
            # task_completed 带完整 final（2026-09-18）：PK 逐家交付渐进展示——前端在
            # 交付时刻即渲染该家完整答案（此前只有 result 事件的 final[:300] 摘要，
            # 完整答案要等整单 final 终态才回填）。终态回填保留（权威），此处让
            # 用户"有一家先看一家"。
            cb({"agent": harness_key, "task_id": k, "t": _life, "status": _visible_status,
                "phase": "整理交付" if _visible_status == "done" else "自救收尾",
                "team_id": team_id, "attempt": attempt,
                "recoverable": bool(_team_state), "usage": result.get("usage"),
                "artifact_count": len(result.get("artifacts") or []),
                **({"final": str(result.get("final") or "")}
                   if _visible_status == "done" and str(result.get("final") or "").strip() else {})})
            cb({"agent": harness_key, "task_id": k, "t": "task_status", "status": _visible_status,
                "team_id": team_id, "attempt": attempt})



    # 补位接力(用户2026-08-28核心:一家坏了没交付·别家补队接力修复)。抽成函数供【逐 wave 后】
    # 调用(P0-1 修复)+末尾兜底调用。默认策略保持事件驱动；离线 A/B 可显式重放旧 wave barrier，
    # 但普通生产请求不从任务文本推断、不自动降级。max_failover 同理只消费调用方明确的运行策略。
    _runtime_team_policy = plan.get("team_policy") if isinstance(plan.get("team_policy"), dict) else {}
    _scheduler_policy = str(_runtime_team_policy.get("scheduler_policy") or "event_driven").strip().lower()
    if _scheduler_policy not in {"event_driven", "wave_barrier"}:
        _scheduler_policy = "event_driven"
    try:
        _MAX_FAILOVER = max(0, int(_runtime_team_policy.get(
            "max_failover", os.environ.get("LEGION_MAX_FAILOVER", "2"))))
    except (TypeError, ValueError):
        _MAX_FAILOVER = 2

    def _do_failover(subset):
        """并行补位当前 wave 的独立失败节点；单节点内部仍按轮次顺序自救。

        旧版 `for k: run_one(...)` 把多个互不依赖的补位串行化。12 家真跑时 4 个失败节点
        全由 codex 接力，整单墙钟 1325s（最长单家仅 396s）。补位彼此没有依赖，应和首轮一样
        受 GatewayLease 控制并行；同一节点的第 2 轮仍必须等第 1 轮失败后再换家。
        """
        def _rescue(k):
            failed_harness = task_assignments.get(k) or k
            with failover_lock:
                if k in failover_active:
                    return
                failover_active.add(k)
            if is_cancelled(qhash):
                return
            r = results.get(k)
            if (r or {}).get("status") == "cancelled":
                return
            from error_envelope import requires_execution_pause, requires_request_budget_pause
            if requires_execution_pause(r) or _native_terminal_message_absent(r):
                return
            if any(requires_request_budget_pause(row) for row in _provider_results()):
                return
            _failover_node = raw_tasks.get(k) if isinstance(raw_tasks.get(k), dict) else {}
            # 锁定的独立验证角色失败时不能由被审作者/调查者接力，否则“独立核验”会静默变成自证。
            # 保留失败态交给上层重新选择真正独立的评委，而不是降低证据标准换取表面成功。
            if _failover_node.get("verification_role") and _failover_node.get("assignment_locked"):
                if cb:
                    cb({"agent": "commander", "t": "verification_result", "task_id": k,
                        "status": "unverified", "x": "独立验证者未交付；该验证任务禁止普通补位冒充原评委"})
                return
            if _is_delivered(r) or k in missing_deps or (r or {}).get("status") == "blocked":
                return
            tried = {failed_harness}
            relay_rounds = 0
            # 环境故障逃生(2026-09-23):bwrap/namespace 耗尽时按失败分类选家——
            # 换 codex 只会撞同一堵墙,优先无内建 bwrap 沙盒的家
            _rescue_kind = str(((r or {}).get("error_envelope") or {}).get("category")
                               or (r or {}).get("fail_kind") or "")
            while (relay_rounds < _MAX_FAILOVER
                   and not is_cancelled(qhash)
                   and not is_cancelled(qhash, k, scope="task")
                   and not requires_execution_pause(r)
                   and not any(requires_request_budget_pause(row) for row in _provider_results())):
                relay = _pick_relay_harness(failed_harness, tried, valid, _rescue_kind)
                if not relay:
                    break
                tried.add(relay)
                relay_rounds += 1
                prev_err = str((r or {}).get("error") or (r or {}).get("final") or (r or {}).get("status") or "未知失败")
                if cb:
                    cb({"agent": "commander", "t": "reason",
                        "x": f"⚠️ 任务 {k} 的执行者 {failed_harness} 没交付({prev_err[:60]}）"
                             f"→ 让 {relay} 补位接力",
                        "failover": {"task_id": k, "failed": failed_harness,
                                     "relay": relay, "round": relay_rounds}})
                _continuation = {
                    "continuation_of": k, "handoff_from": failed_harness,
                    "handoff_to": relay, "handoff_reason": prev_err[:1000],
                    "logical_conversation_id": str(conversation_id or qhash),
                    "attempt_round": relay_rounds,
                }
                _prior_relay_owner = str((r or {}).get("assigned_harness")
                                         or (r or {}).get("harness") or failed_harness)
                _relay_bound = False
                def _restore_unstarted_relay_binding():
                    nonlocal _relay_bound
                    if not (_project_context and _relay_bound):
                        return
                    try:
                        # Compare-and-set under the project lock: never overwrite
                        # another actor's newer assignment or task facts.
                        _project_context.record_task_bindings(
                            team_id, {k: {"assigned_harness": _prior_relay_owner}},
                            user_id=user_id, conversation_id=str(conversation_id or qhash),
                            expected_assigned_harnesses={k: relay}, assignments_only=True)
                        _relay_bound = False
                    except Exception as exc:
                        if cb:
                            cb({"agent": "commander", "t": "task_status", "task_id": k,
                                "status": "blocked", "phase": "接力归属恢复",
                                "error_type": type(exc).__name__,
                                "x": "未启动的接力归属无法安全恢复；保留现有项目记录。"})
                if _project_context:
                    # Bind before execution under the existing owner lock. This
                    # changes no workspace, user, grant or dependency.
                    try:
                        _project_context.record_task_bindings(
                            team_id, {k: {"assigned_harness": relay}},
                            user_id=user_id, conversation_id=str(conversation_id or qhash),
                            expected_assigned_harnesses={k: _prior_relay_owner}, assignments_only=True)
                        _relay_bound = True
                    except Exception as exc:
                        if cb:
                            cb({"agent": "commander", "t": "task_status", "task_id": k,
                                "status": "blocked", "phase": "接力归属校验",
                                "error_type": type(exc).__name__,
                                "x": "接力归属未能保存；保留原执行现场，不启动未绑定成员。"})
                        break
                    if _request_cancelled(qhash, k, relay):
                        _restore_unstarted_relay_binding()
                        break
                _relay_project_context = _project_snapshot_for(k)
                relay_task = (
                    f"用户原始目标：{question}\n"
                    f"你在继续逻辑任务 {k}，不是从零开始。原执行者 {failed_harness} 未收口：{prev_err[:1000]}\n"
                    "你来接力延续这一节点。先读取 PROJECT_CONTEXT_JSON、TASK_MEMORY_JSON 与已有产物，"
                    "复现当前现场；保留已通过部分，只推进 next riskiest gap，并把修复与验证写回同一项目。\n"
                    f"你负责:{tasks.get(k) or '回答用户问题'}{media_ctx}")
                try:
                    import legion_workdir as _wd
                    _relay_ctx = _wd.summarize_for_relay(qhash, k)
                    if _relay_ctx:
                        relay_task += _relay_ctx
                except Exception:
                    pass
                # task_memory 是运行时产物事实源；部分 Harness 不写 legion_workdir/state.json，
                # 但 run_one 已扫描并持久化 artifacts。把这些引用也明确交给接力家，避免成品已在
                # 磁盘却因原成员空 final 而被接力家从零重做。这里只给路径 context，不宣称已通过。
                try:
                    import artifact_provenance as _artifact_provenance
                    _partial_artifacts = _artifact_provenance.merge_artifacts(
                        (r or {}).get("artifacts"), (r or {}).get("discovered_artifacts"))
                except Exception:
                    _partial_artifacts = [*(r or {}).get("artifacts", []),
                                          *(r or {}).get("discovered_artifacts", [])]
                if _partial_artifacts:
                    relay_task += ("\n\n【失败成员已生成的部分产物·请先打开验证，合格就复用并补齐最终交付，"
                                   "不合格再修；不要仅凭文件存在宣称完成】\n" +
                                   json.dumps(_partial_artifacts, ensure_ascii=False)[:12000])
                _rdir = _resolve_direction(plan, k, relay)
                # 未配置补位专用预算时，继承 run_one 的通用预算，不能暗加 300 秒截断。
                # 显式配置仍沿用原有约定；取消、原生调用监护和进程回收保持不变。
                _relay_timeout = (max(60.0, float(os.environ["LEGION_FAILOVER_TIMEOUT"]))
                                  if "LEGION_FAILOVER_TIMEOUT" in os.environ else None)
                # 多个失败节点可同时由同一家接力；每个节点用独立 workdir/native session，
                # 但 lease 和执行都持续读取原请求的 task/harness 取消令牌。
                relay_qhash = f"{qhash}_relay_{k}"
                relay_conversation_id = f"{conversation_id or qhash}:task:{k}"
                _failed_result = r or {}
                _relay_facts = {
                    "goal": question, "role": str(tasks.get(k) or "回答用户问题"),
                    "task_id": k, "team_id": team_id,
                    "delivery_contract": dict(plan.get("delivery_contract") or {}),
                    "orchestration": (orchestration_spec.to_dict() if orchestration_spec else {}),
                    "upstream_artifacts": _partial_artifacts,
                    "upstream_results": [{"key": k, "status": _failed_result.get("status"),
                                          "error_envelope": _failed_result.get("error_envelope")}],
                    "project_context": _relay_project_context,
                }
                _relay_facts.update(_native_scope_facts(
                    plan, _failover_node, request_id=team_id))
                _relay_cancel = lambda _k=k, _relay=relay: _request_cancelled(qhash, _k, _relay)
                try:
                    import model_registry as _relay_models
                    _relay_assignment = _relay_models.resolve_assignments(
                        {"model_overrides": plan.get("model_overrides") or {},
                         "directions": {relay: _rdir}}, [relay]).get(relay, {})
                    if not str(_relay_assignment.get("source") or "").startswith(
                            ("operator_binding", "request_override", "user_fixed")):
                        _relay_assignment = {}  # Preserve existing automatic relay selection.
                except Exception:
                    _relay_assignment = {}
                try:
                    relay_res = _run_one_with_gateway(
                        relay, relay_task, lease_plan=plan, cancel_check=_relay_cancel,
                        preferred_model=_relay_assignment.get("model"),
                        preferred_appid=_relay_assignment.get("appid"),
                        _provider_results=_provider_results,
                        qhash=relay_qhash,
                        cb=(lambda ev, _k=k, _cb=cb: _cb({**ev, "relay_for": _k})) if cb else None,
                        attempt_timeout=_relay_timeout, direction=_rdir, user_id=user_id,
                        conversation_id=relay_conversation_id,
                        runtime_brief=_rt_brief, task_facts=_relay_facts)
                except BaseException:
                    _restore_unstarted_relay_binding()
                    raise
                if ((relay_res or {}).get("status") == "cancelled"
                        and not (relay_res or {}).get("execution_calls")):
                    _restore_unstarted_relay_binding()
                    break
                if ((relay_res or {}).get("blocked_before_native")
                        and not (relay_res or {}).get("execution_calls")):
                    _restore_unstarted_relay_binding()
                    # No relay author ran. Preserve the original native result
                    # and artifacts while attaching only the trusted request
                    # budget stop; never relabel it as the relay's failure.
                    relay_facts = relay_res.get("native_facts") if isinstance(relay_res, dict) else {}
                    observed_budget = (relay_facts.get("runtime_budget_observation")
                                       if isinstance(relay_facts, dict) else None)
                    owner_fact = observed_budget if isinstance(observed_budget, dict) else relay_res
                    held = _request_budget_pause(lambda: [owner_fact], dict(r or {}))
                    if held is None:
                        # Preserve a non-budget admission refusal separately;
                        # the attempted replacement did not execute and is not
                        # the author of the original response or files.
                        held = dict(r or {})
                        native = dict(held.get("native_facts") or {})
                        native["unstarted_relay_admission"] = {
                            "harness": relay, "blocked_before_native": True,
                            **{field: deepcopy(relay_res[field]) for field in
                               ("status", "error", "error_envelope", "fail_kind") if field in relay_res}}
                        held["native_facts"] = native
                        if isinstance(relay_res.get("error_envelope"), dict):
                            native.setdefault("execution_failure", {field: deepcopy(held[field]) for field in
                                              ("status", "error", "error_envelope", "fail_kind") if field in held})
                            held["error_envelope"] = deepcopy(relay_res["error_envelope"])
                            held["fail_kind"] = str(relay_res["error_envelope"].get("category") or "")
                    held.update(key=k, task_id=k, harness=_prior_relay_owner,
                                assigned_harness=_prior_relay_owner)
                    results[k] = held
                    try:
                        if _team_state and not _team_state.replace_result(
                                team_id, k, held, previous_harness=_prior_relay_owner,
                                reason="relay_blocked_before_native"):
                            raise RuntimeError("blocked_relay_task_missing")
                    except Exception as exc:
                        held.setdefault("continuation_persistence_errors", []).append({
                            "store": "team_state", "error": type(exc).__name__})
                    break
                if is_cancelled(qhash) or is_cancelled(qhash, k, scope="task"):
                    if not (relay_res or {}).get("execution_calls"):
                        _restore_unstarted_relay_binding()
                    break
                previous = dict(r or {})
                previous_harness = str(previous.get("harness") or failed_harness)
                previous_attempts = list(previous.pop("prior_attempts", None) or [])
                relay_res = dict(relay_res)
                relay_res["prior_attempts"] = [
                    *previous_attempts, previous, *(relay_res.get("prior_attempts") or []),
                ]
                _continuation = {**_continuation, "handoff_from": previous_harness}
                _relay_message_absent = _native_terminal_message_absent(relay_res)
                if _is_delivered(relay_res) or _relay_message_absent:
                    try:
                        import artifact_provenance as _artifact_provenance
                        _relay_artifacts = _artifact_provenance.merge_artifacts(
                            _partial_artifacts, relay_res.get("artifacts"),
                            relay_res.get("discovered_artifacts"))
                        _artifact_provenance.attach_lineage(
                            _relay_artifacts, objective=question,
                            task=str(tasks.get(k) or "回答用户问题"), task_id=k,
                            agent=relay, actions=relay_res.get("tool_trace") or [],
                            inherited_from=previous_harness)
                        if _relay_artifacts:
                            relay_res["artifacts"] = _relay_artifacts
                            relay_res["inherited_artifacts"] = [
                                item for item in _relay_artifacts
                                if any(handoff.get("from") == previous_harness
                                       for handoff in (item.get("lineage") or {}).get("handoffs", []))]
                    except Exception:
                        pass
                    relay_res["relayed_from"] = failed_harness
                    relay_res["key"] = k
                    relay_res["task_id"] = k
                    relay_res["harness"] = relay
                    relay_res["assigned_harness"] = relay
                    relay_res["relay_by"] = relay
                    relay_res["via_relay"] = True   # 补位交付标记(outcome.via_failover 消费·体检可辨"靠接力")
                    relay_res["continuation"] = _continuation
                    # 接力交付也要解析验证协议(2026-09-07真跑实锤:verify超时被接力时
                    # REVISE结论埋在接力final里无人解析→revision轮永不触发)。接力的是
                    # 节点角色不只是任务文本。
                    relay_res = _attach_verification_result_fields(
                        relay_res, raw_tasks.get(k), dependencies.get(k, []))
                    results[k] = relay_res
                    if _project_context:
                        try:
                            _project_packet.clear()
                            _project_packet.update(_project_context.record_result(
                                team_id, k, relay_res, assigned_harness=relay,
                                continuation=_continuation))
                        except Exception:
                            pass
                    task_assignments[k] = relay
                    if isinstance(raw_tasks.get(k), dict):
                        raw_tasks[k]["assigned_harness"] = relay
                        for field in ("assign_to", "harness", "agent"):
                            if field in raw_tasks[k]:
                                raw_tasks[k][field] = relay
                    try:
                        if _team_state:
                            _team_state.update_assignment(team_id, k, relay, reason="failover")
                            _team_state.replace_result(team_id, k, relay_res,
                                                       previous_harness=previous_harness,
                                                       reason="failover")
                    except Exception:
                        pass
                    if _delivery_milestone:
                        try:
                            _delivery_milestone.update(list(results.values()))
                        except Exception:
                            pass
                    if cb:
                        cb({"agent": "commander", "t": "result", "task_id": k,
                            "x": (f"{relay} 原生执行已结束但本轮最终正文缺失；保留任务 {k} 的原生现场"
                                  if _relay_message_absent else
                                  f"✅ {relay} 接力成功·补上任务 {k} 的交付")})
                        if _partial_artifacts:
                            cb({"agent": relay, "t": "artifact_handoff", "stage": "artifact_ready",
                                "task_id": k, "status": "ready_for_verification",
                                "artifact_count": len(_partial_artifacts),
                                "artifacts": _partial_artifacts,
                                "x": f"已接收 {failed_harness} 留下的 {len(_partial_artifacts)} 个产物，进入复验"})
                    break
                # Updating only local r stops this loop but leaves the outer
                # request and durable node looking at the original failure.
                try:
                    import artifact_provenance as _artifact_provenance
                    relay_res["artifacts"] = _artifact_provenance.merge_artifacts(
                        _partial_artifacts, relay_res.get("artifacts"),
                        relay_res.get("discovered_artifacts"))
                except Exception:
                    relay_res["artifacts"] = [
                        *_partial_artifacts, *(relay_res.get("artifacts") or []),
                        *(relay_res.get("discovered_artifacts") or []),
                    ]
                relay_res.update({
                    "key": k, "task_id": k, "harness": relay, "assigned_harness": relay,
                    "relayed_from": failed_harness, "relay_by": relay, "via_relay": False,
                    "continuation": _continuation,
                })
                results[k] = relay_res
                task_assignments[k] = relay
                if isinstance(raw_tasks.get(k), dict):
                    raw_tasks[k]["assigned_harness"] = relay
                    for field in ("assign_to", "harness", "agent"):
                        if field in raw_tasks[k]:
                            raw_tasks[k][field] = relay
                if _project_context:
                    try:
                        packet = _project_context.record_result(
                            team_id, k, relay_res, assigned_harness=relay,
                            continuation=relay_res["continuation"])
                        _project_packet.clear()
                        _project_packet.update(packet)
                    except Exception as exc:
                        relay_res.setdefault("continuation_persistence_errors", []).append({
                            "store": "project_context", "error": type(exc).__name__})
                if _team_state:
                    try:
                        _team_state.update_assignment(team_id, k, relay, reason="failed_relay")
                        if not _team_state.replace_result(
                                team_id, k, relay_res, previous_harness=previous_harness,
                                reason="failed_relay"):
                            raise RuntimeError("failed_relay_task_missing")
                    except Exception as exc:
                        relay_res.setdefault("continuation_persistence_errors", []).append({
                            "store": "team_state", "error": type(exc).__name__})
                r = relay_res
                if cb:
                    cb({"agent": "commander", "t": "reason",
                        "x": f"{relay} 接力要求暂停·原生原因和已有成果已交回原请求"
                             if requires_execution_pause(relay_res) else
                             f"{relay} 接力仍未通·继续找下一个能接的家" if relay_rounds < _MAX_FAILOVER
                             else f"接力用尽·{k} 这一单确实没能交付"})

        workers = []
        for key in list(subset):
            thread = threading.Thread(target=_rescue, args=(key,), daemon=True)
            thread.start()
            workers.append(thread)
        for thread in workers:
            thread.join()

    # 不设父层军团总墙钟(用户2026-08-28钉死·实测45s会把能成功的jcode杀成白卷)。
    # 每家由自己的harness循环自主收敛;父层只等本层全部结束再放行DAG下一层。真正卡死由
    # run_one 的单进程安全上限兜住并把失败回流重试,不是父层按"跑得久"判失败。
    _coordination_sequence = 0
    _coordination_history: list[dict] = []
    from error_envelope import requires_execution_pause

    def _snapshot_execution_plan():
        snapshot = deepcopy(plan)
        snapshot["tasks"] = {
            key: {**deepcopy(raw_tasks.get(key) or {}),
                  "task": tasks.get(key) or "回答问题",
                  "assigned_harness": task_assignments.get(key) or "",
                  "depends_on": list(dependencies.get(key) or []),
                  "dependency_policy": dependency_policies.get(key, _DEFAULT_DEP_POLICY)}
            for key in tasks}
        return snapshot

    def _return_wave_boundary(proposal, pending):
        # All workers and relays in this wave have joined. Pending nodes have
        # no result yet: do not send them through tail failover/critic/repair.
        _intermediate_stop.set()
        snapshot = _snapshot_execution_plan()
        if _execution_plan is not None:
            _execution_plan.update(deepcopy(snapshot))
        ordered = [results[key] for key in keys if key in results]
        cancelled = bool(is_cancelled(qhash) or proposal.get("cancelled"))
        outcome = _typed_outcome(ordered, cancelled)
        delivered = int(outcome.get("delivered_count") or 0)
        all_failed = (bool(ordered) and outcome.get("status") not in {"completed", "cancelled"}
                      and delivered == 0)
        outcome.update(status="cancelled" if cancelled else "incomplete", total=len(tasks))
        return {
            "results": ordered, "team_id": team_id, "dependencies": deepcopy(dependencies),
            "task_assignments": dict(task_assignments), "hierarchy": dict(hierarchy),
            "coordination_history": list(_coordination_history), "coordination": proposal,
            "outcome": outcome, "status": "cancelled" if cancelled else "partial",
            "cancelled": cancelled, "partial": not cancelled,
            "delivered_count": delivered,
            "total": len(tasks), "all_failed": all_failed,
            "_coordination_handoff": {
                "proposal": deepcopy(proposal), "pending_task_ids": list(pending),
                "execution_plan": snapshot,
            },
        }

    for lvl in sorted(waves):
        _project_packet = _current_project_packet()
        plan["project_context"] = deepcopy(_project_packet)
        completed_queue = __import__("queue").Queue()
        threads = []
        def _event_worker(task_id):
            try:
                _worker(task_id)
            finally:
                completed_queue.put(task_id)
        for k in waves[lvl]:
            th = threading.Thread(target=_event_worker, args=(k,), daemon=True)
            th.start()
            threads.append((k, th))
        if _scheduler_policy == "wave_barrier":
            # 旧运行策略仅供同 revision 离线 A/B 重放：整层全部结束后才补位。默认生产路径不走这里。
            for _k, th in threads:
                th.join()
            for k, _th in threads:
                if k not in results:
                    harness = task_assignments.get(k) or k
                    results[k] = {"key": k, "task_id": k, "harness": harness,
                                  "assigned_harness": harness, "status": "error", "final": "", "elapsed": 0.0,
                                  "error": "harness worker 意外退出且未返回结果"}
                    if cb:
                        cb({"agent": harness, "task_id": k, "t": "result", "x": results[k]["error"]})
            _do_failover([k for k in waves[lvl] if not _is_delivered(results.get(k))])
            continue
        # 真事件驱动释放：worker 完成即写 queue，主线程阻塞等通知；不再每 250ms 轮询所有线程。
        _pending = {k: th for k, th in threads}
        _rescue_threads, _early_rescued = [], set()
        while _pending:
            k = completed_queue.get()
            th = _pending.pop(k, None)
            if th:
                th.join()
            if k not in results:
                harness = task_assignments.get(k) or k
                results[k] = {"key": k, "task_id": k, "harness": harness,
                              "assigned_harness": harness, "status": "error", "final": "", "elapsed": 0.0,
                              "error": "harness worker 意外退出且未返回结果"}
                if cb:
                    cb({"agent": harness, "task_id": k, "t": "result", "x": results[k]["error"]})
            if not _is_delivered(results.get(k)):
                _early_rescued.add(k)
                _rt = threading.Thread(target=_do_failover, args=([k],), daemon=True)
                _rt.start()
                _rescue_threads.append(_rt)
        for _rt in _rescue_threads:
            _rt.join()
        # P0-1:本 wave 结束后对本层仍失败节点兜底扫一遍(事件驱动路径已处理过的不再重试·
        # 防双重补位烧网关;这里兜 _rescue 内部跳过的边界)。上游救活→下一 wave 的 blocked
        # 下游能真跑(不再等所有 wave 后·避免 blocked 下游永久饿死)。已交付的会被跳过。
        _do_failover([k for k in waves[lvl]
                      if k not in _early_rescued and not _is_delivered(results.get(k))])
        # Non-graph updates retain the current waves. A graph-changing proposal
        # crosses this joined-wave boundary intact, before any old successor runs.
        _pending_after_wave = [task_id for later in sorted(waves) if later > lvl
                               for task_id in waves[later] if task_id not in results]
        _previous_coordination = _coordination_history[-1] if _coordination_history else {}
        _new_completed_result = any(
            task_id not in (_retained_results or {}) and _is_delivered(results.get(task_id))
            for task_id in waves[lvl])
        if (_pending_after_wave
                and any(task_id not in (_retained_results or {}) for task_id in waves[lvl])
                and _coordination_enabled(plan, mode) and not is_cancelled(qhash)
                and not _provider_backoff_tasks(results, dependencies)
                and not any(requires_execution_pause(item.get("result"))
                            for item in _coordination_history)
                and (not _coordination_unavailable(_previous_coordination)
                     or (_previous_coordination.get("error") == "captain_execution_failed"
                         and _new_completed_result))):
            _project_packet = _current_project_packet()
            _coordination_sequence += 1
            proposal = _run_captain_coordination(
                question, plan, team_id,
                {task_id: results.get(task_id) for task_id in waves[lvl] if task_id in results},
                _pending_after_wave, phase=f"wave_{lvl}_complete", sequence=_coordination_sequence,
                cb=cb, user_id=user_id, conversation_id=conversation_id,
                _provider_results=_provider_results)
            if requires_execution_pause(proposal.get("result")) or _coordination_unavailable(proposal):
                _coordination_history.append(proposal)
            if proposal.get("cancelled") or is_cancelled(qhash):
                if not _coordination_history or _coordination_history[-1] is not proposal:
                    _coordination_history.append(proposal)
                return _return_wave_boundary(proposal, _pending_after_wave)
            if proposal.get("ok") and not requires_execution_pause(proposal.get("result")):
                _coordination_history.append(proposal)
                if proposal.get("action") == "continue":
                    current = _snapshot_execution_plan()
                    future, _ = _apply_coordination_to_plan(
                        current, proposal, pending_only=set(_pending_after_wave),
                        valid_harnesses=valid, allow_graph_changes=True)
                    local, _ = _apply_coordination_to_plan(
                        current, proposal, pending_only=set(_pending_after_wave),
                        valid_harnesses=valid, allow_graph_changes=False)
                    policy_changed = any(
                        future["tasks"][key].get("dependency_policy", _DEFAULT_DEP_POLICY)
                        != dependency_policies.get(key, _DEFAULT_DEP_POLICY)
                        for key in _pending_after_wave)
                    if future != local or policy_changed:
                        return _return_wave_boundary(proposal, _pending_after_wave)
                _project_packet = _current_project_packet()
                plan, changed = _apply_coordination_to_plan(
                    plan, proposal, pending_only=set(_pending_after_wave),
                    valid_harnesses=valid, allow_graph_changes=False)
                if changed:
                    raw_tasks = dict(plan.get("tasks") or raw_tasks)
                    # 本轮只允许改未启动节点，不改拓扑；重新绑定让换人/能力要求立刻生效。
                    try:
                        import collaboration_runtime as _coordination_binding
                        plan, _new_decisions = _coordination_binding.bind_plan(
                            plan, valid, runnable=runnable, max_harnesses=_harness_cap)
                        raw_tasks = dict(plan.get("tasks") or raw_tasks)
                        assignment_decisions.extend(_new_decisions)
                        for task_id in _pending_after_wave:
                            node = raw_tasks.get(task_id) if isinstance(raw_tasks.get(task_id), dict) else {}
                            tasks[task_id] = str(node.get("task") or tasks.get(task_id) or "回答问题")
                            task_assignments[task_id] = str(node.get("assigned_harness") or
                                                            task_assignments.get(task_id) or "")
                            dependencies[task_id] = [str(dep) for dep in
                                                     (node.get("depends_on") or dependencies.get(task_id, []))]
                    except Exception:
                        pass
                    if _team_state:
                        try:
                            _team_state.ensure(
                                team_id, question,
                                {task_id: raw_tasks[task_id] for task_id in _pending_after_wave
                                 if task_id in raw_tasks},
                                {task_id: dependencies.get(task_id, []) for task_id in _pending_after_wave},
                                captain=str(plan.get("captain") or ""))
                        except Exception:
                            pass

    # ── 运行中首脑换届:当前首脑可判断另一家更适合并提交 handoff_captain。─────────────
    # 换届不只改标签：接任者拿到前任完整产出/错误/产物和原目标，真执行剩余交付。
    # 每个 dispatch 最多一代换届，避免 A↔B 循环；接任结果不再递归解释 handoff_captain。
    _captain = str(plan.get("captain") or "")
    if (_captain and isinstance(results.get(_captain), dict)
            and _captain not in _provider_backoff_tasks(results, dependencies, dependency_policies)
            and _captain not in (_retained_results or {})):
        _captain_result = results[_captain]
        _cr = (_captain_result.get("orchestration_request") or {}).get("handoff_captain")
        if isinstance(_cr, dict):
            _to = str(_cr.get("to") or "").strip()
            _why = str(_cr.get("reason") or "首脑主动移交")[:500]
            try:
                _runnable_captains = set(_aigc_ready().get("runnable_clis", []))
            except Exception:
                _runnable_captains = set()
            if _to in valid and _to in _runnable_captains and _to != _captain:
                try:
                    _sel = _team_state.select_captain(team_id, _to, reason=_why) if _team_state else None
                except Exception:
                    _sel = None
                plan["captain"] = _to
                if cb:
                    cb({"agent": _to, "t": "captain_handoff", "captain": _to,
                        "previous": _captain, "generation": (_sel or {}).get("generation"),
                        "x": f"{_captain} 将首脑职责移交给 {_to}：{_why}"})
                _prior_text = str(_captain_result.get("final") or _captain_result.get("error") or "")
                _prior_artifacts = list(_captain_result.get("artifacts") or [])
                _remaining = str(_cr.get("task") or _cr.get("next_task") or
                                 "接管本单首脑职责，核对前任成果并完成用户目标的剩余交付与验证")
                _handoff_task = (
                    f"用户原始目标：{question}\n\n你已接任本单首脑（前任：{_captain}）。\n"
                    f"移交原因：{_why}\n剩余任务：{_remaining}\n\n"
                    f"【前任完整产出/错误】\n{_prior_text}\n\n"
                    f"【前任产物引用】\n{json.dumps(_prior_artifacts, ensure_ascii=False)}\n\n"
                    "请读取可用产物继续完成，不要重头做；完成后真验证并交付。")
                if cb:
                    cb({"agent": _to, "t": "task_resumed", "status": "running",
                        "phase": "接任首脑续做", "recoverable": True,
                        "x": f"{_to} 已读取 {_captain} 的现场，开始完成剩余交付"})
                _successor = _run_one_with_gateway(
                    _to, _handoff_task, lease_plan=plan,
                    _provider_results=_provider_results,
                    cancel_check=lambda: _request_cancelled(qhash, _captain, _to),
                    qhash=f"{qhash}_captain_handoff_{_to}",
                    cb=(lambda ev: cb({**ev, "captain": _to, "handoff_from": _captain})) if cb else None,
                    direction=_resolve_direction(plan, _to), user_id=user_id,
                    conversation_id=conversation_id,
                    runtime_brief=_rt_brief,
                    task_facts={"goal": question, "role": _remaining, "team_id": team_id,
                                "task_id": f"captain_handoff_{_to}",
                                "upstream_artifacts": _prior_artifacts,
                                "upstream_results": [{"key": _captain,
                                                      "status": _captain_result.get("status"),
                                                      "error_envelope": _captain_result.get("error_envelope")} ],
                                # 接任首脑也必须继承项目现场(与 relay/revision 同治·
                                # 2026-09-07 盘点补齐:否则换首脑后掉回空目录从零重做)。
                                "project_context": (_project_context.snapshot(team_id, f"captain_handoff_{_to}", packet=_project_packet)
                                                    if _project_context else dict(_project_packet or {}))})

                _successor["captain_handoff_from"] = _captain
                _captain_result["captain_handoff_result"] = _successor
                if _to not in results:
                    results[_to] = _successor
                    keys.append(_to)
                else:
                    results[_to].setdefault("captain_continuations", []).append(_successor)
                if cb:
                    cb({"agent": _to, "t": "task_completed" if _is_delivered(_successor) else "task_failed",
                        "status": "done" if _is_delivered(_successor) else "error",
                        "phase": "接任首脑交付", "captain": _to,
                        "artifact_count": len(_successor.get("artifacts") or []),
                        "x": f"{_to} 接任后{'完成交付' if _is_delivered(_successor) else '仍未交付'}"})

    # ── 成员编排自主权:handoff(用户2026-08-28:harness 能请求"这活该给codex"·不只被动返final)──
    # 学 omnigent Polly(动态委派给更合适的coding agent)+ deepseek agent-team(成员参与编排)。
    # 成员在 result 里【自主】带 orchestration_request.handoff={"to":"codex","reason":"..."} 时,
    # 父层就把这活【转交】给它指定的家(它自己判断谁更合适·不是父层猜)。可选字段·不填=零回归。
    # 守死路:这不是答案融合/投票,是"成员说这活我不对口·转给对口的家"——增加成员对编排的发言权。
    # 防滥用:每单最多 handoff 1 次(转交后即定·不无限转)·目标必须是可跑家·不能转给自己。
    _handoff_done: set = set()
    for k in list(keys):
        if (k in _provider_backoff_tasks(results, dependencies, dependency_policies)
                or k in (_retained_results or {})):
            continue
        # The same explicit peer request uses an independent support node in all entry paths.
        req0 = (results.get(k) or {}).get("orchestration_request") or {}
        if isinstance(req0, dict) and isinstance(req0.get("need_peer"), dict):
            peer_res = _run_requested_peer(
                question, plan, results[k], task_id=k, team_id=team_id,
                user_id=user_id, conversation_id=str(conversation_id or qhash),
                cb=cb, runtime_brief=_rt_brief, project_packet=_project_packet,
                _provider_results=_provider_results, qhash=qhash,
                cancel_check=lambda _k=k: _request_cancelled(qhash, _k, task_assignments.get(_k) or _k))
            if peer_res and peer_res.get("task_id"):
                # Supporting inputs are observable and budgeted, never delivery candidates.
                _peer_id = str(peer_res["task_id"])
                results[_peer_id] = peer_res
                keys.append(_peer_id)
                task_assignments[_peer_id] = str(peer_res.get("harness") or "")
                raw_tasks[_peer_id] = {"task": str(peer_res.get("ask") or ""),
                                      "assigned_harness": task_assignments[_peer_id],
                                      "task_kind": "peer_input", "supporting_context": True}
                tasks[_peer_id] = str(peer_res.get("ask") or "")
                dependencies[_peer_id] = []
                dependency_policies[_peer_id] = _DEFAULT_DEP_POLICY

        # need_approval：成员遇到需用户批准才能做的动作(git push / PR / release / public deploy)时,
        # 先交已能做的部分,末尾写 <<LEGION_ORCH>>{"need_approval":{...}}<<END>>。父层【不替它决定】,
        # 而是真卡住这个动作→给前端推 waiting_approval→等用户 decide→把决定作为新 context 回流让它续跑。
        # 这是"给用户决定权"(context not control):不是父层猜该不该做,是把选择权交给用户+把结果给成员。
        appr_req = req0.get("need_approval") if isinstance(req0, dict) else None
        if (_team_state and isinstance(appr_req, dict)
                and not _request_cancelled(qhash, k, task_assignments.get(k) or k)):
            import approval_state as _appr
            capability = str(appr_req.get("capability") or "unknown_action").strip()[:120]
            appr_reason = str(appr_req.get("reason") or "").strip()[:1000]
            appr_args = appr_req.get("arguments") if isinstance(appr_req.get("arguments"), dict) else {}
            requester_harness = task_assignments.get(k) or k
            reqd = _appr.request(team_id, agent=requester_harness, capability=capability, arguments=appr_args,
                                 task_key=k, reason=appr_reason, ttl_s=3600)
            approval = reqd.get("approval") or {}
            approval_id = approval.get("id") or ""
            if approval_id:
                if cb:
                    cb({"agent": requester_harness, "task_id": k, "t": "approval", "status": "waiting_approval",
                        "x": f"🔐 {requester_harness} 为任务 {k} 请求批准执行「{capability}」·已暂停等你决定",
                        "approval": {"id": approval_id, "capability": capability,
                                     "reason": appr_reason, "team_id": team_id}})
                # 轮询等 decide(TTL 内)。受 is_cancelled 约束:用户喊停整单就不再干等。
                decided = None
                _deadline = time.time() + 3600
                while (time.time() < _deadline
                       and not _request_cancelled(qhash, k, requester_harness)):
                    time.sleep(2)
                    _cur = _appr.list_approvals(team_id, include_terminal=True)
                    _row = next((a for a in (_cur.get("approvals") or [])
                                 if a.get("id") == approval_id), None)
                    if _row and _row.get("status") in ("approved", "denied", "expired"):
                        decided = _row
                        break
                _status = ((decided or {}).get("status") or
                           ("cancelled" if _request_cancelled(qhash, k, requester_harness) else "expired"))
                if cb:
                    _msg = {"approved": "✅ 已获批准", "denied": "🚫 已被拒绝",
                            "expired": "⏳ 审批超时未决", "cancelled": "⏹ 整单已取消"}.get(_status, _status)
                    cb({"agent": requester_harness, "task_id": k, "t": "approval", "status": _status,
                        "x": f"🔐 任务 {k} 的「{capability}」审批结果:{_msg}",
                        "approval": {"id": approval_id, "capability": capability, "decision": _status}})
                # 把审批结果作为新 context 回流给该成员续跑(context not control:告诉它用户的决定,
                # 由它自己决定接下来怎么做——批准就去做,拒绝就走替代路径/说明并交付已能做的部分)。
                if (_status in ("approved", "denied")
                        and not _request_cancelled(qhash, k, requester_harness)):
                    _verdict = ("用户已批准" if _status == "approved" else "用户已拒绝")
                    _denied_reason = str((decided or {}).get("decision_reason") or "").strip()
                    _cont_task = (
                        f"用户问题：{question}\n你上一轮请求批准执行「{capability}」"
                        f"({appr_reason})。{_verdict}这个动作。\n"
                        + ("现在可以真正执行它,然后把最终结果交付给用户。"
                           if _status == "approved"
                           else "请不要执行它;改用不需要该权限的替代方式完成,或明确说明因未获批准而略过,"
                                "并把你已经能交付的部分完整给用户。")
                        # 用户拒绝理由必须原样回流(2026-09-07真跑实锤:deny写的200字重建
                        # 指令只存approvals从不回流→被拒成员只看到"别部署"看不到"该怎么改"
                        # →诚实交付离线包而无重建。context not control:用户理由是最高优先级事实。
                        + (f"\n【用户拒绝时给出的理由·必须逐条回应·按它修正交付而不是绕开它】\n{_denied_reason[:2000]}"
                           if _status == "denied" and _denied_reason else "")
                        + f"\n你负责:{tasks.get(k) or '回答用户问题'}{media_ctx}")
                    _cont_res = _run_one_with_gateway(
                        requester_harness, _cont_task, lease_plan=plan,
                        _provider_results=_provider_results,
                        cancel_check=lambda _k=k, _h=requester_harness: _request_cancelled(qhash, _k, _h),
                        qhash=f"{qhash}_appr_{k}",
                        cb=(lambda ev: cb({**ev, "approval_cont_for": k,
                                          "task_id": k})) if cb else None,
                        direction=_resolve_direction(plan, k, requester_harness), user_id=user_id,
                        conversation_id=conversation_id,
                        runtime_brief=_rt_brief,
                        task_facts={"goal": question, "role": str(tasks.get(k) or "回答用户问题"),
                                    "task_id": k, "team_id": team_id,
                                    "project_context": (_project_context.snapshot(team_id, k, packet=_project_packet)
                                                        if _project_context else dict(_project_packet or {}))})
                    # 续跑结果覆盖该成员交付(批准后真做出来的/拒绝后的替代交付才是最终结果)
                    if isinstance(_cont_res, dict) and (_cont_res.get("final") or _cont_res.get("artifacts")):
                        _cont_res = dict(_cont_res)
                        _cont_res.update({"key": k, "task_id": k, "harness": requester_harness,
                                          "assigned_harness": requester_harness,
                                          "approval_resolved": {"capability": capability,
                                                                 "decision": _status}})
                        results[k] = _cont_res

        # 用户取消(军团自审 P0):整单被喊停就不再启动任何 handoff 转交(那是自动继续跑别家·
        # 违背"用户说停就停")。放循环内每次查·整单取消对全部成员生效。
        if _request_cancelled(qhash, k, task_assignments.get(k) or k):
            continue
        r = results.get(k)
        if not isinstance(r, dict) or k in _handoff_done:
            continue
        req = r.get("orchestration_request") or {}
        ho = req.get("handoff") if isinstance(req, dict) else None
        if not isinstance(ho, dict):
            continue
        to = str(ho.get("to") or "").strip()
        try:
            _runnable = set(_aigc_ready().get("runnable_clis", []))
        except Exception:
            _runnable = set()
        current_harness = task_assignments.get(k) or k
        if to not in valid or to not in _runnable or to == current_harness:
            continue  # 目标不可跑/是当前执行者 → 忽略这个请求(不硬转)
        _handoff_done.add(k)
        _reason = str(ho.get("reason") or "成员请求转交给更合适的家")[:120]
        if cb:
            cb({"agent": "commander", "t": "reason",
                "x": f"🔀 {current_harness} 主动请求把任务 {k} 转交给 {to}({_reason})→ 执行 handoff",
                "handoff": {"task_id": k, "from": current_harness, "to": to, "reason": _reason}})
        _ho_task = (f"用户问题：{question}\n成员 {current_harness} 判断任务 {k} 更适合你,原因:{_reason}\n"
                    f"它给的过渡产出/上下文:{str(r.get('final') or '')[:800]}\n"
                    f"你负责:{tasks.get(k) or '回答用户问题'}{media_ctx}")
        # handoff 换 owner 后新家必须继承项目现场,否则掉回空目录从零重做(与 relay :4725 同治)。
        _ho_facts = {"goal": question, "role": str(tasks.get(k) or "回答用户问题"),
                     "task_id": k, "team_id": team_id,
                     "project_context": (_project_context.snapshot(team_id, k, packet=_project_packet)
                                         if _project_context else dict(_project_packet or {}))}
        _ho_res = _run_one_with_gateway(
            to, _ho_task, lease_plan=plan,
            _provider_results=_provider_results,
            cancel_check=lambda _k=k, _to=to: _request_cancelled(qhash, _k, _to),
            qhash=qhash,
            cb=(lambda ev: cb({**ev, "handoff_for": k})) if cb else None,
            attempt_timeout=None, direction=_resolve_direction(plan, k, to), user_id=user_id,
            conversation_id=conversation_id,
            runtime_brief=_rt_brief, task_facts=_ho_facts)
        if _is_delivered(_ho_res):
            _ho_res = dict(_ho_res)
            _ho_res["handoff_from"] = current_harness
            _ho_res["key"] = k
            _ho_res["task_id"] = k
            _ho_res["harness"] = to
            _ho_res["assigned_harness"] = to
            _ho_res["handoff_by"] = to
            results[k] = _ho_res
            task_assignments[k] = to
            if isinstance(raw_tasks.get(k), dict):
                raw_tasks[k]["assigned_harness"] = to
            try:
                if _team_state:
                    _team_state.update_assignment(team_id, k, to, reason="handoff")
            except Exception:
                pass
            if cb:
                cb({"agent": "commander", "t": "result", "x": f"✅ {to} 接手 {k} 请求转交的活·已交付"})

    # ── 成员编排自主权:fork(成员判断任务太大→自己请求拆子任务并行)──────────────
    # 学 deepseek agent-team spawnTeammate+共享任务DAG、omnigent Polly动态delegate。
    # result.orchestration_request.fork=[{"task":"子任务","suggest_key":"cc"}, ...]
    # 是【成员自主发起】;父层只执行请求(不替它拆)。可选·不填=零回归。每成员一次最多3个是
    # 基础设施并发边界(防一条返回意外炸出无限进程),不是语义门;fork出的子任务不再递归处理
    # orchestration_request(本层只收集结果)→天然一层收敛。子结果挂回父result.fork_results,
    # 不做答案融合/投票(守死路),用户/父成员自己看并继续。
    for k in list(keys):
        # 用户取消:整单喊停就不再启动 fork 子任务(自动继续跑更多进程·违背"用户说停就停")。
        if _request_cancelled(qhash, k, task_assignments.get(k) or k):
            continue
        if (k in _provider_backoff_tasks(results, dependencies, dependency_policies)
                or k in (_retained_results or {})):
            continue
        parent_res_value = results.get(k)
        if not isinstance(parent_res_value, dict):
            continue
        parent_res = parent_res_value
        req = parent_res.get("orchestration_request") or {}
        forks = req.get("fork") if isinstance(req, dict) else None
        if not isinstance(forks, list) or not forks:
            continue
        try:
            _runnable = set(_aigc_ready().get("runnable_clis", []))
        except Exception:
            _runnable = set()
        parent_harness = task_assignments.get(k) or k
        child_specs, used_children = [], {parent_harness}
        _known_fork_ids = set(raw_tasks)
        if _team_state:
            _known_fork_ids.update((_team_state.load(team_id) or {}).get("tasks") or {})
        for idx, spec in enumerate(forks[:3]):
            if not isinstance(spec, dict) or not str(spec.get("task") or "").strip():
                continue
            child_task_id = str(spec.get("task_id") or f"{k}_fork_{idx + 1}")
            if child_task_id in _known_fork_ids:
                parent_res.setdefault("fork_protocol_feedback", []).append({
                    "task_id": child_task_id, "reason": "fork_task_id_already_exists"})
                continue  # An auxiliary request cannot replace a live or saved task.
            suggested = str(spec.get("suggest_key") or "").strip()
            child = (suggested if suggested in valid and suggested in _runnable and suggested not in used_children
                     else _pick_relay_harness(parent_harness, used_children, valid))
            if not child:
                continue
            child_text = str(spec["task"])
            supporting = bool(spec.get("supporting_context")
                              or (plan.get("initial_author") or {}).get("task_id") == k)
            if _team_state:
                try:
                    _team_state.add_task(
                        team_id, child_task_id, child_text, [k], agent=child,
                        metadata={"kind": "fork", "parent_task_id": k,
                                  "supporting_context": supporting,
                                  "reason": str(spec.get("reason") or "")[:120]},
                        registered=valid, exclusive=True)
                except Exception as exc:
                    feedback = {"task_id": child_task_id,
                                "reason": ("fork_task_id_already_exists" if isinstance(exc, FileExistsError)
                                           else "fork_task_registration_failed"),
                                "error_type": type(exc).__name__}
                    parent_res.setdefault("fork_protocol_feedback", []).append(feedback)
                    if _project_context:
                        try:
                            _project_context.record_result(team_id, k, parent_res, assigned_harness=parent_harness)
                        except Exception:
                            pass
                    continue  # Never adopt or replace a task registered by another execution.
            used_children.add(child)
            _known_fork_ids.add(child_task_id)
            child_specs.append((idx, child_task_id, child, child_text,
                                str(spec.get("reason") or "")[:120]))
            tasks[child_task_id] = child_text
            dependencies[child_task_id] = [k]
            dependency_policies[child_task_id] = "all_succeeded"
            task_assignments[child_task_id] = child
            raw_tasks[child_task_id] = {"task": child_text, "task_id": child_task_id,
                                        "assigned_harness": child, "depends_on": [k],
                                        "dependency_policy": "all_succeeded",
                                        "supporting_context": supporting}
            keys.append(child_task_id)
        if not child_specs:
            continue
        if cb:
            cb({"agent": "commander", "t": "dispatch", "task_id": k,
                "x": f"🌿 任务 {k} 动态拆出 {len(child_specs)} 个持久子节点并行：" +
                     ", ".join(task_id for _, task_id, _, _, _ in child_specs),
                "fork": {"parent_task_id": k,
                         "children": [{"task_id": task_id, "harness": child}
                                      for _, task_id, child, _, _ in child_specs]}})
        fork_results: list[dict] = []
        _fork_lock = _results_lock
        _fork_parent_node = ((_team_state.load(team_id) or {}).get("tasks") or {}).get(k) or {} if _team_state else {}

        def _fork_cancelled(child_task_id, child):
            if (_request_cancelled(qhash, k, parent_harness)
                    or _request_cancelled(qhash, child_task_id, child)):
                return True
            if _team_state:
                state = _team_state.load(team_id) or {}
                parent = (state.get("tasks") or {}).get(k) or {}
                node = (state.get("tasks") or {}).get(child_task_id) or {}
                return bool(_team_state._cancel_request_for(state, k, parent)
                            or _team_state._cancel_request_for(state, child_task_id, node))
            return False

        def _fork_worker(idx, child_task_id, child, child_task, reason):
            full = (f"用户问题：{question}\n父任务 {k} 的执行者 {parent_harness} 主动拆给你的子任务：{child_task}\n"
                    f"拆分原因：{reason or '父成员判断需要并行子任务'}\n"
                    f"父成员当前成果/上下文：{str(parent_res.get('final') or '')[:1200]}{media_ctx}")
            # A waiter must observe cancellation and the same request budget before claim.
            child_attempt = None
            child_project_context = None
            stopped = None
            try:
                if _fork_cancelled(child_task_id, child):
                    stopped = {"status": "cancelled", "final": "", "error": "用户停止",
                               "cancelled": True, "blocked_before_native": True, "execution_calls": 0}
                    if _team_state:
                        _team_state.cancel_queued(team_id, child_task_id, "父任务或子任务已取消")
                else:
                    stopped = _request_budget_pause(_provider_results)
                    if stopped and _team_state:
                        _team_state.mark_task_blocked(
                            team_id, child_task_id, str(stopped.get("error") or "request budget exhausted"),
                            facts={"error_envelope": deepcopy(stopped.get("error_envelope") or {})})
                if stopped:
                    stopped.update({"key": child_task_id, "task_id": child_task_id,
                                    "harness": child, "assigned_harness": child,
                                    "fork_parent": k, "fork_index": idx,
                                    "supporting_context": bool(raw_tasks[child_task_id].get("supporting_context"))})
                    with _fork_lock:
                        results[child_task_id] = stopped
                        fork_results.append(stopped)
                    return
                if not _team_state or not _project_context:
                    raise LookupError("fork_project_context_unavailable")
                if _team_state:
                    def check_fork_claim(state):
                        nonlocal child_project_context
                        _require_mail_context_owner(state, team_id, user_id,
                                                   conversation_id or qhash, bound_team=True)
                        parent = (state.get("tasks") or {}).get(k) or {}
                        node = (state.get("tasks") or {}).get(child_task_id) or {}
                        if (_team_state._cancel_request_for(state, k, parent)
                                or parent.get("assigned_harness") != parent_harness
                                or parent.get("attempt_seq") != _fork_parent_node.get("attempt_seq")
                                or not parent.get("finished_at") or parent.get("active_attempt") is not None
                                or node.get("assigned_harness") != child
                                or (node.get("metadata") or {}).get("kind") != "fork"
                                or (node.get("metadata") or {}).get("parent_task_id") != k
                                or node.get("status") != "pending" or node.get("attempt_seq") != 0):
                            raise PermissionError("fork_task_binding_changed")
                        # The exclusive TeamState child must also be bound in
                        # the canonical project before native execution/export.
                        # Merge only this new task, never replay the old packet.
                        packet = _project_context.load(team_id)
                        _require_mail_context_owner(packet, team_id, user_id,
                                                   conversation_id or qhash, bound_team=True)
                        if any(row.get("task_id") == child_task_id
                               for row in packet.get("tasks") or [] if isinstance(row, dict)):
                            raise PermissionError("fork_project_task_already_exists")
                        packet = _project_context.record_task_bindings(
                            team_id, {child_task_id: raw_tasks[child_task_id]},
                            user_id=user_id, conversation_id=str(conversation_id or qhash),
                            expected_assigned_harnesses={k: parent_harness})
                        child_project_context = _project_context.snapshot(
                            team_id, child_task_id, packet=packet)
                    child_attempt = _team_state.begin_attempt(
                        team_id, child_task_id, owner=f"server:{os.getpid()}", owner_check=check_fork_claim)
            except Exception as exc:
                with _fork_lock:
                    row = {
                        "key": child_task_id, "task_id": child_task_id, "harness": child,
                        "assigned_harness": child, "fork_parent": k, "fork_index": idx,
                        "status": "blocked", "final": "", "error": type(exc).__name__,
                        "blocked_before_native": True, "execution_calls": 0,
                        "supporting_context": bool(raw_tasks[child_task_id].get("supporting_context")),
                        "fork_execution_observation": "task_claim_rejected; native execution not started"}
                    results[child_task_id] = row
                    fork_results.append(row)
                return
            def fork_progress(event):
                if _team_state and _event_advances_attempt(event):
                    _team_state.renew_attempt(team_id, child_task_id, child_attempt)
                if cb:
                    cb({**event, "fork_parent": k, "task_id": child_task_id})
            try:
                import model_registry as _fork_models
                child_binding = _fork_models.resolve_assignments(plan, [child]).get(child) or {}
            except Exception:
                child_binding = {}
            # 子任务继承原工程和请求范围，并使用独立的原生任务会话。
            try:
                cr = _run_one_with_gateway(
                    child, full, lease_plan=plan,
                    _provider_results=_provider_results,
                    cancel_check=lambda _id=child_task_id, _child=child: _fork_cancelled(_id, _child),
                    qhash=f"{qhash}_fork_{child_task_id}",
                    cb=fork_progress,
                    preferred_model=child_binding.get("model"), preferred_appid=child_binding.get("appid"),
                    attempt_timeout=None, direction=_resolve_direction(plan, child_task_id, child), user_id=user_id,
                    conversation_id=f"{conversation_id or qhash}:task:{child_task_id}",
                    runtime_brief=_rt_brief, capability_policy=dict(plan.get("capability_policy") or {}),
                    task_facts={"goal": question, "role": child_task[:200] or "子任务",
                                "task_id": child_task_id, "team_id": team_id, "fork_parent": k,
                                "attempt_id": child_attempt,
                                **_native_scope_facts(plan, raw_tasks[child_task_id], request_id=team_id),
                                "project_context": child_project_context})
                cr = dict(cr)
            except Exception as exc:
                cr = {"status": "error", "final": "", "error": type(exc).__name__,
                      "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                         "retryable": False, "code": "fork_execution_exception",
                                         "message": type(exc).__name__},
                      "fork_execution_observation": "native invocation raised; original author retained"}
            cr.update({"key": child_task_id, "task_id": child_task_id, "harness": child,
                       "assigned_harness": child, "fork_parent": k, "fork_index": idx,
                       "requested_task": child_task,
                       "supporting_context": bool(raw_tasks[child_task_id].get("supporting_context"))})
            if cr["supporting_context"]:
                cr["peer_for"] = k
            if _team_state and not _team_state.finish_attempt(team_id, child_task_id, child_attempt, cr):
                cr.update(status="blocked", final="", fork_execution_observation="late_attempt_not_accepted")
            # Publish typed results before the next bounded fork acquires its slot.
            with _fork_lock:
                results[child_task_id] = cr
                fork_results.append(cr)

        try:
            _fork_parallel = max(1, min(3, int((plan.get("team_policy") or {}).get("max_parallel", 3))))
        except (TypeError, ValueError):
            _fork_parallel = 1
        _fork_slots = threading.BoundedSemaphore(_fork_parallel)
        def _fork_slot_worker(*args):
            with _fork_slots:
                _fork_worker(*args)
        fthreads = []
        for spec in child_specs:
            th = threading.Thread(target=_fork_slot_worker, args=spec, daemon=True)
            th.start(); fthreads.append(th)
        for th in fthreads:
            th.join()
        fork_results.sort(key=lambda x: int(x.get("fork_index", 0)))
        parent_res["fork_results"] = fork_results
        parent_res["fork_count"] = len(fork_results)
        for child_result in fork_results:
            child_id = str(child_result.get("task_id"))
            results[child_id] = child_result
            if _team_state and _project_context and not child_result.get("blocked_before_native"):
                try:
                    def check_fork_publication(state):
                        _require_mail_context_owner(state, team_id, user_id,
                                                   conversation_id or qhash, bound_team=True)
                        parent = (state.get("tasks") or {}).get(k) or {}
                        saved_child = (state.get("tasks") or {}).get(child_id) or {}
                        if (parent.get("assigned_harness") != parent_harness
                                or parent.get("attempt_seq") != _fork_parent_node.get("attempt_seq")
                                or parent.get("active_attempt") is not None
                                or not parent.get("finished_at")
                                or not saved_child.get("finished_at")
                                or saved_child.get("active_attempt") is not None
                                or _request_cancelled(qhash, k, parent_harness)):
                            raise PermissionError("fork_publication_attempt_changed")
                    _project_context.record_result(
                        team_id, child_id, child_result,
                        assigned_harness=str(child_result.get("assigned_harness") or ""))
                    message = _team_state.send_message(
                        team_id, str(child_result.get("assigned_harness") or ""), k,
                        str(child_result.get("final") or child_result.get("error") or ""),
                        artifact_refs=list(child_result.get("artifacts") or []),
                        idempotency_key=child_id, owner_check=check_fork_publication)
                    child_result["mailbox_ref"] = {
                        "team_id": team_id, "recipient": k, "message_id": message.get("id")}
                    _team_state.replace_result(
                        team_id, k, parent_res, previous_harness=parent_harness,
                        reason="fork_inputs_available", owner_check=check_fork_publication)
                except Exception as exc:
                    parent_res.setdefault("fork_protocol_feedback", []).append({
                        "task_id": child_id, "reason": "fork_publication_failed",
                        "error_type": type(exc).__name__})
        if cb:
            done_n = sum(1 for x in fork_results if _is_delivered(x))
            cb({"agent": "commander", "t": "result",
                "x": f"🌿 {k} 的子任务完成 {done_n}/{len(fork_results)}·结果已挂回父成员",
                "fork": {"parent": k, "done": done_n, "total": len(fork_results)}})


    # 末尾兜底 failover(等价旧行为·处理逐 wave 没覆盖到的·如无依赖平行失败节点)。逐 wave failover
    # 已在 wave 循环内做(见上方 for lvl 循环)·此处对全部剩余失败节点再扫一遍(已交付的会被跳过)。
    _do_failover(list(keys))

    ordered = [results[k] for k in keys if k in results]
    _verification_reports = [
        {"key": r.get("assigned_harness") or r.get("harness") or r.get("key"),
         "task_id": r.get("task_id") or r.get("key"), "role": r.get("verification_role"),
         "targets": r.get("verification_targets") or [],
         "report_id": str((r.get("verification_report") or {}).get("report_id") or
                          f"verification:{r.get('task_id') or r.get('key')}"),
         "assessed_candidate_id": str((r.get("verification_targets") or [""])[0] or ""),
         **(r.get("verification_report") or {})}
        for r in ordered if r.get("verification_role")
    ]
    _verification_nodes = {str(report.get("task_id") or "") for report in _verification_reports}
    _supporting_nodes = {str(key) for key, node in raw_tasks.items()
                         if isinstance(node, dict) and node.get("supporting_context")}
    _candidate_results = [r for r in ordered
                          if str(r.get("key") or "") not in _verification_nodes | _supporting_nodes]
    if mode == "relay":
        _delivered_results = [r for r in _candidate_results if _is_delivered(r)]
        _tail = _candidate_results[-1] if _candidate_results else None
        _final_result = (_tail if _is_delivered(_tail)
                         else (_delivered_results[-1] if _delivered_results else _tail))
    else:
        _final_result = None
    _pre_revision_results = list(_candidate_results)
    # 被独立验证者明确要求 revise/fail 时，把反对意见保留为下一轮可复用的程序化经验；
    # 不覆盖候选正文、不做投票，也不由父层擅自改答案。
    _revision_results = []
    _pitfall_ids: list[str] = []
    for _report in _verification_reports:
        # 验证节点结束时已在 worker 接受 attempt 后即时写入共享 Context；此处只消费，
        # 不再二次 append。同一 report_id 的幂等 upsert 仍在 ProjectContext 兜底。
        if _report.get("verdict") not in {"revise", "fail"}:
            continue
        try:
            import pitfall_registry as _pitfalls
            for _finding in _report.get("findings") or [str(_report.get("revision_request") or "verification failure")]:
                _stored = _pitfalls.record(
                    category=str(_report.get("role") or "verification"), finding=str(_finding),
                    evidence=list(_report.get("evidence") or []), source=str(_report.get("key") or "verification"),
                    task_id=str((_report.get("targets") or [""])[0]),
                    candidate_id=str((_report.get("targets") or [""])[0]),
                    revision_request=str(_report.get("revision_request") or ""))
                if (_stored.get("pitfall") or {}).get("id"):
                    _pitfall_ids.append(_stored["pitfall"]["id"])
        except Exception:
            pass
        try:
            import legion_memory as _verification_memory
            _verification_memory.add(
                question,
                json.dumps({"verification_role": _report.get("role"),
                            "findings": _report.get("findings") or [],
                            "evidence": _report.get("evidence") or [],
                            "revision_request": _report.get("revision_request") or ""},
                           ensure_ascii=False),
                source=str(_report.get("key") or "verification"),
                tags=["verification", "pitfall", str(_report.get("role") or "critic")],
                mtype="procedural", user_id=user_id)
        except Exception:
            pass
    # Teamwork 式“失败综合后再跑”：只有验证者明确给出 revision_request 时才续一轮，
    # 修订者看到候选、反对意见和证据；父层不替它写修法，也不投票。
    if orchestration_spec and orchestration_spec.verification.max_rounds > 0 and not is_cancelled(qhash):
        _active_objections = [report for report in _verification_reports
                              if report.get("verdict") in {"revise", "fail"}
                              and str(report.get("revision_request") or "").strip()]
        _original_revision_owner = str(
            orchestration_spec.verification.revision_owner or plan.get("captain") or "")
        _revision_owner, _revision_owner_reason = _select_revision_owner(
            _original_revision_owner, "", _candidate_results, _active_objections, valid)
        if _revision_owner_reason == "verifier_replace_owner" and cb:
            _failed_owner = next((str(row.get("assigned_harness") or row.get("harness") or "")
                                  for row in _candidate_results), _original_revision_owner)
            cb({"agent": "legion", "t": "owner_replaced", "status": "running",
                "from_owner": _failed_owner, "to_owner": _revision_owner,
                "x": f"独立审计确认 owner 能力不匹配，已从 {_failed_owner} 换为 {_revision_owner}"})
        for _round in range(orchestration_spec.verification.max_rounds):
            if not _active_objections or not _revision_owner:
                break
            _revision_attempt = None
            _revision_id = f"revision_{_round + 1}_{_revision_owner}"
            _revision_dependencies = list(dict.fromkeys(
                str(target) for report in _active_objections
                for target in (report.get("targets") or []) if str(target)))
            _quality_facts = {
                "north_star": plan.get("north_star") or {},
                "critical_dimensions": plan.get("critical_dimensions") or [],
                "claim_ceiling": plan.get("claim_ceiling") or "UNVALIDATED",
                "captain_thesis": plan.get("captain_thesis") or {},
                "quality_context": plan.get("quality_context") or {},
                "gap_plan": plan.get("gap_plan") or [],
            }
            _revision_task = (
                f"用户原始目标：{question}\n"
                "以下为本轮修订的质量上下文，包含用户要求、已有记录及前任／模型的目标、推断和策略，并非均已验证。"
                "请保留用户明示要求与原始来源；其余判断按原证据状态理解，可据新证据修正：\n"
                f"{json.dumps(_quality_facts, ensure_ascii=False)}\n"
                f"以下是独立验证者对现有候选的反对意见与证据：\n"
                f"{json.dumps(_active_objections, ensure_ascii=False)}\n"
                "请先判断问题是局部缺陷、方向错误还是 owner 能力不匹配。局部缺陷才在原方案上修；若证据支持"
                "replace_direction 或 replace_owner，应明确提交新 hypothesis 或 handoff 请求，不要继续给弱方向打补丁。"
                "修订后分别报告 runtime、flow、outcome、frontier 事实，并让声明强度不超过证据。"
            )
            try:
                if _team_state:
                    _team_state.add_task(team_id, _revision_id, _revision_task,
                                         _revision_dependencies,
                                         agent=_revision_owner,
                                         metadata={"kind": "revision", "round": _round + 1})
                if _delivery_milestone:
                    _delivery_milestone.observe_context(
                        _project_packet, include_gaps=True, include_journey=False)
                    _delivery_milestone.revision(
                        started=True, owner=_revision_owner, round_number=_round + 1)
                if cb:
                    cb({"agent": _revision_owner, "t": "task_added", "phase": "根据反对意见修订",
                        "task_id": _revision_id, "x": f"验证发现缺陷，{_revision_owner} 开始第 {_round + 1} 轮修订"})
                _revision_attempt = (_team_state.begin_attempt(
                    team_id, _revision_id, owner=f"server:{os.getpid()}") if _team_state else None)
                _revised = _run_one_with_gateway(
                    _revision_owner, _revision_task, lease_plan=plan,
                    _provider_results=_provider_results,
                    cancel_check=lambda _id=_revision_id, _owner=_revision_owner:
                        _request_cancelled(qhash, _id, _owner),
                    qhash=f"{qhash}_{_revision_id}",
                    cb=(lambda ev: cb({**ev, "revision_round": _round + 1})) if cb else None,
                    direction=_resolve_direction(plan, _revision_dependencies[0] if _revision_dependencies else "",
                                                 _revision_owner), user_id=user_id,
                    conversation_id=conversation_id, runtime_brief=_rt_brief,
                    task_facts={"goal": question, "role": "revision", "team_id": team_id,
                                "task_id": _revision_id,
                                "delivery_contract": dict(plan.get("delivery_contract") or {}),
                                "orchestration": orchestration_spec.to_dict(),
                                "upstream_results": _active_objections,
                                # 修订家必须继承项目现场,否则换 owner 掉回空目录重做(与 relay :4725 同治)。
                                "project_context": (_project_context.snapshot(team_id, _revision_id, packet=_project_packet)
                                                    if _project_context else dict(_project_packet or {}))})
                _revised = dict(_revised)
                _revised.update({"key": _revision_id, "task_id": _revision_id,
                                 "harness": _revision_owner, "assigned_harness": _revision_owner,
                                 "revision_round": _round + 1,
                                 "revision_of": [target for report in _active_objections
                                                 for target in (report.get("targets") or [])]})
                _revision_results.append(_revised)
                if _delivery_milestone and _is_delivered(_revised):
                    _delivery_milestone.revision(
                        started=False, owner=_revision_owner, round_number=_round + 1,
                        artifact_count=len(_revised.get("artifacts") or []))
                if _team_state and _revision_attempt is not None:
                    _team_state.finish_attempt(team_id, _revision_id, _revision_attempt, _revised)
                if _project_context:
                    _project_packet.clear()
                    _project_packet.update(_project_context.record_result(
                        team_id, _revision_id, _revised, assigned_harness=_revision_owner))
                if _is_delivered(_revised):
                    break
            except Exception as exc:
                _revision_failure = {"key": _revision_id, "task_id": _revision_id,
                                     "harness": _revision_owner, "assigned_harness": _revision_owner,
                                     "status": "error", "final": "",
                                     "error": f"revision:{type(exc).__name__}:{exc}"}
                _revision_results.append(_revision_failure)
                if _team_state and locals().get("_revision_attempt") is not None:
                    try:
                        _team_state.finish_attempt(
                            team_id, _revision_id, _revision_attempt, _revision_failure)
                    except Exception:
                        pass
                if _project_context:
                    try:
                        _project_packet.clear()
                        _project_packet.update(_project_context.record_result(
                            team_id, _revision_id, _revision_failure,
                            assigned_harness=_revision_owner))
                    except Exception:
                        pass
                break
        ordered.extend(_revision_results)
        _candidate_results.extend(_revision_results)
    # 修订完成不能自封通过：原验证者若与修订者不同，读取修订成品独立复验；否则另选可运行成员。
    _post_revision_reports = []
    if _revision_results and orchestration_spec and orchestration_spec.verification.require_independent:
        for _index, _revised in enumerate(_revision_results, 1):
            if not _is_delivered(_revised):
                continue
            _audit_attempt = None
            _reviser = str(_revised.get("assigned_harness") or _revised.get("harness") or "")
            _reviewer = next((str(report.get("key") or "") for report in _verification_reports
                              if str(report.get("key") or "") in valid
                              and str(report.get("key") or "") != _reviser), "")
            if not _reviewer:
                _reviewer = _pick_relay_harness(_reviser, {_reviser}, valid) or ""
            if not _reviewer:
                continue
            _audit_id = f"audit_revision_{_index}_{_reviewer}"
            _audit_task = (
                f"用户原始目标：{question}\n以下是修订后的候选：\n{str(_revised.get('final') or '')}\n"
                f"产物：{json.dumps(_revised.get('artifacts') or [], ensure_ascii=False)}\n"
                f"验收标准：{json.dumps(orchestration_spec.verification.criteria, ensure_ascii=False)}\n"
                "请独立复验修订是否真正解决原 objection，先给人可读结论。"
                "末尾只追加一个下列协议块；findings/evidence 使用字符串数组。"
                "具体判断由你根据证据决定，竖线表示可选值，须选择其中一项而非照抄枚举串：\n"
                '<<LEGION_VERIFY>>{"verdict":"pass|revise|fail|blocked","findings":[],"evidence":[],'
                '"revision_request":"","recommended_action":"keep_owner|replace_owner|replace_direction|inconclusive",'
                '"replacement_requirements":[]}<<END>>')
            if _team_state:
                try:
                    _team_state.add_task(team_id, _audit_id, _audit_task,
                                         [str(_revised.get("task_id") or "")], agent=_reviewer,
                                         metadata={"kind": "post_revision_audit"})
                except Exception:
                    pass
            try:
                _audit_attempt = (_team_state.begin_attempt(
                    team_id, _audit_id, owner=f"server:{os.getpid()}") if _team_state else None)
                _audited = _run_one_with_gateway(
                    _reviewer, _audit_task, lease_plan=plan,
                    _provider_results=_provider_results,
                    attempt_timeout=_VERIFY_TIMEOUT,  # 复验同硬超时:不阻塞 final 交付
                    cancel_check=lambda _id=_audit_id, _reviewer=_reviewer:
                        _request_cancelled(qhash, _id, _reviewer),
                    qhash=f"{qhash}_{_audit_id}",
                    cb=(lambda ev: cb({**ev, "task_id": _audit_id,
                                      "post_revision_audit": True})) if cb else None,
                    direction="programming", user_id=user_id,
                    # This registered audit is its own task, not the parent's
                    # native conversation. Keep retries in its stable lane.
                    conversation_id=f"{conversation_id or qhash}:task:{_audit_id}",
                    runtime_brief=_rt_brief,
                    task_facts={"goal": question, "role": "auditor",
                                "team_id": team_id, "task_id": _audit_id,
                                "project_context": (
                                    _project_context.snapshot(team_id, _audit_id, packet=_project_packet)
                                    if _project_context else dict(_project_packet or {}))})
                from legion_contracts import parse_verification_report as _parse_verify
                _clean, _parsed = _parse_verify(str(_audited.get("final") or ""))
                _superseded_ids = [str(report.get("report_id") or "") for report in _verification_reports
                                   if report.get("verdict") in {"revise", "fail"}
                                   and set(str(value) for value in report.get("targets") or []) &
                                       set(str(value) for value in _revised.get("revision_of") or [])]
                _report = {"key": _reviewer, "role": "auditor",
                           "targets": [str(_revised.get("task_id") or "")],
                           "report_id": f"post_revision:{_audit_id}",
                           "assessed_candidate_id": str(_revised.get("task_id") or ""),
                           "supersedes_report_ids": [value for value in _superseded_ids if value],
                           **(_parsed or {"verdict": "unverified", "findings": [], "evidence": [],
                                          "revision_request": "独立复验未返回结构化报告"})}
                _post_revision_reports.append(_report)
                _audited.update({"key": _audit_id, "task_id": _audit_id, "harness": _reviewer,
                                 "assigned_harness": _reviewer, "final": _clean,
                                 "verification_role": "auditor", "verification_report": _report})
                if _team_state and _audit_attempt is not None:
                    _team_state.finish_attempt(team_id, _audit_id, _audit_attempt, _audited)
                if _project_context:
                    _project_packet.clear()
                    _project_packet.update(_project_context.record_result(
                        team_id, _audit_id, _audited, assigned_harness=_reviewer))
                    _project_packet.clear()
                    _project_packet.update(_project_context.record_verification(team_id, _report))
                ordered.append(_audited)
                if _report.get("verdict") == "pass":
                    try:
                        import pitfall_registry as _pitfalls
                        for _pitfall_id in _pitfall_ids:
                            _pitfalls.resolve(_pitfall_id, revision_task_id=str(_revised.get("task_id") or ""),
                                              evidence=list(_report.get("evidence") or []))
                    except Exception:
                        pass
            except Exception as _audit_exc:
                _audit_failure = {"key": _audit_id, "task_id": _audit_id,
                                  "harness": _reviewer, "assigned_harness": _reviewer,
                                  "status": "error", "final": "", "error": str(_audit_exc),
                                  "verification_role": "auditor"}
                if _team_state and locals().get("_audit_attempt") is not None:
                    try:
                        _team_state.finish_attempt(team_id, _audit_id, _audit_attempt, _audit_failure)
                    except Exception:
                        pass
                _failure_report = {"key": _reviewer, "role": "auditor",
                                   "targets": [str(_revised.get("task_id") or "")],
                                   "report_id": f"post_revision:{_audit_id}",
                                   "assessed_candidate_id": str(_revised.get("task_id") or ""),
                                   "verdict": "unverified", "findings": [str(_audit_exc)],
                                   "evidence": [], "revision_request": "复验执行失败"}
                _post_revision_reports.append(_failure_report)
                if _project_context:
                    try:
                        _project_packet.clear()
                        _project_packet.update(_project_context.record_result(
                            team_id, _audit_id, _audit_failure, assigned_harness=_reviewer))
                        _project_packet.clear()
                        _project_packet.update(_project_context.record_verification(team_id, _failure_report))
                    except Exception:
                        pass
        if _post_revision_reports:
            _verification_reports.extend(_post_revision_reports)
    # 最终 Success Auditor 必须放在交付契约补救之后，审计的是最终成品，不是修订前版本。
    _success_audit = {"enabled": False, "verdict": "not_required", "profile": "answer",
                      "winner": "", "scores": {}, "reason": "简单任务无需额外模型审计"}
    _frontier_review_reports: list[dict] = []
    _usage_overhead_results: list[dict] = []
    _verified_quality = None
    # 运行资格账本回写(S3 根因④收口·2026-09-02):每次真单跑完把成员级战绩写进
    # harness_performance·下一次 plan_request_with_model 的 roster 就能读到该家真实
    # readiness(declared→verified→experienced)。之前账本只有体检脚本手写·运行时
    # 真单不积累→规划器永远只见声明画像。scene 用 plan 方向(没有就 general)·mode 用
    # 真实执行模式。整体兜底:回写失败绝不影响交付(上游断链铁律)。
    try:
        import harness_performance as _perf
        _scene = str((plan.get("direction") if isinstance(plan, dict) else "") or "general")
        _mode = str(mode or "single")
        for r in ordered:
            # 补位交付的战绩归【真实执行家】(r.relay_by)·不归原节点——hermes 挂了由 codex
            # 接力成功,记在 hermes 名下会把"它其实首轮失败"洗成好战绩,资格账本失真。
            _actual = str(r.get("relay_by") or r.get("assigned_harness") or
                          r.get("harness") or r.get("key") or "")
            if not _actual:
                continue
            _perf.record(
                _actual, _scene, _mode,
                ok=_is_delivered(r),
                wall_s=float(r.get("elapsed") or 0),
                artifacts=int(len(r.get("artifacts") or []) + len(r.get("discovered_artifacts") or [])),
                source="runtime")
            if r.get("relay_by"):   # 原节点如实记一笔失败(它自己确实没交付)
                _perf.record(str(r.get("key") or ""), _scene, _mode, ok=False,
                             wall_s=float(r.get("elapsed") or 0), source="runtime_relayed")
    except Exception:
        pass
    delivered = sum(1 for r in _candidate_results if _is_delivered(r))
    timed_out = sum(1 for r in _candidate_results if r.get("status") == "timeout")
    cancelled_count = sum(1 for r in _candidate_results if r.get("status") == "cancelled")
    whole_cancelled = is_cancelled(qhash)
    _outcome = _typed_outcome(_candidate_results, whole_cancelled,
                              dict(plan.get("delivery_contract") or {}))
    # 成员给了文字 final 但显式文件契约没满足时，普通 failover 不会触发（status=done）。这里按
    # 【契约事实】追加一轮补救：把缺失项和已有候选交给另一个当前可运行成员。父层不替它写文件，
    # 也不把“有文字”当成功；补救结果仍须重新过同一 delivery contract。
    try:
        _contract_repair_rounds = max(0, int(_runtime_team_policy.get(
            "contract_repair_rounds", os.environ.get("LEGION_CONTRACT_REPAIR_ROUNDS", "1"))))
    except (TypeError, ValueError):
        _contract_repair_rounds = 1
    # Missing native prose is a runtime observation, not a failed file contract.
    # Exclude only that branch; independent candidates still use ordinary repair.
    from error_envelope import paused_execution_tasks
    _terminal_message_branches = paused_execution_tasks(
        results, dependencies, dependency_policies,
        pause=lambda row: bool(_native_terminal_message_absent(row)),
        delivered=_is_delivered, dependency_check=_deps_satisfied)
    _repair_sources = [
        row for row in _candidate_results
        if str(row.get("task_id") or row.get("key") or "") not in _terminal_message_branches
        and not _native_terminal_message_absent(row)]
    if (_outcome.get("contract_incomplete") and not is_cancelled(qhash)
            and _contract_repair_rounds > 0 and _repair_sources
            and not _provider_backoff_tasks(results, dependencies)
            and not any(requires_execution_pause(item.get("result")) for item in _coordination_history)):
        _failed_result = _repair_sources[-1]
        _failed_owner = str(_failed_result.get("assigned_harness")
                            or _failed_result.get("harness") or _failed_result.get("key") or "")
        _repair_owner = _pick_relay_harness(_failed_owner, {_failed_owner}, valid)
        if _repair_owner:
            _missing = (_outcome.get("delivery_contract") or {}).get("missing") or []
            _existing = [a for row in _candidate_results for a in
                         [*(row.get("artifacts") or []), *(row.get("discovered_artifacts") or [])]]
            # 换 owner 继承项目现场(2026-09-07实锤·铁律第四处漏网):contract_repair 之前
            # 只带本单产物与用户孤句。会话续单时(用户"在原项目上迭代")本单可能因全灭而无
            # 任何产物·兜底家看不到原项目文件在哪只能从零重做(把迭代请求做成孤立图片)。
            # 修:带上 plan.conversation_context(server 已注入含上轮产物路径)与 project_context。
            _history_ctx = str(plan.get("conversation_context") or "").strip()
            # 质量事实回流(2026-09-07真跑实锤:修复轮只看到"缺什么文件类型"看不到"为什么不行"
            # →把同一个伪3D重新打包了事)。修复者必须看见:验证者反对意见/翻译官SPEC产物/
            # 用户审批拒绝理由——它修的是【质量】不只是【文件类型清单】。纯事实回流·零control。
            _quality_repair_ctx = ""
            try:
                _vr = [{_f: _rep.get(_f) for _f in ("role", "task_id", "verdict", "findings",
                                                   "evidence", "revision_request", "targets")}
                       for _rep in _verification_reports
                       if _rep.get("verdict") in ("revise", "fail")]
                if _vr:
                    _quality_repair_ctx += ("\n【独立验证者的反对意见与证据·修复必须逐条回应】\n"
                                            + json.dumps(_vr, ensure_ascii=False)[:8000])
            except Exception:
                pass
            try:
                _spec_arts = []
                for _tk, _tv in raw_tasks.items():
                    if (isinstance(_tv, dict)
                            and str(_tv.get("task_kind") or "") in
                            ("frontier_research", "translate_research", "direction_review",
                             "critical_sample")):
                        _tr = results.get(_tk) or {}
                        _spec_arts.extend([*(_tr.get("artifacts") or []),
                                           *(_tr.get("discovered_artifacts") or [])])
                if _spec_arts:
                    _quality_repair_ctx += ("\n【本单质量规格与研究产物·先读再修·不得降档】\n"
                                            + json.dumps(_spec_arts, ensure_ascii=False)[:8000])
            except Exception:
                pass
            try:
                import approval_state as _appr_mod
                _rows = (_appr_mod.list_approvals(team_id, include_terminal=True)
                         .get("approvals") or [])
                _denied = [{"capability": _a.get("capability"),
                            "decision_reason": _a.get("decision_reason")}
                           for _a in _rows
                           if _a.get("status") == "denied" and _a.get("decision_reason")]
                if _denied:
                    _quality_repair_ctx += ("\n【用户审批拒绝理由·逐条回应·按它修正而不是绕开】\n"
                                            + json.dumps(_denied, ensure_ascii=False)[:4000])
            except Exception:
                pass
            _repair_task = (
                f"用户原始目标：{question}\n"
                + (f"\n【同一会话上一轮的现场·原项目文件已存在·可直接读取/修改·在它基础上迭代·不要从零重做】\n{_history_ctx[:8000]}\n" if _history_ctx else "")
                + _quality_repair_ctx
                + f"\n上一位成员给了回答，但机器验收发现显式交付契约仍缺："
                f"{json.dumps(_missing, ensure_ascii=False)[:8000]}\n"
                f"已有候选与产物引用：{json.dumps(_existing, ensure_ascii=False)[:8000]}\n"
                "请直接完成缺失交付并自行打开/运行验证；已有合格部分可复用。不要只描述计划，"
                "最终必须返回真实文件路径。具体工具与做法由你自己决定。")
            if cb:
                cb({"agent": "commander", "t": "reason", "phase": "补齐交付契约",
                    "x": f"文字回答已到，但文件契约未满足 → {_repair_owner} 补齐并真验"})
            try:
                _repair = _run_one_with_gateway(
                    _repair_owner, _repair_task, lease_plan=plan,
                    _provider_results=_provider_results,
                    cancel_check=lambda _owner=_repair_owner: _request_cancelled(qhash, "contract_repair", _owner),
                    qhash=f"{qhash}_contract_repair_1",
                    cb=(lambda ev: cb({**ev, "contract_repair": True})) if cb else None,
                    direction=_resolve_direction(plan, _failed_owner or _repair_owner),
                    user_id=user_id,
                    conversation_id=f"{conversation_id or qhash}:contract-repair:{_repair_owner}",
                    runtime_brief=_rt_brief,
                    task_facts={"goal": question, "role": "contract_repair", "team_id": team_id,
                                "task_id": "contract_repair",
                                "delivery_contract": dict(plan.get("delivery_contract") or {}),
                                "upstream_artifacts": _existing,
                                "upstream_results": [
                                    {name: row[name] for name in
                                     ("key", "task_id", "harness", "status", "error",
                                      "error_envelope", "native_facts", "native_checkpoint", "workdir")
                                     if name in row}
                                    for row in ordered if isinstance(row, dict)],
                                "project_context": dict(plan.get("project_context") or {})})
                _repair["contract_repair"] = True
                _repair["repair_for"] = _failed_owner
                _candidate_results.append(_repair)
                ordered.append(_repair)
                _outcome = _typed_outcome(_candidate_results, whole_cancelled,
                                          dict(plan.get("delivery_contract") or {}))
            except Exception as exc:
                _candidate_results.append({"key": _repair_owner, "status": "error", "final": "",
                                           "error": f"contract_repair:{type(exc).__name__}:{exc}"})
                _outcome = _typed_outcome(_candidate_results, whole_cancelled,
                                          dict(plan.get("delivery_contract") or {}))
    # Success Auditor 审的是契约补救后的最终成果。简单任务不启用；复杂任务只追加一次独立审计，
    # 审计不再触发修订循环，避免“为了质量无限加模型”的协调税。
    try:
        import success_auditor as _success
        _audit_candidates = [row for row in _candidate_results if _is_delivered(row)]
        _audit_enabled, _audit_profile = _success.should_run(plan, _audit_candidates)
        _success_audit["profile"] = _audit_profile
        _frontier_contract = _success.frontier_contract(plan) if _audit_enabled and _audit_profile == "frontier" else {}
        if _audit_enabled and _audit_profile == "frontier" and not _frontier_contract.get("ready"):
            _success_audit.update({"enabled": True, "verdict": "blocked", "frontier": {
                "verdict": "PARITY_OR_INCONCLUSIVE", "proven": False,
                "contract": _frontier_contract, "judge_count": 0, "winning_dimensions": [],
                "blockers": _frontier_contract.get("blockers") or []},
                "reason": "尚未证明超越同题顶尖参照：" + "；".join(
                    _frontier_contract.get("blockers") or ["参照合同不完整"])})
        if (_audit_enabled and _audit_candidates and not is_cancelled(qhash)
                and (_audit_profile != "frontier" or _frontier_contract.get("ready"))):
            _authors = {str(row.get("assigned_harness") or row.get("harness") or row.get("key") or "")
                        for row in _audit_candidates}
            _anonymity = _success.author_hidden_for_prompt(_audit_candidates, _authors)
            _reviewer = _pick_relay_harness("", set(_authors), valid) or ""
            if not _reviewer:
                _success_audit.update({"enabled": True, "verdict": "unverified",
                                       "reason": "没有独立于候选作者的可运行审计者"})
            else:
                _audit_plan = {**plan, "_prior_verification_reports": list(_verification_reports)}
                _audit_prompt, _aliases = _success.build_prompt(question, _audit_plan, _audit_candidates)
                if cb:
                    cb({"agent": _reviewer, "t": "verification_started", "stage": "verifying",
                        "phase": "最终成功审计", "x": f"{_reviewer} 正在匿名复核用户需求满足度"})
                _audit_run = _run_one_with_gateway(
                    _reviewer, _audit_prompt, lease_plan=plan,
                    _provider_results=_provider_results,
                    attempt_timeout=_VERIFY_TIMEOUT,  # 成功审计同硬超时:不阻塞 final 交付
                    cancel_check=lambda _reviewer=_reviewer: _request_cancelled(qhash, "success_audit", _reviewer),
                    qhash=f"{qhash}_success_audit",
                    cb=(lambda ev, _cb=cb: _cb({**ev, "success_audit": True})) if cb else None,
                    direction="深度推理", user_id=user_id,
                    conversation_id=f"{conversation_id or qhash}:success-audit",
                    runtime_brief=_rt_brief, execution_depth="standard")
                _audit_clean, _success_audit = _success.parse_report(
                    str(_audit_run.get("final") or ""), _aliases, reviewer=_reviewer,
                    author_hidden=bool(_anonymity.get("hidden")))
                _success_audit.update({"enabled": True, "profile": _audit_profile,
                                       "summary": _audit_clean[:2000],
                                       "reviewer_model": str(_audit_run.get("model_used") or _audit_run.get("model") or ""),
                                       "usage": _audit_run.get("usage") or {}})
                _usage_overhead_results.append(_audit_run)
                _verification_reports.append({"key": _reviewer, "task_id": "success_audit",
                                              "role": "success_auditor",
                                              "targets": list(_success_audit.get("scores") or {}),
                                              "report_id": "success_audit:final",
                                              "assessed_candidate_id": str(_success_audit.get("winner") or
                                                                            "__delivery__"),
                                              "supersedes_report_ids": [
                                                  str(report.get("report_id")) for report in _verification_reports
                                                  if report.get("report_id")],
                                              **_success_audit})
                if _success_audit.get("winner") and _success_audit["winner"] != "__delivery__":
                    _final_result = next((row for row in _audit_candidates
                                          if str(row.get("task_id") or row.get("key") or "")
                                          == _success_audit["winner"]), _final_result)
                if _audit_profile == "frontier":
                    _frontier_review_reports.append(dict(_success_audit))
                    _second_reviewer = _pick_relay_harness(
                        "", set(_authors) | {_reviewer}, valid) or ""
                    if _second_reviewer:
                        _reviewer_model = str(_audit_run.get("model_used") or _audit_run.get("model") or "")
                        _alternate = select_usable_model(
                            "深度推理", exclude_models={_reviewer_model} if _reviewer_model else None,
                            harness_key=_second_reviewer)
                        _second_run = _run_one_with_gateway(
                            _second_reviewer, _audit_prompt, lease_plan=plan,
                            _provider_results=_provider_results,
                            cancel_check=lambda _reviewer=_second_reviewer:
                                _request_cancelled(qhash, "frontier_audit_2", _reviewer),
                            qhash=f"{qhash}_frontier_audit_2",
                            cb=(lambda ev, _cb=cb: _cb({**ev, "success_audit": True})) if cb else None,
                            preferred_model=str(_alternate.get("model") or "") or None,
                            preferred_appid=str(_alternate.get("appid") or "") or None,
                            direction="深度推理", user_id=user_id,
                            conversation_id=f"{conversation_id or qhash}:frontier-audit-2",
                            runtime_brief=_rt_brief, execution_depth="standard")
                        _second_clean, _second_report = _success.parse_report(
                            str(_second_run.get("final") or ""), _aliases, reviewer=_second_reviewer,
                            author_hidden=bool(_anonymity.get("hidden")))
                        _second_report.update({"enabled": True, "profile": _audit_profile,
                                               "summary": _second_clean[:2000],
                                               "reviewer_model": str(_second_run.get("model_used") or _second_run.get("model") or ""),
                                               "usage": _second_run.get("usage") or {}})
                        _frontier_review_reports.append(_second_report)
                        _verification_reports.append({"key": _second_reviewer,
                                                      "task_id": "frontier_audit_2",
                                                      "role": "success_auditor",
                                                      "targets": list(_second_report.get("scores") or {}),
                                                      **_second_report})
                        _usage_overhead_results.append(_second_run)
                    _success_audit["frontier"] = _success.frontier_verdict(
                        plan, _frontier_review_reports)
                    if not _success_audit["frontier"].get("proven"):
                        _success_audit["verdict"] = "blocked"
                        _success_audit["reason"] = (
                            "尚未证明超越同题顶尖参照：" + "；".join(
                                _success_audit["frontier"].get("blockers") or ["独立盲评未形成窄面胜出证据"]))
                if cb:
                    cb({"agent": _reviewer, "t": "verification_result", "stage": "verifying",
                        "phase": "最终成功审计", "success_audit": _success_audit,
                        "x": _success_audit.get("reason") or f"质量审计：{_success_audit.get('verdict')}"})
    except Exception as _success_exc:
        _audit_config = plan.get("success_audit")
        _success_audit = {"enabled": isinstance(_audit_config, dict) and _audit_config.get("enabled") is True,
                          "verdict": "unverified", "profile": "unknown",
                          "winner": "", "scores": {},
                          "reason": f"Success Auditor 未完成：{type(_success_exc).__name__}: {_success_exc}"}

    # frontier 档的失败/证据不足必须回流同一验收中心待办，不能只在一次响应里消失；
    # 这里只登记真实审计缺口，不自动改代码、manifest 或发布状态。
    if _success_audit.get("enabled") and _success_audit.get("profile") == "frontier" and not (
            (_success_audit.get("frontier") or {}).get("proven")):
        try:
            import verification_reviews as _quality_reviews
            _quality_target = "session:" + str(conversation_id or qhash)
            _frontier = _success_audit.get("frontier") or {}
            _quality_reviews.save(
                _quality_target, "issue", str(_success_audit.get("reason") or "前沿比较未形成胜出证据"),
                "success-auditor", feature="同题顶尖基线交付质量",
                priority="P1", upgrade_status="blocked", machine_status="issue",
                source_finding_id="frontier:" + str(qhash), source_finding_kind="frontier_delivery",
                root_cause_status="unresolved", root_cause_evidence=[], skeleton_zones=[],
                evidence="；".join(str(item) for item in (_frontier.get("blockers") or [])),
                tracked_files=[], require_machine_gate=True, require_lifecycle_gate=True,
                continuous_reverify=True,
                acceptance_criteria="同一 golden task、同一限制下，经两名隐藏作者身份的独立评委确认：正确性不退化且至少两个维度一致胜出",
                user_value="用户拿到的是有同题证据证明优于顶尖参照的成品，而不是模型自评的高分",
                upgrade_description="补齐同题参照、真实产物证据和独立盲评后持续改进",
                next_action="先定位未达前沿的源码/流程根因；根因未核验前禁止改骨架",
                _workflow_token=_quality_reviews._WORKFLOW_TOKEN)
        except Exception:
            pass

    if (orchestration_spec and orchestration_spec.verification.enabled) or _success_audit.get("enabled"):
        try:
            import task_outcome as _task_outcome
            _outcome = _task_outcome.from_dispatch(
                {"results": _candidate_results, "cancelled": whole_cancelled,
                 "verification_reports": _verification_reports,
                 "verification_required": True, "success_audit": _success_audit,
                 "revision_results": _revision_results},
                delivery_contract=dict(plan.get("delivery_contract") or {}))
        except Exception:
            pass

    # 质量统计只接受结构化独立验证；Success Auditor 的六维 overall 优先于粗粒度 verdict。
    if _verification_reports:
        if _delivery_milestone:
            try:
                _delivery_milestone.update(list(results.values()), _verification_reports)
            except Exception:
                pass
        try:
            import success_auditor as _success
            _verified_quality = _success.score(_success_audit)
        except Exception:
            _verified_quality = None
        if _verified_quality is None:
            _score_map = {"pass": 100.0, "revise": 50.0, "fail": 0.0}
            _verified_scores = [_score_map[report["verdict"]] for report in _verification_reports
                                if report.get("verdict") in _score_map]
            if _verified_scores:
                _verified_quality = sum(_verified_scores) / len(_verified_scores)
        if _verified_quality is not None:
            try:
                import harness_performance as _verified_perf
                for _candidate in _pre_revision_results:
                    if _candidate.get("verification_role"):
                        continue
                    # The independent score belongs to the assessed delivery,
                    # not every earlier candidate or supporting contribution.
                    if (len(_pre_revision_results) != 1 or _revision_results
                            or _candidate.get("supporting_context")):
                        continue
                    _verified_perf.record(
                        str(_candidate.get("relay_by") or _candidate.get("assigned_harness")
                            or _candidate.get("harness") or _candidate.get("key") or ""),
                        str(plan.get("direction") or next(iter((plan.get("directions") or {}).values()), "general")),
                        str(mode or "single"), ok=_is_delivered(_candidate),
                        wall_s=float(_candidate.get("elapsed") or 0), quality=_verified_quality,
                        artifacts=len(_candidate.get("artifacts") or []), source="independent_verifier")
            except Exception:
                pass
    # 交付知识库收割(2026-09-08用户钉死'不想B站变特殊例子'):world_class 交付的锚点表/已证伪
    # 死路/桂冠参照自动入持久知识库,下次同品类交付直接站上本次终点。与成功审计无关——
    # 失败交付的反模式同样是最有价值的知识(B站v1黑屏实锤)。fail-open 绝不阻塞。
    try:
        import delivery_knowledge as _dk
        _packet_for_kb = (_project_context.load(team_id) or {}) if _project_context else {}
        _dk.capture_from_packet(question, plan, _packet_for_kb, results,
                                team_id=str(team_id), user_id=user_id)
    except Exception:
        pass
    # 只有通过最终成功审计（或无需重型审计）的成品进入经验库，避免把低质量答案放大到下一轮。
    if _success_audit.get("verdict") in {"pass", "not_required"}:
        _record_experience(question, _candidate_results, user_id=user_id)
    try:
        import harness_performance as _collab_perf
        _contract_result = _outcome.get("delivery_contract") or {}
        _checks = _contract_result.get("checks") or []
        _contract_score = (100.0 * sum(bool(item.get("passed")) for item in _checks) / len(_checks)
                           if _checks else None)
        _collab_perf.record_collaboration(
            str(plan.get("direction") or next(iter((plan.get("directions") or {}).values()), "general")),
            str(mode or "single"), len(set(task_assignments.values())),
            completed=_outcome.get("status") == "completed",
            wall_s=time.time() - _dispatch_started, quality=_verified_quality,
            contract_score=_contract_score,
            attempts=sum(int(r["attempts"]) if r.get("attempts") is not None else 1 for r in ordered),
            failovers=sum(bool(r.get("via_relay") or r.get("via_failover")) for r in _candidate_results),
            artifacts=sum(len(r.get("artifacts") or []) + len(r.get("discovered_artifacts") or [])
                          for r in _candidate_results),
            pattern=(orchestration_spec.pattern if orchestration_spec else mode),
            verification=bool(orchestration_spec and orchestration_spec.verification.enabled),
        )
    except Exception:
        pass
    if cb:
        if whole_cancelled:
            _summary = f"本次军团已停止：保留 {delivered} 家已完成交付，停止 {cancelled_count} 家"
        else:
            _extra = ((f"（{timed_out} 家超时" if timed_out else "（") +
                      (f"·{cancelled_count} 家由用户停止" if cancelled_count else "") + "）") \
                     if timed_out or cancelled_count else ""
            _summary = f"军团完成：{delivered}/{len(ordered)} 家交付{_extra}"
        cb({"agent": "commander", "t": "summary", "x": _summary,
            "done": delivered, "total": len(ordered), "cancelled": whole_cancelled,
            "cancelled_count": cancelled_count})
    # ── UI 模式契约(2026-08-29·前端两模式联动的唯一事实源)─────────────────────────
    # 前端有 simple/full 两种视图,自动切换【只能由真实结构化现场触发】,不猜。
    # 触发规则(命中任一即 full·否则 simple):
    #   1) 多家真交付(delivered≥2)——用户需要看谁交了什么·比较/挑选
    #   2) 有编排结构(hierarchy 有依赖 或 cyclic 非空)——树状指挥图值得展开
    #   3) 有 orchestration_request(handoff/fork/need_peer)——成员自主编排链路要可见
    #   4) 有超时/全失败/取消——用户需要看清状态并决策(重试/换家/放弃)
    # 只给 context 不加 control:前端仍可让用户强制回落简洁模式(sticky per session)。
    _has_orch_req = any(isinstance((r or {}).get("orchestration_request"), dict) for r in ordered)
    _has_deps = bool(dependencies) or bool(cyclic)
    _needs_decision = (bool(timed_out) or bool(ordered and delivered == 0 and not whole_cancelled)
                       or bool(whole_cancelled) or bool(_outcome.get("contract_incomplete")))
    _mode_reasons = []
    if delivered >= 2:
        _mode_reasons.append("multi_delivered")
    if _has_deps:
        _mode_reasons.append("has_orchestration_deps")
    if _has_orch_req:
        _mode_reasons.append("member_orchestration_request")
    if _needs_decision:
        _mode_reasons.append("needs_user_decision")
    _ui_mode = "full" if _mode_reasons else "simple"
    mode_reason = {"mode": _ui_mode, "triggers": _mode_reasons,
                   "delivered": delivered, "total": len(ordered),
                   "has_deps": _has_deps, "has_orch_req": _has_orch_req,
                   "needs_decision": _needs_decision}

    try:
        import token_telemetry as _token_telemetry
        _usage_summary = _token_telemetry.summarize([*ordered, *_usage_overhead_results])
        _usage_summary["delivery_members"] = len(ordered)
        _usage_summary["overhead_calls"] = len(_usage_overhead_results)
    except Exception:
        _usage_summary = {"members": [], "available_members": 0,
                          "unavailable_members": len(ordered)}
    _overhead_usage_records = [
        {"key": row.get("key") or row.get("harness") or "audit",
         "model_used": row.get("model_used") or row.get("model") or "",
         "usage": _token_telemetry.normalize(
             row.get("usage"), model=str(row.get("model_used") or row.get("model") or ""),
             elapsed=row.get("elapsed"))}
        for row in _usage_overhead_results if isinstance(row, dict)]
    # 顶层完成契约(诚实·让前端/调用方一眼知道整单结果·不用自己遍历漏判)。
    # 依据(harness 源码):codex `thread_manager.rs:203-214 ThreadShutdownReport` 把多编排的
    # 每个 thread 终态【分类收集】成 completed/submit_failed/timed_out,不用一个笼统布尔;
    # 且 `codex_delegate.rs:237` 明确区分 TurnComplete vs TurnAborted(完成≠中止)。对齐:
    #   delivered_count=真交付家数(≈completed·过 _is_delivered·不是光看 status=done)
    #   timed_out=超时慢家数(≈timed_out·和"真失败"分开·前端可区分"慢"vs"错")
    #   all_failed=补位接力用尽后一家都没交付(≈全非 completed·前端显式提示重试·不假装成功)
    _intermediate_stop.set()  # 看门狗退场:本单已收尾
    if _execution_plan is not None:
        # In-process snapshot for the parent only, not another public plan
        # field. Include compiler-generated nodes, their policies and bindings.
        _execution_plan.update(_snapshot_execution_plan())
    out = {"results": ordered, "hierarchy": hierarchy, "team_id": team_id,
            "execution_mode": mode,
            "dependencies": dependencies, "cyclic": cyclic, "cycle_broken": cycle_broken_edges,
            "gold_unknown": True,
            "task_assignments": task_assignments, "assignment_decisions": assignment_decisions,
            "delivered_count": int(_outcome.get("delivered_count") or delivered),
            "total": int(_outcome.get("total") or len(ordered)),
            "timed_out": timed_out,
            "mode_reason": mode_reason,
            # 取消语义(军团自审 P0):前端据此不把"用户停止"包装成"军团完成/失败"。
            "cancelled": whole_cancelled, "cancelled_count": cancelled_count,
            "all_failed": bool(ordered) and _outcome.get("status") not in {"completed", "cancelled"}
                          and int(_outcome.get("delivered_count") or 0) == 0,
            # typed 终态(2026-09-02 S3 五根因①③):completed/partial/failed/incomplete/
            # cancelled/blocked 单一事实源·规划器(资格账本)/Session/SSE/UI 四处共用同一份,
            # 不再各自判断"完成"。纯投影·不改答案·不加语义门。
            "orchestration_spec": (orchestration_spec.to_dict() if orchestration_spec else {}),
            "scheduler_policy": _scheduler_policy,
            "pipeline": list(plan.get("pipeline") or []) if mode == "relay" else [],
            "final_result": _final_result,
            "team_size_decision": _team_size_decision,
            "coordination_history": _coordination_history,
            "verification_reports": _verification_reports,
            "verification_required": bool(
                (orchestration_spec and orchestration_spec.verification.enabled) or
                _success_audit.get("enabled")),
            "success_audit": _success_audit,
            # winner 兜底(2026-09-13·前端联调对账§12):PK 多家交付时用户需要明确推荐,
            # 成功审计未启用(简单任务 not_required)时 winner 恒空→前端🏆推荐章永远不出现。
            # 兜底:审计有 winner 用审计;否则取 _winner_final 同源选择(交付家·非字数排序),
            # 并派生 winner_note(推荐理由·中性表述·不做贬低式对比)。
            "winner": _success_audit.get("winner") or _recommend_winner(ordered, mode, _final_result),
            "overhead_usage_records": _overhead_usage_records,
            # winner_note(推荐理由·中性白话):审计 note 优先;兜底从交付事实拼(用时/交付物数),
            # 不做"比谁好X%"式贬低对比(产品功能设计§F3.5 文案纪律)。
            "winner_note": _success_audit.get("reason") or _winner_note(
                _success_audit.get("winner") or _recommend_winner(ordered, mode, _final_result), ordered),
            # baseline(F3.5 基线对照·后端配合改动.md§10):单模型对照结果。
            # 当前形态:对照档案(harness_baselines/)命中时填·未命中时省略(前端不渲染)。
            # 预跑存档任务排期在后台·本字段是前端唯一消费口。
            "baseline": _baseline_snapshot(str(question), ordered),
            "usage": _usage_summary,
            "revision_results": _revision_results,
            "delivery_contract": dict(plan.get("delivery_contract") or {}),
            "experience_intent": dict(plan.get("experience_intent") or {}),
            "frontier_research_contract": dict(plan.get("frontier_research_contract") or {}),
            "selection": _selection_facts(
                {"winner": _success_audit.get("winner") or _recommend_winner(ordered, mode, _final_result)},
                ordered, mode, _final_result, _success_audit),
            "supporting_context_results": [r for r in ordered if r.get("supporting_context")],
            "outcome": _outcome,
            "partial": _outcome.get("status") == "partial",
            "contract_incomplete": bool(_outcome.get("contract_incomplete"))}
    # A later accepted control turn supersedes an earlier execution failure,
    # without deleting that failure from the evidence history.
    latest_coordination = _coordination_history[-1] if _coordination_history else {}
    unavailable = latest_coordination if _coordination_unavailable(latest_coordination) else None
    coordination_cancelled = any(item.get("cancelled") for item in _coordination_history)
    if unavailable:
        out["coordination"] = unavailable
        out["coordination_incomplete"] = True
    if coordination_cancelled:
        out["cancelled"] = True
    if unavailable or coordination_cancelled:
        import task_outcome
        task_outcome.attach(out)
        out["status"] = ("cancelled" if out["cancelled"] else
                         "partial" if out["partial"] else "error")
    return out


def _no_progress(current: str, previous: str) -> bool:
    """续跑转圈检测(依据 CC pilot_v2.py:28"连续真转圈才刹车" + DeepSeek
    repeat-tool-reminder:监视重复·只提醒/结束不否决)。不是语义门:只在两轮输出【规范化后完全相同】
    才算无新进展(不做模糊相似度猜测·避免误伤合理迭代);让循环提前收尾而不是白跑到6轮。
    """
    def norm(s):
        return " ".join(str(s or "").lower().split())
    a, b = norm(current), norm(previous)
    return bool(a and b and a == b)


def _baseline_snapshot(question: str, ordered: list) -> dict | None:
    """F3.5 基线对照快照：后台对高频 query 预跑的单模型结果存档命中时返回。

    存档位置:harness_baselines/<query_sha1[:16]>.json {model_name, content, wall_s, artifacts}。
    未命中返回 None(final 省略 baseline 字段·前端对照区自动隐藏)——不造假数据。
    """
    import hashlib as _hl
    try:
        import pathlib as _pl
        _dir = _pl.Path(__file__).resolve().parent.parent.parent / "harness_baselines"
        _key = _hl.sha1(str(question or "").strip().encode("utf-8")).hexdigest()[:16]
        _path = _dir / f"{_key}.json"
        if not _path.is_file():
            return None
        import json as _json
        snap = _json.loads(_path.read_text(encoding="utf-8"))
        if not (snap.get("content") or "").strip():
            return None
        return {"model_name": str(snap.get("model_name") or "单模型对照"),
                "content": str(snap.get("content")),
                "wall_s": snap.get("wall_s"),
                "artifacts": snap.get("artifacts") or []}
    except Exception:
        return None


_VERSION_RE = __import__("re").compile(r"[-_ v](\d+)$|\((\d+)\)$")


def _selection_row(pool: list, winner: str) -> dict | None:
    """Resolve a unique task first, then a unique legacy key; never guess."""
    if not winner:
        return None
    for field in ("task_id", "key"):
        matches = [row for row in pool if str(row.get(field) or "") == winner]
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def _selection_facts(out: dict, ordered: list, mode: str, final_result,
                     success_audit: dict) -> dict:
    """Describe the actual final selection without inventing a quality decision."""
    candidates = [row for row in (ordered or [])
                  if not row.get("supporting_context") and not row.get("verification_role")]
    delivered = [row for row in candidates if _is_delivered(row) and (row.get("final") or "").strip()]
    pool = delivered or [row for row in candidates if (row.get("final") or "").strip()]
    if not pool:
        pool = [row for row in (out.get("results") or [])
                if not row.get("supporting_context") and (row.get("final") or "").strip()]
    winner = str(out.get("winner") or success_audit.get("winner") or "")
    audit_winner = str(success_audit.get("winner") or "")
    chosen = _selection_row(pool, winner)
    if winner and chosen is None:
        if isinstance(final_result, dict) and final_result in pool and mode == "relay":
            chosen, source = final_result, "final_result_reference"
        else:
            source = ("success_audit_bundle" if winner == "__delivery__" and audit_winner == winner
                      else "unresolved_winner")
    elif chosen is not None:
        source = "success_audit" if winner == audit_winner else "explicit_winner"
    elif isinstance(final_result, dict) and final_result in pool:
        chosen, source = final_result, "final_result_reference"
    elif (len(delivered) > 1 and (mode == "pk" or
          (mode != "relay" and bool(success_audit.get("winner_rejected"))))):
        source = "unadjudicated_candidates"
    elif pool:
        chosen, source = pool[-1], "ordered_delivery_fallback"
    else:
        source = "no_candidate"
    evidence = success_audit.get("evidence_refs") or success_audit.get("evidence") or []
    return {
        "selected_task_id": str((chosen or {}).get("task_id") or (chosen or {}).get("key") or ""),
        "selected_harness": str((chosen or {}).get("relay_by") or (chosen or {}).get("harness") or ""),
        "source": source,
        "order_basis": "task_order" if source in {"ordered_delivery_fallback", "unadjudicated_candidates"} else "not_applicable",
        "author_basis": "not_recorded",
        "audit_basis": "recorded" if source in {"success_audit", "success_audit_bundle"} else "not_recorded",
        "evidence_refs": list(evidence) if source in {"success_audit", "success_audit_bundle"}
                         and isinstance(evidence, (list, tuple)) else [],
    }


def _winner_note(winner: str, ordered: list) -> str:
    """推荐理由兜底：从交付事实(耗时/产物数)拼中性白话；无 winner 返回空。"""
    row = next((r for r in (ordered or [])
                if str(r.get("key") or r.get("task_id") or "") == str(winner or "")), None)
    if not row:
        return ""
    bits = []
    arts = row.get("artifacts") or []
    if arts:
        bits.append(f"交付 {len(arts)} 个产物")
    elapsed = row.get("elapsed")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        bits.append(f"用时 {round(elapsed)}s")
    return (winner + "：" + "·".join(bits)) if bits else ""


def _recommend_winner(ordered: list, mode: str, final_result) -> str:
    """Only return an actually selected/audited winner, never mere delivery order.

    Order remains available through results and _winner_final. In PK, a final
    row can disagree with an earlier member without proving it is better.
    """
    if mode != "pk":
        return ""
    results = [r for r in (ordered or [])
               if not r.get("supporting_context") and not r.get("verification_role")]
    delivered = [r for r in results if _is_delivered(r) and (r.get("final") or "").strip()]
    if len(delivered) < 2:
        return ""  # 少于两家交付无"推荐"可言
    if isinstance(final_result, dict) and final_result in delivered:
        return str(final_result.get("key") or final_result.get("task_id") or "")
    return ""


def _winner_final(out: dict) -> str:
    """取被审计/显式选中的最终交付；没有质量证据时保持执行顺序，不以字数冒充质量。"""
    results = [r for r in (out.get("results") or [])
               if not r.get("supporting_context") and not r.get("verification_role")]
    delivered = [r for r in results if _is_delivered(r) and (r.get("final") or "").strip()]
    pool = delivered or [r for r in results if (r.get("final") or "").strip()]
    if not pool:
        # A review is not a replacement for an existing user deliverable. If
        # it is the only actual record, preserve that evidence rather than
        # silently returning an empty answer.
        pool = [r for r in (out.get("results") or [])
                if not r.get("supporting_context") and (r.get("final") or "").strip()]
    if not pool:
        return ""
    winner = str(out.get("winner") or (out.get("success_audit") or {}).get("winner") or "")
    final_result = out.get("final_result") if isinstance(out.get("final_result"), dict) else None
    if winner:
        chosen = _selection_row(pool, winner)
        if chosen is not None:
            return str(chosen.get("final") or "")
        # Relay has an independently established tail/last-delivered result.
        # An invalid winner or a bundle audit must not select a different row.
        if str(out.get("execution_mode") or "") == "relay" and final_result in pool:
            return str(final_result.get("final") or "")
        return ""
    if final_result and final_result in pool:
        return str(final_result.get("final") or "")
    mode = str(out.get("execution_mode") or "")
    audit = out.get("success_audit") if isinstance(out.get("success_audit"), dict) else {}
    if len(delivered) > 1 and (mode == "pk" or (mode != "relay" and audit.get("winner_rejected"))):
        # Independent PK candidates or a rejected bundle choice provide no
        # basis to silently promote a single row as the final answer.
        return ""
    return str(pool[-1].get("final") or "")


def _persistent_candidate_context(out: dict) -> str:
    """Preserve bounded, unranked candidate facts when no final was selected."""
    rows = [row for row in (out.get("results") or [])
            if isinstance(row, dict) and not row.get("supporting_context")
            and not row.get("verification_role") and _is_delivered(row)
            and isinstance(row.get("final"), str) and row["final"].strip()]
    if not rows:
        return ""
    # Cap both count and characters; disclose omissions instead of presenting
    # an incomplete excerpt as a selected answer or a full history.
    visible = rows[:12]
    per_row = max(60, 1200 // len(visible))
    lines = []
    for row in visible:
        label = str(row.get("task_id") or row.get("key") or "未上报")[:80]
        text = row["final"].strip()
        excerpt = text[:per_row]
        lines.append(f"{label}: {excerpt}" + (" [正文已截断]" if len(text) > per_row else ""))
    if len(rows) > len(visible):
        lines.append(f"另有 {len(rows) - len(visible)} 条候选未列入摘要；完整结果保留在原记录。")
    return "【未裁决候选·仅为续作事实，非最终结论】\n" + "\n".join(lines)


def _pilot_run_state(r: dict) -> dict:
    """单成员结果 → 既有续航分类器的事实投影。

    有返回允许结束本次调用，但不证明用户目标完成；缺少声明记为未知，
    而不是 False（后者会让合法短答无端续跑）。保留明确的原生继续意图，
    不执行它的编排提案、不赋予首脑身份或扩大运行预算。
    """
    d = bool(_is_delivered(r))
    state = {"delivered": d, "mutation_seen": d,
             "delivery_return_ready": d,
             "scope_goal_met": None if d else False, "scope_progressed": d,
             "all_failed": not d}
    absent = _native_terminal_message_absent(r)
    if absent:
        state["native_terminal_message_absent"] = absent
    # 默认短答可完成；已有明确目标事实不能被本轮 done 覆盖。
    for field in ("scope_goal_met", "scope_progressed"):
        if isinstance(r.get(field), bool):
            state[field] = r[field]
    if r.get("coordination_incomplete") is True:
        state["scope_goal_met"] = False
    status = str(r.get("status") or "")
    boundary = None
    if status == "cancelled" or r.get("cancelled") is True:
        boundary = {"kind": "user_cancelled", "source": "run_result.cancelled"}
    elif r.get("persistent_capped") is True or r.get("budget_exhausted") is True:
        boundary = {"kind": "budget_exhausted", "source": "run_result.budget"}
    elif status in {"waiting", "blocked", "user_decision"}:
        boundary = {"kind": status, "source": "run_result.status"}
    request = r.get("orchestration_request")
    coordinate = request.get("coordinate") if isinstance(request, dict) else None
    if isinstance(coordinate, dict) and coordinate.get("action") == "continue":
        state["scope_goal_met"] = False
        state["native_continuation"] = {
            "source": "orchestration_request.coordinate",
            "action": "continue",
        }
        if isinstance(coordinate.get("reason"), str):
            state["native_continuation"]["reason"] = coordinate["reason"]
    elif isinstance(coordinate, dict) and coordinate.get("action") in {"blocked", "user_decision"}:
        if boundary is None:
            boundary = {"kind": coordinate["action"], "source": "orchestration_request.coordinate"}
            if isinstance(coordinate.get("reason"), str):
                boundary["reason"] = coordinate["reason"]
    if boundary:
        state["scope_goal_met"] = False
        state["continuation_stop"] = boundary
    return state


def _collect_benchmark_refs(out: dict, plan: dict | None = None) -> list:
    """从本单明确来源记录提取完整可回访链接，来源存在不代表质量达标。

    来源覆盖 plan/项目现场的 quality_context、frontier_context 和显式 source_ref。
    不再从生成答案或交付物 URL 中猜世界参照；这会把自己的网页误认成对标对象。
    """
    import quality_context
    facts = quality_context.benchmark_snapshot(plan)
    return list(dict.fromkeys(row["url"] for row in facts["source_index"]))[:6]


def _plan_empty_continuation(question: str, plan: dict, previous_plan: dict,
                             team_id: str, guidance: str, *,
                             user_id: str = "", conversation_id: str = "",
                             cancel_check: Optional[Callable[[], bool]] = None,
                             _provider_results: Optional[Callable[[], list[dict]]] = None,
                             _result_observer: Optional[Callable[[dict], None]] = None) -> tuple[dict, str]:
    """No open nodes: request new work from the existing planner, not default agents."""
    import team_state
    if is_cancelled(team_id) or (cancel_check and cancel_check()):
        return plan, "planner_cancelled"
    context = dict(previous_plan)
    context["team_id"] = str(team_id)
    context["conversation_context"] = str(plan.get("conversation_context") or "")
    # 缺省仍动态选人；显式空/无效边界不能被 planner/apply 的 truthy 默认扩成全池。
    candidates = None
    for source in (previous_plan, plan):
        policy = source.get("team_policy")
        if not isinstance(policy, dict) or "candidate_harnesses" not in policy:
            continue
        raw = policy["candidate_harnesses"]
        if not isinstance(raw, (list, tuple, set)):
            return plan, "continuation_candidate_boundary_invalid"
        pool = list(dict.fromkeys(key.strip() for key in raw
                                  if isinstance(key, str) and key.strip()))
        candidates = pool if candidates is None else [key for key in candidates if key in pool]
    planner_kwargs = {"request_context": context, "user_id": user_id,
                      "conversation_id": conversation_id,
                      "cancel_check": cancel_check or (lambda: is_cancelled(team_id)),
                      "_provider_results": _provider_results}
    if candidates is not None:
        valid = set(_harness_keys()) & set(candidates)
        candidates = [key for key in candidates if key in valid]
        if not candidates:
            return plan, "continuation_candidate_boundary_empty"
        context["team_policy"] = {**(context.get("team_policy") or {}),
                                  "candidate_harnesses": list(candidates)}
        planner_kwargs["candidate_harnesses"] = list(candidates)
    proposed = plan_request_with_model(question + "\n" + guidance, **planner_kwargs)
    if _result_observer is not None:
        _result_observer(proposed)
    if (proposed.get("cancelled") or proposed.get("status") == "cancelled"
            or is_cancelled(team_id) or (cancel_check and cancel_check())):
        return plan, "planner_cancelled"
    if not proposed.get("ok") or not proposed.get("tasks"):
        return plan, str(proposed.get("error") or "continuation_plan_has_no_tasks")
    if candidates is not None:
        # 提案不得通过任一既有绑定别名绕过原候选；校验完才允许写任务。
        for node in proposed["tasks"].values():
            if isinstance(node, dict):
                for field in ("assigned_harness", "assign_to", "harness", "agent"):
                    assigned = str(node.get(field) or "").strip()
                    if assigned and assigned not in valid:
                        return plan, "continuation_assignment_outside_candidates:" + assigned
        proposed = {**proposed, "team_policy": {
            **(plan.get("team_policy") or {}), **(proposed.get("team_policy") or {}),
            "candidate_harnesses": list(candidates)}}
    else:
        valid = set(_harness_keys())
    updated, changed = _apply_coordination_to_plan(
        plan, {"next_plan": proposed}, valid_harnesses=valid, allow_graph_changes=True)
    if not changed:
        return plan, "continuation_plan_unchanged"
    if candidates is not None:
        from collaboration_runtime import bind_plan
        updated, _ = bind_plan(updated, valid)
        if updated.get("binding_errors"):
            return plan, "continuation_candidate_binding_failed"
    known = (team_state.load(team_id) or {}).get("tasks") or {}
    pending_facts = {}
    for task_id in updated.get("_coordination_reopen_task_ids") or []:
        node = updated["tasks"][task_id]
        if task_id in known:
            reopened = team_state.reopen_task(team_id, task_id, text=str(node.get("task") or ""),
                                  dependencies=list(node.get("depends_on") or []),
                                  agent=str(node.get("assigned_harness") or ""),
                                  reason="planner_continuation")
            if not reopened:
                return plan, "continuation_task_not_reopened:" + task_id
        else:
            added = team_state.add_task(team_id, task_id, str(node.get("task") or ""),
                                        list(node.get("depends_on") or []),
                                        agent=str(node.get("assigned_harness") or ""),
                                        metadata={"kind": "planner_continuation"}, registered=valid)
            if added.get("status") != "pending" or added.get("active_attempt") is not None:
                continue
        pending_facts[task_id] = {
            **node, "task_kind": node.get("task_kind")
            or (known.get(task_id) or {}).get("task_kind") or "planner_continuation"}
    if pending_facts:
        team_state.ensure(
            team_id, question, pending_facts,
            {key: list(node.get("depends_on") or []) for key, node in pending_facts.items()},
            registered=valid, sync_pending_only=True)
    return updated, ""


def _autocommit_delivery(round_plan: dict, team_id: str, rounds: int, out: dict) -> dict:
    """交付后把项目现场自动提交（只 commit，不 push、不部署——上线仍需用户确认）。

    只在同时满足时动手：有 repo_root、是真 git 仓库、本轮有真实交付、工作区有改动。
    失败只记录事实，绝不改变交付结果。
    """
    import subprocess
    import stat
    import tempfile
    from pathlib import Path
    # 单元测试/离线回归禁止 spawn 外部进程，也不应产生 git 副作用；生产运行不受影响。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"status": "skipped", "reason": "test_environment"}
    packet = round_plan.get("project_context") or {}
    root = str(packet.get("repo_root") or packet.get("workspace_root")
               or round_plan.get("repo_root") or "").strip()
    if not root:
        return {"status": "skipped", "reason": "no_repo_root"}
    delivered = [r for r in (out.get("results") or [])
                 if isinstance(r, dict) and str(r.get("status")) in ("done", "partial")]
    if not delivered:
        return {"status": "skipped", "reason": "nothing_delivered"}
    # 只提交本轮真实产物路径。过去用 git add -A 会把工作区里无关的运行时文件
    # （CLI cache/logs/device_id）一并卷进交付 commit——实跑已回退过一次。
    root_resolved = os.path.realpath(root)
    paths: list[str] = []
    for row in delivered:
        for item in ((row.get("artifacts") or []) + (row.get("discovered_artifacts") or [])):
            raw = item.get("path") if isinstance(item, dict) else item
            raw = str(raw or "").strip()
            if not raw:
                continue
            original = Path(os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw)))
            try:
                relative = original.relative_to(root_resolved)
                # A directory/link is not evidence for all files behind it.
                parents = [original, *(Path(root_resolved).joinpath(*relative.parts[:i])
                                      for i in range(1, len(relative.parts)))]
                if (not stat.S_ISREG(original.lstat().st_mode)
                        or any(p.is_symlink() for p in parents)):
                    continue
            except (OSError, ValueError):
                continue
            absolute = str(original)
            paths.append(os.path.relpath(absolute, root_resolved))
    paths = sorted(set(paths))
    if not paths:
        return {"status": "skipped", "reason": "no_artifacts", "root": root}

    # Inherited Git repository/index/config overrides must not redirect a commit.
    # The target repository's own configuration/hooks still apply.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def _run(args, *, check=False):
        result = subprocess.run(["git", "--literal-pathspecs", "-C", root_resolved, *args],
                                capture_output=True, text=True, timeout=60, env=env)
        if check and result.returncode:
            raise RuntimeError("git_" + args[0] + "_failed")
        return result

    try:
        probe = _run(["rev-parse", "--is-inside-work-tree"])
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return {"status": "skipped", "reason": "not_a_git_repo", "root": root}
        top = _run(["rev-parse", "--show-toplevel"], check=True).stdout.strip()
        if os.path.realpath(top) != root_resolved:
            return {"status": "skipped", "reason": "not_repository_root", "root": root}
        with tempfile.TemporaryDirectory(prefix="legion-delivery-index-") as directory:
            env["GIT_INDEX_FILE"] = os.path.join(directory, "index")
            head_before = _run(["rev-parse", "--verify", "HEAD"])
            if head_before.returncode == 0:
                _run(["read-tree", head_before.stdout.strip()], check=True)
            else:
                _run(["read-tree", "--empty"], check=True)
            _run(["add", "--", *paths], check=True)
            staged = _run(["diff", "--cached", "--name-only", "-z"], check=True)
            changed = [name for name in staged.stdout.split("\0") if name]
            if not changed:
                return {"status": "clean", "root": root, "paths": len(paths)}
            if not set(changed) <= set(paths):
                raise RuntimeError("unexpected_staged_paths")
            if _run(["rev-parse", "--verify", "HEAD"]).stdout != head_before.stdout:
                raise RuntimeError("head_changed_during_delivery")
            message = f"feat(legion): 自主 loop 第{rounds}轮交付 [{team_id}]"
            _run(["commit", "-m", message], check=True)
            head = _run(["rev-parse", "--short", "HEAD"], check=True)
            return {"status": "committed", "root": root, "commit": head.stdout.strip(),
                    "files": len(changed), "paths": changed[:20], "user_index_preserved": True}
    except Exception as exc:
        return {"status": "error", "reason": type(exc).__name__, "root": root}


def dispatch_persistent(question: str, plan: dict | None = None,
                        cb: Optional[Callable[[dict], None]] = None,
                        qhash: str = "adhoc", mode: str = "pk",
                        user_id: str = "default", conversation_id: str = "",
                        max_rounds: int = 6, *, _retained_results: dict | None = None,
                        _provider_results: Optional[Callable[[], list[dict]]] = None) -> dict:
    """跨轮自主循环(用户 2026-08-28 goal·军团亲口最想要的"跨轮自主循环"):
    军团有 pilot 判断力(pilot_check/classify 能说"没完成要继续")·但过去没接进执行循环——
    dispatch 跑完一轮就停·军团"跨轮会失忆·答完就停"。这里把已有的 pilot 判断真接进循环:

      dispatch 一轮 → 取 winner final → pilot.evaluate 判 continue/stop
        → stop(done/blocked/waiting/user_decision) 收尾返回
        → continue 则带【前几轮真实轨迹 + 该继续的原因】自动再派一轮(不失忆·学 codex history 回流)
      直到 pilot 判 done/真阻塞/达 max_rounds 上限(防跑飞)。

    context not control:pilot 判断本就是"只提醒不否决"·续跑是"多给一轮机会让它自己做完"·
    不强制、不改答案。pilot 关(enabled=False)时 evaluate 直接返 disabled→单轮即返(退化成 dispatch)。
    """
    from copy import deepcopy
    import legion_pilot as _lp
    from error_envelope import (
        requires_execution_pause, requires_native_settlement, is_provider_authorization_denial,
        requires_request_budget_pause, normalize as normalize_execution_error, paused_execution_tasks,
    )

    invocation_usage: dict[tuple, dict] = {}
    retained_usage_exclusions: set[str] = set()

    def refresh_invocation_usage(out):
        """Accounting only: retained work is context, not another model call."""
        import token_telemetry
        controls = list(out.get("coordination_history") or [])
        if isinstance(out.get("coordination"), dict):
            controls.append(out["coordination"])
        for control in controls:
            if control.get("source") == "native_author_proposal":
                continue  # This is the already-accounted member reply, not a new control call.
            records = control.get("execution_usage_records")
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict) and isinstance(record.get("qhash"), str):
                        invocation_usage[("coordination", record["qhash"])] = {
                            **record, "task_id": record["qhash"], "usage_role": "coordination"}
                continue
            result = control.get("result")
            if not isinstance(result, dict):
                continue
            sequence = control.get("sequence")
            invocation_usage[("coordination", sequence)] = {
                **result, "task_id": f"coordination:{sequence}", "usage_role": "coordination"}
        rows = list(invocation_usage.values())
        out["usage"] = {
            **token_telemetry.summarize(rows),
            "scope": "persistent_invocation",
            "member_calls": sum(row.get("usage_role") == "member" for row in rows),
            "coordination_calls": sum(row.get("usage_role") == "coordination" for row in rows),
            "auxiliary_calls": sum(row.get("usage_role") == "auxiliary" for row in rows),
            "retained_results_excluded": len(retained_usage_exclusions),
            "excludes": ["captain_election_outside_this_invocation",
                         "external_tool_costs_not_reported_by_native_harness"],
        }

    def retain_request_results(out):
        # Keep successes from earlier rounds even when the active plan narrowed.
        out["results"] = list(_request_results.values())
        out["dependencies"] = dict(_request_dependencies)
        out["task_assignments"] = {
            task_id: node.get("assigned_harness", "")
            for task_id, node in _request_tasks.items()}
        candidates = [row for row in out["results"]
                      if not row.get("supporting_context") and not row.get("verification_role")]
        import task_outcome
        outcome = task_outcome.from_dispatch({**out, "results": candidates})
        out["outcome"] = {**(out.get("outcome") or {}), **outcome}
        out["delivered_count"] = int(outcome.get("delivered_count") or 0)
        out["total"] = int(outcome.get("total") or len(candidates))
        out["all_failed"] = not bool(out["delivered_count"])
        out["partial"] = outcome.get("status") == "partial"
        refresh_invocation_usage(out)

    def pause_coordination(out, proposal, rounds):
        # The native control-call failure is an existing result, not a model
        # proposal or a reason to re-plan/rebind the same rejected request.
        # Cancellation may arrive while the coordinator is returning. Preserve
        # it before recomputing the existing outcome and choosing the stop reason.
        out["cancelled"] = bool(out.get("cancelled") or is_cancelled(qhash)
                                or proposal.get("cancelled"))
        if not out["cancelled"]:
            out["coordination_incomplete"] = True
        retain_request_results(out)
        out["coordination"] = proposal
        out["persistent_rounds"] = rounds
        refresh_invocation_usage(out)
        if out["cancelled"]:
            out["status"] = "cancelled"
            out["pilot"] = {"round": rounds, "action": "stop", "kind": "user_cancelled"}
            return out
        out["status"] = "partial" if out.get("partial") else "error"
        denied = is_provider_authorization_denial(proposal["result"].get("error_envelope"))
        unsettled = requires_native_settlement(proposal["result"])
        out["pilot"] = {"round": rounds, "action": "stop",
                        "kind": ("request_budget_exhausted"
                                 if requires_request_budget_pause(proposal["result"]) else
                                 "provider_authorization_required" if denied else
                                 "native_execution_unconfirmed" if unsettled else "provider_backoff")}
        if denied:
            out["pilot"]["reason"] = "提供方已拒绝本次授权；需管理员处理，本请求不自动重试。"
        elif unsettled:
            out["pilot"]["reason"] = "原生任务是否结束尚未确认；保留会话和现场，确认后再续作。"
        return out

    def incomplete_coordination(out, proposal, rounds):
        # Keep real delivered work. A protocol failure cannot certify completion,
        # nor can it authorize replaying that work or inventing a replacement plan.
        out["coordination"] = proposal
        out["coordination_incomplete"] = True
        retain_request_results(out)
        out["status"] = "partial" if out.get("partial") else "error"
        out["persistent_rounds"] = rounds
        out["pilot"] = {"round": rounds, "action": "stop",
                        "kind": ("coordination_execution_failed"
                                 if proposal.get("error") == "captain_execution_failed"
                                 else "coordination_protocol_error")}
        return out

    plan = dict(plan or {})
    original_question = str(question)
    next_guidance = ""
    # Keep the incoming request boundary separate from later member proposals.
    _author_request_policy = deepcopy(plan.get("team_policy") or {})
    _request_snapshot = (plan.get("input_snapshot") or {}).get("request_team_policy")
    if isinstance(_request_snapshot, dict):
        _author_request_policy.update(deepcopy(_request_snapshot))
    trajectory: list[str] = []   # 前几轮真实轨迹(winner final 摘要)·下一轮回流给军团·不失忆
    previous_full_final: str | None = None
    # per-key 跨轮轨迹(cc 复验揪出的真缺口·2026-08-28):旧版跨轮续跑只回流【全军团 winner】的
    # final 到 conversation_context——每家看到的是"上轮最佳产出"(常是别家),看不到"我自己上一轮
    # 干到哪"。续跑续的是别家残片而非自己的轨迹。这里额外按 key 累积每家自己上一轮的产出,通过
    # plan 下放给 _build_prelude,让它给每家单独拼上"你自己上一轮的产出"。纯 context·零 control。
    per_key_traj: dict[str, str] = {}
    last_out = None
    best_out = None  # 最近一轮有真实交付（results 含 done/partial）的 out——轮上限收尾兜底
    rounds = 0
    # 每轮结构化诊断：把"有没有世界参照、gap 关没关、产物有没有新增、停滞几轮"
    # 这些客观事实累积下来，收尾时随结果返回并落盘。它只陈述事实，不替军团判好坏。
    persistent_diagnostics: list[dict] = []
    coordination_history: list[dict] = []
    # P0-6 continue→dispatch acknowledgement 状态:reopen 时间戳/无 ack 轮数。
    _reopen_at: dict[str, float] = {}
    _unacked_counts: dict[str, int] = {}
    # P1-4 恢复循环进展预算:连续 resume 无新增 artifact hash/gap关闭/owner变化→强制root-cause。
    _prev_progress_fp = None
    _stagnant_rounds = 0
    _stagnant_note = ""
    _ack_root_cause = ""
    # Request-local execution facts. A mixed backpressure round keeps its full
    # original graph and completed inputs, rather than deleting dependency edges
    # or rebinding a held task to another model/credential.
    _request_results: dict[str, dict] = {}
    _request_tasks: dict[str, dict] = {}
    _request_dependencies: dict[str, list] = {}

    def request_provider_facts():
        current = [*(_provider_results() if _provider_results is not None else []),
                   *list(_request_results.values()),
                   *(item["result"] for item in coordination_history
                     if isinstance(item.get("result"), dict))]
        return current + [
            attempt for row in current for attempt in (row.get("prior_attempts") or [])
            if isinstance(attempt, dict) and attempt.get("error_envelope")]

    # 与 dispatch 相同的私有运行事实入口；不读取 public plan/payload 中的结果。
    _retained_results = deepcopy(_retained_results or {})
    # Keep model provenance separate from scheduler retention. A reopened node
    # must execute, but may still resume its own previously persisted cursor.
    _continuation_results = deepcopy(_retained_results)
    while rounds < max_rounds:
        rounds += 1
        round_plan = dict(plan)
        # 下一轮规划可能刚补了新节点。到实际 dispatch 前再补齐其已交付前置，
        # 复用同一请求结果；若提早于规划恢复，后来新增的依赖就会变成“缺失”。
        _round_tasks = {key: dict(node) if isinstance(node, dict) else {"task": str(node)}
                        for key, node in (round_plan.get("tasks") or {}).items()}
        _declared_task_ids = set(_round_tasks)
        for task_id in round_plan.get("_coordination_reopen_task_ids") or []:
            _retained_row = _retained_results.get(str(task_id))
            _retained_absent = _native_terminal_message_absent(_retained_row)
            if (not requires_execution_pause(_retained_row)
                    and not (_retained_absent and not _retained_absent["checkpoint_saved"])):
                _retained_results.pop(str(task_id), None)
        _prerequisites = [str(dep) for node in _round_tasks.values()
                          for dep in (node.get("depends_on") or [])]
        while _prerequisites:
            dep = _prerequisites.pop()
            if dep in _round_tasks or dep not in _request_tasks:
                continue
            _dep_result = _request_results.get(dep)
            _dep_absent = _native_terminal_message_absent(_dep_result)
            if not (_is_delivered(_dep_result) or requires_execution_pause(_dep_result)
                    or (_dep_absent and not _dep_absent["checkpoint_saved"])):
                continue
            _round_tasks[dep] = dict(_request_tasks[dep])
            _retained_results[dep] = _request_results[dep]
            _prerequisites.extend(_request_dependencies.get(dep, []))
        # Automatically restored prerequisites are earlier context, not a new
        # final declaration. Preserve their recorded order before the current
        # model-declared nodes; a LIFO walk must not move old research to the
        # default final position. Do not reorder the model's current choices.
        _restored_context = {
            task_id: _round_tasks[task_id] for task_id in _request_tasks
            if task_id in _round_tasks and task_id not in _declared_task_ids}
        round_plan["tasks"] = {**_restored_context, **_round_tasks}
        # 每轮从持久项目现场读取军团最新研究、证据、Gap、产物与失败事实。旧实现只复用启动时
        # plan，导致 frontier 节点已经把世界语境写回磁盘，下轮却仍拿旧 Context 重跑。这里
        # 只刷新事实 Context，不替军团修改任务内容或解法。
        try:
            import project_context as _persistent_context
            _latest_packet = _persistent_context.load(str(plan.get("team_id") or qhash))
            if _latest_packet:
                round_plan["project_context"] = _latest_packet
                for _field in ("experience_intent", "north_star", "critical_dimensions", "claim_ceiling",
                               "quality_protocol", "captain_thesis", "audition_evidence",
                               "frontier_research_contract", "frontier_context", "quality_context",
                               "journey_observations", "gap_plan"):
                    if _latest_packet.get(_field):
                        round_plan[_field] = _latest_packet[_field]
        except Exception:
            pass
        # 轨迹回流(军团点名要的):把前几轮自己的真实产出作为 context 带进下一轮·不是失忆重跑。
        if trajectory:
            round_plan["conversation_context"] = (
                (plan.get("conversation_context") or "")
                + "\n\n【本目标前几轮军团自己的产出·你在此基础上继续推进·别重头再来】\n"
                + "\n---\n".join(trajectory[-3:]))
        # per-key 轨迹下放:_build_prelude 会给对应家拼"你自己上一轮的产出"(接力自己·非看 winner)。
        if per_key_traj:
            round_plan["per_key_trajectory"] = dict(per_key_traj)
        if cb and rounds > 1:
            cb({"agent": "commander", "t": "task_resumed", "status": "running",
                "phase": "继续推进", "round": rounds, "recoverable": True,
                "x": f"🔄 pilot 判定这一{('单' if _lp.load_user_config(user_id).get('scope')!='session' else '会话')}目标还没真达成 → 自动续跑第 {rounds} 轮(带前几轮轨迹·不失忆)"})
        round_question = (original_question +
                          (f"\n\n【续跑指引·持续首脑/Pilot 判上一轮未达成】{next_guidance}"
                           if next_guidance else ""))
        import quality_context as _quality_facts
        _reference_facts = _quality_facts.benchmark_snapshot(round_plan)
        round_plan["reference_facts"] = _reference_facts
        if rounds == 1 or _reference_facts["reference_count"]:
            round_question += (
                "\n\n【世界标准研究·已记录现场】quality_context.sources 等结构化来源记录共 "
                + str(_reference_facts["reference_count"]) + " 条。"
                "来源存在、访问成功、完成比较分析、实际采用、作品改善是不同事实，不能互相替代。"
                "完整来源、参照分析及真实体验观察保留在本单项目现场；"
                "这份索引不表示已达标，也不规定本题的制作步骤。"
                "\n" + json.dumps(_reference_facts, ensure_ascii=False))
        _continuation_kwargs = {"_retained_results": _retained_results} if _retained_results else {}
        if _continuation_results:
            _continuation_kwargs["_continuation_results"] = _continuation_results
        if _provider_results is not None:
            _continuation_kwargs["_provider_results"] = request_provider_facts
        _execution_plan: dict = {}
        out = dispatch(round_question, plan=round_plan, cb=cb, qhash=qhash, mode=mode, user_id=user_id,
                       conversation_id=conversation_id, _execution_plan=_execution_plan,
                       **_continuation_kwargs)
        _wave_handoff = out.get("_coordination_handoff")
        coordination_history.extend(out.get("coordination_history") or [])
        out["coordination_history"] = list(coordination_history)
        for row in out.get("results") or []:
            task_id = str(row.get("task_id") or row.get("key") or "")
            if task_id in _retained_results:
                retained_usage_exclusions.add(task_id)
            else:
                invocation_usage[("member", rounds, task_id)] = {**row, "usage_role": "member"}
        for index, row in enumerate(out.get("overhead_usage_records") or []):
            if isinstance(row, dict):
                invocation_usage[("auxiliary", rounds, index)] = {**row, "usage_role": "auxiliary"}
        refresh_invocation_usage(out)
        for _row in out.get("results") or []:
            _task_id = str(_row.get("task_id") or _row.get("key") or "")
            if _task_id:
                _request_results[_task_id] = _row
                _continuation_results[_task_id] = _row
        for _task_id, _node in (_execution_plan.get("tasks") or round_plan.get("tasks") or {}).items():
            _node = dict(_node) if isinstance(_node, dict) else {"task": str(_node)}
            # dispatch returns compiled edges (including hierarchy/relay) and
            # actual assignments; don't reconstruct them by deleting old nodes.
            _deps = (out.get("dependencies") or {}).get(
                _task_id, _request_dependencies.get(_task_id, _node.get("depends_on") or []))
            _request_dependencies[_task_id] = list(_deps)
            _node["depends_on"] = list(_deps)
            _owner = (out.get("task_assignments") or {}).get(_task_id)
            if _owner:
                _node["assigned_harness"] = _owner
                for _alias in ("assign_to", "harness", "agent"):
                    if _alias in _node:
                        _node[_alias] = _owner
            _request_tasks[_task_id] = _node
        _terminal_messages = {
            task_id: observation for task_id, row in _request_results.items()
            if (observation := _native_terminal_message_absent(row))}
        # These are runtime observations, not fabricated member "continue"
        # proposals. Keep the actual author (including a relay) and native scope.
        for _task_id in _terminal_messages:
            if _task_id in _request_tasks:
                plan.setdefault("tasks", {})[_task_id] = dict(_request_tasks[_task_id])
        # 终态口径修复（2026-09-22）：记录「最近一轮有真实交付」的 out——轮上限收尾时
        # last_out 可能是纯协调轮（results 空），但成品在前几轮已落盘（首脑 complete + 产物
        # 终核）。收尾时若 last_out.results 无交付而 best_out 有，用 best_out 合并收尾，
        # 防止「成品在盘、终态却 failed 0/0」（18:13 落地页单实锤：landing.zip 2.9MB 在盘、
        # 验证链双 pass，因末轮是协调轮统计 0 交付误报 failed）。
        if (out.get("results") and any(str(r.get("status")) in ("done", "partial")
                                       for r in out.get("results") or [])):
            best_out = out
        last_out = out
        # P0-6 continue→dispatch acknowledgement(治R6·11次reopen 0次attempt):上轮 reopen
        # 的开放节点本轮必须有 queued/attempt_started/blocked_with_reason 之一;连续两轮
        # 无 ack 自动强制 rebind/re-dispatch 并注入 root-cause 事实,禁止再次纯 reopen。
        _ack_rows = []
        for _ack_task_id, _ack_reopened_at in list(_reopen_at.items()):
            try:
                import team_state as _ack_ts
                _ack = _ack_ts.attempt_ack_status(
                    str(plan.get("team_id") or qhash), _ack_task_id, after_ts=_ack_reopened_at)
            except Exception:
                _ack = {"acked": False, "via": "", "ts": 0.0}
            _latency = None
            if _ack.get("acked") and _ack.get("ts"):
                _latency = round(float(_ack.get("ts")) - float(_ack_reopened_at), 3)
                _reopen_at.pop(_ack_task_id, None)
                _unacked_counts.pop(str(_ack_task_id), None)
            else:
                _unacked_counts[str(_ack_task_id)] = _unacked_counts.get(str(_ack_task_id), 0) + 1
            _ack_rows.append({"task_id": str(_ack_task_id), "acked": bool(_ack.get("acked")),
                              "via": str(_ack.get("via") or ""),
                              "coordination_to_attempt_latency_s": _latency,
                              "unacked_rounds": _unacked_counts.get(str(_ack_task_id), 0)})
        if _ack_rows:
            out["coordination_acks"] = _ack_rows
        _stale_unacked = sorted(tid for tid, count in _unacked_counts.items()
                                if count >= 2 and tid not in _terminal_messages)
        if _stale_unacked:
            for _ack_tid in _stale_unacked:
                _ack_node = (plan.get("tasks") or {}).get(_ack_tid)
                if isinstance(_ack_node, dict):
                    _ack_node.pop("assignment_locked", None)
                    for _ack_field in ("assigned_harness", "assign_to", "harness", "agent"):
                        _ack_node.pop(_ack_field, None)
                    _ack_node["rebind_reason"] = "dispatch_ack_timeout"
            plan["_coordination_reopen_task_ids"] = sorted(
                set(plan.get("_coordination_reopen_task_ids") or []) | set(_stale_unacked))
            _ack_root_cause = (f"【协调执行断链root-cause】以下开放节点连续多轮无任何执行痕迹"
                               f"(queued/attempt_started/blocked均无):{','.join(_stale_unacked[:5])}——"
                               "已强制清除绑定重选owner,本轮必须让它们真实起跑或明确blocked,禁止再次纯reopen")
            if cb:
                cb({"agent": "commander", "t": "reason",
                    "x": f"⛔ {_ack_root_cause}"})
        # 用户取消是运行终态，不是“未完成待续跑”。整单取消和成员级取消都必须停止 Pilot；
        # 否则单节点被取消后会连续重派 6 轮，多节点中被明确停掉的成员也会被下一轮复活。
        # cancelled_count / result.status 都是 dispatch 已产生的客观运行事实，不涉及答案质量判断。
        _cancelled_results = [row for row in (out.get("results") or [])
                              if row.get("status") == "cancelled"]
        if out.get("cancelled") or is_cancelled(qhash) or _cancelled_results:
            out["persistent_rounds"] = rounds
            out["cancelled_count"] = max(int(out.get("cancelled_count", 0) or 0),
                                         len(_cancelled_results))
            out["cancelled"] = bool(out.get("cancelled") or is_cancelled(qhash))
            out["pilot"] = {"round": rounds, "action": "stop", "kind": "user_cancelled"}
            return out
        _paused_coordination = next(
            (item for item in out.get("coordination_history", [])
             if requires_execution_pause(item.get("result"))), None)
        if _paused_coordination:
            return pause_coordination(out, _paused_coordination, rounds)
        _request_policies = {key: node.get("dependency_policy", _DEFAULT_DEP_POLICY)
                             for key, node in _request_tasks.items()}
        _provider_paused = _provider_backoff_tasks(
            _request_results, _request_dependencies,
            _request_policies)
        _terminal_held = paused_execution_tasks(
            _request_results, _request_dependencies, _request_policies,
            pause=lambda row: bool(
                (observation := _native_terminal_message_absent(row))
                and not observation["checkpoint_saved"]),
            delivered=_is_delivered, dependency_check=_deps_satisfied)
        _paused = _provider_paused | _terminal_held
        _open_independent = set()
        if _paused:
            # Preserve even successes from earlier rounds, before ordinary
            # continuation narrowed its task list. These are facts, not a new
            # quality decision; use the existing outcome projection.
            retain_request_results(out)
            _open_independent = {
                task_id for task_id in _request_tasks
                if task_id not in _paused and not _is_delivered(_request_results.get(task_id))}
            if not _open_independent and _provider_paused:
                out["persistent_rounds"] = rounds
                _authorization_denied = any(
                    is_provider_authorization_denial(_request_results.get(key, {}).get("error_envelope"))
                    for key in _paused)
                _native_unsettled = any(
                    requires_native_settlement(_request_results.get(key)) for key in _paused)
                from error_envelope import requires_output_recovery_pause
                _output_limited = any(
                    requires_output_recovery_pause(_request_results.get(key)) for key in _paused)
                _request_budget = any(
                    requires_request_budget_pause(_request_results.get(key)) for key in _paused)
                out["pilot"] = {
                    "round": rounds, "action": "stop",
                    "kind": ("request_budget_exhausted" if _request_budget else
                             "provider_authorization_required" if _authorization_denied else
                             "native_execution_unconfirmed" if _native_unsettled else
                             "output_recovery_required" if _output_limited else "provider_backoff")}
                if _request_budget:
                    out["pilot"]["reason"] = "原请求总预算已用尽；保留原任务与已有成果，不以换家续作取得新额度。"
                elif _authorization_denied:
                    out["pilot"]["reason"] = "提供方已拒绝本次授权；需管理员处理，本请求不自动重试。"
                elif _native_unsettled:
                    out["pilot"]["reason"] = "原生任务是否结束尚未确认；保留会话和现场，确认后再续作。"
                elif _output_limited:
                    out["pilot"]["reason"] = "输出截断恢复仍未解决；保留原线程和文件，不再原样重放。"
                return out
            plan = {**plan, **_execution_plan,
                    "tasks": {task_id: dict(node) for task_id, node in _request_tasks.items()}}
            _limited = _provider_backoff_tasks(_request_results, {})
            _retained_results = {
                task_id: row for task_id, row in _request_results.items()
                if task_id in _limited or _is_delivered(row)
                or (task_id in _terminal_messages
                    and not _terminal_messages[task_id]["checkpoint_saved"])}
        if out.get("coordination_incomplete") and not out.get("cancelled"):
            return incomplete_coordination(out, out.get("coordination") or {}, rounds)
        final = _winner_final(out)
        same_full_final = (previous_full_final is not None
                           and _no_progress(final, previous_full_final))
        previous_full_final = final
        # An empty selected final is deliberate for unresolved candidates; it
        # must not erase their factual evidence from a later continuation.
        trajectory_fact = final[:1500] if final else _persistent_candidate_context(out)
        if trajectory_fact:
            trajectory.append(trajectory_fact)
        coordination = {}
        objective_outcome = out.get("outcome") if isinstance(out.get("outcome"), dict) else {}
        objective_complete = bool(
            objective_outcome.get("status") == "completed"
            and not objective_outcome.get("verification_incomplete")
            and not objective_outcome.get("contract_incomplete"))
        _author_proposal = {}
        _author_origin = round_plan.get("initial_author") or {}
        if isinstance(_author_origin, dict) and _author_origin.get("task_id"):
            _author_task_id = str(_author_origin["task_id"])
            _author_result = next((row for row in out.get("results") or []
                                   if str(row.get("task_id") or row.get("key") or "") == _author_task_id), {})
            _author_request = (_author_result.get("orchestration_request") or {}).get("coordinate")
            if (isinstance(_author_request, dict)
                    and bool(_lp.load_user_config(user_id).get("enabled"))
                    and _author_task_id not in _retained_results
                    and not requires_execution_pause(_author_result)
                    and not _author_result.get("supporting_context")):
                import collaboration_runtime as _author_protocol
                import project_context as _author_context
                import team_state as _author_team
                _author_state = _author_team.load(str(round_plan.get("team_id") or qhash))
                _author_packet = _author_context.load(str(round_plan.get("team_id") or qhash))
                _author_context.require_owner(_author_packet, user_id, conversation_id or qhash)
                _author_context.require_owner(_author_state, user_id, conversation_id or qhash)
                _author_owner = str(_author_result.get("assigned_harness")
                                    or _author_result.get("harness") or "")
                _author_node = (_author_state.get("tasks") or {}).get(_author_task_id) or {}
                if (_author_node.get("assigned_harness") == _author_owner
                        and _author_node.get("active_attempt") is None and _author_node.get("finished_at")):
                    _author_proposal = _author_protocol.normalize_coordination(
                        _author_request, protocol=dict(_author_result.get("collaboration_protocol") or {}),
                        require_action=True, strict=True)
                    _author_proposal.update(
                        source="native_author_proposal", author=_author_owner, task_id=_author_task_id,
                        sequence=rounds, result=_author_result)
                    if not _author_proposal.get("ok"):
                        _author_proposal["error"] = "author_invalid_coordinate_shape"
                    # A native plan may reorganize work, never enlarge the caller's
                    # candidate/permission/resource boundaries or clear them.
                    _next = _author_proposal.get("next_plan") or {}
                    _proposed_policy = _next.get("team_policy")
                    if "team_policy" in _next and not isinstance(_proposed_policy, dict):
                        _author_proposal.update(ok=False, action=None,
                                               error="author_invalid_team_policy")
                    elif isinstance(_proposed_policy, dict):
                        _next["team_policy"] = {**_proposed_policy, **_author_request_policy}
                    _author_valid = set(_harness_keys())
                    _request_candidates = _author_request_policy.get("candidate_harnesses")
                    if _request_candidates is not None:
                        if not isinstance(_request_candidates, (list, tuple, set)):
                            _author_valid = set()
                        else:
                            _author_valid &= {str(key) for key in _request_candidates if str(key)}
                    _owner_patches = [*(_author_proposal.get("task_updates") or {}).values(),
                                      *(_next.get("tasks") or {}).values()]
                    _outside_owners = sorted({str(node[field]) for node in _owner_patches
                                              if isinstance(node, dict)
                                              for field in ("assigned_harness", "assign_to", "harness", "agent")
                                              if node.get(field) and str(node[field]) not in _author_valid})
                    _conflicting_owners = any(
                        len({str(node[field]) for field in ("assigned_harness", "assign_to", "harness", "agent")
                             if node.get(field)}) > 1 for node in _owner_patches if isinstance(node, dict))
                    if _conflicting_owners:
                        _author_proposal.update(ok=False, action=None, error="author_conflicting_assignment_aliases")
                    if _outside_owners:
                        _author_proposal.update(ok=False, action=None,
                            error="author_assignment_outside_request_candidates",
                            protocol_feedback={"outside_request_candidates": _outside_owners})
        _author_continue_without_graph = False
        if ((_wave_handoff or _author_proposal
             or (not _paused and _coordination_enabled(round_plan, mode)))
                and not is_cancelled(qhash)):
            if _wave_handoff:
                coordination = _wave_handoff["proposal"]
                plan = {**plan, **_wave_handoff["execution_plan"],
                        "tasks": {key: dict(node) for key, node in _request_tasks.items()}}
            elif _author_proposal:
                coordination = _author_proposal
                plan = {**plan, **_execution_plan,
                        "tasks": {key: dict(node) for key, node in _request_tasks.items()}}
            else:
                coordination = _run_captain_coordination(
                    original_question, round_plan, str(round_plan.get("team_id") or qhash),
                    out.get("results") or [], [], phase="round_complete", sequence=rounds,
                    run_facts={"outcome": out.get("outcome") or {},
                               "verification_reports": out.get("verification_reports") or [],
                               "success_audit": out.get("success_audit") or {},
                               "contract_incomplete": bool(out.get("contract_incomplete")),
                               "cycle_broken": out.get("cycle_broken") or []},
                    cb=cb, user_id=user_id, conversation_id=conversation_id,
                    _provider_results=request_provider_facts)
            if (requires_execution_pause(coordination.get("result"))
                    or coordination.get("cancelled") or is_cancelled(qhash)):
                return pause_coordination(out, coordination, rounds)
            if (coordination.get("action") in _COORDINATION_ACTIONS
                    or _coordination_unavailable(coordination)):
                # 结构未生效也是本次真实协调结果，不能只保留成功提案。
                if not _wave_handoff:
                    coordination_history.append(coordination)
                out["coordination"] = coordination
                out["coordination_history"] = list(coordination_history)
                refresh_invocation_usage(out)
            if _author_proposal and not coordination.get("ok"):
                _author_context.record_coordination(
                    str(round_plan.get("team_id") or qhash), _author_owner, coordination)
                return incomplete_coordination(out, coordination, rounds)
            if _coordination_unavailable(coordination):
                return incomplete_coordination(out, coordination, rounds)
            if coordination.get("ok"):
                if coordination.get("action") == "continue":
                    next_plan, changed = _apply_coordination_to_plan(
                        plan, coordination,
                        pending_only=(set(_wave_handoff["pending_task_ids"]) if _wave_handoff else None),
                        valid_harnesses=_author_valid if _author_proposal else _harness_keys(),
                        allow_graph_changes=True)
                    _author_continue_without_graph = bool(_author_proposal and not changed)
                    if changed:
                        plan = next_plan
                        try:
                            import collaboration_runtime as _persistent_binding
                            _valid = _author_valid if _author_proposal else set(_harness_keys())
                            _runnable = set(_aigc_ready().get("runnable_clis", [])) & _valid
                            plan, _ = _persistent_binding.bind_plan(
                                plan, _valid, runnable=_runnable)
                            if _author_proposal and plan.get("binding_errors"):
                                raise ValueError("author_plan_binding_failed")
                        except Exception as exc:
                            if _wave_handoff or _author_proposal:
                                return incomplete_coordination(
                                    out, {**coordination, "ok": False,
                                          "error": "coordination_handoff_bind_failed",
                                          "runtime_error": type(exc).__name__}, rounds)
                        try:
                            import team_state as _persistent_team_state
                            current_state = _persistent_team_state.load(
                                str(plan.get("team_id") or qhash)) or {}
                            known_tasks = set((current_state.get("tasks") or {}))
                            scoped_policy = _native_scope_facts(plan)
                            current_spec = dict(current_state.get("orchestration_spec") or {})
                            saved_policy = dict(current_spec.get("team_policy") or {})
                            if scoped_policy or "native_session_scope" in saved_policy:
                                if scoped_policy:
                                    saved_policy["native_session_scope"] = scoped_policy["native_session_scope"]
                                else:
                                    saved_policy.pop("native_session_scope", None)
                                current_spec["team_policy"] = saved_policy
                                _persistent_team_state.ensure(
                                    str(plan.get("team_id") or qhash), original_question, {}, {},
                                    orchestration_spec=current_spec, user_id=user_id,
                                    conversation_id=conversation_id)
                            reopen_ids = {str(value) for value in
                                          (plan.get("_coordination_reopen_task_ids") or [])}
                            # Pause roots before changing persistent status.
                            # Do not use old-graph descendants: the received
                            # proposal may have legitimately released them.
                            reopen_ids -= {
                                key for key, row in _request_results.items()
                                if requires_execution_pause(row)
                                or ((observation := _native_terminal_message_absent(row))
                                    and not observation["checkpoint_saved"])}
                            pending_facts = {}
                            for task_id, node in (plan.get("tasks") or {}).items():
                                if str(task_id) not in reopen_ids:
                                    continue
                                node = dict(node) if isinstance(node, dict) else {"task": str(node)}
                                if task_id not in known_tasks:
                                    added = _persistent_team_state.add_task(
                                        str(plan.get("team_id") or qhash), str(task_id),
                                        str(node.get("task") or "回答问题"),
                                        list(node.get("depends_on") or []),
                                        agent=str(node.get("assigned_harness") or ""),
                                        metadata={"kind": "captain_next_plan", "round": rounds,
                                                  "native_session_scope": node.get("native_session_scope")},
                                        registered=set(_harness_keys()))
                                    if added.get("status") != "pending" or added.get("active_attempt") is not None:
                                        continue
                                else:
                                    reopened = _persistent_team_state.reopen_task(
                                        str(plan.get("team_id") or qhash), str(task_id),
                                        text=str(node.get("task") or "回答问题"),
                                        dependencies=list(node.get("depends_on") or []),
                                        agent=str(node.get("assigned_harness") or ""),
                                        reason="author_next_plan" if _author_proposal else "captain_next_plan",
                                        native_session_scope=node.get("native_session_scope"))
                                    if not reopened:
                                        continue
                                pending_facts[str(task_id)] = {
                                    **node, "task_kind": node.get("task_kind")
                                    or ((current_state.get("tasks") or {}).get(task_id) or {}).get("task_kind")
                                    or "captain_next_plan"}
                                # P0-6:记录 reopen 时刻,ack 检查据此算 coordination_to_attempt_latency。
                                _reopen_at[str(task_id)] = time.time()
                            if pending_facts:
                                _persistent_team_state.ensure(
                                    str(plan.get("team_id") or qhash), original_question, pending_facts,
                                    {key: list(node.get("depends_on") or [])
                                     for key, node in pending_facts.items()},
                                    user_id=user_id, conversation_id=conversation_id,
                                    registered=set(_harness_keys()), sync_pending_only=True)
                        except Exception as exc:
                            if _wave_handoff or _author_proposal:
                                return incomplete_coordination(
                                    out, {**coordination, "ok": False,
                                          "error": "coordination_handoff_persist_failed",
                                          "runtime_error": type(exc).__name__}, rounds)
                        # P0-5 入图前统一强绑定:坏 owner(未注册/回退task_id)自动重绑,
                        # 无候选明确 blocked 拒绝持久化为 pending 死节点。
                        _rebind_summary = _rebind_or_block_unbound_tasks(
                            str(plan.get("team_id") or qhash), plan)
                        if _rebind_summary.get("rebound") or _rebind_summary.get("blocked"):
                            if cb:
                                cb({"agent": "commander", "t": "reason",
                                    "x": f"🔁 强绑定:重绑{len(_rebind_summary.get('rebound') or [])}个节点/"
                                         f"blocked{len(_rebind_summary.get('blocked') or [])}个无候选节点"})
                        if _wave_handoff:
                            try:
                                import project_context as _handoff_context
                                _handoff_context.initialize(
                                    str(plan.get("team_id") or qhash), original_question, plan,
                                    mode=mode, user_id=user_id, conversation_id=conversation_id)
                            except Exception as exc:
                                return incomplete_coordination(
                                    out, {**coordination, "ok": False,
                                          "error": "coordination_handoff_context_failed",
                                          "runtime_error": type(exc).__name__}, rounds)
                            out.pop("_coordination_handoff", None)
                            if _paused:
                                # A held branch suppresses new control calls, not
                                # a proposal already returned at the joined wave.
                                # Re-evaluate runnable work against that new graph;
                                # the old graph can have no independent successor.
                                future_tasks = plan.get("tasks") or {}
                                future_dependencies = {
                                    key: list(node.get("depends_on") or [])
                                    for key, node in future_tasks.items()}
                                future_policies = {
                                    key: node.get("dependency_policy", _DEFAULT_DEP_POLICY)
                                    for key, node in future_tasks.items()}
                                _provider_paused = _provider_backoff_tasks(
                                    _request_results, future_dependencies, future_policies)
                                _terminal_held = paused_execution_tasks(
                                    _request_results, future_dependencies, future_policies,
                                    pause=lambda row: bool(
                                        (observation := _native_terminal_message_absent(row))
                                        and not observation["checkpoint_saved"]),
                                    delivered=_is_delivered, dependency_check=_deps_satisfied)
                                _paused = _provider_paused | _terminal_held
                                _open_independent = {
                                    key for key in future_tasks if key not in _paused
                                    and (key in reopen_ids
                                         or not _is_delivered(_request_results.get(key)))}
                                for key in reopen_ids - _paused:
                                    _retained_results.pop(key, None)
                    # 锚点与复验观察已进入首脑上下文；是否返工、返工哪些维度
                    # 由它的 task_updates / next_plan 表达，不再由平台追加业务任务。
                elif coordination.get("action") in {"blocked", "user_decision"}:
                    if _author_proposal:
                        _author_context.record_coordination(
                            str(round_plan.get("team_id") or qhash), _author_owner, coordination)
                    out["persistent_rounds"] = rounds
                    out["pilot"] = {"round": rounds, "action": "stop",
                                    "kind": "captain_" + str(coordination.get("action"))}
                    return out
                elif (coordination.get("action") == "complete" and not objective_complete
                      and not _author_proposal):
                    coordination.update({
                        "ok": False, "action": "continue",
                        "error": "captain_complete_conflicts_with_objective_outcome",
                        "reason": (str(coordination.get("reason") or "")
                                   + "；客观交付或复验仍未闭合，需继续调整任务"),
                    })
        if _author_proposal:
            try:
                _author_receipt_team = str(round_plan.get("team_id") or qhash)
                _messages = list(coordination.get("messages") or [])
                def _check_author_message_owner(state):
                    _require_mail_context_owner(state, _author_receipt_team, user_id,
                                               conversation_id or qhash, bound_team=True)
                    node = (state.get("tasks") or {}).get(_author_task_id) or {}
                    expected_owner = str(((plan.get("tasks") or {}).get(_author_task_id) or {}).get(
                        "assigned_harness") or _author_owner)
                    if (node.get("assigned_harness") != expected_owner
                            or node.get("attempt_seq") != _author_node.get("attempt_seq")
                            or node.get("active_attempt") is not None
                            or node.get("queue_owner") or node.get("lease_owner")
                            or (not node.get("finished_at") and _author_task_id not in
                                set(plan.get("_coordination_reopen_task_ids") or []))):
                        raise PermissionError("author_proposal_attempt_changed")
                delivered = _author_team.send_messages(
                    _author_receipt_team, _author_owner, _messages,
                    owner_check=_check_author_message_owner) if _messages else []
                coordination["message_receipts"] = [{
                    "message_id": message.get("id"), "to": message.get("to"),
                    "correlation_id": (coordination.get("collaboration_protocol") or {}).get("correlation_id"),
                    "meaning": "persisted input; adoption unverified",
                } for message in delivered]
            except Exception as exc:
                coordination.setdefault("protocol_feedback", {})["message_delivery_error"] = type(exc).__name__
                coordination.update(ok=False, error="author_message_delivery_failed")
            _author_context.record_coordination(
                str(round_plan.get("team_id") or qhash), _author_owner, coordination)
            if not coordination.get("ok"):
                return incomplete_coordination(out, coordination, rounds)
        if coordination_history:
            out["coordination_history"] = list(coordination_history)
            out.setdefault("coordination", coordination_history[-1])
        # per-key 轨迹累积:记下每家自己这轮的实质产出(下轮回流给它自己·接力自己的活)。
        for r in (out.get("results") or []):
            _k = r.get("key") or r.get("harness")
            _f = (r.get("final") or "").strip()
            if _k and _f:
                per_key_traj[_k] = _f[:1500]
        # 下一轮只执行开放节点。已完成的前置节点以私有只读结果回放，保留依赖边和完整输入，
        # 不重复执行副作用。显式 coordination reopen 的节点仍进入开放集。
        _round_results = {str(r.get("task_id") or r.get("key") or ""): r
                          for r in (out.get("results") or [])
                          if not r.get("supporting_context")}
        # Consume the same self-continuation contract as server single. This
        # reopens only this member's existing task; it cannot change a team plan
        # or overrule an explicitly applied captain decision.
        _native_requests = {}
        if ((len(_round_results) == 1 and not coordination.get("ok"))
                or _author_continue_without_graph):
            for _task_id, _row in _round_results.items():
                if _author_continue_without_graph and _task_id != _author_task_id:
                    continue
                _intent = _pilot_run_state(_row).get("native_continuation")
                if _task_id and _intent:
                    if _row.get("error_envelope") or _row.get("fail_kind"):
                        _runtime_error = normalize_execution_error(
                            str(_row.get("status") or "error"),
                            error=str(_row.get("error") or ""),
                            fail_kind=str(_row.get("fail_kind") or ""),
                            supplied=_row.get("error_envelope"))
                        if not _runtime_error.retryable:
                            retain_request_results(out)
                            out["persistent_rounds"] = rounds
                            out["pilot"] = {
                                "round": rounds, "action": "stop",
                                "kind": "native_continuation_runtime_stop",
                                "reason": "原运行结果不可自动重试；保留结果和现场，未重开原任务。"}
                            return out
                    _native_requests[_task_id] = _intent
        _coordination_reopen = {str(value) for value in
                                (plan.get("_coordination_reopen_task_ids", []) or [])}
        _terminal_resume = {
            task_id for task_id, observation in _terminal_messages.items()
            if observation["checkpoint_saved"] and task_id not in _paused
            and task_id in (plan.get("tasks") or {})}
        for _task_id in _terminal_resume:
            _node = plan["tasks"][_task_id]
            _node.pop("idempotency_key", None)
            _node.pop("input_hash", None)
        _unfinished = {task_id for task_id, row in _round_results.items()
                       if task_id and not _is_delivered(row)} | _coordination_reopen | set(_native_requests)
        if _wave_handoff:
            # These have never run: neither failed results nor disposable nodes.
            _unfinished.update(_wave_handoff["pending_task_ids"])
        if _unfinished and not _paused:
            _tasks = {}
            for _task_id, _node in (plan.get("tasks") or {}).items():
                if str(_task_id) not in _unfinished:
                    continue
                _node = dict(_node) if isinstance(_node, dict) else {"task": str(_node)}
                _node["depends_on"] = list(
                    (_node.get("depends_on") or []) if str(_task_id) in _coordination_reopen
                    else _request_dependencies.get(str(_task_id), _node.get("depends_on") or []))
                if str(_task_id) in _coordination_reopen or str(_task_id) in _native_requests:
                    _node.pop("idempotency_key", None)
                    _node.pop("input_hash", None)
                if (not _node.get("assignment_locked")
                        and str(_task_id) not in _native_requests
                        and str(_task_id) not in _terminal_messages
                        and not (_wave_handoff and _task_id in _wave_handoff["pending_task_ids"])):
                    for _assignment_key in ("assigned_harness", "assign_to", "harness", "agent"):
                        _node.pop(_assignment_key, None)
                    _node["preferred_harnesses"] = []
                    _node["rebind_reason"] = "previous_assignment_did_not_deliver"
                _tasks[str(_task_id)] = _node
            _retained_results = {}
            plan["tasks"] = _tasks
        elif not _paused:
            plan["tasks"] = {}
        plan.pop("_coordination_reopen_task_ids", None)
        # pilot 判断:该继续还是可以停(带丰富 run_state 事实·彻底让 pilot/模型自己判断·不替它定规则)。
        # 军团 goal"彻底让模型和pilot判断是否持续":给它更全的现场事实——不只"有没有交付"这一个布尔,
        # 还给【产出了几个真文件·交付/总家数·失败家的分类分布】,让它据此判断是收尾还是该继续/换招。
        # 这是 context not control:给事实(delivered/artifacts/fail_kinds)让 pilot 自己判·不写死续跑规则。
        _res = out.get("results") or []
        _art_count = sum(len(r.get("artifacts") or []) + len(r.get("discovered_artifacts") or [])
                         for r in _res)
        _fail_kinds = [r.get("fail_kind") for r in _res
                       if r.get("status") not in ("done",) and r.get("fail_kind")]
        delivered_count = int(out.get("delivered_count", 0) or 0)
        total = int(out.get("total", len(_res)) or len(_res))
        # 保留既有计划的返回条件，不把返回统计升格成用户目标完成。
        # 同题竞争有一个可交回结果即可；不同节点仍须全部返回。
        # 这里不改队形、开放节点或自动续作策略。
        _round_tasks_for_scope = round_plan.get("tasks") or {}
        _task_texts = ({str(v.get("task") if isinstance(v, dict) else v)
                        for v in _round_tasks_for_scope.values()}
                       if _round_tasks_for_scope else set())
        if len(_task_texts) > 1:
            delivery_return_ready = bool(
                total and delivered_count == total and not _fail_kinds
                and not out.get("all_failed"))
        else:
            delivery_return_ready = bool(delivered_count) and not out.get("all_failed")
        scope_goal_met = None if delivery_return_ready else False
        scope_goal_met_source = None if delivery_return_ready else "runtime_open_conditions"
        _verification_open = bool(out.get("verification_incomplete") or
                                  (out.get("outcome") or {}).get("verification_incomplete"))
        _contract_open = bool(out.get("contract_incomplete") or
                              (out.get("outcome") or {}).get("contract_incomplete"))
        _captain_requests_continue = bool(
            coordination.get("action") == "continue" and
            (coordination.get("ok") or coordination.get("error") in {
                "captain_complete_conflicts_with_objective_outcome",
                "captain_continue_without_plan", "captain_continue_without_applicable_plan"}))
        _captain_confirms_complete = bool(coordination.get("ok") and
                                          coordination.get("action") == "complete")
        if _verification_open or _contract_open or _captain_requests_continue:
            delivery_return_ready = False
            scope_goal_met = False
            scope_goal_met_source = (
                "captain_coordination" if _captain_requests_continue else "runtime_open_conditions")
        elif coordination.get("ok"):
            delivery_return_ready = delivery_return_ready and _captain_confirms_complete
            scope_goal_met = delivery_return_ready and _captain_confirms_complete
            scope_goal_met_source = (
                "captain_coordination" if scope_goal_met else "runtime_open_conditions")
        # P1-4 恢复循环进展预算(治goal18·20次resume高活动低产出):连续 resume 无新增
        # artifact hash/gap关闭/合法 owner 变化,2 轮后强制 root-cause/rebind 而非继续
        # claim/release——不是直接停(那是掩盖),是换路径;再无进展由 max_rounds 收口。
        _fp_packet = round_plan.get("project_context") or {}
        _fp_artifacts = frozenset(
            (str(row.get("path") or ""), str(row.get("sha256") or ""))
            for row in (_fp_packet.get("artifacts") or []) if isinstance(row, dict))
        _fp_open_gaps = frozenset(
            str(row.get("dimension") or row.get("gap") or "")
            for row in (_fp_packet.get("gap_plan") or [])
            if isinstance(row, dict)
            and str(row.get("status") or "open").lower() in {"open", "improved", "inconclusive"})
        _fp_owners = frozenset(
            (str(row.get("task_id") or row.get("key") or ""),
             str(row.get("assigned_harness") or row.get("harness") or ""))
            for row in (out.get("results") or []))
        _progress_fp = (_fp_artifacts, _fp_open_gaps, _fp_owners)
        if _prev_progress_fp is not None and rounds > 1:
            _no_new_artifact = _fp_artifacts <= _prev_progress_fp[0]
            _no_gap_closed = _fp_open_gaps >= _prev_progress_fp[1]
            _no_owner_change = (not _fp_owners) or _fp_owners == _prev_progress_fp[2]
            if _no_new_artifact and _no_gap_closed and _no_owner_change:
                _stagnant_rounds += 1
            else:
                _stagnant_rounds = 0
        _prev_progress_fp = _progress_fp
        out["progress_stagnant_rounds"] = _stagnant_rounds
        if (_stagnant_rounds >= 2 and _captain_requests_continue
                and coordination.get("source") != "native_author_proposal"):
            for _stg_id, _stg_node in (plan.get("tasks") or {}).items():
                if (isinstance(_stg_node, dict) and not _stg_node.get("assignment_locked")
                        and str(_stg_id) not in _terminal_messages):
                    for _stg_field in ("assigned_harness", "assign_to", "harness", "agent"):
                        _stg_node.pop(_stg_field, None)
                    _stg_node["rebind_reason"] = "progress_budget_exhausted"
            _stagnant_note = (f"【恢复循环进展预算触发】连续{_stagnant_rounds}轮无新增产物hash/"
                              f"无gap关闭/无owner变化——必须换方法：换工具或换参照、缩小切口，"
                              "或如实写 blocked 与根因；全部未锁定节点已换owner重派，禁止原样重试")
            if cb:
                cb({"agent": "commander", "t": "reason", "x": f"⛔ {_stagnant_note}"})
        run_state = {
            "delivered": delivered_count > 0,
            "mutation_seen": delivered_count > 0,
            "delivered_count": delivered_count,
            "total": total,
            "artifact_count": _art_count,          # 产出的真文件数(有丰富产物 vs 只有文字·pilot 据此判够不够)
            "fail_kinds": _fail_kinds,             # 失败家的分类(idle_stall/gateway…·pilot 判该不该换招续跑)
            "all_failed": bool(out.get("all_failed")),
            "delivery_return_ready": delivery_return_ready,
            "scope_goal_met": scope_goal_met,
            "scope_goal_met_source": scope_goal_met_source,
            "scope_progressed": bool(delivered_count or _art_count),
            "verification_incomplete": _verification_open,
            "contract_incomplete": _contract_open,
            "open_gaps": list((round_plan.get("project_context") or {}).get("open_gaps") or []),
            "stagnant_rounds": _stagnant_rounds,
            "reference_facts": _quality_facts.benchmark_snapshot(round_plan),
            "benchmark_ref_count": _quality_facts.benchmark_snapshot(round_plan)["reference_count"],
            "captain_coordination": coordination,
            "goal": original_question,
            # 世界对标引用(2026-09-08用户钉死'对标要具体链接/图不是模糊描述'+'模型满足后自己再看对标'):
            # 从研究锚点表提取具名参照URL喂给pilot·收口时对照自证·context loop 不 control。
            "benchmark_refs": _collect_benchmark_refs(out, round_plan),
        }
        if _native_requests:
            run_state["scope_goal_met"] = False
            run_state["scope_goal_met_source"] = "orchestration_request.coordinate"
            run_state["native_continuation"] = next(iter(_native_requests.values()))
            out["native_continuation"] = dict(_native_requests)
        if _terminal_messages:
            run_state["native_terminal_message_absent"] = dict(_terminal_messages)
            out["native_terminal_message_absent"] = dict(_terminal_messages)
        verdict = ({"action": "continue", "kind": "captain_continue",
                    "next_action": str(coordination.get("reason") or "首脑已基于最新现场生成下一轮任务")}
                   if _captain_requests_continue else _lp.evaluate(final, run_state, user_id))
        if (verdict.get("action") == "continue" and _terminal_held
                and not _open_independent):
            verdict = {"action": "stop", "kind": "native_terminal_message_unrecoverable",
                       "reason": "本轮缺少最终正文，且没有可靠的原生恢复游标；保留原任务、依赖和产物，不另起作者。"}
        out["pilot"] = {"round": rounds, "action": verdict.get("action"),
                        "kind": verdict.get("kind"), "reason": verdict.get("reason")}
        # 每轮结构化诊断：只记客观事实（交付数/产物数/世界参照/停滞轮数/决定），
        # 供下一轮上下文、收尾结果与人工复盘使用；不做质量裁判。
        persistent_diagnostics.append({
            "round": rounds,
            "delivered_count": delivered_count,
            "total": total,
            "artifact_count": _art_count,
            "open_gaps": len(run_state.get("open_gaps") or []),
            "stagnant_rounds": _stagnant_rounds,
            "references_recorded": run_state["benchmark_ref_count"] > 0,
            "reference_analysis_count": len(run_state["reference_facts"]["analysis"]),
            "benchmark_ref_count": run_state["benchmark_ref_count"],
            "verification_open": _verification_open,
            "contract_open": _contract_open,
            "delivery_return_ready": run_state["delivery_return_ready"],
            "scope_goal_met": run_state["scope_goal_met"],
            "scope_goal_met_source": run_state["scope_goal_met_source"],
            "native_continuation_requested": bool(_native_requests),
            "native_terminal_message_absent": sorted(_terminal_messages),
            "action": verdict.get("action"),
            "kind": verdict.get("kind"),
        })
        out["persistent_diagnostics"] = list(persistent_diagnostics)
        if verdict.get("action") != "continue":
            # A peer is a saved contribution, not another main result or a vanished prior call.
            supporting = [row for row in _request_results.values()
                          if row.get("supporting_context") and row.get("peer_for")]
            if supporting:
                present = {str(row.get("task_id") or row.get("key") or "")
                           for row in out.get("results") or []}
                out["results"] = [*(out.get("results") or []), *(
                    row for row in supporting if str(row.get("task_id")) not in present)]
            # 本轮返回、声明完成或既有运行停因均可收尾，不等于独立质量验收。
            out["persistent_rounds"] = rounds
            out["persistent_diagnostics"] = list(persistent_diagnostics)
            out["autocommit"] = _autocommit_delivery(
                round_plan, str(plan.get("team_id") or qhash), rounds, out)
            return out
        # 短摘要只用于上下文预算，不能代表完整结果相同。即使完整结果重复，
        # 也不得盖过上方已记录的产物/gap/owner变化；这不是对作品品质的判断。
        if (same_full_final and _stagnant_rounds > 0
                and not _captain_requests_continue and not _terminal_resume):
            out["persistent_rounds"] = rounds
            out["persistent_stalled"] = True
            if cb:
                cb({"agent": "commander", "t": "reason",
                    "x": f"续跑第 {rounds} 轮和上轮产出一致(无新进展)→ 提前收尾交付当前成果(不空转)"})
            return out
        _native_reopen = set(_native_requests) | _terminal_resume
        if _native_reopen and rounds < max_rounds:
            try:
                import team_state as _native_team_state
                _native_team_id = str(plan.get("team_id") or qhash)
                _native_tasks = (_native_team_state.load(_native_team_id) or {}).get("tasks") or {}
                for _task_id in sorted(_native_reopen):
                    if _task_id not in _native_tasks:
                        continue  # Older stateless callers have no persisted node to reopen.
                    _node = plan["tasks"][_task_id]
                    if not _native_team_state.reopen_task(
                            _native_team_id, _task_id, text=str(_node.get("task") or ""),
                            dependencies=list(_node.get("depends_on") or []),
                            agent=str(_request_results[_task_id].get("assigned_harness")
                                      or _request_results[_task_id].get("harness")
                                      or _request_results[_task_id].get("key") or ""),
                            reason=("native_terminal_message_absent"
                                    if _task_id in _terminal_resume else "native_member_continue")):
                        raise RuntimeError("native_task_reopen_not_applied")
                    _retained_results.pop(_task_id, None)
                    _reopen_at[_task_id] = time.time()
            except Exception as exc:
                out["continuation_error"] = "native_task_reopen:" + type(exc).__name__
                out["persistent_rounds"] = rounds
                out["pilot"] = {"action": "stop", "kind": "continuation_runtime_error",
                                "reason": "已有续作决定，但原任务重开未生效；保留原结果和现场。"}
                return out
        # continue:把 pilot 给的具体纠正动作也带进下一轮 question(让军团看到"哪没达成")
        nxt = verdict.get("next_action") or "上一轮还没真达成目标·请继续推进到完成"
        if _native_requests:
            nxt += "\n【原成员报告的未完事项】" + "\n".join(
                str(intent.get("reason") or "原任务尚未完成") for intent in _native_requests.values())
        if _terminal_messages:
            nxt += _native_terminal_message_context(_terminal_messages)
        if coordination.get("error") in {
                "captain_continue_without_plan", "captain_continue_without_applicable_plan"}:
            # 沿现有续跑预算/规划入口反馈真实协议错误，不另起重试循环，
            # 不把上一轮已完成任务重跑来“修”一份根本未应用的提案。
            failed_proposal = {
                "error": coordination["error"],
                "protocol_feedback": coordination.get("protocol_feedback") or {},
                "task_updates": coordination.get("task_updates") or {},
                "next_plan": coordination.get("next_plan") or {},
            }
            encoded_feedback = json.dumps(failed_proposal, ensure_ascii=False)
            if len(encoded_feedback) > 6000:
                encoded_feedback = json.dumps({
                    "error": coordination["error"], "details_omitted": True,
                    "decision_ref": {
                        "path": str((round_plan.get("project_context") or {}).get("packet_path") or ""),
                        "team_id": str(plan.get("team_id") or qhash), "field": "decisions",
                        "captain": coordination.get("captain"), "sequence": coordination.get("sequence")},
                }, ensure_ascii=False)
            nxt += "\n【上一首脑提案未生效；请基于原提案纠正结构，不另造需求】" + encoded_feedback
        if _ack_root_cause:
            nxt = f"{nxt}；{_ack_root_cause}"
        if _stagnant_note:
            nxt = f"{nxt}；{_stagnant_note}"
        plan["conversation_context"] = (round_plan.get("conversation_context") or "")
        next_guidance = str(nxt)
        if not plan.get("tasks") and rounds < max_rounds:
            try:
                plan, continuation_error = _plan_empty_continuation(
                    original_question, plan, round_plan, str(plan.get("team_id") or qhash), next_guidance,
                    user_id=user_id, conversation_id=conversation_id,
                    cancel_check=lambda: is_cancelled(qhash),
                    _provider_results=request_provider_facts,
                    _result_observer=lambda proposed: out.update(continuation_planning=proposed))
            except Exception as exc:
                continuation_error = "continuation_plan:" + type(exc).__name__
            _planning = out.get("continuation_planning") or {}
            if continuation_error:
                # Preserve this auxiliary call's typed runtime facts in the
                # existing result, not in a second scheduler or credential store.
                for field in ("error_envelope", "native_facts", "blocked_before_native", "execution_calls"):
                    if field in _planning:
                        out[field] = deepcopy(_planning[field])
            if continuation_error == "planner_cancelled" or is_cancelled(qhash):
                out["cancelled"] = True
                retain_request_results(out)
                out["status"] = "cancelled"
                out["persistent_rounds"] = rounds
                out["pilot"] = {"round": rounds, "action": "stop", "kind": "user_cancelled"}
                return out
            if continuation_error:
                retain_request_results(out)
                out["continuation_error"] = continuation_error
                out["persistent_rounds"] = rounds
                out["status"] = ("blocked" if requires_execution_pause(_planning) else
                                 "partial" if out.get("delivered_count") else "error")
                out["outcome"].update(
                    status="partial" if out.get("delivered_count") else
                           "blocked" if out["status"] == "blocked" else "failed",
                    failure_stage="planning", planner_error=continuation_error)
                out["pilot"] = {**verdict, "action": "stop",
                    "kind": ("request_budget_exhausted" if requires_request_budget_pause(_planning) else
                             "provider_authorization_required" if
                             is_provider_authorization_denial(_planning.get("error_envelope")) else
                             "native_execution_unconfirmed" if requires_native_settlement(_planning) else
                             "continuation_planning_failed")}
                return out
    # 达轮数上限:如实标记(不假装完成·不无限跑)
    if last_out is not None:
        # 终态口径修复（2026-09-22）：末轮是纯协调轮（results 无交付）而前轮有真实成果时，
        # 用最近有交付的 best_out 作为收尾基底，再合并末轮的协调事实——成品在盘就不报 failed 0/0。
        _last_delivered = any(str(r.get("status")) in ("done", "partial")
                              for r in (last_out.get("results") or []))
        if not _last_delivered and best_out is not None:
            _merged = {**best_out}
            _merged.update({
                k: v for k, v in last_out.items()
                if k in ("coordination", "coordination_acks", "coordination_history",
                         "persistent_rounds", "pilot", "continuation_error", "note")})
            _merged["persistent_rounds"] = rounds
            _merged["persistent_capped"] = True
            _merged["persistent_capped_merged_from_earlier_round"] = True
            _merged["persistent_diagnostics"] = list(persistent_diagnostics)
            _merged["autocommit"] = {"status": "skipped", "reason": "retained_earlier_round"}
            if cb:
                cb({"agent": "commander", "t": "reason",
                    "x": f"已连续自主推进 {rounds} 轮达上限·按最近一轮真实交付收尾(末轮为协调轮·成果已合并)"})
            return _merged
        last_out["persistent_rounds"] = rounds
        last_out["persistent_capped"] = True
        last_out["persistent_diagnostics"] = list(persistent_diagnostics)
        last_out["autocommit"] = _autocommit_delivery(round_plan, str(plan.get("team_id") or qhash), rounds, last_out)
        if cb:
            cb({"agent": "commander", "t": "reason",
                "x": f"已连续自主推进 {rounds} 轮达上限·先收尾交付当前成果(可再派继续)"})
    return last_out or {"results": [], "error": "无结果"}


def _record_experience(question: str, ordered: list, user_id: str = ""):
    """把本次成功交付的家的经验落进军团跨家记忆(context not control:只记事实不评判)。

    军团独有价值:codex 这次答的·cc 下次 search 能看到。不碰各家自有记忆(见 legion-memory-harness-study.md)。

    ★ user_id 贯穿(军团自审 penguin/codex/deepseek 一致 P0·2026-08-28):
      底层 legion_memory.add(user_id=) 早已支持按用户隔离,但这个上层出口一直没传 user_id→
      每条军团经验都落成"无归属"(_visible_to 视作 default)→别的登录用户 search 时会召回到
      本用户的任务产出(跨用户记忆串·隐私风险)。修:把 dispatch 已确立的 user_id 一路传到这里,
      经验按 user_id 归属(mtype="long":对本人跨家共享·对别人不可见)。这不是加门,是把"这条经验
      是谁的"这个真实事实如实记上——底层隔离机制才真正生效。
    """
    try:
        import legion_memory
    except Exception:
        return
    for r in ordered:
        if r.get("status") == "done" and (r.get("final") or "").strip():
            try:
                legion_memory.add(question, r["final"], source=r.get("key", "legion"),
                                  user_id=user_id or "")
            except Exception:
                pass
            # 品味沉淀(2026-09-08用户钉死'沉淀快速落地·不断提升世界顶级品味理解'):
            # 研究/翻译官节点产出的锚点表文件【路径+来源】也入经验——下次相似任务召回时
            # 军团能看到“上次这个品类的世界对标研究在哪·直接站在上次品味上·不用从零再搜”。
            try:
                _node_kind = str(r.get("task_kind") or "")
                if _node_kind in ("frontier_research", "translate_research"):
                    _arts = [*(r.get("artifacts") or []), *(r.get("discovered_artifacts") or [])]
                    for _a in _arts:
                        _p = str((_a or {}).get("path") or "")
                        if _p:
                            legion_memory.add(
                                f"世界对标研究沉淀:{question[:60]}",
                                f"研究/翻译官锚点表落盘于 {_p}（含具体对标链接与参数配方）",
                                source=str(r.get("key") or "legion"),
                                user_id=user_id or "")
            except Exception:
                pass


def _recall_experience(question: str, cb=None, user_id: str = "") -> str:
    """召回军团历史上相似问题的经验·作为只读上下文注入(context not control·标明供参考)。

    仿 hermes RecallStatus:透明告知捞到什么·模型自己判断用不用·没相关就不注入(省上下文)。
    cb 非空时·emit 一条【用户可见】过程事件:让用户看到"军团在变聪明——参考了历史上别家答过的
    相似问题"(用户中心·过程透明可感·学习飞轮对用户的可感知价值·不是隐形后端优化)。
    """
    try:
        import legion_memory
        # user_id 隔离召回(军团自审 P0):只捞本用户归属的经验+全局共享经验(working/procedural),
        # 不会把别的用户的任务产出召回给当前用户(_visible_to 按 user_id 过滤)。
        r = legion_memory.search(question, top=2, user_id=user_id or "")
    except Exception:
        return ""
    exps = r.get("experiences") if isinstance(r, dict) else None
    if not exps:
        return ""
    # 用户可见:如实陈述【发生的事实】——给军团注入了 N 条历史经验供参考。★ context not control:
    # 不替军团断言"参考了/辅助了本次回答"(用没用是军团的事·只有它自己知道·父层绝不替它宣称,
    # 那正是 adopted 要军团自己回报的原因)。只报"注入了什么·供参考",效果由军团自己决定。
    if cb:
        _froms = "、".join(sorted({str(e.get("from") or "军团") for e in exps}))
        try:
            cb({"t": "reason", "x": f"💡 找到 {len(exps)} 条军团历史经验({_froms} 答过相似问题)·已作参考交给它自己判断"})
        except Exception:
            pass
    lines = [f"- [{e.get('from')}答过; entry_id={e.get('entry_id') or '未持久化'}; "
             f"adoptable={bool(e.get('adoptable'))}] Q:{e['q'][:80]} → A:{e['a'][:200]}"
             for e in exps]
    # 学习飞轮闭环(用户 goal"通过学习优化上下文"):召回只是一半·还得知道哪条真有用,好经验才能
    # 浮上来(mark_adopted 提置信度·prune 淘汰没人采纳的)。只有 harness 自己知道用没用·父层绝不猜——
    # 给它真回报入口(context not control),用了就自己报。学 legion_memory 双票之"采纳票"。
    _tool = os.path.join(HERE, "legion_tool.py")
    adopt_hint = (
        "\n【若真采用了 adoptable=True 的经验，可按确切 entry_id 回报；未采用就不回报】\n"
        f"  python3.12 {_tool!r} adopted \"<本次召回中的entry_id>\""
        if any(e.get("adoptable") for e in exps) else "")
    return ("\n\n【军团历史经验召回·相似问题别家/自己答过·仅供参考不一定对·自己判断】\n"
            + "\n".join(lines) + adopt_hint)


_AIGC_READY_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_AIGC_READY_TTL = 90.0


def _probe_appid_only() -> dict:
    """只探 appid(env + local models.json fallback)·秒级·无 CLI 探活。

    appid 探测和慢的 21 家 CLI 探活分开:appid 是秒级的·占位分支也该做全(保持 health 契约真实·
    区分 deploy secret vs local fallback)·只有慢的 runnable_clis 才延后到后台填。
    """
    out = {"appid_configured": False, "appid_source": None, "appid_deployable": False,
           "gateway": os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"}
    for _name in ("MEYO_UNIFIED_APPID", "AIGC_APPID"):
        if os.environ.get(_name):
            out["appid_configured"] = True
            out["appid_deployable"] = True
            out["appid_source"] = f"env:{_name}"
            return out
    try:
        d = _load_model_router()
        ap = d["programming"]["candidates"][0]["appids"][0]
        out["appid_configured"] = bool(ap)
        out["appid_source"] = ("operator_model_catalog" if "MEYO_MODEL_CATALOG_PATH" in os.environ
                               else "local_models_json") if ap else None
    except Exception as e:
        out["appid_error"] = type(e).__name__
    return out


def _aigc_ready(force: bool = False, cached_only: bool = False) -> dict:
    """探测军团出答案的真实依赖:AIGC 网关配置(appid) + CLI 家是否在。

    真相(2026-08-27 实测):所有家(codex/cc/hermes…)出答案都要经美团 AIGC 网关
    (aigc.sankuai.com,由 comparison/responses_chat_bridge 本地代理转发,appid 认证)。
    appid 来自环境变量 AIGC_APPID,或 ~/.claude/skills/aigc-model-router/models.json。
    这是上线的硬依赖——探测它,别让服务在没网关时静默失败。

    探 CLI 家可用性要 shutil.which + external_agent probe 子进程(单家最长 45s),
    /api/legion/harnesses、/captain 都靠它→前端首屏等 9-12s。runnable 集合在一个 session
    内稳定,加 90s TTL 缓存(同 install_registry/_CAP_CACHE 模式);装/卸后经 invalidate 失效。
    """
    if not force and _AIGC_READY_CACHE["data"]:
        _age = time.time() - _AIGC_READY_CACHE["ts"]
        if _age < _AIGC_READY_TTL:
            return {**_AIGC_READY_CACHE["data"], "from_cache": True}
        # SWR:过期但有旧值→立即返回旧值让派活不等 2.85s 探活·后台异步 force 重填。
        _swr_bg_refresh("aigc_ready", lambda: _aigc_ready(force=True))
        return {**_AIGC_READY_CACHE["data"], "from_cache": True, "stale": True}
    # cached_only(用户 2026-09-01 loop 揪出残留盲区):进程刚起、预热线程还没填完时,完全无旧值→
    # SWR 走不到(没旧值可返)→原会同步付 24s 冷探活。/health 端点(前端"新开页面"首屏打·curl 探针
    # 打)若走这条会卡 24s。cached_only=True 时:无缓存不同步探活·触发后台填充·立即返回 warming 占位
    # (端口秒连+首屏不卡·真实 runnable 集合等后台填完下次请求就有)。派活路径不传 cached_only·仍会
    # 首次同步填一次(它需要真实 runnable 才能选家·但那时预热多半已填好·撞不到)。
    if not force and cached_only:
        _swr_bg_refresh("aigc_ready", lambda: _aigc_ready(force=True))
        appid_ok = bool(os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID"))
        return {"appid_configured": appid_ok, "appid_source": ("env" if appid_ok else None),
                "appid_deployable": appid_ok, "clis": {}, "runnable_clis": [],
                "gateway": os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native",
                "warming": True}
    # 首次冷填:single-flight——并发撞冷时只第一个真探活(external probe 子进程贵·惊群压垮)。
    # ★并发拖累根治(用户 2026-09-01 loop·同 harness_keys 那处):拿不到锁的线程【不阻塞等】
    # (旧版 with _singleflight_lock 会等第一个探活完 23s→N家选家全串行)。改 acquire(blocking=
    # False):抢到锁的同步探活填缓存;抢不到的立即返回【全集 runnable 占位】(全部注册家当可跑·
    # 秒回不探活)+后台填真值。选家用全集占位够(真不可跑的家 run 时失败→补位接力兜底)。
    if not force:
        # 缓存新鲜直接返(快路径)
        if _AIGC_READY_CACHE["data"] and (time.time() - _AIGC_READY_CACHE["ts"] < _AIGC_READY_TTL):
            return {**_AIGC_READY_CACHE["data"], "from_cache": True}
        # 冷/过期:任何线程都【不在热路径同步探活】——触发一次后台 force 填(single-flight 由
        # _swr_bg_refresh 自己去重·只起一个后台任务)·自己立即返回占位(有旧值用旧值·否则全集占位)。
        # 派活热路径永不付 23s 探测成本(用户 2026-09-01 loop:抢到锁的线程也不该阻塞)。
        _swr_bg_refresh("aigc_ready", lambda: _aigc_ready(force=True))
        if _AIGC_READY_CACHE["data"]:
            return {**_AIGC_READY_CACHE["data"], "from_cache": True, "stale": True}
        _all: set[str] = set()
        try:
            import harness_registry as _hr2
            _all = set(_hr2._STANDALONE_ADAPTERS.keys())
        except Exception:
            _all = {"codex", "cc", "hermes", "penguin", "jcode", "deepseek", "prime", "deepagents", "omnigent"}
        try:
            import external_agent_registry as _er2
            # warming 占位同款过滤(2026-09-02 固化):runnable_clis 有 12+ 派单层消费点(captain 候选/need_peer/relay),占位把 capability 家标 True=契约污染。
            _all |= {str(key) for key in _answer_keys_from_catalog(_er2.load_catalog()) if key}
        except Exception:
            pass
        # appid 探测是秒级(读 env + local models.json)·占位分支也做全·保持 health 契约真实
        # (只有慢的 runnable_clis 用占位·appid 字段不延后)。
        _ap = _probe_appid_only()
        _ap.update({"clis": {k: True for k in _all}, "runnable_clis": sorted(_all), "warming": True})
        return _ap
    import shutil
    # appid 探测复用 _probe_appid_only(单一事实源·占位分支和真实探测一致)
    out = _probe_appid_only()
    out["clis"] = {}
    # 各家可跑性探测(根因修 2026-08-28:旧代码只写死6个CLI名·漏了 prime/deepagents/auto
    # 这3家其实有 adapter 且依赖都在→被误判"不在runnable_clis"→总指挥永远看不见它们·实测这3家
    # 真派单全 done)。改为覆盖全部已注册 adapter·每家按【自己真实的依赖】判定·不写死不猜:
    #   CLI 家 = which 到可执行;Python 家 = 能 import 依赖;自研 auto = 引擎脚本在。
    _agent_dir = os.path.join(HERE, "..", "..", "seed-users-system", "qa_system", "agent")

    def _dep_ok(key: str) -> bool:
        import subprocess  # deepagents 探活需子进程查 python3.12(见下·对齐真跑解释器)
        try:
            if key in ("codex", "hermes"):
                return bool(shutil.which(key))
            if key == "cc":
                return bool(shutil.which("claude"))
            if key == "deepseek":
                return bool(shutil.which("dsh"))
            if key == "penguin":
                return bool(shutil.which("penguin") or os.path.exists(os.path.expanduser("~/.penguin/bin/penguin")))
            if key == "jcode":
                from external_adapters.jcode_native import executable
                return bool(executable())
            if key == "prime":
                # 旧 key 执行时已薄接官方 prime-intellect；探测也必须读同一入口。
                import external_agent_registry as _prime_registry
                return bool(_prime_registry.probe("prime-intellect").get("execution_ready"))
            if key == "deepagents":
                # 探活必须对齐真跑的解释器:adapter 真跑用 `python3.12 -c code`(harness_registry.py:1437)
                # ·依赖装在 python3.12。旧代码在 server 自己的解释器(可能是 3.14)里 find_spec
                # →3.14 没装 langchain_openai→误判 deepagents 掉线·但它真跑其实能成(同 prime 探测查错名的病·
                # 探活判据必须和真跑路径一致)。改为探 python3.12 本身。
                py312 = shutil.which("python3.12")
                if not py312:
                    return False
                try:
                    _r = subprocess.run([py312, "-c", "import importlib.util,sys; "
                                         "sys.exit(0 if importlib.util.find_spec('langchain_openai') else 1)"],
                                        capture_output=True, timeout=8)
                    return _r.returncode == 0
                except Exception:
                    return False
            if key == "auto":
                return False  # auto已彻底剔除军团(2026-09-08用户钉死·不参与任何环节)
            if key == "omnigent":  # 编排放大器·有 CLI + backend adapter 即可跑(经它调子家出答案)
                return bool(shutil.which("omnigent"))
            # 外部成员由统一 registry 做 revision/build/credential/transport 探测；
            # 不能再按内置家名硬编码，否则虽已动态注册却永远进不了运行池。
            try:
                import external_agent_registry as _external
                row = _external.probe(key)
                if row.get("key") == key:
                    return bool(row.get("execution_ready"))
            except Exception:
                pass
            # 注:cua/supervision/herdr/orca 不是 harness(是能力后端)·已从 MEYO_HARNESSES 挪到
            # MEYO_CAPABILITIES·不进 runnable_clis 派单池;它们的可用性由 capability 层单独探测。
        except Exception:
            return False
        # ★彻底落实新家(用户 2026-09-01"丢个adapter就活"):走到这=没有专门探测分支命中·
        #   也不在 external catalog。若 key 是【已注册的内置 adapter】(代码里真注册的家)·放行——
        #   内置 adapter 的依赖由它自己运行时处理·不该因"_dep_ok 没为它写探测分支"就被剔出池
        #   (否则 dogfood 揪出的真缺口:新注册 adapter 探不到→取消双标后被误剔→丢个adapter不活)。
        #   外部未知家仍 return False(探不到=没装·诚实剔除)。
        try:
            import harness_registry as _hr_reg
            if key in getattr(_hr_reg, "_STANDALONE_ADAPTERS", {}):
                return True
        except Exception:
            pass
        return False

    # 探测全部已注册 adapter 的家(注册表是单一事实源·别再手写子集)
    try:
        adapter_keys = sorted(_harness_keys())
    except Exception:
        adapter_keys = ["codex", "cc", "hermes", "penguin", "jcode", "deepseek", "prime", "deepagents"]
    # 并发探测(速度底线·用户 2026-09-01 骂醒:21家串行探测 34s·首屏/缓存失效后第一单干等)。
    # 每家 _dep_ok 是独立的 which/probe 子进程·无共享状态·可安全并行→34s 压到最慢一家的耗时(~3s)。
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=min(len(adapter_keys) or 1, 24)) as _ex:
        _results = list(_ex.map(lambda k: (k, _dep_ok(k)), adapter_keys))
    for _k, _ok in _results:
        out["clis"][_k] = _ok
    out["runnable_clis"] = [k for k, v in out["clis"].items() if v]
    _AIGC_READY_CACHE["data"] = out
    _AIGC_READY_CACHE["ts"] = time.time()
    return out


def invalidate_aigc_ready_cache() -> None:
    """装/卸 harness 或外部 agent 后调——清 _aigc_ready 缓存,让 harnesses/captain 拿真实可用集。"""
    _AIGC_READY_CACHE["data"] = None
    _AIGC_READY_CACHE["ts"] = 0.0


# ── 工具体检:各家 harness CLI + 公用工具(搜索/浏览器/skill)就绪 + 缺的建议 ──
#   用户要"让 agent 搜清单、装缺的、调通验证、自己有个数"。这里如实报告每样装没装、
#   缺的给安装命令,不盲目装重依赖(dsh 1.5G Node/prime 本地无源码)——按需装。
_HARNESS_CLI = {
    "codex": "codex", "cc": "claude", "hermes": "hermes",
    "penguin": "penguin", "jcode": "jcode", "deepseek": "dsh", "prime": "prime-agent",
}
_HARNESS_INSTALL = {
    "deepseek": "npx @deepseek-ai/dsh  (Node 生态·较重·按需)",
    "prime": "使用 harness_onboard.py --key prime-intellect 准备已登记的官方运行依赖",
}
# 公用工具(军团共享):pip 包 → import 名
_SHARED_TOOLS = {
    "ddgs": "ddgs", "duckduckgo_search": "duckduckgo_search", "tavily-python": "tavily",
    "wikipedia": "wikipedia", "beautifulsoup4": "bs4", "lxml": "lxml",
    "httpx": "httpx", "aiohttp": "aiohttp", "playwright": "playwright",
    "browser-use": "browser_use", "python-dotenv": "dotenv", "requests": "requests",
}


def tool_inventory() -> dict:
    """工具体检:军团各家 CLI + 公用工具的就绪状态 + 缺的安装建议。"""
    import shutil
    import importlib.util
    harnesses = []
    for key, cli in _HARNESS_CLI.items():
        if key == "jcode":
            from external_adapters.jcode_native import executable
            cli = executable() or cli
        if key == "prime":
            try:
                import external_agent_registry as _prime_registry
                row = _prime_registry.probe("prime-intellect")
            except Exception:
                row = {}
            target = (row.get("probe") or {}).get("target")
            shown = " ".join(map(str, target)) if isinstance(target, list) else str(target or "prime-intellect")
            installed = bool(row.get("available") and row.get("revision_ok"))
            harnesses.append({"key": key, "cli": shown, "entry_key": "prime-intellect",
                              "installed": installed,
                              "install_hint": None if installed else _HARNESS_INSTALL[key]})
            continue
        ok = bool(shutil.which(cli))
        harnesses.append({"key": key, "cli": cli, "installed": ok,
                          "install_hint": None if ok else _HARNESS_INSTALL.get(key, f"安装 {cli}")})
    def _pkg_installed(mod: str) -> bool:
        # find_spec 对有复杂 __init__(lazy import/依赖自检)的包(如 browser_use)可能
        # 抛异常或误报 None。用 find_spec 为主 + 异常时退回 importlib.metadata 元数据存在性,
        # 两者都不真 import(避开重包的 import 副作用)。
        try:
            if importlib.util.find_spec(mod) is not None:
                return True
        except (ImportError, ValueError, ModuleNotFoundError):
            pass
        except Exception:
            pass
        # 退回:按 pip 分发名查元数据(mod→pkg 名不同的走 _SHARED_TOOLS 反查)
        try:
            import importlib.metadata as _md
            dist = {v: k for k, v in _SHARED_TOOLS.items()}.get(mod, mod)
            _md.version(dist)
            return True
        except Exception:
            return False

    tools = []
    for pkg, mod in _SHARED_TOOLS.items():
        ok = _pkg_installed(mod)
        tools.append({"pkg": pkg, "module": mod, "installed": ok,
                      "install_hint": None if ok else f"pip install {pkg}"})
    missing_h = [h["key"] for h in harnesses if not h["installed"]]
    missing_t = [t["pkg"] for t in tools if not t["installed"]]
    return {
        "harnesses": harnesses, "shared_tools": tools,
        "summary": {
            "harness_ready": sum(1 for h in harnesses if h["installed"]),
            "harness_total": len(harnesses),
            "tools_ready": sum(1 for t in tools if t["installed"]),
            "tools_total": len(tools),
            "missing_harnesses": missing_h,
            "missing_tools": missing_t,
            "all_shared_tools_ready": not missing_t,
        },
    }


_GATEWAY_URL = "https://aigc.sankuai.com"


# 军团语义方向 → aigc-model-router/models.json 的真实 key。
# 根因(2026-08-28 dogfood 实测):run_one 曾收到 research/vision/image 等别名,
# 而 models.json 只有 programming/vision_understanding/image_generation/
# video_generation/general_chat_high_coverage → d.get(别名) 全空 → 换模型自愈
# 拿到 None → 一撞就交白卷。这里统一归一,让任意语义方向都能命中真实候选。
# 注意:这只是【兜底归一】——正路是规划器模型读能力特征自己判方向(见 plan prompt·context
# not control)。此表仅在直接调 run_one 传了语义别名、或模型没给方向时用,把别名对到能力
# 对口的真实 key(深搜/推理类→programming会调工具·纯写作→general_chat),不是替模型硬分类。
_DIRECTION_ALIASES = {
    "programming": "programming", "code": "programming", "coding": "programming", "engineering": "programming",
    "vision": "vision_understanding", "vision_understanding": "vision_understanding",
    "image_understanding": "vision_understanding", "ocr": "vision_understanding",
    # 2026-09-02 24query验收根修:image/video 方向在 models.json 里的候选是【文生图/视频模型】·
    # 而 select_usable_model 的全部消费方(run_one 尝试/下放fallback/状态兜底)都是给 chat 型
    # harness 选【对话模型】——把 gpt-image-2 当 chat 模型必 HTTP 400 白烧一轮(hermes×gpt-image-2
    # bad:5·attempt1 源头是 scene 池、重试源头是本表)。生图任务正确路径=chat 驱动模型+调共享
    # 工具 gen_image(真实图像模型只活在 gen_image 工具内部路由)。故 image/video 语义方向归一
    # 到 programming(会调工具的最强 chat 方向)。
    "image": "programming", "image_generation": "programming", "gen_image": "programming",
    "video": "programming", "video_generation": "programming",
    "chat": "general_chat_high_coverage", "general_chat_high_coverage": "general_chat_high_coverage",
    # 纯写作/闲聊/长文 → general_chat(MiniMax-M3等·覆盖稳)。
    "writing": "general_chat_high_coverage", "long_form": "general_chat_high_coverage",
    # research/reasoning/analysis/plan = 需要【联网深搜+调工具+强推理】的方向 → programming
    # (gpt-5.6-sol/gpt-5.4·实测会真调工具联网搜索)。2026-08-28 BC最难题实测根因:research
    # 曾映射到 general_chat 的 MiniMax-M3(纯chat不会调工具)→ codex 深搜秒退、主进程黑箱卡5min;
    # 换 programming 的 gpt-5.6-sol → 持续搜索事件+291s命中Aberdeen。深搜要会调工具的强模型。
    "research": "programming", "reasoning": "programming",
    "analysis": "programming", "plan": "programming", "deep_research": "programming",
}


def _normalize_direction(direction: str | None) -> str:
    """把任意军团语义方向归一到 models.json 的真实 key(未知别名退 programming)。"""
    return _DIRECTION_ALIASES.get((direction or "").strip().lower(), "programming")


def _load_model_router() -> dict:
    import model_registry
    return model_registry._load_models()


_APPID_MATRIX_CACHE: dict[str, Any] = {"data": None}


def _load_appid_matrix() -> dict:
    """加载 appid×model 交叉矩阵(552次真实HTTP实测·比 models.json 手配的 appids 更全)。
    用户 2026-08-29:'用动态化的appid去实验·model router里有全部appid最好的'——models.json 里
    最强模型(gpt-5.6-sol/GLM-5.3)只手配了1个 appid,但 matrix 实测它们能在 8 个 appid 上跑。
    用 matrix 的 by_model[model].pass_appids 补全可用 appid 池,轮换才有得错开。"""
    import model_registry
    return model_registry._load_appid_matrix()


def _appids_for_model_full(model: str, models_json_appids: list) -> list:
    """某模型的【全部实测可用 appid 池】= models.json 手配 ∪ matrix 实测 pass_appids(去重保序)。
    这样 gpt-5.6-sol 的 appid 池从 1(手配)→8(实测),并发轮换才真能错开避免撞限流。
    2026-09-10 服务器别名制:池内别名统一过 _resolve_alias;未解析的剔除(绝不发别名当 Bearer)。"""
    pool = list(models_json_appids or [])
    try:
        mx = _load_appid_matrix().get("by_model", {}).get(model, {})
        for a in (mx.get("pass_appids") or []):
            if a not in pool:
                pool.append(a)
    except Exception:
        pass
    try:
        import model_registry as _mr
        resolved = []
        for a in pool:
            r = _mr._resolve_alias(str(a))
            if r and r not in resolved:
                resolved.append(r)
        return resolved
    except ImportError:
        # 极端隔离(无 model_registry):宁空勿假——绝不返回未解析别名。
        return []


# ── per-harness 模型健康记忆(治"忽好忽坏"结构根因)────────────────────────────
# 现象:penguin/deepagents 对 general_chat 首选 MiniMax-M3 直接 rc1,每次派活都先撞一次再靠
# 跨方向兜底救回——表现为"这次快那次慢"。根因=首轮模型对每家不一定对口,且【军团不记得上次
# 谁跟谁不合】,每单重交学费。修法(给 context 不加 control·学 hermes provider health):把
# 各家跑某模型的真实成败记下来,下次给这家排序时把它【近期成功过的】提前、【近期连续失败的】
# 降后。不硬编码禁用(仍可被跨方向/穷尽选到)——只是让它凭历史事实自己少走弯路。
_MODEL_HEALTH_CACHE: dict[str, Any] = {"data": None}
_MODEL_HEALTH_LOCK = threading.RLock()


def _model_health_path() -> str:
    try:
        import legion_paths
        return legion_paths.home_under("model_health.json")
    except Exception:
        return os.path.expanduser("~/.meyo/legion/model_health.json")


def _load_model_health() -> dict:
    # mtime 失效重读(军团 cc dogfood 揪出:旧代码只判 data is None·缓存一旦加载永不失效→
    # worker 子进程写盘更新 model_health 后·主进程读的还是旧缓存·健康重排用死数据)。
    path = _model_health_path()
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0.0
    if _MODEL_HEALTH_CACHE["data"] is None or _MODEL_HEALTH_CACHE.get("mtime") != mtime:
        try:
            with open(path) as f:
                _MODEL_HEALTH_CACHE["data"] = json.load(f)
        except Exception:
            _MODEL_HEALTH_CACHE["data"] = {}
        _MODEL_HEALTH_CACHE["mtime"] = mtime
    return _MODEL_HEALTH_CACHE["data"]


@contextmanager
def _model_health_write_lock():
    """锁住 model_health 的跨进程读改写；原子 replace 只防半截，不能防并发丢计数。"""
    with _MODEL_HEALTH_LOCK:
        if _fcntl is None:
            yield
            return
        path = _model_health_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+") as handle:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def record_model_health(harness_key: str, model: str, ok: bool,
                        failure_category: str = "") -> None:
    """记录可归因的模型成败；环境、Harness、工具与用户失败不记模型坏账。"""
    if not harness_key or not model:
        return
    if not ok and str(failure_category or "") not in {
            "gateway", "rate_limit", "provider", "model_config", "truncated", "empty"}:
        return
    # 派单池纯净性铁律(用户 2026-09-02 钉死):capability/control-plane/ui-platform 家
    # 永不进答题池,它们的"战绩账"只会来自误派——记下来会污染 model_rank_observer 的
    # 模型排名(browser-use 0/51 脏账实证)。写入口直接拦,账本保持只有答题家。
    try:
        import external_agent_registry as _er_for_roles
        _item = next((a for a in _er_for_roles.load_catalog() if a.get("key") == harness_key), None)
        if _item and (_item.get("execution_role") or "agent") in ("capability", "control-plane", "ui-platform"):
            return
    except Exception:
        pass
    try:
        with _model_health_write_lock():
            # 必须在文件锁内重新读盘。多个 worker 都从旧缓存 load→改→replace，文件虽不损坏，
            # 后写者仍会覆盖先写者的计数；这里把整个事务锁成一个原子动作。
            path = _model_health_path()
            try:
                with open(path) as handle:
                    data = json.load(handle)
            except Exception:
                data = {}
            rec = data.setdefault(harness_key, {}).setdefault(model, {"good": 0, "bad": 0})
            rec["good" if ok else "bad"] += 1
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            _MODEL_HEALTH_CACHE["data"] = data
            try:  # 更新缓存 mtime·避免刚写的被自己的 mtime 校验判失效重读
                _MODEL_HEALTH_CACHE["mtime"] = os.path.getmtime(path)
            except Exception:
                pass
    except Exception:
        pass


def _health_rank(harness_key: str, model: str) -> int:
    """该家跑该模型的健康分:近期成功过→靠前(正),连续失败多→靠后(负),无记录=0(中性)。"""
    if not harness_key:
        return 0
    rec = (_load_model_health().get(harness_key) or {}).get(model)
    if not rec:
        return 0
    return rec.get("good", 0) - rec.get("bad", 0)


def select_usable_model(direction: str = "programming",
                        exclude_models: Optional[set[str]] = None,
                        harness_key: str = "", *,
                        _provider_results: Optional[Callable[[], list[dict]]] = None) -> dict:
    """网关降级:从 aigc-model-router 里挑一个【当下真能用且本请求没试过】的模型+appid。

    用户铁律"网关不行就选那几个可用的模型跑,让你自己有个数":
      - 优先用 trusted_appids 白名单(按顺序·已验证成功率高)
      - 跳过 known_broken_models 的坏模型 + quota_exhausted_appids 的耗尽 appid
      - 跳过 ``exclude_models``:同一次请求失败后必须真换模型,不能原地重试同一家
        (100场景实测旧链路会把 MiniMax-M3 连试两次,自愈等于没换)。
      - ``harness_key`` 给了就按 per-harness 模型健康【重排候选】:这家近期成功过的模型提前、
        连续失败的降后(治"忽好忽坏"·不硬禁用·仍可被穷尽选到)。
    私有请求事实只收窄原候选池，不授予新权限；执行前仍须重新检查。
    返回 {model, appid, reason}；授权过滤耗尽时额外保留原 authorization_hold，
    使调用方不能把空选择交给环境默认凭证或其它兜底。
    """
    d = _load_model_router()
    if not d:
        return {"model": None, "appid": None, "reason": "models.json 不可读"}
    direction = _normalize_direction(direction)   # research/vision/image… → 真实 key
    # broken models 是 dict 列表(取 .model);exhausted appids 是字符串列表
    _broken_raw = (d.get("known_broken_models") or {}).get("models") or []
    broken = set(m.get("model") if isinstance(m, dict) else m for m in _broken_raw)
    excluded = set(exclude_models or ())
    # ★别名制统一(2026-09-10 监督复现漏项):exhausted/trusted 与 _full 同过 _resolve_alias,
    # 否则耗尽过滤(别名≠真实值)永真、trusted∩池永空→绕白名单。缺映射=该授权不可用。
    def _resolve_all(vals):
        try:
            import model_registry as _mr
            return [r for r in (_mr._resolve_alias(str(a)) for a in vals) if r]
        except Exception:
            return [str(a) for a in vals]
    exhausted = set(_resolve_all((d.get("quota_exhausted_appids") or {}).get("appids") or []))
    trusted = [r for r in _resolve_all(
        [t["appid"] for t in (d.get("trusted_appids") or {}).get("list", []) if t.get("appid")])
        if r not in exhausted]
    cands = (d.get(direction) or {}).get("candidates") or []
    # 能力顺序优先；健康账本只把明确失败且从未成功的组合沉底，累计流量不能让旧次强模型越级。
    if harness_key:
        def _health_bucket(candidate):
            rec = (_load_model_health().get(harness_key) or {}).get(candidate.get("model", "")) or {}
            return 1 if int(rec.get("good", 0)) == 0 and int(rec.get("bad", 0)) > 0 else 0
        cands = sorted(cands, key=_health_bucket)
        # 家×模型兼容矩阵(治未病·2026-09-02):账本连败组合(该家零胜连败≥5)排序沉底——
        # 首轮/自愈轮都先挑别的好模型,不白烧一轮已知大概率失败的组合;但【不硬过滤】:
        # 全池只剩连败组合时仍可选(给翻身机会·环境病污染的组合一次成功即出清)。
        # 静态结构性不兼容(cline×reasoning 等)在下方候选循环里 continue 硬过滤(重试也不变)。
        try:
            import harness_model_compat as _hmc
            _bad = _hmc.ledger_incompatible(harness_key)
            if _bad:
                cands = sorted(cands, key=lambda c: (c.get("model", "") in _bad, 0))
        except Exception:
            pass
    # appid 轮换起点(用户 2026-08-29:并发按序错开 appid·避免全撞 trusted[0] 限流)。
    # 全进程单调游标每次取一个新起点·并发的多个 worker 天然分到不同 appid。
    try:
        _rot = next(_APPID_CURSOR) if _APPID_CURSOR is not None else 0
    except Exception:
        _rot = 0
    # 静态结构性不兼容(家×模型·重试也不变):hard 跳过。来源 harness_model_compat
    # (真跑钉死的 400/残片病·如 cline×reasoning)。新家撞同类 400 登记进表即全家防。
    try:
        import harness_model_compat as _hmc
        _static_bad = _hmc.STATIC_INCOMPATIBLE.get(harness_key, set())
    except Exception:
        _static_bad = set()
    authorization_results = _provider_results() if _provider_results is not None else []
    authorization_hold = None
    for c in cands:
        model = c.get("model")
        if not model or model in broken or model in excluded:
            continue
        if model in _static_bad:
            continue
        # 该模型可用的 appid = models.json手配 ∪ matrix实测(补全池·让轮换有得错开),再 ∩ 白名单(未耗尽)
        _full = _appids_for_model_full(model, c.get("appids") or [])
        if not _full:
            continue  # 别名无映射(授权未注入):该候选如实不可用,绝不发别名当Bearer
        model_appids = [a for a in _full if a not in exhausted]
        appid_pool = [a for a in trusted if a in model_appids] or model_appids
        if authorization_results:
            from error_envelope import request_provider_authorization_hold
            eligible = []
            for appid in appid_pool:
                held = request_provider_authorization_hold(
                    authorization_results, appid,
                    os.environ.get("MEYO_UNIFIED_GATEWAY")
                    or "https://aigc.sankuai.com/v1/openai/native")
                if held:
                    if authorization_hold is None:
                        authorization_hold = held
                else:
                    eligible.append(appid)
            # All subsequent cooldown/rotation fallbacks use only this subset.
            appid_pool = eligible
        # ① 优先未在冷却(429限流)的 appid;② 在池内按轮换游标错开起点(并发分散)
        live_pool = [a for a in appid_pool if not _appid_in_cooldown(a)]
        chosen_pool = live_pool or appid_pool   # 全冷却了也别选不出·退回全池(至少能跑)
        pick_appid = chosen_pool[_rot % len(chosen_pool)] if chosen_pool else None
        if pick_appid:
            _cool = "" if pick_appid in live_pool else "·(池内均在冷却·仍试)"
            return {"model": model, "appid": pick_appid,
                    "reason": f"选用 {model}（appid {pick_appid[:8]}…·轮换错开{_cool}）"}
    if authorization_hold is not None:
        return {"model": None, "appid": None, "authorization_hold": authorization_hold,
                "reason": "既有候选未满足本请求的授权条件"}
    why = "全部 broken/耗尽" if not excluded else "剩余候选已全部试过或 broken/耗尽"
    return {"model": None, "appid": None,
            "reason": f"{direction} 方向无可用模型（{why}）"}


def _select_cross_direction(current_direction: str, excluded: set[str]) -> dict:
    """当前能力方向全失败时,跨方向挑一个未试模型作最后兜底。

    依据(harness 源码·三证):hermes `cron/scheduler_provider.py:82-89 is_available()`——
    "named provider 返回 False,resolver 回退到 built-in(最稳兜底)"。同一哲学:某方向的模型
    这家 CLI 都跑不了(相当于该 provider 对它 unavailable),就回退到【兼容面最广的兜底】。
    hermes 的兜底是 built-in;我们的"built-in"= programming 方向的 gpt/Claude 类通用模型
    (对各家 CLI 兼容性最广),所以 programming 放遍历第一位。仍遵守 broken/quota 过滤 +
    excluded 去重(对应 hermes `provider_catalog` 的 broken 过滤 + order 遍历)。
    不给某家硬绑某模型(那会越补越死)——从真实 model router 动态找,未来新家自动受益。
    """
    current = _normalize_direction(current_direction)
    d = _load_model_router()
    # 先尝试兼容面最广的 programming,再遍历 router 里的其它正式方向(跳元数据 key)。
    order = ["programming", "general_chat_high_coverage", "long_context", "vision_understanding"]
    order.extend(k for k, v in d.items()
                 if isinstance(v, dict) and isinstance(v.get("candidates"), list))
    seen_dirs = set()
    for direction in order:
        direction = _normalize_direction(direction)
        if direction == current or direction in seen_dirs:
            continue
        seen_dirs.add(direction)
        pick = select_usable_model(direction, exclude_models=excluded)
        if pick.get("model"):
            return {**pick, "reason": f"跨方向兜底({current}→{direction}): {pick.get('reason')}"}
    return {"model": None, "appid": None, "reason": "所有方向候选均已试过/broken/耗尽"}


def gateway_status(probe: bool = True) -> dict:
    """网关就绪 + 降级建议:网关通不通、当下该用哪个模型/appid(用户要'自己有个数')。"""
    status = {"gateway_url": _GATEWAY_URL, "reachable": None}
    if probe:
        try:
            import urllib.request
            req = urllib.request.Request(_GATEWAY_URL, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as r:
                status["reachable"] = r.status < 500
        except Exception as e:
            # 连不上/超时/被拒 = 网关这条路当前不通
            status["reachable"] = False
            status["probe_error"] = f"{type(e).__name__}: {str(e)[:60]}"
    status["fallback_model"] = select_usable_model("programming")
    return status


def health(probe_gateway: bool = False, fast: bool = False) -> dict:
    """军团内核自检:编排层 + 出答案的真实依赖(AIGC 网关/CLI) + 降级建议。

    ready = 编排层 OK 且 appid 已配置 且 至少一家 CLI 在——这三者齐了军团才真能出答案。
    probe_gateway=True 时还真探测网关可达性(慢~5s,默认不探)。
    fast=True(用户 2026-09-01 loop):/health 端点(前端"新开页面"首屏/curl 探针)专用——绝不同步
      触发冷探活。进程刚起、预热未完成时立即返回 warming 态(端口秒连+首屏不卡),真实数据等后台
      预热填好下次请求就有。banner/派活等需要真实数据的路径不传 fast。
    """
    import model_registry
    explicit = model_registry.explicit_responses_status("codex")
    if explicit is not None:
        import shutil
        executable = bool(shutil.which("codex"))
        configured = explicit["configured"]
        # This contribution is scoped to the configured member. It neither
        # grants another member its key nor rewrites the legacy execution pool.
        # Check current config before any old health cache; no model probe here.
        return {
            "orchestrator_ok": _ORCH_OK, "orchestrator_error": _ORCH_ERR,
            "orchestrator_dir": _ORCH_DIR,
            "harness_count": len(_HARNESS_LIST_CACHE.get("data") or []),
            "aigc": {"appid_configured": configured, "appid_deployable": configured,
                     "appid_source": "operator_binding" if configured else None,
                     "clis": {"codex": executable},
                     "runnable_clis": ["codex"] if executable else [],
                     "provider_verified": False, "warming": False},
            "ready_to_serve": bool(_ORCH_OK and configured and executable),
            "warming": False, "readiness_scope": ["codex"],
            "readiness_basis": "configuration_and_executable", "provider_verified": False,
            "fallback_model": {"model": explicit["model"],
                               "reason": "已配置·未验证模型调用" if configured else "当前无有效模型配置"},
        }
    aigc = _aigc_ready(cached_only=fast)
    # fast 模式下 harness_count 只从缓存拿(不触发 list_harnesses 首次同步 4s 探活)·无缓存返 0
    if fast:
        _cached_list = _HARNESS_LIST_CACHE.get("data")
        harness_count = len(_cached_list) if _cached_list is not None else 0
    else:
        harness_count = len(list_harnesses())
    ready = _ORCH_OK and aigc["appid_configured"] and bool(aigc["runnable_clis"])
    out = {"orchestrator_ok": _ORCH_OK, "orchestrator_error": _ORCH_ERR,
           "orchestrator_dir": _ORCH_DIR, "harness_count": harness_count,
           "aigc": aigc, "ready_to_serve": ready,
           "warming": bool(aigc.get("warming"))}
    # /health 无需认证：只投影公开字段，不透传内部凭证或含凭证前缀的 reason。
    # 重新生成公开说明，避免未来 selector 的诊断文本增加其他敏感信息。
    def _public_model(pick: dict) -> dict:
        model = pick.get("model")
        return {"model": model,
                "reason": "已选用可用模型" if model else "当前无可用模型"}

    out["fallback_model"] = _public_model(select_usable_model("programming"))
    if probe_gateway:
        gateway = gateway_status(probe=True)
        out["gateway"] = {**gateway,
                          "fallback_model": _public_model(gateway["fallback_model"])}
    return out


# ── 军团统一能力总线(任何军团成员·含未来新加·一次拿全部可用能力) ────────
_CAP_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CAP_LOCK = threading.RLock()
# ecosystem 后端探测(cua/supervision 等)慢:cua --help 卡、supervision import CV 库首次 ~7s。
# 这些"某后端可用/不可用"状态很稳定,不该每次 capabilities 缓存过期(60s)都重付十几秒。
# 单独给 10 分钟 TTL——把慢探测的成本从"每 60s"降到"每 600s",cold capabilities 也不再被拖垮。
_ECO_PROBE_CACHE = {"data": None, "ts": 0.0}
_ECO_PROBE_TTL = 600.0


def available_capabilities(force: bool = False) -> dict:
    """军团能问"我现在能用哪些能力?"的统一入口(聚合各分散模块·带怎么调)。

    现在的军团 + 未来新加的 harness 调它都拿同一份全量能力——能力总线与具体 harness 解耦。
    每类带 ready(真装可用)+ invoke(军团怎么调)。聚合慢源(搜索/环境)缓存 60s。
    """
    with _CAP_LOCK:
        cached = _CAP_CACHE["data"]
        fresh = cached is not None and time.time() - _CAP_CACHE["ts"] < 60
    if not force and fresh and isinstance(cached, dict):
        return {**cached, "from_cache": True}
    # 过期时先交旧快照，后台单飞刷新；能力展示不能每分钟把用户卡在慢探针上。
    if not force and isinstance(cached, dict):
        _swr_bg_refresh("capabilities", lambda: available_capabilities(force=True))
        return {**cached, "from_cache": True, "stale": True}
    if not force:
        # 首次冷态也不能同步探 ecosystem/环境（实测 12s）。先给结构完整的 warming
        # 快照，后台单飞填真值；这和 toolbox/catalog 的冷态契约一致。
        empty = {"media": [], "search": {}, "file": [], "web": [], "ecosystem": [],
                 "installed": {}, "toolbox": {}, "how_to_call": {},
                 "summary": {"media": 0, "search_engines": 0, "file": 0, "web": 0,
                             "ecosystem": 0, "ecosystem_ready": 0, "installed": 0,
                             "toolbox": 0}}
        _swr_bg_refresh("capabilities", lambda: available_capabilities(force=True))
        return {**empty, "from_cache": False, "warming": True, "stale": False}
    caps = {"media": [], "search": {}, "file": [], "web": [], "ecosystem": [],
            "installed": {}, "toolbox": {}, "how_to_call": {}}

    # 媒体能力(可执行·media_tools)· 标注每个是否本机真可用(缺依赖会断)
    try:
        import media_tools
        import shutil
        aigc_ok = os.path.isfile(os.path.expanduser(
            "~/.claude/skills/aigc-model-router/scripts/call_model.py"))
        ffmpeg_ok = bool(shutil.which("ffmpeg"))
        mlist = media_tools.list_media_tools()
        # 生图/识图/PDF摘要靠 aigc skill;剪辑/抽帧靠 ffmpeg;缺则标 ready=False+需装什么
        for m in mlist:
            if m["id"] in ("gen_image", "see_image"):
                m["ready"] = aigc_ok
                m["needs"] = None if aigc_ok else "aigc-model-router skill(生图/识图需要)"
            elif m["id"] in ("trim_media", "extract_audio", "video_frame"):
                m["ready"] = ffmpeg_ok
                m["needs"] = None if ffmpeg_ok else "ffmpeg(brew install ffmpeg)"
            elif m["id"] == "gen_video":
                m["ready"] = False
                m["needs"] = "网关视频配额(当前枯竭)"
            else:
                m["ready"] = True
                m["needs"] = None
        caps["media"] = mlist
        caps["how_to_call"]["media"] = "run_media_tool(tool_id, params) 或 POST /api/legion/tool"
    except Exception:
        pass

    # 搜索源(search_fabric 4体系)
    try:
        import install_registry
        ss = install_registry.discover_search_sources()
        caps["search"] = {"systems": len(ss.get("systems", [])),
                          "engines": ss["summary"]["total_engines"],
                          "ready": ss["summary"]["ready"],
                          "by_system": [{"name": s["system"], "engines": len(s["engines"])}
                                        for s in ss.get("systems", [])]}
        caps["how_to_call"]["search"] = "search_fabric.SearchFabric().search(query, top_k=8) · 军团各家自带(免费多源:wikipedia/ddgs/brave)"
    except Exception:
        pass

    # 文件管道(file_hub)
    try:
        import file_hub  # noqa: F401
        caps["file"] = ["describe(任意文件→类型+预览)", "read_spreadsheet(Excel/CSV)",
                        "read_code / describe_repo(本地代码库)", "to_harness_context(喂给harness)"]
        caps["how_to_call"]["file"] = "file_hub.describe(path) / 上传走 /api/legion/upload"
    except Exception:
        pass

    # 网页 CLI(opencli)
    try:
        import opencli_bridge
        if opencli_bridge.available():
            caps["web"] = ["opencli(100+网站→CLI·JSON喂harness)"]
            caps["how_to_call"]["web"] = "opencli_bridge.run(args) 或 POST /api/legion/opencli"
    except Exception:
        pass

    # ecosystem 能力后端：此前 catalog 虽有 4 个后端和 runner，但生产能力总线零引用，等于
    # “仓库里有插座，墙上没开孔”。这里按 registry 动态探测并暴露真实状态/入口，不把它们
    # 混进能独立答题的 harness 池。新增后端只需在 BACKEND_PROBES 注册即可自动出现。
    try:
        if (not force and _ECO_PROBE_CACHE["data"] is not None
                and time.time() - _ECO_PROBE_CACHE["ts"] < _ECO_PROBE_TTL):
            caps["ecosystem"] = list(_ECO_PROBE_CACHE["data"])
        else:
            import meyo_harnesses.backends as _backends
            eco = []
            for name, probe in _backends.BACKEND_PROBES.items():
                if name == "omnigent":
                    continue  # omnigent 是正式 harness，已在成员池
                try:
                    state = probe()
                except Exception as exc:
                    state = {"available": False, "reason": f"{type(exc).__name__}:{exc}"}
                eco.append({"key": name, **state})
            caps["ecosystem"] = eco
            _ECO_PROBE_CACHE["data"] = list(eco)
            _ECO_PROBE_CACHE["ts"] = time.time()
        caps["how_to_call"]["ecosystem"] = (
            "harness_baselines/meyo-orchestrator/meyo_harnesses/backends/<key>_backend.py；"
            "先 probe，再由 harness 自主调用该 backend 暴露的方法")
    except Exception:
        pass

    # 已装库(全域环境·军团可直接 import/调用)
    try:
        import install_registry
        env = install_registry.discover_environment()
        caps["installed"] = {"total": env["summary"]["total"], "by_kind": env["summary"]["by_kind"]}
        caps["how_to_call"]["installed"] = "pip库直接import·CLI直接调·GitHub仓库在repo内"
    except Exception:
        pass

    # 工具箱(skill+代码工具计数)
    try:
        import toolbox
        tb = toolbox.scan_toolbox()
        caps["toolbox"] = {"total": tb["summary"]["total"], "skills": tb["summary"]["skills"],
                           "runnable": tb["summary"].get("runnable_tools", 0)}
        caps["how_to_call"]["toolbox"] = "GET /api/legion/toolbox?cap= 按能力筛"
    except Exception:
        pass

    caps["summary"] = {
        "media": len(caps["media"]), "search_engines": caps["search"].get("engines", 0),
        "file": len(caps["file"]), "web": len(caps["web"]),
        "ecosystem": len(caps["ecosystem"]),
        "ecosystem_ready": sum(bool(x.get("available")) for x in caps["ecosystem"]),
        "installed": caps["installed"].get("total", 0),
        "toolbox": caps["toolbox"].get("total", 0),
    }
    with _CAP_LOCK:
        _CAP_CACHE["data"] = caps
        _CAP_CACHE["ts"] = time.time()
    return {**caps, "from_cache": False}


def capabilities_health() -> dict:
    """军团武装自检:每类能力几个可用·缺什么·军团启动就知道自己武装到什么程度。"""
    c = available_capabilities()
    s = c["summary"]
    return {
        "armed": s["media"] > 0 and s["search_engines"] > 0 and s["installed"] > 0,
        "media_ready": s["media"], "search_engines": s["search_engines"],
        "file_ops": s["file"], "web_tools": s["web"],
        "ecosystem_ready": s["ecosystem_ready"], "ecosystem_total": s["ecosystem"],
        "installed_libs": s["installed"], "toolbox_items": s["toolbox"],
        "note": f"军团武装:{s['media']}媒体+{s['search_engines']}搜索引擎+"
                f"{s['ecosystem_ready']}/{s['ecosystem']}生态能力后端+"
                f"{s['installed']}已装库+{s['toolbox']}工具箱项·可用",
    }


# ── 持续运行按钮(pilot 续航,hook 与军团共用同一内核 + 同一共享纲领) ────────
def pilot_settings(user_id: str = "default") -> dict:
    """给前端渲染"持续运行"按钮/设置面板(说人话)。"""
    import legion_pilot as _lp
    cfg = _lp.load_user_config(user_id)
    return _lp.settings_for_humans(cfg)


def pilot_save_settings(user_id: str, config: dict) -> dict:
    """保存某用户的续航设置(开关/强度/拦哪些);只存白名单字段。"""
    import legion_pilot as _lp
    return _lp.save_user_config(user_id, config or {})


def pilot_check(reply: str, run_state: dict | None = None, user_id: str = "default") -> dict:
    """一轮结束调它:按钮开着时,判断该继续推进目标还是可以停(带共享纲领)。"""
    import legion_pilot as _lp
    return _lp.evaluate(reply, run_state or {}, user_id)


if __name__ == "__main__":
    import json
    print(json.dumps(health(), ensure_ascii=False, indent=2))
    print(json.dumps(list_harnesses(), ensure_ascii=False, indent=2))
