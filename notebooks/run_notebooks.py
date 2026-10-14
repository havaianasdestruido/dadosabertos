#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Execute the indexclone notebooks **in place** and keep every output.

    python3 notebooks/run_notebooks.py                # all notebooks
    python3 notebooks/run_notebooks.py 01 03          # a subset, matched by name
    python3 notebooks/run_notebooks.py --list

The whole point of this folder is that the committed ``.ipynb`` files carry their
rendered graphs and tables, so the analysis is readable on GitHub without
running anything. This runner therefore:

* executes each notebook with its own kernel, writing the results **back into the
  same file** (no ``--to``, no temporary copy, no ``ClearOutputPreprocessor``);
* **never strips** outputs, execution counts or metadata, before or after the run;
* verifies afterwards that each notebook really does contain embedded images and
  fails loudly if one of them produced none.

Outputs are only ever *added*, so re-running is safe and idempotent.

Requirements: ``pip install -r notebooks/requirements-notebooks.txt``
"""

from __future__ import annotations

import argparse
import os
import sys

import nbformat
from nbclient import NotebookClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: long enough for the byte-level verification in 04 (it re-hashes every file)
TIMEOUT = 900


def notebook_paths(args) -> list[str]:
    names = sorted(f for f in os.listdir(HERE) if f.endswith(".ipynb"))
    if args.notebooks:
        picked = []
        for wanted in args.notebooks:
            match = [n for n in names if n.startswith(wanted) or n == wanted]
            if not match:
                raise SystemExit(f"no notebook in notebooks/ matches {wanted!r}")
            picked.extend(match)
        # keep order, drop duplicates
        return sorted(set(picked))
    return names


def count_images(nb) -> int:
    n = 0
    for cell in nb.cells:
        for out in cell.get("outputs", []):
            if not isinstance(out, dict):
                continue
            data = out.get("data", {}) or {}
            if any(str(k).startswith("image/") for k in data):
                n += 1
    return n


def run_one(path: str) -> bool:
    name = os.path.basename(path)
    nb = nbformat.read(path, as_version=4)

    client = NotebookClient(
        nb,
        timeout=TIMEOUT,
        kernel_name="python3",
        resources={"metadata": {"path": ROOT}},
        allow_errors=False,
    )
    print(f"→ executing {name} …", flush=True)
    client.execute()

    images = count_images(nb)
    errors = sum(1 for c in nb.cells for o in c.get("outputs", []) if o.get("output_type") == "error")

    # write back, outputs intact
    nbformat.write(nb, path)
    print(
        f"   saved {name}: {len(nb.cells)} cells, {images} embedded images, {errors} error outputs",
        flush=True,
    )
    if errors:
        print(f"   !! {name} finished with {errors} error output(s)", file=sys.stderr)
        return False
    if images == 0:
        print(f"   !! {name} produced no images — check the plotting cells", file=sys.stderr)
        return False
    return True


def check_only(paths) -> int:
    """Assert that every notebook still carries its outputs (images included).

    Nothing in this repository strips notebook outputs, but a stray
    ``nbstripout`` / ``jupyter nbconvert --clear-output`` in a hook or CI job
    would silently empty them. Run this after any test run to prove it did not
    happen.
    """
    bad = 0
    for name in paths:
        nb = nbformat.read(os.path.join(HERE, name), as_version=4)
        cells_with_output = sum(1 for c in nb.cells if c.get("outputs"))
        images = count_images(nb)
        exec_counts = sum(1 for c in nb.cells if c.get("execution_count"))
        ok = cells_with_output > 0 and images > 0
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} {name}: {cells_with_output} cells with output, "
            f"{exec_counts} executed, {images} images"
        )
    if bad:
        print(f"\n{bad} notebook(s) lost their outputs — re-run this script.", file=sys.stderr)
        return 1
    print(f"\nall {len(paths)} notebooks still carry their outputs and images.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("notebooks", nargs="*", help="name prefixes, e.g. 01 or 03_duplicates.ipynb")
    ap.add_argument("--list", action="store_true", help="list the notebooks and exit")
    ap.add_argument(
        "--check",
        action="store_true",
        help="do not execute: verify every notebook still has outputs and images",
    )
    args = ap.parse_args()

    paths = notebook_paths(args)
    if args.list:
        for p in paths:
            print(p)
        return 0
    if args.check:
        return check_only(paths)

    print(f"repository root: {ROOT}\n")
    ok = True
    for name in paths:
        ok &= run_one(os.path.join(HERE, name))

    print()
    if ok:
        print(f"all {len(paths)} notebooks executed — outputs and images are embedded in the files.")
        return 0
    print("one or more notebooks needs attention (see above).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
