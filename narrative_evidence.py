# -*- coding: utf-8 -*-
"""叙事证据（F2）：把抓取到的年报/公告正文切成**可定位的短证据**。

为什么需要：底稿只有数值（收入/利润/现金流），要解释"为什么变"必须回到年报的
经营讨论、财务附注与风险段落。抓到的正文是一整页 3 万字文本，整段塞给模型既超
上下文、又无法复核；这里按小节标题切分、按关键词归类，每条只留 400 字证据 +
**位置**（小节路径 + 字符区间），供简报的"业务背景/变化解释/风险与核查"引用。

三条纪律：
- **只定位、不编造**：没抓到的类别记为缺口（`missing_kinds`），不用模型知识补；
- **确定性**：全部由文本与关键词算出，不调用模型（同一输入必得同一输出）；
- **定位口径诚实**（D1 收紧）：网页正文没有页码，记"小节路径 + 字符区间"；
  检索摘要没有正文，记 `has_location=False` 并只计入 `snippet_hints`（线索）；
  `located` = **已准入 + 有正文位置 + 按 (url, 片段指纹) 去重**的片段数——
  摘要不补"已取得定位"的数量，也不清除"某类证据缺失"；同一 URL 已有正文证据时
  不再登记它的检索摘要（一个 URL 不能靠摘要复制多算覆盖）。
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
# 校验规则版本：规则变化后**旧缓存不得当作当前已验证结果**（缺字段的旧缓存按新规则
# 只读重算，或在页面上标待复核）。改动 validate_record/admission 语义时必须递增。
RULES_VERSION = "f3a-1"
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
# 发布日：URL 路径里的日期（2025-04-03 / 2025/04 / 2025）；紧凑写法 20260301 也认
_PUB_DATE_RE = re.compile(r"(19|20)\d{2}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?")
_PUB_COMPACT_RE = re.compile(r"(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
_PUB_YEAR_PATH_RE = re.compile(r"[/\-_.]((?:19|20)\d{2})(?=[/\-_.]|$)")
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


def document_provenance(doc: dict, company: str = "", company_id: str = "") -> str:
    """文档 provenance：内容**本身就是发行人年度报告** → `issuer_annual_report`。

    与 `source_type`（按**访问路径**的域名判）分开：同一份年报原文可以挂在第三方
    平台上（实机：东财公告页转载洋河 2024 年报全文）。只有标题是报告本身（不是
    "解读/点评/摘要"）且主体命中时才算发行人文件；"年报解读"类文章仍是第三方。
    """
    title = str((doc or {}).get("title") or "")
    if not _DOC_PERIOD_RE.search(title):
        return ""
    if re.search(r"解读|点评|评论|研报|摘要|点评报告|观点|分析", title):
        return ""
    if _subject_state(doc, company, company_id) != "ok":
        return ""
    return "issuer_annual_report"


def _doc_period(title: str, url: str = "") -> str:
    """文档自身的报告期（标题里的"2026年年度报告"）；取不到返回空串（不猜）。"""
    m = _DOC_PERIOD_RE.search(str(title or ""))
    return m.group(0)[:4] if m else ""


def _published_at(doc: dict) -> str:
    """发布日：**只看 URL 路径里的日期**（取不到返回空串，缺发布日记 unknown）。

    报告期不是发布日期：标题里的年份（"2024年年度报告"）是**报告期**，不能当发布日
    （实机反例：没有发布日的文档被推成"年初"，于是晚于资料截止的文档照样 applicable）。
    文档期与发布日分别记录；只有年月/只有年份时精度另记，由校验按保守方式处理。
    """
    url = str(doc.get("url") or "")
    m = _PUB_DATE_RE.search(url)
    if m:
        return m.group(0).replace("年", "-").replace("月", "-").rstrip("-.")
    m = _PUB_COMPACT_RE.search(url)
    if m:                                    # 202603011438… → 2026-03-01（东财等常见写法）
        s = m.group(0)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    m = _PUB_YEAR_PATH_RE.search(url)
    if m:
        return m.group(1)
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


def _date_parts(text: str) -> tuple[tuple[int, int, int], str] | None:
    """日期文本 → `((年,月,日), 精度)`；精度 ∈ day/month/year；取不到返回 None。

    **不虚构缺失部分**：只有年月就记 `month`（日按 1 参与排序，但精度另记），
    只有年就记 `year`——精度不足时由 `validate_record` 保守处理，不当作已核实日期。
    """
    s = str(text or "").strip()
    if not s:
        return None
    m = re.match(r"^((?:19|20)\d{2})[-/年.](\d{1,2})(?:[-/月.](\d{1,2}))?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            return (y, mo, int(m.group(3))), "day"
        return (y, mo, 1), "month"
    y = re.match(r"^((?:19|20)\d{2})$", s)
    if y:
        return (int(y.group(1)), 1, 1), "year"
    return None


def _norm_date(text: str) -> str:
    """`2025/03` / `2025-04-03` / `2025年3月` → `YYYY-MM-DD`（统一落盘格式）。"""
    parts = _date_parts(text)
    if not parts:
        return ""
    (y, mo, d), _prec = parts
    return f"{y:04d}-{mo:02d}-{d:02d}"


def validate_record(rec: dict, doc: dict, *, company: str = "", company_id: str = "",
                    periods=None, as_of: str = "") -> dict:
    """证据的**契约适用性**校验（先校验，再分类/绑定主张）。

    有字符位置只证明"在某段文本里找到"，不证明主体、期间与资料截止成立。
    返回 `{validation_status, admission, subject, subject_state, document_period,
    published_at, published_precision, excluded}`。

    - `admission` 是**明确准入状态**：`admitted`（可当证据）/`comparison`（比较披露）/
      `unknown`（缺字段，待核查，**不得当已核实证据**）/`excluded`（明确不适用）。
    - 缺失字段独立记 unknown，**不反向用请求身份填实来源**：`subject` 只写文档自身
      可核验到的身份（本批无法核验 → 空串），不再回填请求公司。
    - 日期按**完整精度**比较；只有年月/年份时精度另记，按保守方式处理（可能晚于
      资料截止的，判 unknown 而不是 applicable）。
    """
    years = [int(y) for y in (periods or [])]
    subject_state = _subject_state(doc, company, company_id)
    doc_period = _doc_period(str(doc.get("title") or ""), str(doc.get("url") or ""))
    raw_pub = _published_at(doc)
    parts = _date_parts(raw_pub)
    pub, prec = (_norm_date(raw_pub), parts[1]) if parts else ("", "")
    cutoff = _date_parts(as_of)
    status = "applicable"
    if subject_state == "mismatch":
        status = "subject_mismatch"
    elif pub and prec == "day" and cutoff:
        # 完整日期比较：晚于资料截止日 → 排除（4/20 > 4/15）
        status = "after_as_of" if parts[0] > cutoff[0] else "applicable"
    elif pub and prec in ("month", "year"):
        # 精度不足：只知年月/年份时不能断言"未晚于截止"（可能晚于），记 unknown
        if cutoff and (parts[0][:2] > cutoff[0][:2] if prec == "month"
                       else parts[0][:1] > cutoff[0][:1]):
            status = "after_as_of"
        else:
            status = "unknown_published_at"
    elif not pub:
        # 没有发布日（如只有报告期）：不当作已核实时点
        status = "unknown_published_at"
    if status == "applicable":
        if doc_period and years and int(doc_period) > max(years):
            status = "period_after_contract"
        elif doc_period and years and int(doc_period) < min(years):
            # 早于契约期间的文档：若证据段落里出现契约期间，则算**比较披露**（允许）；
            # 否则期间不适用（不按"只含最新年份"粗暴拒绝）
            snip = str(rec.get("snippet") or "") + str(rec.get("section") or "")
            status = ("comparison" if any(str(y) in snip for y in years)
                      else "period_before_contract")
        elif not doc_period:
            # 新闻/解读类页面标题里通常没有"报告期"：**缺项记未标注**，不据此排除——
            # 发布日与主体都已核实，期间由读者按小节/段落自行判断（实机：21财经 2024-05-14
            # 的行业观察文章因"期间未标注"被挡在证据之外）
            status = "period_unstated"
    elif status == "unknown_published_at" and doc_period and years \
            and int(doc_period) > max(years):
        # 发布日未知但报告期已晚于契约期间：明确不适用（这一条不依赖发布日）
        status = "period_after_contract"
    # 主体未识别：即使期间/日期没问题，也不能当"已验证支持本期变化"的证据
    if status == "applicable" and subject_state != "ok":
        status = "unknown_subject"
    excluded = status in ("subject_mismatch", "after_as_of", "period_after_contract",
                          "period_before_contract")
    admission = ("excluded" if excluded
                 else "admitted" if status in ("applicable", "period_unstated")
                 else "comparison" if status == "comparison" else "unknown")
    return {"validation_status": status, "admission": admission,
            "subject": "", "subject_state": subject_state,
            "document_period": doc_period, "published_at": pub,
            "published_precision": prec, "excluded": excluded}


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


def _demote_change_target(best_score: int, source: str = "") -> str | None:
    """"命中变化关键词但无因果语言"的降级目标：发行人文件 → 附注（数字出处）；
    第三方（新闻/解读）→ 业务背景。不能一律叫"财务附注"——实机里 21 财经的业绩
    说明会报道被标成财务附注（读者会以为它是报表附注）；它实际是经营背景。
    来源未知（空串）沿用旧口径（附注）：调用方应尽量把 `source_type` 传进来。
    """
    if best_score <= 0:
        return None
    if source and "issuer" not in str(source).lower():
        return KIND_BACKGROUND
    return KIND_NOTES


def classify(title: str, body: str = "", *, source: str = "") -> str | None:
    """按关键词给小节归类；标题命中权重 3、正文（前 200 字）命中权重 1。

    C2-3：归为"变化解释"的小节必须有**因果语言**、且不是会计政策套话——标题写着
    "经营情况讨论与分析"、内容却是准则/政策声明的段落不能当变化原因。
    `source`：文档来源类型（`source_type()` / `document_provenance()` 的值），
    决定"无因果的变化提及"降级为附注还是业务背景（见 `_demote_change_target`）。
    """
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
    if best == KIND_CHANGE and (is_policy_text(body) or not is_causal(body)):
        return _demote_change_target(best_score, source)
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


# 散文体关键词（段落模式专用）：新闻/解读类页面的正文不会写"经营情况讨论与分析"
# 这类标题词，而是"营业收入同比下滑""渠道去库存""合同负债"这种叙述。标题模式仍用
# 上面那套**标题词**（保持标题判定的精度），只有段落回退时用这套。
PROSE_KEYWORDS: dict[str, tuple[str, ...]] = {
    KIND_BACKGROUND: ("主营业务", "主要业务", "产品结构", "品牌", "行业竞争", "市场格局",
                      "渠道", "经销商", "客户", "产能", "经营模式", "市场份额"),
    KIND_CHANGE: ("营业收入同比", "净利润同比", "收入同比", "同比增长", "同比下滑",
                  "同比下降", "业绩说明会", "经营业绩", "营收", "净利", "动销", "去库存",
                  "提价", "价格调整"),
    KIND_NOTES: ("合同负债", "预收款", "应收账款", "存货", "经销商库存", "现金流",
                 "销售费用", "管理费用", "研发投入", "税金", "分红", "派息"),
    KIND_RISK: ("风险", "不确定性", "竞争加剧", "压力", "下滑", "挑战", "承压", "库存高企"),
}


# 付费墙/未登录占位：这类文字不是证据（实机：前瞻眼页字段显示"会员可见"）
_NO_CONTENT_MARKERS = ("会员可见", "登录后可见", "请登录", "订阅后", "付费可见",
                       "暂无数据", "加载中", "内容不存在")

# 因果语言：**变化解释**必须说出"为什么变"，只命中"同比/现金流"关键词的段落不算。
# 实机教训：会计政策声明、仅重复现金流数字的附注都能命中关键词，被当成"经营变化原因"
# 写进简报（读者会以为管理层解释过了）。
CAUSAL_MARKERS = ("主要系", "所致", "由于", "原因", "影响", "导致", "带动", "推动",
                  "得益于", "拖累", "拉动", "主要是", "归因于", "源于", "使得", "从而")
# 会计政策/准则套话：编制口径声明，不是经营变化原因
_POLICY_MARKERS = ("会计政策", "会计估计", "企业会计准则", "准则第", "采用修订",
                   "修订后的", "财政部", "追溯调整", "编制基础",
                   "计量属性", "确认与计量", "重要会计政策")


def is_causal(text: str) -> bool:
    """段落里有没有"为什么变"的语言（无因果词 = 不是变化解释）。"""
    body = str(text or "")
    return any(k in body for k in CAUSAL_MARKERS)


def is_policy_text(text: str) -> bool:
    """会计政策/准则套话（编制口径声明，不能冒充经营变化原因）。"""
    body = str(text or "")
    hits = sum(1 for k in _POLICY_MARKERS if k in body)
    return hits >= 2 or (hits >= 1 and "会计" in body)


def _prose_classify(body: str, *, source: str = "") -> str | None:
    """段落模式归类：按**散文体关键词**打分（命中数最多者胜，平手按 KIND_ORDER）。

    C2-3：命中"变化"关键词不等于解释——会计政策声明、仅重复数字的段落一律**不归为
    变化解释**（降级目标按来源分：发行人文件 → 附注，第三方 → 业务背景，
    见 `_demote_change_target`；附注也不能当解释用，见 `report_brief._change_explanation`）。
    """
    text = str(body or "")[:400]
    best: str | None = None
    best_score = 0
    for kind in KIND_ORDER:
        score = sum(1 for kw in PROSE_KEYWORDS.get(kind, ()) if kw in text)
        if score > best_score:
            best, best_score = kind, score
    if best == KIND_CHANGE and (is_policy_text(text) or not is_causal(text)):
        # 无因果语言 → 只是"提到了变化"；政策套话 → 是编制口径声明。
        # 两者都不得作为变化原因进入简报；降级目标见 _demote_change_target。
        return _demote_change_target(best_score, source)
    return best


def _paragraph_records(doc: dict, *, periods=None, company: str = "",
                       company_id: str = "", as_of: str = "",
                       max_per_kind: int = MAX_PER_KIND,
                       snippet_chars: int = SNIPPET_CHARS) -> list[dict]:
    """**无小节标题**的页面回退：按段落窗口取证据（新闻/解读类页面常见）。

    为什么需要：实机里 21 财经那篇 2024-05-14 的行业观察（主体命中、发布日在截止内）
    因为整页没有标题行而产出 0 条证据，简报只能写"未取得"——而它确实含可用的经营信息。
    段落模式下定位写成"段落 N（字符 a-b）"（有页码时带页码），与标题模式同样可回溯。
    """
    text = str((doc or {}).get("text") or "")
    if not text:
        return []
    url = str(doc.get("url") or "")
    title = str(doc.get("title") or "")
    stype = source_type(url)
    _prov = document_provenance(doc, company, company_id) or stype
    page_offsets = doc.get("page_offsets")
    years = [str(y) for y in (periods or [])]
    picked: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    pos = 0
    for n, raw in enumerate(re.split(r"\n\s*\n", text), 1):
        start = text.find(raw, pos) if raw else pos
        if start < 0:
            start = pos
        pos = start + len(raw)
        body = " ".join(str(raw or "").split())
        if len(body) < 25:          # 太短的片段（导航/页脚/一句话标题）不作证据
            continue
        if any(k in body for k in _NO_CONTENT_MARKERS):
            continue          # 付费墙/未登录占位："会员可见"不是证据
        kind = _prose_classify(body, source=_prov)
        if not kind:
            continue
        snip = _snippet(body, snippet_chars)
        key = (kind, snip[:60])
        if key in seen:
            continue
        seen.add(key)
        bucket = picked.setdefault(kind, [])
        if len(bucket) >= max_per_kind:
            continue
        page = _page_of(start, page_offsets)
        loc = (f"第 {page} 页 · 段落 {n}（字符 {start}-{start + len(raw)}）" if page
               else f"段落 {n}（字符 {start}-{start + len(raw)}）")
        rec = {
            "kind": kind, "kind_label": KIND_LABELS[kind], "title": title, "url": url,
            "source_type": stype,
            "document_provenance": document_provenance(doc, company, company_id),
            "publisher": publisher_of(url), "section": f"段落 {n}",
            "snippet": snip, "char_start": start, "char_end": start + len(raw),
            "page": page,
            "period_hint": next((y for y in years if y in body), ""),
            "has_location": True, "locator": loc,
            "content_hash": hashlib.sha256(snip.encode("utf-8")).hexdigest()[:16],
            "fetched_at": str(doc.get("fetched_at") or ""),
            "extraction": "paragraph",
        }
        rec.update(validate_record(rec, doc, company=company, company_id=company_id,
                                   periods=periods, as_of=as_of))
        bucket.append(rec)
    out: list[dict] = []
    for kind in KIND_ORDER:
        out.extend(picked.get(kind) or [])
    return out


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
    _prov = document_provenance(doc, company, company_id) or stype
    years = [str(y) for y in (periods or [])]
    page_offsets = doc.get("page_offsets")
    picked: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for sec in split_sections(text, page_offsets=page_offsets):
        kind = classify(sec["title"], sec["body"], source=_prov)
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
            "document_provenance": document_provenance(doc, company, company_id),
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
    if not out:
        # 整页没有可分类的小节标题（新闻/解读类页面常见）→ 段落窗口回退
        return _paragraph_records(doc, periods=periods, company=company,
                                  company_id=company_id, as_of=as_of,
                                  max_per_kind=max_per_kind,
                                  snippet_chars=snippet_chars)
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


def admission_of(url: str, *, evidence: dict | None = None,
                 fetched_urls=(), known_urls=()) -> str:
    """来源准入的**唯一规则**（结构化事实/叙事证据/正文引用/上传材料共用）。

    返回 `admitted | comparison | unknown | excluded`：

    - 叙事证据里已判定的结果优先（同一 URL 在不同入口必须同一结论）；
    - 出现在已抓取正文里（`fetch_snapshot.json`）但未取得定位 → `unknown`
      （只罗列、未核验，不得显示为"已采用"）；
    - 只在检索候选里出现过、从未抓取 → `unknown`；
    - 其余（结构化来源、用户材料）由调用方按自身身份判定后传入，这里返回 `unknown`。
    """
    u = str(url or "").strip()
    if not u:
        return "unknown"
    for r in ((evidence or {}).get("records") or []):
        if str(r.get("url") or "") == u:
            adm = str(r.get("admission") or "")
            if adm:
                return adm
            return "excluded" if r.get("excluded") else "unknown"
    if u in set(fetched_urls or ()):
        return "unknown"
    if u in set(known_urls or ()):
        return "unknown"
    return "unknown"


def source_registry(urls, *, evidence: dict | None = None,
                    fetched_urls=(), known_urls=()) -> dict[str, str]:
    """`{url: admission}` 登记表：全入口共享同一份准入结论。"""
    out: dict[str, str] = {}
    for u in urls or ():
        s = str(u or "").strip()
        if s and s not in out:
            out[s] = admission_of(s, evidence=evidence, fetched_urls=fetched_urls,
                                  known_urls=known_urls)
    return out


def excluded_urls(evidence: dict | None = None) -> set[str]:
    """已明确排除的 URL（任何入口都不得重新准入）。"""
    return {str(r.get("url") or "") for r in ((evidence or {}).get("records") or [])
            if r.get("admission") == "excluded" or r.get("excluded")}


def _cached_docs(task_id: str, *, project=None) -> list[dict]:
    """缓存里留下的历史页面正文（`fetch_snapshot.json`）——规则版本变化时按新规则重算用。"""
    try:
        docs, _snips = _read_inputs(task_id, project)
        return [d for d in docs if str(d.get("text") or "").strip()]
    except Exception:
        return []


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
        _surl = str(s.get("url") or "")
        kind = classify(title, body, source=source_type(_surl))
        if not kind:
            continue
        url = str(s.get("url") or "")
        # D1：同一 URL 已有**带定位**的正文证据时，不再登记它的检索摘要——一个 URL
        # 不能靠"摘要复制"多算一条覆盖（实机：同一新闻 URL 两条记录，located 被抬到 2）
        if url and any(str(d.get("url") or "") == url for d in docs):
            continue
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
    prev_payload = read(task_id, ws_dir=ws_dir) or {}
    prev_rules = str(prev_payload.get("rules_version") or "")
    merged: list[dict] = list(records)
    if prev_rules == RULES_VERSION:
        # 同一规则版本：保留上一轮来自别的页面的证据（同一 URL 以本轮重新抽取为准）
        for r in prev_payload.get("records") or []:
            if not r.get("has_location") or str(r.get("url") or "") in seen_urls:
                continue
            merged.append(r)
    elif prev_payload:
        logger.info("叙事证据缓存规则版本不同（%s→%s，task=%s）：按最新规则重算",
                    prev_rules or "无", RULES_VERSION, task_id)
        for doc in _cached_docs(task_id, project=project):
            if str(doc.get("url") or "") in seen_urls:
                continue
            try:
                merged.extend(extract_sections(dict(doc, fetched_at=fetched_at),
                                               periods=years, company=company,
                                               company_id=company_id, as_of=as_of))
            except Exception:                    # noqa: BLE001 - 单篇失败不影响其余
                continue
    dedup: dict[tuple, dict] = {}
    for r in merged:
        key = (str(r.get("kind")), str(r.get("url")), str(r.get("section")),
               str(r.get("snippet"))[:60])
        dedup.setdefault(key, r)
    ordered: list[dict] = []
    for kind in KIND_ORDER:
        bucket = [r for r in dedup.values()
                  if r.get("kind") == kind and r.get("has_location")
                  and r.get("admission") in ("admitted", "comparison")]
        bucket.sort(key=lambda r: (str(r.get("url") or ""), int(r.get("char_start") or 0)))
        ordered.extend(bucket[:MAX_PER_KIND])
    # 非准入记录（错主体/超截止/期间不符/缺字段）与无定位的检索摘要照实保留：
    # 前者用于向读者说明"为什么这些材料没被采用"，不静默丢弃
    ordered.extend(r for r in dedup.values()
                   if not r.get("has_location")
                   or r.get("admission") not in ("admitted", "comparison"))
    records = ordered
    # D1：`located` 只数**已准入且有可复核正文位置**的片段，并按 (url, 片段指纹) 去重；
    # 检索摘要（has_location=False）另计 `snippet_hints`，只作线索——
    # 它既不补"已取得定位"的数量，也不清除"某类证据缺失"。
    located_records: list[dict] = []
    _seen_loc: set[tuple[str, str]] = set()
    for r in records:
        if not r.get("has_location"):
            continue
        if str(r.get("admission") or "") not in ("admitted", "comparison"):
            continue
        key = (str(r.get("url") or ""), str(r.get("content_hash") or ""))
        if key in _seen_loc:
            continue
        _seen_loc.add(key)
        located_records.append(r)
    snippet_hints = [r for r in records if not r.get("has_location")]
    missing = [k for k in KIND_ORDER
               if not any(r["kind"] == k for r in located_records)]
    sources: list[dict] = []
    for r in records:
        if not r.get("url") or any(s["url"] == r["url"] for s in sources):
            continue
        sources.append({"url": r["url"], "title": r.get("title") or "",
                        "source_type": r.get("source_type") or "third_party",
                        "publisher": r.get("publisher") or "",
                        "admission": r.get("admission") or "unknown",
                        "has_location": bool(r.get("has_location"))})
    excluded = [{"url": r.get("url") or "", "title": r.get("title") or "",
                 "validation_status": r.get("validation_status") or "",
                 "admission": r.get("admission") or "",
                 "document_period": r.get("document_period") or "",
                 "published_at": r.get("published_at") or "",
                 "published_precision": r.get("published_precision") or "",
                 "locator": r.get("locator") or ""}
                for r in records if r.get("admission") not in ("admitted", "comparison")]
    payload = {
        "ok": bool(located_records),
        "company": company,
        "company_id": company_id,
        "as_of": as_of,
        "periods": years,
        "rules_version": RULES_VERSION,
        "records": records,
        "missing_kinds": missing,
        "missing_labels": [KIND_LABELS[k] for k in missing],
        "sources": sources,
        "excluded": excluded[:12],
        "docs": len(docs),
        # `located`：已准入 + 有正文位置 + 去重后的**片段数**（不数摘要、不数来源）；
        # `snippet_hints`：只有检索摘要、没有正文定位的线索数（不计入覆盖）。
        "located": len(located_records),
        "snippet_hints": len(snippet_hints),
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
