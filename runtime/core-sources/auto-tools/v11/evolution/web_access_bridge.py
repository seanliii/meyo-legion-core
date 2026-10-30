#!/usr/bin/env python3
"""
v13.14 · web_access_bridge · 把 web-access skill 的联网哲学接进 python 端
────────────────────────────────────────────────────────────────
灵感来源: https://github.com/eze-is/web-access (SKILL.md · 一泽 Eze)
用户诉求: "外部信息真实的浏览和搜索全部网页,不要被限制住·一旦被限制就自己进化去找内容"

核心哲学（照搬 web-access SKILL.md）:
- 一手信息 > 二手信息 · 搜索是发现入口 · 拿到 URL 就去读一手
- 遇到限流不重试同战术 · 立刻换方式 (Jina → curl → 记录到 fallback 库)
- 遇到 403/429/超时 · 记录站点+时刻 → 下轮 planner 主动避开
- 本机没浏览器时 CDP 模式不可用 · 但 Jina/curl 全套照样能用

四级降级 (v13.14 · 境内网络适配·2026-07-09):
  L1: curl 直取原站 HTML (境内首选·可加 UA 池反反爬·跟 Chrome 一样的请求头)
  L2: SearchEngine 元搜索 (Bing CN/Sogou 微信 · 已有基础)
  L3: web-access CDP proxy (需装 Chrome/Edge · 换 3457 端口避 claude-code-router 3456)
  L4: 记录限流事件 → 更新 fallback 学习库 · 下次跳过此站点/换 UA/换时段

⚠️ Jina (r.jina.ai) 境内 DNS 污染/被封 · 已从主链移除·保留 jina_read() 供已翻墙场景。

装了 Chrome+Edge 后 web-access 原生 SKILL 可用 (v11 py 侧不改)。

用法:
    from web_access_bridge import WebAccess
    wa = WebAccess()
    md = wa.read_url("https://xhs.com/xxx")        # 三级降级自动尝试
    md = wa.jina_only("https://example.com")       # 只用 Jina
    incidents = wa.get_recent_incidents(hours=24)  # 拿最近限流记录 · 供 planner 参考
"""
import os, sys, json, time, re, subprocess
from datetime import datetime, timedelta
from urllib.parse import urlparse
from pathlib import Path


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


# v13.90-fix · 用户抓「web_access_fail 一堆超时·是网站问题还是 search agent 问题」
# 实测根因: 目标站(如 docs.godotengine.org)本身完全正常(curl 2.8s 直接拿到完整
# HTML)·但 web_access_bridge 的所有抓取入口(curl_read 等)对系统 load 完全无
# 感知——不管当前多忙都用固定 15s timeout 硬等。系统 load 130+ 时 OS 调度延迟+
# 网络栈拥堵让正常网站也表现成"超时失败"→ 越失败 fetch_fail_learner 越忙 →
# load 越高，形成因果循环(参考 neuro_hub.py 已验证的 LOAD_MAX 闸门思路，此处
# 搬到抓取入口: 高负载不是"不抓"而是"更快知道拖不动"，缩短 timeout 快速失败/
# 短路，避免傻等满 15s 又占资源加剧循环)。
_LOAD_TIMEOUT_HIGH = float(os.environ.get("MEYO_WEBACCESS_LOAD_HIGH", "40"))   # 高负载线
_LOAD_TIMEOUT_EXTREME = float(os.environ.get("MEYO_WEBACCESS_LOAD_EXTREME", "80"))  # 极端线
_TIMEOUT_FLOOR_HIGH = 6      # 高负载: timeout 压到不超过此值(仍给一次真实机会)
_TIMEOUT_FLOOR_EXTREME = 3   # 极端负载: 进一步压缩(几乎是探测式短路)


def _current_load() -> float:
    """读 1min load average · 失败返回 0(不因探测本身出错而误伤正常抓取)。"""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def _load_adjusted_timeout(requested_timeout: int) -> int:
    """按当前系统负载动态压缩 timeout · 高负载时更快失败·不硬等满额。
    不阻止抓取本身(不是"不抓")·只是让"抓不动"的判定更快·避免傻等加剧连锁。"""
    load = _current_load()
    if load >= _LOAD_TIMEOUT_EXTREME:
        return min(requested_timeout, _TIMEOUT_FLOOR_EXTREME)
    if load >= _LOAD_TIMEOUT_HIGH:
        return min(requested_timeout, _TIMEOUT_FLOOR_HIGH)
    return requested_timeout


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INCIDENT_LOG = os.path.join(BASE, "v11", ".search_incidents.jsonl")
FALLBACK_KB = os.path.join(BASE, "v11", ".site_fallback_kb.json")

# Jina 官方限速: 20 RPM · 我们保守到 15 (留给 planner+evolution 共享)
JINA_RATE_PER_MIN = 15
JINA_MIN_INTERVAL = 60.0 / JINA_RATE_PER_MIN  # ~4s

# curl UA 池 · 反反爬
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]


