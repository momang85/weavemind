# -*- coding: utf-8 -*-
"""C5-4 探针：**显式选版实际内部导出**＋**许可门覆盖 market 读数**。

覆盖（全部离线；不重跑旧套件；不付费；旧包不动）：
  A 许可门零影响契约：无 quant／无 market 段的成员 → 投影原样返回、不产结论
  B market 读数走同一许可门：带 `market_comparison.delivery_eligible=False` 的
    `analysis/c3_experiment_*.json` → 移出本包＋限制说明；其余成员原样保留
  C fail closed：market 段被删掉 `delivery_eligible` 一位 → 同样受限（不默认放行）
  D **显式选 C4 冻结实验（无 market 段）实际内部导出**：包内实验 JSON 与
    `experiments/245bcd….json` **逐字节相同**，MD/manifest 同源（C4-3 修复真实落地）
  E **显式选 C5 新实验（带受限 market 段）实际内部导出**：受限 JSON 成员按许可门移出
    包外，限制说明与 manifest 的 withheld 哈希记进包内；工作区 staging 仍逐字节同版
  F 旧包不动；市场读数不绕门泄漏（正文/附件/上下文）

用法：`python -X utf8 docs/evidence/20261012-c5-review/c5_export_probe.py`
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import delivery_pipeline as DP                       # noqa: E402
import research_report_adapter as A                  # noqa: E402
from report_version import body_hash                 # noqa: E402

OUT = Path(__file__).resolve().parent
STUDY = ROOT / "tmp" / "research-studies" / "0af20919e6245b88"
WS = Path(os.environ["TEMP"]) \
    / "agent_workspace" / "tasks" / "projects" / "default" \
    / "study-report-0af20919e6245b88-002304.SZ"
NEW_EXP = "80693f9870e8337f394fcf49"        # C5 冻结基线更新实验（带受限 market 段）
OLD_EXP = "245bcd5e56ef7c5eafaf11e2"        # C4 冻结实验（无 market 段）
ENTITY = "002304.SZ"
STUDY_ID = "0af20919e6245b888974ee17321f327f99580ab745687e80aca6dae6921e7f4f"
RESULT_HASH = "0f21c7a36f0e28fd24d2b79634036ec736ca7cba18f58040b7621a27242b1cf3"
NOTE_ARC = "quant/EXTERNAL_DELIVERY_RESTRICTION.md"
C3_ARC = "analysis/c3_experiment_002304.SZ.json"
C3_MD = "analysis/c3_experiment_002304.SZ.md"

results: list[dict] = []
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
    if not ok:
        fails.append(f"{name}: {detail[:200]}")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _zips(ws: Path) -> dict:
    return {p.name: _sha(p.read_bytes()) for p in sorted(ws.glob("deliverables*.zip"))}


def _members(zip_path: Path) -> dict:
    out: dict = {}
    if zip_path.is_file():
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if not n.endswith("/"):
                    out[n] = z.read(n)
    return out


def _manifest(members: dict) -> dict:
    try:
        return json.loads(members.get("PACKAGE_MANIFEST.json", b"{}").decode("utf-8"))
    except Exception:                                # noqa: BLE001
        return {}


def _leak(text: str) -> list[str]:
    """受限**市场读数**的特征值（块名/窗口标签/窗口数值）是否出现在文本里。

    注：行情**数据集 id** 自 C2b 起就是正文的出处标识（数据来源，不是读数），
    不属于本门要挡的受限读数，故不列入。"""
    marks = ["market_comparison", "第1个交易日", "第5个交易日", "第20个交易日",
             "0.005663", "-0.024119", "-0.083054"]
    return [m for m in marks if m in text]


def _export(experiment_id: str) -> dict:
    return A.generate_report(study_id=STUDY_ID, entity_id=ENTITY, result_hash=RESULT_HASH,
                             feature_cutoff="2025-04-30", evaluation_as_of="2026-10-09",
                             experiment_id=experiment_id)


def _acceptance_for(body_text: str) -> tuple[bool, str]:
    """工作区版本账本：包内正文 → 交付记录 → 对应版本的机器验收（self-contained）。"""
    try:
        rv = json.loads((WS / "report_versions.json").read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return False, "report_versions.json unreadable"
    bh = body_hash(body_text)
    hit = next((x for x in (rv.get("deliveries") or [])
                if str(x.get("delivered_sha256") or "") == bh and x.get("ok")), None)
    if not hit:
        return False, f"delivery(bh={bh[:12]}) not found"
    vid = str(hit.get("accepted_body_sha256") or "")
    v = next((v for v in (rv.get("versions") or {}).values()
              if str(v.get("version_id") or "") == vid), None)
    if not v:
        return False, f"version({vid[:12]}) not found"
    ov = str((v.get("acceptance") or {}).get("overall") or "")
    return ov == "pass", f"overall={ov} aligned={hit.get('aligned')}"


def _find_zip(pred) -> tuple[Path | None, list[str]]:
    """已存在的同身份真实包（按内容判）：重跑不堆包，只复核已出的那份真实内部导出。"""
    hits = [p for p in sorted(WS.glob("deliverables*.zip")) if pred(_members(p))]
    return (hits[-1] if hits else None), [p.name for p in hits]


# ---------------------------------------------------------------- A/B/C 许可门
new_exp_bytes = (STUDY / "experiments" / f"{NEW_EXP}.json").read_bytes()
old_exp_bytes = (STUDY / "experiments" / f"{OLD_EXP}.json").read_bytes()
mc_blob = json.loads(new_exp_bytes.decode("utf-8"))
check("B0 选中的新实验确实带 market 段且免费源不可对外",
      isinstance(mc_blob.get("market_comparison"), dict)
      and mc_blob["market_comparison"].get("delivery_eligible") is False,
      f"delivery_eligible={(mc_blob.get('market_comparison') or {}).get('delivery_eligible')}")

plain = {"report/README.md": "# 正文\n".encode("utf-8"),
         "analysis/q1_followup_002304.SZ.md": "## 续页\n".encode("utf-8")}
proj, info = DP.external_delivery_projection(dict(plain))
check("A1 无 quant／无 market 段 → 原样返回、不产结论",
      proj == plain and info is None,
      f"info={info}")

mixed = dict(plain)
mixed[C3_ARC] = new_exp_bytes
proj, info = DP.external_delivery_projection(dict(mixed))
check("B1 market 读数受限 → 该成员移出包外", C3_ARC not in proj, f"members={sorted(proj)}")
check("B2 其余成员原样保留（逐字节）",
      proj.get("report/README.md") == plain["report/README.md"]
      and proj.get("analysis/q1_followup_002304.SZ.md") == plain["analysis/q1_followup_002304.SZ.md"],
      f"{sorted(proj)}")
check("B3 限制说明随包并列出被移出成员与 sha256",
      NOTE_ARC in proj and C3_ARC in proj[NOTE_ARC].decode("utf-8", "ignore")
      and _sha(new_exp_bytes)[:16] in proj[NOTE_ARC].decode("utf-8", "ignore"),
      f"note arcs={[str(k) for k in proj if 'RESTRICTION' in str(k)]}")
check("B4 判据＝market_comparison 自带的 delivery_eligible（不改许可口径）",
      bool(info) and "delivery_eligible" in str(info.get("basis") or ""),
      str(info.get("basis") or ""))

tampered = dict(mixed)
tb = json.loads(new_exp_bytes.decode("utf-8"))
tb["market_comparison"].pop("delivery_eligible", None)
tampered[C3_ARC] = json.dumps(tb, ensure_ascii=False).encode("utf-8")
proj, info = DP.external_delivery_projection(dict(tampered))
check("C1 market 段缺 delivery_eligible ⇒ fail closed（不默认放行）",
      C3_ARC not in proj, f"members={sorted(proj)}")

nomc = dict(plain)
old_nc = json.loads(new_exp_bytes.decode("utf-8"))
old_nc.pop("market_comparison", None)
nomc["analysis/other_summary.json"] = json.dumps(old_nc, ensure_ascii=False).encode("utf-8")
proj, info = DP.external_delivery_projection(dict(nomc))
check("C2 无 market 段的 analysis 成员零影响",
      proj.get("analysis/other_summary.json") == nomc["analysis/other_summary.json"] and info is None,
      f"info={info}")

# ---------------------------------------------------------------- D/E 两次真实导出
before = _zips(WS)
# 先看有没有**同身份**的真实包（重跑不堆包）；没有才现出一版（首次运行＝真出包）
zip_a, hits_a = _find_zip(
    lambda m: C3_ARC in m and _sha(m[C3_ARC]) == _sha(old_exp_bytes) and C3_MD in m
    and not (m.get("PACKAGE_MANIFEST.json") and _manifest(m).get("external_delivery")))
zip_b, hits_b = _find_zip(
    lambda m: C3_ARC not in m and NOTE_ARC in m
    and ((_manifest(m).get("external_delivery") or {}).get("withheld") or {}
         ).get(C3_ARC) == _sha(new_exp_bytes))
fresh_a = fresh_b = False
if zip_a is None:
    res_a = _export(OLD_EXP)                   # C4 冻结实验（无 market 段）
    zip_a = WS / str(res_a.get("zip_name") or "")
    fresh_a = True
if zip_b is None:
    res_b = _export(NEW_EXP)                   # C5 新实验（带受限 market 段）
    zip_b = WS / str(res_b.get("zip_name") or "")
    fresh_b = True
after = _zips(WS)

mem_a = _members(zip_a)
mem_b = _members(zip_b)
man_a = _manifest(mem_a)
man_b = _manifest(mem_b)
body_a = mem_a.get("reports/report.md", b"").decode("utf-8", "ignore")
md_a = mem_a.get(C3_MD, b"").decode("utf-8", "ignore")

# —— D：显式选 C4 冻结实验（无 market 段）→ 完整 JSON 本当随包 ——
check("D1 显式选 C4 冻结实验：包真实存在且机器验收 pass（首次由本探针现出包）",
      zip_a.is_file() and (not fresh_a or (bool(res_a.get("ok"))
                                           and str(res_a.get("acceptance")) == "pass")),
      f"zip={zip_a.name} fresh={fresh_a} hits={len(hits_a)}")
check("D2 包内完整实验 JSON 与 experiments/245bcd….json **逐字节相同**",
      _sha(mem_a.get(C3_ARC, b"")) == _sha(old_exp_bytes) and C3_ARC in mem_a,
      f"{_sha(mem_a.get(C3_ARC, b''))[:16]} vs {_sha(old_exp_bytes)[:16]}")
check("D3 manifest 同源：PACKAGE_MANIFEST.files 记下同一 sha256",
      (man_a.get("files") or {}).get(C3_ARC) == _sha(old_exp_bytes),
      f"{(man_a.get('files') or {}).get(C3_ARC, '')[:16]}")
check("D4 正文同源：包内 report.md 字节哈希＝manifest 的 delivered_md_sha256",
      man_a.get("delivered_md") == "reports/report.md"
      and man_a.get("delivered_md_sha256") == _sha(mem_a.get("reports/report.md", b"")),
      f"{str(man_a.get('delivered_md_sha256'))[:16]}")
check("D5 附件 md 随包（同一实验渲染稿）且无许可门限制说明（无 market 段不触发）",
      C3_MD in mem_a and NOTE_ARC not in mem_a and not (man_a.get("external_delivery")),
      f"md={C3_MD in mem_a} note={NOTE_ARC in mem_a}")
check("D6 三态如实：未采纳/未复核/不对外",
      (not fresh_a or res_a.get("human_review") == "unreviewed")
      and not (man_a.get("external_delivery") or {}).get("allowed", False),
      f"fresh={fresh_a} ext={man_a.get('external_delivery')}")
try:
    _acc_a, _acc_a_why = _acceptance_for(body_a)
except Exception as exc:                         # noqa: BLE001
    _acc_a, _acc_a_why = False, str(exc)[:80]
check("D7 版本账本：包内正文 → 交付记录 → 该版机器验收 overall=pass", _acc_a, _acc_a_why)

# —— E：显式选 C5 新实验（带受限 market 段）→ 受限成员按许可门移出包外 ——
body_b = mem_b.get("reports/report.md", b"").decode("utf-8", "ignore")
md_b = mem_b.get(C3_MD, b"").decode("utf-8", "ignore")
ctx_b_text = mem_b.get("analysis/context.json", b"").decode("utf-8", "ignore")
ctx_b: dict = {}
try:
    ctx_b = json.loads(ctx_b_text)
except Exception:                                # noqa: BLE001
    ctx_b = {}
check("E1 显式选 C5 新实验：包真实存在且机器验收 pass（首次由本探针现出包）",
      zip_b.is_file() and (not fresh_b or (bool(res_b.get("ok"))
                                           and str(res_b.get("acceptance")) == "pass")),
      f"zip={zip_b.name} fresh={fresh_b} hits={len(hits_b)}")
check("E2 受限实验 JSON 成员被许可门拦下（不在包内）",
      C3_ARC not in mem_b, f"{sorted(k for k in mem_b if k.startswith('analysis/'))[:8]}")
check("E3 限制说明在包内并记下被移出成员与 sha256",
      NOTE_ARC in mem_b and C3_ARC in mem_b[NOTE_ARC].decode("utf-8", "ignore")
      and _sha(new_exp_bytes)[:16] in mem_b[NOTE_ARC].decode("utf-8", "ignore"),
      f"{NOTE_ARC in mem_b}")
ext_b = man_b.get("external_delivery") or {}
check("E4 manifest 记录 withheld 该成员 sha256（受限逐条可核，不静默）",
      ext_b.get("allowed") is False
      and (ext_b.get("withheld") or {}).get(C3_ARC) == _sha(new_exp_bytes),
      f"allowed={ext_b.get('allowed')} withheld={list((ext_b.get('withheld') or {}).keys())[:3]}")
staged = WS / "analysis" / C3_ARC.split("/", 1)[1]
check("E5 工作区 staging 的实验 JSON 与 experiments/80693f….json **逐字节相同**（内部保存成立）",
      staged.is_file() and staged.read_bytes() == new_exp_bytes,
      f"{_sha(staged.read_bytes() if staged.is_file() else b'')[:16]} vs {_sha(new_exp_bytes)[:16]}")
check("E6 context.json/公司视图同一实验 id（一次选定贯穿正文/附件/视图）",
      str(ctx_b.get("c3_experiment_id") or "") == NEW_EXP
      and "显式" in str(ctx_b.get("c3_experiment_source") or "")
      and str((((ctx_b.get("company_view") or {}).get("realization") or {})
               .get("c3_experiment") or {}).get("experiment_id") or "") == NEW_EXP,
      f"ctx={ctx_b.get('c3_experiment_id')} src={str(ctx_b.get('c3_experiment_source'))[:24]}")
check("E7 附件 md 与同一实验同源（同 run＋新截止 2026-04-28／历史重建=False）",
      "370dba92a40e36a3" in md_b and "2026-04-28" in md_b and "历史重建=False" in md_b,
      f"{C3_MD in mem_b}: {md_b[:80]}")
# 注：正文/上下文里的价格窗表是 **C2b 既有段**（已采纳基线，带自己的"许可边界"声明），
# C5 不重写已验收正文；这里验的是 C5 不另开第二条未设门的路（无 market 块无泄漏）。
check("E8 C5 不另开未设门的路：context/附件 md 不含 market 块或窗口标签",
      "market_comparison" not in ctx_b_text and not _leak(md_b)
      and "第1个交易日" not in ctx_b_text,
      f"ctx has mc={'market_comparison' in ctx_b_text} md leaks={_leak(md_b)}")
check("E9 C2b 既有价格窗表带许可边界声明（如实标注，非 C5 新增旁路）",
      "许可边界" in body_b and "仅内部试验" in body_b,
      f"许可边界={'许可边界' in body_b}")
check("E10 正文同源：包内 report.md 字节哈希＝manifest 的 delivered_md_sha256",
      man_b.get("delivered_md") == "reports/report.md"
      and man_b.get("delivered_md_sha256") == _sha(mem_b.get("reports/report.md", b"")),
      f"{str(man_b.get('delivered_md_sha256'))[:16]}")
try:
    _acc_b, _acc_b_why = _acceptance_for(body_b)
except Exception as exc:                         # noqa: BLE001
    _acc_b, _acc_b_why = False, str(exc)[:80]
check("E11 版本账本：包内正文 → 交付记录 → 该版机器验收 overall=pass", _acc_b, _acc_b_why)

# ---------------------------------------------------------------- F 旧包与零影响
check("F1 既有包逐个不动（sha256 前后一致；同版重跑诚实复用）",
      all(after.get(k) == before[k] for k in before),
      f"before={len(before)} after={len(after)} new={sorted(set(after) - set(before))}")
proj, info = DP.external_delivery_projection({C3_ARC: old_exp_bytes})
check("F2 无 market 段的实验字节过门零影响（C4 冻结原版不被拦）",
      C3_ARC in proj and info is None, f"info={info}")

# ---------------------------------------------------------------- 汇总
out = {"scope": "C5-4 探针：显式选版实际内部导出（两次）＋许可门覆盖 market 读数",
       "export_c4_frozen": {"experiment_id": OLD_EXP, "zip": zip_a.name,
                            "zip_exists": zip_a.is_file(), "produced_now": fresh_a,
                            "matched_existing": hits_a,
                            "contains_full_json": C3_ARC in mem_a},
       "export_c5_update": {"experiment_id": NEW_EXP, "zip": zip_b.name,
                            "zip_exists": zip_b.is_file(), "produced_now": fresh_b,
                            "matched_existing": hits_b,
                            "withheld": C3_ARC not in mem_b and NOTE_ARC in mem_b},
       "checks": results, "total": len(results),
       "passed": sum(1 for r in results if r["ok"]), "fails": fails}
(OUT / "c5_export_results.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{out['passed']}/{out['total']} PASS")
print(f"  C4 冻结实验包：{zip_a.name}（完整 JSON 在包内={C3_ARC in mem_a}，现出包={fresh_a}）")
print(f"  C5 新实验包：{zip_b.name}（受限 JSON 按门移出={C3_ARC not in mem_b}，现出包={fresh_b}）")
for f in fails:
    print("FAIL:", f)
sys.exit(0 if not fails else 1)
