"""Display formatting helpers."""

from __future__ import annotations

import time


def format_size(size: int) -> str:
    """Everything-style size: KB rounded up, with thousands separators."""
    if size < 0:
        return ""
    if size == 0:
        return "0 KB"
    return f"{(size + 1023) // 1024:,} KB"


def format_time(epoch: float) -> str:
    if epoch <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return ""


def split_ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 else ""
