"""研究意图/契约状态（P0-C）：一般任务、待补契约的研究、范围内研究、范围外请求。

背景（2026-10-22 运行诊断 P0-4，实机反例）：

- 伊利、海天、传奇影业三轮自由文本提交的公司研究都没有落库
  `research_request_json`，`research_state.json` 全部记成 `not_applicable`
  （"非研究任务"），官方发现也返回 `no_contract`——研究质量检查因此**失去适用对象**，
  与机器验收 pass 并存，看起来"研究完成"；
- 传奇影业（非 A 股）这类范围外请求被隐式当成普通生成任务跑完。

这里的裁决（确定性、零 LLM）：

- `intent`：`general`（一般任务）／`company_research`（公司研究请求）／
  `out_of_scope`（范围外请求）；
- `status`：`not_applicable`（一般任务：合法地不做研究检查）／`needs_contract`
  （研究请求但契约缺字段：待补，**不得绕过研究检查**）／`in_scope`（范围内）／
  `out_of_scope`（范围外：给边界说明与人工材料路径，不装成范围内研究）；
- 契约解析复用 `facts.parse_research_request`（同一 schema、同一缺口清单），
  **不猜主体**：识别不到公司/代码就列缺口请用户确认；
- **不回填**：历史无契约任务只读标明原因与显式修复建议（`backfill="not_attempted"`），
  不悄悄补写原始契约或历史状态；
- 合法普通任务继续 `not_applicable`，行为与旧行为完全一致。
"""

from __future__ import annotations

import re

INTENT_GENERAL = "general"
INTENT_COMPANY_RESEARCH = "company_research"
INTENT_OUT_OF_SCOPE = "out_of_scope"

STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_NEEDS_CONTRACT = "needs_contract"
STATUS_IN_SCOPE = "in_scope"
STATUS_OUT_OF_SCOPE = "out_of_scope"

SCHEMA_ID = "research_request@1"

# 公司研究请求的信号：研究动词 + 公司名形态 / 证券代码 / 披露与财务词汇。
# 这只是**意图识别**（决定要不要研究检查），不是主体认定（那是 parse 的职责）。
_RESEARCH_VERBS = ("研究", "分析", "评估", "看看", "梳理", "对比", "跟踪", "调研")
_DISCLOSURE_WORDS = ("年报", "年度报告", "半年报", "季报", "财报", "披露", "公告",
                     "营收", "营业收入", "净利润", "现金流", "业绩", "毛利率",
                     "两期", "同比", "经营")
_CODE_RE = re.compile(r"(?<!\d)(?:600|601|603|605|000|001|002|003|300|301|688|689)\d{3}(?!\d)")
# 带市场后缀的代码（600887.SH / 00700.HK / AAPL.US）
_CODE_MARKET_RE = re.compile(r"\b\d{6}\.(sh|sz|bj)\b|\b\d{4,5}\.hk\b|\b[a-z]{1,5}\.us\b", re.I)
_ENTITY_HINT_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:股份|集团|控股|有限公司|公司|银行|证券|保险|乳业|药业|科技|能源|汽车|电器|钢铁|食品|酒业|影业|传媒|电子|医药|航空)")
# 金融行业（首期不做）：名称或请求里出现这些形态 → 范围外边界
_FIN_INDUSTRY_RE = re.compile(r"(银行|证券|保险|信托|券商|基金|期货|金融租赁|消费金融)")
# 非 A 股市场信号
_NON_CN_MARKET_RE = re.compile(
    r"(港股|港交所|H股|美股|纳斯达克|纽交所|nasdaq|nyse|10-K"
    r"|\b\d{4,5}\.hk\b|\b[a-z]{1,5}\.us\b)", re.I)
_A_SHARE_HINT_RE = re.compile(r"(A股|沪深|上交所|深交所|沪市|深市|创业板|科创板|主板)")
# 保守公司名：**只在带企业形态后缀时**认（"伊利股份/中国平安集团"），
# 裸名（"贵州茅台"）留给用户确认——不猜身份。
_CONSERVATIVE_NAME_RE = re.compile(
    r"[\u4e00-\u9fff]{2,10}(?:股份|集团|控股|有限公司|乳业|药业|酒业|科技|电器|汽车|钢铁|食品|影业|传媒|电子)")
_CN_CODE_RE = re.compile(r"(?<!\d)(?:600|601|603|605|000|001|002|003|300|301|688|689)\d{3}(?!\d)")


