# -*- coding: utf-8 -*-
"""报告 Markdown → PDF（F3，纯 Python，零第三方依赖）。

选型说明：
- 当前环境没有 reportlab / fpdf2 / weasyprint，且网络受限不便新增依赖，
  故采用"最轻方案"：纯 Python 直接写 PDF 对象流；
- 中文：嵌入系统中文字体（Windows: simhei.ttf / msyh.ttc / simsun.ttc，
  Linux: Noto CJK），用 Type0 + Identity-H 编码渲染，不依赖系统字体安装；
- 图表：解析 PNG（FlateDecode + 过滤器还原）与 JPEG（DCTDecode）并嵌入页面；
- 表格：Markdown 表格转 PDF 网格；代码块以等宽小字输出。

对外入口：markdown_to_pdf(markdown, title, workspace) -> bytes
"""

from __future__ import annotations

import io
import os
import re
import struct
import zlib

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN_L = 50.0
MARGIN_R = 50.0
MARGIN_T = 56.0
MARGIN_B = 56.0
USABLE_W = PAGE_W - MARGIN_L - MARGIN_R
USABLE_H = PAGE_H - MARGIN_T - MARGIN_B

_HEADING_SIZES = {1: 16.0, 2: 14.0, 3: 12.5, 4: 11.5, 5: 11.0, 6: 10.5}
BODY_SIZE = 10.5
CODE_SIZE = 9.0
TABLE_SIZE = 9.0


# ─────────────────────────── TrueType 解析 ───────────────────────────

