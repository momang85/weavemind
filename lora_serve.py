# -*- coding: utf-8 -*-
"""本地 LoRA 多实例推理管理器（为多 Worker 规模化铺路）。

设计（关键：12GB 显存约束下的多 LoRA 共享）：
- 7B base 模型 4-bit 只加载【一次】（~5.9GB 显存）；
- 多个 LoRA adapter（每个 ~20MB）全部通过 PeftModel.load_adapter 加载进 adapter 池；
- 每个 Worker 一个 HTTP 端口（8765/8766/...），请求按端口路由到对应 adapter
  （生成前 set_adapter 动态切换，adapter 切换开销极小）；
- 单实例兼容：无配置文件或仅 1 个 server 时行为与旧版一致。

配置：lora_servers.json
{
  "base_model": "models/...",
  "servers": {
    "content_summary": {"port": 8765, "lora_path": "models/...", "system_prompt": "...", "enabled": true},
    "ranking":        {"port": 8766, "lora_path": "models/...", ...}
  }
}

接口（每个端口相同）：
  POST /generate  {"instruction": "...", "max_tokens": 4096}
  → {"status": "success", "summary": "...", "charts": [...], "sources": [...], "server": "name"}
"""
import json
import os
import sys
import threading

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

CONFIG_FILE = os.environ.get("WM_LORA_CONFIG", "lora_servers.json")

# 兼容旧环境变量（单实例模式）
_DEFAULT_MODEL = os.environ.get("WM_LOCAL_MODEL", "models/Qwen2.5-7B-Instruct")
_DEFAULT_LORA = os.environ.get("WM_LORA_PATH", "models/lora_content_summary")
_DEFAULT_PORT = int(os.environ.get("WM_LOCAL_PORT", "8765"))

_model = None
_tokenizer = None
_adapter_names: dict[int, str] = {}   # port -> adapter name
_server_names: dict[int, str] = {}    # port -> server name
_server_prompts: dict[int, str] = {}  # port -> system prompt
_gpu_lock = threading.Lock()


