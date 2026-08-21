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
import re
from pathlib import Path


# ─────────────────────────────────────────────
# 数字提取
# ─────────────────────────────────────────────

_NUM_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(万亿|千亿|百亿|亿|万)?\s*"
    r"(美元|港元|元|人民币|%|％)?"
)

_DISCLOSED_MARKERS = ("基于模型知识", "未在本次检索中验证", "未验证", "模型估算", "模型知识")


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


def _collect_sources(workspace) -> dict[str, str]:
    """收集可溯源数据源文本：search_results / fetch_snapshot / clean_chart_data。"""
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
    for key in ("market_data", "market_share", "macro_indicators", "market_trends"):
        for r in data.get(key) or []:
            if not isinstance(r, dict):
                continue
            try:
                rv = float(r.get("value"))
            except (TypeError, ValueError):
                continue
            ru = str(r.get("unit") or "")
            if abs(rv - v) < max(0.5, abs(v) * 0.005) and (
                not unit or not ru or unit == ru or unit in ru or ru in unit
            ):
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
    for r in md:
        if not isinstance(r, dict):
            continue
        try:
            rv = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if rv and abs(rv - v) / rv <= 0.02:
            return True
    return False


def check_number_traceability(report: str, sources: dict, threshold: float = 0.7) -> dict:
    """数字溯源校验：报告中的财务数字能否在检索/快照/清洗数据中找到。
    返回 {pass, details, total_count, traceable_count, unverifiable_count, ...}。"""
    nums = extract_financial_numbers(report)
    total = len(nums)
    if total == 0:
        return {
            "pass": True,
            "details": "报告未检出需要溯源的财务数字",
            "total_count": 0, "traceable_count": 0, "unverifiable_count": 0,
            "traceable": [],
            "untraceable": [],
        }
    src_norm = {k: _norm(v) for k, v in sources.items() if v}
    clean_text = sources.get("clean_chart_data") or ""
    traceable: list[dict] = []
    untraceable: list[dict] = []
    disclosed: list[dict] = []
    for n in nums:
        hit = None
        if clean_text and _traceable_in_clean(n, clean_text):
            hit = "clean_chart_data"
        elif clean_text and _derived_traceable(n, clean_text):
            hit = "derived_from_clean"
        else:
            if n["unit"]:
                for k, st in src_norm.items():
                    if any(c and c in st for c in _candidates(n)):
                        hit = k
                        break
            else:
                for k, st in src_norm.items():
                    if _bare_match(n["value"], st):
                        hit = k
                        break
        item = {"raw": n["raw"], "value": n["value"], "unit": n["unit"]}
        if hit:
            item["source"] = hit
            item["derived"] = hit == "derived_from_clean"
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
    disclosed_rate = len(disclosed) / total
    # 细分：财务金额（亿/万/元/美元单位）与 其他数字（%等）分开统计
    amounts = [n for n in nums if any(u in n["unit"] for u in ("亿", "万", "元", "美元"))]
    traceable_keys = {(t.get("value"), t.get("unit")) for t in traceable}
    amount_ok = sum(1 for n in amounts if (n["value"], n["unit"]) in traceable_keys)
    amount_rate = (amount_ok / len(amounts)) if amounts else 1.0
    passed = rate >= threshold or total < 3
    details = (
        f"数字溯源率 {rate:.0%}（{len(traceable)}/{total}）"
        + f"；财务金额溯源率 {amount_rate:.0%}（{amount_ok}/{len(amounts)}）"
        + (f"；已披露（模型知识标注）{disclosed_rate:.0%}（{len(disclosed)}）" if disclosed else "")
        + ("" if passed else f"，低于阈值 {threshold:.0%}")
        + (f"；不可溯源示例：{'、'.join(u['raw'][:20] for u in untraceable[:5])}" if untraceable else "")
    )
    return {
        "pass": passed,
        "details": details,
        "total_count": total,
        "traceable_count": len(traceable),
        "unverifiable_count": len(untraceable),
        "amount_rate": round(amount_rate, 3),
        "amount_traceable": amount_ok,
        "amount_total": len(amounts),
        "disclosed_count": len(disclosed),
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
    return src in ("clean_chart_data", "derived_from_clean",
                   "structured", "structured_financials")


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
    trace = check_number_traceability(report, sources)
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
)


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
    return {"urls": urls, "domains": domains, "media": media, "titles": titles}


