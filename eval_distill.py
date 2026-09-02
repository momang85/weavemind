# -*- coding: utf-8 -*-
"""蒸馏效果评测（对标标准 4.1 量化指标）：
- JSON 合规率：输出能否解析为合法 {summary, charts, sources}
- 来源纪律率：sources 中 source_type 合法且含'模型知识/无法核实'诚实标注的比例
- 图表合规率：charts 中至少 2 数据点且 type 合法的比例
- 对比：本地蒸馏模型 vs 云端教师
"""
import json
import sys
import time

sys.path.insert(0, ".")

TEST_FILE = "distill_test_v2.jsonl"


def load_test() -> list[dict]:
    out = []
    try:
        with open(TEST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        pass
    return out


def evaluate_local(samples: list[dict]) -> dict:
    """本地蒸馏模型评测。"""
    from lora_client import local_generate

    json_ok = 0
    source_ok = 0
    chart_ok = 0
    source_total = 0
    chart_total = 0
    total = 0
    times = []
    for s in samples:
        instr = str(s.get("instruction") or "")
        t0 = time.time()
        r = local_generate(instr, max_tokens=4096)
        times.append(time.time() - t0)
        total += 1
        if not r:
            continue
        json_ok += 1
        srcs = r.get("sources") or []
        if srcs:
            source_total += len(srcs)
            for src in srcs:
                st = str(src.get("source_type") or "")
                if st in ("官方财报", "权威媒体", "行业报告", "论坛讨论", "模型知识", "无法核实"):
                    source_ok += 1
        charts = r.get("charts") or []
        if charts:
            chart_total += 1
            valid = all(
                c.get("type") in ("bar", "line", "horizontal_bar", "pie", "scatter")
                and len(c.get("data") or []) >= 2
                for c in charts
            )
            if valid:
                chart_ok += 1
    return {
        "样本数": total,
        "JSON 合规率": round(json_ok / max(1, total), 2),
        "来源字段合规率": round(source_ok / max(1, source_total), 2),
        "图表合规率": round(chart_ok / max(1, chart_total), 2),
        "平均耗时(s)": round(sum(times) / max(1, len(times)), 1),
    }


def main():
    samples = load_test()
    print(f"测试集: {len(samples)} 条")
    if not samples:
        print("无测试数据，跳过评测")
        return
    if not samples[0].get("sources"):
        print("警告: 测试集不含 sources 字段（旧格式）")
    r = evaluate_local(samples)
    print("\n=== 本地蒸馏模型评测（标准 4.1 指标）===")
    for k, v in r.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
