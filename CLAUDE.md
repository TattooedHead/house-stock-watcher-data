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
- `scraper/test_all.py` — read-only health audit of the whole dataset (schema, dupes, dates, amounts, tickers, overview). Run anytime: `python scraper/test_all.py`
- `scraper/backfill_jammed.py` — one-time backfill that recovered jammed rows already logged before the fix (see below); safe to keep or discard
- `scraper/fix_amounts.py`, `scraper/fix_dates.py`, `scraper/fix_garbled_years.py`, `scraper/fix_dupes.py` — one-time data repairs (already applied; see "Data-quality cleanup" below). Each imports from `fetch.py` and backs up before writing; safe to keep or discard
- `data/all_transactions.json` — output dataset
- `data/jammed_rows.jsonl` — log of jammed rows that still can't be parsed (now only the orphan fragments below)

## Jammed rows (fixed)
pdfplumber sometimes fails to split a table row into separate columns and dumps the whole row into `col[0]` as one string (other columns `None`). `parse_jammed_row()` in `fetch.py` now recovers these inline during a normal run. They come in three shapes (the OCR also garbles letter case and injects `gfedc` checkbox noise that can split the amount range across a line wrap):
- **A** — ticker inline before the type: `Apple Inc. (AAPL) [ST] P 8/1/18 8/1/18 $1,001 - $15,000`
- **B** — ticker alone at the start of the next line: `...Company P ... $1,001 - $15,000` then `(NWN)`
- **C** — long asset name wrapped, ticker mid-continuation: `Intl Business Machines S ... $1,001 - $15,000` then `Corporation (IBM)`

Filtering matches the normal parser: a row is kept only if it carries no asset-type tag (ticker ⇒ stock) or carries `[ST]`/`[EQ]`; other tags (`[OP]`, `[ET]`, …) are dropped. Rows with no ticker (bonds, municipal warrants, real estate) are correctly dropped.

The already-logged backlog (~20.5k rows) was folded in via `backfill_jammed.py` — **+13,002 net-new trades** (dataset 10,447 → 23,449). Backfill dedups at the trade level, not by `filing_id`, because jammed rows belong to filings whose clean rows are already present.

> **Dedup key — IMPORTANT:** the correct trade-level key is `(filing_id, ticker, transaction_date, type, amount, owner)`. **`owner` is required.** A rep can make the same trade the same day in two accounts (e.g. Self + Dependent Child); those are real, distinct disclosures and a key without `owner` wrongly collapses them. (The original backfill used a 5-field key without `owner` — see the dedupe cleanup below for the correction.)

### Deferred — orphan fragments (Scenario 3, not yet done)
~1,800 jammed rows are split across **two separate log entries**: the type/date/amount sit in one entry and the ticker continuation (`Interests (sDlP)`) in another. Recovering them means pairing fragments by doc ID + proximity — fragile, with a real risk of mis-pairing wrong data. Left for later; the fragments remain in `data/jammed_rows.jsonl`, so it's not a closed door.

### Newlines in `amount` (fixed)
`parse_pdf` used to only `.strip()` the amount cell, so multi-line cells kept an internal newline (`"$15,001 -\n$50,000"`), which also corrupted `amount_mid`. Now it collapses whitespace (`" ".join(cell.split())`). The 2,287 already-written rows were repaired by `scraper/fix_amounts.py`, which also recomputed `amount_mid` for every row — filling the 6,542 older rows that predated the field. Dataset is now schema-consistent: every row has `amount_mid` (None only for the one non-numeric "Spouse/DC Over" amount).

## Data-quality cleanup (2026-05-29)
Ran `test_all.py` over the full dataset and fixed three issues. Dataset **23,449 → 23,368**; audit now verdict-clean.

1. **Date padding (fixed at source + repaired).** Dates were stored as the PDF wrote them, so some were single-digit (`3/7/2018`) instead of `MM/DD/YYYY`. `normalize_date()` in `fetch.py` now zero-pads both date fields in both write paths (idempotent, lossless); `fix_dates.py` repaired the ~4,580 existing ones.
2. **Garbled years (repaired only).** 8 dates had OCR-garbled years (`3031`, `2202`, `1935`, `2001`). `fix_garbled_years.py` snaps any year outside 2008–2027 to the filing year parsed from `source_url` (`/ptr-pdfs/YYYY/`), corroborated by the row's other date. No scraper-side guard was added — it's a heuristic, deferred.
3. **Duplicate trades (repaired only).** 109 rows shared the 5-field key, but **only 81 were true artifacts** (byte-identical); the other 28 differed only by `owner` and are real distinct trades. `fix_dupes.py` removed the 81 using the correct `owner`-inclusive key (see the IMPORTANT note above).

> **Deferred root cause:** the 81 artifacts clustered in 44 filings (up to 5 copies each), which points at the PDF parser re-emitting rows at multi-page table boundaries. The data is clean now, but `fetch.py` could regenerate dupes on a future run — not yet investigated.

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
