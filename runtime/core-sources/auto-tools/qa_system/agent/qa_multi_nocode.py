#!/usr/bin/env python3
"""v51 · 每层自动判 nocode + 部署
用户 2026-07-27 抓「不是应该与各行动项自动决定和使用创建部署吗」

设计:
  1. 输入: opus 已生的工具锥 markdown (含 4 层 ### ▶ )
  2. longcat-2.0 判每层需求:
     - 该层内容是否需要交互工具 (True/False)
     - 若 True · 生 nocode prompt (页面规格)
  3. 需要 nocode 的层 · 并发 subprocess 调 nocode_delivery
  4. 拿到 URL 回写该层的「**nocode 部署** · URL」段

调用:
  from qa_multi_nocode import deploy_multi_nocode
  final_md = deploy_multi_nocode(question, cone_md, qid="q01")
"""
import os
import re
import sys
import json
import time
import queue
import threading
import subprocess
import concurrent.futures
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_CLI = os.environ.get("MEYO_CLAUDE_CLI", "claude")
NOCODE_CLI = os.environ.get("MEYO_NOCODE_CLI", "nocode")
SKILL_ID = os.environ.get("MEYO_NOCODE_SKILL_ID", "2981")


def _call_llm(model, prompt, timeout=45):
    try:
        r = subprocess.run(
            [CLAUDE_CLI, "--print", "--model", model, prompt],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "CLAUDE_SKIP_USER_HOOKS": "1",
                 "CLAUDE_IN_RULE_EXTRACTOR": "1"},
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def split_cone_layers(cone_md):
    """把 ## 开始行动 段拆成 N 层 · 每层含 title+body"""
    layers = []
    # 找 ### ▶ 段
    parts = re.split(r'\n(?=### ▶)', cone_md)
    for p in parts:
        m = re.match(r'###\s*▶\s*([^\n]+)', p.strip())
        if m:
            title = m.group(1).strip()
            layers.append({"title": title, "body": p.strip()})
    return layers


def judge_layer_needs_nocode(question, layer_title, layer_body):
    """longcat-2.0 判该层是否需要 nocode 交互工具"""
    prompt = f"""判断这层「开始行动」是否需要 nocode 交互工具。

# 原题
{question}

# 该层标题
{layer_title}

# 该层内容 (前 800 字)
{layer_body[:800]}

# 判决标准
需要 nocode (need_tool=true) 的场景:
- 有**用户输入 → 系统输出**的交互 (填字段·点按钮·出结果)
- 数据要**动态生成/组合** (不是静态打印表)
- 内容有**多种参数组合** (客户类型 × 流失阶段 × 决策链)

不需要 nocode (need_tool=false) 的场景:
- 静态 markdown 表格/prompt 打印用
- 已推荐现成工具 (飞书多维表·github repo)
- 纯思路/checklist·用户自己填就行

# 严格 JSON
{{"need_tool": true/false, "reason": "20 字·为什么",
  "tool_spec": "若 true·1-2 句描述工具功能 (输入 X → 输出 Y)·else 空字符串"}}
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=25)
    d = _parse_json(r)
    return {
        "need_tool": bool(d.get("need_tool")),
        "reason": d.get("reason", ""),
        "tool_spec": d.get("tool_spec", ""),
    }


def _read_proc_with_timeout(proc, wall_deadline, poll_s=1.0):
    """带真正超时保护地读取子进程 stdout,直到进程退出或超过 wall_deadline。

    修复#5(死锁风险):原实现是主线程里直接 `proc.stdout.readline()`——这是
    阻塞调用,只有"读到一行"或"管道被关闭(EOF)"才会返回。若子进程卡住
    (比如 nocode CLI 内部网络请求挂死、或意外弹出等待终端输入的提示)且
    既不产生新的一行输出、也不退出、也不关闭 stdout,readline() 会永久
    阻塞,外层 `if time.time()-t0>400: kill()` 的超时检查根本执行不到
    (它在 readline() 返回之后才跑),整个调用线程(以及在
    ThreadPoolExecutor 里等它的线程池 worker)会被无限期占住,拖垮
    并发部署乃至整个 auto agent 任务。

    这里改用后台线程专门做阻塞 readline,主线程通过 Queue.get(timeout=..)
    轮询,poll_s 秒问一次"有没有新行/该不该超时",从而保证即使子进程完全
    不产生输出也能在超过 wall_deadline 时被判定超时并 kill,不会死等。
    reader 线程即便阻塞在 readline 上也不影响主线程退出(daemon=True)。

    返回: (lines: list[str], timed_out: bool)
    """
    q = queue.Queue()

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
                if not line:
                    break
        except Exception:
            pass
        finally:
            q.put(None)  # 哨兵:reader 结束(EOF 或异常)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    lines = []
    reader_done = False
    while True:
        if time.time() > wall_deadline:
            try:
                proc.kill()
            except Exception:
                pass
            return lines, True
        try:
            item = q.get(timeout=poll_s)
        except queue.Empty:
            # 没有新输出 · 只要没超时就继续等,同时兜底看看进程是否已退出
            # 但 stdout 未关闭导致 reader 线程卡住(极少见但可能发生)
            if proc.poll() is not None and reader_done:
                break
            continue
        if item is None:
            reader_done = True
            # reader 线程已结束(EOF)。再确认一次进程已退出;
            # 极端情况下 stdout 被提前关闭但进程还没退出,给它一点时间。
            if proc.poll() is not None:
                break
            continue
        lines.append(item)
    return lines, False


def deploy_one_nocode(layer_idx, layer_title, tool_spec, qid=""):
    """真跑 nocode create + deploy · 返 URL 或 None"""
    if not tool_spec:
        return None
    prompt = f"""建一个「{layer_title}」的交互工具页面·帮 CSM 快速产出针对具体客户的定制脚本。

