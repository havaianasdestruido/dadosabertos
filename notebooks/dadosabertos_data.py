# -*- coding: utf-8 -*-
"""Shared data access for the indexclone notebooks.

The notebooks in this folder read *only* what ``scraper.py`` already wrote to
``outputs/`` (the real listing inventory) and ``outputs-replica/`` (a full run
with real bytes over HTTP against the local replica).  Nothing here touches the
network and nothing here writes into the report folders -- the notebooks are
read-only consumers, so a run can never be corrupted by exploring it.

Typical use from a notebook::

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath("dadosabertos_data")))
    import dadosabertos_data as d

    inv = d.load_manifest("outputs")        # 127 discovered documents
    run = d.load_manifest("outputs-replica")  # 39 documents, actually fetched

Every loader adds a ``site`` column (the folder name) because the per-site
report files do not carry it themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import unquote

import pandas as pd

# --------------------------------------------------------------------------
# locating the repository
# --------------------------------------------------------------------------

#: marker file that identifies the checkout root
_ROOT_MARKER = "scraper.py"

#: report files produced by scraper.py in each ``<site>/_reports/`` folder
REPORT_FILES = (
    "manifest.csv",
    "manifest.json",
    "duplicates.csv",
    "candidate_duplicates.csv",
    "errors.log",
    "summary.json",
    "state.json",
)

#: the two committed result trees
TREES = ("outputs", "outputs-replica")


def repo_root() -> str:
    """Return the checkout root.

    Works whether the kernel was started from the repository root or from this
    ``notebooks/`` folder: we walk upwards from the current directory and from
    this module's own directory looking for ``scraper.py`` next to ``outputs/``.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for start in (os.getcwd(), here):
        path = os.path.abspath(start)
        while True:
            if os.path.isfile(os.path.join(path, _ROOT_MARKER)) and os.path.isdir(
                os.path.join(path, "outputs")
            ):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise RuntimeError(
        "could not locate the indexclone checkout (scraper.py + outputs/) "
        f"from cwd={os.getcwd()!r} or {here!r}"
    )


ROOT = repo_root()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

_BINARY_SUFFIXES = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(n) -> str:
    """Format a byte count the way the README does (``1.4 MiB``)."""
    if n is None or pd.isna(n):
        return "-"
    n = float(n)
    if n == 0:
        return "0 B"
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in _BINARY_SUFFIXES:
        if n < 1024 or unit == _BINARY_SUFFIXES[-1]:
            return f"{sign}{n:,.0f} {unit}" if unit == "B" else f"{sign}{n:,.1f} {unit}"
        n /= 1024.0
    return f"{sign}{n:,.1f} PiB"


def name_of(url_or_path) -> str:
    """Last segment of a URL or local path, percent-decoded.

    Report URLs keep their percent-encoding (``Plano_de_Capacita%C3%A7%C3%A3o_2019.pdf``)
    and local paths do not, so every label goes through here before it is shown.
    """
    return unquote(str(url_or_path).rsplit("/", 1)[-1])


_SIZE_STEPS = ((1024 ** 3, "1 GiB"), (1024 ** 2, "1 MiB"), (1024, "1 KiB"))
_SIZE_FRACTIONS = ((1024 ** 2 / 10, "0.1 MiB"), (1024 ** 2 / 100, "0.01 MiB"), (1024 / 10, "0.1 KiB"))


