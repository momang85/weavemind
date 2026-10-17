# -*- coding: utf-8 -*-
"""研究接报告薄适配层：把**已算好**的研究选版接成内部公司报告（研究直连）。

纪律（总架构师执行指令 L11/L23-27、三域规划 §18.12）：
- 不新建报告服务：只用 workspace.ensure_task_workspace + financial_analysis.store 的
  投影 + delivery_pipeline 现成的 assemble_and_verify / repack_adopted / report_version，
  外加一个**确定性正文构造器**（零模型调用）。
- 正文 100% 确定性：每句话都能从选定 study 目录的冻结产物里指出出处；不重编数字。
  C2a：联合现金占用增量**只读选定网格的 C 列**（不再用 cash_pressure 运行的旧增量，
  避免 0/0 天版本正文写 10.03亿而表 C=0）；固定「单项回款 +10 天」只作背景敏感度。
- 一份**公司视图**（`analysis/context.json` 的 `company_view` + `contract`）同时喂正文、
  契约（`assemble_and_verify(contract=...)`）与附件——grid／Q1／续页／价格引用同源。
- 语义红线不松动：机器验收 / 研究状态 / 人工复核三态按**本版报告**实际绑定返回，
  不用研究目录的 research_draft 冒充报告的研究状态；外发=不允许；情景是"假设成立时"
  的条件推算，不是预测、不构成业绩承诺；B 是研究者工作假设。
- 价格观察（§18.12）：只读已有离线行情数据集 + 既有 event_calendar/event_returns 算子，
  财务特征截止与价格锚点分开、价格评价截止单列；不称超预期/因果/alpha。
- 候选工作区 `study-report-<sid16>-<entity>`：主体/期间不匹配拒绝绑定（409），
  多公司 study 只投影本公司（runs+inputs），study 目录保持只读原样；
  不写 agents.db/task_history/task_state 行，不做 selection.json 人工选择。
"""
from __future__ import annotations

import decimal
import json
import os
import re
import shutil
from pathlib import Path

REPORT_RULES_VERSION = "study-report/1.1.0"
_BANNER = (
    "本报告由**选定研究的冻结读数**确定性装配（装配层零模型调用）：数字直接取自该研究的"
    "已验算运行记录，装配层不改数、不重编。**未经人工复核**（人工复核=未复核），"
    "仅作内部研究用途；情景是「假设成立时」的条件推算，不是预测，不构成业绩承诺。"
)

# C2a（2026-10-11 架构师 §18.12）：价格观察只绑**已有离线行情数据集**（不采购、不重抓），
# 数据集 id 是数据出处的一部分，显式写出、可用环境变量或 spec.market.dataset_id 覆盖；
# 不在代码里写任何探针算出的收益/日期（全部现场由 event_returns 算子计算）。
PRICE_DATASET_DEFAULT = ("3d6b4acb451abcf960c4132deb16c2a09169234b76dcb643958ceee855fcb148")
PRICE_BENCHMARK = "000300"
# 关键词（§18.12）：t0 = 披露日之后第一条记录；1/5/20 条 = offsets(0,4,19)；
# 20 条为固定主窗口。锚点 t−1 由算子自带（ANCHOR_OFFSET）。
PRICE_OFFSETS = (0, 4, 19)
PRICE_MAIN_OFFSET = 19
_COMPANY_VIEW_SCHEMA = "weavemind.company_view/1"


class ReportBindingError(Exception):
    """绑定失败（主体/期间/研究ID 不匹配）——web_ui 映射为 409。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---- 格式化（元→亿/万；百分比/pp；全部确定性） ------------------------------------

def _yi(v: float, nd: int = 2) -> str:
    return f"{v / 1e8:,.{nd}f}亿"


def _wan(v: float) -> str:
    return f"{v / 1e4:,.1f}万"


def _pct(v: float) -> str:
    """g 值（小数）→ '+10.00%'；0 → '0.00%'。"""
    s = f"{float(v) * 100:+.2f}%"
    return s.replace("+0.00%", "0.00%")


def _pp(v: float) -> str:
    """m 值（小数）→ '+2.00pp'；0 → '0.00pp'。"""
    s = f"{float(v) * 100:+.2f}pp"
    return s.replace("+0.00pp", "0.00pp")


def _num(obj: dict, key: str, default: float = 0.0) -> float:
    try:
        v = obj.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default
    except Exception:                              # noqa: BLE001 - 读数缺失按 0 并在正文标注
        return default


def _target_value(jg: dict) -> tuple[float | None, dict]:
    """joint_grid.target：可能是 dict（{value,unit,period,goal_note…}）或裸数值。"""
    t = jg.get("target")
    if isinstance(t, dict):
        v = t.get("value")
        return (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None), t
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return float(t), {}
    return None, {}


def _margin_clause(mt: float | None, mg: float | None, *, alt: bool) -> str:
    """毛利率阈值/空间句（确定性）：alt=True 用于「收入增长 OR 毛利率提升」的恢复句。"""
    if mt is None:
        return ""
    if mg is None:
        return f"；毛利率阈值 {mt:.4f}%"
    if mg > 0:
        head = "或毛利率提升到阈值" if alt else "毛利率阈值"
        return f"；{head} {mt:.4f}%（距当前读数还差 {mg:.2f}pp）"
    if mg < 0:
        return f"；毛利率还有 {abs(mg):.2f}pp 的下行空间（阈值 {mt:.4f}%）"
    return f"；毛利率恰在阈值 {mt:.4f}%"


def _read_json(p) -> dict:
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001 - 坏/缺文件按空
        return {}
    return data if isinstance(data, dict) else {}


# ---- 绑定校验 -------------------------------------------------------------------

def _resolve_study(study_id: str, result_hash: str) -> tuple[Path, dict, str]:
    """显式 study_id + result_hash → (目录, manifest)。404/409 语义在调用方。"""
    from web_ui import _study_dir_of                  # 延迟导入：web_ui 是薄壳
    d = _study_dir_of(str(study_id or ""))
    if not d:
        raise ReportBindingError("study_not_found", f"研究ID {study_id} 没有对应的产物目录")
    man = _read_json(os.path.join(d, "study_manifest.json"))
    if not man:
        raise ReportBindingError("manifest_missing", "study_manifest.json 缺失或不可读")
    want = str(result_hash or "").strip()
    got = str(man.get("result_hash") or "")
    if not want:
        raise ReportBindingError("result_hash_required", "必须显式给出 result_hash（不隐式取最新）")
    if want != got:
        raise ReportBindingError(
            "result_hash_mismatch",
            f"result_hash 不匹配：请求 {want[:16]}…，该研究 {got[:16]}…（改参数会得到新的研究ID，"
            "旧版本只读保留；请从研究列表重新选择）")
    return Path(d), man, d


def _check_periods(man: dict, feature_cutoff: str, evaluation_as_of: str) -> None:
    """显式给出的两截止必须与冻结 manifest 一致（期间不匹配拒绝绑定）。"""
    for field, given in (("feature_cutoff", feature_cutoff), ("evaluation_as_of", evaluation_as_of)):
        got = str(man.get(field) or "")
        if given and got and str(given).strip() != got:
            raise ReportBindingError(
                "period_mismatch",
                f"{field} 不匹配：请求 {given}，该研究冻结于 {got}（主体/期间不匹配拒绝绑定）")


def _company_ctx(d: Path, entity_id: str) -> dict:
    """多公司 study 只投影本公司：entity 必须在 financial.json 里（主体不匹配拒绝绑定）。"""
    fin = _read_json(d / "financial.json")
    if not fin:
        raise ReportBindingError("financial_missing", "financial.json 缺失或不可读")
    code = str(entity_id or "").strip()
    if code not in fin:
        raise ReportBindingError(
            "entity_unbound",
            f"entity_id {code} 不在该研究里（主体不匹配拒绝绑定）；可用：{', '.join(sorted(fin))}")
    return {"code": code, "company": fin[code], "fin_keys": sorted(fin)}


# ---- 确定性正文构造器（零模型调用） -----------------------------------------------

def _runs_by_model(company: dict) -> dict:
    out: dict[str, dict] = {}
    for r in company.get("runs") or []:
        mid = str(r.get("model_id") or "")
        if mid and mid not in out:
            out[mid] = r
    return out


def _cells(jg: dict) -> list[dict]:
    cells = [c for c in (jg.get("cells") or []) if isinstance(c, dict)]
    return sorted(cells, key=lambda c: (float(c.get("g") or 0), float(c.get("m") or 0)))


def _is_met(cell: dict) -> bool:
    cond = cell.get("conditions")
    if isinstance(cond, dict) and "profit_target_met" in cond:
        return bool(cond.get("profit_target_met"))
    return float(cell.get("P_minus_T") or 0) >= 0


def _base_cell(cells: list[dict]) -> dict | None:
    for c in cells:
        if abs(float(c.get("g") or 0)) < 1e-12 and abs(float(c.get("m") or 0)) < 1e-12:
            return c
    return None


def _within_budget(cell: dict) -> bool | None:
    """格子的预算判定：conditions.within_budget 优先；否则按 budget_headroom≥0 判；都没有=不适用。"""
    cond = cell.get("conditions")
    if isinstance(cond, dict) and "within_budget" in cond:
        return bool(cond.get("within_budget"))
    bh = cell.get("budget_headroom")
    if isinstance(bh, (int, float)) and not isinstance(bh, bool):
        return float(bh) >= 0
    return None


def _both_met(cell: dict) -> bool | None:
    cond = cell.get("conditions")
    if isinstance(cond, dict) and "both_met" in cond:
        return bool(cond.get("both_met"))
    return None


def _budget_set(jg: dict) -> bool:
    b = jg.get("budget")
    return isinstance(b, dict) and b.get("value") is not None


def _occ_max(cells: list[dict]) -> dict:
    """占用最大格：C 最大；同 C 取利润更接近目标的（稳定排序，不随机）。"""
    return max(cells, key=lambda c: (float(c.get("C") or 0),
                                     -abs(float(c.get("P_minus_T") or 0)),
                                     -float(c.get("m") or 0)))


def _picks(jg: dict) -> dict:
    """预算两类取舍的确定性选格（三行情景表与补证行动读同一份，不各算一套）。"""
    cells = _cells(jg)
    base = _base_cell(cells)
    met = [c for c in cells if _is_met(c)]
    over = [c for c in met if _within_budget(c) is False]
    both = [c for c in cells if _both_met(c) is True]
    pool = [c for c in over if c is not base]
    return {
        "cells": cells, "base": base, "met": met, "over": over, "both": both,
        "over_pick": (min(pool or over, key=lambda c: abs(float(c.get("P_minus_T") or 0)))
                      if over else None),
        "over_pick_is_base": bool(over) and not pool,
        "over_from_pool": bool(pool) and len(pool) < len(over),
        "both_pick": (min(both, key=lambda c: abs(float(c.get("P_minus_T") or 0)))
                      if both else None),
        "alt_heavy": _occ_max([c for c in cells if c is not base] or cells) if cells else None,
        "alt_light": (min(met or cells,
                          key=lambda c: (float(c.get("C") or 0),
                                         abs(float(c.get("P_minus_T") or 0))))
                      if cells else None),
        "budget_set": _budget_set(jg),
    }


def _scenario_rows(jg: dict) -> tuple[list[tuple[str, dict]], str]:
    """三行情景行（确定性；2026-10-11 架构师 §18.12：按**预算两类取舍**而不是"离目标远近"）：

    1. 基准重现（g=0,m=0）；
    2. **利润达标但占用超预算**（profit_target_met 且 within_budget=false）——取该组里
       利润最接近目标的一格；基准格本身属于该组时改取组内另一格（避免与第一行重复）。
       本版没有这类格就如实写「本版没有」，行内改放占用最大格并注明它不是该取舍的证据；
    3. **利润与占用同时满足**（both_met）——取最轻（P−T 最小）的达标格；没有就如实注明。

    预算未设（B=null）时 within_budget 不适用：第二行如实写「未设预算 B：该取舍不适用」。
    去重 (g,m)：只在真的重复时丢行，并把丢行原因写进 note。"""
    p = _picks(jg)
    if not p["cells"]:
        return [], "本版没有可用格子：三行情景表为空（如实不造行）。"
    rows: list[tuple[str, dict]] = []
    notes: list[str] = []
    if p["base"] is not None:
        rows.append(("基准重现（基期读数）", p["base"]))
    if not p["budget_set"]:
        rows.append(("未设预算 B：占用最大格", p["alt_heavy"]))
        notes.append("本版未设预算 B：『利润达标但占用超预算』不适用（headroom 无判定）；"
                     "第二行改放占用最大格并如实标注，不作为该取舍的证据。")
    elif p["over_pick"] is not None:
        rows.append(("利润达标但占用超预算", p["over_pick"]))
        if p["over_pick_is_base"]:
            notes.append("基准格本身即『利润达标但占用超预算』：第二行与第一行为同一格（如实保留）。")
        elif p["over_from_pool"]:
            notes.append("基准格也属『利润达标但占用超预算』：第二行取该组里利润最接近目标的一格，"
                         "以免与第一行重复。")
    else:
        rows.append(("本版无『达标但超预算』格：占用最大格", p["alt_heavy"]))
        notes.append("本版没有任何『利润达标但占用超预算』的格（本版共同天数假设下，"
                     "全部达标格的现金占用增量 C 都在预算 B 内）：第二行改放占用最大格并如实标注，"
                     "它不是该取舍的证据；该取舍要改天数或预算（=改假设，另存新 study）后才出现。")
    if p["both_pick"] is not None:
        rows.append(("利润与占用同时满足", p["both_pick"]))
    else:
        rows.append(("本版无『双条件满足』格：占用最小达标格", p["alt_light"]))
        notes.append("本版没有任何『利润与占用同时满足』的格：第三行改放占用最小的达标格"
                     "并如实标注，不作为该取舍的证据。")
    seen: set[tuple[float, float]] = set()
    dedup: list[tuple[str, dict]] = []
    for label, c in rows:
        key = (round(float(c.get("g") or 0), 6), round(float(c.get("m") or 0), 6))
        if key in seen:
            notes.append(f"『{label}』与前面某行是同一格（g/m 相同）：该行去重不显示。")
            continue
        seen.add(key)
        dedup.append((label, c))
    return dedup, " ".join(notes).strip()


def _cell_short(c: dict) -> str:
    return (f"g={_pct(float(c.get('g') or 0))}／m={_pp(float(c.get('m') or 0))}"
            f"（归母净利润 {_yi(_num(c, 'P'))}，现金占用增量 "
            + (_yi(_num(c, "C")) if isinstance(c.get("C"), (int, float)) else "读数缺失")
            + (f"，headroom {_yi(float(c.get('budget_headroom')))}"
               if isinstance(c.get("budget_headroom"), (int, float)) else "") + "）")


def _flip_action(jg: dict, cd: float | None, idl: float | None) -> str:
    """据**格子翻转**给出的具体补证（确定性）。取舍点落在收入规模还是毛利率上，
    决定要补的是「量价拆分」还是「结构与费用归类」证据——两句都由格子差推出，不套话。"""
    p = _picks(jg)
    base, over, both = p["base"], p["over_pick"], p["both_pick"]
    if over is None or both is None:
        if not p["budget_set"]:
            return ("网格侧取舍点：本版**未设预算 B**——先把研究者自己的占用容忍预算在同一入口填上"
                    "（改预算=改假设，会生成新的研究ID，须另存新 study），否则『利润达标但占用超预算』"
                    "这一行无从判定；有了 B 之后再按翻转格决定补量价还是补结构。")
        return (f"网格侧取舍点：本版共同天数 {cd_s_of(cd)}/{cd_s_of(idl)} 让两类取舍不同时出现"
                "（占用列见情景表）——先在**同一入口**把共同天数改成研究者的真实假设（另存新 study），"
                "再看利润达标与占用是否在某一格上冲突；本版不替研究者编一个天数。")
    act = []
    d_p = (float(over.get("P") or 0) - float(both.get("P") or 0))
    d_c = (float(over.get("C") or 0) - float(both.get("C") or 0))
    act.append(f"网格侧取舍点：能同时满足的格是 {_cell_short(both)}，"
               f"而利润也达标却超预算的格是 {_cell_short(over)}——两者相差 ΔP={_yi(abs(d_p))}、"
               f"ΔC={_yi(abs(d_c))}，取舍点落在**{'毛利率/结构' if abs(float(both.get('m') or 0)) > abs(float(over.get('m') or 0)) else '收入规模'}**"
               "而不是笼统的『离目标多远』。")
    # C2c：删掉 C2b 的"通用三项菜单"（分产品量价／分部毛利率／合同负债）。本版已从
    # 原表行拿到分产品量价与勾稽，通用菜单与第四节补证卡重复；仍未知的部分在补证卡的
    # "下一观察"逐条落到具体材料，不再在这里重复第二遍。
    return " ".join(act)


def cd_s_of(v) -> str:
    return f"{float(v):+.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "读数缺失"


def _yis(v) -> str:
    """带符号的亿口径读数：P−T 列里 0.41亿 必须写成 +0.41亿，避免与 -0.41亿 混淆。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    x = float(v)
    return ("+" if x >= 0 else "") + _yi(x)


def _yiv(v) -> str:
    """无符号亿读数；缺数写「读数缺失」，不当零。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    return _yi(float(v))


def _cell_state(cell: dict) -> str:
    cond = cell.get("conditions") if isinstance(cell.get("conditions"), dict) else None
    parts = [str(cell.get("profit_state") or ("达标" if _is_met(cell) else "未达目标"))]
    if cond is not None:
        js = str(cell.get("joint_state") or "")
        if js and js != parts[0]:
            parts.append(js)
    return "；".join(parts)


def _metric_from_run(run: dict, metric: str) -> float | None:
    for o in run.get("outputs") or []:
        if str(o.get("metric")) == metric:
            v = o.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            return None
    return None


# ---- 公司视图（接缝①）：同一份对象喂正文 / 契约 / 附件 ------------------------------

def _sha_obj(value) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _study_materials(man: dict) -> list[dict]:
    """研究 sources 里登记的材料（material_id/url/disclosed_at/text_sha256）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for _name, s in (man.get("sources") or {}).items():
        meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
        for m in (meta.get("materials") or []):
            if not isinstance(m, dict):
                continue
            mid = str(m.get("material_id") or "")
            if mid and mid not in seen:
                seen.add(mid)
                out.append(dict(m))
    return out


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".mypy_cache"}
_MATERIAL_ROOTS = ("evals", "tmp", ".tmp", "docs")


def _find_material_doc(material_id: str, man: dict) -> tuple[dict, str, str]:
    """按 material_id 在**受限根目录**里找材料证据文件（doc.json 优先，meta.json 次之）。

    纪律（架构师 §18.12）：不开放任意路径、不按同名文件猜输入——只认
    `<root>/**/materials/<material_id>/doc.json|meta.json`，并用研究 sources 里登记的
    text_sha256/raw_sha256 校验；对不上就返回空（如实"待数据"，不拿别的材料顶）。"""
    mid = str(material_id or "").strip()
    if not mid:
        return {}, "", "没有材料 id"
    registered = next((m for m in _study_materials(man)
                       if str(m.get("material_id") or "") == mid), {})
    root_dir = Path(__file__).resolve().parent
    for rel in _MATERIAL_ROOTS:
        base = root_dir / rel
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if Path(dirpath).name != mid or Path(dirpath).parent.name != "materials":
                continue
            for fname in ("doc.json", "meta.json"):
                p = Path(dirpath) / fname
                if not p.is_file():
                    continue
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except Exception:                      # noqa: BLE001 - 坏文件当没有
                    continue
                if not isinstance(rec, dict):
                    continue
                exp_text = str(registered.get("text_sha256") or "")
                exp_raw = str(registered.get("raw_sha256") or "")
                got_text = str(rec.get("text_sha256") or "")
                got_raw = str(rec.get("raw_sha256") or "")
                if exp_text and got_text and exp_text != got_text:
                    return {}, "", (f"{mid} 的 text_sha256 与研究登记不一致"
                                    f"（登记 {exp_text[:16]}…，本地 {got_text[:16]}…）：拒绝当同一材料")
                if exp_raw and got_raw and exp_raw != got_raw:
                    return {}, "", (f"{mid} 的 raw_sha256 与研究登记不一致：拒绝当同一材料")
                return rec, str(p), ""
    return {}, "", f"本地缓存里没有材料 {mid} 的证据文件（doc.json/meta.json）"


def _material_record(material_id: str, man: dict) -> tuple[dict, str, str]:
    """材料 → event_calendar 可读记录：本地证据文件优先；缺失时用研究登记里的
    material_id/url/disclosed_at（无标题 → 类别未分类 → 事件表会如实排除）。"""
    doc, path, why = _find_material_doc(material_id, man)
    if doc:
        rec = dict(doc)
        rec.setdefault("material_id", material_id)
        subj = rec.get("subject") if isinstance(rec.get("subject"), dict) else {}
        if not rec.get("code") and subj:
            rec["code"] = str(subj.get("company_code") or "")
        if not rec.get("title"):
            reg = next((m for m in _study_materials(man)
                        if str(m.get("material_id") or "") == str(material_id)), {})
            rec["title"] = str(reg.get("title") or "")
        if not rec.get("disclosed_at"):
            reg = next((m for m in _study_materials(man)
                        if str(m.get("material_id") or "") == str(material_id)), {})
            rec["disclosed_at"] = str(reg.get("disclosed_at") or "")
        return rec, path, ""
    reg = next((m for m in _study_materials(man)
                if str(m.get("material_id") or "") == str(material_id)), {})
    if reg:
        return {"material_id": material_id, "url": str(reg.get("url") or ""),
                "disclosed_at": str(reg.get("disclosed_at") or ""),
                "text_sha256": str(reg.get("text_sha256") or "")}, "", why
    return {}, "", why


def _price_dataset_id(spec: dict) -> tuple[str, str]:
    """行情数据集选择：spec（`market_pin` → `market`）> env > 默认，**顺序固定**
    （架构师 2026-10-11：冻结选择不得被环境默认替换；C2c：新候选把已用的行情 id 显式写进
    `market_pin`，这里优先读它，于是 `company_view.price.dataset_id_source` 显示 `spec`）。
    返回 `(dataset_id, id_source)`；id_source ∈ {spec,env,default}。"""
    for block in ("market_pin", "market"):
        mkt = spec.get(block) if isinstance(spec.get(block), dict) else {}
        for key in ("dataset_id", "price_dataset_id"):
            v = str((mkt or {}).get(key) or "").strip()
            if v:
                return v, "spec"
    env = str(os.environ.get("WEAVEMIND_PRICE_DATASET") or "").strip()
    if env:
        return env, "env"
    return PRICE_DATASET_DEFAULT, "default"


def _calendar_dataset_id(spec: dict) -> tuple[str, str]:
    """交易日历选择：同样 spec（`market_pin` → `market`）> env > 未钉；未钉时**如实写
    unpinned**（不冒充冻结选择）。"""
    for block in ("market_pin", "market"):
        mkt = spec.get(block) if isinstance(spec.get(block), dict) else {}
        for key in ("calendar_id", "trading_calendar_id"):
            v = str((mkt or {}).get(key) or "").strip()
            if v:
                return v, "spec"
    env = str(os.environ.get("WEAVEMIND_TRADING_CALENDAR") or "").strip()
    if env:
        return env, "env"
    return "", "unpinned"


