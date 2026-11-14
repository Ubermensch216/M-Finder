"""Per-user search IPC: lets other programs (and ``mfinder search``) query a running instance.

The pipe is named after the user's SID and its ACL grants only that user, so one
user's index is never exposed to another. Replaces Everything's window-message SDK.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import __version__, query
from .engine import Engine
from .ipc import PipeClient, PipeError, PipeServer, current_user_sid, sddl_for

MAX_COUNT = 5000


def pipe_name() -> str:
    return "M-Finder-" + current_user_sid()


def _row(idx, i: int) -> dict:
    size, mtime = idx.meta(i)
    return {
        "name": idx.names[i],
        "path": idx.parent_path(i),
        "is_dir": idx.is_dir(i),
        "size": size if size >= 0 else None,
        "date_modified": mtime if mtime > 0 else None,
        "date_created": idx.ctime[i] or None,
        "attributes": idx.attrs[i],
    }


class QueryIpc:
    def __init__(self, engine: Engine, on_show: Optional[Callable[[str], None]] = None) -> None:
        self.engine = engine
        self.on_show = on_show
        sid = current_user_sid()
        self.server = PipeServer("M-Finder-" + sid, self.handle, sddl_for([sid], system_and_admins=False))

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()

    def handle(self, req: dict, send_bin) -> dict:
        op = req.get("op")
        e = self.engine
        if op == "ping":
            return {"ok": True, "version": __version__}
        if op == "status":
            idx = e.index
            return {
                "ok": True, "objects": idx.live_count(), "building": e.building, "status": e.status,
                "roots": [{"path": r["path"], "kind": r["kind"]} for r in idx.roots],
            }
        if op == "show":
            if self.on_show:
                self.on_show(str(req.get("query", "")))
            return {"ok": True}
        if op == "search":
            flags = query.Flags(
                case=bool(req.get("case")), ww=bool(req.get("ww")), path=bool(req.get("path")), regex=bool(req.get("regex"))
            )
            ids = e.search(
                str(req.get("query", "")), req.get("sort") or "name", bool(req.get("asc", True)),
                flags=flags, filter_name=req.get("filter"),
            )
            offset = max(0, int(req.get("offset", 0)))
            count = max(0, min(MAX_COUNT, int(req.get("count", 100))))
            return {
                "ok": True,
                "total": len(ids),
                "results": [_row(e.index, i) for i in ids[offset : offset + count]],
            }
        raise ValueError(f"unknown op {op!r}")


def request(req: dict, timeout_ms: int = 2000) -> dict:
    with PipeClient(pipe_name(), timeout_ms) as c:
        return c.call(req)


def running() -> bool:
    try:
        return bool(request({"op": "ping"}).get("ok"))
    except (PipeError, OSError):
        return False
