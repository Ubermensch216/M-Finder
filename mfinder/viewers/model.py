"""Format-neutral document model shared by the readers and the Tk renderers.

All lengths are in points (1/72 inch). Colours are ``#rrggbb`` strings or None (= default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    size: Optional[float] = None  # pt; None = document default
    color: Optional[str] = None
    bg: Optional[str] = None
    sup: int = 0  # 1 superscript, -1 subscript
    font: Optional[str] = None
    mono: bool = False
    link: Optional[str] = None


@dataclass
class Image:
    data: bytes
    width: Optional[float] = None  # pt
    height: Optional[float] = None
    alt: str = ""


@dataclass
class Para:
    runs: list = field(default_factory=list)  # Run | Image
    align: str = "left"  # left | center | right | justify
    left: float = 0.0
    first: float = 0.0  # first-line indent relative to ``left`` (negative = hanging)
    before: float = 0.0
    after: float = 0.0
    line: Optional[float] = None  # line height multiplier
    bg: Optional[str] = None
    rule: bool = False  # horizontal rule / page break
    bar: Optional[str] = None  # left bar colour (block quotes)

    def text(self) -> str:
        return "".join(r.text for r in self.runs if isinstance(r, Run))


@dataclass
class Cell:
    blocks: list = field(default_factory=list)  # Para | Table
    colspan: int = 1
    rowspan: int = 1
    bg: Optional[str] = None
    width: Optional[float] = None  # pt
    border: bool = True
    valign: str = "top"


@dataclass
class Table:
    rows: list = field(default_factory=list)  # list[list[Cell]]
    col_widths: list = field(default_factory=list)  # pt, may contain None
    border: bool = True


@dataclass
class Doc:
    blocks: list = field(default_factory=list)  # Para | Table
    note: str = ""  # shown above the document (e.g. "truncated")
    base_size: float = 10.0
    font: Optional[str] = None


# ---- spreadsheets ---------------------------------------------------------------


@dataclass
class SCell:
    text: str = ""
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None
    bg: Optional[str] = None
    align: str = ""  # "", left, center, right
    wrap: bool = False
    border: bool = False
    size: Optional[float] = None


@dataclass
class Sheet:
    name: str = ""
    cells: dict = field(default_factory=dict)  # (row, col) 0-based -> SCell
    col_widths: dict = field(default_factory=dict)  # col -> px
    row_heights: dict = field(default_factory=dict)  # row -> px
    default_col: float = 64.0
    default_row: float = 20.0
    nrows: int = 0
    ncols: int = 0
    merges: list = field(default_factory=list)  # (r1, c1, r2, c2)
    truncated: bool = False


@dataclass
class Workbook:
    sheets: list = field(default_factory=list)


Block = Union[Para, Table]
