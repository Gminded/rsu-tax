import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

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
        print("Error in _clean_num()", file=sys.stderr)
        return None

def _parse_mmddyyyy(s: str) -> Optional[str]:
    for fmt in ("%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None

def parse_pdf(pdf_path: Path) -> Dict[str, Optional[str]]:
    boxes = _collect_boxes(pdf_path)

    # Release Date block (the cell to the right contains date, shares released, etc.)
    block = _neighbor_block_right(boxes, "Release Date")
    date_out = None
    if block:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        date_raw = _first_match(r"(\d{2}[-/]\d{2}[-/]\d{4})", lines)
        if date_raw:
            date_out = _parse_mmddyyyy(date_raw)

    # Market Value Per Share (first line in the same 3–5 line block starting with "$")
    block_mv = _neighbor_block_right(boxes, "Market Value Per Share") or block or ""
    lines_mv = [ln for ln in block_mv.splitlines() if ln.strip()]
    # heuristic: first line starting with $ is the market value per share
    mv_raw = next((ln.strip() for ln in lines_mv if ln.strip().startswith("$")), None)
    mv = _clean_num(mv_raw)

    # Stock distribution: Award Shares / (Shares Traded|Sold) / Shares Issued
    block_dist = (_neighbor_block_right(boxes, "Award Shares") or
                  _neighbor_block_right(boxes, "Shares Traded") or
                  _neighbor_block_right(boxes, "Shares Sold") or "")
    lines_sd = [ln for ln in block_dist.splitlines() if ln.strip()]
    grant = _clean_num(lines_sd[0]) if len(lines_sd) >= 1 else None
    withheld = _clean_num(lines_sd[1]) if len(lines_sd) >= 2 else None
    issued = _clean_num(lines_sd[2]) if len(lines_sd) >= 3 else None

    # NEW: Award Date
    award_date_block = _neighbor_block_right(boxes, "Award Date") or ""
    award_date_lines = [ln for ln in award_date_block.splitlines() if ln.strip()]
    award_date_raw = _first_match(r"(\d{2}[-/]\d{2}[-/]\d{4})", award_date_lines)
    award_date_out = _parse_mmddyyyy(award_date_raw) if award_date_raw else None

    # NEW: Award Number (typ. a numeric or alphanumeric code)
    award_num_block = _neighbor_block_right(boxes, "Award Number") or ""
    award_num_lines = [ln for ln in award_num_block.splitlines() if ln.strip()]
    award_number = None
    if award_num_lines:
        # take the first token on the fourth line; adjust if your PDFs show a different layout
        for line in award_num_lines:
            value = re.findall(r"\bR\d+", line)
            if value:
                award_number = value[0]
                break

    return {
        "Release Date": date_out,
        "Granted": int(round(grant)) if grant is not None else None,
        "Withheld": abs(int(round(withheld))) if withheld is not None else None,
        "Issued": int(round(issued)) if issued is not None else None,
        "Price per share ($)": round(mv, 2) if mv is not None else None,
        "Award Date": award_date_out,
        "Award Number": award_number,
    }
