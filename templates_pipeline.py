# -*- coding: utf-8 -*-
"""templates_pipeline——模板路由/固化与计划规范化域（从 orchestrator_v2 抽取）。

normalize_steps / topic_tokens 原样搬迁为模块函数（T10 第一阶段）；
route_template / consolidate_template 经依赖注入搬迁（T10b）：模板加载、直接交付
计划、LLM、验收回调等以显式参数传入，orchestrator 保留同名薄委托注入。
常量（KNOWN_CAPABILITIES / _HUMAN_IN_LOOP_ALLOWED / _TOPIC_STOPWORDS）仍以
orchestrator_v2 为单一来源，函数内晚导入，避免双份漂移与循环导入。
"""
import json
import logging
import os
import re

logger = logging.getLogger("orchestrator_v2")


def normalize_steps(steps: list, max_steps: int) -> list[dict]:
    """规范化计划步骤：去重 ID、剔除空指令、补齐默认字段、限制步数。"""
    from orchestrator_v2 import KNOWN_CAPABILITIES, _HUMAN_IN_LOOP_ALLOWED

    out: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("step_id") or i)
        if sid in seen:
            sid = f"{sid}-{i}"
        seen.add(sid)
        instruction = str(s.get("instruction") or "").strip()
        if not instruction:
            continue
        # 验收点兜底（对标标准 3.2 Plan & Execute）：planner 漏写时自动补
        if "验收：" not in instruction:
            instruction = instruction + "\n验收：步骤完成后输出可验证的结果（文件/数据/文本均可）。"
        s["instruction"] = instruction
        s["step_id"] = sid
        # 校验能力字段：非法/多值拼接时回退到 content_summary
        cap = str(s.get("capability") or "content_summary").strip()
        if "," in cap:
            cap = next((c.strip() for c in cap.split(",") if c.strip() in KNOWN_CAPABILITIES), "content_summary")
        elif cap not in KNOWN_CAPABILITIES:
            cap = "content_summary"
        # 能力纠偏：安装依赖的"package"步骤实为环境准备，改派 code_execution 执行 pip
        if cap == "package" and any(
            k in instruction for k in ("安装", "install", "依赖", "pip")
        ):
            cap = "code_execution"
            s["instruction"] = (
                "使用 pip 安装所需依赖（已安装则跳过），并验证 import 成功："
                f"{instruction}"
            )
            instruction = s["instruction"]
        s["capability"] = cap
        mode = str(s.get("mode") or "parallel").strip().lower()
        if mode not in ("pipeline", "parallel", "human_in_loop"):
            mode = "parallel"
        # P1-2：低风险内部能力步骤不允许 human_in_loop，强制串行 pipeline，
        # 避免执行前等人工确认导致整轮超时（如 package 被规划器误标）。
        if mode == "human_in_loop" and cap not in _HUMAN_IN_LOOP_ALLOWED:
            logger.info("低风险步骤强制 pipeline：%s（step %s）", cap, sid)
            mode = "pipeline"
        s["mode"] = mode
        s.setdefault("timeout", 300)
        out.append(s)
    if len(out) > max_steps:
        logger.warning("Plan normalized from %d to %d steps (max_steps=%d)",
                       len(out), max_steps, max_steps)
        out = out[:max_steps]
    return out


def topic_tokens(text: str) -> set[str]:
    """从目标中提取主题词（中文 2-4 字片段 + 英文词，剔除停用词）。"""
    import re as _re

    from orchestrator_v2 import _TOPIC_STOPWORDS

    tokens = set()
    for w in _re.findall(r"[\u4e00-\u9fff]{2,4}", text):
        if w not in _TOPIC_STOPWORDS:
            tokens.add(w)
    tokens |= {w.lower() for w in _re.findall(r"[a-z][a-z0-9-]{2,}", text)}
    return tokens


