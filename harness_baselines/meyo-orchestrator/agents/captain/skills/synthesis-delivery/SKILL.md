---
name: synthesis-delivery
description: 交叉核对成员结果，生成最终产物并通过确定性交付门后才向用户完成。
---

# 综合与最终交付

## 收口步骤

1. 收齐每个任务的结构化结果；卡住项由 provider 取消换人，不无限等待。
2. 归并重复 findings，并保存每条结论对应的 evidence。
3. 至少选择一个高风险结论，由不同成员独立核对。
4. 生成用户要求的全部产物；首脑本人负责最终综合文件，不把“写总结”再次外包。
5. 对照交付合同逐项检查：
   - 必需文件存在且非空
   - JSON 字段齐全且能解析
   - 关键结论和未验证项都出现在最终回答
   - 测试命令及状态可追溯
6. 未通过交付门时继续修复，不得回复“等待中”“成员仍在运行”“稍后给你”。

## 标准最终 JSON

```json
{
  "agents_used": [],
  "findings": [{"claim": "", "evidence": [], "verified_by": []}],
  "cross_check": {"claim": "", "status": "confirmed|conflicted", "evidence": []},
  "tests_recommended": [],
  "unverified": [],
  "artifacts": []
}
```

## 最终回答

必须一次给齐：

- 产物绝对路径或可访问链接
- 三条最重要结论
- 实际使用的成员
- 运行过的验收
- 仍未验证项

没有最终产物就没有完成。
