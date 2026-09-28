# Geofiles Caxias do Sul — quadras (replica)

* source listing: <https://replica.local/caxias/>
* output folder: `geofiles.caxias.rs.gov.br`
* cloned at: 2026-09-28T18:05:14Z
* documents discovered: **6** (6 fetched this run, 482.3 KiB unique bytes stored)
* downloaded in this run: 482.3 KiB
* duplicates collapsed: **0** (0 B)
* status: downloaded=6

## Layout

```
geofiles.caxias.rs.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

nginx autoindex keeps the literal '..' anchor; directories are reached through a 301 redirect to the trailing-slash URL.

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-uylb0xki/replica_sites.json --site geofiles.caxias.rs.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:38471/ --dupe-strategy hardlink
```
