#!/usr/bin/env python3
"""Download HMRC monthly exchange-rate CSVs from the Trade Tariff API."""

import argparse
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

URL = ("https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/"
       "files/monthly_csv_{year}-{month}.csv")


def months(start, end):
    """Yield (year, month) tuples inclusive from start to end (date objects)."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def download_rates(start, end, dest=Path(".")):
    """Download monthly CSVs for the [start, end] month range into dest.

    Returns the list of saved Paths. Months not yet published (404) are
    skipped, since HMRC publishes mid-month for the month ahead.
    """
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for year, month in months(start, end):
        url = URL.format(year=year, month=month)
        try:
            with urlopen(url) as resp:
                data = resp.read()
        except HTTPError as e:
            if e.code == 404:  # not published yet
                sys.stderr.write(f"skip {year}-{month}: not published\n")
                continue
            raise
        out = dest / f"monthly_csv_{year}-{month}.csv"
        out.write_bytes(data)
        saved.append(out)
        sys.stderr.write(f"saved {out}\n")
    return saved


def _parse_month(s):
    return date(*map(int, s.split("-")), 1) if "-" in s else date(int(s), 1, 1)


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("start", type=_parse_month, help="start month, YYYY-M (or YYYY = January)")
    p.add_argument("end", type=_parse_month, nargs="?", default=date.today(),
                   help="end month, YYYY-M (default: this month)")
    p.add_argument("-d", "--dest", type=Path, default=Path("."), help="output directory")
    args = p.parse_args(argv[1:])
    download_rates(args.start, args.end, args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
