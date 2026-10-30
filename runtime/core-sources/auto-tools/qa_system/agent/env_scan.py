"""env_scan · 2026-08-10 · agent自主探测电脑环境(用户: agent自己发现能用什么·电脑/桌面agent都是工具·彻底接管电脑)

不是外部写死清单告诉agent·而是agent运行时【自己调用】这个工具·动态发现:
  - 装了哪些CLI工具/软件(能直接用的)
  - 有哪些可互动的其他agent/LLM(claude/ollama等·可委托/协作)
  - 有哪些本地服务(端口·如ollama LLM、数据库)
  - 沙盒能力(docker/venv·没有的软件可装了再用)
  - 网络/代理现状(能连外网吗·走代理吗)
agent理解后直接使用;缺的用沙盒装。穷尽办法·电脑本身就是工具。
"""
import os
import shutil
import subprocess as _sp
import socket
import json as _json


def _which(name):
    return shutil.which(name) or ""


def _port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _run(cmd, timeout=5):
    try:
        r = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def scan_environment(probe_categories=None):
    """agent自主探测电脑能用什么。返回结构化环境报告(agent读了自己决定用啥)。
    不写死"该用什么工具"·只客观报告"有什么"·判断权交给agent。
    probe_categories: 限定探测范围(可选)·默认全扫。"""
    report = {}

    # ① CLI工具/软件 —— 探测常见+专用·agent自己判断哪个对当前题有用
    tools = {}
    probe_tools = [
        # 媒体
        "yt-dlp", "ffmpeg", "ffprobe", "imagemagick", "convert",
        # 文档/OCR
        "tesseract", "pandoc", "pdftotext", "qpdf", "libreoffice",
        # 计算/数据
        "python3", "sqlite3", "jq", "R", "gnuplot",
        # 专业引擎
        "stockfish", "lc0",
        # 开发/沙盒
        "docker", "git", "gh", "node", "npm", "brew", "cargo", "go",
        # 网络
        "curl", "wget", "aria2c",
    ]
    for t in probe_tools:
        p = _which(t)
        if p:
            tools[t] = p
    report["cli_tools"] = tools

    # ② 可互动的其他agent/LLM —— 电脑上别的AI也是工具(可委托/协作/交叉验证)
    agents = {}
    for a in ["claude", "ollama", "cursor", "aider", "llm", "gh"]:
        p = _which(a)
        if p:
            agents[a] = p
    # ollama 本地模型清单(能用哪些本地LLM)
    if agents.get("ollama") and _port_open(11434):
        models = _run(["ollama", "list"], timeout=8)
        agents["ollama_models"] = models[:500]
    report["agents"] = agents

    # ③ 本地服务(端口) —— LLM网关/数据库/缓存
    services = {}
    known = {3456: "CCR(claude网关)", 11434: "ollama(本地LLM)",
             5432: "postgres", 6379: "redis", 3000: "web", 8080: "web"}
    for port, name in known.items():
        if _port_open(port):
            services[f"{port}"] = name
    report["local_services"] = services

    # ④ 沙盒能力 —— 缺的软件能不能装了再用(docker/venv/pip/brew)
    sandbox = {}
    if _which("docker"):
        docker_ok = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
        sandbox["docker"] = docker_ok or "installed(daemon可能没起)"
    sandbox["pip_install"] = bool(_which("pip") or _which("pip3"))
    sandbox["brew_install"] = bool(_which("brew"))
    sandbox["venv"] = True  # python3 自带
    report["sandbox"] = sandbox

    # ⑤ 网络/代理现状 —— 能连外网吗·走代理吗(agent决定工具走不走代理)
    net = {}
    proxy = ""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        if os.environ.get(k):
            proxy = os.environ[k]
            break
    if not proxy:
        # macOS 系统代理
        scutil = _run(["scutil", "--proxy"], timeout=5)
        if "HTTPSProxy" in scutil:
            import re
            h = re.search(r"HTTPSProxy\s*:\s*(\S+)", scutil)
            p = re.search(r"HTTPSPort\s*:\s*(\S+)", scutil)
            if h and p:
                proxy = f"http://{h.group(1)}:{p.group(1)}"
    net["system_proxy"] = proxy or "(无·或直连)"
    net["note"] = ("有代理时·连外网工具(yt-dlp等)加 --proxy 走它;LLM走本地不走代理"
                   if proxy else "直连")
    report["network"] = net

    return report


def format_report(report):
    """把探测报告格式化成给agent读的文本(客观陈述有什么·不替它决定用什么)。"""
    lines = ["【环境探测·你(agent)能用什么】客观报告·你自己判断哪个对当前任务有用:"]
    if report.get("cli_tools"):
        lines.append(f"· CLI工具({len(report['cli_tools'])}个): " + ", ".join(report["cli_tools"].keys()))
    if report.get("agents"):
        ag = {k: v for k, v in report["agents"].items() if k != "ollama_models"}
        lines.append(f"· 可互动的其他agent/LLM: " + ", ".join(ag.keys()))
        if report["agents"].get("ollama_models"):
            lines.append(f"  ollama本地模型: {report['agents']['ollama_models'][:200]}")
    if report.get("local_services"):
        lines.append(f"· 本地服务: " + ", ".join(f"{k}={v}" for k, v in report["local_services"].items()))
    if report.get("sandbox"):
        sb = report["sandbox"]
        lines.append(f"· 沙盒(缺的软件可装了再用): docker={sb.get('docker','无')}, "
                     f"pip={sb.get('pip_install')}, brew={sb.get('brew_install')}, venv={sb.get('venv')}")
    if report.get("network"):
        lines.append(f"· 网络: 系统代理={report['network']['system_proxy']} ({report['network']['note']})")
    lines.append("→ 有的直接用·缺的用docker/venv/pip/brew装了再用·别的agent(claude/ollama)可委托协作。穷尽办法。")
    return "\n".join(lines)


if __name__ == "__main__":
    r = scan_environment()
    print(format_report(r))
    print("\n--- 原始JSON ---")
    print(_json.dumps(r, ensure_ascii=False, indent=1)[:800])
