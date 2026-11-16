"""Direct $MFT reader: file size and timestamps without a per-file stat().

``scan_metadata`` yields batches of ``(record_number, size, ctime, mtime, atime)``
for every in-use base record. Only metadata leaves this module — never raw
record bytes — so it is safe for the privileged service to expose it.

Records whose attributes live in extension records ($ATTRIBUTE_LIST) are skipped
and keep falling back to the lazy ``os.stat`` path.
"""

from __future__ import annotations

import struct
import threading
import time
from array import array
from typing import BinaryIO, Iterator, Optional

from .index import FILE_ATTRIBUTE_DIRECTORY, Index

EPOCH_DELTA = 116444736000000000  # FILETIME of 1970-01-01
CHUNK = 4 << 20

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_u64 = struct.Struct("<Q").unpack_from
_times = struct.Struct("<QQQQ").unpack_from


class MftError(OSError):
    pass


def filetime_to_epoch(ft: int) -> float:
    if ft <= 0:
        return 0.0
    return (ft - EPOCH_DELTA) / 10_000_000


def apply_fixups(rec: bytearray, sector: int = 512) -> bool:
    off, cnt = _u16(rec, 4)[0], _u16(rec, 6)[0]
    if cnt < 2 or off + cnt * 2 > len(rec):
        return False
    usn = rec[off : off + 2]
    for k in range(1, cnt):
        end = k * sector
        if end > len(rec):
            return False
        if rec[end - 2 : end] != usn:
            return False  # torn write
        rec[end - 2 : end] = rec[off + k * 2 : off + k * 2 + 2]
    return True


def parse_runs(buf: bytes, pos: int):
    """Decode an NTFS runlist into (lcn, length_in_clusters) pairs (sparse runs skipped)."""
    runs, lcn = [], 0
    while pos < len(buf) and buf[pos]:
        hdr = buf[pos]
        ls, os_ = hdr & 0xF, hdr >> 4
        length = int.from_bytes(buf[pos + 1 : pos + 1 + ls], "little")
        delta = int.from_bytes(buf[pos + 1 + ls : pos + 1 + ls + os_], "little", signed=True)
        pos += 1 + ls + os_
        if os_ == 0:
            continue  # sparse
        lcn += delta
        runs.append((lcn, length))
    return runs


class MftLayout:
    def __init__(self, record_size: int, sector: int, runs: list[tuple[int, int]], cluster: int, data_size: int):
        self.record_size = record_size
        self.sector = sector
        self.runs = runs  # (start byte, length bytes)
        self.cluster = cluster
        self.total_records = data_size // record_size


