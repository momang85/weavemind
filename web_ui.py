"""WeaveMind Web UI"""
import base64, hashlib, hmac, html, io, json, logging, mimetypes, os, re, secrets, socket, sqlite3, subprocess, sys, tempfile, threading, time, uuid, zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
import redis

from audit_logger import audit_log, read_audit
from workspace import (
    _safe_project,
    list_projects,
    task_workspace,
)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
DB_PATH = os.environ.get("REGISTRY_DB", "agents.db")
PORT = int(os.environ.get("WEB_PORT", "8080"))
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PROJECT_DIR = os.path.join(tempfile.gettempdir(), "agent_workspace", "project")

_task_results = {}
_task_lock = threading.Lock()

# ---- 报告分享（公开只读链接）----
# token → task_id 映射持久化到磁盘 JSON。选择理由：
# 当前 Redis 承担消息总线职责且未确认开启持久化，重启后分享链接不应失效，
# 磁盘文件 + 原子替换更稳，也不依赖外部服务可用性。
SHARE_FILE = os.environ.get(
    "SHARE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "share_links.json"),
)
SHARE_TTL_SECONDS = int(os.environ.get("SHARE_TTL_SECONDS", str(7 * 24 * 3600)))
SHARE_AUTH_COOKIE_TTL = 7 * 24 * 3600  # 密码验证通过的 Cookie 有效期 7 天
_share_lock = threading.Lock()

_events = []
_sched_runner_ref: list = []  # 运行中的调度器实例（/api/scheduled-jobs 读 last_results 用）
_failure_notify_lock = threading.Lock()
_failure_notified: set[str] = set()  # 已发过失败通知的 task_id（跨通道去重）


def _try_claim_failure_notify(task_id: str) -> bool:
    """失败通知全局去重：同一 task_id 只允许一个通道发一次失败通知。

    调度器结果追踪（runner._on_failure）与全局监听器（D1 分支）都会
    遇到同一失败任务；没有去重时用户会收到两条内容雷同的失败通知。"""
    with _failure_notify_lock:
        if task_id in _failure_notified:
            return False
        _failure_notified.add(task_id)
        # 上限保护：常驻进程防无界增长
        if len(_failure_notified) > 500:
            for k in list(_failure_notified)[:-300]:
                _failure_notified.discard(k)
        return True


def _publish_alert(alert_type: str, message: str, service: str = "scheduler") -> None:
    """发布 orchestrator:alert 事件（Health 页事件流）；失败静默（尽力而为）。"""
    try:
        _new_redis().publish("orchestrator:alert", json.dumps({
            "type": alert_type,
            "service": service,
            "message": message,
            "timestamp": _now_iso(),
        }, ensure_ascii=False))
    except Exception:
        pass


def _llm_precheck_notify(reasons: list[str]) -> list[str]:
    """端点预检结果的统一出口：非空即发布 llm_balance_low 告警（Health 可见）。

    两条提交路径（/task 与 /api/single-agent）共用，避免同源风险
    只在主路径告警、单智能体路径静默放行。返回原 reasons 便于调用方判断。"""
    if reasons:
        _publish_alert(
            "llm_balance_low",
            "LLM 端点预警：" + "；".join(reasons) + "，请及时处理",
            service="llm",
        )
    return reasons


_embed_alert_state = {"last_ts": 0.0}
_EMBED_ALERT_COOLDOWN_SECONDS = 1800


def _publish_embed_alert(notice: str) -> None:
    """Embedding 降级告警：按冷却窗口发布，避免状态轮询把事件时间线刷满。"""
    now = time.time()
    if now - float(_embed_alert_state.get("last_ts") or 0.0) < _EMBED_ALERT_COOLDOWN_SECONDS:
        return
    _embed_alert_state["last_ts"] = now
    _publish_alert("embedding_degraded", "Embedding 预警：" + notice, service="memory")
_events_lock = threading.Lock()
_evt_seq = 0
_rate_limiter = None
_START_TIME = time.time()
_METRICS_SUMMARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics_summary.json")
_STALE_AFTER_SECONDS = int(os.environ.get("STALE_TASK_TIMEOUT", "1800"))
_memory_manager = None
_memory_manager_lock = threading.Lock()
_mem_stats_cache = {"ts": 0.0, "data": {"conversations": 0, "strategies": 0}}
_mem_stats_lock = threading.Lock()
_EVOLUTION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution_history.json")
_evolution_results = []
_evolution_lock = threading.Lock()

# ---- 多用户鉴权（轻量方案） ----
# 会话只存内存：重启后需重新登录；token 用 secrets.token_urlsafe(32) 生成。
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
_PBKDF2_ITERATIONS = int(os.environ.get("PBKDF2_ITERATIONS", "200000"))
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _get_rate_limiter():
    """惰性初始化限流器（默认每 IP 30 次/分钟，可用环境变量调整）。"""
    global _rate_limiter
    if _rate_limiter is None:
        from security import RateLimiter
        _rate_limiter = RateLimiter(
            limit=int(os.environ.get("RATE_LIMIT_PER_MIN", "30")),
            window=60.0,
        )
    return _rate_limiter

# ---- 登录 / 分享密码 暴力破解防护（进程内滑动窗口 + 临时锁定） ----
_LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
_LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))
_bruteforce: dict[str, list[float]] = {}   # key -> 失败时间戳列表
_bruteforce_lock = threading.Lock()


def _bf_check(key: str):
    """返回当前 key 是否在锁定中（True=锁定）以及剩余秒数（0 表示未锁定）。"""
    now = time.time()
    with _bruteforce_lock:
        bucket = [t for t in _bruteforce.get(key, []) if now - t < _LOGIN_LOCKOUT_SECONDS]
        if len(bucket) >= _LOGIN_MAX_FAILS:
            _bruteforce[key] = bucket
            return True, int(_LOGIN_LOCKOUT_SECONDS - (now - bucket[-1]))
        _bruteforce[key] = bucket
        return False, 0


def _bf_record(key: str):
    """记录一次失败尝试（用于登录与分享密码）。"""
    now = time.time()
    with _bruteforce_lock:
        bucket = [t for t in _bruteforce.get(key, []) if now - t < _LOGIN_LOCKOUT_SECONDS]
        bucket.append(now)
        _bruteforce[key] = bucket


def _bf_reset(key: str):
    """登录/分享验证成功后清除失败记录。"""
    with _bruteforce_lock:
        _bruteforce.pop(key, None)

_memory_summary_cache = {"text": "", "ts": 0.0, "signature": ""}
_memory_summary_lock = threading.Lock()
_memory_summary_generating = False
_memory_data_cache = {"ts": 0.0, "data": None}
_memory_data_lock = threading.Lock()
_memory_data_refreshing = False
_TEMPLATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")

def _new_redis():
    """带超时的 Redis 客户端：Redis 不可用时快速失败，避免请求挂死。"""
    return redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
        socket_connect_timeout=2, socket_timeout=3,
    )

def _redis_ready(timeout: float = 0.5) -> bool:
    """快速 TCP 探测：Redis 不可用（如容器停止后 Docker 遗留的静默端口代理）时，
    毫秒级失败，避免 redis-py 在连接超时后反复重试导致请求挂死。"""
    try:
        with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


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

def _load_shares() -> dict:
    """读取分享映射，并顺带清理过期 token；文件缺失/损坏时返回空映射。"""
    try:
        with open(SHARE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    now = time.time()
    changed = False
    for token, info in list(data.items()):
        exp = info.get("expires_at") if isinstance(info, dict) else None
        if exp:
            try:
                if datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp() <= now:
                    data.pop(token, None)
                    changed = True
            except Exception:
                pass
    if changed:
        _save_shares(data)
    return data

def _save_shares(data: dict) -> None:
    """原子写分享映射：先写临时文件再 os.replace，避免服务中断损坏映射文件。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SHARE_FILE)), exist_ok=True)
        tmp = SHARE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SHARE_FILE)
    except Exception:
        pass

def _generate_share_token(
    task_id: str,
    password: str | None = None,
    ttl_hours: float | None = None,
) -> str:
    """生成/复用任务的分享 token（F6：支持密码与自定义有效期）。
    选择“幂等复用”：同一任务重复生成保持同一链接，撤销后再生成才换新 token，
    避免同一报告产生多个失控链接。
    - password：None 表示复用已有记录不改动；空串表示清除密码；非空则存 pbkdf2 哈希；
    - ttl_hours：None 表示沿用默认（SHARE_TTL_SECONDS）；否则按小时重算 expires_at。"""
    with _share_lock:
        data = _load_shares()
        for token, info in data.items():
            if isinstance(info, dict) and info.get("task_id") == task_id:
                if password is not None:
                    if password:
                        info["password_hash"] = _hash_password(password)
                    else:
                        info.pop("password_hash", None)
                if ttl_hours is not None:
                    info["expires_at"] = (
                        datetime.now(timezone.utc)
                        + timedelta(hours=float(ttl_hours))
                    ).isoformat()
                _save_shares(data)
                return token
        token = secrets.token_urlsafe(16)
        record = {
            "task_id": task_id,
            "created_at": _now_iso(),
            "expires_at": (
                datetime.now(timezone.utc)
                + timedelta(
                    hours=float(ttl_hours)
                    if ttl_hours is not None
                    else SHARE_TTL_SECONDS / 3600
                )
            ).isoformat(),
        }
        if password:
            record["password_hash"] = _hash_password(password)
        data[token] = record
        _save_shares(data)
        return token

def _find_share_token(task_id: str) -> str | None:
    """查询某任务当前的分享 token；未分享返回 None。"""
    with _share_lock:
        data = _load_shares()
        for token, info in data.items():
            if isinstance(info, dict) and info.get("task_id") == task_id:
                return token
    return None

def _resolve_share_token(token: str) -> str | None:
    """按 token 解析 task_id；非法/过期 token 返回 None。"""
    with _share_lock:
        info = _load_shares().get(token)
        return info.get("task_id") if isinstance(info, dict) else None

def _revoke_share_token(task_id: str) -> int:
    """撤销某任务的全部分享 token，返回撤销数量。"""
    with _share_lock:
        data = _load_shares()
        removed = [
            t for t, info in data.items()
            if isinstance(info, dict) and info.get("task_id") == task_id
        ]
        for t in removed:
            data.pop(t, None)
        if removed:
            _save_shares(data)
        return len(removed)


def _task_exists(task_id: str) -> bool:
    """任务是否存在于内存结果或 SQLite 历史中。"""
    with _task_lock:
        if task_id in _task_results:
            return True
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        row = db.execute("SELECT 1 FROM task_history WHERE task_id=?", (task_id,)).fetchone()
        db.close()
        return bool(row)
    except Exception:
        return False


def _delete_task(task_id: str) -> bool:
    """删除任务：撤销分享 → 移除内存结果 → 删除 SQLite 历史。
    工作区落盘文件保留（避免误删交付物，属可恢复设计）。"""
    _revoke_share_token(task_id)
    with _task_lock:
        _task_results.pop(task_id, None)
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        db.execute("DELETE FROM task_history WHERE task_id=?", (task_id,))
        db.commit()
        db.close()
        return True
    except Exception:
        return False

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
        if "context" not in cols:
            db.execute("ALTER TABLE task_history ADD COLUMN context TEXT DEFAULT ''")
        if "project" not in cols:
            db.execute("ALTER TABLE task_history ADD COLUMN project TEXT DEFAULT 'default'")
        if "user" not in cols:
            # P1-3：结果缓存需按提交者隔离，避免不同用户/项目/上下文互相命中缓存
            db.execute("ALTER TABLE task_history ADD COLUMN user TEXT DEFAULT ''")
        if "steps_json" not in cols:
            # BUG-8：持久化步骤/日志，历史页载入后仍可回放执行计划（重启不丢）
            db.execute("ALTER TABLE task_history ADD COLUMN steps_json TEXT DEFAULT ''")
        if "logs_json" not in cols:
            db.execute("ALTER TABLE task_history ADD COLUMN logs_json TEXT DEFAULT ''")
        # C1：会话持久化——webui 重启后未过期会话自动恢复，不再全员掉登录
        db.execute(
            "CREATE TABLE IF NOT EXISTS sessions("
            "token TEXT PRIMARY KEY, user TEXT, role TEXT, expires REAL)"
        )
        db.commit(); db.close()
        _load_sessions()
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
            "SELECT task_id, goal, status, report, context FROM task_history "
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
        if r["context"]:
            parts.append(f"  用户补充背景: {str(r['context'])[:200]}")
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
        incoming = {k: v for k, v in cfg.items() if v is not None}
        # users 段（含密码哈希）只允许服务端通过初始管理员/环境变量流程管理，
        # 前端保存配置时不得覆盖或注入用户。
        incoming.pop("users", None)
        # GET 回显已将 api_key 脱敏为空串；前端未改动时不得把真实
        # 密钥清空——只有显式提交非空新值才覆盖（与通知配置同口径）
        prev_llm_sections = {
            k: (existing.get(k) or {}) for k in _LLM_KEY_SECTIONS
        }
        for sec in _LLM_KEY_SECTIONS:
            inc = incoming.get(sec)
            if not isinstance(inc, dict):
                continue
            if not str(inc.get("api_key") or ""):
                prev_key = str((prev_llm_sections.get(sec) or {}).get("api_key") or "")
                if prev_key:
                    inc["api_key"] = prev_key
        existing.update(incoming)
    else:
        existing = cfg
    with open(CONFIG_PATH,"w",encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


# LLM 相关且含 api_key 的配置段：脱敏与"空值不回写"保护统一按此表
_LLM_KEY_SECTIONS = ("llm", "embedding", "planner", "backup", "image")


def _public_config(cfg: dict | None) -> dict:
    """返回给前端的配置副本：剥离 users 段与全部密钥。

    - users（含密码哈希）不入响应；
    - LLM 相关五段的 api_key 置空 + api_key_set 标记（已配置），
      前端据此显示"已配置（留空保持不变）"掩码态；明文 key 永不下发；
    - notifications 段复用 public_notifications_config（脱敏
      password/sendkey，与 /api/notifications 同口径）；
    - 兜底递归清扫其余段中的 api_key/password/secret/sendkey/token 类键。
    """
    out = dict(cfg or {})
    out.pop("users", None)
    # 先统一清零密钥类键（递归兜底），再补"已配置"标记
    out = _scrub_secret_keys(out)
    for sec in _LLM_KEY_SECTIONS:
        sec_cfg = out.get(sec)
        if isinstance(sec_cfg, dict):
            orig_key = str(((cfg or {}).get(sec) or {}).get("api_key") or "")
            sec_cfg["api_key_set"] = bool(orig_key)
            sec_cfg["api_key"] = ""
    if isinstance(out.get("notifications"), dict):
        try:
            from notifications import public_notifications_config
            out["notifications"] = public_notifications_config(out["notifications"])
        except Exception:
            pass
    out.pop("audit", None)  # 审计配置属服务端内部，不回显
    return out


def _scrub_secret_keys(node):
    """递归清扫 api_key/password/secret/sendkey/token 类键（防御性兜底；
    跳过 *_set 类布尔标记键，避免误伤"已配置"状态位）。"""
    if isinstance(node, dict):
        cleaned = {}
        for k, v in node.items():
            lk = str(k).lower()
            if (
                not lk.endswith("_set")
                and any(s in lk for s in ("api_key", "password", "secret", "sendkey", "token"))
            ):
                cleaned[k] = ""
                continue
            cleaned[k] = _scrub_secret_keys(v)
        return cleaned
    if isinstance(node, list):
        return [_scrub_secret_keys(v) for v in node]
    return node


# ---- 用户与密码（pbkdf2_hmac + 随机盐，绝不明文） ----

def _hash_password(password: str) -> str:
    """生成自描述哈希：pbkdf2_sha256$迭代次数$盐$哈希（均 base64）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${_PBKDF2_ITERATIONS}"
        f"${base64.b64encode(salt).decode('ascii')}"
        f"${base64.b64encode(dk).decode('ascii')}"
    )


def _verify_password(password: str, stored: str | None) -> bool:
    """常量时间比较；格式非法一律失败。"""
    if not stored:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _user_hash_valid(stored: str | None) -> bool:
    """判断哈希是否可解析（pbkdf2_sha256 + 合法 base64）。
    config.example.json 里的 <BASE64_SALT>/<BASE64_HASH> 占位符不可解析，
    因此被视作“未初始化”，首次访问仍可创建管理员，避免示例配置被照抄后锁死。"""
    if not stored:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = str(stored).split("$")
        base64.b64decode(salt_b64)
        base64.b64decode(hash_b64)
        return algo == "pbkdf2_sha256" and int(iterations) > 0
    except Exception:
        return False


def _load_users() -> dict:
    """读取 config.json 的 users 段；缺失或损坏返回空字典。"""
    cfg = _load_config()
    users = cfg.get("users") if isinstance(cfg, dict) else None
    return users if isinstance(users, dict) else {}


def _users_initialized() -> bool:
    """是否存在至少一个可用的密码哈希（占位哈希不算初始化）。"""
    return any(
        isinstance(user, dict) and _user_hash_valid(user.get("password_hash"))
        for user in _load_users().values()
    )


def _save_users(users: dict) -> bool:
    """把 users 段合并写回 config.json。"""
    cfg = _load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["users"] = users
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _ensure_users_on_startup():
    """首次启动引导：
    1) config.json 已有 users 段 → 不做任何事；
    2) 设置了 WEAVEMIND_ADMIN_PASSWORD → 自动创建 admin（用户名可用
       WEAVEMIND_ADMIN_USERNAME 覆盖，默认 admin）；
    3) 都没有 → 打日志提示，首次访问登录页时走 /api/setup-admin 创建初始管理员。
    """
    users = _load_users()
    if _users_initialized():
        return
    password = os.environ.get("WEAVEMIND_ADMIN_PASSWORD", "")
    if password:
        username = (os.environ.get("WEAVEMIND_ADMIN_USERNAME", "admin") or "admin").strip() or "admin"
        # 丢弃占位/损坏哈希（如 config.example.json 的 <BASE64_*>），避免污染用户表
        users = {
            name: info for name, info in users.items()
            if isinstance(info, dict) and _user_hash_valid(info.get("password_hash"))
        }
        users[username] = {
            "password_hash": _hash_password(password),
            "role": "admin",
            "created_at": _now_iso(),
        }
        if _save_users(users):
            logging.getLogger("web_ui").info(
                "已通过 WEAVEMIND_ADMIN_PASSWORD 自动创建初始管理员：%s", username
            )
            audit_log(username, "startup", "user.bootstrap", target=username, result="ok",
                      detail="自动创建于启动时（环境变量）")
        else:
            logging.getLogger("web_ui").warning("无法写入 config.json，初始管理员创建失败")
    else:
        logging.getLogger("web_ui").warning(
            "config.json 无 users 段且未设置 WEAVEMIND_ADMIN_PASSWORD；"
            "首次访问登录页时可创建初始管理员（仅一次）。"
        )


