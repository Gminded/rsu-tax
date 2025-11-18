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

import sys
import csv
from pathlib import Path
from datetime import date
from typing import List

from parse_pdf import parse_pdf

def main(argv: List[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: parse_stock_release_pdfs.py <file1.pdf> [<file2.pdf> ...]\n")
        return 2

    fieldnames = ["Release Date", "Granted", "Sold", "Issued", "Price per share ($)"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()

    rows = []

    for p in argv[1:]:
        row = parse_pdf(Path(p))
        # Filter out Award fields (and anything else not listed)
        rows.append({k: row.get(k) for k in fieldnames})
    rows.sort(key=(lambda x: date.fromisoformat(x["Release Date"])))

    for row in rows:
        writer.writerow(row)
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
