#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""一次性诊断:启动全部可跑军团抓同一篇微信文章,看每家真实表现。

用户 2026-08-28:"启动全部军团,看看到底军团咋回事——连你都会绕 UA,它们这么聪明
不可能不知道,肯定是链路有问题挡住了。"这个脚本一把把全部家并行派出去抓同一 URL,
打印每家 status/attempts/是否托词/final,用真数据回答"哪家通、哪家卡、卡在哪"。
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

import legion_core as L  # noqa: E402


def main():
    url = "https://mp.weixin.qq.com/s/KfjaqIUbVEano9YAOCbJBg"
    task = f"读这篇微信公众号文章并总结:{url}。用中文给①标题主题②核心观点③关键结论。"
    runnable = L._aigc_ready().get("runnable_clis", [])
    print(f"=== 启动全部军团({len(runnable)}家):{runnable} ===", flush=True)

    def cb(ev):
        print(f"  [{ev.get('agent')}·{ev.get('t')}] {str(ev.get('x',''))[:180]}", flush=True)

    plan = {"tasks": {k: task for k in runnable}}
    r = L.dispatch(task, plan=plan, cb=cb, qhash="allteam_wechat_probe")

    print("\n====== 全军团结果汇总 ======", flush=True)
    compact = []
    for res in (r.get("results") or []):
        k = res.get("key"); st = res.get("status"); el = res.get("elapsed")
        at = res.get("attempts"); fin = str(res.get("final", ""))
        stall = L._looks_like_stall(fin)
        got = "微信" in fin or "横评" in fin or "记忆" in fin or "OpenViking" in fin
        print(f"[{k}] status={st} attempts={at} elapsed={el}s "
              f"stall={stall} 真抓到内容={got} len={len(fin)}", flush=True)
        print(f"     final: {fin[:200]}\n", flush=True)
        compact.append(f"{k}={st}/{'✓抓到' if got else '✗没抓到'}/{'托词' if stall else 'ok'}/{el}s")
    # 单行 summary(tail 也能看全部家):
    print("SUMMARY | " + " | ".join(compact), flush=True)


if __name__ == "__main__":
    main()
