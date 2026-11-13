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


def apply(root: tk.Misc, name: str) -> str:
    """Select a palette and style ttk/tk widgets. Call before creating windows."""
    global current
    current = resolve(name)
    COLORS.clear()
    COLORS.update(PALETTES[current])
    c = COLORS
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
        style.configure("TNotebook", background=c["window"])
        style.configure("TNotebook.Tab", background=c["button"], foreground=c["fg"], padding=(8, 3))
        style.map("TNotebook.Tab", background=[("selected", c["window"])])
        style.configure("Vertical.TScrollbar", background=c["button"], arrowcolor=c["fg"])
        root.configure(bg=c["window"])
        for opt, val in (
            ("*Menu.background", c["window"]), ("*Menu.foreground", c["fg"]), ("*Menu.activeBackground", c["sel"]),
            ("*Menu.activeForeground", c["fg"]), ("*Listbox.background", c["entry_bg"]), ("*Listbox.foreground", c["fg"]),
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