def read_layout(f: BinaryIO) -> MftLayout:
    f.seek(0)
    boot = f.read(512)
    if len(boot) < 512 or boot[3:11] != b"NTFS    ":
        raise MftError("not an NTFS boot sector")
    bps = _u16(boot, 0x0B)[0]
    spc = boot[0x0D]
    cluster = bps * spc
    mft_lcn = _u64(boot, 0x30)[0]
    cpr = struct.unpack_from("<b", boot, 0x40)[0]
    rec_size = cpr * cluster if cpr > 0 else 1 << (-cpr)
    f.seek(mft_lcn * cluster)
    raw = f.read((rec_size + bps - 1) // bps * bps)
    rec = bytearray(raw[:rec_size])
    if rec[:4] != b"FILE" or not apply_fixups(rec, bps):
        raise MftError("cannot read $MFT record 0")
    pos = _u16(rec, 0x14)[0]
    while pos + 8 <= len(rec):
        atype, alen = _u32(rec, pos)[0], _u32(rec, pos + 4)[0]
        if atype == 0xFFFFFFFF or alen == 0:
            break
        if atype == 0x80 and rec[pos + 8]:  # non-resident $DATA
            run_off = _u16(rec, pos + 0x20)[0]
            data_size = _u64(rec, pos + 0x30)[0]
            runs = [(lcn * cluster, n * cluster) for lcn, n in parse_runs(bytes(rec), pos + run_off)]
            return MftLayout(rec_size, bps, runs, cluster, data_size)
        pos += alen
    raise MftError("$MFT has no non-resident $DATA")


def parse_record(rec: bytearray):
    """(size, ctime, mtime, atime, is_dir) for an in-use base record, else None."""
    flags = _u16(rec, 0x16)[0]
    if not flags & 1 or _u64(rec, 0x20)[0]:
        return None
    pos = _u16(rec, 0x14)[0]
    n = len(rec)
    times = None
    size = -1
    have_data = False
    while pos + 8 <= n:
        atype, alen = _u32(rec, pos)[0], _u32(rec, pos + 4)[0]
        if atype == 0xFFFFFFFF or alen == 0 or pos + alen > n:
            break
        if atype == 0x10 and not rec[pos + 8]:
            co = _u16(rec, pos + 0x14)[0]
            c, m, _mft, a = _times(rec, pos + co)
            times = (filetime_to_epoch(c), filetime_to_epoch(m), filetime_to_epoch(a))
        elif atype == 0x20:
            return None  # attributes spread over extension records
        elif atype == 0x80 and rec[pos + 9] == 0 and not have_data:  # unnamed $DATA
            have_data = True
            size = _u32(rec, pos + 0x10)[0] if not rec[pos + 8] else _u64(rec, pos + 0x30)[0]
        pos += alen
    if times is None:
        return None
    return (-1 if flags & 2 else (size if have_data else 0),) + times + (bool(flags & 2),)


def scan_metadata(
    f: BinaryIO,
    layout: Optional[MftLayout] = None,
    cancel: Optional[threading.Event] = None,
    wanted: Optional[array] = None,
    batch: int = 20000,
) -> Iterator[list[tuple]]:
    """Yield batches of (record_number, size, ctime, mtime, atime); size is -1 for folders.

    ``wanted`` optionally maps record number -> truthy (array of ints) to skip unneeded records.
    """
    lay = layout or read_layout(f)
    rs = lay.record_size
    recno = 0
    out: list[tuple] = []
    per_chunk = max(rs, CHUNK // rs * rs)
    for start, length in lay.runs:
        done = 0
        while done < length:
            if cancel is not None and cancel.is_set():
                return
            n = min(per_chunk, length - done)
            f.seek(start + done)
            buf = f.read(n)
            if not buf:
                break
            for o in range(0, len(buf) - rs + 1, rs):
                r = recno + o // rs
                if wanted is not None and (r >= len(wanted) or wanted[r] < 0):
                    continue
                if buf[o : o + 4] != b"FILE":
                    continue
                rec = bytearray(buf[o : o + rs])
                if not apply_fixups(rec, lay.sector):
                    continue
                p = parse_record(rec)
                if p is not None:
                    out.append((r, p[0], p[1], p[2], p[3]))
                    if len(out) >= batch:
                        yield out
                        out = []
            recno += n // rs
            done += n
            time.sleep(0)  # yield GIL between chunks so UI scrolling stays smooth
    if out:
        yield out


# ---------------------------------------------------------------------------
# Applying results to an Index
# ---------------------------------------------------------------------------

FRN_MASK = 0x0000FFFFFFFFFFFF


def record_map(index: Index, vol: int) -> array:
    """array: record number -> entry id (or -1) for one NTFS volume."""
    fm = index.frn_maps[vol]
    top = max((f & FRN_MASK for f in fm), default=0)
    m = array("i", [-1]) * (top + 1)
    for frn, i in fm.items():
        m[frn & FRN_MASK] = i
    return m


def apply_batch(index: Index, rec2id: array, batch: list[tuple]) -> int:
    n = 0
    size, mtime, ctime, atime, attrs = index.size, index.mtime, index.ctime, index.atime, index.attrs
    for r, sz, ct, mt, at in batch:
        if r >= len(rec2id):
            continue
        i = rec2id[r]
        if i < 0 or not index.alive[i] or mtime[i] != 0.0:
            continue
        if not attrs[i] & FILE_ATTRIBUTE_DIRECTORY:
            size[i] = sz
        ctime[i], atime[i] = ct, at
        mtime[i] = mt or 0.001
        n += 1
    return n


def fill_metadata(
    index: Index,
    vol: int,
    cancel: Optional[threading.Event] = None,
    progress=None,
    access=None,
) -> int:
    """Fill size/date for every entry of NTFS volume ``vol`` by reading its $MFT. Returns entries filled."""
    from .ntfs import LocalVolume

    own = access is None
    if own:
        access = LocalVolume(index.roots[vol]["path"])
    total = 0
    try:
        rec2id = record_map(index, vol)
        for batch in access.scan_metadata(cancel, rec2id):
            with index.lock:
                total += apply_batch(index, rec2id, batch)
            index.touch_meta()
            if progress:
                progress(total)
    finally:
        if own:
            access.close()
    index.touch_meta()
    return total
