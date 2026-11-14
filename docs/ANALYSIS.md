# Everything 1.5.0.1416b — analysis notes

Target: `C:\Program Files\Everything` (`Everything.exe` 5,118,632 bytes, SHA-256
`1171a761…c637c`, PE, voidtools). Tooling: [REA](https://github.com/morluto/rea)
`rea-agents` 6.8.0 CLI (`inspect-artifact`, `inspect-pe-imports`,
`inspect-pe-resources`, `inspect-desktop-application`), run statically — the
program was never executed or modified.

## What the analysis could and could not establish

| Source | Result |
|---|---|
| REA PE import table (477 functions, 9 DLLs) | Strong evidence of architecture — see below |
| `Everything.ini` | Feature switches (IPC, pipe IPC, service, context menu, export, ETP…) |
| `Everything.lng` | A bzip2 container of per-language string tables; the decoded UI strings name the features and error paths (USN journal, database, ETP/HTTP servers, folder indexes, exclude rules, sort confirmation) |
| Disassembly / decompilation | **Not available.** REA needs Hopper, Ghidra or IDA for native pseudocode and none is installed here. `Everything.exe` also keeps almost no plaintext strings. Nothing below is claimed from decompiled code. |

Where behaviour is not evidenced by the binary, this project follows
Everything's publicly documented search syntax and standard NTFS APIs. It is a
clean-room, functionally equivalent implementation: no Everything code or
assets were copied.

## Architecture evidenced by the imports

* **Indexing**: `CreateFileW` + `DeviceIoControl` (volume handle, USN/MFT
  control codes), `GetVolumeInformationW`, `GetDriveTypeW`,
  `GetLogicalDriveStringsW`, `GetDiskFreeSpace*` → enumerate fixed NTFS
  volumes and read the MFT / USN journal. `FindFirstFileW/FindNextFileW` and
  `FindFirstChangeNotificationW` → a folder-walk fallback for non-NTFS and
  folder indexes.
* **Privilege handling**: `OpenProcessToken`, `GetTokenInformation`,
  `LookupPrivilegeValueW`, `AdjustTokenPrivileges` plus the service APIs
  (`CreateServiceW`, `StartServiceW`, `ControlService`,
  `RegisterServiceCtrlHandlerW`, `StartServiceCtrlDispatcherW`,
  `SetServiceObjectSecurity`) → consistent with a service doing the raw-volume
  work for an unelevated UI (`allow_everything_service`, `index_as_admin` in the
  ini); the actual split of duties was not verified without a decompiler.
* **IPC**: `CreateNamedPipeW`, `ConnectNamedPipe`, `WaitNamedPipeW`,
  `FindWindowW`/window messages → the SDK/`es.exe` interface
  (`allow_ipc`, `allow_ipc_pipe`, `allow_window_message_filter_ipc`).
* **UI/shell**: `comctl32` (list-view, image lists), `Shell_NotifyIconW`
  (tray), `RegisterHotKey` (global "new window" hotkey), `SHFileOperationW`
  (delete/rename/copy/move), `ShellExecuteExW`, `SHBrowseForFolderW`,
  `SHGetFileInfoW` (icons), clipboard + `DragAcceptFiles` (copy/paste/drag-drop),
  `GetOpenFileNameW`/`GetSaveFileNameW` (export), `SetWindowsHookExW`.
* **Networking** (from strings; the import table has no Winsock functions, so
  it is probably loaded at run time — not verified): ETP/FTP and HTTP servers
  with port, user/password, logging, "allow download".

## Behaviour taken from the decoded UI strings

* Database: `Everything.db`, written to `Everything.db.tmp` then replaced;
  explicit errors for *does not exist / not a valid database / unsupported
  version / corrupt / out of date*. → `fileindex.db`, atomic replace, same
  error classes (`index.DatabaseError`).
* Indexing phases: create/delete/open **USN Journal**, sort folder indexes
  (FRN → pointer conversion, removing sparse folders, sorting names), "Saving to
  disk…".
* Per-volume options: include in database, monitor changes, USN journal
  maximum size / allocation delta.
* Folder indexes and an ordered include/exclude rule list (folder, file,
  wildcard filter); "exclude hidden / system".
* Search toggles: match case (Ctrl+I), whole word (Ctrl+B), match path
  (Ctrl+U), regex (Ctrl+R) — default off.
* Columns Name / Path / Size / Date Modified; export of results; "confirm
  large sort" dialog; "confirm opening N files"; zoom in/out/normal; status bar
  showing object and selection counts; context menu (Open, Open Path, Copy
  Path/Name/Full Path, Delete).

## Mapping to this implementation

| Everything feature | M-Finder module |
|---|---|
| MFT enumeration (`FSCTL_ENUM_USN_DATA`) | `ntfs.index_volume` |
| Size / dates straight from `$MFT` | `mftparse` (record fixups, `$STANDARD_INFORMATION`, `$DATA`, runlists) |
| USN journal live updates, resume from saved USN | `ntfs.UsnMonitor`, `ntfs.apply_usn_record`, `index.roots[*].next_usn` |
| Folder / non-NTFS indexing and live updates | `scanner`, `watcher` (`ReadDirectoryChangesW`) |
| Exclude hidden/system/folders/wildcards | `scanner.Excludes` (applied in every indexer) |
| Compact index, sorted views, folder sizes | `index.Index` (struct-of-arrays), `query.sort_results`, `Index.compute_folder_sizes` |
| `Everything.db` | `Index.save` / `Index.load` (zlib, atomic, format v2) |
| Search syntax incl. `dm:` `dc:` `da:` `content:` | `query.py` |
| File lists (`.efu`) as index sources, export | `filelist.py` |
| Filters and bookmarks | `settings.filters/bookmarks`, `Engine.search`, `gui.SearchWindow` |
| Service for unelevated UI (privileged volume access) | `service.py` (`VolumeProxy`, `ServiceVolume`, SCM install/control) |
| Named-pipe IPC (SDK / `es.exe` role) | `ipc.py`, `queryipc.py`, `mfinder search` |
| Tray icon, global hotkey, run in background, run at startup | `tray.py`, `winshell.py`, `gui.Controller` |
| Multiple search windows sharing one index | `gui.Controller` / `gui.SearchWindow` |
| ETP/FTP server | `etp.py` |
| HTTP server | `httpserver.py` (read-only) |
| Themes, language packs | `theme.py`, `i18n.py` + `locales/*.json` |
| Elevation helper | `winshell.relaunch_as_admin` |

## Deliberate differences / remaining gaps

* **Service location rule.** A SYSTEM service that runs files from a
  user-writable folder is a privilege-escalation hole, so `service install`
  refuses unless Python and this package sit under Program Files/Windows
  (override: `--unsafe-allow-user-paths`). The service exposes only journal/MFT
  names and `$MFT`-parsed metadata — never raw disk bytes — to fixed local
  drives, and only to the SIDs it was installed for. **The SCM install/start/stop
  code path has not been exercised on a real machine** (the proxy logic itself
  is tested through the same pipe server in console mode).
* **IPC is not binary-compatible with Everything's SDK.** Everything uses
  `WM_COPYDATA` window messages and its own pipe protocol; programs written for
  the Everything SDK will not talk to M-Finder. M-Finder has its own
  per-user pipe protocol (`queryipc.py`) and `mfinder search`.
* **ETP.** The server speaks plain FTP (passive mode, read-only) plus
  `SITE SEARCH <query>`; Everything's ETP wire extensions were not observable
  in the binary analysis, so Everything's own ETP client may not be able to
  search it. There is no ETP *client* inside the GUI.
* **Content search** reads files on demand (UTF-8, UTF-16LE, ANSI; ASCII case
  folding), with a size cap; it is a scan, not an index.
* **Folder size** is recomputed in the background after changes (every ~30 s),
  not incrementally per event.
* **Not implemented:** custom column sets, file-type icons, drag and drop,
  rename in place, check-for-updates, a bundled installer / `.exe`.
