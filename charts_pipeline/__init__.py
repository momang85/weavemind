# -*- coding: utf-8 -*-
"""charts_pipeline——图表域编排逻辑（C2 深化拆分）。

从 orchestrator_v2 抽出的图表生成/渲染/同步编排主体。
orchestrator 通过混入 ChartPipelineMixin 保留全部方法签名（测试零改动），
但实现不再藏身 6600 行编排器：图表流程的后续改动只碰本包 + chart_assembly。

依赖约定：本模块不得在顶层 import orchestrator_v2（避免循环导入）；
需要 orchestrator 模块级工具（_sanitized_process_env）时在方法体内延迟导入。
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import chart_assembly

# 保持与 orchestrator_v2 相同的 logger 名：日志行为（含测试 assertLogs）零变化
logger = logging.getLogger("orchestrator_v2")

# 经营研究三图的规格指纹：选择没变就不重渲染（文件落在 project/ 下）
ANALYSIS_CHART_FP = "analysis_charts_fingerprint.json"


def _rules_version() -> str:
    try:
        from financial_analysis.validation import RULES_VERSION
        return str(RULES_VERSION)
    except Exception:                                # noqa: BLE001
        return ""


def selected_analysis_specs(ws) -> list[dict]:
    """**经营研究三图**（利润瀑布／现金桥／情景比较）的规格——与正文同一组选定运行。

    唯一实现：运行期渲染（`OrchestratorV2._analysis_chart_specs`）与**选择变化后的
    重渲染**（`refresh_selected_analysis_charts`）都走这里，避免两处各写一套导致
    "正文按新选择、图还是旧选择"（同源断裂）。
    数据来源：`analysis/analysis_runs.json` 的已验证运行 + `analysis/dataset.json`
    + `store.select_for_report` 的**同一条选择**；不在这里另跑参数，也不从正文文字反猜数字。
    """
    try:
        from financial_analysis import charts as _charts
        from financial_analysis import store as _fa_store
        picked, _notes = _fa_store.select_for_report(ws, rules_version=_rules_version())
        if not picked:
            return []
        ds = _fa_store.dataset_from_inputs(ws)
        specs: list[dict] = []
        od = next((r for r in picked if str(r.model_id) == "operating_drivers"), None)
        cash = next((r for r in picked if str(r.model_id) == "cash_reconciliation"), None)
        scen = [r for r in picked if str(r.model_id) == "scenario_sensitivity"]
        if od is not None and ds is not None:
            specs.append(_charts.profit_waterfall(od, ds))
        if cash is not None:
            specs.append(_charts.cash_bridge_waterfall(cash, which="cur"))
        if len(scen) >= 2:
            specs.append(_charts.scenario_threshold_comparison(
                [(f"档位{i + 1}", r) for i, r in enumerate(scen)]))
        elif scen:
            specs.append(_charts.scenario_outcome_bars(scen[0]))
        ok = [s for s in specs if s.get("available")]
        if len(ok) != len(specs):
            logger.info("analysis charts: %d/%d 可用（其余如实不出图）", len(ok), len(specs))
        return ok
    except Exception as exc:                         # noqa: BLE001
        logger.warning("经营研究三图规格生成失败：%s", str(exc)[:140])
        return []


def render_chart_data(project, *, task_id: str = "") -> dict:
    """`chart_data.json` → PNG + `chart_manifest.json`（独立可调用，不依赖编排器实例）。

    `project` 是任务的项目目录（渲染脚本的工作目录）。与编排器里那条路**同一段代码**：
    写脚本 → 子进程渲染 → 回填 manifest → 同步到 `workspace/charts/`（报告从那里取图）。
    """
    project = Path(project)
    src = project / "chart_data.json"
    if not src.exists():
        return {"ok": False, "reason": "no_chart_data", "charts": []}
    # __file__ 为 charts_pipeline/__init__.py：上溯两级才是仓库根
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = chart_assembly.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", repo_root)
    script_path = project / "render_charts.py"
    out = err = ""
    code = -1
    try:
        script_path.write_text(script, encoding="utf-8")
        from orchestrator_v2 import _sanitized_process_env
        proc = subprocess.run([sys.executable, str(script_path)], cwd=str(project),
                              capture_output=True, timeout=180,
                              env=_sanitized_process_env())
        code = int(proc.returncode)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        if code != 0:
            logger.warning("render_charts failed: %s", (out + "\n" + err)[:400])
        else:
            for line in out.splitlines():
                if line.startswith("SKIP "):
                    logger.info("chart skipped: %s", line)
        # P2-4：渲染后回填 manifest——保证每张已渲染 PNG 都有 file+keywords 条目
        chart_assembly._backfill_chart_manifest(project)
        if task_id:
            try:
                from workspace import task_charts_dir
                cdir = task_charts_dir(task_id)
                for png in project.glob("*.png"):
                    shutil.copy2(png, cdir / png.name)
                mf = project / "chart_manifest.json"
                if mf.exists():
                    shutil.copy2(mf, cdir / mf.name)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("chart sync failed: %s", str(exc)[:120])
    except Exception as exc:                         # noqa: BLE001
        logger.warning("render_charts error: %s", exc)
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}", "charts": []}
    mf = project / "chart_manifest.json"
    charts: list[dict] = []
    try:
        charts = list(json.loads(mf.read_text(encoding="utf-8")).get("charts") or [])
    except Exception:                                # noqa: BLE001
        charts = []
    return {"ok": code == 0, "returncode": code, "charts": charts}


def refresh_selected_analysis_charts(task_id: str) -> dict:
    """**选择变化后**按当前选定运行重渲染经营研究三图（只动这三张）。

    为什么需要（阶段X §7 实机）：三图在运行期由 content_summary 步渲染一次；此后用户在
    分析工作台改选/采纳情景运行，正文按新选择装配，**图与图注却还是运行期那一版**——
    正文写"使用者情景 91.04 亿"、图 3 仍画"80.33 亿"，同一份交付里两个数（同源断裂）。
    选择没变（规格指纹相同）就不重渲染，不产生无谓写入。
    """
    from workspace import task_project_dir, task_workspace
    try:
        ws, project = task_workspace(task_id), task_project_dir(task_id)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "reason": f"workspace: {str(exc)[:100]}", "charts": []}
    specs = selected_analysis_specs(ws)
    if not specs:
        # 没有可用规格时**不动**既有图（宁可不刷新，也不用空规格把图删了）
        return {"ok": False, "reason": "no_analysis_specs", "charts": []}
    fp = hashlib.sha256(json.dumps(specs, ensure_ascii=False, sort_keys=True,
                                   default=str).encode("utf-8")).hexdigest()
    fp_path = project / ANALYSIS_CHART_FP
    try:
        prev = json.loads(fp_path.read_text(encoding="utf-8")).get("fingerprint") or ""
    except Exception:                                # noqa: BLE001
        prev = ""
    if prev == fp:
        return {"ok": True, "changed": False, "fingerprint": fp, "charts": []}
    try:
        (project / "chart_data.json").write_text(
            json.dumps({"charts": specs}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "reason": f"write: {str(exc)[:100]}", "charts": []}
    info = render_chart_data(project, task_id=str(task_id))
    charts = [c for c in (info.get("charts") or [])
              if str(c.get("file") or "") in {f"chart_{i}.png" for i in (1, 2, 3)}]
    try:
        fp_path.write_text(json.dumps(
            {"fingerprint": fp, "specs": len(specs),
             "chart_ids": [str(c.get("chart_id") or "") for c in charts],
             "values": [dict(c.get("binding") or {}) for c in charts]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                # noqa: BLE001
        pass
    logger.info("分析三图按当前选择重渲染（task=%s）：%d 张（changed=%s）",
                task_id, len(charts), info.get("ok"))
    return {"ok": bool(info.get("ok")), "changed": True, "fingerprint": fp, "charts": charts}


class ChartPipelineMixin:
    """图表编排混入：_render_clean_chart_data / _render_chart_data /
    _generate_search_charts 三个方法的原样搬迁（行为零变化）。"""

    def _render_clean_chart_data(self, task_id: str, goal: str) -> None:
        """数据驱动兜底（P1-3）：clean_chart_data 中 ≥2 个可作图数据点即渲染；
        若 market_data 行不足 2 条，先从 structured_data.json 补齐 crypto/macro
        行情点再转可作图行。LLM 规格缺失时保证加密/宏观任务仍有图表。"""
        if not self._wants_visualization(goal):
            return
        # 研究任务不出检索统计图（词频/域名分布回答的是"搜了多少"，不是"公司怎么样"）：
        # 它的图由底稿确定性生成（两期对比/同比/质量）。守卫放在**方法内部**——
        # 调用方除编排器外还有 structured_pipeline 的快照回收/预载两条路，逐处加会漏。
        try:
            if self._is_research_task(task_id):
                return
        except AttributeError:
            pass
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
        无效规格跳过并记录原因；有效图输出 chart_N.png + chart_manifest.json。

        实现在模块级 `render_chart_data`（与"选择变化后重渲染"**同一段代码**）：
        此前这段逻辑只存在于这个混入方法里，于是"按新选择重出图"没有可复用的入口。
        """
        if not self._wants_visualization(goal):
            return
        from workspace import task_project_dir
        render_chart_data(task_project_dir(task_id), task_id=str(task_id))

    def _generate_search_charts(self, task_id: str, goal: str) -> None:
        """确定性基线图表：来源分布、主要主体提及频率、主题热词。
        语义类图表（趋势/份额/指标）由 LLM 结构化数据渲染（_render_chart_data）。"""
        import subprocess
        import sys
        import os
        if not self._wants_visualization(goal):
            return
        # 研究任务不出检索统计图（理由同 `_render_clean_chart_data`）
        try:
            if self._is_research_task(task_id):
                return
        except AttributeError:
            pass
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
