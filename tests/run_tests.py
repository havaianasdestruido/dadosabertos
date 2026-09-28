#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test suite for indexclone (a.k.a. scraper.py).

    python3 tests/run_tests.py            # everything
    python3 tests/run_tests.py -v         # verbose
    python3 -m unittest tests.run_tests.TestEndToEnd -v

Two layers:

* unit tests for the parsers/helpers, fed with the same HTML dialects the six
  production sites use (see tests/make_replica.py);
* an end-to-end test that starts the replica over HTTP and drives the real CLI:
  crawl -> download -> de-duplicate -> hardlink -> report -> resume -> refresh.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scraper                                                        # noqa: E402
from tests import make_replica, replica_server                        # noqa: E402

REPLICA = os.path.join(HERE, "replica")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ==========================================================================
# unit tests: helpers
# ==========================================================================


class TestHelpers(unittest.TestCase):
    def test_parse_size_token(self):
        self.assertEqual(scraper.parse_size_token("3.8M"), 3984588)
        self.assertEqual(scraper.parse_size_token("387K"), 396288)
        self.assertEqual(scraper.parse_size_token("104M"), 109051904)
        self.assertEqual(scraper.parse_size_token("1.2G"), 1288490188)
        self.assertEqual(scraper.parse_size_token("500"), 500)
        self.assertIsNone(scraper.parse_size_token("-"))
        self.assertIsNone(scraper.parse_size_token(""))
        self.assertIsNone(scraper.parse_size_token("n/a"))

    def test_parse_listing_date(self):
        self.assertEqual(scraper.parse_listing_date("31-Aug-2015 15:32"),
                         "2015-08-31T15:32:00Z")
        self.assertEqual(scraper.parse_listing_date("2024-02-16 14:56"),
                         "2024-02-16T14:56:00Z")
        self.assertEqual(scraper.parse_listing_date("08-Mar-2021 22:07"),
                         "2021-03-08T22:07:00Z")
        self.assertIsNone(scraper.parse_listing_date("nope"))

    def test_canonical_url(self):
        self.assertEqual(scraper.canonical_url("HTTPS://Example.GOV/a%20b.pdf"),
                         "https://example.gov/a%20b.pdf")
        self.assertEqual(scraper.canonical_url("http://example.gov:80/x"), "http://example.gov/x")
        self.assertEqual(scraper.canonical_url("https://example.gov:443/x/"),
                         "https://example.gov/x/")
        self.assertEqual(scraper.canonical_url("https://example.gov/#frag"),
                         "https://example.gov/")

    def test_sort_links_are_not_documents(self):
        self.assertTrue(scraper.is_sort_link("https://x.gov/?C=N;O=D"))
        self.assertTrue(scraper.is_sort_link("https://x.gov/dir/?sort=name"))
        self.assertFalse(scraper.is_sort_link("https://x.gov/dir/?file=1"))
        self.assertFalse(scraper.is_sort_link("https://x.gov/dir/a.pdf"))

    def test_url_under_prefix_keeps_scope(self):
        base = "https://x.gov/dados/"
        self.assertTrue(scraper.url_under_prefix("https://x.gov/dados/a/b.pdf", base))
        self.assertTrue(scraper.url_under_prefix("https://x.gov/dados/", base))
        self.assertFalse(scraper.url_under_prefix("https://x.gov/outros/a.pdf", base))
        self.assertFalse(scraper.url_under_prefix("https://y.gov/dados/a.pdf", base))
        self.assertFalse(scraper.url_under_prefix("https://x.gov/dados2/a.pdf", base))

    def test_rel_dir_for(self):
        self.assertEqual(scraper.rel_dir_for("https://x.gov/dados/", "https://x.gov/dados/"), "")
        self.assertEqual(scraper.rel_dir_for("https://x.gov/dados/2024/", "https://x.gov/dados/"),
                         "2024")
        self.assertEqual(scraper.rel_dir_for("https://x.gov/dados/a%20b/", "https://x.gov/dados/"),
                         "a b")

    def test_sanitize_component(self):
        self.assertEqual(scraper.sanitize_component("a b.pdf"), "a b.pdf")
        self.assertEqual(scraper.sanitize_component("a/b:c*d?.pdf"), "a_b_c_d_.pdf")
        self.assertEqual(scraper.sanitize_component("trailing. ."), "trailing")
        self.assertEqual(scraper.sanitize_component(".."), "_")
        self.assertEqual(scraper.sanitize_component("con.txt"), "con_.txt")
        long_name = scraper.sanitize_component("x" * 300 + ".pdf")
        self.assertLessEqual(len(long_name), 180)
        self.assertTrue(long_name.endswith(".pdf"))
        self.assertEqual(scraper.sanitize_component("bad\x01name.pdf"), "badname.pdf")

    def test_local_relpath_mirrors_tree_and_avoids_collisions(self):
        taken = {}
        base = "https://x.gov/dados/"
        self.assertEqual(
            scraper.local_relpath("https://x.gov/dados/2024/a%20b.pdf", base, taken),
            "2024/a b.pdf")
        # a second, different URL that sanitises to the same path gets a suffix
        self.assertEqual(
            scraper.local_relpath("https://x.gov/dados/2024/a%3Ab.pdf", base, taken),
            "2024/a_b.pdf")
        self.assertEqual(
            scraper.local_relpath("https://x.gov/dados/2024/a*b.pdf", base, taken),
            "2024/a_b_2.pdf")
        self.assertEqual(scraper.local_relpath("https://x.gov/dados/index.html", base, taken),
                         "index.html")

    def test_request_ready_encodes_but_never_double_encodes(self):
        self.assertEqual(scraper.request_ready("https://x.gov/a b.pdf"), "https://x.gov/a%20b.pdf")
        self.assertEqual(scraper.request_ready("https://x.gov/AUDIÊNCIA.pdf"),
                         "https://x.gov/AUDI%C3%8ANCIA.pdf")
        self.assertEqual(scraper.request_ready("https://x.gov/a%20b.pdf"),
                         "https://x.gov/a%20b.pdf")

    def test_decode_html_honours_meta_charset(self):
        raw = '<meta charset="iso-8859-1"><p>AUDIÊNCIA</p>'.encode("iso-8859-1")
        self.assertIn("AUDIÊNCIA", scraper.decode_html(raw))
        self.assertIn("AUDIÊNCIA", scraper.decode_html("<p>AUDIÊNCIA</p>".encode("utf-8")))

    def test_human_and_budget(self):
        self.assertEqual(scraper.human(0), "0 B")
        self.assertEqual(scraper.human(1536), "1.5 KiB")
        budget = scraper.ByteBudget(1000)
        self.assertTrue(budget.take(600))
        self.assertFalse(budget.take(600))
        self.assertTrue(budget.take(400))

    def test_robots_parser(self):
        body = "User-agent: *\nDisallow: /privado/\nDisallow: /tmp\n\nUser-agent: bot\nDisallow: /\n"
        self.assertEqual(scraper.Fetcher._parse_robots(body), ["/privado/", "/tmp"])
        # a '*' block without rules must not block anything
        self.assertEqual(scraper.Fetcher._parse_robots("User-agent: *\nAllow: /\n"), [])


