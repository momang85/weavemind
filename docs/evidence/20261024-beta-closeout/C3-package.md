# C3 试用包与可复现交付报告（BETA-CLOSE-1024）

日期：2026-10-24｜执行：lead

## 1. 端口现状（只读诊断，未启动/未停止任何生产服务）

| 端口 | 状态 | 说明 |
|---|---|---|
| 8080（生产） | **无监听** | `logs/webui.log` 最后写入 2026-10-23 16:17:45；本单未启动、未停止、未部署 8080 |
| 8083 | **无监听** | BETA-1023 的隔离实例（`tmp/r1next_isolated_instance.py`），已随会话结束退出 |
| 8084 | **无监听** | BETA-1023 的 fresh 实例（`tmp/b4_fresh_instance.py`），同上 |
| 8085 / 8086 | 曾监听，现已退出 | 本单 C0–C2 隔离实例；退出原因：启动它们的持久 shell 被 `timeout` 误用中断（已记录，不影响证据） |

结论：不是「服务崩了」，而是生产实例此前已手动停止、隔离实例随会话结束退出；本单全程未触碰 8080。

## 2. 试用包

- 文件：`tmp/rc-package-1024/weavemind-rc-20261024.zip`
- **zip sha256 = `8d5a2fc82202feae2a6cdc3b824cef2a816380909fc4062aa98780b3c1f68819`**（3.7 MB，333 个文件，解包 12.06 MB）
- 清单：包内 `weavemind/MANIFEST.sha256.json` + 包外 `tmp/rc-package-1024/MANIFEST.sha256`（逐文件 sha256）
- 内容：本单**未提交的改动源码**（`web_ui.py` `screen_store.py` `screen_prepare.py`）+ 全部根级运行模块 + 子包（adapters / charts_pipeline / financial_analysis / prompts / quant_research / risk_research / skills / structured_pipeline / examples）+ **前端构建产物** `frontend/dist-screen-check` + 内置官方披露缓存 + `requirements.txt`/`start.bat`/`launcher.py`
- 关键哈希：`web_ui.py 5d7a739e564a6973…`、`screen_store.py 3a294208356dd6e3…`、`screen_prepare.py 1cf68327e41e998d…`、`stock_screen.py 4fbc9ef22af631e2…`（与架构师探针指纹一致，说明筛选实现未漂移）
- 不含：`.env`、`config.json`、`credentials.json`、`agents.db`、`logs/`、`tmp/`、`docs/`、`_trial_*`/`_dbg_*`/`_probe*` 调试脚本（脚本自动扫描：secret-scan 仅命中两个凭据**模块** `credentials.py`/`auth_credentials.py`，其中无密钥字面量，`sk-` 扫描为空）

## 3. 包外启动验证（工作区外解包，8087）

解包到 `C:\Users\ding0\Desktop\weavemind-rc-1024\weavemind`（工作区之外），用包内数据目录启动：

| 检查 | 结果 |
|---|---|
| 启动 | **PASS** — 8087 监听，日志在包目录 |
| 导入不回落工作区 | **PASS** — 首次尝试因缺 `financial_analysis` 直接 `ModuleNotFoundError`（证明 import 只认包内路径），补齐子包后启动成功 |
| 首启建号 | **PASS** — `POST /api/setup-admin` 200，`admin` / `[已脱敏]`（包内新建，生产账号不受影响） |
| 健康与单写者 | **PASS** — `/api/health`：`screen_writer{claimed_by_this_process:true,pid:2744}`，`build.restart_required:false` |
| 真实缓存可用 | **PASS** — `/api/screen/pool` 2 家（`002304.SZ` `600031.SH`）；物化数据集 `official=True, synthetic=False` |
| 跑通一次筛选 | **PASS** — `counts={pass:2, fail:0, insufficient:0, total:2}`；成本账本 `integrity=ok` |
| 停机 | 验证后已停止（pid 2744） |

## 4. 未验项（如实）

| 项 | 状态 | 复现命令 |
|---|---|---|
| Python **3.11** 运行 | **未验** | 本机只有 3.14.3 与 3.12（3.12 缺 `redis`），无 3.11。复现：`py -3.11 -c "import sys;print(sys.version)"` 后按第 3 节步骤启动 |
| 前端/后端**同版本**校验 | **PASS（同包构建）** | 包内 `frontend/dist-screen-check` 与本单源码同批构建；`build.restart_required=false` |
| 无缓存首启 / 合成演示 / 用户导入真实材料 | **PARTIAL** | 首次启动（包内新建 config/DB/存储）PASS；真实缓存读取 PASS；「合成演示」以 8086 实例验证（C2）；用户导入真实材料见 C2 未验项 |
| 停启后数据持久化 | **PASS** | 8085/8086 重启后数据集、清单、成本账本仍在（清单 rev 稳定、integrity=ok） |
| 回滚 | **未验（未做生产部署，无回滚可滚）** | — |

## 5. 发布门（Release Gate）

| 门 | 判定 | 依据 |
|---|---|---|
| 存储一致性（C0） | **通过** | 13+24 项回归、单写者拒绝实测 |
| 数据准备入口（C1） | **通过** | 21/21 HTTP + 真实浏览器 momang 全链路 |
| 浏览器/文件（C2） | **有条件通过** | 原生文件选择/下载落盘/窄屏/时序 5 项未验（环境限制，已给人工步骤） |
| 可复现试用包（C3） | **通过（3.11 未验）** | 包外启动、建号、真实缓存、筛选、成本账本全 PASS |
| 真实测试用户 | **0 人 / 未验** | 本单无外部测试者 |

**阶段完成 ≠ 发布验收**：上述为阶段门结果；发布验收需产品负责人对 C2 未验项与 3.11 环境签字确认。
