#!/usr/bin/env python3
"""军团模型战绩观察器 v2·自动重排主力序(用户 2026-09-02 定:谁稳谁排前·自动替换)。

数据源=军团真实运行战绩(~/.legion/model_health.json·每家x模型 good/bad·record_model_health 每单落账)。
打分=Wilson 95%% 下界(小样本不虚高)。规则:
  1. 主力钉 gpt-5.6-sol + GLM-5.3(用户定稿)·兜底钉 aws.claude-opus-4.7(锁末位·不参与重排);
  2. 钉选主力也要资格赛:n>=30 且 wilson<0.35 = 明确坏了 → 自动换下·战绩最优者补位
     (用户:谁稳谁上·自动替换);样本 n<10 保持现位(数据不足不动);
  3. 能力档池自动发现:models.json programming 候选 + model_health 实战新模型·新 harness
     同理自动并入聚合(新家跑过的模型自动参与)——以后来新模型/新家零手改;
  4. 便宜快答档(flash/LongCat/4o-mini/GLM-5.2/deepseek-chat)战绩再好也不进能力档;
  5. vertex 全家+known_broken 绝不入池(上游封禁·战绩是历史);
  6. --apply 才真写 models.json+同步本机 CCR fallback 链·记 _rank_history;默认 dry-run。
用法: python3.12 model_rank_observer.py [--apply]
"""
import json, math, os, sys

MH = os.path.expanduser("~/.legion/model_health.json")
MJ = os.path.expanduser("~/.claude/skills/aigc-model-router/models.json")
CR = os.path.expanduser("~/.claude-code-router/config.json")
PIN_PRIMARY = ["gpt-5.6-sol", "GLM-5.3"]
ANCHOR_LAST = "aws.claude-opus-4.7"
CHEAP_TIER = {"deepseek-v4-flash", "GLM-5.3-flash", "GLM-5.2", "LongCat-2.0", "gpt-4o-mini", "deepseek-chat", "Doubao-Seed-1.6-thinking"}
MIN_N = 10
DISQ_N, DISQ_LB = 30, 0.35
EXCLUDE = ("vertex.", "aws.claude-opus-4.8", "aws.claude-opus-4.6", "model-", "m-b", "m-c", "parent-picked")
BAD_NAMES = {"m", "a", "b", "c", "A", "B", "RELAY"}  # 测试残留名单


