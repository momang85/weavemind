#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""干净环境端到端门禁（MKT-P0-1）：从零 clone → 装依赖 → 起服务 → 提交任务 → 出报告。

为什么要有它：此前的验证都在**带着未提交改动、带着既有 Redis/工作区**的本机进行，
"从真 clone 到出第一份报告"这条路径从未在干净环境跑通过。这个脚本把那条路径脚本化，
模型指向 `scripts/stub_llm.py`（不烧真实额度），Redis 与 WebUI 都用独立端口，
因此**不会**干扰正在运行的本机实例。

用法：
    python scripts/e2e_clean_check.py            # 本机演练（默认）
    python scripts/e2e_clean_check.py --ci       # CI 里跑（复用 runner 的 Redis）

退出码：0 = 走通（起服务并产出报告）；1 = 失败（打印失败点与最后日志行）。

安全边界：本脚本只会请求**自己刚在这台机器上启动**的回环地址（见 `_assert_loopback`），
不访问任何外部主机；发给 stub 的凭据是**每次运行随机生成**的占位值，不是任何真实密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORTS = {"web": 8099, "redis": 6390, "stub": 8799}
# 部署烟测的**成功态**：只有这两种才算"跑通并产出可交付结果"。
# FAILED/CANCELLED 必须走负向用例（见 --negative），不能算成功。
SUCCESS_STATES = ("SUCCESS", "SUCCESS_WITH_ISSUES")
# 本脚本起过的替身进程（含负向用例重启的那个），finally 里统一收掉
_STUB_PROCS: list = []
# 允许请求的回环主机（只有本脚本自己启动的服务在这些地址上）
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def _assert_loopback(url: str, allowed_ports: set[int]) -> None:
    """请求前校验：只允许 http + 回环主机 + 显式端口白名单。

    这个门禁脚本会轮询自己刚启动的服务；把校验写死在请求入口，避免环境变量被改错时
    变成"朝任意地址发请求"。
    """
    parts = urllib.parse.urlparse(str(url))
    if parts.scheme != "http":
        raise SystemExit(f"只允许 http 回环请求，收到：{parts.scheme!r}")
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise SystemExit(f"只允许回环主机，收到：{parts.hostname!r}")
    try:
        port = int(parts.port or 0)
    except (TypeError, ValueError):
        port = 0
    if port not in allowed_ports:
        raise SystemExit(f"端口不在白名单：{port}（允许 {sorted(allowed_ports)}）")


def _stub_credential() -> str:
    """stub 端点的占位凭据：**每次运行随机生成**，源码里不出现任何凭据字面量。"""
    return "stub-" + secrets.token_hex(8)


