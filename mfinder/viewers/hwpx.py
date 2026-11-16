"""HWPX (.hwpx, Hangul's OWPML/XML zip) reader -> ``model.Doc``."""

from __future__ import annotations

import re
import zipfile

from . import common as C
from .model import Cell, Doc, Image, Para, Run, Table

L = C.local
MAX_BLOCKS = 4000
ALIGN = {"JUSTIFY": "justify", "LEFT": "left", "RIGHT": "right", "CENTER": "center",
         "DISTRIBUTE": "justify", "DISTRIBUTE_SPACE": "justify"}
STRIKE_SHAPES = frozenset({
    "SOLID", "DOT", "DASH", "DASH_DOT", "DASH_DOT_DOT", "LONG_DASH",
    "CIRCLE", "DOUBLE_SLIM", "SLIM_THICK", "THICK_SLIM", "SLIM_THICK_SLIM",
    "WAVE", "DOUBLE_WAVE",
})


def _is_strikeout(s) -> bool:
    if s is None:
        return False
    stype = (s.get("type") or "").upper()
    if stype == "NONE":
        return False
    sshape = (s.get("shape") or "").upper()
    return sshape in STRIKE_SHAPES if sshape else bool(stype)


class _Header:
    def __init__(self, root) -> None:
        self.chars: dict[str, dict] = {}
        self.paras: dict[str, dict] = {}
        self.fills: dict[str, dict] = {}
        self.fonts: dict[str, str] = {}
        if root is None:
            return
        for ff in C.find_all(root, "fontface"):
            if (ff.get("lang") or "").upper() == "HANGUL":
                for f in C.children(ff, "font"):
                    self.fonts[f.get("id")] = f.get("face")
        for cp in C.find_all(root, "charPr"):
            d = {"size": C.to_float(cp.get("height"), 1000) / 100, "color": C.hex_color(cp.get("textColor")),
                 "bg": C.hex_color(cp.get("shadeColor")) if cp.get("shadeColor") not in (None, "none") else None}
            d["bold"] = C.child(cp, "bold") is not None
            d["italic"] = C.child(cp, "italic") is not None
            u = C.child(cp, "underline")
            d["underline"] = u is not None and (u.get("type") or "NONE").upper() != "NONE"
            s = C.child(cp, "strikeout")
            d["strike"] = _is_strikeout(s)
            d["sup"] = 1 if C.child(cp, "supscript") is not None else (-1 if C.child(cp, "subscript") is not None else 0)
            fr = C.child(cp, "fontRef")
            if fr is not None:
                d["font"] = self.fonts.get(fr.get("hangul"))
            self.chars[cp.get("id")] = d
        for pp in C.find_all(root, "paraPr"):
            d = {"align": "left"}
            al = C.child(pp, "align")
            if al is not None:
                d["align"] = ALIGN.get((al.get("horizontal") or "LEFT").upper(), "left")
            margin = next(iter(C.find_all(pp, "margin")), None)
            if margin is not None:
                def mv(name):
                    c = C.child(margin, name)
                    return C.to_float(c.get("value")) / 100 if c is not None else 0.0
                d["left"], d["first"] = mv("left"), mv("intent")
                d["before"], d["after"] = mv("prev"), mv("next")
            ls = next(iter(C.find_all(pp, "lineSpacing")), None)
            if ls is not None and (ls.get("type") or "PERCENT").upper() == "PERCENT":
                v = C.to_float(ls.get("value"), 160)
                if 50 <= v <= 400:
                    d["line"] = v / 100
            self.paras[pp.get("id")] = d
        for bf in C.find_all(root, "borderFill"):
            d = {"bg": None, "border": False}
            for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
                b = C.child(bf, side)
                if b is not None and (b.get("type") or "NONE").upper() not in ("NONE", ""):
                    d["border"] = True
            for wb in C.find_all(bf, "winBrush"):
                fc = wb.get("faceColor")
                if fc and fc.lower() != "none":
                    d["bg"] = C.hex_color(fc)
            self.fills[bf.get("id")] = d


