# C：报告脱敏与真正独立的试用包 — 执行记录（DATA-RC2-1024，阶段 C）

- 执行人：pkg-hygiene（阶段 C 执行代理）；日期：2026-10-24（Asia/Shanghai）
- 依据：`docs/员工任务_数据可信与独立试用包RC2_20261024.md` §5（C 验收标准）、
  `docs/evidence/20261024-data-rc2/architect_review.json`（`package_review` + `additional_source_review`）、
  `AGENTS.md`
- 本阶段未改动 `web_ui.py`、`screen_store.py`、`screen_prepare.py`、`adapters/`、`test_*.py`
  （A/B 阶段由 Lead 并行处理，C 阶段对它们只读）。
- 密钥纪律：全文不出现任何真实口令/密钥/cookie 值；只报告「文件 + 字段类别 + 数量」，
  字面量一律以 `[已脱敏]` 表示。

## 结论一览

| 项 | 内容 | 状态 |
|---|---|---|
| C1 | 报告/脚本口令脱敏（23 文件：38 处改环境变量 + 11 处 `[已脱敏]`） | 完成，残留密码类扫描 = 0 |
| C2 | 旧包（20261024）标记「不可分发」，保留不删 | 完成 |
| C3 | 默认前端入口构建 + 静态检查 + 8089 隔离实例验证 | 完成（HTTP 全 200，页面与 `frontend/dist/index.html` 字节一致） |
| C4 | `tools/build_rc2_package.py` + RC2 包 + 工作区之外解包运行（8090） | 完成（构建/清单自检/解包核对/全流程探针全 PASS） |
| C5 | Python 3.11 定向运行 | **未验**（本机无 3.11；3.12 附加观察仅供参考，不构成 3.11 结论） |
| C6 | 本报告 | 完成 |

---

## C1 报告/脚本口令脱敏

方法：逐文件内容改写 + 双重扫描器复核；计数以**内容推导**为准（不以生成时的 summary JSON 为准）。

### 变更明细（文件 + 字段类别 + 数量）

| 文件 | 字段类别 | 数量 |
|---|---|---|
| `tmp/*.py` 共 19 个脚本 | 账号口令字面量 → `_rc2_env("RC_*_PASSWORD")` 环境变量读取（每文件自带 helper） | 38 处（ADMIN 16 / VIEWER 11 / RESEARCHER 9 / MOMANG 2） |
| `tmp/closeout_instance.py` | 文档性口令示例 → `[已脱敏]` | 4 |
| `tmp/r1c_isolated_instance.py` | 文档性口令示例 → `[已脱敏]` | 1 |
| `tmp/r1next_isolated_instance.py` | 文档性口令示例 → `[已脱敏]` | 2 |
| `docs/evidence/20261024-beta-closeout/C3-package.md` | 文档示例口令 → `[已脱敏]` | 1 |
| `docs/evidence/20261024-beta-closeout/acceptance-matrix.md` | 同上 | 1 |
| `docs/evidence/20261024-beta-closeout/logs/momang-session.md` | 同上 | 1 |
| `docs/evidence/20261023-screen-beta/B2-user-flow.md` | 同上 | 1 |

合计：**23 个文件；38 处环境变量化；11 处 `[已脱敏]`**。

### 契约验证与复核

- 环境变量契约：未设置对应变量时脚本立即退出并提示
  `缺少环境变量 RC_RESEARCHER_PASSWORD：本脚本口令已脱敏，请设置后重试。`；设置后 `--help` 正常（exit 0）。
- 编译复核：19 个改写脚本 `py_compile` 全部通过（0 失败）。
- 残留扫描（最终状态复扫，扫描器 `tmp/rc2-package/_c1_scan.py`，只报类别不报值）：
  - 密码类（closeout 账号口令 / rc-admin 口令 / 角色口令 / sk 风格 key / cookie 字面量）：**0**；
  - `assignment_literal`：14 处，逐条人工核验**全部是 `api_key` 字段的占位值「unused」桩**
    （隔离实例脚本的 LLM 配置桩及其生成的 `config.json`），非真实密钥；
  - `sk-` 命中已甄别为中文散文（`docs/evidence/20261019-live-e2e/README.md:285`、
    `docs/evidence/20261022-logic-audit/README.md:109`），非密钥；
  - cookie 命中 2 处为 `Set-Cookie: session=…` 说明性占位，非真实会话值。
