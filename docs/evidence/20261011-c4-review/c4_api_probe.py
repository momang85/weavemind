# -*- coding: utf-8 -*-
"""C4 窄 POST 的**组件级**探针（不起服务）：正例 options/evaluate/save/open ＋ 反例边界。

说明：这是组件级调用（伪 handler 捕获 `_json`），**不冒充**浏览器 GUI 复核；
GUI 需要管理员登录，恢复条件登记在 README。"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import web_ui                                                     # noqa: E402

YH_SID = "0af20919e6245b88"
YH_HASH = "0f21c7a36f0e28fd24d2b79634036ec736ca7cba18f58040b7621a27242b1cf3"
YH_RUN = "370dba92a40e36a3acdfee14644085602e088e625db536b8111f2da9ac2329b4"


class Fake:
    path = "/api/research-study/experiment"

    def __init__(self):
        self.payload = None
        self.code = 200

    def _client_ip(self):
        return "127.0.0.1"

    def _json(self, obj, code=200, extra_headers=None):
        self.payload = obj
        self.code = code
        return obj


def call(body, expect=200, what=""):
    f = Fake()
    web_ui._post_research_study_experiment(f, f.path, body, {"user": "c4-probe"})
    tag = "ok " if f.code == expect else "!! "
    print(f"{tag}{what or body.get('action')}: code={f.code}")
    if f.code != expect:
        print("    payload:", json.dumps(f.payload, ensure_ascii=False)[:300])
    return f


def main() -> int:
    base = {"study_id": YH_SID, "result_hash": YH_HASH, "entity_id": "002304.SZ"}
    f = call({**base, "action": "options", "run_id": YH_RUN, "view": "current"}, 200, "options")
    o = f.payload
    print("   runs:", [(r["run_id"][:12], r["source"], {v: x["applicable"] for v, x in r["views"].items()})
                       for r in o["runs"]])
    print("   旧材料:", [m["material_id"] + "@" + m["disclosed_at"] for m in o["old_materials"]])
    print("   新材料:", [m["material_id"] + "@" + m["disclosed_at"] for m in o["new_materials"]])
    print("   候选项(current):", [(c["metric"], c["item_yi"]) for c in o["candidates"]["current"]][:3])

    f = call({**base, "action": "evaluate", "run_id": YH_RUN, "view": "current",
              "metric": "operating_payable_increase", "old_material_id": "f42e747c73850b59",
              "new_material_id": "2ce860dddd94e5a6"}, 200, "evaluate")
    art = f.payload
    cur = next(c for c in art["experiment"]["cells"] if c["view"] == "current")
    print("   本期格:", cur["points_yi"], "λ*", cur["zero_lambda"])
    print("   卡状态:", [(c["judgment_id"], c["status"]) for c in art["cards"]])
    print("   摘要首行:", art["summary_markdown"].splitlines()[0])

    f = call({**base, "action": "save", "run_id": YH_RUN, "view": "current",
              "metric": "operating_payable_increase", "old_material_id": "f42e747c73850b59",
              "new_material_id": "2ce860dddd94e5a6"}, 200, "save")
    eid = f.payload["saved"]["experiment_id"]
    print("   experiment_id:", eid, "| files:", f.payload["saved"]["files"])
    print("   urls:", json.dumps(f.payload["saved"]["urls"], ensure_ascii=False))

    f = call({**base, "action": "open", "experiment_id": eid}, 200, "open")
    print("   重开 ok:", f.payload["identity"]["experiment_id"] == eid,
          "| 正文哈希一致:", f.payload["identity"].get("content_sha256_matches"))
    print("   重开摘要行数:", len(f.payload["summary_markdown"].splitlines()))

    # 反例：身份/形态/边界（都不该被"宽容"）
    call({**base, "action": "evaluate", "run_id": YH_RUN, "view": "current"}, 200, "反例-未选材料（应 pending）")
    call({"study_id": YH_SID, "entity_id": "002304.SZ", "action": "options"}, 400, "反例-缺 result_hash")
    call({**base, "result_hash": "0" * 64, "action": "options"}, 409, "反例-result_hash 不符")
    call({**base, "action": "options", "entity_id": "000001.SZ"}, 400, "反例-研究里没有的主体")
    call({**base, "action": "evaluate", "run_id": "第一条", "view": "current"}, 400, "反例-run_id 非法")
    call({**base, "action": "evaluate", "run_id": "f" * 64, "view": "current"}, 400, "反例-非本主体的 run")
    call({**base, "action": "evaluate", "run_id": YH_RUN, "view": "daily"}, 400, "反例-view 非法")
    call({**base, "action": "evaluate", "run_id": YH_RUN, "view": "current",
          "metric": "../../etc/passwd"}, 400, "反例-metric 传路径")
    call({**base, "action": "evaluate", "run_id": YH_RUN, "view": "current",
          "old_material_id": "C:/Users/x/doc.json"}, 400, "反例-材料传路径")
    call({"study_id": "不存在的研究", "action": "options", "entity_id": "002304.SZ"}, 404, "反例-研究不存在")
    call({**base, "action": "open", "experiment_id": "zz"}, 404, "反例-实验 id 形态")
    call({**base, "action": "delete"}, 400, "反例-未知 action")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
