#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indexclone — a polite cloner/deduper for open-data directory listings.

It walks public "autoindex" style listings (Apache ``<pre>``/table indexes,
nginx autoindex, generic HTML pages full of links) and mirrors every document
it finds, one output folder per site, with full de-duplication.

Why this exists
---------------
Several Brazilian public bodies publish open data as plain web-directory
listings instead of an API/catalogue.  Mirroring them by hand is painful
because the same document is frequently published many times (e.g.
``AUDIENCIA_2024_-_LISTA_DE_PRESENCA.pdf`` and its ``...1.pdf`` clone).
This tool crawls the listing, downloads everything, and removes duplicates by
content (SHA-256), keeping a complete audit trail of what was collapsed.

Key behaviour
-------------
* one output folder per site: ``<out>/<slug>/{files,_index,_reports}``
* de-duplication by content hash (optionally short-circuited by a
  Range-based probe so huge duplicates are never downloaded twice)
* resumable / idempotent: re-running only fetches what changed
* polite: per-host rate limit, retries with backoff, robots.txt aware
* audit trail: manifest.json/csv, duplicates.csv, errors.log, summary.json
* no third-party dependencies (stdlib only), Python 3.8+

Typical use
-----------
    # everything from sites.json (one folder per site under ./outputs)
    python3 scraper.py --config sites.json --all

    # a single listing, ad-hoc
    python3 scraper.py --url https://example.gov.br/dados/ --slug my_dataset

    # rehearse without downloading payloads
    python3 scraper.py --config sites.json --all --dry-run

    # mirror to a scratch disk and hard-link duplicates instead of copying
    python3 scraper.py --config sites.json --all --out /data/mirror \
        --dupe-strategy hardlink --jobs 8 --rate 5

Exit codes: 0 = ok, 1 = finished with per-file errors, 2 = fatal/setup error.

Author: Arena agent.  Licence: MIT (public-domain data, tool provided as-is).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import dataclasses
import datetime as dt
import gzip
import hashlib
import html as html_mod
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Sequence, Set, Tuple

__version__ = "1.0.0"

DEFAULT_UA = (
    "indexclone/{} (+https://github.com/havaianasdestruido/dadosabertos; "
    "open-data mirroring bot; contact: repository owner)".format(__version__)
)
CHUNK = 256 * 1024          # streaming read size
PROBE_BYTES = 64 * 1024     # head/tail probe size for range-based dedupe
LOG = logging.getLogger("indexclone")

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n: Optional[float]) -> str:
    """12345678 -> '11.8 MiB'."""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0 or unit == "PiB":
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "{:.1f} PiB".format(n)


def parse_size_token(token: str) -> Optional[int]:
    """'3.8M' / '387K' / '104M' / '-' (Apache human sizes) -> bytes."""
    token = token.strip()
    if not token or token == "-":
        return None
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([KMGTP])?", token, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    return int(value * mult.get((m.group(2) or "").upper(), 1))


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS.update({"fev": 2, "abr": 4, "mai": 5, "ago": 8, "set": 9, "out": 10, "dez": 12})


