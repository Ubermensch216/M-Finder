"""Dialogs: options, bookmark and filter organisers."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from typing import TYPE_CHECKING

from . import i18n, service, theme, winshell
from .engine import is_admin
from .i18n import _
from .theme import COLORS
from .tray import parse_hotkey

if TYPE_CHECKING:  # pragma: no cover
    from .gui import Controller


def ask_text(parent, title: str, prompt: str, initial: str = "") -> str | None:
    s = simpledialog.askstring(title, prompt, initialvalue=initial, parent=parent)
    return s.strip() if s and s.strip() else None


def center_on(dlg: tk.Toplevel, parent) -> None:
    """Place ``dlg`` at the centre of ``parent`` (kept inside the screen) and show it.

    The dialog must have been ``withdraw()``n when it was created so it never appears at the default position first.
    """
    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
    except tk.TclError:
        dlg.deiconify()
        return
    x = px + (pw - w) // 2
    y = py + (ph - h) // 2
    x = max(0, min(x, dlg.winfo_screenwidth() - w))
    y = max(0, min(y, dlg.winfo_screenheight() - h))
    dlg.geometry(f"+{x}+{y}")
    dlg.deiconify()


_MOD_KEYSYMS = {"Control_L": "ctrl", "Control_R": "ctrl", "Alt_L": "alt", "Alt_R": "alt", "Shift_L": "shift", "Shift_R": "shift"}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")
_MOD_STATE = (("ctrl", 0x0004), ("shift", 0x0001), ("alt", 0x20000))  # Tk event.state bits on Windows
_MOD_TITLE = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}


def hotkey_text(spec: str) -> str:
    """'ctrl+alt+f' -> 'Ctrl + Alt + F' (empty stays empty)."""
    mods, key = [], ""
    for part in (p.strip().lower() for p in spec.split("+") if p.strip()):
        if part == "control":
            part = "ctrl"
        if part in _MOD_TITLE:
            mods.append(part)
        else:
            key = part.upper() if len(part) <= 3 or part == "space" else part
            key = "Space" if part == "space" else key
    mods.sort(key=_MOD_ORDER.index)
    return " + ".join([_MOD_TITLE[m] for m in mods] + ([key] if key else []))


def hotkey_key_token(keysym: str) -> str | None:
    """Tk keysym -> the key part of a hotkey spec ('f', '5', 'f9', 'space'), None if it cannot be a global hotkey."""
    if len(keysym) == 1 and keysym.isalnum() and keysym.isascii():
        return keysym.lower()
    if keysym == "space":
        return "space"
    if keysym[:1] in ("F", "f") and keysym[1:].isdigit() and 1 <= int(keysym[1:]) <= 24:
        return keysym.lower()
    return None


class HotkeyBox(ttk.Frame):
    """Hotkey picker: click it, then press the keys one at a time (modifiers first, then one key).

    Esc cancels, Backspace takes back the last modifier. ``var`` always holds the spec ('ctrl+alt+f', '' = none).
    """

    def __init__(self, parent, var: tk.StringVar, label: str) -> None:
        super().__init__(parent)
        self.var = var
        self.capturing = False
        self.pending: list[str] = []
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text=label, width=28).pack(side="left")
        self.shown = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.shown, width=22, state="readonly", cursor="hand2")
        self.entry.pack(side="left")
        ttk.Button(row, text=_("Clear"), width=6, command=self.clear).pack(side="left", padx=6)
        self.hint = ttk.Label(self, foreground=COLORS["dim"], wraplength=440, justify="left")
        self.hint.pack(anchor="w", pady=(2, 0))
        self.entry.bind("<Button-1>", self._click)
        self.entry.bind("<FocusIn>", lambda e: self._begin())
        self.entry.bind("<FocusOut>", lambda e: self._end())
        self.entry.bind("<KeyPress>", self._key)
        self._end()

    def _click(self, _e) -> str:
        self.entry.focus_set()
        self._begin()
        return "break"

    def _begin(self) -> None:
        if not self.capturing:
            self.capturing, self.pending = True, []
        self._refresh()

    def _end(self) -> None:
        self.capturing, self.pending = False, []
        self._refresh()

    def _refresh(self, hint: str = "") -> None:
        if self.capturing:
            hint = hint or _("Press the keys one at a time: Ctrl / Alt / Shift, then a key. Esc cancels, Backspace undoes.")
            text = " + ".join(_MOD_TITLE[m] for m in self.pending) + " + " if self.pending else _("Press the keys...")
        else:
            text = hotkey_text(self.var.get()) or _("(none)")
        self.shown.set(text)
        self.hint.configure(text=hint)

    def clear(self) -> None:
        self.var.set("")
        self._end()

    def _key(self, e) -> str | None:
        if e.keysym == "Tab":
            return None  # let focus move on
        if not self.capturing:
            self._begin()
        if e.keysym == "Escape":
            self._end()
            self.winfo_toplevel().focus_set()
            return "break"
        if e.keysym == "BackSpace":
            if self.pending:
                self.pending.pop()
            self._refresh()
            return "break"
        mod = _MOD_KEYSYMS.get(e.keysym)
        if mod:
            if mod not in self.pending:
                self.pending.append(mod)
            self._refresh()
            return "break"
        tok = hotkey_key_token(e.keysym)
        mods = list(self.pending) + [m for m, bit in _MOD_STATE if e.state & bit and m not in self.pending]
        if tok is None:
            self._refresh(_("This key cannot be used."))
        elif not mods:
            self._refresh(_("A modifier key (Ctrl, Alt or Shift) is required."))
        else:
            mods.sort(key=_MOD_ORDER.index)
            self.var.set("+".join(mods + [tok]))
            self._end()
            self.winfo_toplevel().focus_set()
        return "break"


class SyntaxHelpDialog(tk.Toplevel):
    """Read-only reference of the search syntax: a heading per group, the term on the left, its meaning on the right."""

    COL = 230  # x position of the description column

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(_("Search syntax"))
        self.transient(parent)
        theme.apply_window(self)
        try:
            self.iconbitmap(parent.ctl.icon_path)
        except (tk.TclError, AttributeError):
            pass
        c = COLORS
        self.configure(bg=c["window"])
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Scrollbar(outer, orient="vertical")
        bar.pack(side="right", fill="y")
        t = self.text = tk.Text(outer, width=78, height=30, wrap="word", bd=0, highlightthickness=1,
                                highlightbackground=c["line"], highlightcolor=c["line"], bg=c["bg"], fg=c["fg"],
                                padx=14, pady=10, cursor="arrow", spacing1=2, spacing3=2,
                                selectbackground=c["sel"], selectforeground=c["fg"],
                                inactiveselectbackground=c["sel_inactive"], yscrollcommand=bar.set)
        t.pack(side="left", fill="both", expand=True)
        bar.configure(command=t.yview)
        base = tkfont.nametofont("TkDefaultFont").actual("family")
        t.tag_configure("h", font=(base, 11, "bold"), foreground=c["accent"], spacing1=14, spacing3=4)
        t.tag_configure("first", spacing1=0)
        t.tag_configure("row", lmargin1=12, lmargin2=self.COL, tabs=(self.COL,))
        t.tag_configure("code", font=("Consolas", 10), foreground=c["accent"])
        t.tag_configure("desc", font=(base, 10), foreground=c["fg"])
        t.tag_configure("note", font=(base, 9), foreground=c["dim"], lmargin1=12, lmargin2=12)
        for i, (title, rows, note) in enumerate(self.sections()):
            t.insert("end", title + "\n", ("h",) if i else ("h", "first"))
            for code, desc in rows:
                t.insert("end", code, ("row", "code"))
                t.insert("end", "\t" + desc + "\n", ("row", "desc"))
            if note:
                t.insert("end", note + "\n", ("note",))
        t.configure(state="disabled")
        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=8, pady=(0, 8))
        ok = ttk.Button(btn, text=_("OK"), command=self.destroy)
        ok.pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        ok.focus_set()
        center_on(self, parent)

    @staticmethod
    def sections():
        return [
            (_("Basics"), [
                ("a b", _("both a AND b")),
                ("a | b", _("a OR b")),
                ("!a", _("NOT a")),
                ("< a | b > c", _("grouping")),
                ('"a b"', _("exact phrase")),
                ("*  ?", _("wildcards (match the whole name)")),
                ("c:\\dir\\x", _("a backslash matches the full path")),
            ], ""),
            (_("Modifiers (affect the terms that follow)"), [
                ("case:  nocase:", _("match case / ignore case")),
                ("regex:  noregex:", _("regular expression on / off")),
                ("wholeword:  ww:", _("whole word")),
                ("path:  nopath:", _("match the full path / the name only")),
                ("wfn:", _("whole file name")),
            ], ""),
            (_("Functions"), [
                ("file:  folder:", _("files only / folders only")),
                ("ext:jpg;png", _("extensions")),
                ("empty:", _("empty folders")),
                ("attrib:H", _("attributes")),
                ("depth:<3", _("folder depth")),
                ("len:>20", _("name length")),
                ("parent:C:\\Windows", _("direct children of a folder")),
                ("infolder:C:\\Windows", _("direct children of a folder")),
                ("content:" + _("text"), _("file contents")),
            ], ""),
            (_("Size"), [
                ("size:>10mb", _("larger than 10 MB")),
                ("size:1kb..1mb", _("between 1 KB and 1 MB")),
                ("size:large", _("also: tiny, small, medium, huge, gigantic, empty")),
            ], ""),
            (_("Date"), [
                ("dm:today  dm:thisweek", _("modified today / this week")),
                ("dm:2024-05", _("modified in May 2024")),
                ("dm:>2024-01-01", _("modified after Jan 1, 2024")),
                ("dm:last3days", _("modified in the last 3 days")),
            ], _("dc: created date, da: accessed date")),
            (_("Macros"), [
                ("audio:  video:  pic:", _("media files")),
                ("doc:  zip:  exe:", _("documents, archives, programs")),
            ], ""),
            (_("Shortcuts"), [
                ("Ctrl+I", _("case sensitive")),
                ("Ctrl+B", _("whole word")),
                ("Ctrl+U", _("match path")),
                ("Ctrl+R", _("regular expression")),
                ("Ctrl+P", _("options")),
                ("F5", _("rebuild the index")),
                ("Ctrl+N", _("new window")),
                ("Ctrl+D", _("bookmark")),
                ("Ctrl+Shift+C", _("copy the full path")),
            ], ""),
        ]


class _ListOrganizer(tk.Toplevel):
    """Generic ordered list editor (rename / delete / move)."""

    def __init__(self, parent, title: str, items: list[dict], label: str, on_save) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent)
        theme.apply_window(self)
        self.items = [dict(i) for i in items]
        self.on_save = on_save
        self.lb = tk.Listbox(self, width=48, height=12, exportselection=False)
        self.lb.grid(row=0, column=0, rowspan=6, padx=8, pady=8)
        for b, (text, cmd) in enumerate(
            ((_("Rename..."), self.rename), (_("Edit search..."), self.edit), (_("Delete"), self.delete),
             (_("Move up"), lambda: self.move(-1)), (_("Move down"), lambda: self.move(1)))
        ):
            ttk.Button(self, text=text, command=cmd).grid(row=b, column=1, sticky="ew", padx=(0, 8), pady=2)
        bar = ttk.Frame(self)
        bar.grid(row=7, column=0, columnspan=2, sticky="e", padx=8, pady=(0, 8))
        ttk.Button(bar, text=_("Cancel"), command=self.destroy).pack(side="right")
        ttk.Button(bar, text=_("OK"), command=self.ok).pack(side="right", padx=6)
        self.refresh()
        center_on(self, parent)
        self.grab_set()

    def refresh(self, select: int | None = None) -> None:
        self.lb.delete(0, "end")
        for it in self.items:
            self.lb.insert("end", f"{it['name']}   —   {it.get('search', '')}")
        if select is not None and self.items:
            self.lb.selection_set(max(0, min(select, len(self.items) - 1)))

    def _sel(self) -> int | None:
        s = self.lb.curselection()
        return s[0] if s else None

    def rename(self) -> None:
        i = self._sel()
        if i is None:
            return
        name = ask_text(self, _("Rename"), _("Name:"), self.items[i]["name"])
        if name:
            self.items[i]["name"] = name
            self.refresh(i)

    def edit(self) -> None:
        i = self._sel()
        if i is None:
            return
        text = simpledialog.askstring(_("Edit search"), _("Search:"), initialvalue=self.items[i].get("search", ""), parent=self)
        if text is not None:
            self.items[i]["search"] = text
            self.refresh(i)

    def delete(self) -> None:
        i = self._sel()
        if i is not None:
            del self.items[i]
            self.refresh(i)

    def move(self, d: int) -> None:
        i = self._sel()
        if i is None or not 0 <= i + d < len(self.items):
            return
        self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
        self.refresh(i + d)

    def ok(self) -> None:
        self.on_save(self.items)
        self.destroy()


def organize_bookmarks(win) -> None:
    ctl = win.ctl
    _ListOrganizer(win, _("Organize Bookmarks"), ctl.settings.bookmarks, "bookmark", ctl.set_bookmarks)


def organize_filters(win) -> None:
    ctl = win.ctl
    _ListOrganizer(win, _("Organize Filters"), ctl.settings.filters, "filter", ctl.set_filters)


class OptionsDialog(tk.Toplevel):
    def __init__(self, win) -> None:
        super().__init__(win)
        self.withdraw()
        self.win, self.ctl = win, win.ctl
        s = self.s = self.ctl.settings
        self.title(_("M-Finder Options"))
        self.transient(win)
        self.resizable(False, False)
        theme.apply_window(self)
        try:
            self.iconbitmap(self.ctl.icon_path)
        except tk.TclError:
            pass
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.vars: dict[str, tk.Variable] = {}

        def tab(title):
            f = ttk.Frame(nb, padding=10)
            nb.add(f, text=title)
            return f

        def check(parent, key, text, **kw):
            v = self.vars[key] = tk.BooleanVar(value=getattr(s, key))
            ttk.Checkbutton(parent, text=text, variable=v).pack(anchor="w", **kw)

        def entry(parent, key, text, width=30, show="", cast=str):
            row = ttk.Frame(parent)
            row.pack(anchor="w", fill="x", pady=2)
            ttk.Label(row, text=text, width=28).pack(side="left")
            v = self.vars[key] = tk.StringVar(value=str(getattr(s, key)))
            ttk.Entry(row, textvariable=v, width=width, show=show).pack(side="left")

        # General
        g = tab(_("General"))
        check(g, "confirm_large_sort", _("Confirm large sorts (size / date of 100,000+ items)"))
        check(g, "show_folder_sizes", _("Show folder sizes"))
        check(g, "folder_sizes", _("Compute folder sizes in the background"))
        entry(g, "font_size", _("Font size:"), 6)
        entry(g, "content_max_mb", _("Content search max file size (MB):"), 6)
        entry(g, "db_path", _("Database file (blank = default):"), 34)
        row = ttk.Frame(g)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text=_("Language:"), width=28).pack(side="left")
        langs = i18n.available()
        self._lang_codes = ["auto"] + list(langs)
        self.cb_lang = ttk.Combobox(row, state="readonly", width=20, values=[_("System default")] + list(langs.values()))
        self.cb_lang.current(self._lang_codes.index(s.language) if s.language in self._lang_codes else 0)
        self.cb_lang.pack(side="left")
        row = ttk.Frame(g)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text=_("Theme:"), width=28).pack(side="left")
        self._themes = ["system", "light", "dark"]
        self.cb_theme = ttk.Combobox(row, state="readonly", width=20, values=[_("System default"), _("Light"), _("Dark")])
        self.cb_theme.current(self._themes.index(s.theme) if s.theme in self._themes else 0)
        self.cb_theme.pack(side="left")
        ttk.Label(g, text=_("Language and theme changes apply after restarting M-Finder."), foreground=COLORS["dim"]).pack(anchor="w", pady=6)

        # Indexes
        ix = tab(_("Indexes"))
        ttk.Label(ix, text=_("Folders and drives to index (empty = all fixed drives):")).pack(anchor="w")
        self.lb_roots = self._listbox(ix, s.roots, _("Add Folder..."), self._add_dir)
        ttk.Label(ix, text=_("File lists (.efu / .csv / .txt):")).pack(anchor="w", pady=(6, 0))
        self.lb_lists = self._listbox(ix, s.filelists, _("Add File List..."), self._add_list)
        check(ix, "use_mft", _("Use the NTFS MFT for fast indexing (needs administrator or the service)"), pady=(8, 0))
        check(ix, "monitor", _("Monitor NTFS volumes with the USN journal"))
        check(ix, "monitor_folders", _("Monitor other folders with change notifications"))
        check(ix, "use_service", _("Use the M-Finder service when not running as administrator"))
        sv = ttk.Frame(ix)
        sv.pack(anchor="w", pady=(6, 0))
        self.lbl_service = ttk.Label(sv, text="")
        self.lbl_service.pack(side="left", padx=(0, 8))
        for text, fn in ((_("Install"), self._svc_install), (_("Uninstall"), lambda: self._svc(service.uninstall)),
                         (_("Start"), lambda: self._svc(service.start_service)), (_("Stop"), lambda: self._svc(service.stop_service))):
            ttk.Button(sv, text=text, width=9, command=fn).pack(side="left", padx=1)
        self._svc_refresh()

        # Exclude
        ex = tab(_("Exclude"))
        check(ex, "exclude_hidden", _("Exclude hidden files and folders"))
        check(ex, "exclude_system", _("Exclude system files and folders"))
        ttk.Label(ex, text=_("Exclude folders:")).pack(anchor="w", pady=(8, 0))
        self.lb_ex = self._listbox(ex, s.exclude_folders, _("Add Folder..."), self._add_ex)
        ttk.Label(ex, text=_("Exclude files (wildcards, separated by ;):")).pack(anchor="w", pady=(8, 0))
        self.v_pat = tk.StringVar(value=";".join(s.exclude_patterns))
        ttk.Entry(ex, textvariable=self.v_pat, width=62).pack(anchor="w")

        # Servers
        sv = tab(_("Servers"))
        check(sv, "ipc_enabled", _("Enable search IPC (command line / other programs, current user only)"))
        ttk.Label(sv, text=_("HTTP server"), font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        check(sv, "http_enabled", _("Enable HTTP server on startup"))
        for k, t, sh in (("http_bind", _("Bind address:"), ""), ("http_port", _("Port:"), ""), ("http_user", _("Username:"), ""), ("http_password", _("Password:"), "*")):
            entry(sv, k, t, 24, sh)
        ttk.Label(sv, text=_("ETP / FTP server"), font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        check(sv, "etp_enabled", _("Enable ETP/FTP server on startup"))
        check(sv, "etp_allow_download", _("Allow file downloads (otherwise browse/search only)"))
        for k, t, sh in (("etp_bind", _("Bind address:"), ""), ("etp_port", _("Port:"), ""), ("etp_user", _("Username:"), ""), ("etp_password", _("Password:"), "*")):
            entry(sv, k, t, 24, sh)
        ttk.Label(sv, text=_("Bind to 0.0.0.0 only with a password. HTTP and FTP send credentials in clear text."), foreground=COLORS["dim"]).pack(anchor="w", pady=4)

        # Background
        bg = tab(_("Background"))
        check(bg, "tray_icon", _("Show icon in the notification area"))
        check(bg, "run_in_background", _("Keep running in the tray when the window is closed"))
        check(bg, "start_minimized", _("Start minimized"))
        check(bg, "single_instance", _("Only allow one M-Finder process (reuse the running one)"))
        self.v_startup = tk.BooleanVar(value=winshell.is_run_on_startup() if os.name == "nt" else False)
        ttk.Checkbutton(bg, text=_("Run M-Finder when Windows starts"), variable=self.v_startup).pack(anchor="w")
        self.vars["global_hotkey"] = tk.StringVar(value=s.global_hotkey)
        HotkeyBox(bg, self.vars["global_hotkey"], _("Global hotkey:")).pack(anchor="w", fill="x", pady=2)
        ttk.Label(bg, text=_("The hotkey and tray icon need the tray icon option."), foreground=COLORS["dim"]).pack(anchor="w", pady=4)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text=_("Cancel"), command=self.destroy).pack(side="right")
        ttk.Button(bar, text=_("OK"), command=self._ok).pack(side="right", padx=6)
        center_on(self, win)
        self.grab_set()

    # -- helpers --
    def _listbox(self, parent, items, add_text, add_cmd) -> tk.Listbox:
        lb = tk.Listbox(parent, height=4, width=64)
        lb.pack(fill="x", pady=2)
        for it in items:
            lb.insert("end", it)
        bf = ttk.Frame(parent)
        bf.pack(anchor="w")
        ttk.Button(bf, text=add_text, command=lambda: add_cmd(lb)).pack(side="left")
        ttk.Button(bf, text=_("Remove"), command=lambda: [lb.delete(i) for i in reversed(lb.curselection())]).pack(side="left", padx=6)
        return lb

    def _add_dir(self, lb) -> None:
        d = filedialog.askdirectory(parent=self, title=_("Select folder to index"))
        if d:
            lb.insert("end", os.path.normpath(d))

    _add_ex = _add_dir

    def _add_list(self, lb) -> None:
        f = filedialog.askopenfilename(parent=self, filetypes=[("File list", "*.efu *.csv *.txt"), ("All", "*.*")])
        if f:
            lb.insert("end", os.path.normpath(f))

    def _svc_refresh(self) -> None:
        try:
            st = service.status()
        except OSError as e:
            st = str(e)
        self.lbl_service.configure(text=_("Service:") + " " + st)

    def _svc(self, fn) -> None:
        try:
            fn()
        except OSError as e:
            messagebox.showerror(_("M-Finder Service"), f"{e}", parent=self)
        self.after(800, self._svc_refresh)

    def _svc_install(self) -> None:
        if not is_admin():
            messagebox.showinfo(_("M-Finder Service"), _("Installing the service needs administrator rights. Use Tools > Restart as Administrator first."), parent=self)
            return
        self._svc(service.install)

    def _ok(self) -> None:
        s = self.s
        try:
            for k, v in self.vars.items():
                val = v.get()
                if k in ("http_port", "etp_port", "font_size", "content_max_mb"):
                    val = int(val)
                    if (k.endswith("port") and not 1 <= val <= 65535) or (k in ("font_size",) and not 6 <= val <= 32) or (k == "content_max_mb" and val < 1):
                        raise ValueError(k)
                setattr(s, k, val)
        except ValueError as e:
            messagebox.showerror(_("Options"), _("Invalid value for") + f" {e}", parent=self)
            return
        hk = s.global_hotkey.strip()
        if hk and not parse_hotkey(hk):
            messagebox.showerror(_("Options"), _("Invalid hotkey. Use e.g. ctrl+alt+f or win+f9."), parent=self)
            return
        def _index_config(cfg):
            return (
                list(cfg.roots), list(cfg.filelists), cfg.use_mft, cfg.exclude_hidden, cfg.exclude_system,
                list(cfg.exclude_folders), list(cfg.exclude_patterns), cfg.db_path, cfg.use_service,
            )

        old = _index_config(s)
        s.roots, s.filelists = list(self.lb_roots.get(0, "end")), list(self.lb_lists.get(0, "end"))
        s.exclude_folders = list(self.lb_ex.get(0, "end"))
        s.exclude_patterns = [p.strip() for p in self.v_pat.get().split(";") if p.strip()]
        s.language = self._lang_codes[self.cb_lang.current()]
        s.theme = self._themes[self.cb_theme.current()]
        s.global_hotkey = hk
        if os.name == "nt":
            try:
                winshell.set_run_on_startup(self.v_startup.get())
                s.run_on_startup = self.v_startup.get()
            except OSError as e:
                messagebox.showerror(_("Options"), str(e), parent=self)
        try:
            s.save()
        except OSError as e:
            messagebox.showerror(_("Options"), f"{e}", parent=self)
        changed = old != _index_config(s)
        self.destroy()
        self.ctl.settings_changed()
        if changed and messagebox.askyesno(_("Options"), _("Indexing options changed. Rebuild the index now?"), parent=self.win):
            self.ctl.rebuild()
