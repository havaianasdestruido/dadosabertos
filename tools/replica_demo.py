#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end demonstration: clone the offline replica of the six sites.

    python3 tools/replica_demo.py                 # one run, into outputs-replica/
    python3 tools/replica_demo.py --second-run    # + prove the resume is a no-op
    python3 tools/replica_demo.py --out /tmp/x    # somewhere else

The replica (tests/make_replica.py) reproduces the six real listing dialects and
serves documents over real HTTP, so this is a genuine run of the crawler: real
bytes are fetched, hashed, de-duplicated, hard-linked and reported — only the
*hosts* are local. Point `sites.json` at the real URLs and drop `--rewrite` for
the real thing.
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
from tests import make_replica, replica_server            # noqa: E402

REPLICA_DIR = os.path.join(ROOT, "tests", "replica")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="run the six-site replica demo")
    ap.add_argument("--out", default=os.path.join(ROOT, "outputs-replica"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--rate", type=float, default=0.0)
    ap.add_argument("--second-run", action="store_true",
                    help="run again afterwards (in a temp dir) to show the no-op")
    args = ap.parse_args(argv)

    make_replica.build(REPLICA_DIR)
    httpd, base, _ = replica_server.serve_in_thread(REPLICA_DIR)
    tmp = tempfile.mkdtemp(prefix="indexclone-demo-")
    try:
        site_list = make_replica.build(os.path.join(tmp, "replica"))
        cfg = os.path.join(tmp, "replica_sites.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"defaults": {"max_depth": 5}, "sites": site_list}, fh,
                      indent=1, ensure_ascii=False)
        rewrite = "https://replica.local/={}/".format(base.rstrip("/"))
        print("replica served at {}".format(base))
        code = scraper.main(["--config", cfg, "--all", "--out", args.out,
                             "--jobs", str(args.jobs), "--rate", str(args.rate),
                             "--rewrite", rewrite])
        if args.second_run:
            print("\n--- second run (should download nothing) ---")
            again = os.path.join(tmp, "second")
            shutil.copytree(args.out, again)
            scraper.main(["--config", cfg, "--all", "--out", again,
                          "--jobs", str(args.jobs), "--rate", str(args.rate),
                          "--rewrite", rewrite])
            total = 0
            for slug in os.listdir(again):
                path = os.path.join(again, slug, "_reports", "summary.json")
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        total += int(json.load(fh).get("bytes_downloaded_this_run") or 0)
            print("second run downloaded {} bytes in total".format(total))
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    return code  # noqa: B012


if __name__ == "__main__":
    sys.exit(main())
