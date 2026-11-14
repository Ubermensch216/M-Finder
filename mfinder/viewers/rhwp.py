"""Page-accurate HWP / HWPX rendering through the rhwp CLI (https://github.com/edwardkim/rhwp, MIT).

``rhwp export-pdf -p N`` lays a page out with Hancom's typesetting rules (page margins, headers and
footers, line breaking, table borders, shapes, ...) and embeds the fonts, so the result can be shown
by the PDF viewer. Pages are rendered one at a time (about 5 s each) into an on-disk cache, which
makes re-opening a document instant.

The executable is optional: when it is missing or fails, the caller falls back to the built-in
Python reader (:mod:`mfinder.viewers.hwp` / :mod:`.hwpx`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from ..settings import app_dir

EXTS = (".hwp", ".hwpx")
EXE_NAME = "rhwp.exe" if os.name == "nt" else "rhwp"
MAX_FILE = 128 << 20
FIRST_PAGE_TIMEOUT = 240
PAGE_TIMEOUT = 300
CACHE_LIMIT = 600 << 20

#: user configuration, set by the preview pane from the settings
CONFIG = {"enabled": True, "path": "", "webfonts": False}


def configure(path: str = "", webfonts: bool = False, enabled: bool = True) -> None:
    CONFIG["path"] = path or ""
    CONFIG["webfonts"] = bool(webfonts)
    CONFIG["enabled"] = bool(enabled)


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_exe() -> str:
    """Path of the rhwp executable, or "" when none is installed."""
    cands: list[str] = []
    for p in (os.environ.get("RHWP_EXE", ""), CONFIG["path"]):
        if p:
            cands.append(p)
    here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _root()
    meipass = getattr(sys, "_MEIPASS", "")
    for base in filter(None, (here, meipass)):
        cands += [os.path.join(base, "rhwp", EXE_NAME), os.path.join(base, EXE_NAME),
                  os.path.join(base, "vendor", "rhwp", EXE_NAME)]
    cands += [os.path.join(app_dir(), "rhwp", EXE_NAME)]
    for c in cands:
        if os.path.isfile(c):
            return c
    return shutil.which("rhwp") or ""


def available() -> bool:
    return bool(find_exe())


def cache_root() -> str:
    return os.path.join(app_dir(), "cache", "rhwp")


def _key(path: str, exe: str, webfonts: bool) -> str:
    st = os.stat(path)
    ex = os.stat(exe)
    raw = f"{os.path.normcase(os.path.abspath(path))}|{st.st_mtime_ns}|{st.st_size}|{ex.st_mtime_ns}|{ex.st_size}|{int(webfonts)}"
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _env(webfonts: bool) -> dict:
    env = dict(os.environ)
    if not webfonts:
        # never reach out to a CDN from a file browser: use installed / already cached fonts only
        env["RHWP_WEBFONT_OFFLINE"] = "1"
    return env


def _popen_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _prune_cache(keep: str) -> None:
    """Delete the least recently used document caches once the cache exceeds CACHE_LIMIT."""
    root = cache_root()
    try:
        entries = []
        total = 0
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
            entries.append((os.path.getmtime(d), d, size))
            total += size
        if total <= CACHE_LIMIT:
            return
        for _mt, d, size in sorted(entries):
            if os.path.basename(d) == keep:
                continue
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            if total <= CACHE_LIMIT * 0.8:
                break
    except OSError:
        pass


class Job:
    """One rendering run of a document; ``cancel()`` stops it and kills a running rhwp process."""

    def __init__(self) -> None:
        self.cancelled = False
        self._procs: set = set()
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled = True
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def run(self, cmd: list, env: dict, cwd: str, timeout: float) -> tuple[int, str]:
        """-> (exit code, stdout). Code -1: cancelled, -2: timeout / could not start."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    env=env, cwd=cwd, creationflags=_popen_flags())
        except OSError:
            return -2, ""
        with self._lock:
            self._procs.add(proc)
        t0 = time.monotonic()
        box: list = []
        try:
            reader = threading.Thread(target=_drain, args=(proc, box), daemon=True)
            reader.start()
            while reader.is_alive():
                reader.join(0.2)
                if self.cancelled:
                    proc.kill()
                    return -1, ""
                if time.monotonic() - t0 > timeout:
                    proc.kill()
                    return -2, ""
            out = box[0] if box else b""
            proc.wait(5)
            return proc.returncode, out.decode("utf-8", "replace")
        finally:
            with self._lock:
                self._procs.discard(proc)
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            reader.join(2)  # the pipe closes once the process is gone
            try:
                proc.stdout.close()
            except OSError:
                pass


