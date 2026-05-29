import os
import io
import re
import json
import time
import zipfile
import logging
import requests
import pdfplumber
from xml.etree import ElementTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

YEARS = [2022, 2023, 2024, 2025, 2026]
ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
HEADERS = {"User-Agent": "HouseStockWatcher/1.0 illshootthat@gmail.com"}
DELAY = 0.5

OWNER_MAP = {"": "Self", "SP": "Spouse", "JT": "Joint", "DC": "Dependent Child"}
TYPE_MAP = {"P": "Purchase", "S": "Sale", "E": "Exchange"}
ASSET_INCLUDE = {"[ST]", "[EQ]"}

TICKER_RE = re.compile(r'\(([A-Z]{1,5})\)')
ASSET_TYPE_RE = re.compile(r'\[([A-Z]{2})\]')


def fetch_index(year):
    url = ZIP_URL.format(year=year)
    log.info(f"Downloading index ZIP for {year}...")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Failed to download ZIP for {year}: {e}")
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml_name = f"{year}FD.xml"
            with z.open(xml_name) as f:
                tree = ElementTree.parse(f)
    except Exception as e:
        log.error(f"Failed to parse ZIP for {year}: {e}")
        return []

    members = []
    for member in tree.getroot().findall("Member"):
        filing_type = (member.findtext("FilingType") or "").strip()
        if filing_type != "P":
            continue
        members.append({
            "first": (member.findtext("First") or "").strip(),
            "last": (member.findtext("Last") or "").strip(),
            "district": (member.findtext("StateDst") or "").strip(),
            "year": year,
            "doc_id": (member.findtext("DocID") or "").strip(),
        })
    log.info(f"  Found {len(members)} PTR filings for {year}")
    return members


def parse_pdf(pdf_bytes, member):
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                table = page.extract_table()
                if not table:
                    continue
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
                        "representative": f"{member['first']} {member['last']}",
                        "district": member["district"],
                        "owner": owner,
                        "filing_id": member["doc_id"],
                        "source_url": PDF_URL.format(year=member["year"], doc_id=member["doc_id"]),
                    })
    except Exception as e:
        log.error(f"Failed to parse PDF for DocID {member['doc_id']}: {e}")
    return trades


def fetch_pdf(member):
    url = PDF_URL.format(year=member["year"], doc_id=member["doc_id"])
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.error(f"Failed to download PDF {member['doc_id']}: {e}")
        return None


def sort_key(trade):
    date_str = trade.get("disclosure_date", "")
    try:
        parts = date_str.split("/")
        return (int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        return (0, 0, 0)


def main():
    all_trades = []
    for year in YEARS:
        members = fetch_index(year)
        for i, member in enumerate(members):
            log.info(f"  [{year}] {i+1}/{len(members)} — {member['first']} {member['last']} ({member['doc_id']})")
            pdf_bytes = fetch_pdf(member)
            if pdf_bytes:
                trades = parse_pdf(pdf_bytes, member)
                all_trades.extend(trades)
            time.sleep(DELAY)

    all_trades.sort(key=sort_key, reverse=True)
    log.info(f"Total trades extracted: {len(all_trades)}")

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "all_transactions.json")
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    log.info(f"Written to {out_path}")


if __name__ == "__main__":
    main()
