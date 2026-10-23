# -*- coding: utf-8 -*-
"""RUNTIME-1022 P0-A：工作区寻址稳定性回归。

验证（全部离线、全部隔离：独立 TEMP 沙箱、显式 WEAVEMIND_WORKSPACE_ROOT、
屏蔽内建历史根；不触碰真实 agents.db / config / credentials / 在线实例）：

1. 稳定默认根不随 TEMP/TMP 漂移（子进程级验证）；
2. 两个不同 TEMP/TMP 的子进程对同一任务定位一致、字节哈希一致；
3. 只读定位不建目录；越界路径被净化；项目隔离；同名冲突与空壳显式处理；
4. 持久绑定（task_roots.json）写入/回退/失效；
5. ensure 沿用既有真实根、新任务建在稳定根；
6. 报告/底稿/数据集/导出读链在隔离环境走通（含 web_ui 只读端点函数）。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import workspace as ws

REPO = Path(os.path.dirname(os.path.abspath(__file__)))

# 内建历史根按真实系统临时目录发现——测试里屏蔽，改用显式登记根构造场景
_REAL_BUILTIN = ws._builtin_legacy_roots


def _builtin_off():
    return []


def _write(p: Path, data: bytes | str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8") if isinstance(data, str) else p.write_bytes(data)
    return p


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class WorkspaceAddressingTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="wsaddr-"))
        self.stable = self.base / "stable"
        self.stable.mkdir()
        self._saved = {k: os.environ.get(k) for k in
                       ("WEAVEMIND_WORKSPACE_ROOT", "WEAVEMIND_DATA_DIR",
                        "WEAVEMIND_WORKSPACE_LEGACY_ROOTS")}
        self._old_root = ws.WORKSPACE_ROOT
        self._old_builtin = ws._builtin_legacy_roots
        os.environ.pop("WEAVEMIND_DATA_DIR", None)
        os.environ.pop("WEAVEMIND_WORKSPACE_LEGACY_ROOTS", None)
        ws._builtin_legacy_roots = _builtin_off
        self.configure(self.stable)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ws.WORKSPACE_ROOT = self._old_root
        ws._builtin_legacy_roots = self._old_builtin
        with ws._index_lock:
            ws._locate_cache.clear()
            ws._task_project_index.clear()
        shutil.rmtree(self.base, ignore_errors=True)

    # -- 基础工具 --

    def configure(self, root: Path):
        os.environ["WEAVEMIND_WORKSPACE_ROOT"] = str(root)
        ws.configure_workspace_root(root)

    def make_legacy_task(self, root: Path, tid: str, *, full: bool = True,
                         project: str = "default", flat: bool = False) -> Path:
        if flat:
            d = root / tid
        else:
            d = root / "projects" / project / tid
        d.mkdir(parents=True)
        if full:
            _write(d / "project" / "working_paper.json", '{"ok": true}')
            _write(d / "project" / "financials.json", '{"rows": [1, 2]}')
            _write(d / "analysis" / "dataset.json", '{"rows": []}')
            _write(d / "reports" / "report.md", "# 报告\n")
            for i in range(8):
                _write(d / "project" / f"material_{i}.txt", f"m{i}")
        else:
            _write(d / "project" / "official_discovery.json", '{"a": 1}')
            _write(d / "project" / "note.txt", "shell")
        return d

    # -- 1. 默认根不随 TEMP 漂移（子进程级） --

    def test_default_root_ignores_temp_env(self):
        code = (
            "import sys, os, json;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import workspace;"
            "print(json.dumps({'root': str(workspace.WORKSPACE_ROOT),"
            "'temp': __import__('tempfile').gettempdir()}))"
        )
        out = {}
        for tag in ("sessionA", "sessionB"):
            ta = self.base / f"temp-{tag}"
            ta.mkdir()
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("WEAVEMIND_")}
            env.update({"TEMP": str(ta), "TMP": str(ta), "TMPDIR": str(ta),
                        "PYTHONIOENCODING": "utf-8"})
            r = subprocess.run([sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            out[tag] = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["sessionA"]["root"], out["sessionB"]["root"],
                         "两个不同 TEMP 的进程必须解析出同一个默认工作区根")
        self.assertNotIn(out["sessionA"]["root"].lower(),
                         {out[t]["temp"].lower() for t in out},
                         "默认根不得落在 TEMP 下")

    def test_explicit_root_and_data_root_priority(self):
        code = (
            "import sys, os, json;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import workspace;"
            "print(str(workspace.WORKSPACE_ROOT))"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("WEAVEMIND_")}
        env["PYTHONIOENCODING"] = "utf-8"
        explicit = self.base / "explicit-root"
        explicit.mkdir()
        e2 = dict(env); e2["WEAVEMIND_WORKSPACE_ROOT"] = str(explicit)
        r = subprocess.run([sys.executable, "-c", code], env=e2,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), explicit, "显式根优先级最高")

        data = self.base / "data-root"
        data.mkdir()
        e3 = dict(env); e3["WEAVEMIND_DATA_DIR"] = str(data)
        r = subprocess.run([sys.executable, "-c", code], env=e3,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), data / "workspace",
                         "容器数据根语义保持：<数据根>/workspace")

    # -- 2. 跨 TEMP/跨进程一致 --

    def test_two_sessions_same_task_same_bytes(self):
        legacy = self.base / "legacyA"
        tid = "tt-cross-1"
        real = self.make_legacy_task(legacy, tid)
        probe = real / "project" / "financials.json"
        code = (
            "import sys, json, hashlib;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "import workspace as ws;"
            "info = ws.locate_task_workspace('tt-cross-1', bind=False);"
            "p = info['path'] / 'project' / 'financials.json';"
            "h = hashlib.sha256(p.read_bytes()).hexdigest();"
            "print(json.dumps({'path': str(info['path']), 'sha': h,"
            "'found': info['found'], 'source': info['source']}))"
        )
        results = []
        for tag in ("X", "Y"):
            ta = self.base / f"temp-{tag}"
            ta.mkdir()
            env = dict(os.environ)
            env.update({"TEMP": str(ta), "TMP": str(ta), "TMPDIR": str(ta),
                        "WEAVEMIND_WORKSPACE_LEGACY_ROOTS": str(legacy),
                        "PYTHONIOENCODING": "utf-8"})
            env.pop("WEAVEMIND_WORKSPACE_ROOT", None)
            r = subprocess.run([sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            results.append(json.loads(r.stdout.strip().splitlines()[-1]))
        self.assertEqual(results[0]["path"], results[1]["path"])
        self.assertEqual(results[0]["sha"], results[1]["sha"])
        self.assertEqual(results[0]["sha"], sha256_file(probe))
        self.assertTrue(results[0]["found"])

    # -- 3. 只读定位纪律 --

    def test_readonly_missing_task_creates_nothing(self):
        tid = "tt-ghost-404"
        info = ws.locate_task_workspace(tid)
        self.assertFalse(info["found"])
        self.assertIsNone(info["path"])
        self.assertFalse((self.stable / tid).exists())
        self.assertFalse((self.stable / "projects").exists())
        self.assertFalse(ws._bindings_file().exists(), "未命中不得写绑定")
        # 便捷只读入口同样不建目录
        self.assertIsNone(ws.existing_task_workspace(tid))
        self.assertIsNone(ws.existing_task_project_dir(tid))
        self.assertFalse((self.stable / "projects").exists())

    def test_traversal_task_id_never_escapes(self):
        evil = "../../escape_target"
        info = ws.locate_task_workspace(evil)
        p = info.get("path")
        if info["found"] and p is not None:
            self.assertTrue(str(p).startswith(str(self.stable)),
                            "解析结果必须在根内")
        self.assertFalse((self.base / "escape_target").exists())
        # 净化后不含路径分隔符与引导点，无法逃逸
        self.assertNotIn("/", ws._safe_task_id(evil))
        self.assertNotIn("\\", ws._safe_task_id(evil))
        self.assertFalse(ws._safe_task_id(evil).startswith("."))
        self.assertEqual(ws._safe_task_id("../.."), "task")

    def test_project_isolation(self):
        legacy = self.base / "legacyB"
        tid = "tt-iso-1"
        a = self.make_legacy_task(legacy, tid, project="alpha")
        b = self.make_legacy_task(legacy, tid, project="beta")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        ia = ws.locate_task_workspace(tid, project="alpha", bind=False)
        ib = ws.locate_task_workspace(tid, project="beta", bind=False)
        self.assertEqual(ia["path"], a)
        self.assertEqual(ib["path"], b)
        # 无显式 project：同分候选必须显式报冲突，且不写绑定
        iany = ws.locate_task_workspace(tid, bind=False)
        self.assertTrue(iany["conflict"], "两个满分同名目录必须报冲突")
        self.assertEqual(iany["path"], ia["path"], "平局裁定需确定")
        self.assertFalse(ws._bindings_file().exists())

    def test_shell_never_beats_full(self):
        legacy = self.base / "legacyC"
        tid = "tt-shell-1"
        full = self.make_legacy_task(legacy, tid, full=True, flat=True)
        shell = self.make_legacy_task(legacy, tid, full=False)
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        info = ws.locate_task_workspace(tid, bind=False)
        self.assertTrue(info["found"])
        self.assertEqual(info["path"], full, "空壳不得顶替完整产物")
        self.assertFalse(info["empty"])
        cands = {Path(c["path"]) for c in info["candidates"]}
        self.assertEqual(cands, {full, shell}, "两份候选都要显式列出")

    def test_conflict_reported_on_equal_scores(self):
        legacy = self.base / "legacyD"
        tid = "tt-conflict-1"
        d1 = legacy / "projects" / "p1" / tid
        d2 = legacy / "projects" / "p2" / tid
        _write(d1 / "project" / "only_in_d1.txt", "x")
        _write(d2 / "project" / "only_in_d2.txt", "y")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        i1 = ws.locate_task_workspace(tid, bind=False)
        i2 = ws.locate_task_workspace(tid, bind=False)
        self.assertTrue(i1["conflict"])
        self.assertEqual(i1["path"], i2["path"], "冲突选择必须确定性")
        self.assertEqual(len(i1["candidates"]), 2)

    def test_registered_roots_env_and_file(self):
        legacy = self.base / "legacyE"
        tid = "tt-reg-1"
        real = self.make_legacy_task(legacy, tid, flat=True)
        self.configure(self.stable)
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        info = ws.locate_task_workspace(tid, bind=False)
        self.assertTrue(info["found"] and info["path"] == real)
        # 文件登记路径
        os.environ.pop("WEAVEMIND_WORKSPACE_LEGACY_ROOTS", None)
        self.assertTrue(ws.register_legacy_root(legacy))
        with ws._index_lock:
            ws._locate_cache.clear()
        info = ws.locate_task_workspace(tid, bind=False)
        self.assertTrue(info["found"] and info["path"] == real)
        roots_file = self.stable / ws._ROOTS_FILE
        self.assertTrue(roots_file.is_file())
        stored = json.loads(roots_file.read_text(encoding="utf-8"))["roots"]
        self.assertIn(str(Path(stored[0]).resolve()), {str(legacy.resolve())},
                      "登记的根要能定位回 fixture 目录")
        # 非法登记被拒绝
        self.assertFalse(ws.register_legacy_root(str(self.base / "nope")))
        self.assertFalse(ws.register_legacy_root("C:\\" if os.name == "nt" else "/"))

    def test_overridden_root_never_ambient_scans_module_dir(self):
        """RUNTIME-1022 复查回归：根被显式覆盖后，模块目录旁 workspace/ 里的
        同名残留目录不得隐式劫持定位（曾把 test_prune_superseded 的写与删
        引去 <repo>/workspace/t1 残留）。"""
        repo_ws = ws.default_workspace_root()
        tid = "tt-ambient-1"
        # 先在真实稳定根里造一个同名残留（完整产物，评分更高）
        real = self.make_legacy_task(repo_ws, tid, flat=True)
        try:
            # 切到隔离根后定位：残留目录不在登记清单里，不得命中
            fresh = self.base / "freshRoot"
            fresh.mkdir()
            self.configure(fresh)
            with ws._index_lock:
                ws._locate_cache.clear()
                ws._task_project_index.clear()
            info = ws.locate_task_workspace(tid, bind=False)
            self.assertFalse(
                info["found"] and info["path"] == real,
                "显式覆盖根后不得隐式回扫模块目录旁的旧根",
            )
            if info["found"]:
                self.assertIn(str(info["path"]), {str(fresh / tid)}, info)
            # 显式登记后恢复定位（历史回查只走显式登记）
            os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(repo_ws)
            with ws._index_lock:
                ws._locate_cache.clear()
            info2 = ws.locate_task_workspace(tid, bind=False)
            self.assertTrue(info2["found"] and info2["path"] == real,
                            "显式登记旧根后要能定位回残留目录")
        finally:
            shutil.rmtree(real, ignore_errors=True)

    def test_binding_written_on_found_and_falls_back_when_gone(self):
        legacy = self.base / "legacyF"
        tid = "tt-bind-1"
        real = self.make_legacy_task(legacy, tid, project="default")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        info = ws.locate_task_workspace(tid, bind=True)
        self.assertTrue(info["found"])
        bfile = ws._bindings_file()
        self.assertTrue(bfile.is_file(), "跨根发现要写持久绑定（写的是绑定索引，不是产物目录）")
        binding = json.loads(bfile.read_text(encoding="utf-8"))["bindings"][tid]
        self.assertEqual(Path(binding["root"]), real.resolve())
        # 绑定指向的目录消失 → 回退扫描，不得误报成功
        with ws._index_lock:
            ws._locate_cache.clear()
        shutil.rmtree(real)
        info2 = ws.locate_task_workspace(tid, bind=False)
        self.assertFalse(info2["found"], "产物目录消失后绑定必须失效回退")
        # 顺带验证：即便 fixture 被删，也没有在稳定根造出同名目录
        self.assertFalse((self.stable / "projects" / "default" / tid).exists())

    # -- 5. ensure：沿用真实根 / 新任务建在稳定根 --

    def test_ensure_keeps_existing_real_root(self):
        legacy = self.base / "legacyG"
        tid = "tt-ensure-1"
        real = self.make_legacy_task(legacy, tid, project="default")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        wsdir = ws.ensure_task_workspace(tid, "default")
        self.assertEqual(wsdir, real, "已有产物的任务必须沿用真实根")
        for sub in ("project", "reports", "data", "charts"):
            self.assertTrue((wsdir / sub).is_dir())
        self.assertFalse((self.stable / "projects" / "default" / tid).exists())

    def test_ensure_new_task_lands_in_stable_root_and_binds(self):
        tid = "tt-ensure-new"
        wsdir = ws.ensure_task_workspace(tid, "default")
        self.assertEqual(wsdir, self.stable / "projects" / "default" / tid)
        binding = json.loads(
            ws._bindings_file().read_text(encoding="utf-8")
        )["bindings"][tid]
        self.assertEqual(Path(binding["root"]), wsdir.resolve())
        self.assertEqual(binding["project"], "default")

    # -- 6. 布局兼容与 task_workspace 语义 --

    def test_flat_and_projects_layouts(self):
        legacy = self.base / "legacyH"
        t1, t2 = "tt-flat-1", "tt-proj-1"
        flat = self.make_legacy_task(legacy, t1, flat=True)
        proj = self.make_legacy_task(legacy, t2, project="default")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        i1 = ws.locate_task_workspace(t1, bind=False)
        i2 = ws.locate_task_workspace(t2, bind=False)
        self.assertEqual((i1["path"], i1["layout"]), (flat, "flat"))
        self.assertEqual((i2["path"], i2["layout"]), (proj, "projects"))

    def test_task_workspace_keeps_backward_semantics(self):
        legacy = self.base / "legacyI"
        tid = "tt-sem-1"
        real = self.make_legacy_task(legacy, tid, project="default")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        self.assertEqual(ws.task_workspace(tid), real)
        self.assertEqual(ws.task_workspace(tid, "default"), real)
        # 不存在的任务：返回既定默认路径（不创建），写方照旧 mkdir
        ghost = ws.task_workspace("tt-sem-ghost", "default")
        self.assertEqual(ghost, self.stable / "projects" / "default" / "tt-sem-ghost")
        self.assertFalse(ghost.exists())

    # -- 7. 读链隔离：报告/底稿/数据集/导出 --

    def test_read_chain_over_legacy_task(self):
        legacy = self.base / "legacyJ"
        tid = "tt-chain-1"
        real = self.make_legacy_task(legacy, tid, project="default")
        os.environ["WEAVEMIND_WORKSPACE_LEGACY_ROOTS"] = str(legacy)
        with ws._index_lock:
            ws._locate_cache.clear()
        # 底稿读链（web_ui working_paper 端点同款只读定位）
        proj = ws.existing_task_project_dir(tid)
        self.assertIsNotNone(proj)
        wp = json.loads((proj / "working_paper.json").read_text(encoding="utf-8"))
        self.assertTrue(wp["ok"])
        # 数据集读链
        ds = json.loads((ws.task_workspace(tid) / "analysis" / "dataset.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(ds, {"rows": []})
        # 导出清单登记读链（delivery_pipeline 同款：workspace.task_workspace + hash）
        fp = ws.task_workspace(tid) / "project" / "financials.json"
        self.assertEqual(hashlib.sha256(fp.read_bytes()).hexdigest(),
                         sha256_file(real / "project" / "financials.json"))
        # 研究状态读取（delivery_pipeline.read_research_state，ws_dir=None → 自寻址）
        import delivery_pipeline as dp
        state = dp.read_research_state(tid)
        self.assertEqual(state, {}, "无 research_state.json 时如实返回空，不编造")

    def test_read_chain_missing_task_returns_404_without_writes(self):
        tid = "tt-chain-ghost"
        # web_ui working_paper 端点的定位层（existing_task_project_dir）
        # 不得建目录：任务不存在 → None → 端点 404
        self.assertIsNone(ws.existing_task_project_dir(tid))
        self.assertFalse((self.stable / "projects" / "default" / tid).exists())
        self.assertFalse((self.stable / ws._BINDINGS_FILE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
