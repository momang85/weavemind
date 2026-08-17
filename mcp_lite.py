# -*- coding: utf-8 -*-
"""MCP-lite：自研 stdio JSON-RPC 服务器（对标标准 3.1 MCP）。

实现 initialize / tools/list / tools/call 三个核心方法；
工具执行复用 tool_dispatch（经 Redis 派发给现有 worker），
工具定义来自 tool_contracts.TOOL_REGISTRY（谁提供工具谁定义工具）。

用法：
  python mcp_lite.py --stdio     # 作为 MCP Server（stdin/stdout JSON-RPC）
  python mcp_lite.py --list      # 打印本地工具目录（调试）
"""

import argparse
import json
import sys

from tool_contracts import TOOL_REGISTRY
from tool_dispatch import dispatch_tool


def _input_schema(tool: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": tool.get("parameters", {}).get("instruction", ""),
            },
            "timeout": {"type": "integer", "description": "调用超时（秒）"},
            "task_id": {"type": "string", "description": "关联任务 ID（审计用）"},
        },
        "required": ["instruction"],
    }


class MCPServer:
    """MCP-lite 服务器：单条 JSON-RPC 消息处理。"""

    def handle(self, msg) -> dict:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method = msg.get("method")
        params = msg.get("params") or {}
        mid = msg.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "weavemind-mcp-lite", "version": "0.1"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "tools": [
                        {"name": t["name"], "description": t["description"],
                         "inputSchema": _input_schema(t)}
                        for t in TOOL_REGISTRY
                    ]
                },
            }
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            if not name:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "name required"}}
            if not args.get("instruction"):
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "arguments.instruction required"}}
            try:
                result = dispatch_tool(
                    name,
                    str(args["instruction"]),
                    task_id=str(args.get("task_id") or ""),
                    timeout=int(args.get("timeout") or 300),
                )
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(result.get("result", result), ensure_ascii=False),
                        }],
                        "isError": result.get("status") != "SUCCESS",
                    },
                }
            except Exception as exc:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)[:300]}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method: {method}"}}


def serve_stdio() -> None:
    """逐行读取 stdin 的 JSON-RPC 消息，逐条应答到 stdout。"""
    # Windows 控制台默认 GBK 编码，无法编码部分中文/上标字符（如 ²），
    # 会导致 tools/list 等含非 ASCII 的响应崩溃。强制 UTF-8 保证跨平台一致。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    server = MCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = server.handle(msg)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for t in TOOL_REGISTRY:
            print(f"- {t['name']}: {t['description']}")
        return 0
    serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
