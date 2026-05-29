# House Stock Watcher — Build Brief
**For a new Claude session. Read every word before doing anything.**

---

## Who You Are Working With

**Chris** — no coding background, no stock market knowledge. Windows 11 machine. Plain English required at all times. No jargon without explanation.

**Non-negotiable working rules:**
- No code in chat — files only, always complete
- 3-sentence plan summary before any code, no code without Chris's go-ahead
- One step at a time — present a plan, wait for approval, then build
- Explain everything like Chris has never touched code before
- Token charge after every response
- Accuracy is the only standard. Anything less than 100% is unacceptable.

---

## Why This Project Exists

Chris is building **Capitol Edge** — a personal stock intelligence dashboard that tracks congressional trade disclosures (members of Congress must disclose stock trades within 45 days under the STOCK Act), cross-references them with news, insider trades, and macro indicators, and produces plain-English BUY/SELL/AVOID recommendations.

Capitol Edge needs House of Representatives trade data. The problem:

- **housestockwatcher.com** — the only known free House trade data source — went offline in 2025. Returns HTTP 403 errors. Dead.
- **Financial Modeling Prep (FMP) free tier** — the current fallback in Capitol Edge — is hard-capped at 100 records per call, page 0 only. Attempting page 1 returns a 402 "upgrade required" error. It gives a rolling window of the latest 100 House trades and nothing more. No historical data.
- **All other free alternatives** (Politician Trade Tracker, Quiver Quantitative, etc.) either have extremely low free tier limits (60 requests/month) or require payment.

The Senate equivalent — the Senate Stock Watcher GitHub repo at `https://github.com/timothycarambat/senate-stock-watcher-data` — works beautifully. It fetches Senate disclosures from the Senate's public API, stores them as a flat JSON file, and serves them via `raw.githubusercontent.com`. Capitol Edge calls one URL and gets all 8,350+ Senate trades for free, forever.

**The goal of this project:** Build the exact same thing for House data. A public GitHub repository that fetches House trade disclosures from the official government source, parses them, and serves a clean flat JSON file that Capitol Edge (and anyone else) can call for free.

---

## What We Discovered About the Official House Data Source

This was fully investigated in the previous session. Here are the confirmed facts.

### The Official Source

The U.S. House Clerk publishes a public ZIP file for each calendar year:

```
https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
```

Examples:
- `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2025FD.zip`
- `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2024FD.zip`
- `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2023FD.zip`

These URLs require no login, no API key, and no payment. Plain HTTPS GET.

**Required header on all requests:**
```
User-Agent: HouseStockWatcher/1.0 your@email.com
```

Without a User-Agent the server may reject the request.

### What's Inside Each ZIP

Two files:
- `<YEAR>FD.xml` — index of all financial disclosures filed that year
- `<YEAR>FD.txt` — same data as tab-separated text

The XML and TXT are **indexes only** — they list who filed what, but do not contain the actual trade data. They are a table of contents.

### The XML Index Structure

Each entry in the XML looks like this:

```xml
<Member>
  <Prefix>Hon.</Prefix>
  <Last>Aderholt</Last>
  <First>Robert B.</First>
  <Suffix />
  <FilingType>P</FilingType>
  <StateDst>AL04</StateDst>
  <Year>2025</Year>
  <FilingDate>9/10/2025</FilingDate>
  <DocID>20032062</DocID>
</Member>
```

**FilingType codes — only one matters for us:**

| Code | Meaning |
|---|---|
| **P** | **Periodic Transaction Report — this is the stock trade disclosure** |
| C | Annual financial disclosure report |
| D | Annual report for newly elected members |
| A | Amendment to a prior filing |
| X | Extension request |
| W | Withdrawal |
| T, E, G, B, O, H | Various other filing types |

**We only want FilingType = "P".** In 2025, there are 515 PTR filings.

### Where the Actual Trade Data Lives

Each PTR filing (FilingType = P) has a corresponding PDF at:

```
https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf
```

Example:
```
https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20032062.pdf
```

**Confirmed fact:** There is no XML or JSON version of individual filings. The following were all tested and all returned 404:
- `<DocID>.xml` at the ptr-pdfs URL
- `<DocID>.json` at the ptr-pdfs URL
- `DisclosureSearch/result?docid=<DocID>`
- `DisclosureSearch/GetMemberSearchResult?docid=<DocID>`

The trade data lives only in the PDFs. The PDFs must be parsed.

### PDF Structure — Confirmed Parseable

