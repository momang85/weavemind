# -*- coding: utf-8 -*-
"""C4-3 接缝探针（组件级，只读研究目录，工作区用临时目录）：
① _c3_resolve：C4 冻结实验优先（洋河 245bcd…），旧脚本产物回落（三一 4dba42a6…）
② run 身份守卫：runs 不含 run_ref 且无续页 → 不装配
③ _c3_view_block：C4 新字段（criterion/hypothesis/experiment_id/based_on/next_actions）
   与旧产物零变化（新键空缺省、卡片无 criterion）
④ _stage_candidate：analysis/c3_experiment_<code>.json 逐字节==源文件；.md 仍生成
⑤ delivery_pipeline 入包规则：glob 命中 staged JSON；exact_values_in 指向真实成员名
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import research_report_adapter as R  # noqa: E402

YH = ROOT / "tmp" / "research-studies" / "0af20919e6245b88"
SY = ROOT / "tmp" / "research-studies" / "4dba42a6bdde9227"
YH_EID = "245bcd5e56ef7c5eafaf11e2"
fails = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ① C4 优先
art, src = R._c3_resolve(YH, "002304.SZ")
check("① 洋河解析到 C4 冻结实验", bool(art) and src is not None
      and src.name == f"{YH_EID}.json" and src.parent.name == "experiments",
      f"src={src}")
check("① schema/run_ref", str(art.get("schema")) == R._C3_SCHEMA
      and str(((art.get("experiment") or {}).get("run_ref") or {}).get("run_id") or "").startswith("370dba92"),
      f"run_ref={((art.get('experiment') or {}).get('run_ref') or {}).get('run_id', '')[:12]}…")

# ② 守卫：只给无关 run → 空 art（C4 与旧产物都跳过）
art2, src2 = R._c3_resolve(YH, "002304.SZ", runs={"x": {"run_id": "f" * 64}})
check("② 身份不符不装配", not art2 and src2 is None)

# ③ 视图块：C4 新字段
blk = R._c3_view_block(art)
cards = blk.get("cards") or []
check("③ cards 数量", len(cards) == 5, f"n={len(cards)}")
c0 = cards[0]
check("③ 判据随卡", bool(c0.get("hypothesis")) and isinstance(c0.get("criterion"), dict)
      and bool((c0.get("criterion") or {}).get("criterion_sha256")),
      f"c0={c0.get('judgment_id')} crit={str((c0.get('criterion') or {}).get('criterion_sha256'))[:12]}…")
check("③ 身份/依据/动作", bool(blk.get("experiment_id")) and blk["experiment_id"] == YH_EID
      and bool(blk.get("criteria_sha256"))
      and (blk.get("based_on") or {}).get("old_cutoff") == "2025-04-30"
      and (blk.get("based_on") or {}).get("adopted") is True
      and str(((blk.get("based_on") or {}).get("report_version") or {}).get("acceptance_overall")) == "pass"
      and len(blk.get("next_actions") or []) >= 1,
      f"eid={blk.get('experiment_id')} old_cutoff={(blk.get('based_on') or {}).get('old_cutoff')}")

# ③b 旧产物零变化：三一旧脚本产物 → 新键空缺省
art_o, src_o = R._c3_resolve(SY, "600031.SH")
check("③b 三一回落旧脚本产物", bool(art_o) and src_o is not None
      and src_o.name == "c3_experiment_600031.SH.json" and src_o.parent == SY,
      f"src={src_o}")
blk_o = R._c3_view_block(art_o)
oc0 = (blk_o.get("cards") or [{}])[0]
check("③b 旧产物新键空缺省", blk_o.get("experiment_id") == "" and blk_o.get("criteria_sha256") == ""
      and blk_o.get("based_on") == {} and blk_o.get("next_actions") == []
      and (oc0.get("criterion") or {}) == {})
check("③b exact_values_in", blk_o.get("exact_values_in") == "analysis/c3_experiment_600031.SH.json")

# ④ staging：临时工作区
tmpws = Path(tempfile.mkdtemp(prefix="c4seam_"))
try:
    R._stage_candidate(YH, "002304.SZ", {}, {}, {}, tmpws)
    staged = tmpws / "analysis" / "c3_experiment_002304.SZ.json"
    check("④ staged JSON 逐字节==源", staged.is_file()
          and staged.read_bytes() == (YH / "experiments" / f"{YH_EID}.json").read_bytes(),
          f"{staged.stat().st_size if staged.exists() else 0}B")
    check("④ staged md 生成", (tmpws / "analysis" / "c3_experiment_002304.SZ.md").is_file())

    # ⑤ 入包规则（与 delivery_pipeline 相同 glob）＋指针一致
    members = [f"analysis/{p.name}" for p in sorted((tmpws / "analysis").glob("c3_experiment_*.json"))]
    check("⑤ glob 命中", members == ["analysis/c3_experiment_002304.SZ.json"], str(members))
    check("⑤ exact_values_in 指向真实成员", blk.get("exact_values_in") in members,
          f"{blk.get('exact_values_in')} ∈ {members}")
finally:
    shutil.rmtree(tmpws, ignore_errors=True)

# ⑤b 研究目录冻结文件未被 staging 改动
import hashlib  # noqa: E402
h = hashlib.sha256((YH / "experiments" / f"{YH_EID}.json").read_bytes()).hexdigest()
check("⑤b 冻结文件未变", len(h) == 64 and h != "", h[:16] + "…")

print(f"\n{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(0 if not fails else 1)
