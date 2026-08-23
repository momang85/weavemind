# -*- coding: utf-8 -*-
"""腾讯行情适配器（qt.gtimg.cn）：批量报价 + 活跃候选池排行。

接口（免费无 key，GBK 编码，字段以 ~ 分隔）：
    GET https://qt.gtimg.cn/q=sh600519,sz000858,usAAPL
    每次最多 50 只；返回多行 v_{code}="字段1~字段2~...";

实测字段索引（与多个公开字段表核对）：
    0 未知 / 1 名称 / 2 代码 / 3 最新价 / 4 昨收 / 5 今开 /
    6 成交量(手) / 7 外盘 / 8 内盘 / ... / 30 时间 /
    31 涨跌 / 32 涨跌幅% / 33 最高 / 34 最低 /
    35 价格/量/额 / 36 成交量(手) / 37 成交额(万元) /
    38 换手率% / 44 流通市值(亿) / 45 总市值(亿)

注意：任务说明中“index 5=涨跌%”与实际字段表不符（5 是今开），
涨跌幅按 index 32 解析；index 5 仅作为个别接口变体的缺失兜底。
"""

import http.client
import logging
import re
import time
import urllib.error

from adapters.ashare_ranking import _get_via_socket, _get_via_urllib
from adapters.source_health import ensure_available, mark_failure, mark_success

_logger = logging.getLogger(__name__)

_BASE = "https://qt.gtimg.cn"
_BATCH_SIZE = 50

# 完整浏览器头：与东方财富适配器同样的双通道思路（urllib 失败 → socket HTTP/1.0）。
_TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://gu.qq.com/",
    "Connection": "close",
}

# 近月高成交额/高活跃度 A股候选池（约 110 只，覆盖大金融、白酒、新能源、
# 半导体、算力、有色、央企蓝筹等），用于无现成排行接口时的活跃度近似。
A_SHARE_CANDIDATES: list[tuple[str, str]] = [
    ("sh600519", "贵州茅台"), ("sh601318", "中国平安"),
    ("sh601138", "工业富联"), ("sh601127", "赛力斯"),
    ("sh688981", "中芯国际"), ("sh688041", "海光信息"),
    ("sh688256", "寒武纪"), ("sh688008", "澜起科技"),
    ("sh688111", "金山办公"), ("sh688036", "传音控股"),
    ("sh601012", "隆基绿能"), ("sh600438", "通威股份"),
    ("sh600089", "特变电工"), ("sh600028", "中国石化"),
    ("sh601857", "中国石油"), ("sh600938", "中国海油"),
    ("sh600900", "长江电力"),
    ("sh601985", "中国核电"), ("sz003816", "中国广核"),
    ("sh601899", "紫金矿业"), ("sh603993", "洛阳钼业"),
    ("sh601600", "中国铝业"), ("sh600111", "北方稀土"),
    ("sh601168", "西部矿业"),
    ("sh600362", "江西铜业"),
    ("sz000807", "云铝股份"), ("sz000933", "神火股份"),
    ("sh600547", "山东黄金"), ("sh600489", "中金黄金"),
    ("sh600988", "赤峰黄金"), ("sh601398", "工商银行"),
    ("sh601288", "农业银行"), ("sh601988", "中国银行"),
    ("sh601939", "建设银行"), ("sh601328", "交通银行"),
    ("sh601166", "兴业银行"), ("sh600000", "浦发银行"),
    ("sh600016", "民生银行"), ("sh601818", "光大银行"),
    ("sh601998", "中信银行"), ("sz000001", "平安银行"),
    ("sz002142", "宁波银行"), ("sh600919", "江苏银行"),
    ("sh601838", "成都银行"), ("sh600036", "招商银行"),
    ("sh600030", "中信证券"), ("sh601688", "华泰证券"),
    ("sh601211", "国泰海通"), ("sz000776", "广发证券"),
    ("sh600958", "东方证券"), ("sh601377", "兴业证券"),
    ("sz000166", "申万宏源"), ("sh601995", "中金公司"),
    ("sh600999", "招商证券"), ("sz300059", "东方财富"),
    ("sz300033", "同花顺"), ("sz300803", "指南针"),
    ("sh600570", "恒生电子"), ("sz002230", "科大讯飞"),
    ("sh601728", "中国电信"), ("sh600941", "中国移动"),
    ("sh601633", "长城汽车"), ("sh600104", "上汽集团"),
    ("sh601238", "广汽集团"), ("sz000625", "长安汽车"),
    ("sz002594", "比亚迪"), ("sz300750", "宁德时代"),
    ("sz000725", "京东方A"), ("sz000100", "TCL科技"),
    ("sz002475", "立讯精密"), ("sz002241", "歌尔股份"),
    ("sh603501", "韦尔股份"), ("sh603986", "兆易创新"),
    ("sh688012", "中微公司"), ("sz002371", "北方华创"),
    ("sz000063", "中兴通讯"), ("sz300308", "中际旭创"),
    ("sz300502", "新易盛"), ("sz002463", "沪电股份"),
    ("sz300476", "胜宏科技"), ("sh600183", "生益科技"),
    ("sz000977", "浪潮信息"), ("sh603019", "中科曙光"),
    ("sz000938", "紫光股份"), ("sh600809", "山西汾酒"),
    ("sz000858", "五粮液"), ("sz000568", "泸州老窖"),
    ("sz002304", "洋河股份"), ("sz000596", "古井贡酒"),
    ("sh600887", "伊利股份"), ("sh603288", "海天味业"),
    ("sz000333", "美的集团"), ("sz000651", "格力电器"),
    ("sh600690", "海尔智家"), ("sh601888", "中国中免"),
    ("sz000002", "万科A"), ("sh600048", "保利发展"),
    ("sz001979", "招商蛇口"), ("sz002271", "东方雨虹"),
    ("sh601668", "中国建筑"), ("sh601390", "中国中铁"),
    ("sh601186", "中国铁建"), ("sh601800", "中国交建"),
    ("sh601006", "大秦铁路"), ("sh601816", "京沪高铁"),
    ("sz002352", "顺丰控股"), ("sh601919", "中远海控"),
    ("sh601111", "中国国航"), ("sh600029", "南方航空"),
    ("sh601021", "春秋航空"),
    ("sz300124", "汇川技术"), ("sz002050", "三花智控"),
    ("sh601766", "中国中车"), ("sh601989", "中国重工"),
    ("sh600150", "中国船舶"), ("sh600276", "恒瑞医药"),
    ("sh603259", "药明康德"),
    ("sz300760", "迈瑞医疗"), ("sh688271", "联影医疗"),
    ("sz002714", "牧原股份"),
]