**Tool required:** `pdfplumber` (Python library, free, `pip install pdfplumber`)

The PDFs have a consistent table structure. This was verified across 5 different filings from different members.

**Table columns (in order):**
1. ID (usually blank)
2. Owner (who made the trade — see codes below)
3. Asset (company name with ticker and asset type embedded)
4. Transaction Type (P = Purchase, S = Sale, E = Exchange)
5. Date (trade date, format MM/DD/YYYY)
6. Notification Date (disclosure date, format MM/DD/YYYY)
7. Amount (dollar range string)
8. Cap. Gains > $200? (Yes/No, usually blank)

**Owner codes:**
| Code | Meaning |
|---|---|
| (blank) | The member themselves |
| SP | Spouse |
| JT | Joint (member + spouse) |
| DC | Dependent child |

**Asset field format:**
The asset name contains the ticker in parentheses and the asset type in brackets:
```
Apple Inc. - Common Stock (AAPL) [ST]
Kroger Company (KR) [ST]
GSK plc American Depositary Shares (GSK) [ST]
```

**Asset type codes in brackets (the ones we care about):**
| Code | Meaning | Include? |
|---|---|---|
| [ST] | Stock | YES |
| [EQ] | Equity equivalent | YES |
| [BO] | Bond / corporate bond | NO — no stock ticker |
| [RE] | Real estate | NO |
| [MU] | Municipal security | NO |
| (no bracket) | Unknown / other | SKIP |

**Critical parsing detail:** pdfplumber extracts each trade as two rows, not one. The first row is a garbled partial line. The second row contains the actual usable data. The pattern of the useful row is:

```python
['', 'OWNER_CODE', 'Company Name (TICKER)\n[TYPE]', 'P/S/E', 'MM/DD/YYYY', 'MM/DD/YYYY', '$X - $Y', '']
```

To identify a real data row: Transaction Type column (index 3) must be exactly `P`, `S`, or `E`.

**Real examples extracted from actual PDFs:**

From Robert B. Aderholt (DocID 20032062):
```
Owner: (self) | Asset: GSK plc American Depositary Shares (GSK) [ST] | Type: S | Trade: 07/28/2025 | Disclosure: 08/11/2025 | Amount: $1,001 - $15,000
```

From Marjorie Taylor Greene (DocID 20026791, 2 pages, multiple trades):
```
Owner: (self) | Asset: Apple Inc. - Common Stock (AAPL) [ST] | Type: P | Trade: 02/12/2025 | Disclosure: 02/13/2025 | Amount: $1,001 - $15,000
```

From Greg Landsman (DocID 20030207):
```
Owner: SP | Asset: Kroger Company (KR) [ST] | Type: S | Trade: 05/09/2025 | Disclosure: 05/12/2025 | Amount: $250,001 - $500,000
```

**Known edge cases to handle:**
- Multi-page PDFs (some members have many trades — iterate all pages)
- Null bytes (`\x00`) appear in some footer text (not in data fields — safe to ignore)
- Ticker sometimes cut off due to column width — extract from the full asset string using regex `\(([A-Z]{1,5})\)`
- Some bonds/real estate entries have no ticker in parentheses — these will naturally fail the regex and get dropped

---

## What the Output JSON Should Look Like

The output must be a single flat JSON array that Capitol Edge can consume as a drop-in replacement for the FMP House data. Match this schema exactly:

```json
[
  {
    "transaction_date": "02/12/2025",
    "disclosure_date": "02/13/2025",
    "ticker": "AAPL",
    "asset_description": "Apple Inc. - Common Stock",
    "asset_type": "Stock",
    "type": "Purchase",
    "amount": "$1,001 - $15,000",
    "representative": "Marjorie Taylor Greene",
    "district": "GA14",
    "owner": "Self",
    "filing_id": "20026791",
    "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20026791.pdf"
  }
]
```

**Field mapping rules:**
- `transaction_date` → Date column from PDF table (MM/DD/YYYY)
- `disclosure_date` → Notification Date column from PDF table (MM/DD/YYYY)
- `ticker` → extracted from parentheses in Asset field using regex `\(([A-Z]{1,5})\)`
- `asset_description` → Asset field with ticker/type codes stripped
- `asset_type` → mapped from bracket code: `[ST]`/`[EQ]` → "Stock", others → their full name
- `type` → P → "Purchase", S → "Sale", E → "Exchange"
- `amount` → Amount column verbatim
- `representative` → First + Last from XML index entry
- `district` → StateDst from XML index entry
- `owner` → mapped: blank → "Self", SP → "Spouse", JT → "Joint", DC → "Dependent Child"
- `filing_id` → DocID from XML index
- `source_url` → constructed PDF URL

