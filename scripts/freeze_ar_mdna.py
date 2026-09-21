# -*- coding: utf-8 -*-
"""D3：有界补取真实年报的"管理层讨论与分析"片段，冻结为离线正例。

为什么需要：现有 `evals/real/yanghe_ar2024_excerpt.json` 摘的页码（2/3/5/10/64）
不含**带因果语言**的 MD&A 正文，于是"真实经营讨论进入对应解释"这条正向分支
一直没有真实材料可证（合成正例不算数）。

边界（按阶段 D 指令 + 本仓网络策略）：
- 抓取走 `net_policy.fetch_document`：严格公网校验 + **按已验 IP 连接** + 不跟随重定向
  + 字节上限 + 审计记录；脚本自身不直接发起 HTTP；
- 主机固定、`art_code` 只接受编号形状（不接受任意 URL）；限定页数与速率；
- 不绕过反爬/付费墙；不调用模型；
- 产物带 art_code/披露日/页码/正文 hash，便于离线复验。

用法：python scripts/freeze_ar_mdna.py [art_code]
产物：evals/real/yanghe_ar2024_mdna_excerpt.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("WM_CASE_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

DEFAULT_ART = "AN202504281664011244"          # 洋河股份 2024 年年度报告（2025-04-29 披露）
HOST = "np-cnotice-stock.eastmoney.com"       # 固定主机（不拼接任意域名）
ART_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
MAX_PAGES = 16                                # 有界：只看报告前 16 页（第三节通常在此范围内）
SLEEP_S = 1.0
MAX_EXCERPT_CHARS = 6000
# 选句判据用**生产同款** `narrative_evidence.is_causal`（不自造标记表，避免两处口径漂移）；
# 另要求句子谈到核心指标——只有"说了为什么"且"说的是哪个指标"的句子才是变化解释。
TOPIC = ("营业收入", "归属于上市公司股东的净利润", "净利润", "毛利率",
         "经营活动产生的现金流量净额")


def _url_for(art: str, page: int) -> str:
    return (f"https://{HOST}/api/content/ann"
            f"?art_code={art}&client_source=web&page_index={int(page)}")


def _fetch_json(url: str) -> dict:
    """经网络策略抓取（公网校验 + 按已验 IP 连接 + 不跟随重定向），解析为 JSON。"""
    import net_policy
    doc = net_policy.fetch_document(url, timeout=30, max_bytes=512 * 1024)
    return json.loads(str(doc.get("text") or ""))


def _sentences_with_causality(text: str) -> list[str]:
    """按句取"谈到指标 + 说出原因"的句子（保留原句，不改写）。

    判据与生产一致：`narrative_evidence.is_causal`（政策套话/仅复述数字不算），
    并要求句子谈到核心指标——否则"说了原因但没说哪个指标"的句子对不上解释对象。
    另加 `is_policy_text` 守卫：披露规则/准则说明类句子也会命中"影响"这类词
    （实测第 71 页的"非经常性损益界定说明"），但它们不是经营变化解释。
    """
    import narrative_evidence as ne
    out: list[str] = []
    for raw in re.split(r"(?<=[。；])", str(text or "")):
        s = " ".join(raw.split())
        if len(s) < 20 or len(s) > 400:
            continue
        if not any(t in s for t in TOPIC):
            continue
        if not ne.is_causal(s) or ne.is_policy_text(s):
            continue
        out.append(s)
    return out


def _target_sentences(text: str) -> list[str]:
    """发行人自己披露的**经营目标句**（目标/计划/力争 + 数值或区间）。

    目标类判断的正向样本必须来自发行人原文（D1 未验项）："力争营业收入同比增长 5%-10%"
    这类句子要能连同披露日与页码一起冻结，供目标分支核对目标年度/目标值/披露时点。
    """
    out: list[str] = []
    for raw in re.split(r"(?<=[。；])", str(text or "")):
        s = " ".join(raw.split())
        if len(s) < 12 or len(s) > 300:
            continue
        if not any(k in s for k in ("目标", "计划", "力争", "预算")):
            continue
        if not re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:万亿|千亿|百亿|亿元|万元|亿|万|元|%|％)", s) \
                and not re.search(r"\d+\s*[%％]?\s*[—\-~至]\s*\d", s):
            continue
        out.append(s)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    art = (argv[0] if argv else DEFAULT_ART).strip() or DEFAULT_ART
    if not ART_RE.match(art):
        print(f"art_code 形状非法（只接受编号，不接受 URL）：{art[:40]}", file=sys.stderr)
        return 2
    sections: list[dict] = []
    targets: list[dict] = []
    meta: dict = {}
    total = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            d = _fetch_json(_url_for(art, page))
        except Exception as exc:                  # noqa: BLE001 - 单页失败不阻断其余
            print(f"第 {page} 页抓取失败：{str(exc)[:100]}", file=sys.stderr)
            time.sleep(SLEEP_S)
            continue
        data = d.get("data") or {}
        meta = {"art_code": str(data.get("art_code") or art),
                "title": str(data.get("notice_title") or ""),
                "notice_date": str(data.get("notice_date") or "")[:10],
                "short_name": str(data.get("short_name") or ""),
                "page_size": int(data.get("page_size") or 0)}
        text = str(data.get("notice_content") or "")
        hits = _sentences_with_causality(text)
        for s in hits:
            if total + len(s) > MAX_EXCERPT_CHARS:
                break
            sections.append({"page": page, "text": s})
            total += len(s)
        # 目标句单独冻结：目标类判断的正向样本必须来自发行人原文
        if len(targets) < 4:
            for s in _target_sentences(text):
                if not any(t["text"] == s for t in targets):
                    targets.append({"page": page, "text": s})
        if total >= MAX_EXCERPT_CHARS:
            break
        time.sleep(SLEEP_S)
    if not sections:
        print("未取得带因果语言的 MD&A 句子（保持缺口，不合成）", file=sys.stderr)
        return 1
    body = "\n".join(f"[第 {s['page']} 页] {s['text']}" for s in sections)
    payload = {
        "note": ("洋河股份 2024 年年度报告「管理层讨论与分析」真实片段"
                 "（有界抓取；经 net_policy 公网校验与已验 IP 连接）"),
        "art_code": meta.get("art_code") or art,
        "title": meta.get("title") or "江苏洋河酒厂股份有限公司 2024 年年度报告",
        "published_at": meta.get("notice_date") or "2025-04-29",
        "source_host": HOST,
        "pages_scanned": MAX_PAGES,
        "pages_with_hits": sorted({s["page"] for s in sections}),
        "sections": sections,
        # 发行人自己披露的经营目标句（目标类判断的正向样本）
        "targets": targets,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = ROOT / "evals" / "real" / "yanghe_ar2024_mdna_excerpt.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {out}：{len(sections)} 句、{total} 字，页码 {payload['pages_with_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
