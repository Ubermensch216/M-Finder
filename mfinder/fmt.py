"""Display formatting helpers."""

from __future__ import annotations

import time


_TIME_CACHE: dict[int, str] = {}


def format_size(size: int) -> str:
    """File size in KB rounded up, with thousands separators."""
    if size < 0:
        return ""
    if size == 0:
        return "0 KB"
    return f"{(size + 1023) >> 10:,} KB"


def format_time(epoch: float) -> str:
    if epoch <= 0:
        return ""
    m = int(epoch) // 60
    s = _TIME_CACHE.get(m)
    if s is not None:
        return s
    try:
        s = time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return ""
    if len(_TIME_CACHE) > 32768:
        _TIME_CACHE.clear()
    _TIME_CACHE[m] = s
    return s


def split_ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 else ""