def _drain(proc, box: list) -> None:
    try:
        box.append(proc.stdout.read())
    except (OSError, ValueError):
        box.append(b"")


class RhwpDoc:
    """A document rendered page by page: ``page_file(i)`` exists once page ``i`` has been rendered."""

    def __init__(self, path: str, exe: str, webfonts: bool, directory: str, pages: int) -> None:
        self.path, self.exe, self.webfonts = path, exe, webfonts
        self.dir = directory
        self.pages = pages

    def page_file(self, i: int) -> str:
        return os.path.join(self.dir, f"p{i}.pdf")

    def has(self, i: int) -> bool:
        return os.path.isfile(self.page_file(i))

    def render(self, i: int, job: Job, timeout: float = PAGE_TIMEOUT) -> bool:
        if self.has(i):
            return True
        part = os.path.join(self.dir, f"p{i}.part-{os.getpid()}-{threading.get_ident()}.pdf")
        code, _out = job.run([self.exe, "export-pdf", self.path, "-p", str(i), "-o", part], _env(self.webfonts),
                             self.dir, timeout)
        ok = code == 0 and os.path.isfile(part) and os.path.getsize(part) > 100
        try:
            if ok:
                os.replace(part, self.page_file(i))
            elif os.path.isfile(part):
                os.remove(part)
        except OSError:
            ok = self.has(i)
        return ok


def open_doc(path: str, job: Job | None = None) -> RhwpDoc | None:
    """Render page 1 and return the document, or None when rhwp is unavailable or cannot read the file."""
    exe = find_exe() if CONFIG["enabled"] else ""
    if not exe:
        return None
    try:
        if os.path.getsize(path) > MAX_FILE:
            return None
        webfonts = bool(CONFIG["webfonts"])
        key = _key(path, exe, webfonts)
        d = os.path.join(cache_root(), key)
        os.makedirs(d, exist_ok=True)
        os.utime(d)
    except OSError:
        return None
    job = job or Job()
    meta = os.path.join(d, "meta.json")
    pages = 0
    try:
        with open(meta, encoding="utf-8") as fh:
            pages = int(json.load(fh).get("pages", 0))
    except (OSError, ValueError, TypeError):
        pages = 0
    doc = RhwpDoc(path, exe, webfonts, d, pages)
    if pages and doc.has(0):
        return doc
    part = os.path.join(d, f"p0.part-{os.getpid()}.pdf")
    code, out = job.run([exe, "export-pdf", path, "-p", "0", "--json", "-o", part], _env(webfonts), d, FIRST_PAGE_TIMEOUT)
    if code != 0 or not os.path.isfile(part) or os.path.getsize(part) < 100:
        _discard(part)
        return None
    pages = _page_count(out)
    if pages < 1:
        _discard(part)
        return None
    try:
        os.replace(part, doc.page_file(0))
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump({"pages": pages, "source": os.path.basename(path)}, fh)
    except OSError:
        return None
    doc.pages = pages
    _prune_cache(key)
    return doc


def _discard(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def _page_count(stdout: str) -> int:
    """pageCount from the ``--json`` manifest (the last JSON object on stdout)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return int(json.loads(line).get("pageCount", 0))
            except (ValueError, TypeError):
                break
    m = re.search(r'"pageCount"\s*:\s*(\d+)', stdout)
    return int(m.group(1)) if m else 0
