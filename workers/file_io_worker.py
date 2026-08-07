"""织光 (ZhiGuang) - File I/O Worker（受限工作区内的文件读写，支持中文指令与多文件写入）。"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)

WRITE_MARKERS = (
    "write", "save", "create",
    "保存", "创建", "写入", "写为", "写出", "另存为", "保存为",
    "生成文件", "编写", "新建", "放置",
)
READ_MARKERS = ("read", "读取", "读入", "查看", "列出", "打印")
FILENAME_PREFIXES = ("保存为", "保存到", "另存为", "保存", "创建", "生成", "写入", "文件：", "文件")


def _loads_json_loose(text) -> dict:
    """宽容解析 LLM 返回的 JSON（容忍代码围栏与前后说明文字）。"""
    if isinstance(text, dict):
        return text
    s = str(text).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s).rstrip("`").strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _sanitize_filename(name: str) -> str:
    """去掉 LLM 常给文件名附加的动词前缀（如"保存为angry_birds.html"）。"""
    name = name.strip().strip('"').strip("'").strip("`")
    for p in FILENAME_PREFIXES:
        if name.startswith(p):
            name = name[len(p):].lstrip(" ：:，,、")
            break
    return name.strip()


class FileIoWorker(AsyncWorkerBase):
    """在受限工作区内执行文件读写操作（中文/英文指令均可）。"""

    _class_capabilities = ["file_io"]
    WORKSPACE_DIR = os.path.join(tempfile.gettempdir(), "agent_workspace", "project")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        os.makedirs(self.WORKSPACE_DIR, exist_ok=True)

    def _safe_path(self, filename: str) -> Path:
        """将文件名限定在工作区内，防止路径穿越。"""
        base = Path(self.WORKSPACE_DIR).resolve()
        # 统一把反斜杠当路径分隔符：Windows 原生支持；Linux/macOS 上
        # 反斜杠是合法文件名字符，不归一化会导致 "..\\.." 绕过逃逸检测。
        normalized = str(filename).replace("\\", "/")
        path = (base / normalized).resolve()
        if not str(path).startswith(str(base)):
            raise ValueError(f"Path escapes workspace: {filename}")
        return path

    async def execute(self, instruction: str) -> str:
        try:
            lower = instruction.lower()
            if "read" in lower and "log" in lower:
                return await self._handle_read_logs(instruction)
            if any(k in instruction for k in WRITE_MARKERS):
                return await self._handle_write_file(instruction)
            if any(k in instruction for k in READ_MARKERS):
                return await self._handle_read_file(instruction)
            return await self._handle_unknown_instruction(instruction)
        except Exception as exc:
            logger.error("File operation error: %s", exc)
            raise RuntimeError(f"File operation error: {exc}") from exc

    async def _handle_read_logs(self, instruction: str) -> str:
        return "Log reading is not supported in the file workspace; check logs/ directory instead."

    async def _handle_read_file(self, instruction: str) -> str:
        resp = await self._call_llm(
            instruction=(
                f"从指令中提取要读取的文件名（不含路径）。指令：'{instruction}'。只返回文件名。"
            )
        )
        filename = resp.strip().strip('"').strip("'")
        if not filename or any(c in filename for c in "\\/:*?\""):
            m = re.search(r"([\w\-.]+\.\w{1,8})", instruction)
            if m:
                filename = m.group(1)
        if not filename:
            return json.dumps({"status": "failed", "error": "无法从指令中提取文件名"}, ensure_ascii=False)
        try:
            path = self._safe_path(filename)
        except ValueError as exc:
            return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)
        if not path.exists() or not path.is_file():
            return json.dumps({"status": "failed", "error": f"File not found: {filename}"}, ensure_ascii=False)
        content = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({
            "status": "success", "filename": filename, "path": str(path),
            "content": content[:20000], "chars": len(content),
        }, ensure_ascii=False)

    async def _handle_write_file(self, instruction: str) -> str:
        resp = await self._call_llm(
            instruction=(
                "从指令中提取要写入的所有【文件名】及【完整内容】。"
                "若指令引用了[上一步结果 x]或类似内容，则把被引用的代码/文本作为对应文件的内容。"
                f"指令：'{instruction}'\n"
                '只输出严格JSON：{"files": [{"filename": "路径/文件名", "content": "完整内容"}]}'
            )
        )
        data = _loads_json_loose(resp)
        files = data.get("files")
        if not isinstance(files, list) or not files:
            if data.get("filename") and data.get("content") is not None:
                files = [{"filename": data["filename"], "content": data["content"]}]
        if not files:
            # 兜底：从指令中提取文件名，内容取 [上一步结果] 之后的部分
            m = re.search(r"([\w\-.]+\.\w{1,8})", instruction)
            m2 = re.search(r"\[上一步结果\s*\S*\]:\s*(.+)", instruction, re.S)
            if m and m2:
                files = [{"filename": m.group(1), "content": m2.group(1).strip()}]
        if not files:
            return json.dumps({"status": "failed", "error": "无法从指令中提取文件名与内容"}, ensure_ascii=False)

        written = []
        for f in files:
            filename = _sanitize_filename(str(f.get("filename") or ""))
            content = f.get("content")
            if not filename or content is None:
                continue
            try:
                path = self._safe_path(filename)
            except ValueError as exc:
                return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
            written.append({"filename": filename, "path": str(path), "chars": len(str(content))})
        if not written:
            return json.dumps({"status": "failed", "error": "没有可写的文件"}, ensure_ascii=False)
        return json.dumps({"status": "success", "files": written}, ensure_ascii=False)

    async def _handle_unknown_instruction(self, instruction: str) -> str:
        return await self._call_llm(
            instruction=(
                f"说明文件操作仅限在 {self.WORKSPACE_DIR} 内读写，并请用户给出明确的保存/读取指令。"
                f"指令：'{instruction}'。简要回复。"
            )
        )


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-file-io")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = FileIoWorker(
        agent_id="fileioworker",
        capabilities=FileIoWorker._class_capabilities,
        registry=registry,
        messaging=messaging,
        max_concurrency=5,
    )
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