class TestConfigDefaults(unittest.TestCase):
    def test_defaults_reach_keys_the_site_did_not_set(self):
        sites, defaults = scraper.load_config(os.path.join(ROOT, "sites.json"))
        self.assertEqual(len(sites), 6)
        self.assertEqual(defaults.get("max_depth"), 4)
        # every site spells out its own depth, so `defaults` must not touch them
        self.assertEqual([s.max_depth for s in sites], [3, 2, 4, 3, 2, 5])

    def test_zero_and_false_configured_values_are_real_values(self):
        sites, _ = scraper.load_config(_write_config({
            "defaults": {"max_depth": 7, "follow_external": True},
            "sites": [{"slug": "a", "url": "https://a.gov/"},
                      {"slug": "b", "url": "https://b.gov/", "max_depth": 0,
                       "follow_external": False}]}))
        by_slug = {s.slug: s for s in sites}
        self.assertEqual(by_slug["a"].max_depth, 7)          # filled from defaults
        self.assertTrue(by_slug["a"].follow_external)
        self.assertEqual(by_slug["b"].max_depth, 0)          # explicit 0 survives
        self.assertFalse(by_slug["b"].follow_external)
        # ... and by_slug["b"] keeps it through a second merge
        self.assertEqual(by_slug["b"].merged({"max_depth": 9}).max_depth, 0)


def _write_config(payload: dict) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="indexclone-cfg-"), "sites.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


# ==========================================================================
# the production config shipped with the repo
# ==========================================================================


class TestProductionConfig(unittest.TestCase):
    def test_sites_json_covers_the_six_requested_listings(self):
        sites, defaults = scraper.load_config(os.path.join(ROOT, "sites.json"))
        self.assertEqual(len(sites), 6)
        self.assertEqual([s.slug for s in sites], [
            "dadosabertos.pgfn.gov.br",
            "valiprev.sp.gov.br",
            "geofiles.caxias.rs.gov.br",
            "macau.rn.gov.br",
            "comissaodaverdade.al.sp.gov.br",
            "dados.cvm.gov.br",
        ])
        for site in sites:
            self.assertTrue(site.url.startswith("https://"))
            self.assertEqual(site.url, site.url.rstrip("?&"))  # no dangling '?SA'
            self.assertTrue(site.entry_urls, site.slug)
        # the defaults are shared, the notes are per-site
        self.assertEqual(defaults.get("max_depth"), 4)
        self.assertTrue(all(s.note for s in sites))


# ==========================================================================
# unit tests: listing dialects
# ==========================================================================


class TestListingParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(REPLICA):
            make_replica.build(REPLICA)

    def parse(self, relpath: str, base: str = "https://replica.local/"):
        text = read(os.path.join(REPLICA, relpath))
        return scraper.parse_listing(text, base + relpath.replace("/index.html", "/"), base)

    def test_bullet_index_without_metadata_pgfn(self):
        entries = self.parse("pgfn/index.html")
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e.size is None and e.mtime is None for e in entries))
        self.assertTrue(all(e.unknown for e in entries))          # resolved by probing
        self.assertIn("2025_trimestre_04", {e.name for e in entries})

    def test_bullet_index_files_pgfn(self):
        entries = self.parse("pgfn/2025_trimestre_04/index.html")
        names = {e.name for e in entries}
        self.assertEqual(names, {"Dados_abertos_FGTS.zip", "Dados_abertos_Nao_Previdenciario.zip",
                                 "Dados_abertos_Previdenciario.zip"})

    def test_apache_table_valiprev(self):
        entries = self.parse("valiprev/uploads/paginas/certidoes/pdf/index.html")
        by_name = {e.name: e for e in entries}
        self.assertNotIn("Parent Directory", by_name)
        self.assertNotIn("../", by_name)
        # truncated cells recovered from the link title
        self.assertIn("10_2023-LGPD-ARTE-VISUAL-E-IMPRESSAO-DAS-ELEICOES-DO-CONSELHO-DISPENSA.pdf",
                      by_name)
        dispensa = by_name["10_2023-LGPD-ARTE-VISUAL-E-IMPRESSAO-DAS-ELEICOES-DO-CONSELHO-DISPENSA.pdf"]
        self.assertEqual(dispensa.size, 3800)
        self.assertEqual(dispensa.mtime, "2024-02-16T14:56:00Z")
        self.assertFalse(dispensa.is_dir)
        self.assertEqual(by_name["500.pdf"].size, 387 * 1024)
        self.assertEqual(by_name["AUDIÊNCIA_2023_-_LISTA_DE_PRESENÇA.pdf"].size, 338 * 1024)
        # sort links must never become entries
        self.assertTrue(all("?C=" not in e.url for e in entries))

    def test_nginx_autoindex_caxias(self):
        dated = self.parse("caxias/pub/quadras/45/index.html")
        self.assertEqual(len(dated), 3)
        self.assertEqual(dated[0].size, 210 * 1024)
        self.assertEqual(dated[0].mtime, "2024-03-11T08:22:00Z")
        dateless = self.parse("caxias/pub/quadras/46/index.html")
        self.assertEqual(len(dateless), 3)
        self.assertEqual(dateless[0].size, 210 * 1024)            # size without a date
        self.assertIsNone(dateless[0].mtime)
        self.assertTrue(all(not e.is_dir for e in dateless))

    def test_link_farm_macau(self):
        years = self.parse("macau/diario/index.html")
        self.assertEqual(len(years), 1)
        self.assertEqual(years[0].name, "2013 - Diário Oficial de Macau")
        months = self.parse("macau/diario/2013 - Diário Oficial de Macau/index.html")
        self.assertEqual(len(months), 3)
        self.assertTrue(all(e.size is None for e in months))
        pdfs = self.parse("macau/diario/2013 - Diário Oficial de Macau/03 - Março-2013/index.html")
        self.assertEqual(len(pdfs), 2)
        self.assertTrue(pdfs[0].url.endswith("macau-2013-03-11.pdf"))

    def test_ods_link_list_comissao(self):
        entries = self.parse("comissao/upload/index.html")
        self.assertEqual(len(entries), 8)
        self.assertIn("001 - Ficha DEOPS Paulo Roberto Pinto.pdf", {e.name for e in entries})
        self.assertTrue(all(e.size is None for e in entries))

    def test_apache_pre_with_metadata_cvm(self):
        entries = self.parse("cvm/dados/index.html")
        self.assertEqual({e.name for e in entries}, {"ADM_CART", "CIA_ABERTA", "FIDC"})
        self.assertTrue(all(e.is_dir for e in entries))
        self.assertEqual(entries[0].mtime, "2020-02-27T16:28:00Z")
        self.assertIsNone(entries[0].size)
        files = self.parse("cvm/dados/ADM_CART/index.html")
        by_name = {e.name: e for e in files}
        self.assertEqual(by_name["ADM_CART_CAD_2025.zip"].size, 51 * 1024)
        self.assertEqual(by_name["ADM_CART_CAD_2025.zip"].mtime, "2025-07-11T18:23:00Z")

    def test_scope_is_enforced(self):
        outside = scraper.parse_listing(
            '<html><body><a href="https://other.example/x.pdf">x.pdf</a>'
            '<a href="https://x.gov/dados/ok.pdf">ok.pdf</a></body></html>',
            "https://x.gov/dados/", "https://x.gov/dados/")
        self.assertEqual([e.name for e in outside], ["ok.pdf"])

    def test_candidate_duplicates_heuristic(self):
        entries = [
            scraper.Entry("https://x.gov/a/001-ArquivoCEMDP.pdf", "001-ArquivoCEMDP.pdf",
                          False, 33792, None),
            scraper.Entry("https://x.gov/a/001-ArquivoCEMDP-devanir.pdf",
                          "001-ArquivoCEMDP-devanir.pdf", False, 33792, None),
            scraper.Entry("https://x.gov/a/outra-coisa.pdf", "outra-coisa.pdf", False, 33792, None),
            scraper.Entry("https://x.gov/a/unico.pdf", "unico.pdf", False, 111, None),
        ]
        rows = scraper.SiteCloner._candidate_duplicates(entries)
        urls = {r["candidate_url"] for r in rows}
        self.assertIn("https://x.gov/a/001-ArquivoCEMDP-devanir.pdf", urls)
        self.assertNotIn("https://x.gov/a/unico.pdf", urls)

    def test_looks_like_listing(self):
        self.assertTrue(scraper.looks_like_listing("<h1>Index of /x</h1>"))
        self.assertTrue(scraper.looks_like_listing(
            "<html>" + "".join('<a href="f{}.pdf">f{}.pdf</a>'.format(i, i) for i in range(9))))
        self.assertFalse(scraper.looks_like_listing(
            "<html><body><h1>A notícia</h1><p>texto</p></body></html>"))


# ==========================================================================
# end-to-end: replica over HTTP, real CLI
# ==========================================================================