# 美股候选池（约 60 只，覆盖大型科技、芯片、消费、金融、能源与热门标的）。
US_CANDIDATES: list[tuple[str, str]] = [
    ("usAAPL", "苹果"), ("usMSFT", "微软"), ("usNVDA", "英伟达"),
    ("usTSLA", "特斯拉"), ("usAMZN", "亚马逊"), ("usGOOGL", "谷歌A"),
    ("usGOOG", "谷歌C"), ("usMETA", "Meta"), ("usAVGO", "博通"),
    ("usAMD", "超威半导体"), ("usPLTR", "Palantir"), ("usNFLX", "奈飞"),
    ("usBABA", "阿里巴巴"), ("usINTC", "英特尔"), ("usORCL", "甲骨文"),
    ("usQCOM", "高通"), ("usSMCI", "超微电脑"), ("usMU", "美光"),
    ("usTSM", "台积电"), ("usCRM", "赛富时"), ("usDIS", "迪士尼"),
    ("usKO", "可口可乐"), ("usJPM", "摩根大通"), ("usV", "维萨"),
    ("usMA", "万事达"), ("usUNH", "联合健康"), ("usXOM", "埃克森美孚"),
    ("usBAC", "美国银行"), ("usWMT", "沃尔玛"), ("usCOST", "好市多"),
    ("usPG", "宝洁"), ("usHD", "家得宝"), ("usPEP", "百事"),
    ("usPFE", "辉瑞"), ("usMRK", "默沙东"), ("usABBV", "艾伯维"),
    ("usCVX", "雪佛龙"), ("usADBE", "Adobe"), ("usCSCO", "思科"),
    ("usIBM", "IBM"), ("usTXN", "德州仪器"), ("usAXP", "美国运通"),
    ("usGS", "高盛"), ("usMS", "摩根士丹利"), ("usCAT", "卡特彼勒"),
    ("usBA", "波音"), ("usGE", "通用电气"), ("usLLY", "礼来"),
    ("usAMAT", "应用材料"), ("usLRCX", "泛林集团"), ("usNOW", "ServiceNow"),
    ("usINTU", "财捷"), ("usUBER", "优步"), ("usCOIN", "Coinbase"),
    ("usMSTR", "MicroStrategy"), ("usHOOD", "Robinhood"), ("usRIVN", "Rivian"),
    ("usSOFI", "SoFi"), ("usDKNG", "DraftKings"), ("usEBAY", "eBay"),
]


