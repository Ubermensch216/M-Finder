"""Named-pipe IPC (length-prefixed frames) with explicit pipe security.

Frame: u32 length, u8 type, payload.  Types: JSON request/response (1), binary
chunk (2), end of stream with JSON status (3), error with JSON detail (4).
A request is one JSON frame; the reply is zero or more binary/JSON frames
followed by an end (3) or error (4) frame.
"""

from __future__ import annotations

import ctypes
import json
import struct
import sys
import threading
from ctypes import wintypes
from typing import Callable, Iterator, Optional

T_JSON, T_BIN, T_END, T_ERR = 1, 2, 3, 4
MAX_FRAME = 64 << 20
ERROR_PIPE_BUSY = 231
ERROR_PIPE_CONNECTED = 535
PIPE_ACCESS_DUPLEX = 3
PIPE_TYPE_BYTE_WAIT_NOREMOTE = 0x0 | 0x0 | 0x8  # byte type/read mode, blocking, reject remote clients
PIPE_UNLIMITED_INSTANCES = 255
GENERIC_RW = 0xC0000000
_INVALID = ctypes.c_void_p(-1).value

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    _k32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
    ]
    _k32.CreateNamedPipeW.restype = wintypes.HANDLE
    _k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.ConnectNamedPipe.restype = wintypes.BOOL
    _k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE


class PipeError(OSError):
    pass


def current_user_sid() -> str:
    """String SID of the user this process runs as."""
    tok = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x8, ctypes.byref(tok)):  # TOKEN_QUERY
        raise PipeError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        need = wintypes.DWORD(0)
        _adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))  # TokenUser
        buf = ctypes.create_string_buffer(need.value)
        if not _adv.GetTokenInformation(tok, 1, buf, need.value, ctypes.byref(need)):
            raise PipeError(ctypes.get_last_error(), "GetTokenInformation failed")
        sid_ptr = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
        out = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
            raise PipeError(ctypes.get_last_error(), "ConvertSidToStringSid failed")
        sid = out.value
        _k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
        return sid
    finally:
        _k32.CloseHandle(tok)


def sddl_for(sids: list[str], system_and_admins: bool = True) -> str:
    """Protected DACL granting full pipe access only to the listed SIDs (+ SYSTEM and Administrators)."""
    parts = ["D:P"]
    if system_and_admins:
        parts += ["(A;;GA;;;SY)", "(A;;GA;;;BA)"]
    for s in sids:
        if not s.startswith("S-") or any(c not in "S-0123456789" for c in s):
            raise ValueError(f"bad SID {s!r}")
        parts.append(f"(A;;GA;;;{s})")
    return "".join(parts)


def pipe_path(name: str) -> str:
    return "\\\\.\\pipe\\" + name


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def _read_exact(h, n: int) -> bytes:
    buf = ctypes.create_string_buffer(n)
    got = 0
    ret = wintypes.DWORD(0)
    while got < n:
        ok = _k32.ReadFile(h, ctypes.byref(buf, got), n - got, ctypes.byref(ret), None)
        if not ok or ret.value == 0:
            raise PipeError(ctypes.get_last_error(), "pipe closed")
        got += ret.value
    return buf.raw


def _write_all(h, data: bytes) -> None:
    ret = wintypes.DWORD(0)
    view = memoryview(data)
    off = 0
    while off < len(data):
        chunk = bytes(view[off : off + (1 << 20)])
        if not _k32.WriteFile(h, chunk, len(chunk), ctypes.byref(ret), None):
            raise PipeError(ctypes.get_last_error(), "pipe write failed")
        off += ret.value


def send_frame(h, ftype: int, payload: bytes) -> None:
    _write_all(h, struct.pack("<IB", len(payload), ftype) + payload)


def recv_frame(h) -> tuple[int, bytes]:
    length, ftype = struct.unpack("<IB", _read_exact(h, 5))
    if length > MAX_FRAME:
        raise PipeError(0, "frame too large")
    return ftype, _read_exact(h, length) if length else b""


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

Handler = Callable[[dict, Callable[[bytes], None]], dict]


