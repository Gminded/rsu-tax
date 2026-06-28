"""
Tests for the orders.json → orders.csv parsing in scripts/download_etrade.py.

Only the pure parsing is tested (no network / Playwright): a captured E*Trade
orders.json `data.pse.list` entry must map to the orders.csv schema that
load_sales recognises, with all broker/SEC fees summed and income tax excluded.
"""
import download_etrade as de


# A synthetic order shaped like the E*Trade orders.json feed (all values fake —
# the field names and formats are what matter for the parser).
SAMPLE_ORDER = {
    "actionDate": "06/09/2026",
    "executionDate": "20260609133507",
    "avgPrice": 50.00,
    "executedPrice": 50.00,
    "status": "Settled",
    "transType": "Sell Restricted Stock",
    "tradeId": 1,
    "commissionFee": 7.99,
    "postageHandlingFee": 0.0,
    "brokerAssitFee": 0.0,
    "specialHandlingFee": 0.0,
    "applicableTax": 12.34,   # income tax — must NOT be added to Fee
    "secFee": 1.10,
    "numberOfShares": 100.0,
}


class TestOrderDate:
    def test_prefers_execution_datetime(self):
        assert de._order_date(SAMPLE_ORDER) == "2026-06-09"

    def test_falls_back_to_action_date(self):
        assert de._order_date({"actionDate": "1/5/2024"}) == "2024-01-05"

    def test_missing_dates_returns_none(self):
        assert de._order_date({}) is None


class TestOrdersToRows:
    def test_maps_to_load_sales_schema(self):
        (row,) = de._orders_to_rows([SAMPLE_ORDER])
        assert row == {
            "Date": "2026-06-09",
            "Type": "Sell Restricted Stock",
            "Shares": 100.0,
            "Price per share ($)": 50.00,
            "Fee ($)": 9.09,   # commission 7.99 + sec 1.10; tax excluded
        }

    def test_skips_unexecuted_orders(self):
        unexecuted = {**SAMPLE_ORDER, "executedPrice": 0, "avgPrice": 0}
        assert de._orders_to_rows([unexecuted]) == []

    def test_discards_cancelled_orders(self):
        # A cancelled order never completed → not a chargeable disposal, even if
        # it still carries shares/price fields.
        cancelled = {**SAMPLE_ORDER, "status": "Cancelled", "statusCode": "CA"}
        assert de._orders_to_rows([cancelled]) == []

    def test_keeps_only_settled_among_mixed_statuses(self):
        mixed = [
            SAMPLE_ORDER,
            {**SAMPLE_ORDER, "tradeId": 2, "status": "Open", "statusCode": "OP"},
            {**SAMPLE_ORDER, "tradeId": 3, "status": "Rejected", "statusCode": "RJ"},
        ]
        rows = de._orders_to_rows(mixed)
        assert len(rows) == 1

    def test_type_classifies_as_sell_downstream(self, ccb):
        # The carried transType must read as a disposal in the engine.
        assert ccb._classify_type("Sell Restricted Stock") == ccb.SELL_TYPE