def parse_listing_date(text: str) -> Optional[str]:
    """Return an ISO-8601 UTC timestamp for the usual autoindex date formats."""
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}):(\d{2})", text)
    if m:
        mon = MONTHS.get(m.group(2).lower()[:3])
        if mon:
            try:
                return dt.datetime(int(m.group(3)), mon, int(m.group(1)),
                                   int(m.group(4)), int(m.group(5)),
                                   tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", text)
    if m:
        try:
            return dt.datetime(*[int(x) for x in m.groups()], tzinfo=dt.timezone.utc
                               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

_SORT_QUERY_KEYS = ("c", "o", "sort", "order")


def canonical_url(url: str) -> str:
    """Normalised key for de-duplicating *URLs* (not a fetchable URL)."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/~:@!$&'()*+,;=")
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and parts.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parts.scheme == "https":
        netloc = netloc[:-4]
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _same_file(left: str, right: str) -> bool:
    try:
        return os.path.exists(left) and os.path.exists(right) \
            and os.path.samefile(left, right)
    except OSError:
        return False


def name_key(name: str) -> str:
    """Loose name identity: 'Dados_abertos_FGTS.zip' -> 'dadosabertosfgts'.

    Used to spot a re-publication of the same document *before* downloading it,
    since in a metadata-less listing the name is all we have.
    """
    base = (name or "").rsplit("/", 1)[-1].lower()
    base = re.sub(r"\.(pdf|zip|csv|dwg|kml|kmz|jpg|jpeg|png|docx?|xlsx?|txt|json|xml|gz)$",
                  "", base)
    return re.sub(r"[^a-z0-9]+", "", base)


def as_dir_url(url: str) -> str:
    """Normalised directory URL (trailing slash, as after a 301)."""
    return url if url.endswith("/") else url + "/"


def is_sort_link(url: str) -> bool:
    """Apache offers '?C=N;O=D' style sort links — never documents."""
    query = urllib.parse.urlsplit(url).query
    if not query:
        return False
    keys = {k.lower() for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True)}
    if not keys:
        return False
    return keys.issubset(set(_SORT_QUERY_KEYS))


def url_under_prefix(url: str, prefix: str) -> bool:
    """True when *url* is inside the site scope (host + path prefix)."""
    u, p = urllib.parse.urlsplit(url), urllib.parse.urlsplit(prefix)
    if (u.hostname or "").lower() != (p.hostname or "").lower():
        return False
    if (u.port or 0) != (p.port or 0):
        # tolerate default-port vs explicit-port mismatch
        if not ({u.port, p.port} <= {None, 80, 443}):
            return False
    base = p.path if p.path.endswith("/") else p.path + "/"
    path = u.path or "/"
    return path.startswith(base) or canonical_url(url) == canonical_url(prefix)


def rel_dir_for(url: str, base_url: str) -> str:
    """Relative directory of *url* inside the mirrored site (posix style)."""
    u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base_url)
    bpath = urllib.parse.unquote(b.path)
    if not bpath.endswith("/"):
        bpath += "/"
    path = urllib.parse.unquote(u.path)
    if path.startswith(bpath):
        path = path[len(bpath):]
    elif path == bpath.rstrip("/"):
        path = ""
    else:  # scope widened by the user; keep a readable tree anyway
        path = path.lstrip("/")
    path = path.lstrip("/")
    while "%" in path:  # belt & braces for double-encoded listings
        new = urllib.parse.unquote(path)
        if new == path:
            break
        path = new
    return path.strip("/")


_RESERVED_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def decode_component(raw: str) -> str:
    """Percent-decode a single path component, tolerating latin-1 servers."""
    try:
        return urllib.parse.unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return urllib.parse.unquote(raw, encoding="latin-1", errors="replace")


def sanitize_component(name: str, max_len: int = 180) -> str:
    """Turn an arbitrary URL component into a safe file name."""
    name = html_mod.unescape(name or "").replace("\x00", "")
    name = re.sub(r"[\x01-\x1f\x7f]", "", name)
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    name = name.rstrip(" .")  # trailing dots/spaces break Windows/rsync
    if not name:
        name = "_"
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 12:
        stem, ext = stem[: max(1, max_len - len(ext) - 1)], "." + ext
    else:
        stem, ext = name[:max_len], ""
    if stem.lower() in _RESERVED_WIN:
        stem += "_"
    return stem + ext


def local_relpath(url: str, base_url: str,
                  taken: Optional[Dict[str, str]] = None) -> str:
    """Mirror path (posix, relative to ``files/``) for *url*.

    ``taken`` maps an already used relative path back to its canonical URL so
    that two distinct URLs which collapse to the same file name do not
    overwrite each other.
    """
    u = urllib.parse.urlsplit(url)
    segments = [s for s in urllib.parse.unquote(u.path).split("/") if s not in ("", ".")]
    base_segments = [s for s in urllib.parse.unquote(urllib.parse.urlsplit(base_url).path).split("/")
                     if s not in ("", ".")]
    if base_segments and segments[: len(base_segments)] == base_segments:
        segments = segments[len(base_segments):]
    if u.path.endswith("/"):
        segments = segments + ["index.html"]
    segments = [sanitize_component(decode_component(s)) for s in segments] or ["index.html"]
    rel = "/".join(segments)
    if taken is not None:
        canon = canonical_url(url)
        if rel in taken and taken[rel] != canon:
            stem, dot, ext = rel.rpartition(".")
            i = 2
            while "{}_{}{}{}".format(stem, i, dot, ext) in taken:
                i += 1
            rel = "{}_{}{}{}".format(stem, i, dot, ext)
        taken[rel] = canon
    return rel


# --------------------------------------------------------------------------
# listing parsing
# --------------------------------------------------------------------------

RE_ANCHOR = re.compile(
    r"""<a\b(?P<pre>[^>]*?)\bhref\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s>]+))"""
    r"""[^>]*>(?P<text>.*?)</a\s*>""",
    re.I | re.S)
RE_TAG = re.compile(r"<[^>]+>")
RE_DATE = re.compile(
    r"(?:\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})")
RE_SIZE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?\s*[KMGTP])?(?<![\w.])"
                     r"(?:(?P<num>\d+)\s*(?P<unit>[KMGTP])?\b|(?P<dash>-))", re.I)
RE_INDEX_HINT = re.compile(r"index\s+of\b", re.I)


@dataclasses.dataclass
class Entry:
    """One link found in a listing.

    ``is_dir`` is authoritative only when the listing says so (trailing slash)
    or when a size proves it is a file. Listings that publish neither dates nor
    sizes (Macau, PGFN, OpenDataSoft) leave ``unknown`` set, and the crawler
    resolves those with a 4 KiB probe.
    """

    url: str
    name: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[str] = None
    unknown: bool = False
    origin: str = "listing"     # listing | probe

    def as_row(self) -> Dict[str, object]:
        return {"url": self.url, "name": self.name, "is_dir": self.is_dir,
                "size": self.size, "mtime": self.mtime, "origin": self.origin}


def _text_of(fragment: str) -> str:
    return html_mod.unescape(RE_TAG.sub("", fragment or "")).replace("\xa0", " ").strip()


def _meta_from_text(text: str) -> Tuple[Optional[str], Optional[int]]:
    """(mtime, size) from a plain index text run: '27-Feb-2020 16:28   -   '."""
    mtime = parse_listing_date(text)
    size = None
    if mtime:                       # the size always follows the date
        pos = RE_DATE.search(text)
        after = text[pos.end(): pos.end() + 40] if pos else ""
        msz = re.search(r"(\d+(?:[.,]\d+)?\s*[KMGTP]?|-)", after, re.I)
        if msz:
            size = parse_size_token(msz.group(1).replace(" ", ""))
    else:                           # date-less servers still publish a bare size
        msz = re.fullmatch(r"\s*(\d+|[\d.]+\s*[KMGTP]|-)\s*", text, re.I)
        if msz:
            size = parse_size_token(msz.group(1).replace(" ", ""))
    return mtime, size


def _attr(tag: str, name: str) -> str:
    m = re.search(r"""\b{}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""".format(re.escape(name)),
                  tag or "", re.I)
    if not m:
        return ""
    return html_mod.unescape(next(g for g in m.groups() if g is not None) or "").strip()


def parse_listing(html_text: str, page_url: str, scope: str,
                  follow_external: bool = False) -> List[Entry]:
    """Extract entries from any of the autoindex flavours we care about.

    Handles Apache ``<pre>`` indexes, Apache 2.4 tables, nginx autoindex and
    plain link-farm pages where each anchor is a sibling directory.
    """
    entries: List[Entry] = []
    seen = set()
    for m in RE_ANCHOR.finditer(html_text):
        href = (m.group("dq") or m.group("sq") or m.group("bare") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "data:")):
            continue
        if href.startswith("?"):  # in-page sort/filter link
            continue
        url = urllib.parse.urljoin(page_url, href)
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            continue
        if is_sort_link(url):
            continue
        if not follow_external and not url_under_prefix(url, scope):
            continue
        text = _text_of(m.group("text"))
        if text.lower().startswith("parent directory") or href.rstrip("/") in ("..", "../", "."):
            continue
        if text.lower().startswith("up to ") or " to parent directory" in text.lower():
            continue
        # -- metadata: whatever follows the anchor, past the cell boundary
        tail = html_text[m.end(): m.end() + 800]
        mtime = size = None
        # Apache 2.4 tables carry the exact values in data-sort-value attributes
        exact_date = re.search(r'class="[^"]*\bdatetime\b[^"]*"[^>]*data-sort-value="([^"]+)"',
                               tail, re.I)
        if exact_date:
            mtime = parse_listing_date(exact_date.group(1))
        exact_size = re.search(r'class="[^"]*\bsize\b[^"]*"[^>]*data-sort-value="(\d+)"',
                               tail, re.I)
        if exact_size:
            size = int(exact_size.group(1))
        if mtime is None or size is None:
            # skip the cell boundary so the visible text run starts at the value
            body = re.sub(r"^\s*(?:</(?:td|th|span|div|p)>\s*<t[dh][^>]*>|<br\s*/?>|</?p[^>]*>)",
                          "", tail, flags=re.I)
            runs = []
            cut = body.find("<")
            if cut > 0:
                runs.append(body[:cut])                 # <pre> dialect: plain text run
            row_end = body.find("</tr>")
            if row_end > 0:
                runs.append(_text_of(body[:row_end]))   # table dialect: whole row
            if cut < 0:
                runs.append(body)
            for run in runs:
                run_mtime, run_size = _meta_from_text(_text_of(run))
                mtime = mtime if mtime is not None else run_mtime
                size = size if size is not None else run_size
                if mtime is not None and size is not None:
                    break
        title = _attr(m.group(0), "title")
        if (not text or "..&gt;" in text or "..>" in text or text.endswith(("...", "…"))) and title:
            text = title                      # truncated autoindex cell -> link title
        name = text.rsplit("/", 1)[-1] if text.endswith("/") else text
        if not name or "..>" in name or "…" in name:
            name = decode_component(
                urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or name
        else:
            name = name.strip()
        is_dir = href.endswith("/") or url.endswith("/") or text.endswith("/")
        if size is not None:
            is_dir = False
        unknown = (not is_dir) and size is None and mtime is None
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        entries.append(Entry(url=url, name=name, is_dir=is_dir, size=size,
                             mtime=mtime, unknown=unknown, origin="listing"))
    return entries


def looks_like_listing(html_text: str) -> bool:
    """True for pages that enumerate files (autoindex) rather than content."""
    if RE_INDEX_HINT.search(html_text[:4000]):
        return True
    anchors = RE_ANCHOR.findall(html_text)
    return len(anchors) >= 3 and len(html_text) < 400_000


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class RateLimiter:
    """Simple minimum-interval limiter, one gate per host."""

    def __init__(self, per_second: float):
        self.interval = 0.0 if per_second <= 0 else 1.0 / per_second
        self._locks: Dict[str, threading.Lock] = {}
        self._next: Dict[str, float] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def wait(self, host: str) -> None:
        if self.interval <= 0:
            return
        lock = self._lock_for(host)
        with lock:
            now = time.monotonic()
            when = self._next.get(host, 0.0)
            if when > now:
                time.sleep(when - now)
                now = time.monotonic()
            self._next[host] = now + self.interval


class _SkipRequest(Exception):
    """Internal: a limit stopped this document (already recorded)."""


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.url, self.status = url, status


def request_ready(url: str) -> str:
    """Percent-encode the parts of *url* http.client cannot send (spaces, UTf-8).

    Listings do occasionally publish hrefs with raw spaces or accents; browsers
    encode them silently, and so must we before handing the URL to urllib.
    """
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="!#$%&'()*+,-./:;=@[]_~")
    query = urllib.parse.quote(parts.query, safe="!#$%&'()*+,-./:;=?@[]_~")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


class Fetcher:
    """urllib wrapper: retries, backoff, gzip, throttling, robots.txt."""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 60.0,
                 retries: int = 3, rate: float = 2.0, respect_robots: bool = True):
        self.ua = user_agent
        self.timeout = timeout
        self.retries = max(1, retries)
        self.limiter = RateLimiter(rate)
        self.respect_robots = respect_robots
        self._robots: Dict[str, Optional[Tuple[bool, List[str]]]] = {}
        self._robots_lock = threading.Lock()
        handlers = [urllib.request.HTTPRedirectHandler()]
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", self.ua),
                                  ("Accept", "*/*"),
                                  ("Accept-Encoding", "gzip, deflate")]

    # -- robots -----------------------------------------------------------
    def _robots_for(self, url: str) -> Optional[List[str]]:
        parts = urllib.parse.urlsplit(url)
        origin = "{}://{}".format(parts.scheme, parts.netloc)
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        rules: Optional[List[str]] = None
        try:
            robots_url = origin + "/robots.txt"
            self.limiter.wait(parts.netloc)
            req = urllib.request.Request(robots_url, headers={"User-Agent": self.ua})
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(256 * 1024).decode("utf-8", "replace")
            rules = self._parse_robots(body)
        except Exception as exc:  # noqa: BLE001 - robots is best-effort
            LOG.debug("robots.txt for %s unavailable: %s", origin, exc)
            rules = None
        with self._robots_lock:
            self._robots[origin] = rules
        return rules

    @staticmethod
    def _parse_robots(body: str) -> List[str]:
        """Disallow rules that apply to a plain '*' user-agent block."""
        disallow: List[str] = []
        active = False
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                active = value == "*"
            elif field == "disallow" and active and value:
                disallow.append(value)
        return disallow

    def robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rules = self._robots_for(url) or []
        path = urllib.parse.urlsplit(url).path or "/"
        for rule in rules:
            if rule == "/" or path.startswith(rule):
                return False
        return True

    # -- requests ---------------------------------------------------------
    def open_stream(self, url: str, headers: Optional[Dict[str, str]] = None):
        """Context manager -> (status, headers, file-like). Retries transient faults."""
        url = request_ready(url)
        parts = urllib.parse.urlsplit(url)
        req_headers = {"User-Agent": self.ua, "Accept": "*/*",
                       "Accept-Encoding": "gzip, deflate"}
        req_headers.update(headers or {})
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self.limiter.wait(parts.netloc)
            req = urllib.request.Request(url, headers=req_headers)
            try:
                resp = self.opener.open(req, timeout=self.timeout)
                status, hdrs = resp.getcode(), dict(resp.headers.items())
                if (hdrs.get("Content-Encoding", "").lower() == "gzip"
                        and "Range" not in req_headers):
                    resp = gzip.GzipFile(fileobj=resp)
                return _StreamContext(resp, status, hdrs)
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in (408, 425, 429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = self._retry_delay(attempt, exc.headers.get("Retry-After"))
                    LOG.debug("HTTP %s on %s, retry in %.1fs", status, url, delay)
                    time.sleep(delay)
                    last = exc
                    continue
                if status == 404:
                    raise FetchError(url, "HTTP 404 not found", 404) from exc
                raise FetchError(url, "HTTP {} {}".format(status, exc.reason), status) from exc
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    delay = self._retry_delay(attempt, None)
                    LOG.debug("network error on %s (%s), retry in %.1fs", url, exc, delay)
                    time.sleep(delay)
                    continue
                raise FetchError(url, "network error: {}".format(exc)) from exc
        raise FetchError(url, "exhausted retries: {}".format(last))

    @staticmethod
    def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(120.0, float(retry_after))
            except ValueError:
                pass
        return min(60.0, (2 ** attempt) + random.random())

    def get_text(self, url: str, limit: int = 4 * 1024 * 1024) -> Tuple[str, Dict[str, str], str]:
        """GET a small text resource -> (text, headers, final_url)."""
        with self.open_stream(url, headers={"Accept": "text/html,*/*"}) as stream:
            raw = stream.read(limit)
            headers, final = stream.headers, stream.url
        charset = _charset_from_headers(headers)
        text = raw.decode(charset, "replace") if charset else decode_html(raw)
        return text, headers, final

    def head(self, url: str) -> Dict[str, str]:
        with self.open_stream(url, headers={"Range": "bytes=0-0"}) as stream:
            return dict(stream.headers)


class _StreamContext:
    """Tiny context manager so responses are always closed."""

    def __init__(self, stream, status: int, headers: Dict[str, str]):
        self._stream = stream
        self.status = status
        self.headers = {k: v for k, v in headers.items()}
        self.url = getattr(stream, "url", None) or getattr(stream, "geturl", lambda: None)()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)


def _charset_from_headers(headers: Dict[str, str]) -> Optional[str]:
    ctype = headers.get("Content-Type", "")
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    return m.group(1) if m else None


def decode_html(raw: bytes) -> str:
    """Decode HTML bytes, honouring <meta charset>, defaulting to utf-8/latin-1."""
    head = raw[:4096].decode("ascii", "ignore")
    m = re.search(r"""<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""", head, re.I)
    if m:
        try:
            return raw.decode(m.group(1), "replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


# --------------------------------------------------------------------------
# de-duplication
# --------------------------------------------------------------------------

class DedupIndex:
    """Global content index: hash -> first stored copy (+ size/head/tail probes).

    Thread safe: downloads run in a pool but every mutation happens under a lock.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.by_hash: Dict[str, Dict[str, object]] = {}
        self.by_size: Dict[int, List[Dict[str, object]]] = {}
        self.by_name: Dict[str, List[Dict[str, object]]] = {}
        self._lock = threading.Lock()
        self.stats = {"hash_hits": 0, "probe_hits": 0, "collapsed_bytes": 0}

    # -- registration -----------------------------------------------------
    def register(self, record: Dict[str, object]) -> None:
        with self._lock:
            self._register_locked(record)

    def _register_locked(self, record: Dict[str, object]) -> None:
        self.by_hash.setdefault(str(record["sha256"]), record)
        if record.get("size") is not None:
            bucket = self.by_size.setdefault(int(record["size"]), [])
            if record not in bucket:
                bucket.append(record)
        key = name_key(str(record.get("name") or record.get("url") or ""))
        if key:
            bucket = self.by_name.setdefault(key, [])
            if record not in bucket:
                bucket.append(record)

    def claim(self, record: Dict[str, object]) -> Optional[Dict[str, object]]:
        """Atomically register *record*; returns the earlier copy if there is one.

        Two threads finishing identical downloads at the same time would both
        miss a plain lookup-then-register, so the whole decision happens here.
        """
        with self._lock:
            existing = self.by_hash.get(str(record["sha256"]))
            if existing is not None:
                return existing
            self._register_locked(record)
            return None

    def lookup_hash(self, sha256: str) -> Optional[Dict[str, object]]:
        if not self.enabled:
            return None
        with self._lock:
            return self.by_hash.get(sha256)

    def known_name_keys(self) -> set:
        with self._lock:
            return set(self.by_name)

    def candidates_by_name(self, name: str) -> List[Dict[str, object]]:
        if not self.enabled:
            return []
        with self._lock:
            return list(self.by_name.get(name_key(name), ()))

    def candidates_by_size(self, size: Optional[int]) -> List[Dict[str, object]]:
        if not self.enabled or size is None:
            return []
        with self._lock:
            return list(self.by_size.get(int(size), ()))

    def note_hit(self, method: str, saved_bytes: int) -> None:
        with self._lock:
            if method == "range-probe":
                self.stats["probe_hits"] += 1
            else:
                self.stats["hash_hits"] += 1
            self.stats["collapsed_bytes"] += saved_bytes

    def known_hashes(self) -> Dict[str, Dict[str, object]]:
        with self._lock:
            return dict(self.by_hash)


