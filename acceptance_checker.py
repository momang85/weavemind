# -*- coding: utf-8 -*-
"""结构化验收器（acceptance checker）：确定性 checklist，输出缺口报告。

验收器不直接提高报告质量，但它让"质量差在哪里"变成可读的结构化报告：
- 每条检查是确定性函数（无 LLM），输出 {pass, details, 计数...}
- run_acceptance 汇总为缺口报告 {report_id, checks, overall, gaps}
- 缺口报告写入任务工作区 acceptance_report.json，并注入反思上下文，
  让反思从"重做一遍"变成"精准补缺口"。

第一条检查：数字溯源校验（报告里的数字能否在检索结果/快照/清洗数据中找到）。
后续可扩展：章节完整性、主体归属、来源标注诚实性、图表存在性。
"""

import json
import logging
import re
import itertools
from pathlib import Path
from hashlib import sha256
from time import gmtime, strftime
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 规则版本化（可对账）
# ─────────────────────────────────────────────

# 判定规则集版本：任何规则表/阈值/正则变更时必须 bump，
# 并同步更新 test_p0 的指纹基线（test_acceptance_rules_fingerprint_stable
# 会因指纹变化而失败，强制走"改规则→bump 版本→更新基线"流程）。
ACCEPTANCE_RULES_VERSION = "2026.09.12"

# 纳入指纹的规则表（常量名；内部按 key/元素排序后哈希，顺序无关）
_FINGERPRINT_RULES = (
    "_TRACEABILITY_THRESHOLDS", "_DISCLOSED_MARKERS",
    "_FINANCIAL_MARKERS", "_RESEARCH_MARKERS",
    "_OTHER_ENTITIES", "_CORE_FIN_WORDS", "_RELATION_VERBS",
    "_DOMAIN_MEDIA", "_MEDIA_ALIAS_GROUPS", "_EXTRA_MEDIA_NAMES",
    "_NEGATION_MARKERS", "_GENERIC_HONEST_MARKERS",
    "_PLACEHOLDER_MARKERS", "_LIST_REQUIREMENT_KEYWORDS",
    "_NUMERIC_REQUIREMENT_KEYWORDS",
    "_FRESHNESS_BLOCK_MARKERS", "_TIME_SENSITIVE_MARKERS",
    "_STATUS_MARKERS", "_META_SOURCE_TALK", "_TABLE_NOTE_WORDS",
    "_PROFILE_REPORT_CHECKS", "_CODE_GOAL_HINTS", "_DATA_GOAL_HINTS",
)
# 纳入指纹的关键正则（取 .pattern）
_FINGERPRINT_PATTERNS = (
    "_AUTHORITY_DOC_RE", "_SOURCE_LIST_HEADING_RE", "_INLINE_REF_RE",
    "_MEDIA_WORD_RE", "_FRESHNESS_DATE_RE",
)


def _fingerprint_normalize(val):
    """规则值规范化：字典按键排序、集合转排序列表，保证指纹与声明顺序无关。"""
    if isinstance(val, dict):
        return {str(k): _fingerprint_normalize(v) for k, v in sorted(val.items())}
    if isinstance(val, (set, frozenset)):
        return sorted(str(v) for v in val)
    if isinstance(val, (list, tuple)):
        return [_fingerprint_normalize(v) for v in val]
    return str(val)


def rules_fingerprint() -> str:
    """规则指纹：规则表与关键正则规范化后的 sha256 前 8 位。

    用于验收报告/事件的对账——同一份结果能反查"哪一版规则判的"，
    规则变了指纹必变（配合版本守卫测试强制 bump 版本号）。"""
    g = globals()
    parts: list[str] = []
    for name in _FINGERPRINT_RULES:
        val = g.get(name)
        if val is None:
            continue
        parts.append(
            name + "=" + json.dumps(
                _fingerprint_normalize(val), ensure_ascii=False, sort_keys=True,
            )
        )
    for name in _FINGERPRINT_PATTERNS:
        obj = g.get(name)
        if obj is not None and hasattr(obj, "pattern"):
            parts.append(name + "=" + str(obj.pattern))
    return sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]


# ─────────────────────────────────────────────
# 验收事件流（可回放、可对账）
# ─────────────────────────────────────────────

ACCEPTANCE_EVENTS_FILE = "acceptance_events.jsonl"


def acceptance_events_path(task_id: str) -> Path:
    """{task_dir}/acceptance_events.jsonl，与 acceptance_report.json 同层。"""
    try:
        from workspace import task_workspace
        return task_workspace(task_id) / ACCEPTANCE_EVENTS_FILE
    except Exception:
        return Path(ACCEPTANCE_EVENTS_FILE)


def build_acceptance_event(
    result: dict, trigger: str = "", iteration: int = 0,
    duration_ms: int = 0,
) -> dict:
    """由验收结果构造一条可对账的审计事件（schema 单一来源）。"""
    checks = result.get("checks") or {}
    # 仅显式 pass=False 的检查计入失败（缺 pass 字段的辅助项不算）
    failed = sorted(
        k for k, c in checks.items() if (c or {}).get("pass", True) is False
    )
    url_health = checks.get("url_health") or {}
    return {
        "task_id": str(result.get("report_id") or ""),
        "trigger": str(trigger or ""),
        "iteration": int(iteration or 0),
        "rules_version": str(result.get("rules_version") or ACCEPTANCE_RULES_VERSION),
        "rules_fingerprint": str(result.get("rules_fingerprint") or rules_fingerprint()),
        "report_sha256": str(result.get("report_sha256") or ""),
        "overall": str(result.get("overall") or ""),
        "profile": str(result.get("profile") or ""),
        "gaps_count": len(result.get("gaps") or []),
        "gaps": [str(g)[:200] for g in (result.get("gaps") or [])][:10],
        "checks_failed": failed,
        "url_health_dead": int(url_health.get("dead_count") or 0),
        "duration_ms": int(duration_ms or 0),
        "timestamp": strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()),
    }


def append_acceptance_event(task_id: str, event: dict) -> None:
    """追加一条验收事件（JSONL，一行一事件，可顺序回放）。

    事件流失败绝不影响验收主流程（与 step_diagnosis 同策略：静默降级）。"""
    try:
        p = acceptance_events_path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_acceptance_events(task_id: str) -> list[dict]:
    """按写入顺序读取验收事件（坏行跳过，不抛）。"""
    p = acceptance_events_path(task_id)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                rec["seq"] = len(out) + 1  # 顺序号由读取序确定，回放可直接排序
                out.append(rec)
    except Exception:
        return out
    return out


# ─────────────────────────────────────────────
# 数字提取
# ─────────────────────────────────────────────

_NUM_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(万亿|千亿|百亿|亿|万)?\s*"
    r"(美元|港元|元|人民币|USD|US\$|%|％)?"
)

_DISCLOSED_MARKERS = ("基于模型知识", "未在本次检索中验证", "未验证", "模型估算", "模型知识")

# 货币单位归组（F7）：crypto 适配器用 USD，报告常用“美元/$”，需等价匹配
_CURRENCY_UNIT_GROUPS = (
    ("usd", "美元", "$"),
    ("hkd", "港元", "港币", "hk$"),
    ("cny", "人民币", "元"),
)

# 中文数量级：万亿=1e12、千亿=1e11、百亿=1e10、亿=1e8、万=1e4
_SCALE_MAP = {"万亿": 1e12, "千亿": 1e11, "百亿": 1e10, "亿": 1e8, "万": 1e4}


def _norm(s: str) -> str:
    """去掉逗号/全角逗号/空白，保留小数点，便于模糊匹配。"""
    s = re.sub(r"[,\uFF0C\s]+", "", str(s or ""))
    return re.sub(r"\.0+(?=\D|$)", "", s)  # 1152.0亿 → 1152亿，避免小数写法差异


def extract_financial_numbers(text: str) -> list[dict]:
    """从正文提取需要溯源的财务数字：
    - 带单位（亿/万/美元/元/%）的数字
    - 无单位但 ≥4 位有效数字的大数（如 1383）
    排除：纯年份（19xx/20xx）、URL 内的数字、无单位的个位数。"""
    t = str(text or "")
    # 屏蔽图片引用/文件路径/任务 ID（报告内嵌图表路径含 ui-xxxx，会被误当数字）
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)          # ![name](path)
    t = re.sub(r"[A-Za-z]:\\[^\s]*", " ", t)             # C:\...\路径
    t = re.sub(r"/tasks/ui-[a-z0-9]+/[^\s]*", " ", t)    # /tasks/ui-xxx/...
    url_spans = [m.span() for m in re.finditer(r"https?://\S+", t)]

    def in_url(pos: int) -> bool:
        return any(s <= pos < e for s, e in url_spans)

    nums: list[dict] = []
    for m in _NUM_UNIT_RE.finditer(t):
        pos = m.start()
        if in_url(pos):
            continue
        value_raw = m.group(1)
        unit_big = m.group(2) or ""
        unit_small = m.group(3) or ""
        digits = value_raw.replace(",", "")
        if not unit_big and not unit_small:
            # 股票代码/代码后缀（00700、0700.HK、0700.T）→ 跳过
            tail = t[m.end():m.end() + 6]
            if re.match(r"\.(HK|T|O|N|US|L|A|B|SS|SZ)\b", tail):
                continue
            if re.fullmatch(r"0\d{3,}", digits):
                continue  # 前导零代码（00700）
            if re.fullmatch(r"(19|20)\d{2}", digits):
                continue  # 纯年份
            if len(digits.replace(".", "")) < 4:
                continue  # 无单位的个位数/短数，太泛
        unit = unit_big + unit_small  # 如 "亿元" "万" "%"
        nums.append({
            "value": digits,
            "unit": unit,
            "raw": m.group(0).strip(),
            "pos": pos,
        })
    return nums


def _candidates(num: dict) -> list[str]:
    """生成匹配候选：数字+单位、数字+短单位、大数裸数字、百分比。"""
    v = num["value"]
    unit = num["unit"]
    cands: list[str] = []
    if unit:
        cands.append(_norm(v + unit))
        short = re.sub(r"^(万亿|千亿|百亿|亿|万).*$", lambda m: m.group(1), unit)
        if short != unit:
            cands.append(_norm(v + short))
        if unit.endswith("%"):
            cands.append(_norm(v + "%"))
            cands.append(_norm(v + "％"))
    # 裸数字（无单位）：用词边界匹配，避免 "789" 命中 "4789"/"2019" 的子串
    return [c for c in dict.fromkeys(cands) if c]


def _bare_match(value: str, text: str) -> bool:
    """无单位大数按词边界匹配（前后不是数字/字母/点）。"""
    return re.search(
        r"(?<![A-Za-z0-9.])\s*" + re.escape(value) + r"\s*(?![A-Za-z0-9.])",
        text,
    ) is not None


def _value_scale(unit: str) -> float:
    """单位中文数量级换算系数：'万亿美元' → 1e12，'亿元' → 1e8。"""
    u = str(unit or "")
    for key in ("万亿", "千亿", "百亿", "亿", "万"):
        if key in u:
            return _SCALE_MAP[key]
    return 1.0


def _units_equivalent(a: str, b: str) -> bool:
    """单位等价判断：USD/美元/$、HKD/港元、CNY/人民币/元 视为同一单位；
    无单位任意匹配；其余保持包含关系匹配（兼容既有口径）。"""
    a = str(a or "").strip().lower()
    b = str(b or "").strip().lower()
    if not a or not b:
        return True
    if a == b or a in b or b in a:
        return True
    if a in ("%", "％") and b in ("%", "％"):
        return True

    def _in_group(x: str, grp: tuple[str, ...]) -> bool:
        return any((x == g) or (len(g) >= 2 and g in x) for g in grp)

    return any(_in_group(a, grp) and _in_group(b, grp) for grp in _CURRENCY_UNIT_GROUPS)


def _collect_sources(workspace) -> dict[str, str]:
    """收集可溯源数据源文本：search_results / fetch_snapshot / clean_chart_data /
    structured_data（F7：crypto/macro/news 适配器数据必须参与数字溯源）。"""
    ws = Path(workspace)
    proj = ws / "project"
    src: dict[str, str] = {}
    try:
        sr = proj / "search_results.json"
        if sr.exists():
            items = json.loads(sr.read_text(encoding="utf-8"))
            parts = []
            for it in items or []:
                if isinstance(it, dict):
                    parts.append(
                        f"{it.get('title') or ''} {it.get('url') or ''} "
                        f"{it.get('snippet') or ''}"
                    )
            src["search_results"] = "\n".join(parts)
    except Exception:
        pass
    try:
        fs = proj / "fetch_snapshot.json"
        if fs.exists():
            snaps = json.loads(fs.read_text(encoding="utf-8"))
            src["fetch_snapshot"] = "\n".join(
                f"{s.get('title') or ''} {s.get('url') or ''} {s.get('text') or ''}"
                for s in snaps or []
            )
    except Exception:
        pass
    try:
        cd = proj / "clean_chart_data.json"
        if cd.exists():
            src["clean_chart_data"] = cd.read_text(encoding="utf-8")
    except Exception:
        pass
    try:
        sd = proj / "structured_data.json"
        if sd.exists():
            src["structured_data"] = sd.read_text(encoding="utf-8")
    except Exception:
        pass
    # 结构化财务（东财/SEC/巨潮适配器的落盘产物）：这是**已抓取并留档**的权威来源，
    # 必须参与溯源。此前只认 clean_chart_data.json——而该文件要在清洗步骤跑过之后
    # 才存在（实测：财务任务若在清洗前降级/失败，金额与比率全部被判"不可溯源"，
    # 报告里明明写着 1309.04 亿元、91.29% 却溯源率 0%）。
    try:
        fin = proj / "financials.json"
        if fin.exists():
            payload = json.loads(fin.read_text(encoding="utf-8")) or {}
            _metrics = (("revenue", "亿元"), ("net_profit", "亿元"),
                        ("gross_profit", "亿元"), ("gross_margin", "%"),
                        ("operating_profit", "亿元"),
                        ("total_assets", "亿元"), ("total_liabilities", "亿元"),
                        ("operating_cashflow", "亿元"),
                        ("rd_expense", "亿元"), ("roe", "%"))
            # 两种形状都要认：单实体是顶层 financials；对比任务是
            # {source: multi_entity, companies:[{name, financials}]}——只读顶层会让
            # 对比任务在"清洗未跑"时全部判不可溯源（假失败）。
            rows: list[tuple[str, dict]] = []
            _top_company = str((payload.get("metadata") or {}).get("company") or "")
            # 金额单位不能对所有市场都写"亿元"：美股（SEC）的量纲是"亿美元"，
            # 写错会让报告里的"亿美元"只能靠短单位兜底命中——既掩盖口径又不利于主体绑定。
            # 优先用源自己声明的单位（metadata.unit，单实体/多实体各自读）。
            _md_unit = str((payload.get("metadata") or {}).get("unit") or "")
            for r in payload.get("financials") or []:
                rows.append((_top_company, r, _md_unit))
            for ent in payload.get("companies") or []:
                if not isinstance(ent, dict):
                    continue
                _ent_unit = str((ent.get("metadata") or {}).get("unit") or "") or _md_unit
                for r in ent.get("financials") or []:
                    rows.append((str(ent.get("name") or ""), r, _ent_unit))
            parts = []
            for entity, r, md_unit in rows:
                if not isinstance(r, dict):
                    continue
                for key, unit in _metrics:
                    v = r.get(key)
                    if v is None:
                        continue
                    use_unit = unit
                    if unit == "亿元" and md_unit:
                        use_unit = md_unit      # 金额类指标改用源声明的单位
                    parts.append(
                        f"{entity}{r.get('year')}年{r.get('report_type') or ''} "
                        f"{key}={v}{use_unit} 值 {v} {use_unit}"
                    )
            src["financials"] = "\n".join(parts)
    except Exception as exc:
        # 静默会让"该源为空"与"解析失败"无法区分 → 溯源率假性下降查不出原因
        logger.warning("financials.json 解析失败，该源不参与溯源：%s", str(exc)[:150])
    return src


