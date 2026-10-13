# C4 复核证据包 — 普通研究页「选研究 → 挑战判断 → 新披露更新 → 保存行动」

- 指令：`docs/evidence/20261011-c3-accept/dsh-c4-instruction.txt`（sha256 `47ac7dec…`）
- 上位：C3 有限通过（`docs/evidence/20261011-b4-c3-review/`），用户实验台未完成 → 进入 C4
- 本包只证 **C4 新通路**：不重跑 37/28/13/57/258 旧套件与全仓 CI 付费链，不重复 C3/C2e 已验收内容
- 结论：**31/31 通过**（`results.json`）；三个补充探针各自 ALL PASS；新前端产物已由本地服务按新哈希提供

## 做了什么（C4-1…C4-4）

| 项 | 落地 |
|---|---|
| C4-1 装配提成公开函数 | 新 `financial_analysis/research_experiment.py`（1193 行）：`experiment_options / prepare_company_research_experiment / experiment_summary_markdown / save_experiment / load_experiment / list_experiments / list_candidate_runs / find_registered_material / mechanism_criteria / experiment_identity`。输入显式 `study/result/entity/run/view/metric/fractions` ＋登记材料；数值走 `reversion_ladder`（同一实现），更新走 `continuation`；**无固定 sid／run 前缀／用户名 Temp 路径**——候选工作区一律 `from workspace import task_workspace` 现场解析 |
| C4-2 窄 POST ＋ 实验区 | `web_ui.py` 新增 `POST /api/research-study/experiment`（`action=options/evaluate/save/open`，107 行纯加法）；`frontend/src/pages/ResearchStudy.tsx` 新增实验区：主体选择、候选 run（逐条视角可用性）、视角／候选项、0/50/100% 结果与反转条件、旧机制快照 × 新披露更新、保存与重开。浏览器**只传 id/hash**，不传数字与任意路径；主 run 或续页 `cash_run_ref` 由后端给出 |
| C4-3 必要接缝 | 完整实验 JSON **同版真实入包**：`research_report_adapter._c3_resolve` 先取 `experiments/*.json` 再回落旧 `c3_experiment_<code>.json`；`_stage_candidate` 逐字节 `copyfile` 成 `analysis/c3_experiment_<code>.json`；`delivery_pipeline` 按名收录该 glob；`exact_values_in` 指向真实成员。原研究／`result_hash` **不回写**；新实验独立内容身份（`experiment_id`＝正文哈希前 24 位）可冻结可重开；旧机制依据绑定旧材料与快照 hash，父报告只作技术来源 |
| C4-4 判据与一页摘要 | 判据定义一次（文案与求值同源，`criterion_sha256`）：三一原主导项＝营运资本项，故不能写「利润仍主导」；洋河原现金已为负，`ΔOCF<0` 不等于新削弱；库存**单窗口削弱**与**两窗口确认**分开成字段。摘要 PDF **实页数 == 1**（`report_pdf._page_contents` 实测） |

## 复核怎么跑

```powershell
python -X utf8 docs/evidence/20261011-c4-review/c4_review.py     # 31 项，写 results.json
python -X utf8 docs/evidence/20261011-c4-review/c4_probe2.py     # 装配冒烟（值/卡/保存重开/一页）
python -X utf8 docs/evidence/20261011-c4-review/c4_api_probe.py  # 窄 POST 组件级 4 正例 + 12 反例
python -X utf8 docs/evidence/20261011-c4-review/c4_seam_probe.py # 接缝 14 项
python -X utf8 scripts/c3_experiment.py                          # 只读重放：冻结实验与 C3 基线逐值重放
```

`c4_review.py` 覆盖：A 四格新通路值 == C2e 精确参考（三一同比 `0:91.060580000 / 0.5:32.9946550000 / 1:-25.071270000` 亿元、λ* `0.7841137465733991148853652120`、可达 true；洋河本期 `-7.6304032679 / 15.16157821075 / 37.9535596894`、λ* `0.1673922751088502363681697682`、true；洋河同比 `-53.9175156407 / -31.54017334815 / -9.1628310556`、λ* `1.204734568918189741345669703`、false）；B 判据单一定义；C 冻结／幂等／一页；D 接缝入包；E 另一 run／另一候选 id 无需改代码；F 守卫（未知视角不造数、身份不符不装配）。

