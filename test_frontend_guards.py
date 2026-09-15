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


class TestDemoGuardCoverage(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
