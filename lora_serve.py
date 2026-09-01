# -*- coding: utf-8 -*-
"""本地 LoRA 推理服务（替换 content_summary 的 LLM 调用）。

设计：
- 训练完成后把 lora_out 放到 models/lora_content_summary；
- 本服务在独立进程加载 Qwen2.5-7B-Instruct + LoRA（4-bit），
  通过本地 HTTP JSON 接口提供 content_summary 专用生成；
- content_summary_worker 优先走本地（快、零 API 成本），失败回退云端 API。

接口：
  POST /generate  {"instruction": "...", "max_tokens": 4096}
  → {"summary": "...", "charts": [...]}
"""
import json
import os
import sys
import threading

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_PATH = os.environ.get("WM_LOCAL_MODEL", "models/Qwen2.5-7B-Instruct")
LORA_PATH = os.environ.get("WM_LORA_PATH", "models/lora_content_summary")
PORT = int(os.environ.get("WM_LOCAL_PORT", "8765"))

_model = None
_tokenizer = None
_lock = threading.Lock()


def load():
    global _model, _tokenizer
    if _model is not None:
        return
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
    if _tokenizer.pad_token is None:
        _tokenizer.pad_token = _tokenizer.eos_token
    base = AutoModelForCausalLM.from_pretrained(
        MODEL_PATH, quantization_config=bnb, device_map="auto",
        trust_remote_code=True,
    )
    _model = PeftModel.from_pretrained(base, LORA_PATH)
    _model.eval()
    print(f"LoRA 模型加载完成: {LORA_PATH}", flush=True)


def generate(instruction: str, max_tokens: int = 4096) -> dict:
    """单次生成：返回 {summary, charts, raw}；失败抛异常由调用方回退。"""
    load()
    with _lock:
        import torch
        msgs = [
            {"role": "system", "content": "你是织光 WeaveMind 的内容总结 Worker。"
                                          "根据指令生成 Markdown 总结与图表数据。"},
            {"role": "user", "content": str(instruction)},
        ]
        text = _tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = _tokenizer(text, return_tensors="pt").to(_model.device)
        with torch.no_grad():
            out = _model.generate(
                **inputs, max_new_tokens=max_tokens,
                do_sample=True, temperature=0.3, top_p=0.9,
                pad_token_id=_tokenizer.pad_token_id,
            )
        raw = _tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return _parse_output(raw)


def _parse_output(raw: str) -> dict:
    """解析模型输出：summary + [CHART_DATA]...JSON。"""
    raw = str(raw or "").strip()
    summary = raw
    charts: list = []
    marker = "[CHART_DATA]"
    if marker in raw:
        summary, _, charts_part = raw.partition(marker)
        try:
            charts = json.loads(charts_part.strip())
            if not isinstance(charts, list):
                charts = []
        except Exception:
            charts = []
    return {"summary": summary.strip(), "charts": charts, "raw": raw[:200]}


def main():
    load()
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            try:
                result = generate(body.get("instruction", ""), int(body.get("max_tokens") or 4096))
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

    print(f"LoRA 推理服务: http://127.0.0.1:{PORT}", flush=True)
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
