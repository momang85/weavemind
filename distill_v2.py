# -*- coding: utf-8 -*-
"""蒸馏数据合成 v2（对标标准 4.1：数据合成蒸馏全流程）。

流程：多样化查询生成（temperature 0.9）→ 教师标注（0.1，来源约束）→
      schema 质量过滤 → 去重 → 增量写入（中断续跑）→ 并行标注。

针对 content_summary 的两个短板：
- 来源标注纪律：教师输出必须含 sources 字段（每条数据点的来源声明），
  并明确'无法核实→标注基于模型知识'（对齐验收器诚实性检查）；
- 图表提取：教师输出必须含 charts（结构化图表规格），无数据→空数组。
"""
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import extract_json_object

ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
TEACHER_MODEL = "glm-4-flash"

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
# 参数化 Worker 类型：文件按 worker 区分，多 Worker 蒸馏互不覆盖。
# content_summary 是首个 Worker，历史上使用固定文件名（distill_v2_raw.jsonl 等），
# 为兼容既有数据/评测集，content_summary 继续使用旧文件名。
DEFAULT_WORKER = "content_summary"
_LEGACY_FILES = {  # worker -> (raw, train, test)
    "content_summary": ("distill_v2_raw.jsonl", "distill_data_v2.jsonl", "distill_test_v2.jsonl"),
}


def _data_files(worker: str) -> tuple[str, str, str]:
    """返回 (raw, train, test) 文件路径。"""
    if worker in _LEGACY_FILES:
        raw, train, test = _LEGACY_FILES[worker]
    else:
        raw, train, test = (
            f"distill_{worker}_raw.jsonl",
            f"distill_{worker}_data.jsonl",
            f"distill_{worker}_test.jsonl",
        )
    return (
        os.path.join(OUT_DIR, raw),
        os.path.join(OUT_DIR, train),
        os.path.join(OUT_DIR, test),
    )

TARGET_TOTAL = int(os.environ.get("DISTILL_TARGET", "120"))
TEST_SIZE = 20
MAX_WORKERS = 8

# 教师标注 System Prompt：严格 JSON + 来源纪律 + 图表规格
TEACHER_SYSTEM = """你是专业内容总结师兼数据提取器。根据用户指令生成金融/行业研究报告总结。

严格按以下 JSON 输出，不要输出任何其他内容：
{
  "summary": "Markdown 格式的总结正文",
  "charts": [图表规格数组],
  "sources": [来源声明数组]
}

【summary 要求】高质量的 Markdown：总体摘要、关键发现、数据要点（Markdown 表格）、建议。
【charts 要求】数组元素：{"question":"调研问题","conclusion":"一句话结论","type":"bar|line|horizontal_bar|pie|scatter","title":"指标+时间+地域+单位","x_axis_title":"...","y_axis_title":"...（单位）","unit":"单位","section_hint":"章节名","time_range":"年份","region":"地域","source":"数据来源","data":[{"label":"口径/来源","value":1500,"year":2025,"caliber":"口径","source":"https://..."}]}
  规则：①时间序列→line，类别≤10→bar，占比且≤5类→pie；②至少 2 个可对比数据点；③数值与来源完全一致保留小数；④无可靠数值→[]。
【sources 要求】数组元素：{"claim":"正文中的数据声明/来源标注","source_type":"官方财报|权威媒体|行业报告|论坛讨论|模型知识|无法核实","detail":"具体来源描述（网站/报告名/公司名）"}
  纪律（极其重要）：
  ① 每个数据点/声明必须给出来源；无法核实的数据标注 source_type="模型知识"，detail 写"基于模型知识，未在本次检索中验证"；
  ② 严禁把'新闻门户单篇报道、股票论坛个股讨论'等非权威来源标注为权威；如实标注 source_type；
  ③ 来源标注必须诚实，宁可标"无法核实"也不编造权威来源。
只输出 JSON。"""

