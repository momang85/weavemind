# -*- coding: utf-8 -*-
"""织光 - 图像生成 Worker（AI PS 制图能力）。

调用 OpenAI gpt-image 系列模型，为报告/调研任务生成与主题搭配的
信息图插画，保存到任务工作区的 project 目录，由报告生成器嵌入报告。

环境变量：
    IMAGE_API_KEY / OPENAI_API_KEY  图像 API Key
    IMAGE_MODEL                     默认 gpt-image-2
    IMAGE_SIZE                      默认 1536x1024（横向，适配报告）
    IMAGE_QUALITY                   默认 low（快、省）
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging


class ImageGeneratorWorker(AsyncWorkerBase):
    _class_capabilities = ["image_generator"]
    _needs_task = True

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_generate, instruction, task or {},
        )

    def _sync_generate(self, instruction: str, task: dict) -> str:
        key = (
            os.environ.get("IMAGE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        if not key:
            return json.dumps(
                {"status": "failed", "error": "IMAGE_API_KEY / OPENAI_API_KEY 未配置"},
                ensure_ascii=False,
            )
        base_url = os.environ.get("IMAGE_API_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("IMAGE_MODEL", "gpt-image-2")
        size = os.environ.get("IMAGE_SIZE", "1536x1024")
        quality = os.environ.get("IMAGE_QUALITY", "low")
        if task.get("workspace"):
            out_dir = Path(str(task["workspace"])) / "project"
        else:
            out_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "project"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=base_url)
            resp = client.images.generate(
                model=model,
                prompt=str(instruction)[:2000],
                size=size,
                quality=quality,
                n=1,
            )
            path = out_dir / "report_illustration.png"
            item = resp.data[0]
            if item.b64_json:
                path.write_bytes(base64.b64decode(item.b64_json))
            elif item.url:
                import urllib.request
                with urllib.request.urlopen(item.url, timeout=60) as r:
                    path.write_bytes(r.read())
            else:
                return json.dumps(
                    {"status": "failed", "error": "Image API 未返回图片数据"},
                    ensure_ascii=False,
                )
            return json.dumps({
                "status": "success",
                "path": str(path),
                "filename": path.name,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {"status": "failed", "error": f"Image generation failed: {exc}"},
                ensure_ascii=False,
            )


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-image-generator")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = ImageGeneratorWorker(
        agent_id="image_generator",
        capabilities=ImageGeneratorWorker._class_capabilities,
        registry=registry,
        messaging=messaging,
        max_concurrency=2,
    )
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        import asyncio
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
