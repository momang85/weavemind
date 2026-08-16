# -*- coding: utf-8 -*-
"""LLM-as-Judge 校准（对标标准 C3-3.6 偏见警示）。

用黄金测试集（人工打分）校准自动 judge：
- 平均绝对误差（MAE）：judge 分数与人工分数的差距
- 通过/失败一致率：以阈值判定 pass/fail 的一致性
- 报告写入 evals/calibration_report.json
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

METRICS = ("answer_correctness", "faithfulness", "context_recall", "context_precision")


def load_golden(path: str | None = None) -> list[dict]:
    p = Path(path) if path else Path(__file__).parent / "golden.json"
    return json.loads(p.read_text(encoding="utf-8"))


def calibrate(path: str | None = None, threshold: float = 0.7) -> dict:
    from evals.judge import score_record

    golden = load_golden(path)
    rows = []
    for g in golden:
        auto = score_record({
            "question": g["question"],
            "answer": g["answer"],
            "contexts": g["contexts"],
            "ground_truth": g["ground_truth"],
        })
        human = {m: float(g["human_scores"].get(m, 0.0)) for m in METRICS}
        row = {"id": g["id"], "auto": auto, "human": human}
        row["mae"] = round(
            sum(abs(auto.get(m, 0.0) - human[m]) for m in METRICS) / len(METRICS), 3
        )
        row["pass"] = sum(auto.values()) / len(METRICS) >= threshold
        row["human_pass"] = sum(human.values()) / len(METRICS) >= threshold
        rows.append(row)

    mae = round(sum(r["mae"] for r in rows) / len(rows), 3) if rows else 1.0
    agree = (
        round(sum(1 for r in rows if r["pass"] == r["human_pass"]) / len(rows), 3)
        if rows else 0.0
    )
    report = {
        "threshold": threshold,
        "n": len(rows),
        "mae": mae,
        "pass_agreement": agree,
        "rows": rows,
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S", __import__("time").gmtime()),
    }
    (Path(__file__).parent / "calibration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info("Judge calibration: MAE=%.3f, pass agreement=%.2f", mae, agree)
    return report


if __name__ == "__main__":
    import sys
    rep = calibrate()
    print(json.dumps({
        "n": rep["n"], "mae": rep["mae"], "pass_agreement": rep["pass_agreement"],
    }, ensure_ascii=False))
    sys.exit(0 if rep["mae"] <= 0.5 else 1)
