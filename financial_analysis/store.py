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
    lines.append(f"> 运行 run={run.run_id[:12]}（模型 {run.model_id} {run.model_version}；"
                 f"数据集 {run.dataset_hash[:12]}；参数 {run.params_hash}；"
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
        lines.append(f"- 下一项验证动作：{card0['next_action']}")
    lines.append("")
    return "\n".join(lines)
