# Portal Dados Abertos CVM (replica)

* source listing: <https://replica.local/cvm/dados/>
* output folder: `dados.cvm.gov.br`
* cloned at: 2026-09-28T13:07:21Z
* documents discovered: **5** (5 fetched this run, 307.0 KiB unique bytes stored)
* downloaded in this run: 403.0 KiB
* duplicates collapsed: **1** (96.0 KiB)
* status: downloaded=4, duplicate=1

## Layout

```
dados.cvm.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

Apache <pre> index with dates and human sizes; 'CIA_ABERTA' and 'FIDC' both publish a 96 KiB 'doc' archive with identical bytes, while 'cia_aberta_fre_2025.zip' has the same size with different content — the deduper must collapse the first pair only.

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-8hwai92t/replica_sites.json --site dados.cvm.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:39505/ --dupe-strategy hardlink
```
