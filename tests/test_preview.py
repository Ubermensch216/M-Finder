import os
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from filefinder import preview

SS, MSS, CUTOFF = 512, 64, 4096
FREE, ENDC, FATSEC = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


def build_ole(streams: dict[str, bytes]) -> bytes:
    """Minimal OLE2 writer. Keys are 'Name' or 'Storage/Name' (one level)."""
    storages = sorted({k.split("/")[0] for k in streams if "/" in k})
    # directory entries: root, storages, streams
    entries = [("Root Entry", 5, None)]
    for st in storages:
        entries.append((st, 1, None))
    for k, v in streams.items():
        entries.append((k.split("/")[-1], 2, v))
    # place data
    mini = bytearray()
    mini_fat: list[int] = []
    start_of: dict[int, int] = {}
    big: list[tuple[int, bytes]] = []
    for i, (_n, typ, data) in enumerate(entries):
        if typ != 2:
            continue
        if len(data) < CUTOFF:
            start_of[i] = len(mini) // MSS
            n = max(1, -(-len(data) // MSS))
            for j in range(n):
                mini_fat.append(len(mini_fat) + 1 if j < n - 1 else ENDC)
            mini += data.ljust(n * MSS, b"\0")
        else:
            big.append((i, data))
    sectors: list[bytes] = []
    fat: list[int] = []

    def add(data: bytes) -> int:
        n = max(1, -(-len(data) // SS))
        first = len(sectors)
        for j in range(n):
            sectors.append(data[j * SS : (j + 1) * SS].ljust(SS, b"\0"))
            fat.append(len(sectors) if j < n - 1 else ENDC)
        return first

    ministream_start = add(bytes(mini)) if mini else ENDC
    minifat_start = ENDC
    if mini_fat:
        minifat_start = add(struct.pack(f"<{len(mini_fat)}I", *mini_fat))
    for i, data in big:
        start_of[i] = add(data)
    # directory
    dirents = bytearray()
    child = {}
    # root.child -> first top-level entry, chained through "right"
    top = [i for i, (n, t, d) in enumerate(entries) if i > 0 and not any(
        k.endswith(n) and "/" in k for k in streams if t == 2 and k.split("/")[-1] == n)]
    in_storage = {st: [] for st in storages}
    for k in streams:
        if "/" in k:
            idx = next(i for i, (n, t, d) in enumerate(entries) if t == 2 and n == k.split("/")[-1])
            in_storage[k.split("/")[0]].append(idx)
    right = {}

    def chain(lst):
        for a, b in zip(lst, lst[1:]):
            right[a] = b
        return lst[0] if lst else FREE

    child[0] = chain(top)
    for st in storages:
        child[1 + storages.index(st)] = chain(in_storage[st])
    for i, (name, typ, data) in enumerate(entries):
        nm = name.encode("utf-16-le") + b"\0\0"
        e = nm.ljust(64, b"\0") + struct.pack("<HBB", len(nm), typ, 1)
        e += struct.pack("<III", FREE, right.get(i, FREE), child.get(i, FREE))
        e = e.ljust(116, b"\0")
        if typ == 5:
            st, size = ministream_start, len(mini)
        elif typ == 2:
            st, size = (start_of[i], len(data))
        else:
            st, size = 0, 0
        e += struct.pack("<IQ", st, size)
        dirents += e.ljust(128, b"\0")
    dir_start = add(bytes(dirents))
    # FAT sectors
    nfat = 1
    while nfat * (SS // 4) < len(sectors) + nfat:
        nfat += 1
    fat_start = len(sectors)
    fat += [FATSEC] * nfat
    fat += [FREE] * (nfat * (SS // 4) - len(fat))
    body = b"".join(sectors) + struct.pack(f"<{len(fat)}I", *fat)
    difat = [fat_start + i for i in range(nfat)] + [FREE] * (109 - nfat)
    header = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
        + struct.pack("<IIIIIIIII", 0, nfat, dir_start, 0, CUTOFF, minifat_start, -(-len(mini_fat) // (SS // 4)), ENDC, 0)
        + struct.pack("<109I", *difat)
    )
    return header.ljust(SS, b"\0") + body


def hwp_para(text: str) -> bytes:
    """PARA_TEXT payload: text plus a tab control (8 WCHARs) and a paragraph end."""
    raw = b""
    for ch in text:
        if ch == "\t":
            raw += struct.pack("<H", 9) + b"\0" * 12 + struct.pack("<H", 9)
        else:
            raw += ch.encode("utf-16-le")
    raw += struct.pack("<H", 13)
    return raw


def record(tag: int, payload: bytes) -> bytes:
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | 0xFFF << 20, len(payload)) + payload
    return struct.pack("<I", tag | len(payload) << 20) + payload


def make_hwp(paras: list[str], compressed: bool = True, flags_extra: int = 0, big_pad: int = 0) -> bytes:
    body = b"".join(record(66, b"\0" * 22) + record(67, hwp_para(p)) for p in paras)
    body += record(72, b"x" * big_pad) if big_pad else b""
    if compressed:
        c = zlib.compressobj(wbits=-15)
        body = c.compress(body) + c.flush()
    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, int(compressed) | flags_extra)
    return build_ole({
        "FileHeader": header.ljust(256, b"\0"),
        "BodyText/Section0": body,
        "PrvText": "preview only".encode("utf-16-le"),
    })


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_text_encodings(self):
        s = "안녕하세요 hello"
        for name, data, label in (
            ("u8.txt", s.encode("utf-8"), "UTF-8"),
            ("u8bom.txt", b"\xef\xbb\xbf" + s.encode("utf-8"), "UTF-8"),
            ("u16.txt", b"\xff\xfe" + s.encode("utf-16-le"), "UTF-16LE"),
            ("u16nobom.txt", s.encode("utf-16-le"), "UTF-16LE"),
            ("ansi.txt", s.encode("cp949"), "ANSI"),
        ):
            p = preview.load(self.write(name, data))
            self.assertEqual(p.text, s, name)
            self.assertIn(label, p.kind, name)

    def test_binary_and_missing(self):
        self.assertEqual(preview.load(self.write("b.bin", bytes(range(256)) * 20)).error, "binary")
        self.assertTrue(preview.load(os.path.join(self.dir, "nope.txt")).error)
        self.assertEqual(preview.load(self.dir).error, "folder")

    def test_crlf_normalised_and_truncation(self):
        self.assertEqual(preview.load(self.write("a.txt", b"a\r\nb\rc")).text, "a\nb\nc")
        big = self.write("big.txt", b"x" * (preview.MAX_TEXT_BYTES + 100))
        p = preview.load(big)
        self.assertTrue(p.truncated)
        self.assertEqual(len(p.text), preview.MAX_CHARS)

    def test_docx_xlsx_pptx_hwpx_odt(self):
        def z(name, files):
            path = os.path.join(self.dir, name)
            with zipfile.ZipFile(path, "w") as zf:
                for k, v in files.items():
                    zf.writestr(k, v)
            return path

        w = '<w:document><w:body><w:p><w:r><w:t>첫째 줄</w:t></w:r></w:p><w:p><w:r><w:t>A &amp; B</w:t></w:r><w:tab/><w:r><w:t>끝</w:t></w:r></w:p></w:body></w:document>'
        self.assertEqual(preview.load(z("a.docx", {"word/document.xml": w})).text.strip(), "첫째 줄\nA & B\t끝")
        x = '<sst><si><t>alpha</t></si><si><r><t>be</t></r><r><t>ta</t></r></si></sst>'
        self.assertEqual(preview.load(z("a.xlsx", {"xl/sharedStrings.xml": x})).text, "alpha\nbeta")
        ppt = {f"ppt/slides/slide{n}.xml": f"<p:sld><a:p><a:t>s{n}</a:t></a:p></p:sld>" for n in (10, 2, 1)}
        t = preview.load(z("a.pptx", ppt)).text
        self.assertLess(t.index("s1\n"), t.index("s2"))
        self.assertLess(t.index("s2"), t.index("s10"))
        h = '<hs:sec><hp:p><hp:run><hp:t>한글 문서</hp:t></hp:run></hp:p><hp:p><hp:run><hp:t>둘째</hp:t></hp:run></hp:p></hs:sec>'
        self.assertEqual(preview.load(z("a.hwpx", {"Contents/section0.xml": h})).text.strip(), "한글 문서\n둘째")
        o = '<office:text><text:p>one</text:p><text:h>two</text:h></office:text>'
        self.assertEqual(preview.load(z("a.odt", {"content.xml": o})).text.strip(), "one\ntwo")

    def test_broken_zip_falls_back(self):
        p = preview.load(self.write("bad.docx", b"PK\x03\x04 not really"))
        self.assertFalse(p.error and p.error != "binary")

    def test_hwp5(self):
        for compressed in (True, False):
            for pad in (0, 20000):  # 20000 forces the extended record size and regular (non-mini) stream
                path = self.write("t.hwp", make_hwp(["대한민국 헌법", "제1조\t민주공화국", ""], compressed, big_pad=pad))
                p = preview.load(path)
                self.assertEqual(p.error, "", (compressed, pad))
                self.assertEqual(p.kind, "HWP")
                self.assertEqual(p.text, "대한민국 헌법\n제1조\t민주공화국\n\n")

    def test_hwp5_encrypted_and_garbage(self):
        self.assertEqual(preview.load(self.write("e.hwp", make_hwp(["x"], flags_extra=2))).error, "encrypted")
        self.assertEqual(preview.load(self.write("g.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 600)).error, "unreadable")
        self.assertEqual(preview.load(self.write("g2.hwp", b"short")).error, "unreadable")

    def test_ole_reader_matches_olefile(self):
        try:
            import olefile
        except ImportError:
            self.skipTest("olefile not installed")
        data = make_hwp(["abc"], big_pad=20000)
        ole = olefile.OleFileIO(data)
        self.assertTrue(ole.exists("BodyText/Section0"))
        self.assertEqual(ole.openstream("PrvText").read().decode("utf-16-le"), "preview only")

    def test_content_search_reads_hwp_and_docx(self):
        from filefinder import query
        from filefinder.index import Index
        from filefinder.scanner import scan_folder

        self.write("a.hwp", make_hwp(["제1조 계약의 목적", "끝"]))
        self.write("b.hwp", make_hwp(["다른 내용"]))
        path = os.path.join(self.dir, "c.docx")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("word/document.xml", "<w:p><w:t>계약서 초안</w:t></w:p>")
        self.write("d.txt", "계약 plain".encode("utf-8"))
        idx = Index()
        scan_folder(idx, os.path.realpath(self.dir))

        def names(q):
            return sorted(idx.names[i] for i in query.search(idx, q))

        self.assertEqual(names("content:계약"), ["a.hwp", "c.docx", "d.txt"])
        self.assertEqual(names("content:계약 ext:hwp"), ["a.hwp"])
        self.assertEqual(names("regex: content:다."), ["b.hwp"])
        self.assertEqual(names("content:없는말"), [])

    def test_search_terms(self):
        self.assertEqual(preview.search_terms('content:foo ext:txt content:"hello world" a|content:<x y>'), ["foo", "hello world", "x y"])
        self.assertEqual(preview.search_terms("report"), [])


if __name__ == "__main__":
    unittest.main()
