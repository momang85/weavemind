#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性本地全量核对：**按 CI 的同一份清单、每个文件一个进程**跑完并汇总。

为什么需要它：CI 的 backend 作业在**第一个失败文件**就停，改一处只能看到一处；
上一批（`bcc6c6d`）就是这样把 `test_offline_delivery` 的 4 个失败漏到 CI 上的。
本脚本只读本地代码，不联网、不写工作区（各用例自己用临时目录）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"


def ci_test_files() -> list[str]:
    src = CI.read_text(encoding="utf-8")
    out: list[str] = []
    for f in re.findall(r"(test_[a-z0-9_]+\.py)", src):
        if f not in out and (ROOT / f).is_file():
            out.append(f)
    return out


def main() -> int:
    files = ci_test_files()
    only = sys.argv[1:]
    if only:
        files = [f for f in files if any(o in f for o in only)]
    fails: list[tuple[str, str]] = []
    print(f"共 {len(files)} 个文件（每文件一进程）", flush=True)
    for f in files:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", f],
            cwd=str(ROOT), capture_output=True, text=True, errors="replace",
        )
        tail = (proc.stderr or "") + (proc.stdout or "")
        ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", tail)
        status = "OK" if (proc.returncode == 0 and "OK" in tail) else "FAIL"
        line = (f"{f:44s} {status:4s} "
                + (f"{ran.group(1)} tests / {ran.group(2)}s" if ran else "no summary"))
        print(line, flush=True)
        if status != "OK":
            bad = [ln for ln in tail.splitlines()
                   if ln.startswith(("FAIL:", "ERROR:", "AssertionError"))][:6]
            fails.append((f, "\n".join(bad) or tail[-800:]))
    print("\n==== 汇总 ====", flush=True)
    print(f"通过 {len(files) - len(fails)} / {len(files)}", flush=True)
    for f, detail in fails:
        print(f"\n--- {f}\n{detail}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
