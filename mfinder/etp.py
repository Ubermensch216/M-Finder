"""ETP-style FTP server: browse and search the index over FTP.

Virtual tree: ``/`` lists the indexed drives/folders as ``/C``, ``/D`` …; everything
below maps to the real path (``/C/Windows`` = ``C:\\Windows``). Listings come from
the index; downloads (``RETR``) read the real file, and only for paths that resolve
inside the index and (when ``allow_download`` is set). Extension: ``SITE SEARCH <query>``
sends matching virtual paths over the data connection.

Passive mode only (no PORT: avoids FTP bounce). FTP is clear text — keep it on
localhost or a trusted network.
"""

from __future__ import annotations

import hmac
import ntpath
import os
import socket
import socketserver
import threading
import time
from typing import Optional

from . import query
from .engine import Engine

MAX_LINE = 4096


def to_real(vpath: str) -> Optional[str]:
    """Virtual '/C/dir/x' -> 'C:\\dir\\x' (None for '/')."""
    parts = [p for p in vpath.split("/") if p]
    if not parts:
        return None
    return parts[0] + ":\\" + "\\".join(parts[1:]) if len(parts[0]) == 1 else None


def to_virtual(real: str) -> str:
    drive, rest = ntpath.splitdrive(real)
    return "/" + drive.rstrip(":") + "/" + rest.strip("\\").replace("\\", "/") if rest.strip("\\") else "/" + drive.rstrip(":")


