"""NTFS fast indexing: MFT enumeration and USN change-journal monitoring.

Both need a volume handle (``\\\\.\\C:``), which Windows only grants to
administrators. A background service or elevated process can open the volume,
and callers fall back to :mod:`scanner` on ``PermissionError``.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Iterator, Optional

from .index import FILE_ATTRIBUTE_DIRECTORY, Index
from .scanner import Excludes

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_CREATE_USN_JOURNAL = 0x000900E7

USN_REASON_DATA_OVERWRITE = 0x1
USN_REASON_DATA_EXTEND = 0x2
USN_REASON_DATA_TRUNCATION = 0x4
USN_REASON_FILE_CREATE = 0x100
USN_REASON_FILE_DELETE = 0x200
USN_REASON_RENAME_OLD_NAME = 0x1000
USN_REASON_RENAME_NEW_NAME = 0x2000
USN_REASON_BASIC_INFO_CHANGE = 0x8000
USN_REASON_CLOSE = 0x80000000

ERROR_ACCESS_DENIED = 5
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181

USN_HEADER = struct.Struct("<IHHQQqqIIIIHH")  # 60 bytes, name follows
MFT_RECORD_MASK = 0x0000FFFFFFFFFFFF
ROOT_RECORD = 5
FIRST_USER_RECORD = 24  # records below this (other than root) are NTFS metafiles


def parse_usn_records(buf: bytes, offset: int = 0) -> Iterator[tuple]:
    """Yield (frn, parent_frn, usn, reason, attrs, name) from a USN_RECORD_V2 stream."""
    n = len(buf)
    while offset + USN_HEADER.size <= n:
        (length, major, _minor, frn, parent, usn, _ts, reason, _src, _sec, attrs, name_len, name_off) = (
            USN_HEADER.unpack_from(buf, offset)
        )
        if length < USN_HEADER.size or offset + length > n:
            break
        if major == 2:
            raw = buf[offset + name_off : offset + name_off + name_len]
            yield frn, parent, usn, reason, attrs, raw.decode("utf-16-le", "replace")
        offset += (length + 7) & ~7


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT
    _k32.GetLogicalDrives.restype = wintypes.DWORD

_INVALID = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
SHARE_RW = 0x1 | 0x2 | 0x4


def fixed_drives() -> list[str]:
    """Roots of all fixed (non-removable, non-network) drives, e.g. ['C:\\\\', 'D:\\\\']."""
    if sys.platform != "win32":
        return []
    mask = _k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if _k32.GetDriveTypeW(root) == 3:
                out.append(root)
    return out


def filesystem_name(root: str) -> str:
    if sys.platform != "win32":
        return ""
    fs = ctypes.create_unicode_buffer(32)
    ok = _k32.GetVolumeInformationW(root, None, 0, None, None, None, fs, len(fs))
    return fs.value if ok else ""


def is_ntfs_root(path: str) -> bool:
    p = path.rstrip("\\/")
    return len(p) == 2 and p[1] == ":" and filesystem_name(p + "\\").upper() == "NTFS"


def open_volume(root: str) -> int:
    letter = root.rstrip("\\/")[:2]
    h = _k32.CreateFileW(f"\\\\.\\{letter}", GENERIC_READ, SHARE_RW, None, 3, 0, None)
    if h in (None, _INVALID):
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            raise PermissionError(f"Access denied opening volume {letter} (run as administrator)")
        raise OSError(err, f"Cannot open volume {letter}: error {err}")
    return h


def close_handle(h: int) -> None:
    _k32.CloseHandle(h)


def _ioctl(h: int, code: int, inbuf: bytes, out_size: int):
    out = ctypes.create_string_buffer(out_size)
    ret = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(h, code, inbuf, len(inbuf), out, out_size, ctypes.byref(ret), None)
    if not ok:
        return None, ctypes.get_last_error()
    return out.raw[: ret.value], 0


def query_journal(h: int) -> dict:
    data, err = _ioctl(h, FSCTL_QUERY_USN_JOURNAL, b"", 80)
    if data is None:
        raise OSError(err, f"Cannot query USN journal: error {err}")
    jid, first, nxt, lowest, maxusn, maxsize, delta = struct.unpack_from("<QqqqqQQ", data)
    return {"journal_id": jid, "first_usn": first, "next_usn": nxt, "lowest_valid_usn": lowest}


def create_journal(h: int, max_size: int = 32 << 20, delta: int = 8 << 20) -> None:
    _, err = _ioctl(h, FSCTL_CREATE_USN_JOURNAL, struct.pack("<QQ", max_size, delta), 0)
    if err:
        raise OSError(err, f"Cannot create USN journal: error {err}")


def enum_mft(h: int, high_usn: int) -> Iterator[tuple]:
    """Yield (frn, parent_frn, attrs, name) for every file on the volume."""
    start = 0
    while True:
        data, err = _ioctl(h, FSCTL_ENUM_USN_DATA, struct.pack("<Qqq", start, 0, high_usn), 1 << 20)
        if data is None:
            if err == ERROR_HANDLE_EOF:
                return
            raise OSError(err, f"Cannot enumerate MFT: error {err}")
        if len(data) <= 8:
            return
        start = struct.unpack_from("<Q", data)[0]
        for frn, parent, _usn, _reason, attrs, name in parse_usn_records(data, 8):
            yield frn, parent, attrs, name


# ---------------------------------------------------------------------------
# Volume access (direct handle here; the M-Finder service provides a proxy)
# ---------------------------------------------------------------------------


class LocalVolume:
    """Privileged access to one NTFS volume through a direct handle (needs administrator)."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.h = open_volume(root)

    def query_journal(self) -> dict:
        return query_journal(self.h)

    def create_journal(self) -> None:
        create_journal(self.h)

    def enum_mft(self, high_usn: int) -> Iterator[tuple]:
        return enum_mft(self.h, high_usn)

    def read_journal(self, start_usn: int, journal_id: int) -> tuple[int, list[tuple]]:
        req = struct.pack("<qIIQQQ", start_usn, 0xFFFFFFFF, 1, 0, 0, journal_id)
        data, err = _ioctl(self.h, FSCTL_READ_USN_JOURNAL, req, 1 << 18)
        if data is None:
            raise OSError(err, f"Cannot read USN journal: error {err}")
        return struct.unpack_from("<q", data)[0], list(parse_usn_records(data, 8))

    def scan_metadata(self, cancel=None, wanted=None):
        from . import mftparse

        letter = self.root.rstrip("\\/")[:2]
        with open("\\\\.\\" + letter, "rb", buffering=0) as f:
            yield from mftparse.scan_metadata(f, cancel=cancel, wanted=wanted)

    def close(self) -> None:
        if self.h:
            close_handle(self.h)
            self.h = None


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def index_volume(
    index: Index,
    root: str,
    excludes: Optional[Excludes] = None,
    progress: Optional[Callable[[int], None]] = None,
    cancel: Optional[threading.Event] = None,
    access=None,
) -> int:
    """Index an NTFS volume through its MFT. Raises PermissionError/OSError on failure.

    ``access`` is a volume-access object (default: :class:`LocalVolume`).
    """
    excludes = excludes or Excludes()
    own = access is None
    if own:
        access = LocalVolume(root)
    try:
        try:
            info = access.query_journal()
        except OSError as e:
            if e.errno != ERROR_JOURNAL_NOT_ACTIVE:
                raise
            access.create_journal()
            info = access.query_journal()

        records: dict[int, tuple[int, int, str]] = {}
        for n, (frn, parent, attrs, name) in enumerate(access.enum_mft(info["next_usn"])):
            if (frn & MFT_RECORD_MASK) < FIRST_USER_RECORD:
                continue
            records[frn] = (parent, attrs, name)
            if progress is not None and n % 50000 == 0:
                progress(n)
            if cancel is not None and cancel.is_set():
                break
    finally:
        if own:
            access.close()

    vol, root_id = index.add_root(root, "ntfs")
    index.roots[vol]["journal_id"] = info["journal_id"]
    index.roots[vol]["next_usn"] = info["next_usn"]
    fmap = index.frn_maps[vol]

    # Assign ids, then link parents and prune unreachable / excluded subtrees.
    ids: dict[int, int] = {}
    for frn, (parent, attrs, name) in records.items():
        ids[frn] = index.add(name, -1, attrs, frn=frn, vol=vol)
    root_name = index.names[root_id]
    n = len(index)
    state = bytearray(n)  # 0 unknown, 1 keep, 2 drop
    state[root_id] = 1
    hidden_rules = excludes.hidden or excludes.system or excludes._patterns
    folder_rules = excludes.any_folder_rules

    def decide(frn: int) -> int:
        chain = []
        cur = frn
        verdict = 2
        while True:
            i = ids.get(cur)
            if i is None:
                if (cur & MFT_RECORD_MASK) == ROOT_RECORD:
                    verdict = 1
                break
            s = state[i]
            if s:
                verdict = s
                break
            chain.append(cur)
            cur = records[cur][0]
            if len(chain) > 4096:
                break
        # unwind from the top of the chain downwards
        parent_id = ids.get(cur, root_id) if verdict == 1 else -1
        for c in reversed(chain):
            i = ids[c]
            if verdict == 1:
                _p, attrs, name = records[c]
                if hidden_rules and (excludes.attr_excluded(attrs) or excludes.name_excluded(name)):
                    state[i] = 2
                    verdict = 2
                    continue
                index.parent[i] = parent_id
                state[i] = 1
                parent_id = i
            else:
                state[i] = 2
        return verdict

    for frn in records:
        i = ids[frn]
        if not state[i]:
            decide(frn)
    for frn, i in ids.items():
        if state[i] == 1:
            fmap[frn] = i
        else:
            index.alive[i] = 0

    if folder_rules:
        _apply_folder_excludes(index, vol, root_id, excludes)

    index.rev += 1
    return vol