- 原始账号记录与生产配置未读取、未更改；未复用任何真实口令做隔离账号（隔离账号口令一律运行时随机生成、只存哈希、不落盘明文）。

---

## C2 旧包标记「不可分发」

- 标记文件：`tmp/rc-package-1024/DO-NOT-DISTRIBUTE.md`（2026-10-24 写入）。
- 两个原因：① 包内含真实缓存 `data/screen_cache/official_disclosure_cache.json`，再分发许可未确认；
  ② 缺默认前端入口 `frontend/dist/index.html`（只有 `dist-screen-check/`，标准入口打不开页面）。
- 旧包保留用于核对，未删除、未覆盖；本轮复核 sha256 仍为
  `8d5a2fc82202feae2a6cdc3b824cef2a816380909fc4062aa98780b3c1f68819`（3,692,887 字节，334 条目），与 architect 记录一致。

---

## C3 默认前端入口

- 构建：`frontend/` 下 `npm run build`（= `tsc -b && vite build`），exit 0（16.78s，包含 A/B 阶段 Lead 正在修改的 `StockScreen` 源码状态）。
- 产物：`frontend/dist/` 共 34 文件 / 977,653 字节；`index.html` 804 字节，
  引用 `/assets/index-Dvfs4tap.js`（214,981 字节）与 `/assets/index-SyayeYPe.css`（47,734 字节），
  favicon 为内联 data URI。注意：Vite 资产哈希随每次重建变化，报告以 2026-10-24 12:06 构建为准。
- 静态检查：`tmp/rc2-package/_c3_static_check.py`，exit 0（refs=3：ok=2 / bad=0 / skipped=1 内联）。
- 隔离实例（仅本单拥有，回环绑定）：`tmp/pkg_instance_8089.py`，端口 8089，
  数据目录 `tmp/pkg-8089`，**不覆盖 `web_ui.DIST_DIR`**（使用默认 `frontend/dist`，即标准入口路径）：

| 请求 | 结果 |
|---|---|
| `GET /` | 200，804 字节，与磁盘 `frontend/dist/index.html` **逐字节一致** |
| `GET /assets/index-Dvfs4tap.js` | 200，214,981 字节 |
| `GET /assets/index-SyayeYPe.css` | 200，47,734 字节 |

- 校验脚本 `tmp/rc2-package/_c3_verify_8089.py` exit 0；验证后实例已停止（8089 连接拒绝，确认关闭）。

---

## C4 打包器与 RC2 独立试用包

### 打包器

- 工具：`tools/build_rc2_package.py`（正式工具目录）。CLI：`--out <目录>` 构建、`--verify <zip>` 完整性自检（退出码 0/1）。
- 显式白名单：根级 `*.py`（排除 `_trial_*`/`_dbg_*`/`_probe*`/`probe_*`/`_l3_probe*`）、
  `adapters/`、`charts_pipeline/`、`evals/`、`examples/`、`financial_analysis/`、`prompts/`、
  `quant_research/`、`risk_research/`、`skills/`、`structured_pipeline/`、`validators/`、`workers/`、
  `frontend/dist/`、`requirements.txt`、`start.bat`、`launcher.py`、`README.md`、生成的合成样例。
  **对任务清单的白名单补充说明**：`evals/` 与 `validators/` 不在 Lead 给的目录清单里，但
  `orchestrator_v2.py`（`web_ui` 顶层依赖）对其顶层 import，缺了包内 `import web_ui` 直接失败——
  故纳入并在此说明。
- 默认不含：`data/screen_cache/*`、`config.json`、`credentials.json`、`.env`、`agents.db`、
  `*.log`、`tmp/`、`workspace/`（数据目录；`workspace.py` 代码保留）、`docs/`、`node_modules/`、
  `frontend/src/`、`__pycache__`、`frontend/dist-screen-check/`。
- 生成物：`BUILD.json`（build_id = UTC 时间戳 + 文件清单短哈希、Python 版本、命令、文件数、总字节、
  排除清单、样例声明）与 `MANIFEST.sha256.json`（逐文件 sha256+bytes，覆盖包内**除清单自身外**
  全部文件，含 `BUILD.json`）；写包后自检逐文件复核哈希与覆盖面，任何不一致 exit 1。

### 构建结果（exit 0）

