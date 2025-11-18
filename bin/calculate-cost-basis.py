#!/usr/bin/env python3
import sys, argparse
import pandas as pd
from datetime import datetime

def parse_date_ymd(s: str) -> datetime:
    s = s.replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d")

def parse_date_dmy(s: str) -> datetime:
    return datetime.strptime(s, "%d/%m/%Y")

def main(argv):
    p = argparse.ArgumentParser(description="Rename release confirmation PDFs using parse_pdf metadata.")
    p.add_argument("--releases", "-r", help="The stock release confirmations file in csv format", required=True)
    p.add_argument("--exrates", "-x", help="The exchange rates file in csv format", required=True)
    p.add_argument("--sales", "-s", help="The sale events files in csv format")
    args = p.parse_args(argv[1:])

    releases = args.releases
    exrates = args.exrates
    sales = args.sales

    # Load data
    releases_df = pd.read_csv(releases)
    exrates_df = pd.read_csv(exrates)

    # Normalize and convert dates
    releases_df["Date"] = releases_df["Release Date"].astype(str).str.replace("/", "-")
    releases_df["Date_dt"] = releases_df["Date"].apply(parse_date_ymd)
    exrates_df["Start_dt"] = exrates_df["Start Date"].apply(parse_date_dmy)
    exrates_df["End_dt"] = exrates_df["End Date"].apply(parse_date_dmy)

    # For each stock release date, find the valid rate
    def find_rate(date):
        for _, r in exrates_df.iterrows():
            if r["Start_dt"] <= date <= r["End_dt"]:
                return r["Currency units per £1"]
        return None

    releases_df["GBP/USD"] = releases_df["Date_dt"].apply(find_rate)

    # Sort by release date
    releases_df = releases_df.sort_values("Date_dt").reset_index(drop=True)

    # Compute weighted averages
    issued = releases_df["Issued"]
    price_usd = releases_df["Price per share ($)"]
    rates = releases_df["GBP/USD"]

    # Weighted average USD cost basis
    total_shares = issued.sum()
    cost_basis_usd = (price_usd * issued).sum() / total_shares
    # Weighted average GBP cost basis (price divided by rate)
    price_gbp = price_usd / rates
    cost_basis_gbp = (price_gbp * issued).sum() / total_shares

    # Output rows
    output_cols = ["Date", "Granted", "Withheld", "Issued", "Price per share ($)", "GBP/USD"]
    releases_df.to_csv(sys.stdout, index=False, columns=output_cols)

    # Append totals
    print(f"USD cost basis: {cost_basis_usd:.2f}")
    print(f"GBP cost basis: {cost_basis_gbp:.2f}")

if __name__ == "__main__":
    sys.exit(main(sys.argv))
