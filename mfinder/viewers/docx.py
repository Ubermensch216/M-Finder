"""Word (.docx) reader -> ``model.Doc`` (runs, styles, numbering, tables, pictures)."""

from __future__ import annotations

import posixpath
import re
import zipfile

from . import common as C
from .model import Cell, Doc, Image, Para, Run, Table

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_V = "{urn:schemas-microsoft-com:vml}"
NS_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"

MAX_BLOCKS = 4000
HIGHLIGHT = {
    "yellow": "#ffff00", "green": "#00ff00", "cyan": "#00ffff", "magenta": "#ff00ff", "blue": "#0000ff",
    "red": "#ff0000", "darkBlue": "#000080", "darkCyan": "#008080", "darkGreen": "#008000",
    "darkMagenta": "#800080", "darkRed": "#800000", "darkYellow": "#808000", "darkGray": "#808080",
    "lightGray": "#c0c0c0", "black": "#000000", "white": "#ffffff",
}


def _w(el, name):
    return el.find(NS_W + name) if el is not None else None


def _wa(el, name, key="val"):
    """Attribute ``w:<key>`` of the child ``w:<name>`` (None if absent)."""
    c = _w(el, name)
    return None if c is None else c.get(NS_W + key)


def _on(el, name) -> bool | None:
    """Toggle property: None if absent, else True/False."""
    c = _w(el, name)
    if c is None:
        return None
    v = c.get(NS_W + "val")
    return v not in ("0", "false", "off", "none")


class _Styles:
    def __init__(self, root) -> None:
        self.p: dict[str, dict] = {}
        self.defaults_r: dict = {}
        self.table_borders: dict[str, bool] = {}
        self.default_para = None
        if root is None:
            return
        dd = root.find(NS_W + "docDefaults")
        if dd is not None:
            rpr = dd.find(f"{NS_W}rPrDefault/{NS_W}rPr")
            self.defaults_r = self._rpr(rpr)
        for st in root.findall(NS_W + "style"):
            sid = st.get(NS_W + "styleId")
            if not sid:
                continue
            self.p[sid] = {
                "type": st.get(NS_W + "type"),
                "based": _wa(st, "basedOn"),
                "r": self._rpr(_w(st, "rPr")),
                "p": self._ppr(_w(st, "pPr")),
                "default": st.get(NS_W + "default") == "1",
            }
            tb = st.find(f"{NS_W}tblPr/{NS_W}tblBorders")
            if tb is not None:
                self.table_borders[sid] = any(
                    (b.get(NS_W + "val") not in (None, "nil", "none")) for b in tb)
            if st.get(NS_W + "type") == "paragraph" and st.get(NS_W + "default") == "1":
                self.default_para = sid

    @staticmethod
    def _rpr(rpr) -> dict:
        d: dict = {}
        if rpr is None:
            return d
        for key, name in (("b", "b"), ("i", "i"), ("strike", "strike"), ("strike", "dstrike")):
            v = _on(rpr, name)
            if v is not None:
                d[key] = v
        u = _wa(rpr, "u")
        if u is not None:
            d["u"] = u != "none"
        sz = _wa(rpr, "sz")
        if sz:
            d["sz"] = C.to_float(sz) / 2
        col = _wa(rpr, "color")
        if col is not None:
            d["color"] = C.hex_color(col) if col != "auto" else None
        hl = _wa(rpr, "highlight")
        if hl:
            d["bg"] = HIGHLIGHT.get(hl)
        shd = _w(rpr, "shd")
        if shd is not None and shd.get(NS_W + "fill") not in (None, "auto"):
            d["bg"] = C.hex_color(shd.get(NS_W + "fill"))
        va = _wa(rpr, "vertAlign")
        if va:
            d["sup"] = {"superscript": 1, "subscript": -1}.get(va, 0)
        rf = _w(rpr, "rFonts")
        if rf is not None:
            d["font"] = rf.get(NS_W + "eastAsia") or rf.get(NS_W + "ascii") or rf.get(NS_W + "hAnsi")
        return d

    @staticmethod
    def _ppr(ppr) -> dict:
        d: dict = {}
        if ppr is None:
            return d
        jc = _wa(ppr, "jc")
        if jc:
            d["align"] = {"center": "center", "right": "right", "end": "right", "both": "justify",
                          "distribute": "justify"}.get(jc, "left")
        ind = _w(ppr, "ind")
        if ind is not None:
            left = ind.get(NS_W + "left") or ind.get(NS_W + "start")
            if left is not None:
                d["left"] = C.to_float(left) / 20
            fl, hang = ind.get(NS_W + "firstLine"), ind.get(NS_W + "hanging")
            if hang is not None:
                d["first"] = -C.to_float(hang) / 20
            elif fl is not None:
                d["first"] = C.to_float(fl) / 20
        sp = _w(ppr, "spacing")
        if sp is not None:
            if sp.get(NS_W + "before") is not None:
                d["before"] = C.to_float(sp.get(NS_W + "before")) / 20
            if sp.get(NS_W + "after") is not None:
                d["after"] = C.to_float(sp.get(NS_W + "after")) / 20
            line, rule = sp.get(NS_W + "line"), sp.get(NS_W + "lineRule")
            if line and rule in (None, "auto"):
                d["line"] = C.to_float(line) / 240
        shd = _w(ppr, "shd")
        if shd is not None and shd.get(NS_W + "fill") not in (None, "auto"):
            d["bg"] = C.hex_color(shd.get(NS_W + "fill"))
        return d

    def resolve(self, sid, kind: str) -> dict:
        out: dict = {}
        chain, seen = [], set()
        while sid and sid in self.p and sid not in seen:
            seen.add(sid)
            chain.append(self.p[sid])
            sid = self.p[sid]["based"]
        for st in reversed(chain):
            out.update(st[kind])
        return out


