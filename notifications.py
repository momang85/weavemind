# -*- coding: utf-8 -*-
"""任务完成外部通知（F5）。

统一入口 notify_task_done(task_id, goal, status, report_link, summary)：
- webhook：POST JSON 到 url（通用，30s 超时）
- serverchan：POST https://sctapi.ftqq.com/<sendkey>.send
- email：smtp 配置（host/port/user/password/from/to）

每个渠道有 enabled 开关；发送失败只记日志，绝不阻塞任务完成流程。
配置存放在 config.json 的 notifications 段，由 web_ui 的
GET/POST /api/notifications（仅 admin）读写。
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import threading
from email.mime.text import MIMEText
from email.utils import formataddr

logger = logging.getLogger("notifications")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SHARE_FILE = os.environ.get(
    "SHARE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "share_links.json"),
)

# 渠道默认值：新增渠道在此登记，读取配置时自动补齐缺失字段
DEFAULT_NOTIFICATIONS = {
    "webhook": {
        "enabled": False,
        "url": "",
    },
    "serverchan": {
        "enabled": False,
        "sendkey": "",
    },
    "email": {
        "enabled": False,
        "host": "",
        "port": 465,
        "user": "",
        "password": "",
        "from": "",
        "to": [],
    },
}

# 敏感字段：GET /api/notifications 回显时置空，防止密码/密钥泄露到前端
SECRET_FIELDS = {"password", "sendkey"}


def load_notifications_config(cfg_path: str | None = None) -> dict:
    """读取 config.json 的 notifications 段；文件缺失/损坏返回全禁用默认。"""
    path = cfg_path or CONFIG_PATH
    section: dict | None = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            section = cfg.get("notifications")
    except Exception:
        section = None
    out: dict[str, dict] = {}
    for key, default in DEFAULT_NOTIFICATIONS.items():
        item = section.get(key) if isinstance(section, dict) else None
        if isinstance(item, dict):
            merged = dict(default)
            merged.update(item)
            out[key] = merged
        else:
            out[key] = dict(default)
    return out


def save_notifications_config(cfg: dict, cfg_path: str | None = None) -> bool:
    """把 notifications 段合并写回 config.json；返回是否成功。"""
    path = cfg_path or CONFIG_PATH
    try:
        existing = {}
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
        existing_section = existing.get("notifications")
        if not isinstance(existing_section, dict):
            existing_section = {}
        merged: dict[str, dict] = {}
        for key, value in (cfg or {}).items():
            if key not in DEFAULT_NOTIFICATIONS:
                continue
            item = dict(value) if isinstance(value, dict) else dict(DEFAULT_NOTIFICATIONS[key])
            # GET 回显已剥离密码/密钥；前端改其他字段时不得把密钥清空，
            # 只有显式提交非空新值才更新
            for secret in SECRET_FIELDS:
                if secret in item and not item.get(secret):
                    prev = (existing_section.get(key) or {}).get(secret)
                    if prev:
                        item[secret] = prev
            merged[key] = item
        existing["notifications"] = {
            key: merged[key] for key in DEFAULT_NOTIFICATIONS if key in merged
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.warning("save notifications config failed: %s", str(exc)[:150])
        return False


def public_notifications_config(cfg: dict | None = None) -> dict:
    """返回给前端的通知配置副本：剥离密码/密钥字段，回显为已配置状态。"""
    cfg = cfg if cfg is not None else load_notifications_config()
    out: dict[str, dict] = {}
    for key, item in cfg.items():
        item = dict(item or {})
        for secret in SECRET_FIELDS:
            if secret in item:
                item[secret] = ""
        out[key] = item
    return out


def find_share_link(task_id: str, share_file: str | None = None) -> str:
    """在 share_links.json 中查找任务已生成的分享路径；未分享返回空串。
    仅复用已有分享，绝不自动创建（任务未分享则不附链接，只附摘要）。"""
    path = share_file or SHARE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        for token, info in data.items():
            if isinstance(info, dict) and info.get("task_id") == task_id:
                return f"/share/{token}"
    except Exception:
        pass
    return ""


def make_summary(report: str, max_chars: int = 300) -> str:
    """从报告正文提取通知摘要：去掉 Markdown 语法与空行，截断到 max_chars。"""
    text = str(report or "")
    # 去掉行内 Markdown 链接/图片/加粗/行内代码，避免摘要满是语法符号
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_`|~-]{1,}", " ", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " ".join(lines)
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1] + "…"
    return summary


def _send_webhook(cfg: dict, payload: dict) -> bool:
    """通用 webhook：POST JSON 到 url（30s 超时）。"""
    import requests

    url = str(cfg.get("url") or "").strip()
    if not url:
        logger.warning("notifications.webhook: url 未配置，跳过")
        return False
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return True


def _send_serverchan(cfg: dict, payload: dict) -> bool:
    """Server酱：POST https://sctapi.ftqq.com/<sendkey>.send。
    title=任务完成，desp=摘要+链接。"""
    import requests

    sendkey = str(cfg.get("sendkey") or "").strip()
    if not sendkey:
        logger.warning("notifications.serverchan: sendkey 未配置，跳过")
        return False
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    goal = str(payload.get("goal") or "")
    title = f"任务完成：{goal[:40]}" if goal else "任务完成"
    desp_parts = [
        str(payload.get("summary") or ""),
        f"任务ID：{payload.get('task_id') or ''}",
        f"状态：{payload.get('status') or ''}",
    ]
    if payload.get("link"):
        desp_parts.append(f"报告链接：{payload['link']}")
    resp = requests.post(
        url,
        data={
            "title": title,
            "desp": "\n\n".join(p for p in desp_parts if p),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return True


def _send_email(cfg: dict, payload: dict) -> bool:
    """SMTP 邮件：正文=摘要+链接；支持 SSL(465) 与 STARTTLS(其他端口)。"""
    host = str(cfg.get("host") or "").strip()
    if not host:
        logger.warning("notifications.email: host 未配置，跳过")
        return False
    user = str(cfg.get("user") or "").strip()
    password = str(cfg.get("password") or "")
    sender = str(cfg.get("from") or user or "").strip()
    to_raw = cfg.get("to") or []
    if isinstance(to_raw, str):
        to_list = [t.strip() for t in to_raw.replace(";", ",").split(",") if t.strip()]
    else:
        to_list = [str(t).strip() for t in to_raw if str(t).strip()]
    if not sender or not to_list:
        logger.warning("notifications.email: from/to 未配置，跳过")
        return False
    port = int(cfg.get("port") or 465)
    goal = str(payload.get("goal") or "")
    title = f"任务完成：{goal[:40]}" if goal else "任务完成"
    body_parts = [
        str(payload.get("summary") or ""),
        f"任务ID：{payload.get('task_id') or ''}",
        f"状态：{payload.get('status') or ''}",
    ]
    if payload.get("link"):
        body_parts.append(f"报告链接：{payload['link']}")
    msg = MIMEText("\n\n".join(body_parts), "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = formataddr(("WeaveMind", sender))
    msg["To"] = ", ".join(to_list)
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        try:
            smtp.starttls()
        except smtplib.SMTPException:
            pass
    try:
        if user:
            smtp.login(user, password)
        smtp.sendmail(sender, to_list, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return True


def notify_task_done(
    task_id: str,
    goal: str = "",
    status: str = "",
    report_link: str = "",
    summary: str = "",
    cfg: dict | None = None,
) -> dict:
    """按 config.json 的 notifications 配置分发任务完成通知。
    每个渠道独立 try/except：任一渠道失败只记日志，不影响其他渠道与任务流程。
    返回 {sent, failed, skipped} 供测试断言。"""
    cfg = cfg if cfg is not None else load_notifications_config()
    payload = {
        "task_id": task_id,
        "goal": goal,
        "status": status,
        "link": report_link or "",
        "summary": summary or make_summary(""),
    }
    sent: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for key, sender in (
        ("webhook", _send_webhook),
        ("serverchan", _send_serverchan),
        ("email", _send_email),
    ):
        item = cfg.get(key) or {}
        if not item.get("enabled"):
            skipped.append(key)
            continue
        try:
            if sender(item, payload):
                sent.append(key)
                logger.info("notification %s sent for task %s", key, task_id)
            else:
                skipped.append(key)
        except Exception as exc:
            failed.append(key)
            logger.warning(
                "notification %s failed for task %s: %s",
                key, task_id, str(exc)[:200],
            )
    return {"sent": sent, "failed": failed, "skipped": skipped}


def notify_task_done_async(
    task_id: str,
    goal: str = "",
    status: str = "",
    report_link: str = "",
    summary: str = "",
) -> None:
    """后台线程分发通知：不阻塞任务完成流程（orchestrator 接线用）。"""
    threading.Thread(
        target=notify_task_done,
        args=(task_id, goal, status, report_link, summary),
        daemon=True,
        name=f"notify-{task_id}",
    ).start()