# ---- 会话（内存 token → 用户/角色/过期时间） ----

def _load_sessions():
    """启动时从 SQLite 恢复未过期会话到内存；过期条目顺带清理。"""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT token, user, role, expires FROM sessions").fetchall()
        db.close()
        now = time.time()
        alive = [(r["token"], r["user"], r["role"], r["expires"]) for r in rows]
        with _sessions_lock:
            for token, user, role, expires in alive:
                if expires > now:
                    _sessions[token] = {
                        "user": user, "role": role, "expires": expires,
                    }
        try:
            db = sqlite3.connect(DB_PATH, timeout=5)
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.commit(); db.close()
        except Exception:
            pass
    except Exception:
        pass


def _create_session(username: str, role: str) -> str:
    """创建会话并返回 token；顺带清理已过期会话，防止内存无限增长。"""
    _cleanup_sessions()
    token = secrets.token_urlsafe(32)
    expires = time.time() + SESSION_TTL_SECONDS
    with _sessions_lock:
        _sessions[token] = {
            "user": username,
            "role": role,
            "expires": expires,
        }
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        db.execute(
            "INSERT OR REPLACE INTO sessions(token, user, role, expires) "
            "VALUES(?,?,?,?)", (token, username, role, expires),
        )
        db.commit(); db.close()
    except Exception:
        pass
    return token


def _get_session(token: str | None) -> dict | None:
    if not token:
        return None
    with _sessions_lock:
        session = _sessions.get(token)
        if not session:
            return None
        if time.time() > session.get("expires", 0):
            _sessions.pop(token, None)
            try:
                db = sqlite3.connect(DB_PATH, timeout=5)
                db.execute("DELETE FROM sessions WHERE token=?", (token,))
                db.commit(); db.close()
            except Exception:
                pass
            return None
        return session


def _delete_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def _cleanup_sessions() -> None:
    now = time.time()
    with _sessions_lock:
        for token in [t for t, s in _sessions.items() if now > s.get("expires", 0)]:
            _sessions.pop(token, None)

def _merge_progress_message(existing: dict, data: dict) -> None:
    """把一条 push_progress 消息合并进任务内存态（纯合并，无副作用）。

    T11：全局监听器与 SSE 连接共用同一合并逻辑，保证 /task/{id} 快照与
    SSE 推送的形态一致；DB 写入/分享等副作用只在全局监听器执行一次。
    """
    ptype = data.get("type")
    payload = data.get("payload")
    if ptype and isinstance(payload, dict):
        if ptype == "task_complete":
            existing["status"] = payload.get("status", existing.get("status", "PENDING"))
            existing["report"] = payload.get("report", existing.get("report", ""))
            existing["steps"] = payload.get("steps", existing.get("steps", []))
            # P0-1/P0-2：验收缺口摘要 + LLM 降级汇总随任务结果暴露
            existing["acceptance"] = payload.get("acceptance")
            existing["llm_degraded"] = payload.get("llm_degraded")
        elif ptype == "plan_update":
            # 合并而非替换：迭代/修复轮的步骤只带当轮，
            # 直接替换会让前端计划树"缩水"，看起来不按顺序
            new_steps = payload.get("steps", [])
            merged = {s.get("step_id"): s for s in existing.get("steps", [])}
            for s in new_steps:
                if s.get("step_id"):
                    merged[s["step_id"]] = s
            existing["steps"] = list(merged.values())
        elif ptype == "log":
            logs = existing.get("logs", [])
            # 幂等去重：SSE 连接的本地快照与全局监听器可能先后合并同一条
            # 消息（订阅与快照读取之间存在窗口），同内容末条重复时跳过
            entry = {
                "id": len(logs),
                "timestamp": payload.get("timestamp") or time.strftime("%H:%M:%S"),
                "agent": payload.get("agent", ""),
                "type": payload.get("type", "info"),
                "message": payload.get("message", ""),
            }
            if logs and (
                logs[-1].get("message") == entry["message"]
                and logs[-1].get("agent") == entry["agent"]
                and logs[-1].get("type") == entry["type"]
            ):
                pass
            else:
                logs.append(entry)
                existing["logs"] = logs
        elif ptype == "agent_status":
            existing["agent_status"] = payload
        elif ptype == "plan":
            existing["steps"] = payload.get("steps", existing.get("steps", []))
        # Keep status if not set
        if "status" not in existing:
            existing["status"] = "RUNNING"
    else:
        # Direct state update (e.g. final result)
        existing.update(data)


def _listen_results():
    while not _redis_ready():
        time.sleep(2)
    r = _new_redis()
    ps = r.pubsub(); ps.subscribe("orchestrator:response")
    for msg in ps.listen():
        if msg["type"] == "message":
            try:
                data = json.loads(msg["data"]); tid = data.get("task_id","")
                if tid:
                    with _task_lock:
                        existing = _task_results.get(tid, {})
                        ptype = data.get("type")
                        payload = data.get("payload")
                        _merge_progress_message(existing, data)
                        _task_results[tid] = existing
                        # 防止内存无限增长：最多保留最近 300 个任务
                        if len(_task_results) > 300:
                            for _k in list(_task_results)[:-200]:
                                _task_results.pop(_k, None)
                    if ptype == "task_complete":
                        # DB 写入与通知在锁外执行：这些是慢 I/O，
                        # 放锁内会阻塞所有 /task/{id} 读取与 SSE 快照
                        try:
                            db = sqlite3.connect(DB_PATH, timeout=5)
                            db.execute(
                                "INSERT INTO task_history(task_id,goal,status,report,steps_json,logs_json,completed_at)"
                                " VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)"
                                " ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
                                " report=excluded.report,steps_json=excluded.steps_json,"
                                " logs_json=excluded.logs_json,completed_at=CURRENT_TIMESTAMP",
                                (tid, existing.get("goal",""), (payload or {}).get("status","UNKNOWN"),
                                 (payload or {}).get("report",""),
                                 json.dumps(existing.get("steps", []), ensure_ascii=False),
                                 json.dumps(existing.get("logs", [])[-200:], ensure_ascii=False)),
                            )
                            db.commit(); db.close()
                        except Exception: pass
                        # D1：日报项目任务终态自动生成分享链接（落地页入口），
                        # 并补发一条带链接的通知（编排器通知早于分享生成，链接会缺失）
                        # T2：FAILED 任务不生成分享链接（空报告落地页伤信任），
                        # 只发失败通知 + 告警事件。失败通知按 task_id 全局去重——
                        # 调度器结果追踪（runner._on_failure）与本监听器都可能
                        # 对同一失败任务发通知，双通道只发一条
                        try:
                            if str(existing.get("project") or "") == "daily-report":
                                _status = str(existing.get("status") or "")
                                if _status == "FAILED":
                                    if _try_claim_failure_notify(tid):
                                        from notifications import notify_task_done
                                        notify_task_done(
                                            tid,
                                            goal=str(existing.get("goal") or ""),
                                            status="FAILED",
                                            summary="日报任务执行失败，未生成落地页；请检查健康页事件与编排器日志",
                                        )
                                    _publish_alert("daily_report_failed", f"日报任务失败：{tid}")
                                else:
                                    _generate_share_token(tid)
                                    from notifications import (
                                        find_share_link,
                                        make_summary,
                                        notify_task_done,
                                    )
                                    notify_task_done(
                                        tid,
                                        goal=str(existing.get("goal") or ""),
                                        status=_status,
                                        report_link=find_share_link(tid),
                                        summary=make_summary(
                                            str(existing.get("report") or "")
                                        ),
                                    )
                        except Exception:
                            pass
            except Exception: pass

def _listen_events():
    """订阅告警/进化/守护事件，供 Health 页真实展示。"""
    while not _redis_ready():
        time.sleep(2)
    r = _new_redis()
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
                # 自增序号做 id：列表裁剪后 len 会回弹到固定值，
                # 用 len 当 id 会导致所有新事件同 id（前端 key 冲突）
                global _evt_seq
                _evt_seq += 1
                _events.append({
                    "id": f"evt-{_evt_seq}",
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
    # 端点/余额预警单列一类，避免被兜底成 guardian 混淆告警语义
    if "llm" in t or "balance" in t or "endpoint" in t:
        return "llm"
    if "scale" in t or "guardian" in t:
        return "guardian"
    return "guardian"

def _system_status():
    try:
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
        agents = [dict(row) for row in db.execute("SELECT * FROM agents").fetchall()]
        for a in agents:
            a["last_heartbeat"] = _iso_utc(a.get("last_heartbeat"))
        queues = {}
        if _redis_ready():
            r = _new_redis()
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
        try:
            from llm_client import get_endpoint_health
            llm_health = get_endpoint_health()
        except Exception:
            llm_health = {}
        try:
            # A3：余额感知预检（30s TTL 缓存），llm_health.balance 供前端直接展示
            from llm_client import get_balance_status
            llm_health["balance"] = get_balance_status()
        except Exception:
            pass
        try:
            # 主备多样性校验：Health 页不提交任务也能看到同源单点风险
            from llm_client import (
                check_endpoint_diversity, endpoint_vendors,
            )
            _ok, _reason = check_endpoint_diversity()
            _vendors = endpoint_vendors()
            llm_health["diversity"] = {
                "ok": _ok,
                "reason": _reason,
                "primary_vendor": _vendors.get("primary") or "",
                "backup_vendor": _vendors.get("backup") or "",
            }
        except Exception:
            pass
        try:
            from llm_client import get_endpoint_warning
            llm_warning = get_endpoint_warning()
        except Exception:
            llm_warning = ""
        # Embedding 降级（额度耗尽/网络不通）：记忆与提示词经验检索随之失效，
        # 界面上的表现只是"记忆命中率 0%"，故并入 llm_warning 走既有横幅展示，
        # 并按冷却窗口发布一次告警事件（避免每 3 秒轮询刷屏）
        embedding_health = {}
        try:
            import embed_health as _embed_health
            embedding_health = _embed_health.embedding_health()
            _notice = _embed_health.degradation_notice()
            if _notice:
                llm_warning = f"{llm_warning}；{_notice}" if llm_warning else _notice
                _publish_embed_alert(_notice)
        except Exception:
            embedding_health = {}
        search_health = {}
        if _redis_ready():
            try:
                raw = _new_redis().get("search_engine_health")
                if raw:
                    search_health = json.loads(raw)
            except Exception:
                pass
        try:
            # 行情数据源健康（eastmoney/tencent/sina 熔断 + 冷却），进程内快照
            from adapters.source_health import get_health as get_source_health
            source_health = get_source_health()
        except Exception:
            source_health = {}
        try:
            # Roadmap 余项④：月度预算状态（超限降级可观测）
            from costs import get_budget_status
            budget = get_budget_status()
        except Exception:
            budget = {}
        return {
            "agents": agents,
            "llm_health": llm_health,
            "llm_warning": llm_warning,
            "embedding_health": embedding_health,
            "search_health": search_health,
            "source_health": source_health,
            "budget": budget,
            "queues": queues,
            "tasks": {"total": total, "success": success, "today": today},
            "memory": memory,
            "code_sandbox": _code_sandbox_status(),
            "recent": recent,
            "uptime_sec": int(time.time() - _START_TIME),
            "survival_rate": survival,
            "llm_usage": _get_llm_usage(),
        }
    except Exception:
        return {"agents":[],"queues":{},"tasks":{"total":0,"success":0,"today":0},
                "memory":{"conversations":0,"strategies":0},"recent":[],"uptime_sec":0,"survival_rate":100,
                "llm_usage":{"calls":0,"prompt_tokens":0,"completion_tokens":0}}

def _code_sandbox_status() -> dict:
    """沙箱状态快照（实际模式/判定来源/docker 可用性/镜像是否存在）。
    状态接口与健康检查共用，供部署者确认容器级隔离是否生效。"""
    try:
        from code_sandbox import sandbox_status
        return sandbox_status()
    except Exception:
        return {
            "mode": "restricted",
            "mode_explicit": None,
            "mode_env_raw": os.environ.get("CODE_EXECUTION_SANDBOX"),
            "mode_source": "unknown",
            "docker_available": False,
            "sandbox_image": None,
            "sandbox_image_exists": False,
        }

def _get_llm_usage():
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    if _redis_ready():
        try:
            r = _new_redis()
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
            # 非阻塞获取：若预热线程正在初始化，本请求直接返回 None（快速失败）
            if not _memory_manager_lock.acquire(blocking=False):
                return _memory_manager
            try:
                if _memory_manager is None:
                    from memory_manager import MemoryManager
                    _memory_manager = MemoryManager(os.environ.get("MEMORY_DIR", "./chroma_memory"))
            finally:
                _memory_manager_lock.release()
        return _memory_manager
    except Exception:
        return None

def _refresh_memory_stats() -> dict:
    global _mem_stats_cache
    mem = _get_memory_manager()
    data = {"conversations": 0, "strategies": 0}
    if mem is not None:
        try:
            data = mem.stats()
        except Exception:
            data = {"conversations": 0, "strategies": 0}
    with _mem_stats_lock:
        _mem_stats_cache["ts"] = time.time()
        _mem_stats_cache["data"] = data
    return data

def _get_memory_stats() -> dict:
    """TTL 缓存 + 后台刷新：Chroma 再慢也不阻塞 /api/status。"""
    with _mem_stats_lock:
        cached = _mem_stats_cache
        if time.time() - cached["ts"] < 60:
            return cached["data"]
    threading.Thread(target=_refresh_memory_stats, daemon=True).start()
    return cached["data"]

def _memory_health(mem, stats: dict) -> dict:
    """用已缓存的 stats 组装健康度，避免请求路径再次 count Chroma。

    兼容只接受无参 memory_health() 的测试桩（TypeError 时降级为无参调用）。
    """
    default = {
        "injections": 0, "hits": 0, "hit_rate": 0.0,
        "strategy_count": 0, "conversation_count": 0, "expired_purged": 0,
    }
    if mem is None:
        return default
    try:
        health = mem.memory_health(stats)
    except TypeError:
        health = mem.memory_health()
    if not isinstance(health, dict):
        return default
    return health

def _refresh_memory_data(mem) -> dict:
    """后台刷新 /api/memory 的列表数据（列表 get 不进请求路径）。"""
    global _memory_data_cache, _memory_data_refreshing
    try:
        stats = _get_memory_stats()
        data = {
            "stats": stats,
            "conversations": mem.list_conversations(50) if mem is not None else [],
            "strategies": mem.list_strategies(50) if mem is not None else [],
            "memory_health": _memory_health(mem, stats),
        }
        with _memory_data_lock:
            _memory_data_cache = {"ts": time.time(), "data": data}
        return data
    finally:
        with _memory_data_lock:
            _memory_data_refreshing = False

def _get_memory_data():
    """记忆列表：60s 缓存 + 后台刷新，避免 Chroma 慢操作阻塞页面。"""
    global _memory_data_cache, _memory_data_refreshing
    mem = _get_memory_manager()
    stats = _get_memory_stats()
    health = _memory_health(mem, stats)
    if mem is None:
        return {
            "stats": stats,
            "conversations": [],
            "strategies": [],
            "memory_health": health,
        }
    with _memory_data_lock:
        cached = _memory_data_cache
        if cached["data"] is not None and time.time() - cached["ts"] < 60:
            return cached["data"]
        if _memory_data_refreshing:
            # 已有后台刷新在跑：直接返回现有数据，避免重复全量 get
            return cached["data"] if cached["data"] is not None else {
                "stats": stats,
                "conversations": [],
                "strategies": [],
                "memory_health": health,
            }
        _memory_data_refreshing = True
    # 首次无缓存/缓存过期：返回现有数据，列表由后台线程补全（不阻塞请求）
    threading.Thread(target=_refresh_memory_data, args=(mem,), daemon=True).start()
    if cached["data"] is not None:
        return cached["data"]
    return {
        "stats": stats,
        "conversations": [],
        "strategies": [],
        "memory_health": health,
    }

def _build_summary_fallback(stats: dict) -> str:
    """纯本地兜底自述：不访问 Chroma/LLM，保证接口秒回。"""
    return (
        f"我是织光——一支运行在你本地的 AI 团队。我已完成了 {stats.get('conversations', 0)} 项任务、"
        f"沉淀了 {stats.get('strategies', 0)} 条成功策略。"
        "我可以帮你调研、分析数据、写报告，还会在任务后自我复盘与进化。"
    )

def _build_memory_summary(mem, stats: dict) -> tuple[str, str]:
    """读取真实记忆并生成自述（仅供后台线程调用，可接受慢操作）。"""
    try:
        convs = mem.list_conversations(30) or []
        strats = mem.list_strategies(30) or []
    except Exception:
        convs, strats = [], []
    goals = [str(c.get("metadata", {}).get("goal", ""))[:100] for c in convs if c.get("metadata", {}).get("goal")]
    topics = [str(s.get("metadata", {}).get("goal_keywords", ""))[:80] for s in strats if s.get("metadata", {}).get("goal_keywords")]
    sig = f"{stats.get('conversations')}:{stats.get('strategies')}"
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
        text = _build_summary_fallback(stats)
    return text, sig

def _start_memory_summary_refresh(mem, stats: dict) -> bool:
    """后台重新生成系统自述；已有一轮生成时不重复启动。"""
    global _memory_summary_generating
    with _memory_summary_lock:
        if _memory_summary_generating:
            return False
        _memory_summary_generating = True

    def _run():
        try:
            text, sig = _build_memory_summary(mem, stats)
            if text:
                with _memory_summary_lock:
                    global _memory_summary_cache
                    _memory_summary_cache = {
                        "text": text, "ts": time.time(), "signature": sig,
                    }
        finally:
            global _memory_summary_generating
            with _memory_summary_lock:
                _memory_summary_generating = False

    threading.Thread(target=_run, daemon=True).start()
    return True

def _get_memory_summary(refresh: bool = False) -> dict:
    """系统自述：默认请求优先返回 600s 缓存；无缓存时立即回兜底并后台生成。

    显式 ?refresh=1 才同步重新生成（用户主动要求新文案，可接受等待）；
    默认请求的 Chroma list/LLM 只出现在后台线程，接口本身不等待慢操作。
    """
    global _memory_summary_cache
    mem = _get_memory_manager()
    stats = _get_memory_stats()
    health = _memory_health(mem, stats)
    if mem is None:
        return {"summary": "", "cached": False, "error": "memory unavailable",
                "memory_health": health}
    sig = f"{stats.get('conversations')}:{stats.get('strategies')}"
    now = time.time()
    with _memory_summary_lock:
        cached = dict(_memory_summary_cache)
        cache_fresh = (
            bool(cached.get("text"))
            and sig == cached.get("signature")
            and now - cached.get("ts", 0) < 600
        )
        if not refresh and cache_fresh:
            return {"summary": cached["text"], "cached": True,
                    "memory_health": health}
    if refresh:
        # 显式刷新：同步生成，成功后写回缓存（允许慢）
        text, sig = _build_memory_summary(mem, stats)
        with _memory_summary_lock:
            _memory_summary_cache = {
                "text": text, "ts": time.time(), "signature": sig,
            }
        return {"summary": text, "cached": False, "memory_health": health}
    # 默认冷启动：立即返回兜底，后台生成新文案
    _start_memory_summary_refresh(mem, stats)
    return {
        "summary": _build_summary_fallback(stats), "cached": False,
        "memory_health": health, "generating": True,
    }

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

def _safe_project_path(rel: str, tid: str | None = None) -> str | None:
    """把相对路径限定在（任务的）project 工作区内，防止路径穿越。"""
    base = os.path.abspath(
        str((task_workspace(tid) / "project") if tid else PROJECT_DIR)
    )
    # 反斜杠统一为正斜杠：Windows 上防 "..\\" 穿越，Linux/macOS 上反斜杠
    # 是合法文件名字符，不归一化会绕过逃逸检测
    normalized = str(rel).replace("\\", "/")
    p = os.path.abspath(os.path.join(base, normalized))
    if p != base and not p.startswith(base + os.sep):
        return None
    return p


def _safe_workspace_path(rel: str, tid: str) -> str | None:
    """把相对路径限定在任务工作区根目录（charts/data/reports 等），防路径穿越。"""
    base = os.path.abspath(str(task_workspace(tid)))
    normalized = str(rel).replace("\\", "/")
    p = os.path.abspath(os.path.join(base, normalized))
    if p != base and not p.startswith(base + os.sep):
        return None
    return p

def _task_deliverables(tid: str) -> list[dict]:
    """从该任务 package 步骤的 zip 产物列出交付文件（名称/大小/类型）。"""
    def _zip_entries(zip_path: str) -> list[dict]:
        out: list[dict] = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if "_check_" in name or name.startswith("__pycache__"):
                        continue
                    ext = os.path.splitext(name)[1].lower().lstrip(".")
                    if ext == "html":
                        kind = "html"
                    elif ext == "py":
                        kind = "py"
                    elif ext in ("md", "markdown"):
                        kind = "md"
                    elif ext:
                        kind = ext
                    else:
                        kind = "file"
                    out.append({"name": name, "size": info.file_size, "kind": kind})
        except Exception:
            pass
        return out

    files: list[dict] = []
    zip_path = None
    with _task_lock:
        data = _task_results.get(tid)
    steps = (data or {}).get("steps") or []
    for s in steps:
        res = s.get("result") or {}
        text = str(res.get("result") or "")
        m = re.search(r"Download: file://([^\s]+)", text)
        if m:
            zp = m.group(1).strip()
            if os.path.exists(zp):
                zip_path = zp  # 取最后一个（修复轮的最终交付包）
    if zip_path:
        files = _zip_entries(zip_path)
    if not files:
        # 兜底 1：仅对成功任务，步骤不在内存时（服务重启后）取最新打包产物；
        # 失败任务显示"最新 zip"会把别的任务的旧文件误挂到本任务上。
        try:
            db = sqlite3.connect(DB_PATH, timeout=5)
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT status FROM task_history WHERE task_id=?", (tid,),
            ).fetchone()
            db.close()
            ok_status = bool(row and row["status"] == "SUCCESS")
        except Exception:
            ok_status = False
        if ok_status:
            # 优先该任务自己的成果文件夹，其次旧版共享打包目录
            candidates: list[str] = []
            task_ws = task_workspace(tid)
            try:
                candidates += [
                    str(p) for p in task_ws.glob("*.zip")
                ]
            except Exception:
                pass
            if not candidates:
                pkg_dir = os.path.join(tempfile.gettempdir(), "agent_packages")
                try:
                    candidates = [
                        os.path.join(pkg_dir, n) for n in os.listdir(pkg_dir)
                        if n.endswith(".zip")
                    ]
                except Exception:
                    pass
            if candidates:
                files = _zip_entries(max(candidates, key=os.path.getmtime))
    if not files:
        # 兜底 2：该任务 project 目录最近窗口内的产物
        cutoff = time.time() - 120 * 60
        proj_dir = str(task_workspace(tid) / "project")
        for root, _, names in os.walk(proj_dir):
            for n in names:
                p = os.path.join(root, n)
                try:
                    if os.path.getmtime(p) < cutoff:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(p, proj_dir).replace("\\", "/")
                if "_check_" in rel or rel.startswith("__pycache__"):
                    continue
                ext = os.path.splitext(n)[1].lower().lstrip(".")
                if ext == "html":
                    kind = "html"
                elif ext == "py":
                    kind = "py"
                elif ext in ("md", "markdown"):
                    kind = "md"
                elif ext:
                    kind = ext
                else:
                    kind = "file"
                files.append({"name": rel, "size": os.path.getsize(p), "kind": kind})
        files.sort(key=lambda x: x["name"])
    return files

def _clean_report_text(report: str) -> str:
    """报告文本清洗：剥离 LLM 常见的整体 Markdown 围栏（前端/分享页结构化显示）。"""
    try:
        from common import strip_outer_markdown_fence
        return strip_outer_markdown_fence(str(report or ""))
    except Exception:
        return str(report or "")


def _get_task_report_data(tid: str) -> dict | None:
    """取任务的分享数据（报告正文/目标/状态/时间）：优先内存结果，其次 SQLite。
    服务重启后 _task_results 为空，仍可从 agents.db 的 task_history.report 恢复。"""
    with _task_lock:
        data = _task_results.get(tid)
    if data:
        report = str(data.get("final_report") or data.get("report") or "")
        if report.strip():
            return {
                "task_id": tid,
                "goal": str(data.get("goal") or ""),
                "status": str(data.get("status") or ""),
                "report": _clean_report_text(report),
                "created_at": str(data.get("created_at") or ""),
            }
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT task_id, goal, status, report, created_at, completed_at "
            "FROM task_history WHERE task_id=?", (tid,),
        ).fetchone()
        db.close()
        if row and str(row["report"] or "").strip():
            out = dict(row)
            out["report"] = _clean_report_text(out.get("report") or "")
            return out
    except Exception:
        pass
    return None

