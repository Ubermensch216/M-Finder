import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_core import make_tree

from mfinder import filelist, i18n, ipc, mftparse, query, service, theme, tray, watcher
from mfinder.engine import Engine
from mfinder.index import FILE_ATTRIBUTE_DIRECTORY, Index
from mfinder.scanner import Excludes, scan_folder
from mfinder.settings import Settings, default_filters


def scanned(tmp):
    root = os.path.realpath(tmp)
    make_tree(root)
    idx = Index()
    scan_folder(idx, root)
    return root, idx


class DatesSizesContent(unittest.TestCase):
    def test_created_accessed_and_db_v2_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root, idx = scanned(d)
            now = time.time()
            self.assertEqual(len(query.search(idx, "dc:today file:")), len([i for i in range(len(idx)) if idx.alive[i] and not idx.is_dir(i) and idx.parent[i] >= 0]))
            self.assertTrue(query.search(idx, "da:today ext:py"))
            self.assertEqual(query.search(idx, "dc:2001"), [])
            db = os.path.join(d, "x.db")
            idx.save(db)
            back = Index.load(db)
            i = query.search(back, "main.py")[0]
            self.assertAlmostEqual(back.ctime[i], now, delta=120)
            self.assertAlmostEqual(back.atime[i], now, delta=120)

    def test_folder_sizes(self):
        with tempfile.TemporaryDirectory() as d:
            root, idx = scanned(d)
            self.assertTrue(idx.compute_folder_sizes())
            self.assertTrue(idx.folder_sizes_ready)
            pics = idx.resolve(os.path.join(root, "Pics"))
            self.assertEqual(idx.size[pics], 300_000 + 5_000_000 + 100)
            self.assertEqual(idx.size[idx.resolve(os.path.join(root, "really_empty"))], 0)
            names = lambda q: sorted(idx.names[i] for i in query.search(idx, q))
            self.assertIn("Pics", names("folder: size:>5mb"))
            self.assertNotIn("Docs", names("folder: size:>5mb"))
            db = os.path.join(d, "f.db")
            idx.save(db)
            self.assertTrue(Index.load(db).folder_sizes_ready)

    def test_content_search(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            for name, data in (("a.txt", b"hello World here"), ("u16.txt", "naïve ÉCOLE".encode("utf-16-le")),
                               ("big.bin", b"x" * 5000 + b"needle42"), ("none.txt", b"nothing")):
                with open(os.path.join(root, name), "wb") as f:
                    f.write(data)
            idx = Index()
            scan_folder(idx, root)
            names = lambda q, **kw: sorted(idx.names[i] for i in query.search(idx, q, **kw))
            self.assertEqual(names("content:world"), ["a.txt"])  # ASCII case-insensitive
            self.assertEqual(names("case: content:world"), [])
            self.assertEqual(names("content:naïve"), ["u16.txt"])  # UTF-16 content
            self.assertEqual(names("content:needle42 ext:bin"), ["big.bin"])
            self.assertEqual(names("regex: content:ne+dle\\d+"), ["big.bin"])
            self.assertEqual(names("content:needle42", content_max=100), [])  # over the size limit
            self.assertEqual(names("content:hello"), ["a.txt"])
            ev = threading.Event()
            ev.set()
            self.assertIsNone(query.search(idx, "content:zzz", cancel=ev))

    def test_filters_compose(self):
        with tempfile.TemporaryDirectory() as d:
            root, idx = scanned(d)
            ids = query.search(idx, "cat", filter_text="pic:")
            self.assertEqual(sorted(idx.names[i] for i in ids), ["cat.JPG", "cat2.jpg"])
            ids = query.search(idx, "", filter_text="folder:", filter_flags=query.Flags())
            self.assertTrue(ids and all(idx.is_dir(i) for i in ids))
            ids = query.search(idx, "dog", filter_text="pic:")
            self.assertEqual([idx.names[i] for i in ids], ["dog.png"])
            ids = query.search(idx, "dog", filter_text="doc:")
            self.assertEqual(ids, [])

    def test_engine_filters_and_defaults(self):
        s = Settings()
        names = [f["name"] for f in default_filters()]
        self.assertIn("Picture", names)
        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, "t")
            make_tree(tree)
            s.roots, s.use_mft, s.db_path, s.monitor = [tree], False, os.path.join(d, "e.db"), False
            e = Engine(s)
            e.rebuild()
            try:
                self.assertEqual(sorted(e.index.names[i] for i in e.search("cat", filter_name="Picture")), ["cat.JPG", "cat2.jpg"])
                self.assertEqual(e.search("cat", filter_name="Document"), [])
                s.current_filter = "Picture"
                self.assertEqual(len(e.search("cat")), 2)
                deadline = time.time() + 10
                while e.filling and time.time() < deadline:
                    time.sleep(0.05)
                time.sleep(0.2)
                self.assertTrue(e.index.folder_sizes_ready)
            finally:
                e.shutdown()


