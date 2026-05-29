"""
fix_garbled_years.py — one-time repair: correct OCR-garbled date YEARS.

A handful of dates have wildly out-of-range years (e.g. 3031, 2202, 1935,
2001) from OCR errors in the source PDFs. Each PDF lives in a year-stamped
folder in its source_url (/ptr-pdfs/YYYY/), which is the authoritative filing
year. For any date whose year is outside 2008-2027, replace ONLY the year
with that filing year (month/day untouched).

Guard: only applies when the filing year itself is in range, so we never
write fresh garbage. Idempotent (safe to re-run).

Backs up to all_transactions.preyearfix.backup.json (gitignored) first.

Run:  python scraper/fix_garbled_years.py
"""

import json
import re
import shutil
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "all_transactions.json"
BACKUP = DATA.with_name("all_transactions.preyearfix.backup.json")
DATE_FIELDS = ("transaction_date", "disclosure_date")
YEAR_MIN, YEAR_MAX = 2008, 2027
FILING_YEAR_RE = re.compile(r"/ptr-pdfs/(\d{4})/")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def in_range(year):
    return YEAR_MIN <= year <= YEAR_MAX


def main():
    rows = json.load(open(DATA, encoding="utf-8"))
    shutil.copy2(DATA, BACKUP)
    print(f"Backed up {len(rows):,} rows -> {BACKUP.name}")

    changed = 0
    skipped_no_anchor = 0
    examples = []
    for r in rows:
        fm = FILING_YEAR_RE.search(r.get("source_url", ""))
        if not fm:
            continue
        filing_year = int(fm.group(1))
        if not in_range(filing_year):
            continue  # guard: never anchor to an out-of-range filing year

        for f in DATE_FIELDS:
            m = DATE_RE.match(str(r.get(f, "")))
            if not m:
                continue
            mm, dd, yyyy = m.groups()
            if in_range(int(yyyy)):
                continue  # date already fine
            before = r[f]
            after = f"{mm}/{dd}/{filing_year}"
            r[f] = after
            changed += 1
            examples.append((r["filing_id"], f, before, after))

    json.dump(rows, open(DATA, "w", encoding="utf-8"), indent=2)

    print(f"Corrected garbled years: {changed}")
    print("Changes (filing | field: before -> after):")
    for fid, f, b, a in examples:
        print(f"  {fid} | {f}: {b} -> {a}")


if __name__ == "__main__":
    main()
