"""Tiny gettext-style localisation: English source strings are the keys.

Language files are JSON objects ``{"English text": "translation"}``. Built-in ones
live in ``filefinder/locales``; users can add ``*.json`` files to
``%APPDATA%\\FileFinder\\locales`` (the file name, e.g. ``de.json``, is the language code).
"""

from __future__ import annotations

import json
import os
import sys

from .settings import app_dir

_table: dict[str, str] = {}
_LANG_NAMES = {"en": "English", "ko": "한국어"}
_PRIMARY_LANG_IDS = {0x12: "ko", 0x09: "en", 0x07: "de", 0x0C: "fr", 0x0A: "es", 0x11: "ja", 0x04: "zh"}


def _dirs() -> list[str]:
    return [os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales"), os.path.join(app_dir(), "locales")]


def available() -> dict[str, str]:
    """code -> display name for every language that can be selected."""
    langs = {"en": "English"}
    for d in _dirs():
        try:
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    code = fn[:-5]
                    langs[code] = _LANG_NAMES.get(code, code)
        except OSError:
            pass
    return langs


def system_language() -> str:
    if sys.platform == "win32":
        import ctypes

        try:
            return _PRIMARY_LANG_IDS.get(ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF, "en")
        except OSError:
            pass
    return "en"


def init(language: str = "auto") -> str:
    """Load ``language`` ('auto' = Windows UI language). Returns the code actually used."""
    global _table
    code = system_language() if language == "auto" else language
    _table = {}
    if code != "en":
        for d in _dirs():
            path = os.path.join(d, code + ".json")
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _table.update({str(k): str(v) for k, v in data.items()})
            except (OSError, ValueError):
                pass
    return code if _table or code == "en" else "en"


def _(text: str) -> str:
    return _table.get(text, text)
