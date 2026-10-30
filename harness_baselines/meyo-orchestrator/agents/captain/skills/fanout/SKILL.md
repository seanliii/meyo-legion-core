---
name: fanout
description: 按依赖与冲突分波并行，限定成员预算，并用结构化验收契约驱动实现任务。
---

# 高效并行派活

1. 先画最小依赖图：无依赖且不写同一文件的任务才并行；有依赖的按波次执行。
2. 每个任务包必须包含唯一 title、目的、范围、输入、输出、验收命令、时间预算和禁止项。
3. 实现成员不得继续创建 Agent 或 fork；一个任务只有一层 worker。
4. 每波最多 2 个活跃 child；完成一个才补下一个，减少拥堵。
5. provider 负责超时 stop、健康替补和证据 handoff；首脑不靠轮询猜状态。
6. 返回必须是结构化 JSON：

```json
{
  "status": "completed|blocked",
  "changed_files": [],
  "artifacts": [],
  "tests": [{"command": "", "status": "passed|failed"}],
  "unverified": []
}
```

7. 结果缺字段、测试未过或必需产物不存在时，任务不算完成；进入修复波次。
8. 不因为某家失败而突破用户规定的成员上限；替补替换原席位，不增加并发名额。
