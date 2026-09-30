# -*- coding: utf-8 -*-
"""A1（**已交付部分**）：有界读取的字节上限 + 容量策略单一来源（全部离线）。

本文件**不含** `transport`/`net_policy` 的接线用例——那两处改造在半验证状态下打红了
5 个既有 `test_net_policy` 用例，按纪律**已回退**（见
`docs/evidence/a1_transport_deadline_20260929.md` 的"本批未交付"一节）：
下次要用真实 HTTP 响应形状的替身（`http.client` 需要完整响应行/头与 EOF 语义）重做，
不能靠"改测试到通过"。

留下的两件在本批被独立验证过：
1. `read_with_deadline` 的**字节上限**（`ResponseTooLarge`：与"超时"分开，不再整段读进内存）；
2. 上传/下载/抓取/页数上限的**单一来源**（`transfer_limits`，可用环境变量覆盖）。
"""
from __future__ import annotations

import gzip
import http.server
import socket
import threading
import time
import unittest
from unittest import mock

from adapters import search_runner as sr
from adapters import transport as tr
import net_policy


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status: int, body: bytes, *, ctype="application/json",
              chunked=False, gzip_body=False, drip=0.0):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if gzip_body:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 16):
                piece = body[i:i + 16]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 16):
                self.wfile.write(body[i:i + 16])
                self.wfile.flush()
                if drip:
                    time.sleep(drip)
            return

    def do_GET(self):
        if self.path.startswith("/slowheader"):
            # S1 反例（09-30 复核）：**逐字节慢响应头**。每次 recv 都短于 socket 超时，
            # 于是 `urlopen(timeout=0.05)` 自己不会超时，整体却能拖到 0.5s 以上。
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            self.wfile.flush()
            for hdr in (b"Content-Type: text/plain\r\n",
                        b"Content-Length: 2\r\n",
                        b"X-Slow: " + b"a" * 400 + b"\r\n",
                        b"\r\n"):
                for i in range(0, len(hdr), 4):
                    self.wfile.write(hdr[i:i + 4])
                    self.wfile.flush()
                    time.sleep(0.008)
            self.wfile.write(b"ok")
            self.wfile.flush()
            return
        if self.path.startswith("/slow"):
            self._send(200, b"z" * 400, drip=0.06)
        elif self.path.startswith("/big"):
            self._send(200, b"y" * 5000)
        elif self.path.startswith("/chunked"):
            self._send(200, b"%PDF-1.7 chunked", ctype="application/pdf", chunked=True)
        elif self.path.startswith("/gz"):
            self._send(200, b'{"ok":true}', gzip_body=True)
        else:
            self._send(200, b'{"ok":true}')


class _Server:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestBoundedReadByteCap(unittest.TestCase):
    """`read_with_deadline(max_bytes=…)`：超限报 `ResponseTooLarge`，与超时分开。"""

    def setUp(self):
        self.srv = _Server()
        self.addCleanup(self.srv.close)

    def _open(self, path):
        import urllib.request
        return urllib.request.urlopen(self.srv.url(path), timeout=10)

    def test_over_cap_raises_too_large(self):
        with self._open("/big") as resp:
            with self.assertRaises(sr.ResponseTooLarge):
                sr.read_with_deadline(resp, time.monotonic() + 10, max_bytes=1000)

    def test_under_cap_reads_normally(self):
        with self._open("/ok") as resp:
            self.assertEqual(
                sr.read_with_deadline(resp, time.monotonic() + 10, max_bytes=10_000),
                b'{"ok":true}')

    def test_chunked_body_is_decoded_by_the_bounded_reader(self):
        """真实 `http.client.HTTPResponse`：chunked 由它透明解块，正文逐字节一致。"""
        with self._open("/chunked") as resp:
            self.assertEqual(
                sr.read_with_deadline(resp, time.monotonic() + 10), b"%PDF-1.7 chunked")

    def test_no_cap_means_no_limit(self):
        with self._open("/big") as resp:
            self.assertEqual(len(sr.read_with_deadline(resp, time.monotonic() + 10)), 5000)