---

## GitHub Repository Setup

### Step 1 — Create a GitHub Account

1. Go to **https://github.com**
2. Click **Sign up**
3. Use email: illshootthat@gmail.com
4. Choose a username (suggestion: `djchrismit` or `capitoledge` or similar — your choice)
5. Complete the free account setup. No credit card required.

### Step 2 — Create the Repository

1. Once logged in, click the **+** icon (top right) → **New repository**
2. Repository name: `house-stock-watcher-data`
3. Description: `Free public House of Representatives stock trade disclosures — rebuilt from official House Clerk PTR filings`
4. Visibility: **Public** (required — private repos can't serve raw files for free)
5. Check **Add a README file**
6. Click **Create repository**

### Step 3 — What Goes in the Repo

```
house-stock-watcher-data/
├── .github/
│   └── workflows/
│       └── update.yml          ← GitHub Actions workflow (runs daily)
├── data/
│   └── all_transactions.json   ← The main output file Capitol Edge reads
├── scraper/
│   └── fetch.py                ← The Python scraper script
├── requirements.txt
└── README.md
```

### Step 4 — The GitHub Actions Workflow

The workflow must:
- Run on a daily schedule (cron)
- Also be triggerable manually (workflow_dispatch)
- Install Python + pdfplumber + requests
- Run the scraper
- Commit and push any changes to `data/all_transactions.json`

GitHub Actions is **free for public repositories** with no monthly limit.

---

## The Scraper — What It Must Do

In plain English, the scraper does this:

1. Download the ZIP files for the current year and the past N years from the House Clerk
2. Parse the XML index inside each ZIP to get all PTR filing DocIDs and member names
3. For each DocID, download the corresponding PDF
4. Extract the trade table from the PDF using pdfplumber
5. For each row where the Transaction Type is P, S, or E and the asset has a `[ST]` or `[EQ]` tag:
   - Extract the ticker using regex
   - Map all fields to the output schema
6. Combine all years into one flat JSON array
7. Sort by disclosure_date descending (newest first)
8. Write to `data/all_transactions.json`

**Rate limiting:** Add a 0.5-second delay between PDF downloads. The House Clerk server is a government server — don't hammer it.

**Error handling:** If a PDF fails to download or parse, log the DocID and skip it. Do not crash the whole run.

**Historical data:** Start with 2022, 2023, 2024, 2025, and 2026 ZIP files. Each year is a separate ZIP at the same URL pattern.

---

## How Capitol Edge Will Use This Repo

Once the repo is live and `data/all_transactions.json` is populated, Capitol Edge's `services/congressional.py` will be updated to replace the FMP House call with:

```
https://raw.githubusercontent.com/<YOUR_USERNAME>/house-stock-watcher-data/main/data/all_transactions.json
```

This is identical to how Capitol Edge already fetches Senate data from the Senate GitHub repo. One HTTP GET, full history, always current, completely free.

---

## Current Capitol Edge Status (Context Only)

Capitol Edge is a separate project currently in progress at:
`C:\Users\djchr\Documents\App Development\Stock Trading App\capitol-edge\`

Phase 1, Steps 1–4 are complete and tested:
- Step 1: Project setup (folders, Python venv, React/Vite, .env, start.bat)
- Step 2: Database (all tables, WAL mode, 4 indexes)
- Step 3: Congressional fetcher (Senate GitHub repo + FMP House)
- Step 4: Stock price fetcher (yfinance, tested and working)

**Step 5 (congressional + stock normalizer) is paused** while this House scraper repo is built. Once this repo is live and serving data, the normalizer will be updated to consume it instead of FMP, and Capitol Edge development will resume at Step 5.

The tech stack for Capitol Edge is Python/FastAPI backend + React/Vite frontend + SQLite database. The Python venv uses Python 3.12 (not 3.14) at:
`C:\Users\djchr\Documents\App Development\Stock Trading App\capitol-edge\backend\venv\`

---

## Before Writing Any Code in the New Session

1. Confirm Chris has created his GitHub account
2. Confirm the repo `house-stock-watcher-data` is created and public
3. Present the 3-sentence plan for the scraper
4. Get explicit go-ahead before writing any file
5. Build `scraper/fetch.py` first and test it locally
6. Then build the GitHub Actions workflow
7. Then wire Capitol Edge to consume the new URL
