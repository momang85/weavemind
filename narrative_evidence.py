# -*- coding: utf-8 -*-
"""叙事证据（F2）：把抓取到的年报/公告正文切成**可定位的短证据**。

为什么需要：底稿只有数值（收入/利润/现金流），要解释"为什么变"必须回到年报的
经营讨论、财务附注与风险段落。抓到的正文是一整页 3 万字文本，整段塞给模型既超
上下文、又无法复核；这里按小节标题切分、按关键词归类，每条只留 400 字证据 +
**位置**（小节路径 + 字符区间），供简报的"业务背景/变化解释/风险与核查"引用。

三条纪律：
- **只定位、不编造**：没抓到的类别记为缺口（`missing_kinds`），不用模型知识补；
- **确定性**：全部由文本与关键词算出，不调用模型（同一输入必得同一输出）；
- **定位口径诚实**：网页正文没有页码，记"小节路径 + 字符区间"；检索摘要没有正文，
  记 `has_location=False`，不计入"已取得证据"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import workspace

logger = logging.getLogger(__name__)

EVIDENCE_FILE = "narrative_evidence.json"
MAX_PER_KIND = 4
SNIPPET_CHARS = 400

KIND_BACKGROUND = "business_background"
KIND_CHANGE = "change_explanation"
KIND_NOTES = "footnote"
KIND_RISK = "risk"

KIND_LABELS = {
    KIND_BACKGROUND: "业务背景",
    KIND_CHANGE: "经营变化解释",
    KIND_NOTES: "财务附注",
    KIND_RISK: "风险因素",
}
KIND_ORDER = (KIND_BACKGROUND, KIND_CHANGE, KIND_NOTES, KIND_RISK)

# 归类关键词：标题命中权重高于正文命中（标题说"风险因素"才算风险段）
KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    KIND_BACKGROUND: (
        "主营业务", "主要业务", "经营模式", "业务模式", "主要产品", "产品与客户",
        "公司业务", "业务概要", "行业情况", "公司简介", "核心竞争力", "销售模式",
        "客户与市场", "经营计划",
    ),
    KIND_CHANGE: (
        "经营情况讨论与分析", "管理层讨论与分析", "经营回顾", "经营情况回顾",
        "报告期内经营情况", "营业收入变动", "收入变动", "利润变动", "业绩变动",
        "变动原因", "经营成果", "财务状况分析", "经营分析",
    ),
    KIND_NOTES: (
        "财务报表附注", "财务附注", "现金流量表附注", "现金流量分析", "营业收入明细",
        "分部报告", "主要会计政策", "应收账款", "存货", "研发投入", "营运资本",
    ),
    KIND_RISK: (
        "风险因素", "可能面对的风险", "经营风险", "风险提示", "风险与对策",
        "主要风险", "风险分析", "不确定因素",
    ),
}

# 发布者身份按**解析后的域名**判定：标题里出现"年报"或 URL 查询参数里带着官方域名，
# 都不能证明发布者身份（实机：东方财富 API 被标成"发行人年报/官方披露"，
# 前瞻眼被标成"发行人年报"）。域名不在名单里 → 第三方。
OFFICIAL_HOSTS = ("sse.com.cn", "szse.cn", "hkexnews.hk", "cninfo.com.cn",
                  "sec.gov", "bse.cn", "neeq.com.cn", "sseinfo.com", "hkex.com.hk")

_HEAD_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[节章部分]"),
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),
    re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]"),
    re.compile(r"^\d{1,2}[、.．]\s*\S"),
    re.compile(r"^#{1,4}\s+\S"),
)
_HEAD_MAX_CHARS = 40
_LEVEL_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[节章部分]"),
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),
    re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]"),
    re.compile(r"^\d{1,2}[、.．]\s*\S"),
)
_SENT_END = "。！？；.!?;"

# 文档自身的报告期：标题里的"2026年年度报告"这类写法（取最后一个年份）
_DOC_PERIOD_RE = re.compile(r"(19|20)\d{2}\s*年?\s*(?:年度报告|年报|annual report)", re.I)
# 发布日：URL 或标题里的日期（2027-04-01 / 2027/04 / 2027年4月）
_PUB_DATE_RE = re.compile(r"(19|20)\d{2}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?")
# 只有年份的发布线索（URL 路径 /2027/outlook、标题"2027年展望"）：按该年年初算
_PUB_YEAR_PATH_RE = re.compile(r"[/\-_.]((?:19|20)\d{2})(?=[/\-_.]|$)")
_PUB_YEAR_TITLE_RE = re.compile(r"((?:19|20)\d{2})\s*年")
# 主体形态：公司/股份/集团等企业名结尾（用于"是不是别家公司"的判定）
_ENTITY_HINT_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:股份|集团|控股|公司|酒业|银行|能源|"
                             r"科技|医药|地产|汽车|电器|电力|煤业|矿业)")


def publisher_of(url: str) -> str:
    """URL → 发布者域名（去 www.；**只看主机名**，不把查询参数里的域名当发布者）。"""
    try:
        from urllib.parse import urlsplit
        host = str(urlsplit(str(url or "")).hostname or "").lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def source_type(url: str) -> str:
    """来源类型：**只按主机名**判官方披露，其余一律第三方。"""
    host = publisher_of(url)
    if not host:
        return "third_party"
    return "issuer_annual_report" if any(
        host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS) else "third_party"


def _doc_period(title: str, url: str = "") -> str:
    """文档自身的报告期（标题里的"2026年年度报告"）；取不到返回空串（不猜）。"""
    m = _DOC_PERIOD_RE.search(str(title or ""))
    return m.group(0)[:4] if m else ""


def _published_at(doc: dict) -> str:
    """发布日：URL 路径或标题里的日期（取最早出现的那个）；取不到返回空串（不猜）。

    只有年份的线索（`/2027/outlook`、"2027年展望"）按该年**年初**算——用于判断
    "这份材料是不是在资料截止日之后才出现"，宁可判早也不放过晚于截止日的材料。
    """
    url = str(doc.get("url") or "")
    title = str(doc.get("title") or "")
    m = _PUB_DATE_RE.search(url) or _PUB_DATE_RE.search(title)
    if m:
        return m.group(0).replace("年", "-").replace("月", "-").rstrip("-.")
    m = _PUB_YEAR_PATH_RE.search(url) or _PUB_YEAR_TITLE_RE.search(title)
    if m:
        return f"{m.group(1)}-01-01"
    return ""


def _subject_state(doc: dict, company: str, company_id: str) -> str:
    """主体适用性：命中本主体 → ok；只出现别家主体 → mismatch；都没有 → unknown。"""
    title = str(doc.get("title") or "")
    text = str(doc.get("text") or "")[:4000]
    subject = str(company or "").strip()
    code = str(company_id or "").strip()
    if subject and subject in (title + text):
        return "ok"
    if code:
        from facts import bare_code
        if bare_code(code) and bare_code(code) in (title + text):
            return "ok"
    hints = {h for h in _ENTITY_HINT_RE.findall(title)}
    if hints and subject:
        return "mismatch"
    return "unknown"


def _date_key(text: str) -> tuple[int, int] | None:
    """日期 → (年, 月) 元组；取不到返回 None（按数字比较，不做字符串比较）。"""
    s = str(text or "").strip()
    if not s:
        return None
    m = re.match(r"^((?:19|20)\d{2})[-/年.](\d{1,2})", s)
    if not m:
        m = re.match(r"^((?:19|20)\d{2})$", s)
        if not m:
            return None
        return int(m.group(1)), 1
    return int(m.group(1)), int(m.group(2))


def _norm_date(text: str) -> str:
    """`2025/03` / `2025-04-03` / `2025年3月` → `YYYY-MM-DD`（统一落盘格式）。"""
    s = str(text or "").strip()
    m = re.match(r"^((?:19|20)\d{2})[-/年. ](\d{1,2})(?:[-/月. ](\d{1,2}))?", s)
    if not m:
        y = re.match(r"^((?:19|20)\d{2})$", s)
        return f"{y.group(1)}-01-01" if y else ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3) or 1):02d}"


def validate_record(rec: dict, doc: dict, *, company: str = "", company_id: str = "",
                    periods=None, as_of: str = "") -> dict:
    """证据的**契约适用性**校验（先校验，再分类/绑定主张）。

    有字符位置只证明"在某段文本里找到"，不证明主体、期间与资料截止成立。
    返回 `{validation_status, subject, document_period, published_at, excluded}`；
    缺失字段记 `unknown`（保留但标注），错主体/超截止**明确排除**。
    """
    years = [int(y) for y in (periods or [])]
    subject_state = _subject_state(doc, company, company_id)
    doc_period = _doc_period(str(doc.get("title") or ""), str(doc.get("url") or ""))
    pub = _norm_date(_published_at(doc))
    status = "applicable"
    if subject_state == "mismatch":
        status = "subject_mismatch"
    elif pub and _date_key(pub) and _date_key(as_of) and _date_key(pub) > _date_key(as_of):
        status = "after_as_of"
    elif doc_period and years and int(doc_period) > max(years):
        status = "period_after_contract"
    elif doc_period and years and int(doc_period) < min(years):
        # 早于契约期间的文档：若证据段落里出现契约期间，则算**比较披露**（允许）；
        # 否则期间不适用（不按"只含最新年份"粗暴拒绝）
        snip = str(rec.get("snippet") or "") + str(rec.get("section") or "")
        status = "comparison" if any(str(y) in snip for y in years) else "period_before_contract"
    elif not doc_period:
        status = "unknown_period"
    excluded = status in ("subject_mismatch", "after_as_of", "period_after_contract",
                          "period_before_contract")
    return {"validation_status": status, "subject": company or company_id or "",
            "subject_state": subject_state, "document_period": doc_period,
            "published_at": pub, "excluded": excluded}


def _heading_level(line: str) -> int | None:
    """标题层级：0=节/markdown 标题，1=一、，2=（一），3=1.；不是标题返回 None。"""
    if line.startswith("#"):
        return 0
    for i, pat in enumerate(_LEVEL_PATTERNS):
        if pat.match(line):
            return i
    return None


def _is_heading(line: str) -> bool:
    s = str(line or "").strip()
    if not s:
        return False
    if s.startswith("#"):
        return len(s) <= 80
    if len(s) > _HEAD_MAX_CHARS:
        return False
    if any(p.match(s) for p in _HEAD_PATTERNS):
        return True
    # 短行 + 命中类别关键词 + 无句末标点：年报正文里常见的无编号小标题
    if len(s) <= 24 and not any(c in s for c in _SENT_END):
        return any(k in s for kws in KIND_KEYWORDS.values() for k in kws)
    return False


def split_sections(text: str, page_offsets=None) -> list[dict]:
    """按标题行切分正文，返回 `{title, path, body, start, end, page}`。

    `page_offsets`（PDF 专用）：`[(字符起点, 页码)]`，用于给每个小节标出所在页。
    """
    lines = str(text or "").splitlines()
    out: list[dict] = []
    cur: dict | None = None
    parents: dict[int, str] = {}
    offset = 0
    for raw in lines:
        line = str(raw or "").strip()
        step = len(str(raw or "")) + 1          # +1：行尾换行符
        level = _heading_level(line) if _is_heading(line) else None
        if level is not None:
            if cur is not None:
                cur["end"] = offset
                out.append(cur)
                cur = None
            title = line.lstrip("#").strip()
            parents[level] = title
            for k in [k for k in parents if k > level]:
                parents.pop(k, None)
            path = " > ".join(parents[k] for k in sorted(parents))
            cur = {"title": title, "path": path, "lines": [],
                   "start": offset, "end": offset}
        elif cur is not None:
            cur["lines"].append(line)
        offset += step
    if cur is not None:
        cur["end"] = offset
        out.append(cur)
    for sec in out:
        sec["body"] = "\n".join(sec["lines"]).strip()
        sec["page"] = _page_of(sec["start"], page_offsets)
    return [s for s in out if s["body"] or s["title"]]


def _page_of(char_start: int, page_offsets) -> int | None:
    """字符位置 → 页码（PDF 用；没有页码信息返回 None）。"""
    page = None
    for start, no in (page_offsets or []):
        if int(char_start) >= int(start):
            page = int(no)
        else:
            break
    return page


def classify(title: str, body: str = "") -> str | None:
    """按关键词给小节归类；标题命中权重 3、正文（前 200 字）命中权重 1。"""
    head = str(title or "")
    lead = str(body or "")[:200]
    best: str | None = None
    best_score = 0
    for kind in KIND_ORDER:
        score = 0
        for kw in KIND_KEYWORDS[kind]:
            if kw in head:
                score += 3
            elif kw in lead:
                score += 1
        if score > best_score:
            best, best_score = kind, score
    return best


def _snippet(body: str, limit: int = SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(c) for c in _SENT_END)
    if idx >= limit // 2:
        cut = cut[: idx + 1]
    return cut.strip()


def extract_sections(doc: dict, *, periods=None, company: str = "",
                     company_id: str = "", as_of: str = "",
                     max_per_kind: int = MAX_PER_KIND,
                     snippet_chars: int = SNIPPET_CHARS) -> list[dict]:
    """一份抓取文档 → 带定位的证据记录（每类最多 `max_per_kind` 条）。

    每条都先过**契约适用性校验**（主体/期间/资料截止），并把校验字段一并带出；
    不适用的记录照实保留（`excluded=True` + 原因），由调用方排除与说明，不静默丢弃。
    """
    text = str((doc or {}).get("text") or "")
    if not text:
        return []
    url = str((doc or {}).get("url") or "")
    title = str((doc or {}).get("title") or "")
    stype = source_type(url)
    years = [str(y) for y in (periods or [])]
    page_offsets = doc.get("page_offsets")
    picked: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for sec in split_sections(text, page_offsets=page_offsets):
        kind = classify(sec["title"], sec["body"])
        if not kind:
            continue
        snip = _snippet(sec["body"], snippet_chars)
        if not snip:
            continue
        key = (kind, sec["path"], snip[:60])
        if key in seen:
            continue
        seen.add(key)
        bucket = picked.setdefault(kind, [])
        if len(bucket) >= max_per_kind:
            continue
        hint = next((y for y in years if y in (sec["path"] + snip)), "")
        rec = {
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "title": title,
            "url": url,
            "source_type": stype,
            "publisher": publisher_of(url),
            "section": sec["path"],
            "snippet": snip,
            "char_start": int(sec["start"]),
            "char_end": int(sec["end"]),
            "page": sec.get("page"),
            "period_hint": hint,
            "has_location": True,
            "locator": _locator_text(sec),
            "content_hash": hashlib.sha256(snip.encode("utf-8")).hexdigest()[:16],
            "fetched_at": str(doc.get("fetched_at") or ""),
        }
        rec.update(validate_record(rec, doc, company=company, company_id=company_id,
                                   periods=periods, as_of=as_of))
        bucket.append(rec)
    out: list[dict] = []
    for kind in KIND_ORDER:
        out.extend(picked.get(kind) or [])
    return out


def _locator_text(sec: dict) -> str:
    """定位文本：有页码就写页码（PDF），否则写小节 + 字符区间（网页）。"""
    page = sec.get("page")
    if page:
        return f"第 {page} 页 · 小节：{sec['path']}（字符 {sec['start']}-{sec['end']}）"
    return f"小节：{sec['path']}（字符 {sec['start']}-{sec['end']}）"


def _project_dir(task_id: str, project=None):
    return workspace.task_project_dir(task_id, project) if project \
        else workspace.task_project_dir(task_id)


def _read_json(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_inputs(task_id: str, project=None) -> tuple[list[dict], list[dict]]:
    """工作区里的抓取正文与检索摘要（后者无正文定位，只作提示）。"""
    proj = _project_dir(task_id, project)
    docs = _read_json(proj / "fetch_snapshot.json") or []
    snips = _read_json(proj / "search_results.json") or []
    return ([d for d in docs if isinstance(d, dict)],
            [s for s in snips if isinstance(s, dict)])


def _contract_hint(task_id: str, goal: str = "") -> tuple[list[int], str, str, str]:
    """契约提示：期间 / 主体名 / 稳定标识 / 资料截止（用于证据适用性校验）。"""
    try:
        from working_paper_export import resolve_request
        req, _c, _s = resolve_request(task_id, goal, {}, None)
        if req is not None:
            return ([int(y) for y in (req.periods or [])], str(req.company or ""),
                    str(req.company_id or ""), str(req.as_of or ""))
    except Exception:
        pass
    return [], "", "", ""


def build(task_id: str, *, periods=None, company: str = "", company_id: str = "",
          as_of: str = "", goal: str = "", ws_dir=None, project=None,
          extra_docs=None) -> dict:
    """从工作区抓取正文提取叙事证据并落盘 `narrative_evidence.json`（幂等，不联网）。

    `extra_docs`：刚抓取、尚未落进 `fetch_snapshot.json` 的文档（`{title,url,text}`），
    按 URL 去重后并入——抓取回灌有它自己的启用条件，证据提取不依赖那条件。
    """
    if not periods and not company:
        periods, company, company_id, as_of = _contract_hint(task_id, goal)
    if not as_of:
        as_of = _contract_hint(task_id, goal)[3]
    years = [int(y) for y in (periods or [])]
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    records: list[dict] = []
    try:
        docs, snips = _read_inputs(task_id, project)
    except Exception as exc:                     # noqa: BLE001 - 证据提取不得拖垮主线
        logger.warning("叙事证据输入读取失败（task=%s）：%s", task_id, str(exc)[:140])
        docs, snips = [], []
    for doc in (extra_docs or []):
        if not isinstance(doc, dict) or not str(doc.get("text") or "").strip():
            continue
        url = str(doc.get("url") or "")
        if url and any(str(d.get("url") or "") == url for d in docs):
            continue
        docs = docs + [doc]
    for doc in docs:
        try:
            doc = dict(doc, fetched_at=fetched_at)
            records.extend(extract_sections(doc, periods=years, company=company,
                                            company_id=company_id, as_of=as_of))
        except Exception as exc:                 # noqa: BLE001 - 单篇失败不影响其余
            logger.warning("叙事证据切分失败（task=%s）：%s", task_id, str(exc)[:140])
    # 检索摘要：只作无定位提示（不计入"已取得证据"）
    for s in snips[:20]:
        title = str(s.get("title") or "")
        body = str(s.get("snippet") or "")
        kind = classify(title, body)
        if not kind:
            continue
        url = str(s.get("url") or "")
        rec = {
            "kind": kind, "kind_label": KIND_LABELS[kind], "title": title, "url": url,
            "source_type": source_type(url), "publisher": publisher_of(url), "section": "",
            "snippet": _snippet(body, 200), "char_start": 0, "char_end": 0,
            "page": None,
            "period_hint": next((str(y) for y in years if str(y) in (title + body)), ""),
            "has_location": False, "locator": "检索摘要（未取得正文定位）",
            "content_hash": hashlib.sha256(
                _snippet(body, 200).encode("utf-8")).hexdigest()[:16],
            "fetched_at": fetched_at,
        }
        rec.update(validate_record(rec, {"title": title, "url": url, "text": body},
                                   company=company, company_id=company_id,
                                   periods=periods, as_of=as_of))
        records.append(rec)
    # 每次抓取成功都会重跑本函数：**保留**上一轮来自别的页面的证据（同一 URL 以本轮
    # 重新抽取的为准）。否则后抓的一页会把先抓那页的证据覆盖掉（抓取回灌写快照有它
    # 自己的长度门槛，不能假定快照一定收录了每一页）。
    seen_urls = {str(d.get("url") or "") for d in docs}
    merged: list[dict] = list(records)
    for r in (read(task_id, ws_dir=ws_dir) or {}).get("records") or []:
        if not r.get("has_location") or str(r.get("url") or "") in seen_urls:
            continue
        merged.append(r)
    dedup: dict[tuple, dict] = {}
    for r in merged:
        key = (str(r.get("kind")), str(r.get("url")), str(r.get("section")),
               str(r.get("snippet"))[:60])
        dedup.setdefault(key, r)
    ordered: list[dict] = []
    for kind in KIND_ORDER:
        bucket = [r for r in dedup.values()
                  if r.get("kind") == kind and r.get("has_location")
                  and not r.get("excluded")]
        bucket.sort(key=lambda r: (str(r.get("url") or ""), int(r.get("char_start") or 0)))
        ordered.extend(bucket[:MAX_PER_KIND])
    # 不适用的记录（错主体/超截止/期间不符）与无定位的检索摘要照实保留：
    # 前者用于向读者说明"为什么这些材料没被采用"，不静默丢弃
    ordered.extend(r for r in dedup.values()
                   if not r.get("has_location") or r.get("excluded"))
    records = ordered
    missing = [k for k in KIND_ORDER
               if not any(r["kind"] == k and r.get("has_location") and not r.get("excluded")
                          for r in records)]
    sources: list[dict] = []
    for r in records:
        if not r.get("url") or any(s["url"] == r["url"] for s in sources):
            continue
        sources.append({"url": r["url"], "title": r.get("title") or "",
                        "source_type": r.get("source_type") or "third_party",
                        "publisher": r.get("publisher") or "",
                        "has_location": bool(r.get("has_location"))})
    excluded = [{"url": r.get("url") or "", "title": r.get("title") or "",
                 "validation_status": r.get("validation_status") or "",
                 "document_period": r.get("document_period") or "",
                 "published_at": r.get("published_at") or "",
                 "locator": r.get("locator") or ""}
                for r in records if r.get("excluded")]
    payload = {
        "ok": bool([r for r in records if r.get("has_location") and not r.get("excluded")]),
        "company": company,
        "company_id": company_id,
        "as_of": as_of,
        "periods": years,
        "records": records,
        "missing_kinds": missing,
        "missing_labels": [KIND_LABELS[k] for k in missing],
        "sources": sources,
        "excluded": excluded[:12],
        "docs": len(docs),
        "located": sum(1 for r in records
                       if r.get("has_location") and not r.get("excluded")),
        "built_at": fetched_at,
    }
    _write(task_id, payload, ws_dir=ws_dir)
    return payload


def _write(task_id: str, payload: dict, *, ws_dir=None) -> None:
    try:
        ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / EVIDENCE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:                     # noqa: BLE001 - 落盘失败只记日志
        logger.warning("叙事证据落盘失败（task=%s）：%s", task_id, str(exc)[:120])


def read(task_id: str, *, ws_dir=None) -> dict | None:
    """读已落盘的叙事证据；不存在时返回 None（调用方可按需 `build`）。"""
    try:
        ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
        data = _read_json(ws / EVIDENCE_FILE)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