def _load_config() -> dict:
    """读取多实例配置；失败返回空 dict（退回单实例模式）。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and isinstance(cfg.get("servers"), dict):
            return cfg
    except Exception:
        pass
    return {}


def _config_servers(cfg: dict) -> list[dict]:
    """规范化 server 列表：多实例配置 > 单实例环境变量。"""
    servers = []
    for name, s in (cfg.get("servers") or {}).items():
        if not isinstance(s, dict) or not s.get("enabled", True):
            continue
        servers.append({
            "name": str(name),
            "port": int(s.get("port") or 0),
            "lora_path": str(s.get("lora_path") or ""),
            "system_prompt": str(s.get("system_prompt") or ""),
        })
    if not servers:
        servers.append({
            "name": "default",
            "port": _DEFAULT_PORT,
            "lora_path": _DEFAULT_LORA,
            "system_prompt": "",
        })
    return servers


def load():
    """加载 base 一次 + 全部 adapter（adapter 池按端口索引）。"""
    global _model, _tokenizer, _adapter_names, _server_names, _server_prompts
    if _model is not None:
        return
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    cfg = _load_config()
    servers = _config_servers(cfg)
    base_path = str(cfg.get("base_model") or _DEFAULT_MODEL)

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    _tokenizer = AutoTokenizer.from_pretrained(base_path, trust_remote_code=True)
    if _tokenizer.pad_token is None:
        _tokenizer.pad_token = _tokenizer.eos_token
    base = AutoModelForCausalLM.from_pretrained(
        base_path, quantization_config=bnb, device_map="auto",
        trust_remote_code=True,
    )

    first = True
    for s in servers:
        try:
            if first:
                _model = PeftModel.from_pretrained(base, s["lora_path"], adapter_name=s["name"])
                first = False
            else:
                _model.load_adapter(s["lora_path"], adapter_name=s["name"])
            _adapter_names[s["port"]] = s["name"]
            _server_names[s["port"]] = s["name"]
            _server_prompts[s["port"]] = s["system_prompt"]
            print(f"  adapter 已加载: {s['name']} <- {s['lora_path']} (port {s['port']})", flush=True)
        except Exception as exc:
            print(f"  !! adapter 加载失败 {s['name']}: {str(exc)[:120]}", flush=True)
    if first:
        raise RuntimeError("没有可用的 LoRA adapter")
    _model.eval()
    print(f"base 模型加载完成: {base_path} | adapters: {sorted(_adapter_names.values())}", flush=True)


def generate(instruction: str, max_tokens: int = 4096, port: int = 0) -> dict:
    """按端口选择 adapter 生成；失败抛异常由调用方回退。"""
    load()
    with _gpu_lock:
        import torch
        name = _adapter_names.get(port) or _adapter_names.get(_DEFAULT_PORT) or next(iter(_adapter_names.values()))
        _model.set_adapter(name)
        prompt = _server_prompts.get(port) or (
            "你是织光 WeaveMind 的内容总结 Worker。根据指令生成 Markdown 总结与图表数据。"
        )
        msgs = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": str(instruction)},
        ]
        text = _tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = _tokenizer(text, return_tensors="pt").to(_model.device)
        # 重试机制：代码跑偏输出（R/Python 代码）自动重试一次
        for attempt in range(2):
            with torch.no_grad():
                out = _model.generate(
                    **inputs, max_new_tokens=max_tokens,
                    do_sample=True, temperature=0.2, top_p=0.85,
                    pad_token_id=_tokenizer.pad_token_id,
                )
            raw = _tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            parsed = _parse_output(raw)
            head = str(raw or "")[:200].strip()
            if not (
                head.startswith("library(") or head.startswith("import ")
                or head.startswith("def ") or head.startswith("```r")
                or head.startswith("```python") or head.startswith("# ")
            ):
                return parsed
            if attempt == 0:
                print(f"检测到代码跑偏输出，重试 (len={len(raw)})", flush=True)
        return parsed


def _parse_output(raw: str) -> dict:
    """解析模型输出：优先 {summary, charts} JSON；否则 summary + [CHART_DATA]/[SOURCES] 块。"""
    raw = str(raw or "").strip()
    t = raw
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) >= 2 else t
        t = t.strip().lstrip("json").strip()
    i = t.find("{")
    if i >= 0:
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(t[i:j + 1])
                        if isinstance(data, dict):
                            s = data.get("summary")
                            c = data.get("charts")
                            if isinstance(s, str) and s.strip():
                                return {
                                    "summary": s.strip(),
                                    "charts": c if isinstance(c, list) else [],
                                    "raw": raw[:200],
                                }
                    except Exception:
                        pass
                    break
    summary = raw
    charts: list = []
    sources: list = []
    marker = "[CHART_DATA]"
    if marker in raw:
        summary, _, rest = raw.partition(marker)
        charts_part, _, sources_part = rest.partition("[SOURCES]")
        try:
            charts = json.loads(charts_part.strip())
            if not isinstance(charts, list):
                charts = []
        except Exception:
            charts = []
        if sources_part:
            try:
                sources = json.loads(sources_part.strip())
                if not isinstance(sources, list):
                    sources = []
            except Exception:
                sources = []
    else:
        smarker = "[SOURCES]"
        if smarker in raw:
            summary, _, sources_part = raw.partition(smarker)
            try:
                sources = json.loads(sources_part.strip())
                if not isinstance(sources, list):
                    sources = []
            except Exception:
                sources = []
    return {"summary": summary.strip(), "charts": charts, "sources": sources, "raw": raw[:200]}


def main():
    load()
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cfg = _load_config()
    servers = _config_servers(cfg)
    ports = sorted({s["port"] for s in servers if s["port"] > 0})

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                body = {}
            # 请求所属端口 = 本 server 的端口（每端口一个实例，按端口选 adapter）
            port = self.server.server_address[1]
            try:
                result = generate(body.get("instruction", ""), int(body.get("max_tokens") or 4096), port=port)
                result["server"] = _server_names.get(port, "default")
                payload = {"status": "success", **result}
            except Exception as exc:
                payload = {"status": "failed", "error": str(exc)[:300]}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    servers_list = []
    for port in ports:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
            servers_list.append(httpd)
            print(f"  LoRA 服务 {_server_names.get(port, '?')}: http://127.0.0.1:{port}", flush=True)
        except Exception as exc:
            print(f"  端口 {port} 启动失败: {str(exc)[:100]}", flush=True)
    if not servers_list:
        print("无可用端口，退出", flush=True)
        sys.exit(1)

    # 每个端口一个线程 serve（共享模型与 GPU 锁）
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers_list]
    for t in threads:
        t.start()
    print(f"多实例 LoRA 管理器就绪: {len(servers_list)} 个服务", flush=True)
    try:
        while True:
            import time
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