def _traceable_in_clean(num: dict, clean_text: str) -> bool:
    """在 clean_chart_data.json 中按数值+单位精确比对。"""
    try:
        data = json.loads(clean_text)
    except Exception:
        return False
    try:
        v = float(num["value"])
    except (TypeError, ValueError):
        v = None
    if v is None:
        return False
    unit = num["unit"]
    scaled_v = v * _value_scale(unit)
    for key in ("market_data", "market_share", "macro_indicators", "market_trends"):
        for r in data.get(key) or []:
            if not isinstance(r, dict):
                continue
            try:
                rv = float(r.get("value"))
            except (TypeError, ValueError):
                continue
            ru = str(r.get("unit") or "")
            scaled_rv = rv * _value_scale(ru)
            if abs(scaled_rv - scaled_v) < max(0.5, abs(scaled_v) * 0.005) and (
                _units_equivalent(unit, ru)
            ):
                return True
    return False


_ARITH_METRIC_WORDS = (
    "换手率", "涨跌幅", "最新价", "成交额", "成交量", "收盘价", "开盘价",
    "营收", "净利润", "归母净利润", "毛利率", "毛利润", "总资产", "总负债",
    "经营现金流", "市占率", "份额",
)


def _eval_arith_expression(expr: str) -> float | None:
    """只允许"数字 + 四则运算 + 括号"的算式求值。

    报告文本是模型生成/外部拼接的**不可信输入**，因此不使用任何动态求值入口；
    这里显式解析语法树，只放行常量与四则运算节点，字符集校验作为第一道闸。
    """
    import ast as _ast
    if not re.fullmatch(r"[0-9.,\s*/+\-()]+", expr or ""):
        return None
    try:
        tree = _ast.parse(str(expr).replace(",", "").strip(), mode="eval")
    except SyntaxError:
        return None

    def _walk(node):
        if isinstance(node, _ast.Expression):
            return _walk(node.body)
        if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, _ast.USub):
            inner = _walk(node.operand)
            return None if inner is None else -inner
        if isinstance(node, _ast.BinOp):
            left, right = _walk(node.left), _walk(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, _ast.Add):
                return left + right
            if isinstance(node.op, _ast.Sub):
                return left - right
            if isinstance(node.op, _ast.Mult):
                return left * right
            if isinstance(node.op, _ast.Div):
                return None if right == 0 else left / right
        return None

    try:
        return _walk(tree)
    except Exception:
        return None


def _unit_profile(unit: str) -> tuple[str, float]:
    """单位 → (量纲类, 相对"元"的缩放)：pct / amount(元=1) / count。

    用于挡住"1200 万元 + 1000 万元 = 2200 亿元"这类**量纲错一万倍**的伪计算。
    """
    u = str(unit or "")
    if u in ("%", "％"):
        return ("pct", 1.0)
    for marker, factor in (("万亿", 1e12), ("千亿", 1e11), ("百亿", 1e10),
                           ("亿", 1e8), ("万", 1e4), ("元", 1.0)):
        if marker in u:
            return ("amount", factor)
    return ("count", 1.0)


def _collect_source_records(clean_text: str, user_text: str, sources: dict | None) -> list[dict]:
    """把各来源通道整理成 `(数值, 单位, 量纲, 缩放, 出处)` 记录列表——**不拼接文本**。

    结构化通道（clean_chart_data JSON）单独解析；用户材料按"数字+单位"成对抽取。
    这样结构化计算不会被另一通道的文本破坏。
    """
    records: list[dict] = []
    if clean_text:
        try:
            data = json.loads(clean_text)
            for row in (data.get("market_data") or []):
                if not isinstance(row, dict):
                    continue
                try:
                    val = float(row.get("value"))
                except (TypeError, ValueError):
                    continue
                cls, scale = _unit_profile(str(row.get("unit") or ""))
                records.append({"value": val, "unit": str(row.get("unit") or ""),
                                "class": cls, "scale": scale,
                                "currency": _currency_of(str(row.get("unit") or "")),
                                "label": str(row.get("label") or "")})
        except Exception:
            pass
    _um_text = str(user_text or "")
    _UNIT_RE = (r"(\d[\d,]*(?:\.\d+)?)\s*"
                r"(万亿|千亿|百亿|亿美元|亿港元|万美元|万港元|亿|万元|万|元|港元|美元|%)")
    # 用 finditer 拿**每个出现位置**（此前用 find(token) 取首次出现，同值多次出现会错位）
    for _m in re.finditer(_UNIT_RE, _um_text):
        token, unit = _m.group(1), _m.group(2)
        try:
            val = float(token.replace(",", ""))
        except ValueError:
            continue
        cls, scale = _unit_profile(unit)
        # 期间与指标按**所在子句**取，且指标取离数字最近的那个：
        # 跨子句的 ±20 字窗口会把相邻指标的词串到本数字上（实测踩过）。
        _c_start, _c_end = _clause_span(_um_text, _m.start(1), _m.end(2))
        _clause = _um_text[_c_start:_c_end]
        _at_in_clause = max(0, _m.start(1) - _c_start)
        records.append({"value": val, "unit": unit, "class": cls, "scale": scale,
                        "currency": _currency_of(unit),
                        "label": "user_material",
                        "period": _period_near(_clause, _at_in_clause),
                        "indicator": _indicator_near(_clause, _at_in_clause)})
    for k, v in (sources or {}).items():
        if k in ("clean_chart_data", "user_material"):
            continue        # 已单独处理，避免把 JSON 文本当记录源
        for token, unit in re.findall(
                r"(\d[\d,]*(?:\.\d+)?)\s*(万亿|千亿|百亿|亿美元|亿港元|万美元|万港元|亿|万元|万|元|港元|美元|%)", str(v or "")):
            try:
                val = float(token.replace(",", ""))
            except ValueError:
                continue
            cls, scale = _unit_profile(unit)
            records.append({"value": val, "unit": unit, "class": cls, "scale": scale,
                            "currency": _currency_of(unit), "label": k})
    return records


def _currency_of(unit: str) -> str:
    """单位 → 币种（CNY/HKD/USD；百分比等非金额返回空串）。

    币种必须参与操作数绑定：`万元` 与 `万美元` 相加/相除都是错币种，不能算通过。
    """
    u = str(unit or "")
    if "美元" in u:
        return "USD"
    if "港元" in u or "港币" in u:
        return "HKD"
    if "元" in u or u in ("万", "亿", "万亿", "千亿", "百亿"):
        return "CNY"
    return ""


def _operand_roles(expr: str) -> list[tuple[str, str]]:
    """列出算式里的操作数及其角色：`(token, "conversion"|"input")`。

    常量豁免必须**按位置**判定，不能用"整串里出现过合法用法"来豁免整串
    （实测缺陷：材料只有"2025 年收入 1000 万元"时，`1000/1-1` 里的分母 1 被
    末尾的 `-1` 连带豁免 → 认证成"增长 99900%"）。

    允许的换算角色只有两种，且必须真的处在该位置上：
    - `1`/`2`：作为 `+`/`-` 的右操作数**且是整式最后一个操作数**（`…-1` 这种增速调整）；
    - `100`：作为整式最后的 `*` 乘数（百分数换算）。
    其余位置一律按"财务输入"处理——必须有来源记录。
    """
    out: list[tuple[str, str]] = []
    matches = list(re.finditer(r"\d[\d,]*(?:\.\d+)?", str(expr or "")))
    for idx, m in enumerate(matches):
        tok = m.group(0)
        before = str(expr)[:m.start()].rstrip()
        prev_op = before[-1] if before else ""
        is_last = idx == len(matches) - 1
        after = str(expr)[m.end():].strip()
        conversion = False
        if tok in ("1", "2") and prev_op in ("+", "-") and is_last and not after:
            conversion = True
        elif tok == "100" and prev_op == "*" and is_last and not after:
            conversion = True
        out.append((tok, "conversion" if conversion else "input"))
    return out


_REQ_DATA_HINTS = ("数据", "数值", "数字", "财务", "营收", "收入", "净利润", "利润",
                   "现金流", "毛利", "指标", "多少", "增幅", "增速", "市场规模",
                   "出货量", "销量", "财报", "年报", "季报", "业绩")
_REQ_SOURCE_HINTS = ("来源", "引用", "出处", "官方", "公告", "链接", "权威", "原文",
                     "可核实", "佐证", "披露")
_REQ_DELIVERABLE_HINTS = ("报告", "表格", "图表", "清单", "对比", "提纲", "结论")
# 定性研究线索：明确"不需要数字/仅定性"时不得强迫造数字
_REQ_QUALITATIVE_HINTS = ("定性", "不需要具体数字", "无需具体数字", "只要观点",
                          "不必给出数字", "不需要数字", "仅需定性", "无需数据")


def derive_requirements(goal: str, capabilities=None) -> dict:
    """从**目标**推导明确任务要求（R0.1）。

    为什么要它：此前的严宽只按 financial/research 标签选，于是"目标要数据与官方来源、
    报告却什么都没有"在 research 档下照样 `overall=pass`（零数字/无来源反而更容易过）。
    这里把要求显式化：需要数据吗？需要来源吗？点名了哪些指标与期间？要交付什么？
    """
    g = str(goal or "")
    qualitative = any(h in g for h in _REQ_QUALITATIVE_HINTS)
    indicators = [w for w in _INDICATOR_WORDS if w in g]
    periods = sorted({m.group(1) for m in re.finditer(r"(20\d{2})\s*年", g)})
    needs_data = (not qualitative) and (
        any(h in g for h in _REQ_DATA_HINTS) or bool(indicators) or bool(periods)
    )
    needs_sources = any(h in g for h in _REQ_SOURCE_HINTS)
    deliverables = [h for h in _REQ_DELIVERABLE_HINTS if h in g]
    return {
        "qualitative": qualitative,
        "needs_data": needs_data,
        "needs_sources": needs_sources,
        "indicators": indicators,
        "periods": periods,
        "deliverables": deliverables,
    }


def check_requirement_coverage(goal: str, report_text: str, reqs: dict,
                               nt: dict, sources: dict | None,
                               source_list: dict | None = None) -> dict:
    """目标达成 vs 诚实披露**分开**输出（R0.1 的四态）。

    - `executed`：报告本体是否产出（非空、有正文段）；
    - `honest_disclosure`：缺的必需项是否被显式披露（"未披露/未获取/待补充/基于模型知识"）；
    - `goal_met`：目标点名的数据/来源是否**真的拿到**（可溯源数值 + 有来源清单/URL）；
    - `evidence`：数字可溯源率与来源条数（沿用 number_traceability / 来源清单的结论）。

    只有 `goal_met` 为真才可能 `pass`；缺失但诚实披露 → `partial`；缺失且未披露 → `fail`
    或 `unknown`（连正文都没有）。**允许交付诚实缺口稿，但不冒充合格研究成果。**
    """
    text = str(report_text or "")
    # "已执行"只看**有没有产出正文**：代码/数据类任务的报告就是一段交付说明，
    # 用长度阈值会把合法短报告误判成"没做"（实测回归：脚本任务的交付说明被判 unknown）
    executed = bool(text.strip())
    disclosed_markers = tuple(_DISCLOSED_MARKERS) + (
        "未披露", "未获取", "待补充", "未提供", "暂缺", "无法获取", "未公开",
    )
    lower = text
    gaps: list[str] = []
    # 数据要求
    data_ok = True
    if reqs.get("needs_data"):
        traceable = list((nt or {}).get("traceable") or [])
        computed = int((nt or {}).get("computed_count") or 0)
        covered = float((nt or {}).get("covered_ratio") or 0.0)
        total = int((nt or {}).get("total_count") or 0)
        wanted = [i for i in (reqs.get("indicators") or [])]
        # "点名指标是否给出可溯源数值"：逐个指标看有没有对应数字（此前那段
        # `for w ... if ... or True: pass` 是死循环，判定实际只落在 any() 上）
        wanted_ok = False
        for w in wanted:
            if any(w in str(t.get("raw") or "") or w in str(t.get("source") or "")
                   for t in traceable):
                wanted_ok = True
                break
        # 目标点名了指标时以"该指标是否有可溯源数值"为准；否则看整体覆盖
        if wanted:
            data_ok = wanted_ok or covered >= 0.5
        else:
            data_ok = (total > 0 and covered >= 0.5) or computed > 0
        if not data_ok:
            gaps.append(
                "目标要求给出" + ("、".join(wanted) if wanted else "具体数值")
                + f"，报告未提供可溯源数值（可溯源 {covered:.0%}，共 {total} 个数字）"
            )
    # 来源要求
    sources_ok = True
    if reqs.get("needs_sources"):
        src_items = list((source_list or {}).get("items") or [])
        urls = re.findall(r"https?://[^\s)>\]]+", text)
        cited = int((nt or {}).get("cited_count") or 0)
        src_keys = [k for k in (sources or {}) if k not in ("user_material",)]
        sources_ok = bool(src_items) or bool(urls) or cited > 0
        if not sources_ok:
            gaps.append("目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目")
    told = any(m in lower for m in disclosed_markers)
    if not executed:
        status = "unknown"
    elif data_ok and sources_ok:
        status = "pass"
    elif told:
        status = "partial"
    else:
        status = "fail"
    return {
        "pass": status == "pass",
        "status": status,
        "executed": executed,
        "honest_disclosure": told,
        "goal_met": bool(data_ok and sources_ok),
        "needs_data": bool(reqs.get("needs_data")),
        "needs_sources": bool(reqs.get("needs_sources")),
        "indicators": list(reqs.get("indicators") or []),
        "periods": list(reqs.get("periods") or []),
        "gaps": gaps,
        "counted": bool(gaps),
        "details": (
            "目标达成" if (data_ok and sources_ok)
            else ("缺失但已诚实披露" if told else "目标要求的数据/来源缺失且未披露")
        ),
        "applicable": True,
    }


def _normalized_numbers(text: str) -> str:
    """把正文里的数字归一化（去掉千分位与数字内空格），便于与底稿值逐字比对。"""
    return re.sub(r"(?<=\d)[,\u3000 ](?=\d)", "", str(text or ""))


# 占位/未完成标记：出现这些词说明该处**没有真的分析**，不能算"已解释"。
_PLACEHOLDER_MARKERS = (
    "待写", "待补充", "待完善", "待填", "尚未分析", "未分析", "未展开", "待核",
    "占位", "todo", "tbd", "n/a", "[待", "（待", "(待", "略）", "略。",
)


def _has_placeholder(text: str) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _sentences(text: str) -> list[str]:
    """粗切句：按句末标点与换行切分（含 Markdown 表格行）。"""
    parts = re.split(r"[。！？!?\n]+", str(text or ""))
    return [p.strip() for p in parts if p and p.strip()]


