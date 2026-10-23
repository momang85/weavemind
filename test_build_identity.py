# -*- coding: utf-8 -*-
"""P1-E：实例身份与构建指纹（离线）。

验证：
1. 身份视图不泄露绝对路径 / 环境变量值 / 密钥——只有文件名、短哈希、计数；
2. 指纹来自文件字节，不用 git HEAD 冒充；磁盘再变 → `restart_required` 且点名文件；
3. 前端构建（dist）与开发态（无 dist）如实区分；
4. 工作区根 / 数据根身份是短指纹，不是路径。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_identity as bi


REPO = Path(__file__).resolve().parent


class TestNoLeak(unittest.TestCase):
    def test_identity_has_no_paths_or_secrets(self):
        s = json.dumps(bi.identity(), ensure_ascii=False)
        self.assertNotIn("C:", s)
        self.assertNotIn("\\", s)
        self.assertNotIn("织光", s)
        self.assertNotIn("api_key", s)
        self.assertNotIn("sk-", s)

    def test_workspace_ids_are_short_hashes(self):
        with mock.patch.dict(os.environ, {"WEAVEMIND_WORKSPACE_ROOT": str(REPO)},
                             clear=False):
            ids = bi._workspace_ids()
        for k in ("workspace_root_id", "data_root_id"):
            v = ids.get(k) or ""
            self.assertTrue(v == "" or (len(v) == 8 and all(
                c in "0123456789abcdef" for c in v)), (k, v))
        self.assertNotIn(str(REPO), json.dumps(ids, ensure_ascii=False))


class TestSourceFingerprint(unittest.TestCase):
    def test_stable_across_calls(self):
        a = bi.source_fingerprint()
        b = bi.source_fingerprint()
        self.assertEqual(a["sha"], b["sha"])
        self.assertEqual(a["files"]["orchestrator_v2.py"]["sha"],
                         b["files"]["orchestrator_v2.py"]["sha"])
        self.assertNotEqual(a["files"]["orchestrator_v2.py"]["sha"], "missing")

    def test_tmp_base_reports_missing(self):
        base = Path(tempfile.mkdtemp(prefix="bi-miss-"))
        try:
            fp = bi.source_fingerprint(base)
            self.assertEqual(fp["files"]["orchestrator_v2.py"]["sha"], "missing")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_changed_byte_changes_sha(self):
        base = Path(tempfile.mkdtemp(prefix="bi-chg-"))
        try:
            src = REPO / "logging_setup.py"
            (base / "logging_setup.py").write_bytes(src.read_bytes())
            before = bi.source_fingerprint(base)
            (base / "logging_setup.py").write_bytes(
                src.read_bytes() + b"\n# probe\n")
            after = bi.source_fingerprint(base)
            self.assertNotEqual(before["sha"], after["sha"])
            changed = bi.changed_files(before, after)
            self.assertEqual(changed, ["logging_setup.py"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_restart_required_names_files(self):
        disk = bi.source_fingerprint()
        loaded = bi.loaded_snapshot()
        # 模拟"启动后 orchestrator_v2.py 在磁盘上被改过"：只动快照里的哈希
        loaded["files"]["orchestrator_v2.py"]["sha"] = "different000"
        ident_loaded = {"files": loaded["files"], "sha": loaded["sha"],
                        "at": loaded["at"]}
        with mock.patch.object(bi, "_LOADED", ident_loaded):
            ident = bi.identity()
        self.assertTrue(ident["source"]["restart_required"])
        self.assertIn("orchestrator_v2.py", ident["source"]["changed"])
        self.assertTrue(ident["restart_required"])
        self.assertIn("受控重启", ident["note"])

    def test_loaded_snapshot_is_a_copy(self):
        snap = bi.loaded_snapshot()
        snap["files"]["web_ui.py"]["sha"] = "tampered00"
        self.assertNotEqual(bi.loaded_snapshot()["files"]["web_ui.py"]["sha"],
                            "tampered00")


class TestFrontendFingerprint(unittest.TestCase):
    def test_built_state(self):
        base = Path(tempfile.mkdtemp(prefix="bi-fe-"))
        try:
            dist = base / "frontend" / "dist" / "assets"
            dist.mkdir(parents=True)
            (base / "frontend" / "dist" / "index.html").write_text(
                "<html></html>", encoding="utf-8")
            (dist / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
            fe = bi.frontend_fingerprint(base)
            self.assertEqual(fe["state"], "built")
            self.assertEqual(fe["assets"], 1)
            self.assertNotEqual(fe["index_sha"], "missing")
            self.assertNotEqual(fe["assets_sha"], "missing")
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_dev_state_without_dist(self):
        base = Path(tempfile.mkdtemp(prefix="bi-fe2-"))
        try:
            fe = bi.frontend_fingerprint(base)
            self.assertEqual(fe["state"], "dev")
            self.assertEqual(fe["index_sha"], "missing")
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestSourceFileList(unittest.TestCase):
    def test_names_only_no_paths(self):
        for name in bi.SOURCE_FILES:
            self.assertFalse("/" in name and len(name) > 3, name)
        self.assertIn("orchestrator_v2.py", bi.SOURCE_FILES)
        self.assertIn("build_identity.py", bi.SOURCE_FILES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
