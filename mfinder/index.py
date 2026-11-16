"""In-memory file index (struct-of-arrays) with an on-disk database."""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import zlib
from array import array
from typing import Callable, Iterable, Optional

FILE_ATTRIBUTE_DIRECTORY = 0x10
MAGIC = b"FFDB"
DB_VERSION = 2


class DatabaseError(Exception):
    pass


class Index:
    """All entries live in parallel arrays; an entry id is its position.

    Deleted entries are tombstoned in ``alive`` and dropped on the next save.
    ``mtime == 0.0`` means size/date metadata has not been fetched yet,
    ``mtime == -1.0`` means the fetch failed. Directory ``size`` is -1 until
    :meth:`compute_folder_sizes` has run.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.rev = 0
        self.meta_rev = 0
        self.names: list[str] = []
        self.lower: list[str] = []
        self.parent = array("i")
        self.attrs = array("I")
        self.size = array("q")
        self.mtime = array("d")
        self.ctime = array("d")
        self.atime = array("d")
        self.frn = array("Q")
        self.vol = array("H")
        self.alive = bytearray()
        self.roots: list[dict] = []
        self._frn_maps: list[dict[int, int]] = []
        self._frn_maps_ready = True
        self.folder_sizes_ready = False
        self.folder_sizes_rev = -1
        self.extra: dict = {}  # persisted build info (e.g. file lists used)
        self.sorted_by_name = False
        self.num_dirs = 0
        self.num_roots = 0
        self._sorted_rev = -1
        self._needs_save = False
        self._name_order: Optional[list[int]] = None
        self._name_order_rev = -1
        self._dir_paths: dict[int, str] = {}
        self._live_count_cache = 0
        self._live_count_rev = -1
        self._has_child: Optional[bytearray] = None
        self._has_child_rev = -1
        self._children: Optional[dict[int, dict[str, int]]] = None  # built on demand, then kept current

    # ---- basic access -------------------------------------------------

    @property
    def frn_maps(self) -> list[dict[int, int]]:
        if not self._frn_maps_ready:
            self.ensure_frn_maps()
        return self._frn_maps

    @frn_maps.setter
    def frn_maps(self, val: list[dict[int, int]]) -> None:
        self._frn_maps = val
        self._frn_maps_ready = True

    def ensure_frn_maps(self) -> list[dict[int, int]]:
        if self._frn_maps_ready:
            return self._frn_maps
        with self.lock:
            if self._frn_maps_ready:
                return self._frn_maps
            maps: list[dict[int, int]] = [{} for _ in self.roots]
            ntfs_vols = {v for v, r in enumerate(self.roots) if r.get("kind") == "ntfs"}
            if ntfs_vols:
                frn, vol, alive = self.frn, self.vol, self.alive
                for i in range(len(self.names)):
                    if alive[i]:
                        f = frn[i]
                        if f:
                            v = vol[i]
                            if v in ntfs_vols:
                                maps[v][f] = i
            self._frn_maps = maps
            self._frn_maps_ready = True
            return maps

    def __len__(self) -> int:
        return len(self.names)

    def live_count(self) -> int:
        if self._live_count_rev != self.rev:
            self._live_count_cache = self.alive.count(1)
            self._live_count_rev = self.rev
        return self._live_count_cache

    def is_dir(self, i: int) -> bool:
        return bool(self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY)

    def add_root(self, path: str, kind: str, name: Optional[str] = None) -> tuple[int, int]:
        """Register an indexed root; returns (volume index, root entry id)."""
        name = name if name is not None else (path.rstrip("\\/") or path)
        with self.lock:
            vol = len(self.roots)
            self.roots.append({"path": path, "kind": kind, "root_id": -1, "journal_id": 0, "next_usn": 0})
            self._frn_maps.append({})
            rid = self.add(name, -1, FILE_ATTRIBUTE_DIRECTORY, mtime=-1.0, vol=vol)  # roots have no metadata to fetch
            self.roots[vol]["root_id"] = rid
        return vol, rid

    def add(
        self,
        name: str,
        parent: int,
        attrs: int,
        size: int = -1,
        mtime: float = 0.0,
        frn: int = 0,
        vol: int = 0,
        ctime: float = 0.0,
        atime: float = 0.0,
    ) -> int:
        i = len(self.names)
        self.names.append(name)
        self.lower.append(name.lower())
        self.parent.append(parent)
        self.attrs.append(attrs)
        self.size.append(size)
        self.mtime.append(mtime)
        self.ctime.append(ctime)
        self.atime.append(atime)
        self.frn.append(frn)
        self.vol.append(vol)
        self.alive.append(1)
        if self._children is not None and parent >= 0:
            self._children.setdefault(parent, {})[self.lower[i]] = i
        return i

    def relocate(self, i: int, name: str, parent: int) -> None:
        """Rename and/or move an entry, keeping the children map consistent."""
        with self.lock:
            if self._children is not None:
                d = self._children.get(self.parent[i])
                if d and d.get(self.lower[i]) == i:
                    del d[self.lower[i]]
            self.names[i] = name
            self.lower[i] = name.lower()
            self.parent[i] = parent
            if self._children is not None and parent >= 0:
                self._children.setdefault(parent, {})[self.lower[i]] = i
            if self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY:
                self._dir_paths.clear()
            self.rev += 1

    def remove(self, i: int) -> None:
        with self.lock:
            if not self.alive[i]:
                return
            self.alive[i] = 0
            if self._children is not None:
                d = self._children.get(self.parent[i])
                if d and d.get(self.lower[i]) == i:
                    del d[self.lower[i]]
            if self._frn_maps_ready:
                v = self.vol[i]
                fm = self._frn_maps[v] if v < len(self._frn_maps) else None
                if fm is not None and self.frn[i] and fm.get(self.frn[i]) == i:
                    del fm[self.frn[i]]
            if self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY:
                self._dir_paths.clear()
            self.rev += 1

    def remove_tree(self, i: int) -> int:
        """Tombstone an entry and everything below it. Returns how many were removed."""
        with self.lock:
            kids = self.children_map()
            stack, n = [i], 0
            while stack:
                cur = stack.pop()
                if self.alive[cur]:
                    self.remove(cur)
                    n += 1
                sub = kids.get(cur)
                if sub:
                    stack.extend(sub.values())
            return n

    def touch(self) -> None:
        self.rev += 1
        self.meta_rev += 1

    def touch_meta(self) -> None:
        self.meta_rev += 1

    # ---- paths ---------------------------------------------------------

    def path_of(self, i: int) -> str:
        parts = []
        parent = self.parent
        names = self.names
        while i >= 0:
            parts.append(names[i])
            i = parent[i]
        parts.reverse()
        return "\\".join(parts)

    def full_path(self, i: int) -> str:
        p = self.path_of(i)
        if self.parent[i] < 0 and len(p) == 2 and p[1] == ":":
            p += "\\"
        return p

    def parent_path(self, i: int) -> str:
        p = self.parent[i]
        if p < 0:
            return ""
        res = self._dir_paths.get(p)
        if res is None:
            res = self.full_path(p)
            if len(self._dir_paths) > 65536:
                self._dir_paths.clear()
            self._dir_paths[p] = res
        return res

    # ---- children lookup (live watcher, FTP listing) ---------------------

    def children_map(self) -> dict[int, dict[str, int]]:
        """parent id -> {lower name -> id} for live entries (built once, then maintained)."""
        with self.lock:
            if self._children is None:
                m: dict[int, dict[str, int]] = {}
                parent, lower, alive = self.parent, self.lower, self.alive
                for i in range(len(parent)):
                    p = parent[i]
                    if p >= 0 and alive[i]:
                        d = m.get(p)
                        if d is None:
                            d = m[p] = {}
                        d[lower[i]] = i
                self._children = m
            return self._children

    def child_of(self, parent: int, name: str) -> Optional[int]:
        return self.children_map().get(parent, {}).get(name.lower())

    def resolve(self, path: str) -> Optional[int]:
        """Entry id for an absolute path that lies under an indexed root, else None."""
        p = path.replace("/", "\\")
        low = p.lower().rstrip("\\")
        best = None
        for r in self.roots:
            rp = r["path"].rstrip("\\/").lower()
            if (low == rp or low.startswith(rp + "\\")) and (best is None or len(rp) > len(best[0])):
                best = (rp, r)
        if best is None:
            return None
        rp, r = best
        cur = r["root_id"]
        rest = p.rstrip("\\")[len(rp):].strip("\\")
        for part in filter(None, rest.split("\\")):
            cur = self.child_of(cur, part)
            if cur is None:
                return None
        return cur if self.alive[cur] else None

    # ---- lazy metadata (size / dates) -------------------------------------

    def meta(self, i: int) -> tuple[int, float]:
        if self.mtime[i] == 0.0:
            try:
                st = os.stat(self.full_path(i))
                if not self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY:
                    self.size[i] = st.st_size
                self.ctime[i] = getattr(st, "st_birthtime", st.st_ctime)
                self.atime[i] = st.st_atime
                self.mtime[i] = st.st_mtime or 0.001
            except (OSError, ValueError):
                self.mtime[i] = -1.0
        return self.size[i], self.mtime[i]

    def invalidate_meta(self, i: int) -> None:
        self.mtime[i] = 0.0
        if not self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY:
            self.size[i] = -1

    def unfetched(self) -> int:
        return self.mtime.count(0.0)

    def ensure_meta(
        self,
        ids: Iterable[int],
        cancel: Optional[threading.Event] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        mtime = self.mtime
        todo = [i for i in ids if mtime[i] == 0.0]
        total = len(todo)
        for n, i in enumerate(todo):
            if n % 2048 == 0:
                if cancel is not None and cancel.is_set():
                    return False
                if progress is not None:
                    progress(n, total)
            self.meta(i)
        return True

    # ---- folder sizes ------------------------------------------------------

    def compute_folder_sizes(self, cancel: Optional[threading.Event] = None) -> bool:
        """Sum file sizes into every ancestor folder (Kahn-style bottom-up pass)."""
        with self.lock:
            rev = self.rev
            n = len(self.names)
            parent = array("i", self.parent)
            attrs = array("I", self.attrs)
            size = array("q", self.size)
            alive = bytes(self.alive)
            complete = self.mtime.count(0.0) <= min(5000, n // 20)
        pend = array("i", [0]) * n
        total = array("q", [0]) * n
        for i in range(n):
            if alive[i]:
                p = parent[i]
                if p >= 0:
                    pend[p] += 1
                if not attrs[i] & FILE_ATTRIBUTE_DIRECTORY and size[i] > 0:
                    total[i] = size[i]
        stack = [i for i in range(n) if alive[i] and pend[i] == 0]
        done = 0
        while stack:
            i = stack.pop()
            p = parent[i]
            if p >= 0:
                total[p] += total[i]
                pend[p] -= 1
                if pend[p] == 0:
                    stack.append(p)
            done += 1
            if done % 100000 == 0:
                if cancel is not None and cancel.is_set():
                    return False
                time.sleep(0)
        with self.lock:
            m = min(n, len(self.names))
            for i in range(m):
                if alive[i] and attrs[i] & FILE_ATTRIBUTE_DIRECTORY and self.alive[i]:
                    self.size[i] = total[i]
            self.folder_sizes_ready = complete
            self.folder_sizes_rev = rev
            self.meta_rev += 1
        return True

    def child_flags(self) -> bytearray:
        """bytearray with 1 for every entry that has at least one live child."""
        with self.lock:
            if self._has_child is None or self._has_child_rev != self.rev:
                has = bytearray(len(self.names))
                parent = self.parent
                alive = self.alive
                for i in range(len(parent)):
                    p = parent[i]
                    if p >= 0 and alive[i]:
                        has[p] = 1
                self._has_child = has
                self._has_child_rev = self.rev
            return self._has_child

    # ---- ordering & persistence ----------------------------------------

    def _sorted_order(self) -> tuple[list[int], int, int]:
        """Return (keep_ids, num_roots, num_dirs) ordered as: roots, sorted dirs, sorted files."""
        n = len(self.names)
        alive, parent, attrs, lower = self.alive, self.parent, self.attrs, self.lower
        root_ids = [r["root_id"] for r in self.roots if 0 <= r["root_id"] < n and alive[r["root_id"]]]
        root_set = set(root_ids)
        extra_roots = [i for i in range(n) if alive[i] and parent[i] < 0 and i not in root_set]
        dirs = [i for i in range(n) if alive[i] and parent[i] >= 0 and (attrs[i] & FILE_ATTRIBUTE_DIRECTORY)]
        files = [i for i in range(n) if alive[i] and parent[i] >= 0 and not (attrs[i] & FILE_ATTRIBUTE_DIRECTORY)]
        dirs.sort(key=lower.__getitem__)
        files.sort(key=lower.__getitem__)
        return root_ids + extra_roots + dirs + files, len(root_ids) + len(extra_roots), len(dirs)

    def sort_in_place(self) -> None:
        """Compact tombstoned entries and order live entries before the index is published."""
        with self.lock:
            n = len(self.names)
            if self.sorted_by_name and self._sorted_rev == self.rev and self.alive.count(0) == 0:
                return
            keep, num_roots, num_dirs = self._sorted_order()
            parent, attrs, lower = self.parent, self.attrs, self.lower
            remap = array("i", [-1]) * n
            for new_i, old_i in enumerate(keep):
                remap[old_i] = new_i
            self.names = [self.names[i] for i in keep]
            self.lower = [lower[i] for i in keep]
            self.parent = array("i", (remap[parent[i]] if parent[i] >= 0 else -1 for i in keep))
            self.attrs = array("I", (attrs[i] for i in keep))
            self.size = array("q", (self.size[i] for i in keep))
            self.mtime = array("d", (self.mtime[i] for i in keep))
            self.ctime = array("d", (self.ctime[i] for i in keep))
            self.atime = array("d", (self.atime[i] for i in keep))
            self.frn = array("Q", (self.frn[i] for i in keep))
            self.vol = array("H", (self.vol[i] for i in keep))
            count = len(keep)
            self.alive = bytearray(b"\x01") * count
            for r in self.roots:
                if 0 <= r["root_id"] < n:
                    r["root_id"] = remap[r["root_id"]]
            self._frn_maps_ready = False
            self._children = None
            self._has_child = None
            self._dir_paths.clear()
            self.sorted_by_name = True
            self.num_roots = num_roots
            self.num_dirs = num_dirs
            self._sorted_rev = self.rev
            self._name_order = list(range(num_roots, count))
            self._name_order_rev = self.rev
            self._live_count_cache = count
            self._live_count_rev = self.rev

    def name_order(self) -> list[int]:
        """Return live non-root entry ids sorted by (folders first, lower-case name ascending)."""
        if self._name_order is not None and self._name_order_rev == self.rev:
            return self._name_order
        with self.lock:
            if self._name_order is not None and self._name_order_rev == self.rev:
                return self._name_order
            if self.sorted_by_name and self._sorted_rev == self.rev:
                self._name_order = list(range(self.num_roots, len(self.names)))
                self._name_order_rev = self.rev
                return self._name_order
            n = len(self.names)
            alive, parent, attrs, lower = self.alive, self.parent, self.attrs, self.lower
            dirs = [i for i in range(n) if alive[i] and parent[i] >= 0 and (attrs[i] & FILE_ATTRIBUTE_DIRECTORY)]
            files = [i for i in range(n) if alive[i] and parent[i] >= 0 and not (attrs[i] & FILE_ATTRIBUTE_DIRECTORY)]
            dirs.sort(key=lower.__getitem__)
            files.sort(key=lower.__getitem__)
            self.num_dirs = len(dirs)
            self._name_order = dirs + files
            self._name_order_rev = self.rev
            return self._name_order

    def save(self, path: str) -> None:
        if array("i").itemsize != 4 or array("I").itemsize != 4:
            raise DatabaseError("unsupported platform array widths")
        with self.lock:
            n = len(self.names)
            if self.sorted_by_name and self._sorted_rev == self.rev and self.alive.count(0) == 0:
                names = list(self.names)
                col_bytes = [
                    self.parent.tobytes(),
                    self.attrs.tobytes(),
                    self.size.tobytes(),
                    self.mtime.tobytes(),
                    self.ctime.tobytes(),
                    self.atime.tobytes(),
                    self.frn.tobytes(),
                    self.vol.tobytes(),
                ]
                roots = [dict(r) for r in self.roots]
                num_roots, num_dirs = self.num_roots, self.num_dirs
            else:
                keep, num_roots, num_dirs = self._sorted_order()
                remap = array("i", [-1]) * n
                for new_i, old_i in enumerate(keep):
                    remap[old_i] = new_i
                names = [self.names[i] for i in keep]
                parent = array("i", (remap[self.parent[i]] if self.parent[i] >= 0 else -1 for i in keep))
                col_bytes = [parent.tobytes()] + [
                    array(code, (arr[i] for i in keep)).tobytes()
                    for code, arr in (
                        ("I", self.attrs), ("q", self.size), ("d", self.mtime), ("d", self.ctime),
                        ("d", self.atime), ("Q", self.frn), ("H", self.vol),
                    )
                ]
                roots = [dict(r, root_id=remap[r["root_id"]] if 0 <= r["root_id"] < n else -1) for r in self.roots]
            meta_dict = {
                "roots": roots,
                "folder_sizes": self.folder_sizes_ready,
                "extra": self.extra,
                "sorted": True,
                "num_roots": num_roots,
                "num_dirs": num_dirs,
            }
        count = len(names)
        name_blob = "\0".join(names).encode("utf-8", "surrogatepass")
        body = b"".join([struct.pack("<II", count, len(name_blob)), name_blob] + col_bytes)
        meta = json.dumps(meta_dict).encode("utf-8")
        compressed = zlib.compress(body, 1)
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            with open(tmp, "wb") as f:
                f.write(MAGIC)
                f.write(struct.pack("<II", DB_VERSION, len(meta)))
                f.write(meta)
                f.write(compressed)
            os.replace(tmp, path)
            self._needs_save = False
        except OSError as e:
            raise DatabaseError(f"Failed to write database: {e}") from e

    @classmethod
    def load(cls, path: str) -> "Index":
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise DatabaseError("Database file does not exist") from None
        except OSError as e:
            raise DatabaseError(f"Failed to read database: {e}") from e
        if len(data) < 12 or data[:4] != MAGIC:
            raise DatabaseError("Not an M-Finder database")
        version, meta_len = struct.unpack_from("<II", data, 4)
        if version != DB_VERSION:
            raise DatabaseError(f"Unsupported database version {version:08x}")
        try:
            meta = json.loads(data[12 : 12 + meta_len].decode("utf-8"))
            body = zlib.decompress(data[12 + meta_len :])
            count, name_len = struct.unpack_from("<II", body, 0)
            pos = 8
            name_blob = body[pos : pos + name_len]
            pos += name_len
            names = name_blob.decode("utf-8", "surrogatepass").split("\0") if count else []
            if len(names) != count:
                raise ValueError("name count mismatch")

            def take(code: str):
                nonlocal pos
                a = array(code)
                nbytes = a.itemsize * count
                chunk = body[pos : pos + nbytes]
                if len(chunk) != nbytes:
                    raise ValueError("truncated arrays")
                a.frombytes(chunk)
                pos += nbytes
                return a

            idx = cls()
            idx.names = names
            idx.lower = [n.lower() for n in names]
            idx.parent = take("i")
            idx.attrs = take("I")
            idx.size = take("q")
            idx.mtime = take("d")
            idx.ctime = take("d")
            idx.atime = take("d")
            idx.frn = take("Q")
            idx.vol = take("H")
            idx.alive = bytearray(b"\x01") * count
            idx.roots = meta["roots"]
            idx.folder_sizes_ready = bool(meta.get("folder_sizes"))
            if idx.folder_sizes_ready:
                idx.folder_sizes_rev = idx.rev
            idx.extra = meta.get("extra") or {}
            idx._live_count_cache = count
            idx._live_count_rev = idx.rev
            idx._frn_maps = [{} for _ in idx.roots]
            idx._frn_maps_ready = False
            if meta.get("sorted"):
                idx.sorted_by_name = True
                idx.num_roots = int(meta.get("num_roots", sum(1 for r in idx.roots if r.get("root_id", -1) >= 0)))
                idx.num_dirs = int(meta.get("num_dirs", 0))
                idx._sorted_rev = idx.rev
                idx._name_order = list(range(idx.num_roots, count))
                idx._name_order_rev = idx.rev
            else:
                idx.sort_in_place()
                idx._needs_save = True
        except (ValueError, KeyError, struct.error, zlib.error) as e:
            raise DatabaseError("Database is corrupt") from e
        return idx
