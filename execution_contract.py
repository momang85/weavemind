# -*- coding: utf-8 -*-
"""执行契约：把研究请求里的**身份事实**变成贯穿"规划 → Critic 修订 → 派发"的硬约束。

为什么需要它：检索短查询此前是步骤指令里的一行 `[检索查询] …` 文本。Critic 用模型
修订计划时，替换后的步骤会丢掉这一行（实机 ui-706c5ef4a5：派发里没有 `[检索查询]`，
引擎收到的 query 是整段任务要求 + 历史经验 + 技能教训的拼接，还混进"2025年三季报"
与 2026——与 2023–2024 的契约冲突）。**可被模型删掉的一行提示不是契约。**

本模块把主体/代码/期间/as_of/报表口径/文档类型/必需指标做成结构化对象：
- 检索 query 由它**生成**（按年度、短、带代码与文档类型）；
- 计划修订后由它**重建**（`apply_to_steps`）并校验不变量（`violations`）；
- 派发时带**指纹**（`fingerprint`），与任务当前契约不一致就不派发；
- 期间冲突的历史教训/技能文本由它**标注不适用**（`conflicting_periods`），
  而不是删掉整个历史库。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

CONTRACT_VERSION = 1
DOC_TYPE_ANNUAL = "年度报告"
DOC_TYPE_QUARTERLY = "季度报告"

# 契约块在指令里的标记（幂等重建时先剥旧的，再加新的）
QUERY_MARK = "[检索查询]"
# 结构化重试计划的查询行：优先于上面的常规查询行（上一批查询已被证明打不出结果）
RETRY_MARK = "[重试检索查询]"
# 重试计划**已用尽**的显式停止标记（P1-c②）：指令里出现它时，Worker **不得**回退到
# 契约常规查询，也不得拿整段指令当查询——"没有新查询"就该如实停下来。
# 此前只有一句中文说明（"本轮没有可用的新查询"），Worker 读不出来，于是**自然回退**
# 到上一批已证明打不出结果的查询上：换词的那一轮等于原地重试。
NO_NEW_QUERY_MARK = "[无新查询]"
CONTRACT_MARK = "[研究契约]"

# 期间词：与"年度报告"契约冲突的其它报告期（历史教训里常见）
# Q0 补：**正规名称**必须与口语简称同样在列。旧表只有"半年报"，于是**正式名**
# `2024年半年度报告` 从年度契约里漏了过去（审查复核实测）；同理补"半年度/中期报告"。
_PERIOD_WORDS = ("一季报", "中报", "半年报", "半年度报告", "半年度", "中期报告",
                 "三季报", "季报", "半年业绩", "季度报告")
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
# 年报披露在次年上半年：`as_of` 所在年份允许出现（披露日/资料截止日）
_AS_OF_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})")
# 年份后面紧跟"报告期词"= 在声明**报告所属期间**（不是披露时间）：
# `2025年年度报告` / `2025年报` / `2025 年度` / `2025年三季度报告`
_YEAR_REPORT_RE = re.compile(r"\s*年?\s*(?:度\s*)?(?:年度报告|年报|年度|半年度|"
                             r"三季报|半年报|一季报|季报|季度报告)")
# **倒装**：报告期词在前、年份在后（`年度报告2025公告` / `半年度报告2024`）。
# 必须**紧邻**（只允许空格与左括号）：`2024年度报告，2025年4月披露` 里逗号隔开 → 披露语境；
# 且年份后面若是"年X月"这类日期，则那一年是**披露时间**（合法），不是报告期声明。
_YEAR_AFTER_REPORT_RE = re.compile(
    r"(?:半年度报告|三季度报告|一季度报告|季度报告|年度报告|半年度|半年报|三季报|"
    r"一季报|季报|中报|年报|年度)\s*[（(]?\s*((?:19|20)\d{2})(?!\d)")
# 披露/日期语境：`2025年4月披露`、`2025-04-29 公告`、`资料截至 2025`
_DISCLOSURE_HINT_RE = re.compile(
    r"(披露|发布|公告|刊登|出具|截至|资料截止|数据时效|更新|日期|报告日)")
_DATE_LIKE_RE = re.compile(r"\s*年\s*\d{1,2}\s*月|[\-/.]\d{1,2}[\-/.]\d{1,2}")


def _disclosure_context(text: str, start: int, end: int, *, window: int = 12) -> bool:
    """该年份是否出现在**披露/日期**语境里（而不是被当成报告期）。

    `2025年4月披露`（后面跟月份、附近有"披露"类词）→ 是；
    `2025年度报告` → 不是（那是报告期声明，即便它是 `as_of` 年份也不合法）。
    """
    tail = text[end:end + window]
    head = text[max(0, start - window):start]
    if _DATE_LIKE_RE.match(tail):
        return True
    return bool(_DISCLOSURE_HINT_RE.search(tail) or _DISCLOSURE_HINT_RE.search(head))


def _as_int_list(values) -> tuple[int, ...]:
    out: list[int] = []
    for v in values or []:
        try:
            y = int(str(v).strip())
        except Exception:
            continue
        if 1900 <= y <= 2200 and y not in out:
            out.append(y)
    return tuple(sorted(out))


@dataclass(frozen=True)
class ExecutionContract:
    """一次研究的执行契约（身份事实；说不清的部分留空，不猜）。"""

    company: str = ""
    company_id: str = ""
    market: str = ""
    periods: tuple[int, ...] = ()
    caliber: str = ""
    as_of: str = ""
    doc_type: str = DOC_TYPE_ANNUAL
    required_metrics: tuple[str, ...] = ()
    perspective: str = ""
    subject_type: str = ""
    version: int = CONTRACT_VERSION

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_request(cls, request) -> "ExecutionContract":
        """从 `facts.ResearchRequest` 构造；缺字段留空（契约不猜主体/期间）。"""
        if request is None:
            return cls()
        metrics = tuple(str(m) for m in (getattr(request, "required_metrics", None) or []))
        return cls(
            company=str(getattr(request, "company", "") or ""),
            company_id=str(getattr(request, "company_id", "") or ""),
            market=str(getattr(request, "market", "") or ""),
            periods=_as_int_list(getattr(request, "periods", None)),
            caliber=str(getattr(request, "caliber", "") or ""),
            as_of=str(getattr(request, "as_of", "") or ""),
            required_metrics=metrics,
            perspective=str(getattr(request, "perspective", "") or ""),
            subject_type=str(getattr(request, "subject_type", "") or ""),
        )

    @classmethod
    def from_wire(cls, raw) -> "ExecutionContract | None":
        """从派发载荷恢复；结构不对返回 None（不猜、不兜底）。"""
        if not isinstance(raw, dict) or not raw:
            return None
        if int(raw.get("version") or 0) != CONTRACT_VERSION:
            return None
        return cls(
            company=str(raw.get("company") or ""),
            company_id=str(raw.get("company_id") or ""),
            market=str(raw.get("market") or ""),
            periods=_as_int_list(raw.get("periods")),
            caliber=str(raw.get("caliber") or ""),
            as_of=str(raw.get("as_of") or ""),
            doc_type=str(raw.get("doc_type") or DOC_TYPE_ANNUAL),
            required_metrics=tuple(str(m) for m in (raw.get("required_metrics") or [])),
            perspective=str(raw.get("perspective") or ""),
            subject_type=str(raw.get("subject_type") or ""),
        )

    def to_wire(self) -> dict:
        """JSON 安全载荷（含指纹）：派发/落盘/比对都用它。"""
        return {
            "version": self.version,
            "fingerprint": self.fingerprint(),
            "company": self.company,
            "company_id": self.company_id,
            "market": self.market,
            "periods": list(self.periods),
            "caliber": self.caliber,
            "as_of": self.as_of,
            "doc_type": self.doc_type,
            "required_metrics": list(self.required_metrics),
            "perspective": self.perspective,
            "subject_type": self.subject_type,
            # C2：固定问题集随契约落盘（题目/类型/所需证据），页面与装配读同一份
            "questions": self.questions(),
        }

    # ── 身份 ────────────────────────────────────────────────
    def questions(self) -> list[dict]:
        """任务创建时固定的问题集（最多三问）——类型与证据要求随契约一起版本化。

        由 `question_assessment.question_set` 生成（题目/边界来自报告侧同一份定义），
        因此"这个问题要什么证据"在提交时就定下来，装配与页面读的是同一份。
        """
        try:
            from question_assessment import question_set
            return question_set(self.required_metrics, perspective=self.perspective,
                                subject_type=self.subject_type)
        except Exception:                        # noqa: BLE001 - 生成失败不拖垮契约
            return []

    def question_identity(self) -> list[list[str]]:
        """参与指纹的问题身份投影：`[[metric, question_type], …]`（排序后可比对）。"""
        rows = [[str(q.get("metric") or ""), str(q.get("question_type") or "")]
                for q in self.questions()]
        return sorted(r for r in rows if r[0])

    def identity(self) -> dict:
        """参与指纹的身份字段（顺序无关的规范投影）。"""
        return {
            "company": self.company.strip(),
            "company_id": self.company_id.strip().upper(),
            "market": self.market.strip().lower(),
            "periods": list(self.periods),
            "caliber": self.caliber.strip(),
            "as_of": self.as_of.strip(),
            "doc_type": self.doc_type.strip(),
            "required_metrics": sorted(m.strip() for m in self.required_metrics),
            "perspective": self.perspective.strip(),
            "subject_type": self.subject_type.strip(),
        }

    def fingerprint(self) -> str:
        """契约指纹：身份字段 + **问题集**（指标 + 类型）。

        `identity()` 保持"能当构造参数用"的纯字段投影（测试与调用方会拿它重建契约），
        因此问题集单独并入指纹载荷——问题集变了，指纹就变，旧绑定不再判"当前"。
        """
        payload = json.dumps({"identity": self.identity(),
                              "questions": self.question_identity()},
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def matches(self, wire) -> bool:
        """载荷是否就是本契约（版本 + 指纹都一致）。"""
        if not isinstance(wire, dict):
            return False
        if int(wire.get("version") or 0) != self.version:
            return False
        return str(wire.get("fingerprint") or "") == self.fingerprint()

    @property
    def subject(self) -> str:
        return self.company or self.company_id or "目标公司"

    def label(self) -> str:
        """给人和给模型看的短标签：主体（代码）。"""
        if self.company and self.company_id:
            return f"{self.company}（{self.company_id}）"
        return self.subject

    def allowed_years(self) -> set[int]:
        """允许**出现**的年份：契约期间 + `as_of` 年份（披露日所在年）。

        `as_of` 缺失时不额外放宽——宁严不宽，避免历史教训里的其它年份混进来。
        注意这只是"允许出现"：`as_of` 年份允许出现，是因为**披露日/资料截止日**会写到它
        （"2024 年年度报告，2025 年 4 月披露"）。它**不是**目标报告期——把 `as_of`
        年份当期间用（"2025 年度报告"冒充本契约目标）由 `conflicting_periods` 单独挡
        （D-① 裁决：报告期与披露日**分开**校验）。
        """
        years = set(self.periods)
        years |= self.disclosure_years()
        return years

    def period_years(self) -> set[int]:
        """**报告所属期间**（研究目标的期间）——只有这些年份能当报告期。"""
        return set(self.periods)

    def disclosure_years(self) -> set[int]:
        """**披露/资料截止**所在年份（`as_of`）：只用于日期语境，不扩展目标期间。"""
        m = _AS_OF_YEAR_RE.search(self.as_of or "")
        return {int(m.group(1))} if m else set()

    # ── 检索查询 ────────────────────────────────────────────
    def queries(self, *, metrics: bool = True) -> list[str]:
        """按年度生成短查询（每个期间一条，带代码与文档类型）。

        短查询只含"主体 + 期间 + 文档类型 (+ 指标)"：整段任务要求（含"每个数字须能
        回溯…银行对公视角"这类样板）扔给检索器会让引擎大面积无结果（实机
        ui-750185076a / ui-706c5ef4a5）。
        """
        who = self.label()
        years = list(self.periods) or [None]
        metric_part = ""
        if metrics:
            from facts import metric_label
            labels = [metric_label(m) for m in (self.required_metrics or [])][:3]
            metric_part = (" " + " ".join(labels)) if labels else ""
        out = []
        for y in years:
            head = f"{who} {y}年{self.doc_type}" if y else f"{who} {self.doc_type}"
            out.append(f"{head}{metric_part}".strip())
        return out

    def retry_query_line(self, *, tried=(), limit: int = 4) -> str:
        """指令里的重试查询块；没有新查询时返回空串（编排器据此不假装有换词方案）。"""
        return "\n".join(f"{RETRY_MARK} {q}"
                         for q in self.retry_queries(tried=tried, limit=limit))

    def query_line(self) -> str:
        """指令里的查询块（多行，每行一条按年短查询）。"""
        return "\n".join(f"{QUERY_MARK} {q}" for q in self.queries())

    # 结构化重试的资料面：主体/期间/文档类型/截止都不动，只换"要哪一份资料"。
    RETRY_FACETS = ("经营情况讨论与分析", "主要财务指标", "公告", "全文")

    def retry_queries(self, *, tried=(), limit: int = 4) -> list[str]:
        """下一批检索查询（结构化重试计划），`RETRY_MARK` 前缀交给 Worker 采用。

        为什么要有这个方法（专项 §5"补查重试的真正输入"）：检索失败/零召回后如果只是把
        同一批查询再打一遍，引擎返回的结果必然一样；此前编排器只在指令尾追加一句"请换
        查询词"，模型改不动实际查询。这里按**契约字段**换资料面——MD&A 正文 / 指标底稿 /
        公告 / 原件全文——逐条排除 `tried` 里已出现过的字符串，既不猜模型给的词，也不靠放松
        相关性阈值去凑候选；期间外年份仍然不可能出现（全部由 `self.periods` 生成）。
        """
        who = self.label()
        years = list(self.periods) or [None]
        used = {str(t or "").strip() for t in (tried or ())}
        out: list[str] = []
        limit = max(1, int(limit))
        for y in years:                      # 先按期间铺开，再换资料面：每个期间都能拿到重试查询
            head = f"{who} {y}年{self.doc_type}" if y else f"{who} {self.doc_type}"
            for facet in self.RETRY_FACETS:
                q = f"{head} {facet}".strip()
                if q in used or q in out:
                    continue
                out.append(q)
                if len(out) >= limit:
                    return out
        return out

    def contract_line(self) -> str:
        """指令里的契约块：主体/期间/口径/as_of/文档类型/必需指标（以此为准）。"""
        from facts import metric_label
        metrics = "、".join(metric_label(m) for m in (self.required_metrics or [])) or "未声明"
        span = "、".join(str(y) for y in self.periods) or "未声明"
        return (
            f"{CONTRACT_MARK} 主体 {self.label()}；期间 {span}；文档类型 {self.doc_type}；"
            f"报表口径 {self.caliber or '未声明'}；资料截至 {self.as_of or '未声明'}；"
            f"必需指标 {metrics}。以此为准，不得替换主体或期间。"
        )

    # ── 期间冲突 ────────────────────────────────────────────
    def conflicting_periods(self, text: str) -> list[str]:
        """文本里与契约冲突的期间表述（用于标注"不适用"，不用于删文本）。

        D-① 裁决（报告期与披露日**分开**校验）：
        - **报告期**只能用 `periods` 里的年份：`2025年年度报告`／`2025年报` 这类
          "年份 + 报告期词"即使年份等于 `as_of` 年份也算冲突（那是**披露**年份，
          不是本契约的目标期间）；
        - **披露日/资料截止日**语境里的 `as_of` 年份合法："2024年度报告，2025年4月披露"
          → 2024 是报告期、2025 是披露时间，两者都留；
        - 其它契约外年份照旧算冲突；与 `doc_type` 不符的报告期词照旧算冲突。
        """
        text = str(text or "")
        if not text:
            return []
        allowed = self.allowed_years()
        period_years = self.period_years()
        hits: list[str] = []
        for m in _YEAR_RE.finditer(text):
            y = int(m.group(0))
            if y not in allowed:
                hits.append(m.group(0))
                continue
            if y in period_years:
                continue
            # `as_of` 年份：紧跟"报告期词"就算是**报告期声明**（不合法）；
            # 否则只有**披露/日期语境**才算合法。注意必须**锚在年份后面**匹配
            # （`re.search` 会看到句子后半段的"…披露的年度报告"而误判成报告期）。
            if _YEAR_REPORT_RE.match(text, m.end()) or \
                    not _disclosure_context(text, m.start(), m.end()):
                hits.append(m.group(0))
        if self.doc_type == DOC_TYPE_ANNUAL:
            for w in _PERIOD_WORDS:
                if w in text:
                    hits.append(w)
        elif self.doc_type == DOC_TYPE_QUARTERLY:
            if "年度报告" in text and "季度报告" not in text:
                hits.append("年度报告")
        # **倒装年份**（Q0 补）：报告期词在前、年份紧随其后（`年度报告2025公告`）。
        # 旧实现只看"年份后面"的词，于是这种写法从年度契约里漏过去（审查复核实测）。
        # 判据：年份不在目标期间；且年份后面**不是**"年X月"这类日期——
        # `2024年度报告2025年4月披露` 里的 2025 是披露时间，仍然合法。
        for m in _YEAR_AFTER_REPORT_RE.finditer(text):
            y = int(m.group(1))
            if y in period_years:
                continue
            if _DATE_LIKE_RE.match(text[m.end():m.end() + 8]):
                continue
            hits.append(m.group(0).strip())
        # 去重且保持出现顺序
        seen: set[str] = set()
        out: list[str] = []
        for h in hits:
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out

    def mark_inapplicable(self, text: str) -> tuple[str, list[str]]:
        """给冲突文本加"本次不适用"前缀（保留原文，不删除历史库）。

        返回 (标注后的文本, 命中的冲突词)。
        """
        text = str(text or "")
        hits = self.conflicting_periods(text)
        if not hits:
            return text, []
        note = f"（本次不适用：与本次契约期间冲突——{'、'.join(hits[:4])}）"
        return f"{note}{text}", hits

    # ── 计划/指令 ───────────────────────────────────────────
    _RESEARCH_CAPS = ("web_search", "web_fetch")

    def apply_to_steps(self, steps: list[dict]) -> tuple[list[dict], list[str]]:
        """把契约**结构化地**写回每个步骤，并重建检索查询行。

        幂等：先剥掉旧的 `[检索查询]` 行与契约行，再加当前的——这样 Critic 修订稿、
        反思重做稿、恢复重放的计划拿到的是同一份契约，不会带着上一版的查询走。

        **重试批次必须活过重建**（P1-c①）：`[重试检索查询]` 是编排器按同一契约生成的
        "换了资料面"的下一批查询，重建时逐条过 `query_in_contract`——契约内的保留、
        契约外的拒绝并记进修复记录。若整批都在契约外，新的指令里就不含重试行，
        Worker 据此回退契约查询是**正确**的（没有可用的新查询）；而"契约内明明有、
        却被重建吞掉"才是本地修掉的缺陷。

        返回 (步骤, 修复记录)。**不修改调用方传入的对象**（返回新列表/新字典）。
        """
        out: list[dict] = []
        repairs: list[str] = []
        wire = self.to_wire()
        for raw in (steps or []):
            if not isinstance(raw, dict):
                out.append(raw)
                continue
            s = dict(raw)
            s["contract"] = wire
            if str(s.get("capability") or "") in self._RESEARCH_CAPS:
                old = str(s.get("instruction") or "")
                # 取出重试批次（在剥离之前）——剥离会去掉契约行，重试行本该保留，
                # 但先取出来才能逐条做契约校验并在重建后原样放回
                _retry_old = self.retry_queries_in(old)
                stripped, removed = self._strip_contract_marks(old)
                if removed:
                    repairs.append(f"{s.get('step_id')}: 剥离旧契约标记 {len(removed)} 处")
                _keep_retry: list[str] = []
                _drop_retry: list[str] = []
                for q in _retry_old:
                    _ok, _why = self.query_in_contract(q)
                    (_keep_retry if _ok else _drop_retry).append(q if _ok else f"{q}（{_why}）")
                # 旧的 `[重试检索查询]` 行**一律先摘掉**：批次整体按上面校验过的集合重建，
                # 否则被拒的契约外查询会留在 stripped 里跟着指令一起发出去（实测漏网）。
                if _retry_old:
                    stripped = "\n".join(
                        ln for ln in stripped.splitlines()
                        if not ln.strip().startswith(RETRY_MARK))
                if _keep_retry:
                    repairs.append(
                        f"{s.get('step_id')}: 保留契约内重试查询 {len(_keep_retry)} 条")
                if _drop_retry:
                    repairs.append(
                        f"{s.get('step_id')}: 拒绝契约外重试查询 {len(_drop_retry)} 条"
                        f"：{_drop_retry[0][:60]}")
                new = self._build_instruction(stripped, s)
                if _keep_retry:
                    new = (f"{new}\n" + "\n".join(f"{RETRY_MARK} {q}" for q in _keep_retry))
                if new != old:
                    s["instruction"] = new
                    repairs.append(f"{s.get('step_id')}: 按契约重建检索查询")
            out.append(s)
        return out, repairs

    def query_in_contract(self, q: str) -> tuple[bool, str]:
        """该查询是否落在本契约内 → `(是否保留, 原因)`（P1-c①：保留契约内、拒绝契约外）。

        "契约内"两条判据，都由契约字段算出（不看模型自述）：

        1. 不与契约期间冲突——沿用 `conflicting_periods`（契约外年份 + 与 `doc_type`
           不符的报告期词）；
        2. 提到了本主体（简称 / 代码 / 全称之一）——"换主体"与"换资料面"是两件事，
           重试只允许换资料面（`retry_queries` 只改 facet）。

        重试批次里的查询据此**逐条保留或拒绝**：不能因为是"重试"就放过契约外主体/期间。
        """
        s = str(q or "").strip()
        if not s:
            return False, "空查询"
        hits = self.conflicting_periods(s)
        if hits:
            return False, f"含契约外期间 {'、'.join(hits[:3])}"
        subjects = [str(t) for t in (self.label(), self.company, self.company_id)
                    if str(t or "").strip()]
        if subjects and not any(t in s for t in subjects):
            return False, f"未提到契约主体（{subjects[0]}）"
        return True, ""

    def retry_queries_in(self, text: str) -> list[str]:
        """从指令文本里取出 `[重试检索查询]` 行（保序、去重、去空白）。

        重建指令时必须**先取出来再重建**——否则契约标记处的截断会把整批重试查询
        连同"重试 N"提示一起吞掉（2026-09-28 反例：附 4 条重试查询，重建后 0 条）。
        """
        out: list[str] = []
        for line in str(text or "").splitlines():
            s = line.strip()
            if not s.startswith(RETRY_MARK):
                continue
            q = s.split(RETRY_MARK, 1)[1].strip()
            if q and q not in out:
                out.append(q)
        return out

    @staticmethod
    def _strip_contract_marks(text: str) -> tuple[str, list[str]]:
        """剥掉旧的 `[检索查询]` 与 `[研究契约]` 标记（幂等重建的第一步）。

        模型修订稿里的标记可能出现在行内（"检索… [检索查询] …"），也可能整行就是它；
        两种都要处理：整行是标记 → 丢该行，行内出现 → 只丢掉标记及其后的同段内容。

        **`[研究契约]` 只丢它自己那一行**（2026-09-28 修正）：契约块是**单行**
        （见 `contract_line()`），此前用 `body.split(CONTRACT_MARK)[0]` 把标记之后的
        **全部内容**一并截断——而编排器的`[重试检索查询]`批次恰恰追加在指令**末尾**，
        于是"重建"把整批新查询（和"重试 N"提示）吞掉，Worker 见不到重试行就**自然回退
        到契约原查询**：换了查询词的那一轮等于原地重试。
        """
        removed: list[str] = []
        keep: list[str] = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(QUERY_MARK):
                removed.append(QUERY_MARK)
                continue
            if QUERY_MARK in line:
                # 行内标记：标记及其后面通常是模型自己写的查询串，整段丢掉
                head = line.split(QUERY_MARK, 1)[0].rstrip()
                removed.append(QUERY_MARK)
                if head:
                    keep.append(head)
                continue
            if stripped.startswith(CONTRACT_MARK):
                # 契约行**整行丢掉**：它的全部内容都由 `contract_line()` 生成，
                # `_build_instruction` 会重新写一份。保留其后的"正文"会让每次重建
                # 都多积累一份契约正文（实测三次重建后 3 份）。
                removed.append(CONTRACT_MARK)
                continue
            if CONTRACT_MARK in line:
                # 行内契约标记（历史写法）：只切掉标记及其后同段，保留标记之前的内容
                head = line.split(CONTRACT_MARK, 1)[0].rstrip()
                removed.append(CONTRACT_MARK)
                if head:
                    keep.append(head)
                continue
            keep.append(line)
        return "\n".join(keep), removed

    def _build_instruction(self, body: str, step: dict) -> str:
        cap = str(step.get("capability") or "")
        if cap == "web_search":
            head = (
                f"{self.query_line()}\n"
                f"检索 {self.label()} 的{self.doc_type}与财务数据的权威来源"
                f"（优先公司公告/交易所/官方年报）；至少一条要指向契约期间的"
                f"**发行人年报/公告**（经营情况讨论与分析、管理层讨论、财务附注、风险因素），"
                f"返回含原始 URL 的结果列表。"
            )
        else:  # web_fetch
            head = (
                f"从上游搜索结果中选取与 {self.label()} 契约期间匹配的"
                f"**发行人{self.doc_type}/公告正文页**（经营情况讨论与分析、管理层讨论、"
                f"经营回顾），抓取完整正文并保留小节标题、原始 URL 与全部数字"
                f"（年份、金额、币种、单位、报表口径）；主链接失败则换备用链接。"
            )
        tail = body.strip()
        # **幂等**（P1-c①）：`head` 是装配器自己写的，重建时必须先摘掉旧的再加一次。
        # 此前无条件前置，每次重建都多出一份——实测"检索 洋河股份…的权威来源"这句在
        # 三次重建后出现 4 次，指令越滚越长。只摘**不含查询行**的那部分（查询行已由
        # `_strip_contract_marks` 处理），逐份移除。
        if tail:
            _hp = "\n".join(ln for ln in head.splitlines()
                            if ln.strip() and not ln.strip().startswith(QUERY_MARK)).strip()
            while _hp and _hp in tail:
                tail = tail.replace(_hp, "", 1).strip()
            tail = tail.strip("\n")
        out = f"{head}\n{self.contract_line()}"
        return f"{out}\n{tail}" if tail else out

    def violations(self, steps: list[dict]) -> list[str]:
        """计划不变量：研究步骤必须带本契约，且**检索查询行**不得含冲突期间。

        只对 `[检索查询]` 行判期间冲突（那是真正送给引擎的串）；指令正文里的历史
        提示由 `mark_inapplicable` 标注，不在这里判失败——历史文本保留是刻意的。
        """
        bad: list[str] = []
        for s in (steps or []):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("step_id") or "?")
            cap = str(s.get("capability") or "")
            if cap not in self._RESEARCH_CAPS:
                continue
            if not self.matches(s.get("contract")):
                bad.append(f"{sid}: 缺少本契约（或指纹不一致）")
                continue
            for line in str(s.get("instruction") or "").splitlines():
                if not line.strip().startswith(QUERY_MARK):
                    continue
                q = line.split(QUERY_MARK, 1)[1]
                hits = self.conflicting_periods(q)
                if hits:
                    bad.append(f"{sid}: 检索查询含冲突期间 {hits}")
        return bad

    def plan_notes(self) -> dict:
        """随计划落盘/展示的契约摘要（供复核"这次按什么契约跑"）。"""
        return {"fingerprint": self.fingerprint(), "version": self.version,
                "label": self.label(), "periods": list(self.periods),
                "doc_type": self.doc_type, "as_of": self.as_of,
                "queries": self.queries()}


def contract_for_task(task) -> "ExecutionContract | None":
    """从编排器持有的任务状态取契约（恢复路径也走这里，避免两处各造一份）。"""
    if not task:
        return None
    req = getattr(task, "research_request", None)
    if req is None:
        return None
    return ExecutionContract.from_request(req)