class _Numbering:
    def __init__(self, root) -> None:
        self.abstract: dict[str, dict[int, dict]] = {}
        self.nums: dict[str, str] = {}
        self.counters: dict[tuple, int] = {}
        if root is None:
            return
        for an in root.findall(NS_W + "abstractNum"):
            lv = {}
            for l in an.findall(NS_W + "lvl"):
                ind = _w(_w(l, "pPr"), "ind")
                left = first = 0.0
                if ind is not None:
                    left = C.to_float(ind.get(NS_W + "left") or ind.get(NS_W + "start")) / 20
                    hang = ind.get(NS_W + "hanging")
                    first = -C.to_float(hang) / 20 if hang else C.to_float(ind.get(NS_W + "firstLine")) / 20
                lv[C.to_int(l.get(NS_W + "ilvl"))] = {
                    "fmt": _wa(l, "numFmt") or "decimal", "text": _wa(l, "lvlText") or "",
                    "start": C.to_int(_wa(l, "start"), 1), "left": left, "first": first,
                }
            self.abstract[an.get(NS_W + "abstractNumId")] = lv
        for n in root.findall(NS_W + "num"):
            self.nums[n.get(NS_W + "numId")] = _wa(n, "abstractNumId")

    @staticmethod
    def _fmt(n: int, fmt: str) -> str:
        if fmt == "lowerLetter":
            return chr(ord("a") + (n - 1) % 26)
        if fmt == "upperLetter":
            return chr(ord("A") + (n - 1) % 26)
        if fmt in ("lowerRoman", "upperRoman"):
            vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                    (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
            s, x = "", n
            for v, t in vals:
                while x >= v:
                    s, x = s + t, x - v
            return s.upper() if fmt == "upperRoman" else s
        if fmt == "ganada":
            return "가나다라마바사아자차카타파하"[(n - 1) % 14]
        if fmt in ("decimalEnclosedCircle", "decimalEnclosedCircleChinese"):
            return chr(0x2460 + n - 1) if 1 <= n <= 20 else str(n)
        return str(n)

    def label(self, num_id: str, ilvl: int):
        """-> (label text, left pt, first pt) or None."""
        lv = self.abstract.get(self.nums.get(num_id, ""), {})
        info = lv.get(ilvl)
        if info is None:
            return None
        if info["fmt"] == "bullet":
            txt = info["text"]
            return ("•" if not txt or ord(txt[0]) >= 0xF000 or txt in "o" else txt[0]) + " ", info["left"], info["first"]
        key = (num_id, ilvl)
        self.counters[key] = self.counters.get(key, info["start"] - 1) + 1
        for k in [k for k in self.counters if k[0] == num_id and k[1] > ilvl]:
            del self.counters[k]

        def repl(m):
            lvl = int(m.group(1)) - 1
            ci = lv.get(lvl, info)
            n = self.counters.get((num_id, lvl), ci["start"])
            return self._fmt(n, ci["fmt"])

        text = re.sub(r"%(\d)", repl, info["text"] or f"%{ilvl + 1}.")
        return text + " ", info["left"], info["first"]


class _Reader:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        names = set(zf.namelist())
        self.doc_path = "word/document.xml"
        if self.doc_path not in names:
            raise ValueError("no word/document.xml")
        self.rels = self._load_rels(self.doc_path)
        self.styles = _Styles(self._xml("word/styles.xml", names))
        self.numbering = _Numbering(self._xml("word/numbering.xml", names))
        self.blocks_n = 0
        self.truncated = False
        base = self.styles.defaults_r.get("sz", 10.0)
        self.base_size = base
        self.default_font = self.styles.defaults_r.get("font")

    def _xml(self, name, names):
        if name not in names:
            return None
        try:
            return C.parse_xml(C.read_zip_part(self.zf, name))
        except Exception:
            return None

    def _load_rels(self, part: str) -> dict:
        rels_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        out = {}
        if rels_path in self.zf.namelist():
            root = C.parse_xml(C.read_zip_part(self.zf, rels_path))
            base = posixpath.dirname(part)
            for r in root:
                tgt = r.get("Target", "")
                if r.get("TargetMode") == "External":
                    out[r.get("Id")] = ("ext", tgt)
                else:
                    path = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join(base, tgt))
                    out[r.get("Id")] = ("int", path)
        return out

    def image(self, rid, width=None, height=None):
        ent = self.rels.get(rid)
        if not ent or ent[0] != "int" or ent[1] not in self.zf.namelist():
            return None
        info = self.zf.getinfo(ent[1])
        if info.file_size > C.MAX_EMBED_IMAGE:
            return None
        return Image(C.read_zip_part(self.zf, ent[1]), width, height)

    # ---- paragraphs ----
    def run_props(self, rpr, base: dict) -> Run:
        d = dict(base)
        d.update(_Styles._rpr(rpr))
        rs = _wa(rpr, "rStyle") if rpr is not None else None
        if rs:
            tmp = self.styles.resolve(rs, "r")
            tmp.update(_Styles._rpr(rpr))
            d.update(tmp)
        return Run(bold=bool(d.get("b")), italic=bool(d.get("i")), underline=bool(d.get("u")),
                   strike=bool(d.get("strike")), size=d.get("sz"), color=d.get("color"), bg=d.get("bg"),
                   sup=d.get("sup", 0), font=d.get("font"))

    def runs_of(self, el, base: dict, out: list, link: str | None = None) -> None:
        for ch in el:
            tag = ch.tag
            if tag == NS_W + "r":
                self._run(ch, base, out, link)
            elif tag in (NS_W + "hyperlink", NS_W + "ins", NS_W + "smartTag", NS_W + "fldSimple", NS_W + "sdtContent",
                         NS_W + "customXml"):
                lk = link
                if tag == NS_W + "hyperlink":
                    ent = self.rels.get(ch.get(NS_R + "id"))
                    lk = ent[1] if ent else (ch.get(NS_W + "anchor") or link)
                self.runs_of(ch, base, out, lk)
            elif tag == NS_W + "sdt":
                c = ch.find(NS_W + "sdtContent")
                if c is not None:
                    self.runs_of(c, base, out, link)
            elif tag == NS_MC + "AlternateContent":
                choice = ch.find(NS_MC + "Choice")
                self.runs_of(choice if choice is not None else ch, base, out, link)

    def _run(self, r, base, out, link):
        run = self.run_props(r.find(NS_W + "rPr"), base)
        if link:
            run.underline = True
            run.color = run.color or "#0563c1"
            run.link = link
        buf: list[str] = []

        def flush():
            if buf:
                nr = Run(**{**run.__dict__, "text": "".join(buf)})
                out.append(nr)
                buf.clear()

        for ch in r:
            t = ch.tag
            if t in (NS_W + "t", NS_W + "instrText"):
                if t == NS_W + "t":
                    buf.append(ch.text or "")
            elif t == NS_W + "tab":
                buf.append("\t")
            elif t in (NS_W + "br", NS_W + "cr"):
                if ch.get(NS_W + "type") == "page":
                    flush()
                    out.append("PAGEBREAK")
                else:
                    buf.append("\n")
            elif t == NS_W + "noBreakHyphen":
                buf.append("‑")
            elif t == NS_W + "sym":
                code = ch.get(NS_W + "char")
                if code:
                    try:
                        buf.append(chr(int(code, 16) & 0xFFFF if int(code, 16) < 0xF000 else 0x2022))
                    except ValueError:
                        pass
            elif t == NS_W + "drawing":
                flush()
                img = self._drawing(ch)
                if img:
                    out.append(img)
            elif t == NS_W + "pict":
                flush()
                for im in ch.iter(NS_V + "imagedata"):
                    img = self.image(im.get(NS_R + "id"))
                    if img:
                        out.append(img)
                        break
            elif t == NS_MC + "AlternateContent":
                flush()
                choice = ch.find(NS_MC + "Choice")
                for d in (choice if choice is not None else ch).iter(NS_W + "drawing"):
                    img = self._drawing(d)
                    if img:
                        out.append(img)
        flush()

    def _drawing(self, dr):
        ext = None
        for e in dr.iter(NS_WP + "extent"):
            ext = e
            break
        w = h = None
        if ext is not None:
            w, h = C.to_float(ext.get("cx")) / 12700, C.to_float(ext.get("cy")) / 12700
        for blip in dr.iter(NS_A + "blip"):
            return self.image(blip.get(NS_R + "embed") or blip.get(NS_R + "link"), w, h)
        return None

    def para(self, p, in_cell: bool = False) -> list:
        """-> list of blocks (a paragraph may be split by page breaks)."""
        ppr = p.find(NS_W + "pPr")
        sid = _wa(ppr, "pStyle") if ppr is not None else None
        sid = sid or self.styles.default_para
        pp = dict(self.styles.resolve(sid, "p"))
        pp.update(_Styles._ppr(ppr))
        base_r = dict(self.styles.defaults_r)
        base_r.update(self.styles.resolve(sid, "r"))
        # paragraph mark run properties don't style the text; only the style does
        out: list = []
        self.runs_of(p, base_r, out)
        label = None
        num = _w(ppr, "numPr") if ppr is not None else None
        if num is None and sid in self.styles.p:
            # numbering can also come from the paragraph style
            pass
        if num is not None:
            nid = _wa(num, "numId")
            if nid and nid != "0":
                label = self.numbering.label(nid, C.to_int(_wa(num, "ilvl")))
        blocks, cur = [], []

        def make(runs):
            para = Para(runs=runs, align=pp.get("align", "left"), left=pp.get("left", 0.0), first=pp.get("first", 0.0),
                        before=pp.get("before", 0.0), after=pp.get("after", 0.0), line=pp.get("line"), bg=pp.get("bg"))
            return para

        for item in out:
            if item == "PAGEBREAK":
                blocks.append(make(cur))
                blocks.append(Para(rule=True))
                cur = []
            else:
                cur.append(item)
        blocks.append(make(cur))
        if label is not None and blocks:
            txt, left, first = label
            first_para = blocks[0]
            r0 = first_para.runs[0] if first_para.runs and isinstance(first_para.runs[0], Run) else None
            first_para.runs.insert(0, Run(text=txt, bold=r0.bold if r0 else False, size=r0.size if r0 else None))
            if "left" not in pp or first_para.left == 0:
                first_para.left = left
                first_para.first = first
        return blocks

    # ---- tables ----
    def table(self, tbl) -> Table:
        tpr = tbl.find(NS_W + "tblPr")
        grid = tbl.find(NS_W + "tblGrid")
        col_w = [C.to_float(g.get(NS_W + "w")) / 20 for g in grid.findall(NS_W + "gridCol")] if grid is not None else []
        border = False
        if tpr is not None:
            tb = tpr.find(NS_W + "tblBorders")
            if tb is not None:
                border = any((b.get(NS_W + "val") not in (None, "nil", "none")) for b in tb)
            ts = _wa(tpr, "tblStyle")
            if ts and self.styles.table_borders.get(ts):
                border = True
            elif ts and "grid" in ts.lower():
                border = True
        rows: list[list[Cell]] = []
        open_merge: dict[int, Cell] = {}
        for tr in tbl.findall(NS_W + "tr"):
            row: list[Cell] = []
            col = 0
            for tc in tr.findall(NS_W + "tc"):
                tcpr = tc.find(NS_W + "tcPr")
                span = C.to_int(_wa(tcpr, "gridSpan"), 1) if tcpr is not None else 1
                vm = _w(tcpr, "vMerge") if tcpr is not None else None
                if vm is not None and vm.get(NS_W + "val") in (None, "continue"):
                    top = open_merge.get(col)
                    if top is not None:
                        top.rowspan += 1
                    col += span
                    continue
                cell = Cell(colspan=span)
                if tcpr is not None:
                    shd = _w(tcpr, "shd")
                    if shd is not None and shd.get(NS_W + "fill") not in (None, "auto"):
                        cell.bg = C.hex_color(shd.get(NS_W + "fill"))
                    tw = _w(tcpr, "tcW")
                    if tw is not None and tw.get(NS_W + "type") == "dxa":
                        cell.width = C.to_float(tw.get(NS_W + "w")) / 20
                    va = _wa(tcpr, "vAlign")
                    if va:
                        cell.valign = {"center": "center", "bottom": "bottom"}.get(va, "top")
                    tcb = tcpr.find(NS_W + "tcBorders")
                    if tcb is not None and any(b.get(NS_W + "val") not in (None, "nil", "none") for b in tcb):
                        border = True
                cell.blocks = self.container(tc, in_cell=True) or [Para()]
                if vm is not None:
                    open_merge[col] = cell
                else:
                    for k in range(col, col + span):
                        open_merge.pop(k, None)
                row.append(cell)
                col += span
            rows.append(row)
        if not col_w:
            n = max((sum(c.colspan for c in r) for r in rows), default=0)
            col_w = [None] * n
        t = Table(rows=rows, col_widths=col_w, border=border)
        for r in rows:
            for c in r:
                c.border = border
        return t

    def container(self, el, in_cell: bool = False) -> list:
        out: list = []
        for ch in el:
            if self.blocks_n >= MAX_BLOCKS:
                self.truncated = True
                break
            if ch.tag == NS_W + "p":
                out.extend(self.para(ch, in_cell))
                self.blocks_n += 1
            elif ch.tag == NS_W + "tbl":
                out.append(self.table(ch))
                self.blocks_n += 1
            elif ch.tag == NS_W + "sdt":
                c = ch.find(NS_W + "sdtContent")
                if c is not None:
                    out.extend(self.container(c, in_cell))
            elif ch.tag == NS_MC + "AlternateContent":
                pass
        return out


def read(path: str) -> Doc:
    with zipfile.ZipFile(path) as zf:
        rd = _Reader(zf)
        root = C.parse_xml(C.read_zip_part(zf, rd.doc_path))
        body = root.find(NS_W + "body")
        if body is None:
            raise ValueError("no body")
        blocks = rd.container(body)
    doc = Doc(blocks=blocks, base_size=rd.base_size or 10.0, font=rd.default_font)
    if rd.truncated:
        doc.note = C.truncated_note()
    return doc
