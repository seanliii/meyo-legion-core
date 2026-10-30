# -*- coding: utf-8 -*-
"""security_guards · shell/curl/url 安全守卫层(2026-08-27 从 qa_auto_agent god-file 抽出)。

职责: 纯函数式的执行前安全/反爬/格式守卫——危险 shell 检测、curl 各类守卫(搜索引擎/
视频站/HTML-vs-API/学术反爬)、下载校验、URL 分类、超时注入。全部是"输入命令/URL 字符串→
返回提醒/改写"的纯判断·零文件系统副作用·零 auto 反向依赖(_safe_qid 来自 run_state)。

这是 execution 工具的安全底座·独立后为后续抽 execution_tools 铺路。
auto 通过 from security_guards import * 拿回全部符号(调用点零改动)。
"""
import os, re
from run_state import _safe_qid

_DANGEROUS_SHELL_PATTERNS = [
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",   # fork bomb
    r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+/(\s|$)",  # rm -rf /
    r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+~(\s|/|$)",  # rm -rf ~
    r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+\*\s*$",   # rm -rf * (工作目录全清，容错兜底)
    r">\s*/dev/sd[a-z]",                              # 直接写块设备
    r"mkfs\.",                                        # 格式化
]

_SENSITIVE_PATH_PATTERNS = [
    r"\.meyo/agent_web_tokens\.json",   # 全体用户的认证 token 明文表
    r"\.meyo/hitl/",                     # 审批记录(含其他用户任务的操作详情)
    r"\.ssh/",                            # SSH 私钥/known_hosts
    r"\.aws/", r"\.gcloud/", r"\.config/gcloud",  # 云凭证
    r"/etc/passwd", r"/etc/shadow", r"/etc/sudoers",
    r"\.netrc", r"\.git-credentials",
]

_SENSITIVE_PATH_PATTERNS_OUTSIDE_SANDBOX_ONLY = [
    r"\.env$",                            # 只挡"以 .env 结尾"，不误伤
                                           # 如 .env.example 这类模板文件
]


def _is_dangerous_shell(command: str) -> str:
    """命中已知的毁灭性命令模式则返回原因说明，否则返回 ""。"""
    import re as _re
    for pat in _DANGEROUS_SHELL_PATTERNS:
        if _re.search(pat, command):
            return f"命中已知毁灭性命令模式(pattern={pat})，已拦截不执行"
    return ""


# 跨用户沙箱目录匹配:命令文本里出现"别人的沙箱目录名"就该拦——不要求
# 拿到具体路径再 abspath(shell_exec 场景做不到，命令是自由文本，无法像
# file_read 那样先解析出单一路径)，直接用目录名模式匹配足够识别这个
# 意图明确的越权动作(正常任务不会无缘无故在命令行里写别人的 qid 目录名)。
_SANDBOX_DIR_NAME_RE = None  # 延迟编译，见 _shell_command_path_violation


