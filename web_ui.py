"""WeaveMind Web UI"""
import json, os, sqlite3, threading, time, uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
DB_PATH = os.environ.get("REGISTRY_DB", "agents.db")
PORT = int(os.environ.get("WEB_PORT", "8080"))
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_task_results = {}
_task_lock = threading.Lock()

_events = []
_events_lock = threading.Lock()
_START_TIME = time.time()
_METRICS_SUMMARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics_summary.json")
_STALE_AFTER_SECONDS = int(os.environ.get("STALE_TASK_TIMEOUT", "1800"))
_memory_manager = None
_memory_manager_lock = threading.Lock()
_EVOLUTION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution_history.json")
_evolution_results = []
_evolution_lock = threading.Lock()
_memory_summary_cache = {"text": "", "ts": 0.0, "signature": ""}
_TEMPLATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _iso_utc(s):
    """把 SQLite 的 UTC 时间 'YYYY-MM-DD HH:MM:SS' 转成带时区的 ISO 字符串。"""
    if not s:
        return s
    s = str(s).strip()
    if "T" in s or s.endswith("Z"):
        return s
    return s.replace(" ", "T") + "Z"

def _init_db():
    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS task_history(task_id TEXT PRIMARY KEY, goal TEXT, status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP, report TEXT)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(task_history)").fetchall()]
        if "conversation_id" not in cols:
            db.execute("ALTER TABLE task_history ADD COLUMN conversation_id TEXT DEFAULT ''")
        if "parent_task_id" not in cols:
            db.execute("ALTER TABLE task_history ADD COLUMN parent_task_id TEXT DEFAULT ''")
        db.commit(); db.close()
    except Exception: pass

def _list_tasks(limit=50):
    try:
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM task_history ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        db.close()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = _iso_utc(d.get("created_at"))
            d["completed_at"] = _iso_utc(d.get("completed_at"))
            out.append(d)
        return out
    except Exception: return []

def _build_conversation_context(conv_id, limit=4):
    """取该会话最近的消息（要求+结果摘要），拼成后续任务的对话上下文。"""
    if not conv_id:
        return ""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5); db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT task_id, goal, status, report FROM task_history "
            "WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        db.close()
    except Exception:
        return ""
    parts = []
    for r in reversed(rows):
        goal = (r["goal"] or "")[:300]
        status = r["status"] or "PENDING"
        snippet = ""
        if r["report"]:
            snippet = str(r["report"])[:200].replace("\n", " ")
        elif status == "PENDING":
            snippet = "(未完成)"
        parts.append(f"- 用户要求: {goal} | 结果({status}): {snippet}")
    return "\n".join(parts)

def _list_conversations(limit=50):
    """按会话分组的历史列表（用于对话切换）。"""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5); db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT conversation_id, COUNT(*) AS message_count, "
            "MAX(created_at) AS last_updated "
            "FROM task_history WHERE conversation_id != '' "
            "GROUP BY conversation_id ORDER BY last_updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            conv = dict(r)
            first = db.execute(
                "SELECT goal, status FROM task_history WHERE conversation_id=? ORDER BY created_at ASC LIMIT 1",
                (conv["conversation_id"],),
            ).fetchone()
            last = db.execute(
                "SELECT status FROM task_history WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                (conv["conversation_id"],),
            ).fetchone()
            conv["title"] = (first["goal"] if first else "")[:80]
            conv["last_status"] = last["status"] if last else ""
            conv["last_updated"] = _iso_utc(conv.get("last_updated"))
            out.append(conv)
        db.close()
        return out
    except Exception:
        return []

def _get_conversation(conv_id):
    """返回某个会话的全部消息。"""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5); db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT task_id, goal, status, created_at, completed_at, report "
            "FROM task_history WHERE conversation_id=? ORDER BY created_at ASC",
            (conv_id,),
        ).fetchall()
        db.close()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = _iso_utc(d.get("created_at"))
            d["completed_at"] = _iso_utc(d.get("completed_at"))
            d["report_preview"] = (d.get("report") or "")[:500]
            out.append(d)
        return out
    except Exception:
        return []