def fingerprint(path: str, size: int) -> Tuple[str, str]:
    """(head, tail) SHA-256 digests of the first/last PROBE_BYTES of a file."""
    with open(path, "rb") as fh:
        head = hashlib.sha256(fh.read(PROBE_BYTES))
        fh.seek(max(0, size - PROBE_BYTES))
        tail = hashlib.sha256(fh.read(PROBE_BYTES))
    return head.hexdigest(), tail.hexdigest()


# --------------------------------------------------------------------------
# site crawling / cloning
# --------------------------------------------------------------------------

@dataclasses.dataclass
class SiteConfig:
    slug: str
    url: str
    urls: Optional[List[str]] = None      # extra entry points, same output folder
    title: str = ""
    max_depth: int = 4
    max_file_bytes: Optional[int] = None
    max_files: Optional[int] = None
    include: Optional[str] = None
    exclude: Optional[str] = None
    follow_external: bool = False
    note: str = ""
    # the keys the config file spelled out for this site: a real 0/False counts
    # as a value, so `defaults` must not overwrite it (and must be usable at all)
    explicit: Set[str] = dataclasses.field(default_factory=set, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "SiteConfig":
        known = {f.name for f in dataclasses.fields(cls) if f.name != "explicit"}
        values = {k: v for k, v in data.items() if k in known}
        config = cls(**values)  # type: ignore[arg-type]
        # a JSON null means "not set" (so `defaults` may fill it); 0/False are values
        config.explicit = {k for k, v in values.items() if v is not None}
        return config

    @property
    def entry_urls(self) -> List[str]:
        """All listing pages the crawl starts from (the first one is the base)."""
        extra = [u for u in (self.urls or []) if u and u != self.url]
        return [self.url] + extra

    def merged(self, defaults: Dict[str, object]) -> "SiteConfig":
        """Apply the file's `defaults` to every key this site did not set itself."""
        base = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)
                if f.name != "explicit"}
        applied = {k for k in defaults if k in base and k not in self.explicit}
        for key in applied:
            base[key] = defaults[key]
        merged = SiteConfig(**base)  # type: ignore[arg-type]
        merged.explicit = set(self.explicit) | applied
        return merged