class MftParseTests(unittest.TestCase):
    @staticmethod
    def make_record(size=1234, nonresident=False, directory=False, attr_list=False, torn=False):
        rec = bytearray(1024)
        rec[0:4] = b"FILE"
        struct.pack_into("<HH", rec, 4, 0x30, 3)  # update sequence at 0x30, 3 entries (usn + 2 sectors)
        struct.pack_into("<H", rec, 0x14, 0x38)
        struct.pack_into("<H", rec, 0x16, 1 | (2 if directory else 0))
        pos = 0x38
        ft = lambda e: int(e * 10_000_000) + mftparse.EPOCH_DELTA
        struct.pack_into("<IIBBHHH", rec, pos, 0x10, 0x60, 0, 0, 0, 0, 0)
        struct.pack_into("<IH", rec, pos + 0x10, 0x48, 0x18)
        struct.pack_into("<QQQQ", rec, pos + 0x18, ft(1_700_000_000), ft(1_700_000_100), ft(1_700_000_200), ft(1_700_000_300))
        pos += 0x60
        if attr_list:
            struct.pack_into("<II", rec, pos, 0x20, 0x20)
            pos += 0x20
        if not directory:
            if nonresident:
                struct.pack_into("<IIB", rec, pos, 0x80, 0x48, 1)
                struct.pack_into("<Q", rec, pos + 0x30, size)
                pos += 0x48
            else:
                struct.pack_into("<IIBB", rec, pos, 0x80, 0x20, 0, 0)
                struct.pack_into("<I", rec, pos + 0x10, size)
                pos += 0x20
        struct.pack_into("<I", rec, pos, 0xFFFFFFFF)
        usn = b"\x07\x00"
        rec[0x30:0x32] = usn
        rec[0x32:0x34], rec[0x34:0x36] = rec[510:512], rec[1022:1024]  # save the real sector tails
        rec[510:512], rec[1022:1024] = usn, (b"\x00\x00" if torn else usn)
        return rec

    def test_resident_and_nonresident(self):
        for nonres in (False, True):
            r = self.make_record(size=4321, nonresident=nonres)
            self.assertTrue(mftparse.apply_fixups(r))
            size, c, m, a, is_dir = mftparse.parse_record(r)
            self.assertEqual((size, c, m, a, is_dir), (4321, 1_700_000_000.0, 1_700_000_100.0, 1_700_000_300.0, False))

    def test_directory_attrlist_and_torn(self):
        r = self.make_record(directory=True)
        mftparse.apply_fixups(r)
        self.assertEqual(mftparse.parse_record(r)[0], -1)
        r = self.make_record(attr_list=True)
        mftparse.apply_fixups(r)
        self.assertIsNone(mftparse.parse_record(r))
        self.assertFalse(mftparse.apply_fixups(self.make_record(torn=True)))

    def test_runlist(self):
        self.assertEqual(mftparse.parse_runs(bytes([0x21, 0x18, 0x34, 0x56, 0x21, 0x10, 0x00, 0x10, 0x00]), 0), [(0x5634, 0x18), (0x5634 + 0x1000, 0x10)])
        self.assertEqual(mftparse.parse_runs(bytes([0x11, 0x08, 0x10, 0x11, 0x04, 0xF0, 0x00]), 0), [(0x10, 8), (0x00, 4)])  # negative delta
        self.assertEqual(mftparse.parse_runs(bytes([0x11, 0x08, 0x10, 0x01, 0x04, 0x11, 0x02, 0x05, 0x00]), 0), [(0x10, 8), (0x15, 2)])  # sparse run skipped

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_fill_matches_stat_on_real_volume(self):
        from mfinder import ntfs
        from mfinder.engine import is_admin

        if not is_admin():
            self.skipTest("needs administrator")
        drive = os.path.splitdrive(os.path.realpath(__file__))[0] + "\\"
        if not ntfs.is_ntfs_root(drive):
            self.skipTest("not NTFS")
        # index only the test directory's ancestors cheaply: whole volume is fine but slow, so sample the repo
        idx = Index()
        ntfs.index_volume(idx, drive)
        mftparse.fill_metadata(idx, 0)
        here = idx.resolve(os.path.dirname(os.path.realpath(__file__)))
        for cid in idx.children_map()[here].values():
            st = os.stat(idx.full_path(cid))
            if not idx.is_dir(cid):
                self.assertEqual(idx.size[cid], st.st_size)
            self.assertAlmostEqual(idx.mtime[cid], st.st_mtime, delta=2)


