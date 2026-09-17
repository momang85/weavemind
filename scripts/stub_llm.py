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
        # **不排检索步骤**：替身回的这个计划就是烟测任务的全部步骤，若含 `web_search`，
        # 烟测就会去抓真实搜索页（`adapters/text_search.py` → Bing）。实测在 CI 上 Bing
        # 偶尔返回空/被拦 → 触发 search-fallback 的 code_execution 步骤 → 该步骤失败并替换
        # 步骤 1 的结果 → 下游因依赖失败被阻塞 → 任务 FAILED → 闸门间歇变红（同一签名在改动
        # 前的 `0330b6f` 上就出现过）。部署烟测证明的是"装得上、起得来、跑得通、产出夹具内容"，
        # 不该由外部检索决定成败；检索链路的覆盖在 S1/S2 的单独证据里。
        {"step_id": "1", "capability": "content_summary",
         "instruction": "按目标直接组织要点（仅用模型知识与夹具内容，不检索外部资料）",
         "timeout": 120},
        {"step_id": "2", "capability": "report_generator",
         "instruction": "生成结构化报告（夹具内容，含数字与期间）", "timeout": 120},
        {"step_id": "3", "capability": "package",
         "instruction": "把交付物打包为 ZIP", "timeout": 120},
    ]
}

_PLAN_HINTS = ("拆解", "规划", "steps", "计划", "json")

# 规划请求的**特征**：只有规划器会这样要输出——系统提示说明"把目标拆成步骤/返回 JSON"，
# 或用户提示同时含"拆解/规划"与 JSON 要求。
#
# 早期判据只看"消息里出现 步/计划/json"就回计划 JSON，结果**报告步骤**的提示里
# 提到"按计划步骤写报告"也被误判成规划：交付物里出现的是计划 JSON 而不是报告正文。
# 实测（干净环境烟测）：任务 SUCCESS、验收还 pass，而落盘的"报告"开头是
# `# 报告` + `{"steps": [...]}`——内容判据正是靠夹具标记才把这种假绿拦住。
_PLAN_SYSTEM_RE = re.compile(
    r"(break .{0,20}goals? into|decompose .{0,20}(goal|task)|拆解.{0,10}(目标|任务)"
    r"|规划器|planner\b)", re.I)
_PLAN_JSON_RE = re.compile(r'(json|"steps"\s*:|步骤列表|steps)', re.I)


def _wants_plan(system: str, user: str) -> bool:
    """这次请求是不是"让模型给出计划"。"""
    if _PLAN_SYSTEM_RE.search(system or ""):
        return True
    return bool(re.search(r"(拆解|规划)", user or "")
                and _PLAN_JSON_RE.search(user or ""))


def _reply_text(payload: dict) -> str:
    """按请求内容决定回什么（规划 → JSON；其它 → 报告正文）。"""
    msgs = payload.get("messages") or []
    system = ""
    user = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if role == "system" and not system:
            system = content
        elif role != "system":
            user += "\n" + content
    if _wants_plan(system, user):
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
