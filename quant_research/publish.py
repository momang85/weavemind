# -*- coding: utf-8 -*-
"""量化读数**入包与许可门**（Y2 接入）：读数怎么随交付包走、谁能对外。

架构要求的接入方式是**附件**，不是改写主文：
规划 §11.2 说"先接正常选版/同版附件，旧主文只有限保留必要结论与边界"。
所以这里只做三件事：

1. **落盘**：读数写进工作区 `quant/`（`event_returns.json` / `backtest.json` / …）；
2. **随包**：`payload_bytes(ws)` 交出 `{"quant/…": bytes}`，与 `analysis/*` 同一套"有才进包、
   不制造空文件"契约 ⇒ **没有量化读数的工作区，交付包内容一字不变**（零存量影响）；
3. **许可门**：`external_delivery_gate()` 按每条读数自带的 `delivery_eligible` 判"能否对外"，
   免费源/个人非商业一律 `allowed=False` 并写原因；结果写进 `quant/quant_manifest.json`。

另给 `detail_markdown()`：**数值留底稿**（正文只放结论）。底稿里每个数字都带
`input_kind` 绑定与 `reading_hash`，可离线复算。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

QUANT_DIR = "quant"
MANIFEST = "quant/quant_manifest.json"
DETAIL = "quant/quant_detail.md"
SCHEMA = "weavemind.quant_package/0"

# 读数文件名（工作区 `quant/` 下）；键是稳定标识，值是文件名
READINGS = {"event_returns": "quant/event_returns.json", "backtest": "quant/backtest.json"}


def quant_dir(ws) -> Path:
    return Path(ws) / QUANT_DIR


def _atomic_write(path: Path, blob: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def store_reading(ws, name: str, reading: dict) -> dict:
    """把一条读数写进工作区（`quant/<name>.json`）；名字不认识就拒，避免随手堆文件。"""
    if name not in READINGS:
        return {"ok": False, "reason": f"未登记的读数名：{name}（允许：{sorted(READINGS)}）"}
    d = quant_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    arc = READINGS[name]
    p = Path(ws) / arc
    _atomic_write(p, json.dumps(reading, ensure_ascii=False, indent=1).encode("utf-8"))
    return {"ok": True, "path": str(p), "arcname": arc}


def load_readings(ws) -> dict:
    """读回工作区里的读数（坏文件跳过并记录，不让一个坏文件毁掉整包导出）。"""
    out: dict = {}
    bad: list[str] = []
    for name, arc in READINGS.items():
        p = Path(ws) / arc
        if not p.is_file():
            continue
        try:
            out[name] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            bad.append(arc)
    if bad:
        out["_unreadable"] = bad
    return out


# ---------------------------------------------------------------- 许可门

def external_delivery_gate(readings: dict) -> dict:
    """能不能对外：**每条读数各自判定**，任一不可对外则整体 `allowed=False`。

    判定用读数自带的 `delivery_eligible`（由算子按价格许可算出来），这里**不重新解释许可**，
    也不因为"我们内部看过了"就放行。没有读数 ⇒ `allowed=False` + `reason=没有量化读数`
    （不默认放行）。
    """
    real = {k: v for k, v in (readings or {}).items()
            if isinstance(v, dict) and not k.startswith("_")}
    if not real:
        return {"allowed": False, "reason": "没有量化读数：不产对外许可结论（不默认放行）",
                "blocked_by": []}
    blocked, reasons = [], []
    for name, r in sorted(real.items()):
        lic = str((r.get("input_kind") or {}).get("license")
                  or r.get("license") or "") or "未标注"
        if r.get("delivery_eligible") is True:
            continue
        blocked.append(name)
        reasons.append(f"{name}：许可「{lic}」⇒ 仅内部试验"
                       + (f"（{r.get('delivery_block_reason')}）" if r.get("delivery_block_reason")
                          else ""))
    if blocked:
        return {"allowed": False, "blocked_by": blocked,
                "reason": "存在不可对外的量化读数：" + "；".join(reasons)
                          + "。对外交付须机构授权或只交付衍生结论，原始数据不得随包外发",
                "readings_checked": sorted(real)}
    return {"allowed": True, "blocked_by": [], "readings_checked": sorted(real),
            "reason": "全部读数的价格许可允许对外"}


# ---------------------------------------------------------------- 随包字节

def manifest(ws) -> dict:
    """包内清单：每条读数一行摘要 + 输入绑定 + 许可门结论（离线可核对）。"""
    readings = load_readings(ws)
    rows = []
    for name, r in sorted((readings or {}).items()):
        if name.startswith("_") or not isinstance(r, dict):
            continue
        rows.append({
            "reading": name,
            "operator": r.get("operator"),
            "impl_version": r.get("impl_version"),
            "status": r.get("status"),
            "input_kind": r.get("input_kind") or {
                "input_kind": "market_history",
                "dataset_id": (r.get("spec") or {}).get("dataset_id"),
                "license": r.get("license")},
            "input_fingerprint": r.get("input_fingerprint"),
            "spec_hash": r.get("spec_hash"),
            "reading_hash": r.get("reading_hash"),
            "delivery_eligible": r.get("delivery_eligible"),
            "what_this_is_not": [x for x in (r.get("limits") or [])[:2]],
        })
    return {"schema": SCHEMA, "readings": rows,
            "count": len(rows),
            "unreadable": readings.get("_unreadable") or [],
            "external_delivery": external_delivery_gate(readings),
            "note": ("量化读数以**附件**形式随包（主文不改写）；每条读数带输入绑定与哈希，"
                     "可离线复算；许可门结论见 external_delivery")}


def detail_markdown(ws) -> str:
    """底稿文本：**数值留底稿**（正文只放结论）。没有读数就返回空串（不制造空文件）。"""
    readings = load_readings(ws)
    real = {k: v for k, v in (readings or {}).items()
            if isinstance(v, dict) and not k.startswith("_")}
    if not real:
        return ""
    gate = external_delivery_gate(readings)
    out = ["# 量化读数底稿（Y2）", "",
           "> 本节是**底稿**：数字放这里，正文只放结论。每条读数带输入绑定与哈希，可离线复算。",
           "> **不是因果结论、不是 alpha**；许可门结论见文末。", ""]
    er = real.get("event_returns")
    if er:
        out += ["## 事件窗口基准调整收益（观察值，不可执行）", ""]
        ik = er.get("input_kind") or {}
        out += [f"- 输入绑定：`{ik.get('input_kind')}` dataset `{str(ik.get('dataset_id'))[:16]}…`"
                f"，口径 {ik.get('adj_basis')}，许可 {ik.get('license')}",
                f"- 输入指纹 `{er.get('input_fingerprint')}`，读数哈希 `{er.get('reading_hash')}`", ""]
        out += ["| 事件 | 窗口 | t+0 | t+1 | t+5 | t+20 |", "|---|---|---|---|---|---|"]
        for r in er.get("readings") or []:
            pts = {p["offset"]: p for p in r.get("points") or []}
            def g(off):
                p = pts.get(off) or {}
                v = p.get("excess_return")
                if v is None:
                    return {"pending": "待成熟", "missing": "缺行情"}.get(p.get("state") or "", "—")
                return f"{v * 100:+.2f}%"
            out.append(f"| {r.get('label')}（{r.get('event_date')}） | "
                       f"{'→'.join(r.get('affected_window') or [])} | "
                       f"{g(0)} | {g(1)} | {g(5)} | {g(20)} |")
        out += ["", "（超额＝几何基准调整；`t0`＝披露日之后第一个交易日；窗口重叠的事件不进聚合）", ""]
    bt = real.get("backtest")
    if bt:
        m = bt.get("metrics") or {}
        out += ["## 组合回测账本（固定规则，无拟合参数）", ""]
        ik = bt.get("input_kind") or {}
        out += [f"- 输入绑定：dataset `{str(ik.get('dataset_id'))[:16]}…`，"
                f"口径 {ik.get('adj_basis')}，许可 {ik.get('license')}",
                f"- 输入指纹 `{bt.get('input_fingerprint')}`，规格哈希 `{bt.get('spec_hash')}`，"
                f"读数哈希 `{bt.get('reading_hash')}`",
                f"- 区间 {m.get('start')} → {m.get('end')}（{m.get('days')} 个交易日），"
                f"成交 {m.get('orders')} 笔",
                f"- 总收益 **{m.get('total_return')}**（不含成本 {m.get('nocost_total_return')}，"
                f"成本拖累 {m.get('cost_drag')}）；年化 {m.get('annualized_return')}",
                f"- 最大回撤 **{m.get('max_drawdown')}**；平均敞口 {m.get('avg_exposure')}；"
                f"换手 {m.get('turnover_ratio')}",
                f"- 基准 {m.get('benchmark', {}).get('return')} ⇒ 超额 "
                f"{m.get('benchmark', {}).get('excess_return')}",
                f"- 账本审计 {'通过' if (bt.get('audit') or {}).get('ok') else '未通过 → 结果不可用'}", ""]
    out += ["## 许可门（决定这份包能不能对外）", "",
            f"- 对外分发：**{'允许' if gate['allowed'] else '不允许'}**",
            f"- 结论：{gate['reason']}", ""]
    return "\n".join(out) + "\n"


def payload_bytes(ws) -> dict:
    """交付快照要收的成员：`quant/*`。**没有量化读数就返回空字典**（包内容一字不变）。"""
    d = quant_dir(ws)
    if not d.is_dir():
        return {}
    read = load_readings(ws)
    real = [k for k in (read or {}) if not k.startswith("_")]
    if not real:
        return {}
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "quant_manifest.json",
                  json.dumps(manifest(ws), ensure_ascii=False, indent=1).encode("utf-8"))
    md = detail_markdown(ws)
    if md:
        _atomic_write(d / "quant_detail.md", md.encode("utf-8"))
    out: dict = {}
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.name.endswith(".tmp"):
            out[f"{QUANT_DIR}/{p.name}"] = p.read_bytes()
    return out


def summary() -> dict:
    return {"schema": SCHEMA, "readings": sorted(READINGS), "quant_dir": QUANT_DIR,
            "manifest": MANIFEST, "detail": DETAIL}
