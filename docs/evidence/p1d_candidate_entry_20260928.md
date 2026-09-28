# P1-d 另一半：按新材料生成**候选正文**入口（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §5 第二条

> 在原任务提供"按新材料生成候选正文"的正常入口：展示材料/契约版本、影响步骤、现有预算
> 与授权；第一批幂等/恢复修完后接线。同一次动作只生成一个候选，只重做依赖新材料的步骤，
> 旧工件和人工文字保留；新候选需显式采纳，旧批准不继承。此处实现入口不等于授权本轮新增
> 付费生成，先用冻结输出/provider替身贯穿UI到执行处理器。

基线 `f7f80c8`。**未联网、未跑付费整链、未调用模型**：候选走**确定性装配**。

## 一、接缝（上一节已定位，本节接线）

补材料链此前**刻意停在正文之前**——`handle_add_material()` 最后一行原话是
「正文需按新材料重生成时**另行授权**」。本批把那句"另行授权"接上：
`POST /api/task/<id>/candidate` → `OrchestratorV2.handle_regenerate_candidate()`。

分工与补材料一致：**HTTP 入口只做鉴权/归属/运行态检查/投递**，装配与版本登记都在编排器里，
因此网页与本地调试走同一条装配路径。

## 二、六条要求逐条落实（每条都有用例）

| 要求 | 落实 | 用例 |
|---|---|---|
| ① 展示材料/契约版本、影响步骤、现有预算与授权 | 响应带 `material_id` / `material_fingerprint` / `contract_fingerprint` / `affected_steps` / `stopped_steps` / `budget` / `generation{mode,paid,model_called}` / `acceptance` | `…budget_and_generation_mode_are_reported` |
| ② 同一次动作只生成一个候选 | 幂等键 =（材料身份 + 契约指纹 + **当前采纳版本**）的 sha256，落 `candidate_state.json`；命中即复用并标 `created:false` | `…idempotent_per_material_and_contract`（并断言版本库**不多出**第二个） |
| ③ 只重做依赖新材料的步骤 | `affected_steps` 只含产出交付正文的能力（`report_generator`/`content_summary`），其余进 `stopped_steps`；检索/清洗步骤**不重做** | 同上（`aff=={s3}`、`stop=={s1,s2}`） |
| ④ 旧工件与人工文字保留 | 只 `VersionStore.record()`，**不** `adopt()`、不写交付投影、不覆盖 `reports/report.md`；候选正文由**当前采纳版的分析节**重新装配，人工写的那段仍在 | `…generated_but_not_adopted`（断言采纳指针未动 + `人工写下的分析段` 仍在候选里） |
| ⑤ 新候选需显式采纳、旧批准不继承 | 响应 `adopted:false` / `requires_explicit_adoption:true` / `old_approval_inherited:false` / `parent_version_id`=当前采纳版；验收是**对候选正文**跑的，自带结论 | 同上 |
| ⑥ 不新增付费生成 | 默认确定性装配（`model_called:false`）；请求带 `allow_paid` → **409 `paid_not_authorized`**，且**不投递、不落任何候选**（不悄悄降级成确定性装配） | `…paid_generation_is_refused_not_silently_downgraded` |

另外两条：没有已并入的新材料 → `no_new_material`（不凭空造候选）；
入口鉴权：别人的任务 403、终态任务 409、运行中 409（都不投递）。

**"已并入"的判据**（实测修正）：`material_intake.read_index()` 的条目**不带 `refresh`**
（`_entry_of` 只放不随解析变化的身份与结论摘要），所以逐条回读该材料的 meta 才算数：
准入通过 **且** 已并入快照 **且** `refresh.ok`。否则"有材料记录"会被误当成"有新材料"。

## 三、顺带修掉一处"标签说谎"（同族问题）

接线过程中在候选正文里看到：简报表头写着「**单位：吨**」，而全文金额都是亿元。

根因在 `working_paper_export`：表头单位取"第一行有单位的"——

```python
for r in rows:
    u = str(r.get("unit") or "").strip()
    if u: unit = u; break          # 底稿里先出现的是销售量（吨）行
```

底稿里同时有实物量（吨）、单价（元/吨）与金额行，谁先出现就定了全表单位。
**数字没错、标签错**，读者据此换算全错。修法：只认**金额单位**（精确匹配，`元/吨` 不算），
优先按**契约必需指标**的金额行判；判不出来就留空，正文照实写"见表中标注"——不猜。

用例 `test_brief_header_unit_is_the_amount_unit_not_a_tonnage_row` 先用**真的会触发它**的
资料面（补材料带来的量价/结构事实 + 结构化财务）断言"底稿里确实有吨行"，
再断言表头是「单位：亿元」。

## 四、测试（7 条，全部离线，走同一条生产函数）

装置复用既有补材料夹具：隔离工作区 + 任务记录 + `_LoopRedis`（复刻编排器
`regenerate_candidate` 分支：处理 → 写收执键）+ **未绑定**的
`OrchestratorV2.handle_regenerate_candidate`。**被测的是生产里那个函数**，
只有运行环境（Redis/模型/budget）是替身；HTTP 入口走 `web_ui._post_task_candidate`
本身，因此"UI → 执行处理器"是**贯穿**的，不是跳过入口直接调 helper。

夹具里补了**结构化财务**（两期亿元）：底稿没有它就不产出——`write_working_paper`
明确"不编一份空底稿"。补上之后"补材料 → refresh → 底稿 → 装配候选"这条链在离线用例里
也是完整的，而不是靠跳过环节凑出成功。

## 五、未做与边界（如实列出）

1. **页面按钮未加**：前端是 React/TS + 入库的 `dist`，加按钮要改组件并重建 bundle；
   本批只做到 **HTTP 入口**（页面调用的就是它）。**没有**声称"页面已经能点"。
2. **模型重生成未接线**：`allow_paid` 明确 409。接它要先有本轮的付费授权与预算口径，
   不在本批。
3. **候选的采纳动作**沿用既有 `VersionStore.adopt()`（`/review/edit` 那条路径已有的地基），
   本批没有新增采纳端点。
4. 候选生成**不写交付投影**，所以页面的"当前交付"在采纳前仍是旧版本——这是刻意的。

## 六、口径声明

全部验证离线完成（替身 + 冻结公告文本 + 两期结构化财务），**未对任何真实站点发起请求**、
未消耗额度、未调用模型；未改门禁/阈值/模型/权限/模板；未删除任何日志、备份或产物。
