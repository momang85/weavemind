"""来源准入（P0-C）：候选 → 待核验 → 已准入／已拒绝，**一条判据给所有入口用**。

背景（2026-10-22 运行诊断 P0-3，实机反例）：

- 任务 `ui-631efe6983`（传奇影业）的抓取步实际拿到国家统计局《2025 年国民经济和
  社会发展统计公报》——政策/宏观材料，与"某公司两期经营"没有主体关系；
- 任务 `ui-b23642e5de`（伊利股份 600887）的抓取步实际拿到 SKF `6008` 深沟球轴承
  产品页，正文只有 `SKF`；
- 两例都是**域名权威、HTTP 200、正文非空**，然后被当作"联网研究过"的证据。

因此这里把准入判据一次讲清，并让候选选择（抓取前）、来源桶（报告/导出）、
下游上下文（喂模型）**共用同一结论**，不在三处各写一套：

1. **主体**：标题/正文出现本主体名或证券代码 → 命中；只出现别家主体形态 → 明确不符；
   都没有 → 未核验（不是"不符"，也不是"命中"）。
2. **期间**：文档自身报告期（"2024 年年度报告"）不在契约期间 → 不适用；
   晚于契约期间 → 不适用；缺报告期 → 未标注（不据此排除，也不据此准入）。
3. **资料截止日**：发布日晚于 `as_of` → 不适用；发布日未知 → 未核验。
4. **文档类型/用途**：`company_financial`（本公司披露/财务解读）、`company_business`
   （本公司经营新闻）、`macro_background`（宏观/统计公报）、`industry_background`
   （行业/产业研究）。**宏观与行业材料只能作背景，不能当公司财务证据**——
   这条与主体是否提及无关，是用途判据。
5. **元数据不足 → 有界验证**（`pending`）：先抓一次正文再判；核验后仍无本主体/代码、
   也无契约期间内的财务披露标记 → 记 `rejected`（"非本公司披露"），
   **不**把未知材料一律杀光，也**不**把未知材料当已准入。

判据本身不新造解析：主体/期间/发布日/截止日全部复用 `narrative_evidence`
（`validate_record` 是证据层同一套规则），这里只补"用途"和"阶段"两件事。
"""

from __future__ import annotations

import re

STAGE_CANDIDATE = "candidate"      # 还没判（无契约主体等）
STAGE_PENDING = "pending"          # 元数据不足：需抓正文做有界核验
STAGE_ADMITTED = "admitted"        # 可当证据
STAGE_REJECTED = "rejected"        # 明确不适用

PURPOSE_COMPANY_FINANCIAL = "company_financial"
PURPOSE_COMPANY_BUSINESS = "company_business"
PURPOSE_MACRO = "macro_background"
PURPOSE_INDUSTRY = "industry_background"
PURPOSE_UNKNOWN = "unknown"

PURPOSES = (PURPOSE_COMPANY_FINANCIAL, PURPOSE_COMPANY_BUSINESS, PURPOSE_MACRO,
            PURPOSE_INDUSTRY, PURPOSE_UNKNOWN)
STAGES = (STAGE_CANDIDATE, STAGE_PENDING, STAGE_ADMITTED, STAGE_REJECTED)

# 用途线索（**形态学/通用词**，不针对具体公司、不列站点黑名单）
_MACRO_MARKERS = (
    "国民经济", "统计公报", "国家统计局", "统计局", "宏观", "国内生产总值", "gdp",
    "cpi", "ppi", "社会消费品零售", "固定资产投资", "工业增加值", "货币供应",
    "货币政策", "财政政策", "国务院", "五年规划", "经济普查",
)
_INDUSTRY_MARKERS = (
    "行业", "产业", "景气", "pmi", "渗透率", "竞争格局", "供应链", "上游", "下游",
    "赛道", "市场规模", "行业研究", "行业观察", "板块", "产能", "集中度",
)
_FINANCIAL_MARKERS = (
    "年报", "年度报告", "半年报", "季报", "财报", "营业收入", "净利润", "现金流",
    "资产负债", "毛利率", "业绩", "业绩预告", "分红", "披露", "审计报告",
    "利润总额", "所有者权益", "每股收益",
)
# 明确不是"公司披露"的页面形态（产品页/下载页/词典/导航）——只用于"核验后仍无主体"
# 时的证据补强，不单独作为剔除理由
_NON_DISCLOSURE_MARKERS = ("产品", "型号", "轴承", "词典", "汉语", "下载", "登录",
                           "首页", "导航", "报价单", "参数")