class Session(socketserver.StreamRequestHandler):
    server: "EtpServer"

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(300)
        self.cwd = "/"
        self.user = ""
        self.authed = False
        self.pasv: Optional[socket.socket] = None

    # -- helpers --
    def reply(self, text: str) -> None:
        self.wfile.write((text + "\r\n").encode("utf-8"))
        self.wfile.flush()

    def abspath(self, arg: str) -> str:
        p = arg if arg.startswith("/") else self.cwd.rstrip("/") + "/" + arg
        out: list[str] = []
        for part in p.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if out:
                    out.pop()
            else:
                out.append(part)
        return "/" + "/".join(out)

    def lookup(self, vpath: str):
        """(real path, entry id) for an indexed path; ('', None) for the virtual root."""
        idx = self.server.engine.index
        real = to_real(vpath)
        if real is None:
            return "", None
        i = idx.resolve(real)
        return real, i

    def open_data(self) -> Optional[socket.socket]:
        if self.pasv is None:
            self.reply("425 Use PASV first")
            return None
        self.pasv.settimeout(15)
        try:
            conn, _ = self.pasv.accept()
        except OSError:
            self.reply("425 Cannot open data connection")
            return None
        finally:
            self.pasv.close()
            self.pasv = None
        return conn

    def list_lines(self, vpath: str, names_only: bool) -> Optional[list[str]]:
        eng = self.server.engine
        idx = eng.index
        if vpath == "/":
            kids = [(to_virtual(r["path"].rstrip("\\/")).lstrip("/"), r["root_id"]) for r in idx.roots if r["kind"] != "filelist"]
            rows = [(n, True, 0, 0.0) for n, _ in kids]
        else:
            real, i = self.lookup(vpath)
            if i is None:
                return None
            if not idx.is_dir(i):
                size, mtime = idx.meta(i)
                rows = [(idx.names[i], False, size, mtime)]
            else:
                rows = []
                for cid in idx.children_map().get(i, {}).values():
                    size, mtime = idx.meta(cid)
                    rows.append((idx.names[cid], idx.is_dir(cid), size, mtime))
        rows.sort(key=lambda r: (not r[1], r[0].lower()))
        if names_only:
            return [r[0] for r in rows]
        out = []
        for name, is_dir, size, mtime in rows:
            when = time.strftime("%b %d %H:%M", time.localtime(mtime)) if mtime > 0 else "Jan 01 00:00"
            out.append(f"{'d' if is_dir else '-'}r--r--r-- 1 ftp ftp {max(size, 0):>12} {when} {name}")
        return out

    def send_lines(self, lines: list[str]) -> None:
        conn = self.open_data()
        if conn is None:
            return
        self.reply("150 Opening data connection")
        try:
            with conn:
                conn.sendall(("\r\n".join(lines) + ("\r\n" if lines else "")).encode("utf-8"))
            self.reply("226 Transfer complete")
        except OSError:
            self.reply("426 Connection closed; transfer aborted")

    # -- main loop --
    def handle(self) -> None:
        self.reply("220 M-Finder ETP server ready")
        while True:
            try:
                raw = self.rfile.readline(MAX_LINE)
            except (OSError, ValueError):
                return
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            if cmd == "QUIT":
                self.reply("221 Goodbye")
                return
            if cmd == "USER":
                self.user, self.authed = arg, False
                self.reply("331 Password required")
                continue
            if cmd == "PASS":
                self.authed = self.server.check_login(self.user, arg)
                if not self.authed:
                    time.sleep(1)  # slow down guessing
                self.reply("230 Logged in" if self.authed else "530 Login incorrect")
                continue
            if cmd in ("SYST", "FEAT", "OPTS", "NOOP", "TYPE", "MODE", "STRU"):
                self.reply({"SYST": "215 UNIX Type: L8", "FEAT": "211-Features:\r\n UTF8\r\n SIZE\r\n MDTM\r\n211 End",
                            "OPTS": "200 OK", "NOOP": "200 OK", "TYPE": "200 Type set", "MODE": "200 OK",
                            "STRU": "200 OK"}[cmd])
                continue
            if not self.authed:
                self.reply("530 Please log in")
                continue
            try:
                self.command(cmd, arg)
            except OSError:
                self.reply("451 Local error")

    def command(self, cmd: str, arg: str) -> None:
        eng = self.server.engine
        idx = eng.index
        if cmd == "PWD":
            self.reply(f'257 "{self.cwd}"')
        elif cmd in ("CWD", "CDUP"):
            target = self.abspath(".." if cmd == "CDUP" else arg)
            if target == "/" or (self.lookup(target)[1] is not None and idx.is_dir(self.lookup(target)[1])):
                self.cwd = target
                self.reply("250 Directory changed")
            else:
                self.reply("550 No such directory")
        elif cmd in ("PASV", "EPSV"):
            if self.pasv:
                self.pasv.close()
            s = socket.socket()
            s.bind((self.request.getsockname()[0], 0))
            s.listen(1)
            self.pasv = s
            host, port = s.getsockname()[:2]
            if cmd == "EPSV":
                self.reply(f"229 Entering Extended Passive Mode (|||{port}|)")
            else:
                self.reply("227 Entering Passive Mode (%s,%d,%d)" % (host.replace(".", ","), port >> 8, port & 255))
        elif cmd in ("PORT", "EPRT"):
            self.reply("502 Active mode is disabled")
        elif cmd in ("LIST", "NLST"):
            path = self.abspath(arg if arg and not arg.startswith("-") else ".")
            lines = self.list_lines(path, cmd == "NLST")
            if lines is None:
                self.reply("550 No such file or directory")
            else:
                self.send_lines(lines)
        elif cmd == "SITE" and arg.upper().startswith("SEARCH "):
            text = arg[7:]
            try:
                ids = eng.search(text, "name", True)
            except query.QueryError as e:
                self.reply(f"501 {e}")
                return
            lines = [to_virtual(idx.full_path(i)) for i in ids[:100000]]
            self.send_lines(lines)
        elif cmd == "SIZE":
            _, i = self.lookup(self.abspath(arg))
            if i is None or idx.is_dir(i):
                self.reply("550 Not a file")
            else:
                self.reply(f"213 {max(idx.meta(i)[0], 0)}")
        elif cmd == "MDTM":
            _, i = self.lookup(self.abspath(arg))
            if i is None:
                self.reply("550 No such file")
            else:
                self.reply("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(max(idx.meta(i)[1], 0))))
        elif cmd == "RETR":
            self.retr(self.abspath(arg))
        else:
            self.reply("502 Command not implemented")

    def retr(self, vpath: str) -> None:
        eng = self.server.engine
        if not eng.settings.etp_allow_download:
            self.reply("550 Downloads are disabled")
            return
        real, i = self.lookup(vpath)
        idx = eng.index
        if i is None or idx.is_dir(i):
            self.reply("550 No such file")
            return
        # refuse to follow junctions/symlinks out of the indexed tree
        root = next((r["path"] for r in idx.roots if real.lower().startswith(r["path"].rstrip("\\/").lower())), real)
        if not os.path.realpath(real).lower().startswith(os.path.realpath(root).lower().rstrip("\\/")):
            self.reply("550 Refusing to follow a link out of the indexed folder")
            return
        try:
            fh = open(real, "rb")
        except OSError:
            self.reply("550 Cannot open file")
            return
        with fh:
            conn = self.open_data()
            if conn is None:
                return
            self.reply("150 Opening data connection")
            try:
                with conn:
                    while chunk := fh.read(1 << 16):
                        conn.sendall(chunk)
                self.reply("226 Transfer complete")
            except OSError:
                self.reply("426 Connection closed; transfer aborted")

    def finish(self) -> None:
        if self.pasv:
            self.pasv.close()
        super().finish()


class EtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, engine: Engine, bind: str, port: int, user: str = "", password: str = "") -> None:
        self.engine = engine
        self.user, self.password = user, password
        super().__init__((bind, port), Session)

    def check_login(self, user: str, password: str) -> bool:
        if not self.user and not self.password:
            return True
        ok_user = hmac.compare_digest(user.encode(), self.user.encode())
        ok_pass = hmac.compare_digest(password.encode(), self.password.encode())
        return ok_user and ok_pass


class EtpService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.server: Optional[EtpServer] = None

    def start(self) -> None:
        s = self.engine.settings
        self.server = EtpServer(self.engine, s.etp_bind, s.etp_port, s.etp_user, s.etp_password)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="etp").start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def running(self) -> bool:
        return self.server is not None
