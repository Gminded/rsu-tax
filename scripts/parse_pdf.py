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

import re
import sys
import warnings
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

def _neighbor_block_right_of(boxes, box, y_tol=8) -> Optional[str]:
    """Text of the leftmost box sitting to the right of `box`, on the same row
    (vertical centres within `y_tol`).  Shared by the label-search variant below
    and by callers that already hold the label box (e.g. the Cash Distribution
    column, whose label and value blocks are each multi-line)."""
    y_center = (box["y0"] + box["y1"]) / 2
    right = [r for r in boxes if r["x0"] > box["x1"] and abs(((r["y0"]+r["y1"])/2) - y_center) < y_tol]
    right = sorted(right, key=lambda r: r["x0"])
    return right[0]["text"] if right else None

def _neighbor_block_right(boxes, label, y_tol=8) -> Optional[str]:
    cands = [b for b in boxes if label.lower() in b["text"].lower()]
    for b in cands:
        block = _neighbor_block_right_of(boxes, b, y_tol)
        if block is not None:
            return block
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
    original = s
    s = s.strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    # An empty or whitespace-only cell is "absent", not malformed: return None so
    # the caller can raise a clear, field-specific error if the value is required.
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"Cannot parse numeric value from {original!r}")
    return -v if neg else v

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

    # Per-share dollar amounts share one cell, listed in column order:
    #   [0] Market Value Per Share, [1] Award Price Per Share, [2] Sale Price Per Share
    # The Sale Price line is present only when shares were actually sold on the
    # market ("Shares Sold"); net-settled releases ("Shares Traded") omit it, in
    # which case the withheld shares are settled at market value (no gain).
    block_mv = _neighbor_block_right(boxes, "Market Value Per Share") or block or ""
    lines_mv = [ln for ln in block_mv.splitlines() if ln.strip()]
    dollar_lines = [ln.strip() for ln in lines_mv if ln.strip().startswith("$")]
    mv   = _clean_num(dollar_lines[0]) if len(dollar_lines) >= 1 else None
    sale = _clean_num(dollar_lines[2]) if len(dollar_lines) >= 3 else None

    # Release Date — required
    if date_out is None:
        raise ValueError(
            "Could not extract 'Release Date' from PDF. "
            "Expected a date next to a 'Release Date' label in MM/DD/YYYY or MM-DD-YYYY format."
        )

    # Market value per share — required
    if mv is None:
        raise ValueError(
            "Could not extract 'Market Value Per Share' from PDF. "
            "Expected a '$...' value next to a 'Market Value Per Share' label."
        )

    # Sanity check (warning, not error): the broker's sale price for withheld
    # shares is realised on the release date and should be close to the
    # release-date market value.  A figure far outside that band usually means a
    # mis-parsed column rather than a genuine price move, so warn and let the
    # caller decide — we do NOT fail, because intraday moves can legitimately be
    # large.
    if sale is not None and mv > 0 and not (0.5 * mv <= sale <= 2.0 * mv):
        warnings.warn(
            f"Sale price per share (${sale:.2f}) differs markedly from market "
            f"value per share (${mv:.2f}) in {pdf_path.name}; verify the PDF "
            f"parsed correctly.",
            stacklevel=2,
        )

    # Stock distribution: Award Shares / (Shares Traded|Sold) / Shares Issued
    block_dist = (_neighbor_block_right(boxes, "Award Shares") or
                  _neighbor_block_right(boxes, "Shares Traded") or
                  _neighbor_block_right(boxes, "Shares Sold") or "")
    lines_sd = [ln for ln in block_dist.splitlines() if ln.strip()]
    grant = _clean_num(lines_sd[0]) if len(lines_sd) >= 1 else None
    withheld = _clean_num(lines_sd[1]) if len(lines_sd) >= 2 else None
    issued = _clean_num(lines_sd[2]) if len(lines_sd) >= 3 else None

    if grant is None:
        raise ValueError(
            "Could not extract granted share count from PDF. "
            "Expected numeric values next to 'Award Shares', 'Shares Traded', or 'Shares Sold' label."
        )
    if withheld is None:
        raise ValueError(
            f"Could not extract withheld share count from PDF (granted={grant}). "
            "Expected a second numeric value in the share-distribution block."
        )
    withheld = abs(int(round(withheld)))
    if issued is None:
        raise ValueError(
            f"Could not extract issued share count from PDF (granted={grant}, withheld={withheld}). "
            "Expected a third numeric value in the share-distribution block."
        )

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

    # Fee — incidental cost of disposal, allowable under TCGA 1992 s.38(1)(c).
    # It appears only on Sell-to-cover releases, in the "Cash Distribution"
    # section, where the withheld shares were sold on the open market.  Labels
    # and amounts sit in two vertically-aligned column blocks, e.g.
    #     Total Sale Price        $4,023.46
    #     Total Tax              ($3,922.65)
    #     Fee                       ($21.79)
    #     Total Due Participant      $79.02
    # Pair the two blocks line-by-line and read the row whose label is "Fee", so
    # the value is found by its label rather than a fixed offset.  Absent (a
    # net-settled "Shares Traded" release) leaves the fee None.
    fee = None
    for b in boxes:
        label_lines = [ln.strip() for ln in b["text"].splitlines() if ln.strip()]
        if "Fee" not in label_lines:
            continue
        value_block = _neighbor_block_right_of(boxes, b)
        if not value_block:
            continue
        value_lines = [ln.strip() for ln in value_block.splitlines() if ln.strip()]
        if len(value_lines) != len(label_lines):
            continue
        fee = _clean_num(value_lines[label_lines.index("Fee")])
        break
    # The PDF shows the fee as a negative cash movement, e.g. ($21.79); carry it
    # as a positive cost magnitude so callers can subtract it from the gain.
    if fee is not None:
        fee = abs(fee)

    # Validate Issued == Granted - Withheld
    if int(round(issued)) != int(round(grant)) - withheld:
        raise ValueError(
            f"Data integrity check failed: Issued ({issued}) != Granted ({grant}) - Withheld ({withheld}). "
            "The PDF may use an unexpected layout or the share counts are inconsistent."
        )

    return {
        "Release Date": date_out,
        "Granted": int(round(grant)) if grant is not None else None,
        "Sold": withheld if withheld is not None else None,
        "Issued": int(round(issued)) if issued is not None else None,
        "Price per share ($)": round(mv, 2) if mv is not None else None,
        "Sale price per share ($)": round(sale, 2) if sale is not None else None,
        "Fee ($)": round(fee, 2) if fee is not None else None,
        "Award Date": award_date_out,
        "Award Number": award_number,
    }