# 这些校验状态是**明确不适用**（不是"判不准"）
_HARD_REJECT_STATUS = {
    "subject_mismatch": "主体不符：出现别家主体形态且不含本主体/本代码",
    "after_as_of": "发布日晚于资料截止日",
    "period_after_contract": "报告期晚于契约期间",
    "period_before_contract": "报告期早于契约期间（只能作比较/背景，不能当本期证据）",
}
_SOFT_STATUS = {
    "unknown_subject": "主体未核验",
    "unknown_published_at": "发布日未知",
    "period_unstated": "报告期未标注",
    "applicable": "主体与期间均适用",
    "comparison": "比较披露",
}


def contract_fields(contract) -> dict:
    """契约 → 四个判据字段；接受 `ResearchRequest`、dict、None。"""
    if contract is None:
        return {"company": "", "company_id": "", "periods": [], "as_of": ""}
    if isinstance(contract, dict):
        src = contract
    else:                                        # ResearchRequest 一类对象
        src = {k: getattr(contract, k, "") for k in
               ("company", "company_id", "periods", "as_of")}
    periods: list[int] = []
    for y in src.get("periods") or []:
        s = str(y).strip()
        if s.isdigit():
            periods.append(int(s))
    return {
        "company": str(src.get("company") or "").strip(),
        "company_id": str(src.get("company_id") or "").strip(),
        "periods": sorted(set(periods)),
        "as_of": str(src.get("as_of") or "").strip(),
    }


def _blob(candidate: dict) -> str:
    return " ".join(str(candidate.get(f) or "") for f in
                    ("title", "snippet", "summary", "url", "content", "text")).lower()


def classify_purpose(candidate: dict, *, subject_ok: bool = False) -> str:
    """文档用途：本公司财务／本公司经营／宏观背景／行业背景／未知。

    宏观/行业判据**优先于**主体命中：一份"统计公报"即使顺带提到公司名，也不能当
    公司财务证据（它的用途是宏观背景）。这是用途判据，与站点信誉无关。
    """
    blob = _blob(candidate)
    financial = any(k in blob for k in _FINANCIAL_MARKERS)
    if any(k in blob for k in _MACRO_MARKERS):
        return PURPOSE_COMPANY_FINANCIAL if (subject_ok and financial) else PURPOSE_MACRO
    if subject_ok:
        return PURPOSE_COMPANY_FINANCIAL if financial else PURPOSE_COMPANY_BUSINESS
    if any(k in blob for k in _INDUSTRY_MARKERS):
        return PURPOSE_INDUSTRY
    return PURPOSE_UNKNOWN


def _doc_of(candidate: dict, body: str = "") -> dict:
    text = str(body or "").strip()
    if not text:
        text = " ".join(str(candidate.get(f) or "") for f in ("snippet", "summary", "content"))
    return {"title": str(candidate.get("title") or ""),
            "url": str(candidate.get("url") or candidate.get("href") or ""),
            "text": text}


