"""Picture files (.png, .jpg, .gif, .bmp ...) -> a decoded :class:`Raster` for the image viewer.

Decoding happens off the UI thread. Backends, best first: Pillow (many formats, EXIF rotation,
transparency), PyMuPDF (png/jpg/gif/bmp/tiff), then Tk's own PNG/GIF reader (shown 1:1).
"""

from __future__ import annotations

import base64
import os

from . import common as C

try:  # optional
    from PIL import Image as _PIL, ImageOps as _PILOps  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    _PIL = _PILOps = None

try:  # optional
    import pymupdf  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    pymupdf = None

#: extensions previewed as pictures (what actually decodes depends on the installed backends)
EXTS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".jfif": "JPEG", ".gif": "GIF",
        ".bmp": "BMP", ".dib": "BMP", ".webp": "WebP", ".tif": "TIFF", ".tiff": "TIFF", ".ico": "Icon"}

MAX_FILE = 256 << 20
MAX_PIXELS = 400_000_000  # decoded size cap (a decompression bomb must not eat all memory)


class ImageError(Exception):
    pass


class Raster:
    """A decoded picture that can hand out RGB PPM data at any size."""

    def __init__(self, width: int, height: int, fmt: str = "") -> None:
        self.width, self.height, self.fmt = width, height, fmt
        self.native = False  # True: no resampling possible, shown 1:1 (Tk fallback)

    def ppm(self, w: int, h: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def tk_data(self) -> bytes | None:
        return None


class _PilRaster(Raster):
    def __init__(self, im, fmt: str) -> None:
        super().__init__(im.width, im.height, fmt)
        self.im = im

    def ppm(self, w: int, h: int) -> bytes:
        im = self.im
        if (w, h) != im.size:
            resample = _PIL.Resampling.LANCZOS if w < im.width else _PIL.Resampling.BICUBIC
            im = im.resize((w, h), resample, reducing_gap=2.0 if w < im.width else None)
        return b"P6 %d %d 255\n" % im.size + im.tobytes()


class _MuRaster(Raster):
    def __init__(self, pix, fmt: str) -> None:
        super().__init__(pix.width, pix.height, fmt)
        self.pix = pix

    def ppm(self, w: int, h: int) -> bytes:
        pix = self.pix
        if (w, h) != (pix.width, pix.height):
            pix = pymupdf.Pixmap(pix, w, h)
        return pix.tobytes("ppm")


class _TkRaster(Raster):
    def __init__(self, data: bytes, w: int, h: int, fmt: str) -> None:
        super().__init__(w, h, fmt)
        self.data = data
        self.native = True

    def tk_data(self) -> bytes:
        return base64.b64encode(self.data)


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _gif_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    return 0, 0


def _pil_decode(data: bytes, fmt_hint: str) -> Raster:
    import io

    _PIL.MAX_IMAGE_PIXELS = MAX_PIXELS
    with _PIL.open(io.BytesIO(data)) as im:
        fmt = (im.format or fmt_hint or "").upper()
        if im.width * im.height > MAX_PIXELS:
            raise ImageError("too large")
        im.seek(0)  # animated GIF / multi-page TIFF: first frame
        im = _PILOps.exif_transpose(im) or im
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = _PIL.new("RGBA", rgba.size, (255, 255, 255, 255))
            bg.alpha_composite(rgba)
            im = bg.convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")
        else:
            im.load()
    return _PilRaster(im, fmt)


def _mu_decode(data: bytes, fmt_hint: str) -> Raster:
    pix = pymupdf.Pixmap(data)
    if pix.width * pix.height > MAX_PIXELS:
        raise ImageError("too large")
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return _MuRaster(pix, fmt_hint)


def read(path: str) -> Raster:
    """Decode ``path``. Raises :class:`ImageError` / OSError when it is not a readable picture."""
    if os.path.getsize(path) > MAX_FILE:
        raise ImageError("too large")
    with open(path, "rb") as fh:  # read fully so no handle stays open (the file could not be moved/deleted)
        data = fh.read()
    fmt_hint = EXTS.get(os.path.splitext(path)[1].lower(), "")
    errors: list[Exception] = []
    for backend, ok in ((_pil_decode, _PIL is not None), (_mu_decode, pymupdf is not None)):
        if not ok:
            continue
        try:
            return backend(data, fmt_hint)
        except ImageError:
            raise
        except Exception as e:  # truncated / unsupported: try the next backend
            errors.append(e)
    kind = C.image_kind(data)
    if kind in ("png", "gif"):
        w, h = _png_size(data) if kind == "png" else _gif_size(data)
        if w and h:
            return _TkRaster(data, w, h, kind.upper())
    raise ImageError("unreadable")
