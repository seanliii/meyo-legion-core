---
name: delivery-design
description: 先设计用户最有用的交付组合，再调用专业工具制作富媒体、可下载、可验证的最终交付包。
---

# 交付设计

首脑不是把成员文字拼起来，而是负责把结果做成用户真正能使用的成品。

## 先选交付载体

按任务目标选择最少但完整的组合：

- 快速决策/审计：`summary.md` + 可机器读取的 `report.json`
- 数据比较：再加 `comparison.csv` 或 `.xlsx` 与一张图表
- 产品/方案展示：再加自包含 `report.html`，含目录、图表、证据链接
- 正式汇报：再加 PDF；只有用户需要演示时才做幻灯片
- 视觉创意：图片 moodboard / SVG 图解；需要动态表达才做视频
- 音频场景：文字稿 + 音频，不交只有音频的黑盒结果

不要为了“丰富”制造无价值格式；每个载体必须解决一个使用场景。

## 工具选择

1. 先用 `list_skills` / `list_mcp_tools` / 军团工具箱查询真实可用工具。
2. PDF 优先使用 make-pdf/pdf skill；HTML 使用 design-html；图表优先生成 SVG/HTML；视频使用已注册视频工具。
3. 工具不可用时降级到 HTML/Markdown/CSV，不得伪造生成成功。
4. 每个专业 worker 的任务包写明最终文件路径、格式、尺寸/编码和验收方式。

## 产物质量

- HTML 必须自包含、可直接打开、移动端可读。
- JSON 必须能解析，字段稳定。
- CSV/XLSX 列名清楚、单位明确、无混合类型。
- PDF 无截断、字体可读、链接有效。
- 图片有 alt/说明；视频有标题、时长、编码和封面。
- 所有结论都能追溯到 evidence。

## 最终交付包

至少包含：

```json
{
  "manifest_version": 1,
  "title": "",
  "artifacts": [{"path": "", "mime": "", "purpose": "", "verified": true}],
  "findings": [],
  "evidence": [],
  "unverified": [],
  "verification": []
}
```

首脑必须逐个检查文件存在、非空、格式可打开，并在最终回答中解释每个产物该怎么用。
