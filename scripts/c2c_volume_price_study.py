#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C2c（§18.14，2026-10-11）：把**已核的原表行**投影成冻结研究输入，量价两层闭合。

本脚本**不新写解析器**：量价/结构行由现役 `narrative_evidence.extract_volume_price`（确定性、
离线）从已准入的缓存年报原件 `ee4429eecac66f50` 抽出，抽出的正是原表里印着的那几行
（`line` 原句 ＋ `locator` 小节/字符区间 ＋ `text_sha256`）；本脚本只做三件事：

1. **投影**：把抽出的行按 `(主体, 年度, 单位, 维度范围)` 写成原预览器
   （`adapters.wind_import.preview_file`）可读的行——吨行与元行**分行**（`unit` 是行级的），
   分产品三行与公司合计行**分行**（`dimension` 只在分维度行上有意义），
   于是"公司收入 ÷ 白酒销量"这类**范围错配**在数据集层就能被识别；
   时间戳一律写材料自己的 `disclosed_at`/`available_at`（2026-04-28）——
   新比较数**不回填** 2025-04-30 特征截止的主卡；
2. **核对**：分产品三行必须与表内「营业收入合计」闭合（≤0.01 元），
   且表内合计必须与已投影的公司营业收入逐位一致（跨材料一致性）；不闭合就不写、报原因；
3. **组 spec**：以已通过的主卡 spec（`--base-study-id`，默认 C2a/C2b 主卡）为底，
   只改名字并加 `market_pin`（显式钉住实际用到的行情 dataset_id 与交易日历 calendar_id），
   然后走原入口 `study.run_study → write_study`（不新增引擎、不改旧研究）。

用法：
    python scripts/c2c_volume_price_study.py                 # 只打印将要写入的行与自检
    python scripts/c2c_volume_price_study.py --run           # 写输入目录并跑一次新研究
    python scripts/c2c_volume_price_study.py --run --out-study-dir tmp/research-studies
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_INPUT_DIR = ROOT / "tmp" / "q1-real-inputs"          # B1 已投影的四份缓存（只读）
DEFAULT_BASE_STUDY = "3913767fb6971c1c"                   # C2a/C2b 已通过主卡（只读）
OUT_INPUT_DIR = ROOT / "tmp" / "c2c-inputs"
DEFAULT_OUT_STUDY_DIR = ROOT / "tmp" / "research-studies"
MATERIAL_ID = "ee4429eecac66f50"
DOC_PATH = (ROOT / "evals" / "a2_official_chain_20260929" / "002304-2025" /
            "project" / "materials" / MATERIAL_ID / "doc.json")
ENTITY_ID, ENTITY = "002304.SZ", "洋河股份"
R3_VERSION = "r3_operating_projection/1.0.0"
PERIODS = (2025, 2024)                                    # 本期 / 上期（原表两列）
# 显式来源（架构师 §18.14）：实际被消费的行情数据集与交易日历 id，写进新候选 spec
MARKET_PIN = {
    "price_dataset_id": "3d6b4acb451abcf960c4132deb16c2a09169234b76dcb643958ceee855fcb148",
    "calendar_id": "49413c7d1c48ce4c59f8e63706705c28d4b66edc9d047ab381ea60bd600e5435",
    "note": ("C2c 显式来源：行情＝akshare 前复权离线集（2023-01-03..2026-09-30，2724 行）；"
             "交易日历＝akshare/sina 日历集（1990-12-19..2026-12-31，8797 个交易日）。"
             "两者由 study 生成路径按 id 真加载并记绑定指纹，不再按环境默认另取。"),
}
_MONEY_METRICS = (("revenue_product_baijiu", "白酒"),
                  ("revenue_product_hongjiu", "红酒"),
                  ("revenue_product_other", "其他"))


def _d(value) -> Decimal:
    return Decimal(str(value))


def _q(value) -> str:
    return str(_d(value).quantize(Decimal("0.01")))


def load_base() -> tuple[list[dict], dict, dict]:
    """B1 已投影的行与台账（只读复用，不重投影）。"""
    rows = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    ledger = json.loads((BASE_INPUT_DIR / "projection_ledger.json").read_text(encoding="utf-8"))
    spec = json.loads((BASE_INPUT_DIR / "study.json").read_text(encoding="utf-8"))
    return rows, ledger, spec


