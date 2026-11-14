"""Markdown (.md) reader -> ``model.Doc`` (CommonMark-ish subset + GFM tables / task lists)."""

from __future__ import annotations

import os
import re

from . import common as C
from .model import Cell, Doc, Image, Para, Run, Table

MAX_BYTES = 4 << 20
HEADING_SIZE = {1: 2.0, 2: 1.6, 3: 1.3, 4: 1.15, 5: 1.0, 6: 0.9}
CODE_BG = "#f3f4f6"
LINK = "#0969da"

_INLINE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<href>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|<(?P<auto>https?://[^>\s]+)>"
    r"|(?P<bi>\*\*\*|___)(?P<bitext>.+?)(?P=bi)"
    r"|(?P<b>\*\*|__)(?P<btext>.+?)(?P=b)"
    r"|(?P<s>~~)(?P<stext>.+?)(?P=s)"
    r"|(?<![\w*])(?P<i>[*_])(?P<itext>[^\s*_][^*_]*?)(?P=i)(?![\w*])"
    r"|(?P<url>https?://[^\s<>)]+)"
)


def _style(base: Run, **kw) -> Run:
    d = dict(base.__dict__)
    d.update(kw)
    return Run(**d)


def inline(text: str, base: Run, base_dir: str, out: list, depth: int = 0) -> None:
    pos = 0
    text = text.replace("\\\n", "\n")
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(_style(base, text=_unescape(text[pos : m.start()])))
        pos = m.end()
        g = m.groupdict()
        if g["codetext"] is not None:
            out.append(_style(base, text=g["codetext"].strip(), mono=True, bg=CODE_BG))
        elif g["src"] is not None:
            img = _load_image(g["src"], base_dir)
            if img:
                img.alt = g["alt"] or ""
                out.append(img)
            else:
                out.append(_style(base, text=f"[{g['alt'] or 'image'}]", color="#888888", italic=True))
        elif g["href"] is not None:
            inline(g["ltext"], _style(base, color=LINK, underline=True, link=g["href"]), base_dir, out, depth + 1)
        elif g["auto"] is not None or g["url"] is not None:
            u = g["auto"] or g["url"]
            out.append(_style(base, text=u, color=LINK, underline=True, link=u))
        elif g["bitext"] is not None and depth < 4:
            inline(g["bitext"], _style(base, bold=True, italic=True), base_dir, out, depth + 1)
        elif g["btext"] is not None and depth < 4:
            inline(g["btext"], _style(base, bold=True), base_dir, out, depth + 1)
        elif g["stext"] is not None and depth < 4:
            inline(g["stext"], _style(base, strike=True), base_dir, out, depth + 1)
        elif g["itext"] is not None and depth < 4:
            inline(g["itext"], _style(base, italic=True), base_dir, out, depth + 1)
        else:
            out.append(_style(base, text=m.group(0)))
    if pos < len(text):
        out.append(_style(base, text=_unescape(text[pos:])))


def _unescape(s: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~<])", r"\1", s)


def _load_image(src: str, base_dir: str):
    if re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I) and not re.match(r"^file:", src, re.I):
        return None
    path = src[7:] if src.lower().startswith("file:///") else src
    path = os.path.normpath(os.path.join(base_dir, path.replace("/", os.sep)))
    try:
        if os.path.getsize(path) > 8 << 20:
            return None
        with open(path, "rb") as f:
            return Image(f.read())
    except OSError:
        return None


def _table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def read(path: str) -> Doc:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    big = len(raw) > MAX_BYTES
    from .. import preview

    res = preview.decode_text(raw[:MAX_BYTES])
    if res is None:
        raise ValueError("binary")
    text = res[0].replace("\r\n", "\n").replace("\r", "\n")
    return parse(text, os.path.dirname(path), "markdown truncated" if big else "")


