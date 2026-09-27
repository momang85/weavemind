# S2 下半：健康状态语义与快照实例身份

日期：2026-09-27。批次：搜索网络与金融资料专项 **S2 下半**（专项 §6 第 4 条）。
上半见 `s2_network_egress_20260927.md`（出口显式化、代理失败不落直连、同一套诊断类别）。

## 1. 先钉住旧行为（问题的形状）

`health_registry` 改造前有 5 套健康源、字段名在 `healthy/ok/degraded/cooldown_until` 之间混用，
而最要命的一条是：**没有快照时 `ok=True`**（`probe_search`/`probe_market_source` 都写
"无快照（视为未知）"却返回绿色）。也就是说"没观测到"和"观测到健康"在页面上长得一模一样，
而这两件事的处置完全不同（前者要去看工作台/连接，后者什么都不用做）。

## 2. 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `health_registry._entry` | `{name, ok, reason, since, source_process, detail}` | 增加 **`state`**（权威判定）、`instance`、`checked_at`、`stale`；`ok` 降为兼容视图（`ok = state ∈ {available, degraded}`） |
| 状态集 | 只有 `ok` 二值 | `available / degraded / unavailable / unknown`（`STATES`，可被测试与页面直接引用） |
| `probe_search` / `probe_market_source` | 无快照 → `ok=True` | 无快照 / 快照过期 / 快照来自**别的实例** → `unknown` 且 `ok=False`；部分引擎/源坏 → `degraded`（仍 `ok=True`）；全坏 → `unavailable` |
| `snapshot_meta()` / `split_snapshot()` / `snapshot_freshness()`（新） | 无 | 快照自带 `_instance`/`_checked_at`；读取方按 `SNAPSHOT_TTL_SECONDS`（默认 300 秒，`WM_HEALTH_SNAPSHOT_TTL` 可调）判过期，异实例一律 `unknown` |
| `instance_id()`（新） | 无 | `WM_INSTANCE_ID` 优先，否则工作区根目录 + 端口的 sha256 前 10 位（不含用户名等） |
| `worker_base._publish_health_snapshot`、`adapters/source_health._publish_snapshot` | 只写状态表 | 附带实例身份与观测时刻（写失败不影响主链路，与既有约定一致） |
| `unhealthy()` | `not ok` | `state ∈ {unavailable, unknown}`（`degraded` 不算——它仍可用）；新增 `degraded_items()` / `unknown_items()` |
| `web_ui` `/api/config/requirements` | 只给 `health_ok`/`health_reason` | 增加 `health_state` / `health_stale` / `health_checked_at`，并单列 `unknown_health` |
| `frontend Settings` 徽章 | `ok=false` 一律红"异常" | `unknown` → 灰"未检查"/"读数过期"（tooltip 说清是无快照还是过期）；`degraded` → 黄"降级"；`unavailable` → 红"异常"（欠费仍单独识别） |

**没动的**：探针集合、`llm/embedding/sandbox/planner/mcp/lora` 的判据、沙箱"隔离就绪 ≠ 能跑"
的双事实表达、告警去重逻辑；`frontend/dist` 与源码一起重构建（本仓库要求两者同提交）。

## 3. 最小验收逐条（专项 §6 第 4 条）

| 验收项 | 证据 |
|---|---|
| 缺快照不能绿色可用 | `test_no_snapshot_is_unknown_not_healthy`：`_redis_get` 返回空 → `search`/`market_source` 均 `unknown` 且 `ok=False` |
| 过期不能绿色可用 | `test_stale_snapshot_is_unknown_and_other_instance_too`：观测时刻推到 TTL 之前 → `unknown` |
| 快照带实例身份、防互串 | 同上：快照写着别的实例 → `unknown`，原因写明"来自其它实例"；写入侧 `snapshot_meta()` 由两个发布点统一携带 |
| 一个可选源坏 ≠ 全部不可用 | `test_partial_failure_is_degraded_not_unavailable`：1/2 引擎坏 → `degraded` 且 `ok=True`；2/2 坏 → `unavailable` |
| 页面状态准确 | `Settings.tsx` 徽章四态（未检查/读数过期/降级/异常）；接口字段 `health_state`、`unknown_health` 已就位；`test_frontend_guards` 绿 |
| 不破既有调用方 | `test_snapshot_uniform_fields_and_covers_dependencies`（字段全集与 `ok`/`state` 一致性）、`unhealthy()` 语义调整后的 `test_unhealthy_filters_only_failing` |

## 4. 命令与结果

```
python -m unittest test_p0 test_frontend_guards test_settings_requirements test_startup_readiness
# 543 tests OK（含新增健康状态 4 例；前端 dist 与源码同版重建）
npm run build   # frontend/dist 重建（Settings 徽章四态）
```

## 5. 未做 / 未验（照实）

- **未做**：`web_ui.source_health` 本地字段与统一 `dependencies` 的**最终同源收敛**（专项 §3-4
  提到的"旧字段与统一 dependencies 需最终同源"）——本批只把统一视图做实并接进设置页，
  `/api/status` 里那个本地 `source_health` 字典仍在（读取方是页面旧字段）。
- **未做**：S4 的"资料获取能力：可用/部分/未检查/不可用"首屏视图与一次主动检查——本批只提供了
  状态语义与接口字段。
- **未验**：本批为离线单测 + 前端构建；**未在运行中的页面上用真实快照看过四态**（见下条）。
- **运行实例复验（2026-09-27 09:1x，本机 `localhost:8080`）**：系统重启后按既有顺序
  `deps --fix` → `start` 起回 16/16 服务（便携 Redis v8；8080 上只有 webui pid=13724，单实例）。
  服务启动时间晚于 S2b 提交（09:09:22 < 09:10:46），且服务端 `index.html` 引用的主包
  `assets/index-B42K-_sS.js` 与本批构建产物同名 ⇒ **后端与前端都在跑新代码**。
  实测 Redis 里 `search_engine_health` / `wm:source:health` **尚无快照**（搜索健康只在 worker
  跑过检索后发布、行情源只在用过行情后发布）⇒ 页面对 `search`/`market_source` 应显示
  "未检查（unknown）"，正是本批要的语义（旧代码这两处会显示绿色"正常"）。
  真机页面的**四态渲染**仍未验：重启后 `sessions` 表为空，admin 账号（`momang`/`reviewer`）
  密码不在执行者手上——**不伪造会话**，留用户登录后当场复核（登录后打开设置页即可看徽章）。
- **包内已复验**（运行包 `2026.09.27.1`，见 `s2_network_egress_20260927.md` 第 5 节）：
  包内 `health_registry.snapshot()` 给出 `search`/`market_source` = `unknown` 且非绿、
  `code_sandbox` = `unavailable`、其余 `available`；包内 `frontend/dist` 的 `Settings-*.js`
  含"未检查/读数过期"徽章文案（与源码同版）。
- 实例指纹取"工作区根 + 端口"：同一台机器上若两个实例共享同一工作区与端口，指纹会相同
  （现实里端口不同即不同）；`WM_INSTANCE_ID` 可显式指定以覆盖。