def _shell_command_path_violation(qid, command: str) -> str:
    """扫描 shell 命令文本，命中敏感路径片段或"别的 qid 沙箱目录名"则拒绝。
    不做完整 shell 语法解析(变量展开/引号转义等)，只做字面字符串匹配——
    这是纵深防御的一层，不是唯一防线，档不住处心积虑的编码绕过，但足以
    挡住"忘记了/没意识到"级别的误用，成本收益合理。"""
    import re as _re
    global _SANDBOX_DIR_NAME_RE
    c = command
    # A1. 敏感路径红线(不分沙箱内外，命令文本里直接出现这些路径片段就拒绝，
    # 不需要先 resolve，字面出现就说明命令打算碰它，无论是否会真的执行成功)。
    for pat in _SENSITIVE_PATH_PATTERNS:
        if _re.search(pat, c):
            return (f"[已拦截] 命令中出现敏感路径(pattern={pat})，涉及认证凭证/"
                     f"密钥/系统账户信息，任何任务都不允许访问。如果这不是你的本意，"
                     f"请检查命令里的路径拼写；如果确实需要，请说明理由改走其他方式。")
    # A2. .env 类:shell 命令是自由文本，没有单一确定路径可判断"是否在自己
    # 沙箱内"(不像 file_read/write 那样)——这里用一个务实的简化判断:命令
    # 文本里如果出现了自己的沙箱目录名，视为在操作沙箱内的文件，不拦截
    # (跟 file_write 沙箱内 .env 放行的口径保持一致)；否则按"疑似沙箱外"
    # 处理，拦截 .env 类文件访问。
    own_dir_name = f"qa_auto_{_safe_qid(qid)}"
    if own_dir_name not in c:
        for pat in _SENSITIVE_PATH_PATTERNS_OUTSIDE_SANDBOX_ONLY:
            if _re.search(pat, c):
                return (f"[已拦截] 命令中出现疑似项目密钥文件(pattern={pat})，"
                         f"且不在你自己的沙箱目录下操作，不允许访问。如果这是你自己"
                         f"任务需要的配置文件，请把它放在你的沙箱目录内再操作。")
    # B. 跨用户沙箱隔离:命令文本里出现"qa_auto_<不是自己的qid>"这种目录名
    if _SANDBOX_DIR_NAME_RE is None:
        _SANDBOX_DIR_NAME_RE = _re.compile(r"qa_auto_[A-Za-z0-9_]+")
    for m in _SANDBOX_DIR_NAME_RE.finditer(c):
        if m.group(0) != own_dir_name:
            return (f"[已拦截] 命令中出现其他任务的沙箱目录名({m.group(0)})，"
                     f"跨用户/跨任务访问不允许。你自己的沙箱目录名是 {own_dir_name}。")
    return ""


# F1(2026-08-07) · 治工具超时:日志坐实 35/44 次 shell 超时是 curl/wget 抓搜索引擎/
# 反爬站(duckduckgo/google search/web.archive.org/hathitrust…)——这些站慢/被墙/反爬,
# curl 干等 30s 抓不下来还反复重试拖垮墙钟。根因=agent 手写 curl 抓网页没用现成
# run_parallel_search(内部走 SearchAgent·有反爬/镜像/超时保护)。工具层防呆:
# 检测"curl 抓搜索引擎/反爬站"→不放它干等,引导换 run_parallel_search。
# 只拦这一种错误用法·结构化 API/直链下载的 curl 照常放行。
_CURL_SLOW_HOSTS = (
    "duckduckgo.com", "google.com/search", "www.google.com/search",
    "bing.com/search", "search.yahoo", "baidu.com/s?", "web.archive.org/cdx",
    "hathitrust.org/search", "catalog.hathitrust", "library.oapen.org/handle",
    "scholar.google", "semanticscholar.org/search",
)


# v120 FixL(2026-08-17·nat9 实证): win_chain 赢链重放的命令是【历史已验证成功】
# (hit=1 时归档)的确定性调用·不是模型临场手撸。F1/F3/F4 防呆拦它们=把赢链掐死
# (nat9 00d579ea: 15步重放 curl 全被拦→链废→DEGRADED UNKNOWN)。重放期间豁免。
# 安全边界: 只豁免工具级引导性拦截;路径红线(密钥/跨沙箱)仍拦——赢链是历史
# 真跑过的命令·不该触红线·若触了说明环境变了·拦下回退正常循环是对的。
_REPLAY_EXEMPT = False


def _curl_search_engine_guard(command: str) -> str:
    """命中"curl/wget 抓搜索引擎/反爬慢站"→返回引导语;否则 ""(放行)。"""
    if _REPLAY_EXEMPT:
        return ""
    c = command.lower()
    if not ("curl" in c or "wget" in c):
        return ""
    for host in _CURL_SLOW_HOSTS:
        if host in c:
            return (
                "[已拦截·会超时] 你在用 curl/wget 抓搜索引擎/反爬站,这些站慢或反爬,"
                "curl 大概率 30s 超时白等。**请改用 run_parallel_search(question)**——"
                "它内部走 SearchAgent,有反爬/国内镜像/超时保护,专门干这个。"
                "curl 只留给结构化 API(ExPASy/PubChem/GitHub API)和直链文件下载。"
            )
    return ""


