#!/usr/bin/env python3
import sys
import pandas as pd
from datetime import datetime

def parse_date_ymd(s: str) -> datetime:
    s = s.replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d")

def parse_date_dmy(s: str) -> datetime:
    return datetime.strptime(s, "%d/%m/%Y")

def main(argv):
    if len(argv) != 3:
        sys.stderr.write("Usage: calculate-cost-basis.py <stock_release.csv> <exchange_rates.csv>\n")
        return 2

    stock_csv = argv[1]
    rates_csv = argv[2]

    # Load data
    stock_df = pd.read_csv(stock_csv)
    rates_df = pd.read_csv(rates_csv)

    # Normalize and convert dates
    stock_df["Date"] = stock_df["Date"].astype(str).str.replace("/", "-")
    stock_df["Date_dt"] = stock_df["Date"].apply(parse_date_ymd)
    rates_df["Start_dt"] = rates_df["Start Date"].apply(parse_date_dmy)
    rates_df["End_dt"] = rates_df["End Date"].apply(parse_date_dmy)

    # For each stock release date, find the valid rate
    def find_rate(date):
        for _, r in rates_df.iterrows():
            if r["Start_dt"] <= date <= r["End_dt"]:
                return r["Currency units per £1"]
        return None

    stock_df["GBP/USD"] = stock_df["Date_dt"].apply(find_rate)

    # Sort by release date
    stock_df = stock_df.sort_values("Date_dt").reset_index(drop=True)

    # Compute weighted averages
    issued = stock_df["Issued"]
    price_usd = stock_df["Price per share ($)"]
    rates = stock_df["GBP/USD"]

    # Weighted average USD cost basis
    total_shares = issued.sum()
    cost_basis_usd = (price_usd * issued).sum() / total_shares
    # Weighted average GBP cost basis (price divided by rate)
    price_gbp = price_usd / rates
    cost_basis_gbp = (price_gbp * issued).sum() / total_shares

    # Output rows
    output_cols = ["Date", "Granted", "Withheld", "Issued", "Price per share ($)", "GBP/USD"]
    stock_df.to_csv(sys.stdout, index=False, columns=output_cols)

    # Append totals
    print(f"USD cost basis: {cost_basis_usd:.2f}")
    print(f"GBP cost basis: {cost_basis_gbp:.2f}")

if __name__ == "__main__":
    sys.exit(main(sys.argv))
