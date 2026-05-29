import os
import io
import sys
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

_now = datetime.datetime.now(datetime.UTC).year
SCAN_YEARS = list(range(2008, _now + 1))
# Optional override for testing: set the SCAN_YEARS env var to a comma-separated
# list (e.g. "2025,2026") to limit the scrape to those years. Unset — as in CI —
# scans every year.
_env_years = os.environ.get("SCAN_YEARS")
if _env_years:
    SCAN_YEARS = [int(y.strip()) for y in _env_years.split(",") if y.strip()]
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


def load_manifest(manifest_path, existing_trades):
    """Load the filings manifest: doc_id -> {year, trades, jammed, fetched_at}.
    Every filing we've successfully fetched+parsed lives here — INCLUDING ones
    that produced zero trades — so we never re-download them. On first run (no
    file) we seed it from the existing dataset so the filings we already have
    count as seen. jammed counts can't be reconstructed retroactively, so
    seeded entries get jammed=0 and fetched_at=None."""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    manifest = {}
    for t in existing_trades:
        fid = t.get("filing_id")
        if not fid:
            continue
        if fid not in manifest:
            ym = re.search(r"/ptr-pdfs/(\d{4})/", t.get("source_url", ""))
            manifest[fid] = {
                "year": int(ym.group(1)) if ym else None,
                "trades": 0,
                "jammed": 0,
                "fetched_at": None,
            }
        manifest[fid]["trades"] += 1
    return manifest


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


def normalize_date(s, filing_year=None):
    """Zero-pad a M/D/YYYY date to MM/DD/YYYY, and (when filing_year is given)
    snap an out-of-range OCR-garbled year to the authoritative filing year.
    Idempotent. Returns the input unchanged (stripped) if it isn't a 3-part
    all-numeric date, so unrecoverable values are never silently mangled."""
    s = (s or "").strip()
    parts = s.split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        m, d, y = parts
        if filing_year and not (2008 <= int(y) <= _now + 1):
            y = str(filing_year)
        return f"{int(m):02d}/{int(d):02d}/{y}"
    return s


