# P1-e：取件拒收要分因 —— HTTP 状态 / Content-Type / 类型 / 损坏 vs 真扫描件（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §5 第三条

> 985字节取件拒收要分清HTTP/协议/类型/损坏PDF与真扫描件。先查响应状态、Content-Type、
> PDF头、字节上限及解析错误（含chunked响应处理）；仅对确实有效、可解析但无文本的PDF
> 建议OCR。严禁为镜像可访问而绕代理、TLS、SSRF或反爬。

基线 `126186c`。**未跑付费整链、未联网**：全部用假响应与本地字节离线验证。

## 一、缺口：四种完全不同的事共用一句话

修前 `annual_report_pdf.doc_from_url()`：

```python
raw = data if data is not None else fetch_bytes(url)
if not raw:
    return None                       # 下载失败 / 超限 —— 无类别
pages = pages_text(raw)
if not pages or sum(...) < MIN_TEXT_CHARS:
    logger.info("PDF 无可提取文本（扫描件或受保护）：%s", url)
    return None                       # 解析失败(损坏) 与 真扫描件 合成一句
```

而 `fetch_bytes()` 走的是**文本**通道：

```python
text = get_via_urllib(url, timeout=timeout, encoding="latin-1")
data = str(text).encode("latin-1", errors="replace")   # PDF 是二进制：文本往返
```

两个后果：

1. **响应层信息全丢**——状态码、Content-Type、实际字节数都没带出来。"985 字节取件"
   到底是 HTTP 403 的 HTML 错误页、Content-Type 不对、下载被截断，还是真扫描件，
   日志上分不出来，而**处置完全不同**：
   - HTTP 4xx/3xx → 换来源 / 取真实地址；
   - `not_pdf`（HTML/JSON）→ **别当扫描件**，换来源；
   - `truncated` → **重下**；
   - `corrupt` → 重试；
   - `scanned_no_text` → 只有这一类才该建议 OCR（重下无用）。
2. **二进制走文本往返**——`latin-1` 是 1:1 字节映射，看似等价，但依赖"响应能被
   无损解码再编码"，是一条不必要的风险路径。

## 二、修法（三层，按排查顺序）

### 1. 传输层：新增 `adapters.transport.get_bytes_via_urllib()`

与 `get_via_urllib` 走**同一条**通道与**同一套**前置校验
（`_throttle_and_rewrite` + `_require_egress_ok` + `_validate_public_url`），差别只有：

- **不解码**，直接取字节；
- 带回 `status` / `content_type` / `transfer_encoding` / `body_bytes` / `over_limit` /
  `error_kind` / `error`；
- **非 2xx 不抛**：`HTTPError` 的错误页正文照样读出来（"985 字节取回"常常就是
  一张 403/302 的 HTML 页），状态码单列；
- **3xx 不跟随**（与 raw socket 通道语义一致，防重定向到别处）；
- `max_bytes` 给定时**按上限读**（多读 1 字节判超限），不把超大响应整个读进内存；
- chunked 由 `http.client` 透明解块，是否 chunked 记进 `transfer_encoding` 备查。

**出口纪律不变**：本函数**只有一条通道**，不存在降级直连；代理失败经
`net_policy.classify_network_error` 记 `proxy_error`。

### 2. PDF 层：`fetch_bytes(meta=...)` + `classify_pdf_bytes(data, meta=...)`

类别表（互斥，判不出归 `unknown`，不假装成别的）：

| kind | 判据 | 该做什么 |
|---|---|---|
| `http_error` | 响应状态 ≥ 400 | 换来源；**别当扫描件** |
| `http_redirect` | 3xx（不跟随） | 取真实地址 |
| `too_large` | 超字节上限 | 换分批/换源 |
| `empty` | 0 字节 | 重试/换源 |
| `not_pdf` | 字节头不是 `%PDF-`（HTML / JSON / 其它） | **不是 PDF**；判据里带前 40 字节与 Content-Type |
| `truncated` | 有 `%PDF-` 头但尾 2KB 无 `%%EOF` | **重下** |
| `corrupt` | 解析器报错 | 重试 |
| `encrypted` | 加密且空口令解不开 | 受保护文档，不是扫描件 |
| `scanned_no_text` | **解析成功**、页数正常，但可提取文本 < 40 字符 | **只有这一类**才建议 OCR |
| `fetch_failed` / `proxy_error` / `dns_error` / `timeout` … | 连接层（复用 `net_policy` 类别表） | 见类别 |

