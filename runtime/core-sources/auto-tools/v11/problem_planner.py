"""
v11 · Agent A · 问题规划师
========================
输入：一个真实职业问题（如"PM 客户拍桌不想再卷价格"）
输出：拆解成 3-5 个可解决的子任务 + 每个子任务的成功标准

不做：搜 skill、跑 skill、写内容 —— 只规划

保存：v11/plans/YYYYMMDD_HHMMSS_slug.json
"""
import os, sys, json, re, glob, requests
from datetime import datetime

# v13.63 · 盲区 3 · 用户元规则前置注入 (checklist injector)
try:
    from evolution.checklist_injector import get_prefix as _get_checklist_prefix
except Exception:
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "evolution"))
        from checklist_injector import get_prefix as _get_checklist_prefix
    except Exception:
        _get_checklist_prefix = lambda caller=None, brief=True: ""


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V11 = os.path.dirname(os.path.abspath(__file__))
PLANS_DIR = os.path.join(V11, "plans")
os.makedirs(PLANS_DIR, exist_ok=True)


def _get_llm_config():
    base_url, auth = "", ""
    for f in glob.glob("/var/folders/*/*/T/claude-code-router/ccr-settings-*.json"):
        try:
            env = json.load(open(f)).get("env", {})
            base_url = env.get("ANTHROPIC_BASE_URL", "") or base_url
            auth = env.get("ANTHROPIC_AUTH_TOKEN", "") or auth
            if auth:
                break
        except:
            pass
    return (
        base_url or os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:3456"),
        auth or os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
    )



# v13.67 · turn_hook · 每次 _llm 后落 self_prompt + 异步 judge
try:
    import sys as __sys, os as __os
    __sys.path.insert(0, __os.path.join(__os.path.dirname(__os.path.abspath(__file__)), "evolution"))
    from turn_hook import record_self_prompt as __rsp
except Exception:
    __rsp = None
def __hook_after_llm(caller, prompt, response):
    if __rsp:
        try: __rsp(caller, str(prompt)[:8000], str(response)[:8000])
        except Exception: pass

def _llm(prompt: str, max_tokens: int = 2000, timeout: int = 120,
         model: str = "sankuai,aws.claude-haiku-4.5") -> str:
    """v13.17 (2026-07-09) · 用户解除预算·加 model 参数
    默认仍 haiku (跑量高·如挖题拆子任务)
    但可以 model='sankuai,aws.claude-opus-4.7' 走 opus (对决策关键点)
    - agent_search.plan_queries → 用 opus (query 质量决定命中)
    - agent_search.extract_evidence → 用 opus (evidence 抽取决定 writer 素材)"""
    base, auth = _get_llm_config()
    resp = requests.post(
        f"{base}/v1/messages",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth}",
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise Exception(f"LLM {resp.status_code}: {resp.text[:200]}")
    text = "".join(b.get("text", "") for b in resp.json().get("content", []))
    __hook_after_llm(f"problem_planner._llm[{model.split(',')[-1][:20]}]", prompt, text)  # v13.67
    return text


def _llm_opus(prompt: str, max_tokens: int = 3000, timeout: int = 180) -> str:
    """快捷: 走 opus-4.7 · 用于 query 拆分/evidence 抽取等决策关键点"""
    return _llm(prompt, max_tokens=max_tokens, timeout=timeout,
                model="sankuai,aws.claude-opus-4.7")


def plan_problem(problem: str, role: str = "") -> dict:
    """
    分析一个职业问题 → 拆解 → 定义成功标准

    Returns:
        {
            "problem": str,
            "role": str,
            "problem_type": str,        # 归类：分析型/生产型/决策型/沟通型
            "subtasks": [
                {
                    "id": "T1",
                    "name": str,
                    "description": str,
                    "success_criteria": [str, str, ...],  # 3-5 条硬标准
                    "expected_output_shape": str,          # 期望的产出形态
                },
                ...
            ],
            "overall_success": [str, ...],  # 整体解决问题的判定标准
            "acceptable_min_score": int,     # 单次尝试可接受最低分（默认 70）
        }
    """
    _checklist_pfx = _get_checklist_prefix(caller="problem_planner", brief=True)
    prompt = f"""{_checklist_pfx}\n你是一位资深的 AI 应用架构师，专精于把职业问题拆解成 AI 可以真实解决的子任务。

## 用户的职业问题
**岗位**：{role or '未指定'}
**问题**：{problem}

## 你的任务
1. 判断问题类型（分析型 / 生产型 / 决策型 / 沟通型）
2. 拆成 **2-3 个**（不是 3-5！精简）**可以独立跑 AI skill 解决**的子任务 · **少而精 · 每个必须核心** · v12.8 提速
3. 每个子任务定义 3-5 条**可验证的成功标准**（不是"感觉不错"，是"是否输出了 N 个具体的 X"）
4. 给出整体解决问题的判定标准

**核心原则**：**subtasks 少 = 内容更聚焦**（3 个 subtask × 5 步 tool = 15 步；2 个 subtask × 5 步 = 10 步 · 快 33%）

## 输出（严格 JSON）
```json
{{
  "problem_type": "<分析型/生产型/决策型/沟通型>",
  "subtasks": [
    {{
      "id": "T1",
      "name": "<子任务名>",
      "description": "<20 字内说清要做什么>",
      "success_criteria": [
        "<可验证的硬标准 1>",
        "<可验证的硬标准 2>",
        "..."
      ],
      "expected_output_shape": "<列表/表格/文档/JSON 等>",
      "why_this_first": "<为什么这一步优先做>"
    }},
    ...
  ],
  "overall_success": [
    "<整体成功判定标准 1>",
    "<整体成功判定标准 2>",
    ...
  ],
  "acceptable_min_score": 70
}}
```

只输出 JSON，不要任何其他文字。"""
    raw = _llm(prompt, max_tokens=2500)
    m = re.search(r"\{[\s\S]+\}", raw)
    if not m:
        return {"error": "parse_failed", "raw": raw[:500]}
    try:
        plan = json.loads(m.group())
    except json.JSONDecodeError as e:
        return {"error": f"json_error: {e}", "raw": raw[:500]}

    # 补齐元数据
    plan["problem"] = problem
    plan["role"] = role
    plan["planned_at"] = datetime.now().isoformat()

    # 落盘
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^\w一-鿿]+", "-", problem[:30])[:30].strip("-")
    path = os.path.join(PLANS_DIR, f"{ts}_{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    plan["_saved_to"] = path
    return plan


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--problem", required=True)
    p.add_argument("--role", default="")
    args = p.parse_args()

    plan = plan_problem(args.problem, args.role)
    print(f"═══ Plan (类型={plan.get('problem_type','?')}) ═══")
    for st in plan.get("subtasks", []):
        print(f"\n【{st['id']}】 {st['name']}")
        print(f"  {st['description']}")
        for c in st.get("success_criteria", []):
            print(f"  ✓ {c}")
    print(f"\n落盘: {plan.get('_saved_to', '?')}")
