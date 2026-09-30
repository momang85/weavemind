# -*- coding: utf-8 -*-
"""T0-a 隔离双实例验收：合成账户 + 隔离库 + 两个真实 web 服务进程。

为什么需要它：单元测试只证明"同进程清空内存后按真源重建"，而主审反例是
"另一实例仍放行"。这里用**两个（必要时第三个）真实 `web_ui.Handler` 进程**
共享同一个隔离 SQLite/分享文件，全部走真实 HTTP：

- 会话：A 登录 → B 认得；A 登出 → B 立刻 401；登出**之后**新起的实例 C 也 401；
- 分享：自造 `share_<token>=ok` 被拒；A 签发凭据 B 可用；跨分享不可用；
  改密后 B 的旧凭据失效；正文与附件共用同一授权；
- 账户：A 删号/降权 → B 手上旧 token 立刻失去管理员权限。

只用合成账户、临时目录、随机端口；不读真实 config.json、不碰真实 agents.db、
不改真实口令与当前权限、不发外部网络请求。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "t0a_credential_isolation.json")

ADMIN_PWD = "t0a-admin"          # 合成口令：仅本脚本临时库使用，非真实凭据
ADMIN_PWD_NEW = "t0a-admin2"
BOB_PWD = "t0a-bob1"              # ≥8 位：_post_users 对新用户有最小长度要求
CAROL_PWD = "t0a-carol"
SHARE_PWD = "t0a-share1"
SHARE_PWD_NEW = "t0a-share2"


# ---------------------------------------------------------------- 子进程（服务实例）

def _serve(port: int) -> int:
    """子进程：用隔离路径跑真实 Handler（等价于再起一个 webui 实例）。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)          # 根目录平坦布局，子进程从 scripts/ 启动
    import web_ui
    from http.server import ThreadingHTTPServer

    web_ui.CONFIG_PATH = os.environ["T0A_CONFIG"]
    import workspace as ws
    ws.configure_workspace_root(os.environ["T0A_WS"])
    try:
        web_ui._init_db()          # 生产启动顺序：建表 + 载入会话
    except Exception:
        pass
    seed_tid = os.environ.get("T0A_SEED_TID") or ""
    if seed_tid:
        for rel in (("project", "charts"), ("charts",)):
            p = ws.task_workspace(seed_tid).joinpath(*rel) / "a.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"png")
    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), web_ui.Handler)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟跳转：把 302 原样交回调用方（分享口令验证要读它的 Set-Cookie）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Instance:
    """一个真实 web 服务进程（独立端口、共享隔离库）。"""

    def __init__(self, name: str, env: dict, log_dir: str):
        self.name = name
        self.port = _free_port()
        self.log = os.path.join(log_dir, f"{name}.log")
        self._fh = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", str(self.port)],
            env=env, cwd=ROOT, stdout=self._fh, stderr=subprocess.STDOUT,
        )
        self._wait_ready()

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st, _, _ = self.request("GET", "/api/health")
                if st == 200:
                    return
            except Exception:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} 启动即退出，见 {self.log}")
            time.sleep(0.2)
        raise RuntimeError(f"{self.name} 未在 {timeout}s 内就绪，见 {self.log}")

    def request(self, method: str, path: str, body=None, cookie: str = "",
                follow: bool = True):
        """发一个真实 HTTP 请求。

        `follow=False` 用于分享口令验证：那一步是 302 + Set-Cookie，浏览器会存下
        凭据再跳转；urllib 默认会**跟跳转且丢掉 Cookie**，于是探针拿到的是
        "跳过去以后没凭据"的 401——那是探针假象，不是产品缺陷。
        """
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(
            _NoRedirect())
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self._fh.close()
        except Exception:
            pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cookie_value(headers: dict, name: str) -> str:
    raw = headers.get("Set-Cookie", "")
    for part in str(raw).split(","):
        piece = part.strip().split(";")[0]
        key, _, value = piece.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


# ---------------------------------------------------------------- 验收场景

class Check:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, step: str, expected, actual, detail: str = "") -> bool:
        ok = expected == actual
        self.rows.append({
            "step": step, "expected": expected, "actual": actual,
            "pass": bool(ok), "detail": detail,
        })
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {step}: 期望={expected!r} 实际={actual!r} {detail}")
        return ok