class TencentQuotesError(RuntimeError):
    """腾讯行情获取失败：携带通道与原因，便于上层按 <通道>: <原因> 记录。"""

    def __init__(self, channel: str, reason: str):
        super().__init__(f"{channel}: {reason}")
        self.channel = channel
        self.reason = reason


def _num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' / 空串等缺失标记返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v


def _get(url: str, timeout: int = 15, attempt: int = 1) -> str:
    """GET 文本：复用东方财富适配器的 urllib → socket HTTP/1.0 双通道。

    腾讯响应为 GBK，双通道均按 gbk 解码；两通道都失败抛异常并带原因。
    """
    try:
        return _get_via_urllib(
            url, timeout=timeout, encoding="gbk", headers=_TENCENT_HEADERS,
        )
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
        urllib_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "tencent fetch attempt %d failed: urllib: %s",
            attempt, urllib_reason,
        )
    try:
        return _get_via_socket(
            url, timeout=timeout, encoding="gbk", headers=_TENCENT_HEADERS,
        )
    except Exception as exc:
        socket_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "tencent fetch attempt %d failed: socket: %s",
            attempt, socket_reason,
        )
        raise TencentQuotesError(
            "urllib+socket",
            f"urllib: {urllib_reason}; socket: {socket_reason}",
        ) from exc


def _tencent_code(code: str) -> str:
    """裸代码 → 腾讯带市场前缀的代码；已带前缀则原样返回。"""
    code = str(code).strip()
    low = code.lower()
    if re.match(r"^(sh|sz|bj|hk|us)\d*$", low):
        return code
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return code


def _bare_code(code: str) -> str:
    """sh600519 → 600519；usAAPL → AAPL（与 eastmoney 行结构保持一致）。"""
    code = str(code).strip()
    m = re.match(r"^(sh|sz|bj|hk|us)(.+)$", code, flags=re.IGNORECASE)
    return m.group(2) if m else code


def _parse_quotes_text(text: str, requested_codes: list[str]) -> dict:
    """解析 qt.gtimg.cn 响应：{原始请求代码: {name, price, change_pct, volume, amount, ...}}。"""
    wanted: dict[str, str] = {}
    for orig, tcode in zip(requested_codes, [_tencent_code(c) for c in requested_codes]):
        wanted[tcode.lower()] = orig
    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("v_"):
            continue
        key_part, sep, rest = line.partition("=")
        if not sep:
            continue
        tcode = key_part[2:].strip().lower()
        if tcode not in wanted:
            continue
        body = rest.strip()
        if body.startswith('"'):
            body = body[1:]
        if body.endswith('";'):
            body = body[:-2]
        elif body.endswith('"'):
            body = body[:-1]
        fields = body.split("~")
        if len(fields) < 39:
            # v_pv_none_match 等无效代码行/停牌残缺行：跳过
            continue
        price = _num(fields[3])
        change_pct = _num(fields[32])
        if change_pct is None:
            # 兜底：个别接口变体把涨跌幅放在 index 5（正常数据 5 是今开）
            change_pct = _num(fields[5])
        volume = _num(fields[6])
        amount_wan = _num(fields[37])
        out[wanted[tcode]] = {
            "name": str(fields[1] or ""),
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "amount": (
                round(amount_wan * 1e4, 2) if amount_wan is not None else None
            ),
            "turnover_pct": _num(fields[38]),
            "market_cap_yi": _num(fields[45]),
        }
    return out