def _apply_folder_excludes(index: Index, vol: int, root_id: int, excludes: Excludes) -> None:
    cache: dict[int, bool] = {root_id: False}

    def excluded(i: int) -> bool:
        r = cache.get(i)
        if r is not None:
            return r
        p = index.parent[i]
        if p >= 0 and excluded(p):
            r = True
        else:
            r = bool(index.attrs[i] & FILE_ATTRIBUTE_DIRECTORY) and excludes.folder_excluded(index.full_path(i))
        cache[i] = r
        return r

    for i in range(len(index)):
        if index.vol[i] == vol and index.alive[i] and i != root_id and excluded(i):
            index.alive[i] = 0
            f = index.frn[i]
            if f:
                index.frn_maps[vol].pop(f, None)


# ---------------------------------------------------------------------------
# Live monitoring
# ---------------------------------------------------------------------------


def apply_usn_record(index: Index, vol: int, rec: tuple, excludes: Optional[Excludes] = None) -> bool:
    """Apply one journal record to the index. Returns True if the index changed."""
    frn, parent_frn, _usn, reason, attrs, name = rec
    if (frn & MFT_RECORD_MASK) < FIRST_USER_RECORD:
        return False
    fmap = index.frn_maps[vol]
    with index.lock:
        existing = fmap.get(frn)
        if reason & USN_REASON_FILE_DELETE:
            if existing is not None:
                index.remove(existing)
                return True
            return False
        if reason & (USN_REASON_FILE_CREATE | USN_REASON_RENAME_NEW_NAME):
            if excludes and (excludes.attr_excluded(attrs) or excludes.name_excluded(name)):
                if existing is not None:
                    index.remove(existing)
                    return True
                return False
            parent_id = fmap.get(parent_frn)
            if parent_id is None:
                if (parent_frn & MFT_RECORD_MASK) == ROOT_RECORD:
                    parent_id = index.roots[vol]["root_id"]
                else:
                    return False
            if existing is None:
                i = index.add(name, parent_id, attrs, frn=frn, vol=vol)
                fmap[frn] = i
            else:
                i = existing
                index.relocate(i, name, parent_id)
                index.attrs[i] = attrs
                index.invalidate_meta(i)
            index.rev += 1
            return True
        if reason & (
            USN_REASON_DATA_OVERWRITE | USN_REASON_DATA_EXTEND | USN_REASON_DATA_TRUNCATION | USN_REASON_BASIC_INFO_CHANGE
        ):
            if existing is not None:
                index.invalidate_meta(existing)
                index.touch_meta()
                return True
    return False


