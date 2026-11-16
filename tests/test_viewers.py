import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fixtures

from mfinder import preview, query, viewers
from mfinder.index import Index
from mfinder.scanner import scan_folder
from mfinder.viewers import docx, html, hwp, hwpx, markdown, rhwp, xlsx
from mfinder.viewers import model as M


def paras(doc):
    return [b for b in doc.blocks if isinstance(b, M.Para)]


def tables(doc):
    return [b for b in doc.blocks if isinstance(b, M.Table)]


def images(doc):
    return [r for p in paras(doc) for r in p.runs if isinstance(r, M.Image)]


rhwp.configure(enabled=False)  # the built-in readers are under test; rhwp has its own tests


class Samples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = fixtures.make_all(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def path(self, name):
        return self.files[name]


class DocxTests(Samples):
    def test_styles_numbering_table_image(self):
        d = docx.read(self.path("sample.docx"))
        ps = paras(d)
        head = ps[0].runs[0]
        self.assertEqual((head.text, head.bold, head.size, head.color), ("분기 보고서", True, 20.0, "#1f3864"))
        self.assertEqual(ps[1].align, "center")
        runs = ps[1].runs
        self.assertTrue(runs[0].bold and runs[0].italic)
        self.assertTrue(runs[1].underline and runs[1].color == "#c00000")
        self.assertEqual(runs[2].bg, "#ffff00")
        self.assertEqual([p.text() for p in ps[2:5]], ["1. 첫째 항목", "2. 둘째 항목", "• 글머리 기호"])
        self.assertGreater(ps[2].left, 0)
        self.assertLess(ps[2].first, 0)  # hanging indent
        t = tables(d)[0]
        self.assertTrue(t.border)
        top = t.rows[0]
        self.assertEqual((top[0].colspan, top[0].bg), (2, "#d9e2f3"))
        self.assertEqual(top[1].rowspan, 2)  # vertical merge folded into rowspan
        self.assertEqual(len(t.rows[1]), 2)  # the continuation cell is not emitted
        img = images(d)[0]
        self.assertEqual((img.width, img.height), (90.0, 54.0))  # EMU -> pt


class HwpxTests(Samples):
    def test_header_styles_table_image(self):
        d = hwpx.read(self.path("sample.hwpx"))
        ps = paras(d)
        r = ps[0].runs[0]
        self.assertEqual((r.text, r.bold, r.underline, r.size, r.color, r.font), ("사업 계획서", True, True, 16.0, "#0000ff", "맑은 고딕"))
        self.assertEqual(ps[0].align, "center")
        t = tables(d)[0]
        self.assertTrue(t.border)
        self.assertEqual(t.col_widths, [90.0, 180.0])
        self.assertEqual(t.rows[0][0].bg, "#fff2cc")
        self.assertEqual(len(images(d)), 1)

    def test_strikeout_shapes(self):
        import zipfile
        header = '''<hh:head xmlns:hh="h"><hh:refList><hh:charProperties>
<hh:charPr id="0" height="1000"><hh:strikeout shape="3D" color="#000000"/></hh:charPr>
<hh:charPr id="1" height="1000"><hh:strikeout type="NONE" shape="SOLID" color="#000000"/></hh:charPr>
<hh:charPr id="2" height="1000"><hh:strikeout shape="SOLID" color="#000000"/></hh:charPr>
<hh:charPr id="3" height="1000"><hh:strikeout shape="DASH" color="#000000"/></hh:charPr>
</hh:charProperties></hh:refList></hh:head>'''
        sec = '''<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p>
<hp:run charPrIDRef="0"><hp:t>plain3d</hp:t></hp:run>
<hp:run charPrIDRef="1"><hp:t>noneType</hp:t></hp:run>
<hp:run charPrIDRef="2"><hp:t>solid</hp:t></hp:run>
<hp:run charPrIDRef="3"><hp:t>dash</hp:t></hp:run>
</hp:p></hs:sec>'''
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "strike.hwpx")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("Contents/header.xml", header)
                z.writestr("Contents/section0.xml", sec)
            runs = paras(hwpx.read(p))[0].runs
            self.assertEqual([(r.text, r.strike) for r in runs],
                             [("plain3d", False), ("noneType", False), ("solid", True), ("dash", True)])


