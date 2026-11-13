"""Persistent user settings (JSON in %APPDATA%\\FileFinder)."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FileFinder")


def default_filters() -> list[dict]:
    def f(name, search=""):
        return {"name": name, "search": search, "case": False, "ww": False, "path": False, "regex": False}

    return [
        f("Everything"), f("Audio", "audio:"), f("Compressed", "zip:"), f("Document", "doc:"),
        f("Executable", "exe:"), f("Folder", "folder:"), f("Picture", "pic:"), f("Video", "video:"),
    ]


@dataclass
class Settings:
    # indexing
    roots: list[str] = field(default_factory=list)  # empty = all fixed drives
    filelists: list[str] = field(default_factory=list)  # .efu/.csv/.txt files indexed as extra roots
    use_mft: bool = True
    use_service: bool = True  # use the FileFinder service for MFT/USN access when not elevated
    monitor: bool = True
    monitor_folders: bool = True  # ReadDirectoryChangesW for non-NTFS / folder indexes
    folder_sizes: bool = True
    exclude_hidden: bool = False
    exclude_system: bool = False
    exclude_folders: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    db_path: str = ""
    # search
    match_case: bool = False
    match_whole_word: bool = False
    match_path: bool = False
    regex: bool = False
    sort_key: str = "name"
    sort_ascending: bool = True
    content_max_mb: int = 64
    filters: list[dict] = field(default_factory=default_filters)
    current_filter: str = "Everything"
    bookmarks: list[dict] = field(default_factory=list)  # {name, search, filter, case, ww, path, regex, sort, asc}
    # view
    theme: str = "system"  # system | light | dark
    language: str = "auto"  # auto | en | ko | <locale file>
    font_size: int = 10
    confirm_large_sort: bool = True
    show_status_bar: bool = True
    show_folder_sizes: bool = True
    show_preview: bool = True
    preview_height: int = 220
    geometry: str = "900x600"
    column_widths: dict = field(default_factory=lambda: {"name": 280, "path": 380, "size": 90, "mtime": 140})
    # background / shell integration
    tray_icon: bool = False
    run_in_background: bool = False  # closing the window hides it to the tray
    start_minimized: bool = False
    global_hotkey: str = ""  # e.g. "ctrl+alt+f"
    run_on_startup: bool = False
    single_instance: bool = True
    # servers
    ipc_enabled: bool = True
    http_enabled: bool = False
    http_bind: str = "127.0.0.1"
    http_port: int = 8080
    http_user: str = ""
    http_password: str = ""
    etp_enabled: bool = False
    etp_bind: str = "127.0.0.1"
    etp_port: int = 2121
    etp_user: str = ""
    etp_password: str = ""
    etp_allow_download: bool = False

    @property
    def database_file(self) -> str:
        return self.db_path or os.path.join(app_dir(), "fileindex.db")

    @property
    def settings_file(self) -> str:
        return os.path.join(app_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            with open(s.settings_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return s
        known = {f.name for f in dataclasses.fields(cls)}
        for k, v in data.items():
            if k in known:
                setattr(s, k, v)
        return s

    def save(self) -> None:
        os.makedirs(app_dir(), exist_ok=True)
        tmp = self.settings_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(self), f, indent=2)
        os.replace(tmp, self.settings_file)

    def find_filter(self, name: str) -> dict | None:
        for f in self.filters:
            if f.get("name") == name:
                return f
        return None