def _norm_subject_hit(doc: dict, company: str, code: str, *,
                      issuer_form_only: bool = False) -> bool:
    """**归一后**的主体命中：全称/简称互认（"伊利股份" ⊆ "内蒙古伊利实业集团
    股份有限公司"），复用 `facts._norm_name` 的噪声剥离，不做任何猜测。

    `issuer_form_only`：把**误判 mismatch**的文档救回来时额外要求"股份有限公司"
    （上市主体全称形态）——"江苏洋河集团有限公司"这类**集团/母公司**名同样含
    简称核心，但它是另一个法人主体（实机 ui-750185076a 的集团评级报告），
    没有"股份有限公司"就不能当上市主体全称。
    只用于把"unknown/mismatch → ok"**升级**（减少误杀），从不制造 mismatch。"""
    try:
        from facts import _norm_name, bare_code
        title_text = str(doc.get("title") or "") + " " + str(doc.get("text") or "")
        blob = _norm_name(title_text)
        comp = _norm_name(str(company or ""))
        if not (comp and len(comp) >= 2 and comp in blob):
            return False
        if issuer_form_only and "股份有限公司" not in str(doc.get("title") or ""):
            return False
        return True
    except Exception:                            # noqa: BLE001 - 归一不可用按原判
        pass
    return False


def _recheck_date_period(res: dict, doc: dict, fields: dict) -> str:
    """主体升级为"已核验"后，**独立复算**期间/截止日（validate_record 的状态里
    subject_mismatch 抢占了日期判断，升级后必须补算，不能拿 applicable 蒙混）。"""
    try:
        import narrative_evidence as ne
        pub = str(res.get("published_at") or "")
        prec = str(res.get("published_precision") or "")
        if pub and prec == "day" and fields["as_of"]:
            parts = ne._date_parts(pub)
            cut = ne._date_parts(fields["as_of"])
            if parts and cut and parts[0] > cut[0]:
                return "after_as_of"
        dp = str(res.get("document_period") or "")
        periods = [int(y) for y in (fields.get("periods") or [])]
        if dp and periods:
            y = int(dp)
            if y > max(periods):
                return "period_after_contract"
            if y < min(periods):
                return "period_before_contract"
    except Exception:                            # noqa: BLE001 - 复算失败按 applicable
        pass
    return "applicable"


