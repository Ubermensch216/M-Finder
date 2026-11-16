# M-Finder 1.0 — Architecture & Design Notes

**M-Finder** is a high-performance desktop file and document search engine for Windows, featuring raw NTFS volume indexing (`$MFT` and USN Change Journal), structured document content search, and native format-aware file previews.

## System Architecture

* **Indexing (`ntfs.py`, `mftparse.py`, `scanner.py`)**:
  * Enumerates fixed NTFS volumes via `CreateFileW` + `DeviceIoControl` (`FSCTL_ENUM_USN_DATA`) and reads `$MFT` records directly (`$STANDARD_INFORMATION`, `$DATA`, runlists) to populate file sizes and timestamps without per-file `stat()` calls.
  * Provides a directory-walk fallback (`os.scandir` / `FindFirstFileW`) for non-NTFS drives and custom folder roots.
* **Live Change Monitoring (`ntfs.py`, `watcher.py`)**:
  * Tracks volume changes in real time via the NTFS USN Change Journal (`FSCTL_READ_USN_JOURNAL`), resuming from the saved USN position on startup.
  * Monitors folder roots and non-NTFS volumes using `ReadDirectoryChangesW`.
* **Privilege Separation (`service.py`, `winshell.py`)**:
  * Supports running elevated (`Restart as Administrator`) or delegating raw volume access to the optional `MFinderSvc` Windows service so the GUI can run as a standard user.
* **In-Memory & On-Disk Database (`index.py`)**:
  * Uses a compact struct-of-arrays layout (`Index`) pre-ordered by `(roots, folders, files)` (`sort_in_place()`) with lazy `frn_maps`, separate structural (`rev`) vs metadata (`meta_rev`) revisions, background folder size computation (`compute_folder_sizes`), and atomic compressed persistence (`fileindex.db`).
* **Query Engine (`query.py`)**:
  * Compiles boolean queries (`AND`, `OR`, `NOT`, `< >` grouping), wildcards, regular expressions, path matching, metadata ranges (`size:`, `dm:`, `dc:`, `da:`, `len:`, `depth:`, `attrib:`), type macros (`audio:`, `pic:`, `doc:`, `video:`, `zip:`, `exe:`), and on-demand file content searches (`content:`).
* **Format-Aware Preview & Content Extraction (`preview.py`, `preview_pane.py`, `viewers/`)**:
  * Extracts text and renders formatted documents directly in the preview pane:
    * **HWP (`.hwp`) & HWPX (`.hwpx`)**: Built-in OLE/OWPML parsers for paragraphs, character styles, merged tables, and embedded images, plus optional page-accurate rendering via `rhwp`.
    * **Office Open XML (`.docx`, `.xlsx`, `.pptx`) & ODF (`.odt`, `.ods`, `.odp`)**: Styled Word documents, interactive Excel spreadsheet grids with number/date formatting, and presentation text extraction.
    * **PDF (`.pdf`) & Images**: Multi-page PDF rendering with hit highlighting and image scaling via `PyMuPDF` / `Pillow` / Tk.
    * **Markdown (`.md`), HTML (`.html`/`.htm`), Plain Text**: Rich formatting and toggleable HTML page/code views.
* **IPC & Network Servers (`ipc.py`, `queryipc.py`, `httpserver.py`, `etp.py`)**:
  * Per-user SID-ACL protected Named Pipe IPC for CLI queries (`m-finder search`) and single-instance window reuse.
  * Built-in read-only HTTP server (HTML & JSON) and passive-mode FTP/ETP server.

## Module Mapping

| Feature Area | M-Finder Module (`mfinder/`) |
|---|---|
| MFT enumeration (`FSCTL_ENUM_USN_DATA`) | `ntfs.index_volume` |
| Size / dates straight from `$MFT` | `mftparse` (record fixups, `$STANDARD_INFORMATION`, `$DATA`, runlists) |
| USN journal live updates, resume from saved USN | `ntfs.UsnMonitor`, `ntfs.apply_usn_record`, `index.roots[*].next_usn` |
| Folder / non-NTFS indexing and live updates | `scanner`, `watcher` (`ReadDirectoryChangesW`) |
| Exclude hidden/system/folders/wildcards | `scanner.Excludes` (applied in every indexer) |
| Compact index, sorted views, folder sizes | `index.Index` (struct-of-arrays), `query.sort_results`, `Index.compute_folder_sizes` |
| On-disk index database (`fileindex.db`) | `Index.save` / `Index.load` (zlib, atomic, format v2) |
| Search syntax incl. `dm:` `dc:` `da:` `content:` | `query.py` |
| File lists (`.efu`, `.csv`, `.txt`) import & export | `filelist.py` |
| Filters and bookmarks | `settings.filters/bookmarks`, `Engine.search`, `gui.SearchWindow` |
| Service for unelevated UI (privileged volume access) | `service.py` (`VolumeProxy`, `ServiceVolume`, SCM install/control) |
| Named-pipe IPC (`m-finder search`) | `ipc.py`, `queryipc.py` |
| Tray icon, global hotkey, run in background, run at startup | `tray.py`, `winshell.py`, `gui.Controller` |
| Multiple search windows sharing one index | `gui.Controller` / `gui.SearchWindow` |
| FTP/ETP server | `etp.py` |
| HTTP server | `httpserver.py` (read-only) |
| Themes, language packs | `theme.py`, `i18n.py` + `locales/*.json` |
| Elevation helper | `winshell.relaunch_as_admin` |
| Rich file preview pane & structured content extraction | `preview.py`, `preview_pane.py`, `viewers/` (shared viewer helpers in `viewers/common.py`) |
| Standalone executable packaging | `packaging/build_exe.py`, `packaging/entry.py` (PyInstaller `--onefile` default to `dist/M-Finder.exe` with trimmed Tcl/Tk & module exclusions, optional `--onedir`) |

## Design & Security Notes

* **Service location rule**: A SYSTEM service running code from a user-writable folder is a privilege-escalation risk. When running as a standalone `M-Finder.exe`, `service install` automatically stages a copy to `C:\Program Files\M-Finder\M-Finder.exe` (`_stage_frozen_exe`). For non-frozen source runs, `service install` refuses unless both Python and the `mfinder` package reside under `Program Files` or `Windows` (override: `--unsafe-allow-user-paths`). The service exposes only journal/MFT names and `$MFT`-parsed metadata — never raw disk bytes — for fixed local drives and the installed SIDs.
* **Per-user IPC isolation**: Named pipes use an explicit SDDL DACL restricted to the current user's SID and reject remote network clients.
* **Content search**: Reads files on demand (UTF-8, UTF-16LE, ANSI, plus `.hwp`, `.hwpx`, `.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`, and `.pdf`) up to a configurable size cap (`content_max_mb`).
* **Folder sizes**: Recomputed in the background after index changes (~30 s interval).