# F3(2026-08-09·用户「浏览器能读YouTube你不会用·不像人用全套工具」) · 治
# 多模态题手撸 shell:实证 8b3379c0(YouTube 短片题)手撸 40+ 步 curl 抓
# youtube.com 网页/API 全反爬失败·从没调现成的 transcribe_audio。工具层硬拦
# (prompt 软引导拉不动已成型的手撸行为·和 F1 同理):检测"curl/wget 抓视频站
# 网页"→拦下·引导用 transcribe_audio(要音轨内容)或 run_parallel_search(找视频/
# 读描述)。yt-dlp 抽音轨是合法用法(放行)·只拦 curl/wget 抓视频站网页这种没用的。
_VIDEO_SITES = (
    "youtube.com", "youtu.be", "vimeo.com", "bilibili.com", "dailymotion.com",
    "twitch.tv", "tiktok.com",
)


def _curl_video_site_guard(command: str) -> str:
    """命中"curl/wget 抓视频站网页/API"(非 yt-dlp)→返回引导语;否则 ""(放行)。"""
    if _REPLAY_EXEMPT:   # v120 FixL: 赢链重放豁免
        return ""
    c = command.lower()
    # yt-dlp 抽音轨/元数据是合法的·放行(transcribe_audio 内部也走 yt-dlp)
    if "yt-dlp" in c or "youtube-dl" in c:
        return ""
    if not ("curl" in c or "wget" in c):
        return ""
    for host in _VIDEO_SITES:
        if host in c:
            return (
                "[已拦截·会反爬失败] 你在用 curl/wget 抓视频站网页,这些站反爬,"
                "curl 大概率抓不到有效内容(实证有题这样手撸 40+ 步全失败)。按题目性质选:\n"
                "  · 要【视频里说了啥/音轨内容/第N个短片的信息】→ 用 "
                "**transcribe_audio(youtube链接)**,它内部 yt-dlp 抽音轨+whisper 转录。\n"
                "  · 要【频道页/About页上的外链、频道描述、元数据】→ 用 shell_exec 跑 "
                "`yt-dlp -J '<频道或视频URL>' --skip-download`(v108:秒级拿JSON元数据·含"
                "描述/外链/标题·yt-dlp 已放行)——别猜域名。\n"
                "  · 要【找哪个视频/视频的描述/发布信息/物种资料等文字数据】→ 用 "
                "**run_parallel_search(question)**(有反爬/镜像保护),或 curl 官方 API"
                "(如 googleapis.com/youtube/v3·带 key 时)。\n"
                "别再手撸 curl 抓视频站网页。"
            )
    return ""


# F4(2026-08-11·用户「你的搜索优化对这题没用上」抓的真bug) · 治
# "curl 直连学术/强反爬全文站撞 reCAPTCHA":实证米氏方程题(c526d8d6)、
# PubChem 化合物题(384d0dd8)两道复盘发现,虽然 v96i 已经把 NCBI/PMC/
# EuropePMC 等域名加进了 search_agent_orchestrator.DOMAIN_TIER(tier≥1
# 强制走 playwright/browseract 渲染过验证)，但那套分级只有 agent 主动调用
# fetch_url_smart/smart_fetch 时才会生效——而 agent 完全可以（也确实发生了）
# 无视工具描述里的提示，直接 shell_exec 手撸 curl 硬连这些域名，等于绕过
# 了整套反爬分级优化，优化形同虚设。F1/F3 已经证明"工具层硬拦截"比"prompt
# 软引导"管用得多，这里照同样模式再补一道:curl/wget 命令里出现
# DOMAIN_TIER 里 tier≥1(需要过反爬/JS 挑战)的域名 → 直接拦截，引导换
# fetch_url_smart(内部按域名分级自动选战术，不需要 agent 自己判断)。
# 动态从 search_agent_orchestrator.DOMAIN_TIER 取域名列表(单一数据源，
# 不再维护第二份重复清单，DOMAIN_TIER 加新域名这里自动生效)。
_ACADEMIC_TIER_HOSTS = None


def _get_academic_tier_hosts():
    """懒加载 + 缓存 DOMAIN_TIER 里 tier>=1 的域名(排除已被 F1/F3 覆盖的
    搜索引擎/视频站，避免重复拦截逻辑打架)。导入失败(比如路径没配好)
    就返回空 tuple，guard 直接放行，不影响主流程。"""
    global _ACADEMIC_TIER_HOSTS
    if _ACADEMIC_TIER_HOSTS is not None:
        return _ACADEMIC_TIER_HOSTS
    try:
        from search_agent_orchestrator import DOMAIN_TIER
        _covered = set(_CURL_SLOW_HOSTS) | set(_VIDEO_SITES)
        _ACADEMIC_TIER_HOSTS = tuple(
            h for h, tier in DOMAIN_TIER.items() if tier >= 1 and h not in _covered
        )
    except Exception:
        _ACADEMIC_TIER_HOSTS = tuple()
    return _ACADEMIC_TIER_HOSTS


