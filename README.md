# README

## Disclaimer
This project helps you parse RSU release PDFs from e*trade, convert them to CSV, enrich with FX rates / sales, compute gains and losses suitable for reporting to HMRC.

I created it as a personal project for personal use, but I am now sharing it in the hope that it can help others to avoid some of the problems that arise from having to deal with a US-centric stock broker as a British taxpayer. With that said I can definitely **not guarantee** that anything in this project is correct or even useful, nor that you won't be punished by HMRC if you use my tools.

The "same day" and "bed and breakfasting" rules are currently ignored. I will probably implement them soon.

## Dependencies
In order to run the tools you will need python 3 (version 3.12 or later is recommended) and pip.
virtualenv is recommended.
You can install them on Debian with:
```
sudo apt install python3 python3-pip python3-virtualenv
```

The pandas and pdfminer.six python modules are also required. They automatically installed in a virtual environment if `setup.sh` is run.

## Setup and Run
Execute `setup.sh` to set up the environment. Execute `run.sh` to run the tools in sequence and
go from a bunch of pdf files from e*trade and a list of sales to a full calculation of the gains and losses. Start with `run.sh -h` to see a description of the parameters.

### Stock Release Confirmations
These can be obtained from e*trade as PDF files. In their website go to the "At work" section and select "My Account" -> "Benefit History". Expand "Restricted Stock (RS)" and click on the "View Confirmation of Release" links to download the files. For convenience you can use short names for the files (e.g. single digits or characters), then use `rename-release-confirmations.py` to automatically give them sensible names.

### Sales
The list of sales must be manually created. A sample file is provided in sales/sales.csv. Use the same format.

### Exchange Rates
The monthly exchange rates from HMRC that cover the period from January 2018 to November 2025 (or a later month if I remember to update this project) are already included. You can get more from https://www.trade-tariff.service.gov.uk/exchange_rates. Note that there exist no "official" exchange rates as such. HMRC accepts other sources too, e.g. your own bank or the Bank of England. However you must use the same source consistently throughout your calculations.

## Components

### 1) `parse-stock-releases.py`
E*trade provides all the information relative to vested RSUs: release date, number of shares released, number of shares sold to cover tax, market value per share, etc. The problem is that all of those goodies are encoded in inconvient PDF files. The `parse-stock-releases.py` script is intended to extract the relevant data from the PDF files and print it in a convenient CSV format that is suitable for further processing. If you are here chances are that you came specifically for this tool.
- CLI that reads one or more release confirmation PDFs.
- Uses `parse_pdf` and outputs:
  - `Release Date, Granted, Withheld, Issued, Price per share ($)`
- Sorted by `Release Date` ascending.
- **Output:** CSV to stdout.

### 2) `calculate-cost-basis.py`
- **Inputs:**
  - Releases CSV (from `parse-stock-releases.py`)
  - FX table `exchange-rates.csv` (`Start Date`/`End Date` in `DD/MM/YYYY`, `Currency units per £1` = GBP→USD)
  - Optional `sales.csv` (independent sell transactions)
- **Features:**
  - Appends a valid **`GBP/USD`** rate on each event date.
  - Merges **Buy (releases)** and **Sell (sales)** into one timeline and sorts by date.
  - Prepends `Type` column: `Buy` or `Sell`.
  - Adds `Avg cost per share (GBP)` as a running weighted-average of holdings after each event, as per the Section 104 holding rules.
  - Prints final **USD** and **GBP** cost basis across **buys only** (weighted by `Issued`).
- **Flexible sales headers:** auto-detects typical columns (date, shares, USD price). If your headers differ, adjust the detection list in the script.

### 3) `rename-release-confirmations.py`
A convenience script that renames PDFs using `parse_pdf` metadata.

### 4) `parse_pdf.py` (module)
It provides a single function to be used in other scripts.
- **Function:** `parse_pdf(path: Path) -> dict`
- **Extracts:**  
  - `Release Date` (release date, `YYYY-MM-DD`)  
  - `Granted`, `Withheld`, `Issued` (share counts)  
  - `Price per share ($)`  
  - `Award Date`, `Award Number`
- **How it works:** PDF text-box layout parsing via `pdfminer.six` with positional lookups next to labels (e.g., “Release Date”, “Award Shares”, “Award Date”, “Award Number”, etc.).

### References
The relevant HMRC rules can be found at https://www.gov.uk/government/publications/shares-and-capital-gains-tax-hs284-self-assessment-helpsheet/hs284-shares-and-capital-gains-tax-2024.