需求 (基于 tool_spec): {tool_spec}

页面白底、主色 #ff2442、现代简洁风·纯 HTML + Vanilla JS·单文件·CDN 可用·左右分栏输入-输出。

左侧输入区必须有:
- 3-6 个字段 (客户名/合同金额/流失阶段等·根据 tool_spec 自选)
- 2 个示例数据按钮 (点击自动填入 demo)
- 主按钮「🚀 生成」·主色 #ff2442

右侧输出区:
- 卡片式布局展示结果
- 顶部两个按钮「📋 复制」「📥 导出 Markdown」

字体系统默认无衬线·圆角 12px·卡片轻投影·输入框聚焦时边框变主色。所有生成逻辑在前端 JS 模板拼装。"""

    t0 = time.time()
    p_create = None
    p_deploy = None
    try:
        # create · 用 Popen bufsize=1 拿实时 log
        p_create = subprocess.Popen(
            [NOCODE_CLI, "create", "--skillId", SKILL_ID, prompt],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        create_out, timed_out = _read_proc_with_timeout(p_create, t0 + 400)
        if timed_out:
            print(f"    [multi_nocode] L{layer_idx+1} create 超时(400s)·已 kill",
                  file=sys.stderr)
            return None
        m = re.search(r'chatId["\':\s]+([a-z0-9-]+)', "".join(create_out))
        if not m:
            return None
        chat_id = m.group(1)

        # deploy
        p_deploy = subprocess.Popen(
            [NOCODE_CLI, "deploy", chat_id],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        deploy_out, timed_out = _read_proc_with_timeout(p_deploy, t0 + 550)
        if timed_out:
            print(f"    [multi_nocode] L{layer_idx+1} deploy 超时(550s)·已 kill",
                  file=sys.stderr)
            return None
        m = re.search(r'https://[a-z0-9-]+\.mynocode\.host', "".join(deploy_out))
        if not m:
            return None
        return {
            "url": m.group(0),
            "chat_id": chat_id,
            "duration": time.time() - t0,
            "layer_idx": layer_idx,
            "layer_title": layer_title,
        }
    except Exception:
        return None
    finally:
        # fail-safe 兜底:无论走哪条路径退出(含未预料的异常),都确保两个
        # 子进程不会变成孤儿进程继续占用资源(之前只在超时分支里 kill,
        # 其余异常路径子进程可能残留)。
        for p in (p_create, p_deploy):
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass


def rewrite_layer_with_nocode(layer_body, nocode_info, tool_spec):
    """把 nocode URL 嵌进该层内部 · 替换或追加 nocode 部署段"""
    if not nocode_info or not nocode_info.get("url"):
        return layer_body

    url = nocode_info["url"]

    # 让 opus 生「打开怎么用」100-150 字使用说明
    prompt = f"""为下面这个 nocode 工具·写 100-150 字的「打开怎么用」使用说明。

# 工具功能
{tool_spec}

# 输出格式 (严格·markdown)
```
**nocode 部署** · [工具真标题·10-15 字](URL)

**打开怎么用**:
1. 填 [具体字段 · 3-5 个]
2. 点 [具体按钮]
3. 得 [具体输出]
4. 然后 [下一步]
```

