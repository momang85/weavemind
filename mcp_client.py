# -*- coding: utf-8 -*-
"""MCP Client（对标标准 3.1 MCP：谁提供工具谁定义工具，即插即用）。

- stdio 传输：spawn 外部 MCP Server 子进程，按行 JSON-RPC 通信（initialize/tools/list/tools/call）；
- HTTP 传输：Streamable HTTP（POST JSON-RPC）；
- 配置：config.json 的 `mcp_servers` 段声明 [{"name":..., "command":..., "args":[...]} 或 {"name":..., "url":...}]；
- 发现的第三方工具注册到 EXTERNAL_TOOLS，tool_dispatch 优先路由到 MCP 执行。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

EXTERNAL_TOOLS: dict[str, dict] = {}
_LOCK = threading.Lock()
_MCP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class MCPClient:
    """单条 MCP 连接（stdio 或 HTTP）。HTTP 型支持鉴权头（付费数据源/带 token 的网关）。"""

    def __init__(self, name: str, command=None, args=None, url=None, timeout: float = 30.0,
                 headers=None, token: str = "", token_header: str = "Authorization",
                 token_prefix: str = "Bearer "):
        self.name = name
        self.url = url
        self.timeout = timeout
        self._proc = None
        self._next_id = 1
        self._tools: list[dict] = []
        # HTTP 型鉴权：headers 是配置里的非密钥头；token 来自密钥仓
        # （mcp_servers.<name>.api_key），默认按 Authorization: Bearer 发送；
        # `auth=header` 的服务可用 token_header/token_prefix 改成 X-API-Key 之类。
        self.headers: dict = dict(headers or {})
        if token:
            hdr = str(token_header or "Authorization")
            self.headers.setdefault(hdr, f"{token_prefix}{token}")
        if url:
            return
        if not command:
            raise ValueError("stdio MCP 需要 command")
        self._proc = subprocess.Popen(
            [command] + (args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        )

    def _request_stdio(self, method: str, params=None) -> dict:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("MCP server 未启动")
        rid = self._next_id
        self._next_id += 1
        self._proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params or {},
        }, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server 无响应")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == rid:
                return msg
        raise RuntimeError("MCP request timeout")

    def _request_http(self, method: str, params=None) -> dict:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     **self.headers},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        # 兼容 SSE 行与纯 JSON
        for line in text.splitlines():
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                return json.loads(line)
        return json.loads(text)

    def _request(self, method: str, params=None) -> dict:
        return self._request_http(method, params) if self.url else self._request_stdio(method, params)

    def initialize(self) -> dict:
        resp = self._request("initialize", {"protocolVersion": "2024-11-05"})
        return resp.get("result", {})

    def list_tools(self) -> list[dict]:
        resp = self._request("tools/list")
        self._tools = resp.get("result", {}).get("tools", [])
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self._request("tools/call", {"name": name, "arguments": arguments})
        result = resp.get("result", {})
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return {
            "status": "FAILED" if result.get("isError") else "SUCCESS",
            "result": text or json.dumps(result, ensure_ascii=False),
            "isError": bool(result.get("isError")),
        }

    def close(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass


def _server_auth(server: dict, name: str) -> tuple[str, str, str]:
    """MCP HTTP 服务的鉴权：返回 (token, header 名, 前缀)。

    密钥来源：环境变量 `WEAVEMIND_MCP_<NAME>_KEY` 优先，其次本机密钥仓
    `mcp_servers.<name>.api_key`（设置页写入，**不进 config.json**）。
    `auth=header` 的服务把 token 放进自定义头（无 Bearer 前缀），例如
    Wind/iFinD 中转常见的 `X-API-Key`。
    """
    import re as _re
    env_name = "WEAVEMIND_MCP_" + _re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_KEY"
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        try:
            import credentials
            token = credentials.get_secret(f"mcp_servers.{name}.api_key")
        except Exception:
            token = ""
    header = str(server.get("auth_header") or "Authorization")
    mode = str(server.get("auth") or "bearer").lower()
    prefix = "" if mode == "header" else "Bearer "
    return token, header, prefix


def load_mcp_servers(config: dict | None = None) -> list[MCPClient]:
    """从 config.json 的 mcp_servers 段创建 MCP 连接。"""
    if config is None:
        try:
            with open(_MCP_PATH, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            return []
    servers = config.get("mcp_servers") or []
    clients = []
    for s in servers:
        try:
            name = str(s.get("name") or "mcp")
            token, token_header, token_prefix = _server_auth(s, name)
            clients.append(MCPClient(
                name=name,
                command=s.get("command"),
                args=list(s.get("args") or []),
                url=s.get("url"),
                timeout=float(s.get("timeout") or 30),
                headers=dict(s.get("headers") or {}) if isinstance(s.get("headers"), dict) else None,
                token=token, token_header=token_header, token_prefix=token_prefix,
            ))
        except Exception:
            continue
    return clients


def discover_external_tools(config: dict | None = None) -> list[dict]:
    """连接配置的 MCP Server 并注册第三方工具到 EXTERNAL_TOOLS。"""
    global EXTERNAL_TOOLS
    found: list[dict] = []
    for client in load_mcp_servers(config):
        try:
            client.initialize()
            tools = client.list_tools()
            with _LOCK:
                for t in tools:
                    name = str(t.get("name") or "")
                    if not name:
                        continue
                    EXTERNAL_TOOLS[name] = {"client": client, "tool": t}
                    found.append({"name": name, "description": t.get("description", ""),
                                  "server": client.name})
        except Exception:
            client.close()
    return found


def call_external_tool(name: str, arguments: dict) -> dict | None:
    """按名称调用已注册的 MCP 第三方工具；未注册返回 None。"""
    with _LOCK:
        entry = EXTERNAL_TOOLS.get(name)
    if not entry:
        return None
    try:
        return entry["client"].call_tool(name, arguments)
    except Exception as exc:
        return {"status": "FAILED", "result": f"MCP 调用失败: {exc}"[:300]}


if __name__ == "__main__":
    # 调试：python mcp_client.py --discover
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        found = discover_external_tools()
        print(f"发现 {len(found)} 个 MCP 第三方工具：")
        for t in found:
            print(f"  - {t['name']}（{t['server']}）: {t['description'][:80]}")
    else:
        print("用法：python mcp_client.py --discover")
