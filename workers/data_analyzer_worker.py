"""Data Analyzer Worker — EDA with 3 charts, no LLM needed."""
import asyncio, json, os, tempfile, time
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db_paths
from async_worker_base import AsyncWorkerBase

CHART_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "charts"
pass

class ContractGap(RuntimeError):
    """研究契约要素不全：**不跑模型**，把缺什么直接说给读者（K0-a）。

    为什么宁可不跑：裸载荷里"有哪些年度就取最新两期"、不认主体/口径/as_of，会让
    "请求 2023/2024 而载荷只有 2025"、"错公司/错口径/晚披露"静默进入模型并算出
    validated 的结论——比缺一个模型卡危险得多。
    """


def _research_contract(task: dict, payload: dict):
    """取本次任务的**研究契约**（与底稿同一条解析链）→ `(request, gaps, source)`。

    复用 `working_paper_export.resolve_request`：落库契约优先，自由文本兜底；
    这里只判"够不够跑模型"：主体、两个以上年度期间、报表口径、资料截止日 as_of。
    """
    from facts import UNKNOWN
    from working_paper_export import resolve_request

    md = dict((payload or {}).get("metadata") or {})
    if str((payload or {}).get("source") or "") == "multi_entity":
        md = dict(((payload or {}).get("companies") or [{}])[0].get("metadata") or {}) or md
    root = str(((task or {}).get("context") or {}).get("root_task_id")
               or (task or {}).get("task_id") or "")
    goal = str((task or {}).get("goal") or "")
    request, _cands, source = resolve_request(
        root, goal, md, (payload or {}).get("resolution") or {})
    gaps: list[str] = []
    if request is None or not (getattr(request, "company", "")
                               or getattr(request, "company_id", "")):
        gaps.append("研究主体（公司名或股票代码）")
    else:
        if len([p for p in (getattr(request, "periods", None) or [])
                if str(p).strip()]) < 2:
            gaps.append("两个以上年度期间")
        if str(getattr(request, "caliber", "") or UNKNOWN) == UNKNOWN:
            gaps.append("报表口径（合并/母公司）")
        if not str(getattr(request, "as_of", "") or "").strip():
            gaps.append("资料截止日 as_of")
    return request, gaps, source


def _with_extra_gaps(ds, extra: list[str]):
    """把"哪些观察因与契约不相容被排除"记进清单缺口（冻结结果不可变 → 换一份）。"""
    import dataclasses

    gaps = tuple(list(ds.manifest.gaps) + [g for g in extra if g])
    return dataclasses.replace(ds, manifest=dataclasses.replace(ds.manifest, gaps=gaps))