class WatcherApplierTests(unittest.TestCase):
    def test_apply_events(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            os.makedirs(os.path.join(root, "a"))
            idx = Index()
            scan_folder(idx, root)
            ap = watcher.ChangeApplier(idx, 0)
            find = lambda q: sorted(idx.full_path(i)[len(root):] for i in query.search(idx, q))
            with open(os.path.join(root, "a", "new.txt"), "w") as f:
                f.write("x")
            self.assertTrue(ap.apply(watcher.ACTION_ADDED, "a\\new.txt"))
            self.assertEqual(find("new.txt"), ["\\a\\new.txt"])
            os.rename(os.path.join(root, "a", "new.txt"), os.path.join(root, "a", "moved.txt"))
            ap.apply(watcher.ACTION_RENAMED_OLD, "a\\new.txt")
            ap.apply(watcher.ACTION_RENAMED_NEW, "a\\moved.txt")
            self.assertEqual((find("new.txt"), find("moved.txt")), ([], ["\\a\\moved.txt"]))
            os.makedirs(os.path.join(root, "b", "c"))
            with open(os.path.join(root, "b", "c", "deep.dat"), "w") as f:
                f.write("y")
            self.assertTrue(ap.apply(watcher.ACTION_ADDED, "b"))  # a folder arriving with content
            self.assertEqual(find("deep.dat"), ["\\b\\c\\deep.dat"])
            self.assertTrue(ap.apply(watcher.ACTION_REMOVED, "b"))
            self.assertEqual(find("deep.dat"), [])
            self.assertEqual(find("c"), [])
            self.assertFalse(ap.apply(watcher.ACTION_REMOVED, "never\\existed"))
            data = struct.pack("<III", 0, 1, 10) + "a.txt".encode("utf-16-le")
            self.assertEqual(watcher.parse_notifications(data), [(1, "a.txt")])
            ex = watcher.ChangeApplier(idx, 0, Excludes(patterns=["*.tmp"]))
            open(os.path.join(root, "x.tmp"), "w").close()
            self.assertFalse(ex.apply(watcher.ACTION_ADDED, "x.tmp"))


class FileListTests(unittest.TestCase):
    def test_export_import_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root, idx = scanned(os.path.join(d))
            idx.compute_folder_sizes()
            ids = query.search(idx, "", filter_text="")
            efu = os.path.join(d, "out.efu")
            self.assertEqual(filelist.export_results(idx, ids, efu), len(ids))
            head = open(efu, encoding="utf-8-sig").readline().strip()
            self.assertEqual(head, "Filename,Size,Date Modified,Date Created,Attributes")
            new = Index()
            filelist.import_filelist(new, efu)
            self.assertEqual(sorted(new.names[i] for i in query.search(new, "ext:py")), ["main.py", "test_main.py"])
            i = query.search(new, "dog.png")[0]
            self.assertEqual(new.size[i], 5_000_000)
            self.assertAlmostEqual(new.mtime[i], idx.mtime[query.search(idx, "dog.png")[0]], delta=1)
            self.assertEqual(new.full_path(i).lower(), os.path.join(root, "Pics", "dog.png").lower())
            self.assertTrue(query.search(new, "dm:today file:"))
            txt = os.path.join(d, "o.txt")
            filelist.export_results(idx, ids, txt)
            plain = Index()
            filelist.import_filelist(plain, txt)
            self.assertEqual(len(query.search(plain, "ext:py")), 2)

    def test_engine_indexes_filelist_and_detects_change(self):
        with tempfile.TemporaryDirectory() as d:
            lst = os.path.join(d, "l.efu")
            with open(lst, "w", encoding="utf-8-sig", newline="") as f:
                f.write('Filename,Size,Date Modified,Date Created,Attributes\r\n"Z:\\data\\one.bin",10,,,0\r\n"Z:\\data",,,,16\r\n')
            tree = os.path.join(d, "t")
            make_tree(tree)
            s = Settings()
            s.roots, s.filelists, s.use_mft, s.monitor = [tree], [lst], False, False
            s.db_path = os.path.join(d, "d.db")
            e = Engine(s)
            e.rebuild()
            e.shutdown()
            e2 = Engine(s)
            self.assertTrue(e2.load())
            self.assertEqual([e2.index.full_path(i) for i in e2.search("one.bin")], ["Z:\\data\\one.bin"])
            time.sleep(0.05)
            os.utime(lst, (time.time() + 5, time.time() + 5))
            self.assertFalse(Engine(s).load())  # list changed on disk -> index is stale


class IpcTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "named pipes")
    def test_pipe_roundtrip_streaming_and_errors(self):
        name = f"M-Finder-test-{os.getpid()}-{int(time.time())}"

        def handler(req, send_bin):
            if req["op"] == "echo":
                return {"you": req["x"]}
            if req["op"] == "stream":
                for i in range(3):
                    send_bin(bytes([i]) * (1 << 20 if i == 2 else 10))  # a 1 MiB frame too
                return {"n": 3}
            raise PermissionError("nope")

        sid = ipc.current_user_sid()
        self.assertTrue(sid.startswith("S-1-"))
        srv = ipc.PipeServer(name, handler, ipc.sddl_for([sid], system_and_admins=False))
        srv.start()
        try:
            self.assertEqual(ipc.call(name, {"op": "echo", "x": "héllo"}), {"you": "héllo"})
            with ipc.PipeClient(name) as c:
                frames = list(c.stream({"op": "stream"}))
                self.assertEqual([len(p) for t, p in frames if t == ipc.T_BIN], [10, 10, 1 << 20])
                self.assertEqual(json.loads(frames[-1][1]), {"n": 3})
                with self.assertRaises(ipc.RemoteError) as cm:
                    c.call({"op": "bad"})
                self.assertIn("nope", str(cm.exception))
                self.assertEqual(c.call({"op": "echo", "x": 1}), {"you": 1})  # connection survives an error
            with self.assertRaises(ipc.PipeError):  # a second server may not squat the name
                ipc.PipeServer(name, handler, ipc.sddl_for([sid])).start()
        finally:
            srv.stop()
        with self.assertRaises(ipc.PipeError):
            ipc.call(name, {"op": "echo", "x": 1})

    def test_sddl_rejects_injection(self):
        self.assertEqual(ipc.sddl_for(["S-1-5-21-1-2-3-1001"], False), "D:P(A;;GA;;;S-1-5-21-1-2-3-1001)")
        for bad in ("S-1-5;(A;;GA;;;WD)", "WD", "S-1-5-1 "):
            with self.assertRaises(ValueError):
                ipc.sddl_for([bad])

    @unittest.skipUnless(sys.platform == "win32", "named pipes")
    def test_query_ipc_end_to_end(self):
        from mfinder import queryipc

        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, "t")
            make_tree(tree)
            s = Settings()
            s.roots, s.use_mft, s.monitor, s.db_path = [tree], False, False, os.path.join(d, "q.db")
            e = Engine(s)
            e.rebuild()
            shown = []
            if queryipc.running():
                self.skipTest("a real M-Finder is running for this user")
            quits = []
            q = queryipc.QueryIpc(e, on_show=shown.append, on_quit=lambda: quits.append(1))
            q.start()
            try:
                self.assertTrue(queryipc.running())
                res = queryipc.request({"op": "search", "query": "cat", "filter": "Picture", "count": 1, "sort": "size", "asc": False})
                self.assertEqual(res["total"], 2)
                self.assertEqual(res["results"][0]["name"], "cat.JPG")
                # the next page of the same query comes from the cached result list
                page2 = queryipc.request({"op": "search", "query": "cat", "filter": "Picture", "offset": 1, "count": 1, "sort": "size", "asc": False})
                self.assertEqual((page2["total"], len(page2["results"])), (2, 1))
                self.assertNotEqual(page2["results"][0]["name"], "cat.JPG")
                status = queryipc.request({"op": "status"})
                self.assertEqual(status["objects"], e.index.live_count())
                self.assertTrue(status["ready"])
                self.assertIn("Picture", queryipc.request({"op": "filters"})["filters"])
                queryipc.request({"op": "quit"})
                self.assertEqual(quits, [1])
                queryipc.request({"op": "show", "query": "hi"})
                self.assertEqual(shown, ["hi"])
                with self.assertRaises(OSError):
                    queryipc.request({"op": "search", "query": "regex:(", "regex": True})
            finally:
                q.stop()
                e.shutdown()