def parse_jammed_row(raw, member):
    """Recover a trade dict from a jammed col[0] string, or None if unparseable.
    Output shape is identical to a normally-parsed trade."""
    fields = _jammed_fields(raw)
    if fields is None:
        return None
    owner_code, tx_type, tx_date, disc_date, amount_raw, ticker, asset_desc = fields
    return {
        "transaction_date": normalize_date(tx_date, member["year"]),
        "disclosure_date": normalize_date(disc_date, member["year"]),
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
    ok = True
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
                        "transaction_date": normalize_date(row[col["tx_date"]] or "", member["year"]),
                        "disclosure_date": normalize_date(row[col["disc_date"]] or "", member["year"]),
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
        ok = False
    return trades, jammed, ok


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


def dedup_trades(trades):
    """Drop byte-identical duplicate trades the PDF parser sometimes emits
    (wrapped rows split in two, or rows repeated across a page break).
    owner is in the key on purpose: the same trade for Self vs Dependent
    Child is two real, distinct disclosures and must both be kept."""
    seen = set()
    out = []
    for r in trades:
        k = (r.get("filing_id"), r.get("ticker"), r.get("transaction_date"),
             r.get("type"), r.get("amount"), r.get("owner"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# Fields/values the dataset must satisfy after the guards have run. Anything
# the guards could NOT auto-clean is reported in plain English by the gate.
_EXPECTED_FIELDS = {
    "transaction_date", "disclosure_date", "ticker", "asset_description",
    "asset_type", "type", "amount", "amount_mid", "representative",
    "district", "owner", "filing_id", "source_url",
}
_CRITICAL_FIELDS = ("ticker", "representative", "filing_id",
                    "transaction_date", "type", "amount")
_VALID_TYPES = set(TYPE_MAP.values())
_DATE_OK_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def validate_dataset(trades):
    """Return a list of plain-English problem messages for issues the guards
    could NOT auto-clean (so a human/Claude can look). Empty list == clean."""
    issues = []
    for r in trades:
        fid = r.get("filing_id", "?")
        rep = r.get("representative", "?")
        missing = _EXPECTED_FIELDS - set(r.keys())
        if missing:
            issues.append(f"Filing {fid} ({rep}): record is missing fields {sorted(missing)}")
        for f in _CRITICAL_FIELDS:
            v = r.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                issues.append(f"Filing {fid} ({rep}): required field '{f}' is empty")
        for f in ("transaction_date", "disclosure_date"):
            v = str(r.get(f, ""))
            ok = False
            if _DATE_OK_RE.match(v):
                mm, dd, yy = int(v[:2]), int(v[3:5]), int(v[-4:])
                ok = 1 <= mm <= 12 and 1 <= dd <= 31 and 2008 <= yy <= _now + 1
            if not ok:
                issues.append(f"Filing {fid} ({rep}): {f} '{v}' isn't a readable date I could fix")
        if r.get("type") not in _VALID_TYPES:
            issues.append(f"Filing {fid} ({rep}): transaction type '{r.get('type')}' is not Purchase/Sale/Exchange")
    return issues


def main():
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "all_transactions.json")
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    existing_trades, known_ids = load_existing(out_path)
    manifest_path = os.path.join(os.path.dirname(out_path), "filings_manifest.json")
    manifest = load_manifest(manifest_path, existing_trades)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    log.info(f"Loaded {len(existing_trades)} existing trades, "
             f"{len(manifest)} filings in manifest. Scanning years: {SCAN_YEARS}")

    jammed_path = os.path.join(os.path.dirname(out_path), "jammed_rows.jsonl")
    jammed_log = open(jammed_path, "a", encoding="utf-8")

    new_trades = []
    for year in SCAN_YEARS:
        members = fetch_index(year)
        new_in_year = [m for m in members if m["doc_id"] not in manifest]
        log.info(f"  {len(new_in_year)} new filings to fetch for {year}")
        for i, member in enumerate(new_in_year):
            log.info(f"  [{year}] {i+1}/{len(new_in_year)} — {member['first']} {member['last']} ({member['doc_id']})")
            pdf_bytes = fetch_pdf(member)
            if pdf_bytes:
                trades, jammed, ok = parse_pdf(pdf_bytes, member)
                # Record in the manifest ONLY on a clean parse — even if it found
                # zero trades. A genuine parse failure (ok=False) is left out so
                # it's retried next run. The jammed count is kept so the deferred
                # orphan-fragment recovery can later find and re-fetch exactly the
                # filings that need it (jammed > 0) instead of rescraping the lot.
                if ok:
                    new_trades.extend(trades)
                    for j in jammed:
                        jammed_log.write(json.dumps(j) + "\n")
                    jammed_log.flush()
                    manifest[member["doc_id"]] = {
                        "year": member["year"],
                        "trades": len(trades),
                        "jammed": len(jammed),
                        "fetched_at": today,
                    }
            time.sleep(DELAY)

    jammed_log.close()
    log.info(f"Jammed rows log written to {jammed_path}")

    all_trades = existing_trades + new_trades
    all_trades.sort(key=sort_key, reverse=True)

    deduped = dedup_trades(all_trades)
    removed = len(all_trades) - len(deduped)
    if removed:
        log.info(f"Removed {removed} duplicate row(s) at write time")
    all_trades = deduped
    log.info(f"Total trades: {len(all_trades)} ({len(new_trades)} new)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    log.info(f"Written to {out_path}")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    log.info(f"Manifest written: {len(manifest)} filing(s) tracked")

    # Post-scrape audit gate. The data is already saved (we never lose it or
    # stall the feed); we only ALERT on anything the guards couldn't clean.
    issues = validate_dataset(all_trades)
    if not issues:
        log.info("Audit gate: dataset is clean — nothing the guards couldn't handle.")
        return
    report_path = os.path.join(os.path.dirname(out_path), "validation_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Dataset validation found {len(issues)} issue(s) the scraper could not auto-clean.\n")
        f.write("The data was still saved; these rows need a human (or Claude) to review.\n\n")
        for line in issues:
            f.write(line + "\n")
    for line in issues[:50]:
        log.error("AUDIT ISSUE: " + line)
    log.error(f"Audit gate FAILED: {len(issues)} issue(s). Details in {report_path}. "
              f"Data was saved; failing the run so you're alerted.")
    sys.exit(1)


if __name__ == "__main__":
    main()
