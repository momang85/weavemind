# H0 提交防护与 C2 测试门禁（2026-09-27，Harness 接续）

批次：**H0**（指令 `docs/Harness接续执行指令_20260927.md` §2-H0）。基线 `2519d9a`；
本批提交 `8a44f85`。范围只有两行配置与文档更正，不含实现改动。

## 1. H0.1 配置备份忽略规则

**缺口**：`.gitignore` 原有两条模式

```
config.json.bak        # 只匹配这一个确切文件名
config.json.*.bak      # 要求以 .bak 结尾
```

都匹配不到 `config.json.bak.<tag>-<timestamp>` 形态（后缀在 `.bak` 之后），
因此 `config.json.bak.f3a-20260920-102321` 一直显示为 `??` 未跟踪项——
距一次 `git add .` 入库只差一步。

**修法**：在既有"本地配置备份"小节内新增一行 `.gitignore`：

```
# 带标记后缀的备份：config.json.bak.<tag>-<timestamp>（两个旧模式都匹配不到）
config.json.bak.*
```

**验证（只输出路径，不读/不打印任何值）**：

```
git check-ignore -v config.json.bak.f3a-20260920-102321   # 修前 exit=1，修后命中新规则
```

修后本机 7 个备份逐条全部忽略：`config.json.bak`、`config.json.20260912.bak`、
`config.json.damaged.bak`、`config.json.probe-scratch.bak`、`config.json.wizard-e2e.bak`、
`config.json.wizard-live.bak`、`config.json.bak.f3a-20260920-102321`。

**是否已泄露**：`git log --all --oneline -- 'config.json.bak*'` **无输出**——含密钥的备份
从未入库；本机存在不等于已发生泄露。按指令**不自动轮换密钥**、不删除/移动任何备份。

## 2. H0.2 C2 测试纳入 CI 门禁

**缺口**：`test_question_assessment.py` 随 C2 提交（`291c117`），但**已提交的 `ci.yml` 不含
它的步骤**（HEAD 46 步）；该步骤只存在于工作区一处既有未提交改动里 → 这批语义最重的用例
（判据表、覆盖判定、主体适用性、方向措辞）当时没有 CI 门禁。

**修法**：精确接续该 hunk（2 行）并提交。

**验证**：

| 检查 | 结果 |
|---|---|
| `git show HEAD:.github/workflows/ci.yml` 含 `test_question_assessment` | 1 处 |
| 已提交 CI 的测试步骤数 | **47** |
| 跟踪的 `test_*.py` 文件数 | **47** |
| 双向差集（CI 有而文件无 / 文件有而 CI 无） | **均为空** |
| `test_question_assessment` 是否被跟踪 | 是（`291c117`） |

即"CI 指向不存在文件"的风险不存在，也没有漏跑的文件。

**暂存纪律**（先查暂存区为空，再精确暂存）：

- 只暂存：`.gitignore` 的本批新增 hunk + `ci.yml` 的该步骤 hunk（该文件 diff 仅此一处）。
- 未暂存保留：`.gitignore` 的 `dump.rdb`（**09-19 既有改动**，`M` 状态仍在）。
- 未触碰：`templates.json`、`evals/cases/auto_grown.json`、4 处 docs 脏改、`dist/`、
  `_trial_*`×6、`probe_batchA.py`、`trial_tasks.json`、`worker_base.py.log`、`agents.db`。
- 未使用 `git add .`；未恢复任何脏文件；未创建/移动/删除工件。

## 3. H0.3 文档更正（归因与过时事实）

| 文件 | 原表述 | 改为 |
|---|---|---|
| `c2_question_contract_20260927.md` §6 | "该文件已在**架构师**未提交的 `ci.yml` 里挂上" | "既有未提交改动……**作者未核实其来源**，按既有改动记录，不归为架构师改动"；并记 H0 已纳入 `8a44f85`（46→47 步） |
| 同上 | "（18 例……）" | "（20 例）"——本批实测 `Ran 20 tests`，原文计数过时 |
| `n4_scenario_drills_20260926.md` §9 | "本地 ci.yml 有一行未提交改动**指向不存在的** `test_question_assessment.py`" | 该文件已由 C2 建立并提交，H0 已纳入步骤，现为 47 步；来源未核实按既有改动记录 |
| `c1_material_entry_20260927.md` §2 | "里面**有别人的**未提交工作" | "其中有既有未提交工作，**来源未核实**" |

## 4. 未验 / 未做（照实）

- **未推送**：远端 CI 仍是 46 步；"CI 里这一步真能跑绿"要等推送后由远端复跑确认。本批不推送。
- **未重跑全仓**：一行忽略规则 + 一行门禁，用 `git check-ignore` 逐条与 CI 文件集双向差集作证据；
  未跑 47 个测试文件来"证明"配置正确。
- **不属本批**（按指令明确不做）：清理 75.84MB `worker_base.py.log`、`agents.db`+WAL、44 项未跟踪物、
  7 个配置备份；密钥轮换；模板/模型/权限/代理/门禁改动。
- 工作区仍有其他既有脏改（`templates.json` 保护文件、`evals/cases/auto_grown.json` 运行期生长、
  4 处 docs），本批未动、也未提交。