def _load_config():
    try:
        with open(CONFIG_PATH,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return {"llm":{"api_key":"","base_url":"","model":""},"redis":{"host":"localhost","port":6379},"system":{"task_timeout":90}}

def _save_config(cfg):
    # 与现有配置合并，避免前端表单未携带的段（如 embedding）被覆盖丢失
    existing = _load_config() or {}
    if isinstance(existing, dict):
        existing.update({k: v for k, v in cfg.items() if v is not None})
    else:
        existing = cfg
    with open(CONFIG_PATH,"w",encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

def _listen_results():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ps = r.pubsub(); ps.subscribe("orchestrator:response")
    for msg in ps.listen():
        if msg["type"] == "message":
            try:
                data = json.loads(msg["data"]); tid = data.get("task_id","")
                if tid:
                    with _task_lock:
                        existing = _task_results.get(tid, {})
                        # If progress update, merge payload
                        if data.get("type") and data.get("payload"):
                            ptype = data["type"]
                            payload = data["payload"]
                            if ptype == "task_complete":
                                existing["status"] = payload.get("status", existing.get("status", "PENDING"))
                                existing["report"] = payload.get("report", existing.get("report", ""))
                                existing["steps"] = payload.get("steps", existing.get("steps", []))
                                # Persist to SQLite so History page updates
                                try:
                                    db = sqlite3.connect(DB_PATH, timeout=5)
                                    db.execute("INSERT INTO task_history(task_id,goal,status,report,completed_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,report=excluded.report,completed_at=CURRENT_TIMESTAMP",
                                        (tid, existing.get("goal",""), payload.get("status","UNKNOWN"), payload.get("report","")))
                                    db.commit(); db.close()
                                except Exception: pass
                            elif ptype == "plan_update":
                                existing["steps"] = payload.get("steps", existing.get("steps", []))
                            elif ptype == "log":
                                logs = existing.get("logs", [])
                                logs.append(payload)
                                existing["logs"] = logs
                            elif ptype == "agent_status":
                                existing["agent_status"] = payload
                            elif ptype == "plan":
                                existing["steps"] = payload.get("steps", existing.get("steps", []))
                            # Keep status if not set
                            if "status" not in existing: existing["status"] = "RUNNING"
                        else:
                            # Direct state update (e.g. final result)
                            existing.update(data)
                        _task_results[tid] = existing
                        # 防止内存无限增长：最多保留最近 300 个任务
                        if len(_task_results) > 300:
                            for _k in list(_task_results)[:-200]:
                                _task_results.pop(_k, None)
            except Exception: pass

def _listen_events():
    """订阅告警/进化/守护事件，供 Health 页真实展示。"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ps = r.pubsub()
    ps.subscribe("orchestrator:alert", "orchestrator:evolution_result", "guardian.heartbeat")
    for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            if msg["channel"] == "orchestrator:evolution_result":
                _append_evolution(data)
            etype = data.get("type", msg["channel"].split(":")[-1])
            with _events_lock:
                _events.append({
                    "id": f"evt-{len(_events)}",
                    "timestamp": data.get("timestamp") or _now_iso(),
                    "type": _map_event_type(etype),
                    "service": data.get("service", msg["channel"]),
                    "message": data.get("message") or data.get("summary") or json.dumps(data, ensure_ascii=False)[:120],
                })
                if len(_events) > 200:
                    del _events[:-200]
        except Exception:
            pass

def _map_event_type(t: str) -> str:
    t = str(t).lower()
    if "quarantin" in t or "dead" in t or "crash" in t or "fail" in t:
        return "crash"
    if "reviv" in t or "recover" in t:
        return "recovery"
    if "evolution" in t:
        return "evolution"
    if "scale" in t or "guardian" in t:
        return "guardian"
    return "guardian"

def _system_status():
    try:
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
        agents = [dict(row) for row in db.execute("SELECT * FROM agents").fetchall()]
        for a in agents:
            a["last_heartbeat"] = _iso_utc(a.get("last_heartbeat"))
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        try: queues = {a["agent_id"]: r.llen(f"task_queue:{a['agent_id']}") for a in agents}
        except Exception: queues = {}
        total = db.execute("SELECT COUNT(*) as c FROM task_history").fetchone()["c"]
        success = db.execute("SELECT COUNT(*) as c FROM task_history WHERE status='SUCCESS'").fetchone()["c"]
        today = db.execute(
            "SELECT COUNT(*) as c FROM task_history WHERE created_at >= datetime('now', '-1 day')"
        ).fetchone()["c"]
        recent = [dict(row) for row in db.execute("SELECT * FROM task_history ORDER BY created_at DESC LIMIT 5").fetchall()]
        for r in recent:
            r["created_at"] = _iso_utc(r.get("created_at"))
            r["completed_at"] = _iso_utc(r.get("completed_at"))
        db.close()
        online = sum(1 for a in agents if not a.get("status", "").startswith("offline"))
        survival = round(online / len(agents) * 100, 1) if agents else 100
        # 真实记忆统计（ChromaDB）
        memory = _get_memory_stats()
        return {
            "agents": agents,
            "queues": queues,
            "tasks": {"total": total, "success": success, "today": today},
            "memory": memory,
            "recent": recent,
            "uptime_sec": int(time.time() - _START_TIME),
            "survival_rate": survival,
            "llm_usage": _get_llm_usage(),
        }
    except Exception:
        return {"agents":[],"queues":{},"tasks":{"total":0,"success":0,"today":0},
                "memory":{"conversations":0,"strategies":0},"recent":[],"uptime_sec":0,"survival_rate":100,
                "llm_usage":{"calls":0,"prompt_tokens":0,"completion_tokens":0}}

def _get_llm_usage():
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        keys = r.keys("llm_usage*")
        for k in keys:
            raw = r.get(k)
            if raw:
                d = json.loads(raw)
                total["calls"] += int(d.get("calls", 0))
                total["prompt_tokens"] += int(d.get("prompt_tokens", 0))
                total["completion_tokens"] += int(d.get("completion_tokens", 0))
        if total["calls"]:
            return total
    except Exception:
        pass
    try:
        from llm_client import get_usage_stats
        return get_usage_stats()
    except Exception:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

def _get_memory_manager():
    """缓存 MemoryManager 实例，避免每个请求重建 Chroma 客户端。"""
    global _memory_manager
    try:
        if _memory_manager is None:
            with _memory_manager_lock:
                if _memory_manager is None:
                    from memory_manager import MemoryManager
                    _memory_manager = MemoryManager(os.environ.get("MEMORY_DIR", "./chroma_memory"))
        return _memory_manager
    except Exception:
        return None

def _get_memory_stats():
    mem = _get_memory_manager()
    if mem is None:
        return {"conversations": 0, "strategies": 0}
    try:
        return mem.stats()
    except Exception:
        return {"conversations": 0, "strategies": 0}

def _get_memory_data():
    mem = _get_memory_manager()
    return {
        "stats": _get_memory_stats(),
        "conversations": mem.list_conversations(50) if mem else [],
        "strategies": mem.list_strategies(50) if mem else [],
    }

def _get_memory_summary(refresh: bool = False) -> dict:
    """让 LLM 基于真实记忆生成一段"系统自述"（带缓存与兜底文案）。"""
    global _memory_summary_cache
    mem = _get_memory_manager()
    stats = _get_memory_stats()
    if mem is None:
        return {"summary": "", "cached": False, "error": "memory unavailable"}
    convs = mem.list_conversations(30)
    strats = mem.list_strategies(30)
    goals = [str(c.get("metadata", {}).get("goal", ""))[:100] for c in convs if c.get("metadata", {}).get("goal")]
    topics = [str(s.get("metadata", {}).get("goal_keywords", ""))[:80] for s in strats if s.get("metadata", {}).get("goal_keywords")]
    sig = f"{stats.get('conversations')}:{stats.get('strategies')}:{len(goals)}:{len(topics)}"
    now = time.time()
    if (not refresh and _memory_summary_cache["text"]
            and sig == _memory_summary_cache["signature"]
            and now - _memory_summary_cache["ts"] < 600):
        return {"summary": _memory_summary_cache["text"], "cached": True}

    fallback = (
        f"我是织光——一支运行在你本地的 AI 团队。我已完成了 {stats.get('conversations', 0)} 项任务、"
        f"沉淀了 {stats.get('strategies', 0)} 条成功策略"
        + (f"，最近涉足：{'、'.join(topics[:4])}" if topics else "")
        + "。我可以帮你调研、分析数据、写报告，还会在任务后自我复盘与进化。"
    )
    text = ""
    if goals or topics:
        prompt = (
            "你是织光智能体系统。请基于以下真实记忆数据，用第一人称写一段 150-300 字的中文自述，"
            "说明：1) 我服务过哪些类型的任务（挑 3-5 个代表性目标）；2) 我积累了多少经验"
            f"（对话 {stats.get('conversations', 0)} 条、策略 {stats.get('strategies', 0)} 条）；"
            "3) 我擅长什么、能怎么帮你。语气自信、有感染力，适合发布到社交媒体。直接输出正文，不要标题。\n\n"
            f"最近任务目标：{json.dumps(goals[:8], ensure_ascii=False)}\n"
            f"成功策略主题：{json.dumps(topics[:8], ensure_ascii=False)}"
        )
        try:
            from llm_client import call_llm
            result = call_llm("你是一个会自我介绍的多智能体系统。", prompt, expect_json=False)
            text = str(result.get("content") or "").strip()
        except Exception:
            text = ""
    if not text:
        text = fallback
    _memory_summary_cache = {"text": text, "ts": now, "signature": sig}
    return {"summary": text, "cached": False}

def _append_evolution(result: dict) -> None:
    """记录一轮进化结果（内存 + 落盘），供锦标赛回放。"""
    global _evolution_results
    with _evolution_lock:
        _evolution_results.append(result)
        if len(_evolution_results) > 30:
            _evolution_results = _evolution_results[-30:]
        try:
            with open(_EVOLUTION_FILE, "w", encoding="utf-8") as f:
                json.dump(_evolution_results, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def _load_evolution_history():
    global _evolution_results
    try:
        if os.path.exists(_EVOLUTION_FILE):
            with open(_EVOLUTION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _evolution_results = data[-30:]
    except Exception:
        pass

def _load_templates() -> list:
    try:
        with open(_TEMPLATES_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("templates", [])
    except Exception:
        return []

def _find_cached_task(goal: str, ttl_min: int):
    """结果缓存：相同目标在 TTL 内有过 SUCCESS，直接返回旧结果。"""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5); db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT task_id, report FROM task_history "
            "WHERE goal=? AND status='SUCCESS' AND completed_at IS NOT NULL "
            "AND completed_at >= datetime('now', ?) ORDER BY completed_at DESC LIMIT 1",
            (goal, f"-{ttl_min} minutes"),
        ).fetchone()
        db.close()
        return dict(row) if row else None
    except Exception:
        return None

HTML = '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>WeaveMind</title></head><body><div id="root"></div><script type="module" src="http://localhost:5173/@vite/client"></script><script type="module" src="http://localhost:5173/src/main.tsx"></script></body></html>'
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")

class Handler(BaseHTTPRequestHandler):
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _serve_dist(self, path: str) -> bool:
        """生产模式：伺服前端构建产物（frontend/dist），含 SPA 回退。"""
        if not os.path.isdir(DIST_DIR):
            return False
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = os.path.realpath(os.path.join(DIST_DIR, rel))
        if not target.startswith(os.path.realpath(DIST_DIR)):
            return False
        if not os.path.isfile(target):
            if "." not in os.path.basename(rel):
                target = os.path.join(DIST_DIR, "index.html")
            else:
                return False
        if not os.path.isfile(target):
            return False
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        try:
            with open(target, "rb") as f:
                body = f.read()
        except Exception:
            return False
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            if self._serve_dist("/"):
                return
            return self._html(HTML)
        if p == "/api/status": return self._json(_system_status())
        if p == "/api/config": return self._json(_load_config())
        if p == "/api/events":
            with _events_lock:
                return self._json({"events": list(reversed(_events[-100:]))})
        if p == "/api/memory":
            return self._json(_get_memory_data())
        if p == "/api/memory/summary":
            refresh = "refresh=1" in urlparse(self.path).query
            return self._json(_get_memory_summary(refresh))
        if p == "/api/templates":
            return self._json({"templates": _load_templates()})
        if p == "/api/evolution":
            with _evolution_lock:
                return self._json({"rounds": list(reversed(_evolution_results))})
        if p == "/api/metrics":
            try:
                with open(_METRICS_SUMMARY, "r", encoding="utf-8") as f:
                    return self._json(json.load(f))
            except Exception:
                return self._json({"error": "no metrics yet"}, 404)
        if p == "/tasks": return self._json({"tasks": _list_tasks(50)})
        if p == "/api/conversations": return self._json({"conversations": _list_conversations(50)})
        if p.startswith("/api/conversations/"):
            conv_id = p.split("/api/conversations/")[-1]
            msgs = _get_conversation(conv_id)
            if not msgs:
                return self._json({"error": "conversation not found"}, 404)
            return self._json({"conversation_id": conv_id, "messages": msgs})
        if p.startswith("/task/"):
            tid = p.split("/task/")[-1]
            with _task_lock: data = _task_results.get(tid)
            if not data:
                for t in _list_tasks(100):
                    if t.get("task_id") == tid: data = t; break
            if data: return self._json({
                "task_id": tid,
                "status": data.get("status", "PENDING"),
                "goal": data.get("goal", ""),
                "steps": data.get("steps", []),
                "report": data.get("final_report") or data.get("report", ""),
            })
            return self._json({"error":"not found"},404)
        if p.startswith("/task/") and p.endswith("/report"):
            tid = p.split("/task/")[-1].rsplit("/report", 1)[0]
            with _task_lock: data = _task_results.get(tid)
            if not data:
                for t in _list_tasks(100):
                    if t.get("task_id") == tid:
                        data = t
                        break
            if data and data.get("report"):
                return self._html(data["report"])
            return self._json({"error": "report not found"}, 404)
        if self._serve_dist(p):
            return
        return self._json({"error":"not found"},404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw) if raw else {}
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        if self.path == "/task":
            g = body.get("goal","").strip()
            # 模板：允许不传 goal，自动取模板目标与确定性步骤
            tpl_name = str(body.get("template") or "").strip()
            template_steps = None
            tpl_goal = ""
            if tpl_name:
                for tpl in _load_templates():
                    if tpl.get("name") == tpl_name:
                        template_steps = tpl.get("steps")
                        tpl_goal = str(tpl.get("goal") or "")
                        break
            if not g:
                g = tpl_goal
            if not g: return self._json({"error":"goal required"},400)
            conv_id = (body.get("conversation_id") or "").strip()
            parent_id = (body.get("parent_task_id") or "").strip()
            is_new_conversation = not conv_id
            if is_new_conversation:
                conv_id = "conv-" + uuid.uuid4().hex[:10]
            context = _build_conversation_context(conv_id) if not is_new_conversation else ""
            if not parent_id and not is_new_conversation:
                try:
                    db = sqlite3.connect(DB_PATH, timeout=3); db.row_factory = sqlite3.Row
                    row = db.execute(
                        "SELECT task_id FROM task_history WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                        (conv_id,),
                    ).fetchone()
                    parent_id = row["task_id"] if row else ""
                    db.close()
                except Exception:
                    pass
            tid = "ui-" + uuid.uuid4().hex[:10]
            auto_run = bool(body.get("auto_run", True))
            # 结果缓存：相同目标在 TTL 内已成功
            ttl = int(body.get("cache_ttl_min") or 0)
            if ttl > 0:
                cached = _find_cached_task(g, ttl)
                if cached and cached.get("report"):
                    return self._json({
                        "task_id": cached["task_id"], "status": "SUCCESS",
                        "cached": True, "report": cached["report"],
                    })
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            r.publish("orchestrator:main", json.dumps({
                "task_id": tid, "goal": g, "context": context,
                "auto_run": auto_run, "template_steps": template_steps,
            }, ensure_ascii=False))
            with _task_lock: _task_results[tid] = {"task_id":tid,"status":"PENDING","goal":g,"steps":[],"report":"","conversation_id":conv_id,"auto_run":auto_run}
            # Write to SQLite so History shows immediately
            try:
                db = sqlite3.connect(DB_PATH, timeout=3)
                db.execute(
                    "INSERT INTO task_history(task_id,goal,status,conversation_id,parent_task_id) VALUES(?,?,?,?,?)",
                    (tid, g, "PENDING", conv_id, parent_id),
                )
                db.commit(); db.close()
            except Exception: pass
            return self._json({"task_id":tid,"conversation_id":conv_id,"status":"PENDING"})
        if self.path == "/api/plan/confirm":
            tid = str(body.get("task_id", "")).strip()
            if not tid:
                return self._json({"error": "task_id required"}, 400)
            action = body.get("action", "confirm")
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            r.rpush(f"plan_confirm:{tid}", json.dumps({
                "action": action,
                "steps": body.get("steps"),
            }, ensure_ascii=False))
            return self._json({"status": "ok"})
        if self.path == "/api/evolution/trigger":
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            r.publish("orchestrator:main", json.dumps({"task_id":"evo-"+str(int(time.time())),"goal":"EVOLUTION_TRIGGER"}, ensure_ascii=False))
            return self._json({"status":"triggered"})

        if self.path == "/api/single-agent":
            g = body.get("goal","").strip()
            if not g: return self._json({"error":"goal required"},400)
            import time as _t
            start = _t.time()
            try:
                from llm_client import LLMClient
                llm = LLMClient()
                result = llm.call("Answer directly.", g, expect_json=False)
                return self._json({"result": result, "duration": round(_t.time()-start,1)})
            except Exception as e:
                return self._json({"result": f"Error: {e}", "duration": _t.time()-start})

        if self.path == "/api/kill-worker":
            agent_id = body.get("agent_id","")
            if not agent_id: return self._json({"error":"agent_id required"},400)
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            r.publish(f"agent.kill.{agent_id}", json.dumps({"action":"die"}))
            return self._json({"status":"killed","agent_id":agent_id})
        if self.path == "/api/config":
            _save_config(body)
            llm = body.get("llm",{})
            if llm.get("api_key"): os.environ["LLM_API_KEY"] = llm["api_key"]
            if llm.get("base_url"): os.environ["LLM_BASE_URL"] = llm["base_url"]
            if llm.get("model"): os.environ["LLM_MODEL"] = llm["model"]
            return self._json({"status":"saved"})
        return self._json({"error":"not found"},404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args): pass

def main():
    from logging_setup import setup_logging
    setup_logging("webui")
    _init_db()
    _load_evolution_history()
    threading.Thread(target=_listen_results, daemon=True).start()
    threading.Thread(target=_listen_events, daemon=True).start()

    def _cleanup_stale_tasks():
        """把长时间卡在 PENDING 的任务标记为过期，避免永久悬挂。"""
        while True:
            time.sleep(60)
            try:
                db = sqlite3.connect(DB_PATH, timeout=5)
                db.execute(
                    "UPDATE task_history SET status='FAILED', report='Task expired (no completion within %d s)' "
                    "WHERE status='PENDING' AND created_at < datetime('now', ?)",
                    (f"-{_STALE_AFTER_SECONDS} seconds",),
                )
                db.commit(); db.close()
            except Exception:
                pass

    threading.Thread(target=_cleanup_stale_tasks, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"http://localhost:{PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()

if __name__ == "__main__":
    main()
