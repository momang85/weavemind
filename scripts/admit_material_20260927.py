# -*- coding: utf-8 -*-
"""把已保存的公告文本经**正常补材料入口**送进任务（C1）。

与 `admit_saved_material_20260923.py` 的区别：旧脚本直接写 `fetch_snapshot.json`
并调 `narrative_evidence.build`——绕过产品入口。本脚本走**摄取链**：

1. `material_intake.store(channel=local_file)`：保存原件（本地调试通道，与网页上传
   在材料记录里分开记），拿到材料 id（幂等键）；
2. 投递 `{"type": "add_material"}` 到 `orchestrator:main`——与网页提交同一条消息，
   由**编排器**取件/准入/并入资料快照/重做证据与结构/记待办；
3. 等 `material_ack:<material_id>` 收执并打印结果，再沿身份核对一遍
   （材料 id → 原件 hash → 正文 hash → 快照 hash → 证据定位）。

素材：洋河 2024 年年度报告原文的有界取件（东财公告文本 API，接口片段定位）。
冻结文本经 `merge_chunks` 合并并**保留片段偏移**，因此定位仍是 `api_chunk`，
不是 PDF 页码——它不是"已验 PDF"。

用法：python scripts/admit_material_20260927.py [task_id]
环境：与运行实例同一 Redis——端口按实例配置解析（REDIS_PORT 环境变量优先，
否则读 config.json 的 `redis.port`；不硬编码某个实例的选择）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ART = ROOT / "evals" / "real" / "yanghe_ar2024_pages_20260922.json"
TASK = sys.argv[1] if len(sys.argv) > 1 else ""
COMPANY, CODE, AS_OF = "洋河股份", "002304.SZ", "2025-04-30"
PERIODS = [2023, 2024]
ACK_TIMEOUT = float(os.environ.get("WM_MATERIAL_ACK_TIMEOUT", "180") or 180)


def redis_endpoint() -> tuple[str, int]:
    """实例的 Redis 端点：环境变量优先，否则读 config.json（与 launcher 同源）。"""
    host = os.environ.get("REDIS_HOST") or ""
    env_port = str(os.environ.get("REDIS_PORT") or "").strip()
    if host and env_port:
        return host, int(env_port)
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        rcfg = cfg.get("redis") or {}
    except Exception:                            # noqa: BLE001 - 读不到按默认
        rcfg = {}
    return (host or str(rcfg.get("host") or "127.0.0.1"),
            int(env_port or rcfg.get("port") or 6379))


def envelope_from_frozen() -> tuple[dict, bytes]:
    import narrative_evidence as ne
    data = json.loads(ART.read_text(encoding="utf-8"))
    chunks = [(int(p["api_chunk"]), str(p.get("text") or ""))
              for p in data["pages"] if p.get("text")]
    text, offsets, gaps = ne.merge_chunks(sorted(chunks, key=lambda x: x[0]))
    env = {
        "title": str(data.get("notice_title_meta") or "洋河股份:2024年年度报告"),
        "url": str(data["pages"][0]["url"]), "text": text,
        "chunk_offsets": offsets, "chunk_gaps": gaps,
        "chunk_size": int(data.get("chunk_size") or 5000),
        "location_kind": "api_chunk",
        "art_code": data.get("art_code"), "published_at": data.get("published_at"),
    }
    return env, json.dumps(env, ensure_ascii=False).encode("utf-8")


def main() -> int:
    if not TASK:
        print("用法：python scripts/admit_material_20260927.py <task_id>", file=sys.stderr)
        return 2
    import material_intake as mi
    env, raw = envelope_from_frozen()
    stored = mi.store(task_id=TASK, channel=mi.CHANNEL_LOCAL, raw=raw,
                      filename="yanghe_ar2024_api_chunks.json",
                      content_type="application/json",
                      title=env["title"], doc_type="年度报告")
    if not stored.get("ok"):
        print("材料保存失败：", stored.get("error"), file=sys.stderr)
        return 1
    mid = str(stored["material_id"])
    print(f"材料已保存：{mid}（{'已存在，复用' if stored.get('duplicate') else '新建'}，"
          f"{len(raw)} 字节，通道 {mi.CHANNEL_LOCAL}）")

    import redis as _redis
    from common import _NO_REDIS_RETRY
    host, port = redis_endpoint()
    r = _redis.Redis(host=host, port=port, decode_responses=True,
                     socket_connect_timeout=3, socket_timeout=5,
                     retry=_NO_REDIS_RETRY)
    ack_key = f"material_ack:{mid}"
    try:
        # 先登记待办再投递：投递失败时"这份材料还没摄取"这件事仍留在台账里，
        # 编排器下次启动会补做（与网页入口同一顺序）
        r.sadd("material_pending_tasks", TASK)
        r.delete(ack_key)
        r.publish("orchestrator:main", json.dumps(
            {"type": "add_material", "task_id": TASK, "material_id": mid,
             "channel": mi.CHANNEL_LOCAL, "user_id": "local-script"},
            ensure_ascii=False))
    except Exception as exc:                     # noqa: BLE001
        print(f"投递失败（编排器/Redis 不可达 {host}:{port}）：{exc}", file=sys.stderr)
        return 1
    print(f"已投递给编排器（{host}:{port}），等收执…")
    deadline = time.time() + ACK_TIMEOUT
    result = None
    while time.time() < deadline:
        got = r.get(ack_key)
        if got:
            result = json.loads(got)
            break
        time.sleep(0.5)
    if not isinstance(result, dict):
        print(f"{ACK_TIMEOUT:.0f}s 内没有收到收执；原件已保存，编排器恢复后会自动摄取",
              file=sys.stderr)
        return 1

    print(f"摄取结论：status={result.get('status')} ok={result.get('ok')}")
    if not result.get("ok"):
        print(f"  原因：{result.get('reason') or result.get('detail') or ''}")
        return 1
    verdict = result.get("verdict") or {}
    print(f"  来源类别：{(verdict.get('doc') or {}).get('source_class')}"
          f"（{verdict.get('provenance_label')}）")
    print(f"  披露日：{(verdict.get('cutoff') or {}).get('disclosed_at')}"
          f"（精度 {(verdict.get('cutoff') or {}).get('precision')}，"
          f"依据 {(verdict.get('cutoff') or {}).get('basis')}）")
    print(f"  小节 {verdict.get('section_count')} 个，证据 {len(verdict.get('evidence') or [])} 条")
    for k, v in (verdict.get("metric_states") or {}).items():
        print(f"  逐指标 {k}：{v.get('label')}")
    scope = verdict.get("read_scope") or {}
    print(f"  查阅范围：正文 {scope.get('text_chars')} 字，已解析 {len(scope.get('parsed_ranges') or [])} 段，"
          f"未解析 {len(scope.get('unparsed_ranges') or [])} 段")

    # 沿身份核对：材料 → 快照 → 证据
    import hashlib
    import narrative_evidence as ne
    import workspace as ws_mod
    proj = Path(ws_mod.task_project_dir(TASK))
    snap_file = proj / "fetch_snapshot.json"
    snap = json.loads(snap_file.read_text(encoding="utf-8"))
    doc = next((d for d in snap if str(d.get("material_id") or "") == mid), None)
    if doc is None:
        print("  核对失败：快照里找不到这份材料", file=sys.stderr)
        return 1
    ev = ne.read(TASK) or {}
    print(f"  快照：{len(snap)} 篇；原件 sha256 {str(doc.get('raw_sha256'))[:16]}…，"
          f"正文 sha256 {str(doc.get('text_sha256'))[:16]}…，解析版本 {doc.get('parser_version')}，"
          f"准入规则 {doc.get('admission_rules')}")
    print(f"  证据：snapshot_sha256="
          f"{str(ev.get('snapshot_sha256'))[:16]}…（磁盘复算 "
          f"{hashlib.sha256(snap_file.read_bytes()).hexdigest()[:16]}…）")
    located = [x for x in (ev.get("records") or []) if x.get("has_location")]
    print(f"  可定位记录 {len(located)} 条；样例定位："
          f"{str((located[0] if located else {}).get('locator'))[:90]}")
    refresh = result.get("refresh") or {}
    print(f"  重做：证据 {refresh.get('evidence')}；结构 {(refresh.get('structure') or {}).get('version_id', '')[:12]}")
    for p in refresh.get("pending") or []:
        print(f"  待办：{p.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