def _share_image_src(src: str, tid: str) -> str:
    """把报告图片链接指向可复用的 /files/<task_id>/ 静态路由（无需复制图片）。
    报告链接通常已被 _rewrite_report_links 改写成 /files/<task_id>/...，
    这里再兜底处理相对路径与旧版绝对工作区路径。"""
    src = str(src).strip()
    low = src.lower()
    if low.startswith(("/files/", "http://", "https://", "data:image/")):
        return src
    if tid:
        try:
            ws = str(task_workspace(tid)).replace("\\", "/")
            if src.replace("\\", "/").startswith(ws):
                rel = src.replace("\\", "/")[len(ws):].lstrip("/")
                return f"/files/{tid}/{rel}"
        except Exception:
            pass
        rel = src.lstrip("./").replace("\\", "/")
        return f"/files/{tid}/{rel}"
    return src

def _safe_href(url: str, image: bool = False) -> str:
    """只放行 http(s)/mailto/相对路径/data 图片等安全协议，杜绝 javascript: 等注入。"""
    url = str(url).strip()
    low = url.lower()
    allowed = ("http://", "https://", "mailto:", "#", "/")
    if image:
        allowed += ("data:image/",)
    if low.startswith(allowed) or url.startswith(("./", "../")):
        return url
    return "#"


# ---------------------------------------------------------------------------
# 分享页结构化渲染（与前端 ReportViewer 解析逻辑等价的服务端实现）：
# 数据时效卡 / 目录大纲 / 参考来源卡片 / 免责声明弱化
# ---------------------------------------------------------------------------

def _report_fence_mask(lines: list) -> list[bool]:
    """标记围栏代码块内的行（避免把代码内容误当标题/来源解析）。"""
    mask: list[bool] = []
    in_fence = False
    for ln in lines:
        if re.match(r"^\s*(```|~~~)", ln):
            in_fence = not in_fence
            mask.append(True)
        else:
            mask.append(in_fence)
    return mask


def _plain_heading(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)\s]+\)", r"\1", str(text)).replace("*", "").replace("_", "").strip()


def _report_parse_toc(md: str) -> list[dict]:
    """提取 #/##/### 目录大纲（跳过代码块内伪标题）。"""
    lines = str(md).split("\n")
    mask = _report_fence_mask(lines)
    toc: list[dict] = []
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", ln)
        if not m:
            continue
        text = _plain_heading(m.group(2))
        if text:
            toc.append({"text": text[:60], "level": len(m.group(1))})
    return toc


def _report_parse_freshness(md: str) -> str | None:
    """提取"数据时效"区块内容（标题区块或含数据截止/快照时间的段落）。"""
    lines = str(md).split("\n")
    mask = _report_fence_mask(lines)
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,6})\s*(.+)$", ln.strip())
        if m and re.search(r"数据时效|数据截止时间", m.group(2)):
            section = []
            for j in range(i + 1, len(lines)):
                if not mask[j] and re.match(r"^#{1,6}\s+", lines[j]):
                    break
                t = lines[j].strip()
                if t and not re.match(r"^\s*(```|~~~)", t):
                    section.append(re.sub(r"^[-*•]\s+", "", t))
            text = re.sub(r"\s+", " ", " ".join(section)).replace("*", "").replace("_", "").strip()
            return text or None
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        if re.search(r"数据截至|数据截止|快照时间", ln):
            text = re.sub(r"^#{1,6}\s*", "", ln)
            text = re.sub(r"^[-*•]\s+", "", text).replace("*", "").replace("_", "").strip()
            if text:
                return text
    return None


_SOURCE_HEADING_WORDS = ("参考来源", "数据来源", "参考资料", "引用来源", "参考文献")


def _report_is_source_heading(text: str) -> bool:
    t = str(text).strip()
    return any(w in t for w in _SOURCE_HEADING_WORDS) or bool(re.match(r"^(来源|References?|Sources?)$", t, re.I))


def _report_parse_sources(md: str) -> tuple[str, list[dict], str | None]:
    """提取文末参考来源区块：[标题](URL) 列表。返回 (剔除来源区块后的正文, 来源列表, 原区块文本)。
    无法结构化时原样保留（来源列表为空、原区块文本非空）。"""
    lines = str(md).split("\n")
    mask = _report_fence_mask(lines)
    start = -1
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,6})\s*(.+)$", ln.strip())
        if m and _report_is_source_heading(m.group(2)):
            start = i
            break
    if start < 0:
        return str(md), [], None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not mask[j] and re.match(r"^#{1,6}\s+", lines[j]):
            end = j
            break
    raw_section = "\n".join(lines[start + 1:end])
    items: list[dict] = []
    for k in range(start + 1, end):
        if mask[k]:
            continue
        trimmed = lines[k].strip()
        if not trimmed or trimmed.startswith("!["):
            continue
        m = re.search(r"\[([^\]]+)\]\(([^)\s]+)\)", trimmed)
        if not m:
            continue
        prefix = trimmed[:m.start()].strip()
        if prefix and not re.match(r"^(?:[-*•]\s*|\[\d{1,2}\]\s*|\d{1,3}[.、.)]\s*)+$", prefix):
            continue
        title = m.group(1).replace("*", "").replace("_", "").strip()
        url = m.group(2).strip()
        if not title or not url:
            continue
        try:
            domain = re.sub(r"^www\.", "", (url.split("//", 1)[-1] if "//" in url else url).split("/")[0])
        except Exception:
            domain = url
        items.append({"title": title[:80], "url": url, "domain": domain[:60]})
    if not items:
        return str(md), [], raw_section
    rest = "\n".join(lines[:start] + lines[end:]).strip()
    return rest, items, None


def _report_parse_disclaimer(md: str) -> tuple[str, str | None]:
    """提取免责声明区块文本。返回 (剔除后的正文, 免责文本)。"""
    lines = str(md).split("\n")
    mask = _report_fence_mask(lines)
    start = -1
    for i, ln in enumerate(lines):
        if mask[i]:
            continue
        m = re.match(r"^(#{1,6})\s*(.+)$", ln.strip())
        if m and "免责声明" in m.group(2):
            start = i
            break
    if start < 0:
        return str(md), None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not mask[j] and re.match(r"^#{1,6}\s+", lines[j]):
            end = j
            break
    text = re.sub(r"\[([^\]]+)\]\([^)\s]+\)", r"\1", " ".join(lines[start + 1:end]))
    text = re.sub(r"\s+", " ", text).replace("*", "").replace("_", "").strip()
    if not text:
        return str(md), None
    rest = "\n".join(lines[:start] + lines[end:]).strip()
    return rest, text


def _share_page_structured(md: str, task_id: str = "") -> dict:
    """解析报告结构化元素，供分享页渲染（与前端 ReportViewer 等价）。

    D2：追加 top_stats（首张表格前 4 行 → 结论卡片）与 traceability
    （来源数 + 验收可溯源率，来自任务工作区 acceptance_report.json）。"""
    try:
        from common import strip_outer_markdown_fence
        md = strip_outer_markdown_fence(str(md or ""))
    except Exception:
        md = str(md or "")
    body, sources, _sec = _report_parse_sources(md)
    body, disclaimer = _report_parse_disclaimer(body)
    # D2：首张 Markdown 表格的前 4 行（指标/数值/来源列）→ TOP 结论卡片
    top_stats: list[dict] = []
    rows: list[list[str]] = []
    header: list[str] = []
    for line in str(md).split("\n"):
        t = line.strip()
        if not t.startswith("|"):
            if header:
                break
            continue
        cells = [c.strip() for c in t.strip("|").split("|")]
        if all(not c or set(c) <= {"-", ":", "—"} for c in cells):
            continue
        if not header:
            header = cells
            continue
        rows.append(cells)
        if len(rows) >= 4:
            break
    if header and rows:
        k_i = next((i for i, h in enumerate(header) if "指标" in h or "数值" in h), 0)
        v_i = next((i for i, h in enumerate(header) if "数值" in h), min(1, len(header) - 1))
        for r in rows:
            if len(r) > v_i and str(r[v_i]).strip():
                top_stats.append({
                    "k": str(r[k_i])[:24] if len(r) > k_i else "",
                    "v": str(r[v_i])[:32],
                })
    traceability: dict = {}
    if task_id:
        try:
            acc_path = task_workspace(task_id) / "acceptance_report.json"
            if acc_path.exists():
                acc = json.loads(acc_path.read_text(encoding="utf-8"))
                ntc = (acc.get("checks") or {}).get("number_traceability") or {}
                if ntc:
                    traceability = {
                        "total": int(ntc.get("total_count") or 0),
                        "traced": int(ntc.get("traceable_count") or 0),
                        "computed": int(ntc.get("computed_count") or 0),
                        "disclosed": int(ntc.get("disclosed_count") or 0),
                        "overall": str(acc.get("overall") or ""),
                    }
        except Exception:
            pass
    return {
        "freshness": _report_parse_freshness(md),
        "toc": _report_parse_toc(body),
        "sources": sources,
        "disclaimer": disclaimer,
        "body": body,
        "top_stats": top_stats,
        "traceability": traceability,
    }


def _markdown_to_html(md: str, task_id: str) -> str:
    """极简 Markdown → 安全 HTML（用于公开分享页）。
    先整体转义再构建标签，报告内容无法注入脚本；
    支持标题/列表/表格/代码块/图片/链接/引用/分割线等报告常用语法。"""
    def _inline(text: str) -> str:
        text = html.escape(str(text), quote=False)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        def _img(m):
            alt = m.group(1)
            src = _safe_href(_share_image_src(m.group(2), task_id), image=True)
            return (
                f'<img src="{html.escape(src, quote=True)}" '
                f'alt="{html.escape(alt, quote=True)}" loading="lazy" />'
            )
        text = re.sub(
            r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)", _img, text,
        )
        def _link(m):
            href = _safe_href(m.group(2))
            label = m.group(1)
            return f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">{label}</a>'
        text = re.sub(
            r"\[([^\]]+)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)", _link, text,
        )
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)
        return text

    if not md:
        return "<p>（暂无报告内容）</p>"
    lines = str(md).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def _is_table_sep(line: str) -> bool:
        s = line.strip()
        if not s.startswith("|"):
            return False
        return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", s)) and "-" in s

    while i < n:
        line = lines[i]
        stripped = line.strip()
        # 围栏代码块
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏
            code = html.escape("\n".join(buf))
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            continue
        # GFM 表格
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            rows = [lines[i]]
            i += 1
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            def _cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            thead = "".join(f"<th>{_inline(c)}</th>" for c in _cells(rows[0]))
            tbody = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(r)) + "</tr>"
                for r in rows[2:]
            )
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue
        # 标题
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            level = len(hm.group(1))
            out.append(f"<h{level}>{_inline(hm.group(2))}</h{level}>")
            i += 1
            continue
        # 分割线
        if re.fullmatch(r"(\*\*\*|---|___)\s*", stripped):
            out.append("<hr />")
            i += 1
            continue
        # 引用块
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + "<br/>".join(_inline(x) for x in buf) + "</blockquote>")
            continue
        # 无序/有序列表（连续行合并成一个 <ul>/<ol>）
        ul_m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        ol_m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if ul_m or ol_m:
            ordered = bool(ol_m)
            pat = r"^\s*\d+[.)]\s+(.*)$" if ordered else r"^\s*[-*+]\s+(.*)$"
            buf = []
            while i < n:
                m = re.match(pat, lines[i])
                if not m:
                    break
                buf.append(f"<li>{_inline(m.group(1))}</li>")
                i += 1
            out.append(f"<{'ol' if ordered else 'ul'}>{''.join(buf)}</{'ol' if ordered else 'ul'}>")
            continue
        # 空行跳过
        if not stripped:
            i += 1
            continue
        # 普通段落
        buf = []
        while i < n:
            s = lines[i].strip()
            if not s:
                break
            if (
                s.startswith(("```", "#", ">", "- ", "* ", "+ "))
                or re.match(r"^\s*\d+[.)]\s+", lines[i])
                or re.fullmatch(r"(\*\*\*|---|___)\s*", s)
                or (s.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]))
            ):
                break
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br/>".join(_inline(x) for x in buf) + "</p>")
    return "\n".join(out)