class HwpTests(Samples):
    def test_docinfo_styles_table_picture(self):
        d = hwp.read(self.path("sample.hwp"))
        ps = paras(d)
        title = ps[0]
        self.assertEqual(title.align, "center")
        self.assertEqual((title.runs[0].bold, title.runs[0].size, title.runs[0].color), (True, 20.0, "#ff0000"))
        mixed = ps[1].runs
        self.assertEqual([r.text for r in mixed], ["일반 본문과 빨간 ", "글자가 ", "섞인 문단입니다."])
        self.assertEqual(mixed[1].color, "#c02020")
        t = tables(d)[0]
        self.assertEqual([[c.blocks[0].text() for c in r] for r in t.rows], [["항목", "내용 needle"], ["예산", "1,000,000원"]])
        self.assertEqual(t.rows[0][0].bg, "#ffe5cc")
        self.assertTrue(t.border)
        img = images(d)[0]
        self.assertEqual((img.width, img.height), (60.0, 36.0))
        self.assertEqual(img.data[:4], b"\x89PNG")  # decompressed BinData

    def test_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.hwp")
            with open(p, "wb") as f:
                f.write(b"not an ole file")
            self.assertEqual(viewers.load(p).error, "unreadable")
            enc = os.path.join(d, "e.hwp")
            from test_preview import make_hwp as make_enc
            with open(enc, "wb") as f:
                f.write(make_enc(["x"], flags_extra=2))
            self.assertEqual(viewers.load(enc).error, "encrypted")


