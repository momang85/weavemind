# -*- coding: utf-8 -*-
"""补材料入口的摄取/准入服务（C1）：网页直链与上传文件走**同一条**准入链。

资料获取顺序里的"人工补充"这一段。用户在任务页给出披露直链或上传文件后：
原始内容按内容寻址落盘 → 经 `adapters.disclosure_ingest` 的判据（主体/期间/披露日/
正文完整性）判定 → 准入通过的正文并入任务既有的 `project/fetch_snapshot.json`。
证据、引用、来源清单读的都是这一份快照，**不另建证据库**。

三条边界：

1. **身份可核对**：原件候选、准入结论、可定位、支持结论分别记状态；原始内容 hash、
   正文 hash、解析版本、真实主体/期间/日期依据都写进材料清单。规则版本变化时旧
   `admitted` 不复用——`admission_rules_version()` 对不上就重跑准入。
2. **上传不是服务器读文件**：网页上传只接受请求体里的字节，存储路径由本模块生成，
   文件名只当标签。本地调试读文件是另一条通道（`local_file`），与网页上传在清单里
   分开记，不互相冒充。
3. **不默认重跑整任务**：材料变化后只重做**确定性**的下游（证据 → 结构 → 底稿），
   需要模型重新生成的步骤记成待办（等待既有预算/授权），不在本模块发起。
"""

from __future__ import annotations

import transfer_limits

import hashlib
import json
import logging
import time
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

MATERIALS_DIR = "materials"
INDEX_FILE = "index.json"
META_FILE = "meta.json"
DOC_FILE = "doc.json"

# 解析/摄取实现版本：与原件一起记进清单，解析方式变了才能发现"同一份原件出的正文不同"
PARSER_VERSION = "intake-1"

# 容量策略的**单一来源**（transfer_limits，可用环境变量覆盖）：
# 上传/下载/抓取三处此前各自硬编码，同一个文件会出现「这个入口能进、那个入口被拒」。
MAX_BYTES = transfer_limits.UPLOAD_MAX_BYTES   # 单件上限（与 /api/context/extract 同口径）
MAX_PAGES = transfer_limits.PDF_MAX_PAGES      # 与 annual_report_pdf.MAX_PAGES 同源
MIN_TEXT_CHARS = 40

CHANNEL_LINK = "web_link"
CHANNEL_UPLOAD = "web_upload"
CHANNEL_LOCAL = "local_file"
CHANNELS = (CHANNEL_LINK, CHANNEL_UPLOAD, CHANNEL_LOCAL)
CHANNEL_LABEL = {
    CHANNEL_LINK: "网页提交的披露直链（由服务取件）",
    CHANNEL_UPLOAD: "网页上传的文件（请求体字节）",
    CHANNEL_LOCAL: "本地调试读入的文件（不经网页上传）",
}

KIND_PDF = "pdf"
KIND_HTML = "html"
KIND_TEXT = "text"
KIND_JSON = "json"
ALLOWED_KINDS = (KIND_PDF, KIND_HTML, KIND_TEXT, KIND_JSON)
_KIND_EXT = {KIND_PDF: "pdf", KIND_HTML: "html", KIND_TEXT: "txt", KIND_JSON: "json"}
_KIND_LABEL = {KIND_PDF: "PDF", KIND_HTML: "网页(HTML)", KIND_TEXT: "纯文本",
               KIND_JSON: "JSON"}

STATE_PENDING = "pending_intake"     # 已保存原件，等编排器摄取
STATE_ADMITTED = "admitted"
STATE_REJECTED = "rejected"
STATE_FETCH_FAILED = "fetch_failed"

