# -*- coding: utf-8 -*-
"""execution_tools · auto 的执行/文件/视觉/视频工具工厂。

2026-08-27 从 qa_auto_agent god-file 抽出(原2286行最大单块)。工具实现按 qid 绑定，
预算/日志/安全/状态等共享依赖由 auto 显式 configure 注入；本模块不反向 import god-file。
后续可继续按 Hermes Footprint Ladder 拆成 terminal/file/vision/video toolsets 给军团复用。
"""
import json
import os
import re
import subprocess as _subprocess
import sys
import tempfile
import time


def configure(**dependencies):
    globals().update(dependencies)


def _make_execution_tools(qid):
    """通用执行工具三件套：shell_exec / file_read / file_write。"""

    def shell_exec(command: str, cwd: str = "", timeout_s: float = 60) -> str:
        """在沙箱工作目录(或指定 cwd)里执行 shell 命令，返回 stdout/stderr/
        exit_code。写代码、跑测试、装依赖、查环境都用这个。timeout_s 最高
        300 秒，超时会被强制杀掉。危险的毁灭性命令(fork bomb/rm -rf 根目录
        或主目录)会被拦截并提示，不会真的执行。"""
        t0 = time.time()
        s = _sess(qid)
        # 按命令内容判重:连续 curl 不同 API 做考据是正常的,只有完全相同命令
        # 连续重复才算空转(见 _CONTENT_DEDUP_TOOLS)
        allow, guard_msg = _check_budget(s, "shell_exec", content_key=f"{command}|{cwd}")
        if not allow:
            _log_tool(qid, "shell_exec", {"command": command}, guard_msg, time.time() - t0)
            return guard_msg

        danger = _is_dangerous_shell(command)
        if danger:
            summary = f"[已拦截] {danger}。如果你确实需要类似效果，请换成更精确、范围更小的命令。"
            _log_tool(qid, "shell_exec", {"command": command}, summary, time.time() - t0)
            return summary

        # 解题循环不能修改宿主系统。brew/apt/yum 等系统包管理器会长时间阻塞、
        # 污染环境且绕开已有能力栈；缺能力应先调用已注册工具或 scan_environment。
        # 这不限制任务沙箱内的确定性脚本，也不替 agent 决定题解。
        import re as _re_shell_policy
        if _re_shell_policy.search(r"(?:^|[;&|]\s*)?(?:sudo\s+)?(?:brew|apt(?:-get)?|yum|dnf|pacman)\s+(?:install|upgrade|update)\b", command, _re_shell_policy.I):
            summary = (
                "[解题环境边界] 已拦截系统包管理命令：它会修改宿主环境且容易长时间阻塞，"
                "不是当前题目的证据或计算步骤。请优先使用已注册的专用能力（如 sympy_calc、"
                "tesseract_ocr、pdf_extract）或 scan_environment 确认现有环境；只有用户明确要求配置环境时才应安装系统包。"
            )
            _log_tool(qid, "shell_exec", {"command": command}, summary, time.time() - t0)
            return summary

        # 路径隔离(2026-08-09):shell_exec 是自由文本命令，不能像 file_read/
        # file_write 那样先解析出单一路径再校验，但同样的红线必须挡住——
        # 否则 file_read 挡了 "读 token 文件"，agent 转头一句
        # `cat ~/.meyo/agent_web_tokens.json` 就绕过去了，等于白防。这里
        # 用命令文本里的路径片段匹配同一套 _SENSITIVE_PATH_PATTERNS(不做
        # 反引号/变量展开等复杂 shell 语法解析，命中字面路径片段就够挡住
        # 绝大多数误用/试探，真正处心积虑用编码/间接方式绕过的攻击不在
        # 这层工具级防御的目标范围内，那是操作系统级沙箱该管的事)。
        cmd_path_violation = _shell_command_path_violation(qid, command)
        if cmd_path_violation:
            _log_tool(qid, "shell_exec", {"command": command}, cmd_path_violation, time.time() - t0)
            return cmd_path_violation

        # F1 · 拦"curl/wget 抓搜索引擎/反爬站"(治 35/44 次 curl 超时根因)——不放它
        # 干等 30s,引导换 run_parallel_search。结构化 API/直链照常放行。
        # v120 FixL: 赢链重放期间豁免(_REPLAY_EXEMPT·guard 内自查)。
        _se_guard = _curl_search_engine_guard(command)
        if _se_guard:
            _log_tool(qid, "shell_exec", {"command": command}, _se_guard, time.time() - t0)
            return _se_guard
        # F3 · 拦"curl/wget 抓视频站网页"(治多模态题手撸 shell·引导 transcribe_audio/
        # run_parallel_search)。yt-dlp 抽音轨放行。
        _vid_guard = _curl_video_site_guard(command)
        if _vid_guard:
            _log_tool(qid, "shell_exec", {"command": command}, _vid_guard, time.time() - t0)
            return _vid_guard
        # F4 · 拦"curl/wget 直连学术/强反爬全文站"(治 v96i 反爬分级优化被绕过·
        # agent 无视 fetch_url_smart 直接手撸 curl 撞 reCAPTCHA)。
        _acad_guard = _curl_academic_antibot_guard(command)
        if _acad_guard:
            # 增量1: 不再只给提示·先自动走隐身梯度抓该 URL(抓到就返回正文·存 evidence)。
            #   fail-safe: 隐身也失败→退回原提示文本(_acad_guard)·行为不破坏。
            _escalated = _antibot_auto_escalate(command, qid=qid, session=s,
                                                reason=_acad_guard)
            _out = _escalated or _acad_guard
            _log_tool(qid, "shell_exec", {"command": command}, _out, time.time() - t0)
            return _out
        # F2 · curl/wget 没带超时的自动注入 --max-time(快速失败·不干等到 shell 超时)
        command = _inject_curl_maxtime(command)
        # v-eff2(病1复核) · 建议性提示(不硬拦)：curl 拿百科/wiki 整页正文而非 API 端点时
        #   执行完成后在结果尾部附上提示，不影响本次命令执行。
        _html_hint = _curl_html_vs_api_guard(command)

        # R2 · HITL 审批:风险命令(非毁灭性但该问一下,如 sudo/git push/rm -r)挂起
        # 等用户在网页批。CLI/无网页场景 MEYO_HITL_MODE=off 时全放行,不破坏旧行为。
        # v120 FixL: 赢链重放豁免(_REPLAY_EXEMPT)——重放命令是历史已验证跑过的,
        # 无人值守回测场景挂起等待=必 deny·链断。(nat9 04a04a9b 实证 HITL deny 挡路)
        if not _REPLAY_EXEMPT and _HITL_AVAILABLE and _hitl.needs_approval("shell_exec", command):
            _log_tool(qid, "shell_exec", {"command": command}, "[待审批] 已挂起等你批准…", time.time() - t0)
            verdict = _hitl.request_approval(qid, "shell_exec", command)
            if verdict.get("decision") != "approved":
                summary = f"[已拒绝执行] 用户未批准这条命令({verdict.get('decision')})。换个更安全的做法,或说明为什么需要它。"
                _log_tool(qid, "shell_exec", {"command": command}, summary, time.time() - t0)
                return summary

        work_dir = _resolve_path(qid, cwd) if cwd else _sandbox_dir(qid)
        if cwd:
            cwd_violation = _path_violation(qid, work_dir)
            if cwd_violation:
                _log_tool(qid, "shell_exec", {"command": command, "cwd": cwd}, cwd_violation, time.time() - t0)
                return cwd_violation
        try:
            os.makedirs(work_dir, exist_ok=True)
        except Exception:
            pass
        real_timeout = min(max(1.0, float(timeout_s or 60)), 300.0)

        # v13.95 · 系统代理透传：把探测到的 VPN proxy 注入子进程 env
        # 子进程继承父进程 env，再追加代理变量(不覆盖已有的)。
        # 这样 curl/pip/git/python -c requests 等都能走代理。
        _proxy_env = _get_system_proxy_env()
        _exec_env = {**os.environ, **_proxy_env} if _proxy_env else None

        try:
            r = _subprocess.run(
                command, shell=True, cwd=work_dir,
                capture_output=True, text=True, timeout=real_timeout,
                env=_exec_env,
                # 资源限制(2026-08-09):preexec_fn 只在 POSIX 且非 Windows 时
                # 生效(_HAS_RESOURCE=False 时 _shell_preexec_limits 是空函数，
                # 传了也无副作用)，在 fork 出的子进程里设 CPU/内存/进程数上限，
                # 防止单条命令(死循环/内存泄漏/意外 fork 风暴)拖垮宿主机上
                # 其他用户正在跑的任务。
                preexec_fn=_shell_preexec_limits if _HAS_RESOURCE else None,
            )
            stdout = (r.stdout or "")[:4000]
            stderr = (r.stderr or "")[:4000]
            summary = (
                f"exit_code={r.returncode} · cwd={work_dir}\n"
                f"[stdout]\n{stdout or '(空)'}\n"
                + (f"[stderr]\n{stderr}\n" if stderr else "")
            )
            if _html_hint:
                summary += "\n" + _html_hint
            # ── 独立可进化模块接线(v98F · 2026-08-13·核心文件仅此一处改·架构活) ──
            # shell 失败时·调 shell_error_handler(白名单内·带pytest·进化能安全改它)拿分级建议:
            # 网络类→退避重试/降级换传输·脚本类→修脚本·环境类→别硬磕。这段"错误分级+重试/降级"
            # 逻辑之前【核心文件里没有】(进化诊断反复指出的缺口)·现在收进独立模块·进化改那个模块
            # 就能自愈这类题·不碰7600行核心。fail-safe:模块缺失/出错退回原行为(不拼hint)。
            if r.returncode != 0:
                try:
                    import shell_error_handler as _seh
                    _cnt = (s.get("_budget") or {}).get("_shell_fail_by_cmd", {})
                    _attempt = _cnt.get(command[:60], 0) + 1
                    _cnt[command[:60]] = _attempt
                    _rs_sessions.ensure_budget(s)["_shell_fail_by_cmd"] = _cnt
                    _seh_hint = _seh.advise_hint(command, r.returncode, stdout, stderr, attempt=_attempt)
                    if _seh_hint:
                        summary += "\n" + _seh_hint
                except Exception:
                    pass  # 模块缺失/出错→退回原行为·不影响 shell_exec 主流程
            # v77d(2026-08-10)环境资源错误识别(用户抓的第2题真bug:视觉题遇
            #   `fork: Resource temporarily unavailable`【狂重试 120 次】死循环·撞
            #   interrupted)。通用治法:识别"环境资源问题"(fork/资源不可用/no space/
            #   too many open files)——这【不是题目问题·是机器资源耗尽】·重试同类
            #   命令没意义。
            # v96b(2026-08-10) · 改滑动窗口(见 _check_budget 环境资源熔断段落注释):
            #   旧版按"连续"计数,会被 echo/ls 这类必成功的轻量探测命令清零,导致
            #   census/dragons 两题实测中熔断从未触发、死循环到 recursion_limit。
            #   这里只负责记录本次是否命中,真正的窗口判断+硬拦截在 _check_budget。
            _env_err_markers = ("resource temporarily unavailable", "fork:",
                                "cannot allocate memory", "too many open files",
                                "no space left", "cannot fork")
            _low = (stderr or "").lower()
            _is_env_err = bool(r.returncode != 0 and any(m in _low for m in _env_err_markers))
            _sb = s.get("_budget") or {}
            _win = _sb.get("env_err_window")
            if _win is None:
                _win = []
            _win.append(_is_env_err)
            _ENV_ERR_WINDOW_SIZE = 8
            if len(_win) > _ENV_ERR_WINDOW_SIZE:
                _win = _win[-_ENV_ERR_WINDOW_SIZE:]
            _sb["env_err_window"] = _win
            s["_budget"] = _sb
            if _is_env_err:
                _hits = sum(1 for x in _win if x)
                summary += (
                    f"\n\n[⚠️ 环境资源错误] 本次因机器资源耗尽(fork/内存/句柄不足)失败"
                    f"(最近{len(_win)}次里已有{_hits}次)。这不是题目问题,是运行环境紧张,"
                    "多次命中会被硬拦截 shell_exec,建议尽早换用 run_parallel_search 等"
                    "不 fork 子进程的工具。")
        except _subprocess.TimeoutExpired:
            summary = f"[超时] 命令执行超过 {real_timeout}s 被强制终止。"
            # v97 通用超时自愈:自动分析命令在干哪类活→给对应换法(不代劳·嵌入必经失败点)
            summary += _timeout_selfheal_hint(command)
        except Exception as e:
            summary = f"shell_exec 异常: {type(e).__name__}: {str(e)[:200]}"

        # v108(M4) · 下载/渲染产物三关校验(魔数+大小+内容探针): 在假文件上
        # 推理是 8d46/0b26/f2fe 三题的共同错因——下载声称成功≠拿到真数据。
        try:
            summary = _validate_shell_downloads(qid, command, work_dir, t0, summary)
        except Exception:
            pass

        # v108b(M3b) · shell_exec 专属返回点补挂单源对账: shell_exec 有自己的
        # handler 早返回·不经过通用分发尾部——bec7/8d46 的 curl 全走这里。
        # 先把本次调用记入域名累计器(完整 command 只在此刻可得·tool_log 落盘
        # 已截断)·再查是否已形成单源支配→一次性提醒。
        try:
            if "http" in command:
                _sb_m3b = s.get("_budget")
                if _sb_m3b is None:
                    _sb_m3b = s["_budget"] = {}
                _m3b_record_host(_sb_m3b, "shell_exec", command, summary)
                _rc_note = _single_source_reconcile_nudge(s)
                if _rc_note:
                    summary = summary + _rc_note
        except Exception:
            pass

        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "shell_exec", {"command": command, "cwd": cwd}, summary, time.time() - t0)
        return summary

    def file_read(path: str, max_bytes: int = 20000) -> str:
        """读取文件内容。相对路径解析到本任务的沙箱工作目录，绝对路径原样
        读取。max_bytes 限制读取长度(默认 2万字节)，防止大文件撑爆上下文，
        超出部分会被截断并提示。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "file_read")
        if not allow:
            _log_tool(qid, "file_read", {"path": path}, guard_msg, time.time() - t0)
            return guard_msg

        real_path = _resolve_path(qid, path)
        # 路径隔离(2026-08-09):file_read 此前对路径完全没有任何限制，用
        # 绝对路径可以直接读走 token 表/ssh 密钥/其他用户沙箱——这是先拒绝
        # 再执行的红线检查，不像 file_write 那样走 HITL 商量，因为"读走
        # 别人凭证"没有任何正当理由需要用户手动批准放行。
        violation = _path_violation(qid, real_path)
        if violation:
            _log_tool(qid, "file_read", {"path": path}, violation, time.time() - t0)
            return violation
        try:
            if not os.path.exists(real_path):
                summary = f"文件不存在: {real_path}"
            elif os.path.isdir(real_path):
                entries = os.listdir(real_path)[:100]
                summary = f"{real_path} 是目录，内容({len(entries)}项): {entries}"
            else:
                cap = min(max(100, int(max_bytes or 20000)), 200000)
                with open(real_path, "rb") as f:
                    blob = f.read(cap + 1)
                truncated = len(blob) > cap
                text = blob[:cap].decode("utf-8", errors="replace")
                summary = text + (f"\n\n[...已截断，文件超过 {cap} 字节...]" if truncated else "")
        except Exception as e:
            summary = f"file_read 异常: {type(e).__name__}: {str(e)[:200]}"

        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "file_read", {"path": path}, summary[:200], time.time() - t0)
        return summary

    def file_write(path: str, content: str, mode: str = "overwrite") -> str:
        """写入文件。相对路径写入本任务的沙箱工作目录，绝对路径原样写入
        (写入前想清楚后果，尤其是覆盖模式)。mode="overwrite"(默认，覆盖)
        或 "append"(追加)。会自动创建父目录。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "file_write")
        if not allow:
            _log_tool(qid, "file_write", {"path": path}, guard_msg, time.time() - t0)
            return guard_msg

        real_path = _resolve_path(qid, path)
        # 路径隔离(2026-08-09):敏感路径/跨用户沙箱是硬红线，直接拒绝，
        # 不走下面的 HITL 商量流程——写别人凭证文件没有"用户批准就可以"
        # 的正当性,跟 file_read 保持同一优先级(先红线拒绝，HITL 审批
        # 只处理"沙箱外但本身不违规"的场景，例如写到仓库目录)。
        violation = _path_violation(qid, real_path)
        if violation:
            _log_tool(qid, "file_write", {"path": path}, violation, time.time() - t0)
            return violation
        # R2 · HITL 审批:写沙箱外的绝对路径(可能改仓库/系统文件)挂起等用户批
        if _HITL_AVAILABLE and _hitl.needs_approval("file_write", real_path):
            _log_tool(qid, "file_write", {"path": path}, "[待审批] 写沙箱外·已挂起等你批准…", time.time() - t0)
            verdict = _hitl.request_approval(qid, "file_write", f"写入 {real_path}")
            if verdict.get("decision") != "approved":
                summary = f"[已拒绝写入] 用户未批准写 {real_path}({verdict.get('decision')})。改写到沙箱内,或说明理由。"
                _log_tool(qid, "file_write", {"path": path}, summary, time.time() - t0)
                return summary
        try:
            os.makedirs(os.path.dirname(real_path) or ".", exist_ok=True)
            file_mode = "a" if mode == "append" else "w"
            with open(real_path, file_mode, encoding="utf-8") as f:
                f.write(content)
            summary = f"已{'追加' if mode == 'append' else '写入'} {len(content)} 字符到 {real_path}"
        except Exception as e:
            summary = f"file_write 异常: {type(e).__name__}: {str(e)[:200]}"

        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "file_write", {"path": path, "mode": mode}, summary, time.time() - t0)
        return summary

    def read_image_ocr(path_or_url: str) -> str:
        """对图片做 OCR 抽取里面的文字。支持:①本地图片路径(.png/.jpg/.jpeg/.gif/.bmp/.tiff·
        相对路径解析到沙箱)②图片URL(http/https)③PDF路径(扫描件/图片PDF·会转页OCR)。
        用于:题目附件是图片/截图/图表(轴标签/图例/水印)、照片里的文字、扫描件——这些
        信息不在网页文本/PDF文本层里,只能靠视觉OCR读出。返回抽到的文字(或失败原因)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "read_image_ocr", content_key=path_or_url)
        if not allow:
            _log_tool(qid, "read_image_ocr", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import sys as _sys
            _ev = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "v11", "evolution")
            if _ev not in _sys.path:
                _sys.path.insert(0, _ev)
            from web_access_bridge import WebAccess
            wa = WebAccess()
            src = (path_or_url or "").strip()
            _lo = src.lower()
            if src.startswith(("http://", "https://")):
                # 图片URL / PDF URL → 走 read_url(已含图片OCR+扫描件PDF OCR分支)
                res = wa.read_url(src)
                txt = (res or {}).get("content", "") if isinstance(res, dict) else ""
                via = (res or {}).get("via", "?") if isinstance(res, dict) else "?"
            else:
                # 本地路径: 相对→沙箱; PDF→_ocr_pdf_pages; 图片→直接tesseract
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "read_image_ocr", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    summary = f"read_image_ocr: 文件不存在 {real}"
                    _log_tool(qid, "read_image_ocr", {"src": src}, summary, time.time() - t0)
                    return summary
                if _lo.endswith(".pdf"):
                    txt = wa._ocr_pdf_pages(real, _max_pages=3) or ""
                    via = "local_pdf_ocr"
                else:
                    import pytesseract  # type: ignore
                    from PIL import Image  # type: ignore
                    with Image.open(real) as _img:
                        _w, _h = _img.size
                        if max(_w, _h) < 1000:
                            _sc = min(3, 1000 // max(1, min(_w, _h)) + 1)
                            _img = _img.resize((_w * _sc, _h * _sc))
                        txt = pytesseract.image_to_string(_img).strip()
                    via = "local_image_ocr"
            if txt:
                summary = f"[OCR·{via}] 抽到 {len(txt)} 字:\n{txt[:6000]}"
            else:
                summary = f"[OCR·{via}] 未抽到文字(纯图/照片无文本·或下载失败)"
        except ImportError as _ie:
            summary = f"read_image_ocr: OCR库未装({str(_ie)[:60]})·可先 pip_install pytesseract Pillow"
        except Exception as e:
            summary = f"read_image_ocr 异常: {type(e).__name__}: {str(e)[:150]}"
        # v96(2026-08-10) · 视觉卫兵计数:本次 OCR 是否"无效"(抽不到字/结果太短/异常)。
        # 只看"抽到 N 字"这个成功标记,没有就算无效——累计给 _check_budget 的视觉卫兵判断。
        _ocr_ok = False
        try:
            import re as _re_ocr
            _m = _re_ocr.search(r"抽到\s*(\d+)\s*字", summary)
            _ocr_ok = bool(_m and int(_m.group(1)) >= 20)
        except Exception:
            _ocr_ok = False
        _budget96 = s.get("_budget")
        if _budget96 is not None:
            if _ocr_ok:
                _budget96["ocr_ineffective_streak"] = 0
            else:
                _budget96["ocr_ineffective_streak"] = _budget96.get("ocr_ineffective_streak", 0) + 1
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "read_image_ocr", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def see_image_vlm(path_or_url: str, question: str = "") -> str:
        """用视觉大模型(VLM)直接"看"图回答问题,而不是只抽文字。
        用于 read_image_ocr 抽不到有效信息或题目本质是【视觉推理】的场景:
        棋局/图表结构/手写公式/图像中物体计数与颜色/截图里的界面布局/
        需要理解图像空间关系而非单纯读文字 —— 这些 OCR 抽字没用,要模型真正"看图"。
        用法: 先 read_image_ocr 试一次,如果答案对不上或图片明显是"要看"而不是"要读字"
        (棋盘/图表/手绘/界面截图),再用本工具把图和问题一起交给 VLM。
        question 传空则默认让模型详细描述图片内容。
        v13.92 OP-4 · 用真实 GAIA 视觉错题验证过(国际象棋/分数列表/评分测验/
        价格表/彩色数字网格 5/5 真实调用成功,能看懂图像结构)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "see_image_vlm", content_key=path_or_url)
        if not allow:
            _log_tool(qid, "see_image_vlm", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import sys as _sys
            _agent_dir = os.path.dirname(os.path.abspath(__file__))
            if _agent_dir not in _sys.path:
                _sys.path.insert(0, _agent_dir)
            from see_image import see as _see
            src = (path_or_url or "").strip()
            if src.startswith(("http://", "https://")):
                local_or_url = src
            else:
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "see_image_vlm", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    summary = f"see_image_vlm: 文件不存在 {real}"
                    _log_tool(qid, "see_image_vlm", {"src": src}, summary, time.time() - t0)
                    return summary
                local_or_url = real
            q = question.strip() or "详细描述这张图片的内容,包括所有可见的文字、数字、图形结构。"
            # v99b(2026-08-12 · 混排worksheet结构化共识)硬前置JSON-only schema:
            #   探针证明软追加VLM无视·硬前置(You MUST respond with ONLY JSON)VLM乖乖出结构。
            #   只在题目/页面像worksheet(含"image/图/worksheet/quiz/problem/答案")时前置·
            #   避免污染普通视觉题的自由描述。fail-safe:import失败就用原q。
            try:
                import vision_consensus as _vc
                _lowq = (q or "").lower()
                if any(k in _lowq for k in ("worksheet", "quiz", "problem", "fraction",
                                            "answer", "作答", "答案", "题", "分数")):
                    q = _vc.SCHEMA_PROMPT_STRICT + q
            except Exception:
                pass
            ans = _see(local_or_url, q)
            # v97c(2026-08-11) 网关瞬时抖动韧性:VLM返空是网关间歇性返空(实测手动连调稳定返
            #   1781字·但agent那次恰好撞返空)——不是图/逻辑问题。空返回自动重试2次(短backoff),
            #   把"时灵时不灵"磨平,让下游放大触发条件拿到真内容判断。不代劳·纯抗抖动韧性。
            if not (ans or "").strip():
                for _retry in range(2):
                    try:
                        time.sleep(1.5 * (_retry + 1))
                        ans = _see(local_or_url, q)
                        if (ans or "").strip():
                            break
                    except Exception:
                        pass
            summary = (f"[VLM看图] {ans[:2000]}" if ans
                      else "[VLM看图] 未返回内容(网关重试3次仍空·可能网关持续不可用)")
            # v99f(2026-08-13 · 目标"让他自己用成功招法"·修引导位置):
            #   上版把skill_search引导挂在"搜文字被拦"处·但worksheet题一上来就直接看图·
            #   根本不撞那关卡→引导0触发。改到【首次看图返回时】直接把匹配的成功招法how_to
            #   附进summary·agent立刻看到可复用方法(不依赖它主动skill_search)。只注入一次·
            #   只在命中win-tactic skill时。fail-safe。
            try:
                if not budget.get("_win_tactic_hinted"):
                    budget["_win_tactic_hinted"] = True
                    import skill_library as _sl3
                    _hh = _sl3.search_skills(
                        (session.get("question") or "") + " 读图 图像 worksheet 分数 手写 多行", 3)
                    _best = next((h for h in _hh if "win-tactic" in (h.get("name") or "")
                                  and h.get("score", 0) >= 0.4), None)
                    if _best:
                        _full = _sl3.load_skill(_best["name"])
                        _howto = (_full.get("body") or _full.get("content") or "")[:600] if isinstance(_full, dict) else ""
                        if _howto:
                            summary += (f"\n\n★[已匹配成功招法 {_best['name']}·照它做]★\n{_howto}"
                                        f"\n(这是从真实成功解题提炼的方法·不是答案·照方法自己读)")
            except Exception:
                pass
            # v97(2026-08-11) 嵌入必经路径:VLM自认看不清→自动裁剪放大重读(不依赖agent主动选see_image_zoom)。
            # 洞察:有效自进化改造=嵌入agent必经路径(如triage注入约束),而非加可选工具等agent主动用。
            # see_image_zoom采纳惯性同see_video_frame——所以把放大精读焊进see_image_vlm的低置信兜底。
            # 触发放大的两种情况:①VLM返回了但自认看不清 ②VLM整个空返回(网关抖动)——
            #   后者更该放大兜底:整图这次没读出来,切块各自独立调用可能成功(实测polyv97
            #   整图VLM返None但grid_scan_read能读出紫色边长)。
            # v99d(2026-08-13 · M-82 病第三次根治): 措辞触发→语义判断。
            #   病根: _lowconf 和 _q_wants_read 和 _vlm_partial 全靠关键词列表
            #   (英文+中文枚举)·换个措辞就漏·永远打地鼠。
            #   修法(机制而非设定值):
            #     _lowconf: 不靠关键词·靠 VLM 返回信息量(数字密度+文本长度)判低置信
            #     _q_wants_read: 不靠关键词·靠 triage 的 modality_needs(已有语义判断)
            #                    + 问题含数值提取意图(检查答案是否需要从图里读数字)
            #     _vlm_partial: 不靠关键词·靠 VLM 返回的数字数量 < 期望值
            _ans_text = str(ans or "")
            _ans_nums = re.findall(r'-?\d+(?:\.\d+)?', _ans_text)
            _ans_text_len = len(_ans_text.strip())
            # 低置信语义判据: 空返回 / 数字极少 / 文本极短(信息量不足)
            _lowconf = (not ans) or _ans_text_len < 20 or (
                len(_ans_nums) == 0 and _ans_text_len < 100
            )
            # v99q(2026-08-13): VLM 主动说"看不清/无法阅读"但返回了很长的解释
            #   →_lowconf=False→不触发自动放大→agent 跳过 zoom。
            #   通用修:不用固定词表(换个措辞就漏)·用 LLM 判断 VLM 回复是否表达了
            #   "没能从图中读到有效信息"。fail-safe:LLM 异常返回 False(不误拦)。
            _vlm_says_unclear = False
            if _ans_text_len >= 100 and not _lowconf:
                try:
                    import reflective_mind as _RM_uc
                    _uc_raw = _RM_uc._mm().ask("claude-sonnet",
                        f"判断以下 VLM(视觉语言模型)看图后的回复·是否表达了「没能从图中读到"
                        f"有效信息/看不清/无法辨认/内容不可读」的意思?\n"
                        f"只看 VLM 是否明确表达了自己读不到/读不清——不看它具体在描述什么。\n"
                        f"输出JSON: {{\"unclear\": true}} 或 {{\"unclear\": false}}\n"
                        f"只输出一个JSON。\n\nVLM回复(前500字): {_ans_text[:500]}",
                        timeout=15, max_tokens=50)
                    import re as _re_uc, json as _json_uc
                    _uc_s = (_uc_raw or "").strip()
                    _i_uc, _j_uc = _uc_s.find("{"), _uc_s.rfind("}")
                    if _i_uc >= 0 and _j_uc > _i_uc:
                        _uc_d = _json_uc.loads(_uc_s[_i_uc:_j_uc+1])
                        _vlm_says_unclear = _uc_d.get("unclear", False)
                except Exception:
                    pass  # fail-safe:LLM 异常不误判
            _lowconf = _lowconf or _vlm_says_unclear
            # 题目侧: 是否属于“读图后还要精确运算”——只消费 triage 的模态语义，
            # 不从中英文措辞反推意图。纯抄写/识别题即使不命中此分支，低信息量门仍会
            # 触发裁块精读；它们不应被错误地推去计算。
            _triage_mods = set((s.get("triage") or {}).get("modality_needs") or [])
            _q_wants_read = {"vision", "compute"}.issubset(_triage_mods)
            # VLM 侧: 首轮读数是否缺失——语义判据:
            #   VLM 返回的数字数量不足以支持多值计算。
            _vlm_partial = (not ans) or len(_ans_nums) < 2
            _reading_numbers = _q_wants_read and _vlm_partial
            # v99l(2026-08-13·分数图17项实证):题目要"列出全部/every/all/逐个+按顺序"时·
            #   整图读【必漏】(实测整图只读9个题干·grid逐块读到手写答案区5/35,7/21等·命中11/17)。
            #   这类"穷举提取"题即使整图返回了内容(_lowconf=False)也要强制走 grid 扫描+合并·
            #   否则整图读一次就收下残缺答案。语义判据(不硬编码单一措辞):
            #   问题含"全部/所有/every/all/list/列出/逐个/each" + 提取意图·且答案是多项(列表)。
            _q_low = (q or "").lower() + " " + (s.get("question") or "").lower()
            _wants_full_list = any(k in _q_low for k in (
                "every ", "all ", "list ", "each ", "全部", "所有", "列出", "逐个", "每一个", "每个",
            )) and any(k in _q_low for k in (
                "fraction", "answer", "value", "number", "item", "分数", "答案", "数值", "项", "条目",
            ))
            # 整图读到的条目数(粗判是否可能漏:穷举题读到的越少越可能漏)
            _n_items_read = len(set(re.findall(r"\d+\s*/\s*\d+", _ans_text))) + len(_ans_nums)
            # ── 墙钟预算闸(2026-08-12·实证:auto-zoom 4宫格=4次额外VLM调用·让单次
            #   see_image_vlm 从7.5s涨到87-92s·同步塞在一个工具调用里·墙钟闸只在工具"入口"
            #   查→查不到这80s→整个wall被一次工具吃光→2工具就撞280s hard-wall gave_up)。
            #   修:放大前先看已耗时·超过 hard_wall 的一半就【不做4宫格】(太贵)·改留轻提示让
            #   agent 需要时自己对准区域 see_image_zoom(单块·便宜)。有时间才做全套4宫格。
            _b_wall = (s.get("_budget") or {})
            _elapsed_now = time.time() - float(_b_wall.get("t_start") or time.time())
            _wall_cap = float(_b_wall.get("hard_wall_s") or 900)
            _zoom_time_ok = _elapsed_now < _wall_cap * 0.5
            # 只在本地图(非URL)且未指定region时自动放大——URL放大需先下载,本地图crop_zoom_read能直接处理
            if (_lowconf or _reading_numbers or _wants_full_list) and not src.startswith(("http://", "https://")) and not _zoom_time_ok:
                # 时间不够做多宫格·留轻提示(不塞VLM调用·不吃墙钟)
                summary += ("\n[读数可能不全·时间预算有限] 若上面读得不全/有矛盾·别急着收尾:"
                            "用 see_image_zoom 对准【缺的那一小块】单独放大重读(比全扫快)·"
                            "读全再作答。"
                            + ("\n[⚠️穷举提取题·整图读必漏] 这题要列出全部条目·整图读几乎肯定漏"
                               "(实测整图只读到一部分)·务必分区域 see_image_zoom 逐块读全再合并去重按顺序作答。"
                               if _wants_full_list else ""))
            elif (_lowconf or _reading_numbers or _wants_full_list) and not src.startswith(("http://", "https://")):
                try:
                    import see_image_zoom as _siz
                    # v99l:穷举提取题(_wants_full_list)用更细 3x3 grid(整图必漏·实测3x3读到
                    #   手写答案区·命中11/17 >> 整图9且全读题干区)·普通低置信仍用 2x2(省时)。
                    _grid_dim = (3, 3) if _wants_full_list else (2, 2)
                    _zr = _siz.grid_scan_read(local_or_url, q, grid=_grid_dim, zoom=2)
                    if _zr.get("ok") and _zr.get("answer"):
                        _why = ("整图VLM空返回" if not ans
                                else ("读数计算题·切块读准每个小数字" if _reading_numbers
                                      else "整图没读清"))
                        # 2026-08-12 · 不再[:1200]截断切块读数(分数题17个分数会被砍)——切块结果
                        #   本身就是"读全"的产物·截断=白切;给足额度·超长才截且打截断标。
                        _zra = str(_zr.get("answer") or "")
                        if len(_zra) > 4000:
                            _zra = _zra[:4000] + "\n[⚠️切块读数过长被截断·如仍缺请分区域see_image_zoom]"
                        summary += (f"\n[自动放大精读·{_why}→切4块各放大2x精读] {_zra}")
                        # v97d(2026-08-11) 落地self_evolve自诊断(conf0.72)"感知-计算分离":VLM读得到
                        #   数字但边看边心算复杂计算(几何面积/多值聚合)会算错。读数计算题在放大读准
                        #   数字后·强制提示把数字交给代码精确算·别让VLM/你自己心算(嵌入必经路径版)。
                        if _reading_numbers:
                            # 2026-08-12 · 治「感知-计算分离提示把纯读题引偏去算」(实证:分数题
                            #   瓶颈是没读全17个·不是没算·提示却猛推'写代码算'误导)。分两步:
                            #   第一步永远是【先读全+交叉核对】(感知韧性)·第二步才是【有算术才交代码算】。
                            summary += (
                                "\n[⚠️读全优先·感知韧性·必做] 上面切块读到的可能【不完整/互相矛盾】——"
                                "**先别急着下结论/finalize**。第一步:把各块+整图读到的条目【全部列出来、去重、数一遍数量】,"
                                "确认没漏(题目问几个/列表多长·就核对到那个数);若某块说'空白/没有'而别块有内容,"
                                "以有内容的为准,并【再看一次整图】补齐。读不全就换 see_image_zoom 对准缺的区域重读,别放弃。"
                                "\n[⚠️感知-计算分离·仅当有算术时] 若题目【需要对读到的数字做运算】(面积/求和/百分比/计数聚合),"
                                "读全后把数字写进 shell_exec 用 python 精确算·别心算(例:正交多边形→方向+边长累加成顶点·"
                                "鞋带公式 area=½|Σ(x_i·y_{i+1}−x_{i+1}·y_i)|)。若题目只是【原样抄写读到的条目】(如把每个分数按序写出),"
                                "则【不需要算】·把读全、核对顺序做扎实即可,别被'用代码算'带偏。")
                        _rs_sessions.ensure_budget(s)["auto_zoom_used"] = True
                except Exception as _ze:
                    summary += f"\n[自动放大精读失败] {type(_ze).__name__}"
        except Exception as e:
            summary = f"see_image_vlm 异常: {type(e).__name__}: {str(e)[:150]}"
        # v96(2026-08-10) · 调过 VLM 就说明已经在正确路径上,视觉卫兵不用再逼它。
        # ── 门控病修复(2026-08-12 · 错题归因·古德哈特) ──
        # 病根(实证墓碑题):vlm_ever_used 原来一【被调用】就置真·不管成不成功。
        # 于是 VLM 明说"cannot make out any readable text"(读不出)、甚至上面
        # except 异常了·卫兵也不再逼它换源→agent 把【猜测】当【读取】交卷。
        # 修法(校验证据·不校验动作):只在 VLM【真抽到实质内容】时才算"模态已消费"·
        # 明确说看不清/读不出/异常时【不置真】·让视觉卫兵继续引导换源(如切文本源
        # Find-a-Grave)或结构化声明"此路不通+原因"·而不是拿猜测满足门控。
        _budget96b = s.get("_budget")
        if _budget96b is not None:
            _sl = (summary or "").lower()
            _vlm_failed = (
                summary.startswith("see_image_vlm 异常")
                or any(m in _sl for m in (
                    "cannot make out", "can't make out", "unable to read",
                    "no readable text", "cannot read", "can't read",
                    "too blurry", "看不清", "读不出", "无法辨认", "无法读", "识别不出"))
            )
            if not _vlm_failed:
                # 真消费了模态证据 → 卫兵放行(在正确路径上了)
                _budget96b["vlm_ever_used"] = True
                _budget96b["ocr_ineffective_streak"] = 0
            # else: VLM 读不出 → 不置真·保持逼它换源/声明墙(证据未消费)
        # G3(2026-08-12): 本次读取作为独立候选进投票池(不 blend·供 finalize 截断取最强)
        _record_observation_anchor(s, "see_image_vlm", path_or_url, summary)
        _record_perception_vote(s, "see_image_vlm", path_or_url, summary)
        # v98t(2026-08-12 · 推导链痕迹门的抽值端) · 结构化标注 VLM 读出的数值。
        #   病根(多边形题实证):VLM 读清了所有紫色边长(10,6,4,8,6,1…),但没有任何机制
        #   强制"读出数字之后必须去代码里算"——读得越清楚越绕开放大/计算分支,越没人算,
        #   最后靠 VLM 目测 14 边凹多边形面积→错→gave_up。CC 归因"VLM 硬精度边界"是错的
        #   (第八次边界误判):精度没问题,是【计算缺失】被误诊成【感知不足】。
        #   正确形式(机制而非魔法数字+关键词表·治触发集枚举病第三次发作):
        #   这里只做一件通用、无措辞/无语言依赖的事——把 VLM 返回文本里的所有数值抽出来
        #   累积进 budget["_vlm_extracted_values"]。是否"该去算"的判断交给 finalize 时的
        #   推导链痕迹门(_maybe_block_finalize)用语义/结构判据做(答案是否是这些提取值的
        #   函数 + 本题有没有真跑过 shell_exec),而不是在这里用关键词表软提示。
        try:
            import re as _re_pc2
            _nums_in_ans = _re_pc2.findall(r"-?\d+(?:\.\d+)?", str(summary))
            if _nums_in_ans and (s.get("_budget") is not None):
                _ev = s["_budget"].setdefault("_vlm_extracted_values", [])
                # 只累积、去重(保序)、封顶防爆——供 finalize 门判"答案是否可由提取值推导"
                for _n in _nums_in_ans:
                    if _n not in _ev:
                        _ev.append(_n)
                del _ev[400:]
        except Exception:
            pass
        # finish_reason 感知消费(2026-08-12):see_image 截断时在文本末尾打 [⚠️TRUNCATED] 标
        #   (多边形题实证:VLM 逐边 re-trace 被砍在中途→读数不完整)。这里是消费点:
        #   截断=这批读数不完整·明确提示 agent 别拿它作答·先重读/放大补全(不静默当完整用)。
        try:
            if "[⚠️TRUNCATED" in str(summary):
                _b_tr = s.get("_budget")
                if _b_tr is not None:
                    _b_tr["_vlm_truncated_count"] = int(_b_tr.get("_vlm_truncated_count", 0) or 0) + 1
                summary = (str(summary) +
                           "\n[⚠️读数不完整·必做] 上面 VLM 输出被 max_tokens 截断(如逐边读到一半就断了)——"
                           "这批数字/读数【不完整】,别拿它直接作答或计算。先【放大/分块重读】把缺的部分补齐,"
                           "确认读全了再往下走。")
                try:
                    _emit_gate_event(qid, "vlm_truncated", "warn",
                                     {"count": int((s.get("_budget") or {}).get("_vlm_truncated_count", 0))})
                except Exception:
                    pass
        except Exception:
            pass
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "see_image_vlm", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def read_table(path_or_url: str, max_rows: int = 200, sheet: str = "") -> str:
        """读表格/文档结构化内容为文本。支持:①.xlsx/.xlsm(→openpyxl·可指定sheet)
        ②.csv/.tsv(→内置csv)③.docx(→python-docx·段落+表格)。本地路径(相对→沙箱)或URL。
        用于:题目附件是电子表格/文档(每格数据/多sheet/嵌入表格)——这些信息在二进制文件里,
        file_read当文本读会拿到乱码。返回按行渲染的表格文本(markdown风格·截max_rows行)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "read_table", content_key=f"{path_or_url}|{sheet}")
        if not allow:
            _log_tool(qid, "read_table", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        real = None
        _tmp_dl = None
        try:
            src = (path_or_url or "").strip()
            _lo = src.lower().split("?")[0]
            # URL → 先下载到临时文件
            if src.startswith(("http://", "https://")):
                import tempfile as _tf, subprocess as _sp
                _sfx = next((e for e in (".xlsx", ".xlsm", ".csv", ".tsv", ".docx")
                             if _lo.endswith(e)), ".bin")
                _fd, _tmp_dl = _tf.mkstemp(suffix=_sfx)
                os.close(_fd)
                _sp.run(["curl", "-sL", "--max-time", "20", "--max-filesize",
                         str(10 * 1024 * 1024), "-o", _tmp_dl, src],
                        capture_output=True, timeout=25)
                real = _tmp_dl
            else:
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "read_table", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    summary = f"read_table: 文件不存在 {real}"
                    _log_tool(qid, "read_table", {"src": src}, summary, time.time() - t0)
                    return summary
            _rl = real.lower()
            _lines = []
            if _rl.endswith((".xlsx", ".xlsm")):
                import openpyxl  # type: ignore
                _wb = openpyxl.load_workbook(real, read_only=True, data_only=True)
                _sheets = [sheet] if sheet and sheet in _wb.sheetnames else _wb.sheetnames
                for _sn in _sheets:
                    _ws = _wb[_sn]
                    _lines.append(f"### Sheet: {_sn}")
                    _n = 0
                    for _row in _ws.iter_rows(values_only=True):
                        if _n >= max_rows:
                            _lines.append(f"...(超过 {max_rows} 行·已截断)")
                            break
                        _cells = ["" if _c is None else str(_c) for _c in _row]
                        if any(_cells):
                            _lines.append(" | ".join(_cells))
                            _n += 1
                _wb.close()
            elif _rl.endswith((".csv", ".tsv")):
                import csv as _csv
                _delim = "\t" if _rl.endswith(".tsv") else ","
                with open(real, newline="", encoding="utf-8", errors="replace") as _f:
                    for _i, _row in enumerate(_csv.reader(_f, delimiter=_delim)):
                        if _i >= max_rows:
                            _lines.append(f"...(超过 {max_rows} 行·已截断)")
                            break
                        _lines.append(" | ".join(_row))
            elif _rl.endswith(".docx"):
                import docx  # type: ignore
                _doc = docx.Document(real)
                for _p in _doc.paragraphs:
                    if _p.text.strip():
                        _lines.append(_p.text)
                for _ti, _tbl in enumerate(_doc.tables):
                    _lines.append(f"### 表格{_ti + 1}")
                    for _row in _tbl.rows:
                        _lines.append(" | ".join(_c.text for _c in _row.cells))
                        if len(_lines) > max_rows:
                            break
            else:
                summary = (f"read_table: 不支持的格式 {real}(仅 xlsx/xlsm/csv/tsv/docx)。"
                           "图片用 read_image_ocr·PDF用 read_image_ocr 或直接读 URL。")
                _log_tool(qid, "read_table", {"src": src}, summary, time.time() - t0)
                return summary
            _txt = "\n".join(_lines).strip()
            summary = (f"[read_table] {len(_lines)} 行:\n{_txt[:8000]}"
                       if _txt else "[read_table] 文件解析成功但内容为空")
        except ImportError as _ie:
            summary = f"read_table: 库未装({str(_ie)[:60]})·可先 pip_install openpyxl python-docx"
        except Exception as e:
            summary = f"read_table 异常: {type(e).__name__}: {str(e)[:150]}"
        finally:
            try:
                if _tmp_dl and os.path.exists(_tmp_dl):
                    os.remove(_tmp_dl)
            except Exception:
                pass
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "read_table", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def read_pdf(path_or_url: str, question: str = "") -> str:
        """读 PDF 附件并带着问题遍历它,返回答案+证据。用于:题目附件是 PDF
        (表格/报表/名单/多页文档)——本工具走「可寻址空间+证据引导遍历」骨架:
        把 PDF 建成带(page,bbox)坐标的块索引(数字PDF走pdfplumber·扫描件自动升级
        docling OCR),再由统一遍历循环 observe→decide→act 找出答案。比 read_image_ocr
        (把PDF当图OCR·读不出结构化表)准得多。question 传你要从PDF里找/算的具体问题。

        v13.98(2026-08-13·PDF一竖到底接入):原 read_table 明确不支持 PDF·PDF 只能走
        read_image_ocr 图片式读→读不到干净表。本工具补齐这条链:pdf_space(indexer+reader)
        → traversal_core(骨架) → 这里注册成工具·agent 遇 PDF 题自己会调。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "read_pdf", content_key=f"{path_or_url}|{question}")
        if not allow:
            _log_tool(qid, "read_pdf", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        _tmp_dl = None
        try:
            src = (path_or_url or "").strip()
            # URL → 下载到临时文件
            if src.startswith(("http://", "https://")):
                import tempfile as _tf, subprocess as _sp
                _fd, _tmp_dl = _tf.mkstemp(suffix=".pdf")
                os.close(_fd)
                _sp.run(["curl", "-sL", "--max-time", "20", "--max-filesize",
                         str(10 * 1024 * 1024), "-o", _tmp_dl, src],
                        capture_output=True, timeout=25)
                real = _tmp_dl
            else:
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "read_pdf", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    summary = f"read_pdf: 文件不存在 {real}"
                    _log_tool(qid, "read_pdf", {"src": src}, summary, time.time() - t0)
                    return summary
            # 骨架驱动遍历:PdfSpace 挂 traversal_core
            import importlib
            _tc = importlib.import_module("traversal_core")
            _sp_mod = importlib.import_module("space_pdf")
            goal = (question or "").strip() or "Summarize the key structured content of this PDF."
            space = _sp_mod.PdfSpace(real)
            if not space.index.get("ok"):
                summary = f"read_pdf: PDF 解析失败·{space.index.get('reason','')[:120]}"
                _log_tool(qid, "read_pdf", {"src": src}, summary, time.time() - t0)
                return summary
            r = _tc.traverse(space, goal, max_steps=8)
            _note = space.index.get("reason", "")
            if r.ok:
                summary = f"[read_pdf·{space.index.get('backend','?')}] 答案: {r.answer[:1500]}\n(遍历{r.steps}步·{_note})"
            else:
                # 不静默:遍历没收敛也把证据摘要回传·让 agent 继续判断
                _ev = "; ".join(str(e.get("content",""))[:200] for e in r.evidence[-3:])
                summary = (f"[read_pdf·未收敛·{r.stop_reason}] 已读到证据: {_ev[:1200]}"
                           f"\n(可再调 read_pdf 换更具体 question·或本 PDF={_note})")
        except ImportError as _ie:
            summary = f"read_pdf: 依赖未装({str(_ie)[:80]})·pdfplumber 应已装·扫描件需 docling"
        except Exception as e:
            summary = f"read_pdf 异常: {type(e).__name__}: {str(e)[:150]}"
        finally:
            try:
                if _tmp_dl and os.path.exists(_tmp_dl):
                    os.remove(_tmp_dl)
            except Exception:
                pass
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "read_pdf", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def query_table(path_or_url: str, question: str = "") -> str:
        """读表格附件(xlsx/xlsm/csv/tsv)并带着问题遍历它,返回答案+证据。用于:题目附件是
        电子表格(销量/名单/库存/清单),要做筛选/求和/分组/最值/对比这类结构化查询。本工具走
        「可寻址空间+证据引导遍历」骨架:表→带表头/分节的结构化记录(自动识别装饰标题行、
        分节行如DVD/Blu-Ray作为_section列保留、裁空列)→统一遍历循环求答案。

        比 read_table(只把表渲染成文本给你自己看+自己心算·大数求和易错)更准:内置"数值聚合
        精确计算不心算"原则。question 传你要从表里查/算的具体问题。多sheet会自动遍历。

        v13.98(2026-08-13·CSV/Excel一竖到底):space_table(indexer+reader·复用pdf_space表格清洗)
        → traversal_core(骨架·同PDF) → 这里注册·agent遇表格题自己会调。回测6/6(求和/对比/筛选/分节)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "query_table", content_key=f"{path_or_url}|{question}")
        if not allow:
            _log_tool(qid, "query_table", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        _tmp_dl = None
        try:
            src = (path_or_url or "").strip()
            _lo = src.lower().split("?")[0]
            if src.startswith(("http://", "https://")):
                import tempfile as _tf, subprocess as _sp
                _sfx = next((e for e in (".xlsx", ".xlsm", ".csv", ".tsv") if _lo.endswith(e)), ".xlsx")
                _fd, _tmp_dl = _tf.mkstemp(suffix=_sfx)
                os.close(_fd)
                _sp.run(["curl", "-sL", "--max-time", "20", "--max-filesize",
                         str(10 * 1024 * 1024), "-o", _tmp_dl, src],
                        capture_output=True, timeout=25)
                real = _tmp_dl
            else:
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "query_table", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    summary = f"query_table: 文件不存在 {real}"
                    _log_tool(qid, "query_table", {"src": src}, summary, time.time() - t0)
                    return summary
            import importlib
            _tc = importlib.import_module("traversal_core")
            _st_mod = importlib.import_module("space_table")
            goal = (question or "").strip() or "Summarize the key structured content of this table."
            space = _st_mod.TableSpace(real)
            if not space.ok:
                summary = f"query_table: 表格解析失败·{space.error[:120]}"
                _log_tool(qid, "query_table", {"src": src}, summary, time.time() - t0)
                return summary
            r = _tc.traverse(space, goal, max_steps=8)
            _note = f"{len(space.sheets)}个sheet"
            if r.ok:
                summary = f"[query_table] 答案: {r.answer[:1500]}\n(遍历{r.steps}步·{_note})"
            else:
                _ev = "; ".join(str(e.get("content", ""))[:200] for e in r.evidence[-2:])
                summary = (f"[query_table·未收敛·{r.stop_reason}] 已读到: {_ev[:1200]}"
                           f"\n(可再调 query_table 换更具体 question)")
        except ImportError as _ie:
            summary = f"query_table: 依赖未装({str(_ie)[:80]})·openpyxl/pandas 应已装"
        except Exception as e:
            summary = f"query_table 异常: {type(e).__name__}: {str(e)[:150]}"
        finally:
            try:
                if _tmp_dl and os.path.exists(_tmp_dl):
                    os.remove(_tmp_dl)
            except Exception:
                pass
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "query_table", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def traverse_image(path_or_url: str, question: str = "") -> str:
        """带问题遍历一张图(空间检索)。整图读不清时自动分网格逐块精读·证据图登记·
        比单次 see_image_vlm 更能抠细节。用于:图里信息分散/有小字细节/需定位某区域再读。
        走「可寻址空间(bbox地址)+证据引导遍历」骨架:full整图→grid分块→区域精读。
        question 传你要从图里找/读的具体问题。(棋盘/乐谱等有专用求解器的仍优先专用工具)"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "traverse_image", content_key=f"{path_or_url}|{question}")
        if not allow:
            _log_tool(qid, "traverse_image", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            src = (path_or_url or "").strip()
            if not src.startswith(("http://", "https://")):
                real = _resolve_path(qid, src)
                violation = _path_violation(qid, real)
                if violation:
                    _log_tool(qid, "traverse_image", {"src": src}, violation, time.time() - t0)
                    return violation
                if not os.path.exists(real):
                    r = f"traverse_image: 文件不存在 {real}"
                    _log_tool(qid, "traverse_image", {"src": src}, r, time.time() - t0)
                    return r
                src = real
            import importlib
            _tc = importlib.import_module("traversal_core")
            _si = importlib.import_module("space_image")
            goal = (question or "").strip() or "Read and describe all details in this image."
            # 先整体观察，再把中间证据驱动的 bbox/grid 跳转交给统一遍历器。
            # grid 数量按问题复杂度由决策器选择，不把某类图片或题目写死。
            space = _si.ImageSpace(src, grid=(3, 3))
            r = _tc.traverse(space, goal, max_steps=7)
            _record_traversal_result(s, "traverse_image", r, goal)
            if r.ok:
                summary = f"[traverse_image] 答案: {r.answer[:1500]}\n(遍历{r.steps}步)"
            else:
                _ev = "; ".join(str(e.get("content", ""))[:200] for e in r.evidence[-2:])
                summary = f"[traverse_image·未收敛·{r.stop_reason}] 已读到: {_ev[:1000]}"
        except Exception as e:
            summary = f"traverse_image 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "traverse_image", {"src": path_or_url}, summary, time.time() - t0)
        return summary

    def traverse_video(url: str, question: str = "") -> str:
        """带问题遍历视频时间轴(时间检索)。先扫时间轴定位含文字/关键时刻·再跳到该时刻精读画面·
        证据图登记。用于:问"视频某处画面显示什么/标题卡/记分牌"。比盲猜时间戳精读更准。
        走「可寻址空间(时间戳地址)+证据引导遍历」骨架:index扫时间轴→t:{秒}精读。
        url 传 YouTube 链接或搜索描述(内置代理·自动搜)。问台词内容用 transcribe_audio。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "traverse_video", content_key=f"{url}|{question}")
        if not allow:
            _log_tool(qid, "traverse_video", {"src": url}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import importlib
            _tc = importlib.import_module("traversal_core")
            _sa = importlib.import_module("space_av")
            goal = (question or "").strip() or "What is shown in this video?"
            # 把闭包层真实 ASR 注入 AV 空间；音频不再只是“请另调工具”的提示地址。
            space = _sa.AVSpace((url or "").strip(), audio_transcriber=transcribe_audio)
            # 时间轴索引、帧间比较和未探索时段均由 EvidenceMap 维护；给复杂视频
            # 留出足够的“扫→精读→比较”回合，而非只抽一帧就结束。
            r = _tc.traverse(space, goal, max_steps=7)
            _record_traversal_result(s, "traverse_video", r, goal)
            if r.ok:
                summary = f"[traverse_video] 答案: {r.answer[:1500]}\n(遍历{r.steps}步)"
                # traverse 成功同样是已消费的视频原始证据；必须与抽帧/转录
                # 走同一门控契约，不能因工具名称不同而继续被视频硬约束拦住。
                _mark_video_evidence_consumed(s, r.answer, level="read", source="traverse_video")
                _record_observation_anchor(s, "traverse_video", url, summary)
            else:
                _ev = "; ".join(str(e.get("content", ""))[:200] for e in r.evidence[-2:])
                summary = (f"[traverse_video·未收敛·{r.stop_reason}] 已读到: {_ev[:1000]}"
                           f"\n(视频下不动=本机网络拦视频站·换二手资料:转录站/wiki/fandom)")
        except Exception as e:
            summary = f"traverse_video 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "traverse_video", {"src": url}, summary, time.time() - t0)
        return summary

    def traverse_web(seed_or_url: str, question: str = "") -> str:
        """带问题遍历网页信息空间。输入搜索词或 URL：先搜索/打开入口，再从页面正文中
        发现可继续读取的链接与分页候选，按中间证据动态选择下一页。用于多页资料、目录页、
        需要从页面间关系找信息的任务；普通已知单页 URL 仍可用 fetch_url_smart 快速直取。
        正文复用 smart_fetch 的浏览器渲染/反爬分级，页面链接仅作可审计的导航候选。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "traverse_web", content_key=f"{seed_or_url}|{question}")
        if not allow:
            _log_tool(qid, "traverse_web", {"seed": seed_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import importlib
            _tc = importlib.import_module("traversal_core")
            _sw = importlib.import_module("space_web")
            seed = (seed_or_url or "").strip()
            goal = (question or "").strip() or seed or "Find the relevant information on the web."
            space = _sw.WebSpace(seed_query=seed if not seed.startswith(("http://", "https://")) else "")
            # 已知 URL 直接作为首个可观察地址，避免无意义地先搜索一次。
            if seed.startswith(("http://", "https://")):
                space.entry_addrs = lambda: [f"url:{seed}"]
            r = _tc.traverse(space, goal, max_steps=7)
            _record_traversal_result(s, "traverse_web", r, goal)
            if r.ok:
                summary = f"[traverse_web] 答案: {r.answer[:1500]}\\n(遍历{r.steps}步)"
            else:
                _ev = "; ".join(str(e.get("content", ""))[:200] for e in r.evidence[-3:])
                summary = f"[traverse_web·未收敛·{r.stop_reason}] 已读到: {_ev[:1200]}"
        except Exception as e:
            summary = f"traverse_web 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "traverse_web", {"seed": seed_or_url}, summary, time.time() - t0)
        return summary

    def gui_operate(operation: str, question: str = "") -> str:
        """操作桌面GUI(点击/键盘/挪窗)——最像人也最危险的能动空间(可写·不可逆)。
        operation:
          · "read"           → 截屏+VLM看当前屏幕(只读·不需审批·先看再操作)
          · "click:x,y"      → 在屏幕坐标点击
          · "type:文本"       → 键盘输入文本
          · "key:回车/tab等"  → 按键
          · "activate:应用名"  → 切到某应用窗口(可逆·不需审批)
        安全红线(不可绕过):click/type/key 这类不可逆写操作【执行前必须过 hitl 人工审批】·
        用户未批准则不执行。需系统授权(屏幕录制+辅助功能)·没授权明确回传。
        用于:题目要求真操作桌面软件(填表单/点按钮/操作GUI程序)。question 是读屏时要看什么。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "gui_operate", content_key=operation)
        if not allow:
            _log_tool(qid, "gui_operate", {"op": operation}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import importlib
            _sg = importlib.import_module("space_gui")
            space = _sg.GuiSpace(qid, require_approval=True)  # 审批门开(红线)
            op = (operation or "").strip()
            if op == "read" or op.startswith("window:"):
                addr = "screen" if op == "read" else op
                obs = space.observe(addr, question)
                summary = f"[gui_operate·read] {str(obs.content)[:1200]}\n({obs.note[:80]})"
            else:
                # 写操作/切窗:走 space_gui.act(内部强制审批·系统权限门)
                from traversal_core import Action
                r = space.act(Action(kind="act", target=op))
                summary = f"[gui_operate·{op.split(':')[0]}] ok={r.get('ok')} · {r.get('detail','')[:200]}"
        except Exception as e:
            summary = f"gui_operate 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "gui_operate", {"op": operation}, summary, time.time() - t0)
        return summary

    def ask_human(question: str) -> str:
        """卡住时向人求助(委托人类空间)。用于:多方尝试后仍拿不到关键信息、需要人工提供
        (私有资料/主观判断/权限内的东西)——像人一样卡住会问人。把 question 交给用户,
        阻塞等人回复(网页端),拿到答复并入证据。fail-safe:超时/没人答明确回传,不静默假装。
        别滥用:能自己查/自己算的先自己做,真穷尽了再问。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "ask_human", content_key=question)
        if not allow:
            _log_tool(qid, "ask_human", {"q": question}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import importlib
            _sh = importlib.import_module("space_human")
            from traversal_core import Action
            space = _sh.HumanSpace(qid)
            r = space.act(Action(kind="delegate", target="human", query=question))
            if r.get("ok"):
                summary = f"[ask_human] 人已回复: {str(r.get('answer',''))[:1200]}"
            else:
                summary = f"[ask_human·未问到] {r.get('detail','')[:200]}"
        except Exception as e:
            summary = f"ask_human 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "ask_human", {"q": question}, summary, time.time() - t0)
        return summary

    def transcribe_audio(path_or_url: str, max_seconds: int = 600, start_seconds: float = 0) -> str:
        """把音频/视频转成文字(语音识别·faster-whisper)。支持:①本地音视频文件
        (.mp3/.wav/.m4a/.mp4/.mov/.webm·相对→沙箱)②直链音视频URL③YouTube/视频站
        链接(→yt-dlp抽音轨)。用于:题目附件是录音/视频、或问「某YouTube视频里说了啥/
        第N个短片里的数字」——这些信息只在音轨里,读不到文字。返回转录文本(截max_seconds秒·
        防超长)。首次会下载whisper模型(~150MB·base)。
        start_seconds: v100(2026-08-14):从视频/音频的第N秒开始转录(不从头)。
        用于:题目问「2分钟处说了啥」→传 start_seconds=110 先裁剪再转录·
        避免从头转到目标时刻之前就截断。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "transcribe_audio", content_key=path_or_url)
        if not allow:
            _log_tool(qid, "transcribe_audio", {"src": path_or_url}, guard_msg, time.time() - t0)
            return guard_msg
        import tempfile as _tf, subprocess as _sp, glob as _glob
        _workdir = None
        _audio = None
        try:
            src = (path_or_url or "").strip()
            _lo = src.lower().split("?")[0]
            _workdir = _tf.mkdtemp(prefix="transcribe_")
            _VID_SITES = ("youtube.com", "youtu.be", "vimeo.com", "bilibili.com",
                          "dailymotion.com", "twitch.tv")
            if any(_v in src.lower() for _v in _VID_SITES):
                # v100c: 检测非视频URL(搜索结果页/频道页)·给有用提示
                _src_low = src.lower()
                if "/results?" in _src_low or "/search?" in _src_low or "/channel/" in _src_low:
                    return (
                        "transcribe_audio: 传入的URL不是视频页·是搜索结果页/频道页。"
                        "请先用 shell_exec(yt-dlp --flat-playlist '搜索URL') 或 run_parallel_search "
                        "找到具体视频的watch?v=XXX链接·再传给 transcribe_audio。"
                        "transcribe_audio 需要直接的视频URL·不能是搜索页/列表页。"
                    )
                # 视频站 → yt-dlp 抽音轨为 wav(16k·whisper偏好)·走系统代理/VPN
                _out = os.path.join(_workdir, "a.%(ext)s")
                _ytcmd = ["yt-dlp", "-x", "--audio-format", "wav",
                          "--postprocessor-args", "-ar 16000",
                          "--max-filesize", "100M", "-o", _out, src]
                try:
                    import video_frame as _vf
                    _px = _vf._detect_proxy()
                    if _px:
                        _ytcmd = _ytcmd[:1] + ["--proxy", _px] + _ytcmd[1:]
                except Exception:
                    pass
                _r = _sp.run(_ytcmd, capture_output=True, text=True, timeout=180)
                _cands = _glob.glob(os.path.join(_workdir, "a.*"))
                if not _cands:
                    return (f"transcribe_audio: yt-dlp 抽音轨失败(rc={_r.returncode})·"
                            f"{(_r.stderr or '')[:150]}")
                _audio = _cands[0]
            elif src.startswith(("http://", "https://")):
                _sfx = next((e for e in (".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm")
                             if _lo.endswith(e)), ".bin")
                _audio = os.path.join(_workdir, "a" + _sfx)
                _sp.run(["curl", "-sL", "--max-time", "120", "--max-filesize",
                         str(100 * 1024 * 1024), "-o", _audio, src],
                        capture_output=True, timeout=130)
            else:
                _real = _resolve_path(qid, src)
                _violation = _path_violation(qid, _real)
                if _violation:
                    return _violation
                if not os.path.exists(_real):
                    return f"transcribe_audio: 文件不存在 {_real}"
                _audio = _real
            if not _audio or not os.path.exists(_audio) or os.path.getsize(_audio) == 0:
                return "transcribe_audio: 未拿到可转录的音频文件(下载/抽取失败)"
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as _ie:
                return (f"transcribe_audio: 转录库未装({str(_ie)[:60]})·"
                        "可先 pip_install faster-whisper·或用 shell_exec 调 whisper CLI")
            # base 模型·CPU·int8(轻快)。模型缓存复用·首次下载~150MB。
            # 本机直连 huggingface 常超时(EDR/网络)·未设镜像时兜底走 hf-mirror(纯增量·
            # 已设则尊重用户配置)。不影响已缓存模型的加载。
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # xet+镜像不兼容(401)·走传统下载
            # v100: 如果指定了 start_seconds·先用 ffmpeg 裁剪到目标时刻再转录
            # 避免从头转到目标时刻之前就截断(whisper 慢·max_seconds 不够)
            _transcribe_file = _audio
            _time_offset = 0.0
            if start_seconds > 0:
                _clipped = os.path.join(_workdir or tempfile.gettempdir(), "clipped.wav")
                _clip_cmd = ["ffmpeg", "-y", "-ss", str(start_seconds),
                             "-i", _audio, "-t", str(max_seconds),
                             "-ar", "16000", "-ac", "1", _clipped]
                _cr = _sp.run(_clip_cmd, capture_output=True, text=True, timeout=60)
                if _cr.returncode == 0 and os.path.exists(_clipped) and os.path.getsize(_clipped) > 0:
                    _transcribe_file = _clipped
                    _time_offset = start_seconds
                # 如果裁剪失败·降级从头转(不阻断)
            _model = WhisperModel("base", device="cpu", compute_type="int8")
            _segments, _info = _model.transcribe(_transcribe_file, beam_size=1)
            _texts = []
            for _seg in _segments:
                # 加上偏移量·让时间戳对应原视频的真实时刻(AV 统一证据图)
                _real_start = _seg.start + _time_offset
                _real_end = _seg.end + _time_offset
                if _seg.start > max_seconds:
                    _texts.append(f"...(超过 {max_seconds}s·已截断)")
                    break
                # 时间戳是 AV 统一证据图的地址契约：画面 t=30 与台词 t=30 才能相互验证。
                _texts.append(f"[{_real_start:.2f}-{_real_end:.2f}] {_seg.text.strip()}")
            _txt = "".join(_texts).strip()
            summary = (f"[transcribe·{getattr(_info,'language','?')}] {len(_txt)}字:\n{_txt[:8000]}"
                       if _txt else "[transcribe] 转录成功但无语音内容(纯音乐/静音?)")
            if _txt:  # v98b 真转出内容才算模态证据消费·转录文本是read级(读到实际台词)
                _mark_video_evidence_consumed(s, _txt, level="read", source="transcribe_audio")
        except _sp.TimeoutExpired:
            summary = "transcribe_audio: 下载/抽取超时"
        except Exception as e:
            summary = f"transcribe_audio 异常: {type(e).__name__}: {str(e)[:150]}"
        finally:
            try:
                if _workdir and os.path.isdir(_workdir):
                    import shutil as _sh
                    _sh.rmtree(_workdir, ignore_errors=True)
            except Exception:
                pass
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "transcribe_audio", {"src": path_or_url, "start_seconds": start_seconds}, summary, time.time() - t0)
        return summary

    # (2026-08-27 瘦身) fetch_gaia_file 已删:纯GAIA benchmark工具(从gaia-benchmark/GAIA
    # HF dataset下载题目附件)·真实用户无GAIA task_id附件·测评遗留。

    def extract_structured(source: str, schema_hint: str, max_items: int = 200) -> str:
        """v77e(2026-08-10·顶尖能力融合)把【非结构化一手资料】抽成【可逐项处理的结构化清单】。
        用于:题目要"从原始文档逐条数/逐项查/算百分比"(如 USDA 分级标准清单、论文里的列表、
        网页里散落的条目)——read_table 只能读已是表格的 xlsx/csv,这个能把 HTML/PDF正文/一段
        文本里【散落的实体】用 LLM 抽成 JSON 数组(每行一个实体+字段),之后你就能遍历/计数/算。

        source: URL(http/https·自动下载含PDF)、本地路径(相对→沙箱)、或直接一段文本。
        schema_hint: 说明要抽什么(如 "每个标准项:名称、所属区段(dried/dehydrated/frozen/chilled)")。
        返回抽到的结构化清单(JSON数组·条数+预览),并存入 session 供后续引用。fail-safe。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "extract_structured", content_key=f"{source}|{schema_hint}")
        if not allow:
            _log_tool(qid, "extract_structured", {"src": source[:80]}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import sys as _sys
            _ev = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "v11", "evolution")
            if _ev not in _sys.path:
                _sys.path.insert(0, _ev)
            # 1) 拿到原始文本:URL→下载(含PDF·不开CDP走http)/本地路径→读/直接文本
            src = (source or "").strip()
            raw = ""
            if src.startswith(("http://", "https://")):
                from web_access_bridge import WebAccess
                res = WebAccess().read_url(src)
                raw = (res or {}).get("content", "") if isinstance(res, dict) else ""
            elif len(src) < 400 and os.path.exists(_resolve_path(qid, src)):
                _rp = _resolve_path(qid, src)
                if _path_violation(qid, _rp):
                    raw = ""
                else:
                    raw = open(_rp, encoding="utf-8", errors="ignore").read()
            else:
                raw = src  # 直接当文本内容
            if not raw or len(raw.strip()) < 20:
                summary = "extract_structured: 拿不到有效内容(URL下载失败/文件空/文本太短)"
                _log_tool(qid, "extract_structured", {"src": source[:80]}, summary, time.time() - t0)
                return summary
            # 2) LLM 按 schema_hint 抽成结构化 JSON 数组
            import qa_llm_gateway as _elg
            _prompt = (
                f"从下面的一手资料里·把符合要求的条目【逐个】抽成结构化 JSON 数组"
                f"(每个对象一个条目·字段按需求)。只输出 JSON 数组·不要解释。\n\n"
                f"要抽什么: {schema_hint}\n\n"
                f"资料(截断):\n{raw[:14000]}\n\nJSON 数组:")
            _r = _elg.call(_prompt, timeout=60, max_tokens=4000)
            _out = (_r.get("out") if isinstance(_r, dict) else str(_r)).strip()
            # 3) 解析+存 session
            import json as _json, re as _re
            _m = _re.search(r"\[.*\]", _out, _re.DOTALL)
            _arr = []
            if _m:
                try:
                    _arr = _json.loads(_m.group(0))
                except Exception:
                    _arr = []
            if isinstance(_arr, list) and _arr:
                _arr = _arr[:max_items]
                s.setdefault("structured_extracts", []).append(
                    {"source": src[:120], "schema": schema_hint[:120], "items": _arr})
                _preview = _json.dumps(_arr[:8], ensure_ascii=False)[:1500]
                summary = (f"[extract_structured] 抽出 {len(_arr)} 个结构化条目"
                           f"(已存·可继续遍历/计数/算)。预览前8条:\n{_preview}")
            else:
                summary = (f"[extract_structured] 没抽出结构化条目(资料里可能没有符合"
                           f"schema 的清单·或 LLM 未按格式返回)。原始返回前300字:\n{_out[:300]}")
        except Exception as e:
            summary = f"extract_structured 异常: {type(e).__name__}: {str(e)[:150]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "extract_structured", {"src": source[:80]}, summary, time.time() - t0)
        return summary

    def query_science_db(db: str, query: str) -> str:
        """v96e(2026-08-10 · 补专业库短板)统一封装 GAIA 常考的几个权威科学数据库
        查询接口,别再让 agent 每次现学现凑不同 API 的 URL 格式/参数名(实测 EC
        编号题这类"化学试剂→EC编号"的查询,agent 常常没调对 API 或干脆用
        run_parallel_search 搜网页浪费好几步,还容易搜偏)。

        db 传下面几个之一(大小写不敏感):
          · "pubchem"  —— PubChem 化合物库:query 传化合物名/CAS号,查 CID、
            分子式、分子量、IUPAC名等基础物性。
          · "expasy" 或 "ec" —— ExPASy ENZYME 酶学数据库:query 传【EC编号】
            (如 "1.11.1.7")查该酶的官方名称/别名/催化反应;也可以传【酶名称】
            反查 EC 编号(如 "horseradish peroxidase")。
          · "uniprot" —— UniProt 蛋白数据库:query 传蛋白名/基因名,查蛋白
            条目/功能/物种来源。
          · "kegg" —— KEGG 通路/化合物数据库:query 传化合物名/通路名。
        返回该库查到的结构化信息文本(或查无结果/接口异常的原因)。
        用法建议:先用这个查权威库,查不到再退回 shell_exec/curl 手动拼接
        更细粒度的请求,或 run_parallel_search 找间接资料。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "query_science_db", content_key=f"{db}|{query}")
        if not allow:
            _log_tool(qid, "query_science_db", {"db": db, "query": query}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import urllib.request as _sreq
            import urllib.parse as _sparse
            _db = (db or "").strip().lower()
            _q = (query or "").strip()
            summary = ""
            if not _q:
                summary = "query_science_db: query 不能为空"
            elif _db == "pubchem":
                # PubChem PUG REST:按名称查基础物性(CID/分子式/分子量/IUPAC名)
                # v96f(2026-08-10 修复) · 原请求的 CanonicalSMILES 字段已被 PubChem
                # 弃用(实测该参数会被静默忽略、返回里根本没有这个 key),导致
                # SMILES 始终显示 None。改请求 IsomericSMILES(新替代字段),读取时
                # 再按 SMILES/IsomericSMILES/ConnectivitySMILES 顺序兜底取值。
                url = (
                    "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
                    + _sparse.quote(_q)
                    + "/property/MolecularFormula,MolecularWeight,IUPACName,IsomericSMILES/JSON"
                )
                req = _sreq.Request(url, headers={"User-Agent": "qa-auto-agent-science-db"})
                with _sreq.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                props = (data.get("PropertyTable") or {}).get("Properties") or []
                if props:
                    p = props[0]
                    _smiles = p.get("SMILES") or p.get("IsomericSMILES") or p.get("ConnectivitySMILES")
                    summary = (
                        f"[PubChem] {_q} → CID={p.get('CID')}, "
                        f"分子式={p.get('MolecularFormula')}, "
                        f"分子量={p.get('MolecularWeight')}, "
                        f"IUPAC名={p.get('IUPACName')}, "
                        f"SMILES={_smiles}"
                    )
                else:
                    summary = f"[PubChem] 未查到 '{_q}' 的记录(换个更标准的化学名/CAS号再试)"
            elif _db in ("expasy", "ec"):
                # v96f(2026-08-10 修复) · 原 rest.uniprot.org/enzyme/* 端点实测已 404
                # (UniProt 早已下线该镜像),导致 EC 题双向查询从头到尾就没真正成功过。
                # 改用 ExPASy 官方站点 enzyme.expasy.org 的稳定接口:
                #   · 按 EC 编号正查: https://enzyme.expasy.org/EC/<ec>.txt (flat file)
                #   · 按名称反查:     https://enzyme.expasy.org/cgi-bin/enzyme/enzyme-search-de?<name>
                #     (返回 HTML,用正则把 "<a href="/EC/x.x.x.x">...</a></td><td><pre>DE ..." 摘出来)
                import re as _re
                if "." in _q and _q.replace(".", "").replace("-", "").isdigit():
                    url = f"https://enzyme.expasy.org/EC/{_sparse.quote(_q)}.txt"
                    req = _sreq.Request(url, headers={"User-Agent": "qa-auto-agent-science-db"})
                    with _sreq.urlopen(req, timeout=15) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                    de_lines = _re.findall(r"^DE\s+(.+)$", text, _re.MULTILINE)
                    an_lines = _re.findall(r"^AN\s+(.+)$", text, _re.MULTILINE)
                    ca_lines = _re.findall(r"^CA\s+(.+)$", text, _re.MULTILINE)
                    if de_lines:
                        name = " ".join(de_lines)
                        summary = f"[ExPASy ENZYME] EC {_q} → 官方名称: {name}"
                        if an_lines:
                            summary += f"(别名: {'; '.join(an_lines)})"
                        if ca_lines:
                            summary += f"\n催化反应: {' '.join(ca_lines)}"
                    else:
                        summary = f"[ExPASy ENZYME] 未查到 EC {_q} 的记录(该编号可能已废弃/迁移,或格式有误)"
                else:
                    # 按酶名称反查 EC 编号:ExPASy 官方按 DE(描述名)全文检索
                    url = ("https://enzyme.expasy.org/cgi-bin/enzyme/enzyme-search-de?"
                           + _sparse.quote(_q))
                    req = _sreq.Request(url, headers={"User-Agent": "qa-auto-agent-science-db"})
                    with _sreq.urlopen(req, timeout=15) as resp:
                        html = resp.read().decode("utf-8", errors="replace")
                    # 每条结果形如: <a href= "/EC/1.11.1.7">1.11.1.7</a></td><td><pre>DE   peroxidase.
                    hits = _re.findall(
                        r'href=\s*"/EC/([0-9.\-]+)"[^<]*</a>.*?<pre>\s*DE\s+([^\n<]+)',
                        html, _re.DOTALL)
                    if hits:
                        lines = [f"[ExPASy ENZYME] 按名称 '{_q}' 搜到 {len(hits)} 条:"]
                        for ec_no, name in hits[:8]:
                            lines.append(f"  · EC {ec_no} = {name.strip()}")
                        summary = "\n".join(lines)
                    else:
                        # 官方全文检索没搜到,退化到 UniProtKB 按蛋白名搜索并取其 ecNumbers 字段交叉验证
                        url2 = ("https://rest.uniprot.org/uniprotkb/search?query="
                                + _sparse.quote(_q) + "+AND+reviewed:true"
                                "&format=json&size=5&fields=accession,protein_name,ec")
                        req2 = _sreq.Request(url2, headers={"User-Agent": "qa-auto-agent-science-db"})
                        with _sreq.urlopen(req2, timeout=15) as resp2:
                            data2 = json.loads(resp2.read().decode("utf-8", errors="replace"))
                        results2 = data2.get("results") or []
                        ec_lines = []
                        for r2 in results2:
                            ecs = ((r2.get("proteinDescription") or {}).get("recommendedName") or {}).get(
                                "ecNumbers") or []
                            if ecs:
                                pname = ((r2.get("proteinDescription") or {}).get("recommendedName") or {}).get(
                                    "fullName", {}).get("value", "?")
                                ec_lines.append(f"  · {pname}: EC " + ", ".join(e.get("value", "?") for e in ecs))
                        if ec_lines:
                            summary = (f"[UniProtKB 交叉验证] 按名称 '{_q}' 搜到:\n" + "\n".join(ec_lines))
                        else:
                            summary = f"[ExPASy ENZYME] 未搜到与 '{_q}' 相关的酶记录(换更标准的英文酶名再试)"
            elif _db == "uniprot":
                url = ("https://rest.uniprot.org/uniprotkb/search?query="
                       + _sparse.quote(_q) + "&format=json&size=5&fields=accession,protein_name,organism_name,gene_names")
                req = _sreq.Request(url, headers={"User-Agent": "qa-auto-agent-science-db"})
                with _sreq.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                results = data.get("results") or []
                if results:
                    lines = [f"[UniProt] '{_q}' 搜到 {len(results)} 条:"]
                    for r_ in results[:5]:
                        acc = r_.get("primaryAccession", "?")
                        pname = ((r_.get("proteinDescription") or {}).get("recommendedName") or {}).get(
                            "fullName", {}).get("value", "?")
                        org = (r_.get("organism") or {}).get("scientificName", "?")
                        lines.append(f"  · {acc}: {pname} ({org})")
                    summary = "\n".join(lines)
                else:
                    summary = f"[UniProt] 未搜到与 '{_q}' 相关的蛋白记录"
            elif _db == "kegg":
                # v96f(2026-08-10 补全) · 原版只查 find/compound,遇到"通路名"
                # (如 "glycolysis")会查空——KEGG compound 和 pathway 是两个独立
                # 端点,分不清用户传的是化合物名还是通路名时,两个都查、汇总返回。
                _kegg_parts = []
                for _kind, _label in (("compound", "化合物"), ("pathway", "通路")):
                    try:
                        _url_k = f"https://rest.kegg.jp/find/{_kind}/" + _sparse.quote(_q)
                        _req_k = _sreq.Request(_url_k, headers={"User-Agent": "qa-auto-agent-science-db"})
                        with _sreq.urlopen(_req_k, timeout=15) as _resp_k:
                            _text_k = _resp_k.read().decode("utf-8", errors="replace")
                        if _text_k.strip():
                            _kegg_parts.append(f"[KEGG-{_label}] '{_q}' 匹配:\n{_text_k[:800]}")
                    except Exception:
                        pass
                summary = "\n\n".join(_kegg_parts) if _kegg_parts else f"[KEGG] 未查到 '{_q}' 的化合物/通路记录"
            else:
                summary = (f"query_science_db: 不支持的 db='{db}'。"
                           "支持: pubchem / expasy(或ec) / uniprot / kegg")
        except Exception as e:
            summary = (f"query_science_db 异常: {type(e).__name__}: {str(e)[:200]}。"
                       "可能是网络问题或该库没有这条记录,可换 shell_exec/curl 直接调 API"
                       "或用 run_parallel_search 找间接资料。")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "query_science_db", {"db": db, "query": query}, summary[:1500], time.time() - t0)
        return summary

    shell_exec.__name__ = "shell_exec"
    shell_exec.__doc__ = ("在沙箱工作目录执行 shell 命令，拿到 stdout/stderr/exit_code。"
                          "写代码、跑测试、查环境、装依赖都用这个。")
    file_read.__name__ = "file_read"
    file_read.__doc__ = "读取文件内容(相对路径解析到本任务沙箱目录)。"
    file_write.__name__ = "file_write"
    file_write.__doc__ = "写入文件(相对路径写入本任务沙箱目录)，mode=overwrite/append。"
    def ask_expert_model(model: str, question: str) -> str:
        """换一个【不同家的专家模型】独立解答/复核这道题(模型即工具·用户架构)。
        用于: ①算了答案不确定→让另一个模型独立复算交叉验证 ②本模型卡住→换个
        模型换个思路。可选 model: gpt-5(强推理/数学)、gemini-2.5-pro(强多模态/长文)、
        claude-opus(强代码/严谨)。把完整题目+已知信息传给它·拿它的独立答案。
        ⚠️【重要·别只传题目干问】如果这题涉及冷门具体事实(某篇论文的具体公式/某个
        网页的具体数字)，必须把你已经抓到的【证据原文片段】原样贴进 question 里再问，
        不能只写"这道题答案是什么"就丢给专家模型——专业/经典文献很可能就在它的训练
        语料里，它会看似自信地"凭记忆"答出来，你会误以为这是"独立验证"，但其实
        跟你自己没查证据、直接靠记忆编答案是同一类风险，没有交叉验证的价值。正确
        用法是把"证据+你的推导过程"都写进去，问它"帮我核对这个算法/结论对不对"，
        而不是空手问"是多少"。
        用法: ask_expert_model("gpt-5", "题目：...\\n已查到的原文证据：<粘贴抓到的关键段落原文>\\n我的推导：...\\n请核对/独立算出X")"""
        t0 = time.time()
        s = _sess(qid)
        # 单模型评测必须将所有子调用锁到同一网关模型，不能由 agent 切到其它专家模型。
        _single_model = os.environ.get("MEYO_QA_SINGLE_MODEL", "").strip()
        if _single_model:
            model = _single_model
        else:
            # ★换高的专家模型(用户2026-08-20:gpt-5不好使ReadTimeout 182s)。默认优先
            #   claude-opus(唯一有降级池·撞400自动换opus-4.7/sonnet重试
            #   最稳)·opus家族不可用降gemini-2.5-pro·gpt-5排最后(它openai家族range(2)
            #   retry易90→180s超时)。agent选了gpt-5/空/gpt类→改走opus。env
            #   MEYO_EXPERT_MODEL_PREF 可覆盖(逗号分隔优先序)。
            _pref = os.environ.get("MEYO_EXPERT_MODEL_PREF",
                                   "claude-opus,gemini-2.5-pro,gpt-5").split(",")
            _pref = [x.strip() for x in _pref if x.strip()]
            _ml = str(model or "").lower()
            # agent 没明确指定 或 指定了 gpt 类(不好使)→用偏好首选。明确指定 claude/
            #   gemini 的尊重其选择(它可能有意换家交叉验证)。
            if not _ml or "gpt" in _ml or _ml in ("auto", "default", "expert"):
                model = _pref[0] if _pref else "claude-opus"
        allow, guard = _check_budget(s, "ask_expert_model", content_key=model + question[:50])
        if not allow:
            _log_tool(qid, "ask_expert_model", {"model": model}, guard, time.time() - t0)
            return guard
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import multi_model as _mm
            # ★细缝3修(真e2e Russell题实证:gpt-5 ReadTimeout 182s拖垮全局·因openai家族
            #   range(2)同族retry 90s→180s)。用ThreadPool硬超时包裹:主线程到点即返回·
            #   不等底层retry链。墙钟紧张(剩<180s)时更短超时+提示换本地推理。fail-safe。
            _mm_to = 80
            try:
                _budg = (s or {}).get("_budget") or {}
                _wall_left = float(_budg.get("wall_deadline", 0) or 0) - time.time()
                if 0 < _wall_left < 200:
                    _mm_to = max(30, int(_wall_left * 0.4))   # 墙钟紧·别把余量全押专家模型
            except Exception:
                _mm_to = 80
            from concurrent.futures import ThreadPoolExecutor as _TPE
            from concurrent.futures import TimeoutError as _FTO
            _ex = _TPE(max_workers=1)
            _fut = _ex.submit(_mm.ask, model, question, timeout=_mm_to, max_tokens=1500)
            try:
                r = _fut.result(timeout=_mm_to + 5)
                _ex.shutdown(wait=False)
            except _FTO:
                try:
                    _fut.cancel(); _ex.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                r = {"ok": False, "out": "",
                     "err": "ask_expert_model 硬超时 %ds(网关慢·别再等专家模型·用已有证据自己收敛)" % _mm_to}
            # ★首选失败→自动降级偏好序里下一个(opus挂→gemini·双保险·非单模型锁定时)。
            #   每级都硬超时包裹·总耗时受控。fail-safe。
            if (not r.get("ok")) and not _single_model:
                try:
                    _pref2 = os.environ.get("MEYO_EXPERT_MODEL_PREF",
                                            "claude-opus,gemini-2.5-pro,gpt-5").split(",")
                    _pref2 = [x.strip() for x in _pref2 if x.strip() and x.strip() != model]
                    for _fbm in _pref2[:1]:   # 只降 1 级(省墙钟)
                        _ex2 = _TPE(max_workers=1)
                        _fut2 = _ex2.submit(_mm.ask, _fbm, question,
                                            timeout=_mm_to, max_tokens=1500)
                        try:
                            _r2 = _fut2.result(timeout=_mm_to + 5)
                            _ex2.shutdown(wait=False)
                        except _FTO:
                            try:
                                _fut2.cancel(); _ex2.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                            _r2 = {"ok": False}
                        if _r2.get("ok"):
                            r = _r2
                            model = _fbm   # 记实际用的降级模型
                            break
                except Exception:
                    pass
            if r["ok"]:
                summary = f"[{model}] " + r["out"]
            else:
                # (2026-08-15·C) 额度/限流错单独提醒:告诉 agent 这不是普通失败·是该换模型·并列可用的
                if _mm.is_quota_error(r.get("err", "")):
                    _st = _mm.quota_status(probe=False)
                    _alt = [x for x in (_st.get("usable") or []) if x != model]
                    summary = (f"[{model}·⚠️额度/限流不够] {str(r['err'])[:100]}\n"
                               f"→ 别再用 {model}·换这些可用模型重试: {', '.join(_alt[:5]) or '(暂无·可 check_model_quota 探测)'}")
                    _emit_gate_event(qid, "model_quota", "exhausted", {"model": model})
                else:
                    summary = f"[{model}] 调用失败: {r['err']}"
        except Exception as e:
            summary = f"ask_expert_model 异常: {type(e).__name__}: {str(e)[:120]}"
        # v96j(2026-08-11 用户: "凭记忆不对吧") · 可审计性修复:
        # 之前 _log_tool 通用截断(80字符)+这里手动截断(60字符)双重砍断，导致事后完全无法
        # 判断"专家模型是读了我传的证据回答，还是凭自己训练记忆认出这题/这篇论文回答的"——
        # 这正是用户点破的漏洞:米氏方程题GPT-5说出论文作者/年份细节，但那篇论文是经典高引用
        # 论文，GPT-5训练语料里很可能本来就"记得"，不能只凭它答对+语气像在核对，就当成
        # "基于证据的独立验证"。现在把完整 question 落盘到 full_prompt 字段(不参与
        # str(v)[:80]通用截断，因为 _log_tool 只截断顶层 args 里的值，这里改成单独存)，
        # 供事后审计"证据是否真的传进去了"；同时在 summary 前缀标注证据是否命中，
        # 便于以后 review 不用逐条翻 full_prompt。
        _evidence_passed = len(question) > 500 or any(
            marker in question for marker in
            ("fetch_url_smart", "关键结论段", "evidence", "证据", "已抓到", "原文", "抓到"))
        _audit_tag = "[已附证据文本]" if _evidence_passed else "[⚠️疑似仅传题目·未附证据原文·答案可能凭模型自身记忆]"
        _log_tool(qid, "ask_expert_model",
                  {"model": model, "q": question[:60], "q_len": len(question),
                   "full_prompt": question}, f"{_audit_tag} {summary}", time.time() - t0)
        return summary

    def see_image_expert(model: str, path_or_url: str, question: str) -> str:
        """用【指定的多模态专家模型】看图(claude VLM 看不准时换其他模型看同一张图)。
        model: 可用视觉模型之一——优先选和 see_image_vlm 【不同家】的模型(异构才能互相纠错)。
        推荐优先级(vision路由·经验库动态调整): gemini-2.5-pro > gpt-5 > claude-opus > gpt-4o-mini。
        如果首选模型超时/失败·系统会自动轮转到下一个可用视觉模型(无需你重试)·
        所以只传一个首选模型名即可。模型=工具·失败自动换下一个。
        path_or_url: 本地图路径或图片URL。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard = _check_budget(s, "see_image_expert", content_key=model + path_or_url)
        if not allow:
            _log_tool(qid, "see_image_expert", {"model": model}, guard, time.time() - t0)
            return guard
        try:
            import base64 as _b64
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import multi_model as _mm
            src = (path_or_url or "").strip()
            if src.startswith(("http://", "https://")):
                import requests as _rq
                img = _rq.get(src, timeout=30).content
            else:
                real = _resolve_path(qid, src)
                if not os.path.exists(real):
                    r = f"see_image_expert: 文件不存在 {real}"
                    _log_tool(qid, "see_image_expert", {"model": model, "src": src}, r, time.time() - t0)
                    return r
                img = open(real, "rb").read()
            mt = "image/png" if src.lower().endswith(".png") else "image/jpeg"
            b64 = _b64.b64encode(img).decode()
            # v98u/v99b(2026-08-12/v99b升级) · 动态多模型轮转·彻底去硬编码。
            #   v98u病根: fallback 写死 "gemini-2.5-pro"——gpt-5超时→gemini·gemini挂了就彻底失败。
            #   v99b修法: 用 model_orchestrator.usable_models(vision_only=True) 拿全部可用视觉
            #   模型列表(探测缓存+用户配置·新加模型自动发现)·失败就轮转到下一个·直到全部失败。
            #   timeout策略: 首次40s·每次fallback递减5s但不低于20s(给后续计算步留预算)。
            #   模型=工具·agent自主调整组合·不依赖任何硬编码模型名。
            try:
                import model_orchestrator as _mo
                _vision_pool = [m for m in _mo._pick("vision", vision_only=True) if m != model]
            except Exception:
                _vision_pool = ["gemini-2.5-pro", "claude-opus", "gpt-4o-mini"]
                if model in _vision_pool:
                    _vision_pool.remove(model)
            _tried = [model]
            _cur_timeout = 40
            # v99b(2026-08-12)混排worksheet结构化共识:硬前置JSON-only(同see_image_vlm)
            try:
                import vision_consensus as _vc
                _lowq = (question or "").lower()
                if any(k in _lowq for k in ("worksheet", "quiz", "problem", "fraction",
                                            "answer", "作答", "答案", "题", "分数")):
                    question = _vc.SCHEMA_PROMPT_STRICT + question
            except Exception:
                pass
            res = _mm.ask(model, question, image_b64=b64, media_type=mt, timeout=_cur_timeout, max_tokens=1200)
            if not res.get("ok"):
                _first_err = str(res.get("err", ""))
                _is_any_fail = any(k in _first_err.lower() for k in (
                    "timeout", "readtimeout", "超时", "timed out", "http 5", "connect", "error"))
                _fallback_tried = []
                _fallback_summary = None
                for _fb_model in _vision_pool:
                    if _fb_model in _tried:
                        continue
                    _tried.append(_fb_model)
                    _fallback_tried.append(_fb_model)
                    # M-a 校准债(v99c): 递减 5s/最低 20s 是拍出来的·正确来源应从实测 p95
                    # 推导(各模型 timeout = p95 * 1.2)。价格表(M-a)上线后自动替换这里的常量。
                    _cur_timeout = max(20, _cur_timeout - 5)
                    _res_fb = _mm.ask(_fb_model, question, image_b64=b64, media_type=mt,
                                      timeout=_cur_timeout, max_tokens=1200)
                    if _res_fb.get("ok"):
                        _fallback_summary = (
                            f"[{_fb_model} 看图·fallback from {model}失败] " + _res_fb["out"])
                        break
                    # 非超时错误(HTTP 4xx·模型不存在)·不值得继续轮转·直接退出
                    _fb_err = str(_res_fb.get("err", ""))
                    if any(k in _fb_err.lower() for k in ("404", "401", "403", "not found", "invalid")):
                        break
                if _fallback_summary:
                    summary = _fallback_summary
                    # 验收1(v99c): 成功的 fallback 模型写回经验库·败者也记·bandit 闭环
                    # v99d: 同时写 elapsed_s(从 _res_fb.elapsed 取)·latency 惩罚排序依赖
                    try:
                        import model_orchestrator as _mo2
                        _winning_fb = [m for m in _tried[1:] if m in _fallback_summary]
                        _winning_fb = _winning_fb[0] if _winning_fb else _tried[-1]
                        _mo2.record_experience("vision", _winning_fb, correct=True,
                                               qid=str(qid), ts=time.time(),
                                               elapsed_s=_res_fb.get("elapsed"))
                        for _fail_m in _tried[:-1]:
                            _mo2.record_experience("vision", _fail_m, correct=False,
                                                   qid=str(qid), ts=time.time())
                    except Exception:
                        pass
                else:
                    _all_tried = ", ".join(_tried)
                    summary = (f"[see_image_expert 全部模型失败({_all_tried})] "
                               f"首次错误: {_first_err[:80]}")
                    # 全部失败也记账·降低这批模型在 vision 路由的优先级
                    try:
                        import model_orchestrator as _mo2
                        for _fail_m in _tried:
                            _mo2.record_experience("vision", _fail_m, correct=False,
                                                   qid=str(qid), ts=time.time())
                    except Exception:
                        pass
            else:
                summary = f"[{model} 看图] " + res["out"]
                # 首选模型直接成功 → 正向强化(带 elapsed_s)
                try:
                    import model_orchestrator as _mo2
                    _mo2.record_experience("vision", model, correct=True,
                                           qid=str(qid), ts=time.time(),
                                           elapsed_s=res.get("elapsed"))
                except Exception:
                    pass
        except Exception as e:
            summary = f"see_image_expert 异常: {type(e).__name__}: {str(e)[:120]}"
        # G3(2026-08-12): 本次读取作为独立候选进投票池(不 blend·供 finalize 截断取最强)
        _record_observation_anchor(_sess(qid), "see_image_expert", path_or_url, summary)
        _record_perception_vote(_sess(qid), "see_image_expert", path_or_url, summary)
        _log_tool(qid, "see_image_expert", {"model": model, "src": path_or_url}, summary, time.time() - t0)
        return summary

    # (2026-08-27 瘦身) solve_chess_move 已删:GAIA象棋题专用(棋盘图→stockfish算招)·真实调用0次·benchmark题型工具。

    def see_video_frame(url: str, at_time: str = "0:00", question: str = "What is shown in this frame?") -> str:
        """看视频指定时刻的画面(问'某YouTube视频X分钟处显示什么'用这个·转音轨没用时)。
        yt-dlp下片段→ffmpeg抽该时刻帧→VLM看画面。at_time='2:00'/'1:41'/秒数。
        下不动(网络/反爬)会明确回传原因·引导换渠道找二手资料。
        ★M-83显式标签优先(反Goodhart·治"看整体猜错"):要【识别画面里是什么】(赛道/地点/
        人物/型号)时·别只抽那一刻让VLM猜整体印象(易撞更著名的相似项·如Yoshi Circuit被猜成
        Mario Circuit)。优先抽【最可能出现显式文字标签的时刻】——片头/标题卡/记分板/字幕条·
        让VLM【读屏幕上的字】(带全名/前缀)而非猜。question里写清'读出画面里显示的文字标签/
        名称'。屏幕显式文字 > 视觉整体印象。多抽几个时刻交叉更稳。
        ★★关键(self_evolve conf0.72):题目指定的时刻(如'2分钟处')常是【内容进行中·无文字标签】
        的画面·VLM只能看画面猜。而显式名称文字(赛道名/标题)出现在【结构性锚点】:赛段开始/发车
        瞬间/片头/场景切换(通常在片段前段·如0:20-0:45或每小节开头)。识别名称类问题·**别只抽
        题目指定时刻**·先抽结构锚点时刻读出全名文字(如赛道题抽发车标题卡读'GCN Yoshi Circuit')·
        再用指定时刻帧做画面一致性验证。指定时刻答'看起来像X'=推断·锚点帧读到'文字写着X'=确证。"""
        t0 = time.time()
        # v97j(2026-08-11) 修真凶bug:see_video_frame闭包漏了 s = _sess(qid)·一调就
        #   `name 's' is not defined`崩→兜底self_healed任务早退。这才是所有视频题
        #   see_video_frame=0的真根因——不是agent不用·是工具一调就崩(视频题才触发·
        #   所以简单题/视觉题复现不出这个error)。视频约束/硬门控逼agent用它也白搭·
        #   一用就异常。补上s定义。
        s = _sess(qid)
        allow, guard = _check_budget(s, "see_video_frame", content_key=url + at_time)
        if not allow:
            _log_tool(qid, "see_video_frame", {"url": url, "at": at_time}, guard, time.time() - t0)
            return guard
        try:
            import video_frame as _vf
            res = _vf.see_video_frame(url, at_time=at_time, question=question)
            if res.get("ok"):
                # v99a · M-83仪表联动: video_frame模块已在返回值里判好 evidence_type·
                #   直接读·不在包装层重复做同一套信号匹配(单一来源原则)。
                _lvl = res.get("evidence_type", "inferred")  # 新字段·旧版兼容默认inferred
                _label_found = res.get("explicit_label_found", False)
                _hint_from_tool = res.get("hint", "")
                # 把仪表信息嵌进 summary·agent 当场看到·不靠记 docstring
                summary = f"[视频帧 {at_time}][{_lvl.upper()}] {res['answer']}"
                if _hint_from_tool:
                    summary += f"\n⚠️ {_hint_from_tool}"
                # v98b 门控校验证据:真成功抽到帧才置"模态证据已消费"(不是调过就算)
                _mark_video_evidence_consumed(s, res["answer"], level=_lvl, source="see_video_frame")
                # v98c M-82:真读到显式文字(read级)才=查询键已验证·门放行;inferred不放行(逼验证)
                # 固化观测锚点：后续可以反驳，但不能静默遗忘已经看到的候选事实。
                s.setdefault("evidence_anchors", []).append({
                    "kind": "video_frame", "level": _lvl, "claim": str(res["answer"])[:1200],
                    "source": str(res.get("video_url") or url)[:300], "at_time": at_time,
                })
                del s["evidence_anchors"][:-24]
                if _lvl == "read":
                    _bg = s.get("_budget")
                    if _bg is not None:
                        _bg["_key_verified"] = True
                # v99a · M-84联动: 同一个inferred键累计K次重采样仍未read→主动引导label_sweep
                #   （文本里明确要求："停止重抽同一时刻·换证据来源"）
                _pk = s.get("_perceived_keys") or []
                _inferred_streak = sum(1 for k in _pk if k.get("level") == "inferred")
                if _lvl == "inferred" and _inferred_streak >= 3 and not s.get("_label_sweep_suggested"):
                    s["_label_sweep_suggested"] = True
                    summary += (f"\n⛔ [M-84联动] 你已在无标签画面上连续推断{_inferred_streak}次·"
                                f"停止重抽同一时刻。换证据来源: "
                                f"调 label_sweep(url='{url[:60]}') 扫全片定位含文字时刻·"
                                f"再用 see_video_frame 精读那些时刻。")
                # v98h 机制主动补抽标题卡帧(self_evolve conf0.72·软引导agent不改抽帧时刻→机制主动做):
                #   识别名称类问题(问what/which/名称/赛道)+指定时刻得inferred(看画面猜·没读到文字)+
                #   还没补抽过 → 自动补抽片段前段(标题卡/发车常在开头)读文字。不代劳解题·是补一帧
                #   让agent拿到"读到的文字"而非只有"猜的"。风险防护:只识别类问题触发·补抽1次。
                _q_l = (question or "").lower()
                _is_naming = any(k in _q_l for k in ("what track", "which track", "what racetrack",
                    "which racetrack", "what is the name", "which.*shown", "what.*shown", "identify",
                    "哪条", "哪个", "什么赛道", "名称", "叫什么", "which stage", "what level", "what map"))
                if _lvl == "inferred" and _is_naming and not s.get("_titlecard_probed"):
                    s["_titlecard_probed"] = True
                    try:
                        # 补抽片段开头读显式文字(魔法时间戳"0:30/0:20"仅作默认探针·
                        # label_sweep 才是通用定位器·这里是快速兜底)
                        for _probe_at in ("0:30", "0:20"):
                            _r2 = _vf.see_video_frame(url, at_time=_probe_at,
                                question="Read the EXACT on-screen text labels/title shown in this frame "
                                         "(track name, stage title, any displayed text). Quote the literal text.")
                            _a2 = str(_r2.get("answer") or "")
                            _lvl2 = _r2.get("evidence_type", "inferred")
                            if _r2.get("ok") and _a2.strip():
                                summary += (f"\n[机制补抽标题卡·{_probe_at}][{_lvl2.upper()}·读显式文字] {_a2[:600]}")
                                s.setdefault("_perceived_keys", []).append(
                                    {"level": _lvl2, "value": _a2[:200]})
                                s.setdefault("evidence_anchors", []).append({
                                    "kind": "video_frame", "level": _lvl2, "claim": _a2[:1200],
                                    "source": str(_r2.get("video_url") or url)[:300], "at_time": _probe_at,
                                })
                                del s["evidence_anchors"][:-24]
                                if _lvl2 == "read":
                                    _bg2 = s.get("_budget")
                                    if _bg2 is not None:
                                        _bg2["_key_verified"] = True
                                break
                    except Exception:
                        pass
            else:
                # 失败也是可解释的工具结果。此前漏掉此分支导致 _log_tool 前引用
                # 未初始化的 summary，进而把视频题错误标记为 interrupted。
                summary = f"[视频帧失败] {res.get('reason', '未返回可用画面')}"
        except Exception as e:
            summary = f"see_video_frame 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "see_video_frame", {"url": url, "at": at_time}, summary, time.time() - t0)
        return summary

    def label_sweep(url: str, max_sec: int = 120) -> str:
        """【视频标签时刻扫描】稀疏扫视频·用OCR文字密度定位含显式文字标签的时刻·
        返回含显著文字的时间戳列表+文字预览。零领域知识·赛车标题卡/片头/记分牌/字幕全通用。
        先用此工具定位·再用 see_video_frame(url, at_time=...) 精读文字帧——
        比盲猜一个时刻抽帧再让VLM猜整体印象精准得多(inferred→read级证据)。
        url: 视频URL或搜索描述。max_sec: 扫前多少秒(默认120)。
        返回: 含文字的时刻列表·按文字量排序·hits=[]说明整视频无明显文字(纯画面)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard = _check_budget(s, "label_sweep", content_key=url)
        if not allow:
            _log_tool(qid, "label_sweep", {"url": url}, guard, time.time() - t0)
            return guard
        try:
            import video_frame as _vf
            res = _vf.label_sweep(url, max_sec=max_sec)
            if res.get("ok"):
                hits = res.get("hits") or []
                if hits:
                    _lines = [f"  {h['ts']}({h['time_sec']}s): {h['text_preview'][:60]!r} [{h['char_count']}字符]"
                              for h in hits]
                    summary = (f"[label_sweep] 找到{len(hits)}个含文字时刻(按文字量排序)·"
                               f"用 see_video_frame 精读这些时刻:\n" + "\n".join(_lines))
                else:
                    summary = (f"[label_sweep] 未发现明显文字帧(前{max_sec}s/{res['total_scanned']}帧OCR均空)·"
                               f"视频可能全程无文字标签。换证据: 查视频标题/简介/评论区文本。"
                               f"\n({res.get('reason','')})")
            else:
                summary = f"[label_sweep失败] {res.get('reason','')}"
        except Exception as e:
            summary = f"label_sweep 异常: {type(e).__name__}: {str(e)[:120]}"
        # (2026-08-27 瘦身) 原 _inferred_entity_gate_passed 标记设置已删:inferred实体确认门已删·
        #   此标记无消费者成死代码。label_sweep 工具本身保留(通用视频扫描能力)。
        _log_tool(qid, "label_sweep", {"url": url}, summary, time.time() - t0)
        return summary

    def see_image_zoom(path_or_url: str, question: str, region: str = "", zoom: int = 3) -> str:
        """【裁剪放大精读图】读小字/细节·see_image看整图读不清时用(agent自诊断提的:VLM读细节
        精度不足→裁剪目标区域+放大再读)。region='left,top,right,bottom'比例0-1(如'0,0.6,1,1'
        =下40%·墓碑背景碑在哪块就框哪块)·留空=整图放大。zoom放大倍数(小字建议3-4)。
        读墓碑小字韵文/图里小数字边长/画作细节等·比重复see_image整图有效。"""
        t0 = time.time()
        s = _sess(qid)  # 2026-08-12 修:漏绑 s→NameError 崩(实证:分数题agent调本工具即崩·记gave_up)
        allow, guard = _check_budget(s, "see_image_zoom", content_key=path_or_url + region)
        if not allow:
            _log_tool(qid, "see_image_zoom", {"src": path_or_url}, guard, time.time() - t0)
            return guard
        try:
            import see_image_zoom as _siz
            src = path_or_url.strip()
            if src.startswith(("http://", "https://")):
                import requests as _rq, base64 as _b64
                img = _rq.get(src, timeout=30).content
                real = f"/tmp/meyo_zoom_{abs(hash(src)) % 10**8}.png"
                open(real, "wb").write(img)
            else:
                real = _resolve_path(qid, src)
            _reg = None
            if region:
                try:
                    _reg = tuple(float(x) for x in region.split(","))
                except Exception:
                    _reg = None
            r = _siz.crop_zoom_read(real, question, region=_reg, zoom=int(zoom))
            summary = (f"[裁剪放大读] {r['answer']}" if r.get("ok")
                       else f"[裁剪放大失败] {r.get('reason','')}")
        except Exception as e:
            summary = f"see_image_zoom 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "see_image_zoom", {"src": path_or_url, "region": region}, summary[:1200], time.time() - t0)
        return summary

    def set_reflect_threshold(n: int, reason: str = "") -> str:
        """【自己调你的绕圈反思阈值】(用户:反思闸的数字该agent自己按每道题改·不是写死给你)。
        n=你判断【这道题】搜文字/抓网页类工具搜几次还没进展就该停下来反思换法(2-15)。
        怎么定:①简单事实题(一搜就有的)设小(3-4)·搜几次没有说明方向错了该早反思;
        ②复杂多跳题(要串多个来源/专业库)设大(8-10)·容许你多搜几次别过早被打断;
        ③要看画面/听音频/精确计算的题设小·因为它本质不该靠搜文字。
        开题时根据题目性质先设一次·中途发现估错可再调。这是你自主控制解题节奏的手段。"""
        t0 = time.time()
        s = _sess(qid)
        try:
            _n = max(2, min(15, int(n)))
            s["_wander_thresh"] = _n
            summary = (f"[反思阈值已设为{_n}] 你判断这题搜文字类工具累计{_n}次还没实质进展就会"
                       f"触发反思脑帮你换策略。理由: {reason[:100] or '(未说明)'}")
        except Exception as e:
            summary = f"set_reflect_threshold 异常: {type(e).__name__}: {str(e)[:100]}"
        _log_tool(qid, "set_reflect_threshold", {"n": n, "reason": reason}, summary, time.time() - t0)
        return summary

    def request_more_budget(reason: str) -> str:
        """【自己给自己加预算/加时】(层2·v98x·你自主控制解题节奏的手段)。
        当你判断【正在多步计算/多跳链的中间·方向对·还差几步就出结果·但快撞墙钟/步数上限】时·
        调这个给自己加一段墙钟+步数继续(像人发现'再给我5分钟就能算完')。reason 必须写清:
        你现在到哪一步了、还差什么、为什么再给点预算就能完成(空理由会被拒)。
        【重要·不是万能续命】:只有你【最近确有实质进展】(抓到新证据/算出中间结果/刚跑了
        计算脚本)时才批准;如果你只是在原地打转/反复拿不到(无进展)·加预算也没用·会被拒·
        那时该换策略(换工具/来源/问 ask_expert_model)或 document_wall 诚实声明·而不是续命。
        每题最多加有限次·墙钟有绝对上限。"""
        # 注:上面必须是【裸字符串字面量】才会被 Python 赋给 __doc__。
        # 2026-08-12 事故:原来写成 \"\"\"...\"\"\" % BUDGET_EXTEND_MAX,带了 % 运算就不再是
        #   字面量 → __doc__=None → deepagents 编译子agent工具时 ValueError(缺docstring)
        #   → create_deep_agent 启动即崩 → 每题 final 空 → 记 gave_up(伪装成"解题失败/能力边界")。
        #   具体次数上限见 BUDGET_EXTEND_MAX 常量,不放进 docstring 以保字面量。
        t0 = time.time()
        s = _sess(qid)
        b = s.get("_budget") or {}
        try:
            _reason = (reason or "").strip()
            _n_ext = int(b.get("_budget_extends", 0) or 0)
            # v119 FixE(2026-08-17 · bec745 实证): 内外层墙钟互不感知——内层把 hard_wall
            #   续到 ~2000s·外层 subprocess 1320s 先杀·续命全白续且死得无归因。
            #   修法: 外层若传了 MEYO_OUTER_DEADLINE(绝对unix秒)·换算成 budget.t_start
            #   相对秒的封顶值——续完的墙必须留在外层 kill 之前·给收尾留活口。无外层则不封。
            #   注意: 本函数在 _make_execution_tools 工厂作用域·没有外层 run 的 t_start·
            #   elapsed 一律用 budget["t_start"](与 _check_budget 同一基准·resume 也一致)。
            _outer_dl = float(os.environ.get("MEYO_OUTER_DEADLINE", "0") or 0)
            _outer_cap_rel = None
            if _outer_dl > time.time():
                _elapsed_b = time.time() - float(b.get("t_start") or time.time())
                _outer_cap_rel = _elapsed_b + max(
                    0.0, _outer_dl - time.time() - _OUTER_FINISH_MARGIN_S)
            _outer_blocked = (_outer_cap_rel is not None
                              and _outer_cap_rel <= float(b.get("hard_wall_s") or 0) + 1.0)
            # 拒:理由太空
            if len(_reason) < 8:
                summary = ("[加预算·拒绝] 你没说清为什么需要加预算(现在到哪步/还差什么/"
                           "为什么再给点就能完成)。写清理由再调——加预算不是无条件续命。")
                _emit_gate_event(qid, "budget_extend", "reject",
                                 {"why": "empty_reason", "n_ext": _n_ext})
            # 拒:超上限
            elif _n_ext >= _budget_extend_max(s):
                summary = (f"[加预算·拒绝] 本题已自主加预算 {_n_ext} 次(上限 {_budget_extend_max(s)})。"
                           f"再加也到顶了——现在【必须收尾】:基于已有证据/中间结果 finalize_answer"
                           f"(可标置信度)·或 document_wall 诚实声明此路不通。别再续命空转。")
                _emit_gate_event(qid, "budget_extend", "reject",
                                 {"why": "over_max", "n_ext": _n_ext})
            # 拒(v119 FixE): 外层墙已在眼前·续不出新空间——再批只会被外层强杀。
            #   不消耗续命次数(这不是 agent 的错·是外层环境的硬约束)。
            elif _outer_blocked:
                summary = ("[加预算·拒绝·外层墙] 本题运行在外层监督的超时墙之下——"
                           "外层墙已在眼前(续命空间≈0)·再批时间也只会被外层强杀、"
                           "颗粒无收。现在【必须立即收尾】: 基于已有证据/中间结果 "
                           "finalize_answer(标置信度)·或 document_wall 诚实声明。")
                _emit_gate_event(qid, "budget_extend", "reject",
                                 {"why": "outer_wall", "n_ext": _n_ext,
                                  "outer_cap_rel": round(_outer_cap_rel or 0)})
            else:
                # 真实进展信号:近期证据/正文涨了·或最近一步是刚跑完的计算(shell_exec 有实质输出)
                # v120b FixG: 判据抽成 _recent_progress_signals——与软墙/收敛 nudge
                #   的分诊【同口径铁律】: nudge 指了加时路·这里必批·不出现指路后拒载。
                _grew, _recent_compute = _recent_progress_signals(s, b)
                if not (_grew or _recent_compute):
                    # 拒:无真实进展 → 加预算治不了"方向错/原地打转"
                    summary = ("[加预算·拒绝] 你最近几步没有实质进展(没抓到新证据/没算出中间结果)——"
                               "这种情况加预算也是继续空转。真正该做的是【换策略】:换工具/换来源/"
                               "问 ask_expert_model 换个模型独立算/skill_search 找现成方法·或若真走不通"
                               "就 document_wall 诚实声明。等你有了新进展(哪怕一小步)再来加预算。")
                    _emit_gate_event(qid, "budget_extend", "reject",
                                     {"why": "no_progress", "n_ext": _n_ext})
                # v-eff(2026-08-18·病1) · 加时绝对硬上限(相对 base×倍率)——
                #   已到顶就【拒批】(不消耗次数·不是 agent 的错·是防失控硬墙)·
                #   逼它用已有证据收尾。这是治"跑23分钟"的最核心一道拦。
                elif (MEYO_ENGINE_EFFICIENCY
                      and float(b.get("hard_wall_s") or 0) >= _effective_wall_cap(b) - 1.0):
                    _cap = _effective_wall_cap(b)
                    _base = float(b.get("base_wall_s") or 0)
                    summary = (
                        f"[加预算·拒绝·绝对硬上限] 本题累计加时已到硬顶 {_cap:.0f}s"
                        f"(=base {_base:.0f}s × {BUDGET_EXTEND_WALL_CAP_MULT:g})——再续也会被这道"
                        f"硬墙挡回·继续烧=白烧。现在【必须立即收尾】: 基于已有证据/中间结果/"
                        f"已锁定候选 finalize_answer(标置信度)·或 document_wall 诚实声明。"
                        f"效率铁律:及时收尾好过无限烧墙。")
                    _emit_gate_event(qid, "budget_extend", "reject",
                                     {"why": "abs_wall_cap", "cap": round(_cap),
                                      "base": round(_base), "n_ext": _n_ext})
                else:
                    # 批准:加墙钟(有绝对上限)+加步数
                    _old_wall = float(b.get("hard_wall_s") or 0)
                    # v-eff(2026-08-18·病1): 上限取 min(相对 base×倍率, 旧绝对秒兜底)。
                    _new_wall = min(_effective_wall_cap(b),
                                    _old_wall * (1 + BUDGET_EXTEND_WALL_FRAC))
                    # v119 FixE: 有外层墙时·续完的墙不得晚于外层死线-收尾余量
                    #   (否则内层还在冲刺·外层先杀·续命白续)。
                    if _outer_cap_rel is not None:
                        _new_wall = min(_new_wall, max(_old_wall + 30.0, _outer_cap_rel))
                    b["hard_wall_s"] = _new_wall
                    b["soft_wall_s"] = _new_wall * DEFAULT_SOFT_WALL_RATIO
                    _old_iter = int(b.get("max_iterations") or 30)
                    b["max_iterations"] = _old_iter + _budget_step_add(s)
                    b["_budget_extends"] = _n_ext + 1
                    # 加了预算·软墙提醒/收敛标记复位·让新预算真正可用
                    b["soft_nudge_sent"] = False
                    b["_converge_nudged"] = False
                    b["hard_block_active"] = False
                    summary = (f"[加预算·已批准第{_n_ext + 1}/{_budget_extend_max(s)}次] 检测到你确有进展·"
                               f"墙钟 {_old_wall:.0f}s→{_new_wall:.0f}s·步数上限 {_old_iter}→"
                               f"{b['max_iterations']}。抓紧把手头的计算/多跳链走完再 finalize——"
                               f"这是给你完成【正在进行的正确工作】的时间·不是重新开一条路的时间。"
                               f"理由记录: {_reason[:120]}")
                    _emit_gate_event(qid, "budget_extend", "approve",
                                     {"n_ext": _n_ext + 1, "old_wall": round(_old_wall),
                                      "new_wall": round(_new_wall), "reason": _reason[:80]})
        except Exception as e:
            summary = f"request_more_budget 异常: {type(e).__name__}: {str(e)[:100]}"
        _log_tool(qid, "request_more_budget", {"reason": reason[:120]}, summary, time.time() - t0)
        return summary

    def document_wall(what_failed: str, why: str, evidence: str = "") -> str:
        """【显式声明此路不通(能力墙)】(v98b·门控的结构化出口)。当你穷尽方法后确认某条路
        真走不通(订阅墙/反爬/资源不存在/工具真不适用)·用这个结构化声明·而非默默放弃或
        编个答案。声明后模态/工具门控放行(它认"你证明了墙"·不再逼你用那个工具)。
        what_failed: 哪条路/哪个工具走不通(如'see_video_frame抽帧')。
        why: 为什么走不通(如'该视频已被删除·yt-dlp报404')。
        evidence: 支撑这个判断的证据(如'yt-dlp输出:Video unavailable')。
        诚实用它=认知合规(证明了墙)·滥用它逃避=会被后续验证抓。"""
        t0 = time.time()
        s = _sess(qid)
        # v99b · 模态穷尽清单准入(2026-08-12):
        #   document_wall 原本是主观感·agent 觉得没路就能宣布·无审计。
        #   GameGrumps 实证：试了 2 帧就宣布"内容天花板"——清单准入直接拦住。
        #   视频题清单：①指定时刻帧 ②结构性时刻帧(含机制补抽) ③label_sweep
        #              ④transcribe_audio(音频ASR) ⑤字幕/元数据文本
        #   ①② 靠 _video_evidence_ok；③ 靠 _label_sweep_suggested 或已调；
        #   ④ 靠 tool_log 里 transcribe_audio；⑤ 靠 tool_log 里 fetch_url_smart
        #      命中视频元数据模式。清单不闭合：拦下 + 教学(列出未试项)。
        #   闭合条件宽松：清单项有任意已试记录即算已试，不要求成功——
        #   真试了下不动/报错才是有效证据，一次都没试直接宣墙=早泄。
        _triage_mods = (s.get("triage") or {}).get("modality_needs") or []
        _q_low = (s.get("question") or "").lower()
        _is_video_task = (
            any(m in ("video", "vision", "audio") for m in _triage_mods)
            and any(k in _q_low for k in ("youtube", "video", "视频", "影片", "channel",
                                           "narrated", "uploaded", "youtu.be"))
        )
        if _is_video_task:
            _tlog = s.get("tool_log") or []
            _called_tools = {t.get("tool") for t in _tlog}
            # video_modal_exhaustion 是新注册表的组合执行器；它把每种模态的实际
            # 执行结果结构化写入 session，墙门必须消费这个事实而不是只认旧工具名。
            # 不能因“工具名称变了”否认已经完成的帧/音频/元数据尝试。
            _modal_reports = s.get("video_modal_exhaustion") or []
            _latest_modal = _modal_reports[-1] if _modal_reports and isinstance(_modal_reports[-1], dict) else {}
            _modal_ran = "video_modal_exhaustion" in _called_tools
            # 检查各项是否已试过；清单准入只要求实际尝试，不以成功作为前置条件。
            _tried_frame = bool(s.get("_video_evidence_ok") or "see_video_frame" in _called_tools or
                                (_modal_ran and "frames" in _latest_modal))
            _tried_sweep = bool(s.get("_label_sweep_suggested") or "label_sweep" in _called_tools or
                                (_modal_ran and "frames" in _latest_modal))
            # 注册表版与 auto 闭包版 ASR 都是同一音频模态能力。
            _tried_audio = bool({"transcribe_audio", "transcribe_audio_local"} & _called_tools or
                                (_modal_ran and "audio" in _latest_modal))
            # 元数据闭合改按结构化工具结果/证据来源判断，避免通过命令措辞猜测。
            _tried_meta = bool(_modal_ran and "metadata_subtitles" in _latest_modal) or any(
                t.get("tool") == "fetch_url_smart" and "video" in str(t.get("summary") or "").lower()
                for t in _tlog
            ) or any(
                str(e.get("source") or "") in {"video_metadata", "yt_dlp_metadata"}
                for e in (s.get("evidence") or []) if isinstance(e, dict)
            )
            _untried = []
            if not _tried_frame:
                _untried.append("④ see_video_frame / transcribe_audio (帧或音轨，视频最直接证据)")
            if not _tried_sweep:
                _untried.append("② label_sweep (稀疏扫全片OCR定位含文字时刻)")
            if not _tried_audio:
                _untried.append("③ transcribe_audio (ASR转录·GameGrumps主持人会喊赛道名)")
            if not _tried_meta:
                _untried.append("⑤ 视频元数据文本 (yt-dlp --print title/description/subtitles·字幕或简介常直接写内容)")
            if _untried:
                _untried_str = "\n".join(f"  · {u}" for u in _untried)
                summary = (
                    f"[document_wall 未通过模态穷尽检查] 你试了{len(_called_tools)}种工具就宣布墙·"
                    f"但视频题的证据清单还有 {len(_untried)} 项没碰过：\n{_untried_str}\n"
                    "每一项都比'试两帧就宣墙'便宜·先逐项试完再宣布天花板。\n"
                    "清单全闭合后再调 document_wall——那才是真边界，不是模态覆盖缺口。"
                )
                _log_tool(qid, "document_wall", {"what": what_failed, "why": why,
                           "_wall_blocked": True}, summary, time.time() - t0)
                return summary
        # 提案G2/W: 通用模态穷尽阶梯(2026-08-14·把视频专用清单泛化到全模态)
        #   错题共性:证据未穷尽即宣墙(document_wall被模态准入拦/看门狗停滞后直接放弃)。
        #   逻辑:对 triage 声明的每个模态需求·检查对应通道工具是否至少试过一次·
        #        有明确相关通道从没碰过→拦下+教学(列未试通道)·而非直接放行宣墙。
        #   fail-safe:只在'有相关通道且一次都没试'时拦·任何异常→放行(不误伤真边界)。
        try:
            _tlog2 = s.get("tool_log") or []
            _called2 = {t.get("tool") for t in _tlog2}
            _mods2 = set((s.get("triage") or {}).get("modality_needs") or [])
            # 模态→该模态"至少试过一种通道"的判定(非视频·视频已在上面单独处理)
            _MOD_CHANNELS = {
                "vision": {"see_image_vlm", "see_image_expert", "see_image_zoom",
                           "solve_fraction_worksheet"},
                "image": {"see_image_vlm", "see_image_expert", "see_image_zoom",
                          "solve_fraction_worksheet"},
                "pdf": {"read_pdf"},
                "table": {"read_table", "query_table"},
                "audio": {"transcribe_audio", "transcribe_audio_local"},
                "web": {"fetch_url_smart", "search_web", "web_search"},
            }
            _untried2 = []
            for _m in _mods2:
                if _m in ("text", "video"):  # text无专用通道·video已上面处理
                    continue
                _chs = _MOD_CHANNELS.get(_m)
                if not _chs:
                    continue
                if not (_chs & _called2):  # 该模态一个通道都没碰过
                    _untried2.append(f"{_m}: 试 {' / '.join(sorted(_chs))} 之一")
            if _untried2:
                _u2 = "\n".join(f"  · {u}" for u in _untried2)
                summary = (
                    f"[document_wall 未通过模态穷尽检查] 题目triage声明需要模态 {sorted(_mods2)}·"
                    f"但有 {len(_untried2)} 个模态通道从没试过就要宣墙:\n{_u2}\n"
                    "每个通道都比'没试就宣墙'便宜·逐个试完再判是否真边界。\n"
                    "全试过(哪怕失败·真试了报错才算证据)再调 document_wall。")
                _log_tool(qid, "document_wall", {"what": what_failed, "why": why,
                           "_wall_blocked": True, "_gap": "modality_not_exhausted"},
                          summary, time.time() - t0)
                return summary
        except Exception:
            pass  # fail-safe:穷尽检查本身出错绝不拦真边界·照常放行

        s["_documented_wall"] = {"what": what_failed[:150], "why": why[:200], "evidence": evidence[:300]}
        summary = (f"[已声明能力墙] {what_failed[:80]} 走不通·因: {why[:120]}。"
                   "门控放行(你已证明此路不通·非动作合规而是认知合规)。"
                   "现在可基于已有证据尽力作答·或换真正不同的路。")
        _log_tool(qid, "document_wall", {"what": what_failed, "why": why}, summary, time.time() - t0)
        return summary

    def check_model_health(quick: bool = True) -> str:
        """【探测当前可用的模型·感知到某模型超时/失败时主动调这个】(v99c·D2·自主诊断)。
        每次 see_image_expert/ask_expert_model 遇到超时或 ok=False 后·调这个确认哪些模型还活着·
        下一步的工具调用就会自动路由到探测可用的模型·而不是死盯着同一个挂掉的模型重试。

        返回每个视觉/通用模型的当前可用状态 + 推荐路由顺序。

        quick=True(默认): 只读探测缓存(0.1s·无额外 API 消耗)·显示上次探测结果。
        quick=False: 真实发一句 "Reply: OK" 探测所有模型(每模型约30s·总计约1-2min·
          只在"缓存过期超过6小时"或"明确怀疑环境变了"时用)。"""
        t0 = time.time()
        s = _sess(qid)
        try:
            import sys as _sys2
            _agent_dir = os.path.dirname(os.path.abspath(__file__))
            if _agent_dir not in _sys2.path:
                _sys2.path.insert(0, _agent_dir)
            import multi_model as _mm2
            import model_orchestrator as _mo2

            if not quick:
                # 真实探测·结果写入缓存供后续 usable_models() 读
                _rep = _mm2.probe_models(save=True, ts=time.time())
                lines = []
                for name, info in _rep.items():
                    mark = "✅" if info["available"] else "❌"
                    lines.append(f"  {mark} {name}: {info.get('elapsed', '?')}s "
                                 f"{info.get('err', '')[:50]}")
                _usable = _mm2.usable_models()
                _vis = _mm2.usable_models(vision_only=True)
                summary = (f"[模型健康探测·完整版·{len(_rep)}个]\n" +
                           "\n".join(lines) +
                           f"\n可用: {_usable}\n视觉可用: {_vis}")
            else:
                # 只读缓存
                _all = _mm2.available_models()
                lines = []
                for name, spec in _all.items():
                    av = spec.get("available", None)
                    if av is True:
                        mark = "✅(cached)"
                    elif av is False:
                        mark = "❌(cached)"
                    else:
                        mark = "?(未探测)"
                    vision = "👁" if spec.get("vision") else "  "
                    lines.append(f"  {mark} {vision} {name}")
                _usable = _mm2.usable_models()
                _vis = _mm2.usable_models(vision_only=True)
                # 经验库路由顺序
                _vis_pref = _mo2._pick("vision", vision_only=True)
                summary = (f"[模型健康快照(缓存)]\n" +
                           "\n".join(lines) +
                           f"\n当前可用: {_usable}"
                           f"\n视觉推荐顺序(经验库): {_vis_pref}"
                           f"\n提示: 缓存6小时以上或环境有变·传 quick=False 做真实探测")
        except Exception as e:
            summary = f"check_model_health 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "check_model_health", {"quick": quick}, summary, time.time() - t0)
        return summary

    def scan_environment() -> str:
        """探测这台电脑你(agent)能用什么(用户: 让agent自主理解+接管电脑环境·电脑/桌面agent
        都是工具)。客观报告: 装了哪些CLI工具/软件、有哪些可互动的其他agent/LLM(claude/ollama
        可委托协作)、本地服务、沙盒能力(docker/venv·缺的软件可装了再用)、网络代理现状。
        遇到不知道该用什么工具、或怀疑"没有某能力"时·先调这个看清现状·别凭空断言没有。
        有的直接用·缺的用docker/venv/pip/brew装了再用·别的agent可委托。穷尽办法。"""
        t0 = time.time()
        try:
            import env_scan as _es
            report = _es.scan_environment()
            summary = _es.format_report(report)
        except Exception as e:
            summary = f"scan_environment 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "scan_environment", {}, summary[:1500], time.time() - t0)
        return summary

    def ask_local_llm(prompt: str, model: str = "") -> str:
        """委托【本地 LLM(ollama)】离线推理(用户: 别的agent/本地模型都是工具·穷尽办法)。
        不烧API额度·不走外网·适合: 批量小活、交叉验证第二意见、离线可算的推理。
        model 留空自动选一个本地已装模型。先 scan_environment 看有哪些本地模型可用。
        本地大模型(32b)较慢·小模型(10b)快;都比外部API省。"""
        t0 = time.time()
        s = _sess(qid)  # 2026-08-12 修:漏绑 s→NameError 崩(见 see_image_zoom/solve_chess_move 同款)
        allow, guard = _check_budget(s, "ask_local_llm", content_key=prompt[:80] + model)
        if not allow:
            _log_tool(qid, "ask_local_llm", {"model": model}, guard, time.time() - t0)
            return guard
        try:
            import requests as _rq
            # 没指定模型→拿本地第一个可用的
            if not model:
                tags = _rq.get("http://127.0.0.1:11434/api/tags", timeout=8).json()
                models = [m["name"] for m in tags.get("models", [])]
                # 优先小模型(快)
                model = next((m for m in models if any(s in m for s in ["10", "7b", "8b", "3b"])), models[0] if models else "")
                if not model:
                    return "ask_local_llm: 本地没有 ollama 模型·先 scan_environment 确认·或用 shell_exec `ollama pull <model>` 拉一个"
            r = _rq.post("http://127.0.0.1:11434/api/generate",
                         json={"model": model, "prompt": prompt[:4000], "stream": False},
                         timeout=180)
            resp = r.json().get("response", "") if r.status_code == 200 else f"HTTP {r.status_code}"
            summary = f"[本地LLM {model}] {resp[:1000]}"
        except Exception as e:
            summary = (f"ask_local_llm 异常: {type(e).__name__}: {str(e)[:120]}。"
                       "ollama 可能没起(shell_exec `ollama serve`)或模型没拉·可换外部 ask_expert_model。")
        _log_tool(qid, "ask_local_llm", {"model": model}, summary[:1200], time.time() - t0)
        return summary

    read_image_ocr.__name__ = "read_image_ocr"
    see_image_vlm.__name__ = "see_image_vlm"
    read_table.__name__ = "read_table"
    transcribe_audio.__name__ = "transcribe_audio"
    extract_structured.__name__ = "extract_structured"
    query_science_db.__name__ = "query_science_db"
    ask_expert_model.__name__ = "ask_expert_model"
    see_image_expert.__name__ = "see_image_expert"
    see_video_frame.__name__ = "see_video_frame"
    see_image_zoom.__name__ = "see_image_zoom"
    scan_environment.__name__ = "scan_environment"
    ask_local_llm.__name__ = "ask_local_llm"
    set_reflect_threshold.__name__ = "set_reflect_threshold"
    request_more_budget.__name__ = "request_more_budget"
    document_wall.__name__ = "document_wall"
    label_sweep.__name__ = "label_sweep"
    traverse_web.__name__ = "traverse_web"
    return [shell_exec, file_read, file_write, read_image_ocr, see_image_vlm, read_table,
        read_pdf,  # v13.98 · PDF一竖到底:pdf_space+traversal_core骨架驱动·遇PDF附件题走结构化遍历(替代把PDF当图OCR)
        query_table,  # v13.98 · CSV/Excel一竖到底:space_table+骨架·表格题走结构化遍历(筛选/求和/分组·精确算不心算)
        traverse_image,  # v13.98 · 图像空间(bbox)挂骨架:整图读不清自动分块精读+证据图
        traverse_video,  # v13.98 · AV空间(时间轴)挂骨架:扫时间轴定位+跳时刻精读
        traverse_web,  # 网页空间:搜索/页面/分页链接形成可寻址证据图并动态遍历
        gui_operate,  # v13.98 · GUI操作空间(能动·可写):读屏/点击/键盘·写操作强制hitl审批(不可逆·红线)
        ask_human,  # v13.98 · human协作空间(能动):卡住时委托人类·像人一样求助·hitl等答复fail-safe
        transcribe_audio, extract_structured, query_science_db,
        ask_expert_model, see_image_expert, see_video_frame,
        label_sweep,  # v99a · M-83真仪器:稀疏扫帧OCR定位含文字时刻·替代"盲猜时间戳+VLM整体猜"
        see_image_zoom, scan_environment, ask_local_llm, set_reflect_threshold,
        request_more_budget,  # 层2 · agent 自主加预算(有进展信号+上限·守"越跑越慢"红线)
        document_wall]

