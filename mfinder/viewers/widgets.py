"""Tk viewers: rich documents, spreadsheets, PDF pages and plain text.

Every view exposes the same small interface used by the preview pane:

    frame                    the tk widget to pack
    show(model)              display a loaded model
    highlight(rx) -> int     mark matches of a compiled regex (None clears), returns the count
    step(d) -> (cur, total)  jump to the next / previous match
    zoom_by(delta)           Ctrl+wheel zoom
    clear()
"""

from __future__ import annotations

import base64
import bisect
import collections
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

from . import common as C
from . import model as M

try:  # optional: PDF pages and picture conversion/scaling
    import pymupdf  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    pymupdf = None

PX = 96 / 72
PAPER, INK = "#ffffff", "#000000"
HIT_BG, CUR_BG = "#ffe066", "#ff9632"
GRID = "#8a8a8a"
DEFAULT_FAMILY = "Malgun Gothic"
MONO_FAMILY = "Consolas"
MAX_HITS = 20000
MAX_CELL_WIDGETS = 1800


def have_pymupdf() -> bool:
    return pymupdf is not None


# ---- images ---------------------------------------------------------------------------


def make_photo(img: M.Image, max_w: int, zoom: float):
    """-> (PhotoImage | None, width px, height px)."""
    data = img.data
    tw = th = None
    if img.width and img.height:
        tw, th = img.width * PX * zoom, img.height * PX * zoom
    png = None
    pw = ph = 0
    if pymupdf is not None:
        try:
            pix = pymupdf.Pixmap(data)
            if pix.colorspace is not None and pix.colorspace.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            ow, oh = pix.width, pix.height
            if tw is None:
                tw, th = ow * zoom, oh * zoom
            if tw > max_w:
                th, tw = th * max_w / tw, float(max_w)
            tw, th = max(1, int(tw)), max(1, int(th))
            if (tw, th) != (ow, oh):
                pix = pymupdf.Pixmap(pix, tw, th)
            png = pix.tobytes("png")
            pw, ph = pix.width, pix.height
        except Exception:
            png = None
    if png is None:
        if C.image_kind(data) not in ("png", "gif"):
            return None, 0, 0
        png = data
    try:
        photo = tk.PhotoImage(data=base64.b64encode(png))
    except tk.TclError:
        return None, 0, 0
    return photo, pw or photo.width(), ph or photo.height()


# ---- text helpers ---------------------------------------------------------------------


def mark_hits(w: tk.Text, rx) -> list[tuple[str, str]]:
    """Tag every match of ``rx`` in ``w`` with 'hit'; returns [(start, end)] Tk indices."""
    content = w.get("1.0", "end-1c")
    starts = [0]
    idx = 0
    while True:
        i = content.find("\n", idx)
        if i < 0:
            break
        starts.append(i + 1)
        idx = i + 1
    out = []
    for m in rx.finditer(content):
        a, b = m.span()
        if a == b:
            continue
        la = bisect.bisect_right(starts, a) - 1
        lb = bisect.bisect_right(starts, b) - 1
        i1, i2 = f"{la + 1}.{a - starts[la]}", f"{lb + 1}.{b - starts[lb]}"
        w.tag_add("hit", i1, i2)
        out.append((i1, i2))
        if len(out) >= MAX_HITS:
            break
    return out


class _View:
    """Shared plumbing: frame, zoom, hit list."""

    def __init__(self, master, pane) -> None:
        self.pane = pane
        self.frame = tk.Frame(master)
        self.zoom = 1.0
        self.hits: list = []
        self.cur = -1

    def zoom_by(self, delta: int) -> None:
        z = self.zoom * (1.15 if delta > 0 else 1 / 1.15)
        self.zoom = max(0.4, min(4.0, z))
        self.apply_zoom()

    def apply_zoom(self) -> None:
        pass

    def clear(self) -> None:
        self.hits, self.cur = [], -1

    def step(self, d: int):
        if not self.hits:
            return 0, 0
        self.cur = (self.cur + d) % len(self.hits)
        self.goto(self.cur)
        return self.cur + 1, len(self.hits)

    def goto(self, i: int) -> None:
        pass

    def _wheel(self, e, widget) -> str:
        if e.state & 0x4:  # Ctrl
            self.zoom_by(1 if e.delta > 0 else -1)
        else:
            widget.yview_scroll(-int(e.delta / 120) * 3, "units")
        return "break"


# ---- plain text ---------------------------------------------------------------------------


class TextView(_View):
    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        self.size = pane.font_size
        self.text = tk.Text(self.frame, wrap="word", state="disabled", relief="flat", borderwidth=0,
                            font=(MONO_FAMILY, self.size), padx=8, pady=4, undo=False)
        sb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("hit", background=HIT_BG, foreground="#000000")
        self.text.tag_configure("cur", background=CUR_BG, foreground="#000000")
        self.text.tag_configure("note", foreground="#888888")
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.text.bind("<MouseWheel>", lambda e: self._wheel(e, self.text))
        self.text.bind("<Control-a>", lambda e: (self.text.tag_add("sel", "1.0", "end"), "break")[1])

    def apply_theme(self, colors: dict) -> None:
        self.text.configure(bg=colors["bg"], fg=colors["fg"], insertbackground=colors["fg"],
                            selectbackground=colors["sel"], selectforeground=colors["fg"],
                            inactiveselectbackground=colors["sel_inactive"])
        self.text.tag_configure("note", foreground=colors["dim"])

    def set_font_size(self, size: int) -> None:
        self.size = size
        self.apply_zoom()

    def apply_zoom(self) -> None:
        self.text.configure(font=(MONO_FAMILY, max(6, round(self.size * self.zoom))))

    def show(self, model) -> None:
        text, truncated = model
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", text)
        if truncated:
            t.insert("end", "\n\n... (truncated)", "note")
        t.configure(state="disabled")
        t.yview_moveto(0)
        self.hits, self.cur = [], -1

    def clear(self) -> None:
        super().clear()
        self.show(("", False))

    def highlight(self, rx) -> int:
        t = self.text
        t.tag_remove("hit", "1.0", "end")
        t.tag_remove("cur", "1.0", "end")
        self.hits, self.cur = (mark_hits(t, rx) if rx is not None else []), -1
        return len(self.hits)

    def goto(self, i: int) -> None:
        t = self.text
        t.tag_remove("cur", "1.0", "end")
        i1, i2 = self.hits[i]
        t.tag_add("cur", i1, i2)
        t.see(i1)


# ---- rich documents -----------------------------------------------------------------------