class TestEndToEnd(unittest.TestCase):
    """Drives scraper.main() against the replica server, in a temp folder."""

    @classmethod
    def setUpClass(cls):
        if os.path.isdir(REPLICA):
            shutil.rmtree(REPLICA)
        replica_sites = make_replica.build(REPLICA)
        cls.httpd, cls.base, cls.thread = replica_server.serve_in_thread(REPLICA)
        cls.tmp = tempfile.mkdtemp(prefix="indexclone-e2e-")
        cls.out = os.path.join(cls.tmp, "outputs")
        cls.config = os.path.join(cls.tmp, "replica_sites.json")
        with open(cls.config, "w", encoding="utf-8") as fh:
            json.dump({"defaults": {"max_depth": 5}, "sites": replica_sites}, fh, indent=1)
        cls.rewrite = ["https://replica.local/={}/".format(cls.base.rstrip("/"))]
        cls.access = []
        replica_server.RangeHandler.access_log = cls.access

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def run_cli(self, extra, expect=0, out=None):
        args = ["--config", self.config, "--all",
                "--out", out or self.out, "--jobs", "6", "--rate", "0", "--quiet",
                "--rewrite", self.rewrite[0]] + extra
        code = scraper.main(args)
        self.assertEqual(code, expect, "scraper.main returned {} for {}".format(code, extra))
        return code

    def summary(self, slug: str) -> dict:
        return json.loads(read(os.path.join(self.out, slug, "_reports", "summary.json")))

    def stored_files(self):
        """{relative path: size} for every payload currently on disk."""
        out = {}
        for slug in os.listdir(self.out):
            files = os.path.join(self.out, slug, "files")
            for dirpath, _, names in os.walk(files):
                for name in names:
                    full = os.path.join(dirpath, name)
                    out[os.path.relpath(full, self.out)] = os.path.getsize(full)
        return out

    def manifest_rows(self, slug: str):
        import csv as csvmod
        with open(os.path.join(self.out, slug, "_reports", "manifest.csv"), encoding="utf-8") as fh:
            return list(csvmod.DictReader(fh))

    # -- tests (ordered by name: test_01 ...) ----------------------------
    def test_01_first_run_downloads_everything(self):
        self.run_cli([])
        files = os.path.join(self.out, "dadosabertos.pgfn.gov.br", "files")
        self.assertTrue(os.path.exists(os.path.join(files, "2025_trimestre_04",
                                                    "Dados_abertos_FGTS.zip")))
        self.assertTrue(os.path.exists(os.path.join(files, "Portal_da_Cidadania_Tributaria",
                                                    "Sistema_de_Parcelamento_2025.zip")))
        cvm = os.path.join(self.out, "dados.cvm.gov.br", "files", "ADM_CART")
        self.assertTrue(os.path.exists(os.path.join(cvm, "ADM_CART_CAD_2025.zip")))
        valiprev = os.path.join(self.out, "valiprev.sp.gov.br", "files",
                                "uploads/paginas/certidoes/pdf")
        self.assertTrue(os.path.exists(os.path.join(valiprev, "500.pdf")))
        self.assertTrue(os.path.exists(
            os.path.join(valiprev, "AUDIÊNCIA_2023_-_LISTA_DE_PRESENÇA.pdf")))
        macau = os.path.join(self.out, "macau.rn.gov.br", "files",
                             "2013 - Diário Oficial de Macau/03 - Março-2013")
        self.assertTrue(os.path.exists(os.path.join(macau, "macau-2013-03-11.pdf")))

        # payloads are byte-identical to the originals
        origin = os.path.join(REPLICA, "valiprev/uploads/paginas/certidoes/pdf/500.pdf")
        copy = os.path.join(valiprev, "500.pdf")
        self.assertEqual(scraper.hashlib.sha256(open(origin, "rb").read()).hexdigest(),
                         scraper.hashlib.sha256(open(copy, "rb").read()).hexdigest())

        # audit trail
        for slug in ("dadosabertos.pgfn.gov.br", "valiprev.sp.gov.br",
                     "geofiles.caxias.rs.gov.br", "macau.rn.gov.br",
                     "comissaodaverdade.al.sp.gov.br", "dados.cvm.gov.br"):
            root = os.path.join(self.out, slug)
            for name in ("README.md", "_reports/manifest.json", "_reports/manifest.csv",
                         "_reports/duplicates.csv", "_reports/summary.json",
                         "_reports/state.json", "_reports/errors.log"):
                self.assertTrue(os.path.exists(os.path.join(root, name)),
                                "missing {} for {}".format(name, slug))
            self.assertTrue(os.path.isdir(os.path.join(root, "_index/pages")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "_reports", "global.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "_reports", "README.md")))

    def test_02_dedupes_identical_documents(self):
        dupes = {}
        import csv as csvmod
        for slug in os.listdir(self.out):
            path = os.path.join(self.out, slug, "_reports", "duplicates.csv")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    dupes[slug] = list(csvmod.DictReader(fh))
        # pgfn: the FGTS archive is republished in every quarter (byte probe hit)
        # pgfn: each of the three quarterly archives is repeated in the next quarter
        pgfn = dupes["dadosabertos.pgfn.gov.br"]
        self.assertEqual(len(pgfn), 3)
        self.assertTrue(all(row["sha256"] for row in pgfn))
        # the same-size gate means the copies are compared byte-range-wise and
        # never downloaded twice, whatever the thread scheduling
        self.assertEqual({row["method"] for row in pgfn}, {"range-probe"})
        fgts = [row for row in pgfn if row["kept_url"].endswith("FGTS.zip")]
        self.assertEqual(len(fgts), 1)
        pair = sorted([fgts[0]["kept_url"], fgts[0]["duplicate_url"]])
        self.assertTrue(pair[0].endswith("2025_trimestre_03/Dados_abertos_FGTS.zip"))
        self.assertTrue(pair[1].endswith("2025_trimestre_04/Dados_abertos_FGTS.zip"))
        # comissao: same bytes under different names (full-hash hit)
        comissao = dupes["comissaodaverdade.al.sp.gov.br"]
        self.assertEqual({row["method"] for row in comissao}, {"sha256"})
        self.assertEqual(len(comissao), 2)
        # cvm: the same archive in two different datasets
        cvm = dupes["dados.cvm.gov.br"]
        self.assertEqual(len(cvm), 1)
        self.assertEqual(cvm[0]["method"], "sha256")
        # valiprev: three DISPENSA files with identical bytes (2 collapsed)
        self.assertEqual(len(dupes["valiprev.sp.gov.br"]), 2)
        # macau: two identical editions with different names
        self.assertEqual(len(dupes["macau.rn.gov.br"]), 1)

    def test_03_duplicates_are_hardlinked_not_copied(self):
        root = os.path.join(self.out, "dadosabertos.pgfn.gov.br", "files")
        original = os.path.join(root, "2025_trimestre_03", "Dados_abertos_FGTS.zip")
        duplicate = os.path.join(root, "2025_trimestre_04", "Dados_abertos_FGTS.zip")
        self.assertTrue(os.path.exists(duplicate))
        self.assertEqual(os.stat(original).st_ino, os.stat(duplicate).st_ino)
        self.assertEqual(os.stat(original).st_nlink, 2)
        # ... and the replica did NOT download the 300 KiB twice: one full body,
        # plus the two 64 KiB range probes that identified the copy
        requests = [r for r in self.access if r[1].endswith("Dados_abertos_FGTS.zip")]
        self.assertEqual(len([r for r in requests if not r[2]]), 1, "one full body only")
        # both copies of a repeated name look up their size first (1 byte each),
        # then only the second one pays for the head+tail probe
        self.assertEqual(len([r for r in requests if r[2] == "bytes=0-0"]), 2)
        probes = [r for r in requests if r[2] in ("bytes=0-65535", "bytes=-65536")]
        self.assertEqual(len(probes), 2, "head+tail byte probe instead of a download")

    def test_04_same_size_but_different_content_is_not_a_duplicate(self):
        # cvm publishes two 96 KiB archives; only one pair is byte-identical
        rows = self.manifest_rows("dados.cvm.gov.br")
        by_path = {r["local_path"]: r for r in rows}
        doc_pair = [by_path["CIA_ABERTA/cia_aberta_doc_2025.zip"],
                    by_path["FIDC/fidc_doc_2025.zip"]]
        self.assertEqual(sorted(r["status"] for r in doc_pair), ["downloaded", "duplicate"])
        self.assertEqual(doc_pair[0]["sha256"], doc_pair[1]["sha256"])
        # same size, different content: neither the probe nor the hash may merge them
        self.assertEqual(by_path["CIA_ABERTA/cia_aberta_fre_2025.zip"]["status"], "downloaded")
        self.assertNotEqual(by_path["CIA_ABERTA/cia_aberta_fre_2025.zip"]["sha256"],
                            doc_pair[0]["sha256"])

    def test_05_second_run_is_a_no_op(self):
        before = self.access[:]
        stored_before = self.stored_files()
        self.run_cli([])
        new_requests = [r for r in self.access if r not in before]
        bodies = [p for _, p, rng in new_requests if not rng]
        self.assertEqual(bodies, [], "second run should not download any payload")
        # ... and it must not remove anything either (a stale listed size used to
        # make a file look like its own duplicate and unlink it)
        self.assertEqual(self.stored_files(), stored_before)
        self.assertTrue(os.path.getsize(os.path.join(
            self.out, "geofiles.caxias.rs.gov.br", "files", "pub/quadras/45/LEIAME.txt")))
        for slug in ("dados.cvm.gov.br", "geofiles.caxias.rs.gov.br",
                     "dadosabertos.pgfn.gov.br", "valiprev.sp.gov.br",
                     "macau.rn.gov.br", "comissaodaverdade.al.sp.gov.br"):
            summary = self.summary(slug)
            self.assertEqual(summary["bytes_downloaded_this_run"], 0, slug)
            statuses = {r["status"] for r in self.manifest_rows(slug)}
            self.assertTrue(statuses <= {"unchanged", "duplicate"}, (slug, statuses))

    def test_06_resume_after_a_deleted_file(self):
        victim = os.path.join(self.out, "macau.rn.gov.br", "files",
                              "2013 - Diário Oficial de Macau/01 - Janeiro-2013",
                              "macau-2013-01-15.pdf")
        os.remove(victim)
        self.run_cli([])
        self.assertTrue(os.path.exists(victim))
        rows = {r["local_path"]: r for r in self.manifest_rows("macau.rn.gov.br")}
        self.assertEqual(rows["2013 - Diário Oficial de Macau/01 - Janeiro-2013/"
                              "macau-2013-01-15.pdf"]["status"], "downloaded")

    def test_07_refresh_redownloads(self):
        before = self.access[:]
        self.run_cli(["--site", "geofiles.caxias.rs.gov.br", "--refresh"])
        new = [p for _, p, rng in self.access[len(before):] if not rng]
        self.assertGreaterEqual(len(new), 3)
        self.assertGreater(self.summary("geofiles.caxias.rs.gov.br")["bytes_downloaded_this_run"], 0)

    def test_08_dupe_strategy_report_keeps_nothing(self):
        out = os.path.join(self.tmp, "out-report")
        scraper.main(["--config", self.config,
                      "--site", "comissaodaverdade.al.sp.gov.br", "--out", out,
                      "--rate", "0", "--quiet", "--dupe-strategy", "report",
                      "--rewrite", self.rewrite[0]])
        files = os.path.join(out, "comissaodaverdade.al.sp.gov.br", "files")
        names = set(os.listdir(files))
        # which copy of an identical pair "wins" depends on scheduling, but only
        # one of them may be stored
        self.assertEqual(len({"001-ArquivoCEMDP.pdf", "001-ArquivoCEMDP-devanir.pdf"} & names), 1)
        self.assertEqual(len(names), 6)
        import csv as csvmod
        with open(os.path.join(out, "comissaodaverdade.al.sp.gov.br", "_reports",
                               "duplicates.csv"), encoding="utf-8") as fh:
            rows = list(csvmod.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("not stored" in row["action"] for row in rows))
        self.assertTrue(all(row["duplicate_path"] == "" for row in rows))

    def test_09_dry_run_writes_no_payload_and_reports_candidates(self):
        out = os.path.join(self.tmp, "out-dry")
        scraper.main(["--config", self.config,
                      "--site", "valiprev.sp.gov.br", "--out", out,
                      "--rate", "0", "--quiet", "--dry-run", "--rewrite", self.rewrite[0]])
        root = os.path.join(out, "valiprev.sp.gov.br")
        self.assertEqual(os.listdir(os.path.join(root, "files")), [])
        rows = self.manifest_rows_dir(out, "valiprev.sp.gov.br")
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["status"] for r in rows}, {"dry_run"})
        import csv as csvmod
        with open(os.path.join(root, "_reports", "candidate_duplicates.csv"),
                  encoding="utf-8") as fh:
            candidates = list(csvmod.DictReader(fh))
        # the three DISPENSA files share a size and a name: 3 similar pairs
        # (pairwise heuristic), all of them verified by hash at download time
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(c["size"] == "3800" for c in candidates))
        self.assertTrue(all(c["name_similarity"] for c in candidates))
        self.assertTrue(os.path.isdir(os.path.join(root, "_index", "pages")))

    def test_10_limits_are_honoured(self):
        out = os.path.join(self.tmp, "out-limits")
        scraper.main(["--config", self.config,
                      "--site", "geofiles.caxias.rs.gov.br", "--out", out,
                      "--rate", "0", "--quiet", "--max-file-bytes", "5000",
                      "--rewrite", self.rewrite[0]])
        rows = {r["url"]: r for r in self.manifest_rows_dir(out, "geofiles.caxias.rs.gov.br")}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows["https://replica.local/caxias/pub/quadras/45/quadra_45.dwg"]
                         ["status"], "skipped")
        self.assertEqual(rows["https://replica.local/caxias/pub/quadras/45/quadra_45_lotes.kml"]
                         ["status"], "skipped")
        self.assertEqual(rows["https://replica.local/caxias/pub/quadras/45/LEIAME.txt"]["status"],
                         "downloaded")
        files = os.path.join(out, "geofiles.caxias.rs.gov.br", "files")
        self.assertTrue(os.path.exists(os.path.join(files, "pub/quadras/46/LEIAME.txt")))
        self.assertFalse(os.path.exists(os.path.join(files, "pub/quadras/45/quadra_45.dwg")))

    def test_11_dead_links_are_reported_not_fatal(self):
        text = read(os.path.join(REPLICA, "cvm/dados/index.html"))
        self.assertIn("ADM_CART", text)      # sanity: replica intact
        rows = self.manifest_rows("dados.cvm.gov.br")
        self.assertTrue(rows)
        self.assertTrue(all(r["error"] == "" for r in rows))

    # helper for the temp-dir runs
    def manifest_rows_dir(self, out: str, slug: str):
        import csv as csvmod
        with open(os.path.join(out, slug, "_reports", "manifest.csv"), encoding="utf-8") as fh:
            return list(csvmod.DictReader(fh))

    def test_11b_index_pages_cannot_escape_the_output_folder(self):
        # a listing URL whose path contains percent-encoded dot segments decodes to
        # '..' after rel_dir_for(); the audit-page writer must neutralise it
        site = scraper.SiteConfig(slug="escape", url="https://escape.local/dir/")
        cloner = scraper.SiteCloner(site=site, out_root=os.path.join(self.tmp, "escape-root"),
                                    fetcher=scraper.Fetcher(rate=0),
                                    dedup=scraper.DedupIndex())
        cloner._save_index_page("https://escape.local/%2e%2e/%2e%2e/pwned/", "<html>x</html>")
        written = []
        for dirpath, _, names in os.walk(os.path.join(self.tmp, "escape-root")):
            written += [os.path.join(dirpath, n) for n in names]
        self.assertTrue(written, "the page should still be stored somewhere")
        pages = os.path.join(cloner.index_root, "pages")
        for path in written:
            self.assertEqual(os.path.commonpath([pages, os.path.realpath(path)]),
                             os.path.realpath(pages))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))
        self.assertEqual(cloner.errors, [])          # neutralised, not refused

    def test_11c_escaped_index_page_is_refused_when_sanitising_cannot_save_it(self):
        # belt and braces: if a traversal ever survived sanitisation, the write is
        # refused instead of leaving the pages root (simulated here by disabling
        # sanitisation and feeding a path that walks up two levels)
        site = scraper.SiteConfig(slug="escape2", url="https://escape.local/dir/")
        cloner = scraper.SiteCloner(site=site, out_root=os.path.join(self.tmp, "escape-root2"),
                                    fetcher=scraper.Fetcher(rate=0),
                                    dedup=scraper.DedupIndex())
        keep = (scraper.rel_dir_for, scraper.sanitize_component)
        scraper.rel_dir_for = lambda url, base: "../../pwned2"
        scraper.sanitize_component = lambda name, max_len=180: name     # passthrough
        try:
            cloner._save_index_page("https://escape.local/dir/page.html", "<html>x</html>")
        finally:
            scraper.rel_dir_for, scraper.sanitize_component = keep
        stored = [os.path.join(d, n)
                  for d, _, names in os.walk(os.path.join(self.tmp, "escape-root2"))
                  for n in names]
        self.assertEqual(stored, [], stored)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned2")))
        self.assertEqual([e["stage"] for e in cloner.errors], ["index-page"])
        self.assertEqual(cloner.errors[0]["error"], "path escapes the pages root")

    def test_11d_total_byte_budget_is_charged_once(self):
        # the cap used to be charged twice (reservation + per-chunk), so a run
        # stopped at half of it; the replica payloads are ~5.1 MiB in total
        out = os.path.join(self.tmp, "out-budget")
        self.run_cli(["--max-total-bytes", "200000"], out=out)
        total = 0
        for slug in os.listdir(out):
            path = os.path.join(out, slug, "_reports", "summary.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    total += int(json.load(fh)["bytes_downloaded_this_run"])
        self.assertLessEqual(total, 200000)
        self.assertGreater(total, 150000, "the cap must not fire at half of itself")

    def test_12_include_filter_keeps_folders_traversable(self):
        # regression: --include must only filter documents, never prune the walk
        out = os.path.join(self.tmp, "out-include")
        self.run_cli(["--site", "geofiles.caxias.rs.gov.br",
                      "--include", r"\.dwg$"], out=out)
        names = sorted(os.listdir(os.path.join(out, "geofiles.caxias.rs.gov.br", "files",
                                               "pub", "quadras", "45")))
        self.assertEqual([n for n in names if n.endswith(".dwg")],
                         ["quadra_45.dwg"])
        self.assertFalse([n for n in names if n.endswith(".kml")])
        self.assertTrue(os.path.exists(os.path.join(
            out, "geofiles.caxias.rs.gov.br", "files", "pub", "quadras", "46",
            "quadra_46.dwg")))

    def test_12b_rerun_hint_replaces_the_actual_argv_in_the_readme(self):
        out = os.path.join(self.tmp, "out-hint")
        hint = "python3 scraper.py --config sites.json --site {slug}   # real crawl"
        self.run_cli(["--site", "dados.cvm.gov.br", "--rerun-hint", hint], out=out)
        readme = read(os.path.join(out, "dados.cvm.gov.br", "README.md"))
        self.assertIn("python3 scraper.py --config sites.json --site dados.cvm.gov.br",
                      readme)
        self.assertNotIn("{slug}", readme)
        # the wrapper's own temp config must not leak into the instruction
        self.assertNotIn(self.config, readme)

    def test_13_exclude_filter_prunes_a_subtree(self):
        out = os.path.join(self.tmp, "out-exclude")
        self.run_cli(["--site", "geofiles.caxias.rs.gov.br", "--exclude", r"/46/"], out=out)
        files = os.path.join(out, "geofiles.caxias.rs.gov.br", "files", "pub", "quadras")
        self.assertFalse(os.path.exists(os.path.join(files, "46")))
        self.assertTrue(os.path.exists(os.path.join(files, "45", "LEIAME.txt")))


class TestRobotsAndErrors(unittest.TestCase):
    """robots.txt, 404s and mid-stream failures must be polite and non-fatal."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="indexclone-robots-")
        os.makedirs(os.path.join(cls.tmp, "docs"))
        with open(os.path.join(cls.tmp, "robots.txt"), "w") as fh:
            fh.write("User-agent: *\nDisallow: /docs/private/\n")
        os.makedirs(os.path.join(cls.tmp, "docs/private"))
        with open(os.path.join(cls.tmp, "docs/private/secret.txt"), "w") as fh:
            fh.write("secret")
        with open(os.path.join(cls.tmp, "docs/public.txt"), "w") as fh:
            fh.write("public")
        listing = """<html><head><title>Index of /docs/</title></head><body>
        <a href="public.txt">public.txt</a> 1.0K
        <a href="private/">private/</a>
        <a href="gone.txt">gone.txt</a>
        </body></html>"""
        with open(os.path.join(cls.tmp, "docs/index.html"), "w") as fh:
            fh.write(listing)
        cls.httpd, cls.base, _ = replica_server.serve_in_thread(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_robots_is_respected_and_404_is_recorded(self):
        out = os.path.join(self.tmp, "out")
        code = scraper.main(["--url", "{}/docs/".format(self.base), "--slug", "robotstest",
                             "--out", out, "--rate", "0", "--quiet"])
        self.assertEqual(code, 1)                       # finished, with per-file errors
        files = os.path.join(out, "robotstest", "files")
        self.assertTrue(os.path.exists(os.path.join(files, "public.txt")))
        self.assertFalse(os.path.exists(os.path.join(files, "private/secret.txt")))
        errors = read(os.path.join(out, "robotstest", "_reports", "errors.log"))
        self.assertIn("disallowed", errors)
        self.assertIn("gone.txt", errors)

    def test_ignore_robots_downloads_everything(self):
        out = os.path.join(self.tmp, "out2")
        scraper.main(["--url", "{}/docs/".format(self.base), "--slug", "robotstest2",
                      "--out", out, "--rate", "0", "--quiet", "--ignore-robots"])
        files = os.path.join(out, "robotstest2", "files")
        self.assertTrue(os.path.exists(os.path.join(files, "private/secret.txt")))


def load_tests(loader, tests, pattern):
    return tests


# ==========================================================================
# the byte budget must bound what is really fetched
# ==========================================================================


class _FakeStream:
    """Minimal stand-in for Fetcher's response object."""

    def __init__(self, body: bytes, headers=None, status: int = 200):
        self._body, self._pos = body, 0
        self.headers, self.status, self.url = headers or {}, status, ""

    def read(self, size: int) -> bytes:
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestBudgetAccounting(unittest.TestCase):
    """A short reservation must not let a stream run past --max-total-bytes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="indexclone-budget-unit-")
        self.budget = scraper.ByteBudget(50000)
        self.cloner = scraper.SiteCloner(
            site=scraper.SiteConfig(slug="b", url="https://b.local/"),
            out_root=self.tmp, fetcher=scraper.Fetcher(rate=0),
            dedup=scraper.DedupIndex(), budget=self.budget)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entry(self, size):
        return scraper.Entry(url="https://b.local/f.bin", name="f.bin", is_dir=False,
                             size=size, origin="probe")

    def test_bytes_beyond_a_short_listed_size_are_charged(self):
        self.budget = scraper.ByteBudget(100000)     # room for the whole body
        self.cloner.budget = self.budget
        entry = self._entry(1024)                    # listing says 1.0K ...
        body = b"x" * 61440                          # ... the server sends 60 KiB
        self.cloner._open_stream = lambda url, headers=None: _FakeStream(
            body, {"Content-Length": str(len(body))})
        self.assertTrue(self.cloner._check_budget(entry, "f.bin", None))
        self.assertEqual(self.budget.used, 1024)     # only the listed size reserved
        tmp = os.path.join(self.tmp, "f.bin.part")
        written, _, _ = self.cloner._download(entry, tmp, None, "f.bin")
        self.assertEqual(written, len(body))
        self.assertEqual(self.budget.used, len(body), "the excess must be charged")

    def test_response_length_is_reserved_when_the_listing_publishes_no_size(self):
        entry = self._entry(None)
        body = b"y" * 4096
        self.cloner._open_stream = lambda url, headers=None: _FakeStream(
            body, {"Content-Length": str(len(body))})
        written, _, _ = self.cloner._download(entry, os.path.join(self.tmp, "g.part"),
                                             None, "g.bin")
        self.assertEqual(written, len(body))
        self.assertEqual(self.budget.used, len(body))    # reserved, not double charged

    def test_a_stream_of_unknown_length_is_charged_as_it_arrives(self):
        entry = self._entry(None)
        body = b"z" * 4096
        self.cloner._open_stream = lambda url, headers=None: _FakeStream(body)
        written, _, _ = self.cloner._download(entry, os.path.join(self.tmp, "h.part"),
                                             None, "h.bin")
        self.assertEqual(written, len(body))
        self.assertEqual(self.budget.used, len(body))

    def test_the_stream_stops_at_the_cap_and_records_a_skip(self):
        self.budget = scraper.ByteBudget(20000)
        self.cloner.budget = self.budget
        entry = self._entry(1024)
        body = b"x" * 61440
        self.cloner._open_stream = lambda url, headers=None: _FakeStream(
            body, {"Content-Length": str(len(body))})
        self.assertTrue(self.cloner._check_budget(entry, "f.bin", None))
        with self.assertRaises(scraper._SkipRequest):
            self.cloner._download(entry, os.path.join(self.tmp, "i.part"), None, "f.bin")
        self.assertLessEqual(self.budget.used, 20000)
        self.assertEqual([r["status"] for r in self.cloner.records], ["skipped"])
        self.assertIn("total byte budget", self.cloner.records[0]["error"])


class TestLyingListingBudget(unittest.TestCase):
    """End to end: a listing that understates a size must not defeat the cap."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="indexclone-budget-")
        os.makedirs(os.path.join(cls.tmp, "docs"))
        with open(os.path.join(cls.tmp, "docs", "big.bin"), "wb") as fh:
            fh.write(b"x" * 61440)                    # 60 KiB on the wire ...
        with open(os.path.join(cls.tmp, "docs", "index.html"), "w", encoding="utf-8") as fh:
            fh.write('<html><head><title>Index of /docs/</title></head><body>'
                     '<a href="big.bin">big.bin</a> 1.0K</body></html>')   # ... listed as 1K
        cls.httpd, cls.base, _ = replica_server.serve_in_thread(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, cap: int, slug: str):
        out = os.path.join(self.tmp, slug)
        code = scraper.main(["--url", "{}/docs/".format(self.base), "--slug", slug,
                             "--out", out, "--rate", "0", "--quiet",
                             "--max-total-bytes", str(cap)])
        summary = json.loads(read(os.path.join(out, slug, "_reports", "summary.json")))
        with open(os.path.join(out, slug, "_reports", "manifest.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return code, summary, rows, os.path.join(out, slug, "files", "big.bin")

    def test_a_cap_smaller_than_the_real_body_skips_the_file(self):
        _, summary, rows, stored = self._run(20000, "capped")
        self.assertEqual(summary["bytes_downloaded_this_run"], 0)
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertIn("total byte budget", rows[0]["error"])
        self.assertFalse(os.path.exists(stored), "nothing may be stored past the cap")

    def test_a_cap_large_enough_downloads_it_and_accounts_for_every_byte(self):
        _, summary, rows, stored = self._run(100000, "roomy")
        self.assertEqual(summary["bytes_downloaded_this_run"], 61440)
        self.assertEqual(rows[0]["status"], "downloaded")
        self.assertTrue(os.path.exists(stored))
        self.assertEqual(os.path.getsize(stored), 61440)


if __name__ == "__main__":
    unittest.main(verbosity=2)
