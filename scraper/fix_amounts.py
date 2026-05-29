"""One-time repair: normalize `amount` strings and recompute `amount_mid`.

The normal parser used to only .strip() the amount cell, so multi-line cells
kept an internal newline (e.g. "$15,001 -\n$50,000"), which also corrupted
amount_mid. fetch.py now collapses that whitespace; this script applies the
same normalization to the rows already written, and recomputes amount_mid for
every row (which also fills in older rows that predate the amount_mid field).
Safe to keep or discard once run.
"""
import os
import json
import logging

import fetch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TXN_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "all_transactions.json"))


def main():
    with open(TXN_PATH, encoding="utf-8") as f:
        trades = json.load(f)
    log.info(f"Loaded {len(trades)} trades")

    amount_changed = 0
    mid_changed = 0
    mid_added = 0
    for t in trades:
        clean = " ".join((t.get("amount") or "").split())
        if clean != t.get("amount"):
            amount_changed += 1
            t["amount"] = clean
        new_mid = fetch.parse_amount_mid(clean) if clean else None
        if "amount_mid" not in t:
            mid_added += 1
            t["amount_mid"] = new_mid
        elif t["amount_mid"] != new_mid:
            mid_changed += 1
            t["amount_mid"] = new_mid

    log.info(f"amount strings normalized: {amount_changed}")
    log.info(f"amount_mid recomputed (changed): {mid_changed}")
    log.info(f"amount_mid added to old rows:    {mid_added}")

    with open(TXN_PATH, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)
    log.info(f"Wrote {len(trades)} trades to {TXN_PATH}")


if __name__ == "__main__":
    main()
