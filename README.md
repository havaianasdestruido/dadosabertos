# indexclone — mirroring + de-duplicating public document listings

`scraper.py` walks public directory listings recursively, downloads every
document it finds, de-duplicates them, and writes one auditable folder per site.

The six listings it is configured for (`sites.json`):

| # | site | listing | shape |
| --- | --- | --- | --- |
| 1 | `dadosabertos.pgfn.gov.br` | `/` | quarterly folders + 3 ZIPs, **re-published in every quarter** |
| 2 | `valiprev.sp.gov.br` | `/uploads/paginas/certidoes/pdf/` | one flat Apache folder, many `…1.pdf` re-publications |
| 3 | `geofiles.caxias.rs.gov.br` | `/pub/quadras/45/` | nginx autoindex, nested quadra folders |
| 4 | `macau.rn.gov.br` | `/diario/` | 13 years × 12 months of daily PDFs |
| 5 | `comissaodaverdade.al.sp.gov.br` | `/upload/` | ~3 000 flat files, several > 100 MB |
| 6 | `dados.cvm.gov.br` | `/dados/` | ~24 datasets × years, same archive in several datasets |

## Read this first: what was actually run here

This checkout was produced in a sandbox with **no network egress to those
hosts** (only GitHub/PyPI/npm are reachable; every `*.gov.br` connection is
reset). The crawler itself is complete and was run end-to-end, but the payloads
in `outputs-replica/` come from a **local replica** of the six sites, and the
inventories in `outputs/` come from **listing pages captured through the
platform's page reader** on 2026-09-28. Concretely:

| what | where | how it was produced |
| --- | --- | --- |
| real listing inventory (URLs, sizes, dates, duplicate candidates) | `outputs/<slug>/` | `python3 tools/snapshot_inventory.py` — crawls the captured listings (offline) |
| full run, real bytes over HTTP: download → hash → dedupe → hardlink → report → resume | `outputs-replica/<slug>/` | `python3 tools/replica_demo.py --second-run` |
| listing captures (verbatim names/dates/sizes) | `tests/fixtures/real/*.tsv` | read from the six sites; see that folder's README for coverage |
| offline replica of the six dialects | generated (git-ignored) | `python3 tests/make_replica.py` |

On a machine with normal internet access the only change is the config — there
is no sandbox-specific logic in the scraper:

```bash
python3 scraper.py --config sites.json --all --out outputs      # the real thing
```

`geofiles.caxias.rs.gov.br` could not be read at all from this sandbox (two
attempts, with and without `?C=N;O=A`), so no listing was captured for it and
`outputs/geofiles.caxias.rs.gov.br/` is absent; the nginx dialect it is assumed
to use is covered by the replica. Run the command above (or `--dry-run`) from a
normal network to see what the host really publishes before mirroring it.

## Usage

```bash
# everything in sites.json
python3 scraper.py --config sites.json --all

# one site, resumable (re-running skips what is already complete)
python3 scraper.py --config sites.json --site dados.cvm.gov.br

# inspect without downloading anything (crawls, lists documents and dupes)
python3 scraper.py --config sites.json --all --dry-run

# an ad-hoc listing
python3 scraper.py --url https://example.gov/dados/ --slug example --jobs 8

# what else is there
python3 scraper.py --help
python3 scraper.py --config sites.json --list
```

Useful flags: `--jobs N` parallel downloads, `--rate` requests/second/host
(default 2), `--max-depth`, `--max-files`, `--max-file-bytes`,
`--max-total-bytes`, `--include/--exclude REGEX`, `--dupe-strategy
hardlink|copy|report|skip` (default `hardlink`), `--dry-run`, `--refresh`
(re-download even when unchanged), `--ignore-robots`, `--no-range-probe`.

Only the Python standard library is used (Python 3.8+).

## Output layout

```
outputs/
├── <slug>/
│   ├── README.md              # source, counts, notes, the exact re-run command
│   ├── files/                 # the mirrored documents, mirroring the listing tree
│   ├── _index/pages/          # every listing page as fetched (audit trail)
│   └── _reports/
│       ├── manifest.csv/json  # one row per document: status, size, sha256, path, errors
│       ├── duplicates.csv     # collapsed duplicates: method, kept copy, action
│       ├── candidate_duplicates.csv  # same size + similar name (dry-run heuristic)
│       ├── errors.log         # per-URL failures (never fatal)
│       ├── summary.json       # machine-readable totals for the run
│       └── state.json         # resume state (also the dedup index seed)
└── _reports/                  # global roll-up + duplicates_all_sites.csv
```

