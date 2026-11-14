"""Thin Windows shell helpers (open, reveal in Explorer, recycle bin)."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

FO_DELETE = 0x3
FOF_ALLOWUNDO = 0x40


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def open_item(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening the user's own selection is the point
    else:
        subprocess.Popen(["xdg-open", path])


def reveal(path: str) -> None:
    """Open the containing folder in Explorer with the item selected."""
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def recycle(paths: list[str]) -> bool:
    """Move paths to the Recycle Bin (Explorer shows its own confirmation)."""
    if sys.platform != "win32" or not paths:
        return False
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.fFlags = FOF_ALLOWUNDO
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


# ---- startup registration and elevation ------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "M-Finder"


def frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source_prefix() -> str:
    code = f"import sys; sys.path.insert(0, r'{package_root()}'); from mfinder.cli import main; main(sys.argv[1:])"
    return f'-c "{code}"'


def launch_command(args: str = "", windowless: bool = True) -> str:
    """Command line that starts M-Finder (the built .exe, or this source checkout)."""
    exe = sys.executable
    if frozen():
        return f'"{exe}" {args}'.rstrip()
    if windowless and exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        exe = w if os.path.exists(w) else exe
    return f'"{exe}" {_source_prefix()} {args}'.rstrip()


def set_run_on_startup(enabled: bool) -> None:
    """Add/remove the per-user Run entry (starts minimised in the tray)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, launch_command("gui --minimized"))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def is_run_on_startup() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def relaunch_as_admin(args: str = "gui") -> bool:
    """Start a new elevated instance (shows the UAC prompt). Returns False if declined."""
    params = args if frozen() else f"{_source_prefix()} {args}"
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32
