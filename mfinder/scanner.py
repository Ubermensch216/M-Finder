"""Portable directory-walk indexer (used for folders, non-NTFS volumes and as fallback)."""

from __future__ import annotations

import fnmatch
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .index import FILE_ATTRIBUTE_DIRECTORY, Index

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass
class Excludes:
    hidden: bool = False
    system: bool = False
    folders: list[str] = field(default_factory=list)  # full folder paths, prefix match
    patterns: list[str] = field(default_factory=list)  # wildcard filters on the file name

    def __post_init__(self) -> None:
        self._folders = tuple(f.rstrip("\\/").lower() for f in self.folders if f.strip())
        self._patterns = tuple(p.lower() for p in self.patterns if p.strip())

    def attr_excluded(self, attrs: int) -> bool:
        return bool(
            (self.hidden and attrs & FILE_ATTRIBUTE_HIDDEN)
            or (self.system and attrs & FILE_ATTRIBUTE_SYSTEM)
        )

    def name_excluded(self, name: str) -> bool:
        n = name.lower()
        return any(fnmatch.fnmatchcase(n, p) for p in self._patterns)

    def folder_excluded(self, path: str) -> bool:
        p = path.lower()
        return any(p == f or p.startswith(f + "\\") for f in self._folders)

    @property
    def any_folder_rules(self) -> bool:
        return bool(self._folders)


def scan_folder(
    index: Index,
    root_path: str,
    excludes: Optional[Excludes] = None,
    progress: Optional[Callable[[int], None]] = None,
    cancel: Optional[threading.Event] = None,
    kind: str = "folder",
) -> int:
    """Walk ``root_path`` and add everything under it. Returns the root's volume index."""
    vol, root_id = index.add_root(root_path, kind)
    scan_into(index, root_path, root_id, vol, excludes, progress, cancel)
    return vol


def scan_into(
    index: Index,
    dir_path: str,
    dir_id: int,
    vol: int,
    excludes: Optional[Excludes] = None,
    progress: Optional[Callable[[int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> int:
    """Walk ``dir_path`` and add its contents under existing entry ``dir_id``. Returns count added."""
    excludes = excludes or Excludes()
    stack = [(dir_path, dir_id)]
    count = 0
    while stack:
        if cancel is not None and cancel.is_set():
            break
        path, pid = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    attrs |= FILE_ATTRIBUTE_DIRECTORY
                if excludes.attr_excluded(attrs) or excludes.name_excluded(e.name):
                    continue
                child_path = e.path
                if is_dir and excludes.folder_excluded(child_path):
                    continue
                i = index.add(
                    e.name,
                    pid,
                    attrs,
                    -1 if is_dir else st.st_size,
                    st.st_mtime or 0.001,
                    0,
                    vol,
                    getattr(st, "st_birthtime", st.st_ctime),
                    st.st_atime,
                )
                count += 1
                if is_dir and not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    stack.append((child_path, i))
            if progress is not None:
                progress(count)
    return count
