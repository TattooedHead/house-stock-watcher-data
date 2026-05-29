"""
fix_dates.py — one-time repair: zero-pad existing dates to MM/DD/YYYY.

Applies fetch.normalize_date() to transaction_date and disclosure_date on
every row in data/all_transactions.json, so the on-disk data matches the
schema the scraper now produces. Idempotent (safe to re-run).

Backs up the current file to all_transactions.predates.backup.json first
(gitignored). Does NOT touch garbled YEARS (e.g. 3031) — that's a separate
issue; this only fixes day/month padding.

Run:  python scraper/fix_dates.py
"""

import json
import shutil
from pathlib import Path

import fetch  # reuse normalize_date — single source of truth

DATA = Path(__file__).resolve().parent.parent / "data" / "all_transactions.json"
BACKUP = DATA.with_name("all_transactions.predates.backup.json")
DATE_FIELDS = ("transaction_date", "disclosure_date")


def main():
    rows = json.load(open(DATA, encoding="utf-8"))
    shutil.copy2(DATA, BACKUP)
    print(f"Backed up {len(rows):,} rows -> {BACKUP.name}")

    changed = {f: 0 for f in DATE_FIELDS}
    examples = []
    for r in rows:
        for f in DATE_FIELDS:
            before = r.get(f, "")
            after = fetch.normalize_date(before)
            if after != before:
                changed[f] += 1
                if len(examples) < 8:
                    examples.append((f, before, after))
                r[f] = after

    json.dump(rows, open(DATA, "w", encoding="utf-8"), indent=2)

    print(f"Repaired transaction_date: {changed['transaction_date']:,}")
    print(f"Repaired disclosure_date:  {changed['disclosure_date']:,}")
    print("Examples (field: before -> after):")
    for f, b, a in examples:
        print(f"  {f}: {b!r} -> {a!r}")


if __name__ == "__main__":
    main()
