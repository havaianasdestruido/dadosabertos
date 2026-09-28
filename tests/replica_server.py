#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tiny static server used by the test suite to serve the offline replica.

It behaves like a real autoindex server in the ways that matter to the scraper:

* ``GET /dir``  -> 301 redirect to ``/dir/``   (tests the directory probe)
* ``GET /dir/`` -> ``index.html`` when present, otherwise a generated listing
* single-range requests (``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n``) -> 206,
  which is what the scraper's cheap duplicate probe relies on
* keeps a per-request access log so tests can assert on what was fetched

Run standalone:  python3 tests/replica_server.py tests/replica 8000
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import sys
import threading
from typing import Optional, Tuple

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "ReplicaIndex/1.0"
    protocol_version = "HTTP/1.0"       # close per request: simple + deterministic
    access_log: Optional[list] = None   # set by the test harness

    def __init__(self, *args, directory: Optional[str] = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        if self.access_log is not None:
            self.access_log.append((self.command, self.path, self.headers.get("Range")))
        if os.environ.get("REPLICA_VERBOSE"):
            super().log_message(fmt, *args)

    # -- directory handling ----------------------------------------------
    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path) and not self.path.endswith("/"):
            self.send_response(301)
            new = self.path + "/"
            if self.path.endswith("?"):
                new = self.path[:-1] + "/?"
            self.send_header("Location", new)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if os.path.isdir(path):
            for index in ("index.html", "index.htm"):
                candidate = os.path.join(path, index)
                if os.path.exists(candidate):
                    path = candidate
                    break
            else:
                return super().send_head()          # generated listing, like nginx
        if not os.path.exists(path):
            return super().send_head()              # -> 404
        try:
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        fs = os.fstat(fh.fileno())
        size = fs.st_size
        ctype = self.guess_type(path)
        rng = self._parse_range(self.headers.get("Range"), size)
        if rng:
            start, end = rng
            self._remaining = end - start + 1
            fh.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            return fh
        self._remaining = None
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.end_headers()
        return fh

    @staticmethod
    def _parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
        if not header:
            return None
        m = RANGE_RE.match(header.strip())
        if not m:
            return None
        first, last = m.group(1), m.group(2)
        if first == "" and last == "":
            return None
        if first == "":                       # suffix range: last N bytes
            length = min(int(last), size)
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
        if start >= size or start > end:
            return None
        return start, min(end, size - 1)

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def make_server(directory: str, port: int = 0, host: str = "127.0.0.1"):
    handler = functools.partial(RangeHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve_in_thread(directory: str, port: int = 0, host: str = "127.0.0.1"):
    """Start the replica server in a background thread; returns (httpd, url, thread)."""
    httpd = make_server(directory, port, host)
    thread = threading.Thread(target=httpd.serve_forever, name="replica-server", daemon=True)
    thread.start()
    actual = httpd.server_address[1]
    return httpd, "http://{}:{}".format(host, actual), thread


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    directory = argv[0] if argv else "tests/replica"
    port = int(argv[1]) if len(argv) > 1 else 8000
    if not os.path.isdir(directory):
        print("no such directory: {}".format(directory), file=sys.stderr)
        return 2
    httpd, url, _ = serve_in_thread(directory, port, host="0.0.0.0")
    print("serving {} at {}".format(directory, url))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
