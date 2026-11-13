"""Small read-only HTTP search server (HTML + JSON), like Everything's HTTP server.

It lists matches only; it never serves file contents.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import query
from .engine import Engine
from .fmt import format_size, format_time

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>FileFinder</title>
<style>body{{font:14px Segoe UI,sans-serif;margin:1em}}input[type=text]{{width:40em}}
table{{border-collapse:collapse;margin-top:1em}}td,th{{padding:2px 12px 2px 0;text-align:left}}
th a{{color:inherit}}.n{{text-align:right}}.d{{font-weight:bold}}</style></head><body>
<form method="get"><input type="text" name="s" value="{q}" autofocus>
<input type="hidden" name="sort" value="{sort}"><input type="hidden" name="ascending" value="{asc}">
<input type="submit" value="Search"></form>
<p>{summary}</p>
<table><tr><th><a href="{h_name}">Name</a></th><th><a href="{h_path}">Path</a></th>
<th class="n"><a href="{h_size}">Size</a></th><th><a href="{h_mtime}">Date Modified</a></th></tr>
{rows}</table>{pager}</body></html>"""


def make_server(engine: Engine, bind: str, port: int, user: str = "", password: str = "") -> ThreadingHTTPServer:
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() if (user or password) else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "FileFinder"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _authorized(self) -> bool:
            if expected is None:
                return True
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), expected.encode()):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="FileFinder"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            if url.path not in ("/", ""):
                self.send_error(404)
                return
            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            text = qs.get("s", qs.get("search", ""))
            sort = qs.get("sort", "name")
            asc = qs.get("ascending", "1") != "0"
            want_json = qs.get("json") == "1"
            try:
                offset = max(0, int(qs.get("offset", qs.get("o", "0"))))
                count = max(1, min(1000, int(qs.get("count", qs.get("c", "32")))))
            except ValueError:
                self.send_error(400, "bad offset/count")
                return
            flags = query.Flags(
                case=qs.get("case") == "1",
                ww=qs.get("wholeword") == "1",
                path=qs.get("path") == "1",
                regex=qs.get("regex") == "1",
            )
            try:
                ids = engine.search(text, sort if sort in query.SORT_KEYS else "name", asc, flags=flags)
            except query.QueryError as e:
                self._send(400, "text/plain; charset=utf-8", str(e).encode())
                return
            idx = engine.index
            page = ids[offset : offset + count]
            if want_json:
                rows = []
                for i in page:
                    size, mtime = idx.meta(i)
                    rows.append(
                        {
                            "type": "folder" if idx.is_dir(i) else "file",
                            "name": idx.names[i],
                            "path": idx.parent_path(i),
                            "size": size if size >= 0 else None,
                            "date_modified": mtime if mtime > 0 else None,
                        }
                    )
                body = json.dumps({"totalResults": len(ids), "results": rows}).encode()
                self._send(200, "application/json", body)
                return
            rows_html = []
            for i in page:
                size, mtime = idx.meta(i)
                cls = ' class="d"' if idx.is_dir(i) else ""
                rows_html.append(
                    f"<tr><td{cls}>{html.escape(idx.names[i])}</td><td>{html.escape(idx.parent_path(i))}</td>"
                    f'<td class="n">{format_size(size)}</td><td>{format_time(mtime)}</td></tr>'
                )

            def link(**kw):
                params = {"s": text, "sort": sort, "ascending": "1" if asc else "0", "offset": offset, "count": count}
                params.update(kw)
                from urllib.parse import urlencode

                return html.escape("/?" + urlencode(params))

            def sort_link(key):
                return link(sort=key, ascending="0" if (sort == key and asc) else "1", offset=0)

            pager = []
            if offset > 0:
                pager.append(f'<a href="{link(offset=max(0, offset - count))}">&laquo; Prev</a>')
            if offset + count < len(ids):
                pager.append(f'<a href="{link(offset=offset + count)}">Next &raquo;</a>')
            body = PAGE.format(
                q=html.escape(text, quote=True),
                sort=html.escape(sort, quote=True),
                asc="1" if asc else "0",
                summary=f"{len(ids):,} objects" if text else f"{idx.live_count():,} objects indexed",
                h_name=sort_link("name"), h_path=sort_link("path"),
                h_size=sort_link("size"), h_mtime=sort_link("mtime"),
                rows="\n".join(rows_html),
                pager=" &nbsp; ".join(pager),
            ).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((bind, port), Handler)


class HttpService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        s = self.engine.settings
        self.server = make_server(self.engine, s.http_bind, s.http_port, s.http_user, s.http_password)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="http").start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def running(self) -> bool:
        return self.server is not None