def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def _i16(b: bytes, off: int) -> int:
    return struct.unpack_from(">h", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


class TTFont:
    """最小 TrueType 解析：cmap(格式4/12)、hmtx、head/hhea/maxp，用于排版与嵌入。"""

    def __init__(self, path: str) -> None:
        with open(path, "rb") as f:
            data = f.read()
        # TTC 容器：依次尝试每个子字体，取第一个可解析的
        # （部分 TTC 的第一个子字体可能是特殊字重/变体，表内容异常）
        if data[:4] == b"ttcf":
            n_fonts = _u32(data, 8)
            for i in range(max(0, min(n_fonts, 64))):
                off = _u32(data, 12 + 4 * i)
                try:
                    self._init_ttf(data[off:])
                    return
                except Exception:
                    continue
            raise ValueError(f"no usable subfont in TTC: {path}")
        self._init_ttf(data)

    def _init_ttf(self, data: bytes) -> None:
        self.data = data
        num_tables = _u16(data, 4)
        tables: dict[str, tuple[int, int]] = {}
        pos = 12
        for _ in range(num_tables):
            tag = data[pos:pos + 4].decode("latin-1")
            tables[tag] = (_u32(data, pos + 8), _u32(data, pos + 12))
            pos += 16
        self._tables = tables
        self._table_data = {
            tag: data[off:off + length]
            for tag, (off, length) in tables.items()
        }
        # OpenType/CFF 字体（如 NotoSansCJK.ttc、部分思源字体）只有 CFF 轮廓，
        # 本解析器按 TrueType(glyf) 嵌入（CIDFontType2），CFF 字体会产生乱码。
        # 加载时直接判为不支持，调用方回退 Helvetica（无中文）或换字体。
        if "CFF " in tables and "glyf" not in tables:
            raise ValueError("OpenType/CFF font not supported")
        head = self._table_data["head"]
        self.units_per_em = _u16(head, 18)
        self.x_min, self.y_min = _i16(head, 36), _i16(head, 38)
        self.x_max, self.y_max = _i16(head, 40), _i16(head, 42)
        hhea = self._table_data["hhea"]
        self.ascent = _i16(hhea, 4)
        self.descent = _i16(hhea, 6)
        self.num_hmetrics = _u16(hhea, 34)
        maxp = self._table_data["maxp"]
        self.num_glyphs = _u16(maxp, 4)
        self._advances = self._parse_hmtx()
        self._cmap = self._parse_cmap()
        # 有效性校验：异常子字体（如部分 TTC 的首个子字体表内容为空）
        # 会得到 num_glyphs=0 / 空 cmap，渲染全乱码，视为不可用。
        if self.num_glyphs <= 0 or not self._cmap:
            raise ValueError(f"unusable font (glyphs={self.num_glyphs}, cmap={len(self._cmap)}): {path}")

    def _parse_hmtx(self) -> list[int]:
        hmtx = self._table_data["hmtx"]
        advances: list[int] = []
        for i in range(self.num_hmetrics):
            advances.append(_u16(hmtx, i * 4))
        return advances

    def _parse_cmap(self) -> dict[int, int]:
        """合并**所有** Unicode cmap 子表（而不是只留一个）。

        实测缺陷（CI 上暴露）：Linux 字体（DroidSansFallbackFull.ttf）的 cmap 里，
        ASCII/数字与 CJK 分处不同子表；此前 `best = m or best` 只保留**最后一个**
        命中的子表，于是取到的那份不含 ASCII → `glyph_id("1") == 0` → PDF 里
        「第 1 页」渲染成「第  页」、`no_such_chart.png` 里的英文整段消失
        （抽取文本里是一串 \\x00）。合并后 ASCII 与 CJK 都能解析。

        优先级（高者覆盖低者）：Windows 全量 (3,10) > Windows BMP (3,1) >
        Unicode (0,x) > 其它。
        """
        cmap = self._table_data.get("cmap")
        if not cmap:
            return {}
        n = _u16(cmap, 2)
        tables: list[tuple[int, dict[int, int]]] = []
        for i in range(n):
            pid, eid = _u16(cmap, 4 + i * 8), _u16(cmap, 6 + i * 8)
            off = _u32(cmap, 8 + i * 8)
            sub = cmap[off:]
            if len(sub) < 4:
                continue
            fmt = _u16(sub, 0)
            if fmt == 4:
                m = self._parse_cmap4(sub)
            elif fmt == 12:
                m = self._parse_cmap12(sub)
            elif fmt == 0:
                m = self._parse_cmap0(sub)
            elif fmt == 6:
                m = self._parse_cmap6(sub)
            else:
                continue
            if not m:
                continue
            if pid == 3 and eid == 10:
                pri = 3
            elif pid == 3 and eid == 1:
                pri = 2
            elif pid == 0:
                pri = 1
            else:
                pri = 0
            tables.append((pri, m))
        merged: dict[int, int] = {}
        for _pri, m in sorted(tables, key=lambda x: x[0]):
            for cp, gid in m.items():
                if gid:
                    merged[cp] = gid
        return merged

    @staticmethod
    def _parse_cmap0(sub: bytes) -> dict[int, int]:
        """format 0：256 字节映射（ASCII 常只出现在这类子表里）。

        实测依据：CI 上的 DroidSansFallbackFull.ttf 只有 format 4/12 的 CJK 子表被
        解析到时，ASCII/数字**一个都取不到**（`glyph_id("1") == 0`），PDF 里数字与
        英文整段渲染成空白。这类字体的 Latin 覆盖通常放在 Mac Roman(1,0) 的
        format 0 子表里——不解析它，正文里的数字、英文、文件名就全丢。
        """
        out: dict[int, int] = {}
        for cp in range(256):
            gid = sub[6 + cp] if len(sub) > 6 + cp else 0
            if gid:
                out[cp] = gid
        return out

    @staticmethod
    def _parse_cmap6(sub: bytes) -> dict[int, int]:
        """format 6：裁剪映射（firstCode + 连续 glyphId 数组）。"""
        try:
            first = _u16(sub, 6)
            count = _u16(sub, 8)
        except Exception:
            return {}
        out: dict[int, int] = {}
        for i in range(count):
            pos = 10 + i * 2
            if len(sub) < pos + 2:
                break
            gid = _u16(sub, pos)
            if gid:
                out[first + i] = gid
        return out

    @staticmethod
    def _parse_cmap4(sub: bytes) -> dict[int, int]:
        try:
            seg_count_x2 = _u16(sub, 6)
            seg_count = seg_count_x2 // 2
            end = [0] * seg_count
            for i in range(seg_count):
                end[i] = _u16(sub, 14 + i * 2)
            start_off = 14 + seg_count_x2 + 2
            start = [0] * seg_count
            for i in range(seg_count):
                start[i] = _u16(sub, start_off + i * 2)
            delta_off = start_off + seg_count_x2
            delta = [0] * seg_count
            for i in range(seg_count):
                delta[i] = _u16(sub, delta_off + i * 2)
            range_off = delta_off + seg_count_x2
            range_off_pos = [0] * seg_count
            for i in range(seg_count):
                range_off_pos[i] = _u16(sub, range_off + i * 2)
            glyph_arr = range_off + seg_count_x2
            out: dict[int, int] = {}
            for i in range(seg_count):
                s, e = start[i], end[i]
                if s > e or s == 0xFFFF:
                    continue
                for cp in range(s, e + 1):
                    ro = range_off_pos[i]
                    if ro == 0:
                        gid = (cp + delta[i]) & 0xFFFF
                    else:
                        addr = range_off + i * 2 + ro + (cp - s) * 2
                        gid = _u16(sub, addr) if addr + 1 < len(sub) else 0
                        if gid:
                            gid = (gid + delta[i]) & 0xFFFF
                    if gid:
                        out[cp] = gid
            return out
        except Exception:
            return {}

    @staticmethod
    def _parse_cmap12(sub: bytes) -> dict[int, int]:
        try:
            n_groups = _u32(sub, 12)
            out: dict[int, int] = {}
            for i in range(n_groups):
                base = 16 + i * 12
                s, e, g = _u32(sub, base), _u32(sub, base + 4), _u32(sub, base + 8)
                for cp in range(s, min(e, s + 65535) + 1):
                    out[cp] = g + (cp - s)
            return out
        except Exception:
            return {}

    def glyph_id(self, ch: str) -> int:
        return self._cmap.get(ord(ch), 0)

    def advance(self, ch: str) -> int:
        gid = self.glyph_id(ch)
        if gid == 0:
            return 0
        if gid < self.num_hmetrics:
            return self._advances[gid]
        return self._advances[-1] if self._advances else 0

    def text_width(self, text: str, size: float) -> float:
        upem = self.units_per_em or 1000
        total = sum(self.advance(ch) for ch in text)
        return total / upem * size


def _candidate_fonts() -> list[str]:
    env = os.environ.get("WEAVEMIND_PDF_FONT")
    candidates: list[str] = []
    if env:
        candidates.append(env)
    candidates += [
        # 只用 .ttf 真 TrueType（TTC 集合的部分 Windows 打包存在非标准偏移，
        # 解析不可靠；CFF 轮廓也不支持），保证跨平台一致。
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\Deng.ttf",
        r"C:\Windows\Fonts\STSONG.TTF",
        r"/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        r"/usr/share/fonts/truetype/wqy/wqy-microhei.ttf",
        r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttf",
        r"/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    return candidates


def _load_font() -> TTFont | None:
    for p in _candidate_fonts():
        try:
            if os.path.exists(p):
                return TTFont(p)
        except Exception:
            continue
    return None


# ─────────────────────────── Markdown 解析 ───────────────────────────

def _norm_title(text: str) -> str:
    """标题比较用的归一化：去空白与常见标点，忽略大小写后缀差异。

    用于判断"正文首个标题是否就是封面标题"，只做保守归一（空白/常见标点/全半角
    空格），避免把"复核结论"和"复核结论与建议"当成同一个而误删真标题。
    """
    t = _inline_plain(str(text or "")).lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[：:，,。.、；;！!？?“”\"'（）()【】\[\]《》<>—\-_*#]+", "", t)
    return t


def _inline_plain(text: str) -> str:
    """把内联 Markdown 语法剥掉，保留可读纯文本。"""
    # 转义下划线先换成占位符：`\_` 必须在剥斜体**之前**保护，否则
    # `ratio\_net\_margin` 会被 `_..._` 斜体规则吞掉两个下划线
    # （实测渲染成 "rationetmargin"）
    _ESC_US = "\x00"
    text = text.replace("\\_", _ESC_US)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    # 转义过的下划线（`\_`）在剥完斜体后还原成字面下划线：否则 `ratio_net_margin`
    # 这类稳定标识会被当成斜体标记吞掉（实测渲染成 "rationetmargin"）
    text = text.replace(_ESC_US, "_")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def _split_blocks(md: str) -> list[dict]:
    lines = str(md or "").split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        fence = re.match(r"^```", stripped)
        if fence:
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"type": "code", "text": "\n".join(code)})
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            blocks.append({
                "type": "heading",
                "level": len(heading.group(1)),
                "text": _inline_plain(heading.group(2)),
            })
            i += 1
            continue
        if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", stripped):
            blocks.append({"type": "hr"})
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^\|?[\s:|-]+\|?\s*$", next_line) and "|" in next_line:
                rows: list[list[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = [
                        _inline_plain(c)
                        for c in lines[i].strip().strip("|").split("|")
                    ]
                    rows.append(cells)
                    i += 1
                if len(rows) >= 2:
                    # 跳过表头分隔行（|---|---|）
                    blocks.append({"type": "table", "rows": rows[0:1] + rows[2:]})
                continue
        if re.match(r"^\s*[-*+]\s+", stripped):
            items: list[str] = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i].strip()):
                items.append(
                    _inline_plain(re.sub(r"^\s*[-*+]\s+", "", lines[i].strip()))
                )
                i += 1
            blocks.append({"type": "list", "items": items, "ordered": False})
            continue
        if re.match(r"^\s*\d+[.)]\s+", stripped):
            items = []
            while i < len(lines) and re.match(
                r"^\s*\d+[.)]\s+", lines[i].strip(),
            ):
                items.append(
                    _inline_plain(re.sub(r"^\s*\d+[.)]\s+", "", lines[i].strip()))
                )
                i += 1
            blocks.append({"type": "list", "items": items, "ordered": True})
            continue
        if stripped.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(_inline_plain(lines[i].strip().lstrip(">").strip()))
                i += 1
            blocks.append({"type": "quote", "lines": quote})
            continue
        img = re.match(
            r"^!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)\s*$", stripped,
        )
        if img:
            blocks.append({
                "type": "image",
                "alt": img.group(1),
                "src": img.group(2),
            })
            i += 1
            continue
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                break
            if re.match(r"^(#{1,6}\s|```|[-*+]\s|\d+[.)]\s|>)", nxt):
                break
            if nxt.startswith("|") or nxt.startswith("!"):
                break
            if re.match(r"^\s*(?:-{3,}|\*{3,})\s*$", nxt):
                break
            para.append(nxt)
            i += 1
        blocks.append({"type": "para", "text": _inline_plain(" ".join(para))})
    return blocks


# ─────────────────────────── 图片解析 ───────────────────────────

