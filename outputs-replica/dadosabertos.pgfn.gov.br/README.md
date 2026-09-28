# Dados Abertos PGFN (replica)

* source listing: <https://replica.local/pgfn/>
* output folder: `dadosabertos.pgfn.gov.br`
* cloned at: 2026-09-28T13:05:53Z
* documents discovered: **7** (7 fetched this run, 1.1 MiB unique bytes stored)
* downloaded in this run: 1.1 MiB
* duplicates collapsed: **3** (1.0 MiB)
* status: downloaded=4, duplicate=3

## Layout

```
dadosabertos.pgfn.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

Bullet-style index without sizes: the crawler decides file-vs-directory by probing. The FGTS archive is repeated in every quarter -> collapsed by the byte probe.

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-sj49agkl/replica_sites.json --site dadosabertos.pgfn.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:33313/ --dupe-strategy hardlink
```
