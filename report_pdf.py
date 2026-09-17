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
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
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

    def _wrap(self, text: str, size: float, max_w: float) -> list[str]:
        """按字符宽度折行（中文/英文混排）。"""
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
                lines.append(cur)
                cur = ch
        if cur or not lines:
            lines.append(cur)
        return lines

    def _render_block(self, block: dict, workspace: os.PathLike | None) -> None:
        btype = block.get("type")
        if btype == "heading":
            size = _HEADING_SIZES.get(int(block.get("level") or 1), BODY_SIZE)
            self._ensure_space(size * 1.9)
            self.cursor_y -= size * 0.55
            self._draw_text(MARGIN_L, str(block.get("text") or ""), size,
                            (0.08, 0.13, 0.24))
            self.cursor_y -= size * 1.35
        elif btype == "para":
            self._ensure_space(BODY_SIZE * 1.7)
            for line in self._wrap(
                str(block.get("text") or ""), BODY_SIZE, USABLE_W,
            ):
                self._draw_text(MARGIN_L, line, BODY_SIZE, (0.10, 0.10, 0.12))
                self.cursor_y -= BODY_SIZE * 1.62
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
            self._ensure_space(CODE_SIZE * 1.55 * len(lines) + 12)
            self.page_content += (
                f"q {0.95} {0.95} {0.97} rg "
                f"{MARGIN_L:.2f} {self.cursor_y - 4:.2f} "
                f"{USABLE_W:.2f} {len(lines) * CODE_SIZE * 1.55 + 10:.2f} re f Q\n"
            ).encode()
            self.cursor_y -= 10
            for line in lines:
                for sub in self._wrap(line, CODE_SIZE, USABLE_W - 12):
                    self._draw_text(
                        MARGIN_L + 8, sub, CODE_SIZE, (0.22, 0.25, 0.30),
                    )
                    self.cursor_y -= CODE_SIZE * 1.55
            self.cursor_y -= 6
        elif btype == "quote":
            self._ensure_space(BODY_SIZE * 1.7)
            for line in block.get("lines") or []:
                for sub in self._wrap(str(line), BODY_SIZE, USABLE_W - 14):
                    self._draw_text(
                        MARGIN_L + 14, sub, BODY_SIZE, (0.35, 0.36, 0.40),
                    )
                    self.cursor_y -= BODY_SIZE * 1.55
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

    def _draw_table_header(self, top_y: float, rows: list[list[str]], ncols: int,
                           header_h: float) -> None:
        """画表头（底色 + 拼接标题）。跨页续画时也走这里，保证一致。

        底色矩形覆盖 `[top_y - header_h, top_y]`，**文字基线要落在框内**
        （此前直接用未调整的 cursor_y 画，白字落在色块上方，视觉上是"表头跑出框"）。
        """
        self.page_content += (
            f"q 0.16 0.20 0.34 rg {MARGIN_L:.2f} {top_y - header_h:.2f} "
            f"{USABLE_W:.2f} {header_h:.2f} re f Q\n"
        ).encode()
        self.cursor_y = top_y - 5 - TABLE_SIZE
        self._draw_text(
            MARGIN_L + 4, "  ".join(
                str(c)[:20] for c in (rows[0] + [""] * ncols)[:ncols]
            ), TABLE_SIZE, (1, 1, 1),
        )

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
        total_h = sum(
            max(len(lines) for lines in row) * line_h + pad * 2
            for row in cell_lines
        )
        header_h = max(len(lines) for lines in cell_lines[0]) * line_h + pad * 2
        self._ensure_space(total_h + 10)
        self.cursor_y -= 6
        y = self.cursor_y
        self._draw_table_header(y, rows, ncols, header_h)
        # 简化实现：表头仅输出拼接文本；正文逐行绘制
        y -= header_h
        for row in cell_lines[1:]:
            row_h = max(len(lines) for lines in row) * line_h + pad * 2
            if y - row_h - header_h < MARGIN_B:
                # 跨页：先给本页留表头位置，再在新页**重画表头**——否则续页的第一行
                # 数据没有任何列名，"第 4 行那个数字是哪一列"只能回到上一页翻。
                self._finish_page()
                self._new_page()
                y = self.cursor_y
                self._draw_table_header(y, rows, ncols, header_h)
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
    if title and blocks:
        first = blocks[0]
        if first.get("type") == "heading" and _norm_title(
                str(first.get("text") or "")) == _norm_title(str(title)):
            blocks = blocks[1:]
    for block in blocks:
        builder._render_block(block, workspace)
    return builder.finish()