def _run(cmd: list[str], cwd: Path, env: dict, timeout: float = 600,
         check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        _log(f"命令失败（{proc.returncode}）：{' '.join(cmd)}")
        _log((proc.stdout or "")[-1500:])
        _log((proc.stderr or "")[-800:])
        raise SystemExit(1)
    return proc


def _wait_http(url: str, allowed_ports: set[int], timeout: float = 60) -> bool:
    _assert_loopback(url, allowed_ports)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:            # 401/403 也算"服务在听"
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci", action="store_true", help="CI 模式（Redis 用 6379）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    ap.add_argument("--timeout", type=float, default=900.0, help="任务等待上限（秒）")
    ap.add_argument("--negative", dest="negative", action="store_true", default=True,
                    help="同时跑负向用例（端点故障注入 → 必须如实失败且成功判据拒绝）")
    ap.add_argument("--no-negative", dest="negative", action="store_false",
                    help="只跑成功路径（调试用）")
    args = ap.parse_args()

    ports = dict(DEFAULT_PORTS)
    if args.ci:
        ports["redis"] = 6379
    allowed_ports = {ports["web"], ports["redis"], ports["stub"]}
    tmp = Path(tempfile.mkdtemp(prefix="wm_e2e_"))
    clone = tmp / "clone"
    stub_proc = None
    started = False
    report: dict = {
        "ok": False, "stage": "init", "clone": str(clone),
        # 口径声明：本门禁只证明"装得上、起得来、跑通、产出夹具预期产物"，
        # 用固定模型替身，**不证明真实上市公司研究的金融质量**（那是 S1/S2 的单独关卡）。
        "gate": "部署烟测（固定模型替身）",
        "proves": ["干净 clone 可安装", "依赖自检通过", "服务可就绪",
                   "任务能跑到成功终态", "交付物含夹具标记与夹具事实"],
        "does_not_prove": ["真实模型质量", "真实 SEC/行情抓取", "金融数字正确性"],
    }
    try:
        # 1) 干净 clone（只取版本库内容：未提交/被忽略的文件一律不参与）
        report["stage"] = "clone"
        _run(["git", "clone", "--quiet", str(ROOT), str(clone)], ROOT, os.environ.copy())
        head = _run(["git", "rev-parse", "--short", "HEAD"], clone,
                    os.environ.copy()).stdout.strip()
        report["commit"] = head
        _log(f"clone 完成：{clone}（commit {head}）")

        env = os.environ.copy()
        # 2) 依赖自检（requirements 覆盖 / 前端产物就绪 / Redis 可达）
        report["stage"] = "deps"
        env.update({
            "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(ports["redis"]),
            "WEB_PORT": str(ports["web"]),
            "SKIP_REDIS_CHECK": "1",           # Redis 由本机/CI 环境提供
        })
        deps = _run([sys.executable, "launcher.py", "deps"], clone, env, timeout=300,
                    check=False)
        report["deps_ok"] = deps.returncode == 0
        _log(f"依赖自检退出码={deps.returncode}")

        # 3) stub 模型（不烧真实额度；凭据为运行时随机占位）
        report["stage"] = "stub"
        base = f"http://127.0.0.1:{ports['stub']}/v1"
        stub_log = tmp / "stub.log"
        stub_proc = _spawn_stub(sys.executable, clone, ports["stub"], stub_log, "ok")
        if not _wait_http(f"{base}/models", allowed_ports, timeout=30):
            raise SystemExit("stub 模型未就绪")
        key = _stub_credential()
        env.update({
            "LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL": "stub-model",
            "PLANNER_LLM_BASE_URL": base, "PLANNER_LLM_API_KEY": key,
            "PLANNER_LLM_MODEL": "stub-model",
            "BACKUP_LLM_BASE_URL": base, "BACKUP_LLM_API_KEY": key,
            "BACKUP_LLM_MODEL": "stub-model",
            "EMBEDDING_BASE_URL": base, "EMBEDDING_API_KEY": key,
            "EMBEDDING_MODEL": "stub-embed",
            "WEAVEMIND_IDENTITY_MODE": "local",
        })
        _log(f"stub 就绪：{base}")

        # 4) 起服务
        report["stage"] = "start"
        up = _run([sys.executable, "launcher.py", "start"], clone, env, timeout=420,
                  check=False)
        started = True
        tail = (up.stdout or "").strip().splitlines()[-3:]
        report["launcher_tail"] = tail
        if not _wait_http(f"http://127.0.0.1:{ports['web']}/api/health",
                          allowed_ports, timeout=90):
            raise SystemExit(f"服务未就绪：{' | '.join(tail)}")
        _log(f"服务就绪：http://127.0.0.1:{ports['web']}")

        # 5) 提交任务（webui 同一条通道）并等到终态
        report["stage"] = "task"
        task_id = _submit(clone, env)
        report["task_id"] = task_id
        status = _await_terminal(clone, env, task_id, args.timeout)
        report["status"] = status
        if status not in ("SUCCESS", "SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED"):
            raise SystemExit(f"任务未在 {args.timeout:.0f}s 内进入终态：{status}")
        # 成功烟测只认**成功态**：FAILED/CANCELLED 是"服务跑起来了"以外的结果，
        # 此前它们照样往下走、只要文本非空就判通过（假绿）。
        if status not in SUCCESS_STATES:
            raise SystemExit(
                f"任务终态是 {status}（不是 {SUCCESS_STATES}）："
                "部署烟测只接受成功态，失败/取消必须按负向用例单独断言")

        # 6) 产出报告（按夹具内容判，不按"非空"判）
        report["stage"] = "report"
        produced, detail = _report_produced(clone, env, task_id)
        report["report_produced"] = produced
        report["report_detail"] = detail
        if not produced:
            raise SystemExit("任务结束但没有产出**夹具预期**的报告：" + detail)
        report["ok"] = True
        report["stage"] = "done"

        # 7) 负向用例（默认开启）：把替身切成"端点报错"，再走一次同一条链路，
        #    断言任务**如实失败**且成功判据拒绝它的文本。没有这一步，
        #    "失败也算产出报告"的假绿没有任何回归保护。
        if args.negative:
            report["stage"] = "negative"
            neg = _run_negative_case(clone, env, ports, allowed_ports, stub_log,
                                     args.timeout, report)
            if not neg:
                return 1
        return 0
    except SystemExit as exc:
        if str(exc):
            _log(f"失败于阶段 {report.get('stage')}：{exc}")
        return 1
    finally:
        # 无论成败都要停掉本克隆的服务与 stub，避免影响本机实例
        if started:
            subprocess.run([sys.executable, "launcher.py", "stop"], cwd=str(clone),
                           env=os.environ.copy(), capture_output=True, text=True,
                           timeout=180)
        for proc in _STUB_PROCS:
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:
                pass

        out = ROOT / "docs" / "evidence" / "e2e_clean_last.json"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            _log(f"结果写入 {out}")
        except Exception:
            pass
        if args.keep:
            _log(f"保留临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _submit(clone: Path, env: dict) -> str:
    """按 webui 的通道提交任务并等收执。"""
    sys.path.insert(0, str(clone))
    import uuid

    import redis  # noqa: PLC0415

    from common import _NO_REDIS_RETRY
    r = redis.Redis(host=env["REDIS_HOST"], port=int(env["REDIS_PORT"]),
                    decode_responses=True, socket_connect_timeout=5,
                    socket_timeout=5, retry=_NO_REDIS_RETRY)
    tid = "ui-" + uuid.uuid4().hex[:10]
    r.delete(f"task_ack:{tid}")
    r.publish("orchestrator:main", json.dumps({
        "task_id": tid, "goal": "检索公开资料并生成一份含来源链接的研究报告",
        "project": "default", "context": "", "auto_run": True,
        "template_steps": None, "user_id": "", "report_confirm": False,
        "conversation_id": "", "parent_task_id": "",
    }, ensure_ascii=False))
    deadline = time.time() + 60
    while time.time() < deadline:
        if r.get(f"task_ack:{tid}"):
            return tid
        time.sleep(0.5)
    raise SystemExit("60s 内没收到任务收执（orchestrator 未消费队列）")


def _status(tid: str) -> str:
    """读任务终态；**读不到就返回空串**，由调用方继续轮询。

    这里必须容错，不能把"还没落库"当成错误：服务刚起来时数据库文件可能已存在、
    但表还没建好（`sqlite3.OperationalError: no such table: task_history`）——
    一次查询异常就把整条门禁打红。实测在 CI 上就是这样翻车的（同一份代码上一次是
    绿的），属于"没有重试的脆弱门禁"。
    """
    import sqlite3

    import db_paths  # noqa: PLC0415

    try:
        con = sqlite3.connect(db_paths.resolve_db_path())
    except Exception as exc:                     # 库文件暂时打不开
        _log(f"状态查询暂不可用（继续轮询）：{str(exc)[:80]}")
        return ""
    try:
        row = con.execute("SELECT status FROM task_history WHERE task_id=?",
                          (tid,)).fetchone()
    except sqlite3.OperationalError as exc:      # 表还没建好
        _log(f"任务表尚未就绪（继续轮询）：{str(exc)[:80]}")
        return ""
    except Exception as exc:
        _log(f"状态查询失败（继续轮询）：{str(exc)[:80]}")
        return ""
    finally:
        con.close()
    return str(row[0]) if row else ""


def _await_terminal(clone: Path, env: dict, tid: str, timeout: float) -> str:
    del clone, env              # 只依赖已注入的模块路径
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        st = _status(tid)
        if st != last:
            _log(f"任务状态：{st or '（未落库）'}")
            last = st
        if st in ("SUCCESS", "SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED"):
            return st
        time.sleep(3.0)
    return last


def _spawn_stub(python: str, clone: Path, port: int, log_path: Path,
                mode: str) -> subprocess.Popen:
    """起一个替身端点进程（`mode=fail` 时端点如实报错）。"""
    env = os.environ.copy()
    env["WM_STUB_MODE"] = mode
    proc = subprocess.Popen(
        [python, str(clone / "scripts" / "stub_llm.py"), "--port", str(port)],
        stdout=open(log_path, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT, env=env)
    _STUB_PROCS.append(proc)
    return proc


def _run_negative_case(clone: Path, env: dict, ports: dict, allowed_ports: set,
                       stub_log: Path, timeout: float, report: dict) -> bool:
    """负向用例：端点报错时任务必须**如实失败**，且成功判据拒绝它的文本。

    断言两件事（缺一不可）：
    1. 终态不是成功态（此前 FAILED 也被当作可继续的终态）；
    2. `_report_produced` 对失败运行返回 False —— 失败说明是"非空文本"，
       早期判据会把它当报告，这正是假绿的来源。
    """
    for proc in _STUB_PROCS:                    # 换模式要重启替身
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            pass
    _STUB_PROCS.clear()
    _spawn_stub(sys.executable, clone, ports["stub"], stub_log, "fail")
    base = f"http://127.0.0.1:{ports['stub']}/v1"
    if not _wait_http(f"{base}/models", allowed_ports, timeout=30):
        report["negative_ok"] = False
        report["negative_detail"] = "故障注入替身未就绪"
        _log("负向用例失败：故障注入替身未就绪")
        return False
    tid = _submit(clone, env)
    neg_status = _await_terminal(clone, env, tid, min(float(timeout), 300.0))
    report["negative_task_id"] = tid
    report["negative_status"] = neg_status
    produced, detail = _report_produced(clone, env, tid)
    report["negative_report_accepted"] = produced
    report["negative_detail"] = detail
    ok = (neg_status not in SUCCESS_STATES) and (not produced)
    report["negative_ok"] = ok
    _log(f"负向用例：终态={neg_status}（{'符合预期' if neg_status not in SUCCESS_STATES else '预期外成功'}），"
         f"成功判据={'拒绝' if not produced else '误接受'}；{detail}")
    return ok


def _report_produced(clone: Path, env: dict, tid: str) -> tuple[bool, str]:
    """**成功路径**的报告产出的判据：交付物里必须能找到夹具标记与夹具数字。

    早期判据只要"任务库 report 非空"就算通过——失败说明（"LLM 端点不可用…"）与取消说明
    （"任务被用户取消…"）同样是"非空"，于是失败/取消也能让成功烟测变绿（假绿）。
    现在按**内容**判：必须含替身报告里的夹具标记（`FIXTURE_MARK`）与夹具事实
    （`FIXTURE_FACTS`），否则一律不算"产出了预期报告"。

    交付物可以是任务库 report，也可以是工作区里的报告文件；两处都按同一内容判据。
    """
    del env
    import sqlite3

    import db_paths  # noqa: PLC0415
    import workspace as ws_mod  # noqa: PLC0415

    try:
        from scripts.stub_llm import FIXTURE_FACTS, FIXTURE_MARK  # noqa: PLC0415
    except Exception:                       # 以脚本方式运行时按同目录导入
        sys.path.insert(0, str(ROOT / "scripts"))
        from stub_llm import FIXTURE_FACTS, FIXTURE_MARK  # noqa: PLC0415

    con = sqlite3.connect(db_paths.resolve_db_path())
    try:
        row = con.execute("SELECT report FROM task_history WHERE task_id=?",
                          (tid,)).fetchone()
    finally:
        con.close()
    candidates: list[tuple[str, str]] = []
    db_text = str((row[0] if row else "") or "")
    if db_text.strip():
        candidates.append(("任务库 report", db_text))
    ws = ws_mod.task_workspace(tid)
    rp = Path(ws) / "reports" / "report.md"
    if rp.exists():
        txt = rp.read_text(encoding="utf-8", errors="replace")
        if txt.strip():
            candidates.append((str(rp), txt))
    if not candidates:
        return False, f"任务库 report 为空且 {rp} 不存在"
    problems: list[str] = []
    for where, text in candidates:
        miss = [t for t in (FIXTURE_MARK, *FIXTURE_FACTS) if t not in text]
        if not miss:
            return True, f"{where} 含夹具标记与全部夹具事实（{len(text)} 字符）"
        problems.append(f"{where} 缺少 {miss}")
    return False, "；".join(problems) + "（失败/取消说明同样是非空文本，不算报告）"


if __name__ == "__main__":
    raise SystemExit(main())
