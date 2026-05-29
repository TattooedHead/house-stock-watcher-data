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
SCAN_YEARS = list(range(2008, _now + 1))
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

# --- Jammed-row recovery ---------------------------------------------------
# pdfplumber sometimes fails to split a table row into columns and dumps the
# whole row into col[0] as one string (with the other columns None). These rows
# come in three shapes depending on where the (TICKER) landed:
#   A  asset name + (TICKER) inline, before the type:  "Apple Inc. (AAPL) [ST] P 8/1/18 8/1/18 $1,001 - $15,000"
#   B  ticker alone at the start of the next line:     "Northwest Natural gas Company P ... $1,001 - $15,000\n(NWN)"
#   C  long asset name wrapped, ticker mid-continuation:"Intl Business Machines S ... $1,001 - $15,000\nCorporation (IBM)"
# OCR garbles letter case (sP, [sT], (AAPl)) and injects "gfedc" checkbox noise
# that can split the amount range across a line wrap. All handled below.
_JAMMED_AMT = (
    r"(?:(?P<amt_low>\$[\d,]+(?:\.\d+)?)"
    r"(?:\s*-\s*(?:gfedc\s*)?(?P<amt_high>\$[\d,]+(?:\.\d+)?))?"
    r"|(?P<amt_special>Spouse/DC\s+Over))"
)
_JAMMED_A = re.compile(
    r"^(?:\d{7,12}\s+)?(?:(?P<owner_code>SP|JT|DC)\s+)?"
    r"(?P<asset_raw>.+?\([A-Za-z]{1,5}\))"
    r"(?:\s+\[[A-Za-z]{2}\])?"
    r"\s+(?P<tx_type>[PSE])\s+"
    r"(?P<tx_date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<disc_date>\d{1,2}/\d{1,2}/\d{4})\s+"
    + _JAMMED_AMT,
    re.IGNORECASE,
)
_JAMMED_B = re.compile(
    r"^(?:(?P<owner_code>SP|JT|DC)\s+)?"
    r"(?P<asset_raw>.+?)\s+(?P<tx_type>[PSE])\s+"
    r"(?P<tx_date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<disc_date>\d{1,2}/\d{1,2}/\d{4})\s+"
    + _JAMMED_AMT + r"[^\n]*\n\((?P<ticker>[A-Za-z]{1,5})\)",
    re.IGNORECASE,
)
_JAMMED_C = re.compile(
    r"^(?:\d{7,12}\s+)?(?:(?P<owner_code>SP|JT|DC)\s+)?"
    r"(?P<asset_head>.+?)\s+(?P<tx_type>[PSE])\s+"
    r"(?P<tx_date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<disc_date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amt_low>\$[\d,]+(?:\.\d+)?)\s*-\s*(?:gfedc\b\s*)?"
    r"(?P<amt_mid>[^$]*?)(?P<amt_high>\$[\d,]+(?:\.\d+)?)"
    r"[^\n]*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# Lines after which the trade data ends and free-text begins; the real ticker
# and asset-type tag always appear before the first of these.
_JAMMED_NOISE = re.compile(r"(?im)^\s*(FIL[I1]NG\s+STATUS|SUBHOLDING\s+OF|DESCRIPTION|LOCATION)\s*:")
_JAMMED_TICKER = re.compile(r"\(([A-Za-z]{1,5})\)")
_JAMMED_INLINE_TICKER = re.compile(r"\(([A-Za-z]{1,5})\)\s*$")
_JAMMED_TAG = re.compile(r"\[([A-Za-z]{2})\]")
_JAMMED_ASSET_OK = {"ST", "EQ"}  # mirror ASSET_INCLUDE; other tags are non-equity


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


def _jammed_amount(low, high, special):
    if special:
        return special.strip()
    if high:
        return f"{low} - {high}"
    return low


def _jammed_asset_type_ok(text):
    """False if an asset-type tag is present and is not an equity ([ST]/[EQ])."""
    nm = _JAMMED_NOISE.search(text)
    region = text[: nm.start()] if nm else text
    m = _JAMMED_TAG.search(region)
    return not (m and m.group(1).upper() not in _JAMMED_ASSET_OK)


def _jammed_fields(raw):
    """Pull (owner_code, tx_type, tx_date, disc_date, amount, ticker, asset) from a
    jammed row string, or None if it isn't a recoverable stock trade."""
    if not _jammed_asset_type_ok(raw):
        return None
    m = _JAMMED_A.match(raw)
    if m:
        gd = m.groupdict()
        tm = _JAMMED_INLINE_TICKER.search(gd["asset_raw"])
        amt = _jammed_amount(gd["amt_low"], gd["amt_high"], gd["amt_special"])
        return (gd["owner_code"], gd["tx_type"], gd["tx_date"], gd["disc_date"],
                amt, tm.group(1).upper(), gd["asset_raw"][: tm.start()].strip(" -"))
    m = _JAMMED_B.match(raw)
    if m:
        gd = m.groupdict()
        amt = _jammed_amount(gd["amt_low"], gd["amt_high"], gd["amt_special"])
        return (gd["owner_code"], gd["tx_type"], gd["tx_date"], gd["disc_date"],
                amt, gd["ticker"].upper(), gd["asset_raw"].strip())
    m = _JAMMED_C.match(raw)
    if m:
        cont = (m.group("amt_mid") or "") + " " + m.group("rest")
        nm = _JAMMED_NOISE.search(cont)
        region = cont[: nm.start()] if nm else cont
        tickers = _JAMMED_TICKER.findall(region)
        if not tickers:
            return None
        idx = region.rfind("(" + tickers[-1] + ")")
        cont_clean = region[:idx] + region[idx + len(tickers[-1]) + 2:]
        asset = _JAMMED_TAG.sub(" ", m.group("asset_head") + " " + cont_clean)
        asset = re.sub(r"\s+", " ", asset).strip()
        amt = _jammed_amount(m.group("amt_low"), m.group("amt_high"), None)
        return (m.group("owner_code"), m.group("tx_type"), m.group("tx_date"),
                m.group("disc_date"), amt, tickers[-1].upper(), asset)
    return None


def parse_jammed_row(raw, member):
    """Recover a trade dict from a jammed col[0] string, or None if unparseable.
    Output shape is identical to a normally-parsed trade."""
    fields = _jammed_fields(raw)
    if fields is None:
        return None
    owner_code, tx_type, tx_date, disc_date, amount_raw, ticker, asset_desc = fields
    return {
        "transaction_date": tx_date.strip(),
        "disclosure_date": disc_date.strip(),
        "ticker": ticker,
        "asset_description": asset_desc,
        "asset_type": "Stock",
        "type": TYPE_MAP.get(tx_type.upper(), tx_type.upper()),
        "amount": amount_raw,
        "amount_mid": parse_amount_mid(amount_raw),
        "representative": f"{member['first']} {member['last']}",
        "district": member["district"],
        "owner": OWNER_MAP.get((owner_code or "").upper(), "Self"),
        "filing_id": member["doc_id"],
        "source_url": PDF_URL.format(year=member["year"], doc_id=member["doc_id"]),
    }


def parse_pdf(pdf_bytes, member):
    trades = []
    jammed = []
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
                    if row[0] and len(row) > 1 and all(c is None for c in row[1:]):
                        recovered = parse_jammed_row(row[0], member)
                        if recovered is not None:
                            trades.append(recovered)
                        else:
                            jammed.append({
                                "year": member["year"],
                                "doc_id": member["doc_id"],
                                "representative": f"{member['first']} {member['last']}",
                                "district": member["district"],
                                "raw": row[0],
                            })
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

                    # Collapse internal whitespace: multi-line amount cells
                    # otherwise keep a newline, e.g. "$15,001 -\n$50,000".
                    amount_raw = " ".join((row[col["amount"]] or "").split())
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
    return trades, jammed


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

    jammed_path = os.path.join(os.path.dirname(out_path), "jammed_rows.jsonl")
    jammed_log = open(jammed_path, "a", encoding="utf-8")

    new_trades = []
    for year in SCAN_YEARS:
        members = fetch_index(year)
        new_in_year = [m for m in members if m["doc_id"] not in known_ids]
        log.info(f"  {len(new_in_year)} new filings to fetch for {year}")
        for i, member in enumerate(new_in_year):
            log.info(f"  [{year}] {i+1}/{len(new_in_year)} — {member['first']} {member['last']} ({member['doc_id']})")
            pdf_bytes = fetch_pdf(member)
            if pdf_bytes:
                trades, jammed = parse_pdf(pdf_bytes, member)
                new_trades.extend(trades)
                for j in jammed:
                    jammed_log.write(json.dumps(j) + "\n")
                jammed_log.flush()
            time.sleep(DELAY)

    jammed_log.close()
    log.info(f"Jammed rows log written to {jammed_path}")

    all_trades = existing_trades + new_trades
    all_trades.sort(key=sort_key, reverse=True)
    log.info(f"Total trades: {len(all_trades)} ({len(new_trades)} new)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    log.info(f"Written to {out_path}")


if __name__ == "__main__":
    main()