class TestNetPolicyChunkedAndDeadline(unittest.TestCase):
    """`net_policy._read_http_response`：成熟解析（chunked/gzip）+ **时钟同源**的总截止。

    用 socketpair 直接喂字节流——不走 `fetch_document` 的公网校验与 IP pinning
    （守卫另有专项测试），只验"响应怎么被读成字节"。
    """

    def _serve(self, payload: bytes, *, drip: float = 0.0):
        srv, cli = socket.socketpair()
        self.addCleanup(srv.close)
        self.addCleanup(cli.close)

        def _feed():
            try:
                step = 8 if drip else len(payload)
                for i in range(0, len(payload), step):
                    if drip:
                        time.sleep(drip)
                    srv.sendall(payload[i:i + step])
            except Exception:                        # noqa: BLE001
                pass
            finally:
                try:
                    srv.shutdown(socket.SHUT_WR)
                except Exception:                    # noqa: BLE001
                    pass

        threading.Thread(target=_feed, daemon=True).start()
        return cli

    def test_chunked_body_is_decoded(self):
        body = b"%PDF-1.7 hello chunked"
        framed = (b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
                  b"Transfer-Encoding: chunked\r\n\r\n")
        for i in range(0, len(body), 8):
            piece = body[i:i + 8]
            framed += b"%x\r\n%s\r\n" % (len(piece), piece)
        framed += b"0\r\n\r\n"
        out = net_policy._read_http_response(self._serve(framed), url="http://x/y.PDF",
                                            egress="direct", cap=1_000_000, budget=5.0)
        self.assertEqual(out["raw"], body, "chunked 必须解开：分块框架不能进正文")
        self.assertIn("chunked", out["transfer_encoding"])

    def test_gzip_body_is_decoded(self):
        body = b'{"ok":true}'
        blob = gzip.compress(body)
        payload = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                   b"Content-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(blob)) + blob
        out = net_policy._read_http_response(self._serve(payload), url="http://x/y",
                                            egress="direct", cap=1_000_000, budget=5.0)
        self.assertEqual(out["raw"], body)

    def test_total_deadline_on_slow_chunked_body(self):
        """反例：`started` 是墙钟而读取用单调钟 → 截止算成几十亿秒、永不触发。"""
        framed = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for _ in range(40):
            framed += b"8\r\n" + b"z" * 8 + b"\r\n"
        framed += b"0\r\n\r\n"
        t0 = time.monotonic()
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve(framed, drip=0.02),
                                          url="http://x/y", egress="direct",
                                          cap=1_000_000, budget=0.2)
        self.assertLess(time.monotonic() - t0, 1.5, "总截止未执行")
        self.assertIn("超时", str(ctx.exception))

    def test_slow_response_header_cannot_outrun_the_root_deadline(self):
        """**慢状态行/头**也吃根截止（K0-c 实机反例：50ms 预算实耗 525ms）。

        `begin()` 里是 `fp.readline()` 连续 recv：只有把截止线包进头解析的每一次底层读，
        预算才真的成立——否则"头还没读完"就永远等下去。
        """
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n"
        payload = head + b"hi"
        t0 = time.monotonic()
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve(payload, drip=0.06),
                                          url="http://x/y", egress="direct",
                                          cap=1_000_000, budget=0.2)
        elapsed = time.monotonic() - t0
        # 头自己就要 0.5s+（8 字节/60ms）：能在这个容差内返回，说明**头解析也被截止线管着**
        self.assertLess(elapsed, 0.35, f"根截止 0.2s，实耗 {elapsed:.3f}s（头没被截止）")
        self.assertIn("超时", str(ctx.exception), str(ctx.exception))

    def test_bounded_gzip_stops_before_inflating_everything(self):
        """压缩炸弹：**解压过程中**就查上限，不是解完整包再检查（K0-c 静态反例）。"""
        bomb = gzip.compress(b"\0" * (4 * 1024 * 1024))     # 压缩后 ~4 KiB
        self.assertLess(len(bomb), 64 * 1024, "夹具前提：压缩体本身在上限之内")
        payload = (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                   b"Content-Length: %d\r\n\r\n" % len(bomb)) + bomb
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve(payload), url="http://x/y",
                                          egress="direct", cap=64 * 1024, budget=5.0)
        self.assertIn("解压后超过上限", str(ctx.exception))

    def test_redirect_not_followed_and_cap_enforced(self):
        payload = b"HTTP/1.1 302 Found\r\nLocation: http://elsewhere/\r\nContent-Length: 0\r\n\r\n"
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve(payload), url="http://x/y",
                                          egress="direct", cap=1000, budget=5.0)
        self.assertIn("重定向", str(ctx.exception))
        body = b"z" * 5000
        big = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        with self.assertRaises(net_policy.FetchError) as ctx2:
            net_policy._read_http_response(self._serve(big), url="http://x/y",
                                          egress="direct", cap=1000, budget=5.0)
        self.assertIn("上限", str(ctx2.exception))


