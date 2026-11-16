"""Live change monitoring for folder/non-NTFS indexes via ReadDirectoryChangesW."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional

from .index import FILE_ATTRIBUTE_DIRECTORY, Index
from .scanner import FILE_ATTRIBUTE_REPARSE_POINT, Excludes, scan_into

FILE_LIST_DIRECTORY = 0x1
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FILTER = 0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0x40  # names, attrs, size, write, creation
ACTION_ADDED, ACTION_REMOVED, ACTION_MODIFIED, ACTION_RENAMED_OLD, ACTION_RENAMED_NEW = 1, 2, 3, 4, 5
ERROR_OPERATION_ABORTED = 995

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadDirectoryChangesW.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p,
    ]
    _k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    _k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
_INVALID = ctypes.c_void_p(-1).value


def parse_notifications(data: bytes) -> list[tuple[int, str]]:
    """FILE_NOTIFY_INFORMATION chain -> [(action, relative path)]."""
    out, pos = [], 0
    while pos + 12 <= len(data):
        nxt = int.from_bytes(data[pos : pos + 4], "little")
        action = int.from_bytes(data[pos + 4 : pos + 8], "little")
        nlen = int.from_bytes(data[pos + 8 : pos + 12], "little")
        name = data[pos + 12 : pos + 12 + nlen].decode("utf-16-le", "replace")
        out.append((action, name))
        if not nxt:
            break
        pos += nxt
    return out


class ChangeApplier:
    """Turns path-level change events into index edits (OS independent, unit-testable)."""

    def __init__(self, index: Index, vol: int, excludes: Optional[Excludes] = None) -> None:
        self.index = index
        self.vol = vol
        self.excludes = excludes or Excludes()
        self.root = index.roots[vol]["path"].rstrip("\\/")

    def apply(self, action: int, rel: str) -> bool:
        idx = self.index
        full = self.root + "\\" + rel
        with idx.lock:
            existing = idx.resolve(full)
            if action in (ACTION_REMOVED, ACTION_RENAMED_OLD):
                return existing is not None and idx.remove_tree(existing) > 0
            if action == ACTION_MODIFIED:
                if existing is None:
                    return False
                idx.invalidate_meta(existing)
                idx.touch_meta()
                return True
            if action in (ACTION_ADDED, ACTION_RENAMED_NEW):
                if existing is not None:
                    idx.invalidate_meta(existing)
                    idx.touch_meta()
                    return True
                return self._add(full)
        return False

    def _add(self, full: str) -> bool:
        idx, ex = self.index, self.excludes
        parent = idx.resolve(os.path.dirname(full))
        if parent is None:
            return False
        try:
            st = os.lstat(full)
        except OSError:
            return False
        attrs = getattr(st, "st_file_attributes", 0)
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        if is_dir:
            attrs |= FILE_ATTRIBUTE_DIRECTORY
        name = os.path.basename(full)
        if ex.attr_excluded(attrs) or ex.name_excluded(name) or (is_dir and ex.folder_excluded(full)):
            return False
        i = idx.add(
            name, parent, attrs, -1 if is_dir else st.st_size, st.st_mtime or 0.001, 0, self.vol,
            getattr(st, "st_birthtime", st.st_ctime), st.st_atime,
        )
        if is_dir and not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            scan_into(idx, full, i, self.vol, ex)  # contents may already exist (moved-in folder)
        idx.touch()
        return True


class FolderWatcher(threading.Thread):
    def __init__(
        self,
        index: Index,
        vol: int,
        excludes: Optional[Excludes] = None,
        on_stale: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(daemon=True, name=f"watch-{index.roots[vol]['path']}")
        self.applier = ChangeApplier(index, vol, excludes)
        self.vol = vol
        self.on_stale = on_stale
        self._h = None
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()
        if self._h:
            _k32.CancelIoEx(self._h, None)

    def run(self) -> None:
        root = self.applier.index.roots[self.vol]["path"]
        if not root.endswith("\\"):
            root += "\\"
        h = _k32.CreateFileW(root, FILE_LIST_DIRECTORY, 7, None, 3, FILE_FLAG_BACKUP_SEMANTICS, None)
        if h in (None, _INVALID):
            return
        self._h = h
        buf = ctypes.create_string_buffer(1 << 16)
        ret = wintypes.DWORD(0)
        try:
            while not self._stopping.is_set():
                ok = _k32.ReadDirectoryChangesW(h, buf, len(buf), True, NOTIFY_FILTER, ctypes.byref(ret), None, None)
                if not ok:
                    if not self._stopping.is_set() and ctypes.get_last_error() != ERROR_OPERATION_ABORTED and self.on_stale:
                        self.on_stale(self.vol)
                    return
                if ret.value == 0:  # buffer overflowed: events were lost, only a rescan is safe
                    if self.on_stale:
                        self.on_stale(self.vol)
                    return
                for action, rel in parse_notifications(buf.raw[: ret.value]):
                    self.applier.apply(action, rel)
        finally:
            self._h = None
            _k32.CloseHandle(h)
