# -*- coding: utf-8 -*-
"""**C4 研究实验台装配**（架构师指令 2026-10-11／三域规划 §18.18）。

职责边界（照着裁决写的）：

* **只有装配**。四格的数学在 `operators.cash_reconciliation.reversion_ladder`，
  机制卡与披露更新的假说／读数／判定在 `continuation.snapshot/compare`；
  本模块把「显式 study/result/entity/run ＋ view/metric/fractions ＋ 登记材料」
  装成一份可冻结的实验 JSON，并返回一页摘要与内部 URL。
* **不写常量**：不给公司写死文案、不按「第一条 cash」取数、不引用固定研究/运行 ID、
  不写死任何用户目录。每个格子的判断句都由**该 run 自己的数**算出来。
* **判据只定义一次**（`mechanism_criteria`）：假说标题／观察条件／单窗口削弱／
  两窗口确认由同一函数产出，正文、附件、页面与 `continuation._decide` 的求值
  读的是同一份东西；旧主导项是谁，条件就问「是否仍是**那一项**」——
  三一原主导是营运资本，就不会被写成「利润是否仍主导」；洋河原现金已负，
  ΔOCF 再转负**不算**新的削弱。
* **三态严格分离**：机器验收／研究状态（`not_applicable`）／人工复核（未复核）
  原样透传，本模块不代签、不合并。
* **原研究与冻结包只读**：新实验有**独立内容身份**（`identity.experiment_id`＝
  产物正文哈希），保存只写研究目录下的 `experiments/`，不回写 `study_manifest.json`、
  不改 `result_hash`、不碰任何已交付包。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

__all__ = [
    "SCHEMA", "IMPL", "EXPERIMENTS_DIR",
    "material_roots", "find_registered_material", "scan_registered_materials",
    "list_candidate_runs", "experiment_options",
    "mechanism_criteria", "prepare_company_research_experiment",
    "experiment_summary_markdown", "save_experiment", "load_experiment",
    "list_experiments", "experiment_urls", "experiment_identity",
    "registered_market_inventory", "market_comparison",
]

# 与研究详情／报告适配层既有消费方保持同一 schema（新增字段向后兼容）。
SCHEMA = "weavemind.c3_experiment/1"
IMPL = "financial_analysis.research_experiment/1"
EXPERIMENTS_DIR = "experiments"
CASH_MODEL = "cash_reconciliation"
_YI = Decimal("100000000")

# 现金桥主导项身份 → 中文标签（与 `continuation` 的求值同源，不另编一套说法）。
CASH_DRIVER_LABEL = {"net_profit": "利润项", "working_capital": "营运资本项"}

# 受限材料根（§18.12 纪律：不开放任意路径，只认 `<root>/**/materials/<id>/doc.json`）。
_DEFAULT_MATERIAL_ROOTS = ("evals", "tmp", ".tmp", "docs")
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".mypy_cache"}
_ENV_ROOTS = "WEAVEMIND_MATERIAL_ROOTS"


# --------------------------------------------------------------------- 小工具
def _d(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal(0)


def _yi(v) -> Decimal:
    return (_d(v) / _YI)


def _yi_txt(v, places: str = "0.001") -> str:
    """亿元**显示**（2–3 位）；精确值始终在原字段里，这里只供文案与摘要。"""
    try:
        return str(_yi(v).quantize(Decimal(places)))
    except (ArithmeticError, ValueError):
        return str(v)


def _jsonable(obj):
    """快照里的模型对象（ModelRun/Dataset）按**身份字段**化简，其余原样；
    这样旧快照哈希覆盖真实内容，又不把对象整只塞进 JSON。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    rid = getattr(obj, "run_id", None)
    if rid:
        return {"run_id": str(rid), "dataset_hash": str(getattr(obj, "dataset_hash", "") or "")}
    dh = getattr(obj, "dataset_hash", None)
    if dh:
        return {"dataset_hash": str(dh)}
    return f"<{type(obj).__name__}>"


