# -*- coding: utf-8 -*-
"""e2e_verify——交付物贯通验证域（T9 从 orchestrator_v2 抽取）。

四个无 self 依赖的方法原样搬迁为模块函数；orchestrator 保留同名薄委托。
_prune_superseded_files/_build_delivery_summary 因依赖 messaging/内部静态方法，
暂留 orchestrator（下次一并处理）。
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading

from workspace import task_project_dir, task_workspace

logger = logging.getLogger("orchestrator_v2")


def _sanitized_process_env(base=None):
    """延迟获取 orchestrator 的进程环境净化函数（避免顶层循环导入）。"""
    import orchestrator_v2 as _o
    return _o._sanitized_process_env(base)


def run_e2e_verification(files, project_dir, game_goal):
    """对最终交付物做贯通验证（确定性，不依赖 LLM）：
    HTML → 文档结构 + 内联 JS 语法（node --check）+ 本地 HTTP 可访问；
    PY → 编译 + 无头冒烟运行（超时视为启动成功）。"""
    import http.server
    import socketserver
    import sys
    import tempfile
    import urllib.request

    results: list[dict] = []
    htmls = [f for f in files if f["kind"] == "html"]
    pys = [f for f in files if f["kind"] == "py"]

    # Node 是否可用（用于 JS 语法校验）
    js_checker = None
    try:
        p = subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        if p.returncode == 0:
            js_checker = "node"
    except Exception:
        js_checker = None

    for f in htmls:
        fp = os.path.join(project_dir, f["name"])
        # 优先浏览器级"可玩"验证（Playwright 缺失时自动安装）
        try:
            pw_ok, pw_detail, shot = playwright_verify(
                project_dir, f["name"], fp, require_game=game_goal,
            )
        except Exception as exc:
            pw_ok, pw_detail, shot = False, f"Playwright 验证异常: {exc}", ""
        if pw_ok:
            results.append({
                "name": f["name"], "type": "html", "ok": True,
                "detail": pw_detail, "screenshot": shot,
            })
            continue
        if "降级" not in pw_detail and "不可用" not in pw_detail:
            results.append({
                "name": f["name"], "type": "html", "ok": False,
                "detail": pw_detail, "screenshot": shot,
            })
            continue
        # Playwright 不可用 → 降级为静态检查
        notes: list[str] = []
        ok = True
        if not os.path.isfile(fp):
            results.append({"name": f["name"], "type": "html", "ok": False, "detail": "文件不存在"})
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as exc:
            results.append({"name": f["name"], "type": "html", "ok": False, "detail": f"读取失败: {exc}"})
            continue
        if "<!doctype html" not in content.lower() and "<html" not in content.lower():
            ok = False
            notes.append("缺少 HTML 文档结构")
        if "<canvas" not in content.lower():
            notes.append("无 <canvas>")
        if "<script" not in content.lower():
            ok = False
            notes.append("无 <script>（页面没有交互逻辑）")
        if content.lower().count("<script") != content.lower().count("</script>"):
            ok = False
            notes.append("<script>/</script> 标签不配平（JS 不会执行）")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", content, re.S)
        if js_checker and scripts:
            tmp_js = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8",
                ) as tf:
                    tf.write("\n".join(scripts))
                    tmp_js = tf.name
                p = subprocess.run(
                    [js_checker, "--check", tmp_js],
                    capture_output=True, timeout=15,
                )
                if p.returncode != 0:
                    ok = False
                    notes.append(
                        "JS 语法错误: " + p.stderr.decode("utf-8", errors="replace")[:120]
                    )
            except Exception as exc:
                notes.append(f"JS 校验异常: {exc}")
            finally:
                try:
                    if tmp_js:
                        os.unlink(tmp_js)
                except Exception:
                    pass
        # 本地 HTTP 可访问性（模拟在浏览器中打开）
        try:
            class _H(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **k):
                    super().__init__(*a, directory=project_dir, **k)

                def log_message(self, *a):
                    pass

            srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/{f['name']}", timeout=10,
                ) as resp:
                    body = resp.read(256)
                    if resp.status != 200 or not body:
                        ok = False
                        notes.append("HTTP 无法访问")
                    else:
                        notes.append("HTTP 200 可访问")
            finally:
                srv.shutdown()
        except Exception as exc:
            ok = False
            notes.append(f"HTTP 失败: {exc}")
        results.append({
            "name": f["name"], "type": "html",
            "ok": ok, "detail": "；".join(notes) or "通过",
        })

    for f in pys:
        fp = os.path.join(project_dir, f["name"])
        if not os.path.isfile(fp):
            results.append({"name": f["name"], "type": "py", "ok": False, "detail": "文件不存在"})
            continue
        try:
            p = subprocess.run(
                [sys.executable, "-m", "py_compile", fp],
                capture_output=True, timeout=20,
                env=_sanitized_process_env(),
            )
            if p.returncode != 0:
                results.append({
                    "name": f["name"], "type": "py", "ok": False,
                    "detail": "编译失败: " + p.stderr.decode("utf-8", errors="replace")[:150],
                })
                continue
        except Exception as exc:
            results.append({"name": f["name"], "type": "py", "ok": False, "detail": f"编译异常: {exc}"})
            continue
        env = _sanitized_process_env()
        env["SDL_VIDEODRIVER"] = "dummy"
        try:
            proc = subprocess.Popen(
                [sys.executable, fp],
                cwd=os.path.dirname(fp),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                out, _ = proc.communicate(timeout=15)
                rc = proc.returncode
                ok = rc == 0
                detail = out.decode("utf-8", errors="replace")[:120] if ok else f"退出码 {rc}"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                ok = True
                detail = "启动成功（15s 超时未崩溃）"
            results.append({
                "name": f["name"], "type": "py",
                "ok": ok, "detail": detail,
            })
        except Exception as exc:
            results.append({"name": f["name"], "type": "py", "ok": False, "detail": f"运行异常: {exc}"})
    return results


def playwright_verify(project_dir, rel_name, fp, require_game):
    """用无头 Chromium 真实打开页面验证：
    require_game=True → 模拟拖拽/键盘交互（"能玩"级，canvas 有绘制）；
    require_game=False → 普通页面正常渲染（有内容、无 JS 错误）。
    返回 (是否通过, 详情, 截图路径)；Playwright 缺失时自动安装。"""
    import http.server
    import socketserver
    import urllib.request

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        from env_setup import ensure_playwright
        ok, msg = ensure_playwright(install_browser=True)
        if not ok:
            return False, f"Playwright 不可用（{msg}），降级为静态检查", ""
        from playwright.sync_api import sync_playwright
    # 画布指纹 JS 常量延迟导入（避免顶层循环依赖）
    from orchestrator_v2 import _FINGERPRINT_JS

    screenshot_dir = os.path.join(project_dir, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    shot = os.path.join(screenshot_dir, rel_name.replace("/", "_").replace(".html", ".png"))
    srv = None
    try:
        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=project_dir, **k)

            def log_message(self, *a):
                pass

        srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/{rel_name}"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 960, "height": 640})
            js_errors: list[str] = []
            game_over_seen = {"v": False}
            page.on("dialog", lambda d: (game_over_seen.__setitem__("v", True), d.dismiss()))
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
            page.goto(url, timeout=15000)
            page.wait_for_timeout(800)
            # 编码检查：页面必须是 UTF-8（否则中文界面/得分会显示为乱码）
            try:
                enc = str(page.evaluate("document.characterSet") or "")
            except Exception:
                enc = ""
            if enc and "utf-8" not in enc.lower():
                browser.close()
                return False, f"页面编码 {enc} 非 UTF-8（中文会显示为乱码）", shot
            if not require_game:
                # 普通页面：不要求 canvas，只需内容可见、无 JS 错误
                visible = page.evaluate(
                    """() => {
                        const t = (document.body && document.body.innerText || '').trim();
                        const hasMedia = !!document.querySelector('img,canvas,video,iframe');
                        return { len: t.length, hasMedia, text: t.slice(0, 80) };
                    }"""
                )
                try:
                    page.screenshot(path=shot)
                except Exception:
                    pass
                if js_errors:
                    browser.close()
                    return False, "JS 错误: " + " | ".join(js_errors[:2]), shot
                if not visible["len"] and not visible["hasMedia"]:
                    browser.close()
                    return False, "页面内容为空（没有可见文字或媒体元素）", shot
                browser.close()
                return True, (
                    f"浏览器加载 OK，页面有内容（{visible['len']} 字符，无 JS 错误）"
                ), shot
            canvas = page.query_selector("canvas")
            if not canvas:
                browser.close()
                return False, "页面无 <canvas>（不是可视化游戏）", shot
            box = canvas.bounding_box()
            if not box or box["width"] < 50 or box["height"] < 50:
                browser.close()
                return False, f"canvas 尺寸异常 {box}", shot
            # 模拟拖拽（弹弓类）与键盘方向键（贪吃蛇类）交互
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            page.mouse.move(cx * 0.75, cy * 0.9)
            page.mouse.down()
            page.mouse.move(cx * 0.35, cy * 0.4, steps=8)
            page.mouse.up()
            for _k in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
                page.keyboard.press(_k)
                page.wait_for_timeout(120)
            page.wait_for_timeout(800)
            state = page.evaluate(
                """() => {
                    const s = document.querySelector(
                        '#score, .score, [class*="score"], [id*="score"], .ui-text'
                    );
                    const cv = document.querySelector('canvas');
                    let nonBlank = false;
                    if (cv && cv.width > 0 && cv.height > 0) {
                        try {
                            const ctx = cv.getContext('2d');
                            const img = ctx.getImageData(0, 0, cv.width, cv.height).data;
                            const r0 = img[0], g0 = img[1], b0 = img[2];
                            let varied = 0;
                            for (let i = 0; i < img.length; i += 4) {
                                if (Math.abs(img[i]-r0) > 12 || Math.abs(img[i+1]-g0) > 12 || Math.abs(img[i+2]-b0) > 12) {
                                    varied++;
                                    if (varied > 300) break;
                                }
                            }
                            nonBlank = varied > 300;
                        } catch (e) { nonBlank = false; }
                    }
                    return {
                        scoreText: s ? (s.textContent || '') : '',
                        canvasDataLen: cv ? cv.toDataURL().length : 0,
                        nonBlank,
                    };
                }"""
            )
            try:
                page.screenshot(path=shot)
            except Exception:
                pass
            # 撞墙重启验证：持续朝一个方向走直到出界/失败，随后尝试
            # 换方向键、Enter/Space、点击画布，确认游戏循环仍在运行。
            # "第一次撞墙就永久卡死"的伪可玩游戏在这里被拦下。
            for _ in range(45):
                page.keyboard.press("ArrowUp")
                page.wait_for_timeout(130)
            restart_ok = False
            if game_over_seen["v"]:
                # 出现游戏结束弹窗才算"撞墙失败"；无该机制的拖拽型游戏
                # （弹弓类，结束后画面静止属正常）自动跳过此项断言
                for _ in range(2):
                    fp_wall = page.evaluate(_FINGERPRINT_JS)
                    page.keyboard.press("ArrowRight")
                    page.wait_for_timeout(700)
                    if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                        restart_ok = True
                        break
                    for extra in ("Enter", "Space"):
                        page.keyboard.press(extra)
                        page.wait_for_timeout(400)
                        if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                            restart_ok = True
                            break
                    if restart_ok:
                        break
                    page.mouse.click(cx, cy)
                    page.wait_for_timeout(400)
                    if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                        restart_ok = True
                        break
                if not restart_ok:
                    browser.close()
                    return False, "撞墙/失败后游戏未重启（游戏循环卡死，不可玩）", shot
            browser.close()
        if js_errors:
            return False, "JS 错误: " + " | ".join(js_errors[:2]), shot
        if not state.get("nonBlank"):
            return False, "canvas 渲染为空白（游戏没有实际绘制内容）", shot
        score = str(state.get("scoreText") or "")[:30]
        detail = "浏览器加载 + 拖拽/方向键模拟 OK，canvas 有渲染内容（无 JS 错误）"
        if score:
            detail += f"；分数/状态='{score}'"
        return True, detail, shot
    except Exception as exc:
        return False, f"浏览器验证异常: {exc}", ""
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


def is_game_goal(goal):
    """判断目标是否"可玩"类（游戏/交互），决定贯通测试走哪种验证。"""
    g = str(goal or "").lower()
    return any(k in g for k in (
        "游戏", "玩", "playable", "game", "canvas", "pygame",
        "贪吃蛇", "打砖块", "弹弓", "小鸟", "棋盘", "2048", "扫雷",
        "五子棋", "射击", "闯关", "体感", "可玩",
    ))


def sweep_workspace_artifacts(task_id):
    """收尾清扫：删除 __pycache__ 与临时校验文件，只保留最新交付包，
    让成果文件夹干净可移动。"""
    ws = task_workspace(task_id)
    try:
        for p in ws.rglob("*"):
            try:
                if p.name == "__pycache__" and p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.is_file() and (
                    p.name.startswith("_check_")
                    or p.name.startswith(".test_")
                    or p.suffix in (".pyc", ".pyo")
                ):
                    p.unlink(missing_ok=True)
            except Exception:
                continue
        # 多轮迭代会产生多个 zip（每轮打包一次）：只保留最新一份
        zips = sorted(
            (p for p in ws.glob("*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for z in zips[:-1]:
            try:
                z.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Workspace sweep failed for %s: %s", task_id, exc)
