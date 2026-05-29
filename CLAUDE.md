# House Stock Watcher — Scraper Notes

## What this does
Downloads House PTR (Periodic Transaction Report) filings from the House disclosure site, parses trade tables out of the PDFs, and writes everything to `data/all_transactions.json`.

## How it runs
- GitHub Actions runs `scraper/fetch.py` on a schedule
- It downloads the yearly ZIP index, finds new PTR filing IDs, fetches only those PDFs, parses trades, and appends to the JSON
- Deduplication is by `filing_id` — already-seen doc IDs are skipped entirely

## Key files
- `scraper/fetch.py` — the scraper
- `scraper/test_one.py` — single-filing test against a known MTG filing (doc 20026791)
- `scraper/backfill_jammed.py` — one-time backfill that recovered jammed rows already logged before the fix (see below); safe to keep or discard
- `data/all_transactions.json` — output dataset
- `data/jammed_rows.jsonl` — log of jammed rows that still can't be parsed (now only the orphan fragments below)

## Jammed rows (fixed)
pdfplumber sometimes fails to split a table row into separate columns and dumps the whole row into `col[0]` as one string (other columns `None`). `parse_jammed_row()` in `fetch.py` now recovers these inline during a normal run. They come in three shapes (the OCR also garbles letter case and injects `gfedc` checkbox noise that can split the amount range across a line wrap):
- **A** — ticker inline before the type: `Apple Inc. (AAPL) [ST] P 8/1/18 8/1/18 $1,001 - $15,000`
- **B** — ticker alone at the start of the next line: `...Company P ... $1,001 - $15,000` then `(NWN)`
- **C** — long asset name wrapped, ticker mid-continuation: `Intl Business Machines S ... $1,001 - $15,000` then `Corporation (IBM)`

Filtering matches the normal parser: a row is kept only if it carries no asset-type tag (ticker ⇒ stock) or carries `[ST]`/`[EQ]`; other tags (`[OP]`, `[ET]`, …) are dropped. Rows with no ticker (bonds, municipal warrants, real estate) are correctly dropped.

The already-logged backlog (~20.5k rows) was folded in via `backfill_jammed.py` — **+13,002 net-new trades** (dataset 10,447 → 23,449). Backfill dedups at the trade level `(filing_id, ticker, transaction_date, type, amount)`, not by `filing_id`, because jammed rows belong to filings whose clean rows are already present.

### Deferred — orphan fragments (Scenario 3, not yet done)
~1,800 jammed rows are split across **two separate log entries**: the type/date/amount sit in one entry and the ticker continuation (`Interests (sDlP)`) in another. Recovering them means pairing fragments by doc ID + proximity — fragile, with a real risk of mis-pairing wrong data. Left for later; the fragments remain in `data/jammed_rows.jsonl`, so it's not a closed door.

### Newlines in `amount` (fixed)
`parse_pdf` used to only `.strip()` the amount cell, so multi-line cells kept an internal newline (`"$15,001 -\n$50,000"`), which also corrupted `amount_mid`. Now it collapses whitespace (`" ".join(cell.split())`). The 2,287 already-written rows were repaired by `scraper/fix_amounts.py`, which also recomputed `amount_mid` for every row — filling the 6,542 older rows that predated the field. Dataset is now schema-consistent: every row has `amount_mid` (None only for the one non-numeric "Spouse/DC Over" amount).

## Output schema
Each trade in `all_transactions.json`:
```
transaction_date    MM/DD/YYYY
disclosure_date     MM/DD/YYYY
ticker              e.g. AAPL
asset_description   company name, stripped of ticker and asset type tag
asset_type          always "Stock" (non-equity assets are filtered out)
type                Purchase | Sale | Exchange
amount              raw range string, e.g. "$1,001 - $15,000"
amount_mid          integer midpoint of range, or None if unparseable
representative      full name
district            e.g. GA14
owner               Self | Spouse | Joint | Dependent Child
filing_id           House doc ID
source_url          direct PDF link
```
