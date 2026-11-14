"""Command-line entry point: GUI by default, plus search/index/serve/etp/service sub-commands."""

from __future__ import annotations

import argparse
import ntpath
import os
import sys
import time

from . import __version__, filelist, query
from .engine import Engine
from .fmt import format_size, format_time
from .settings import Settings


def _engine(args) -> Engine:
    settings = Settings.load()
    if getattr(args, "root", None):
        settings.roots = list(args.root)
    if getattr(args, "filelist", None):
        settings.filelists = list(args.filelist)
    if getattr(args, "no_mft", False):
        settings.use_mft = False
    return Engine(settings)


def _ensure_index(engine: Engine, reindex: bool) -> None:
    if not reindex and engine.load():
        return
    engine.on_status = lambda t: print("\r" + t.ljust(70), end="", file=sys.stderr, flush=True)
    t = time.time()
    engine.rebuild()
    print(f"\rIndexed in {time.time() - t:.1f}s".ljust(72), file=sys.stderr)


def _ipc_search(args, text: str):
    """Ask a running M-Finder; returns (total, rows) or None when none is running."""
    if sys.platform != "win32" or args.reindex or args.root or args.filelist or args.no_ipc:
        return None
    from . import queryipc

    try:
        res = queryipc.request({
            "op": "search", "query": text, "case": args.case, "ww": args.whole_word, "path": args.match_path,
            "regex": args.regex, "sort": args.sort, "asc": not args.descending, "filter": args.filter,
            "offset": 0, "count": args.count if args.count is not None else queryipc.MAX_COUNT,
        }, timeout_ms=1500)
    except OSError:
        return None
    if not res.get("ok"):
        return None
    return res["total"], res["results"]


def cmd_search(args) -> int:
    text = " ".join(args.query)
    via_ipc = _ipc_search(args, text)
    if via_ipc is not None:
        total, rows = via_ipc
        for r in rows:
            full = ntpath.join(r["path"], r["name"]) if r["path"] else r["name"]
            if args.long:
                print(f"{format_time(r['date_modified'] or 0):16} {format_size(r['size'] if r['size'] is not None else -1):>14}  {full}")
            else:
                print(full)
        if args.count is not None and total > len(rows):
            print(f"... {total - len(rows):,} more ({total:,} total)", file=sys.stderr)
        if args.export:
            print("--export needs a local search; add --no-ipc", file=sys.stderr)
            return 2
        return 0 if total else 1
    engine = _engine(args)
    _ensure_index(engine, args.reindex)
    flags = query.Flags(case=args.case, ww=args.whole_word, path=args.match_path, regex=args.regex)
    try:
        ids = engine.search(text, args.sort, not args.descending, flags=flags, filter_name=args.filter)
    except query.QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    idx = engine.index
    shown = ids if args.count is None else ids[: args.count]
    for i in shown:
        if args.long:
            size, mtime = idx.meta(i)
            print(f"{format_time(mtime):16} {format_size(size):>14}  {idx.full_path(i)}")
        else:
            print(idx.full_path(i))
    if args.count is not None and len(ids) > len(shown):
        print(f"... {len(ids) - len(shown):,} more ({len(ids):,} total)", file=sys.stderr)
    if args.export:
        n = filelist.export_results(idx, ids, args.export)
        print(f"exported {n:,} entries to {args.export}", file=sys.stderr)
    return 0 if ids else 1


def cmd_index(args) -> int:
    engine = _engine(args)
    _ensure_index(engine, True)
    print(f"{engine.index.live_count():,} objects -> {engine.settings.database_file}")
    for r in engine.index.roots:
        print(f"  {r['path']} ({ {'ntfs': 'MFT', 'folder': 'folder scan', 'filelist': 'file list'}.get(r['kind'], r['kind']) })")
    return 0


def _serve_forever(engine: Engine, *services) -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for s in services:
            s.stop()
        engine.shutdown()


def cmd_serve(args) -> int:
    from .httpserver import HttpService

    engine = _engine(args)
    s = engine.settings
    if args.port:
        s.http_port = args.port
    if args.bind:
        s.http_bind = args.bind
    _ensure_index(engine, args.reindex)
    engine.start_monitors()
    engine.start_background()
    svc = HttpService(engine)
    svc.start()
    print(f"Serving http://{s.http_bind}:{s.http_port}/  (Ctrl+C to stop)")
    _serve_forever(engine, svc)
    return 0


def cmd_etp(args) -> int:
    from .etp import EtpService

    engine = _engine(args)
    s = engine.settings
    if args.port:
        s.etp_port = args.port
    if args.bind:
        s.etp_bind = args.bind
    if args.user is not None:
        s.etp_user = args.user
    if args.password is not None:
        s.etp_password = args.password
    s.etp_allow_download = args.allow_download or s.etp_allow_download
    _ensure_index(engine, args.reindex)
    engine.start_monitors()
    engine.start_background()
    svc = EtpService(engine)
    svc.start()
    print(f"ETP/FTP server on ftp://{s.etp_bind}:{s.etp_port}/ "
          f"(downloads {'enabled' if s.etp_allow_download else 'disabled'}; Ctrl+C to stop)")
    _serve_forever(engine, svc)
    return 0


