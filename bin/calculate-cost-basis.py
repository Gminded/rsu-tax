# Copyright (C) 2025 Gianluca Guidi
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, see
# <https://www.gnu.org/licenses/>.

import sys, argparse
import pandas as pd
from datetime import datetime

TYPE_LABEL = "Type"
DATE_LABEL = "Date"
GRANTED_LABEL = "Granted"
SOLD_LABEL = "Sold"
ISSUED_LABEL = "Issued"
START_DATE_LABEL = "Start Date"
END_DATE_LABEL = "End Date"
RELEASE_DATE_LABEL = "Release Date"
PRICE_PER_SHARE_USD_LABEL = "Price per share ($)"
PRICE_PER_SHARE_GBP_LABEL = "Price per share (GBP)"
GBP_USD_LABEL = "GBP/USD"
HOLDINGS_GBP_LABEL = "Holdings (GBP)"
GAINS_LABEL = "Gains / Losses (GBP)"

# ---------- Date parsing ----------
def parse_date_ymd(s: str) -> datetime:
    # normalize separators then parse YYYY-MM-DD
    s = str(s).strip().replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d")

def parse_date_dmy(s: str) -> datetime:
    # DD/MM/YYYY
    return datetime.strptime(str(s).strip(), "%d/%m/%Y")

# ---------- Exchange rate lookup (inclusive ranges) ----------
def attach_rate(df_dates: pd.Series, exrates_df: pd.DataFrame) -> pd.Series:
    # naive O(N*M) scan; fine for small tables. For big tables, replace with interval merge.
    def find_rate(d):
        for _, r in exrates_df.iterrows():
            if r["Start_dt"] <= d <= r["End_dt"]:
                return r["Currency units per £1"]
        return None
    return df_dates.apply(find_rate)

# ---------- Sales CSV helpers ----------
CANDIDATE_DATE = ["date", "sale date", "transaction date"]
CANDIDATE_SHARES = ["shares", "quantity", "units", "issued", "shares sold", "qty"]
CANDIDATE_PRICE = ["price per share ($)", "priceusd", "price", "sale price", "sale price ($)"]

def _find_col(cols, candidates):
    lc = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in lc:
            return lc[cand]
    # loose contains match
    for c in cols:
        cl = c.lower()
        if any(cand in cl for cand in candidates):
            return c
    return None

def load_sales(sales_csv: str) -> pd.DataFrame:
    s = pd.read_csv(sales_csv)
    cols = list(s.columns)
    date_col = _find_col(cols, CANDIDATE_DATE)
    shares_col = _find_col(cols, CANDIDATE_SHARES)
    price_col = _find_col(cols, CANDIDATE_PRICE)
    if not date_col or not shares_col or not price_col:
        raise ValueError(
            f"sales.csv missing recognizable columns. "
            f"Found: {cols}. Need Date~{CANDIDATE_DATE}, "
            f"Shares~{CANDIDATE_SHARES}, Price~{CANDIDATE_PRICE}"
        )
    out = pd.DataFrame({
        DATE_LABEL: s[date_col].astype(str).str.replace("/", "-"),
        SOLD_LABEL: s[shares_col].astype(float),   # will be treated as positive count sold
        PRICE_PER_SHARE_USD_LABEL: s[price_col].astype(float),
    })
    return out


def get_gains_and_holdings(events):
    gains_series = []
    holdings_gbp_series = []
    holdings_units = 0.0
    holdings_gbp = 0.0

    for i, row in events.iterrows():
        typ = row[TYPE_LABEL]
        price_gbp = float(row[PRICE_PER_SHARE_GBP_LABEL] or 0.0)
        gain = 0

        if typ == "Buy":
            issued_units = float(row[ISSUED_LABEL] or 0.0)
            # Add the allowable expenditure on the new shares to the pool of cost
            holdings_gbp += issued_units * price_gbp
            holdings_gbp_series.append(holdings_gbp)
            # Increase number of shares
            holdings_units += issued_units
        else:  # Sell
            sold_units = float(row[SOLD_LABEL] or 0.0)
            # To calculate the gain or loss...
            # First, calculate the amount of allowable expenditure by multiplying the pool of cost by
            allowable_cost = holdings_gbp * sold_units / holdings_units
            # Second, calculate the gain or loss
            disposal_proceeeds = sold_units * price_gbp
            gain = disposal_proceeeds - allowable_cost
            # Third, adjust the Section 104 holding
            holdings_units -= sold_units
            holdings_gbp -= allowable_cost
            holdings_gbp_series.append(holdings_gbp)
            # Check for errors
            if holdings_units < 0:
                # If a sell exceeds holdings, we stop and warn to stderr.
                print(f"[ERROR] Selling more shares than currently held; holdings became negative at row {i}.", file=sys.stderr)
                raise ValueError("Selling more shares than currently held")

        gains_series.append(gain)
    return gains_series, holdings_gbp_series


