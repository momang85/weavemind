# -*- coding: utf-8 -*-
"""密钥泄漏扫描：检查被 git 追踪的文件中是否混入 API Key/Token。
CI 与本地提交前均可运行；发现即返回非 0 退出码。
"""
import re
import subprocess
import sys


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, timeout=30
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),          # OpenAI/DeepSeek 风格
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),         # GitHub PAT
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),             # AWS
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
]

SKIP_SUFFIXES = (".ipynb", ".jsonl", ".lock", ".min.js", ".map")
SKIP_DIRS = ("标准/", "node_modules/", "chroma_memory")


def scan() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for f in tracked_files():
        if any(f.startswith(d) for d in SKIP_DIRS):
            continue
        if f.endswith(SKIP_SUFFIXES):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for pat in PATTERNS:
                        m = pat.search(line)
                        if m:
                            val = m.group(0)
                            # 跳过环境变量引用（全大写标识符，如 EMBEDDING_API_KEY）
                            candidate = val.split("=", 1)[-1].strip().strip("\"'")
                            if re.fullmatch(r"[A-Z][A-Z0-9_]{5,}", candidate):
                                continue
                            hits.append((f, f"line {lineno}: {val[:24]}..."))
                            break
        except Exception:
            continue
    return hits


def main() -> int:
    hits = scan()
    if hits:
        print("!! 检测到疑似密钥泄漏（请立即轮换并移除）：")
        for f, msg in hits:
            print(f"   {f}: {msg}")
        return 1
    print("OK: 未发现密钥泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