# 不得接受的容器/二进制：不接受执行压缩内容（专项 C1）
_ARCHIVE_MAGIC = ((b"PK\x03\x04", "ZIP"), (b"\x1f\x8b", "GZIP"), (b"7z\xbc\xaf", "7Z"),
                  (b"Rar!\x1a\x07", "RAR"), (b"BZh", "BZIP2"), (b"\xfd7zXZ", "XZ"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _task_ws(task_id: str, ws_dir=None) -> Path:
    if ws_dir:
        return Path(ws_dir)
    import workspace
    return Path(workspace.task_workspace(task_id))


def materials_dir(task_id: str, *, ws_dir=None, project=None) -> Path:
    """材料目录：跟着任务工作区走（`project/materials/`）。"""
    if project:
        import workspace
        base = Path(workspace.task_project_dir(task_id, project))
    else:
        base = _task_ws(task_id, ws_dir)
        if not str(base).endswith("project"):
            base = base / "project"
    return base / MATERIALS_DIR


def _meta_path(task_id: str, mid: str, *, ws_dir=None, project=None) -> Path:
    return materials_dir(task_id, ws_dir=ws_dir, project=project) / str(mid) / META_FILE


def raw_sha256(raw: bytes) -> str:
    return hashlib.sha256(bytes(raw or b"")).hexdigest()


def mid_for(*, channel: str, url: str = "", filename: str = "",
            raw_sha256: str = "") -> str:
    """材料身份（幂等键）：直链按 URL，文件按内容 hash。

    同一份材料重复提交只产生一条记录——重复上传不该让同一份年报在证据里出现两次，
    也不该让"再传一次"变成第二次取件。
    """
    if str(channel) == CHANNEL_LINK:
        key = f"{CHANNEL_LINK}|{str(url or '').strip()}"
    else:
        key = (f"{str(channel)}|{str(filename or '').strip().lower()}"
               f"|{str(raw_sha256 or '')}")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def sniff_kind(*, filename: str = "", content_type: str = "", raw: bytes = b"") -> str:
    """内容优先、其次文件名/声明类型。字节头判 PDF 只看魔数，不看文件名。"""
    head = bytes((raw or b"")[:1024])
    if head[:5] == b"%PDF-":
        return KIND_PDF
    name = str(filename or "").lower()
    ctype = str(content_type or "").lower()
    if name.endswith(".pdf") or "application/pdf" in ctype:
        return KIND_PDF
    if name.endswith((".html", ".htm", ".xhtml")) or "html" in ctype:
        return KIND_HTML
    if name.endswith(".json") or "json" in ctype:
        return KIND_JSON
    if name.endswith((".txt", ".md", ".csv", ".text")) or ctype.startswith("text/"):
        return KIND_TEXT
    return ""


def validate_content(*, kind: str, raw: bytes) -> tuple[bool, str]:
    """上传前的体检：类型、体积、容器、页数。返回 `(通过, 原因)`。

    拒绝理由要能对上人话——"不支持的类型"说不出是哪种，操作者无法自查。
    """
    body = bytes(raw or b"")
    if not body:
        return False, "内容为空"
    if len(body) > MAX_BYTES:
        return False, f"超过单件上限（{len(body)} > {MAX_BYTES} 字节）"
    head = body[:1024]
    for magic, name in _ARCHIVE_MAGIC:
        if head.startswith(magic):
            return False, f"不接受压缩/打包内容（检测到 {name}）"
    if str(kind) not in ALLOWED_KINDS:
        return False, f"不支持的类型（仅支持 PDF / HTML / 纯文本 / JSON）"
    if str(kind) != KIND_PDF and b"\x00" in body[:4096]:
        return False, "内容像二进制文件（含空字节），不是可读文本"
    if str(kind) == KIND_PDF:
        pages = _pdf_page_count(body)
        if pages and pages > MAX_PAGES:
            return False, f"页数超过上限（{pages} > {MAX_PAGES}）"
    return True, ""


def _pdf_page_count(raw: bytes) -> int:
    """PDF 页数；解析器不可用时返回 0（跳过页数检查，不当成 0 页的通过理由）。"""
    try:
        import io
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception as exc:                     # noqa: BLE001 - 检查器不可用只记日志
        logger.info("PDF 页数检查跳过：%s", str(exc)[:120])
        return 0


class _HtmlText(HTMLParser):
    """HTML → 正文文本：只去脚本/样式/导航，不做正文抽取（正文完整性由准入判据判）。"""

    _SKIP = ("script", "style", "noscript", "iframe", "svg", "nav", "footer")

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = str(data or "").strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(str(html or ""))
    except Exception as exc:                     # noqa: BLE001 - 畸形 HTML 按已收内容
        logger.info("HTML 解析中断（按已收内容）：%s", str(exc)[:120])
    return "\n".join(line for line in parser.text().splitlines() if line.strip())


def _doc_envelope(payload: dict, *, url: str, title: str) -> dict | None:
    """JSON **文档信封** → 文档对象；不是信封返回 None。

    信封形如 `{"text": "...", "chunk_offsets": [[0, 1], ...], "title": "..."}`：接口
    片段正文必须带片段偏移，否则"第几段"这个定位就丢了——公告文本 API 的 `page_index`
    是**片段**不是 PDF 页，丢掉偏移只能退回"字符区间"，定位精度不同（专项 C1：
    洋河冻结材料经正常入口贯穿后仍是 api_chunk，不写成已验 PDF）。
    """
    if not isinstance(payload, dict):
        return None
    text = str(payload.get("text") or "")
    if not text.strip():
        return None
    doc = {"title": str(payload.get("title") or title or ""), "url": str(payload.get("url") or url),
           "text": text, "fetched_at": _now()}
    for key in ("chunk_offsets", "page_offsets"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            doc[key] = [[int(p[0]), int(p[1])] for p in val if isinstance(p, (list, tuple))
                        and len(p) >= 2]
    for key in ("chunk_gaps", "location_kind", "chunk_size", "art_code", "published_at",
                "disclosed_at", "notice_date", "operator_disclosed_at"):
        if payload.get(key) not in (None, "", []):
            doc[key] = payload[key]
    if not doc.get("chunk_offsets") and not doc.get("page_offsets"):
        return None
    return doc


def build_doc(*, raw: bytes, kind: str, url: str = "", title: str = "",
              content_type: str = "") -> tuple[dict | None, dict]:
    """原始字节 → `(文档对象, 解析信息)`；解析不出来返回 `(None, 解析信息)`。"""
    info: dict = {"parser": PARSER_VERSION, "kind": str(kind),
                  "location_kind": "", "pages": 0, "truncated": False,
                  "truncation_reason": "", "chars": 0, "content_type": str(content_type or "")}
    body = bytes(raw or b"")
    if str(kind) == KIND_PDF:
        from annual_report_pdf import doc_from_pages, pages_text
        pages = pages_text(body)
        chars = sum(len(p or "") for p in pages)
        info.update({"parser": "pypdf", "pages": len(pages), "chars": chars,
                     "location_kind": "page"})
        if not pages or chars < MIN_TEXT_CHARS:
            info["truncation_reason"] = "PDF 无可提取文本（扫描件或受保护）"
            return None, info
        if len(pages) >= MAX_PAGES:
            info.update({"truncated": True, "truncation_reason": "页数达到解析上限"})
        return doc_from_pages(title or Path(str(url).split("?")[0]).name or "材料",
                              url, pages), info
    text = body.decode("utf-8", "replace")
    if str(kind) == KIND_HTML:
        text = html_to_text(text)
        info["location_kind"] = "section"
    elif str(kind) == KIND_JSON:
        # 先认**文档信封**（带片段/页码偏移的正文）；不是信封才按纯文本处理
        try:
            envelope = _doc_envelope(json.loads(text), url=url, title=title)
        except Exception:                        # noqa: BLE001 - JSON 不合法按纯文本
            envelope = None
        if envelope is not None:
            info["location_kind"] = ("page" if envelope.get("page_offsets")
                                     else "api_chunk")
            info["chars"] = len(str(envelope.get("text") or ""))
            info["envelope"] = True
            if str(envelope.get("truncated") or ""):
                info["truncated"] = bool(envelope.get("truncated"))
            return envelope, info
        info["location_kind"] = "section"
    else:
        info["location_kind"] = "section"
    if len(text.strip()) < MIN_TEXT_CHARS:
        info["truncation_reason"] = "正文过短（可能不是正文页）"
        return None, info
    info["chars"] = len(text)
    return {"title": str(title or ""), "url": str(url or ""), "text": text,
            "fetched_at": _now()}, info


# ── 落盘与清单 ──────────────────────────────────────────────

def read_index(task_id: str, *, ws_dir=None, project=None) -> list[dict]:
    try:
        p = materials_dir(task_id, ws_dir=ws_dir, project=project) / INDEX_FILE
        if not p.is_file():
            return []
        items = json.loads(p.read_text(encoding="utf-8")) or []
        return [i for i in items if isinstance(i, dict)]
    except Exception as exc:                     # noqa: BLE001 - 读不到按空清单
        logger.warning("材料清单读取失败（task=%s）：%s", task_id, str(exc)[:120])
        return []


def _entry_of(meta: dict) -> dict:
    """清单条目：只放**不随解析变化**的身份与结论摘要。"""
    return {k: meta.get(k) for k in (
        "material_id", "channel", "channel_label", "kind", "kind_label", "title",
        "url", "filename", "bytes", "raw_sha256", "text_sha256", "status", "reason",
        "source_class", "provenance", "provenance_label", "rules_version",
        "retrieved_at", "created_at", "updated_at", "attached", "period", "doc_type",
        "disclosure_date", "date_precision", "date_basis", "metric_states",
        "read_scope", "evidence_count", "section_count", "parse")}


def _write_index(task_id: str, items: list[dict], *, ws_dir=None, project=None) -> None:
    try:
        d = materials_dir(task_id, ws_dir=ws_dir, project=project)
        d.mkdir(parents=True, exist_ok=True)
        (d / INDEX_FILE).write_text(
            json.dumps(sorted(items, key=lambda i: str(i.get("created_at") or "")),
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:                     # noqa: BLE001 - 清单写不进只记日志
        logger.warning("材料清单落盘失败（task=%s）：%s", task_id, str(exc)[:140])


def save_meta(task_id: str, meta: dict, *, ws_dir=None, project=None) -> None:
    """写 meta 并同步清单（两处始终一致：页面读清单，摄取读 meta）。"""
    mid = str((meta or {}).get("material_id") or "")
    if not mid:
        return
    try:
        meta["updated_at"] = _now()
        d = materials_dir(task_id, ws_dir=ws_dir, project=project) / mid
        d.mkdir(parents=True, exist_ok=True)
        (d / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except Exception as exc:                     # noqa: BLE001
        logger.warning("材料元数据落盘失败（task=%s/%s）：%s", task_id, mid, str(exc)[:140])
        return
    items = [i for i in read_index(task_id, ws_dir=ws_dir, project=project)
             if str(i.get("material_id") or "") != mid]
    items.append(_entry_of(meta))
    _write_index(task_id, items, ws_dir=ws_dir, project=project)


def load(task_id: str, mid: str, *, ws_dir=None, project=None) -> dict | None:
    try:
        p = _meta_path(task_id, mid, ws_dir=ws_dir, project=project)
        if not p.is_file():
            return None
        meta = json.loads(p.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else None
    except Exception:                            # noqa: BLE001
        return None


def raw_path(task_id: str, meta: dict, *, ws_dir=None, project=None) -> Path:
    """原件路径：扩展名在**落盘时**定下（`raw_ext`），此后不再随类型判定变化——
    否则第二次读取会拿着新扩展名找一个不存在的文件，进而误判成"没取过"再发一次请求。
    """
    ext = str((meta or {}).get("raw_ext") or "") or \
        _KIND_EXT.get(str((meta or {}).get("kind") or KIND_TEXT), "bin")
    d = materials_dir(task_id, ws_dir=ws_dir, project=project) / str(meta.get("material_id"))
    return d / f"raw.{ext}"


def doc_path(task_id: str, mid: str, *, ws_dir=None, project=None) -> Path:
    return materials_dir(task_id, ws_dir=ws_dir, project=project) / str(mid) / DOC_FILE


def store(*, task_id: str, channel: str, raw: bytes = b"", url: str = "",
          filename: str = "", content_type: str = "", kind: str = "", title: str = "",
          period: str = "", doc_type: str = "年度报告", note: str = "",
          declared_disclosed_at: str = "", ws_dir=None, project=None) -> dict:
    """保存一份待摄取材料（**不判定**，判定在 `admit`）。返回材料记录（含幂等结果）。

    直链只记 URL（取件在 `admit` 里走既有内容通道，失败如实记 `fetch_failed`）；
    文件必须先过 `validate_content`，存储路径由本模块生成，文件名只作标签。
    """
    channel = str(channel or "")
    if channel not in CHANNELS:
        return {"ok": False, "error": f"未知通道：{channel}"}
    url = str(url or "").strip()
    filename = str(filename or "").strip()
    host = ""
    if channel == CHANNEL_LINK:
        if not url:
            return {"ok": False, "error": "直链材料缺少 url"}
        try:
            from narrative_evidence import publisher_of
            host = publisher_of(url)
        except Exception:                        # noqa: BLE001
            host = ""
        if not host:
            return {"ok": False, "error": "不是可解析主机的 http(s) 直链"}
    else:
        if not raw:
            return {"ok": False, "error": "文件材料缺少内容"}
        kind = str(kind or "") or sniff_kind(filename=filename,
                                             content_type=content_type, raw=raw)
        ok, why = validate_content(kind=kind, raw=raw)
        if not ok:
            return {"ok": False, "error": why}
    mid = mid_for(channel=channel, url=url, filename=filename,
                  raw_sha256=raw_sha256(raw) if raw else "")
    existing = load(task_id, mid, ws_dir=ws_dir, project=project)
    if existing is not None:
        return {"ok": True, "duplicate": True, "material_id": mid, "material": existing}
    meta = {
        "ok": True, "material_id": mid, "task_id": str(task_id), "channel": channel,
        "channel_label": CHANNEL_LABEL.get(channel, channel),
        "kind": kind if channel != CHANNEL_LINK else "",
        "kind_label": _KIND_LABEL.get(kind, "") if channel != CHANNEL_LINK else "",
        "title": str(title or filename or url), "url": url, "filename": filename,
        "content_type": str(content_type or ""), "host": host,
        "bytes": len(raw or b""), "raw_sha256": raw_sha256(raw) if raw else "",
        "raw_ext": (_KIND_EXT.get(kind, "bin") if channel != CHANNEL_LINK else "bin"),
        "text_sha256": "", "status": STATE_PENDING, "reason": "", "note": str(note or ""),
        "period": str(period or ""), "doc_type": str(doc_type or ""),
        "declared_disclosed_at": str(declared_disclosed_at or "").strip()[:10],
        "disclosure_date": "", "date_precision": "", "date_basis": "",
        "source_class": "", "provenance": "", "provenance_label": "",
        "rules_version": "", "parse": {}, "read_scope": {}, "metric_states": {},
        "evidence": [], "evidence_count": 0, "section_count": 0,
        "attached": False, "refresh": {}, "pending": [],
        "created_at": _now(), "updated_at": _now(), "retrieved_at": "",
    }
    try:
        d = materials_dir(task_id, ws_dir=ws_dir, project=project) / mid
        d.mkdir(parents=True, exist_ok=True)
        if raw:
            p = raw_path(task_id, meta, ws_dir=ws_dir, project=project)
            p.write_bytes(bytes(raw))
            meta["raw_path"] = f"project/{MATERIALS_DIR}/{mid}/{p.name}"
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": f"原材料落盘失败：{str(exc)[:160]}"}
    save_meta(task_id, meta, ws_dir=ws_dir, project=project)
    logger.info("材料已保存（task=%s，%s，%s，%d 字节）：%s", task_id, mid, channel,
                len(raw or b""), (url or filename)[:120])
    return {"ok": True, "duplicate": False, "material_id": mid, "material": meta}


# ── 取件与准入 ─────────────────────────────────────────────

def _fetch_link(url: str, *, timeout: float = 30.0) -> dict:
    """直链取件：走**既有内容通道**（策略校验/已验 IP/出口模式都在 `net_policy` 那层）。

    本模块不新增请求点，也不自己判域名——安全边界只有一处。
    """
    import net_policy
    resp = net_policy.fetch_document(url, timeout=timeout)
    return {"raw": bytes(resp.get("raw") or b""),
            "content_type": str((resp.get("headers") or {}).get("content-type") or ""),
            "status": int(resp.get("status") or 0),
            "egress": str(resp.get("egress") or "")}


def _metrics_of(metrics) -> list[str]:
    if metrics:
        return [str(m) for m in metrics]
    try:
        from delivery_pipeline import DEFAULT_MANDATORY_METRICS
        return [str(m) for m in DEFAULT_MANDATORY_METRICS]
    except Exception:                            # noqa: BLE001
        return ["revenue", "net_profit", "operating_cashflow"]


def admit(*, task_id: str, mid: str, company: str, company_code: str = "", periods=(),
          as_of: str = "", metrics=(), goal: str = "", doc_type: str = "",
          ws_dir=None, project=None) -> dict:
    """材料 → 准入结论（取件 + 解析 + 判据 + 查阅范围），并把结论写回材料记录。

    已准入且规则版本未变时**不重跑**（避免每次刷新都重解析同一份年报）；
    规则版本变了则重跑，旧 `admitted` 不复用（专项 C1）。
    """
    from adapters import disclosure_ingest as di

    meta = load(task_id, mid, ws_dir=ws_dir, project=project)
    if meta is None:
        return {"ok": False, "status": "missing", "detail": f"材料不存在：{mid}"}
    rules_now = di.admission_rules_version()
    if (str(meta.get("status")) == STATE_ADMITTED
            and str(meta.get("rules_version") or "") == rules_now
            and not company_mismatch(meta, company, company_code)):
        # 复用既有结论：把**同一套字段**摊平出来（页脚/脚本读的是这些名字，
        # 缺字段会显示成 None，看起来像"没判定过"）
        evidence = list(meta.get("evidence") or [])
        out = {"ok": True, "material_id": mid, "status": STATE_ADMITTED,
               "reused": True, "rules_version": rules_now,
               "provenance_label": str(meta.get("provenance_label") or ""),
               "evidence_count": len(evidence),
               "metric_states": dict(meta.get("metric_states") or {}),
               "verdict": {"status": di.ADMITTED,
                           "provenance": str(meta.get("provenance") or ""),
                           "provenance_label": str(meta.get("provenance_label") or ""),
                           "evidence": evidence,
                           "section_count": int(meta.get("section_count") or 0),
                           "metric_states": dict(meta.get("metric_states") or {}),
                           "read_scope": dict(meta.get("read_scope") or {}),
                           "rules_version": rules_now,
                           "hash": str(meta.get("text_sha256") or ""),
                           "doc": {"source_class": meta.get("source_class") or "",
                                   "url": str(meta.get("url") or ""),
                                   "title": str(meta.get("title") or "")},
                           "cutoff": {"disclosed_at": meta.get("disclosure_date") or "",
                                      "precision": meta.get("date_precision") or "",
                                      "basis": meta.get("date_basis") or ""}}}
        return out

    # 1. 原件：直链按需取件（取过就不再发请求）；文件读已保存的字节
    raw, fetch_error, egress = b"", "", ""
    src_path = raw_path(task_id, meta, ws_dir=ws_dir, project=project)
    if str(meta.get("channel")) == CHANNEL_LINK:
        if src_path.is_file():
            try:
                raw = src_path.read_bytes()
            except Exception as exc:             # noqa: BLE001
                fetch_error = f"原件读取失败：{str(exc)[:120]}"
        elif meta.get("url"):
            try:
                got = _fetch_link(str(meta["url"]))
                raw, egress = got["raw"], got["egress"]
                meta["content_type"] = got["content_type"]
                meta["http_status"] = got["status"]
                meta["egress"] = egress
            except Exception as exc:             # noqa: BLE001 - 网络/策略失败如实记
                fetch_error = f"{type(exc).__name__}: {str(exc)[:180]}"
    else:
        try:
            raw = src_path.read_bytes() if src_path.is_file() else b""
        except Exception as exc:                 # noqa: BLE001
            fetch_error = f"原件读取失败：{str(exc)[:120]}"
    if not raw:
        meta.update({"status": STATE_FETCH_FAILED, "reason": fetch_error or "未取得原内容",
                     "rules_version": rules_now, "retrieved_at": _now()})
        save_meta(task_id, meta, ws_dir=ws_dir, project=project)
        return {"ok": False, "material_id": mid, "status": STATE_FETCH_FAILED,
                "detail": fetch_error or "未取得原内容",
                "metric_states": {m: {"state": di.METRIC_NOT_OBTAINED,
                                      "label": di.METRIC_STATE_LABEL[di.METRIC_NOT_OBTAINED]}
                                  for m in _metrics_of(metrics)}}
    meta["raw_sha256"] = raw_sha256(raw)
    meta["bytes"] = len(raw)
    try:
        src_path.parent.mkdir(parents=True, exist_ok=True)
        if not src_path.is_file():
            src_path.write_bytes(raw)
        meta["raw_path"] = f"project/{MATERIALS_DIR}/{mid}/{src_path.name}"
    except Exception as exc:                     # noqa: BLE001 - 存不下原件仍可继续判定
        logger.warning("原件落盘失败（task=%s/%s）：%s", task_id, mid, str(exc)[:140])

    # 2. 解析
    kind = str(meta.get("kind") or "") or sniff_kind(
        filename=str(meta.get("filename") or ""),
        content_type=str(meta.get("content_type") or ""), raw=raw)
    doc, parse_info = build_doc(raw=raw, kind=kind, url=str(meta.get("url") or ""),
                               title=str(meta.get("title") or ""),
                               content_type=str(meta.get("content_type") or ""))
    meta.update({"kind": kind, "kind_label": _KIND_LABEL.get(kind, ""), "parse": parse_info,
                 "rules_version": rules_now, "retrieved_at": _now()})
    if doc is None:
        meta.update({"status": STATE_REJECTED,
                     "reason": parse_info.get("truncation_reason") or "正文解析失败",
                     "detail": "原件已保存，但没能得到可用正文"})
        save_meta(task_id, meta, ws_dir=ws_dir, project=project)
        return {"ok": False, "material_id": mid, "status": STATE_REJECTED,
                "detail": meta["reason"], "parse": parse_info}
    # 直链的标题：正文若给出标题就用它（上传的标题是操作者写的标签）
    if str(meta.get("channel")) == CHANNEL_LINK:
        doc["title"] = str(meta.get("title") or doc.get("title") or "")
    # 操作者声明的披露日：只在材料自己没带来源日期时才起作用（依据单独标注）
    if str(meta.get("declared_disclosed_at") or ""):
        doc["operator_disclosed_at"] = str(meta["declared_disclosed_at"])

    # 3. 准入判据（与自动检索、离线重入**同一条**链）
    provenance = (di.PROVENANCE_MANUAL_URL if str(meta.get("channel")) == CHANNEL_LINK
                  else di.PROVENANCE_MANUAL_FILE)
    metrics_list = _metrics_of(metrics)
    verdict = di.ingest(doc, company=str(company or ""), company_code=str(company_code or ""),
                        periods=list(periods or ()), as_of=str(as_of or ""),
                        provenance=provenance, doc_type=str(doc_type or meta.get("doc_type")
                                                            or "年度报告"),
                        section_limit=8, section_pick="questions", metrics=metrics_list)
    doc["truncated"] = bool(parse_info.get("truncated"))
    doc["truncation_reason"] = str(parse_info.get("truncation_reason") or "")
    doc["text_sha256"] = di.document_hash(doc)
    meta["text_sha256"] = doc["text_sha256"]
    if str(verdict.get("status")) != di.ADMITTED:
        meta.update({"status": STATE_REJECTED,
                     "reason": str(verdict.get("reason") or ""),
                     "detail": str(verdict.get("detail") or ""),
                     "may_use_as_background": bool(verdict.get("may_use_as_background")),
                     "metric_states": {m: {"state": di.METRIC_NOT_LOCATED,
                                           "label": di.METRIC_STATE_LABEL[di.METRIC_NOT_LOCATED]}
                                       for m in metrics_list}})
        save_meta(task_id, meta, ws_dir=ws_dir, project=project)
        logger.info("材料未准入（task=%s，%s）：%s", task_id, mid, meta["reason"])
        return {"ok": False, "material_id": mid, "status": STATE_REJECTED,
                "reason": meta["reason"], "detail": meta["detail"], "verdict": verdict,
                "metric_states": dict(meta.get("metric_states") or {})}

    # 4. 结论与身份写回（主体/期间/披露日的**依据**也要落下来，不能只留结论）
    docd = verdict.get("doc") or {}
    cutoff = verdict.get("cutoff") or {}
    doc.update({"material_id": mid, "source_class": str(docd.get("source_class") or ""),
                "provenance": str(verdict.get("provenance") or ""),
                "channel": str(meta.get("channel") or ""),
                # 原件身份：取得原始内容 hash + 字节数；正文 hash 另在 text_sha256
                "raw_sha256": str(meta.get("raw_sha256") or ""),
                "raw_bytes": int(meta.get("bytes") or 0),
                "parser_version": PARSER_VERSION,
                "admission_rules": rules_now,
                "subject": {"company": str(company or ""), "company_code": str(company_code or ""),
                            "basis": "body"},
                "periods": list(periods or ()),
        "period_basis": "title" if periods else "",
        "subject": {"company": str(company or ""), "company_code": str(company_code or ""),
                    "basis": "body"},
        "disclosed_at": str(cutoff.get("disclosed_at") or ""),
                "date_precision": str(cutoff.get("precision") or ""),
                "date_basis": str(cutoff.get("basis") or ""),
                "retrieved_at": str(meta.get("retrieved_at") or "")})
    try:
        dp = doc_path(task_id, mid, ws_dir=ws_dir, project=project)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:                     # noqa: BLE001
        logger.warning("材料正文落盘失败（task=%s/%s）：%s", task_id, mid, str(exc)[:140])
    meta.update({
        "status": STATE_ADMITTED, "reason": "", "detail": "",
        "source_class": str(docd.get("source_class") or ""),
        "provenance": str(verdict.get("provenance") or ""),
        "provenance_label": str(verdict.get("provenance_label") or ""),
        "disclosure_date": str(cutoff.get("disclosed_at") or ""),
        "date_precision": str(cutoff.get("precision") or ""),
        "date_basis": str(cutoff.get("basis") or ""),
        "periods": list(periods or ()),
        "subject": dict(doc.get("subject") or {}),
        "evidence": list(verdict.get("evidence") or []),
        "evidence_count": len(verdict.get("evidence") or []),
        "section_count": int(verdict.get("section_count") or 0),
        "metric_states": dict(verdict.get("metric_states") or {}),
        "read_scope": dict(verdict.get("read_scope") or {}),
    })
    save_meta(task_id, meta, ws_dir=ws_dir, project=project)
    logger.info("材料已准入（task=%s，%s）：%s；小节 %d，证据 %d", task_id, mid,
                docd.get("source_class"), meta["section_count"], meta["evidence_count"])
    return {"ok": True, "material_id": mid, "status": STATE_ADMITTED, "reused": False,
            "rules_version": rules_now, "verdict": verdict, "parse": parse_info}


def company_mismatch(meta: dict, company: str, company_code: str) -> bool:
    """材料记录里的研究对象与本次请求是否不同。

    不同则**必须重判**：同一份材料在另一个任务名下被准入过，不等于对本任务也成立
    （主体判据依赖请求里的公司名与代码）。
    """
    want = str(company or "").strip()
    if not want:
        return False
    have = str((meta.get("subject") or {}).get("company") or "").strip()
    return bool(have) and have != want


# ── 并入快照 ───────────────────────────────────────────────

def snapshot_path(task_id: str, *, ws_dir=None, project=None) -> Path:
    if project:
        import workspace
        return Path(workspace.task_project_dir(task_id, project)) / "fetch_snapshot.json"
    base = _task_ws(task_id, ws_dir)
    return (base if str(base).endswith("project") else base / "project") / "fetch_snapshot.json"


def attach(*, task_id: str, mid: str, ws_dir=None, project=None) -> dict:
    """把已准入正文并入 `project/fetch_snapshot.json`（按材料 id 幂等替换）。

    快照是证据/引用/来源清单的**同一输入**：材料不写进这里，等于没进研究链。
    """
    meta = load(task_id, mid, ws_dir=ws_dir, project=project)
    if meta is None:
        return {"ok": False, "error": f"材料不存在：{mid}"}
    if str(meta.get("status")) != STATE_ADMITTED:
        return {"ok": False, "error": f"材料未准入（{meta.get('status')}），不得写进资料集"}
    try:
        doc = json.loads(doc_path(task_id, mid, ws_dir=ws_dir, project=project)
                         .read_text(encoding="utf-8"))
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "error": f"材料正文读取失败：{str(exc)[:140]}"}
    if not str(doc.get("text") or "").strip():
        return {"ok": False, "error": "材料正文为空"}
    snap = snapshot_path(task_id, ws_dir=ws_dir, project=project)
    items: list[dict] = []
    if snap.is_file():
        try:
            loaded = json.loads(snap.read_text(encoding="utf-8")) or []
            items = [i for i in loaded if isinstance(i, dict)]
        except Exception:                        # noqa: BLE001 - 旧快照不可读按空重建
            logger.warning("旧资料快照不可读，按空快照重建（task=%s）", task_id)
            items = []
    url = str(doc.get("url") or "")
    replaced = False

    def _same(it: dict) -> bool:
        if str(it.get("material_id") or "") == str(mid):
            return True
        return bool(url) and str(it.get("url") or "") == url

    kept = [i for i in items if not _same(i)]
    replaced = len(kept) != len(items)
    kept.append(doc)
    snap.parent.mkdir(parents=True, exist_ok=True)
    tmp = snap.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(snap)
    meta["attached"] = True
    meta["snapshot_docs"] = len(kept)
    save_meta(task_id, meta, ws_dir=ws_dir, project=project)
    logger.info("材料并入资料快照（task=%s，%s）：%s（替换旧条目=%s，快照 %d 篇）",
                task_id, mid, snap.name, replaced, len(kept))
    return {"ok": True, "material_id": mid, "docs": len(kept), "replaced": replaced,
            "snapshot": str(snap)}


# ── 材料变化后的确定性重做 ──────────────────────────────────

def identity_snapshot(task_id: str, *, ws_dir=None, project=None) -> dict:
    """资料与证据的**身份指纹**（用于判断一次补材料到底有没有改变可用于回答的东西）。

    两段组成：有定位的证据记录（网址 + 片段 hash + 准入 + 期间）与快照文档身份
    （材料 id、来源类别、provenance、准入规则版本、正文 hash）。只比"文件动过没有"
    不够：同一份正文重复摄取会把文件写一遍，报告其实无需重做；反过来，正文没变但
    来源类别变了（用户材料 vs 官方披露）就**必须**重验——免责声明与来源清单会跟着变。
    """
    import hashlib as _h
    recs: list[str] = []
    try:
        import narrative_evidence as ne
        payload = ne.read(task_id, ws_dir=ws_dir) or {}
        for r in (payload.get("records") or []):
            if not r.get("has_location"):
                continue
            recs.append("|".join(str(r.get(k) or "") for k in
                                 ("url", "content_hash", "admission", "document_period",
                                  "published_at")))
    except Exception as exc:                     # noqa: BLE001 - 读不到按"无证据"
        logger.info("身份指纹：证据读取失败（task=%s）：%s", task_id, str(exc)[:120])
    docs: list[str] = []
    try:
        for d in (json.loads(snapshot_path(task_id, ws_dir=ws_dir, project=project)
                             .read_text(encoding="utf-8")) or []):
            if not isinstance(d, dict):
                continue
            docs.append("|".join(str(d.get(k) or "") for k in
                                 ("url", "material_id", "source_class", "provenance",
                                  "admission_rules", "text_sha256", "disclosed_at",
                                  "date_basis")))
    except Exception as exc:                     # noqa: BLE001 - 没有快照就是空
        logger.info("身份指纹：快照读取失败（task=%s）：%s", task_id, str(exc)[:120])
    blob = "\n".join(sorted(recs) + sorted(docs))
    return {"records": len(recs), "docs": len(docs),
            "combined": _h.sha256(blob.encode("utf-8", "replace")).hexdigest()}


def refresh(*, task_id: str, goal: str = "", project=None, ws_dir=None,
            previous: dict | None = None) -> dict:
    """材料变化后的**确定性**下游：证据 → 结构 → 底稿。不调用模型、不改交付正文。

    `previous` = 摄取前的身份指纹（`identity_snapshot`）。比对结论决定是否要重验：
    身份没变（同一份正文、同一来源类别）就不该报"待重验"，也不该产生待办——
    假待办会让人以为正文必须重做，真变化则必须明说。需要模型重新生成的步骤只记成
    待办：正文是交付物，重生成要走既有预算与授权，不在补材料的一步里顺手做掉。
    """
    out: dict = {"ok": True, "task_id": str(task_id), "stale": False, "pending": [],
                 "evidence": {}, "structure": {}, "working_paper": {}}
    evidence = {}
    try:
        import narrative_evidence as ne
        evidence = ne.build(task_id, goal=goal, ws_dir=ws_dir, project=project) or {}
        out["evidence"] = {
            "records": len(evidence.get("records") or []),
            "located": int(evidence.get("located") or 0),
            "docs": int(evidence.get("docs") or 0),
            "rules_version": str(evidence.get("rules_version") or ""),
            "snapshot_sha256": str(evidence.get("snapshot_sha256") or ""),
            "missing_kinds": list(evidence.get("missing_kinds") or []),
        }
    except Exception as exc:                     # noqa: BLE001 - 证据重建失败必须报出来
        logger.warning("材料后证据重建失败（task=%s）：%s", task_id, str(exc)[:160])
        out.update({"ok": False, "error": f"证据重建失败：{str(exc)[:160]}"})
        return out

    adopted_body, adopted_id = "", ""
    try:
        from report_version import VersionStore
        store = VersionStore(_task_ws(task_id, ws_dir), task_id)
        current = store.adopted()
        if current is not None:
            adopted_body = str(current.body or "")
            adopted_id = str(current.version_id or "")
    except Exception as exc:                     # noqa: BLE001
        logger.info("材料后读取采纳版本失败（task=%s）：%s", task_id, str(exc)[:140])

    try:
        import report_brief
        if adopted_body and report_brief.is_research_task(task_id, goal, ws_dir=ws_dir):
            structure = report_brief.build_structure(task_id, goal, adopted_body,
                                                     project=project, ws_dir=ws_dir)
            if structure:
                report_brief.stamp_structure_version(structure, adopted_id)
                report_brief.write_structure(task_id, structure, ws_dir=ws_dir)
                out["structure"] = {
                    "version_id": adopted_id,
                    "evidence_fingerprint": str((structure.get("evidence") or {}).get("fingerprint") or ""),
                    "findings": len(structure.get("findings") or []),
                    "gaps": len(structure.get("gaps") or []),
                    "citations": len(structure.get("citations") or []),
                }
    except Exception as exc:                     # noqa: BLE001 - 结构重装配失败记缺口
        logger.warning("材料后结构重装配失败（task=%s）：%s", task_id, str(exc)[:160])
        out["structure_error"] = str(exc)[:160]

    try:
        from working_paper_export import write_working_paper
        wp = write_working_paper(task_id, goal, project=project) or {}
        # `write_working_paper` 的 `rows` 是**条数**（不是行列表）
        out["working_paper"] = {"ok": bool(wp.get("ok")),
                                "rows": int(wp.get("rows") or 0)}
    except Exception as exc:                     # noqa: BLE001
        logger.warning("材料后底稿重生成失败（task=%s）：%s", task_id, str(exc)[:160])
        out["working_paper"] = {"ok": False, "error": str(exc)[:160]}

    # 交付正文是否仍与新资料一致：不一致就**明说**待重验，而不是悄悄沿用旧正文；
    # 一致（同一份正文、同一来源类别）就不产生假待办。
    now = identity_snapshot(task_id, ws_dir=ws_dir, project=project)
    before_fp = str((previous or {}).get("combined") or "")
    out["identity"] = {"before": before_fp, "after": now["combined"],
                       "changed": (not before_fp) or before_fp != now["combined"],
                       "records": now["records"], "docs": now["docs"]}
    # 摄取前没留指纹（旧任务/直接调用）时按**保守**处理：不能证明没变，就当变过
    out["stale"] = bool(out["identity"]["changed"])
    if out["stale"]:
        out["pending"] = [{
            "kind": "model_regeneration",
            "reason": ("资料集身份已变化，正文需按新材料重新生成（等待既有预算/授权；"
                       "本次只重做了证据、结构与底稿）"),
        }]
    else:
        out["note"] = ("资料与证据身份未变（同一份正文、同一来源类别）："
                       "本次只刷新了记录，交付正文无需重验")
    return out


def status(task_id: str, *, mid: str = "", ws_dir=None, project=None) -> dict:
    """材料状态：单条或全量（页面/接口共用一份口径）。"""
    if mid:
        meta = load(task_id, mid, ws_dir=ws_dir, project=project)
        return {"ok": bool(meta), "material": meta or {}}
    items = read_index(task_id, ws_dir=ws_dir, project=project)
    return {"ok": True, "count": len(items), "materials": items,
            "admitted": sum(1 for i in items if str(i.get("status")) == STATE_ADMITTED),
            "pending": [str(i.get("material_id")) for i in items
                        if str(i.get("status")) in (STATE_PENDING, STATE_FETCH_FAILED)]}


def pending_materials(task_id: str, *, ws_dir=None, project=None) -> list[dict]:
    """还没摄取成功的材料（`pending_intake` / `fetch_failed`）——供编排器启动补做。"""
    return [i for i in read_index(task_id, ws_dir=ws_dir, project=project)
            if str(i.get("status")) in (STATE_PENDING, STATE_FETCH_FAILED)]


def load_doc(task_id: str, mid: str, *, ws_dir=None, project=None) -> dict | None:
    """已准入的正文对象（并入快照/回灌清洗用的就是它）。"""
    try:
        p = doc_path(task_id, mid, ws_dir=ws_dir, project=project)
        if not p.is_file():
            return None
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:                            # noqa: BLE001
        return None


def record_refresh(task_id: str, mid: str, refresh: dict, *, ws_dir=None,
                   project=None) -> None:
    """把本轮确定性重做的结果与待办写回材料记录（页面据此显示"还差什么"）。"""
    meta = load(task_id, mid, ws_dir=ws_dir, project=project)
    if meta is None:
        return
    meta["refresh"] = dict(refresh or {})
    meta["pending"] = list((refresh or {}).get("pending") or [])
    save_meta(task_id, meta, ws_dir=ws_dir, project=project)
