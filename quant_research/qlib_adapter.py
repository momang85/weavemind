# -*- coding: utf-8 -*-
"""Qlib **适配层**（Y2b）：只做"能接"的三件事，**不在主环境安装 Qlib**。

架构 §9.3/§7 的边界：Qlib 依赖重（numpy/scipy/pandas/cython/lightgbm…），装进主环境会把
"基础研究启动"拖慢、并引入不可复现的版本面。所以：

- **数据导出**（本模块，只用标准库）：把 `market_history/0` 载荷导成 Qlib 能读的 CSV
  （`date,instrument,open,close,volume` ＋ 基准单独一张）—— 这是真能跑、可测试的部分；
- **可用性探测**（`available()`）：`qlib` 能 import 才为真，否则返回原因与安装指引，
  **绝不返回伪造结果**；
- **运行**（`run_via_qlib`）：只有 `available()` 为真才委托；否则返回 `unavailable` + 原因。
  委托实现留作下一步（等独立环境就绪后按真实 Qlib API 写，不预先编造接口调用）。

独立环境（**不装主环境**）建议步骤，供你或后续会话执行：
    python -m venv %USERPROFILE%\\wm_qlibenv
    %USERPROFILE%\\wm_qlibenv\\Scripts\\python.exe -m pip install -U pip
    %USERPROFILE%\\wm_qlibenv\\Scripts\\python.exe -m pip install pyqlib
装好后本模块的 `available()` 才会在**那个**解释器里为真（主环境仍为假）。
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

ADAPTER = "qlib_adapter_v1"
SCHEMA = "weavemind.qlib_bridge/0"
QLIB_COLUMNS = ("date", "instrument", "open", "close", "volume")


def available() -> tuple[bool, str]:
    """Qlib 是否可用（只看能否 import，不猜版本能力）。"""
    try:
        import qlib                                            # noqa: F401
        return True, ""
    except Exception as exc:                                   # noqa: BLE001
        return False, (f"未安装 qlib（{type(exc).__name__}）：按架构约束**不装主环境**，"
                       "请在独立环境安装后于该解释器内使用本适配层")


def describe() -> dict:
    ok, why = available()
    return {"adapter": ADAPTER, "schema": SCHEMA, "qlib_available": ok, "reason": why,
            "install_recipe": [
                r"python -m venv %USERPROFILE%\wm_qlibenv",
                r"%USERPROFILE%\wm_qlibenv\Scripts\python.exe -m pip install -U pip",
                r"%USERPROFILE%\wm_qlibenv\Scripts\python.exe -m pip install pyqlib",
            ],
            "boundary": ("主环境不装 Qlib；数据导出与账本审计在本仓库内完成，"
                         "Qlib 仅作交叉验证目标")}


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def export_qlib_csv(payload: dict, out_dir, *, benchmark: str = "") -> dict:
    """`market_history/0` → Qlib 可读 CSV（标的表 + 基准表分开）。

    Qlib 的 `instrument` 用 `代码`（不带交易所后缀，与我们的载荷一致）；缺失价的行**不写**并计数，
    不补 0、不前值填充。
    """
    d = Path(out_dir)
    if not d.is_dir():
        return {"ok": False, "reason": f"输出目录不存在：{d}"}
    rows = payload.get("data") or []
    if not rows:
        return {"ok": False, "reason": "载荷里没有行情行"}
    subj_path, bench_path = d / "qlib_subjects.csv", d / "qlib_benchmark.csv"
    written, skipped = {"subjects": 0, "benchmark": 0}, 0
    with subj_path.open("w", encoding="utf-8", newline="") as fh_s, \
            bench_path.open("w", encoding="utf-8", newline="") as fh_b:
        ws = csv.DictWriter(fh_s, fieldnames=QLIB_COLUMNS)
        wb = csv.DictWriter(fh_b, fieldnames=QLIB_COLUMNS)
        ws.writeheader()
        wb.writeheader()
        for r in rows:
            code = str(r.get("code") or "")
            close = _f(r.get("close"))
            if not code or close is None:
                skipped += 1
                continue
            rec = {"date": str(r.get("date"))[:10], "instrument": code,
                   "open": _f(r.get("open")) if _f(r.get("open")) is not None else "",
                   "close": close,
                   "volume": r.get("volume") if r.get("volume") not in (None, "") else ""}
            if benchmark and code == str(benchmark):
                wb.writerow(rec)
                written["benchmark"] += 1
            elif not benchmark or code != str(benchmark):
                ws.writerow(rec)
                written["subjects"] += 1
    return {"ok": True, "schema": SCHEMA, "adapter": ADAPTER,
            "subjects_csv": str(subj_path), "benchmark_csv": str(bench_path),
            "rows": written, "skipped_no_close": skipped,
            "note": ("Qlib 未安装 ⇒ 这里只做**数据导出**（可测试、不依赖 qlib）；"
                     "真正跑 Qlib 需在独立环境用同一份 CSV 建 dataset")}


def run_via_qlib(*args, **kwargs) -> dict:
    """只有 Qlib 可用才委托运行；否则**明确不可用**（不产假结果、不吞参数）。"""
    ok, why = available()
    if not ok:
        return {"status": "unavailable", "adapter": ADAPTER, "reason": why}
    return {"status": "unavailable", "adapter": ADAPTER,
            "reason": ("qlib 已安装但本适配层的委托实现尚未编写：按纪律**不预先编造** Qlib 调用，"
                       "请先给出目标工作流（dataset/模型/回测配置），再按真实 API 补实现")}


def qlib_env_hint() -> str:
    env = os.environ.get("WEAVEMIND_QLIB_PYTHON") or ""
    return env or "（未设置 WEAVEMIND_QLIB_PYTHON：独立环境解释器路径，例如 %USERPROFILE%\\wm_qlibenv\\Scripts\\python.exe）"
