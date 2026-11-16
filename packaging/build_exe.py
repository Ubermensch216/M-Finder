"""Build distributable standalone executable with PyInstaller.

    python packaging/build_exe.py            # builds dist/M-Finder.exe (single portable file)
    python packaging/build_exe.py --onedir   # optional folder build dist/M-Finder/ (+ zip)

Output: dist/M-Finder.exe (single self-contained executable; no Python or extra files needed).
Requires:  pip install pyinstaller
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mfinder import __version__  # noqa: E402
from mfinder.tray import make_icon_png  # noqa: E402

# Installed in many Python environments or stdlib modules never used by M-Finder; exclude them to keep the exe small and fast to unpack.
HEAVY = [
    "numpy", "scipy", "pandas", "llvmlite", "numba", "PIL", "cryptography", "lxml", "matplotlib", "IPython",
    "sympy", "cv2", "torch", "tensorflow", "uharfbuzz", "pytest", "jedi", "docutils", "sphinx",
    "fontTools", "brotli", "zopfli", "ssl", "_ssl", "asyncio", "_asyncio", "_overlapped",
    "multiprocessing", "_multiprocessing", "sqlite3", "_sqlite3", "xmlrpc",
    # pymupdf 2.x ships an optional ML layout analyser (~45 MB of ONNX models); M-Finder only renders pages / extracts text.
    "pymupdf.layout", "pymupdf4llm", "onnxruntime", "networkx", "psutil", "yaml", "tabulate", "flatbuffers", "google",
]

BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")


def write_icon(path: str) -> None:
    """Multi-size PNG-compressed .ico (16/32/48/256 px) drawn by tray.make_icon_png."""
    sizes = [16, 32, 48, 256]
    pngs = [make_icon_png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, png in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    with open(path, "wb") as f:
        f.write(head + entries + b"".join(pngs))


def write_version_file(path: str) -> None:
    v = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:3]) + (0,)
    text = f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("CompanyName", "M-Finder"),
      StringStruct("FileDescription", "M-Finder - instant file name search"),
      StringStruct("FileVersion", "{__version__}"),
      StringStruct("InternalName", "M-Finder"),
      StringStruct("OriginalFilename", "M-Finder.exe"),
      StringStruct("ProductName", "M-Finder"),
      StringStruct("ProductVersion", "{__version__}")])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])])
  ])
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_spec(spec_path: str, mode: str, icon: str, version: str) -> None:
    entry = os.path.join(ROOT, "packaging", "entry.py")
    locales = os.path.join(ROOT, "mfinder", "locales")
    excludes = ["unittest", "pydoc", "setuptools", "pip", "test"] + HEAVY
    keep_enc = {"ascii.enc", "utf-8.enc", "iso8859-1.enc", "cp949.enc", "cp1252.enc", "unicode.enc"}
    spec = f"""# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [{entry!r}],
    pathex=[{ROOT!r}],
    binaries=[],
    datas=[({locales!r}, 'mfinder/locales')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes={excludes!r},
    noarchive=False,
    optimize=0,
)

_DROP_DATA_PREFIXES = (
    'pymupdf/layout/',
    '_tcl_data/tzdata/',
    '_tcl_data/msgs/',
    '_tk_data/msgs/',
    '_tk_data/images/',
    '_tcl_data/http1.0/',
    '_tcl_data/opt0.4/',
    'tcl8/',
)
_KEEP_ENC = {keep_enc!r}

def _keep_data(item):
    dest = item[0].replace('\\\\', '/')
    if dest.startswith(_DROP_DATA_PREFIXES):
        return False
    if dest.startswith('_tcl_data/encoding/'):
        return dest.rsplit('/', 1)[-1] in _KEEP_ENC
    return True

a.datas = [x for x in a.datas if _keep_data(x)]

_DROP_BINS = {{'libssl-3.dll', 'sqlite3.dll'}}
a.binaries = [x for x in a.binaries if x[0].replace('\\\\', '/').rsplit('/', 1)[-1].lower() not in _DROP_BINS]

pyz = PYZ(a.pure)
"""
    if mode == "onefile":
        spec += f"""
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='M-Finder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version={version!r},
    icon=[{icon!r}],
)
"""
    else:
        spec += f"""
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='M-Finder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version={version!r},
    icon=[{icon!r}],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='M-Finder',
)
"""
    os.makedirs(os.path.dirname(spec_path), exist_ok=True)
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(spec)


def run_pyinstaller(mode: str, icon: str, version: str) -> None:
    work = os.path.join(BUILD, mode)
    spec_path = os.path.join(work, "M-Finder.spec")
    write_spec(spec_path, mode, icon, version)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", work, spec_path,
    ]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true")
    ap.add_argument("--onedir", action="store_true")
    args = ap.parse_args()
    modes = [m for m, on in (("onefile", args.onefile), ("onedir", args.onedir)) if on] or ["onefile"]
    os.makedirs(BUILD, exist_ok=True)
    icon, version = os.path.join(BUILD, "mfinder.ico"), os.path.join(BUILD, "version_info.txt")
    write_icon(icon)
    write_version_file(version)
    # onedir first: both write dist/M-Finder*, onefile creates the file, onedir the folder
    for mode in sorted(modes, reverse=True):
        run_pyinstaller(mode, icon, version)
    outputs = []
    exe = os.path.join(DIST, "M-Finder.exe")
    folder = os.path.join(DIST, "M-Finder")
    zpath = os.path.join(DIST, f"M-Finder-{__version__}.zip")
    sums = os.path.join(DIST, "SHA256SUMS.txt")
    if os.path.isfile(sums):
        os.remove(sums)
    if "onedir" not in modes:
        if os.path.isdir(folder):
            shutil.rmtree(folder)
        if os.path.isfile(zpath):
            os.remove(zpath)
    if "onedir" in modes:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _, fns in os.walk(folder):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    z.write(full, os.path.join("M-Finder", os.path.relpath(full, folder)))
        outputs.append(zpath)
    if "onefile" in modes and os.path.isfile(exe):
        outputs.append(exe)
    for path in outputs:
        print(f"{os.path.getsize(path) / 1e6:7.1f} MB  {path}")
    shutil.rmtree(BUILD, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