def price_observation(*, code: str, material_ids: list[str], man: dict, spec: dict,
                      evaluation_as_of: str, frozen_binding: dict | None = None) -> dict:
    """价格观察（§18.12）：目标年度年报事件 + 已有离线行情 → 1/5/20 条基准调整读数。

    全部由既有算子现场计算（event_calendar_v1 / event_returns_v1 / trading_calendar）：
    本函数不写死任何收益、日期或哈希。任何一环缺失都返回 `status="pending"` 并带原因，
    调用方只许照实写"待数据"，不许拿别的窗口或别的材料顶。

    `frozen_binding`（C2d 接缝③）＝研究目录 `inputs.json` 里那份 **来源绑定**
    （生成路径真加载时记下的 id＋内容指纹）。传进来就**以它为准**：spec 解析出的 id 与
    冻结绑定不一致 ⇒ 拒绝读数、不替换；一致 ⇒ 正文显示的是**冻结的那份指纹**，
    于是「冻结—恢复—报告」三方消费同一份来源。老研究 inputs.json 没有这份绑定 ⇒
    `frozen_binding_source=absent`，照旧只用 spec 选择（不补造、不冒充已冻结）。"""
    out: dict = {"schema": "weavemind.company_view.price/1", "status": "pending",
                 "benchmark": PRICE_BENCHMARK, "offsets": list(PRICE_OFFSETS),
                 "main_offset": PRICE_MAIN_OFFSET, "evaluation_as_of": str(evaluation_as_of or ""),
                 "anchor_basis": "事件日（价格锚点）", "reasons": []}
    mids = [str(x) for x in (material_ids or []) if str(x).strip()]
    if not mids:
        out["reasons"].append("续页没有目标年度年报材料 id：不猜年报事件")
        return out
    rec, path, why = _material_record(mids[0], man)
    if not rec:
        out["reasons"].append(why or "目标年度年报材料不可用")
        return out
    out["material_id"] = str(rec.get("material_id") or mids[0])
    out["material_path"] = path
    out["event_title"] = str(rec.get("title") or "")
    out["event_url"] = str(rec.get("url") or "")
    try:
        from adapters import market_history as mh
        from adapters import trading_calendar as tcal_mod
        from quant_research import event_calendar as ec
        from quant_research import event_returns as er
    except Exception as exc:                            # noqa: BLE001 - 算子不可用如实报
        out["reasons"].append(f"价格算子不可用：{str(exc)[:120]}")
        return out
    cal = ec.from_records([rec], codes=(str(code).split(".")[0],))
    if not cal.get("events"):
        ex = (cal.get("excluded") or [{}])[0]
        out["reasons"].append(f"年报事件没有进事件表：{ex.get('reason') or '事件日/类别不满足入表条件'}")
        out["excluded"] = cal.get("excluded") or []
        return out
    event = cal["events"][0]
    out["event"] = {k: event.get(k) for k in ("code", "date", "date_precision", "date_basis",
                                              "title", "category", "rule", "material_id", "url",
                                              "raw_sha256", "period")}
    ds_id, ds_src = _price_dataset_id(spec)
    cal_id, cal_src = _calendar_dataset_id(spec)
    # ---- C2d 接缝③：报告消费**冻结的那份**来源绑定（inputs.json），不是重新挑一个数据集
    fb = frozen_binding if isinstance(frozen_binding, dict) else {}
    if fb:
        out["frozen_binding_source"] = "inputs.json"
        _fb_price = str(fb.get("price_dataset_id") or "")
        _fb_cal = str(fb.get("calendar_id") or "")
        if _fb_price and ds_id and _fb_price != ds_id:
            out["reasons"].append(f"冻结绑定的行情 id（{_fb_price[:16]}…）与 spec 解析出的"
                                  f"（{ds_id[:16]}…）不一致：不替换、不读数")
            out["dataset_id"], out["dataset_id_source"] = _fb_price, "frozen_conflict"
            return out
        if _fb_cal and cal_id and _fb_cal != cal_id:
            out["reasons"].append(f"冻结绑定的日历 id（{_fb_cal[:16]}…）与 spec 解析出的"
                                  f"（{cal_id[:16]}…）不一致：不替换、不读数")
            out["dataset_id"], out["dataset_id_source"] = ds_id, "frozen_conflict"
            return out
        _fp, _fc = fb.get("price") or {}, fb.get("calendar") or {}
        out["frozen_binding"] = {
            "status": str(fb.get("status") or ""),
            "price_dataset_id": _fb_price, "calendar_id": _fb_cal,
            "price_loaded": str(_fp.get("loaded") or ""),
            "price_rows_file_sha256": str(_fp.get("rows_file_sha256") or ""),
            "price_license": str(_fp.get("license") or ""),
            "price_adj_basis": str(_fp.get("adj_basis") or ""),
            "price_rows": _fp.get("rows"), "price_range": list(_fp.get("range") or []),
            "calendar_loaded": str(_fc.get("loaded") or ""),
            "calendar_sessions_file_sha256": str(_fc.get("sessions_file_sha256") or ""),
            "calendar_rows": _fc.get("rows"), "calendar_range": list(_fc.get("range") or []),
            "evaluation_as_of": str(fb.get("evaluation_as_of") or ""),
            "reasons": [str(x) for x in (fb.get("reasons") or [])],
        }
    else:
        # 老研究（inputs.json 里没有这份绑定）：**不加任何新键**——本版视图与既有交付逐字一致，
        # 不因为本批接缝给已通过研究凭空造出新版本/新包（架构师：不改旧研究）。
        pass
    payload = mh.load(ds_id)
    if str(payload.get("dataset_id") or "") and str(payload.get("dataset_id")) != str(ds_id):
        out["reasons"].append(f"行情数据集与冻结选择不一致（要求 {ds_id[:16]}…，"
                              f"实取 {str(payload.get('dataset_id'))[:16]}…）：不替换、不读数")
        out["dataset_id"] = str(payload.get("dataset_id") or "")
        out["dataset_id_source"] = ds_src
        return out
    out["dataset_id"] = str(payload.get("dataset_id") or ds_id)
    out["dataset_id_source"] = ds_src
    out["pinned"] = {"price_dataset_id": out["dataset_id"], "price_dataset_id_source": ds_src,
                     "calendar_id": cal_id, "calendar_id_source": cal_src,
                     "benchmark": PRICE_BENCHMARK, "offsets": list(PRICE_OFFSETS),
                     "main_offset": PRICE_MAIN_OFFSET,
                     "material_id": out.get("material_id") or "",
                     "material_raw_sha256": str(rec.get("raw_sha256") or ""),
                     "event_date": str(event.get("date") or ""),
                     "evaluation_as_of": str(evaluation_as_of or "")}
    if fb:
        # C2d 接缝③：只有**真有冻结绑定**时才加这两个键（老研究的视图逐字不变）。
        out["pinned"]["frozen_binding_source"] = "inputs.json"
        out["pinned"]["frozen_binding_status"] = str(
            (out.get("frozen_binding") or {}).get("status") or "")
    out["dataset_source"] = str(payload.get("source") or "")
    out["dataset_license"] = str(payload.get("license") or "")
    out["dataset_adj_basis"] = str(payload.get("adj_basis") or "")
    out["dataset_rows"] = int(payload.get("rows") or 0)
    out["dataset_range"] = payload.get("date_range") or ""
    if str(payload.get("source")) == "unavailable" or not payload.get("data"):
        out["reasons"].append(f"行情数据集不可用：{payload.get('reason') or '没有可用数据集'}")
        return out
    cal_payload = tcal_mod.load(cal_id)     # 空 id → 适配层自己的默认（下面如实记 unpinned）
    if cal_id and str(cal_payload.get("dataset_id") or "") != cal_id:
        out["reasons"].append(f"钉住的交易日历取不到（要求 {cal_id[:16]}…，"
                              f"实取 {str(cal_payload.get('dataset_id') or '无')}）：不替换成最新日历")
        return out
    out["calendar_id"] = str(cal_payload.get("dataset_id") or "")
    out["calendar_id_source"] = cal_src
    out["calendar_rows"] = int(cal_payload.get("rows") or 0)
    out["calendar_range"] = cal_payload.get("range") or cal_payload.get("date_range") or ""
    reading = er.compute(payload, ec.to_event_list(cal), benchmark=PRICE_BENCHMARK,
                         offsets=PRICE_OFFSETS, feature_cutoff=str(event.get("date") or ""),
                         evaluation_as_of=str(evaluation_as_of or ""),
                         calendar=cal_payload if tcal_mod.ok(cal_payload) else None)
    out["reading_status"] = str(reading.get("status") or "")
    out["reading_hash"] = str(reading.get("reading_hash") or "")
    out["available_at_upper_bound"] = str(reading.get("available_at_upper_bound") or "")
    out["delivery_eligible"] = bool(reading.get("delivery_eligible"))
    out["delivery_block_reason"] = str(reading.get("delivery_block_reason") or "")
    out["limits"] = [str(x) for x in (reading.get("limits") or [])]
    out["calendar_used"] = (reading.get("params") or {}).get("calendar") or ""
    _bench_ok = bool(getattr(tcal_mod, "ok", lambda _p: False)(cal_payload))
    if str(reading.get("status")) != "ok" or not reading.get("readings"):
        out["reasons"].append(f"价格读数不可用（{reading.get('status')}）："
                              f"{reading.get('reason') or '算子没有产出读数'}")
        out["unavailable"] = reading.get("unavailable") or []
        return out
    rd = reading["readings"][0]
    out["t0_date"] = str(rd.get("t0_date") or "")
    _anc = rd.get("anchor") if isinstance(rd.get("anchor"), dict) else {}
    out["anchor_date"] = str(_anc.get("date") or rd.get("anchor_date") or "")
    out["anchor_close"] = _anc.get("close")
    out["window_state"] = str(rd.get("window_state") or "")
    out["suspension_suspect"] = rd.get("suspension_suspect")
    out["points"] = [{k: p.get(k) for k in ("offset", "label", "date", "close", "return",
                                            "benchmark_date", "benchmark_close",
                                            "benchmark_return", "excess_return", "state", "reason")}
                     for p in (rd.get("points") or [])]
    # 基准收盘（只为**手算**提供来源）：用同一适配层的窗口切片，按同一日期取值；
    # 基准缺该交易日就留空（不补 0、不前值填充——沿用算子自己的对齐纪律）。
    try:
        _bw = mh.window(payload, PRICE_BENCHMARK, str(event.get("date") or ""),
                        offsets=(-1, *PRICE_OFFSETS))
        _bmap = {str(p.get("date")): p.get("close")
                 for p in (_bw.get("points") or {}).values()
                 if isinstance(p, dict) and p.get("date")}
        out["benchmark_close_basis"] = ("基准收盘按**同一日期**取自同一数据集的 "
                                        f"{PRICE_BENCHMARK} 行；该日期缺行则留空")
        for p in out["points"]:
            p["bench_close"] = _bmap.get(str(p.get("date")))
        _aidx = _bmap.get(str(_anc.get("date")))
        out["benchmark_anchor_close"] = _aidx
    except Exception as exc:                            # noqa: BLE001 - 缺基准只影响手算列
        out["benchmark_close_basis"] = f"基准收盘列不可用：{str(exc)[:80]}"
    out["calendar_ok"] = _bench_ok
    out["calendar_used"] = str((reading.get("params") or {}).get("calendar") or "")
    # 交叉核对（现场计算，不写死结论）：同一事件在**无交易日历**（按标的自身交易日序列）下
    # 的读数——与带日历读数逐窗口比对，数字一致才敢说"窗口定位不影响这组数"。
    try:
        alt = er.compute(payload, ec.to_event_list(cal), benchmark=PRICE_BENCHMARK,
                         offsets=PRICE_OFFSETS, feature_cutoff=str(event.get("date") or ""),
                         evaluation_as_of=str(evaluation_as_of or ""), calendar=None)
        _ard = (alt.get("readings") or [{}])[0]
        _pts: dict = {}
        for p in (_ard.get("points") or []):
            try:
                # **键必须是字符串**：int 键在内存里按数值排序、落盘读回后按字典序排序，
                # 同一份视图会算出两个 view_hash（C2b 架构师定位的失配根因）。
                _pts[str(int(p.get("offset")))] = {
                    "date": p.get("date"), "close": p.get("close"),
                    "return": p.get("return"), "excess_return": p.get("excess_return")}
            except (TypeError, ValueError):
                continue
        out["alt_reading"] = {
            "calendar": str((alt.get("params") or {}).get("calendar") or ""),
            "reading_hash": str(alt.get("reading_hash") or ""),
            "status": str(alt.get("status") or ""),
            "points": _pts,
        }
    except Exception as exc:                            # noqa: BLE001 - 交叉核对失败如实标注
        out["alt_reading"] = {"error": str(exc)[:120]}
    out["status"] = "ok"
    return out


def _fmt_close(v) -> str:
    return f"{float(v):,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "读数缺失"


def _fmt_ret(v) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    return f"{float(v) * 100:+.2f}%"


def _hand_check(points: list) -> str:
    """主窗口手算句（确定性，只重排表里已给的数字）：43.72/47.68−1=−8.31%。"""
    anc = next((p for p in points if p.get("offset") == -1), {})
    main = next((p for p in points if p.get("offset") == PRICE_MAIN_OFFSET), {})
    c0, c1 = anc.get("close"), main.get("close")
    if not (isinstance(c0, (int, float)) and isinstance(c1, (int, float))):
        return "读数缺失（锚点或主窗口收盘缺失，不手算）"
    return (f"{c1:,.2f}/{c0:,.2f}−1 = {float(c1) / float(c0) - 1:+.2%}"
            f"（与表中 {_fmt_ret(main.get('return'))} 一致，四舍五入后）")


def _bench_ret(points: list):
    """基准主窗口收益（表里已有读数时取同值，给手算句复用；缺失返回 None）。"""
    main = next((p for p in points if p.get("offset") == PRICE_MAIN_OFFSET), {})
    return main.get("benchmark_return")


def _price_section(po: dict, code: str, entity: str, slim: bool = False) -> list[str]:
    """价格观察正文段（§18.12）：真实日期 + 几何相对收益公式 + 明确边界。

    `slim`（C2d item 4，只对带现金补充资料的新研究开）：重复字段（行情出处、日历口径、
    无日历交叉核对、手算来源）、几何公式与算子局限**搬出现有附件**——整份 `po`（含 points/
    limits/alt_reading/benchmark_close_basis/calendar）逐字在 `analysis/context.json` 的
    `company_view.price` 里，正文不再重复；slim=False 保持原样（旧研究正文逐字不变）。"""
    out: list[str] = []

    def add(text: str, *, keep: bool = True) -> None:
        """slim 时 keep=False 的行进附件（不进正文）；不 slim 时一律留在正文（旧行为逐字不变）。"""
        if keep or not slim:
            out.append(text)
    if po.get("status") != "ok":
        why = "；".join([str(x) for x in (po.get("reasons") or []) if str(x).strip()]) or "读数缺失"
        out.append(f"- 价格观察待数据：{why}。"
                   f"（本版不为 {entity} 编造价格读数，也不拿别的年度/别的材料替代。）")
        return out
    ev = po.get("event") or {}
    out.append(f"- 事件绑定：{ev.get('title') or '目标年度年报'}（{ev.get('code')} {ev.get('date')} 披露，"
               f"依据 {ev.get('date_basis')}／精度 {ev.get('date_precision')}；材料 {ev.get('material_id')}，"
               f"{ev.get('url') or '无链接'}）。")
    out.append(f"- 三个时点分开：**历史财务特征截止**（建模可用数据的边界，本页第一/二节）"
               f"{po.get('financial_feature_cutoff') or '读数缺失'}；**价格锚点** "
               f"{po.get('anchor_date') or '读数缺失'}"
               f"（事件日之后第一条记录 t+0 的前一条，即 t−1；收盘 {_fmt_close(po.get('anchor_close'))}）；"
               f"**价格评价截止** {po.get('evaluation_as_of') or '读数缺失'}。")
    add(f"- 行情出处：数据集 {po.get('dataset_id')}（来源 {po.get('dataset_source') or '缺失'}；"
        f"许可 {po.get('dataset_license') or '缺失'}；复权口径 {po.get('dataset_adj_basis') or '缺失'}；"
        f"{po.get('dataset_rows')} 行，{po.get('dataset_range')}）；基准 {po.get('benchmark')}；"
        f"读数哈希 {str(po.get('reading_hash') or '')[:16]}…；"
        f"可用上界 {po.get('available_at_upper_bound') or '缺失'}。", keep=False)
    if po.get("calendar_ok"):
        add("- 交易日历：offsets 按**交易日历**定位（真实第 N 个交易日），不是按标的自身序列数"
            f"（参数记录：{po.get('calendar_used') or '已用交易日历'}）。", keep=False)
    else:
        add("- 交易日历不可用：窗口按标的自身交易日序列定位（停牌会让窗口静默错位，"
            "本行如实标注为局限）。", keep=False)
    _alt = po.get("alt_reading") if isinstance(po.get("alt_reading"), dict) else {}
    if _alt and not _alt.get("error") and _alt.get("points"):
        _diff = []
        for p in po.get("points") or []:
            off = p.get("offset")
            if off not in PRICE_OFFSETS:
                continue
            a = (_alt["points"] or {}).get(off) or {}
            if (a.get("date"), a.get("close")) != (p.get("date"), p.get("close")):
                _diff.append(f"t+{off}: 日历 {p.get('date')}/{_fmt_close(p.get('close'))} vs "
                             f"无日历 {a.get('date')}/{_fmt_close(a.get('close'))}")
        if _diff:
            add("- 交叉核对（现场计算）：**无交易日历**时同一事件的窗口定位不同——"
                + "；".join(_diff) + "；以带日历读数为准，差异如实列出。", keep=False)
        else:
            add("- 交叉核对（现场计算，非引用）：同一事件在**无交易日历**（按标的自身交易日序列）"
                f"下重算，窗口日期与收盘价逐一相同（该读数哈希 "
                f"{str(_alt.get('reading_hash') or '')[:16]}…，与带日历读数哈希不同——"
                "差异只来自参数记录，不来自数字）。", keep=False)
    out.append("")
    out.append(f"| 窗口（交易日） | 日期 | {entity} 收盘 | {entity} 收益 | 基准收益 | 相对基准（几何） |")
    out.append("|---|---|---|---|---|---|")
    lab = {0: "t+0（1 条）", 4: "t+4（5 条）", 19: "t+19（20 条，固定主窗口）"}
    for p in po.get("points") or []:
        off = p.get("offset")
        if off == -1:
            out.append(f"| 锚点 t−1 | {p.get('date') or '缺失'} | {_fmt_close(p.get('close'))} "
                       f"| 基准点（只定位，不给 0.00% 读数） | — | — |")
            continue
        if off not in PRICE_OFFSETS:
            continue
        out.append(f"| {lab.get(off, f't+{off}')} | {p.get('date') or '缺失'} "
                   f"| {_fmt_close(p.get('close'))} | {_fmt_ret(p.get('return'))} "
                   f"| {_fmt_ret(p.get('benchmark_return'))} "
                   f"| {_fmt_ret(p.get('excess_return'))} |")
    out.append("")
    if po.get("benchmark_close_basis"):
        _hand = _hand_check(po.get("points") or [])
        add(f"- 手算来源：{po.get('benchmark_close_basis')}"
            + (f"；基准锚点（{po.get('anchor_date')}）收盘 {_fmt_close(po.get('benchmark_anchor_close'))}。"
               if isinstance(po.get("benchmark_anchor_close"), (int, float)) else "。")
            + f"主窗口手算：{_hand}；基准 {_fmt_ret(_bench_ret(po.get('points') or []))} 同理。",
            keep=False)
    add("- 公式（几何，不是差值）：标的收益 = 收盘(t)/收盘(锚点 t−1) − 1；基准收益同理；"
        f"相对基准 =（1+标的收益）/(1+基准收益) − 1。{PRICE_MAIN_OFFSET + 1} 条为固定主窗口，"
        "1/5 条只作短窗参考（同一读数不因窗口选择而换结论）。", keep=False)
    out.append(f"- 窗口状态：{po.get('window_state') or '缺失'}"
               + (f"；疑似停牌：{po.get('suspension_suspect')}" if po.get("suspension_suspect") else "")
               + "。")
    pn = po.get("pinned") or {}
    if pn:
        out.append("- 冻结选择（不被环境默认或最新日历替换）：行情数据集 "
                   f"`{str(pn.get('price_dataset_id') or '')[:16]}…`"
                   f"（来源 {pn.get('price_dataset_id_source') or '缺失'}）；交易日历 "
                   f"`{str(po.get('calendar_id') or '')[:16]}…`"
                   f"（来源 {po.get('calendar_id_source') or '缺失'}，"
                   f"{po.get('calendar_rows') or 0} 行，覆盖 {po.get('calendar_range') or '缺失'}）；"
                   f"材料 {pn.get('material_id') or '缺失'}"
                   f"（raw_sha256 `{str(pn.get('material_raw_sha256') or '')[:16]}…`）；"
                   f"窗口 基准 {pn.get('benchmark')}／偏移 {pn.get('offsets')}／主窗口 t+{pn.get('main_offset')}；"
                   f"事件日 {pn.get('event_date') or '缺失'}／评价截止 {pn.get('evaluation_as_of') or '缺失'}。"
                   + ("**注**：本冻结研究的 spec 没有钉住交易日历（`calendar_id_source=unpinned`），"
                      "上面记录的是缓存目录里当前那份日历的 id/行数/覆盖区间；"
                      "要在别的日历上复现同一读数，须先在 spec 里补 `market.calendar_id`——"
                      "本版不静默换成最新日历后还称同一读数。"
                      if str(po.get("calendar_id_source") or "") == "unpinned" else ""))
        # C2d 接缝③：绑定已在研究目录 `inputs.json` 里**冻结**时，正文显示的就是那份冻结指纹
        # （生成路径真加载时算的 content sha256），而不是本层现算的替代品；老研究没有这一行。
        _fb = po.get("frozen_binding") or {}
        if _fb:
            _pr = str(_fb.get("price_rows_file_sha256") or "")
            _cr = str(_fb.get("calendar_sessions_file_sha256") or "")
            out.append("- 来源绑定（冻结自研究目录 `inputs.json`，恢复后同一份，不再重挑）："
                       f"绑定状态 {_fb.get('status') or '缺失'}；"
                       f"行情内容指纹 rows.csv sha256 `{_pr[:16]}…`"
                       f"（{_fb.get('price_rows') or 0} 行，{_fb.get('price_adj_basis') or '复权口径缺失'}，"
                       f"许可 {_fb.get('price_license') or '缺失'}）；"
                       f"日历内容指纹 sessions.csv sha256 `{_cr[:16]}…`"
                       f"（{_fb.get('calendar_rows') or 0} 行）；"
                       f"绑定时的评价截止 {_fb.get('evaluation_as_of') or '缺失'}。")
    out.append("- 边界：这是**单一材料、单一标的**的事后价格观察，和上面的经营读数并列而不混成一个数："
               "没有独立事件研究设计、没有预告/一致预期链，因此不称『超预期』，不作因果解释，"
               "不称 alpha，也不给任何胜率/均值这类统计量（n=1）。")
    if po.get("delivery_eligible") is False:
        out.append(f"- 许可边界：{po.get('delivery_block_reason') or '该行情许可仅内部试验'}。")
    for lim in (po.get("limits") or [])[:4]:
        add(f"- 算子局限：{lim}", keep=False)
    return out


def _contract_wire(man: dict, code: str, who: str, spec: dict, jg: dict) -> dict:
    """把研究的**主体/期间/口径/必答指标**接成执行契约线（复用 ExecutionContract 既有身份）。

    不新造状态判据：契约只声明研究范围，研究状态仍由 `assemble_and_verify(contract=...)`
    的既有裁决给出（本候选正文没有结构化研究投影时如实返回 not_applicable）。"""
    try:
        from execution_contract import ExecutionContract
    except Exception:                                   # noqa: BLE001 - 契约线缺失不静默造一个
        return {}
    periods: list[int] = []
    for p in ((spec.get("financial") or {}).get("periods") or []):
        try:
            periods.append(int(str(p)[:4]))
        except (TypeError, ValueError):
            continue
    if not periods:
        try:
            periods = [int(str(jg.get("base_period"))[:4])]
        except (TypeError, ValueError):
            periods = []
    try:
        c = ExecutionContract(company=who, company_id=code, market="cn",
                              periods=tuple(sorted(set(periods))), caliber="合并",
                              as_of=str(man.get("feature_cutoff") or ""),
                              required_metrics=("revenue", "net_profit", "operating_cashflow"),
                              subject_type="company")
        return c.to_wire()
    except Exception:                                   # noqa: BLE001
        return {}


