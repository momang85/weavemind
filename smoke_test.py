"""织光 (ZhiGuang) - 端到端冒烟测试

前提：Redis 已启动、WebUI 已运行（python launcher.py）。
用法：
    python smoke_test.py                 # 快速任务（1-2 分钟）
    python smoke_test.py --pipeline      # 完整数据流水线（5-10 分钟）
    python smoke_test.py --no-submit     # 仅检查环境（Redis/配置/API）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

import redis

WEBUI = "http://localhost:8080"

PIPELINE_GOAL = (
    "数据科学流水线: "
    "1.web_search搜索房价数据集 "
    "2.data_loader加载sklearn数据 "
    "3.data_analyzer做EDA生成图表 "
    "4.model_trainer训练模型 "
    "5.report_generator生成报告"
)
QUICK_GOAL = "请用中文一句话介绍织光智能体系统"


def check_env() -> list[str]:
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
    return problems


def api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{WEBUI}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


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

    problems = check_env()
    if problems:
        for p in problems:
            print(f"[ENV-FAIL] {p}")
        return 1
    print("[ENV-OK] Redis + config.json")

    if args.no_submit:
        return 0

    goal = PIPELINE_GOAL if args.pipeline else QUICK_GOAL
    ok = run_task(goal)
    print("[RESULT]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