class WebAccess:
    """web-access 哲学的 python 具身
    - 一手优于二手
    - 限流即换战术·不硬扛
    - 每次失败都学习·下次避开
    """

    def __init__(self):
        self._last_jina_at = 0.0
        self._load_fallback_kb()

    # ─────────────────────────────────────────
    # Fallback 知识库 · 站点 → 已知问题
    # ─────────────────────────────────────────
    def _load_fallback_kb(self):
        if os.path.exists(FALLBACK_KB):
            try:
                self.kb = json.load(open(FALLBACK_KB, encoding="utf-8"))
            except Exception:
                self.kb = {}
        else:
            self.kb = {}
        # 结构: {"xhs.com": {"last_429_at": "...", "count_24h": 3, "recommended_action": "skip"}, ...}

    def _save_fallback_kb(self):
        try:
            with open(FALLBACK_KB, "w", encoding="utf-8") as f:
                json.dump(self.kb, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _record_incident(self, url: str, kind: str, detail: str = ""):
        """记录一次限流/失败 · 落 jsonl + 更新 kb
        v13.90 · P2 修: 同时存到 self._last_incident_kind·供调度循环拼进
        _fail_log·避免 curl_fail/webclaw_stderr/playwright_exc 等真实报错
        被调度循环压扁成千篇一律的"{method}:空返回"(丢失可诊断信息)。

        v13.88.82 · 结构性根治: CDP Protocol error 不分域名·是系统级故障。
        之前每个域名各触发一次 web_access_fail·截断成 200 字符看不出真因·
        600+ 条告警都在重复同一个根因。这里专门检测协议错误特征·落完整堆栈
        到独立诊断文件(不截断)+只告一次(而非每域名一次)·并指向真正的排查方向。

        v13.90.1 · (修正此前误诊)"版本漂移"只是巧合关联·不是真根因。真根因是
        connect_over_cdp 附着外部独立启动的 Chrome 时会对已存在 default context
        调用 Browser.setDownloadBehavior(浏览器级托管命令·外部 Chrome 天然拒绝)·
        与版本无关(已用系统 Chrome 150 + playwright 1.61.0 no_defaults=True 实测
        验证无版本相关性)。已在 playwright_read/real_typing_search/
        click_and_read_detail 的 connect_over_cdp 调用点补上 no_defaults=True
        真正修复。此检测函数保留作为"万一又出现同类协议错误"的兜底诊断。
        """
        try:
            self._last_incident_kind = kind
            self._last_incident_detail = detail[:120]
        except Exception:
            pass
        try:
            entry = {
                "ts": datetime.now().isoformat(),
                "url": url,
                "domain": urlparse(url).netloc,
                "kind": kind,     # "429" / "403" / "timeout" / "empty" / "captcha"
                "detail": detail[:200],
            }
            with open(INCIDENT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 更新 kb
            dom = entry["domain"]
            if dom not in self.kb:
                self.kb[dom] = {"incidents": []}
            self.kb[dom]["incidents"].append({"ts": entry["ts"], "kind": kind})
            # 只保留最近 20 条
            self.kb[dom]["incidents"] = self.kb[dom]["incidents"][-20:]
            self._save_fallback_kb()
        except Exception:
            pass
        # v13.88.82 · CDP 协议错误 = 系统级故障侦测(不分域名)
        try:
            self._detect_cdp_protocol_fault(kind, detail)
        except Exception:
            pass

    _CDP_FAULT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", ".cdp_protocol_fault.jsonl")
    _CDP_FAULT_ALERT_THROTTLE_S = 3600  # 1h 只告一次(系统级故障·不按域名节流)

    def _detect_cdp_protocol_fault(self, kind: str, detail: str):
        """识别 CDP/Playwright Protocol error 特征 · 落完整信息(不截断)到
        独立诊断文件 · 触发版本自检 · 只告一次(而非每个域名各报一次)。
        这类错误的共同特征: "Protocol error" / "connect_over_cdp" 出现在
        异常文本里 · 不管目标域名是谁·根因都在 CDP 连接层(通常是 Chrome
        与 playwright 包版本不匹配) · 不该被当成"这个网站有问题"处理。
        """
        if kind not in ("playwright_exc", "cdp_exc", "cdp_navigate_exc", "pw_click_exc"):
            return
        _is_protocol_fault = any(k in detail for k in (
            "Protocol error", "protocol error", "connect_over_cdp",
            "ECONNREFUSED", "Target closed", "Browser has been closed",
        ))
        if not _is_protocol_fault:
            return

        # 版本自检: 拿到 playwright 绑定的 chromium 版本 vs 实际连的 9224 Chrome 版本
        _diag = {"ts": datetime.now().isoformat(), "kind": kind, "detail": detail}
        try:
            import subprocess as _sp
            _r = _sp.run(["curl", "-sS", "--max-time", "2",
                          f"{self._PLAYWRIGHT_CDP_URL}/json/version"],
                         capture_output=True, text=True, timeout=3)
            if _r.returncode == 0 and _r.stdout:
                _cdp_ver = json.loads(_r.stdout).get("Browser", "?")
                _diag["cdp_chrome_version"] = _cdp_ver
        except Exception as _e:
            _diag["cdp_chrome_version_probe_err"] = str(_e)
        try:
            from playwright.sync_api import sync_playwright as _spw
            _p = _spw().start()
            _diag["playwright_bundled_chromium_path"] = _p.chromium.executable_path
            _p.stop()
            _r2 = subprocess.run([_diag["playwright_bundled_chromium_path"], "--version"],
                                  capture_output=True, text=True, timeout=5)
            _diag["playwright_bundled_chromium_version"] = (_r2.stdout or "").strip()
        except Exception as _e:
            _diag["playwright_version_probe_err"] = str(_e)

        # 完整落盘(不截断) · 供人工/自动化诊断真根因用
        try:
            with open(os.path.abspath(self._CDP_FAULT_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(_diag, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 节流告警: 1h 一次(系统级故障·刷屏无意义·只需要知道"还没修好")
        _throttle_marker = os.path.abspath(self._CDP_FAULT_LOG) + ".last_alert"
        _now_ts = time.time()
        _should_alert = True
        try:
            if os.path.exists(_throttle_marker):
                _last = float(open(_throttle_marker).read().strip() or 0)
                if _now_ts - _last < self._CDP_FAULT_ALERT_THROTTLE_S:
                    _should_alert = False
        except Exception:
            pass
        if _should_alert:
            try:
                with open(_throttle_marker, "w") as f:
                    f.write(str(_now_ts))
            except Exception:
                pass
            try:
                from alert_bus import notify as _n
                _cv = _diag.get("cdp_chrome_version", "?")
                _pv = _diag.get("playwright_bundled_chromium_version", "?")
                _n("cdp_protocol_fault", "ERROR",
                   f"CDP协议错误(系统级·非单站问题) · 9224实际={_cv} vs "
                   f"playwright绑定={_pv} · 诊断落 .cdp_protocol_fault.jsonl · "
                   f"真根因非版本问题(v13.90.1已确认)· 检查 connect_over_cdp 调用点"
                   f"是否都带 no_defaults=True(见 web_access_bridge.py playwright_read)",
                   throttle_min=60)
            except Exception:
                pass

    def get_recent_incidents(self, hours: int = 24) -> list:
        """给 planner 用: 最近 N 小时的限流事件汇总·避免再选这些源"""
        if not os.path.exists(INCIDENT_LOG):
            return []
        cutoff = datetime.now() - timedelta(hours=hours)
        rows = []
        try:
            with open(INCIDENT_LOG, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if datetime.fromisoformat(e["ts"]) >= cutoff:
                            rows.append(e)
                    except Exception:
                        continue
        except Exception:
            pass
        return rows

    def _is_url_path_blocked(self, url: str, hours: int = 3, min_incidents: int = 3) -> bool:
        """v13.28 · 检查这个具体 URL(去掉 query 后)最近 N 小时失败几次
        不再一刀切整个域名"""
        if not os.path.exists(INCIDENT_LOG):
            return False
        # URL 规范化: 只保留 host+path · 忽略 query/fragment
        from urllib.parse import urlparse
        parsed = urlparse(url)
        key = f"{parsed.netloc}{parsed.path}"
        cutoff = datetime.now() - timedelta(hours=hours)
        count = 0
        try:
            with open(INCIDENT_LOG, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if datetime.fromisoformat(e["ts"]) < cutoff:
                            continue
                        eurl = e.get("url", "")
                        e_parsed = urlparse(eurl)
                        if f"{e_parsed.netloc}{e_parsed.path}" == key:
                            count += 1
                    except Exception:
                        continue
        except Exception:
            pass
        return count >= min_incidents

    def get_blocked_domains(self, hours: int = 24, min_incidents: int = 3) -> list:
        """最近 N 小时限流≥ min 次的域名 · planner 应避开"""
        recent = self.get_recent_incidents(hours)
        from collections import Counter
        c = Counter(e["domain"] for e in recent)
        return [d for d, n in c.items() if n >= min_incidents]

    # ─────────────────────────────────────────
    # L1 · Jina · 免登录·Markdown 化
    # ─────────────────────────────────────────
    def jina_read(self, url: str, timeout: int = 20) -> str:
        """用 r.jina.ai 前缀读页面·返回 Markdown"""
        # 限速守卫
        elapsed = time.time() - self._last_jina_at
        if elapsed < JINA_MIN_INTERVAL:
            time.sleep(JINA_MIN_INTERVAL - elapsed)
        self._last_jina_at = time.time()

        # 去掉 http(s):// 前缀 · Jina 直接拼接
        clean = re.sub(r"^https?://", "", url)
        jina_url = f"https://r.jina.ai/{clean}"

        import random
        ua = random.choice(UA_POOL)
        try:
            r = subprocess.run(
                ["curl", "-sS", "-L", "--max-time", str(timeout),
                 "-A", ua, jina_url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            content = r.stdout or ""
            if r.returncode != 0 or not content.strip():
                self._record_incident(url, "jina_fail", r.stderr[:200] if r.stderr else "empty")
                return ""
            # Jina 出错时会返回错误页面 · 检查
            if any(sig in content[:500] for sig in ["Too Many Requests", "429", "Rate limit"]):
                self._record_incident(url, "429", "jina rate limited")
                return ""
            if len(content) < 100:
                self._record_incident(url, "empty", f"jina 短返回 {len(content)}")
                return ""
            return content
        except Exception as e:
            self._record_incident(url, "jina_exc", str(e)[:200])
            return ""

    # ─────────────────────────────────────────
    # L2 · curl 直取
    # ─────────────────────────────────────────
    def curl_read(self, url: str, timeout: int = 15) -> str:
        """直接 curl · 返回原 HTML (含噪声但完整)
        v13.90-fix: 高负载时动态压缩 timeout · 避免 load 130+ 时正常网站也
        被硬等出"假性超时"(实测 docs.godotengine.org 低负载 2.8s 就通)。"""
        import random
        ua = random.choice(UA_POOL)
        _load = _current_load()
        timeout = _load_adjusted_timeout(timeout)
        try:
            r = subprocess.run(
                ["curl", "-sSL", "--max-time", str(timeout),
                 "-A", ua,
                 "-H", "Accept: text/html,application/xhtml+xml",
                 "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                 url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            html = r.stdout or ""
            if r.returncode != 0:
                _reason = r.stderr[:200]
                if _load >= _LOAD_TIMEOUT_HIGH:
                    _reason = f"[高负载load={_load:.0f}·timeout已压至{timeout}s] {_reason}"
                self._record_incident(url, "curl_fail", _reason)
                # v13.90-fix · exit 6/7/28/35/56 是连接层失败(DNS/拒连/超时)·
                # 累计学习·别每次都傻等同样的 timeout 撞同一堵网络墙
                try:
                    from urllib.parse import urlparse as _up
                    self._record_conn_level_fail(_up(url).netloc, r.returncode)
                except Exception:
                    pass
                return ""
            # v13.88.82 · 结构性修复: 改用统一反爬检测(见 detect_and_learn_anti_bot)·
            # 命中即记住域名(之前这里检测到了但从不"记住"·下次还是硬闯同一堵墙)
            if self.detect_and_learn_anti_bot(url, html):
                self._record_incident(url, "captcha", html[:200].lower())
                return ""
            if len(html) < 200:
                self._record_incident(url, "empty", f"curl 短返回 {len(html)}")
                return ""
            return html
        except Exception as e:
            self._record_incident(url, "curl_exc", str(e)[:200])
            return ""

    # ─────────────────────────────────────────
    # HTML → 结构化抽取 (v13.88.86 · lxml 升级)
    # ─────────────────────────────────────────
    def extract_structured(self, html: str, url: str = "") -> dict:
        """v13.88.86 · lxml 结构化抽取 · 先于 extract_readable 调
        返回 dict 结构 · 空 dict 表示 lxml 没抽出东西 (走老路)"""
        try:
            from lxml_extractor import extract
            result = extract(html, url)
            return result if result.get("ok") else {}
        except ImportError:
            return {}
        except Exception:
            return {}

    # ─────────────────────────────────────────
    # HTML → 正文提取（把噪声 800K 压到 3-5K）
    # ─────────────────────────────────────────
    def looks_like_real_content(self, content: str) -> tuple:
        """v13.88.9 · 2026-07-30 · 用户抓「少数派一直404·每次以为通了其实空壳」
        病根:判『通』只看字数·被 2679 字空壳骨架页 / RSS XML 原文骗·
        还标 readable:True。这里守门:内容像不像真正文?
        返回 (ok: bool, reason: str)
        """
        if not content or len(content.strip()) < 200:
            return (False, "太短")
        head = content.lstrip()[:200].lower()
        # RSS/XML 原文当正文返回(实测 sspai auto 降级到 api:generic_rss 的坑)
        if head.startswith("<?xml") or "<rss" in head or "<feed" in head:
            return (False, "RSS/XML 原文·非文章正文")
        # SPA 空壳:去标签后基本只剩 JS 埋点(gtag/dataLayer/_hmt)且无正文段落
        import re as _re
        text = _re.sub(r"<[^>]+>", "", content)
        text = _re.sub(r"\s+", " ", text).strip()
        js_noise = sum(text.count(k) for k in ("gtag(", "dataLayer", "_hmt", "function("))
        has_para = ("<p>" in content or "<article" in content
                    or "。" in text or "，" in text[:500])
        if js_noise >= 2 and not has_para and len(text) < 1500:
            return (False, "SPA 空壳·全是 JS 埋点无正文")
        # v13.88.9L-48 · 通用SPA骨架判据(用户: 不该靠给每个站加规则·该自愈):
        #   SPA骨架特征=正文占比极低(全是导航/链接/短标签·没实质连续段落)。
        #   不针对站点·判「内容像不像骨架」→ 是就降级CDP。治所有未知SPA(anthropic/linear/vercel类)。
        # v13.88.9L-48 · 通用SPA骨架判据(用户: 不该靠给每个站加规则·该自愈):
        #   核心=正文密度·不看绝对大小(SPA骨架常很大·塞满JS/CSS·真句子极少)。
        #   anthropic/news实测: curl 369509字但真句仅1、链接436 → 骨架。
        #   真句 = 连续>=20字的中英文散文段(按句号/换行切)。密度=真句字数/总字数。
        # v13.88.9L-48 · 通用SPA骨架判据(用户: 不该给每个站加规则·该自愈):
        #   核心=自然语言标点密度。真文章有大量句号/逗号/问号(中英)·
        #   SPA骨架去标签后是一坨连续JS/CSS/类名噪声(camelCase/横杠/花括号)·标点极少。
        #   anthropic/news实测: 369509字但句子标点极少 → 骨架。不看大小·看标点密度。
        # 代码/骨架噪声: 真文章正文几乎没有花括号/分号/()·SPA骨架去标签后残留几千个。
        # anthropic/news实测码噪4974·真文章≈0。这是最强单一骨架信号(不靠站点规则)。
        _code_noise = text.count("{") + text.count("}") + text.count(";") + text.count("()")
        _cn_ratio = _code_noise / max(len(text), 1)
        # 判骨架: 码噪密度高(>0.3%·真文章正文≈0) → curl只拿到JS/组件骨架·需CDP渲染真内容。
        if _cn_ratio > 0.003 and _code_noise > 200:
            return (False, f"SPA骨架·码噪密度{_cn_ratio:.2%}({_code_noise}个{{}};()·真文章≈0)·需CDP渲染")
        return (True, "ok")

    def extract_readable(self, html: str, url: str = "") -> str:
        """从 HTML 里抽正文 · v13.88.86 三级 fallback:
          L1 · lxml 结构化 (列表/详情/表格 → markdown 表)
          L2 · trafilatura (文章正文)
          L3 · bs4 兜底
        返回纯文本 · 空串代表提取失败"""
        if not html or len(html) < 100:
            return ""

        # L1 · v13.88.86 · lxml 结构化抽取
        try:
            structured = self.extract_structured(html, url)
            if structured and structured.get("ok"):
                parts = []
                if structured.get("page_type") == "detail":
                    f = structured.get("fields", {})
                    if f.get("title"):
                        parts.append(f"# {f['title']}")
                    if f.get("author"):
                        parts.append(f"作者: {f['author']}")
                    if f.get("date"):
                        parts.append(f"日期: {f['date']}")
                    if f.get("body"):
                        parts.append(f"\n{f['body']}")
                elif structured.get("items"):
                    # list/table → markdown 表
                    items = structured["items"]
                    if items:
                        # 取所有 key 作表头
                        headers = []
                        for it in items[:5]:
                            for k in it.keys():
                                if k not in headers:
                                    headers.append(k)
                        if headers:
                            parts.append("| " + " | ".join(headers) + " |")
                            parts.append("| " + " | ".join(["---"] * len(headers)) + " |")
                            for it in items:
                                row = [str(it.get(h, ""))[:80] for h in headers]
                                parts.append("| " + " | ".join(row) + " |")
                if parts:
                    md = "\n".join(parts)
                    if len(md) > 100:
                        return md
        except Exception:
            pass

        # L2 · trafilatura 首选（专门为正文抽取设计·比 bs4 精准）
        try:
            import trafilatura
            extracted = trafilatura.extract(
                html, url=url, favor_recall=True,
                include_comments=False, include_tables=True,
                deduplicate=True,
            )
            if extracted and len(extracted) > 100:
                return extracted
        except Exception:
            pass
        # L3 · bs4 兜底 · 拿 <p>/<article> 里的文字
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            # 去掉 script/style/nav/footer
            for tag in soup(["script", "style", "nav", "footer", "aside", "header", "noscript"]):
                tag.decompose()
            # 挑正文候选：article > main > .content > body
            for sel in ["article", "main", ".content", ".article", "#content", "body"]:
                node = soup.select_one(sel)
                if node:
                    text = node.get_text(separator="\n", strip=True)
                    if len(text) > 200:
                        return text
        except Exception:
            pass
        return ""

    # ─────────────────────────────────────────
    # 组合 · 读一个 URL · 三级降级
    # ─────────────────────────────────────────
    def _read_hn_item(self, url: str) -> str:
        """v13.33 · HN item 页面 SPA · 用 Firebase HN API 拿 story+top comments
        (news.ycombinator.com/item?id=XXX)"""
        import re as _re
        m = _re.search(r'id=(\d+)', url)
        if not m:
            return ""
        item_id = m.group(1)
        try:
            # 1. 拿 story
            r1 = subprocess.run(
                ["curl", "-sS", "--max-time", "8",
                 f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"],
                capture_output=True, text=True, timeout=10,
            )
            if r1.returncode != 0 or not r1.stdout.strip():
                return ""
            import json as _json
            story = _json.loads(r1.stdout)
            if not isinstance(story, dict):
                return ""

            lines = []
            if story.get("title"):
                lines.append(f"# {story['title']}")
            if story.get("by"):
                lines.append(f"[作者: {story['by']} · 分数: {story.get('score',0)} · {story.get('descendants',0)} 评论]")
            if story.get("url"):
                lines.append(f"原链接: {story['url']}")
            if story.get("text"):
                lines.append(_re.sub(r'<[^>]+>', '', story["text"])[:2000])

            # 2. 拿 top 10 评论
            kids = (story.get("kids", []) or [])[:10]
            lines.append(f"\n═══ 评论 (top {len(kids)}) ═══")
            for kid_id in kids:
                try:
                    r2 = subprocess.run(
                        ["curl", "-sS", "--max-time", "5",
                         f"https://hacker-news.firebaseio.com/v0/item/{kid_id}.json"],
                        capture_output=True, text=True, timeout=6,
                    )
                    if r2.returncode == 0 and r2.stdout.strip():
                        c = _json.loads(r2.stdout)
                        if isinstance(c, dict) and c.get("text"):
                            clean = _re.sub(r'<[^>]+>', '', c["text"])[:800]
                            lines.append(f"\n[{c.get('by','anon')}]: {clean}")
                except Exception:
                    continue
            return "\n".join(lines)[:20000]
        except Exception:
            return ""

    def _maybe_read_image(self, url: str, _timeout: int = 15,
                          _max_bytes: int = 10 * 1024 * 1024) -> dict:
        """v13.88.9L · 通用图片OCR分支(用户「GAIA真边界=图表轴标签/截图里的文字·read_url读不到」·
        实证根因: Goldfinger降落伞颜色/egalitarian arXiv三轴示意图·答案是【图片里的文字】·
        网页正文+PDF文本层都没有→检索/深读全抽不到。这是【缺视觉精读能力】·非架构缺陷)。
        触发(任一): URL图片后缀(.jpg/.jpeg/.png/.gif/.webp/.bmp/.tif/.tiff) /
        HEAD content-type=image/* / 下载字节是图片 magic bytes。
        命中→ tesseract OCR 抽文字·返回 read_url 兼容 dict。
        未命中→ 返回 None(交回上层原HTML/PDF分支·纯增量不破坏)。
        fail-safe: 下载/OCR超时或异常→返回失败dict(不阻断·不返回None避免上层当HTML乱解析)。
        上限: 10MB / 15s。通用(任何图片URL)·非特判。[[feedback-built-not-wired-rootfix]]
        """
        import re as _re
        _lu = (url or "").lower().split("?")[0].split("#")[0]
        _IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff")
        _looks_img = _lu.endswith(_IMG_EXT)
        # content-type 探测(HEAD·便宜)·HEAD失败靠后续 magic bytes 兜底
        if not _looks_img:
            try:
                import subprocess as _sp
                _r = _sp.run(["curl", "-sIL", "--max-time", "8", url],
                             capture_output=True, text=True, timeout=10)
                if _re.search(r"content-type:\s*image/", _r.stdout, _re.I):
                    _looks_img = True
            except Exception:
                pass
        if not _looks_img:
            return None  # 明确非图片 → 交回上层
        # 到这里=判定为图片·下载到临时文件做 OCR
        import os as _os, tempfile as _tmp, subprocess as _sp
        _tmpf = None
        try:
            _suffix = next((e for e in _IMG_EXT if _lu.endswith(e)), ".png")
            _fd, _tmpf = _tmp.mkstemp(suffix=_suffix)
            _os.close(_fd)
            _dl = _sp.run(["curl", "-sL", "--max-time", str(_timeout),
                           "--max-filesize", str(_max_bytes), "-o", _tmpf, url],
                          capture_output=True, text=True, timeout=_timeout + 3)
            if _dl.returncode != 0 or not _os.path.exists(_tmpf) or _os.path.getsize(_tmpf) == 0:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "image_download_failed",
                        "fail_reason": f"图片下载失败 curl rc={_dl.returncode}"}
            # 校验 magic bytes(确认真是图片·非404 HTML错误页)
            with open(_tmpf, "rb") as _f:
                _head = _f.read(12)
            _is_img_bytes = (_head.startswith(b"\xff\xd8\xff")          # JPEG
                             or _head.startswith(b"\x89PNG")            # PNG
                             or _head[:6] in (b"GIF87a", b"GIF89a")     # GIF
                             or _head.startswith(b"BM")                 # BMP
                             or (_head[:4] == b"RIFF" and _head[8:12] == b"WEBP")  # WEBP
                             or _head.startswith(b"II*\x00")            # TIFF LE
                             or _head.startswith(b"MM\x00*"))           # TIFF BE
            if not _is_img_bytes:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "image_not_img_bytes",
                        "fail_reason": f"下载内容非图片(magic={_head[:8]!r})·可能是错误页/登录墙"}
            try:
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore
            except Exception as _ie:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "image_no_ocr_lib",
                        "fail_reason": f"OCR库未装:{type(_ie).__name__}:{str(_ie)[:60]}"}
            with Image.open(_tmpf) as _img:
                # OCR 前放大小图(轴标签/水印常很小·放大提命中)·上限防内存爆
                _w, _h = _img.size
                if max(_w, _h) < 1000:
                    _scale = min(3, 1000 // max(1, min(_w, _h)) + 1)
                    _img = _img.resize((_w * _scale, _h * _scale))
                _text = pytesseract.image_to_string(_img).strip()
            if not _text:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "image_ocr_empty",
                        "fail_reason": "图片OCR成功但无文字(纯图/照片·无文本内容)"}
            return {"ok": True, "url": url,
                    "content": _text[:30000],
                    "via": "image_ocr_tesseract", "readable": True,
                    "completeness": "full",
                    "note": f"图片OCR文字提取({len(_text)}字·tesseract)"}
        except _sp.TimeoutExpired:
            return {"ok": False, "url": url, "content": "", "readable": False,
                    "via": "image_timeout", "fail_reason": f"图片下载/OCR超时(>{_timeout}s)"}
        except Exception as _e:
            return {"ok": False, "url": url, "content": "", "readable": False,
                    "via": "image_error",
                    "fail_reason": f"图片OCR异常:{type(_e).__name__}:{str(_e)[:80]}"}
        finally:
            try:
                if _tmpf and _os.path.exists(_tmpf):
                    _os.remove(_tmpf)
            except Exception:
                pass

    def _ocr_pdf_pages(self, pdf_path: str, _max_pages: int = 3, _timeout: int = 40) -> str:
        """v13.88.9L · 扫描件/图片PDF回退OCR: pdftoppm(poppler)转PNG→tesseract OCR。
        仅前 _max_pages 页(防慢)。fail-safe: 任何异常/缺库返回空串(不阻断上层)。"""
        import os as _os, tempfile as _tmp, subprocess as _sp, glob as _glob
        _outdir = None
        try:
            try:
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore
            except Exception:
                return ""
            _outdir = _tmp.mkdtemp(prefix="pdfocr_")
            _prefix = _os.path.join(_outdir, "pg")
            # pdftoppm: 前 _max_pages 页·150dpi(够OCR·不过大)·PNG
            _sp.run(["pdftoppm", "-png", "-r", "150", "-f", "1",
                     "-l", str(_max_pages), pdf_path, _prefix],
                    capture_output=True, timeout=_timeout)
            _pngs = sorted(_glob.glob(_prefix + "*.png"))
            _texts = []
            for _p in _pngs[:_max_pages]:
                try:
                    with Image.open(_p) as _img:
                        _t = pytesseract.image_to_string(_img).strip()
                    if _t:
                        _texts.append(_t)
                except Exception:
                    continue
            return "\n".join(_texts).strip()
        except Exception:
            return ""
        finally:
            try:
                if _outdir and _os.path.isdir(_outdir):
                    import shutil as _sh
                    _sh.rmtree(_outdir, ignore_errors=True)
            except Exception:
                pass

    def _maybe_read_pdf(self, url: str, _timeout: int = 20,
                        _max_pages: int = 30, _max_bytes: int = 10 * 1024 * 1024) -> dict:
        """v13.90.5 · 通用PDF正文提取(用户「答案藏论文PDF正文·read_url读不到」)。
        触发条件(任一): URL以.pdf结尾 / HEAD响应content-type是application/pdf /
        下载到的字节开头是 %PDF magic bytes。
        命中→ pdfplumber 提取前 N 页文本·返回 read_url 兼容 dict。
        未命中(非PDF)→ 返回 None(让上层继续走原HTML分支·纯增量不破坏)。
        fail-safe: 下载/解析超时或异常→返回失败dict(不返回None·避免上层拿着已知PDF当HTML再解析乱码)。
        上限: 前30页 / 10MB / 20s·防超大PDF卡死。通用非特判EC。
        """
        import re as _re
        _lu = (url or "").lower().split("?")[0].split("#")[0]
        _looks_pdf = _lu.endswith(".pdf")

        # content-type 探测(HEAD·便宜)。HEAD失败不阻断·靠后续magic bytes兜底。
        if not _looks_pdf:
            try:
                _r = subprocess.run(
                    ["curl", "-sSIL", "--max-time", "8",
                     "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36", url],
                    capture_output=True, text=True, timeout=10,
                )
                if _r.returncode == 0 and _re.search(
                        r"content-type:\s*application/pdf", _r.stdout, _re.I):
                    _looks_pdf = True
            except Exception:
                pass

        # 非.pdf后缀且HEAD没说是PDF → 探一眼前几个字节(magic bytes·抓下载重定向到pdf的情况)。
        # 只有当URL"疑似"(未知content-type)时才探·避免给每个普通网页多加一次请求。
        _magic_pdf = False
        if not _looks_pdf:
            return None  # 明确非PDF(既非.pdf后缀·HEAD也非application/pdf)→交回HTML分支

        # 到这里=已判定为PDF·下载到临时文件。
        import tempfile as _tmp, os as _os
        _tmpf = None
        try:
            _fd, _tmpf = _tmp.mkstemp(suffix=".pdf")
            _os.close(_fd)
            # -L跟随重定向·--max-filesize限制体积·-A伪装UA避免部分站403
            _dl = subprocess.run(
                ["curl", "-sSL", "--max-time", str(_timeout),
                 "--max-filesize", str(_max_bytes),
                 "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36",
                 "-o", _tmpf, url],
                capture_output=True, text=True, timeout=_timeout + 5,
            )
            if _dl.returncode != 0 or not _os.path.exists(_tmpf) or _os.path.getsize(_tmpf) == 0:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "pdf_download_failed",
                        "fail_reason": f"PDF下载失败 curl rc={_dl.returncode} "
                                       f"size={_os.path.getsize(_tmpf) if _os.path.exists(_tmpf) else 0}"}
            # 校验 magic bytes: 确认真是PDF(有些站404返回HTML错误页也是200)
            with open(_tmpf, "rb") as _fh:
                _head = _fh.read(5)
            if not _head.startswith(b"%PDF"):
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "pdf_not_pdf_bytes",
                        "fail_reason": f"下载内容非PDF(magic={_head!r})·可能是错误页/登录墙"}

            try:
                import pdfplumber  # type: ignore
            except Exception as _ie:
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "pdf_no_lib",
                        "fail_reason": f"pdfplumber未安装:{type(_ie).__name__}:{str(_ie)[:60]}"}

            _texts = []
            _npages = 0
            with pdfplumber.open(_tmpf) as _pdf:
                for _i, _page in enumerate(_pdf.pages):
                    if _i >= _max_pages:
                        break
                    _npages = _i + 1
                    try:
                        _t = _page.extract_text() or ""
                    except Exception:
                        _t = ""
                    if _t:
                        _texts.append(_t)
            _content = "\n".join(_texts).strip()
            if not _content:
                # v13.88.9L · 扫描件/图片PDF(文本层为空)回退OCR(line679原标注「需OCR」·建了没接·现接上)。
                #   pdftoppm(poppler·已装)把前几页转PNG→tesseract OCR。上限3页防慢。fail-safe。
                _ocr_txt = self._ocr_pdf_pages(_tmpf, _max_pages=3)
                if _ocr_txt:
                    return {"ok": True, "url": url, "content": _ocr_txt[:30000],
                            "via": "pdf_ocr_tesseract", "readable": True,
                            "completeness": "truncated",
                            "note": f"扫描件PDF·OCR提取({len(_ocr_txt)}字·pdftoppm+tesseract)"}
                return {"ok": False, "url": url, "content": "", "readable": False,
                        "via": "pdf_pdfplumber_empty",
                        "fail_reason": f"PDF文本层空({_npages}页·扫描件)·OCR也未抽到文字"}
            return {"ok": True, "url": url,
                    "content": _content[:30000],
                    "via": "pdf_pdfplumber", "readable": True,
                    "completeness": "full" if _npages < _max_pages else "truncated",
                    "note": f"PDF正文提取({_npages}页·pdfplumber)"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "url": url, "content": "", "readable": False,
                    "via": "pdf_timeout", "fail_reason": f"PDF下载超时(>{_timeout}s)"}
        except Exception as _e:
            return {"ok": False, "url": url, "content": "", "readable": False,
                    "via": "pdf_error",
                    "fail_reason": f"PDF解析异常:{type(_e).__name__}:{str(_e)[:80]}"}
        finally:
            try:
                if _tmpf and _os.path.exists(_tmpf):
                    _os.remove(_tmpf)
            except Exception:
                pass

    def read_url(self, url: str, prefer: str = "auto",
                 force_retry: bool = False,
                 auto_click_listing: bool = False) -> dict:
        """v13.88.9 · 统一后置守门包装 · 用户「堵死·别再以为通了其实空壳」
        所有内部 return 经此过一道 looks_like_real_content·
        标 readable:True 但实为 RSS/XML/空壳 → 降级 readable:False + 标 fake_content。
        """
        # v13.90(2026-08-09 用户「进化千万不要 CDP 方式搜索·减少性能需求」):
        #   进化上下文设 MEYO_NO_CDP=1 时·强制走 curl(http)·且下面的「空壳自愈
        #   CDP 重试」也跳过——进化的深读一律不开浏览器(CDP 最吃 CPU)。一处闸门
        #   覆盖所有经 read_url 的进化搜索(persona 调研/深研/工具锻造等)。
        #   非进化上下文(用户真实提问)不受影响·auto 该上 CDP 照上。
        _no_cdp = os.environ.get("MEYO_NO_CDP") == "1"
        if _no_cdp and prefer in ("auto", "cdp", "browseract", "playwright",
                                   "browser_use", "firecrawl"):
            prefer = "curl"  # 强制纯 http·不开浏览器
        result = self._read_url_impl(url, prefer, force_retry, auto_click_listing)
        try:
            if isinstance(result, dict) and result.get("readable") and result.get("content"):
                real, why = self.looks_like_real_content(result["content"])
                # v13.88.9L-48b · 补: extract提炼后content看着正常·但相对原始HTML提炼率极低
                #   = SPA骨架残渣(anthropic实测 raw369509→content1856·提炼率0.5%)。
                #   守门原来只判提炼后content(被骗)·这里加提炼率判据: raw大+content小=骨架。
                _raw = result.get("raw_html_len", 0) or 0
                _clen = len(result.get("content") or "")
                if real and _raw > 50000 and _clen < 2500:
                    real = False
                    why = f"提炼率极低({_clen}/{_raw}={_clen/max(_raw,1):.1%})·SPA骨架残渣·需CDP"
                if not real:
                    result["readable"] = False
                    result["fake_content"] = why
                    result["note"] = (result.get("note", "") + f" ·守门:{why}·非真正文").strip()
                    # v13.88.9L-40 · 空壳检测到了不能只标记·要自愈: 换CDP真浏览器重试一次。
                    #   根因(用户抓AA读不到): curl拿SPA空壳被当成功·守门只标fake不降级→
                    #   永远读不到JS渲染的真内容。检测→自愈接上(建了没接同类)。
                    _via = result.get("via", "")
                    # MEYO_NO_CDP=1(进化上下文)→ 跳过空壳自愈的 CDP 重试·
                    #   宁可读不到也不开浏览器(进化省电优先·用户明令)。
                    if (not _no_cdp) and _via not in ("cdp", "citadel") and prefer != "cdp":
                        _cdp = self._read_url_impl(url, prefer="cdp",
                                                    force_retry=True,
                                                    auto_click_listing=auto_click_listing)
                        if isinstance(_cdp, dict) and _cdp.get("content"):
                            _real2, _ = self.looks_like_real_content(_cdp["content"])
                            if _real2:
                                _cdp["note"] = (_cdp.get("note", "") + " ·空壳自愈:curl空壳→CDP重试成功").strip()
                                return _cdp
        except Exception:
            pass
        return result

    def _read_url_impl(self, url: str, prefer: str = "auto",
                        force_retry: bool = False,
                        auto_click_listing: bool = False) -> dict:
        """按 web-access 哲学读一个 URL(内部实现·出口由 read_url 统一守门)"""
        # v13.90.5 · 通用 PDF 深读分支(用户「答案藏在论文/报告PDF正文·read_url只处理HTML读不到」)
        #   诊断: GAIA 大量错题(EC编号/精确统计/深检索)共性根因 = 答案在论文PDF方法学正文·
        #   不在网页摘要/snippet·而 read_url 只走 HTML extract → 遇PDF拿到乱码/空。
        #   这里【纯增量】前置一道 PDF 探测: URL 以 .pdf 结尾 或 content-type=application/pdf
        #   或抓到内容开头是 %PDF magic → 走 pdfplumber 提取正文。通用(任何PDF URL)·
        #   fail-safe(下载/解析异常不阻断·落回原 HTML 降级链)。[[feedback-truncation-fix-all-consumers]]
        # v13.88.9L · 通用图片OCR分支(用户「GAIA真边界=图表轴标签/截图文字·read_url读不到」)·
        #   前置于PDF/HTML(图片后缀/content-type最明确)。命中→OCR·未命中→None交回下方PDF/HTML。
        #   fail-safe: 任何异常不阻断。[[feedback-built-not-wired-rootfix]]
        try:
            _img_res = self._maybe_read_image(url)
            if _img_res is not None:
                return _img_res
        except Exception:
            pass
        try:
            _pdf_res = self._maybe_read_pdf(url)
            if _pdf_res is not None:
                return _pdf_res
        except Exception:
            pass  # PDF探测/解析任何异常都不阻断·继续走原HTML降级链

        # v13.88.9L-39 · km.sankuai.com 内网学城·需SSO鉴权·普通HTTP/浏览器读不到(403/登录墙)。
        #   系统有 citadel 能读(写KM用了·bridge_to_km:277)·但deep_read没接citadel→
        #   遇KM链接读不到(用户抓「我能读qa_agent读不到」)。这里分流: KM→citadel。
        #   [[feedback-built-not-wired-rootfix]] 读写用两套通道·读那套没接citadel=建了没接。
        if "km.sankuai.com" in url:
            try:
                import re as _re, subprocess as _sp
                _m = _re.search(r'/(?:collabpage|page)/(\d+)', url)
                if _m:
                    _cid = _m.group(1)
                    _r = _sp.run(["oa-skills", "citadel", "getSimpleMarkdown",
                                  "--contentId", _cid],
                                 capture_output=True, text=True, timeout=60)
                    if _r.returncode == 0 and _r.stdout:
                        return {"ok": True, "url": url, "content": _r.stdout[:20000],
                                "via": "citadel", "completeness": "full"}
                    return {"ok": False, "url": url, "content": "",
                            "via": "citadel", "err": f"citadel rc={_r.returncode}"}
            except Exception as _e:
                return {"ok": False, "url": url, "content": "",
                        "via": "citadel", "err": f"{type(_e).__name__}:{str(_e)[:80]}"}

        # v13.90.2 · zhihu.com 域名分流(诚实标局限版·非假装能读):
        #   排查过程: zhihu_official_api.py 有现成官方 API(已配 ZHIHU_ACCESS_SECRET·
        #   search_zhihu_official/search_global_official 实测关键词搜索秒返高质量
        #   结构化内容含vote_up_count)·但只在 search_dag/search_graph 的"搜索规划"
        #   阶段接了(_site_top_action)·本函数(直接给URL读正文)完全不知道这个API
        #   存在→任何调用方直接扔一个 zhihu.com URL 过来都会硬撞登录墙(验证码登录/
        #   密码登录/未注册手机验证)。
        #   实测过 2 种"按URL反查"方案·均不可行(诚实记录·非回避):
        #     方案A: 纯数字 question_id 当 query 搜 → 命中不相关内容(ID不是语义词)。
        #     方案B: filter_expr='host=="zhihu.com"' 过滤 → 结果数=0(host==语法在
        #            这个API上未生效·search_recent()里早有same结论"host==语法暂未
        #            测通"·这次用真实ID+真实关键词都复现同一结论·非偶然)。
        #   结论: 该官方 API 只服务"搜索发现"场景(拿关键词→出候选URL列表)·不服务
        #   "已有确定URL→反查其内容"场景·两者是不同的产品能力·API 没提供后者。
        #   真正的修复方向应在【调用方】: search_dag 等"搜索规划"层已经知道 zhihu
        #   该走 friday_zh API 直接拿内容(hit 里就有 content 字段)·不该再把搜出来
        #   的 zhihu.com URL 二次丢给 read_url 去"抓正文"(这一步注定撞墙且多余)。
        #   本处仅做诚实标注 + 短路省资源: 不再让 zhihu.com/question 或 zhuanlan
        #   URL 去闯 CDP/playwright 全套降级链(反正大概率还是登录墙)·直接 curl 一次
        #   探个成本最低的底·读不到就如实告知调用方"改用搜索"·不做无谓的重型抓取。
        if ("zhihu.com" in url and "/question/" in url) or "zhuanlan.zhihu.com/p/" in url:
            _zh_raw = self.curl_read(url)
            # 注意: looks_like_real_content 吃的是"提取后正文"而非原始HTML·
            # 原始HTML即使是登录墙骨架也会因为带一堆meta/CSRF token凑够200字
            # 逃过太短检测(实测踩过这个坑) → 必须先 extract_readable 再判断。
            _zh_text = self.extract_readable(_zh_raw, url) if _zh_raw else ""
            if _zh_text and self.looks_like_real_content(_zh_text)[0]:
                return {"ok": True, "url": url, "content": _zh_text,
                        "via": "curl", "readable": True,
                        "note": "zhihu.com 游客可见内容(未触发登录墙)"}
            return {
                "ok": False, "url": url, "content": "",
                "via": "zhihu_login_wall_known_limitation", "readable": False,
                "fail_reason": "zhihu.com需登录·官方API不支持按URL精确反查(只支持"
                               "关键词搜索)·CDP硬破解大概率仍是登录墙",
                "suggestion": "改用 zhihu_official_api.search_zhihu_official(关键词) "
                              "直接拿结构化内容·而非拿到URL后再来read_url抓正文",
            }
        # v13.88.9 · 2026-07-30 · 用户「直连不只 github·多少规则·能抓更多站」
        # prefer=auto 时先过 channel_router 统一规则表(16 条·raw/api/curl/cdp)·
        # 改写 URL(如 github→raw)+ 把 auto 升级成具体通道·未知站默认 curl 不乱试。
        # 不覆盖调用方显式指定的 prefer。
        if prefer == "auto":
            try:
                import sys as _s, os as _o
                _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
                from channel_router import route as _route
                _r = _route(url)
                if _r.get("matched"):
                    url = _r["url"]  # 可能已改写(github→raw)
                    _ch = _r["channel"]
                    # raw/curl → curl 直连;api 暂走 curl(结构化留给专门 API 组件);cdp → cdp
                    prefer = "cdp" if _ch == "cdp" else "curl"
            except Exception:
                pass  # 路由挂了不影响原有降级链

        # v13.83 · github.com/X/repo 首选 raw · 用户 R16 「github 反爬 → raw 绕」
        # 只对 github.com/X/repo (2 段 path) 生效·不影响 issues/pulls/tree
        if "://github.com/" in url:
            import re as _re
            m = _re.match(r"^https?://github\.com/([^/]+)/([^/?#]+)/?$", url)
            if m:
                org, repo = m.groups()
                raw_urls = [
                    f"https://raw.githubusercontent.com/{org}/{repo}/main/README.md",
                    f"https://raw.githubusercontent.com/{org}/{repo}/master/README.md",
                ]
                for raw_url in raw_urls:
                    raw_content = self.curl_read(raw_url)
                    if raw_content and len(raw_content) > 200:
                        return {
                            "ok": True, "content": raw_content,
                            "via": "github_raw_fallback",
                            "domain": "raw.githubusercontent.com",
                            "url": url, "raw_url": raw_url,
                            "note": f"github.com/{org}/{repo} 切 raw · 省时且免反爬",
                            "readable": True,
                        }

        # v13.82 · 已知反爬域名·直接切 anysearch·不再硬闯 (用户 R15 "记住")
        if self.is_known_captcha_domain(url):
            try:
                from urllib.parse import parse_qs, urlparse as _up, unquote
                q = parse_qs(_up(url).query)
                search_q = q.get("q", q.get("query", q.get("wd", [""])))[0]
                if search_q and self.anysearch_available():
                    r = self.anysearch_search(unquote(search_q), max_results=10)
                    if r:
                        return {
                            "ok": True, "content": r,
                            "via": "anysearch_known_captcha_domain",
                            "domain": _up(url).netloc, "url": url,
                            "note": f"已记录反爬域名·直接切 anysearch (省时+免封 IP)",
                        }
            except Exception:
                pass

        # v13.90-fix · 用户抓「x.com/t.me 资源不足也要有办法访问·一定要想办法」
        # 已知网络路径阻断域名(TCP/TLS 层连接超时·非反爬)→ 本机 curl/CDP/playwright
        # 走的是同一条被阻断的网络路径·重试/换 timeout 都没用。anysearch 走独立
        # 后端网络出口(不经本机网络栈)·能真正穿透拿到等价内容·而非空手而归。
        if self.is_network_blocked_domain(url) and self.anysearch_available():
            from urllib.parse import urlparse as _up
            _domain = _up(url).netloc
            _extracted = self.anysearch_extract(url)
            if _extracted and len(_extracted) > 100:
                return {
                    "ok": True, "content": _extracted,
                    "via": "anysearch_extract_network_blocked",
                    "domain": _domain, "url": url,
                    "note": "已知网络阻断域名 · anysearch 独立出口直接抽取原页面(绕过本机网络阻断)",
                    "readable": True,
                }
            # 单页抽取失败(比如需要登录态才能看的详情页)→ 退而求其次搜等价内容
            _title_guess = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
            _r = self.anysearch_search(f"{_domain} {_title_guess}", max_results=8) if _title_guess else ""
            if _r:
                return {
                    "ok": True, "content": _r,
                    "via": "anysearch_search_network_blocked",
                    "domain": _domain, "url": url,
                    "note": "已知网络阻断域名 · 单页抽取未果 · 已退而求其次搜索等价内容",
                }

        # v13.81 · Google 反爬 URL 检测·遇到 recaptcha 页直接切 anysearch
        _lu = url.lower()
        if "google.com/sorry" in _lu or "recaptcha" in _lu:
            # 从 continue= 参数抽真实 target
            try:
                from urllib.parse import parse_qs, urlparse as _up, unquote
                q = parse_qs(_up(url).query)
                real_target = q.get("continue", [""])[0]
                if real_target:
                    real_target = unquote(real_target)
                    # 从 Google 搜索 URL 抽 query
                    if "/search?" in real_target:
                        qs = parse_qs(_up(real_target).query)
                        search_q = qs.get("q", [""])[0]
                        if search_q and self.anysearch_available():
                            # 切 anysearch 绕过
                            r = self.anysearch_search(search_q, max_results=10)
                            if r:
                                return {
                                    "ok": True,
                                    "content": r,
                                    "via": "anysearch_bypass_recaptcha",
                                    "domain": "google-bypass",
                                    "url": url,
                                    "note": "google reCAPTCHA detected·auto switched to anysearch",
                                }
            except Exception:
                pass
            # 抽不出·也不硬闯·返 blocked
            return {
                "ok": False,
                "content": "",
                "via": "google_recaptcha_detected",
                "url": url,
                "note": "遇 Google reCAPTCHA · 建议 (1) 用 anysearch (2) 手动过一次验证",
            }
        # v13.48 · Playwright 屏幕外 Chrome 优先 (不打扰用户)
        # v13.47 · 演绎修:列表页自动点击详情
        if auto_click_listing:
            _lu = url.lower()
            _is_listing = any(k in _lu for k in [
                '/search', '/s?', 'q=', '?wd=', 'query=',
                '/topic/', '/tag/', '/tags/', '/category/',
                '/latest', '/hot', '/trending', '/explore',
                '/feed', '/index'
            ])
            _is_detail = any(k in _lu for k in [
                '/article/details/', '/p/', '/post/', '/answer/',
                '/status/', '/item?id='
            ])
            if _is_listing and not _is_detail:
                # v13.48 · 优先 playwright (屏幕外·不弹窗)
                click_r = None
                if self.playwright_available():
                    click_r = self.playwright_click_and_read(url)
                if not (click_r and click_r.get("ok")) and self.browser_use_available():
                    click_r = self.browser_use_click_and_read(url, wait_ms_after_click=5000)
                if click_r and click_r.get("ok") and len(click_r.get("detail_text", "")) > 500:
                    return {
                        "ok": True,
                        "content": f"[列表页] {click_r.get('list_title', '')}\n\n"
                                   f"[点进详情] {click_r.get('detail_title', '')}\n"
                                   f"URL: {click_r.get('detail_url', '')}\n\n"
                                   f"{click_r['detail_text']}",
                        "via": click_r.get("via", "click_and_read"),
                        "domain": urlparse(url).netloc,
                        "url": url,
                        "detail_url": click_r.get("detail_url", ""),
                        "readable": True,
                    }
                # 失败就 fallback

        domain = urlparse(url).netloc
        result = {"ok": False, "content": "", "via": "failed", "domain": domain, "url": url}
        # v13.88.9 · 用户「V2EX 返 0 字·别静默失败·标清楚为啥没抓到」
        # 累积每个通道的失败原因·全败时写进 result·让调用方能诊断+换战术
        _fail_log = []

        # v13.88.x · api_discovery hook (用户 2026-07-24 抓:CDP 硬撞 20s×N 浪费)
        # 已知官方 API 的高反爬域名·先走 API·拿到直接返·跳过整个 for loop。
        # 关闭: MEYO_QA_API_FIRST=0
        if os.environ.get("MEYO_QA_API_FIRST", "1") != "0":
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from api_discovery import discover_api as _disc, try_api_first as _tafirst
                _info = _disc(domain)
                if _info.get("has_api") and _info.get("parser") != "sogou_weixin_bridge":
                    _r = _tafirst(url, domain=domain)
                    # v13.88.9 · 用户抓「以为通了其实空壳」根治前移:
                    # api_discovery 会把 RSS feed 当"有API"·拿回 XML 抢在渲染前返回·
                    # 这里守门:RSS/XML/空壳 不算 api-first 成功·让它继续走渲染降级链。
                    _content = _r.get("content", "") if _r.get("ok") else ""
                    _real, _why = (self.looks_like_real_content(_content)
                                   if _content else (False, "空"))
                    if _real:
                        result.update({
                            "ok": True,
                            "content": _content,
                            "via": _r.get("via", "api_first"),
                            "readable": True,
                            "api_endpoint": _r.get("endpoint", ""),
                            "note": f"api-first · {_r.get('note','')} · 省 CDP (t={_r.get('time_s','?')}s)",
                        })
                        try:
                            self._bookmark_record(url, _r.get("via", "api_first"),
                                                    True, len(_content))
                        except Exception:
                            pass
                        return result
                    # api 拿到的是 RSS/空壳 → 不返回·继续走下面的渲染降级链
            except Exception:
                # 探测失败静默·回退原降级链
                pass

        # v13.28 · blocked 检查改成"同一 URL 最近 3+ 次失败"·而非"整个域名"
        # v13.42 · HN item 走 Firebase API
        if "news.ycombinator.com/item" in url:
            hn_text = self._read_hn_item(url)
            if hn_text and len(hn_text) > 100:
                result.update({"ok": True, "content": hn_text,
                              "via": "hn_algolia_api", "readable": True})
                self._bookmark_record(url, "hn_algolia_api", True, len(hn_text))
                return result

        # v13.62 · HN 首页/newest/best 境内 curl 挂 79s + stealth/playwright 挂
        # 用户 2026-07-13 抓包:HN 域名整站需海外网络·item 走 firebase 通·首页走 Algolia 也通
        _lu2 = url.lower()
        if ("news.ycombinator.com" in _lu2 and
            ("news.ycombinator.com/$" in _lu2 + "$" or
             _lu2.rstrip("/").endswith("news.ycombinator.com") or
             any(p in _lu2 for p in ["/news", "/newest", "/best", "/front", "/ask", "/show"]))):
            try:
                r_hn = subprocess.run(
                    ["curl", "-sS", "--max-time", "8",
                     "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"],
                    capture_output=True, text=True, timeout=10,
                )
                if r_hn.returncode == 0 and r_hn.stdout.strip():
                    import json as _json
                    data = _json.loads(r_hn.stdout)
                    lines = ["# Hacker News · Front Page (via Algolia API)\n"]
                    for h in (data.get("hits") or [])[:30]:
                        title = h.get("title") or h.get("story_title") or ""
                        pts = h.get("points") or 0
                        cmts = h.get("num_comments") or 0
                        u = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
                        lines.append(f"- [{pts}⬆ {cmts}💬] {title}\n  {u}")
                    if len(lines) > 3:
                        result.update({"ok": True, "content": "\n".join(lines)[:20000],
                                       "via": "hn_algolia_frontpage", "readable": True})
                        self._bookmark_record(url, "hn_algolia_frontpage", True,
                                               len(result["content"]))
                        return result
            except Exception:
                pass

        # v13.43 · 硬死链 URL 直接 skip (不管 force_retry)
        # 用户抓包 sspai.com/whoops 404 后加
        dead_patterns = ["/whoops", "/404", "/not-found", "/error-page",
                          "/redirect", "/captcha", "/verify"]
        if any(p in url.lower() for p in dead_patterns):
            result["via"] = "skip_dead_url"
            return result

        if not force_retry:
            path_blocked = self._is_url_path_blocked(url, hours=3, min_incidents=3)
            if path_blocked:
                # v13.88.9 · 用户「别静默失败·标清楚为啥没抓到」
                # skip 也要可诊断:因近 3h 反复失败被跳·带上建议(换URL/换同类/强制重试)
                result["via"] = "skip_url_recently_failed"
                result["fail_reason"] = "该 URL 近 3h 已失败 ≥3 次·主动跳过避免硬撞"
                result["suggestion"] = "换同类 URL 补 / 或 force_retry=True 强抓 / 或该站已死"
                result["note"] = f"跳过 · {result['fail_reason']} · {result['suggestion']}"
                return result

        # v13.14 境内适配 (2026-07-09 · 用户开了 Chrome remote debugging port 9222)：
        # CDP 可用 → CDP 首选 (带登录态·过反爬)·curl 兜底
        # CDP 不可用 → curl 首选·Jina 已从默认路径移除 (境内 DNS 污染)
        if prefer == "browseract":
            order = ["browseract", "firecrawl", "playwright", "curl"]
        elif prefer == "firecrawl":
            order = ["firecrawl", "playwright", "curl"]
        elif prefer == "playwright":
            order = ["playwright", "curl"]
        elif prefer == "browser_use":
            order = ["browser_use", "playwright", "curl"]
        elif prefer == "cdp":
            order = ["cdp", "curl"]
        elif prefer == "jina":
            order = ["jina", "curl"]
        elif prefer == "curl":
            order = ["curl", "playwright"]
        else:  # auto
            # v13.60 · 反爬站 3 层排序 (真跑测过速度/内容):
            #   BrowserAct(11.6s/26k) > Firecrawl(34s/45k) > Playwright(6s/6k)
            # BrowserAct 是速度/内容甜点 · 首选
            # Firecrawl 补内容深度 · 用于关键源
            # Playwright 免费 fallback
            reflex_hard = ('xiaohongshu.com', 'xhslink.com',
                           'mp.weixin.qq.com', 'weixin.qq.com',
                           'zhihu.com', 'weibo.com', 'douyin.com',
                           'bilibili.com', 'xiaobot.net', 'csdn.net',
                           'juejin.cn', 'google.com')
            hard = any(d in domain for d in reflex_hard)
            if hard:
                # v13.70 · 小红书专用 bsk click flow · 反爬顶层
                # (2026-07-14 实测: bsk SPA click → is_404=false·URL → is_404=true)
                order = []
                is_xhs = "xiaohongshu.com" in domain or "xhslink.com" in domain
                if is_xhs and self.bsk_available():
                    order.append("bsk")             # 小红书专线 · click flow 绕反爬

                # v13.61+ · 反爬站 6 层排序:
                if self.webclaw_available():
                    order.append("webclaw")          # 免费·brew 装·Rust 高速
                if self.stealth_scraper_available():
                    order.append("stealth_scraper")  # 免费自建 · 中等反爬够
                # v13.88.18 · 用户 2026-07-24 「BrowserSkill 是垃圾·撤」
                # 撤销 bsk_general · 老 bsk_read_xhs 保留 (仅小红书专线还能用)
                if self.playwright_available():
                    order.append("playwright")       # 免费 · 屏幕外 chrome
                if self.browseract_available():
                    order.append("browseract")       # 付费 tier 2 · 免费额度
                if self.firecrawl_available():
                    order.append("firecrawl")       # 付费 tier 3 · 硬骨头
                if self.browser_use_available():
                    order.append("browser_use")
                order.append("curl")
            else:
                # 普通站:curl 秒返 · 挂了升级
                order = ["curl"]
                if self.webclaw_available():
                    order.append("webclaw")
                if self.stealth_scraper_available():
                    order.append("stealth_scraper")
                order.append("playwright")

        # v40 · P1 · 本地 CDP 类方法 (playwright/cdp/browser_use/bsk) 拿不到闸门就 skip 到下一降级
        # 避免 16 query 全排队等 CDP · 云 API 类 (firecrawl/browseract/stealth) 不受此限
        _LOCAL_CDP_METHODS = {"cdp", "browser_use", "playwright", "bsk"}
        try:
            from perf_guard import try_acquire_local_cdp, release_local_cdp
            _has_semaphore = True
        except Exception:
            _has_semaphore = False

        for m in order:
            raw = ""
            _skip_cdp = False
            if _has_semaphore and m in _LOCAL_CDP_METHODS:
                # 非阻塞尝试 · 拿不到直接跳 · 不排队
                if not try_acquire_local_cdp(timeout=0.0):
                    _skip_cdp = True
            try:
                if _skip_cdp:
                    _fail_log.append(f"{m}:本地CDP闸门忙·跳过")
                    continue
                if m == "curl":
                    raw = self.curl_read(url)
                elif m == "jina":
                    raw = self.jina_read(url)
                elif m == "cdp":
                    if not self.cdp_available():
                        _fail_log.append("cdp:proxy不可用")
                        continue
                    raw = self.cdp_navigate_and_read(url)
                elif m == "browser_use":
                    if not self.browser_use_available():
                        continue
                    raw = self.browser_use_read(url)
                elif m == "playwright":
                    if not self.playwright_available():
                        _fail_log.append("playwright:不可用")
                        continue
                    raw = self.playwright_read(url)
                elif m == "firecrawl":
                    if not self.firecrawl_available():
                        continue
                    raw = self.firecrawl_read(url)
                elif m == "browseract":
                    if not self.browseract_available():
                        continue
                    raw = self.browseract_read(url)
                elif m == "stealth_scraper":
                    if not self.stealth_scraper_available():
                        continue
                    raw = self.stealth_scraper_read(url)
                elif m == "webclaw":
                    if not self.webclaw_available():
                        continue
                    raw = self.webclaw_read(url)
                elif m == "bsk":
                    if not self.bsk_available():
                        continue
                    raw = self.bsk_read_xhs(url)
                # v13.88.18 · bsk_general 撤 (用户「BrowserSkill 是垃圾·撤」)
                else:
                    continue
            finally:
                if _has_semaphore and m in _LOCAL_CDP_METHODS and not _skip_cdp:
                    release_local_cdp()
            if not raw:
                # v13.90 · P2 修: 优先用 _record_incident 记的真实失败原因
                # (curl_fail/webclaw_stderr/playwright_exc/timeout 等)·
                # 不再统一压成没有信息量的"{m}:空返回"·让告警能诊断真根因
                _last_kind = getattr(self, "_last_incident_kind", None)
                _last_detail = getattr(self, "_last_incident_detail", "")
                if _last_kind:
                    _fail_log.append(f"{m}:{_last_kind}" + (f"({_last_detail[:60]})" if _last_detail else ""))
                    self._last_incident_kind = None  # 消费后清空·避免下一通道误用旧原因
                    self._last_incident_detail = ""
                else:
                    _fail_log.append(f"{m}:空返回")
                continue
            # 智能正文提取 (HTML → text) · CDP 已经是 innerText 就跳过
            if m in ("curl",) and raw.lstrip().startswith("<"):
                readable = self.extract_readable(raw, url)
                # v13.88.9 · 守门:抽出来的是不是真正文?空壳/XML → 不算通·继续降级
                _real, _why = self.looks_like_real_content(readable) if readable else (False, "空")
                if readable and _real:
                    result.update({"ok": True, "content": readable, "raw_html_len": len(raw),
                                   "via": m, "readable": True})
                    self._bookmark_record(url, m, True, len(readable))
                    return result
                # 抽取出来是空壳/XML → 不 return·让 order 继续降级到 playwright 渲染
                if readable and not _real:
                    self._bookmark_record(url, m, False, 0)
                    _fail_log.append(f"{m}:空壳/{_why}")
                    continue
                # 抽取失败·但 raw 有内容·退回原文
                result.update({"ok": True, "content": raw[:20000], "via": m, "readable": False})
                self._bookmark_record(url, m, True, min(len(raw), 20000))
                return result
            result.update({"ok": True, "content": raw, "via": m, "readable": True})
            self._bookmark_record(url, m, True, len(raw))
            # v13.81+ · 返回前检测反爬内容
            return self._check_anti_bot_content(result, url)
        # 全降级失败 · 也记 · rating 下调交给 rate()
        # v13.88.9 · 用户「V2EX 返 0 字·别静默失败」· 补可诊断信息:
        # 哪些通道试过·各自为啥挂·让调用方能换战术(换 URL/避开该站)·而非干瘪 failed
        self._bookmark_record(url, "failed", False, 0)
        _tried = [m for m in order]
        result["attempts"] = _fail_log
        result["tried_channels"] = _tried
        result["fail_reason"] = (" · ".join(_fail_log) if _fail_log
                                 else f"全部 {len(_tried)} 通道未产出内容(可能反爬/URL失效/渲染未起)")
        result["note"] = f"抓取失败 · 试过 {_tried} · {result['fail_reason'][:120]}"
        # v13.88.81 · 结构性修复: 告警分级 · 避免"合理失败"污染告警池
        #   根因: 测试假域名(NXDOMAIN)、需认证站点(code.claude.com)、example.com 空壳
        #   这些都不是系统 bug·但之前全部走 WARN 级告警 → 告警池被噪声淹没
        #   修复: 分类失败原因·只对"真实异常"(原本可访问的站突然挂了)发 WARN
        #         合理失败降为 INFO·测试环境跳过
        _fail_class = self._classify_fail(domain, url, _fail_log)
        try:
            from alert_bus import notify as _n
            if _fail_class == "test_env":
                pass  # pytest 环境·不发告警
            elif _fail_class in ("nxdomain", "test_domain", "auth_required", "empty_url"):
                _n("web_access_fail", "INFO",
                   f"{domain} 全通道失败(合理) · {_fail_class} · {result['fail_reason'][:60]}",
                   throttle_min=60)
            else:
                # v13.90.1 · 结构性补全: real_fail 若这次失败恰好让该域名达到
                # 网络阻断学习阈值(_record_conn_level_fail 在 curl_read 内部已先
                # 记过)·下次 read_url 会被 is_network_blocked_domain 前置拦截
                # 直接走 anysearch 绕道·不会再走到这条 WARN。文案里标注清楚·
                # 避免值班人员误以为"同一域名反复告警=没人管"。
                _already_learned = self.is_network_blocked_domain(url, min_count=3)
                _suffix = " · 已学习为网络阻断域名·下次自动切anysearch绕道" if _already_learned else ""
                _n("web_access_fail", "WARN",
                   f"{domain} 全通道失败 · {result['fail_reason'][:80]}{_suffix}",
                   throttle_min=30)
        except Exception:
            pass
        # v13.88.9 · 用户「按闭环该搜外部·不懂就搜·事件驱动非 cron」
        # 抓取全败=事件 → 节流通过则【当场】fire-and-forget 后台进程去 learn·
        # 搜「这个站怎么抓/有没有别的入口」·不阻塞抓取返回·不等结果。
        # 节流:同域名 6h 只触发一次(V2EX 死链反复失败不会雪崩)。
        try:
            self._fire_fetch_fail_learn(domain, url, result["fail_reason"])
        except Exception:
            pass
        return result

    # v13.88.81 · 失败分类器 · 区分"合理失败"和"真实异常"
    # 避免 NXDOMAIN/测试域名/需认证站点 等合理失败污染 WARN 告警池
    _TEST_DOMAIN_PATTERNS = (
        "does-not-exist", "dead-site", "zzz999", "cn-test.com",
        "example.com", "example.org", "test.com", "localhost",
        "127.0.0.1", "fake-domain", "nonexistent",
    )
    _AUTH_REQUIRED_DOMAINS = (
        "code.claude.com", "claude.ai", "chat.openai.com",
        "chatgpt.com", "login", "sso.", "auth.",
    )

    def _classify_fail(self, domain: str, url: str, fail_log: list) -> str:
        """分类抓取失败的原因类型·用于决定告警级别。
        返回值:
          test_env       - pytest/CI 环境
          nxdomain       - 域名不存在 (DNS 解析失败)
          test_domain    - 测试用假域名
          auth_required  - 需认证/登录态的站点
          empty_url      - URL 为空
          real_fail      - 真实异常 (原本可访问的站挂了)
        """
        # 测试环境
        if any(k in os.environ for k in ("PYTEST_CURRENT_TEST", "CI_TEST_MODE", "_DRY_RUN")):
            return "test_env"
        # 空 URL
        if not domain or not url:
            return "empty_url"
        _dl = domain.lower()
        # 测试域名
        if any(p in _dl for p in self._TEST_DOMAIN_PATTERNS):
            return "test_domain"
        # 需认证域名
        if any(p in _dl for p in self._AUTH_REQUIRED_DOMAINS):
            return "auth_required"
        # DNS 解析失败 (NXDOMAIN) · 从 fail_log 检测
        _fail_text = " ".join(fail_log).lower()
        if any(k in _fail_text for k in ("nxdomain", "could not resolve", "name resolution",
                                          "dns", "no such host")):
            return "nxdomain"
        return "real_fail"

    _FETCH_FAIL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", ".fetch_fail_learn_log.jsonl")

    _FETCH_FAIL_QUEUE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".fetch_fail_queue.jsonl")

    def _fire_fetch_fail_learn(self, domain: str, url: str, reason: str):
        """抓取全败=事件 → 节流通过则【入待学队列+敲神经中枢门铃】·中枢批量消费。
        神经链层三(v13.88.9):不再 spawn 任何后台进程(历史进程雪崩教训·
        feedback-process-avalanche:几十进程互相 spawn 把机器干到 load 200+)·
        只写文件(入队+门铃)·唯一常驻的神经中枢 1 进程去派 fetch_fail_learner --consume。
        事件驱动(非 cron 轮询)·同域名 6h 只触发一次(死链反复失败仍不雪崩·节流是防雪崩核心)。"""
        import time as _t
        log = os.path.abspath(self._FETCH_FAIL_LOG)
        cutoff = _t.time() - 6 * 3600
        # 节流:同域名 6h 内已触发 → 跳(读触发日志)。★ 防雪崩核心·勿弱化:
        # 同一死链反复失败只入队一次·避免队列被单站刷爆。
        try:
            if os.path.exists(log):
                for line in open(log, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r.get("domain") == domain and r.get("epoch", 0) > cutoff:
                            return
                    except Exception:
                        continue
        except Exception:
            pass
        _epoch = _t.time()
        # 记触发日志(节流依据·6h 判重就靠它)
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"epoch": _epoch, "ts": datetime.now().isoformat(),
                                    "domain": domain, "url": url,
                                    "reason": reason[:200]}, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # ① 入待学队列(不 spawn·只 append 一行)·中枢派 fetch_fail_learner --consume 批量学
        #    (三步闭环:精准搜→校验→回写 channel_router)。入队失败静默·不影响抓取返回。
        try:
            q = os.path.abspath(self._FETCH_FAIL_QUEUE)
            with open(q, "a", encoding="utf-8") as f:
                f.write(json.dumps({"domain": domain, "url": url,
                                    "reason": reason[:200], "epoch": _epoch,
                                    "ts": datetime.now().isoformat()},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
        # ② 敲神经中枢门铃(fetch_learn 环)·中枢秒级感知→派 --consume。健壮 import+静默。
        try:
            import sys as _sys
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in _sys.path:
                _sys.path.insert(0, _here)
            import neuro_signal as _ns
            _ns.mark("fetch_learn", source=f"fetch_fail:{domain}")
        except Exception:
            pass

    # v13.82 · 反爬 lesson 库 · 用户 R15 「hook 一下·下次记住」
    _CAPTCHA_LESSON_FILE = os.path.expanduser("~/.claude/.captcha_domains.json")
    _known_captcha_domains = None

    def _load_captcha_lessons(self):
        """加载已知反爬域名·避免下次硬闯"""
        if self._known_captcha_domains is not None:
            return self._known_captcha_domains
        try:
            if os.path.exists(self._CAPTCHA_LESSON_FILE):
                with open(self._CAPTCHA_LESSON_FILE) as f:
                    self._known_captcha_domains = set(json.load(f).get("domains", []))
            else:
                self._known_captcha_domains = set()
        except Exception:
            self._known_captcha_domains = set()
        return self._known_captcha_domains

    def _save_captcha_lesson(self, domain: str):
        """记住这个域名被反爬过"""
        try:
            self._load_captcha_lessons()
            self._known_captcha_domains.add(domain)
            os.makedirs(os.path.dirname(self._CAPTCHA_LESSON_FILE), exist_ok=True)
            with open(self._CAPTCHA_LESSON_FILE, "w") as f:
                json.dump({"domains": sorted(self._known_captcha_domains),
                          "updated": datetime.now().isoformat()}, f, indent=2)
        except Exception:
            pass

    def is_known_captcha_domain(self, url: str) -> bool:
        """已知反爬域名·下次同域名直接切绕道·不硬闯"""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            return domain in self._load_captcha_lessons()
        except Exception:
            return False

    # v13.88.82 · 结构性根治: 反爬检测"三头蛇"问题
    #   根因(用户抓截图 dl.dell.com/alphascience.blog 反复 captcha 却从不绕道):
    #   全仓有 3 处独立的反爬关键词检测(curl_read L379 / _check_anti_bot_content
    #   L1311 / cdp_navigate_and_read L2659) · 三份关键词表互不相同也互不联动。
    #   只有 _check_anti_bot_content 那一处会调 _save_captcha_lesson"记住并下次
    #   绕道" · 但它的关键词表最窄(没有 cloudflare/access denied) · 导致 curl_read
    #   已经用更宽的关键词表识别出了 captcha(kind=captcha 写进 incident log)·
    #   却从来没有真正被"记住" → 同一域名每次都重新硬闯一遍、每次都触发告警。
    #   修复: 统一成 1 份关键词表 + 1 个检测函数 · 任何调用点识别到反爬信号都
    #   直接触发学习(而不是只有 1/3 的调用点会学)。
    #
    # v13.90.3 · 结构性根治(第二只头): "验证码反爬"表统一了·"登录墙"表还是
    #   独立的另一份·同一类"三头蛇"问题在另一条支线重演。
    #   排查(用户抓 v11_race 告警「login_state 类采集报错连续5次」):
    #   证据链 = search_agent.search_dag() → search_dag.run_dag() 抓证据 → 完全
    #   不做"是否是登录墙"检测 → 含"Not logged in"的登录墙原文被当正文塞进
    #   external_evidence → 传进 write_experience_post 生成整篇帖子 → 帖子写完
    #   *之后* v11_race.py 的 _COLLECT_ERROR_MARKERS 硬门才拦下来(事后补救)。
    #   代价: 每命中一次都白跑一整轮 racer 并行 + LLM 写作(分钟级算力)才拦截·
    #   而且告警文案「需要手动重新登录对应平台」具有误导性——真正原因不是任何
    #   外部平台账号登录失效·而是抓证据阶段没有源头过滤登录墙内容。
    #   本表(_ANTI_BOT_SIGNALS)只覆盖验证码/限流类反爬·不含登录墙类信号·
    #   是这条链路"抓证据时判断不出登录墙"的直接原因(该查的表压根没这些词)。
    #   修复: 把 v11_race.py 的 _COLLECT_ERROR_MARKERS 合并进本表(单一真源)·
    #   detect_and_learn_anti_bot 命中登录墙关键词也会记住该域名+下次绕道·
    #   使"证据抓取阶段"和"发帖前事后硬门"共享同一份判断标准，不再各自维护。
    _ANTI_BOT_SIGNALS = (
        "unusual traffic from your computer",
        "系统检测到您的计算机网络中存在异常流量",
        "recaptcha",
        "进行人机身份验证",
        "please complete the security check",
        "cloudflare", "just a moment", "access denied",
        "请开启 javascript", "verify you are human",
        "请求太频繁", "请稍后再试", "验证码", "安全验证",
        "you are being rate limited", "too many requests",
        "please slow down",
        # v13.90.3 · 登录墙类(与 v11_race.py _COLLECT_ERROR_MARKERS 保持同源)
        "not logged in", "please run /login", "login required",
        "请先登录", "请登录", "未登录",
        "401 unauthorized", "authentication required",
    )

    def detect_and_learn_anti_bot(self, url: str, content: str) -> bool:
        """统一反爬检测入口 · 命中任一信号立刻记住该域名(供 is_known_captcha_domain
        下次直接绕道用) · 返回 True 表示识别到反爬 · 调用方应视为抓取失败处理。
        所有走 curl/cdp/check_anti_bot 的地方都该走这一个函数·不再各自维护
        互不联动的关键词表副本。"""
        if not content:
            return False
        head = content[:3000].lower()
        if not any(sig in head for sig in self._ANTI_BOT_SIGNALS):
            return False
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            self._save_captcha_lesson(domain)
        except Exception:
            pass
        return True

    # v13.90-fix · 用户抓「x.com/t.me 全通道失败·资源不足也要有办法访问·一定要想办法」
    # 根因排查(curl -v 实测): TCP 三次握手成功、TLS ClientHello 已发出后卡死直到
    # 超时(exit 28) —— 这是网络层连接阻断的特征(非 captcha/反爬也非负载问题)·
    # 不管怎么调 timeout 或换 curl/CDP/playwright 通道都没用(同机同网络路径)。
    # 治本: 不是"重试同战术"，是"识别出这类站→直接换到不受本机网络路径限制的
    # 通道"(anysearch 走独立后端网络出口，不受本机连接阻断影响)。
    # 复用 captcha lesson 的"学习并记住"哲学·但触发信号不同:
    #   captcha = 拿到内容后检测到验证码特征文本
    #   网络阻断 = curl exit code 属于连接层失败(6 DNS/7 拒连/28 超时)且连续命中
    _BLOCKED_LESSON_FILE = os.path.expanduser("~/.claude/.network_blocked_domains.json")
    _CONN_LEVEL_CURL_CODES = {6, 7, 28, 35, 56}  # DNS失败/拒连/超时/SSL/收发失败
    _known_blocked_domains = None

    def _load_blocked_lessons(self):
        if self._known_blocked_domains is not None:
            return self._known_blocked_domains
        try:
            if os.path.exists(self._BLOCKED_LESSON_FILE):
                with open(self._BLOCKED_LESSON_FILE) as f:
                    self._known_blocked_domains = json.load(f).get("domains", {})
            else:
                self._known_blocked_domains = {}
        except Exception:
            self._known_blocked_domains = {}
        return self._known_blocked_domains

    def _record_conn_level_fail(self, domain: str, curl_exit_code: int):
        """记一次连接层失败(非内容层)·累计到阈值(3次)才判定为「网络阻断域名」
        (避免偶发丢包/单次网络抖动就误判成永久阻断)。
        v13.90.1 · 修复: domain 为空(URL parse 失败/裸 IP 无 netloc 等边缘场景)
        不该被记成 "" 这个假域名·会污染 lesson 库且永远匹配不到真实 URL。"""
        if not domain or curl_exit_code not in self._CONN_LEVEL_CURL_CODES:
            return
        try:
            self._load_blocked_lessons()
            rec = self._known_blocked_domains.get(domain, {"count": 0})
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last_ts"] = datetime.now().isoformat()
            rec["last_exit_code"] = curl_exit_code
            self._known_blocked_domains[domain] = rec
            os.makedirs(os.path.dirname(self._BLOCKED_LESSON_FILE), exist_ok=True)
            with open(self._BLOCKED_LESSON_FILE, "w") as f:
                json.dump({"domains": self._known_blocked_domains,
                          "updated": datetime.now().isoformat()}, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def is_network_blocked_domain(self, url: str, min_count: int = 3) -> bool:
        """连续 min_count 次连接层失败(非内容层反爬)→ 判定网络路径阻断·
        下次直接绕道 anysearch(独立网络出口)·别再对着同一堵墙撞 timeout。"""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            rec = self._load_blocked_lessons().get(domain)
            return bool(rec and rec.get("count", 0) >= min_count)
        except Exception:
            return False

    def _check_anti_bot_content(self, result: dict, url: str) -> dict:
        """v13.81 · 检测响应内容里的反爬信号·若是 Google 就切 anysearch
        v13.82 · 记住反爬域名·下次先绕道 (用户 R15 要求"记住怎么进化")

        v13.88.82 · 结构性修复: 改用统一 detect_and_learn_anti_bot (关键词表
        扩到含 cloudflare/access denied 等·之前这里关键词表最窄却是唯一真正
        触发"记住"的地方·导致 curl_read 已识别的 captcha 从未被记住)。
        """
        content = result.get("content", "") or ""
        is_captcha = self.detect_and_learn_anti_bot(url, content)  # 内部已做记忆
        if not is_captcha:
            return result
        # 尝试从 URL 抽 query 切 anysearch
        try:
            from urllib.parse import urlparse as _up, parse_qs
            parsed = _up(url)
            q_params = parse_qs(parsed.query)
            query = q_params.get("q", q_params.get("query", q_params.get("wd", [""])))[0]
            if not query and "search" in parsed.path.lower():
                # 从 path 段抽 · 例 /search/AI+副业
                query = parsed.path.split("/")[-1].replace("+", " ")
            if query and self.anysearch_available():
                r = self.anysearch_search(query, max_results=10)
                if r and len(r) > 500:
                    return {
                        "ok": True,
                        "content": r,
                        "via": "anysearch_bypass_content_captcha",
                        "domain": "captcha-bypass",
                        "url": url,
                        "note": f"检测到反爬内容·自动切 anysearch (原 URL: {url[:80]})",
                        "readable": True,
                    }
        except Exception:
            pass
        # 抽不出·标记 blocked
        result["ok"] = False
        result["via"] = "captcha_blocked"
        result["note"] = "遇反爬·anysearch 也无法绕道·建议手动过验证"
        return result

    def _bookmark_record(self, url, via, ok, content_len):
        """v13.63 · 站点收藏 hook · 每次抓取累积画像 · 失败不阻塞"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from site_bookmark import record as _br
            _br(url, via, ok, content_len, 0.0)
        except Exception:
            pass

    # ─────────────────────────────────────────
    # v13.61++ · smart_search · Agent 专用统一搜索层
    # 2026-07-13 · AnySearch PH 榜 Top 1·17 垂直源意图路由
    # ─────────────────────────────────────────
    def smart_search(self, query: str, max_results: int = 5,
                      domain: str = "auto", fallback: bool = True) -> dict:
        """统一搜索入口 · AnySearch 优先 · 降级 webclaw + Bing
        domain: auto(自动路由) / general / code / finance / legal / social_media / academic / security
        返回:
        {
          "ok": bool,
          "content": markdown 结构化正文,
          "via": "anysearch" | "webclaw+bing" | "curl+bing",
          "took_ms": int,
          "n_results": int,
        }
        """
        import time as _t
        t0 = _t.time()
        # 意图路由 · auto = 关键词匹配
        if domain == "auto":
            q_lower = query.lower()
            if any(k in q_lower for k in ["代码", "code", "github", "sdk", "api"]):
                domain = "code"
            elif any(k in q_lower for k in ["公司", "工商", "融资", "投资", "商业"]):
                domain = "business"
            elif any(k in q_lower for k in ["社交", "reddit", "微博", "小红书", "twitter", "x."]):
                domain = "social_media"
            elif any(k in q_lower for k in ["法律", "合规", "监管"]):
                domain = "legal"
            elif any(k in q_lower for k in ["论文", "arxiv", "paper", "academic"]):
                domain = "academic"
            else:
                domain = "general"
        # tier 1 · AnySearch
        if self.anysearch_available():
            r = self.anysearch_search(query, max_results=max_results, domain=domain)
            if r and len(r) > 500:
                # 计数结果条目
                n = r.count("### ")
                return {
                    "ok": True,
                    "content": r,
                    "via": "anysearch",
                    "took_ms": int((_t.time() - t0) * 1000),
                    "n_results": n,
                    "domain": domain,
                }
        # tier 2 · webclaw + Bing
        if fallback and self.webclaw_available():
            import urllib.parse
            bing_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            r = self.webclaw_read(bing_url, max_chars=15000)
            if r and len(r) > 500:
                return {
                    "ok": True,
                    "content": r,
                    "via": "webclaw+bing",
                    "took_ms": int((_t.time() - t0) * 1000),
                    "n_results": 0,  # 是搜索结果页·没抽标题
                    "domain": domain,
                }
        return {"ok": False, "content": "", "via": "none", "took_ms": int((_t.time() - t0) * 1000), "n_results": 0}

    # ─────────────────────────────────────────
    # 批量读 · 给 agent_search 用
    # ─────────────────────────────────────────
    def read_urls(self, urls: list, max_workers: int = 2, prefer: str = "auto",
                  batch_timeout: int = 90) -> list:
        # v13.20 · 降 6→2 · 用户"电脑卡"投诉后加 (保持·不盲目提并发)
        # v13.88.9 · 整批 90s 超时→未完 future 结果丢。改: 超时后 cancel 未完·标记
        #   which 条 timeout·让上游知道丢了谁 (不再静默整批丢)
        """批量读 · 返回和 urls 同顺序的 dict 列表 · 超时项标 via=batch_timeout"""
        import concurrent.futures
        results = [None] * len(urls)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            fut_to_idx = {pool.submit(self.read_url, u, prefer): i for i, u in enumerate(urls)}
            try:
                for f in concurrent.futures.as_completed(fut_to_idx, timeout=batch_timeout):
                    idx = fut_to_idx[f]
                    try:
                        results[idx] = f.result()
                    except Exception:
                        results[idx] = {"ok": False, "content": "", "via": "exception",
                                        "domain": urlparse(urls[idx]).netloc, "url": urls[idx]}
            except concurrent.futures.TimeoutError:
                # 批超时·cancel 未完成的·未完的标 via=batch_timeout (不静默丢·记 URL)
                for fut, idx in fut_to_idx.items():
                    if results[idx] is None:
                        fut.cancel()
                        results[idx] = {"ok": False, "content": "", "via": "batch_timeout",
                                        "domain": urlparse(urls[idx]).netloc, "url": urls[idx]}
        return [r or {"ok": False, "via": "timeout"} for r in results]

    # ─────────────────────────────────────────
    # CDP 桥（如果 Chrome/Edge 装了且开了 debug 端口 · 就把请求转到 web-access CDP proxy）
    # ─────────────────────────────────────────
    def cdp_available(self) -> bool:
        """检查 web-access CDP proxy 是否活着"""
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "2", "http://localhost:3457/health"],
                capture_output=True, text=True, timeout=3,
            )
            return r.returncode == 0 and "ok" in (r.stdout or "").lower()
        except Exception:
            return False

    # ─────────────────────────────────────────
    # v13.48 · Playwright + 独立 debug Chrome (端口 9224 · 屏幕外)
    # 用户 2026-07-10 loop 抓包:
    #   - 一直问调试权限 (browser-use 每次弹) → 换独立 profile 一劳永逸
    #   - 弹空白页 / 打断窗口 → --window-position=-2400,-2400 藏屏幕外
    #   - 不许一直切用户焦点 → 独立 Chrome 进程·headed 但屏幕外
    # 启动命令(手动跑一次·终身有效·除非 kill):
    #   nohup "/Applications/Google Chrome.app/.../Google Chrome" \
    #     --remote-debugging-port=9224 --user-data-dir=~/.meyo/debug-chrome-profile \
    #     --window-position=-2400,-2400 --disable-notifications \
    #     --disable-blink-features=AutomationControlled &
    # ─────────────────────────────────────────
    _PLAYWRIGHT_CDP_URL = "http://localhost:9224"

    # ─────────────────────────────────────────
    # v13.59 · Scrapegraph-ai 结构化后端 (2026-07-13)
    # 用户 2026-07-13 提供爆火 Scrapegraph-ai · GitHub 万 star
    # 定位:不是替代抓取·是补 LLM 结构化输出这一段
    # 组合:web_access_bridge 拿 raw → scrapegraph JSONScraperGraph 结构化
    # ─────────────────────────────────────────
    def scrapegraph_available(self) -> bool:
        """检查:scrapegraphai 装了 + langchain-anthropic 装了 + 有 LLM auth"""
        try:
            import scrapegraphai  # noqa
            import langchain_anthropic  # noqa
        except ImportError:
            return False
        # 用 claude-code-router 本地 auth
        return bool(os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or
                    os.environ.get("ANTHROPIC_API_KEY", ""))

    def scrapegraph_structured_extract(self, url_or_text: str, prompt: str,
                                         is_url: bool = True,
                                         max_chars: int = 20000) -> dict:
        """v13.59 · 用 raw text 或 URL + Scrapegraph 抽结构化数据
        典型用法:先 read_url 拿 text·再 structured_extract 抽

        用户原文:'列出页面中所有的文章标题和发布日期'
        返回: {ok, data, via, t_fetch, t_extract}
        """
        result = {"ok": False, "data": None, "via": "scrapegraph"}
        if not self.scrapegraph_available():
            result["reason"] = "scrapegraph not available"
            return result
        try:
            import time as __t
            import json as __j

            # 拿 raw text
            if is_url:
                t_fetch = __t.time()
                r_fetch = self.read_url(url_or_text, prefer='auto')
                result["t_fetch"] = round(__t.time() - t_fetch, 1)
                if not r_fetch.get('ok'):
                    result["reason"] = f"fetch fail via={r_fetch.get('via')}"
                    return result
                text = (r_fetch.get('content') or '')[:max_chars]
            else:
                text = url_or_text[:max_chars]

            # 结构化
            from scrapegraphai.graphs import JSONScraperGraph
            auth = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or \
                   os.environ.get("ANTHROPIC_API_KEY", "")
            base = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:3456")
            graph_config = {
                "llm": {
                    "api_key": auth,
                    "model": "anthropic/claude-3-5-haiku-latest",
                    "base_url": base,
                },
                "verbose": False,
            }
            source = __j.dumps({"page_content": text}, ensure_ascii=False)
            t_extract = __t.time()
            g = JSONScraperGraph(prompt=prompt, source=source, config=graph_config)
            data = g.run()
            result["t_extract"] = round(__t.time() - t_extract, 1)
            result["data"] = data
            result["ok"] = bool(data)
            return result
        except Exception as e:
            result["reason"] = f"exc: {type(e).__name__}: {str(e)[:200]}"
            return result

    # ─────────────────────────────────────────
    # v13.58 · Firecrawl 后端 (2026-07-13)
    # 用户 2026-07-13 提供 Firecrawl 评估要求·反爬最强 P95 3.4s 96% 覆盖
    # 现在系统痛点:CDP 频繁 no_tid · Google/Zhihu 挡 · 反爬吃力
    # 策略:反爬站首选 Firecrawl · 保留 playwright/browser_use/curl 作 fallback
    # API key 从环境变量 FIRECRAWL_API_KEY 读 · 无 key 优雅降级
    # ─────────────────────────────────────────
    def firecrawl_available(self) -> bool:
        """检查:firecrawl-py 装了 + 有 API key"""
        try:
            import firecrawl  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())

    def firecrawl_read(self, url: str, formats: list = None,
                        max_chars: int = 30000) -> str:
        """v13.58 · 用 Firecrawl 抓 URL · 返回 markdown"""
        if not self.firecrawl_available():
            return ""
        try:
            from firecrawl import Firecrawl
            app = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
            doc = app.scrape(url, formats=formats or ["markdown"])
            content = ""
            if hasattr(doc, "markdown"):
                content = doc.markdown or ""
            elif hasattr(doc, "data"):
                content = str(doc.data)[:max_chars]
            elif isinstance(doc, dict):
                content = doc.get("markdown", "") or doc.get("content", "")
            return (content or "")[:max_chars]
        except Exception as e:
            self._record_incident(url, "firecrawl_exc", str(e)[:200])
            return ""

    def firecrawl_agent_search(self, prompt: str, max_chars: int = 20000) -> dict:
        """v13.58 · 让 Firecrawl agent 自主搜 · 一句话搞定
        用户原文:'找到 Notion 的定价方案' 这种描述性 prompt
        免费 tier 每日 5 次"""
        if not self.firecrawl_available():
            return {"ok": False, "reason": "no key"}
        try:
            from firecrawl import Firecrawl
            app = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
            r = app.agent(prompt=prompt)
            data = r.data if hasattr(r, "data") else r
            return {"ok": True, "data": str(data)[:max_chars]}
        except Exception as e:
            return {"ok": False, "reason": str(e)[:200]}

    # ─────────────────────────────────────────
    # v13.60 · BrowserAct 后端 (2026-07-13)
    # 真跑对比:11.6s / 26768字 · 甜点位置(Playwright 6584 / Firecrawl 45354)
    # stealth-extract 独立 · 无需 session · 反爬强 · 云端处理
    # ─────────────────────────────────────────
    # browser-act 二进制:上线不依赖开发机绝对路径。优先 env·再 PATH·再常见安装位。
    #   缺失时下方 os.path.exists 检查会优雅跳过该后端(不崩)。
    _BROWSER_ACT_BIN = (
        os.environ.get("MEYO_BROWSER_ACT_BIN")
        or __import__("shutil").which("browser-act")
        or os.path.expanduser("~/.local/bin/browser-act"))

    # ─────────────────────────────────────────
    # v13.61 · 自建 stealth 后端 (免费·内化 Firecrawl 原理)
    # 用户 2026-07-13:「我们不想花钱·搜索一下能不能自己搭建」
    # 从 firecrawl 开源代码抄:playwright + UA轮换 + stealth补丁 + html2text
    # 免费·反爬能力≈70% Firecrawl (差在没云端代理池)
    # ─────────────────────────────────────────
    def stealth_scraper_available(self) -> bool:
        """检查:自建 stealth 依赖装了"""
        try:
            import playwright, playwright_stealth, fake_useragent, html2text  # noqa
            return True
        except ImportError:
            return False

    def stealth_scraper_read(self, url: str, max_chars: int = 30000,
                              output_format: str = "markdown") -> str:
        """免费自建版 · 效果次于 Firecrawl 但反爬中等站够用"""
        if not self.stealth_scraper_available():
            return ""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from stealth_scraper import stealth_scrape
            r = stealth_scrape(url, output_format=output_format)
            if r.get("ok"):
                return (r.get("content") or "")[:max_chars]
        except Exception as e:
            self._record_incident(url, "stealth_scraper_exc", str(e)[:200])
        return ""

    # ─────────────────────────────────────────
    # v13.61+ · AnySearch · Agent 专用搜索 · 2026-07-13 PH 榜 Top 1
    # 真跑对比:1.5-2.5s · 结构化 markdown 8k 字 · 17 垂直源意图路由
    # 强于其他一切:同源衰减+信息密度仲裁+混合排序 3 独有算法
    # 免费匿名 5/min · 注册后 20/min · quota 无限
    # 硬站(知乎)靠 anysearch 也可能通(它有专门爬取通道)
    # ─────────────────────────────────────────
    _ANYSEARCH_CLI = os.path.expanduser("~/.claude/skills/anysearch/scripts/anysearch_cli.py")

    def anysearch_available(self) -> bool:
        """检查:anysearch CLI 存在"""
        return os.path.exists(self._ANYSEARCH_CLI)

    def anysearch_search(self, query: str, max_results: int = 5,
                         domain: str = "general") -> str:
        """AnySearch 意图路由搜索 · 返回 markdown 结构化正文
        domain: general/code/finance/legal/social_media/security/academic 等 17 类
        """
        if not self.anysearch_available():
            return ""
        try:
            env = os.environ.copy()
            # 从多路兜底拿 key
            key = env.get("ANYSEARCH_API_KEY") or "as_sk_014e9d9a51d7bdd0729ed558816a1277"
            env["ANYSEARCH_API_KEY"] = key
            r = subprocess.run(
                ["python3", self._ANYSEARCH_CLI, "search", query,
                 "--max_results", str(max_results),
                 "--domain", domain],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
            if r.stderr:
                self._record_incident(query, "anysearch_stderr", r.stderr[:200])
        except subprocess.TimeoutExpired:
            self._record_incident(query, "anysearch_timeout", "30s")
        except Exception as e:
            self._record_incident(query, "anysearch_exc", str(e)[:200])
        return ""

    def anysearch_extract(self, url: str, max_chars: int = 30000) -> str:
        """AnySearch extract · 单页 markdown 抽取"""
        if not self.anysearch_available():
            return ""
        try:
            env = os.environ.copy()
            key = env.get("ANYSEARCH_API_KEY") or "as_sk_014e9d9a51d7bdd0729ed558816a1277"
            env["ANYSEARCH_API_KEY"] = key
            r = subprocess.run(
                ["python3", self._ANYSEARCH_CLI, "extract", url],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout[:max_chars]
            if r.stderr:
                self._record_incident(url, "anysearch_extract_stderr", r.stderr[:200])
        except subprocess.TimeoutExpired:
            self._record_incident(url, "anysearch_extract_timeout", "30s")
        except Exception as e:
            self._record_incident(url, "anysearch_extract_exc", str(e)[:200])
        return ""

    # ─────────────────────────────────────────
    # v13.61+ · webclaw · Rust MCP 爬虫 (2026-07-09 brew 装)
    # 真跑对比:CSDN 0.58s / 51k 字·GitHub 1.1s / 9k 字·知乎 0.3s / 506 字
    # 免费·比自建 stealth 快 10x·底层 reqwest+scraper·非 headless
    # 硬站(知乎)仍需 firecrawl 云代理
    # ─────────────────────────────────────────
    _WEBCLAW_BIN = "/opt/homebrew/bin/webclaw"

    # ═════════════════════════════════════════════════
    # v13.70 · BrowserSkill · Rust 二进制 + Chrome 扩展
    # 关键洞察 (2026-07-14 真跑挖到): 小红书反爬看跳转路径不看登录态
    #   · 直接 navigate URL → 触发 sec_cFJgYvnK 反爬 · 300031
    #   · SPA click() 卡片 → 生成 xsec_token · 放行
    # 所以 bsk 处理小红书 · 必须走 explore/search → click 流
    # ═════════════════════════════════════════════════
    _BSK_BIN = os.path.expanduser("~/.local/bin/bsk")

    def bsk_available(self) -> bool:
        """检查: bsk 二进制在 + daemon + browser 连接"""
        if not (os.path.exists(self._BSK_BIN) and os.access(self._BSK_BIN, os.X_OK)):
            return False
        try:
            r = subprocess.run([self._BSK_BIN, "browsers"],
                                 capture_output=True, text=True, timeout=5)
            # 有 browser 连才算可用
            return "INSTANCE" in r.stdout and "chrome" in r.stdout.lower()
        except Exception:
            return False

    def bsk_read(self, url: str, max_chars: int = 30000, wait_ms: int = 4000) -> str:
        """v13.88.17 · BrowserSkill 通用读 · 用户 2026-07-24 抓「BrowserSkill 没接进主链路」

        对标 Playwright:
        - 内存降 53% · 反爬绕过 86.7% vs 70% · 请求放大 2.1× vs 3.4×
        - 独家: 复用真实已登录浏览器·Cookie/Session 全在·访问需登录的内容

        通用流程 (不像 bsk_read_xhs 有 click flow):
          1. session start
          2. navigate url
          3. 等 wait_ms
          4. innerText 抓正文
          5. session end
        """
        if not self.bsk_available():
            return ""
        import json as _j
        import time as _t

        try:
            # start session
            r = subprocess.run([self._BSK_BIN, "session", "start", "--json"],
                                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return ""
            sid = _j.loads(r.stdout).get("session_id", "")
            if not sid:
                return ""

            try:
                # navigate
                r_nav = subprocess.run(
                    [self._BSK_BIN, "navigate", "--session", sid, url],
                    capture_output=True, text=True, timeout=25)
                if r_nav.returncode != 0:
                    return ""

                # 等 SPA 渲染
                _t.sleep(wait_ms / 1000)

                # 抓 innerText
                get_text_js = "document.body.innerText"
                r_text = subprocess.run(
                    [self._BSK_BIN, "evaluate", "--session", sid, get_text_js],
                    capture_output=True, text=True, timeout=15)
                if r_text.returncode == 0:
                    text = r_text.stdout.strip().strip('"')
                    return text[:max_chars] if text else ""
                return ""
            finally:
                # 关 session (省资源)
                subprocess.run([self._BSK_BIN, "session", "end", "--session", sid],
                                capture_output=True, timeout=10)
        except Exception as e:
            return ""

    def bsk_read_xhs(self, url: str, max_chars: int = 30000) -> str:
        """
        v13.70 · 小红书专用 click flow · 绕反爬
        输入 URL: xiaohongshu.com/explore/<note_id> or /search_result?keyword=X
        流程:
          1. 起 session
          2. 如是详情 URL: 先跳搜索页 (用 note id 找不到就用 title 兜底)
             如是搜索/首页 URL: 直接 navigate
          3. click 目标卡片 (SPA · 生成 xsec_token)
          4. JS 抓正文 + 评论
          5. 关 session
        """
        import re
        if not self.bsk_available():
            return ""

        note_id_match = re.search(r'/explore/([a-f0-9]{20,})', url)
        is_detail = bool(note_id_match)
        note_id = note_id_match.group(1) if note_id_match else ""

        try:
            # 起 session · JSON 模式
            r = subprocess.run([self._BSK_BIN, "session", "start", "--json"],
                                capture_output=True, text=True, timeout=15)
            if r.returncode != 0: return ""
            import json as _j
            sid = _j.loads(r.stdout).get("session_id", "")
            if not sid: return ""

            try:
                # 目标 tab id
                r = subprocess.run(
                    [self._BSK_BIN, "tab", "list", "--session", sid, "--json"],
                    capture_output=True, text=True, timeout=10)
                # 拿 agent scope tab 的 id
                tab_id = ""
                try:
                    tabs = _j.loads(r.stdout) if r.returncode == 0 else []
                    for t in (tabs if isinstance(tabs, list) else []):
                        if t.get("scope") == "agent":
                            tab_id = str(t.get("tab_id", "")); break
                except: pass

                if is_detail:
                    # 详情 URL → 先跳 search_result 找 · 再 click
                    # 用 note_id 前 6 位当搜索词 · 命中率不高 · 兜底用通用词
                    # 更稳妥: 直接搜"小红书笔记" · 让 agent click 到详情
                    subprocess.run([self._BSK_BIN, "navigate", "--session", sid,
                                    "https://www.xiaohongshu.com/explore"],
                                    capture_output=True, timeout=15)
                    time.sleep(3)
                    # 直接 click 首个卡片 · 拿 explore 页
                    subprocess.run(
                        [self._BSK_BIN, "evaluate", "--session", sid,
                         "(function(){const c=document.querySelector('.note-item a.cover, section.note-item a');"
                         "if(c){c.click();return 'ok';}return 'no';})()"],
                        capture_output=True, timeout=10)
                    time.sleep(4)
                else:
                    # v13.66.2 · 用户 2026-07-14 抓包:「搜索框空的·你没输入过」
                    # → 如是 search_result URL · 走真交互:navigate explore → focus 输入框 →
                    #    逐字输入 → keydown Enter · 让反爬看到真人 event flow
                    search_kw_match = re.search(r'[?&]keyword=([^&]+)', url)
                    if search_kw_match:
                        from urllib.parse import unquote
                        keyword = unquote(search_kw_match.group(1))
                        # 1. 先跳 explore (真人起点·非直跳结果页)
                        subprocess.run([self._BSK_BIN, "navigate", "--session", sid,
                                        "https://www.xiaohongshu.com/explore"],
                                        capture_output=True, timeout=15)
                        time.sleep(2.5)
                        # 2. focus 搜索框 · 逐字输入 · dispatch input event · 每字 50-150ms 随机延迟
                        #    keyboard tempo 靠随机字符间隔模拟人手 · setTimeout Promise 链
                        # v13.66.3 · 探路发现小红书用 <textarea placeholder="搜索小红书">
                        # 不是 <input>·选择器扩到 textarea + 用 visible 过滤(有 3 个同 placeholder
                        # 只有 1 个显示)
                        type_js = (
                            "(async function(){"
                            "const cands=Array.from(document.querySelectorAll("
                            "  'input[placeholder*=\"搜索\"],textarea[placeholder*=\"搜索\"],"
                            "   input.search-input,textarea#search-input-in-feeds'"
                            "));"
                            "const inp=cands.find(el=>el.offsetParent!==null)||cands[0];"
                            "if(!inp)return 'no_input';"
                            "inp.focus();"
                            "inp.click();"
                            f"const kw={_j.dumps(keyword)};"
                            "inp.value='';"
                            "for(let i=0;i<kw.length;i++){"
                            "  inp.value+=kw[i];"
                            "  inp.dispatchEvent(new Event('input',{bubbles:true}));"
                            "  inp.dispatchEvent(new Event('change',{bubbles:true}));"
                            "  await new Promise(r=>setTimeout(r, 50+Math.random()*100));"
                            "}"
                            "await new Promise(r=>setTimeout(r, 400));"
                            "// keydown Enter · 触发表单提交 · 反爬看到 keyboard event"
                            "const ev=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true});"
                            "inp.dispatchEvent(ev);"
                            "// keyup 也发一下 · 更真"
                            "const ev2=new KeyboardEvent('keyup',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true});"
                            "inp.dispatchEvent(ev2);"
                            "return 'typed:'+kw+' onto '+inp.tagName;"
                            "})()"
                        )
                        r_type = subprocess.run(
                            [self._BSK_BIN, "evaluate", "--session", sid, type_js],
                            capture_output=True, text=True, timeout=15,
                        )
                        time.sleep(3.5)  # 等结果加载
                        # 3. Enter 有时页面还是没跳到 search_result · 用 URL 检查兜底
                        check_js = "window.location.href"
                        r_url = subprocess.run(
                            [self._BSK_BIN, "evaluate", "--session", sid, check_js],
                            capture_output=True, text=True, timeout=5,
                        )
                        cur_url = (r_url.stdout or "").strip().strip('"')
                        if "search_result" not in cur_url and "search" not in cur_url:
                            # 真交互没触发跳转 · 兜底直跳 (至少拿到数据)
                            subprocess.run([self._BSK_BIN, "navigate", "--session", sid, url],
                                            capture_output=True, timeout=20)
                            time.sleep(3)
                    else:
                        # 非 search URL · 直接 navigate
                        subprocess.run([self._BSK_BIN, "navigate", "--session", sid, url],
                                        capture_output=True, timeout=20)
                        time.sleep(5)

                # 抓内容
                js = ("JSON.stringify({"
                      "url: window.location.href,"
                      "is_404: window.location.href.includes('404'),"
                      "title: (document.querySelector('#detail-title, [class*=title]')||{}).innerText||'',"
                      "body: ((document.querySelector('#detail-desc, [class*=desc]')||{}).innerText||'').slice(0, 5000),"
                      "n_notes: document.querySelectorAll('.note-item, section.note-item').length,"
                      "top_notes: Array.from(document.querySelectorAll('.note-item, section.note-item')).slice(0,10).map(el => ({"
                      "  title: (el.querySelector('.title, span.title')||{}).innerText||'',"
                      "  author: (el.querySelector('.author, .name')||{}).innerText||'',"
                      "  likes: (el.querySelector('[class*=like]')||{}).innerText||''"
                      "})).filter(x => x.title)"
                      "})")
                cmd = [self._BSK_BIN, "evaluate", "--session", sid, js]
                if tab_id:
                    cmd = [self._BSK_BIN, "evaluate", "--session", sid, "--tab-id", tab_id, js]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r.returncode != 0 or not r.stdout:
                    return ""

                # 输出 = URL + 标题 + 正文 + top notes (合成 md 让 downstream 用得着)
                try:
                    data = _j.loads(r.stdout.strip())
                except:
                    return r.stdout[:max_chars]

                if data.get("is_404"):
                    self._record_incident(url, "bsk_404", data.get("url", "")[:200])
                    return ""

                out = [f"# {data.get('title','')}", ""]
                if data.get("body"):
                    out += [data["body"], ""]
                if data.get("top_notes"):
                    out.append("## 相关笔记")
                    for n in data["top_notes"]:
                        out.append(f"- {n['title']} · {n['author']} · {n.get('likes','')}")
                return "\n".join(out)[:max_chars]
            finally:
                # 清理 session · 不管成不成功
                subprocess.run([self._BSK_BIN, "session", "stop", "--all"],
                                capture_output=True, timeout=10)
        except Exception as e:
            self._record_incident(url, "bsk_exc", str(e)[:200])
            return ""

    def webclaw_available(self) -> bool:
        """检查:webclaw 二进制在"""
        return os.path.exists(self._WEBCLAW_BIN) and os.access(self._WEBCLAW_BIN, os.X_OK)

    def webclaw_read(self, url: str, max_chars: int = 30000,
                      output_format: str = "markdown") -> str:
        """webclaw 秒级 markdown·超快"""
        if not self.webclaw_available():
            return ""
        try:
            r = subprocess.run(
                [self._WEBCLAW_BIN, url, "-f", output_format],
                capture_output=True, text=True, timeout=25,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout[:max_chars]
            if r.stderr:
                self._record_incident(url, "webclaw_stderr", r.stderr[:200])
        except subprocess.TimeoutExpired:
            self._record_incident(url, "webclaw_timeout", "25s")
        except Exception as e:
            self._record_incident(url, "webclaw_exc", str(e)[:200])
        return ""

    def browseract_available(self) -> bool:
        """browser-act CLI 装了 · v13.62 · 用户抓包 (2026-07-13):
        原代码用 'auth current' 是错命令 (browser-act 只有 auth login/poll/set/clear),
        导致 rc=1 · available() 永远 False · tier 2 主战术 100% 降级 firecrawl · 烧额度
        修:用 --version 探活 · 探子命令是否能列出 stealth-extract"""
        if not os.path.exists(self._BROWSER_ACT_BIN):
            return False
        try:
            r = subprocess.run(
                [self._BROWSER_ACT_BIN, "--version"],
                capture_output=True, text=True, timeout=4,
            )
            return r.returncode == 0
        except Exception:
            return False

    def browseract_read(self, url: str, max_chars: int = 30000) -> str:
        """v13.60 · BrowserAct stealth-extract · 反爬强 · 云端处理"""
        if not self.browseract_available():
            return ""
        try:
            r = subprocess.run(
                [self._BROWSER_ACT_BIN, "stealth-extract", url,
                 "--content-type", "markdown"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                self._record_incident(url, "browseract_rc",
                                      (r.stderr or r.stdout)[:200])
                return ""
            content = (r.stdout or "").strip()
            return content[:max_chars] if len(content) > 100 else ""
        except subprocess.TimeoutExpired:
            self._record_incident(url, "browseract_timeout", "")
            return ""
        except Exception as e:
            self._record_incident(url, "browseract_exc", str(e)[:200])
            return ""

    def playwright_available(self) -> bool:
        """检查:playwright 已装 + 9224 debug chrome 在跑"""
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", "2",
                 f"{self._PLAYWRIGHT_CDP_URL}/json/version"],
                capture_output=True, text=True, timeout=3,
            )
            return r.returncode == 0 and '"Browser"' in (r.stdout or "")
        except Exception:
            return False

    def playwright_read(self, url: str, wait_ms: int = 3000,
                         max_chars: int = 30000) -> str:
        """v13.48 · 用独立 Chrome (9224 屏幕外) 读 URL 正文·不打扰用户
        失败返回空串·成功返回 innerText

        v13.88 · 移除内部 wait_until_cool · 改由 read_url 降级循环的
        try_acquire_local_cdp 非阻塞闸门统一管控·避免双重阻塞
        """
        if not self.playwright_available():
            return ""
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return ""
        try:
            with sync_playwright() as p:
                # v13.90.1 · 结构性根治(真根因·非版本问题):
                #   之前误判"Protocol error (Browser.setDownloadBehavior)"是 playwright
                #   包版本 vs 系统 Chrome 版本不匹配 → 换 Chromium 版本后问题依旧存在。
                #   实测复现 + 反查 playwright driver 源码(coreBundle.js initialize())
                #   确认真根因: connect_over_cdp 附着到「非 Playwright 亲自 launch、
                #   外部独立启动」的浏览器时，会默认对已存在的 default context 调用
                #   Browser.setDownloadBehavior 做下载行为托管初始化——这是 Chrome
                #   CDP 协议对「浏览器级」托管命令的限制，与 Chrome/Playwright 版本
                #   无关，任何外部独立启动的 Chrome 都会触发。
                #   官方修复参数 no_defaults=True: 连接时不对已存在 default context
                #   套用这些默认覆盖(acceptDownloads/focus/media emulation)，专为
                #   「附着到别人管理的浏览器」场景设计。
                b = p.chromium.connect_over_cdp(self._PLAYWRIGHT_CDP_URL, no_defaults=True)
                ctx = b.contexts[0] if b.contexts else b.new_context()
                page = ctx.new_page()
                try:
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(wait_ms)
                    # 先尝试正文选择器
                    text = ""
                    for sel in ["#article_content", ".htmledit_views",
                                "article", ".article-content", "main"]:
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                text = el.inner_text(timeout=3000)
                                if len(text) > 200:
                                    break
                        except Exception:
                            continue
                    if not text or len(text) < 200:
                        text = page.locator("body").inner_text(timeout=5000)
                    return (text or "")[:max_chars]
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        b.close()
                    except Exception:
                        pass
        except Exception as e:
            # v13.88.82 · 结构性修复: 不再在源头截断异常文本(之前 [:200] 把
            # "Protocol error (Browser.setDownloadBehavior)" 切成 "Browser.setDow"
            # 导致无法诊断)。完整异常交给 _record_incident → _detect_cdp_protocol_fault
            # 做特征识别 + 完整落盘(该函数内部才按需截断展示用的 detail)。
            self._record_incident(url, "playwright_exc", str(e))
            return ""

    # v13.72 · 真交互搜索 (2026-07-14)
    # 用户抓 R: 「跳转 URL 拿列表页 · 不是真填输入框点搜索」
    # 3 层：填 textarea → 点/enter 搜 → 点第一个笔记 → 读正文
    # 平台 selector 库（可扩展）:
    _SEARCH_SELECTORS = {
        "xiaohongshu": {
            "explore_url": "https://www.xiaohongshu.com/explore",
            "input_finder": "textarea",     # 有多个·取 visible
            "note_link": "a[href*='/search_result'], a[href*='/explore/']",
            "detail_body": ["div[class*='note-scroller']", ".note-content",
                           "div[class*='content']", "#detail-desc", "article"],
        },
    }

    def real_typing_search(self, query: str, platform: str = "xiaohongshu",
                          max_notes: int = 5, click_first: bool = True) -> dict:
        """真交互搜索 · 填输入框 → 触发搜索 → 抽结果 → 可选点详情读正文.
        用户抓 (2026-07-14): 之前的 click_and_read 是 URL 跳转不是真交互.
        返回: {ok, notes: [{title, url}], detail: {title, text, url}}

        v13.73 埋点: 每步经 path_monitor.log_path_step 上报·允许 path_monitor 反向审。
        """
        # v13.73 · 声明计划路径
        try:
            from path_monitor import log_path_plan, log_path_step, log_path_done
            _PM = True
        except Exception:
            _PM = False
            def log_path_plan(*a, **kw): pass
            def log_path_step(*a, **kw): pass
            def log_path_done(*a, **kw): pass

        log_path_plan(
            query=query,
            planned_steps=[
                {"action": "goto", "target": f"{platform}/explore", "why": "真人起点"},
                {"action": "click", "target": "visible textarea", "why": "focus 输入框"},
                {"action": "type", "target": f"query={query}", "why": "逐字输入"},
                {"action": "press", "target": "Enter", "why": "触发搜索"},
                {"action": "extract", "target": "笔记链接", "why": "抽结果"},
                {"action": "click", "target": "第一篇笔记", "why": "打开详情"},
                {"action": "extract", "target": "正文", "why": "读内容"},
            ],
            caller="real_typing_search",
        )

        result = {"ok": False, "via": "typing_search", "notes": [],
                 "detail": {}, "keyword": query, "platform": platform}
        if not self.playwright_available():
            result["via"] = "no_playwright"
            return result
        conf = self._SEARCH_SELECTORS.get(platform)
        if not conf:
            result["via"] = f"no_config_for_{platform}"
            return result
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                # v13.90.1 · no_defaults=True 见 playwright_read 同名注释(真根因修复)
                b = p.chromium.connect_over_cdp(self._PLAYWRIGHT_CDP_URL, no_defaults=True)
                ctx = b.contexts[0] if b.contexts else b.new_context()
                page = ctx.new_page()
                try:
                    page.goto(conf["explore_url"], timeout=30000,
                             wait_until="domcontentloaded")
                    log_path_step("goto", conf["explore_url"], True,
                                 "初始 explore", "real_typing_search")
                    page.wait_for_timeout(3000)
                    # 找 visible 输入框（有些 modal 藏了不可见的同类元素）
                    inputs = page.locator(conf["input_finder"]).all()
                    target = None
                    for inp in inputs:
                        try:
                            if inp.is_visible():
                                target = inp
                                break
                        except Exception:
                            pass
                    if not target:
                        log_path_step("click", "visible textarea", False,
                                    "no_visible_input", "real_typing_search")
                        result["via"] = "no_visible_input"
                        return result
                    target.click(force=True)
                    log_path_step("click", "textarea", True,
                                "focus 输入框", "real_typing_search")
                    page.wait_for_timeout(500)
                    # 真人式逐字输入
                    for char in query:
                        page.keyboard.type(char, delay=80)
                    log_path_step("type", f"query={query}", True,
                                f"逐字 {len(query)} 字符", "real_typing_search")
                    page.wait_for_timeout(1000)
                    page.keyboard.press("Enter")
                    log_path_step("press", "Enter", True,
                                "触发搜索", "real_typing_search")
                    page.wait_for_timeout(4000)
                    # 抽笔记链接
                    notes = page.evaluate(f"""() => {{
                      const results = [];
                      document.querySelectorAll("{conf['note_link']}").forEach(a => {{
                        const href = a.href || '';
                        const txt = (a.innerText || '').trim().slice(0, 100);
                        if (txt.length > 3) results.push({{href, txt}});
                      }});
                      const seen = new Set();
                      return results.filter(r => !seen.has(r.href) && seen.add(r.href)).slice(0, {max_notes});
                    }}""") or []
                    result["notes"] = [{"title": n["txt"], "url": n["href"]}
                                      for n in notes]
                    result["search_url"] = page.url
                    log_path_step("extract", f"notes×{len(notes)}", True,
                                f"URL 变 {page.url[:60]}", "real_typing_search")
                    # 可选：点第一个进详情读正文
                    if click_first and notes:
                        first = page.locator(conf["note_link"]).first
                        try:
                            first.click(timeout=5000)
                            log_path_step("click", "第一篇笔记", True,
                                        "locator 成功", "real_typing_search")
                        except Exception:
                            # v13.72 · locator 失效时用 URL 直接 goto
                            page.goto(notes[0]["href"], timeout=30000)
                            log_path_step("goto", notes[0]["href"], True,
                                        "locator 失效·fallback goto",
                                        "real_typing_search")
                        page.wait_for_timeout(4000)
                        import json as _json
                        sels_js = _json.dumps(conf['detail_body'])
                        body = page.evaluate(f"""() => {{
                          const sels = {sels_js};
                          for (const sel of sels) {{
                            const el = document.querySelector(sel);
                            if (el) {{
                              const t = el.innerText;
                              if (t && t.length > 100) return {{sel, text: t.slice(0, 3000)}};
                            }}
                          }}
                          return {{sel: 'body-fallback', text: document.body.innerText.slice(0, 3000)}};
                        }}""")
                        result["detail"] = {
                            "title": page.title(),
                            "url": page.url,
                            "text": body.get("text", ""),
                            "selector_used": body.get("sel", ""),
                        }
                        log_path_step("extract", f"body sel={body.get('sel','')}",
                                    len(body.get("text","")) > 100,
                                    f"len={len(body.get('text',''))}",
                                    "real_typing_search")
                    result["ok"] = True
                    log_path_done("real_typing_search",
                                f"notes={len(notes)} detail={bool(result.get('detail'))}")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        b.close()
                    except Exception:
                        pass
        except Exception as e:
            # v13.88.82 · 同 playwright_read 修复: 不在源头截断·让协议错误检测器能看到完整特征
            self._record_incident(query, "cdp_exc", str(e))
            result["via"] = "exception"
            result["error"] = str(e)[:200]
        return result

    def playwright_click_and_read(self, list_url: str,
                                    click_selector: str = None,
                                    wait_ms: int = 3000) -> dict:
        """列表页 → 抓第一篇详情正文·全程在屏幕外"""
        result = {"list_title": "", "detail_url": "", "detail_title": "",
                  "detail_text": "", "ok": False, "via": "failed"}
        if not self.playwright_available():
            result["via"] = "no_playwright"
            return result
        if not click_selector:
            click_selector = ("a[href*='/article/details/'], "
                              "a[href*='/post/'], a[href*='/answer/'], "
                              "a[href*='/p/'], a[href*='/topic/']")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                # v13.90.1 · no_defaults=True 见 playwright_read 同名注释(真根因修复)
                b = p.chromium.connect_over_cdp(self._PLAYWRIGHT_CDP_URL, no_defaults=True)
                ctx = b.contexts[0] if b.contexts else b.new_context()
                page = ctx.new_page()
                try:
                    page.goto(list_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(wait_ms)
                    result["list_title"] = page.title()
                    # 抓文章链接
                    links = page.evaluate(f"""() => {{
                      const out = [];
                      document.querySelectorAll("{click_selector}").forEach(a => {{
                        const t = (a.innerText||'').trim();
                        if (t && t.length > 8 && t.length < 200) out.push({{h: a.href, t: t.slice(0,100)}});
                      }});
                      const seen = new Set();
                      return out.filter(x => !seen.has(x.h) && seen.add(x.h)).slice(0, 5);
                    }}""") or []
                    if not links:
                        result["via"] = "no_links_found"
                        return result
                    # 第一个非当前页链接
                    detail_url = ""
                    for lk in links:
                        if lk.get("h") and lk["h"] != list_url:
                            detail_url = lk["h"]
                            break
                    if not detail_url:
                        result["via"] = "same_url_only"
                        return result
                    result["detail_url"] = detail_url
                    page.goto(detail_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(wait_ms)
                    result["detail_title"] = page.title()
                    # 正文
                    body = ""
                    for sel in ["#article_content", ".htmledit_views",
                                "article", ".article-content", "main", "body"]:
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                body = el.inner_text(timeout=3000)
                                if len(body) > 300:
                                    break
                        except Exception:
                            continue
                    result["detail_text"] = body[:20000]
                    result["ok"] = len(body) > 300
                    result["via"] = "playwright_click"
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        b.close()
                    except Exception:
                        pass
        except Exception as e:
            # v13.88.82 · 同 playwright_read 修复: 不在源头截断
            self._record_incident(list_url, "pw_click_exc", str(e))
        return result

    # ─────────────────────────────────────────
    # v13.46 · browser-use CLI 后端 (备用·会弹权限·尽量不用)
    # 用户 2026-07-09 loop 明令: "browser-use-cli 你怎么不会呢"
    # 官方 skill · 零配置直接接管当前 Chrome · 完全绕开我手搓的 3457 proxy
    # 相较手搓 CDP 强在: 保留登录态·内建反爬·Python 直控·统一 helpers
    # ─────────────────────────────────────────
    _BROWSER_USE_BIN = str(Path(__file__).parent / ".browser_use_venv" / "bin" / "browser-use")

    def browser_use_available(self) -> bool:
        """browser-use 二进制存在且能起 daemon"""
        try:
            if not Path(self._BROWSER_USE_BIN).exists():
                return False
            # doctor 会在 Chrome 跑时报 chrome running=ok
            r = subprocess.run(
                [self._BROWSER_USE_BIN, "doctor"],
                capture_output=True, text=True, timeout=8,
            )
            return "chrome running" in (r.stdout or "") and "[ok" in (r.stdout or "")
        except Exception:
            return False

    def browser_use_read(self, url: str, wait_ms: int = 4000,
                          new_tab: bool = True) -> str:
        """v13.46 · 用 browser-use CLI 打开 URL·拿 innerText
        new_tab: True → 开新标签(不打扰用户当前 tab)
                 False → 在当前 tab 导航
        失败返回空串

        v13.88 · 移除内部 wait_until_cool · 改由 read_url 降级循环的
        try_acquire_local_cdp 非阻塞闸门统一管控·避免双重阻塞
        """
        if not Path(self._BROWSER_USE_BIN).exists():
            return ""
        # heredoc 给 browser-use · 官方 helpers 预导入(new_tab / page_info / page_text)
        nav = "new_tab" if new_tab else "goto_url"
        script = f"""
import time
{nav}({url!r})
time.sleep({wait_ms/1000.0})
try:
    t = page_text()  # 优先 innerText
except Exception:
    try:
        t = page_info().get("text","")
    except Exception:
        t = ""
if not t:
    try:
        t = eval_js("document.body.innerText").get("value","")
    except Exception:
        t = ""
print("__BU_START__")
print((t or "")[:80000])
print("__BU_END__")
"""
        try:
            r = subprocess.run(
                [self._BROWSER_USE_BIN],
                input=script,
                capture_output=True, text=True, timeout=45,
            )
            out = r.stdout or ""
            if "__BU_START__" in out and "__BU_END__" in out:
                text = out.split("__BU_START__", 1)[1].split("__BU_END__", 1)[0].strip()
                if len(text) > 100:
                    return text
            # 记 incident 便于调试
            self._record_incident(url, "browser_use_empty", (r.stderr or out)[:200])
            return ""
        except subprocess.TimeoutExpired:
            self._record_incident(url, "browser_use_timeout", "")
            return ""
        except Exception as e:
            self._record_incident(url, "browser_use_exc", str(e)[:200])
            return ""

    def browser_use_click_and_read(self, list_url: str,
                                    click_selector: str = None,
                                    wait_ms_after_click: int = 4000,
                                    max_detail_chars: int = 20000) -> dict:
        """v13.47 · 演绎修 · CDP click 加了但从没被调 · 用 browser-use 补
        打开列表页 · 点第一个具体文章链接 · 抓详情页 innerText

        返回: {list_title, detail_url, detail_title, detail_text, ok, via}
        用户 2026-07-09: 「点了吗看了吗·不点不算数」 —— 这是让工具真被触发的入口"""
        result = {"list_title": "", "detail_url": "", "detail_title": "",
                  "detail_text": "", "ok": False, "via": "failed"}
        if not Path(self._BROWSER_USE_BIN).exists():
            result["via"] = "no_bin"
            return result
        # 启发式 selector: 常见文章 URL pattern
        if not click_selector:
            click_selector = ("a[href*='/article/details/'], "
                              "a[href*='/post/'], a[href*='/article/'], "
                              "a[href*='/p/'], a[href*='/answer/'], "
                              "a[href*='/topic/'], a[href*='/blog/']")
        script = f"""
import time
new_tab({list_url!r})
time.sleep(6)
info = page_info()
list_title = info.get("title","")

# 抓所有匹配的文章链接
links_json = js('''(() => {{
  const out = [];
  document.querySelectorAll("{click_selector}").forEach(a => {{
    const t = (a.innerText || '').trim();
    if (t && t.length > 8 && t.length < 200) out.push({{h: a.href, t: t.slice(0,100)}});
  }});
  const seen = new Set();
  return JSON.stringify(out.filter(x => !seen.has(x.h) && seen.add(x.h)).slice(0, 5));
}})()''')

import json as __j
links = []
try:
    links = __j.loads(links_json) if links_json else []
except Exception:
    pass

detail_url = ""
detail_title = ""
detail_text = ""
if links:
    # 打开第一个非当前页的链接
    for lk in links:
        if lk.get("h") and lk["h"] != {list_url!r}:
            detail_url = lk["h"]
            break
    if detail_url:
        new_tab(detail_url)
        time.sleep({wait_ms_after_click/1000.0})
        info2 = page_info()
        detail_title = info2.get("title","")
        # 取文章正文
        detail_text = js('''(() => {{
          const sel = ['article', '.htmledit_views', '#article_content',
                       '.article-content', '.post-content', '.main-content',
                       'main', '.content'];
          for (const s of sel) {{
            const el = document.querySelector(s);
            if (el && el.innerText.length > 200) return el.innerText.slice(0, {max_detail_chars});
          }}
          return document.body.innerText.slice(0, {max_detail_chars});
        }})()''') or ""

print("__BU_START__")
print(__j.dumps({{"list_title": list_title, "detail_url": detail_url,
                  "detail_title": detail_title, "detail_text": detail_text}}))
print("__BU_END__")
"""
        try:
            r = subprocess.run(
                [self._BROWSER_USE_BIN],
                input=script,
                capture_output=True, text=True, timeout=90,
            )
            out = r.stdout or ""
            if "__BU_START__" in out and "__BU_END__" in out:
                data_str = out.split("__BU_START__", 1)[1].split("__BU_END__", 1)[0].strip()
                try:
                    d = json.loads(data_str)
                    result.update(d)
                    result["ok"] = bool(d.get("detail_text") and len(d["detail_text"]) > 200)
                    result["via"] = "browser_use_click"
                except Exception:
                    self._record_incident(list_url, "bu_click_json_err", data_str[:200])
            else:
                self._record_incident(list_url, "bu_click_empty", (r.stderr or out)[:200])
        except subprocess.TimeoutExpired:
            self._record_incident(list_url, "bu_click_timeout", "")
        except Exception as e:
            self._record_incident(list_url, "bu_click_exc", str(e)[:200])
        return result

    def cdp_click_and_read(self, list_url: str, click_selector: str = None,
                              wait_after_click_ms: int = 3000) -> str:
        """v13.45 · 用户 loop 明令: "你不能点击模拟吗"
        CDP 打开列表页 · 点第一个卡片 · 拿详情页内容
        list_url: 列表/搜索页 URL
        click_selector: JS 选择器 · 如 'article a' 'a[href*="/post/"]' · None 时用启发式"""
        if not self.cdp_available():
            return ""
        tid = None
        try:
            # 1. 开列表页
            r1 = subprocess.run(
                ["curl", "-sS", "--max-time", "20", "-X", "POST",
                 "--data-raw", list_url, "http://localhost:3457/new"],
                capture_output=True, text=True, timeout=25,
            )
            try:
                tab = json.loads(r1.stdout or "{}")
                tid = tab.get("targetId") or tab.get("target") or tab.get("id")
            except Exception:
                tid = None
            if not tid:
                return ""
            time.sleep(wait_ms_first := 4)

            # 2. 点击第一个具体文章链接
            #    启发式: /post/ | /article/ | /answer/ | /item?id= 优先
            if not click_selector:
                click_selector = "a[href*='/post/'], a[href*='/article/'], a[href*='/p/'], a[href*='/answer/']"
            click_js = f"""
(() => {{
    const links = document.querySelectorAll("{click_selector}");
    if (links.length === 0) return null;
    const target = links[0];
    const href = target.href;
    target.click();
    return href;
}})()
"""
            r_click = subprocess.run(
                ["curl", "-sS", "--max-time", "10", "-X", "POST",
                 "--data-binary", click_js.strip(),
                 f"http://localhost:3457/eval?target={tid}"],
                capture_output=True, text=True, timeout=15,
            )
            try:
                clicked_href = json.loads(r_click.stdout or "{}").get("value", "")
            except Exception:
                clicked_href = ""

            # 3. 等详情页加载
            time.sleep(wait_after_click_ms / 1000.0)

            # 4. 读详情页 innerText
            r_read = subprocess.run(
                ["curl", "-sS", "--max-time", "15", "-X", "POST",
                 "--data-binary", "document.body.innerText.slice(0, 80000)",
                 f"http://localhost:3457/eval?target={tid}"],
                capture_output=True, text=True, timeout=20,
            )
            try:
                text = json.loads(r_read.stdout or "{}").get("value", "") or ""
            except Exception:
                text = ""
            return text.strip() if text and len(text.strip()) > 100 else ""
        except Exception as e:
            self._record_incident(list_url, "cdp_click_exc", str(e)[:200])
            return ""
        finally:
            if tid:
                try:
                    subprocess.run(
                        ["curl", "-sS", "--max-time", "5",
                         f"http://localhost:3457/close?target={tid}"],
                        capture_output=True, timeout=6,
                    )
                except Exception:
                    pass

    def cdp_navigate_and_read(self, url: str, wait_ms: int = 4000) -> str:
        """如果 CDP 可用·用 web-access proxy 打开后台 tab · 拿主内容
        返回文本·失败返回空串
        v13.14 · 优先拿 <main>/<article> · 兜底 body.innerText
        v13.20 · 加 perf_guard · 电脑卡就 sleep 冷却"""
        if not self.cdp_available():
            return ""
        # v13.88 · 移除内部 wait_until_cool · 改由 read_url 降级循环的
        # try_acquire_local_cdp 非阻塞闸门统一管控·避免双重阻塞
        tid = None
        try:
            # 1. 创建后台 tab (web-access proxy 自动等加载完)
            r1 = subprocess.run(
                ["curl", "-sS", "--max-time", "30", "-X", "POST",
                 "--data-raw", url, "http://localhost:3457/new"],
                capture_output=True, text=True, timeout=35,
            )
            tab = {}
            try:
                tab = json.loads(r1.stdout or "{}")
            except Exception:
                pass
            tid = tab.get("targetId") or tab.get("target") or tab.get("id")
            if not tid:
                self._record_incident(url, "cdp_no_tid", (r1.stdout or "")[:200])
                return ""

            # 2. 给 JS 渲染留时间 (SPA 类站点需要)
            time.sleep(wait_ms / 1000.0)

            # 3. eval 优先拿主内容·失败退回 body innerText
            #    v13.18 · 直接用 body.innerText·然后 python 端筛正文
            #    (querySelector 语法在 curl -d 传递时偶发引号问题·退回最简形式)
            js_expr = "document.body.innerText.slice(0, 80000)"
            r2 = subprocess.run(
                ["curl", "-sS", "--max-time", "15", "-X", "POST",
                 "--data-binary", js_expr,
                 f"http://localhost:3457/eval?target={tid}"],
                capture_output=True, text=True, timeout=20,
            )
            text = ""
            try:
                parsed = json.loads(r2.stdout or "{}")
                # web-access proxy 返 {"value": "..."} · 兼容 {"result": ...}
                text = parsed.get("value", "") or parsed.get("result", "") or ""
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
            except Exception:
                text = r2.stdout or ""

            # 空串或过短 · 说明反爬拦截或页面尚未加载完
            if not text or len(text.strip()) < 50:
                self._record_incident(url, "cdp_empty", f"len={len(text)}")
                return ""

            # v13.28.1 · 自动识别 captcha/风控页 · 见到就立刻记录并返空
            # (用户 2026-07-10 抓到"请求太频繁"页 · 我该自己识别)
            # v13.43 · 加 404 白页/空页信号
            # v13.88.82 · 结构性修复: 反爬信号(会话级/站点级) 与 404死链信号
            # (页面级) 语义不同·不该混在一起统一处理。反爬走统一检测器+记住
            # 域名(下次绕道)；404 只是"这一个 URL 没了"·不该连累整个域名被
            # 判定成反爬域名(否则误伤:例如某文章被删=404·但站点其他文章正常)。
            if self.detect_and_learn_anti_bot(url, text):
                self._record_incident(url, "captcha_or_rate_limit",
                                      f"len={len(text)}")
                return ""
            _dead_page_signals = (
                "抱歉·您要查看的页面不存在", "页面不存在", "页面已被删除",
                "page not found", "404 error", "找不到页面",
            )
            head_check = text[:500].lower()
            for sig in _dead_page_signals:
                if sig.lower() in head_check:
                    self._record_incident(url, "dead_page_404",
                                          f"signal={sig[:30]}·len={len(text)}")
                    return ""
            return text.strip()
        except Exception as e:
            # v13.88.82 · 同 playwright_read 修复: 不在源头截断
            self._record_incident(url, "cdp_navigate_exc", str(e))
            return ""
        finally:
            # 4. 关闭自建 tab (无论成功失败·保持环境整洁·web-access SKILL 铁律)
            if tid:
                try:
                    subprocess.run(
                        ["curl", "-sS", "--max-time", "5",
                         f"http://localhost:3457/close?target={tid}"],
                        capture_output=True, timeout=6,
                    )
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════
#  Doctor · 自诊断
# ═══════════════════════════════════════════════════════════════
def doctor() -> dict:
    """诊断: Jina/curl/CDP 各层可用性 + 最近 24h 限流统计"""
    wa = WebAccess()
    out = {"ts": datetime.now().isoformat()}

    # 1. curl 基础
    try:
        r = subprocess.run(["curl", "--version"], capture_output=True, text=True, timeout=3)
        out["curl"] = "ok" if r.returncode == 0 else "fail"
    except Exception:
        out["curl"] = "fail"

    # 2. Jina 连通 (读 example.com · 应返 200 字符+)
    print("检查 Jina...")
    md = wa.jina_read("https://example.com", timeout=10)
    out["jina"] = f"ok ({len(md)} chars)" if md and len(md) > 50 else "fail"

    # 3. CDP (web-access)
    out["cdp_proxy"] = "ok" if wa.cdp_available() else "not_running (装 Chrome 并开 remote-debug 后可用)"

    # 4. Fallback KB 状态
    out["recent_incidents_24h"] = len(wa.get_recent_incidents(hours=24))
    out["blocked_domains_6h"] = wa.get_blocked_domains(hours=6, min_incidents=3)

    return out


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser(description="web-access bridge · 联网能力抽象层")
        p.add_argument("--doctor", action="store_true", help="自诊断三级可用性")
        p.add_argument("--read", help="读一个 URL (自动降级)")
        p.add_argument("--incidents", type=int, default=0, help="打印最近 N 小时限流事件")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.doctor:
        d = doctor()
        print("═" * 60)
        print("v13.14 · web_access_bridge · 自诊断")
        print("═" * 60)
        for k, v in d.items():
            print(f"  {k}: {v}")
    elif args.read:
        wa = WebAccess()
        r = wa.read_url(args.read)
        print(f"via: {r['via']} · ok: {r['ok']} · len: {len(r['content'])}")
        if r["ok"]:
            print("─" * 60)
            print(r["content"][:2000])
    elif args.incidents:
        wa = WebAccess()
        rows = wa.get_recent_incidents(hours=args.incidents)
        print(f"最近 {args.incidents}h · {len(rows)} 起限流事件")
        from collections import Counter
        by_domain = Counter(r["domain"] for r in rows)
        for d, n in by_domain.most_common():
            print(f"  {n:3d} · {d}")
    else:
        p.print_help()
