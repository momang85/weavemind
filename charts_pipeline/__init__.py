# -*- coding: utf-8 -*-
"""charts_pipeline——图表域编排逻辑（C2 深化拆分）。

从 orchestrator_v2 抽出的图表生成/渲染/同步编排主体。
orchestrator 通过混入 ChartPipelineMixin 保留全部方法签名（测试零改动），
但实现不再藏身 6600 行编排器：图表流程的后续改动只碰本包 + chart_assembly。

依赖约定：本模块不得在顶层 import orchestrator_v2（避免循环导入）；
需要 orchestrator 模块级工具（_sanitized_process_env）时在方法体内延迟导入。
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import chart_assembly

logger = logging.getLogger(__name__)


class ChartPipelineMixin:
    """图表编排混入：_render_clean_chart_data / _render_chart_data /
    _generate_search_charts 三个方法的原样搬迁（行为零变化）。"""

    def _render_clean_chart_data(self, task_id: str, goal: str) -> None:
        """数据驱动兜底（P1-3）：clean_chart_data 中 ≥2 个可作图数据点即渲染；
        若 market_data 行不足 2 条，先从 structured_data.json 补齐 crypto/macro
        行情点再转可作图行。LLM 规格缺失时保证加密/宏观任务仍有图表。"""
        if not self._wants_visualization(goal):
            return
        from workspace import task_project_dir
        project = task_project_dir(task_id)
        clean_path = project / "clean_chart_data.json"
        if not clean_path.exists():
            return
        try:
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
        except Exception:
            return
        row_count = sum(
            len(clean.get(key) or []) for key in (
                "market_data", "market_trends", "macro_indicators", "market_share",
            )
        )
        if row_count < 2:
            # 清洗/回灌可能覆盖了结构化行 → 从 structured_data.json 补齐
            self._remerge_structured_points(task_id)
            try:
                clean = json.loads(clean_path.read_text(encoding="utf-8"))
            except Exception:
                return
        specs = self._filter_chart_specs(self._clean_rows_to_specs(clean), goal)
        if not specs:
            return
        try:
            from chart_specs import validate_specs
            valid, _issues = validate_specs(specs)
        except Exception:
            return
        if not valid:
            return
        chart_path = project / "chart_data.json"
        existing: list[dict] = []
        if chart_path.exists():
            try:
                existing = json.loads(
                    chart_path.read_text(encoding="utf-8")
                ).get("charts") or []
            except Exception:
                existing = []
        seen_titles = {str(s.get("title") or "") for s in existing}
        merged = list(existing) + [
            s for s in valid if str(s.get("title") or "") not in seen_titles
        ]
        chart_path.write_text(
            json.dumps({"charts": merged}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        self._render_chart_data(task_id, goal)

    def _render_chart_data(self, task_id: str, goal: str) -> None:
        """确定性渲染 LLM 结构化图表规格（chart_data.json → {"charts": [...]}）：
        语义（问题/结论/口径）由 LLM 负责；数字、标注、视觉编码由脚本保证。
        无效规格跳过并记录原因；有效图输出 chart_N.png + chart_manifest.json。"""
        import subprocess
        import sys
        if not self._wants_visualization(goal):
            return
        from workspace import task_project_dir
        project = task_project_dir(task_id)
        src = project / "chart_data.json"
        if not src.exists():
            return
        # __file__ 为 charts_pipeline/__init__.py：上溯两级才是仓库根
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = chart_assembly.RENDER_CHART_SCRIPT
        script = script.replace("__REPO_ROOT__", repo_root)
        script_path = project / "render_charts.py"
        try:
            script_path.write_text(script, encoding="utf-8")
            from orchestrator_v2 import _sanitized_process_env
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(project), capture_output=True, timeout=180,
                env=_sanitized_process_env(),
            )
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                logger.warning("render_charts failed: %s", (out + "\n" + err)[:400])
            else:
                for line in out.splitlines():
                    if line.startswith("SKIP "):
                        logger.info("chart skipped: %s", line)
            # P2-4：渲染后回填 manifest——保证每张已渲染 PNG 都有 file+keywords
            # 条目（修复"实际 4 图但 chart_manifest.json 空数组"的交付缺口）
            chart_assembly._backfill_chart_manifest(project)
            # 图表同步到 workspace/charts/（report_generator 从该目录发现图表并嵌入报告）
            try:
                from workspace import task_charts_dir
                cdir = task_charts_dir(task_id)
                for png in project.glob("*.png"):
                    shutil.copy2(png, cdir / png.name)
                mf = project / "chart_manifest.json"
                if mf.exists():
                    shutil.copy2(mf, cdir / mf.name)
            except Exception as exc:
                logger.warning("chart sync failed: %s", str(exc)[:120])
        except Exception as exc:
            logger.warning("render_charts error: %s", exc)

    def _generate_search_charts(self, task_id: str, goal: str) -> None:
        """确定性基线图表：来源分布、主要主体提及频率、主题热词。
        语义类图表（趋势/份额/指标）由 LLM 结构化数据渲染（_render_chart_data）。"""
        import subprocess
        import sys
        import os
        if not self._wants_visualization(goal):
            return
        from workspace import task_project_dir
        project = task_project_dir(task_id)
        # __file__ 为 charts_pipeline/__init__.py：上溯两级才是仓库根
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = project / "search_results.json"
        clean_src = project / "clean_chart_data.json"
        if not src.exists():
            return
        # 数据清洗兜底：绘图只读清洗后数据（搜索→清洗→绘图）
        if not clean_src.exists():
            try:
                from clean_data import clean_file
                clean_file(src, clean_src, goal=goal)
            except Exception as exc:
                logger.warning("clean_data failed: %s", str(exc)[:100])
                return
        script = chart_assembly.MAKE_CHARTS_SCRIPT
        script = script.replace("__REPO_ROOT__", repo_root.replace("\\", "/"))
        script_path = project / "make_charts.py"
        try:
            script_path.write_text(script, encoding="utf-8")
            from orchestrator_v2 import _sanitized_process_env
            proc = subprocess.run(
                [sys.executable, str(script_path), str(goal or "")],
                cwd=str(project), capture_output=True, timeout=120,
                env=_sanitized_process_env(),
            )
            if proc.stdout:
                out = proc.stdout.decode("utf-8", errors="replace").strip()
                if out:
                    logger.info("make_charts(%s): %s", task_id, out[:500])
            if proc.returncode != 0:
                logger.warning("make_charts failed: %s", proc.stderr.decode("utf-8", errors="replace")[:200])
            else:
                # 探索性图表同步到 workspace/charts/，供报告内联嵌入
                try:
                    from workspace import task_charts_dir
                    cdir = task_charts_dir(task_id)
                    for png in project.glob("*.png"):
                        shutil.copy2(png, cdir / png.name)
                except Exception as exc:
                    logger.warning("search-chart sync failed: %s", str(exc)[:120])
                # 语义图已同步到 charts/，回填 chart_manifest.json（chart pipeline
                # 先行回填 chart_N，make_charts 生成的语义图必须在此补齐）
                try:
                    chart_assembly._backfill_chart_manifest(project)
                except Exception as exc:
                    logger.warning("search-chart manifest backfill failed: %s", str(exc)[:120])
        except Exception as exc:
            logger.warning("make_charts error: %s", exc)
