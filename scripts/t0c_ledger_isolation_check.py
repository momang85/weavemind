# -*- coding: utf-8 -*-
"""T0-c 隔离双进程验收：票据状态机与共享计数放在**真实 Redis** 上，两个进程各看各的。

单进程用例只能证明"同一份内存里的判断是对的"。主审反例要的是：
**另一个进程**不能凭自己那份 open 副本独立释放，插队也不能落进结算窗口。
每个步骤都在**新起的子进程**里执行，共享同一份隔离账本目录与 Redis 键空间：

- A 预留 800/1000 → B 再要 800 必须被拒（不得合计 1600）；
- 结算后共享计数等于**实际用量**（不是 800-800+800 的来回）；
- 另一个进程拿同一张票再结算/退票 → 共享状态机拒绝且不改计数；
- 待对账 → 共享计数**不释放上界**；对账后按实际用量结算。

Redis 不可用时报告 SKIP 并退出码 2（不假装通过）。只用专属前缀，结束时清理。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "t0c_ledger_isolation.json")
KEY_PREFIX = "wm:budget:t0c-isolation"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 仓库存档守卫：任何 redis.Redis(...) 都要显式关掉内建重试
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)


def _redis_client():
    import redis
    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    return redis.Redis(host=host, port=port, decode_responses=True,
                       socket_connect_timeout=3, socket_timeout=3,
                       retry=_NO_REDIS_RETRY)


def _budget():
    """子进程侧：同一份隔离账本目录 + 同一 Redis 键空间。"""
    import root_budget as rb
    ws = os.environ["T0C_WS"]
    os.makedirs(ws, exist_ok=True)
    return rb.RootBudget(os.environ["T0C_ROOT"], ws,
                         rb.BudgetLimits(max_tokens=1000),
                         redis_factory=_redis_client, multiprocess=True)


def _act(name: str, ticket: str) -> dict:
    b = _budget()
    if name == "reserve":
        tokens = int(os.environ.get("T0C_TOKENS", "800"))
        try:
            return {"ticket": b.reserve("llm", tokens=tokens), "refused": False}
        except Exception as exc:                      # noqa: BLE001 - 被拒是预期
            return {"ticket": "", "refused": True, "reason": str(exc)[:160]}
    if name == "settle":
        actual = int(os.environ.get("T0C_ACTUAL", "800"))
        b.settle(ticket, tokens=actual, usage_known=True)
        return {"ok": True}
    if name == "counter":
        b._shared_available()
        return {"tokens": b._shared("tokens"), "calls": b._shared("calls")}
    if name == "remote_settle":
        return {"verdict": b._remote_ticket_op("settle", ticket,
                                               upper_tokens=int(os.environ.get("T0C_UPPER", "800")),
                                               actual_tokens=int(os.environ.get("T0C_ACTUAL", "800")))}
    if name == "unsettled":
        return {"ok": bool(b.mark_unsettled(ticket, reason="取消：放弃等待"))}
    if name == "reconcile":
        return {"ok": bool(b.reconcile(ticket, tokens=int(os.environ.get("T0C_ACTUAL", "100")),
                                       usage_known=True, note="供应商回执晚到"))}
    if name == "ticket_state":
        return {"state": b._remote_ticket_info(ticket).get("state")}
    raise SystemExit(f"unknown act: {name}")


def _run_child(args: list[str], env: dict) -> dict:
    out = os.path.join(os.environ.get("TEMP", "."),
                       f"t0c_iso_{int(time.time() * 1000)}_{os.getpid()}.json")
    child_env = dict(env, PYTHONIOENCODING="utf-8")
    with open(out, "w", encoding="utf-8") as fh:
        rc = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                            cwd=ROOT, env=child_env, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
    with open(out, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    try:
        os.remove(out)
    except OSError:
        pass
    if rc != 0:
        return {"__error__": f"子进程退出码 {rc}: {raw.strip()[-300:]}"}
    try:
        return json.loads(raw.strip().splitlines()[-1])
    except Exception:
        return {"__error__": f"子进程输出不是 JSON: {raw.strip()[-300:]}"}


def _probe() -> dict:
    checks: list[dict] = []

    def rec(step: str, expected, actual, detail: str = "") -> None:
        ok = expected == actual
        checks.append({"step": step, "expected": expected, "actual": actual,
                       "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: 期望={expected!r} 实际={actual!r} {detail}")

    r = _redis_client()
    try:
        r.ping()
    except Exception as exc:
        print(f"SKIP: Redis 不可用（{str(exc)[:120]}），本验收不做，也不假装通过")
        return {"verdict": "skip", "reason": str(exc)[:200]}

    tmp = tempfile.mkdtemp(prefix="t0c_ledger_")
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    env = dict(os.environ)
    env.update({"T0C_WS": ws, "T0C_ROOT": "t0c-isolation"})
    try:
        out = _run_child(["--act", "reserve"], env)
        rec("A 预留 800/1000", True, bool(out.get("ticket")), str(out)[:120])
        ticket = str(out.get("ticket") or "")

        out = _run_child(["--act", "reserve"], env)
        rec("B 进程的 800 被拒（不得合计 1600）", True, bool(out.get("refused")),
            str(out.get("reason") or "")[:100])

        out = _run_child(["--act", "settle", "--ticket", ticket], env)
        rec("A 结算（实际 800）", True, bool(out.get("ok")))
        out = _run_child(["--act", "counter"], env)
        rec("结算后共享 tokens = 实际用量 800", 800, out.get("tokens"))
        out = _run_child(["--act", "reserve"], env)
        rec("结算后 B 的 800 仍被拒", True, bool(out.get("refused")))

        out = _run_child(["--act", "ticket_state", "--ticket", ticket], env)
        rec("共享票据状态 = SETTLED", "SETTLED", out.get("state"))
        out = _run_child(["--act", "remote_settle", "--ticket", ticket], env)
        rec("另一进程再结算同一张票被拒（状态机拒）", "SETTLED", out.get("verdict"))
        out = _run_child(["--act", "counter"], env)
        rec("被拒的第二次结算不改计数", 800, out.get("tokens"))

        # 待对账 → 不释放上界；对账 → 按实际用量
        env["T0C_TOKENS"] = "100"
        out = _run_child(["--act", "reserve"], env)
        ticket2 = str(out.get("ticket") or "")
        rec("A 再预留 100（剩余额度内）", True, bool(ticket2), str(out)[:120])
        out = _run_child(["--act", "unsettled", "--ticket", ticket2], env)
        rec("转待对账", True, bool(out.get("ok")))
        out = _run_child(["--act", "counter"], env)
        rec("待对账**不释放**上界（800+100）", 900, out.get("tokens"))
        env["T0C_ACTUAL"] = "40"
        out = _run_child(["--act", "reconcile", "--ticket", ticket2], env)
        rec("对账按实际用量结算", True, bool(out.get("ok")))
        out = _run_child(["--act", "counter"], env)
        rec("对账后共享 tokens = 800 + 40", 840, out.get("tokens"))
    finally:
        try:
            keys = list(r.scan_iter(match=f"{KEY_PREFIX}*"))
            if keys:
                r.delete(*keys)
            print(f"清理专属键 {len(keys)} 个（前缀 {KEY_PREFIX}）")
        except Exception as exc:
            print(f"清理失败（不影响结论）：{str(exc)[:100]}")
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in checks if not c["pass"]]
    report = {
        "batch": "T0-c",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "isolation": {"store": "真实 Redis（专属键前缀，仅本脚本使用）",
                      "processes": "每个步骤一个新起子进程",
                      "ledger": "同一份临时账本目录"},
        "checks": checks,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "verdict": "pass" if not failed else "fail",
    }
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n证据：{os.path.relpath(EVIDENCE, ROOT)}  通过 {report['passed']}/{len(checks)}  "
          f"结论={report['verdict']}")
    return report


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", default="")
    ap.add_argument("--ticket", default="")
    args = ap.parse_args()
    if args.act:
        print(json.dumps(_act(args.act, args.ticket), ensure_ascii=False))
        return 0
    report = _probe()
    if report.get("verdict") == "skip":
        return 2
    return 0 if report.get("verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
