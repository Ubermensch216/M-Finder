"""Dialogs: options, bookmark and filter organisers."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
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


class _ListOrganizer(tk.Toplevel):
    """Generic ordered list editor (rename / delete / move)."""

    def __init__(self, parent, title: str, items: list[dict], label: str, on_save) -> None:
        super().__init__(parent)
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
        entry(bg, "global_hotkey", _("Global hotkey (e.g. ctrl+alt+f):"), 20)
        ttk.Label(bg, text=_("The hotkey and tray icon need the tray icon option."), foreground=COLORS["dim"]).pack(anchor="w", pady=4)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text=_("Cancel"), command=self.destroy).pack(side="right")
        ttk.Button(bar, text=_("OK"), command=self._ok).pack(side="right", padx=6)
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
