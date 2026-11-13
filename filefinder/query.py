"""Everything-style search syntax: parser, compiler and sorter.

Syntax (subset of Everything's):
  space = AND, ``|`` = OR, ``!`` = NOT, ``< >`` = grouping, ``"..."`` = quoting
  ``*`` / ``?`` wildcards (match the whole name), ``\\`` in a term matches the full path
  modifiers   case: nocase: regex: noregex: wholeword: ww: path: nopath: wfn:
              startwith: endwith:   (without a value they apply to what follows)
  functions   file: folder: ext: size: dm: len: parent: infolder: depth: empty: attrib:
  macros      audio: video: pic: doc: zip: exe:
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import preview
from .fmt import split_ext
from .index import FILE_ATTRIBUTE_DIRECTORY, Index

DIR = FILE_ATTRIBUTE_DIRECTORY


class QueryError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Flags:
    case: bool = False
    ww: bool = False
    path: bool = False
    regex: bool = False
    wfn: bool = False
    start: bool = False
    end: bool = False


MACROS = {
    "audio": "mp3;wav;flac;aac;ogg;wma;m4a;opus;aiff;mid;midi;ape",
    "video": "mp4;mkv;avi;mov;wmv;flv;webm;m4v;mpg;mpeg;3gp;ts",
    "pic": "jpg;jpeg;png;gif;bmp;tif;tiff;webp;ico;svg;heic;raw;psd",
    "picture": "jpg;jpeg;png;gif;bmp;tif;tiff;webp;ico;svg;heic;raw;psd",
    "doc": "doc;docx;xls;xlsx;ppt;pptx;pdf;txt;rtf;odt;ods;odp;md;csv;hwp;hwpx",
    "document": "doc;docx;xls;xlsx;ppt;pptx;pdf;txt;rtf;odt;ods;odp;md;csv;hwp;hwpx",
    "zip": "zip;rar;7z;tar;gz;bz2;xz;cab;iso;tgz;zst",
    "compressed": "zip;rar;7z;tar;gz;bz2;xz;cab;iso;tgz;zst",
    "exe": "exe;bat;cmd;com;msi;scr;ps1",
    "executable": "exe;bat;cmd;com;msi;scr;ps1",
}

# name -> (flag attribute, value) for modifiers usable bare (affect following terms)
MODIFIERS = {
    "case": ("case", True), "nocase": ("case", False),
    "regex": ("regex", True), "noregex": ("regex", False),
    "wholeword": ("ww", True), "ww": ("ww", True),
    "nowholeword": ("ww", False), "noww": ("ww", False),
    "path": ("path", True), "matchpath": ("path", True),
    "nopath": ("path", False), "nomatchpath": ("path", False),
    "wfn": ("wfn", True), "wholefilename": ("wfn", True),
    "nowfn": ("wfn", False), "nowholefilename": ("wfn", False),
    "startwith": ("start", True), "startswith": ("start", True),
    "endwith": ("end", True), "endswith": ("end", True),
}
FUNCS = {
    "file", "files", "folder", "folders", "ext", "size", "dm", "datemodified", "dc", "datecreated",
    "da", "dateaccessed", "content", "len",
    "parent", "infolder", "nosubfolders", "depth", "empty", "attrib", "attributes",
} | set(MACROS)


# ---------------------------------------------------------------------------
# Lexer / parser -> AST
# ---------------------------------------------------------------------------

_PREFIX = re.compile(r"[A-Za-z]+:")


def tokenize(text: str) -> list[tuple]:
    toks: list[tuple] = []
    i, n, depth = 0, len(text), 0
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "|":
            toks.append(("or",)); i += 1
        elif c == "!":
            toks.append(("not",)); i += 1
        elif c == "<":
            toks.append(("open",)); depth += 1; i += 1
        elif c == ">" and depth > 0:
            toks.append(("close",)); depth -= 1; i += 1
        else:
            buf: list[str] = []
            inq = False
            while i < n:
                c = text[i]
                if c == '"':
                    inq = not inq
                    i += 1
                    continue
                if not inq:
                    if c.isspace() or c == "|":
                        break
                    if c == ">" and depth > 0 and not _PREFIX.fullmatch("".join(buf)):
                        break
                buf.append(c)
                i += 1
            word = "".join(buf)
            if word:
                toks.append(("word", word))
    return toks


class _Parser:
    def __init__(self, toks: list[tuple], flags: Flags) -> None:
        self.toks = toks
        self.pos = 0
        self.flags = flags

    def peek(self) -> Optional[str]:
        return self.toks[self.pos][0] if self.pos < len(self.toks) else None

    def parse(self):
        node = self.or_expr()
        if self.pos < len(self.toks):
            raise QueryError("Unexpected '>'")
        return node

    def or_expr(self):
        parts = [self.and_expr()]
        while self.peek() == "or":
            self.pos += 1
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else ("or", parts)

    def and_expr(self):
        items = []
        while self.peek() not in (None, "or", "close"):
            items.append(self.unary())
        items = [x for x in items if x != ("true",)]
        if not items:
            return ("true",)
        return items[0] if len(items) == 1 else ("and", items)

    def unary(self):
        kind = self.peek()
        if kind == "not":
            self.pos += 1
            if self.peek() in (None, "or", "close"):
                return ("true",)
            return ("not", self.unary())
        if kind == "open":
            self.pos += 1
            node = self.or_expr()
            if self.peek() == "close":
                self.pos += 1
            return node
        word = self.toks[self.pos][1]
        self.pos += 1
        return self.word(word)

    def word(self, word: str):
        name, sep, value = word.partition(":")
        lname = name.lower()
        if sep and name.isalpha():
            if lname in MODIFIERS:
                attr, val = MODIFIERS[lname]
                if value == "":
                    self.flags = dataclasses.replace(self.flags, **{attr: val})
                    return ("true",)
                return ("text", value, dataclasses.replace(self.flags, **{attr: val}))
            if lname in FUNCS:
                return self.func(lname, value)
        return ("text", word, self.flags)

    def func(self, name: str, value: str):
        if name in ("file", "files", "folder", "folders"):
            node = ("isdir",) if name.startswith("folder") else ("isfile",)
            if value:
                return ("and", [node, ("text", value, self.flags)])
            return node
        if name in MACROS:
            node = ("ext", frozenset(MACROS[name].split(";")))
            if value:
                return ("and", [node, ("text", value, self.flags)])
            return node
        if name == "ext":
            exts = frozenset(e.strip().lstrip(".").lower() for e in value.split(";") if e.strip())
            return ("ext", exts)
        if name == "empty":
            return ("empty",)
        if not value:
            raise QueryError(f"'{name}:' needs a value")
        if name == "size":
            return ("size", _numeric(value, _size_atom))
        if name in _DATE_FUNCS:
            return ("date", _DATE_FUNCS[name], _numeric(value, _date_atom))
        if name == "content":
            return ("content", value, self.flags)
        if name == "len":
            return ("len", _numeric(value, _int_atom))
        if name == "depth":
            return ("depth", _numeric(value, _int_atom))
        if name in ("parent", "infolder", "nosubfolders"):
            return ("parent", value.replace("/", "\\").rstrip("\\").lower())
        if name in ("attrib", "attributes"):
            return ("attrib", _attrib_mask(value))
        raise QueryError(f"Unknown function '{name}:'")


_DATE_FUNCS = {
    "dm": "mtime", "datemodified": "mtime", "dc": "ctime", "datecreated": "ctime",
    "da": "atime", "dateaccessed": "atime",
}


def parse(text: str, flags: Flags = Flags()):
    return _Parser(tokenize(text), flags).parse()


# ---------------------------------------------------------------------------
# Numeric / date helpers
# ---------------------------------------------------------------------------

_SIZE_UNITS = {
    "": 1, "b": 1, "k": 1 << 10, "kb": 1 << 10, "kib": 1 << 10, "m": 1 << 20, "mb": 1 << 20,
    "mib": 1 << 20, "g": 1 << 30, "gb": 1 << 30, "gib": 1 << 30, "t": 1 << 40, "tb": 1 << 40,
}
_SIZE_CONST = {
    "empty": (0, 0), "tiny": (1, 10 << 10), "small": ((10 << 10) + 1, 100 << 10),
    "medium": ((100 << 10) + 1, 1 << 20), "large": ((1 << 20) + 1, 16 << 20),
    "huge": ((16 << 20) + 1, 128 << 20), "gigantic": ((128 << 20) + 1, math.inf),
    "unknown": (-1, -1),
}


def _int_atom(s: str):
    try:
        v = int(s)
    except ValueError:
        raise QueryError(f"Invalid number '{s}'") from None
    return v, v


def _size_atom(s: str):
    s = s.strip().lower()
    if s in _SIZE_CONST:
        return _SIZE_CONST[s]
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)", s)
    if not m or m.group(2) not in _SIZE_UNITS:
        raise QueryError(f"Invalid size '{s}'")
    v = int(float(m.group(1)) * _SIZE_UNITS[m.group(2)])
    return v, v


_DATE_RE = re.compile(
    r"(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2})(?:[ T](\d{1,2})(?::(\d{2})(?::(\d{2}))?)?)?)?)?"
)
_LAST_RE = re.compile(r"last(\d+)(second|minute|hour|day|week|month|year)s?")
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def _add_months(d: datetime, months: int) -> datetime:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return d.replace(year=y, month=m + 1)


def _date_atom(s: str, now: Optional[datetime] = None):
    """Return the [start, end) interval (as epoch seconds) a date expression denotes."""
    s = s.strip().lower()
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def span(a: datetime, b: datetime):
        return a.timestamp(), math.nextafter(b.timestamp(), -math.inf)

    if s == "today":
        return span(today, today + timedelta(days=1))
    if s == "yesterday":
        return span(today - timedelta(days=1), today)
    week = today - timedelta(days=today.weekday())
    if s == "thisweek":
        return span(week, week + timedelta(days=7))
    if s == "lastweek":
        return span(week - timedelta(days=7), week)
    month = today.replace(day=1)
    if s == "thismonth":
        return span(month, _add_months(month, 1))
    if s == "lastmonth":
        return span(_add_months(month, -1), month)
    year = today.replace(month=1, day=1)
    if s == "thisyear":
        return span(year, year.replace(year=year.year + 1))
    if s == "lastyear":
        return span(year.replace(year=year.year - 1), year)
    m = _LAST_RE.fullmatch(s)
    if m:
        return now.timestamp() - int(m.group(1)) * _UNIT_SECONDS[m.group(2)], now.timestamp()
    m = _DATE_RE.fullmatch(s)
    if m:
        parts = [int(g) if g else None for g in m.groups()]
        y, mo, d, hh, mi, ss = parts
        try:
            if mo is None:
                a = datetime(y, 1, 1); b = datetime(y + 1, 1, 1)
            elif d is None:
                a = datetime(y, mo, 1); b = _add_months(a, 1)
            elif hh is None:
                a = datetime(y, mo, d); b = a + timedelta(days=1)
            elif mi is None:
                a = datetime(y, mo, d, hh); b = a + timedelta(hours=1)
            elif ss is None:
                a = datetime(y, mo, d, hh, mi); b = a + timedelta(minutes=1)
            else:
                a = datetime(y, mo, d, hh, mi, ss); b = a + timedelta(seconds=1)
        except ValueError:
            raise QueryError(f"Invalid date '{s}'") from None
        return span(a, b)
    raise QueryError(f"Invalid date '{s}'")


def _numeric(value: str, atom: Callable) -> tuple[float, float]:
    """Parse a comparison/range expression into an inclusive (lo, hi) interval."""
    v = value.strip()
    if ".." in v:
        a, _, b = v.partition("..")
        lo = atom(a)[0] if a.strip() else -math.inf
        hi = atom(b)[1] if b.strip() else math.inf
        return lo, hi
    for op in (">=", "<=", ">", "<", "="):
        if v.startswith(op):
            lo, hi = atom(v[len(op):])
            if op == ">=": return lo, math.inf
            if op == "<=": return -math.inf, hi
            if op == ">": return math.nextafter(hi, math.inf), math.inf
            if op == "<": return -math.inf, math.nextafter(lo, -math.inf)
            return lo, hi
    return atom(v)


_ATTR_BITS = {"R": 0x1, "H": 0x2, "S": 0x4, "D": 0x10, "A": 0x20, "T": 0x100, "L": 0x400, "C": 0x800, "O": 0x1000, "I": 0x2000, "E": 0x4000}


def _attrib_mask(letters: str) -> int:
    mask = 0
    for ch in letters.upper():
        if ch not in _ATTR_BITS:
            raise QueryError(f"Unknown attribute '{ch}'")
        mask |= _ATTR_BITS[ch]
    return mask


# ---------------------------------------------------------------------------
# Compiler: AST -> predicate(i)
# ---------------------------------------------------------------------------


class Cancelled(Exception):
    pass


class _Ctx:
    def __init__(self, index: Index, cancel: Optional[threading.Event] = None, content_max: int = 64 << 20) -> None:
        self.index = index
        self.cancel = cancel
        self.content_max = content_max
        self._dirs: dict[int, str] = {}

    def dir_path(self, d: int) -> str:
        p = self._dirs.get(d)
        if p is None:
            idx = self.index
            par = idx.parent[d]
            p = idx.names[d] if par < 0 else self.dir_path(par) + "\\" + idx.names[d]
            self._dirs[d] = p
        return p

    def full(self, i: int) -> str:
        idx = self.index
        par = idx.parent[i]
        if par < 0:
            n = idx.names[i]
            return n + "\\" if len(n) == 2 and n[1] == ":" else n
        return self.dir_path(par) + "\\" + idx.names[i]


def _wild_to_regex(term: str) -> str:
    out = []
    for ch in term:
        out.append(".*" if ch == "*" else "." if ch == "?" else re.escape(ch))
    return "".join(out)


def _compile_text(term: str, flags: Flags, ctx: _Ctx) -> Callable[[int], bool]:
    idx = ctx.index
    is_wild = not flags.regex and ("*" in term or "?" in term)
    has_sep = not flags.regex and ("\\" in term or "/" in term)
    use_path = flags.path or has_sep
    if use_path and not flags.regex:
        term = term.replace("/", "\\")
    if not flags.case:
        reflags = re.IGNORECASE
    else:
        reflags = 0

    if use_path:
        if flags.case:
            get = ctx.full
        else:
            get = lambda i: ctx.full(i).lower()  # noqa: E731
    else:
        get = idx.names.__getitem__ if flags.case else idx.lower.__getitem__

    try:
        if flags.regex:
            rx = re.compile(term, reflags)
            tgt = ctx.full if use_path else idx.names.__getitem__
            return lambda i: rx.search(tgt(i)) is not None
        if is_wild:
            rx = re.compile(_wild_to_regex(term), reflags | re.DOTALL)
            tgt = ctx.full if use_path else idx.names.__getitem__
            return lambda i: rx.fullmatch(tgt(i)) is not None
    except re.error as e:
        raise QueryError(f"Invalid regular expression: {e}") from None

    t = term if flags.case else term.lower()
    if flags.wfn:
        return lambda i: get(i) == t
    if flags.ww:
        rx = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", reflags)
        tgt = ctx.full if use_path else idx.names.__getitem__
        return lambda i: rx.search(tgt(i)) is not None
    if flags.start:
        return lambda i: get(i).startswith(t)
    if flags.end:
        return lambda i: get(i).endswith(t)
    return lambda i: t in get(i)


def _compile_content(term: str, flags: Flags, ctx: _Ctx) -> Callable[[int], bool]:
    """Match file contents (UTF-8, UTF-16LE and the ANSI code page; ASCII case folding)."""
    idx = ctx.index
    attrs = idx.attrs
    rx = None
    if flags.regex:
        try:
            rx = re.compile(term, 0 if flags.case else re.IGNORECASE)
        except re.error as e:
            raise QueryError(f"Invalid regular expression: {e}") from None
    needle = term if flags.case else term.lower()
    pats = {needle.encode("utf-8"), needle.encode("utf-16-le")}
    try:
        pats.add(needle.encode("mbcs"))
    except (LookupError, UnicodeEncodeError):
        pass
    keep = max(len(p) for p in pats) - 1
    case = flags.case
    structured = preview.STRUCTURED_EXTS

    def match_text(text: str) -> bool:
        if rx is not None:
            return rx.search(text) is not None
        return needle in (text if case else text.lower())

    def f(i: int) -> bool:
        if attrs[i] & DIR:
            return False
        if ctx.cancel is not None and ctx.cancel.is_set():
            raise Cancelled()
        try:
            if os.path.splitext(idx.names[i])[1].lower() in structured:
                # HWP / HWPX / Office / OpenDocument keep their text compressed: search the extracted text
                path = ctx.full(i)
                if os.path.getsize(path) <= ctx.content_max:
                    pv = preview.load(path)
                    if not pv.error and match_text(pv.text):
                        return True
                    if not pv.error:
                        return False
            with open(ctx.full(i), "rb") as fh:
                if os.fstat(fh.fileno()).st_size > ctx.content_max:
                    return False
                if rx is not None:
                    raw = fh.read()
                    return rx.search(raw.decode("utf-8", "ignore")) is not None or rx.search(
                        raw.decode("utf-16-le", "ignore")
                    ) is not None
                tail = b""
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        return False
                    data = tail + (chunk if case else chunk.lower())
                    if any(p in data for p in pats):
                        return True
                    tail = data[-keep:] if keep else b""
        except OSError:
            return False

    return f


def _compile(node, ctx: _Ctx) -> Callable[[int], bool]:
    idx = ctx.index
    kind = node[0]
    if kind == "true":
        return lambda i: True
    if kind == "and":
        fs = [_compile(n, ctx) for n in node[1]]
        if len(fs) == 2:
            a, b = fs
            return lambda i: a(i) and b(i)
        return lambda i: all(f(i) for f in fs)
    if kind == "or":
        fs = [_compile(n, ctx) for n in node[1]]
        return lambda i: any(f(i) for f in fs)
    if kind == "not":
        f = _compile(node[1], ctx)
        return lambda i: not f(i)
    if kind == "text":
        return _compile_text(node[1], node[2], ctx)
    attrs = idx.attrs
    if kind == "isdir":
        return lambda i: bool(attrs[i] & DIR)
    if kind == "isfile":
        return lambda i: not attrs[i] & DIR
    if kind == "ext":
        exts = node[1]
        names = idx.names
        return lambda i: not attrs[i] & DIR and split_ext(names[i]) in exts
    if kind == "empty":
        has = idx.child_flags()
        return lambda i: bool(attrs[i] & DIR) and not has[i]
    if kind == "size":
        lo, hi = node[1]
        size, mtime = idx.size, idx.mtime

        def f_size(i: int) -> bool:
            if not attrs[i] & DIR and mtime[i] == 0.0:
                idx.meta(i)
            s = size[i]
            if s < 0:  # unknown (folder before sizes were computed, or failed lookup)
                return lo == -1 and hi == -1
            return lo <= s <= hi

        return f_size
    if kind == "date":
        lo, hi = node[2]
        arr = getattr(idx, node[1])
        mtime = idx.mtime

        def f_date(i: int) -> bool:
            if mtime[i] == 0.0:
                idx.meta(i)
            return lo <= arr[i] <= hi

        return f_date
    if kind == "content":
        return _compile_content(node[1], node[2], ctx)
    if kind == "len":
        lo, hi = node[1]
        names = idx.names
        return lambda i: lo <= len(names[i]) <= hi
    if kind == "depth":
        lo, hi = node[1]
        parent = idx.parent

        def f_depth(i: int) -> bool:
            d = 0
            while parent[i] >= 0:
                i = parent[i]
                d += 1
            return lo <= d <= hi

        return f_depth
    if kind == "parent":
        want = node[1]
        parent = idx.parent
        return lambda i: parent[i] >= 0 and ctx.dir_path(parent[i]).lower() == want
    if kind == "attrib":
        mask = node[1]
        return lambda i: attrs[i] & mask == mask
    raise QueryError(f"Internal error: unknown node {kind}")


def _plain_substring(node) -> Optional[str]:
    """Lower-cased term when node is a plain, case-insensitive name substring match."""
    if node[0] != "text":
        return None
    term, f = node[1], node[2]
    if (f.case or f.ww or f.path or f.regex or f.wfn or f.start or f.end) or not term:
        return None
    if any(c in term for c in "*?\\/"):
        return None
    return term.lower()


_COST = {"size": 1, "date": 1, "content": 2}


def _flatten_and(*asts) -> list:
    items = []
    for a in asts:
        items.extend(a[1] if a[0] == "and" else [a])
    return [x for x in items if x != ("true",)] or [("true",)]


def search(
    index: Index,
    text: str,
    flags: Flags = Flags(),
    cancel: Optional[threading.Event] = None,
    filter_text: str = "",
    filter_flags: Optional[Flags] = None,
    content_max: int = 64 << 20,
) -> Optional[list[int]]:
    """Return matching entry ids in index order, or None if cancelled.

    ``filter_text`` is an extra query (a "filter" such as ``pic:``) ANDed with ``text``.
    """
    ast = parse(text, flags)
    if filter_text.strip():
        ast = ("and", _flatten_and(parse(filter_text, filter_flags or Flags()), ast))
    with index.lock:
        ctx = _Ctx(index, cancel, content_max)
        n = len(index)
        alive, parent, lower = index.alive, index.parent, index.lower
        CH = 4096

        items = ast[1] if ast[0] == "and" else [ast]
        sub, rest = None, items
        for k, node in enumerate(items):
            s = _plain_substring(node)
            if s is not None:
                sub, rest = s, items[:k] + items[k + 1 :]
                break
        rest = sorted(rest, key=lambda nd: _COST.get(nd[0], 0))  # cheap predicates first, file reads last
        fn = _compile(("and", rest) if len(rest) > 1 else (rest[0] if rest else ("true",)), ctx)
        trivial = all(nd == ("true",) for nd in rest)

        out: list[int] = []
        try:
            for start in range(0, n, CH):
                if cancel is not None and cancel.is_set():
                    return None
                end = min(n, start + CH)
                if sub is not None:
                    cand = [i for i in range(start, end) if sub in lower[i] and alive[i] and parent[i] >= 0]
                else:
                    cand = [i for i in range(start, end) if alive[i] and parent[i] >= 0]
                out.extend(cand if trivial else [i for i in cand if fn(i)])
        except Cancelled:
            return None
        return out


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

SORT_KEYS = ("name", "path", "size", "mtime", "ext")


def sort_needs_meta(key: str) -> bool:
    return key in ("size", "mtime")


def sort_results(
    index: Index,
    ids: list[int],
    key: str,
    ascending: bool = True,
    cancel: Optional[threading.Event] = None,
) -> Optional[list[int]]:
    ids = list(ids)
    reverse = not ascending
    with index.lock:
        lower, attrs = index.lower, index.attrs
        if key == "name":
            ids.sort(key=lower.__getitem__, reverse=reverse)
            ids.sort(key=lambda i: 0 if attrs[i] & DIR else 1)
        elif key == "path":
            ctx = _Ctx(index)
            parent = index.parent
            ids.sort(
                key=lambda i: (ctx.dir_path(parent[i]).lower() if parent[i] >= 0 else "", lower[i]),
                reverse=reverse,
            )
        elif key == "ext":
            ids.sort(key=lambda i: (split_ext(lower[i]), lower[i]), reverse=reverse)
        elif key in ("size", "mtime"):
            if not index.ensure_meta(ids, cancel):
                return None
            arr = index.size if key == "size" else index.mtime
            ids.sort(key=arr.__getitem__, reverse=reverse)
        else:
            raise QueryError(f"Unknown sort key '{key}'")
    return ids