def _section_text(report: str, keywords: tuple[str, ...]) -> str:
    """取某个小节（标题含关键词）到下一个标题之间的正文。"""
    lines = str(report or "").splitlines()
    start = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and any(k in line for k in keywords):
            start = i + 1
            break
    if start < 0:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out).strip()


def _binding_tokens(metric: str, label: str) -> tuple[str, ...]:
    """该指标在正文里的**绑定词**：数值必须与这些词之一同句，才算"讲到了这个指标"。

    只写"同比/净利率"这类词而不给读数不算覆盖（架构复核 P2）；反过来，要求逐字命中
    改名后的完整标签（"归母净利率"）又太脆——报告写"净利率 12.61%"是合规表达。
    """
    m = str(metric or "")
    if m.endswith("_yoy"):
        return ("同比", "增速", "增长", "下降")
    table = {
        "net_margin": ("净利率",),
        "cashflow_coverage": ("现金流",),
        "debt_ratio": ("资产负债率", "负债率"),
        "rd_intensity": ("研发",),
    }
    if m in table:
        return table[m]
    return (str(label or ""),) if label else ()


def check_analysis_completeness(report: str, goal: str, task_id: str) -> dict:
    """**分析完整性**（研究任务）：正文有没有把已选事实讲全、讲成分析。

    三层分开判，缺一层说一层（架构复核 P2 的反例：六个裸数字 + "同比、净利率、现金流……
    尚未分析。风险：待写。结论：待写" 曾被判 pass——因为"概念词一出现就算分析"）：
    ① 数值覆盖：每个必需 (指标, 年度) 的数值是否出现；
    ② 解释覆盖：该数值所在的句子里是否同时出现指标名、且**没有**占位/未完成标记
       （"待写/尚未分析/待补充"不算解释）；派生指标要求**数值**出现，不接受只写"同比"两字；
    ③ 风险/结论覆盖：小节存在且去掉占位标记后仍有实质内容。

    实机教训（`ui-2084c2c9cc`）：底稿 6/6 事实 + 3 条同比齐全、硬门槛通过，正文却只写了
    2 个数字、没有任何分析——既有检查一个都拦不住。本检查**只提示、不计入 overall**
    （`counted=False`）：先把缺口显性化、观察一批真实任务，再决定是否收紧门禁。
    """
    gaps: list[str] = []
    try:
        from working_paper_export import build_result
        paper = build_result(task_id, goal)
    except Exception as exc:                     # 读不到底稿 → 不适用，不误报
        return {"pass": True, "counted": False, "applicable": False,
                "analysis_gaps": [], "gaps": [],
                "details": f"无底稿可核对（{str(exc)[:80]}）：不适用"}
    if not paper.get("ok"):
        return {"pass": True, "counted": False, "applicable": False,
                "analysis_gaps": [], "gaps": [],
                "details": "非研究任务（无结构化财务底稿）：不适用"}

    norm = _normalized_numbers(report)
    rows = list(paper.get("rows_detail") or [])
    derived = list(paper.get("derived_detail") or [])
    # 只把**契约点名的必需指标**算作"必须写进正文"；总资产/总负债这类支撑事实
    # 是比率的输入、不是交付要求，缺了不该报缺口（它们的意义体现在比率读数上）。
    _required = {str(m) for m in ((paper.get("request") or {}).get("required_metrics") or [])}
    required_rows = [r for r in rows
                     if not _required or str(r.get("metric") or "") in _required]
    sents = _sentences(report)

    def _num_forms(value) -> list[str]:
        out: list[str] = []
        for f in (f"{value}", f"{value:g}"):
            if f not in out:
                out.append(f)
        return out

    def _value_seen(value) -> bool:
        return any(f in norm for f in _num_forms(value))

    def _explained(label: str, value, metric: str) -> bool:
        """该值是否出现在"带该指标绑定词、且非占位"的句子里（按指标绑定，不靠全文任一处）。"""
        tokens = _binding_tokens(metric, label)
        for s in sents:
            if not _value_seen_in(s, value):
                continue
            if tokens and not any(t and t in s for t in tokens):
                continue
            if _has_placeholder(s):
                continue
            return True
        return False

    def _value_seen_in(sentence: str, value) -> bool:
        s = _normalized_numbers(sentence)
        return any(f in s for f in _num_forms(value))

    # ① 数值覆盖 + ② 解释覆盖（必需事实）
    numbers_missing: list[str] = []
    unexplained: list[str] = []
    for r in required_rows:
        label = str(r.get("metric_label") or r.get("metric") or "")
        tag = f"{label} {r.get('period')}（{r.get('value')}{r.get('unit') or ''}）"
        if not _value_seen(r.get("value")):
            numbers_missing.append(tag)
        elif not _explained(label, r.get("value"), str(r.get("metric") or "")):
            unexplained.append(tag)
    if numbers_missing:
        gaps.append("正文未给出必需事实的数值：" + "、".join(numbers_missing[:6])
                    + (f" 等 {len(numbers_missing)} 项" if len(numbers_missing) > 6 else ""))
    if unexplained:
        gaps.append("正文有数值但未与该指标绑定或标为未完成（不构成分析）："
                    + "、".join(unexplained[:6])
                    + (f" 等 {len(unexplained)} 项" if len(unexplained) > 6 else ""))

    # ② 派生指标：要求**读数**出现（只写"同比/净利率"这类词不算），且非占位
    derived_missing: list[str] = []
    for d in derived:
        label = str(d.get("metric_label") or d.get("metric") or "")
        tag = f"{label} {d.get('period')}（{d.get('value')}%）"
        if not _value_seen(d.get("value")):
            derived_missing.append(tag)
        elif not _explained(label, d.get("value"), str(d.get("metric") or "")):
            unexplained.append(tag)
    if derived_missing:
        gaps.append("正文未给出派生指标（同比/比率）的读数：" + "、".join(derived_missing[:6])
                    + (f" 等 {len(derived_missing)} 项" if len(derived_missing) > 6 else ""))
    # ③ 风险 / 结论覆盖：小节存在 + 去掉占位标记后仍有实质内容
    risk_text = _section_text(report, ("风险",))
    risk_ok = bool(risk_text) and not _has_placeholder(risk_text) and len(risk_text) >= 10
    if not risk_text:
        gaps.append("正文缺少风险提示段落")
    elif not risk_ok:
        gaps.append("风险提示段落是占位或过短（未构成风险描述）")
    concl_text = _section_text(report, ("结论", "小结", "总结"))
    concl_ok = bool(concl_text) and not _has_placeholder(concl_text) and len(concl_text) >= 10
    if not concl_text:
        gaps.append("正文缺少结论段落")
    elif not concl_ok:
        gaps.append("结论段落是占位或过短（未构成结论）")

    total = len(required_rows) + len(derived)
    return {
        "pass": not gaps,
        "counted": False,                        # 只提示：不进 overall/gaps 汇总
        "applicable": True,
        "analysis_gaps": gaps,
        "facts_total": len(required_rows), "facts_missing": len(numbers_missing),
        "facts_unexplained": len(unexplained),
        "derived_total": len(derived), "derived_missing": len(derived_missing),
        "risk_ok": risk_ok, "conclusion_ok": concl_ok,
        "gaps": gaps,
        "details": (
            f"分析要素覆盖完整（必需事实 {len(required_rows)} 项、派生 {len(derived)} 项都在正文，"
            "风险与结论有实质内容）"
            if not gaps else
            f"分析要素缺口 {len(gaps)} 类：数值缺 {len(numbers_missing)}/{len(required_rows)}、"
            f"解释缺 {len(unexplained)}、派生读数缺 {len(derived_missing)}/{len(derived)}、"
            f"风险{'有' if risk_ok else '缺'}、结论{'有' if concl_ok else '缺'}（共 {total} 项）"
        ),
    }


def _combo_semantics(combo: list[dict], expr: str, period: str, indicator: str,
                     res_cls: str) -> str:
    """操作数语义**联合**判定：每个必需操作数都要绑定指标/期间/币种。

    返回 `ok`（全部绑定一致）/ `unknown`（有操作数或报告上下文没绑定）/ `mismatch`（冲突）。
    实测缺陷：`2024年收入1000万元` + `2025年净利润1200万元` 时，
    `2025年净利润增长20%（1200/1000-1）` 因为"分子对上了"就被判 ok——分母错了指标与期间。
    """
    if not combo:
        return "mismatch"
    if not period and not indicator:
        return "unknown"
    # 币种必须一致（百分比结果不要求）
    currencies = {str(r.get("currency") or "") for r in combo}
    currencies.discard("")
    if len(currencies) > 1:
        return "mismatch"
    # 每个操作数都要有明确的指标与期间，否则整式只能停在 unknown
    for r in combo:
        if not (str(r.get("indicator") or "") or str(r.get("period") or "")):
            return "unknown"
    is_ratio = res_cls == "pct" or bool(re.search(r"[-+]\s*[12]\s*$|\*\s*100\s*$", str(expr or "")))
    pairs = [(str(r.get("period") or ""), str(r.get("indicator") or "")) for r in combo]
    if not is_ratio:
        # 加减/同尺度合成：所有操作数同指标同期
        for p, i in pairs:
            if period and p and p != period:
                return "mismatch"
            if indicator and i and indicator not in i and i not in indicator:
                return "mismatch"
        return "ok"
    # 比例/增速：分子=(期间,指标)，分母=(上一年,同指标)，允许顺序对调
    try:
        prev_year = str(int(period) - 1) if period else ""
    except ValueError:
        prev_year = ""
    for pair_a, pair_b in (pairs, list(reversed(pairs))):
        p_a, i_a = pair_a
        p_b, i_b = pair_b
        ok_a = (not indicator) or (indicator in i_a or i_a in indicator)
        ok_b = (not indicator) or (indicator in i_b or i_b in indicator)
        y_a = (not period) or p_a == period
        y_b = (not prev_year) or p_b == prev_year
        if ok_a and ok_b and y_a and y_b:
            return "ok"
    return "mismatch"


def _constant_role_ok(expr: str, tok: str) -> bool:
    """兼容包装：`tok` 在 `expr` 中是否存在**至少一个允许的换算角色位置**。

    新代码请直接用 `_operand_roles`（按位置逐操作数判定）；保留本函数仅供既有调用/测试。
    """
    return any(t == tok and role == "conversion" for t, role in _operand_roles(expr))


_INDICATOR_WORDS = ("营业收入", "归母净利润", "净利润", "毛利率", "收入", "营收",
                    "经营现金流", "现金流", "资本支出", "每股收益", "EPS", "毛利")


def _clause_span(text: str, start: int, end: int) -> tuple[int, int]:
    """数字所在**子句**的 [起, 止) 下标（以 ；;，,。.\n 为界）。

    实测缺陷：`2024 年收入 1000 万元，2025 年收入 1200 万元；2024 年毛利率 30%`
    里，1200 的 ±20 字窗口越过了分号，"毛利率"（词表里排在"收入"之前）被记成
    它的指标 → 逐操作数绑定时正确算式反被判成错配。
    """
    t = str(text or "")
    left = max([t.rfind(ch, 0, start) for ch in "；;，,。.\n"] + [-1])
    rights = [t.find(ch, end) for ch in "；;，,。.\n"]
    rights = [r for r in rights if r != -1]
    right = min(rights) if rights else len(t)
    return left + 1, right


def _clause_of(text: str, start: int, end: int) -> str:
    """数字所在子句的文本（`_clause_span` 的便捷包装）。"""
    a, b = _clause_span(text, start, end)
    return str(text or "")[a:b]


def _indicator_near(clause: str, at: int) -> str:
    """子句里**离数字最近**的指标词（只看数字之前），重叠时取更长者。"""
    best, best_end, best_len = "", -1, 0
    for w in _INDICATOR_WORDS:
        for m in re.finditer(re.escape(w), str(clause or "")):
            if m.end() > at:
                continue
            if m.end() > best_end or (m.end() == best_end and len(w) > best_len):
                best, best_end, best_len = w, m.end(), len(w)
    return best


def _period_near(clause: str, at: int) -> str:
    """子句里**离数字最近**的年份（只看数字之前）；取不到返回空串。

    同样不能用"子句里第一个年份"：句首的"公司2025年经营表现"会把后面的
    2024 年数字标成 2025，逐操作数绑定时正确算式因此被判期间错配。
    """
    best, best_end = "", -1
    for m in re.finditer(r"(20\d{2})\s*年", str(clause or "")):
        if m.start() > at:
            continue
        if m.end() > best_end:
            best, best_end = m.group(1), m.end()
    return best


def _context_semantics(prefix: str, window: str) -> tuple[str, str]:
    """从数字**之前的上下文**取 (期间, 指标)；取不到返回空串（=未知）。"""
    ctx = (str(prefix or "")[-40:] + " " + str(window or ""))
    period = ""
    m = re.search(r"(20\d{2})\s*年", ctx)
    if m:
        period = m.group(1)
    indicator = next((w for w in _INDICATOR_WORDS if w in ctx), "")
    return period, indicator


def _semantics_match(records: list[dict], period: str, indicator: str) -> str:
    """来源记录与报告上下文的语义匹配：`ok` / `unknown` / `mismatch`。

    - 报告没写期间与指标 → `unknown`（**可追溯但不升级为已验证金融结论**）；
    - 报告写了，且与来源记录一致 → `ok`；
    - 报告写了，但与所有来源记录都冲突 → `mismatch`（拒绝，如"2023 年净利润"配 2024/2025 收入材料）。
    """
    if not records:
        return "mismatch"
    if not period and not indicator:
        return "unknown"
    for r in records:
        blob = "%s %s %s" % (r.get("label") or "", r.get("period") or "", r.get("indicator") or "")
        if period and str(r.get("period") or "") and str(r["period"]) != period:
            continue
        if indicator and indicator not in blob:
            continue
        return "ok"
    return "mismatch"


def _records_for_value(records: list[dict], token: str) -> list[dict]:
    """按**数值**取候选来源记录（可能多条：同值不同来源/指标/单位）。"""
    try:
        val = float(str(token).replace(",", ""))
    except ValueError:
        return []
    return [r for r in records if abs(float(r["value"]) - val) <= 1e-9]


def _extract_balanced_expr(window: str) -> str:
    """取窗口开头 `（…）` 里**括号配对**的整段算式。

    为什么不能只用正则：增长率的标准写法是 `(1741.44 - 1505.6) / 1505.6 * 100`，
    懒匹配会在内层 `)` 处截断，得到不平衡的 `(1741.44 - 1505.6` → 判不可溯源
    （F1 实机简报因此掉到 74%）。这里按配对深度取整段；实质校验（求值、操作数能在
    来源里对上、量纲一致）一条不减。
    """
    t = str(window or "").lstrip()
    if not t or t[0] not in "（(":
        return ""
    depth = 0
    for i, ch in enumerate(t):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth -= 1
            if depth == 0:
                return t[1:i].strip()
    return ""


