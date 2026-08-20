"""织光 (ZhiGuang) - 端到端冒烟测试

前提：Redis 已启动、WebUI 已运行（python launcher.py）。
用法：
    python smoke_test.py                 # 快速任务（1-2 分钟）
    python smoke_test.py --pipeline      # 完整数据流水线（5-10 分钟）
    python smoke_test.py --no-submit     # 仅检查环境（Redis/配置/API）

多用户鉴权（V1.0+）：
    - 启动时先尝试 POST /api/login 获取会话 token，之后 api() 自动附带
      Authorization: Bearer <token>；
    - 账号默认 admin/admin，可用环境变量 WEAVEMIND_SMOKE_USER /
      WEAVEMIND_SMOKE_PASSWORD 覆盖；
    - 旧版（/api/login 返回 404/405）自动退化为无 token 直连。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import redis

WEBUI = "http://localhost:8080"

# 登录成功后保存的 Bearer token；None 表示旧版无鉴权（不带 token）
_TOKEN: str | None = None

PIPELINE_GOAL = (
    "数据科学流水线: "
    "1.web_search搜索房价数据集 "
    "2.data_loader加载sklearn数据 "
    "3.data_analyzer做EDA生成图表 "
    "4.model_trainer训练模型 "
    "5.report_generator生成报告"
)
QUICK_GOAL = "请用中文一句话介绍织光智能体系统"


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict]:
    """底层 HTTP 请求：返回 (HTTP 状态码, 解析后的 JSON)。

    网络层异常（连接失败/超时）直接向上抛出；HTTP 非 2xx 也返回状态码与响应体。
    """
    url = f"{WEBUI}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        # 多用户鉴权：登录后所有 API 请求自动带 Bearer token
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        code = exc.code
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}
    return code, payload


def api(method: str, path: str, body: dict | None = None) -> dict:
    """调用 WebUI API；登录成功后自动附带 Authorization: Bearer <token>。"""
    code, payload = _request(method, path, body, token=_TOKEN)
    if not (200 <= code < 300):
        raise RuntimeError(f"API {method} {path} 返回 HTTP {code}: {payload}")
    return payload


def check_env() -> list[str]:
    """环境检查：Redis、config.json、WebUI /api/health。

    全部使用公开端点/本地配置，不依赖登录，登录失败不会阻塞本检查。
    """
    problems = []
    try:
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()
    except Exception as exc:
        problems.append(f"Redis 不可达: {exc}")
    try:
        with open("config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        llm = cfg.get("llm", {})
        if not (llm.get("api_key") and llm.get("base_url") and llm.get("model")):
            problems.append("config.json 缺少完整 llm 配置")
    except Exception as exc:
        problems.append(f"config.json 读取失败: {exc}")
    try:
        # /api/health 是公开健康检查端点，无需登录
        code, payload = _request("GET", "/api/health", timeout=10)
        if code in (404, 405):
            # 旧版 WebUI 可能没有 /api/health；不阻塞，登录阶段再做兼容判断
            pass
        elif code != 200 or payload.get("status") != "ok":
            problems.append(f"WebUI /api/health 异常: HTTP {code} {payload}")
    except Exception as exc:
        problems.append(f"WebUI /api/health 不可达: {exc}")
    return problems


def try_login() -> bool:
    """尝试登录并设置全局 _TOKEN。

    返回 True 表示已启用 Bearer 鉴权；False 表示旧版无鉴权（继续无 token 运行）。
    凭据错误或系统未初始化管理员时，打印明确错误并以 exit 2 退出。
    """
    global _TOKEN
    username = os.environ.get("WEAVEMIND_SMOKE_USER") or "admin"
    password = os.environ.get("WEAVEMIND_SMOKE_PASSWORD") or "admin"
    print(
        f"[auth] 尝试登录 {username}（默认 admin/admin，可用 "
        "WEAVEMIND_SMOKE_USER / WEAVEMIND_SMOKE_PASSWORD 覆盖）"
    )
    try:
        code, payload = _request(
            "POST", "/api/login", {"username": username, "password": password}
        )
    except Exception as exc:
        print(f"[AUTH-FAIL] 登录接口不可达: {exc}")
        sys.exit(1)
    if code in (404, 405):
        # 旧版没有登录接口：说明该版本无鉴权，向后兼容继续无 token 运行
        print("[AUTH-SKIP] 当前版本无 /api/login（旧版），继续无 token 运行")
        return False
    if code == 200 and payload.get("token"):
        _TOKEN = payload["token"]
        print(
            f"[AUTH-OK] 登录成功：user={payload.get('user')} "
            f"role={payload.get('role')} expires_in={payload.get('expires_in')}"
        )
        return True
    if code == 401 and payload.get("setup_required"):
        print(
            "[AUTH-FAIL] 系统尚未初始化管理员，无法登录；请先设置 "
            "WEAVEMIND_ADMIN_PASSWORD 后重启服务，或打开登录页创建初始管理员"
        )
        sys.exit(2)
    if code == 401:
        print(
            "[AUTH-FAIL] 登录失败（HTTP 401）：用户名或密码错误；请通过 "
            "WEAVEMIND_SMOKE_USER / WEAVEMIND_SMOKE_PASSWORD 指定正确账号"
        )
        sys.exit(2)
    print(f"[AUTH-FAIL] 登录接口异常（HTTP {code}）: {payload}")
    sys.exit(1)


def run_task(goal: str, timeout: int = 900) -> bool:
    print(f"[submit] {goal[:60]}...")
    submitted = api("POST", "/task", {"goal": goal})
    tid = submitted.get("task_id")
    if not tid:
        print(f"[FAIL] 提交失败: {submitted}")
        return False
    print(f"[task]  {tid}")

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        try:
            data = api("GET", f"/task/{tid}")
        except Exception:
            continue
        status = data.get("status", "PENDING")
        steps = data.get("steps") or []
        done = sum(1 for s in steps if (s.get("result") or {}).get("status") == "SUCCESS")
        failed = sum(1 for s in steps if (s.get("result") or {}).get("status") == "FAILED")
        print(
            f"  [{status}] steps={len(steps)} ok={done} failed={failed} "
            f"elapsed={int(time.time()-start)}s"
        )
        if status in ("SUCCESS", "FAILED"):
            report = data.get("report") or data.get("final_report") or ""
            print(f"[report] {report[:400]}")
            return status == "SUCCESS"
    print(f"[FAIL] 超时 {timeout}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", action="store_true", help="运行完整数据流水线")
    parser.add_argument("--no-submit", action="store_true", help="只检查环境")
    args = parser.parse_args()

    # 环境检查先于登录执行，且只用公开端点，登录失败不会阻塞环境检查
    problems = check_env()
    if problems:
        for p in problems:
            print(f"[ENV-FAIL] {p}")
        return 1
    print("[ENV-OK] Redis + config.json + WebUI /api/health")

    # 尝试登录；旧版无鉴权时返回 False，保持无 token 直连
    auth_enabled = try_login()

    if args.no_submit:
        # --no-submit 模式也要验证带 token 可访问受保护接口
        try:
            status = api("GET", "/api/status")
        except Exception as exc:
            print(f"[FAIL] /api/status 访问失败: {exc}")
            return 1
        print(
            f"[OK] /api/status 可访问（Bearer 鉴权={auth_enabled}）: "
            f"{json.dumps(status, ensure_ascii=False)[:160]}"
        )
        return 0

    goal = PIPELINE_GOAL if args.pipeline else QUICK_GOAL
    ok = run_task(goal)
    print("[RESULT]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
