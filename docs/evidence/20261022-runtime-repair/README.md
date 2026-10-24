# RUNTIME-1022 第一批 P0-A：稳定产物寻址（2026-10-22）

> 第二批 P0-B（出口诊断）见 [P0-B.md](P0-B.md)；主机网络人工调整说明见
> [P0-B-manual-host-network.md](P0-B-manual-host-network.md)。
>
> 第一批复查修正（同晚）：跨根恢复改为**只走显式登记**（ambient 扫描系统临时目录会让
> 测试 id 撞上历史残留，真实复现为 `test_p0` 聚合用例 count=1）；绑定索引改为跟随
> 当前工作区根。历史 5 任务复核口径不变，`verify_history.py` 结果仍 5/5 一致。

## 结论（先说清）

新代码验证通过，**在线生效待受控重启**：8080 端口进程 PID 24572 于 10-22 17:08 启动，
本批改动全部在其启动之后，在线实例尚未载入。历史 5 个任务的产物已全部定位到真实根，
哈希已核验（见 `history_verification.json`），海天缺底稿属实（原任务就没产出，不补造）。

## 根因

`workspace.py` 旧版默认根 = `tempfile.gettempdir()/agent_workspace/tasks`。DSH 会话给每个
shell 注入不同的 TEMP/TMP（实测本会话 `gettempdir()` =
`C:\Users\ding0\AppData\Local\Temp\dsh-win-bash-18344-7905fb0c`，诊断会话是另一个），
于是任务在稳定位置的 `agents.db` 里，产物却落在随会话漂移的临时根：打开历史任务时
定位到的是空目录 → 报告页显示"没有底稿/没有交付包"。

## 修改文件（本任务全部改动，不含他人 dirty paths）

| 文件 | 改动 |
|---|---|
| `workspace.py` | 默认根改为模块目录旁 `workspace/`（显式 `WEAVEMIND_WORKSPACE_ROOT` > `WEAVEMIND_DATA_DIR` > 稳定默认，不再依赖 `tempfile.gettempdir()`）；新增 `system_temp_dir()`（OS 真实临时目录，`%LOCALAPPDATA%\Temp`/`/tmp`，不受会话 TEMP 注入影响）；新增历史根登记（`WEAVEMIND_WORKSPACE_LEGACY_ROOTS` 环境变量 + `<稳定根>/roots.json` + 内建已知形态）与 `register_legacy_root()`（显式、幂等、校验：绝对路径/真实目录/拒盘符根）；新增 `locate_task_workspace()`（只读定位：缓存→持久绑定→扫全部根，候选评分=产物标记数+文件数，空壳必然低于完整产物，平局报 `conflict` 并确定性裁定）与 `existing_task_workspace()` / `existing_task_project_dir()`（纯只读，绝不创建）；新增 `bind_task_root()` 持久绑定 `task_roots.json`（**与 agents.db 同目录**——绑定是任务身份元数据，测试隔离 DB 即隔离绑定；绑定目录消失自动回退重扫；跨根发现才写，当前根内不重复落盘）；`ensure_task_workspace` 先定位沿用真实根、找不到才建新并绑定 |
| `web_ui.py` | 4 处读链改为只读定位（原来用会建目录的 `task_project_dir`）：`/api/task/<id>/working_paper` 端点、底稿下载端点、导出清单登记底稿、图表清单读取——任务不存在 → 404，不产生空 `project/` |
| `test_workspace_addressing.py`（新） | 16 用例，见下 |
| `test_task_persistence.py` | `test_local_defaults_unchanged` 按新不变量更新（旧断言钉死的正是 P0-A 要修的 temp 漂移根） |
| `AGENTS.md` | 测试计数 92 → 93（新增本文件） |
| `.gitignore` | 增加 `task_roots.json`（运行时写入，绝不上传；`workspace/` 已在忽略清单） |

## 定向测试与真实退出码

| 命令 | 结果 | 退出码 |
|---|---|---|
| `python test_workspace_addressing.py` | 16/16 OK | 0 |
| `python test_task_persistence.py` | 51/51 OK | 0 |
| `python test_report_version.py` / `test_review_protocol.py` / `test_working_paper.py` / `test_delivery_chain.py` | OK（11/11） | 0 |
| `test_acceptance_adversarial / admission / auth_audit / cancel_semantics / checkpointer / fact_fidelity / financial_analysis / narrative_evidence / offline_delivery / orchestrator_v2 / r0_boundaries / report_quality / review_edit_api / review_isolation / root_budget / task24 / task26 / task_persistence / task_time_optimization / trial_metrics / us_chain` | 全部 OK（financial_analysis 跳过 1、review_edit_api 跳过 3 为既有 skip） | 0 |
| `python -m py_compile workspace.py web_ui.py` | 通过 | 0 |