def _formula_derived_in_report(num: dict, window: str, records: list[dict],
                               prefix: str = "") -> tuple[bool, str]:
    """V1：报告里**紧邻数字**的完整公式，且操作数能按"值 + 单位"在来源记录里对上。

    严格三关（任一不满足即保持"未核实"，不授予计算值标记）：

    1. **完整表达式**：窗口必须以括号包住的整段算式开头，不做"截取局部二元式"的兜底
       ——否则 `1200/1000-1` 会被截成 `1200/1000`，把 20% 算成 120%；
    2. **操作数按值匹配**（带 token 边界），不接受数字子串命中（`12` 不能命中 `1200`）；
    3. **量纲/缩放绑定**：操作数之间必须同量纲同缩放；金额结果要求与操作数缩放一致
       （挡住 `1200+1000 = 2200 亿元`），百分比结果允许"同单位金额相除"或"百分比相减"。

    指标/期间语义尚未绑定（未实现部分保持未核实，不声称已验证）。
    """
    try:
        target = float(num["value"])
    except (TypeError, ValueError):
        return False, "mismatch"
    # 必须紧跟一个**括号配对**的完整算式（允许算式内部再有括号，如增长率的标准写法）
    expr = _extract_balanced_expr(window)
    if not expr or expr[0] not in "0123456789(（":
        return False, "mismatch"
    value = _eval_arith_expression(expr)
    if value is None:
        return False, "mismatch"
    res_cls, res_scale = _unit_profile(str(num.get("unit") or ""))
    if res_cls not in ("amount", "pct"):
        return False, "mismatch"
    candidates = [value, value * 100.0] if res_cls == "pct" else [value]
    if not any(abs(c - target) <= max(0.005 * abs(c), 0.01) for c in candidates):
        return False, "mismatch"

    toks_roles = _operand_roles(expr)
    if not toks_roles:
        return False, "mismatch"
    # 每个**输入**操作数的候选记录（同一数值可能有多条来源），稍后联合筛选；
    # 换算角色（末尾 -1 / *100）按位置豁免——分母位置上的 1 不在此列。
    option_lists: list[list[dict]] = []
    for tok, role in toks_roles:
        if role == "conversion":
            continue
        cands = _records_for_value(records, tok)
        if not cands:
            return False, "mismatch"                 # 缺输入（含把常量当财务输入）→ 拒绝
        option_lists.append(cands)
    if not option_lists:
        return False, "mismatch"

    period, indicator = _context_semantics(prefix, window)
    for combo in itertools.product(*option_lists):
        if len({(r["class"], r["scale"]) for r in combo}) != 1:
            continue                                 # 操作数之间量纲不一致
        cls, scale = combo[0]["class"], combo[0]["scale"]
        if res_cls == "amount" and not (cls == "amount" and abs(scale - res_scale) < 1e-9):
            continue                                 # 金额结果必须与操作数同量纲同缩放
        if res_cls == "pct" and cls not in ("pct", "amount"):
            continue
        sem = _combo_semantics(combo, expr, period, indicator, res_cls)
        if sem == "mismatch":
            continue                                 # 指标/期间/币种与来源冲突 → 换组合
        return True, sem                             # ok = 全部绑定一致；unknown = 未绑定
    return False, "mismatch"


def _arithmetic_derived_from_clean(num: dict, clean_text: str) -> bool:
    """B2：报告数字是否可由 clean 数据按指标算术导出（求和/均值/头部占比）。
    覆盖"TOP10总成交额 1161.03亿"（求和）、"平均换手率 4.49%"（均值）、
    "头部三强合计占比 37.5%"（前 k 项占比）这类计算值——它们不应被当
    "不可溯源"扣分并触发无效重做。容差 0.5%。"""
    try:
        data = json.loads(clean_text)
    except Exception:
        return False
    md = data.get("market_data") or []
    try:
        v = float(num["value"])
    except (TypeError, ValueError):
        return False
    groups: dict[str, list[float]] = {}
    for r in md:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "")
        metric = next((w for w in _ARITH_METRIC_WORDS if w in label), None)
        if not metric:
            continue
        try:
            x = float(r.get("value") or 0)
        except (TypeError, ValueError):
            continue
        groups.setdefault(metric, []).append(x)
    tol = max(abs(v) * 0.005, 0.005)
    for metric, xs in groups.items():
        if len(xs) < 2:
            continue
        s = sum(xs)
        if abs(s - v) <= tol:  # 求和（TOP10总成交额）
            return True
        if abs(s / len(xs) - v) <= tol:  # 均值（平均换手率）
            return True
        # 头部 k 项占比（37.5% = 前三/总）
        if num["unit"].endswith("%") and s:
            top = sorted(xs, reverse=True)
            for k in range(2, min(6, len(top)) + 1):
                if abs(sum(top[:k]) / s * 100 - v) <= tol:
                    return True
    return False


def _derived_traceable(num: dict, clean_text: str) -> bool:
    """派生值溯源：报告里的同比增速 / 约数金额可追溯到结构化数据。
    - % 值：与同指标相邻年份 (b/a-1)*100 一致（容差 0.05 个百分点）；
    - 金额：与结构化值在 2% 容差内（"突破3000亿"≈3030.52 亿）。"""
    try:
        data = json.loads(clean_text)
    except Exception:
        return False
    md = data.get("market_data") or []
    try:
        v = float(num["value"])
    except (TypeError, ValueError):
        return False
    if num["unit"].endswith("%"):
        by_metric: dict[str, list] = {}
        for r in md:
            if not isinstance(r, dict):
                continue
            if r.get("year") is None or r.get("value") is None:
                continue
            try:
                lbl = re.sub(r"^\d{4}年", "", str(r.get("label") or ""))
                by_metric.setdefault(lbl, []).append(
                    (int(r["year"]), float(r["value"]))
                )
            except (TypeError, ValueError):
                continue
        for pts in by_metric.values():
            pts.sort()
            for i in range(1, len(pts)):
                _y0, a = pts[i - 1]
                _y1, b = pts[i]
                if a and abs(abs(b / a - 1) * 100 - v) < 0.05:
                    return True
        return False
    scaled_v = v * _value_scale(num["unit"])
    for r in md:
        if not isinstance(r, dict):
            continue
        try:
            rv = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        scaled_rv = rv * _value_scale(str(r.get("unit") or ""))
        if scaled_rv and abs(scaled_rv - scaled_v) / scaled_rv <= 0.02:
            return True
    return False


# 溯源率阈值按任务域区分（F7 + P2-1）：
# - financial：0.7（财报数字必须高度可溯源）；
# - crypto/macro：0.5——依赖结构化适配器数据（CoinGecko/FRED），结构化覆盖
#   不足时如实降为 SUCCESS_WITH_ISSUES（不算虚假，不误判为模型编造）；
# - news：0.6（标题/摘要数字为主）；
# - research/general：0.2——≥5 个数字但可溯源 <20% 判 FAIL（疑似模型知识
#   未标注）；<5 个数字样本过少 → 通过但 details 注明。调用方可显式传
#   threshold 覆盖。
_TRACEABILITY_THRESHOLDS = {
    "financial": 0.7,
    "crypto": 0.5,
    "macro": 0.5,
    "news": 0.6,
    "research": 0.2,
}

# 财务域特征词（优先于 research/general，避免"分析腾讯财报"被归入调研域）
_FINANCIAL_MARKERS = (
    "财报", "年报", "季报", "营收", "净利润", "净利", "利润", "负债", "财务",
    "市值", "股票", "股价", "港交所", "港股", "a股", "美股", "上市公司",
    "业绩", "毛利率", "现金流", "financial", "revenue", "earnings",
)

# 调研/通用域特征词：报告、调研、研报、盘点、综述、文章、分析、研究等
_RESEARCH_MARKERS = (
    "报告", "调研", "研报", "盘点", "综述", "文章", "分析", "研究",
    "report", "research", "analysis", "survey",
)


def traceability_domain(goal: str) -> str:
    """从任务目标判定数字溯源域：crypto/macro/news/financial/research。

    P2-1：未命中专业域（crypto/macro/news/financial）的目标一律归入
    research/general 兜底域，按调研报告覆盖率规则验收；"分析腾讯财报"
    这类目标因含财报/营收等财务特征词仍按 financial 从严判定。
    """
    g = str(goal or "").lower()
    if any(
        k in g for k in (
            "加密货币", "比特币", "以太坊", "币价", "虚拟货币", "数字货币",
            "btc", "eth", "bitcoin", "ethereum", "crypto", "coin",
        )
    ):
        return "crypto"
    if any(
        k in g for k in (
            "gdp", "cpi", "通胀", "通货膨胀", "失业率", "宏观", "宏观经济",
            "pmi", "消费者物价",
        )
    ):
        return "macro"
    if any(
        k in g for k in (
            "最新新闻", "头条", "要闻", "今日新闻", "实时新闻", "新闻资讯",
            "news", "headline",
        )
    ):
        return "news"
    if any(k in g for k in _FINANCIAL_MARKERS):
        return "financial"
    if any(k in g for k in _RESEARCH_MARKERS):
        return "research"
    return "research"


def _subject_of(text: str) -> str:
    """从一段文本里取**主体**（公司/实体名）；取不到返回空串（=未知）。

    复用任务分类器的公司提取（中英文都支持），因此"宁德时代/比亚迪/Apple/AAPL"
    都能被认出来。**未知不等于冲突**：只有两边都已知且不同才判冲突，
    避免把没写主体的报告一律否掉。
    """
    try:
        from task_classifier import _extract_company
        return str(_extract_company(str(text or "")) or "")
    except Exception:
        return ""


def _subjects_conflict(report_subject: str, source_subject: str) -> bool:
    """主体冲突：两边都已知、且互不包含才算冲突。"""
    a = str(report_subject or "").strip()
    b = str(source_subject or "").strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return False
    return True


def _line_with(text: str, needle: str) -> str:
    """取包含 `needle` 的那一行（financials 通道每行都带实体前缀）。"""
    for line in str(text or "").splitlines():
        if needle and needle in line:
            return line
    return ""


def _subject_conflict_for(n: dict, report: str, goal: str, source_text: str,
                          candidates: list[str]) -> bool:
    """报告里的这个数字与命中来源的**主体**是否冲突（数字-主体绑定）。

    实测缺口（架构复核点名）：报告写"宁德时代营收 1741 亿元"、来源其实是
    "比亚迪营收 1741 亿元"，只比 `(值, 单位)` 会判可溯源。这里补主体维度：
    报告侧主体取自数字所在子句，取不到再退回**任务目标**（报告通常只说一家公司）；
    来源侧主体取自命中行。
    """
    pos = int(n.get("pos") or 0)
    raw = str(n.get("raw") or "")
    # 只用**紧邻数字的子句**里写明的公司：不拿任务目标兜底。
    # 目标兜底会把"多实体对比报告"和"代码/全称别名不一致"误判成主体冲突（假阴性），
    # 而架构复核明确要求不能靠收紧把真话否掉。子句提不出主体 → 视为未知 → 不冲突。
    report_subject = _subject_of(_clause_of(str(report or ""), pos, pos + len(raw)))
    if not report_subject:
        return False
    # 定位"包含这个数字的那一行"：候选串可能带空格/单位差异（"6000.0 亿元" vs
    # "6000.0亿元"），退而用数值核心匹配。**找不到行就按未知处理、不判冲突**——
    # 退化成"整块文本"会把主体取成第一家公司，在对比类报告里制造假阴性。
    needles = [str(n.get("value") or "")] + [str(c) for c in candidates if c]
    for needle in needles:
        if not needle:
            continue
        line = _line_with(source_text, needle)
        if not line:
            continue
        src_subject = _subject_of(line)
        if src_subject and _subjects_conflict(report_subject, src_subject):
            return True
    return False