# v-eff2(2026-08-19·病1 复核·shell_exec 精细化门) · 通用"HTML 正文页 vs
#   结构化 API 端点"识别——不是砍 shell_exec(它承担计算脚本/权威 API 直连两
#   项正当职责,砍了会连带废掉这两项能力,是错误方向)，而是补一道【不依赖
#   域名黑名单】的通用规则:命中"整页 HTML 正文"特征(无 API/JSON 端点标志)
#   时提示换 fetch_url_smart(它有 8-14K 分级预算+反爬升级,比裸 curl 拿
#   渲染前 HTML 更完整);命中结构化端点特征(api.php/action=query/
#   format=json/REST 路径等)照常放行——这正是权威库精确查询的合理用法。
#   只是【建议性提示】不硬拦截(和 F1/F3/F4 直接拒绝执行不同)：shell_exec
#   仍可继续跑，只是把更优选项递到模型面前，不误伤计算脚本/API 直连场景。
_API_ENDPOINT_HINTS = (
    "api.php", "action=query", "format=json", "format=xml", "/api/",
    "/rest/", "/v1/", "/v2/", "/v3/", "wikidata.org/wiki/special:entitydata",
    ".json", "accept: application/json", "opensearch", "/query?", "/sparql",
)
_HTML_PAGE_URL_RE = None


def _curl_html_vs_api_guard(command: str) -> str:
    """命中"curl/wget 拿整页 HTML 正文"(非 API 端点)→建议换 fetch_url_smart；
    命中结构化 API 端点特征→放行("")。建议性提示不硬拦截。"""
    global _HTML_PAGE_URL_RE
    if _REPLAY_EXEMPT:
        return ""
    c = command.lower()
    if not ("curl" in c or "wget" in c):
        return ""
    if any(h in c for h in _API_ENDPOINT_HINTS):
        return ""  # 结构化端点特征命中·合理用法·放行
    if _HTML_PAGE_URL_RE is None:
        import re as _re_html
        _HTML_PAGE_URL_RE = _re_html.compile(
            r"https?://[^\s\"']*\.(?:org|com|net)/wiki/[^\s\"'?]+"
            r"|https?://[^\s\"']*wikipedia\.org/[^\s\"']+")
    _m = _HTML_PAGE_URL_RE.search(command)
    if not _m:
        return ""  # 不是典型的百科/wiki 整页 URL 模式·不确定·不打扰
    return (
        "[提示·非拦截] 你在 curl 一个百科/wiki 整页 URL(非 API 端点)——"
        "这类页面直接 curl 拿到的是完整渲染前 HTML(含导航栏/侧边栏噪声)，"
        "不如换成结构化 API(如 Wikipedia 的 "
        "`?action=query&prop=extracts&format=json&titles=...`)拿到干净"
        "摘要更省字符，或直接用 **fetch_url_smart(完整URL)**(已适配"
        "8-14K 信息预算+反爬升级，比裸 curl 更完整)。如果你已经知道要"
        "curl 的是某个确定的 API 端点，忽略此提示继续即可。"
    )


