# -*- coding: utf-8 -*-
"""RC2 独立试用包打包器（DATA-RC2-1024 阶段 C4）。

- 显式白名单（不整仓打包）：根级 *.py、指定代码目录、frontend/dist、
  requirements.txt / start.bat / launcher.py / README.md、生成的合成样例。
- 默认**不含**：真实财务缓存 data/screen_cache/*、config.json、credentials.json、
  .env、agents.db、*.log、tmp/、workspace/（数据目录）、docs/、node_modules/、
  frontend/src/、_trial_*/_dbg_*/_probe*/probe_*/_l3_probe* 调试脚本、__pycache__。
- data/ 下**只放**合成样例 data/samples/synthetic_5_companies.json
  （5 家虚构公司，metadata.synthetic=true，来源 URL 为 about:synthetic-demo）。
- 生成 BUILD.json（build_id=时间戳+短哈希、Python 版本、命令、文件数、总字节）
  与 MANIFEST.sha256.json（逐文件 sha256+bytes；覆盖包内除清单文件自身外的全部文件）。
- 自检：写包后逐文件复核哈希与覆盖面；任何不一致 → exit 1。
  `--verify <zip>` 对既有包做同样的完整性自检（退出码 0/非 0）。

用法：
  C:/Python314/python.exe tools/build_rc2_package.py --out tmp/rc2-package
  C:/Python314/python.exe tools/build_rc2_package.py --verify tmp/rc2-package/weavemind-rc2-<build_id>.zip

密钥纪律：本工具只复制白名单源码，不读取、不打包任何凭据文件；不打印任何秘密。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import sys
import zipfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_PREFIX = "weavemind"          # zip 内顶层目录
MANIFEST_NAME = "MANIFEST.sha256.json"
BUILD_NAME = "BUILD.json"
SAMPLE_PATH = "data/samples/synthetic_5_companies.json"

# ---- 白名单 ----
ROOT_PY_EXCLUDE_PREFIXES = ("_trial_", "_dbg_", "_probe", "probe_", "_l3_probe")
ROOT_FILES = ("requirements.txt", "start.bat", "launcher.py", "README.md")
CODE_DIRS = (
    "adapters", "charts_pipeline", "evals", "examples", "financial_analysis",
    "prompts", "quant_research", "risk_research", "skills", "structured_pipeline",
    "validators", "workers",
)
FRONTEND_DIST = "frontend/dist"

FORBIDDEN_PARTS = ("screen_cache", "__pycache__", "node_modules", ".git")
FORBIDDEN_NAMES = ("config.json", "credentials.json", ".env", "agents.db",
                   "agents.db-wal", "agents.db-shm")
FORBIDDEN_SUFFIXES = (".log", ".pyc", ".pyo", ".db-shm", ".db-wal")

# ---- 合成样例（5 家虚构公司 × 2 个年度期间；金额为构造整数，单位元）----
SYNTHETIC_COMPANIES = (
    # (entity, code, rev23, gross23, np23, ocf23, ta23, tl23, rev24, gross24, np24, ocf24, ta24, tl24)
    ("幻光重工股份有限公司（虚构演示）", "690001.SH",
     9_800_000_000, 2_400_000_000, 950_000_000, 1_300_000_000, 22_000_000_000, 13_000_000_000,
     10_500_000_000, 2_700_000_000, 1_100_000_000, 1_500_000_000, 24_000_000_000, 13_500_000_000),
    ("星桥新能源股份有限公司（虚构演示）", "690002.SH",
     3_200_000_000, 950_000_000, 310_000_000, 280_000_000, 8_000_000_000, 4_300_000_000,
     4_100_000_000, 1_250_000_000, 440_000_000, 400_000_000, 9_200_000_000, 4_600_000_000),
    ("青禾生物科技股份有限公司（虚构演示）", "690003.SH",
     1_600_000_000, 1_050_000_000, 220_000_000, 150_000_000, 5_500_000_000, 2_400_000_000,
     1_550_000_000, 1_000_000_000, 180_000_000, 190_000_000, 5_900_000_000, 2_700_000_000),
    ("云帆消费股份有限公司（虚构演示）", "690004.SH",
     6_700_000_000, 2_500_000_000, 540_000_000, 720_000_000, 11_000_000_000, 7_200_000_000,
     7_100_000_000, 2_750_000_000, 610_000_000, 680_000_000, 11_500_000_000, 7_600_000_000),
    ("磐石机械股份有限公司（虚构演示）", "690005.SH",
     4_400_000_000, 1_300_000_000, 360_000_000, 330_000_000, 9_600_000_000, 4_200_000_000,
     4_600_000_000, 1_450_000_000, 390_000_000, 290_000_000, 10_000_000_000, 4_500_000_000),
)
PERIODS = (("2023-12-31", "2024-04-25"), ("2024-12-31", "2025-04-25"))
SOURCE_URL = "about:synthetic-demo"


def synthetic_sample() -> dict:
    """5 家虚构公司的合成演示宽表（metadata.synthetic=true；标签始终跟随数据）。"""
    rows = []
    for (entity, code, rev23, gross23, np23, ocf23, ta23, tl23,
         rev24, gross24, np24, ocf24, ta24, tl24) in SYNTHETIC_COMPANIES:
        for (period, avail), (rev, gross, np_, ocf, ta, tl) in zip(
                PERIODS, ((rev23, gross23, np23, ocf23, ta23, tl23),
                          (rev24, gross24, np24, ocf24, ta24, tl24))):
            rows.append({
                "entity": entity,
                "code": code,
                "period": period,
                "unit": "元",
                "currency": "CNY",
                "caliber": "合并",
                "period_type": "年报",
                "revenue": rev,
                "operating_cost": rev - gross,
                "gross_profit": gross,
                "net_profit": np_,
                "net_profit_consolidated": int(np_ * 1.04 // 1_000_000 * 1_000_000),
                "operating_cashflow": ocf,
                "rd_expense": int(rev * 0.045 // 1_000_000 * 1_000_000),
                "total_assets": ta,
                "total_liabilities": tl,
                "accounts_receivable": int(rev * 0.18 // 1_000_000 * 1_000_000),
                "inventory": int(rev * 0.12 // 1_000_000 * 1_000_000),
                "accounts_payable": int(rev * 0.14 // 1_000_000 * 1_000_000),
                "admin_expense": int(rev * 0.05 // 1_000_000 * 1_000_000),
                "selling_expense": int(rev * 0.07 // 1_000_000 * 1_000_000),
                "taxes_and_surcharges": int(rev * 0.008 // 1_000_000 * 1_000_000),
                "available_at": avail,
                "disclosed_at": avail,
                "source_url": SOURCE_URL,
            })
    return {
        "metadata": {
            "source": "synthetic-rc2-demo",
            "synthetic": True,
            "user_provided": False,
            "note": "5 家虚构公司的合成演示数据：全部金额为构造值，不是任何真实上市公司的"
                    "财务数据，也不是官方缓存。标签「合成演示」会随数据始终展示。",
            "license": "synthetic-demo-only",
            "generator": "tools/build_rc2_package.py",
        },
        "rows": rows,
    }


def is_forbidden(zip_path: str) -> str | None:
    """包内相对路径（weavimind/…）命中禁入规则 → 返回原因。"""
    rel = zip_path[len(PACKAGE_PREFIX) + 1:] if zip_path.startswith(PACKAGE_PREFIX + "/") else zip_path
    if rel.startswith("frontend/src/"):
        return "forbidden frontend source"
    parts = rel.split("/")
    if parts[0] == "evals":
        # evals 运行时数据不进包（Lead 复核决定）：cases/claims/brief/临时目录/报告
        if len(parts) > 2 or parts[1] in ("cases", "claims", "brief") \
                or parts[1] == "calibration_report.json" or not parts[1].endswith(".py"):
            return "forbidden evals runtime data: %s" % "/".join(parts[:2])
    for p in parts[:-1]:
        if p in FORBIDDEN_PARTS or p in ("tmp", "docs", "workspace"):
            return "forbidden dir: %s" % p
    name = parts[-1]
    if name in FORBIDDEN_NAMES:
        return "forbidden file: %s" % name
    if name.endswith(FORBIDDEN_SUFFIXES):
        return "forbidden suffix: %s" % name
    for pref in ROOT_PY_EXCLUDE_PREFIXES:
        if name.startswith(pref):
            return "forbidden debug script: %s" % name
    if name.startswith(".env"):
        return "forbidden env file"
    return None


def collect_files() -> list[tuple[str, str]]:
    """白名单收集 → [(zip 内路径, 磁盘绝对路径)]（不含 BUILD.json/MANIFEST/样例）。"""
    out: list[tuple[str, str]] = []
    seen = set()

    def add(abs_path: str, rel: str):
        zp = "%s/%s" % (PACKAGE_PREFIX, rel.replace("\\", "/"))
        if zp in seen:
            return
        reason = is_forbidden(zp)
        if reason:
            raise SystemExit("白名单文件命中禁入规则（%s）：%s" % (reason, rel))
        seen.add(zp)
        out.append((zp, abs_path))

    for name in ROOT_FILES:
        p = os.path.join(REPO, name)
        if not os.path.isfile(p):
            raise SystemExit("缺少必需文件：%s" % name)
        add(p, name)

    for f in sorted(os.listdir(REPO)):
        p = os.path.join(REPO, f)
        if f.endswith(".py") and os.path.isfile(p):
            if any(f.startswith(pref) for pref in ROOT_PY_EXCLUDE_PREFIXES):
                continue
            add(p, f)

    for d in CODE_DIRS:
        base = os.path.join(REPO, d)
        if not os.path.isdir(base):
            raise SystemExit("缺少必需目录：%s" % d)
        if d in ("evals", "validators"):
            # Lead 复核决定（DATA-RC2-1024）：orchestrator_v2/web_ui 只按需懒加载
            # 这两个包的 .py 源码；evals/ 下的 cases/claims/brief/临时目录与
            # calibration_report.json 是实例运行时数据（准入经验/评审记录），
            # 不是源码，**不得进试用包**——只收顶层 *.py。
            for fn in sorted(os.listdir(base)):
                p = os.path.join(base, fn)
                if not os.path.isfile(p) or not fn.endswith(".py"):
                    continue
                if any(fn.startswith(pref) for pref in ROOT_PY_EXCLUDE_PREFIXES):
                    continue
                add(p, os.path.join(d, fn))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if x not in FORBIDDEN_PARTS and not x.startswith(".")]
            for fn in sorted(filenames):
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, REPO)
                if fn.endswith(FORBIDDEN_SUFFIXES) or fn in FORBIDDEN_NAMES:
                    continue
                if any(fn.startswith(pref) for pref in ROOT_PY_EXCLUDE_PREFIXES):
                    continue
                add(p, rel)

    dist = os.path.join(REPO, FRONTEND_DIST)
    idx = os.path.join(dist, "index.html")
    if not os.path.isfile(idx):
        raise SystemExit("缺少默认前端入口：%s（先在 frontend/ 下执行 npm run build）" % idx)
    for dirpath, dirnames, filenames in os.walk(dist):
        dirnames[:] = [x for x in dirnames if x not in FORBIDDEN_PARTS]
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, REPO)
            if fn.endswith(FORBIDDEN_SUFFIXES):
                continue
            add(p, rel)
    return out


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build(out_dir: str) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    files = collect_files()
    sample = synthetic_sample()
    sample_bytes = json.dumps(sample, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")

    entries: list[tuple[str, bytes]] = [(zp, open(ap, "rb").read()) for zp, ap in files]
    entries.append(("%s/%s" % (PACKAGE_PREFIX, SAMPLE_PATH), sample_bytes))

    file_list_digest = sha256_bytes(
        "\n".join("%s %s" % (zp, sha256_bytes(b)) for zp, b in sorted(entries)).encode("utf-8"))
    build_id = "%s-%s" % (stamp, file_list_digest[:8])

    build_json = {
        "schema": "weavemind.rc2.build/1",
        "build_id": build_id,
        "created_at_utc": stamp,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "command": "python tools/build_rc2_package.py --out %s" % out_dir,
        "package_root": PACKAGE_PREFIX,
        "file_count": len(entries) + 2,   # + BUILD.json + MANIFEST.sha256.json
        "total_bytes": 0,                 # 定点迭代填充（BUILD/MANIFEST 自身字节计入）
        "manifest": MANIFEST_NAME,
        "manifest_covers": "包内除 MANIFEST.sha256.json 自身外的全部文件（含 BUILD.json）",
        "synthetic_sample": {
            "path": SAMPLE_PATH,
            "synthetic": True,
            "companies": len(SYNTHETIC_COMPANIES),
            "rows": len(sample["rows"]),
            "source_url": SOURCE_URL,
        },
        "excluded": [
            "data/screen_cache/*（真实缓存）", "config.json", "credentials.json", ".env",
            "agents.db", "*.log", "tmp/", "workspace/（数据目录，保留 workspace.py 代码）",
            "docs/", "node_modules/", "frontend/src/", "_trial_*/_dbg_*/_probe*/probe_*/_l3_probe*",
            "__pycache__", "frontend/dist-screen-check/",
            "evals/cases|claims|brief|临时目录与 calibration_report.json（实例运行时数据，只收 evals 顶层 .py）",
        ],
        "data_dir_contents": "仅 " + SAMPLE_PATH + "（合成样例）",
    }

    # 定点迭代：total_bytes 计入 BUILD.json 与 MANIFEST 自身的字节数，
    # BUILD.json 哈希进 manifest → 两者互相影响，迭代到长度收敛（通常 2-3 轮）。
    build_bytes = manifest_bytes = b""
    total = 0
    for _ in range(8):
        build_json["total_bytes"] = total
        build_bytes = json.dumps(build_json, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
        files_map = {zp: {"sha256": sha256_bytes(b), "bytes": len(b)}
                     for zp, b in sorted(entries)}
        bp = "%s/%s" % (PACKAGE_PREFIX, BUILD_NAME)
        files_map[bp] = {"sha256": sha256_bytes(build_bytes), "bytes": len(build_bytes)}
        manifest = {
            "schema": "weavemind.rc2.manifest/1",
            "build_id": build_id,
            "package_root": PACKAGE_PREFIX,
            "note": "本清单覆盖包内除 MANIFEST.sha256.json 自身外的全部文件（含 BUILD.json）；"
                    "--verify 逐文件复核 sha256 与字节数，任何不一致 exit 1。",
            "total_files": len(files_map) + 1,   # +1 = MANIFEST 自身
            "files": files_map,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
        new_total = sum(len(b) for _, b in entries) + len(build_bytes) + len(manifest_bytes)
        if new_total == total:
            break
        total = new_total
    else:
        raise SystemExit("total_bytes 定点迭代未收敛")

    all_entries = entries + [
        ("%s/%s" % (PACKAGE_PREFIX, BUILD_NAME), build_bytes),
        ("%s/%s" % (PACKAGE_PREFIX, MANIFEST_NAME), manifest_bytes),
    ]

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "weavemind-rc2-%s.zip" % build_id)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for zp, b in all_entries:
            zi = zipfile.ZipInfo(zp, date_time=(2026, 10, 24, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, b)

    # ---- 写包后自检：从磁盘重新读 zip，逐文件复核 ----
    problems = verify_zip(zip_path)
    if problems:
        print("BUILD 自检失败：")
        for p in problems:
            print("  - %s" % p)
        return 1
    zsha = sha256_bytes(open(zip_path, "rb").read())
    print("BUILD OK")
    print("zip: %s" % zip_path)
    print("zip_sha256: %s" % zsha)
    print("build_id: %s" % build_id)
    print("files: %d total_bytes: %d" % (build_json["file_count"], build_json["total_bytes"]))
    print("sample: %s（%d 家虚构公司，%d 行，source_url=%s）"
          % (SAMPLE_PATH, len(SYNTHETIC_COMPANIES), len(sample["rows"]), SOURCE_URL))
    print("verify: python tools/build_rc2_package.py --verify %s" % zip_path)
    return 0


def verify_zip(zip_path: str) -> list[str]:
    """完整性自检：清单覆盖全部文件 + 逐文件哈希/字节数一致 + 无禁入路径。"""
    problems: list[str] = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as exc:
        return ["无法打开 zip：%s" % exc]
    names = [n for n in zf.namelist() if not n.endswith("/")]
    try:
        manifest = json.loads(zf.read("%s/%s" % (PACKAGE_PREFIX, MANIFEST_NAME)).decode("utf-8"))
    except KeyError:
        return ["包内缺少 %s/%s" % (PACKAGE_PREFIX, MANIFEST_NAME)]
    mfiles = manifest.get("files") or {}
    mset = set(mfiles)
    zset = set(names)
    for n in sorted(zset - mset - {"%s/%s" % (PACKAGE_PREFIX, MANIFEST_NAME)}):
        problems.append("清单未覆盖的文件：%s" % n)
    for n in sorted(mset - zset):
        problems.append("清单声明但包内缺失：%s" % n)
    for n in sorted(zset & mset):
        data = zf.read(n)
        meta = mfiles[n]
        if meta.get("bytes") != len(data):
            problems.append("字节数不一致：%s（manifest=%s 实际=%d）" % (n, meta.get("bytes"), len(data)))
        h = sha256_bytes(data)
        if str(meta.get("sha256") or "").lower() != h:
            problems.append("sha256 不一致：%s" % n)
        reason = is_forbidden(n)
        if reason:
            problems.append("禁入内容：%s（%s）" % (n, reason))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="RC2 独立试用包打包器")
    ap.add_argument("--out", help="输出目录（生成 weavemind-rc2-<build_id>.zip）")
    ap.add_argument("--verify", help="对既有 zip 做完整性自检（退出码 0/非 0）")
    args = ap.parse_args()
    if args.verify:
        problems = verify_zip(os.path.abspath(args.verify))
        if problems:
            print("VERIFY FAIL（%d 项）" % len(problems))
            for p in problems:
                print("  - %s" % p)
            return 1
        zf = zipfile.ZipFile(args.verify)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        manifest = json.loads(zf.read("%s/%s" % (PACKAGE_PREFIX, MANIFEST_NAME)).decode("utf-8"))
        zsha = sha256_bytes(open(args.verify, "rb").read())
        print("VERIFY OK")
        print("zip: %s" % os.path.abspath(args.verify))
        print("zip_sha256: %s" % zsha)
        print("build_id: %s" % manifest.get("build_id"))
        print("files: %d（manifest 覆盖 %d 项 + 清单自身，逐文件 sha256 一致，无禁入内容）"
              % (len(names), len(manifest.get("files") or {})))
        return 0
    if not args.out:
        ap.error("需要 --out <目录> 或 --verify <zip>")
    return build(os.path.abspath(args.out))


if __name__ == "__main__":
    sys.exit(main())
