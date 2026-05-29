"""
test_all.py — read-only health audit of data/all_transactions.json.

Does NOT modify the dataset. Prints a report of structural and data-quality
issues so we can judge whether the record is solid enough to build on.

Run:  python scraper/test_all.py
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "all_transactions.json"

EXPECTED_FIELDS = {
    "transaction_date", "disclosure_date", "ticker", "asset_description",
    "asset_type", "type", "amount", "representative", "district",
    "owner", "filing_id", "source_url", "amount_mid",
}
CRITICAL = ["ticker", "representative", "filing_id", "transaction_date"]
VALID_TYPES = {"Purchase", "Sale", "Exchange"}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,6}$")  # plain US tickers, allows BRK.A style
MONEY_RE = re.compile(r"\$\s?[\d,]+")  # does the amount contain a dollar figure?
YEAR_MIN, YEAR_MAX = 2008, 2027


def sample(items, n=5):
    """First n items as a short, readable list."""
    return items[:n]


def main():
    if not DATA.exists():
        print(f"FATAL: {DATA} not found")
        sys.exit(1)

    try:
        rows = json.load(open(DATA, encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"FATAL: not valid JSON — {e}")
        sys.exit(1)

    if not isinstance(rows, list):
        print(f"FATAL: top level is {type(rows).__name__}, expected a list")
        sys.exit(1)

    total = len(rows)
    print("=" * 60)
    print(f"HEALTH AUDIT — {DATA.name}")
    print(f"Total records: {total:,}")
    print("=" * 60)

    issues = 0  # count of distinct problem categories that fired

    # 2. Schema completeness ------------------------------------------------
    missing_field = Counter()
    extra_field = Counter()
    for r in rows:
        for f in EXPECTED_FIELDS - r.keys():
            missing_field[f] += 1
        for f in r.keys() - EXPECTED_FIELDS:
            extra_field[f] += 1
    print("\n[2] SCHEMA COMPLETENESS")
    if not missing_field and not extra_field:
        print("    OK — every record has exactly the 13 expected fields")
    else:
        issues += 1
        if missing_field:
            print(f"    MISSING fields: {dict(missing_field)}")
        if extra_field:
            print(f"    UNEXPECTED fields: {dict(extra_field)}")

    # 3. Empty critical fields ---------------------------------------------
    empty = Counter()
    for r in rows:
        for f in CRITICAL:
            v = r.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                empty[f] += 1
    print("\n[3] EMPTY CRITICAL FIELDS (ticker/representative/filing_id/transaction_date)")
    if not empty:
        print("    OK — no blanks in critical fields")
    else:
        issues += 1
        print(f"    BLANKS: {dict(empty)}")

    # 4. Duplicates ---------------------------------------------------------
    key = lambda r: (r.get("filing_id"), r.get("ticker"),
                     r.get("transaction_date"), r.get("type"), r.get("amount"),
                     r.get("owner"))
    counts = Counter(key(r) for r in rows)
    dupes = {k: c for k, c in counts.items() if c > 1}
    dupe_extra = sum(c - 1 for c in dupes.values())
    print("\n[4] DUPLICATE TRADES  key=(filing_id,ticker,txn_date,type,amount,owner)")
    if not dupes:
        print("    OK — no duplicate trades")
    else:
        issues += 1
        print(f"    {len(dupes):,} keys duplicated, {dupe_extra:,} redundant rows")
        for k in sample(list(dupes)):
            print(f"      x{counts[k]}  {k}")

    # 5. amount_mid integrity ----------------------------------------------
    bad_type = []        # amount_mid is neither int nor None
    suspicious_none = [] # amount_mid is None but amount looks numeric
    for r in rows:
        am = r.get("amount_mid")
        if am is not None and not isinstance(am, int):
            bad_type.append(r)
        if am is None and MONEY_RE.search(str(r.get("amount", ""))):
            suspicious_none.append(r)
    print("\n[5] amount_mid INTEGRITY")
    if not bad_type and not suspicious_none:
        print("    OK — every amount_mid is an int, or None only for non-numeric amounts")
    else:
        issues += 1
        if bad_type:
            print(f"    WRONG TYPE: {len(bad_type)} rows (amount_mid not int/None)")
            for r in sample(bad_type):
                print(f"      {r.get('filing_id')}  amount_mid={r.get('amount_mid')!r}")
        if suspicious_none:
            print(f"    None DESPITE numeric amount: {len(suspicious_none)} rows")
            for r in sample(suspicious_none):
                print(f"      {r.get('filing_id')}  amount={r.get('amount')!r}")

    # 6. Date sanity --------------------------------------------------------
    bad_fmt = Counter()
    bad_year = []
    for r in rows:
        for f in ("transaction_date", "disclosure_date"):
            v = str(r.get(f, ""))
            if not DATE_RE.match(v):
                bad_fmt[f] += 1
                continue
            yr = int(v[-4:])
            if not (YEAR_MIN <= yr <= YEAR_MAX):
                bad_year.append((f, v, r.get("filing_id")))
    print(f"\n[6] DATE SANITY (MM/DD/YYYY, year {YEAR_MIN}-{YEAR_MAX})")
    if not bad_fmt and not bad_year:
        print("    OK — all dates well-formed and in range")
    else:
        issues += 1
        if bad_fmt:
            print(f"    BAD FORMAT: {dict(bad_fmt)}")
        if bad_year:
            print(f"    OUT-OF-RANGE YEAR: {len(bad_year)} dates")
            for f, v, fid in sample(bad_year):
                print(f"      {f}={v}  filing_id={fid}")

    # 7. asset_type ---------------------------------------------------------
    at = Counter(r.get("asset_type") for r in rows)
    print("\n[7] asset_type")
    if set(at) == {"Stock"}:
        print(f"    OK — all 'Stock' ({at['Stock']:,})")
    else:
        issues += 1
        print(f"    MIXED: {dict(at)}")

    # 8. type ---------------------------------------------------------------
    tp = Counter(r.get("type") for r in rows)
    bad_tp = {k: v for k, v in tp.items() if k not in VALID_TYPES}
    print("\n[8] transaction type")
    if not bad_tp:
        print(f"    OK — {dict(tp)}")
    else:
        issues += 1
        print(f"    UNEXPECTED types: {bad_tp}")
        print(f"    (valid breakdown: {{k:v for k,v in tp.items() if k in VALID_TYPES}})")

    # 9. Ticker sanity ------------------------------------------------------
    bad_ticker = [r for r in rows
                  if not TICKER_RE.match(str(r.get("ticker", "")))]
    print("\n[9] TICKER SANITY (plain A-Z/0-9, <=6 chars)")
    if not bad_ticker:
        print("    OK — all tickers look clean")
    else:
        issues += 1
        print(f"    SUSPICIOUS: {len(bad_ticker)} tickers")
        seen = Counter(r.get("ticker") for r in bad_ticker)
        for t, c in seen.most_common(10):
            print(f"      {c:>4}x  {t!r}")

    # 10. Overview ----------------------------------------------------------
    by_year = Counter()
    for r in rows:
        v = str(r.get("transaction_date", ""))
        if DATE_RE.match(v):
            by_year[v[-4:]] += 1
    print("\n[10] OVERVIEW")
    print(f"    Unique filings:        {len({r.get('filing_id') for r in rows}):,}")
    print(f"    Unique representatives:{len({r.get('representative') for r in rows}):>7,}")
    print(f"    Unique tickers:        {len({r.get('ticker') for r in rows}):,}")
    print("    Trades per transaction year:")
    for yr in sorted(by_year):
        print(f"      {yr}: {by_year[yr]:,}")

    # Verdict ---------------------------------------------------------------
    print("\n" + "=" * 60)
    if issues == 0:
        print("VERDICT: clean — no issue categories fired.")
    else:
        print(f"VERDICT: {issues} issue categor{'y' if issues == 1 else 'ies'} fired — see above.")
    print("=" * 60)


if __name__ == "__main__":
    main()
