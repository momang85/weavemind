# 09-28 下午复核：首批反例复现结果（A / B）

基线 `1a5fd9b`。全部**隔离复现**（stdlib + 临时 SQLite/工作区，零模型、零外网、不连真库）。

## A. 重复执行残余（4 条，全部复现并已修）

| 反例 | 复现方式 | 修前行为 | 修后 |
|---|---|---|---|
| A-1 二次 DB 读取失败仍放行 | 真 SQLite：先 `mark_received`→`promote`→`mark_running`（行已是 RUNNING），再把 `_connect` 换成"**第一次成功、之后抛错**" | `promote_received` 返回 **`absent`** → 调用方走旧路径、`accepted=True` | 返回 **`error`**；`accept_task_request` 拒绝 |
| A-2 同 QUEUED 重复取得启动权 | 同一行连续两次 `mark_running` | 第二次仍返回 **True** | 第二次返回 **False**（真实受影响行数） |
| A-2 端到端双线程 | 同一份 `list_unstarted_queued` 快照喂两次 `resume_unstarted_queued` | **起了 2 个执行线程** | 只起 **1 个**（第二次 `n=0`） |
| A-4 RECEIVED 恢复丢运行选项 | `claim_receipt(run_options={auto_run:False, report_confirm:True, template_steps:['s1']})` → `list_received` | `list_received` **没有该列**，恢复后回默认值 | 带出原选项，恢复时 `auto_run=False` 不被改写 |

根因（都已定位到行）：
- `task_state.read_task` 把**任何**异常吞成 `{}` → `promote_received` 里那句
  `except` 是**死代码**，DB 故障被当成"缺行"。修：新增 `read_task_checked()` 返回
  `found/not_found/error`，执行权裁决改用它（`read_task` 语义不变，供"读不到按空"的调用方）。
- `mark_running` `commit` 后无条件 `return True`。修：返回 `rowcount > 0`。
- `list_received` 的 SELECT 没有 `run_options_json`。修：补列 + 恢复时传参。

## B. 计算溯源的两处"数值互借"（全部复现，**修前**）

### B-1 伪造派生行 100% 溯源

```
rows=[]  derived=[{entity: 洋河股份, metric: ghost, value: 999, unit: 亿元,
                   formula: "1+1", derived_from: ["missing-a","missing-b"]}]
正文：「洋河股份 2024 年收入增加 999 亿元。」× 3
```

**修前**：`pass=True`、`traceable=1/1`、`computed_count=1`、`arithmetic_ok=True`、`ratio=100%`。

即：**只查"非空"，既不求值也不核对输入是否存在**。公式写成 `1+1`、值写成 999、
输入 ID 全是不存在的 `missing-a/b`，照样进"计算"通道。

### B-2 跨公司数字被借来当本公司的差额

```
rows=[{entity: 宁德时代, metric: revenue, period: 2023年, value: 100, unit: 亿元},
      {entity: 比亚迪,   metric: revenue, period: 2024年, value: 200, unit: 亿元}]
正文：「洋河股份 2024 年收入增加 100 亿元。」
```

**修前**：`pass=True`、`traceable=1`，命中 `workpaper_derived`，note =
"与底稿派生事实一致（带算式与输入 fact_id）"。

即：**按 metric 聚合出的无主体差额**（|200−100| = 100）被当成本任务的派生事实，
洋河的数字命中了"宁德时代与比亚迪之间的差额"。跨主体、跨界别、跨币种一律没拦。

### B-3 归因规则错判"主要来自毛利端"

纯函数反例：净利降 101、毛利降 100 → 毛利线以下净额变化 = −101 −(−100) = **−1**。

- 金额事实：毛利端贡献 100/101 ≈ **99%** 的降幅，"利润下滑主要来自毛利端"**是对的**；
- **修前** `_gross_line_conflict("利润下滑主要来自毛利端", -1)` 返回
  "归因方向与底稿相反…"→ 该句被判 `unsupported`。

根因：拿 `gap < 0`（"毛利线以下是净拖累"）当成了"主因在毛利线以下"。两者不是一回事。
正确判据是**比较两侧的金额贡献绝对值**（`|Δ毛利|` vs `|gap|`），率变化另列。

## 已实施（A + B）与修后读数

### A（全部落地）