def size_granularity(n):
    """How precise can a *published* size possibly be? -> ``(step_bytes, label)``.

    Apache-style listings publish rounded strings — ``9M``, ``3.8M``, ``387K`` —
    and ``scraper.parse_size_token`` turns them into exact-looking byte counts
    (``9M`` -> 9437184). Two completely different files therefore end up with the
    *same* recorded size whenever they round to the same string.

    This returns the coarsest step that explains the value, i.e. the **best**
    precision the published size can have. Only meaningful for ``outputs/``,
    where sizes were parsed out of listing text; ``outputs-replica/`` sizes are
    real ``Content-Length`` bytes and are exact.
    """
    if n is None or pd.isna(n):
        return (None, "unknown")
    n = float(n)
    if n == 0:
        return (1.0, "exact bytes")
    for step, label in _SIZE_STEPS:
        if n >= step and abs(n / step - round(n / step)) < 1e-9:
            return (step, f"rounded to \u2264 {label}")
    for step, label in _SIZE_FRACTIONS:
        if n >= step and abs(n / step - round(n / step)) < 1e-6:
            return (step, f"rounded to ~{label}")
    return (1.0, "exact bytes")


def ext_of(name) -> str:
    """Lower-cased extension of a document name, ``""`` when there is none."""
    if not isinstance(name, str) or "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


#: Brazilian government domain suffixes to strip when making an axis label
_GOV_TAIL = {
    "br", "gov", "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma",
    "mg", "ms", "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr",
    "rs", "sc", "se", "sp", "to",
}
#: host labels that carry no identity ("dados.cvm.gov.br" is really CVM)
_GENERIC_HOST = {"dados", "dadosabertos", "geofiles", "www", "portal", "arquivos", "files"}
#: hand-shortened labels
_ALIAS = {"comissaodaverdade": "comissao"}


def short_site(site: str) -> str:
    """``dados.cvm.gov.br`` -> ``cvm`` -- a short label that fits on an axis."""
    parts = [p for p in site.split(".") if p]
    while len(parts) > 1 and parts[-1].lower() in _GOV_TAIL:
        parts.pop()
    if not parts:
        return site
    for p in parts:
        if p.lower() not in _GENERIC_HOST:
            return _ALIAS.get(p.lower(), p)
    return parts[0]


