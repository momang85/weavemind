# -*- coding: utf-8 -*-
"""本机密钥仓（credentials.json）：设置页里新增的密钥**不再写进 config.json**。

为什么要单独一个文件（项目积压 #1 的落地起点）：`config.json` 一直被明文装着
LLM/embedding 的 api_key，而它同时是"可分享、可备份、可提交模板"的配置载体——
备份文件（`config.json.bak.*`）与截图教程都会把密钥一起带出去。数据源密钥
（Wind/iFinD/自建 API）比模型 key 更敏感：它绑着**付费许可**，泄露的代价不只是扣费。

四条边界：

1. **只存密钥，不存配置**：本文件的内容是 `{"secrets": {"点号路径": "值"}}`。
   非密钥字段（base_url、市场、指标名）仍在 config.json，二者用同一条点号路径对应，
   例如配置里的 `data_sources[i].id="wind"` ↔ 密钥路径 `data_sources.wind.api_key`。
2. **永不下发**：HTTP 层（`/api/config`、`/api/config/requirements`）只回"是否已配置"，
   明文只在本进程内被取数通道读取；本模块不提供批量导出接口。
3. **不进 git、尽量不进别的进程**：`credentials.json` 已加入 .gitignore；POSIX 下写盘
   即 `0600`（Windows 由 ACL 决定，这里尽力 `os.chmod`，失败不报错）。
4. **读取失败不等于密钥为空**：文件损坏/权限异常记 WARNING 并按空处理，但**不覆盖**
   原文件（只有显式写入才会落盘），避免"配置页保存一次就把所有密钥清掉"。

线程：写入用进程内锁 + 临时文件 `os.replace` 原子替换；跨进程并发写不是本模块的目标
（个人版只有 webui 进程写）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CACHE: dict = {"mtime": None, "size": None, "secrets": {}}

COMMENT = (
    "本机密钥仓：由设置页/服务端写入，请勿提交到 git、请勿截图分享。"
    "结构为 {\"secrets\": {\"data_sources.<id>.api_key\": \"...\"}}；"
    "HTTP 接口只回「是否已配置」，不回明文。"
)


def credentials_path() -> Path:
    """密钥仓路径：`WEAVEMIND_CREDENTIALS` 优先，否则与 config.json 同目录。"""
    env = os.environ.get("WEAVEMIND_CREDENTIALS") or ""
    if env.strip():
        return Path(env.strip()).expanduser().resolve()
    cfg_env = os.environ.get("WEAVEMIND_CONFIG") or ""
    base = (Path(cfg_env).expanduser().resolve().parent if cfg_env.strip()
            else Path(__file__).resolve().parent)
    return base / "credentials.json"


def _read_raw() -> dict:
    path = credentials_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    except Exception as exc:                        # pragma: no cover - 权限/奇异路径
        logger.warning("密钥仓不可读（按空处理，不改写原文件）：%s", str(exc)[:120])
        return {}
    if _CACHE["mtime"] == st.st_mtime and _CACHE["size"] == st.st_size:
        return _CACHE["secrets"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("密钥仓解析失败（按空处理，不改写原文件）：%s", str(exc)[:120])
        return {}
    secrets = data.get("secrets") if isinstance(data, dict) else None
    out = secrets if isinstance(secrets, dict) else {}
    _CACHE.update({"mtime": st.st_mtime, "size": st.st_size, "secrets": dict(out)})
    return _CACHE["secrets"]


def _write_raw(secrets: dict) -> bool:
    path = credentials_path()
    payload = {"_comment": COMMENT, "secrets": dict(secrets)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".credentials.", suffix=".tmp",
                                   dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except Exception:                            # Windows：ACL 由系统决定，尽力而为
            pass
        os.replace(tmp, path)
        _CACHE.update({"mtime": None, "size": None, "secrets": dict(secrets)})
        try:
            st = path.stat()
            _CACHE["mtime"], _CACHE["size"] = st.st_mtime, st.st_size
        except Exception:                            # pragma: no cover
            pass
        return True
    except Exception as exc:
        logger.warning("密钥仓写入失败：%s", str(exc)[:140])
        try:
            os.unlink(tmp)                           # type: ignore[misc]
        except Exception:
            pass
        return False


def get_secret(path: str, default: str = "") -> str:
    """按点号路径取密钥；没有就回 default（空串），不抛。"""
    with _LOCK:
        value = _read_raw().get(str(path or ""))
    return str(value) if value else default


def set_secret(path: str, value: str) -> bool:
    """写入/覆盖一条密钥。空值等同于删除（调用方不必自己判分支）。"""
    key = str(path or "").strip()
    if not key:
        return False
    with _LOCK:
        secrets = dict(_read_raw())
        text = str(value or "")
        if text:
            secrets[key] = text
        else:
            secrets.pop(key, None)
        return _write_raw(secrets)


def delete_secret(path: str) -> bool:
    key = str(path or "").strip()
    if not key:
        return False
    with _LOCK:
        secrets = dict(_read_raw())
        if key not in secrets:
            return True
        secrets.pop(key, None)
        return _write_raw(secrets)


def has_secret(path: str) -> bool:
    return bool(get_secret(path))


def configured_paths(prefix: str = "") -> list[str]:
    """已配置密钥的路径清单（**只有路径，没有值**），供设置页显示"已配置"。"""
    with _LOCK:
        keys = list(_read_raw().keys())
    pre = str(prefix or "")
    return sorted(k for k in keys if not pre or k.startswith(pre))


def secret_state(path: str) -> dict:
    """给 HTTP 层的"是否已配置"标记（永不含值）。"""
    return {"set": has_secret(path)}


def forget_prefix(prefix: str) -> int:
    """删除某个前缀下的全部密钥（数据源被删除时用，避免留下孤儿凭据）。"""
    pre = str(prefix or "")
    if not pre:
        return 0
    with _LOCK:
        secrets = dict(_read_raw())
        gone = [k for k in secrets if k == pre or k.startswith(pre + ".")]
        if not gone:
            return 0
        for k in gone:
            secrets.pop(k, None)
        _write_raw(secrets)
        return len(gone)


def reset_cache_for_test() -> None:
    with _LOCK:
        _CACHE.update({"mtime": None, "size": None, "secrets": {}})
