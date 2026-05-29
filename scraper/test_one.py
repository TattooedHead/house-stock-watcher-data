import io
import re
import requests
import pdfplumber

# Marjorie Taylor Greene — confirmed multi-trade PDF from the project brief
DOC_ID = "20026791"
YEAR = 2025
PDF_URL = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DOC_ID}.pdf"
HEADERS = {"User-Agent": "HouseStockWatcher/1.0 chrishangsleben@gmail.com"}

OWNER_MAP = {"": "Self", "SP": "Spouse", "JT": "Joint", "DC": "Dependent Child"}
TYPE_MAP = {"P": "Purchase", "S": "Sale", "E": "Exchange"}
ASSET_INCLUDE = {"[ST]", "[EQ]"}

TICKER_RE = re.compile(r'\(([A-Z]{1,5})\)')
ASSET_TYPE_RE = re.compile(r'\[([A-Z]{2})\]')

print(f"Fetching {PDF_URL}...")
resp = requests.get(PDF_URL, headers=HEADERS, timeout=30)
resp.raise_for_status()
print("Downloaded. Parsing...")

trades = []
with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
    for page_num, page in enumerate(pdf.pages, 1):
        table = page.extract_table()
        if not table:
            print(f"  Page {page_num}: no table found")
            continue
        print(f"  Page {page_num}: {len(table)} rows")
        for row in table:
            if not row or len(row) < 8:
                continue
            tx_type = (row[3] or "").strip()
            if tx_type not in ("P", "S", "E"):
                continue
            asset_raw = (row[2] or "").replace("\n", " ").strip()

            type_match = ASSET_TYPE_RE.search(asset_raw)
            if not type_match:
                continue
            bracket = f"[{type_match.group(1)}]"
            if bracket not in ASSET_INCLUDE:
                continue

            ticker_match = TICKER_RE.search(asset_raw)
            if not ticker_match:
                continue
            ticker = ticker_match.group(1)

            asset_desc = ASSET_TYPE_RE.sub("", asset_raw)
            asset_desc = TICKER_RE.sub("", asset_desc).strip(" -")

            owner_code = (row[1] or "").strip()
            owner = OWNER_MAP.get(owner_code, owner_code)

            trades.append({
                "transaction_date": (row[4] or "").strip(),
                "disclosure_date": (row[5] or "").strip(),
                "ticker": ticker,
                "asset_description": asset_desc,
                "asset_type": "Stock",
                "type": TYPE_MAP.get(tx_type, tx_type),
                "amount": (row[6] or "").strip(),
                "representative": "Marjorie Taylor Greene",
                "district": "GA14",
                "owner": owner,
                "filing_id": DOC_ID,
                "source_url": PDF_URL,
            })

print(f"\n--- {len(trades)} trade(s) extracted ---\n")
for t in trades:
    print(f"  {t['representative']} | {t['ticker']} | {t['type']} | {t['transaction_date']} | {t['amount']} | Owner: {t['owner']}")
