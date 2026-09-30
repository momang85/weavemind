# -*- coding: utf-8 -*-
"""可信凭据接缝（阶段 T0-a）：服务端可验证的会话与分享授权。

为什么单独成模块：此前“分享放行”只是一个客户端常量 Cookie
（`share_<token>=ok`），登出只删进程内字典、角色只在会话签发时缓存进内存。
阶段T 把它收成一份服务端真源：

- **会话**：SQLite 行 + 账户“会话代次”（gen）。登出在库里删行；改密/降权/删号
  递增代次，签发时代次不符的旧会话一律失效。内存字典降级为缓存。
- **角色**：不信任签发时的缓存；调用方每次按当前账户配置重新取。
- **分享授权**：口令验证成功后签发**服务端随机**凭据（库内只存 SHA-256），
  绑定 share_id、到期时间与分享代次；改密/撤销立即失效，跨分享不可用。

本模块只依赖标准库与一个 SQLite 路径（调用方传 `web_ui.DB_PATH`），**不 import
web_ui**，因此可以用隔离库直接验收。表在首次使用时就地创建且幂等，与 webui
的 `_init_db` 谁先发生都不冲突。

失败语义：读不到真源一律按“未知”拒绝，不拿内存缓存冒充有效凭据。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

SESSION_TTL_DEFAULT = 86400

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sessions("
    "token TEXT PRIMARY KEY, user TEXT, role TEXT, expires REAL, "
    "gen INTEGER NOT NULL DEFAULT 0, issued REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS auth_principal("
    "principal TEXT PRIMARY KEY, gen INTEGER NOT NULL DEFAULT 0, updated REAL)",
    "CREATE TABLE IF NOT EXISTS share_grants("
    "grant_hash TEXT PRIMARY KEY, share_id TEXT NOT NULL, "
    "gen INTEGER NOT NULL DEFAULT 0, issued REAL NOT NULL DEFAULT 0, "
    "expires REAL NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_share_grants_share ON share_grants(share_id)",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


_ensured: set[str] = set()
_ensured_lock = threading.Lock()


def _ensure_once(db_path: str) -> None:
    """每个库路径每进程只建一次表（幂等；避免每次调用都跑 DDL）。"""
    key = os.path.abspath(str(db_path))
    if key in _ensured:
        return
    with _ensured_lock:
        if key in _ensured:
            return
        ensure_schema(db_path)
        _ensured.add(key)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def ensure_schema(db_path: str) -> None:
    """建表 + 给既有 `sessions` 表补 gen/issued 列（老库就地升级，幂等）。"""
    conn = _connect(db_path)
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        cols = _columns(conn, "sessions")
        if "gen" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN gen INTEGER NOT NULL DEFAULT 0")
        if "issued" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN issued REAL NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


# ---- 主体代次（改密/降权/删号递增；分享改密/撤销递增） ----

def _generation_conn(conn: sqlite3.Connection, principal: str) -> int:
    row = conn.execute(
        "SELECT gen FROM auth_principal WHERE principal=?", (str(principal),)
    ).fetchone()
    return int(row["gen"]) if row is not None else 0


def generation_of(db_path: str, principal: str) -> int:
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        return _generation_conn(conn, principal)
    finally:
        conn.close()


def bump_generation(db_path: str, principal: str) -> int:
    """原子递增并返回新代次（跨进程只有一条真源）。"""
    principal = str(principal)
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO auth_principal(principal, gen, updated) VALUES(?,1,?) "
            "ON CONFLICT(principal) DO UPDATE SET gen = auth_principal.gen + 1, "
            "updated = excluded.updated",
            (principal, time.time()),
        )
        gen = _generation_conn(conn, principal)
        conn.commit()
        return gen
    finally:
        conn.close()


# ---- 会话 ----

def create_session(db_path: str, user: str, role: str,
                   ttl_seconds: float, gen: int | None = None) -> str:
    """签发会话（可持久）。gen 缺省取该账户当前代次。"""
    _ensure_once(db_path)
    user = str(user)
    if gen is None:
        gen = generation_of(db_path, user)
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + float(ttl_seconds)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions(token, user, role, expires, gen, issued) "
            "VALUES(?,?,?,?,?,?)",
            (token, user, str(role or ""), expires, int(gen), now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_session(db_path: str, token: str) -> dict | None:
    _ensure_once(db_path)
    if not token:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT token, user, role, expires, gen, issued FROM sessions WHERE token=?",
            (str(token),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "token": row["token"], "user": row["user"], "role": row["role"],
        "expires": float(row["expires"] or 0), "gen": int(row["gen"] or 0),
        "issued": float(row["issued"] or 0),
    }


def list_live_sessions(db_path: str, now: float | None = None) -> list[dict]:
    """未过期会话（启动时重建内存缓存用）。"""
    now = time.time() if now is None else now
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT token, user, role, expires, gen, issued FROM sessions WHERE expires > ?",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"token": r["token"], "user": r["user"], "role": r["role"],
         "expires": float(r["expires"] or 0), "gen": int(r["gen"] or 0),
         "issued": float(r["issued"] or 0)}
        for r in rows
    ]


def verify_session_row(db_path: str, token: str) -> dict | None:
    """一次读拿到"会话行 + 该账户当前代次"（每请求只查一次真源）。

    返回 None 表示会话行不存在（已登出/已撤销/从未签发）；账户仍在、代次是否
    相符由调用方按当前账户配置判定。`current_gen` 为该账户当前代次（无记录=0）。
    """
    _ensure_once(db_path)
    if not token:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT s.token, s.user, s.role, s.expires, s.gen, s.issued, "
            "       p.gen AS current_gen "
            "FROM sessions s LEFT JOIN auth_principal p ON p.principal = s.user "
            "WHERE s.token=?",
            (str(token),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "token": row["token"], "user": row["user"], "role": row["role"],
        "expires": float(row["expires"] or 0), "gen": int(row["gen"] or 0),
        "issued": float(row["issued"] or 0),
        "current_gen": int(row["current_gen"] or 0),
    }


def delete_session(db_path: str, token: str) -> int:
    """持久撤销单个会话（登出/失效清理都走这里）。"""
    _ensure_once(db_path)
    if not token:
        return 0
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM sessions WHERE token=?", (str(token),))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def delete_sessions_for(db_path: str, user: str) -> int:
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM sessions WHERE user=?", (str(user),))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def revoke_account(db_path: str, user: str) -> dict:
    """改密/降权/删号：递增代次并删掉该账户全部会话（一次事务）。"""
    user = str(user)
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO auth_principal(principal, gen, updated) VALUES(?,1,?) "
            "ON CONFLICT(principal) DO UPDATE SET gen = auth_principal.gen + 1, "
            "updated = excluded.updated",
            (user, time.time()),
        )
        removed = int(
            conn.execute("DELETE FROM sessions WHERE user=?", (user,)).rowcount or 0
        )
        gen = _generation_conn(conn, user)
        conn.commit()
        return {"gen": gen, "sessions_removed": removed}
    finally:
        conn.close()


def purge_expired(db_path: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        removed = int(
            conn.execute("DELETE FROM sessions WHERE expires <= ?", (now,)).rowcount or 0
        )
        conn.execute("DELETE FROM share_grants WHERE expires <= ?", (now,))
        conn.commit()
        return removed
    finally:
        conn.close()


# ---- 分享授权（口令验证通过后的服务端凭据） ----

def _hash_grant(grant: str) -> str:
    return hashlib.sha256(str(grant).encode("utf-8")).hexdigest()


def issue_share_grant(db_path: str, share_id: str, ttl_seconds: float) -> str:
    """签发分享授权凭据；库内只存哈希，返回明文一次。"""
    _ensure_once(db_path)
    share_id = str(share_id)
    grant = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + max(0.0, float(ttl_seconds))
    gen = generation_of(db_path, f"share:{share_id}")
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO share_grants"
            "(grant_hash, share_id, gen, issued, expires) VALUES(?,?,?,?,?)",
            (_hash_grant(grant), share_id, int(gen), now, expires),
        )
        conn.commit()
    finally:
        conn.close()
    return grant


def verify_share_grant(db_path: str, share_id: str, grant: str,
                       now: float | None = None) -> bool:
    """凭据是否对该分享有效：存在、未过期、属该分享、代次未变。"""
    if not grant or not share_id:
        return False
    now = time.time() if now is None else now
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT share_id, gen, expires FROM share_grants WHERE grant_hash=?",
            (_hash_grant(grant),),
        ).fetchone()
        if row is None:
            return False
        if not hmac.compare_digest(str(row["share_id"]), str(share_id)):
            return False
        if float(row["expires"] or 0) <= now:
            return False
        if int(row["gen"] or 0) != _generation_conn(conn, f"share:{share_id}"):
            return False
        return True
    finally:
        conn.close()


def revoke_share_grants(db_path: str, share_id: str) -> dict:
    """分享改密/撤销：递增分享代次并删掉已下发凭据。"""
    share_id = str(share_id)
    _ensure_once(db_path)
    conn = _connect(db_path)
    try:
        principal = f"share:{share_id}"
        conn.execute(
            "INSERT INTO auth_principal(principal, gen, updated) VALUES(?,1,?) "
            "ON CONFLICT(principal) DO UPDATE SET gen = auth_principal.gen + 1, "
            "updated = excluded.updated",
            (principal, time.time()),
        )
        removed = int(
            conn.execute("DELETE FROM share_grants WHERE share_id=?", (share_id,)).rowcount or 0
        )
        gen = _generation_conn(conn, principal)
        conn.commit()
        return {"gen": gen, "grants_removed": removed}
    finally:
        conn.close()
