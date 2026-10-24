# C1 数据准备入口收口报告（BETA-CLOSE-1024）

日期：2026-10-24｜执行：lead｜隔离实例 8085（tmp/closeout-8085）

## 1. 结论

- **PASS（机器验收）**：`logs/c1-http-prepare.log` — 「RESULT: PASS — 21/21 checks」（`tmp/c1_http_prepare.py`）
- **PASS（真实浏览器，momang 账号）**：截图 `screens/c1-03-momang-real-collect-run.png`（截图内可见 momang 管理员、真实采集数据集、run_id、来源与证据链接）
- 单元测试 `test_screen_prepare.py` 8/8 通过；`screen_prepare.py` 新模块 471 行。

## 2. 入口形态（产品语言）

「数据准备：我已有材料 / 缺哪几格 / 从哪来的（**在已有数据里搜索 ≠ 取数**）」：

1. **我已有材料**：内置官方披露缓存（服务端配置，只读）+ 本作用域数据集 + 研究清单 + 过往运行。
2. **覆盖预览**：按「公司 × 期间」给 有材料 / 缺口，并注明 `via`（我的数据集 / 内置官方缓存）；**这是覆盖预览，不是取数**（压测证据：预览只 +1 previews，网络计数不变）。
3. **有作用域的数据集**：`save_dataset_from_source` 由服务端判定身份（`official` / `user_provided` / `synthetic`），用户不能声明。
4. **筛选 → 原文**：数据集行带 `source_url`；冻结运行的证据面板逐条给读数算式 + 来源文档（真实可见 cninfo PDF：`http://static.cninfo.com.cn/finalpage/2025-04-18/1223129214.PDF`）。

## 3. 数据源选择表（授权/费用不知道就写 unknown）

| 来源 ID | 名称 | 形态 | 授权 | 费用 |
|---|---|---|---|---|
| `official_cache` | 内置官方披露缓存（整理后宽表，只读） | 本地 | unknown | unknown |
| `user_upload` | 我自己的材料（CSV/底稿） | 本地 | 自有材料 | 0 |
| `adapter:eastmoney` | 东方财富数据中心（年报口径财务指标） | 联网 | unknown | unknown |
| `adapter:cninfo` | 巨潮资讯（官方披露 PDF 原文） | 联网 | unknown | unknown |

## 4. 取数约束与错误处理

- **只接受「来源 ID + 公司 + 期间」**：body 含 `url/urls/path/file/filepath` 一律 400 `client_url_rejected`（3 组反例已测）。
- **同主机策略拒绝一次就停批**：首家公司 `denied_by_policy` 后其余 `skipped_policy_blocked`，不再逐个硬试（历史问题「35 次相同失败」不复现）；`network_requests = attempts - denied`，被拒请求不计入已抓取。
- **探测模式**（先试一家）：只试第一家，其余 `not_attempted`，被拒明细与真实连接分列。
- **真实 vs 合成覆盖分账**：官方缓存物化 `official=True/synthetic=False`；适配器取数 `adapter_collected/适配器取数（未核验）`；合成缓存 `synthetic_demo/合成演示数据（不进真实候选）`（本单实测：合成数据集在界面与运行横幅都标「不进入真实候选、不进入交付与验收证据」）。
- **存储失败**：`StoreWriteError` → 503 `store_unavailable`（「没有写入任何数据集，可原样重试」）。

## 5. 隔离响应覆盖（**模拟传输，明确标注 simulated**）

| 情形 | 触发方式 | 结果 |
|---|---|---|
| 成功 | 真实 eastmoney 探测（momang，1 家） | 2 行、`prepare_requests+1`、`network_attempts=1` |
| 限流/超时 | 传输层 `_MIN_INTERVAL` 3s + 单次超时 | 归入 `dns_or_timeout`/`connect_error`，计入 `network_failed`，不冒充成功 |
| 策略拒绝 | `net_policy.validate_public_url` 固定拒绝（**simulated**：monkeypatch，非真实网络） | 批量停止、`denied>=1`、`requests=0`、不生成数据集 |
| 缺字段 | 适配器返回非 dict/缺 financials | `schema_error`，该公司 `no_data`，其余继续 |
| 部分成功 | 一家成功一家缺 | 只物化成功公司，明确「不宣称全池完成」 |

## 6. 未验 / 边界

- cninfo 适配器默认关闭（`WEAVEMIND_CNINFO_ENABLED` off），本单只走 eastmoney 真实探测；cninfo 取数仅以「来源表 + 已有材料链接」体现。
- 未调用任何付费接口；未改网络/凭据/安全配置；未碰生产数据目录。
