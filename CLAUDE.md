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
- `data/all_transactions.json` — output dataset

## Known issue: jammed rows (not yet fixed)
pdfplumber sometimes fails to split a table row into separate columns. When this happens, the entire row ends up as a single string in `col[0]` with null bytes (`\x00`), and all other columns come back as `None`. These rows get silently skipped because `row[tx_type_col]` is None.

Confirmed affected rows in doc 20026791: ADBE, DVN, PLTR.

A future fix would detect these jammed rows (e.g. `row[0]` is a long string and `row[1]` is None) and parse the `col[0]` string directly using regex instead of column indices.

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
