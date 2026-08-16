# -*- coding: utf-8 -*-
"""生成 requirements.lock（当前环境已知可用的依赖快照，对标标准 C1 环境标准化）。
用法：python scripts/make_lock.py
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out = root / "requirements.lock"
    try:
        freeze = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True, text=True, timeout=120,
        )
        if freeze.returncode != 0:
            print(f"pip freeze 失败: {freeze.stderr[:300]}")
            return 1
    except Exception as exc:
        print(f"pip freeze 异常: {exc}")
        return 1
    header = (
        "# 已知可用的依赖快照（由 scripts/make_lock.py 生成，勿手改）\n"
        f"# 生成时间：{datetime.now(timezone.utc).isoformat()}\n"
        "# 用法：pip install -r requirements.lock\n"
        "# 重新生成：python scripts/make_lock.py\n"
    )
    body = [l for l in freeze.stdout.splitlines() if l.strip() and not l.startswith("#")]
    out.write_text(header + "\n".join(sorted(set(body))) + "\n", encoding="utf-8")
    print(f"已生成 {out}（{len(set(body))} 个包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