def route_template(
    goal: str,
    task_id: str,
    *,
    load_templates,
    direct_plan,
    keyword_match,
    messaging,
    now_iso,
    plan_llm,
) -> list[dict] | None:
    """由 LLM 判断目标是否适合确定性模板（含直接交付模板）；
    失败时回退到关键词判断。返回模板 steps 或 None（走完整规划）。"""
    from ws_helpers import push_progress

    # P2-6 手工模板优先于 auto-*：再排一次，防御 _load_templates 被覆盖/顺序变化
    templates = sorted(
        load_templates(),
        key=lambda t: str(t.get("name") or "").startswith("auto-"),
    )
    # 关键词快速路由：明确命中的任务直接走确定性模板，省掉 LLM 路由调用
    direct = direct_plan(goal)
    if direct:
        push_progress(messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": "模板路由：直接交付（关键词命中，跳过 LLM；模板来源：manual）",
                       "timestamp": now_iso()})
        return direct
    tpl = keyword_match(goal, templates)
    if tpl:
        src = "auto" if str(tpl.get("name") or "").startswith("auto-") else "manual"
        push_progress(messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": f"模板命中（关键词）：{tpl.get('name')}（模板来源：{src}）",
                       "timestamp": now_iso()})
        return tpl.get("steps") or None
    if not templates:
        return direct_plan(goal)
    push_progress(messaging, task_id, "log",
                  {"type": "plan", "agent": "orchestrator",
                   "message": f"模板路由（LLM）：{len(templates)} 个模板待匹配（手工模板优先）",
                   "timestamp": now_iso()})
    # P2-6 auto-* 模板描述增强：goal 中的"目标公司/集团"等占位符
    # 用具体公司名替换后再展示，便于 LLM 判断是否匹配
    company = ""
    try:
        from task_classifier import _extract_company
        company = _extract_company(goal)
    except Exception:
        pass

    def _display_goal(t: dict) -> str:
        """展示模板描述；auto-* 模板含公司占位符时替换为具体公司名。"""
        desc = str(t.get("goal") or "")[:120]
        if company and str(t.get("name") or "").startswith("auto-"):
            for pat in ("目标公司/集团", "目标公司官网", "目标公司", "公司/集团"):
                desc = desc.replace(pat, company)
        return desc

    try:
        tpl_list = "\n".join(
            f"- {t.get('name')}: {_display_goal(t)}" for t in templates
        )
        prompt = (
            "判断以下用户目标是否适合使用现成的确定性执行模板。\n"
            f"可用模板：\n{tpl_list or '（无）'}\n\n"
            "规则：\n"
            '1. 仅当目标与某个模板的【核心任务】基本一致时才选择该模板'
            '（例如目标是"房价预测/数据科学流水线"才能选"数据分析流水线"；'
            '目标是"用户评论情感分析"而模板是房价预测 → 不匹配）→ {"template": "模板名"}\n'
            '1a. 特别注意"公司调研与财报分析"模板：只有目标包含公司/集团的'
            '发展历程、业务现状或公司调研要求时才可选它；'
            '目标仅涉及财报/财务指标分析（营收、净利润、毛利率、现金流、'
            '财务健康度等，不含发展历程/现状调研）→ 不选该模板 → {"template": null}\n'
            '2. 若目标适合"直接交付"（单产物如游戏/脚本/工具/单文件页面，'
            '无需外部调研或多技能协作）→ {"template": "direct_deliverable"}\n'
            '2a. auto- 前缀模板仅在无手工模板匹配时选用；'
            '手工模板（名称不以 auto- 开头）优先\n'
            '3. 否则（需要规划拆解/多技能协作/外部资料）→ {"template": null}\n'
            f"目标：{goal[:400]}\n只输出JSON。"
        )
        raw = plan_llm().call(
            "你是任务路由专家，判断任务类型并选择模板，只输出JSON。",
            prompt,
            expect_json=True,
            usage="plan",
        )
        if isinstance(raw, dict):
            name = raw.get("template")
        else:
            clean = str(raw).strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean).rstrip("`").strip()
            name = json.loads(clean).get("template")
        if name == "direct_deliverable":
            direct = direct_plan(goal)
            if direct:
                return direct
        elif name:
            tpl = next((t for t in templates if t.get("name") == name), None)
            if tpl:
                is_auto = str(name).startswith("auto-")
                has_manual = any(
                    not str(t.get("name") or "").startswith("auto-")
                    for t in templates
                )
                if is_auto and has_manual:
                    # 手工模板优先：库中存在手工模板时 auto-* 不可选，
                    # 直接交给完整规划，避免 auto-financial 抢占手工模板
                    push_progress(
                        messaging, task_id, "log",
                        {"type": "plan", "agent": "orchestrator",
                         "message": (
                             f"模板命中被拒（LLM 选了 auto-* {name}，"
                             "但库中存在手工模板，auto-* 仅在无手工模板时选用）"
                         ),
                         "timestamp": now_iso()},
                    )
                else:
                    src = "auto" if is_auto else "manual"
                    push_progress(
                        messaging, task_id, "log",
                        {"type": "plan", "agent": "orchestrator",
                         "message": f"模板命中（LLM）：{name}（模板来源：{src}）",
                         "timestamp": now_iso()},
                    )
                    return tpl.get("steps") or None
    except Exception as exc:
        logger.info("Template routing via LLM skipped: %s", exc)
    push_progress(messaging, task_id, "log",
                  {"type": "plan", "agent": "orchestrator",
                   "message": "模板路由结束：未命中模板，进入 LLM 规划",
                   "timestamp": now_iso()})
    # 关键词回退
    return direct_plan(goal)


