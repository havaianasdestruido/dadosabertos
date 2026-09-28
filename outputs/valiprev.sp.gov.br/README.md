# valiprev.sp.gov.br

* source listing: <https://valiprev.sp.gov.br/uploads/paginas/certidoes/pdf/>
* output folder: `valiprev.sp.gov.br`
* cloned at: 2026-09-28T17:33:17Z
* documents discovered: **53** (0 fetched this run, 0 B unique bytes stored)
* downloaded in this run: 0 B
* duplicates collapsed: **0** (0 B)
* status: dry_run=53

## Layout

```
valiprev.sp.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

_(none)_

Re-run / resume with:

```bash
python3 scraper.py --config sites.json --site valiprev.sp.gov.br    # the real crawl (needs internet; downloads the documents)
python3 tools/snapshot_inventory.py --out outputs    # rebuild this offline inventory
```
