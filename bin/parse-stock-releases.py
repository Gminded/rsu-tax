#!/usr/bin/env python3
import re
import sys
import csv
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Dict, List, Tuple

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextBox, LTTextLine, LAParams

def _collect_boxes(pdf_path: Path):
    laparams = LAParams(line_margin=0.2, char_margin=2.0, word_margin=0.1, boxes_flow=None)
    boxes = []
    for page_layout in extract_pages(str(pdf_path), laparams=laparams):
        for element in page_layout:
            if isinstance(element, (LTTextContainer, LTTextBox, LTTextLine)):
                text = element.get_text().strip()
                if text:
                    x0, y0, x1, y1 = element.bbox
                    boxes.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return boxes

def _neighbor_block_right(boxes, label, y_tol=8) -> Optional[str]:
    cands = [b for b in boxes if label.lower() in b["text"].lower()]
    for b in cands:
        y_center = (b["y0"] + b["y1"]) / 2
        right = [r for r in boxes if r["x0"] > b["x1"] and abs(((r["y0"]+r["y1"])/2) - y_center) < y_tol]
        right = sorted(right, key=lambda r: r["x0"])
        if right:
            return right[0]["text"]
    return None

def _first_match(pattern, lines: List[str]) -> Optional[str]:
    rx = re.compile(pattern)
    for line in lines:
        m = rx.search(line.strip())
        if m:
            return m.group(1)
    return None

def _clean_num(s: Optional[str]) -> Optional[float]:
    if not s: return None
    s = s.strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None

def parse_pdf(pdf_path: Path) -> Dict[str, Optional[str]]:
    boxes = _collect_boxes(pdf_path)

    # Release Date block
    block = _neighbor_block_right(boxes, "Release Date")
    date_out = None
    if block:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        date_raw = _first_match(r"(\d{2}-\d{2}-\d{4})", lines)
        if date_raw:
            try:
                from datetime import datetime
                date_out = datetime.strptime(date_raw, "%m-%d-%Y").strftime("%Y-%m-%d")
            except Exception:
                pass

    # Market Value Per Share block (same block can be reused, but safe to look it up)
    block_mv = _neighbor_block_right(boxes, "Market Value Per Share") or block or ""
    lines_mv = [ln for ln in block_mv.splitlines() if ln.strip()]
    # heuristic: first line starting with $ is the market value per share
    mv_raw = next((ln.strip() for ln in lines_mv if ln.strip().startswith("$")), None)
    mv = _clean_num(mv_raw)

    # Award Shares / Shares Traded/Sold / Shares Issued block
    block_dist = (_neighbor_block_right(boxes, "Award Shares") or
                  _neighbor_block_right(boxes, "Shares Traded") or
                  _neighbor_block_right(boxes, "Shares Sold") or "")
    lines_sd = [ln for ln in block_dist.splitlines() if ln.strip()]
    grant = _clean_num(lines_sd[0]) if len(lines_sd) >= 1 else None
    withheld = _clean_num(lines_sd[1]) if len(lines_sd) >= 2 else None
    issued = _clean_num(lines_sd[2]) if len(lines_sd) >= 3 else None

    return {
        "Date": date_out,
        "Granted": int(round(grant)) if grant is not None else None,
        "Withheld": abs(int(round(withheld))) if withheld is not None else None,
        "Issued": int(round(issued)) if issued is not None else None,
        "Price per share ($)": round(mv, 2) if mv is not None else None,
    }

def main(argv: List[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("Usage: parse_stock_release_pdfs.py <file1.pdf> [<file2.pdf> ...]\n")
        return 2

    fieldnames = ["Date", "Granted", "Withheld", "Issued", "Price per share ($)"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()

    all_rows = []

    for p in argv[1:]:
        path = Path(p)
        all_rows.append(parse_pdf(path))
    all_rows.sort(key=(lambda x: date.fromisoformat(x["Date"])))

    for row in all_rows:
        writer.writerow(row)
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
