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
import threading
import time
import unittest

from adapters import search_runner as sr


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status: int, body: bytes, *, ctype="application/json",
              chunked=False, gzip_body=False):
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
            self.wfile.write(body)
            self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/big"):
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


class TestCapacityPolicyIsCentralised(unittest.TestCase):
    def test_limits_come_from_one_place(self):
        import transfer_limits as tl
        import material_intake as mi
        import annual_report_pdf as arp
        self.assertEqual(tl.UPLOAD_MAX_BYTES, 3 * 1024 * 1024)
        self.assertEqual(tl.DOWNLOAD_MAX_BYTES, 30 * 1024 * 1024)
        self.assertEqual(mi.MAX_BYTES, tl.UPLOAD_MAX_BYTES, "上传上限必须来自单一来源")
        self.assertEqual(arp.MAX_BYTES, tl.DOWNLOAD_MAX_BYTES, "下载上限必须来自单一来源")
        self.assertEqual(mi.MAX_PAGES, tl.PDF_MAX_PAGES)

    def test_explain_is_specific(self):
        import transfer_limits as tl
        msg = tl.explain("upload")
        self.assertIn("3 MiB", msg)
        self.assertIn("材料上传", msg)
        self.assertTrue(tl.within("upload", 1024))
        self.assertFalse(tl.within("upload", tl.UPLOAD_MAX_BYTES + 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
