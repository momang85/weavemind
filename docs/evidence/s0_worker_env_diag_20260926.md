# S0 同 Worker 环境诊断：事实表与两环境差异（09-26 夜）

日期：2026-09-26。批次：搜索网络与金融资料专项 **S0**（专项 §4）。交付物：运行包
`weavemind-2026.09.26.9-win-x64.zip`（sha256
`b2a80be0c38870367c2806de3372a0efe7d44af55e6a68bbe52487cbd240f9c9`）。

工具：`search_diag.py`（新）。它只做两件事：**离线事实** + **一次有界公开探测**
（≤6 次调用、≤60 秒、无重试、无模型调用），并且**不新建请求点**——每个探测都调用项目既有通道
（`adapters/text_search` 的 Bing、ddgs SDK、`adapters/eastmoney` 结构化、`adapters/cninfo` 披露、
`adapters/transport` 文档下载），因此天然继承既有网络边界。

命令（在包内执行才是 Worker 同环境）：

```
cd <解压后的运行包>
runtime\python.exe search_diag.py            # 人类可读事实表
runtime\python.exe search_diag.py --no-network   # 只做离线事实，不发请求
```

## 1. 两个环境的事实表（同一台机器、同一时刻）

| 通道 | 源码环境 `git:fad3150`（Python 3.14.3，ddgs 9.14.4） | 包内环境 `package:2026.09.26.9`（Python 3.11.9，ddgs 9.16.0） |
|---|---|---|
| `search_html`（Bing HTML，项目函数） | **ok** items=10 0.48s | **ok** items=10 0.53s |
| `search_sdk`（ddgs，**单后端单次**） | **ok** items=5 2.89s（backend=yandex） | **parse_error** 32.11s（`ConnectError … google.com/wml/search`） |
| `disclosure_api`（cninfo） | not_configured | not_configured |
| `structured_api`（东财聚合源） | **ok** items=1 0.30s | **ok** items=1 0.30s |
| `document`（已知公开披露文件） | **ok** 0.21s（取回 HTML 查看页，非 PDF） | **ok** 0.19s（同上） |
| 预算占用 | 4/6 次，剩余 56.1s | 4/6 次，剩余 26.9s |

依赖版本差异（同一次诊断的 `dependencies` 字段）：`ddgs 9.14.4 → 9.16.0`、`urllib3 2.7.0 → 2.8.0`、
`redis 8.0.1 → 8.1.0`、`lxml 6.0.2 → 6.1.3`、`certifi 2026.06.17 → 2026.07.22`，
**`bs4` 源码有（4.15.0）、包内缺失**。两侧 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` 都**未设置**。

三处"环境来源"因此是分开的：包内（上表右列，Worker 真实环境）、源码（左列）、以及"浏览器能打开
主页/别的机器联网"这类**不能**代表 Worker 的观察——后两者不参与结论。

## 2. 这次诊断查实的三件事

1. **包内第一个后端不可用，且要等 32 秒才知道。** 默认引擎清单首位是 `yandex`，而包内 ddgs 9.16
   对它的调用以 `ConnectError（google.com/wml/search）` 结束，耗时 32 秒；源码环境同一调用 2.9 秒
   返回 5 条。这直接解释了 21:00 那次任务的检索放大：worker 的 `_ddg_engines()` 从该清单首位开始，
   而轻量路径 `_search_ddg` 是"逐引擎试到有结果"的阶梯——包内实测**一次调用 90 秒**
   （首轮诊断里亲眼跑到，随后被本轮诊断自己标为超预算）。
2. **Bing HTML 通道两侧都可用**（10 条结果、0.5 秒）。也就是说"外网全断"不成立：
   检索失败是**按引擎/站点**发生的，不是网络整体不可用。
3. **官方披露通道确实未闭合**（`cninfo.enabled()=False`），而"已知公开披露文件"取回的
   **是公告查看页 HTML，不是 PDF**——"有 PDF 解析器"不等于"能发现并取得年报原文"，
   与专项 §3-7 的判断一致。这一条留给 S3。

## 3. 诊断工具自身修掉的两个问题（本轮暴露）

- **预算只有前置检查，没有后置核对**：包内 ddgs 一次调用 90 秒冲穿了 60 秒预算，后续探测全部
  "budget exhausted"。已改为每次调用后核对实际耗时，超预算即改判 `timeout` 并写明
  （`overrun_budget`），不再把"跑超了"写成正常。
- **探测不要走多引擎阶梯**：首版直接调 `text_search._search_ddg`（9 引擎阶梯），等于在诊断里
  复现要收敛的放大模式。已改为**直接单后端单次调用 SDK**。

## 4. 复验命令与结果

```
python -m unittest test_search_quality_unified      # 24 OK（含 S0 诊断 8 例：分类/预算/脱敏/零结果/类型判定）
runtime\python.exe search_diag.py --no-network      # 离线事实：身份/依赖/入口/配置/代理（只报布尔）
runtime\python.exe search_diag.py                   # 有界探测：4/6 次调用、≤60s
```

## 5. 未验与下一步（照实）

- **未做**：不输出代理地址/密钥/客户查询正文（诊断只报布尔与版本号，有单测钉住）；未做全后端探测
  （专项 §4 明确要求不做）；`HTTP次数未知` 如实标注（ddgs 隐藏其请求次数）。
- **未验**：真实 Redis 5、物理断网、另一台干净机器（沿用前批结论，未重开）。
- **S1 要收敛的三处**（由本事实表直接指向）：① 引擎清单与包内实际可用后端不一致（首位即失效，
  且要 32 秒才失败）；② 轻量路径的"逐引擎阶梯"与 worker 的 `alive=None → auto` 重扫；
  ③ 检索缺共享截止与次数预算（本次实测 90 秒/次即为证据）。