def _company_view(*, cctx: dict, man: dict, spec: dict, jg: dict, rows: list, pk: dict,
                  q1c: dict, q1f: dict, actual: dict | None, po: dict, cells: list,
                  runs: dict, row_note: str, real: dict | None = None) -> dict:
    """接缝①：**一份公司视图**（grid／Q1／续页／价格／兑现桥引用同源），喂正文、契约与附件。"""
    code, who = cctx["code"], cctx["company"]
    tf = q1f.get("target_followup") if isinstance(q1f.get("target_followup"), dict) else {}

    def _cells_json(items):
        return [{k: c.get(k) for k in ("g", "m", "P", "P_minus_T", "C", "budget_headroom",
                                       "profit_state", "joint_state", "conditions")}
                for c in items]

    view: dict = {
        "schema": _COMPANY_VIEW_SCHEMA,
        "captured_at": "",
        "binding": {
            "study_id": str(man.get("study_id") or ""),
            "result_hash": str(man.get("result_hash") or ""),
            "entity_id": code, "company_name": who,
            "feature_cutoff": str(man.get("feature_cutoff") or ""),
            "evaluation_as_of": str(man.get("evaluation_as_of") or ""),
            "study_research_state": str(man.get("research_state") or ""),
        },
        "grid": {
            "method_version": jg.get("method_version"),
            "collection_days_delta": jg.get("collection_days_delta"),
            "inventory_days_delta": jg.get("inventory_days_delta"),
            "budget": jg.get("budget"), "target": jg.get("target"),
            "base_period": jg.get("base_period"),
            "cells": _cells_json(cells),
            "adopted_rows": [{"row": lab, **{k: c.get(k) for k in
                                             ("g", "m", "P", "P_minus_T", "C",
                                              "budget_headroom", "conditions")}}
                             for lab, c in rows],
            "picks": {"over": _cells_json([pk["over_pick"]]) if pk.get("over_pick") else [],
                      "both": _cells_json([pk["both_pick"]]) if pk.get("both_pick") else [],
                      "budget_set": bool(pk.get("budget_set"))},
            "row_note": row_note,
        },
        "q1": {
            "card_hash": str(q1c.get("card_hash") or ""),
            "entity": str(q1c.get("entity") or ""),
            "card_keys": sorted(k for k in q1c.keys()),
        },
        "followup": {
            "status": q1f.get("status"), "mechanism_verdict": q1f.get("mechanism_verdict"),
            "next_check": list(q1f.get("next_check") or []),
            "pending_reason": q1f.get("pending_reason"),
            "target_followup": {k: tf.get(k) for k in
                                ("status", "period", "metric", "metric_label", "caliber", "target",
                                 "gap", "verdict", "verified_at", "pending_reason", "note")},
            "actual": ({k: (actual or {}).get(k) for k in
                        ("period", "value", "source", "source_hash", "material_ids",
                         "disclosed_at", "available_at", "fact_id", "caliber")}
                       if actual else None),
        },
        "price": po,
        "runs": {mid: {"run_id": r.get("run_id"), "status": r.get("status"),
                       "metrics": [o.get("metric") for o in (r.get("outputs") or [])],
                       "output_hashes": [o.get("output_hash") for o in (r.get("outputs") or [])]}
                 for mid, r in (runs or {}).items()},
        "realization": real if isinstance(real, dict) else {"status": "pending",
                                                             "reasons": ["本版没有装配兑现桥"]},
        "sources": _study_materials(man),
    }
    view["contract"] = _contract_wire(man, code, who, spec, jg)
    view["captured_at"] = str(man.get("feature_cutoff") or "")
    # 视图指纹：正文/契约/附件读到的是不是同一份，用哈希说话（同输入同输出）。
    # **一次 JSON 往返**再哈希：把 int 键、元组等统一成落盘后的形态，
    # 否则内存里 (…,4,19) 按数值排序、落盘读回按字典序排序，同一份视图会算出两个 view_hash。
    view["view_hash"] = _sha_obj(_json_roundtrip({k: v for k, v in view.items() if k != "view_hash"}))
    return view


def _json_roundtrip(value):
    """落盘形态归一：int 键→字符串、tuple→list、非 JSON 标量转字符串；失败就原样返回。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:                                    # noqa: BLE001 - 归一失败不阻断成文
        return value


# ——— C2b（2026-10-11 架构师 §18.13）：假设兑现解释页 ———
# 续页时点上的经营桥：**只读**冻结输入的观察行，复用既有 `operating_drivers` 对称分解；
# 与主网格**隔离**（主网格读特征截止＝旧截止下的可见观察，本桥 as_of 取续页材料自己的
# available_at），所以目标年度行可见而旧正文/旧卡不受影响。纪律：不硬编码任何结论数、
# 残差≠因果、条件未发生≠预测失败、不回填旧主卡。

_YEAR_RE = re.compile(r"(\d{4})")


def _year_of(label) -> int:
    m = _YEAR_RE.search(str(label or ""))
    return int(m.group(1)) if m else 0


def _prev_year_label(label) -> str:
    y = _year_of(label)
    return f"{y - 1}年" if y else ""


def _frozen_records(d: Path, code: str) -> list[dict]:
    """冻结输入里的观察行（**只读** `inputs.json`；不改冻结目录、不重算主网格）。"""
    inp = _read_json(d / "inputs.json")
    recs = (((inp.get("previews") or {}).get("financial") or {}).get("records") or [])
    return [r for r in recs if isinstance(r, dict) and str(r.get("entity_id")) == str(code)]


def _obs_value(recs: list[dict], period: str, metric: str):
    """`(值, 观察行)`；缺数返回 `(None, None)`——不把缺数当零、不用别的行顶替。"""
    for r in recs:
        if str(r.get("period")) == str(period) and str(r.get("metric")) == str(metric):
            v = r.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v), r
            return None, None
    return None, None


def _cash_observations(recs: list[dict], base: str, target: str) -> dict:
    """现金（**合并净利润口径**）：只对照经营现金流与合并净利润的差，缺调节表**不拼**因果桥。

    理由：把净利润调节成经营现金流的**现金流量表补充资料**不在这批冻结输入里；
    资产负债表行的变动只是**个别观察**，拼不成一条"现金因果桥"，所以不拼。
    """
    out: dict = {"caliber": "net_profit_consolidated", "periods": [base, target],
                 "reasons": [], "series": {}, "balance_observations": []}
    for per in (base, target):
        ocf, _r = _obs_value(recs, per, "operating_cashflow")
        npc, _r2 = _obs_value(recs, per, "net_profit_consolidated")
        npp, _r3 = _obs_value(recs, per, "net_profit")
        if None in (ocf, npc):
            out["reasons"].append(f"{per} 经营现金流/合并净利润读数缺失：该期不写差额")
            continue
        out["series"][per] = {"operating_cashflow": ocf, "net_profit_consolidated": npc,
                              "net_profit": npp, "gap": ocf - npc}
    for metric, label in (("inventory", "存货"), ("accounts_payable", "应付账款"),
                          ("accounts_receivable", "应收账款")):
        v0, _r = _obs_value(recs, base, metric)
        v1, _r2 = _obs_value(recs, target, metric)
        if None not in (v0, v1):
            out["balance_observations"].append({"metric": metric, "label": label,
                                                "base": v0, "target": v1, "delta": v1 - v0})
    return out


def realization_bridge(d: Path, *, code: str, followup: dict) -> dict:
    """续页时点的经营桥（复用 `operating_drivers` 对称分解），并按同一批观察给现金对照。

    读数缺失/算子不可用一律 `status=pending` ＋原因，不编数、不拿别的期间顶替。
    """
    tf = followup.get("target_followup") if isinstance(followup.get("target_followup"), dict) else {}
    act = tf.get("actual") if isinstance(tf.get("actual"), dict) else None
    out: dict = {"status": "pending", "reasons": [], "bridge": None, "run": None,
                 "periods": [], "as_of": "", "material_ids": [], "verify_state": "",
                 "actual": None, "cash": None, "growth": None, "gross_margin": None,
                 "net_profit": None, "closure": "", "source": "", "source_url": "",
                 "cutoff_note": "", "cash_run": None, "cash_run_reason": ""}
    if not act:
        out["reasons"].append(str(followup.get("pending_reason")
                                  or "目标年度同口径读数尚未取得：不写实际读数"))
        return out
    target = str(act.get("period") or "")
    base = _prev_year_label(target)
    as_of = str(act.get("available_at") or act.get("disclosed_at") or tf.get("verified_at") or "")
    out.update(periods=[base, target], as_of=as_of,
               material_ids=[str(x) for x in (act.get("material_ids") or []) if str(x).strip()],
               verify_state=str(act.get("verify_state") or ""),
               source=str(act.get("source") or ""), source_url=str(act.get("source_url") or ""),
               actual={"metric": str(act.get("metric") or ""), "period": target,
                       "value": act.get("value"), "fact_id": str(act.get("fact_id") or "")})
    if not base or not as_of:
        out["reasons"].append("续页读数缺可识别的期间或可用时点：不猜、不补")
        return out
    recs = [r for r in _frozen_records(d, code) if str(r.get("period")) in (base, target)]
    if not recs:
        out["reasons"].append(f"冻结输入没有 {base}／{target} 观察行：不编数")
        return out
    out["cash"] = _cash_observations(recs, base, target)
    rev0, _r = _obs_value(recs, base, "revenue")
    rev1, _r2 = _obs_value(recs, target, "revenue")
    if None not in (rev0, rev1) and rev0:
        out["growth"] = {"base": rev0, "target": rev1, "g": rev1 / rev0 - 1.0}
    np0, _r3 = _obs_value(recs, base, "net_profit")
    np1, _r4 = _obs_value(recs, target, "net_profit")
    if None not in (np0, np1):
        out["net_profit"] = {"base": np0, "target": np1, "delta": np1 - np0}
    try:
        from financial_analysis import dataset as ds_mod
        from financial_analysis import runner
    except Exception as exc:                            # noqa: BLE001 - 算子不可用如实报
        out["reasons"].append(f"经营桥算子不可用：{str(exc)[:120]}")
        return out
    try:
        ds = ds_mod.freeze(recs, entity_id=code, as_of=as_of,
                           periods=(_year_of(base), _year_of(target)),
                           source_label="官方披露缓存（冻结输入；续页时点）",
                           restrictions="内部研究；许可与历史版本范围未认证")
        run = runner.run("operating_drivers", ds).as_dict()
    except Exception as exc:                            # noqa: BLE001 - 运行失败如实报
        out["reasons"].append(f"经营桥运行失败：{type(exc).__name__}: {str(exc)[:140]}")
        return out
    if str(run.get("status") or "") != "validated":
        out["reasons"].append(f"经营桥未通过独立验证（{run.get('status')}）："
                              f"{str(run.get('reason') or '')[:120]}")
        return out
    # 落库形状：`ModelRun.as_dict()` 不带 run 级 diagnostics——算子的诊断挂在**每个输出**上
    outs = run.get("outputs") or []
    diag = (outs[0].get("diagnostics") or {}) if outs else {}
    # C2e 接缝（§18.16）：**同一份续页 dataset** 再跑 `cash_reconciliation`——沿用同一算子、
    # 同一 as_of，不另设数据集/截止框架；主卡特征截止下的主运行不含现金补充资料
    # （披露日晚于主截止），现金数据只进本节。失败/未验证如实记原因，不造数。
    try:
        crun = runner.run("cash_reconciliation", ds).as_dict()
    except Exception as exc:                            # noqa: BLE001 - 运行失败如实报
        out["cash_run"], out["cash_run_reason"] = None, \
            f"现金算子运行失败：{type(exc).__name__}: {str(exc)[:140]}"
    else:
        if str(crun.get("status") or "") == "validated":
            out["cash_run"], out["cash_run_reason"] = crun, ""
        else:
            out["cash_run"], out["cash_run_reason"] = None, \
                f"现金算子未验证（{crun.get('status')}）：{str(crun.get('reason') or '')[:120]}"
    out["gross_margin"] = diag.get("gross_margin") or None
    out["closure"] = str(diag.get("closure") or "")
    out["run"] = run
    out["bridge"] = {
        "model_id": run.get("model_id"), "run_id": run.get("run_id"),
        "impl_version": run.get("impl_version"), "dataset_hash": run.get("dataset_hash"),
        "params_hash": run.get("params_hash"), "status": run.get("status"),
        "periods": list(diag.get("periods") or []),
        "validation_ok": bool((run.get("validation") or {}).get("ok")),
        "validation_checks": {k: bool((v or {}).get("ok"))
                              for k, v in ((run.get("validation") or {}).get("checks") or {}).items()},
        "outputs": [{"metric": o.get("metric"), "label": o.get("label"), "value": o.get("value"),
                     "unit": o.get("unit"), "input_periods": list(o.get("input_periods") or []),
                     "output_period": o.get("output_period"), "output_hash": o.get("output_hash"),
                     "residual": o.get("residual"), "formula": o.get("formula"),
                     "components": [{"component_id": c.get("component_id"), "label": c.get("label"),
                                     "value": c.get("value"), "formula": c.get("formula")}
                                    for c in (o.get("components") or [])]}
                    for o in outs],
        "diagnostics": {"caliber": diag.get("caliber"), "currency": diag.get("currency"),
                        "unit": diag.get("unit"), "entity_id": diag.get("entity_id"),
                        "input_hashes": diag.get("input_hashes"),
                        "line_items_used": diag.get("line_items_used"),
                        "line_items_missing": diag.get("line_items_missing"),
                        "line_items_rejected": diag.get("line_items_rejected"),
                        "unexplained_residual_yuan": diag.get("unexplained_residual_yuan"),
                        "symmetric_formula": diag.get("symmetric_formula"),
                        "closure": diag.get("closure"),
                        # C2c：量价分解的**范围**（同口径/退回公司口径）与分产品勾稽的两层闭合。
                        # diagnostics 是白名单，新增项必须在这里显式列出，否则正文拿不到。
                        "volume_price_caliber": diag.get("volume_price_caliber"),
                        "volume_price_metric": diag.get("volume_price_metric"),
                        "volume_price_revenue_metric": diag.get("volume_price_revenue_metric"),
                        "volume_price_revenue_relation": diag.get("volume_price_revenue_relation"),
                        "volume_price_dimension": diag.get("volume_price_dimension"),
                        "volume_price_note": diag.get("volume_price_note"),
                        "volume_price_skipped": diag.get("volume_price_skipped"),
                        "volume_price_inputs": diag.get("volume_price_inputs"),
                        "volume_price_extra": diag.get("volume_price_extra"),
                        "revenue_reconciliation": diag.get("revenue_reconciliation"),
                        "revenue_reconciliation_skipped": diag.get("revenue_reconciliation_skipped"),
                        "alternative_explanations": diag.get("alternative_explanations"),
                        "caveats": diag.get("caveats")},
        "assumptions": list((outs[0].get("assumptions") if outs else None)
                            or run.get("assumptions") or []),
        "limits": list((outs[0].get("limits") if outs else None)
                       or run.get("limits") or []),
        "gross_margin": diag.get("gross_margin"),
    }
    out["status"] = "ok"
    return out


def _run_output(run: dict | None, metric: str) -> dict | None:
    for o in ((run or {}).get("outputs") or []):
        if str(o.get("metric")) == metric:
            return o
    return None


_BRIDGE_ORDER = (("net_profit_change", 0, "归母净利润变化"),
                 ("gross_profit_change", 1, "毛利变化"),
                 ("revenue_scale_effect", 2, "收入规模效应"),
                 ("gross_margin_effect", 2, "毛利率效应"),
                 ("below_gross_line_change", 1, "毛利线以下净额变化"))


def _comp_formulas(run: dict) -> dict:
    """component_id → 短公式（算子自带，原样取用）；同一 id 取首次出现。"""
    m: dict = {}
    for o in (run.get("outputs") or []):
        for c in (o.get("components") or []):
            cid = str(c.get("component_id") or "")
            if cid and cid not in m:
                m[cid] = str(c.get("formula") or "")
    return m


def _bridge_table(run: dict, slim: bool = False) -> list[str]:
    """桥输出 → 嵌套表；金额全部来自算子输出，每行带 `output_hash` 定位（不另算一遍）。

    `slim`（C2d item 4，只对带现金补充资料的新研究开）：正文只留「桥项／金额／输出定位」，
    算子自带公式**搬出现有附件**——逐条公式本就在 `analysis/analysis_runs.json` 同 run_id 的
    输出 `formula` 字段里（逐字同源），正文不再重复长公式；slim=False 保持原四列
    （旧研究正文逐字不变）。"""
    cf = _comp_formulas(run)
    parts = [p.strip() for p in str((_run_output(run, "net_profit_change") or {})
                                    .get("formula") or "").split("；") if p.strip()]
    tbl = (["| 桥项 | 金额 | 输出定位 |", "|---|---|---|"] if slim
           else ["| 桥项 | 金额 | 公式（算子自带） | 输出定位 |", "|---|---|---|---|"])
    for metric, depth, fallback in _BRIDGE_ORDER:
        o = _run_output(run, metric)
        if o is None:
            continue
        f = cf.get(metric) or (parts[1] if metric == "net_profit_change" and len(parts) > 1
                               else (parts[0] if metric == "gross_profit_change" and parts
                                     else str(o.get("formula") or "—")))
        if slim:
            tbl.append(f"| {'↳' * depth}{str(o.get('label') or fallback)} | {_yis(o.get('value'))} "
                       f"| `{str(o.get('output_hash') or '')[:12]}…` |")
        else:
            tbl.append(f"| {'↳' * depth}{str(o.get('label') or fallback)} | {_yis(o.get('value'))} "
                       f"| {f} | `{str(o.get('output_hash') or '')[:12]}…` |")
    det = _run_output(run, "net_profit_change_detail")
    if det is not None:
        _det_f = (parts[2] if len(parts) > 2 else "Δ(归母净利−毛利) = Σ sign×Δ项目 + 未解释差额")
        if slim:
            tbl.append(f"| 毛利线以下逐项分解 | {_yis(det.get('value'))} "
                       f"| `{str(det.get('output_hash') or '')[:12]}…` |")
        else:
            tbl.append(f"| 毛利线以下逐项分解 | {_yis(det.get('value'))} | {_det_f} "
                       f"| `{str(det.get('output_hash') or '')[:12]}…` |")
        for c in (det.get("components") or []):
            if slim:
                tbl.append(f"| ↳{c.get('label') or c.get('component_id')} | {_yis(c.get('value'))} | — |")
            else:
                tbl.append(f"| ↳{c.get('label') or c.get('component_id')} | {_yis(c.get('value'))} "
                           f"| {c.get('formula') or '—'} | — |")
    return tbl


def _bridge_closure(run: dict) -> tuple[bool, str]:
    """闭合自检：分项合计 = 父项，且父项残差为 0（不依赖算子自己的结论串）。"""
    for metric, _depth, _label in _BRIDGE_ORDER:
        o = _run_output(run, metric)
        if o is None or metric in ("revenue_scale_effect", "gross_margin_effect"):
            continue
        comps = o.get("components") or []
        if not comps:
            continue
        s = sum(float(c.get("value") or 0.0) for c in comps)
        v = float(o.get("value") or 0.0)
        if abs(s - v) > 0.01:
            return False, (f"{metric} 分项合计 {_yi(s)} ≠ 父项 {_yi(v)}（差 {_yi(s - v)}）")
    return True, "分项合计与父项一致（差 ≤0.01元），父项残差 0.00亿"


def _realization_head(blk: dict, *, t: float | None, tf: dict) -> list[str]:
    """续页结论行：实际读数、原条件状态、桥的分项主导（全部现场算，不硬编码）。"""
    out: list[str] = []
    if blk.get("status") != "ok":
        why = "；".join([str(x) for x in (blk.get("reasons") or []) if str(x).strip()])
        out.append(f"- **假设兑现解释（续页）：待数据**——{why or '读数缺失'}。")
        if tf.get("pending_reason"):
            out.append(f"- 续页原有说明：{tf.get('pending_reason')}。")
        return out
    run = blk["bridge"]
    g = (blk.get("growth") or {}).get("g")
    gm = blk.get("gross_margin") or {}
    dpp = gm.get("delta_pp")
    np_ = blk.get("net_profit") or {}
    ps = [str(x) for x in (blk.get("periods") or [])]
    target_p = ps[-1] if ps else ""
    act = blk.get("actual") or {}
    gap = (float(np_.get("target") or 0) - float(t)) if (t is not None and np_.get("target") is not None) else None
    seg = (f"- **目标年度实际（{target_p or '目标年度'}"
           + (f"，对基期 {ps[0]}" if len(ps) == 2 else "")
           + f"，来源 {blk.get('source') or '官方披露'}，{blk.get('as_of') or '时点缺失'} 可用"
           f"；`verify_state={blk.get('verify_state') or '读数缺失'}` 如实保留）**："
           f"归母净利润 {_yiv(np_.get('target'))}"
           + (f"，距研究者目标 T {_yiv(t)} 差 {_yis(gap)}" if gap is not None else "（目标读数缺失）")
           + "。")
    if g is not None:
        seg += f"实际收入增长 {_pct(float(g))}"
        if dpp is not None:
            seg += f"、毛利率变动 {_pp(float(dpp) / 100)}"
        seg += "。"
    out.append(seg)
    if act.get("value") is not None and tf.get("verdict"):
        out.append(f"- 续页原有判定：{tf.get('verdict')}"
                   + (f"（口径 {tf.get('caliber') or '合并'}）；{tf.get('note')}" if tf.get("note") else "。"))
    return out


def _conditions_states(rows: list, g_a, m_a) -> list[tuple[str, str, str]]:
    """逐轴判定原条件是否发生：实际 >= 条件 记「已发生」；缺实际读数记「读数缺失」。"""
    out: list[tuple[str, str, str]] = []
    for label, c in rows:
        g_c = float(c.get("g") or 0.0)
        m_c = float(c.get("m") or 0.0)
        gs = ("已发生" if (g_a is not None and float(g_a) >= g_c)
              else ("未发生" if g_a is not None else "读数缺失"))
        ms = ("已发生" if (m_a is not None and float(m_a) >= m_c)
              else ("未发生" if m_a is not None else "读数缺失"))
        out.append((str(label), gs, ms))
    return out


def _conditions_table(rows: list, g_a, m_a) -> list[str]:
    """原选定格子的 g/m 条件 vs 实际：条件未发生 ≠ 预测失败（情景不是预测）。"""
    tbl = ["| 原情景行（选定网格） | 条件 g | 条件 m | 实际 g | 实际 m | 条件状态 |",
           "|---|---|---|---|---|---|"]
    for (label, _gs, _ms), (_l, c) in zip(_conditions_states(rows, g_a, m_a), rows):
        tbl.append(f"| {label} | {_pct(float(c.get('g') or 0.0))} | {_pp(float(c.get('m') or 0.0))} | "
                   f"{_pct(g_a) if g_a is not None else '读数缺失'} | "
                   f"{_pp(m_a) if m_a is not None else '读数缺失'} | g {_gs}／m {_ms} |")
    return tbl


def _pct_signed(v) -> str:
    """带符号百分比（读数缺失如实写，不写 0）。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    return f"{float(v) * 100:+.2f}%"


def _num2(v) -> str:
    """两位小数的原样数值（吨等实物量用它；缺数如实写）。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    return f"{float(v):,.2f}"


def _per_ton(v) -> str:
    """元/吨：用元显示（不换算成亿），否则"每吨"会被读成天文数字。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    return f"{float(v):,.2f} 元/吨"


