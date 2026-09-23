# R2 证据（09-23 晚间批次）：把修改与导出做成可靠的版本操作

**指令**：`docs/阶段D系统收敛与研究效能提升_20260923.md` §5（R2）
**范围**：无新增 HTTP、无付费模型调用；受控交错与离线渲染探针代替并发实机（按指令
"先离线调度屏障，不需付费任务才能测并发"）。

## 1. 受保护读取：交付正文必须能证明属于**这一版**（根因三残留）

复核探针（`snapshot_probe_98be0e2.py`）复现两条：① 读交付 A 后切采纳 B，仍
HTTP200/verify_ok=true 而**包内正文 A、身份 B**；② 快照 A 后仅替换图表/底稿/证据为 B，
仍全部自检 ok 而证据指纹 A、文件 B。

**修复**（`delivery_pipeline.export_snapshot`）：正文与采纳版本在同一受保护步骤里确定，
并用**两条证据**证明归属，任一成立即可：

1. **版本库交付登记**：装配时写下的 `deliveries[]`（`delivered_sha256` +
   `report_version_id` + `aligned`）；登记与当前身份一致但 hash 不符 → `version changed`；
2. **生产渲染核对**：研究任务用 `research_candidate_body` 把采纳正文重渲一次，
   交付正文必须包住它（外壳允许不同字节）。

两条都不成立 → **fail closed**：`binding unverified`，调用方 409（页面提示"请先重新装配/
重验该版本"）。未知依赖不再伪装成已验证：清单新增 `binding_verified` 与 `delivered_sha256`。
`repack_adopted(snapshot=…)` 发布前**再核一次** `body_sha256`（身份与正文文本同一版）。

实机（`ui-706c5ef4a5`）：`binding_verified=True`、`body_text_sha256=6f2c004d…`、
`delivered_sha256=3d357590…`（与页面下载、包内 `reports/report.md` 一致）。

受控反例（`test_delivery_chain.TestSameVersionDeliveryChain`）：
- `test_snapshot_rejects_delivered_body_from_another_version`：旧正文 A vs 已登记的 B →
  `version changed`；未登记正文 → 同样拒绝；登记为空且无渲染入口 → `binding unverified`；
  登记一致 → 通过并标 `binding_verified`。
- `test_snapshot_keeps_version_identity_and_rejects_interleaving`（快照后换版拒绝）保留。

## 2. 发布策略：先校验、后原子发布

`repack_adopted` 改为在**临时包**上复算逐成员 hash（并对照快照 hash），全部 `ok` 才
`os.replace` 上线；任一项不符即删除临时包并返回 `error`（"未上线新包（旧包保持可用）"），
不再出现"坏包先出现在下载列表"的窗口。

受控反例：`test_publish_verification_failure_keeps_old_package`（注入错误期望 hash →
不产生新包、旧包集合不变、临时文件清空）。同秒两次导出仍各自成包（既有用例）。

## 3. 规则/逻辑身份（不只靠版本标签）

- 绑定新增 `logic_fingerprint`（`question_assessment.py` + `report_brief.py` +
  `acceptance_checker.py` 的源码指纹）；`state_is_current` 与 `staleness_reason` 都读它
  （新逻辑可能改变同一问题的结论 → 必须待重验）。
- 结构对象、导出快照、包内清单均带该指纹（清单 `logic_fingerprint` 字段）。
- 反例：`test_logic_fingerprint_change_invalidates_state`（旧指纹 → 失效，原因写"判定逻辑变化"）。

## 4. 长列表与其他可跨页块

离线生产渲染探针：一个超长列表项曾有 **1010 个非空白字符**落在页外（`para`/`quote`
已在 09-23 修过，列表与代码块未修）。现按**逐行** `_ensure_space` 处理列表项与代码块
（可跨页续排）。
用例：`test_report_quality.TestPdfPagination.test_long_list_item_does_not_run_off_the_page`
（长列表项 + 长代码块 → 无文字低于页脚线、页数 ≥ 2）。

## 5. 本轮测试命令与结果

```
REDIS_PORT=6399 python -m unittest test_delivery_chain          # 338 OK（新增 3 例：绑定/发布/逻辑指纹）
REDIS_PORT=6399 python -m unittest test_report_quality          # 34 OK（新增长列表分页例）
REDIS_PORT=6399 python -m unittest test_report_version test_review_edit_api \
  test_offline_delivery test_narrative_evidence test_question_assessment  # 130 OK
```

CI 门禁修正：新增测试文件必须登记在 `ci.yml`（`test_deploy_manifest.py` 的守卫）；
`test_question_assessment.py` 已加入后端作业。

## 6. 未验项（如实列出）

- **真实 Redis 多进程**与**真实并发修订**下的导出未跑实机（本轮为单进程 + 受控交错）；
  "两个导出同时进行"用同秒用例覆盖（同进程），跨进程互斥仍属既有未验项；
- 图注与图同页的**长图注续排**未改（现状：短图注与图同页，长图注未验证）；
- 页面"查看差异 → 采纳/拒绝 → 修改 → 重验 → 重包"整链在 R3 批次走正常页面完成
  （本轮只做离线快照/打包与受控反例）；
- 5 页主文软目标未追求（11 页、0 越界、13 载荷 hash 一致的现状保留）。
