# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/Dev/file_finder/packaging/entry.py'],
    pathex=['D:/Dev/file_finder'],
    binaries=[],
    datas=[('D:/Dev/file_finder/filefinder/locales', 'filefinder/locales')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'pydoc', 'setuptools', 'pip', 'test', 'numpy', 'scipy', 'pandas', 'llvmlite', 'numba', 'PIL', 'cryptography', 'lxml', 'matplotlib', 'IPython', 'sympy', 'cv2', 'torch', 'tensorflow', 'uharfbuzz', 'pytest', 'jedi', 'docutils', 'sphinx'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FileFinder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='D:/Dev/file_finder/build/version_info.txt',
    icon=['D:/Dev/file_finder/build/filefinder.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FileFinder',
)