def _resolve_tpl_path(tpl_path: str | None) -> str:
    """模板库路径（#5 输出目录可配置）：
    调用方显式指定 tpl_path 时取规范化绝对路径（测试/部署可用任意合法路径）；
    否则读环境变量 WEAVEMIND_TPL_DIR（指向目录，写其下 templates.json）；
    都未配置时回落到模块旁 templates.json。"""
    if tpl_path:
        return os.path.abspath(str(tpl_path))
    env_dir = (os.environ.get("WEAVEMIND_TPL_DIR") or "").strip()
    if env_dir:
        return os.path.abspath(os.path.join(env_dir, "templates.json"))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")


def consolidate_template(
    goal: str,
    all_steps: list[dict],
    tpl_path: str | None = None,
    task_id: str = "",
    *,
    consolidation_key,
    acceptance_passed,
    count_verified_chain,
    threshold: int,
) -> None:
    """成功的复杂任务沉淀为确定性模板（探索→固化）：
    按 domain×能力链 分组，要求本次验收 pass 且历史同链 pass ≥ 阈值；
    模板使用类型级 goal（不含具体公司名），避免模板库爆炸。"""
    try:
        if not all_steps:
            return
        steps: list[dict] = []
        # 目标主题词：用于单步指令清洗后的主题一致性校验
        tokens = topic_tokens(goal)
        if not tokens:
            # 目标无法提取主题词（可能中文损坏/过短）：保守不沉淀，避免污染模板库
            logger.info("Template not consolidated (no topic tokens): %s", goal[:40])
            return
        for s in all_steps[:8]:
            cap = s.get("capability")
            if cap in ("report_generator", "package"):
                continue  # 收尾打包类步骤不进模板，保留核心能力链（含内容摘要）
            ins = str(s.get("instruction") or "")
            # 剥离追加的反思反馈与自动沉淀的历史教训块
            # （"【历史教训（自动沉淀）】反思要求重做：…"、"【反思要求重做】…"），
            # 保留原可执行指令；经反思重做的任务同样可固化
            for marker in ("【历史教训", "【反思要求重做】", "反思要求重做"):
                if marker in ins:
                    ins = ins.split(marker, 1)[0].strip()
                    break
            # 纯记忆注入（指令整体是历史经验上下文）才跳过
            if ins.startswith("历史经验"):
                continue
            if "用户目标：" in ins:
                ins = ins.split("用户目标：", 1)[-1]
            if "任务目标：" in ins:
                ins = ins.split("任务目标：", 1)[-1]
            if "原始指令：" in ins:
                ins = ins.split("原始指令：", 1)[-1]
            ins = ins.strip()
            if not ins:
                continue
            # 单步主体一致性：步骤指令混入其他公司且不含目标公司
            # （如跨公司教训串味后的"搜索美团；…"）→ 记忆残渣，丢弃
            try:
                from acceptance_checker import _OTHER_ENTITIES
                from task_classifier import _extract_company
                _tgt_comp = _extract_company(goal)
                if _tgt_comp and _tgt_comp not in ins and any(
                    e != _tgt_comp and e in ins for e in _OTHER_ENTITIES
                ):
                    continue
            except Exception:
                pass
            # 截断时尽量在句子边界，避免沉淀出半句指令
            if len(ins) > 200:
                head = ins[:200]
                cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
                ins = head if cut <= 120 else head[:cut + 1]
            steps.append({
                "step_id": str(len(steps) + 1),
                "capability": cap,
                "instruction": ins,
                "timeout": 180,
            })
        if len(steps) < 2:
            return
        # 主题一致性校验：步骤指令必须包含目标主题词，否则可能是跑偏任务（不沉淀）
        hay = " ".join(str(s.get("instruction", "")) for s in steps)
        if not any(t in hay for t in tokens):
            logger.info("Template not consolidated (off-topic execution): %s", goal[:40])
            return
        # ── 探索→固化：domain×能力链 验收驱动 ──
        domain, chain = consolidation_key(goal, all_steps)
        # 本次任务验收：存在验收报告时必须 pass
        if task_id:
            acc = acceptance_passed(task_id)
            if acc is False:
                logger.info("Template not consolidated (acceptance fail): %s", goal[:40])
                return
        # 历史同链验收 pass 计数（不含本次）
        verified = count_verified_chain(domain, chain, task_id)
        if verified + 1 < threshold:
            logger.info(
                "Template not consolidated yet (%s/%d verified): %s",
                verified + 1, threshold, goal[:40],
            )
            return
        type_goal = {
            "financial": "公司/集团的发展历程与现状，并分析历年财报",
            "code": "生成可运行的代码/程序并验证交付",
            "general": "搜索并总结目标主题的现状，输出结构化报告",
        }.get(domain, "通用任务调研与报告")
        name = f"auto-{domain}-{'-'.join(chain[:3]) or 'pipeline'}"
        path = _resolve_tpl_path(tpl_path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tpls = data.get("templates", [])
        tpls = [t for t in tpls if t.get("name") != name]
        # 模板必须公司无关：把具体公司名替换为"目标公司"，避免固化出
        # "搜索腾讯控股…"这类只对单一公司有效的指令（goal 已是类型级）
        try:
            from task_classifier import _extract_company
            company = _extract_company(goal)
            if company:
                for s in steps:
                    ins = str(s.get("instruction") or "")
                    # 英文公司名括号（如"（Tencent Holdings Ltd）"）也剥离，
                    # 避免模板残留单一公司英文名
                    try:
                        import re as _re
                        ins = _re.sub(
                            r"[（(][A-Za-z][A-Za-z0-9 .&'-]*(?:Ltd|Inc|Corp|Holdings|Group|Limited|Co)[^）)]*[）)]",
                            "", ins,
                        )
                    except Exception:
                        pass
                    for pat, rep in (
                        (company + "控股", "目标公司/集团"),
                        (company + "集团", "目标公司/集团"),
                        (company + "官网", "目标公司官网"),
                        (company, "目标公司"),
                    ):
                        ins = ins.replace(pat, rep)
                    s["instruction"] = ins
        except Exception:
            pass
        tpls.append({"name": name, "goal": type_goal, "steps": steps})
        data["templates"] = tpls[-30:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Template consolidated: %s (%d steps, domain=%s)", name, len(steps), domain)
    except Exception as exc:
        logger.warning("Template consolidation failed: %s", exc)
