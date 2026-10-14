# Notebooks — exploring what the crawler produced

Six Jupyter notebooks that read the report files in `outputs/` and
`outputs-replica/` and turn them into tables and graphs. **They are committed
with their outputs already rendered**, so every figure is readable straight on
GitHub — you do not have to run anything to see the analysis.

They are strictly read-only consumers: nothing here fetches a URL, and nothing
writes into the report folders, so exploring a run can never corrupt it.

| notebook | what it answers |
| --- | --- |
| [`00_data_map.ipynb`](00_data_map.ipynb) | what is in this checkout: which report files exist, how big they are, how the two result trees differ, and where the audit trail lives |
| [`01_inventory_explorer.ipynb`](01_inventory_explorer.ipynb) | the **127 real documents** the portals publish — counts per portal, metadata coverage, size distribution, file types, publication timeline, the heavyweights |
| [`02_replica_run.ipynb`](02_replica_run.ipynb) | the **full run** over the local replica: outcome per document, bytes on the wire, a line-by-line byte reconciliation, range-probe versus SHA-256, throughput |
| [`03_duplicates.ipynb`](03_duplicates.ipynb) | which documents are re-publications of each other — verified collapses versus the dry-run heuristic, duplicate groups, renaming habits, and what to re-check |
| [`04_audit_health.ipynb`](04_audit_health.ipynb) | can the reports be trusted — eight checks, including re-hashing every stored file against its recorded SHA-256 |
| [`05_explorer_playground.ipynb`](05_explorer_playground.ipynb) | a scratch pad: plain-Python knobs (`SITES`, `EXTENSIONS`, `MIN_BYTES`, `STATUS`, …) that re-slice any of the above |

## Running them

```bash
python3 -m pip install -r notebooks/requirements-notebooks.txt

python3 notebooks/run_notebooks.py            # execute all six, in place
python3 notebooks/run_notebooks.py 03         # just the ones matching "03"
python3 notebooks/run_notebooks.py --list
python3 notebooks/run_notebooks.py --check    # verify outputs are still there
```

`run_notebooks.py` writes the results **back into the same `.ipynb` file** and
never clears outputs, execution counts or metadata — before, during or after the
run. Re-running is safe and idempotent: outputs are only ever added. If a
notebook somehow produces no figure, the runner says so and exits non-zero.

The notebooks import [`dadosabertos_data.py`](dadosabertos_data.py), a small
shared loader (find the repo root, read every `manifest.csv` / `summary.json` /
`state.json` / `duplicates.csv`, hash files on disk). It locates the checkout by
walking up from the current directory, so a notebook works whether the kernel
started in the repository root or in `notebooks/`.

## Three findings worth knowing before you read the numbers

1. **"Bytes downloaded" means two different things.**
   `global.json → totals.bytes_downloaded` is the bytes *stored* (5.1 MiB); the
   per-site `summary.json → bytes_downloaded_this_run` is everything that crossed
   the network (5.5 MiB). The 374 KiB gap is duplicates that had to be fetched
   before their SHA-256 could collapse them. Notebook 02 reconciles all of it.

2. **Of the 1.4 MiB "saved", only 1.0 MiB was traffic actually avoided.** Those
   are the three PGFN archives the range probe recognised from two 64 KiB
   requests each. The rest was downloaded and then collapsed.

3. **The published sizes are rounded, not exact.** Apache-style listings say
   `9M`, `3.8M`, `387K`, and the scraper parses those into exact-looking byte
   counts. Six different yearly CVM archives all publish `9M`, so "same listed
   size" is very weak evidence for CVM (a 1 MiB bucket) and much stronger for
   Valiprev (a 1 KiB bucket). Notebook 01 §3b quantifies it and notebook 03
   flags every cluster accordingly.

## Notes

* `outputs-replica/` sizes are real `Content-Length` bytes and are exact;
  `outputs/` sizes are parsed from listing text and are rounded.
* The replica run hard-linked duplicates, but a Git checkout restores each path
  as its own file, so `nlink` is 1 everywhere. Compare the `sha256` columns
  instead — notebook 04 does exactly that.
* `geofiles.caxias.rs.gov.br` only exists in `outputs-replica/`; the host could
  not be read from the sandbox that produced `outputs/`.
