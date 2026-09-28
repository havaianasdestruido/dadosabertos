# Valiprev — certidões e audiências (replica)

* source listing: <https://replica.local/valiprev/>
* output folder: `valiprev.sp.gov.br`
* cloned at: 2026-09-28T13:07:21Z
* documents discovered: **8** (8 fetched this run, 2.1 MiB unique bytes stored)
* downloaded in this run: 2.1 MiB
* duplicates collapsed: **2** (7.4 KiB)
* status: downloaded=6, duplicate=2

## Layout

```
valiprev.sp.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

Apache table dialect: names are truncated in the visible cell and recovered from the link title/URL. The three DISPENSA files share identical bytes (a real pattern on this site).

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-8hwai92t/replica_sites.json --site valiprev.sp.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:39505/ --dupe-strategy hardlink
```
