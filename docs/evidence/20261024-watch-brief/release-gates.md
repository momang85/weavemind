# 发布门槛（TRIAL-READY G1–G6 + WATCH-BRIEF W 批增量）最终判定

- 日期：2026-10-24；执行人：lead（集成）+ pkg-hygiene（T0 G1–G4）+ research-materials（T1 G5–G6）
- 判定规则：**有实测反例 → 修复 → 重放同一输入确认 → 回归 exit 0**，才算通过；未验项单列，不用「应该没问题」充当通过。

## 1. TRIAL-READY 六条门槛

| 门槛 | 修复前（架构师探针） | 修复 | 修复后实测 | 判定 |
| --- | --- | --- | --- | --- |
| **G1** 同比跨年 | 2022 收入 100、2024 收入 150（缺 2023）→ `revenue_growth ok +50% pass` | `stock_screen.judge_growth()` 单点判据：只认严格相邻上一自然年；缺就 `missing` 并说明「不用更早年度顶替、不前值补齐」 | 同输入重放：`missing value=null insufficient=1`；正控 2023+2024 → `ok 50.0 pass` | **通过** |
| **G2** 可计算判据分歧 | 资产总计=0：预览说 `covered`、执行说 `insufficient` | `ComputableValue`/`judge_ratio()` 单一判据，`screen_prepare._field_status` 与执行读数共用；缺值/无效/unknown/冲突分类明确 | 资产=0 → 预览 `not_applicable`（分母为 0：不可算，不编造），执行同状态同原因；五字段逐条断言预览状态==执行状态 | **通过** |
| **G3** 预览跨数据集拼行 | 2023 在数据集 A、2024 在 B，绑定 B 预览却拼出 A 的 2023 → `ok`；执行 `insufficient` | `_resolve_preview_input()` 把预览绑到与执行同一份输入；同比上一年只从同一输入取 | 同构造：预览 `missing`（缺上一年度），绑定只认 B；两年并入一集 → 两边同时转可算 | **通过** |
| **G4** 缺币种默认 CNY | 源响应无 `CURRENCY` → 存 `currency="CNY"` 并硬算比率 | `adapters/eastmoney.py` 港股/A 股同规则：缺 → 空串 + `currency_source="unknown"` + note，声明的照抄 | 无 CURRENCY → 存 `""`、`revenue=123456789` 全精度；`debt_ratio unknown`（关键元数据未知）不硬算；`CURRENCY="HKD"` → 照抄 | **通过** |
| **G5** 来源布尔失真 | 调用方声明 `user_provided=true`，落库却为 false | `screen_store` 按 `provenance/origin/parent_task_id` 派生四个独立布尔；`research_material` 新来源类；旧数据集读侧回填规范化（不改冻结内容） | `test_screen_store`（71）含来源布尔/身份用例 + `test_trial_t1`（18）全绿；`dataset_source_meta` 返回 `origin_identity{kind,label,四布尔,legacy,mismatch}` | **通过** |
| **G6** 跨用户数据串味 | 别的用户 801 行挡住自己的旧任务（列举不隔离） | `research_materials._scoped_task_rows` 在 SQL 层按 owner 等值过滤后排序分页，返回 `counts` 审计数字 | `test_trial_t1` 覆盖本人可见/他人不可见/计数；`test_research_materials` 10 项全绿 | **通过** |

## 2. WATCH-BRIEF 增量门槛

| 门槛 | 验证方式 | 判定 |
| --- | --- | --- |
| 公司页三入口同一路由、寻址完整（code+run/dataset） | 浏览器三入口截图 + 接口冒烟（run/select_input/dataset/not_in_run/corrupt） | 通过 |
| 简报相对「上次记录」、不叫日报 | `title=本次记录相对上次记录`（接口 + UI）；基准显式设置 | 通过 |
| 零模型、零外网 | `test_watch_brief` 桩断言无 `urlopen`；`cost.model_calls=0`；简报本体不发请求 | 通过 |
| 基准并发保护 | `baseline_run` 纳入 watchlist rev；过期 `if_rev` → 409 且不写入（集成冒烟实测 409） | 通过 |
| 不可比不算变化 | 期间政策/规则/实现版本/来源变更进 `comparability.flags`；读数差异不冒充经营变化；`not_comparable` 明确 | 通过 |
| 旧结果不重写 | 简报幂等测试断言 run.json 字节不变；无自动重算 | 通过 |
| 合成/用户/官方来源不混措辞 | 前端来源标签与说明；三一口径差异继续标记不合算 | 通过 |

## 3. 分发条件

- 分发包：`tmp/watch-brief-package/weavemind-rc2-20261024T141228Z-cd6a388f.zip`，sha256 `657c0d36…b737`，`--verify` exit 0，365 文件，含 `watch_brief.py`、`CompanyPage` 构建产物；**这是唯一候选包**。
- 旧包 `tmp/rc2-package-final/weavemind-rc2-20261024T052644Z-2fa79a83.zip`（`d7aef516…`）已被本包取代，`tmp/rc-package-1024/weavemind-rc-20261024.zip` 继续保留 `DO-NOT-DISTRIBUTE.md`。
- 未 commit/未 push/未建分支；真实缓存、生产数据、真实密钥未动。
- 发布前仍需用户侧完成 `manual-acceptance.md` 的项；本报告不代替人工复核。