Status values in the manifest: `downloaded`, `duplicate`, `unchanged`
(already mirrored), `skipped` (a limit said no — reason included), `dry_run`,
`error`. Nothing is ever silently dropped: a document is either stored or
accounted for in a row.

## How de-duplication works

1. Every stored file gets a full **SHA-256**, plus head/tail fingerprints of the
   first and last 64 KiB.
2. Before downloading a file whose **listed size** matches something already
   stored, the crawler sends two single-range requests (64 KiB each). Identical
   head *and* tail ⇒ same document ⇒ the body is never fetched
   (`"method": "range-probe"`). Servers that ignore `Range` fall back to a
   normal download, so no correctness depends on it.
3. Downloads that share a size are serialised (`_size_gate`), so a duplicate can
   never be fetched twice concurrently before the index is updated.
4. Whatever survives step 2/3 is hashed and, if the hash is already known, not
   stored (or hard-linked, copied, reported or skipped — `--dupe-strategy`).
5. When a listing publishes no size at all but the name repeats (the PGFN
   pattern), the size is looked up with a 1-byte range request first, which
   brings the case back under step 2.

Measured on the replica (`outputs-replica`): 39 documents, 5.1 MiB downloaded,
9 duplicates collapsed, 1.4 MiB never fetched — the three PGFN archives were
recognised with **two 64 KiB range requests each instead of a full re-download**
and stored as hard links (one inode, verified in the test suite). A second run
over an unchanged tree downloads **0 bytes** and removes nothing.

## Politeness and housekeeping

* `robots.txt` is fetched once per host and obeyed (a rule only blocks when it
  matches the URL; `--ignore-robots` overrides knowingly).
* 2 requests/second/host by default, `--rate 0` to disable; a descriptive
  User-Agent; retries with backoff on 5xx/network errors.
* `--rewrite OLD=NEW` exists for test servers: fetches are redirected while
  every report/state URL stays the original one.
* Ctrl-C finishes the current files, writes reports and exits 0 (or 130 on a
  second interrupt). Exit codes: `0` clean, `1` finished with per-file errors,
  `2` fatal/config problem.

## Tests

```bash
python3 tests/run_tests.py -v          # 38 tests, no network access needed
```

* `TestHelpers` — URL canonicalisation (sort links, `?SA`-style parameters,
  ports, fragments), size/date parsing, filename sanitising and collision
  handling, robots parser, byte budget.
* `TestListingParsing` — one test per real dialect: Apache `<pre>` (CVM),
  Apache 2.4 table with truncated cells + `data-sort-value` (Valiprev), plain
  bullet links with and without metadata (PGFN), link farm (Macau), nginx
  autoindex (Caxias), OpenDataSoft file list (Comissão), scope enforcement and
  the dry-run candidate heuristic.
* `TestEndToEnd` — starts the replica over HTTP and drives `scraper.main()`:
  first run, duplicates (range probe and SHA-256), hardlink inode equality,
  "same size ≠ same bytes", second run is a no-op that deletes nothing,
  resume after a deletion, `--refresh`, `--dupe-strategy report`, `--dry-run`
  (a full inventory with candidate pairs, no payload), limit flags, `--include`
  and `--exclude` (the latter prunes whole subtrees, the former never stops the
  walk), dead links.
* `TestRobotsAndErrors` — robots-disallowed URLs are recorded and skipped (and
  downloaded with `--ignore-robots`), 404s are reported without aborting.
* `TestProductionConfig` — `sites.json` really lists the six requested
  listings.

`tests/make_replica.py` builds the replica (deterministic payloads, each site in
its own listing dialect, deliberate duplicates to exercise every dedup path);
`tests/replica_server.py` is a stdlib static server with `301 /dir → /dir/`,
single-range `206` support and an access log the tests assert on.

## Limits worth knowing

* Listing pages are parsed with regexes for four dialects; anything else falls
  back to "every `<a href>` that is not a sort link", with a 4 KiB probe to tell
  a folder from a document when the listing gives no size/date. Sites that
  render listings in JavaScript will not be crawlable.
* A document is only re-downloaded when the listing's size/date changed (or with
  `--refresh`). Listings that publish nothing about a file therefore rely on
  `--refresh` to notice an in-place replacement.
* The `dados.cvm.gov.br` and `macau.rn.gov.br` trees are very large; slice them
  with `--max-depth`, `--include`, `--max-file-bytes` or `--max-total-bytes`
  before a first full run — the manifest records every skip with its reason.
