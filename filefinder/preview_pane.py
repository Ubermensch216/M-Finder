"""The preview pane of a search window: picks the viewer for the selected file's format."""

from __future__ import annotations

import os
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from . import preview, viewers
from .i18n import _
from .theme import COLORS
from .viewers import widgets

if TYPE_CHECKING:  # pragma: no cover
    from .gui import SearchWindow


class PreviewPane(tk.Frame):
    """Header (name, match counter, ◀ ▶, zoom) above the viewer that fits the file type."""

    def __init__(self, master, app: "SearchWindow") -> None:
        super().__init__(master, bg=COLORS["window"])
        self.app = app
        self.font_size = app.ctl.settings.font_size
        self.seq = 0
        self.q: queue.Queue = queue.Queue()
        self.path = ""
        self.active = None
        self.views: dict = {}
        self.loading = False

        bar = tk.Frame(self, bg=COLORS["header_bg"])
        bar.pack(fill="x")
        self.lbl = tk.Label(bar, text="", anchor="w", bg=COLORS["header_bg"], fg=COLORS["fg"])
        self.lbl.pack(side="left", fill="x", expand=True, padx=6)
        for text, cmd in (("▶", lambda: self.step(1)), ("◀", lambda: self.step(-1)),
                          ("+", lambda: self.zoom(1)), ("−", lambda: self.zoom(-1))):
            ttk.Button(bar, text=text, width=3, command=cmd).pack(side="right", padx=1, pady=1)
        self.lbl_hits = tk.Label(bar, text="", bg=COLORS["header_bg"], fg=COLORS["dim"])
        self.lbl_hits.pack(side="right", padx=6)
        self.container = tk.Frame(self, bg=COLORS["bg"])
        self.container.pack(fill="both", expand=True)
        self.msg = tk.Label(self.container, text="", bg=COLORS["bg"], fg=COLORS["dim"], justify="center",
                            wraplength=420)

    # ---- viewer management -------------------------------------------------------------

    def _view(self, kind: str):
        v = self.views.get(kind)
        if v is None:
            cls = {"text": widgets.TextView, "rich": widgets.RichView, "sheet": widgets.SheetView,
                   "pdf": widgets.PdfView}[kind]
            v = self.views[kind] = cls(self.container, self)
            if kind == "text":
                v.apply_theme(COLORS)
        return v

    def _activate(self, view) -> None:
        if self.active is view:
            return
        self._deactivate()
        if view is not None:
            view.frame.pack(fill="both", expand=True)
        self.active = view

    def _deactivate(self) -> None:
        if self.active is not None:
            self.active.frame.pack_forget()
            self.active = None
        self.msg.pack_forget()

    def _message(self, text: str) -> None:
        if self.active is not None:
            # free the heavy document (PDF handle, table widgets) of the previous file
            self.active.clear()
        self._deactivate()
        self.msg.configure(text=text)
        self.msg.pack(fill="both", expand=True)
        self.lbl_hits.configure(text="")

    def set_font_size(self, size: int) -> None:
        self.font_size = size
        v = self.views.get("text")
        if v is not None:
            v.set_font_size(size)

    def zoom(self, d: int) -> None:
        if self.active is not None:
            self.active.zoom_by(d)

    # ---- loading -----------------------------------------------------------------------

    def clear(self, msg: str = "") -> None:
        self.seq += 1
        self.path = ""
        self.lbl.configure(text="")
        self._message(msg)

    def show_file(self, path: str) -> None:
        """Parse ``path`` in a worker thread (big or odd files must not freeze the UI)."""
        if path == self.path:
            return
        self.seq += 1
        seq = self.seq
        self.path = path
        self.lbl.configure(text=os.path.basename(path))
        self._message(_("Loading..."))

        def work() -> None:
            self.q.put((seq, path, viewers.load(path)))

        threading.Thread(target=work, daemon=True).start()
        if not self.loading:
            self.loading = True
            self.after(40, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                seq, path, loaded = self.q.get_nowait()
                if seq == self.seq:
                    self.loading = False
                    self._display(path, loaded)
                    return
        except queue.Empty:
            pass
        self.after(40, self._poll)

    def _display(self, path: str, ld: "viewers.Loaded") -> None:
        name = os.path.basename(path)
        head = f"{name}  [{ld.label}]" if ld.label else name
        if ld.error:
            msgs = {
                "binary": _("This file has no text to preview (binary file)."),
                "folder": _("Folders have no preview."),
                "encrypted": _("This document is encrypted; its text cannot be previewed."),
                "unreadable": _("This file could not be read."),
                "nopdf": _("PDF preview needs the PyMuPDF library (pip install pymupdf)."),
            }
            self.lbl.configure(text=head)
            self._message(msgs.get(ld.error, ld.error))
            return
        view = self._view(ld.kind)
        try:
            self._activate(view)
            self.update_idletasks()
            view.show(ld.model)
        except ValueError as e:
            self.lbl.configure(text=head)
            self._message(msgs_for(str(e)))
            return
        except Exception as e:  # a viewer bug must not break the window
            self.lbl.configure(text=head)
            self._message(_("This file could not be read.") + f"\n({type(e).__name__}: {e})")
            return
        self.lbl.configure(text=head)
        self.highlight()

    # ---- matches ---------------------------------------------------------------------------

    def _regex(self):
        terms = preview.search_terms(self.app.var.get())
        if not terms:
            return None, False
        fl = self.app.flags()
        alts = [t if fl.regex else re.escape(t) for t in terms]
        try:
            return re.compile("|".join(f"(?:{a})" for a in alts), 0 if fl.case else re.IGNORECASE), True
        except re.error:
            return None, True

    def pdf_terms(self) -> list[str]:
        return [] if self.app.flags().regex else preview.search_terms(self.app.var.get())

    def case_sensitive(self) -> bool:
        return bool(self.app.flags().case)

    def highlight(self) -> None:
        v = self.active
        if v is None or not self.path:
            return
        rx, wanted = self._regex()
        n = v.highlight(rx)
        if isinstance(v, widgets.PdfView):
            self.lbl_hits.configure(text=_("Searching...") if rx is not None and v.pdf_pending() else "")
            return
        if n:
            self.step(1)
        else:
            self.lbl_hits.configure(text=_("No matches") if wanted else "")

    def render_done(self, view) -> None:
        """A time-sliced viewer finished drawing: now matches can be marked."""
        if view is self.active:
            self.highlight()

    def hits_ready(self, view) -> None:
        if view is self.active:
            if view.hits:
                self.step(1)
            else:
                self.lbl_hits.configure(text=_("No matches"))

    def step(self, d: int) -> None:
        v = self.active
        if v is None:
            return
        cur, total = v.step(d)
        self.lbl_hits.configure(text=f"{cur:,} / {total:,}" if total else "")


def msgs_for(code: str) -> str:
    return {
        "encrypted": _("This document is encrypted; its text cannot be previewed."),
        "binary": _("This file has no text to preview (binary file)."),
    }.get(code, _("This file could not be read.") + (f"\n({code})" if code else ""))