def extract_r3() -> tuple[dict, list[dict], dict]:
    """既有抽取器 → 原表行（含原句/定位/指纹）＋ 投影行 ＋ R3 台账。"""
    import narrative_evidence as ne

    doc = json.loads(DOC_PATH.read_text(encoding="utf-8"))
    vp = ne.extract_volume_price([doc], periods=list(PERIODS))
    if not vp.get("ok"):
        raise SystemExit(f"量价/结构行未取得（{vp.get('reason') or '未识别'}）：不投影、不编数")
    want = {("实物量", "白酒销售量"), ("实物量", "白酒生产量"), ("实物量", "白酒库存量"),
            ("分产品", "白酒"), ("分产品", "红酒"), ("分产品", "其他")}
    facts = {}
    for f in vp.get("facts") or []:
        key = (str(f.get("group")), str(f.get("row_label")))
        if key in want:
            facts[key] = f
    missing = sorted(f"{g}·{r}" for (g, r) in want if (g, r) not in facts)
    if missing:
        raise SystemExit(f"原表行缺失：{'、'.join(missing)}——只报缺口，不拿别的行顶")
    idx = {str(p): i for i, p in enumerate(vp.get("periods") or [])}      # [2025, 2024]
    disclosed = str(doc.get("disclosed_at") or doc.get("operator_disclosed_at") or "")
    url = str(doc.get("url") or "")
    text_sha = str(doc.get("text_sha256") or "")
    rows: list[dict] = []
    ledger: dict = {"schema": "weavemind.r3_operating_projection/1", "version": R3_VERSION,
                    "extractor": "narrative_evidence.extract_volume_price",
                    "material_id": MATERIAL_ID, "doc_path": str(DOC_PATH.relative_to(ROOT)),
                    "raw_sha256": str(doc.get("raw_sha256") or ""), "text_sha256": text_sha,
                    "url": url, "title": str(doc.get("title") or ""),
                    "disclosed_at": disclosed, "periods": list(vp.get("periods") or []),
                    "rows": [], "closure": {}, "cross_check": {}}
    commons = {"code": ENTITY_ID, "entity": ENTITY, "period_type": "年度", "period_basis": "累计",
               "disclosed_at": disclosed, "available_at": disclosed,
               "retrieved_at": str(doc.get("retrieved_at") or "")[:19],
               "currency": "CNY", "caliber": "合并", "restatement": "",
               "source": "official_disclosure_cache", "source_url": url,
               "material_ids": [MATERIAL_ID], "projection_version": R3_VERSION}
    for year in PERIODS:
        i = idx[str(year)]
        period = f"{year}-12-31"
        pstart = f"{year}-01-01"

        def _row(metrics: dict, unit: str, dimension: str, quote: str, locator: str,
                 stage: str, flow: bool, fids: list[str]) -> dict:
            row = {**commons, "period": period, "period_end": period,
                   "unit": unit, "dimension": dimension, "quote": quote, "locator": locator,
                   "table_name": stage, "orig_fact_ids": fids, "r3_row": len(rows) + 1}
            if flow:
                row["period_start"] = pstart
            row.update({k: float(v) for k, v in metrics.items()})   # 与 B1 投影同格式（float）
            rows.append(row)
            ledger["rows"].append({"period": period, "unit": unit, "dimension": dimension,
                                   "metrics": {k: _q(v) for k, v in metrics.items()},
                                   "quote": quote,
                                   "locator": locator, "stage": stage,
                                   "orig_fact_ids": fids, "row_number": len(rows)})
            return row

        for metric, label in _MONEY_METRICS:
            f = facts[("分产品", label)]
            _row({metric: _d(f["cur" if year == PERIODS[0] else "prev"])}, "元", f"分产品:{label}",
                 str(f.get("line") or ""), str(f.get("locator") or ""),
                 "（1）营业收入构成", True, [f"volume_price:{f.get('group')}:{f.get('row_label')}"])
        tot = dict(vp.get("total") or {})
        if tot.get("cur") is None or tot.get("prev") is None:
            raise SystemExit("表内「营业收入合计」未取得：闭合核对做不了，不投影")
        _row({"revenue_total_reported": _d(tot["cur" if year == PERIODS[0] else "prev"])},
             "元", "公司合计", str(tot.get("line") or ""),
             f"字符 {tot.get('char_start')}-{tot.get('char_end')}", "（1）营业收入构成",
             True, ["volume_price:total"])
        vol = {m: facts[("实物量", r)] for m, r in
               (("sales_volume_baijiu", "白酒销售量"), ("production_volume_baijiu", "白酒生产量"))}
        _row({m: _d(f["cur" if year == PERIODS[0] else "prev"]) for m, f in vol.items()},
             "吨", "分产品:白酒",
             "；".join(str(f.get("line") or "") for f in vol.values()),
             "；".join(str(f.get("locator") or "") for f in vol.values()),
             "（1）主要产品的生产量、销售量、库存量", True,
             [f"volume_price:实物量:{f.get('row_label')}" for f in vol.values()])
        inv = facts[("实物量", "白酒库存量")]
        _row({"inventory_volume_baijiu": _d(inv["cur" if year == PERIODS[0] else "prev"])},
             "吨", "分产品:白酒", str(inv.get("line") or ""), str(inv.get("locator") or ""),
             "（1）主要产品的生产量、销售量、库存量", False,
             [f"volume_price:实物量:{inv.get('row_label')}"])
        # 组内闭合：分产品三行 = 表内合计
        parts = sum((_d(facts[("分产品", lab)]["cur" if year == PERIODS[0] else "prev"])
                     for _, lab in _MONEY_METRICS), Decimal("0"))
        total = _d(tot["cur" if year == PERIODS[0] else "prev"])
        gap = parts - total
        ledger["closure"][str(year)] = {"products_sum": _q(parts), "total_reported": _q(total),
                                        "gap_yuan": _q(gap), "closed": abs(gap) <= Decimal("0.01")}
    # 跨材料一致性：新投影的表内合计必须等于已投影的公司营业收入（同一主体同一期间）
    base = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    for year in PERIODS:
        old = next((r for r in base if r.get("code") == ENTITY_ID
                    and str(r.get("period") or "").startswith(str(year))
                    and r.get("revenue") is not None), None)
        new = next(r for r in rows if r.get("code") == ENTITY_ID
                   and str(r.get("period") or "").startswith(str(year))
                   and r.get("revenue_total_reported") is not None)
        if old is None:
            ledger["cross_check"][str(year)] = {"status": "no_base_row"}
            continue
        gap = _d(new["revenue_total_reported"]) - _d(old["revenue"])
        ledger["cross_check"][str(year)] = {"base_revenue": _q(old["revenue"]),
                                            "r3_total_reported": _q(new["revenue_total_reported"]),
                                            "gap_yuan": _q(gap),
                                            "consistent": abs(gap) <= Decimal("0.01")}
    return vp, rows, ledger


