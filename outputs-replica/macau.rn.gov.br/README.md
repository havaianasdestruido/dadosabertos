# Diário Oficial de Macau (replica)

* source listing: <https://replica.local/macau/diario/>
* output folder: `macau.rn.gov.br`
* cloned at: 2026-09-28T13:07:21Z
* documents discovered: **5** (5 fetched this run, 540.0 KiB unique bytes stored)
* downloaded in this run: 690.0 KiB
* duplicates collapsed: **1** (150.0 KiB)
* status: downloaded=4, duplicate=1

## Layout

```
macau.rn.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

Pure link farm: every href lacks a trailing slash, a date and a size, so each one is probed. Two 2013 editions are byte-identical under different names.

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-8hwai92t/replica_sites.json --site macau.rn.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:39505/ --dupe-strategy hardlink
```
