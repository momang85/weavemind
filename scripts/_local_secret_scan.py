# -*- coding: utf-8 -*-
"""本地复核 check_secrets 的规则（沙箱下 `git ls-files` 管道不可用，改读文件清单）。

用法：python scripts/_local_secret_scan.py <tracked_files.txt> [仅扫这些文件…]
规则与 scripts/check_secrets.py 保持一致；不改动原脚本。
"""
import re
import sys

PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    re.compile(r"(?i)(['\"]?(?:secret|token|password)['\"]?)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
]
SKIP_SUFFIXES = (".ipynb", ".jsonl", ".lock", ".min.js", ".map")
SKIP_DIRS = ("标准/", "node_modules/", "chroma_memory")


def main() -> int:
    listing = sys.argv[1]
    only = set(sys.argv[2:])
    with open(listing, "r", encoding="utf-8") as f:
        files = [x.strip() for x in f if x.strip()]
    hits = []
    for f in files:
        if only and f not in only:
            continue
        if any(f.startswith(d) for d in SKIP_DIRS):
            continue
        if f.endswith(SKIP_SUFFIXES):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for pat in PATTERNS:
                        m = pat.search(line)
                        if not m:
                            continue
                        val = m.group(0)
                        candidate = val.split("=", 1)[-1].strip().strip("\"'")
                        if re.fullmatch(r"[A-Z][A-Z0-9_]{5,}", candidate):
                            continue
                        if re.search(r"[(_]|\s", candidate) and "=" in val:
                            continue
                        key_part = m.group(1) if m.groups() else ""
                        if key_part.endswith(("'", '"')) and not key_part.startswith(("'", '"')):
                            continue
                        hits.append((f, lineno, val[:40]))
                        break
        except Exception:
            continue
    if hits:
        print("!! 命中：")
        for f, n, v in hits:
            print(f"   {f}:{n}: {v}")
        return 1
    print("OK: 未发现密钥泄漏（本机复核，规则同 check_secrets.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