class _Painter:
    """Draws ``model`` blocks into Tk Text widgets (the main one and table cells)."""

    def __init__(self, owner: "RichView") -> None:
        self.o = owner
        self.fonts: dict = {}
        self.lines: dict = {}
        self.glyphs: dict = {}
        self.tags: dict = {}
        self.photos: dict = {}
        try:
            fams = {f.lower(): f for f in tkfont.families()}
        except tk.TclError:
            fams = {}
        self.families = fams
        self.default_family = fams.get(DEFAULT_FAMILY.lower()) or fams.get("segoe ui") or "TkDefaultFont"

    # -- fonts / tags --
    def px(self, pt: float) -> int:
        return max(5, round(pt * PX * self.o.zoom))

    def family(self, run: M.Run) -> str:
        if run.mono:
            return self.families.get(MONO_FAMILY.lower(), "Courier New")
        if run.font:
            f = self.families.get(run.font.lower())
            if f:
                return f
        if self.o.doc_font:
            f = self.families.get(self.o.doc_font.lower())
            if f:
                return f
        return self.default_family

    def font_key(self, run: M.Run) -> tuple:
        size = (run.size or self.o.base) * (0.72 if run.sup else 1.0)
        key = (self.family(run), self.px(size), run.bold, run.italic)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family=key[0], size=-key[1], weight="bold" if run.bold else "normal",
                                          slant="italic" if run.italic else "roman")
        return key

    def font_for(self, run: M.Run) -> tkfont.Font:
        return self.fonts[self.font_key(run)]

    def linespace(self, key: tuple) -> int:
        v = self.lines.get(key)
        if v is None:
            v = self.lines[key] = self.fonts[key].metrics("linespace")
        return v

    def width(self, key: tuple, text: str) -> int:
        """Pixel width of ``text``; Tk's font.measure is slow, so glyph widths are cached
        (every Hangul syllable / CJK ideograph shares one width)."""
        cache = self.glyphs.setdefault(key, {})
        f = self.fonts[key]
        total = 0
        for ch in text:
            w = cache.get(ch)
            if w is None:
                o = ord(ch)
                rep = "가" if 0xAC00 <= o <= 0xD7A3 else "一" if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF) else ch
                w = cache.get(rep)
                if w is None:
                    w = cache[rep] = f.measure(rep)
                cache[ch] = w
            total += w
        return total

    def run_tag(self, w: tk.Text, run: M.Run) -> str:
        key = (str(w), self.family(run), self.px((run.size or self.o.base) * (0.72 if run.sup else 1.0)), run.bold,
               run.italic, run.underline, run.strike, run.color, run.bg, run.sup)
        name = self.tags.get(key)
        if name is None:
            name = self.tags[key] = f"r{len(self.tags)}"
            f = self.font_for(run)
            w.tag_configure(name, font=f, foreground=run.color or INK, background=run.bg or "",
                            underline=run.underline, overstrike=run.strike,
                            offset=int(f.metrics("linespace") * 0.35) * run.sup if run.sup else 0)
        return name

    def para_metrics(self, p: M.Para) -> tuple:
        z = self.o.zoom
        lm2 = max(0, round(p.left * PX * z))
        lm1 = max(0, round((p.left + p.first) * PX * z))
        sp1, sp3 = round(p.before * PX * z), round(p.after * PX * z)
        sp2 = 0
        if p.line and p.line > 1:
            sp2 = round((p.line - 1) * self.px(self.o.base) * 1.25)
        return lm1, lm2, sp1, sp3, sp2

    def para_tag(self, w: tk.Text, p: M.Para) -> str:
        lm1, lm2, sp1, sp3, sp2 = self.para_metrics(p)
        bg = p.bg or ("#f6f8fa" if p.bar else "")
        key = (str(w), p.align, lm1, lm2, sp1, sp3, sp2, bg)
        name = self.tags.get(key)
        if name is None:
            name = self.tags[key] = f"p{len(self.tags)}"
            w.tag_configure(name, justify={"center": "center", "right": "right"}.get(p.align, "left"),
                            lmargin1=lm1, lmargin2=lm2, spacing1=sp1, spacing2=sp2, spacing3=sp3, background=bg)
            w.tag_lower(name)
        return name

    # -- images --
    def photo(self, img: M.Image, max_w: int):
        key = (id(img), max_w, self.o.zoom)
        hit = self.photos.get(key)
        if hit is None:
            hit = self.photos[key] = make_photo(img, max_w, self.o.zoom)
            if hit[0] is not None:
                self.o.refs.append(hit[0])
        return hit

    # -- measuring (cell heights are computed up front; embedded widgets are not mapped yet) --
    def measure_para(self, p: M.Para, width: int) -> int:
        lm1, lm2, sp1, sp3, sp2 = self.para_metrics(p)
        avail = max(20, width - lm2 - 8)
        base_h = self.linespace(self.font_key(M.Run(size=self.o.base)))
        st = {"lines": 1, "x": max(0, lm1 - lm2), "h": base_h, "total": 0}

        def newline():
            st["total"] += st["h"]
            st["lines"] += 1
            st["x"], st["h"] = 0, base_h

        for r in p.runs:
            if isinstance(r, M.Image):
                ph, pw, ih = self.photo(r, avail)
                if ph is None:
                    continue
                if st["x"] + pw > avail and st["x"] > 0:
                    newline()
                st["x"] += pw
                st["h"] = max(st["h"], ih + 4)
                continue
            key = self.font_key(r)
            lh = self.linespace(key)
            st["h"] = max(st["h"], lh)
            for tok in re.findall(r"\n|\s+|\S+", r.text):
                if tok == "\n":
                    newline()
                    continue
                if "\t" in tok:
                    tok = tok.replace("\t", "        ")
                wd = self.width(key, tok)
                if tok.isspace():
                    st["x"] += wd
                    continue
                if st["x"] + wd <= avail:
                    st["x"] += wd
                    continue
                if st["x"] > 0:
                    newline()
                    st["h"] = max(st["h"], lh)
                if wd <= avail:
                    st["x"] = wd
                else:  # a very long word breaks anywhere
                    for ch in tok:
                        cw = self.width(key, ch)
                        if st["x"] + cw > avail:
                            newline()
                            st["h"] = max(st["h"], lh)
                        st["x"] += cw
        total = st["total"] + st["h"]
        return total + sp1 + sp3 + sp2 * (st["lines"] - 1) + 2

    # -- drawing --
    def fill(self, w: tk.Text, blocks: list, avail: int) -> None:
        for _ in self.iter_fill(w, blocks, avail):
            pass

    def iter_fill(self, w: tk.Text, blocks: list, avail: int):
        """Draw block by block, yielding in between so the UI can stay responsive."""
        for b in blocks:
            if isinstance(b, M.Table):
                self.table(w, b, avail)
            else:
                self.para(w, b, avail)
            yield

    def para(self, w: tk.Text, p: M.Para, avail: int) -> None:
        if p.rule:
            self.rule(w, avail)
            return
        ptag = self.para_tag(w, p)
        start = w.index("end-1c")
        if not p.runs:
            w.insert("end", " ", (self.run_tag(w, M.Run(text=" ")),))
        for r in p.runs:
            if isinstance(r, M.Image):
                photo, pw, ph = self.photo(r, max(40, avail - 20))
                if photo is None:
                    w.insert("end", f"[{r.alt or 'image'}]", (self.run_tag(w, M.Run(color="#888888", italic=True)),))
                else:
                    w.image_create("end", image=photo, padx=2)
            elif r.text:
                w.insert("end", r.text, (self.run_tag(w, r),))
        w.insert("end", "\n")
        w.tag_add(ptag, start, "end-1c")

    def rule(self, w: tk.Text, avail: int) -> None:
        fr = tk.Frame(w, height=1, width=max(40, avail - 12), bg="#c8c8c8")
        w.window_create("end", window=fr, pady=5)
        w.insert("end", "\n")

    @staticmethod
    def _flatten(blocks: list) -> list:
        out = []
        for b in blocks:
            if isinstance(b, M.Table):
                for row in b.rows:
                    txt = " | ".join(" ".join(x.text() for x in c.blocks if isinstance(x, M.Para)) for c in row)
                    out.append(M.Para(runs=[M.Run(text=txt)]))
            else:
                out.append(b)
        return out

    @staticmethod
    def _layout(table: M.Table):
        occ, out = set(), []
        nrows = len(table.rows)
        for r, row in enumerate(table.rows):
            c = 0
            for cell in row:
                while (r, c) in occ:
                    c += 1
                rs = max(1, min(cell.rowspan, nrows - r))
                cs = max(1, cell.colspan)
                out.append((cell, r, c, rs, cs))
                for dr in range(rs):
                    for dc in range(cs):
                        occ.add((r + dr, c + dc))
                c += cs
        return out

    def table(self, w: tk.Text, table: M.Table, avail: int) -> None:
        cells = self._layout(table)
        if not cells:
            return
        if self.o.cell_budget < len(cells):  # too many widgets: degrade to text lines
            self.fill(w, self._flatten([table]), avail)
            return
        self.o.cell_budget -= len(cells)
        z = self.o.zoom
        ncols = max(c + cs for _, _, c, _, cs in cells)
        nrows = max(r + rs for _, r, _, rs, _ in cells)
        colw: list = [None] * ncols
        for i, wpt in enumerate(table.col_widths[:ncols]):
            colw[i] = wpt * PX * z if wpt else None
        for cell, r, c, rs, cs in cells:
            if cs == 1 and colw[c] is None and cell.width:
                colw[c] = cell.width * PX * z
        known = [x for x in colw if x]
        avg = (sum(known) / len(known)) if known else min(avail / max(ncols, 1), 260)
        colw = [max(26.0, x or avg) for x in colw]
        gap = 1 if table.border else 0
        usable = max(120, avail - 24 - gap * (ncols + 1))
        total = sum(colw)
        if total > usable:
            colw = [max(20.0, x * usable / total) for x in colw]
        colw = [int(x) for x in colw]

        tf = tk.Frame(w, bg=GRID if table.border else PAPER, highlightthickness=gap,
                      highlightbackground=GRID if table.border else PAPER)
        mark = f"tbl{len(self.o.table_marks)}"
        w.mark_set(mark, "end-1c")
        w.mark_gravity(mark, "left")
        self.o.table_marks.append(mark)
        heights = [18] * nrows
        placed = []
        for cell, r, c, rs, cs in cells:
            cw = sum(colw[c : c + cs]) + gap * (cs - 1)
            blocks = self._flatten(cell.blocks) or [M.Para()]
            need = sum(self.measure_para(p, cw - 6) for p in blocks if isinstance(p, M.Para)) + 6
            placed.append((cell, r, c, rs, cs, cw, blocks, need))
            if rs == 1:
                heights[r] = max(heights[r], need)
        for cell, r, c, rs, cs, cw, blocks, need in placed:
            if rs > 1:
                have = sum(heights[r : r + rs])
                if need > have:
                    heights[r + rs - 1] += need - have
        for cell, r, c, rs, cs, cw, blocks, need in placed:
            bg = cell.bg or PAPER
            avail_h = sum(heights[r : r + rs]) + gap * (rs - 1)
            # one Text per cell (no wrapper frame: widget count dominates the rendering time);
            # vertical alignment is done with the Text's internal padding
            pady = 2 if cell.valign == "top" else max(2, (avail_h - need) // (2 if cell.valign == "center" else 1) + 2)
            t = tk.Text(tf, wrap="word", bd=0, highlightthickness=0, padx=3, pady=pady, bg=bg, fg=INK, cursor="arrow",
                        takefocus=0, width=1, height=1, undo=False, insertwidth=0,
                        font=self.font_for(M.Run(size=self.o.base)))
            t.grid(row=r, column=c, rowspan=rs, columnspan=cs, sticky="nsew",
                   padx=(0, gap if c + cs < ncols else 0), pady=(0, gap if r + rs < nrows else 0))
            t.tag_configure("hit", background=HIT_BG, foreground="#000000")
            t.tag_configure("cur", background=CUR_BG, foreground="#000000")
            self.fill(t, blocks, cw - 6)
            t.configure(state="disabled")
            t.bind("<MouseWheel>", lambda e: self.o._wheel(e, self.o.text))
            t.bind("<Button-1>", lambda e, t=t: t.focus_set())
            self.o.cell_texts.append((t, mark, tf))
        for i, hgt in enumerate(heights):
            tf.grid_rowconfigure(i, minsize=hgt)
        for i, cwid in enumerate(colw):
            tf.grid_columnconfigure(i, minsize=cwid + (gap if i < ncols - 1 else 0))
        w.window_create("end", window=tf, padx=2, pady=2)
        w.insert("end", "\n")


class RichView(_View):
    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        self.doc: M.Doc | None = None
        self.base = 10.0
        self.doc_font = None
        self.refs: list = []
        self.cell_texts: list = []
        self.table_marks: list = []
        self.cell_budget = MAX_CELL_WIDGETS
        self._last_w = 0
        self._resize_job = None
        self.rendering = False
        self._iter = None
        self._step_job = None
        self._frac = 0.0
        self.text = tk.Text(self.frame, wrap="word", state="disabled", relief="flat", borderwidth=0, bg=PAPER, fg=INK,
                            padx=18, pady=10, undo=False, cursor="arrow", insertwidth=0,
                            selectbackground="#b3d7ff", selectforeground=INK, inactiveselectbackground="#d9e8fb")
        sb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("hit", background=HIT_BG, foreground="#000000")
        self.text.tag_configure("cur", background=CUR_BG, foreground="#000000")
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.text.bind("<MouseWheel>", lambda e: self._wheel(e, self.text))
        self.text.bind("<Control-a>", lambda e: (self.text.tag_add("sel", "1.0", "end"), "break")[1])
        self.text.bind("<Configure>", self._configure)
        self.painter: _Painter | None = None

    def _configure(self, e) -> None:
        if self.doc is None or abs(e.width - self._last_w) < 50:
            return
        if self._resize_job is not None:
            self.frame.after_cancel(self._resize_job)
        self._resize_job = self.frame.after(250, self._rerender)

    def _rerender(self) -> None:
        self._resize_job = None
        if self.doc is not None and abs(self.text.winfo_width() - self._last_w) >= 50:
            self.render(keep_view=True)

    def apply_zoom(self) -> None:
        if self.doc is not None:
            self.render(keep_view=True)

    def show(self, model: M.Doc) -> None:
        self.doc = model
        self.base = model.base_size or 10.0
        self.doc_font = model.font
        self.render()

    def clear(self) -> None:
        super().clear()
        if self._step_job is not None:
            self.frame.after_cancel(self._step_job)
            self._step_job = None
        self._iter, self.rendering = None, False
        self.doc = None
        self._reset()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _reset(self) -> None:
        for t, _m, cf in self.cell_texts:
            try:
                cf.destroy()
            except tk.TclError:
                pass
        self.cell_texts, self.table_marks, self.refs = [], [], []
        self.cell_budget = MAX_CELL_WIDGETS
        self.hits, self.cur = [], -1
        self.painter = None

    def render(self, keep_view: bool = False) -> None:
        """Start (or restart) drawing; the work is sliced into ~30 ms steps."""
        t = self.text
        if self._step_job is not None:
            self.frame.after_cancel(self._step_job)
            self._step_job = None
        self._frac = t.yview()[0] if keep_view else 0.0
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._reset()
        self.painter = _Painter(self)
        width = max(300, t.winfo_width() if t.winfo_width() > 50 else 640)
        self._last_w = t.winfo_width()
        if self.doc.note:
            t.insert("end", f"({self.doc.note})\n", ("note",))
            t.tag_configure("note", foreground="#888888")
        t.configure(state="disabled")
        self._iter = self.painter.iter_fill(t, self.doc.blocks, width - 40)
        self.rendering = True
        self._step()

    def _step(self) -> None:
        self._step_job = None
        if self._iter is None:
            return
        t = self.text
        t.configure(state="normal")
        deadline = time.perf_counter() + 0.03
        done = False
        try:
            while time.perf_counter() < deadline:
                next(self._iter)
        except StopIteration:
            done = True
        except tk.TclError:  # widget destroyed while drawing
            self._iter, self.rendering = None, False
            return
        t.configure(state="disabled")
        if not done:
            if self._frac and t.yview()[1] - t.yview()[0] < 1.0:
                t.yview_moveto(self._frac)
            self._step_job = self.frame.after(1, self._step)
            return
        self._iter, self.rendering = None, False
        t.yview_moveto(self._frac)
        self.pane.render_done(self)

    # -- highlighting --
    def highlight(self, rx) -> int:
        if self.rendering:  # the pane re-applies the highlight when drawing is finished
            return 0
        allw = [self.text] + [c[0] for c in self.cell_texts]
        for w in allw:
            w.tag_remove("hit", "1.0", "end")
            w.tag_remove("cur", "1.0", "end")
        self.hits, self.cur = [], -1
        if rx is None or self.doc is None:
            return 0
        items = []
        for seq, w in enumerate(allw):
            for i1, i2 in mark_hits(w, rx):
                line, col = (int(x) for x in i1.split("."))
                if w is self.text:
                    key = (line, col, 0, 0, 0, 0)
                else:
                    mark = self.cell_texts[seq - 1][1]
                    ml, mc = (int(x) for x in self.text.index(mark).split("."))
                    key = (ml, mc, 1, seq, line, col)
                items.append((key, w, i1, i2))
            if len(items) >= MAX_HITS:
                break
        items.sort(key=lambda x: x[0])
        self.hits = [(w, i1, i2, key) for key, w, i1, i2 in items]
        return len(self.hits)

    def goto(self, i: int) -> None:
        for w, *_ in self.hits:
            w.tag_remove("cur", "1.0", "end")
        w, i1, i2, key = self.hits[i]
        w.tag_add("cur", i1, i2)
        if w is self.text:
            self.text.see(i1)
            return
        mark = self.cell_texts[key[3] - 1][1]
        self.text.see(mark)
        self.text.update_idletasks()
        bb = w.bbox(i1)
        if bb:
            y = w.winfo_rooty() - self.text.winfo_rooty() + bb[1]
            h = self.text.winfo_height()
            if y < 20 or y > h - 40:
                self.text.yview_scroll(int(y - h / 3), "pixels")


# ---- spreadsheets ---------------------------------------------------------------------------


class SheetView(_View):
    HDR_W, HDR_H = 46, 20

    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        f = self.frame
        self.book: M.Workbook | None = None
        self.sheet: M.Sheet | None = None
        self.si = 0
        self.fonts: dict = {}
        self._job = None
        self.corner = tk.Canvas(f, width=self.HDR_W, height=self.HDR_H, bg="#f0f0f0", highlightthickness=0)
        self.colh = tk.Canvas(f, height=self.HDR_H, bg="#f0f0f0", highlightthickness=0)
        self.rowh = tk.Canvas(f, width=self.HDR_W, bg="#f0f0f0", highlightthickness=0)
        self.body = tk.Canvas(f, bg="#ffffff", highlightthickness=0, takefocus=True)
        self.vsb = ttk.Scrollbar(f, orient="vertical", command=self._yview)
        self.hsb = ttk.Scrollbar(f, orient="horizontal", command=self._xview)
        self.tabs = tk.Frame(f, bg="#f0f0f0")
        self.corner.grid(row=0, column=0)
        self.colh.grid(row=0, column=1, sticky="ew")
        self.rowh.grid(row=1, column=0, sticky="ns")
        self.body.grid(row=1, column=1, sticky="nsew")
        self.vsb.grid(row=1, column=2, sticky="ns")
        self.hsb.grid(row=2, column=1, sticky="ew")
        self.tabs.grid(row=3, column=0, columnspan=3, sticky="ew")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(1, weight=1)
        self.body.configure(yscrollcommand=self._ysb, xscrollcommand=self._xsb)
        for c in (self.body, self.colh, self.rowh, self.corner):
            c.bind("<MouseWheel>", self._mw)
            c.bind("<Shift-MouseWheel>", lambda e: (self.body.xview_scroll(-int(e.delta / 120) * 3, "units"), "break")[1])
        self.body.bind("<Configure>", lambda e: self._schedule())
        self.body.bind("<Button-1>", lambda e: self.body.focus_set())
        self.body.bind("<Control-c>", lambda e: "break")

    # -- scrolling --
    def _mw(self, e):
        if e.state & 0x4:
            self.zoom_by(1 if e.delta > 0 else -1)
        else:
            self.body.yview_scroll(-int(e.delta / 120) * 3, "units")
        return "break"

    def _yview(self, *a) -> None:
        self.body.yview(*a)

    def _xview(self, *a) -> None:
        self.body.xview(*a)

    def _ysb(self, a, b) -> None:
        self.vsb.set(a, b)
        self._schedule()

    def _xsb(self, a, b) -> None:
        self.hsb.set(a, b)
        self._schedule()

    def _schedule(self) -> None:
        if self._job is None:
            self._job = self.frame.after_idle(self._redraw)

    # -- model --
    def show(self, model: M.Workbook) -> None:
        self.book = model
        self.hits, self.cur = [], -1
        self._build_tabs()
        self.select(0)

    def clear(self) -> None:
        super().clear()
        self.book = self.sheet = None
        self.body.delete("all")
        self.colh.delete("all")
        self.rowh.delete("all")
        for w in self.tabs.winfo_children():
            w.destroy()

    def _build_tabs(self) -> None:
        for w in self.tabs.winfo_children():
            w.destroy()
        self.tab_btns = []
        for i, sh in enumerate(self.book.sheets):
            b = tk.Label(self.tabs, text=sh.name, padx=10, pady=2, bg="#f0f0f0", fg="#222222", cursor="hand2",
                         relief="flat", borderwidth=1)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, i=i: self.select(i))
            self.tab_btns.append(b)

    def select(self, i: int) -> None:
        if not self.book or not 0 <= i < len(self.book.sheets):
            return
        self.si = i
        self.sheet = sh = self.book.sheets[i]
        for k, b in enumerate(self.tab_btns):
            b.configure(bg="#ffffff" if k == i else "#f0f0f0", fg="#107c41" if k == i else "#222222",
                        font=(DEFAULT_FAMILY, 9, "bold" if k == i else "normal"))
        self._layout()
        self.body.xview_moveto(0)
        self.body.yview_moveto(0)
        self._schedule()

    def _font(self, bold: bool, italic: bool, size: float | None) -> tkfont.Font:
        px = max(7, round((size or 11) * PX * self.zoom))
        key = (bold, italic, px)
        f = self.fonts.get(key)
        if f is None:
            f = self.fonts[key] = tkfont.Font(family=DEFAULT_FAMILY, size=-px, weight="bold" if bold else "normal",
                                              slant="italic" if italic else "roman")
        return f

    def _layout(self) -> None:
        sh, z = self.sheet, self.zoom
        ncols = max(sh.ncols + 1, 12)
        nrows = max(sh.nrows + 1, 40)
        self.xs = [0.0]
        for c in range(ncols):
            self.xs.append(self.xs[-1] + sh.col_widths.get(c, sh.default_col) * z)
        self.ys = [0.0]
        for r in range(nrows):
            self.ys.append(self.ys[-1] + sh.row_heights.get(r, sh.default_row) * z)
        self.anchor, self.covered = {}, set()
        budget = 200_000
        for r1, c1, r2, c2 in sh.merges:
            self.anchor[(r1, c1)] = (min(r2, nrows - 1), min(c2, ncols - 1))
            for r in range(r1, min(r2, nrows - 1) + 1):
                for c in range(c1, min(c2, ncols - 1) + 1):
                    if (r, c) != (r1, c1) and budget > 0:
                        self.covered.add((r, c))
                        budget -= 1
        self.body.configure(scrollregion=(0, 0, self.xs[-1], self.ys[-1]))

    def apply_zoom(self) -> None:
        if self.sheet is not None:
            fx, fy = self.body.xview()[0], self.body.yview()[0]
            self._layout()
            self.body.xview_moveto(fx)
            self.body.yview_moveto(fy)
            self._schedule()

    # -- drawing --
    def _redraw(self) -> None:
        self._job = None
        sh = self.sheet
        if sh is None:
            return
        b = self.body
        w, h = b.winfo_width(), b.winfo_height()
        if w < 5 or h < 5:
            return
        x0, y0 = int(b.canvasx(0)), int(b.canvasy(0))
        c0 = max(0, bisect.bisect_right(self.xs, x0) - 1)
        c1 = min(len(self.xs) - 2, bisect.bisect_right(self.xs, x0 + w))
        r0 = max(0, bisect.bisect_right(self.ys, y0) - 1)
        r1 = min(len(self.ys) - 2, bisect.bisect_right(self.ys, y0 + h))
        b.delete("all")
        self.colh.delete("all")
        self.rowh.delete("all")
        hf = self._font(False, False, 10)
        for c in range(c0, c1 + 1):
            xa, xb = self.xs[c] - x0, self.xs[c + 1] - x0
            if xb > xa:
                self.colh.create_rectangle(xa, 0, xb, self.HDR_H, fill="#f0f0f0", outline="#c8c8c8")
                self.colh.create_text((xa + xb) / 2, self.HDR_H / 2, text=_col_name(c), font=hf, fill="#444444")
        for r in range(r0, r1 + 1):
            ya, yb = self.ys[r] - y0, self.ys[r + 1] - y0
            if yb > ya:
                self.rowh.create_rectangle(0, ya, self.HDR_W, yb, fill="#f0f0f0", outline="#c8c8c8")
                self.rowh.create_text(self.HDR_W / 2, (ya + yb) / 2, text=str(r + 1), font=hf, fill="#444444")
        # grid lines
        for c in range(c0, c1 + 2):
            x = self.xs[min(c, len(self.xs) - 1)] - x0
            b.create_line(x, 0, x, h, fill="#e1e1e1")
        for r in range(r0, r1 + 2):
            y = self.ys[min(r, len(self.ys) - 1)] - y0
            b.create_line(0, y, w, y, fill="#e1e1e1")
        hit_set = {k: (i == self.cur) for i, k in enumerate(self.hits)}
        # merged areas that intersect the view are drawn from their anchor, even if it is off-screen
        todo = []
        for (ar, ac), (br, bc) in self.anchor.items():
            if br >= r0 and ar <= r1 and bc >= c0 and ac <= c1:
                todo.append((ar, ac, br, bc))
        done = set()
        for ar, ac, br, bc in todo:
            done.add((ar, ac))
            self._cell(b, ar, ac, br, bc, x0, y0, hit_set)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if (r, c) in done or (r, c) in self.covered:
                    continue
                if (r, c) in sh.cells:
                    self._cell(b, r, c, r, c, x0, y0, hit_set)
        # hits in cells that have no data entry cannot exist; hits are text cells

    def _cell(self, cv, r, c, br, bc, x0, y0, hit_set) -> None:
        sh = self.sheet
        cell = sh.cells.get((r, c))
        xa, ya = self.xs[c] - x0, self.ys[r] - y0
        xb, yb = self.xs[min(bc + 1, len(self.xs) - 1)] - x0, self.ys[min(br + 1, len(self.ys) - 1)] - y0
        if xb <= xa or yb <= ya:
            return
        hit = hit_set.get((r, c))
        bg = (CUR_BG if hit else HIT_BG) if hit is not None else (cell.bg if cell and cell.bg else None)
        if bg:
            cv.create_rectangle(xa, ya, xb, yb, fill=bg, outline="#e1e1e1")
        if cell and cell.border:
            cv.create_rectangle(xa, ya, xb, yb, outline="#555555")
        if not cell or not cell.text:
            return
        f = self._font(cell.bold, cell.italic, cell.size)
        color = cell.color or INK
        width = xb - xa - 6
        text = cell.text
        if cell.wrap:
            cv.create_text(xa + 3, ya + 2, text=text, anchor="nw", font=f, fill=color, width=max(10, width))
            return
        text = text.replace("\n", " ")
        align = cell.align or "left"
        # text may flow into empty neighbours (left aligned only)
        if align == "left":
            k = bc + 1
            while k < len(self.xs) - 1 and (r, k) not in sh.cells and k - c < 30:
                width += self.xs[k + 1] - self.xs[k]
                k += 1
        if f.measure(text) > width:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if f.measure(text[:mid]) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            text = text[:lo]
            if not text:
                return
        y = (ya + yb) / 2
        if align == "right":
            cv.create_text(xb - 3, y, text=text, anchor="e", font=f, fill=color)
        elif align == "center":
            cv.create_text((xa + xb) / 2, y, text=text, anchor="center", font=f, fill=color)
        else:
            cv.create_text(xa + 3, y, text=text, anchor="w", font=f, fill=color)

    # -- hits --
    def highlight(self, rx) -> int:
        self.hits, self.cur = [], -1
        if rx is not None and self.sheet is not None:
            hits = []
            for (r, c), cell in self.sheet.cells.items():
                if cell.text and rx.search(cell.text):
                    hits.append((r, c))
            hits.sort()
            self.hits = hits[:MAX_HITS]
        self._schedule()
        return len(self.hits)

    def goto(self, i: int) -> None:
        r, c = self.hits[i]
        b = self.body
        w, h = b.winfo_width(), b.winfo_height()
        tw, th = self.xs[-1], self.ys[-1]
        x, y = self.xs[c], self.ys[r]
        if tw > 0:
            b.xview_moveto(max(0.0, (x - w / 3) / tw))
        if th > 0:
            b.yview_moveto(max(0.0, (y - h / 3) / th))
        self._schedule()