| 项 | 值 |
|---|---|
| zip | `tmp/rc2-package/weavemind-rc2-20261024T043139Z-ba0f6d57.zip` |
| zip sha256 | `0673adb2cd86b783d5030abc020da5d706932d95bafea82388b6d3c2e2a38240` |
| zip 大小 | 22,665,282 字节 |
| 文件数 | 414（清单覆盖 413 + 清单自身） |
| 未压缩总量 | 39,734,411 字节 |
| 合成样例 | `data/samples/synthetic_5_companies.json`：5 家虚构公司（690001–690005.SH）× 2 个年度期间 = 10 行；`metadata.synthetic=true`；`source_url=about:synthetic-demo`；`data/` 下**仅此 1 个文件** |
| `--verify` 复核 | exit 0（全覆盖 + 逐文件 sha256 一致 + 无禁入内容） |

### 工作区之外解包运行（端口 8090）

- 解包目录：`C:\Users\ding0\Desktop\weavemind-rc2-probe\weavemind`（工作区之外）。
- 解包核对（`tmp/rc2-package/_c4_unpack_check.py`）：磁盘 414 文件 vs 清单 413+1，哈希全一致，exit 0。
- 启动方式：`tmp/pkg_instance_8090_from_package.py`——`sys.path` 仅插入解包目录，
  断言 `web_ui.__file__` 位于解包目录内（不 import、不回落工作区）；
  `agents.db`/日志/筛选目录/分享目录/记忆目录全部指向 `tmp/pkg-8090`（工作区内隔离数据目录），
  解包目录保持整洁；`WEAVEMIND_SCREEN_CACHE` 指向**包内合成样例**（标准入口即「合成演示」缓存）；
  `config.json` 置于 `tmp/pkg-8090`（空配置，首启无任何用户）；回环绑定 127.0.0.1:8090。
- 全流程探针（`tmp/rc2-package/_c4b_probe_8090.py`，exit 0；口令随机生成，只在内存，不打印不落盘）：

| 步骤 | 结果 |
|---|---|
| `GET /` | 200，804 字节，与**包内** `frontend/dist/index.html` 逐字节一致 |
| `GET /api/health` | 200，`status=ok` |
| `POST /api/setup-admin`（首启建号） | 200，role=admin |
| `POST /api/login` | 200，user=rc2-probe-admin，role=admin |
| `GET /api/screen/pool`（合成样例作缓存） | 200，池内 **5 家合成公司**（690001–690005.SH），`source.provenance.kind=synthetic_demo`、`kind_label=「合成演示」`、`synthetic=true` |
| `POST /api/screen/run`（规则 `debt_ratio<=60`，uniform 期间政策，as_of=2025-12-31） | 200，`counts={pass:4, fail:1, insufficient:0, total:5}`，`report_period=2024年`，来源 `kind_label=「合成演示」`，`result_hash=66066b9f…`（run_id `329c3151…` 由内容身份决定，同输入复现同 ID） |
| `POST /api/screen/datasets`（用户自导 CSV，2 家虚构主体） | 200，`dataset_id=03bcfae6…` |
| `GET /api/screen/pool?dataset=…` | 200，池内 2 家，`kind=user_provided`、`kind_label=「用户提供」` |
| `POST /api/screen/run`（dataset） | 200，`counts={pass:1, fail:1, insufficient:0, total:2}`，来源标「用户提供」 |

- 探针后停止实例（8090 连接拒绝确认关闭）；解包目录清理运行时残留
  （`chroma_memory/chroma.sqlite3` 为实例运行时生成、**包内无此文件**；`__pycache__` 清理）后复检 exit 0。

---

## C5 Python 3.11 定向运行：**未验**

本机不存在 Python 3.11 运行时（逐项确认）：

| 检查 | 命令 | 结果 |
|---|---|---|
| py 启动器 | `py -3.11 --version` | `No suitable Python runtime found`，exit 103 |
| PATH | `python3.11 --version` | command not found，exit 127 |
| 常见安装路径 | `ls C:\Python311\python.exe` | 不存在 |
| 本机版本清单 | `py -0` | 仅 3.14（默认）、3.12、Anaconda 3.7 条目 |

- 附加观察（**不构成 3.11 兼容结论**）：Python 3.12.9 下对包内全部 `.py` 执行
  `py -3.12 -m compileall -q .` exit 0；`stock_screen` / `screen_store` 在 3.12.9 下 import 成功。