def admit(candidate: dict, contract=None, *, body: str = "") -> dict:
    """对一条候选给出准入结论（确定性、零网络、零 LLM）。

    返回 `{url, stage, admission, purpose, usable_as, subject_state, document_period,
    published_at, doc_type, reasons, need_body_verify, verified_with_body}`：

    - `stage`：`candidate`（无契约主体，未判）/ `pending`（待核验）/ `admitted` /
      `rejected`；
    - `usable_as`：`evidence`（可当公司证据）/ `background`（只能作背景）/
      `none`（不得使用）/ `unknown`（待核验）；
    - `need_body_verify`：元数据不足，抓一次正文再判才算有界核验完成。
    """
    cand = candidate if isinstance(candidate, dict) else {}
    fields = contract_fields(contract)
    doc = _doc_of(cand, body)
    url = doc["url"]
    reasons: list[str] = []
    out = {
        "url": url,
        "stage": STAGE_CANDIDATE,
        "admission": "unknown",
        "purpose": PURPOSE_UNKNOWN,
        "usable_as": "unknown",
        "subject_state": "unknown",
        "document_period": "",
        "published_at": "",
        "doc_type": "",
        "reasons": reasons,
        "need_body_verify": False,
        "verified_with_body": bool(str(body or "").strip()),
    }
    has_subject = bool(fields["company"] or fields["company_id"])
    try:
        import narrative_evidence as ne
        res = ne.validate_record({"snippet": doc["text"], "section": ""}, doc,
                                 company=fields["company"],
                                 company_id=fields["company_id"],
                                 periods=fields["periods"], as_of=fields["as_of"])
    except Exception as exc:                    # noqa: BLE001 - 判据不可用就"未判定"
        out["reasons"].append(f"准入判据不可用：{str(exc)[:80]}")
        return out
    status = str(res.get("validation_status") or "")
    subject_state = str(res.get("subject_state") or "unknown")
    if subject_state == "unknown" and _norm_subject_hit(doc, fields["company"],
                                                        fields["company_id"]):
        # 全称/简称归一后命中：主体**已核验**（这不是猜，是命名归一）
        subject_state = "ok"
        if status == "unknown_subject":
            status = "applicable"
        res["subject_state"] = subject_state
        res["validation_status"] = status
    elif subject_state == "mismatch" and _norm_subject_hit(doc, fields["company"],
                                                           fields["company_id"],
                                                           issuer_form_only=True):
        # **反例修正**（实测）：`_subject_state` 用"出现别家主体形态"判 mismatch，
        # 而本公司**全称**同样带企业后缀（"内蒙古伊利实业集团股份有限公司 2023 年
        # 年度报告"），旧判据把它误判成别家主体 → 正确披露被挡在证据之外。
        # 归一后命中即认定为同一主体，再独立复算期间/截止日（不白白放过）。
        subject_state = "ok"
        status = _recheck_date_period(res, doc, fields)
        res["subject_state"] = subject_state
        res["validation_status"] = status
    out.update({
        "subject_state": subject_state,
        "document_period": str(res.get("document_period") or ""),
        "published_at": str(res.get("published_at") or ""),
        "doc_type": _doc_type(doc),
        "admission": str(res.get("admission") or "unknown"),
    })
    purpose = classify_purpose(doc, subject_ok=(subject_state == "ok"))
    out["purpose"] = purpose

    if not has_subject:
        # 没有契约主体：不做主体判定，也不减损来源（非公司研究的检索列表）
        out["reasons"].append("无契约主体：不做主体/期间准入")
        out["need_body_verify"] = False
        return out

    # ① 用途判据优先：宏观/行业材料不得当公司财务证据（与主体是否提及无关）。
    #    还没抓正文时先核验（宏观报告可能点名本主体）；核验后仍按用途定：只能作背景。
    if purpose in (PURPOSE_MACRO, PURPOSE_INDUSTRY):
        if not out["verified_with_body"]:
            out.update({"stage": STAGE_PENDING, "usable_as": "unknown",
                        "need_body_verify": True})
            out["reasons"].append(
                f"{'宏观/统计' if purpose == PURPOSE_MACRO else '行业/产业'}材料："
                "需抓正文确认是否涉及本主体（有界验证；即便提及也只能作背景）")
            return out
        out.update({"stage": STAGE_REJECTED, "usable_as": "background",
                    "admission": "excluded"})
        out["reasons"].append(
            f"{'宏观/统计' if purpose == PURPOSE_MACRO else '行业/产业'}材料："
            "只能作背景，不能当公司财务证据")
        return out

    # ② 明确不适用（主体不符/晚于截止/期间不符）
    if status in _HARD_REJECT_STATUS:
        out["stage"] = STAGE_REJECTED
        out["usable_as"] = "background" if status == "period_before_contract" else "none"
        out["reasons"].append(_HARD_REJECT_STATUS[status])
        if out["document_period"]:
            out["reasons"].append(f"文档报告期 {out['document_period']}")
        if out["published_at"]:
            out["reasons"].append(f"发布日 {out['published_at']}")
        return out

    # ③ 主体/期间/日期核验结论
    soft = _SOFT_STATUS.get(status, status)
    if status in ("applicable", "period_unstated") and subject_state == "ok":
        out.update({"stage": STAGE_ADMITTED, "usable_as": "evidence"})
        out["reasons"].append(soft)
        return out
    if status == "comparison":
        out.update({"stage": STAGE_ADMITTED, "usable_as": "evidence"})
        out["reasons"].append("比较披露：可作两期对照来源")
        return out

    if subject_state == "unknown":
        if not out["verified_with_body"]:
            out.update({"stage": STAGE_PENDING, "usable_as": "unknown",
                        "need_body_verify": True})
            out["reasons"].append("主体未核验：需抓一次正文后按同一判据复核（有界验证）")
            return out
        # 正文已核验仍无本主体/代码：不是"判不准"，是可证的无关材料
        blob = _blob(doc)
        period_hit = any(str(y) in blob for y in fields["periods"])
        financial = any(k in blob for k in _FINANCIAL_MARKERS)
        if not period_hit and not financial:
            shape = [k for k in _NON_DISCLOSURE_MARKERS if k in blob]
            out.update({"stage": STAGE_REJECTED, "usable_as": "none"})
            out["reasons"].append(
                "正文核验后仍无本主体/本代码，也无契约期间内的财务披露标记"
                + (f"（页面形态：{'、'.join(shape[:3])}）" if shape else "")
                + "：非本公司披露，不作证据")
            return out
        out.update({"stage": STAGE_PENDING, "usable_as": "unknown"})
        out["reasons"].append("正文有期间/财务线索但主体仍无法核验：保留为待核验，不作已准入")
        return out

    # 期间/发布日不足但主体已核验 → 仍需核验时点（不静默准入）
    out.update({"stage": STAGE_PENDING, "usable_as": "unknown"})
    out["reasons"].append(f"{soft}：主体已核验但时点/期间不足，保留为待核验")
    if not out["verified_with_body"]:
        out["need_body_verify"] = True
    return out


