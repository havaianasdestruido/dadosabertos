#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build an inventory of the six real listings from the committed snapshots.

This sandbox has no egress to the .gov.br hosts (only GitHub/PyPI/npm are
reachable), so the payloads themselves cannot be downloaded here. What *can* be
done offline — and is done here — is to run the real crawler over the captured
listing pages:

  * the listing pages are rendered to a local tree (tools/build_snapshot.py),
  * served over HTTP by tests/replica_server.py,
  * crawled by scraper.py with --dry-run and --rewrite, so the crawler sees the
    original URLs while the bytes come from disk,
  * the manifest, the duplicate candidates and the listing pages end up under
    `outputs/<slug>/`, exactly as they would on a real run.

Swap `--dry-run` for a real network run (see README) to download the documents.

    python3 tools/snapshot_inventory.py            # writes ./outputs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import scraper                                            # noqa: E402
from tests import replica_server                          # noqa: E402
sys.path.insert(0, HERE)
import build_snapshot                                     # noqa: E402

DEPTHS = {
    # the snapshots cover the entry listing + one level for some sites; a real
    # run uses sites.json, whose max_depth reaches the bottom of each tree
    "dadosabertos.pgfn.gov.br": 0,
    "valiprev.sp.gov.br": 0,
    "geofiles.caxias.rs.gov.br": 0,
    "macau.rn.gov.br": 0,
    "comissaodaverdade.al.sp.gov.br": 0,
    "dados.cvm.gov.br": 0,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--out", default=os.path.join(ROOT, "outputs"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--rate", type=float, default=0.0,
                    help="requests/second/host (0 = as fast as the loopback allows)")
    ap.add_argument("--keep-snapshot", action="store_true",
                    help="keep the rendered snapshot tree next to the output")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="indexclone-snapshots-")
    try:
        build_snapshot.build(tmp)
        httpd, base, _ = replica_server.serve_in_thread(tmp)
        cfg_path = os.path.join(tmp, "sites.json")
        sites = build_snapshot.sites_for_config()
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"defaults": {"max_depth": 0}, "sites": sites}, fh,
                      indent=1, ensure_ascii=False)
        rewrites = []
        for site in sites:
            rewrites += ["--rewrite", "https://{}/={}/{}/".format(
                site["slug"], base.rstrip("/"), site["slug"])]
        args_list = ["--config", cfg_path, "--all", "--out", args.out,
                     "--jobs", str(args.jobs), "--rate", str(args.rate), "--dry-run",
                     "--quiet"] + rewrites
        print("serving the snapshot tree at {}".format(base))
        code = scraper.main(args_list)
        print("inventory written to {}".format(args.out))
        httpd.shutdown()
        httpd.server_close()
        if args.keep_snapshot:
            dest = os.path.join(args.out, "_snapshot_tree")
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(tmp, dest, ignore=shutil.ignore_patterns("sites.json"))
            print("snapshot tree kept in {}".format(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