def print_events(events):
    # --- Output rows (with requested first column Type) ---
    output_cols = [TYPE_LABEL, DATE_LABEL, GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL, HOLDINGS_GBP_LABEL, GAINS_LABEL]
    out = events.copy()
    for c in (PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL, GAINS_LABEL, HOLDINGS_GBP_LABEL):
        out[c] = out[c].apply(lambda x: f"{x:.4f}" if pd.notnull(x) else "")
    out.to_csv(sys.stdout, index=False, columns=output_cols, float_format="%.0f")


def main(argv):
    p = argparse.ArgumentParser(description="Calculate combined cost basis with optional sales.")
    p.add_argument("--releases", "-r", required=True, help="Stock releases CSV (from parse-stock-releases.py)")
    p.add_argument("--exrates", "-x", required=True, help="Exchange rates CSV (Start/End Date in DD/MM/YYYY)")
    p.add_argument("--sales", "-s", help="Sales CSV (optional)")
    args = p.parse_args(argv[1:])

    # --- Load releases ---
    rel = pd.read_csv(args.releases)

    # Earlier versions sometimes had 'Release Date' or 'Date'. Normalize.
    if DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[DATE_LABEL].astype(str).str.replace("/", "-")
    elif RELEASE_DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[RELEASE_DATE_LABEL].astype(str).str.replace("/", "-")
    else:
        raise ValueError("Releases CSV must include 'Date' or 'Release Date' column.")

    # Required columns for releases
    required_rel = [GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL]
    for col in required_rel:
        if col not in rel.columns:
            raise ValueError(f"Releases CSV missing required column: {col}")

    DATE_DT = "Date_dt"
    rel[DATE_DT] = rel[DATE_LABEL].apply(parse_date_ymd)

    # --- Load exchange rates and build date ranges ---
    xr = pd.read_csv(args.exrates)
    xr["Start_dt"] = xr[START_DATE_LABEL].apply(parse_date_dmy)
    xr["End_dt"]   = xr[END_DATE_LABEL].apply(parse_date_dmy)

    # Attach rate to releases
    rel[GBP_USD_LABEL] = attach_rate(rel[DATE_DT], xr)

    # Type column for releases
    rel[TYPE_LABEL] = "Buy"

    # --- Sales (optional) ---
    sales = args.sales
    if sales:
        s = load_sales(args.sales)
        s[DATE_DT] = s[DATE_LABEL].apply(parse_date_ymd)
        s[GBP_USD_LABEL] = attach_rate(s[DATE_DT], xr)
        # For sales, set Granted/Issued to 0, keep Sold as "shares sold"
        s[GRANTED_LABEL] = 0.0
        s[ISSUED_LABEL] = 0.0
        s[TYPE_LABEL] = "Sell"
        # Reorder to match releases columns
        s = s[[TYPE_LABEL, DATE_LABEL, DATE_DT, GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL]]
    # Align releases columns similarly
    rel = rel[[TYPE_LABEL, DATE_LABEL, DATE_DT, GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL]]

    # --- Combine & sort ---
    events = rel if sales is None else pd.concat([rel, s], ignore_index=True)
    events = events.sort_values(DATE_DT).reset_index(drop=True)

    # --- Running average cost per share in GBP (weighted-average method) ---
    # price_gbp = USD price / (GBP/USD)
    events[PRICE_PER_SHARE_GBP_LABEL] = events[PRICE_PER_SHARE_USD_LABEL] / events[GBP_USD_LABEL]
    gains, holdings = get_gains_and_holdings(events)
    events[GAINS_LABEL] = gains
    events[HOLDINGS_GBP_LABEL] = holdings
    print_events(events)

if __name__ == "__main__":
    sys.exit(main(sys.argv))