def _doc_type(doc: dict) -> str:
    try:
        import narrative_evidence as ne
        kind = ne.doc_kind(str(doc.get("title") or ""), str(doc.get("url") or ""))
        if kind:
            return kind
        return "third_party"
    except Exception:                            # noqa: BLE001
        return ""


def hard_reject(result: dict) -> bool:
    """是否**已证明**不适用（用于抓取前剔除：只认可证的三类，不认"待核验"）。"""
    if str((result or {}).get("stage") or "") != STAGE_REJECTED:
        return False
    return str((result or {}).get("usable_as") or "") != "background"


def admit_many(candidates, contract=None, *, bodies: dict | None = None) -> dict:
    """批量准入：返回 `{admitted, pending, rejected, background, counts}`。

    `bodies`：可选的 `{url: 正文}`（已抓取的页面用正文复核，同一条判据）。
    """
    bodies = bodies or {}
    admitted: list[dict] = []
    pending: list[dict] = []
    rejected: list[dict] = []
    background: list[dict] = []
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        url = str(cand.get("url") or cand.get("href") or "")
        res = admit(cand, contract, body=bodies.get(url, ""))
        rec = dict(cand, **{"_admission": res})
        stage = res["stage"]
        if stage == STAGE_ADMITTED:
            admitted.append(rec)
        elif stage == STAGE_PENDING:
            pending.append(rec)
        elif stage == STAGE_REJECTED:
            if res["usable_as"] == "background":
                background.append(rec)
            rejected.append(rec)
        else:
            # 无契约主体等"未判定"：按原样保留（不减损来源）
            admitted.append(rec)
    counts = {"admitted": len(admitted), "pending": len(pending),
              "rejected": len(rejected), "background": len(background),
              "total": len(admitted) + len(pending) + len(rejected)}
    return {"admitted": admitted, "pending": pending, "rejected": rejected,
            "background": background, "counts": counts}


def reason_text(result: dict) -> str:
    """一条候选的准入理由（写进审计/报告，不含提示词与正文）。"""
    rs = list((result or {}).get("reasons") or [])
    return "；".join(rs) if rs else str((result or {}).get("admission") or "")


def subject_anchors_ok(candidate: dict, contract) -> bool:
    """候选是否命中契约主体（快速判据；权威结论用 `admit`）。"""
    fields = contract_fields(contract)
    if not (fields["company"] or fields["company_id"]):
        return False
    blob = _blob(candidate)
    if fields["company"] and fields["company"] in blob:
        return True
    code = fields["company_id"]
    if code:
        digits = re.sub(r"\D", "", code)
        if digits and digits in re.sub(r"\D", "", blob):
            return True
    return False
