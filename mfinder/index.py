"""In-memory file index (struct-of-arrays) with an on-disk database."""

from __future__ import annotations

import json
import os
import struct
import threading
import zlib
from array import array
from itertools import compress
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
        self.frn_maps: list[dict[int, int]] = []
        self.folder_sizes_ready = False
        self.folder_sizes_rev = -1
        self.extra: dict = {}  # persisted build info (e.g. file lists used)
        self._has_child: Optional[bytearray] = None
        self._has_child_rev = -1
        self._children: Optional[dict[int, dict[str, int]]] = None  # built on demand, then kept current

    # ---- basic access -------------------------------------------------

    def __len__(self) -> int:
        return len(self.names)

    def live_count(self) -> int:
        return self.alive.count(1)

    def is_dir(self, i: int) -> bool:
        return bool(self.attrs[i] & FILE_ATTRIBUTE_DIRECTORY)

    def add_root(self, path: str, kind: str, name: Optional[str] = None) -> tuple[int, int]:
        """Register an indexed root; returns (volume index, root entry id)."""
        name = name if name is not None else (path.rstrip("\\/") or path)
        with self.lock:
            vol = len(self.roots)
            self.roots.append({"path": path, "kind": kind, "root_id": -1, "journal_id": 0, "next_usn": 0})
            self.frn_maps.append({})
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
            v = self.vol[i]
            fm = self.frn_maps[v] if v < len(self.frn_maps) else None
            if fm is not None and self.frn[i] and fm.get(self.frn[i]) == i:
                del fm[self.frn[i]]
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
        return self.full_path(p)

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
            complete = self.mtime.count(0.0) == 0
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
            if done % 100000 == 0 and cancel is not None and cancel.is_set():
                return False
        with self.lock:
            m = min(n, len(self.names))
            for i in range(m):
                if alive[i] and attrs[i] & FILE_ATTRIBUTE_DIRECTORY and self.alive[i]:
                    self.size[i] = total[i]
            self.folder_sizes_ready = complete
            self.folder_sizes_rev = rev
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

    # ---- persistence ---------------------------------------------------

    def _compact_locked(self) -> tuple[list[str], array, list[array], list[dict]]:
        n = len(self.names)
        alive = self.alive
        dead_count = alive.count(0)
        if dead_count == 0:
            cols = [self.attrs, self.size, self.mtime, self.ctime, self.atime, self.frn, self.vol]
            return self.names, self.parent, cols, [dict(r) for r in self.roots]
        col_specs = (("I", self.attrs), ("q", self.size), ("d", self.mtime), ("d", self.ctime),
                     ("d", self.atime), ("Q", self.frn), ("H", self.vol))
        remap = array("i", [-1]) * n
        # Sparse deletions (typical live session): copy contiguous byte spans instead of per-element Python loops.
        if dead_count <= 4096:
            spans = []
            prev = 0
            pos = 0
            while True:
                d = alive.find(0, pos)
                if d < 0:
                    break
                if prev < d:
                    spans.append((prev, d))
                prev = d + 1
                pos = prev
            if prev < n:
                spans.append((prev, n))
            names: list[str] = []
            new_pos = 0
            for a, b in spans:
                names.extend(self.names[a:b])
                remap[a:b] = array("i", range(new_pos, new_pos + b - a))
                new_pos += b - a
            mv_p = memoryview(self.parent).cast("B")
            raw_parent = array("i")
            raw_parent.frombytes(b"".join(mv_p[a * 4 : b * 4] for a, b in spans))
            parent = array("i", (remap[p] if p >= 0 else -1 for p in raw_parent))
            cols = []
            for code, arr in col_specs:
                item = arr.itemsize
                mv = memoryview(arr).cast("B")
                out = array(code)
                out.frombytes(b"".join(mv[a * item : b * item] for a, b in spans))
                cols.append(out)
        else:
            new_pos = 0
            for i, ok in enumerate(alive):
                if ok:
                    remap[i] = new_pos
                    new_pos += 1
            names = list(compress(self.names, alive))
            parent = array("i", (remap[p] if p >= 0 else -1 for p in compress(self.parent, alive)))
            cols = [array(code, compress(arr, alive)) for code, arr in col_specs]
        roots = [dict(r, root_id=remap[r["root_id"]]) for r in self.roots]
        return names, parent, cols, roots

    def save(self, path: str) -> None:
        if array("i").itemsize != 4 or array("I").itemsize != 4:
            raise DatabaseError("unsupported platform array widths")
        with self.lock:
            names, parent, cols, roots = self._compact_locked()
            count = len(names)
            name_blob = "\0".join(names).encode("utf-8", "surrogatepass")
            body = b"".join(
                [struct.pack("<II", count, len(name_blob)), name_blob, parent.tobytes()]
                + [c.tobytes() for c in cols]
            )
            meta = json.dumps({"roots": roots, "folder_sizes": self.folder_sizes_ready, "extra": self.extra}).encode("utf-8")
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            with open(tmp, "wb") as f:
                f.write(MAGIC)
                f.write(struct.pack("<II", DB_VERSION, len(meta)))
                f.write(meta)
                f.write(zlib.compress(body, 1))
            os.replace(tmp, path)
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
        except (ValueError, KeyError, struct.error, zlib.error) as e:
            raise DatabaseError("Database is corrupt") from e
        idx.frn_maps = [{} for _ in idx.roots]
        for i in range(count):
            f = idx.frn[i]
            if f:
                v = idx.vol[i]
                if v < len(idx.roots) and idx.roots[v]["kind"] == "ntfs":
                    idx.frn_maps[v][f] = i
        return idx
