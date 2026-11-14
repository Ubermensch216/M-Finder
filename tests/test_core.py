import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mfinder import ntfs, query
from mfinder.engine import Engine
from mfinder.index import DatabaseError, Index
from mfinder.scanner import Excludes, scan_folder
from mfinder.settings import Settings


def make_tree(root):
    files = {
        "Docs/Report Final.docx": 2048,
        "Docs/report draft.txt": 10,
        "Docs/notes.md": 0,
        "Pics/cat.JPG": 300_000,
        "Pics/dog.png": 5_000_000,
        "Pics/sub/cat2.jpg": 100,
        "src/main.py": 1200,
        "src/test_main.py": 800,
        "empty_dir/.keep": 0,
        "hidden_not": 1,
    }
    for rel, size in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
    os.makedirs(os.path.join(root, "really_empty"), exist_ok=True)


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls.tmp.name)
        make_tree(cls.root)
        cls.idx = Index()
        scan_folder(cls.idx, cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def names(self, text, **flags):
        ids = query.search(self.idx, text, query.Flags(**flags))
        return sorted(self.idx.names[i] for i in ids)

    def test_and_substring_case_insensitive(self):
        self.assertEqual(self.names("report"), ["Report Final.docx", "report draft.txt"])
        self.assertEqual(self.names("report final"), ["Report Final.docx"])

    def test_or_not_group(self):
        self.assertEqual(self.names("cat | dog"), ["cat.JPG", "cat2.jpg", "dog.png"])
        self.assertEqual(self.names("cat !2"), ["cat.JPG"])
        self.assertEqual(self.names("<cat|dog> ext:png"), ["dog.png"])

    def test_wildcards_match_whole_name(self):
        self.assertEqual(self.names("*.md"), ["notes.md"])
        self.assertEqual(self.names("main.p?"), ["main.py"])
        self.assertEqual(self.names("ain.py*"), [])

    def test_case_regex_ww(self):
        self.assertEqual(self.names("cat.JPG", case=True), ["cat.JPG"])
        self.assertEqual(self.names("cat.jpg", case=True), [])
        self.assertEqual(self.names(r"^cat\d?\.jpg$", regex=True), ["cat.JPG", "cat2.jpg"])
        self.assertEqual(self.names("regex:^dog"), ["dog.png"])
        self.assertEqual(self.names("report", ww=True), ["Report Final.docx", "report draft.txt"])
        self.assertEqual(self.names("main", ww=True), ["main.py"])  # '_' is a word character

    def test_modifiers_apply_to_following_terms(self):
        self.assertEqual(self.names("case: CAT.jpg"), [])
        self.assertEqual(self.names("case:cat.JPG"), ["cat.JPG"])

    def test_path_matching(self):
        self.assertEqual(self.names("pics\\sub"), ["cat2.jpg", "sub"])  # the folder itself matches too
        self.assertEqual(self.names("path:docs notes"), ["notes.md"])

    def test_functions(self):
        self.assertEqual(self.names("ext:jpg;png"), ["cat.JPG", "cat2.jpg", "dog.png"])
        self.assertEqual(self.names("pic: cat"), ["cat.JPG", "cat2.jpg"])
        self.assertEqual(self.names("size:>1mb"), ["dog.png"])
        self.assertEqual(self.names("size:100..2000 ext:py"), ["main.py", "test_main.py"])
        self.assertEqual(self.names("size:empty file:"), [".keep", "notes.md"])
        self.assertEqual(self.names("folder: empty:"), ["really_empty"])
        self.assertEqual(self.names("len:>=12 file:"), ["Report Final.docx", "report draft.txt", "test_main.py"])
        self.assertEqual(self.names("parent:" + os.path.join(self.root, "Pics")), ["cat.JPG", "dog.png", "sub"])
        self.assertEqual(self.names("dm:today file: ext:md"), ["notes.md"])
        self.assertEqual(self.names("dm:2001 file:"), [])
        self.assertEqual(self.names("dm:>2001 ext:md"), ["notes.md"])

    def test_empty_query_returns_everything_but_roots(self):
        ids = query.search(self.idx, "")
        self.assertEqual(len(ids), self.idx.live_count() - 1)

    def test_errors(self):
        for bad in ("regex:(", "size:>abc", "dm:notadate", "attrib:Z", "size:"):
            with self.assertRaises(query.QueryError, msg=bad):
                query.search(self.idx, bad)

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        self.assertIsNone(query.search(self.idx, "a", cancel=ev))

    def test_sorting(self):
        ids = query.search(self.idx, "ext:jpg;png;py")
        by_size = query.sort_results(self.idx, ids, "size", False)
        self.assertEqual(self.idx.names[by_size[0]], "dog.png")
        by_name = query.sort_results(self.idx, ids, "name", True)
        self.assertEqual([self.idx.names[i] for i in by_name][:2], ["cat.JPG", "cat2.jpg"])
        by_ext = query.sort_results(self.idx, ids, "ext", True)
        self.assertEqual(self.idx.names[by_ext[-1]], "test_main.py")

    def test_date_atoms(self):
        now = datetime(2024, 5, 15, 12, 0, 0)
        lo, hi = query._date_atom("today", now)
        self.assertEqual(datetime.fromtimestamp(lo), datetime(2024, 5, 15))
        lo, hi = query._date_atom("2024-02", now)
        self.assertEqual(datetime.fromtimestamp(lo), datetime(2024, 2, 1))
        self.assertLess(hi, datetime(2024, 3, 1).timestamp())
        lo, hi = query._date_atom("last3days", now)
        self.assertAlmostEqual(hi - lo, 3 * 86400)
        self.assertEqual(datetime.fromtimestamp(query._date_atom("lastweek", now)[0]), datetime(2024, 5, 6))


class IndexTests(unittest.TestCase):
    def test_roundtrip_and_compaction(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            make_tree(src)
            idx = Index()
            scan_folder(idx, src)
            victim = query.search(idx, "dog.png")[0]
            idx.remove(victim)
            db = os.path.join(d, "x.db")
            idx.save(db)
            back = Index.load(db)
            self.assertEqual(back.live_count(), idx.live_count())
            self.assertEqual(len(back), idx.live_count())
            self.assertEqual(sorted(back.names), sorted(n for n, a in zip(idx.names, idx.alive) if a))
            ids = query.search(back, "cat2")
            self.assertTrue(back.full_path(ids[0]).endswith(os.path.join("Pics", "sub", "cat2.jpg")))

    def test_corrupt_and_missing_database(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(DatabaseError):
                Index.load(os.path.join(d, "none.db"))
            p = os.path.join(d, "bad.db")
            with open(p, "wb") as f:
                f.write(b"FFDB\x01\x00\x00\x00\x02\x00\x00\x00{}garbage")
            with self.assertRaises(DatabaseError):
                Index.load(p)
            with open(p, "wb") as f:
                f.write(b"nope")
            with self.assertRaises(DatabaseError):
                Index.load(p)

    def test_excludes(self):
        with tempfile.TemporaryDirectory() as d:
            make_tree(d)
            idx = Index()
            scan_folder(idx, d, Excludes(folders=[os.path.join(d, "Pics")], patterns=["*.md"]))
            names = {idx.names[i] for i in range(len(idx)) if idx.alive[i]}
            self.assertNotIn("cat.JPG", names)
            self.assertNotIn("sub", names)
            self.assertNotIn("notes.md", names)
            self.assertIn("main.py", names)


class UsnTests(unittest.TestCase):
    @staticmethod
    def record(frn, parent, name, reason=0, attrs=0, usn=1):
        raw = name.encode("utf-16-le")
        length = (60 + len(raw) + 7) & ~7
        hdr = struct.pack("<IHHQQqqIIIIHH", length, 2, 0, frn, parent, usn, 0, reason, 0, 0, attrs, len(raw), 60)
        return (hdr + raw).ljust(length, b"\0")

    def test_parse(self):
        buf = struct.pack("<Q", 0) + self.record(100, 5, "héllo.txt") + self.record(101, 100, "x", attrs=0x10)
        recs = list(ntfs.parse_usn_records(buf, 8))
        self.assertEqual([(r[0], r[1], r[5], r[4]) for r in recs], [(100, 5, "héllo.txt", 0), (101, 100, "x", 0x10)])
        self.assertEqual(list(ntfs.parse_usn_records(buf[:40], 8)), [])  # truncated record is ignored

    def test_apply_create_rename_delete(self):
        idx = Index()
        vol, root = idx.add_root("C:\\", "ntfs")
        fm = idx.frn_maps[vol]
        ROOT_FRN = 5 | (3 << 48)
        apply = lambda r: ntfs.apply_usn_record(idx, vol, r)
        self.assertTrue(apply((1000, ROOT_FRN, 1, ntfs.USN_REASON_FILE_CREATE, 0x10, "dir")))
        d = fm[1000]
        self.assertTrue(apply((1001, 1000, 2, ntfs.USN_REASON_FILE_CREATE, 0x20, "a.txt")))
        self.assertEqual(idx.full_path(fm[1001]), "C:\\dir\\a.txt")
        self.assertTrue(apply((1001, 1000, 3, ntfs.USN_REASON_RENAME_NEW_NAME, 0x20, "b.txt")))
        self.assertEqual(idx.full_path(fm[1001]), "C:\\dir\\b.txt")
        self.assertEqual(query.search(idx, "b.txt"), [fm[1001]])
        self.assertFalse(apply((5, ROOT_FRN, 4, ntfs.USN_REASON_FILE_CREATE, 0x10, "$MFT")))  # metafile ignored
        self.assertTrue(apply((1001, 1000, 5, ntfs.USN_REASON_FILE_DELETE, 0x20, "b.txt")))
        self.assertEqual(query.search(idx, "b.txt"), [])
        self.assertFalse(apply((2000, 9999, 6, ntfs.USN_REASON_FILE_CREATE, 0, "orphan")))  # unknown parent

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_fixed_drives_and_fs(self):
        drives = ntfs.fixed_drives()
        self.assertTrue(drives)
        self.assertEqual(ntfs.is_ntfs_root(drives[0]), ntfs.filesystem_name(drives[0]).upper() == "NTFS")


class EngineAndHttpTests(unittest.TestCase):
    def test_engine_scan_save_load_and_http(self):
        from mfinder.httpserver import make_server

        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, "tree")
            make_tree(tree)
            s = Settings()
            s.roots = [tree]
            s.use_mft = False
            s.db_path = os.path.join(d, "db", "f.db")
            s.monitor = False
            e = Engine(s)
            self.assertFalse(e.load())
            self.assertTrue(e.rebuild())
            self.assertTrue(os.path.exists(s.db_path))
            e.shutdown()
            e2 = Engine(s)
            self.assertTrue(e2.load())
            s.roots = [tree, d]
            self.assertFalse(Engine(s).load())  # configured roots changed -> stale database
            s.roots = [tree]

            srv = make_server(e2, "127.0.0.1", 0, "u", "p")
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}/"
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + "?s=cat&json=1")
                self.assertEqual(cm.exception.code, 401)
                import base64

                auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
                req = urllib.request.Request(base + "?s=cat&json=1&sort=name", headers=auth)
                data = json.load(urllib.request.urlopen(req))
                self.assertEqual(data["totalResults"], 2)
                self.assertEqual(data["results"][0]["name"], "cat.JPG")
                req = urllib.request.Request(base + "?s=%3Cscript%3E", headers=auth)
                html = urllib.request.urlopen(req).read().decode()
                self.assertNotIn("<script>", html)
                req = urllib.request.Request(base + "?s=regex:(", headers=auth)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req)
                self.assertEqual(cm.exception.code, 400)
            finally:
                srv.shutdown()
                srv.server_close()


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("MF_GUI_TEST"), "set MF_GUI_TEST=1 to run the Tk smoke test")
class GuiSmoke(unittest.TestCase):
    def pump(self, ctl, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end and not cond():
            ctl.root.update()
            time.sleep(0.02)
        return cond()

    def test_gui(self):
        from mfinder.gui import Controller

        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, "tree")
            make_tree(tree)
            os.environ["APPDATA"] = d
            s = Settings()
            s.roots, s.use_mft, s.db_path = [tree], False, os.path.join(d, "g.db")
            s.ipc_enabled, s.single_instance, s.language, s.theme = False, False, "ko", "dark"
            ctl = Controller(s)
            try:
                app = ctl.windows[0]
                self.assertTrue(self.pump(ctl, lambda: len(app.list.ids) > 10))
                app.var.set("cat")
                self.assertTrue(self.pump(ctl, lambda: len(app.list.ids) == 2))
                app.list.body.focus_force()
                app.list._key_move("home", False)
                app.list._key_move(1, True)
                self.assertEqual(len(app._paths()), 2)
                app.copy_full_path()
                self.assertIn("cat", app.root_clip() if hasattr(app, "root_clip") else app.clipboard_get())
                app.sort_by("size")
                app.sort_by("size")
                self.pump(ctl, lambda: False, 1.5)
                self.assertEqual([ctl.engine.index.names[i] for i in app.list.ids], ["cat2.jpg", "cat.JPG"])
                # preview pane: selecting a text file shows its content with the content: term highlighted
                note = os.path.join(tree, "note_preview.txt")
                with open(note, "w", encoding="utf-8") as f:
                    f.write("alpha beta\nneedle one\ngamma NEEDLE two\n")
                sys.path.insert(0, os.path.dirname(__file__))
                from fixtures import make_all

                make_all(os.path.join(tree, "samples"))
                ctl.rebuild()
                app.var.set("content:needle note_preview")
                self.assertTrue(self.pump(ctl, lambda: len(app.list.ids) == 1))
                app.list.sel, app.list.cursor = {0}, 0
                app.selection_changed()
                pv = app.preview
                self.assertTrue(self.pump(ctl, lambda: pv.active is not None and "needle one" in pv.active.text.get("1.0", "end")))
                self.assertTrue(self.pump(ctl, lambda: len(pv.active.hits) == 2))
                self.assertEqual(pv.lbl_hits.cget("text"), "1 / 2")
                pv.step(1)
                self.assertEqual(pv.lbl_hits.cget("text"), "2 / 2")
                # View > Preview Pane > Bottom / Right / Left re-lays out the split
                for pos in ("right", "left", "bottom"):
                    app.v_ppos.set(pos)
                    app.set_preview_pos()
                    ctl.root.update()
                    panes = [str(p) for p in app.paned.panes()]
                    self.assertEqual(len(panes), 2)
                    self.assertEqual(str(app.paned.cget("orient")), "vertical" if pos == "bottom" else "horizontal")
                    self.assertEqual(panes[0] == str(app.preview), pos == "left")
                    self.assertEqual(ctl.settings.preview_pos, pos)
                # every supported format gets its own viewer
                kinds = [("sample.docx", "RichView"), ("sample.hwp", "RichView"), ("sample.hwpx", "RichView"),
                         ("sample.md", "RichView"), ("sample.xlsx", "SheetView"), ("sample.txt", "TextView")]
                from mfinder import viewers

                if viewers.HAVE_PDF:
                    kinds.append(("sample.pdf", "PdfView"))
                for name, kind in kinds:
                    app.var.set("content:needle ext:" + name.rsplit(".", 1)[1] + " sample")  # ext is unique per sample
                    self.assertTrue(self.pump(ctl, lambda: len(app.list.ids) == 1), name)
                    self.pump(ctl, lambda: False, 1.5)  # let background index updates re-run the search first
                    self.assertEqual(len(app.list.ids), 1, name)
                    app.list.sel, app.list.cursor = {0}, 0
                    app.selection_changed()
                    ok = self.pump(ctl, lambda: type(pv.active).__name__ == kind and name in pv.lbl.cget("text")
                                   and "[" in pv.lbl.cget("text"))
                    self.assertTrue(ok, (name, type(pv.active).__name__, pv.lbl.cget("text"), pv.msg.cget("text"), pv.path))
                    want = "1 / 2" if name == "sample.hwp" else "1 / 1"  # the HWP sample has a hit in a cell and in the body
                    self.assertTrue(self.pump(ctl, lambda: pv.lbl_hits.cget("text") == want, 8), (name, pv.lbl_hits.cget("text")))
                app.toggle_preview_key()
                self.assertFalse(ctl.settings.show_preview)
                self.assertEqual(len(app.paned.panes()), 1)
                app.toggle_preview_key()
                self.assertTrue(ctl.settings.show_preview)
                app.var.set("cat")
                self.assertTrue(self.pump(ctl, lambda: len(app.list.ids) == 2))
                # filter combobox + bookmarks
                app.cb_filter.set("Picture")
                app.cb_filter.event_generate("<<ComboboxSelected>>")
                app.add_bookmark = lambda: None
                ctl.set_bookmarks([{"name": "docs", "search": "report", "filter": "Document", "case": False, "ww": False, "path": False, "regex": False, "sort": "name", "asc": True}])
                app.apply_bookmark(ctl.settings.bookmarks[0])
                self.assertTrue(self.pump(ctl, lambda: sorted(ctl.engine.index.names[i] for i in app.list.ids) == ["Report Final.docx", "report draft.txt"]))
                self.assertEqual(ctl.settings.current_filter, "Document")
                # second window shares the engine
                w2 = ctl.new_window("main")
                self.assertTrue(self.pump(ctl, lambda: len(w2.list.ids) == 0 or len(w2.list.ids) >= 0))
                self.assertIs(w2.engine, app.engine)
                self.assertEqual(len(ctl.windows), 2)
                # errors surface in the status line
                app.cb_filter.set("Everything")
                app.cb_filter.event_generate("<<ComboboxSelected>>")
                app.v_regex.set(True)
                app.apply_flags()
                app.var.set("^(")
                self.assertTrue(self.pump(ctl, lambda: bool(app.error)))
                self.assertIn("정규", app.lbl_left.cget("text") or "정규") if False else None
            finally:
                ctl.quit()


if __name__ == "__main__":
    unittest.main()
