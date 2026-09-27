# C3 收口：新人可行动状态统一（2026-09-27，Harness 接续）

批次：**C3 收口**（`docs/真实研究闭环与阶段D收口_20260927.md` §4-C3；指令 §2-H3）。
本文件是 C3 的收口记录：把"任务状态 / 收执 / 依赖健康 / 待材料"四处信息收敛成
**一份状态 + 一句人话 + 一个入口**，并逐条对照 C3 的四项验收。

## 1. 反例 → 修法

| # | 反例 | 修前 | 修后 |
|---|---|---|---|
| 1 | 新人看得出"没成功"，看不出"该做什么" | `status`/`phase` 是给机器看的词；收执、待材料、依赖健康分散在四处 | `classify_task()` 译成六个**可行动状态**，每个状态配**唯一**推荐入口 |
| 2 | 认不出的状态被当成"在跑"，用户一直等 | 前端把非终态一律当进行中 | 认不出的状态按**未接收**处理并明说"无法识别"，不许猜 |
| 3 | "未知/无快照/别的实例"显示成绿色 | `/api/status` 并列 `dependencies` 与 `source_health` 两份视图 | `unified_health()`：**唯一一份**四态视图，`ok` 要求全部 available 且至少一条；空视图、未知、过期、异实例一律**不算绿** |
| 4 | 失败只给"失败"，不说去哪修 | 无 | 失败时优先指向**能修的那个原因**：模型未配置 → 去设置；资料源不可用 → 健康页；都没有 → 看详情 |
| 5 | 同一件事两处显示（源熔断） | `dependencies` 与 `source_health` 各说一遍 | 旧 `source_health` 只在依赖快照**未覆盖**该源时补条目，并标 `legacy` 便于日后删除 |
| 6 | 已完成被读成"研究通过" | 进度条一绿到底 | 已完成状态的消息固定提醒"**机器验收 ≠ 研究通过，请人工复核**" |

## 2. 代码位置

- `actionable_state.py`（新，纯函数）：六个任务状态 + 七个行动入口 + `classify_task()` /
  `health_causes()` / `unified_health()`；**不写库、不发请求、不参与验收判定**。
- `web_ui.py`：`/api/status` 增加唯一 `health` 字段（`dependencies`/`source_health` 保留为
  兼容别名并注明）；`/api/task/<id>` 增加 `actionable` 字段；新增轻量端点
  `GET /api/task/<id>/actionable`。
- 前端：`frontend/src/lib/actionable.ts`（动作名 → 页面意图，认不出不给按钮）+
  `TaskConsole` 顶部提示条（配色：只有 `done` 是绿，`waiting_material`/`pending_consume`
  是黄，`failed` 是红，其余中性）；`frontend/dist` 干净重建。
- `test_actionable_state.py`（新，31 例）进 CI：**48 步 == 48 个测试文件**，双向差集为空。

## 3. C3 四项验收逐条对照

| 验收要求 | 现状 | 证据 |
|---|---|---|
| **重复提交同幂等键只产生一个任务和一次实际执行** | ✅ 代码 + 单测：web_ui 发布前查一次、编排器再查一次；命中则**不登记不执行**，收执 `accepted:dedup:<id>`，调用方据 `_effective_task_id` 跳过 | `test_task_persistence.TestSubmitIdempotencyAndTimeline`（6 例）、`test_writer_consolidation` 幂等 4 例 |
| **接收后进程重启可查到收执并恢复** | ✅ 代码 + 单测：收执（`RECEIVED`）+ 幂等键 + 时间线随任务行持久化；`resume_received_tasks(older_than=10)` 在订阅前原子推进（`promote_received`）后执行，**只执行一次** | `test_task_persistence.TestReceiptRecovery`（8 例，含 "恢复一次后 `n2 == 0`"） |
| **缺材料后补入能继续且不重复已完成的外部调用** | 🟡 C1 已实现"只重做依赖该材料的环节、原件已在盘的不重复发请求"，并有 `material_pending_tasks` 局部恢复；本批把它纳入可行动状态（`waiting_material` → 去补材料）。**实机链路仍未复验**（不登录、不伪造会话） | C1 证据 `c1_material_entry_20260927.md`；本批 `classify_task(material_pending=True)` 用例 |
| **两个实例同 Redis 不会重复处理** | 🟡 启动闸门（`claim_orchestrator_ownership`：已有活实例则拒绝启动）+ 执行权单一裁决（`promote_received` 原子 UPDATE）都有单测；**未**用真实双进程 + 真实 Redis 复验（本机 Redis 处于 MISCONF 拒写，见 H1 证据 §4/§5） | `test_startup_readiness.TestOrchestratorOwnership`（5 例）、`TestReceiptRecovery.test_promote_is_the_single_execution_right_arbiter` |

## 4. 验证（本会话实测，CI 口径逐文件）

| 套件 | 结果 |
|---|---|
| `test_actionable_state`（新） | **31 OK**（六态映射、原因排序、未知不算绿、legacy 折叠、端点四例） |
| `test_task_state` | **11 OK**（列集断言随新增三列更新：幂等键/时间线/归属） |
| `test_p0` | **412 OK**（含 `/api/status` 既有断言） |
| `test_startup_readiness` | **57 OK** |
| `test_settings_requirements` / `test_deploy_manifest` / `test_task_projection` / `test_metrics_scope` | 26 / 30 / 18 / 7 OK |
| 前端 `node --test` | **60 例全过**（新增 `actionable.test.mjs` 9 例） |
| `tsc -b` / `test_frontend_guards` | 通过 / **53 OK** |

## 5. 未验 / 未做（照实）

1. **真实双进程 + 真实 Redis**：启动闸门与执行权裁决只有替身单测。本机 Redis 因 `dir`
   配置错误处于 MISCONF 拒写（H1 证据 §5 已记），沙箱早前也起不了私有 Redis。
2. **真实 UI 端到端**：未在浏览器里演练"同键重复提交只跑一次""提交后杀编排器再启动自动
   恢复""等材料 → 补材料 → 继续"（本会话不登录、不伪造会话）。
3. **页面状态表尚未合并 health 与收执**：本批把两者都归到可行动状态与 `unified_health`，
   但**健康页本身**仍按原样展示 `dependencies`；页面上"收执时间线"也没有可视化
   （数据已在任务行的 `submit_timeline_json` 与 `/api/task/<id>/actionable` 里）。
4. **C4 全部未开始**：只重建一次便携包、同包真实材料/同版交付、当前版本评分交接、
   第二家公司原文、真人每份 ≥8/10。
5. 第 3 节表格里两句 🟡（缺材料实机、双实例真机）——**不以单测替代实机**，
   按 C3 的原文要求保留为未验。
