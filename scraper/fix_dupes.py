"""
fix_dupes.py — one-time repair: remove byte-identical duplicate trades.

The scraper occasionally emits the same trade row twice (likely multi-page
tables re-extracted at page breaks). Dedup on
(filing_id, ticker, transaction_date, type, amount, OWNER), keeping the first
occurrence. owner is in the key on purpose: a rep can make the same trade the
same day in two accounts (Self + Dependent Child) — those are real, distinct
trades and must be kept. Idempotent (safe to re-run).

Backs up to all_transactions.predupe.backup.json (gitignored) first.

Run:  python scraper/fix_dupes.py
"""

import json
import shutil
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "all_transactions.json"
BACKUP = DATA.with_name("all_transactions.predupe.backup.json")


def key(r):
    return (r["filing_id"], r["ticker"], r["transaction_date"],
            r["type"], r["amount"], r["owner"])


def main():
    rows = json.load(open(DATA, encoding="utf-8"))
    shutil.copy2(DATA, BACKUP)
    print(f"Backed up {len(rows):,} rows -> {BACKUP.name}")

    seen = set()
    kept = []
    dropped = []
    for r in rows:
        k = key(r)
        if k in seen:
            dropped.append(r)
            continue
        seen.add(k)
        kept.append(r)

    json.dump(kept, open(DATA, "w", encoding="utf-8"), indent=2)

    print(f"Removed {len(dropped)} duplicate rows: {len(rows):,} -> {len(kept):,}")
    print("Sample of dropped rows:")
    for r in dropped[:8]:
        print(f"  {r['filing_id']} {r['ticker']} {r['transaction_date']} "
              f"{r['type']} {r['amount']} | owner: {r['owner']}")


if __name__ == "__main__":
    main()
