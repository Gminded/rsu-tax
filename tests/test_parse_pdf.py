"""
Tests for bin/parse_pdf.py.

Full end-to-end parsing of real PDFs is not tested here (that would require
e*trade fixture files).  Instead we test every helper function in isolation
and verify that parse_pdf() raises loud, descriptive errors for all mandatory
fields that are absent or inconsistent.
"""
from pathlib import Path
from unittest.mock import patch

import pytest


# ── _clean_num ────────────────────────────────────────────────────────────────

class TestCleanNum:
    def test_plain_integer(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("800") == pytest.approx(800.0)

    def test_plain_float(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("123.45") == pytest.approx(123.45)

    def test_dollar_sign_stripped(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("$1234.56") == pytest.approx(1234.56)

    def test_comma_thousands_separator(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("$1,234.56") == pytest.approx(1234.56)

    def test_parentheses_mean_negative(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("(100)") == pytest.approx(-100.0)

    def test_parentheses_with_dollar(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("($50.00)") == pytest.approx(-50.0)

    def test_none_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num(None) is None

    def test_empty_string_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("") is None

    def test_whitespace_only_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("   ") is None

    def test_non_numeric_string_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("not_a_number") is None

    def test_leading_trailing_whitespace_tolerated(self, parse_pdf_mod):
        assert parse_pdf_mod._clean_num("  $42.00  ") == pytest.approx(42.0)


# ── _parse_mmddyyyy ───────────────────────────────────────────────────────────

class TestParseMmDdYyyy:
    def test_slash_format(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("03/15/2024") == "2024-03-15"

    def test_dash_format(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("03-15-2024") == "2024-03-15"

    def test_year_boundary(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("12/31/2023") == "2023-12-31"

    def test_january_first(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("01/01/2020") == "2020-01-01"

    def test_wrong_order_yyyymmdd_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("2024-03-15") is None

    def test_dd_mm_yyyy_rejected(self, parse_pdf_mod):
        """Day-first format (15/03/2024) must be rejected — month=15 is invalid."""
        assert parse_pdf_mod._parse_mmddyyyy("15/03/2024") is None

    def test_garbage_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._parse_mmddyyyy("not-a-date") is None


# ── _first_match ──────────────────────────────────────────────────────────────

class TestFirstMatch:
    def test_match_in_first_line(self, parse_pdf_mod):
        assert parse_pdf_mod._first_match(r"(\d{4})", ["year 2024"]) == "2024"

    def test_match_in_later_line(self, parse_pdf_mod):
        lines = ["no digits", "year 2024 here"]
        assert parse_pdf_mod._first_match(r"(\d{4})", lines) == "2024"

    def test_returns_first_occurrence_only(self, parse_pdf_mod):
        lines = ["2020 was first", "2024 came later"]
        assert parse_pdf_mod._first_match(r"(\d{4})", lines) == "2020"

    def test_no_match_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._first_match(r"(\d{4})", ["no digits here"]) is None

    def test_empty_list_returns_none(self, parse_pdf_mod):
        assert parse_pdf_mod._first_match(r"(\d{4})", []) is None

    def test_date_pattern(self, parse_pdf_mod):
        lines = ["Release Date", "03/15/2024\n1000\n800"]
        result = parse_pdf_mod._first_match(r"(\d{2}[-/]\d{2}[-/]\d{4})", lines)
        assert result == "03/15/2024"


# ── _neighbor_block_right ─────────────────────────────────────────────────────

class TestNeighborBlockRight:
    def _box(self, text, x0, x1, y0=100, y1=110):
        return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1}

    def test_finds_neighbour_to_the_right(self, parse_pdf_mod):
        boxes = [
            self._box("Release Date", 0, 80),
            self._box("03/15/2024", 90, 200),
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") == "03/15/2024"

    def test_case_insensitive_label_matching(self, parse_pdf_mod):
        boxes = [
            self._box("release date", 0, 80),
            self._box("03/15/2024", 90, 200),
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") == "03/15/2024"

    def test_partial_label_match(self, parse_pdf_mod):
        """Label search uses substring matching."""
        boxes = [
            self._box("Award Shares Granted", 0, 100),
            self._box("1000", 110, 200),
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Award Shares") == "1000"

    def test_label_not_present_returns_none(self, parse_pdf_mod):
        boxes = [self._box("Something Else", 0, 80), self._box("value", 90, 200)]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") is None

    def test_no_box_to_the_right_returns_none(self, parse_pdf_mod):
        """Value box is to the left of the label — should not be selected."""
        boxes = [
            self._box("Release Date", 200, 300),
            self._box("03/15/2024", 0, 190),
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") is None

    def test_y_tolerance_accepted(self, parse_pdf_mod):
        """Neighbour a few pixels above/below the label is within tolerance."""
        boxes = [
            self._box("Release Date", 0, 80, y0=100, y1=110),
            self._box("03/15/2024", 90, 200, y0=104, y1=114),   # centre offset = 4px < 8px tol
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") == "03/15/2024"

    def test_y_tolerance_exceeded_ignored(self, parse_pdf_mod):
        """Neighbour too far away on the y-axis is ignored."""
        boxes = [
            self._box("Release Date", 0, 80, y0=100, y1=110),
            self._box("wrong row", 90, 200, y0=200, y1=210),    # centre 60px below
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") is None

    def test_picks_leftmost_right_neighbour(self, parse_pdf_mod):
        """When several boxes are to the right, the leftmost is returned."""
        boxes = [
            self._box("Release Date", 0, 80),
            self._box("far",   250, 350),
            self._box("close", 90,  200),
        ]
        assert parse_pdf_mod._neighbor_block_right(boxes, "Release Date") == "close"


# ── parse_pdf error reporting ─────────────────────────────────────────────────

def _lv_boxes(label, value, y=100):
    """A label box and a neighbouring value box."""
    return [
        {"text": label, "x0": 0,  "y0": y,     "x1": 80,  "y1": y + 10},
        {"text": value, "x0": 90, "y0": y + 1,  "x1": 250, "y1": y + 9},
    ]

def _full_boxes():
    """Minimal boxes sufficient for a successful parse."""
    return (
        _lv_boxes("Release Date", "03/15/2024", y=100)
        + _lv_boxes("Market Value Per Share", "$100.00", y=130)
        + _lv_boxes("Award Shares", "1000\n(200)\n800", y=160)
    )


class TestParsePdfErrors:
    def _patch(self, parse_pdf_mod, boxes):
        return patch.object(parse_pdf_mod, "_collect_boxes", return_value=boxes)

    def test_missing_release_date_raises_with_clear_message(self, parse_pdf_mod):
        boxes = _full_boxes()
        # Remove the Release Date label so date_out stays None
        boxes = [b for b in boxes if "Release Date" not in b["text"]]
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="Release Date"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_unparseable_date_raises(self, parse_pdf_mod):
        """A date block present but with no valid MM/DD/YYYY pattern raises."""
        boxes = (
            _lv_boxes("Release Date", "not-a-date", y=100)
            + _lv_boxes("Market Value Per Share", "$100.00", y=130)
            + _lv_boxes("Award Shares", "1000\n(200)\n800", y=160)
        )
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="Release Date"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_missing_market_value_raises_with_clear_message(self, parse_pdf_mod):
        boxes = [b for b in _full_boxes() if "Market Value" not in b["text"]]
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="Market Value Per Share"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_missing_share_distribution_raises_for_grant(self, parse_pdf_mod):
        """No Award Shares block → grant is None → descriptive error."""
        boxes = (
            _lv_boxes("Release Date", "03/15/2024", y=100)
            + _lv_boxes("Market Value Per Share", "$100.00", y=130)
            # No Award Shares / Shares Traded / Shares Sold boxes
        )
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="granted"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_missing_withheld_line_raises(self, parse_pdf_mod):
        """Distribution block with only one line → withheld is None."""
        boxes = (
            _lv_boxes("Release Date", "03/15/2024", y=100)
            + _lv_boxes("Market Value Per Share", "$100.00", y=130)
            + _lv_boxes("Award Shares", "1000", y=160)   # only granted, no withheld line
        )
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="withheld"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_missing_issued_line_raises(self, parse_pdf_mod):
        """Distribution block with two lines → issued is None."""
        boxes = (
            _lv_boxes("Release Date", "03/15/2024", y=100)
            + _lv_boxes("Market Value Per Share", "$100.00", y=130)
            + _lv_boxes("Award Shares", "1000\n(200)", y=160)   # no issued line
        )
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="issued"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_integrity_check_failure_raises_with_clear_message(self, parse_pdf_mod):
        """Issued ≠ Granted − Withheld → data-integrity error."""
        boxes = (
            _lv_boxes("Release Date", "03/15/2024", y=100)
            + _lv_boxes("Market Value Per Share", "$100.00", y=130)
            + _lv_boxes("Award Shares", "1000\n(150)\n800", y=160)  # 1000-150=850 ≠ 800
        )
        with self._patch(parse_pdf_mod, boxes):
            with pytest.raises(ValueError, match="[Ii]ntegrity|[Ii]ssued"):
                parse_pdf_mod.parse_pdf(Path("dummy.pdf"))

    def test_valid_boxes_parse_without_error(self, parse_pdf_mod):
        """Smoke test: _full_boxes() should parse cleanly."""
        with self._patch(parse_pdf_mod, _full_boxes()):
            result = parse_pdf_mod.parse_pdf(Path("dummy.pdf"))
        assert result["Release Date"] == "2024-03-15"
        assert result["Granted"] == 1000
        assert result["Sold"] == 200
        assert result["Issued"] == 800
        assert result["Price per share ($)"] == pytest.approx(100.00)


# ── parse-stock-releases error accumulation ───────────────────────────────────

class TestParseStockReleases:
    def test_nonexistent_file_reported_loudly(self, parse_releases, capsys):
        rc = parse_releases.main(["prog", "/nonexistent/file.pdf"])
        _, err = capsys.readouterr()
        assert rc == 1
        assert "/nonexistent/file.pdf" in err
        assert "ERROR" in err

    def test_all_failures_reported_before_abort(self, parse_releases, capsys):
        """Processing continues past the first failure so all bad files are listed."""
        rc = parse_releases.main(["prog", "/bad1.pdf", "/bad2.pdf"])
        _, err = capsys.readouterr()
        assert rc == 1
        assert "/bad1.pdf" in err
        assert "/bad2.pdf" in err

    def test_output_sorted_by_release_date(self, parse_releases, capsys):
        """Rows must be sorted by Release Date ascending regardless of input order."""
        rows_by_date = [
            {"Release Date": "2020-06-01", "Granted": 500, "Sold": 0,   "Issued": 500, "Price per share ($)": 12.0},
            {"Release Date": "2020-01-01", "Granted": 100, "Sold": 10,  "Issued": 90,  "Price per share ($)": 10.0},
            {"Release Date": "2021-03-01", "Granted": 200, "Sold": 20,  "Issued": 180, "Price per share ($)": 15.0},
        ]
        # Feed them out of order (June, Jan, March)
        call_order = [rows_by_date[0], rows_by_date[1], rows_by_date[2]]

        import csv, io
        # parse-stock-releases does `from parse_pdf import parse_pdf`, which binds the
        # function into its own namespace — so we must patch it there, not on parse_pdf.
        with patch.object(parse_releases, "parse_pdf", side_effect=call_order):
            rc = parse_releases.main(["prog", "a.pdf", "b.pdf", "c.pdf"])
        assert rc == 0
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))
        dates = [r["Release Date"] for r in rows]
        assert dates == sorted(dates)
