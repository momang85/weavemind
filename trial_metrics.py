# -*- coding: utf-8 -*-
"""S3：真实试用的最小指标（一次试用一条记录；**未知不当 0**）。

架构指令的最小指标集：
  首次拿到可复核报告所需时间 / 必需指标正确覆盖数 vs 总数 / 错误认证数 /
  人工修改的关键字段数 / 复核耗时 / 费用（已知/估算/未知） / 是否愿再做第二个任务。

三条纪律（都有测试）：
- **未知就是未知**：拿不到的字段写 `null` + 原因，不写 0、不推算（0 会读成"没有"或"免费"）；
- **身份匿名**：只留匿名标识（`trial_id`）与类别（dev/demo/trial），不记用户名/邮箱；
- **类别不明记 unknown**：漏标类别不等于"真实试用"，统计时单列。

费用沿用现有口径：`costs`/`llm_client` 里已知就记 known，能估算记 estimated（带依据），
否则 unknown——空身份与漏费用都不能当零参与平均。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CATEGORY_DEV = "dev"
CATEGORY_DEMO = "demo"
CATEGORY_TRIAL = "trial"
CATEGORY_UNKNOWN = "unknown"
CATEGORIES = (CATEGORY_DEV, CATEGORY_DEMO, CATEGORY_TRIAL, CATEGORY_UNKNOWN)

COST_KNOWN = "known"
COST_ESTIMATED = "estimated"
COST_UNKNOWN = "unknown"


def new_trial_id() -> str:
    """匿名试用标识：随机、不可反查用户。"""
    return "trial-" + secrets.token_hex(4)


def normalize_category(value: str | None) -> str:
    c = str(value or "").strip().lower()
    return c if c in CATEGORIES else CATEGORY_UNKNOWN


@dataclass
class Measurement:
    """一个可缺测的测量值：`value=None` 时**必须**给出 `reason`。"""

    value: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.value is None and not self.reason:
            self.reason = "未采集"

    @property
    def known(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict:
        out = asdict(self)
        out["known"] = self.known
        return out


@dataclass
class TrialRecord:
    trial_id: str
    category: str = CATEGORY_UNKNOWN
    task_id: str = ""                      # 只留任务 id（可用于本地复算），不含身份
    started_at: float = field(default_factory=time.time)
    # 关键指标（缺测就写 null + 原因）
    time_to_reviewable_report_sec: Measurement = field(default_factory=Measurement)
    required_metrics_present: Measurement = field(default_factory=Measurement)
    required_metrics_total: Measurement = field(default_factory=Measurement)
    wrong_certifications: Measurement = field(default_factory=Measurement)
    human_edited_fields: Measurement = field(default_factory=Measurement)
    review_seconds: Measurement = field(default_factory=Measurement)
    cost_kind: str = COST_UNKNOWN               # known / estimated / unknown
    cost_usd: Measurement = field(default_factory=Measurement)
    would_do_second_task: str = CATEGORY_UNKNOWN  # yes / no / unknown
    notes: str = ""

    def as_dict(self) -> dict:
        out = asdict(self)
        out["trial_id"] = self.trial_id
        out["category"] = normalize_category(self.category)
        out["cost_kind"] = self.cost_kind if self.cost_kind in (
            COST_KNOWN, COST_ESTIMATED, COST_UNKNOWN) else COST_UNKNOWN
        out["would_do_second_task"] = (
            self.would_do_second_task if self.would_do_second_task in ("yes", "no")
            else CATEGORY_UNKNOWN)
        return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def summarize(records: list[TrialRecord]) -> dict:
    """汇总：分母只算**已知**样本，未知单列——不用 0 填坑、不虚报覆盖率。"""
    out: dict = {"trials": len(records), "by_category": {}, "metrics": {}}
    for r in records:
        cat = normalize_category(r.category)
        out["by_category"][cat] = out["by_category"].get(cat, 0) + 1

    def _known(attr: str) -> list[float]:
        vals = []
        for r in records:
            m = getattr(r, attr)
            if isinstance(m, Measurement) and m.value is not None:
                vals.append(float(m.value))
        return vals

    for attr in ("time_to_reviewable_report_sec", "human_edited_fields",
                 "review_seconds", "wrong_certifications"):
        vals = _known(attr)
        out["metrics"][attr] = {
            "known": len(vals),
            "unknown": len(records) - len(vals),
            "median": _median(vals),
        }
    # 必需指标覆盖：按"有值的那几次试用"算比例，并明说样本数
    covered = []
    for r in records:
        preset, total = r.required_metrics_present.value, r.required_metrics_total.value
        if preset is not None and total:
            covered.append(float(preset) / float(total))
    out["metrics"]["required_metrics_coverage"] = {
        "known": len(covered),
        "unknown": len(records) - len(covered),
        "median": _median(covered),
    }
    cost_known = [float(r.cost_usd.value) for r in records
                  if r.cost_usd.value is not None]
    out["cost"] = {
        "known": sum(1 for r in records if r.cost_kind == COST_KNOWN),
        "estimated": sum(1 for r in records if r.cost_kind == COST_ESTIMATED),
        "unknown": sum(1 for r in records if r.cost_kind == COST_UNKNOWN),
        "usd_total": round(sum(cost_known), 6) if cost_known else None,
        "note": "未知/估算不并入 total；total 为 None 表示一次都没测到金额",
    }
    out["would_do_second_task"] = {
        "yes": sum(1 for r in records if r.would_do_second_task == "yes"),
        "no": sum(1 for r in records if r.would_do_second_task == "no"),
        "unknown": sum(1 for r in records
                       if r.would_do_second_task not in ("yes", "no")),
    }
    return out


def record_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root else Path(__file__).resolve().parent / "docs" / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    return base / "trial_records.jsonl"


def append_record(record: TrialRecord, *, root: str | Path | None = None) -> Path:
    """追加一条试用记录（JSONL：一次一行，便于人工核对与后续解析）。"""
    p = record_path(root)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
    logger.info("试用记录已追加：%s（%s）", p, record.trial_id)
    return p


def load_records(root: str | Path | None = None) -> list[TrialRecord]:
    p = record_path(root)
    if not p.exists():
        return []
    out: list[TrialRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        out.append(_from_dict(raw))
    return out


def _from_dict(raw: dict) -> TrialRecord:
    def _m(key: str) -> Measurement:
        v = raw.get(key)
        if isinstance(v, dict):
            return Measurement(value=v.get("value"), reason=str(v.get("reason") or ""))
        if isinstance(v, (int, float)):
            return Measurement(value=float(v))
        return Measurement()

    return TrialRecord(
        trial_id=str(raw.get("trial_id") or new_trial_id()),
        category=normalize_category(raw.get("category")),
        task_id=str(raw.get("task_id") or ""),
        started_at=float(raw.get("started_at") or 0.0),
        time_to_reviewable_report_sec=_m("time_to_reviewable_report_sec"),
        required_metrics_present=_m("required_metrics_present"),
        required_metrics_total=_m("required_metrics_total"),
        wrong_certifications=_m("wrong_certifications"),
        human_edited_fields=_m("human_edited_fields"),
        review_seconds=_m("review_seconds"),
        cost_kind=str(raw.get("cost_kind") or COST_UNKNOWN),
        cost_usd=_m("cost_usd"),
        would_do_second_task=str(raw.get("would_do_second_task") or CATEGORY_UNKNOWN),
        notes=str(raw.get("notes") or ""),
    )


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def record_from_task(task_id: str, *, category: str = CATEGORY_UNKNOWN,
                     root: str | Path | None = None,
                     workspace_root: str | Path | None = None) -> TrialRecord:
    """从一次已完成的任务里**尽量**取指标：取不到的一律 unknown（并写明原因）。

    能自动取到的：必需指标覆盖（读底稿的 completeness）、是否产出可复核底稿与耗时
    （用任务工作区的文件时间近似）；取不到的（人工修改字段数、复核耗时、费用、
    是否愿再做）留给人工填写——**不猜**。
    """
    rec = TrialRecord(trial_id=new_trial_id(), category=normalize_category(category))
    rec.task_id = task_id if _TASK_ID_RE.match(str(task_id or "")) else ""
    if not rec.task_id:
        rec.notes = "task_id 不合法，未关联任务"
        return rec
    try:
        from workspace import task_project_dir  # 本地导入，避免循环依赖
        proj = Path(workspace_root) if workspace_root else None
        proj = task_project_dir(task_id, None) if proj is None else proj / task_id / "project"
        paper_path = proj / "working_paper.json"
        if not paper_path.exists():
            rec.required_metrics_present = Measurement(None, "该任务没有底稿（无结构化财务）")
            rec.required_metrics_total = Measurement(None, "该任务没有底稿（无结构化财务）")
            return rec
        paper = json.loads(paper_path.read_text(encoding="utf-8"))
        comp = paper.get("completeness") or {}
        rec.required_metrics_present = Measurement(
            float(comp.get("present") or 0), "")
        rec.required_metrics_total = Measurement(float(comp.get("required") or 0), "")
        done = paper_path.stat().st_mtime
        started = (proj.parent / "started_at").stat().st_mtime \
            if (proj.parent / "started_at").exists() else None
        if started:
            rec.time_to_reviewable_report_sec = Measurement(max(0.0, done - started))
        else:
            rec.time_to_reviewable_report_sec = Measurement(
                None, "缺任务开始时间，无法算首份可复核报告耗时")
        rec.wrong_certifications = Measurement(
            None, "需人工复核后填写（错误认证需人判断，机器不代填）")
    except Exception as exc:                     # noqa: BLE001 - 指标采集不得拖垮主线
        rec.notes = f"采集失败：{str(exc)[:120]}"
    return rec
