# -*- coding: utf-8 -*-
"""`market_history`（量化域读侧）：**自由数据源抓取 → 本地文件 → 校验后进研究**。

分工（架构约束：基础研究不被量化环境拖累、不在主环境装数据引擎）：

    抓取侧（可选环境，独立 venv）  `scripts/fetch_market_history.py`
        akshare（免费、无 token）／tushare 免费档（120 积分=仅非复权日线，需 `TUSHARE_TOKEN`）
        → 写原始文件 + 侧车 meta（来源/许可/采集时间/字段）
    读侧（本模块，**只用标准库**）  归一化 → 校验 → `market_history/0` 载荷
        → 落 `<dir>/<dataset_id>/{rows.csv,meta.json}`，`dataset_id = sha256(原始文件字节)`

纪律：
- **`available_at` 逐行必填**（防未来函数）：事件窗口只允许取 `available_at` 晚于事件日的行。
- **许可随数据走**：`license`（个人非商业/机构授权/免费源·仅内部试验/未知）。免费源与个人档
  **不得**进对外下载包；`unknown` 许可按"仅内部试验"处理并显式标注。
- 缺数据**不猜**：没有文件/字段不全时返回 `unavailable` + 原因，绝不返回 0 或用快照拼历史。
- 复权口径必须写明（前复权/后复权/不复权）；缺 `adj_factor` 时只报未复权并标注。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "weavemind.market_history/0"
CAPABILITY = "market_history"
REQUIRED_COLUMNS = ("code", "date", "close", "available_at", "source")
OPTIONAL_COLUMNS = ("open", "high", "low", "volume", "amount", "adj_factor", "role")
LICENSE_FREE_TRIAL = "免费源·仅内部试验"
LICENSE_PERSONAL = "个人非商业"
LICENSE_INSTITUTIONAL = "机构授权"


def data_dir() -> Path:
    """数据落地目录：`WEAVEMIND_MARKET_DIR` → 否则 `<工作区根>/../market_history`。"""
    env = str(os.environ.get("WEAVEMIND_MARKET_DIR") or "").strip()
    if env:
        return Path(env)
    try:
        import workspace as _ws
        return Path(_ws.WORKSPACE_ROOT).parent / "market_history"
    except Exception:                                    # noqa: BLE001
        return Path.cwd() / "market_history"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_date(v) -> str:
    s = str(v or "").strip()
    for sep in ("-", "/", "."):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            if len(parts) >= 3:
                return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return s[:10] if len(s) >= 8 and s[:4].isdigit() else ""


def _num(v):
    s = str(v if v is not None else "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_rows(path: Path) -> tuple[list[dict], str]:
    """原始文件 → 行（CSV/JSON，标准库读；表头缺失即空）。"""
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                         # noqa: BLE001
            return [], f"JSON 解析失败：{str(exc)[:80]}"
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        return [r for r in (rows or []) if isinstance(r, dict)], ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)], ""
    except Exception as exc:                             # noqa: BLE001
        return [], f"CSV 解析失败：{str(exc)[:80]}"


def normalize(raw_rows: list[dict], *, source: str, license: str,
              adj_basis: str = "") -> tuple[list[dict], list[str]]:
    """原始行 → 契约行；不合规行**逐行记录原因**（不静默丢）。"""
    rows: list[dict] = []
    issues: list[str] = []
    for i, r in enumerate(raw_rows, 1):
        low = {str(k).strip().lower(): v for k, v in r.items()}
        out = {
            "code": str(low.get("code") or low.get("ts_code") or low.get("证券代码") or "").strip(),
            "date": _norm_date(low.get("date") or low.get("trade_date") or low.get("日期")),
            "close": _num(low.get("close") or low.get("收盘")),
            "available_at": str(low.get("available_at") or "").strip(),
            "source": str(low.get("source") or source or "").strip(),
            "adj_factor": _num(low.get("adj_factor")),
            "role": str(low.get("role") or "").strip(),
        }
        for k in ("open", "high", "low", "volume", "amount"):
            out[k] = _num(low.get(k) or low.get({"volume": "vol"}.get(k, k)))
        missing = [c for c in ("code", "date", "close", "available_at", "source") if not out[c]]
        if missing:
            issues.append(f"第 {i} 行缺 {'/'.join(missing)}：{str(r)[:80]}")
            continue
        rows.append(out)
    if adj_basis:
        for r in rows:
            r["adj_basis"] = str(adj_basis)
    return rows, issues


def import_file(path, *, source: str = "", license: str = LICENSE_FREE_TRIAL,
                adj_basis: str = "", extra: dict | None = None) -> dict:
    """原始文件 → 契约数据集（`<dir>/<dataset_id>/{rows.csv,meta.json}`）。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "reason": f"文件不存在：{p}"}
    blob = p.read_bytes()
    dataset_id = hashlib.sha256(blob).hexdigest()
    raw_rows, err = _read_rows(p)
    if err:
        return {"ok": False, "reason": err, "dataset_id": dataset_id}
    if not raw_rows:
        return {"ok": False, "reason": "文件里没有数据行", "dataset_id": dataset_id}
    rows, issues = normalize(raw_rows, source=source, license=license, adj_basis=adj_basis)
    if not rows:
        return {"ok": False, "reason": "全部行都不合契约（见 issues）", "issues": issues,
                "dataset_id": dataset_id}
    d = data_dir() / dataset_id
    d.mkdir(parents=True, exist_ok=True)
    cols = ["code", "date", "open", "high", "low", "close", "volume", "amount",
            "adj_factor", "available_at", "source", "role", "adj_basis"]
    with (d / "rows.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    meta = {"schema": SCHEMA, "dataset_id": dataset_id, "source": str(source or ""),
            "license": str(license or "unknown"), "adj_basis": str(adj_basis or ""),
            "imported_at": _now(), "raw_file": p.name, "raw_bytes": len(blob),
            "raw_sha256": dataset_id, "rows": len(rows), "issues": issues[:50],
            "instruments": sorted({r["code"] for r in rows}),
            "date_range": [min(r["date"] for r in rows), max(r["date"] for r in rows)],
            "unavailable": []}
    if extra:
        meta.update(dict(extra))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return {"ok": True, "dataset_id": dataset_id, "dir": str(d), "meta": meta,
            "issues": issues}


