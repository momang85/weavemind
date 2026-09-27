# -*- coding: utf-8 -*-
"""生成**研究员评分交接单**：自动引用当前采纳版本与交付包，并核对三者是否同版。

反例（阶段 D 复核 + 09-24 复核）：评分交接文档里手抄版本号与 ZIP 链接，
任务一改版/重新导出，交接单就指向旧包——研究员按旧链接评分，结论对不上当前稿。
这里每次从工作区**实读**三样东西并当场核对：

1. 当前采纳版本（`report_version.VersionStore.adopted()`）；
2. 最新交付包 `deliverables_*.zip`（算 sha256）；
3. 包内 `PACKAGE_MANIFEST.json` 的 `report_version_id`（交付包的版本绑定）。

三者不同版时**明确写"不可用于评分"**，而不是照样给出一张表。
评分表沿用 `docs/研究员评分表_F3_20260920.md` 的五项判据（各 0/1/2，≥8/10，
不得有错主体/错期/错引用/虚构因果）；表由**研究员**填，模型不自评。

用法：
    python scripts/scoring_handoff.py <task_id> [--out docs/evidence/xxx.md]
    python scripts/scoring_handoff.py <task_id> --print     # 只打印不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUBRIC = (
    ("① 问题回答程度", "只有零散数字，读不出\"公司怎么样\"",
     "有变化与方向，但缺关键一问（如增长来源/现金质量）",
     "两三个最值得关注的变化清楚，缺口与下一步要查的材料写明"),
    ("② 数字可复算", "数字对不上来源或算不出来", "主要数字能对上，个别需追问",
     "每个读数都能按给出口径/算式复算（含同比、比率）"),
    ("③ 解释有证据", "原因像是编的或与材料无关", "部分解释有出处，个别越界",
     "每条解释都指向具体来源位置（小节/页码），没有证据的写成待核查"),
    ("④ 风险具体", "只有\"存在风险\"这类空话", "有风险条目但缺证据或触发条件",
     "风险带对应证据、会使判断改变的观察条件、要补的材料"),
    ("⑤ 修改复用便利", "想改只能重跑任务", "能改但版本/导出对不上",
     "页面上直接改正文→重验→同版导出，耗时短且版本清楚"),
)


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _latest_package(ws: Path) -> Path | None:
    zips = sorted(p for p in ws.glob("deliverables_*.zip") if p.is_file())
    return zips[-1] if zips else None


def _manifest_in_zip(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "PACKAGE_MANIFEST.json" not in zf.namelist():
                return {}
            return json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8"))
    except Exception:                                 # noqa: BLE001
        return {}


def collect(task_id: str) -> dict:
    """实读当前状态；读不到的一律留空/标未知，绝不编造。"""
    import task_state
    from report_version import VersionStore
    from workspace import task_workspace

    row = task_state.read_task(task_id) or {}
    ws = Path(str(task_workspace(task_id)))
    out: dict = {"task_id": task_id, "ws": str(ws), "row": row}

    store = VersionStore(ws, task_id)
    ver = store.adopted()
    out["adopted"] = ver
    if ver is not None:
        out["version_id"] = ver.version_id
        out["identity_id"] = ver.identity_id()
        out["acceptance_overall"] = ver.acceptance_overall()
        out["acceptance_bound"] = ver.acceptance_for_this_body()
        out["needs_reverify"] = ver.acceptance_needs_reverify()
        out["version_created_at"] = ver.created_at
        out["body_chars"] = len(ver.body or "")

    zip_path = _latest_package(ws)
    out["zip"] = zip_path
    if zip_path is not None:
        out["zip_name"] = zip_path.name
        out["zip_sha256"] = _sha256_file(zip_path)
        out["zip_bytes"] = zip_path.stat().st_size
        out["zip_mtime"] = zip_path.stat().st_mtime
        man = _manifest_in_zip(zip_path)
        out["zip_manifest"] = man
        out["zip_version_id"] = str(man.get("report_version_id") or "")
        out["zip_md_sha256"] = str(man.get("delivered_md_sha256") or "")
        out["zip_pdf_sha256"] = str(man.get("pdf_sha256") or "")
        out["zip_packaged_at"] = str(man.get("packaged_at") or "")

    exp = ws / "export_manifest.json"
    if exp.exists():
        try:
            out["export_manifest"] = json.loads(exp.read_text(encoding="utf-8"))
        except Exception:                             # noqa: BLE001
            out["export_manifest"] = {}
    return out


def same_version(info: dict) -> tuple[str, str]:
    """交付包是否与**当前采纳版本**同版。返回 `(verdict, 说明)`。

    verdict ∈ yes / no / unknown —— unknown 一律按"不可用于评分"呈现。
    """
    vid = str(info.get("identity_id") or "")
    zvid = str(info.get("zip_version_id") or "")
    if info.get("zip") is None:
        return "unknown", "没有找到交付包（deliverables_*.zip）"
    if not zvid:
        return "unknown", "交付包内没有 PACKAGE_MANIFEST.json 的 report_version_id"
    if not vid:
        return "unknown", "任务没有当前采纳版本，无法与包内版本比对"
    if zvid == vid:
        return "yes", f"包内报告版本 {zvid[:16]}… 与当前采纳版本一致"
    return "no", (f"包内版本 {zvid[:16]}… ≠ 当前采纳版本 {vid[:16]}…："
                  "该包是旧版，**不可用于评分**（请按当前版本重新导出）")


def render(info: dict) -> str:
    row = info.get("row") or {}
    vid = str(info.get("version_id") or "")
    lines: list[str] = []
    lines.append(f"# 研究员评分交接单 · 任务 `{info['task_id']}`")
    lines.append("")
    lines.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
                 "（本文件由 `scripts/scoring_handoff.py` 从工作区实读生成，"
                 "版本与包 hash 均非手抄）")
    lines.append("")
    lines.append("## 1. 这份稿子是哪一版")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 任务 | `{info['task_id']}` |")
    lines.append(f"| 目标 | {str(row.get('goal') or '（未记录）')[:120]} |")
    lines.append(f"| 任务状态 | {row.get('status') or '（未知）'} |")
    lines.append(f"| 研究契约（截至日等） | "
                 f"{(row.get('research_request') or {}).get('as_of') or '（未记录）'} |")
    lines.append(f"| **当前采纳版本**（正文全量 sha256） | `{vid or '（无采纳版本）'}` |")
    lines.append(f"| 版本身份（正文+来源+规则） | `{info.get('identity_id') or '—'}` |")
    lines.append(f"| 机器验收结论 | {info.get('acceptance_overall') or '（未知）'} |")
    lines.append(f"| 验收是否绑定**本版**正文（全量 hash） | "
                 f"{'是' if info.get('acceptance_bound') else '否/未知'} |")
    lines.append(f"| 是否需要重验（旧短 hash 验收） | "
                 f"{'是' if info.get('needs_reverify') else '否'} |")
    if info.get("version_created_at"):
        lines.append(f"| 版本建立时间 | "
                     f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['version_created_at']))} |")
    lines.append(f"| 正文字数 | {info.get('body_chars') or 0} |")
    lines.append("")
    verdict, why = same_version(info)
    lines.append("## 2. 交付包与同版核对")
    lines.append("")
    if info.get("zip") is not None:
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 交付包 | `{info['zip_name']}` |")
        lines.append(f"| 包 sha256 | `{info['zip_sha256']}` |")
        lines.append(f"| 包大小 | {info['zip_bytes']:,} 字节 |")
        lines.append(f"| 包内报告版本 | `{info.get('zip_version_id') or '—'}` |")
        lines.append(f"| 包内 Markdown sha256 | `{info.get('zip_md_sha256') or '—'}` |")
        lines.append(f"| 包内 PDF sha256 | `{info.get('zip_pdf_sha256') or '—'}` |")
        lines.append("")
    mark = {"yes": "✅ 同版", "no": "❌ **不同版**", "unknown": "⚠️ 无法判定"}[verdict]
    lines.append(f"**同版核对：{mark}** —— {why}")
    lines.append("")
    if verdict != "yes":
        lines.append("> 在补齐同版交付包之前，**请不要**按本单评分："
                     "分数会绑到一个不是当前稿的包上。")
        lines.append("")
    lines.append("## 3. 打开方式（同一台机器、同一实例）")
    lines.append("")
    lines.append(f"- 任务页：`http://127.0.0.1:8080/` → 控制台「项目结果」→ 点本任务的**查看**"
                 f"（任务 id `{info['task_id']}`）")
    lines.append(f"- 正文 Markdown：`/api/task/{info['task_id']}/report.md`")
    lines.append(f"- PDF：`/api/task/{info['task_id']}/pdf`")
    lines.append(f"- 交付包：`/api/task/{info['task_id']}/deliverables`")
    lines.append(f"- 工作区（只读核对用）：`{info['ws']}`")
    lines.append("")
    lines.append("## 4. 五项评分（**研究员填**，各 0/1/2，建议 ≥8/10）")
    lines.append("")
    lines.append("| 项 | 0 分 | 1 分 | 2 分 | 本次得分 | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for name, z, o, t in RUBRIC:
        lines.append(f"| {name} | {z} | {o} | {t} |  |  |")
    lines.append("")
    lines.append("| 其它必填 | 填写 |")
    lines.append("|---|---|")
    lines.append("| 合计（/10） |  |")
    lines.append("| 是否存在严重问题（错主体/错期/错引用/虚构因果） |  |")
    lines.append("| 复核耗时 |  |")
    lines.append("| 你做的关键改动 |  |")
    lines.append("| 是否愿意做第二份报告 |  |")
    lines.append("")
    lines.append("## 5. 请务必知道的三件事")
    lines.append("")
    lines.append("1. **机器验收 ≠ 研究通过**：上表的验收结论只说明确定性检查"
                 "（数字可溯源、引用对得上、声明齐全）通过；")
    lines.append("2. **本单不由模型自评**：分数、严重问题、耗时与改动都必须由**研究员**填；"
                 "空表或模型自评不能作为阶段通过依据；")
    lines.append("3. **费用未知**：无计费口径可读，任何耗时都是墙钟时间，不是成本。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成研究员评分交接单（自动引用当前版本与交付包）")
    ap.add_argument("task_id")
    ap.add_argument("--out", default="", help="输出文件（默认打印到 stdout）")
    ap.add_argument("--print", dest="to_stdout", action="store_true", help="打印而不写文件")
    args = ap.parse_args(argv)

    info = collect(args.task_id)
    text = render(info)
    if args.out and not args.to_stdout:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        verdict, why = same_version(info)
        print(f"已写出 {out}（同版核对：{verdict} —— {why}）")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
