# -*- coding: utf-8 -*-
"""部署清单守卫：镜像装得齐、依赖锁对得上、CI 跑得到。

三类"没人看就退化"的东西，此前都会一直绿到生产才炸：

1. **Dockerfile 的 COPY 清单 vs 运行入口的本地导入图**——`orchestrator_v2` 顶层
   `from charts_pipeline/structured_pipeline import ...`，而镜像只拷了根级 `.py` 与
   `workers/`，于是编排器子进程（launcher 起）导入即崩；当时 CI 既不构建镜像也不导入
   检查，所以没人发现。这里把导入图算出来，逐个要求镜像里有对应条目。
2. **`requirements-runtime.lock` vs `requirements.txt`**——加了下限依赖却忘更新锁，
   只有容器/CI 才能发现；并且锁里若混进训练或平台专属包（torch/pywin32 等）会装不上。
3. **CI 执行的测试文件 vs 仓库里的 `test_*.py`**——曾有 12 个文件（金融链、任务状态、
   检查点、记忆降级、鉴权审计…）长期不在门禁内。
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCKERFILE = ROOT / "Dockerfile"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
RUNTIME_LOCK = ROOT / "requirements-runtime.lock"
REQUIREMENTS = ROOT / "requirements.txt"

# 镜像启动后会导入/执行的根级模块（launcher 主进程与它拉起的子进程）
ENTRY_FILES = (
    "launcher.py",
    "web_ui.py",
    "orchestrator_v2.py",
    "worker_base.py",
    "async_worker_base.py",
    "metrics_collector.py",
)

# 导入图看不出来的运行资源（非 .py 或按路径启动的目录）
REQUIRED_RESOURCES = (
    "workers",                    # launcher 按脚本路径拉起 worker 进程
    "templates.json",             # web_ui/orchestrator 读任务模板
    "config.example.json",        # 容器内首次配置的引导模板
    "frontend/dist",              # 由构建阶段产出
    "skills",                     # 技能注册表（缺目录则记录教训失败）
    "evals",                      # 评测用例与自动生长
    "requirements-runtime.lock",  # 镜像安装依赖的依据
)

# 整机训练快照（requirements.lock）里才有的包：出现在运行锁里说明生成时环境被污染
LOCK_FORBIDDEN = (
    "torch", "torchvision", "ultralytics", "faster-whisper",
    "sentence-transformers", "pyinstaller", "pywin32", "pywin32-ctypes",
    "pefile", "customtkinter", "pygame-ce", "nvidia-cublas-cu12",
    "nvidia-cudnn-cu12", "nvidia-cuda-runtime-cu12",
)


# ── Dockerfile 解析 ────────────────────────────────────────────────

def _norm_dest(dest: str) -> str:
    d = dest.replace("\\", "/")
    if d.startswith("./"):
        d = d[2:]
    if d in (".", ""):
        return ""
    return d.rstrip("/")


def _copy_records() -> list[dict]:
    """解析 Dockerfile 的 COPY 行（跳过 --chown 之类的标志，记录 --from 阶段拷贝）。"""
    recs: list[dict] = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        tokens = line.split()[1:]
        from_stage = False
        while tokens and tokens[0].startswith("--"):
            if tokens[0].startswith("--from="):
                from_stage = True
            tokens.pop(0)
        if len(tokens) < 2:
            continue
        recs.append({
            "dest": _norm_dest(tokens[-1]),
            "sources": tokens[:-1],
            "from_stage": from_stage,
        })
    return recs


def _tracked_paths() -> set[str] | None:
    """git 索引里的全部路径。只有这些条目会出现在干净检出里（CI 就是这么取的），
    本地"目录还在"是因为里面躺着被 gitignore 的产物。"""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _implanted_entries(recs: list[dict]) -> set[str]:
    """镜像里最终会出现的顶层条目（相对 /app）。"""
    entries: set[str] = set()
    for rec in recs:
        dest = rec["dest"]
        if rec["from_stage"]:
            # 构建阶段产物（如 COPY --from=frontend-build /build/dist frontend/dist）
            if dest:
                entries.add(dest)
            continue
        for src in rec["sources"]:
            s = src.replace("\\", "/")
            if "*" in s:
                entries.add(f"{dest}/{s}" if dest else s)
                continue
            if s.endswith("/"):
                name = s.rstrip("/").split("/")[-1]
                if dest and dest.split("/")[-1] == name:
                    entries.add(dest)
                else:
                    entries.add(f"{dest}/{name}" if dest else name)
                continue
            name = Path(s).name
            entries.add(f"{dest}/{name}" if dest else name)
    return entries


def _covered(rel: str, entries: set[str]) -> bool:
    if rel in entries:
        return True
    # 根级 .py 由 `COPY *.py ./` 覆盖
    if "/" not in rel and rel.endswith(".py"):
        return any(fnmatch.fnmatch(rel, e) for e in entries if "*" in e)
    return False


# ── 本地导入图 ────────────────────────────────────────────────────

def _is_local_top(name: str) -> bool:
    return (ROOT / f"{name}.py").is_file() or (ROOT / name / "__init__.py").is_file()


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if _is_local_top(top):
                    found.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 包内相对导入：整包已在镜像里
                continue
            top = (node.module or "").split(".")[0]
            if top and _is_local_top(top):
                found.add(top)
    return found


def _required_local_entries() -> set[str]:
    """运行入口的本地导入闭包 → 镜像里必须存在的顶层条目。"""
    queue: list[Path] = [ROOT / f for f in ENTRY_FILES if (ROOT / f).is_file()]
    queue += sorted((ROOT / "workers").glob("*.py"))
    seen: set[Path] = set()
    tops: set[str] = set()
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for top in _imports_of(path):
            tops.add(top)
            single = ROOT / f"{top}.py"
            if single.is_file():
                queue.append(single)
            else:
                queue += sorted((ROOT / top).rglob("*.py"))
    return {f"{t}.py" if (ROOT / f"{t}.py").is_file() else t for t in tops}


class TestDockerfileManifest(unittest.TestCase):
    def setUp(self):
        self.recs = _copy_records()
        self.entries = _implanted_entries(self.recs)
        self.assertTrue(self.recs, "Dockerfile 里没有解析到任何 COPY 行")

    def test_runtime_imports_are_in_image(self):
        missing = sorted(
            rel for rel in _required_local_entries() if not _covered(rel, self.entries))
        self.assertEqual(
            missing, [],
            f"运行入口会导入这些本地条目，但 Dockerfile 没拷进镜像：{missing}")

    def test_required_resources_are_in_image(self):
        missing = sorted(r for r in REQUIRED_RESOURCES if not _covered(r, self.entries))
        self.assertEqual(
            missing, [],
            f"这些运行资源不在镜像里（容器重建/启动会失败或功能缺失）：{missing}")

    def test_config_json_is_not_baked(self):
        """真实配置（含密钥）绝不能烘进镜像；容器里靠引导或挂载。"""
        self.assertNotIn("config.json", self.entries)

    def test_copy_sources_exist_in_clean_checkout(self):
        """COPY 的源必须在干净检出里存在。反例：`COPY prompts/ ./prompts/`——该目录
        本地只装 gitignore 的 overrides.json，CI 检出后目录根本不存在，构建以
        "not found" 失败；本地因为目录还在，怎么跑都看不出问题。"""
        tracked = _tracked_paths()
        if tracked is None:
            self.skipTest("非 git 工作树，无法核对检出等价性")
        bad: list[str] = []
        for rec in self.recs:
            if rec["from_stage"]:
                continue
            for src in rec["sources"]:
                s = src.replace("\\", "/").rstrip("/")
                if "*" in s or "?" in s:
                    if not any(fnmatch.fnmatch(t, s) for t in tracked):
                        bad.append(f"{src}（通配式在检出里无匹配）")
                elif not any(t == s or t.startswith(s + "/") for t in tracked):
                    bad.append(src)
        self.assertEqual(bad, [], f"这些 COPY 源不在干净检出里，构建必失败：{bad}")


# ── 运行依赖锁 ────────────────────────────────────────────────────

def _ver_tuple(v: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.split(r"[.\-+]", v) if x.isdigit()]
    return tuple((nums + [0, 0, 0, 0])[:4])


def _split_req(line: str) -> tuple[str, str]:
    m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
    name = (m.group(1) if m else line).lower().replace("_", "-")
    return name, (m.group(2).strip() if m else "")


class TestRuntimeLock(unittest.TestCase):
    def _pins(self) -> dict[str, str]:
        self.assertTrue(RUNTIME_LOCK.is_file(), "缺少 requirements-runtime.lock")
        pins: dict[str, str] = {}
        for raw in RUNTIME_LOCK.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "==" not in line:
                continue
            name, _, ver = line.partition("==")
            pins[name.strip().lower().replace("_", "-")] = ver.strip()
        return pins

    def test_lock_satisfies_requirements(self):
        pins = self._pins()
        missing, too_low = [], []
        for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, spec = _split_req(line)
            if name not in pins:
                missing.append(line)
                continue
            if spec.startswith(">="):
                if _ver_tuple(pins[name]) < _ver_tuple(spec[2:]):
                    too_low.append(f"{name} {pins[name]} < {spec[2:]}")
            elif spec.startswith("=="):
                if pins[name] != spec[2:]:
                    too_low.append(f"{name} {pins[name]} != {spec[2:]}")
        self.assertEqual(missing, [], "运行锁缺少 requirements.txt 的依赖（改依赖后需重跑 make_lock --runtime）")
        self.assertEqual(too_low, [], "运行锁版本不满足 requirements.txt 的下限")

    def test_lock_excludes_training_and_platform_packages(self):
        pins = self._pins()
        leaked = sorted(p for p in LOCK_FORBIDDEN if p in pins)
        self.assertEqual(
            leaked, [],
            f"运行锁里混进了训练/平台专属包（生成时环境被污染）：{leaked}")

    def test_ci_and_dockerfile_install_from_lock(self):
        self.assertIn("requirements-runtime.lock", CI_YML.read_text(encoding="utf-8"),
                      "CI 未使用运行依赖锁")
        self.assertIn("requirements-runtime.lock", DOCKERFILE.read_text(encoding="utf-8"),
                      "Dockerfile 未使用运行依赖锁")


class TestPersistentDataRoot(unittest.TestCase):
    """容器重建后必须还在的东西（任务库/工作区/分享/审计/提示词覆盖）要挂到数据根，
    而数据根必须真的被 compose 挂成卷——只声明 ENV 不挂卷等于没做。"""

    def test_image_declares_data_root(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("WEAVEMIND_DATA_DIR=/data", text,
                      "镜像必须声明可变数据根，否则产物落到容器临时目录")
        self.assertIn("WEAVEMIND_DB=/data/agents.db", text)

    def test_compose_mounts_the_data_root(self):
        compose = ROOT / "docker-compose.yml"
        self.assertTrue(compose.is_file(), "缺少 docker-compose.yml")
        text = compose.read_text(encoding="utf-8")
        m = re.search(r"^\s{2}app:\n(.*?)(?=^\s{2}\w+:|\Z)", text, re.S | re.M)
        self.assertIsNotNone(m, "compose 里找不到 app 服务")
        self.assertRegex(m.group(1), r"-\s*[\w.\-]+:/data\b",
                         "app 服务必须把数据根挂成卷（否则容器重建即丢历史与审计）")


# ── CI 覆盖面 ─────────────────────────────────────────────────────

class TestComposeKeepsContainerAlive(unittest.TestCase):
    """compose 的"容器起来之后要一直在"不变量（实测事故：容器起来即退出）。

    `python launcher.py start` 拉起子进程后**自己会返回**：宿主上没问题（终端还在），
    容器里 PID 1 一退出，子进程被一起收走、`/api/health` 永远不就绪，CI 的
    "Compose up and readiness check" 就是这样一直红的。守护模式 + Redis 就绪探针
    是修法，这里把它钉住。
    """

    COMPOSE = ROOT / "docker-compose.yml"

    def _doc(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML 不可用")
        return yaml.safe_load(self.COMPOSE.read_text(encoding="utf-8"))

    def test_app_runs_in_supervise_mode(self):
        doc = self._doc()
        env = doc["services"]["app"].get("environment") or []
        env = [str(e) for e in env]
        self.assertIn(
            "WEAVEMIND_SUPERVISE=1", env,
            "容器必须常驻守护：否则 launcher start 返回后 PID 1 退出，服务全被收走")

    def test_redis_has_healthcheck_and_app_waits_for_it(self):
        doc = self._doc()
        self.assertTrue(doc["services"]["redis"].get("healthcheck"),
                        "redis 需要就绪探针（depends_on 默认只保证启动顺序）")
        dep = doc["services"]["app"].get("depends_on")
        self.assertIsInstance(dep, dict,
                              "app 必须用 condition: service_healthy 等 redis 真就绪")
        self.assertEqual(dep.get("redis", {}).get("condition"), "service_healthy")

    def test_app_binds_all_interfaces_inside_container(self):
        """容器内的 WebUI 必须绑 0.0.0.0，否则端口映射空转。

        `web_ui.py` 默认 `BIND_HOST=127.0.0.1`（未加鉴权时不暴露到局域网），
        在容器里就是"只监听容器自己"——`ports: 8080:8080` 把流量转到容器 IP 时
        连不上。实测：容器起来、16/16 服务存活、`docker compose ps` 显示映射正常，
        但宿主机 readiness 探活 200 秒全失败。
        """
        doc = self._doc()
        env = [str(e) for e in (doc["services"]["app"].get("environment") or [])]
        self.assertIn(
            "BIND_HOST=0.0.0.0", env,
            "容器内必须显式绑 0.0.0.0，否则发布出去的端口拿不到响应")


class TestCiSuiteCoverage(unittest.TestCase):
    def test_every_test_file_runs_in_ci(self):
        ci = CI_YML.read_text(encoding="utf-8")
        executed = set(re.findall(r"python\s+(test_[A-Za-z0-9_]+\.py)", ci))
        on_disk = {p.name for p in ROOT.glob("test_*.py")}
        self.assertEqual(
            sorted(on_disk - executed), [],
            "以下测试文件不在 CI 门禁内，会随代码漂移静默失效")

    def test_workflow_yaml_parses(self):
        """CI 工作流必须能被 YAML 解析。

        实测过的事故：新加的步骤名里写了冒号（`- name: xxx (R1: bound PASS...)`），
        冒号后带空格让整份 YAML 解析失败——GitHub 照样创建一个 run，但**零作业**、
        结论 failure，本地跑测试全绿、看不出任何异常。这类"门禁自身坏了"的问题
        必须由测试拦住，而不是等下一次 CI 红灯再猜。
        """
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML 不可用")
        try:
            doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        except Exception as exc:            # noqa: BLE001 - 解析失败就是要报出来
            self.fail(f".github/workflows/ci.yml 无法解析：{exc}")
        self.assertIsInstance(doc, dict)
        jobs = doc.get("jobs") or {}
        self.assertTrue(jobs, "工作流必须至少有一个作业")
        for jname, job in jobs.items():
            self.assertIn("runs-on", job, f"作业 {jname} 缺少 runs-on")
            for idx, step in enumerate(job.get("steps") or []):
                has_run = "run" in step
                has_uses = "uses" in step
                self.assertEqual(
                    has_run + has_uses, 1,
                    f"作业 {jname} 第 {idx} 步必须恰有 run 或 uses 之一：{sorted(step)}")

    def test_workflow_step_names_avoid_unquoted_colons(self):
        """步骤名/作业名里出现 `: ` 必须加引号（否则就是上一例的解析事故）。"""
        raw = CI_YML.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in raw.splitlines()
            if re.match(r"^\s*-?\s*name:\s+[^\"']*: ", line)
        ]
        self.assertEqual(offenders, [],
                         "这些 name 含未加引号的冒号，会让 YAML 解析失败")


class TestOrchestratorRunTestsAreOffline(unittest.TestCase):
    """驱动 `orchestrator.run()` 的测试文件必须隔离 LLM 预检。

    `run()` 开头会做两次**真实网络**预检（端点可达性 + 余额）。不隔离时：
    - 本机 `config.json` 有真实 key → 真端点；余额耗尽（402）就在预检被拒，
      用例断言的"反思/交付/恢复/审批"逻辑根本没发生（表现为各种毫不相关的断言失败）；
    - CI 无 key → "端点不可用"，同样早退。
    这一种机依赖在本会话里把四个套件打成过假红，所以按静态规则钉住。
    """

    def test_run_drivers_isolate_prechecks(self):
        offenders = []
        for path in sorted(ROOT.glob("test_*.py")):
            src = path.read_text(encoding="utf-8")
            if not re.search(r"\b(?:o|orch|self\.o|_orch)\.run\(", src):
                continue
            isolated = (
                "stub_llm_prechecks" in src
                or "get_balance_status" in src
                or "endpoints_available" in src
            )
            if not isolated:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "这些文件驱动 run() 却没隔离 LLM 预检，会随本机 key/余额状态假红："
            f"{offenders}（用 tests_support.stub_llm_prechecks 或自行 patch）")


if __name__ == "__main__":
    unittest.main()
