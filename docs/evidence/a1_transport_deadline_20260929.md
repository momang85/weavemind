# A1：分通道诊断与传输边界（总截止 / chunked 解码 / 容量单一来源）

- 基线：`27d2dee`（A0 之后）。依据 `docs/金融资料获取与事实化补链_20260929.md` §3 与
  `docs/evidence/20260929-acquisition-facts-review.md`「获取诊断」。
- 未重扫外网、未调模型、未动真库；全部离线（本地 `http.server` 替身 + socketpair）。
- 本批**只做能独立验证的部分**：传输边界的三个真实缺陷 + 容量策略统一。
  东财 77 字节的**根因仍未定**（见 §4：需要一次同环境有界请求，本批未发）。

## 1. 三个真实缺陷（改前 → 改后）

| # | 缺陷 | 位置 | 改后 |
|---|---|---|---|
| 1 | `resp.read()` **无总截止**：`urlopen(timeout)` 只管单次 socket 操作，慢体/慢分块整体可远超预算 | `adapters/transport.py:147`（文本）、`:201`（二进制） | 两处都改走 `read_with_deadline`（每次底层读都查钟、chunked 也受检）+ 默认字节上限；`read_timeout`/`too_large` 与 `http_error` 分开报 |
| 2 | **手拆响应头且不认 chunked**：`raw.partition(b"\r\n\r\n")` 之后把 body 当正文 → 分块框架混进 PDF（**PDF 直接损坏**），gzip 也没解 | `net_policy.py:559`（材料直链取件走的正是它） | 换成 `http.client.HTTPResponse(sock)`：成熟解析状态行/头、透明解 chunked；正文用 `read_with_deadline`（总量上限 + 总截止）；gzip 显式解；**3xx 仍不跟随、仍是已验 IP 直连、仍不带凭据** |
| 3 | **时钟不同源导致总截止失效**：`started` 是 `time.time()`（墙钟），`read_with_deadline` 默认用 `time.monotonic()` 比时间 → 余量算成几十亿秒 | 本批新增的 `_read_http_response` | 函数内部用单调钟重新起算 `deadline`；socketpair 慢分块用例实测 0.2s 预算 → 在 1.5s 内抛 `FetchError("读取超时…")` |

第 3 条是**本批自己引入又当场抓出**的：如果不写那条 socketpair 用例，材料直链路径会带着
"看起来有截止、其实永远不触发"的假保证上线。

## 2. 容量策略单一来源（`transfer_limits.py`，新）

| 项 | 旧 | 新 |
|---|---|---|
| 材料上传 | `material_intake.MAX_BYTES = 3 * 1024 * 1024`（硬编码） | `transfer_limits.UPLOAD_MAX_BYTES`（3 MiB，可用 `WEAVEMIND_UPLOAD_MAX_BYTES` 覆盖） |
| PDF 下载 | `annual_report_pdf.MAX_BYTES = 30_000_000` | `transfer_limits.DOWNLOAD_MAX_BYTES`（30 MiB） |
| 正文/二进制抓取 | transport 内隐式无上限 | `TEXT_MAX_BYTES`（8 MiB）/ `BINARY_MAX_BYTES`（64 MiB） |
| PDF 解析页数 | 两处各写 400 | `transfer_limits.PDF_MAX_PAGES`（**1200**：A 股长年报 400 页会误拒；上限仍存在） |

`transfer_limits.explain(kind)` 给用户**具体**超限说明（含数字与入口名），不再是"解析失败"。

## 3. 定向验证（`test_transport_deadline.py`，13 项 OK）

本地替身覆盖：慢体（文本/二进制通道都在总截止处停）、chunked（**逐字节比对解块后的正文**）、
gzip、非 2xx（状态 + 错误页正文都留下）、200+`success:false`（内容层失败不当异常）、
超上限（`too_large` 而不是"解析失败"）、3xx 不跟随、socketpair 慢分块的总截止、
容量单一来源（上传/下载常量确实来自 `transfer_limits`）。

`net_policy` 的守卫未放宽：测试里对本地地址**临时**放行 SSRF 校验（守卫另有专项测试），
产品路径的校验一字未改——用例注释里写明了这一点。

## 4. 东财 77 字节：原因**仍未知**（保持 unknown）

本批没有发新的外网请求，因此**没有**新增根因证据。已确认的只是：
- 两家公司、两类 `reportName`（`RPT_MAINFINADATA` / `RPT_F10_A`）都返回 **77 字节 / 0 行**；
- 旧探针用原始 URL 直取（未走生产契约），且只记了字节数——**没有** HTTP 状态、Content-Type、
  API `success`/`code`/`message`，也没有真实参数（`bare_code()` 去后缀 vs 生产用的完整报表名）。

按 A1 的下一步（需要一次**同环境、经正常入口、有界**的请求）：用生产参数契约发一次请求，
记录状态码 / Content-Type / 字节数 / 脱敏 `success|code|message` / 响应 hash，然后按
`not_implemented` / `empty_result` / `network_error` / `protocol_error` / `schema_changed`
之一归因；归不了就保持 `unknown_cause`，不写"站点/出口限制"。

## 5. 仍未做（A1 剩余）

1. **诊断矩阵**（入口 × 真实 upstream_family × 出口 × 原因码）尚未落地成代码/表；
   `health_registry` 四态与原因层词表的接线未做。
2. `net_policy.fetch_document` 的**逐 IP 完整预算**仍是"每次 recv 前查钟"的循环（够用但
   与 `read_with_deadline` 两套实现并存）——是否合并成一处，留给下一批评估。
3. 重定向/多次地址尝试/重试是否共用根截止与剩余额度：本批只确认 3xx 不跟随，**未**改多地址预算分配。
4. 上传/下载上限**对用户可见的页面提示**未做（只在错误信息里）。
5. A2（官方披露/IR 实际链接发现与正常文件导入）未开始。
