# -*- coding: utf-8 -*-
"""前端不变量守卫（源码级，纯 Python，无需 Node/JS 测试运行器）。

为什么是源码级：本仓库没有 JS 测试运行器（`frontend/package.json` 只有 dev/build/preview），
CI 的前端作业只做 `npm ci && npm run build`。既有做法是把"绝不退化的约定"写成 Python 断言
（见 `test_isolation_scope.py`、`test_p0.TestLauncherCrossDevice`），这里沿用同一模式并把
本轮（F2b）修过的四类问题钉死：

1. **字号下限 12px**：`text-[10px]` 这类任意值会绕过 Tailwind 的字号档位（全仓曾有 97 处）。
2. **不留死声明**：`components/ui.tsx` 的导出、`tailwind.config.js` 的颜色令牌，都必须有使用方
   ——旧配置里的 `accent` 定义了却零引用，属于同类问题。
3. **状态显示走唯一实现**：状态徽章/标签/颜色只能来自 `lib/statusMeta`（或 `components/ui` 的
   `StatusBadge`），不得在页面里自建映射或直接渲染后端英文枚举。
4. **演示模式的真实操作边界**：`lib/demoGuard.ts` 的拦截前缀清单必须覆盖前端实际发出的所有
   写请求（这一条正是为了防住上一批的漏拦：`/api/task/<id>/cancel` 不以 `/task` 开头）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "frontend" / "src"
UI_TSX = SRC / "components" / "ui.tsx"
DEMO_GUARD = SRC / "lib" / "demoGuard.ts"
TAILWIND = ROOT / "frontend" / "tailwind.config.js"

# 演示模式下必须被拦截的写/付费端点（前缀）
REQUIRED_DEMO_PREFIXES = (
    "/task",                 # 提交任务
    "/api/task",             # 取消/重跑（上一批漏拦过）
    "/api/quick-answer",     # 快答 / 单智能体直发
    "/api/memory",           # 记忆删除
    "/api/evolution",        # 触发/审批演化
    "/api/notifications",    # 通知配置
    "/api/scheduled-jobs",   # 定时任务
    "/api/users",            # 用户管理
    "/api/config",           # 配置写回 / 连通性测试
    "/api/plan",             # 计划确认
    "/api/deliverable",      # 运行交付物（服务端执行代码）
    "/api/verify",           # 数据溯源（LLM 费用）
    "/api/kill-worker",      # 终止 worker
    "/api/llm-mode",         # 切换 LLM 路由
)
# 不允许出现的"死条目"：前端没有这些路由（曾经写成守卫前缀，看着像覆盖了其实没有）
FORBIDDEN_DEMO_PREFIXES = ("/api/settings", "/api/agents")

# 演示模式下必须被显式排除（否则用户退出不了/登不回来）
ALLOWED_STATE_CHANGING = ("/api/logout", "/api/login", "/api/setup-admin", "/api/auth")

# 展示任务状态的文件：必须使用统一实现
STATUS_BEARING = (
    "pages/History.tsx",
    "pages/Agents.tsx",
    "pages/AuditPage.tsx",
    "pages/Settings.tsx",
    "components/console/ConsoleSideTabs.tsx",
    "components/TaskTreeView.tsx",
    "components/StepInspector.tsx",
)


def _sources() -> list[Path]:
    # 跳过 .mimosa 下的扫描缓存副本（不是源码）
    return [p for p in SRC.rglob("*")
            if p.suffix in (".ts", ".tsx") and ".mimosa" not in p.parts]


class TestFontFloor(unittest.TestCase):
    def test_no_sub_12px_tailwind_arbitrary_values(self):
        bad = []
        for path in _sources():
            for m in re.finditer(r"text-\[(\d+)px\]", path.read_text(encoding="utf-8")):
                if int(m.group(1)) < 12:
                    bad.append(f"{path.relative_to(ROOT)}:{m.group(0)}")
        self.assertEqual(bad, [], f"字号低于 12px（文本可读性下限）：{bad[:8]}")

    def test_no_sub_12px_canvas_or_inline_font(self):
        bad = []
        pattern = re.compile(r"font(?:Size)?\s*[:=]\s*['\"](?:bold\s+)?(\d+)px")
        for path in _sources():
            for m in pattern.finditer(path.read_text(encoding="utf-8")):
                if int(m.group(1)) < 12:
                    bad.append(f"{path.relative_to(ROOT)}:{m.group(0)}")
        self.assertEqual(bad, [], f"画布/内联字号低于 12px：{bad[:8]}")


class TestNoDeadDeclarations(unittest.TestCase):
    def test_ui_exports_are_used_somewhere(self):
        src = UI_TSX.read_text(encoding="utf-8")
        exported = re.findall(r"export function (\w+)", src)
        self.assertTrue(exported, "ui.tsx 应导出原语")
        others = [(p, p.read_text(encoding="utf-8")) for p in _sources() if p != UI_TSX]
        unused = [name for name in exported
                  if not any(re.search(rf"\b{name}\b", text) for _, text in others)]
        self.assertEqual(unused, [], f"这些原语声明了但没有任何使用方（死声明）：{unused}")

    def test_color_tokens_are_used_somewhere(self):
        cfg = TAILWIND.read_text(encoding="utf-8")
        # 取 colors 段里的顶层令牌名：accent / surface / line / ink / state
        tokens = re.findall(r"^\s{8}(\w+):\s*\{", cfg, re.M)
        self.assertTrue(tokens, "tailwind.config.js 里应声明颜色令牌")
        texts = [p.read_text(encoding="utf-8") for p in _sources()]
        unused = []
        for tok in tokens:
            if tok == "fontSize":
                continue
            used = any(re.search(rf"(bg|text|border|ring|from|to|via|shadow)-{tok}\b", t) for t in texts)
            if not used:
                unused.append(tok)
        self.assertEqual(unused, [], f"这些颜色令牌零引用（死令牌）：{unused}")


class TestStatusSemanticsUnified(unittest.TestCase):
    def test_status_bearing_files_use_shared_implementation(self):
        missing = []
        for rel in STATUS_BEARING:
            text = (SRC / rel).read_text(encoding="utf-8")
            if not any(k in text for k in ("statusMeta", "StatusBadge")):
                missing.append(rel)
        self.assertEqual(missing, [],
                         f"这些文件展示任务状态但没走统一实现（statusMeta/StatusBadge）：{missing}")

    def test_no_raw_status_enum_rendered_in_jsx_text(self):
        """禁止在 JSX 文本位直接渲染后端英文枚举（如 >{m.status}<）。"""
        pattern = re.compile(r">\s*\{[A-Za-z_][A-Za-z0-9_.]*\.(?:status|last_status)\}\s*<")
        bad = []
        for path in _sources():
            for m in pattern.finditer(path.read_text(encoding="utf-8")):
                bad.append(f"{path.relative_to(ROOT)}:{m.group(0).strip()}")
        self.assertEqual(bad, [], f"直接渲染了状态枚举原文：{bad[:8]}")


class TestDemoBoundaryRealBehavior(unittest.TestCase):
    """演示边界必须由**真实行为测试**兜底，前缀清单只是补充。

    架构复核 01:05 明确：断言前缀存在不等于缺陷关闭——要证明"请求没有真的发出去"、
    "退出演示后不再拦截"、"403 不再被当成成功"、"刷新失败不留旧值"。这些在
    `frontend/tests/*.test.mjs` 里用 Node 内存 fetch 替身验证，并由 CI 执行。
    """

    NODE_TESTS = ROOT / "frontend" / "tests"
    CI_YML = ROOT / ".github" / "workflows" / "ci.yml"

    def test_behavior_test_files_exist(self):
        files = {p.name for p in self.NODE_TESTS.glob("*.test.mjs")}
        for name, why in (
            ("demoGuard.test.mjs", "演示守卫：真实出网次数 / 退出后是否仍拦截"),
            ("apiResult.test.mjs", "API 结果判定：403 拦截与真实失败不得显示成功"),
            ("metricsState.test.mjs", "指标状态机：失败后不得保留旧快照当当前值"),
        ):
            self.assertIn(name, files, f"缺少行为测试 {name}（{why}）")

    def test_behavior_tests_run_in_ci(self):
        ci = self.CI_YML.read_text(encoding="utf-8")
        self.assertIn("--test tests/*.test.mjs", ci,
                      "前端行为测试没有接进 CI（没跑过的测试不算闸门）")

    def test_paid_get_paths_are_declared(self):
        """GET 不能一概视作无副作用：记忆摘要冷缓存与 ?refresh=1 都会调用付费模型。"""
        src = DEMO_GUARD.read_text(encoding="utf-8")
        self.assertIn("DEMO_BLOCKED_GET", src, "缺少付费 GET 清单")
        self.assertIn("/api/memory/summary", src, "记忆摘要未按付费接口拦截")

    def test_url_param_is_initialization_only(self):
        """URL 的 `?demo` 只能用于初始化：显式开关过之后必须以显式状态为准。"""
        src = DEMO_GUARD.read_text(encoding="utf-8")
        self.assertIn("resolved", src, "缺少「显式状态优先」标记")
        self.assertNotIn("if (state.active) return true", src,
                         "又写成 URL 与开关做或运算（退出后仍会拦截）")

    def test_memory_handlers_judge_response(self):
        """Memory 的删除/演化/审批必须逐个判定响应结果，不得无条件报成功。"""
        src = (SRC / "pages" / "Memory.tsx").read_text(encoding="utf-8")
        self.assertIn("readApiOutcome", src, "处理函数未统一判定响应")
        self.assertGreaterEqual(
            src.count("readApiOutcome(") + src.count("apiOutcome("), 4,
            "删除/演化/审批/摘要四处都要判定结果（否则 403 会显示成功）")
        self.assertNotIn("setNotice('删除完成')", src, "删除仍无条件报成功")
        self.assertNotIn("setNotice('进化已触发", src, "演化仍无条件报成功")

    def test_metrics_page_uses_state_machine(self):
        """失败即不可用：指标页必须走 lib/metricsState（旧实现只设错误文案、留着旧值）。"""
        src = (SRC / "pages" / "MetricsPage.tsx").read_text(encoding="utf-8")
        self.assertIn("metricsFailure", src, "指标页未使用状态机（刷新失败会继续显示旧值）")
        self.assertIn("lastOkAt", src, "失败后要明确标注「上次成功刷新」，而不是继续当当前值")


class TestDemoGuardCoverage(unittest.TestCase):
    """前缀清单（结构检查）。真实行为见 TestDemoBoundaryRealBehavior。"""

    def _prefixes(self) -> list[str]:
        text = DEMO_GUARD.read_text(encoding="utf-8")
        block = text.split("DEMO_BLOCKED_PREFIXES", 1)[1].split("] as const", 1)[0]
        return re.findall(r"'(/[^']*)'", block)

    def test_required_prefixes_present(self):
        prefixes = self._prefixes()
        missing = [p for p in REQUIRED_DEMO_PREFIXES
                   if not any(pre == p or pre.startswith(p) for pre in prefixes)]
        self.assertEqual(missing, [], f"演示模式漏拦这些写/付费端点：{missing}")

    def test_no_dead_prefixes(self):
        prefixes = self._prefixes()
        dead = [p for p in FORBIDDEN_DEMO_PREFIXES if p in prefixes]
        self.assertEqual(dead, [], f"守卫里写了不存在的路由前缀（看着像覆盖了）：{dead}")

    def test_session_endpoints_allowed(self):
        prefixes = self._prefixes()
        for allowed in ALLOWED_STATE_CHANGING:
            hit = [p for p in prefixes if allowed.startswith(p) or p.startswith(allowed)]
            self.assertEqual(hit, [], f"{allowed} 必须放行（否则演示模式下无法退出/登录）：{hit}")

    def test_every_state_changing_frontend_call_is_covered(self):
        """扫描前端所有非 GET 请求，逐条比对本清单——上一批的漏拦由此发现。"""
        prefixes = self._prefixes()
        allowed = ALLOWED_STATE_CHANGING
        offenders = []
        call = re.compile(
            r"fetch\(\s*([`'\"])(?P<path>[^`'\"]*)\1(?P<rest>.{0,200}?)\)",
            re.S,
        )
        for path in _sources():
            text = path.read_text(encoding="utf-8")
            for m in call.finditer(text):
                rest = m.group("rest") or ""
                method_m = re.search(r"method:\s*['\"](\w+)", rest)
                method = (method_m.group(1) if method_m else "GET").upper()
                if method in ("GET", "HEAD", "OPTIONS"):
                    continue
                raw = m.group("path")
                target = raw.split("${", 1)[0] or raw  # 动态模板取字面前缀
                target = target.replace("\\", "")
                if not target.startswith("/"):
                    continue
                if any(target.startswith(a) for a in allowed):
                    continue
                covered = any(target == p or target.startswith(p) or p.startswith(target)
                              for p in prefixes)
                if not covered:
                    line = text[:m.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line} {method} {raw}")
        self.assertEqual(offenders, [],
                         f"演示模式未覆盖的写请求（应补进 DEMO_BLOCKED_PREFIXES 或显式放行）：{offenders}")


class TestDemoModeDisablesRealActions(unittest.TestCase):
    """会产生真实变更/费用的页面，必须显式感知演示模式（按钮禁用 + 原因提示）。"""

    FILES = (
        "pages/Settings.tsx",
        "pages/Memory.tsx",
        "pages/Health.tsx",
        "components/ReportViewer.tsx",
        "components/ModeToggle.tsx",
        "components/console/PlanPanel.tsx",
        "components/console/SubmitPanel.tsx",
    )

    def test_files_reference_demo_mode(self):
        missing = [rel for rel in self.FILES
                   if "demoMode" not in (SRC / rel).read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"这些文件有真实写操作但未处理演示模式：{missing}")


class TestNoHookAfterEarlyReturn(unittest.TestCase):
    """同一个组件函数体内，hook 不得出现在早退（`if (…) return …`）之后。

    这是 App.tsx 白屏（React #300/#310）的根因形态：早退之后调 hook，会让登录/加载态翻转时
    本次渲染的 hook 数量与上次不同，React 抛错并卸载整棵树（白屏，或被 ErrorBoundary 兜住显示
    "出错了"）。本轮修 Settings 时又踩过一次，故固化成守卫。

    判定口径：先把文件按**顶层函数/类声明**切块（一个块 ≈ 一个组件），块内要求
    "首个 hook 之后不得再有缩进 ≤2 的早退再接 hook"。函数内部的裸 `return`（缩进更深）不算。
    """

    HOOK = re.compile(
        r"\buse(?:State|Effect|Memo|Callback|Ref|Context|TaskStore|VisibleInterval|DemoRunner|"
        r"Reducer|LayoutEffect|Transition|DeferredValue|SyncExternalStore)\s*\(")
    BLOCK_START = re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:function\s+\w+|memo\(\s*function|const\s+\w+\s*[:=]|class\s+\w+)")
    EARLY_RETURN = re.compile(r"^\s{0,2}(?:if\s*\(.*\)\s*(?:\{\s*)?return\b|\{\s*return\b)")

    def _blocks(self, lines: list[str]) -> list[list[str]]:
        starts = [i for i, l in enumerate(lines) if self.BLOCK_START.match(l)]
        out = []
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(lines)
            out.append(lines[s:e])
        return out or [lines]

    def _offenders_in(self, text: str, label: str) -> list[str]:
        offenders = []
        for block in self._blocks(text.splitlines()):
            first_hook = next((i for i, l in enumerate(block) if self.HOOK.search(l)), None)
            if first_hook is None:
                continue
            early = [i for i, l in enumerate(block)
                     if i > first_hook and self.EARLY_RETURN.match(l)]
            for idx in early:
                for j in range(idx + 1, len(block)):
                    if self.HOOK.search(block[j]):
                        head = block[0].strip()[:44]
                        offenders.append(f"{label} 于「{head}」：早退后仍有 hook（块内第 {j + 1} 行）")
                        break
        return offenders

    def test_no_hook_call_after_early_return(self):
        offenders = []
        for path in _sources():
            offenders += self._offenders_in(path.read_text(encoding="utf-8"),
                                            str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [],
                         f"这些 hook 位于组件体早退之后，会导致 React #300/#310：{offenders[:6]}")

    def test_detector_flags_known_bad_pattern(self):
        """守卫自检：构造已知会崩的写法，必须被抓到（防"守卫失效后无人察觉"）。"""
        bad = (
            "export default function Demo() {\n"
            "  const [loading, setLoading] = useState(true)\n"
            "  if (loading) return <p>loading</p>\n"
            "  const mode = useTaskStore(s => s.demoMode)\n"
            "  return <div>{mode}</div>\n"
            "}\n"
        )
        good = (
            "export default function Demo() {\n"
            "  const [loading, setLoading] = useState(true)\n"
            "  const mode = useTaskStore(s => s.demoMode)\n"
            "  if (loading) return <p>loading</p>\n"
            "  return <div>{mode}</div>\n"
            "}\n"
        )
        self.assertTrue(self._offenders_in(bad, "synthetic-bad"),
                        "自检失败：早退后调 hook 的写法没被抓到")
        self.assertEqual(self._offenders_in(good, "synthetic-good"), [],
                         "自检失败：正确写法被误报")


class TestOverlaysEscapeTransformedAncestors(unittest.TestCase):
    """浮层必须挂到 body（createPortal）。

    报告容器带 `.animate-fade-in`（`will-change: opacity, transform`），而 transform / will-change
    会为 `fixed` 后代新建**包含块**：留在树内的浮层会被拉到容器全高。实测图表放大浮层高 6408px、
    按钮跑到视口之上（top=-106）；既有的分享对话框更糟——top=-5693，用户根本看不到、无法生成分享链接。
    两处都改成 createPortal 后，浮层高度=视口高度、按钮均在视口内。
    """

    OVERLAY_FILES = (
        "components/ReportViewer.tsx",              # 图表放大浮层 + 分享对话框
        "components/AppLayout.tsx",                 # 窄屏「更多」抽屉
        "components/console/ConsoleSideTabs.tsx",   # 重跑确认
    )

    def test_overlay_files_import_portal(self):
        missing = [rel for rel in self.OVERLAY_FILES
                   if "createPortal" not in (SRC / rel).read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"这些文件有全屏浮层但没走 createPortal：{missing}")

    def test_every_fullscreen_overlay_is_portaled(self):
        bad = []
        for rel in self.OVERLAY_FILES:
            src = (SRC / rel).read_text(encoding="utf-8")
            fixed = src.count("fixed inset-0")
            portals = src.count("createPortal(")
            if fixed > portals:
                bad.append(f"{rel}: fixed inset-0 ×{fixed} > createPortal ×{portals}")
        self.assertEqual(bad, [], f"有全屏浮层没挂到 body（会被祖先 transform 拉长）：{bad}")


class TestReportPageWiring(unittest.TestCase):
    """报告页必须渲染后端**早就在返回**的证据字段（F3a 之前这些字段一个都没上屏）。"""

    def setUp(self):
        self.text = (SRC / "components" / "ReportViewer.tsx").read_text(encoding="utf-8")

    def test_traceability_fields_rendered(self):
        for field in ("covered_ratio", "amount_rate", "untraceable", "unverifiable_count"):
            self.assertIn(field, self.text, f"数字可信度卡缺少 {field}")

    def test_fingerprint_fields_rendered(self):
        for field in ("rules_version", "rules_fingerprint", "report_sha256", "evaluated_at"):
            self.assertIn(field, self.text, f"证据指纹缺少 {field}")

    def test_timeline_endpoint_wired(self):
        self.assertIn("/acceptance/timeline", self.text, "验收时间线未接线")

    def test_top_stats_matches_share_page_convention(self):
        """结论卡与分享页同口径：列取表头含"指标"/"数值"者，键截 24 字、值截 32 字。"""
        self.assertIn("parseTopStats", self.text)
        self.assertIn("slice(0, 24)", self.text)
        self.assertIn("slice(0, 32)", self.text)

    def test_chart_figure_features(self):
        self.assertIn("草稿级", self.text, "缺少草稿图标签")
        self.assertIn("Escape", self.text, "图表放大缺 Esc 关闭")
        self.assertIn("下载图片", self.text, "缺单图下载")


class TestReportExportBinding(unittest.TestCase):
    """导出必须绑定"哪一版正文"（M0-a）：Markdown 也从服务端取字节，
    否则清单里没有它的 hash，页面/PDF/清单就可能指向不同版本。"""

    def setUp(self):
        self.text = (SRC / "components" / "ReportViewer.tsx").read_text(encoding="utf-8")

    def test_markdown_download_uses_server_export(self):
        body = self.text.split("const downloadMarkdown", 1)[1].split("\n  }", 1)[0]
        self.assertIn("/report.md", body, "Markdown 下载未走服务端导出（清单拿不到 hash）")

    def test_print_path_reads_version_headers(self):
        self.assertIn("X-Report-Version-Id", self.text, "打印件未标注导出对应版本")
        self.assertIn("X-Report-Draft", self.text, "打印件未标注未验收草稿")

    def test_print_body_comes_from_served_export(self):
        """打印页正文优先取服务端同一份（内存稿只作兜底），且仍先转义再套标签。"""
        self.assertIn("esc(printBody)", self.text)
        self.assertNotIn("innerHTML = esc(report.final_report)", self.text)


class TestMobileEntryPoints(unittest.TestCase):
    """窄屏必须有演示开关与退出登录入口：此前两者都带 hidden sm:flex / hidden md:flex，
    <640px 完全够不到（模拟实测发现）。"""

    def setUp(self):
        self.text = (SRC / "components" / "AppLayout.tsx").read_text(encoding="utf-8")

    def test_bottom_nav_trimmed_with_more_entry(self):
        self.assertIn("MOBILE_PRIMARY", self.text, "底栏未做 4+更多 收敛")
        self.assertIn("MOBILE_MORE", self.text, "缺少其余页面的窄屏入口")

    def test_mobile_drawer_has_demo_and_logout(self):
        for label in ("退出登录", "演示模式"):
            self.assertIn(label, self.text, f"窄屏抽屉缺少 {label}")


class TestStepInspectorHygiene(unittest.TestCase):
    """步骤详情：中文字段、结果白名单、本机路径脱敏、原始 JSON 默认折叠。"""

    def setUp(self):
        self.text = (SRC / "components" / "StepInspector.tsx").read_text(encoding="utf-8")

    def test_paths_are_redacted(self):
        self.assertIn("export function redactPaths", self.text)
        self.assertGreaterEqual(self.text.count("redactPaths("), 4,
                                "脱敏函数定义了却没在实际渲染路径上使用")

    def test_raw_json_is_opt_in(self):
        self.assertIn("查看原始 JSON", self.text)
        self.assertIn("useState(false)", self.text, "原始 JSON 不应默认展开")

    def test_chinese_labels_present(self):
        for label in ("步骤详情", "能力", "执行体", "指令", "执行结果"):
            self.assertIn(label, self.text, f"步骤详情缺少中文标签：{label}")

    def test_no_english_field_labels_left(self):
        for legacy in (">Step Details<", ">Capability<", ">Instruction<", ">Result<", ">Decision Trace<"):
            self.assertNotIn(legacy, self.text, f"仍有英文标签 {legacy}")


class TestRerunRequiresConfirmation(unittest.TestCase):
    """重跑会重新规划并消耗额度，必须先确认（此前点一下就跑）。"""

    def test_rerun_opens_confirmation(self):
        text = (SRC / "components" / "console" / "ConsoleSideTabs.tsx").read_text(encoding="utf-8")
        self.assertIn("确认重跑", text, "重跑没有二次确认")
        self.assertNotIn("onClick={() => onSubmit(m.goal)}", text,
                         "重跑按钮又变回直接提交（绕过确认）")


class TestExportVersionNamespaces(unittest.TestCase):
    """面板"包与当前不同版"提示必须按正文版号比较（与复核栏同一口径）。

    起因（浏览器实测）：复核栏显示"本版 65ec49d1…"（正文 SHA256），导出行却显示
    "当前版本 20719ee1…"（谱系标识 identity_id）——同一份正文在同一页出现两个
    "当前版本"。钉死：面板比较/展示用 package_body_version_id / current_body_version_id，
    谱系标识只作老后端的回退；后端导出块必须给出这两字段。
    """

    def test_panel_stale_package_note_prefers_body_version_ids(self):
        text = (SRC / "components" / "ResearchBriefPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("package_body_version_id", text, "面板未按正文版号比较包的版本")
        self.assertIn("current_body_version_id", text, "面板未展示当前正文版号")
        # 谱系标识只允许作回退（老后端没有新字段时）
        self.assertIn("|| exportState.manifest_version_id", text)
        self.assertIn("|| exportState.current_version_id", text)

    def test_export_state_type_declares_body_version_ids(self):
        text = (SRC / "stores" / "types.ts").read_text(encoding="utf-8")
        self.assertIn("package_body_version_id?: string", text)
        self.assertIn("current_body_version_id?: string", text)

    def test_backend_export_block_provides_body_version_ids(self):
        text = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn('"package_body_version_id"', text)
        self.assertIn('"current_body_version_id"', text)
        self.assertIn("export = _export_payload(tid, _ws, _state)", text,
                      "任务页导出块未走 _export_payload（字段可能悄悄丢回单一口径）")


class TestFilesUrlSegmentEncoding(unittest.TestCase):
    """/files/<tid>/<rel> 的 rel 必须**逐段**编码（浏览器实测教训）。

    后端 /files 路由不做 URL 解码（`urlparse(self.path).path` 原样使用），
    整段 encodeURIComponent("charts/chart_1.png") 会把斜杠编成 %2F → 404，
    结果页正文图表与生成文件下载全部挂掉。钉死：拼 /files URL 时不得对
    含斜杠的相对路径整体 encodeURIComponent。
    """

    def test_report_viewer_encodes_files_rel_per_segment(self):
        text = (SRC / "components" / "ReportViewer.tsx").read_text(encoding="utf-8")
        self.assertNotIn("encodeURIComponent(raw)}`", text,
                         "图片链接又变回整段编码（%2F 会 404）")
        self.assertNotIn("encodeURIComponent(name)", text,
                         "文件下载链接又变回整段编码（%2F 会 404）")
        self.assertIn("raw.split('/').map(encodeURIComponent).join('/')", text)
        self.assertIn("name.split('/').map(encodeURIComponent).join('/')", text)

    def test_report_image_placeholders_extreme_aspect(self):
        """超高画布（chart_qa.MAX_CANVAS_ASPECT=12 的历史异常产物）不得在页面硬嵌。

        浏览器实测：1038×121366 的旧图按 max-h-[420px] 缩放后只剩 ~3.6px 宽，
        嵌在页里等于隐形。必须换成如实占位说明（尺寸+原因+可下载）。"""
        text = (SRC / "components" / "ReportViewer.tsx").read_text(encoding="utf-8")
        self.assertIn("dims.h / dims.w > 12", text, "缺少与 chart_qa 同阈值的纵横比检查")
        self.assertIn("图片尺寸异常", text, "超高画布没有如实占位说明")
        self.assertIn("naturalWidth", text, "未读取图片真实尺寸（onLoad naturalWidth）")


if __name__ == "__main__":
    unittest.main()
