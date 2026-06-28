# README

## Disclaimer
This project helps you parse RSU release PDFs from e*trade, convert them to CSV, enrich with FX rates / sales, compute gains and losses suitable for reporting to HMRC.

I created it as a personal project for personal use, but I am now sharing it in the hope that it can help others to avoid some of the problems that arise from having to deal with a US-centric stock broker as a British taxpayer. With that said I can definitely **not guarantee** that anything in this project is correct or even useful, nor that you won't be punished by HMRC if you use my tools.

The HMRC share-identification rules are implemented in full: same-day acquisitions are matched first, followed by acquisitions in the 30 days after the disposal (bed-and-breakfasting rule), and any remaining quantity is matched against the Section 104 pool. The output includes a "Matching Rule" column showing which rule(s) were applied to each disposal.

## Dependencies
In order to run the tools you will need python 3 (version 3.12 or later is recommended) and pip.
virtualenv is recommended.
You can install them on Debian with:
```
sudo apt install python3 python3-pip python3-virtualenv
```

The pandas and pdfminer.six python modules are also required. They automatically installed in a virtual environment if `setup.sh` is run.

## Setup and Run

Execute `setup.sh` to set up the environment.

### Web GUI

The recommended way to use the tools is the web GUI. Launch it with:
```
source env/bin/activate
streamlit run app.py
```
Then open `http://localhost:8501` in your browser.

The GUI has three input sections:

- **RSU Releases** — drag-and-drop one or more e*trade release confirmation PDFs. Each file is parsed immediately and the results appear in an editable table so you can correct any parse errors.
- **Sales & other acquisitions** — an editable table for voluntary sell transactions (not tax-withholding) and for generic acquisitions (ESPP, open-market buys, option exercises) that should join the Section 104 pool. A **Type** dropdown marks each row as *Sell* or *Buy*. Pre-loaded from `sales/sales.csv` if it exists.
- **Exchange Rates** — automatically loaded from the bundled HMRC monthly rate files. You can upload additional CSVs here if your releases fall outside the covered period.

Click **Calculate Gains & Losses** to run the full HS284 calculation. The results show a capital gains summary by UK tax year and a complete event timeline, with a **Download CSV** button to export the output.

### Command-line pipeline

Execute `run.sh` to run the tools in sequence and go from a bunch of PDF files from e*trade and a list of sales to a full calculation of the gains and losses. Start with `run.sh -h` to see a description of the parameters.

### Stock Release Confirmations
These can be obtained from e*trade as PDF files. The easiest way is to run `download_etrade.py`, which logs in to E*Trade and downloads all release confirmation PDFs automatically. Alternatively, in their website go to the "At work" section and select "My Account" -> "Benefit History". Expand "Restricted Stock (RS)" and click on the "View Confirmation of Release" links to download the files. For convenience you can use short names for the files (e.g. single digits or characters), then use `rename-release-confirmations.py` to automatically give them sensible names.

### Sales and other acquisitions
The list of sales must be manually created. A sample file is provided in sales/sales.csv. Use the same format.

The file can also carry **acquisitions other than RSU releases** — ESPP purchases, open-market buys, or option exercises — so they join the **same Section 104 pool** as your RSUs (HMRC pools all shares of the same class together, regardless of how they were acquired). Add an optional `Type` column and set it to `Buy` for an acquisition or `Sell` for a disposal; rows with no `Type` are treated as sells. For a `Buy` row the price is the per-share cost basis. An optional `Fee ($)` column records the broker fee on a disposal, an allowable incidental cost of disposal (TCGA 1992 s.38(1)(c)) that is deducted from the gain. For example:

```
Date,Type,Shares,Price per share ($),Fee ($)
2024-03-15,Buy,40,98.50,         # ESPP purchase into the pool
2024-06-01,Sell,100,127.05,9.99  # disposal, with a $9.99 broker fee
```

In the web GUI the same thing is done with the **Type** dropdown in the "Sales & other acquisitions" table.

### Exchange Rates
The monthly exchange rates from HMRC that cover the period from January 2018 to November 2025 (or a later month if I remember to update this project) are already included. You can get more from https://www.trade-tariff.service.gov.uk/exchange_rates, or run `download-rates.sh` to download them automatically. Note that there exist no "official" exchange rates as such. HMRC accepts other sources too, e.g. your own bank or the Bank of England. However you must use the same source consistently throughout your calculations.

## Tests

Activate the virtual environment and run pytest:
```
source env/bin/activate
python -m pytest tests/ -v
```

## Components