def _extract_source_claims(report: str) -> list[str]:
    """提取报告中的来源声明（文本声明 + 表格来源单元格）。"""
    claims: list[str] = []
    for m in re.finditer(
        r"(?:数据来源|来源|引自|出自|来自|根据)\s*[：:]?\s*([^。；\n，,|]{2,60})",
        report,
    ):
        c = m.group(1).strip()
        if c and c not in claims:
            claims.append(c)
    # 表格"来源"列单元格（非 URL 部分）
    for line in report.splitlines():
        if line.strip().startswith("|") and "来源" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            for c in cells:
                c2 = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", c)
                if c2 and len(c2) >= 2 and "http" not in c2 and c2 not in claims:
                    claims.append(c2)
    return claims


def check_source_labeling(report: str, sources: dict) -> dict:
    """来源标注诚实性：报告中"数据来源：X"的 X 是否真的在检索/快照中出现。
    - 含 URL / 命中已知媒体名 / 命中源标题 → 诚实
    - 标注'模型知识/未验证' → 诚实（已明示）
    - 声明具体权威文档（年报/公告/官网/招股书）但源中无 → 虚假标注
    - '建议以X为准' 类建议句不判虚假。"""
    known = _known_sources(sources)
    claims = _extract_source_claims(report)
    mislabeled: list[str] = []
    checked = 0
    # 来源列里的状态/免责说明不是来源声明（"无直接关联/已剔除/待获取/不适用"）
    _STATUS_MARKERS = (
        "无直接关联", "已剔除", "不适用", "待获取", "未获取", "暂缺",
        "未披露", "无此字段", "说明", "备注", "数据完整性", "口径说明",
    )
    for c in claims:
        if any(k in c for k in ("建议以", "仅供参考", "说明", "清单", "名称", "序号")):
            continue
        if any(k in c for k in _STATUS_MARKERS):
            continue
        if "模型知识" in c or "未验证" in c or "未在本次检索" in c:
            continue
        checked += 1
        if any(u in c for u in known["urls"]):
            continue
        if any(d and d in c for d in known["domains"]):
            continue
        if any(m and m in c for m in known["media"]):
            continue
        # 媒体别名归组：声明含组内别名 X，而已知媒体含同组别名 Y（X≠Y）→ 同源诚实
        if any(
            any(a in c for a in grp if a)
            and any(b in known["media"] for b in grp if b)
            for grp in _MEDIA_ALIAS_GROUPS
        ):
            continue
        if any(t and t in c for t in known["titles"] if len(t) >= 4):
            continue
        if re.search(r"(年报|财报|公告|官网|投资者关系|招股书|报表|审计)", c):
            mislabeled.append(c)
    passed = len(mislabeled) == 0
    details = (
        f"来源声明检查 {checked} 条，虚假标注 {len(mislabeled)} 条"
        + (f"（{'、'.join(mislabeled[:5])}）" if mislabeled else "，全部可溯源或已明示")
    )
    return {
        "pass": passed,
        "details": details,
        "checked_count": checked,
        "mislabeled_count": len(mislabeled),
        "mislabeled": mislabeled[:10],
    }


# ─────────────────────────────────────────────
# Checklist runner
# ─────────────────────────────────────────────

def run_acceptance(task_id: str, goal: str, report_text: str, workspace) -> dict:
    """运行验收 checklist，输出缺口报告。"""
    sources = _collect_sources(workspace)
    checks: dict = {}
    checks["number_traceability"] = check_number_traceability(report_text, sources)
    checks["entity_attribution"] = check_entity_attribution(
        report_text, sources, goal,
    )
    checks["source_labeling"] = check_source_labeling(report_text, sources)
    gaps = [c["details"] for c in checks.values() if not c["pass"]]
    overall = "pass" if not gaps else "fail"
    return {
        "report_id": str(task_id),
        "goal": str(goal or "")[:120],
        "checks": checks,
        "overall": overall,
        "gaps": gaps,
    }