# 多样化查询生成模板（覆盖 场景 × 风格 × 边界）
QUERY_PROMPTS = [
    # A1: 公司财务分析
    "生成8条公司财务分析类指令，覆盖：营收/净利润/毛利率/现金流/资产负债/业务结构，"
    "公司包括但不限于：腾讯、阿里巴巴、贵州茅台、宁德时代、比亚迪、苹果、特斯拉、微软。"
    "风格多样：正式研究、快速要点、带'给出数据来源'、带'附数据时效与免责声明'。每条一行。",
    # A2: 市场排行/统计
    "生成8条市场统计排行类指令，覆盖：A股成交额前N/前5%占比、涨幅榜、板块资金流、"
    "美股/港股排行、加密货币市值排名。部分指令强调'说明计算口径'、'给出数据截至时间'。每条一行。",
    # A3: 行业研究
    "生成8条行业研究类指令，覆盖：AI芯片、新能源汽车、光伏、半导体、云计算、"
    "智能手机、储能、医疗。要求'调研市场规模及竞争格局'、'给出主要厂商份额'。每条一行。",
    # A4: 宏观经济
    "生成8条宏观经济分析类指令，覆盖：GDP、CPI、利率、失业率、PMI、货币政策，"
    "要求'分析趋势'、'对比历史'、'给出数据来源'。每条一行。",
    # A5: 对比分析（多实体）
    "生成8条对比分析类指令，格式如'对比A与B的XX指标'，覆盖：宁德时代vs比亚迪、"
    "苹果vs微软、阿里vs腾讯、华为vs小米等组合，要求'给出数据来源'。每条一行。",
    # A6: 边界与含糊表达
    "生成8条边界情况指令：非常简短（10字内）、含糊表述（'帮我看看那个股票'）、"
    "混合多问题（一个指令包含两个请求）、带具体年份数字。每条一行。",
]

NUM_ROUNDS = 3

_write_lock = threading.Lock()


