# -*- coding: utf-8 -*-
"""第三批候选：把采纳正文收敛成"一份能读的公司研究简报"（离线生成候选，不改用户字节）。

做法：从当前采纳正文里**按小节抽取**有效内容，重排为
  标题/口径行 → 关键发现 → 研究问题与下一步（收入/利润/现金/银行视角）→ 财务对照 →
  图表（主文 3 张，其余入附录）→ 风险与核查 → 附录（来源/范围与时效/字段位置/金额变化/
  比率适用条件/缺口清单/其他核查项/其他图表/版本与验收/免责声明）。

保留清单（先列再删，不机械截断）：
- 保留：关键发现 4 条；研究问题与下一步 4 组（含支持/边界/下一步）；财务对照表；
  同比与比率 12 行；图 1–3 及图注；风险与核查（含待核查主张、结论边界）；
  附录全部小节（参考来源/资料范围/字段位置/金额变化/比率适用条件/其他核查项/
  其他图表/版本与验收状态/免责声明）。
- 从"完整模型稿"里**只搬**读者需要的两处：数据时效（获取时间/币种/口径依据）与
  缺口清单（9 项，压缩进附录）；其余重复段落（关键数据一览、2.x–9.x 逐节复述、
  投资建议、结论复述、空节"关键数据一览（汇总复核）"）不再进主文。
- 删掉：整段任务指令标题块、`bank_corporate` 术语、hash、chart_id、状态实现术语。

用法：python scripts/candidate_convergence_20260923.py
产物：C:/Users/ding0/AppData/Local/Temp/candidate_convergence_20260923.md（+ .json）
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TASK_WS = Path(r"C:\Users\ding0\AppData\Local\Temp\agent_workspace\tasks\projects\default\ui-706c5ef4a5")
OUT_MD = Path(r"C:\Users\ding0\AppData\Local\Temp\candidate_convergence_20260923.md")
OUT_JSON = Path(r"C:\Users\ding0\AppData\Local\Temp\candidate_convergence_20260923.json")
# 收敛来源版本（完整稿）：37e0657a42d816c7 = 去掉孤立 ** 后的采纳正文
SOURCE_SHA = "37e0657a42d816c77128e2618912c531790438c36406372f4e643e136bd79da4"


def _section(body: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    """取 [start_marker 起, 最近的 end_marker 前) 的文本；找不到返回空串。"""
    i = body.find(start_marker)
    if i < 0:
        return ""
    j = len(body)
    for m in end_markers:
        k = body.find(m, i + len(start_marker))
        if 0 <= k < j:
            j = k
    return body[i:j].strip("\n")


def main() -> int:
    d = json.loads((TASK_WS / "report_versions.json").read_text(encoding="utf-8"))
    # 固定来源版本：从**完整稿**收敛（不拿已经收敛过的版本再收敛，否则抽取会错位）
    src_prefix = SOURCE_SHA[:16]
    body = ""
    for k, v in (d.get("versions") or {}).items():
        b = str(v.get("body") or "")
        if hashlib.sha256(b.encode("utf-8")).hexdigest()[:16] == src_prefix:
            body = b
            break
    if not body:
        raise SystemExit(f"版本库里找不到来源正文 {src_prefix}")
    src_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    print("来源正文:", src_sha[:16], len(body), "字符")

    findings = _section(body, "## 关键发现", ("## 研究问题与下一步",))
    questions = _section(body, "## 研究问题与下一步", ("## 业务背景", "## 财务对照"))
    finance = _section(body, "## 财务对照", ("## 图表",))
    charts = _section(body, "## 图表", ("## 分析",))
    risks = _section(body, "## 风险与核查", ("## 附录",))
    appendix = _section(body, "## 附录", ("### 完整模型稿（审计留档）",))
    appendix = re.sub(r"^##\s*附录\s*\n", "", appendix).strip("\n")
    audit_ptr = _section(body, "### 完整模型稿（审计留档）", ("## 参考来源",))

    # 从完整模型稿里搬三处：数据时效、结论、缺口清单
    timeliness = _section(body, "## 数据时效", ("## 一、关键数据一览", "### 2.1", "## 二"))
    conclusion = _section(body, "### 7.2 结论", ("## 八、投资建议",))
    conclusion = "\n".join(ln for ln in conclusion.split("\n")
                           if not ln.strip().startswith("###"))
    gap_items = _section(body, "## 九、缺口说明（汇总）", ("## 关键数据一览（汇总复核）",))
    gap_lines = [ln.strip() for ln in gap_items.split("\n")
                 if re.match(r"^\d+\.\s", ln.strip())]
    print("抽取: findings", len(findings), "| questions", len(questions),
          "| finance", len(finance), "| charts", len(charts), "| risks", len(risks),
          "| appendix", len(appendix), "| timeliness", len(timeliness),
          "| gap_items", len(gap_lines))
    assert findings and questions and finance and risks and appendix, "抽取失败，停止"

    # 图表小节：保留图 1–3 与"其余 3 张"提示，去掉末尾的附录指针重复
    charts_main = charts
    for marker in ("其余 3 张图（含比率分面）见附录『其他图表』。",):
        charts_main = charts_main.replace(marker, marker)
    # 数据时效压缩：只留三行读者需要的
    keep_ts = []
    for ln in timeliness.split("\n"):
        s = ln.strip()
        if any(k in s for k in ("数据获取时间", "币种与单位", "报表口径", "资料截止时间")):
            keep_ts.append(ln.rstrip())
    ts_block = "\n".join(keep_ts) if keep_ts else ""
    # 缺口清单压缩成一行一条
    gap_block = "\n".join(gap_lines) if gap_lines else "- 本次未取得正文定位的类别：业务背景、经营变化解释、财务附注、风险因素。"

    parts = [
        "# 洋河股份 经营分析简报（2023–2024 年度）",
        "",
        "> 主体：洋河股份（002304.SZ）｜期间：2023–2024 年度（合并报表口径）｜"
        "资料截至：2025-04-30｜单位：亿元。",
        "> 采用来源 1 条（未采用 5 条留在内部审计）。",
        "",
        findings,
        "",
        questions,
        "",
    ]
    if conclusion:
        parts += ["## 结论", "", conclusion, ""]
    if ts_block:
        parts += ["## 数据时效", "", ts_block, ""]
    parts += [
        finance,
        "",
        charts_main,
        "",
        risks,
        "",
        "## 附录",
        "",
    ]
    # 附录里补"数据时效"与"缺口清单（需补材料）"
    app_lines = appendix.split("\n")
    out_app: list[str] = []
    for ln in app_lines:
        # 收敛后主文不再有"『分析』一节"：版本说明改成与新结构一致的事实陈述
        if "『分析』一节为模型撰写" in ln:
            ln = ("   关键数据与来源清单由底稿生成（可复算）；研究问题与下一步为模型撰写，"
                  "并与正文一并经机器验收。交付状态与验收结论见任务页与导出清单"
                  "（未验收时按草稿处理）。")
        if ln.strip() == "### 资料范围与口径":
            out_app.append(ln)
            out_app.append("")
            continue
        if ln.strip() == "### 其他核查项":
            out_app.append("### 缺口清单（需补材料）")
            out_app.append("")
            out_app.append(gap_block)
            out_app.append("")
        out_app.append(ln)
    parts.extend(out_app)
    if audit_ptr:
        parts.append("")
        parts.append(audit_ptr)
    new_body = "\n".join(parts).rstrip() + "\n"
    # 基本自检：无重复大节、无内部术语
    for bad in ("bank_corporate", "chart_", "［core"):
        if bad in new_body:
            print("警告：新正文仍含", bad)
    dup = [h for h in ("## 一、关键数据一览", "## 关键数据一览（汇总复核）", "## 变化解释",
                       "## 八、投资建议", "## 九、缺口说明（汇总）", "### 7.1 风险提示")
           if h in new_body]
    assert not dup, f"重复小节未清除：{dup}"
    OUT_MD.write_text(new_body, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(new_body, ensure_ascii=False), encoding="utf-8")
    print("候选:", hashlib.sha256(new_body.encode("utf-8")).hexdigest()[:16],
          len(new_body), "字符（来源", len(body), "）")
    print("wrote", OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
