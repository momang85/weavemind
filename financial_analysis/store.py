# -*- coding: utf-8 -*-
"""分析运行的**落盘与同版核对**：页面 / 图 / 底稿 / 正文 / ZIP 绑到同一次运行。

为什么需要它：分析卡与图在包内共用一个 `run_id` 只是必要条件；只要没有**跨文件**的
同一性检查，"正文引用了某次运行"就仍然是一句自述。这里做三件事：

1. `save_run/load_runs`：把 `ModelRun`（含每个输出的 `output_hash`）原子落到任务工作区
   `analysis_runs.json`，同一个 `run_id` 覆盖更新（不产生第二次运行）；
2. `binding_summary`：给交付清单用的身份块（run_id / dataset_hash / 输出 hash）；
3. `verify_body_binding`：从**正文的分析卡区块**反解出 run 与 output，
   逐条与落盘记录核对——数值不符、运行不存在、运行未通过验证，都必须报出来。
   没有分析卡区块（既有样本）时如实返回"无卡可核"，不假装通过也不无端失败。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .contracts import ModelRun, RunStatus, ValidatedOutput

SCHEMA = "weavemind.analysis_runs/1"
RUNS_NAME = "analysis_runs.json"
ARC_NAME = "analysis/analysis_runs.json"      # 冻结包内的路径
# 可复算输入（K2，2026-09-29 夜验收）："选定模型可离线复算"要求包里有**实际使用的
# 观察快照 + dataset manifest + 分析计划 + 契约/数据集来源**——只有 run 摘要、dataset hash
# 或输出值都不够（hash 不是输入，值不是输入）。
DATASET_NAME = "dataset.json"
PLAN_NAME = "plan.json"
CONTEXT_NAME = "context.json"
ARC_INPUTS = {"dataset": "analysis/dataset.json", "plan": "analysis/plan.json",
              "context": "analysis/context.json"}
SCHEMA_INPUTS = "weavemind.analysis_inputs/1"
CARD_HEADING = "## 分析卡"
_HEADING_RE = re.compile(r"^## ", re.M)
_RUN_TAG_RE = re.compile(r"run=([0-9a-f]{12})")
_OUTPUT_TAG_RE = re.compile(r"output[= ]([0-9a-f]{12}-\d{2}-[A-Za-z_]+)")
_VALUE_RE = re.compile(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元|%|吨)")

_OUT_TUPLE_FIELDS = ("input_periods", "components", "inputs", "assumptions", "limits")


def runs_path(ws) -> Path:
    return Path(ws) / RUNS_NAME


# ── 序列化往返（模型里是 dataclass，落盘要能原样读回来）────────────────────

def output_from_dict(d: dict) -> ValidatedOutput:
    kw = {k: v for k, v in dict(d or {}).items()
          if k in ValidatedOutput.__dataclass_fields__ and k != "output_hash"}
    for f in _OUT_TUPLE_FIELDS:
        if f in kw and kw[f] is not None:
            kw[f] = tuple(kw[f])
    return ValidatedOutput(**kw)


def run_from_dict(d: dict) -> ModelRun:
    kw = {k: v for k, v in dict(d or {}).items()
          if k in ModelRun.__dataclass_fields__ and k != "outputs"}
    kw["outputs"] = tuple(output_from_dict(o) for o in (d.get("outputs") or ()))
    return ModelRun(**kw)


def save_run(ws, run: ModelRun) -> Path:
    """原子落盘（临时文件 + 替换）；同 `run_id` 覆盖更新，不追加第二份。"""
    path = runs_path(ws)
    data = load_file(path)
    runs = [r for r in (data.get("runs") or []) if str(r.get("run_id")) != run.run_id]
    runs.append(run.as_dict())
    data = {"schema": SCHEMA, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "runs": runs}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_file(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001 - 坏文件按空
        return {}
    return data if isinstance(data, dict) else {}


def load_runs(ws) -> list[dict]:
    return list(load_file(runs_path(ws)).get("runs") or [])


def model_runs(ws) -> list[ModelRun]:
    out: list[ModelRun] = []
    for d in load_runs(ws):
        try:
            out.append(run_from_dict(d))
        except Exception:                              # noqa: BLE001 - 坏记录跳过
            continue
    return out


def validated_runs(ws) -> list[ModelRun]:
    return [r for r in model_runs(ws) if r.status == RunStatus.VALIDATED]


def payload_bytes(ws) -> bytes | None:
    """进冻结包的字节（没有运行记录就返回 None：不制造空文件）。"""
    data = load_file(runs_path(ws))
    if not data.get("runs"):
        return None
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def inputs_dir(ws) -> Path:
    return Path(ws) / "analysis"


def save_inputs(ws, *, dataset, plan=None, context: dict | None = None) -> dict:
    """落盘**可复算输入**：`analysis/dataset.json` / `plan.json` / `context.json`。

    为什么单独一份（K2）：交付包里"这次分析用了哪些观察"必须能被**离线重放**——
    `analysis_runs.json` 只有 run 的身份与输出，没有输入；把 dataset 与计划一起留下，
    任何人拿包就能 `recompute_run()` 复算并逐值核对。
    """
    d = inputs_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    try:
        ds_blob = {"schema": SCHEMA_INPUTS, "kind": "dataset",
                   "dataset": dataset.as_dict() if hasattr(dataset, "as_dict") else dataset,
                   "dataset_hash": str(getattr(dataset, "dataset_hash", "") or ""),
                   "manifest": (dataset.manifest.as_dict()
                                if hasattr(dataset, "manifest") else {})}
        (d / DATASET_NAME).write_text(json.dumps(ds_blob, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
        out["dataset"] = str(d / DATASET_NAME)
    except Exception as exc:                           # noqa: BLE001 - 输入落盘失败不静默
        logger.warning("分析输入（dataset）落盘失败：%s", str(exc)[:140])
    if plan is not None:
        try:
            (d / PLAN_NAME).write_text(json.dumps(
                {"schema": SCHEMA_INPUTS, "kind": "plan",
                 "plan": plan.as_dict() if hasattr(plan, "as_dict") else plan},
                ensure_ascii=False, indent=1), encoding="utf-8")
            out["plan"] = str(d / PLAN_NAME)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("分析输入（plan）落盘失败：%s", str(exc)[:140])
    if context is not None:
        try:
            (d / CONTEXT_NAME).write_text(json.dumps(
                {"schema": SCHEMA_INPUTS, "kind": "context", **dict(context or {})},
                ensure_ascii=False, indent=1), encoding="utf-8")
            out["context"] = str(d / CONTEXT_NAME)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("分析输入（context）落盘失败：%s", str(exc)[:140])
    return out


def input_payload_bytes(ws) -> dict[str, bytes]:
    """`{包内路径: 字节}`——进冻结包的可复算输入（没有就不放，不造空文件）。"""
    out: dict[str, bytes] = {}
    d = inputs_dir(ws)
    for key, arc in ARC_INPUTS.items():
        p = d / f"{key}.json"
        try:
            if p.is_file() and p.stat().st_size > 0:
                out[arc] = p.read_bytes()
        except Exception:                              # noqa: BLE001 - 读不到就不进包
            continue
    return out


def load_inputs(ws) -> dict:
    """读回可复算输入：`{dataset_hash, manifest, observations, plan, context}`。"""
    d = inputs_dir(ws)
    out: dict = {}
    for key in ("dataset", "plan", "context"):
        p = d / f"{key}.json"
        try:
            out[key] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except Exception:                              # noqa: BLE001 - 坏文件按空
            out[key] = {}
    return out


# ── 分析选择绑定（L0-b，2026-09-30 复核 U1/U2）──────────────────────────────
# 为什么需要一份**选择**（而不是"取前两条运行"）：用户可以改假设复算出多条运行；
# 哪一条进正文必须是**用户的选择**，不能由装配器按最早/最新替他决定。
# 一条选择记下：模型 → run_id + dataset_hash + 参数 + 规则版本 + 采纳报告身份；
# 正文、图、底稿、清单、ZIP 都消费这一份，历史运行原样保留。
SELECTION_NAME = "selection.json"
ARC_SELECTION = "analysis/selection.json"
SCHEMA_SELECTION = "weavemind.analysis_selection/1"


def selection_path(ws) -> Path:
    return inputs_dir(ws) / SELECTION_NAME


def load_selection(ws) -> dict:
    data = load_file(selection_path(ws))
    if not data:
        return {"schema": SCHEMA_SELECTION, "entries": []}
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    return {"schema": str(data.get("schema") or SCHEMA_SELECTION),
            "updated_at": str(data.get("updated_at") or ""),
            "adopted_identity": str(data.get("adopted_identity") or ""),
            "note": str(data.get("note") or ""),
            "entries": entries}


def selection_payload_bytes(ws) -> bytes | None:
    """进冻结包的字节（没有选择就不放，不造空文件）。"""
    p = selection_path(ws)
    try:
        if p.is_file() and p.stat().st_size > 0:
            return p.read_bytes()
    except Exception:                                  # noqa: BLE001
        return None
    return None


def save_selection(ws, entry: dict, *, adopted_identity: str = "", note: str = "") -> dict:
    """记录/替换某个模型的**所选运行**（同模型第二次选择即替换，旧运行仍留在运行记录里）。

    返回落盘后的选择块（含全部条目），供调用方回显。
    """
    cur = load_selection(ws)
    mid = str((entry or {}).get("model_id") or "")
    entries = [e for e in cur.get("entries") or []
               if str(e.get("model_id") or "") != mid]
    entries.append(dict(entry or {}))
    entries.sort(key=lambda e: str(e.get("model_id") or ""))
    data = {"schema": SCHEMA_SELECTION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "adopted_identity": str(adopted_identity or cur.get("adopted_identity") or ""),
            "note": str(note or cur.get("note") or ""),
            "entries": entries}
    try:
        d = inputs_dir(ws)
        d.mkdir(parents=True, exist_ok=True)
        p = selection_path(ws)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:                           # noqa: BLE001 - 落盘失败不静默
        logger.warning("分析选择落盘失败：%s", str(exc)[:140])
    return data


def selection_status(ws, *, dataset_hash: str = "", rules_version: str = "") -> dict:
    """逐条核对所选运行**还是不是当前可用的**：数据集/规则/参数/输出/验证状态任一不符都报出来。

    R0-b（2026-09-30 下午复核）：此前只核 数据集与规则版本，`model_id`/`params`/`output_ids`
    与运行记录不一致也照样 `ok`——选择记录可以用当前常量或错参冒充一条真实运行。现在**逐项**
    与运行记录比对：模型、参数（含 params_hash）、输出 id 列表、规则版本（含"运行没记规则版本"
    的 `unknown_rules`）。任何缺失、篡改、不匹配都不得算 `ok`。
    """
    cur = load_selection(ws)
    runs = {str(r.get("run_id") or ""): r for r in load_runs(ws)}
    ent_out: list[dict] = []
    for e in cur.get("entries") or []:
        run = runs.get(str(e.get("run_id") or ""))
        state, why = "ok", ""
        if run is None:
            state, why = "run_missing", "所选运行不在运行记录里（可能被清理）：需重新选择/复算"
        elif str(run.get("status")) != RunStatus.VALIDATED:
            state, why = ("not_validated",
                          f"所选运行的状态是 {run.get('status')}：未通过验证的读数不进正文")
        elif dataset_hash and str(e.get("dataset_hash") or "") != str(dataset_hash):
            state, why = "dataset_changed", "所选运行绑定的数据集已变：旧结果过期，需重算"
        elif dataset_hash and str(run.get("dataset_hash") or "") != str(dataset_hash):
            state, why = "binding_mismatch", "所选运行与当前数据集不一致：需重算"
        elif str(e.get("model_id") or "") != str(run.get("model_id") or ""):
            state, why = ("model_mismatch",
                          f"选择记录的模型（{e.get('model_id')}）与运行记录"
                          f"（{run.get('model_id')}）不一致：选择必须指向真实运行")
        elif (str(e.get("params_hash") or "") or dict(e.get("params") or {})) and (
                str(e.get("params_hash") or "") != str(run.get("params_hash") or "")
                or dict(e.get("params") or {}) != dict(run.get("params") or {})):
            state, why = ("params_mismatch",
                          "选择记录的参数与运行记录不一致：不得用当前默认值冒充所选参数")
        elif list(e.get("output_ids") or []) and list(e.get("output_ids") or []) != [
                str(o.get("output_id") or "") for o in (run.get("outputs") or ())]:
            state, why = ("outputs_mismatch",
                          "选择记录的输出 id 与运行记录不一致：选择的不是这一条运行的输出")
        else:
            _run_rules = str(run.get("rules_version") or "")
            _ent_rules = str(e.get("rules_version") or "")
            if not _run_rules:
                state, why = ("unknown_rules",
                              "该运行未记录验证规则版本（历史记录）：可作历史读数，"
                              "但不得当作按当前规则已验证，需重算后再采纳")
            elif not _ent_rules:
                state, why = ("rules_mismatch",
                              "选择记录没有写验证规则版本：不得以当前常量替代缺失证据")
            elif rules_version and _ent_rules != str(rules_version):
                state, why = "rules_changed", "独立验证规则已更新：需按新规则重算后再采纳"
            elif _ent_rules != _run_rules:
                state, why = ("rules_mismatch",
                              f"选择记录的规则版本（{_ent_rules}）与运行记录"
                              f"（{_run_rules}）不一致：不得以当前常量替代缺失证据")
        ent_out.append({**{k: e.get(k) for k in
                           ("model_id", "run_id", "dataset_hash", "params_hash",
                            "rules_version", "selected_at", "question")},
                        "state": state, "why": why,
                        "params": dict(e.get("params") or {}),
                        "output_ids": list(e.get("output_ids") or [])})
    return {"schema": SCHEMA_SELECTION, "entries": ent_out,
            "adopted_identity": str(cur.get("adopted_identity") or ""),
            "ok": bool(ent_out) and all(e["state"] == "ok" for e in ent_out),
            "stale": [e for e in ent_out if e["state"] != "ok"]}


def restore_selection(ws, blob: dict) -> dict:
    """把选择文件**按给定内容**写回（采纳失败时的回滚入口，保留旧有效选择）。

    只用于"以暂存候选试装配、失败回滚"这一条路径：不接受空 blob 造成的静默清空——
    `entries` 为空时也照写（调用方给出的是**上一次的真实状态**）。
    """
    data = {"schema": SCHEMA_SELECTION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "adopted_identity": str((blob or {}).get("adopted_identity") or ""),
            "note": str((blob or {}).get("note") or ""),
            "entries": [dict(e) for e in ((blob or {}).get("entries") or [])]}
    try:
        d = inputs_dir(ws)
        d.mkdir(parents=True, exist_ok=True)
        p = selection_path(ws)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("分析选择回滚失败：%s", str(exc)[:140])
    return data


def dataset_from_inputs(ws):
    """从 `analysis/dataset.json` 还原 `AnalysisDataset`（离线复算的唯一入口）。"""
    from .contracts import AnalysisDataset, DatasetManifest, Observation

    blob = load_inputs(ws).get("dataset") or {}
    ds = blob.get("dataset") or {}
    man = dict(ds.get("manifest") or blob.get("manifest") or {})
    obs = []
    for o in (ds.get("observations") or ()):
        kw = {k: v for k, v in dict(o).items()
              if k in Observation.__dataclass_fields__
              and k not in ("observation_hash", "usable")}
        for f in ("derived_from",):
            if f in kw and kw[f] is not None:
                kw[f] = tuple(kw[f])
        obs.append(Observation(**kw))
    kw = {k: v for k, v in man.items() if k in DatasetManifest.__dataclass_fields__}
    for f in ("periods", "gaps", "conflicts", "available_models"):
        if f in kw and kw[f] is not None:
            kw[f] = tuple(kw[f])
    manifest = DatasetManifest(**kw)
    index = {}
    for o in obs:
        if o.usable:
            index.setdefault((o.metric, o.period, o.caliber), o)
    return AnalysisDataset(manifest=manifest, observations=tuple(obs), index=index)


def _same_substance(a: dict, b: dict) -> bool:
    """两个输出记录**数值/身份**是否相同（忽略分项标签与公式的**措辞**）。

    用途（L0-c 之后的复算比对）：显示文本按参数生成、旧包是写死的默认文案时，
    输出指纹会变，但"输入 → 数值"这条可复算证据没有变。把两者分开记，
    既不放过真正的数值差异，也不把"改了措辞"说成"数对不上"。
    """
    try:
        for k in ("metric", "unit"):
            if str(a.get(k) or "") != str(b.get(k) or ""):
                return False
        # `output_period` 比**期间身份**（年份 token），不比括号里的显示措辞：
        # 旧包写 `2024年（上行）`、新码按参数写 `2024年（使用者情景（…））`——
        # 期间还是 2024 年，措辞变化单列进 `label_changes`。
        ya = set(re.findall(r"(?:19|20)\d{2}", str(a.get("output_period") or "")))
        yb = set(re.findall(r"(?:19|20)\d{2}", str(b.get("output_period") or "")))
        if ya != yb:
            return False
        if abs(float(a.get("value") or 0) - float(b.get("value") or 0)) > 1e-9:
            return False
        if list(a.get("inputs") or []) != list(b.get("inputs") or []):
            return False
        ca = [{kk: c.get(kk) for kk in ("value", "unit")} for c in (a.get("components") or ())]
        cb = [{kk: c.get(kk) for kk in ("value", "unit")} for c in (b.get("components") or ())]
        return ca == cb
    except (TypeError, ValueError):
        return False


def recompute_run(ws, run_id: str) -> dict:
    """**离线复算**：用包内 dataset + 该 run 的参数重跑算子，与落盘输出逐值比对。

    返回 `{ok, run_id, model_id, status, mismatches, outputs}`；只用输入不调模型、
    不发请求——这正是"仅 run 摘要/hash/输出值不能替代可复算输入"的检验方式。
    """
    from . import runner as _runner

    runs = {str(r.get("run_id") or ""): r for r in load_runs(ws)}
    rec = runs.get(str(run_id))
    if rec is None:
        return {"ok": False, "reason": "包内没有这次运行的记录", "run_id": run_id}
    model_id = str(rec.get("model_id") or "")
    try:
        ds = dataset_from_inputs(ws)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "reason": f"包内数据集不可还原：{str(exc)[:140]}",
                "run_id": run_id, "model_id": model_id}
    try:
        if model_id.startswith("ratio:"):
            # 同年比率走注册算子入口（不是 `run()`）：标签来自 run 记录，参数就是
            # 注册表里声明的分子/分母（不在记录里另存一份，避免两套真值）
            label = model_id.split(":", 1)[1]
            hit = next((r for r in _runner.known_ratios() if str(r[0]) == label), None)
            if hit is None:
                return {"ok": False, "reason": f"注册表里没有比率 {label!r}",
                        "run_id": run_id, "model_id": model_id}
            again = _runner.ratio_run(hit[0], hit[1], hit[2], ds)
        else:
            again = _runner.run(model_id, ds, params=dict(rec.get("params") or {}))
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "reason": f"复算失败：{str(exc)[:140]}",
                "run_id": run_id, "model_id": model_id}
    mism: list[str] = []
    # **规则集版本**单独报（L0-a）：旧包里落盘的 run 可能是在旧验证规则下通过验证的，
    # 复算用的是**当前**规则。规则版本不同 → 记 `rules_stale`，要求按新规则重算；
    # 但它**不算值不一致**：值/指纹比对是"输入可复算"这条证据（K2 两包 7/7），
    # 两件事分开记，互不冒充。
    _stored_rules = str(rec.get("rules_version") or "")
    rules_stale = bool(_stored_rules) and _stored_rules != _runner.RULES_VERSION
    if again.status != rec.get("status"):
        mism.append(f"状态不同：复算 {again.status} vs 包内 {rec.get('status')}")
    want = {str(o.get("output_id") or ""): o for o in (rec.get("outputs") or ())}
    got = {str(o.get("output_id") or ""): o for o in
           (again.as_dict().get("outputs") or ())}
    # 指纹差异分两类（L0-c）：**数值/身份**变了 → 不一致（致命）；
    # 只是分项标签/公式的**措辞**变了（例如情景标签改为从参数生成）→ 单列 `label_changes`，
    # 不冒充"数对不上"，也不藏起来。
    label_changes: list[str] = []
    for oid, o in want.items():
        g = got.get(oid)
        if g is None:
            mism.append(f"复算没有 {oid}")
            continue
        if abs(float(g.get("value") or 0) - float(o.get("value") or 0)) > max(
                abs(float(o.get("value") or 0)) * 0.005, 0.01):
            mism.append(f"{oid} 值不同：{g.get('value')} vs {o.get('value')}")
        if str(g.get("output_hash") or "") != str(o.get("output_hash") or ""):
            if _same_substance(g, o):
                label_changes.append(
                    f"{oid}：数值/单位/期间/输入一致，**分项标签/公式措辞或分项契约字段"
                    "（如 component_id 新增）**与落盘记录不同"
                    "（如情景标签改为按参数生成、L0-c/R1-b 后分项带上稳定 id）")
            else:
                mism.append(f"{oid} 输出指纹不同")
    return {"ok": not mism, "run_id": run_id, "model_id": model_id,
            "status": again.status, "dataset_hash": ds.dataset_hash,
            "mismatches": mism,
            "label_changes": label_changes,
            "stored_rules_version": _stored_rules or "未记录",
            "current_rules_version": _runner.RULES_VERSION,
            "rules_stale": rules_stale,
            "rules_note": ("包内 run 通过验证时用的是另一版验证规则：值可复算，"
                           "但不得当作按当前规则已验证，需重算"
                           if rules_stale else ""),
            "outputs": [{"output_id": o.get("output_id"), "value": o.get("value"),
                         "unit": o.get("unit")} for o in want.values()]}


def binding_summary(runs) -> dict:
    """交付清单里的**身份块**：只放可核对的身份与输出 hash，不放整段正文。"""
    items = []
    for r in runs or ():
        d = r if isinstance(r, dict) else r.as_dict()
        items.append({
            "run_id": str(d.get("run_id") or ""),
            "model_id": str(d.get("model_id") or ""),
            "model_version": str(d.get("model_version") or ""),
            "impl_version": str(d.get("impl_version") or ""),
            "dataset_hash": str(d.get("dataset_hash") or ""),
            "params_hash": str(d.get("params_hash") or ""),
            "status": str(d.get("status") or ""),
            "validation_ok": bool((d.get("validation") or {}).get("ok")),
            "outputs": [{"output_id": str(o.get("output_id") or ""),
                         "metric": str(o.get("metric") or ""),
                         "value": o.get("value"), "unit": str(o.get("unit") or ""),
                         "output_hash": str(o.get("output_hash") or "")}
                        for o in (d.get("outputs") or ())],
        })
    return {"schema": SCHEMA, "count": len(items),
            "validated": sum(1 for i in items if i["status"] == RunStatus.VALIDATED),
            "runs": items}


# ── 正文 → 运行 的同一性核对 ──────────────────────────────────────────────

def _card_blocks(body: str) -> list[str]:
    t = str(body or "")
    out: list[str] = []
    for m in re.finditer(re.escape(CARD_HEADING), t):
        rest = t[m.end():]
        nxt = _HEADING_RE.search(rest)
        out.append(rest[:nxt.start()] if nxt else rest)
    return out


def verify_body_binding(ws, body: str) -> dict:
    """正文的分析卡 ↔ 落盘运行：**同一次运行、同一个数**才算绑定。

    返回 `{ok, run_ids, outputs:[{output_id, found, value_ok}], problems, note}`。
    没有分析卡区块时 `ok=True` 且 `note="无分析卡区块"`——既有交付不因此被改判。
    """
    blocks = _card_blocks(body)
    if not blocks:
        return {"ok": True, "run_ids": [], "outputs": [], "problems": [],
                "note": "无分析卡区块（本次交付没有分析卡）"}
    index: dict[str, tuple[dict, dict]] = {}
    for d in load_runs(ws):
        for o in (d.get("outputs") or ()):
            index[str(o.get("output_id") or "")] = (d, o)
    problems: list[str] = []
    run_ids: list[str] = []
    seen: list[dict] = []
    for block in blocks:
        m = _RUN_TAG_RE.search(block)
        run_tag = m.group(1) if m else ""
        if run_tag:
            run_ids.append(run_tag)
        tagged = _OUTPUT_TAG_RE.findall(block)
        if not tagged:
            problems.append("分析卡里没有任何 output 标识：无法与运行记录核对")
        for oid in tagged:
            hit = index.get(oid)
            if not hit:
                problems.append(f"正文引用 output {oid}，但工作区没有这次运行的记录")
                seen.append({"output_id": oid, "found": False, "value_ok": False})
                continue
            run_d, out_d = hit
            if run_tag and not str(run_d.get("run_id") or "").startswith(run_tag):
                problems.append(f"output {oid} 属于运行 {str(run_d.get('run_id'))[:12]}，"
                                f"与卡上写的 run={run_tag} 不是同一次")
            if str(run_d.get("status") or "") != RunStatus.VALIDATED:
                problems.append(f"output {oid} 所属运行状态是 {run_d.get('status')}："
                                f"未通过验证的读数不得进正文")
            # 数值核对：同一个 output 在正文里出现时必须是同一个数（同单位）
            want = out_d.get("value")
            unit = str(out_d.get("unit") or "")
            found_ok = False
            for vm in _VALUE_RE.finditer(block):
                try:
                    got = float(vm.group(1).replace(",", ""))
                except ValueError:
                    continue
                if abs(got - float(want)) <= max(abs(float(want)) * 0.005, 0.01) \
                        and (not unit or vm.group(2) == unit):
                    found_ok = True
                    break
            if not found_ok:
                problems.append(f"output {oid} 的值（{want}{unit}）没有在分析卡里以同一"
                                f"数值出现：正文与运行不是同一版")
            seen.append({"output_id": oid, "found": True, "value_ok": found_ok,
                         "run_id": str(run_d.get("run_id") or "")})
    return {"ok": not problems, "run_ids": sorted(set(run_ids)), "outputs": seen,
            "problems": problems,
            "note": "" if problems else "正文分析卡与落盘运行一致（同 run、同数值）"}


def render_card_block(run: ModelRun) -> str:
    """把已验证运行渲染成**可核对的**分析卡区块（严格格式，便于同一性核对）。

    格式是契约的一部分：`run=<12位>` + 每个读数一行并带 `output <output_id>`。
    """
    from .report_adapter import analysis_card

    outs = list(run.outputs or ())
    lines = [CARD_HEADING, ""]
    _kind = ""
    try:
        from .report_adapter import card_traits
        _kind = str(card_traits(run.model_id).get("kind") or "")
    except Exception:                                  # noqa: BLE001 - 性质取不到就留空
        _kind = ""
    lines.append(f"> 运行 run={run.run_id[:12]}（模型 {run.model_id} {run.model_version}；"
                 + (f"性质 {_kind}；" if _kind else "")
                 + f"数据集 {run.dataset_hash[:12]}；参数 {run.params_hash}；"
                 f"验证 {'通过' if (run.validation or {}).get('ok') else '未通过'}）")
    for o in outs:
        card = analysis_card(run, o.output_id)
        lines.append(f"- **{card['title']}**：{card['judgement']}　output {o.output_id}")
        for d in card.get("drivers") or ():
            if d.get("label") and d.get("label") != o.label:
                lines.append(f"  - 贡献项：{d['label']} "
                             f"{d['value']:+,.2f}{d.get('unit') or o.unit}"
                             f"（= {d.get('formula') or '—'}）")
        if o.metric == (outs[0].metric if outs else "") and card.get("unexplained", {}).get("value") is not None:
            lines.append(f"  - 未解释段：{card['unexplained']['value']:+,.2f}{o.unit}"
                         f"（{card['unexplained']['note']}）")
    if outs:
        card0 = analysis_card(run, outs[0].output_id)
        # U1（2026-10-01）：经营驱动卡另外给**三段式**正文（三项最大贡献／替代解释／待核查），
        # 否则读者只看到"每个输出一行"，仍不知道钱从哪来、还有没有别的解释。
        try:
            from .report_adapter import render_operating_drivers_block
            lines.extend(render_operating_drivers_block(run))
        except Exception:                                  # noqa: BLE001 - 渲染失败不加这段
            pass
        lines.append(f"- 下一项验证动作：{card0['next_action']}")
    lines.append("")
    return "\n".join(lines)