覆盖的验收点（任务书第一批 1/2/4）：
1. **跨 TEMP/跨进程一致**：两个子进程 TEMP/TMP 各自独立，对同一任务解析出同一路径、
   `financials.json` 字节哈希一致；显式根优先级、`WEAVEMIND_DATA_DIR` 容器语义、
   legacy flat/projects 布局均有测试。
2. **只读纪律**：不存在的任务 `found=False` 且稳定根不产生任何目录/绑定；越界任务 id
   净化后不含路径分隔符、解析结果必须位于根内；同名冲突（同分）显式报 `conflict` 且
   平局裁定确定；2 文件空壳不得顶替完整产物；显式 project 不跨项目串读。
3. **读链隔离**：底稿/数据集/导出 hash/研究状态读取在沙箱（临时 DB/配置/凭据/工作区）
   走通；无 `research_state.json` 如实返回空。
4. **绑定**：跨根发现写绑定（DB 同目录）、绑定目录消失自动失效回退、`ensure` 新任务
   建在稳定根并绑定、已有产物任务沿用真实根。

## 历史核验（任务书第一批 3）

`verify_history.py`（只读，`bind=False`，不写任何文件到产物根）逐个复核：

| 任务 | 实际原目录 | 文件数 | 底稿 | 财务 | 数据集 | 交付包 |
|---|---|---:|---|---|---|---|
| 伊利 `ui-b23642e5de` | `Temp\dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default\ui-b23642e5de` | 51 | ✓ | ✓ | ✓ | 2 zip |
| 茅台 `ui-a0d82a26c4` | 同上（茅台目录） | 51 | ✓ | ✓ | ✓ | 1 zip |
| 洋河 `ui-d59152ebb9` | `Temp\dsh-win-bash-37684-38250d7e\...\ui-d59152ebb9`（60 文件完整目录；另一根的 2 文件空壳被列为候选但未选中） | 60 | ✓ | ✓ | ✓ | 3 zip |
| 海天 `ui-8b28c95ab1` | `Temp\dsh-win-bash-37684-7b69ad23\...\ui-8b28c95ab1` | 26 | ✗ | ✗ | ✗ | 1 zip |
| 三一 `ui-cc853cf86b` | `Temp\agent_workspace\tasks\projects\default\ui-cc853cf86b` | 57 | ✓ | ✗ | ✓ | 3 zip |

- 五个任务全部定位成功，与诊断快照期望的根**一致**，无冲突；逐文件 SHA-256 见
  `history_verification.json`（底稿/财务/数据集/报告/zip 均已记录）。
- 海天味业原任务确实没有产出底稿/财务/数据集（26 个文件为报告与过程记录）——
  **不凭空补造**；三一缺 `financials.json`（诊断一致）。
- 洋河与三一的 2 文件空壳目录被定位器列为候选但**未误选**（评分规则：完整产物
  标记多、文件多者胜）。

## 与诊断快照的对照

- 快照"伊利、茅台旧目录各 51 文件（financials/working_paper/analysis 都在），当前默认
  目录为空" → 复核一致：伊利/茅台各 51 文件全在 `dsh-win-bash-37684-7b69ad23` 根。
- 快照"洋河完整产物在 38250d7e 根（60 文件）" → 一致。
- 快照"三一在当前默认根 57 文件（financials=false）" → 一致（该根就是诊断会话当时的
  `Temp\agent_workspace\tasks`，内建历史根形态覆盖到它）。

## 剩余风险与未解决项

1. **旧产物仍在系统临时目录**：Windows 磁盘清理/临时目录清理可能删掉它们。只读定位已
   恢复可读性，但**实体迁移（复制到稳定根）未做**——按任务书要求不移动不删除；如需
   合并，待批准后提供独立的复制迁移工具（dry-run + 逐文件 SHA-256 清单 + 冲突报告）。
2. **在线生效**：PID 24572 仍在跑旧代码；重启后新默认根 = `<仓库>/workspace`，历史任务
   靠内建历史根 + 绑定定位。重启前建议先做 `roots.json` 显式登记（`register_legacy_root`
   或设 `WEAVEMIND_WORKSPACE_LEGACY_ROOTS`），防临时根被清理后失联。
3. 每次定位未命中会扫一层系统临时目录的 `dsh-*/agent_workspace/tasks`（有界 ≤20 根），
   临时目录异常大时理论上有 IO 开销；定位命中后走缓存/绑定，不重复扫。
4. "可读恢复 ≠ 重新验收通过"：报告/底稿能打开了，其研究质量结论不变。

## 边界遵守

未 commit/push/建分支；未改生产 config/凭据/系统 DNS/代理；未重启在线服务；未付费重跑；
未删除/移动旧产物（仅删除了本批测试误写入 `workspace/task_roots.json` 的沙箱绑定残留，
其中无任何真实产物）；他人 dirty paths（frontend、`docs/evidence/20261012-c5-review`、
`evals/cases/auto_grown.json` 等）原样保留；测试全部指向临时沙箱。