def _curl_academic_antibot_guard(command: str) -> str:
    """命中"curl/wget 直连学术/强反爬全文站"→返回引导语;否则 ""(放行)。
    v13.94 · 修「pubchem 被误伤」:判定前先用 classify_target 复核一次
    真实 tier(它会应用 _SUBDOMAIN_TIER_OVERRIDE 白名单排除 pubchem 这类
    同父域名但走独立结构化 API 的场景)，避免这里的粗粒度域名子串命中
    和 search_agent_orchestrator 的精细判定口径不一致、各改各的漏同步。"""
    if _REPLAY_EXEMPT:   # v120 FixL: 赢链重放豁免
        return ""
    c = command.lower()
    if not ("curl" in c or "wget" in c):
        return ""
    for host in _get_academic_tier_hosts():
        if host not in c:
            continue
        try:
            from search_agent_orchestrator import classify_target
            import re as _re2
            _m = _re2.search(r"https?://[^\s\"']+", command)
            if _m and classify_target(_m.group(0)).get("tier", 0) == 0:
                continue  # 复核后是白名单域名(如 pubchem)，放行
        except Exception:
            pass
        return (
            f"[已拦截·会撞反爬] 你在用 curl/wget 直连 {host}，这类学术/强反爬"
            "站对自动化直连请求常返回 reCAPTCHA 验证页或空壳 HTML 而不是真"
            "正文(实证:米氏方程题 curl 拿到的是 21KB 全是 recaptcha 关键词的"
            "挑战页，换 5+ 次 UA/镜像 URL 全部撞墙)。**请改用 "
            "fetch_url_smart(完整URL)**——它内部按域名反爬强度自动分级选"
            "战术(普通页面 curl 秒回；这类强反爬站自动升级为真实浏览器"
            "渲染绕过验证)，不需要你自己判断该用什么方式。"
        )
    return ""


# ══ 增量1(Kimi 反爬方案·最核心) · 反爬拦截点自动降级隐身浏览器 ══════════════
#   病根: 上面的 _curl_academic_antibot_guard 命中时【只返回提示文本】→ agent
#   看到提示后往往又用 curl(或改 UA/镜像)重试 → 死循环撞墙·从不真正换武器。
#   修: 命中反爬拦截时·不再只给提示·而是【自动】把该 URL 交给隐身抓取梯度
#   (search_fabric_fetch: nodriver→patchright→camoufox)抓正文·抓到就直接存
#   evidence 并把正文返回给 agent(它拿到了真内容·不必再 curl)。
#   fail-safe: 隐身梯度失败/模块缺失/异常 → 返回 None·调用方退回原提示文本。
#   env: MEYO_ANTIBOT_AUTOESCALATE=0 关闭(默认开)·关了就是老行为(只提示)。
_ANTIBOT_URL_RE = None


def _antibot_autoescalate_enabled() -> bool:
    return os.environ.get("MEYO_ANTIBOT_AUTOESCALATE", "1").strip().lower() not in ("0", "false", "off", "no")


def _extract_first_url(command: str) -> str:
    """从 shell 命令里抠出第一个 http(s) URL(去掉尾随引号/反引号)。"""
    global _ANTIBOT_URL_RE
    try:
        import re as _re_u
        if _ANTIBOT_URL_RE is None:
            _ANTIBOT_URL_RE = _re_u.compile(r"https?://[^\s\"'`|>;]+")
        m = _ANTIBOT_URL_RE.search(command or "")
        return m.group(0).rstrip("'\"`") if m else ""
    except Exception:
        return ""


def _antibot_auto_escalate(command: str, qid: str = "", session: dict = None,
                           reason: str = "") -> str:
    """反爬拦截命中时自动走隐身梯度抓正文。成功→返回带正文的结果串(并存
    evidence);失败/关闭/无 URL→返回 ""(调用方退回原提示文本·fail-safe)。"""
    if not _antibot_autoescalate_enabled():
        return ""
    url = _extract_first_url(command)
    if not url:
        return ""
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _ev_dir = _Path(__file__).resolve().parent.parent.parent / "v11" / "evolution"
        if str(_ev_dir) not in _sys.path:
            _sys.path.insert(0, str(_ev_dir))
        import search_fabric_fetch as _sff
    except Exception:
        return ""   # 模块加载失败·fail-safe 退回提示
    try:
        # reason 为空时·escalate 内部默认按 antibot_challenge 分类(拦截点触发=反爬)
        res = _sff.escalate_fetch_stealth(url, query="", error_text=reason, timeout=40)
    except Exception:
        return ""
    if not (res and res.get("ok") and res.get("content")):
        return ""   # 隐身也失败·退回提示让 agent 换 fetch_url_smart/搜索
    content = res.get("content", "")
    via = res.get("via", "stealth")
    # 存 evidence(和 fetch_url_smart 成功路径一致·下游能消费)
    try:
        if session is not None:
            ev = session.setdefault("evidence", [])
            ev.append({"url": url, "title": url, "content": content,
                       "source": f"antibot_autoescalate({via})"})
    except Exception:
        pass
    preview = content[:600].replace("\n", " ")
    return (f"[反爬自动降级] 检测到 curl/wget 直连会撞反爬·已自动改走隐身浏览器"
            f"梯度({via})抓取 {url} · 拿到 {len(content)} 字符 · 已存 evidence"
            f"（不需要你再用 curl 重试）\n正文预览: {preview}")