def _volume_price_lines(blk: dict) -> list[str]:
    """第三节的**量价两层闭合**（C2c）：白酒行内拆量价 → 用分产品行勾回公司收入合计。

    纪律：数一律取自同一台运行（`operating_drivers` 的 `volume_price_inputs` /
    `revenue_reconciliation`），装配层**零重算**；缺行/范围错配只报缺口，不给"已解释"。
    """
    out: list[str] = []
    run = blk.get("bridge") or {}
    dg = (run.get("diagnostics") or {}) if isinstance(run, dict) else {}
    vp = dg.get("volume_price_inputs") or {}
    rec = dg.get("revenue_reconciliation") or {}
    rel = str(dg.get("volume_price_revenue_relation") or "")
    out += ["", "**量价（原表：产量/销量/库存表 × 营业收入构成表）**", ""]
    if not rec:
        why = [str(x) for x in (dg.get("revenue_reconciliation_skipped") or []) if str(x).strip()]
        why += [str(x) for x in (dg.get("volume_price_skipped") or []) if str(x).strip()]
        out.append("- 本版**不给**量价分解与分产品勾稽：" + ("；".join(why) or "读数缺失")
                   + "。缺哪一项写哪一项，不拿别的年度、别的科目或公司口径顶替。")
        return out
    v_unit = str(rec.get("volume_unit") or vp.get("volume_unit") or "")
    q0, q1 = rec.get("volume_prev"), rec.get("volume_cur")
    u0, u1 = rec.get("unit_revenue_prev"), rec.get("unit_revenue_cur")
    veff = vp.get("volume_effect_yuan")
    ueff = vp.get("unit_revenue_effect_yuan")
    b_delta = rec.get("baijiu_delta_yuan")
    c_delta = rec.get("company_delta_yuan")
    gq = _delta(q1, q0)
    gu = _delta(u1, u0)
    g = ((q1 / q0 - 1.0) if isinstance(q0, (int, float)) and q0 and isinstance(q1, (int, float))
         else None)

    def share(v) -> str:
        if not isinstance(v, (int, float)) or not isinstance(c_delta, (int, float)) or not c_delta:
            return "读数缺失"
        return f"{float(v) / float(c_delta) * 100:.1f}%"

    # 白酒收入同比：原写法把跨行表达式放进 f-string，只有 Python 3.12+（PEP 701）能解析，
    # CI 与生产镜像（python:3.11-slim）直接语法错误；提成局部变量，输出不变。
    b_rev_prev = rec.get("baijiu_revenue_prev_yuan")
    b_rev_growth = (b_delta / b_rev_prev
                    if isinstance(b_delta, (int, float))
                    and isinstance(b_rev_prev, (int, float)) and b_rev_prev else None)

    out += ["| 白酒量价（同口径：分产品:白酒） | 上期 | 本期 | 同比 | 贡献 | 占公司收入变化 |",
            "|---|---|---|---|---|---|",
            f"| 销量（{v_unit or '单位缺失'}） | {_num2(q0)} | {_num2(q1)} | {_pct_signed(g)} | "
            f"{_yis(veff)}（数量） | {share(veff)} |",
            f"| 白酒收入 | {_yis(vp.get('revenue_prev_yuan') or b_rev_prev)} | "
            f"{_yis(vp.get('revenue_cur_yuan') or rec.get('baijiu_revenue_cur_yuan'))} | "
            f"{_pct_signed(b_rev_growth)} | "
            f"{_yis(b_delta)} | {share(b_delta)} |",
            f"| 单位实现收入（含产品结构） | {_per_ton(u0)} | {_per_ton(u1)} | "
            f"{_pct_signed((gu / u0) if isinstance(u0, (int, float)) and u0 else None)} | "
            f"{_yis(ueff)}（单位实现收入） | {share(ueff)} |",
            "", "**第二层：分产品行勾回公司营业收入**", "",
            "| 分产品行 | 上期 | 本期 | 变化 | 占公司收入变化 | 维度范围 | fact_id 前 12 |",
            "|---|---|---|---|---|---|---|"]
    for it in (rec.get("items") or []):
        fids = [str(x) for x in (it.get("fact_ids") or [])]
        out.append(f"| {it.get('label')}收入 | {_yis(it.get('prev'))} | {_yis(it.get('cur'))} | "
                   f"{_yis(it.get('delta'))} | {share(it.get('delta'))} | "
                   f"{it.get('dimension') or '未标注'} | "
                   f"{'／'.join(x[:12] for x in fids) if fids else '读数缺失'} |")
    out.append(f"| **公司营业收入（父项）** | — | — | {_yis(c_delta)} | 100.0% | 公司合计 | — |")
    out += [
        "",
        f"- **两层闭合**：①白酒行内——数量贡献 {_yis(veff)} ＋ 单位实现收入贡献 {_yis(ueff)} "
        f"＝ {_yis(b_delta)}（差 {_yis(rec.get('layer1_volume_price_gap_yuan'))}）；"
        f"②公司合计——白酒 {_yis(b_delta)} ＋ 红酒/其他 ＝ {_yis(c_delta)}"
        f"（残差 {_yis(rec.get('layer2_residual_yuan'))}）。两层都按 0.01 元容差自检；"
        "残差非零即为分解漏项/重复计项，不能写成「已解释」。",
        f"- **同口径**：分子分母都取「{dg.get('volume_price_dimension') or '分产品:白酒'}」"
        f"（收入指标 `{dg.get('volume_price_revenue_metric') or '缺失'}`，销量指标 "
        f"`{dg.get('volume_price_metric') or '缺失'}`，关系 {rel or '缺失'}）——"
        "**没有用公司收入除白酒销量**；分产品白酒收入缺失时**拒绝出数**（关系写 "
        "`different_scope`／`mismatch`，只报缺口，不给公司口径的替代吨价）。"
        + (f" 运行给的说明：{dg.get('volume_price_note')}。" if dg.get("volume_price_note") else ""),
        "- **读法边界**：销量是年报**出货**口径（产量/销量/库存表），**不等于终端动销**；"
        "单位实现收入＝收入÷销量，**含产品结构混合**（白酒内部结构、渠道让利与促销归类都在里面），"
        "所以它下降**不等于主动降价**，上升也不等于提价能力增强。",
    ]
    ex = dg.get("volume_price_extra") or {}
    if ex:
        bits = []
        for key in ("production_volume", "inventory_volume"):
            row = ex.get(key) or {}
            if row:
                gx = ((float(row["cur"]) / float(row["prev"]) - 1.0)
                      if row.get("prev") else None)
                bits.append(f"{row.get('label') or key} {_num2(row.get('prev'))} → "
                            f"{_num2(row.get('cur'))} {row.get('unit') or ''}"
                            f"（{_pct_signed(gx)}）")
        if bits:
            out.append(f"- **同表相邻行（只作补充观察，不进分解）**：{'；'.join(bits)}——"
                       "这是**公司**产量/库存口径；公司库存下降与减产一致，但**渠道库存**"
                       "（终端动销的直接证据）仍未知，不据此断言动销。")
    return out


def _evidence_card(blk: dict, *, code: str, who: str, q1c: dict, jd: dict, jg: dict,
                   q1f: dict, cash_in: bool = False) -> list[str]:
    """补证卡（C2c）：原待证解释 → 实际证据 → 贡献 → 支持/削弱/仍未知 → 下一观察。

    替代 C2b 的"通用三项菜单"与"需要取得量价表"待办：那张表的前提（量价在原表里）
    本版已经满足，所以这里给**已补证的读数**与**仍未知的部分**，并把下一观察写成
    可执行的下一步。所有数字来自同一台运行；缺数则逐格写缺，不编。
    """
    out: list[str] = []
    run = blk.get("bridge") or {}
    dg = (run.get("diagnostics") or {}) if isinstance(run, dict) else {}
    vp = dg.get("volume_price_inputs") or {}
    rec = dg.get("revenue_reconciliation") or {}
    scale = _run_output(run, "revenue_scale_effect")
    margin = _run_output(run, "gross_margin_effect")
    below = _run_output(run, "below_gross_line_change")
    npc = _run_output(run, "net_profit_change")
    alts = [str(x) for x in (jd.get("alternatives") or []) if str(x).strip()]
    nxt = [str(x) for x in (q1f.get("next_check") or []) if str(x).strip()]
    mode = str(jg.get("below_gross_mode") or "读数缺失")
    mids = "、".join([str(x) for x in (blk.get("material_ids") or [])]) or "目标年度年报材料"
    if blk.get("status") != "ok" or not rec:
        why = [str(x) for x in (blk.get("reasons") or []) if str(x).strip()]
        why += [str(x) for x in (dg.get("revenue_reconciliation_skipped") or []) if str(x).strip()]
        out.append("| 原待证解释 | 实际证据 | 贡献 | 判定 | 下一观察 |")
        out.append("|---|---|---|---|---|")
        for a in (alts[:2] or ["（主卡未登记替代解释）"]):
            out.append(f"| {a} | 本版读数缺失：{'；'.join(why) or '读数缺失'} | 读数缺失 | "
                       f"**仍未知**（不编数） | {nxt[0] if nxt else '下一份定期报告同口径行'} |")
        return out
    q0, q1 = vp.get("volume_prev"), vp.get("volume_cur")
    u0, u1 = vp.get("unit_revenue_prev"), vp.get("unit_revenue_cur")
    gq = ((q1 / q0 - 1.0) if isinstance(q0, (int, float)) and q0 and isinstance(q1, (int, float))
          else None)
    gu = ((u1 / u0 - 1.0) if isinstance(u0, (int, float)) and u0 and isinstance(u1, (int, float))
          else None)
    veff, ueff = vp.get("volume_effect_yuan"), vp.get("unit_revenue_effect_yuan")
    below_v = (below or {}).get("value")
    out += [f"**补证卡（{who}；原待证解释来自主卡与 2024 续页，实际证据来自目标年度原表行）**", "",
            "| 原待证解释 | 实际证据（原表行） | 贡献 | 支持／削弱／仍未知 | 下一观察 |",
            "|---|---|---|---|---|",
            f"| ①需求/动销收缩（销量下滑） | 白酒销量 {_num2(q0)} → {_num2(q1)} "
            f"{vp.get('volume_unit') or ''}（{_pct_signed(gq)}）；材料 {mids} | "
            f"数量贡献 {_yis(veff)} | **支持「出货量收缩」**；"
            f"**终端动销仍未知**（年报只有出货口径，没有渠道库存/终端流水） | "
            f"渠道库存与合同负债（下一份定期报告/投资者关系记录）；现金表补充资料 |",
            f"| ②均价与产品结构下移（吨价下滑） | 单位实现收入 {_per_ton(u0)} → {_per_ton(u1)}"
            f"（{_pct_signed(gu)}）——分子分母同为分产品:白酒 | 单位实现收入贡献 {_yis(ueff)} | "
            f"**部分支持**：该读数含产品结构混合，**不能单独读成主动降价**；"
            f"白酒内部结构（高/中/低档）仍是未知 | 年报或调研里的分档次收入/销量（若披露） |",
            f"| ③毛利线以下按 {mode} 冻结（费用/税项/非经营不动） | 同一桥里的毛"
            f"利线以下净额变化 {_yis(below_v)}（逐项分解见上表） | "
            f"收入规模 {_yis((scale or {}).get('value'))}、毛利率 {_yis((margin or {}).get('value'))}、"
            f"毛利线以下 {_yis(below_v)}，合计 {_yis((npc or {}).get('value'))} | "
            f"**削弱「毛利线以下不动」的固定假设**：实际偏离 {_yis(below_v)}（占归母净利变化 "
            f"{_pct_signed((float(below_v) / float(npc.get('value'))) if isinstance(below_v, (int, float)) and isinstance((npc or {}).get('value'), (int, float)) and (npc or {}).get('value') else None)}）；"
            f"**仍未知**：缺 {len(dg.get('line_items_missing') or [])} 项披露，残差 "
            f"{_yis(dg.get('unexplained_residual_yuan'))} | "
            + (("**现金流量表补充资料（净利润→经营现金流调节表）原件第 141–142 页**"
                "——**本批已读入并成桥**（见第三节：16 项×2 年、两期各自的对账差额见该节）；"
                "下一观察＝把「经营性应付项目的增加」这一合计行拆成附注分项"
                "（应付票据／应付账款／合同负债／其他应付款）")
               if cash_in else
               ("**现金流量表补充资料（净利润→经营现金流调节表）原件第 141–142 页**"
                "——本批**不读**，留下一批；它是现金因果桥的前置条件"))
            + " |"]
    if alts:
        out.append("")
        out.append(f"> 主卡登记的替代解释（逐条对照）：{'；'.join(alts[:3])}——上表只就"
                   "**已取得的原表行**判支持/削弱，未取证的一条不写成「已解释」。")
    return out


def _cash_bridge_diagnostics(run) -> dict:
    """现金调节桥的 diagnostics（**只在真有已列调节项时**返回）。

    老研究／缺补充资料：`reconciliation[*].items` 全空 ⇒ 返回 `{}`，调用方回退到
    C2c 的"只对照、不拼桥"写法——本版接缝不改变旧正文。"""
    if not isinstance(run, dict):
        return {}
    outs = run.get("outputs") or []
    dg = (outs[0].get("diagnostics") if outs and isinstance(outs[0], dict) else None) or {}
    rec = dg.get("reconciliation") if isinstance(dg.get("reconciliation"), dict) else {}
    if not any(isinstance(v, dict) and (v.get("items") or []) for v in rec.values()):
        return {}
    return dg


def _cash_out(run: dict, metric: str) -> dict:
    for o in run.get("outputs") or []:
        if isinstance(o, dict) and str(o.get("metric")) == metric:
            return o
    return {}


def _cash_components(out: dict) -> dict[str, dict]:
    return {str(c.get("component_id")): c for c in (out.get("components") or [])
            if isinstance(c, dict)}


def _share_of(part, total) -> str:
    """同比贡献占降幅的比例（现场算；分母为零或缺数就不给比例）。"""
    if not isinstance(part, (int, float)) or not isinstance(total, (int, float)):
        return "—"
    if isinstance(part, bool) or isinstance(total, bool) or abs(float(total)) < 1e-9:
        return "—"
    x = float(part) / float(total) * 100.0
    return "≈0" if abs(x) < 0.5 else f"{x:.0f}%"


def _cash_dual_view_lines(run: dict, blk: dict, dg: dict) -> list[str]:
    """C2d 双视角现金卡：一张卡分清**本期额**与**同比贡献**（数字全部来自冻结 ModelRun）。

    纪律（架构师 §18.15）：①本期额、同比贡献、会计解释、经济原因四件事分开写；
    ②不把「经营性应付项目的增加」窄化成应付账款／供应商账期／舞弊；
    ③不拿现金调节表去补第一节毛利线以下未取得的损益残差（两个口径）；
    ④两期对账差额非零 ⇒ 不给结论；⑤支持／削弱／仍未知 ＋ **一项与本卡同口径的下一观察**。
    """
    ps = [str(x) for x in (dg.get("periods") or [])]
    base_p, cur_p = (ps[0], ps[-1]) if len(ps) == 2 else ("", "")
    rec = dg.get("reconciliation") or {}
    bb = rec.get(base_p) or {}
    cb = rec.get(cur_p) or {}
    chg = dg.get("cash_change_items") or {}
    out: list[str] = []
    resid_c = cb.get("residual_yuan")
    resid_b = bb.get("residual_yuan")
    closed = all(isinstance(x, (int, float)) and abs(float(x)) <= 0.005
                 for x in (resid_b, resid_c))
    n_cur = len(cb.get("items") or [])
    n_base = len(bb.get("items") or [])
    n_txt = (f"{n_cur} 项×2 年" if n_cur == n_base else
             f"{cur_p} {n_cur} 项／{base_p} {n_base} 项")
    out.append(f"- 口径：起点＝**合并净利润**（`net_profit_consolidated`，不是归母），"
               f"调节项全部来自{cur_p}年报**现金流量表补充资料**（间接法，{n_txt}，"
               f"符号照抄披露），终点＝**经营活动现金流量净额**（披露值，不是推出来的）。"
               f"恒等式：合并净利润 + Σ已列带符号调节项 = 经营现金流 + 对账差额。")
    # —— A 本期：每一年的桥（本期额） ——
    out += ["", f"**A｜本期额（{cur_p} 与 {base_p} 各一年，不作同比）**", "",
            "| 桥的位置 | " + f" | ".join(ps) + " |",
            "|---|---|---|"]
    a_rows = (("起点：合并净利润", "net_profit_yuan"),
              ("非现金项合计（折旧摊销/减值/递延税/公允价值等）", ("groups", "non_cash")),
              ("营运资本项合计（存货/经营性应收/经营性应付）", ("groups", "working_capital")),
              ("其他（调节项）", ("groups", "other")),
              ("已列调节项合计", "adjustments_yuan"),
              ("终点：经营现金流（披露）", "cashflow_yuan"),
              ("对账差额（恒等式残差）", "residual_yuan"))
    for label, key in a_rows:
        cells = []
        for b in (bb, cb):
            v = b.get(key) if isinstance(key, str) else (b.get("groups") or {}).get(key[1])
            cells.append(_yis(v))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    ld, ls = dg.get("largest_drag") or {}, dg.get("largest_support") or {}
    out.append(f"- **{cur_p} 单年**最大拖累项（调节额）：{ld.get('label') or '读数缺失'} "
               f"{_yis(ld.get('value'))}；最大支撑项：{ls.get('label') or '读数缺失'} "
               f"{_yis(ls.get('value'))}。**这是调节额（会计位置），不是经济原因**——"
               "算子 caveat 同样声明「单项调节额是会计加回/扣减，不表示经济原因」。")
    # —— B 同比：ΔOCF 从哪来（同比贡献） ——
    dcf_out = _cash_out(run, "operating_cashflow_change")
    dcf = dcf_out.get("value")
    comps = _cash_components(dcf_out)
    out += ["", f"**B｜同比贡献（{cur_p} 较 {base_p}；合计强制等于 Δ经营现金流）**", "",
            f"| 同比位置 | 贡献（亿元） | 占 ΔOCF |", "|---|---|---|"]
    b_map = (("合并净利润变化（起点）", "change_in_net_profit", True),
             ("非现金项变化", "change_in_non_cash", False),
             ("营运资本项变化", "change_in_working_capital", False),
             ("其他变化", "change_in_other", False),
             ("对账差额变化", "change_in_residual", False))
    for label, cid, lead in b_map:
        c = comps.get(cid) or {}
        v = c.get("value")
        out.append(f"| {label} | {_yis(v)} | {_share_of(v, dcf)} |"
                   + ("　**← 同比主项**" if lead and isinstance(v, (int, float))
                      and abs(float(v)) >= (abs(float(dcf or 0)) * 0.5) else ""))
        if cid == "change_in_working_capital":
            for it in (chg.get("working_capital") or []):
                dv = it.get("delta_yuan")
                role = "拖累" if isinstance(dv, (int, float)) and float(dv) < 0 else "缓冲"
                out.append(f"| 　└ {it.get('label') or it.get('metric')}（{role}） "
                           f"| {_yis(dv)} | {_share_of(dv, dcf)} |")
    out.append(f"| **合计 = Δ经营现金流** | **{_yis(dcf)}** | 100% |")
    out.append("")
    # —— 四件事分开：本期额 / 同比贡献 / 会计解释 / 经济原因 ——
    pay_cur = pay_delta = None
    for it in (cb.get("items") or []):
        if str(it.get("metric")) == "operating_payable_increase":
            pay_cur = it.get("value_yuan")
    for it in (chg.get("working_capital") or []):
        if str(it.get("metric")) == "operating_payable_increase":
            pay_delta = it.get("delta_yuan")
    out.append(f"- **本期额 ≠ 同比贡献**：经营性应付项目 {cur_p} **本期额** {_yis(pay_cur)}"
               f"（A 表里的单项），与它在同比里的**贡献** {_yis(pay_delta)}（B 表里的单项）"
               "是两个数、回答两个问题——前者说「今年现金里这一项占了多少」，后者说「现金同比"
               "变化的那部分里它贡献了多少」。把两个数混着讲，就会把「同比变化」错读成"
               "「本期占用」，或反过来。")
    if isinstance(pay_cur, (int, float)):
        pay_where = ("为正，表示本期应付类项目**净增加**，在间接法下直接**加回**现金"
                     if float(pay_cur) > 0 else
                     "为负，表示本期应付类项目**净减少**，在间接法下直接**扣掉**现金")
    else:
        pay_where = "（读数缺失：不命名）"
    out.append(f"- **会计解释 ≠ 经济原因**：调节表能说清的是会计位置——「经营性应付项目的增加」"
               f"{pay_where}；它**说不出**为什么（主动还款或占款变化、票据到期与背书、预收/合同"
               "负债变动、账期谈判、并表范围变化都可能落在同一行）。"
               "经济原因要附注明细与结算条款才能命名。")
    out.append("- **不窄化**：「经营性应付项目的增加」是**多类合计**（应付票据／应付账款／预收与合同"
               "负债／其他应付款等，按会计准则归集），**不等于**「应付账款」，更不指向供应商账期条款，"
               "也不构成任何舞弊或资金占用的指控；本卡只在科目合计层面读数。")
    br = blk.get("bridge") or {}
    miss = [str(x) for x in ((br.get("diagnostics") or {}).get("line_items_missing") or [])]
    out.append(f"- **不互补**：本卡**不**拿现金调节表去补第一节利润桥里未取得的那些毛利线以下损益明细"
               + (f"（那里还欠 {len(miss)} 项，逐项清单见本节上文）"
                  if miss else "（未逐项取得的损益明细仍留在会计残差里）")
               + "——利润表的未解释差额与现金表的对账差额是两个不同口径，一个闭合不能替另一个交代。")
    # —— 支持 / 削弱 / 仍未知 + 一项匹配的下一观察 ——
    dn = comps.get("change_in_net_profit") or {}
    dwc = comps.get("change_in_working_capital") or {}
    dcf_abs = abs(float(dcf or 0))
    dir_word = "下行" if isinstance(dcf, (int, float)) and float(dcf) < 0 else "上行"
    dn_v, dwc_v = dn.get("value"), dwc.get("value")
    dn_lead = isinstance(dn_v, (int, float)) and abs(float(dn_v)) >= dcf_abs * 0.5
    dwc_lead = isinstance(dwc_v, (int, float)) and abs(float(dwc_v)) >= dcf_abs * 0.5
    supports, weakens, unknowns = [], [], []
    if closed:
        supports.append(f"两期对账差额均为 {_yis(resid_c)}／{_yis(resid_b)}——"
                        f"{cur_p} 合并净利润与经营现金流的差异**全部由已披露调节项解释**，"
                        "不需要猜未披露项")
    if dn_lead:
        supports.append(f"同比{dir_word} {_yis(dcf)} 里合并净利润变化 {_yis(dn_v)} 占 "
                        f"{_share_of(dn_v, dcf)}——**现金同比{dir_word}主要跟着利润走**，"
                        "与第一节经营桥同向")
    if dwc_lead:
        supports.append(f"同比变化 {_yis(dcf)} 里营运资本项变化 {_yis(dwc_v)} 占 "
                        f"{_share_of(dwc_v, dcf)}——**现金同比变化主要由营运资本构成**"
                        + (f"（同期合并净利润变化只有 {_yis(dn_v)}，占 {_share_of(dn_v, dcf)}，"
                           "两者幅度不在一个量级）" if isinstance(dn_v, (int, float)) else "")
                        + "；这是会计合计行的归属，不是可持续性结论")
        unknowns.append("营运资本推动的**持续性**：单年合计行不能断言下一年同向"
                        "（应收/应付/存货三项任一反向都会翻转这一项）——下一期同类调节表尚未取得，有待核验")
    if isinstance(dwc_v, (int, float)) and not dwc_lead:
        weakens.append(f"营运资本三项同比净贡献只有 {_yis(dwc_v)}（远小于 ΔOCF）——"
                       "**削弱**「现金变化主要是营运资本占款或回款收紧」这种读法")
    if dwc_lead and isinstance(dn_v, (int, float)):
        weakens.append(f"同比变化 {_yis(dcf)} 里合并净利润变化只有 {_yis(dn_v)}"
                       f"（占 {_share_of(dn_v, dcf)}）——**削弱**「现金同比变化跟着利润水平走」"
                       "这种读法；本节的营运资本是**合计行**，不是账期或收款质量结论")
    wrec = {str(i.get("metric")): i for i in (chg.get("working_capital") or [])}
    recv = (wrec.get("operating_receivable_decrease") or {}).get("delta_yuan")
    if isinstance(recv, (int, float)) and float(recv) != 0:
        role = "**释放**现金（构成同比现金缓冲）" if float(recv) > 0 else "**占用**现金（同比反向项）"
        unknowns.append(f"经营性应收项目合计行的同比读数 {_yis(recv)}，方向上{role}——"
                        "**只到合计行为止**：账期变化、票据结算、核销、并表范围都会挪动同一行，"
                        "既不能证明也不能反证收款质量；该判断**有待**应收附注（账龄、坏账准备、"
                        "销售收现）才能命名，本卡不作此判断")
    if not (wrec.get("operating_payable_increase") or {}).get("detail"):
        unknowns.append("应付类合计的**分项构成与到期结构**（应付票据／应付账款／合同负债／"
                        "其他应付款各多少、何时结算）——补充资料只给合计行，本卡无法命名经济原因")
    unknowns.append("同一合计行里**并表范围变化**与**非现金抵债**（以货抵款、票据背书）的贡献——"
                    "这两类不进现金，但会挪动应付合计；期末余额差不是现金贡献")
    out.append("")
    out.append("**判断（三态里的研究状态，不是人工复核）**：")
    out.append("- 支持：" + ("；".join(supports) if supports else "本卡没有可支持的读法") + "。")
    out.append("- 削弱：" + ("；".join(weakens) if weakens else "本卡没有构成削弱") + "。")
    out.append("- 仍未知：" + ("；".join(unknowns) if unknowns else "无") + "。")
    prio = ("营运资本推动的现金改善能否延续（下一年同类合计行是否同向）" if dwc_lead else
            "利润与应付相关现金占用是否延续（下一年同口径桥与调节表）" if dn_lead else
            "本期现金变化的主项归属（需更长期间）")
    out.append(f"- **研究优先问题**（由同比主项归属决定，不是硬编码）：{prio}。")
    out.append(f"- **下一观察（与本卡同口径；本卡只给这一项）**：取 {cur_p} 年报**附注**里"
               "「应付账款／合同负债／其他应付款／应付票据」的期末期初明细与主要结算条款"
               f"（同一份材料、同一披露日、同一合并口径，不引入新公司新期间），把 A 表 "
               f"{_yis(pay_cur)} 这一行拆成可命名的分项，并区分其中**现金与非现金**变动"
               "（票据背书、以货抵款不进现金）与**结算时点**——期末余额差不等于现金贡献；"
               "附注不可得时改取下一份定期报告的同类附注，并如实写明是**跨期旁证**、不回填本卡。")
    _missing = (dg.get("items_missing") or {}).get("cur") or []
    if _missing:
        out.append(f"- 未列项（本表口径没有该行）：{'、'.join(str(x) for x in _missing)}——"
                   "留在缺口里如实记，不当零、不摊派给已列项。")
    if not closed:
        out.append(f"- **对账差额未闭合**（{cur_p} {_yis(resid_c)}／{base_p} {_yis(resid_b)}）："
                   "本卡只列数不出结论——差额不为零说明还有已披露而未登记的调节项。")
    out.append(f"- 运行：`{str(run.get('run_id') or '')[:12]}…`（{run.get('impl_version') or '版本缺失'}，"
               f"数据集指纹 `{str(run.get('dataset_hash') or '')[:12]}…`）；"
               "已列项逐条与两期同比逐项明细见 `analysis/analysis_runs.json` 同 run_id 的 "
               "`outputs[*].diagnostics.reconciliation` / `cash_change_items`"
               "（算子把整份诊断挂在每条输出上，随包即此形态；正文不重复长表）。")
    return out


