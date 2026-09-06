# 溯源体检 API（POST /api/verify）

**定位**：把织光的确定性验收器独立成"报告体检"能力——给自媒体、内容团队、外部 AI 应用做数字溯源审查。这是商业化路线报告中的②号动作。

## 请求

```
POST /api/verify
Authorization: Bearer <session-token>（仅 admin）
Content-Type: application/json

{
  "report_text": "……待体检的报告 Markdown/纯文本，至少 50 字符……",
  "task_id": "可选。传入本系统任务 id 时自动收集该任务工作区的检索/快照/结构化数据作为溯源底库",
  "goal": "可选。用于判定溯源域（financial/crypto/macro/research）"
}
```

## 响应

```json
{
  "ok": true,
  "domain": "financial",
  "numbers": {
    "total": 27,                 // 检出数字总数
    "cited": 14,                  // 引用值（可溯源到来源）
    "computed": 1,                // 计算值（可由来源数据算术验证：均值/求和/占比）
    "disclosed_model_knowledge": 2,  // 已标注"基于模型知识"
    "untraced": 10,               // 不可溯源（真实缺口）
    "coverage_ratio": 0.519
  },
  "source_labeling": {
    "pass": true,
    "checked": 6,
    "mislabeled": [],             // 疑似虚假来源标注
    "suggestions": []             // 建议补录的域名媒体映射
  },
  "disclaimer": { "present": true },
  "gaps": [],
  "summary": "数字 27 个：引用 14 / 计算 1 / 模型知识 2 / 不可溯源 10"
}
```

## 计费接入点

`web_ui._post_verify` 内已预留注释位——按 report_text 长度计费（如每千字 ¥0.1-0.5），
接入计费系统时只需在该函数入口加一条额度校验。

## 示例

```bash
curl -X POST http://localhost:8080/api/verify \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"report_text": "贵州茅台2025年营收1720.54亿元…（数据来源：东方财富数据中心）"}'
```
