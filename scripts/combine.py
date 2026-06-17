#!/usr/bin/env python3

import sys
import csv
from pathlib import Path


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("Usage: combine.py exrates1.csv [<exrates2.csv> ...]\n")
        return 2

    fieldnames = ["Country/Territories", "Currency", "Currency code",
                  "Currency units per £1", "Start Date", "End Date"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()

    err_code = 0
    for p in argv[1:]:
        path = Path(p)
        row = get_usd_row(path)
        if row is None:
            sys.stderr.write(f"Error: no USD entry found in {path}\n")
            err_code = 1
            continue
        if len(row) != len(fieldnames):
            sys.stderr.write(
                f"Error: USD row in {path} has {len(row)} fields, "
                f"expected {len(fieldnames)} ({fieldnames})\n"
            )
            err_code = 1
            continue
        row = dict(zip(fieldnames, row))
        writer.writerow(row)
    return err_code


def get_usd_row(path):
    with open(path, "rb") as f:
        while True:
            raw = f.readline()
            if not raw:  # EOF — USA/USD row not found in this file
                return None
            line = raw.decode("ISO-8859-1").strip()
            if "USA,Dollar,USD" in line:
                return line.split(",")

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