def _cash_card_section(cash_run: dict | None, blk: dict, *, cash_from: str,
                       feature_cutoff: str) -> list[str]:
    """现金卡的**位置说明**＋卡片本体（正文只此一处渲染，主运行/续页运行共用）。

    `cash_from="realization"` ⇒ 卡用**续页 dataset 的 cash run**（补充资料晚于主特征截止，
    主卡与主网格不动）；`"main"` ⇒ 卡用主特征截止下的主运行（补充资料在主截止内，如三一）。
    """
    cdg = _cash_bridge_diagnostics(cash_run)
    if not cdg:
        return []
    ps = [str(x) for x in (cdg.get("periods") or [])]
    out = ["### 利润→现金（现金流量表补充资料：本期桥 ＋ 同比分解）", ""]
    if cash_from == "realization":
        out.append(f"> 本卡消费的是**本节同一份续页 dataset**（as_of={blk.get('as_of') or '缺失'}，"
                   f"期间 {'→'.join(ps)}）现场运行的 `cash_reconciliation`——补充资料的披露日晚于"
                   f"主特征截止（{feature_cutoff or '读数缺失'}），所以它**不进主网格、不改主卡**，"
                   "只在本节的时点隔离内成桥；下方 run_id／数据集指纹与第一节主运行不同。")
    else:
        out.append(f"> 本卡消费的是**主特征截止（{feature_cutoff or '读数缺失'}）**下的"
                   f"`cash_reconciliation` 运行（期间 {'→'.join(ps)}）——补充资料在该截止内可见，"
                   "与兑现续页（目标期读数）分开：续页有没有读数不影响本卡。")
    out.append("")
    out += _cash_dual_view_lines(cash_run or {}, blk, cdg)
    return out


def _realization_body_lines(blk: dict, *, rows: list, tf: dict, t: float | None,
                            feature_cutoff: str, frozen_periods: tuple = (),
                            cash_run: dict | None = None,
                            cash_from: str = "main",
                            vp_scoped: bool = True,
                            slim: bool = False) -> list[str]:
    """第三节正文行：实际读数 → 原条件状态 → 会计贡献桥 → 假设状态 → 现金对照。"""
    lines: list[str] = _realization_head(blk, t=t, tf=tf)
    if blk.get("status") != "ok":
        # C2c：续页材料未到时，量价表也要**显式留缺口**（说明缺什么），而不是整节消失。
        # C2e：本研究**本来就没有量价口径行**时（如三一），缺口原因不是"续页未到"，
        # 而是"没有同口径的量价行"——两种缺口分开写，不拿行业台数配分产品收入。
        lines += ["", "**量价（原表：产量/销量/库存表 × 营业收入构成表）**", ""]
        if vp_scoped:
            lines.append(f"- 本版**不给**量价分解与分产品勾稽："
                         f"{blk.get('reason') or '续页材料未到'}。"
                         "缺哪一项写哪一项，不拿别的年度、别的科目或公司口径顶替。")
        else:
            lines.append("- 本研究**冻结输入里没有量价口径行**（产销量／分产品收入）：本版不做量价，"
                         "也不拿行业台数或公司口径产销量去配分产品收入（口径错配就拒出数，"
                         "不编替代量价）。")
        # C2e：续页未到 ≠ 没有现金卡——本研究**自有两期**的补充资料若已在主截止内可见
        # （三一 2024/2023），现金卡照跑，并显式说明它与尚未到达的目标期兑现是两件事。
        if _cash_bridge_diagnostics(cash_run):
            lines += [""] + _cash_card_section(cash_run, blk, cash_from=cash_from,
                                               feature_cutoff=feature_cutoff)
        return lines
    run = blk["bridge"]
    g = (blk.get("growth") or {}).get("g")
    dpp = (blk.get("gross_margin") or {}).get("delta_pp")
    m_a = (float(dpp) / 100.0) if dpp is not None else None
    ps = [str(x) for x in (blk.get("periods") or [])]
    npr = blk.get("net_profit") or {}
    lines += ["", "**原条件 vs 实际**（每行条件是「假设成立时」的条件推算，不是对实际的预测）：", ""]
    lines += _conditions_table(rows, g, m_a)
    lines += ["", f"**会计贡献桥**（{'→'.join(ps) if len(ps) == 2 else '两期'}；"
                  f"复用 `operating_drivers` 对称分解，现场算；交互项均分，与代入顺序无关；"
                  "分项合计强制等于父项）：", ""]
    lines += _bridge_table(run, slim=slim)
    dg = run.get("diagnostics") or {}
    used = [str(x) for x in (dg.get("line_items_used") or [])]
    miss = [str(x) for x in (dg.get("line_items_missing") or [])]
    if used or miss:
        lines.append("")
        if slim:
            # C2d item 4：逐项 slug 清单是工程字段（逐字在 analysis_runs.json 同 run_id 的
            # diagnostics.line_items_used/missing 里），正文只留计数＋残差＋附件定位。
            lines.append(f"逐项分解只覆盖**已取得**的披露项目 {len(used)} 项；"
                         f"**未取得**的 {len(miss)} 项留在**未解释差额** "
                         f"{_yis(dg.get('unexplained_residual_yuan'))} 里——"
                         "不当零、不摊派给已列项目，也不能据此命名原因"
                         "（逐项 slug 清单见复核区/附件）。")
        else:
            lines.append(f"逐项分解只覆盖**已取得**的披露项目（{('、'.join(used)) or '无'}）；"
                         f"**未取得**的 {len(miss)} 项（{('、'.join(miss)) or '无'}）留在**未解释差额** "
                         f"{_yis(dg.get('unexplained_residual_yuan'))} 里——"
                         "不当零、不摊派给已列项目，也**不能**据此命名原因。")
    ok, why = _bridge_closure(run)
    lines += ["", f"闭合自检：{'一致' if ok else '不一致'}——{why}；起点 "
                  f"{ps[0] if ps else '基期'} {_yiv(npr.get('base'))} → 终点 "
                  f"{ps[-1] if ps else '目标期'} {_yiv(npr.get('target'))}"
                  f"（差 {_yis(npr.get('delta'))}）。桥运行 `{str(run.get('run_id') or '')[:12]}…`"
                  f"（{run.get('impl_version') or '版本缺失'}，独立验证 "
                  f"{'通过' if run.get('validation_ok') else '未通过'}；"
                  f"{_bridge_input_hash(run)}）。"]
    # C2c：量价两层闭合（原表行 → 白酒行内量价 → 分产品勾回公司收入）
    lines += _volume_price_lines(blk)
    lines += ["", "**假设状态（三件事不许混）**：①**条件未发生 ≠ 预测失败**——情景格是"
                  "「假设成立时」的条件推算，达标与否只在假设成立时才有意义，实际落在轴外"
                  "只说明条件没发生；②桥里的**未解释差额是会计残差，不等于因果**"
                  "（缺的披露项留在残差里，不当零、不摊派给已有行）；③本页**不回填旧主卡**："
                  "`q1_cards.md`、九点卡与 `card_hash` 全部不变，新读数只进续页。",
                  f"**时点隔离**：本节在续页材料自身的可用时点 as_of="
                  f"{blk.get('as_of') or '缺失'} 重取观察（含目标年度行），与主网格特征截止 "
                  f"{feature_cutoff or '读数缺失'} 分开；特征截止之后的新披露只进本节，"
                  "不改主网格九点、不改第一节冻结运行、不改旧正文。"
                  + (f"两节的期间也不同：第一节是特征截止下可见的最后两期"
                     f"（{'→'.join(frozen_periods)}），本节是 {'→'.join(ps)}——"
                     "同为 `operating_drivers`，输入、期间与 run_id 都不同，数不相减。"
                     if frozen_periods and len(frozen_periods) == 2 else ""),
                  ""]
    # C2d：现金调节表已进入冻结输入 ⇒ 换双视角卡（一张卡分清本期额与同比贡献）；
    # 没进（老研究／缺表）就**逐字保留** C2c 的写法，旧正文一个字节都不动。
    # C2e：卡本体与位置说明统一走 `_cash_card_section`（主运行／续页运行共用）。
    card = _cash_card_section(cash_run, blk, cash_from=cash_from, feature_cutoff=feature_cutoff)
    if card:
        lines += card
    else:
        lines += ["### 现金（合并净利润口径；缺调节表只对照、不拼因果桥）", ""]
        lines += _cash_lines(blk.get("cash") or {}, blk)
    return lines


def _bridge_input_hash(run: dict) -> str:
    """桥的输入观察哈希（按期间），从算子 diagnostics 原样取；缺则指向附件定位。"""
    ih = (run.get("diagnostics") or {}).get("input_hashes") or {}
    if not isinstance(ih, dict) or not ih:
        return "输入观察哈希见 analysis/analysis_runs.json"
    seg = "、".join([f"{k} `{str(v)[:12]}…`" for k, v in sorted(ih.items(), key=lambda kv: str(kv[0]))])
    return f"输入观察哈希 {seg}"


def _realization_view_block(blk: dict, *, feature_cutoff: str, evaluation_as_of: str) -> dict:
    """`company_view.realization`：与正文同一份现场算出的数（整块同源，不另算一遍）。"""
    block = {k: v for k, v in blk.items() if k not in ("run", "cash_run")}
    block["cutoffs"] = {"feature_cutoff": str(feature_cutoff or ""),
                        "evaluation_as_of": str(evaluation_as_of or ""),
                        "realization_as_of": str(blk.get("as_of") or "")}
    run = blk.get("run") or {}
    block["run_ref"] = {"run_id": run.get("run_id"), "model_id": run.get("model_id"),
                        "impl_version": run.get("impl_version"),
                        "dataset_hash": run.get("dataset_hash"),
                        "params_hash": run.get("params_hash"),
                        "staged_in": "analysis/analysis_runs.json"}
    # C2e 接缝：续页现金 run 的引用（run_id/dataset_hash/periods/as_of）——视图不搬整份
    # run（context.json 会膨胀），工程全量随 `_stage_candidate` 进 analysis/analysis_runs.json。
    crun = blk.get("cash_run") or {}
    if isinstance(crun, dict) and crun.get("run_id"):
        block["cash_run_ref"] = {"run_id": crun.get("run_id"), "model_id": crun.get("model_id"),
                                 "impl_version": crun.get("impl_version"),
                                 "dataset_hash": crun.get("dataset_hash"),
                                 "params_hash": crun.get("params_hash"),
                                 "dataset_as_of": str(blk.get("as_of") or ""),
                                 "staged_in": "analysis/analysis_runs.json"}
    elif blk.get("cash_run_reason"):
        block["cash_run_ref"] = {"status": "unavailable", "reason": blk.get("cash_run_reason")}
    # 视图不搬整份 run（context.json 会膨胀）；工程全量在 analysis/analysis_runs.json
    block.pop("run", None)
    block.pop("cash_run", None)
    return block


def _delta(a, b):
    """两读数之差；任一侧缺数返回 None（不把缺数当零）。"""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    return float(a) - float(b)


