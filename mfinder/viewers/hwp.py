"""HWP 5.x (.hwp, OLE compound file) reader -> ``model.Doc``.

Parses ``DocInfo`` (character / paragraph shapes, border fills, binary item table) and the
``BodyText`` record trees (paragraphs, tables, pictures, text boxes).
"""

from __future__ import annotations

import re
import struct
import zlib

from .. import preview as P
from . import common as C
from .model import Cell, Doc, Image, Para, Run, Table

TAG_ID_MAPPINGS, TAG_BIN_DATA, TAG_FACE_NAME, TAG_BORDER_FILL, TAG_CHAR_SHAPE, TAG_PARA_SHAPE = 17, 18, 19, 20, 21, 25
TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_PARA_CHAR_SHAPE = 66, 67, 68
TAG_CTRL_HEADER, TAG_LIST_HEADER, TAG_TABLE, TAG_PICTURE = 71, 72, 77, 85
MAX_BLOCKS = 4000


class ViewerError(Exception):
    pass


class _Node:
    __slots__ = ("tag", "level", "data", "kids")

    def __init__(self, tag: int, level: int, data: bytes) -> None:
        self.tag, self.level, self.data, self.kids = tag, level, data, []


def _records(data: bytes):
    pos, n = 0, len(data)
    while pos + 4 <= n:
        (hdr,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, level, size = hdr & 0x3FF, (hdr >> 10) & 0x3FF, hdr >> 20
        if size == 0xFFF:
            if pos + 4 > n:
                return
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, level, data[pos : pos + size]
        pos += size


def _tree(data: bytes) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[_Node] = []
    for tag, level, rec in _records(data):
        node = _Node(tag, level, rec)
        while stack and stack[-1].level >= level:
            stack.pop()
        (stack[-1].kids if stack else roots).append(node)
        stack.append(node)
    return roots


def _bgr(v: int) -> str | None:
    if v == 0xFFFFFFFF:
        return None
    return "#%02x%02x%02x" % (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def _u16(d: bytes, o: int, default: int = 0) -> int:
    return struct.unpack_from("<H", d, o)[0] if len(d) >= o + 2 else default


def _u32(d: bytes, o: int, default: int = 0) -> int:
    return struct.unpack_from("<I", d, o)[0] if len(d) >= o + 4 else default


def _i32(d: bytes, o: int, default: int = 0) -> int:
    return struct.unpack_from("<i", d, o)[0] if len(d) >= o + 4 else default


class _DocInfo:
    def __init__(self, data: bytes) -> None:
        self.chars: list[dict] = []
        self.paras: list[dict] = []
        self.fills: list[dict] = []
        self.bins: list[tuple[int, str, int]] = []  # (storage id, ext, attr)
        faces: list[str] = []
        counts: list[int] = []
        for tag, _lvl, d in _records(data):
            try:
                if tag == TAG_ID_MAPPINGS:
                    counts = list(struct.unpack_from(f"<{min(len(d) // 4, 18)}I", d, 0))
                elif tag == TAG_FACE_NAME:
                    n = _u16(d, 1)
                    faces.append(d[3 : 3 + n * 2].decode("utf-16-le", "replace"))
                elif tag == TAG_BIN_DATA:
                    attr = _u16(d, 0)
                    if attr & 0xF in (1, 2) and len(d) >= 6:
                        bid, n = _u16(d, 2), _u16(d, 4)
                        self.bins.append((bid, d[6 : 6 + n * 2].decode("utf-16-le", "replace"), attr))
                    else:
                        self.bins.append((0, "", attr))
                elif tag == TAG_BORDER_FILL:
                    border = any(d[2 + i * 6] != 0 for i in range(4)) if len(d) >= 26 else False
                    bg = None
                    if len(d) >= 40 and _u32(d, 32) & 1:
                        bg = _bgr(_u32(d, 36))
                    self.fills.append({"border": border, "bg": bg})
                elif tag == TAG_CHAR_SHAPE and len(d) >= 56:
                    attr = _u32(d, 46)
                    hangul_faces = faces[: counts[1]] if len(counts) > 1 else faces
                    fid = _u16(d, 0)
                    col = _u32(d, 52)
                    self.chars.append({
                        "size": max(2.0, _i32(d, 42) / 100), "italic": bool(attr & 1), "bold": bool(attr & 2),
                        "underline": ((attr >> 2) & 3) in (1, 3), "strike": ((attr >> 18) & 7) != 0,
                        "sup": 1 if (attr >> 15) & 1 else (-1 if (attr >> 16) & 1 else 0),
                        "color": _bgr(col) if col != 0 else None,
                        "font": hangul_faces[fid] if fid < len(hangul_faces) else None,
                    })
                elif tag == TAG_PARA_SHAPE and len(d) >= 24:
                    attr = _u32(d, 0)
                    align = {0: "justify", 1: "left", 2: "right", 3: "center", 4: "justify", 5: "justify"}.get((attr >> 2) & 7, "left")
                    d_ = {"align": align, "left": _i32(d, 4) / 200, "first": _i32(d, 12) / 200,
                          "before": _i32(d, 16) / 200, "after": _i32(d, 20) / 200}
                    if attr & 3 == 0:
                        v = _u32(d, 24)
                        if 50 <= v <= 400:
                            d_["line"] = v / 100
                    self.paras.append(d_)
            except (struct.error, IndexError):
                continue


def _decode_text(rec: bytes) -> list[tuple[int, str]]:
    """PARA_TEXT -> [(wchar position, character)], control codes resolved."""
    out: list[tuple[int, str]] = []
    i, n, pos = 0, len(rec) // 2 * 2, 0
    while i < n:
        ch = rec[i] | rec[i + 1] << 8
        if ch >= 32:
            out.append((pos, chr(ch)))
            i += 2
            pos += 1
        elif ch == 13:
            break
        elif ch == 10:
            out.append((pos, "\n"))
            i += 2
            pos += 1
        elif ch in (30, 31):
            out.append((pos, " "))
            i += 2
            pos += 1
        elif ch == 0 or 24 <= ch <= 29:
            i += 2
            pos += 1
        else:  # inline / extended control occupying 8 WCHARs
            if ch == 9:
                out.append((pos, "\t"))
            i += 16
            pos += 8
    return out


class _Reader:
    def __init__(self, ole: "P._Ole", compressed: bool) -> None:
        self.ole = ole
        self.compressed = compressed
        di = ole.find("DocInfo")
        if di is None:
            raise ViewerError("unreadable")
        raw = ole.read(di)
        if compressed:
            raw = P._inflate(raw)
        self.info = _DocInfo(raw)
        self.count = 0
        self.truncated = False

    # ---- images ----
    def image(self, bin_item: int, w=None, h=None) -> Image | None:
        if not 1 <= bin_item <= len(self.info.bins):
            return None
        bid, ext, attr = self.info.bins[bin_item - 1]
        if not ext:
            return None
        idx = self.ole.find("BinData", f"BIN{bid:04X}.{ext}")
        if idx is None:
            idx = self.ole.find("BinData", f"BIN{bid:04x}.{ext}")
        if idx is None:
            return None
        data = self.ole.read(idx)
        mode = (attr >> 4) & 3
        if mode == 1 or (mode == 0 and self.compressed):
            try:
                data = P._inflate(data)
            except zlib.error:
                pass
        if len(data) > C.MAX_EMBED_IMAGE:
            return None
        return Image(data, w, h)

    def pictures(self, node: _Node, w, h) -> list:
        out = []
        for k in node.kids:
            if k.tag == TAG_PICTURE and len(k.data) >= 73:
                img = self.image(_u16(k.data, 71), w, h)
                if img:
                    out.append(img)
            out.extend(self.pictures(k, w, h))
        return out

    def textbox_paras(self, node: _Node) -> list:
        out: list = []
        kids = node.kids
        for i, k in enumerate(kids):
            if k.tag == TAG_LIST_HEADER:
                for pk in k.kids:
                    if pk.tag == TAG_PARA_HEADER:
                        out.extend(self.para(pk))
                j = i + 1
                while j < len(kids) and kids[j].tag == TAG_PARA_HEADER:
                    out.extend(self.para(kids[j]))
                    j += 1
            elif k.tag != TAG_PARA_HEADER:
                out.extend(self.textbox_paras(k))
        return out

    # ---- paragraphs / tables ----
    def para(self, node: _Node) -> list:
        d = node.data
        pp = self.info.paras[_u16(d, 8)] if _u16(d, 8) < len(self.info.paras) else {}
        text_rec = None
        shapes: list[tuple[int, int]] = []
        ctrls = []
        for k in node.kids:
            if k.tag == TAG_PARA_TEXT:
                text_rec = k.data
            elif k.tag == TAG_PARA_CHAR_SHAPE:
                n = len(k.data) // 8
                shapes = [struct.unpack_from("<II", k.data, i * 8) for i in range(n)]
            elif k.tag == TAG_CTRL_HEADER:
                ctrls.append(k)
        para = Para(align=pp.get("align", "left"), left=pp.get("left", 0.0), first=pp.get("first", 0.0),
                    before=pp.get("before", 0.0), after=pp.get("after", 0.0), line=pp.get("line"))
        if text_rec:
            chars = _decode_text(text_rec)
            j, buf, cur = 0, [], -1
            shapes.sort()

            def flush():
                if buf:
                    cs = self.info.chars[cur] if 0 <= cur < len(self.info.chars) else {}
                    para.runs.append(Run(text="".join(buf), bold=cs.get("bold", False), italic=cs.get("italic", False),
                                         underline=cs.get("underline", False), strike=cs.get("strike", False),
                                         size=cs.get("size"), color=cs.get("color"), sup=cs.get("sup", 0),
                                         font=cs.get("font")))
                    buf.clear()

            for pos, ch in chars:
                while j + 1 < len(shapes) and shapes[j + 1][0] <= pos:
                    j += 1
                sid = shapes[j][1] if shapes else -1
                if sid != cur:
                    flush()
                    cur = sid
                buf.append(ch)
            flush()
        extra: list = []
        for c in ctrls:
            cid = c.data[:4][::-1]
            try:
                if cid == b"tbl ":
                    extra.append(self.table(c))
                elif cid == b"gso ":
                    w, h = _u32(c.data, 16) / 100, _u32(c.data, 20) / 100
                    para.runs.extend(self.pictures(c, w or None, h or None))
                    extra.extend(self.textbox_paras(c))
            except (struct.error, IndexError, ViewerError):
                continue
        if not para.runs and extra:
            return extra
        return [para] + extra

    def table(self, node: _Node) -> Table:
        nrows = ncols = 0
        cells: list[Cell] = []
        widths: dict[int, float] = {}
        cur: Cell | None = None
        anchors: list[tuple[int, int]] = []
        any_border = False

        def add_paras(nodes):
            for pk in nodes:
                if pk.tag == TAG_PARA_HEADER and cur is not None:
                    cur.blocks.extend(self.para(pk))

        for k in node.kids:
            if k.tag == TAG_TABLE:
                nrows, ncols = _u16(k.data, 4), _u16(k.data, 6)
            elif k.tag == TAG_LIST_HEADER:
                d = k.data
                cur = Cell()
                if len(d) >= 34:
                    col, row = _u16(d, 8), _u16(d, 10)
                    cur.colspan, cur.rowspan = max(1, _u16(d, 12, 1)), max(1, _u16(d, 14, 1))
                    cur.width = _u32(d, 16) / 100
                    bf = _u16(d, 32)
                    fill = self.info.fills[bf - 1] if 1 <= bf <= len(self.info.fills) else {}
                    cur.bg = fill.get("bg")
                    cur.border = bool(fill.get("border"))
                    any_border = any_border or cur.border
                    if cur.colspan == 1:
                        widths[col] = cur.width
                    anchors.append((row, col))
                else:
                    anchors.append((len(anchors), 0))
                cells.append(cur)
                add_paras(k.kids)
            elif k.tag == TAG_PARA_HEADER:
                add_paras([k])
        order = sorted(range(len(cells)), key=lambda i: anchors[i])
        rows: list[list[Cell]] = []
        last = None
        for i in order:
            r = anchors[i][0]
            if r != last:
                rows.append([])
                last = r
            if not cells[i].blocks:
                cells[i].blocks = [Para()]
            rows[-1].append(cells[i])
        col_w = [widths.get(i) for i in range(ncols)]
        t = Table(rows=rows, col_widths=col_w, border=any_border)
        for r in rows:
            for c in r:
                c.border = any_border
        return t

    def section(self, data: bytes) -> list:
        out: list = []
        for node in _tree(data):
            if node.tag != TAG_PARA_HEADER:
                continue
            if self.count >= MAX_BLOCKS:
                self.truncated = True
                break
            try:
                out.extend(self.para(node))
            except (struct.error, IndexError, KeyError):
                pass
            self.count += 1
        return out


def read(path: str) -> Doc:
    with open(path, "rb") as fh:
        data = fh.read(256 << 20)
    try:
        ole = P._Ole(data)
    except (ValueError, struct.error) as e:
        raise ViewerError("unreadable") from e
    hi = ole.find("FileHeader")
    flags = 0
    if hi is not None:
        hdr = ole.read(hi)
        if len(hdr) >= 40:
            flags = struct.unpack_from("<I", hdr, 36)[0]
    if flags & 2 or flags & 4:
        raise ViewerError("encrypted")
    rd = _Reader(ole, bool(flags & 1))
    sections = sorted((n for n in ole.listdir("BodyText") if re.fullmatch(r"Section\d+", n, re.I)), key=C.natural_key)
    blocks: list = []
    for name in sections:
        raw = ole.read(ole.find("BodyText", name))
        if flags & 1:
            raw = P._inflate(raw)
        blocks.extend(rd.section(raw))
        if rd.truncated:
            break
    return Doc(blocks=blocks, base_size=10.0, note=C.truncated_note() if rd.truncated else "")
