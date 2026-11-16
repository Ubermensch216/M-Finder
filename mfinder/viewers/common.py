"""Helpers shared by the document readers."""

from __future__ import annotations

import os
import re
import zipfile
import xml.etree.ElementTree as ET
from .model import Image, Run

MAX_PART = 64 << 20
MAX_EMBED_IMAGE = 8 << 20
TRUNCATED_NOTE = "document truncated"


def truncated_note() -> str:
    return TRUNCATED_NOTE


def style_run(base: Run, **kw) -> Run:
    d = dict(base.__dict__)
    d.update(kw)
    return Run(**d)


def load_local_image(src: str, base_dir: str) -> Image | None:
    if not src or (re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I) and not re.match(r"^file:", src, re.I)):
        return None
    path = src[7:] if src.lower().startswith("file:///") else src
    path = os.path.normpath(os.path.join(base_dir, path.replace("/", os.sep)))
    try:
        if os.path.getsize(path) > MAX_EMBED_IMAGE:
            return None
        with open(path, "rb") as f:
            return Image(f.read())
    except OSError:
        return None


def read_zip_part(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_PART:
        raise ValueError(f"{name} is too large")
    with zf.open(info) as fh:
        return fh.read(MAX_PART + 1)


def parse_xml(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def local(tag: str) -> str:
    """Tag name without its namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_all(el: ET.Element, name: str):
    """All descendants whose local name is ``name``."""
    return [e for e in el.iter() if local(e.tag) == name]


def child(el: ET.Element, name: str):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el: ET.Element, name: str):
    return [c for c in el if local(c.tag) == name]


def attr(el: ET.Element, name: str, default=None):
    """Attribute by local name (ignores the namespace prefix)."""
    if el is None:
        return default
    v = el.get(name)
    if v is not None:
        return v
    for k, val in el.attrib.items():
        if k.rsplit("}", 1)[-1] == name:
            return val
    return default


def to_float(s, default=0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def to_int(s, default=0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return default


def hex_color(s: str | None) -> str | None:
    """'#RRGGBB' / 'RRGGBB' / 'FFRRGGBB' -> '#rrggbb'; 'auto'/'none' -> None."""
    if not s:
        return None
    s = s.strip().lstrip("#")
    if len(s) == 8:
        s = s[2:]
    if len(s) != 6:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return "#" + s.lower()


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def image_kind(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    return ""
