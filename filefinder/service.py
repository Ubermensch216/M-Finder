"""FileFinder service: lets an unelevated UI use the NTFS MFT / USN journal.

Like Everything's service, a small privileged process opens the raw volume so the
GUI does not have to run as administrator. The service never hands out raw disk
bytes: it exposes only (a) the USN journal / MFT enumeration (names, file
reference numbers, attributes) and (b) metadata parsed from $MFT (size and
timestamps). Access is limited to fixed local drives and to the SIDs it was
installed for.

SECURITY: a SYSTEM service that runs code from a user-writable location is a
privilege-escalation hole. ``install`` therefore refuses unless the interpreter
and this package live in an admin-only location (Program Files / Windows), or
``--unsafe-allow-user-paths`` is given explicitly.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
import threading
from ctypes import wintypes
from typing import Iterator, Optional

from . import ntfs
from .ipc import PipeClient, PipeError, PipeServer, RemoteError, T_BIN, T_END, current_user_sid, sddl_for

SERVICE_NAME = "FileFinderSvc"
DISPLAY_NAME = "FileFinder"
PIPE_NAME = "FileFinder-Service"
_ROOT_RE = re.compile(r"^[A-Za-z]:\\?$")

REC = struct.Struct("<QQqIIH")  # frn, parent, usn, reason, attrs, name length (utf-16 bytes)
META = struct.Struct("<Qqddd")  # record number, size, ctime, mtime, atime
BATCH_RECORDS = 4000


# ---------------------------------------------------------------------------
# wire encoding
# ---------------------------------------------------------------------------


def encode_records(recs) -> bytes:
    out = []
    for frn, parent, usn, reason, attrs, name in recs:
        raw = name.encode("utf-16-le")
        out.append(REC.pack(frn, parent, usn, reason, attrs, len(raw)) + raw)
    return b"".join(out)


def decode_records(buf: bytes) -> Iterator[tuple]:
    pos = 0
    while pos + REC.size <= len(buf):
        frn, parent, usn, reason, attrs, n = REC.unpack_from(buf, pos)
        pos += REC.size
        yield frn, parent, usn, reason, attrs, buf[pos : pos + n].decode("utf-16-le", "replace")
        pos += n


# ---------------------------------------------------------------------------
# server side
# ---------------------------------------------------------------------------


class VolumeProxy:
    """Request handler run inside the service (needs administrator/SYSTEM)."""

    def handle(self, req: dict, send_bin) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "service": True}
        root = str(req.get("root", ""))
        if not _ROOT_RE.match(root) or root[:2].upper() + "\\" not in [d.upper() for d in ntfs.fixed_drives()]:
            raise PermissionError("only fixed local drive roots are allowed")
        if not ntfs.is_ntfs_root(root):
            raise ValueError("not an NTFS volume")
        vol = ntfs.LocalVolume(root)
        try:
            if op == "query_journal":
                return vol.query_journal()
            if op == "create_journal":
                vol.create_journal()
                return {}
            if op == "enum_mft":
                batch = []
                for frn, parent, attrs, name in vol.enum_mft(int(req["high_usn"])):
                    batch.append((frn, parent, 0, 0, attrs, name))
                    if len(batch) >= BATCH_RECORDS:
                        send_bin(encode_records(batch))
                        batch = []
                if batch:
                    send_bin(encode_records(batch))
                return {}
            if op == "read_journal":
                nxt, recs = vol.read_journal(int(req["start_usn"]), int(req["journal_id"]))
                send_bin(encode_records(recs))
                return {"next_usn": nxt}
            if op == "scan_metadata":
                for batch in vol.scan_metadata(None, None):
                    send_bin(b"".join(META.pack(*t) for t in batch))
                return {}
            raise ValueError(f"unknown op {op!r}")
        finally:
            vol.close()


def start_proxy(allow_sids: list[str]) -> PipeServer:
    server = PipeServer(PIPE_NAME, VolumeProxy().handle, sddl_for(allow_sids))
    server.start()
    return server


# ---------------------------------------------------------------------------
# client side: same interface as ntfs.LocalVolume
# ---------------------------------------------------------------------------


class ServiceVolume:
    def __init__(self, root: str) -> None:
        self.root = root
        try:
            with PipeClient(PIPE_NAME, 1000) as c:
                if not c.call({"op": "ping"}).get("service"):
                    raise OSError(0, "FileFinder service not responding")
        except PipeError as e:
            raise OSError(e.errno, f"FileFinder service not available: {e}") from e

    def _stream(self, req: dict) -> Iterator[bytes]:
        req = dict(req, root=self.root)
        with PipeClient(PIPE_NAME) as c:
            for ftype, payload in c.stream(req):
                if ftype == T_BIN:
                    yield payload

    def _call(self, req: dict) -> dict:
        with PipeClient(PIPE_NAME) as c:
            return c.call(dict(req, root=self.root))

    def query_journal(self) -> dict:
        return self._call({"op": "query_journal"})

    def create_journal(self) -> None:
        self._call({"op": "create_journal"})

    def enum_mft(self, high_usn: int) -> Iterator[tuple]:
        for chunk in self._stream({"op": "enum_mft", "high_usn": high_usn}):
            for frn, parent, _u, _r, attrs, name in decode_records(chunk):
                yield frn, parent, attrs, name

    def read_journal(self, start_usn: int, journal_id: int):
        recs = []
        with PipeClient(PIPE_NAME) as c:
            res = {}
            for ftype, payload in c.stream(
                {"op": "read_journal", "root": self.root, "start_usn": start_usn, "journal_id": journal_id}
            ):
                if ftype == T_BIN:
                    recs.extend((f, p, u, r, a, n) for f, p, u, r, a, n in decode_records(payload))
                elif ftype == T_END:
                    import json

                    res = json.loads(payload.decode("utf-8"))
        return res["next_usn"], recs

    def scan_metadata(self, cancel: Optional[threading.Event] = None, wanted=None):
        for chunk in self._stream({"op": "scan_metadata"}):
            if cancel is not None and cancel.is_set():
                return
            batch = [t for t in META.iter_unpack(chunk) if wanted is None or (t[0] < len(wanted) and wanted[t[0]] >= 0)]
            if batch:
                yield batch

    def close(self) -> None:
        pass


def service_available() -> bool:
    try:
        with PipeClient(PIPE_NAME, 300) as c:
            return bool(c.call({"op": "ping"}).get("service"))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Service Control Manager plumbing (ctypes)
# ---------------------------------------------------------------------------

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_AUTO_START = 2
SERVICE_ERROR_NORMAL = 1
SERVICE_ALL_ACCESS = 0xF01FF
SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN = 1, 5
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 1, 4
ERROR_SERVICE_DOES_NOT_EXIST = 1060

if sys.platform == "win32":
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in (
            "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
            "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]

    class SERVICE_DESCRIPTIONW(ctypes.Structure):
        _fields_ = [("lpDescription", wintypes.LPWSTR)]

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", ctypes.c_void_p)]

    _MAIN_FN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    _HANDLER_FN = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    _SC = wintypes.SC_HANDLE if hasattr(wintypes, "SC_HANDLE") else ctypes.c_void_p
    _adv.OpenSCManagerW.restype = _SC
    _adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _adv.CreateServiceW.restype = _SC
    _adv.CreateServiceW.argtypes = [_SC, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPCWSTR,
                                    wintypes.LPCWSTR, wintypes.LPCWSTR]
    _adv.OpenServiceW.restype = _SC
    _adv.OpenServiceW.argtypes = [_SC, wintypes.LPCWSTR, wintypes.DWORD]
    _adv.StartServiceW.argtypes = [_SC, wintypes.DWORD, ctypes.c_void_p]
    _adv.ControlService.argtypes = [_SC, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
    _adv.QueryServiceStatus.argtypes = [_SC, ctypes.POINTER(SERVICE_STATUS)]
    _adv.DeleteService.argtypes = [_SC]
    _adv.CloseServiceHandle.argtypes = [_SC]
    _adv.ChangeServiceConfig2W.argtypes = [_SC, wintypes.DWORD, ctypes.c_void_p]
    _adv.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
    _adv.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    _adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, _HANDLER_FN, ctypes.c_void_p]
    _adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]

_state: dict = {}


def _set_status(state: int, accepted: int = 0, wait_hint: int = 0) -> None:
    st = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accepted, 0, 0, 0, wait_hint)
    _adv.SetServiceStatus(_state["handle"], ctypes.byref(st))


def _parse_allowed_sids(argv: list[str]) -> list[str]:
    sids = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--allow-sid"]
    if not sids:
        raise SystemExit("service started without --allow-sid; refusing to open the pipe to nobody")
    return sids


def service_main() -> None:
    """Entry point when launched by the Service Control Manager."""
    allow = _parse_allowed_sids(sys.argv[1:])
    stop = threading.Event()

    def handler(control, _etype, _data, _ctx):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            _set_status(SERVICE_STOP_PENDING, wait_hint=5000)
            stop.set()
        return 0

    def svc_main(_argc, _argv):
        _state["handler"] = _HANDLER_FN(handler)
        _state["handle"] = _adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, _state["handler"], None)
        _set_status(SERVICE_START_PENDING, wait_hint=5000)
        try:
            server = start_proxy(allow)
        except OSError:
            _set_status(SERVICE_STOPPED)
            return
        _set_status(SERVICE_RUNNING, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)
        stop.wait()
        server.stop()
        _set_status(SERVICE_STOPPED)

    _state["main"] = _MAIN_FN(svc_main)
    table = (SERVICE_TABLE_ENTRYW * 2)(
        SERVICE_TABLE_ENTRYW(SERVICE_NAME, ctypes.cast(_state["main"], ctypes.c_void_p)),
        SERVICE_TABLE_ENTRYW(None, None),
    )
    if not _adv.StartServiceCtrlDispatcherW(table):
        raise SystemExit(f"StartServiceCtrlDispatcher failed: {ctypes.get_last_error()} (not started by the SCM?)")


def run_console(allow_sids: Optional[list[str]] = None) -> None:
    """Run the proxy in the foreground (elevated shell) — for testing without installing a service."""
    server = start_proxy(allow_sids or [current_user_sid()])
    print(f"FileFinder volume proxy listening on \\\\.\\pipe\\{PIPE_NAME} (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


# ---- install / control --------------------------------------------------------


def _safe_location(path: str) -> bool:
    bases = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "SystemRoot", "ProgramW6432")]
    p = os.path.normcase(os.path.realpath(path))
    return any(b and p.startswith(os.path.normcase(os.path.realpath(b)) + os.sep) for b in bases)


def install_problems() -> list[str]:
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "frozen", False):
        checks = (("FileFinder executable", sys.executable),)
    else:
        checks = (("Python interpreter", sys.executable), ("FileFinder package", pkg_root))
    return [f"{what} is in a location writable by non-administrators: {path}" for what, path in checks if not _safe_location(path)]


def service_command_line(allow_sids: list[str]) -> str:
    args = " ".join(f"--allow-sid {s}" for s in allow_sids)
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" service-run {args}'
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = f"import sys; sys.path.insert(0, r'{pkg_root}'); from filefinder.service import service_main; service_main()"
    return f'"{sys.executable}" -I -c "{code}" {args}'


def _scm():
    h = _adv.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    if not h:
        raise OSError(ctypes.get_last_error(), "OpenSCManager failed (run as administrator)")
    return h


def install(allow_sids: Optional[list[str]] = None, allow_user_paths: bool = False, start: bool = True) -> None:
    problems = install_problems()
    if problems and not allow_user_paths:
        raise PermissionError(
            "Refusing to install a SYSTEM service that executes user-writable files:\n  "
            + "\n  ".join(problems)
            + "\nInstall FileFinder under Program Files, or pass --unsafe-allow-user-paths if you accept the risk."
        )
    sids = allow_sids or [current_user_sid()]
    scm = _scm()
    try:
        h = _adv.CreateServiceW(scm, SERVICE_NAME, DISPLAY_NAME, SERVICE_ALL_ACCESS, SERVICE_WIN32_OWN_PROCESS,
                                SERVICE_AUTO_START, SERVICE_ERROR_NORMAL, service_command_line(sids),
                                None, None, None, None, None)
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateService failed")
        try:
            desc = SERVICE_DESCRIPTIONW("Lets FileFinder index NTFS volumes without running as administrator.")
            _adv.ChangeServiceConfig2W(h, 1, ctypes.byref(desc))
            if start and not _adv.StartServiceW(h, 0, None):
                raise OSError(ctypes.get_last_error(), "StartService failed")
        finally:
            _adv.CloseServiceHandle(h)
    finally:
        _adv.CloseServiceHandle(scm)


def _open(access: int):
    scm = _scm()
    h = _adv.OpenServiceW(scm, SERVICE_NAME, access)
    err = ctypes.get_last_error()
    _adv.CloseServiceHandle(scm)
    if not h:
        raise OSError(err, "service is not installed" if err == ERROR_SERVICE_DOES_NOT_EXIST else "OpenService failed")
    return h


def uninstall() -> None:
    h = _open(SERVICE_ALL_ACCESS)
    try:
        st = SERVICE_STATUS()
        _adv.ControlService(h, SERVICE_CONTROL_STOP, ctypes.byref(st))
        if not _adv.DeleteService(h):
            raise OSError(ctypes.get_last_error(), "DeleteService failed")
    finally:
        _adv.CloseServiceHandle(h)


def start_service() -> None:
    h = _open(SERVICE_ALL_ACCESS)
    try:
        if not _adv.StartServiceW(h, 0, None):
            raise OSError(ctypes.get_last_error(), "StartService failed")
    finally:
        _adv.CloseServiceHandle(h)


def stop_service() -> None:
    h = _open(SERVICE_ALL_ACCESS)
    try:
        st = SERVICE_STATUS()
        if not _adv.ControlService(h, SERVICE_CONTROL_STOP, ctypes.byref(st)):
            raise OSError(ctypes.get_last_error(), "ControlService failed")
    finally:
        _adv.CloseServiceHandle(h)


def status() -> str:
    try:
        h = _open(0x4)  # SERVICE_QUERY_STATUS
    except OSError as e:
        if e.errno == ERROR_SERVICE_DOES_NOT_EXIST:
            return "not installed"
        raise
    try:
        st = SERVICE_STATUS()
        _adv.QueryServiceStatus(h, ctypes.byref(st))
        return {1: "stopped", 2: "starting", 3: "stopping", 4: "running"}.get(st.dwCurrentState, str(st.dwCurrentState))
    finally:
        _adv.CloseServiceHandle(h)
