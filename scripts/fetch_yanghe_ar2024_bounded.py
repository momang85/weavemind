# -*- coding: utf-8 -*-
"""第二小批：**仅资料获取**的受限验证——取洋河 2024 年报匹配原文（≤6 HTTP / ≤3 分钟）。

边界（按 09-22 晚间指令 §3 第二小批 1–2 与既有网络策略）：
- 走 `net_policy.fetch_document`（公网校验 + 按已验 IP 连接 + 不跟随重定向 + 字节上限 + 审计）；
- 固定主机与 art_code 形状校验；**最多 6 次 HTTP**、总时长 ≤180s、页间 1s；
- 不调用模型、不开研究任务、不绕过反爬；
- 产物记录：art_code、披露日、原始 URL、真实页码、每页正文字符数与**原始字节 hash**。

用法：python scripts/fetch_yanghe_ar2024_bounded.py
产物：evals/real/yanghe_ar2024_pages_20260922.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ART = "AN202504281664011244"                 # 洋河股份 2024 年年度报告（2025-04-29 披露）
HOST = "np-cnotice-stock.eastmoney.com"      # 固定主机
ART_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
PAGES = (2, 3, 4, 5, 10, 11)                 # 已知含 MD&A/行业/风险的页；**6 页 = 6 次请求**
MAX_HTTP = 6
MAX_SECONDS = 180.0
SLEEP_S = 1.0
OUT = ROOT / "evals" / "real" / "yanghe_ar2024_pages_20260922.json"


def _url_for(page: int) -> str:
    return (f"https://{HOST}/api/content/ann"
            f"?art_code={ART}&client_source=web&page_index={int(page)}")


def main() -> int:
    if not ART_RE.match(ART):
        print("art_code 形状非法", file=sys.stderr)
        return 2
    import net_policy
    t0 = time.time()
    pages: list[dict] = []
    http_count = 0
    published_at = ""
    for pg in PAGES:
        if http_count >= MAX_HTTP or (time.time() - t0) > MAX_SECONDS:
            print(f"已达上限（HTTP {http_count}，{time.time() - t0:.0f}s）：停止取页")
            break
        url = _url_for(pg)
        try:
            doc = net_policy.fetch_document(url, timeout=30, max_bytes=512 * 1024)
        except Exception as exc:                 # noqa: BLE001 - 失败如实记录，不换站重试
            pages.append({"page": pg, "url": url, "error": str(exc)[:200]})
            print(f"page {pg}: 失败 {str(exc)[:120]}")
            http_count += 1
            time.sleep(SLEEP_S)
            continue
        http_count += 1
        raw = doc.get("raw") or b""
        body = str(doc.get("text") or "")
        # 公告文本 API 返回 JSON 信封，页正文在 `data.notice_content`：只取正文，
        # 不把信封当原文（否则下游切分拿到的是一坨 JSON）
        text = body
        meta: dict = {}
        try:
            j = json.loads(body)
            data = (j or {}).get("data") or {}
            text = str(data.get("notice_content") or "")
            meta = {"notice_title": str(data.get("notice_title") or ""),
                    "notice_date": str(data.get("notice_date") or ""),
                    "page_size": data.get("page_size")}
        except Exception as exc:                 # noqa: BLE001 - 非 JSON 就按纯文本留
            print(f"page {pg}: 非 JSON 信封（{str(exc)[:60]}），按纯文本保存")
        pages.append({"page": pg, "url": url,
                      "raw_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
                      "raw_bytes": len(bytes(raw)),
                      "text_chars": len(text),
                      "text": text, **meta})
        if not published_at:
            cand = str(meta.get("notice_date") or "")
            m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", cand or text)
            if m:
                published_at = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        print(f"page {pg}: {len(text)} 字符，hash {pages[-1]['raw_sha256'][:12]}")
        time.sleep(SLEEP_S)
    payload = {
        "note": ("洋河股份 2024 年年度报告原文的有界取页（东财公告文本 API）。"
                 "每页记录原始 URL、真实页码、原始字节 hash 与正文；供离线准入/定位链复验。"),
        "company": "洋河股份", "company_id": "002304.SZ",
        "period": 2024, "doc_type": "年度报告",
        "art_code": ART, "published_at": published_at or "2025-04-29",
        "http_requests": http_count, "elapsed_sec": round(time.time() - t0, 1),
        "pages": pages, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT}（{http_count} 次 HTTP，{payload['elapsed_sec']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
