# 任务4 — acceptance_checker.py 报告体裁配置化

## 背景
acceptance_checker.py 目前完全硬编码，0 处配置文件读取（grep 证实）。
核心问题：
- _TRACEABILITY_THRESHOLDS 只有 domain 轴，没有体裁轴（年报/半年报/事件研究）
- freshness_block 要求"数据截止块+日期"，但事件研究（并购/回购公告）天然没有此块 → 误 fail
- 溯源率对半年报偏严（样本量天然少，total=4 且 rate=0.5 即 fail）

## config.json 新增块（在 acceptance_checker 读取前先加到 config.example.json）
```json
"acceptance": {
  "profiles": {
    "annual_report": {
      "traceability": { "financial": 0.7, "research": 0.5 },
      "freshness_required": true,
      "placeholder_max": 3
    },
    "semi_annual": {
      "traceability": { "financial": 0.55, "research": 0.4 },
      "freshness_required": true,
      "placeholder_max": 5
    },
    "event_study": {
      "traceability": { "financial": 0.5, "research": 0.3 },
      "freshness_required": false,
      "freshness_alternatives": ["公告日", "事件日", "披露日"],
      "placeholder_max": 5
    }
  },
  "default_profile": "annual_report"
}
```

## 实现步骤（最小改动原则）
1. run_acceptance 函数签名加参数：report_type: str = "annual_report"
2. 函数体顶部加载 config.json 里 acceptance.profiles[report_type]，
   若键不存在则用 default_profile 对应的值。
3. 将 check_number_traceability 调用处（生产路径）的 threshold 改为从 profile 读取，
   按 domain 映射（financial → profile.traceability.financial，等）。
4. freshness_block 判定改为：
   - 若 profile.freshness_required == False：
     检查文本是否含 freshness_alternatives 里的词，含则 pass，否则 hint-only（不 fail）
   - 若 True：维持原逻辑
5. deliverable_completeness 的 placeholder_max 改为读 profile.placeholder_max（原硬编码 3）。
6. bump ACCEPTANCE_RULES_VERSION 为 "2026.10.16"，更新 rules_fingerprint() 的基线。
7. delivery_pipeline.py 的 run_acceptance 调用处（L2085/2097）透传
   report_type（从任务元数据字段读取，字段名自行查找确认）。

## 验收
- 跑 test_p0 指纹基线测试，确认版本号和指纹更新正确（预期：测试要求更新基线后通过）。
- 构造一个 event_study 类型的最小报告（含"公告日"但无"数据截止"块），
  确认 freshness_block 不再 fail。
- 构造一个 semi_annual 报告，financial 域 total=4 rate=0.55（2.2/4），
  确认新阈值 0.55 下 pass（原 0.7 下 fail）。

## 约束
- 不改任何判定规则，只改阈值来源和 freshness 豁免逻辑。
- 保留 ACCEPTANCE_RULES_VERSION 版本指纹机制，这是有意设计。
- config 里的数值以本文件为准，不得自行调整。
