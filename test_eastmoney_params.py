# -*- coding: utf-8 -*-
"""东财结构化接口：**参数契约**与失败自证（离线，用真实记录下来的响应体）。

根因（2026-09-29 同环境一次有界请求实测）：

- `filter=(SECURITY_CODE="002304.SZ")` → `{"version":null,"result":null,
  "success":false,"message":"参数错误为空","code":9201}`（**89 字节、0 行**）；
- `filter=(SECURITY_CODE="002304")` → 正常 `result.pages=1`，`data[0].SECUCODE="002304.SZ"`。

也就是说：接口一直是好的，`SECURITY_CODE` 字段存的是**裸代码**，带后缀就被判参数错。
路由层本来会先 `facts.bare_code()`，但直接调用适配器（探针、材料核验、脚本）就会踩到，
于是反复被读成"站点/出口限制"。修法：**去后缀放进适配器内部**，并把接口自己的
`success/code/message` 带进异常，让"参数错"与"真没数据"在日志里长得不一样。
"""
from __future__ import annotations

import json
import unittest
import urllib.parse as up

from adapters import eastmoney as em

# 真实记录的响应体（脱敏：只保留字段与状态词，不含任何凭据/查询正文）
_REAL_9201 = ('{"version":null,"result":null,"success":false,'
              '"message":"参数错误为空","code":9201}').encode("utf-8")
_REAL_OK = ('{"version":"x","result":{"pages":1,"count":2,"data":[{"SECUCODE":"002304.SZ",'
            '"SECURITY_CODE":"002304","SECURITY_NAME_ABBR":"YH"}]},"success":true,'
            '"message":"ok","code":0}').encode("utf-8")


class TestSecurityCodeIsBare(unittest.TestCase):
    def test_both_url_builders_strip_the_exchange_suffix(self):
        for build in (em._api_url, em._api_url_ashare):
            url = up.unquote(build("600031.SH"))
            self.assertIn('SECURITY_CODE="600031"', url, url)
            self.assertNotIn('SECURITY_CODE="600031.SH"', url)

    def test_bare_code_passes_through_unchanged(self):
        self.assertIn('SECURITY_CODE="002304"', up.unquote(em._api_url_ashare("002304")))

    def test_hk_code_also_stripped(self):
        self.assertIn('SECURITY_CODE="00700"', up.unquote(em._api_url("00700.HK")))

    def test_bare_helper_handles_odd_inputs(self):
        self.assertEqual(em._bare(""), "")
        self.assertEqual(em._bare("600031.SH"), "600031")
        self.assertEqual(em._bare("600031"), "600031")


class TestEmptyResultExplainsItself(unittest.TestCase):
    def test_api_error_is_quoted_not_hidden(self):
        """用户可见的报错必须带 `success/code/message`，否则排障只能靠手工探针。"""
        msg = em._explain_empty(json.loads(_REAL_9201), "002304")
        self.assertIn("9201", msg)
        self.assertIn("success=False", msg)
        self.assertIn("参数错误为空", msg)

    def test_true_empty_is_worded_differently(self):
        msg = em._explain_empty({"success": True, "result": {"data": []}}, "002304")
        self.assertIn("0 行", msg)
        self.assertNotIn("9201", msg)

    def test_recorded_ok_body_is_not_treated_as_error(self):
        data = json.loads(_REAL_OK)
        self.assertTrue(data["result"]["data"], "样本本身应有数据")
        self.assertEqual(em._explain_empty(data, "002304"),
                         "EastMoney 返回 0 行（success=True，代码 002304）")


class TestRealFetchStillUsesTheBareFilter(unittest.TestCase):
    """不发请求：把 `_get` 换成替身，确认真正发出去的 URL 是裸代码过滤。"""

    def test_fetch_ashare_sends_bare_filter(self):
        import unittest.mock as mock
        seen = {}

        def _fake_get(url, timeout=25):
            seen["url"] = url
            return _REAL_OK.decode("utf-8")

        with mock.patch.object(em, "_get", _fake_get):
            em.fetch_ashare("洋河股份", "002304.SZ", year_range=(2020, 2030))
        self.assertIn('SECURITY_CODE="002304"', up.unquote(seen["url"]))
        self.assertNotIn("002304.SZ", up.unquote(seen["url"]))

    def test_empty_response_surfaces_the_api_code(self):
        import unittest.mock as mock
        with mock.patch.object(em, "_get", lambda url, timeout=25: _REAL_9201.decode("utf-8")):
            with self.assertRaises(RuntimeError) as ctx:
                em.fetch_ashare("洋河股份", "002304.SZ")
        self.assertIn("9201", str(ctx.exception))
        self.assertIn("002304", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