class MarkdownTests(Samples):
    def test_blocks_and_inline(self):
        d = markdown.read(self.path("sample.md"))
        ps = paras(d)
        h1 = ps[0].runs[0]
        self.assertTrue(h1.bold)
        self.assertGreater(h1.size, 15)
        body = next(p for p in ps if "일반 문단" in p.text())
        by_text = {r.text: r for r in body.runs if isinstance(r, M.Run)}
        self.assertTrue(by_text["굵게"].bold)
        self.assertTrue(by_text["기울임"].italic)
        self.assertTrue(by_text["코드"].mono)
        self.assertTrue(by_text["취소선"].strike)
        self.assertEqual(by_text["링크"].link, "https://example.com")
        texts = [p.text() for p in ps]
        self.assertIn("☑ 완료된 항목", texts)
        self.assertIn("☐ 남은 항목", texts)
        self.assertTrue(any(t.startswith("1.") for t in texts))
        t = tables(d)[0]
        self.assertEqual([c.blocks[0].align for c in t.rows[0]], ["left", "right"])
        self.assertTrue(t.rows[0][0].blocks[0].runs[0].bold)
        code = [p for p in ps if p.bg == markdown.CODE_BG and p.runs and p.runs[0].mono]
        self.assertEqual(len(code), 2)
        self.assertTrue(any(p.rule for p in ps))

    def test_local_image(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.png"), "wb") as f:
                f.write(fixtures.png())
            with open(os.path.join(d, "a.md"), "w", encoding="utf-8") as f:
                f.write("![로고](a.png)\n\n![없음](missing.png) ![원격](https://x/y.png)\n")
            doc = markdown.read(os.path.join(d, "a.md"))
            self.assertEqual(len(images(doc)), 1)
            self.assertIn("[없음]", "".join(p.text() for p in paras(doc)))


class XlsxTests(Samples):
    def test_cells_styles_formats(self):
        wb = xlsx.read(self.path("sample.xlsx"))
        self.assertEqual([s.name for s in wb.sheets], ["매출", "메모"])
        sh = wb.sheets[0]
        head = sh.cells[(0, 0)]
        self.assertEqual((head.text, head.bold, head.bg, head.color, head.align), ("월", True, "#1f4e78", "#ffffff", "center"))
        self.assertEqual(sh.cells[(1, 1)].text, "246,912원")  # custom number format with a quoted suffix
        self.assertEqual(sh.cells[(1, 2)].text, "2.00%")
        self.assertRegex(sh.cells[(1, 0)].text, r"^\d{4}-\d{2}-\d{2}$")  # built-in date format 14
        self.assertEqual(sh.cells[(1, 1)].align, "right")  # numbers right-align by default
        self.assertTrue(sh.cells[(1, 1)].border)
        self.assertEqual(sh.merges, [(0, 4, 1, 6)])
        self.assertAlmostEqual(sh.col_widths[0], 14 * 7 + 5)
        self.assertEqual(wb.sheets[1].cells[(0, 0)].text, "두 번째 시트")

    def test_number_formats(self):
        f = xlsx.format_number
        self.assertEqual(f(1234.5, "#,##0.00"), "1,234.50")
        self.assertEqual(f(0.256, "0.0%"), "25.6%")
        self.assertEqual(f(-5, "#,##0;(#,##0)"), "(5)")
        self.assertEqual(f(12, "General"), "12")
        self.assertEqual(f(45000, "yyyy-mm-dd"), "2023-03-15")
        self.assertEqual(f(45000.75, "yyyy-mm-dd h:mm"), "2023-03-15 18:00")
        self.assertEqual(f(1234567, '"₩"#,##0'), "₩1,234,567")
        self.assertEqual(f(5, '0" 개"'), "5 개")


class HtmlTests(Samples):
    def test_html_page_and_code_modes(self):
        d = html.read(self.path("sample.html"))
        ps = paras(d)
        self.assertEqual(ps[0].runs[0].text, "HTML 보고서")
        self.assertTrue(ps[0].runs[0].bold)
        self.assertEqual(len(tables(d)), 1)
        page_ld = viewers.load(self.path("sample.html"), html_mode="page")
        code_ld = viewers.load(self.path("sample.html"), html_mode="code")
        self.assertEqual(page_ld.kind, "rich")
        self.assertEqual(code_ld.kind, "text")
        self.assertIn("<h1>HTML 보고서</h1>", code_ld.model[0])


class LoaderTests(Samples):
    def test_kinds(self):
        kinds = {n: viewers.load(p) for n, p in self.files.items()}
        self.assertEqual({n: k.kind for n, k in kinds.items()},
                         {"sample.docx": "rich", "sample.hwpx": "rich", "sample.hwp": "rich", "sample.md": "rich",
                          "sample.html": "rich", "sample.xlsx": "sheet", "sample.pdf": "pdf", "sample.txt": "text"})
        self.assertEqual(kinds["sample.txt"].model[0].splitlines()[1], "needle in a haystack")

    def test_broken_files_fall_back_to_text(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("bad.docx", "bad.xlsx", "bad.hwpx"):
                p = os.path.join(d, name)
                with open(p, "wb") as f:
                    f.write(b"PK\x03\x04 truncated")
                ld = viewers.load(p)
                self.assertTrue(ld.kind in ("text", "") and (ld.error or "text only" in ld.label), (name, ld))
            self.assertEqual(viewers.load(d).error, "folder")
            self.assertTrue(viewers.load(os.path.join(d, "missing.docx")).error)

    def test_unlisted_types_still_show_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "n.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write("log line")
            self.assertEqual(viewers.load(p).model[0], "log line")


class ImageTests(unittest.TestCase):
    def test_png_and_broken(self):
        from mfinder.viewers import image

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            with open(p, "wb") as f:
                f.write(fixtures.png(64, 40))
            ld = viewers.load(p)
            self.assertEqual((ld.kind, ld.label, ld.error), ("image", "PNG", ""))
            r = ld.model
            self.assertEqual((r.width, r.height), (64, 40))
            if not r.native:
                self.assertEqual(r.ppm(32, 20), b"P6 32 20 255\n" + r.ppm(32, 20)[13:])
                self.assertEqual(len(r.ppm(32, 20)), 13 + 32 * 20 * 3)
            bad = os.path.join(d, "bad.jpg")
            with open(bad, "wb") as f:
                f.write(b"not a picture")
            ld = viewers.load(bad)
            self.assertEqual((ld.kind, ld.error, ld.label), ("", "unreadable", "JPEG"))
            self.assertIn(".png", image.EXTS)


class RhwpTests(Samples):
    def tearDown(self):
        rhwp.configure(enabled=False)

    def test_page_count_from_manifest(self):
        self.assertEqual(rhwp._page_count('noise\n{"backend":"svg","pageCount":35,"renderedCount":1}\n'), 35)
        self.assertEqual(rhwp._page_count("no json here"), 0)

    def test_unavailable_falls_back_to_builtin_reader(self):
        rhwp.configure(path=os.path.join(self.tmp.name, "missing", "rhwp.exe"), enabled=True)
        old = os.environ.pop("RHWP_EXE", None)
        try:
            if not rhwp.find_exe():
                self.assertIsNone(rhwp.open_doc(self.path("sample.hwp")))
        finally:
            if old:
                os.environ["RHWP_EXE"] = old
        rhwp.configure(enabled=False)
        self.assertIsNone(rhwp.open_doc(self.path("sample.hwp")))
        self.assertEqual(viewers.load(self.path("sample.hwp")).kind, "rich")

    def test_job_captures_output_and_cancels(self):
        job = rhwp.Job()
        code, out = job.run([sys.executable, "-c", "print('hello')"], dict(os.environ), self.tmp.name, 30)
        self.assertEqual((code, out.strip()), (0, "hello"))
        code, _ = job.run([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), self.tmp.name, 0.5)
        self.assertEqual(code, -2)  # timeout
        job.cancel()
        code, _ = job.run([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), self.tmp.name, 30)
        self.assertEqual(code, -1)


class ContentSearchTests(Samples):
    def test_content_search_covers_every_format(self):
        idx = Index()
        scan_folder(idx, os.path.realpath(self.tmp.name))
        found = sorted(idx.names[i] for i in query.search(idx, "content:needle"))
        want = ["sample.docx", "sample.html", "sample.hwp", "sample.hwpx", "sample.md", "sample.txt", "sample.xlsx"]
        if viewers.HAVE_PDF:
            want.append("sample.pdf")
        self.assertEqual(found, sorted(want))

    @unittest.skipUnless(viewers.HAVE_PDF, "PyMuPDF not installed")
    def test_pdf_text(self):
        p = preview.load(self.path("sample.pdf"))
        self.assertEqual(p.kind, "PDF")
        self.assertIn("Page 2 heading", p.text)


if __name__ == "__main__":
    unittest.main()
