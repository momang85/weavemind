# -*- coding: utf-8 -*-
"""A 股原始披露摄取（S3 窄闭环）：发现 → 取件 → 身份/期间/截止校验 → 可定位证据。

资料获取顺序里本模块只管"官方披露发现及原文"这一段（专项 §7）：上游是任务内已准入
文件与冻结缓存，下游才是有标识的结构化补充源。**不新建请求点**——取件一律走既有通道
（`annual_report_pdf.fetch_bytes` → `adapters.transport` → `net_policy`），本模块只做
判定与组装。

三条硬边界：

1. **巨潮未连通就是未连通**：`discover()` 如实返回 `unavailable` 与原因，不伪造公告列表，
   也不把搜索摘要当原文（"有 PDF 解析器"≠"能发现并取得目标公司年报"）。
2. **人工取得的材料必须标注来源**：`manual_url` / `manual_file` 不得写成"自动检索通过"。
3. **主体、报告期、披露日三样都要有证据**：错主体 / 错期 / 晚于截止 / 披露日未知分别拒绝，
   且最后一种不得用于"截至日成立"的断言（材料可作背景，但不能当已核验的时点证据）。

证据定位复用 `narrative_evidence`（PDF 页码或公告文本接口片段），并绑定正文 hash——
"可定位"与"可复验"是同一件事的两面。
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# 摄取结论
ADMITTED = "admitted"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"

# 拒绝原因（每条都要能对上一句人话）
REJECT_NOT_OFFICIAL = "not_official_source"
REJECT_SUBJECT = "subject_mismatch"
REJECT_SUBJECT_UNKNOWN = "subject_unknown"
REJECT_PERIOD = "period_mismatch"
REJECT_PERIOD_UNKNOWN = "period_unknown"
REJECT_AFTER_CUTOFF = "after_cutoff"
REJECT_CUTOFF_UNKNOWN = "cutoff_unknown"
REJECT_EMPTY = "empty_document"
REJECT_ERROR_PAGE = "error_page"
REJECT_NOT_REPORT_BODY = "not_report_body"

REJECT_TEXT = {
    REJECT_NOT_OFFICIAL: "不是发行人原始披露（域名非官方，且标题/主体也不足以证明是报告本身）",
    REJECT_SUBJECT: "主体不符：文档指向的是另一家公司",
    REJECT_SUBJECT_UNKNOWN: "主体无法判定：文档里既没有本主体名称，也没有证券代码",
    REJECT_PERIOD: "报告期不符：文档自身的报告期不是请求的期间",
    REJECT_PERIOD_UNKNOWN: "报告期无法判定：标题里没有报告期（正文里出现某年份不算——"
                           "年报正文处处是对比年）",
    REJECT_AFTER_CUTOFF: "披露日晚于资料截止日：该材料不能用于截至日成立的断言",
    REJECT_CUTOFF_UNKNOWN: "披露日未知或精度不足（只有年/月）：截至判断不成立"
                           "（材料可作背景，不得当已核验时点证据）",
    REJECT_EMPTY: "文档没有正文（空文档或取件失败）",
    REJECT_ERROR_PAGE: "取回的是错误/拒绝页（如 Access denied / 403），不是原始披露正文",
    REJECT_NOT_REPORT_BODY: "正文里找不到报告应有的内容特征（标题说是年报不等于正文是年报）",
}

# 错误页/拒绝页标记：命中即拒（标题可能是对的——实机反例：标题为年报、正文为拒绝页）
_ERROR_PAGE_MARKERS = (
    "access denied", "request rejected", "403 forbidden", "404 not found",
    "forbidden", "请求被拒绝", "访问被拒绝", "无权限访问", "页面不存在",
    "captcha", "验证码", "人机验证", "are you a robot", "security check",
)
# 报告正文应有的特征（任一命中即可；不是"全文必须含某段"，避免误杀摘要型披露）
_REPORT_BODY_MARKERS = (
    "年度报告", "财务报表", "营业收入", "资产负债表", "利润表", "现金流量表",
    "董事会", "公司简介", "主要会计数据", "归属于上市公司股东",
)

# 人工取得的两种来源（与自动检索区分，写进 provenance）
PROVENANCE_AUTO = "auto_search"
PROVENANCE_MANUAL_URL = "manual_url"
PROVENANCE_MANUAL_FILE = "manual_file"
PROVENANCE_LABEL = {
    PROVENANCE_AUTO: "自动检索取得",
    PROVENANCE_MANUAL_URL: "人工提供的官方直链",
    PROVENANCE_MANUAL_FILE: "人工取得的文件",
}


def _reject(reason: str, **extra) -> dict:
    out = {"status": REJECTED, "reason": reason,
           "detail": REJECT_TEXT.get(reason, reason)}
    out.update(extra)
    return out


def document_hash(doc: dict) -> str:
    """正文 hash（可复验的绑定目标）：只对正文文本取 sha256。"""
    return hashlib.sha256(str((doc or {}).get("text") or "").encode(
        "utf-8", "replace")).hexdigest()


def _parse_calendar_date(text: str) -> tuple[str, str]:
    """日期文本 → `(YYYY-MM-DD, 精度)`；**按真实日历校验**，非法日期返回 `("", "")`。

    为什么不能只看长度和大小：`2025-00-99` 长度够、字符串比较也"小于"截止日，
    但它在日历上不存在（专项 §3.2 反例）；`2025/04/29` 同样是真实日期，不能因为
    分隔符不同就判未知。精度：只有年/月时如实标注，且**不得**用于截至判断。
    """
    import re as _re
    from datetime import date as _date

    raw = str(text or "").strip()
    m = _re.match(r"^((?:19|20)\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?日?", raw)
    if not m:
        m = _re.match(r"^((?:19|20)\d{2})(\d{2})(\d{2})$", raw)
        if not m:
            m = _re.match(r"^((?:19|20)\d{2})$", raw)
            if not m:
                return "", ""
            return f"{m.group(1)}", "year"
    year, month = int(m.group(1)), int(m.group(2))
    day = int(m.group(3)) if m.group(3) else 0
    try:
        if day:
            return _date(year, month, day).isoformat(), "day"
        return _date(year, month, 1).isoformat()[:7], "month"
    except ValueError:
        logger.info("日期不合法（日历校验未通过）：%r", raw[:20])
        return "", ""


def _declared_disclosure(doc: dict) -> tuple[str, str, str]:
    """披露/公告日 → `(日期, 精度, 依据)`；拿不到证据就是 `("", "", "")`。

    依据只认两类（专项 §3.2）：**来源字段**（`disclosed_at`/`published_at`/
    `notice_date`）与**已知来源格式**的 URL（公告文本 art_code、路径里的
    `YYYY-MM-DD`）。任意查询参数不再当证据——`?asof=2020-01-01` 曾经能冒充披露日。
    精度要一起返回：日级才允许用于截至判断。
    """
    for key in ("disclosed_at", "published_at", "notice_date"):
        val = str((doc or {}).get(key) or "").strip()[:10]
        if val:
            norm, prec = _parse_calendar_date(val)
            if norm:
                return norm, prec, f"source_field:{key}"
    try:
        from narrative_evidence import _published_at
        val = _published_at(doc) or ""
    except Exception:
        val = ""
    if not val:
        return "", "", ""
    norm, prec = _parse_calendar_date(val)
    return (norm, prec, "source_url_format") if norm else ("", "", "")


def discover(company: str, company_code: str = "", periods=(), doc_type: str = "年度报告") -> dict:
    """官方披露**发现**：当前只有巨潮一条链路，且它未连通。

    如实返回 `unavailable`：接口探针未通过（POST 查询恒空、webapi 需 mcode），
    所以"自动发现公告列表"这件事本版本做不到——不能拿搜索结果冒充，也不绕验证码。
    操作者的正当出路写进 `next_steps`：提供官方直链或人工取得的文件，走同一摄取与准入。
    """
    try:
        from adapters import cninfo
        if bool(cninfo.enabled()):
            # 启用态下也如实报告结果（探针未通过 → 抛错即为结论，不吞掉）
            try:
                rows = cninfo._fetch_annual_rows(str(company_code or ""), periods)
                if rows:
                    return {"status": ADMITTED, "channel": "cninfo", "rows": len(rows)}
            except Exception as exc:                 # noqa: BLE001
                return {"status": UNAVAILABLE, "channel": "cninfo",
                        "reason": f"巨潮接口不可用：{str(exc)[:120]}"}
    except Exception as exc:                         # noqa: BLE001
        return {"status": UNAVAILABLE, "channel": "cninfo",
                "reason": f"巨潮适配器不可读：{str(exc)[:120]}"}
    return {
        "status": UNAVAILABLE,
        "channel": "cninfo",
        "reason": "巨潮公告发现接口未连通（探针恒空/需 token），本版本不做自动发现",
        "next_steps": [
            "提供该年报在官方披露平台的直链（manual_url）",
            "或人工取得的 PDF/文本文件（manual_file）",
        ],
    }


def pick_official_candidates(candidates, *, company: str, company_code: str = "",
                             period: str = "", doc_type: str = "年度报告") -> list[dict]:
    """从检索候选里挑出**可送去取件**的原始披露候选（纯函数，不发请求）。

    只留两类：① 官方披露域（按主机名判定）；② 标题本身就是报告且主体命中（第三方转载
    原文，实机见过）。每条附 `why`，被排除的也会在调用方日志里说明原因——"没有候选"
    与"候选都不合格"是两件事。
    """
    from narrative_evidence import document_provenance, publisher_of, source_type, _subject_state

    keep: list[dict] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        url = str(cand.get("url") or "").strip()
        # 只要求"能解析出主机名"：真正发请求的是既有抓取通道（net_policy 全量校验），
        # 这里判的是"这条候选值不值得送去取件"，不是安全边界。
        host = publisher_of(url)
        if not host:
            continue
        title = str(cand.get("title") or "")
        subject = _subject_state({"title": title, "text": ""}, company, company_code)
        if subject != "ok":
            continue
        official = source_type(url) == "issuer_annual_report"
        is_report = document_provenance({"title": title}, company, company_code) \
            == "issuer_annual_report"
        if not (official or is_report):
            continue
        if period and str(period) not in title:
            continue
        if doc_type and str(doc_type)[:2] not in title and not official:
            continue
        keep.append({
            "url": url, "title": title, "host": host,
            "why": "官方披露域" if official else "标题即报告本身（第三方转载原文）",
        })
    return keep


def ingest(doc: dict, *, company: str, company_code: str = "", periods=(),
           as_of: str = "", provenance: str = PROVENANCE_AUTO,
           doc_type: str = "年度报告", section_limit: int = 5) -> dict:
    """一份原始披露 → 准入结论 + 可定位证据。

    `doc`：`{title, url, text, page_offsets|chunk_offsets, disclosed_at?}`（取件由调用方用
    既有通道完成）。返回：
    - 准入：`{status: admitted, provenance, provenance_label, doc, evidence, cutoff, hash}`
    - 拒绝：`{status: rejected, reason, detail}`（含 `may_use_as_background` 标记）
    """
    from narrative_evidence import (document_provenance, publisher_of, source_type,
                                    split_sections, _subject_state)

    if str(provenance) not in PROVENANCE_LABEL:
        provenance = PROVENANCE_AUTO
    url = str((doc or {}).get("url") or "")
    title = str((doc or {}).get("title") or "")
    text = str((doc or {}).get("text") or "")
    if not text.strip():
        return _reject(REJECT_EMPTY, may_use_as_background=False)

    # 正文体检（C0-1）：标题说是年报 **不等于** 正文是年报——实机反例是"标题为年报、
    # 正文为拒绝页/别家公司年报"。先看正文是不是错误页，再看正文有没有报告特征。
    low_head = text[:4000].lower()
    if any(m in low_head for m in _ERROR_PAGE_MARKERS) and len(text) < 20000:
        return _reject(REJECT_ERROR_PAGE, may_use_as_background=False)
    if not any(m in text[:20000] for m in _REPORT_BODY_MARKERS):
        return _reject(REJECT_NOT_REPORT_BODY, chars=len(text),
                       may_use_as_background=False)

    official = source_type(url) == "issuer_annual_report"
    is_report = document_provenance({"title": title}, company, company_code) \
        == "issuer_annual_report"
    # 主体判定用**正文**（C0-1）：标题写洋河、正文是别家年报的反例必须被拒。
    # 正文命中本主体名称或代码才算 ok；没有本主体时，若正文出现别家主体名 → 主体不符，
    # 否则只能记"主体无法判定"。视线范围取前 6 万字符：公告正文的封面常落在片段中段
    # （实测样本"洋河股份"首次出现于第 8123 字符），只看前几千字符会误杀正例。
    from narrative_evidence import _ENTITY_HINT_RE
    body_head = text[:60000]
    body_ok = bool(company and company in body_head)
    if not body_ok and company_code:
        from facts import bare_code
        code_bare = bare_code(company_code)
        body_ok = bool(code_bare and code_bare in body_head)
    if not body_ok:
        others = _ENTITY_HINT_RE.findall(body_head)
        return _reject(REJECT_SUBJECT if others else REJECT_SUBJECT_UNKNOWN,
                       host=publisher_of(url), subject_source="body",
                       others=others[:3], may_use_as_background=False)
    if not (official or is_report):
        return _reject(REJECT_NOT_OFFICIAL, host=publisher_of(url),
                       may_use_as_background=True)

    want_periods = [str(p) for p in (periods or ()) if str(p).strip()]
    if want_periods:
        # 用**文档自己的报告期**（标题里的"2024年年度报告"）判定，不看正文里出现过哪些
        # 年份：年报正文处处是对比年（2023/2022 都在），拿"正文含该年"当证据等于不校验。
        from narrative_evidence import _doc_period
        doc_period = _doc_period(title, url)
        if not doc_period:
            return _reject(REJECT_PERIOD_UNKNOWN, periods=want_periods,
                           may_use_as_background=True)
        if doc_period not in want_periods:
            return _reject(REJECT_PERIOD, doc_period=doc_period, periods=want_periods,
                           may_use_as_background=True)

    disclosed, precision, basis = _declared_disclosure(doc)
    cutoff = {"disclosed_at": disclosed, "precision": precision, "basis": basis,
              "as_of": str(as_of or "")}
    if as_of:
        if not disclosed or precision != "day":
            # 披露日未知/精度不足 → 截至判断不成立（专项 §7：不得用期末或报告期兜底）
            return _reject(REJECT_CUTOFF_UNKNOWN, cutoff=cutoff,
                           may_use_as_background=True)
        cutoff["verdict"] = "within" if disclosed <= str(as_of) else "after"
        if cutoff["verdict"] == "after":
            return _reject(REJECT_AFTER_CUTOFF, cutoff=cutoff,
                           may_use_as_background=False)
    else:
        cutoff["verdict"] = "not_requested"

    sections = split_sections(text, page_offsets=doc.get("page_offsets"),
                              chunk_offsets=doc.get("chunk_offsets"))
    evidence = [{
        "title": str(sec.get("title") or "")[:80],
        "path": str(sec.get("path") or "")[:120],
        "start": int(sec.get("start") or 0), "end": int(sec.get("end") or 0),
        "page": sec.get("page"), "chunk": sec.get("chunk"),
    } for sec in sections[:max(1, int(section_limit))]]

    # 来源类别：**不因标题是年报或用户直链就升为官方**（专项 C0-1）
    if official:
        source_class = "official_disclosure"
    elif provenance == PROVENANCE_MANUAL_FILE:
        source_class = "user_file"
    else:
        source_class = "third_party_mirror"
    return {
        "status": ADMITTED,
        "provenance": provenance,
        "provenance_label": PROVENANCE_LABEL[provenance],
        "doc": {"title": title[:160], "url": url, "host": publisher_of(url),
                "periods": want_periods, "doc_type": str(doc_type or ""),
                "source_class": source_class},
        "cutoff": cutoff,
        "evidence": evidence,
        "section_count": len(sections),
        "hash": document_hash(doc),
        "note": "证据坐标为字符区间 + 页码/接口片段；正文 hash 与本次取件内容绑定",
    }