def load(dataset_id: str = "") -> dict:
    """读最近/指定数据集 → `market_history/0` 载荷；缺数据返回 `unavailable`。"""
    base = data_dir()
    if not base.is_dir():
        return {"schema": SCHEMA, "source": "unavailable",
                "reason": f"没有数据目录（{base}）：先跑 scripts/fetch_market_history.py 或用 import_file 导入",
                "rows": [], "unavailable": [{"what": "rows", "reason": "无数据目录"}]}
    dirs = ([base / dataset_id] if dataset_id else
            sorted([d for d in base.iterdir() if (d / "meta.json").is_file()],
                   key=lambda d: (d / "meta.json").stat().st_mtime, reverse=True))
    for d in dirs:
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        rows, err = _read_rows(d / "rows.csv")
        if err or not rows:
            continue
        return {"schema": SCHEMA, "dataset_id": meta.get("dataset_id"),
                "source": meta.get("source"), "license": meta.get("license"),
                "adj_basis": meta.get("adj_basis"), "imported_at": meta.get("imported_at"),
                "instruments": meta.get("instruments"), "date_range": meta.get("date_range"),
                "rows": int(meta.get("rows") or len(rows)), "issues": meta.get("issues") or [],
                "unavailable": [], "data": rows}
    return {"schema": SCHEMA, "source": "unavailable", "reason": "没有可用数据集",
            "rows": [], "unavailable": [{"what": "rows", "reason": "没有可用数据集"}]}


def window(payload: dict, code: str, event_date: str, *, offsets=(-1, 0, 1, 5, 20),
           benchmark: str = "") -> dict:
    """事件窗口切片：只取 `available_at > 事件日` 之后的行（防未来函数），基准同窗口对齐。

    `offsets` 以**交易日序号**计（数据集内该标的的交易日序列），不按自然日。
    """
    rows = [r for r in (payload.get("data") or []) if str(r.get("code")) == str(code)]
    rows.sort(key=lambda r: str(r.get("date")))
    dates = [str(r.get("date")) for r in rows]
    if not dates:
        return {"ok": False, "reason": f"数据集里没有 {code} 的行"}
    ev = _norm_date(event_date)
    after = [i for i, d in enumerate(dates) if d > ev]
    if not after:
        return {"ok": False, "reason": f"{code} 在事件日 {ev} 之后没有交易日行"}
    base = after[0]
    out = {"ok": True, "code": code, "event_date": ev, "base_index": base,
           "future_filter": "只取 date > 事件日 的行（available_at 缺行时以 date 兜底并标注）",
           "points": {}}
    for off in offsets:
        i = base + int(off)
        if 0 <= i < len(rows):
            out["points"][f"t{off:+d}"] = {"date": dates[i], "close": rows[i].get("close"),
                                           "adj_factor": rows[i].get("adj_factor")}
        else:
            out["points"][f"t{off:+d}"] = {"date": None, "close": None}
    if benchmark:
        b = window(payload, benchmark, ev, offsets=offsets)
        out["benchmark"] = {"code": benchmark, "ok": b.get("ok"),
                            "points": b.get("points") if b.get("ok") else None,
                            "reason": b.get("reason") if not b.get("ok") else ""}
    return out