def fetch_quotes(codes: list[str]) -> dict:
    """批量拉取腾讯行情（一次 ≤50 只，自动分批）。

    Args:
        codes: 股票代码列表，支持裸代码（600519）或带前缀（sh600519/usAAPL）。

    Returns:
        {请求代码: {name, price, change_pct, volume, amount, turnover_pct,
        market_cap_yi}}；volume 单位为手，amount 单位为元。
        失败抛 TencentQuotesError（带通道与原因）。
    """
    if not codes:
        return {}
    original = [str(c).strip() for c in codes]
    result: dict[str, dict] = {}
    for i in range(0, len(original), _BATCH_SIZE):
        batch = original[i:i + _BATCH_SIZE]
        tcodes = [_tencent_code(c) for c in batch]
        url = f"{_BASE}/q=" + ",".join(tcodes)
        text = _get(url)
        parsed = _parse_quotes_text(text, batch)
        missing = [c for c in batch if c not in parsed]
        if missing:
            raise TencentQuotesError(
                "parse",
                f"腾讯行情缺少 {len(missing)} 只数据: {','.join(missing[:10])}",
            )
        result.update(parsed)
    return result


def _rank_candidate_pool(
    pool: list[tuple[str, str]],
    metric: str,
    top_n: int,
    source: str,
    market: str,
) -> dict:
    """候选池拉行情 → 按 metric 排序 → eastmoney_ranking 兼容 payload。

    source 决定健康注册表键（tencent_ranking / tencent_us_ranking）：
    冷却期内快失败，成功/失败同步维护源健康状态。
    """
    top_n = max(1, min(int(top_n), 50))
    ensure_available(source)
    try:
        codes = [code for code, _name in pool]
        quotes = fetch_quotes(codes)
        sort_key = "volume" if metric == "volume" else "amount"
        valid = [
            (code, q) for code, q in quotes.items()
            if q.get(sort_key) is not None
        ]
        valid.sort(key=lambda kv: kv[1][sort_key], reverse=True)
        rows: list[dict] = []
        for i, (code, q) in enumerate(valid[:top_n], 1):
            volume = q.get("volume")
            amount = q.get("amount")
            rows.append({
                "rank": i,
                "code": _bare_code(code),
                "name": q.get("name") or "",
                "price": q.get("price"),
                "change_pct": q.get("change_pct"),
                "volume_hand": volume,
                "volume_wan_hand": (
                    round(volume / 1e4, 2) if volume is not None else None
                ),
                "amount_yuan": amount,
                "amount_yi": (
                    round(amount / 1e8, 2) if amount is not None else None
                ),
                "turnover_pct": q.get("turnover_pct"),
                "market_cap_yi": q.get("market_cap_yi"),
            })
        if not rows:
            raise TencentQuotesError("empty", "腾讯候选池无有效行情数据")
        result = {
            "rows": rows,
            "metric": metric,
            "top_n": len(rows),
            "source": source,
            "market": market,
            "source_url": (
                f"{_BASE}/q="
                + ",".join(
                    [_tencent_code(c) for c, _ in pool[:_BATCH_SIZE]]
                )
            ),
            "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        mark_failure(source, exc)
        raise
    mark_success(source)
    return result


def fetch_ranking(metric: str = "amount", top_n: int = 10) -> dict:
    """A股 成交额/成交量排行前 N（腾讯候选池近似，无现成排行接口）。

    Returns 结构与 eastmoney_ranking 兼容，source 标 tencent_ranking。
    """
    return _rank_candidate_pool(
        A_SHARE_CANDIDATES, metric, top_n, "tencent_ranking", "A股",
    )


def fetch_us_ranking(metric: str = "amount", top_n: int = 10) -> dict:
    """美股 成交额/成交量排行前 N（腾讯候选池近似）。

    Returns 结构与 eastmoney_ranking 兼容，source 标 tencent_us_ranking。
    """
    return _rank_candidate_pool(
        US_CANDIDATES, metric, top_n, "tencent_us_ranking", "美股",
    )


if __name__ == "__main__":
    import sys
    market = sys.argv[1] if len(sys.argv) > 1 else "a"
    metric = sys.argv[2] if len(sys.argv) > 2 else "amount"
    fn = fetch_us_ranking if market == "us" else fetch_ranking
    result = fn(metric, 10)
    print(__import__("json").dumps(result, ensure_ascii=False, indent=1))
