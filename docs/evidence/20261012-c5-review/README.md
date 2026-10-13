# 10-12 C5：经营判断 × 市场反应（内部研究通路）

当前执行以总架构师指令及三域规划 §18.19 为准；本目录**不是新规划**，只放本轮 C5 的实现与可复核证据。

## 一、C5 四项的落点

| 项 | 落点 | 证据 |
|---|---|---|
| C5-1 无主体 options 只返登记主体 | `web_ui.py` `_post_research_study_experiment`：study/hash 校验后，**无 entity 的 `options`** 返 `{entities…, entity_required:true, study{…}}`，**不**默认第一主体、**不**扫材料、**不**求值、**不**建候选工作区；`evaluate`/`save` 仍要求 entity | `c5_entry_probe.py`（10/10） |
| C5-2 经营判断 × 市场反应（主产物） | `financial_analysis/research_experiment.py`：`market_comparison(…)` 复用 `event_returns`/`event_calendar`，三窗口 1/5/20 交易日＝偏移 `0/4/19`，股票/基准/**几何相对**并列经营支持/削弱与下一观察；生产从数据源算，不写死 | `c5_market_probe.py`（63/63）、`c5_update_probe.py`（39/39） |
| C5-3 沿现 `experiments` 冻结原判 | `baseline_experiment_id` 显式更新：较早侧读数/判据**照抄冻结 JSON**（`baseline_kind`/`baseline_experiment_id`/`baseline_criteria_sha256`），不现场重建旧判断；无原版时显式标「历史重建」 | `c5_update_probe.py`：新实验 `80693f9870e8337f394fcf49`，5/5 判据 sha256 与基线相同 |
| C5-4 一次解析 ＋ 实际内部导出 ＋ 许可门 | adapter 请求带显式 `experiment_id` → 只解析 `experiments/<id>.json` 一次贯穿正文/附件/视图；`delivery_pipeline._market_delivery_blocked` 把带**受限市场读数**的实验成员按门移出包外并留说明 | `c5_export_probe.py`（28/28） |

## 二、怎么复核（全部离线、无外部额度）

```powershell
python -X utf8 docs/evidence/20261012-c5-review/c5_entry_probe.py    # C5-1 入口（10/10）
python -X utf8 docs/evidence/20261012-c5-review/c5_market_probe.py   # C5-2 三窗口（63/63）
python -X utf8 docs/evidence/20261012-c5-review/c5_update_probe.py   # C5-3 沿冻结原判（39/39）
python -X utf8 docs/evidence/20261012-c5-review/c5_export_probe.py   # C5-4 两次真实导出（28/28）
python -X utf8 docs/evidence/20261012-c5-review/c5_frozen_guard.py   # 旧基线只读守护（verdict 全 true）
python -X utf8 -m unittest test_quant_publish                        # 许可门未回归（21 tests OK）
```

探针**幂等**：`c5_export_probe.py` 先按内容身份在候选工作区找既有真实包（C4 冻结实验＝包内含完整 JSON；C5 新实验＝包内**无** JSON 且 `manifest.external_delivery.withheld` 记下同 sha256），找到就复核、不再出包；只有没有同身份包时才现出一次。

行情缓存是**本机临时目录**（`%TEMP%\agent_workspace\market_history`）里的免费源数据，可能被系统清理：清理后 `c5_market_probe.py`/`c5_update_probe.py` 会**如实停在 exit 2**（「环境缺件」，不覆盖已记录读数、不拿别的数据集顶替）；恢复＝重新导入行情集（`scripts/fetch_market_history.py`，外部抓取）后重跑。10-13 实测：缓存被清理，两个探针按上述行为停下；已记录读数（10-12 的 63/63、39/39）未动。

## 三、两次真实内部导出（同一候选工作区）

- C4 冻结实验 `245bcd5e56ef7c5eafaf11e2` → `deliverables_20261012_115504_387b7f.zip`：包内 `analysis/c3_experiment_002304.SZ.json` **与冻结 JSON 逐字节相同**（= C4 期批评的「没有真实新 ZIP」在 C5 显式选版后落地）。
- C5 新实验 `80693f9870e8337f394fcf49` → `deliverables_20261012_115505_6b1386.zip`：该 JSON **按许可门移出包外**，包内留 `quant/EXTERNAL_DELIVERY_RESTRICTION.md`（记被移出成员与其 sha256），`PACKAGE_MANIFEST.json` 的 `external_delivery.withheld` 逐条可核；工作区 staging 的同一 JSON 与冻结产物逐字节相同（**内部保存成立**，受限只针对对外包面）。
- 两包正文 `reports/report.md` 的字节哈希＝manifest `delivered_md_sha256`，并经 `report_versions.json`（交付记录 → 该版机器验收 `pass`）自洽核对。

## 四、如实说明（不夸大成"通过"）

1. **C4 期旧包本来就不含实验 JSON 成员**：11 个 C4 期包里 `analysis/c3_experiment_002304.SZ.json` 一律缺席（只有 `.md`）——这正是 C4「没有真实新 ZIP」的实情；C5 起**显式选版**才把完整 JSON 带进包（或按许可门移出）。旧包**不动**（`c5_frozen_guard.json` 记其成员与哈希）。
2. **行情数据集 id 自 C2b 起就是正文的出处标识**（数据来源声明，不是读数），故它不是 C5 许可门要挡的受限读数；`c5_export_probe.py` 的 `_leak()` 只查市场**读数**特征（块名/窗口标签/窗口数值）。
3. **正文里的价格窗表是 C2b 既有段**（带自己的「许可边界…仅内部试验」声明，C3/C4 已受理的版面）；C5 **不重写已验收正文**，只保证**不另开第二条未设门的路**：`market_comparison` 块不进正文、不进附件 md、不进 `analysis/context.json`。
4. **候选导出会堆包**是既有行为（C4 期同样连出 5 个），本轮未删任何包；`c5_git_status.txt` 与 `c5_frozen_guard.json` 保留现场供复核。
5. **C5 判据修订后，旧产物的只读重放会有意出现差异**（现金卡删去未执行的「ΔOCF 转正负」文案、库存口径改为「本窗口停止累积 vs 两窗口持续确认」分开叙述）：旧冻结 JSON **未被改写**（`8176dca4…` 不变），差异只在"用新口径重放旧输入"时可见。
6. **GUI 实机条件**：用户真实浏览器里 `127.0.0.1:8091` 仍是**登录页**（`c5_gui_8091_login.png`；DOM 探测 `hasPassword=true, hasPanel=false`）——本轮重启只加载新代码，**不代填口令、不索要口令、不借他人会话**。已在该浏览器打开该页（标签组「C5 研究实验台复核」），登录后即可按 ⑧「取研究主体」→ 选主体/run →（可选）市场对照 → 求值/保存走闭环。

## 五、本轮改动的实现面（纯加法）

`web_ui.py`、`financial_analysis/research_experiment.py`、`financial_analysis/continuation.py`、`delivery_pipeline.py`、`research_report_adapter.py`、`frontend/src/pages/ResearchStudy.tsx`（⑧ 区新增「市场对照」「沿冻结原判/历史重建」选版与结果段），前端重建产物 `frontend/dist`（`index.html` 改指新 bundle，新增 `assets/ResearchStudy-BMnOnlmw.js` 等；旧 tracked 资产按原样保留）。未新增服务/worker/DB/DSL/框架，未改模型权限、密钥、鉴权，未对外交付、未跑全量 CI 与旧套件（除 `test_quant_publish` 这一处回归）。

## 六、文件清单

- `c5_entry_probe.py` / `c5_entry_results.json`：C5-1（含一次 8091 环回 401 认证探测）
- `c5_market_probe.py` / `c5_market_results.json`：C5-2 三窗口（参考值取自 C4 受理证据）
- `c5_update_probe.py` / `c5_update_results.json`：C5-2/C5-3 端到端只读（产出新实验 `80693f…`）
- `c5_export_probe.py` / `c5_export_results.json`：C5-4 两次真实内部导出＋许可门
- `c5_frozen_guard.py` / `c5_frozen_guard.json`：C4 基线/旧包/受保护文件哈希守护
- `c5_git_status.txt`、`c5_head.txt`（`c85371e`）、`c5_gui_8091_login.png`：现场与 GUI 条件