### 1) `parse-stock-releases.py`
E*trade provides all the information relative to vested RSUs: release date, number of shares released, number of shares sold to cover tax, market value per share, etc. The problem is that all of those goodies are encoded in inconvient PDF files. The `parse-stock-releases.py` script is intended to extract the relevant data from the PDF files and print it in a convenient CSV format that is suitable for further processing. If you are here chances are that you came specifically for this tool.
- CLI that reads one or more release confirmation PDFs.
- Uses `parse_pdf` and outputs:
  - `Release Date, Granted, Withheld, Issued, Price per share ($), Sale price per share ($), Fee ($)`
  - `Sale price per share ($)` and `Fee ($)` are populated only for sell-to-cover releases (where the broker sold the withheld shares on the market); they are blank for net-settled (`Shares Traded`) releases.
- Sorted by `Release Date` ascending.
- **Output:** CSV to stdout.

### 2) `combine.py`
Merges multiple HMRC monthly exchange-rate CSVs into a single file containing only the USD rows.
- CLI that reads one or more HMRC monthly CSV files.
- Extracts the `USA,Dollar,USD` row from each file and writes a combined CSV to stdout.
- **Output:** CSV to stdout (fed into `calculate-cost-basis.py` via `run.sh`).

### 3) `calculate-cost-basis.py`
- **Inputs:**
  - Releases CSV (from `parse-stock-releases.py`)
  - FX table `exchange-rates.csv` (`Start Date`/`End Date` in `DD/MM/YYYY`, `Currency units per £1` = GBP→USD)
  - Optional `sales.csv` (independent sell transactions)
- **Features:**
  - Appends a valid **`GBP/USD`** rate on each event date.
  - Merges **Buy (releases)** and **Sell (sales)** into one timeline and sorts by date.
  - Prepends `Type` column: `Buy` or `Sell`.
  - Implements the full HMRC share-identification order: same-day rule → 30-day rule → Section 104 pool.
  - Inserts `WithholdingSell` rows for shares the broker withheld to cover income tax on RSU vests. Their cost basis is the market value at release (same-day rule); the gain is zero when the shares were net-settled at market value (`Shares Traded`), but a `Shares Sold` release records a separate `Sale price per share` and the difference from market value, **less the broker `Fee`**, is a (usually small) chargeable gain/loss.
  - Deducts any broker **`Fee`** as an allowable incidental cost of disposal (TCGA 1992 s.38(1)(c)) — both the sell-to-cover fee parsed from release confirmations and an optional `Fee ($)` column on `sales.csv` disposals. The fee is converted to GBP at the disposal-date rate and subtracted from the gain.
  - Adds a `Matching Rule` column indicating which identification rule(s) applied to each disposal.
  - Adds `Price per share (GBP)`, `Sale price per share (GBP)` and `Fee (GBP)` as output columns for easier verification.
  - The tax-year summary counts **taxable events** (genuine sells plus withholding sells whose sale price differed from market value) and lists every UK tax year in range, including those with no taxable events.
  - Prints a capital-gains summary by UK tax year to stderr after generating the CSV.
- **Flexible sales headers:** auto-detects typical columns (date, shares, USD price). If your headers differ, adjust the detection list in the script.

### 4) `download_etrade.py`
Downloads all release confirmation PDFs from E*Trade automatically using Playwright.
- On first run it opens a visible browser so you can log in; the session is saved to `.etrade_session.json` and subsequent runs are headless.
- Uses the Stock Plan Confirmations JSON API to obtain an authoritative list of every confirmation, then downloads each PDF by its unique `confirmationId`.
- Skips files that are already on disk, making re-runs idempotent.
- Renames each downloaded PDF to its canonical name via `rename-release-confirmations.py`.
- **Output:** PDFs saved to `release-confirmations/`.

### 5) `download-rates.sh`
Downloads HMRC monthly exchange-rate CSV files for every month from 2021 to the current year.
- Iterates over years and months, fetching each CSV from the HMRC trade-tariff service.
- Run from the directory where you want the files saved, or move them into `monthly-exchange-rates-by-hmrc/` afterwards.

### 6) `rename-release-confirmations.py`
A convenience script that renames PDFs using `parse_pdf` metadata.

### 7) `parse_pdf.py` (module)
It provides a single function to be used in other scripts.
- **Function:** `parse_pdf(path: Path) -> dict`
- **Extracts:**  
  - `Release Date` (release date, `YYYY-MM-DD`)  
  - `Granted`, `Withheld`, `Issued` (share counts)  
  - `Price per share ($)`  
  - `Award Date`, `Award Number`
- **How it works:** PDF text-box layout parsing via `pdfminer.six` with positional lookups next to labels (e.g., “Release Date”, “Award Shares”, “Award Date”, “Award Number”, etc.).

### References
The relevant HMRC rules can be found at https://www.gov.uk/government/publications/shares-and-capital-gains-tax-hs284-self-assessment-helpsheet/hs284-shares-and-capital-gains-tax-2026.
