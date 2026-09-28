# dadosabertos.pgfn.gov.br

* source listing: <https://dadosabertos.pgfn.gov.br/>
* output folder: `dadosabertos.pgfn.gov.br`
* cloned at: 2026-09-28T13:07:44Z
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
python3 scraper.py --config /tmp/indexclone-snapshots-p2ua_gbr/sites.json --site dadosabertos.pgfn.gov.br --out outputs --jobs 4 --rate 0.0 --rewrite https://dadosabertos.pgfn.gov.br/=http://127.0.0.1:43981/dadosabertos.pgfn.gov.br/ --rewrite https://macau.rn.gov.br/=http://127.0.0.1:43981/macau.rn.gov.br/ --rewrite https://comissaodaverdade.al.sp.gov.br/=http://127.0.0.1:43981/comissaodaverdade.al.sp.gov.br/ --rewrite https://dados.cvm.gov.br/=http://127.0.0.1:43981/dados.cvm.gov.br/ --rewrite https://valiprev.sp.gov.br/=http://127.0.0.1:43981/valiprev.sp.gov.br/ --dupe-strategy hardlink --dry-run
```