class ServiceTests(unittest.TestCase):
    def test_record_encoding_roundtrip(self):
        recs = [(1 << 48 | 77, 5, 123, 0x100, 0x20, "héllo 한글.txt"), (9, 5, 0, 0, 0x10, "d")]
        self.assertEqual(list(service.decode_records(service.encode_records(recs))), recs)
        meta = service.META.pack(77, 4096, 1.5, 2.5, 3.5)
        self.assertEqual(list(service.META.iter_unpack(meta)), [(77, 4096, 1.5, 2.5, 3.5)])

    def test_proxy_rejects_bad_requests(self):
        p = service.VolumeProxy()
        self.assertTrue(p.handle({"op": "ping"}, None)["service"])
        for root in ("C:\\Windows", "\\\\server\\share", "", "CC:", "../x"):
            with self.assertRaises(PermissionError):
                p.handle({"op": "query_journal", "root": root}, None)

    def test_install_refuses_user_writable_locations(self):
        problems = service.install_problems()
        if "Program Files" in sys.executable:
            self.skipTest("interpreter is in Program Files")
        self.assertTrue(problems)
        with self.assertRaises(PermissionError) as cm:
            service.install(["S-1-5-21-1-2-3-1001"], allow_user_paths=False)
        self.assertIn("user-writable", str(cm.exception))

    def test_command_line_and_sid_args(self):
        cmd = service.service_command_line(["S-1-5-21-1-2-3-1001"])
        self.assertIn("--allow-sid S-1-5-21-1-2-3-1001", cmd)
        self.assertIn("-I -c", cmd)
        self.assertEqual(service._parse_allowed_sids(["x", "--allow-sid", "S-1-5-1"]), ["S-1-5-1"])
        with self.assertRaises(SystemExit):
            service._parse_allowed_sids(["--other"])