def check_number_traceability(
    report: str,
    sources: dict,
    threshold: float | None = None,
    domain: str | None = None,
    goal: str = "",
    subject_check: bool = True,
) -> dict:
    """数字溯源校验：报告中的数字能否在检索/快照/清洗/结构化数据中找到。
    threshold 默认按 domain 取 _TRACEABILITY_THRESHOLDS；未给 domain 时用 0.7。
    返回 {pass, details, total_count, covered_ratio, traceable_count,
    unverifiable_count, ...}。P2-1 按域区分：research/general 若数字 ≥5 且
    可溯源 <20% 判 FAIL，<5 个数字通过但注明样本过少。"""
    if threshold is None:
        threshold = _TRACEABILITY_THRESHOLDS.get(domain or "", 0.7)
    nums = extract_financial_numbers(report)
    total = len(nums)
    if total == 0:
        return {
            "pass": True,
            "details": "报告未检出需要溯源的财务数字",
            "total_count": 0, "traceable_count": 0, "unverifiable_count": 0,
            "covered_ratio": 0.0,
            "traceable": [],
            "untraceable": [],
        }
    src_norm = {k: _norm(v) for k, v in sources.items() if v}
    # 主体抽取要用**未归一化**的原文：`_norm` 会去掉换行，行内的实体前缀就取不到了
    src_raw = {k: str(v) for k, v in sources.items() if v}
    clean_text = sources.get("clean_chart_data") or ""
    # V1：用户材料本身是**来源**（不证明内容真实），但必须**分通道**处理：
    # clean_chart_data 是结构化 JSON，任何文本拼接都会让结构化解析失败
    # （实测：仅加入 user_material 文本，就把原本 pass 的同比计算判成 fail）。
    user_text = str((sources or {}).get("user_material") or "")
    # 公式核验的来源记录（结构化记录 + 用户材料里带单位的数字），不拼成一段文本
    source_records = _collect_source_records(clean_text, user_text, sources)
    traceable: list[dict] = []
    untraceable: list[dict] = []
    disclosed: list[dict] = []
    for n in nums:
        hit = None
        if clean_text:
            # clean_chart_data 命中**同样要过主体归属**：此前它优先接受并直接定案，
            # 绕过了后面的子句主体筛选——于是"clean 里是 B 公司的数字"也能给
            # "报告写 A 公司"的数字当来源（S1-1 定位的 P1）。
            # 修法是让**所有来源通道共用同一套归属校验**，不给结构化源开特例。
            _clean_kind = ""
            if _traceable_in_clean(n, clean_text):
                _clean_kind = "clean_chart_data"
            elif _derived_traceable(n, clean_text):
                _clean_kind = "derived_from_clean"
            if _clean_kind:
                _cands_clean = [c for c in _candidates(n) if c and c in clean_text] \
                    or list(_candidates(n))
                if subject_check and _subject_conflict_for(
                        n, report, goal, clean_text, _cands_clean):
                    logger.debug("clean 命中因主体不符被跳过：%s", n.get("value"))
                else:
                    hit = _clean_kind
        if hit is None:
            if n["unit"]:
                for k, st in src_norm.items():
                    cands = [c for c in _candidates(n) if c and c in st]
                    if not cands:
                        continue
                    # 数字-主体绑定：命中来源属于**另一家公司**时不算可溯源
                    if subject_check and _subject_conflict_for(
                            n, report, goal, src_raw.get(k, st), cands):
                        continue
                    hit = k
                    break
            else:
                for k, st in src_norm.items():
                    if not _bare_match(n["value"], st):
                        continue
                    if subject_check and _subject_conflict_for(
                            n, report, goal, src_raw.get(k, st), [str(n["value"])]):
                        continue
                    hit = k
                    break
        item = {"raw": n["raw"], "value": n["value"], "unit": n["unit"]}
        if not hit and clean_text and _arithmetic_derived_from_clean(n, clean_text):
            hit = "derived_computed"
        # V1：报告内写明的**完整公式**（紧邻数字），且操作数能按值+单位+指标/期间联合匹配
        _derived_sem = ""
        if not hit:
            _win_start = int(n.get("pos") or 0) + len(str(n.get("raw") or ""))
            _ok, _derived_sem = _formula_derived_in_report(
                n, report[_win_start:_win_start + 40], source_records,
                prefix=report[max(0, _win_start - 40):_win_start])
            if _ok:
                hit = "derived_computed"
        if hit:
            item["source"] = hit
            item["derived"] = hit in ("derived_from_clean", "derived_computed")
            if hit == "user_material":
                # 与用户提供的材料一致：**是来源**，但真实性未经独立核实
                item["user_provided"] = True
                item["verified"] = False
            if hit == "derived_computed":
                # 派生值的四类结论**分开输出**（不得合并成"已验证"）：
                # 数值可追溯 + 算术正确 已成立；指标/期间可能仍未绑定；外部核实继承用户输入。
                item["arithmetic_ok"] = True
                item["indicator_period"] = _derived_sem or "unknown"
                item["verified"] = False
                if (item["indicator_period"] or "unknown") == "unknown":
                    item["semantics_unverified"] = True
            traceable.append(item)
        else:
            # 数字后紧跟"基于模型知识/未验证"标注 → 已披露，不算缺口
            after = report[n["pos"]:n["pos"] + 60]
            if any(m in after for m in _DISCLOSED_MARKERS):
                item["disclosed"] = True
                disclosed.append(item)
            else:
                untraceable.append(item)
    rate = traceable.__len__() / total
    covered_ratio = round(rate, 3)
    disclosed_rate = len(disclosed) / total
    # 细分：财务金额（亿/万/元/美元单位）与 其他数字（%等）分开统计
    amounts = [n for n in nums if any(u in n["unit"] for u in ("亿", "万", "元", "美元"))]
    traceable_keys = {(t.get("value"), t.get("unit")) for t in traceable}
    amount_ok = sum(1 for n in amounts if (n["value"], n["unit"]) in traceable_keys)
    amount_rate = (amount_ok / len(amounts)) if amounts else 1.0
    # P2-1：按域区分判定强度
    # - research/general：≥5 个数字但可溯源 <20% → FAIL（疑似模型知识未标注）；
    #   <5 个数字 → 通过但注明样本过少。
    # - 其他域：rate ≥ threshold 或总数 <3 时通过（financial 70% 阈值不变）。
    research_note = ""
    if domain == "research":
        if total >= 5 and rate < threshold:
            passed = False
            research_note = (
                f"调研报告数字覆盖率过低：{len(traceable)}/{total}"
                "，疑似模型知识未标注"
            )
        else:
            passed = True
            if total < 5:
                research_note = f"数字样本过少（{total} 个）"
    else:
        passed = rate >= threshold or total < 3
    # B2 三档分类：引用值 / 计算值（算术可验证）/ 模型知识（已披露标注）
    _computed = [t for t in traceable if t.get("source") == "derived_computed"]
    _cited = [t for t in traceable if t not in _computed]
    # V1：用户材料单独计数（不再与"模型知识"混为一谈，也不算"不可溯源"）
    _user_input = [t for t in traceable if t.get("user_provided")]
    details = (
        (research_note + "；" if research_note else "")
        + f"数字溯源率 {rate:.0%}（{len(traceable)}/{total}）"
        + f"；引用 {len(_cited)} / 计算 {len(_computed)} / 模型知识 {len(disclosed)}"
        + f"；财务金额溯源率 {amount_rate:.0%}（{amount_ok}/{len(amounts)}）"
        + (f"；域={domain}" if domain else "")
        + (f"；结构化数据覆盖 {len(traceable)}/{total}" if "structured_data" in src_norm else "")
        + (f"；已披露（模型知识标注）{disclosed_rate:.0%}（{len(disclosed)}）" if disclosed else "")
        + ("" if passed else f"，低于阈值 {threshold:.0%}")
        + (f"；不可溯源示例：{'、'.join(u['raw'][:20] for u in untraceable[:5])}" if untraceable else "")
    )
    return {
        "pass": passed,
        "details": details,
        "domain": domain or "",
        "threshold": round(float(threshold), 3),
        "total_count": total,
        "traceable_count": len(traceable),
        "covered_ratio": covered_ratio,
        "unverifiable_count": len(untraceable),
        "amount_rate": round(amount_rate, 3),
        "amount_traceable": amount_ok,
        "amount_total": len(amounts),
        "disclosed_count": len(disclosed),
        "user_input_count": len(_user_input),
        "computed_count": len(_computed),
        "cited_count": len(_cited),
        "traceable": traceable,
        "untraceable": untraceable[:10],
    }


# ─────────────────────────────────────────────
# 主体归属检查：数字是否属于目标公司
# ─────────────────────────────────────────────

_OTHER_ENTITIES = (
    "Line", "阿里巴巴", "阿里", "字节跳动", "字节", "百度", "京东",
    "美团", "小米", "华为", "苹果", "微软", "谷歌", "网易", "快手", "拼多多",
    "滴滴", "联想", "中兴", "三星", "索尼", "亚马逊", "奈飞", "软银",
    "日本通讯", "国民银行", "大和证券", "Lululemon", "露露柠檬",
    "Nike", "耐克", "Adidas", "阿迪达斯", "特斯拉",
)

# 归属校验的语境词：核心财务声明词（营收/利润/资产…）触发判定；
# 投资/收购等关系动词（钱属于投资方）不算污染
_CORE_FIN_WORDS = (
    "营收", "收入", "净利润", "净利", "利润", "毛利率", "总资产", "总负债",
    "负债", "资产", "现金流", "销售额", "占比", "经营利润", "市值",
)
_RELATION_VERBS = ("投资", "入股", "收购", "融资", "获得", "出资", "补贴", "捐赠", "认购")
_NON_CORP_COMPOUNDS = (
    "百度百科", "百度知道", "百度贴吧", "百度文库", "谷歌学术",
    "维基百科", "微软百科", "阿里云盘",
)


def _entity_in(ctx: str, e: str) -> bool:
    """实体是否出现在上下文（剔除百科/文库等平台名里的公司字）。"""
    c = str(ctx or "")
    for comp in _NON_CORP_COMPOUNDS:
        if e in comp:
            c = c.replace(comp, "")
    return e in c


def _target_entity(goal: str) -> str:
    """从目标提取公司主体（腾讯/恒大/特斯拉…）；取"集团/公司/控股"前的最长 2-4 字。"""
    g = str(goal or "")
    try:
        from task_classifier import _extract_company
        c = _extract_company(g)
        if c:
            return c
    except Exception:
        pass
    m = re.search(r"([\u4e00-\u9fff]{2,6}?)(?:集团|控股|公司)", g)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z][A-Za-z0-9\-]{1,10})", g)
    return m.group(1) if m else ""


def _locate_and_context(source_text: str, value: str, unit: str, radius: int = 80):
    """在源文本中定位数字，返回【包含该数字的句子】。
    句子级归属：标题/相邻句出现目标公司不算归属证据（如"官宣与腾讯合作"的
    Line 新闻里，4% 是 Line 的，不是腾讯的）。"""
    st = _norm(source_text)
    cands = _candidates({"value": value, "unit": unit})
    if not cands:
        cands = [value]
    sentences = re.split(r"[。！？；;\n]+", st)
    hits: list[tuple[str, str]] = []
    for c in cands:
        for i, s in enumerate(sentences):
            if c in s:
                prev = sentences[i - 1] if i > 0 else ""
                hits.append((s, prev))
        if hits:
            break
    return hits


def _is_trusted_number(n: dict) -> bool:
    """结构化/清洗/派生值直接信任：不再进入网络源证据检查。
    （P1-1：比亚迪 80.72% 是东财 HOLDER_PROFIT_YOY 派生值，不应因网络源
    上下文里出现页面噪音实体而被判污染。）"""
    if n.get("derived") is True:
        return True
    src = str(n.get("source") or "")
    return src in (
        "clean_chart_data", "derived_from_clean",
        "structured", "structured_financials", "structured_data",
    )


def _nearby_entity(src_text: str, num: dict, entity: str, radius: int = 80) -> bool:
    """实体是否出现在数值 ±radius 字符窗口内。
    用于过滤导航/分享按钮等页面噪音（如"新浪新闻快手"账号矩阵里的"快手"），
    只有真正与数值相邻的他司实体才构成归属证据。"""
    if entity not in src_text:
        return False
    cands = _candidates(num) or [str(num.get("value") or "")]
    for c in cands:
        idx = src_text.find(c)
        while idx >= 0:
            start = max(0, idx - radius)
            end = min(len(src_text), idx + len(c) + radius)
            if entity in src_text[start:end]:
                return True
            idx = src_text.find(c, idx + 1)
    return False


def _prev_near_entity(sprev: str, entity: str, radius: int = 80) -> bool:
    """前一句的实体是否靠近数值（数值紧接前句之后，取前句尾部 ±radius 字符）。
    Line 案例的"日本通讯App Line"在句尾 → 命中；页面导航的"快手"在长句开头
    → 不命中。"""
    if entity not in sprev:
        return False
    tail = sprev[-radius:] if len(sprev) > radius else sprev
    return entity in tail


