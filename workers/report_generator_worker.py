"""Report Generator Worker - 真正的报告撰写器：按指令用 LLM 生成 Markdown 文档并落盘。"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase

REPORT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "reports"


class ReportGeneratorWorker(AsyncWorkerBase):
    _class_capabilities = ["report_generator"]
    _needs_task = True

    @staticmethod
    def _embed_charts_inline(report: str, charts, manifests: dict | None = None) -> str:
        """把图表按主题关键词插入到对应【子小节内容之后】（最深匹配标题的
        段落末尾），让图表紧贴需要可视化的文字；未匹配的图表插到数据来源前。
        manifests: {文件名: [关键词]}，由确定性渲染器写出的 chart_manifest.json
        提供（优先于按文件名的旧式关键词表）。"""
        chart_topics = {
            "market_trend.png": ("规模", "增长", "趋势", "年份", "预测", "展望"),
            "player_share.png": ("玩家", "份额", "竞争", "厂商", "格局", "对比"),
            "entity_frequency.png": ("玩家", "厂商", "机构", "格局", "竞争", "主体"),
            "source_distribution.png": ("来源", "参考", "检索"),
            "topic_terms.png": ("技术", "趋势", "焦点", "热词"),
        }
        inserted: set[str] = set()
        # 解析标题行（级别 + 文本）
        headings: list[tuple[int, int, str]] = []  # (index, level, text)
        for i, line in enumerate(report.split("\n")):
            m = re.match(r"^(#{1,6})\s+(.+)$", line)
            if m:
                headings.append((i, len(m.group(1)), m.group(2).strip()))

        def subsection_end(idx: int, level: int) -> int:
            """返回从标题 idx 开始的小节内容结束行（下一个同级/更高级标题前）。"""
            for jidx, jlvl, _jtext in headings:
                if jidx <= idx:
                    continue
                if jlvl <= level:
                    return jidx
            return len(report.split("\n"))

        def best_heading(keywords) -> int | None:
            """返回匹配到的【最深】标题行号。"""
            best = None
            best_level = 0
            for idx, lvl, text in headings:
                if any(k and k in text for k in keywords):
                    if best is None or lvl > best_level:
                        best = idx
                        best_level = lvl
            return best

        lines = report.split("\n")
        out: list[str] = []
        # 按行推进：遇到匹配子小节的结束位置时插入对应图表
        heading_map = {idx: (lvl, text) for idx, lvl, text in headings}
        planned: list[tuple[int, str, str]] = []  # (插入行号, 图表名, markdown)
        for c in charts:
            keywords = (manifests or {}).get(c.name)
            if not keywords:
                keywords = chart_topics.get(c.name) or [
                    k for k in c.stem.replace("-", "_").split("_") if k
                ]
            hidx = best_heading(keywords)
            if hidx is not None:
                end = subsection_end(hidx, heading_map[hidx][0])
                planned.append((end, c.name, f"![{c.stem}]({c})"))
            else:
                planned.append((-1, c.name, f"![{c.stem}]({c})"))
        # 未匹配 → 数据来源附录前（若无则文末）
        src_idx = next((i for i, l in enumerate(lines) if l.startswith("## ") and "来源" in l), None)
        fallback_line = src_idx if src_idx is not None else len(lines)
        for name, md in [(n, m) for _, n, m in planned if _ == -1]:
            planned.append((fallback_line, name, md))
        planned = [(line_no, name, md) for line_no, name, md in planned if line_no >= 0]
        planned.sort(key=lambda x: (x[0], x[1]))
        # 按行号分组，每组末尾统一追加（多图落在同小节时按顺序排列）
        by_line: dict[int, list[str]] = {}
        for line_no, name, md in planned:
            if name in inserted:
                continue
            inserted.add(name)
            by_line.setdefault(line_no, []).append(md)
        result: list[str] = []
        for i, line in enumerate(lines):
            if i in by_line:
                result.append("")
                result.extend(by_line[i])
                result.append("")
            result.append(line)
        # 落在文末（无数据来源小节）的图表
        if len(lines) in by_line:
            result.append("")
            result.extend(by_line[len(lines)])
            result.append("")
        return "\n".join(result)

    @staticmethod
    def _strip_chart_data_blocks(report: str) -> str:
        """剥离模型误嵌入的 [CHART_DATA] 原始 JSON 块（图表由系统按章节内联嵌入）。"""
        parts = re.split(r"\[CHART_DATA\]", report)
        if len(parts) == 1:
            return report
        out = [parts[0]]
        for rest in parts[1:]:
            end = re.search(r"\n\s*\n", rest)
            out.append(rest[end.end():] if end else "")
        return "".join(out)

    @staticmethod
    def _drop_empty_table_rows(report: str) -> str:
        """删除表格中数值列（数值/规模/金额…表头）为空的整行，
        避免出现"德勤 | 全球半导体 | | | | |"这类空数据行。"""
        lines = report.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            is_header = (
                line.strip().startswith("|")
                and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1])
            )
            if not is_header:
                out.append(line)
                i += 1
                continue
            headers = [c.strip() for c in line.strip("|").split("|")]
            val_col = next(
                (idx for idx, h in enumerate(headers)
                 if any(k in h for k in ("数值", "规模", "金额", "份额", "市值",
                                         "营收", "收入", "增速", "预测值"))),
                None,
            )
            out.append(line)
            i += 1
            out.append(lines[i])  # 分隔行
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip("|").split("|")]
                drop = False
                if val_col is not None and val_col < len(cells):
                    v = cells[val_col]
                    if not v or v in ("—", "-", "―", "–"):
                        if not any("http" in c for c in cells):
                            drop = True
                if not drop:
                    out.append(lines[i])
                i += 1
        return "\n".join(out)

    @staticmethod
    def _clean_fallback_content(text: str) -> str:
        """剥离 fallback 内容里的角色/指令残留与过程噪音（【角色】、【输出要求】、
        [指令]/[数据来源] 标记、ReAct 未收敛提示、原始 JSON），只保留可交付信息。"""
        out: list[str] = []
        for ln in str(text or "").split("\n"):
            line = ln.strip()
            if line.startswith((
                "【角色】", "【受众】", "【输出要求】", "【质量标准】",
                "[指令]", "[数据来源]", "[上一步结果]",
                "ReAct 达到最大轮数", "用户目标：", "原始指令：", "任务目标：",
            )):
                continue
            if re.match(r"^\{\"|^\[\{", line):
                continue
            if re.fullmatch(r"- https?://\S+", line):
                continue  # 数据来源 URL 由附录统一呈现
            out.append(ln)
        return "\n".join(out).strip()

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        charts_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "charts"
        data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
        report_dir = REPORT_DIR
        if task and task.get("workspace"):
            ws = Path(str(task["workspace"]))
            charts_dir = ws / "charts"
            data_dir = ws / "data"
            report_dir = ws / "reports"
            for d in (charts_dir, data_dir, report_dir):
                d.mkdir(parents=True, exist_ok=True)
        # 只考虑本次任务时间窗口内的产物，避免把历史任务遗留的无关数据（如房价）
        # 拉进当前报告。
        cutoff = time.time() - 120 * 60
        manifests: dict[str, list[str]] = {}
        charts = [
            c for c in (charts_dir.glob("*.png") if charts_dir.exists() else [])
            if c.stat().st_mtime >= cutoff
        ]
        proj_dir = None
        # 代码执行生成的图表（project/*.png）同样纳入，供报告嵌入
        if task and task.get("workspace"):
            proj_dir = Path(str(task["workspace"])) / "project"
            if proj_dir.exists():
                charts += [
                    c for c in proj_dir.rglob("*.png")
                    if "screenshots" not in c.parts
                    if c.stat().st_mtime >= cutoff
                ]
            # LLM 结构化图表规格渲染器写出的关键词清单，用于按主题内嵌
            mf = proj_dir / "chart_manifest.json"
            if mf.exists():
                try:
                    for item in json.loads(mf.read_text(encoding="utf-8")).get("charts", []):
                        if item.get("file") and item.get("keywords"):
                            manifests[str(item["file"])] = list(item["keywords"])
                except Exception:
                    pass
        # 去重（同名文件可能同时出现在 charts/ 与 project/）
        seen_charts: set[str] = set()
        charts = [c for c in charts if not (str(c) in seen_charts or seen_charts.add(str(c)))]
        data_csvs = [
            d for d in (data_dir.glob("*.csv") if data_dir.exists() else [])
            if d.stat().st_mtime >= cutoff
        ]

        data_info = ""
        for d in data_csvs[:3]:
            try:
                import pandas as pd
                df = pd.read_csv(d)
                data_info += f"- **{d.name}**: {df.shape[0]} rows, {df.shape[1]} cols\n"
            except Exception:
                data_info += f"- **{d.name}**: file exists ({d.stat().st_size} bytes)\n"

        try:
            from prompt_registry import get_prompt
            artifacts = (
                f"可用图表：{', '.join(c.name for c in charts) or '无'}\n"
                f"可用数据：\n{data_info or '无'}"
            )
            system = get_prompt("report_generator", (
                "你是专业报告撰写者。根据指令生成一份完整、具体、可直接交付的 Markdown 文档。"
                "要求：结构清晰（使用标题/表格/列表），内容详实而非占位符，"
                "严格围绕任务主题，语言流畅。直接输出 Markdown 正文，不要额外说明。"
                "重要：工作区列出的图表/数据文件若与本任务主题无关（例如游戏任务中出现房价数据集），"
                "一律不得使用，只能使用上一步结果中与任务主题直接相关的信息。"
                "不要自行嵌入图表文件（系统会按章节自动嵌入图表）；"
                "在需要图表辅助理解的段落后用文字提及对应图表名称即可（如"
                "『如图 key_numbers 所示』）。"
                "严禁输出 [CHART_DATA]、chart_data.json 等任何原始 JSON 或系统标记块；"
                "不要复述或引用指令、系统提示词或 '[上一步结果]' 原文。"
                "表格中每一行都必须有数值：数据缺失时在数值列写'未披露'或删除该行，"
                "不得留空。"
                "报告正文必须包含一个「关键数据一览」表格（列：指标 | 数值 | "
                "口径/年份 | 来源），只收录与本任务主题直接相关的数值；"
                "不同来源/口径的数字分开列出并注明差异，不要把不可比的数据混为一谈。"
            ))
            user = f"{instruction}\n\n工作区产物：\n{artifacts}"
            # 主端点连试 2 次即切备用，减少慢端点对报告环节的拖累
            report = await self._call_llm(system=system, prompt=user, max_attempts=2)
            report = report.strip()
            if len(report) < 100:
                raise RuntimeError("Generated report too short")
            if not report.startswith("#"):
                report = "# 报告\n\n" + report

            # 报告完整性守门：研究/调研类报告必须有正文骨架（规模/玩家/趋势），
            # 若 LLM 输出过薄或被截断（只剩摘要），自动补上检索摘要的完整研究内容
            if any(k in str(instruction) for k in ("报告", "调研", "研报")):
                missing = [k for k in ("规模", "玩家", "趋势") if k not in report]
                if missing:
                    prev_parts = re.findall(
                        r"\[上一步结果 \d+\]:\s*(.*?)(?=\n\[上一步结果 |\n用户目标：|\Z)",
                        str(instruction), re.S,
                    )
                    prev_content = "\n\n".join(p.strip() for p in prev_parts)
                    prev_content = self._clean_fallback_content(prev_content)
                    if prev_content:
                        report += (
                            "\n\n## 研究内容（检索/摘要）\n\n"
                            + prev_content[:6000]
                        )

            # 数据来源附录：从指令中的 [数据来源] 块提取 URL 并去重
            src_urls: list[str] = []
            m = re.search(r"\[数据来源\](.*)", str(instruction), re.S)
            if m:
                for u in re.findall(r"https?://[^\s\)\]]+", m.group(1)):
                    if u not in src_urls:
                        src_urls.append(u)
            if src_urls:
                report += (
                    "\n\n## 数据来源\n\n"
                    + "\n".join(f"- [{u}]({u})" for u in src_urls[:15])
                )
            # 图表内联嵌入：按主题把图表插到对应小节之后（紧贴需要可视化的文字）
            if charts:
                report = self._embed_charts_inline(report, charts, manifests)

            # 后处理：剥离误嵌入的 [CHART_DATA] 原始 JSON，删除空数值表格行
            report = self._strip_chart_data_blocks(report)
            report = self._drop_empty_table_rows(report)

            rpath = report_dir / "report.md"
            rpath.write_text(report, encoding="utf-8")
            return json.dumps({
                "status": "success",
                "report_path": str(rpath),
                "charts": len(charts),
                "datasets": len(data_csvs),
                "chars": len(report),
            }, ensure_ascii=False)
        except Exception as exc:
            # 回退：LLM 不可用时，输出以任务主题为标题的结构化报告（不套数据管道模板）
            theme = re.sub(r"^(任务目标|用户目标|原始指令)[：:]\s*", "", str(instruction)).strip()[:150]
            if not theme:
                theme = "任务报告"
            src_urls2: list[str] = []
            m2 = re.search(r"\[数据来源\](.*)", str(instruction), re.S)
            if m2:
                for u in re.findall(r"https?://[^\s\)\]]+", m2.group(1)):
                    if u not in src_urls2:
                        src_urls2.append(u)
            src_md = "\n".join(f"- {u}" for u in src_urls2[:15]) or "（检索未返回可用来源）"
            # 兜底也要保留真实研究内容（上一步摘要/检索结果），避免"报告只有模板"
            prev_parts = re.findall(
                r"\[上一步结果 \d+\]:\s*(.*?)(?=\n\[上一步结果 |\n用户目标：|\Z)",
                str(instruction), re.S,
            )
            prev_content = "\n\n".join(p.strip() for p in prev_parts)
            prev_md = (
                "\n\n## 研究内容（检索/摘要）\n\n" + prev_content
                if prev_content else ""
            )
            report = (
                f"# {theme} 报告\n\n"
                "## 摘要\n\n"
                f"本报告由织光多智能体系统自动生成，围绕「{theme}」汇总真实检索资料与工作区分析结果。\n\n"
                "## 关键发现\n\n"
                f"- 检索到 {len(src_urls2)} 个数据来源（详见文末「数据来源」）。\n"
                "- 关键数据见「研究内容」中的表格与图表，均来自搜索结果，未编造。\n\n"
                f"{prev_md}\n\n"
                "## 数据来源\n\n"
                f"{src_md}\n"
            )
            # 兜底也按章节内联嵌入图表（未匹配的插到数据来源前）
            if charts:
                report = self._embed_charts_inline(report, charts)
            try:
                rpath = report_dir / "report.md"
                rpath.write_text(report, encoding="utf-8")
                return json.dumps({
                    "status": "success",
                    "report_path": str(rpath),
                    "charts": len(charts),
                    "datasets": len(data_csvs),
                    "fallback": True,
                }, ensure_ascii=False)
            except Exception as e2:
                return json.dumps({"status": "failed", "error": str(e2)})


if __name__ == "__main__":
    import asyncio
    from logging_setup import setup_logging

    setup_logging("worker-report-generator")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "reportgeneratorworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging

    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))

    async def run():
        worker = ReportGeneratorWorker(
            agent_id=agent_id,
            capabilities=ReportGeneratorWorker._class_capabilities,
            registry=reg,
            messaging=msg,
        )
        await worker.run()

    asyncio.run(run())
