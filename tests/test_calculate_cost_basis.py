"""
Tests for scripts/calculate-cost-basis.py.

Covers:
- Section 104 pool arithmetic
- Same-day rule (HMRC rule 1)
- 30-day / bed-and-breakfasting rule (HMRC rule 2)
- Rule priority ordering
- WithholdingSell row injection and zero-gain behaviour
- Matching-rule notes in output
- Tax-year label helper
- Exchange-rate lookup (attach_rate)
- Sales CSV flexible header detection (load_sales)
- Error cases (sell exceeds pool, etc.)
- Full-pipeline integration via main()
"""
import csv
import io
import sys
from datetime import datetime

import pandas as pd
import pytest


# ── event-DataFrame factory ───────────────────────────────────────────────────

@pytest.fixture
def mk(ccb):
    """
    Return a factory that builds an events DataFrame from compact tuples.

        mk([(type, 'YYYY-MM-DD', shares, price_gbp), ...])

    GBP/USD is set to 1.0 so that USD price == GBP price, keeping the maths
    straightforward.  All shares for a Buy go into ISSUED_LABEL; all shares
    for a Sell/WithholdingSell go into SOLD_LABEL.
    """
    def _make(rows):
        records = []
        for typ, date_str, shares, price in rows:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            rec = {
                ccb.TYPE_LABEL:                typ,
                ccb.DATE_LABEL:                date_str,
                ccb.DATE_DT:                   dt,
                ccb.GRANTED_LABEL:             float(shares) if typ == ccb.BUY_TYPE else 0.0,
                ccb.SOLD_LABEL:                0.0          if typ == ccb.BUY_TYPE else float(shares),
                ccb.ISSUED_LABEL:              float(shares) if typ == ccb.BUY_TYPE else 0.0,
                ccb.PRICE_PER_SHARE_USD_LABEL: float(price),
                ccb.GBP_USD_LABEL:             1.0,
                ccb.PRICE_PER_SHARE_GBP_LABEL: float(price),
            }
            records.append(rec)
        return pd.DataFrame(records)
    return _make


# ── Section 104 pool ──────────────────────────────────────────────────────────

class TestSection104Pool:
    def test_simple_buy_then_sell(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 15.0),
        ])
        gains, holdings, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[0] == pytest.approx(0.0)
        assert gains[1] == pytest.approx(50 * 15.0 - 50 * 10.0)   # 250
        assert holdings[0] == pytest.approx(100 * 10.0)
        assert holdings[1] == pytest.approx(50 * 10.0)

    def test_weighted_average_across_two_buys(self, ccb, mk):
        # 100 sh @ £10 + 100 sh @ £20 → pool avg £15
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.BUY_TYPE,  "2020-03-01", 100, 20.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 18.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        # pool cost = 3000; sell 100 of 200 → allowable = 1500; proceeds = 1800
        assert gains[2] == pytest.approx(1800.0 - 1500.0)

    def test_pool_cost_reduces_correctly_after_sell(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 200, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  75, 12.0),
        ])
        _, holdings, _, _ = ccb.get_gains_and_holdings(events)
        assert holdings[1] == pytest.approx(125 * 10.0)

    def test_sequential_sells_deplete_pool(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 300, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 12.0),
            (ccb.SELL_TYPE, "2020-09-01", 100, 14.0),
            (ccb.SELL_TYPE, "2020-12-01", 100, 16.0),
        ])
        gains, holdings, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(100 * (12 - 10))
        assert gains[2] == pytest.approx(100 * (14 - 10))
        assert gains[3] == pytest.approx(100 * (16 - 10))
        assert holdings[3] == pytest.approx(0.0)

    def test_sell_exactly_pool_quantity_allowed(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 12.0),
        ])
        gains, holdings, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(100 * (12 - 10))
        assert holdings[1] == pytest.approx(0.0)

    def test_sell_exceeds_pool_raises_value_error(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 50, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 60, 12.0),
        ])
        with pytest.raises(ValueError, match="pool|held|available"):
            ccb.get_gains_and_holdings(events)

    def test_sell_with_empty_pool_raises(self, ccb, mk):
        events = mk([
            (ccb.SELL_TYPE, "2020-06-01", 10, 12.0),
        ])
        with pytest.raises(ValueError):
            ccb.get_gains_and_holdings(events)


# ── Same-day rule ─────────────────────────────────────────────────────────────