| 项 | 改动 |
|---|---|
| A-1 | `task_state.read_task_checked()` 返回 `found/not_found/error`；`promote_received` 改用它（`read_task` 语义不变，供"读不到按空"的调用方） |
| A-2 | `mark_running` 返回 `UPDATE` 的**真实受影响行数 > 0**；两份相同快照只有一份能起线程 |
| A-3 | 租约记**单调钟** `renewed_mono` + `gen`；`ownership_held()` 加本地有效期（TTL+宽限）；`ownership_lease()`/`ownership_lost()`/`_owner_fingerprint()`；`started` 事件记持有者**指纹**；`_lease_superseded(task_id)` 作为**按任务**的派发/落库闸门；已取消是终态不得被覆盖 |
| A-4 | `record_submit_event(owner=...)`、`list_received` 补 `run_options_json`、`resume_received_tasks` 按原选项恢复 |

> A-3 的收窄（重要）：第一版用"本进程曾经持有过、现在没了"这种**全局粘性**判据，
> 结果同一进程里**与租约无关**的任务也落不了库（实测 `test_offline_delivery` 的交付状态
> 变成 `unknown`）。改成**按任务**判：只有"这个任务启动时的持有者指纹 ≠ 我的、且我现在
> 也没持有租约"才拦。新任务由**入口**闸门（`accept_task_request` / `resume_*`）负责，
> 已启动任务由这道按任务的闸门负责——两层合起来才覆盖"入口 + 已启动"。

### B（全部落地）

- 新增 `_eval_arith()`：**AST 白名单**四则运算（禁 `eval`；函数调用/幂/下标/比较一律 None）；
- 新增 `_verify_derived_row()`：逐输入 ID 存在 + 受控求值 + 复算值≈记录值（0.5%/0.01）
  + 同主体/币种/金额量纲/口径 + 期间非空 + **算式里每个字面量都要对得上某个输入**
  （换算常量 `*100` / 末尾 `±1` 除外，用 AST 角色判定）；
- 差额改为按 **(主体, 指标, 币种, 金额量纲, 口径)** 分组（不再按 `metric` 聚合）；
- 结构化事实以 **JSON 字符串**通道参与匹配（`sources` 的契约是 `dict[str,str]`，
  直接放 list 会让别处对值做正则时抛 `expected string or bytes-like object, got 'list'`——
  实测踩到并修）；该键在**所有文本匹配通道**里显式排除，否则 JSON 里的数字又会被当文本命中；
- 匹配 `_match_derived_fact()`：值按**绝对值**比（符号由方向词承载）+ 量纲一致
  + **主体不冲突**（两侧都有值时要求互相包含，或与任务目标共享专名片段）；
- B-3：`_gross_line_conflict()` 改判据为**金额贡献比较**（`|Δ毛利|` vs `|gap|`），
  取不到 Δ毛利或两侧相差 <2% → 不表态。

### 修后读数

| 检查 | 修前 | 修后 |
|---|---|---|
| B-1 伪造派生行（`1+1`/999/输入不存在） | traceable=1（100%） | **0** |
| B-2 跨公司差额被借 | traceable=1 | **0** |
| B-3 净利降 101/毛利降 100 →"主要来自毛利端" | 误判 unsupported | **不标**（与底稿一致） |
| 洋河原句"侵蚀主要发生在毛利线以下"（gap=+4.58/Δ毛利=−38.01） | unsupported | **仍 unsupported**（原结论保留） |
| 冻结样本溯源率 | 94%（352/375，含互借） | **94%（352/375）**，但计算通道 **只认复算通过的事实** |
| A-1 二次读故障 | `absent` → 放行 | **`error`** → 拒绝 |
| A-2 同 QUEUED 二次认领 | True（双线程） | **False**（单线程） |
| A-3 续租过期一小时 | `held=True` | **失租**；按任务闸门拦截迟到结果 |
| A-4 RECEIVED 恢复 | 丢运行选项 | **原样恢复** |

**分账**（冻结样本，375 个内容数字）：引用 251 / **计算 101** / 定位元数据 48（不进分母）
/ 未支持 22。计算类 101 条**全部**来自"复算通过"的结构化事实
（16 条算式派生 + 可复算同期差额），不再有"字符串里恰好有同值"这种命中。


- 架构师对 **94% 不能作为验收通过证据**的判断成立：新增的计算通道**可以接受伪造公式
  与跨公司数字**，支持条件被放宽（阈值 0.7 未动，但"支持"的定义松了）。
  我此前把它当作 P1-a 的成果交付，**这个定性需要更正**——它是"假失败减少了"，
  但同时引入了"假通过"，两者必须一起说。
- 已交内容不受影响的判断：A-1/A-2/A-4 是纯收紧；B-1/B-2 需要把"字符串通道"换成
  **结构化复算**（受控求值 + 逐输入存在 + 主体/币种/单位/期间一致），B-3 需要改判据。
