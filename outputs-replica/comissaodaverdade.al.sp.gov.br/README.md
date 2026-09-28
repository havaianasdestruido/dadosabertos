# Comissão da Verdade ALESP — acervo (replica)

* source listing: <https://replica.local/comissao/upload/>
* output folder: `comissaodaverdade.al.sp.gov.br`
* cloned at: 2026-09-28T18:05:14Z
* documents discovered: **8** (8 fetched this run, 626.0 KiB unique bytes stored)
* downloaded in this run: 747.0 KiB
* duplicates collapsed: **2** (121.0 KiB)
* status: downloaded=6, duplicate=2

## Layout

```
comissaodaverdade.al.sp.gov.br/
├── files/                 # the mirrored documents (mirror of the listing tree)
├── _index/pages/          # copy of every listing page as fetched (audit trail)
└── _reports/              # manifest.json/csv, duplicates.csv, errors.log, state.json
```

## Notes

Links without metadata; content-identical documents appear under different names ('001-ArquivoCEMDP.pdf' vs '...-devanir.pdf'), which is exactly what the SHA-256 deduper is for.

Re-run / resume with:

```bash
python3 scraper.py --config /tmp/indexclone-demo-uylb0xki/replica_sites.json --site comissaodaverdade.al.sp.gov.br --out outputs-replica --jobs 4 --rate 0.0 --rewrite https://replica.local/=http://127.0.0.1:38471/ --dupe-strategy hardlink
```