class UiSupportTests(unittest.TestCase):
    def test_i18n(self):
        try:
            self.assertEqual(i18n.init("ko"), "ko")
            self.assertEqual(i18n._("Open"), "열기")
            self.assertEqual(i18n._("Never translated"), "Never translated")
            self.assertIn("ko", i18n.available())
            self.assertEqual(i18n.init("en"), "en")
            self.assertEqual(i18n._("Open"), "Open")
            self.assertEqual(i18n.init("xx"), "en")  # unknown language falls back
        finally:
            i18n.init("en")

    def test_every_ui_string_is_translated(self):
        import ast
        import glob

        keys = set(json.load(open(os.path.join(os.path.dirname(__file__), "..", "mfinder", "locales", "ko.json"), encoding="utf-8")))
        for f in glob.glob(os.path.join(os.path.dirname(__file__), "..", "mfinder", "*.py")):
            for n in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_" and n.args:
                    try:
                        v = ast.literal_eval(n.args[0])
                    except ValueError:
                        continue
                    self.assertIn(v, keys, f"{os.path.basename(f)}: untranslated {v[:40]!r}")

    def test_themes_and_hotkeys(self):
        self.assertIn(theme.resolve("system"), ("light", "dark"))
        self.assertEqual(theme.resolve("bogus"), "light")
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            theme.apply(root, "dark")
            bar = theme.MenuBar(root)
            m = bar.add_menu("File")
            m.add_command(label="Open")
            btn, menu = bar.menus[0]
            self.assertEqual(bar.cget("bg"), theme.PALETTES["dark"]["window"])
            self.assertEqual(btn.cget("bg"), theme.PALETTES["dark"]["window"])
            self.assertEqual(btn.cget("fg"), theme.PALETTES["dark"]["fg"])
            self.assertEqual(btn.cget("activebackground"), theme.PALETTES["dark"]["sel"])
            self.assertIs(menu, m)
        finally:
            theme.apply(root, "light")
            root.destroy()
        self.assertEqual(tray.parse_hotkey("Ctrl+Shift+F12"), (6, 0x7B))
        self.assertIsNone(tray.parse_hotkey("f12"))  # a bare key would hijack typing
        self.assertIsNone(tray.parse_hotkey("ctrl+"))
        png = tray.make_icon_png(16)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        with tempfile.TemporaryDirectory() as d:
            p = tray.ensure_icon_file(d)
            self.assertEqual(open(p, "rb").read(4), b"\x00\x00\x01\x00")

    def test_settings_roundtrip_with_new_fields(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("APPDATA")
            os.environ["APPDATA"] = d
            try:
                s = Settings()
                s.bookmarks = [{"name": "pics", "search": "cat"}]
                s.filters.append({"name": "Mine", "search": "ext:xyz", "case": False, "ww": False, "path": False, "regex": False})
                s.theme, s.language, s.global_hotkey = "dark", "ko", "ctrl+alt+f"
                s.save()
                t = Settings.load()
                self.assertEqual((t.theme, t.language, t.global_hotkey), ("dark", "ko", "ctrl+alt+f"))
                self.assertEqual(t.bookmarks, s.bookmarks)
                self.assertIsNotNone(t.find_filter("Mine"))
                self.assertIsNone(t.find_filter("nope"))
                t.filters[0]["name"] = "Everything"
                t.current_filter = "Everything"
                t.save()
                migrated = Settings.load()
                self.assertEqual(migrated.current_filter, "All")
                self.assertEqual(migrated.filters[0]["name"], "All")
            finally:
                if old is None:
                    del os.environ["APPDATA"]
                else:
                    os.environ["APPDATA"] = old


if __name__ == "__main__":
    unittest.main()