def build_spec(base: dict) -> dict:
    """以已通过主卡 spec 为底：只改名与来源钉住，其余字段（含两个截止、网格、天数）逐字保留。"""
    spec = json.loads(json.dumps(base, ensure_ascii=False))
    spec["name"] = ("C2c：洋河/三一 2024 经营机制卡＋利润目标×天数冲击 3×3 网格"
                    "（官方缓存年报；特征截止 2025-04-30；新增原表量价/分产品行＋显式来源钉住）")
    spec["market_pin"] = dict(MARKET_PIN)
    return spec


def write_inputs(rows: list[dict], ledger: dict, spec: dict, *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_rows = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    payload = {"metadata": {"source": "official_disclosure_cache", "synthetic": False,
                            "projection_version": "q1_projection/1.0.0",
                            "r3_projection_version": R3_VERSION,
                            "r3_note": "原表量价/分产品行由 extract_volume_price 投影，见 r3_ledger.json"},
               "rows": list(base_rows) + list(rows)}
    (out_dir / "financials.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "study.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (out_dir / "r3_ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    (out_dir / "projection_ledger.json").write_text(
        (BASE_INPUT_DIR / "projection_ledger.json").read_text(encoding="utf-8"), encoding="utf-8")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="写输入目录并跑一次新研究")
    ap.add_argument("--base-study-id", default=DEFAULT_BASE_STUDY)
    ap.add_argument("--out-input-dir", default=str(OUT_INPUT_DIR))
    ap.add_argument("--out-study-dir", default=str(DEFAULT_OUT_STUDY_DIR))
    a = ap.parse_args(argv)

    base_rows, base_ledger, _ = load_base()
    frozen = json.loads((Path(a.out_study_dir) / a.base_study_id / "inputs.json")
                        .read_text(encoding="utf-8"))
    spec = build_spec(frozen["spec"])
    vp, rows, ledger = extract_r3()
    bad = [k for k, v in ledger["closure"].items() if not v["closed"]]
    bad += [k for k, v in ledger["cross_check"].items() if v.get("consistent") is False]
    print(f"底稿行 {len(base_rows)} ＋ 原表行 {len(rows)} ＝ {len(base_rows) + len(rows)}")
    print(f"分产品闭合：{json.dumps(ledger['closure'], ensure_ascii=False)}")
    print(f"跨材料一致：{json.dumps(ledger['cross_check'], ensure_ascii=False)}")
    print(f"材料 {MATERIAL_ID} text_sha256 {ledger['text_sha256'][:16]}… 披露 {ledger['disclosed_at']}")
    print(f"spec 名字：{spec['name']}")
    print(f"来源钉住：行情 {MARKET_PIN['price_dataset_id'][:16]}… 日历 {MARKET_PIN['calendar_id'][:16]}…")
    if bad:
        print(f"自检未过（{bad}）：停止，不写输入")
        return 2
    if not a.run:
        print("（未写盘；加 --run 才会写输入目录并跑新研究）")
        return 0
    out = write_inputs(rows, ledger, spec, out_dir=Path(a.out_input_dir))
    print(f"输入目录 {out}")
    from quant_research import study as qstudy
    result = qstudy.run_study(spec, input_dir=out)
    target = qstudy.write_study(result, output_dir=Path(a.out_study_dir))
    mb = (result.get("quant") or {}).get("market_binding") or {}
    print(f"研究ID {result['study_id']}")
    print(f"result_hash {result['result_hash']}")
    print(f"目录 {target}")
    print(f"来源绑定 status={mb.get('status')} 行情来源={mb.get('price_dataset_id_source')} "
          f"日历来源={mb.get('calendar_id_source')}")
    print("RESULT_JSON " + json.dumps({"study_id": result["study_id"],
                                       "result_hash": result["result_hash"],
                                       "dir": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