def check_entity_attribution(
    report: str, sources: dict, goal: str,
) -> dict:
    """主体归属检查：报告/清洗数据中的数字应属于目标公司。
    对每个可溯源数字，以【报告句子】判定归属：报告句子含其他公司实体且不含目标主体
    → 标记为归属污染（如把 Line 的 4% 当腾讯的写进句子）。
    不再按源文本上下文判定——报告里腾讯"营收增速30%"与源文本里 AWS"运营利润率约30%"
    是不同指标的同名数值，源上下文含其他实体不代表报告引用错误。"""
    target = _target_entity(goal)
    contaminated: list[dict] = []
    ambiguous: list[dict] = []
    checked = 0
    if not target:
        return {"pass": True, "details": "无法从目标提取主体，跳过", "checked_count": 0,
                "contaminated_count": 0, "contaminated": []}

    def _web_other_only(num: dict, target: str) -> bool:
        """数字在所有网络源（search/fetch）上下文里只归其他公司、从未出现在目标
        上下文 → True（如 Line 的 4% 被写进腾讯报告）。结构化数据不参与本判定。"""
        other_found = False
        for src_key, src_text in sources.items():
            if src_key == "clean_chart_data" or not src_text:
                continue
            for sctx, sprev in _locate_and_context(src_text, num["value"], num["unit"]):
                if target in sctx:
                    return False
                # P1-1：目标允许出现在前一句（对称于他司检查），
                # 但前句若同时混入他司（"官宣与腾讯合作，Line 用户流失"类合作新闻）
                # 不算归属证据——保护 Line 4% 真污染检测
                prev_others = [
                    e for e in _OTHER_ENTITIES
                    if _entity_in(sprev, e) and e.lower() != target.lower()
                ]
                if target in sprev and not prev_others:
                    return False
                s_others = [
                    e for e in _OTHER_ENTITIES
                    if e.lower() != target.lower()
                    and (
                        _nearby_entity(sctx, num, e)
                        or _prev_near_entity(sprev, e)
                    )
                ]
                if s_others and any(w in sctx for w in _CORE_FIN_WORDS):
                    other_found = True
        return other_found

    # 1) 报告中的数字（可溯源部分）：以报告句子判定归属
    # 归属检查自己会做实体推理；这里关掉主体冲突筛选，避免两套判断互相污染
    trace = check_number_traceability(report, sources, goal=goal, subject_check=False)
    for n in trace.get("traceable", []):
        ctxs = _locate_and_context(report, n["value"], n["unit"])
        for ctx, prev in ctxs:
            checked += 1
            others = [
                e for e in _OTHER_ENTITIES
                if _entity_in(ctx, e)
                and e.lower() != target.lower()
            ]
            target_here = target in ctx
            has_rel = any(v in ctx for v in _RELATION_VERBS)
            has_core = any(w in ctx for w in _CORE_FIN_WORDS)
            if has_rel:
                # 投资/收购等关系句（"腾讯投资X亿元"）不算污染
                continue
            # 报告句子明确提到其他公司（如"低于苹果（约25%）"）是诚实的同行对比，
            # 不是污染；真污染由下方"目标句子 + 源证据"分支捕获（Line 的 4% 案例）
            if target_here and not others and not _is_trusted_number(n):
                # 报告声明数字属于目标：若它只出现在其他公司的网络源上下文
                # （从未出现在目标上下文）→ 污染（Line 的 4% 被归入腾讯案例）
                if _web_other_only(n, target):
                    contaminated.append({
                        "value": f"{n['raw']}",
                        "entity": "网络源证据指向其他公司",
                        "context": ctx[:90],
                    })
            elif not target_here and not others:
                # 中性句子（无任何公司实体）：信任报告归属，避免
                # 源文本碰巧出现同名数值（如 AWS"运营利润率约30%"）误报
                ambiguous.append({"value": n["raw"], "context": ctx[:70]})

    # 2) 清洗数据行（含来源 URL 的数值行）
    clean_text = sources.get("clean_chart_data") or ""
    if clean_text:
        try:
            data = json.loads(clean_text)
            for key in ("market_data", "market_share", "macro_indicators", "market_trends"):
                for r in data.get(key) or []:
                    if not isinstance(r, dict):
                        continue
                    val = str(r.get("value") or "")
                    unit = str(r.get("unit") or "")
                    src_url = str(r.get("source") or "")
                    label = str(r.get("label") or "")
                    # 行 label 已明确归属目标主体（如"腾讯营收"）→ 归属正确，不查源上下文
                    if target and target in label:
                        continue
                    # label 把行归给其他公司 → 直接污染（如"Line占比"行）
                    label_others = [
                        e for e in _OTHER_ENTITIES
                        if e in label and e.lower() != target.lower()
                    ]
                    if label_others:
                        contaminated.append({
                            "value": f"{label} = {val}{unit}",
                            "entity": "、".join(label_others[:2]),
                            "context": f"label: {label[:90]}",
                        })
                        continue
                    # 中性 label 行：值只出现在其他公司的网络源上下文 → 污染
                    if _web_other_only({"value": val, "unit": unit}, target):
                        contaminated.append({
                            "value": f"{label} = {val}{unit}",
                            "entity": "网络源证据指向其他公司",
                            "context": f"label: {label[:60]} / source: {src_url[:60]}",
                        })
                        continue
                    checked += 1
        except Exception:
            pass
    # 去重
    seen = set()
    uniq = []
    for c in contaminated:
        k = (c["value"], c["entity"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    passed = len(uniq) == 0
    details = (
        f"主体归属校验 {checked} 处上下文，污染 {len(uniq)} 处"
        + (f"（{uniq[0]['value']} 属 {uniq[0]['entity']}）" if uniq else "，无不属于目标公司的数字")
        + (f"；归属模糊 {len(ambiguous)} 处" if ambiguous else "")
    )
    return {
        "pass": passed,
        "details": details,
        "checked_count": checked,
        "contaminated_count": len(uniq),
        "contaminated": uniq[:10],
        "ambiguous_count": len(ambiguous),
    }


# ─────────────────────────────────────────────
# 来源标注诚实性检查
# ─────────────────────────────────────────────

_DOMAIN_MEDIA = {
    "sina.com.cn": "新浪", "ofweek.com": "OFweek",
    "21jingji.com": "21财经", "21cn.com": "21财经",
    "yicai.com": "第一财经", "xueqiu.com": "雪球", "qianzhan.com": "前瞻",
    "jiemian.com": "界面", "zhihu.com": "知乎", "toutiao.com": "今日头条",
    "163.com": "网易", "eastmoney.com": "东方财富", "10jqka.com.cn": "同花顺",
    "pedaily.cn": "投资界", "baijing.cn": "白鲸出海", "cls.cn": "财联社",
    "infoq.cn": "InfoQ", "gov.cn": "中国政府网", "hkex.com.hk": "港交所",
    "tencent.com": "腾讯官网", "ir.tencent.com": "腾讯投资者关系",
    "csdn.net": "CSDN博客", "woshipm.com": "人人都是产品经理",
    "meituan.com": "美团官网", "weiyangx.com": "未央网",
    "sgpjbg.com": "三个皮匠报告", "alishui.com": "满银", "baidu.com": "百度",
    # BUG-6 补录：实测被误判"虚假标注"的真实抓取域名
    "100est.com": "百优价值网", "faxiangongchang.com": "天下工厂",
    "9fzt.com": "九方智投", "cofool.com": "叩富网",
    "xiniudata.com": "新牛数据", "dfcfw.com": "东方财富PDF",
    "cninfo.com.cn": "巨潮资讯网",
    "nbd.com.cn": "每日经济新闻", "hstong.com": "华盛通",
    "ykzq.com": "粤开证券", "fddi.fudan.edu.cn": "复旦金融研究院",
    # 行业调研实测补录：检索实际返回的中文行业研究/问卷站点
    "chinairn.com": "中研网", "chinabaogao.com": "中国报告网",
    "wenjuan.com": "问卷网",
}

# 媒体别名组：声明中出现的别名与已知媒体名归组匹配。
# 例：报告写"21经济网"而域名映射为"21财经"、转载署名"21世纪经济报道"，
# 三者应视为同一来源；"新浪财经/新浪新闻"与"新浪"同理。
_MEDIA_ALIAS_GROUPS = (
    ("21财经", "21经济网", "21世纪经济报道"),
    ("新浪", "新浪财经", "新浪新闻"),
    ("网易", "网易财经", "网易新闻"),
    ("腾讯", "腾讯新闻", "腾讯财经"),
    ("凤凰", "凤凰网", "凤凰财经"),
    ("第一财经", "第一财经日报"),
    ("财联社", "财联社电报"),
    ("天下工厂", "天下工厂产业研究院", "法向工厂"),
    ("东吴证券", "东吴证券研究所"),
    ("九方智投", "九方"),
    ("百优价值网", "百优"),
    ("中研网", "中研普华"),
)

# A2：已登记的媒体名/别名集合——已登记的词不再重复建议补录
# 无固定域名的权威机构（SNE Research 是韩国动力电池研究机构，报告常引用）
_EXTRA_MEDIA_NAMES = ("SNE Research", "SNE", "FRED")

_KNOWN_MEDIA_NAMES = frozenset(_DOMAIN_MEDIA.values()) | frozenset(
    name for grp in _MEDIA_ALIAS_GROUPS for name in grp
) | frozenset(_EXTRA_MEDIA_NAMES)

# 形如「XX网/XX报/XX财经/XX新闻」的媒体词；
# 尾随否定避免把「XX财经网」拆成「XX财经」+「网」两个候选
_MEDIA_WORD_RE = re.compile(
    r"([\u4e00-\u9fff]{2,8}?)(网|新闻|财经|报)(?!网|新闻|财经|报)"
)

# 域名线索（可选提取，不强求）：http(s) URL 主机名或裸二级域名
_DOMAIN_CLUE_RE = re.compile(
    r"https?://([^/\s，。；、]+)"
    r"|([a-z0-9][a-z0-9-]*\.(?:com|cn|net|org|info|io|cc|com\.cn|co\.uk|gov\.cn))",
    re.IGNORECASE,
)


def _normalize_domain_clue(raw: str) -> str:
    """归一化域名线索：去 www. 前缀与路径，仅保留主机名。"""
    d = str(raw or "").strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d


def suggest_domain_media(claim: str) -> list[str]:
    """从来源声明文本中提取疑似媒体词（形如「XX网/XX报/XX财经/XX新闻」），
    供测试与未来人工确认流程使用，作为 _DOMAIN_MEDIA 静态表的补录建议。
    若声明同时含域名线索则附带提示；只建议、不自动改表。"""
    if not claim:
        return []
    suggestions: list[str] = []
    seen: set[str] = set()
    domain_hint = ""
    for m in _DOMAIN_CLUE_RE.finditer(claim):
        raw = (m.group(1) or m.group(2) or "").strip()
        if raw:
            domain_hint = _normalize_domain_clue(raw.split("/", 1)[0])
            break
    for m in _MEDIA_WORD_RE.finditer(claim):
        prefix, suffix = m.group(1), m.group(2)
        # 排除「年报/财报/月报/周报/公报/报表/报道」等非媒体词
        if suffix == "报" and prefix[-1:] in ("年", "财", "月", "周", "公", "表", "道"):
            continue
        word = m.group(0)
        if word in _KNOWN_MEDIA_NAMES or word in seen:
            continue
        seen.add(word)
        suggestions.append(
            word + (f"（域名线索：{domain_hint}）" if domain_hint else "")
        )
    return suggestions


def _known_sources(sources: dict) -> dict:
    """从检索/快照/清洗数据构建已知来源集合：URL、域名、媒体名、标题。"""
    urls: set[str] = set()
    domains: set[str] = set()
    media: set[str] = set()
    titles: list[str] = []
    for key, text in sources.items():
        if key == "clean_chart_data":
            try:
                for r in json.loads(text).get("market_data", []):
                    u = str(r.get("source") or "")
                    if u.startswith("http"):
                        urls.add(u)
            except Exception:
                pass
            continue
        for u in re.findall(r"https?://[^\s\)\]\"]+", text):
            urls.add(u)
        for m in re.finditer(r"https?://([^/\s]+)", text):
            dom = m.group(1).lower().replace("www.", "")
            domains.add(dom)
            for k, name in _DOMAIN_MEDIA.items():
                if k in dom:
                    media.add(name)
        for t in re.findall(r"(?:title|标题)[：:]\s*([^\n]{4,60})", text):
            titles.append(t)
        # 拍平文本形态：标题紧邻 URL 之前（"…东吴证券研究所 1/39 … https://…"）。
        # 取整行而非固定窗口，避免窗口截断把券商名切掉（限长 120 防吞行）
        for m in re.finditer(r"https?://[^\s\]\)\"]+", text):
            ls = text.rfind("\n", 0, m.start()) + 1
            pre = text[ls:m.start()][-120:]
            t = re.sub(r"^\s*\d+\s*", "", pre).strip(" -—_。;；")
            if len(t) >= 4:
                titles.append(t)
        # P1-2：署名/转引媒体识别——如"同花顺_新浪新闻"、"来源：同花顺"、
        # "XX转载"等，把署名媒体名并入已知媒体集合（内容真实存在于抓取正文时，
        # 声明"同花顺财务诊断"不应因域名是 k.sina.com.cn 而误判虚假标注）
        for m in re.finditer(
            r"([\u4e00-\u9fffA-Za-z0-9]{2,12})[_\s]*(?:新浪新闻|新浪财经|网易|搜狐|"
            r"腾讯新闻|凤凰网|界面|第一财经|21世纪经济报道|东方财富)",
            text,
        ):
            media.add(m.group(1).strip())
        for m in re.finditer(
            r"(?:来源|转自|转载自|原文来自)[：:]\s*([\u4e00-\u9fffA-Za-z0-9]{2,12})",
            text,
        ):
            media.add(m.group(1).strip())
    # 标题派生媒体词典：从标题尾段提取媒体词形（"…--手机中研网"→
    # "手机中研网"、"…_中研普华"→"中研普华"）并入已知媒体，声明只写
    # 媒体名也可对上，减少对 _DOMAIN_MEDIA 静态表的依赖
    for t in titles:
        for seg in re.split(r"[-—_·|｜\s]+", t):
            seg = seg.strip()
            m3 = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,10})(?:网|新闻|财经|报)$", seg)
            if m3:
                media.add(m3.group(1))
    for _nm in _EXTRA_MEDIA_NAMES:
        media.add(_nm)
    return {"urls": urls, "domains": domains, "media": media, "titles": titles}


def _claim_fragment(c: str) -> bool:
    """非来源声明片段：纯分隔线/纯数字/百分比/引用编号/单字/括号失衡碎片。
    （实测把表格分隔线 "------"、费率 "0.60%"、编号 "[1]"、碎片 "称 X）" 误判为虚假标注）"""
    c = str(c or "").strip()
    if not c:
        return True
    if re.fullmatch(r"[-—–_=~·\s]+", c):
        return True
    if re.fullmatch(r"[\d\s.%％()（）\[\]【】\-—–]+", c):
        return True
    if re.fullmatch(r"\[\d+\]|【\d+】", c):
        return True
    if len(c) < 2:
        return True
    # 连接词/动词残留前缀（"称 -2.85亿）（+42.28%"、"于公开渠道"）
    if re.match(r"^[称于将已未并及]", c):
        return True
    # 负数开头的数值碎片（"-2.85亿"）
    if re.match(r"^[-—–]\d", c):
        return True
    # 书名号内容不是来源声明（实测：《王者荣耀》国际版和《DNF手游》海外发行）
    if "《" in c or "》" in c:
        return True
    # 纯大写短词（"PS"、"HK" 类碎片；≥4 字符如 "SNE" 常是真实机构缩写，
    # 仅 1-3 字符判碎片——真实来源名几乎不会只写 3 个大写字母且无其它内容）
    if re.fullmatch(r"[A-Z]{1,3}", c):
        return True
    # 地区/渠道词碎片（"中国大陆"、"海外" 等不是来源主体）
    if c in ("中国大陆", "海外", "全球", "国内", "国外", "亚太", "北美", "欧洲", "港台"):
        return True
    # Markdown 加粗标记（"**数据来源**"）不是来源主体
    if "**" in c:
        return True
    # 编号列表项碎片（"1. [高端酒分化复苏"、"1. 标题"）
    if re.match(r"^\d+[\.、)]", c):
        return True
    # 纯数值+单位（"134.7亿"、"37.08亿"）不是来源声明
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:亿|万|元|%|倍)?", c):
        return True
    # 斜杠短语（"机构/来源"、"此前同类任务的反思/自迭代"）：来源名几乎不含"/"，
    # 含 URL 的条目在提取层已单独放行，这里只拦纯文本斜杠碎片
    if "/" in c and "http" not in c:
        return True
    # 含括号的长描述句（"高毛利业务（视频号广告、小游戏）占比提升以及AI
    # 技术（混元大模型）…"）不是来源声明；短名称带单括号（"东方财富（A股）"）保留
    if c.count("（") + c.count("(") >= 1 and len(c) > 15:
        return True
    # Markdown 列表符开头的短语（实测"- 高端产品国窖1573系列持续放量"）：
    # 剥掉列表符后若不含媒体/机构词特征则判碎片；含（如"- 新浪财经"）保留
    _stripped = c
    _bullet = False
    while _stripped and _stripped[0] in "-*•·":
        _stripped = _stripped[1:].strip()
        _bullet = True
    if _bullet:
        _media_hint = (
            "网" in _stripped or "报" in _stripped or "证券" in _stripped
            or "财经" in _stripped or "银行" in _stripped or "基金" in _stripped
            or "研报" in _stripped or "数据" in _stripped or "统计" in _stripped
        )
        if not _media_hint:
            return True
    if c in ("来源", "年份", "链接", "口径", "单位", "数值", "指标",
             "时间", "地域", "样本", "说明", "序号", "备注", "状态",
             "限制", "综合费率", "附录", "口径/年份", "机构/来源",
             "来源链接", "来源编号", "口径说明", "以发布时间为准",
             "发布时间为准", "编号"):
        return True
    # 括号失衡（"称 X）" 之类被截断的碎片）：左括号数 != 右括号数
    if c.count("（") + c.count("(") != c.count("）") + c.count(")"):
        return True
    return False


def _extract_source_claims(report: str) -> list[str]:
    """提取报告中的来源声明（文本声明 + 表格来源单元格）。"""
    claims: list[str] = []
    for m in re.finditer(
        r"(?:数据来源|来源|引自|出自|来自|根据)\s*[：:]?\s*([^。；\n，,|]{2,60})",
        report,
    ):
        c = m.group(1).strip()
        if not c:
            continue
        # "来源为X"/"来源是X" 句式：剥掉引导字，避免把"公开财经报道"这类
        # 泛化诚实表述误抽成来源声明主体
        if c[:1] in ("为", "是") and len(c) > 1:
            c = c[1:].strip()
        # （数据来源：X）括号声明：捕获可能吞入尾部右括号（如"腾讯官方年报）"）
        if c.endswith(("）", ")")):
            c = c[:-1].strip()
        if not c:
            continue
        # BUG-6：分隔线/纯数字/百分比/引用编号/括号失衡碎片不是来源声明
        if _claim_fragment(c):
            continue
        # 参考来源清单条目（如 '1. [来源](https://example.com/a)'）：链接本身
        # 即来源引用，不属于"数据来源：X"声明，交给 source_list_completeness 检查
        if "](http" in c:
            rest = re.sub(
                r"\[[^\]]+\]\([^)]*\)", "", c
            ).strip(" .。；）)")
            if not rest or rest.isdigit():
                continue
        if c not in claims:
            claims.append(c)
    # 表格"来源"列单元格（非 URL 部分）：逐表解析——报告含多张表格时，
    # 每张表按自己的表头定位来源列（旧实现把首个分隔符后的所有行都当
    # 第一张表的数据行，会把后续表的"口径说明"单元格错配成来源声明）
    lines = report.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        header_cells = [
            c.strip() for c in lines[i].strip().strip("|").split("|")
        ]
        if i + 1 >= len(lines):
            i += 1
            continue
        sep_cells = [
            c.strip() for c in lines[i + 1].strip().strip("|").split("|")
        ]
        if not (sep_cells and all(
            not c or set(c) <= {"-", ":", "—"} for c in sep_cells
        )):
            i += 1
            continue
        # 来源列细化：'来源链接' 列是 URL（不属声明），'口径' 列是计算口径
        # （叙述性说明，不属来源）——都不作为来源声明抓取
        src_cols = [
            j for j, h in enumerate(header_cells)
            if "来源" in h and "链接" not in h and "口径" not in h
        ]
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = [
                c.strip() for c in lines[j].strip().strip("|").split("|")
            ]
            for ci in src_cols:
                if ci >= len(cells):
                    continue
                c2 = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[ci])
                if c2 and len(c2) >= 2 and "http" not in c2 and not _claim_fragment(c2) and c2 not in claims:
                    claims.append(c2)
            j += 1
        i = j
    return claims


# 括号披露注释：括号内是对来源构成的自愿披露/免责说明，
# 不参与主体匹配（可在 details/mislabeled 中保留原声明文本）
_PAREN_DISCLOSURE_RE = re.compile(r"（[^（）]*）|\([^()]*\)")

# 来源列里的状态/免责说明不是来源声明（"无直接关联/已剔除/待获取/不适用"）
_STATUS_MARKERS = (
    "无直接关联", "已剔除", "不适用", "待获取", "未获取", "暂缺",
    "未披露", "无此字段", "说明", "备注", "数据完整性", "口径说明",
    "直接披露", "待核实", "自行整理",
)
# 元叙述：报告解释来源构成/质量的句子（"来源 [1] 仅为报告目录页，
# 不含任何统计数值"、"其余来源均与主题无关或同源无额外数据"），
# 是诚实披露本身而非"数据来源：X"声明
_META_SOURCE_TALK = (
    "仅为", "只是", "目录页", "报告框架", "无具体数值",
    "均与主题无关", "同源无额外数据", "不含任何统计",
    "与主题无关",
)
# 表格来源列的短备注词（精确匹配，避免误伤含这些字的真实媒体名）
_TABLE_NOTE_WORDS = (
    "验证", "待核实", "直接披露", "自行整理", "数据缺失", "未披露",
)