def _inject_curl_maxtime(command: str, cap: int = 20) -> str:
    """F2 · curl/wget 没带超时参数时自动注入,快速失败而非干等到 shell 30s 超时。"""
    import re as _re
    c = command
    try:
        if _re.search(r"\bcurl\b", c) and "--max-time" not in c and "-m " not in c:
            c = _re.sub(r"\bcurl\b", f"curl --max-time {cap}", c, count=1)
        elif _re.search(r"\bwget\b", c) and "--timeout" not in c and "-T " not in c:
            c = _re.sub(r"\bwget\b", f"wget --timeout={cap} --tries=1", c, count=1)
    except Exception:
        return command
    return c


# ══ v108(M4·2026-08-16 GAIA-165复盘) · 下载/渲染产物三关校验 ═════════════
# 病根实证(3类):
#   ① 8d46: 下载的房源 xlsx 其实是反爬拦截 HTML——agent 在假文件上跑 pandas
#      算出错误答案还自信交卷;
#   ② 0b26: playwright 渲染 ScienceDirect 拿到 "There was a problem" 错误页
#      当成正文解析;
#   ③ f2fe: 声称是 PDF 的文件其实是 JS 挑战页。
# 通用机制: shell_exec 完成后·对本次命令【新产出】的文件做三关校验——
#   第一关(魔数): 扩展名 vs 文件头(%PDF/PNG/JPG/ZIP);
#   第二关(大小): 0 字节/小千阈值;
#   第三关(内容探针): HTML/text 里扫反爬/错误页特征串。
# 校验失败 → 在返回摘要里追加醒目警告·逼 agent 换源重下·不在假数据上推理。
# fail-safe: 任何异常静默(不影响 shell 主流程)。
_DOWNLOAD_CHECK_EXTS = {".pdf", ".json", ".html", ".htm", ".csv", ".xlsx", ".png", ".jpg", ".jpeg"}
_HTML_ERROR_MARKERS = (
    "there was a problem", "access denied", "attention required",
    "recaptcha", "just a moment", "captcha", "challenge-platform",
    "cf-browser-verification", "enable javascript", "403 forbidden",
    "service unavailable", "request blocked", "请求被拒", "人机验证",
    # 搜索引擎结果/中转页当"正文"是假证据(2026-08-26·0b26 实证:fetch 目标站却
    #   经 google_cache 回退·抓回的是 Google Search 结果页 HTML·被当 ScienceDirect
    #   正文推理→算错。命中即判可疑·逼 agent 换直链源·不在搜索页 HTML 上抽数。
    "<title>google search</title>", "google search</title>",
    "id=\"search\"", "did not match any documents",
    "your search -", "about the results", "<title>bing</title>",
    "duckduckgo", "consent.google", "before you continue to google",
)
_MAGIC_CHECKS = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".xlsx": (b"PK",),
}


def _probe_file_suspicious(path: str):
    """三关校验单个文件。返回 None(可信) 或 问题描述字符串。"""
    import os as _os
    try:
        if not _os.path.isfile(path):
            return None
        ext = _os.path.splitext(path)[1].lower()
        size = _os.path.getsize(path)
        if size == 0:
            return f"文件为 0 字节(下载未完成)"
        with open(path, "rb") as f:
            head = f.read(4096)
        # 第一关: 魔数
        if ext in _MAGIC_CHECKS and size > 0:
            if not any(head.startswith(m) for m in _MAGIC_CHECKS[ext]):
                _head_txt = head[:120].decode("utf-8", errors="replace").strip()
                return (f"魔数不匹配: 声称 {ext} 但文件头是 {_head_txt[:60]!r}"
                        "——很可能是反爬拦截页/错误页伪装的假文件")
        # 第二关: 大小异常小(非空但 <200B 的"数据文件"几乎必是错误响应)
        if ext in (".json", ".csv", ".xlsx", ".pdf") and size < 200:
            return f"文件仅 {size} 字节(过小·大概率是错误响应不是真数据)"
        # 第三关: 内容探针(html/htm/json)
        if ext in (".html", ".htm", ".json"):
            _low = head.decode("utf-8", errors="replace").lower()
            for m in _HTML_ERROR_MARKERS:
                if m in _low:
                    return f"内容探针命中错误页特征({m!r})——拿到的是反爬/错误页不是真正文"
        if ext == ".json":
            try:
                import json as _json_p
                with open(path, encoding="utf-8") as f:
                    _json_p.load(f)
            except Exception:
                pass  # jsonl 等宽松格式不误报·魔数/探针已覆盖主场景
        return None
    except Exception:
        return None


