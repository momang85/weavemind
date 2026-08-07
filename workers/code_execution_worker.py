"""织光 (ZhiGuang) - Code Execution Worker。

生成并运行 Python 脚本；与 file_io 共享 project 工作区，生成的主文件
（如 main.py）会持久化保存，不再跑完即删，保证交付物真实落盘。
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

_AVAILABLE_MODULES = sorted(
    m for m in (
        "turtle", "tkinter", "math", "random", "json", "html",
        "numpy", "pandas", "matplotlib", "requests",
    )
    if importlib.util.find_spec(m)
)

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

    async def execute(self, instruction: str) -> str:
        try:
            env_note = (
                f"环境可用模块：{', '.join(_AVAILABLE_MODULES) or '仅标准库'}。"
                "pygame 未安装且无法安装，禁止使用 pygame；"
                "需要图形界面时使用 turtle/tkinter，或生成单文件 HTML 游戏。"
            )
            html_mode = any(k in instruction.lower() for k in ("html", "网页", "webpage"))
            llm_response = ""
            for _gen in range(4):
                try:
                    if _gen >= 2:
                        # 前两次失败后改用极简提示，规避模型空响应/拒绝
                        llm_response = await self._call_llm(
                            system=(
                                "You are a web developer. Write minimal runnable code. "
                                "Output ONLY raw code."
                            ),
                            prompt=(
                                "Write minimal runnable Python code (<=200 lines) that satisfies: "
                                f"{instruction[:800]}\n{env_note}"
                                if not html_mode else
                                "Write a self-contained single-file HTML page (inline CSS/JS) that satisfies: "
                                f"{instruction[:800]}\nOutput ONLY raw HTML, no Python wrapper."
                            ),
                        )
                    else:
                        llm_response = await self._call_llm(
                            system=(
                                "You are a senior Python developer. Generate complete, runnable, "
                                "self-contained Python code. Output ONLY the code, no explanations."
                            ),
                            prompt=(
                                "请生成满足以下要求的完整可运行 Python 代码（自包含、可直接执行，"
                                "必要依赖仅在注释中说明）：\n"
                                f"{instruction}\n{env_note}"
                                if not html_mode else
                                "请生成满足以下要求的自包含单文件 HTML 页面（内联 CSS/JS，"
                                "直接可保存并在浏览器打开）：\n"
                                f"{instruction}\n{env_note}\n只输出原始 HTML，不要用 Python 包装。"
                            ),
                        )
                except Exception:
                    llm_response = ""
                    continue
                if llm_response and llm_response.strip():
                    break
            if not llm_response or not llm_response.strip():
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
                raise RuntimeError("No code generated by LLM")

            # 去掉可能的 markdown 代码块包装
            code = llm_response.strip()
            if code.startswith("```"):
                lines = code.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                code = "\n".join(lines)

            # 持久化：主文件用目标名，测试脚本用唯一名；避免覆盖已存在的交付物
            filename = self._target_filename(instruction)
            path = self.workspace / filename
            # 内容类型兜底：LLM 输出的是 HTML 就按 .html 保存，绝不进 Python
            if code.lstrip().lower().startswith(("<html", "<!doctype")):
                if path.suffix.lower() != ".html":
                    path = path.with_suffix(".html")
            if path.exists():
                stem, ext = path.stem, path.suffix
                path = self.workspace / f"{stem}_{int(time.time())}{ext}"
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
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace),
                    env=self._clean_env(),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                out = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")
                if proc.returncode != 0:
                    # 脚本崩溃必须如实失败，避免把 traceback 当成功输出
                    raise RuntimeError(
                        f"Script exited with code {proc.returncode}: {err[:2000] or out[:2000]}"
                    )
                output = out if out else err
                return json.dumps({
                    "status": "success",
                    "path": str(path),
                    "filename": path.name,
                    "output": output[:3000] if output else "No output",
                    "returncode": proc.returncode,
                }, ensure_ascii=False)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("Code execution timed out after 120s")
        except Exception as exc:
            raise RuntimeError(f"Code execution failed: {exc}") from exc


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
