---
name: investigate
description: 用结构化任务契约并行委派只读调查，禁止递归派生，收集可合并证据。
---

# 只读调查编排

## 派发前

1. 把目标拆成相互独立、边界明确的调查面；通常 2 个视角足够。
2. 每个任务包必须包含：
   - `purpose: explore`
   - 精确文件/目录范围
   - 需要回答的问题
   - 证据格式 `file:line`
   - 时间预算
   - 明确写“禁止修改文件；禁止再创建或派发任何子 Agent/fork”
3. 只选择 `sys_agent_list` 中已注册且当前健康的 `meyo-*` 成员。
4. 同一波最多 2 个 child，避免上下文和成本失控。

## Worker 返回契约

要求每个成员最终只返回一个 JSON 对象：

```json
{
  "status": "completed|blocked",
  "findings": [{"claim": "", "evidence": ["file:line"]}],
  "unverified": [],
  "recommended_tests": []
}
```

禁止只回复“正在处理”“已启动 fork”或自由散文。

## 运行中

- 派发后让 provider 监督预算、取消与替补；不要用反复 `send` 催促代替状态管理。
- 收到完成通知后读 inbox；结果空/格式错才读 child history。
- 一个成员卡住不阻塞全局：保留已完成证据，取消卡住项并交给健康替补。

## 综合

- 只基于结构化结果形成结论。
- 两家共同支持的结论标为已交叉验证。
- 冲突项必须补证据或列入 `unverified`，不得由首脑拍脑袋决定。