def _png_to_rgb(data: bytes) -> tuple[int, int, bytes] | None:
    """解析 PNG → (宽, 高, RGB 字节)，支持 8bit 灰度/真彩/调色板/带 alpha。"""
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        w = h = bit_depth = color_type = 0
        idat = b""
        plte: bytes | None = None
        trns: bytes | None = None
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            payload = data[pos + 8:pos + 8 + length]
            pos += 12 + length
            if ctype == b"IHDR":
                w, h, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
            elif ctype == b"IDAT":
                idat += payload
            elif ctype == b"PLTE":
                plte = payload
            elif ctype == b"tRNS":
                trns = payload
            if ctype == b"IEND":
                break
        if not idat or bit_depth != 8:
            return None
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
        if not channels:
            return None
        raw = zlib.decompress(idat)
        stride = w * channels
        out = bytearray(w * h * 3)
        prev = bytearray(stride)
        for y in range(h):
            row_start = y * (stride + 1)
            if row_start + stride > len(raw):
                return None
            flt = raw[row_start]
            scan = bytearray(raw[row_start + 1:row_start + 1 + stride])
            if flt == 1:
                for x in range(channels, stride):
                    scan[x] = (scan[x] + scan[x - channels]) & 0xFF
            elif flt == 2:
                for x in range(stride):
                    scan[x] = (scan[x] + prev[x]) & 0xFF
            elif flt == 3:
                for x in range(stride):
                    a = scan[x - channels] if x >= channels else 0
                    scan[x] = (scan[x] + ((a + prev[x]) >> 1)) & 0xFF
            elif flt == 4:
                for x in range(stride):
                    a = scan[x - channels] if x >= channels else 0
                    b = prev[x]
                    c = prev[x - channels] if x >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (
                        b if pb <= pc else c
                    )
                    scan[x] = (scan[x] + pr) & 0xFF
            for x in range(w):
                if color_type == 2:
                    r, g, b = scan[x * 3], scan[x * 3 + 1], scan[x * 3 + 2]
                    o = (y * w + x) * 3
                    out[o], out[o + 1], out[o + 2] = r, g, b
                elif color_type == 6:
                    r, g, b, a = (
                        scan[x * 4], scan[x * 4 + 1],
                        scan[x * 4 + 2], scan[x * 4 + 3],
                    )
                    inv = 255 - a
                    o = (y * w + x) * 3
                    out[o] = (r * a + 255 * inv) // 255
                    out[o + 1] = (g * a + 255 * inv) // 255
                    out[o + 2] = (b * a + 255 * inv) // 255
                elif color_type == 0:
                    v = scan[x]
                    o = (y * w + x) * 3
                    out[o], out[o + 1], out[o + 2] = v, v, v
                elif color_type == 4:
                    v, a = scan[x * 2], scan[x * 2 + 1]
                    inv = 255 - a
                    o = (y * w + x) * 3
                    out[o] = (v * a + 255 * inv) // 255
                    out[o + 1] = out[o]
                    out[o + 2] = out[o]
                elif color_type == 3 and plte:
                    idx = scan[x]
                    if idx * 3 + 2 < len(plte):
                        r, g, b = plte[idx * 3], plte[idx * 3 + 1], plte[idx * 3 + 2]
                        a = trns[idx] if trns and idx < len(trns) else 255
                        inv = 255 - a
                        o = (y * w + x) * 3
                        out[o] = (r * a + 255 * inv) // 255
                        out[o + 1] = (g * a + 255 * inv) // 255
                        out[o + 2] = (b * a + 255 * inv) // 255
            prev = scan
        return w, h, bytes(out)
    except Exception:
        return None


def _jpeg_info(data: bytes) -> tuple[int, int] | None:
    """解析 JPEG SOF 标记 → (宽, 高)。"""
    try:
        if data[:2] != b"\xff\xd8":
            return None
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(
                    ">HH", data[pos + 5:pos + 9],
                )
                return w, h
            pos += 2 + length
        return None
    except Exception:
        return None


def _resolve_image_src(src: str, workspace: os.PathLike | None) -> str | None:
    """把报告图片链接解析为本地文件：
    /files/<task_id>/<rel>、绝对路径、相对 workspace 路径。"""
    src = str(src).strip()
    if src.lower().startswith(("http://", "https://", "data:")):
        return None
    path = src.replace("\\", "/")
    m = re.match(r"^/files/[^/]+/(.+)$", path)
    if m and workspace:
        cand = os.path.join(str(workspace), m.group(1).replace("/", os.sep))
        if os.path.isfile(cand):
            return cand
        return None
    if os.path.isabs(path):
        cand = os.path.normpath(path)
        return cand if os.path.isfile(cand) else None
    if workspace:
        cand = os.path.normpath(os.path.join(str(workspace), path))
        if os.path.isfile(cand):
            return cand
    return None


# ─────────────────────────── PDF 构建 ───────────────────────────

# 数学符号归一：Unicode 减号（U+2212）等在中文字体里常缺字形，PDF 里显示成方框/丢失
# （实机：第 1/6 页机械核对式"（= Δ归母净利润 □ Δ毛利…"）。公式符号必须可读，
# 因此统一映射到 ASCII 等价字符——这些字符在所有字体里都有字形。
_MATH_SYMBOL_MAP = {
    "\u2212": "-",    # MINUS SIGN
    "\uff0d": "-",    # FULLWIDTH HYPHEN-MINUS
    "\u2010": "-",    # HYPHEN
    "\u2011": "-",    # NON-BREAKING HYPHEN
    "\u2013": "-",    # EN DASH
    "\u2014": "-",    # EM DASH
    "\u00a0": " ",    # NBSP（断行异常的一个来源）
    "\u2009": " ",    # THIN SPACE
    "\u202f": " ",    # NARROW NBSP
}


def normalize_math_symbols(text: str) -> str:
    """把易缺字形的数学/排版符号换成 ASCII 等价字符（可读性优先）。"""
    out = str(text or "")
    for k, v in _MATH_SYMBOL_MAP.items():
        out = out.replace(k, v)
    return out


def _escape_text(text: str) -> bytes:
    """标准字体（无嵌入 TTF）的文本字符串转义（Latin-1 近似）。"""
    out = bytearray()
    for ch in text:
        cp = ord(ch)
        if cp < 256:
            b = bytes([cp])
            if ch in ("\\", "(", ")"):
                b = b"\\" + b
            out += b
        else:
            out += b"?"
    return bytes(out)