def wilson(g, b):
    n = g + b
    if n == 0:
        return 0.0
    p = g / n; z = 1.96; z2 = z * z
    return (p + z2 / (2 * n) - z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / (1 + z2 / n)


def aggregate() -> dict:
    if not os.path.exists(MH):
        return {}
    agg = {}
    for harness, models in (json.load(open(MH)) or {}).items():
        if not isinstance(models, dict):
            continue
        for m, gb in models.items():
            if not isinstance(gb, dict) or any(t in m for t in EXCLUDE):
                continue
            a = agg.setdefault(m, {"good": 0, "bad": 0, "fams": {}})
            a["good"] += int(gb.get("good") or 0)
            a["bad"] += int(gb.get("bad") or 0)
            if (gb.get("good") or 0) > 0:
                a["fams"][harness] = gb.get("good")
    return agg


def _eligible(m):
    return m and m != ANCHOR_LAST and m not in BAD_NAMES and not any(t in m for t in EXCLUDE) and m not in CHEAP_TIER


def discover_pool(mj, agg):
    """能力档池自动发现:候选(人工验过的) + 实战新模型·排除死模型/便宜档/兜底钉。"""
    pool, seen = [], set()
    for c in (mj.get("programming") or {}).get("candidates", []):
        m = c.get("model") or ""
        if _eligible(m) and m not in seen:
            pool.append(m); seen.add(m)
    for m in agg:
        if _eligible(m) and m not in seen:
            pool.append(m); seen.add(m)
    return pool


def build_ranking(mj, agg):
    """钉选+资格赛+数据排序 → 完整主力序(含被换下主力的响亮警告)。"""
    pool = discover_pool(mj, agg)
    scored = {}
    for m in pool:
        a = agg.get(m, {"good": 0, "bad": 0})
        scored[m] = (wilson(a["good"], a["bad"]), a["good"] + a["bad"])
    # 低样本(n<MIN_N)不参与数据排序·保持 models.json 现位(数据不足不动·1样本wilson是噪音)
    cur_order = [c.get("model") for c in (mj.get("programming") or {}).get("candidates", [])]
    rankable = [m for m in pool if scored[m][1] >= MIN_N]
    insufficient = [m for m in pool if scored[m][1] < MIN_N]
    rankable.sort(key=lambda m: -scored[m][0])
    insufficient.sort(key=lambda m: cur_order.index(m) if m in cur_order else 999)
    ranked = rankable + insufficient
    # 资格赛:钉选主力 n>=DISQ_N 且 wilson<DISQ_LB → 自动换下·由非钉选最优者补位
    pins, demoted = [], []
    for m in PIN_PRIMARY:
        lb, n = scored.get(m, (0.0, 0))
        if n >= DISQ_N and lb < DISQ_LB:
            demoted.append((m, lb, n))
        else:
            pins.append(m)
    for m, lb, n in demoted:
        repl = next((r for r in ranked if r not in pins and r not in [d[0] for d in demoted]), None)
        if repl:
            pins.append(repl)
    rest = [m for m in ranked if m not in pins]
    return pins, rest, demoted, scored


def apply_models_json(mj, pins, rest, anchor):
    cands = mj["programming"]["candidates"]
    cur = [c["model"] for c in cands]
    order = pins + rest + [anchor] + [c["model"] for c in cands if c["model"] not in pins + rest + [anchor]]
    a = [c for c in cands if c["model"] in order]
    a.sort(key=lambda c: order.index(c["model"]))
    if [c["model"] for c in a] == cur:
        return cur, cur, False
    for i, c in enumerate(a):
        c["rank"] = i + 1
    mj["programming"]["candidates"] = a
    hist = mj.setdefault("_rank_history", [])
    hist.append({"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                 "before": cur, "after": [c["model"] for c in a], "source": "model_health wilson_lb v2"})
    mj["_rank_history"] = hist[-20:]
    return cur, [c["model"] for c in a], True


def sync_ccr_fallback(pins, rest):
    if not os.path.exists(CR):
        return
    c = json.load(open(CR))
    m = [x for x in (pins + rest) if x in ("gpt-5.6-sol", "GLM-5.3", "gpt-5.4", "deepseek-v4-pro", "gpt-5.6-terra", "gpt-5.5")]
    while len(m) < 4:
        m.append(m[-1] if m else "gpt-5.6-sol")
    fb = ["sankuai," + m[0], "sankuai," + m[0], "sankuai," + m[1], "sankuai," + m[1], "sankuai," + m[2], "sankuai," + m[3], "sankuai," + ANCHOR_LAST]
    c["fallback"] = {"default": fb, "background": fb, "think": fb, "longContext": fb}
    json.dump(c, open(CR, "w"), ensure_ascii=False, indent=2)


def main():
    do_apply = "--apply" in sys.argv
    agg = aggregate()
    mj = json.load(open(MJ))
    print("=== 军团模型真实战绩(跨家聚合·model_health·新家自动并入) ===")
    for m, a in sorted(agg.items(), key=lambda kv: -wilson(kv[1]["good"], kv[1]["bad"]))[:12]:
        n = a["good"] + a["bad"]
        print("  %-26s good=%4d bad=%4d n=%4d lb=%.3f fams=%d" % (m, a["good"], a["bad"], n, wilson(a["good"], a["bad"]), len(a["fams"])))
    pins, rest, demoted, scored = build_ranking(mj, agg)
    print()
    print("=== 排序(钉选主力+资格赛+数据) ===")
    print("  主力钉:", " + ".join(pins))
    for m, lb, n in demoted:
        print("  !!! %s 被资格赛换下(n=%d lb=%.2f<%.2f·战绩最优者补位)" % (m, n, lb, DISQ_LB))
    print("  数据序:", " > ".join(rest[:6]))
    before, after, changed = apply_models_json(mj, pins, rest, ANCHOR_LAST)
    if not do_apply:
        print()
        print("  dry-run(未写入). 新序预览:", after[:8])
        print("  确认后: python3.12 model_rank_observer.py --apply")
        return
    if changed:
        json.dump(mj, open(MJ, "w"), ensure_ascii=False, indent=2)
        sync_ccr_fallback(pins, rest)
        print()
        print("  [已写入] before:", before[:8])
        print("  [已写入] after :", after[:8])
        print("  [已同步] CCR fallback: 主力x2各2次+数据位+兜底", ANCHOR_LAST)
    else:
        print()
        print("  [无变化] 排序已一致")


if __name__ == "__main__":
    main()
