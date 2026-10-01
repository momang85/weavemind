# -*- coding: utf-8 -*-
"""金融分析包（阶段 Q）：冻结数据集 → 注册模型 → 独立验证 → 分析卡。

设计边界（架构决策 `docs/金融分析与受控建模阶段Q_20260929.md` §4）：

- **不回指编排器/HTTP 巨文件**：本包只依赖标准库与自身；现役 Worker/编排器通过适配层调用
  这里的纯函数入口（`financial_analysis.api` 那组函数）。
- **计算核心零模型调用**：本包不 import `llm_client`/`web_ui`/`orchestrator_v2`。
- **身份贯穿**：`AnalysisDataset.manifest.dataset_hash` + `ModelSpec` 版本 + 参数 hash
  → `ModelRun.run_id`；数据集一变，旧 run 立刻 `expired`（不给"改了数还沿用旧结果"留口子）。
- **不适用/缺输入就停**：不猜最后一列、不补造输入、不用百分比差代替金额归因。
"""

from .contracts import (  # noqa: F401
    AnalysisDataset, AnalysisPlan, DatasetManifest, ModelRun, ModelSpec,
    MissingInput, NotApplicable, NotComputable, Observation, RunStatus,
    ValidatedOutput, full_identity_ok, report_scope_ok,
)
from . import charts  # noqa: F401
from .dataset import freeze_from_facts, freeze_from_working_paper  # noqa: F401
from .registry import available_for, spec, specs  # noqa: F401
from .report_adapter import analysis_card, chart_spec, delivery_binding  # noqa: F401
from .runner import compile_plan, raise_for_status, ratio_run, revalidate, run  # noqa: F401
from .validation import tamper_check, unusable_inputs, validate_output  # noqa: F401

__all__ = [
    "AnalysisDataset", "AnalysisPlan", "DatasetManifest", "ModelRun", "ModelSpec",
    "MissingInput", "NotApplicable", "NotComputable", "Observation", "RunStatus",
    "ValidatedOutput",
    "report_scope_ok", "full_identity_ok",
    "freeze_from_facts", "freeze_from_working_paper",
    "charts",
    "available_for", "spec", "specs",
    "compile_plan", "run", "ratio_run", "raise_for_status", "revalidate",
    "validate_output", "tamper_check", "unusable_inputs",
    "analysis_card", "chart_spec", "delivery_binding",
]