class DataAnalyzerWorker(AsyncWorkerBase):
    _class_capabilities = ["data_analyzer"]
    _needs_task = True

    # ── 金融任务：按**显式数据集 + 分析计划**走注册模型（不猜最新 CSV、不猜末列）──────
    _FINANCIAL_MARKERS = ("[研究契约]", "归母净利", "营业收入", "经营现金流", "financial_analysis")

    @staticmethod
    def _financial_source(ws: Path):
        """工作区里**本次任务自己的**金融事实来源 → `(kind, path)`，都没有则 `None`。

        判定只看文件是否存在，且**只认这两个显式来源**（不猜最新 CSV、不猜末列——那套
        猜测路径正是 Q1 要求取消的）：

        - `project/working_paper.json`：现役研究链落盘的**事实底稿**（含已选事实、口径
          证据与派生行）——首选；
        - `project/financials.json`：预载的**结构化财务载荷**（来源与元数据随行）——
          底稿还没落盘时的退路。

        为什么必须有退路（2026-09-29 付费整跑 `ui-f4bac0d202` 实机）：底稿此前只在
        **收尾装配**时才写，而 `data_analyzer` 步骤跑在装配之前——那一步找不到底稿，
        退到通用 EDA、报 "No fresh CSV found in workspace"，重试三次后被**重规划成
        content_summary**（文字概括），注册模型一次都没跑，`analysis/analysis_runs.json`
        一份没有 → 交付硬门槛如实拦下整单（「分析未完成：交付正文只有数据与底稿」）。
        """
        for kind, name in (("working_paper", "working_paper.json"),
                           ("financials", "financials.json")):
            for cand in (Path(ws) / "project" / name, Path(ws) / name):
                if cand.is_file():
                    return kind, cand
        return None

    @staticmethod
    def _freeze_dataset(kind: str, path: Path, *, task: dict | None,
                        required_metrics, available_models):
        """按来源冻结数据集（`(dataset, 来源标签)`）。

        两条路都是**显式来源 + 来源声明的元数据**，都不做"哪个数更好"的猜测：
        同一 (指标, 期间, 口径) 出现互不相容的值由冻结层如实标记冲突，不择一。

        **退路同样绑定研究契约**（K0-a，2026-09-29 夜验收）：`financials.json` 只是
        载荷，不是契约——主体/两期/口径/as_of 一律来自任务契约；与该契约不相容的观察
        （错公司、别的口径、披露日晚于 as_of）**不参与冻结**，一条都不剩就如实报缺口。
        """
        import financial_analysis as fa

        label = f"worker:{path.name}"
        if kind == "working_paper":
            obj = json.loads(path.read_text(encoding="utf-8"))
            ds = fa.freeze_from_working_paper(
                obj, source_label=label, required_metrics=required_metrics,
                available_models=available_models)
            return ds, label
        import facts as _facts
        payload = json.loads(path.read_text(encoding="utf-8"))
        request, gaps, _source = _research_contract(task or {}, payload)
        if gaps:
            raise ContractGap(
                "金融分析缺少研究契约要素：" + "、".join(gaps)
                + "（不得用裸载荷默认取最新两期；请补齐研究请求后重跑）")
        want_cal = str(getattr(request, "caliber", "") or "")
        as_of = str(getattr(request, "as_of", "") or "")[:10]
        kept, dropped = [], {"subject": 0, "caliber": 0, "as_of": 0}
        for f in _facts.facts_from_financials(payload):
            ok, _why = _facts.check_subject(request, f.entity, f.entity_id, f.market)
            if not ok:
                dropped["subject"] += 1
                continue
            if want_cal and str(getattr(f, "caliber", "") or "") != want_cal:
                dropped["caliber"] += 1
                continue
            _disc = str(getattr(f, "disclosed_at", "") or "")[:10]
            if as_of and _disc and _disc > as_of:
                dropped["as_of"] += 1
                continue
            kept.append(f)
        if not kept:
            raise ContractGap(
                "载荷里没有与契约相容的观察（主体/口径/as_of）："
                f"契约主体={request.company or request.company_id}、口径={want_cal}、"
                f"as_of={as_of}；被排除 "
                + "、".join(f"{k} {v} 条" for k, v in dropped.items() if v)
                + "。请核对来源或补材料")
        ds = fa.freeze_from_facts(
            kept, request=request, source_label=label,
            required_metrics=required_metrics, available_models=available_models)
        if dropped and any(dropped.values()):
            ds = _with_extra_gaps(ds, [
                "与契约不相容的观察已排除：" + "、".join(
                    f"{k} {v} 条" for k, v in dropped.items() if v)])
        return ds, label

    def _run_financial(self, ws: Path, instruction: str, task: dict, source) -> dict:
        """冻结数据集 → 编译计划 → 跑注册模型 → 落盘运行记录（确定性、零模型调用）。"""
        import financial_analysis as fa
        from financial_analysis import store as fa_store

        kind, path = source
        try:
            ds, source_label = self._freeze_dataset(
                kind, path, task=task,
                required_metrics=("revenue", "net_profit", "gross_profit",
                                  "operating_cashflow"),
                available_models=[m.model_id for m in fa.specs()])
        except ContractGap as exc:
            # 如实失败：不产出任何运行记录，也不拿别的数顶上（读者拿到可行动缺口）
            return {"status": "failed", "mode": "financial",
                    "dataset_source": {"kind": kind, "file": path.name,
                                       "label": f"worker:{path.name}"},
                    "error": str(exc), "gap": str(exc), "runs": [], "cards": [],
                    "chart_specs": [],
                    "note": "研究契约要素不全：不跑注册模型（缺口见 error）"}
        plan = fa.compile_plan(str(instruction or ""), ds, prefer=("profit_bridge",))
        runs, cards, specs = [], [], []
        for item in plan.adopted:
            run = fa.run(item.model_id, ds, params=item.params, question=item.question)
            fa_store.save_run(ws, run)
            runs.append({"run_id": run.run_id, "model_id": run.model_id,
                         "status": run.status, "reason": run.reason,
                         "validation_ok": bool((run.validation or {}).get("ok")),
                         "outputs": [{"output_id": o.output_id, "metric": o.metric,
                                      "value": o.value, "unit": o.unit,
                                      "output_period": o.output_period}
                                     for o in run.outputs]})
            if run.status == fa.RunStatus.VALIDATED and run.outputs:
                cards.append(fa.analysis_card(run, run.outputs[0].output_id))
                specs.append(fa.chart_spec(run, run.outputs[0].output_id))
        # 同年比率：同样走注册算子（零分母 not_computable，不产出数字）
        for rlabel, num, den, _desc in fa.runner.known_ratios():
            rr = fa.ratio_run(rlabel, num, den, ds)
            if rr.status == fa.RunStatus.VALIDATED:
                fa_store.save_run(ws, rr)
                runs.append({"run_id": rr.run_id, "model_id": rr.model_id,
                             "status": rr.status, "reason": rr.reason,
                             "validation_ok": True,
                             "outputs": [{"output_id": o.output_id, "metric": o.metric,
                                          "value": o.value, "unit": o.unit,
                                          "output_period": o.output_period}
                                         for o in rr.outputs]})
        ok_runs = [r for r in runs if r["status"] == fa.RunStatus.VALIDATED]
        adopted_ok = [r for r in runs if r["model_id"] in
                      {a.model_id for a in plan.adopted} and r["status"] == fa.RunStatus.VALIDATED]
        # 状态如实（三档）：请求的模型一个都用不上 → failed；全过**且没有被拒绝的模型**
        # → success；其余（部分过、或有模型因缺输入被拒）→ partial —— 缺输入/不适用
        # 不是成功，读者必须看到缺口。
        if not plan.adopted or not adopted_ok:
            status = "failed"
        elif len(adopted_ok) == len(plan.adopted) and not plan.rejected:
            status = "success"
        else:
            status = "partial"
        return {
            "status": status,
            "mode": "financial",
            # 数据集来自哪一个显式来源：读者要能分清"底稿冻结的"与"预载载荷冻结的"
            # （覆盖度不同，来源标签与 dataset_hash 一起进运行记录，可复核）
            "dataset_source": {"kind": kind, "file": path.name, "label": source_label},
            "dataset_hash": ds.dataset_hash,
            "dataset": {"entity": ds.manifest.entity, "entity_id": ds.manifest.entity_id,
                        "periods": list(ds.manifest.periods),
                        "usable": ds.manifest.usable, "observations": ds.manifest.observations,
                        "gaps": list(ds.manifest.gaps), "conflicts": list(ds.manifest.conflicts)},
            "plan": {"adopted": [a.model_id for a in plan.adopted],
                     "rejected": list(plan.rejected)},
            "runs": runs,
            "cards": cards,
            "chart_specs": specs,
            "note": ("金融任务走显式数据集与注册模型（冻结数据集→分析计划→独立验证→"
                     "运行记录），不用“最新 CSV/末列当目标”的猜测路径"),
        }

    @staticmethod
    def _load_frame(fpath: Path) -> pd.DataFrame:
        """读取数据文件：CSV 直读；JSON（structured_data.json）按
        rows/items/points 数组转 DataFrame，让预载的排行/宏观数据
        无需 CSV 也能做 EDA。"""
        if fpath.suffix.lower() != ".json":
            return pd.read_csv(fpath)
        with fpath.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            payload = data.get("data") or {}
            for key in ("rows", "items", "points"):
                rows = payload.get(key)
                if isinstance(rows, list) and rows:
                    return pd.DataFrame(rows)
            flat = {
                k: v for k, v in payload.items()
                if not isinstance(v, (list, dict)) and v is not None
            }
            if flat:
                return pd.DataFrame([flat])
            raise ValueError(f"{fpath.name} 中没有可消费的行数据")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return pd.DataFrame(data)
        raise ValueError(f"{fpath.name} 不是结构化数据（无法转为 DataFrame）")

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        try:
            chart_dir = CHART_DIR
            data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
            ws = Path(tempfile.gettempdir()) / "agent_workspace"
            if task and task.get("workspace"):
                ws = Path(str(task["workspace"]))
                chart_dir = ws / "charts"
                data_dir = ws / "data"
                chart_dir.mkdir(parents=True, exist_ok=True)
                data_dir.mkdir(parents=True, exist_ok=True)
            # Find data path from instruction or use latest CSV in workspace
            import re
            ws_path = Path(ws)
            # **金融任务优先**（Q1）：工作区有本次任务自己的金融事实（底稿或预载载荷）时，
            # 必须按显式数据集 + 分析计划走注册模型——"最新 CSV + 末列当目标"那条猜测
            # 路径不适用于金融任务（两期财报不是通用 EDA 数据集，末列也不是预测目标）。
            _fin = self._financial_source(ws_path)
            if _fin is not None:
                return json.dumps(self._run_financial(ws_path, instruction, task or {}, _fin),
                                  ensure_ascii=False)
            paths = re.findall(
                r'[A-Za-z]:[\\/][^\s,]+\.(?:csv|xlsx|json)|/tmp/[^\s,]+\.(?:csv|xlsx|json)',
                instruction,
            )
            if paths:
                fpath = Path(paths[0].replace("\\", "/"))
            else:
                # 预载结构化数据优先：ranking.csv / structured_data.json 是本次任务
                # 刚预载的真实数据，即使指令未显式给路径也可直接消费（断链修复）
                candidates: list[Path] = []
                ranking_csv = data_dir / "ranking.csv"
                structured_json = ws / "project" / "structured_data.json"
                if ranking_csv.exists():
                    candidates.append(ranking_csv)
                if structured_json.exists():
                    candidates.append(structured_json)
                if not candidates:
                    # 仅当指令明确涉及数据分析，且工作区存在 1 小时内的新 CSV 时才兜底，
                    # 避免把历史任务遗留的无关数据（如加州房价）拉进当前任务。
                    keywords = ("分析", "数据", "csv", "数据集", "eda", "统计", "建模", "训练", "房价", "预测", "回归")
                    instruction_l = instruction.lower()
                    if not any(k in instruction_l for k in keywords):
                        return json.dumps({"status": "failed", "error": "No data path provided in instruction"}, ensure_ascii=False)
                    now = time.time()
                    candidates = [
                        p for p in data_dir.glob("*.csv")
                        if now - p.stat().st_mtime < 3600
                    ]
                if not candidates:
                    return json.dumps({"status": "failed", "error": "No fresh CSV found in workspace"}, ensure_ascii=False)
                fpath = candidates[0]

            df = self._load_frame(fpath)
            shape = list(df.shape)
            cols = list(df.columns)
            missing = df.isnull().sum().to_dict()
            numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
            target_col = df.columns[-1]  # assume last column is target
            if len(df) < 2 or len(numeric_cols) < 2:
                # 单行/无数值列（如单点行情快照）：无法做相关性/散点，
                # 如实返回成功但无图，避免 worker 内部异常
                return json.dumps({
                    "status": "success",
                    "shape": shape,
                    "columns": cols,
                    "missing": missing,
                    "target": target_col,
                    "charts": [],
                    "data_path": str(fpath),
                }, ensure_ascii=False)

            # Chart 1: Numeric columns distribution histogram
            fig1, axes = plt.subplots(1, min(3, len(numeric_cols)), figsize=(12, 4))
            if len(numeric_cols) == 1:
                axes = [axes]
            for ax, col in zip(axes, numeric_cols[:3]):
                df[col].hist(ax=ax, bins=30, alpha=0.7)
                ax.set_title(col)
            plt.tight_layout()
            chart1 = str(chart_dir / "histograms.png")
            fig1.savefig(chart1, dpi=100)
            plt.close(fig1)

            # Chart 2: Correlation heatmap
            fig2, ax2 = plt.subplots(figsize=(10, 8))
            corr = df[numeric_cols].corr()
            sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm", ax=ax2)
            plt.tight_layout()
            chart2 = str(chart_dir / "heatmap.png")
            fig2.savefig(chart2, dpi=100)
            plt.close(fig2)

            # Chart 3: Target vs top feature scatter
            if len(numeric_cols) >= 2:
                top_feat = corr[target_col].drop(target_col).abs().idxmax()
                fig3, ax3 = plt.subplots(figsize=(8, 6))
                ax3.scatter(df[top_feat], df[target_col], alpha=0.5)
                ax3.set_xlabel(top_feat); ax3.set_ylabel(target_col)
                ax3.set_title(f"{target_col} vs {top_feat}")
                plt.tight_layout()
                chart3 = str(chart_dir / "scatter.png")
                fig3.savefig(chart3, dpi=100)
                plt.close(fig3)
            else:
                chart3 = ""

            return json.dumps({
                "status": "success",
                "shape": shape,
                "columns": cols,
                "missing": missing,
                "target": target_col,
                "charts": [chart1, chart2, chart3],
                "data_path": str(fpath),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "failed", "error": str(e)})


if __name__ == "__main__":
    import asyncio, sys, os
    from logging_setup import setup_logging
    setup_logging("worker-data-analyzer")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "dataanalyzerworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging
    reg = AsyncRegistry(db_paths.resolve_db_path())
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))
    async def run():
        worker = DataAnalyzerWorker(agent_id=agent_id, capabilities=DataAnalyzerWorker._class_capabilities, registry=reg, messaging=msg)
        await worker.run()
    asyncio.run(run())