def _identity_from_text(text: str) -> tuple[str, str, str]:
    """自由文本里的**可证身份**：`(公司名, 代码, 市场)`，取不到就是空串。

    只认两类可证事实：6 位 A 股代码（前缀确定市场）与带企业后缀的公司名；
    带市场后缀的代码（`00700.HK` / `AAPL.US`）同样确定性可读。
    """
    s = str(text or "")
    name = ""
    m = _CONSERVATIVE_NAME_RE.search(s)
    if m:
        cand = m.group(0)
        for verb in _RESEARCH_VERBS:
            if cand.startswith(verb) and len(cand) > len(verb) + 2:
                cand = cand[len(verb):]
        name = cand
    code, market = "", ""
    m = re.search(r"\b(\d{4,5})\.HK\b", s, re.I)
    if m:
        code, market = m.group(0).upper(), "hk"
    else:
        m = re.search(r"\b([A-Z]{1,5})\.US\b", s, re.I)
        if m:
            code, market = m.group(0).upper(), "us"
        else:
            m = _CN_CODE_RE.search(s)
            if m:
                code, market = m.group(0), "cn"
    if market == "cn" and _NON_CN_MARKET_RE.search(s):
        market = ""                          # 混写（"A股 vs 美股"）不认市场
    return name, code, market


def _extract_code(text: str) -> str:
    m = re.search(r"(?<!\d)\d{6}(?!\d)", str(text or ""))
    return m.group(0) if m else ""


def is_company_research(goal: str) -> bool:
    """自由文本是否体现**公司研究请求**（意图识别，不认定主体）。"""
    text = str(goal or "").strip()
    if not text:
        return False
    if _CODE_RE.search(text) or _CODE_MARKET_RE.search(text):
        return True
    if any(w in text for w in _DISCLOSURE_WORDS):
        return True
    verb = any(v in text for v in _RESEARCH_VERBS)
    return bool(verb and _ENTITY_HINT_RE.search(text))


def _scope_boundary(fields: dict, goal: str) -> tuple[str, str]:
    """范围外判别：返回 `(status, boundary)`；范围内返回 `("", "")`。

    首期边界：**A 股非金融公司、两个年度**。触边的请求如实说明边界，
    并给出人工路径，不把范围外请求装成范围内研究。
    """
    text = str(goal or "")
    market = str(fields.get("market") or "")
    company = str(fields.get("company") or "")
    name_blob = f"{company} {text}"
    if _NON_CN_MARKET_RE.search(name_blob) or market in ("hk", "us"):
        where = {"hk": "港股", "us": "美股"}.get(market, "非 A 股市场")
        return (STATUS_OUT_OF_SCOPE,
                f"首期只支持 A 股上市公司研究；本请求指向{where}（{company or '未具名主体'}）。"
                "可以提供该主体的披露材料由系统整理，但不在自动研究的首期范围内")
    if _FIN_INDUSTRY_RE.search(name_blob):
        return (STATUS_OUT_OF_SCOPE,
                f"首期只支持 A 股**非金融**公司研究；「{company or text[:20]}」属于金融机构，"
                "其报表口径与质量比率不适用企业口径。可按人工材料路径单独评估")
    periods = [int(y) for y in (fields.get("periods") or []) if str(y).isdigit()]
    if len(periods) > 2:
        return (STATUS_OUT_OF_SCOPE,
                f"首期只支持**两个年度**的两期对照（请求了 {len(periods)} 期："
                f"{periods}）。可拆成相邻两期分别研究后再人工汇总")
    return ("", "")


def _questions_for(missing: list[str], fields: dict) -> list[str]:
    """缺口 → 逐项问句（按现有 schema，请用户确认，不代填）。"""
    qs: list[str] = []
    have = set(missing)
    if not fields.get("company") and not fields.get("company_id"):
        qs.append("研究对象是哪家公司？请给出公司全名或 6 位证券代码")
    elif "market" in have and not fields.get("company_id"):
        qs.append("请确认市场与代码（如 600887.SH）；仅凭公司名无法确定交易所")
    if any("年度" in m for m in have) or not fields.get("periods"):
        qs.append("研究哪两个年度？（首期为两期对照，如 2023 与 2024）")
    if any("截至" in m for m in have):
        qs.append("资料截止到哪一天？（如 2025-04-30）")
    if any("口径" in m for m in have):
        qs.append("按合并报表还是母公司口径？")
    return qs


