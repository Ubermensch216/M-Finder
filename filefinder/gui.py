"""Tk GUI: shared Controller (engine, servers, tray) and any number of search windows."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import __version__, filelist, i18n, query, theme, winshell
from .engine import Engine, is_admin
from .etp import EtpService
from .fmt import format_size, format_time, split_ext
from .gui_dialogs import OptionsDialog, ask_text, organize_bookmarks, organize_filters
from .httpserver import HttpService
from .i18n import _
from .index import FILE_ATTRIBUTE_DIRECTORY
from .preview_pane import PreviewPane
from .settings import Settings, app_dir
from .theme import COLORS


def syntax_help() -> str:
    return _(
        "Search syntax\n\n"
        "  a b          both a AND b            a | b      a OR b\n"
        "  !a           NOT a                   < a | b > c    grouping\n"
        "  \"a b\"        exact phrase            * ?        wildcards (match whole name)\n"
        "  c:\\dir\\x     a backslash matches the full path\n\n"
        "Modifiers (affect the terms that follow):\n"
        "  case:  nocase:  regex:  noregex:  wholeword:  ww:  path:  nopath:  wfn:\n\n"
        "Functions:\n"
        "  file:  folder:  ext:jpg;png  empty:  attrib:H  depth:<3  len:>20\n"
        "  parent:C:\\Windows  infolder:C:\\Windows  content:text  (file contents)\n"
        "  size:>10mb  size:1kb..1mb  size:large|tiny|small|medium|huge|gigantic|empty\n"
        "  dm:today  dm:thisweek  dm:2024-05  dm:>2024-01-01  dm:last3days   (dc: created, da: accessed)\n"
        "Macros:  audio:  video:  pic:  doc:  zip:  exe:\n\n"
        "Shortcuts: Ctrl+I case, Ctrl+B whole word, Ctrl+U path, Ctrl+R regex, Ctrl+P options,\n"
        "F5 rebuild, Ctrl+N new window, Ctrl+D bookmark, Ctrl+Shift+C copy full path."
    )


class VirtualList(tk.Frame):
    """Canvas-drawn table that only renders the visible rows, so millions of results stay fast."""

    COLS = [("name", "Name"), ("path", "Path"), ("size", "Size"), ("mtime", "Date Modified"), ("ext", "File Type")]

    def __init__(self, master, app: "SearchWindow") -> None:
        super().__init__(master)
        self.app = app
        s = app.engine.settings
        self.font = tkfont.Font(family="Segoe UI", size=s.font_size)
        self.rowh = self.font.metrics("linespace") + 4
        self.ids: list[int] = []
        self.first = 0
        self.sel: set[int] = set()
        self.cursor = -1
        self.anchor = -1
        self._resize = None
        self._dragged = False

        self.header = tk.Canvas(self, height=self.rowh + 2, bg=COLORS["header_bg"], highlightthickness=0)
        self.body = tk.Canvas(self, bg=COLORS["bg"], highlightthickness=0, takefocus=True)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.yview)
        self.header.grid(row=0, column=0, sticky="ew")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.sb.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        b = self.body
        b.bind("<Configure>", lambda e: self.redraw())
        b.bind("<Button-1>", self._click)
        b.bind("<Control-Button-1>", lambda e: self._click(e, ctrl=True))
        b.bind("<Shift-Button-1>", lambda e: self._click(e, shift=True))
        b.bind("<Double-Button-1>", lambda e: self.app.open_selected())
        b.bind("<Button-3>", self._right_click)
        b.bind("<MouseWheel>", self._wheel)
        b.bind("<FocusIn>", lambda e: self.redraw())
        b.bind("<FocusOut>", lambda e: self.redraw())
        for key, step in (("Up", -1), ("Down", 1), ("Prior", "page-"), ("Next", "page+"), ("Home", "home"), ("End", "end")):
            b.bind(f"<{key}>", lambda e, st=step: self._key_move(st, False))
            b.bind(f"<Shift-{key}>", lambda e, st=step: self._key_move(st, True))
        b.bind("<Return>", lambda e: self.app.open_selected())
        b.bind("<Delete>", lambda e: self.app.delete_selected())
        b.bind("<Control-a>", lambda e: self.select_all())
        self.header.bind("<Configure>", lambda e: self.draw_header())
        self.header.bind("<Button-1>", self._header_press)
        self.header.bind("<B1-Motion>", self._header_drag)
        self.header.bind("<ButtonRelease-1>", self._header_release)
        self.header.bind("<Motion>", self._header_motion)
        self.header.bind("<MouseWheel>", self._wheel)

    # ---- data -----------------------------------------------------------

    def widths(self) -> dict:
        w = self.app.engine.settings.column_widths
        if "ext" not in w:
            w["ext"] = 80
        return w

    def set_ids(self, ids: list[int], keep_view: bool = False) -> None:
        keep_ids = None
        if keep_view and self.sel:
            keep_ids = {self.ids[p] for p in self.sel if p < len(self.ids)}
        first = self.first if keep_view else 0
        self.ids = ids
        self.first = first
        self.sel = set()
        if keep_ids:
            self.sel = {p for p, i in enumerate(ids[:200000]) if i in keep_ids}
        self.cursor = min(self.cursor, len(ids) - 1) if keep_view else -1
        self.anchor = self.cursor
        self._clamp()
        self.redraw()
        self.app.selection_changed()

    def selected_ids(self) -> list[int]:
        return [self.ids[p] for p in sorted(self.sel) if p < len(self.ids)]

    def set_font_size(self, size: int) -> None:
        self.font.configure(size=size)
        self.rowh = self.font.metrics("linespace") + 4
        self.header.configure(height=self.rowh + 2)
        self.draw_header()
        self.redraw()

    # ---- drawing --------------------------------------------------------

    def _page(self) -> int:
        return max(1, self.body.winfo_height() // self.rowh)

    def _clamp(self) -> None:
        self.first = max(0, min(self.first, max(0, len(self.ids) - self._page())))

    def _fit(self, text: str, width: int) -> str:
        if width <= 8 or self.font.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.font.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def draw_header(self) -> None:
        h = self.header
        h.delete("all")
        h.configure(bg=COLORS["header_bg"])
        s = self.app.engine.settings
        x = 0
        for key, title in self.COLS:
            w = self.widths()[key]
            label = _(title)
            if key == s.sort_key:
                label += "  ▲" if s.sort_ascending else "  ▼"
            h.create_text(x + 6, (self.rowh + 2) // 2, text=label, anchor="w", font=self.font, fill=COLORS["fg"])
            x += w
            h.create_line(x, 2, x, self.rowh, fill=COLORS["line"])
        h.create_line(0, self.rowh + 1, 4000, self.rowh + 1, fill=COLORS["line"])

    def redraw(self) -> None:
        c = self.body
        c.delete("all")
        c.configure(bg=COLORS["bg"])
        w, h = c.winfo_width(), c.winfo_height()
        engine = self.app.engine
        idx = engine.index
        s = engine.settings
        show_dir_sizes = s.show_folder_sizes and idx.folder_sizes_ready
        total = len(self.ids)
        widths = self.widths()
        focus = c.focus_get() is c
        nvis = h // self.rowh + 1
        sel_bg = COLORS["sel"] if focus else COLORS["sel_inactive"]
        for r in range(nvis):
            pos = self.first + r
            if pos >= total:
                break
            i = self.ids[pos]
            y = r * self.rowh
            if pos in self.sel:
                c.create_rectangle(0, y, w, y + self.rowh, fill=sel_bg, outline="")
            if pos == self.cursor and focus:
                c.create_rectangle(0, y, w - 1, y + self.rowh - 1, outline=COLORS["dim"], dash=(1, 2))
            is_dir = bool(idx.attrs[i] & FILE_ATTRIBUTE_DIRECTORY)
            x = 0
            ty = y + self.rowh // 2
            c.create_rectangle(x + 4, y + 4, x + 14, y + self.rowh - 4, fill=COLORS["folder" if is_dir else "file"], outline="")
            c.create_text(x + 20, ty, anchor="w", font=self.font, fill=COLORS["fg"], text=self._fit(idx.names[i], widths["name"] - 24))
            x += widths["name"]
            c.create_text(x + 6, ty, anchor="w", font=self.font, fill=COLORS["fg"], text=self._fit(idx.parent_path(i), widths["path"] - 10))
            x += widths["path"]
            if idx.mtime[i] == 0.0:
                idx.meta(i)
            size, mtime = idx.size[i], idx.mtime[i]
            c.create_text(x + widths["size"] - 8, ty, anchor="e", font=self.font, fill=COLORS["fg"],
                          text=(format_size(size) if show_dir_sizes else "") if is_dir else format_size(size))
            x += widths["size"]
            c.create_text(x + 6, ty, anchor="w", font=self.font, fill=COLORS["fg"], text=format_time(mtime))
            x += widths["mtime"]
            ftype = _("Folder") if is_dir else split_ext(idx.names[i]).upper()
            c.create_text(x + 6, ty, anchor="w", font=self.font, fill=COLORS["fg"], text=self._fit(ftype, widths["ext"] - 10))
        page = max(1, h // self.rowh)
        if total <= page:
            self.sb.set(0, 1)
        else:
            self.sb.set(self.first / total, min(1.0, (self.first + page) / total))

    # ---- scrolling ------------------------------------------------------

    def yview(self, *args) -> None:
        total = len(self.ids)
        if args[0] == "moveto":
            self.first = int(float(args[1]) * total)
        elif args[0] == "scroll":
            n = int(args[1])
            self.first += n * (self._page() - 1 if args[2] == "pages" else 1)
        self._clamp()
        self.redraw()

    def _wheel(self, e) -> None:
        self.first -= int(e.delta / 120) * 3
        self._clamp()
        self.redraw()

    def _ensure_visible(self, pos: int) -> None:
        if pos < self.first:
            self.first = pos
        elif pos >= self.first + self._page():
            self.first = pos - self._page() + 1
        self._clamp()

    # ---- selection ------------------------------------------------------

    def _pos_at(self, y: int) -> int:
        pos = self.first + y // self.rowh
        return pos if 0 <= pos < len(self.ids) else -1

    def _click(self, e, ctrl: bool = False, shift: bool = False):
        self.body.focus_set()
        pos = self._pos_at(e.y)
        if pos < 0:
            if not ctrl and not shift:
                self.sel = set()
        elif shift and self.anchor >= 0:
            a, b = sorted((self.anchor, pos))
            self.sel = set(range(a, b + 1))
            self.cursor = pos
        elif ctrl:
            self.sel ^= {pos}
            self.cursor = self.anchor = pos
        else:
            self.sel = {pos}
            self.cursor = self.anchor = pos
        self.redraw()
        self.app.selection_changed()
        return "break"

    def _right_click(self, e) -> None:
        self.body.focus_set()
        pos = self._pos_at(e.y)
        if pos >= 0 and pos not in self.sel:
            self.sel = {pos}
            self.cursor = self.anchor = pos
            self.redraw()
            self.app.selection_changed()
        self.app.show_context_menu(e.x_root, e.y_root)

    def _key_move(self, step, extend: bool):
        total = len(self.ids)
        if not total:
            return "break"
        cur = self.cursor if self.cursor >= 0 else 0
        if step == "home":
            new = 0
        elif step == "end":
            new = total - 1
        elif step == "page-":
            new = cur - self._page() + 1
        elif step == "page+":
            new = cur + self._page() - 1
        else:
            new = cur + step if self.cursor >= 0 else 0
        new = max(0, min(total - 1, new))
        if extend and self.anchor >= 0:
            a, b = sorted((self.anchor, new))
            self.sel = set(range(a, b + 1))
        else:
            self.sel = {new}
            self.anchor = new
        self.cursor = new
        self._ensure_visible(new)
        self.redraw()
        self.app.selection_changed()
        return "break"

    def select_all(self):
        self.sel = set(range(len(self.ids)))
        self.redraw()
        self.app.selection_changed()
        return "break"

    def invert_selection(self) -> None:
        self.sel = set(range(len(self.ids))) - self.sel
        self.redraw()
        self.app.selection_changed()

    # ---- header interaction --------------------------------------------

    def _border_at(self, x: int):
        edge = 0
        for key, _t in self.COLS:
            edge += self.widths()[key]
            if abs(x - edge) <= 4:
                return key
        return None

    def _col_at(self, x: int):
        edge = 0
        for key, _t in self.COLS:
            edge += self.widths()[key]
            if x < edge:
                return key
        return None

    def _header_motion(self, e) -> None:
        self.header.configure(cursor="sb_h_double_arrow" if self._border_at(e.x) else "")

    def _header_press(self, e) -> None:
        self._dragged = False
        key = self._border_at(e.x)
        self._resize = (key, e.x, self.widths()[key]) if key else None

    def _header_drag(self, e) -> None:
        if self._resize:
            key, x0, w0 = self._resize
            self.widths()[key] = max(30, w0 + e.x - x0)
            self._dragged = True
            self.draw_header()
            self.redraw()

    def _header_release(self, e) -> None:
        if not self._dragged and not self._resize:
            key = self._col_at(e.x)
            if key:
                self.app.sort_by(key)
        self._resize = None


class SearchWindow(tk.Toplevel):
    def __init__(self, ctl: "Controller", query_text: str = "") -> None:
        super().__init__(ctl.root)
        self.ctl = ctl
        s = ctl.settings
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.seq = 0
        self._after = None
        self.last_rev = -1
        self.last_index_id = 0
        self.last_run = 0.0
        self.error = ""
        self.title("FileFinder")
        self.geometry(s.geometry)
        self.minsize(480, 240)
        self.configure(bg=COLORS["window"])
        theme.apply_window(self)
        try:
            self.iconbitmap(ctl.icon_path)
        except tk.TclError:
            pass

        self.v_case = tk.BooleanVar(value=s.match_case)
        self.v_ww = tk.BooleanVar(value=s.match_whole_word)
        self.v_path = tk.BooleanVar(value=s.match_path)
        self.v_regex = tk.BooleanVar(value=s.regex)
        self.v_status = tk.BooleanVar(value=s.show_status_bar)
        self.v_preview = tk.BooleanVar(value=s.show_preview)
        self.v_ppos = tk.StringVar(value=s.preview_pos if s.preview_pos in ("bottom", "right", "left") else "right")
        self._preview_after = None
        self.var = tk.StringVar(value=query_text)

        self._build_menu()
        top = ttk.Frame(self)
        top.pack(fill="x", padx=2, pady=2)
        self.cb_filter = ttk.Combobox(top, state="readonly", width=14)
        self.cb_filter.pack(side="left", padx=(0, 4))
        self.cb_filter.bind("<<ComboboxSelected>>", self._filter_changed)
        self.entry = ttk.Entry(top, textvariable=self.var, font=("Segoe UI", s.font_size))
        self.entry.pack(side="left", fill="x", expand=True)
        self.paned = tk.PanedWindow(self, orient="vertical", sashwidth=5, bd=0, bg=COLORS["line"], sashrelief="flat")
        self.paned.pack(fill="both", expand=True)
        self.list = VirtualList(self.paned, self)
        self.preview = PreviewPane(self.paned, self)
        self.layout_preview()
        self.statusbar = ttk.Frame(self)
        self.lbl_left = ttk.Label(self.statusbar, text="")
        self.lbl_right = ttk.Label(self.statusbar, text="")
        self.lbl_left.pack(side="left", padx=6)
        self.lbl_right.pack(side="right", padx=6)
        if s.show_status_bar:
            self.statusbar.pack(fill="x", side="bottom")
        self.refresh_filters()
        self.rebuild_bookmark_menu()

        self.var.trace_add("write", lambda *_a: self.schedule_search())
        self.entry.bind("<Down>", lambda e: self._focus_list())
        self.entry.bind("<Escape>", lambda e: self.var.set(""))
        self.entry.bind("<Return>", lambda e: self._focus_list())
        for seq, fn in (
            ("<Control-i>", lambda: self._toggle(self.v_case)),
            ("<Control-b>", lambda: self._toggle(self.v_ww)),
            ("<Control-u>", lambda: self._toggle(self.v_path)),
            ("<Control-r>", lambda: self._toggle(self.v_regex)),
            ("<Control-p>", self.options),
            ("<F5>", ctl.rebuild),
            ("<Control-n>", ctl.new_window),
            ("<Control-w>", self.on_close),
            ("<Control-d>", self.add_bookmark),
            ("<Control-f>", self._focus_entry),
            ("<Control-l>", self._focus_entry),
            ("<F3>", self._focus_entry),
            ("<Control-Shift-C>", self.copy_full_path),
            ("<Control-c>", self._copy_shortcut),
            ("<Control-plus>", lambda: self.zoom(1)),
            ("<Control-equal>", lambda: self.zoom(1)),
            ("<Control-minus>", lambda: self.zoom(-1)),
            ("<Control-0>", lambda: self.zoom(0)),
            ("<F4>", self.toggle_preview_key),
            ("<Alt-p>", self.toggle_preview_key),
        ):
            self.bind(seq, lambda e, f=fn: (f(), "break")[1])
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_title()
        self.entry.focus_set()

    @property
    def engine(self) -> Engine:
        return self.ctl.engine

    # ---- searching ---------------------------------------------------------

    def flags(self) -> query.Flags:
        return query.Flags(case=self.v_case.get(), ww=self.v_ww.get(), path=self.v_path.get(), regex=self.v_regex.get())

    def schedule_search(self) -> None:
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(30, self.run_search)

    def run_search(self, keep_view: bool = False) -> None:
        self._after = None
        self.cancel.set()
        cancel = self.cancel = threading.Event()
        self.seq += 1
        seq = self.seq
        s = self.ctl.settings
        text = self.var.get()
        flags = self.flags()
        flt = s.current_filter
        self.last_index_id = id(self.engine.index)
        self.last_run = time.time()
        rev = self.engine.index.rev
        self._set_title()
        self.preview.highlight()

        def work() -> None:
            try:
                ids = self.engine.search(text, s.sort_key, s.sort_ascending, cancel, flags, flt)
            except query.QueryError as e:
                self.q.put(("error", seq, str(e)))
                return
            if ids is not None:
                self.q.put(("results", seq, ids, keep_view, rev))

        threading.Thread(target=work, daemon=True).start()

    def poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "results" and msg[1] == self.seq:
                    self.error = ""
                    self.list.set_ids(msg[2], keep_view=msg[3])
                    self.last_rev = msg[4]
                elif msg[0] == "error" and msg[1] == self.seq:
                    self.error = msg[2]
                    self.selection_changed()
        except queue.Empty:
            pass
        idx = self.engine.index
        if (
            not self.engine.building
            and (idx.rev != self.last_rev or id(idx) != self.last_index_id)
            and time.time() - self.last_run > 1.0
            and self.last_rev != -1
        ):
            self.run_search(keep_view=True)
        self.update_right_label()

    def update_right_label(self) -> None:
        e = self.engine
        idx = e.index
        if e.building:
            txt = e.status
        elif e.filling:
            txt = _("Reading file information...")
        elif not idx.roots:
            txt = e.status
        else:
            n = f"{idx.live_count():,} " + _("objects")
            kinds = {r["kind"] for r in idx.roots}
            if kinds <= {"ntfs", "filelist"} and "ntfs" in kinds:
                txt = n + "  (" + _("NTFS MFT + USN journal") + ")"
            elif not is_admin() and "ntfs" not in kinds:
                txt = n + "  (" + _("folder scan; run as administrator or install the service for MFT") + ")"
            else:
                txt = n
        self.lbl_right.configure(text=txt)

    def _set_title(self) -> None:
        t = self.var.get().strip()
        self.title(f"{t} - FileFinder" if t else "FileFinder")

    def sort_by(self, key: str) -> None:
        s = self.ctl.settings
        if key == s.sort_key:
            s.sort_ascending = not s.sort_ascending
        else:
            s.sort_key, s.sort_ascending = key, key in ("name", "path", "ext")
        self.set_sort(s.sort_key, s.sort_ascending)

    def set_sort(self, key: str, ascending: bool) -> None:
        s = self.ctl.settings
        if (
            query.sort_needs_meta(key)
            and s.confirm_large_sort
            and len(self.list.ids) > 100000
            and self.engine.index.unfetched() > 0
        ):
            if not messagebox.askyesno(
                _("Confirm sort"),
                _("Sorting by size or date of this many items requires reading each file's information and may take several minutes.")
                + "\n\n" + _("Continue?"),
                parent=self,
            ):
                return
        s.sort_key, s.sort_ascending = key, ascending
        for w in self.ctl.windows:
            w.list.draw_header()
        self.run_search()

    def _toggle(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self.apply_flags()

    def apply_flags(self) -> None:
        s = self.ctl.settings
        s.match_case, s.match_whole_word, s.match_path, s.regex = (
            self.v_case.get(), self.v_ww.get(), self.v_path.get(), self.v_regex.get())
        self.run_search()

    # ---- filters / bookmarks --------------------------------------------------------

    def refresh_filters(self) -> None:
        s = self.ctl.settings
        names = [f["name"] for f in s.filters] or ["Everything"]
        self.cb_filter.configure(values=[_(n) if n in DEFAULT_FILTER_NAMES else n for n in names])
        self._filter_names = names
        cur = s.current_filter if s.current_filter in names else names[0]
        s.current_filter = cur
        self.cb_filter.current(names.index(cur))

    def _filter_changed(self, _e=None) -> None:
        self.ctl.settings.current_filter = self._filter_names[self.cb_filter.current()]
        self.run_search()
        self.entry.focus_set()

    def add_filter(self) -> None:
        name = ask_text(self, _("Add to filters"), _("Filter name:"))
        if not name:
            return
        f = self.flags()
        self.ctl.set_filters(
            self.ctl.settings.filters + [{"name": name, "search": self.var.get(), "case": f.case, "ww": f.ww, "path": f.path, "regex": f.regex}]
        )

    def add_bookmark(self) -> None:
        name = ask_text(self, _("Add to bookmarks"), _("Bookmark name:"), self.var.get())
        if not name:
            return
        s = self.ctl.settings
        f = self.flags()
        self.ctl.set_bookmarks(s.bookmarks + [{
            "name": name, "search": self.var.get(), "filter": s.current_filter, "case": f.case, "ww": f.ww,
            "path": f.path, "regex": f.regex, "sort": s.sort_key, "asc": s.sort_ascending,
        }])

    def apply_bookmark(self, b: dict) -> None:
        s = self.ctl.settings
        self.v_case.set(b.get("case", False)); self.v_ww.set(b.get("ww", False))
        self.v_path.set(b.get("path", False)); self.v_regex.set(b.get("regex", False))
        s.sort_key, s.sort_ascending = b.get("sort", s.sort_key), b.get("asc", s.sort_ascending)
        if b.get("filter") and s.find_filter(b["filter"]):
            s.current_filter = b["filter"]
            self.refresh_filters()
        self.var.set(b.get("search", ""))
        self.apply_flags()

    def rebuild_bookmark_menu(self) -> None:
        m = self.bookmark_menu
        m.delete(0, "end")
        m.add_command(label=_("Add to Bookmarks..."), command=self.add_bookmark, accelerator="Ctrl+D")
        m.add_command(label=_("Organize Bookmarks..."), command=lambda: organize_bookmarks(self))
        bms = self.ctl.settings.bookmarks
        if bms:
            m.add_separator()
        for b in bms:
            m.add_command(label=b["name"], command=lambda b=b: self.apply_bookmark(b))

    # ---- selection / actions --------------------------------------------------

    def _paths(self) -> list[str]:
        idx = self.engine.index
        return [idx.full_path(i) for i in self.list.selected_ids()]

    def selection_changed(self) -> None:
        self._schedule_preview()
        n, sel = len(self.list.ids), len(self.list.sel)
        if self.error:
            self.lbl_left.configure(text=self.error)
        elif sel:
            self.lbl_left.configure(text=f"{sel:,} / {n:,} " + _("objects selected"))
        else:
            self.lbl_left.configure(text=f"{n:,} " + _("objects"))

    def _schedule_preview(self) -> None:
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(120, self._update_preview)

    def _update_preview(self) -> None:
        self._preview_after = None
        if not self.v_preview.get():
            return
        ids = self.list.selected_ids()
        if not ids:
            self.preview.clear()
        elif len(ids) > 1:
            self.preview.clear(f"{len(ids):,} " + _("objects selected"))
        else:
            self.preview.show_file(self.engine.index.full_path(ids[0]))

    def toggle_preview_key(self) -> None:
        self.v_preview.set(not self.v_preview.get())
        self.toggle_preview()

    def toggle_preview(self) -> None:
        self._remember_preview_size()
        self.ctl.settings.show_preview = self.v_preview.get()
        self.layout_preview()
        if self.v_preview.get():
            self.preview.path = ""
            self._update_preview()

    def set_preview_pos(self) -> None:
        """View > Preview Pane > Bottom / Right / Left."""
        self._remember_preview_size()
        s = self.ctl.settings
        s.preview_pos = self.v_ppos.get()
        s.show_preview = True
        self.v_preview.set(True)
        self.layout_preview()
        self.preview.path = ""
        self._update_preview()

    def _preview_shown(self) -> bool:
        return str(self.preview) in [str(p) for p in self.paned.panes()]

    def _remember_preview_size(self) -> None:
        try:
            if not self._preview_shown():
                return
            s = self.ctl.settings
            if str(self.paned.cget("orient")) == "horizontal":
                w = self.preview.winfo_width()
                if w > 100:
                    s.preview_width = w
            else:
                h = self.preview.winfo_height()
                if h > 60:
                    s.preview_height = h
        except tk.TclError:
            pass

    def layout_preview(self) -> None:
        """(Re)build the split between the result list and the preview pane."""
        s = self.ctl.settings
        for w in (self.list, self.preview):
            try:
                self.paned.forget(w)
            except tk.TclError:
                pass
        pos = self.v_ppos.get()
        side = pos in ("left", "right")
        self.paned.configure(orient="horizontal" if side else "vertical")
        if not s.show_preview:
            self.paned.add(self.list, minsize=80, stretch="always")
            return
        size = {"width": max(160, s.preview_width)} if side else {"height": max(80, s.preview_height)}
        pane_kw = dict(minsize=150 if side else 60, stretch="never", **size)
        if pos == "left":
            self.paned.add(self.preview, **pane_kw)
            self.paned.add(self.list, minsize=120, stretch="always")
        else:
            self.paned.add(self.list, minsize=120 if side else 80, stretch="always")
            self.paned.add(self.preview, **pane_kw)

    def open_selected(self) -> None:
        paths = self._paths()
        if len(paths) > 10 and not messagebox.askyesno(_("Confirm open"), _("Are you sure you want to open this many items?") + f" ({len(paths)})", parent=self):
            return
        for p in paths:
            try:
                winshell.open_item(p)
            except OSError as e:
                messagebox.showerror("FileFinder", f"{p}\n\n{e}", parent=self)
                break

    def open_path(self) -> None:
        for p in self._paths()[:10]:
            winshell.reveal(p)

    def _clip(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def copy_full_path(self) -> None:
        self._clip("\r\n".join(self._paths()))

    def copy_name(self) -> None:
        idx = self.engine.index
        self._clip("\r\n".join(idx.names[i] for i in self.list.selected_ids()))

    def copy_parent_path(self) -> None:
        idx = self.engine.index
        self._clip("\r\n".join(idx.parent_path(i) for i in self.list.selected_ids()))

    def _copy_shortcut(self) -> None:
        f = self.focus_get()
        if f is self.entry:
            self.entry.event_generate("<<Copy>>")
        elif f is not None and str(f).startswith(str(self.preview)) and isinstance(f, tk.Text) and f.tag_ranges("sel"):
            f.event_generate("<<Copy>>")  # copy the text selected in the preview
        else:
            self.copy_full_path()

    def delete_selected(self) -> None:
        ids = self.list.selected_ids()
        if not ids:
            return
        idx = self.engine.index
        paths = [idx.full_path(i) for i in ids]
        winshell.recycle(paths)
        for i, p in zip(ids, paths):
            if not os.path.lexists(p):
                idx.remove(i)
        self.run_search(keep_view=True)

    def export(self) -> None:
        f = filedialog.asksaveasfilename(
            parent=self, title=_("Export results"), defaultextension=".efu",
            filetypes=[("Everything file list", "*.efu"), ("CSV", "*.csv"), ("Text", "*.txt")],
        )
        if not f:
            return
        try:
            filelist.export_results(self.engine.index, self.list.selected_ids() or self.list.ids, f)
        except OSError as e:
            messagebox.showerror("FileFinder", f"{e}", parent=self)

    def open_filelist(self) -> None:
        f = filedialog.askopenfilename(parent=self, filetypes=[("File list", "*.efu *.csv *.txt"), ("All", "*.*")])
        if f:
            self.ctl.add_filelist(os.path.normpath(f), self)

    def show_context_menu(self, x: int, y: int) -> None:
        m = tk.Menu(self, tearoff=0)
        st = "normal" if self.list.sel else "disabled"
        m.add_command(label=_("Open"), command=self.open_selected, state=st)
        m.add_command(label=_("Open Path"), command=self.open_path, state=st)
        m.add_separator()
        m.add_command(label=_("Copy Full Path to Clipboard"), command=self.copy_full_path, state=st, accelerator="Ctrl+Shift+C")
        m.add_command(label=_("Copy Name to Clipboard"), command=self.copy_name, state=st)
        m.add_command(label=_("Copy Path to Clipboard"), command=self.copy_parent_path, state=st)
        m.add_separator()
        m.add_command(label=_("Delete (Recycle Bin)"), command=self.delete_selected, state=st, accelerator="Del")
        m.tk_popup(x, y)

    # ---- menus -----------------------------------------------------------------

    def _build_menu(self) -> None:
        ctl = self.ctl
        mb = tk.Menu(self)
        f = tk.Menu(mb, tearoff=0)
        f.add_command(label=_("New Search Window"), command=ctl.new_window, accelerator="Ctrl+N")
        f.add_command(label=_("Open File List..."), command=self.open_filelist)
        f.add_separator()
        f.add_command(label=_("Open"), command=self.open_selected)
        f.add_command(label=_("Open Path"), command=self.open_path)
        f.add_separator()
        f.add_command(label=_("Export..."), command=self.export)
        f.add_separator()
        f.add_command(label=_("Close"), command=self.on_close, accelerator="Ctrl+W")
        f.add_command(label=_("Exit"), command=ctl.quit)
        mb.add_cascade(label=_("File"), menu=f)

        e = tk.Menu(mb, tearoff=0)
        e.add_command(label=_("Copy Full Path"), command=self.copy_full_path, accelerator="Ctrl+Shift+C")
        e.add_command(label=_("Copy Name"), command=self.copy_name)
        e.add_command(label=_("Copy Path"), command=self.copy_parent_path)
        e.add_separator()
        e.add_command(label=_("Select All"), command=lambda: self.list.select_all(), accelerator="Ctrl+A")
        e.add_command(label=_("Invert Selection"), command=lambda: self.list.invert_selection())
        e.add_separator()
        e.add_command(label=_("Delete (Recycle Bin)"), command=self.delete_selected, accelerator="Del")
        mb.add_cascade(label=_("Edit"), menu=e)

        v = tk.Menu(mb, tearoff=0)
        v.add_checkbutton(label=_("Status Bar"), variable=self.v_status, command=self.toggle_status)
        pm = tk.Menu(v, tearoff=0)
        pm.add_checkbutton(label=_("Show Preview Pane"), variable=self.v_preview, command=self.toggle_preview, accelerator="F4")
        pm.add_separator()
        for value, title in (("bottom", "Bottom"), ("right", "Right"), ("left", "Left")):
            pm.add_radiobutton(label=_(title), variable=self.v_ppos, value=value, command=self.set_preview_pos)
        v.add_cascade(label=_("Preview Pane"), menu=pm)
        v.add_separator()
        v.add_command(label=_("Zoom In"), command=lambda: self.zoom(1), accelerator="Ctrl++")
        v.add_command(label=_("Zoom Out"), command=lambda: self.zoom(-1), accelerator="Ctrl+-")
        v.add_command(label=_("Reset Zoom"), command=lambda: self.zoom(0), accelerator="Ctrl+0")
        v.add_separator()
        sm = tk.Menu(v, tearoff=0)
        for key, title in (("name", "Name"), ("path", "Path"), ("size", "Size"), ("mtime", "Date Modified"), ("ext", "Extension")):
            sm.add_command(label=_(title), command=lambda k=key: self.set_sort(k, k in ("name", "path", "ext")))
        sm.add_separator()
        sm.add_command(label=_("Ascending"), command=lambda: self.set_sort(self.ctl.settings.sort_key, True))
        sm.add_command(label=_("Descending"), command=lambda: self.set_sort(self.ctl.settings.sort_key, False))
        v.add_cascade(label=_("Sort By"), menu=sm)
        mb.add_cascade(label=_("View"), menu=v)

        s = tk.Menu(mb, tearoff=0)
        s.add_checkbutton(label=_("Match Case"), variable=self.v_case, command=self.apply_flags, accelerator="Ctrl+I")
        s.add_checkbutton(label=_("Match Whole Word"), variable=self.v_ww, command=self.apply_flags, accelerator="Ctrl+B")
        s.add_checkbutton(label=_("Match Path"), variable=self.v_path, command=self.apply_flags, accelerator="Ctrl+U")
        s.add_checkbutton(label=_("Enable Regex"), variable=self.v_regex, command=self.apply_flags, accelerator="Ctrl+R")
        s.add_separator()
        s.add_command(label=_("Add to Filters..."), command=self.add_filter)
        s.add_command(label=_("Organize Filters..."), command=lambda: organize_filters(self))
        mb.add_cascade(label=_("Search"), menu=s)

        self.bookmark_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label=_("Bookmarks"), menu=self.bookmark_menu)

        t = tk.Menu(mb, tearoff=0)
        t.add_command(label=_("Options..."), command=self.options, accelerator="Ctrl+P")
        t.add_command(label=_("Rebuild Index"), command=ctl.rebuild, accelerator="F5")
        t.add_command(label=_("Compute Folder Sizes Now"), command=ctl.compute_folder_sizes)
        t.add_separator()
        t.add_checkbutton(label=_("HTTP Server"), variable=ctl.http_var, command=ctl.toggle_http)
        t.add_checkbutton(label=_("ETP/FTP Server"), variable=ctl.etp_var, command=ctl.toggle_etp)
        if not is_admin() and os.name == "nt":
            t.add_separator()
            t.add_command(label=_("Restart as Administrator"), command=ctl.restart_as_admin)
        mb.add_cascade(label=_("Tools"), menu=t)

        h = tk.Menu(mb, tearoff=0)
        h.add_command(label=_("Search Syntax"), command=lambda: messagebox.showinfo(_("Search syntax"), syntax_help(), parent=self))
        h.add_command(label=_("About FileFinder"), command=self.about)
        mb.add_cascade(label=_("Help"), menu=h)
        self.config(menu=mb)

    def _focus_entry(self) -> None:
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def _focus_list(self) -> None:
        self.list.body.focus_set()
        if self.list.ids and not self.list.sel:
            self.list.sel = {0}
            self.list.cursor = self.list.anchor = 0
            self.list.redraw()
            self.selection_changed()

    def toggle_status(self) -> None:
        self.ctl.settings.show_status_bar = self.v_status.get()
        if self.v_status.get():
            self.statusbar.pack(fill="x", side="bottom")
        else:
            self.statusbar.pack_forget()

    def zoom(self, delta: int) -> None:
        s = self.ctl.settings
        s.font_size = 10 if delta == 0 else max(6, min(32, s.font_size + delta))
        for w in self.ctl.windows:
            w.list.set_font_size(s.font_size)
            w.preview.set_font_size(s.font_size)

    def about(self) -> None:
        messagebox.showinfo(
            _("About FileFinder"),
            f"FileFinder {__version__}\n\n" + _("An Everything-style instant file name search.") + "\n"
            + _("NTFS MFT indexing, USN journal live updates, Everything search syntax."),
            parent=self,
        )

    def options(self) -> None:
        OptionsDialog(self)

    def on_close(self) -> None:
        self._remember_preview_size()
        self.ctl.close_window(self)


DEFAULT_FILTER_NAMES = {"Everything", "Audio", "Compressed", "Document", "Executable", "Folder", "Picture", "Video"}


class Controller:
    """Owns the Tk root, the shared engine, servers, tray icon and the search windows."""

    def __init__(self, settings: Settings, query_text: str = "", minimized: bool = False) -> None:
        self.settings = settings
        i18n.init(settings.language)
        self.root = tk.Tk()
        self.root.withdraw()
        theme.apply(self.root, settings.theme)
        from .tray import ensure_icon_file

        self.icon_path = ensure_icon_file(app_dir())
        self.engine = Engine(settings)
        self.q: queue.Queue = queue.Queue()
        self.engine.on_status = lambda t: self.q.put(("status", t))
        self.windows: list[SearchWindow] = []
        self.http, self.etp = HttpService(self.engine), EtpService(self.engine)
        self.http_var = tk.BooleanVar(master=self.root, value=False)
        self.etp_var = tk.BooleanVar(master=self.root, value=False)
        self.tray = None
        self.ipc = None
        self._quitting = False

        self._start_ipc()
        self._start_tray()
        if minimized and self.tray is not None:
            self.hidden = True
        else:
            self.hidden = False
            self.new_window(query_text)
        threading.Thread(target=self._startup, daemon=True).start()
        if settings.http_enabled:
            self.toggle_http(force=True)
        if settings.etp_enabled:
            self.toggle_etp(force=True)
        self.root.after(100, self._poll)

    # ---- lifecycle ---------------------------------------------------------------

    def _startup(self) -> None:
        e = self.engine
        if e.load():
            e.start_monitors()
            e.start_background()
        else:
            e.rebuild()
        self.q.put(("ready",))

    def _start_ipc(self) -> None:
        if not self.settings.ipc_enabled or os.name != "nt":
            return
        from .queryipc import QueryIpc

        try:
            self.ipc = QueryIpc(self.engine, on_show=lambda q: self.q.put(("show", q)))
            self.ipc.start()
        except OSError:
            self.ipc = None

    def _start_tray(self) -> None:
        s = self.settings
        if not s.tray_icon or os.name != "nt":
            return
        from .tray import Tray

        items = [("show", _("Show FileFinder")), ("new", _("New Search Window")), ("rebuild", _("Rebuild Index")), ("-", ""), ("exit", _("Exit"))]
        t = Tray("FileFinder", self.icon_path, items, lambda ev: self.q.put(("tray", ev)))
        if t.start():
            self.tray = t
            if s.global_hotkey and not t.set_hotkey(s.global_hotkey):
                messagebox.showwarning("FileFinder", _("Could not register the global hotkey (invalid or already in use)."))
        else:
            self.tray = None

    def settings_changed(self) -> None:
        """Apply settings that can take effect without a restart."""
        s = self.settings
        for w in self.windows:
            w.refresh_filters()
            w.list.set_font_size(s.font_size)
        if s.tray_icon and self.tray is None:
            self._start_tray()
        elif not s.tray_icon and self.tray is not None:
            self.tray.stop()
            self.tray = None
        elif self.tray is not None:
            self.tray.set_hotkey(s.global_hotkey)
        if self.http.running:
            self.toggle_http(force=True, restart=True)
        if self.etp.running:
            self.toggle_etp(force=True, restart=True)
        self.engine.start_monitors()

    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "ready":
                    for w in list(self.windows):
                        w.run_search()
                elif kind == "show":
                    self.show(msg[1])
                elif kind == "tray":
                    self._tray_event(msg[1])
        except queue.Empty:
            pass
        for w in list(self.windows):
            w.poll()
        if not self._quitting:
            self.root.after(100, self._poll)

    def _tray_event(self, ev: str) -> None:
        if ev in ("show", "hotkey"):
            self.show()
        elif ev == "new":
            self.new_window()
        elif ev == "rebuild":
            self.rebuild()
        elif ev == "exit":
            self.quit()

    # ---- windows -----------------------------------------------------------------

    def new_window(self, query_text: str = "") -> SearchWindow:
        w = SearchWindow(self, query_text)
        self.windows.append(w)
        self.hidden = False
        if self.engine.index.roots:
            w.run_search()
        return w

    def show(self, query_text: str | None = None) -> None:
        if not self.windows:
            self.new_window(query_text or "")
            return
        w = self.windows[-1]
        w.deiconify()
        w.lift()
        w.focus_force()
        if query_text:
            w.var.set(query_text)
        w._focus_entry()

    def close_window(self, win: SearchWindow) -> None:
        s = self.settings
        s.geometry = win.geometry()
        if len(self.windows) > 1:
            self.windows.remove(win)
            win.destroy()
        elif s.run_in_background and self.tray is not None:
            win.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        s = self.settings
        if self.windows:
            s.geometry = self.windows[-1].geometry()
        try:
            s.save()
        except OSError:
            pass
        if self.tray:
            self.tray.stop()
        if self.ipc:
            self.ipc.stop()
        self.http.stop()
        self.etp.stop()
        for w in self.windows:
            w.cancel.set()
        self.engine.shutdown()
        self.root.destroy()

    # ---- shared actions ------------------------------------------------------------

    def rebuild(self) -> None:
        if self.engine.building:
            return

        def work() -> None:
            self.engine.rebuild()
            self.q.put(("ready",))

        threading.Thread(target=work, daemon=True).start()

    def compute_folder_sizes(self) -> None:
        def work() -> None:
            self.engine.index.ensure_meta(range(len(self.engine.index)))
            self.engine.index.compute_folder_sizes()
            self.engine.index.touch()

        threading.Thread(target=work, daemon=True).start()

    def add_filelist(self, path: str, parent) -> None:
        if path not in self.settings.filelists:
            self.settings.filelists.append(path)
            self.settings.save()
        if messagebox.askyesno("FileFinder", _("Rebuild the index now to include this file list?"), parent=parent):
            self.rebuild()

    def set_bookmarks(self, items: list[dict]) -> None:
        self.settings.bookmarks = items
        for w in self.windows:
            w.rebuild_bookmark_menu()

    def set_filters(self, items: list[dict]) -> None:
        self.settings.filters = items
        for w in self.windows:
            w.refresh_filters()

    def toggle_http(self, force: bool = False, restart: bool = False) -> None:
        if restart:
            self.http.stop()
        want = True if force else self.http_var.get()
        if self.http.running and not want:
            self.http.stop()
        elif want and not self.http.running:
            try:
                self.http.start()
            except OSError as e:
                messagebox.showerror("HTTP Server", str(e))
                want = False
        self.http_var.set(self.http.running)

    def toggle_etp(self, force: bool = False, restart: bool = False) -> None:
        if restart:
            self.etp.stop()
        want = True if force else self.etp_var.get()
        if self.etp.running and not want:
            self.etp.stop()
        elif want and not self.etp.running:
            try:
                self.etp.start()
            except OSError as e:
                messagebox.showerror("ETP/FTP Server", str(e))
        self.etp_var.set(self.etp.running)

    def restart_as_admin(self) -> None:
        if winshell.relaunch_as_admin("gui --new-instance"):
            self.quit()


def run(settings: Settings, query_text: str = "", minimized: bool = False, new_instance: bool = False) -> int:
    if os.name == "nt" and settings.single_instance and not new_instance:
        from . import queryipc

        if queryipc.running():  # hand the query to the running instance and leave
            try:
                queryipc.request({"op": "show", "query": query_text})
                return 0
            except OSError:
                pass
    Controller(settings, query_text, minimized).root.mainloop()
    return 0
