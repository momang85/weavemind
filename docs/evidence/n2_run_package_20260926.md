# N2：Windows 新人运行包（本地候选包）（09-26）

**指令**：`docs/新人一键启动与首次研究体验_20260925.md` §4。
**构建说明**：[新人运行包构建说明.md](../新人运行包构建说明.md)（面向构建/维护者）。
**范围**：只生成本地候选包与说明；**未**发布 Release、**未**上传任何数据。

## 1. 候选包实况

```
python scripts/build_run_package.py --version 2026.09.26 --out dist
```

| 项 | 值 |
|---|---|
| 包目录 / 分发 zip | `dist/weavemind-2026.09.26-win-x64/` ／ `weavemind-2026.09.26-win-x64.zip` |
| zip 大小 / 摘要 | 291,727,356 字节 ／ `sha256 28ce59f777ae600a5823a37245835998c94e27323ebe91d1a9b586eff3e1a77a` |
| 解压后大小 / 文件数 | 约 1.1 GB ／ 23,984（其中绝大部分是运行依赖，见下） |
| 运行时 | 可重定位 Python **3.11.9**（python.org embeddable，`sha256 009d6bf7e3b2ddca…`） |
| 运行依赖 | **108 个包**（`requirements-runtime-win.lock`，win_amd64/cp311）；`jieba` 无 wheel，按宿主构建后搬入并在报告中标注 |
| 便携 Redis | 随包 `.weavemind/downloads/redis-windows.zip`，摘要与既有参考值一致（`4e8f2f95…`） |
| 前端 | 已构建 `frontend/dist`（新人不需要 Node） |
| 中文字体 | 使用系统字体（未随包分发；`--font` 可显式指定） |
| 清单 | `package_manifest.json`（版本/平台/组件/逐文件 SHA256）+ `VERSION`（→ 应用识别 `package:2026.09.26`） |

包内**没有**：`config.json`、`agents.db`、`.env`、`models/`、`loras/`、`logs/`、
`requirements.lock`（整机快照）、任何密钥或历史数据——构建末尾的秘密扫描与
构建机绝对路径扫描均为 **0 命中**。

## 2. 独立复核（构建器之外再查一遍）

用**包内解释器**执行（`runtime/python.exe`）：

```
ssl / sqlite3 → Python 3.11.9, OpenSSL 3.0.13, SQLite 3.45.1
应用与第三方导入 → import task_intent, question_assessment, net_policy, code_sandbox,
                  dep_check, jieba, pandas, matplotlib, chromadb, redis, aiosqlite,
                  httpx, psutil  → 全部成功（APP_IMPORTS_OK）
可选包导入（报告内）→ jieba: true
禁用内容检查 → config.json / agents.db / models / logs / requirements.lock 均不存在
离线 Redis 入口 → .weavemind/downloads/redis-windows.zip 存在
```

## 3. 构建过程中修掉的四个真问题

| # | 现象 | 原因与修复 |
|---|---|---|
| 1 | 生成 Windows 锁时**覆盖了 Linux 锁** | `make_lock.py` 固定写 `requirements-runtime.lock`；新增 `--out`，Windows 锁写 `requirements-runtime-win.lock`。已 `git checkout` 恢复 Linux 锁 |
| 2 | 运行时下载被拒（主机不在白名单） | `dep_check` 校验器会先剥 `www.` 前缀再比对白名单 → 白名单写裸域名 `python.org` |
| 3 | 包内解释器起不来：`No module named 'encodings'` | 我整份重写了 `._pth` 且把标准库 zip 名算错（`python3119.zip`）。改为：保留原 `._pth` 的非 `import` 行，追加 `python311.zip` / `.` / `Lib\site-packages` / `import site`，并断言标准库 zip 存在 |
| 4 | 包被塞进 15 GB 模型文件；`workers/*.log` 把构建机路径带进包 | 源码清单里误写 `models`（首轮实测 15 GB）；`_is_excluded` 新增 `models/loras/tmp/dist/evals/logs/chroma_memory*` 与 `*.log/*.jsonl/*.db/*.pid` 排除 |

另外把秘密扫描分档：`sk-*` 与私钥块**全包**扫，`api_key=` 之类宽模式只扫**自有内容**
（第三方 `huggingface_hub` 里的 `api_key: Optional[str] = None` 是参数声明，全包扫会假警报）。

## 4. 用例（CI 覆盖）

`test_deploy_manifest.TestRunPackageBuilder`（10 例，随该文件进 CI）：

- 源码清单含必需部件、排除 `config.json`/`agents.db`/`requirements.lock`/`models/`/`loras/`/
  `tmp/`/`dist/`/`evals/`/`logs/`/测试文件/`__pycache__`；
- 策略清单必须挡住重型目录（`models`/`loras`/`dist` 在 `FORBIDDEN_IN_PACKAGE` 里）；
- 秘密扫描与绝对路径扫描能抓到植入的假密钥与构建机路径；
- 清单字段（版本/平台/逐文件 SHA256/`VERSION`）与 `runtime_identity()` 对接（`package:<版本>`）；
- Windows 锁与 Linux 锁分开（文件名、`--platform`、`--python-version`、`--only-binary :all:`）；
- 下载/解压复用 `dep_check` 的既有实现，白名单含 `python.org`；
- 拒绝非 https、非白名单主机与含上跳段的目标路径；索引地址拒绝 http/环回/私网；
- `inside()` 拒绝越界路径。

## 5. 未验 / 边界（如实列出）

- **干净环境未验**：构建报告 `clean_env_verified=false`。真机验收（无 Python/Node/Docker、
  中文与空格路径、移动目录、断网恢复）属 **N4**，本批不宣称通过；开发机上的"包内解释器
  导入"只是必要条件。
- 包体积：解压约 1.1 GB（`scipy/kubernetes/onnxruntime/pandas/sklearn/chromadb` 等占大头）。
  `kubernetes` 是 chromadb 的传递依赖，属可进一步裁剪的候选；本轮**不做**裁剪，先保证
  "解压能用"，把体积与裁剪空间如实记在这里。
- 中文字体未随包分发（Windows 用系统字体）；Docker 路径仍缺字体（既有已记录缺口）。
- 便携 Redis 随包只作离线复用素材；实际启动仍由依赖自检按 5.1 的顺序处理（系统已装优先）。
- 未做：代码签名、安装器、自动更新；包内没有 `config.json`，首次运行仍要填模型与密钥
  （这一步按 N3 走页面引导，不能替他生成）。
