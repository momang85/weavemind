# -*- coding: utf-8 -*-
"""M0-d 成功经验与模板准入：**唯一**显式谓词。

为什么需要单独一个模块：此前准入散落在三处，且都是"默认放行"：
- `MemoryManager.consolidate_memory`：没有验收报告（UNKNOWN）时照旧沉淀策略；
- `templates_pipeline`：只拦 `acceptance is False`，UNKNOWN 仍按"本次已验证"计数；
- `prompt_refinery`：只要触发了反思/失败就改写全局提示词，不看这次运行是否被承认。

结果是"已验证成功次数"被无证据样本抬高，模板固化阈值随之失真，单个任务的教训
还可能升级成全局指令。这里把判据写成显式谓词，三处共用，且**默认拒绝**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUCCESS = "SUCCESS"
ACCEPTED = "pass"

# 不算成功经验/已验证的终态（显式列出，避免"没想到的终态"被当作成功）
NON_SUCCESS_STATUSES = (
    "SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED", "QUEUED",
    "RUNNING", "PENDING", "PARTIAL", "TIMEOUT", "ERROR", "",
)


@dataclass
class AdmissionDecision:
    """准入结论：`admitted` 决定能否进经验池，`verified` 决定能否计入已验证成功。"""

    admitted: bool = False
    verified: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"admitted": self.admitted, "verified": self.verified,
                "reasons": list(self.reasons)}

    def __bool__(self) -> bool:          # 便于 `if decision:` 直接判准入
        return self.admitted


def admit_success(
    *,
    status: str = SUCCESS,
    acceptance: dict | None = None,
    hard_ok: bool = True,
    hard_reasons: list[str] | None = None,
    review: dict | None = None,
    mode: str = "local",
    version_bound: bool = True,
) -> AdmissionDecision:
    """显式准入谓词（M0-d）。

    admitted（可进策略/经验池）要求全部满足：
      1. 终态是 SUCCESS —— SUCCESS_WITH_ISSUES / FAILED / CANCELLED 都不算成功；
      2. 有验收结果且 `overall == "pass"`（缺验收 = 未知，按**不通过**处理）；
      3. 满足任务硬约束（交付一致性等，由调用方判定后传入 `hard_ok`）；
      4. 验收绑定在**选中的那一版正文**上（`version_bound`）。

    verified（计入"已验证成功次数"与模板固化）额外要求：
      5. 必需评审已完成且拿到绑定 PASS。银行口径下这是硬要求；local 下若评审降级，
         仍然只是 admitted，不得当成"已验证成功"。
    """
    d = AdmissionDecision()
    if str(status or "").upper() != SUCCESS:
        d.reasons.append(f"终态不是成功（{status or '未知'}）")
        return d
    if not isinstance(acceptance, dict) or not acceptance:
        d.reasons.append("没有验收结果（未知，不得计成功经验）")
        return d
    if str(acceptance.get("overall") or "") != ACCEPTED:
        d.reasons.append(f"验收未通过（overall={acceptance.get('overall') or '未知'}）")
        return d
    if not version_bound:
        d.reasons.append("验收未绑定到选中的正文版本（版本不可信）")
        return d
    if not hard_ok:
        d.reasons.extend(hard_reasons or ["任务硬约束未满足"])
        return d

    mode = str(mode or "local").lower()
    verdict = str((review or {}).get("verdict") or "").upper()
    if mode == "bank" and verdict != "PASS":
        # 银行口径：必需评审没有绑定 PASS 的执行连经验池都不进
        d.reasons.append(f"银行口径缺少绑定 PASS（verdict={verdict or '缺失'}）")
        return d
    d.admitted = True
    if verdict != "PASS":
        d.reasons.append("评审未完成（降级）：可作经验参考，但不是已验证成功")
        return d
    d.verified = True
    return d


def verified_success(**kwargs) -> bool:
    """便捷判定：本次运行能否计入"已验证成功"（模板固化阈值用）。"""
    return admit_success(**kwargs).verified