def cmd_service(args) -> int:
    from . import service

    try:
        if args.action == "install":
            service.install(args.allow_sid, args.unsafe_allow_user_paths)
            print("Service installed and started.")
        elif args.action == "uninstall":
            service.uninstall()
            print("Service removed.")
        elif args.action == "start":
            service.start_service()
        elif args.action == "stop":
            service.stop_service()
        elif args.action == "status":
            print(service.status())
        elif args.action == "run-console":
            service.run_console(args.allow_sid)
    except (OSError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_service_run(args) -> int:
    """Entry point the Service Control Manager launches (see service.service_command_line)."""
    from . import service

    service.service_main()
    return 0


def _attach_console() -> None:
    """A windowed (no-console) .exe has no stdout; borrow the parent's console for CLI output."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    else:  # launched from Explorer: nowhere to print, but never crash on print()
        sys.stdout = sys.stderr = open(os.devnull, "w")


def cmd_gui(args) -> int:
    from .gui import run

    return run(
        Settings.load(), " ".join(getattr(args, "query", []) or []), args.minimized, args.new_instance,
        getattr(args, "headless", False),
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mfinder", description="Instant file search (Everything-style).")
    p.add_argument("--version", action="version", version=f"mfinder {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, local=True):
        sp.add_argument("--root", action="append", help="folder/drive to index (repeatable)")
        sp.add_argument("--filelist", action="append", help="index an .efu/.csv/.txt file list (repeatable)")
        sp.add_argument("--no-mft", action="store_true", help="never use the NTFS MFT")

    g = sub.add_parser("gui", help="open the search window (default)")
    g.add_argument("query", nargs="*")
    g.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    g.add_argument("--new-instance", action="store_true", help="do not reuse a running instance")
    g.add_argument("--headless", action="store_true",
                   help="no window: keep the index and serve searches over IPC (used by M-Dir); ends on the IPC 'quit' request")
    g.set_defaults(func=cmd_gui)

    s = sub.add_parser("search", help="search from the command line (uses a running M-Finder if there is one)")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--count", type=int)
    s.add_argument("-i", "--case", action="store_true", help="match case")
    s.add_argument("-w", "--whole-word", action="store_true")
    s.add_argument("-p", "--match-path", action="store_true")
    s.add_argument("-r", "--regex", action="store_true")
    s.add_argument("-l", "--long", action="store_true", help="show date and size")
    s.add_argument("-f", "--filter", help="named filter, e.g. Picture")
    s.add_argument("--export", metavar="FILE", help="write results to .efu/.csv/.txt")
    s.add_argument("--sort", choices=query.SORT_KEYS, default="name")
    s.add_argument("--descending", action="store_true")
    s.add_argument("--reindex", action="store_true")
    s.add_argument("--no-ipc", action="store_true", help="do not ask a running M-Finder")
    common(s)
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("index", help="(re)build the index database")
    common(i)
    i.set_defaults(func=cmd_index)

    h = sub.add_parser("serve", help="run the HTTP search server")
    h.add_argument("--port", type=int)
    h.add_argument("--bind")
    h.add_argument("--reindex", action="store_true")
    common(h)
    h.set_defaults(func=cmd_serve)

    e = sub.add_parser("etp", help="run the ETP/FTP server")
    e.add_argument("--port", type=int)
    e.add_argument("--bind")
    e.add_argument("--user")
    e.add_argument("--password")
    e.add_argument("--allow-download", action="store_true")
    e.add_argument("--reindex", action="store_true")
    common(e)
    e.set_defaults(func=cmd_etp)

    v = sub.add_parser("service", help="manage the privileged M-Finder service")
    v.add_argument("action", choices=["install", "uninstall", "start", "stop", "status", "run-console"])
    v.add_argument("--allow-sid", action="append", help="SID allowed to use the service (default: you)")
    v.add_argument("--unsafe-allow-user-paths", action="store_true",
                   help="install even though the files are writable by non-administrators (privilege-escalation risk)")
    v.set_defaults(func=cmd_service)

    sr = sub.add_parser("service-run")  # internal: started by the Service Control Manager
    sr.set_defaults(func=cmd_service_run)

    argv = sys.argv[1:] if argv is None else list(argv)
    if getattr(sys, "frozen", False) and argv[:1] and argv[0] in ("search", "index", "serve", "etp", "service", "-h", "--help", "--version"):
        _attach_console()  # CLI use of the windowed .exe; double-click / GUI launches must not grab a console
    if not argv or (argv[0] not in sub.choices and not argv[0].startswith("-")):
        argv = ["gui"] + argv  # bare words open the GUI pre-filled with that query
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