# 否定/谨慎语境：命中即视为如实披露，不判虚假标注
_NEGATION_MARKERS = (
    "非财报类", "非官方", "非权威", "未经证实", "不构成", "不代表",
    "仅供参考", "网络传言", "市场传闻", "未经核实", "待核实",
    "并非", "非正式", "未经确认", "未能核实", "无法核实",
    "无法确认", "不保证", "疑似", "传闻", "据传", "网传", "小道消息",
)

# 泛化诚实表述：不指向可核验的具体来源，检索缺失不判虚假
_GENERIC_HONEST_MARKERS = (
    "公开渠道", "公开资料", "公开信息", "公开数据", "公开报道",
    "公开披露", "公开来源", "公开平台", "公开新闻",
    "网络公开", "互联网公开", "公司披露", "企业披露",
)

# 权威文档词：命中且无否定/谨慎语境、检索中无 → 虚假标注
_AUTHORITY_DOC_RE = re.compile(r"(年报|财报|公告|官网|投资者关系|招股书|报表|审计)")


def auto_repair_source_labels(report: str, mislabeled: list[str]) -> str:
    """把验收器判定的虚假来源标注确定性地降级为诚实披露。

    - 正文声明 `数据来源：X`（X 在 mislabeled 中且非结构词）→
      `数据来源：基于模型知识，未在本次检索中验证`；
    - 表格来源列中等于 mislabeled 项的单元格 → `模型知识`（表头感知，
      只替换分隔符行之后的数据行，不碰表头与其他列）。
    保守原则：只替换精确命中项，不做任何模糊改写；返回改写后的全文。
    """
    if not mislabeled:
        return report
    mis_set = {
        str(c).strip() for c in mislabeled
        if str(c).strip() and not _claim_fragment(str(c).strip())
    }
    if not mis_set:
        return report
    out = report
    # 1) 正文声明替换
    for c in sorted(mis_set, key=len, reverse=True):
        pat = re.compile(
            r"(数据来源|来源|引自|出自|来自|根据)\s*[：:]?\s*"
            + re.escape(c)
        )
        out = pat.sub(r"\1：基于模型知识，未在本次检索中验证", out)
    # 2) 表格来源列单元格替换（逐表解析：每张表按自己的表头定位来源列，
    #    与判定器一致；只替换分隔符行之后的数据行，不碰表头与其他列）
    lines = out.split("\n")
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        header_cells = [
            c.strip() for c in lines[i].strip().strip("|").split("|")
        ]
        if i + 1 >= len(lines):
            i += 1
            continue
        sep_cells = [
            c.strip() for c in lines[i + 1].strip().strip("|").split("|")
        ]
        if not (sep_cells and all(
            not c or set(c) <= {"-", ":", "—"} for c in sep_cells
        )):
            i += 1
            continue
        src_cols = [
            j for j, h in enumerate(header_cells)
            if "来源" in h and "链接" not in h and "口径" not in h
        ]
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = [
                c.strip() for c in lines[j].strip().strip("|").split("|")
            ]
            for ci in src_cols:
                if ci < len(cells) and cells[ci] in mis_set:
                    cells[ci] = "模型知识"
            lines[j] = "| " + " | ".join(cells) + " |"
            j += 1
        i = j
    return "\n".join(lines)


def check_source_labeling(report: str, sources: dict) -> dict:
    """来源标注诚实性：报告中"数据来源：X"的 X 是否真的在检索/快照中出现。
    - 含 URL / 命中已知媒体名 / 命中源标题 → 诚实
    - 括号披露注释（如"（含特定新闻门户单篇报道、非财报类资讯链接）"）
      是自愿披露，不参与主体匹配；纯披露无主体则跳过
    - 标注'模型知识/未验证' → 诚实（已明示）
    - 否定/谨慎语境（非财报类/非官方/网络传言/未经核实/仅供参考…）→ 诚实
    - 声明具体来源/权威文档（年报/公告/官网/招股书…）但源中无 → 虚假标注
    - '建议以X为准' 类建议句不判虚假。"""
    known = _known_sources(sources)
    claims = _extract_source_claims(report)
    mislabeled: list[str] = []
    checked = 0
    for c in claims:
        if any(k in c for k in ("建议以", "仅供参考", "说明", "清单", "名称", "序号")):
            continue
        if any(k in c for k in _STATUS_MARKERS):
            continue
        if any(k in c for k in _META_SOURCE_TALK):
            continue
        if "模型知识" in c or "未验证" in c or "未在本次检索" in c:
            continue
        # 表格来源列的短备注词（"验证/直接披露"等）：完整等于备注词
        # 的短声明跳过（用精确匹配而非包含匹配，避免误伤含这些字的
        # 真实媒体名，如"XX验证报告网"）
        if len(c.strip()) <= 6 and c.strip() in _TABLE_NOTE_WORDS:
            continue
        # 括号收紧：仅当剥去括号注释后为空（纯披露注释）才跳过；
        # 括号含媒体署名（"休闲食品专题（中研网）"）时用完整声明匹配
        if not _PAREN_DISCLOSURE_RE.sub("", c).strip():
            continue  # 纯披露注释，无声明主体
        checked += 1
        body = c  # 匹配主体 = 完整声明（含括号署名）
        if any(u in body for u in known["urls"]):
            continue
        if any(d and d in body for d in known["domains"]):
            continue
        if any(m and m in body for m in known["media"]):
            continue
        # 媒体别名归组：声明含组内别名 X，而已知媒体含同组别名 Y（X≠Y）→ 同源诚实
        if any(
            any(a in body for a in grp if a)
            and any(b in known["media"] for b in grp if b)
            for grp in _MEDIA_ALIAS_GROUPS
        ):
            continue
        if any(t and t in body for t in known["titles"] if len(t) >= 4):
            continue
        # BUG-6：声明主体是已知标题的子串（"东吴证券[3]" ⊂ 标题
        # "东吴证券宁德时代深度研究"）→ 同一来源，诚实；先剥离 [n] 引用标记
        seg = re.sub(r"[\[【]\d+[\]】]", "", body).strip(" .。；）)（")
        if len(seg) >= 2 and any(seg in t for t in known["titles"] if t):
            continue
        # 否定/谨慎语境：'非财报类/非官方/未经证实/网络传言/仅供参考' 等
        # 为如实披露，不判虚假标注（含括号披露注释中的否定词）
        if any(k in c for k in _NEGATION_MARKERS):
            continue
        # 标题派生媒体词典兜底：声明中的媒体词形（"中研网"）是已知检索
        # 标题的子串（"手机中研网"）→ 同一来源，诚实（静态域名映射
        # 缺失时靠标题自动对上，不依赖人工补录）
        if any(
            w and any(w in t for t in known["titles"] if t)
            for w in (m.group(0) for m in _MEDIA_WORD_RE.finditer(c))
        ):
            continue
        # 权威文档判定（否定感知）：仅当不含否定词且命中权威文档词，
        # 且检索中无对应文档 → 虚假标注
        if _AUTHORITY_DOC_RE.search(body):
            mislabeled.append(c)
            continue
        # 泛化诚实表述（公开渠道/公开资料等）：无可核验的具体主体，
        # 检索缺失不判虚假；"公开*"开头一律视为泛化表述
        # （覆盖"公开财经报道""公开市场数据"等未逐一登记的变体）
        if c.strip().startswith("公开"):
            continue
        if any(k in c for k in _GENERIC_HONEST_MARKERS):
            continue
        # 其他具体来源声明（如"东方财富数据中心"）：无括号披露、无否定词、
        # 无泛化诚实表述，且检索中无 → 虚假标注
        mislabeled.append(c)
    passed = len(mislabeled) == 0
    # A2：虚假标注声明若含「XX网/XX报/XX财经/XX新闻」媒体词，
    # 追加补录建议（供人工确认 _DOMAIN_MEDIA，不自动改表）
    suggestions: list[str] = []
    for c in mislabeled:
        for s in suggest_domain_media(c):
            if s not in suggestions:
                suggestions.append(s)
    details = (
        f"来源声明检查 {checked} 条，虚假标注 {len(mislabeled)} 条"
        + (f"（{'、'.join(mislabeled[:5])}）" if mislabeled else "，全部可溯源或已明示")
    )
    if suggestions:
        details += "；" + "；".join(
            f"建议补录域名媒体映射：{s}" for s in suggestions[:5]
        )
    return {
        "pass": passed,
        "details": details,
        "checked_count": checked,
        "mislabeled_count": len(mislabeled),
        "mislabeled": mislabeled[:10],
        "suggestions": suggestions[:10],
    }


# ─────────────────────────────────────────────
# 交付物完整性检查（Bug2：验收无数字=pass 空转 → 空壳报告也判 FAIL）
# ─────────────────────────────────────────────

# 数据缺失占位标记：报告出现这些字样说明关键数据未披露
_PLACEHOLDER_MARKERS = (
    "未披露", "未获取", "待补充", "数据缺失", "待获取", "暂缺", "暂无", "无数据",
)

# 列表/排名类交付要求关键词（命中后必须提供数据表格）
_LIST_REQUIREMENT_KEYWORDS = (
    "列表", "前十", "排名", "排行", "明细", "清单", "top", "表格", "榜单",
)

# 数字型交付要求关键词（用于 details 标注，辅助人工判断）
_NUMERIC_REQUIREMENT_KEYWORDS = (
    "成交量", "成交额", "金额", "市值", "营收", "净利润", "数据", "数字",
)


def check_deliverable_completeness(
    report: str, goal: str, domain: str | None = None,
) -> dict:
    """交付物完整性检查：从 goal 提取关键交付要求（列表/前十/排名/表格类
    与数字要求），检查报告是否被数据缺失占位标记填满、是否缺少必需表格。

    规则：
    - financial 域报告含未披露/未获取/待补充/数据缺失等占位标记 ≥3 处 → FAIL；
      非金融域（行业调研/新闻/宏观/加密货币等）豁免该惩罚——诚实披露
      "数据缺失"本身就是交付质量，数字溯源仍由 number_traceability 把关，
      避免"调研任务必然 SUCCESS_WITH_ISSUES"的结构性双重惩罚；
    - goal 要求列表/前十/排名且报告无任何表格，或表格内容全为占位 → FAIL；
    - 正常完整报告（含真实数据表格/无列表要求）→ pass。
    """
    r = str(report or "")
    g = str(goal or "").lower()
    domain = domain or traceability_domain(goal)
    placeholder_count = sum(r.count(m) for m in _PLACEHOLDER_MARKERS)
    list_required = any(k in g for k in _LIST_REQUIREMENT_KEYWORDS)
    numeric_required = any(k in g for k in _NUMERIC_REQUIREMENT_KEYWORDS)

    table_lines = [
        ln for ln in r.splitlines()
        if ln.strip().startswith("|") and ln.count("|") >= 2
    ]
    has_table = bool(table_lines)
    table_all_placeholder = False
    if has_table:
        # 定位 Markdown 分隔行，其前一行视为表头（不参与占位判定）
        sep_idx = -1
        for i, ln in enumerate(table_lines):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if all(not c or set(c) <= {"-", ":", "—"} for c in cells):
                sep_idx = i
                break
        data_rows: list[list[str]] = []
        for i, ln in enumerate(table_lines):
            if i in (sep_idx, sep_idx - 1) and sep_idx > 0:
                continue
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            # 跳过 Markdown 表头/分隔行（---/:--: 等）
            if all(not c or set(c) <= {"-", ":", "—"} for c in cells):
                continue
            data_rows.append(cells)
        filled = [c for row in data_rows for c in row if c]
        if filled:
            table_all_placeholder = all(
                any(m in c for m in _PLACEHOLDER_MARKERS) for c in filled
            )

    if placeholder_count >= 3 and domain == "financial":
        passed = False
        detail = (
            f"交付物不完整：报告含 {placeholder_count} 处数据缺失占位"
            "（未披露/未获取/待补充/数据缺失）"
        )
    elif list_required and (not has_table or table_all_placeholder):
        passed = False
        detail = (
            "交付物不完整：目标要求列表/前十/排名，但报告"
            + ("没有可用的数据表格" if not has_table else "表格内容全部为占位/未披露")
        )
    else:
        passed = True
        if placeholder_count >= 3:
            # 非金融域豁免：诚实披露数据缺失不双重惩罚（数字溯源仍从严）
            detail = (
                f"交付物完整性：报告含 {placeholder_count} 处数据缺失占位，"
                f"非金融域（{domain}）豁免占位惩罚——诚实披露视为交付质量，"
                "数字要求仍由 number_traceability 把关"
            )
        else:
            detail = (
                f"交付物完整性：占位标记 {placeholder_count} 处"
                + ("，含数据表格" if has_table else "，无表格要求或表格非必需")
                + ("，目标要求列表/前十" if list_required else "")
                + ("，目标含数字要求" if numeric_required else "")
            )
    return {
        "pass": passed,
        "details": detail,
        "placeholder_count": placeholder_count,
        "has_table": has_table,
        "table_all_placeholder": table_all_placeholder,
        "list_required": list_required,
        "numeric_required": numeric_required,
    }


# ─────────────────────────────────────────────
# V1.2 竞品启示：三级溯源链 / 数据时效 / 免责声明
# ─────────────────────────────────────────────

# 文末来源清单小节标题（兼容既有"数据来源"附录与 V1.2 强制"参考来源"）
_SOURCE_LIST_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:参考来源|来源清单|参考资料|参考文献|数据来源|来源附录)\s*$",
    re.M,
)
# 正文上标引用 [1] [2]（排除 Markdown 链接 [n](url) 与图片语法）
_INLINE_REF_RE = re.compile(r"\[(\d{1,3})\](?!\()")


def _body_without_source_list(report: str) -> str:
    """去掉文末来源清单小节后的正文（引用编号只统计正文）。"""
    t = str(report or "")
    m = _SOURCE_LIST_HEADING_RE.search(t)
    return t[:m.start()] if m else t


def extract_source_list(report: str) -> list[dict]:
    """提取文末来源清单条目：[{num, title, url}]。

    支持编号列表（1. / 1、 / 1)）、Markdown 链接 [标题](URL)、表格行与裸 URL；
    找不到来源清单小节返回空列表。"""
    t = str(report or "")
    m = _SOURCE_LIST_HEADING_RE.search(t)
    if not m:
        return []
    entries: list[dict] = []
    for line in t[m.end():].splitlines():
        if re.match(r"^#{1,6}\s+", line):
            break  # 来源清单结束（遇到新的小节）
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-", ":", "—", "|", " "}:
            continue
        num = None
        body = stripped
        nm = re.match(r"^(\d{1,3})[.、)．]\s*(.*)$", stripped)
        if nm:
            num = int(nm.group(1))
            body = nm.group(2).strip()
        if not body:
            continue
        # Markdown 链接 [标题](URL)（任意 scheme，URL 格式检查兜底）
        lm = re.match(r"^\[([^\]]*)\]\(([^)\s]+)\)$", body)
        if lm:
            entries.append({
                "num": num,
                "title": lm.group(1).strip(),
                "url": lm.group(2),
            })
            continue
        # 表格行（任意单元格含 http(s) URL）
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        url = next((c for c in cells if re.match(r"^https?://", c)), None)
        if url and len(cells) >= 2:
            title = next((c for c in cells if c and c != url), "") or ""
            entries.append({"num": num, "title": title, "url": url})
            continue
        # 裸 URL（任意 scheme，非 http(s) 由格式检查兜底）
        um = re.search(r"[a-z][a-z0-9+.-]*://[^\s)\]\"']+", body)
        if um:
            raw = um.group(0).rstrip(".,;:，。；：")
            title = body.replace(um.group(0), "").strip(" -–—()（）")
            entries.append({"num": num, "title": title, "url": raw})
            continue
        # 无 URL 的编号条目（标题条目，URL 格式检查会兜底）
        entries.append({"num": num, "title": body, "url": None})
    return entries