def assess(goal: str, *, stored_request=None, metadata=None) -> dict:
    """裁决一次请求的意图与契约状态。

    返回 `{intent, status, source, fields, missing, questions, boundary, reason,
    repair, backfill, schema}`。**只读**：不写库、不回填、不修改任何状态。
    """
    text = str(goal or "")
    out = {
        "intent": INTENT_GENERAL,
        "status": STATUS_NOT_APPLICABLE,
        "source": "none",            # stored（落库契约）/ text（文本解析）/ none
        "fields": {"company": "", "company_id": "", "market": "", "periods": [],
                   "as_of": "", "caliber": ""},
        "missing": [],
        "questions": [],
        "boundary": "",
        "reason": "",
        "repair": "",
        "backfill": "not_attempted",
        "schema": SCHEMA_ID,
    }

    def _fields_of(req) -> dict:
        return {
            "company": str(getattr(req, "company", "") or ""),
            "company_id": str(getattr(req, "company_id", "") or ""),
            "market": str(getattr(req, "market", "") or ""),
            "periods": [int(y) for y in (getattr(req, "periods", None) or [])],
            "as_of": str(getattr(req, "as_of", "") or ""),
            "caliber": str(getattr(req, "caliber", "") or ""),
        }

    # ① 落库契约（用户表单确认过的）：范围内/范围外按契约判
    persisted: dict | None = None
    if isinstance(stored_request, dict) and stored_request:
        persisted = stored_request
    req = None
    try:
        from facts import ResearchRequest
        req = ResearchRequest.from_payload(persisted) if persisted is not None else None
    except Exception:                            # noqa: BLE001 - 读不到按无契约处理
        req = None
    if req is not None and (req.company or req.company_id or req.periods):
        out["source"] = "stored"
        out["fields"] = _fields_of(req)
        status, boundary = _scope_boundary(out["fields"], req.goal or text)
        if status:
            out["intent"] = INTENT_OUT_OF_SCOPE
            out["status"] = status
            out["boundary"] = boundary
            out["reason"] = "契约齐备但超出首期范围：明确说明边界，不按范围内研究交付"
            return out
        out["intent"] = INTENT_COMPANY_RESEARCH
        out["status"] = STATUS_IN_SCOPE
        out["reason"] = "落库契约齐备：范围内公司研究"
        return out

    # ② 没有落库契约：先识别意图
    if not is_company_research(text):
        out["reason"] = "一般任务：请求未体现公司研究（合法地不做研究检查）"
        return out

    # ③ 公司研究请求（自由文本）：解析文本契约，列缺口，**不猜主体**
    try:
        from facts import parse_research_request
        req = parse_research_request(text, identity_source="text")
    except Exception as exc:                     # noqa: BLE001
        out.update({"intent": INTENT_COMPANY_RESEARCH,
                    "status": STATUS_NEEDS_CONTRACT,
                    "reason": f"研究请求解析失败：{str(exc)[:80]}",
                    "repair": "用研究表单提交公司/期间/口径后再跑"})
        return out
    out["source"] = "text"
    # 解析器（`facts`）负责 schema 与缺口口径；它认不出的**可证身份**（带企业后缀的
    # 公司名、6 位 A 股代码、带市场后缀的代码）在这里保守补齐——只补证据里确实写着
    # 的东西，裸名（"贵州茅台"）不猜，留给用户确认。
    _name, _code, _mkt = _identity_from_text(text)
    try:
        if _name and not str(getattr(req, "company", "") or ""):
            req.company = _name
            req.identity_source = req.identity_source or "text"
        if _code and not str(getattr(req, "company_id", "") or ""):
            req.company_id = _code
        if _mkt and str(getattr(req, "market", "") or "") in ("", "unknown"):
            req.market = _mkt
    except Exception:                            # noqa: BLE001 - 补不上就按缺口走
        pass
    fields = _fields_of(req)
    gaps = [str(g) for g in (getattr(req, "gaps", None) or [])]

    def _resolved(gap: str) -> bool:
        if "未识别到公司" in gap:
            return bool(fields["company"] or fields["company_id"])
        if "未确定市场" in gap:
            return fields["market"] in ("cn", "hk", "us")
        return False

    missing = [g for g in gaps if not _resolved(g)]
    out["fields"] = fields
    status, boundary = _scope_boundary(fields, text)
    if status:
        out["intent"] = INTENT_OUT_OF_SCOPE
        out["status"] = status
        out["boundary"] = boundary
        out["reason"] = ("自由文本请求超出首期范围：给出边界说明与人工材料路径，"
                         "不按范围内研究交付")
        out["repair"] = ("如需整理该主体的披露材料：提供原始文件走人工材料通道；"
                         "或改提 A 股非金融公司的两期研究")
        return out
    out["intent"] = INTENT_COMPANY_RESEARCH
    out["missing"] = missing
    out["questions"] = _questions_for(missing, out["fields"])
    out["status"] = STATUS_NEEDS_CONTRACT if missing else STATUS_IN_SCOPE
    if missing:
        out["reason"] = "公司研究请求但契约缺字段：待补契约（不得绕过研究检查，也不代填）"
        out["repair"] = ("在研究表单里确认缺失字段后重新提交；提交前系统只做检索类"
                         "一般处理，不宣称完成研究")
    else:
        out["reason"] = "自由文本研究请求且字段可解析：按文本契约执行（身份来源 text，未表单确认）"
    return out


def describe(assessment: dict) -> str:
    """一句话人读描述（日志/回执用）。"""
    a = assessment or {}
    bits = [str(a.get("intent") or "?"), str(a.get("status") or "?")]
    if a.get("boundary"):
        bits.append(str(a["boundary"])[:80])
    elif a.get("missing"):
        bits.append(f"缺 {len(a['missing'])} 项：{('、'.join(map(str, a['missing'][:2])))}")
    return " / ".join(bits)