def _validate_shell_downloads(qid, command, work_dir, t_start, summary):
    """shell_exec 完成后追加到 summary 的产物校验警告(无警告返回原 summary)。

    采集本次命令新产出文件的途径: ①curl/wget 的 -o/--output 显式路径;
    ②work_dir 内 mtime ∈ [t_start-1, now] 且扩展名在检查集内的新文件;
    ③/tmp 下同窗口的检查集文件(限4个·治 playwright 存 /tmp 的场景)。
    """
    try:
        import re as _re_v
        _cand = []
        for m in _re_v.finditer(r"(?:-o|--output)[= ]\s*(\S+)", command):
            p = m.group(1).strip("\"'")
            if p and not p.startswith("-"):
                _cand.append(p if os.path.isabs(p) else os.path.join(work_dir, p))
        _lo = command.lower()
        _is_net = ("curl" in _lo) or ("wget" in _lo) or ("python" in _lo) or ("playwright" in _lo)
        if _is_net:
            _now = time.time()
            _win = (t_start - 1.0, _now + 1.0)
            for _dir, _cap in ((work_dir, 8), ("/tmp", 4)):
                try:
                    _dir_start = len(_cand)
                    for name in os.listdir(_dir):
                        if len(_cand) - _dir_start >= _cap:
                            break
                        p = os.path.join(_dir, name)
                        if os.path.splitext(name)[1].lower() not in _DOWNLOAD_CHECK_EXTS:
                            continue
                        try:
                            mt = os.path.getmtime(p)
                        except Exception:
                            continue
                        if _win[0] <= mt <= _win[1] and p not in _cand:
                            _cand.append(p)
                except Exception:
                    continue
        warns = []
        for p in _cand[:6]:
            _w = _probe_file_suspicious(p)
            if _w:
                warns.append(f"· {os.path.basename(p)}: {_w}")
        if warns:
            return (summary +
                    "\n\n[⚠️ v108 产物校验失败·别在这些文件上推理]\n" + "\n".join(warns) +
                    "\n→ 这是反爬拦截页/错误响应/残缺下载·不是真数据。请:①换源重下"
                    "(fetch_url_smart 有反爬分级) ②或改用官方 API/镜像 ③拿到真文件"
                    "后再解析。基于假文件的任何计算结果都是错的。")
        return summary
    except Exception:
        return summary


# ═══════════════════════════════════════════════════════════════════════
# v98(2026-08-11 用户「给agent通用性·三个通用缺口机制·全部不碰YouTube这个词」)
#   缺口1 工具前置条件不可见·缺口2 失败没分类·缺口3 缺一跳(定位资源URL)没被发现。
#   对应三个通用机制:①工具前置条件Schema+会教学的错误消息 ②失败三分法+输入修复
#   重试环 ③locate前置跳进策略库。下面是两个纯确定性基建(仪表·非设定值·合法)。
# ═══════════════════════════════════════════════════════════════════════

