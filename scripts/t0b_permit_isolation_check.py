# -*- coding: utf-8 -*-
"""T0-b 隔离双进程验收：执行许可记录放在**真实 Redis** 上，两个独立进程各看各的。

为什么需要它：单进程用例只能证明"内存里的判断是对的"。主审反例要的是
"另一个进程（worker / 另一编排器实例）也看得见取消与换尝试"。这里：

- 父进程只做编排，每个步骤都在**新起的子进程**里执行（真跨进程）；
- 记录写在真实 Redis 的**专属前缀**下（`wm:permit:t0bcheck*`，只本脚本用，
  结束时清理；不碰任何真实键）；
- 覆盖：跨进程可见 → 接管换代号后旧许可失效 → 取消跨进程可见 →
  worker 侧据此**不执行**；
- Redis 不可用时**报告 SKIP 并退出码 2**（不假装通过）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "t0b_permit_isolation.json")
PREFIX = "wm:permit:t0bcheck:"
ROOT_TASK = "t0b-isolation"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 仓库守卫（test_startup_readiness.TestNoUnretriedRedisClients）要求：任何
# `redis.Redis(...)` 都必须显式关掉 redis-py 内建重试，否则 Redis 不可达时
# 单次调用会从 2 秒退化成数十秒挂死。
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)


def _redis():
    import redis
    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    return redis.Redis(host=host, port=port, decode_responses=True,
                       socket_connect_timeout=3, socket_timeout=3,
                       retry=_NO_REDIS_RETRY)


def _store():
    import execution_permit as ep
    return ep.RedisPermitStore(_redis(), prefix=PREFIX)


# ---------------------------------------------------------------- 子进程动作

def _act(name: str, permit_wire: dict | None) -> dict:
    import execution_permit as ep
    store = _store()
    if name == "begin":
        p = ep.begin_attempt(ROOT_TASK, epoch=int(os.environ["T0B_EPOCH"]),
                             owner=os.environ.get("T0B_OWNER", "owner-A"), store=store)
        return {"permit": p.to_wire()}
    if name == "validate":
        permit = ep.ExecutionPermit.from_wire(permit_wire)
        ok, reason = ep.validate(permit, ROOT_TASK, store=store)
        return {"ok": bool(ok), "reason": reason,
                "current": (ep.current_permit(ROOT_TASK, store=store).to_wire()
                            if ep.current_permit(ROOT_TASK, store=store) else None)}
    if name == "cancel":
        permit = ep.ExecutionPermit.from_wire(permit_wire)
        cur = ep.note_cancel(ROOT_TASK, store=store)
        return {"cancelled": bool(cur and cur.cancelled),
                "attempt": (cur.attempt_id if cur else ""),
                "asking": (permit.attempt_id if permit else "")}
    if name == "worker":
        # 真实 worker 的许可闸门（不执行子类 execute / 不发布）
        import execution_permit as _ep
        from worker_base import BaseWorker
        _ep.configure_store(store)          # 生产里由 _task_loop 的 _install_permit_store 装

        class _W(BaseWorker):
            def execute(self, instruction: str) -> str:
                raise AssertionError("不应执行")

        w = _W.__new__(_W)
        w.agent_id = "t0b-worker"
        w._contract = None
        w._permit = None
        w._search_status = None
        w._current_ctx = None
        w._current_gaps = []
        executed, published = [], []

        class _D:
            def execute(self, instruction):
                executed.append(instruction)
                return "ran"

        dbl = _D()
        w.execute = dbl.execute
        w._publish_result = lambda tid, status, result: published.append(
            {"status": status, "result": result})
        w._publish_failure = lambda tid, err: published.append(
            {"status": "FAILED", "result": err})
        w._process_task({"task_id": "d-1", "instruction": "查",
                         "permit": permit_wire})
        return {"executed": executed, "published": published}
    raise SystemExit(f"unknown act: {name}")


def _run_child(args: list[str], env: dict) -> dict:
    """子进程输出落到文件再读（沙箱下不依赖管道；统一 UTF-8 免得 GBK 控制台炸）。"""
    out_path = os.path.join(os.environ.get("TEMP", "."),
                            f"t0b_iso_{int(time.time() * 1000)}_{os.getpid()}.json")
    child_env = dict(env, PYTHONIOENCODING="utf-8")
    with open(out_path, "w", encoding="utf-8") as fh:
        rc = subprocess.run([sys.executable, os.path.abspath(__file__), *args],
                            cwd=ROOT, env=child_env, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
    with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    try:
        os.remove(out_path)
    except OSError:
        pass
    if rc != 0:
        return {"__error__": f"子进程退出码 {rc}: {raw.strip()[-400:]}"}
    try:
        return json.loads(raw.strip().splitlines()[-1])
    except Exception:
        return {"__error__": f"子进程输出不是 JSON: {raw.strip()[-400:]}"}


def _probe() -> dict:
    checks: list[dict] = []

    def rec(step: str, expected, actual, detail: str = "") -> None:
        ok = expected == actual
        checks.append({"step": step, "expected": expected, "actual": actual,
                       "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: 期望={expected!r} 实际={actual!r} {detail}")

    base_env = dict(os.environ)
    r = _redis()
    try:
        r.ping()
    except Exception as exc:
        print(f"SKIP: Redis 不可用（{str(exc)[:120]}），本验收不做，也不假装通过")
        return {"verdict": "skip", "reason": str(exc)[:200]}

    try:
        env_a = dict(base_env, T0B_EPOCH="1", T0B_OWNER="owner-A")
        out = _run_child(["--act", "begin"], env_a)
        rec("A 建立尝试（gen=1）", True, "permit" in out, str(out)[:120])
        permit_a = out.get("permit") or {}

        out_b = _run_child(["--act", "validate", "--permit", json.dumps(permit_a)], base_env)
        rec("B 进程看到 A 的许可（跨进程共享真源）", True, out_b.get("ok"),
            f"reason={out_b.get('reason')}")

        env_c = dict(base_env, T0B_EPOCH="3", T0B_OWNER="owner-C")
        out = _run_child(["--act", "begin"], env_c)
        permit_c = out.get("permit") or {}
        rec("C 接管并换代号（gen=3，新尝试）", True, "permit" in out)
        rec("新尝试代号与旧的不同", True,
            bool(permit_c) and permit_c.get("attempt_id") != permit_a.get("attempt_id"))

        out_b2 = _run_child(["--act", "validate", "--permit", json.dumps(permit_a)], base_env)
        rec("B 进程判定旧持有期许可失效（不得借 gen3 复活）", False, out_b2.get("ok"),
            f"reason={out_b2.get('reason')}")

        _run_child(["--act", "cancel", "--permit", json.dumps(permit_c)], base_env)
        out_b3 = _run_child(["--act", "validate", "--permit", json.dumps(permit_c)], base_env)
        rec("取消跨进程可见（当前尝试被取消即拒绝）", "cancelled", out_b3.get("reason"))

        out_w = _run_child(["--act", "worker", "--permit", json.dumps(permit_c)], base_env)
        rec("worker 进程据此不执行", [], out_w.get("executed"), str(out_w)[:160])
        rec("worker 如实回传未执行状态", "CANCELLED",
            (out_w.get("published") or [{}])[0].get("status"))

        out_w2 = _run_child(["--act", "worker", "--permit", json.dumps(permit_a)], base_env)
        rec("worker 对旧尝试同样不执行", [], out_w2.get("executed"))
        rec("worker 旧尝试回传 FAILED（非取消）", "FAILED",
            (out_w2.get("published") or [{}])[0].get("status"))
    finally:
        try:
            keys = list(r.scan_iter(match=f"{PREFIX}*"))
            if keys:
                r.delete(*keys)
            print(f"清理专属键 {len(keys)} 个（前缀 {PREFIX}）")
        except Exception as exc:
            print(f"清理失败（不影响结论）：{str(exc)[:100]}")

    failed = [c for c in checks if not c["pass"]]
    report = {
        "batch": "T0-b",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "isolation": {"store": "真实 Redis（专属前缀，仅本脚本使用）",
                      "processes": "每个步骤一个新起子进程",
                      "prefix": PREFIX},
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
    try:                                   # GBK 控制台不因个别字符中断验收
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", default="")
    ap.add_argument("--permit", default="")
    args = ap.parse_args()
    if args.act:
        wire = json.loads(args.permit) if args.permit else None
        print(json.dumps(_act(args.act, wire), ensure_ascii=False))
        return 0
    report = _probe()
    if report.get("verdict") == "skip":
        return 2
    return 0 if report.get("verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