def _run() -> int:
    tmp = tempfile.mkdtemp(prefix="t0a_two_instance_")
    ws_root = os.path.join(tmp, "workspaces")
    os.makedirs(ws_root, exist_ok=True)
    config_path = os.path.join(tmp, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"llm": {"model": "isolation-check"}, "users": {}}, f,
                  ensure_ascii=False, indent=2)
    db_path = os.path.join(tmp, "agents.db")
    seed_tid = "t-t0a-two-instance"
    env = dict(os.environ)
    env.update({
        "WEAVEMIND_DB": db_path,
        "WEAVEMIND_DATA_DIR": tmp,
        "T0A_CONFIG": config_path,
        "T0A_WS": ws_root,
        "T0A_SEED_TID": seed_tid,
        "WEB_PORT": "0",
    })
    checks = Check()
    instances: list[Instance] = []
    try:
        a = Instance("A", env, tmp)
        instances.append(a)
        # 合成初始管理员（走真实 setup-admin，等价首启引导）
        st, hdrs, _ = a.request("POST", "/api/setup-admin",
                                {"username": "admin", "password": ADMIN_PWD})
        checks.record("合成初始管理员（隔离 config.json）", 200, st)
        b = Instance("B", env, tmp)
        instances.append(b)

        # 预置一条可分享的任务（隔离库，仅本脚本临时目录）
        db = sqlite3.connect(db_path, timeout=5)
        db.execute(
            "INSERT OR REPLACE INTO task_history(task_id, goal, status, report, created_at) "
            "VALUES(?,?,?,?,?)",
            (seed_tid, "隔离分享任务", "SUCCESS", "# 隔离报告\n\n![图](charts/a.png)",
             "2026-09-30T00:00:00+00:00"),
        )
        db.commit()
        db.close()

        # ---- S1 跨实例会话可用（先证明不是"全拒"） ----
        st, hdrs, _ = a.request("POST", "/api/login",
                                {"username": "admin", "password": ADMIN_PWD})
        token_a = _cookie_value(hdrs, "session")
        checks.record("A 登录成功并拿到会话", 200, st)
        st_b, _, _ = b.request("GET", "/api/status", cookie=f"session={token_a}")
        checks.record("S1 另一实例认这份会话（真源共享，不是全拒）", 200, st_b)

        # ---- S2 登出跨实例持久生效 + 登出后新起实例仍拒绝 ----
        st, _, _ = a.request("POST", "/api/logout", cookie=f"session={token_a}")
        checks.record("A 登出返回 200", 200, st)
        checks.record("S2 同实例登出后 401", 401,
                      a.request("GET", "/api/status", cookie=f"session={token_a}")[0])
        checks.record("S2 另一实例登出后 401", 401,
                      b.request("GET", "/api/status", cookie=f"session={token_a}")[0])
        c = Instance("C", env, tmp)      # 登出**之后**新起的进程 = 真重启
        instances.append(c)
        checks.record("S2 登出后新起实例仍 401", 401,
                      c.request("GET", "/api/status", cookie=f"session={token_a}")[0])

        # ---- S3 分享：自造常量 Cookie 被拒 ----
        st, hdrs, _ = a.request("POST", "/api/login",
                                {"username": "admin", "password": ADMIN_PWD})
        token_a2 = _cookie_value(hdrs, "session")
        st, _, body = a.request("POST", "/api/share",
                                {"task_id": seed_tid, "password": SHARE_PWD},
                                cookie=f"session={token_a2}")
        checks.record("A 生成带口令分享", 200, st)
        share_tok = json.loads(body).get("token", "")
        checks.record("分享 token 非空", True, bool(share_tok))
        checks.record("S3 自造 share_<token>=ok 访问分享页被拒", 401,
                      b.request("GET", f"/share/{share_tok}",
                                cookie=f"share_{share_tok}=ok")[0])
        checks.record("S3 自造 share_<token>=ok 取附件被拒", 401,
                      b.request("GET", f"/files/{seed_tid}/charts/a.png",
                                cookie=f"share_{share_tok}=ok")[0])

        # ---- S4 服务端签发的凭据在另一实例可用；正文与附件同一授权 ----
        st, hdrs, _ = b.request("POST", f"/share/{share_tok}/auth",
                                {"password": SHARE_PWD}, follow=False)
        grant = _cookie_value(hdrs, f"share_{share_tok}")
        checks.record("B 走分享口令验证", 302, st)
        checks.record("凭据是服务端随机值（不是常量 ok）", True,
                      bool(grant) and grant != "ok" and len(grant) >= 24,
                      f"len={len(grant)}")
        checks.record("S4 凭据在另一实例可看正文", 200,
                      b.request("GET", f"/share/{share_tok}",
                                cookie=f"share_{share_tok}={grant}")[0])
        checks.record("S4 同一凭据可取附件（共用授权）", 200,
                      b.request("GET", f"/files/{seed_tid}/charts/a.png",
                                cookie=f"share_{share_tok}={grant}")[0])

        # ---- S5 凭据只对该分享有效 ----
        st, _, body2 = a.request("POST", "/api/share", {"task_id": seed_tid,
                                                       "password": SHARE_PWD},
                                 cookie=f"session={token_a2}")
        checks.record("重复生成复用同一分享 token（幂等）", share_tok,
                      json.loads(body2).get("token", ""))
        other_tid = "t-t0a-other"
        db = sqlite3.connect(db_path, timeout=5)
        db.execute(
            "INSERT OR REPLACE INTO task_history(task_id, goal, status, report, created_at) "
            "VALUES(?,?,?,?,?)",
            (other_tid, "隔离分享任务2", "SUCCESS", "# 隔离报告2", "2026-09-30T00:00:00+00:00"),
        )
        db.commit()
        db.close()
        st, _, body3 = a.request("POST", "/api/share",
                                 {"task_id": other_tid, "password": SHARE_PWD},
                                 cookie=f"session={token_a2}")
        share_tok2 = json.loads(body3).get("token", "")
        checks.record("S5 另一分享生成成功", 200, st)
        checks.record("S5 同一凭据对别的分享无效", 401,
                      b.request("GET", f"/share/{share_tok2}",
                                cookie=f"share_{share_tok2}={grant}")[0])

        # ---- S6 改密后旧凭据跨实例失效 ----
        st, _, _ = a.request("POST", "/api/share",
                             {"task_id": seed_tid, "password": SHARE_PWD_NEW},
                             cookie=f"session={token_a2}")
        checks.record("A 改分享口令", 200, st)
        checks.record("S6 改密后旧凭据在另一实例失效", 401,
                      b.request("GET", f"/share/{share_tok}",
                                cookie=f"share_{share_tok}={grant}")[0])
        st, hdrs, _ = b.request("POST", f"/share/{share_tok}/auth",
                                {"password": SHARE_PWD_NEW}, follow=False)
        grant2 = _cookie_value(hdrs, f"share_{share_tok}")
        checks.record("S6 新口令可换新凭据并放行", 200,
                      b.request("GET", f"/share/{share_tok}",
                                cookie=f"share_{share_tok}={grant2}")[0])

        # ---- S7 删号：另一实例手上旧 token 立刻失去管理员权限 ----
        st, _, _ = a.request("POST", "/api/users",
                             {"username": "bob", "password": BOB_PWD, "role": "admin"},
                             cookie=f"session={token_a2}")
        checks.record("A 创建合成管理员 bob", 200, st)
        st, hdrs, _ = b.request("POST", "/api/login",
                                {"username": "bob", "password": BOB_PWD})
        bob_token = _cookie_value(hdrs, "session")
        checks.record("B 用 bob 登录", 200, st)
        checks.record("S7 删号前 bob 可读管理接口", 200,
                      b.request("GET", "/api/users", cookie=f"session={bob_token}")[0])
        st, _, _ = a.request("DELETE", "/api/users/bob", cookie=f"session={token_a2}")
        checks.record("A 删除 bob", 200, st)
        checks.record("S7 删号后另一实例旧 token 读管理接口被拒", 401,
                      b.request("GET", "/api/users", cookie=f"session={bob_token}")[0])
        checks.record("S7 删号后另一实例旧 token 写管理接口被拒", 401,
                      b.request("POST", "/api/config", {"llm": {"model": "x"}},
                                cookie=f"session={bob_token}")[0])
        checks.record("S7 删号后新起实例同样拒绝", 401,
                      c.request("GET", "/api/users", cookie=f"session={bob_token}")[0])

        # ---- S8 降权：另一实例旧 admin 会话立刻失去管理员权限 ----
        st, _, _ = a.request("POST", "/api/users",
                             {"username": "carol", "password": CAROL_PWD,
                              "role": "admin"},
                             cookie=f"session={token_a2}")
        checks.record("A 创建合成管理员 carol", 200, st)
        st, hdrs, _ = b.request("POST", "/api/login",
                                {"username": "carol", "password": CAROL_PWD})
        carol_token = _cookie_value(hdrs, "session")
        checks.record("S8 降权前 carol 可读管理接口", 200,
                      b.request("GET", "/api/users", cookie=f"session={carol_token}")[0])
        st, _, _ = a.request("POST", "/api/users",
                             {"username": "carol", "role": "viewer"},
                             cookie=f"session={token_a2}")
        checks.record("A 降权 carol 为 viewer", 200, st)
        carol_read = b.request("GET", "/api/users", cookie=f"session={carol_token}")[0]
        checks.record("S8 降权后旧 admin 会话读管理接口被拒（401/403）", True,
                      carol_read in (401, 403), f"status={carol_read}")
        carol_write = b.request("POST", "/api/config", {"llm": {"model": "x"}},
                                cookie=f"session={carol_token}")[0]
        checks.record("S8 降权后旧 admin 会话写管理接口被拒（401/403）", True,
                      carol_write in (401, 403), f"status={carol_write}")
        carol_new = c.request("GET", "/api/users", cookie=f"session={carol_token}")[0]
        checks.record("S8 新起实例同样不给管理员（401/403）", True,
                      carol_new in (401, 403), f"status={carol_new}")

        # ---- S9 改密：该账户全部旧会话失效（含其它实例） ----
        st, hdrs, _ = a.request("POST", "/api/login",
                                {"username": "admin", "password": ADMIN_PWD})
        token_a3 = _cookie_value(hdrs, "session")
        checks.record("S9 改密前新会话可用", 200,
                      b.request("GET", "/api/status", cookie=f"session={token_a3}")[0])
        st, _, _ = a.request("POST", "/api/users",
                             {"username": "admin", "password": ADMIN_PWD_NEW},
                             cookie=f"session={token_a3}")
        checks.record("A 改自己的口令", 200, st)
        checks.record("S9 改密后另一实例旧会话失效", 401,
                      b.request("GET", "/api/status", cookie=f"session={token_a3}")[0])
        checks.record("S9 旧口令不再可登录", 401,
                      b.request("POST", "/api/login",
                                {"username": "admin", "password": ADMIN_PWD})[0])
        checks.record("S9 新口令可登录", 200,
                      b.request("POST", "/api/login",
                                {"username": "admin", "password": ADMIN_PWD_NEW})[0])

        failures = [r for r in self_rows(checks) if not r["pass"]]
        report = {
            "batch": "T0-a",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": sys.version.split()[0],
            "isolation": {
                "db": "临时目录 agents.db（WEAVEMIND_DB）",
                "config": "临时目录 config.json（合成账户）",
                "instances": [i.name for i in instances],
                "network": "仅 127.0.0.1 本机端口；无外网请求",
            },
            "checks": self_rows(checks),
            "passed": sum(1 for r in self_rows(checks) if r["pass"]),
            "failed": len(failures),
            "verdict": "pass" if not failures else "fail",
        }
        os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
        with open(EVIDENCE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n证据：{os.path.relpath(EVIDENCE, ROOT)}  "
              f"通过 {report['passed']}/{len(self_rows(checks))}  结论={report['verdict']}")
        return 0 if not failures else 1
    finally:
        for inst in instances:
            inst.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def self_rows(checks: Check) -> list[dict]:
    return checks.rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        return _serve(args.serve)
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
