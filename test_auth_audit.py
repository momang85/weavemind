# -*- coding: utf-8 -*-
"""多用户鉴权与审计日志回归测试（V1.0）。

覆盖：登录成功/失败、无 token 401、viewer 只读限制（写操作 403）、
审计写入与查询、密码哈希不回显、分享链接开启鉴权后仍公开可访问、
初始管理员创建流程、登出/会话过期、Cookie 鉴权、任务删除。
"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import audit_logger
import web_ui
import workspace as ws_mod


def _make_handler():
    """构造继承自 web_ui.Handler 的最小替身，直接驱动路由方法。"""
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

        def html_body(self):
            return self.wfile.getvalue().decode("utf-8")

    return FakeHandler


class TestAuthAudit(unittest.TestCase):
    """鉴权与审计主回归。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="weavemind_auth_")
        self._old_share_file = web_ui.SHARE_FILE
        self._old_db_path = web_ui.DB_PATH
        self._old_config_path = web_ui.CONFIG_PATH
        self._old_audit_file = audit_logger.AUDIT_FILE
        self._old_root = ws_mod.WORKSPACE_ROOT
        self._old_ttl = web_ui.SESSION_TTL_SECONDS
        self._saved_sessions = dict(web_ui._sessions)
        self._saved_results = dict(web_ui._task_results)
        self._FakeHandler = _make_handler()

        web_ui.SHARE_FILE = os.path.join(self._tmp, "share_links.json")
        web_ui.DB_PATH = os.path.join(self._tmp, "test_auth.db")
        web_ui.CONFIG_PATH = os.path.join(self._tmp, "config.json")
        audit_logger.AUDIT_FILE = os.path.join(self._tmp, "audit.jsonl")
        ws_mod.configure_workspace_root(self._tmp)
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
        with web_ui._task_lock:
            web_ui._task_results.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        web_ui.SHARE_FILE = self._old_share_file
        web_ui.DB_PATH = self._old_db_path
        web_ui.CONFIG_PATH = self._old_config_path
        audit_logger.AUDIT_FILE = self._old_audit_file
        ws_mod.WORKSPACE_ROOT = self._old_root
        web_ui.SESSION_TTL_SECONDS = self._old_ttl
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
            web_ui._sessions.update(self._saved_sessions)
        with web_ui._task_lock:
            web_ui._task_results.clear()
            web_ui._task_results.update(self._saved_results)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_config(self, data):
        with open(web_ui.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _seed_users(self):
        self._write_config({
            "llm": {"model": "test"},
            "users": {
                "admin": {
                    "password_hash": web_ui._hash_password("admin123"),
                    "role": "admin",
                },
                "viewer": {
                    "password_hash": web_ui._hash_password("viewer123"),
                    "role": "viewer",
                },
            },
        })

    def _req(self, path, method="GET", body=None, token=None, cookie=None):
        h = self._FakeHandler(path, method, body, token, cookie)
        if method == "GET":
            web_ui.Handler.do_GET(h)
        elif method == "POST":
            web_ui.Handler.do_POST(h)
        elif method == "DELETE":
            web_ui.Handler.do_DELETE(h)
        return h

    def _login(self, username, password):
        h = self._req("/api/login", "POST", {"username": username, "password": password})
        self.assertEqual(h._status, 200, h.json_body())
        return h.json_body()["token"]

    def _audit(self):
        return audit_logger.read_audit(1000)

    def test_login_success_creates_session(self):
        self._seed_users()
        h = self._req("/api/login", "POST", {"username": "admin", "password": "admin123"})
        self.assertEqual(h._status, 200)
        d = h.json_body()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["role"], "admin")
        self.assertGreaterEqual(len(d["token"]), 32)
        self.assertIsNotNone(web_ui._get_session(d["token"]))
        self.assertIn("Set-Cookie", h._headers)
        self.assertIn("session=", h._headers["Set-Cookie"])
        actions = [a["action"] for a in self._audit()]
        self.assertIn("login.success", actions)
        self.assertEqual(self._audit()[-1]["user"], "admin")

    def test_login_failure_401_and_audit(self):
        self._seed_users()
        h = self._req("/api/login", "POST", {"username": "admin", "password": "wrong"})
        self.assertEqual(h._status, 401)
        self.assertIn("error", h.json_body())
        self.assertNotIn("token", h.json_body())
        entry = self._audit()[-1]
        self.assertEqual(entry["action"], "login.failed")
        self.assertEqual(entry["result"], "fail")
        self.assertEqual(entry["target"], "admin")
        # 不存在的用户也返回统一 401，不泄露用户是否存在
        h2 = self._req("/api/login", "POST", {"username": "ghost", "password": "x"})
        self.assertEqual(h2._status, 401)
        self.assertEqual(h2.json_body()["error"], h.json_body()["error"])

    def test_no_token_401_on_data_apis(self):
        self._seed_users()
        for path, method, body in [
            ("/api/status", "GET", None),
            ("/tasks", "GET", None),
            ("/task/abc", "GET", None),
            ("/api/config", "GET", None),
            ("/api/audit", "GET", None),
            ("/task", "POST", {"goal": "测试"}),
            ("/api/share", "POST", {"task_id": "t1"}),
            ("/api/share/t1", "DELETE", None),
            ("/api/task/t1", "DELETE", None),
        ]:
            h = self._req(path, method, body)
            self.assertEqual(h._status, 401, f"{method} {path} 应返回 401")
            self.assertIn("error", h.json_body())

    def test_viewer_readonly_restricted(self):
        self._seed_users()
        viewer_token = self._login("viewer", "viewer123")
        # 只读接口可访问
        self.assertEqual(self._req("/api/status", token=viewer_token)._status, 200)
        self.assertEqual(self._req("/tasks", token=viewer_token)._status, 200)
        self.assertEqual(self._req("/api/conversations", token=viewer_token)._status, 200)
        self.assertEqual(self._req("/api/task/x/deliverables", token=viewer_token)._status, 200)
        # 写操作一律 403
        for path, method, body in [
            ("/task", "POST", {"goal": "测试目标"}),
            ("/api/share", "POST", {"task_id": "t1"}),
            ("/api/share/t1", "DELETE", None),
            ("/api/task/t1", "DELETE", None),
            ("/api/config", "POST", {"llm": {"model": "x"}}),
            ("/api/memory/delete", "POST", {"type": "conversations", "all": True}),
        ]:
            h = self._req(path, method, body, token=viewer_token)
            self.assertEqual(h._status, 403, f"{method} {path} 应为 403")
        # 管理接口（配置/审计/工具审计）对 viewer 403
        self.assertEqual(self._req("/api/config", token=viewer_token)._status, 403)
        self.assertEqual(self._req("/api/audit", token=viewer_token)._status, 403)
        self.assertEqual(self._req("/api/tool-audit", token=viewer_token)._status, 403)
        # admin 不受限（分享一个不存在的任务应走到 404 而非 403）
        admin_token = self._login("admin", "admin123")
        h = self._req("/api/share", "POST", {"task_id": "t-none"}, token=admin_token)
        self.assertEqual(h._status, 404)

    def test_password_hash_never_echoed(self):
        self._seed_users()
        token = self._login("admin", "admin123")
        h = self._req("/api/config", token=token)
        self.assertEqual(h._status, 200)
        raw = self.wfile_text(h)
        self.assertNotIn("admin123", raw)
        self.assertNotIn("password_hash", raw)
        self.assertNotIn("pbkdf2_sha256", raw)
        self.assertNotIn("users", raw)
        # 前端提交 users 段不应覆盖服务端用户
        evil = {"users": {"evil": {"password_hash": "hacked", "role": "admin"}}}
        h2 = self._req("/api/config", "POST", {"llm": {"model": "m"}, **evil}, token=token)
        self.assertEqual(h2._status, 200)
        users = web_ui._load_users()
        self.assertNotIn("evil", users)
        self.assertIn("admin", users)
        self.assertIn("config.save", [a["action"] for a in self._audit()])

    def wfile_text(self, h):
        return h.wfile.getvalue().decode("utf-8")

    def test_share_and_files_public_only_when_shared(self):
        self._seed_users()
        token = self._login("admin", "admin123")
        shared_tid = "t-shared"
        with web_ui._task_lock:
            web_ui._task_results[shared_tid] = {
                "task_id": shared_tid,
                "status": "SUCCESS",
                "goal": "公开分享报告",
                "report": "# 公开报告\n\n![图](charts/a.png)",
            }
        ws = ws_mod.task_workspace(shared_tid)
        (ws / "project" / "charts").mkdir(parents=True)
        (ws / "project" / "charts" / "a.png").write_bytes(b"png")
        (ws / "charts").mkdir(parents=True)
        (ws / "charts" / "a.png").write_bytes(b"png")
        h = self._req("/api/share", "POST", {"task_id": shared_tid}, token=token)
        self.assertEqual(h._status, 200)
        share_token = h.json_body()["token"]
        # 分享页与已分享任务文件：无 token 也可访问
        h2 = self._req(f"/share/{share_token}")
        self.assertEqual(h2._status, 200)
        self.assertIn("公开报告", h2.html_body())
        h3 = self._req(f"/files/{shared_tid}/charts/a.png")
        self.assertEqual(h3._status, 200)
        self.assertEqual(h3.wfile.getvalue(), b"png")
        # 未分享任务的文件：无 token 401，带 admin token 200
        plain_tid = "t-plain"
        ws2 = ws_mod.task_workspace(plain_tid)
        (ws2 / "project" / "charts").mkdir(parents=True)
        (ws2 / "project" / "charts" / "b.png").write_bytes(b"png2")
        (ws2 / "charts").mkdir(parents=True)
        (ws2 / "charts" / "b.png").write_bytes(b"png2")
        h4 = self._req(f"/files/{plain_tid}/charts/b.png")
        self.assertEqual(h4._status, 401)
        h5 = self._req(f"/files/{plain_tid}/charts/b.png", token=token)
        self.assertEqual(h5._status, 200)
        self.assertEqual(h5.wfile.getvalue(), b"png2")

    def test_audit_write_query_limit(self):
        self._seed_users()
        token = self._login("admin", "admin123")
        # 再触发若干操作：配置保存 + 登出
        self._req("/api/config", "POST", {"system": {"task_timeout": 999}}, token=token)
        self._req("/api/logout", "POST", token=token)
        # 错误登录一次
        self._req("/api/login", "POST", {"username": "admin", "password": "bad"})
        h = self._req("/api/audit", token=self._login("admin", "admin123"))
        self.assertEqual(h._status, 200)
        d = h.json_body()
        self.assertGreaterEqual(d["count"], 5)
        self.assertEqual(len(d["entries"]), d["count"])
        entry = d["entries"][-1]
        for field in ("timestamp", "user", "ip", "action", "target", "result"):
            self.assertIn(field, entry)
        # limit 生效
        h2 = self._req("/api/audit?limit=2", token=self._login("admin", "admin123"))
        self.assertEqual(h2.json_body()["count"], 2)

    def test_setup_admin_first_run(self):
        self._write_config({"llm": {"model": "test"}})
        h0 = self._req("/api/auth/bootstrap")
        self.assertTrue(h0.json_body()["setup_required"])
        h1 = self._req("/api/login", "POST", {"username": "admin", "password": "whatever"})
        self.assertEqual(h1._status, 401)
        self.assertTrue(h1.json_body().get("setup_required"))
        # 创建初始管理员
        h2 = self._req("/api/setup-admin", "POST", {"username": "boss", "password": "boss12345"})
        self.assertEqual(h2._status, 200, h2.json_body())
        d = h2.json_body()
        self.assertEqual(d["role"], "admin")
        self.assertIsNotNone(web_ui._get_session(d["token"]))
        self.assertFalse(self._req("/api/auth/bootstrap").json_body()["setup_required"])
        # 已存在用户后不可重复初始化
        h3 = self._req("/api/setup-admin", "POST", {"username": "boss", "password": "boss12345"})
        self.assertEqual(h3._status, 409)
        # 新管理员可登录
        self.assertIsNotNone(self._login("boss", "boss12345"))

    def test_logout_cookie_and_expiry(self):
        self._seed_users()
        token = self._login("admin", "admin123")
        # Cookie 鉴权同样有效
        h = self._req("/api/status", cookie=f"session={token}")
        self.assertEqual(h._status, 200)
        # 登出后 token 立即失效
        h2 = self._req("/api/logout", "POST", token=token)
        self.assertEqual(h2._status, 200)
        self.assertEqual(self._req("/api/status", token=token)._status, 401)
        self.assertIn("logout", [a["action"] for a in self._audit()])
        # 会话过期
        web_ui.SESSION_TTL_SECONDS = -1
        stale = web_ui._create_session("admin", "admin")
        self.assertEqual(self._req("/api/status", token=stale)._status, 401)

    def test_task_delete_and_audit(self):
        self._seed_users()
        token = self._login("admin", "admin123")
        web_ui._init_db()
        db = sqlite3.connect(web_ui.DB_PATH, timeout=5)
        db.execute(
            "INSERT INTO task_history(task_id, goal, status) VALUES(?,?,?)",
            ("t-del-1", "待删除任务", "SUCCESS"),
        )
        db.commit()
        db.close()
        h = self._req("/api/task/t-del-1", "DELETE", token=token)
        self.assertEqual(h._status, 200, h.json_body())
        db = sqlite3.connect(web_ui.DB_PATH, timeout=5)
        row = db.execute("SELECT 1 FROM task_history WHERE task_id=?", ("t-del-1",)).fetchone()
        db.close()
        self.assertIsNone(row)
        self.assertIn("task.delete", [a["action"] for a in self._audit()])
        h2 = self._req("/api/task/t-del-1", "DELETE", token=token)
        self.assertEqual(h2._status, 404)

    def test_env_var_bootstrap_admin(self):
        self._write_config({"llm": {"model": "test"}})
        old_pwd = os.environ.get("WEAVEMIND_ADMIN_PASSWORD")
        old_user = os.environ.get("WEAVEMIND_ADMIN_USERNAME")
        os.environ["WEAVEMIND_ADMIN_PASSWORD"] = "env-pass-123"
        os.environ["WEAVEMIND_ADMIN_USERNAME"] = "admin"
        try:
            web_ui._ensure_users_on_startup()
        finally:
            if old_pwd is None:
                os.environ.pop("WEAVEMIND_ADMIN_PASSWORD", None)
            else:
                os.environ["WEAVEMIND_ADMIN_PASSWORD"] = old_pwd
            if old_user is None:
                os.environ.pop("WEAVEMIND_ADMIN_USERNAME", None)
            else:
                os.environ["WEAVEMIND_ADMIN_USERNAME"] = old_user
        users = web_ui._load_users()
        self.assertIn("admin", users)
        self.assertTrue(web_ui._verify_password("env-pass-123", users["admin"]["password_hash"]))
        token = self._login("admin", "env-pass-123")
        self.assertEqual(self._req("/api/status", token=token)._status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