其中 URL 就是: {url}
只输 markdown 段·不加代码块包裹·不加解释:"""
    nocode_md = _call_llm("longcat-2.0", prompt, timeout=30)

    # 追加到该层末尾
    return layer_body.rstrip() + "\n\n" + nocode_md.strip() + "\n"


def deploy_multi_nocode(question, cone_md, qid="", max_deploys=3, log=print,
                          main_nocode_url=None, main_tool_spec=""):
    """主入口 · v52 · 支持主 nocode URL 融合进最合适的层
    输入: question · 已生 cone_md (含 ## 开始行动 段)
    输出: 新 cone_md · 需要 nocode 的层已嵌真 URL · 主 nocode 也融合
    max_deploys: 最多部署几个 (避免 4 层全部署要 15 min)
    main_nocode_url: step7 已跑好的主 nocode URL · 若有 · longcat-2.0 判嵌哪层最合适
    main_tool_spec: 主 nocode 的功能描述
    """
    layers = split_cone_layers(cone_md)
    if not layers:
        log("  [multi_nocode] 无 ### ▶ 层 · 跳过", file=__import__("sys").stderr)
        return cone_md

    log(f"  [multi_nocode] 拆到 {len(layers)} 层 · 判每层是否需 nocode", file=__import__("sys").stderr)

    # 每层 longcat-2.0 判
    need_deploy = []
    for i, layer in enumerate(layers):
        judge = judge_layer_needs_nocode(question, layer["title"], layer["body"])
        layer["judge"] = judge
        if judge["need_tool"]:
            need_deploy.append((i, layer))
            log(f"    L{i+1} 「{layer['title'][:30]}」· 需 nocode · {judge['reason']}",
                file=__import__("sys").stderr)
        else:
            log(f"    L{i+1} 「{layer['title'][:30]}」· 不需 · {judge['reason']}",
                file=__import__("sys").stderr)

    if not need_deploy:
        log("  [multi_nocode] 无层需 nocode · 跳过", file=__import__("sys").stderr)
        return cone_md

    # 限量 · 避免爆
    need_deploy = need_deploy[:max_deploys]
    log(f"  [multi_nocode] 部署 {len(need_deploy)} 个 nocode (max_deploys={max_deploys}·并发)",
        file=__import__("sys").stderr)

    # 并发部署 (每个层独立 subprocess · 不用 executor 保护)
    deployed = {}  # layer_idx -> nocode_info
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(need_deploy)) as ex:
        futs = {}
        for idx, layer in need_deploy:
            fut = ex.submit(deploy_one_nocode, idx, layer["title"],
                             layer["judge"]["tool_spec"], qid)
            futs[fut] = idx
        for fut in concurrent.futures.as_completed(futs, timeout=700):
            idx = futs[fut]
            try:
                result = fut.result()
                if result:
                    deployed[idx] = result
                    log(f"    L{idx+1} ✅ {result['url']} · {result['duration']:.0f}s",
                        file=__import__("sys").stderr)
                else:
                    log(f"    L{idx+1} ❌ nocode 部署失败",
                        file=__import__("sys").stderr)
            except Exception as e:
                log(f"    L{idx+1} ❌ exception · {str(e)[:80]}",
                    file=__import__("sys").stderr)

    # 回写每层
    new_layers = []
    for i, layer in enumerate(layers):
        if i in deployed:
            new_body = rewrite_layer_with_nocode(
                layer["body"], deployed[i], layer["judge"]["tool_spec"]
            )
            new_layers.append(new_body)
        else:
            new_layers.append(layer["body"])

    # v52 · main_nocode_url 融合到最合适的层 (longcat-2.0 判)
    # 如果主 nocode 已跑好 · 挑一层内嵌 (通常是 MVP 层第 1 层)
    if main_nocode_url and main_tool_spec:
        # 找 MVP (第 1 层)·若已有 nocode 就跳
        if 0 not in deployed and len(new_layers) > 0:
            # 用 longcat-2.0 生使用说明
            nocode_md = rewrite_layer_with_nocode_url_only(
                main_nocode_url, main_tool_spec, layers[0]["title"]
            )
            new_layers[0] = new_layers[0].rstrip() + "\n\n" + nocode_md + "\n"
            log(f"  [multi_nocode] 主 nocode 已融合到 L1: {main_nocode_url}",
                file=__import__("sys").stderr)

    # 重组 · 保留 ## 开始行动 头
    header_m = re.search(r'^(.*?)(?=### ▶)', cone_md, re.DOTALL)
    header = header_m.group(1) if header_m else ""
    new_cone = header + "\n".join(new_layers)

    log(f"  [multi_nocode] 完成 · 嵌入 {len(deployed)}/{len(need_deploy)} 个真 URL + 主 nocode {'已融合' if main_nocode_url else '无'}",
        file=__import__("sys").stderr)
    return new_cone


def rewrite_layer_with_nocode_url_only(url, tool_spec, layer_title):
    """v52 · 只有 URL 没跑 nocode create (step7 已跑) · longcat-2.0 生使用说明"""
    prompt = f"""为下面这个 nocode 工具·写 100-150 字的「打开怎么用」使用说明·融合到「{layer_title}」这一层。

# 工具功能
{tool_spec}

# URL (一字不差用)
{url}

# 输出格式 (严格 markdown)
```
**nocode 部署** · [工具真标题·10-15 字]({url})

**打开怎么用**:
1. 填 [具体字段 · 3-5 个]
2. 点 [具体按钮]
3. 得 [具体输出]
4. 然后 [下一步·和「{layer_title}」层动作衔接]
```

只输 markdown 段·不加代码块包裹·不加解释:"""
    return _call_llm("longcat-2.0", prompt, timeout=30)


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) < 3:
        print("usage: qa_multi_nocode.py <question> <md_file>")
        _s.exit(1)
    q = _s.argv[1]
    md = open(_s.argv[2], encoding="utf-8").read()
    result = deploy_multi_nocode(q, md, qid="q_test")
    print(result[:2000])
