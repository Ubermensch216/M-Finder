"""Helpers shared by the document readers."""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

MAX_PART = 64 << 20


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
    import re

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