def _col_name(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


# ---- pictures ----------------------------------------------------------------------------------


class ImageView(_View):
    """A single picture, fitted to the pane (never enlarged past 100 %) and zoomable with Ctrl+wheel."""

    MARGIN = 10
    MAX_SIDE = 12000
    MAX_PIXELS = 36_000_000

    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        f = self.frame
        self.canvas = tk.Canvas(f, bg="#4a4a4a", highlightthickness=0, takefocus=True)
        self.vsb = ttk.Scrollbar(f, orient="vertical", command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(f, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda e: self._on_configure())
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(e, self.canvas))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: (self.canvas.xview_scroll(-int(e.delta / 120) * 3, "units"), "break")[1])
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<Double-Button-1>", lambda e: self._reset_zoom())
        self.raster = None
        self.photo = None
        self.info = ""
        self._last_w = 0
        self._job = None

    def show(self, model) -> None:
        self.clear()
        self.raster = model
        self.zoom = 1.0
        self.info = f"{model.width}×{model.height}"
        self._schedule()

    def clear(self) -> None:
        super().clear()
        self.raster = self.photo = None
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def highlight(self, rx) -> int:
        return 0

    def _reset_zoom(self) -> None:
        self.zoom = 1.0
        self.apply_zoom()

    def _on_configure(self) -> None:
        if self.raster is not None and abs(self.canvas.winfo_width() - self._last_w) > 8:
            self._schedule()

    def _schedule(self) -> None:
        if self._job is None:
            self._job = self.frame.after_idle(self._draw)

    def apply_zoom(self) -> None:
        if self.raster is not None:
            self._schedule()

    def _fit_scale(self, cw: int, ch: int) -> float:
        r = self.raster
        avail_w, avail_h = max(cw - 2 * self.MARGIN, 50), max(ch - 2 * self.MARGIN, 50)
        return min(1.0, avail_w / r.width, avail_h / r.height)

    def _draw(self) -> None:
        self._job = None
        r = self.raster
        if r is None:
            return
        c = self.canvas
        cw, ch = max(c.winfo_width(), 100), max(c.winfo_height(), 100)
        self._last_w = cw
        c.delete("all")
        self.photo = None
        if r.native:  # Tk fallback: shown 1:1 (zoom out by integer subsampling only)
            try:
                ph = tk.PhotoImage(data=r.tk_data())
                k = max(1, int(round(1 / max(self.zoom, 0.05)))) if self.zoom < 1 else 1
                if k > 1:
                    ph = ph.subsample(k, k)
                elif self.zoom > 1:
                    z = max(1, int(round(self.zoom)))
                    ph = ph.zoom(z, z)
            except tk.TclError:
                return
            w, h = ph.width(), ph.height()
        else:
            s = self._fit_scale(cw, ch) * self.zoom
            w, h = max(1, round(r.width * s)), max(1, round(r.height * s))
            cap = min(self.MAX_SIDE / max(w, h), (self.MAX_PIXELS / (w * h)) ** 0.5, 1e9)
            if cap < 1:
                w, h = max(1, int(w * cap)), max(1, int(h * cap))
            try:
                ph = tk.PhotoImage(data=r.ppm(w, h), format="PPM")
            except (tk.TclError, MemoryError, ValueError):
                return
        self.photo = ph
        tw, th = max(cw, w + 2 * self.MARGIN), max(ch, h + 2 * self.MARGIN)
        x, y = (tw - w) / 2, (th - h) / 2
        c.create_rectangle(x + 3, y + 3, x + w + 3, y + h + 3, fill="#2e2e2e", outline="")
        c.create_image(x, y, anchor="nw", image=ph)
        c.configure(scrollregion=(0, 0, tw, th))
        c.xview_moveto(0)
        c.yview_moveto(0)


