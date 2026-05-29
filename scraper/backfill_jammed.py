"""One-time backfill: recover stock trades from previously-logged jammed rows.

Background: earlier scraper runs couldn't parse "jammed" PDF rows (pdfplumber
dumped a whole row into one cell) and logged them to data/jammed_rows.jsonl
instead. fetch.py now recovers these inline, so future runs won't log them --
but the rows already collected need a one-time pass to fold them into the
dataset. This script does exactly that and then can be discarded.

It reuses parse_jammed_row() from fetch.py (single source of truth) and pulls
each filing's district/representative from the authoritative yearly index.
Dedup is at the TRADE level (filing_id, ticker, date, type, amount), NOT by
filing_id -- jammed rows belong to filings whose clean rows are already in the
dataset, so a filing-level skip would reject every recovery.
"""
import os
import json
import logging

import fetch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
JAMMED_PATH = os.path.join(DATA_DIR, "jammed_rows.jsonl")
TXN_PATH = os.path.join(DATA_DIR, "all_transactions.json")


def trade_key(t):
    return (t["filing_id"], t["ticker"], t["transaction_date"], t["type"], t["amount"])


def main():
    with open(JAMMED_PATH, encoding="utf-8") as f:
        jammed = [json.loads(line) for line in f]
    log.info(f"Loaded {len(jammed)} jammed rows")

    # Authoritative member lookup (district + name) from each year's index.
    years = sorted({r["year"] for r in jammed})
    members = {}
    for year in years:
        for m in fetch.fetch_index(year):
            members[m["doc_id"]] = m
    log.info(f"Built member index for {len(members)} filings across {years}")

    existing, _ = fetch.load_existing(TXN_PATH)
    seen = {trade_key(t) for t in existing}
    log.info(f"Existing dataset: {len(existing)} trades")

    recovered = []
    skipped_no_member = 0
    for r in jammed:
        member = members.get(r["doc_id"])
        if member is None:
            skipped_no_member += 1
            continue
        trade = fetch.parse_jammed_row(r["raw"], member)
        if trade is None:
            continue
        k = trade_key(trade)
        if k in seen:
            continue
        seen.add(k)
        recovered.append(trade)

    log.info(f"Recovered {len(recovered)} net-new trades "
             f"({skipped_no_member} rows skipped: filing not in any index)")

    all_trades = existing + recovered
    all_trades.sort(key=fetch.sort_key, reverse=True)
    with open(TXN_PATH, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    log.info(f"Wrote {len(all_trades)} trades to {TXN_PATH}")


if __name__ == "__main__":
    main()
