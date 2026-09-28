# valiprev.sp.gov.br

* source listing: <https://valiprev.sp.gov.br/uploads/paginas/certidoes/pdf/>
* output folder: `valiprev.sp.gov.br`
* cloned at: 2026-09-28T13:05:52Z
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
python3 scraper.py --config /tmp/indexclone-snapshots-nyxmz0ju/sites.json --site valiprev.sp.gov.br --out outputs --jobs 4 --rate 0.0 --rewrite https://dadosabertos.pgfn.gov.br/=http://127.0.0.1:44665/dadosabertos.pgfn.gov.br/ --rewrite https://macau.rn.gov.br/=http://127.0.0.1:44665/macau.rn.gov.br/ --rewrite https://comissaodaverdade.al.sp.gov.br/=http://127.0.0.1:44665/comissaodaverdade.al.sp.gov.br/ --rewrite https://dados.cvm.gov.br/=http://127.0.0.1:44665/dados.cvm.gov.br/ --rewrite https://valiprev.sp.gov.br/=http://127.0.0.1:44665/valiprev.sp.gov.br/ --dupe-strategy hardlink --dry-run
```
