"""File lists: import an .efu/.csv/.txt list as an index root, and export results."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Iterable, Optional

from .index import FILE_ATTRIBUTE_DIRECTORY, Index
from .mftparse import EPOCH_DELTA, filetime_to_epoch
from .scanner import Excludes

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d")


def _parse_time(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    if text.isdigit():
        return filetime_to_epoch(int(text))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    return 0.0


def _parse_int(text: str, default: int = -1) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        return default


def read_filelist(path: str) -> Iterable[dict]:
    """Yield {'path','size','mtime','ctime','attrs'} rows from an EFU/CSV or plain-text list."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        head = f.readline()
        f.seek(0)
        if "filename" in head.lower():
            for row in csv.DictReader(f):
                row = {(k or "").strip().lower(): v for k, v in row.items()}
                name = (row.get("filename") or "").strip().strip('"')
                if not name:
                    continue
                attrs = _parse_int(row.get("attributes", ""), 0)
                yield {
                    "path": name,
                    "size": _parse_int(row.get("size", "")),
                    "mtime": _parse_time(row.get("date modified", "")),
                    "ctime": _parse_time(row.get("date created", "")),
                    "attrs": attrs,
                }
        else:
            for line in f:
                line = line.strip().strip('"')
                if line:
                    yield {"path": line, "size": -1, "mtime": 0.0, "ctime": 0.0, "attrs": 0}


def import_filelist(index: Index, path: str, excludes: Optional[Excludes] = None) -> int:
    """Add the list's entries to ``index`` under per-drive/per-server roots. Returns entries added."""
    ex = excludes or Excludes()
    roots: dict[str, tuple[int, int]] = {}
    nodes: dict[str, int] = {}
    added = 0

    def root_for(first: str) -> tuple[int, int]:
        key = first.lower()
        if key not in roots:
            roots[key] = index.add_root(first + "\\", "filelist", name=first)
            nodes[key] = roots[key][1]
        return roots[key]

    def ensure_dir(parts: list[str]) -> int:
        nonlocal added
        vol, cur = root_for(parts[0])
        key = parts[0].lower()
        for part in parts[1:]:
            key += "\\" + part.lower()
            nid = nodes.get(key)
            if nid is None:
                nid = nodes[key] = index.add(part, cur, FILE_ATTRIBUTE_DIRECTORY, -1, -1.0, 0, vol)
                added += 1
            cur = nid
        return cur

    for row in read_filelist(path):
        raw = row["path"].replace("/", "\\").rstrip("\\")
        parts = [p for p in raw.split("\\") if p]
        if raw.startswith("\\\\") and parts:
            parts[0] = "\\\\" + parts[0]
        if not parts:
            continue
        is_dir = bool(row["attrs"] & FILE_ATTRIBUTE_DIRECTORY)
        name = parts[-1]
        if len(parts) > 1 and (ex.attr_excluded(row["attrs"]) or ex.name_excluded(name)):
            continue
        if is_dir:
            ensure_dir(parts)
            continue
        if len(parts) == 1:
            continue
        key = "\\".join(parts).lower()
        if key in nodes:
            continue
        parent = ensure_dir(parts[:-1])
        vol = index.vol[parent]
        nodes[key] = index.add(
            name, parent, row["attrs"], row["size"], row["mtime"] or -1.0, 0, vol, row["ctime"], 0.0
        )
        added += 1
    return added


def _filetime(epoch: float) -> str:
    return str(int(epoch * 10_000_000) + EPOCH_DELTA) if epoch > 0 else ""


def export_results(index: Index, ids: Iterable[int], path: str) -> int:
    """Write results as .efu (FILETIME dates), .txt (paths) or .csv (readable dates)."""
    ext = os.path.splitext(path)[1].lower()
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        if ext == ".txt":
            for i in ids:
                fh.write(index.full_path(i) + "\r\n")
                n += 1
            return n
        w = csv.writer(fh)
        w.writerow(["Filename", "Size", "Date Modified", "Date Created", "Attributes"])
        for i in ids:
            size, mtime = index.meta(i)
            ctime = index.ctime[i]
            if ext == ".efu":
                dm, dc = _filetime(mtime), _filetime(ctime)
            else:
                f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t > 0 else ""  # noqa: E731
                dm, dc = f(mtime), f(ctime)
            w.writerow([index.full_path(i), size if size >= 0 else "", dm, dc, index.attrs[i]])
            n += 1
    return n