# ---- PDF ---------------------------------------------------------------------------------------


class PdfView(_View):
    GAP = 12
    MARGIN = 14
    MAX_PAGES = 600
    MAX_STREAM = 100 << 20

    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        f = self.frame
        self.canvas = tk.Canvas(f, bg="#6b6b6b", highlightthickness=0, takefocus=True)
        self.vsb = ttk.Scrollbar(f, orient="vertical", command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(f, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._ysb, xscrollcommand=self._xsb)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda e: self._on_configure())
        self.canvas.bind("<MouseWheel>", lambda e: self._wheel(e, self.canvas))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: (self.canvas.xview_scroll(-int(e.delta / 120) * 3, "units"), "break")[1])
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self.doc = None
        self.path = ""
        self.data = None
        self.sizes: list = []
        self.cache: collections.OrderedDict = collections.OrderedDict()
        self.tops: list = []
        self.scale = 1.0
        self.x_off = 0
        self._job = None
        self._cfg_job = None
        self._last_w = 0
        self.q: queue.Queue = queue.Queue()
        self.search_id = 0
        self.rects: list = []  # (page, x0, y0, x1, y1) in PDF points
        self.note = ""
        self.searching = False

    def _ysb(self, a, b) -> None:
        self.vsb.set(a, b)
        self._schedule()

    def _xsb(self, a, b) -> None:
        self.hsb.set(a, b)
        self._schedule()

    def _schedule(self) -> None:
        if self._job is None:
            self._job = self.frame.after_idle(self._redraw)

    def _on_configure(self) -> None:
        w = self.canvas.winfo_width()
        if self.doc is not None and abs(w - self._last_w) > 30:
            if self._cfg_job is not None:
                self.frame.after_cancel(self._cfg_job)
            self._cfg_job = self.frame.after(200, self._relayout_keep)
        else:
            self._schedule()

    def _relayout_keep(self) -> None:
        self._cfg_job = None
        if self.doc is None:
            return
        fy = self.canvas.yview()[0]
        self._layout()
        self.canvas.yview_moveto(fy)
        self._schedule()

    def show(self, path: str) -> None:
        self.clear()
        self.path = path
        # read the file into memory so no handle stays open (it could not be deleted / moved while previewed)
        self.data = None
        try:
            if os.path.getsize(path) <= self.MAX_STREAM:
                with open(path, "rb") as fh:
                    self.data = fh.read()
        except OSError:
            self.data = None
        self.doc = pymupdf.open(stream=self.data, filetype="pdf") if self.data is not None else pymupdf.open(path)
        if self.doc.needs_pass:
            self.doc.close()
            self.doc = None
            raise ValueError("encrypted")
        n = min(self.doc.page_count, self.MAX_PAGES)
        self.note = "" if self.doc.page_count <= n else f"{n} / {self.doc.page_count} pages"
        self.sizes = []
        for i in range(n):
            r = self.doc.load_page(i).rect
            self.sizes.append((max(r.width, 1.0), max(r.height, 1.0)))
        self._layout()
        self.canvas.yview_moveto(0)
        self.canvas.xview_moveto(0)
        self._schedule()

    def clear(self) -> None:
        super().clear()
        self.search_id += 1
        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass
        self.doc = None
        self.data = None
        self.sizes, self.tops, self.rects = [], [], []
        self.cache.clear()
        self.canvas.delete("all")

    def _layout(self) -> None:
        cw = max(self.canvas.winfo_width(), 200)
        self._last_w = cw
        maxw = max(w for w, _ in self.sizes)
        self.scale = (cw - 2 * self.MARGIN) / maxw * self.zoom
        self.cache.clear()
        y = self.MARGIN
        self.tops = []
        for w, h in self.sizes:
            self.tops.append(y)
            y += h * self.scale + self.GAP
        total_w = max(cw, maxw * self.scale + 2 * self.MARGIN)
        self.total_w = total_w
        self.canvas.configure(scrollregion=(0, 0, total_w, y))

    def apply_zoom(self) -> None:
        if self.doc is not None:
            fy = self.canvas.yview()[0]
            self._layout()
            self.canvas.yview_moveto(fy)
            self._schedule()

    def _photo(self, i: int):
        hit = self.cache.get(i)
        if hit is not None and abs(hit[0] - self.scale) < 1e-6:
            self.cache.move_to_end(i)
            return hit[1]
        page = self.doc.load_page(i)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.scale, self.scale), alpha=False)
        try:
            ph = tk.PhotoImage(data=pix.tobytes("ppm"), format="PPM")
        except tk.TclError:
            ph = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")))
        self.cache[i] = (self.scale, ph)
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        return ph

    def _redraw(self) -> None:
        self._job = None
        if self.doc is None or not self.tops:
            return
        c = self.canvas
        h = c.winfo_height()
        y0 = c.canvasy(0)
        first = max(0, bisect.bisect_right(self.tops, y0) - 1)
        c.delete("all")
        cw = max(c.winfo_width(), 1)
        x = max(self.MARGIN, (self.total_w - max(w for w, _ in self.sizes) * self.scale) / 2) if self.total_w <= cw else self.MARGIN
        i = first
        while i < len(self.tops) and self.tops[i] < y0 + h:
            w, hh = self.sizes[i]
            pw, ph_ = w * self.scale, hh * self.scale
            px = max(x, (self.total_w - pw) / 2) if self.total_w <= cw else x
            c.create_rectangle(px + 3, self.tops[i] + 3, px + pw + 3, self.tops[i] + ph_ + 3, fill="#505050", outline="")
            try:
                ph = self._photo(i)
                c.create_image(px, self.tops[i], anchor="nw", image=ph)
            except Exception:
                self._placeholder(c, i, px, self.tops[i], pw, ph_)
            for k, (pg, rx0, ry0, rx1, ry1) in enumerate(self.rects):
                if pg == i:
                    cur = self.hits and 0 <= self.cur < len(self.hits) and self.hits[self.cur] == k
                    c.create_rectangle(px + rx0 * self.scale, self.tops[i] + ry0 * self.scale,
                                       px + rx1 * self.scale, self.tops[i] + ry1 * self.scale,
                                       fill=CUR_BG if cur else HIT_BG, outline="#ff7f00" if cur else "#e0a800",
                                       width=2 if cur else 1, stipple="gray50")
            i += 1

    def _placeholder(self, c, i: int, x: float, y: float, w: float, h: float) -> None:
        c.create_rectangle(x, y, x + w, y + h, fill="#ffffff", outline="")

    # -- search --
    def highlight(self, rx) -> int:
        self.search_id += 1
        self.rects, self.hits, self.cur = [], [], -1
        self._schedule()
        if rx is None or self.doc is None:
            return 0
        terms = self.pane.pdf_terms()
        if not terms:
            return 0
        sid = self.search_id
        self.searching = True
        path, data, npages = self.path, self.data, len(self.sizes)

        def work() -> None:
            out = []
            try:
                d = pymupdf.open(stream=data, filetype="pdf") if data is not None else pymupdf.open(path)
                for i in range(npages):
                    page = d.load_page(i)
                    for t in terms:
                        for r in page.search_for(t):
                            out.append((i, r.x0, r.y0, r.x1, r.y1))
                    if len(out) >= 5000:
                        break
                d.close()
            except Exception:
                pass
            self.q.put((sid, out))

        threading.Thread(target=work, daemon=True).start()
        self.frame.after(100, self._poll)
        return 0  # the count arrives asynchronously via pane.hits_ready()

    def _poll(self) -> None:
        try:
            sid, out = self.q.get_nowait()
        except queue.Empty:
            if self.doc is not None:
                self.frame.after(100, self._poll)
            return
        if sid != self.search_id:
            return
        self.searching = False
        out.sort(key=lambda r: (r[0], r[2], r[1]))
        self.rects = out
        self.hits = list(range(len(out)))
        self.cur = -1
        self._schedule()
        self.pane.hits_ready(self)

    def pdf_pending(self) -> bool:
        return self.searching

    def goto(self, i: int) -> None:
        pg, x0, y0, x1, y1 = self.rects[self.hits[i]]
        total = self.tops[-1] + self.sizes[-1][1] * self.scale + self.GAP
        y = self.tops[pg] + y0 * self.scale
        self.canvas.yview_moveto(max(0.0, (y - self.canvas.winfo_height() / 3) / total))
        if self.total_w > self.canvas.winfo_width():
            x = x0 * self.scale
            self.canvas.xview_moveto(max(0.0, (x - self.canvas.winfo_width() / 3) / self.total_w))
        self._schedule()


