"""Build distributable executables with PyInstaller.

    python packaging/build_exe.py            # onefile + onedir
    python packaging/build_exe.py --onefile  # only dist/M-Finder.exe
    python packaging/build_exe.py --onedir   # only dist/M-Finder/ (+ zip)

Output: dist/M-Finder.exe (single file, portable), dist/M-Finder/ (folder; use this one
for the Windows service, which must live under Program Files) and dist/M-Finder-<ver>.zip.
Requires:  pip install pyinstaller
"""

from __future__ import annotations

import argparse
import hashlib
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

# Installed in many Python environments, never used by M-Finder; PyInstaller would otherwise bundle them.
HEAVY = ["numpy", "scipy", "pandas", "llvmlite", "numba", "PIL", "cryptography", "lxml", "matplotlib", "IPython",
         "sympy", "cv2", "torch", "tensorflow", "uharfbuzz", "pytest", "jedi", "docutils", "sphinx"]

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


def run_pyinstaller(mode: str, icon: str, version: str) -> None:
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole", f"--{mode}",
        "--name", "M-Finder", "--icon", icon, "--version-file", version,
        "--add-data", f"{os.path.join(ROOT, 'mfinder', 'locales')}{sep}mfinder/locales",
        "--distpath", DIST, "--workpath", os.path.join(BUILD, mode), "--specpath", os.path.join(BUILD, mode),
        "--paths", ROOT,
        "--exclude-module", "unittest", "--exclude-module", "pydoc", "--exclude-module", "setuptools",
        "--exclude-module", "pip", "--exclude-module", "test",  # distutils must stay: pymupdf imports it
        *[a for m in HEAVY for a in ("--exclude-module", m)],
        os.path.join(ROOT, "packaging", "entry.py"),
    ]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true")
    ap.add_argument("--onedir", action="store_true")
    args = ap.parse_args()
    modes = [m for m, on in (("onefile", args.onefile), ("onedir", args.onedir)) if on] or ["onefile", "onedir"]
    os.makedirs(BUILD, exist_ok=True)
    icon, version = os.path.join(BUILD, "mfinder.ico"), os.path.join(BUILD, "version_info.txt")
    write_icon(icon)
    write_version_file(version)
    # onedir first: both write dist/M-Finder*, onefile creates the file, onedir the folder
    for mode in sorted(modes, reverse=True):
        run_pyinstaller(mode, icon, version)
    lines = []
    exe = os.path.join(DIST, "M-Finder.exe")
    folder = os.path.join(DIST, "M-Finder")
    if "onedir" in modes:
        zpath = os.path.join(DIST, f"M-Finder-{__version__}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _, fns in os.walk(folder):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    z.write(full, os.path.join("M-Finder", os.path.relpath(full, folder)))
        lines.append((zpath, sha256(zpath)))
    if "onefile" in modes and os.path.isfile(exe):
        lines.append((exe, sha256(exe)))
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
        for path, digest in lines:
            f.write(f"{digest}  {os.path.basename(path)}\n")
            print(f"{os.path.getsize(path) / 1e6:7.1f} MB  {digest}  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
