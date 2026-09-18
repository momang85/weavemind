# B 批证据：修订后按新版本重新装配交付（2026-09-18）

依据《阶段复核与下一步指令_20260918》§B 与《A批复核与补修指令》收尾要求。
**全部离线**：无实机、无联网、无模型/权限/门禁改动。修订链路、四例矩阵与相关回归都在本机跑过。

## 一、问题与做法

复核指出的缺陷：**修订接口返回新正文验收 PASS，但 manifest 比较的是旧交付 hash，
验收详情仍返回旧 fail / 旧正文 hash**。根因是编排器收尾与 web_ui 修订各写一套
"验收 → 装配 → 记录交付"，两边必然分叉。

做法（按已确认的两条决定）：

- **抽共享模块** `delivery_pipeline.py`，编排器收尾与 web_ui 修订走同一实现：
  - `accept_for_body()`：对**指定正文**跑验收（含来源标注自动修复并复检）、写
    `acceptance_report.json` + 追加审计事件、按完整身份绑定到该版；
  - `assemble_and_verify()`：确保本版有验收 → 重算底稿与研究硬门槛 → 按**固定顺序**
    装配（`交付说明 + 分隔符 + 正文` + 注记 → 链接重写）→ 走唯一谓词判状态 →
    未通过则加草稿注记 → `record_delivery`；
  - `delivery_state()`：页面/导出清单/验收详情**共用的读取口径**（选中版本 + 本版验收 +
    持久化评审事实 + 可重算的硬门槛 + **同一版本**的交付记录）。
- **旧评审不迁移**：`review_state.json` 增记 `report_version_id` 与 `required`；
  共享谓词 `review_valid = (not required) or (verdict=="PASS" and report_version_id == 选中版本)`。
  正文一改就是新版本，旧 PASS 不再覆盖；个人模式（非银行）本不要求评审，因此不受影响。
- 装配顺序固定后，两边产出的交付字节与 `delivered_sha256` 一致——manifest 的
  `aligned`/`final_content_matches` 才有意义。

## 二、四例 × 四面矩阵（`test_review_edit_api.TestRevisionSameVersionMatrix`）

四个面：任务页 `GET /task/<id>` 的 `delivery`/`acceptance`、`GET /api/task/<id>/acceptance`、
导出清单 `_write_export_manifest`、导出头 `/report.md` 的 `X-Report-Version-Id`/`X-Report-Draft`。

| 用例 | 实测 |
|---|---|
| 旧 fail → 新 pass | 四面都指向**新版本**；个人模式且底稿/硬约束达标 → `verified`，`aligned=true`，`X-Report-Draft: 0` |
| 旧 pass → 新 fail | 四面一致为 `draft`，理由含"验收未通过"；页面 `delivery.verified=false`；旧 pass 不残留 |
| 验收异常 | HTTP 200（新版本已采纳），四面为 draft/未知；验收详情是明确的"没有本版验收"（404），**不拿旧版本结论冒充** |
| 交付更新失败 | HTTP **500** + "交付投影写入失败"，`delivery_updated=false`；版本库已是新版但页面仍是旧正文 → 核对不一致 → `draft`，绝不 verified |

另加一例：**旧评审 PASS 不迁移**——银行口径（`review_required=True`）下修订版
`verified=false`、理由含"评审"，四个面一致为 draft；个人模式下同一修订可以是 verified。

## 三、同一版一致性的关键实现点

- **交付说明逐字节复用**：收尾把分隔符之前的交付说明落盘为 `delivery_wrapper.md`，
  修订时优先读它；旧任务回退到"剥离旧正文与自动注记"（`strip_auto_notes`），并在交付物里
  显式标注"交付说明由旧交付正文推导，请人工确认"——不再把上一版的"未验收草稿：<旧理由>"
  带进新稿（"不得继续拿旧失败说明判断新稿"）。
- **验收详情按版本绑定**：磁盘快照属于别的版本时标 `stale_file: true` 且**不返回旧 checks**；
  有本版验收则以它为准并给 `version_bound`。
- **清单不再硬编码**：`verified_delivery(..., review_valid=True, hard_ok=True)` 改为共享
  读取口径；删掉"把交付文档 hash 与研究正文 hash 比较"的兜底分支。无交付记录 → `aligned=null`
  且 `draft`，理由"尚无交付记录（无法核对导出字节）"；导出正文为空 → 同样不得判已验证。
- **投影同步**：新增 `task_state.update_delivery_projection`（正文 + 验收摘要 + 状态，
  用 `derive_status` 派生；CANCELLED/FAILED 拒绝改写），页面顶部的状态不再停在旧结论。
- 顺手修：`_get_task_pdf` 重复写清单；`X-Report-Body-Sha256` 改为描述**实际送达字节**
  （另加 `X-Report-Research-Body-Sha256` 表示该版研究正文）；PDF 字节仍不要求等于 Markdown hash。
- 编排器侧旧实现已删除（154 行），只保留共享实现 + 薄包装，避免两套逻辑分叉。

## 四、定向验证（未跑全仓）

```
python -m unittest test_delivery_chain test_orchestrator_v2 test_p0 test_acceptance_adversarial \
                   test_report_quality test_task_time_optimization test_r0_boundaries \
                   test_report_version test_review_edit_api test_offline_delivery
→ Ran 792 tests ... OK   （含评测闸门通过）
```

过程中两处真实回归被这套回归网抓住并修好：
1. 共享 `rewrite_report_links` 丢掉了 Windows 盘符/相对路径处理 → `test_delivery_chain` 两条失败，
   已按原实现恢复（`/files/<tid>/` 只用于 reports/charts，其余改相对路径）。
2. `read_review_facts` 一度让**文件里的 `required`** 覆盖配置口径 → 旧文件能静默绕过必需评审；
   改为 `required` 一律当场从配置算（文件只作审计留痕）。

## 五、未验证项（不要按已完成记账）

- **未做实机**：修订链路全部为离线证据；真实任务上"改一版 → 重验 → 重新交付"尚未跑过（C 批的有界实机里包含一次修订）。
- **前端没有修订入口**：仓库内不存在 `review/edit` 的调用点（B 的四个面是任务页 payload、
  验收详情、manifest、导出）。要人工改版目前只能调 API；界面入口留到 D 批的试用包一起做。
- **并发写**：`report_versions.json` 仍是进程内锁 + 固定 `.tmp`。修订发生在任务终态之后
  （编排器此时不在写同一任务），因此本批不改锁；"同一任务被两个进程同时改版"不在覆盖范围。
- `delivery_wrapper.md` 只对**新任务**生效；旧任务走推导 + 标注路径（已在交付物里写明）。