def sha256_of(path: str, chunk: int = 1 << 20):
    """SHA-256 of a stored file, or ``None`` when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


# --------------------------------------------------------------------------
# per-tree loaders
# --------------------------------------------------------------------------


def tree_path(tree: str = "outputs") -> str:
    """Absolute path of a result tree."""
    return os.path.join(ROOT, tree)


def site_dirs(tree: str = "outputs"):
    """Site folders inside a tree, skipping ``_reports``. -> ``[(site, path)]``"""
    base = tree_path(tree)
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name.startswith("_") or not os.path.isdir(path):
            continue
        if os.path.isdir(os.path.join(path, "_reports")):
            out.append((name, path))
    return out


def _read_csv(path: str) -> pd.DataFrame:
    """Read one of the report CSVs, tolerating an empty/headers-only file."""
    if not os.path.isfile(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    return df


def load_manifest(tree: str = "outputs") -> pd.DataFrame:
    """Every ``manifest.csv`` of a tree in one frame (plus ``site``/``ext``)."""
    frames = []
    for site, path in site_dirs(tree):
        df = _read_csv(os.path.join(path, "_reports", "manifest.csv"))
        if df.empty:
            continue
        df["site"] = site
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "size" in out:
        out["size"] = pd.to_numeric(out["size"], errors="coerce")
    if "duration_s" in out:
        out["duration_s"] = pd.to_numeric(out["duration_s"], errors="coerce")
    if "name" in out:
        out["ext"] = out["name"].map(ext_of)
    if "remote_mtime" in out:
        out["mtime"] = pd.to_datetime(out["remote_mtime"], errors="coerce", utc=True)
    return out


def load_summary(tree: str = "outputs") -> pd.DataFrame:
    """One row per site with the scalars of ``summary.json``."""
    rows = []
    for site, path in site_dirs(tree):
        p = os.path.join(path, "_reports", "summary.json")
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        row = {"site": site}
        for key in (
            "url",
            "listing_pages",
            "documents_discovered",
            "documents_fetched",
            "documents_bytes",
            "bytes_downloaded_this_run",
            "duplicates",
            "duplicate_bytes_collapsed",
            "candidate_duplicates",
            "errors",
            "elapsed_s",
            "dry_run",
            "started",
            "finished",
        ):
            if key in d:
                row[key] = d[key]
        for status, count in (d.get("status_counts") or {}).items():
            row[f"status_{status}"] = count
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.fillna(0) if not df.empty else df


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_global(tree: str = "outputs") -> dict:
    """The ``_reports/global.json`` roll-up of a tree."""
    p = os.path.join(tree_path(tree), "_reports", "global.json")
    return load_json(p) if os.path.isfile(p) else {}


def load_duplicates(tree: str = "outputs") -> pd.DataFrame:
    """Collapsed duplicates: ``duplicates.csv`` per site, plus the all-sites copy."""
    frames = []
    for site, path in site_dirs(tree):
        p = os.path.join(path, "_reports", "duplicates.csv")
        df = _read_csv(p)
        if not df.empty:
            df["site"] = site
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "size" in out:
        out["size"] = pd.to_numeric(out["size"], errors="coerce")
    return out


def load_candidates(tree: str = "outputs") -> pd.DataFrame:
    """Heuristic ``candidate_duplicates.csv`` rows (same size + similar name)."""
    frames = []
    for site, path in site_dirs(tree):
        p = os.path.join(path, "_reports", "candidate_duplicates.csv")
        df = _read_csv(p)
        if not df.empty:
            df["site"] = site
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "size" in out:
        out["size"] = pd.to_numeric(out["size"], errors="coerce")
    if "name_similarity" in out:
        out["name_similarity"] = pd.to_numeric(out["name_similarity"], errors="coerce")
    return out


def load_state(tree: str = "outputs") -> pd.DataFrame:
    """The resume/dedup index: one row per URL known to ``state.json``."""
    rows = []
    for site, path in site_dirs(tree):
        p = os.path.join(path, "_reports", "state.json")
        if not os.path.isfile(p):
            continue
        d = load_json(p)
        for url, rec in (d.get("files") or {}).items():
            row = {"site": site, "url": url}
            row.update(rec or {})
            rows.append(row)
    df = pd.DataFrame(rows)
    return df.fillna(pd.NA) if not df.empty else df


def duplicate_groups(tree: str = "outputs-replica") -> dict:
    """``global.json -> duplicate_groups``: sha256 -> list of collapse records."""
    return load_global(tree).get("duplicate_groups", {}) or {}


def report_inventory(tree: str = "outputs") -> pd.DataFrame:
    """A map of the report folder itself: which file exists, how big, how many rows."""
    rows = []
    for site, path in site_dirs(tree):
        reports = os.path.join(path, "_reports")
        for fname in REPORT_FILES:
            p = os.path.join(reports, fname)
            rows.append(
                {
                    "site": site,
                    "file": fname,
                    "exists": os.path.isfile(p),
                    "bytes": os.path.getsize(p) if os.path.isfile(p) else 0,
                }
            )
    df = pd.DataFrame(rows)
    # row counts for the tabular reports
    counts = {}
    for site, path in site_dirs(tree):
        for fname in ("manifest.csv", "duplicates.csv", "candidate_duplicates.csv"):
            counts[(site, fname)] = len(_read_csv(os.path.join(path, "_reports", fname)))
        st = os.path.join(path, "_reports", "state.json")
        counts[(site, "state.json")] = (
            len(load_json(st).get("files", {})) if os.path.isfile(st) else 0
        )
    df["rows"] = [counts.get((s, f), 0) for s, f in zip(df["site"], df["file"])]
    return df


def index_pages(tree: str = "outputs"):
    """The saved listing pages -- the audit trail of what the crawler saw."""
    out = []
    base = tree_path(tree)
    for dirpath, _dirnames, filenames in os.walk(base):
        if os.sep + "_index" + os.sep + "pages" not in dirpath:
            continue
        for fname in filenames:
            if fname.endswith(".html"):
                p = os.path.join(dirpath, fname)
                out.append(
                    {
                        "tree": tree,
                        "site": os.path.relpath(p, base).split(os.sep)[0],
                        "path": os.path.relpath(p, base),
                        "bytes": os.path.getsize(p),
                    }
                )
    return pd.DataFrame(out)


def stored_files(tree: str = "outputs-replica") -> pd.DataFrame:
    """Every file physically present under ``<site>/files/``."""
    rows = []
    for site, path in site_dirs(tree):
        files_root = os.path.join(path, "files")
        if not os.path.isdir(files_root):
            continue
        for dirpath, _dirnames, filenames in os.walk(files_root):
            for fname in filenames:
                p = os.path.join(dirpath, fname)
                rows.append(
                    {
                        "site": site,
                        "relpath": os.path.relpath(p, files_root),
                        "abs_path": p,
                        "disk_size": os.path.getsize(p),
                        "nlink": os.stat(p).st_nlink,
                        "inode": os.stat(p).st_ino,
                    }
                )
    return pd.DataFrame(rows)


def verify_against_disk(tree: str = "outputs-replica") -> pd.DataFrame:
    """Join the manifest against the bytes actually stored on disk.

    Returns one row per manifest entry that is supposed to be on disk, with
    ``on_disk``, ``size_matches`` and ``sha256_matches`` columns.  This is the
    check that turns "the report says it worked" into "the bytes are there".
    """
    man = load_manifest(tree)
    if man.empty or "local_path" not in man:
        return pd.DataFrame()
    rows = []
    for _, r in man.iterrows():
        rel = r.get("local_path")
        if not isinstance(rel, str) or not rel:
            continue
        p = os.path.join(tree_path(tree), r["site"], "files", rel)
        st = os.stat(p) if os.path.isfile(p) else None
        digest = sha256_of(p) if st is not None else None
        rows.append(
            {
                "site": r["site"],
                "name": r.get("name"),
                "local_path": rel,
                "status": r.get("status"),
                "manifest_size": r.get("size"),
                "disk_size": st.st_size if st else None,
                "manifest_sha256": r.get("sha256"),
                "disk_sha256": digest,
                "nlink": st.st_nlink if st else None,
                "inode": st.st_ino if st else None,
            }
        )
    df = pd.DataFrame(rows)
    df["on_disk"] = df["disk_size"].notna()
    df["size_matches"] = df["manifest_size"] == df["disk_size"]
    df["sha256_matches"] = (df["manifest_sha256"] == df["disk_sha256"]) & df[
        "manifest_sha256"
    ].notna()
    return df


# --------------------------------------------------------------------------
# plotting defaults
# --------------------------------------------------------------------------

#: one colour per site, stable across every notebook
PALETTE = {
    "dadosabertos.pgfn.gov.br": "#2b6cb0",
    "valiprev.sp.gov.br": "#2f855a",
    "geofiles.caxias.rs.gov.br": "#c05621",
    "macau.rn.gov.br": "#6b46c1",
    "comissaodaverdade.al.sp.gov.br": "#c53030",
    "dados.cvm.gov.br": "#b7791f",
}
_FALLBACK = ["#4c51bf", "#319795", "#d53f8c", "#dd6b20", "#38a169", "#805ad5"]


def colors_for(sites) -> list:
    """Pick a colour per site, falling back to a fixed cycle for unknown ones."""
    out, i = [], 0
    for s in sites:
        if s in PALETTE:
            out.append(PALETTE[s])
        else:
            out.append(_FALLBACK[i % len(_FALLBACK)])
            i += 1
    return out


def setup_plots():
    """Shared matplotlib styling so every notebook's figures look alike."""
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.dpi": 110,
            "savefig.dpi": 110,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.grid": True,
            "grid.alpha": 0.3,
            "grid.linestyle": ":",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 10,
            "font.size": 9,
            "figure.autolayout": False,
        }
    )
    return plt
