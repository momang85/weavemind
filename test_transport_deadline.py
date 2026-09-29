# -*- coding: utf-8 -*-
"""A1：传输边界的**总截止**、chunked 解码与容量策略（本地替身，全部离线）。

覆盖的入口（都在真实调用链上）：
- `net_policy._read_http_response`（材料直链取件走的就是它，已验 IP 直连 + 3xx 不跟随）；
- `adapters.transport.get_via_urllib` / `get_bytes_via_urllib`（两个 urllib 通道）；
- `adapters.search_runner.read_with_deadline` 的字节上限（`ResponseTooLarge`）。

反例（改前会错）：
1. 慢体/慢分块：`resp.read()` 会一直读到对端结束，`urlopen(timeout)` 只管单次 socket 操作
   → 总耗时远超预算；
2. **chunked 正文**：旧 `net_policy` 手拆 `\\r\\n\\r\\n` 后把 body 当内容 → **分块框架混进正文**
   （PDF 直接损坏）；
3. 超过容量上限：旧实现要么整段读进内存，要么把"太大"记成"解析失败"。
"""
from __future__ import annotations

import gzip
import http.server
import socket
import threading
import time
import unittest
from pathlib import Path

from adapters import search_runner as sr
from adapters import transport as tr
import net_policy


class _Handler(http.server.BaseHTTPRequestHandler):
    """本地替身：按路径给出慢体 / chunked / 200+错误体 / 非 2xx / 超大。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):                       # 静音
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
                if drip:
                    time.sleep(drip)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for i in range(0, len(body), 16):
            self.wfile.write(body[i:i + 16])
            self.wfile.flush()
            if drip:
                time.sleep(drip)

    def do_GET(self):
        if self.path.startswith("/slow"):
            self._send(200, b'{"ok":true,"pad":"' + b"x" * 400 + b'"}', drip=0.06)
        elif self.path.startswith("/chunked"):
            self._send(200, b"%PDF-1.7 chunked-body-bytes", ctype="application/pdf",
                       chunked=True)
        elif self.path.startswith("/gz"):
            self._send(200, b'{"ok":true,"gzip":"yes"}', gzip_body=True)
        elif self.path.startswith("/err200"):
            self._send(200, b'{"success":false,"code":"NO_PERMISSION","message":"denied"}')
        elif self.path.startswith("/403"):
            self._send(403, b"<html>forbidden</html>", ctype="text/html")
        elif self.path.startswith("/big"):
            self._send(200, b"y" * 5000)
        else:
            self._send(200, b'{"ok":true}')


class _Server:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestBoundedReads(unittest.TestCase):
    """本地替身直连：**临时放行 SSRF 守卫**（守卫另有专项测试）。

    这里验的是"读取路径有没有总截止/上限"，不是"守卫拦不拦内网"——把本地地址当公网
    只是在测试里放行校验，产品路径的校验一字未改。
    """

    def setUp(self):
        import unittest.mock as mock
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        for name, val in (("_validate_public_url", lambda url: True),
                          ("_require_egress_ok", lambda: None),
                          ("_throttle_and_rewrite", lambda url: url)):
            patcher = mock.patch.object(tr, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ── search_runner 的字节上限（新能力）──
    def test_max_bytes_raises_too_large(self):
        import urllib.request
        with urllib.request.urlopen(self.srv.url("/big"), timeout=10) as resp:
            with self.assertRaises(sr.ResponseTooLarge):
                sr.read_with_deadline(resp, time.monotonic() + 10, max_bytes=1000)

    def test_max_bytes_not_triggered_when_under(self):
        import urllib.request
        with urllib.request.urlopen(self.srv.url("/ok"), timeout=10) as resp:
            self.assertEqual(
                sr.read_with_deadline(resp, time.monotonic() + 10, max_bytes=10_000),
                b'{"ok":true}')

    # ── transport 两个 urllib 通道：总截止 ──
    def test_text_channel_honours_total_deadline_on_slow_body(self):
        t0 = time.monotonic()
        with self.assertRaises(Exception) as ctx:
            tr.get_via_urllib(self.srv.url("/slow"), timeout=1)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0, f"总截止未执行：{elapsed:.2f}s（单次 socket 超时挡不住慢体）")
        self.assertIn("deadline", str(ctx.exception).lower())

    def test_binary_channel_honours_total_deadline_on_slow_body(self):
        t0 = time.monotonic()
        out = tr.get_bytes_via_urllib(self.srv.url("/slow"), timeout=1, max_bytes=10_000)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0, f"总截止未执行：{elapsed:.2f}s")
        self.assertFalse(out["ok"])
        self.assertIn(out["error_kind"], ("read_timeout", "read_error"), out)

    def test_binary_channel_reports_too_large_not_parse_error(self):
        out = tr.get_bytes_via_urllib(self.srv.url("/big"), timeout=5, max_bytes=100)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_kind"], "too_large", out)

    def test_binary_channel_records_status_for_non_2xx(self):
        out = tr.get_bytes_via_urllib(self.srv.url("/403"), timeout=5)
        self.assertEqual(out["status"], 403)
        self.assertEqual(out["error_kind"], "http_error")
        self.assertIn(b"forbidden", out["data"], "错误页正文要留下")

    def test_binary_channel_passes_through_200_error_body(self):
        """200 + `success:false` 是**内容层**失败：状态照记，正文照取，由调用方判。"""
        out = tr.get_bytes_via_urllib(self.srv.url("/err200"), timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], 200)
        self.assertIn(b"NO_PERMISSION", out["data"])


class TestNetPolicyResponseParsing(unittest.TestCase):
    """`net_policy._read_http_response`：成熟解析 + chunked 解块（用 socketpair 直测）。"""

    def _serve_raw(self, payload: bytes, *, drip: float = 0.0):
        srv, cli = socket.socketpair()
        self.addCleanup(srv.close)
        self.addCleanup(cli.close)

        def _feed():
            try:
                if drip:
                    for i in range(0, len(payload), 8):
                        time.sleep(drip)
                        srv.sendall(payload[i:i + 8])
                else:
                    srv.sendall(payload)
            except Exception:                        # noqa: BLE001
                pass
            finally:
                try:
                    srv.shutdown(socket.SHUT_WR)
                except Exception:                    # noqa: BLE001
                    pass

        threading.Thread(target=_feed, daemon=True).start()
        return cli

    def test_chunked_body_is_decoded_not_treated_as_content(self):
        """反例：旧实现手拆头之后把分块框架当正文 → PDF 里有 `1f\\r\\n` 这种垃圾。"""
        body = b"%PDF-1.7 hello chunked"
        framed = b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n" \
                 b"Transfer-Encoding: chunked\r\n\r\n"
        for i in range(0, len(body), 8):
            piece = body[i:i + 8]
            framed += b"%x\r\n%s\r\n" % (len(piece), piece)
        framed += b"0\r\n\r\n"
        cli = self._serve_raw(framed)
        out = net_policy._read_http_response(cli, url="http://x/y.PDF", egress="direct",
                                            cap=1_000_000, budget=5.0,
                                            started=time.time())
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["raw"], body, "chunked 必须被解开，正文逐字节一致")
        self.assertIn("chunked", out["transfer_encoding"])

    def test_gzip_body_is_decoded(self):
        body = b'{"ok":true}'
        payload = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                   b"Content-Encoding: gzip\r\nContent-Length: %d\r\n\r\n"
                   % len(gzip.compress(body))) + gzip.compress(body)
        out = net_policy._read_http_response(self._serve_raw(payload), url="http://x/y",
                                            egress="direct", cap=1_000_000, budget=5.0,
                                            started=time.time())
        self.assertEqual(out["raw"], body)

    def test_total_deadline_on_slow_chunked_body(self):
        framed = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for _ in range(40):
            framed += b"8\r\n" + b"z" * 8 + b"\r\n"
        framed += b"0\r\n\r\n"
        cli = self._serve_raw(framed, drip=0.02)
        t0 = time.monotonic()
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(cli, url="http://x/y", egress="direct",
                                           cap=1_000_000, budget=0.2,
                                           started=time.time())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5, f"总截止未执行：{elapsed:.2f}s")
        self.assertIn("超时", str(ctx.exception))

    def test_redirect_is_not_followed(self):
        payload = (b"HTTP/1.1 302 Found\r\nLocation: http://elsewhere/\r\n"
                   b"Content-Length: 0\r\n\r\n")
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve_raw(payload), url="http://x/y",
                                           egress="direct", cap=1000, budget=5.0,
                                           started=time.time())
        self.assertIn("重定向", str(ctx.exception))

    def test_cap_is_reported_as_too_large(self):
        body = b"z" * 5000
        payload = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        with self.assertRaises(net_policy.FetchError) as ctx:
            net_policy._read_http_response(self._serve_raw(payload), url="http://x/y",
                                           egress="direct", cap=1000, budget=5.0,
                                           started=time.time())
        self.assertIn("上限", str(ctx.exception))


class TestCapacityPolicyIsCentralised(unittest.TestCase):
    def test_upload_and_download_limits_come_from_one_place(self):
        """上传 3 MiB / 下载 30 MB 的**单一来源**（可配置、错误清晰）。"""
        import transfer_limits as tl
        self.assertEqual(tl.UPLOAD_MAX_BYTES, 3 * 1024 * 1024)
        self.assertEqual(tl.DOWNLOAD_MAX_BYTES, 30 * 1024 * 1024)
        self.assertTrue(tl.explain("upload"))
        # 调用方真的用了它（而不是各自硬编码）
        import material_intake as mi
        import annual_report_pdf as arp
        self.assertEqual(mi.MAX_BYTES, tl.UPLOAD_MAX_BYTES)
        self.assertEqual(arp.MAX_BYTES, tl.DOWNLOAD_MAX_BYTES)


if __name__ == "__main__":
    unittest.main(verbosity=1)