class SiteCloner:
    """Crawl one listing and mirror its documents."""

    def __init__(self, site: SiteConfig, out_root: str, fetcher: Fetcher,
                 dedup: DedupIndex, jobs: int = 4, dry_run: bool = False,
                 refresh: bool = False, dupe_strategy: str = "hardlink",
                 max_total_bytes: Optional[int] = None,
                 save_index_pages: bool = True, range_probe: bool = True,
                 budget: Optional["ByteBudget"] = None,
                 rewrite: Optional[Sequence[Tuple[str, str]]] = None,
                 command: str = ""):
        self.site = site
        self.origin_url = site.url
        self.base_url = site.url
        self.root = os.path.join(out_root, site.slug)
        self.files_root = os.path.join(self.root, "files")
        self.index_root = os.path.join(self.root, "_index")
        self.report_root = os.path.join(self.root, "_reports")
        self.fetcher = fetcher
        self.dedup = dedup
        self.jobs = jobs
        self.command = command
        self.dry_run = dry_run
        self.refresh = refresh
        self.dupe_strategy = dupe_strategy
        self.max_total_bytes = max_total_bytes
        self.budget = budget or ByteBudget(max_total_bytes)
        self.save_index_pages = save_index_pages
        self.range_probe = range_probe

        self.taken_paths: Dict[str, str] = {}
        self.records: List[Dict[str, object]] = []
        self.duplicates: List[Dict[str, object]] = []
        self.errors: List[Dict[str, object]] = []
        self.visited: set = set()
        self.state: Dict[str, Dict[str, object]] = {}
        self.state_path = os.path.join(self.report_root, "state.json")
        self.started = time.time()
        self.bytes_downloaded = 0
        self.collapsed_bytes = 0
        self._gates: Dict[int, threading.Lock] = {}
        self._gate_lock = threading.Lock()
        self.candidates: List[Dict[str, object]] = []
        self._rec_lock = threading.Lock()
        self._risky_names: set = set()
        self._include = re.compile(site.include) if site.include else None
        self._exclude = re.compile(site.exclude) if site.exclude else None
        self.robots_blocked = False
        self.rewrite: List[Tuple[str, str]] = []
        for old, new in (rewrite or []):
            self.rewrite.append((old, new))

    # -- helpers ----------------------------------------------------------
    def _fetch_url(self, url: str) -> str:
        """URL actually requested (prefix-rewritten when --rewrite is in use)."""
        for old, new in self.rewrite:
            if url.startswith(old):
                return new + url[len(old):]
        return url

    def _original_url(self, url: str) -> str:
        """Reverse of :meth:`_fetch_url` (used for redirect targets)."""
        for old, new in self.rewrite:
            if url.startswith(new):
                return old + url[len(new):]
        return url

    def _get_text(self, url: str, limit: int = 4 * 1024 * 1024) -> Tuple[str, Dict[str, str], str]:
        text, headers, final = self.fetcher.get_text(self._fetch_url(url), limit)
        return text, headers, self._original_url(final)

    def _open_stream(self, url: str, headers: Optional[Dict[str, str]] = None):
        return self.fetcher.open_stream(self._fetch_url(url), headers)

    def _robots_allows(self, url: str) -> bool:
        return self.fetcher.robots_allows(self._fetch_url(url))

    def relpath(self, url: str) -> str:
        return local_relpath(url, self.base_url, self.taken_paths)

    def _excluded(self, url: str) -> bool:
        """--exclude matches directories too, so whole subtrees can be skipped."""
        return bool(self._exclude and self._exclude.search(url))

    def _selected(self, url: str) -> bool:
        """--include applies to documents only: folders are always traversed."""
        if self._excluded(url):
            return False
        return not (self._include and not self._include.search(url))

    def log(self, msg: str, *args) -> None:
        LOG.info("[%s] " + msg, self.site.slug, *args)

    # -- crawl ------------------------------------------------------------
    def crawl(self) -> List[Entry]:
        """Breadth-first walk of the listing; returns the file entries found."""
        entries_urls = self.site.entry_urls
        queue = deque((u, 0) for u in entries_urls)
        files: "OrderedDict[str, Entry]" = OrderedDict()
        pages = 0
        while queue:
            url, depth = queue.popleft()
            key = canonical_url(url)
            if key in self.visited:
                continue
            self.visited.add(key)
            if not self._robots_allows(url):
                self.robots_blocked = True
                self.log("robots.txt disallows %s (use --ignore-robots to force)", url)
                self.errors.append({"url": url, "stage": "robots", "error": "disallowed"})
                continue
            try:
                text, headers, final_url = self._get_text(url)
            except FetchError as exc:
                self.log("listing failed: %s (%s)", url, exc)
                self.errors.append({"url": url, "stage": "listing", "error": str(exc)})
                continue
            if url in entries_urls and (url.rstrip("/") != final_url.rstrip("/")):
                # followed a redirect on an entry point (e.g. missing trailing '/')
                url = final_url
            pages += 1
            if self.save_index_pages:
                self._save_index_page(url, text)
            entries = parse_listing(text, url, self.base_url, self.site.follow_external)
            n_dirs = 0
            for entry in entries:
                if self._excluded(entry.url):
                    continue
                if entry.unknown:
                    # the listing published no metadata: probe to find out whether
                    # this is a folder to descend into or a document to mirror
                    if self._probe_is_dir(entry.url):
                        if depth + 1 <= self.site.max_depth:
                            queue.append((as_dir_url(entry.url), depth + 1))
                            n_dirs += 1
                        # a folder beyond the depth limit is not a document
                    elif self._selected(entry.url):
                        files.setdefault(canonical_url(entry.url), entry)
                    continue
                if entry.is_dir:
                    if depth + 1 <= self.site.max_depth:
                        queue.append((as_dir_url(entry.url), depth + 1))
                        n_dirs += 1
                    continue
                if self._selected(entry.url):
                    files.setdefault(canonical_url(entry.url), entry)
            self.log("crawl depth=%d dirs=%d files=%d  %s", depth, n_dirs, len(files), url)
        self.log("crawl finished: %d listing pages, %d documents", pages, len(files))
        return list(files.values())

    def _save_index_page(self, url: str, text: str) -> None:
        """Store a listing page under _index/pages/, mirroring its URL path.

        The path comes from a remote URL, so every segment is sanitised exactly
        like a document name, and the resolved directory is checked to be inside
        the pages root before anything is created: a listing reachable as
        ``.../%2e%2e/x/`` must not write outside the output folder.
        """
        pages_root = os.path.join(self.index_root, "pages")
        rel = rel_dir_for(url, self.base_url) or ""
        segments = [sanitize_component(p) for p in rel.split("/") if p]
        target_dir = os.path.realpath(os.path.join(pages_root, *segments))
        if os.path.commonpath([os.path.realpath(pages_root), target_dir]) \
                != os.path.realpath(pages_root):
            self.log("refusing to write a listing page outside %s: %s", pages_root, url)
            self.errors.append({"url": url, "stage": "index-page",
                                "error": "path escapes the pages root"})
            return
        os.makedirs(target_dir, exist_ok=True)
        name = "index.html" if rel == "" or url.endswith("/") else "page.html"
        with open(os.path.join(target_dir, name), "w", encoding="utf-8") as fh:
            fh.write("<!-- fetched {} from {} -->\n".format(utcnow(), url))
            fh.write(text)

    def _probe_is_dir(self, url: str) -> bool:
        """Decide a metadata-less link: directory (keep crawling) or file?

        Cheap by design: a 4 KiB range request, and the body is only read when
        the Content-Type says the answer might be an HTML listing.
        """
        if not self._robots_allows(url):
            return False
        target = self._fetch_url(url)
        try:
            with self._open_stream(url, headers={"Range": "bytes=0-4095"}) as stream:
                final = stream.url or ""
                ctype = (stream.headers.get("Content-Type") or "").lower()
                # servers answer /dir with a 301 to /dir/: the definitive signal
                if final == target.rstrip("/") + "/":
                    return True
                if ctype and "html" not in ctype and "text/plain" not in ctype \
                        and not ctype.startswith("text/"):
                    stream.read(1)
                    return False
                body = stream.read(128 * 1024)
        except FetchError:
            return False
        return looks_like_listing(decode_html(body)) if body else False

    # -- state ------------------------------------------------------------
    def load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                self.state = json.load(fh).get("files", {})
        except (json.JSONDecodeError, OSError) as exc:  # corrupt state -> start clean
            LOG.warning("[%s] unreadable state (%s); continuing fresh", self.site.slug, exc)
            self.state = {}
        # warm the dedup index with everything already on disk
        for url, rec in self.state.items():
            path = rec.get("local_path")
            if rec.get("sha256") and path:
                full = os.path.join(self.files_root, path)
                if os.path.exists(full):
                    self.dedup.register({"sha256": rec["sha256"], "size": rec.get("size"),
                                         "path": full, "url": url, "name": rec.get("name"),
                                         "head": rec.get("head"), "tail": rec.get("tail")})
                    self.taken_paths[path] = canonical_url(url)
        self.log("resumed: %d files already in state", len(self.state))

    def save_state(self) -> None:
        os.makedirs(self.report_root, exist_ok=True)
        payload = {"site": self.site.slug, "updated": utcnow(), "files": self.state}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    # -- download ---------------------------------------------------------
    def clone(self) -> Dict[str, object]:
        os.makedirs(self.files_root, exist_ok=True)
        os.makedirs(self.report_root, exist_ok=True)
        self.load_state()
        entries = self.crawl()
        if self.site.max_files:
            entries = entries[: self.site.max_files]
        if self.dry_run:
            for entry in entries:
                self._record(entry, status="dry_run")
            self.candidates = self._candidate_duplicates(entries)
            self.log("%d duplicate candidates by (size, name) — hashes only after download",
                     len(self.candidates))
            return self._summary(len(entries), 0, 0)
        pending = [e for e in entries if self._needs_fetch(e)]
        if self.range_probe:
            # Names that occur more than once in this listing (or already exist in
            # the index) are the likely duplicates. For those alone we look up the
            # remote size first, so that the byte probe can collapse them with two
            # 64 KiB range requests instead of a full download.
            counts: Dict[str, int] = {}
            for entry in pending:
                if entry.size is None:
                    key = name_key(entry.name)
                    counts[key] = counts.get(key, 0) + 1
            self._risky_names = {k for k, n in counts.items() if n > 1}
            self._risky_names |= self.dedup.known_name_keys()
        pending_keys = {canonical_url(e.url) for e in pending}
        for entry in entries:
            if canonical_url(entry.url) not in pending_keys:
                prev = self.state.get(canonical_url(entry.url)) or {}
                status = "duplicate" if prev.get("duplicate_of") else "unchanged"
                self._record(entry, status=status, local_path=prev.get("local_path"),
                             size=prev.get("size") or entry.size, sha256=prev.get("sha256"),
                             remote_mtime=prev.get("remote_mtime"),
                             dupe_of=prev.get("duplicate_of"),
                             dupe_method=prev.get("dedupe_method"))
        self.log("%d documents queued (%d already complete)", len(pending),
                 len(entries) - len(pending))
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {pool.submit(self._fetch_one, entry): entry for entry in pending}
            for done, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                entry = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - never lose the crawl
                    LOG.error("[%s] unexpected error on %s: %s", self.site.slug, entry.url, exc)
                    self._record(entry, status="error", error="unexpected: {}".format(exc))
                if done % 25 == 0:
                    self.log("progress %d/%d", done, len(pending))
        elapsed = time.time() - started
        self.save_state()
        return self._summary(len(entries), len(pending), elapsed)

    @staticmethod
    def _candidate_duplicates(entries: List[Entry]) -> List[Dict[str, object]]:
        """Pre-download heuristic: identical listed size + near-identical name.

        Only a *hint* — the authoritative de-duplication happens after download
        by SHA-256 (and the range probe). Useful for sizing a mirror before
        paying for the bandwidth, and for spotting 'file.pdf' vs 'file1.pdf'
        republication patterns that are endemic in Brazilian portals.
        """
        import difflib

        def stem(name: str) -> str:
            base = name.rsplit("/", 1)[-1].lower()
            base = re.sub(r"\.(pdf|zip|csv|dwg|jpg|png|docx?|xlsx?)$", "", base)
            base = re.sub(r"[^a-z0-9]+", "", base)
            return re.sub(r"\d+$", "", base)      # drop trailing revision digits

        by_size: Dict[int, List[Entry]] = {}
        for entry in entries:
            if entry.size is None:
                continue
            by_size.setdefault(int(entry.size), []).append(entry)
        rows: List[Dict[str, object]] = []
        for size, members in sorted(by_size.items(), key=lambda kv: -kv[0]):
            if len(members) < 2 or len(members) > 50:
                continue
            members = sorted(members, key=lambda e: (len(e.name), e.name))
            for i, keeper in enumerate(members[:-1]):
                keeper_stem = stem(keeper.name)
                if not keeper_stem:
                    continue
                for other in members[i + 1:]:
                    other_stem = stem(other.name)
                    if not other_stem:
                        continue
                    ratio = difflib.SequenceMatcher(None, keeper_stem, other_stem).ratio()
                    if (other_stem == keeper_stem or other_stem in keeper_stem
                            or keeper_stem in other_stem or ratio >= 0.6):
                        rows.append({
                            "group": "{}:{}".format(size, keeper.name),
                            "size": size, "name_similarity": round(ratio, 3),
                            "kept_url": keeper.url, "candidate_url": other.url,
                            "kept_name": keeper.name, "candidate_name": other.name,
                            "reason": "same listed size + similar name (verify by hash)",
                        })
        return rows

    def _needs_fetch(self, entry: Entry) -> bool:
        prev = self.state.get(canonical_url(entry.url))
        if not prev or self.refresh:
            return True
        if prev.get("duplicate_of"):
            return False        # known duplicate: nothing to download, still reported
        full = os.path.join(self.files_root, str(prev.get("local_path") or ""))
        if not os.path.exists(full) or os.path.getsize(full) != int(prev.get("size") or -1):
            return True
        listed_size = entry.size
        was_listed = prev.get("listed_size", prev.get("size"))
        if (listed_size is not None and was_listed is not None
                and int(was_listed) != int(listed_size)):
            return True         # the remote document changed -> refetch
        if entry.mtime and prev.get("remote_mtime") and entry.mtime != prev["remote_mtime"]:
            return True
        return False

    def _fetch_one(self, entry: Entry) -> None:
        state_key = canonical_url(entry.url)
        prev = self.state.get(state_key) or {}
        if not self.refresh:
            if prev.get("duplicate_of"):
                self._record(entry, status="duplicate", size=prev.get("size"),
                             sha256=prev.get("sha256"), local_path=prev.get("local_path"),
                             dupe_of=prev.get("duplicate_of"),
                             dupe_method=prev.get("dedupe_method"))
                return
            if not self._needs_fetch(entry):
                self._record(entry, status="unchanged", local_path=prev.get("local_path"),
                             size=prev.get("size") or entry.size, sha256=prev.get("sha256"),
                             remote_mtime=prev.get("remote_mtime"))
                return
        rel = str(prev["local_path"]) if prev.get("local_path") else self.relpath(entry.url)
        if (entry.size is None and self.range_probe
                and name_key(entry.name) in self._risky_names):
            # the listing gives no size, but this name is a known repeat: learn
            # the size cheaply so the probe (and the size gate) can do their job
            size_hint = self._remote_size(entry)
            if size_hint:
                entry.size = size_hint
        if not self._check_budget(entry, rel, self.site.max_file_bytes):
            return
        dest = os.path.join(self.files_root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        written = 0
        headers: Dict[str, str] = {}
        digest = ""
        t0 = time.time()
        try:
            # The size gate covers probe *and* index registration, so a second
            # same-sized document always sees the first one and is collapsed
            # with two 64 KiB range requests instead of a full download.
            with self._size_gate(entry.size):
                probe = self._range_probe(entry)
                if probe is not None:
                    self._handle_duplicate(entry, rel, probe, method="range-probe")
                    return
                written, headers, digest = self._download(
                    entry, tmp, self.site.max_file_bytes, rel)
                if written == 0:
                    self._cleanup(tmp)
                    self._record(entry, status="skipped", local_path=rel,
                                 error="empty response body")
                    return
                os.replace(tmp, dest)
                self.bytes_downloaded += written
                head_hash, tail_hash = fingerprint(dest, written)
                kept = self.dedup.claim({"sha256": digest, "size": written, "path": dest,
                                         "url": entry.url, "name": entry.name,
                                         "head": head_hash, "tail": tail_hash})
                if kept is not None and _same_file(str(kept.get("path")), dest):
                    # the listing claimed a different size but the bytes are the
                    # ones we already had: refresh the bookkeeping, keep the file
                    self._cleanup(tmp)
                    prev["size"] = written
                    prev["listed_size"] = entry.size
                    prev["sha256"] = digest
                    prev["head"], prev["tail"] = head_hash, tail_hash
                    prev["content_type"] = headers.get("Content-Type")
                    prev["etag"] = headers.get("ETag")
                    prev["last_modified"] = headers.get("Last-Modified")
                    prev["fetched_at"] = utcnow()
                    self.state[state_key] = prev
                    self.log("unchanged (listed size %s, stored %s) %s",
                             entry.size, written, rel)
                    self._record(entry, status="unchanged", local_path=rel, size=written,
                                 sha256=digest, remote_mtime=entry.mtime)
                    return
                if kept is not None:
                    if not _same_file(str(kept.get("path")), dest):
                        self._cleanup(dest)
                    self._handle_duplicate(entry, rel, kept, method="sha256")
                    return
                self.state[state_key] = {
                    "local_path": rel, "size": written, "sha256": digest, "name": entry.name,
                    "remote_mtime": entry.mtime,
                    "etag": headers.get("ETag"),
                    "last_modified": headers.get("Last-Modified"),
                    "content_type": headers.get("Content-Type"),
                    "head": head_hash, "tail": tail_hash, "fetched_at": utcnow(),
                    "listed_size": entry.size,
                }
        except _SkipRequest:
            self._cleanup(tmp)
            return
        except FetchError as exc:
            self._cleanup(tmp)
            self._record(entry, status="error", error=str(exc), http_status=exc.status)
            return
        except OSError as exc:
            self._cleanup(tmp)
            self._record(entry, status="error", error="local I/O: {}".format(exc))
            return
        self._record(entry, status="downloaded", local_path=rel, size=written,
                     sha256=digest, remote_mtime=entry.mtime,
                     content_type=headers.get("Content-Type"),
                     duration_s=round(time.time() - t0, 3))
        self.log("saved %s (%s)", rel, human(written))

    def _download(self, entry: Entry, tmp: str, max_bytes: Optional[int],
                  budget_rel: str = "") -> Tuple[int, Dict[str, str], str]:
        """Stream one document to *tmp*; returns (bytes, headers, sha256).

        The size advertised by the listing is not trusted: the same limits are
        checked again against the real response, first from its headers and
        then from the running byte count.
        """
        sha = hashlib.sha256()
        written = 0
        with self._open_stream(entry.url) as stream:
            status = stream.status
            if status not in (200, 206):
                raise FetchError(entry.url, "HTTP {}".format(status), status)
            total = _int_or_none(stream.headers.get("Content-Length"))
            if entry.size is not None:
                # the listing size was already reserved in _check_budget
                if not self._budget_head(entry, total, budget_rel, reserve=False):
                    raise _SkipRequest()
            elif not self._budget_head(entry, total, budget_rel, reserve=True):
                raise _SkipRequest()
            # how much of this download the budget already accounted for: the
            # listed size (reserved by _check_budget) or, when the listing
            # published none, the response length (reserved just above). A short
            # reservation must not let the stream run past --max-total-bytes, so
            # every byte beyond it is charged before it is written.
            base = entry.size if entry.size is not None else total
            reserved = int(base or 0)
            charged = 0
            with open(tmp, "wb") as fh:
                while True:
                    buf = stream.read(CHUNK)
                    if not buf:
                        break
                    written += len(buf)
                    if max_bytes and written > max_bytes:
                        self._record(entry, status="skipped", local_path=budget_rel,
                                     error="exceeded max_file_bytes mid-stream")
                        raise _SkipRequest()
                    excess = written - reserved
                    if excess > charged:
                        if not self.budget.extend(excess - charged):
                            self._record(entry, status="skipped", local_path=budget_rel,
                                         error="total byte budget exhausted ({})".format(
                                             human(self.budget.limit)))
                            raise _SkipRequest()
                        charged = excess
                    sha.update(buf)
                    fh.write(buf)
            headers = dict(stream.headers)
        return written, headers, sha.hexdigest()

    def _budget_head(self, entry: Entry, total: Optional[int], rel: str,
                     reserve: bool = True) -> bool:
        """Check the limits against the real response headers."""
        max_bytes = self.site.max_file_bytes
        if max_bytes and total and total > max_bytes:
            self._record(entry, status="skipped", local_path=rel,
                         error="larger than max_file_bytes ({})".format(human(max_bytes)))
            return False
        if reserve and not self.budget.take(total):
            self._record(entry, status="skipped", local_path=rel,
                         error="total byte budget exhausted ({})".format(
                             human(self.budget.limit)))
            return False
        return True

    def _check_budget(self, entry: Entry, rel: str, max_bytes: Optional[int]) -> bool:
        """False (with a record) when a limit forbids storing this document."""
        if max_bytes and entry.size is not None and entry.size > max_bytes:
            self._record(entry, status="skipped", local_path=rel,
                         error="larger than max_file_bytes ({})".format(human(max_bytes)))
            return False
        if entry.size is not None and not self.budget.take(entry.size):
            self._record(entry, status="skipped", local_path=rel,
                         error="total byte budget exhausted ({})".format(
                             human(self.budget.limit)))
            return False
        return True

    def _remote_size(self, entry: Entry) -> Optional[int]:
        """Total size of a remote object, from a one-byte range request."""
        try:
            with self._open_stream(entry.url, headers={"Range": "bytes=0-0"}) as stream:
                content_range = stream.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)\s*$", content_range)
                if m:
                    stream.read(1)
                    return int(m.group(1))
                if stream.status == 200:
                    total = _int_or_none(stream.headers.get("Content-Length"))
                    stream.read(1)
                    return total
        except FetchError:
            return None
        return None

    def _size_gate(self, size: Optional[int]):
        """Serialize downloads that share a listed size (likely duplicates).

        Cheap insurance: without it, two identical 500 MiB files could be
        fetched in parallel and only collapsed afterwards; with it, the second
        one waits, sees the first in the index, and downloads ~128 KiB instead.
        """
        if not self.range_probe or size is None or int(size) < 4 * PROBE_BYTES:
            return contextlib.nullcontext()
        with self._gate_lock:
            lock = self._gates.setdefault(int(size), threading.Lock())
        return lock

    def _range_probe(self, entry: Entry) -> Optional[Dict[str, object]]:
        """Return the identical known copy when head+tail match, else None.

        Two single-range requests (64 KiB each) are enough to recognise a
        duplicate before paying for the whole body; servers that ignore Range
        (HTTP 200) simply fall through to the normal download path.
        """
        if not self.range_probe or entry.size is None or entry.size < 4 * PROBE_BYTES:
            return None
        candidates = [c for c in self.dedup.candidates_by_size(entry.size)
                      if c.get("head") and c.get("tail")]
        if not candidates:
            return None
        edges: List[bytes] = []
        for spec in ("bytes=0-{}".format(PROBE_BYTES - 1), "bytes=-{}".format(PROBE_BYTES)):
            try:
                with self._open_stream(entry.url, headers={"Range": spec}) as stream:
                    if stream.status != 206:
                        return None
                    edges.append(stream.read(PROBE_BYTES))
            except FetchError:
                return None
        if len(edges) != 2 or len(edges[0]) < PROBE_BYTES or len(edges[1]) < PROBE_BYTES:
            return None
        head_fp = hashlib.sha256(edges[0]).hexdigest()
        tail_fp = hashlib.sha256(edges[1]).hexdigest()
        for cand in candidates:
            if cand.get("head") == head_fp and cand.get("tail") == tail_fp:
                return cand
        return None

    def _handle_duplicate(self, entry: Entry, rel: str, kept: Dict[str, object],
                          method: str) -> None:
        kept_path = str(kept.get("path"))
        dest = os.path.join(self.files_root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if _same_file(kept_path, dest):
            # the "duplicate" is this very file: nothing to do but say so
            self._record(entry, status="unchanged", local_path=rel,
                         size=kept.get("size"), sha256=kept.get("sha256"))
            return
        strategy = self.dupe_strategy
        action, stored = "reported (not stored)", False
        if strategy == "hardlink":
            self._cleanup(dest)
            try:
                os.link(kept_path, dest)
                action, stored = "stored as hardlink", True
            except OSError as exc:
                LOG.debug("hardlink failed (%s); keeping a report only", exc)
        elif strategy == "copy":
            self._cleanup(dest)
            shutil.copy2(kept_path, dest)
            action, stored = "stored as copy", True
        elif strategy == "skip":
            action = "skipped (duplicate content)"
        # 'report' stores nothing: the audit trail carries the information
        self.dedup.note_hit(method, int(kept.get("size") or 0))
        self.collapsed_bytes += int(kept.get("size") or 0)
        self.duplicates.append({
            "sha256": kept.get("sha256"), "size": kept.get("size"),
            "duplicate_url": entry.url,
            "duplicate_path": rel if stored else None,
            "kept_url": kept.get("url"),
            "kept_path": os.path.relpath(kept_path, self.files_root).replace(os.sep, "/"),
            "method": method, "action": action,
        })
        self.state[canonical_url(entry.url)] = {
            "local_path": rel if stored else None, "size": kept.get("size"),
            "listed_size": entry.size,
            "sha256": kept.get("sha256"), "name": entry.name, "remote_mtime": entry.mtime,
            "duplicate_of": kept.get("url"), "dedupe_method": method,
            "fetched_at": utcnow(),
        }
        self._record(entry, status="duplicate", local_path=rel if stored else None,
                     size=kept.get("size"), sha256=kept.get("sha256"),
                     remote_mtime=entry.mtime, dupe_of=kept.get("url"),
                     dupe_method=method)
        self.log("duplicate (%s) %s%s == %s", method, rel,
                 "" if stored else " [not stored]",
                 os.path.relpath(kept_path, self.files_root))

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _record(self, entry: Entry, **fields) -> None:
        row = {"url": entry.url, "name": entry.name, "size": entry.size,
               "remote_mtime": entry.mtime, "status": "pending", "local_path": None,
               "sha256": None, "dupe_of": None, "dupe_method": None,
               "http_status": None, "content_type": None, "error": None,
               "duration_s": None}
        row.update({k: v for k, v in fields.items() if v is not None or k in ("local_path",)})
        with self._rec_lock:
            self.records.append(row)

    def _summary(self, discovered: int, pending: int, elapsed: float) -> Dict[str, object]:
        by_status: Dict[str, int] = {}
        for rec in self.records:
            by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        unique: Dict[str, int] = {}
        for rec in self.state.values():
            if rec.get("sha256") and rec.get("local_path"):
                unique[str(rec["sha256"])] = int(rec.get("size") or 0)
        return {
            "site": self.site.slug, "url": self.origin_url,
            "entry_urls": self.site.entry_urls, "title": self.site.title,
            "note": self.site.note,
            "started": dt.datetime.fromtimestamp(self.started, dt.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished": utcnow(),
            "elapsed_s": round(elapsed, 2),
            "listing_pages": len(self.visited),
            "documents_discovered": discovered,
            "documents_fetched": pending,
            "documents_bytes": sum(unique.values()),
            "bytes_downloaded_this_run": self.bytes_downloaded,
            "duplicates": len(self.duplicates),
            "duplicate_bytes_collapsed": self.collapsed_bytes,
            "candidate_duplicates": len(self.candidates),
            "errors": len(self.errors) + by_status.get("error", 0),
            "status_counts": by_status,
            "dry_run": self.dry_run,
        }


class ByteBudget:
    """Shared ceiling for total downloaded bytes across sites."""

    def __init__(self, limit: Optional[int]):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self, size: Optional[int]) -> bool:
        """Reserve *size* bytes (None = unknown yet, the stream accounts later)."""
        if self.limit is None or size is None:
            return True
        with self._lock:
            if self.used + size > self.limit:
                return False
            self.used += size
            return True

    def extend(self, delta: int) -> bool:
        """Account for *delta* bytes read from a response of unknown length."""
        if self.limit is None:
            return True
        with self._lock:
            if self.used + delta > self.limit:
                return False
            self.used += delta
            return True

    def remaining(self) -> Optional[int]:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    def __repr__(self) -> str:
        return "ByteBudget(limit={!r}, used={})".format(self.limit, self.used)


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

MANIFEST_FIELDS = ["url", "local_path", "name", "size", "sha256", "status",
                   "remote_mtime", "http_status", "content_type", "dupe_of",
                   "dupe_method", "duration_s", "error"]


def write_reports(cloner: SiteCloner, summary: Dict[str, object]) -> None:
    reports = cloner.report_root
    os.makedirs(reports, exist_ok=True)

    manifest = {
        "generated_at": utcnow(),
        "tool": "indexclone {}".format(__version__),
        "summary": summary,
        "files": sorted(cloner.records, key=lambda r: str(r.get("local_path") or r["url"])),
    }
    _write_json(os.path.join(reports, "manifest.json"), manifest)
    _write_json(os.path.join(reports, "summary.json"), summary)

    with open(os.path.join(reports, "manifest.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rec in manifest["files"]:
            writer.writerow(rec)

    with open(os.path.join(reports, "duplicates.csv"), "w", newline="", encoding="utf-8") as fh:
        fields = ["sha256", "size", "kept_url", "kept_path", "duplicate_url",
                  "duplicate_path", "method", "action"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in cloner.duplicates:
            writer.writerow(row)

    if cloner.candidates:
        with open(os.path.join(reports, "candidate_duplicates.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, extrasaction="ignore", fieldnames=[
                "group", "size", "name_similarity", "kept_name", "kept_url",
                "candidate_name", "candidate_url", "reason"])
            writer.writeheader()
            for row in cloner.candidates:
                writer.writerow(row)

    with open(os.path.join(reports, "errors.log"), "w", encoding="utf-8") as fh:
        for row in cloner.errors:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        for rec in cloner.records:
            if rec.get("error"):
                fh.write(json.dumps({"url": rec["url"], "stage": "download",
                                     "error": rec["error"]}, ensure_ascii=False) + "\n")

    _write_site_readme(cloner, summary)


def _write_json(path: str, payload: object) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _write_site_readme(cloner: SiteCloner, summary: Dict[str, object]) -> None:
    counts = summary.get("status_counts", {})
    lines = [
        "# {}".format(summary.get("title") or cloner.site.slug),
        "",
        "* source listing: <{}>".format(summary.get("url")),
        "* output folder: `{}`".format(summary.get("site")),
        "* cloned at: {}".format(summary.get("finished")),
        "* documents discovered: **{}** ({} fetched this run, {} unique bytes stored)".format(
            summary.get("documents_discovered"), summary.get("documents_fetched"),
            human(summary.get("documents_bytes"))),
        "* downloaded in this run: {}".format(
            human(summary.get("bytes_downloaded_this_run"))),
        "* duplicates collapsed: **{}** ({})".format(
            summary.get("duplicates"), human(summary.get("duplicate_bytes_collapsed"))),
        "* status: {}".format(", ".join("{}={}".format(k, v) for k, v in sorted(counts.items()))
                              or "n/a"),
        "",
        "## Layout",
        "",
        "```",
        "{}/".format(cloner.site.slug),
        "├── files/                 # the mirrored documents (mirror of the listing tree)",
        "├── _index/pages/          # copy of every listing page as fetched (audit trail)",
        "└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json",
        "```",
        "",
        "## Notes",
        "",
        cloner.site.note or "_(none)_",
        "",
        "Re-run / resume with:",
        "",
        "```bash",
        cloner.command or "python3 scraper.py --config sites.json --site {}".format(
            cloner.site.slug),
        "```",
        "",
    ]
    with open(os.path.join(cloner.root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_global_report(out_root: str, summaries: List[Dict[str, object]],
                        dedup: DedupIndex, duplicates_by_site: Dict[str, List[Dict[str, object]]]) -> None:
    """Cross-site roll-up: proves the same bytes were published by several bodies."""
    reports = os.path.join(out_root, "_reports")
    os.makedirs(reports, exist_ok=True)
    by_hash: Dict[str, List[Dict[str, object]]] = {}
    for slug, rows in duplicates_by_site.items():
        for row in rows:
            by_hash.setdefault(str(row["sha256"]), []).append({"site": slug, **row})
    cross = {h: rows for h, rows in by_hash.items()
             if len({r["site"] for r in rows}) > 1 or True}  # every collapsed group
    total_saved = sum(int(r.get("size") or 0) for rows in duplicates_by_site.values() for r in rows)
    payload = {
        "generated_at": utcnow(),
        "tool": "indexclone {}".format(__version__),
        "sites": summaries,
        "totals": {
            "sites": len(summaries),
            "documents_discovered": sum(int(s.get("documents_discovered") or 0) for s in summaries),
            "documents_fetched": sum(int(s.get("documents_fetched") or 0) for s in summaries),
            "bytes_downloaded": sum(int(s.get("documents_bytes") or 0) for s in summaries),
            "duplicates_collapsed": sum(int(s.get("duplicates") or 0) for s in summaries),
        "candidate_duplicates": sum(int(s.get("candidate_duplicates") or 0) for s in summaries),
            "bytes_saved_by_dedupe": total_saved,
            "errors": sum(int(s.get("errors") or 0) for s in summaries),
        },
        "duplicate_groups": cross,
    }
    _write_json(os.path.join(reports, "global.json"), payload)

    with open(os.path.join(reports, "duplicates_all_sites.csv"), "w", newline="",
              encoding="utf-8") as fh:
        fields = ["site", "sha256", "size", "kept_url", "duplicate_url",
                  "duplicate_path", "method", "action"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for slug in sorted(duplicates_by_site):
            for row in duplicates_by_site[slug]:
                writer.writerow({"site": slug, **row})

    lines = [
        "# Global clone report",
        "",
        "| site | documents | fetched | downloaded | duplicates | bytes saved | errors |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in summaries:
        lines.append("| {site} | {documents_discovered} | {documents_fetched} | {bytes} | "
                     "{duplicates} | {saved} | {errors} |".format(
                         site=s.get("site"), documents_discovered=s.get("documents_discovered"),
                         documents_fetched=s.get("documents_fetched"),
                         bytes=human(s.get("documents_bytes")),
                         duplicates=s.get("duplicates"),
                         saved=human(s.get("duplicate_bytes_collapsed")),
                         errors=s.get("errors")))
    totals = payload["totals"]
    lines += ["",
              "**Totals**: {documents_discovered} documents, {bytes} downloaded, "
              "{duplicates} duplicates collapsed ({saved} saved), {errors} errors.".format(
                  documents_discovered=totals["documents_discovered"],
                  bytes=human(totals["bytes_downloaded"]),
                  duplicates=totals["duplicates_collapsed"],
                  saved=human(totals["bytes_saved_by_dedupe"]),
                  errors=totals["errors"]),
              ""]
    with open(os.path.join(reports, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_config(path: str) -> Tuple[List[SiteConfig], Dict[str, object]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return [SiteConfig.from_dict(item) for item in data], {}
    defaults = {k: v for k, v in (data.get("defaults") or {}).items()}
    sites = [SiteConfig.from_dict(item).merged(defaults) for item in data.get("sites", [])]
    return sites, defaults


def parse_rewrites(values: Sequence[str]) -> List[Tuple[str, str]]:
    out = []
    for value in values or []:
        if "=" not in value:
            raise SystemExit("--rewrite expects OLD_PREFIX=NEW_PREFIX, got {!r}".format(value))
        old, _, new = value.partition("=")
        out.append((old, new))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper.py",
        description="Clone + de-duplicate public directory listings (one folder per site).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Examples:\n"
               "  python3 scraper.py --config sites.json --all\n"
               "  python3 scraper.py --url https://example.gov/dados/ --slug example --jobs 8\n"
               "  python3 scraper.py --config sites.json --all --dry-run\n")
    src = p.add_argument_group("what to clone")
    src.add_argument("--config", help="JSON file with a list of sites (see sites.json)")
    src.add_argument("--all", action="store_true", help="clone every site from --config")
    src.add_argument("--site", action="append", default=[], metavar="SLUG",
                     help="only this slug (repeatable)")
    src.add_argument("--url", help="ad-hoc listing URL")
    src.add_argument("--slug", help="folder name for --url")
    src.add_argument("--list", action="store_true", help="list configured sites and exit")
    src.add_argument("--rerun-hint", metavar="TEXT",
                     help="command recorded in each site README instead of this "
                          "actual argv ({slug} is replaced by the site slug); for "
                          "wrapper tools whose own paths are temporary")
    src.add_argument("--rewrite", action="append", default=[], metavar="OLD=NEW",
                     help="fetch OLD through NEW (for replica/test servers); reports "
                          "keep the original URLs")
    out = p.add_argument_group("output")
    out.add_argument("--out", default="outputs", help="root folder for the per-site clones")
    out.add_argument("--dupe-strategy", choices=["hardlink", "copy", "report", "skip"],
                     default="hardlink",
                     help="what to do with a file whose content is already stored")
    out.add_argument("--keep-index-pages", action="store_true", default=True,
                     help="store copies of the listing pages under _index/pages")
    out.add_argument("--no-index-pages", dest="keep_index_pages", action="store_false")
    net = p.add_argument_group("network")
    net.add_argument("--jobs", type=int, default=4, help="parallel downloads")
    net.add_argument("--rate", type=float, default=2.0, help="requests/second per host")
    net.add_argument("--timeout", type=float, default=60.0, help="socket timeout (s)")
    net.add_argument("--retries", type=int, default=3, help="attempts per request")
    net.add_argument("--user-agent", default=DEFAULT_UA)
    net.add_argument("--ignore-robots", action="store_true",
                     help="do not honour robots.txt (only for sites you are allowed to mirror)")
    net.add_argument("--no-range-probe", dest="range_probe", action="store_false", default=True,
                     help="disable the cheap byte-probe for huge duplicates")
    lim = p.add_argument_group("limits / filters")
    lim.add_argument("--max-depth", type=int, help="max directory recursion depth")
    lim.add_argument("--max-files", type=int, help="max documents per site")
    lim.add_argument("--max-file-bytes", type=int, help="skip documents bigger than this")
    lim.add_argument("--max-total-bytes", type=int, help="stop after this many bytes")
    lim.add_argument("--include", help="only URLs matching this regex")
    lim.add_argument("--exclude", help="skip URLs matching this regex")
    run = p.add_argument_group("run mode")
    run.add_argument("--dry-run", action="store_true",
                     help="crawl and report, do not download payloads")
    run.add_argument("--refresh", action="store_true", help="re-download even if unchanged")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("--version", action="version", version="indexclone {}".format(__version__))
    return p


def configure_logging(quiet: bool, verbose: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.quiet, args.verbose)

    sites: List[SiteConfig] = []
    if args.url:
        slug = args.slug or re.sub(r"[^a-z0-9]+", "_",
                                   urllib.parse.urlsplit(args.url).netloc.lower()).strip("_")
        sites.append(SiteConfig(slug=slug, url=args.url, title=args.url))
    if args.config:
        cfg_sites, _ = load_config(args.config)
        if args.site:
            wanted = set(args.site)
            cfg_sites = [s for s in cfg_sites if s.slug in wanted]
            missing = wanted - {s.slug for s in cfg_sites}
            if missing:
                LOG.error("unknown slug(s): %s", ", ".join(sorted(missing)))
                return 2
        elif not args.all and not args.url:
            LOG.info("config has %d sites; pass --all or --site SLUG", len(cfg_sites))
            for s in cfg_sites:
                print("  {:32} {}".format(s.slug, s.url))
            return 0
        sites.extend(cfg_sites)
    if args.list:
        for s in sites:
            print("  {:32} {}".format(s.slug, s.url))
        return 0
    if not sites:
        build_parser().print_help()
        return 2

    # CLI overrides
    for site in sites:
        if args.max_depth is not None:
            site.max_depth = args.max_depth
        if args.max_files is not None:
            site.max_files = args.max_files
        if args.max_file_bytes is not None:
            site.max_file_bytes = args.max_file_bytes
        if args.include:
            site.include = args.include
        if args.exclude:
            site.exclude = args.exclude

    fetcher = Fetcher(user_agent=args.user_agent, timeout=args.timeout,
                      retries=args.retries, rate=args.rate,
                      respect_robots=not args.ignore_robots)
    dedup = DedupIndex(enabled=True)
    budget = ByteBudget(args.max_total_bytes)
    rewrites = parse_rewrites(args.rewrite)
    os.makedirs(args.out, exist_ok=True)

    summaries: List[Dict[str, object]] = []
    duplicates_by_site: Dict[str, List[Dict[str, object]]] = {}
    interrupted = {"flag": False}

    def _on_sigint(signum, frame):  # noqa: ANN001
        if interrupted["flag"]:
            raise SystemExit(130)
        interrupted["flag"] = True
        LOG.warning("interrupted — finishing reports for completed work")

    def command_for(site: SiteConfig) -> str:
        """The command line that reproduces this site's run, for its README."""
        if args.rerun_hint:
            # plain replace: the hint is arbitrary shell text, braces and all
            return args.rerun_hint.replace("{slug}", site.slug)
        parts = ["python3 scraper.py"]
        if args.config:
            parts += ["--config", args.config]
        parts += ["--site", site.slug, "--out", args.out, "--jobs", str(args.jobs)]
        if args.rate != 2.0:
            parts += ["--rate", str(args.rate)]
        for old, new in rewrites:
            parts += ["--rewrite", "{}={}".format(old, new)]
        for flag, value in (("--dupe-strategy", args.dupe_strategy),
                            ("--max-files", args.max_files),
                            ("--max-file-bytes", args.max_file_bytes),
                            ("--max-total-bytes", args.max_total_bytes),
                            ("--include", args.include), ("--exclude", args.exclude)):
            if value not in (None, ""):
                parts += [flag, str(value)]
        if args.dry_run:
            parts.append("--dry-run")
        if args.refresh:
            parts.append("--refresh")
        if not args.keep_index_pages:
            parts.append("--no-index-pages")
        if args.ignore_robots:
            parts.append("--ignore-robots")
        return " ".join(parts)

    signal.signal(signal.SIGINT, _on_sigint)

    exit_code = 0
    for site in sites:
        LOG.info("=== %s (%s)", site.slug, site.url)
        cloner = SiteCloner(site=site, out_root=args.out, fetcher=fetcher, dedup=dedup,
                            jobs=args.jobs, dry_run=args.dry_run, refresh=args.refresh,
                            dupe_strategy=args.dupe_strategy,
                            max_total_bytes=args.max_total_bytes,
                            save_index_pages=args.keep_index_pages,
                            range_probe=args.range_probe, budget=budget,
                            rewrite=rewrites, command=command_for(site))
        try:
            summary = cloner.clone()
        except Exception as exc:  # noqa: BLE001 - one broken site must not kill the run
            LOG.exception("[%s] fatal: %s", site.slug, exc)
            summary = {"site": site.slug, "url": site.url, "fatal": str(exc),
                       "documents_discovered": 0, "documents_fetched": 0,
                       "documents_bytes": 0, "duplicates": 0, "errors": 1,
                       "duplicate_bytes_collapsed": 0, "status_counts": {},
                       "finished": utcnow()}
            exit_code = 2
        cloner.save_state()
        write_reports(cloner, summary)
        summaries.append(summary)
        duplicates_by_site[site.slug] = cloner.duplicates
        LOG.info("[%s] done: %s documents, %s downloaded, %s duplicates, %s errors",
                 site.slug, summary.get("documents_discovered"),
                 human(summary.get("documents_bytes")), summary.get("duplicates"),
                 summary.get("errors"))
        if summary.get("errors"):
            exit_code = exit_code or 1
        if interrupted["flag"]:
            break

    write_global_report(args.out, summaries, dedup, duplicates_by_site)
    LOG.info("wrote %s", os.path.join(args.out, "_reports", "global.json"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
