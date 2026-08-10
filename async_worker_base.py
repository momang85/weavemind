"""织光 (ZhiGuang) -- Async Worker Base. True async concurrency via httpx."""

import asyncio, json, logging, os, time
from abc import ABC, abstractmethod
import redis.asyncio as aioredis
import redis.exceptions

logger = logging.getLogger(__name__)

class AsyncMessaging:
    _MAX_RETRIES = 3; _RETRY_BASE = 0.5
    def __init__(self, redis_host: str, redis_port: int):
        self._host = redis_host; self._port = redis_port; self._redis = None
    async def connect(self):
        for i in range(1, self._MAX_RETRIES+1):
            try:
                self._redis = aioredis.Redis(host=self._host, port=self._port, decode_responses=True, socket_connect_timeout=5, socket_keepalive=True)
                await self._redis.ping(); return
            except (redis.exceptions.ConnectionError, OSError):
                if i < self._MAX_RETRIES: await asyncio.sleep(self._RETRY_BASE*i)
        raise redis.exceptions.ConnectionError("Failed to connect")
    async def close(self):
        if self._redis: await self._redis.close()
    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None: raise RuntimeError("Not connected")
        return self._redis
    async def pop_task(self, agent_id: str, timeout: int=2) -> dict|None:
        key = f"task_queue:{agent_id}"
        try:
            r = await self.redis.brpop(key, timeout=timeout)
            if r: _, p = r; return json.loads(p)
        except Exception: pass
        return None
    async def publish(self, channel: str, message: dict):
        await self.redis.publish(channel, json.dumps(message, ensure_ascii=False))

class AsyncRegistry:
    def __init__(self, db_path: str): self._p = db_path; self._db = None
    async def _g(self):
        if not self._db:
            import aiosqlite
            self._db = await aiosqlite.connect(self._p)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY,capabilities TEXT DEFAULT '',status TEXT DEFAULT 'idle',last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await self._db.commit()
        return self._db
    async def register(self, agent_id: str, caps: list[str], status: str):
        d = await self._g()
        await d.execute("INSERT INTO agents VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(agent_id) DO UPDATE SET capabilities=excluded.capabilities,status=excluded.status,last_heartbeat=CURRENT_TIMESTAMP", (agent_id, ",".join(caps), status))
        await d.commit()
    async def update_heartbeat(self, agent_id: str):
        d = await self._g()
        await d.execute("UPDATE agents SET last_heartbeat=CURRENT_TIMESTAMP WHERE agent_id=?", (agent_id,)); await d.commit()
    async def close(self):
        if self._db: await self._db.close(); self._db = None

class AsyncWorkerBase(ABC):
    _HEARTBEAT_INTERVAL = 10.0
    _needs_task = False

    @classmethod
    def get_capabilities(cls):
        try: return cls._class_capabilities
        except AttributeError: return []

    def __init__(self, agent_id: str, capabilities: list[str], registry: AsyncRegistry,
                 messaging: AsyncMessaging, max_concurrency: int=10):
        self.agent_id = agent_id; self.capabilities = capabilities
        self._registry = registry; self._messaging = messaging
        self.max_concurrency = max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        self._active = 0; self._failures = 0; self._max_failures = 3
        self._lock = asyncio.Lock(); self._hb = None
        self._running = False; self._shutting = False

    async def _call_llm(self, system="", prompt="", instruction="", max_attempts=3) -> str:
        from llm_client import call_llm_async
        result = await call_llm_async(
            system or "You are a helpful assistant.",
            prompt or instruction or "",
            expect_json=False,
            max_attempts=max_attempts,
        )
        return result if isinstance(result, str) else str(result)

    @property
    def llm_client(self):
        return self

    @abstractmethod
    async def execute(self, instruction: str) -> str: ...

    async def run(self):
        await self._messaging.connect()
        await self._registry.register(self.agent_id, self.capabilities, self._status_str())
        self._running = True; self._hb = asyncio.create_task(self._heartbeat())
        self._kill = asyncio.create_task(self._listen_kill())
        try: await self._task_loop()
        except asyncio.CancelledError: pass
        finally: await self._shutdown()

    async def shutdown(self):
        if self._shutting: return
        self._shutting = True; self._running = False
        deadline = time.time() + 30
        while self._active > 0 and time.time() < deadline: await asyncio.sleep(0.5)
        try: await self._registry.register(self.agent_id, self.capabilities, "offline")
        except Exception: pass

    async def _heartbeat(self):
        while self._running:
            try: await self._registry.update_heartbeat(self.agent_id)
            except Exception: pass
            await asyncio.sleep(self._HEARTBEAT_INTERVAL)

    async def _listen_kill(self):
        """监听 agent.kill:{id} 频道，收到 die 指令后优雅退出。"""
        try:
            ps = self._messaging.redis.pubsub()
            await ps.subscribe(f"agent.kill:{self.agent_id}")
            logger.info("Kill listener started for '%s'", self.agent_id)
            async for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    continue
                if data.get("action") == "die":
                    logger.info("Kill signal received for '%s', shutting down", self.agent_id)
                    await self.shutdown()
                    # 进程级兜底：确保 Worker 进程真正退出（避免挂起）
                    import os
                    os._exit(0)
        except Exception as exc:
            logger.warning("Kill listener error for '%s': %s", self.agent_id, exc)

    async def _task_loop(self):
        while self._running:
            async with self._sem:
                if not self._running: break
                task = await self._messaging.pop_task(self.agent_id, timeout=2)
                if not task: continue
                self._active += 1
                await self._update()
                asyncio.create_task(self._handle(task))

    async def _handle(self, task: dict):
        tid = task.get("task_id","?"); instr = task.get("instruction","")
        ok = False; res = ""
        try:
            if self._needs_task:
                res = await self.execute(instr, task); self._failures = 0; ok = True
            else:
                res = await self.execute(instr); self._failures = 0; ok = True
        except Exception as e:
            res = str(e); self._failures += 1
            if self._failures >= self._max_failures:
                await self._registry.register(self.agent_id, self.capabilities, "offline")
        finally:
            self._active -= 1; await self._update()
            await self._messaging._redis.rpush(
                f"task_result:{tid}",
                json.dumps({"task_id":tid,"agent_id":self.agent_id,"status":"SUCCESS" if ok else "FAILED","result":res}, ensure_ascii=False))

    def _status_str(self):
        return f"active:{self._active}/{self.max_concurrency}" if self._active else f"idle:0/{self.max_concurrency}"

    async def _update(self):
        try: await self._registry.register(self.agent_id, self.capabilities, self._status_str())
        except Exception: pass

    async def _shutdown(self):
        if self._hb: self._hb.cancel()
        if getattr(self, "_kill", None): self._kill.cancel()
        try: await self._registry.register(self.agent_id, self.capabilities, "offline")
        except Exception: pass
        try: await self._messaging.close()
        except Exception: pass

class AsyncSearchAgent(AsyncWorkerBase):
    async def execute(self, instruction: str) -> str:
        await asyncio.sleep(0.3)
        return f"[Search] '{instruction}' -> 3 results"

async def amain():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    h = os.environ.get("REDIS_HOST","localhost"); p = int(os.environ.get("REDIS_PORT","6379"))
    db = os.environ.get("REGISTRY_DB","agents.db"); mc = int(os.environ.get("MAX_CONCURRENCY","10"))
    w = AsyncSearchAgent("async_search", ["web_search"], AsyncRegistry(db), AsyncMessaging(h,p), mc)
    await w.run()

def main(): asyncio.run(amain())

if __name__ == "__main__":
    main()
