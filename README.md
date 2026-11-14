# M-Finder

Instant file-name search for Windows, functionally modelled on
[Everything](https://www.voidtools.com/) 1.5. See [docs/ANALYSIS.md](docs/ANALYSIS.md)
for how Everything was analysed (with [REA](https://github.com/morluto/rea)),
which behaviour is evidenced, and the deliberate differences.

## Features

* **Fast indexing** — reads the NTFS Master File Table (~800k–950k entries in 4–14 s) and parses `$MFT` directly for size and dates (800k entries in ~15 s, exact match with `stat`).
* **Live updates** — NTFS USN change journal (resumes from the saved position); `ReadDirectoryChangesW` for folders and non-NTFS volumes.
* **Instant search** — 30–60 ms over ~1M entries, cancelable while you type.
* **Everything search syntax** — AND/OR/NOT/groups, wildcards, `regex:`, `case:`, `ww:`, `path:`, `ext:`, `size:`, `dm:`/`dc:`/`da:`, `parent:`, `empty:`, `content:` (file contents), macros (`pic:`, `doc:` …).
* **File preview with the file's own formatting** — select a result and a pane shows it the way the file looks (View → Preview Pane → *Bottom / Right / Left*, `F4` to show/hide, Ctrl+wheel or `+`/`−` to zoom). `content:` hits are highlighted and ◀ ▶ jump between them. One viewer per format:
  * **.hwp / .hwpx** — with the optional [rhwp](https://github.com/edwardkim/rhwp) CLI installed (see below) every page is typeset exactly like Hancom Office: paper size and margins, headers/footers, line breaking, table borders, shapes, equations and embedded fonts; without it the built-in reader shows fonts, sizes, colours, bold/italic/underline, alignment, indents, tables (merged cells, fills, borders) and pictures
  * **pictures** — .png .jpg .gif .bmp .tif .webp .ico, fitted to the pane (Ctrl+wheel zooms, double-click resets); .webp/.ico/.tif need Pillow, the rest work with PyMuPDF alone
  * **.docx** — styles, numbering/bullets, tables with merged cells, pictures, hyperlinks, highlights
  * **.xlsx** — a spreadsheet grid: sheet tabs, column widths, merged cells, fills, fonts, borders and number/date formats
  * **.pdf** — rendered pages (needs the optional `pymupdf` package; it is bundled in the exe)
  * **.md** — headings, lists, task lists, quotes, tables, code blocks, links, local images
  * **.txt** and anything else text-like — UTF-8/UTF-16/ANSI auto-detected
  
  **Exact HWP/HWPX preview with rhwp** — download `rhwp-v*-windows-x86_64.zip` from the [rhwp releases](https://github.com/edwardkim/rhwp/releases) (MIT licence), check it against `SHA256SUMS.txt`, and unpack it to `%APPDATA%\M-Finder\rhwp\` (or `rhwp\` next to `M-Finder.exe`, or set `rhwp_path` in `settings.json`). Pages are rendered on demand by `rhwp export-pdf -p N` (about 5 s per page; the page you look at first, then the next few — all of them while a `content:` match is being marked) and cached in `%APPDATA%\M-Finder\cache\rhwp\` (600 MB cap, least recently used first), so re-opening a document is instant. If rhwp is missing or cannot read a file the built-in reader is used. rhwp never touches the network unless `"rhwp_webfonts": true` is set (then it may download missing Korean fonts from a public CDN into its own font cache); `"rhwp_enabled": false` turns the whole thing off.

`content:` searches also look *inside* HWP/HWPX/Word/Excel/PowerPoint/OpenDocument files and PDFs (no IFilter or Office needed). Floating shapes/text boxes in HWP are shown in reading order, not at their page position.
* **Folder sizes**, computed in the background and searchable (`folder: size:>1gb`).
* **GUI** — virtual list that stays fast with millions of results, sortable/resizable columns, **filters** (Audio/Picture/… + your own), **bookmarks**, several windows sharing one index, **dark/light theme**, **English/한국어** (add your own `locales/*.json`), tray icon, global hotkey, run in background / at startup.
* **File lists** — index `.efu`/`.csv`/`.txt` lists as extra sources; export results as `.efu`/`.csv`/`.txt`.
* **Servers** — read-only HTTP search page/JSON; ETP-style FTP server (browse, `SITE SEARCH`, optional downloads); per-user named-pipe IPC (used by `mfinder search`, and to reuse a running instance).
* **Service** — optional privileged helper so the GUI can use the MFT/USN journal **without running as administrator**.

Requires Windows and Python 3.10+ (standard library only; tested on 3.14). PDF preview and PDF/picture handling use the optional `pymupdf` package (`pip install pymupdf`, or `pip install .[pdf]`); everything else works without it.

## Run

```powershell
python -m mfinder                          # GUI
python -m mfinder "ext:py size:>50kb"      # GUI pre-filled (reuses a running instance)
python -m mfinder search -l -n 20 "report ext:docx;pdf dm:thisyear"
python -m mfinder search -f Picture --export pics.efu "holiday"
python -m mfinder search "content:TODO ext:py"
python -m mfinder index                    # rebuild the database
python -m mfinder serve --port 8080        # HTTP
python -m mfinder etp --allow-download --user me --password secret
```

`search` asks a running M-Finder over IPC first (live results), otherwise loads
the database. Settings and the database live in `%APPDATA%\M-Finder\`.

### MFT access: administrator or the service

Windows only lets administrators open a raw volume. Options:

1. **Run elevated** (Tools → *Restart as Administrator* in the GUI) — MFT indexing and live USN updates work immediately.
2. **Install the service** (elevated prompt, once): `python -m mfinder service install`, then use the GUI as a normal user. `install` refuses unless Python and this folder are under *Program Files* (a SYSTEM service must not run user-writable code); `--unsafe-allow-user-paths` overrides that and is a privilege-escalation risk. The service serves only names/metadata of fixed local NTFS drives, only to your SID (`--allow-sid` to add others). Test without installing: `python -m mfinder service run-console` in an elevated shell.
3. **Neither** — falls back to walking folders (works everywhere; live updates via change notifications).

## Executables

`python packaging/build_exe.py` (needs `pip install pyinstaller`) writes to `dist/`:

| File | Use |
|---|---|
| `M-Finder.exe` | single portable file (~12 MB), no Python needed. Unpacks to a temp folder at start (~1–2 s). |
| `M-Finder/` + `M-Finder-0.1.0.zip` | folder build: faster start; the one to install under `C:\Program Files\M-Finder` if you want the service. |
| `SHA256SUMS.txt` | checksums |

The exe is a windowed program: double-click for the GUI; from a terminal the CLI works the same
(`M-Finder.exe search "ext:pdf"`, `M-Finder.exe service status`) and prints to that terminal.
The exes are **unsigned**, so Windows SmartScreen / some antivirus products may warn about them
(common for PyInstaller programs); sign them with your own certificate for wide distribution.
For the service: copy the folder build to Program Files, then run `M-Finder.exe service install` from an elevated prompt.

## Search syntax

See *Help → Search Syntax* in the app, or [`query.py`](mfinder/query.py):

```
report final                 both words in the name
cat | dog  !draft            OR / NOT
"my file"  *.txt  img_??.png wildcards match the whole name
c:\users\*\desktop           a backslash switches to full-path matching
ext:jpg;png size:1mb..10mb   functions
dm:last2weeks  dc:2024-05  da:>2024-01-01
content:"hello world" ext:txt   search inside files (UTF-8/UTF-16/ANSI, HWP, DOCX, XLSX, PDF ...)
regex:^IMG_\d{4}\.jpg$
```

Shortcuts: Ctrl+I case, Ctrl+B whole word, Ctrl+U path, Ctrl+R regex, Ctrl+P options, F5 rebuild,
Ctrl+N new window, Ctrl+D bookmark, Ctrl+Shift+C copy full path, Ctrl +/-/0 zoom, Del recycle.

## Security notes

* HTTP and FTP servers are **off by default** and bind `127.0.0.1`. They send credentials in clear text — set a password before binding to a network address. HTTP never serves file contents; FTP serves them only with *Allow downloads*, only for paths in the index, and refuses links that leave the indexed folder.
* The IPC pipe is named after your SID and its ACL grants only you; remote clients are rejected.
* "Run at startup" writes a per-user `Run` registry value (nothing is installed system-wide unless you install the service).

## Tests

```powershell
python -m unittest discover -s tests                         # 40+ tests
$env:MF_GUI_TEST = 1; python -m unittest discover -s tests   # + Tk GUI smoke test
```
An elevated shell additionally runs a check that `$MFT`-parsed sizes/dates equal `os.stat`.

## Limitations

See [docs/ANALYSIS.md](docs/ANALYSIS.md#deliberate-differences--remaining-gaps): not
SDK-compatible with Everything, the service install path is untested on a real SCM,
no GUI ETP client, no installer.
