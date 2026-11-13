"""Format-aware file viewers for the preview pane.

``load(path)`` runs off the UI thread and returns a :class:`Loaded` describing *what* to show;
the Tk widgets in :mod:`filefinder.viewers.widgets` then display it.
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from typing import Any

from .. import preview

try:
    import pymupdf  # type: ignore  # noqa: F401

    HAVE_PDF = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_PDF = False

#: extensions that get a dedicated, formatted viewer
RICH_EXTS = {".hwpx": "HWPX", ".hwp": "HWP", ".docx": "Word", ".xlsx": "Excel", ".pdf": "PDF",
             ".md": "Markdown", ".markdown": "Markdown", ".txt": "Text"}


@dataclass
class Loaded:
    kind: str = ""  # text | rich | sheet | pdf
    model: Any = None
    label: str = ""
    error: str = ""  # binary | folder | encrypted | unreadable | nopdf | <message>


def _text(path: str, label: str = "") -> Loaded:
    pv = preview.load(path)
    if pv.error:
        return Loaded(error=pv.error, label=pv.kind or label)
    return Loaded("text", (pv.text, pv.truncated), label or pv.kind)


def load(path: str) -> Loaded:
    """Parse ``path`` for display. Never raises."""
    from . import docx, hwp, hwpx, markdown, xlsx

    ext = os.path.splitext(path)[1].lower()
    label = RICH_EXTS.get(ext, "")
    try:
        if os.path.isdir(path):
            return Loaded(error="folder")
        if ext == ".pdf":
            if not HAVE_PDF:
                return Loaded(error="nopdf", label=label)
            return Loaded("pdf", path, label)
        if ext == ".txt":
            return _text(path, label)
        try:
            if ext == ".docx":
                return Loaded("rich", docx.read(path), label)
            if ext == ".hwpx":
                return Loaded("rich", hwpx.read(path), label)
            if ext == ".hwp":
                return Loaded("rich", hwp.read(path), label)
            if ext in (".md", ".markdown"):
                return Loaded("rich", markdown.read(path), label)
            if ext == ".xlsx":
                return Loaded("sheet", xlsx.read(path), label)
        except hwp.ViewerError as e:
            return Loaded(error=str(e), label=label)
        except (zipfile.BadZipFile, KeyError, ValueError, TypeError, AttributeError, IndexError, OSError, RecursionError):
            # unreadable as that format: show whatever text can be extracted
            alt = _text(path, label)
            if not alt.error:
                alt.label = f"{label} (text only)"
            else:
                alt.error = alt.error if alt.error != "binary" else "unreadable"
            return alt
        return _text(path)
    except OSError as e:
        return Loaded(error=str(e) or "unreadable")
    except Exception as e:  # a parser bug must never take the UI down
        return Loaded(error=f"unreadable: {type(e).__name__}")