def classify_url(url: str) -> str:
    """【仪表】纯机械识别 URL 形态(等价 file 命令)·不含任何站点专属解题逻辑。
    返回资源类型: video_page/search_page/channel/playlist/article/pdf/
    download/api/image/list/homepage/unknown。让 agent 自己发现"我手里这个是
    搜索页不是视频页"·从而知道还缺"定位确切资源"这一跳。通用跨域(视频/论文/
    数据集/商品都用它判手里 URL 是不是目标资源页)。"""
    import re as _re
    u = (url or "").strip().lower()
    if not u or not u.startswith(("http://", "https://", "www.", "ftp://")):
        return "not_url"  # 是描述/搜索词·不是 URL
    _path = u.split("?", 1)[0]
    _query = u.split("?", 1)[1] if "?" in u else ""
    # 资源页形态不能只看路径：``?v=natural language query`` 看似视频页，实际仍是
    # 搜索意图。通用判据是不依赖某站 ID 长度：解码后的资源参数包含空白即不是原子标识。
    try:
        import urllib.parse as _up_url
        _decoded = _up_url.parse_qs(_query, keep_blank_values=True)
        _resource_values = [value for key, values in _decoded.items()
                            if key.lower() in {"v", "id", "video", "media", "resource"}
                            for value in values]
        if any(any(ch.isspace() for ch in str(value))
               for value in _resource_values if str(value).strip()):
            return "search_page"
    except Exception:
        pass
    # 搜索页(任何站点·通用形态:路径含 search/results 或 query 含 search/q=/query)
    if (_re.search(r"/(search|results|find)\b", _path)
            or _re.search(r"(^|&)(search_query|q|query|keyword|kw)=", _query)):
        return "search_page"
    # 直链文件(按扩展名·通用)
    if _re.search(r"\.(pdf)(\?|$)", _path):
        return "pdf"
    if _re.search(r"\.(mp4|mkv|webm|mov|avi|m3u8)(\?|$)", _path):
        return "video_file"
    if _re.search(r"\.(png|jpe?g|gif|webp|bmp|svg)(\?|$)", _path):
        return "image"
    if _re.search(r"\.(zip|tar|gz|csv|xlsx?|json|xml|parquet)(\?|$)", _path):
        return "download"
    # 视频页(通用视频站形态·非只 YouTube:watch/video/embed/shorts/v/ 段)
    if (_re.search(r"/(watch|embed|shorts|clip)\b", _path)
            or _re.search(r"(^|&)v=", _query)
            or _re.search(r"youtu\.be/[\w-]+", u)
            or _re.search(r"/video/\d", _path)):
        return "video_page"
    # 频道/用户页(通用:channel/user/@handle/c/)
    if _re.search(r"/(channel|user|c|@[\w-]+)\b", _path):
        return "channel"
    if _re.search(r"/(playlist)\b", _path) or "list=" in _query:
        return "playlist"
    # API 端点
    if "/api/" in _path or _path.startswith(("https://api.", "http://api.")):
        return "api"
    # 文章/详情页(有较深路径·像具体页面) vs 首页(仅域名)
    _after_host = _re.sub(r"^\w+://[^/]+", "", _path).strip("/")
    if not _after_host:
        return "homepage"
    if _re.search(r"/(wiki|article|news|post|abs|doi|p)/|/\d{4,}", _path):
        return "article"
    return "unknown"  # 有路径但形态不明·交给 agent 判断


# 工具前置条件 Schema(缺口1):每个工具声明自己的【输入契约】(工具天然属性·
#   非题型知识·不违反反硬编码铁律)。reject_hint 是"会教学的错误消息"——不说
#   "下载异常"(零信息),而是命名缺失的前置跳,让 agent 知道该先做哪一跳。
#   key=工具名,url_types=该工具能吃的 URL 形态白名单,reject_hint 用 {actual} 占位。
_TOOL_PRECONDITIONS = {
    "see_video_frame": {
        "url_types": {"video_page", "video_file", "not_url"},  # not_url=描述·工具内部会搜
        "reject_hint": (
            "你传给 see_video_frame 的是【{actual}】(不是视频页 URL 也不是搜索描述)。"
            "本工具只吃①确切视频页 URL(带 watch?v=/youtu.be/等) 或 ②一句视频搜索描述"
            "(如'GameGrumps Mario Kart 8 Deluxe May 2017'·工具会自己搜)。你缺一个前置跳:"
            "【从题目描述定位确切视频·或直接把描述当搜索词传进来】——别传搜索结果页/频道页 URL。"),
    },
    "read_table": {
        "url_types": {"download", "pdf", "not_url", "unknown", "article"},
        "reject_hint": (
            "你传给 read_table 的是【{actual}】。本工具读表格/文档文件(xlsx/csv/docx)·"
            "需要确切文件直链或本地路径·不是网页/搜索页。缺前置跳:先拿到文件的直链地址。"),
    },
}


