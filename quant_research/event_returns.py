# -*- coding: utf-8 -*-
"""`event_returns_v1`：事件窗口的**基准调整收益读数**（观察值，不是因果结论、不是选股能力）。

为什么单独一族算子（而不是塞进 `financial_analysis`）：输入的**身份契约不同**。财报算子的
`Observation` 带币种/量纲/归属层/期间身份；行情载荷只有逐日价格 + `available_at` + 复权口径。
把行情伪装成 `Observation` 会让"同一性校验"（主体/币种/报表范围）在无意义的字段上通过，
等于把校验做成摆设。这里用**自己的输入契约**：`input_kind = market_history@<dataset_id>`。

## 纪律（每条都对应一个可被检查的字段，不是注释里的口号）

0. **信号信息与事后结果分开**（架构规划 §12.4，本条是上一轮写错、本轮改清的）：
   旧措辞"事件日之后才可用的行不得参与该事件的收益窗口"会把 `t+1/+5/+20` 的**评价结果全部排除**——
   那是把"防未来函数"用成了"不许评价历史"。正确的三段是：

   - `feature_cutoff`：**形成判断/信号**的截点，只允许当时已公开可用的信息；后来的更正/结果/新闻
     不得倒灌成"当时就知道"。锚点（价格起点）就取在这个截点上。
   - `outcome_window`：判断**之后**的实际行情，用来评价历史结果；窗口价格只要**评价时已可得**就能参与，
     仍不得回流成当时特征。
   - `evaluation_as_of`：**截至何时评价**。未满 20 个交易日 ⇒ 该点 `pending`；已成熟但数据缺失 ⇒
     `missing`；两者**都不是 0**，也都不伪装成完整收益。

   另外：日精度披露**不臆造盘中时间**，首例采用**保守的下一交易日规则**（`t0` ＝披露日之后第一个
   交易日）并写清价格起点；**事件反应统计**与**可执行交易收益**分别标识（本算子的收盘对收盘读数
   **不可执行**，见 `kind` 字段）。

1. **防未来函数**：切片只取 `date > 事件日` 的行（复用 `market_history.window` 的唯一实现）；
   读数记录 `available_at` 上界，事后可核"这个读数在当时拿得到"。
2. **口径必须写明**：`adj_basis` 缺失 ⇒ 直接 `unavailable`。标的为**不复权**时，窗口内若含
   除权除息，价格跌幅含分红除权成分，**默认拒绝**给头条超额收益读数（`allow_unadjusted=True`
   才给，并降级为 `caveat`）。
3. **基准同窗口按**日期**对齐**，不按"第 N 个交易日"对齐：基准缺某日 ⇒ 该点超额收益留空并列出，
   **不补 0、不前值填充**。
4. **重叠事件不去重就不聚合**：窗口重叠的事件各自出读数，但**不参与均值/中位数**——重叠窗口
   会重复计同一段价格路径。被排除的事件逐条记原因。
5. **样本不足不给统计量**：保留事件 < 2 个时不产均值；< 3 个时不产"胜率"之类比例的暗示性字段。
6. **许可随读数走**：免费源/个人非商业许可标 `delivery_eligible: False`，不得进对外下载包。
7. **可复算**：读数带 `input_fingerprint`（输入绑定指纹）与 `reading_hash`；`replay()` 用同一
   输入重算并比对，不一致就报差异字段，**不重写旧读数**。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

OPERATOR = "event_returns_v1"
IMPL_VERSION = "event_returns/1.0.0"
SCHEMA = "weavemind.event_returns/0"
INPUT_KIND = "market_history"

# 读侧许可常量拉不起来时的退回值（**只用于判定"不可对外"**，不在正常路径上生效）
INPUT_LICENSE_FREE_TRIAL = "免费源·仅内部试验"
INPUT_LICENSE_PERSONAL = "个人非商业"

DEFAULT_OFFSETS = (0, 1, 5, 20)
ANCHOR_OFFSET = -1

LIMITS_BASE = (
    "这是**事件窗口的价格观察值**，不是因果结论：没有识别策略、没有对照组、样本极小",
    "未做风险调整（无 β/波动率/因子暴露），「超额」仅指减去同期基准，不等于 alpha",
    "基准调整按**日期对齐**；基准缺该交易日则留空，不补 0、不前值填充",
    "`t0` ＝披露日之后第一个交易日（**保守下一交易日规则**：披露只有日精度，不臆造盘中时间）；"
    "价格起点是 `anchor`（`feature_cutoff` 当日或之前最后一个交易日）的收盘价",
    "**收盘对收盘读数不可执行**（`kind=event_reaction_observation`）：披露时点未知，若公告在盘前/盘中"
    "发布，披露日那一档可能已含部分反应 ⇒ 本读数应看作反应幅度的**下界**；可执行收益需盘中时间与"
    "开盘价，本轮不实现，也不把两者混成一个数",
    "窗口未满记 `pending`、已成熟但缺行情记 `missing`：两者**都不报 0**、都不伪装成完整收益",
    "`offsets` 按**交易日历**定位（给了日历才是真实第 N 个交易日）；**没有日历**时退回"
    "标的自身交易日序列，**停牌会让窗口静默错位**，此时只用基准代理报**疑似**",
    "窗口重叠的事件各自出读数但不参与聚合（重叠会重复计同一段价格路径）",
    "免费源/个人非商业许可数据仅内部试验，`delivery_eligible=False`，不得进对外下载包",
)


class MarketNotComputable(RuntimeError):
    """输入不满足本算子的前置条件（口径缺失/无数据/未复权）——**不降级成 0 或空表**。"""


# ---------------------------------------------------------------- 输入绑定与指纹

def binding(payload: dict) -> dict:
    """读数的输入绑定（`input_kind`）：没有这个就不算"绑定了选定运行"。"""
    return {
        "input_kind": INPUT_KIND,
        "schema": str(payload.get("schema") or ""),
        "dataset_id": str(payload.get("dataset_id") or ""),
        "source": str(payload.get("source") or ""),
        "license": str(payload.get("license") or "unknown"),
        "adj_basis": str(payload.get("adj_basis") or ""),
        "imported_at": str(payload.get("imported_at") or ""),
        "date_range": list(payload.get("date_range") or []),
        "instruments": sorted(str(c) for c in (payload.get("instruments") or [])),
        "rows": int(payload.get("rows") or 0),
    }


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(payload: dict) -> str:
    """输入绑定指纹：数据集 id + 口径 + 来源 + 行数一起进，换数据集一定换指纹。"""
    return _sha(_canon(binding(payload)))


def delivery_eligible(payload: dict) -> tuple[bool, str]:
    """能否进对外下载包：**按许可判**，不按"我们内部看过了"判。

    许可常量**不复制**：优先取读侧 `market_history` 的定义（唯一真相）；读侧拉不起来时退回
    字面量并标注 —— 这是许可判定，宁可不判也不误判成"可对外"。
    """
    lic = str(payload.get("license") or "unknown")
    try:
        from adapters import market_history as mh      # 延迟导入：读侧只用标准库
        blocked = (mh.LICENSE_FREE_TRIAL, mh.LICENSE_PERSONAL)
    except Exception:                                   # noqa: BLE001
        blocked = (INPUT_LICENSE_FREE_TRIAL, INPUT_LICENSE_PERSONAL)
    if lic in blocked or lic in ("", "unknown"):
        return False, (f"许可为「{lic or '未标注'}」：仅内部试验，不得进对外下载包；"
                       "对外交付须机构授权或只交付衍生结论")
    return True, ""


# ---------------------------------------------------------------- 切片（防未来函数）

def _series(payload: dict, code: str) -> list[tuple[str, float]]:
    out = []
    for r in (payload.get("data") or []):
        if str(r.get("code")) != str(code):
            continue
        try:
            c = float(r.get("close"))
        except (TypeError, ValueError):
            continue
        out.append((str(r.get("date") or "")[:10], c))
    out.sort(key=lambda t: t[0])
    return out


def _avail_map(payload: dict, code: str) -> dict:
    """逐行 `available_at`（只收**显式声明**的行；未声明的不进表，按常规口径处理）。

    10-06 复核 §3B："采集时间和市场可用时间不能混同"——这里只认行情行自己的
    `available_at`（市场可用时点），缺失就不设门槛，不用采集时间冒充。
    """
    out: dict[str, str] = {}
    for r in (payload.get("data") or ()):
        if str(r.get("code") or r.get("instrument") or "") != str(code):
            continue
        d = str(r.get("date") or "")[:10]
        av = str(r.get("available_at") or "").strip()[:10]
        if d and av:
            out[d] = av
    return out


def _slice(payload: dict, code: str, event_date: str, offsets, *,
           feature_cutoff: str = "", evaluation_as_of: str = "",
           calendar: dict | None = None) -> dict:
    """事件窗口切片（§12.4 三段语义）：

    - **特征/信号截点** `feature_cutoff`（默认＝事件日）：锚点＝该截点**当日或之前最后一个交易日**的收盘。
      锚点只用截点前的信息 ⇒ 后来的更正/结果/新闻不能倒灌成"当时就知道"。
    - **结果窗口** `offsets`：截点**之后**的交易日行，用于评价历史结果；只要评价时已可得就能参与。
    - **评价时点** `evaluation_as_of`：窗口内某点若在数据集里**根本没有交易日**，要区分
      "窗口未满（还没走到）"→ `pending` 与 "已成熟但缺行情" → `missing`，**都不报 0**。

    **`calendar` 给了就按交易日历定位**（`t±N` ＝真实第 N 个交易日）：这样"标的当天没有行情"
    才能在**有日历覆盖**时说清是**停牌**（那天开市、这只票不能交易）还是**休市**。
    不传日历时退回"按标的自身交易日序列数"的老行为 —— 那种情况下停牌会让窗口**静默错位**，
    所以结果里会标 `calendar_basis` 说明用的是哪一种，不假装等价。
    """
    from adapters import market_history as mh
    from adapters import trading_calendar as tc
    ev = mh._norm_date(event_date)                       # noqa: SLF001 读侧同一套日期归一
    if not ev:
        return {"ok": False, "reason": f"事件日无法解析：{event_date!r}"}
    cut = mh._norm_date(feature_cutoff) or ev            # noqa: SLF001
    asof = mh._norm_date(evaluation_as_of) or ""         # noqa: SLF001
    ser = _series(payload, code)
    if not ser:
        return {"ok": False, "reason": f"数据集里没有 {code} 的行"}
    # 10-06 复核 §3B：**逐行 `available_at` 必须真正约束结果**。此前它只被读进
    # `available_at_upper_bound` 这个上界字段，"有行即 observed"——于是评价时点评在
    # 2025-01-02、而未来两天（01-03/01-06）的价格仍被算成 +10%／+20% observed
    # （复核给了可复现输入输出）。这里把"该行在评价时点是否已可得"变成**门槛**：
    #   · 只对**显式声明** `available_at` 的行设门槛（未声明的按"交易日收盘即可得"的
    #     常规口径处理，不凭空造限）；采集时间与市场可用时间分开，各自只用于自己的判据。
    #   · 门槛不过 ⇒ 该点 `pending`（"尚不可得"），**不是** missing（那会暗示停牌/缺数据）。
    avail_of = _avail_map(payload, code)
    if asof:
        held = {d for d, av in avail_of.items() if av and av > asof}
    else:
        held = set()
    # 锚点是**特征侧**价格：它必须在特征截点就可获得，否则就是倒灌
    _anchor_av = avail_of.get(str(ser[0][0])) or ""
    before = [i for i, (d, _) in enumerate(ser) if d <= cut]
    if not before:
        return {"ok": False,
                "reason": f"{code} 在特征截点 {cut} 当日及之前没有交易日行（缺锚点，无法定价格起点）"}
    anchor_i = before[-1]
    _anchor_date = str(ser[anchor_i][0])
    _anchor_av = avail_of.get(_anchor_date) or ""
    if _anchor_av and _anchor_av > cut:
        return {"ok": False,
                "reason": (f"锚点交易日 {_anchor_date} 的行情在特征截点 {cut} 尚不可得"
                           f"（逐行 available_at={_anchor_av}）⇒ 该特征不可用于当时判断"
                           "（不倒灌成锚点）")}
    if anchor_i + 1 >= len(ser):
        return {"ok": False, "reason": f"{code} 在特征截点 {cut} 之后没有交易日行"}
    base_i = anchor_i + 1
    anchor = {"date": ser[anchor_i][0], "close": ser[anchor_i][1]}
    row_of = dict(ser)
    last_date = ser[-1][0]
    eff_asof = asof or last_date
    use_cal = tc.ok(calendar)
    # 数据集覆盖到哪天：**全载荷**的最后日期（不是单只标的），用来判"窗口未满"还是"停牌"
    data_end = max((str(r.get("date") or "")[:10] for r in (payload.get("data") or [])),
                   default=last_date)
    cal_off = tc.session_at_offset(calendar, tc.next_session(calendar, cut), 0) if use_cal else None
    pts: dict[str, dict] = {}
    for off in sorted(set(int(o) for o in offsets) | {ANCHOR_OFFSET}):
        if off == ANCHOR_OFFSET:
            pts[f"t{off:+d}"] = {"date": anchor["date"], "close": anchor["close"],
                                 "state": "observed"}
            continue
        if use_cal and cal_off:
            target = tc.session_at_offset(calendar, cal_off, off)
            if target is None:
                # 偏移超出**日历覆盖**：既不能说"未成熟"（不知道那天开不开），
                # 更不能说 `missing`（那会暗示停牌）。归到 `pending` 并写明是覆盖不足。
                pts[f"t{off:+d}"] = {
                    "date": None, "close": None, "state": "pending",
                    "unavailable": (f"交易日历覆盖到 {calendar.get('range', [None, None])[1]}，"
                                    f"无法确定 t0 之后第 {off} 个交易日 ⇒ 按未成熟处理"
                                    "（不记为 missing，避免暗示停牌）")}
                continue
            if target in row_of:
                if target in held:
                    pts[f"t{off:+d}"] = {
                        "date": target, "close": None, "state": "pending",
                        "unavailable": (f"该交易日行情在评价时点 {eff_asof} 尚不可得"
                                        f"（逐行 available_at={avail_of.get(target)}）"
                                        "⇒ 记为未成熟，不当作已观察结果")}
                    continue
                pts[f"t{off:+d}"] = {"date": target, "close": row_of[target],
                                     "state": "observed"}
                continue
            if target > data_end:
                pts[f"t{off:+d}"] = {
                    "date": target, "close": None, "state": "pending",
                    "unavailable": (f"窗口未满：行情数据到 {data_end}，该点是交易日历上的 "
                                    f"{target}（t0 之后第 {off} 个交易日）")}
            else:
                pts[f"t{off:+d}"] = {
                    "date": target, "close": None, "state": "missing",
                    "unavailable": (f"**交易日历确认 {target} 是交易日**，但 {code} 当日无行情行 "
                                    f"⇒ 停牌或该标的缺数据（不是休市）")}
            continue
        i = base_i + off
        if 0 <= i < len(ser):
            _d = str(ser[i][0])
            if _d in held:
                pts[f"t{off:+d}"] = {
                    "date": _d, "close": None, "state": "pending",
                    "unavailable": (f"该交易日行情在评价时点 {eff_asof} 尚不可得"
                                    f"（逐行 available_at={avail_of.get(_d)}）"
                                    "⇒ 记为未成熟，不当作已观察结果")}
                continue
            pts[f"t{off:+d}"] = {"date": ser[i][0], "close": ser[i][1], "state": "observed"}
            continue
        # 没有日历时：超出数据集范围，分清"未满"与"缺行情"（**都不补 0**）
        need = base_i + off
        if need >= len(ser):
            state = "pending" if eff_asof >= last_date else "missing"
            why = (f"窗口未满：数据集到 {last_date}，该点需要 t0 之后第 {off} 个交易日"
                   if state == "pending" else
                   f"已成熟（数据到 {last_date}）但缺该交易日的行情行：可能停牌/缺数据"
                   "（无交易日历 ⇒ 分不清停牌与休市）")
        else:
            state, why = "missing", "该点落在数据集起始之前（历史未覆盖）"
        pts[f"t{off:+d}"] = {"date": None, "close": None, "state": state, "unavailable": why}
    return {"ok": True, "event_date": ev, "feature_cutoff": cut,
            "evaluation_as_of": eff_asof, "anchor": anchor, "base_index": base_i,
            "base_date": ser[base_i][0], "points": pts,
            "affected_window": [anchor["date"], max(x["date"] or "" for x in pts.values())]}


def _excess(sub_ret, bench_ret):
    """几何基准调整：(1+个股) / (1+基准) - 1；任一缺失返回 None（**不补 0**）。"""
    if sub_ret is None or bench_ret is None:
        return None
    if (1 + Decimal(str(bench_ret))) == 0:
        return None
    q = lambda x: float(Decimal(str(x)).quantize(Decimal("0.000001")))   # noqa: E731
    return q((1 + Decimal(str(sub_ret))) / (1 + Decimal(str(bench_ret))) - 1)


def _ret(anchor: float, close) -> float | None:
    if close is None or not anchor:
        return None
    q = lambda x: float(Decimal(str(x)).quantize(Decimal("0.000001")))   # noqa: E731
    return q(Decimal(str(close)) / Decimal(str(anchor)) - 1)


# ---------------------------------------------------------------- 重叠识别与去重

def dedup(events: list[dict], offsets=DEFAULT_OFFSETS, *, policy: str = "keep_earliest",
          payload: dict | None = None) -> dict:
    """**同标的**窗口重叠识别（跨标的日历重叠不算重叠，见下）。

    为什么必须按标的：重叠之所以要排除，是因为**同一段价格路径被数两次**；不同标的各自的价格
    序列是两个独立单元，日历重叠只是共享市场因子，而聚合本来就拒绝跨标的平均。把跨标的也当重叠
    会把"两家公司同期披露"这种最常见的样本直接砍到一个——那是把纪律用成了丢数据。

    判重叠用**真实交易日间隔**（数据集里该标的的日序，`payload` 给了就用）；数据集缺该标的时
    退回自然日近似（`×5/7`），且判据取 `<=`——**宁可多排除，不可重复计数**。

    `policy="keep_all"` 只做标注、不排除；被排除的事件**仍出单列读数**，只是不进聚合。
    """
    span = max([int(o) for o in offsets] + [0]) - min([int(o) for o in offsets] + [0])
    ordered = sorted(
        ({"code": str(e.get("code") or ""), "date": str(e.get("date") or ""),
          "label": str(e.get("label") or e.get("date") or "")} for e in events),
        key=lambda e: (e["date"], e["code"]))
    kept: list[dict] = []
    dropped: list[dict] = []
    last_by_code: dict[str, dict] = {}
    for e in ordered:
        prev = last_by_code.get(e["code"])
        gap = _session_gap(payload, e["code"], e["date"], prev["date"]) if prev else None
        overlap = bool(prev and gap is not None and gap <= span)
        e["overlaps_previous"] = overlap
        if overlap and policy != "keep_all":
            dropped.append({**e, "reason": (
                f"同一标的 {e['code']} 与已保留事件「{prev['label']}」({prev['date']}) 窗口重叠："
                f"两事件相隔 {gap} 个交易日 ≤ 窗口跨度 {span} ⇒ 该段价格路径会被重复计数，"
                "不进聚合（读数仍单列，可单独看）")})
            continue
        kept.append(e)
        last_by_code[e["code"]] = e
    return {"policy": policy, "span_sessions": span, "kept": kept, "dropped": dropped,
            "cross_code_note": ("跨标的不计重叠：不同标的是两个独立实验单元，聚合本身不跨标的平均")}


def _session_gap(payload: dict | None, code: str, d1: str, d2: str) -> int | None:
    """两个日期之间该标的的**真实交易日数**（左开右闭）；数据集缺该标的时退回自然日近似。"""
    lo, hi = sorted([str(d1)[:10], str(d2)[:10]])
    if payload:
        ser = [d for d, _ in _series(payload, code)]
        if ser:
            return sum(1 for d in ser if lo < d <= hi)
    return _calendar_gap(d1, d2)


def _calendar_gap(d1: str, d2: str) -> int | None:
    """自然日近似交易日数：`floor(天数 × 5/7)`，只用于判重叠，不用于任何收益计算。"""
    from datetime import date
    try:
        a = date(*(int(x) for x in str(d1)[:10].split("-")[:3]))
        b = date(*(int(x) for x in str(d2)[:10].split("-")[:3]))
    except (TypeError, ValueError):
        return None
    return int(abs((a - b).days) * 5 / 7)


# ---------------------------------------------------------------- 主算子

def _calendar_gaps(bench_ser: list[tuple[str, float]], subj: list[tuple[str, float]],
                   start: str, end: str) -> list[str]:
    """以**基准序列为交易日历代理**，找出标的"基准有行、自己没有"的日期。

    为什么需要：`offsets` 是按**标的自己的交易日序列**数的，所以标的停牌一天，`t+5` 会静默地
    变成"晚一个自然交易日的第 5 根 K 线"，而调用方看不出来。缺独立交易日历时，指数序列是唯一
    可用的日历代理 —— 但它**分不清"停牌"与"该标的当日无数据"**，所以只能报"疑似"，不能断言。
    """
    if not bench_ser or not subj:
        return []
    have = {d for d, _ in subj}
    return [d for d, _ in bench_ser if start <= d <= end and d not in have]


def _cal_ok(calendar: dict | None) -> bool:
    try:
        from adapters import trading_calendar as tc
        return tc.ok(calendar)
    except Exception:                                        # noqa: BLE001
        return False


def compute(payload: dict, events: list[dict], *, benchmark: str = "",
            offsets=DEFAULT_OFFSETS, policy: str = "keep_earliest",
            allow_unadjusted: bool = False, feature_cutoff: str = "",
            evaluation_as_of: str = "", calendar: dict | None = None) -> dict:
    """事件窗口基准调整收益读数（确定性；同输入同输出）。

    `feature_cutoff`（默认＝每个事件自己的事件日）与 `evaluation_as_of`（默认＝数据集最后交易日）
    是 §12.4 的两段语义：前者定"当时知道什么"，后者定"截至何时评价"。

    返回 `status="unavailable"` 时**必须**带 `reason`／`unavailable[]`，调用方不得当成 0 收益。
    """
    offs = tuple(sorted({int(o) for o in offsets} | {ANCHOR_OFFSET}))
    bind = binding(payload)
    fp = fingerprint(payload)
    out: dict = {"schema": SCHEMA, "operator": OPERATOR, "impl_version": IMPL_VERSION,
                 "kind": "event_reaction_observation", "executable": False,
                 "input_kind": bind, "input_fingerprint": fp,
                 "params": {"benchmark": str(benchmark or ""), "offsets": list(offs),
                            "policy": str(policy), "allow_unadjusted": bool(allow_unadjusted),
                            "feature_cutoff": str(feature_cutoff or ""),
                            "evaluation_as_of": str(evaluation_as_of or ""),
                            "anchor": "t-1 ＝ feature_cutoff 当日或之前最后一个交易日（价格起点）",
                            "t0": "披露日之后第一个交易日（保守下一交易日规则，不臆造盘中时间）",
                            "price_basis": "收盘价（不可执行；可执行收益需盘中时间与开盘价）",
                            "calendar": ("按交易日历定位 t±N" if _cal_ok(calendar) else
                                         "无交易日历：offsets 按标的自身交易日序列数，"
                                         "停牌会让窗口静默错位")},
                 "limits": list(LIMITS_BASE), "unavailable": [], "readings": [],
                 "aggregate": None}

    if str(payload.get("source")) == "unavailable" or not payload.get("data"):
        out["status"] = "unavailable"
        out["reason"] = str(payload.get("reason") or "没有行情数据")
        out["unavailable"].append({"what": "rows", "reason": out["reason"]})
        return _finalize(out)
    if not bind["adj_basis"]:
        out["status"] = "unavailable"
        out["reason"] = "数据集未标注复权口径（adj_basis 为空）⇒ 不产收益读数（口径不明不得算收益）"
        out["unavailable"].append({"what": "adj_basis", "reason": out["reason"]})
        return _finalize(out)
    if not events:
        out["status"] = "unavailable"
        out["reason"] = "没有事件：不产空读数"
        out["unavailable"].append({"what": "events", "reason": out["reason"]})
        return _finalize(out)

    unadj = "不复权" in bind["adj_basis"] and "adj_factor" not in bind["adj_basis"]
    if unadj and not allow_unadjusted:
        out["status"] = "unavailable"
        out["reason"] = ("标的口径为不复权：窗口内若含除权除息，价格跌幅含分红除权成分，"
                         "不构成收益读数。需复权序列，或显式 allow_unadjusted=True 降级为 caveat 读数")
        out["unavailable"].append({"what": "adj_basis", "reason": out["reason"]})
        return _finalize(out)
    if unadj:
        out["limits"].append("标的为不复权序列且调用方显式放行：若窗口含除权除息，读数被分红除权污染")

    ded = dedup(events, offs, policy=policy, payload=payload)
    dropped_keys = {f"{d['code']}@{d['date']}" for d in ded["dropped"]}
    reason_by_key = {f"{d['code']}@{d['date']}": d["reason"] for d in ded["dropped"]}
    out["overlap"] = {"policy": ded["policy"], "span_sessions": ded["span_sessions"],
                      "aggregate_kept": [f"{e['code']}@{e['date']}" for e in ded["kept"]],
                      "aggregate_dropped": [{"event": k, "reason": reason_by_key[k]}
                                            for k in sorted(dropped_keys)],
                      "cross_code_note": ded["cross_code_note"]}
    bench_ser = _series(payload, benchmark) if benchmark else []

    # 每个事件都出**单列读数**；只有未被判重叠的才 `aggregate_eligible`（读数不缺，只是不进聚合）
    all_events = sorted(ded["kept"] + ded["dropped"], key=lambda e: (e["date"], e["code"]))
    for e in all_events:
        key = f"{e['code']}@{e['date']}"
        sl = _slice(payload, e["code"], e["date"], offs,
                    feature_cutoff=feature_cutoff, evaluation_as_of=evaluation_as_of,
                    calendar=calendar)
        if not sl.get("ok"):
            out["unavailable"].append({"what": key, "reason": sl["reason"]})
            continue
        anchor_c = sl["anchor"]["close"]
        anchors_b = _ret_anchor(bench_ser, sl["anchor"]["date"])
        item = {"label": e["label"], "code": e["code"], "event_date": sl["event_date"],
                "feature_cutoff": sl["feature_cutoff"],
                "evaluation_as_of": sl["evaluation_as_of"],
                "anchor": sl["anchor"], "t0_date": sl["base_date"],
                "affected_window": sl["affected_window"],
                "benchmark": benchmark or "", "points": [], "benchmark_missing": [],
                "aggregate_eligible": key not in dropped_keys,
                "aggregate_exclusion_reason": reason_by_key.get(key, "")}
        states = {sl["points"][f"t{off:+d}"]["state"] for off in offs}
        item["window_state"] = ("pending" if "pending" in states else
                                "partial_missing" if "missing" in states else "observed")
        if benchmark:
            subj_ser = _series(payload, e["code"])
            item["suspension_suspect"] = _calendar_gaps(
                bench_ser, subj_ser, sl["anchor"]["date"],
                max(x["date"] or "" for x in sl["points"].values()))
            if item["suspension_suspect"]:
                out["calendar_proxy_used"] = ("以基准序列为交易日历代理：上述日期基准有行、标的无行，"
                                              "**疑似停牌或缺行**；缺独立交易日历时无法与休市区分，"
                                              "故只报疑似、不断言；offsets 按标的自身交易日计")
        else:
            item["suspension_suspect"] = []
        # **有独立交易日历时升级为定性结论**：那天是交易日但标的不存在 ⇒ 停牌/缺数据。
        # 与上面的"疑似"分开命名，避免把代理推断与日历判定混为一谈。
        if _cal_ok(calendar):
            from adapters import trading_calendar as tc
            gap = tc.suspend_dates(calendar, [d for d, _ in _series(payload, e["code"])],
                                   sl["affected_window"][0], sl["affected_window"][1])
            item["suspension_dates"] = gap.get("suspension_dates") or []
            item["suspension_basis"] = gap.get("basis") or gap.get("reason")
            item["suspension_certain"] = bool(gap.get("ok"))
            if gap.get("suspension_dates"):
                out["calendar_basis"] = ("独立交易日历：`suspension_dates` 是**确证**"
                                         "（日历判定为交易日、标的无行情），不再是疑似")
        else:
            item["suspension_dates"] = []
            item["suspension_certain"] = False
            item["suspension_basis"] = "无独立交易日历：停牌与休市无法区分（只用基准代理报疑似）"
        for off in offs:
            p = sl["points"][f"t{off:+d}"]
            if off == ANCHOR_OFFSET:
                # 锚点是**基准点**，不是一次测量。填 0.00% 会被读成"事件日无波动"，
                # 所以这里显式留空 + 说明；窗口内所有收益都是相对它的变化。
                item["points"].append({"offset": off, "date": p.get("date"),
                                       "close": p.get("close"), "return": None,
                                       "benchmark_return": None, "excess_return": None,
                                       "state": "anchor",
                                       "note": "锚点（价格起点）：无收益读数，仅用于定位",
                                       "unavailable": p.get("unavailable", "")})
                continue
            r = _ret(anchor_c, p.get("close"))
            br = None
            if p.get("close") is None:
                # pending / missing：**不报 0**，也不假装是"零收益"。
                # 有日历时 `date` 是**真实交易日**（缺行情不等于不知道哪天）⇒ 必须带出去，
                # 否则"停牌发生在哪一天"就又丢了。
                item["points"].append({"offset": off, "date": p.get("date"), "close": None,
                                       "return": None, "benchmark_return": None,
                                       "excess_return": None, "state": p.get("state", "missing"),
                                       "unavailable": p.get("unavailable", "")})
                continue
            if benchmark:
                bc = _lookup(bench_ser, p.get("date"))
                b_anchor = _lookup(bench_ser, sl["anchor"]["date"])
                if bc is None or b_anchor is None or not anchors_b:
                    item["benchmark_missing"].append(
                        {"date": p.get("date"), "reason": "基准在该日期没有行 ⇒ 该点超额留空（不补 0）"})
                else:
                    br = _ret(b_anchor, bc)
            item["points"].append({"offset": off, "date": p.get("date"),
                                   "close": p.get("close"), "return": r,
                                   "benchmark_return": br, "excess_return": _excess(r, br),
                                   "state": "observed",
                                   "unavailable": p.get("unavailable", "")})
        out["readings"].append(item)

    if not out["readings"]:
        out["status"] = "unavailable"
        out["reason"] = "所有事件都取不到窗口（见 unavailable）"
        return _finalize(out)
    out["status"] = "ok"
    out["aggregate"] = _aggregate(out["readings"], offs)
    out["available_at_upper_bound"] = _avail_upper(payload, out["readings"])
    return _finalize(out)


def _ret_anchor(bench_ser, anchor_date):
    c = _lookup(bench_ser, anchor_date)
    return bool(c)


def _lookup(ser: list[tuple[str, float]], date_str):
    d = str(date_str or "")[:10]
    if not d:
        return None
    for dd, c in ser:
        if dd == d:
            return c
    return None


def _avail_upper(payload: dict, readings: list[dict]) -> str:
    """读数用到的最后一个交易日的可得时间上界：证明"当时拿得到"。"""
    dates = [p["date"] for r in readings for p in r["points"] if p.get("date")]
    if not dates:
        return ""
    last = max(dates)
    row = next((r for r in (payload.get("data") or [])
                if str(r.get("date"))[:10] == last), None)
    return str((row or {}).get("available_at") or f"{last}（数据集未逐行标注 available_at）")


def _aggregate(readings: list[dict], offs) -> dict:
    """**按标的分别聚合**：同一标的多个不重叠事件可平均；跨标的**不合并**。

    为什么按标的而不是全拒：同一标的的两期披露是两个时点的独立事件，平均是有意义的观察；
    跨标的的"同一日历日"则是共享市场因子的两个单元，合并会把因子暴露当成事件效应。
    """
    used = [r for r in readings if r.get("aggregate_eligible")]
    excluded = [f"{r['code']}@{r['event_date']}" for r in readings
                if not r.get("aggregate_eligible")]
    codes = sorted({r["code"] for r in used})
    agg: dict = {
        "readings_total": len(readings), "readings_used": len(used),
        "excluded_from_aggregate": excluded, "codes": codes,
        "cross_code_pooling": ("跨标的不合并平均：同一日历日对不同标的是两个实验单元，"
                               "合并会把共同市场因子当成事件效应"),
        "by_code": {},
    }
    if not used:
        agg["note"] = "没有可参与聚合的读数（同标的窗口重叠的已排除）"
        return agg
    for code in codes:
        sub = [r for r in used if r["code"] == code]
        cells: dict = {}
        for off in offs:
            vals = [p["excess_return"] for r in sub for p in r["points"]
                    if p["offset"] == off and p["excess_return"] is not None]
            cell = {"n_excess": len(vals),
                    "n_pending": sum(1 for r in sub for p in r["points"]
                                     if p["offset"] == off and p.get("state") == "pending"),
                    "n_missing": sum(1 for r in sub for p in r["points"]
                                     if p["offset"] == off and p.get("state") == "missing")}
            if len(vals) >= 2:
                s = sorted(vals)
                mid = (s[len(s) // 2] if len(s) % 2
                       else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2)
                cell["mean_excess_return"] = sum(vals) / len(vals)
                cell["median_excess_return"] = mid
            else:
                cell["note"] = f"n={len(vals)} < 2：不给均值/中位数（样本不足不产统计量）"
            if len(vals) < 3:
                cell["no_proportion_stat"] = "n < 3：不给「胜率/上涨占比」这类比例字段"
            cells[f"t{off:+d}"] = cell
        agg["by_code"][code] = {"event_dates": [r["event_date"] for r in sub],
                                "n_events": len(sub), "by_offset": cells}
    return agg


def _finalize(out: dict) -> dict:
    """`reading_hash` 覆盖绑定＋参数＋读数＋聚合：换输入/换参数/换结论都会换哈希。"""
    ok, why = delivery_eligible({"license": (out.get("input_kind") or {}).get("license")})
    out["delivery_eligible"] = ok
    if not ok:
        out["delivery_block_reason"] = why
        out["limits"].append(why)
    core = {k: out.get(k) for k in ("schema", "operator", "impl_version", "status", "kind",
                                    "executable", "input_kind", "input_fingerprint", "params",
                                    "readings", "aggregate", "overlap", "unavailable")}
    out["reading_hash"] = _sha(_canon(core))
    return out


def replay(reading: dict, payload: dict, events: list[dict]) -> dict:
    """用同一输入重算，比对读数哈希与**逐点差异**。不一致就报差异，**不改写旧读数**。"""
    params = dict(reading.get("params") or {})
    fresh = compute(payload, events, benchmark=params.get("benchmark", ""),
                    offsets=params.get("offsets") or DEFAULT_OFFSETS,
                    policy=params.get("policy", "keep_earliest"),
                    allow_unadjusted=bool(params.get("allow_unadjusted")),
                    feature_cutoff=params.get("feature_cutoff", ""),
                    evaluation_as_of=params.get("evaluation_as_of", ""))
    diffs: list[str] = []
    if fresh.get("reading_hash") != reading.get("reading_hash"):
        diffs.append("reading_hash 不一致")
    if fresh.get("input_fingerprint") != reading.get("input_fingerprint"):
        diffs.append("input_fingerprint 不一致（输入数据集换了）")
    old = {(r["code"], r["event_date"], p["offset"]): p.get("excess_return")
           for r in reading.get("readings") or [] for p in r.get("points") or []}
    new = {(r["code"], r["event_date"], p["offset"]): p.get("excess_return")
           for r in fresh.get("readings") or [] for p in r.get("points") or []}
    for k in sorted(set(old) | set(new), key=str):
        if old.get(k) != new.get(k):
            diffs.append(f"{k}: {old.get(k)} → {new.get(k)}")
    return {"same": not diffs, "diffs": diffs[:20], "fresh_reading_hash": fresh.get("reading_hash"),
            "old_reading_hash": reading.get("reading_hash"),
            "input_fingerprint": fresh.get("input_fingerprint")}
