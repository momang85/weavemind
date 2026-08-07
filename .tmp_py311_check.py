# -*- coding: utf-8 -*-
"""用 ast.feature_version 模拟 Python 3.11 语法兼容性检查（复现 CI py_compile 失败）。"""
import ast
import subprocess

files = subprocess.check_output(
    ["git", "ls-files", "*.py"], text=True
).splitlines()
failed = []
for f in files:
    with open(f, "r", encoding="utf-8") as fh:
        src = fh.read()
    try:
        ast.parse(src, feature_version=(3, 11))
    except SyntaxError as e:
        failed.append((f, e.lineno, e.msg))
print("checked", len(files), "files")
for f, lineno, msg in failed:
    print("FAIL:", f, "line", lineno, msg)
if not failed:
    print("ALL OK under 3.11 grammar")