`scripts/c3_experiment.py` 为只读重放器：逐研究逐主体，用**冻结产物自己记录的输入**（run／视角／候选项／旧新材料／技术来源）重放，比对数值层（C、a、三项元串按 `Decimal` 归一化、λ*、可达性）与内容身份；`--write` 仅在身份完全一致时幂等重冻结，**永不**写 `c3_experiment_<code>.json`，也不为校验凭空多造产物。

## 服务级冒烟（本地空闲端口，未动 8080）

`WEB_PORT=8091 BIND_HOST=127.0.0.1 python -X utf8 web_ui.py`（临时起、验完即停）：

- `GET /` 200，571B，与 `frontend/dist/index.html` **逐字节相同**，引用 `assets/index-DTzMwCGp.js` / `assets/index-CdohwtMb.css`，二者取回与磁盘一致（214424B / 43978B）
- `GET /assets/ResearchStudy-DhGqIhJq.js` 200（63999B），内含新实验区文案（`算这个格子`／`披露更新`／`已保存实验`／`反转条件`／`挑战判断`／`保存行动`）与 `"/api/research-study/experiment"`（4 处）
- `GET /api/research-study/studies`、`POST /api/research-study/experiment`（无会话）→ **401**「未登录或登录已过期」，鉴权边界保持

## 变更归属（免得复核者被哈希差惊到）

`review_fingerprints.json` 记录的是**当前**工作区哈希；与 C3 包 `…-b4-c3-review/review_fingerprints.json` 的 `unchanged_verified` 对照：

- `acceptance_checker.py` 与 C3 记录**逐字节一致**；本 C4 会话未改任何受保护文件。
- `docs/总架构师执行指令.md`（10-11 19:23）、`docs/架构监督记录.md`（19:25）与 C3 记录不同，但改动时间在 **C3→C4 交接**（写入 C4 指令／登记 C3 接受结论）时，早于本 C4 实现（`research_experiment.py` 等）。
- `financial_analysis/continuation.py`（26/14）在 C4 前段随 C4-1 的「更新复用 continuation」集成点改动，属本批实现面。
- `web_ui.py`（107/0）、`delivery_pipeline.py`（11/0）、`frontend/src/pages/ResearchStudy.tsx`（678/53，含 C3 遗留）、`docs/DeepSeek执行状态.md`（372/1）为本批允许的加法改动；`frontend/dist/*` 因页面变更**必须重建**（新哈希 `ResearchStudy-DhGqIhJq.js` 等 3 项已记入指纹）。

## 边界与未验项（如实的恢复条件）

1. **GUI 实机点击复核未做**：`do_POST` 除登录/登出/初始化外一律要求管理员会话，`config.json` 无固定默认口令，需用户本地输入口令登录。恢复条件：用户给出登录口令或已登录浏览器；届时按 C4-2 清单点一遍（选研究→选主体/run→选视角/候选项→看结果与反转条件→选旧快照与新披露→更新→保存→重开→下载 JSON/摘要）。
   - 组件级 POST 已单独探过（4 正例 + 12 反例），**不拿它冒充 GUI**。
2. 未新增报告交付包、离线样品或模型（C4 指令要求）；C3 已验收两包与 C2e/C2c 基线包 sha256 原样不变（见 `review_fingerprints.json`）。
3. 未重跑旧套件／全仓 CI／CMB 付费链，未增服务、DB、worker、DSL，未重构巨文件。

## 文件清单

- `c4_review.py` / `results.json`：31 项复核与结果
- `c4_probe2.py` / `c4_api_probe.py` / `c4_seam_probe.py`：装配冒烟 / 窄 POST 组件级 / 接缝探针
- `review_fingerprints.json`：HEAD、实现文件 sha256 与 diff 行数、研究目录关键文件、四个交付包 sha256、受保护文件 hash
- `_gen_fingerprints.py`：`review_fingerprints.json` 的生成脚本（本身不计入指纹）
