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
CONTRACT_MARK = "[研究契约]"

# 期间词：与"年度报告"契约冲突的其它报告期（历史教训里常见）
_PERIOD_WORDS = ("一季报", "中报", "半年报", "三季报", "季报", "半年业绩", "季度报告")
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
# 年报披露在次年上半年：`as_of` 所在年份允许出现（披露日/资料截止日）
_AS_OF_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})")


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
        }

    # ── 身份 ────────────────────────────────────────────────
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
        payload = json.dumps(self.identity(), ensure_ascii=False, sort_keys=True)
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
        """允许出现的年份：契约期间 + `as_of` 年份（披露日所在年）。

        `as_of` 缺失时不额外放宽——宁严不宽，避免历史教训里的其它年份混进来。
        """
        years = set(self.periods)
        m = _AS_OF_YEAR_RE.search(self.as_of or "")
        if m:
            years.add(int(m.group(1)))
        return years

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

        - 契约外的年份（期间与 `as_of` 年份之外的 19xx/20xx）；
        - 与 `doc_type` 不符的报告期词（年度报告契约下的"三季报/半年报/一季报"）。
        """
        text = str(text or "")
        if not text:
            return []
        allowed = self.allowed_years()
        hits: list[str] = []
        for m in _YEAR_RE.finditer(text):
            y = int(m.group(0))
            if allowed and y not in allowed:
                hits.append(m.group(0))
        if self.doc_type == DOC_TYPE_ANNUAL:
            for w in _PERIOD_WORDS:
                if w in text:
                    hits.append(w)
        elif self.doc_type == DOC_TYPE_QUARTERLY:
            if "年度报告" in text and "季度报告" not in text:
                hits.append("年度报告")
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

        幂等：先剥掉旧的 `[检索查询]` 行与契约块，再加当前的——这样 Critic 修订稿、
        反思重做稿、恢复重放的计划拿到的是同一份契约，不会带着上一版的查询走。

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
                stripped, removed = self._strip_contract_marks(old)
                if removed:
                    repairs.append(f"{s.get('step_id')}: 剥离旧契约标记 {len(removed)} 处")
                new = self._build_instruction(stripped, s)
                if new != old:
                    s["instruction"] = new
                    repairs.append(f"{s.get('step_id')}: 按契约重建检索查询")
            out.append(s)
        return out, repairs

    @staticmethod
    def _strip_contract_marks(text: str) -> tuple[str, list[str]]:
        """剥掉旧的 `[检索查询]` 与 `[研究契约]` 标记（幂等重建的第一步）。

        模型修订稿里的标记可能出现在行内（"检索… [检索查询] …"），也可能整行就是它；
        两种都要处理：整行是标记 → 丢该行，行内出现 → 只丢掉标记及其后的同段内容。
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
            keep.append(line)
        body = "\n".join(keep)
        if CONTRACT_MARK in body:
            body = body.split(CONTRACT_MARK)[0].rstrip()
            removed.append(CONTRACT_MARK)
        return body, removed

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