class TestSameDayRule:
    def test_same_day_buy_matched_before_pool(self, ccb, mk):
        """Shares acquired on the same day as the disposal are matched first."""
        # Pool has 100 sh @ £10; same-day buy of 50 sh @ £20; sell 50 sh @ £25
        # Without same-day rule: pool avg = (1000+1000)/150 = £13.33, gain = 50*25-50*13.33
        # With same-day rule:   gain = 50*25 - 50*20 = 250
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.BUY_TYPE,  "2020-06-01",  50, 20.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 25.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[2] == pytest.approx(50 * 25 - 50 * 20)   # 250

    def test_same_day_partial_match_rest_from_pool(self, ccb, mk):
        """Sell 150: 50 from same-day buy, 100 from pool."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 200, 10.0),
            (ccb.BUY_TYPE,  "2020-06-01",  50, 20.0),
            (ccb.SELL_TYPE, "2020-06-01", 150, 15.0),
        ])
        gains, holdings, _, _ = ccb.get_gains_and_holdings(events)
        # same-day: 50 @ 20 → cost 1000; pool: 100 @ 10 → cost 1000; total cost = 2000
        expected_gain = 150 * 15 - (50 * 20 + 100 * 10)
        assert gains[2] == pytest.approx(expected_gain)
        # Remaining pool: 100 sh @ 10 each
        assert holdings[2] == pytest.approx(100 * 10.0)

    def test_same_day_buy_exhausted_excess_from_pool(self, ccb, mk):
        """Same-day buy is fully consumed; remainder pulls from pool."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 8.0),
            (ccb.BUY_TYPE,  "2020-06-01",  30, 12.0),
            (ccb.SELL_TYPE, "2020-06-01",  80, 14.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        # 30 same-day @ 12, 50 from pool @ 8
        expected_gain = 80 * 14 - (30 * 12 + 50 * 8)
        assert gains[2] == pytest.approx(expected_gain)

    def test_same_day_buy_note_in_output(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.BUY_TYPE,  "2020-06-01",  50, 20.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 25.0),
        ])
        _, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert "same-day" in notes[2]

    def test_multiple_same_day_buys_consumed_fifo(self, ccb, mk):
        """When two buys land on the same day as a sell, both are matched (FIFO by index)."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-06-01", 30, 10.0),
            (ccb.BUY_TYPE,  "2020-06-01", 40, 15.0),
            (ccb.SELL_TYPE, "2020-06-01", 50, 20.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        # 30 @ 10 + 20 @ 15 = 300 + 300 = 600; proceeds = 50*20 = 1000
        assert gains[2] == pytest.approx(1000 - (30 * 10 + 20 * 15))


# ── 30-day (bed-and-breakfasting) rule ───────────────────────────────────────

class TestThirtyDayRule:
    def test_buy_within_30_days_matched(self, ccb, mk):
        """Classic B&B: sell then buy within 30 days — 30-day rule must apply."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 12.0),
            (ccb.BUY_TYPE,  "2020-06-20",  50, 15.0),   # 19 days after sell
        ])
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        # Without 30-day: gain = 50*12 - 50*10 = 100
        # With 30-day:    gain = 50*12 - 50*15 = -150
        assert gains[1] == pytest.approx(50 * 12 - 50 * 15)
        assert "30-day" in notes[1]

    def test_buy_on_day_30_is_matched(self, ccb, mk):
        """Boundary: buy on exactly day 30 after the sell is within the window."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 12.0),
            (ccb.BUY_TYPE,  "2020-07-01",  50, 15.0),   # exactly 30 days
        ])
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(50 * 12 - 50 * 15)
        assert "30-day" in notes[1]

    def test_buy_on_day_31_not_matched(self, ccb, mk):
        """Buy on day 31 falls outside the window — pool is used instead."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 12.0),
            (ccb.BUY_TYPE,  "2020-07-02",  50, 15.0),   # 31 days after sell
        ])
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(50 * 12 - 50 * 10)   # pool cost
        assert "30-day" not in notes[1]

    def test_30_day_partial_match_rest_from_pool(self, ccb, mk):
        """Sell 100: 60 matched by 30-day rule, 40 from pool."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 12.0),
            (ccb.BUY_TYPE,  "2020-06-15",  60, 14.0),
        ])
        gains, holdings, notes, _ = ccb.get_gains_and_holdings(events)
        expected_gain = 100 * 12 - (60 * 14 + 40 * 10)
        assert gains[1] == pytest.approx(expected_gain)
        assert "30-day" in notes[1]
        assert "Section 104" in notes[1]
        # Pool after: 100 - 40 = 60 sh remain (60 bought via 30-day never entered pool)
        assert holdings[1] == pytest.approx(60 * 10.0)

    def test_30_day_multiple_buys_fifo(self, ccb, mk):
        """Multiple buys in the 30-day window are consumed FIFO (earlier date first)."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 12.0),
            (ccb.BUY_TYPE,  "2020-06-10",  60, 14.0),   # first in window
            (ccb.BUY_TYPE,  "2020-06-20",  50, 11.0),   # second in window
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        # FIFO: 60 @ £14 (first buy), then 40 of 50 @ £11 (second buy); nothing from pool
        expected_gain = 100 * 12 - (60 * 14 + 40 * 11)
        assert gains[1] == pytest.approx(expected_gain)

    def test_30_day_note_includes_acquisition_date(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 12.0),
            (ccb.BUY_TYPE,  "2020-06-15",  50, 14.0),
        ])
        _, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert "2020-06-15" in notes[1]

    def test_pool_unchanged_for_shares_consumed_by_30_day_rule(self, ccb, mk):
        """Shares matched by the 30-day rule never enter the Section 104 pool."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01", 100, 12.0),
            (ccb.BUY_TYPE,  "2020-06-15", 100, 14.0),   # fully consumed by 30-day rule
        ])
        _, holdings, _, _ = ccb.get_gains_and_holdings(events)
        # The sell is matched against the 30-day buy, not the pool — so the pool (100 sh
        # @ £10 = £1 000) is left intact after the sell.
        assert holdings[1] == pytest.approx(1000.0)
        # The Jun-15 buy was fully consumed by the 30-day rule; none of it enters the pool.
        assert holdings[2] == pytest.approx(1000.0)


# ── Rule priority ─────────────────────────────────────────────────────────────

class TestRulePriority:
    def test_same_day_takes_priority_over_30_day(self, ccb, mk):
        """Same-day shares must be consumed before any 30-day shares."""
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.BUY_TYPE,  "2020-06-01",  30, 20.0),   # same-day buy
            (ccb.SELL_TYPE, "2020-06-01",  50, 25.0),
            (ccb.BUY_TYPE,  "2020-06-15",  50, 18.0),   # 30-day buy
        ])
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        # 30 same-day @ 20, then 20 from 30-day @ 18; remaining 0 from pool
        expected_gain = 50 * 25 - (30 * 20 + 20 * 18)
        assert gains[2] == pytest.approx(expected_gain)
        assert "same-day" in notes[2]
        assert "30-day" in notes[2]

    def test_30_day_takes_priority_over_pool(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 100, 10.0),
            (ccb.SELL_TYPE, "2020-06-01",  50, 12.0),
            (ccb.BUY_TYPE,  "2020-06-15",  50, 14.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        # 30-day: 50 @ 14; NOT from pool @ 10
        assert gains[1] == pytest.approx(50 * 12 - 50 * 14)


# ── WithholdingSell rows ──────────────────────────────────────────────────────

class TestWithholdingSell:
    def test_withholding_sell_gain_is_zero(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,             "2020-01-01", 800, 10.0),
            (ccb.WITHHOLDING_SELL_TYPE, "2020-01-01", 200, 10.0),
        ])
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(0.0)

    def test_withholding_sell_does_not_deplete_pool(self, ccb, mk):
        """The pool should contain only the Issued (Buy) shares, not the withheld ones."""
        events = mk([
            (ccb.BUY_TYPE,             "2020-01-01", 800, 10.0),
            (ccb.WITHHOLDING_SELL_TYPE, "2020-01-01", 200, 10.0),
            (ccb.SELL_TYPE,            "2020-06-01", 800, 12.0),
        ])
        gains, holdings, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[2] == pytest.approx(800 * (12 - 10))
        assert holdings[2] == pytest.approx(0.0)

    def test_withholding_sell_note(self, ccb, mk):
        events = mk([
            (ccb.BUY_TYPE,             "2020-01-01", 800, 10.0),
            (ccb.WITHHOLDING_SELL_TYPE, "2020-01-01", 200, 10.0),
        ])
        _, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert "withholding" in notes[1].lower()

    def _ws_events(self, ccb, mv_gbp, sale_gbp, units=200):
        """Buy 800 @ mv plus a same-day WithholdingSell of `units` at `sale_gbp`."""
        return pd.DataFrame([
            {ccb.TYPE_LABEL: ccb.BUY_TYPE, ccb.DATE_LABEL: "2020-01-01",
             ccb.DATE_DT: datetime(2020, 1, 1), ccb.GRANTED_LABEL: 800.0,
             ccb.SOLD_LABEL: 0.0, ccb.ISSUED_LABEL: 800.0,
             ccb.PRICE_PER_SHARE_GBP_LABEL: mv_gbp,
             ccb.SALE_PRICE_PER_SHARE_GBP_LABEL: float("nan")},
            {ccb.TYPE_LABEL: ccb.WITHHOLDING_SELL_TYPE, ccb.DATE_LABEL: "2020-01-01",
             ccb.DATE_DT: datetime(2020, 1, 1), ccb.GRANTED_LABEL: 0.0,
             ccb.SOLD_LABEL: float(units), ccb.ISSUED_LABEL: 0.0,
             ccb.PRICE_PER_SHARE_GBP_LABEL: mv_gbp,
             ccb.SALE_PRICE_PER_SHARE_GBP_LABEL: sale_gbp},
        ])

    def test_withholding_sell_taxable_when_sale_price_below_market_value(self, ccb):
        """Sold to cover at a price below release-date market value → allowable loss."""
        events = self._ws_events(ccb, mv_gbp=10.0, sale_gbp=9.5, units=200)
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(200 * (9.5 - 10.0))   # -100
        assert "vs MV" in notes[1]

    def test_withholding_sell_taxable_when_sale_price_above_market_value(self, ccb):
        """Sold to cover at a price above release-date market value → chargeable gain."""
        events = self._ws_events(ccb, mv_gbp=10.0, sale_gbp=10.25, units=200)
        gains, _, _, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(200 * (10.25 - 10.0))   # +50

    def test_withholding_sell_zero_gain_when_sale_price_equals_market_value(self, ccb):
        events = self._ws_events(ccb, mv_gbp=10.0, sale_gbp=10.0, units=200)
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(0.0)
        assert "vs MV" not in notes[1]

    def test_withholding_sell_does_not_deplete_pool_even_with_sale_price(self, ccb):
        events = self._ws_events(ccb, mv_gbp=10.0, sale_gbp=9.0, units=200)
        _, holdings, _, _ = ccb.get_gains_and_holdings(events)
        # Pool holds only the 800 issued shares; the 200 withheld never enter it.
        assert holdings[1] == pytest.approx(800 * 10.0)

    def test_main_injects_withholding_sell_rows(self, ccb, tmp_path, capsys):
        """A release with Sold > 0 should produce a WithholdingSell row in the output."""
        xr = tmp_path / "xr.csv"
        xr.write_text(
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = tmp_path / "rel.csv"
        rel.write_text(
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-06-01,1000,200,800,10.00\n"
        )
        ccb.main(["prog", "-r", str(rel), "-x", str(xr)])
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))
        types = [r["Type"] for r in rows]
        assert "WithholdingSell" in types
        ws = next(r for r in rows if r["Type"] == "WithholdingSell")
        assert float(ws["Gains / Losses (GBP)"]) == pytest.approx(0.0)
        assert ws["Sold"] == "200"

    def test_no_withholding_sell_when_sold_is_zero(self, ccb, tmp_path, capsys):
        xr = tmp_path / "xr.csv"
        xr.write_text(
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = tmp_path / "rel.csv"
        rel.write_text(
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-06-01,1000,0,1000,10.00\n"
        )
        ccb.main(["prog", "-r", str(rel), "-x", str(xr)])
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))
        assert not any(r["Type"] == "WithholdingSell" for r in rows)


# ── Tax-year label ────────────────────────────────────────────────────────────

class TestTaxYearLabel:
    def _label(self, ccb, y, m, d):
        return ccb._tax_year_label(datetime(y, m, d))

    def test_january_is_in_previous_start_year(self, ccb):
        assert self._label(ccb, 2024, 1, 1) == "2023/24"

    def test_april_5_still_in_old_tax_year(self, ccb):
        assert self._label(ccb, 2024, 4, 5) == "2023/24"

    def test_april_6_starts_new_tax_year(self, ccb):
        assert self._label(ccb, 2024, 4, 6) == "2024/25"

    def test_december_is_in_current_start_year(self, ccb):
        assert self._label(ccb, 2024, 12, 31) == "2024/25"

    def test_two_digit_year_formatted_correctly(self, ccb):
        # 2024/25 not 2024/2025
        label = self._label(ccb, 2024, 6, 1)
        assert label == "2024/25"

    def test_century_boundary(self, ccb):
        assert self._label(ccb, 2099, 4, 6) == "2099/00"


# ── parse_pdf sale-price extraction ───────────────────────────────────────────

class TestParsePdfSalePrice:
    """The third per-share dollar value is the Sale Price; it is present only for
    'Shares Sold' releases (sold on the market), not 'Shares Traded' (net-settled)."""

    REPO = __import__("pathlib").Path(__file__).parent.parent
    SOLD_PDF   = REPO / "release-confirmations" / "2018-02-01-R1084-2020-06-01.pdf"
    TRADED_PDF = REPO / "release-confirmations" / "2018-02-01-R1084-2018-11-15.pdf"

    def test_shares_sold_release_has_distinct_sale_price(self, parse_pdf_mod):
        if not self.SOLD_PDF.exists():
            pytest.skip("sample PDF not present")
        row = parse_pdf_mod.parse_pdf(self.SOLD_PDF)
        assert row["Price per share ($)"] == pytest.approx(112.95)
        assert row["Sale price per share ($)"] == pytest.approx(111.33)
        assert row["Price per share ($)"] != row["Sale price per share ($)"]

    def test_shares_traded_release_has_no_sale_price(self, parse_pdf_mod):
        if not self.TRADED_PDF.exists():
            pytest.skip("sample PDF not present")
        row = parse_pdf_mod.parse_pdf(self.TRADED_PDF)
        assert row["Price per share ($)"] == pytest.approx(44.53)
        assert row["Sale price per share ($)"] is None


# ── attach_rate ───────────────────────────────────────────────────────────────

class TestAttachRate:
    def _exrates(self, ccb):
        rows = [
            {"Start Date": "01/01/2020", "End Date": "31/01/2020",
             "Currency units per £1": 1.3000,
             "Start_dt": datetime(2020, 1, 1), "End_dt": datetime(2020, 1, 31)},
            {"Start Date": "01/02/2020", "End Date": "29/02/2020",
             "Currency units per £1": 1.2800,
             "Start_dt": datetime(2020, 2, 1), "End_dt": datetime(2020, 2, 29)},
        ]
        return pd.DataFrame(rows)

    def test_date_within_range_returns_rate(self, ccb):
        xr = self._exrates(ccb)
        result = ccb.attach_rate(
            pd.Series([datetime(2020, 1, 15)]), xr
        )
        assert result.iloc[0] == pytest.approx(1.3000)

    def test_date_on_start_boundary(self, ccb):
        xr = self._exrates(ccb)
        result = ccb.attach_rate(pd.Series([datetime(2020, 1, 1)]), xr)
        assert result.iloc[0] == pytest.approx(1.3000)

    def test_date_on_end_boundary(self, ccb):
        xr = self._exrates(ccb)
        result = ccb.attach_rate(pd.Series([datetime(2020, 1, 31)]), xr)
        assert result.iloc[0] == pytest.approx(1.3000)

    def test_date_outside_all_ranges_raises_naming_the_date(self, ccb):
        xr = self._exrates(ccb)
        with pytest.raises(ValueError, match="No exchange rate found for 2021-06-01"):
            ccb.attach_rate(pd.Series([datetime(2021, 6, 1)]), xr)

    def test_date_in_second_range(self, ccb):
        xr = self._exrates(ccb)
        result = ccb.attach_rate(pd.Series([datetime(2020, 2, 15)]), xr)
        assert result.iloc[0] == pytest.approx(1.2800)

    def test_multiple_dates_in_series(self, ccb):
        xr = self._exrates(ccb)
        dates = pd.Series([datetime(2020, 1, 10), datetime(2020, 2, 10)])
        result = ccb.attach_rate(dates, xr)
        assert result.iloc[0] == pytest.approx(1.3000)
        assert result.iloc[1] == pytest.approx(1.2800)


# ── load_sales ────────────────────────────────────────────────────────────────

class TestLoadSales:
    def _csv(self, tmp_path, content):
        p = tmp_path / "sales.csv"
        p.write_text(content)
        return str(p)

    def test_standard_headers(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Date,Shares,Price per share ($)\n"
            "2023-01-15,100,25.50\n"
        )
        df = ccb.load_sales(p)
        assert len(df) == 1
        assert df["Sold"].iloc[0] == pytest.approx(100.0)
        assert df["Price per share ($)"].iloc[0] == pytest.approx(25.50)

    def test_alternative_header_names(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Transaction Date,Quantity,Sale Price ($)\n"
            "2023-01-15,200,30.00\n"
        )
        df = ccb.load_sales(p)
        assert df["Sold"].iloc[0] == pytest.approx(200.0)
        assert df["Price per share ($)"].iloc[0] == pytest.approx(30.0)

    def test_etrade_style_headers(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Sale Date,Shares Sold,Price per share ($)\n"
            "2023-03-01,50,18.00\n"
        )
        df = ccb.load_sales(p)
        assert df["Sold"].iloc[0] == pytest.approx(50.0)

    def test_multiple_rows(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Date,Shares,Price per share ($)\n"
            "2023-01-15,100,25.50\n"
            "2023-06-01,200,30.00\n"
        )
        df = ccb.load_sales(p)
        assert len(df) == 2

    def test_unrecognisable_columns_raise_value_error(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "foo,bar,baz\n"
            "2023-01-15,100,25.50\n"
        )
        with pytest.raises(ValueError, match="missing recognizable columns"):
            ccb.load_sales(p)

    def test_type_column_classifies_buy_and_sell(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Date,Type,Shares,Price per share ($)\n"
            "2023-01-15,Buy,100,25.50\n"
            "2023-06-01,Sell,50,30.00\n"
            "2023-07-01,ESPP purchase,10,12.00\n"
        )
        df = ccb.load_sales(p)
        assert list(df[ccb.TYPE_LABEL]) == [ccb.BUY_TYPE, ccb.SELL_TYPE, ccb.BUY_TYPE]

    def test_no_type_column_means_all_sells(self, ccb, tmp_path):
        p = self._csv(tmp_path,
            "Date,Shares,Price per share ($)\n"
            "2023-01-15,100,25.50\n"
        )
        df = ccb.load_sales(p)
        # No Type column at all → build_events treats every row as a Sell.
        assert ccb.TYPE_LABEL not in df.columns


# ── HMRC HS284 (2026) official examples ──────────────────────────────────────

class TestHMRCHS284Examples:
    """
    Tests derived from the worked examples in HMRC helpsheet HS284
    (Shares and Capital Gains Tax, tax year 2025–2026).
    """

    def test_example1_four_purchases_form_single_pool(self, ccb, mk):
        """
        Example 1: Wilson & Strickland plc — four purchases across different years
        all pool into a single Section 104 holding of 12,000 shares.
        Costs below are illustrative; the example only establishes pool composition.
        """
        events = mk([
            (ccb.BUY_TYPE, "1979-06-01", 2000, 1.00),
            (ccb.BUY_TYPE, "1982-11-01", 2500, 2.00),
            (ccb.BUY_TYPE, "1987-08-01", 2500, 3.00),
            (ccb.BUY_TYPE, "2006-05-01", 5000, 4.00),
        ])
        _, holdings, _, _ = ccb.get_gains_and_holdings(events)
        expected_pool = 2000 * 1.0 + 2500 * 2.0 + 2500 * 3.0 + 5000 * 4.0
        assert holdings[3] == pytest.approx(expected_pool)

    def test_example2_bed_and_breakfasting_partial_match(self, ccb, mk):
        """
        Example 2: Mr Schneider sells 4,000 shares on 30 Aug 2025 for £6,000 total
        and buys 500 shares on 11 Sep 2025 for £850 total (£1.70/share).

        HMRC shows the 30-day matched portion:
          Proceeds  (500 / 4,000 × £6,000) = £750
          Cost                               = £850
          Loss                               = £100

        Pool uses £1/share so the total gain across both portions is:
          £6,000 − (£850 + 3,500 × £1.00) = £1,650
        """
        events = mk([
            (ccb.BUY_TYPE,  "2020-01-01", 9500, 1.00),
            (ccb.SELL_TYPE, "2025-08-30", 4000, 1.50),   # £6,000 total
            (ccb.BUY_TYPE,  "2025-09-11",  500, 1.70),   # £850 total, 12 days later
        ])
        gains, _, notes, _ = ccb.get_gains_and_holdings(events)
        assert gains[1] == pytest.approx(6000 - (500 * 1.70 + 3500 * 1.00))
        assert "30-day" in notes[1]
        assert "Section 104" in notes[1]


# ── Full-pipeline integration ─────────────────────────────────────────────────

class TestMainIntegration:
    def _write(self, path, content):
        path.write_text(content)
        return str(path)

    def test_end_to_end_basic_pipeline(self, ccb, tmp_path, capsys):
        """
        Scenario:
          2020-03-01  Buy 900 sh @ £10  (release with 100 withheld → WithholdingSell)
          2020-07-01  Buy 500 sh @ £12  (no withholding)
          2020-09-01  Sell 200 sh @ £15

        Pool before sell: 900*10 + 500*12 = £15 000 across 1 400 sh
        Sell 200: allowable = 15 000/1 400 * 200 = £2 142.857...
        Gain = 200*15 - 2 142.857 = £857.143...

        Tax years (GBP/USD = 1.0 throughout):
          2019/20: WithholdingSell 2020-03-01 → £0
          2020/21: Sell 2020-09-01 → £857.14...
        """
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-03-01,1000,100,900,10.00\n"
            "2020-07-01,500,0,500,12.00\n"
        )
        sales = self._write(tmp_path / "sales.csv",
            "Date,Shares,Price per share ($)\n"
            "2020-09-01,200,15.00\n"
        )

        ccb.main(["prog", "-r", rel, "-x", xr, "-s", sales])
        out, err = capsys.readouterr()

        rows = list(csv.DictReader(io.StringIO(out)))
        types = [r["Type"] for r in rows]

        # All three event types present
        assert "Buy" in types
        assert "WithholdingSell" in types
        assert "Sell" in types

        # WithholdingSell gain is zero
        ws = next(r for r in rows if r["Type"] == "WithholdingSell")
        assert float(ws["Gains / Losses (GBP)"]) == pytest.approx(0.0)
        assert "tax withholding" in ws["Matching Rule"]

        # Sell gain matches manual calculation
        sell = next(r for r in rows if r["Type"] == "Sell")
        pool_cost = 900 * 10 + 500 * 12   # £15 000
        expected_gain = 200 * 15 - pool_cost / 1400 * 200
        assert float(sell["Gains / Losses (GBP)"]) == pytest.approx(expected_gain, rel=1e-4)
        assert "Section 104" in sell["Matching Rule"]

        # Tax-year summary written to stderr
        assert "2019/20" in err
        assert "2020/21" in err

        # FX provenance is stated in the output so the filing is self-documenting.
        assert "HMRC published monthly rates" in err

    def test_30_day_rule_applied_in_pipeline(self, ccb, tmp_path, capsys):
        """
        Sell from pool; then a release occurs within 30 days → 30-day rule must apply.

        2020-01-01  Buy 100 sh @ £10 (pool: 100 sh, £1 000)
        2020-06-01  Sell 50 sh @ £12  (should be matched against 2020-06-15 buy)
        2020-06-15  Buy 50 sh @ £14  (within 30 days of sell)

        With 30-day rule: gain = 50*12 - 50*14 = -£100
        Without it:       gain = 50*12 - 50*10 =  £100
        """
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-01-01,100,0,100,10.00\n"
            "2020-06-15,50,0,50,14.00\n"
        )
        sales = self._write(tmp_path / "sales.csv",
            "Date,Shares,Price per share ($)\n"
            "2020-06-01,50,12.00\n"
        )

        ccb.main(["prog", "-r", rel, "-x", xr, "-s", sales])
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))
        sell = next(r for r in rows if r["Type"] == "Sell")
        assert float(sell["Gains / Losses (GBP)"]) == pytest.approx(50 * 12 - 50 * 14)
        assert "30-day" in sell["Matching Rule"]

    def test_withholding_sale_price_difference_produces_taxable_gain(self, ccb, tmp_path, capsys):
        """A 'Shares Sold' release whose sale price differs from market value is taxable."""
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($),Sale price per share ($)\n"
            "2020-06-01,1000,200,800,10.00,9.50\n"
        )
        ccb.main(["prog", "-r", rel, "-x", xr])
        out, err = capsys.readouterr()
        rows = list(csv.DictReader(io.StringIO(out)))
        ws = next(r for r in rows if r["Type"] == "WithholdingSell")
        assert float(ws["Gains / Losses (GBP)"]) == pytest.approx(200 * (9.50 - 10.00))
        assert ws["Sale price per share (GBP)"] == "9.5000"
        # 1 taxable event (the withholding sell) in 2020/21
        assert "2020/21" in err

    def test_summary_includes_tax_years_without_taxable_events(self, ccb, tmp_path, capsys):
        """Years between the first and last events appear even with zero disposals."""
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2019,31/12/2023\n"
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2019-06-01,100,0,100,10.00\n"
        )
        sales = self._write(tmp_path / "sales.csv",
            "Date,Shares,Price per share ($)\n"
            "2022-09-01,50,15.00\n"
        )
        ccb.main(["prog", "-r", rel, "-x", xr, "-s", sales])
        err = capsys.readouterr().err
        # Span runs 2019/20 .. 2022/23; the middle years have no taxable events
        for ty in ("2019/20", "2020/21", "2021/22", "2022/23"):
            assert ty in err

    def test_output_columns_present(self, ccb, tmp_path, capsys):
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-06-01,100,0,100,10.00\n"
        )
        ccb.main(["prog", "-r", rel, "-x", xr])
        out = capsys.readouterr().out
        header = out.splitlines()[0].split(",")
        for col in ("Type", "Date", "Price per share (GBP)", "Holdings (GBP)",
                    "Gains / Losses (GBP)", "Matching Rule"):
            assert col in header, f"Missing column: {col}"

    def test_missing_exchange_rate_raises(self, ccb, tmp_path):
        """A release date outside all exchange-rate ranges fails loudly at rate
        lookup, naming the offending date, instead of silently defaulting."""
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/01/2020\n"   # only covers January
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-06-01,100,0,100,10.00\n"    # June — outside the rate table
        )
        with pytest.raises(ValueError, match="No exchange rate found for 2020-06-01"):
            ccb.main(["prog", "-r", rel, "-x", xr])


# ── build_events (shared CLI/GUI pipeline) ────────────────────────────────────

class TestBuildEvents:
    """build_events is the single pipeline used by both the CLI and the GUI."""

    def _xr(self):
        return pd.DataFrame({
            "Start Date": ["01/01/2020"],
            "End Date":   ["31/12/2020"],
            "Currency units per £1": [1.0],
        })

    def _releases(self, date_col):
        return pd.DataFrame({
            date_col:              ["2020-03-01", "2020-07-01"],
            "Granted":             [1000, 500],
            "Sold":                [100, 0],
            "Issued":              [900, 500],
            "Price per share ($)": [10.0, 12.0],
        })

    def test_accepts_release_date_or_date_column(self, ccb):
        """The GUI passes 'Release Date'; the CLI passes 'Date'. Both must work."""
        ev_release = ccb.build_events(self._releases("Release Date"), None, self._xr())
        ev_date    = ccb.build_events(self._releases("Date"), None, self._xr())
        cols = [ccb.GAINS_LABEL, ccb.HOLDINGS_GBP_LABEL, ccb.OWNED_SHARES_LABEL]
        pd.testing.assert_frame_equal(ev_release[cols], ev_date[cols])

    def test_injects_withholding_sell_and_computes_pool_sale(self, ccb):
        sales = pd.DataFrame({
            "Date":                ["2020-09-01"],
            "Sold":                [200.0],
            "Price per share ($)": [15.0],
        })
        ev = ccb.build_events(self._releases("Release Date"), sales, self._xr())
        types = list(ev[ccb.TYPE_LABEL])
        assert ccb.WITHHOLDING_SELL_TYPE in types
        sell = ev[ev[ccb.TYPE_LABEL] == ccb.SELL_TYPE].iloc[0]
        # Pool: 900*10 + 500*12 = 15000 over 1400 sh; sell 200 from pool.
        # Reported gains are quantised to pennies (£857.142857… → £857.14).
        assert sell[ccb.GAINS_LABEL] == pytest.approx(round(200 * 15 - 15000 / 1400 * 200, 2))
        assert "Section 104" in sell[ccb.MATCHING_LABEL]

    def test_gains_and_holdings_quantised_to_pennies(self, ccb):
        """Reported gains and pool cost are rounded to 2 dp; avg cost keeps
        full precision (computed from the unrounded pool)."""
        sales = pd.DataFrame({
            "Date":                ["2020-09-01"],
            "Sold":                [200.0],
            "Price per share ($)": [15.0],
        })
        ev = ccb.build_events(self._releases("Release Date"), sales, self._xr())
        for col in (ccb.GAINS_LABEL, ccb.HOLDINGS_GBP_LABEL):
            for v in ev[col].dropna():
                assert round(v, 2) == v, f"{col} value {v!r} not quantised to pennies"
        # Avg cost is a per-share figure and is NOT forced to pennies.
        sell = ev[ev[ccb.TYPE_LABEL] == ccb.SELL_TYPE].iloc[0]
        assert sell[ccb.AVG_COST_GBP_LABEL] == pytest.approx(15000 / 1400)

    def test_includes_owned_and_avg_cost_columns(self, ccb):
        ev = ccb.build_events(self._releases("Release Date"), None, self._xr())
        for col in (ccb.OWNED_SHARES_LABEL, ccb.AVG_COST_GBP_LABEL):
            assert col in ev.columns
        last = ev.iloc[-1]
        assert last[ccb.OWNED_SHARES_LABEL] == pytest.approx(1400.0)
        assert last[ccb.AVG_COST_GBP_LABEL] == pytest.approx(15000 / 1400)

    def test_generic_buy_acquisition_joins_the_pool(self, ccb):
        """A Type=Buy transaction (e.g. ESPP) pools with RSU releases, changing
        the Section 104 average and therefore the gain on a later sale."""
        releases = pd.DataFrame({
            "Release Date":        ["2020-01-01"],
            "Granted":             [100], "Sold": [0], "Issued": [100],
            "Price per share ($)": [10.0],
        })
        sales = pd.DataFrame({
            "Date": ["2020-02-01", "2020-03-01"],
            ccb.TYPE_LABEL: [ccb.BUY_TYPE, ccb.SELL_TYPE],
            "Sold": [100.0, 100.0],
            "Price per share ($)": [20.0, 30.0],
        })
        ev = ccb.build_events(releases, sales, self._xr())

        # Two acquisitions in the pool: 100@£10 + 100@£20 = £3000 over 200 sh.
        assert (ev[ccb.TYPE_LABEL] == ccb.BUY_TYPE).sum() == 2
        sell = ev[ev[ccb.TYPE_LABEL] == ccb.SELL_TYPE].iloc[0]
        # Sell 100 from the pool: allowable = 200 sh avg £15 × 100 = £1500.
        assert sell[ccb.GAINS_LABEL] == pytest.approx(100 * 30 - 1500)
        # The generic Buy is an acquisition, not a disposal — no withholding sell.
        assert (ev[ccb.TYPE_LABEL] == ccb.WITHHOLDING_SELL_TYPE).sum() == 0

    def test_generic_buy_same_day_match(self, ccb):
        """A Buy and Sell on the same day match same-day, bypassing the pool."""
        sales = pd.DataFrame({
            "Date": ["2020-05-01", "2020-05-01"],
            ccb.TYPE_LABEL: [ccb.BUY_TYPE, ccb.SELL_TYPE],
            "Sold": [50.0, 50.0],
            "Price per share ($)": [20.0, 21.0],
        })
        releases = pd.DataFrame({
            "Release Date":        ["2020-01-01"],
            "Granted":             [100], "Sold": [0], "Issued": [100],
            "Price per share ($)": [10.0],
        })
        ev = ccb.build_events(releases, sales, self._xr())
        sell = ev[ev[ccb.TYPE_LABEL] == ccb.SELL_TYPE].iloc[0]
        assert sell[ccb.GAINS_LABEL] == pytest.approx(50 * 21 - 50 * 20)
        assert "same-day" in sell[ccb.MATCHING_LABEL]

    def test_main_uses_build_events(self, ccb, tmp_path, capsys):
        """CLI output and a direct build_events call must agree on the gains."""
        xr = tmp_path / "xr.csv"
        xr.write_text(
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/12/2020\n"
        )
        rel = tmp_path / "rel.csv"
        rel.write_text(
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-03-01,1000,100,900,10.00\n"
            "2020-07-01,500,0,500,12.00\n"
        )
        ccb.main(["prog", "-r", str(rel), "-x", str(xr)])
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))

        ev = ccb.build_events(
            pd.read_csv(rel), None, pd.read_csv(xr)
        )
        cli_buys = [r for r in rows if r["Type"] == "Buy"]
        eng_buys = ev[ev[ccb.TYPE_LABEL] == ccb.BUY_TYPE]
        assert len(cli_buys) == len(eng_buys)
