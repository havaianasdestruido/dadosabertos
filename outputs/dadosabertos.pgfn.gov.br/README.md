# dadosabertos.pgfn.gov.br

* source listing: <https://dadosabertos.pgfn.gov.br/>
* output folder: `dadosabertos.pgfn.gov.br`
* cloned at: 2026-09-28T18:05:13Z
* documents discovered: **6** (0 fetched this run, 0 B unique bytes stored)
* downloaded in this run: 0 B
* duplicates collapsed: **0** (0 B)
* status: dry_run=6

## Layout

```
dadosabertos.pgfn.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

_(none)_

Re-run / resume with:

```bash
python3 scraper.py --config sites.json --site dadosabertos.pgfn.gov.br    # the real crawl (needs internet; downloads the documents)
python3 tools/snapshot_inventory.py --out outputs    # rebuild this offline inventory
```
