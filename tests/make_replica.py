#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the offline replica used by the test suite (and by the committed demo run).

The replica reproduces, next to each other, the directory-listing dialects the
six production sites use:

  replica/localhost/pgfn/       bullet-link index, no sizes/dates  (S3/CloudFront style)
  replica/localhost/valiprev/   Apache 2.4 table, truncated cells, ?C=N;O=D sort links
  replica/localhost/caxias/     nginx autoindex <pre>, no metadata, parent-dir link
  replica/localhost/macau/      plain link farm: years -> months -> PDFs, no metadata
  replica/localhost/comissao/   OpenDataSoft-style plain link list, latin-1 names
  replica/localhost/cvm/        Apache <pre> index with dates/sizes, nested year folders

Every payload is generated deterministically (hash stream), so the SHA-256
digests — and therefore the de-duplication results — are reproducible.
Several documents deliberately contain identical bytes so the deduper has
something to collapse.

Usage:  python3 tests/make_replica.py [--out tests/replica]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import shutil
import sys
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# deterministic payloads
# --------------------------------------------------------------------------


def blob(seed: str, size: int, magic: bytes = b"") -> bytes:
    """Deterministic pseudo-random payload of exactly *size* bytes."""
    out = bytearray()
    counter = 0
    magic = (magic + b" " * 32)[:32]
    out.extend(magic[: min(len(magic), size)])
    while len(out) < size:
        out.extend(hashlib.sha256("{}:{}".format(seed, counter).encode()).digest() * 8)
        counter += 1
    return bytes(out[:size])


def pdf(seed: str, size: int) -> bytes:
    body = blob(seed, max(size, 64), b"%PDF-1.4\n% fake pdf for tests\n")
    return body


def zipf(seed: str, size: int) -> bytes:
    return blob(seed, size, b"PK\x03\x04\x14\x00\x00\x00\x08\x00")


def csv(seed: str, size: int) -> bytes:
    rows = ["CNPJ;DENOM_SOCIAL;DATA_REG"]
    i = 0
    while sum(len(r) + 1 for r in rows) < size:
        rows.append("{}000000000{};FUNDO TESTE {};2025-01-{:02d}".format(i % 9, i, i, (i % 28) + 1))
        i += 1
    return ("\n".join(rows) + "\n").encode("utf-8")[:size]


