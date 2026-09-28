# Real listing snapshots

These files record what the six target listings looked like on **2026-09-28**, as
read through the platform's page fetcher (the sandbox itself has no egress to
these hosts, see the top-level README).

Each `*.tsv` is a capture of one listing page:

```
kind <TAB> name <TAB> url <TAB> last-modified <TAB> size
```

* `kind`      – `dir` or `file` (as shown by the listing)
* `name`      – the link text / file name as published
* `url`       – absolute URL (empty means "derive from the site root + name")
* date/size   – copied verbatim from the listing (`3.8M`, `27-Feb-2020 16:28`, …)

`tools/build_snapshot.py` renders these captures into an on-disk replica of the
listing tree, using the dialect of each site (Apache `<pre>`, Apache 2.4 table,
metadata-less link list). `tools/snapshot_inventory.py` then serves it over HTTP
and runs the real crawler against it with `--dry-run`, producing the inventories
under `outputs/`.

## Coverage and known limits

| site | capture | coverage |
| --- | --- | --- |
| dadosabertos.pgfn.gov.br | `pgfn_root.tsv`, `pgfn_2025_trimestre_04.tsv` | full root (27 quarters + 3 archives + 1 folder) and one quarter |
| valiprev.sp.gov.br | `valiprev_pdf.tsv` | 54 of ~120 files of the `certidoes/pdf` listing (the duplicate families are all present) |
| geofiles.caxias.rs.gov.br | – | the fetcher could not read this host at all (tried twice); only the offline replica covers the nginx dialect |
| macau.rn.gov.br | `macau_root.tsv`, `macau_2013.tsv`, `macau_2013_03.tsv` | full root (2013–2026), the 2013 month list and the 8 editions of March 2013 |
| comissaodaverdade.al.sp.gov.br | `comissao_upload.tsv` | the first 42 of ~3 000 documents |
| dados.cvm.gov.br | `cvm_root.tsv`, `cvm_cad.tsv`, `cvm_cia_aberta_dados.tsv`, `cvm_cia_aberta_doc.tsv`, `cvm_cia_aberta_doc_dfp_dados.tsv` | full root (24 datasets), `ADM_CART/CAD`, `CIA_ABERTA/CAD/DADOS`, `CIA_ABERTA/DOC` and the 17 yearly DFP archives |

Names, dates and sizes are exactly as published; the HTML is generated, so
byte-level markup is *not* a copy of the server response (the listing
*dialects* are reproduced from what the fetcher returned). For a byte-faithful
snapshot of every page, run the crawler with real network access — it saves
each listing page under `<output>/_index/pages/` as it goes.
