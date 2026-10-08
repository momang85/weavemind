# 待人工确认的假说卡（研究草稿）

- 状态：**draft_not_human_confirmed**（未经人工确认 ⇒ 不是事前假说）
- 研究ID：`f6469defdb1eac21298ac2e360c76beec70d8b45a8c4d64ccd3faac362378828`；模拟数据：是
- 边界：10 天压力是设定情景，不当历史信号；不含综合信用分、不含 alpha 声明

## 900001.SH

- 经营改善方向：现金转化<100%（利润未完全转成现金）
- 利润/现金/周转解释：cashflow_coverage_pct=62.5；cfo_minus_profit=-600000.0；receivables_to_revenue=0.15；inventory_to_cost=0.2857142857142857；liabilities_to_assets=0.5416666666666666；margin_gap_to_threshold_pp=-0.8333
- 设定情景（非历史信号）：额外占用 558904.11 元
- 原始来源：calendar=calendar.csv（sha256 5d26fe75879ea270…）；credit_labels=credit_labels.json（sha256 3f49c38131e85abd…）；events=news.json（sha256 8e47131b7ed6bc73…）；financial=financials.csv（sha256 6a6e785d80b2351f…）；fraud_labels=fraud_labels.json（sha256 93b4be186c2486a1…）；market=daily_prices.csv（sha256 8f720e3c000fc007…）
- 可知时点：特征截止 2025-04-30；结果评价截止 2025-06-30；材料最晚可用时间 2025-04-26
- 下一同口径披露：下一期同口径定期报告（同主体/同合并范围/同科目口径）；现金流量表补充资料（经营现金/合并净利润与营运资本三行）；若为真实材料：下一期行情（用于事后反应观察，不作为特征）
- 反证条件：方向反转：下一期同口径现金转化回到 100% 以下且净利同比转负；周转恶化证据：应收增长继续快于收入增长、或库存增长继续快于成本增长；来源/时点问题：可用时间晚于决策时点，或披露被更正（版本可用时间晚于决策）
- 核查线索 receivables_outpace_revenue（0.6）：
- 核查线索 inventory_outpaces_cost（0.5142857142857142）：

## 900002.SH

- 经营改善方向：现金转化<100%（利润未完全转成现金）
- 利润/现金/周转解释：cashflow_coverage_pct=96.0；cfo_minus_profit=-100000.0；receivables_to_revenue=0.21818181818181817；inventory_to_cost=0.23076923076923078；liabilities_to_assets=0.6136363636363636；margin_gap_to_threshold_pp=-3.1818
- 设定情景（非历史信号）：额外占用 994520.55 元
- 原始来源：calendar=calendar.csv（sha256 5d26fe75879ea270…）；credit_labels=credit_labels.json（sha256 3f49c38131e85abd…）；events=news.json（sha256 8e47131b7ed6bc73…）；financial=financials.csv（sha256 6a6e785d80b2351f…）；fraud_labels=fraud_labels.json（sha256 93b4be186c2486a1…）；market=daily_prices.csv（sha256 8f720e3c000fc007…）
- 可知时点：特征截止 2025-04-30；结果评价截止 2025-06-30；材料最晚可用时间 2025-04-26
- 下一同口径披露：下一期同口径定期报告（同主体/同合并范围/同科目口径）；现金流量表补充资料（经营现金/合并净利润与营运资本三行）；若为真实材料：下一期行情（用于事后反应观察，不作为特征）
- 反证条件：方向反转：下一期同口径现金转化回到 100% 以下且净利同比转负；周转恶化证据：应收增长继续快于收入增长、或库存增长继续快于成本增长；来源/时点问题：可用时间晚于决策时点，或披露被更正（版本可用时间晚于决策）
- 核查线索 inventory_outpaces_cost（0.15714285714285714）：

- 人工确认方式：人工逐条确认方向/解释/来源/可知时点与反证条件后，才可把本卡称为「事前假说」；确认动作与时间另行记录，本入口不代签
- 卡hash：`8dfcab6b0d40928a040942e9ef7430eba6d0a64854b0fe527d4d7ef49bcf6dc6`
