import requests
from fetch import fetch_with_retry, find_header_row, parse_amount_mid, parse_pdf

# Marjorie Taylor Greene — confirmed multi-trade PDF from the project brief
MEMBER = {"first": "Marjorie Taylor", "last": "Greene", "district": "GA14", "year": 2025, "doc_id": "20026791"}
PDF_URL = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{MEMBER['year']}/{MEMBER['doc_id']}.pdf"

print(f"Fetching {PDF_URL}...")
resp = fetch_with_retry(PDF_URL)
print("Downloaded. Parsing...")

trades, jammed, ok = parse_pdf(resp.content, MEMBER)

print(f"\n--- {len(trades)} trade(s) extracted (parse ok={ok}, {len(jammed)} jammed) ---\n")
for t in trades:
    print(
        f"  {t['ticker']} | {t['type']} | {t['transaction_date']} "
        f"| {t['amount']} | amount_mid={t['amount_mid']} | owner={t['owner']}"
    )

print("\n--- amount_mid spot check ---")
for t in trades:
    if t["amount_mid"] is None and t["amount"]:
        print(f"  UNPARSED: '{t['amount']}'")
    elif t["amount_mid"] is not None:
        print(f"  '{t['amount']}' -> {t['amount_mid']}")