# ---- HWP / HWPX pages rendered by rhwp ---------------------------------------------------------


class _NoDoc:
    def close(self) -> None:
        pass


class RhwpView(PdfView):
    """Pages of a HWP/HWPX document, each rendered on demand by the rhwp CLI into a one-page PDF.

    Shares the PDF viewer's canvas, zoom and match marking; pages that are not rendered yet show a
    white placeholder. The page being looked at is rendered first, then the next few (all of them
    while a search is active, up to ``MAX_SEARCH_PAGES``).
    """

    AHEAD = 4
    MAX_SEARCH_PAGES = 120
    WORKERS = 2

    def __init__(self, master, pane) -> None:
        super().__init__(master, pane)
        self.rdoc = None
        self.job = None
        self.pages: dict = {}  # page index -> pymupdf page (kept open while shown)
        self.pdfs: dict = {}  # page index -> pymupdf.Document
        self.failed: set = set()
        self.inflight: set = set()
        self.vis = (0, 0)
        self.terms: list = []
        self.info = ""
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.finished: queue.Queue = queue.Queue()
        self.pump_job = None

    # -- lifecycle --
    def show(self, model) -> None:
        from . import rhwp

        self.clear()
        self.rdoc = model
        self.job = rhwp.Job()
        self.doc = _NoDoc()
        n = min(model.pages, self.MAX_PAGES)
        self.note = "" if model.pages <= n else f"{n} / {model.pages} pages"
        first = self._open_page(0)
        w, h = (first.rect.width, first.rect.height) if first is not None else (595.0, 842.0)
        self.sizes = [(max(w, 1.0), max(h, 1.0))] * n
        self._layout()
        self.canvas.yview_moveto(0)
        self.canvas.xview_moveto(0)
        self._schedule()
        self.info = f"{model.pages}p"
        for _ in range(self.WORKERS):
            threading.Thread(target=self._worker, args=(self.job,), daemon=True).start()
        self._pump()

    def clear(self) -> None:
        if self.job is not None:
            self.job.cancel()
        self.wake.set()
        if self.pump_job is not None:
            try:
                self.frame.after_cancel(self.pump_job)
            except Exception:
                pass
            self.pump_job = None
        for d in self.pdfs.values():
            try:
                d.close()
            except Exception:
                pass
        self.pdfs, self.pages, self.failed, self.inflight = {}, {}, set(), set()
        self.rdoc = self.job = None
        self.terms = []
        self.info = ""
        self.vis = (0, 0)
        self.finished = queue.Queue()
        super().clear()

    # -- page access --
    def _open_page(self, i: int):
        pg = self.pages.get(i)
        if pg is not None:
            return pg
        if self.rdoc is None or not self.rdoc.has(i):
            return None
        try:
            with open(self.rdoc.page_file(i), "rb") as fh:  # read fully: no handle stays open on the cache file
                d = pymupdf.open(stream=fh.read(), filetype="pdf")
            pg = d.load_page(0)
        except Exception:
            self.failed.add(i)
            return None
        self.pdfs[i] = d
        self.pages[i] = pg
        return pg

    def _photo(self, i: int):
        hit = self.cache.get(i)
        if hit is not None and abs(hit[0] - self.scale) < 1e-6:
            self.cache.move_to_end(i)
            return hit[1]
        page = self._open_page(i)
        if page is None:
            raise LookupError(i)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.scale, self.scale), alpha=False)
        try:
            ph = tk.PhotoImage(data=pix.tobytes("ppm"), format="PPM")
        except tk.TclError:
            ph = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")))
        self.cache[i] = (self.scale, ph)
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        return ph

    def _placeholder(self, c, i: int, x: float, y: float, w: float, h: float) -> None:
        c.create_rectangle(x, y, x + w, y + h, fill="#ffffff", outline="")
        msg = "⚠" if i in self.failed else f"{i + 1} …"
        c.create_text(x + w / 2, y + min(h / 2, 200), text=msg, fill="#999999", font=(DEFAULT_FAMILY, 18))

    def _redraw(self) -> None:
        super()._redraw()
        if self.rdoc is None or not self.tops:
            return
        c = self.canvas
        y0 = c.canvasy(0)
        y1 = y0 + c.winfo_height()
        first = max(0, bisect.bisect_right(self.tops, y0) - 1)
        last = first
        while last + 1 < len(self.tops) and self.tops[last + 1] < y1:
            last += 1
        self.vis = (first, last)
        self.wake.set()

    # -- background rendering --
    def _wanted(self) -> int | None:
        """Next page worth rendering (called with the lock held)."""
        n = len(self.sizes)
        first, last = self.vis
        ahead = min(n - 1, last + (self.MAX_SEARCH_PAGES if self.terms else self.AHEAD))
        order = list(range(first, last + 1)) + list(range(last + 1, ahead + 1)) + [first - 1, first - 2]
        for i in order:
            if 0 <= i < n and not self.rdoc.has(i) and i not in self.failed and i not in self.inflight:
                return i
        return None

    def _worker(self, job) -> None:
        while not job.cancelled:
            with self.lock:
                rdoc = self.rdoc if self.job is job else None
                i = self._wanted() if rdoc is not None else None
                if i is not None:
                    self.inflight.add(i)
            if rdoc is None:
                return
            if i is None:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            ok = rdoc.render(i, job)
            with self.lock:
                self.inflight.discard(i)
                if not ok and not job.cancelled:
                    self.failed.add(i)
            if not job.cancelled:
                self.finished.put((job, i, ok))

    def _pump(self) -> None:
        """UI thread: pick up the pages the workers finished."""
        self.pump_job = None
        if self.rdoc is None:
            return
        got = []
        try:
            while True:
                job, i, ok = self.finished.get_nowait()
                if job is self.job:
                    got.append((i, ok))
        except queue.Empty:
            pass
        ready = [i for i, ok in got if ok]
        if self.terms:  # pages already in the cache never go through the workers: pick them up for the search
            n = min(len(self.sizes), self.vis[1] + 1 + self.MAX_SEARCH_PAGES)
            ready += [i for i in range(n) if i not in self.pages and i not in self.failed and i not in ready
                      and self.rdoc.has(i)][:6]
        relayout = False
        for i in ready:
            pg = self._open_page(i)
            if pg is None:
                continue
            sz = (max(pg.rect.width, 1.0), max(pg.rect.height, 1.0))
            if i < len(self.sizes) and sz != self.sizes[i]:
                self.sizes[i] = sz
                relayout = True
            self._search_page(i)
        if relayout:
            self._relayout_keep()
        if ready or got:
            self._schedule()
            if self.terms and not self.hits and not self.pdf_pending():
                self.pane.hits_ready(self)  # every page searched, nothing found
        self.pump_job = self.frame.after(150, self._pump)

    # -- search --
    def highlight(self, rx) -> int:
        self.search_id += 1
        self.rects, self.hits, self.cur = [], [], -1
        self.terms = self.pane.pdf_terms() if rx is not None else []
        self._schedule()
        if self.rdoc is None or not self.terms:
            return 0
        self.wake.set()
        for i in sorted(self.pages):
            self._search_page(i)
        return 0  # matches arrive page by page via pane.hits_ready()

    def _search_page(self, i: int) -> None:
        if not self.terms:
            return
        pg = self.pages.get(i)
        if pg is None:
            return
        found = []
        for t in self.terms:
            try:
                found += [(i, r.x0, r.y0, r.x1, r.y1) for r in pg.search_for(t)]
            except Exception:
                pass
        found = [f for f in found if f not in self.rects]
        if not found:
            return
        had = bool(self.hits)
        cur_key = self.rects[self.hits[self.cur]] if had and 0 <= self.cur < len(self.hits) else None
        self.rects = sorted(self.rects + found, key=lambda r: (r[0], r[2], r[1]))
        self.hits = list(range(len(self.rects)))
        self.cur = self.rects.index(cur_key) if cur_key in self.rects else -1
        if not had:
            self.pane.hits_ready(self)

    def pdf_pending(self) -> bool:
        if not self.terms or self.rdoc is None:
            return False
        n = min(len(self.sizes), self.vis[1] + 1 + self.MAX_SEARCH_PAGES)
        return any(not self.rdoc.has(i) and i not in self.failed for i in range(n))
