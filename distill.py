# -*- coding: utf-8 -*-
"""数据蒸馏：用智谱 GLM 教师 API 为 content_summary Worker 生成训练对。

产出：distill_data.jsonl，每行 {instruction, summary, charts}——
  instruction: content_summary 步骤指令（含目标+上下文）
  summary:     教师生成的 Markdown 总结
  charts:      教师生成的结构化图表规格（JSON）

用于 QLoRA 微调 Qwen2.5-7B-Instruct 替换 content_summary 的 LLM 调用。
"""
import json
import os
import sys
import time
import urllib.request

ZHIPU_KEY = "debebbe5fcab4ff89e3ca04b3d6be6b0.haVMzLiyS6S1twvY"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-flash"

TEACHER_SYSTEM = """你是专业内容总结师兼数据提取器。根据指令对内容进行总结、提炼或生成报告。
输出严格 JSON：{"summary": "Markdown 总结正文", "charts": [图表规格数组]}。

summary 要求：高质量的 Markdown 格式；如果是生成最终报告，要包含总体摘要、关键发现、数据要点、建议。
charts 要求：{"question":"调研问题","conclusion":"一句话结论","type":"bar|line|horizontal_bar|pie|scatter","title":"指标+时间+地域+单位","x_axis_title":"...","y_axis_title":"...（单位）","unit":"亿美元","section_hint":"建议插入的章节名","time_range":"2025年","region":"全球","source":"数据来源","sample_size":"5","annotation":"结论注释","missing":"缺失说明","outliers":"异常说明","data":[{"label":"口径/来源","value":1500,"year":2025,"caliber":"德勤预测","source":"https://example.com"}]}
规则：①每张图先给 question 和 conclusion，无法得出结论就跳过该图；②时间序列→line，类别≤10→bar，占比且≤5类→pie；③至少 2 个可对比数据点，禁止单点图；④数值必须与来源完全一致保留小数；⑤没有可靠数值就输出 {"summary": "...", "charts": []}。只输出 JSON。"""

# 蒸馏任务集（覆盖金融/行业/宏观/公司分析等场景）
TASKS = [
    "分析腾讯控股2025年报：营收、净利润、业务结构，附数据时效与免责声明",
    "统计A股前5%成交额占比并列出头部股票，说明计算口径和数据截至时间",
    "对比宁德时代与比亚迪近三年营收和净利润趋势，给出数据来源",
    "分析苹果公司最新财报的营收、毛利率和现金流趋势",
    "调研2025年全球AI芯片市场规模及主要厂商竞争格局",
    "分析中国新能源汽车2025年销量趋势与市场份额",
    "研究美联储2025年利率政策对A股的影响",
    "分析贵州茅台近五年营收和净利润变化趋势",
    "调研2025年全球智能手机出货量及华为/小米/苹果份额",
    "分析中国GDP增速与CPI变化的关系",
    "研究光伏行业2025年产能过剩与价格趋势",
    "分析比亚迪2024年财报的营收、利润和现金流",
    "调研2025年全球云服务市场格局（AWS/微软/阿里/华为）",
    "分析特斯拉2024年财报的毛利率与交付量",
    "研究2025年中国人口结构与消费趋势",
]


def call_zhipu(system: str, user: str, max_tokens: int = 4096) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
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


def _extract_json(text: str) -> dict | None:
    """容错解析：剥离 markdown 围栏，找首个 JSON 对象。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t
        t = t.strip().lstrip("json").strip()
    i = t.find("{")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(t)):
        if t[j] == "{":
            depth += 1
        elif t[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    return None
    return None


def distill(out_path: str = "distill_data.jsonl", limit: int = None) -> int:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    written = 0
    for idx, task in enumerate(TASKS):
        if limit and written >= limit:
            break
        # 注入少量"上下文"模拟真实 worker 指令（含结构化数据提示）
        user = f"指令: {task}\n\n请根据指令生成总结内容并提取图表数据。"
        try:
            raw = call_zhipu(TEACHER_SYSTEM, user, max_tokens=4096)
        except Exception as exc:
            print(f"[{idx}] 失败: {task[:20]}... {str(exc)[:80]}")
            continue
        parsed = _extract_json(raw)
        if not parsed:
            print(f"[{idx}] JSON 解析失败: {task[:20]}... raw={raw[:100]}")
            continue
        record = {
            "instruction": user,
            "summary": str(parsed.get("summary") or ""),
            "charts": parsed.get("charts") or [],
            "teacher": MODEL,
            "task": task,
        }
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        written += 1
        print(f"[{idx}] OK ({written}) {task[:24]}... charts={len(record['charts'])}")
        time.sleep(1)  # 限速保护
    print(f"完成: {written} 条 -> {out_path}")
    return written


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    distill(limit=limit)