def jpg(seed: str, size: int) -> bytes:
    return blob(seed, size, b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")


# --------------------------------------------------------------------------
# listing page generators (the dialects)
# --------------------------------------------------------------------------

PRE_TAG = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
 <head>
  <title>Index of {path}</title>
 </head>
 <body>
<h1>Index of {path}</h1>
  <ul>
  </ul>
<pre><img src="/icons/blank.gif" alt="Icon "> <a href="?C=N;O=D">Name</a>                    <a href="?C=M;O=A">Last modified</a>      <a href="?C=S;O=A">Size</a>  <a href="?C=D;O=A">Description</a><hr>
{rows}<hr></pre>
</body></html>
"""


def apache_pre(path: str, entries: List[Tuple[str, int, str]]) -> str:
    """entries: (name, size, mtime) — size 0 means directory."""
    rows = []
    parent = path.rstrip("/").rsplit("/", 1)[0] + "/" if path not in ("/", "/dados/") else "/"
    rows.append('<img src="/icons/back.gif" alt="[PARENTDIR]"> <a href="{}">Parent Directory</a>'
                '                             -   '.format(parent))
    for name, size, mtime in entries:
        icon = "folder.gif" if size == 0 else "compressed.gif"
        shown = "-" if size == 0 else human_size(size)
        href = urllib_quote(name) + "/" if size == 0 else urllib_quote(name)
        rows.append('<img src="/icons/{}" alt="[   ]"> <a href="{}">{}</a>  {}   {}'
                    .format(icon, href, html.escape(name), mtime, shown))
    return PRE_TAG.format(path=path, bullets="", rows="\n".join(rows) + "\n")


def human_size(size: int) -> str:
    for unit, div in (("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3)):
        if size >= div:
            return "{:.1f}{}".format(size / div, unit)
    return str(size)


def apache_table(path: str, entries: List[Tuple[str, int, str]], truncated: bool = True) -> str:
    """Apache 2.4 table dialect with a parent link, sort links and link titles."""
    rows = []
    i = 0
    for name, size, mtime in entries:
        i += 1
        display = name
        if truncated and len(name) > 22:
            display = name[:20] + "..&gt;"
        icon = "folder" if size == 0 else "file"
        shown = "-" if size == 0 else human_size(size)
        href = urllib_quote(name) + "/" if size == 0 else urllib_quote(name)
        rows.append(
            '  <tr><td class="{}"><img src="/icons/{}.gif" alt="[ ]"></td>'
            '<td><a href="{}" title="{}">{}</a></td>'
            '<td class="datetime" data-sort-value="{}">{}</td>'
            '<td class="size" data-sort-value="{}">{}</td>'
            '<td class="description">&nbsp;</td></tr>'.format(
                icon, icon, href, html.escape(name), display,
                mtime, mtime, size, shown))
    return """<!DOCTYPE html>
<html><head><title>Index of {path}</title>
<meta charset="utf-8"></head><body>
<h1>Index of {path}</h1>
<table id="indexlist">
 <tr><th class="indexcolicon"><img src="/icons/blank.gif" alt="[ICO]"></th>
     <th class="indexcollastmod"><a href="?C=N;O=D">Name</a></th>
     <th class="indexcollastmod"><a href="?C=M;O=A">Last modified</a></th>
     <th class="indexcolsize"><a href="?C=S;O=A">Size</a></th>
     <th class="indexcoldesc"><a href="?C=D;O=A">Description</a></th></tr>
 <tr><th colspan="5"><hr></th></tr>
 <tr><td class="indexcolicon"><img src="/icons/back.gif" alt="[PARENTDIR]"></td>
     <td><a href="../">Parent Directory</a></td><td>&nbsp;</td><td class="indexcolsize">-</td><td>&nbsp;</td></tr>
{rows}
 <tr><th colspan="5"><hr></th></tr>
</table></body></html>
""".format(path=path, rows="\n".join(rows))


def nginx_autoindex(path: str, entries: List[Tuple[str, int, str]]) -> str:
    rows = []
    for name, size, mtime in entries:
        shown = "-" if size == 0 else human_size(size)
        pad = " " * max(1, 40 - len(name))
        href = urllib_quote(name) + "/" if size == 0 else urllib_quote(name)
        rows.append('<a href="{0}">{1}</a>{2}{3}{4}'
                    .format(href, html.escape(name), pad, mtime, shown))
    return """<html>
<head><title>Index of {path}</title></head>
<body bgcolor="white">
<h1>Index of {path}</h1><hr><pre><a href="../">../</a>
{rows}
</pre><hr></body>
</html>
""".format(path=path, rows="\n".join(rows))


def link_farm(path: str, entries: List[Tuple[str, int, str]], title: str) -> str:
    """Plain page whose anchors have no metadata at all (macau.rn.gov.br style)."""
    items = "\n".join(
        '<p><a href="{}">{}</a></p>'.format(html.escape(urllib_quote(name)), html.escape(name))
        for name, _, _ in entries)
    return """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title></head>
<body><h1>{title}</h1>
{items}
</body></html>
""".format(title=html.escape(title), items=items)


def ods_list(path: str, entries: List[Tuple[str, int, str]]) -> str:
    """OpenDataSoft-like page: a bare list of document links, sizes in the page."""
    items = "\n".join(
        '<a class="file" href="{0}">{0}</a>'.format(html.escape(name)) for name, _, _ in entries)
    return """<html><head><meta charset="utf-8"><title>Index of {path}</title></head>
<body class="pagelist"><div id="content"><h1>Index of {path}</h1>
{items}
</div></body></html>
""".format(path=path, items=items)


def urllib_quote(name: str) -> str:
    import urllib.parse
    return urllib.parse.quote(name, safe="")


# --------------------------------------------------------------------------
# the six replica sites
# --------------------------------------------------------------------------

def write_file(root: str, rel: str, data: bytes) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def write_html(root: str, rel_dir: str, name: str, text: str) -> None:
    path = os.path.join(root, rel_dir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_pgfn(root: str) -> Dict[str, object]:
    """Bullet index (no metadata) -> exercises the directory-probe path."""
    quarters = ["2025_trimestre_03", "2025_trimestre_04"]
    doc_names = ["Dados_abertos_FGTS.zip", "Dados_abertos_Nao_Previdenciario.zip",
                 "Dados_abertos_Previdenciario.zip"]
    # the FGTS file is byte-identical in both quarters (real-world republication)
    for quarter in quarters:
        entries = []
        for name in doc_names:
            seed = "pgfn-FGTS" if name.endswith("FGTS.zip") else "pgfn-" + name
            size = 300 * 1024 if name.endswith("FGTS.zip") else (
                320 * 1024 if "Nao_Previdenciario" in name else 420 * 1024)
            write_file(root, "{}/{}".format(quarter, name), zipf(seed, size))
            entries.append((name, size, ""))
        write_html(root, quarter, "index.html", link_farm(
            "/{}/".format(quarter), entries, "Index of /{}/".format(quarter)))
    write_html(root, "", "index.html", link_farm("/", [("Portal_da_Cidadania_Tributaria", 0, "")] +
                                                 [(q, 0, "") for q in quarters],
                                                 "Index of /"))
    write_html(root, "Portal_da_Cidadania_Tributaria", "index.html", link_farm(
        "/Portal_da_Cidadania_Tributaria/",
        [("Sistema_de_Parcelamento_2025.zip", 0, "")],
        "Index of /Portal_da_Cidadania_Tributaria/"))
    write_file(root, "Portal_da_Cidadania_Tributaria/Sistema_de_Parcelamento_2025.zip",
               zipf("pgfn-cidadania", 40 * 1024))
    return {"slug": "dadosabertos.pgfn.gov.br", "url": "https://replica.local/pgfn/",
            "title": "Dados Abertos PGFN (replica)",
            "note": "Bullet-style index without sizes: the crawler decides "
                    "file-vs-directory by probing. The FGTS archive is repeated in "
                    "every quarter -> collapsed by the byte probe."}


def build_valiprev(root: str) -> Dict[str, object]:
    """Apache 2.4 table with truncated cells (needs link-title handling)."""
    entries = [
        ("10_2023-LGPD-ARTE-VISUAL-E-IMPRESSAO-DAS-ELEICOES-DO-CONSELHO-DISPENSA.pdf", 3800, "2024-02-16 14:56"),
        ("10_2023-LGPD-ARTE-VISUAL-E-IMPRESSAO-DAS-ELEICOES-DO-CONSELHO-DISPENSA1.pdf", 3800, "2024-02-19 09:38"),
        ("500.pdf", 387 * 1024, "2024-11-18 11:15"),
        ("AUDIÊNCIA_2023_-_LISTA_DE_PRESENÇA.pdf", 338 * 1024, "2024-10-30 13:43"),
        ("AUDIÊNCIA_2024_-_DI..X.pdf", 706 * 1024, "2024-10-30 14:28"),
        ("AUDIÊNCIA_2024_-_LISTA_DE_PRESENÇA.pdf", 660 * 1024, "2025-11-28 16:41"),
        ("CERTIDÃO_DE_REGULARIDADE_DO_FGTS.pdf", 90 * 1024, "2024-11-12 17:00"),
        ("10_2023-LGPD-ARTE-VISUAL-E-IMPRESSAO-DAS-ELEICOES-DO-CONSELHO-DISPENSA2.pdf", 3800, "2024-11-18 09:02"),
    ]
    for name, size, _ in entries:
        seed = "valiprev-DISPENSA" if "DISPENSA" in name else "valiprev-" + name
        write_file(root, "uploads/paginas/certidoes/pdf/{}".format(name), pdf(seed, size))
    write_html(root, "uploads/paginas/certidoes/pdf", "index.html",
               apache_table("/uploads/paginas/certidoes/pdf",
                            [(n, s, m) for n, s, m in entries]))
    return {"slug": "valiprev.sp.gov.br", "url": "https://replica.local/valiprev/",
            "title": "Valiprev — certidões e audiências (replica)",
            "note": "Apache table dialect: names are truncated in the visible cell and "
                    "recovered from the link title/URL. The three DISPENSA files share "
                    "identical bytes (a real pattern on this site)."}


def build_caxias(root: str) -> Dict[str, object]:
    """nginx autoindex: <pre>, no dates, no trailing slash on directories."""
    quadras = ["45", "46"]
    for quadra in quadras:
        # quadra 45 -> classic nginx autoindex (date + size)
        # quadra 46 -> date-less variant that still publishes sizes
        dated = quadra == "45"
        entries = [
            ("quadra_{}.dwg".format(quadra), 210 * 1024,
             "2024-03-11 08:22" if dated else ""),
            ("quadra_{}_lotes.kml".format(quadra), 30 * 1024,
             "2024-03-11 08:22" if dated else ""),
            ("LEIAME.txt", 1200, "2024-03-11 08:22" if dated else ""),
        ]
        for name, size, _ in entries:
            kind = "txt" if name.endswith(".txt") else "caxias"
            write_file(root, "pub/quadras/{}/{}".format(quadra, name),
                       blob("{}-{}-{}".format(kind, quadra, name), size,
                            b"LEIAME\n" if name.endswith(".txt") else b"DWG"))
        write_html(root, "pub/quadras/{}".format(quadra), "index.html",
                   nginx_autoindex("/pub/quadras/{}/".format(quadra), entries))
    write_html(root, "pub/quadras", "index.html",
               nginx_autoindex("/pub/quadras/", [(q, 0, "") for q in quadras]))
    write_html(root, "pub", "index.html",
               nginx_autoindex("/pub/", [("quadras", 0, "")]))
    return {"slug": "geofiles.caxias.rs.gov.br", "url": "https://replica.local/caxias/",
            "title": "Geofiles Caxias do Sul — quadras (replica)",
            "note": "nginx autoindex keeps the literal '..' anchor; directories are "
                    "reached through a 301 redirect to the trailing-slash URL."}


def build_macau(root: str) -> Dict[str, object]:
    """Link farm: years -> months -> daily PDFs; nothing but anchors."""
    months = [("01 - Janeiro-2013", ["macau-2013-01-02.pdf", "macau-2013-01-15.pdf"]),
              ("02 - Fevereiro-2013", ["macau-2013-02-07.pdf"]),
              ("03 - Março-2013", ["macau-2013-03-11.pdf", "macau-2013-03-11-edicao-2.pdf"])]
    for month, pdfs in months:
        entries = []
        for name in pdfs:
            # the two 2013-01-02 editions are byte-identical under different names
            duplicate = "01-02" in name or "edicao-2" in name
            size = 150 * 1024 if duplicate else 130 * 1024
            seed = "macau-edicao-x" if duplicate else "macau-" + name
            write_file(root, "diario/2013 - Diário Oficial de Macau/{}/{}".format(month, name),
                       pdf(seed, size))
            entries.append((name, size, ""))
        write_html(root, "diario/2013 - Diário Oficial de Macau/{}".format(month), "index.html",
                   link_farm("/diario/2013/{}".format(month), entries, month))
    write_html(root, "diario/2013 - Diário Oficial de Macau", "index.html",
               link_farm("/diario/2013 - Diário Oficial de Macau",
                         [(m, 0, "") for m, _ in months],
                         "Index of /diario/2013 - Diário Oficial de Macau"))
    write_html(root, "diario", "index.html",
               link_farm("/diario/", [("2013 - Diário Oficial de Macau", 0, "")], "Index of /diario/"))
    return {"slug": "macau.rn.gov.br", "url": "https://replica.local/macau/diario/",
            "title": "Diário Oficial de Macau (replica)",
            "note": "Pure link farm: every href lacks a trailing slash, a date and a "
                    "size, so each one is probed. Two 2013 editions are byte-identical "
                    "under different names."}


def build_comissao(root: str) -> Dict[str, object]:
    """OpenDataSoft-style bare link list (real site: 3 000+ PDFs, latin-1 names)."""
    entries = [
        ("001-Antonio-Raymundo-Lucena.pdf", 140 * 1024, ""),
        ("001-Arquivo-CEMDP-Joao-Carlos-Cavalcanti-Reis.pdf", 88 * 1024, ""),
        ("001-Arquivo-CEMDP-Joao-Carlos-Cavalcanti-Reis004.pdf", 88 * 1024, ""),
        ("001-ArquivoCEMDP-devanir.pdf", 33 * 1024, ""),
        ("001-ArquivoCEMDP.pdf", 33 * 1024, ""),
        ("001-CartaLilianRuggia1.jpg", 76 * 1024, ""),
        ("001-Certidao-de-casamento.pdf", 237 * 1024, ""),
        ("001 - Ficha DEOPS Paulo Roberto Pinto.pdf", 52 * 1024, ""),
    ]
    for name, size, _ in entries:
        if "Reis" in name:              # same bytes under two names
            data = pdf("comissao-reis", size)
        elif "CEMDP" in name:           # same bytes under two names
            data = pdf("comissao-cemdp", size)
        elif name.endswith(".jpg"):
            data = jpg(name, size)
        else:
            data = pdf(name, size)
        write_file(root, "upload/{}".format(name), data)
    write_html(root, "upload", "index.html", ods_list(
        "/upload/", entries))
    return {"slug": "comissaodaverdade.al.sp.gov.br", "url": "https://replica.local/comissao/upload/",
            "title": "Comissão da Verdade ALESP — acervo (replica)",
            "note": "Links without metadata; content-identical documents appear under "
                    "different names ('001-ArquivoCEMDP.pdf' vs '...-devanir.pdf'), "
                    "which is exactly what the SHA-256 deduper is for."}


def build_cvm(root: str) -> Dict[str, object]:
    """Apache <pre> index with dates + sizes and nested dataset/year folders."""
    datasets = {
        "ADM_CART": [("ADM_CART_CAD_2024.zip", 64 * 1024, "2025-07-11 18:23"),
                     ("ADM_CART_CAD_2025.zip", 51 * 1024, "2025-07-11 18:23")],
        "CIA_ABERTA": [("cia_aberta_doc_2025.zip", 96 * 1024, "2025-08-01 09:12"),
                       ("cia_aberta_fre_2025.zip", 96 * 1024, "2025-08-01 09:12")],
        "FIDC": [("fidc_doc_2025.zip", 96 * 1024, "2025-08-01 09:12")],
    }
    # the same archive is published under two datasets -> cross-dataset duplicate;
    # 'cia_aberta_fre_2025.zip' has the same *size* but different bytes, so the
    # range probe must not collapse it (a false positive would be data loss).
    seeds = {"cia_aberta_doc_2025.zip": "cvm-doc-2025", "fidc_doc_2025.zip": "cvm-doc-2025"}
    top = []
    for ds, files in datasets.items():
        write_html(root, "dados/{}".format(ds), "index.html",
                   apache_table("/dados/{}/".format(ds),
                                [(name, size, mtime) for name, size, mtime in files]))
        for name, size, _ in files:
            write_file(root, "dados/{}/{}".format(ds, name),
                       zipf(seeds.get(name, name), size))
        top.append((ds, 0, "27-Feb-2020 16:28"))
    write_html(root, "dados", "index.html", apache_pre("/dados/", top))
    return {"slug": "dados.cvm.gov.br", "url": "https://replica.local/cvm/dados/",
            "title": "Portal Dados Abertos CVM (replica)",
            "note": "Apache <pre> index with dates and human sizes; 'CIA_ABERTA' and "
                    "'FIDC' both publish a 96 KiB 'doc' archive with identical bytes, "
                    "while 'cia_aberta_fre_2025.zip' has the same size with different "
                    "content — the deduper must collapse the first pair only."}


BUILDERS = [build_pgfn, build_valiprev, build_caxias, build_macau, build_comissao, build_cvm]


def build(out_root: str) -> List[Dict[str, object]]:
    if os.path.exists(out_root):
        shutil.rmtree(out_root)
    sites, rows = [], []
    for builder in BUILDERS:
        name = builder.__name__.replace("build_", "")
        root = os.path.join(out_root, name)
        os.makedirs(root, exist_ok=True)
        sites.append(builder(root))
        rows.append((name, 0, ""))
    # a browseable root index for humans; the scraper is pointed at each site
    write_html(out_root, "", "index.html", nginx_autoindex("/", rows))
    return sites


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="generate the offline replica fixtures")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "replica"))
    ap.add_argument("--write-sites", help="also write a sites.json pointing at the replica")
    args = ap.parse_args(argv)
    sites = build(args.out)
    print("replica built in {}".format(args.out))
    for site in sites:
        print("  {:32} {}".format(site["slug"], site["url"]))
    if args.write_sites:
        import json
        with open(args.write_sites, "w", encoding="utf-8") as fh:
            json.dump({"sites": sites}, fh, indent=2, ensure_ascii=False)
        print("wrote", args.write_sites)
    return 0


if __name__ == "__main__":
    sys.exit(main())
