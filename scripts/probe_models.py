# -*- coding: utf-8 -*-
"""基元律动（tokenrhythm）模型能力探测——按项目角色选型。

用法：python scripts/probe_models.py [--apply] [--skip-long]
- 默认只输出能力矩阵与推荐（docs 文档）；
- --apply 把推荐自动写入 config.json（llm.model / planner / backup / llm.model_roles），
  写前备份到 <config.json>.bak；
- --skip-long 跳过长文生成项（加速复跑）。

电池（每模型顺序执行）：
1. 短答：max_tokens=100 测延迟；
2. thinking 行为：响应是否含 reasoning_content / content 空 / finish_reason（识别思考耗尽）；
3. JSON 合规：要求输出 {"a":1}，用 common.extract_json_object 校验（planner 硬需求）；
4. 长文：max_tokens=1500 测时长与产出长度。

选型规则按项目角色：
- planner：JSON 合规且短答快；
- exec：长文快且稳定；
- judge：JSON 合规（评分输出）；
- quick：短答快且无思考燃烧；
- backup：与主模型不同系列（不同失效模式）。

安全约束：base_url 只允许 https 公网地址（私网/环回/链路本地阻断）。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
DOCS_NAME = "模型能力探测_20260907.md"

SHORT_QUESTION = "只回复两个字：收到"
JSON_QUESTION = '只输出一个 JSON 对象，格式 {"a": 1}，不要任何其他文字'
LONG_QUESTION = (
    "写一篇 800 字左右的行业观察短文，主题：中国固态电池产业 2026 年现状，"
    "分三段：技术路线、主要玩家、量产挑战。直接输出正文。"
)


def _validate_base_url(base_url: str) -> str:
    """探测只允许访问配置中的 HTTPS 服务商地址：
    协议必须 https、主机名非空、不含 userinfo/query/片段；
    解析后的 IP 必须是公网地址（阻断私网/环回/链路本地）。"""
    parsed = urllib.parse.urlsplit(str(base_url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"base_url 必须是 https://host 形式: {base_url!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url 不允许包含 userinfo/query/片段")
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"base_url 主机无法解析: {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"base_url 解析到非公网地址: {ip}")
    return parsed.scheme + "://" + parsed.hostname + (
        (":" + str(parsed.port)) if parsed.port else ""
    ) + (parsed.path.rstrip("/") or "")


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _list_models(base_url: str, api_key: str) -> list[str]:
    base = _validate_base_url(base_url)
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + api_key},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    return [m.get("id") for m in (d.get("data") or []) if m.get("id")]


def _chat(base_url: str, api_key: str, model: str, question: str,
          max_tokens: int, timeout: int = 300) -> dict:
    """单次 chat 调用；返回原始响应 JSON（失败抛异常）。"""
    base = _validate_base_url(base_url)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=data,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe_model(base_url: str, api_key: str, model: str,
                skip_long: bool) -> dict:
    out: dict = {"model": model, "ok": False, "err": ""}
    # 1) 短答
    try:
        t0 = time.time()
        d = _chat(base_url, api_key, model, SHORT_QUESTION, 100)
        out["short_s"] = round(time.time() - t0, 1)
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        content = str(msg.get("content") or "").strip()
        reasoning = str(msg.get("reasoning_content") or "").strip()
        finish = (d.get("choices") or [{}])[0].get("finish_reason") or ""
        out["content"] = content[:80]
        out["has_reasoning"] = bool(reasoning)
        out["finish_reason"] = finish
        out["thinking_burn"] = (not content) and bool(reasoning) and finish == "length"
        out["ok"] = bool(content)
    except Exception as exc:
        out["err"] = f"short: {type(exc).__name__} {str(exc)[:80]}"
        return out
    # 2) JSON 合规
    try:
        from common import extract_json_object
        d = _chat(base_url, api_key, model, JSON_QUESTION, 300)
        c = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        parsed = extract_json_object(c)
        out["json_ok"] = isinstance(parsed, dict) and parsed.get("a") == 1
    except Exception:
        out["json_ok"] = False
    # 3) 长文
    if not skip_long:
        try:
            t0 = time.time()
            d = _chat(base_url, api_key, model, LONG_QUESTION, 1500, timeout=600)
            c = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            out["long_s"] = round(time.time() - t0, 1)
            out["long_chars"] = len(str(c).strip())
            out["long_ok"] = len(str(c).strip()) >= 300
        except Exception as exc:
            out["long_s"] = -1
            out["long_ok"] = False
            out["long_err"] = f"{type(exc).__name__} {str(exc)[:80]}"
    return out


def _score(row: dict) -> tuple[int, str]:
    """选型打分：返回 (分数, 备注)。"""
    score = 0
    notes = []
    if row.get("ok"):
        score += 2
        if row.get("short_s", 999) <= 15:
            score += 2
        elif row.get("short_s", 999) <= 40:
            score += 1
    else:
        notes.append("短答失败" + (f": {row['err'][:40]}" if row.get("err") else ""))
    if row.get("json_ok"):
        score += 3
    else:
        notes.append("JSON 不合规")
    if row.get("thinking_burn"):
        score -= 3
        notes.append("思考耗尽(空content)")
    if "long_ok" in row:
        if row.get("long_ok"):
            score += 2
        else:
            notes.append("长文不足" + (f": {row.get('long_err','')[:40]}" if row.get("long_err") else ""))
    row["_score"] = score
    row["_notes"] = "；".join(notes)
    return score, "；".join(notes)


def pick_models(results: list[dict]) -> dict:
    """按角色从结果中选型。"""
    rows = [r for r in results if r.get("ok") and not r.get("thinking_burn")]
    scored = sorted(rows, key=lambda r: (-r.get("_score", 0), r.get("short_s", 999)))
    fast = [r for r in rows if r.get("short_s", 999) <= 15]
    long_ok = [r for r in rows if r.get("long_ok")]

    def _first(cands):
        return cands[0]["model"] if cands else ""

    planner = _first([r for r in scored if r.get("json_ok")])
    exec_m = _first(sorted(long_ok, key=lambda r: (r.get("long_s", 999), r.get("short_s", 999))))
    judge = _first([r for r in scored if r.get("json_ok") and r["model"] != planner])
    quick = _first(sorted(fast, key=lambda r: r.get("short_s", 999)))
    # backup 与主模型不同系列（失效模式差异）：排除同前缀
    main_prefix = exec_m.split("-")[0] if exec_m else ""
    backup_cands = [r for r in scored if r["model"] != exec_m
                    and not r["model"].startswith(main_prefix + "-")]
    backup = _first(backup_cands)
    return {
        "planner": planner or exec_m,
        "exec": exec_m,
        "judge": judge or planner or exec_m,
        "quick": quick or planner or exec_m,
        "backup": backup or (scored[1]["model"] if len(scored) > 1 else exec_m),
    }


def write_matrix(results: list[dict], picks: dict) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    docs_path = os.path.normpath(os.path.join(DOCS_DIR, DOCS_NAME))
    if not docs_path.startswith(os.path.normpath(REPO_ROOT) + os.sep):
        raise ValueError("docs 输出路径逃逸仓库目录")
    lines = [
        "# 基元律动模型能力探测报告（2026-09-07）",
        "",
        "探测工具：`scripts/probe_models.py`（短答延迟 / thinking 行为 / JSON 合规 / 长文生成）。",
        "",
        "| 模型 | 短答 | 思考耗尽 | JSON | 长文 | 综合分 | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: -x.get("_score", -99)):
        lines.append(
            f"| {r['model']} | {r.get('short_s', '-')}s | {'⚠️' if r.get('thinking_burn') else '-'} "
            f"| {'✅' if r.get('json_ok') else '❌'} "
            f"| {r.get('long_chars', '-')}字/{r.get('long_s', '-')}s | {r.get('_score', '-')} "
            f"| {r.get('_notes') or ''} |"
        )
    lines += [
        "",
        "## 选型结论",
        f"- 主模型（exec/长文）：`{picks['exec']}`",
        f"- 规划器（planner，JSON 合规）：`{picks['planner']}`",
        f"- 评测（judge）：`{picks['judge']}`",
        f"- 快答（quick）：`{picks['quick']}`",
        f"- 备份（backup，不同系列）：`{picks['backup']}`",
        "",
        "选型依据：planner 要求 JSON 合规且快；exec 要求长文快且稳定；",
        "quick 要求短答最快且无思考燃烧（qwen3.7-flash 类思考耗尽模型已排除）；",
        "backup 选择与主模型不同系列的模型（不同失效模式）。",
        "",
    ]
    with open(docs_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"矩阵已写入 {docs_path}")


def apply_config(picks: dict, base_url: str, api_key: str) -> None:
    cfg = _load_config()
    backup_path = CONFIG_PATH + ".bak"
    import shutil
    shutil.copy(CONFIG_PATH, backup_path)
    cfg["llm"] = {"api_key": api_key, "base_url": base_url, "model": picks["exec"]}
    cfg["planner"] = {"model": picks["planner"], "base_url": base_url, "api_key": api_key}
    cfg["backup"] = {"base_url": base_url, "api_key": api_key, "model": picks["backup"]}
    cfg["llm"]["model_roles"] = {
        "planner": picks["planner"],
        "exec": picks["exec"],
        "judge": picks["judge"],
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"config.json 已更新（备份 {backup_path}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="基元律动模型能力探测")
    ap.add_argument("--apply", action="store_true", help="自动写入 config.json")
    ap.add_argument("--skip-long", action="store_true", help="跳过长文生成项")
    args = ap.parse_args()

    cfg = _load_config()
    llm = cfg.get("llm") or {}
    base_url = str(llm.get("base_url") or "https://tokenrhythm.studio/v1")
    api_key = str(llm.get("api_key") or "")
    if not api_key:
        print("config.json 缺 llm.api_key")
        return 1

    models = _list_models(base_url, api_key)
    print(f"发现 {len(models)} 个模型：{', '.join(models)}")
    results = []
    for m in models:
        print(f"[{m}] 探测中...", flush=True)
        row = probe_model(base_url, api_key, m, args.skip_long)
        score, notes = _score(row)
        print(f"  -> 短答 {row.get('short_s','-')}s | 思考耗尽 {row.get('thinking_burn')} "
              f"| JSON {row.get('json_ok')} | 长文 {row.get('long_chars','-')}字 "
              f"| 分 {score} | {notes}")
        results.append(row)

    picks = pick_models(results)
    print("\n== 选型结论 ==")
    for role, model in picks.items():
        print(f"  {role}: {model}")
    write_matrix(results, picks)
    if args.apply:
        apply_config(picks, base_url, api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