def _canon(obj) -> str:
    return json.dumps(_jsonable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_obj(obj) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _safe_id(raw, *, pattern: str, what: str) -> str:
    sid = str(raw or "").strip()
    if not sid:
        raise ValueError(f"缺少 {what}")
    if not re.fullmatch(pattern, sid):
        raise ValueError(f"{what} 形态不合法：{sid[:60]!r}")
    return sid


def _view_name(view: str) -> str:
    return "本期现金" if view == "current" else "同比增量（ΔOCF）"


# ------------------------------------------------------------------ 登记材料
def material_roots(*, study_dir=None, extra=()) -> list[Path]:
    """材料检索根：调用方显式给的 → 环境变量 `WEAVEMIND_MATERIAL_ROOTS` →
    仓库内受限默认根。**不含任何按公司/研究写死的路径**。"""
    out: list[Path] = []
    repo = Path(__file__).resolve().parents[1]

    def _add(p) -> None:
        try:
            q = Path(p).resolve()
        except (OSError, RuntimeError):
            return
        if q.is_dir() and all(q != r for r in out):
            out.append(q)

    for item in extra or ():
        _add(item)
    for item in re.split(r"[;]", os.environ.get(_ENV_ROOTS) or ""):
        if item.strip():
            _add(Path(item.strip()))
    if study_dir:
        _add(Path(study_dir))
    for rel in _DEFAULT_MATERIAL_ROOTS:
        _add(repo / rel)
    return out


def _iter_material_docs(root: Path, *, limit: int):
    """受限遍历 `<root>/**/materials/<16hex>/doc.json`（跳过噪声目录，限量）。"""
    found = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [x for x in dirnames if x not in _SKIP_DIRS]
        here = Path(dirpath)
        if here.parent.name != "materials" or not re.fullmatch(r"[0-9a-f]{16}", here.name):
            continue
        if "doc.json" not in filenames or found >= limit:
            continue
        found += 1
        yield here / "doc.json"


def find_registered_material(material_id, *, roots=None, registered=None) -> dict:
    """按 `material_id` 取**登记材料**（doc.json）＋研究登记校验。

    `registered` ＝研究 manifest 里登记的该材料（含 `text_sha256`）：本地文件的哈希
    与登记不符就拒绝当同一材料（不拿别的材料顶）。找不到返回 `{}`。"""
    mid = _safe_id(material_id, pattern=r"[0-9a-f]{16}", what="material_id")
    reg = next((m for m in (registered or []) if str(m.get("material_id") or "") == mid), {})
    exp_text = str(reg.get("text_sha256") or "")
    for base in (roots or material_roots()):
        for p in _iter_material_docs(base, limit=400):
            if p.parent.name != mid:
                continue
            rec = _load_json(p)
            if not rec:
                continue
            got_text = str(rec.get("text_sha256") or "")
            if exp_text and got_text and exp_text != got_text:
                return {"material_id": mid, "error": (
                    f"本地 {got_text[:16]}… 与研究登记 {exp_text[:16]}… 不一致：拒绝当同一材料")}
            if not rec.get("text"):
                continue
            return {"material_id": mid, "material": rec, "path": str(p),
                    "registered_text_sha256": exp_text}
    return {"material_id": mid, "error": f"受限根里没有材料 {mid} 的 doc.json"}


def _material_brief(doc: dict, *, path: str = "") -> dict:
    subj = doc.get("subject") or {}
    return {
        "material_id": str(doc.get("material_id") or ""),
        "title": str(doc.get("title") or ""),
        "company": str(subj.get("company") or ""),
        "company_code": str(subj.get("company_code") or ""),
        "periods": list(doc.get("periods") or []),
        "period_basis": str(doc.get("period_basis") or ""),
        "disclosed_at": str(doc.get("disclosed_at") or ""),
        "date_precision": str(doc.get("date_precision") or ""),
        "text_sha256": str(doc.get("text_sha256") or ""),
        "source_class": str(doc.get("source_class") or ""),
        "channel": str(doc.get("channel") or ""),
        "url": str(doc.get("url") or ""),
        "path": path,
    }


def scan_registered_materials(*, entity_id=None, roots=None, limit: int = 300) -> list[dict]:
    """受限根里的**全部登记材料**摘要（按主体过滤，给页面当候选，不让用户填路径）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for base in (roots or material_roots()):
        for p in _iter_material_docs(base, limit=limit):
            rec = _load_json(p)
            if not rec:
                continue
            brief = _material_brief(rec, path=str(p))
            mid = brief["material_id"]
            if not mid or mid in seen:
                continue
            if entity_id and brief["company_code"] and brief["company_code"] != str(entity_id):
                continue
            seen.add(mid)
            out.append(brief)
    out.sort(key=lambda x: (str(x.get("disclosed_at") or ""), str(x.get("material_id") or "")))
    return out


# ---------------------------------------------------------------- 冻结的 run
def _cash_runs(runs, *, source: str) -> list[dict]:
    out: list[dict] = []
    for r in (runs or []):
        if not isinstance(r, dict):
            continue
        if str(r.get("model_id") or "") != CASH_MODEL:
            continue
        out.append({**r, "_source": source})
    return out


def _run_summaries(runs: list[dict]) -> list[dict]:
    from financial_analysis.operators.cash_reconciliation import reversion_ladder

    out: list[dict] = []
    for r in runs:
        lad = {v: reversion_ladder(r, view=v) for v in ("current", "yoy")}
        ok = next((l for l in lad.values() if l.get("applicable")), None)
        diag = {}
        for o in (r.get("outputs") or []):
            if isinstance(o, dict) and o.get("diagnostics"):
                diag = o["diagnostics"]
                break
        rec = diag.get("reconciliation") or {}
        out.append({
            "run_id": str(r.get("run_id") or ""),
            "model_id": str(r.get("model_id") or ""),
            "impl_version": str(r.get("impl_version") or ""),
            "dataset_hash": str(r.get("dataset_hash") or ""),
            "params_hash": str(r.get("params_hash") or ""),
            "status": str(r.get("status") or ""),
            "source": r.get("_source") or "",
            "entity": str(diag.get("entity") or ""),
            "entity_id": str(diag.get("entity_id") or ""),
            "periods": [str(k) for k in rec.keys()],
            "items_current": len((((rec.get(max(rec, key=lambda k: str(k))) or {})).get("items")) or [])
                             if rec else 0,
            "views": {v: {"applicable": bool(l.get("applicable")),
                          "reason": str(l.get("reason") or ""),
                          "metric": str(l.get("metric") or ""),
                          "label": str(l.get("label") or ""),
                          "base_yi": str(l.get("base_yi") or ""),
                          "item_yi": str(l.get("item_yi") or "")}
                      for v, l in lad.items()},
            "note": ("" if ok else "该 run 没有可回撤的带符号项：选它会如实显示不适用原因（不造数）"),
        })
    return out


def list_candidate_runs(*, study_dir, entity_id, workspace_dir=None,
                        extra_runs=()) -> list[dict]:
    """本公司**可选的已验证 cash run**：研究冻结结果里的主 run ＋（显式给出的）
    候选工作区/续页 run。一条也不默认取第一条：页面拿到每条的适用性与原因再选。"""
    eid = str(entity_id or "").strip()
    if not eid:
        raise ValueError("缺少 entity_id")
    runs: list[dict] = []
    fj = _load_json(Path(study_dir) / "financial.json")
    block = fj.get(eid) if isinstance(fj, dict) else {}
    runs += _cash_runs((block or {}).get("runs"), source="study_frozen")
    if workspace_dir:
        ar = _load_json(Path(workspace_dir) / "analysis" / "analysis_runs.json")
        for r in _cash_runs(ar.get("runs"), source="report_continuation"):
            diag = next((o.get("diagnostics") or {} for o in (r.get("outputs") or [])
                         if isinstance(o, dict) and o.get("diagnostics")), {})
            if str(diag.get("entity_id") or "") in ("", eid):   # 主体不符的不进候选
                runs.append(r)
    for r in (extra_runs or ()):
        if isinstance(r, dict) and str(r.get("model_id") or "") == CASH_MODEL:
            runs.append({**r, "_source": r.get("_source") or "caller_supplied"})
    seen: set[str] = set()
    uniq = []
    for r in runs:
        rid = str(r.get("run_id") or "")
        if not rid or rid in seen or str(r.get("status") or "") != "validated":
            continue
        seen.add(rid)
        uniq.append(r)
    return _run_summaries(uniq)


# -------------------------------------------------------------------- 判据
def _fmt_yuan(v) -> str:
    return _yi_txt(v, "0.01")


def mechanism_criteria(judgment_id: str, *, old_readings=None,
                       window_count: int = 1) -> dict:
    """**判据的唯一来源**：假说标题＋观察条件＋单窗口削弱＋两窗口确认＋判据哈希。

    旧快照读数是输入：旧主导项是谁，条件就问谁；旧 ΔOCF 已为负时，
    「ΔOCF 转负」不构成新的削弱。库存的单窗口削弱与两窗口确认**分开写**，
    正文与求值不得各编一套。"""
    from financial_analysis import continuation as cont

    jid = str(judgment_id or "")
    spec = cont.HYPOTHESES.get(jid) or {}
    old = old_readings or {}
    title = str(spec.get("title") or jid)
    condition = str(spec.get("condition") or "")
    weaken_single = ""
    weaken_confirmed = ""
    basis_note = ""

    if jid == cont._jd.J_CASH_WORKING_CAPITAL:
        driver = str(old.get("cash_driver") or "")
        label = CASH_DRIVER_LABEL.get(driver)
        if label:
            condition = (f"下一期同口径经营现金流变化的**主导项**是否仍是{label}"
                         "（主导项换了才叫方向改变；缓冲大小不改变主导项身份）")
            other = ("营运资本项" if driver == "net_profit" else "利润项")
            # C5（C4 机制复核 actionable#1）：现金卡**只保留现已求值的规则**＝主导项换位。
            # 原先「或 ΔOCF 由正转负」这一支从未被 `_decide` 执行（主导项没换它仍判加强），
            # 显示的削弱条件与求值不一致就是缺口——删掉，不留在文案里冒充判据。
            parts = [f"同口径现金桥主导项换位（{label} → {other}）"]
            if old.get("ocf_change") is not None:
                parts.append("ΔOCF 的符号**不作**新削弱判据（旧口径 ΔOCF 已是 "
                             f"{_fmt_yuan(old.get('ocf_change'))} 亿：转负不是新信息）")
            weaken_single = "；".join(parts)
            weaken_confirmed = "连续两个披露窗口同口径主导项均换位（单窗口只是方向改变信号，不写成结构反转）"
        else:
            condition = ("下一期同口径经营现金流变化的**主导项**是否与本期一致"
                         "（旧快照没有同口径主导项读数：方向不比较，不冒充「仍是利润」）")
            weaken_single = "没有旧主导项读数：不定义换位削弱（缺料就不判，不补条件）"
            weaken_confirmed = "补齐两期同口径现金桥后另立判据"
        basis_note = "主导项身份来自旧快照同口径读数（readings_of.cash_driver），不是标题猜测"
    elif jid == cont._jd.J_INVENTORY:
        weaken_single = "同口径库存量同比转负（**单窗口**削弱：只说明这一期没有继续累积）"
        weaken_confirmed = ("连续**两个**披露窗口库存量同比为负／不再累积，"
                            "才写成库存消化（渠道库存另需附注）")
        basis_note = "企业口径库存量，不代表渠道库存"
    else:
        if spec.get("condition"):
            weaken_single = f"下一期同口径读数不再满足观察条件（{title}）"
        else:
            weaken_single = ""
        weaken_confirmed = "连续两个披露窗口均不满足观察条件"

    core = {"judgment_id": jid, "hypothesis": title, "observation_condition": condition,
            "weaken_single": weaken_single, "weaken_confirmed": weaken_confirmed,
            "window_count": int(window_count)}
    core["criterion_sha256"] = _sha_obj(core)
    core["criterion_source"] = basis_note or "continuation.HYPOTHESES 的假说与观察条件"
    return core


# ---------------------------------------------------------------- 格子文案
def _cell_judgment(*, view: str, base: Decimal, item: Decimal, label: str,
                   points: list[dict], zero_lambda_pct: str,
                   reachable) -> str:
    """一句判断：由**这一格的数**算出来（符号／可达性），不按公司、不按视角键选模板。"""
    nian = _view_name(view)
    p100 = next((p for p in points if _d(p.get("reversion_fraction")) == 1),
                points[-1] if points else {})
    y100 = _yi_txt(p100.get("value_yuan"), "0.001") if p100 else "?"
    if item == 0:
        return "该项为 0：没有可回撤的单项（不造数）。"
    if item > 0 and base > 0:
        if reachable:
            return (f"撤回该项 {zero_lambda_pct}% 时{nian}归零：正{nian}对该项**敏感**——"
                    "「构成支撑」只是同口径会计位置，附注分项之前不命名经济原因（其他项固定的反事实）。")
        return (f"100% 回撤后仍 {y100} 亿（λ* 域外：**范围不可达**，不夹逼）："
                f"正{nian}不是被这一单项撑住的；「构成支撑」仍是会计位置判读，"
                "不能据此判断供应商账期、结算条款或回款质量。")
    if item < 0 and base < 0:
        if reachable:
            return (f"解除该项负拖累 {zero_lambda_pct}% 时{nian}归零（100% 后 {y100} 亿）："
                    f"负{nian}对该项拖累**敏感**——拖累的判读仅限会计位置，"
                    "有待应付／应收／存货附注分项才能命名经济原因。")
        return (f"解除该项负拖累仍为负（100% 后 {y100} 亿，λ* 域外，范围不可达）："
                f"负{nian}不是这一单项造成；撤回／解除都**不是**下一期改善预测。")
    if item > 0 and base < 0:
        return (f"该项在负{nian}里是正贡献（缓冲）：撤回只会更负（100% 后 {y100} 亿）；"
                "缓冲的会计位置不等于供应商条款或回款质量证据。")
    return (f"该项在正{nian}里是拖累：撤回会抬高{nian}（100% 后 {y100} 亿）；"
            "这是同口径会计反事实，不是下一期现金预测。")


def _next_actions(*, picked: dict, materials_after: list[dict],
                  feature_cutoff: str, run_ref: dict,
                  exclude_material_id=None) -> list[dict]:
    """两项**下一动作**：由所选回撤项与登记材料算出，不为凑数编材料。

    已经在本实验里用过的较新材料不再当成"下一步"。"""
    acts: list[dict] = []
    sel = picked or {}
    if sel.get("label"):
        acts.append({
            "action": f"取「{sel['label']}」的下一层分项（现金流量表补充资料细项＋相关附注）"
                      "与结算时点，再判读它的作用",
            "why": "本实验只给同口径会计位置：期末余额差不等于现金贡献，分项与结算时点才谈得上"
                   "供应商账期／回款质量（当前材料不足，不预判）",
            "run_ref": {"run_id": run_ref.get("run_id"), "dataset_hash": run_ref.get("dataset_hash")},
            "metric": sel.get("metric"),
        })
    todo = [m for m in (materials_after or [])
            if str(m.get("material_id") or "") != str(exclude_material_id or "")]
    nxt = todo[0] if todo else None
    if nxt:
        acts.append({
            "action": f"登记并回放 {nxt['title']}（{nxt['disclosed_at']}，material "
                      f"{nxt['material_id']}）作为较新一侧，再看同一判据是否延续",
            "why": f"旧截止 {feature_cutoff} 之后的同主体新披露；历史重建按历史重建标注，"
                   "不称事前盲测",
            "material_id": nxt["material_id"],
        })
    else:
        acts.append({
            "action": "下一动作待定（pending）：受限根里没有该主体在旧截止之后的已登记材料，"
                      "不为凑成功取数",
            "why": f"需要旧截止 {feature_cutoff} 之后的同主体披露；未登记就如实 pending",
            "material_id": None,
        })
    while len(acts) < 2:
        acts.append({"action": "在同口径两期上复核公式闭合（合并净利润＋Σ带符号调节项＝经营现金流＋对账差额）"
                               "，闭合不上就标缺口",
                     "why": "四格读数的可信度取决于恒等式是否闭合；这是可自查的技术前置，不是新模型",
                     "material_id": None})
    return acts[:2]


# ------------------------------------------------------------------ 快照侧
def _period_spec(doc: dict):
    """由**材料自己**给的报告期推出快照参数（不按公司、不按标题猜年份）。"""
    raw = [str(x) for x in (doc.get("periods") or [])]
    half = [x for x in raw if re.search(r"H\d", x)]
    if half:
        m = re.match(r"(\d{4})H(\d)", half[-1])
        if m:
            y, h = int(m.group(1)), int(m.group(2))
            return (y - 1, y), (f"{y - 1}H{h}", f"{y}H{h}"), "half"
    years = [int(x) for x in raw if re.fullmatch(r"\d{4}", x)]
    if len(years) >= 2:
        yrs = sorted(years)[-2:]
        return tuple(yrs), tuple(f"{y}年" for y in yrs), "annual"
    if years:
        # 单一年度（材料只登记了本年，如 2025 年报只给 ['2025']）：年报自带上一年
        # 比较期——与上面 H1 分支同一类**由材料自己的报告期推导**，不按标题猜年份。
        y = years[-1]
        return (y - 1, y), (f"{y - 1}年", f"{y}年"), "annual"
    return None, None, "annual"


def _snap_of(doc: dict, *, entity_id: str, company: str, side: str, as_of: str):
    from financial_analysis import continuation as cont

    periods, labels, kind = _period_spec(doc)
    if not periods:
        raise ValueError(f"材料 {doc.get('material_id')} 没有两期可比报告期，不做快照")
    code = re.sub(r"\D", "", str(entity_id or ""))[:6] or str(entity_id or "")
    snap = cont.snapshot(doc, company=company or str((doc.get("subject") or {}).get("company") or ""),
                         company_id=str(entity_id or ""), code=code, periods=periods,
                         as_of=as_of, disclosed_at=str(doc.get("disclosed_at") or ""),
                         label=str(doc.get("title") or ""), period_kind=kind,
                         period_labels=labels, limit=12)
    mat = _material_brief(doc)
    mat["side"] = side
    mat["period_kind"] = kind
    mat["period_labels"] = list(labels)
    mat["used_as_cutoff"] = as_of
    snap_id = {"snapshot_label": snap.get("label"), "source": dict(snap.get("source") or {}),
               "dataset": snap.get("dataset"), "periods": list(periods),
               "period_kind": kind, "period_labels": list(labels)}
    return snap, mat, snap_id, kind


# --------------------------------------------------------------- 主装配函数
def prepare_company_research_experiment(*, study_dir, study_id=None, result_hash=None,
                                        entity_id, run_id, view="current", metric=None,
                                        fractions=("0", "0.5", "1"),
                                        old_material_id=None, new_material_id=None,
                                        workspace_dir=None, roots=None,
                                        technical_source=None, based_on_version=None,
                                        version_workspace=None, version_task_id=None,
                                        extra_runs=(),
                                        baseline_experiment_id=None, market=None) -> dict:
    """**唯一的生产装配入口**（普通 API／脚本都调它）。

    输入全部显式：`study_dir`（研究目录）、`study_id`/`result_hash`（冻结身份）、
    `entity_id`、`run_id`（主 run 或续页 cash run）、`view`/`metric`/`fractions`、
    以及登记材料 `old_material_id`/`new_material_id`。返回实验 JSON（普通 dict）＋
    一页摘要文本＋内部 URL；数值来自 `reversion_ladder`，更新来自 `continuation`；
    缺材料／缺项就如实不适用，**不造数**。"""
    from financial_analysis.operators.cash_reconciliation import (
        REVERSION_BOUNDARY, REVERSION_SCHEMA, reversion_ladder)

    eid = str(entity_id or "").strip()
    if not eid:
        raise ValueError("缺少 entity_id")
    study_dir = Path(study_dir)
    man = _load_json(study_dir / "study_manifest.json")
    sid = str(study_id or man.get("study_id") or study_dir.name)
    rhash = str(result_hash or man.get("result_hash") or "")
    if result_hash and man.get("result_hash") and str(result_hash) != str(man.get("result_hash")):
        raise ValueError(f"result_hash 与研究 {sid} 的冻结登记不符（不冒充同一结果）")
    cutoff = str(man.get("feature_cutoff") or "")
    as_of = str(man.get("evaluation_as_of") or "")

    # ① 选 run：只在**本公司已验证的 cash run**里按 run_id 取（不取第一条）
    all_runs = list_candidate_runs(study_dir=study_dir, entity_id=eid,
                                   workspace_dir=workspace_dir, extra_runs=extra_runs)
    chosen = next((r for r in all_runs if r["run_id"] == str(run_id)), None)
    if chosen is None:
        raise ValueError(f"run {run_id} 不属于研究 {sid}／主体 {eid} 的已验证 cash run 候选")
    run_obj = _find_run_object(study_dir=study_dir, entity_id=eid, run_id=str(run_id),
                               workspace_dir=workspace_dir, extra_runs=extra_runs)
    diag = next((o.get("diagnostics") or {} for o in (run_obj.get("outputs") or [])
                 if isinstance(o, dict) and o.get("diagnostics")), {})
    if str(diag.get("entity_id") or "") not in ("", eid):
        raise ValueError(f"run {run_id} 的主体是 {diag.get('entity_id')}，与 {eid} 不符（不串公司）")
    company = str(diag.get("entity") or "") or _company_of(study_dir, eid)

    views_want = ("current", "yoy")
    ladder_by_view = {v: reversion_ladder(run_obj, view=v,
                                         metric=(metric if v == str(view) else None),
                                         fractions=[_d(f) for f in fractions])
                      for v in views_want}

    cells: list[dict] = []
    for v in views_want:
        lad = ladder_by_view[v]
        if not lad.get("applicable"):
            cells.append({"view": v, "applicable": False, "reason": lad.get("reason"),
                          "boundary": list(REVERSION_BOUNDARY),
                          "run_id": lad["refs"]["run_id"]})
            continue
        pts = lad.get("points") or []
        cells.append({
            "view": v, "applicable": True, "metric": lad["metric"], "label": lad["label"],
            "group": lad["group"], "periods": list(lad.get("periods") or []),
            "base_yuan": lad["base_yuan"], "item_yuan": lad["item_yuan"],
            "base_yi": lad["base_yi"], "item_yi": lad["item_yi"],
            "points": pts,
            "points_yi": [str(p.get("value_yi")) for p in pts],
            "zero_lambda": lad["zero_lambda"], "zero_lambda_pct": lad["zero_lambda_pct"],
            "reachable_within_0_1": lad["reachable_within_0_1"],
            "threshold_note": lad["threshold_note"], "selected_by": lad["selected_by"],
            "item_source": lad.get("item_source") or "",
            "judgment": _cell_judgment(view=v, base=_d(lad["base_yuan"]),
                                       item=_d(lad["item_yuan"]), label=lad["label"],
                                       points=pts, zero_lambda_pct=lad["zero_lambda_pct"],
                                       reachable=lad["reachable_within_0_1"]),
            "boundary": list(REVERSION_BOUNDARY),
        })

    registered = _study_registered_materials(man)
    mat_roots = list(roots or material_roots(study_dir=study_dir))
    after = [m for m in scan_registered_materials(entity_id=eid, roots=mat_roots)
             if m["disclosed_at"] and cutoff and m["disclosed_at"] > cutoff]
    picked = next((c for c in ((ladder_by_view.get(str(view)) or {}).get("candidates") or [])
                   if c.get("selected")), {})
    if not picked.get("metric"):
        picked = next((c for lad in ladder_by_view.values()
                       for c in (lad.get("candidates") or []) if c.get("selected")), {})
    next_actions = _next_actions(picked=picked, materials_after=after,
                                 feature_cutoff=cutoff,
                                 run_ref={"run_id": chosen["run_id"],
                                          "dataset_hash": chosen["dataset_hash"]},
                                 exclude_material_id=new_material_id)

    experiment = {
        "schema": REVERSION_SCHEMA,
        "formula": next((l.get("formula") for l in ladder_by_view.values() if l.get("formula")),
                        "C′(λ) = C − λ·a"),
        "boundary": list(REVERSION_BOUNDARY),
        "lambda_label": next((l.get("lambda_label") for l in ladder_by_view.values()
                              if l.get("lambda_label")), ""),
        "run_ref": {"run_id": chosen["run_id"], "model_id": chosen["model_id"],
                    "impl_version": chosen["impl_version"],
                    "dataset_hash": chosen["dataset_hash"], "params_hash": chosen["params_hash"],
                    "source": chosen["source"], "status": chosen["status"]},
        "selection": {"view": str(view), "metric": metric, "fractions": [str(_d(f)) for f in fractions],
                      "note": "指定视角用显式候选项，另一视角按既有默认（与 C 同向的最大带符号项）"},
        "views": ladder_by_view,
        "cells": cells,
    }

    # ② 机制卡＋披露更新。C5-3 分两条路：
    #    - 显式 `baseline_experiment_id`：原判断**沿已冻结实验**（读数/判据/来源照抄，
    #      不现场重建旧判断冒原假说）；
    #    - 无原版：历史重建路径（page.version.historical_rebuild=True 明示，不称原假说）。
    baseline: dict = {}
    if str(baseline_experiment_id or "").strip():
        b_doc = load_experiment(study_dir=study_dir,
                                experiment_id=str(baseline_experiment_id).strip())
        if not b_doc:
            raise ValueError(f"baseline_experiment_id {baseline_experiment_id} "
                             f"在研究 {sid} 里不存在（从已保存实验里选）")
        if str(b_doc.get("entity_id") or "") != eid:
            raise ValueError(f"基线实验主体是 {b_doc.get('entity_id')}，与 {eid} 不符（不串公司）")
        if str(b_doc.get("study_id") or "") != sid:
            raise ValueError("基线实验属于另一研究（study_id 不符）：原判断不能跨研究沿用")
        b_ident = b_doc.get("identity") or {}
        baseline = {
            "experiment_id": str(b_ident.get("experiment_id")
                                 or str(baseline_experiment_id).strip()),
            "content_sha256": str(b_ident.get("content_sha256") or ""),
            "criteria_sha256": str(b_ident.get("criteria_sha256") or ""),
            "old_material_id": str((b_doc.get("based_on") or {}).get("old_material_id") or ""),
            "artifact": b_doc,
        }
        # 较早一侧材料沿用基线登记的旧材料（旧 `disclosed_at <= cutoff` 在冻结基线里已核）
        old_material_id = old_material_id or baseline["old_material_id"]
    if baseline:
        cards, excluded, update, snapshot_ids = _mechanism_block_frozen(
            baseline=baseline, new_material_id=new_material_id, entity_id=eid,
            company=company, cutoff=cutoff, registered=registered, roots=mat_roots,
            run_ref=experiment["run_ref"])
    else:
        cards, excluded, update, snapshot_ids = _mechanism_block(
            old_material_id=old_material_id, new_material_id=new_material_id,
            entity_id=eid, company=company, cutoff=cutoff, registered=registered,
            roots=mat_roots, ladder_by_view=ladder_by_view, run_ref=experiment["run_ref"])

    # 版本引用（技术来源）：调用方显式给 VersionStore 记录，或给工作区让本模块取
    # **真实字段**（`continuation._old_version_identity`，不再猜 `body_sha256`/`ok`）。
    if based_on_version is None and version_workspace:
        from financial_analysis import continuation as _cont

        ver = _cont._old_version_identity(Path(version_workspace),
                                          str(version_task_id or Path(version_workspace).name))
        if ver.get("frozen"):
            based_on_version = ver
    art = {
        "schema": SCHEMA,
        "impl": IMPL,
        "study_id": sid,
        "result_hash": rhash,
        "entity_id": eid,
        "code": re.sub(r"\D", "", eid)[:6] or eid,
        "company": company,
        "feature_cutoff": cutoff,
        "evaluation_as_of": as_of,
        "generated_note": ("确定性装配：四格数值＝financial_analysis 小纯函数 reversion_ladder"
                           "（本模块只装配，不重算）；机制卡／披露更新＝continuation.snapshot/compare"
                           "（判据由 mechanism_criteria 一次定义，文案与求值同源）；"
                           "三态分离：机器验收／研究状态 not_applicable／人工复核 未复核。"),
        "research_state": {
            "state": _state_of(man),
            "human_review": str(man.get("human_review") or ""),
            "human_review_label": _review_label(man.get("human_review")),
            "external_delivery_allowed": bool((man.get("external_delivery") or {}).get("allowed")),
            "experiment_state": "not_applicable",
            "experiment_state_note": ("本实验不产出研究结论（无评级／目标价／仓位）："
                                      "实验自身的结论状态 not_applicable；研究登记状态与人工复核"
                                      "**原样透传**，不合并、不由代理代签。"),
        },
        "experiment": experiment,
        "cards": cards,
        "excluded_cards": excluded,
        "card_scope": _card_scope(cards, excluded),
        "disclosure_update": update,
        "next_actions": next_actions,
        "snapshot_identities": snapshot_ids,
        "based_on": _based_on(old_material_id=old_material_id, new_material_id=new_material_id,
                              registered=registered, roots=mat_roots, snapshot_ids=snapshot_ids,
                              cards=cards, based_on_version=based_on_version,
                              technical_source=technical_source, sid=sid, rhash=rhash,
                              old_cutoff=cutoff,
                              baseline={k: v for k, v in baseline.items() if k != "artifact"}
                              if baseline else None),
        "available_runs": all_runs,
    }
    # ③ 市场对照（C5-2，可选）：显式选披露事件＋行情集/日历/评价时点时才有这一段；
    #    数值来自 event_returns，许可随读数走；**并列**在经营支持/削弱与下一观察旁边。
    if market:
        mk = dict(market or {})
        mc = market_comparison(entity_id=eid,
                               material_id=str(mk.get("material_id") or ""),
                               price_dataset_id=str(mk.get("price_dataset_id") or ""),
                               calendar_id=str(mk.get("calendar_id") or ""),
                               evaluation_as_of=str(mk.get("evaluation_as_of") or ""),
                               benchmark=str(mk.get("benchmark") or ""),
                               roots=mat_roots, registered=registered)
        mc["alongside_operations"] = [
            {"judgment_id": str(c.get("judgment_id") or ""),
             "title": str(c.get("hypothesis") or c.get("judgment_id") or ""),
             "status": str(c.get("status") or ""),
             "pending": bool(c.get("pending"))} for c in cards]
        mc["next_observation"] = next((str(a.get("action") or "") for a in next_actions), "")
        art["market_comparison"] = mc
    ident = art["based_on"]
    art["identity"] = {
        "impl": IMPL,
        "content_sha256": _sha_obj({k: v for k, v in art.items() if k != "identity"}),
        "criteria_sha256": ident.get("criteria_sha256") or "",
        "old_snapshot_sha256": ident.get("old_snapshot_sha256") or "",
        "run_ref": experiment["run_ref"],
        "study_result_hash": rhash,
        "note": ("新实验的**独立内容身份**：与原 study/result_hash 无回写关系；"
                 "原研究与已交付包只读，本实验另存 `experiments/`。"),
    }
    art["summary_markdown"] = experiment_summary_markdown(art)
    return art


def _find_run_object(*, study_dir, entity_id, run_id, workspace_dir=None, extra_runs=()):
    fj = _load_json(Path(study_dir) / "financial.json")
    block = fj.get(str(entity_id)) if isinstance(fj, dict) else {}
    pool = list((block or {}).get("runs") or [])
    if workspace_dir:
        pool += list((_load_json(Path(workspace_dir) / "analysis" / "analysis_runs.json")
                      .get("runs") or []))
    pool += [r for r in (extra_runs or ()) if isinstance(r, dict)]
    hit = next((r for r in pool if isinstance(r, dict) and str(r.get("run_id") or "") == run_id
                and str(r.get("model_id") or "") == CASH_MODEL), None)
    if hit is None:
        raise ValueError(f"找不到 run {run_id}（只认研究冻结结果／显式候选工作区里的 cash run）")
    return hit


def _company_of(study_dir, entity_id) -> str:
    fj = _load_json(Path(study_dir) / "financial.json")
    block = fj.get(str(entity_id)) if isinstance(fj, dict) else {}
    for r in ((block or {}).get("runs") or []):
        for o in (r.get("outputs") or []):
            d = (o or {}).get("diagnostics") or {}
            if d.get("entity"):
                return str(d["entity"])
    return ""


def _state_of(man: dict) -> str:
    """研究状态原样透传（manifest 里可能是字符串或 dict；不代签、不合并）。"""
    rs = man.get("research_state")
    if isinstance(rs, dict):
        return str(rs.get("state") or "not_applicable")
    return str(rs or "not_applicable")


_REVIEW_LABELS = {"unreviewed": "未复核", "reviewed": "已复核", "": "未登记"}


def _review_label(raw) -> str:
    """人工复核标签：只做**显示映射**，不改变原值、不替用户判定。"""
    key = str(raw or "").strip().lower()
    return _REVIEW_LABELS.get(key, f"未登记（原值 {raw!r}）")


def _study_registered_materials(man: dict) -> list[dict]:
    """研究 manifest 里**登记过的材料**（sources.*.metadata.materials，去重保序）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for blk in (man.get("sources") or {}).values():
        for m in (((blk or {}).get("metadata") or {}).get("materials") or []):
            mid = str(m.get("material_id") or "")
            if mid and mid not in seen:
                seen.add(mid)
                out.append(dict(m))
    return out


def _card_scope(cards: list[dict], excluded: list[dict]) -> str:
    st = "／".join(f"{c.get('judgment_id')}:{c.get('status')}" for c in cards[:6])
    pend = sum(1 for c in cards if c.get("pending"))
    return (f"机制队列 {len(cards)} 张（判据由 mechanism_criteria 一次定义；pending {pend} 张）："
            f"{st or '没有可比旧的登记材料，全部 pending'}。"
            f"未入队列 {len(excluded)} 条。现金**缓冲结构**子判断随更新页呈现，不重复计卡。")


def _mechanism_block(*, old_material_id, new_material_id, entity_id, company, cutoff,
                     registered, roots, ladder_by_view, run_ref):
    """旧快照 → 同公司新披露：机制卡＋披露更新页（判据单一定义，缺料如实 pending）。"""
    from financial_analysis import continuation as cont

    cards: list[dict] = []
    excluded: list[dict] = []
    update: dict = {}
    snap_ids: dict = {}
    if not old_material_id:
        update = {"status": "pending",
                  "reason": "没有选定旧机制快照材料：机制卡与更新页都需要「较早一侧」，不硬凑"}
        return cards, excluded, update, snap_ids
    got_old = find_registered_material(old_material_id, roots=roots, registered=registered)
    if not got_old.get("material"):
        update = {"status": "pending",
                  "reason": got_old.get("error") or "旧材料取不到（受限根里没有／哈希与登记不符）"}
        return cards, excluded, update, snap_ids
    old_doc = got_old["material"]
    snap_old, mat_old, snap_ids["earlier"], old_kind = _snap_of(
        old_doc, entity_id=entity_id, company=company, side="earlier", as_of=cutoff)
    e_read = cont.readings_of(snap_old)
    snap_ids["earlier"]["snapshot_sha256"] = _sha_obj(
        {k: v for k, v in snap_old.items() if not str(k).startswith("_")})

    if not new_material_id:
        update = {"status": "pending", "reason": "没有选定较新披露材料：只出旧机制卡，不做更新",
                  "material_identity": {"earlier": mat_old}}
        cards = _cards_from(snap_old, e_read, mat_old, None, entity_id=entity_id, company=company)
        return cards, excluded, update, snap_ids
    got_new = find_registered_material(new_material_id, roots=roots, registered=registered)
    if not got_new.get("material"):
        update = {"status": "pending",
                  "reason": got_new.get("error") or "新材料取不到（未登记／不在受限根里）",
                  "material_identity": {"earlier": mat_old}}
        cards = _cards_from(snap_old, e_read, mat_old, None, entity_id=entity_id, company=company)
        return cards, excluded, update, snap_ids
    new_doc = got_new["material"]
    if str((new_doc.get("subject") or {}).get("company_code") or "") not in ("", entity_id):
        update = {"status": "blocked",
                  "reason": (f"新材料主体是 {(new_doc.get('subject') or {}).get('company_code')}"
                             f"，与 {entity_id} 不符：不把本公司身份贴到别家材料上"),
                  "material_identity": {"earlier": mat_old}}
        return cards, excluded, update, snap_ids
    new_disclosed = str(new_doc.get("disclosed_at") or "")
    if cutoff and new_disclosed and new_disclosed <= cutoff:
        update = {"status": "blocked",
                  "reason": f"新材料可用时点 {new_disclosed} 不晚于旧截止 {cutoff}：不构成新披露",
                  "material_identity": {"earlier": mat_old}}
        return cards, excluded, update, snap_ids

    snap_new, mat_new, snap_ids["later"], new_kind = _snap_of(
        new_doc, entity_id=entity_id, company=company, side="later", as_of=new_disclosed)
    l_read = cont.readings_of(snap_new)
    snap_ids["later"]["snapshot_sha256"] = _sha_obj(
        {k: v for k, v in snap_new.items() if not str(k).startswith("_")})
    page = cont.compare(snap_old, snap_new, select_ids=cont.proposition_ids())
    page["version"] = {
        "old_cutoff": cutoff, "new_cutoff": new_disclosed,
        "new_cutoff_source": "较新材料自身 disclosed_at（登记材料给出，不由页面传入）",
        "historical_rebuild": True,
        "period_kind": dict(page.get("period_kind") or {}),
    }
    page["annual_followup_note"] = (
        "年度目标不成熟：本期只有半年度读数，年度判断要等下一份年度披露，不倒灌。"
        if new_kind == "half" else "")
    # 判据挂到每一行（单一定义 → 正文/附件/页面读同一份）
    for row in (page.get("hypotheses") or []):
        crit = mechanism_criteria(str(row.get("judgment_id") or ""), old_readings=e_read,
                                  window_count=1)
        row["criteria"] = crit
        row["observation_condition"] = crit["observation_condition"]
        row["weaken_single"] = crit["weaken_single"]
        row["weaken_confirmed"] = crit["weaken_confirmed"]
        row["satisfied"] = ("单窗口削弱已满足；两窗口确认未满足（需要再一个同口径窗口）"
                            if str(row.get("status") or "") == cont.STATUS_WEAKER
                            else "本窗口未触发削弱（方向信号不写成结构反转）")
        row["criterion_sha256"] = crit["criterion_sha256"]
    update = {
        "page": page,
        "render_compact": cont.render_page(page, heading="## 披露更新（真实材料回放）", compact=True),
        "render_full": cont.render_page(page, heading="## 披露更新（真实材料回放）", compact=False),
        "material_identity": {"earlier": mat_old, "later": mat_new},
        "run_ref": dict(run_ref),
    }
    cards = _cards_from(snap_old, e_read, mat_old, (snap_new, l_read, mat_new),
                        entity_id=entity_id, company=company)
    if not (snap_new.get("judgments") or []):
        excluded.append({"judgment_id": "", "title": "较新材料没有可入队列的判断",
                         "reason": "同口径读数缺失：不新建模型、不从静态文档抄金额补数"})
    return cards, excluded, update, snap_ids


def _frozen_cards(baseline, rows, mat_new, bid) -> list[dict]:
    """机制卡：**冻结卡的字段照抄**（判据/来源/下一观察不动），状态与本期读数按新窗口重判。

    C5-3：这是"保留原假说"的关键——不现场重建旧判断；能变的只有本期方向（`_decide`）。
    """
    from financial_analysis import continuation as cont

    art = baseline.get("artifact") or {}
    old_mat = ((art.get("disclosure_update") or {}).get("material_identity") or {}).get("earlier") or {}
    out: list[dict] = []
    for fc in (art.get("cards") or []):
        jid = str(fc.get("judgment_id") or "")
        row = (rows or {}).get(jid)
        card = dict(fc)
        card["baseline_experiment_id"] = bid
        card["earlier_side_source"] = "experiments 冻结读数（baseline_experiment_id 显式沿用，不现场重建）"
        if mat_new:
            card["material_identity"] = {
                "earlier": (fc.get("material_identity") or {}).get("earlier") or old_mat,
                "later": mat_new}
        if row:
            card.update({
                "window": "同口径新窗口（原判据/来源沿冻结实验）", "pending": False,
                "status": row.get("status"), "status_reason": row.get("status_reason"),
                "decision_basis": row.get("decision_basis"),
                "new_readings": row.get("new_readings"), "new_locators": row.get("new_locators"),
                "next_watch": row.get("next_watch"), "alternatives": row.get("alternatives"),
                "materials": row.get("materials"),
                "criterion": row.get("criteria") or fc.get("criterion") or {},
                "criterion_sha256": (row.get("criterion_sha256")
                                     or str(fc.get("criterion_sha256") or "")),
                "satisfied": ("单窗口削弱已满足；两窗口确认未满足（需要再一个同口径窗口）"
                              if str(row.get("status") or "") == cont.STATUS_WEAKER else
                              "本窗口未触发削弱（方向信号不写成结构反转）"),
            })
        else:
            card["pending"] = True
            card["window"] = "本期窗口没有这一行：冻结卡原样保留（不硬凑读数）"
        out.append(card)
    return out


def _mechanism_block_frozen(*, baseline, new_material_id, entity_id, company, cutoff,
                            registered, roots, run_ref):
    """**沿已冻结实验的原判断**更新（C5-3，显式 `baseline_experiment_id` 时走这条）。

    较早一侧不从材料现场重建：读数/判据/下一观察/来源照抄冻结页（`compare_frozen`），
    只把新材料算成较新一侧并重新判定。没有原版时才走 `_mechanism_block` 的历史重建路径
    （那条在 `page.version.historical_rebuild=True` 与摘要里明示"历史重建"，不冒充原假说）。
    """
    from financial_analysis import continuation as cont

    art = baseline.get("artifact") or {}
    bid = str(baseline.get("experiment_id") or "")
    frozen_update = dict(art.get("disclosure_update") or {})
    frozen_page = dict(frozen_update.get("page") or {})
    ref = {
        "experiment_id": bid,
        "content_sha256": str(baseline.get("content_sha256") or ""),
        "criteria_sha256": str(baseline.get("criteria_sha256") or ""),
        "source": "研究目录 experiments/<id>.json（冻结产物，只读）",
        "rule": ("原判断沿冻结读数与判据：不现场重建、不重算旧材料；"
                 "本期只判「上一期的压力/结构是否延续」"),
    }
    if not frozen_page.get("hypotheses") or not ((frozen_page.get("readings") or {}).get("earlier")):
        return [], [], {"status": "pending", "baseline": ref,
                        "reason": (f"基线实验 {bid[:12]}… 里没有冻结的更新页读数："
                                   "不现场重建冒充原假说")}, {}
    got_new = (find_registered_material(new_material_id, roots=roots, registered=registered)
               if new_material_id else {})
    if not got_new.get("material"):
        return ([], [], {"status": "pending", "baseline": ref,
                         "reason": got_new.get("error") or "没有选定较新披露材料",
                         "material_identity": dict(frozen_update.get("material_identity") or {})}, {})
    new_doc = got_new["material"]
    if str((new_doc.get("subject") or {}).get("company_code") or "") not in ("", entity_id):
        return ([], [], {"status": "blocked", "baseline": ref,
                         "reason": (f"新材料主体是 {(new_doc.get('subject') or {}).get('company_code')}"
                                    f"，与 {entity_id} 不符：不把本公司身份贴到别家材料上")}, {})
    new_disclosed = str(new_doc.get("disclosed_at") or "")
    if cutoff and new_disclosed and new_disclosed <= cutoff:
        return ([], [], {"status": "blocked", "baseline": ref,
                         "reason": f"新材料可用时点 {new_disclosed} 不晚于旧截止 {cutoff}：不构成新披露"}, {})
    snap_new, mat_new, snap_id_new, new_kind = _snap_of(
        new_doc, entity_id=entity_id, company=company, side="later", as_of=new_disclosed)
    snap_id_new["snapshot_sha256"] = _sha_obj(
        {k: v for k, v in snap_new.items() if not str(k).startswith("_")})
    frozen_page["baseline_ref"] = ref
    page = cont.compare_frozen(frozen_page, snap_new, baseline_ref=ref)
    frozen_ver = dict((frozen_page.get("version") or {}))
    page["version"] = {
        "old_cutoff": str(frozen_ver.get("old_cutoff") or cutoff),
        "new_cutoff": new_disclosed,
        "new_cutoff_source": "较新材料自身 disclosed_at（登记材料给出，不由页面传入）",
        "historical_rebuild": False,
        "baseline_experiment_id": bid,
        "baseline_criteria_sha256": ref["criteria_sha256"],
        "baseline_note": ("原判断沿已冻结实验：较早一侧读数/判据/来源照抄，不由本次重建；"
                          "无 baseline_experiment_id 时才是历史重建"),
        "period_kind": dict(page.get("period_kind") or {}),
    }
    page["annual_followup_note"] = (
        "年度目标不成熟：本期只有半年度读数，年度判断要等下一份年度披露，不倒灌。"
        if new_kind == "half" else "")
    for row in (page.get("hypotheses") or []):
        row["satisfied"] = ("单窗口削弱已满足；两窗口确认未满足（需要再一个同口径窗口）"
                            if str(row.get("status") or "") == cont.STATUS_WEAKER
                            else "本窗口未触发削弱（方向信号不写成结构反转）")
    rows_by_id = {str(r.get("judgment_id") or ""): r for r in (page.get("hypotheses") or [])}
    frozen_mi = dict(frozen_update.get("material_identity") or {})
    update = {
        "page": page,
        "render_compact": cont.render_page(page, heading="## 披露更新（沿冻结原判）", compact=True),
        "render_full": cont.render_page(page, heading="## 披露更新（沿冻结原判）", compact=False),
        "material_identity": {"earlier": dict(frozen_mi.get("earlier") or {}),
                              "later": mat_new,
                              "earlier_source": "基线实验冻结的旧材料身份（不重新取料）"},
        "run_ref": dict(run_ref),
        "baseline": ref,
    }
    cards = _frozen_cards(baseline, rows_by_id, mat_new, bid)
    return cards, [], update, {"later": snap_id_new,
                               "earlier": {"source": "frozen_experiment", "experiment_id": bid,
                                           "content_sha256": ref["content_sha256"]}}


def _cards_from(snap_old: dict, e_read: dict, mat_old: dict, newer, *,
                entity_id: str, company: str) -> list[dict]:
    """机制卡：**状态来自 compare／readings，判据来自 mechanism_criteria**（同源）。"""
    from financial_analysis import continuation as cont

    ids = cont.proposition_ids()
    rows: dict[str, dict] = {}
    if newer:
        snap_new, l_read, mat_new = newer
        page = cont.compare(snap_old, snap_new, select_ids=ids)
        for row in (page.get("hypotheses") or []):
            rows[str(row.get("judgment_id") or "")] = row
    cards: list[dict] = []
    for jid in ids:
        crit = mechanism_criteria(jid, old_readings=e_read, window_count=1 if rows else 0)
        old_j = next((j for j in (snap_old.get("judgments") or [])
                      if str(j.get("judgment_id") or "") == jid), None)
        row = rows.get(jid)
        card = {
            "judgment_id": jid,
            "proposition": cont.proposition_of(jid),
            "company": company, "code": entity_id,
            "hypothesis": crit["hypothesis"],
            "observation_condition": crit["observation_condition"],
            "weakening_condition_single": crit["weaken_single"],
            "weakening_condition_confirmed": crit["weaken_confirmed"],
            "weakening_condition": crit["weaken_single"],
            "criterion": crit,
            "window": ("同一披露窗口内的同口径对照（期间长度不同：不相减）" if row else
                       "无较新窗口：只登记旧依据与条件"),
            "basis": str((old_j or {}).get("title") or ""),
            "basis_readings": [str(x) for x in ((old_j or {}).get("numbers") or [])[:2]],
            "basis_source": dict(snap_old.get("source") or {}),
            "material_identity": {"earlier": mat_old},
            "status": (str(row.get("status") or "") if row else cont.STATUS_UNKNOWN),
            "status_reason": (str(row.get("status_reason") or "") if row else
                              "没有较新披露：pending，不为凑成功取数"),
            "pending": row is None,
            "decision_basis": (str(row.get("decision_basis") or "") if row else ""),
            "new_readings": [str(x) for x in ((row or {}).get("new_readings") or [])],
            "new_locators": [str(x) for x in ((row or {}).get("new_locators") or [])],
            "next_watch": [str(x) for x in ((row or {}).get("next_watch") or [])][:3],
            "alternatives": [str(x) for x in ((row or {}).get("alternatives") or [])][:2],
            "next_material": (str((row or {}).get("materials") or "") or
                              "下一期同口径披露（补充资料＋附注分项）"),
            "materials": str((row or {}).get("materials") or ""),
        }
        if row and mat_new:
            card["material_identity"]["later"] = mat_new
        cards.append(card)
    # 缓冲结构子判断（现金命题的第二子项，只在有本期三行读数时出）
    buf_id = cont.J_CASH_BUFFER
    buf_row = rows.get(buf_id)
    if buf_row is not None:
        crit = mechanism_criteria(buf_id, old_readings=e_read)
        cards.append({
            "judgment_id": buf_id, "proposition": cont.proposition_of(
                cont._jd.J_CASH_WORKING_CAPITAL),
            "company": company, "code": entity_id,
            "hypothesis": crit["hypothesis"],
            "observation_condition": crit["observation_condition"],
            "weakening_condition_single": crit["weaken_single"],
            "weakening_condition_confirmed": crit["weaken_confirmed"],
            "weakening_condition": crit["weaken_single"],
            "criterion": crit,
            "window": str(buf_row.get("decision_basis") or ""),
            "basis": str(buf_row.get("basis") or ""),
            "basis_readings": [str(x) for x in (buf_row.get("basis_readings") or [])[:2]],
            "basis_source": dict(snap_old.get("source") or {}),
            "material_identity": {"earlier": mat_old, "later": mat_new or {}},
            "status": str(buf_row.get("status") or ""),
            "status_reason": str(buf_row.get("status_reason") or ""),
            "pending": False,
            "decision_basis": str(buf_row.get("decision_basis") or ""),
            "new_readings": [str(x) for x in (buf_row.get("new_readings") or [])],
            "new_locators": [str(x) for x in (buf_row.get("new_locators") or [])],
            "next_watch": [str(x) for x in (buf_row.get("next_watch") or [])][:3],
            "alternatives": [str(x) for x in (buf_row.get("alternatives") or [])][:2],
            "next_material": str(buf_row.get("materials") or ""),
            "materials": str(buf_row.get("materials") or ""),
        })
    return cards


def _based_on(*, old_material_id, new_material_id, registered, roots, snapshot_ids,
              cards, based_on_version, technical_source, sid, rhash, old_cutoff="",
              baseline=None) -> dict:
    """旧机制依据＝**旧材料＋旧快照哈希＋判据哈希**；父研究/父报告只是**技术来源**。

    版本引用只接调用方显式给出的 VersionStore 记录（真实字段），并按字段取，
    不再猜 `body_sha256`/`ok` 得到空哈希／False；含 2025 后续材料的当前 selected
    不会被当成旧截止判断的依据。
    C5-3：显式 `baseline_experiment_id` 时，旧机制依据＝**已冻结实验**（readings/判据/
    来源照抄，`baseline_kind="frozen_experiment"`）；无原版时 `baseline_kind` 记
    **历史重建**（不称"用户当时存下的原假说"）。"""
    crit_list = [c.get("criterion") for c in cards if c.get("criterion")]
    out: dict = {
        "old_material_id": str(old_material_id or ""),
        "old_cutoff": str(old_cutoff or ""),
        "old_cutoff_source": "研究 manifest 的 feature_cutoff（旧机制材料的可用时点界）",
        "old_snapshot_sha256": str((snapshot_ids.get("earlier") or {}).get("snapshot_sha256") or ""),
        "criteria_sha256": _sha_obj(crit_list) if crit_list else "",
        "new_material_id": str(new_material_id or ""),
        "baseline_kind": ("frozen_experiment" if baseline else "historical_rebuild"),
        "role": ("旧机制依据＝已冻结实验（baseline_experiment_id 显式沿用，readings/判据照抄）；"
                 "不是原研究的 result_hash，也不是当前 selected 版本" if baseline else
                 "旧机制依据＝旧材料＋旧快照＋判据哈希；历史重建（用当前代码重放旧材料），"
                 "不是用户当时存下的原假说"),
    }
    if baseline:
        out["baseline_experiment"] = dict(baseline)
    reg_old = next((m for m in (registered or [])
                    if str(m.get("material_id") or "") == str(old_material_id or "")), {})
    if reg_old:
        out["old_text_sha256"] = str(reg_old.get("text_sha256") or "")
        out["old_disclosed_at"] = str(reg_old.get("disclosed_at") or "")
    if based_on_version:
        v = dict(based_on_version)
        acc = v.get("acceptance") if isinstance(v.get("acceptance"), dict) else {}
        out["report_version"] = {
            "version_id": str(v.get("version_id") or v.get("body_sha256") or ""),
            "identity_id": str(v.get("identity_id") or v.get("report_version_id") or ""),
            "acceptance_overall": str(acc.get("overall") or v.get("acceptance_overall") or ""),
            "acceptance_ok": (v.get("acceptance_ok") if v.get("acceptance_ok") is not None
                              else (str(acc.get("overall") or "") == "pass") if acc else None),
            "acceptance_report_sha256": str(acc.get("report_sha256")
                                            or v.get("accepted_body_sha256")
                                            or v.get("acceptance_report_sha256") or ""),
            "rules_version": str(v.get("rules_version") or ""),
            "created_at": v.get("created_at"),
            "adopted": bool(v.get("adopted") if v.get("adopted") is not None
                            else v.get("frozen")),
            "selection_runs": [dict(e) for e in (v.get("selection") or {}).get("runs") or []]
                              if isinstance(v.get("selection"), dict) else [],
            "role": "技术来源（该版本可能已含旧截止之后的材料，不作为旧判断依据）",
        }
    out["technical_source"] = dict(technical_source or {})
    out["technical_source"].setdefault("study_id", sid)
    out["technical_source"].setdefault("result_hash", rhash)
    out["technical_source"].setdefault(
        "note", "父研究/父报告只是技术来源：旧判断的可复核依据是上面登记的旧材料与判据哈希")
    return out


# ------------------------------------------------------------- 市场对照（C5-2）
MARKET_SCHEMA = "weavemind.market_comparison/1"
MARKET_OFFSETS = (0, 4, 19)          # 披露日收盘后第 1/5/20 个交易日（offset 相对锚点）
MARKET_WINDOW_LABEL = {0: "1", 4: "5", 19: "20"}
MARKET_LIMITS = (
    "市场对照只读**披露日收盘之后**的 1/5/20 个交易日；三个时点分开读，不回填："
    "不用年度信息补半年度窗口，也不把市场表现当原因或 alpha 证据",
)

_INDEX_PREFIXES = ("000", "399", "899")


def _code6(x) -> str:
    digits = "".join(ch for ch in str(x or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _pct2(v) -> str:
    """比例（0.0114…）→ 页面百分比两位（None→空串，不把缺数画成 0）。"""
    if v is None:
        return ""
    d = (Decimal(str(v)) * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{'+' if d > 0 else ''}{d}%"


def registered_market_inventory() -> dict:
    """已登记行情集与交易日历清单（元数据取各自 meta.json；**不隐式选任何一个**）。"""
    from adapters import market_history as mh, trading_calendar as tc

    out = {"price_datasets": [], "calendars": []}
    base = mh.data_dir()
    if base.is_dir():
        for d in sorted(base.iterdir()):
            meta = _load_json(d / "meta.json")
            if not meta or str(meta.get("schema") or "") != mh.SCHEMA:
                continue
            out["price_datasets"].append({
                "dataset_id": str(meta.get("dataset_id") or d.name),
                "source": str(meta.get("source") or ""),
                "license": str(meta.get("license") or ""),
                "adj_basis": str(meta.get("adj_basis") or ""),
                "instruments": sorted(str(x) for x in (meta.get("instruments") or [])),
                "date_range": list(meta.get("date_range") or []),
                "imported_at": str(meta.get("imported_at") or ""),
            })
    cbase = tc.data_dir()
    if cbase.is_dir():
        for d in sorted(cbase.iterdir()):
            meta = _load_json(d / "meta.json")
            if not meta or str(meta.get("schema") or "") != tc.SCHEMA:
                continue
            out["calendars"].append({
                "calendar_id": str(meta.get("dataset_id") or d.name),
                "source": str(meta.get("source") or ""),
                "market": str(meta.get("market") or ""),
                "range": list(meta.get("range") or []),
                "imported_at": str(meta.get("imported_at") or ""),
            })
    out["price_datasets"].sort(key=lambda x: x["imported_at"], reverse=True)
    out["calendars"].sort(key=lambda x: x["imported_at"], reverse=True)
    return out


def _benchmark_of(instruments, *, entity_code: str) -> str:
    """基准＝行情集里**非本主体**的指数样标的；推不出唯一解就空（要人显式给，不猜）。"""
    idx = [c for c in sorted(str(x) for x in (instruments or []))
           if c != entity_code and any(c.startswith(p) for p in _INDEX_PREFIXES)]
    return idx[0] if len(idx) == 1 else ""


def market_comparison(*, entity_id, material_id=None, price_dataset_id="", calendar_id="",
                      evaluation_as_of="", benchmark="", roots=None, registered=None) -> dict:
    """**市场对照**（C5-2，可选段）：用户显式选披露事件＋行情集/日历/评价时点 → 三窗口读数。

    - 缺一件就是 `pending` 并给原因：**不隐式取最新、不拿别的数据集顶替**（显式 id 加载，
      载回的 id 与所选不一致同样拒绝）。
    - 数值全部来自现役算子 `quant_research.event_returns.compute`（不写死任何数字）：
      三窗口＝披露日收盘后第 1/5/20 个交易日（offsets 0/4/19），锚点＝披露日（或其前最后
      一个交易日）收盘，相对收益＝几何超额 (1+股票)/(1+基准)−1。
    - 许可随读数走：免费源/个人非商业 → `delivery_eligible=False` 原样冻结，
      对外交付按这一位拦（本模块不另立许可判断）。
    - 本函数不引入任何时钟时间：同一组输入重放得到逐字节同一块（进内容身份）。
    """
    from quant_research import event_calendar as ec, event_returns as er
    from adapters import market_history as mh, trading_calendar as tc

    out: dict = {"schema": MARKET_SCHEMA, "status": "pending",
                 "entity_id": str(entity_id or ""),
                 "selection": {"material_id": str(material_id or ""),
                               "price_dataset_id": str(price_dataset_id or ""),
                               "calendar_id": str(calendar_id or ""),
                               "evaluation_as_of": str(evaluation_as_of or "")},
                 "offsets": list(MARKET_OFFSETS),
                 "window_labels": {str(o): MARKET_WINDOW_LABEL[o] for o in MARKET_OFFSETS},
                 "limits": list(MARKET_LIMITS)}
    mid = out["selection"]["material_id"]
    ds_id = out["selection"]["price_dataset_id"]
    cal_id = out["selection"]["calendar_id"]
    why = ""
    if not mid:
        why = "没有选披露事件（material_id）：市场对照要用户显式选一个披露，不默认用新材料"
    elif not re.fullmatch(r"[0-9a-f]{16}", mid):
        why = "material_id 形态不合法（登记材料 id 为 16 位十六进制）"
    elif not re.fullmatch(r"[0-9a-f]{64}", ds_id):
        why = "没有选行情数据集（price_dataset_id 需 64 位十六进制，从已登记清单里选）"
    elif not re.fullmatch(r"[0-9a-f]{64}", cal_id):
        why = "没有选交易日历（calendar_id 需 64 位十六进制，从已登记清单里选）"
    elif not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", out["selection"]["evaluation_as_of"]):
        why = "没有选评价时点（evaluation_as_of 需 YYYY-MM-DD；三窗口是否已走完按它判）"
    if why:
        out["reason"] = why
        return out
    got = find_registered_material(mid, roots=roots, registered=registered)
    doc = got.get("material") or {}
    if not doc:
        out["reason"] = got.get("error") or "登记材料取不到（受限根里没有／哈希与登记不符）"
        return out
    ev_code = _code6((doc.get("subject") or {}).get("company_code") or entity_id)
    if ev_code != _code6(entity_id):
        out["status"] = "refused"
        out["reason"] = (f"材料主体 {ev_code or '(空)'} 与所选主体 {entity_id} 不是同一公司："
                         "市场对照只对同一主体，不跨公司取数")
        return out
    payload = mh.load(ds_id)
    if (str(payload.get("schema")) != mh.SCHEMA or payload.get("source") == "unavailable"
            or str(payload.get("dataset_id") or "") != ds_id):
        out["status"] = "refused"
        out["reason"] = (f"行情数据集 {ds_id[:12]}… 加载失败（{str(payload.get('reason') or '无原因')}）："
                         "不拿别的数据集顶替（载回的 id 与所选必须一致）")
        return out
    cal = tc.load(cal_id)
    if not tc.ok(cal) or str(cal.get("dataset_id") or "") != cal_id:
        out["status"] = "refused"
        out["reason"] = (f"交易日历 {cal_id[:12]}… 加载失败"
                         f"（{str(((cal.get('unavailable') or [{}])[0] or {}).get('reason') or '无原因')}）："
                         "不拿别的日历顶替（载回的 id 与所选必须一致）")
        return out
    bench = str(benchmark or "").strip() or _benchmark_of(payload.get("instruments") or [],
                                                          entity_code=ev_code)
    if not bench or bench not in [str(x) for x in (payload.get("instruments") or [])]:
        out["status"] = "refused"
        out["instruments"] = sorted(str(x) for x in (payload.get("instruments") or []))
        out["reason"] = ("基准推不出唯一解：行情集里没有唯一的非本主体指数样标的，"
                         "需要显式指定 benchmark（从该数据集的 instruments 里选）")
        return out
    # 事件：登记材料 → 归一化（分类/日级证据）→ 事件表（排除带原因，不静默丢）。
    # 登记材料自带的 `date_precision`/`date_basis` 原样透传：那是它登记时的披露日判据。
    rec = {"title": doc.get("title"), "url": doc.get("url"), "material_id": doc.get("material_id"),
           "raw_sha256": doc.get("raw_sha256"), "disclosed_at": doc.get("disclosed_at"),
           "date_precision": doc.get("date_precision"), "date_basis": doc.get("date_basis"),
           "subject": doc.get("subject") or {}, "code": entity_id,
           "period": next(iter(doc.get("periods") or []), "")}
    ev_table = ec.from_records([rec], codes=[ev_code])
    if not ev_table.get("events"):
        out["reason"] = "材料没进事件表：" + "；".join(
            f"{x.get('material_id')}: {x.get('reason')}" for x in ev_table.get("excluded") or [])
        return out
    events = ec.to_event_list(ev_table)
    reading = er.compute(payload, events, benchmark=bench, offsets=MARKET_OFFSETS,
                         evaluation_as_of=out["selection"]["evaluation_as_of"], calendar=cal)
    rows: list[dict] = []
    for item in reading.get("readings") or []:
        for p in item.get("points") or []:
            off = p.get("offset")
            rows.append({
                "offset": off,
                "window": ("锚点（价格起点）" if off == -1
                           else f"第{MARKET_WINDOW_LABEL.get(off, '?')}个交易日"),
                "date": p.get("date"), "close": p.get("close"),
                "stock_return": p.get("return"),
                "benchmark_return": p.get("benchmark_return"),
                "excess_return": p.get("excess_return"),
                "stock_return_pct": _pct2(p.get("return")),
                "benchmark_return_pct": _pct2(p.get("benchmark_return")),
                "relative_return_pct": _pct2(p.get("excess_return")),
                "state": p.get("state"),
                "note": p.get("note") or p.get("unavailable") or "",
            })
    first = (reading.get("readings") or [{}])[0]
    ok = reading.get("status") == "ok"
    out.update({
        "status": "observed" if ok and first.get("window_state") == "observed" else
                  ("partial" if ok else "pending"),
        "reason": ("" if ok else str(reading.get("reason") or "")),
        "event": (ev_table.get("events") or [{}])[0],
        "material": {"material_id": doc.get("material_id"), "title": doc.get("title"),
                     "disclosed_at": doc.get("disclosed_at"), "text_sha256": doc.get("text_sha256"),
                     "url": doc.get("url")},
        "binding": {"price_dataset_id": ds_id, "calendar_id": cal_id,
                    "input_kind": reading.get("input_kind"),
                    "input_fingerprint": reading.get("input_fingerprint"),
                    "calendar_sessions": len(cal.get("sessions") or []),
                    "calendar_range": [str(x) for x in (cal.get("range") or []) or
                                       list((cal.get("meta") or {}).get("range") or [])]},
        "params": {"benchmark": bench, "offsets": list(MARKET_OFFSETS),
                   "operator": er.OPERATOR, "impl_version": er.IMPL_VERSION,
                   "anchor_rule": "锚点＝披露日（或其前最后一个交易日）收盘价，只用披露日前的信息",
                   "formula": "相对收益＝(1+股票收益)/(1+基准收益)−1（几何超额，收盘对收盘）",
                   "evaluation_as_of": out["selection"]["evaluation_as_of"]},
        "anchor": next((r for r in rows if r["offset"] == -1), None),
        "windows": [r for r in rows if r["offset"] in MARKET_OFFSETS],
        "rows": rows,
        "window_state": first.get("window_state"),
        "suspension_dates": first.get("suspension_dates") or [],
        "suspension_certain": first.get("suspension_certain"),
        "suspension_basis": first.get("suspension_basis"),
        "benchmark_missing": first.get("benchmark_missing") or [],
        "reading_hash": reading.get("reading_hash"),
        "delivery_eligible": bool(reading.get("delivery_eligible")),
        "internal_only": not bool(reading.get("delivery_eligible")),
    })
    if reading.get("delivery_block_reason"):
        out["delivery_block_reason"] = reading["delivery_block_reason"]
    if reading.get("calendar_basis"):
        out["calendar_basis"] = reading["calendar_basis"]
    if reading.get("calendar_proxy_used"):
        out["calendar_proxy_used"] = reading["calendar_proxy_used"]
    return out


# ------------------------------------------------------------------ 一页摘要
def _fmt_yi(v, places: str = "0.001") -> str:
    return _yi_txt(v, places)


def experiment_summary_markdown(art: dict) -> str:
    """**一页**本公司摘要：两格 ＋ 两条更新 ＋ 两项动作（金额 2–3 位、阈值 2 位）。

    元级精确值、公式与全部候选队列**不塞进这一页**：它们在同版 JSON 与原附件里。
    """
    ex = art.get("experiment") or {}
    rr = ex.get("run_ref") or {}
    L = [f"# 研究行动摘要（{art.get('company') or ''} {art.get('entity_id') or ''}）", ""]
    L.append(f"- 研究 `{art.get('study_id')}`｜结果 `{str(art.get('result_hash') or '')[:16]}…`"
             f"｜run `{str(rr.get('run_id') or '')[:16]}…`（{rr.get('source') or ''}）"
             f"｜旧截止 {art.get('feature_cutoff')}")
    for cell in (ex.get("cells") or []):
        name = "本期额（披露 OCF）" if cell.get("view") == "current" else "同比贡献（ΔOCF）"
        if not cell.get("applicable"):
            L.append(f"- **{name}**：不适用——{cell.get('reason')}（不造数）")
            continue
        pts = "／".join(_fmt_yi(p.get("value_yuan")) for p in (cell.get("points") or []))
        L.append(f"- **{name}｜回撤项：{cell.get('label')}**：C={_fmt_yi(cell.get('base_yuan'))} 亿，"
                 f"a={_fmt_yi(cell.get('item_yuan'))} 亿；λ=0／50%／100% → {pts} 亿；"
                 f"λ*={_d(cell.get('zero_lambda_pct')).quantize(Decimal('0.01'))}%（"
                 f"{'域内可达' if cell.get('reachable_within_0_1') else '范围不可达'}）")
        L.append(f"  - 判断：{cell.get('judgment')}")
    acts = art.get("next_actions") or []
    for i, a in enumerate(acts[:2]):
        L.append(f"- **下一观察／动作 {i + 1}**：{a.get('action')}")
    upd = art.get("disclosure_update") or {}
    page = upd.get("page") if isinstance(upd, dict) else None
    if not page:
        L.append(f"- **披露更新**：pending——{upd.get('reason') if isinstance(upd, dict) else '没有更新页'}")
    else:
        v = page.get("version") or {}
        bid = str(v.get("baseline_experiment_id") or "")
        tag = (f"沿冻结原判（原版 `{bid[:12]}…`）" if bid
               else "历史重建（无原版：当前代码重放旧材料，非用户当时存下的原假说）")
        L.append(f"- **披露更新（{tag}）**：旧 {v.get('old_cutoff')} → 新 {v.get('new_cutoff')}"
                 f"（来源：{v.get('new_cutoff_source')}）")
        for row in (page.get("hypotheses") or [])[:2]:
            crit = row.get("criteria") or {}
            L.append(f"  - {row.get('judgment_id')} → **{row.get('status')}**：{row.get('status_reason')}")
            L.append(f"    判据（`{str(crit.get('criterion_sha256') or '')[:12]}…`）："
                     f"{crit.get('observation_condition')}｜单窗口削弱：{crit.get('weaken_single')}")
    mc = art.get("market_comparison") if isinstance(art.get("market_comparison"), dict) else {}
    if mc:
        mst = str(mc.get("status") or "")
        if mst in ("pending", "refused"):
            L.append(f"- **市场对照**：{mst}——{mc.get('reason')}（不隐式取数、不拿别的数据集顶替）")
        else:
            ev = mc.get("event") or {}
            anc = mc.get("anchor") or {}
            mtitle = (mc.get("material") or {}).get("title") or (mc.get("selection") or {}).get("material_id") or ""
            L.append(f"- **市场对照**（{mtitle[:40]}｜事件 {ev.get('date')}｜"
                     f"锚点收盘 {anc.get('close')}）：")
            for w in (mc.get("windows") or []):
                L.append(f"  - {w.get('window')}（{w.get('date')}）：股票 {w.get('stock_return_pct')}%｜"
                         f"基准 {w.get('benchmark_return_pct')}%｜相对 {w.get('relative_return_pct')}%"
                         f"（{w.get('state')}）")
            ops = mc.get("alongside_operations") or []
            sup = "／".join(str(a.get("judgment_id") or "") for a in ops
                            if a.get("status") == "加强")
            wk = "／".join(str(a.get("judgment_id") or "") for a in ops
                           if a.get("status") == "削弱")
            L.append(f"  - 并列经营判断：支持 {sup or '—'}｜削弱 {wk or '—'}"
                     f"｜下一观察：{str(mc.get('next_observation') or '—')[:80]}")
    rs = art.get("research_state") or {}
    L.append(f"- **三态**：机器验收见原报告｜研究状态 `{rs.get('state')}`｜"
             f"人工复核 **未复核**｜对外交付 {rs.get('external_delivery_allowed')}")
    L.append(f"- 精确值与全队列：同版 JSON（`experiments/{art.get('identity', {}).get('experiment_id') or ''}.json`）"
             "与原附件；本页只做版面，不另算一套。")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------ 保存／重开
def _identity_body(art: dict) -> dict:
    """内容身份的**稳定正文**：排除身份块、派生摘要与页面 URL（它们不改变实验内容）。"""
    return {k: v for k, v in art.items()
            if k not in ("identity", "urls", "summary_markdown")}


def experiment_urls(study_id: str, experiment_id: str) -> dict:
    """内部 URL（走既有研究文件接口，不新建服务）。"""
    q = f"?id={study_id}&name="
    return {
        "json": f"/api/research-study/file{q}experiments/{experiment_id}.json",
        "summary": f"/api/research-study/file{q}experiments/{experiment_id}.md",
        "pdf": f"/api/research-study/file{q}experiments/{experiment_id}.pdf",
    }


def experiment_identity(art: dict) -> dict:
    """实验的**内容身份**（不落盘也能算）：同一组输入重放＝同一个 `experiment_id`。

    与 `save_experiment` 用同一份正文定义（`_identity_body`）——页面、脚本与校验
    都用这一个口径，避免"落盘后才有身份、事先算不出"的两套算法。"""
    body = _identity_body(art)
    return {"experiment_id": _sha_obj(body)[:24], "content_sha256": _sha_obj(body),
            "criteria_sha256": str(((art.get("identity") or {}).get("criteria_sha256")) or "")}


def save_experiment(art: dict, *, study_dir) -> dict:
    """把实验**冻结**进研究目录的 `experiments/`（独立内容身份；原 study 只读）。

    内容身份＝产物正文哈希；同一组输入重放得到同一个 `experiment_id`（幂等），
    重开按 id 读回**同一份 JSON**，不重算、不改写研究登记。"""
    study_dir = Path(study_dir)
    body = _identity_body(art)
    eid = _sha_obj(body)[:24]
    ident = dict(art.get("identity") or {})
    ident.update(experiment_identity(art))
    ident.update({"impl": IMPL, "saved_at": None})
    doc = {**art, "identity": ident}
    doc["summary_markdown"] = experiment_summary_markdown(doc)
    out_dir = study_dir / EXPERIMENTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / f"{eid}.json"
    md = out_dir / f"{eid}.md"
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not jp.is_file() or jp.read_text(encoding="utf-8") != text:
        jp.write_text(text, encoding="utf-8", newline="\n")
    if not md.is_file() or md.read_text(encoding="utf-8") != doc["summary_markdown"]:
        md.write_text(doc["summary_markdown"], encoding="utf-8", newline="\n")
    pdf = ""
    try:
        from report_pdf import markdown_to_pdf                      # 可选，缺就不出 PDF
        blob = markdown_to_pdf(doc["summary_markdown"],
                               title=f"研究行动摘要 {doc.get('entity_id') or ''}",
                               workspace=Path(__file__).resolve().parents[1])
        pp = out_dir / f"{eid}.pdf"
        if not pp.is_file() or pp.read_bytes() != blob:   # 幂等：同内容不重写（不动 mtime）
            pp.write_bytes(blob)
        pdf = pp.name
    except Exception:                                               # noqa: BLE001
        pdf = ""
    return {"ok": True, "experiment_id": eid, "content_sha256": _sha_obj(body),
            "files": {"json": jp.name, "summary": md.name, "pdf": pdf},
            "paths": {"json": str(jp), "summary": str(md)},
            "urls": experiment_urls(str(doc.get("study_id") or study_dir.name), eid),
            "reopened_same_bytes": True,
            "note": "同版 JSON：重开按内容身份读回同一份文件（不重算、不改写研究登记）"}


def load_experiment(*, study_dir, experiment_id) -> dict:
    eid = _safe_id(experiment_id, pattern=r"[0-9a-f]{24}", what="experiment_id")
    p = Path(study_dir) / EXPERIMENTS_DIR / f"{eid}.json"
    if not p.is_file():
        return {}
    doc = _load_json(p)
    if not doc:
        return {}
    body = _identity_body(doc)
    doc.setdefault("identity", {})["content_sha256_matches"] = (
        _sha_obj(body) == str(doc["identity"].get("content_sha256") or ""))
    doc["summary_markdown"] = experiment_summary_markdown(doc)
    doc["urls"] = experiment_urls(str(doc.get("study_id") or Path(study_dir).name), eid)
    return doc


def list_experiments(*, study_dir, entity_id=None) -> list[dict]:
    out: list[dict] = []
    d = Path(study_dir) / EXPERIMENTS_DIR
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        doc = _load_json(p)
        if not doc:
            continue
        if entity_id and str(doc.get("entity_id") or "") != str(entity_id):
            continue
        ex = doc.get("experiment") or {}
        out.append({"experiment_id": str((doc.get("identity") or {}).get("experiment_id") or p.stem),
                    "entity_id": str(doc.get("entity_id") or ""),
                    "company": str(doc.get("company") or ""),
                    "run_id": str((ex.get("run_ref") or {}).get("run_id") or ""),
                    "selection": dict(ex.get("selection") or {}),
                    "has_update": bool((doc.get("disclosure_update") or {}).get("page")),
                    "content_sha256": str((doc.get("identity") or {}).get("content_sha256") or "")})
    return out


# ------------------------------------------------------------------- 页面参数
def experiment_options(*, study_dir, entity_id, run_id=None, view="current",
                       workspace_dir=None, roots=None, extra_runs=()) -> dict:
    """页面需要的**可选东西**（全部服务端解析：浏览器不传数字、不传路径）。"""
    study_dir = Path(study_dir)
    man = _load_json(study_dir / "study_manifest.json")
    eid = str(entity_id or "")
    cutoff = str(man.get("feature_cutoff") or "")
    registered = _study_registered_materials(man)
    mat_roots = list(roots or material_roots(study_dir=study_dir))
    runs = list_candidate_runs(study_dir=study_dir, entity_id=eid, workspace_dir=workspace_dir,
                               extra_runs=extra_runs)
    pick = next((r for r in runs if r["run_id"] == str(run_id or "")), None)
    # 逐视角给出**可用性＋候选项＋默认选中项**（页面直接用；不隐式替用户取第一条 cash）
    views: dict = {}
    if pick:
        run_obj = _find_run_object(study_dir=study_dir, entity_id=eid, run_id=pick["run_id"],
                                   workspace_dir=workspace_dir, extra_runs=extra_runs)
        from financial_analysis.operators.cash_reconciliation import reversion_ladder
        for v in ("current", "yoy"):
            lad = reversion_ladder(run_obj, view=v)
            cands = [{"metric": str(c.get("metric")), "label": str(c.get("label")),
                      "group": str(c.get("group")), "item_yuan": str(c.get("item_yuan")),
                      "item_yi": str(c.get("item_yi")),
                      "zero_lambda": str(c.get("zero_lambda")),
                      "reachable_within_0_1": c.get("reachable_within_0_1"),
                      "selected": bool(c.get("selected"))}
                     for c in (lad.get("candidates") or [])]
            views[v] = {
                "view": v,
                "label": {"current": "本期额（披露 OCF）", "yoy": "同比贡献（ΔOCF）"}[v],
                "applicable": bool(lad.get("applicable")),
                "reason": str(lad.get("reason") or ""),
                "metric": str(lad.get("metric") or ""),
                "candidates": cands,
                "selected_metric": next((str(c["metric"]) for c in cands if c.get("selected")), ""),
            }
    mats = scan_registered_materials(entity_id=eid, roots=mat_roots)
    reg_ids = {str(m.get("material_id") or "") for m in registered}
    old_mats = [m for m in mats if m["disclosed_at"] and (not cutoff or m["disclosed_at"] <= cutoff)]
    new_mats = [m for m in mats if m["disclosed_at"] and cutoff and m["disclosed_at"] > cutoff]
    for m in mats:
        m["in_study_registry"] = m["material_id"] in reg_ids
    return {
        "study_id": str(man.get("study_id") or study_dir.name),
        "result_hash": str(man.get("result_hash") or ""),
        "entity_id": eid,
        # 该研究登记的主体（页面只让用户从这里面选，不隐式取第一个）
        "entities": sorted(str(k) for k in (_load_json(study_dir / "financial.json") or {})),
        "feature_cutoff": cutoff,
        "evaluation_as_of": str(man.get("evaluation_as_of") or ""),
        "research_state": {"state": _state_of(man)},
        "human_review": man.get("human_review"),
        "external_delivery": bool((man.get("external_delivery") or {}).get("allowed")),
        # 页面用：每个视角的可用性＋候选项＋默认选中项（`applicable=False` 时给原因）
        "views": views,
        "fractions": [{"value": "0", "label": "0%"}, {"value": "0.5", "label": "50%"},
                      {"value": "1", "label": "100%"}],
        "runs": runs,
        # 兼容别名：按视角键的候选项清单（与 views[v].candidates 同一份）
        "candidates": {v: views[v]["candidates"] for v in views},
        "old_materials": old_mats,
        "new_materials": new_mats,
        "saved_experiments": list_experiments(study_dir=study_dir, entity_id=eid),
        # C5-2：市场对照的**已登记行情清单**（页面只让用户从这里面选，不隐式取最新）；
        # 对照事件就取上面登记材料里的披露日（old/new_materials 各自带 disclosed_at）。
        "market_inventory": registered_market_inventory(),
        "market_selection_note": ("市场对照要显式选：披露事件（登记材料 id）＋行情数据集＋"
                                  "交易日历＋评价时点（YYYY-MM-DD）；缺一件就 pending，"
                                  "不默认用新材料、不隐式取最新。"),
    }