class TestTransportChannelsHonourTotalDeadline(unittest.TestCase):
    """两个 urllib 通道的**总截止**（本地替身；临时放行 SSRF 守卫，产品校验未改）。"""

    def setUp(self):
        import unittest.mock as mock
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        for name, val in (("_validate_public_url", lambda url: True),
                          ("_require_egress_ok", lambda: None),
                          ("_throttle_and_rewrite", lambda url: url)):
            p = mock.patch.object(tr, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_text_channel_stops_at_the_deadline(self):
        """文本通道有总截止（字节通道见下一条，本轮已补齐）。"""
        t0 = time.monotonic()
        with self.assertRaises(Exception) as ctx:
            tr.get_via_urllib(self.srv.url("/slow"), timeout=1)
        self.assertLess(time.monotonic() - t0, 3.0, "总截止未执行（慢体）")
        self.assertIn("deadline", str(ctx.exception).lower())

    def test_binary_channel_stops_at_the_deadline(self):
        """**字节通道也有总截止**（K0-c）：慢体不得把一次 `read` 拖到对端结束。

        保留既有契约：`over_limit` 与"截断到上限"语义不变（下一条 `too_large` 用例）。
        """
        t0 = time.monotonic()
        out = tr.get_bytes_via_urllib(self.srv.url("/slow"), timeout=1, max_bytes=10 ** 6)
        elapsed = time.monotonic() - t0
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error_kind"], "read_timeout", out)
        self.assertLess(elapsed, 3.0, f"总截止未执行（慢体）：{elapsed:.2f}s")

    def test_binary_channel_reports_too_large(self):
        out = tr.get_bytes_via_urllib(self.srv.url("/big"), timeout=5, max_bytes=100)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_kind"], "too_large", out)

    def test_slow_response_header_cannot_be_reported_as_success(self):
        """S1 反例（09-30 复核）：**慢响应头**吃光预算后不得再返回 ok=True。

        旧实现 `_deadline` 在 `urlopen` **之后**才起算：逐字节慢头拖到 ~0.5s，
        之后照样读体成功 → `timeout=0.05` 的取件返回 `ok=True`（复核实测 0.511s）。
        """
        t0 = time.monotonic()
        out = tr.get_bytes_via_urllib(self.srv.url("/slowheader"), timeout=0.05,
                                      max_bytes=100)
        elapsed = time.monotonic() - t0
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error_kind"], "read_timeout", out)
        self.assertIn("总截止", out["error"])
        self.assertIn("响应头", out["error"], "要写明预算是在**头阶段**用尽的")
        self.assertEqual(out["body_bytes"], 0, "超时后不得把体读回来当成功")
        self.assertLess(elapsed, 3.0, f"慢头探针实耗 {elapsed:.3f}s")

    def test_text_channel_slow_header_also_fails_closed(self):
        t0 = time.monotonic()
        with self.assertRaises(Exception) as ctx:
            tr.get_via_urllib(self.srv.url("/slowheader"), timeout=0.05)
        self.assertIn("deadline", str(ctx.exception).lower(), str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_annual_report_pdf_entry_uses_the_bounded_byte_channel(self):
        """验的是**实际入口**（`annual_report_pdf.fetch_bytes`），不只是 helper。"""
        import annual_report_pdf as arp
        meta: dict = {}
        with mock.patch.object(tr, "get_bytes_via_urllib",
                               wraps=tr.get_bytes_via_urllib) as spy:
            data = arp.fetch_bytes(self.srv.url("/chunked"), meta=meta)
        self.assertTrue(spy.called, "PDF 取字节必须走有界字节通道")
        self.assertTrue(data and data.startswith(b"%PDF"), data[:20])
        self.assertEqual(meta.get("status"), 200)


class TestCapacityPolicyIsCentralised(unittest.TestCase):
    def test_limits_come_from_one_place(self):
        import transfer_limits as tl
        import material_intake as mi
        import annual_report_pdf as arp
        # K0-c：上传与披露下载**同一量级**（30 MiB）——4–5 MiB 年报从材料恢复入口
        # 不再被 3 MiB 上限拒绝（同一份文件在下载入口却进得来，是同一个"不一致"）
        self.assertEqual(tl.UPLOAD_MAX_BYTES, 30 * 1024 * 1024)
        self.assertEqual(tl.DOWNLOAD_MAX_BYTES, 30 * 1024 * 1024)
        self.assertEqual(mi.MAX_BYTES, tl.UPLOAD_MAX_BYTES, "上传上限必须来自单一来源")
        self.assertEqual(arp.MAX_BYTES, tl.DOWNLOAD_MAX_BYTES, "下载上限必须来自单一来源")
        self.assertEqual(mi.MAX_PAGES, tl.PDF_MAX_PAGES)
        # 4–5 MiB 的年报必须能进（本轮实机样本量级）
        self.assertTrue(tl.within("upload", 5 * 1024 * 1024))

    def test_upload_env_name_accepts_documented_and_legacy_spelling(self):
        """正式名 `WEAVEMIND_UPLOAD_MAX_BYTES` 生效；旧拼写（少一个 E）仍兼容。"""
        import importlib
        import os
        import transfer_limits as tl
        for env, want in (("WEAVEMIND_UPLOAD_MAX_BYTES", 7 * 1024 * 1024),
                          ("WEAVIMIND_UPLOAD_MAX_BYTES", 9 * 1024 * 1024)):
            with mock.patch.dict(os.environ, {env: str(want)}, clear=False):
                mod = importlib.reload(tl)
                self.assertEqual(mod.UPLOAD_MAX_BYTES, want, env)
        importlib.reload(tl)                     # 还原（模块级常量按导入时读环境）

    def test_explain_is_specific(self):
        import transfer_limits as tl
        msg = tl.explain("upload")
        self.assertIn("30 MiB", msg)
        self.assertIn("材料上传", msg)
        self.assertTrue(tl.within("upload", 1024))
        self.assertFalse(tl.within("upload", tl.UPLOAD_MAX_BYTES + 1))

    # ── 2026-09-29 实机：时间预算也要按通道分（30s 下 4–5 MB 年报必然超时）──

    def test_download_budget_is_wider_than_text_but_still_bounded(self):
        import transfer_limits as tl
        self.assertGreater(tl.timeout_of("download"), tl.timeout_of("text"),
                           "披露文件下载要比正文抓取宽（大文件 + 慢站点）")
        self.assertLessEqual(tl.timeout_of("download"), 600.0, "放宽不等于无上限")
        self.assertIn("披露文件下载", tl.timeout_explain("download"))

    def test_callers_use_the_download_budget(self):
        """调用点不许再各自硬写 30s：材料直链 / PDF 取字节 / web_fetch worker 同源。"""
        from pathlib import Path
        root = Path(__file__).resolve().parent
        mi_src = (root / "material_intake.py").read_text(encoding="utf-8")
        self.assertIn('timeout_of("download")', mi_src,
                      "材料直链取件必须用下载通道的总截止")
        arp_src = (root / "annual_report_pdf.py").read_text(encoding="utf-8")
        self.assertIn('timeout_of("download")', arp_src)
        wf_src = (root / "workers" / "web_fetch_worker.py").read_text(encoding="utf-8")
        self.assertIn("timeout_of(", wf_src, "web_fetch worker 按 URL 类型选通道")
        self.assertNotIn("timeout=30,", wf_src, "不再硬写 30s")

    def test_env_override_applies_to_timeouts(self):
        import importlib
        import os
        import unittest.mock as mock
        import transfer_limits as tl
        with mock.patch.dict(os.environ, {"WEAVEMIND_DOWNLOAD_TIMEOUT": "7"}):
            reloaded = importlib.reload(tl)
            try:
                self.assertEqual(reloaded.timeout_of("download"), 7.0)
            finally:
                os.environ.pop("WEAVEMIND_DOWNLOAD_TIMEOUT", None)
                importlib.reload(reloaded)
        self.assertGreater(tl.timeout_of("download"), 7.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
