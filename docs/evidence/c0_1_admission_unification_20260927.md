# C0-1 统一资料准入（09-27 架构行动）

日期：2026-09-27。批次：**C0-1**（指令 `docs/真实研究闭环与阶段D收口_20260927.md` §3.2、§4-C0.1）。
基线 `453f958`。本批把新（`disclosure_ingest`）旧（`narrative_evidence`）两条准入路径收敛到同一套
日期/主体/期间判据，并关掉 §3.2 列出的全部反例。

## 1. 先固化反例，再修（修前/修后对照）

| # | 反例（§3.2） | 修前 | 修后 |
|---|---|---|---|
| 1 | `2025-00-99` 当披露日 | 长度够、字符串比较"小于"截止日 → **准入** | 真实日历解析（`datetime.date`）→ 非法即无日期，`cutoff_unknown` |
| 2 | `2025/04/29` 对 2025-04-30 截止 | 分隔符不认 → 误判未知 | 斜杠/点/中文年月日都识别 → `2025-04-29` day 精度 |
| 3 | URL 任意参数 `?asof=2020-01-01` 当披露日 | 扫描整个 URL → **准入** | 只认**具名公告标识**（`art_code`/`notice_id` 等）与**日期形状路径段**；其它参数一律不作证据 |
| 4 | 路径里裸年份 `/report/2024` 当发布年 | 认作 year 精度并参与判断 | 不再接受裸年份（路径里的单一年份通常是**报告期**）；月精度如实记 `YYYY-MM`，**不补月初**（补零会被下游读成 day 精度，实测撞到过） |
| 5 | 标题"洋河股份2024年年度报告"、正文是**贵州茅台**年报 | 标题命中即 ok → **准入** | 主体判定改看**正文**（本主体名称或代码），正文出现别家主体 → `subject_mismatch` |
| 6 | 标题年报、正文 `Access denied. Request rejected.` | **准入** | 错误页标记 + 缺报告内容特征 → `error_page` / `not_report_body` |
| 7 | `cninfo-fake.org`、`eastmoney-fake.org`、`ir.evil.org` 拿权威标记 | 片段式匹配命中 | 权威清单改为**完整域**；命中 = 主机名等于它或是它的**子域**；`ir.`/`cninfo` 这类标签不再参与匹配 |
| 8 | 用户直链/第三方镜像被当官方 | 无来源类别 | 准入结果带 `source_class ∈ official_disclosure / third_party_mirror / user_file`——**不因标题是年报或用户直链升为官方** |

日期证据同时带 `basis`（`source_field:<key>` / `source_url_format`）与 `precision`（day/month）；
截至判断只接受 **day** 精度。三个时间（期末/公告日/获取日）继续分开保存（上一批已落）。

## 2. 代码位置

- `adapters/search_quality.py`：`AUTHORITY_OFFICIAL/GOOD/JUNK` 改为完整域；`_domain_hits` 只认
  完整域与受控子域（删掉上一版的 `_registrable_labels` 片段匹配）。
- `narrative_evidence.py`：`_published_at` 收窄为"公告/研报编号（`AN`/`AP`）→ 日期形状路径段
  （`YYYY-MM-DD`/`YYYYMMDD`/`YYYY-MM`）→ 具名参数"，不再扫整个 URL、不再认裸年份。
- `adapters/disclosure_ingest.py`：`_parse_calendar_date`（真实日历 + 精度）、`_declared_disclosure`
  （日期/精度/依据三元组）、`ingest` 的正文体检（错误页、报告特征）与正文主体判定、`source_class`。

## 3. 定向验证

```
python -m unittest test_search_quality_unified test_narrative_evidence test_delivery_chain \
                     test_facts test_working_paper test_p0
# 990 tests OK（含新增：权威域边界 22 例 + 片段条目守卫 1 例；摄取层 9 例；
#  narrative 精度用例改为"裸年份不再产出日期"）
```

被调整的既有断言（修复必须调整行为预期，不删断言不放宽）：
- `test_analysis...` 无；`test_narrative_evidence.test_month_or_year_precision_dates_are_not_admitted`：
  裸年份 URL 现在**不产出日期**（精度空），月份断言保留；
- 两处 fixture 的"晚于截止"用例把 URL 从 `/2027/outlook` 改为日期形状 `/2027-01-15/outlook`
  （保持"晚于截止必被排除"的覆盖，用可证明的日期）；
- `test_delivery_chain.test_cutoff_requires_day_precision`：改为断言 `/report/2024` 无日期证据、
  月精度同样不足以成立截至；
- `test_p0.test_no_snapshot_is_unknown_not_healthy`：补 mock 进程内回退，避免同批用例写脏
  `source_health` 进程内字典造成串扰（之前是顺序相关的偶发失败）。

## 4. 未验 / 未做（照实）

- 未做：旧在线链（`validate_record` 的**权威/发布日**判定）与新摄取层的**完全**同源——本批把两者
  的日期与权威判据对齐到同一规则，但主体判定的窗口不同（旧链 4000 字符、新链 6 万字符，后者
  是实测逼出来的：样本"洋河股份"首次出现在第 8123 字符）。彻底同源留到 C1 接线时一并收口。
- 未验：真实网页错误页样本（本批用的是构造反例 + 真实冻结样本正例）；真机第二家公司材料仍待取得
  （C1 项，见指令 §4-C1）。
- 未做：C0-2 根检索预算、C0-3 出口覆盖（按指令顺序followed在后续提交）。