**顺序就是排查顺序**：响应状态 → Content-Type → PDF 头 → `%%EOF` 尾 → 解析错误 → 文本层。
字节头确实是 PDF 但 Content-Type 声明不符时**不据此拒收**（字节头比声明更可信），
只把矛盾写进判据供人工核对。

### 3. 编排层：类别落进**该步骤的 result**

`orchestrator_v2._try_pdf_evidence()` 传入 `on_reject` 回调，把
`{"ok": false, "reject_kind": …, "reject_detail": …}` 写进 `result["pdf_evidence"]`，
日志也从一句"未取得正文"改成带类别与判据。成功时同样记
`{"ok": true, "pages": N, "chars": M}`。

## 三、代表性判据串（离线实测）

```
HTTP 403 + text/html + 985 字节
  → http_error  「HTTP 403，响应体 985 字节（Content-Type=text/html; charset=utf-8）」
HTTP 200 + text/html + HTML 正文
  → not_pdf     「取回的是 HTML 页面（31 字节；Content-Type=text/html）：'<!DOCTYPE html>…'」
HTTP 200 + %PDF- 头 + 无 %%EOF
  → truncated   「有 %PDF- 头但尾部 2KB 内无 %%EOF（共 36 字节）」
%PDF- 有头有尾但解析器报错
  → corrupt
%PDF- 解析成功（2 页）但文本 0 字符
  → scanned_no_text「解析成功（2 页）但可提取文本 0 字符 < 40（真扫描件/无文本层，重下无用）」
HTTP 302
  → http_redirect
```

## 四、测试（全部离线，无网络）

`test_net_policy.TestByteChannelMetadata`（新增 8 条）：

| 用例 | 断言 |
|---|---|
| `test_binary_round_trip_and_metadata` | 0x00–0xFF 全字节**原样**往返；状态/类型/字节数带出 |
| `test_http_error_returns_status_and_body_size_without_raising` | 403 + 恰好 985 字节 HTML：**不抛**，状态 403、`body_bytes=985` |
| `test_redirect_is_not_followed` | 302 → `http_redirect`，不跟随 |
| `test_byte_limit_truncates_the_read_and_flags_it` | 5000 字节 + 上限 1000 → 只读到 1000、`over_limit` |
| `test_chunked_transfer_encoding_is_recorded` | chunked 记进元信息 |
| `test_ssrf_guard_blocks_before_any_request` | 被守卫拦下时**请求数 0** |
| `test_proxy_error_is_reported_and_never_falls_back_direct` | `proxy_error`，且**不调用**第二通道 |
| `test_byte_channel_also_refuses_before_any_request_when_proxy_required` | `WM_CONTENT_FETCH_MODE=proxy_required` 下**请求数 0**（该通道吞异常，必须单独断言） |

`test_narrative_evidence.TestPdfEvidence`（新增 4 条）：

| 用例 | 断言 |
|---|---|
| `test_reject_reason_separates_http_type_corrupt_and_scan` | HTML/JSON/非 PDF 字节、截断、损坏、`fetch_failed` 五类各自可辨 |
| `test_scanned_pdf_reports_no_text_layer_not_corruption` | 真扫描件报 `scanned_no_text`（**不是**损坏），且判据写"重下无用" |
| `test_reject_callback_failure_does_not_change_outcome` | 回调自己抛异常不改变拒收结论 |

既有出口纪律用例（`dual_channel_get` 代理失败不降级、`proxy_required` 零请求）**仍绿**；
`test_net_policy` 里"不得出现关闭 TLS 校验写法"的静态检查**仍绿**。

回归：`test_net_policy` + `test_narrative_evidence` **113 OK**；
`test_delivery_chain` / `test_offline_delivery` / `test_search_quality_unified` /
`test_root_budget` 全绿（见提交信息）。

## 五、未做与边界

- **未新增任何服务端请求点**：字节通道与文本通道共用同一处 urllib 出口；
- **未绕代理/TLS/SSRF/反爬**：`_validate_public_url` 与 `_require_egress_ok` 原样先跑，
  被拦下时请求数为 0（有断言）；
- **未接入真正的 OCR**：本批只把"该建议 OCR"的那一类单独判出来（`scanned_no_text`），
  没有引入 OCR 依赖、也没有替用户决定换模型；
- `get_via_socket` 的 raw HTTP/1.0 通道**未**做 chunked 解块（文本通道本轮未改）；
  PDF 字节通道走 urllib，chunked 由 `http.client` 透明处理。这是本轮明确的未覆盖面。
- 全部验证用假响应完成，**没有对任何真实站点发起请求**。
