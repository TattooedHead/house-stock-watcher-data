import os
import io
import re
import json
import time
import zipfile
import logging
import datetime
import requests
import pdfplumber
from xml.etree import ElementTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_now = datetime.datetime.utcnow().year
SCAN_YEARS = [_now - 1, _now]
ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
HEADERS = {"User-Agent": "HouseStockWatcher/1.0 chrishangsleben@gmail.com"}
DELAY = 0.5

OWNER_MAP = {"": "Self", "SP": "Spouse", "JT": "Joint", "DC": "Dependent Child"}
TYPE_MAP = {"P": "Purchase", "S": "Sale", "E": "Exchange"}
ASSET_INCLUDE = {"[ST]", "[EQ]"}

TICKER_RE = re.compile(r'\(([A-Z]{1,5})\)')
ASSET_TYPE_RE = re.compile(r'\[([A-Z]{2})\]')
_AMOUNT_CLEAN_RE = re.compile(r'[$,]')


def load_existing(out_path):
    if not os.path.exists(out_path):
        return [], set()
    with open(out_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    known_ids = {t["filing_id"] for t in data}
    return data, known_ids


def fetch_with_retry(url, max_retries=3, backoff=2.0, timeout=30):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = backoff ** attempt
                log.warning(f"Retry {attempt + 1}/{max_retries - 1} for {url}: {e}, waiting {wait:.0f}s")
                time.sleep(wait)
    raise last_exc


def fetch_index(year):
    url = ZIP_URL.format(year=year)
    log.info(f"Downloading index ZIP for {year}...")
    try:
        resp = fetch_with_retry(url)
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


def find_header_row(table):
    for row in table:
        if not row:
            continue
        mapping = {}
        for col_idx, cell in enumerate(row):
            if cell is None:
                continue
            norm = cell.lower().replace("\n", " ").strip()
            if "asset" in norm:
                mapping["asset"] = col_idx
            elif "transaction type" in norm or norm == "type":
                mapping["tx_type"] = col_idx
            elif "transaction date" in norm or norm == "date":
                mapping["tx_date"] = col_idx
            elif "notification" in norm or "disclosure" in norm:
                mapping["disc_date"] = col_idx
            elif "amount" in norm:
                mapping["amount"] = col_idx
            elif norm == "sp" or "owner" in norm:
                mapping["owner"] = col_idx
        if all(k in mapping for k in ("asset", "tx_type", "tx_date", "disc_date", "amount")):
            return mapping
    return None


def parse_amount_mid(amount_str):
    cleaned = _AMOUNT_CLEAN_RE.sub("", amount_str).strip()
    parts = cleaned.split(" - ")
    try:
        if len(parts) == 2:
            return (int(parts[0].strip()) + int(parts[1].strip())) // 2
        nums = re.findall(r'\d+', cleaned)
        if nums:
            return int(nums[0])
    except (ValueError, IndexError):
        pass
    return None


def parse_pdf(pdf_bytes, member):
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            last_col = None
            for page in pdf.pages:
                table = page.extract_table()
                if not table:
                    continue
                col = find_header_row(table)
                if col is not None:
                    last_col = col
                elif last_col is not None:
                    col = last_col
                else:
                    log.warning(f"No column mapping for {member['doc_id']} page {page.page_number}, skipping")
                    continue

                max_col = max(col.values())
                for row in table:
                    if not row or len(row) <= max_col:
                        continue
                    tx_type = (row[col["tx_type"]] or "").strip()
                    if tx_type not in ("P", "S", "E"):
                        continue
                    asset_raw = (row[col["asset"]] or "").replace("\n", " ").strip()

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

                    owner_col = col.get("owner", 1)
                    owner_code = (row[owner_col] or "").strip() if owner_col < len(row) else ""
                    owner = OWNER_MAP.get(owner_code, owner_code)

                    amount_raw = (row[col["amount"]] or "").strip()
                    amount_mid = parse_amount_mid(amount_raw)
                    if amount_mid is None and amount_raw:
                        log.warning(f"Could not parse amount '{amount_raw}' for {member['doc_id']}")

                    trades.append({
                        "transaction_date": (row[col["tx_date"]] or "").strip(),
                        "disclosure_date": (row[col["disc_date"]] or "").strip(),
                        "ticker": ticker,
                        "asset_description": asset_desc,
                        "asset_type": "Stock",
                        "type": TYPE_MAP.get(tx_type, tx_type),
                        "amount": amount_raw,
                        "amount_mid": amount_mid,
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
        resp = fetch_with_retry(url)
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
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "all_transactions.json")
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    existing_trades, known_ids = load_existing(out_path)
    log.info(f"Loaded {len(existing_trades)} existing trades. Scanning years: {SCAN_YEARS}")

    new_trades = []
    for year in SCAN_YEARS:
        members = fetch_index(year)
        new_in_year = [m for m in members if m["doc_id"] not in known_ids]
        log.info(f"  {len(new_in_year)} new filings to fetch for {year}")
        for i, member in enumerate(new_in_year):
            log.info(f"  [{year}] {i+1}/{len(new_in_year)} — {member['first']} {member['last']} ({member['doc_id']})")
            pdf_bytes = fetch_pdf(member)
            if pdf_bytes:
                trades = parse_pdf(pdf_bytes, member)
                new_trades.extend(trades)
            time.sleep(DELAY)

    all_trades = existing_trades + new_trades
    all_trades.sort(key=sort_key, reverse=True)
    log.info(f"Total trades: {len(all_trades)} ({len(new_trades)} new)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    log.info(f"Written to {out_path}")


if __name__ == "__main__":
    main()
