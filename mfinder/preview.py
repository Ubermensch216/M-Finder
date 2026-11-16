"""Extract displayable text from a file for the preview pane (standard library only).

Supported: plain text (UTF-8/UTF-16/ANSI auto-detected), Office Open XML
(.docx/.xlsx/.pptx), OpenDocument (.odt/.ods/.odp), HWPX and the binary HWP 5 format.
"""

from __future__ import annotations

import html
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass

MAX_CHARS = 2_000_000  # never hand the GUI more than this
MAX_TEXT_BYTES = 4 << 20  # plain-text files are read only up to this
MAX_ARCHIVE_PART = 64 << 20  # refuse to inflate a single archive member beyond this


@dataclass
class Preview:
    text: str = ""
    kind: str = ""  # human readable type, e.g. "Text (UTF-8)", "HWP"
    truncated: bool = False
    error: str = ""


# ---- helpers -------------------------------------------------------------------


def _clip(text: str, truncated: bool = False) -> Preview:
    if len(text) > MAX_CHARS:
        text, truncated = text[:MAX_CHARS], True
    return Preview(text=text, truncated=truncated)


def _read_part(zf: zipfile.ZipFile, name: str) -> str:
    info = zf.getinfo(name)
    if info.file_size > MAX_ARCHIVE_PART:
        raise ValueError(f"{name} is too large")
    with zf.open(info) as fh:
        return fh.read(MAX_ARCHIVE_PART + 1).decode("utf-8", "replace")


def _xml_text(xml: str, para_end: str, tab: str | None = None) -> str:
    xml = re.sub(para_end, "\n", xml)
    if tab:
        xml = re.sub(tab, "\t", xml)
    xml = re.sub(r"<[^>]*>", "", xml)
    return html.unescape(xml)


from .viewers.common import natural_key as _natural_key


# ---- plain text ------------------------------------------------------------------