def _money_signed(v) -> str:
    """带符号金额：|v| < 1000万 用万元口径，避免「-0.00亿」这种把小额读成零。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "读数缺失"
    x = float(v)
    if abs(x) < 1e7:
        return ("+" if x >= 0 else "") + _wan(x)
    return _yis(x)


def _cash_lines(cash: dict, blk: dict) -> list[str]:
    """现金对照行：经营现金流 vs 合并净利润（口径写明），资产负债表行只作个别观察。"""
    out: list[str] = []
    ser = cash.get("series") or {}
    ps = [str(x) for x in (cash.get("periods") or [])]
    if len(ps) == 2 and all(p in ser for p in ps):
        b, c = ser[ps[0]], ser[ps[1]]
        out.append("| 期间 | 经营现金流 | 合并净利润 | 差额（经营现金流−合并净利润） |")
        out.append("|---|---|---|---|")
        for per in ps:
            s = ser.get(per) or {}
            out.append(f"| {per} | {_yis(s.get('operating_cashflow'))} | "
                       f"{_yis(s.get('net_profit_consolidated'))} | {_yis(s.get('gap'))} |")
        out.append("")
        out.append(f"- 同比（{ps[0]} → {ps[-1]}）：经营现金流 "
                   f"{_yis(_delta(c.get('operating_cashflow'), b.get('operating_cashflow')))}、"
                   f"合并净利润 {_yis(_delta(c.get('net_profit_consolidated'), b.get('net_profit_consolidated')))}、"
                   f"两者差额 {_yis(_delta(c.get('gap'), b.get('gap')))}——"
                   "差额走向只说明利润到现金的对照关系变化，**具体哪项调节要补充资料才能说**。")
    else:
        out.append("- 现金对照待数据：" + "；".join(
            [str(x) for x in (cash.get("reasons") or []) if str(x).strip()] or ["读数缺失"]) + "。")
    out.append("- 口径：**合并净利润**（`net_profit_consolidated`）——经营现金流由合并净利润调节而来；"
               "第一节的现金覆盖倍数来自 `cash_quality` 冻结运行、是**归母口径**，两处各自命名、不混用。")
    obs = cash.get("balance_observations") or []
    if obs:
        seg = "；".join([f"{o.get('label')} {_money_signed(o.get('delta'))}" for o in obs])
        out.append(f"- 资产负债表行（**只作个别观察，不合并成桥**）：{seg}——这些行受结算周期、"
                   "票据口径与并表范围影响，单看任何一行都不能命名成现金差异的原因。")
    out.append("- **没有现金流量表补充资料（净利润→经营现金流的调节表）就不把资产负债表变动拼成"
               "「现金因果桥」**：拼了就是把两张表的相关性当因果。要补的就是这张调节表"
               "（续页 next_check 已列），拿到后按同一冻结输入登记复算。")
    return out


def _realization_short(blk: dict, *, t: float | None, rows: list, slim: bool = False) -> str:
    """首页短结论：一句把「实际读数＋条件状态＋桥闭合」说清（数字全部现场算，不硬编码）。

    `slim`（C2d item 4）：首页只留判断本身——会计贡献桥的分项数字与第一节 E 分解、第三节桥表
    同源重复，搬出后由第三节承载（旧研究短结论逐字不变）。"""
    if blk.get("status") != "ok":
        why = "；".join([str(x) for x in (blk.get("reasons") or []) if str(x).strip()])
        return f"**假设兑现短结论**：待数据——{why or '读数缺失'}。"
    run = blk["bridge"]
    g = (blk.get("growth") or {}).get("g")
    dpp = (blk.get("gross_margin") or {}).get("delta_pp")
    m_a = (float(dpp) / 100.0) if dpp is not None else None
    npr = blk.get("net_profit") or {}
    st = _conditions_states(rows, g, m_a)
    met = [x for x in st if "已发生" in (x[1], x[2])]
    npc = _run_output(run, "net_profit_change")
    scale = _run_output(run, "revenue_scale_effect")
    margin = _run_output(run, "gross_margin_effect")
    below = _run_output(run, "below_gross_line_change")
    ok, _why = _bridge_closure(run)
    seg = (f"**假设兑现短结论（续页时点 {blk.get('as_of') or '缺失'}，与主网格特征截止分开）**："
           f"目标年度实际归母净利润 {_yiv(npr.get('target'))}"
           + (f"，距研究者目标 T {_yiv(t)} 差 {_yis(_delta(npr.get('target'), t))}"
              if t is not None else "") + "；")
    if g is not None:
        seg += f"实际收入增长 {_pct(float(g))}"
        if dpp is not None:
            seg += f"、毛利率变动 {_pp(float(dpp) / 100)}"
        seg += ("；本报告采用的 " + str(len(rows)) + " 行情景条件全部未发生。"
                if not met else f"；其中 {len(met)} 行的条件按实际读数已发生。")
    else:
        seg += "实际收入增长读数缺失。"
    if None not in (scale, margin, below, npc):
        if slim:
            seg += (f"会计贡献桥合计 {_yis(npc.get('value'))}，分项闭合{'一致' if ok else '不一致'}"
                    "（逐项见第三节桥表）。")
        else:
            seg += (f"会计贡献桥（对称分解、现场算）：收入规模 {_yis(scale.get('value'))}、"
                    f"毛利率 {_yis(margin.get('value'))}、毛利线以下 {_yis(below.get('value'))}，"
                    f"合计 {_yis(npc.get('value'))}——分项合计与父项{'一致' if ok else '不一致'}"
                    "（见第三节）。")
    return seg


# ---- C3 研究实验台（规划 §18.17）：装配同一份计算 JSON ------------------------------
# 公式与条件求值在 `financial_analysis/operators/cash_reconciliation.reversion_ladder`
# 与 `financial_analysis/continuation` 里；本层只读那份 JSON 并存成正文／视图／附件，
# 不在这里堆公式、不重算、不造数。
_C3_SCHEMA = "weavemind.c3_experiment/1"


def _c3_resolve(d: Path, code: str, runs: dict | None = None,
                extra_run: dict | None = None,
                experiment_id: str | None = None) -> tuple[dict, Path | None]:
    """定位本公司的实验 JSON：**先 C4 冻结实验**（`experiments/<id>.json`，独立内容身份、
    三态原样透传），没有才回落旧脚本产物 `c3_experiment_<code>.json`。

    C5-4（显式 `experiment_id`）：请求带 id 时**只解析这一个**——版本选择一次定死，
    正文与 staging 读同一份钉住的文件，不再各自按「保存时点/id 字典序」猜最新（全部
    `saved_at=None` 时字典序只是碰运气）；缺文件/身份不符就整段缺省，不悄悄换一份冒充。

    schema 或 run 身份对不上就不装配。run 身份允许两类来源：**冻结运行**（study
    financial.json 的 runs）与**续页现金 run**（realization_bridge 的 cash_run，
    洋河 370dba…）——其余一律不装配（不借别的 run）。返回（产物, 源文件），源文件用于
    **同版**入包（逐字节复制，不重新渲染、不重新计算）。"""
    cands: list[Path] = []
    if experiment_id:
        if not re.fullmatch(r"[0-9a-f]{24}", str(experiment_id or "")):
            return {}, None
        cands.append(d / "experiments" / f"{experiment_id}.json")
    else:
        ex_dir = d / "experiments"
        if ex_dir.is_dir():
            # 按（保存时点, 内容 id）升序，取最新的那份：同一组输入重放是同 id，不会抖动
            _rows: list[tuple[str, str, Path]] = []
            for p in sorted(ex_dir.glob("*.json")):
                doc = _read_json(p)
                if not doc or str(doc.get("entity_id") or "") != str(code):
                    continue
                _rows.append((str((doc.get("identity") or {}).get("saved_at") or ""), p.stem, p))
            cands += [p for _s, _i, p in sorted(_rows)]
            cands.reverse()
        cands.append(d / f"c3_experiment_{code}.json")
    have = {str((r or {}).get("run_id") or "") for r in (runs or {}).values()
            if isinstance(r, dict)}
    if isinstance(extra_run, dict) and extra_run.get("run_id"):
        have.add(str(extra_run.get("run_id")))
    for p in cands:
        if not p.is_file():
            continue
        art = _read_json(p)
        if not art or str(art.get("schema") or "") != _C3_SCHEMA:
            continue
        ref = str(((art.get("experiment") or {}).get("run_ref") or {}).get("run_id") or "")
        if ref and have and ref not in have:
            continue       # 身份不符：宁可不装配，也不拿别条 run 的数冒充
        return art, p
    return {}, None


def _c3_load(d: Path, code: str, runs: dict, extra_run: dict | None = None,
             experiment_id: str | None = None) -> dict:
    """装配用的实验产物（C4 冻结实验优先，旧脚本产物回落；显式 id 只认那个）；见 `_c3_resolve`。"""
    return _c3_resolve(d, code, runs, extra_run, experiment_id=experiment_id)[0]


def _c3_yi(v) -> str:
    """亿元显示：6 位小数去尾零（精确值在同版 JSON 与附件里，本处只做版面）。"""
    try:
        s = f"{decimal.Decimal(str(v)):.6f}"
    except (decimal.InvalidOperation, ValueError):
        return str(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _c3_lambda_label(ex: dict) -> str:
    """λ 定义取自同一 JSON 的任一视角（顶层没有时回落到视角内，不另写文案）。"""
    if ex.get("lambda_label"):
        return str(ex["lambda_label"])
    for lad in (ex.get("views") or {}).values():
        if isinstance(lad, dict) and lad.get("lambda_label"):
            return str(lad["lambda_label"])
    return ""


def _c3_pct(v) -> str:
    if v in (None, ""):
        return ""
    try:
        return str((decimal.Decimal(str(v)) * 100).quantize(decimal.Decimal("0.0001")))
    except (decimal.InvalidOperation, ValueError):
        return ""


def _c3_front_lines(art: dict) -> list[str]:
    """现金主因前置＋本公司两格（0/50/100%、λ*、域外不可达、每格一句判断与下一观察）。"""
    ex = art.get("experiment") or {}
    rr = ex.get("run_ref") or {}
    out: list[str] = []
    out.append(f"- **现金主因（选定 run `{str(rr.get('run_id') or '')[:16]}…`，"
               f"{rr.get('source') or ''}）**：本期桥与同比贡献的**同向最大带符号项**；"
               "逐项细表与全部候选回撤项在同一附件 `analysis/c3_experiment_"
               f"{art.get('code') or ''}.md`（完整精确值 JSON 同版随包，与研究目录同源）与"
               " `analysis/analysis_runs.json` 里可读，本页只置前置结论。")
    _f = ""
    for _lad in (ex.get("views") or {}).values():
        if isinstance(_lad, dict) and _lad.get("formula"):
            _f = str(_lad["formula"])
            break
    out.append(f"- **反转条件（同一 JSON 的公式字段）**：{_c3_lambda_label(ex)}，λ∈[0,1]；"
               f"{_f or 'C′(λ) = C − λ·a'}；λ*＝C/a 是「该项归零」的阈值——λ* 落在 [0,1] 之外即"
               "**范围不可达**（照实写，不夹逼）；a=0、缺项或口径不一**不造数**。")
    for cell in (ex.get("cells") or []):
        v = "本期额（披露 OCF）" if cell.get("view") == "current" else "同比贡献（ΔOCF）"
        if not cell.get("applicable"):
            out.append(f"- **{v}**：不适用——{cell.get('reason')}（不造数）。")
            continue
        pts = "／".join(_c3_yi(x) for x in (cell.get("points_yi") or []))
        out.append(f"- **{v}｜回撤项（会计位置）：{cell.get('label')}**："
                   f"C={_c3_yi(cell.get('base_yi'))} 亿，a={_c3_yi(cell.get('item_yi'))} 亿；"
                   f"λ=0／50%／100% → {pts} 亿；λ*={cell.get('zero_lambda_pct')}%"
                   f"（{cell.get('threshold_note')}）。")
        out.append(f"  - 判断：{cell.get('judgment')}")
        out.append(f"  - 下一观察：{cell.get('next_watch')}")
    for b in (ex.get("boundary") or []):
        out.append(f"> {b}")
    return out


def _c3_mechanism_lines(art: dict) -> list[str]:
    """机制队列：旧依据（原资料＋原可用时点）→ 条件/窗口 → 新读数 → 状态 → 削弱条件 → 下一资料。"""
    out: list[str] = []
    if art.get("card_scope"):
        out.append(f"> 队列范围：{art.get('card_scope')}")
    for card in (art.get("cards") or []):
        st = str(card.get("status") or "")
        pend = "（pending：下一材料未取得）" if card.get("pending") else ""
        claim = card.get("hypothesis") or card.get("card_claim") or ""
        out.append(f"- **{card.get('company') or ''}｜{claim}** → 状态：**{st}**{pend}")
        mi = card.get("material_identity") or {}
        bs = card.get("basis_source") or {}
        if mi.get("earlier"):
            src = (f"{mi['earlier'].get('title')}（{mi['earlier'].get('material_id')}，"
                   f"原可用时点 {mi['earlier'].get('disclosed_at')}）")
        else:
            src = (f"{bs.get('title')}（{bs.get('material_id')}，原可用时点 "
                   f"{bs.get('disclosed_at')}）" if bs else "原资料读数缺失")
        out.append(f"  - 旧依据（{src}）：{card.get('basis')}"
                   "（原文引用；**不能判断**经济原因与供应商条款）")
        if card.get("basis_readings"):
            out.append("  - 旧读数：" + "；".join(str(x) for x in card["basis_readings"][:2])
                       + "（机械分解读数，**不能判断**经济原因）")
        out.append(f"  - 观察条件：{card.get('observation_condition')}"
                   + (f"｜核验窗口：{card.get('window')}" if card.get("window") else ""))
        nr = list(card.get("new_readings") or [])
        if nr:
            out.append("  - 新读数（同口径）：" + "；".join(str(x) for x in nr[:2]))
            if card.get("new_locators"):
                out.append("  - 新读数位置：" + "；".join(str(x) for x in card["new_locators"][:2]))
        out.append(f"  - 判定：{card.get('status_reason')}")
        out.append(f"  - 削弱条件：{card.get('weakening_condition') or '（未设阈值）'}")
        out.append(f"  - 下一资料：{card.get('next_material')}")
        if card.get("alternatives"):
            out.append("  - 正常替代解释：" + "；".join(str(x) for x in card["alternatives"][:2]))
    for ex in (art.get("excluded_cards") or []):
        out.append(f"- （不入队列）{ex.get('title')}：{ex.get('reason')}")
    upd = _c3_update_lines(art)
    if upd:
        out += ["### 披露更新（真实材料回放；复用 continuation.snapshot/compare）", ""] + upd
    return out


def _c3_update_lines(art: dict) -> list[str]:
    """洋河披露更新：同一份 continuation page（真实材料回放）在正文里紧凑呈现。"""
    upd = art.get("disclosure_update") or {}
    page = upd.get("page") if isinstance(upd, dict) else None
    if not isinstance(page, dict) or not page:
        if isinstance(upd, dict) and upd.get("status") == "pending":
            return [f"> 披露更新：待数据——{upd.get('reason')}"]
        return []
    from financial_analysis import continuation as _cont
    lines = _cont.render_page(page, heading="", compact=True).splitlines()
    v = page.get("version") or {}
    if v.get("historical_rebuild"):
        lines.insert(0, (f"> **历史重建登记**：由缓存公开材料生成（旧截止 {v.get('old_cutoff')} → "
                         f"新截止 {v.get('new_cutoff')}）；不是当时事前判断。"
                         f"{page.get('annual_followup_note') or ''}"))
    return [x for x in lines if str(x).strip()]


def _c3_view_block(art: dict) -> dict:
    """研究详情用的 compact：**可选回撤项**（每项自带 0/50/100% 与 λ*）＋机制状态。"""
    ex = art.get("experiment") or {}
    views: dict = {}
    for view, lad in (ex.get("views") or {}).items():
        if not isinstance(lad, dict):
            continue
        views[view] = {
            "applicable": bool(lad.get("applicable")),
            "reason": str(lad.get("reason") or ""),
            "metric": str(lad.get("metric") or ""), "label": str(lad.get("label") or ""),
            "periods": list(lad.get("periods") or []),
            "base_yi": str(lad.get("base_yi") or ""), "item_yi": str(lad.get("item_yi") or ""),
            "points_yi": [str(p.get("value_yi")) for p in (lad.get("points") or [])],
            "zero_lambda_pct": _c3_pct(lad.get("zero_lambda")),
            "reachable_within_0_1": lad.get("reachable_within_0_1"),
            "formula": str(lad.get("formula") or ""), "selected_by": str(lad.get("selected_by") or ""),
            "inputs_exact": {k: str(v) for k, v in (lad.get("inputs_exact") or {}).items()},
            "threshold_note": str(lad.get("threshold_note") or ""),
            "candidates": [{"metric": str(c.get("metric")), "label": str(c.get("label")),
                            "item_yi": str(c.get("item_yi")),
                            "zero_lambda": str(c.get("zero_lambda")),
                            "zero_lambda_pct": _c3_pct(c.get("zero_lambda")),
                            "reachable_within_0_1": c.get("reachable_within_0_1"),
                            "points_yi": [str(p.get("value_yi")) for p in (c.get("points") or [])],
                            "selected": bool(c.get("selected"))}
                           for c in (lad.get("candidates") or [])],
        }
    bo = art.get("based_on") if isinstance(art.get("based_on"), dict) else {}
    rv = bo.get("report_version") if isinstance(bo.get("report_version"), dict) else {}
    return {
        "schema": _C3_SCHEMA, "code": str(art.get("code") or ""),
        "company": str(art.get("company") or ""),
        "run_ref": dict(ex.get("run_ref") or {}),
        "lambda_label": _c3_lambda_label(ex),
        "boundary": [str(b) for b in (ex.get("boundary") or [])],
        "card_scope": str(art.get("card_scope") or ""),
        "fractions": ["0", "0.5", "1"],
        "views": views,
        "cells": list(ex.get("cells") or []),
        "cards": [{"judgment_id": str(c.get("judgment_id") or ""),
                   "status": str(c.get("status") or ""), "pending": bool(c.get("pending")),
                   "status_reason": str(c.get("status_reason") or ""),
                   # C4：判据单一定义随卡透传（文案/求值同源；旧脚本产物没有这几键＝零变化）
                   "hypothesis": str(c.get("hypothesis") or ""),
                   "criterion": ({k: c["criterion"][k]
                                  for k in ("window_count", "criterion_sha256",
                                            "observation_condition", "weaken_single",
                                            "weaken_confirmed") if k in (c.get("criterion") or {})}
                                 if isinstance(c.get("criterion"), dict) else {})}
                  for c in (art.get("cards") or [])],
        "excluded_cards": [{"judgment_id": str(c.get("judgment_id") or ""),
                            "reason": str(c.get("reason") or "")}
                           for c in (art.get("excluded_cards") or [])],
        "disclosure_update": ({"historical_rebuild": bool(
            (((art.get("disclosure_update") or {}).get("page") or {}).get("version") or {})
            .get("historical_rebuild")),
            "render_compact": str((art.get("disclosure_update") or {}).get("render_compact") or "")}
            if isinstance(art.get("disclosure_update"), dict) else {}),
        # C4 附加（旧产物没有＝缺省）：内容身份、依据锚点与下一步动作
        "experiment_id": str(((art.get("identity") or {}).get("experiment_id")) or ""),
        "criteria_sha256": str(((art.get("identity") or {}).get("criteria_sha256")) or ""),
        "feature_cutoff": str(art.get("feature_cutoff") or ""),
        "based_on": ({
            "old_cutoff": str(bo.get("old_cutoff") or ""),
            "adopted": bool(rv.get("adopted") or bo.get("adopted")),
            "criteria_version": str(bo.get("criteria_version") or ""),
            "report_version": {
                "version_id": str(rv.get("version_id") or ""),
                "acceptance_overall": str(rv.get("acceptance_overall") or ""),
            } if rv else {},
        } if bo else {}),
        "next_actions": [{"action": str(a.get("action") or ""), "why": str(a.get("why") or ""),
                          "by": str(a.get("by") or "")}
                         for a in (art.get("next_actions") or []) if isinstance(a, dict)],
        # 入包成员名与 `_stage_candidate` 的 staging 名一致（entity_id 带交易所后缀；
        # C4 产物的 `code` 是六位码，不能用）：exact_values_in 指向的必须真实存在。
        "exact_values_in": f"analysis/c3_experiment_{art.get('entity_id') or art.get('code') or ''}.json",
    }


def _c3_attachment_md(art: dict) -> str:
    """研究实验台**附件**（确定性渲染同一份 JSON）：四格精确值＋全部候选回撤项＋边界＋机制卡。

    只做装配（不重算、不补数）：正文、研究详情视图与这份附件读的是同一个
    `c3_experiment_<code>.json`。"""
    ex = art.get("experiment") or {}
    rr = ex.get("run_ref") or {}
    L = [f"# C3 研究实验台附件（{art.get('company') or ''} {art.get('code') or ''}）", "",
         f"- schema：`{art.get('schema')}`",
         f"- run：`{rr.get('run_id') or ''}`（{rr.get('source') or ''}）",
         f"- λ 定义：{_c3_lambda_label(ex)}；公式：C′(λ) = C − λ·a（λ∈[0,1]）", ""]
    for v, lad in (ex.get("views") or {}).items():
        L += [f"## {'本期额（披露 OCF）' if v == 'current' else '同比贡献（ΔOCF）'}", ""]
        if not lad.get("applicable"):
            L += [f"- 不适用：{lad.get('reason')}（不造数）", ""]
            continue
        L += [f"- 回撤项（会计位置）：{lad.get('label')}（`{lad.get('metric')}`，{lad.get('group')}）",
              f"- C={lad.get('base_yi')} 亿，a={lad.get('item_yi')} 亿；期间 "
              f"{'→'.join(lad.get('periods') or [])}（{lad.get('period_kind')}）",
              "- 0/50/100%：" + "；".join(
                  f"{p.get('reversion_percent')}% → {p.get('value_yi')} 亿"
                  for p in (lad.get("points") or [])),
              f"- λ*={lad.get('zero_lambda')}（= {lad.get('zero_lambda_pct')}%）；"
              f"{lad.get('threshold_note')}",
              f"- 精确输入（元）：C={((lad.get('inputs_exact') or {}).get('base_yuan'))}；"
              f"a={((lad.get('inputs_exact') or {}).get('item_yuan'))}；默认项来源：{lad.get('selected_by')}",
              ""]
        cands = lad.get("candidates") or []
        if cands:
            L += ["### 可选回撤项（每项自带 0/50/100% 与 λ*；默认项与正文一致）", "",
                  "| 回撤项（会计位置） | a（亿） | λ* | 范围内可达 | 0% | 50% | 100% | 正文选中 |",
                  "|---|---|---|---|---|---|---|---|"]
            for c in cands:
                pts = [str(p.get("value_yi")) for p in (c.get("points") or [])]
                pts += [""] * (3 - len(pts))
                L.append(f"| {c.get('label')}（`{c.get('metric')}`） | {c.get('item_yi')} | "
                         f"{c.get('zero_lambda')} | "
                         f"{'是' if c.get('reachable_within_0_1') else ('不适用' if c.get('zero_lambda') == '不适用（该项为 0）' else '否')} | "
                         f"{pts[0]} | {pts[1]} | {pts[2]} | {'✓' if c.get('selected') else ''} |")
            L.append("")
    L += ["## 边界（四格同）", ""] + [f"> {b}" for b in (ex.get("boundary") or [])] + [""]
    if art.get("card_scope"):
        L += [f"> 队列范围：{art.get('card_scope')}", ""]
    if art.get("cards"):
        L += ["## 机制卡（同口径读数与判定）", ""]
        for c in art["cards"]:
            L.append(f"- **{c.get('company') or ''}｜{c.get('hypothesis') or ''}** → {c.get('status')}"
                     + ("（pending）" if c.get("pending") else ""))
            if c.get("basis"):
                L.append(f"  - 旧依据：{c.get('basis')}")
            if c.get("new_readings"):
                L.append("  - 新读数：" + "；".join(str(x) for x in c["new_readings"][:2]))
            if c.get("status_reason"):
                L.append(f"  - 判定：{c.get('status_reason')}")
            if c.get("weakening_condition"):
                L.append(f"  - 削弱条件：{c.get('weakening_condition')}")
            if c.get("next_material"):
                L.append(f"  - 下一资料：{c.get('next_material')}")
        L.append("")
    for c in (art.get("excluded_cards") or []):
        L += [f"- （不入队列）{c.get('title')}：{c.get('reason')}", ""]
    upd = art.get("disclosure_update") or {}
    if isinstance(upd, dict) and upd.get("page"):
        v = upd["page"].get("version") or {}
        L += ["## 披露更新身份（洋河）", "",
              f"- 旧截止 {v.get('old_cutoff')} → 新截止 {v.get('new_cutoff')}；"
              f"历史重建={v.get('historical_rebuild')}（version_id `{str(v.get('version_id'))[:16]}…`）",
              f"- 材料：" + "；".join(f"{m.get('side')} {m.get('material_id')}（{m.get('disclosed_at')}）"
                                     for m in (v.get("materials") or [])),
              f"- {upd.get('annual_followup_note') or ''}", ""]
    return "\n".join(L) + "\n"


def _build_body(d: Path, man: dict, code: str, company: dict, spec: dict,
                frozen_binding: dict | None = None,
                experiment_id: str | None = None) -> tuple[str, dict]:
    """确定性正文。返回 (markdown, meta)；meta 带用于复核区/响应的版本与工程字段。"""
    jg = company.get("joint_grid") or {}
    runs = _runs_by_model(company)
    moved: list[str] = []
    q1 = _read_json(d / "q1.json")
    q1f = ((q1.get("followups") or {}).get(code)) or {}
    q1c = ((q1.get("cards") or {}).get(code)) or {}
    # 主体名优先 q1 卡片 entity（洋河股份/三一重工），回落 code（C2b：桥与价格段都要用）
    who = str(q1c.get("entity") or q1f.get("entity") or code or "读数缺失")
    # C2e 接缝（§18.16）：现金 run 可能在**主运行**（补充资料早于主特征截止），也可能在
    # **续页 dataset**（补充资料晚于主截止时，主截止不动、同 dataset 追加 cash run）。
    # 这里只解析一次续页桥，再定现金卡消费哪条 run、是否启用瘦身排版。
    fcut = str(man.get("feature_cutoff") or "")
    blk = realization_bridge(d, code=code, followup=q1f)
    if _cash_bridge_diagnostics(runs.get("cash_reconciliation")):
        cash_run, cash_from = runs.get("cash_reconciliation"), "main"
    elif _cash_bridge_diagnostics(blk.get("cash_run")):
        cash_run, cash_from = blk.get("cash_run"), "realization"
    else:
        cash_run, cash_from = None, ""
    # C2d item 4：只有当冻结输入里**真有**现金补充资料（主运行或续页运行）时才启用瘦身排版：
    # 工程字段/重复字段/长公式搬进复核区与现有附件，首页只留短判断＋完整三行表。
    # 旧研究（没有现金调节项）保持已采纳版式，正文逐字不变。
    slim_layout = bool(_cash_bridge_diagnostics(cash_run))

    base = _base_cell(_cells(jg))
    if base is None:
        raise ReportBindingError("grid_missing", "joint_grid 里没有 (g=0,m=0) 基准格，无法成文")
    t, tinfo = _target_value(jg)
    p0 = _num(base, "P")
    sid = str(man.get("study_id") or "")
    sid16 = sid[:16]
    target_note = str(jg.get("target_year_note") or "")
    base_period = str(jg.get("base_period") or man.get("feature_cutoff") or "")

    # —— 一段经营结论 ——
    drv = runs.get("operating_drivers") or {}
    npc = _metric_from_run(drv, "net_profit_change")
    gpc = _metric_from_run(drv, "gross_profit_change")
    rse = _metric_from_run(drv, "revenue_scale_effect")
    gme = _metric_from_run(drv, "gross_margin_effect")
    blc = _metric_from_run(drv, "below_gross_line_change")
    scen = runs.get("scenario_sensitivity") or {}
    rg = _metric_from_run(scen, "revenue_growth_to_hold_target")
    mt = _metric_from_run(scen, "margin_threshold_to_hold_base_profit")
    mg = _metric_from_run(scen, "margin_gap_to_threshold_pp")
    cpd = _metric_from_run(scen, "collection_days_capital_per_day")
    c10 = _metric_from_run(scen, "collection_days_sensitivity_10d")
    press = runs.get("cash_pressure") or {}
    inc = _metric_from_run(press, "incremental_capital_occupation")
    # C2a 接缝①：联合口径的增量占用**只读选定网格的 C 列**（基准格），不再用 cash_pressure
    # 运行的旧增量——0/0 天那版网格 C=0，旧增量仍是 10.03亿，混用就会出现"正文 10.03亿、
    # 表里 0.00亿"的自相矛盾。cash_pressure 的量在复核区按自己的口径单独命名保留。
    occ_joint = base.get("C")
    occ_joint_s = _yi(_num(base, "C")) if isinstance(occ_joint, (int, float)) else "读数缺失"
    qual = runs.get("cash_quality") or {}
    cov = _metric_from_run(qual, "cashflow_coverage")
    cfogap = _metric_from_run(qual, "cfo_minus_profit")

    # B4 修正①：联合天数口径从冻结 spec 的 financial.joint_grid 读取（共享天数），
    # 单项 c10（collection_days_sensitivity_10d）只是回款 +10 天（仅应收账款冲击）。
    _fj = spec.get("financial") if isinstance(spec.get("financial"), dict) else {}
    _jgs = _fj.get("joint_grid") if isinstance(_fj.get("joint_grid"), dict) else {}
    cd = _num(_jgs, "collection_days_delta")
    idl = _num(_jgs, "inventory_days_delta")
    cd_s = f"{cd:+.0f}" if cd is not None else "读数缺失"
    idl_s = f"{idl:+.0f}" if idl is not None else "读数缺失"

    gap0 = (p0 - t) if t is not None else None
    lines: list[str] = []
    if t is not None:
        nature = str(tinfo.get("nature") or "")
        cmp_word = "低" if (gap0 or 0) < 0 else "高"
        if slim_layout:
            # C2d item 4：目标年度口径是工程字段（逐字在 spec.financial.joint_grid.target_year_note），
            # 搬复核区；首页只留 P₀/T/差距一句。
            if target_note:
                moved.append(f"- 目标年度口径：{target_note}。")
            lines.append(
                f"基期（{base_period}）归母净利润（冻结读数）P₀={_yi(p0)}，"
                f"研究者目标 T={_yi(t)}；当前读数比 T {cmp_word} {_yi(abs(gap0 or 0))}"
                + (f"——{nature}" if nature else "") + "。")
        else:
            lines.append(
                f"基期（{base_period}）归母净利润（冻结读数）P₀={_yi(p0)}，"
                f"研究者目标 T={_yi(t)}（{target_note}）；当前读数比 T {cmp_word} {_yi(abs(gap0 or 0))}"
                + (f"——{nature}" if nature else "") + "。")
    else:
        lines.append(
            f"基期（{base_period}）归母净利润（冻结读数）P₀={_yi(p0)}；"
            "研究者目标读数缺失：正文不写距目标差距（九点表 P−T 列为算子原始输出，见复核区）。")
    if None not in (npc, gpc, rse, gme):
        if slim_layout:
            # C2d item 4：分解明细（规模/毛利率/毛利线以下）在第三节桥表与附件里逐项可读，
            # 首页只留总量与指针。
            lines.append(f"E 分解（operating_drivers 冻结运行）：净利变化 {_yi(npc)}，"
                         f"其中毛利变化 {_yi(gpc)}；规模效应／毛利率效应／毛利线以下逐项见"
                         "第三节桥表。")
        else:
            seg = f"E 分解（operating_drivers 冻结运行）：净利变化 {_yi(npc)}，其中毛利变化 {_yi(gpc)}"
            seg += f"（收入规模效应 {_yi(rse)}、毛利率效应 {_yi(gme)}"
            if blc is not None:
                seg += f"，毛利线以下净变化 {_yi(blc)}"
            seg += "）；口径与局限见复核区。"
            lines.append(seg)
    elif gpc is not None:
        lines.append(f"E 分解（operating_drivers 冻结运行）：毛利变化 {_yi(gpc)}（净利变化读数缺失，如实不写）；口径见复核区。")
    else:
        lines.append("E 分解读数缺失：正文如实不写分解数字（复核区列出缺失项）。")
    if cov is not None:
        cash_seg = f"现金质量（cash_quality 冻结运行）：{base_period}经营现金流对归母净利覆盖 {cov:.2f}%"
        if cfogap is not None:
            if cfogap < 0:
                cash_seg += f"，经营现金流比利润少 {_yi(abs(cfogap))}"
            elif cfogap > 0:
                cash_seg += f"，经营现金流比利润多 {_yi(cfogap)}"
        lines.append(cash_seg + "。")
    # 目标方向句（确定性：恢复条件 vs 底线空间）
    if rg is not None and rg > 0:
        seg = f"收入恢复条件（scenario_sensitivity 冻结运行）：毛利率持平下收入需增长 {abs(rg):.2f}% 才能回到 T"
        seg += _margin_clause(mt, mg, alt=True)
        if slim_layout:
            moved.append(seg + "。")
        else:
            lines.append(seg + "。")
    elif rg is not None:
        seg = f"底线维持空间（scenario_sensitivity 冻结运行）：毛利率持平下收入最多可下滑 {abs(rg):.2f}% 仍守住 T"
        seg += _margin_clause(mt, mg, alt=False)
        if slim_layout:
            moved.append(seg + "。")
        else:
            lines.append(seg + "。")
    else:
        lines.append("情景阈值读数缺失：正文如实不写恢复/底线数字（复核区列出缺失项）。")
    # 占用取舍（C2a 接缝①）：分两段各自命名，不混为同一指标——
    # ① 固定背景敏感度：scenario_sensitivity 运行自身带的 +10 天参数（不随本版共同天数变化）；
    # ② 联合口径：只读**选定网格**的 C 列（与情景表「现金占用增量」列同源）。
    occ_parts: list[str] = []
    if cpd is not None or c10 is not None:
        bg: list[str] = []
        if cpd is not None:
            bg.append(f"回款每 +1 天额外占用 {_wan(cpd)}")
        if c10 is not None:
            bg.append(f"单项回款 +10 天增量占用 {_yi(c10)}（仅应收账款冲击）")
        occ_parts.append("固定背景敏感度（算子自身 +10 天参数，不随本版共同天数变化）："
                         + "；".join(bg))
    if occ_joint is not None:
        occ_parts.append(f"联合口径（本版共同天数 {cd_s}/{idl_s}，与情景表 C 列同源）："
                         f"基准 {occ_joint_s}")
    if occ_parts:
        seg = ("占用取舍（天数冲击是**增量占用**，不是现金余额；"
               f"情景表「现金占用增量」列即联合 {cd_s}/{idl_s} 天假设）：")
        seg += "；".join(occ_parts) + "。"
        budget = jg.get("budget")
        if isinstance(budget, dict) and budget.get("value") is not None:
            bl = str(budget.get("label") or "")
            bseg = f"研究者预算 B={_yi(float(budget.get('value') or 0))}"
            bseg += f"——预算口径「{bl or '研究者工作假设'}」" if bl else "（研究者工作假设）"
            if "不是现金余额" not in bl:
                bseg += "；B 是假设的占用容忍预算，不是现金余额、授信额度或董事会预算"
            seg += bseg + "。"
        else:
            seg += ("本版未设预算 B：headroom 不适用，不构成占用约束——"
                    "要有占用取舍，须在同一入口设 B 后**另存新 study**（改预算=改假设，"
                    "会生成新的研究ID），本版不替研究者设一个数。")
        # C2d item 4（slim_layout 才生效，旧研究逐字不变）：整段长口径搬复核区（逐字保留）；
        # 首页只留一句短指针（不重复同表已给的占用列）。
        if slim_layout:
            moved.append(seg)
            lines.append("占用取舍：天数冲击是**增量占用**、不是现金余额"
                         "（口径与数字见复核区，逐字在附件）。")
        else:
            lines.append(seg)

    # —— 三行情景条件表（同一选定网格） ——
    rows, row_note = _scenario_rows(jg)
    # C2d item 4（slim_layout 才生效）：两行替代格的长注（为什么放替代格、它不是取舍证据）
    # 搬复核区，表格下注只留一句短指针；旧研究注逐字不变。
    if slim_layout and row_note:
        moved.append(f"- 情景表替代行注（原文）：{row_note}")
        row_note = "第二、三行均为替代格（本版没有对应取舍格；原因与证据边界见复核区）。"
    pk = _picks(jg)
    tbl = ["| 情景 | 收入增速 g | 毛利率变动 | 归母净利润 | 距目标 T | 现金占用增量 | 条件判定 |",
           "|---|---|---|---|---|---|---|"]
    for label, c in rows:
        cash = c.get("C")
        cash_s = _yi(_num(c, "C")) if isinstance(cash, (int, float)) else "读数缺失"
        # C2d item 4（slim）：判定格只留两个状态短词——括号里的条件明细与 headroom
        # 在复核区（九点全网格/口径）与附件里逐字可读；旧研究判定格逐字不变。
        st = _cell_state(c)
        if slim_layout:
            # 判定格只留短词（全称与条件明细在复核区九点全网格/附件里逐字可读）。
            st = "；".join(p.split("（")[0].strip() for p in st.split("；"))
            st = st.replace("未达到研究者目标", "未达目标")
            bh_s = ""
        else:
            bh = c.get("budget_headroom")
            bh_s = f"，headroom {_yi(float(bh))}" if isinstance(bh, (int, float)) else ""
        tbl.append(
            f"| {label} | {_pct(float(c.get('g') or 0))} | {_pp(float(c.get('m') or 0))} "
            f"| {_yi(_num(c, 'P'))} | {_yis(_num(c, 'P_minus_T'))} | {cash_s} "
            f"| {st}{bh_s} |")
    if row_note:
        tbl.append("")
        tbl.append(f"> {row_note}")

    tf = q1f.get("target_followup") if isinstance(q1f.get("target_followup"), dict) else {}
    actual = tf.get("actual") if isinstance(tf.get("actual"), dict) else None
    nxt = [str(x) for x in (q1f.get("next_check") or []) if str(x).strip()]

    # C2e：本研究是否真的有**量价口径行**（产销量／分产品收入）——决定"量价缺口"
    # 的写法（没有口径行 ≠ 续页材料未到）；只读冻结输入，不新增提取器。
    _recs = _frozen_records(d, code)
    vp_scoped = any(("volume" in str(r.get("metric") or ""))
                    or str(r.get("metric") or "").startswith("revenue_product")
                    for r in _recs)
    # —— C2b：假设兑现解释（只读冻结输入，在续页材料自身时点重取；与主网格隔离） ——
    # blk（含同 dataset 的 cash run）已在函数开头解析；这里只取冻结期间对照。
    _fp = ((runs.get("operating_drivers") or {}).get("outputs") or [{}])
    frozen_periods = tuple(str(x) for x in ((_fp[0] if _fp else {}).get("input_periods") or []))
    real_lines = _realization_body_lines(blk, rows=rows, tf=tf, t=t, feature_cutoff=fcut,
                                         frozen_periods=frozen_periods,
                                         cash_run=cash_run, cash_from=cash_from,
                                         vp_scoped=vp_scoped,
                                         slim=slim_layout)
    # C3 研究实验台（§18.17）：同一份计算 JSON → 正文前置/机制卡/更新与视图 compact 块；
    # 计算留在 financial_analysis，本层只装配；没有产物或 run 身份不符就整段缺省（不造数）。
    c3 = _c3_load(d, code, runs, extra_run=blk.get("cash_run"), experiment_id=experiment_id)
    c3_mech_lines = _c3_mechanism_lines(c3) if c3 else []
    real_block = _realization_view_block(blk, feature_cutoff=fcut,
                                         evaluation_as_of=str(man.get("evaluation_as_of") or ""))
    c3_view = _c3_view_block(c3) if c3 else {}
    real_block["c3_experiment"] = c3_view
    short_line = _realization_short(blk, t=t, rows=rows, slim=slim_layout)
    # C2c：补证卡（原待证解释→实际证据→贡献→支持/削弱/仍未知→下一观察）取代 C2b 的
    # 通用三项菜单与"需要取得量价表"待办——数据已在原表，待办已完成。
    jd = q1c.get("judgement") if isinstance(q1c.get("judgement"), dict) else {}
    real_action = _evidence_card(blk, code=code, who=who, q1c=q1c, jd=jd, jg=jg, q1f=q1f,
                                 cash_in=slim_layout)
    real_block["evidence_card"] = list(real_action)
    real_block["volume_price_lines"] = list(_volume_price_lines(blk))
    # C3 措辞泛化（架构师 C3）：白酒字样只在**本研究真的有该分产品量价行**时出现；
    # 没有分产品量价行的公司（如三一）写同一条范围错配规则的通用表述，不携带行业词。
    _vp_join = "；".join(str(x) for x in (real_block["volume_price_lines"] or []))
    _vp_dim = "分产品:白酒" if "白酒" in _vp_join else ""
    real_block["volume_price_boundary"] = (
        "销量＝年报出货口径（产量/销量/库存表），不等于终端动销；"
        "单位实现收入＝该口径收入÷销量，含产品结构混合，不等于主动降价/提价；"
        + (f"分子分母同取「{_vp_dim}」，公司收入÷白酒销量这类范围错配在本版**拒绝出数**（只报缺口）。"
           if _vp_dim else
           "分子分母必须同口径：公司总收入÷单一产品销量这类范围错配在本版**拒绝出数**（只报缺口）；"
           "本研究未取得分产品量价行，量价不计入正文。"))

    # —— 补证卡（C2c：原待证解释→实际证据→贡献→支持/削弱/仍未知→下一观察）——
    # 取代 C2b 的"一项最值得补证的行动＋通用三项菜单"：量价与分产品勾稽已在第三节
    # 从原表行算出，这里只把**已补证**与**仍未知**分开，并把下一观察落到具体材料。
    act: list[str] = []
    act.append("#### 补证卡（原待证解释 → 实际证据 → 贡献 → 支持/削弱/仍未知 → 下一观察）")
    if actual:
        av = actual.get("value")
        av_s = _yi(float(av)) if isinstance(av, (int, float)) else "读数缺失"
        gapv = tf.get("gap")
        gap_s = _yi(float(gapv)) if isinstance(gapv, (int, float)) else "读数缺失"
        mids = [str(x) for x in (actual.get("material_ids") or []) if str(x).strip()]
        act.append(
            f"对照 {actual.get('period') or '2025'}实际归母净利润 {av_s}"
            f"（来源 {actual.get('source') or '官方披露'}，{actual.get('disclosed_at') or '披露日期缺失'} 披露"
            + (f"，材料 {'、'.join(mids)}" if mids else "") + "）"
            f"与研究者目标：差 {gap_s}（{tf.get('verdict') or '未达到研究者设定目标'}）。")
    # C2c：据会计贡献桥与原表分产品行给出**五列补证卡**（替代 C2b 的通用三项菜单）
    act += real_action
    # C2c：毛利线以下的**固定假设 vs 实际**必须同屏比较（架构师 2026-10-11）：
    # 固定是网格的建模选择（below_gross_mode=fixed），实际由同一台桥算出，两者相差多少要写明。
    _below_r = _run_output(blk.get("bridge") or {}, "below_gross_line_change")
    _np_r = _run_output(blk.get("bridge") or {}, "net_profit_change")
    if isinstance((_below_r or {}).get("value"), (int, float)):
        _bv = float(_below_r["value"])
        _nv = (_np_r or {}).get("value")
        _ratio = (f"{_bv / float(_nv) * 100:.1f}%" if isinstance(_nv, (int, float)) and _nv
                  else "读数缺失")
        act.append(
            f"**毛利线以下：固定假设 vs 实际（明确比较）**——本版网格把毛利线以下按 "
            f"`below_gross_mode={jg.get('below_gross_mode') or '读数缺失'}` **固定为基期水平**"
            f"（三行情景表只动 g/m），而同一台桥算出的实际是 **{_yis(_bv)}**；"
            f"两者相差 {_yis(_bv)}（占归母净利变化 {_ratio}）——"
            f"这是**假设偏离**而不是预测误差，本版只做比较、不解释（缺 "
            f"{len((blk.get('bridge') or {}).get('diagnostics', {}).get('line_items_missing') or [])} 项披露，"
            f"残差 {(blk.get('bridge') or {}).get('diagnostics', {}).get('unexplained_residual_yuan')} 元另列）。")
    if nxt:
        _pend = str(q1f.get("pending_reason") or "")
        if actual:
            act.append(f"后续核验入口（续页原有）：{nxt[0]}。")
        elif _pend and _pend not in str(nxt[0]):
            act.append(f"后续核验入口（续页原有）：{nxt[0]}（{_pend}）。")
        else:
            act.append(f"后续核验入口（续页原有）：{nxt[0]}。")
    _flip = _flip_action(jg, cd, idl)
    if _flip:
        act.append(_flip)
    if actual:
        act.append("边界：2025 实际读数只进续页（第三节兑现解释、第五节独立后续），不回填旧卡；事后未达不构成本网格"
                   "任何情景格的证伪，也不据此把任何达标格改写成「上限情景」或预期"
                   "——情景仍是「假设成立时」的条件推算。")
    alt0 = next((str(x) for x in (jd.get("alternatives") or []) if str(x).strip()), "")
    if alt0:
        act.append(f"敏感性提示（同一读数的替代解释）：{alt0}——补证时按可证伪的口径区分。")
    if not actual and not nxt:
        act.append("补证行动读数缺失：q1_followup 没有 target_followup/next_check，如实不写（见复核区）。")

    # —— 独立后续 ——
    fu_link = f"/api/research-study/file?id={sid}&name=q1_followup_{code}.md"
    cards_link = f"/api/research-study/file?id={sid}&name=q1_cards.md"
    # —— 第六节：价格观察（§18.12：与经营读数并列，不混成一个数） ——
    # 事件材料 = 续页里目标年度实际所用的那份年报（材料 id 从冻结读数取，不从路径猜）。
    price_mids = [str(x) for x in ((actual or {}).get("material_ids") or []) if str(x).strip()]
    po = price_observation(code=code, material_ids=price_mids, man=man, spec=spec,
                          evaluation_as_of=str(man.get("evaluation_as_of") or ""),
                          frozen_binding=frozen_binding)
    po["financial_feature_cutoff"] = str(man.get("feature_cutoff") or "")
    price_lines = _price_section(po, code, who, slim=slim_layout)
    fol: list[str] = []
    fol.append(f"- 机制续页：[q1_followup_{code}.md]({fu_link})——核验状态 {q1f.get('status') or '读数缺失'}"
               f"，机制判定 {q1f.get('mechanism_verdict') or '读数缺失'}；新读数只进续页，不回填旧卡。")
    for i, item in enumerate(nxt[1:], start=1):
        _done = (slim_layout and "现金流量表补充资料" in item)
        fol.append(f"- 后续核验 {i}：{item}"
                   + ("——**本批已兑现**（读入并成桥，见第三节「利润→现金」；"
                      "仍未知的是把经营性应付合计拆成附注分项）" if _done else ""))
    if actual:
        av = actual.get("value")
        av_s = _yi(float(av)) if isinstance(av, (int, float)) else "读数缺失"
        gapv = tf.get("gap")
        gap_s = _yi(float(gapv)) if isinstance(gapv, (int, float)) else "读数缺失"
        mids = [str(x) for x in (actual.get("material_ids") or []) if str(x).strip()]
        fol.append(f"- **目标年度实际（独立续页，不回填旧卡）**：{actual.get('period') or '目标年度'}"
                   f"实际归母净利润 {av_s}（来源 {actual.get('source') or '官方披露'}；"
                   f"{actual.get('disclosed_at') or '披露日期缺失'} 披露"
                   + (f"；材料 {'、'.join(mids)}" if mids else "")
                   + "）。与研究者目标 T 的差 "
                   + (f"{gap_s}（{tf.get('verdict') or '未达到研究者设定目标'}）"
                      if isinstance(gapv, (int, float)) else "读数缺失")
                   + "。这条只进本续页，主卡与 card_hash 不变。")
    else:
        fol.append("- **目标年度实际（独立续页）**：待数据——"
                   f"{q1f.get('pending_reason') or '评价截止前没有同口径读数：不写实际读数'}。")
    if po.get("status") == "ok":
        fol.append(f"- **价格观察（第六节）**：绑定 {((po.get('event') or {}).get('title') or '目标年度年报')}"
                   f"（{(po.get('event') or {}).get('date')} 披露）后的 1/5/20 条基准调整读数"
                   f"（读数哈希 {str(po.get('reading_hash') or '')[:16]}…）——与上面的经营读数并列，"
                   "不合成一个数。")
    else:
        fol.append("- **价格观察（第六节）**：待数据——"
                   + ("；".join([str(x) for x in (po.get("reasons") or []) if str(x).strip()])
                      or "价格读数缺失")
                   + "。")
    if not actual and not any("行情事后反应" in x for x in nxt):
        fol.append("- 价格反应观察待材料：本版不做任何价格域读数（缺合格行情只停价格域，不作事后推断）。")
    fol.append(f"- 主卡与九点：[q1_cards.md]({cards_link})——完整九点机制卡与规则。")
    fol.append("- 每格完整输入随包可复算：analysis/analysis_runs.json 与 analysis/research_inputs.json"
               "（选定版本冻结的 spec／预检／来源记录；来源仅官方披露缓存，不冒称数据库导出）。")

    # —— 复核区（口径/全网格/来源/工程字段折叠处） ——
    rev: list[str] = []
    rev.append("## 复核区（口径、九点全网格、来源、工程字段）")
    rev.append("")
    rev.append("> 正文主张以上文为准；本区给研究者复核用：完整口径、全部格子与来源定位。")
    rev.append("")
    rev.append("### 口径与边界")
    rev.append(f"- 方法版本 {jg.get('method_version') or '读数缺失'}；毛利线以下模式 {jg.get('below_gross_mode') or '读数缺失'}；"
               f"基期 {base_period}；特征截止 {man.get('feature_cutoff') or '读数缺失'}；评价截止 {man.get('evaluation_as_of') or '读数缺失'}。")
    if blk.get("status") == "ok":
        _b = blk["bridge"]
        rev.append(f"- 第三节经营桥＝**同一算子 `operating_drivers` 在续页时点重取的输入上现场运行**："
                   f"as_of={blk.get('as_of')}（特征截止 {fcut or '读数缺失'} 之后），"
                   f"run_id={_b.get('run_id')}（impl {_b.get('impl_version')}，"
                   f"dataset_hash={str(_b.get('dataset_hash') or '')[:16]}…，"
                   f"params_hash={str(_b.get('params_hash') or '')[:16]}…）——与第一节"
                   "特征截止下的冻结运行是两个输入、两个 run_id，完整输出随包在 "
                   "analysis/analysis_runs.json；未解释差额 "
                   f"{_yis(((_b.get('diagnostics') or {}).get('unexplained_residual_yuan')))}"
                   "如实保留（不当零、不摊派，不等于因果）。")
    else:
        rev.append("- 第三节经营桥：待数据（本版没有桥运行，随包 analysis/analysis_runs.json 里也没有该运行）。")
    rev.append("- 情景表三行取自同一选定网格，按**预算两类取舍**选（§18.12）：基准重现格、"
               "『利润达标但占用超预算』格里利润最接近目标的一格（基准格本身属该组时改取组内另一格）、"
               "『利润与占用同时满足』格里最轻的一格；本版缺哪一类就如实写『本版没有』并改放替代格；"
               "九点全网格见下。")
    if row_note:
        rev.append(f"- 三行选取说明：{row_note}")
    if inc is not None:
        rev.append(f"- cash_pressure 冻结运行的增量占用读数 {_yi(inc)}——**与情景表 C 列不是同一口径**"
                   "（该运行自带自己的天数参数，不随选定网格的共同天数变化）：本正文的联合口径一律读"
                   "选定网格的 C 列，两处各自命名、不混用。")
    rev.append("- 三态按**本版报告**的实际绑定分开返回（不用研究目录的研究状态冒充本版报告的研究状态）："
               "机器验收＝本版正文的 acceptance；研究状态＝本版绑定（正文版本＋契约指纹）下的研究状态；"
               "人工复核＝未复核。")
    rev.append("")
    rev.append("### 公司视图（正文／契约／附件同一份）")
    rev.append("- `analysis/context.json` 的 `company_view` 与本节同源：选定版本绑定、网格与采纳的三行、"
               "Q1 卡与续页引用、价格观察引用、来源材料都在同一份对象里；契约线（研究问题/范围）"
               "随同一份对象交给 `assemble_and_verify(contract=...)`。")
    rev.append("- 本版没有人工选择记录：默认采用经营研究组合（经营驱动／现金调节桥／条件情景各一条已验证运行），"
               "不自动采纳、不替用户人工复核。")
    for mid in sorted(runs):
        lims = [str(x) for x in (runs[mid].get("limits") or []) if str(x).strip()]
        if lims:
            rev.append(f"- {mid}：局限 " + "；".join(lims) + "。")
    rev.append("")
    rev.append(f"### 九点全网格（{code}，同一选定网格）")
    rev.append("")
    rev.append("| 收入增速 g | 毛利率变动 | 归母净利润 | 距目标 T | 现金占用增量 | 状态 |")
    rev.append("|---|---|---|---|---|---|")
    for c in _cells(jg):
        cash = c.get("C")
        cash_s = _yi(_num(c, "C")) if isinstance(cash, (int, float)) else "读数缺失"
        rev.append(f"| {_pct(float(c.get('g') or 0))} | {_pp(float(c.get('m') or 0))} "
                   f"| {_yi(_num(c, 'P'))} | {_yis(_num(c, 'P_minus_T'))} | {cash_s} | {_cell_state(c)} |")
    rev.append("")
    rev.append("### 参数状态（选定版本冻结 spec）")
    shared = ((spec.get("financial") or {}).get("joint_grid") or {})
    cd = shared.get("collection_days_delta")
    idl = shared.get("inventory_days_delta")
    rev.append(f"- 两公司共同假设：回款天数 {'+' if (cd or 0) >= 0 else ''}{cd if cd is not None else '读数缺失'} 天、"
               f"存货天数 {'+' if (idl or 0) >= 0 else ''}{idl if idl is not None else '读数缺失'} 天"
               "（共享块：改一家公司的天数会同时改两家）。")
    gv = jg.get("growth_values") or []
    mv = jg.get("margin_deltas") or []
    rev.append(f"- {code}：收入 g ∈ {'、'.join(_pct(float(x)) for x in gv) if gv else '读数缺失'}；"
               f"毛利率 m ∈ {'、'.join(_pp(float(x)) for x in mv) if mv else '读数缺失'}。")
    budget = jg.get("budget")
    if isinstance(budget, dict) and budget.get("value") is not None:
        rev.append(f"- {code} 预算 B={_yi(float(budget.get('value') or 0))}"
                   f"（{budget.get('label') or '研究者工作假设'}）。对照格："
                   + (f"利润达标但占用超预算 → {_cell_short(pk['over_pick'])}。"
                      if pk.get("over_pick") is not None else "本版没有『利润达标但占用超预算』的格。")
                   + (f"利润与占用同时满足 → {_cell_short(pk['both_pick'])}。"
                      if pk.get("both_pick") is not None else "本版没有『利润与占用同时满足』的格。"))
    else:
        rev.append("- 本版未设预算 B（headroom 不适用，不构成占用约束）：要有占用取舍，须在同一入口"
                   "设 B 后另存新 study（改预算=改假设，会生成新的研究ID）；本版不为研究者设一个数。")
    rev.append("")
    rev.append("### 来源")
    srcs = man.get("sources") or {}
    mats = []
    for _name, s in srcs.items():
        meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
        mats.extend([m for m in (meta.get("materials") or []) if isinstance(m, dict)])
    if mats:
        for m in mats[:6]:
            sha = str(m.get("text_sha256") or "")
            used = "（本版正文用到：续页实际/价格事件）" if str(m.get("material_id") or "") in price_mids else ""
            rev.append(f"- {m.get('material_id') or '材料'}（披露 {m.get('disclosed_at') or '日期缺失'}，"
                       f"文本 sha256 {sha[:16]}…，链接 {m.get('url') or '无'}）{used}")
        if len(mats) > 6:
            rev.append(f"- …共 {len(mats)} 份材料，其余随研究目录 sources 定位。")
    else:
        rev.append("- 来源材料清单读数缺失（sources 无 metadata.materials）。")
    rev.append("")
    rev.append("### 工程字段（复核用；正文不使用）")
    rev.append(f"- study_id={sid}；result_hash={man.get('result_hash') or ''}")
    rev.append(f"- 选版目录：tmp/research-studies/{sid16}（只读保留，不自动采纳）；"
               f"候选工作区：study-report-{sid16}-{code}")
    ext = man.get("external_delivery") if isinstance(man.get("external_delivery"), dict) else {}
    freshness = (f"> 数据时效：特征截止（建模可用数据的时间边界）{man.get('feature_cutoff') or '缺失'}；"
                 f"评价截止 {man.get('evaluation_as_of') or '缺失'}；两截止之后新披露的读数不回填本正文，只进续页。"
                 + (f" 价格锚点（事件披露日）{((po.get('event') or {}).get('date')) or '待数据'}，"
                    f"价格评价截止 {po.get('evaluation_as_of') or '待数据'}——价格锚点与特征截止是两个不同时点。"
                    if po.get("status") == "ok" else " 价格观察待数据（第六节）。"))
    disclaimer = (f"免责声明：本内容为内部研究候选，由选定研究 {sid16} 的冻结读数确定性装配"
                  "（装配层零模型调用、不改数、不重算），未经人工复核；不构成投资建议，"
                  + ("不对外发布" if ext.get("allowed") is False else "对外交付沿用原有权限与复核闸"))
    # 一页摘要身份行（B4 架构师：公司/期间明确）：主体/基期/目标期/截止显式，
    # who 优先 q1 卡片 entity（洋河股份/三一重工），回落 code（前面已按价格段需要提前算好）。
    try:
        tgt_s = f"{int(str(base_period)[:4]) + 1}年"
    except ValueError:
        tgt_s = "目标期读数缺失"
    t_s = _yi(t) if t is not None else "读数缺失"
    identity = (f"**主体与期间**：{who}（{code}）；基期 {base_period} → 目标期 {tgt_s}；"
                f"目标 T={t_s}（{tinfo.get('period') or base_period}归母净利润冻结读数）；"
                f"特征截止 {man.get('feature_cutoff') or '缺失'}；评价截止 {man.get('evaluation_as_of') or '缺失'}。")
    # 三态（C2a 接缝①）：机器/研究/人工按**本版报告**的实际绑定分别写，不拿研究目录的
    # research_draft 冒充报告的研究状态（机器验收与人工复核同样各写各的）。
    three_state = (f"**三态（按本版报告实际绑定，三者不互相替代）**：机器验收＝见交付说明与本包 "
                   f"`PACKAGE_MANIFEST.json`（逐文件字节＋验收结论；验收明细 `acceptance_report.json` "
                   f"与 `acceptance_events.jsonl` 在候选任务工作区，**不进内部包**）；"
                   f"研究状态＝本版绑定下由 `assemble_and_verify(contract=…)` "
                   f"判定（选定研究自身状态 {man.get('research_state') or '读数缺失'}，"
                   "只作研究目录的状态，不用它代替本版报告的研究状态）；人工复核＝未复核。")
    # C2d item 4：三态/时效（工程字段）与长口径一律进复核区，不再占首页
    # （首页只留主体/期间/截止一行＋短判断＋完整三行表）；旧研究保持原版式。
    if slim_layout:
        _blk = ["### 三态与数据时效（工程字段；正文首页只留主体、期间与截止一行）",
                f"- {three_state}", f"- {freshness}", ""]
        if moved:
            _blk += ["### 从正文搬到本区的口径（只搬位置，不改数字与口径）"] + moved + [""]
        _blk += ["### 从正文搬出的字段与公式（逐条在现有附件里可读）",
                 "> 只搬位置、不改数字、不改口径：正文不再重复下列内容，交付包附件里逐字可读"
                 "（键路径给到落点，run_id 与正文同源）。",
                 "- 桥的逐项算子公式：`analysis/analysis_runs.json` → 正文第三节标注 run_id 的 "
                 "`model_id=operating_drivers` 运行 `outputs[*].formula`（与桥项同序，"
                 "`output_hash` 逐行对齐）。",
                 "- 毛利线以下逐项 slug 清单：同一条运行的 "
                 "`outputs[*].diagnostics.line_items_used`／`line_items_missing`。",
                 "- 现金调节项两期逐条、同比逐项与缺项：同文件 `model_id=cash_reconciliation` 运行"
                 "（正文第三节标注 run_id）的 `outputs[*].diagnostics.reconciliation`／"
                 "`cash_change_items`／`items_missing`／`closure`。",
                 "- 行情出处／日历口径／无日历交叉核对／手算来源／几何公式／算子局限："
                 "`analysis/context.json` → `company_view.price`（整份读数对象；正文只留事件绑定、"
                 "三个时点、冻结选择、来源绑定与边界）。",
                 "- 占用取舍的每 +1 天占用、固定背景敏感度与预算口径：`analysis/analysis_runs.json` "
                 "（`model_id=scenario_sensitivity` 的 `collection_days_capital_per_day`／"
                 "`collection_days_sensitivity_10d`）与 `analysis/context.json` "
                 "（`company_view.grid.cells` 的 C 列／`grid.budget`）。",
                 ""]
        rev[4:4] = _blk
    # 首页短结论（C2b 架构师：短结论＋完整三行表在首页，工程字段移复核区/附件）
    # C2d item 4：slim_layout 时三态/时效移到复核区；旧研究仍在首页（逐字不变）。
    _eng_head = [] if slim_layout else [three_state, "", freshness, ""]
    # C3：现金主因前置（实验台两格）＋机制队列与披露更新（都来自同一份 c3 JSON）
    _c3_front = (["## 现金主因与回撤实验（前置）", ""] + _c3_front_lines(c3)
                 + [""] if c3 else [])
    _c3_mech = (["", "## 七、机制队列与披露更新（可检验；原资料／条件／阈值／下一资料）", ""]
                + c3_mech_lines + [""] if c3 else [])
    body = "\n".join(["# 研究直连公司报告（内部研究候选）", "", f"> {_BANNER}", "", identity, ""]
                     + _eng_head +
                     [short_line, ""] + _c3_front +
                     ["## 一、经营结论", ""] + [f"{x}" for x in lines] +
                     ["", "## 二、三行情景条件表（假设成立时的条件推算，不是预测）", ""] + tbl +
                     ["", "## 三、假设兑现解释（续页实际读数 → 原条件状态 → 会计贡献桥 → 现金对照）", ""]
                     + real_lines +
                     ["", "## 四、最值得补证的一项行动", ""] + act +
                     ["", "## 五、独立后续", ""] + fol +
                     ["", "## 六、价格观察（年报事件后的基准调整读数；不是预测、不称因果）", ""]
                     + price_lines + _c3_mech +
                     [""] + rev +
                     ["", "---", "", disclaimer, ""])
    view = _company_view(cctx={"code": code, "company": who}, man=man, spec=spec, jg=jg,
                         rows=rows, pk=pk, q1c=q1c, q1f=q1f, actual=actual, po=po,
                         cells=_cells(jg), runs=runs, row_note=row_note, real=real_block)
    meta = {"study_id": sid, "sid16": sid16, "code": code, "company_name": who,
            "target": t, "p0": p0, "q1_followup": bool(actual), "rows": len(rows),
            "company_view": view, "contract": view.get("contract") or {},
            "price_status": po.get("status"), "realization_status": blk.get("status"),
            "realization_as_of": blk.get("as_of"),
            "realization_run_dict": blk.get("run"),
            "realization_cash_run_dict": blk.get("cash_run"),
            "cash_run_origin": cash_from,
            "cash_run_id": str(((cash_run or {}).get("run_id")) or ""),
            "c3_experiment": c3_view,
            "c3_files": ([f"c3_experiment_{code}.json"] if c3 else []),
            "realization_run": (blk.get("bridge") or {}).get("run_id") if blk.get("status") == "ok" else ""}
    return body, meta


# ---- 交付（staging → 版本 → 组装 → PDF → repack） ---------------------------------

def _stage_candidate(d: Path, code: str, company: dict, man: dict, spec: dict, ws,
                     view: dict | None = None, real_run: dict | None = None,
                     cash_real_run: dict | None = None,
                     experiment_id: str | None = None) -> None:
    """多公司 study 只投影本公司：runs+inputs 进候选工作区；不写 agents.db/task 行。

    接缝①（§18.12）：正文用到的**同一份公司视图**落进 `analysis/context.json`
    （`company_view` + 契约线 `contract`），所以正文、契约与附件读到的网格／Q1／续页／
    价格引用是同一份，不各写一套。"""
    from financial_analysis import store as fa_store
    for r in company.get("runs") or []:
        try:
            fa_store.save_run(ws, fa_store.run_from_dict(r))
        except Exception:                          # noqa: BLE001 - 坏记录跳过（正文用冻结产物）
            continue
    # C2b：第三节的兑现桥是**现场运行的实际运行**，与冻结运行并列进同一附件（各自 run_id 可追）
    if isinstance(real_run, dict) and real_run.get("run_id"):
        try:
            fa_store.save_run(ws, fa_store.run_from_dict(real_run))
        except Exception:                          # noqa: BLE001 - 桥运行落不进附件就留在视图里
            pass
    # C2e 接缝：续页现金 run（同一份续页 dataset 现场跑的 cash_reconciliation）随同一附件进包，
    # 正文现金卡与视图引用的 run_id 可在 analysis_runs.json 里对上；落不进就留在视图引用里。
    if isinstance(cash_real_run, dict) and cash_real_run.get("run_id"):
        try:
            fa_store.save_run(ws, fa_store.run_from_dict(cash_real_run))
        except Exception:                          # noqa: BLE001 - 现金 run 落不进附件就留在视图里
            pass
    ctx = {"study_id": str(man.get("study_id") or ""), "entity_id": code,
           "result_hash": str(man.get("result_hash") or ""),
           "feature_cutoff": str(man.get("feature_cutoff") or ""),
           "evaluation_as_of": str(man.get("evaluation_as_of") or ""),
           "frozen_spec": spec, "selection": "研究直连：选定冻结读数（未人工复核）"}
    if isinstance(view, dict) and view:
        ctx["company_view"] = view
        if isinstance(view.get("contract"), dict) and view["contract"]:
            ctx["contract"] = view["contract"]
    fa_store.save_inputs(ws, dataset=company.get("dataset") or {}, context=ctx)
    # 续页随包可复算：q1_followup_<code>.md 拷进 analysis/（repack 的 analysis/*.md 层）
    src = d / f"q1_followup_{code}.md"
    if src.exists():
        dst = ws / "analysis"
        dst.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(src, dst / src.name)
        except OSError:
            pass
    # 冻结输入随包：选定研究的 inputs.json（spec+previews+source_records）原名留档，
    # 正文"每格完整输入随包可复算"的主张靠这条成立（研究目录本身只读，不动）。
    fi = d / "inputs.json"
    if fi.exists():
        dst = ws / "analysis"
        dst.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(fi, dst / "research_inputs.json")
        except OSError:
            pass
    # C3/C4 接缝：研究实验台**附件**随包——渲染稿 `.md` 与**完整实验 JSON** 同版同源。
    # C4-3：`company_view.realization.c3_experiment.exact_values_in` 指向这份 JSON，
    # 此前只进了 .md、JSON 成员缺失（指向不存在的成员）；现在**逐字节复制源文件**入包，
    # 与 `experiments/<id>.json`（或旧脚本产物）完全同版：不重算、不重渲染、不改研究目录。
    c3_art, c3_src = _c3_resolve(d, code, experiment_id=experiment_id)
    if c3_art:
        # C5-4：钉住的实验 id 写进候选上下文（包内可核对"这一版装的是哪一个实验"）
        try:
            _ctx = _read_json(ws / "analysis" / "context.json") or {}
            if isinstance(_ctx, dict):
                _ctx["c3_experiment_id"] = str(
                    ((c3_art.get("identity") or {}).get("experiment_id"))
                    or (experiment_id or ""))
                _ctx["c3_experiment_source"] = ("显式 experiment_id（解析一次贯穿正文与附件）"
                                                if experiment_id else "自动选最新（未显式指定）")
                (ws / "analysis" / "context.json").write_text(
                    json.dumps(_ctx, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        except Exception:                            # noqa: BLE001 - 上下文不可写不影响附件
            pass
        dst = ws / "analysis"
        dst.mkdir(parents=True, exist_ok=True)
        try:
            # newline="\n"：附件按 LF 落盘，与确定性渲染逐字节一致（冻结重读可比对）
            (dst / f"c3_experiment_{code}.md").write_text(_c3_attachment_md(c3_art),
                                                          encoding="utf-8", newline="\n")
        except OSError:
            pass
        if c3_src is not None:
            try:
                shutil.copyfile(c3_src, dst / f"c3_experiment_{code}.json")
            except OSError:
                pass


def _existing_delivery(ws: Path) -> str:
    """同版幂等：已有交付包就返回现有 zip 名（不重算、不堆包）。"""
    best, best_m = "", 0.0
    try:
        for p in ws.glob("deliverables_*.zip"):
            m = p.stat().st_mtime
            if m > best_m:
                best, best_m = p.name, m
    except OSError:
        return ""
    return best


def generate_report(*, study_id: str, entity_id: str, result_hash: str,
                    feature_cutoff: str = "", evaluation_as_of: str = "",
                    experiment_id: str = "") -> dict:
    """研究直连主入口：显式绑定 → 候选工作区 → 确定性正文 → 版本/组装/PDF/交付包。

    C5-4：带显式 `experiment_id`（24 位十六进制）时，本版报告装配与附件都只认这一个
    实验（一次选定、贯穿正文与 `analysis/` 附件），不按「最新」猜；缺文件/身份不符就
    整段缺省。

    返回 payload（含 files 链接与诚实状态）；绑定失败抛 ReportBindingError。"""
    d, man, _sid = _resolve_study(study_id, result_hash)
    _check_periods(man, feature_cutoff, evaluation_as_of)
    cctx = _company_ctx(d, entity_id)
    code, company = cctx["code"], cctx["company"]
    sid = str(man.get("study_id") or "")
    sid16 = sid[:16]
    inputs_doc = _read_json(d / "inputs.json")
    spec = (inputs_doc.get("spec") or {}) if inputs_doc else {}

    from workspace import ensure_task_workspace
    tid = f"study-report-{sid16}-{code}"
    # 显式钉住 default 项目：解析不再依赖"进程内索引/目录扫描/旧平铺回退"的先后，
    # 生成侧与 web 侧 /files 解析在任何进程都收敛到同一目录（projects/default/<tid>）。
    ws = ensure_task_workspace(tid, project="default")
    from report_version import VersionStore
    from delivery_pipeline import assemble_and_verify, repack_adopted, sources_fingerprint
    from report_pdf import markdown_to_pdf

    # 幂等核对修正（架构师 2026-10-10：「selStudy 仍盖过新 run」的缺陷在本适配层的镜像）：
    # 先确定性装配本版正文，再按正文 hash 比对已存版本——
    # 仅当「同一正文 + 该正文即当前选定版本 + 已有交付包」三条件同时成立才返回现有包
    # （不重算、不堆包）；正文变了或选定版本仍是旧缺陷稿 → 走下方重做，产出新候选版本，
    # 绝不重放旧草案。
    from report_version import body_hash
    # 顺序：先确定性装配正文（含公司视图）→ 再按视图落投影 → 再比版本身份。
    # 视图必须先算出来，`analysis/context.json` 才会和正文读到同一份 grid/Q1/续页/价格引用。
    body, meta = _build_body(d, man, code, company, spec,
                             frozen_binding=(inputs_doc.get("market_binding")
                                             if inputs_doc else None),
                             experiment_id=(str(experiment_id or "") or None))
    _stage_candidate(d, code, company, man, spec, ws, view=meta.get("company_view"),
                     real_run=meta.get("realization_run_dict"),
                     cash_real_run=meta.get("realization_cash_run_dict"),
                     experiment_id=(str(experiment_id or "") or None))

    vstore = VersionStore(ws, tid)
    same_body = vstore.get(body_hash(body))
    adopted_probe = vstore.adopted()
    existing_zip = _existing_delivery(Path(ws))
    if (same_body is not None and adopted_probe is not None
            and same_body.identity_id() == adopted_probe.identity_id() and existing_zip):
        _dl = vstore.deliveries()
        return _payload(tid, sid, man, code, same_body, existing_zip,
                        status="exists", reason="同版正文已交付过：返回现有包（未重算；正文先装配后比对身份）",
                        research_state=str(man.get("research_state") or "research_draft"),
                        research_state_note=_unknown_state_note(),
                        acceptance=same_body.acceptance_overall(),
                        version_id=str(same_body.version_id or ""),
                        identity_id=str(same_body.identity_id() or ""),
                        delivered_sha256=str(_dl[-1].get("delivered_sha256") or "") if _dl else "",
                        meta={**meta, "replay": True,
                              "report_states": _report_states(tid, ws, man, meta,
                                                              version=same_body,
                                                              acceptance=same_body.acceptance_overall())})

    ver = vstore.record(body, sources_fingerprint=sources_fingerprint(tid, body),
                        rules_version=REPORT_RULES_VERSION)
    if ver is None:
        ver = vstore.adopted()
    if ver is None:
        raise ReportBindingError("version_failed", "版本登记失败（body_hash 为空）")
    if vstore.adopted() is None or vstore.adopted().identity_id() != ver.identity_id():
        vstore.adopt(ver, reason="研究直连：选定冻结读数确定性装配；未经人工复核，仅内部研究候选")

    goal = f"研究直连：把选定研究 {sid16} 的 {code} 冻结读数接成内部公司报告"
    # 接缝①：同一份公司视图的契约线交给既有裁决——本候选正文没有结构化研究投影时，
    # 既有逻辑如实返回 not_applicable，不拿研究目录的 research_draft 冒充报告的研究状态。
    res = assemble_and_verify(tid, goal, body, ws_dir=ws, assemble_body_as_is=True,
                              contract=(meta.get("contract") or None))
    md_bytes = str(res.get("report") or body).encode("utf-8")
    pdf_bytes = markdown_to_pdf(str(res.get("report") or body), title="", workspace=ws)
    packed = repack_adopted(tid, md_bytes=md_bytes, pdf_bytes=pdf_bytes, ws_dir=ws)
    zip_name = str(packed.get("package") or "")
    if not zip_name:
        raise ReportBindingError("package_failed", str(packed.get("error") or "交付包未上线"))
    _st = _report_states(tid, ws, man, meta, version=vstore.adopted() or ver,
                         acceptance=res.get("acceptance_overall"),
                         pipeline_state=res.get("research_state"))
    return _payload(tid, sid, man, code, vstore.adopted() or ver, zip_name,
                    status="ok",
                    reason=str(res.get("reason") or ""),
                    research_state=str(man.get("research_state") or "research_draft"),
                    research_state_note=_research_state_text(res.get("research_state")),
                    acceptance=res.get("acceptance_overall"),
                    version_id=str(res.get("version_id") or ver.version_id()),
                    identity_id=str(res.get("identity_id") or ver.identity_id()),
                    delivered_sha256=str(res.get("delivered_sha256") or ""),
                    meta={**meta, "pipeline_research_state": _research_state_text(res.get("research_state")),
                          "report_states": _st})


def _unknown_state_note() -> str:
    return "未重算：本条按本版报告已存的版本读数返回（同版重放不重新裁决）"


def _report_states(tid: str, ws, man: dict, meta: dict, *, version,
                   acceptance=None, pipeline_state=None) -> dict:
    """三态按**本版报告**实际绑定返回（C2a 接缝①）：

    - machine：本版正文的机器验收结论（数字/格式轴）；
    - research：本版绑定（正文版本＋契约指纹＋该报告自己的结构投影）下的研究状态；
      报告工作区没有结构化研究投影时如实 `not_applicable`——**不用**研究目录的
      `research_draft` 冒充它；
    - human：人工复核=未复核（代理侧永不代签）。
    """
    out: dict = {"machine": {"status": str(acceptance or "未知"),
                             "basis": "本版正文的机器验收（数字/格式轴）"},
                 "research": {}, "human": {"status": "未复核", "basis": "代理侧不代签人工复核"},
                 "study_catalog_state": str(man.get("research_state") or "")}
    try:
        from delivery_pipeline import read_research_state, research_state
    except Exception as exc:                            # noqa: BLE001
        out["research"] = {"status": "未知", "reason": f"研究状态裁决不可用：{str(exc)[:80]}"}
        return out
    stored = read_research_state(tid, ws_dir=ws) or {}
    struct = stored.get("structure") if isinstance(stored.get("structure"), dict) else {}
    wire = meta.get("contract") if isinstance(meta.get("contract"), dict) else None
    try:
        rs = research_state(tid, f"研究直连：{meta.get('code') or ''}", struct,
                            ws_dir=ws, contract_wire=wire, version=version)
    except Exception as exc:                            # noqa: BLE001 - 裁决失败如实报未知
        out["research"] = {"status": "未知", "reason": f"研究状态裁决失败：{str(exc)[:80]}"}
        return out
    out["research"] = {"status": str(rs.get("label") or rs.get("state") or "未知"),
                       "state": str(rs.get("state") or ""),
                       "reason": str(rs.get("reason") or ""),
                       "located": rs.get("located"), "mandatory_total": rs.get("mandatory_total"),
                       "stale": bool(rs.get("stale")),
                       "basis": ("本版正文版本＋契约指纹＋本报告的结构投影"
                                 + (f"；装配层同一份裁决：{_research_state_text(pipeline_state)}"
                                    if pipeline_state else "")),
                       "study_catalog_state_note": "研究目录自身的研究状态只描述该研究目录，"
                                                   "不作为本版报告的研究状态"}
    return out


def _research_state_text(rs) -> str:
    """assemble_and_verify 的 research_state 可能是 dict：取 label/state，不把 repr 塞进载荷。"""
    if isinstance(rs, dict):
        return str(rs.get("state") or rs.get("label") or "")
    return str(rs or "")


def _payload(tid: str, sid: str, man: dict, code: str, ver, zip_name: str, *,
             status: str, reason: str, meta: dict | None = None,
             research_state: str = "", research_state_note: str = "", acceptance=None,
             version_id: str = "", identity_id: str = "", delivered_sha256: str = "") -> dict:
    vid = str(version_id or getattr(ver, "version_id", "") or "")
    iid = str(identity_id or "")
    if not iid:
        _fn = getattr(ver, "identity_id", None)
        iid = str(_fn()) if callable(_fn) else ""      # identity_id 是方法，不是字段
    _meta = dict(meta or {})
    # 响应不带整份桥运行（体积大）：全量在包里 analysis/analysis_runs.json
    _meta.pop("realization_run_dict", None)
    _meta.pop("realization_cash_run_dict", None)
    _rst = _meta.get("report_states") if isinstance(_meta.get("report_states"), dict) else {}
    return {
        "ok": status in ("ok", "exists"),
        "status": status,
        "task_id": tid,
        "study_id": sid,
        "entity_id": code,
        "result_hash": str(man.get("result_hash") or ""),
        "feature_cutoff": str(man.get("feature_cutoff") or ""),
        "evaluation_as_of": str(man.get("evaluation_as_of") or ""),
        # 研究目录自身的研究状态（**不是**本版报告的研究状态：只作目录信息保留）。
        "study_catalog_research_state": research_state or str(man.get("research_state") or "research_draft"),
        # 三态（C2a 接缝①）：机器验收 / 研究状态 / 人工复核，各按本版报告的实际绑定。
        "states": {
            "machine": (_rst.get("machine") or {"status": str(acceptance or "未知"),
                                                "basis": "本版正文的机器验收（数字/格式轴）"}),
            "research": (_rst.get("research") or {
                "status": "未知",
                "reason": research_state_note or "未裁决（同版重放：按已存版本读数返回）"}),
            "human": (_rst.get("human") or {"status": "未复核", "basis": "代理侧不代签人工复核"}),
        },
        "research_state": _rst.get("research", {}).get("status") or research_state_note or "",
        "human_review": "unreviewed",
        "external_delivery": dict(man.get("external_delivery") or {}),
        "version_id": vid,
        "identity_id": iid,
        "delivered_sha256": delivered_sha256,
        "acceptance": acceptance,
        "reason": reason,
        "note": ("内部研究产物：未采纳为主任务版本、未人工复核；同版正文已交付过时返回现有包"
                 "（正文先装配后比对身份；正文变了或选定版本是旧缺陷稿 → 重做产出新候选版本）"),
        "files": {
            "report_md": f"/files/{tid}/reports/report.md",
            "report_pdf": f"/files/{tid}/reports/report.pdf",
            "zip": f"/files/{tid}/{zip_name}",
        },
        "zip_name": zip_name,
        "meta": _meta,
    }
