"""Engine: owns the index, builds/loads it and keeps it up to date."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional

from . import filelist, mftparse, ntfs, query
from .index import DatabaseError, Index
from .scanner import Excludes, scan_folder
from .settings import Settings
from .watcher import FolderWatcher


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _filelist_stamp(path: str) -> list:
    try:
        return [path, os.path.getmtime(path)]
    except OSError:
        return [path, 0]


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.index = Index()
        self.monitors: list = []
        self.status = "Idle"
        self.building = False
        self.filling = False
        self.ready = False  # an index is loaded or built and can be searched
        self._cancel = threading.Event()
        self._fill_cancel = threading.Event()
        self._build_lock = threading.Lock()
        self._save_lock = threading.Lock()  # the fill worker, maintenance thread and shutdown all save
        self._bg_stop = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        self._last_save_rev = -1
        self.on_status: Optional[Callable[[str], None]] = None

    # ---- helpers ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status = text
        if self.on_status is not None:
            self.on_status(text)

    def excludes(self) -> Excludes:
        s = self.settings
        return Excludes(s.exclude_hidden, s.exclude_system, list(s.exclude_folders), list(s.exclude_patterns))

    def configured_roots(self) -> list[str]:
        return list(self.settings.roots) or ntfs.fixed_drives()

    def open_access(self, root: str):
        """Privileged volume access: direct handle if elevated, else the M-Finder service."""
        try:
            return ntfs.LocalVolume(root)
        except PermissionError:
            if not self.settings.use_service:
                raise
            from .service import ServiceVolume

            return ServiceVolume(root)

    # ---- load / build ----------------------------------------------------

    def _expected_stamps(self) -> list:
        return [_filelist_stamp(p) for p in self.settings.filelists]

    def load(self) -> bool:
        """Load the saved database. Returns False when it is missing or unusable."""
        try:
            idx = Index.load(self.settings.database_file)
        except DatabaseError as e:
            self._set_status(str(e))
            return False
        built = [r["path"].lower() for r in idx.roots if r["kind"] != "filelist"]
        if built != [r.lower() for r in self.configured_roots()] or idx.extra.get("filelists", []) != self._expected_stamps():
            self._set_status("Index locations changed")
            return False
        self.stop_monitors()
        self.index = idx
        self._last_save_rev = idx.rev
        self.ready = True
        self._set_status(f"{idx.live_count():,} objects (database loaded)")
        return True

    def rebuild(self) -> bool:
        """Re-index every configured root and swap the result in. Blocking."""
        if not self._build_lock.acquire(blocking=False):
            return False
        self.building = True
        self._cancel.clear()
        try:
            new = Index()
            total = [0]
            excludes = self.excludes()

            def prog(n: int, label: str = "") -> None:
                self._set_status(f"Indexing {label}... {total[0] + n:,} objects")

            for root in self.configured_roots():
                if self._cancel.is_set():
                    return False
                done = False
                if self.settings.use_mft and ntfs.is_ntfs_root(root):
                    before = len(new)
                    try:
                        access = self.open_access(root)
                        try:
                            ntfs.index_volume(new, root, excludes, lambda n, r=root: prog(n, r), self._cancel, access)
                        finally:
                            access.close()
                        done = True
                    except OSError:
                        # nothing is added to the index before the MFT read succeeds
                        done = False
                    total[0] += len(new) - before
                if not done:
                    before = len(new)
                    scan_folder(new, root, excludes, lambda n, r=root: prog(n, r), self._cancel)
                    total[0] += len(new) - before
            for fl in self.settings.filelists:
                try:
                    filelist.import_filelist(new, fl, excludes)
                except OSError as e:
                    self._set_status(f"Cannot read file list {fl}: {e}")
            new.extra["filelists"] = self._expected_stamps()
            if self._cancel.is_set():
                return False
            self.stop_monitors()
            self._fill_cancel.set()
            self.index = new
            self.ready = True
            self._set_status(f"{new.live_count():,} objects")
            self.save()
            self.start_monitors()
            self.start_background()
            return True
        finally:
            self.building = False
            self._build_lock.release()

    def cancel_build(self) -> None:
        self._cancel.set()

    def save(self, only_if_dirty: bool = False) -> None:
        with self._save_lock:
            if only_if_dirty and self.index.rev == self._last_save_rev:
                return
            try:
                self.index.save(self.settings.database_file)
                self._last_save_rev = self.index.rev
            except DatabaseError as e:
                self._set_status(str(e))

    # ---- background metadata / folder sizes -----------------------------------

    def start_background(self) -> None:
        """Fill size/date from $MFT, compute folder sizes, and keep them fresh."""
        self._fill_cancel.set()
        cancel = self._fill_cancel = threading.Event()
        threading.Thread(target=self._fill_worker, args=(self.index, cancel), daemon=True, name="fill").start()
        if self._bg_thread is None or not self._bg_thread.is_alive():
            self._bg_stop.clear()
            self._bg_thread = threading.Thread(target=self._maintenance, daemon=True, name="maintenance")
            self._bg_thread.start()

    def _fill_worker(self, index: Index, cancel: threading.Event) -> None:
        self.filling = True
        try:
            changed = False
            start_rev = index.rev
            for vol, r in enumerate(index.roots):
                if r["kind"] != "ntfs" or cancel.is_set():
                    continue
                if not any(index.mtime[i] == 0.0 for i in index.frn_maps[vol].values()):
                    continue
                try:
                    access = self.open_access(r["path"])
                except OSError:
                    continue
                try:
                    if mftparse.fill_metadata(index, vol, cancel, access=access):
                        changed = True
                except (OSError, mftparse.MftError):
                    pass
                finally:
                    access.close()
            if cancel.is_set():
                return
            left = [i for i in range(len(index)) if index.alive[i] and index.mtime[i] == 0.0 and index.roots[index.vol[i]]["kind"] != "filelist"]
            if left and len(left) <= 200000:  # records the $MFT parser skipped (extension records)
                if index.ensure_meta(left, cancel):
                    changed = True
            if self.settings.folder_sizes and not cancel.is_set() and (
                not index.folder_sizes_ready or index.folder_sizes_rev != index.rev
            ):
                if index.compute_folder_sizes(cancel):
                    changed = True
            if not cancel.is_set() and (changed or index.rev != start_rev):
                index.touch()
                if index is self.index:
                    self.save()
        finally:
            self.filling = False

    def _maintenance(self) -> None:
        """Every 30 s: refresh folder sizes after changes; every 10 min: persist the database."""
        last_save = time.time()
        while not self._bg_stop.wait(30):
            idx = self.index
            if self.building or self.filling:
                continue
            if self.settings.folder_sizes and idx.folder_sizes_rev != idx.rev:
                idx.compute_folder_sizes(self._bg_stop)
            if time.time() - last_save > 600 and idx.rev != self._last_save_rev and len(idx):
                self.save()
                last_save = time.time()

    # ---- live monitoring -------------------------------------------------

    def start_monitors(self) -> None:
        self.stop_monitors()
        if not self.settings.monitor:
            return
        ex = self.excludes()
        for vol, root in enumerate(self.index.roots):
            if root["kind"] == "ntfs":
                m = ntfs.UsnMonitor(self.index, vol, ex, on_stale=self._stale, open_access=self.open_access)
            elif root["kind"] == "folder" and self.settings.monitor_folders and sys.platform == "win32":
                m = FolderWatcher(self.index, vol, ex, on_stale=self._stale)
            else:
                continue
            m.start()
            self.monitors.append(m)

    def stop_monitors(self) -> None:
        for m in self.monitors:
            m.stop()
        for m in self.monitors:
            m.join(timeout=2)
        self.monitors = []

    def _stale(self, vol: int) -> None:
        # The journal wrapped / events were lost while we were away: only a re-index is safe.
        threading.Thread(target=self.rebuild, daemon=True).start()

    def shutdown(self) -> None:
        self.cancel_build()
        self._fill_cancel.set()
        self._bg_stop.set()
        self.stop_monitors()
        if len(self.index):
            self.save(only_if_dirty=True)

    # ---- searching -------------------------------------------------------

    def flags(self) -> query.Flags:
        s = self.settings
        return query.Flags(case=s.match_case, ww=s.match_whole_word, path=s.match_path, regex=s.regex)

    def search(
        self,
        text: str,
        sort_key: Optional[str] = None,
        ascending: Optional[bool] = None,
        cancel: Optional[threading.Event] = None,
        flags: Optional[query.Flags] = None,
        filter_name: Optional[str] = None,
    ) -> Optional[list[int]]:
        s = self.settings
        ftext, fflags = "", None
        name = s.current_filter if filter_name is None else filter_name
        f = s.find_filter(name) if name else None
        if f and f.get("search"):
            ftext = f["search"]
            fflags = query.Flags(case=f.get("case", False), ww=f.get("ww", False), path=f.get("path", False), regex=f.get("regex", False))
        ids = query.search(self.index, text, flags or self.flags(), cancel, ftext, fflags, s.content_max_mb << 20)
        if ids is None:
            return None
        return query.sort_results(
            self.index, ids, sort_key or s.sort_key, s.sort_ascending if ascending is None else ascending, cancel
        )