- 明确声明：本报告其它部分全部在 Python 3.14.3 下验证；**3.14/3.12 的成功不能当作 3.11 兼容的证明**。
- 复现命令（取得 3.11 后）：
  `py -3.11 -m venv tmp/venv311 && tmp/venv311/Scripts/python -m pip install -r requirements.txt`
  然后 `tmp/venv311/Scripts/python tmp/pkg_instance_8090_from_package.py`（配 `RC2_UNPACK` 环境变量）并重跑 `tmp/rc2-package/_c4b_probe_8090.py`。

---

## 未验 / 边界 / 风险（诚实清单）

1. **C5 未验**：本机无 Python 3.11（见上），目标运行时兼容性保留未验，不做「有条件通过」表述。
2. **全栈启动未演练**：包内验证只覆盖 web_ui 数据面（HTTP + 筛选链路）；`start.bat`/`launcher.py`
   的依赖自检补装、便携 Redis、16 个服务进程拉起未在包内跑（会写环境/拉依赖，超出本单边界）。
   属运行时缺口，非包内代码缺陷的证明。
3. **停启持久化与两版本回退演练未做**：§5 提及的「回退演练（两份隔离包之间）」未包含在本次
   C1–C6 指定任务内，标记未验。
4. **前端 UI 手工走查未做**：C3/C4 均为 HTTP 层验证（页面字节、资源 200、API 契约），未做浏览器
   截图级走查；UI 交互缺陷不在本证据覆盖内。
5. **虚拟账号性质**：8090 的建号→登录→筛选走查全部使用**随机口令的测试账号与合成/自导数据**，
  不构成真实生产账号验证；真实凭据是否轮换由用户决定，代理未动任何真实凭据。
6. **白名单补充**：`evals/`、`validators/` 相对任务清单是新增（必要性见 C4 节），请 Lead 复核认可。
7. 旧包按指示保留在 `tmp/rc-package-1024/`（未删）；RC2 包在本单工作区 `tmp/rc2-package/`，
   正式分发位置由用户/Lead 决定。

## 复现命令汇总（均在仓库根执行）

```bash
# C1 残留复扫（只报类别与数量）
C:/Python314/python.exe tmp/rc2-package/_c1_scan.py

# C2 旧包指纹复核
C:/Python314/python.exe -c "import hashlib;print(hashlib.sha256(open(r'tmp/rc-package-1024/weavemind-rc-20261024.zip','rb').read()).hexdigest())"

# C3 前端构建与静态检查
cd frontend && npm run build && cd ..
C:/Python314/python.exe tmp/rc2-package/_c3_static_check.py

# C3 隔离实例（8089）
C:/Python314/python.exe tmp/pkg_instance_8089.py   # 回环 8089；停止：结束进程
C:/Python314/python.exe tmp/rc2-package/_c3_verify_8089.py

# C4 构建与自检
C:/Python314/python.exe tools/build_rc2_package.py --out tmp/rc2-package
C:/Python314/python.exe tools/build_rc2_package.py --verify tmp/rc2-package/weavemind-rc2-20261024T043139Z-ba0f6d57.zip

# C4 解包（工作区之外）与核对
# 解包 tmp/rc2-package/weavemind-rc2-20261024T043139Z-ba0f6d57.zip 到 C:\Users\ding0\Desktop\weavemind-rc2-probe
set RC2_UNPACK=C:\Users\ding0\Desktop\weavemind-rc2-probe\weavemind
C:/Python314/python.exe tmp/rc2-package/_c4_unpack_check.py
C:/Python314/python.exe tmp/pkg_instance_8090_from_package.py   # 回环 8090；停止：结束进程
C:/Python314/python.exe tmp/rc2-package/_c4b_probe_8090.py
```

（所有构建/检查/探针步骤的实测退出码见正文各表；全部为 0，除 C5 的三处「命令不存在/未安装」如实记录。）

## 追记（Lead 复核与重建，同日 12:5x）