_SHARE_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>分享不存在</title>
<style>
  body { margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f4f5f7; color: #1f2430; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #fff; border: 1px solid #e6e8ee; border-radius: 12px; padding: 44px 56px; text-align: center; }
  h1 { font-size: 20px; margin: 0 0 8px; color: #16213e; }
  p { color: #7a8291; font-size: 14px; margin: 0; }
</style>
</head>
<body>
<div class="card"><h1>分享链接不存在</h1><p>该链接可能已失效或被撤销。</p></div>
</body>
</html>"""

_SHARE_PASSWORD_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>分享访问验证</title>
<style>
  body {{ margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f4f5f7; color: #1f2430; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #fff; border: 1px solid #e6e8ee; border-radius: 12px; padding: 36px 44px; width: 360px;
          box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06); }}
  h1 {{ font-size: 19px; margin: 0 0 8px; color: #16213e; }}
  p {{ color: #7a8291; font-size: 13px; margin: 0 0 18px; line-height: 1.7; }}
  input {{ width: 100%; box-sizing: border-box; border: 1px solid #d8dce4; border-radius: 8px;
          padding: 10px 12px; font-size: 14px; outline: none; }}
  input:focus {{ border-color: #1f6feb; box-shadow: 0 0 0 3px rgba(31,111,235,0.12); }}
  button {{ width: 100%; margin-top: 14px; border: none; border-radius: 8px; background: #1f6feb; color: #fff;
           font-size: 14px; padding: 10px 0; cursor: pointer; }}
  button:hover {{ background: #1857c0; }}
  .error {{ background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; border-radius: 8px;
           padding: 8px 12px; font-size: 13px; margin-bottom: 12px; }}
</style>
</head>
<body>
<div class="card">
  <h1>该分享需要密码</h1>
  <p>此报告已开启访问密码保护，请输入分享者提供的密码后查看。</p>
  {error_html}
  <form method="post" action="/share/{token}/auth">
    <input type="password" name="password" placeholder="请输入访问密码" autofocus required />
    <button type="submit">验证并查看</button>
  </form>
</div>
</body>
</html>"""


def _share_cookie_name(token: str) -> str:
    """分享访问放行 Cookie 名：share_<token>（token 只含 URL 安全字符）。"""
    return f"share_{token}"


def _share_cookie_ok(headers, token: str) -> bool:
    """请求头 Cookie 中是否已有该分享的放行标记。"""
    cookie = headers.get("Cookie") if hasattr(headers, "get") else None
    parts = [x.strip() for x in str(cookie or "").split(";")]
    return _share_cookie_name(token) + "=ok" in parts


def _share_access_ok(headers, token: str) -> bool:
    """分享是否允许当前请求访问：
    - 无密码 → 直接放行（兼容旧行为）；
    - 有密码 → 必须有对应 Cookie（验证通过后由服务端下发）。"""
    try:
        info = _load_shares().get(token)
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    if not info.get("password_hash"):
        return True
    return _share_cookie_ok(headers, token)


def _share_password_page(token: str, error: str = "") -> str:
    """密码输入页：简单 HTML 表单，POST 到 /share/<token>/auth。"""
    error_html = (
        f'<div class="error">{html.escape(error)}</div>' if error else ""
    )
    return _SHARE_PASSWORD_PAGE_HTML.format(token=token, error_html=error_html)


def _build_lang_context(report_lang: str, user_context: str) -> str:
    """Roadmap 余项⑤：报告语言要求上下文。
    zh/空 → 原样返回；其他语言 → 头部注入语言指令（数据保持原样）。"""
    lang = str(report_lang or "").strip()[:10]
    if not lang or lang.lower() == "zh":
        return str(user_context or "")
    lang_ctx = (
        f"【报告语言要求】请用 {lang} 撰写最终报告（标题、正文、"
        f"表格、图表标题与结论全部使用该语言；数据与数字保持原样）。"
    )
    base = str(user_context or "").strip()
    return (lang_ctx + "\n\n" + base).strip() if base else lang_ctx


def _share_page_html(title: str, created_at: str, body_html: str,
                     theme: str = "light", structured: dict | None = None,
                     download_url: str = "") -> str:
    """生成公开只读分享页：自包含 HTML，无系统导航/管理功能。
    theme 支持 light / dark / paper（Roadmap 余项⑤：HTML 模板定制）；
    structured 传入 _share_page_structured() 结果，渲染数据时效卡/目录/来源卡片/免责声明。
    D2：追加 TOP 结论卡片、数据溯源区块与可选的 CSV 下载按钮。"""
    theme = str(theme or "light").strip().lower()
    if theme not in ("light", "dark", "paper"):
        theme = "light"
    st = structured or {}
    time_text = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(
                _iso_utc(str(created_at)).replace("Z", "+00:00"),
            )
            time_text = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_text = str(created_at)[:16]
    meta_html = (
        f'<div class="meta">生成时间：{html.escape(time_text)}</div>'
        if time_text else ""
    )
    theme_css = {
        "light": """
  :root {{ color-scheme: light; }}
  body {{ background: #f4f5f7; color: #1f2430; }}
  h1.title {{ color: #16213e; }}
  main {{ background: #fff; border: 1px solid #e6e8ee; }}
  h1, h2, h3, h4 {{ color: #16213e; }}
  a {{ color: #1f6feb; }}
  code {{ background: #f0f2f5; }}
  th {{ background: #f0f2f5; }}
  th, td {{ border-color: #dfe3ea; }}""",
        "dark": """
  :root {{ color-scheme: dark; }}
  body {{ background: #10151f; color: #d5dbe6; }}
  h1.title {{ color: #eef2f8; }}
  .meta {{ color: #8b93a3; }}
  main {{ background: #1a2230; border: 1px solid #2a3345; }}
  h1, h2, h3, h4 {{ color: #eef2f8; }}
  h2 {{ border-bottom-color: #2a3345; }}
  a {{ color: #6ea8ff; }}
  code {{ background: #232d3f; }}
  pre {{ background: #0b0f16; color: #c9d4e5; }}
  blockquote {{ border-left-color: #3a465c; color: #9aa4b6; }}
  th {{ background: #232d3f; }}
  th, td {{ border-color: #2e394e; }}
  hr {{ border-top-color: #2a3345; }}""",
        "paper": """
  :root {{ color-scheme: light; }}
  body {{ background: #ece7dc; color: #3a342b; font-family: Georgia, "Noto Serif SC", "Songti SC", serif; }}
  h1.title {{ color: #2c2620; font-family: Georgia, "Noto Serif SC", "Songti SC", serif; }}
  main {{ background: #fbf8f1; border: 1px solid #d8d0bd; box-shadow: 0 2px 14px rgba(60, 50, 30, .08); }}
  h1, h2, h3, h4 {{ color: #2c2620; }}
  a {{ color: #8a5a2b; }}
  code {{ background: #efe9db; }}
  th {{ background: #efe9db; }}
  th, td {{ border-color: #d8d0bd; }}
  hr {{ border-top-color: #d8d0bd; }}""",
    }[theme]
    # 结构化区块（数据时效卡 / 目录 / 来源卡片 / 免责声明）
    structured_html = ""
    parts: list[str] = []
    # D2：TOP 结论卡片（首表前 4 行）
    top_stats = st.get("top_stats") or []
    if top_stats:
        cards = "".join(
            '<div class="stat-card"><div class="stat-k">'
            f"{html.escape(s.get('k', ''))}</div>"
            '<div class="stat-v">'
            f"{html.escape(s.get('v', ''))}</div></div>"
            for s in top_stats
        )
        parts.append(f'<div class="stats-row">{cards}</div>')
    # D2：数据溯源区块（来源数 / 可溯源率 / CSV 下载）
    trace = st.get("traceability") or {}
    sources_n = len(st.get("sources") or [])
    trace_lines: list[str] = []
    if sources_n:
        trace_lines.append(f"参考来源 <b>{sources_n}</b> 个")
    if trace.get("total"):
        rate = round(100.0 * trace["traced"] / trace["total"]) if trace["total"] else 0
        trace_lines.append(f"数字可溯源 <b>{trace['traced']}/{trace['total']}</b>（{rate}%）")
        if trace.get("computed"):
            trace_lines.append(f"计算值 <b>{trace['computed']}</b> 个")
        if trace.get("disclosed"):
            trace_lines.append(f"模型知识已标注 <b>{trace['disclosed']}</b> 处")
    dl_html = ""
    if download_url:
        dl_html = (
            '<a class="dl-btn" href="'
            f'{html.escape(download_url, quote=True)}'
            '" download>⬇ 下载排名数据 CSV</a>'
        )
    if trace_lines or dl_html:
        parts.append(
            '<div class="trace-card"><div class="trace-title">数据溯源</div>'
            '<div class="trace-body">' + " · ".join(trace_lines) + "</div>"
            + dl_html + "</div>"
        )
    freshness = st.get("freshness")
    if freshness:
        parts.append(
            '<div class="freshness-card">'
            '<div class="freshness-title">数据时效</div>'
            f'<div class="freshness-text">{html.escape(str(freshness))}</div>'
            "</div>"
        )
    toc = st.get("toc") or []
    if len(toc) >= 2:
        items = "".join(
            f'<a class="toc-item toc-lv{int(e.get("level", 2))}" href="#sec-{i}">'
            f"{html.escape(str(e.get('text', '')))}</a>"
            for i, e in enumerate(toc[:30])
        )
        parts.append(f'<div class="toc-card"><div class="toc-title">目录</div><div class="toc-body">{items}</div></div>')
    sources = st.get("sources") or []
    if sources:
        items = "".join(
            f'<a class="src-item" href="{html.escape(_safe_href(str(s.get("url", ""))), quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">'
            f'<span class="src-idx">{i + 1}</span>'
            f'<span class="src-main"><span class="src-title">{html.escape(str(s.get("title", "")))}</span>'
            f'<span class="src-domain">{html.escape(str(s.get("domain", "")))}</span></span>'
            f'<span class="src-ext">↗</span></a>'
            for i, s in enumerate(sources[:50])
        )
        parts.append(f'<div class="src-card"><div class="src-title">参考来源</div><div class="src-body">{items}</div></div>')
    disclaimer = st.get("disclaimer")
    if disclaimer:
        parts.append(
            f'<div class="disc-card">{html.escape(str(disclaimer))}</div>'
        )
    if parts:
        structured_html = '<div class="structured">' + "".join(parts) + "</div>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{html.escape(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         line-height: 1.75; }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 64px; }}
  header {{ border-bottom: 1px solid #e3e6eb; padding-bottom: 18px; margin-bottom: 26px; }}
  h1.title {{ font-size: 24px; margin: 0 0 8px; line-height: 1.4; }}
  .meta {{ font-size: 13px; color: #7a8291; }}
  main {{ border-radius: 12px; padding: 28px 32px; }}
  h1, h2, h3, h4 {{ line-height: 1.4; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size: 19px; border-bottom: 1px solid #eef0f4; padding-bottom: 6px; }}
  h3 {{ font-size: 16px; }} h4 {{ font-size: 15px; }}
  img {{ max-width: 100%; height: auto; border-radius: 8px; margin: 12px 0; }}
  a {{ text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ padding: 2px 6px; border-radius: 4px; font-size: 90%; }}
  pre {{ padding: 16px; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: transparent; padding: 0; color: inherit; }}
  blockquote {{ margin: 12px 0; padding: 4px 16px; border-left: 3px solid #c9d4e5; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
  th, td {{ border: 1px solid #dfe3ea; padding: 8px 10px; text-align: left; }}
  th {{ font-weight: 600; }}
  hr {{ border: none; border-top: 1px solid #e3e6eb; margin: 20px 0; }}
  ul, ol {{ padding-left: 24px; }}
  footer {{ text-align: center; color: #9aa2b1; font-size: 12px; margin-top: 24px; }}
  /* 结构化区块样式 */
  .structured {{ display: flex; flex-direction: column; gap: 12px; margin-bottom: 18px; }}
  .freshness-card {{ border: 1px solid #e2b93b44; background: #f6edda66; border-radius: 10px; padding: 12px 16px; }}
  .freshness-title {{ font-weight: 600; font-size: 12px; color: #9a7b1e; margin-bottom: 4px; }}
  .freshness-text {{ font-size: 13px; color: #5a5340; }}
  .toc-card {{ border: 1px solid #c9d4e5; border-radius: 10px; padding: 12px 16px; background: #f8fafc; }}
  .toc-title {{ font-weight: 600; font-size: 12px; color: #64748b; margin-bottom: 6px; }}
  .toc-body {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .toc-item {{ font-size: 12px; color: #475569; background: #eef2f7; border-radius: 6px; padding: 3px 8px; text-decoration: none; }}
  .toc-item:hover {{ background: #dbeafe; color: #1d4ed8; }}
  .src-card {{ border: 1px solid #c9d4e5; border-radius: 10px; padding: 12px 16px; background: #f8fafc; }}
  .src-card > .src-title {{ font-weight: 600; font-size: 12px; color: #64748b; margin-bottom: 8px; }}
  .src-body {{ display: flex; flex-direction: column; gap: 6px; }}
  .src-item {{ display: flex; align-items: center; gap: 10px; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 10px; text-decoration: none; background: #fff; }}
  .src-item:hover {{ border-color: #93c5fd; }}
  .src-idx {{ flex: none; width: 20px; height: 20px; border-radius: 6px; background: #dbeafe; color: #1d4ed8; font-size: 11px; font-weight: 600; display: flex; align-items: center; justify-content: center; }}
  .src-main {{ flex: 1; min-width: 0; }}
  .src-title {{ display: block; font-size: 13px; color: #1e293b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .src-domain {{ display: block; font-size: 11px; color: #94a3b8; }}
  .src-ext {{ flex: none; color: #94a3b8; }}
  .disc-card {{ border: 1px solid #e2e8f0; border-radius: 10px; padding: 10px 14px; font-size: 12px; color: #94a3b8; background: #f8fafc; }}
  .stats-row {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .stat-card {{ flex: 1 1 150px; border: 1px solid #c9d4e5; border-radius: 10px; padding: 12px 14px; background: #f8fafc; }}
  .stat-k {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
  .stat-v {{ font-size: 18px; font-weight: 700; color: #16213e; }}
  .trace-card {{ border: 1px solid #c9d4e5; border-radius: 10px; padding: 12px 16px; background: #f8fafc; }}
  .trace-title {{ font-weight: 600; font-size: 12px; color: #64748b; margin-bottom: 4px; }}
  .trace-body {{ font-size: 13px; color: #475569; margin-bottom: 8px; }}
  .dl-btn {{ display: inline-block; font-size: 13px; color: #1d4ed8; background: #dbeafe; border-radius: 8px; padding: 6px 12px; text-decoration: none; }}
  .dl-btn:hover {{ background: #bfdbfe; }}
{theme_css}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 class="title">{html.escape(title)}</h1>
    {meta_html}
  </header>
  {structured_html}
  <main>{body_html}</main>
  <footer>由织光 WeaveMind 生成的公开只读报告</footer>
</div>
</body>
</html>"""

def _find_cached_task(goal: str, ttl_min: int, project: str = "", user: str = "", context: str = ""):
    """结果缓存：相同目标+项目+提交者+上下文在 TTL 内有过 SUCCESS，直接返回旧结果。
    P1-3：缓存键必须包含 project/user/context，否则不同项目或不同用户会互相命中缓存。"""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5); db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT task_id, report, conversation_id FROM task_history "
            "WHERE goal=? AND status='SUCCESS' AND completed_at IS NOT NULL "
            "AND completed_at >= datetime('now', ?)"
            " AND COALESCE(project,'')=? AND COALESCE(user,'')=? AND COALESCE(context,'')=?"
            " ORDER BY completed_at DESC LIMIT 1",
            (goal, f"-{ttl_min} minutes", project, user, context),
        ).fetchone()
        db.close()
        return dict(row) if row else None
    except Exception:
        return None

def _extract_text_from_bytes(filename: str, data: bytes) -> str:
    """从常见文件格式提取纯文本（txt/md/csv/json/pdf/docx/xlsx 等）。"""
    ext = os.path.splitext(filename)[1].lower()
    text = ""
    if ext in (".txt", ".md", ".csv", ".json", ".log", ".py", ".yaml", ".yml",
               ".xml", ".html", ".js", ".ts", ".ini", ".conf"):
        for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, ValueError):
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as exc:
            return f"(docx 解析失败: {exc})"
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages[:20]:
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(t)
            return "\n".join(pages)
        except Exception as exc:
            return f"(pdf 解析失败: {exc})"
    if ext in (".xlsx", ".xls"):
        try:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(data))
            return df.head(300).to_string(index=False)
        except Exception as exc:
            return f"(excel 解析失败: {exc})"
    return f"(暂不支持的文件格式: {ext or '未知'})"


def _publish_task(
    goal: str,
    project: str = "default",
    conversation_id: str = "",
    parent_task_id: str = "",
    context: str = "",
    auto_run: bool = True,
    template_steps: list | None = None,
    user_id: str = "",
    prefix: str = "ui",
    report_confirm: bool = False,
) -> dict:
    """核心提交通道：发布到 Redis orchestrator:main 并登记内存/SQLite。
    供 POST /task 与定时任务调度器共用；失败抛异常由调用方处理。"""
    if not _redis_ready():
        raise RuntimeError("Redis 未连接，任务无法派发")
    project = _safe_project(project)
    tid = f"{prefix}-" + uuid.uuid4().hex[:10]
    r = _new_redis()
    r.publish("orchestrator:main", json.dumps({
        "task_id": tid,
        "goal": goal,
        "project": project,
        "context": context,
        "auto_run": auto_run,
        "template_steps": template_steps,
        "user_id": user_id,
        "report_confirm": bool(report_confirm),
    }, ensure_ascii=False))
    with _task_lock:
        _task_results[tid] = {
            "task_id": tid,
            "status": "PENDING",
            "goal": goal,
            "project": project,
            "steps": [],
            "report": "",
            "conversation_id": conversation_id,
            "auto_run": auto_run,
        }
    try:
        db = sqlite3.connect(DB_PATH, timeout=3)
        db.execute(
            "INSERT INTO task_history"
            "(task_id,goal,status,project,conversation_id,parent_task_id,context,user)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tid, goal, "PENDING", project, conversation_id,
             parent_task_id, context, user_id),
        )
        db.commit()
        db.close()
    except Exception:
        pass
    return {"task_id": tid, "conversation_id": conversation_id,
            "status": "PENDING", "project": project}


def _task_pdf_bytes(tid: str) -> bytes:
    """生成任务报告 PDF；无报告/生成失败抛异常（路由转 404）。"""
    data = _get_task_report_data(tid)
    if not data or not str(data.get("report") or "").strip():
        raise LookupError("report not found")
    from report_pdf import markdown_to_pdf
    return markdown_to_pdf(
        str(data["report"]),
        title=str(data.get("goal") or "任务报告"),
        workspace=task_workspace(tid),
    )


# 前端未构建时的自包含状态页：不依赖 Node/vite，离线可读。
# （此前是一个只引用 localhost:5173/@vite/client 的壳——没装 Node 时白屏）
HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>织光 WeaveMind · 后端运行中</title>
<style>
 body{margin:0;background:#020617;color:#e2e8f0;font:14px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif}
 .wrap{max-width:680px;margin:8vh auto;padding:0 24px}
 h1{font-size:20px;margin:0 0 4px;color:#67e8f9}
 .sub{color:#64748b;font-size:13px;margin-bottom:24px}
 .card{background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:20px;margin-bottom:16px}
 .card h2{font-size:14px;margin:0 0 10px;color:#cbd5e1;font-weight:600}
 code,pre{background:#020617;border:1px solid #1e293b;border-radius:6px;color:#a5f3fc}
 code{padding:2px 6px;font-size:13px}
 pre{padding:12px;overflow-x:auto;font-size:13px}
 ul{margin:8px 0 0;padding-left:20px;color:#94a3b8}
 li{margin:4px 0}
 .ok{color:#34d399}.warn{color:#fbbf24}
</style></head><body><div class="wrap">
<h1>织光 WeaveMind</h1>
<div class="sub">后端已启动，前端界面尚未构建</div>
<div class="card">
  <h2>启用 Web 界面（任选其一）</h2>
  <p>① 构建前端（需要 Node 18+，构建一次即可）：</p>
  <pre>cd frontend
npm install
npm run build</pre>
  <p>② 或用开发模式（需 Node）：<code>cd frontend &amp;&amp; npm run dev</code>，访问 http://localhost:5173</p>
  <p>构建完成后刷新本页即可进入控制台。</p>
</div>
<div class="card">
  <h2>当前可用的后端接口</h2>
  <ul>
    <li><a href="/api/health" style="color:#67e8f9">/api/health</a> — 健康检查</li>
    <li><a href="/api/status" style="color:#67e8f9">/api/status</a> — 运行状态（需登录）</li>
    <li><code>POST /api/login</code>、<code>POST /task</code>、<code>/files/*</code> 等 API 均可用</li>
  </ul>
</div>
<div class="card">
  <h2>排障提示</h2>
  <ul>
    <li>任务无法提交 → 确认 Redis 可用（<code>redis-cli ping</code> 返回 PONG）</li>
    <li>服务未全部在线 → <code>python launcher.py status</code></li>
    <li>依赖缺失 → <code>pip install -r requirements.txt</code></li>
  </ul>
</div>
</div></body></html>"""
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")

class Handler(BaseHTTPRequestHandler):
    def _json(self, data, code=200, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _share_link(self, token: str) -> str:
        """构造分享页绝对链接。

        合成链接的基址优先取环境变量 PUBLIC_BASE_URL（不信任请求的 Host /
        X-Forwarded-Proto，避免 Host/Proto 头污染导致的分享链接投毒，
        防止投毒链接进入通知/邮件）；未配置时回退到请求 Host 以兼容旧行为。
        """
        base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base:
            return f"{base}/share/{token}"
        host = self.headers.get("Host") or f"localhost:{PORT}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}/share/{token}"

    def _cookie_secure_flag(self) -> str:
        """HTTPS 部署下给 Cookie 追加 Secure：依据反向代理透传的 X-Forwarded-Proto。
        （本地直连 HTTP 不加 Secure，避免纯 HTTP 部署下 Cookie 反而无法生效。）"""
        proto = (self.headers.get("X-Forwarded-Proto") or "http").strip().lower()
        return "; Secure" if proto == "https" else ""

    def _client_ip(self) -> str:
        """取客户端 IP：优先 X-Forwarded-For 第一段（配合反向代理）。"""
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return str(xff).split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def _request_token(self) -> str | None:
        """从 Authorization: Bearer <token> 或 Cookie: session=<token> 取 token。"""
        auth = self.headers.get("Authorization", "")
        if str(auth).lower().startswith("bearer "):
            return str(auth)[7:].strip()
        cookie = self.headers.get("Cookie", "")
        for part in str(cookie).split(";"):
            key, _, value = part.strip().partition("=")
            if key == "session":
                return value.strip()
        return None

    def _auth_session(self) -> dict | None:
        """解析并校验当前请求的会话；失效返回 None。"""
        return _get_session(self._request_token())

    def _require_auth(self) -> dict | None:
        """任意登录用户（admin/viewer）。未登录统一返回 401 JSON。"""
        session = self._auth_session()
        if session is None:
            self._json({"error": "未登录或登录已过期"}, 401)
            return None
        return session

    def _require_admin(self) -> dict | None:
        """仅管理员；viewer 返回 403。"""
        session = self._require_auth()
        if session is None:
            return None
        if session.get("role") != "admin":
            self._json({"error": "权限不足：仅管理员可执行此操作"}, 403)
            return None
        return session

    def _is_public_get(self, path: str) -> bool:
        """GET 白名单：公开分享页、健康检查、引导状态、前端静态资源。
        /files/<task_id>/... 仅在任务已生成分享链接时公开（供分享页图片/附件使用）。"""
        if path in ("", "/", "/api/health", "/api/auth/bootstrap"):
            return True
        if path.startswith("/share/"):
            return True
        if path.startswith("/files/"):
            rel = path[len("/files/"):]
            seg = rel.split("/", 1)
            if len(seg) == 2:
                try:
                    token = _find_share_token(seg[0])
                    if token is not None and _share_access_ok(self.headers, token):
                        return True
                except Exception:
                    pass
            return False
        if path.startswith("/api/") or path.startswith("/task/") or path == "/tasks":
            return False
        # 其余路径视为前端静态资源（dist），放行进入；数据接口仍按上述规则鉴权
        return True

    def _role_allowed_get(self, path: str, session: dict) -> bool:
        """viewer 只读：历史/状态/报告可看；配置、审计、工具审计仅 admin。"""
        if session.get("role") == "admin":
            return True
        if (
            path == "/api/config"
            or path == "/api/notifications"
            or path == "/api/audit"
            or path.startswith("/api/audit")
            or path == "/api/tool-audit"
            or path == "/api/scheduled-jobs"
        ):
            self._json({"error": "权限不足：仅管理员可访问"}, 403)
            return False
        return True

    def _handle_login(self):
        """POST /api/login：用户名+密码 → 会话 token（统一 401 提示，不暴露用户是否存在）。"""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw) if raw else {}
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        ip = self._client_ip()
        # 暴力破解防护：per-IP + per-username 双重计数，命中阈值后临时锁定。
        locked, wait = _bf_check(f"login:ip:{ip}")
        if not locked:
            locked, wait = _bf_check(f"login:user:{username or '?'}")
        if locked:
            audit_log(username or "?", ip, "login.failed", target=username,
                      result="fail", detail=f"尝试过多，已临时锁定 {wait}s")
            return self._json({"error": f"尝试次数过多，请 {wait} 秒后重试"}, 429)
        if not _users_initialized():
            audit_log(username or "?", ip, "login.failed", target=username,
                      result="fail", detail="系统尚未初始化管理员")
            return self._json(
                {"error": "系统尚未初始化管理员，请先创建初始管理员", "setup_required": True},
                401,
            )
        users = _load_users()
        user = users.get(username)
        if not user or not _verify_password(password, user.get("password_hash")):
            _bf_record(f"login:ip:{ip}")
            _bf_record(f"login:user:{username or '?'}")
            audit_log(username or "?", ip, "login.failed", target=username,
                      result="fail", detail="用户名或密码错误")
            return self._json({"error": "用户名或密码错误"}, 401)
        _bf_reset(f"login:ip:{ip}")
        _bf_reset(f"login:user:{username or '?'}")
        role = str(user.get("role") or "viewer")
        token = _create_session(username, role)
        audit_log(username, ip, "login.success", target=username, result="ok")
        # 会话凭证只经 HttpOnly Cookie 传递；响应 body 不再携带 token
        # （前端 Login/auth 未消费该字段，body 副本只会增加 XSS 窃取面）
        return self._json({
            "status": "ok",
            "user": username,
            "role": role,
            "expires_in": SESSION_TTL_SECONDS,
        }, extra_headers={
            "Set-Cookie": f"session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}{self._cookie_secure_flag()}",
        })

    def _handle_setup_admin(self):
        """POST /api/setup-admin：仅当系统尚无任何用户时允许创建初始管理员（一次）。"""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw) if raw else {}
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        if _users_initialized():
            return self._json({"error": "系统已存在用户，不能重复初始化"}, 409)
        username = str(body.get("username") or "admin").strip() or "admin"
        password = str(body.get("password") or "")
        if len(password) < 8:
            return self._json({"error": "密码至少 8 位"}, 400)
        users = {
            username: {
                "password_hash": _hash_password(password),
                "role": "admin",
                "created_at": _now_iso(),
            }
        }
        if not _save_users(users):
            return self._json({"error": "无法写入 config.json，初始化失败"}, 500)
        ip = self._client_ip()
        audit_log(username, ip, "user.bootstrap", target=username, result="ok",
                  detail="首次访问时创建初始管理员")
        token = _create_session(username, "admin")
        return self._json({
            "status": "ok",
            "user": username,
            "role": "admin",
            "expires_in": SESSION_TTL_SECONDS,
        }, extra_headers={
            "Set-Cookie": f"session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}{self._cookie_secure_flag()}",
        })

    def _handle_share_auth(self):
        """POST /share/<token>/auth：校验分享密码。
        成功 → Set-Cookie share_<token>=ok（7 天）+ 302 回分享页；
        失败 → 403 密码输入页（带错误提示）；非法 token → 404。"""
        p = urlparse(self.path).path
        token = p.split("/share/", 1)[-1].rsplit("/auth", 1)[0].strip()
        share_info = _load_shares().get(token)
        tid = share_info.get("task_id") if isinstance(share_info, dict) else None
        if not tid:
            return self._html(_SHARE_NOT_FOUND_HTML, 404)
        if not share_info.get("password_hash"):
            # 无密码分享：直接放行（兼容旧链接，不需要进入认证流程）
            return self._redirect(f"/share/{token}")
        password = ""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            if raw:
                try:
                    body = json.loads(raw)
                except Exception:
                    # HTML 表单默认 application/x-www-form-urlencoded
                    form = {}
                    for pair in raw.decode("utf-8", errors="replace").split("&"):
                        if "=" in pair:
                            k, _, v = pair.partition("=")
                            form[unquote(k)] = unquote(v)
                    body = form
                if isinstance(body, dict):
                    password = str(body.get("password") or "")
        except Exception:
            pass
        _bip = self._client_ip()
        locked, wait = _bf_check(f"share:ip:{_bip}")
        if not locked:
            locked, wait = _bf_check(f"share:token:{token}")
        if locked:
            return self._html(
                _share_password_page(token, f"尝试过多，请 {wait} 秒后重试"), 429
            )
        if not password or not _verify_password(password, share_info.get("password_hash")):
            # 密码错误：记录暴力破解计数（per-IP + per-token 双重）。
            _bf_record(f"share:ip:{_bip}")
            _bf_record(f"share:token:{token}")
            return self._html(_share_password_page(token, "密码错误，请重新输入"), 403)
        _bf_reset(f"share:ip:{_bip}")
        _bf_reset(f"share:token:{token}")
        return self._redirect(
            f"/share/{token}",
            extra_headers={
                "Set-Cookie": (
                    f"{_share_cookie_name(token)}=ok; HttpOnly; SameSite=Lax; "
                    f"Path=/; Max-Age={SHARE_AUTH_COOKIE_TTL}{self._cookie_secure_flag()}"
                ),
            },
        )

    def _redirect(self, location: str, extra_headers: dict | None = None):
        """302 跳转：用于分享密码验证成功后的放行。"""
        self.send_response(302)
        self.send_header("Location", location)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_dist(self, path: str) -> bool:
        """生产模式：伺服前端构建产物（frontend/dist），含 SPA 回退。"""
        if not os.path.isdir(DIST_DIR):
            return False
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        _dist_root = os.path.realpath(DIST_DIR)
        target = os.path.realpath(os.path.join(DIST_DIR, rel))
        # 前缀必须带分隔符：否则 dist_backup 等同级目录可借道 ../ 通过检查
        if target != _dist_root and not target.startswith(_dist_root + os.sep):
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
        # 缓存策略：HTML 不缓存（防止浏览器用旧 bundle 引用），
        # 带内容 hash 的静态资源（assets/ 下）允许长缓存
        if ctype == "text/html; charset=utf-8":
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        elif "/assets/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        p = urlparse(self.path).path
        # 鉴权门：公开路径放行，其余数据接口需要登录；config/audit 等仅 admin
        if not self._is_public_get(p):
            session = self._require_auth()
            if session is None:
                return
            if not self._role_allowed_get(p, session):
                return
        # E 重构：业务分支表化（保持原 if 顺序，先匹配先处理）
        for _pred, _h in _GET_ROUTES:
            if _pred(self, p):
                return _h(self, p)
        if self._serve_dist(p):
            return
        return self._json({"error": "not found"}, 404)


    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/login":
            return self._handle_login()
        if p == "/api/setup-admin":
            return self._handle_setup_admin()
        if p == "/api/logout":
            # 登出对 admin/viewer 都开放，但必须携带有效会话
            session = self._require_auth()
            if session is None:
                return
            _delete_session(self._request_token() or "")
            audit_log(session.get("user", ""), self._client_ip(), "logout",
                      target=session.get("user", ""), result="ok")
            return self._json(
                {"status": "ok"},
                extra_headers={"Set-Cookie": "session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"},
            )
        if p.startswith("/share/") and p.endswith("/auth"):
            # F6：公开分享密码验证（无需登录，凭 token 本身访问）
            return self._handle_share_auth()
        # 其余 POST 均为写操作：仅管理员可执行（viewer 403）
        admin = self._require_admin()
        if admin is None:
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw) if raw else {}
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        # E 重构：业务分支表化（保持原 if 顺序，先匹配先处理）
        for _pred, _h in _POST_ROUTES:
            if _pred(self, p):
                return _h(self, p, body, admin)
        return self._json({"error": "not found"}, 404)


    def do_DELETE(self):
        """删除任务或撤销分享（均仅 admin）：
        DELETE /api/task/<task_id>
        DELETE /api/share/<task_id>（兼容 /api/share/revoke + body）"""
        p = urlparse(self.path).path
        admin = self._require_admin()
        if admin is None:
            return
        ip = self._client_ip()
        if p.startswith("/api/task/"):
            tid = p.split("/api/task/", 1)[-1].strip()
            if not tid:
                return self._json({"error": "task_id required"}, 400)
            if not _task_exists(tid):
                return self._json({"error": "task not found"}, 404)
            if not _delete_task(tid):
                return self._json({"error": "删除失败"}, 500)
            audit_log(admin.get("user", ""), ip, "task.delete", target=tid, result="ok")
            return self._json({"status": "ok", "task_id": tid})
        if p.startswith("/api/share/"):
            tid = p.split("/api/share/", 1)[-1].strip()
            if tid == "revoke":
                tid = ""
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length) if length > 0 else b""
                    tid = str((json.loads(raw) if raw else {}).get("task_id") or "").strip()
                except Exception:
                    tid = ""
            if not tid:
                return self._json({"error": "task_id required"}, 400)
            revoked = _revoke_share_token(tid)
            audit_log(admin.get("user", ""), ip, "share.revoke", target=tid,
                      result="ok", detail=f"revoked={revoked}")
            return self._json({"status": "ok", "task_id": tid, "revoked": revoked})
        return self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type, Authorization")
        self.end_headers()

    def log_message(self, fmt, *args): pass

def main():
    from logging_setup import setup_logging
    setup_logging("webui")
    _init_db()
    _ensure_users_on_startup()
    _load_evolution_history()
    def _supervise(fn):
        while True:
            try:
                fn()
            except Exception as exc:
                logging.getLogger("web_ui").warning("Listener crashed, restarting: %s", str(exc)[:120])
                time.sleep(3)

    threading.Thread(target=_supervise, args=(_listen_results,), daemon=True).start()
    threading.Thread(target=_supervise, args=(_listen_events,), daemon=True).start()
    # F2：定时任务调度线程（读 config.json 的 scheduled_jobs，进程内提交）
    def _run_scheduled_jobs():
        from scheduled_jobs import ScheduledJobsRunner

        def _submit(job: dict) -> str:
            submitted = _publish_task(
                goal=str(job.get("goal") or ""),
                project=str(job.get("project") or "default"),
                auto_run=True,
                user_id="scheduler",
                prefix="sched",
            )
            return submitted.get("task_id", "")

        def _outcome(task_id: str):
            """查询调度任务终态（PENDING/运行中返回 None）。"""
            try:
                db = sqlite3.connect(DB_PATH, timeout=5)
                row = db.execute(
                    "SELECT status FROM task_history WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                db.close()
                if not row:
                    return None
                st = str(row[0] or "")
                return st if st in ("SUCCESS", "FAILED", "SUCCESS_WITH_ISSUES") else None
            except Exception:
                return None

        def _on_failure(job_name: str, task_id: str, status: str):
            """调度任务失败回调：告警事件 + 失败通知（按 task_id 去重由 runner 保证）。"""
            _publish_alert(
                "scheduled_task_failed",
                f"定时任务「{job_name}」执行失败：{task_id}（{status}）",
            )
            # 失败通知按 task_id 全局去重（与全局监听器 D1 分支共用一个闸）
            if _try_claim_failure_notify(task_id):
                try:
                    from notifications import notify_task_done_async
                    notify_task_done_async(
                        task_id, goal=f"[定时任务] {job_name}", status="FAILED",
                        summary=f"定时任务「{job_name}」执行失败（{status}），请检查健康页与任务详情",
                    )
                except Exception:
                    pass

        runner = ScheduledJobsRunner(
            submit_fn=_submit, config_path=CONFIG_PATH,
            outcome_fn=_outcome, on_failure=_on_failure,
        )
        _sched_runner_ref.append(runner)
        runner.run()

    threading.Thread(
        target=_supervise, args=(_run_scheduled_jobs,), daemon=True,
    ).start()
    # 预热记忆库（避免首个 /api/status 或 /api/memory 请求阻塞）
    def _prewarm_memory():
        mem = _get_memory_manager()
        _refresh_memory_stats()
        if mem is not None:
            try:
                # 预热 /api/memory 列表缓存（MemoryManager 内部已后台做治理）
                _refresh_memory_data(mem)
            except Exception:
                pass
    threading.Thread(target=_prewarm_memory, daemon=True).start()

    def _cleanup_stale_tasks():
        """把长时间卡在 PENDING 的任务标记为过期，避免永久悬挂。

        P0 时间轴修复：运行中的任务必须豁免——task_history 全程保持 PENDING
        （只有提交与终态会写库），此前"超 30 分钟即翻 FAILED"会误杀合法长任务
        （8 步串行 + 反思重做 + 修复轮可远超 30 分钟），并触发调度器对
        "已失败"任务的重复重提。豁免判据：Redis task_running:{tid} 存在
        （编排器 24h 标记）或内存 _task_results 中状态为 RUNNING。

        T2：scheduler 任务（sched- 前缀 / user=scheduler）过期时不再静默——
        发布 orchestrator:alert 事件（Health 页立即可见）并发送失败通知；
        其他用户任务保持原静默行为（历史行为不变）。"""
        while True:
            time.sleep(60)
            try:
                # 每次循环读最新过期阈值（config system.stale_task_timeout，
                # 默认 3600——对齐最坏合法总时长，环境变量可覆盖）
                try:
                    stale_seconds = int(
                        str(((_load_config().get("system") or {}).get(
                            "stale_task_timeout")) or "")
                        or os.environ.get("STALE_TASK_TIMEOUT", "3600") or 3600
                    )
                except Exception:
                    stale_seconds = 3600
                stale_seconds = max(600, stale_seconds)
                # 豁免集合：运行中的任务（编排器 task_running 标记 / 内存 RUNNING）
                running_ids: set[str] = set()
                try:
                    r = _new_redis()
                    for k in r.scan_iter("task_running:*", count=200):
                        running_ids.add(str(k).split(":", 1)[-1])
                except Exception:
                    pass
                with _task_lock:
                    running_ids |= {
                        str(tid) for tid, st in _task_results.items()
                        if str(st.get("status") or "").upper() == "RUNNING"
                    }
                db = sqlite3.connect(DB_PATH, timeout=5)
                # 先取候选（PENDING 且超时），逐个 UPDATE 时跳过豁免集合——
                # 数量极少（PENDING 任务不会多），逐行处理可控
                candidates = [row[0] for row in db.execute(
                    "SELECT task_id FROM task_history "
                    "WHERE status='PENDING' AND created_at < datetime('now', ?)",
                    (f"-{stale_seconds} seconds",),
                ).fetchall()]
                expired_rows = []
                for tid in candidates:
                    if tid in running_ids:
                        continue
                    db.execute(
                        "UPDATE task_history SET status='FAILED', report=? "
                        "WHERE task_id=? AND status='PENDING'",
                        (
                            f"Task expired (no completion within {stale_seconds} s)",
                            tid,
                        ),
                    )
                    if db.total_changes:
                        expired_rows.append(tid)
                db.commit()
                if not expired_rows:
                    db.close()
                    continue
                # 仅取本轮确实被翻转的 scheduler 任务
                placeholders = ",".join("?" * len(expired_rows))
                expired = [dict(zip(("task_id", "goal", "user"), row)) for row in db.execute(
                    "SELECT task_id, goal, IFNULL(user,'') FROM task_history "
                    "WHERE status='FAILED' AND report=? "
                    f"AND task_id IN ({placeholders}) "
                    "AND (task_id LIKE 'sched-%' OR IFNULL(user,'')='scheduler')",
                    [
                        f"Task expired (no completion within {stale_seconds} s)",
                        *expired_rows,
                    ],
                ).fetchall()]
                db.close()
                _alerted_expired = set()
                for row in expired:
                    tid = str(row.get("task_id") or "")
                    if tid in _alerted_expired:
                        continue
                    _alerted_expired.add(tid)
                    # 告警事件：Health 页事件流即刻可见
                    _publish_alert(
                        "scheduled_task_expired",
                        f"定时任务过期未完成：{tid}（{str(row.get('goal') or '')[:60]}）",
                    )
                    # 失败通知（复用任务完成通道，标题按状态）
                    try:
                        from notifications import notify_task_done_async
                        notify_task_done_async(
                            tid,
                            goal=str(row.get("goal") or ""),
                            status="FAILED",
                            summary=f"定时任务在 {stale_seconds}s 内未完成（编排器未消费或执行中断），已标记失败",
                        )
                    except Exception:
                        pass
            except Exception:
                pass

    threading.Thread(target=_cleanup_stale_tasks, daemon=True).start()
    # 默认只绑定回环地址，避免未加鉴权时暴露到局域网/公网；需要对外服务时用 BIND_HOST=0.0.0.0 显式开启。
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((bind_host, PORT), Handler)
    print(f"http://localhost:{PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()


def _get_health(self, p):
    if p == "/api/health":
        # 公开健康检查：无需登录，供探活/负载均衡使用；附带沙箱状态供部署确认
        return self._json({
            "status": "ok",
            "service": "weavemind-web",
            "time": _now_iso(),
            "code_sandbox": _code_sandbox_status(),
        })

def _get_bootstrap(self, p):
    if p == "/api/auth/bootstrap":
        # 公开引导状态：前端据此判断显示“创建初始管理员”还是登录表单
        return self._json({"setup_required": not _users_initialized()})

def _get_audit(self, p):
    if p == "/api/audit":
        # 审计查询（仅 admin，由 _role_allowed_get 兜底）
        query = urlparse(self.path).query
        limit = 200
        try:
            limit = int([
                pair.split("=", 1)[1] for pair in query.split("&")
                if pair.startswith("limit=")
            ][0])
        except Exception:
            pass
        entries = read_audit(limit)
        return self._json({"entries": entries, "count": len(entries)})

def _get_root(self, p):
    if p == "/":
        if self._serve_dist("/"):
            return
        return self._html(HTML)

def _get_status(self, p):
    if p == "/api/status": return self._json(_system_status())

def _get_projects(self, p):
    if p == "/api/projects":
        # F1：项目列表（扫描 projects/ 目录；旧版平铺任务归入 legacy）
        return self._json({"projects": list_projects()})

def _get_scheduled_jobs(self, p):
    if p == "/api/scheduled-jobs":
        # F2：定时任务列表（GET 仅 admin，见 _role_allowed_get）
        # T2：附带最近触发记录（上次结果列），来自运行中的 runner 调度日志
        from scheduled_jobs import load_jobs
        recent: list[dict] = []
        if _sched_runner_ref:
            try:
                recent = _sched_runner_ref[-1].last_results(30)
            except Exception:
                recent = []
        return self._json({"jobs": load_jobs(CONFIG_PATH), "recent": recent})

def _get_files(self, p):
    if p.startswith("/files/"):
        rel = p[len("/files/"):]
        seg = rel.split("/", 1)
        if len(seg) != 2 or not seg[0]:
            return self._json({"error": "not found"}, 404)
        tid, rel = seg[0], seg[1]
        fp = _safe_workspace_path(rel, tid)
        if fp:
            relative = os.path.relpath(fp, task_workspace(tid)).replace("\\", "/")
            if not relative.startswith(("reports/", "charts/", "data/")):
                fp = None
        if not fp:
            return self._json({"error": "not found"}, 404)
        if not os.path.isfile(fp):
            return self._json({"error": "not found"}, 404)
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except Exception:
            return self._json({"error": "read failed"}, 500)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

def _get_task_acceptance(self, p):
    """T4：任务验收全量报告（四档计数 + traceable 明细）。

    登录即可访问（viewer 允许）——只读工作区 acceptance_report.json；
    报告未生成（任务未到验收阶段）返回 404，前端静默隐藏溯源行。"""
    if p.startswith("/api/task/") and p.endswith("/acceptance"):
        tid = p.split("/api/task/")[-1].rsplit("/acceptance", 1)[0]
        try:
            from workspace import task_workspace
            acc_path = task_workspace(tid) / "acceptance_report.json"
            if not acc_path.exists():
                # 旧任务/未完成：查 SQLite 兜底提示
                return self._json({"error": "not found"}, 404)
            with open(acc_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            return self._json(report)
        except Exception:
            return self._json({"error": "read failed"}, 500)

def _get_task_acceptance_timeline(self, p):
    """验收事件流（可回放/可对账）：按顺序返回该任务历次验收事件。

    事件含 trigger（报告步骤/反思重做）、rules_version/rules_fingerprint
    （判定规则版本与指纹，规则变更后可反查旧结果由哪版规则产出）、
    report_sha256（对应报告文本）、overall/gaps/耗时。
    登录即可访问（viewer 允许）；无事件返回空列表（旧任务）。"""
    if p.startswith("/api/task/") and p.endswith("/acceptance/timeline"):
        tid = p.split("/api/task/")[-1].rsplit("/acceptance/timeline", 1)[0]
        try:
            from acceptance_checker import read_acceptance_events
            events = read_acceptance_events(tid)
            return self._json({"task_id": tid, "count": len(events), "events": events})
        except Exception:
            return self._json({"error": "read failed"}, 500)

def _get_task_deliverables(self, p):
    if p.startswith("/api/task/") and p.endswith("/deliverables"):
        tid = p.split("/api/task/")[-1].rsplit("/deliverables", 1)[0]
        return self._json({"files": _task_deliverables(tid)})

def _get_task_usage(self, p):
    if p.startswith("/api/task/") and p.endswith("/usage"):
        # 每任务 token/成本台账（O-19）
        tid = p.split("/api/task/")[-1].rsplit("/usage", 1)[0]
        try:
            r = _new_redis()
            ledger = r.hgetall(f"llm_usage_task:{tid}") or {}
        except Exception:
            ledger = {}
        from costs import ledger_cost
        return self._json({
            "task_id": tid,
            "ledger": ledger,
            "calls": int(ledger.get("calls", 0) or 0),
            "prompt_tokens": sum(
                int(v) for k, v in ledger.items() if str(k).startswith("pt:")
            ),
            "completion_tokens": sum(
                int(v) for k, v in ledger.items() if str(k).startswith("ct:")
            ),
            "cost_usd": ledger_cost(ledger),
        })

def _get_task_stream(self, p):
    if p.startswith("/api/task/") and p.endswith("/stream"):
        # 步骤级流式输出（O-21）：worker 生成过程中按块发布到 Redis
        tid = p.split("/api/task/")[-1].rsplit("/stream", 1)[0]
        text = ""
        try:
            r = _new_redis()
            chunks = r.lrange(f"stream:{tid}", 0, -1) or []
            r.expire(f"stream:{tid}", 600)
            text = "".join(chunks)
        except Exception:
            pass
        return self._json({"task_id": tid, "text": text[-20000:]})

def _get_task_events(self, p):
    if p.startswith("/api/task/") and p.endswith("/events"):
        # T11 SSE：任务进度实时推送。每条消息后下发合并后的完整快照
        # （与 /task/{id} 同构），前端 SSE 与轮询共用同一套应用逻辑；
        # Redis 不可用时纯心跳降级，前端自动回退轮询。
        tid = p.split("/api/task/")[-1].rsplit("/events", 1)[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        wf = self.wfile

        def _emit(event: str, obj) -> bool:
            try:
                wf.write((
                    f"event: {event}\ndata: "
                    + json.dumps(obj, ensure_ascii=False) + "\n\n"
                ).encode("utf-8"))
                wf.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        merged: dict = {}
        try:
            with _task_lock:
                base = _task_results.get(tid)
            if isinstance(base, dict):
                merged = dict(base)
                merged["steps"] = list(base.get("steps") or [])
                merged["logs"] = list(base.get("logs") or [])
        except Exception:
            pass

        def _subscribe():
            """建立 pubsub 订阅；失败返回 None（稍后重试）。"""
            try:
                r = _new_redis()
                sub = r.pubsub()
                sub.subscribe("orchestrator:response")
                return sub
            except Exception:
                try:
                    sub.close()
                except Exception:
                    pass
                return None

        def _resync_snapshot() -> bool:
            """订阅空窗期（快照读取→订阅生效之间）的消息可能只进了全局态：
            以全局态为准对账，避免本地快照漏日志/漏终态。"""
            try:
                with _task_lock:
                    base = _task_results.get(tid)
                if isinstance(base, dict):
                    merged.update(base)
                    merged["steps"] = list(base.get("steps") or [])
                    merged["logs"] = list(base.get("logs") or [])
                    return _emit("snapshot", {"task_id": tid, "data": merged})
            except Exception:
                pass
            return True

        # 先建订阅再取快照，空窗消息通过对账补偿
        ps = _subscribe()
        if not _resync_snapshot():
            try:
                if ps is not None:
                    ps.close()
            except Exception:
                pass
            return None
        deadline = time.time() + 1800  # 单连接最长 30 分钟
        next_resync = time.time() + 60
        try:
            while time.time() < deadline:
                if ps is None:
                    # Redis 断连：心跳保活并周期性重建订阅（不能只发心跳，
                    # 否则连接看似健康但永远收不到消息）
                    if not _emit("hb", {"t": int(time.time())}):
                        return None
                    time.sleep(15)
                    ps = _subscribe()
                    continue
                try:
                    msg = ps.get_message(ignore_subscribe_messages=True, timeout=15)
                except Exception:
                    # 订阅故障：关闭旧连接，下轮重建
                    try:
                        ps.close()
                    except Exception:
                        pass
                    ps = None
                    msg = None
                if time.time() >= next_resync:
                    # 周期对账：pubsub 与全局监听双通道下防漏
                    next_resync = time.time() + 60
                    if not _resync_snapshot():
                        return None
                if msg and msg.get("type") == "message":
                    try:
                        evt = json.loads(msg["data"])
                    except Exception:
                        continue
                    if str(evt.get("task_id")) != tid:
                        continue
                    _merge_progress_message(merged, evt)
                    if not _emit("snapshot", {"task_id": tid, "data": merged}):
                        return None
                    if str(evt.get("type")) == "task_complete":
                        # 终态已随快照下发；结束本连接（EventSource 会重连，
                        # 前端在收到终态时会主动关闭）
                        return None
                else:
                    if not _emit("hb", {"t": int(time.time())}):
                        return None
        finally:
            try:
                if ps is not None:
                    ps.close()
            except Exception:
                pass
        return None

def _get_task_pdf(self, p):
    if p.startswith("/api/task/") and p.endswith("/pdf"):
        # F3：报告服务端 PDF 导出（Content-Disposition attachment）
        tid = p.split("/api/task/")[-1].rsplit("/pdf", 1)[0]
        try:
            body = _task_pdf_bytes(tid)
        except Exception:
            return self._json({"error": "report not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{tid}.pdf"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

def _get_config(self, p):
    if p == "/api/config": return self._json(_public_config(_load_config()))

def _get_llm_mode(self, p):
    if p == "/api/llm-mode":
        # 当前 LLM 模式（cloud/hybrid）；Redis 优先，config.json 兜底
        mode = "hybrid"
        try:
            if _redis_ready():
                rmode = _new_redis().get("llm_mode")
                if rmode:
                    mode = rmode
        except Exception:
            pass
        if mode not in ("cloud", "hybrid"):
            try:
                mode = str((_load_config().get("system") or {}).get("llm_mode") or "hybrid")
            except Exception:
                mode = "hybrid"
        return self._json({"mode": mode})

def _get_notifications(self, p):
    if p == "/api/notifications":
        # F5：通知配置读取（仅 admin，见 _role_allowed_get）；密码/密钥不回显
        from notifications import (
            load_notifications_config,
            public_notifications_config,
        )
        return self._json({
            "notifications": public_notifications_config(
                load_notifications_config(CONFIG_PATH)
            ),
        })

def _get_events(self, p):
    if p == "/api/events":
        with _events_lock:
            return self._json({"events": list(reversed(_events[-100:]))})

def _get_memory(self, p):
    if p == "/api/memory":
        return self._json(_get_memory_data())

def _get_memory_summary_route(self, p):
    if p == "/api/memory/summary":
        refresh = "refresh=1" in urlparse(self.path).query
        return self._json(_get_memory_summary(refresh))

def _get_templates(self, p):
    if p == "/api/templates":
        return self._json({"templates": _load_templates()})

def _get_evals(self, p):
    if p == "/api/evals":
        # 评测看板数据（O-24）：校准报告 + 近期任务评测分数
        calib = {}
        calib_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "evals", "calibration_report.json"
        )
        try:
            if os.path.exists(calib_path):
                calib = json.loads(open(calib_path, encoding="utf-8").read())
        except Exception:
            pass
        recent = []
        try:
            r = _new_redis()
            for k in r.scan_iter("eval_score:*", count=100):
                tid = str(k).split(":", 1)[-1]
                try:
                    recent.append({"task_id": tid, "scores": json.loads(r.get(k))})
                except Exception:
                    pass
            recent.sort(key=lambda x: x["task_id"], reverse=True)
        except Exception:
            pass
        return self._json({"calibration": calib, "recent": recent[:30]})

def _get_acceptance_suggestions(self, p):
    if p == "/api/acceptance/suggestions":
        # A2：返回该任务验收中的域名媒体补录建议（供人工确认流程；
        # 前端暂不展示亦可，数据已就绪）
        query = urlparse(self.path).query
        tid = ""
        for pair in query.split("&"):
            if pair.startswith("task_id="):
                tid = unquote(pair.split("=", 1)[1]).strip()
        if not tid:
            return self._json({"error": "task_id 参数必填"}, 400)
        suggestions: list[str] = []
        try:
            acc_path = task_workspace(tid) / "acceptance_report.json"
            if acc_path.exists():
                acc = json.loads(acc_path.read_text(encoding="utf-8"))
                suggestions = acc.get("suggestions") or []
                if not suggestions:
                    src = (acc.get("checks") or {}).get("source_labeling") or {}
                    suggestions = src.get("suggestions") or []
        except Exception:
            pass
        return self._json({"task_id": tid, "suggestions": suggestions})

def _get_skills(self, p):
    if p == "/api/skills":
        # Skill 管理数据（O-25）
        try:
            from skill_registry import get_lessons, list_skills
            skills = list_skills()
            for s in skills:
                s["lessons"] = get_lessons(s["name"], limit=3)
            return self._json({"skills": skills})
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

def _get_tool_audit(self, p):
    if p == "/api/tool-audit":
        # 工具审计日志（MCP/ReAct/编排器派发的工具调用记录）
        try:
            from tool_dispatch import recent_audit
            return self._json({"entries": recent_audit(100)})
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

def _get_evolution(self, p):
    if p == "/api/evolution":
        with _evolution_lock:
            return self._json({"rounds": list(reversed(_evolution_results))})

def _get_evolution_pending(self, p):
    if p == "/api/evolution/pending":
        r = _new_redis()
        try:
            items = [json.loads(x) for x in r.lrange("evolution:pending", 0, -1)]
        except Exception:
            items = []
        return self._json({"pending": items})

def _get_metrics(self, p):
    if p == "/api/metrics":
        try:
            with open(_METRICS_SUMMARY, "r", encoding="utf-8") as f:
                return self._json(json.load(f))
        except Exception:
            return self._json({"error": "no metrics yet"}, 404)

def _get_tasks(self, p):
    if p == "/tasks": return self._json({"tasks": _list_tasks(50)})

def _get_conversations(self, p):
    if p == "/api/conversations": return self._json({"conversations": _list_conversations(50)})

def _get_conversation_detail(self, p):
    if p.startswith("/api/conversations/"):
        conv_id = p.split("/api/conversations/")[-1]
        msgs = _get_conversation(conv_id)
        if not msgs:
            return self._json({"error": "conversation not found"}, 404)
        return self._json({"conversation_id": conv_id, "messages": msgs})

def _get_share_data(self, p):
    if p.startswith("/api/share/"):
        # 查询任务当前分享状态（前端刷新后可恢复“已分享/撤销分享”按钮态）
        tid = p.split("/api/share/", 1)[-1].strip()
        token = _find_share_token(tid)
        if token:
            info = _load_shares().get(token) or {}
            return self._json({
                "shared": True,
                "task_id": tid,
                "token": token,
                "path": f"/share/{token}",
                "url": self._share_link(token),
                "protected": bool(info.get("password_hash")),
                "expires_at": info.get("expires_at"),
            })
        return self._json({"shared": False, "task_id": tid})

def _get_share_page(self, p):
    if p.startswith("/share/"):
        # 公开只读分享页：token → task_id → 自包含 HTML 报告（无需登录）。
        # F6：记录带密码时先验证 Cookie，未验证返回 401 密码输入页。
        token = p.split("/share/", 1)[-1].strip()
        share_info = _load_shares().get(token)
        tid = share_info.get("task_id") if isinstance(share_info, dict) else None
        if tid and not _share_access_ok(self.headers, token):
            return self._html(_share_password_page(token), 401)
        data = _get_task_report_data(tid) if tid else None
        if not tid or not data:
            return self._html(_SHARE_NOT_FOUND_HTML, 404)
        # 结构化解析：数据时效卡/目录/来源卡片/免责声明（与前端 ReportViewer 等价）
        try:
            structured = _share_page_structured(data.get("report") or "")
        except Exception:
            structured = {}
        body_html = _markdown_to_html(structured.get("body") or data.get("report") or "", tid)
        title = str(data.get("goal") or "任务报告")
        created = data.get("created_at") or data.get("completed_at") or ""
        # Roadmap 余项⑤：分享页主题模板 ?theme=light|dark|paper
        try:
            from urllib.parse import parse_qs
            theme = (parse_qs(urlparse(self.path).query).get("theme") or ["light"])[0]
        except Exception:
            theme = "light"
        # D2：日报落地页下载按钮——ranking.csv 存在时附下载链接
        download_url = ""
        try:
            if (task_workspace(tid) / "data" / "ranking.csv").exists():
                download_url = f"/files/{tid}/data/ranking.csv"
        except Exception:
            pass
        structured = _share_page_structured(data.get("report") or "", task_id=tid)
        return self._html(_share_page_html(
            title, created, body_html, theme=theme,
            structured=structured, download_url=download_url))

def _get_task_report(self, p):
    if p.startswith("/task/") and p.endswith("/report"):
        tid = p.split("/task/")[-1].rsplit("/report", 1)[0]
        with _task_lock: data = _task_results.get(tid)
        if not data:
            for t in _list_tasks(100):
                if t.get("task_id") == tid:
                    data = t
                    break
        if data and data.get("report"):
            # 与公开分享页同一套渲染：Markdown → 结构化卡片 → 主题化 HTML。
            # 此前直接 self._html(报告正文)，浏览器只看到一坨无样式的 Markdown
            # 源码；深色主题下默认黑字落在深色底上，正文几乎不可见。
            report = data["report"]
            try:
                structured = _share_page_structured(report, task_id=tid)
            except Exception:
                structured = {}
            body_html = _markdown_to_html(
                structured.get("body") or report, tid)
            return self._html(_share_page_html(
                str(data.get("goal") or "任务报告"),
                str(data.get("created_at") or data.get("completed_at") or ""),
                body_html, theme="light", structured=structured,
            ))
        return self._json({"error": "report not found"}, 404)

def _get_task_page(self, p):
    if p.startswith("/task/"):
        tid = p.split("/task/")[-1]
        with _task_lock: data = _task_results.get(tid)
        if not data:
            for t in _list_tasks(100):
                if t.get("task_id") == tid: data = t; break
        if data:
            _steps = data.get("steps") or []
            _logs = data.get("logs") or []
            if not _steps and data.get("steps_json"):
                try:
                    _steps = json.loads(data["steps_json"]) or []
                except Exception:
                    pass
            if not _logs and data.get("logs_json"):
                try:
                    _logs = json.loads(data["logs_json"]) or []
                except Exception:
                    pass
            return self._json({
            "task_id": tid,
            "status": data.get("status", "PENDING"),
            "goal": data.get("goal", ""),
            "steps": _steps,
            "report": data.get("final_report") or data.get("report", ""),
            "logs": _logs,
            "project": data.get("project", ""),
            "revision": bool(data.get("revision")),
            "acceptance": data.get("acceptance"),
            "llm_degraded": data.get("llm_degraded"),
        })
        return self._json({"error":"not found"},404)

def _post_share(self, p, body, admin):
    share_tid = ""
    if p == "/api/share":
        share_tid = str(body.get("task_id") or "").strip()
    elif p.startswith("/api/share/"):
        share_tid = p.split("/api/share/", 1)[-1].strip()
    if share_tid:
        if not _get_task_report_data(share_tid):
            audit_log(admin.get("user", ""), self._client_ip(), "share.generate",
                      target=share_tid, result="fail", detail="任务不存在或没有可分享的报告")
            return self._json({"error": "任务不存在或没有可分享的报告"}, 404)
        # F6：可选密码（body 含 password 字段才处理；空串=清除密码）与
        # 自定义有效期 ttl_hours（默认 168=7 天，上限 720=30 天）
        password = body["password"] if "password" in body else None
        ttl_hours = None
        if "ttl_hours" in body:
            try:
                ttl_hours = float(body["ttl_hours"])
            except (TypeError, ValueError):
                return self._json({"error": "ttl_hours 必须是数字（小时）"}, 400)
            if ttl_hours < 1 or ttl_hours > 720:
                return self._json({"error": "ttl_hours 须在 1~720 小时之间（最长 30 天）"}, 400)
        if isinstance(password, str):
            password = password.strip()
            if len(password) > 128:
                return self._json({"error": "分享密码过长（上限 128 字符）"}, 400)
        token = _generate_share_token(share_tid, password=password, ttl_hours=ttl_hours)
        audit_log(admin.get("user", ""), self._client_ip(), "share.generate",
                  target=share_tid, result="ok")
        share_info = _load_shares().get(token) or {}
        expires_at = share_info.get("expires_at") or ""
        expires_in_days = 7
        try:
            exp_ts = datetime.fromisoformat(
                str(expires_at).replace("Z", "+00:00")
            ).timestamp()
            expires_in_days = max(1, round((exp_ts - time.time()) / 86400))
        except Exception:
            pass
        return self._json({
            "status": "ok",
            "task_id": share_tid,
            "token": token,
            "path": f"/share/{token}",
            "url": self._share_link(token),
            "protected": bool(share_info.get("password_hash")),
            "expires_at": expires_at,
            "expires_in_days": expires_in_days,
        })

def _post_deliverable_run(self, p, body, admin):
    if self.path == "/api/deliverable/run":
        name = str(body.get("path") or "").strip()
        tid = str(body.get("task_id") or "").strip() or None
        if not name:
            return self._json({"error": "path required"}, 400)
        fp = _safe_project_path(name, tid)
        if not fp or not os.path.isfile(fp):
            return self._json({"error": "file not found"}, 404)
        ext = os.path.splitext(fp)[1].lower()
        if ext == ".html":
            prefix = f"/files/{tid}/" if tid else "/files/"
            return self._json({"status": "ok", "open_url": prefix + name.replace("\\", "/")})
        if ext != ".py":
            return self._json({"error": "only .py files can be run"}, 400)
        # P2-5：复用统一沙箱（code_sandbox）运行交付物，而非裸 subprocess。
        # run_script 会经 sanitize_env 剥离密钥，并按沙箱模式（docker/restricted/none）
        # 执行，与 orchestrator/worker 的代码运行路径保持一致。
        try:
            from code_sandbox import run_script
        except Exception:
            run_script = None
        if run_script is None:
            return self._json({"error": "代码沙箱不可用"}, 500)
        try:
            res = run_script(
                fp,
                cwd=os.path.dirname(fp),
                timeout=60,
            )
            stdout = (res.stdout or b"").decode("utf-8", errors="replace")
            stderr = (res.stderr or b"").decode("utf-8", errors="replace")
            output = (stderr + stdout)[-4000:] or "(no output)"
            return self._json({
                "status": "ok",
                "returncode": res.returncode,
                "output": output,
                "sandbox_mode": getattr(res, "sandbox_mode", None),
            })
        except subprocess.TimeoutExpired:
            return self._json({"error": "执行超时（60s）"}, 500)
        except Exception as exc:
            return self._json({"error": f"run failed: {exc}"}, 500)

def _post_task(self, p, body, admin):
    if self.path == "/task":
        g = body.get("goal","").strip()
        # 输入安全（对标 C4-4.4）：长度限制 + 注入检测 + 简单限流
        from security import MAX_GOAL_LEN, RateLimiter, detect_injection, sanitize_goal
        if len(g) > MAX_GOAL_LEN:
            return self._json({"error": f"目标过长（>{MAX_GOAL_LEN} 字符），已拦截"}, 400)
        # 空/损坏目标拦截：全为 "?"/乱码替换符等无法识别字符时拒绝创建
        # （修复 "????????????????" 这类 PENDING 悬挂任务）
        _meaningful = re.sub(r"[?？\uFFFD\s\u3000]+", "", g)
        if len(_meaningful) < 2 or not re.search(
            r"[\u4e00-\u9fffA-Za-z0-9]", _meaningful,
        ):
            return self._json({
                "error": "目标内容无效（为空或包含无法识别的字符），请重新输入",
            }, 400)
        bad, reason = detect_injection(g)
        if bad:
            return self._json({"error": f"输入疑似包含恶意注入（{reason}），已拦截"}, 400)
        client_ip = self.client_address[0] if self.client_address else "?"
        # 本机/回环地址不限流（本地开发与演示不被误伤）
        if client_ip not in ("127.0.0.1", "::1") and not _get_rate_limiter().allow(client_ip):
            return self._json({"error": "请求过于频繁，请稍后再试"}, 429)
        g = sanitize_goal(g)
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
        # B3：提交前余额预检——双端点都余额不足时直接拒绝，避免任务跑一半
        # 全靠降级撑（实测主端点 402 切换 10 次）。预检有 30s TTL，不拖慢提交。
        # 余额不足时同步发布 alert 事件（Health 页可见），避免"静默耗尽"
        try:
            from llm_client import get_balance_status, endpoint_diversity_notice
            _bal = get_balance_status()
            _p = _bal.get("primary") or {}
            _b = _bal.get("backup") or {}
            _reasons = []
            if _p.get("reason") == "insufficient_balance":
                _reasons.append("主端点余额不足")
            if _b.get("reason") == "insufficient_balance":
                _reasons.append("备份端点余额不足")
            _div_notice = endpoint_diversity_notice()
            if _div_notice:
                _reasons.append(_div_notice)
            _reasons = _llm_precheck_notify(_reasons)
            if (
                _p.get("reason") == "insufficient_balance"
                and _b.get("reason") == "insufficient_balance"
            ):
                return self._json(
                    {"error": "全部 LLM 端点余额不足，请充值后重试"}, 503,
                )
        except Exception:
            pass
        conv_id = (body.get("conversation_id") or "").strip()
        parent_id = (body.get("parent_task_id") or "").strip()
        is_new_conversation = not conv_id
        if is_new_conversation:
            conv_id = "conv-" + uuid.uuid4().hex[:10]
        user_context = str(body.get("context") or "").strip()
        if user_context:
            bad, reason = detect_injection(user_context)
            if bad:
                return self._json({"error": f"上下文疑似包含恶意注入（{reason}），已拦截"}, 400)
        # Roadmap 余项⑤：报告语言（zh/en/ja…），默认中文；注入上下文头部
        report_lang = str(body.get("language") or "zh").strip()[:10]
        user_context = _build_lang_context(report_lang, user_context)
        conv_context = _build_conversation_context(conv_id) if not is_new_conversation else ""
        context = "\n\n".join(x for x in (user_context, conv_context) if x)
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
        project = _safe_project(str(body.get("project") or "default"))
        auto_run = bool(body.get("auto_run", True))
        report_confirm = bool(body.get("report_confirm", False))
        # 结果缓存：相同目标在 TTL 内已成功
        ttl = int(body.get("cache_ttl_min") or 0)
        if ttl > 0:
            cached = _find_cached_task(g, ttl, project, admin.get("user", ""), context)
            if cached and cached.get("report"):
                audit_log(admin.get("user", ""), self._client_ip(), "task.submit",
                          target=cached["task_id"], result="ok", detail=f"缓存命中: {g[:120]}")
                return self._json({
                    "task_id": cached["task_id"], "status": "SUCCESS",
                    "cached": True, "report": cached["report"],
                    "conversation_id": cached.get("conversation_id") or "",
                })
        try:
            submitted = _publish_task(
                goal=g,
                project=project,
                conversation_id=conv_id,
                parent_task_id=parent_id,
                context=context,
                auto_run=auto_run,
                template_steps=template_steps,
                user_id=str(body.get("user_id") or ""),
                report_confirm=report_confirm,
            )
            tid = submitted["task_id"]
        except RuntimeError as exc:
            audit_log(admin.get("user", ""), self._client_ip(), "task.submit",
                      target="?", result="fail", detail=f"Redis 发布失败: {g[:120]}")
            return self._json({"error": str(exc)}, 503)
        except Exception:
            audit_log(admin.get("user", ""), self._client_ip(), "task.submit",
                      target="?", result="fail", detail=f"任务派发异常: {g[:120]}")
            return self._json({"error": "任务派发失败"}, 500)
        audit_log(admin.get("user", ""), self._client_ip(), "task.submit",
                  target=tid, result="ok", detail=f"goal: {g[:120]}")
        return self._json(submitted)

def _post_memory_delete(self, p, body, admin):
    if self.path == "/api/memory/delete":
        # 记忆治理（对标标准 3.4）：按类型+ids 删除，或 {all: true} 清空
        mtype = str(body.get("type") or "").strip()
        ids = [str(x) for x in (body.get("ids") or []) if str(x).strip()]
        purge_all = bool(body.get("all"))
        purge_expired = bool(body.get("purge_expired"))
        if purge_expired:
            mem = _get_memory_manager()
            deleted = 0
            if mem is not None:
                deleted += mem.purge_expired()
                deleted += mem.enforce_conversation_cap()
            return self._json({"status": "ok", "deleted": int(deleted or 0),
                               "purge_expired": True})
        if mtype not in ("conversations", "strategies", "prompt_refinements"):
            return self._json({"error": "type 必须是 conversations/strategies/prompt_refinements"}, 400)
        if not ids and not purge_all and not (
            mtype == "prompt_refinements" and body.get("key")
        ):
            return self._json({"error": "需要 ids 或 all=true"}, 400)
        mem = _get_memory_manager()
        if mtype == "conversations":
            deleted = mem.delete_conversations(ids) if ids else mem.delete_all(mem._conversations)
        elif mtype == "strategies":
            deleted = mem.delete_strategies(ids) if ids else mem.delete_all(mem._strategies)
        else:
            deleted = mem.delete_prompt_refinements(
                {"key": str(body.get("key") or "")} if body.get("key") else None
            ) if not ids else mem.delete_by_ids(mem._prompt_refinements, ids)
        return self._json({"status": "ok", "deleted": int(deleted or 0)})

def _post_plan_confirm(self, p, body, admin):
    if self.path == "/api/plan/confirm":
        tid = str(body.get("task_id", "")).strip()
        if not tid:
            return self._json({"error": "task_id required"}, 400)
        action = body.get("action", "confirm")
        if not _redis_ready():
            return self._json({"error": "Redis 未连接，无法确认计划"}, 503)
        r = _new_redis()
        try:
            r.rpush(f"plan_confirm:{tid}", json.dumps({
                "action": action,
                "steps": body.get("steps"),
            }, ensure_ascii=False))
        except Exception:
            return self._json({"error": "Redis 写入失败，无法确认计划"}, 503)
        return self._json({"status": "ok"})

def _post_step_confirm(self, p, body, admin):
    if self.path == "/api/step/confirm":
        # 人机协作：确认/取消单个步骤（mode=human_in_loop）
        tid = str(body.get("task_id", "")).strip()
        sid = str(body.get("step_id", "")).strip()
        if not tid or not sid:
            return self._json({"error": "task_id 和 step_id 必填"}, 400)
        if not _redis_ready():
            return self._json({"error": "Redis 未连接，无法确认步骤"}, 503)
        r = _new_redis()
        try:
            r.rpush(f"step_confirm:{tid}:{sid}", json.dumps({
                "action": str(body.get("action") or "confirm"),
            }, ensure_ascii=False))
        except Exception:
            return self._json({"error": "Redis 写入失败，无法确认步骤"}, 503)
        return self._json({"status": "ok"})

def _post_context_extract(self, p, body, admin):
    if self.path == "/api/context/extract":
        filename = str(body.get("filename") or "").strip()
        b64 = str(body.get("data") or "")
        if not filename or not b64:
            return self._json({"error": "filename and data required"}, 400)
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return self._json({"error": "invalid base64"}, 400)
        if len(raw) > 3 * 1024 * 1024:
            return self._json({"error": "file too large (max 3MB)"}, 413)
        text = _extract_text_from_bytes(filename, raw)
        return self._json({
            "filename": filename,
            "text": text[:30000],
            "chars": len(text),
            "truncated": len(text) > 30000,
        })

def _post_evolution_trigger(self, p, body, admin):
    if self.path == "/api/evolution/trigger":
        if not _redis_ready():
            return self._json({"error": "Redis 未连接，无法触发进化"}, 503)
        r = _new_redis()
        try:
            r.publish("orchestrator:main", json.dumps({"task_id":"evo-"+str(int(time.time())),"goal":"EVOLUTION_TRIGGER"}, ensure_ascii=False))
        except Exception:
            return self._json({"error": "Redis 发布失败，无法触发进化"}, 503)
        return self._json({"status":"triggered"})

def _post_evolution_approve(self, p, body, admin):
    if self.path == "/api/evolution/approve":
        sid = str(body.get("strategy_id") or "").strip()
        approve = bool(body.get("approve", True))
        if not sid:
            return self._json({"error": "strategy_id required"}, 400)
        if not _redis_ready():
            return self._json({"error": "Redis 未连接，无法审批"}, 503)
        r = _new_redis()
        try:
            entries = r.lrange("evolution:pending", 0, -1)
            matched = None
            for raw in entries:
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if item.get("strategy_id") == sid:
                    matched = (raw, item)
                    break
            if not matched:
                return self._json({"error": "pending strategy not found"}, 404)
            raw, item = matched
            r.lrem("evolution:pending", 0, raw)
            deployed = False
            if approve:
                item["status"] = "deployed"
                # Roadmap 余项②：灰度比例（0-1，默认 1.0 全量；<1 时按任务哈希分流）
                try:
                    rollout = min(1.0, max(0.0, float(body.get("rollout", 1.0) or 1.0)))
                except (TypeError, ValueError):
                    rollout = 1.0
                item["rollout"] = rollout
                r.set(
                    f"strategy:active:{item.get('agent_type', 'search_agent')}",
                    json.dumps(item, ensure_ascii=False),
                )
                deployed = True
            return self._json({
                "status": "ok",
                "deployed": deployed,
                "rollout": item.get("rollout", 1.0),
                "agent_type": item.get("agent_type"),
            })
        except Exception as exc:
            return self._json({"error": f"approve failed: {exc}"}, 500)

def _post_single_agent(self, p, body, admin):
    if self.path == "/api/single-agent":
        g = body.get("goal","").strip()
        if not g: return self._json({"error":"goal required"},400)
        # B3：提交前余额预检——双端点都余额不足时直接拒绝，避免任务跑一半
        # 全靠降级撑（实测主端点 402 切换 10 次）。预检有 30s TTL，不拖慢提交。
        # 与 /task 路径同口径：余额不足/主备同源都会发布告警（不再静默放行）
        try:
            from llm_client import get_balance_status, endpoint_diversity_notice
            _bal = get_balance_status()
            _p = _bal.get("primary") or {}
            _b = _bal.get("backup") or {}
            _reasons = []
            if _p.get("reason") == "insufficient_balance":
                _reasons.append("主端点余额不足")
            if _b.get("reason") == "insufficient_balance":
                _reasons.append("备份端点余额不足")
            _div_notice = endpoint_diversity_notice()
            if _div_notice:
                _reasons.append(_div_notice)
            _llm_precheck_notify(_reasons)
            if (
                _p.get("reason") == "insufficient_balance"
                and _b.get("reason") == "insufficient_balance"
            ):
                return self._json(
                    {"error": "全部 LLM 端点余额不足，请充值后重试"}, 503,
                )
        except Exception:
            pass
        import time as _t
        start = _t.time()
        try:
            from llm_client import LLMClient
            llm = LLMClient()
            result = llm.call("Answer directly.", g, expect_json=False)
            text = (
                str((result or {}).get("content") or "")
                if isinstance(result, dict) else str(result or "")
            )
            return self._json({"result": text, "duration": round(_t.time()-start,1)})
        except Exception as e:
            return self._json({"result": f"Error: {e}", "duration": _t.time()-start})

def _post_quick_answer(self, p, body, admin):
    """T3 快答：先检索后作答——answers with sources, honest fallback.

    检索失败时返回 mode=model_knowledge 并在答案尾部声明"未检索"，
    不伪装可溯源。"""
    if self.path == "/api/quick-answer":
        g = str(body.get("goal") or "").strip()
        if not g:
            return self._json({"error": "goal required"}, 400)
        # 余额预检（同 single-agent）
        try:
            from llm_client import get_balance_status
            _bal = get_balance_status()
            _p = _bal.get("primary") or {}
            _b = _bal.get("backup") or {}
            if (
                _p.get("reason") == "insufficient_balance"
                and _b.get("reason") == "insufficient_balance"
            ):
                return self._json(
                    {"error": "全部 LLM 端点余额不足，请充值后重试"}, 503,
                )
        except Exception:
            pass
        import time as _t
        start = _t.time()
        # 1) 检索：Google News RSS（一次 HTTP，含 URL 的结果）
        sources: list[dict] = []
        search_ctx = ""
        mode = "model_knowledge"
        try:
            from adapters.news import fetch_news, fetch_news_fallback
            # Google News RSS 境内常不可达 → Bing/ddgs 文本检索兜底（同为 items 契约）
            news = fetch_news(g) or fetch_news_fallback(g)
            items = (news or {}).get("items") or []
            if items:
                mode = "searched"
                sources = [
                    {"title": str(i.get("title") or "")[:120],
                     "url": str(i.get("link") or "")}
                    for i in items[:5] if i.get("link")
                ]
                search_ctx = (
                    "以下是检索到的最新资讯（供引用，逐条附来源）：\n"
                    + "\n".join(
                        f"- {i.get('title')}（{i.get('link')}，{i.get('published','')}）"
                        for i in items[:5]
                    )
                    + "\n\n"
                )
        except Exception:
            sources = []
        # 2) 作答：带检索上下文要求引用来源
        try:
            from llm_client import LLMClient
            llm = LLMClient()
            system = (
                "你是快速问答助手。"
                + (
                    "回答末尾用一行列出参考来源（[来源] 标题 URL）。"
                    "只依据提供的资讯回答；资讯不足的部分明确说明。"
                    if mode == "searched"
                    else "本次未能检索到资讯：回答末尾必须声明"
                    "'（本次未检索到相关资讯，以上基于模型知识，未经验证）'。"
                )
            )
            content = llm.call(
                system, search_ctx + f"问题：{g}", expect_json=False,
            )
            # call(expect_json=False) 返回 {"content": str}；提取文本，
            # 否则前端把 dict 当字符串渲染会触发 React #31 崩溃
            content = (
                str((content or {}).get("content") or "")
                if isinstance(content, dict) else str(content or "")
            )
            return self._json({
                "content": content,
                "sources": sources,
                "mode": mode,
                "searched_at": _t.strftime("%Y-%m-%d %H:%M:%S") if mode == "searched" else "",
                "duration": round(_t.time() - start, 1),
            })
        except Exception as e:
            return self._json({
                "content": f"Error: {e}", "sources": [], "mode": mode,
                "duration": round(_t.time() - start, 1),
            })

def _post_kill_worker(self, p, body, admin):
    if self.path == "/api/kill-worker":
        agent_id = body.get("agent_id","")
        if not agent_id: return self._json({"error":"agent_id required"},400)
        if not _redis_ready():
            return self._json({"error": "Redis 未连接，无法停止 worker"}, 503)
        r = _new_redis()
        try:
            r.publish(f"agent.kill.{agent_id}", json.dumps({"action":"die"}))
        except Exception:
            return self._json({"error": "Redis 发布失败，无法停止 worker"}, 503)
        return self._json({"status":"killed","agent_id":agent_id})

def _post_config(self, p, body, admin):
    if self.path == "/api/config":
        _save_config(body)
        llm = body.get("llm",{})
        if llm.get("api_key"): os.environ["LLM_API_KEY"] = llm["api_key"]
        if llm.get("base_url"): os.environ["LLM_BASE_URL"] = llm["base_url"]
        if llm.get("model"): os.environ["LLM_MODEL"] = llm["model"]
        # 换 key/端点后余额预检缓存必须失效，否则 30s 内仍按旧凭据判定
        try:
            from llm_client import _clear_balance_cache
            _clear_balance_cache()
        except Exception:
            pass
        audit_log(admin.get("user", ""), self._client_ip(), "config.save", result="ok")
        return self._json({"status":"saved"})

def _post_verify(self, p, body, admin):
    """POST /api/verify：报告溯源体检（商业化 API 雏形）。

    body: {report_text: str, task_id?: str}
    - 仅 report_text：用空来源跑 数字溯源/来源声明/免责声明 三项检查；
    - 带 task_id（本系统任务）：收集该任务工作区来源，返回完整溯源结果。
    返回三档分类（引用/计算/模型知识）与缺口清单——给自媒体、内容团队、
    外部 AI 应用做"报告体检"，每千字计费可在此处接入。"""
    # body 已由 do_POST 解析并传入，此处不得再读 self.rfile（会阻塞）
    if not isinstance(body, dict):
        body = {}
    report_text = str(body.get("report_text") or "").strip()
    if len(report_text) < 50:
        return self._json({"error": "report_text 过短（至少 50 字符）"}, 400)
    tid = str(body.get("task_id") or "").strip()
    sources: dict = {}
    goal = str(body.get("goal") or "") or "report-verify"
    if tid:
        try:
            from acceptance_checker import _collect_sources
            from workspace import task_workspace
            sources = _collect_sources(task_workspace(tid))
            data = _get_task_report_data(tid)
            if data and data.get("goal"):
                goal = str(data["goal"])
        except Exception:
            pass
    try:
        from acceptance_checker import (
            check_disclaimer,
            check_number_traceability,
            check_source_labeling,
            traceability_domain,
        )
        domain = traceability_domain(goal)
        num = check_number_traceability(report_text, sources, domain=domain)
        label = check_source_labeling(report_text, sources)
        disc = check_disclaimer(report_text)
        _cited = int(num.get("cited_count") or 0)
        _computed = int(num.get("computed_count") or 0)
        _disclosed = int(num.get("disclosed_count") or 0)
        _total = int(num.get("total_count") or 0)
        _untraced = int(num.get("unverifiable_count") or 0)
        return self._json({
            "ok": True,
            "domain": domain,
            "numbers": {
                "total": _total,
                "cited": _cited,
                "computed": _computed,
                "disclosed_model_knowledge": _disclosed,
                "untraced": _untraced,
                "coverage_ratio": num.get("covered_ratio"),
            },
            "source_labeling": {
                "pass": bool(label.get("pass")),
                "checked": label.get("checked_count"),
                "mislabeled": (label.get("mislabeled") or [])[:10],
                "suggestions": (label.get("suggestions") or [])[:5],
            },
            "disclaimer": {"present": bool(disc.get("pass"))},
            "gaps": [
                g for g in (
                    [num.get("details") or ""] if not num.get("pass") else []
                ) + (
                    [label.get("details") or ""] if not label.get("pass") else []
                )
            ],
            "summary": (
                f"数字 {_total} 个：引用 {_cited} / 计算 {_computed} / "
                f"模型知识 {_disclosed} / 不可溯源 {_untraced}"
            ),
        })
    except Exception as exc:
        return self._json({"error": f"verify failed: {str(exc)[:120]}"}, 500)


def _post_llm_mode(self, p, body, admin):
    if self.path == "/api/llm-mode":
        # LLM 运行模式切换：cloud=全商业 API；hybrid=本地 LoRA 参与部分 Worker。
        # 写 Redis（worker 实时读）+ config.json（持久化重启后生效）。
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in ("cloud", "hybrid"):
            return self._json({"error": "mode must be cloud|hybrid"}, 400)
        try:
            if _redis_ready():
                _new_redis().set("llm_mode", mode)
        except Exception:
            pass
        cfg = _load_config() or {}
        cfg.setdefault("system", {})["llm_mode"] = mode
        _save_config(cfg)
        audit_log(admin.get("user", ""), self._client_ip(), "llm.mode",
                  result="ok", detail=mode)
        return self._json({"status": "ok", "mode": mode})

def _post_notifications(self, p, body, admin):
    if self.path == "/api/notifications":
        # F5：通知配置保存（仅 admin）：body 可直接是 notifications 段，
        # 也可包裹在 {"notifications": {...}} 中（与 GET 响应一致）
        from notifications import (
            load_notifications_config,
            public_notifications_config,
            save_notifications_config,
        )
        ncfg = body.get("notifications") if isinstance(
            body.get("notifications"), dict
        ) else body
        if not isinstance(ncfg, dict):
            return self._json({"error": "notifications 必须是对象"}, 400)
        if not save_notifications_config(ncfg, CONFIG_PATH):
            return self._json({"error": "保存配置失败"}, 500)
        audit_log(admin.get("user", ""), self._client_ip(),
                  "notifications.save", result="ok")
        return self._json({
            "status": "saved",
            "notifications": public_notifications_config(
                load_notifications_config(CONFIG_PATH)
            ),
        })

def _post_scheduled_jobs(self, p, body, admin):
    if self.path == "/api/scheduled-jobs":
        # F2：定时任务增删改（仅 admin）。body:
        #   {"action": "add"|"update"|"delete", "job": {...}, "name": "..."}
        from scheduled_jobs import load_jobs, normalize_job, save_jobs
        jobs = load_jobs(CONFIG_PATH)
        action = str(body.get("action") or "add").lower()
        name = str(body.get("name") or "").strip()
        if action == "delete":
            if not name:
                return self._json({"error": "name required"}, 400)
            jobs = [j for j in jobs if j.get("name") != name]
        else:
            job = normalize_job(body.get("job") if isinstance(
                body.get("job"), dict
            ) else body)
            if not job:
                return self._json({
                    "error": "job 需要 name/goal，且 interval_minutes 或 "
                             "cron(每日 HH:MM) 至少一项",
                }, 400)
            jobs = [j for j in jobs if j.get("name") != job["name"]]
            jobs.append(job)
        if not save_jobs(jobs, CONFIG_PATH):
            return self._json({"error": "保存配置失败"}, 500)
        audit_log(admin.get("user", ""), self._client_ip(),
                  "scheduled_jobs.save", target=name or "?", result="ok")
        return self._json({"status": "ok", "jobs": jobs})


# ============================================================
# 路由表（E 重构：do_GET/do_POST 业务分支表化，保持原 if 顺序）
# ============================================================
_GET_ROUTES = [
    (lambda self, p: p == "/api/health", _get_health),
    (lambda self, p: p == "/api/auth/bootstrap", _get_bootstrap),
    (lambda self, p: p == "/api/audit", _get_audit),
    (lambda self, p: p == "/", _get_root),
    (lambda self, p: p == "/api/status", _get_status),
    (lambda self, p: p == "/api/projects", _get_projects),
    (lambda self, p: p == "/api/scheduled-jobs", _get_scheduled_jobs),
    (lambda self, p: p.startswith("/files/"), _get_files),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/acceptance/timeline"), _get_task_acceptance_timeline),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/acceptance"), _get_task_acceptance),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/deliverables"), _get_task_deliverables),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/usage"), _get_task_usage),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/stream"), _get_task_stream),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/events"), _get_task_events),
    (lambda self, p: p.startswith("/api/task/") and p.endswith("/pdf"), _get_task_pdf),
    (lambda self, p: p == "/api/config", _get_config),
    (lambda self, p: p == "/api/llm-mode", _get_llm_mode),
    (lambda self, p: p == "/api/notifications", _get_notifications),
    (lambda self, p: p == "/api/events", _get_events),
    (lambda self, p: p == "/api/memory", _get_memory),
    (lambda self, p: p == "/api/memory/summary", _get_memory_summary_route),
    (lambda self, p: p == "/api/templates", _get_templates),
    (lambda self, p: p == "/api/evals", _get_evals),
    (lambda self, p: p == "/api/acceptance/suggestions", _get_acceptance_suggestions),
    (lambda self, p: p == "/api/skills", _get_skills),
    (lambda self, p: p == "/api/tool-audit", _get_tool_audit),
    (lambda self, p: p == "/api/evolution", _get_evolution),
    (lambda self, p: p == "/api/evolution/pending", _get_evolution_pending),
    (lambda self, p: p == "/api/metrics", _get_metrics),
    (lambda self, p: p == "/tasks", _get_tasks),
    (lambda self, p: p == "/api/conversations", _get_conversations),
    (lambda self, p: p.startswith("/api/conversations/"), _get_conversation_detail),
    (lambda self, p: p.startswith("/api/share/"), _get_share_data),
    (lambda self, p: p.startswith("/share/"), _get_share_page),
    (lambda self, p: p.startswith("/task/") and p.endswith("/report"), _get_task_report),
    (lambda self, p: p.startswith("/task/"), _get_task_page),
]

_POST_ROUTES = [
    (lambda self, p: self.path == "/api/share" or self.path.startswith("/api/share/"), _post_share),
    (lambda self, p: self.path == "/api/deliverable/run", _post_deliverable_run),
    (lambda self, p: self.path == "/task", _post_task),
    (lambda self, p: self.path == "/api/memory/delete", _post_memory_delete),
    (lambda self, p: self.path == "/api/plan/confirm", _post_plan_confirm),
    (lambda self, p: self.path == "/api/step/confirm", _post_step_confirm),
    (lambda self, p: self.path == "/api/context/extract", _post_context_extract),
    (lambda self, p: self.path == "/api/evolution/trigger", _post_evolution_trigger),
    (lambda self, p: self.path == "/api/evolution/approve", _post_evolution_approve),
    (lambda self, p: self.path == "/api/single-agent", _post_single_agent),
    (lambda self, p: self.path == "/api/quick-answer", _post_quick_answer),
    (lambda self, p: self.path == "/api/kill-worker", _post_kill_worker),
    (lambda self, p: self.path == "/api/config", _post_config),
    (lambda self, p: self.path == "/api/llm-mode", _post_llm_mode),
    (lambda self, p: self.path == "/api/notifications", _post_notifications),
    (lambda self, p: self.path == "/api/scheduled-jobs", _post_scheduled_jobs),
    (lambda self, p: self.path == "/api/verify", _post_verify),
]


if __name__ == "__main__":
    main()
