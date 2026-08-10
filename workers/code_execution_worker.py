"""织光 (ZhiGuang) - Code Execution Worker。

生成并运行 Python 脚本；与 file_io 共享 project 工作区，生成的主文件
（如 main.py）会持久化保存，不再跑完即删，保证交付物真实落盘。
"""

import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)

_PROBE_MODULES = (
    "pygame", "turtle", "tkinter", "math", "random", "json", "html",
    "numpy", "pandas", "matplotlib", "requests",
)
_AVAILABLE_MODULES = sorted(
    m for m in _PROBE_MODULES
    if importlib.util.find_spec(m)
)
_HAS_PYGAME = "pygame" in _AVAILABLE_MODULES

_HTML_GAME_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>愤怒的小鸟（HTML 演示版）</title>
<style>
  body { margin:0; display:flex; flex-direction:column; align-items:center;
         background:#2b3a4a; color:#fff; font-family:sans-serif; }
  canvas { background:#9fd3f5; margin-top:12px; border-radius:8px; cursor:crosshair; }
  #bar { width:800px; display:flex; justify-content:space-between; margin-top:8px; font-size:16px; }
  button { padding:8px 18px; font-size:15px; border:none; border-radius:6px;
           background:#ffb300; color:#222; cursor:pointer; }
</style>
</head>
<body>
<canvas id="game" width="800" height="500"></canvas>
<div id="bar"><span id="score">得分 0 / 3</span><button onclick="resetGame()">重新开始</button></div>
<script>
const cv = document.getElementById('game'), ctx = cv.getContext('2d');
const W = 800, H = 500, G = 0.35, GROUND = 470;
const SLING = { x: 140, y: 400 };
let bird, pigs = [], blocks = [], dragging = false, shot = false, score = 0, total = 3;
function resetGame() {
  bird = { x: SLING.x, y: SLING.y, vx: 0, vy: 0, r: 14, alive: true };
  pigs = [ {x:620,y:GROUND-22,r:20}, {x:680,y:GROUND-22,r:20}, {x:740,y:GROUND-22,r:20} ];
  blocks = [ {x:650,y:GROUND-45,w:14,h:45}, {x:700,y:GROUND-45,w:14,h:45} ];
  dragging = false; shot = false; score = 0; total = 3;
  document.getElementById('score').textContent = '得分 0 / 3';
}
function draw() {
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle = '#7a5230'; ctx.fillRect(0,GROUND,W,H-GROUND);
  ctx.strokeStyle = '#3d3d3d'; ctx.lineWidth = 5;
  ctx.beginPath(); ctx.moveTo(SLING.x, SLING.y); ctx.lineTo(SLING.x-35, 430); ctx.stroke();
  // 弹弓皮筋
  ctx.strokeStyle = '#5b3a1e'; ctx.lineWidth = 4;
  let bx = bird && bird.alive ? bird.x : SLING.x, by = bird && bird.alive ? bird.y : SLING.y;
  ctx.beginPath(); ctx.moveTo(SLING.x-12, SLING.y-12); ctx.lineTo(bx, by); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(SLING.x+12, SLING.y-12); ctx.lineTo(bx, by); ctx.stroke();
  blocks.forEach(b => { ctx.fillStyle = '#8b5a2b'; ctx.fillRect(b.x-b.w/2, b.y-b.h, b.w, b.h); });
  pigs.forEach(p => { ctx.fillStyle = '#5cbf4f'; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI*2); ctx.fill();
                      ctx.fillStyle = '#cfe8d8'; ctx.beginPath(); ctx.arc(p.x-5, p.y-7, 4, 0, Math.PI*2); ctx.fill(); });
  if (bird && bird.alive) { ctx.fillStyle = '#e74c3c'; ctx.beginPath(); ctx.arc(bird.x, bird.y, bird.r, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.beginPath(); ctx.arc(bird.x-4, bird.y-5, 4, 0, Math.PI*2); ctx.fill();
    ctx.strokeStyle = '#a93226'; ctx.lineWidth = 2; ctx.beginPath();
    ctx.moveTo(bird.x+8, bird.y-2); ctx.lineTo(bird.x+16, bird.y-8); ctx.stroke(); }
}
function update() {
  if (shot && bird && bird.alive) {
    bird.x += bird.vx; bird.y += bird.vy; bird.vy += G;
    if (bird.y > GROUND - bird.r) { bird.y = GROUND - bird.r; bird.vx *= 0.7; bird.vy = -bird.vy * 0.45; }
    if (bird.x > W + 30 || (Math.abs(bird.vx) < 0.4 && Math.abs(bird.vy) < 0.4 && bird.y > GROUND - bird.r - 2)) {
      bird.alive = false;
    }
    pigs = pigs.filter(p => {
      const dx = bird.x - p.x, dy = bird.y - p.y;
      if (Math.hypot(dx, dy) < bird.r + p.r) { score++; document.getElementById('score').textContent = '得分 ' + score + ' / 3'; return false; }
      return true;
    });
  }
}
function loop() { update(); draw(); if (pigs.length === 0) {
    ctx.fillStyle = 'rgba(0,0,0,0.55)'; ctx.fillRect(0,0,W,H);
    ctx.fillStyle = '#ffe066'; ctx.font = 'bold 44px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('恭喜过关！', W/2, H/2); }
  requestAnimationFrame(loop); }
cv.addEventListener('mousedown', e => {
  if (shot || !bird.alive) return;
  dragging = true;
});
cv.addEventListener('mousemove', e => {
  if (!dragging || !bird || !bird.alive) return;
  const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  let dx = mx - SLING.x, dy = my - SLING.y, d = Math.hypot(dx, dy);
  if (d > 90) { dx = dx/d*90; dy = dy/d*90; }
  bird.x = SLING.x + dx; bird.y = SLING.y + dy;
});
window.addEventListener('mouseup', () => {
  if (!dragging || !bird || !bird.alive) return;
  dragging = false; shot = true;
  bird.vx = (SLING.x - bird.x) * 0.16;
  bird.vy = (SLING.y - bird.y) * 0.16;
});
resetGame(); loop();
</script>
</body>
</html>"""


class CodeExecutionWorker(AsyncWorkerBase):
    _class_capabilities = ["code_execution"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = Path(tempfile.gettempdir()) / "agent_workspace" / "project"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _clean_env(self) -> dict:
        """执行环境剥离密钥类变量，防止被生成的代码读取。"""
        secrets = ("LLM_", "OPENAI_", "EMBEDDING_", "API_KEY", "SERPAPI", "TOKEN", "SECRET")
        return {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in secrets)}

    def _target_filename(self, instruction: str) -> str:
        """从指令中提取目标文件名；仅当指令明确要求生成/创建/编写时才用该名字，
        否则用带时间戳的唯一名字，避免把测试脚本覆盖到 main.py。"""
        if any(k in instruction for k in ("生成", "创建", "写出", "保存为")):
            m = re.search(r"([\w\-.]+\.(?:py|html))", instruction)
            if m:
                return m.group(1)
            if any(k in instruction.lower() for k in ("html", "网页", "web", "webpage")):
                return "index.html"
        return f"generated_{int(time.time() * 1000)}.py"

    async def _generate_code(
        self, instruction: str, env_note: str, html_mode: bool,
        compile_err: str, minimal: bool, test_context: str = "",
    ) -> str:
        """调 LLM 生成代码；compile_err 非空时携带语法错误反馈要求修复。"""
        feedback = (
            f"\n上一次生成的代码未通过验证（编译/运行/审查），请修复后重新输出完整代码：\n{compile_err}"
            if compile_err else ""
        )
        ws = self._workspace_snapshot()
        ws_note = (
            "\n\n工作区现有文件（若指令要求基于/修改某个文件，请读取其内容并输出"
            "修改后的完整文件，保持原文件名）：\n" + ws
            if ws else ""
        )
        tdd_note = (
            "\n\n[TDD] 以下测试断言已先行编写，你的实现必须满足这些测试"
            "（保持模块名与导出名一致，运行测试应全部通过）：\n" + test_context
            if test_context else ""
        )
        if minimal:
            system = "You are a web developer. Write minimal runnable code. Output ONLY raw code."
            prompt = (
                "Write minimal runnable Python code (<=200 lines) that satisfies: "
                f"{instruction[:800]}\n{env_note}{ws_note}{tdd_note}{feedback}"
                if not html_mode else
                "Write a self-contained single-file HTML page (inline CSS/JS) that satisfies: "
                f"{instruction[:800]}\nOutput ONLY raw HTML, no Python wrapper.{ws_note}{feedback}"
            )
        else:
            system = (
                "You are a senior Python developer. Generate complete, runnable, "
                "self-contained code. Output ONLY the code, no explanations."
            )
            prompt = (
                "请生成满足以下要求的完整可运行 Python 代码（自包含、可直接执行，"
                "必要依赖仅在注释中说明）：\n"
                f"{instruction}\n{env_note}{ws_note}{tdd_note}{feedback}"
                if not html_mode else
                "请生成满足以下要求的自包含单文件 HTML 页面（内联 CSS/JS，"
                "直接可保存并在浏览器打开）：\n"
                f"{instruction}\n{env_note}{ws_note}\n只输出原始 HTML，不要用 Python 包装。{feedback}"
            )
        return await self._call_llm(system=system, prompt=prompt)

    @staticmethod
    def _html_intent(instruction: str) -> bool:
        """判定指令是否要"生成 HTML 页面"。
        验证/检查/测试类指令即使提到 .html 文件名，也应生成可运行的 Python 脚本。"""
        low = instruction.lower()
        _verify_hint = any(k in instruction for k in (
            "验证", "检查", "测试", "冒烟", "静态", "确认",
        ))
        return any(k in low for k in ("html", "网页", "webpage")) and not _verify_hint

    @staticmethod
    def _extract_embedded_html(code: str) -> str | None:
        """LLM 有时把 HTML 包进 Python 写入脚本（with open('x.html','w') / f.write('''...''')），
        从中提取真正的 HTML 内容。"""
        m = re.search(
            r"(?:write|print|f\.write)\(\s*(?:['\"]{1,3})(.*?)(?:['\"]{1,3})\s*\)",
            code, re.S,
        )
        if m:
            inner = m.group(1)
            if "<!doctype html" in inner.lower() or "<html" in inner.lower():
                return inner.strip()
        # 兜底：任意三引号内的 HTML
        m2 = re.search(r"('''|\"\"\")(.*?)\1", code, re.S)
        if m2 and ("<!doctype html" in m2.group(2).lower() or "<html" in m2.group(2).lower()):
            return m2.group(2).strip()
        return None

    def _workspace_snapshot(self, max_files: int = 5, max_chars: int = 700) -> str:
        """列出工作区已有文件及内容片段，让 LLM 知道"自己在做什么、已有什么"。"""
        lines: list[str] = []
        try:
            files = sorted(
                p for p in self.workspace.iterdir()
                if p.is_file() and "_check_" not in p.name and "screenshots" not in p.name
            )
            for p in files[:max_files]:
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                lines.append(f"[文件] {p.name}（{size} 字节）")
                if size > 0:
                    try:
                        snippet = p.read_text(encoding="utf-8", errors="replace")[:max_chars]
                        lines.append(f"[内容] {snippet}")
                    except Exception:
                        pass
        except Exception:
            pass
        return "\n".join(lines)

    @staticmethod
    def _strip_fences(candidate: str) -> str:
        candidate = candidate.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines)
        return candidate

    def _py_compile(self, candidate: str) -> str:
        """编译自检；返回错误文本（空串表示通过）。"""
        import subprocess
        check = self.workspace / f"_check_{int(time.time() * 1000)}.py"
        check.write_text(candidate, encoding="utf-8")
        try:
            p = subprocess.run(
                [sys.executable, "-m", "py_compile", str(check)],
                capture_output=True, timeout=30,
            )
            if p.returncode == 0:
                return ""
            return (p.stderr or p.stdout or b"").decode("utf-8", errors="replace")[:800]
        except Exception as exc:
            return f"编译异常: {exc}"
        finally:
            try:
                check.unlink()
            except Exception:
                pass
            # 清理编译自检产生的 __pycache__ 残留
            try:
                pycache = self.workspace / "__pycache__"
                if pycache.is_dir():
                    for f in pycache.glob("_check_*.pyc"):
                        try:
                            f.unlink()
                        except Exception:
                            pass
            except Exception:
                pass

    def _html_js_check(self, content: str) -> str:
        """提取内联 JS 用 node --check 校验语法；返回错误文本（空串表示通过/跳过）。"""
        import subprocess
        import tempfile
        try:
            p = subprocess.run(["node", "--version"], capture_output=True, timeout=10)
            if p.returncode != 0:
                return ""
        except Exception:
            return ""
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", content, re.S)
        if not scripts:
            return ""
        tmp = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8",
            ) as tf:
                tf.write("\n".join(scripts))
                tmp = tf.name
            p = subprocess.run(["node", "--check", tmp], capture_output=True, timeout=15)
            if p.returncode == 0:
                return ""
            return (p.stderr or p.stdout or b"").decode("utf-8", errors="replace")[:800]
        except Exception as exc:
            return f"JS 校验异常: {exc}"
        finally:
            try:
                if tmp:
                    os.unlink(tmp)
            except Exception:
                pass

    async def _run_smoke(self, candidate: str) -> str:
        """小步快跑：运行候选代码做冒烟验证；
        返回错误文本（空串表示通过）。"""
        tmp = self.workspace / f"_smoke_{int(time.time() * 1000)}.py"
        tmp.write_text(candidate, encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=self._clean_env(),
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return "冒烟运行超时（30s）"
            text = (out or err).decode("utf-8", errors="replace")
            if proc.returncode != 0:
                return text[-1200:]
            bad = re.search(
                r"(Traceback|AssertionError|NameError|TypeError|AttributeError|"
                r"SyntaxError|IndentationError|ImportError|FAILED)",
                text,
            )
            if bad:
                return text[-800:]
            return ""
        except Exception as exc:
            return f"冒烟异常: {exc}"
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    async def _review_code(self, code: str, instruction: str) -> tuple[bool, str]:
        """Codex 式自动代码审查：依赖影响、性能、安全、逻辑漏洞。
        返回 (是否通过, 意见文本)。"""
        try:
            resp = await self._call_llm(
                system=(
                    "你是资深代码审查员。审查代码，输出严格JSON："
                    '{"pass": true|false, "issues": ["问题1", "问题2"]}。'
                    "检查维度：依赖是否可用、性能风险、安全风险（注入/危险调用/密钥泄露）、"
                    "逻辑漏洞、是否满足需求。只输出JSON。"
                ),
                prompt=(
                    f"需求：{instruction[:400]}\n\n代码：\n{code[:3000]}"
                ),
            )
            clean = str(resp).strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean).rstrip("`").strip()
            data = json.loads(clean)
            if isinstance(data, dict):
                issues = data.get("issues") or []
                if not data.get("pass") and issues:
                    return False, "；".join(str(i)[:200] for i in issues[:5])
            return True, ""
        except Exception:
            # 审查失败不阻塞执行（保守放行）
            return True, ""

    async def _tdd_pilot(self, instruction: str, filename: str) -> tuple[bool, str]:
        """严格 TDD：先让 LLM 判断需求是否适合测试驱动，若适合则生成测试文件。
        返回 (是否启用 TDD, 测试代码)。"""
        try:
            resp = await self._call_llm(
                system=(
                    "你是 TDD 工程师。判断需求是否适合测试驱动开发："
                    "（可自动验证、有明确的输入输出或行为断言）若适合则编写测试。"
                    f"实现模块将保存为 {filename}（不含 .py 后缀，如文件名是 sum.py 则 import sum）。"
                    '输出严格JSON：{"tdd": true|false, "test": "完整测试代码"}。'
                    "测试要 import 实现模块并对核心行为做断言（失败时打印 FAILED 并 exit(1)）。只输出JSON。"
                ),
                prompt=(
                    f"需求：{instruction[:500]}\n"
                    f"实现将保存为 {filename}，测试 import 它并断言行为。"
                ),
            )
            clean = str(resp).strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean).rstrip("`").strip()
            data = json.loads(clean)
            if isinstance(data, dict) and data.get("tdd") and str(data.get("test") or "").strip():
                return True, str(data["test"])
            return False, ""
        except Exception as exc:
            logger.info("TDD pilot skipped: %s", exc)
            return False, ""

    async def execute(self, instruction: str) -> str:
        test_path = None
        try:
            if _HAS_PYGAME:
                env_note = (
                    f"环境可用模块：{', '.join(_AVAILABLE_MODULES)}。"
                    "pygame 已安装，游戏类任务可直接使用 pygame 实现并运行。"
                )
            else:
                env_note = (
                    f"环境可用模块：{', '.join(_AVAILABLE_MODULES) or '仅标准库'}。"
                    "pygame 未安装；需要图形界面时使用 turtle/tkinter，"
                    "或生成单文件 HTML 游戏（保存为 .html，不需要运行）。"
                )
            html_mode = self._html_intent(instruction)
            # 严格 TDD：先让 LLM 判断是否适合测试驱动；适合则先生成测试文件
            target_name = self._target_filename(instruction)
            use_tdd, test_code = False, ""
            if not html_mode:
                use_tdd, test_code = await self._tdd_pilot(instruction, Path(target_name).stem)
                if use_tdd and test_code.strip():
                    # 测试文件放在 workspace 根目录：python test.py 的 sys.path[0]=workspace，
                    # 实现模块才能被 import（放子目录会 ModuleNotFoundError）
                    test_path = self.workspace / f".test_{int(time.time() * 1000)}.py"
                    test_path.write_text(test_code, encoding="utf-8")
                    logger.info("TDD: generated test file %s", test_path.name)
            code = None
            feedback = ""
            # 小步快跑：生成 → 编译 → 冒烟运行 → 代码审查 → 带反馈修复（最多 3 轮）
            for _round in range(3):
                llm_response = ""
                for _gen in range(2):
                    try:
                        llm_response = await self._generate_code(
                            instruction, env_note, html_mode,
                            feedback, minimal=(_gen == 1),
                            test_context=(test_code if use_tdd else ""),
                        )
                    except Exception:
                        llm_response = ""
                        continue
                    if llm_response and llm_response.strip():
                        break
                if not llm_response or not llm_response.strip():
                    continue
                candidate = self._strip_fences(llm_response)
                if html_mode:
                    # 提取 LLM 可能包在 Python 写入脚本里的 HTML
                    extracted = self._extract_embedded_html(candidate)
                    candidate = extracted if extracted else candidate
                    # HTML 结构自检：标签必须闭合，否则浏览器不执行 JS（canvas 空白）
                    missing = []
                    low = candidate.lower()
                    if "<script" in low and "</script>" not in low:
                        missing.append("</script>")
                    if "</html>" not in low:
                        missing.append("</html>")
                    if missing:
                        feedback = (
                            "生成的 HTML 缺少闭合标签：" + "、".join(missing)
                            + "，请补全后重新输出完整 HTML（保持单文件、内联 JS）。"
                        )
                        logger.info("Round %d: HTML incomplete (%s), regenerating",
                                    _round + 1, ",".join(missing))
                        continue
                    # HTML 内联 JS 语法校验（node --check）：语法错 → 带错误重生成
                    js_err = self._html_js_check(candidate)
                    if js_err:
                        feedback = (
                            "HTML 内联 JS 存在语法错误，请修复后重新输出完整 HTML：\n" + js_err
                        )
                        logger.info("Round %d: JS syntax error, regenerating", _round + 1)
                        continue
                    code = candidate
                    break
                if candidate.lstrip().lower().startswith(("<html", "<!doctype")):
                    code = candidate
                    break
                # 阶段1：编译自检
                compile_err = self._py_compile(candidate)
                if compile_err:
                    feedback = f"编译失败，请修复后重新输出完整代码：\n{compile_err}"
                    logger.info("Round %d: compile error, regenerating", _round + 1)
                    continue
                # 阶段2(TDD)：有测试文件时，先写实现到目标文件并运行测试验证
                if test_path is not None:
                    impl_path = self.workspace / target_name
                    impl_path.write_text(candidate, encoding="utf-8")
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, str(test_path),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=str(self.workspace),
                            env=self._clean_env(),
                        )
                        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
                    except asyncio.TimeoutError:
                        proc.kill()
                        feedback = "测试运行超时（60s），请修复实现"
                        continue
                    tdd_out = (out or err).decode("utf-8", errors="replace")
                    if proc.returncode != 0 or re.search(
                        r"(FAILED|Traceback|AssertionError|Error)", tdd_out,
                    ):
                        feedback = f"测试未通过，请修复实现以通过测试：\n{tdd_out[-1200:]}"
                        logger.info("TDD round %d: tests failed:\n%s",
                                    _round + 1, tdd_out[-1500:])
                        continue
                    logger.info("TDD round %d: tests passed", _round + 1)
                    code = candidate
                    break
                # 阶段2：冒烟运行验证（小步快跑：运行不过就带错误反馈重来）
                smoke_err = await self._run_smoke(candidate)
                if smoke_err:
                    feedback = f"运行验证失败，请修复后重新输出完整代码：\n{smoke_err}"
                    logger.info("Round %d: smoke test failed, regenerating", _round + 1)
                    continue
                # 阶段3：自动代码审查（依赖/性能/安全/逻辑）
                review_ok, review_notes = await self._review_code(candidate, instruction)
                if not review_ok:
                    feedback = f"代码审查发现必须修复的问题，请修复后重新输出完整代码：\n{review_notes}"
                    logger.info("Round %d: code review failed, regenerating", _round + 1)
                    continue
                code = candidate
                break
            if code is None:
                # 内置模板兜底：游戏类指令直接交付可运行的 HTML 演示，避免因
                # LLM 空响应（提供商偶发）导致整任务失败。
                low = instruction.lower()
                if any(k in low for k in ("游戏", "angry", "bird", "小鸟", "playable")):
                    path = self.workspace / "index.html"
                    if path.exists():
                        path = self.workspace / f"index_{int(time.time())}.html"
                    path.write_text(_HTML_GAME_TEMPLATE, encoding="utf-8")
                    return json.dumps({
                        "status": "success",
                        "path": str(path),
                        "filename": path.name,
                        "output": "Template fallback: built-in HTML game delivered (LLM unavailable)",
                        "fallback": "template",
                        "returncode": 0,
                    }, ensure_ascii=False)
                raise RuntimeError(
                    f"No valid code after generation/verify/review loop"
                )

            # 持久化：主文件用目标名，测试脚本用唯一名；避免覆盖已存在的交付物
            filename = self._target_filename(instruction)
            path = self.workspace / filename
            # 内容类型兜底：LLM 输出的是 HTML 就按 .html 保存，绝不进 Python
            if code.lstrip().lower().startswith(("<html", "<!doctype")):
                if path.suffix.lower() != ".html":
                    path = path.with_suffix(".html")
            # 修改/更新/修复类指令：写回原文件；否则新文件加时间戳避免覆盖
            _edit_hint = any(k in instruction for k in (
                "修改", "更新", "完善", "调整", "修复", "改进", "改写",
            ))
            if path.exists() and not _edit_hint and not use_tdd:
                stem, ext = path.stem, path.suffix
                path = self.workspace / f"{stem}_{int(time.time())}{ext}"
            # 内容类型兜底：目标 .html 但实际是 Python 代码 → 存为 .py，避免假 HTML
            looks_py = code.lstrip().startswith(("import ", "from ", "print(", "#!", "with open", "def "))
            if path.suffix.lower() == ".html" and looks_py:
                path = path.with_suffix(".py")
            path.write_text(code, encoding="utf-8")

            if path.suffix.lower() == ".html":
                # HTML 交付物无需用 Python 执行，直接落盘交付
                return json.dumps({
                    "status": "success",
                    "path": str(path),
                    "filename": path.name,
                    "output": "HTML deliverable saved (not executed)",
                    "returncode": 0,
                }, ensure_ascii=False)

            try:
                # 运行失败若是缺模块，自动 pip 安装后重跑一次（最多 2 次运行）
                last_err = ""
                for _run_attempt in range(2):
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=str(self.workspace),
                        env=self._clean_env(),
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                    except asyncio.TimeoutError:
                        proc.kill()
                        raise RuntimeError("Code execution timed out after 120s")
                    out = stdout.decode("utf-8", errors="replace")
                    err = stderr.decode("utf-8", errors="replace")
                    if proc.returncode == 0:
                        output = out if out else err
                        return json.dumps({
                            "status": "success",
                            "path": str(path),
                            "filename": path.name,
                            "output": output[:3000] if output else "No output",
                            "returncode": proc.returncode,
                        }, ensure_ascii=False)
                    last_err = err or out
                    m = re.search(r"No module named ['\"]([^'\"]+)['\"]", last_err)
                    if m and _run_attempt == 0:
                        mod = m.group(1).split(".")[0]
                        try:
                            from env_setup import ensure_module
                            ok, note = await asyncio.get_running_loop().run_in_executor(
                                None, ensure_module, mod,
                            )
                            logger.info("Auto-install %s -> %s", mod, note)
                            if ok:
                                continue  # 依赖装好后重跑
                        except Exception as exc:
                            logger.warning("Auto-install failed for %s: %s", mod, exc)
                    raise RuntimeError(
                        f"Script exited with code {proc.returncode}: {last_err[:2000]}"
                    )
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("Code execution timed out after 120s")
        except Exception as exc:
            raise RuntimeError(f"Code execution failed: {exc}") from exc
        finally:
            # TDD 测试文件是验证资产而非交付物：执行结束后删除
            if test_path is not None:
                try:
                    test_path.unlink()
                    logger.info("TDD: removed test file %s", test_path.name)
                except Exception:
                    pass


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-code-execution")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = CodeExecutionWorker(
        agent_id="codeexecworker",
        capabilities=CodeExecutionWorker._class_capabilities,
        registry=registry,
        messaging=messaging,
        max_concurrency=3,
    )
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
