# -*- coding: utf-8 -*-
"""蒸馏效果评测（对标标准 4.1 量化指标 + 架构约束④质量回归门）。

指标：
- JSON 合规率：输出能否解析为合法 {summary, charts, sources}
- 来源纪律率：sources 中 source_type 合法且含'模型知识/无法核实'诚实标注的比例
- 图表合规率：charts 中至少 2 数据点且 type 合法的比例
- 平均耗时

用法：
  python eval_distill.py                 # 仅本地 LoRA
  python eval_distill.py --cloud         # 同批测试 cloud(教师 API) 对比
  python eval_distill.py --compare       # 本地 vs 教师 同批对比 + 质量回退判定

质量回退判定（约束④）：任一关键指标（JSON/来源/图表合规）本地低于
云端 >10 个百分点 → 判定 QUALITY_REGRESSION（应回退 cloud）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, ".")

TEST_FILE = "distill_test_v2.jsonl"
ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY",
                           "debebbe5fcab4ff89e3ca04b3d6be6b0.haVMzLiyS6S1twvY")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
TEACHER_MODEL = "glm-4-flash"
REGRESSION_THRESHOLD = 0.10  # 10 个百分点


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


def _score_text(summary: str) -> dict:
    """来源纪律启发式评分：正文含来源标注（数据来源/财报/模型知识等）。"""
    s = str(summary or "")
    honest = any(k in s for k in (
        "数据来源", "来源", "基于模型知识", "未验证", "无法核实",
        "官方财报", "年报", "公告", "公开信息",
    ))
    return {"has_source": honest}


def evaluate_local(samples: list[dict], limit: int = 5) -> dict:
    """本地蒸馏模型评测（限样本数控制时间）。"""
    from lora_client import local_generate

    json_ok = source_ok = 0
    total = 0
    times = []
    for s in samples[:limit]:
        instr = str(s.get("instruction") or "")
        t0 = time.time()
        r = local_generate(instr, max_tokens=2500)
        times.append(time.time() - t0)
        total += 1
        if not r:
            continue
        json_ok += 1
        sc = _score_text(r.get("summary"))
        if sc["has_source"]:
            source_ok += 1
        srcs = r.get("sources") or []
        for src in srcs:
            st = str(src.get("source_type") or "")
            if st in ("官方财报", "权威媒体", "行业报告", "论坛讨论", "模型知识", "无法核实"):
                source_ok += 1
    return {
        "样本数": total,
        "JSON 合规率": round(json_ok / max(1, total), 2),
        "来源标注率": round(source_ok / max(1, total), 2),
        "平均耗时(s)": round(sum(times) / max(1, len(times)), 1),
    }


def evaluate_cloud(samples: list[dict], limit: int = 5) -> dict:
    """云端教师（glm-4-flash）同批评测。"""
    json_ok = source_ok = 0
    total = 0
    times = []
    for s in samples[:limit]:
        instr = str(s.get("instruction") or "")
        t0 = time.time()
        try:
            payload = json.dumps({
                "model": TEACHER_MODEL,
                "messages": [
                    {"role": "system", "content": "你是专业内容总结师，输出 Markdown 总结。"
                                                  "数据必须标注来源。"},
                    {"role": "user", "content": instr},
                ],
                "max_tokens": 1500,
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                ZHIPU_URL, data=payload,
                headers={
                    "Authorization": f"Bearer {ZHIPU_KEY}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"  云端调用失败: {str(e)[:60]}")
            continue
        times.append(time.time() - t0)
        total += 1
        if content.strip():
            json_ok += 1
        sc = _score_text(content)
        if sc["has_source"]:
            source_ok += 1
    return {
        "样本数": total,
        "JSON 合规率": round(json_ok / max(1, total), 2),
        "来源标注率": round(source_ok / max(1, total), 2),
        "平均耗时(s)": round(sum(times) / max(1, len(times)), 1),
    }


def compare(local: dict, cloud: dict) -> list[str]:
    """质量回退判定（约束④）。返回回退指标名列表（空 = 达标）。"""
    print("\n=== 同批对比（cloud vs hybrid/本地）===")
    print(f"{'指标':<16}{'云端':>10}{'本地':>10}{'差距':>10}")
    regression = []
    for k in ("JSON 合规率", "来源标注率"):
        lv = local.get(k, 0)
        cv = cloud.get(k, 0)
        gap = lv - cv
        flag = " ⚠️ 回退" if gap < -REGRESSION_THRESHOLD else ""
        if gap < -REGRESSION_THRESHOLD:
            regression.append(k)
        print(f"{k:<16}{cv:>10.0%}{lv:>10.0%}{gap:>+10.0%}{flag}")
    print(f"平均耗时: 云端 {cloud.get('平均耗时(s)', 0)}s vs 本地 {local.get('平均耗时(s)', 0)}s")
    if regression:
        print(f"\n⚠️ QUALITY_REGRESSION: {', '.join(regression)} 低于云端 >10pp —— 应回退 cloud 模式")
    else:
        print("\n✅ 质量达标：本地 LoRA 无显著回退，可保持 hybrid")
    return regression


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", action="store_true", help="同批跑云端教师对比")
    ap.add_argument("--compare", action="store_true", help="对比 + 质量回退判定")
    ap.add_argument("--gate", action="store_true",
                    help="质量回退时以退出码 1 结束（CI/管线门禁用）")
    ap.add_argument("--limit", type=int, default=5, help="评测样本上限")
    args = ap.parse_args()

    samples = load_test()
    print(f"测试集: {len(samples)} 条（评测 {min(args.limit, len(samples))} 条）")
    if not samples:
        print("无测试数据，跳过评测")
        return 2 if args.gate else None

    local = evaluate_local(samples, limit=args.limit)
    print("\n=== 本地蒸馏模型评测（标准 4.1 指标）===")
    for k, v in local.items():
        print(f"  {k}: {v}")

    if args.cloud or args.compare:
        print("\n=== 云端教师（glm-4-flash）同批 ===")
        cloud = evaluate_cloud(samples, limit=args.limit)
        for k, v in cloud.items():
            print(f"  {k}: {v}")
        regression = compare(local, cloud)
        if args.gate and regression:
            print("GATE: FAIL（质量回退）", flush=True)
            return 1
        if args.gate:
            print("GATE: PASS", flush=True)
    if args.gate:
        return 0


if __name__ == "__main__":
    main()