def parse(text: str, base_dir: str = "", note: str = "") -> Doc:
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":  # front matter
        for k in range(1, min(len(lines), 60)):
            if lines[k].strip() == "---":
                lines = lines[k + 1 :]
                break
    blocks: list = []
    i, n = 0, len(lines)
    para_buf: list[str] = []
    base = Run()

    def flush_para():
        if para_buf:
            runs: list = []
            inline("\n".join(para_buf), base, base_dir, runs)
            blocks.append(Para(runs=runs, after=8, line=1.35))
            para_buf.clear()

    while i < n:
        line = lines[i]
        s = line.strip()
        # fenced code
        m = re.match(r"^(\s*)(```+|~~~+)\s*([\w+-]*)", line)
        if m:
            flush_para()
            fence = m.group(2)
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith(fence[:3]):
                code.append(lines[i])
                i += 1
            i += 1
            for k, cl in enumerate(code or [""]):
                blocks.append(Para(runs=[Run(text=cl.replace("\t", "    ") or " ", mono=True, size=9.5)], bg=CODE_BG,
                                   left=8, before=0, after=8 if k == len(code) - 1 else 0))
            continue
        if not s:
            flush_para()
            i += 1
            continue
        # heading
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush_para()
            lvl = len(m.group(1))
            runs: list = []
            inline(m.group(2), Run(bold=True, size=10.5 * HEADING_SIZE[lvl]), base_dir, runs)
            blocks.append(Para(runs=runs, before=10 if lvl <= 2 else 6, after=6))
            if lvl <= 2:
                blocks.append(Para(rule=True))
            i += 1
            continue
        # setext heading
        if i + 1 < n and para_buf and re.match(r"^(=+|-+)\s*$", s) and len(para_buf) == 1:
            lvl = 1 if s[0] == "=" else 2
            runs = []
            inline(para_buf.pop(), Run(bold=True, size=10.5 * HEADING_SIZE[lvl]), base_dir, runs)
            blocks.append(Para(runs=runs, before=10, after=6))
            blocks.append(Para(rule=True))
            i += 1
            continue
        # horizontal rule
        if re.match(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$", s):
            flush_para()
            blocks.append(Para(rule=True))
            i += 1
            continue
        # table
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", lines[i + 1]):
            flush_para()
            head = _table_row(line)
            aligns = ["center" if c.strip().startswith(":") and c.strip().endswith(":") else
                      "right" if c.strip().endswith(":") else "left" for c in _table_row(lines[i + 1])]
            rows = [head]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_table_row(lines[i]))
                i += 1
            ncol = len(head)
            trs = []
            for ri, r in enumerate(rows):
                tr = []
                for ci in range(ncol):
                    runs = []
                    inline(r[ci] if ci < len(r) else "", Run(bold=ri == 0), base_dir, runs)
                    p = Para(runs=runs, align=aligns[ci] if ci < len(aligns) else "left")
                    tr.append(Cell(blocks=[p], bg="#f0f2f5" if ri == 0 else None, border=True))
                trs.append(tr)
            blocks.append(Table(rows=trs, col_widths=[None] * ncol, border=True))
            blocks.append(Para(after=6))
            continue
        # block quote
        if s.startswith(">"):
            flush_para()
            q = []
            while i < n and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            sub = parse("\n".join(q), base_dir)
            for b in sub.blocks:
                if isinstance(b, Para):
                    b.left += 14
                    b.bar = "#d0d7de"
                    for r in b.runs:
                        if isinstance(r, Run) and r.color is None:
                            r.color = "#57606a"
                blocks.append(b)
            continue
        # list item
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            flush_para()
            indent = len(m.group(1).replace("\t", "    "))
            marker, body = m.group(2), m.group(3)
            body_lines = [body]
            i += 1
            while i < n and lines[i].strip() and not re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]) \
                    and (lines[i].startswith("  ") or lines[i].startswith("\t")):
                body_lines.append(lines[i].strip())
                i += 1
            label = "•" if marker in "-*+" else marker
            if marker in "-*+" and indent >= 2:
                label = "◦" if indent < 4 else "▪"
            task = re.match(r"^\[([ xX])\]\s+(.*)$", body_lines[0])
            if task:
                label = "☑" if task.group(1) in "xX" else "☐"
                body_lines[0] = task.group(2)
            runs = [Run(text=label + " ")]
            inline(" ".join(body_lines), base, base_dir, runs)
            lvl_left = 18 + indent * 7
            blocks.append(Para(runs=runs, left=lvl_left, first=-14, after=3, line=1.3))
            continue
        # html comment / block html: show as is
        para_buf.append(line.rstrip())
        i += 1
    flush_para()
    return Doc(blocks=blocks, base_size=10.5, note=note)
