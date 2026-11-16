"""HTML (.html / .htm) reader -> ``model.Doc`` using Python's standard ``html.parser``."""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser

from .. import preview
from . import common as C
from .model import Cell, Doc, Image, Para, Run, Table

MAX_BYTES = 4 << 20
HEADING_SIZE = {1: 20.0, 2: 16.0, 3: 13.0, 4: 11.5, 5: 10.0, 6: 9.0}
CODE_BG = "#f3f4f6"
LINK = "#0969da"

_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "figure", "figcaption", "address", "dt", "dd", "dl",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


_style = C.style_run
_load_image = C.load_local_image


class _DocBuilder(HTMLParser):
    def __init__(self, base_dir: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.blocks: list = []
        self.runs: list = []
        self.styles: list[Run] = [Run()]
        self.skip_depth = 0
        self.pre_depth = 0
        self.quote_depth = 0
        self.list_stack: list[tuple[str, int]] = []  # ("ul"|"ol", counter)
        self.heading_level = 0
        self.table_stack: list[dict] = []

    def _cur(self) -> Run:
        return self.styles[-1]

    def _target_blocks(self) -> list:
        if self.table_stack:
            t = self.table_stack[-1]
            if t["cell"] is not None:
                return t["cell"].blocks
        return self.blocks

    def _flush_para(self, before: float = 0.0, after: float = 4.0) -> None:
        if not self.runs:
            return
        # Trim leading/trailing spaces on non-pre paragraphs
        if self.pre_depth == 0:
            while self.runs and isinstance(self.runs[0], Run) and not self.runs[0].text.strip():
                self.runs.pop(0)
            while self.runs and isinstance(self.runs[-1], Run) and not self.runs[-1].text.strip():
                self.runs.pop()
        if not self.runs:
            return
        left = 16.0 * len(self.list_stack) + 20.0 * self.quote_depth
        bar = "#d0d7de" if self.quote_depth else None
        if self.heading_level:
            before = max(before, 8.0)
            after = max(after, 4.0)
        bg = CODE_BG if self.pre_depth > 0 else None
        self._target_blocks().append(
            Para(runs=self.runs, left=left, before=before, after=after, bar=bar, bg=bg)
        )
        self.runs = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        amap = dict(attrs)
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_para()
            self.heading_level = int(t[1])
            self.styles.append(_style(self._cur(), bold=True, size=HEADING_SIZE[self.heading_level]))
        elif t == "br":
            if self.pre_depth > 0:
                self.runs.append(_style(self._cur(), text="\n"))
            else:
                self._flush_para(after=2.0)
        elif t == "hr":
            self._flush_para()
            self._target_blocks().append(Para(rule=True, before=4.0, after=4.0))
        elif t in ("b", "strong"):
            self.styles.append(_style(self._cur(), bold=True))
        elif t in ("i", "em", "cite", "var"):
            self.styles.append(_style(self._cur(), italic=True))
        elif t in ("u", "ins"):
            self.styles.append(_style(self._cur(), underline=True))
        elif t in ("s", "del", "strike"):
            self.styles.append(_style(self._cur(), strike=True))
        elif t in ("code", "kbd", "samp", "tt"):
            self.styles.append(_style(self._cur(), mono=True, bg=CODE_BG))
        elif t == "pre":
            self._flush_para()
            self.pre_depth += 1
            self.styles.append(_style(self._cur(), mono=True, bg=CODE_BG))
        elif t == "sup":
            self.styles.append(_style(self._cur(), sup=1))
        elif t == "sub":
            self.styles.append(_style(self._cur(), sup=-1))
        elif t == "a":
            href = amap.get("href") or ""
            self.styles.append(_style(self._cur(), color=LINK, underline=True, link=href or None))
        elif t == "blockquote":
            self._flush_para()
            self.quote_depth += 1
        elif t in ("ul", "ol"):
            self._flush_para()
            self.list_stack.append((t, 0))
        elif t == "li":
            self._flush_para(after=2.0)
            if self.list_stack:
                kind, idx = self.list_stack[-1]
                idx += 1
                self.list_stack[-1] = (kind, idx)
                bullet = "• " if kind == "ul" else f"{idx}. "
            else:
                bullet = "• "
            self.runs.append(_style(self._cur(), text=bullet, bold=(kind == "ol") if self.list_stack else False))
        elif t == "table":
            self._flush_para()
            self.table_stack.append({"rows": [], "row": None, "cell": None})
        elif t == "tr" and self.table_stack:
            self._flush_para()
            row: list[Cell] = []
            self.table_stack[-1]["rows"].append(row)
            self.table_stack[-1]["row"] = row
        elif t in ("td", "th") and self.table_stack:
            self._flush_para()
            tbl = self.table_stack[-1]
            if tbl["row"] is None:
                tbl["row"] = []
                tbl["rows"].append(tbl["row"])
            try:
                cs = max(1, int(amap.get("colspan") or 1))
            except ValueError:
                cs = 1
            try:
                rs = max(1, int(amap.get("rowspan") or 1))
            except ValueError:
                rs = 1
            cell = Cell(colspan=cs, rowspan=rs, border=True, bg="#f6f8fa" if t == "th" else None)
            tbl["row"].append(cell)
            tbl["cell"] = cell
            if t == "th":
                self.styles.append(_style(self._cur(), bold=True))
        elif t == "img":
            src = amap.get("src") or ""
            alt = amap.get("alt") or ""
            img = _load_image(src, self.base_dir)
            if img is not None:
                img.alt = alt
                self.runs.append(img)
            elif alt:
                self.runs.append(_style(self._cur(), text=f"[{alt}]", color="#888888", italic=True))
        elif t in _BLOCK_TAGS:
            self._flush_para()

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth or t in _VOID_TAGS:
            return

        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_para(before=8.0, after=4.0)
            self.heading_level = 0
            if len(self.styles) > 1:
                self.styles.pop()
        elif t in ("b", "strong", "i", "em", "cite", "var", "u", "ins", "s", "del", "strike",
                   "code", "kbd", "samp", "tt", "sup", "sub", "a"):
            if len(self.styles) > 1:
                self.styles.pop()
        elif t == "pre":
            self._flush_para(before=4.0, after=6.0)
            if self.pre_depth > 0:
                self.pre_depth -= 1
            if len(self.styles) > 1:
                self.styles.pop()
        elif t == "blockquote":
            self._flush_para()
            if self.quote_depth > 0:
                self.quote_depth -= 1
        elif t in ("ul", "ol"):
            self._flush_para()
            if self.list_stack:
                self.list_stack.pop()
        elif t == "li":
            self._flush_para(after=2.0)
        elif t in ("td", "th") and self.table_stack:
            self._flush_para(after=1.0)
            self.table_stack[-1]["cell"] = None
            if t == "th" and len(self.styles) > 1:
                self.styles.pop()
        elif t == "tr" and self.table_stack:
            self._flush_para()
            self.table_stack[-1]["row"] = None
        elif t == "table" and self.table_stack:
            self._flush_para()
            tbl = self.table_stack.pop()
            rows = [r for r in tbl["rows"] if r]
            if rows:
                self._target_blocks().append(Table(rows=rows, border=True))
        elif t in _BLOCK_TAGS:
            self._flush_para()

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data:
            return
        if self.pre_depth > 0:
            self.runs.append(_style(self._cur(), text=data))
            return
        collapsed = re.sub(r"\s+", " ", data)
        if collapsed == " " and (not self.runs or (isinstance(self.runs[-1], Run) and self.runs[-1].text.endswith(" "))):
            return
        self.runs.append(_style(self._cur(), text=collapsed))

    def finish(self) -> list:
        self._flush_para()
        while self.table_stack:
            tbl = self.table_stack.pop()
            rows = [r for r in tbl["rows"] if r]
            if rows:
                self.blocks.append(Table(rows=rows, border=True))
        return self.blocks


def read(path: str) -> Doc:
    with open(path, "rb") as f:
        raw = f.read(MAX_BYTES + 1)
    truncated = len(raw) > MAX_BYTES
    if truncated:
        raw = raw[:MAX_BYTES]
    decoded = preview.decode_text(raw)
    if decoded is None:
        raise ValueError("binary")
    text, _enc = decoded
    parser = _DocBuilder(os.path.dirname(path))
    parser.feed(text)
    parser.close()
    blocks = parser.finish()
    doc = Doc(blocks=blocks)
    if truncated:
        doc.note = C.truncated_note()
    return doc