class _Reader:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        names = zf.namelist()
        self.names = names
        hdr = None
        for n in names:
            if re.fullmatch(r"Contents/header\.xml", n, re.I):
                hdr = C.parse_xml(C.read_zip_part(zf, n))
                break
        self.h = _Header(hdr)
        self.bin = {}
        for n in names:
            if n.lower().startswith("bindata/"):
                stem = n.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                self.bin[stem] = n
        self.count = 0
        self.truncated = False

    def image(self, el) -> Image | None:
        ref = None
        for im in C.find_all(el, "img"):
            ref = im.get("binaryItemIDRef")
            break
        if not ref or ref not in self.bin:
            return None
        name = self.bin[ref]
        if self.zf.getinfo(name).file_size > C.MAX_EMBED_IMAGE:
            return None
        w = h = None
        sz = next(iter(C.find_all(el, "curSz")), None) or next(iter(C.find_all(el, "sz")), None)
        if sz is not None:
            w, h = C.to_float(sz.get("width")) / 100, C.to_float(sz.get("height")) / 100
            if not w or not h:
                w = h = None
        return Image(C.read_zip_part(self.zf, name), w, h)

    def text_of(self, t) -> str:
        out = [t.text or ""]
        for c in t:
            n = L(c.tag)
            if n == "tab":
                out.append("\t")
            elif n == "lineBreak":
                out.append("\n")
            elif n in ("nbSpace", "fwSpace"):
                out.append(" ")
            elif n == "hyphen":
                out.append("-")
            out.append(c.tail or "")
        return "".join(out)

    def para(self, p) -> list:
        pp = self.h.paras.get(p.get("paraPrIDRef"), {})
        para = Para(align=pp.get("align", "left"), left=pp.get("left", 0.0), first=pp.get("first", 0.0),
                    before=pp.get("before", 0.0), after=pp.get("after", 0.0), line=pp.get("line"))
        extra: list = []
        for run in C.children(p, "run"):
            cp = self.h.chars.get(run.get("charPrIDRef"), {})
            for c in run:
                n = L(c.tag)
                if n == "t":
                    txt = self.text_of(c)
                    if txt:
                        para.runs.append(Run(text=txt, bold=cp.get("bold", False), italic=cp.get("italic", False),
                                             underline=cp.get("underline", False), strike=cp.get("strike", False),
                                             size=cp.get("size"), color=cp.get("color"), bg=cp.get("bg"),
                                             sup=cp.get("sup", 0), font=cp.get("font")))
                elif n == "tbl":
                    extra.append(self.table(c))
                elif n == "pic":
                    img = self.image(c)
                    if img:
                        para.runs.append(img)
                elif n in ("rect", "ellipse", "container", "polygon", "line", "arc", "curve", "textart", "ole"):
                    for pic in C.find_all(c, "pic"):
                        img = self.image(pic)
                        if img:
                            para.runs.append(img)
                    for sub in C.find_all(c, "subList"):
                        for sp in C.children(sub, "p"):
                            extra.extend(self.para(sp))
        return [para] + extra

    def table(self, tbl) -> Table:
        bfid = tbl.get("borderFillIDRef")
        border = bool(self.h.fills.get(bfid, {}).get("border"))
        rows: list[list[Cell]] = []
        widths: dict[int, float] = {}
        for tr in C.children(tbl, "tr"):
            row = []
            for tc in C.children(tr, "tc"):
                addr = C.child(tc, "cellAddr")
                span = C.child(tc, "cellSpan")
                sz = C.child(tc, "cellSz")
                fill = self.h.fills.get(tc.get("borderFillIDRef"), {})
                cell = Cell(colspan=max(1, C.to_int(span.get("colSpan"), 1)) if span is not None else 1,
                            rowspan=max(1, C.to_int(span.get("rowSpan"), 1)) if span is not None else 1,
                            bg=fill.get("bg"), border=bool(fill.get("border")) or border)
                if sz is not None:
                    cell.width = C.to_float(sz.get("width")) / 100
                    if addr is not None and cell.colspan == 1:
                        widths[C.to_int(addr.get("colAddr"))] = cell.width
                sub = C.child(tc, "subList")
                if sub is not None:
                    va = (sub.get("vertAlign") or "TOP").upper()
                    cell.valign = {"CENTER": "center", "BOTTOM": "bottom"}.get(va, "top")
                    for p in C.children(sub, "p"):
                        cell.blocks.extend(self.para(p))
                if not cell.blocks:
                    cell.blocks = [Para()]
                if cell.border:
                    border = True
                row.append(cell)
            rows.append(row)
        ncols = C.to_int(tbl.get("colCnt")) or max((sum(c.colspan for c in r) for r in rows), default=0)
        col_w = [widths.get(i) for i in range(ncols)]
        t = Table(rows=rows, col_widths=col_w, border=border)
        for r in rows:
            for c in r:
                c.border = border
        return t

    def section(self, root) -> list:
        out = []
        for p in C.children(root, "p"):
            if self.count >= MAX_BLOCKS:
                self.truncated = True
                break
            out.extend(self.para(p))
            self.count += 1
        return out


def read(path: str) -> Doc:
    with zipfile.ZipFile(path) as zf:
        rd = _Reader(zf)
        secs = sorted((n for n in zf.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n, re.I)),
                      key=C.natural_key)
        if not secs:
            raise ValueError("no sections")
        blocks: list = []
        for n in secs:
            blocks.extend(rd.section(C.parse_xml(C.read_zip_part(zf, n))))
            if rd.truncated:
                break
    return Doc(blocks=blocks, base_size=10.0, note=C.truncated_note() if rd.truncated else "")
