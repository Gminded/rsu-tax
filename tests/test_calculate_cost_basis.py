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

    def test_date_outside_all_ranges_returns_none(self, ccb):
        xr = self._exrates(ccb)
        result = ccb.attach_rate(pd.Series([datetime(2021, 6, 1)]), xr)
        assert result.iloc[0] is None

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

    def test_missing_exchange_rate_produces_blank_gbp_price(self, ccb, tmp_path, capsys):
        """A release date outside all exchange-rate ranges has no GBP price."""
        xr = self._write(tmp_path / "xr.csv",
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
            "USA,Dollar,USD,1.0000,01/01/2020,31/01/2020\n"   # only covers January
        )
        rel = self._write(tmp_path / "rel.csv",
            "Release Date,Granted,Sold,Issued,Price per share ($)\n"
            "2020-06-01,100,0,100,10.00\n"    # June — outside the rate table
        )
        ccb.main(["prog", "-r", rel, "-x", xr])
        out = capsys.readouterr().out
        rows = list(csv.DictReader(io.StringIO(out)))
        # Price per share (GBP) should be blank/empty when rate is unavailable
        assert rows[0]["Price per share (GBP)"] == ""