1. **evals/validators 白名单偏差复核**：orchestrator_v2/web_ui 对这两个包是**懒加载**（函数体内 import）。原包把 evals/ 整目录（含 cases/claims/brief/临时目录、calibration_report.json —— 这些是实例运行时数据，不是源码）打了进去。复核决定：包内只收两包顶层 `*.py`，`is_forbidden` 增加对应禁入规则（evals 运行时数据不进包）。`tools/build_rc2_package.py` 已改。
2. **默认取数路径修复**：包内没有 `data/screen_cache/official_disclosure_cache.json`，首启 `GET /api/screen/pool` 曾返回 500。修复分两层：
   - `screen_store.default_cache_path()`：官方缓存不存在时回退包内合成样例 `data/samples/synthetic_5_companies.json`（provenance 仍标 synthetic=true、official=false，不会冒充官方数据）；
   - `web_ui._get_screen_pool`：仅「文件不存在」一类错误降级为 200 + 空池 + 明确下一步说明（其它读取失败仍 500 如实报）。
3. **重建包**（仅上述 3 文件变化 + 随包源码）：`tmp/rc2-package-rebuilt/weavemind-rc2-20261024T045737Z-9b1d3f9f.zip`
   - sha256 `a057e46e7b54b92f58756577ecbd260e224f323fcd9be0f59447b046aa96c7a7`，359 文件，verify exit 0（manifest 覆盖 358 项 + 清单自身，无禁入内容；evals/validators 段只含 8+3 个 .py）。
4. **首启冒烟（8091，从解包目录直接启动，回环，已停止）**：
   - `GET /` 200；`GET /api/health` 200；setup-config 后 `POST /api/login` 200；
   - `GET /api/screen/pool` → status ok、source.label=`synthetic-rc2-demo`、official=false、synthetic=true、pool=5 家虚构公司（默认路径命中合成样例）；
   - `POST /api/screen/prepare/plan`（690001/690002.SH × 2024）→ 2 格 `covered`；
   - `POST /api/screen/run`（5 家、revenue_growth>0、latest_each）→ counts `{pass:4, fail:1, insufficient:0, total:5}`，result_hash `02426e32…`；
   - 相关回归重跑全绿：`test_screen_store / test_screen_api / test_screen_prepare / test_screen_consistency / test_data_rc2` 均 exit 0（`tmp/r2-*.log`）。
5. 上一版包（043139Z-ba0f6d57）与新 043139Z→045737Z 之间的差异即为上述 3 文件；旧 zip 保留在 `tmp/rc2-package/` 作为过程记录，**分发以 045737Z-9b1d3f9f 为准**。

## 追记（Lead 最终打包，2026-10-24 13:30 前后）

D 阶段发现浏览器端双标签冲突未被前端处理（`saveWatchNote` 不带 `if_version`），且数据集下拉把「官方缓存物化」误显示成「我上传」。Lead 修 `frontend/src/pages/StockScreen.tsx`（带 `if_version` + 409 提示与重读；下拉按 `official || provenance_kind==='official_cache_materialized'` 标注，并新增 `research_material` 标注）后重建 `frontend/dist`，因此**最终分发包以本次重建为准**：

- 最终包：`tmp/rc2-package-final/weavemind-rc2-20261024T052644Z-2fa79a83.zip`
  - sha256 `d7aef5162b6166c72a0b8e96afd8010106e5b1fe59e7d867d0ed3864dec9979d`，359 文件，12,474,789 B
  - `--verify` exit 0（清单覆盖 358 项 + 清单自身；无禁入内容；evals/validators 仅 8+3 个 `.py`；`data/` 仅合成 5 家样例）
- 取代关系：`045737Z-9b1d3f9f`（sha256 `a057e46e…`）为过程版本，**不再分发**；`tmp/rc2-package-rebuilt/` 与 `tmp/rc2-package/` 的 zip 均保留作记录。
- 最终包首启冒烟（8094，从解包目录直启 `web_ui.py`，回环，已停止）：
  - `GET /` 200；`GET /api/health` 200；登录 200；
  - `GET /api/screen/pool` → `synthetic-rc2-demo`、official=false、synthetic=true、5 家；
  - `POST /api/screen/run`（5 家、`revenue_growth>0`、`latest_each`）→ `{pass:4, fail:1, insufficient:0, total:5}`；
  - 前端资产：`index.html` 引用 `assets/index-DdyeeQ-x.js`，`StockScreen-Ca4ROiJz.js` 内 `if_version` 与 409 提示原文均存在（HTTP 200）。
- 仍未验不变：Python 3.11、`start.bat`/`launcher.py up` 全栈启动（见 `release-gates.md`）。
