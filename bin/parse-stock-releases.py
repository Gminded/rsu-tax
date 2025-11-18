#!/usr/bin/env python3
import re
import sys
import csv
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Dict, List, Tuple

from parse_pdf import parse_pdf
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextBox, LTTextLine, LAParams

def main(argv: List[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: parse_stock_release_pdfs.py <file1.pdf> [<file2.pdf> ...]\n")
        return 2

    fieldnames = ["Release Date", "Granted", "Sold", "Issued", "Price per share ($)"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()

    rows = []

    for p in argv[1:]:
        path = Path(p)
        row = parse_pdf(Path(p))
        # Filter out Award fields (and anything else not listed)
        rows.append({k: row.get(k) for k in fieldnames})
    rows.sort(key=(lambda x: date.fromisoformat(x["Release Date"])))

    for row in rows:
        writer.writerow(row)
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