class UsnMonitor(threading.Thread):
    """Polls the USN journal of one NTFS volume and applies changes to the index."""

    def __init__(
        self,
        index: Index,
        vol: int,
        excludes: Optional[Excludes] = None,
        on_stale: Optional[Callable[[int], None]] = None,
        interval: float = 1.0,
        open_access: Optional[Callable[[str], object]] = None,
    ) -> None:
        super().__init__(daemon=True, name=f"usn-{index.roots[vol]['path']}")
        self.index = index
        self.vol = vol
        self.excludes = excludes
        self.on_stale = on_stale
        self.interval = interval
        self.open_access = open_access or LocalVolume
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        root = self.index.roots[self.vol]
        try:
            access = self.open_access(root["path"])
        except OSError:
            return
        try:
            try:
                info = access.query_journal()
            except OSError:
                self._stale()
                return
            if info["journal_id"] != root["journal_id"] or root["next_usn"] < info["lowest_valid_usn"]:
                self._stale()
                return
            next_usn = root["next_usn"]
            while not self._stop_event.is_set():
                try:
                    next_usn, recs = access.read_journal(next_usn, root["journal_id"])
                except OSError as e:
                    if e.errno in (ERROR_JOURNAL_ENTRY_DELETED, ERROR_JOURNAL_NOT_ACTIVE):
                        self._stale()
                        return
                    self._stop_event.wait(self.interval)
                    continue
                for rec in recs:
                    apply_usn_record(self.index, self.vol, rec, self.excludes)
                root["next_usn"] = next_usn
                if not recs:
                    self._stop_event.wait(self.interval)
        finally:
            access.close()

    def _stale(self) -> None:
        if self.on_stale is not None:
            self.on_stale(self.vol)
