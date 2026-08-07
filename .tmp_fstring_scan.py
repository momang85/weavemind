# -*- coding: utf-8 -*-
"""启发式扫描：f-string 表达式部分含反斜杠（Python <=3.11 语法错误）。"""
import re
import subprocess

files = subprocess.check_output(["git", "ls-files", "*.py"], text=True).splitlines()
pat = re.compile(r"f([\"'])(.*?)\1", re.S)


def find_backslash_in_braces(s: str):
    """在 f-string 内容中找出 {…} 区间内含反斜杠的位置。"""
    hits = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "{":
            depth = 1
            j = i + 1
            while j < n and depth:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                j += 1
            expr = s[i + 1 : j - 1]
            if "\\" in expr:
                hits.append((i, expr[:60]))
            i = j
        else:
            i += 1
    return hits


for f in files:
    with open(f, "r", encoding="utf-8") as fh:
        src = fh.read()
    # 文件级：三引号 f-string
    for m in re.finditer(r"f('''|\"\"\")", src):
        q = m.group(1)
        end = src.find(q, m.end())
        if end == -1:
            continue
        body = src[m.end() : end]
        hits = find_backslash_in_braces(body)
        if hits:
            line = src[: m.start()].count("\n") + 1
            for pos, expr in hits:
                print(f"{f}:~{line}: {expr!r}")
    # 行级：单/双引号 f-string
    for lineno, line in enumerate(src.splitlines(), 1):
        for m in pat.finditer(line):
            hits = find_backslash_in_braces(m.group(2))
            if hits:
                for pos, expr in hits:
                    print(f"{f}:{lineno}: {expr!r}")
