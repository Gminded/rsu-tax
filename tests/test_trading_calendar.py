"""
Tests for scripts/trading_calendar.py — rolling a nominal release date forward
to the first real trading day, which decides FX month, UK tax year and which
HS284 matching rule applies.
"""
import datetime

import pytest

import trading_calendar as tc


def _roll(s, exchange="XNAS"):
    return tc.first_trading_day_on_or_after(s, exchange).isoformat()


class TestFirstTradingDay:
    def test_saturday_rolls_to_monday(self):
        assert _roll("2019-06-01") == "2019-06-03"  # Sat → Mon

    def test_sunday_rolls_to_monday(self):
        assert _roll("2019-09-01") == "2019-09-03"  # Sun → Mon

    def test_good_friday_rolls_past(self):
        # Good Friday is a market holiday but not a US federal holiday.
        assert _roll("2019-04-19") == "2019-04-22"

    def test_already_a_trading_day_unchanged(self):
        assert _roll("2019-06-03") == "2019-06-03"

    def test_accepts_date_object(self):
        assert _roll(datetime.date(2019, 6, 1)) == "2019-06-03"

    def test_month_boundary(self):
        # Sat 31 Aug 2019 rolls into September → a different FX month.
        assert _roll("2019-08-31") == "2019-09-03"

    def test_sat_5_april_lands_in_next_tax_year(self, ccb):
        # The boundary case that actually moves money between UK tax years.
        nominal = datetime.datetime(2025, 4, 5)            # Sat 5 Apr → 2024/25
        corrected = tc.first_trading_day_on_or_after("2025-04-05")
        corrected = datetime.datetime(corrected.year, corrected.month, corrected.day)
        assert ccb._tax_year_start(nominal) == 2024
        assert ccb._tax_year_start(corrected) == 2025

    def test_non_default_exchange_behaves_differently(self):
        # US Independence Day 2024 (Thu): NASDAQ closed, London open.
        assert _roll("2024-07-04", "XNAS") == "2024-07-05"
        assert _roll("2024-07-04", "XLON") == "2024-07-04"

    def test_unknown_exchange_raises_listing_valid_codes(self):
        with pytest.raises(ValueError, match="XNAS"):
            tc.first_trading_day_on_or_after("2024-01-02", "NOPE")