def check_source_list_completeness(report: str) -> dict:
    """三级溯源链完整性：正文 [n] 编号引用 ↔ 文末来源清单。

    - 正文每个 [n] 都必须在清单中有对应条目；
    - 清单条目数 ≥ 正文最大引用编号；
    - 清单内 URL 必须为 http(s) 格式。
    报告既无 [n] 引用也无来源清单时跳过（不误伤纯统计任务）。"""
    t = str(report or "")
    refs = {
        int(m.group(1))
        for m in _INLINE_REF_RE.finditer(_body_without_source_list(t))
    }
    entries = extract_source_list(t)
    if not refs and not entries:
        return {
            "pass": True,
            "details": "报告无 [n] 引用与来源清单，跳过三级溯源链检查",
            "refs": [], "missing_refs": [], "list_entries": [],
            "gaps": [], "enabled": False,
        }
    list_nums = {e["num"] for e in entries if e["num"] is not None}
    missing_refs = sorted(r for r in refs if r not in list_nums)
    max_ref = max(refs) if refs else 0
    gaps: list[str] = []
    for r in missing_refs:
        gaps.append(f"正文引用 [n={r}] 在文末来源清单中无对应条目")
    if max_ref and not entries:
        gaps.append("正文含 [n] 引用但文末缺少 '## 参考来源' 清单")
    elif max_ref and len(entries) < max_ref:
        gaps.append(
            f"来源清单条目数 {len(entries)} 小于正文最大引用编号 {max_ref}"
        )
    bad_urls = [
        e for e in entries
        if e["url"] and not re.match(r"^https?://", e["url"])
    ]
    for e in bad_urls:
        gaps.append(f"来源条目 URL 非 http(s) 格式：{e['title'] or e['url']}")
    passed = not gaps
    details = (
        f"三级溯源链：正文引用 {len(refs)} 个编号，来源清单 {len(entries)} 条"
        + (f"；缺失引用条目 {len(missing_refs)} 个" if missing_refs else "")
        + (
            "；清单条目不足"
            if max_ref and entries and len(entries) < max_ref
            else ""
        )
        + (f"；非法 URL {len(bad_urls)} 条" if bad_urls else "")
        + ("，完整" if passed else "")
    )
    return {
        "pass": passed,
        "details": details,
        "refs": sorted(refs),
        "missing_refs": missing_refs,
        "list_entries": [
            {"num": e["num"], "title": e["title"], "url": e["url"]}
            for e in entries
        ],
        "gaps": gaps,
        "enabled": True,
    }


# 数据时效区块特征词：命中即要求"数据时效/数据截止 + 具体时间/日期"
_FRESHNESS_BLOCK_MARKERS = ("数据时效", "数据截止", "数据截至", "更新时间", "行情快照")
# 时效语义特征词：任务/报告完全不涉及这些内容时自动跳过（纯代码任务等）
_TIME_SENSITIVE_MARKERS = (
    "行情", "价格", "股价", "涨跌幅", "成交", "财报", "年报", "营收", "净利润",
    "业绩", "市值", "数据", "新闻", "快照", "排行", "榜单", "截至", "时效",
    "日终", "盘中", "盘后", "收盘", "更新",
)
_FRESHNESS_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}")


def check_freshness_block(report: str, goal: str = "") -> dict:
    """数据时效区块检查：报告须含'数据时效/数据截止'字样 + 具体时间/日期。

    无时效语义的任务（如纯代码任务，报告与目标都不含行情/财报/数据等词）
    自动跳过，不误伤。"""
    t = str(report or "")
    hay = t + "\n" + str(goal or "")
    if not any(m in hay for m in _TIME_SENSITIVE_MARKERS):
        return {
            "pass": True,
            "details": "任务无数据时效语义，跳过",
            "has_freshness_block": False,
            "has_date": False,
            "gaps": [],
            "enabled": False,
        }
    has_block = any(m in t for m in _FRESHNESS_BLOCK_MARKERS)
    has_date = bool(_FRESHNESS_DATE_RE.search(t))
    passed = has_block and has_date
    gaps: list[str] = []
    if not has_block:
        gaps.append("报告缺少'数据时效'区块（需含数据截止时间/行情快照时间）")
    elif not has_date:
        gaps.append("报告含'数据时效'字样但无具体时间/日期（如 2026-08-30）")
    return {
        "pass": passed,
        "details": (
            f"数据时效区块：{'包含' if has_block else '缺失'}，"
            f"具体时间/日期：{'包含' if has_date else '缺失'}"
        ),
        "has_freshness_block": has_block,
        "has_date": has_date,
        "gaps": gaps,
        "enabled": True,
    }


def check_disclaimer(report: str) -> dict:
    """免责声明 + AI 标识检查：financial 域报告必须含'免责声明'与'不构成投资建议'。"""
    t = str(report or "")
    has_disclaimer = "免责声明" in t
    # 兼容官方模板"不构成任何投资建议"与简写"不构成投资建议"
    has_advice_note = re.search(r"不构成(?:任何)?投资建议", t) is not None
    passed = has_disclaimer and has_advice_note
    return {
        "pass": passed,
        "details": (
            f"免责声明/AI 标识：{'包含' if has_disclaimer else '缺失'}，"
            f"'不构成投资建议'：{'包含' if has_advice_note else '缺失'}"
            + ("，通过" if passed else "，缺失")
        ),
        "has_disclaimer": has_disclaimer,
        "has_advice_note": has_advice_note,
        "gaps": (
            []
            if passed
            else ["报告缺少'免责声明'与'不构成投资建议'（AI 生成标识）"]
        ),
        "enabled": True,
    }


# ─────────────────────────────────────────────
# Checklist runner
# ─────────────────────────────────────────────

# 报告格式类检查按任务类型分档：代码/数据类任务没有"来源清单/数据时效/免责声明"
# 要求，此前一律展示为失败项，读起来像交付缺陷（实测"写个脚本"的验收报告里
# source_list_completeness=false）。分档后这类检查标记"N/A"并给出原因。
_PROFILE_REPORT_CHECKS: dict[str, tuple[str, ...]] = {
    "financial": ("source_list_completeness", "freshness_block", "disclaimer"),
    "research": ("source_list_completeness", "freshness_block", "disclaimer"),
    "news": ("source_list_completeness", "freshness_block"),
    "code": (),
    "data": (),
}

# 代码/数据类任务的判定线索（目标文本层）
_CODE_GOAL_HINTS = (
    "python", "脚本", "代码", "单文件", ".py", ".html", "html 页面", "命令行程序",
    "可直接运行", "自包含", "打印", "ascii 柱状图", "小程序",
)
_DATA_GOAL_HINTS = (
    "csv", "数据集", "数据表", "eda", "训练模型", "回归", "预测模型", "特征工程",
    "数据清洗", "建模",
)


def resolve_profile(goal: str, capabilities=None) -> str:
    """判定验收档位：financial / research / news / code / data。

    优先用步骤能力（可精确区分"写代码"与"写报告"），其次用目标线索，
    最后回落到溯源域（financial/crypto/macro → financial，news → news，其余 research）。
    """
    caps = {str(c) for c in (capabilities or []) if str(c).strip()}
    if caps:
        if caps <= {"code_execution", "file_io", "package"}:
            return "code"
        if caps <= {"data_loader", "data_analyzer", "model_trainer", "package", "file_io"}:
            return "data"
    g = str(goal or "").lower()
    domain = traceability_domain(goal)
    if any(h in g for h in _CODE_GOAL_HINTS) and not any(
        k in g for k in _FINANCIAL_MARKERS
    ):
        return "code"
    if any(h in g for h in _DATA_GOAL_HINTS):
        return "data"
    if domain in ("financial", "crypto", "macro"):
        return "financial"
    if domain == "news":
        return "news"
    return "research"


def run_acceptance(task_id: str, goal: str, report_text: str, workspace,
                   capabilities=None, profile: str | None = None) -> dict:
    """运行验收 checklist，输出缺口报告。

    profile 决定"报告格式类检查"是否适用（代码/数据类任务不适用，标记 N/A 并说明
    原因，不再是"失败项"）；是否计入 overall 缺口沿用既有语义（仅 financial 域计数），
    避免本包顺带改变既有判定。
    """
    sources = _collect_sources(workspace)
    # V1：用户材料（任务目标/指令里给出的数字）本身就是来源通道。注入为独立来源，
    # 数字命中它时记为 user_material（真实性未核实），而不是"不可溯源"或"模型知识"。
    # 注意：这不降低任何阈值，也不把未知来源改判为已知来源。
    _user_material = str(goal or "")
    if _user_material.strip():
        sources = dict(sources)
        sources["user_material"] = _user_material
    domain = traceability_domain(goal)
    profile = profile or resolve_profile(goal, capabilities)
    report_checks = set(_PROFILE_REPORT_CHECKS.get(profile, ()))
    checks: dict = {}
    checks["number_traceability"] = check_number_traceability(
        report_text, sources, domain=domain, goal=goal,
    )
    checks["entity_attribution"] = check_entity_attribution(
        report_text, sources, goal,
    )
    checks["source_labeling"] = check_source_labeling(report_text, sources)
    checks["deliverable_completeness"] = check_deliverable_completeness(
        report_text, goal, domain=domain,
    )
    # V1.2 竞品启示：三级溯源链 / 数据时效 / 免责声明。
    # applicable：按 profile 判断是否适用（不适用 → N/A，计 pass 但保留原始结果）；
    # counted：是否计入 overall 缺口（financial 域计入，非金融域只展示）。
    checks["source_list_completeness"] = check_source_list_completeness(report_text)
    checks["freshness_block"] = check_freshness_block(report_text, goal)
    checks["disclaimer"] = check_disclaimer(report_text)
    _V12_REPORT_CHECKS = (
        "source_list_completeness", "freshness_block", "disclaimer",
    )
    # 代码/数据类任务：报告是"交付说明"，其中数字多为程序输出（内置/示例数据），
    # 不存在"可溯源来源"的要求。仅当目标本身要求外部数据（行情/财务口径）时
    # 才继续按溯源规则判定；否则记 N/A —— 否则"脚本跑出的样本汇总"会被判
    # "数字溯源率 0%"（实测销售脚本任务即如此）。
    if profile in ("code", "data"):
        try:
            from task_intent import market_intent
            requires_sourced = market_intent(goal)["needs_market_data"] or any(
                k in str(goal or "").lower() for k in _FINANCIAL_MARKERS
            )
        except Exception:
            requires_sourced = False
        if not requires_sourced:
            _nt = checks["number_traceability"]
            _nt["applicable"] = False
            _nt["counted"] = False
            _nt["raw_pass"] = _nt.get("pass")
            _nt["pass"] = True
            _nt["details"] = (
                f"不适用于 {profile} 类任务（报告数字来自程序输出/示例数据，"
                "无外部来源可溯源要求）"
            )
    checks["number_traceability"].setdefault("applicable", True)
    # R0.1：按**目标里写明的要求**判定（需要数据？需要来源？点名了哪些指标/期间？），
    # 与"诚信披露"分开输出；代码/数据类任务在目标未要求外部数据时不适用。
    _reqs = derive_requirements(goal, capabilities)
    if profile in ("code", "data") and not (
        checks["number_traceability"].get("applicable", True)
        and not checks["number_traceability"].get("counted") is False
    ):
        _reqs = dict(_reqs, needs_data=False, needs_sources=False)
    checks["requirement_coverage"] = check_requirement_coverage(
        goal, report_text, _reqs,
        checks.get("number_traceability") or {},
        sources, checks.get("source_list_completeness") or {},
    )
    # 分析完整性（研究任务）：正文有没有把已选事实讲全。**只提示**——`counted=False`
    # 使其不参与下面的 gaps 汇总与 overall；缺口在 checks 里可见、可诊断。
    checks["analysis_completeness"] = check_analysis_completeness(
        report_text, goal, task_id,
    )
    gaps = []
    _req_gap_msgs: list[str] = []
    for _key, _c in checks.items():
        if _key in _V12_REPORT_CHECKS:
            applicable = _key in report_checks
            _c["applicable"] = applicable
            _c["counted"] = applicable and domain == "financial"
            if not applicable:
                _c["raw_pass"] = _c.get("pass")
                _c["pass"] = True
                _c["details"] = (
                    f"不适用于 {profile} 类任务（无报告来源/时效/免责声明要求）"
                )
        if _c["pass"] or not _c.get("counted", True):
            continue
        # 优先给出检查项自己的**可操作缺口**（如"目标要求给出营业收入…"），
        # 没有细分缺口时退回 details 摘要
        _msgs = [str(m) for m in (_c.get("gaps") or [])] or [str(_c.get("details") or "")]
        gaps.extend(_msgs)
        if _key == "requirement_coverage":
            _req_gap_msgs.extend(_msgs)
    # R0.1：目标达成作为**独立门槛**（不再只按 financial/research 标签选严宽）。
    # 目标要数据/来源而报告都没给 → 不得整体 pass；缺失但已诚实披露 → partial。
    _req = checks.get("requirement_coverage") or {}
    _other_gaps = [g for g in gaps if g not in _req_gap_msgs]
    if gaps:
        overall = "fail"
    else:
        overall = "pass"
    if not _req.get("pass") and not _other_gaps:
        # 只由"目标未达成"造成的非通过：按四态如实给 partial/unknown，
        # 其余检查已经失败时保持 fail（不软化既有判定）
        _st = str(_req.get("status") or "")
        if _st in ("partial", "unknown"):
            overall = _st
    # A2：汇总各检查项的域名媒体补录建议，供 GET /api/acceptance/suggestions 读取
    suggestions: list[str] = []
    for c in checks.values():
        for s in (c.get("suggestions") or []):
            if s not in suggestions:
                suggestions.append(s)
    return {
        "report_id": str(task_id),
        "goal": str(goal or "")[:120],
        "checks": checks,
        "overall": overall,
        "profile": profile,
        "gaps": gaps,
        "suggestions": suggestions,
        # 规则版本化与报告指纹：供 acceptance_report.json / 事件流对账
        "rules_version": ACCEPTANCE_RULES_VERSION,
        "rules_fingerprint": rules_fingerprint(),
        # R0.2：`report_sha256` 给**全量**（身份可证明，用于精确绑定版本）；
        # 短 hash 单独放在 `report_sha256_short`，只作显示/对账，不参与身份判断。
        "report_sha256": sha256(str(report_text or "").encode("utf-8")).hexdigest(),
        "report_sha256_short": sha256(
            str(report_text or "").encode("utf-8")).hexdigest()[:16],
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