def call_teacher(messages, temperature=0.1, max_tokens=4096) -> str:
    payload = json.dumps({
        "model": TEACHER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ZHIPU_URL, data=payload,
        headers={
            "Authorization": f"Bearer {ZHIPU_KEY}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as exc:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def generate_queries(prompt: str) -> list[str]:
    """多样化查询生成（temperature 0.9，标准 4.1）。"""
    raw = call_teacher(
        [{"role": "user", "content": prompt}], temperature=0.9, max_tokens=2000,
    )
    queries = []
    for line in str(raw or "").splitlines():
        q = line.strip()
        q = re.sub(r"^[\d]+[.、)\]]\s*", "", q)
        q = q.strip('"').strip('\u201c').strip('\u201d').strip()
        if len(q) > 5:
            queries.append(q)
    return queries


def _extract_json(text: str) -> dict | None:
    """容错解析：统一走 common.extract_json_object，仅接受对象形态。"""
    result = extract_json_object(text)
    return result if isinstance(result, dict) else None


# 金融/行业研究领域信号词：查询不命中任何信号 → 视为离题噪声丢弃
_FINANCE_SIGNALS = (
    "公司", "股票", "股价", "行情", "年报", "财报", "营收", "净利润", "利润",
    "毛利率", "现金流", "市值", "成交额", "成交量", "涨幅", "跌幅", "排行",
    "占比", "份额", "市场", "行业", "产业", "规模", "趋势", "增长", "增速",
    "宏观", "GDP", "CPI", "利率", "失业率", "通胀", "美联储", "汇率",
    "基金", "债券", "银行", "证券", "投研", "研报", "分析", "对比", "调研",
    "A股", "港股", "美股", "纳斯达克", "比特币", "加密货币", "板块",
)


def _relevant_to_finance(query: str) -> bool:
    """领域相关性校验：离题查询（如'旧款手机换电池'）不进入训练集。"""
    q = str(query or "")
    return any(k in q for k in _FINANCE_SIGNALS)


def validate_label(label: dict) -> bool:
    """schema 质量过滤（标准 4.1）：字段完整 + 类型合法 + 来源纪律。"""
    if not isinstance(label, dict):
        return False
    s = label.get("summary")
    if not isinstance(s, str) or len(s.strip()) < 50:
        return False
    charts = label.get("charts")
    if not isinstance(charts, list):
        return False
    sources = label.get("sources")
    if not isinstance(sources, list):
        return False
    # 来源纪律校验：至少 1 条来源声明；source_type 合法
    VALID_TYPES = {"官方财报", "权威媒体", "行业报告", "论坛讨论", "模型知识", "无法核实"}
    if not sources:
        return False
    for src in sources:
        if not isinstance(src, dict):
            return False
        st = src.get("source_type")
        if st not in VALID_TYPES:
            return False
        if not str(src.get("claim") or "").strip():
            return False
    # 图表规格校验：有 charts 时元素字段合法
    for c in charts:
        if not isinstance(c, dict):
            return False
        if c.get("type") not in ("bar", "line", "horizontal_bar", "pie", "scatter"):
            return False
        data = c.get("data")
        if not isinstance(data, list) or len(data) < 2:
            return False
    return True


def label_one(query: str, stats: dict, print_lock: threading.Lock,
              worker: str = DEFAULT_WORKER) -> bool:
    if not _relevant_to_finance(query):
        with print_lock:
            stats["off_topic"] += 1
        return False
    try:
        raw = call_teacher([
            {"role": "system", "content": TEACHER_SYSTEM},
            {"role": "user", "content": query},
        ], temperature=0.1, max_tokens=4096)
        parsed = _extract_json(raw)
        if not validate_label(parsed):
            with print_lock:
                stats["schema_fail"] += 1
            return False
        sample = {
            "instruction": f"指令: {query}\n\n请根据指令生成总结内容并提取图表数据。",
            "summary": parsed["summary"],
            "charts": parsed["charts"],
            "sources": parsed["sources"],
        }
        with _write_lock:
            raw_file, _, _ = _data_files(worker)
            with open(raw_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        with print_lock:
            stats["ok"] += 1
            print(f"  ✓ [{stats['ok']}] {query[:30]}... charts={len(parsed['charts'])} sources={len(parsed['sources'])}", flush=True)
        return True
    except Exception as exc:
        with print_lock:
            stats["err"] += 1
        return False


def finalize(worker: str = DEFAULT_WORKER):
    """去重、拆分 train/test（按 worker 分开存储）。"""
    raw_file, train_file, test_file = _data_files(worker)
    samples = []
    seen = set()
    if os.path.exists(raw_file):
        with open(raw_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                    key = str(s.get("instruction") or "")[:60]
                    if key not in seen:
                        seen.add(key)
                        samples.append(s)
                except Exception:
                    continue
    random.seed(42)
    random.shuffle(samples)
    test_size = min(TEST_SIZE, max(int(len(samples) * 0.15), 10))
    train, test = samples[test_size:], samples[:test_size]
    with open(train_file, "w", encoding="utf-8") as f:
        for s in train:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(test_file, "w", encoding="utf-8") as f:
        for s in test:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    # 统计来源类型分布
    types = {}
    multi = 0
    for s in samples:
        for src in s.get("sources") or []:
            t = src.get("source_type", "?")
            types[t] = types.get(t, 0) + 1
        if len(s.get("charts") or []) > 0:
            multi += 1
    print(f"\n去重后: {len(samples)} 条 | 训练: {len(train)} | 测试: {len(test)}")
    print(f"含图表样本: {multi}/{len(samples)}")
    print("来源类型分布:", dict(sorted(types.items(), key=lambda x: -x[1])))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", default=DEFAULT_WORKER,
                    help="目标 Worker 名（默认 content_summary，按 worker 分文件）")
    args = ap.parse_args()
    worker = args.worker
    raw_file, _, _ = _data_files(worker)

    stats = {"ok": 0, "schema_fail": 0, "off_topic": 0, "err": 0}
    print_lock = threading.Lock()
    existing = 0
    if os.path.exists(raw_file):
        existing = sum(1 for l in open(raw_file, encoding="utf-8") if l.strip())
    print(f"Worker: {worker} | 已有: {existing} | 目标: {TARGET_TOTAL}")

    for rnd in range(NUM_ROUNDS):
        if existing >= TARGET_TOTAL:
            break
        print(f"\nRound {rnd + 1}: 并行生成查询...")
        all_queries = []
        with ThreadPoolExecutor(max_workers=min(len(QUERY_PROMPTS), 6)) as pool:
            futures = {pool.submit(generate_queries, p): i for i, p in enumerate(QUERY_PROMPTS)}
            for fut in as_completed(futures):
                try:
                    qs = fut.result()
                    all_queries.extend(qs)
                    print(f"  Prompt {futures[fut] + 1}: {len(qs)} 条", flush=True)
                except Exception as e:
                    print(f"  Prompt 失败: {str(e)[:80]}")
        random.shuffle(all_queries)
        print(f"  待标注: {len(all_queries)}")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = [pool.submit(label_one, q, stats, print_lock, worker)
                    for q in all_queries]
            for fut in as_completed(futs):
                fut.result()
        existing = sum(1 for l in open(raw_file, encoding="utf-8") if l.strip())
        print(f"Round {rnd + 1} 结束 | 有效: {stats['ok']} | schema 过滤: {stats['schema_fail']}")

    finalize(worker)
    print(f"\n完成: ok={stats['ok']} schema_fail={stats['schema_fail']} off_topic={stats['off_topic']} err={stats['err']}")


if __name__ == "__main__":
    main()
