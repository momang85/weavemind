#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 stub 模型端点：OpenAI 兼容的 `/v1/chat/completions`（零依赖，仅标准库）。

用途：干净环境端到端门禁要跑"起服务→提交任务→出报告"，但**不允许**为此烧真实
模型额度。这个 stub 按请求内容返回确定性响应：

- 请求像"规划/拆解/JSON" → 返回一个可执行的最小步骤计划（JSON）；
- 其它请求 → 返回一段带来源清单与免责声明的报告正文。

只监听 127.0.0.1，且**不是**被测系统的一部分：它只是测试替身。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPORT_BODY = """# 测试目标研究报告（stub 模型产出）

## 核心指标

| 指标 | 数值 | 口径/年份 | 来源 |
| --- | --- | --- | --- |
| 营业收入 | 1200亿元 | 2025 年度 | [1] |
| 净利润 | 240亿元 | 2025 年度 | [1] |

## 数据时效

数据截至 2025-12-31 年度报告披露日，来源为公开渠道示例数据，日终更新。

## 参考来源

1. [示例来源（stub）](https://example.com/stub-source)

## 免责声明

本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；
数据来源于公开渠道，可能存在延迟或误差；据此操作风险自担。
"""

# 固定模型替身产出里的**夹具标记**：门禁靠它在交付物里认出"这确实是替身那份报告"，
# 而不是"文本非空就算数"——失败说明（如"LLM 端点不可用"）或取消说明同样是"非空"，
# 早期判据会把它们当报告通过（假绿）。
FIXTURE_MARK = "STUB-FIXTURE-REPORT-1200"

# 已在夹具里固定的两个数字与来源，供门禁断言"内容符合夹具预期"。
FIXTURE_FACTS = ("1200亿元", "240亿元", "2025 年度")

REPORT_BODY = REPORT_BODY.replace("# 测试目标研究报告（stub 模型产出）",
                                  f"# 测试目标研究报告（stub 模型产出）\n\n{FIXTURE_MARK}")

# 负向用例用的失败模式：让模型端点直接报错，任务必须如实终结为 FAILED。
STUB_MODE_OK = "ok"
STUB_MODE_FAIL = "fail"

PLAN = {
    "steps": [
        {"step_id": "1", "capability": "web_search",
         "instruction": "检索目标主题的公开资料并记录来源链接", "timeout": 120},
        {"step_id": "2", "capability": "report_generator",
         "instruction": "基于检索结果生成结构化报告并标注来源", "timeout": 120},
        {"step_id": "3", "capability": "package",
         "instruction": "把交付物打包为 ZIP", "timeout": 120},
    ]
}

_PLAN_HINTS = ("拆解", "规划", "steps", "计划", "json")


def _reply_text(payload: dict) -> str:
    """按请求内容决定回什么（规划 → JSON；其它 → 报告正文）。"""
    blob = json.dumps(payload.get("messages") or [], ensure_ascii=False).lower()
    wants_json = bool(payload.get("response_format")) or any(
        h.lower() in blob for h in _PLAN_HINTS)
    if wants_json and re.search(r"步|step|计划|plan", blob):
        return json.dumps(PLAN, ensure_ascii=False)
    return REPORT_BODY


def _stub_mode() -> str:
    """替身工作模式：`ok` 正常回包；`fail` 让端点报错（负向用例专用）。"""
    import os
    mode = str(os.environ.get("WM_STUB_MODE") or STUB_MODE_OK).strip().lower()
    return STUB_MODE_FAIL if mode == STUB_MODE_FAIL else STUB_MODE_OK


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 静默：避免污染门禁日志
        return

    def _json(self, obj: dict, code: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                          # 健康探测
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"data": [{"id": "stub-model"}]})
        return self._json({"status": "ok"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if self.path.rstrip("/").endswith("/embeddings"):
            # embedding 也走 stub：返回确定性的短向量，避免欠费导致记忆写入失败
            n = len(payload.get("input") or [""])
            return self._json({
                "data": [{"embedding": [0.01] * 8, "index": i} for i in range(max(1, n))],
                "model": "stub-embed", "usage": {"total_tokens": 1},
            })
        if _stub_mode() == STUB_MODE_FAIL:
            # 负向用例：端点如实报错，任务必须终结为 FAILED，且不得产出"报告"
            return self._json({
                "error": {"message": "stub 故障注入：模型端点不可用",
                          "type": "stub_injected_failure"},
            }, 500)
        text = _reply_text(payload)
        return self._json({
            "id": "stub-1",
            "object": "chat.completion",
            "model": str(payload.get("model") or "stub-model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 32, "completion_tokens": 64,
                      "total_tokens": 96},
        })


def main() -> int:
    ap = argparse.ArgumentParser(description="stub 模型端点（测试替身）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"stub-llm listening on http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