class PipeServer:
    """Serves one request/reply stream per connection on a named pipe.

    ``handler(request, send_bin)`` returns the final JSON dict; raise an exception
    to send an error frame. ``send_bin`` streams binary chunks before the result.
    """

    def __init__(self, name: str, handler: Handler, sddl: str) -> None:
        self.name = name
        self.handler = handler
        self.sddl = sddl
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conns = 0

    def start(self) -> None:
        # Fail early (e.g. name already owned by another instance) instead of in the thread.
        first = self._create()
        self._thread = threading.Thread(target=self._loop, args=(first,), daemon=True, name=f"pipe-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:  # unblock ConnectNamedPipe
            h = _k32.CreateFileW(pipe_path(self.name), GENERIC_RW, 0, None, 3, 0, None)
            if h not in (None, _INVALID):
                _k32.CloseHandle(h)
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def _create(self):
        sd = ctypes.c_void_p()
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(self.sddl, 1, ctypes.byref(sd), None):
            raise PipeError(ctypes.get_last_error(), "bad pipe security descriptor")
        try:
            sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
            first = 0x00080000 if self._conns == 0 else 0  # FILE_FLAG_FIRST_PIPE_INSTANCE: refuse a squatter
            h = _k32.CreateNamedPipeW(
                pipe_path(self.name), PIPE_ACCESS_DUPLEX | first, PIPE_TYPE_BYTE_WAIT_NOREMOTE,
                PIPE_UNLIMITED_INSTANCES, 1 << 20, 1 << 20, 0, ctypes.byref(sa),
            )
        finally:
            _k32.LocalFree(sd)
        if h in (None, _INVALID):
            raise PipeError(ctypes.get_last_error(), f"cannot create pipe {self.name}")
        self._conns += 1
        return h

    def _loop(self, h) -> None:
        while not self._stop.is_set():
            connected = _k32.ConnectNamedPipe(h, None) or ctypes.get_last_error() == ERROR_PIPE_CONNECTED
            if self._stop.is_set():
                _k32.CloseHandle(h)
                return
            if connected:
                threading.Thread(target=self._serve, args=(h,), daemon=True).start()
                try:
                    h = self._create()
                except PipeError:
                    return
            else:
                _k32.DisconnectNamedPipe(h)

    def _serve(self, h) -> None:
        try:
            while True:
                ftype, payload = recv_frame(h)
                if ftype != T_JSON:
                    send_frame(h, T_ERR, b'{"error":"bad request frame"}')
                    continue
                try:
                    req = json.loads(payload.decode("utf-8"))
                    res = self.handler(req, lambda b: send_frame(h, T_BIN, b))
                    send_frame(h, T_END, json.dumps(res).encode("utf-8"))
                except PipeError:
                    raise
                except Exception as e:  # noqa: BLE001 - report any handler failure to the client
                    send_frame(h, T_ERR, json.dumps({"error": str(e), "errno": getattr(e, "errno", None)}).encode("utf-8"))
        except PipeError:
            pass
        finally:
            _k32.DisconnectNamedPipe(h)
            _k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class RemoteError(OSError):
    pass


class PipeClient:
    def __init__(self, name: str, timeout_ms: int = 2000) -> None:
        path = pipe_path(name)
        while True:
            h = _k32.CreateFileW(path, GENERIC_RW, 0, None, 3, 0, None)
            if h not in (None, _INVALID):
                break
            err = ctypes.get_last_error()
            if err == ERROR_PIPE_BUSY and _k32.WaitNamedPipeW(path, timeout_ms):
                continue
            raise PipeError(err, f"cannot connect to pipe {name}: error {err}")
        self.h = h

    def close(self) -> None:
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def stream(self, req: dict) -> Iterator[tuple[int, bytes]]:
        """Send ``req``; yield (T_BIN, chunk) frames, then (T_END, json) once."""
        send_frame(self.h, T_JSON, json.dumps(req).encode("utf-8"))
        while True:
            ftype, payload = recv_frame(self.h)
            if ftype == T_ERR:
                info = json.loads(payload.decode("utf-8"))
                raise RemoteError(info.get("errno") or 0, info.get("error", "remote error"))
            yield ftype, payload
            if ftype == T_END:
                return

    def call(self, req: dict) -> dict:
        result = {}
        for ftype, payload in self.stream(req):
            if ftype == T_END:
                result = json.loads(payload.decode("utf-8"))
        return result


def call(name: str, req: dict) -> dict:
    with PipeClient(name) as c:
        return c.call(req)
