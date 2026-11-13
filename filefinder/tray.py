"""Notification-area icon and global hotkey, driven by a private native window thread."""

from __future__ import annotations

import ctypes
import os
import queue
import struct
import sys
import threading
import zlib
from ctypes import wintypes
from typing import Callable, Optional

WM_USER = 0x400
WM_APP = 0x8000
WM_CALLBACK = WM_USER + 1
WM_HOTKEY = 0x312
WM_DESTROY = 0x2
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x202, 0x203, 0x205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MOD = {"alt": 1, "ctrl": 2, "control": 2, "shift": 4, "win": 8}
MOD_NOREPEAT = 0x4000
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2
MF_STRING, MF_SEPARATOR = 0, 0x800

if sys.platform == "win32":
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _sh = ctypes.WinDLL("shell32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                    ("uTimeout", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]

    _u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.DefWindowProcW.restype = LRESULT
    _u32.CreateWindowExW.restype = wintypes.HWND
    _u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                     wintypes.HINSTANCE, wintypes.LPVOID]
    _u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.CreatePopupMenu.restype = wintypes.HMENU
    _u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    _u32.DestroyMenu.argtypes = [wintypes.HMENU]
    _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u32.LoadImageW.restype = wintypes.HANDLE
    _u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u32.DestroyWindow.argtypes = [wintypes.HWND]
    _u32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _k32.GetModuleHandleW.restype = wintypes.HMODULE
    _k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


# ---- icon -------------------------------------------------------------------


def make_icon_png(size: int = 32) -> bytes:
    """A blue magnifying glass, rendered with 3x3 supersampling, as PNG bytes."""
    ss = 3
    cx = cy = size * 0.42
    r_out, r_in = size * 0.30, size * 0.20
    h0, h1, hw = (cx + r_out * 0.7, cy + r_out * 0.7), (size * 0.92, size * 0.92), size * 0.07
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    px, py = x + (sx + 0.5) / ss, y + (sy + 0.5) / ss
                    d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                    ring = r_in <= d <= r_out
                    # distance to handle segment
                    vx, vy = h1[0] - h0[0], h1[1] - h0[1]
                    t = max(0.0, min(1.0, ((px - h0[0]) * vx + (py - h0[1]) * vy) / (vx * vx + vy * vy)))
                    dh = ((px - h0[0] - t * vx) ** 2 + (py - h0[1] - t * vy) ** 2) ** 0.5
                    hit += ring or dh <= hw
            a = int(255 * hit / (ss * ss))
            row += bytes((0x1F, 0x6F, 0xD6, a))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def ensure_icon_file(directory: str) -> str:
    """Write (once) a PNG-compressed .ico and return its path."""
    path = os.path.join(directory, "filefinder.ico")
    if not os.path.exists(path):
        png = make_icon_png(32)
        hdr = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22)
        os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as f:
            f.write(hdr + png)
    return path


# ---- hotkey parsing ------------------------------------------------------------


def parse_hotkey(spec: str) -> Optional[tuple[int, int]]:
    """'ctrl+alt+f' -> (modifiers, virtual key); None if invalid."""
    mods, vk = 0, 0
    for part in [p.strip().lower() for p in spec.split("+") if p.strip()]:
        if part in MOD:
            mods |= MOD[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif part == "space":
            vk = 0x20
        else:
            return None
    return (mods, vk) if vk and mods else None


# ---- the tray ------------------------------------------------------------------


class Tray:
    """Runs a hidden window on its own thread. Calls ``on_event(name)`` for
    'show', 'hotkey' or a menu command id (e.g. 'new', 'rebuild', 'exit')."""

    def __init__(self, tooltip: str, icon_path: str, menu_items: list[tuple[str, str]], on_event: Callable[[str], None]) -> None:
        self.tooltip, self.icon_path, self.menu_items, self.on_event = tooltip, icon_path, menu_items, on_event
        self.hwnd = None
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._icon = None
        self._taskbar_msg = 0
        self._proc = None
        self.hotkey_ok = True

    def start(self, timeout: float = 3.0) -> bool:
        self._thread.start()
        return self._ready.wait(timeout) and self.hwnd is not None

    def set_hotkey(self, spec: str) -> bool:
        """Register (or with '' clear) the global hotkey. Returns False if invalid/taken."""
        done = threading.Event()
        res = [True]
        self._cmds.put(("hotkey", spec, res, done))
        _u32.PostMessageW(self.hwnd, WM_APP, 0, 0)
        done.wait(2)
        return res[0]

    def stop(self) -> None:
        if self.hwnd:
            self._cmds.put(("quit",))
            _u32.PostMessageW(self.hwnd, WM_APP, 0, 0)
            self._thread.join(timeout=2)

    # -- thread --
    def _nid(self) -> "NOTIFYICONDATAW":
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd, n.uID, n.uFlags = self.hwnd, 1, NIF_MESSAGE | NIF_ICON | NIF_TIP
        n.uCallbackMessage, n.hIcon, n.szTip = WM_CALLBACK, self._icon, self.tooltip[:127]
        return n

    def _menu(self) -> None:
        m = _u32.CreatePopupMenu()
        for i, (cid, label) in enumerate(self.menu_items, 1):
            if cid == "-":
                _u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
            else:
                _u32.AppendMenuW(m, MF_STRING, i, label)
        pt = wintypes.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))
        _u32.SetForegroundWindow(self.hwnd)  # required so the menu closes when clicking elsewhere
        cmd = _u32.TrackPopupMenu(m, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        _u32.DestroyMenu(m)
        if cmd:
            self.on_event(self.menu_items[cmd - 1][0])

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_CALLBACK:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_event("show")
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_HOTKEY:
                self.on_event("hotkey")
                return 0
            if msg == WM_APP:
                self._drain(hwnd)
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:  # Explorer restarted
                _sh.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))
                return 0
            if msg == WM_DESTROY:
                _u32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - never let an exception unwind into the OS callback
            pass
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _drain(self, hwnd) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "hotkey":
                _u32.UnregisterHotKey(hwnd, 1)
                spec, res, done = cmd[1], cmd[2], cmd[3]
                if spec:
                    parsed = parse_hotkey(spec)
                    res[0] = bool(parsed) and bool(_u32.RegisterHotKey(hwnd, 1, parsed[0] | MOD_NOREPEAT, parsed[1]))
                done.set()
            elif cmd[0] == "quit":
                _u32.UnregisterHotKey(hwnd, 1)
                _sh.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
                _u32.DestroyWindow(hwnd)

    def _run(self) -> None:
        hinst = _k32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        cls = WNDCLASSW()
        cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self._proc, hinst, "FileFinderTrayWindow"
        _u32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = _u32.CreateWindowExW(0x80, cls.lpszClassName, "FileFinder", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            self._ready.set()
            return
        self._icon = _u32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, LR_LOADFROMFILE|LR_DEFAULTSIZE
        self._taskbar_msg = _u32.RegisterWindowMessageW("TaskbarCreated")
        _sh.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))
        self._ready.set()
        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.DispatchMessageW(ctypes.byref(msg))
