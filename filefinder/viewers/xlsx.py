"""Excel (.xlsx) reader -> ``model.Workbook`` (cell styles, merges, column widths, number formats)."""

from __future__ import annotations

import colorsys
import datetime as dt
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from . import common as C
from .model import SCell, Sheet, Workbook

MAX_ROWS = 5000
MAX_COLS = 120
MAX_CELLS = 400_000

BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM",
    20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm", 37: "#,##0;(#,##0)", 38: "#,##0;(#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;(#,##0.00)", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0",
    # East-Asian built-ins (Korean locale)
    27: "yyyy-mm-dd", 28: "yyyy-mm-dd", 29: "yyyy-mm-dd", 30: "yyyy-mm-dd", 31: "yyyy-mm-dd", 32: "h:mm",
    33: "h:mm:ss", 34: "h:mm", 35: "h:mm:ss", 36: "yyyy-mm-dd",
}
KO_DAYS = "월화수목금토일"
THEME_ORDER = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6"]
INDEXED = ["#000000", "#ffffff", "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff", "#00ffff"]


def col_name(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _tint(hexcol: str, tint: float) -> str:
    r, g, b = (int(hexcol[i : i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0, min(1, l)), s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _excel_date(v: float) -> dt.datetime | None:
    try:
        return dt.datetime(1899, 12, 30) + dt.timedelta(days=v)
    except (OverflowError, ValueError):
        return None


def _is_date(code: str) -> bool:
    s = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.', "", code)
    return bool(re.search(r"[ymdhsaA]", s)) and not re.search(r"#|0\.0|0,0", s)


def _fmt_date(v: float, code: str) -> str:
    d = _excel_date(v)
    if d is None:
        return str(v)
    sec = code.split(";")[0]
    ampm = bool(re.search(r"AM/PM|A/P", sec, re.I))
    out, i, n = [], 0, len(sec)
    last_h = False
    toks = []
    while i < n:
        ch = sec[i]
        if ch == '"':
            j = sec.find('"', i + 1)
            j = n if j < 0 else j
            toks.append(("lit", sec[i + 1 : j]))
            i = j + 1
        elif ch == "[":
            j = sec.find("]", i)
            j = n if j < 0 else j
            inner = sec[i + 1 : j]
            toks.append(("elapsed", inner) if inner.lower() in ("h", "hh", "m", "mm", "s", "ss") else ("skip", ""))
            i = j + 1
        elif ch == "\\" and i + 1 < n:
            toks.append(("lit", sec[i + 1]))
            i += 2
        elif ch in "_*":
            i += 2
        elif ch.lower() in "ymdhsa":
            j = i
            while j < n and sec[j].lower() == ch.lower():
                j += 1
            run = sec[i:j]
            if ch.lower() == "a" and sec[i : i + 5].upper() in ("AM/PM",):
                toks.append(("ampm", "")); i += 5; continue
            if ch.lower() == "a" and sec[i : i + 3].upper() == "A/P":
                toks.append(("ampm", "")); i += 3; continue
            toks.append((ch.lower(), run))
            i = j
        else:
            toks.append(("lit", ch))
            i += 1
    for idx, (k, t) in enumerate(toks):
        if k == "m":
            # minute when directly after h or before s
            prev = next((x[0] for x in reversed(toks[:idx]) if x[0] != "lit"), "")
            nxt = next((x[0] for x in toks[idx + 1 :] if x[0] != "lit"), "")
            if prev in ("h", "elapsed") or nxt == "s":
                toks[idx] = ("min", t)
    for k, t in toks:
        if k == "lit":
            out.append(t)
        elif k == "y":
            out.append(f"{d.year:04d}" if len(t) > 2 else f"{d.year % 100:02d}")
        elif k == "m":
            out.append({1: str(d.month), 2: f"{d.month:02d}", 3: d.strftime("%b"), 4: d.strftime("%B")}.get(len(t), str(d.month)))
        elif k == "min":
            out.append(f"{d.minute:02d}" if len(t) > 1 else str(d.minute))
        elif k == "d":
            if len(t) <= 2:
                out.append(f"{d.day:02d}" if len(t) == 2 else str(d.day))
            else:
                out.append(d.strftime("%a") if len(t) == 3 else d.strftime("%A"))
        elif k == "a":
            out.append(KO_DAYS[d.weekday()] + ("요일" if len(t) >= 4 else ""))
        elif k == "h":
            h = d.hour % 12 or 12 if ampm else d.hour
            out.append(f"{h:02d}" if len(t) > 1 else str(h))
        elif k == "s":
            out.append(f"{d.second:02d}" if len(t) > 1 else str(d.second))
        elif k == "ampm":
            out.append("AM" if d.hour < 12 else "PM")
        elif k == "elapsed":
            hrs = int(v * 24)
            out.append(str(hrs))
    return "".join(out)


def _literal(s: str) -> str:
    """Literal text of a format fragment (quotes / escapes resolved)."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = s.find('"', i + 1)
            j = n if j < 0 else j
            out.append(s[i + 1 : j])
            i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        elif c == "_":
            out.append(" ")
            i += 2
        elif c == "*":
            i += 2
        elif c == "[":
            j = s.find("]", i)
            m = re.match(r"\[\$([^\-\]]*)", s[i : j + 1] if j > 0 else "")
            out.append(m.group(1) if m else "")
            i = n if j < 0 else j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_core(sec: str):
    """-> (prefix, numeric core, suffix); core is the run of 0 # ? . , E+ outside quotes."""
    i, n, start = 0, len(sec), -1
    end = -1
    while i < n:
        c = sec[i]
        if c == '"':
            j = sec.find('"', i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "[":
            j = sec.find("]", i)
            i = n if j < 0 else j + 1
            continue
        if c in "0#?" or (c == "." and start >= 0):
            if start < 0:
                start = i
            end = i + 1
        elif c in ",." and start >= 0:
            end = i + 1
        elif c.upper() == "E" and start >= 0 and i + 1 < n and sec[i + 1] in "+-":
            end = i + 2
        elif start >= 0 and c not in "0#?.,":
            break
        i += 1
    if start < 0:
        return sec, "", ""
    return sec[:start], sec[start:end], sec[end:]


def format_number(v: float, code: str) -> str:
    if not code or code.lower() == "general":
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.10g}"
    secs = code.split(";")
    sec, neg_in_sec = secs[0], False
    if v < 0 and len(secs) > 1:
        sec, v, neg_in_sec = secs[1], -v, True
    elif v == 0 and len(secs) > 2:
        sec = secs[2]
    if _is_date(sec):
        return _fmt_date(v, sec)
    pre, core, suf = _split_core(sec)
    if not core:
        return f"{v:.10g}" if v != int(v) else str(int(v))
    pct = "%" in pre or "%" in suf
    if pct:
        v *= 100
    if re.search(r"E[+-]", core, re.I):
        mant, _ = re.split(r"E", core, flags=re.I)[0], 0
        dec = len(mant.split(".")[1]) if "." in mant else 0
        return _literal(pre) + f"{v:.{dec}E}" + _literal(suf)
    dec = len(re.sub(r"[^0#?]", "", core.split(".")[1])) if "." in core else 0
    int_part = core.split(".")[0]
    s = f"{abs(v):,.{dec}f}" if "," in int_part else f"{abs(v):.{dec}f}"
    if dec and re.search(r"#$", core.split(".")[1]):  # trailing optional digits
        s = s.rstrip("0").rstrip(".") if "." in s else s
    if v < 0 and not neg_in_sec:
        s = "-" + s
    return _literal(pre) + s + _literal(suf)


class _Styles:
    def __init__(self, root, theme: list[str]) -> None:
        self.theme = theme
        self.numfmts: dict[int, str] = {}
        self.fonts: list[dict] = []
        self.fills: list[str | None] = []
        self.borders: list[bool] = []
        self.xfs: list[dict] = []
        if root is None:
            return
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        nfs = root.find("m:numFmts", ns)
        for nf in (list(nfs) if nfs is not None else []):
            self.numfmts[C.to_int(nf.get("numFmtId"))] = nf.get("formatCode", "")
        fonts_el = root.find("m:fonts", ns)
        for f in (list(fonts_el) if fonts_el is not None else []):
            d = {"bold": False, "italic": False, "color": None, "size": None}
            for c in f:
                t = C.local(c.tag)
                if t == "b":
                    d["bold"] = c.get("val") not in ("0", "false")
                elif t == "i":
                    d["italic"] = c.get("val") not in ("0", "false")
                elif t == "color":
                    d["color"] = self.color(c)
                elif t == "sz":
                    d["size"] = C.to_float(c.get("val")) or None
            self.fonts.append(d)
        fills_el = root.find("m:fills", ns)
        if fills_el is not None:
            for fl in fills_el:
                pf = fl.find("m:patternFill", ns)
                col = None
                if pf is not None and pf.get("patternType") not in (None, "none"):
                    fg = pf.find("m:fgColor", ns)
                    col = self.color(fg) if fg is not None else None
                    if col is None and pf.get("patternType") == "solid":
                        col = "#d9d9d9"
                self.fills.append(col)
        borders_el = root.find("m:borders", ns)
        if borders_el is not None:
            for b in borders_el:
                self.borders.append(any(c.get("style") for c in b))
        xfs_el = root.find("m:cellXfs", ns)
        if xfs_el is not None:
            for xf in xfs_el:
                al = xf.find("m:alignment", ns)
                self.xfs.append({
                    "numFmt": C.to_int(xf.get("numFmtId")), "font": C.to_int(xf.get("fontId")),
                    "fill": C.to_int(xf.get("fillId")), "border": C.to_int(xf.get("borderId")),
                    "align": (al.get("horizontal") if al is not None else "") or "",
                    "wrap": al is not None and al.get("wrapText") in ("1", "true"),
                })

    def color(self, el) -> str | None:
        rgb = el.get("rgb")
        if rgb:
            return C.hex_color(rgb)
        th = el.get("theme")
        if th is not None and self.theme:
            i = C.to_int(th)
            if 0 <= i < len(self.theme):
                base = self.theme[i]
                tint = C.to_float(el.get("tint"))
                return _tint(base, tint) if tint else base
        ix = el.get("indexed")
        if ix is not None:
            i = C.to_int(ix)
            return INDEXED[i] if i < len(INDEXED) else None
        return None


def _theme_colors(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if n.startswith("xl/theme/") and n.endswith(".xml")]
    if not names:
        return []
    try:
        root = C.parse_xml(C.read_zip_part(zf, names[0]))
    except Exception:
        return []
    cols = {}
    for scheme in C.find_all(root, "clrScheme"):
        for c in scheme:
            k = C.local(c.tag)
            for v in c:
                cols[k] = "#" + (v.get("val") if C.local(v.tag) == "srgbClr" else v.get("lastClr", "000000")).lower()
    return [cols.get(k, "#000000") for k in THEME_ORDER]


def _shared(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out = []
    root = C.parse_xml(C.read_zip_part(zf, "xl/sharedStrings.xml"))
    for si in C.children(root, "si"):
        out.append("".join(t.text or "" for t in C.find_all(si, "t") if not _in_phonetic(si, t)))
    return out


def _in_phonetic(si, t) -> bool:
    for rph in C.children(si, "rPh"):
        if t in list(rph.iter()):
            return True
    return False


def read(path: str) -> Workbook:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("no workbook")
        wb = C.parse_xml(C.read_zip_part(zf, "xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in C.parse_xml(C.read_zip_part(zf, "xl/_rels/workbook.xml.rels")):
                tgt = r.get("Target", "")
                rels[r.get("Id")] = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join("xl", tgt))
        styles = _Styles(C.parse_xml(C.read_zip_part(zf, "xl/styles.xml")) if "xl/styles.xml" in names else None,
                         _theme_colors(zf))
        strings = _shared(zf)
        book = Workbook()
        budget = MAX_CELLS
        for sh in C.find_all(wb, "sheet"):
            rid = next((v for k, v in sh.attrib.items() if k.endswith("}id") or k == "id"), None)
            part = rels.get(rid)
            if not part or part not in names:
                continue
            sheet = Sheet(name=sh.get("name", "Sheet"))
            budget = _read_sheet(zf, part, sheet, styles, strings, budget)
            book.sheets.append(sheet)
        if not book.sheets:
            raise ValueError("no sheets")
        return book


def _read_sheet(zf, part, sheet: Sheet, st: _Styles, strings: list[str], budget: int) -> int:
    data = C.read_zip_part(zf, part)
    import io

    for ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        tag = C.local(el.tag)
        if tag == "sheetFormatPr":
            w = C.to_float(el.get("defaultColWidth"))
            if w:
                sheet.default_col = w * 7 + 5
            h = C.to_float(el.get("defaultRowHeight"))
            if h:
                sheet.default_row = h * 96 / 72
        elif tag == "col":
            lo, hi = C.to_int(el.get("min")) - 1, min(C.to_int(el.get("max")) - 1, MAX_COLS - 1)
            w = C.to_float(el.get("width"), 8.43)
            hidden = el.get("hidden") in ("1", "true")
            for c in range(max(lo, 0), hi + 1):
                sheet.col_widths[c] = 0.0 if hidden else w * 7 + 5
        elif tag == "mergeCell":
            m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", el.get("ref", ""))
            if m:
                sheet.merges.append((int(m.group(2)) - 1, col_index(m.group(1)), int(m.group(4)) - 1, col_index(m.group(3))))
        elif tag == "row":
            r = C.to_int(el.get("r"), sheet.nrows + 1) - 1
            if r >= MAX_ROWS:
                sheet.truncated = True
                el.clear()
                continue
            if el.get("ht") and el.get("customHeight") in ("1", "true", None):
                sheet.row_heights[r] = C.to_float(el.get("ht")) * 96 / 72
            if el.get("hidden") in ("1", "true"):
                sheet.row_heights[r] = 0.0
            for c in C.children(el, "c"):
                col = col_index(re.match(r"[A-Za-z]+", c.get("r", "A")).group(0)) if c.get("r") else 0
                if col >= MAX_COLS:
                    sheet.truncated = True
                    continue
                xf = st.xfs[C.to_int(c.get("s"))] if C.to_int(c.get("s")) < len(st.xfs) else {}
                t = c.get("t")
                v = C.child(c, "v")
                text = ""
                numeric = False
                if t == "s" and v is not None:
                    i = C.to_int(v.text)
                    text = strings[i] if i < len(strings) else ""
                elif t == "inlineStr":
                    text = "".join(x.text or "" for x in C.find_all(c, "t"))
                elif t == "b" and v is not None:
                    text = "TRUE" if (v.text or "0") == "1" else "FALSE"
                elif t in ("str", "e") and v is not None:
                    text = v.text or ""
                elif v is not None and v.text:
                    try:
                        num = float(v.text)
                        numeric = True
                        code = st.numfmts.get(xf.get("numFmt", 0), BUILTIN.get(xf.get("numFmt", 0), "General"))
                        text = format_number(num, code)
                    except ValueError:
                        text = v.text
                fi, fl, bo = xf.get("font", 0), xf.get("fill", 0), xf.get("border", 0)
                font = st.fonts[fi] if fi < len(st.fonts) else {}
                bg = st.fills[fl] if fl < len(st.fills) else None
                border = st.borders[bo] if bo < len(st.borders) else False
                if not text and not bg and not border:
                    continue
                if budget <= 0:
                    sheet.truncated = True
                    break
                budget -= 1
                align = xf.get("align", "")
                if align in ("general", ""):
                    align = "right" if numeric or t == "b" else ""
                sheet.cells[(r, col)] = SCell(text=text, bold=font.get("bold", False), italic=font.get("italic", False),
                                              color=font.get("color"), bg=bg, align=align, wrap=xf.get("wrap", False),
                                              border=border, size=font.get("size"))
                sheet.nrows = max(sheet.nrows, r + 1)
                sheet.ncols = max(sheet.ncols, col + 1)
            el.clear()
    for r1, c1, r2, c2 in sheet.merges:
        sheet.nrows = max(sheet.nrows, min(r2 + 1, MAX_ROWS))
        sheet.ncols = max(sheet.ncols, min(c2 + 1, MAX_COLS))
    return budget
