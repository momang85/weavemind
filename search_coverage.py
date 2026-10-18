"""检索覆盖率汇总（任务19 · 可见性）。

**要解决的问题**：曾被记为 SUCCESS 的 web_search 步里，有一部分其实由非检索执行者
（content_summarizer / codeexecworker 等）代做，返回的是 0 链接的 Markdown。任务14 已经
在步结果里打了 `search_degraded` 标记，但报告读者看不到——"这段结论有没有来源"只能靠猜。

**本模块干什么**：把步骤真值汇总成一份可展示的覆盖率，供两处使用（同一口径）：
- 交付报告末尾的"数据来源摘要"块（`delivery_pipeline.assemble_and_verify` 调用）；
- 任务详情 API 的 `search_coverage` 字段（`web_ui._get_task_page` 调用）。

**纪律**：
- 只**读**步骤真值，不改步状态、不改报告正文，不落库（不往 `steps_json` 里塞伪步骤——
  那会让前端把摘要当一步渲染，也会污染 `metrics_collector` 的步级聚合）；
- 判不出来就说判不出来：没有检索真值的 SUCCESS 步记 `zero_source`（"无来源事实"），
  而不是记成 `ok`；
- 覆盖率 = 有效检索步 / 检索步总数；四个桶**互斥**（按优先级单归类），避免"既降级又零来源"
  被扣两次、算出负数或超过 100%。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("search_coverage")

# 只统计检索步。web_fetch 是"按已知 URL 取正文"，不是检索，不参与覆盖率。
SEARCH_CAPABILITY = "web_search"

# 桶（互斥，按下列优先级单归类）：降级 > 未完成 > 有来源/零来源
BUCKETS = ("ok", "zero_source", "degraded", "incomplete")

FOOTER_SIGNATURE = "> 机器生成 · 覆盖率 = 有效检索步 / 总检索步"


def _val(step: dict, res: dict, key: str):
    """读 `key`：先看步结果体，再退回步骤本身（两种落库形状都认）。"""
    if key in res:
        return res.get(key)
    return step.get(key)


def _classify_step(step: dict) -> str:
    """把一个 web_search 步归入恰好一个桶。

    - `degraded`：非检索执行者代做（任务14 的 `search_degraded`）——不管理由多像成功，
      它都不是检索来的证据，优先级最高；
    - `incomplete`：查询**没跑成**（timeout / rate_limited / providers_cooling /
      预算拒绝 …），或步骤本身不是 SUCCESS；
    - `zero_source`：查询跑完但零命中（`no_results`），或 SUCCESS 却没有任何检索真值记录；
    - `ok`：跑完且有来源（`search_status.status == "ok"`）。
    """
    res = step.get("result")
    if not isinstance(res, dict):
        res = step
    if bool(_val(step, res, "search_degraded")):
        return "degraded"
    status = str(_val(step, res, "status") or "SUCCESS").upper()
    verdict = _val(step, res, "search_verdict")
    if not isinstance(verdict, dict) or not verdict:
        _ss = _val(step, res, "search_status")
        verdict = dict(_ss) if isinstance(_ss, dict) else {}
    st = str(verdict.get("status") or "")
    if st == "ok":
        # 完成的检索才有来源；步本身失败（内层报错被 `_normalize_result` 改判）不算证据
        return "ok" if status == "SUCCESS" else "incomplete"
    if st and st != "no_results":
        return "incomplete"
    if status != "SUCCESS":
        return "incomplete"
    # `no_results`，或 SUCCESS 但**没有任何检索真值**：都按"无来源"记账，不假装有
    return "zero_source"


def summarize_search_coverage(steps) -> dict:
    """汇总检索覆盖率。`steps` 是 `steps_json` 那种步骤列表；不是列表就按空处理。"""
    counts = {b: 0 for b in BUCKETS}
    out = {
        "total_search_steps": 0,
        "ok_steps": 0,
        "zero_source_steps": 0,
        "degraded_steps": 0,
        "incomplete_steps": 0,
        "coverage_rate": None,
    }
    if not isinstance(steps, (list, tuple)):
        return out
    for s in steps:
        if not isinstance(s, dict):
            continue
        if str(s.get("capability") or "").strip() != SEARCH_CAPABILITY:
            continue
        counts[_classify_step(s)] += 1
    total = sum(counts.values())
    out.update({
        "total_search_steps": total,
        "ok_steps": counts["ok"],
        "zero_source_steps": counts["zero_source"],
        "degraded_steps": counts["degraded"],
        "incomplete_steps": counts["incomplete"],
        # 四个桶互斥 → 覆盖率就是 ok 占比，不会出现负数或 >100%
        "coverage_rate": round(counts["ok"] / total, 2) if total else None,
    })
    return out


def coverage_alert(summary: dict) -> str:
    """三级告警行（无问题时返回空串，不加噪声）。"""
    rate = (summary or {}).get("coverage_rate")
    degraded = int((summary or {}).get("degraded_steps") or 0)
    if degraded > 0 or (isinstance(rate, (int, float)) and rate < 0.5):
        return "> ❌ 检索质量严重不足，报告结论需谨慎参考"
    if isinstance(rate, (int, float)) and rate < 0.9:
        return "> ⚠️ 部分段落检索来源不足，建议人工补充"
    return ""


def render_coverage_block(summary: dict, rules_version: str = "") -> str:
    """渲染"数据来源摘要"块；没有检索步（或口径算不出）时返回空串——不追加。"""
    total = int((summary or {}).get("total_search_steps") or 0)
    if total <= 0:
        return ""
    rate = (summary or {}).get("coverage_rate")
    pct = f"{float(rate):.0%}" if isinstance(rate, (int, float)) else "未知"
    degraded = int((summary or {}).get("degraded_steps") or 0)
    zero = int((summary or {}).get("zero_source_steps") or 0)
    incomplete = int((summary or {}).get("incomplete_steps") or 0)
    lines = [
        "---",
        "## 数据来源摘要",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 检索步总数 | {total} |",
        f"| 有效覆盖率 | {pct} |",
        f"| 降级执行步 | {degraded}（由非检索模型完成，来源可能为空）|",
        f"| 零来源步 | {zero} |",
    ]
    if incomplete:
        # 有"没跑成"的检索步时补一行，否则表里的数字对不上（覆盖率只认 ok）
        lines.append(f"| 未完成检索步 | {incomplete}（超时/限流/预算未发请求等）|")
    lines.append("")
    alert = coverage_alert(summary)
    if alert:
        lines.append(alert)
    lines.append(f"{FOOTER_SIGNATURE} · 验收版本 {rules_version or '未知'}")
    return "\n".join(lines)


def strip_coverage_block(text: str) -> str:
    """剥掉上一次装配追加的摘要块（重装/修订时幂等，不叠加、不带旧数字）。"""
    t = str(text or "")
    idx = t.rfind(FOOTER_SIGNATURE)
    if idx < 0:
        return t
    head = t[:idx]
    cut = head.rfind("\n---")
    if cut < 0:
        return t
    return head[:cut].rstrip("\n")
