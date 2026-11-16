"""Colour themes (light / dark / follow Windows) for the Tk GUI."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

PALETTES = {
    "light": {
        "bg": "#ffffff", "fg": "#000000", "sel": "#cce8ff", "sel_inactive": "#e5e5e5", "header_bg": "#f0f0f0",
        "line": "#d0d0d0", "folder": "#e8b83c", "file": "#9db7d6", "dim": "#666666", "window": "#f0f0f0",
        "entry_bg": "#ffffff", "button": "#e1e1e1", "accent": "#0078d4",
    },
    "dark": {
        "bg": "#1e1e1e", "fg": "#e6e6e6", "sel": "#264f78", "sel_inactive": "#37373d", "header_bg": "#2d2d30",
        "line": "#3f3f46", "folder": "#d7a62e", "file": "#6c8fb8", "dim": "#9a9a9a", "window": "#252526",
        "entry_bg": "#2b2b2b", "button": "#3a3a3d", "accent": "#3794ff",
    },
}
COLORS: dict[str, str] = dict(PALETTES["light"])
current = "light"


def system_theme() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except OSError:
            pass
    return "light"


def resolve(name: str) -> str:
    return system_theme() if name == "system" else (name if name in PALETTES else "light")


def _set_win32_app_dark_mode(dark: bool) -> None:
    """Enable/disable Win32 dark mode for native menus and common controls."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ux = ctypes.WinDLL("uxtheme")
        set_mode = ux[135]  # SetPreferredAppMode
        set_mode.argtypes = [ctypes.c_int]
        set_mode.restype = ctypes.c_int
        set_mode(2 if dark else 3)  # 2 = ForceDark, 3 = ForceLight
        flush = ux[136]  # FlushMenuThemes
        flush.argtypes = []
        flush.restype = None
        flush()
    except (OSError, AttributeError, ValueError):
        pass


def apply_window(win: tk.Misc) -> None:
    """Apply native window frame theming (dark title bar on Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        val = ctypes.c_int(1 if current == "dark" else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / Win10 20H1+, older Win10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except (OSError, AttributeError):
        pass


def apply(root: tk.Misc, name: str) -> str:
    """Select a palette and style ttk/tk widgets. Call before creating windows."""
    global current
    current = resolve(name)
    COLORS.clear()
    COLORS.update(PALETTES[current])
    c = COLORS
    _set_win32_app_dark_mode(current == "dark")
    style = ttk.Style(root)
    if current == "dark":
        style.theme_use("clam")
        style.configure(".", background=c["window"], foreground=c["fg"], fieldbackground=c["entry_bg"], bordercolor=c["line"],
                        lightcolor=c["window"], darkcolor=c["window"], troughcolor=c["window"], insertcolor=c["fg"])
        style.map(".", foreground=[("disabled", c["dim"])])
        style.configure("TEntry", fieldbackground=c["entry_bg"], foreground=c["fg"])
        style.configure("TCombobox", fieldbackground=c["entry_bg"], foreground=c["fg"], arrowcolor=c["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", c["entry_bg"])], foreground=[("readonly", c["fg"])])
        style.configure("TSpinbox", fieldbackground=c["entry_bg"], foreground=c["fg"], arrowcolor=c["fg"])
        style.configure("TButton", background=c["button"], foreground=c["fg"])
        style.map("TButton", background=[("active", c["sel"])])
        style.configure("TCheckbutton", indicatorbackground=c["entry_bg"], indicatorforeground=c["fg"])
        style.map("TCheckbutton",
                  indicatorbackground=[("pressed", c["sel"]), ("disabled", c["window"])],
                  indicatorforeground=[("disabled", c["dim"])])
        style.configure("TRadiobutton", indicatorbackground=c["entry_bg"], indicatorforeground=c["fg"])
        style.map("TRadiobutton",
                  indicatorbackground=[("pressed", c["sel"]), ("disabled", c["window"])],
                  indicatorforeground=[("disabled", c["dim"])])
        style.configure("TNotebook", background=c["window"])
        style.configure("TNotebook.Tab", background=c["button"], foreground=c["fg"], padding=(8, 3))
        style.map("TNotebook.Tab", background=[("selected", c["window"])])
        style.configure("Vertical.TScrollbar", background=c["button"], arrowcolor=c["fg"])
        root.configure(bg=c["window"])
        for opt, val in (
            ("*Menu.background", c["window"]), ("*Menu.foreground", c["fg"]), ("*Menu.activeBackground", c["sel"]),
            ("*Menu.activeForeground", c["fg"]), ("*Menu.selectColor", c["fg"]),
            ("*Menu.disabledForeground", c["dim"]),
            ("*Listbox.background", c["entry_bg"]), ("*Listbox.foreground", c["fg"]),
            ("*Listbox.selectBackground", c["sel"]), ("*Listbox.selectForeground", c["fg"]),
            ("*Toplevel.background", c["window"]),
        ):
            root.option_add(opt, val)
    else:
        try:
            style.theme_use("vista" if sys.platform == "win32" else "default")
        except tk.TclError:
            pass
    return current


class MenuBar(tk.Frame):
    """Themed horizontal menu bar for dark mode, where the native Win32 menu bar stays white."""

    def __init__(self, master: tk.Misc) -> None:
        c = COLORS
        super().__init__(master, bg=c["window"], bd=0, highlightthickness=0)
        self._row = tk.Frame(self, bg=c["window"], bd=0, highlightthickness=0)
        self._row.pack(fill="x")
        tk.Frame(self, bg=c["line"], height=1, bd=0, highlightthickness=0).pack(fill="x")
        self.menus: list[tuple[tk.Menubutton, tk.Menu]] = []

    def add_menu(self, label: str) -> tk.Menu:
        c = COLORS
        btn = tk.Menubutton(
            self._row, text=label, direction="below", indicatoron=False,
            bd=0, relief="flat", highlightthickness=0, takefocus=0,
            padx=8, pady=3, font="TkMenuFont",
            bg=c["window"], fg=c["fg"],
            activebackground=c["sel"], activeforeground=c["fg"],
            disabledforeground=c["dim"],
        )
        m = tk.Menu(btn, tearoff=0)
        btn.configure(menu=m)
        btn.pack(side="left")
        self.menus.append((btn, m))
        return m
