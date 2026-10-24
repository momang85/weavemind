# -*- coding: utf-8 -*-
"""R1-SCREEN-1023（R1-B）：筛选 HTTP API 测试（隔离替身，不启生产端口）。

覆盖：
- 鉴权：未登录 401（GET/POST 全部）；viewer 写操作 403、只读可看；admin 全通。
- 路由：fields / preview / run / runs / run / evidence 六个入口的正反路径。
- 参数：坏 JSON、缺 as_of、未知字段、路径穿越式 run_id。
- 证据跳转：从 run 到 evidence 绑 (run_id, entity_id)；错误主体 404 不猜。
- 隔离：WEAVEMIND_SCREEN_DIR/CACHE 指向临时目录；会话与任务缓存照 test_auth_audit 方式隔离。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

import audit_logger
import web_ui
import workspace as ws_mod


def _row(entity, code, period, *, avail, url="http://example.com/report.pdf", **metrics):
    base = {"entity": entity, "code": code, "period": period, "unit": "元",
            "currency": "CNY", "caliber": "合并", "period_type": "年度",
            "source_url": url, "source": "synthetic_test"}
    if avail is not None:
        base["available_at"] = avail
        base["disclosed_at"] = avail
    base.update(metrics)
    return base


def _make_handler():
    """与 test_auth_audit 相同的最小替身：直接驱动 web_ui.Handler 路由。"""
    class FakeHandler(web_ui.Handler):
        def __init__(self, path, method="GET", body=None, token=None, cookie=None):
            self.path = path
            self.command = method
            self.request_version = "HTTP/1.1"
            self.client_address = ("127.0.0.1", 0)
            self.headers = {"Host": "localhost:8080"}
            if token:
                self.headers["Authorization"] = "Bearer " + token
            if cookie:
                self.headers["Cookie"] = cookie
            raw = b""
            if body is not None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.headers["Content-Length"] = str(len(raw))
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self._status = 200
            self._headers = {}

        def send_response(self, code):
            self._status = code

        def send_header(self, key, value):
            self._headers[key] = value

        def end_headers(self):
            pass

        def json_body(self):
            return json.loads(self.wfile.getvalue().decode("utf-8"))

    return FakeHandler


class TestScreenApi(unittest.TestCase):
    """HTTP 层主回归（隔离实例）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="weavemind_screen_api_")
        rows = [
            _row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
                 revenue=200_000_000, gross_profit=50_000_000, net_profit=25_000_000,
                 operating_cashflow=30_000_000, total_assets=200_000_000,
                 total_liabilities=80_000_000),
            _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
                 revenue=220_000_000, gross_profit=55_000_000, net_profit=30_000_000,
                 operating_cashflow=36_000_000, total_assets=210_000_000,
                 total_liabilities=84_000_000),
        ]
        cls._cache = os.path.join(cls._tmp, "cache.json")
        with open(cls._cache, "w", encoding="utf-8") as fh:
            json.dump({"metadata": {"source": "synthetic_test", "synthetic": True},
                       "rows": rows}, fh, ensure_ascii=False)

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("WEAVEMIND_SCREEN_DIR", "WEAVEMIND_SCREEN_CACHE",
                            "WEAVEMIND_SCREEN_PRESETS", "WEAVEMIND_SCREEN_LEGACY_RUNS",
                            "WEAVEMIND_SCREEN_LEGACY_PRESETS")}
        os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(self._tmp, f"runs-{id(self)}")
        os.environ["WEAVEMIND_SCREEN_CACHE"] = self._cache
        os.environ["WEAVEMIND_SCREEN_PRESETS"] = os.path.join(self._tmp, f"presets-{id(self)}")
        os.environ["WEAVEMIND_SCREEN_LEGACY_RUNS"] = os.path.join(
            self._tmp, f"legacy-runs-{id(self)}")
        os.environ["WEAVEMIND_SCREEN_LEGACY_PRESETS"] = os.path.join(
            self._tmp, f"legacy-presets-{id(self)}")
        self._saved = {
            "share": web_ui.SHARE_FILE, "db": web_ui.DB_PATH,
            "config": web_ui.CONFIG_PATH, "audit": audit_logger.AUDIT_FILE,
            "root": ws_mod.WORKSPACE_ROOT, "ttl": web_ui.SESSION_TTL_SECONDS,
            "sessions": dict(web_ui._sessions), "results": dict(web_ui._task_results),
        }
        self._H = _make_handler()
        web_ui.SHARE_FILE = os.path.join(self._tmp, "share_links.json")
        web_ui.DB_PATH = os.path.join(self._tmp, "test_screen.db")
        web_ui.CONFIG_PATH = os.path.join(self._tmp, "config.json")
        audit_logger.AUDIT_FILE = os.path.join(self._tmp, "audit.jsonl")
        ws_mod.configure_workspace_root(self._tmp)
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
        with web_ui._task_lock:
            web_ui._task_results.clear()
        self.addCleanup(self._restore)
        self._seed_users()
        self.admin = self._login("admin", "admin123")
        self.viewer = self._login("viewer", "viewer123")
        self.researcher = self._login("researcher", "researcher123")
        self.researcher2 = self._login("researcher2", "researcher2123")
        self.rules = [{"field": "gross_margin", "op": ">=", "value": 20},
                      {"field": "cashflow_coverage", "op": ">=", "value": 100}]
        self.body = {"pool": ["000001.SZ"], "as_of": "2026-01-31", "rules": self.rules}

    def _restore(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        web_ui.SHARE_FILE = self._saved["share"]
        web_ui.DB_PATH = self._saved["db"]
        web_ui.CONFIG_PATH = self._saved["config"]
        audit_logger.AUDIT_FILE = self._saved["audit"]
        ws_mod.WORKSPACE_ROOT = self._saved["root"]
        web_ui.SESSION_TTL_SECONDS = self._saved["ttl"]
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
            web_ui._sessions.update(self._saved["sessions"])
        with web_ui._task_lock:
            web_ui._task_results.clear()
            web_ui._task_results.update(self._saved["results"])
        shutil.rmtree(os.path.join(self._tmp, f"runs-{id(self)}"), ignore_errors=True)
        shutil.rmtree(os.path.join(self._tmp, f"presets-{id(self)}"), ignore_errors=True)

    def _seed_users(self):
        with open(web_ui.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"llm": {"model": "test"},
                       # B1：普通用户自助研究权限由配置白名单控制（默认空=只有管理员能写）
                       "screen": {"research_roles": ["researcher"]},
                       "users": {
                           "admin": {"password_hash": web_ui._hash_password("admin123"),
                                     "role": "admin"},
                           "viewer": {"password_hash": web_ui._hash_password("viewer123"),
                                      "role": "viewer"},
                           "researcher": {"password_hash": web_ui._hash_password("researcher123"),
                                          "role": "researcher"},
                           # B3：第二个研究账号，用于清单/模板的跨账号隔离验证
                           "researcher2": {"password_hash": web_ui._hash_password("researcher2123"),
                                           "role": "researcher"},
                       }}, f, ensure_ascii=False, indent=2)

    def _req(self, path, method="GET", body=None, token=None):
        h = self._H(path, method, body, token)
        if method == "GET":
            web_ui.Handler.do_GET(h)
        else:
            web_ui.Handler.do_POST(h)
        return h

    def _login(self, username, password):
        h = self._req("/api/login", "POST", {"username": username, "password": password})
        self.assertEqual(h._status, 200, h.json_body())
        import re
        m = __import__("re").search(r"session=([^;]+)", h._headers.get("Set-Cookie", ""))
        self.assertIsNotNone(m)
        return m.group(1)

    # ---------- 鉴权 ----------

    def test_unauthenticated_gets_401(self):
        for path in ("/api/screen/fields", "/api/screen/runs", "/api/screen/run?id=x",
                     "/api/screen/evidence?run=x&entity=y"):
            h = self._req(path)
            self.assertEqual(h._status, 401, path)

    def test_unauthenticated_post_401_and_viewer_403(self):
        h = self._req("/api/screen/preview", "POST", self.body)
        self.assertEqual(h._status, 401)
        h = self._req("/api/screen/run", "POST", self.body, token=self.viewer)
        self.assertEqual(h._status, 403, "viewer 不能执行筛选（写操作）")
        h = self._req("/api/screen/preview", "POST", self.body, token=self.viewer)
        self.assertEqual(h._status, 403, "viewer 不能预览（POST 属写操作面）")

    def test_results_are_scoped_per_user(self):
        # R1.1：结果按服务端推导的作用域分仓——admin 执行的运行，viewer 列表为空、
        # 读详情/证据 404（不是拿到别人的记录）。
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        h = self._req("/api/screen/runs", token=self.viewer)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["runs"], [])
        h = self._req(f"/api/screen/run?id={run['run_id']}", token=self.viewer)
        self.assertEqual(h._status, 404)
        h = self._req(f"/api/screen/evidence?run={run['run_id']}&entity=000001.SZ",
                      token=self.viewer)
        self.assertEqual(h._status, 404)
        # 本作用域（admin）一切照常
        h = self._req("/api/screen/runs", token=self.admin)
        self.assertEqual(len(h.json_body()["runs"]), 1)
        h = self._req(f"/api/screen/run?id={run['run_id']}", token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["run"]["result_hash"], run["result_hash"])
        h = self._req(f"/api/screen/evidence?run={run['run_id']}&entity=000001.SZ",
                      token=self.admin)
        self.assertEqual(h._status, 200)

    def test_client_supplied_scope_never_trusted(self):
        # 客户端自报的 user/project 不被采信：请求体带 scope 也落在会话用户名下
        run = self._req("/api/screen/run", "POST",
                        {**self.body, "scope": {"user": "bob", "project": "x"}},
                        token=self.admin).json_body()["run"]
        self.assertEqual(run["scope"], {"user": "admin", "project": "personal"})
        # viewer 的作用域里看不到这条运行
        h = self._req("/api/screen/runs", token=self.viewer)
        self.assertEqual(h.json_body()["runs"], [])

    # ---------- 字段目录 ----------

    def test_fields_catalog(self):
        h = self._req("/api/screen/fields", token=self.admin)
        self.assertEqual(h._status, 200)
        d = h.json_body()
        self.assertEqual(d["schema"], "weavemind.stock_screen/1")
        ids = [f["field_id"] for f in d["fields"]]
        self.assertEqual(ids, ["revenue_growth", "gross_margin", "operating_cashflow",
                               "debt_ratio", "cashflow_coverage"])
        gm = next(f for f in d["fields"] if f["field_id"] == "gross_margin")
        self.assertEqual(gm["kind"], "pct")
        self.assertEqual(gm["numerator"], "gross_profit")

    # ---------- 预览 ----------

    def test_preview_ok_and_lists_pool_availability(self):
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "pool": ["000001.SZ", "999999.SZ"]},
                      token=self.admin)
        self.assertEqual(h._status, 200)
        d = h.json_body()
        self.assertEqual(d["missing_pool_codes"], ["999999.SZ"])
        self.assertEqual([p["code"] for p in d["available_pool"]], ["000001.SZ"])
        self.assertEqual(d["entities_ready"], 1)
        self.assertEqual(d["source"]["synthetic"], True)

    def test_preview_contract_errors_400(self):
        h = self._req("/api/screen/preview", "POST",
                      {"pool": ["000001.SZ"]}, token=self.admin)   # 缺 as_of
        self.assertEqual(h._status, 400)
        self.assertIn("as_of", h.json_body()["error"])
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "rules": [{"field": "net_margin", "op": ">=", "value": 1}]},
                      token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("未知字段", h.json_body()["error"])
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "rules": []}, token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("至少需要一条筛选条件", h.json_body()["error"])
        # 不带 rules：预览输入本身仍可用（表单第 1 步还没写规则）
        h = self._req("/api/screen/preview", "POST", {"pool": ["000001.SZ"],
                                                      "as_of": "2026-01-31"},
                      token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["rules"], [])

    # ---------- 执行 ----------

    def test_run_executes_and_audits(self):
        h = self._req("/api/screen/run", "POST", self.body, token=self.admin)
        self.assertEqual(h._status, 200)
        d = h.json_body()
        run = d["run"]
        self.assertEqual(run["counts"]["pass"], 1)
        self.assertEqual(run["counts"]["fail"] + run["counts"]["insufficient"], 0)
        self.assertFalse(run.get("reused"))
        # 第二次执行 → 复用
        h2 = self._req("/api/screen/run", "POST", self.body, token=self.admin)
        self.assertTrue(h2.json_body()["run"].get("reused"))
        self.assertEqual(h2.json_body()["run"]["run_id"], run["run_id"])
        # 审计已落（screen.run）
        entries = [a for a in audit_logger.read_audit(50) if a["action"] == "screen.run"]
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["result"], "ok")

    def test_run_contract_error_400_no_partial_result(self):
        bad = {**self.body, "rules": [{"field": "gross_margin", "op": "~", "value": 1}]}
        h = self._req("/api/screen/run", "POST", bad, token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("比较符", h.json_body()["error"])

    # ---------- 查看原版 ----------

    def test_view_original_never_recomputes(self):
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        # 改缓存数据（新输入 → 新身份），但原版读取不变
        with open(self._cache, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        data["rows"][1]["revenue"] = 999_000_000
        with open(self._cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        h = self._req(f"/api/screen/run?id={run['run_id']}", token=self.admin)
        self.assertEqual(h._status, 200)
        again = h.json_body()["run"]
        self.assertEqual(again["result_hash"], run["result_hash"], "查看原版必须原样")
        self.assertEqual(again["rows"]["pass"][0]["readings"][0]["display"],
                         run["rows"]["pass"][0]["readings"][0]["display"])
        # 重新筛选 → 新 snapshot/run
        h2 = self._req("/api/screen/run", "POST", self.body, token=self.admin)
        self.assertEqual(h2._status, 200)
        fresh = h2.json_body()["run"]
        self.assertNotEqual(fresh["snapshot_id"], run["snapshot_id"])
        self.assertNotEqual(fresh["run_id"], run["run_id"])

    def test_run_not_found_404_and_bad_ids(self):
        # 非法字符 → 400 invalid_id（明确报错，不是 500）
        h = self._req("/api/screen/run?id=does-not-exist", token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertEqual(h.json_body()["code"], "invalid_id")
        h = self._req("/api/screen/run?id=..%2F..%2Fetc%2Fpasswd", token=self.admin)
        self.assertEqual(h._status, 400, "路径穿越式 id → 400 invalid_id（不是 500）")
        # 合法格式但不存在（32 位完整 id 不做前缀匹配）→ 404
        h = self._req(f"/api/screen/run?id={'a' * 32}", token=self.admin)
        self.assertEqual(h._status, 404)
        self.assertEqual(h.json_body()["code"], "not_found")
        # 唯一短前缀可用
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        h = self._req(f"/api/screen/run?id={run['run_id'][:8]}", token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["run"]["run_id"], run["run_id"])

    # ---------- 证据 ----------

    def test_evidence_bound_to_run_and_entity(self):
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        h = self._req(f"/api/screen/evidence?run={run['run_id']}&entity=000001.SZ",
                      token=self.admin)
        self.assertEqual(h._status, 200)
        ev = h.json_body()["evidence"]
        self.assertEqual(ev["run_id"], run["run_id"])
        self.assertEqual(ev["verdict"], "pass")
        gm = next(r for r in ev["readings"] if r["field"] == "gross_margin")
        self.assertEqual({d["metric"] for d in gm["inputs"]}, {"gross_profit", "revenue"})
        # 错误主体：该 run 里没有 → 404，不拿别的 run/别的主体顶替
        h = self._req(f"/api/screen/evidence?run={run['run_id']}&entity=888888.SZ",
                      token=self.admin)
        self.assertEqual(h._status, 404)
        self.assertIn("不猜", h.json_body()["error"])
        h = self._req("/api/screen/evidence?run=zzzz&entity=000001.SZ", token=self.admin)
        self.assertEqual(h._status, 400)   # 非法 run_id → 400 invalid_id

    # ---------- 预设（股票池 + 规则） ----------

    def test_presets_requires_login_and_write_is_admin_only(self):
        h = self._req("/api/screen/presets")
        self.assertEqual(h._status, 401)
        h = self._req("/api/screen/preset", "POST",
                      {"name": "x", "pool": ["000001.SZ"], "rules": self.rules},
                      token=self.viewer)
        self.assertEqual(h._status, 403)
        h = self._req("/api/screen/preset/delete", "POST", {"preset_id": "0123456789abcdef"},
                      token=self.viewer)
        self.assertEqual(h._status, 403)
        # R1.1：预设按作用域分仓——admin 存的预设，viewer 列表为空（不是共享实例目录）
        self._req("/api/screen/preset", "POST",
                  {"name": "高毛利", "pool": ["000001.SZ"], "rules": self.rules,
                   "as_of": "2026-01-31"}, token=self.admin)
        h = self._req("/api/screen/presets", token=self.viewer)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["presets"], [])
        h = self._req("/api/screen/presets", token=self.admin)
        self.assertEqual(len(h.json_body()["presets"]), 1)

    def test_preset_save_read_update_delete(self):
        created = self._req("/api/screen/preset", "POST",
                            {"name": "甲", "pool": ["000001.SZ"], "rules": self.rules,
                             "as_of": "2026-01-31"}, token=self.admin)
        self.assertEqual(created._status, 200, created.json_body())
        pid = created.json_body()["preset"]["preset_id"]
        got = self._req(f"/api/screen/preset?id={pid}", token=self.admin)
        self.assertEqual(got._status, 200)
        self.assertEqual(got.json_body()["preset"]["name"], "甲")
        upd = self._req("/api/screen/preset", "POST",
                        {"preset_id": pid, "name": "乙",
                         "pool": ["000001.SZ"], "rules": self.rules}, token=self.admin)
        self.assertEqual(upd._status, 200)
        self.assertEqual(upd.json_body()["preset"]["preset_id"], pid)
        h = self._req("/api/screen/presets", token=self.admin)
        self.assertEqual([p["name"] for p in h.json_body()["presets"]], ["乙"])
        dele = self._req("/api/screen/preset/delete", "POST", {"preset_id": pid},
                         token=self.admin)
        self.assertEqual(dele._status, 200)
        self.assertEqual(self._req(f"/api/screen/preset?id={pid}", token=self.admin)._status, 404)

    def test_preset_keeps_dataset_source_and_rejects_dead_one(self):
        """预设要连同数据来源一起存取；来源过期/删掉后不许静默保存成指向空的预设。"""
        up = self._upload()
        self.assertEqual(up._status, 200, up.json_body())
        did = up.json_body()["dataset"]["dataset_id"]
        created = self._req("/api/screen/preset", "POST",
                            {"name": "带来源", "pool": ["000001.SZ"], "rules": self.rules,
                             "dataset_id": did}, token=self.admin)
        self.assertEqual(created._status, 200, created.json_body())
        self.assertEqual(created.json_body()["preset"]["dataset_id"], did)
        pid = created.json_body()["preset"]["preset_id"]
        listed = self._req("/api/screen/presets", token=self.admin).json_body()["presets"]
        self.assertEqual(listed[0]["dataset_id"], did)
        self.assertEqual(self._req(f"/api/screen/preset?id={pid}", token=self.admin)
                         .json_body()["preset"]["dataset_id"], did)
        bad = self._req("/api/screen/preset", "POST",
                        {"name": "空来源", "pool": ["000001.SZ"], "rules": self.rules,
                         "dataset_id": "f" * 32}, token=self.admin)
        self.assertEqual(bad._status, 400)
        self.assertIn("不存在或已删除", bad.json_body()["error"])
        self.assertEqual([p["name"] for p in
                          self._req("/api/screen/presets", token=self.admin)
                          .json_body()["presets"]], ["带来源"])

    def test_preset_contract_errors_400(self):
        h = self._req("/api/screen/preset", "POST",
                      {"name": "未知字段", "pool": ["000001.SZ"],
                       "rules": [{"field": "net_margin", "op": ">=", "value": 1}]},
                      token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("未知字段", h.json_body()["error"])
        h = self._req("/api/screen/preset", "POST",
                      {"name": "", "pool": [], "rules": self.rules}, token=self.admin)
        self.assertEqual(h._status, 400)
        # 预设 id 越界：400 且不落盘
        h = self._req("/api/screen/preset", "POST",
                      {"preset_id": "../../etc/passwd", "name": "越界",
                       "pool": ["000001.SZ"], "rules": self.rules}, token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertEqual(self._req("/api/screen/presets", token=self.admin)
                         .json_body()["presets"], [])

    def test_preset_delete_missing_404(self):
        h = self._req("/api/screen/preset/delete", "POST",
                      {"preset_id": "0123456789abcdef"}, token=self.admin)
        self.assertEqual(h._status, 404)
        h = self._req("/api/screen/preset/delete", "POST", {}, token=self.admin)
        self.assertEqual(h._status, 400)

    def test_run_payload_exposes_identity_for_staleness(self):
        """原版必须带 impl_version / field_map_version / source.sha256：前端据此判断「过期」而不是靠猜。"""
        import stock_screen
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        self.assertEqual(run["impl_version"], stock_screen.IMPL_VERSION)
        self.assertEqual(run["field_map_version"], stock_screen.FIELD_MAP_VERSION)
        self.assertTrue(run["source"]["sha256"], "输入摘要为空会让「输入已变」无法判断")
        self.assertEqual(run["source"]["sha256"],
                         __import__("hashlib").sha256(
                             open(self._cache, "rb").read()).hexdigest())


    # ---------- R1.1：期间政策 / 展示排序 / 损坏防护 / legacy ----------

    def test_fields_exposes_period_modes(self):
        d = self._req("/api/screen/fields", token=self.admin).json_body()
        modes = [m["mode"] for m in d["period_modes"]]
        self.assertEqual(modes, ["uniform", "pinned", "latest_each"])
        self.assertEqual([m["comparable"] for m in d["period_modes"]],
                         [True, True, False])

    def test_preview_accepts_period_policy_and_normalizes(self):
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "period_policy": {"mode": "pinned",
                                                      "report_period": "2023"}},
                      token=self.admin)
        self.assertEqual(h._status, 200, h.json_body())
        d = h.json_body()
        self.assertEqual(d["period_policy"], {"mode": "pinned",
                                              "report_period": "2023年"})
        self.assertEqual(d["suggested_report_period"], "2024年")
        self.assertEqual(d["per_entity_periods"]["000001.SZ"], ["2023年", "2024年"])
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "period_policy": {"mode": "bogus"}},
                      token=self.admin)
        self.assertEqual(h._status, 400)

    def test_blank_rule_row_is_an_explicit_error_not_silently_dropped(self):
        """前端会丢弃「字段和阈值都空」的行；后端绝不静默删规则（少一条就变了含义）。"""
        rules = [{"field": "gross_margin", "op": ">=", "value": 20},
                 {"field": "", "op": ">=", "value": ""}]
        h = self._req("/api/screen/preview", "POST", {**self.body, "rules": rules},
                      token=self.admin)
        self.assertEqual(h._status, 400, h.json_body())
        errs = " ".join(h.json_body().get("errors") or [h.json_body().get("error", "")])
        self.assertTrue("rules[1]" in errs, errs)
        self.assertTrue("未知字段" in errs or "阈值" in errs, errs)
        h = self._req("/api/screen/preview", "POST",
                      {**self.body, "period_policy": {"mode": "pinned"}},
                      token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("report_period", h.json_body()["error"])

    def test_run_period_policy_changes_identity(self):
        r1 = self._req("/api/screen/run", "POST", self.body,
                       token=self.admin).json_body()["run"]
        r2 = self._req("/api/screen/run", "POST",
                       {**self.body, "period_policy": {"mode": "pinned",
                                                       "report_period": "2023"}},
                       token=self.admin).json_body()["run"]
        self.assertNotEqual(r1["run_id"], r2["run_id"])
        self.assertEqual(r1["report_period"], "2024年")
        self.assertEqual(r2["report_period"], "2023年")
        self.assertTrue(r1["comparable"])
        # 逐家最新：明确标不可横比
        r3 = self._req("/api/screen/run", "POST",
                       {**self.body, "period_policy": {"mode": "latest_each"}},
                       token=self.admin).json_body()["run"]
        self.assertFalse(r3["comparable"])

    def test_run_sort_is_view_only_same_run(self):
        r1 = self._req("/api/screen/run", "POST", self.body,
                       token=self.admin).json_body()["run"]
        h = self._req("/api/screen/run", "POST",
                      {**self.body, "sort": {"field": "gross_margin",
                                             "direction": "asc"}},
                      token=self.admin)
        self.assertEqual(h._status, 200)
        r2 = h.json_body()["run"]
        self.assertEqual(r1["run_id"], r2["run_id"])
        self.assertTrue(r2["reused"])
        self.assertEqual(r2["view"]["sort"], {"field": "gross_margin",
                                              "direction": "asc"})
        # GET 侧同样支持展示排序，且不改哈希
        h = self._req(f"/api/screen/run?id={r1['run_id']}&sort=gross_margin:desc",
                      token=self.admin)
        self.assertEqual(h._status, 200)
        got = h.json_body()["run"]
        self.assertEqual(got["result_hash"], r1["result_hash"])
        self.assertEqual(got["view"]["sort"], {"field": "gross_margin",
                                               "direction": "desc"})
        h = self._req(f"/api/screen/run?id={r1['run_id']}&sort=net_margin:desc",
                      token=self.admin)
        self.assertEqual(h._status, 400)
        h = self._req("/api/screen/run", "POST",
                      {**self.body, "sort": {"field": "gross_margin",
                                             "direction": "up"}},
                      token=self.admin)
        self.assertEqual(h._status, 400)

    def test_frozen_corrupt_is_409_and_recovery_is_explicit(self):
        import screen_store
        run = self._req("/api/screen/run", "POST", self.body,
                        token=self.admin).json_body()["run"]
        root = screen_store.runs_root({"user": "admin", "project": "personal"})
        p = os.path.join(root, run["run_id"], "run.json")
        with open(p, "r+b") as fh:
            fh.seek(0)
            fh.write(b"CORRUPTED")
        with open(p, "rb") as fh:
            damaged = fh.read()
        h = self._req("/api/screen/run", "POST", self.body, token=self.admin)
        self.assertEqual(h._status, 409)
        body = h.json_body()
        self.assertEqual(body["code"], "frozen_corrupt")
        self.assertIn("不会", body["note"] + body["error"])
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), damaged, "损坏文件不得被自动重写")
        # 隔离需要显式 confirm
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": run["run_id"]}, token=self.admin)
        self.assertEqual(h._status, 400)
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": run["run_id"], "confirm": True}, token=self.admin)
        self.assertEqual(h._status, 200, h.json_body())
        self.assertIn("quarantine", h.json_body()["moved_to"])
        # 隔离后同身份重跑 → 新结果可用
        h = self._req("/api/screen/run", "POST", self.body, token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertEqual(h.json_body()["run"]["run_id"], run["run_id"])
        self.assertFalse(h.json_body()["run"]["reused"])
        # 健康结果不允许隔离
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": run["run_id"], "confirm": True}, token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertEqual(h.json_body()["code"], "not_damaged")
        # viewer 不能隔离
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": run["run_id"], "confirm": True}, token=self.viewer)
        self.assertEqual(h._status, 403)

    def test_legacy_endpoints_admin_only(self):
        h = self._req("/api/screen/legacy")
        self.assertEqual(h._status, 401)
        h = self._req("/api/screen/legacy", token=self.viewer)
        self.assertEqual(h._status, 403)
        h = self._req("/api/screen/legacy", token=self.admin)
        self.assertEqual(h._status, 200)
        d = h.json_body()
        self.assertEqual(d["runs"], [])
        self.assertIn("不迁移", d["note"])
        self.assertTrue(d["suggestion"])
        h = self._req("/api/screen/legacy/run?id=" + "f" * 32, token=self.viewer)
        self.assertEqual(h._status, 403)
        h = self._req("/api/screen/legacy/run?id=" + "f" * 32, token=self.admin)
        self.assertEqual(h._status, 404)

    def test_run_does_not_leak_other_scope_preset_ids(self):
        created = self._req("/api/screen/preset", "POST",
                            {"name": "甲的预设", "pool": ["000001.SZ"],
                             "rules": self.rules}, token=self.admin)
        pid = created.json_body()["preset"]["preset_id"]
        # viewer 读不到、删不掉（404 而非 403，避免泄漏存在性）
        h = self._req(f"/api/screen/preset?id={pid}", token=self.viewer)
        self.assertEqual(h._status, 404)
        h = self._req("/api/screen/preset/delete", "POST", {"preset_id": pid},
                      token=self.viewer)
        self.assertEqual(h._status, 403)   # POST 面：viewer 直接被权限挡住
        h = self._req("/api/screen/preset/delete", "POST", {"preset_id": pid},
                      token=self.admin)
        self.assertEqual(h._status, 200)


    # ---------- R1.2 数据入口：模板 / 上传数据集 ----------

    def _csv_text(self) -> str:
        import csv as _csv
        import screen_store as _st
        buf = io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=list(_st.CSV_COLUMNS),
                                 extrasaction="ignore", restval="")
        writer.writeheader()
        for period, avail, rev, gp, np_, ocf in (
                ("2023-12-31", "2024-04-20", 200_000_000, 50_000_000, 25_000_000, 30_000_000),
                ("2024-12-31", "2025-04-18", 220_000_000, 55_000_000, 30_000_000, 36_000_000)):
            writer.writerow({
                "entity": "甲公司", "code": "000001.SZ", "period": period,
                "unit": "元", "currency": "CNY", "caliber": "合并", "period_type": "年报",
                "source_url": "https://example.com/我的年报.pdf",
                "available_at": avail, "disclosed_at": avail,
                "revenue": rev, "gross_profit": gp, "net_profit": np_,
                "operating_cashflow": ocf, "total_assets": 200_000_000,
                "total_liabilities": 80_000_000,
            })
        return buf.getvalue()

    def _upload(self, token=None, **extra):
        body = {"name": "我的导入", "filename": "a.csv", "content": self._csv_text()}
        body.update(extra)
        return self._req("/api/screen/datasets", "POST", body, token=token or self.admin)

    def test_template_download_needs_login_and_is_csv(self):
        h = self._req("/api/screen/template")
        self.assertEqual(h._status, 401)
        h = self._req("/api/screen/template", token=self.viewer)
        self.assertEqual(h._status, 200)
        raw = h.wfile.getvalue()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))          # BOM：Excel 双击不乱码
        body = raw.decode("utf-8-sig")
        self.assertTrue(body.splitlines()[0].startswith("entity,code,period"))
        self.assertIn("text/csv", h._headers.get("Content-Type", ""))
        self.assertIn("attachment", h._headers.get("Content-Disposition", ""))

    def test_upload_requires_admin_and_is_never_official(self):
        h = self._upload(token=self.viewer)
        self.assertEqual(h._status, 403)
        h = self._req("/api/screen/datasets", "POST", {"content": self._csv_text()})
        self.assertEqual(h._status, 401)
        h = self._upload()
        self.assertEqual(h._status, 200, h.json_body())
        data = h.json_body()
        self.assertFalse(data["dataset"]["official"])
        self.assertTrue(data["dataset"]["user_provided"])
        self.assertEqual(data["dataset"]["codes"], ["000001.SZ"])
        did = data["dataset"]["dataset_id"]

        # 列表：admin 看得到，viewer 看不到（作用域隔离）；官方缓存显式标注
        h = self._req("/api/screen/datasets", token=self.admin)
        listing = h.json_body()
        self.assertEqual([d["dataset_id"] for d in listing["datasets"]], [did])
        self.assertEqual(listing["official_source"]["kind"], "official_cache")
        self.assertFalse(listing["official_source"]["official"], "合成夹具不得标成官方")
        h = self._req("/api/screen/datasets", token=self.viewer)
        self.assertEqual(h.json_body()["datasets"], [])

        # 跨作用域按 ID 读 → 404（不泄露存在性）
        h = self._req(f"/api/screen/dataset?id={did}", token=self.viewer)
        self.assertEqual(h._status, 404)
        h = self._req(f"/api/screen/dataset?id={did}", token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertEqual([e["code"] for e in h.json_body()["available_pool"]], ["000001.SZ"])
        h = self._req(f"/api/screen/pool?dataset={did}", token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertIn("用户数据集", h.json_body()["source"]["label"])
        self.assertFalse(h.json_body()["source"]["official"])

    def test_upload_rejects_unknown_columns_and_blank_content(self):
        h = self._upload(content="entity,code,period,备注\n甲,000001.SZ,2024-12-31,备忘\n")
        self.assertEqual(h._status, 400)
        self.assertIn("未知列", h.json_body()["error"])
        h = self._req("/api/screen/datasets", "POST", {"name": "空的"}, token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("缺 content", h.json_body()["error"])

    def test_preview_and_run_use_uploaded_dataset(self):
        did = self._upload().json_body()["dataset"]["dataset_id"]
        body = {**self.body, "dataset_id": did}
        h = self._req("/api/screen/preview", "POST", body, token=self.admin)
        self.assertEqual(h._status, 200, h.json_body())
        preview = h.json_body()
        self.assertIn("用户数据集", preview["source"]["label"])
        self.assertFalse(preview["source"]["official"])
        self.assertTrue(preview["source"]["user_provided"])

        h = self._req("/api/screen/run", "POST", body, token=self.admin)
        self.assertEqual(h._status, 200, h.json_body())
        run = h.json_body()["run"]
        self.assertEqual(run["source"]["dataset_id"], did)
        self.assertFalse(run["source"]["official"])
        self.assertEqual([r["entity_id"] for r in run["rows"]["pass"]], ["000001.SZ"])

        # 证据仍绑 (run_id, entity)：来源 URL 来自用户上传的行
        h = self._req(f"/api/screen/evidence?run={run['run_id']}&entity=000001.SZ",
                      token=self.admin)
        self.assertEqual(h._status, 200)
        self.assertIn("我的年报", json.dumps(h.json_body(), ensure_ascii=False))

        # 用不存在的 dataset_id → 明确 400，而不是悄悄回退到官方缓存
        h = self._req("/api/screen/run", "POST", {**self.body, "dataset_id": "a" * 32},
                      token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("数据集不存在", h.json_body()["error"])

    # ---------- R1.3 两份冻结结果的人工复筛对比 ----------

    def test_compare_requires_login_and_validates_ids(self):
        h = self._req("/api/screen/compare?a=1&b=2")
        self.assertEqual(h._status, 401)
        run = self._req("/api/screen/run", "POST", self.body, token=self.admin).json_body()["run"]
        h = self._req(f"/api/screen/compare?a={run['run_id']}", token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("缺参数", h.json_body()["error"])
        h = self._req(f"/api/screen/compare?a={run['run_id']}&b={run['run_id']}",
                      token=self.admin)
        self.assertEqual(h._status, 400)
        self.assertIn("不能和自己对比", h.json_body()["error"])
        h = self._req(f"/api/screen/compare?a={run['run_id']}&b={'0' * 32}", token=self.admin)
        self.assertEqual(h._status, 404)
        h = self._req(f"/api/screen/compare?a=zz&b=yy", token=self.admin)
        self.assertEqual(h._status, 400)

    def test_compare_two_runs_and_label_not_attributable(self):
        a = self._req("/api/screen/run", "POST", self.body, token=self.admin).json_body()["run"]
        stricter = {**self.body, "rules": [{"field": "gross_margin", "op": ">=", "value": 90}]}
        b = self._req("/api/screen/run", "POST", stricter,
                      token=self.admin).json_body()["run"]
        self.assertNotEqual(a["run_id"], b["run_id"])
        h = self._req(f"/api/screen/compare?a={a['run_id']}&b={b['run_id']}", token=self.admin)
        self.assertEqual(h._status, 200, h.json_body())
        data = h.json_body()
        self.assertEqual(data["schema"], "weavemind.stock_screen.compare/1")
        self.assertIn("rules", {c["key"] for c in data["changed_conditions"]})
        self.assertEqual(data["attribution"]["label"], "不可直接归因")
        # 字段白名单随响应下发，前端不得自造字段
        self.assertEqual([f["field"] for f in data["field_whitelist"]],
                         ["revenue_growth", "gross_margin", "operating_cashflow",
                          "debt_ratio", "cashflow_coverage"])
        # 只读：对比后两份结果仍在，且读取不改哈希
        back = self._req(f"/api/screen/run?id={a['run_id']}", token=self.admin).json_body()
        self.assertEqual(back["run"]["result_hash"], a["result_hash"])

    def test_compare_is_scoped_like_other_reads(self):
        a = self._req("/api/screen/run", "POST", self.body, token=self.admin).json_body()["run"]
        b = self._req("/api/screen/run", "POST",
                      {**self.body, "as_of": "2025-01-31"},
                      token=self.admin).json_body()["run"]
        h = self._req(f"/api/screen/compare?a={a['run_id']}&b={b['run_id']}",
                      token=self.viewer)
        self.assertEqual(h._status, 404, "viewer 看不到别人作用域的结果，也不泄露存在性")


    # ---------- B1：普通用户自助研究权限（最小、本作用域、可配置） ----------

    def test_capabilities_by_role(self):
        h = self._req("/api/screen/capabilities")
        self.assertEqual(h._status, 401)
        d = self._req("/api/screen/capabilities", token=self.admin).json_body()
        self.assertEqual((d["role"], d["can_research"], d["read_only_reason"]),
                         ("admin", True, ""))
        d = self._req("/api/screen/capabilities", token=self.viewer).json_body()
        self.assertFalse(d["can_research"])
        self.assertIn("只读", d["read_only_reason"])
        d = self._req("/api/screen/capabilities", token=self.researcher).json_body()
        self.assertTrue(d["can_research"])
        self.assertEqual(d["read_only_reason"], "")
        self.assertEqual(d["scope"], {"user": "researcher", "project": "personal"})
        self.assertEqual(d["self_service_roles"], ["researcher"])

    def test_researcher_can_upload_and_run_only_in_own_scope(self):
        h = self._upload(token=self.researcher)
        self.assertEqual(h._status, 200, h.json_body())
        did = h.json_body()["dataset"]["dataset_id"]
        h = self._req("/api/screen/run", "POST",
                      {**self.body, "dataset_id": did}, token=self.researcher)
        self.assertEqual(h._status, 200, h.json_body())
        run = h.json_body()["run"]
        self.assertEqual(run["scope"], {"user": "researcher", "project": "personal"})
        h = self._req("/api/screen/runs", token=self.researcher)
        self.assertEqual([r["run_id"] for r in h.json_body()["runs"]], [run["run_id"]])
        # 管理员看不到研究员作用域，反之亦然（跨账户 404，不是 403 泄露存在性）
        self.assertEqual(self._req("/api/screen/runs", token=self.admin).json_body()["runs"], [])
        self.assertEqual(self._req(f"/api/screen/run?id={run['run_id']}",
                                   token=self.admin)._status, 404)
        admin_run = self._req("/api/screen/run", "POST", self.body,
                              token=self.admin).json_body()["run"]
        self.assertEqual(self._req(f"/api/screen/run?id={admin_run['run_id']}",
                                   token=self.researcher)._status, 404)
        # 研究员自己的数据集也只能自己看
        self.assertEqual(self._req(f"/api/screen/dataset?id={did}",
                                   token=self.admin)._status, 404)

    def test_researcher_cannot_reach_admin_surfaces(self):
        for path in ("/api/config", "/api/users", "/api/audit", "/api/screen/legacy"):
            self.assertEqual(self._req(path, token=self.researcher)._status, 403, path)
        # 封存/隔离运行仍是管理员动作（不随研究权限开放）
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": "0" * 32, "reason": "x"}, token=self.researcher)
        self.assertEqual(h._status, 403)
        h = self._req("/api/screen/run/quarantine", "POST",
                      {"run_id": "0" * 32, "reason": "x"}, token=self.admin)
        self.assertIn(h._status, (400, 404))

    def test_viewer_write_is_denied_with_actionable_reason(self):
        h = self._req("/api/screen/run", "POST", self.body, token=self.viewer)
        self.assertEqual(h._status, 403)
        d = h.json_body()
        self.assertEqual(d["code"], "screen_read_only")
        self.assertIn("只读", d["error"])
        self.assertIn("管理员", d["error"])
        self.assertEqual(self._req("/api/screen/runs", token=self.viewer)._status, 200)

    def test_research_roles_default_is_admin_only(self):
        # 缺 screen.research_roles（默认）：researcher 也只有只读，写入口 403
        with open(web_ui.CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg.pop("screen", None)
        with open(web_ui.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        h = self._req("/api/screen/capabilities", token=self.researcher)
        self.assertFalse(h.json_body()["can_research"])
        self.assertIn("只读", h.json_body()["read_only_reason"])
        self.assertEqual(self._upload(token=self.researcher)._status, 403)

    def test_admin_can_create_researcher_role_when_whitelisted(self):
        """B1 反例：登录页曾把非 viewer 角色一律显示成「管理员」；
        建号入口也曾只允许 admin/viewer。白名单内的自助研究角色必须能建、能登录、
        且登录返回的 role 就是 researcher（前端原样保存，不再归一成 admin）。"""
        h = self._req("/api/users", method="POST", token=self.admin,
                      body={"username": "newres", "password": "newres12345",
                            "role": "researcher"})
        self.assertEqual(h._status, 200, h.json_body())
        login = self._req("/api/login", method="POST",
                          body={"username": "newres", "password": "newres12345"})
        self.assertEqual(login._status, 200)
        self.assertEqual(login.json_body()["role"], "researcher")
        # 用新账号自己的会话再确认一次能力（建号成功不等于能写）
        jar = self._login("newres", "newres12345")
        caps = self._req("/api/screen/capabilities", token=jar)
        self.assertTrue(caps.json_body()["can_research"])

    def test_unknown_role_is_rejected_with_actionable_message(self):
        h = self._req("/api/users", method="POST", token=self.admin,
                      body={"username": "auditor1", "password": "auditor12345",
                            "role": "auditor"})
        self.assertEqual(h._status, 400)
        self.assertIn("screen.research_roles", h.json_body()["error"])
        self.assertIn("researcher", h.json_body()["error"])


    # ---------- B3：研究清单 / 规则模板 / 成本分账 ----------

    def test_watchlist_isolated_between_researchers(self):
        add = self._req("/api/screen/watchlist", "POST",
                        {"action": "add", "code": "600519.SH", "name": "贵州茅台",
                         "note": "先观察渠道"}, token=self.researcher)
        self.assertEqual(add._status, 200, add.json_body())
        mine = self._req("/api/screen/watchlist", token=self.researcher).json_body()
        self.assertEqual([i["code"] for i in mine["watchlist"]["items"]], ["600519.SH"])
        self.assertEqual(mine["watchlist"]["items"][0]["note"], "先观察渠道")
        other = self._req("/api/screen/watchlist", token=self.researcher2).json_body()
        self.assertEqual(other["watchlist"]["items"], [])       # 跨账号不可见
        # 另一个账号写自己的清单，不影响第一个账号
        self._req("/api/screen/watchlist", "POST",
                  {"action": "add", "code": "000001.SZ"}, token=self.researcher2)
        mine2 = self._req("/api/screen/watchlist", token=self.researcher).json_body()
        self.assertEqual([i["code"] for i in mine2["watchlist"]["items"]], ["600519.SH"])

    def test_watchlist_viewer_read_only(self):
        read = self._req("/api/screen/watchlist", token=self.viewer)
        self.assertEqual(read._status, 200, read.json_body())
        write = self._req("/api/screen/watchlist", "POST",
                          {"action": "add", "code": "600519.SH"}, token=self.viewer)
        self.assertEqual(write._status, 403, write.json_body())
        self.assertIn("只读", write.json_body()["error"])

    def test_watchlist_bad_code_and_overlong_note(self):
        bad = self._req("/api/screen/watchlist", "POST",
                        {"action": "add", "code": "600519"}, token=self.researcher)
        self.assertEqual(bad._status, 400, bad.json_body())
        self.assertIn("不合法", bad.json_body()["error"])
        long_note = self._req("/api/screen/watchlist", "POST",
                              {"action": "add", "code": "600519.SH",
                               "note": "字" * 501}, token=self.researcher)
        self.assertEqual(long_note._status, 400, long_note.json_body())
        self.assertIn("500", long_note.json_body()["error"])

    def test_rules_template_roundtrip_and_no_run(self):
        exported = self._req("/api/screen/rules/export", "POST",
                             {"rules": self.rules,
                              "period_policy": {"mode": "uniform", "report_period": ""}},
                             token=self.researcher)
        self.assertEqual(exported._status, 200, exported.json_body())
        template = exported.json_body()["template"]
        self.assertNotIn("pool", template)
        self.assertNotIn("000001", json.dumps(template, ensure_ascii=False))
        imported = self._req("/api/screen/rules/import", "POST",
                             {"template": template}, token=self.researcher2)
        self.assertEqual(imported._status, 200, imported.json_body())
        self.assertEqual(imported.json_body()["preview"]["rules_count"], 2)
        self.assertEqual(imported.json_body()["warnings"], [])
        runs = self._req("/api/screen/runs", token=self.researcher2).json_body()
        self.assertEqual(runs.get("runs") or [], [])           # 导入不执行、不落结果

    def test_rules_import_rejects_private_and_unknown(self):
        bad_private = self._req("/api/screen/rules/import", "POST",
                                {"template": {"schema": "weavemind.stock_screen.rules/1",
                                              "rules": self.rules,
                                              "pool": ["600519.SH"]}},
                                token=self.researcher)
        self.assertEqual(bad_private._status, 400, bad_private.json_body())
        self.assertIn("私人", bad_private.json_body()["error"])
        bad_field = self._req("/api/screen/rules/import", "POST",
                              {"template": {"schema": "weavemind.stock_screen.rules/1",
                                            "rules": [{"field": "net_profit", "op": ">=",
                                                       "value": 1}]}},
                              token=self.researcher)
        self.assertEqual(bad_field._status, 400, bad_field.json_body())
        self.assertIn("模板规则不合法", bad_field.json_body()["error"])
        runs = self._req("/api/screen/runs", token=self.researcher).json_body()
        self.assertEqual(runs.get("runs") or [], [])           # 畸形模板不触发运行

    def test_rules_import_viewer_forbidden(self):
        h = self._req("/api/screen/rules/import", "POST",
                      {"template": {"schema": "weavemind.stock_screen.rules/1",
                                    "rules": self.rules}}, token=self.viewer)
        self.assertEqual(h._status, 403, h.json_body())

    def test_cost_endpoint_after_run(self):
        run = self._req("/api/screen/run", "POST", self.body, token=self.researcher)
        self.assertEqual(run._status, 200, run.json_body())
        cost = self._req("/api/screen/cost", token=self.researcher).json_body()["cost"]
        self.assertGreaterEqual(cost["runs_total"], 1)
        self.assertEqual(cost["model_calls"], 0)
        self.assertEqual(cost["external_fee"], "unknown")
        self.assertGreaterEqual(cost["compute_runs"] + cost["cache_hits"], 1)
        viewer_cost = self._req("/api/screen/cost", token=self.viewer)
        self.assertEqual(viewer_cost._status, 200)             # 只读也能看自己的账本

class _BrokenWFile:
    """写完之前客户端就断开：write 抛连接类异常。"""

    def __init__(self, exc):
        self.exc = exc

    def write(self, _body):
        raise self.exc


class TestDisconnectTolerance(unittest.TestCase):
    """客户端刷新/关标签/切账号主动 abort 时，已完成的请求不应变成服务端错误栈。"""

    def test_json_and_html_survive_client_abort(self):
        for exc in (BrokenPipeError(), ConnectionAbortedError(10053, "aborted"),
                    ConnectionResetError(10054, "reset")):
            for call in (lambda h: h._json({"status": "ok"}),
                         lambda h: h._html("<html>ok</html>")):
                handler = _make_handler()("/api/health")
                handler.wfile = _BrokenWFile(exc)
                call(handler)                       # 不应该抛出
                self.assertEqual(handler._status, 200)

if __name__ == "__main__":
    unittest.main(verbosity=1)