def decode_text(raw: bytes) -> tuple[str, str] | None:
    """Return (text, encoding label) or None when the bytes look binary."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "UTF-8"
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace"), "UTF-16LE"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace"), "UTF-16BE"
    sample = raw[:65536]
    if not sample:
        return "", "UTF-8"
    nul = sample.count(0)
    if nul:
        # UTF-16 without BOM: lots of NULs on every other byte
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        if odd > len(sample) * 0.2 and even < odd // 4:
            return raw.decode("utf-16-le", "replace"), "UTF-16LE"
        if even > len(sample) * 0.2 and odd < even // 4:
            return raw.decode("utf-16-be", "replace"), "UTF-16BE"
        return None
    try:
        # a read cut in the middle of a multi-byte character must not make this fail
        raw.decode("utf-8") if len(raw) < MAX_TEXT_BYTES else raw[:-4].decode("utf-8")
        return raw.decode("utf-8", "replace"), "UTF-8"
    except UnicodeDecodeError:
        pass
    for enc in ("mbcs", "cp949"):
        try:
            return raw.decode(enc, "replace"), "ANSI"
        except LookupError:
            continue
    return raw.decode("latin-1"), "ANSI"


def _plain(path: str) -> Preview:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES + 1)
    big = len(raw) > MAX_TEXT_BYTES
    if big:
        raw = raw[:MAX_TEXT_BYTES]
    res = decode_text(raw)
    if res is None:
        return Preview(error="binary")
    text, enc = res
    p = _clip(text.replace("\r\n", "\n").replace("\r", "\n"), big)
    p.kind = f"Text ({enc})"
    return p


# ---- zip based formats --------------------------------------------------------------


def _docx(zf: zipfile.ZipFile) -> str:
    return _xml_text(_read_part(zf, "word/document.xml"), r"</w:p>|<w:br\s*/>|<w:cr\s*/>", r"<w:tab\s*/>")


def _xlsx(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    out = []
    if "xl/sharedStrings.xml" in names:
        xml = _read_part(zf, "xl/sharedStrings.xml")
        for si in re.findall(r"<si\b.*?</si>", xml, re.S):
            out.append(_xml_text(si, r"(?!x)x").strip())
    return "\n".join(out)


def _pptx(zf: zipfile.ZipFile) -> str:
    slides = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural_key)
    out = []
    for n, name in enumerate(slides, 1):
        out.append(f"--- Slide {n} ---")
        out.append(_xml_text(_read_part(zf, name), r"</a:p>").strip())
    return "\n".join(out)


def _hwpx(zf: zipfile.ZipFile) -> str:
    secs = sorted((n for n in zf.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n, re.I)), key=_natural_key)
    return "\n".join(_xml_text(_read_part(zf, n), r"</hp:p>|<hp:lineBreak\s*/>", r"<hp:tab\b[^>]*/>") for n in secs)


def _odf(zf: zipfile.ZipFile) -> str:
    return _xml_text(_read_part(zf, "content.xml"), r"</text:p>|</text:h>|<text:line-break\s*/>", r"<text:tab\s*/>")


_ZIP_KINDS = {
    ".docx": ("Word document", _docx), ".docm": ("Word document", _docx),
    ".xlsx": ("Excel workbook (strings only)", _xlsx), ".xlsm": ("Excel workbook (strings only)", _xlsx),
    ".pptx": ("PowerPoint presentation", _pptx), ".pptm": ("PowerPoint presentation", _pptx),
    ".hwpx": ("HWPX", _hwpx),
    ".odt": ("OpenDocument text", _odf), ".ods": ("OpenDocument spreadsheet", _odf), ".odp": ("OpenDocument presentation", _odf),
}


STRUCTURED_EXTS = frozenset(_ZIP_KINDS) | {".hwp", ".pdf"}


# ---- HWP 5 (OLE compound file) --------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_MAX_SECTORS = 1 << 22


class _Ole:
    """Just enough of the OLE2 compound file format to read named streams."""

    def __init__(self, data: bytes) -> None:
        if data[:8] != _OLE_MAGIC or len(data) < 512:
            raise ValueError("not an OLE file")
        self.d = data
        shift, mshift = struct.unpack_from("<HH", data, 30)
        self.ss, self.mss = 1 << shift, 1 << mshift
        nfat, first_dir, _tx, cutoff, first_mini, nmini, first_dif, ndif = struct.unpack_from("<IIIIIIII", data, 44)
        # offsets 44..75: FAT count, dir start, (txn @52), mini cutoff, miniFAT start/count, DIFAT start/count
        self.cutoff = cutoff
        difat = list(struct.unpack_from("<109I", data, 76))
        sec = first_dif
        for _ in range(min(ndif, 4096)):
            if sec >= _ENDOFCHAIN:
                break
            blk = self._sector(sec)
            vals = struct.unpack_from(f"<{self.ss // 4}I", blk)
            difat.extend(vals[:-1])
            sec = vals[-1]
        self.fat: list[int] = []
        for s in difat[:nfat]:
            if s >= _ENDOFCHAIN:
                break
            self.fat.extend(struct.unpack_from(f"<{self.ss // 4}I", self._sector(s)))
        self.dir = self._parse_dir(self._chain(first_dir))
        self.mini_fat: list[int] = []
        if nmini and first_mini < _ENDOFCHAIN:
            mf = self._chain(first_mini)
            self.mini_fat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf))
        self.mini_stream = self._chain(self.dir[0][3]) if self.dir else b""

    def _sector(self, n: int) -> bytes:
        off = (n + 1) * self.ss
        blk = self.d[off : off + self.ss]
        if len(blk) < self.ss:
            raise ValueError("truncated OLE file")
        return blk

    def _chain(self, start: int) -> bytes:
        out, seen = [], 0
        while start < _ENDOFCHAIN:
            out.append(self._sector(start))
            if start >= len(self.fat):
                break
            start = self.fat[start]
            seen += 1
            if seen > _MAX_SECTORS:
                raise ValueError("corrupt OLE chain")
        return b"".join(out)

    def _mini_chain(self, start: int, size: int) -> bytes:
        out, seen = [], 0
        while start < _ENDOFCHAIN and start < len(self.mini_fat):
            off = start * self.mss
            out.append(self.mini_stream[off : off + self.mss])
            start = self.mini_fat[start]
            seen += 1
            if seen > _MAX_SECTORS:
                raise ValueError("corrupt OLE mini chain")
        return b"".join(out)[:size]

    def _parse_dir(self, raw: bytes) -> list[tuple]:
        """entries: (name, type, child, start, size, left, right)"""
        ents = []
        for off in range(0, len(raw) - 127, 128):
            nlen = struct.unpack_from("<H", raw, off + 64)[0]
            name = raw[off : off + max(nlen - 2, 0)].decode("utf-16-le", "replace")
            typ = raw[off + 66]
            left, right, child = struct.unpack_from("<III", raw, off + 68)
            start = struct.unpack_from("<I", raw, off + 116)[0]
            size = struct.unpack_from("<Q", raw, off + 120)[0]
            ents.append((name, typ, child, start, size, left, right))
        return ents

    def _children(self, idx: int) -> list[int]:
        """All entries in the red-black tree under storage ``idx``."""
        out: list[int] = []
        stack = [self.dir[idx][2]]
        seen = set()
        while stack:
            n = stack.pop()
            if n >= len(self.dir) or n in seen:
                continue
            seen.add(n)
            out.append(n)
            stack.append(self.dir[n][5])
            stack.append(self.dir[n][6])
        return out

    def find(self, *path: str) -> int | None:
        cur = 0
        for part in path:
            for c in self._children(cur):
                if self.dir[c][0].lower() == part.lower():
                    cur = c
                    break
            else:
                return None
        return cur

    def read(self, idx: int) -> bytes:
        _name, _typ, _child, start, size, _l, _r = self.dir[idx]
        if size < self.cutoff:
            return self._mini_chain(start, size)
        return self._chain(start)[:size]

    def listdir(self, *path: str) -> list[str]:
        idx = self.find(*path) if path else 0
        return [] if idx is None else [self.dir[c][0] for c in self._children(idx)]


def _hwp_para_text(rec: bytes) -> str:
    """Decode one PARA_TEXT record (UTF-16LE with embedded control codes)."""
    out = []
    i, n = 0, len(rec) // 2 * 2
    while i < n:
        ch = rec[i] | rec[i + 1] << 8
        if ch >= 32:
            out.append(chr(ch))
            i += 2
        elif ch in (0, 10, 13) or 24 <= ch <= 31:
            if ch in (10, 13):
                out.append("\n")
            i += 2
        else:  # inline/extended control: 16 bytes in total (8 WCHARs)
            if ch == 9:
                out.append("\t")
            i += 16
    return "".join(out)


def _hwp_records(data: bytes):
    pos, n = 0, len(data)
    while pos + 4 <= n:
        (hdr,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, size = hdr & 0x3FF, hdr >> 20
        if size == 0xFFF:
            if pos + 4 > n:
                return
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, data[pos : pos + size]
        pos += size


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj(-15)
    out = d.decompress(data, MAX_ARCHIVE_PART)
    return out


def _hwp(path: str) -> Preview:
    with open(path, "rb") as fh:
        data = fh.read(256 << 20)
    ole = _Ole(data)
    hi = ole.find("FileHeader")
    flags = 0
    if hi is not None:
        hdr = ole.read(hi)
        if len(hdr) >= 40:
            flags = struct.unpack_from("<I", hdr, 36)[0]
    compressed, encrypted = bool(flags & 1), bool(flags & 2)
    if flags & 4 or encrypted:  # password protected / encrypted
        return Preview(kind="HWP", error="encrypted")
    sections = sorted((n for n in ole.listdir("BodyText") if re.fullmatch(r"Section\d+", n, re.I)), key=_natural_key)
    paras: list[str] = []
    for name in sections:
        raw = ole.read(ole.find("BodyText", name))  # type: ignore[arg-type]
        if compressed:
            raw = _inflate(raw)
        for tag, rec in _hwp_records(raw):
            if tag == 67:  # HWPTAG_PARA_TEXT (HWPTAG_BEGIN 16 + 51)
                t = _hwp_para_text(rec)
                paras.append(t if t.endswith("\n") else t + "\n")
        if sum(map(len, paras)) > MAX_CHARS:
            break
    text = "".join(paras)
    if not text.strip():
        # distribution documents / unusual files: fall back to the stored preview text
        pi = ole.find("PrvText")
        if pi is not None:
            text = ole.read(pi).decode("utf-16-le", "replace").replace("\r\n", "\n").replace("\r", "\n")
    p = _clip(text)
    p.kind = "HWP"
    return p


def _pdf(path: str) -> Preview:
    try:
        import pymupdf  # type: ignore
    except Exception:
        return Preview(kind="PDF", error="unreadable")
    parts, total = [], 0
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            return Preview(kind="PDF", error="encrypted")
        for page in doc:
            t = page.get_text()
            parts.append(t)
            total += len(t)
            if total > MAX_CHARS:
                break
    p = _clip("\n".join(parts))
    p.kind = "PDF"
    return p


# ---- entry point -------------------------------------------------------------------------


def load(path: str) -> Preview:
    """Return the preview text for ``path``; never raises (problems land in ``Preview.error``)."""
    try:
        if os.path.isdir(path):
            return Preview(error="folder")
        ext = os.path.splitext(path)[1].lower()
        if ext in _ZIP_KINDS:
            label, fn = _ZIP_KINDS[ext]
            try:
                with zipfile.ZipFile(path) as zf:
                    p = _clip(fn(zf))
                p.kind = label
                return p
            except (zipfile.BadZipFile, KeyError, ValueError):
                return _plain(path)
        if ext == ".pdf":
            try:
                return _pdf(path)
            except Exception:
                return Preview(kind="PDF", error="unreadable")
        if ext == ".hwp":
            try:
                return _hwp(path)
            except (ValueError, struct.error, zlib.error, IndexError):
                return Preview(kind="HWP", error="unreadable")
        return _plain(path)
    except OSError as e:
        return Preview(error=str(e) or "unreadable")


def search_terms(query_text: str) -> list[str]:
    """The ``content:`` operands of a search string (used for highlighting)."""
    terms = []
    for m in re.finditer(r'content:(?:"([^"]*)"|<([^>]*)>|([^\s|!<>()"]+))', query_text, re.I):
        t = m.group(1) or m.group(2) or m.group(3)
        if t:
            terms.append(t)
    return terms