class _PDFBuilder:
    def __init__(self, font: TTFont | None) -> None:
        self.font = font
        self.objects: list[bytes] = []
        self.page_refs: list[int] = []
        # 已排完的页：(内容字节, 该页图片对象号)。页脚要在总页数确定后统一补，
        # 所以页面内容先攒在这里，由 finish() 落成对象（见 _finish_page）。
        self.page_bodies: list[tuple[bytes, list[int]]] = []
        self.image_refs: dict[str, int] = {}
        self.font_ref: int | None = None
        self.used_glyphs: dict[int, int] = {}  # gid -> unicode cp
        # 字形回退：中文字体常常**不含** ASCII/数字（实测 CI 的
        # DroidSansFallbackFull.ttf：`glyph_id("1") == 0`），那样正文里的数字、
        # 英文、文件名会整段渲染成空白。缺字形的单字节字符改用 PDF 内置
        # Helvetica（base-14，不必嵌入、任何阅读器都有）画，保证看得见。
        self.fallback_ref: int | None = None
        self.fallback_used = False
        # obj 1 Catalog, obj 2 Pages
        self.objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        self.objects.append(b"<< /Type /Pages /Kids [] /Count 0 >>")
        if font is not None:
            self.font_ref = self._add_font(font)
        else:
            # 无中文字体时回退内置 Helvetica（西文可用，中文显示为 ?）
            self.font_ref = self._add_obj(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
            )
        # 当前页状态
        self.page_content: bytearray = bytearray()
        self.page_images: list[int] = []
        self.cursor_y = 0.0
        self._new_page()

    def _add_obj(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def _new_page(self) -> None:
        self.page_content = bytearray()
        self.page_images = []
        self.cursor_y = PAGE_H - MARGIN_T - BODY_SIZE

    def _add_font(self, font: TTFont) -> int:
        """嵌入 TTF：FontFile2 → FontDescriptor → CIDFont → Type0 → ToUnicode。"""
        font_bytes = font.data
        compressed = zlib.compress(font_bytes, 9)
        ff2 = self._add_obj(
            b"<< /Length1 " + str(len(font_bytes)).encode()
            + b" /Length " + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n"
            + compressed + b"\nendstream"
        )
        bbox = (
            f"[{font.x_min} {font.y_min} {font.x_max} {font.y_max}]"
        ).encode()
        desc = self._add_obj(
            b"<< /Type /FontDescriptor /FontName /WMCNFont /Flags 4 "
            b"/FontBBox " + bbox
            + b" /ItalicAngle 0 /Ascent " + str(font.ascent).encode()
            + b" /Descent " + str(font.descent).encode()
            + b" /CapHeight 700 /StemV 80 /FontFile2 " + str(ff2).encode()
            + b" 0 R >>"
        )
        # W 数组：按 1000 单位 em 转换（CIDFontType2 约定）
        upem = font.units_per_em or 1000
        widths: list[int] = []
        for gid in range(font.num_glyphs):
            if gid < font.num_hmetrics:
                widths.append(round(font._advances[gid] * 1000 / upem))
            else:
                widths.append(widths[-1] if widths else 1000)
        w_arr = " ".join(str(w) for w in widths).encode()
        cid = self._add_obj(
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /WMCNFont "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
            b"/Supplement 0 >> /FontDescriptor " + str(desc).encode() + b" 0 R "
            + b" /W [0 [" + w_arr + b"]] >>"
        )
        toc = self._add_obj(self._to_unicode_placeholder())
        type0 = self._add_obj(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /WMCNFont "
            b"/Encoding /Identity-H /DescendantFonts [" + str(cid).encode()
            + b" 0 R] /ToUnicode " + str(toc).encode() + b" 0 R >>"
        )
        return type0

    @staticmethod
    def _to_unicode_placeholder() -> bytes:
        cmap = (
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) "
            b"/Supplement 0 >> def\n/CMapName /Adobe-Identity-UCS def\n"
            b"/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\n"
            b"endcodespacerange\n0 beginbfchar\nendbfchar\nendcmap\n"
            b"CMapName currentdict /CMap defineresource pop\nend\nend"
        )
        return b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" \
            + cmap + b"\nendstream"

    def _finalize_to_unicode(self) -> None:
        """回填 ToUnicode（占位对象改造成带 bfchar 的真实 CMap）。"""
        if not self.font_ref or self.font_ref <= 3:
            return
        # 构造顺序：ff2, desc, cid, toc, type0 → ToUnicode = type0 - 1
        toc_ref = self.font_ref - 1
        entries = [
            f"<{gid:04X}> <{cp:04X}>"
            for gid, cp in sorted(self.used_glyphs.items())
        ]
        body = (
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) "
            b"/Supplement 0 >> def\n/CMapName /Adobe-Identity-UCS def\n"
            b"/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\n"
            b"endcodespacerange\n"
        )
        if entries:
            body += f"{len(entries)} beginbfchar\n".encode()
            body += ("\n".join(entries)).encode() + b"\nendbfchar\n"
        body += (
            b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
        )
        self.objects[toc_ref - 1] = (
            b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n"
            + body + b"\nendstream"
        )

    def _needs_fallback(self, ch: str) -> bool:
        """该字符是否要交给内置 Helvetica 画（字体缺字形且落在单字节可编码范围）。"""
        if self.font is None:
            return False
        if ord(ch) >= 256:          # 多字节字符 Helvetica 也画不了，仍交给嵌入字体
            return False
        return self.font.glyph_id(ch) == 0

    def _text_runs(self, text: str) -> list[tuple[str, bool]]:
        """把一段文本切成 (片段, 是否走回退字体) 的运行序列。"""
        runs: list[tuple[str, bool]] = []
        for ch in text:
            fb = self._needs_fallback(ch)
            if runs and runs[-1][1] == fb:
                runs[-1] = (runs[-1][0] + ch, fb)
            else:
                runs.append((ch, fb))
        return runs

    def _text_ops(self, x: float, y: float, text: str, size: float,
                  color: tuple) -> bytes:
        """一段文本的绘制指令（不改游标）。

        缺字形的字符走内置 Helvetica：否则它们会以 glyph 0（.notdef）落笔——页面
        上是空白，抽取文本里是 \\x00（实测 CI 上「第 1 页」变成「第  页」）。
        """
        r, g, b = color
        ops = b""
        cur_x = x
        for run_text, fallback in self._text_runs(text):
            if fallback:
                if not self.fallback_used:
                    self.fallback_used = True
                ref_name = b"/F2"
                encoded = b"(" + _escape_text(run_text) + b")"
            else:
                if self.font is not None:
                    parts: list[str] = []
                    for ch in run_text:
                        gid = self.font.glyph_id(ch)
                        if gid:
                            self.used_glyphs[gid] = ord(ch)
                        parts.append(f"{gid:04X}")
                    encoded = ("<" + "".join(parts) + ">").encode()
                else:
                    encoded = b"(" + _escape_text(run_text) + b")"
                ref_name = b"/F1"
            ops += (
                b"BT " + ref_name + f" {size:.2f} Tf {r} {g} {b} rg "
                f"1 0 0 1 {cur_x:.2f} {y:.2f} Tm ".encode()
            ) + encoded + b" Tj ET\n"
            cur_x += self._run_width(run_text, size, fallback)
        return ops

    def _run_width(self, text: str, size: float, fallback: bool) -> float:
        if fallback or self.font is None:
            return len(text) * size * 0.55
        return self.font.text_width(text, size)

    def _draw_text(self, x: float, text: str, size: float, color: tuple) -> None:
        self.page_content += self._text_ops(x, self.cursor_y, text, size, color)

    def _text_width(self, text: str, size: float) -> float:
        """文本宽度：缺字形的字符按 Helvetica 的近似宽度计入（与绘制口径一致）。"""
        if self.font is None:
            return len(text) * size * 0.55
        total = 0.0
        for run_text, fallback in self._text_runs(text):
            total += self._run_width(run_text, size, fallback)
        return total

    def _footer_ops(self, index: int, total: int) -> bytes:
        """页脚：细线 + 居中页码「第 N 页 / 共 M 页」。

        没有页码时，"这份 PDF 到第几页了、还有多少"只能靠滚动手感判断；
        跨页长表格尤其容易让人以为内容被截断。
        """
        y = MARGIN_B * 0.45
        label = f"第 {index} 页 / 共 {total} 页"
        w = self._text_width(label, 8.5)
        ops = (
            f"q 0.80 0.80 0.83 RG 0.5 w {MARGIN_L:.2f} {y + 12:.2f} m "
            f"{MARGIN_L + USABLE_W:.2f} {y + 12:.2f} l S Q\n"
        ).encode()
        ops += self._text_ops(
            MARGIN_L + max(0.0, (USABLE_W - w) / 2), y, label, 8.5,
            (0.42, 0.44, 0.48),
        )
        return ops

    def _ensure_space(self, needed: float) -> None:
        if self.cursor_y - needed < MARGIN_B:
            self._finish_page()
            self._new_page()

    def _finish_page(self) -> None:
        if not self.page_content and not self.page_images:
            return
        # 不在这里写内容对象：页脚要等总页数确定（见 finish()）。先攒在内存里。
        self.page_bodies.append((bytes(self.page_content), list(self.page_images)))

    def _embed_image(self, src: str, workspace: os.PathLike | None) -> int | None:
        fp = _resolve_image_src(src, workspace)
        if not fp:
            return None
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception:
            return None
        if fp in self.image_refs:
            return self.image_refs[fp]
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            parsed = _png_to_rgb(data)
            if not parsed:
                return None
            w, h, rgb = parsed
            raw = zlib.compress(rgb, 9)
            body = (
                b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode()
                + b" /Height " + str(h).encode()
                + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                + b"/Filter /FlateDecode /Length " + str(len(raw)).encode()
                + b" >>\nstream\n" + raw + b"\nendstream"
            )
        else:
            info = _jpeg_info(data)
            if not info:
                return None
            w, h = info
            body = (
                b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode()
                + b" /Height " + str(h).encode()
                + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                + b"/Filter /DCTDecode /Length " + str(len(data)).encode()
                + b" >>\nstream\n" + data + b"\nendstream"
            )
        ref = self._add_obj(body)
        self.image_refs[fp] = ref
        return ref

    def _draw_image_placeholder(self, src: str, reason: str = "") -> None:
        """图片没能嵌入时画一个占位框，把"这里本来有图"说出来。

        此前是**静默 return**：读者只看到正文里缺了一块，既不知道有图、也不知道
        为什么没有（路径失效/格式不支持/文件为空都表现成"什么都没发生"）。
        """
        label = str(src or "").strip() or "（未提供图片路径）"
        lines = self._wrap(f"[图片未能嵌入] {label}", 9.0, USABLE_W - 16)
        if reason:
            lines += self._wrap(f"原因：{reason}", 8.5, USABLE_W - 16)
        box_h = 12.0 * len(lines) + 16
        self._ensure_space(box_h + 6)
        top = self.cursor_y
        self.page_content += (
            f"q 0.94 0.94 0.96 rg {MARGIN_L:.2f} {top - box_h:.2f} "
            f"{USABLE_W:.2f} {box_h:.2f} re f Q "
            f"q 0.72 0.72 0.76 RG 0.6 w {MARGIN_L:.2f} {top - box_h:.2f} "
            f"{USABLE_W:.2f} {box_h:.2f} re S Q "
            f"q 0.72 0.72 0.76 RG 0.6 w {MARGIN_L:.2f} {top - box_h / 2:.2f} m "
            f"{MARGIN_L + USABLE_W:.2f} {top - box_h / 2:.2f} l S Q\n"
        ).encode()
        y = top - 14
        for line in lines:
            self.page_content += self._text_ops(
                MARGIN_L + 8, y, line, 9.0, (0.42, 0.44, 0.48))
            y -= 12.0
        self.cursor_y = top - box_h - BODY_SIZE

    def _draw_image(self, src: str, workspace: os.PathLike | None) -> None:
        ref = self._embed_image(src, workspace)
        if ref is None:
            self._draw_image_placeholder(src, reason="文件不存在或格式不受支持")
            return
        # 取宽高：重新读取（简单起见）；最大宽度 USABLE_W
        fp = _resolve_image_src(src, workspace)
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception:
            self._draw_image_placeholder(src, reason="读取失败")
            return
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            parsed = _png_to_rgb(data)
            if not parsed:
                self._draw_image_placeholder(src, reason="PNG 解析失败")
                return
            w, h = parsed[0], parsed[1]
        else:
            info = _jpeg_info(data)
            if not info:
                self._draw_image_placeholder(src, reason="既不是 PNG 也不是 JPEG")
                return
            w, h = info
        max_w = USABLE_W
        scale = min(1.0, max_w / w)
        dw, dh = w * scale, h * scale
        # 单页放不下的图不嵌：`_ensure_space(dh)` 会翻页，但翻页之后图片底边仍落在
        # 页面之外（整张图在可视区外），页面上什么都看不到——实测一张 1038×121366
        # 的畸形 PNG（画布被 tight bbox 撑爆）就这样"嵌进去了却看不见"。
        # 这种图按缺图处理：留可见占位，并让导出完整性判定失败。
        if dh > USABLE_H:
            self._draw_image_placeholder(
                src, reason=f"图片尺寸异常（{w}×{h}，高宽比 {h / max(w, 1):.1f}），"
                            f"单页放不下")
            return
        self._ensure_space(dh + BODY_SIZE)
        # 图片底部 = cursor 上方
        y_bottom = self.cursor_y - dh - 4
        idx = len(self.page_images)
        self.page_images.append(ref)
        self.page_content += (
            f"q {dw:.2f} 0 0 {dh:.2f} {MARGIN_L:.2f} {y_bottom:.2f} "
            f"cm /Im{idx} Do Q\n"
        ).encode()
        self.cursor_y = y_bottom - BODY_SIZE

    # 数字 token：折行时不得在数字内部断开（"23.24%" 被拆成 "23" / ".24%" 会让读数
    # 失真，实机第 1 页即如此）。断开点若落在数字/小数/百分号/单位之间，就往前退到
    # 该 token 之前整段换行。
    _NUM_TOKEN_RE = re.compile(r"[0-9][0-9,\.]*%?")

    def _safe_break(self, cur: str) -> int:
        """在 cur 里选一个安全断点（返回保留长度）：不切断数字 token，也不把负号留在行尾。

        09-23 实机：`（-12.83%）` 在行尾断成 `（-` + `12.83%）`——负号与数字分家。
        """
        m = None
        for m in self._NUM_TOKEN_RE.finditer(cur):
            pass
        if m is None:
            if cur.endswith("-"):
                return len(cur) - 1 if len(cur) > 1 else len(cur)
            return len(cur)
        start, end = m.span()
        if end < len(cur):
            return len(cur)          # 最后一个数字 token 已结束，正常断
        # 断点落在数字 token 中间：退到该 token 起点（起点为 0 时只能硬断）
        if start <= 0:
            return len(cur)
        # 数字前面紧跟负号（-12.83 / −12.83）：负号跟着数字一起走，不留行尾
        if cur[start - 1] in ("-", "−"):
            return start - 1 if start - 1 > 0 else len(cur)
        return start

    def _wrap(self, text: str, size: float, max_w: float) -> list[str]:
        """按字符宽度折行（中文/英文混排），数字 token 不拆开。"""
        if not text:
            return [""]
        lines: list[str] = []
        cur = ""
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            trial = cur + ch
            if self._text_width(trial, size) <= max_w or not cur:
                cur = trial
            else:
                cut = self._safe_break(cur)
                # 收尾标点不留行首（"…）。" 的句号被甩到下一行）：断点后紧跟收尾标点时
                # 把它一起留在本行
                while (cut < len(cur) and cur[cut] in "。，、；：）】」”』!.?,;:)]"
                       and self._text_width(cur[:cut + 1], size) <= max_w * 1.06):
                    cut += 1
                if cut < len(cur):
                    lines.append(cur[:cut].rstrip())
                    cur = cur[cut:] + ch
                else:
                    lines.append(cur)
                    cur = ch
        if cur or not lines:
            lines.append(cur)
        return lines

    def _image_display_height(self, src: str, workspace: os.PathLike | None) -> float:
        """图片按可用宽度缩放后的显示高度（拿不到就按占位框高度）。"""
        fp = _resolve_image_src(src, workspace)
        if not fp:
            return 12.0 * 2 + 16 + 6
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception:
            return 12.0 * 2 + 16 + 6
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            parsed = _png_to_rgb(data)
            if not parsed:
                return 12.0 * 2 + 16 + 6
            w, h = parsed[0], parsed[1]
        else:
            info = _jpeg_info(data)
            if not info:
                return 12.0 * 2 + 16 + 6
            w, h = info
        if not w or not h:
            return 12.0 * 2 + 16 + 6
        scale = min(1.0, USABLE_W / float(w))
        dh = float(h) * scale
        if dh > USABLE_H:
            return 12.0 * 2 + 16 + 6      # 畸形图按占位框处理
        return dh + BODY_SIZE + 4

    def _next_block_need(self, nxt: dict | None,
                         workspace: os.PathLike | None = None) -> float:
        """下一块至少需要多少高度（用于标题随内容，避免页末孤行）。"""
        if not nxt:
            return BODY_SIZE * 2.4
        t = str(nxt.get("type") or "")
        if t == "image":
            return self._image_display_height(str(nxt.get("src") or ""), workspace)
        if t == "table":
            rows = nxt.get("rows") or []
            return min(len(rows) + 1, 4) * (BODY_SIZE * 1.6)
        return BODY_SIZE * 2.4

    def _draw_wrapped(self, text: str, size: float, color: tuple, *,
                      x: float | None = None, indent: float = 0.0,
                      line_gap: float | None = None) -> None:
        """逐行绘制并按**每行**检查剩余空间：长段落可拆到下一页。

        09-23 实机：首页结论段只在段首 `_ensure_space` 一次，随后逐行画到底——
        141 个非空白字符落在页面下边界之外（文本抽取读得到、人眼看不到）。
        """
        gap = size * 1.62 if line_gap is None else line_gap
        x0 = MARGIN_L if x is None else x
        for line in self._wrap(str(text or ""), size, USABLE_W - indent):
            self._ensure_space(gap + size * 0.7)
            self._draw_text(x0 + indent, line, size, color)
            self.cursor_y -= gap

    def _render_block(self, block: dict, workspace: os.PathLike | None,
                      nxt: dict | None = None) -> None:
        btype = block.get("type")
        if btype == "heading":
            size = _HEADING_SIZES.get(int(block.get("level") or 1), BODY_SIZE)
            # 标题后必须放得下**下一块**（图片按图片高度），否则换页——不留"标题在
            # 页末、内容在下一页"的孤行（实机第 2 页末的"图表"标题）
            self._ensure_space(size * 1.9 + self._next_block_need(nxt, workspace))
            self.cursor_y -= size * 0.55
            self._draw_text(MARGIN_L, str(block.get("text") or ""), size,
                            (0.08, 0.13, 0.24))
            self.cursor_y -= size * 1.35
        elif btype == "para":
            self._draw_wrapped(str(block.get("text") or ""), BODY_SIZE,
                               (0.10, 0.10, 0.12))
            self.cursor_y -= BODY_SIZE * 0.45
        elif btype == "list":
            for idx, item in enumerate(block.get("items") or []):
                marker = (
                    f"{idx + 1}. " if block.get("ordered") else "- "
                )
                prefix_w = self._text_width(marker, BODY_SIZE)
                lines = self._wrap(
                    str(item), BODY_SIZE, USABLE_W - prefix_w - 8,
                )
                self._ensure_space(BODY_SIZE * 1.62 * max(1, len(lines)))
                first = True
                for line in lines:
                    # R2：**逐行**查空间——超长列表项可跨页续排。离线条目整项只在开头
                    # 查一次空间，多出来的行被画到页面下边界之外（离线生产渲染探针：
                    # 一个超长列表项有 1010 个非空白字符落在页外）。
                    self._ensure_space(BODY_SIZE * 1.62)
                    if first:
                        self._draw_text(
                            MARGIN_L, marker + line, BODY_SIZE,
                            (0.10, 0.10, 0.12),
                        )
                        first = False
                    else:
                        self._draw_text(
                            MARGIN_L + prefix_w + 8, line, BODY_SIZE,
                            (0.10, 0.10, 0.12),
                        )
                    self.cursor_y -= BODY_SIZE * 1.62
            self.cursor_y -= BODY_SIZE * 0.3
        elif btype == "code":
            lines = str(block.get("text") or "").split("\n")
            self._ensure_space(CODE_SIZE * 1.55 * min(len(lines), 3) + 12)
            self.page_content += (
                f"q {0.95} {0.95} {0.97} rg "
                f"{MARGIN_L:.2f} {self.cursor_y - 4:.2f} "
                f"{USABLE_W:.2f} {len(lines) * CODE_SIZE * 1.55 + 10:.2f} re f Q\n"
            ).encode()
            self.cursor_y -= 10
            for line in lines:
                for sub in self._wrap(line, CODE_SIZE, USABLE_W - 12):
                    # R2：代码块同样逐行查空间（可跨页续排，不再画到页外）
                    self._ensure_space(CODE_SIZE * 1.55)
                    self._draw_text(
                        MARGIN_L + 8, sub, CODE_SIZE, (0.22, 0.25, 0.30),
                    )
                    self.cursor_y -= CODE_SIZE * 1.55
            self.cursor_y -= 6
        elif btype == "quote":
            # 09-23：quote 与 para 同类缺陷——只在段首查一次空间，长引用会画到页外。
            # 逐行查空间（引用块可跨页续画）。
            for line in block.get("lines") or []:
                self._draw_wrapped(str(line), BODY_SIZE, (0.35, 0.36, 0.40),
                                   indent=14, line_gap=BODY_SIZE * 1.55)
            self.cursor_y -= BODY_SIZE * 0.3
        elif btype == "table":
            self._render_table(block.get("rows") or [])
        elif btype == "image":
            self._draw_image(str(block.get("src") or ""), workspace)
        elif btype == "hr":
            self._ensure_space(8)
            self.cursor_y -= 8
            # 分隔线：`m`/`l` 各自只吃两个操作数（x y）。此前这里多写了一个宽度操作数
            # （`x y W 0 m x2 y l`），PDF 取**最后两个**数当起点 → 线被画成从
            # (W, 0) 到 (x2, y) 的斜线：位置、长度、方向都不是本意。
            self.page_content += (
                f"q 0.75 0.75 0.78 RG 1 w {MARGIN_L:.2f} {self.cursor_y:.2f} m "
                f"{MARGIN_L + USABLE_W:.2f} {self.cursor_y:.2f} l S Q\n"
            ).encode()
            self.cursor_y -= 14

    def _draw_table_header(self, top_y: float, header_lines: list[list[str]],
                           col_w: float, header_h: float) -> None:
        """画表头（底色 + **逐列**标题）。跨页续画时也走这里，保证一致。

        底色矩形覆盖 `[top_y - header_h, top_y]`，**文字基线要落在框内**
        （此前直接用未调整的 cursor_y 画，白字落在色块上方，视觉上是"表头跑出框"）。

        标题与数据列**共用列宽与 x 起点**（`MARGIN_L + c * col_w + 4`）：此前表头把
        各列名用两个空格拼成一行画在左上角，正文却按列定位，读者会把年度与数字错配
        （实测正常场景第 1 页"指标 2023 2024 口径 来源"全挤在左侧）。
        """
        self.page_content += (
            f"q 0.16 0.20 0.34 rg {MARGIN_L:.2f} {top_y - header_h:.2f} "
            f"{USABLE_W:.2f} {header_h:.2f} re f Q\n"
        ).encode()
        baseline = top_y - 5 - TABLE_SIZE
        for c, lines in enumerate(header_lines):
            x = MARGIN_L + c * col_w + 4
            for line in lines:
                self.page_content += self._text_ops(x, baseline, line, TABLE_SIZE, (1, 1, 1))
                baseline -= TABLE_SIZE * 1.55
            baseline = top_y - 5 - TABLE_SIZE
        self.cursor_y = top_y - header_h

    def _render_table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        col_w = USABLE_W / ncols
        cell_lines: list[list[list[str]]] = []
        for r in rows:
            r = (list(r) + [""] * (ncols - len(r)))[:ncols]
            cell_lines.append([
                self._wrap(str(c), TABLE_SIZE, col_w - 8)
                for c in r
            ])
        line_h = TABLE_SIZE * 1.55
        pad = 5
        header_lines = cell_lines[0]
        header_h = max(len(lines) for lines in header_lines) * line_h + pad * 2
        # 只要求"表头 + 几行数据"放得下就开画，**不再要求整张表**放得下：
        # 此前用 total_h（45 行表格的整高）预留，结果整表被推到下一页，
        # 第一页只剩标题、85% 版面空白（视觉门禁实测）。逐行的分页与跨页重画表头
        # 已经能处理续页，所以这里只需要保证"表头不会孤零零留在页底"。
        min_h = header_h + pad * 2 + line_h * 2 + 12
        self._ensure_space(min_h)
        self.cursor_y -= 6
        y = self.cursor_y
        self._draw_table_header(y, header_lines, col_w, header_h)
        y -= header_h
        for row in cell_lines[1:]:
            row_h = max(len(lines) for lines in row) * line_h + pad * 2
            if y - row_h - header_h < MARGIN_B:
                # 跨页：先给本页留表头位置，再在新页**重画表头**——否则续页的第一行
                # 数据没有任何列名，"第 4 行那个数字是哪一列"只能回到上一页翻。
                self._finish_page()
                self._new_page()
                y = self.cursor_y
                self._draw_table_header(y, header_lines, col_w, header_h)
                y -= header_h
            self.cursor_y = y - pad - TABLE_SIZE
            for c, lines in enumerate(row):
                x = MARGIN_L + c * col_w + 4
                for li, line in enumerate(lines):
                    self._draw_text(x, line, TABLE_SIZE, (0.10, 0.10, 0.12))
                    self.cursor_y -= line_h
                self.cursor_y = y - pad - TABLE_SIZE
            self.page_content += (
                f"q 0.72 0.73 0.76 RG 0.6 w {MARGIN_L:.2f} {y - row_h:.2f} m "
                f"{MARGIN_L + USABLE_W:.2f} {y - row_h:.2f} l S Q\n"
            ).encode()
            y -= row_h
        self.cursor_y = y - 10

    def finish(self) -> bytes:
        self._finish_page()
        # 页脚要写「第 N 页 / 共 M 页」，总页数只有此刻才知道：先把各页内容补上页脚，
        # 再生成对象与 ToUnicode 映射（页脚用到的字形必须在 finalize 之前登记）。
        total = len(self.page_bodies)
        bodies = [
            (content + self._footer_ops(idx, total), images)
            for idx, (content, images) in enumerate(self.page_bodies, start=1)
        ]
        # 有过字形回退就登记内置 Helvetica，并在页面资源里挂成 /F2
        if self.fallback_used and self.fallback_ref is None:
            self.fallback_ref = self._add_obj(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>"
            )
        self._finalize_to_unicode()
        for content, images in bodies:
            content_ref = self._add_obj(
                b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                + content + b"\nendstream"
            )
            resources = b"<< /Font << /F1 " + str(self.font_ref).encode() + b" 0 R"
            if self.fallback_ref:
                resources += b" /F2 " + str(self.fallback_ref).encode() + b" 0 R"
            resources += b" >>"
            if images:
                imgs = b" ".join(
                    b"/Im" + str(i).encode() + b" " + str(ref).encode() + b" 0 R"
                    for i, ref in enumerate(images)
                )
                resources += b" /XObject << " + imgs + b" >>"
            resources += b" >>"
            page_ref = self._add_obj(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
                + f"{PAGE_W:.2f} {PAGE_H:.2f}".encode()
                + b"] /Resources " + resources + b" /Contents "
                + str(content_ref).encode() + b" 0 R >>"
            )
            self.page_refs.append(page_ref)
        # 回填 Pages Kids/Count
        kids = b" ".join(
            str(ref).encode() + b" 0 R" for ref in self.page_refs
        )
        self.objects[1] = (
            b"<< /Type /Pages /Kids [" + kids + b"] /Count "
            + str(len(self.page_refs)).encode() + b" >>"
        )
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for idx, obj in enumerate(self.objects, start=1):
            offsets.append(out.tell())
            out.write(f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref_pos = out.tell()
        out.write(
            f"xref\n0 {len(self.objects) + 1}\n".encode()
            + b"0000000000 65535 f \n"
        )
        for off in offsets[1:]:
            out.write(f"{off:010d} 00000 n \n".encode())
        out.write(
            f"trailer\n<< /Size {len(self.objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n".encode()
        )
        return out.getvalue()


def markdown_to_pdf(
    markdown: str,
    title: str = "",
    workspace: os.PathLike | None = None,
) -> bytes:
    """把任务报告 Markdown 转成 PDF 字节流（标题 + 正文 + 表格 + 图表）。"""
    # 公式符号先归一：Unicode 减号等在 PDF 里会显示成方框/丢失（实机第 1/6 页机械核对式）
    markdown = normalize_math_symbols(markdown)
    # 标题用同一套归一：否则正文 H1 里的 en dash 被归一成 '-' 而 title 参数没有，
    # "封面标题与正文首个标题重复时跳过"的判断会失配 → 标题印两遍
    if title:
        title = normalize_math_symbols(str(title))
    font = _load_font()
    builder = _PDFBuilder(font)
    # 标题
    if title:
        builder._ensure_space(22)
        for line in builder._wrap(str(title), 16, USABLE_W):
            builder._draw_text(MARGIN_L, line, 16, (0.08, 0.13, 0.24))
            builder.cursor_y -= 21
        builder.cursor_y -= 10
        builder.page_content += (
            f"q 0.20 0.42 0.72 RG 1.2 w {MARGIN_L:.2f} {builder.cursor_y:.2f} m "
            f"{MARGIN_L + 80:.2f} {builder.cursor_y:.2f} l S Q\n"
        ).encode()
        builder.cursor_y -= 16
    blocks = _split_blocks(markdown)
    # 封面标题与正文首个标题重复时跳过正文那一个：报告 Markdown 常自带
    # `# 同一标题`，此前会连着出现两遍标题（实测输出 "复核结论 | 复核结论"），
    # 既占版面又像排版事故。
    #
    # 找的是**第一个标题块**而不是 blocks[0]：未验收草稿的正文形状是
    # "引用横幅在前、`# 标题` 在后"，只检查首块会漏（视觉门禁实测：草稿样例里
    # 标题仍然印了两遍）。前置的横幅/说明不属于"正文标题"，不影响判断。
    if title:
        want = _norm_title(str(title))
        for i, blk in enumerate(blocks):
            if blk.get("type") != "heading":
                continue
            if _norm_title(str(blk.get("text") or "")) == want:
                blocks = blocks[:i] + blocks[i + 1:]
            break
    for i, block in enumerate(blocks):
        builder._render_block(block, workspace,
                              blocks[i + 1] if i + 1 < len(blocks) else None)
    return builder.finish()


# ─────────────────────── 结构自检（导出完整性） ───────────────────────
#
# 为什么要有这一层：页数与文本量只证明"PDF 能被解析"，证明不了"该有的图进去了"。
# 实测三个离线场景 PDF 分别缺 5/2/4 张图（引用图全部渲染成"[图片未能嵌入]"占位），
# 而当时的页数/字符检查一律 pass。这里用内容流做**确定性**判定：数嵌入的
# XObject 图、找缺图占位、核对表头与数据列是否共用同一列网格。

_TEXT_OP_RE = re.compile(
    rb"BT /F\d+ ([\d.]+) Tf ([\d.]+) ([\d.]+) ([\d.]+) rg "
    rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm (.*?) Tj ET", re.S)
_HEADER_FILL_RE = re.compile(
    rb"q 0\.16 0\.20 0\.34 rg ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re f Q")
_IMAGE_XOBJECT_RE = re.compile(rb"/Subtype\s*/Image")
_OBJ_RE = re.compile(rb"(\d+) 0 obj\n(.*?)\nendobj", re.S)
_PLACEHOLDER_TEXT = "图片未能嵌入"
# 表格正文的字色（与正文段落同色但字号不同，见 `_render_table`）
_TABLE_TEXT_COLOR = (0.1, 0.1, 0.12)


def _row_key(run: dict) -> int:
    """同一基线（0.5pt 内）算同一行。"""
    return round(run["y"] * 2)


def _column_starts(runs: list[dict]) -> tuple[list[float], int]:
    """从一组表格文本段推出**列位**：在多数行里都出现的 x。

    单元格文字换行会在同一 x 多画一行、字形回退会在同一单元格内多画一段（x 连续
    推进）——两者都不改变"列位"这一事实，所以用"多行共有"来定列，不依赖字宽估算。
    返回（列位 x 列表，行数）。
    """
    rows: dict[int, list[dict]] = {}
    for r in runs:
        rows.setdefault(_row_key(r), []).append(r)
    counts: dict[float, int] = {}
    for _, row in rows.items():
        for x in {round(r["x"], 2) for r in row}:
            counts[x] = counts.get(x, 0) + 1
    if not counts:
        return [], 0
    need = max(1, int(len(rows) * 0.6 + 0.999))
    return sorted(x for x, c in counts.items() if c >= need), len(rows)


def _row_spread(runs: list[dict]) -> float:
    """一组文本段的横向跨度（最右 - 最左）。"""
    xs = [r["x"] for r in runs]
    return (max(xs) - min(xs)) if xs else 0.0


def _page_contents(pdf_bytes: bytes) -> list[bytes]:
    """按**页序**取出各页内容流（本模块写出的 PDF 未压缩，直接取字节）。"""
    objs = {int(m.group(1)): m.group(2) for m in _OBJ_RE.finditer(pdf_bytes)}
    pages: list[tuple[int, bytes]] = []
    for num, body in objs.items():
        if b"/Type /Page " not in body or b"/Contents" not in body:
            continue
        cm = re.search(rb"/Contents (\d+) 0 R", body)
        if not cm:
            continue
        content = objs.get(int(cm.group(1)), b"")
        sm = re.search(rb"stream\n(.*?)\nendstream", content, re.S)
        pages.append((num, sm.group(1) if sm else b""))
    pages.sort()
    return [body for _, body in pages]


def _text_runs_in_pdf(pdf_bytes: bytes) -> list[dict]:
    """PDF 内容流里的文本绘制指令 → `{page, x, y, size, color, text}`。

    只认本模块 `_text_ops` 写出的形状（`BT /F1 <size> Tf <r> <g> <b> rg 1 0 0 1 x y Tm <..> Tj ET`），
    内容流未压缩，因此直接按字节解析即可（不引第三方 PDF 库）。
    非 ASCII 走子集字体的十六进制字形码，无法反解回原文——这里只保留长度与坐标，
    需要**文本内容**的判定（如缺图占位）改从页面文本抽取做，见 `pdf_image_report`。
    """
    out: list[dict] = []
    for page, body in enumerate(_page_contents(pdf_bytes), 1):
        for t in _TEXT_OP_RE.finditer(body):
            size, r, g, b, x, y, payload = t.groups()
            out.append({"page": page, "x": float(x), "y": float(y),
                        "size": float(size), "color": (float(r), float(g), float(b)),
                        "glyphs": max(0, (len(payload) - 2) // 4)
                        if payload.startswith(b"<") else len(payload),
                        "literal": payload[1:-1].decode("latin-1", "replace")
                        if not payload.startswith(b"<") else ""})
    return out


def pdf_page_image_counts(pdf_bytes: bytes) -> list[int]:
    """每页实际绘制的图片数（按页序）。整页只有一张图时文本量极小，
    "文字少"不等于"空白页"——判空白必须把图算进去。"""
    out = []
    for body in _page_contents(pdf_bytes):
        out.append(len(re.findall(rb"/Im\d+ Do", body)))
    return out


def pdf_image_report(pdf_bytes: bytes, *, expected: int = 0,
                     text: str | None = None) -> dict:
    """嵌入图与缺图占位的**确定性**读数。

    `images` 是页面资源里真实登记的图片 XObject 数；`placeholders` 是渲染成
    "[图片未能嵌入]" 的引用数（从**页面文本**数，因为占位文案走子集字体的字形码，
    内容流里读不出原文）。`expected` 由调用方按该报告引用的图表数给出——
    "有几张 PNG 落在磁盘上"不等于"PDF 里有几张图"。

    `drawn_on_page` 是**真的画在页面可视区内**的图片数：登记了 XObject 但变换矩阵把
    它放到页面外（例如畸形尺寸导致高度 5 万 pt）时，页面上什么都看不到——那种情况
    必须算缺图，不能因为"资源里有一个 Image 对象"就报"已嵌入"。
    """
    images = len(_IMAGE_XOBJECT_RE.findall(pdf_bytes))
    drawn = 0
    oversized = 0
    for body in _page_contents(pdf_bytes):
        for m in re.finditer(rb"q ([\d.]+) 0 0 ([\d.]+) ([\d.]+) ([\d.]+) cm /Im\d+ Do Q", body):
            dw, dh, x, y = (float(v) for v in m.groups())
            if dw <= 0 or dh <= 0:
                continue
            if dh > USABLE_H or dw > USABLE_W:
                oversized += 1
                continue
            if y < MARGIN_B - 1 or y + dh > PAGE_H - MARGIN_T + 1:
                oversized += 1                      # 底边或顶边落在可视区之外
                continue
            drawn += 1
    if text is None:
        text = ""
        try:
            from pypdf import PdfReader
            import io as _io
            text = "".join((pg.extract_text() or "") for pg in PdfReader(_io.BytesIO(pdf_bytes)).pages)
        except Exception:                              # noqa: BLE001 - 读不了就没有文本证据
            text = ""
    placeholders = text.count(_PLACEHOLDER_TEXT)
    # 占位框里会写"原因：…"，但排版会按宽度折行，抽取文本里可能夹换行——
    # 去掉所有空白再找关键词，免得"尺寸异常"被断行拆开而漏判。
    flat = re.sub(r"\s+", "", text)
    oversized_placeholder = "尺寸异常" in flat
    issues: list[str] = []
    if placeholders:
        issues.append(f"{placeholders} 处图片未能嵌入"
                      + ("（尺寸异常）" if oversized_placeholder else ""))
    if oversized:
        issues.append(f"{oversized} 张图被放到页面可视区之外（尺寸异常）")
    if expected and drawn < expected:
        issues.append(f"应有 {expected} 张图，实际画在页面上 {drawn} 张")
    return {"images": images, "drawn_on_page": drawn, "oversized": oversized,
            "expected": int(expected or 0),
            "placeholders": placeholders,
            "oversized_placeholder": oversized_placeholder,
            "ok": not issues, "issues": issues}



def pdf_table_grids(pdf_bytes: bytes) -> list[dict]:
    """每张表的**表头/数据列网格**读数：表头单元格是否落在数据列的列位上。

    表的定位：表头底色（`0.16 0.20 0.34 rg` 的填充矩形）标出顶部；数据行是同一页
    中位于表头之下、下一张表头之上的表格正文（表格正文的颜色与字号是固定的
    `(0.10,0.10,0.12)` / `TABLE_SIZE`，正文段落是 `BODY_SIZE`，据此区分）。

    "单元格"= 同一行里 x 间距 > `_CELL_GAP` 的文本段；同一单元格内的字形回退会把
    一段文字拆成多个绘制指令（x 连续推进），按间距合并回一格。判定：表头单元格
    数必须与数据行单元格数一致，且表头每个格位都能在数据行里找到同 x 的格位。
    拼接式表头（各列名连成一个字符串画在左上角）会退化成 1 格 → 判不对齐。
    """
    runs = _text_runs_in_pdf(pdf_bytes)
    headers: list[dict] = []
    for page, body in enumerate(_page_contents(pdf_bytes), 1):
        for f in _HEADER_FILL_RE.finditer(body):
            x, y, w, h = (float(v) for v in f.groups())
            headers.append({"page": page, "top": y + h, "bottom": y, "left": x, "width": w})
    grids: list[dict] = []
    for i, hd in enumerate(headers):
        nxt = next((h["top"] for h in headers[i + 1:] if h["page"] == hd["page"]), None)
        white = [r for r in runs if r["page"] == hd["page"] and r["color"] == (1.0, 1.0, 1.0)
                 and hd["bottom"] <= r["y"] <= hd["top"] + 1]
        cells = [
            r for r in runs
            if r["page"] == hd["page"] and r["color"] == _TABLE_TEXT_COLOR
            and r["size"] == TABLE_SIZE
            and r["y"] < hd["bottom"] and (nxt is None or r["y"] > nxt)
        ]
        header_x = sorted({round(r["x"], 2) for r in white})
        body_x = sorted({round(r["x"], 2) for r in cells})
        h_span, b_span = _row_spread(white), _row_spread(cells)
        # 判定用**平台无关**的三条（字形回退会把同一格拆成多段，各平台拆法不同）：
        # ① 表头必须铺满数据列所在的横向范围——拼接式表头把各列名画在一个 x 上，
        #    跨度退化为 0，这一条直接抓住它；
        # ② 表头格位多数要落在数据列位上（允许少数因字形回退产生的偏移）；
        # ③ 首列仍从左边距 +4 起。
        on_grid = [hx for hx in header_x
                   if any(abs(hx - bx) <= 0.6 for bx in body_x)]
        need_on_grid = max(1, int(len(header_x) * 0.6 + 0.999)) if header_x else 0
        col_w = USABLE_W / len(header_x) if header_x else 0.0
        if not header_x:
            aligned = True
        elif b_span > 0:
            aligned = (h_span >= 0.9 * b_span and len(on_grid) >= need_on_grid
                       and abs(header_x[0] - (MARGIN_L + 4)) <= 0.6)
        else:
            aligned = (abs(header_x[0] - (MARGIN_L + 4)) <= 0.6
                       and all(abs((header_x[j + 1] - header_x[j]) - col_w) <= 0.6
                               for j in range(len(header_x) - 1)))
        grids.append({"page": hd["page"], "cols": len(on_grid),
                      "header_cols": header_x, "body_cols": body_x,
                      "header_span": round(h_span, 1), "body_span": round(b_span, 1),
                      "col_w": round(col_w, 2), "aligned": aligned})
    return grids


