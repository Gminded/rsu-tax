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

import sys, argparse, math
import pandas as pd
from datetime import datetime, timedelta

TYPE_LABEL                = "Type"
DATE_LABEL                = "Date"
DATE_DT                   = "Date_dt"
GRANTED_LABEL             = "Granted"
SOLD_LABEL                = "Sold"
ISSUED_LABEL              = "Issued"
START_DATE_LABEL          = "Start Date"
END_DATE_LABEL            = "End Date"
RELEASE_DATE_LABEL        = "Release Date"
PRICE_PER_SHARE_USD_LABEL = "Price per share ($)"
PRICE_PER_SHARE_GBP_LABEL = "Price per share (GBP)"
SALE_PRICE_PER_SHARE_USD_LABEL = "Sale price per share ($)"
SALE_PRICE_PER_SHARE_GBP_LABEL = "Sale price per share (GBP)"
GBP_USD_LABEL             = "GBP/USD"
HOLDINGS_GBP_LABEL        = "Holdings (GBP)"
OWNED_SHARES_LABEL        = "Owned shares"
AVG_COST_GBP_LABEL        = "Avg cost / share (GBP)"
GAINS_LABEL               = "Gains / Losses (GBP)"
MATCHING_LABEL            = "Matching Rule"

# Type values
BUY_TYPE              = "Buy"
SELL_TYPE             = "Sell"
WITHHOLDING_SELL_TYPE = "WithholdingSell"

# Human-readable labels for the two ways a vest can settle the tax due on it.
# Both are carried internally as WITHHOLDING_SELL_TYPE — they differ only in how
# the withheld shares were settled, which is recorded by the presence of a
# distinct broker sale price (see _is_sell_to_cover).
WITHHOLDING_LABEL   = "Withholding Sell"   # net-settled at market value
SELL_TO_COVER_LABEL = "Sell to cover"      # withheld shares sold on the market


def _is_sell_to_cover(row) -> bool:
    """True when a WithholdingSell disposed of the withheld shares on the open
    market (a distinct broker sale price was recorded) rather than net-settling
    at market value."""
    sale = row.get(SALE_PRICE_PER_SHARE_USD_LABEL)
    return sale is not None and not (isinstance(sale, float) and math.isnan(sale))


def event_type_label(row) -> str:
    """Map an event row's machine Type to a human-readable label, distinguishing
    a net-settled Withholding Sell from a Sell to cover.  Shared by the CLI and
    GUI so the distinction is defined once and never re-derived per interface."""
    if row[TYPE_LABEL] == WITHHOLDING_SELL_TYPE:
        return SELL_TO_COVER_LABEL if _is_sell_to_cover(row) else WITHHOLDING_LABEL
    return row[TYPE_LABEL]

# FX provenance.  HMRC requires that USD→GBP conversions use a single, consistent
# source throughout a return; this project defaults to HMRC's published monthly
# rates.  The note is surfaced in the output so the filing is self-documenting.
FX_PROVENANCE_NOTE = (
    "Exchange rates: HMRC published monthly rates (USD→GBP). "
    "HMRC requires one consistent source across the whole return."
)

# ---------- Field validation ----------
def require_float(val, label: str, context: str) -> float:
    if val is None or (isinstance(val, float) and math.isnan(val)) or str(val).strip() == "":
        raise ValueError(f"[{context}] Required field '{label}' is missing or empty")
    return float(val)

# ---------- Date parsing ----------
def parse_date_ymd(s: str) -> datetime:
    s = str(s).strip().replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d")

def parse_date_dmy(s: str) -> datetime:
    return datetime.strptime(str(s).strip(), "%d/%m/%Y")

# ---------- Exchange rate lookup (inclusive ranges) ----------
def attach_rate(df_dates: pd.Series, exrates_df: pd.DataFrame) -> pd.Series:
    """Look up the GBP→USD rate covering each date.

    Fails loudly, naming the offending date, when no rate range covers it —
    rather than returning None and producing a silent NaN gain downstream.
    """
    def find_rate(d):
        for _, r in exrates_df.iterrows():
            if r["Start_dt"] <= d <= r["End_dt"]:
                return r["Currency units per £1"]
        raise ValueError(
            f"No exchange rate found for {d.date()}: the rate table does not "
            f"cover this date. Add the HMRC monthly rate file for that period "
            f"(or upload it in the Exchange Rates section)."
        )
    return df_dates.apply(find_rate)

# ---------- Sales CSV helpers ----------
CANDIDATE_DATE   = ["date", "sale date", "transaction date"]
CANDIDATE_SHARES = ["shares", "quantity", "units", "issued", "shares sold", "qty"]
CANDIDATE_PRICE  = ["price per share ($)", "priceusd", "price", "sale price", "sale price ($)"]
CANDIDATE_TYPE   = ["type", "transaction type", "record type", "buy/sell", "side", "action"]

# Tokens that mark a row as an acquisition (anything else is treated as a Sell).
_BUY_TOKENS = ("buy", "acqui", "purchase", "espp", "osps", "exercise", "vest", "reinvest")

def _find_col(cols, candidates):
    lc = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in lc:
            return lc[cand]
    for c in cols:
        cl = c.lower()
        if any(cand in cl for cand in candidates):
            return c
    return None

def _classify_type(val) -> str:
    """Map a free-text transaction-type cell to BUY_TYPE or SELL_TYPE."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return SELL_TYPE
    text = str(val).strip().lower()
    if any(tok in text for tok in _BUY_TOKENS):
        return BUY_TYPE
    return SELL_TYPE

def load_sales(sales_csv: str) -> pd.DataFrame:
    s = pd.read_csv(sales_csv)
    cols = list(s.columns)
    date_col   = _find_col(cols, CANDIDATE_DATE)
    shares_col = _find_col(cols, CANDIDATE_SHARES)
    price_col  = _find_col(cols, CANDIDATE_PRICE)
    type_col   = _find_col(cols, CANDIDATE_TYPE)
    if not date_col or not shares_col or not price_col:
        raise ValueError(
            f"sales.csv missing recognizable columns. "
            f"Found: {cols}. Need Date~{CANDIDATE_DATE}, "
            f"Shares~{CANDIDATE_SHARES}, Price~{CANDIDATE_PRICE}"
        )
    out = pd.DataFrame({
        DATE_LABEL:                s[date_col].astype(str).str.replace("/", "-"),
        SOLD_LABEL:                s[shares_col].astype(float),
        PRICE_PER_SHARE_USD_LABEL: s[price_col].astype(float),
    })
    # An optional Type column lets the same file carry generic acquisitions
    # (ESPP / open-market buys / option exercises) so they join the same
    # Section 104 pool as RSU releases.  Absent → every row is a Sell.
    if type_col:
        out[TYPE_LABEL] = s[type_col].apply(_classify_type)
    return out


# ---------- HMRC share-identification rules ----------
# Per HS284, disposals are matched in this priority order:
#   1. Same-day acquisitions
#   2. Acquisitions in the 30 days following the disposal (FIFO within window)
#   3. Section 104 pool (weighted-average cost)

def get_gains_and_holdings(events: pd.DataFrame):
    """
    Returns (gains, pool_costs_after, matching_notes, pool_units_after) — one
    entry per row in events.

    Buy rows enter the Section 104 pool to the extent they are not consumed by
    same-day or 30-day matching against a Sell.  WithholdingSell rows are
    informational (gain = 0, pool unchanged).  pool_units_after is the number of
    shares still held in the Section 104 pool immediately after each event.
    """
    records = events.reset_index(drop=True).to_dict("records")
    n = len(records)

    # How many units of each Buy row have been "reserved" by rules 1 & 2.
    buy_consumed = [0.0] * n

    # --- Pass 1: identify same-day and 30-day matches for every Sell ---
    sell_info = {}  # row index → match details

    for i, rec in enumerate(records):
        if rec[TYPE_LABEL] != SELL_TYPE:
            continue

        sell_date  = rec[DATE_DT]
        sell_units = require_float(rec[SOLD_LABEL], SOLD_LABEL, rec[DATE_LABEL])
        price_gbp  = require_float(rec[PRICE_PER_SHARE_GBP_LABEL], PRICE_PER_SHARE_GBP_LABEL, rec[DATE_LABEL])
        proceeds   = sell_units * price_gbp

        remaining     = sell_units
        matched_cost  = 0.0
        notes         = []

        # Rule 1 — same-day acquisitions
        same_day_matched = 0.0
        for j, buy in enumerate(records):
            if remaining <= 0:
                break
            if buy[TYPE_LABEL] != BUY_TYPE or buy[DATE_DT] != sell_date:
                continue
            avail   = require_float(buy[ISSUED_LABEL], ISSUED_LABEL, buy[DATE_LABEL]) - buy_consumed[j]
            matched = min(remaining, avail)
            if matched <= 0:
                continue
            buy_consumed[j] += matched
            remaining        -= matched
            matched_cost     += matched * require_float(buy[PRICE_PER_SHARE_GBP_LABEL], PRICE_PER_SHARE_GBP_LABEL, buy[DATE_LABEL])
            same_day_matched += matched

        if same_day_matched > 0:
            notes.append(f"same-day ({same_day_matched:.0f} sh)")

        # Rule 2 — acquisitions in the 30 days following the disposal (FIFO)
        if remaining > 0:
            cutoff = sell_date + timedelta(days=30)
            future_buys = sorted(
                [(j, r) for j, r in enumerate(records)
                 if r[TYPE_LABEL] == BUY_TYPE and sell_date < r[DATE_DT] <= cutoff],
                key=lambda x: x[1][DATE_DT],
            )
            thirty_day_by_date: dict[str, float] = {}
            for j, buy in future_buys:
                if remaining <= 0:
                    break
                avail   = require_float(buy[ISSUED_LABEL], ISSUED_LABEL, buy[DATE_LABEL]) - buy_consumed[j]
                matched = min(remaining, avail)
                if matched <= 0:
                    continue
                buy_consumed[j] += matched
                remaining        -= matched
                matched_cost     += matched * require_float(buy[PRICE_PER_SHARE_GBP_LABEL], PRICE_PER_SHARE_GBP_LABEL, buy[DATE_LABEL])
                acq_date = buy[DATE_LABEL]
                thirty_day_by_date[acq_date] = thirty_day_by_date.get(acq_date, 0.0) + matched

            for acq_date, qty in thirty_day_by_date.items():
                notes.append(f"30-day ({qty:.0f} sh acq {acq_date})")

        sell_info[i] = {
            "pool_units":    remaining,      # quantity still to draw from pool
            "pre_pool_cost": matched_cost,
            "proceeds":      proceeds,
            "notes":         notes,
        }

    # --- Pass 2: process events chronologically, maintain pool ---
    pool_units = 0.0
    pool_cost  = 0.0

    gains          = []
    pool_costs_out = []
    pool_units_out = []
    matching_notes = []

    for i, rec in enumerate(records):
        typ       = rec[TYPE_LABEL]
        price_gbp = require_float(rec[PRICE_PER_SHARE_GBP_LABEL], PRICE_PER_SHARE_GBP_LABEL, rec[DATE_LABEL])

        if typ == BUY_TYPE:
            issued     = require_float(rec[ISSUED_LABEL], ISSUED_LABEL, rec[DATE_LABEL])
            into_pool  = issued - buy_consumed[i]
            if into_pool > 1e-9:
                pool_units += into_pool
                pool_cost  += into_pool * price_gbp
            gains.append(0.0)
            pool_costs_out.append(pool_cost)
            pool_units_out.append(pool_units)
            if buy_consumed[i] > 1e-9:
                matching_notes.append(
                    f"{buy_consumed[i]:.0f} sh matched by rule; "
                    f"{into_pool:.0f} sh → Section 104 pool"
                )
            else:
                matching_notes.append("")

        elif typ == WITHHOLDING_SELL_TYPE:
            # Shares the broker sold to cover income tax on the vest.  These were
            # never in the Section 104 pool: under the same-day rule their cost
            # basis is the acquisition cost (market value at release = price_gbp),
            # and the proceeds are the broker's actual sale price.  When the
            # shares were net-settled at market value (no separate sale price)
            # the gain is zero.
            units    = require_float(rec[SOLD_LABEL], SOLD_LABEL, rec[DATE_LABEL])
            sale_gbp = rec.get(SALE_PRICE_PER_SHARE_GBP_LABEL)
            if sale_gbp is None or (isinstance(sale_gbp, float) and math.isnan(sale_gbp)):
                sale_gbp = price_gbp
            gain = (sale_gbp - price_gbp) * units
            gains.append(gain)
            pool_costs_out.append(pool_cost)
            pool_units_out.append(pool_units)
            if abs(gain) > 1e-9:
                matching_notes.append(
                    f"same-day rule (tax withholding); sold {units:.0f} sh @ "
                    f"£{sale_gbp:.4f} vs MV £{price_gbp:.4f}"
                )
            else:
                matching_notes.append("same-day rule (tax withholding)")

        else:  # SELL_TYPE
            if i not in sell_info:
                raise ValueError(
                    f"[{rec[DATE_LABEL]}] SELL record at index {i} was not pre-processed in pass 1"
                )
            info         = sell_info[i]
            pool_to_draw = info["pool_units"]
            pre_cost     = info["pre_pool_cost"]
            proceeds     = info["proceeds"]
            notes        = list(info["notes"])

            allowable = pre_cost

            if pool_to_draw > 1e-9:
                if pool_units < pool_to_draw - 1e-9:
                    raise ValueError(
                        f"[{rec[DATE_LABEL]}] Cannot draw {pool_to_draw:.4f} sh from "
                        f"Section 104 pool: only {pool_units:.4f} sh available. "
                        "Check that all acquisitions are present and in date order."
                    )
                pool_fraction  = pool_to_draw / pool_units
                pool_allowable = pool_cost * pool_fraction
                allowable     += pool_allowable
                pool_units    -= pool_to_draw
                pool_cost     -= pool_allowable
                notes.append(f"Section 104 ({pool_to_draw:.0f} sh)")

            if pool_units < -1e-9:
                raise ValueError(f"[{rec[DATE_LABEL]}] Pool units went negative: {pool_units:.4f}")

            gains.append(proceeds - allowable)
            pool_costs_out.append(pool_cost)
            pool_units_out.append(pool_units)
            matching_notes.append(", ".join(notes) if notes else "Section 104")

    return gains, pool_costs_out, matching_notes, pool_units_out


# ---------- Tax-year summary ----------
def _tax_year_start(dt: datetime) -> int:
    """Calendar year in which the UK tax year containing `dt` begins (6 April)."""
    if dt.month > 4 or (dt.month == 4 and dt.day >= 6):
        return dt.year
    return dt.year - 1

def _tax_year_label(dt: datetime) -> str:
    y = _tax_year_start(dt)
    return f"{y}/{(y + 1) % 100:02d}"


def _taxable_disposals(events: pd.DataFrame) -> pd.DataFrame:
    """Chargeable disposals: every Sell plus any WithholdingSell with a gain/loss."""
    is_sell       = events[TYPE_LABEL] == SELL_TYPE
    is_taxable_ws = ((events[TYPE_LABEL] == WITHHOLDING_SELL_TYPE) &
                     (events[GAINS_LABEL].abs() > 1e-9))
    return events[is_sell | is_taxable_ws].copy()


def capital_loss_claims(events: pd.DataFrame, today: datetime = None) -> list[dict]:
    """
    For each UK tax year with a net allowable loss, return the HMRC notification
    deadline and whether it has passed.

    A capital loss must be notified to HMRC within four years of the end of the
    tax year in which it arose (TMA 1970 s.43).  The tax year 20XX/YY ends on
    5 April 20YY, so the deadline is 5 April four years later — i.e. 5 April of
    (start year + 5).
    """
    if events.empty:
        return []
    if today is None:
        today = datetime.today()

    taxable = _taxable_disposals(events)
    if taxable.empty:
        return []

    taxable["_tystart"] = taxable[DATE_DT].apply(_tax_year_start)
    net_by_year = taxable.groupby("_tystart")[GAINS_LABEL].sum()

    claims = []
    for start_year, net in net_by_year.items():
        if net < -1e-9:  # net loss for the year
            start_year = int(start_year)
            deadline = datetime(start_year + 5, 4, 5)
            claims.append({
                "tax_year": f"{start_year}/{(start_year + 1) % 100:02d}",
                "net_loss": float(net),               # negative
                "deadline": deadline,
                "passed":   today > deadline,
            })
    return claims

def print_tax_year_summary(events: pd.DataFrame) -> None:
    if events.empty:
        return

    # Taxable events are chargeable disposals:
    #   * every genuine Sell, and
    #   * a WithholdingSell only when the broker's sale price differed from the
    #     market value at release, so it realised a (small) gain or loss.  A
    #     WithholdingSell sold/net-settled at market value has gain = 0 and is
    #     not a taxable event.
    taxable = _taxable_disposals(events)

    # Show every UK tax year spanned by the data (continuous, no gaps), so a year
    # with no taxable events still appears as a zero row.
    start = _tax_year_start(events[DATE_DT].min())
    end   = _tax_year_start(events[DATE_DT].max())
    all_years = [f"{y}/{(y + 1) % 100:02d}" for y in range(start, end + 1)]

    taxable["_ty"] = taxable[DATE_DT].apply(_tax_year_label)
    summary = taxable.groupby("_ty")[GAINS_LABEL].agg(
        total_gain="sum",
        n_events="count",
    )

    print("\n=== Capital Gains / Losses by UK Tax Year ===", file=sys.stderr)
    print(f"  {'Tax year':<12}  {'Taxable events':>14}  {'Net gain/loss (GBP)':>22}", file=sys.stderr)
    print(f"  {'-'*12}  {'-'*14}  {'-'*22}", file=sys.stderr)
    for ty in all_years:
        if ty in summary.index:
            gain = summary.loc[ty, "total_gain"]
            n    = int(summary.loc[ty, "n_events"])
        else:
            gain, n = 0.0, 0
        print(f"  {ty:<12}  {n:>14}  £{gain:>21,.2f}", file=sys.stderr)
    print("  (Annual exempt amount and prior-year losses not applied)", file=sys.stderr)
    print(f"  {FX_PROVENANCE_NOTE}", file=sys.stderr)

    claims = capital_loss_claims(events)
    if claims:
        print("\n  --- Capital losses: notify HMRC within 4 years to use them ---",
              file=sys.stderr)
        for c in claims:
            if c["passed"]:
                status = f"DEADLINE PASSED ({c['deadline']:%d %b %Y})"
            else:
                status = f"claim by {c['deadline']:%d %b %Y}"
            print(f"  {c['tax_year']:<12}  loss £{-c['net_loss']:>14,.2f}  {status}",
                  file=sys.stderr)
    print("", file=sys.stderr)


# ---------- Output ----------
def print_events(events: pd.DataFrame) -> None:
    output_cols = [
        TYPE_LABEL, DATE_LABEL, GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL,
        PRICE_PER_SHARE_USD_LABEL, SALE_PRICE_PER_SHARE_USD_LABEL,
        GBP_USD_LABEL, PRICE_PER_SHARE_GBP_LABEL, SALE_PRICE_PER_SHARE_GBP_LABEL,
        HOLDINGS_GBP_LABEL, GAINS_LABEL, MATCHING_LABEL,
    ]
    out = events.copy()
    for c in (PRICE_PER_SHARE_USD_LABEL, SALE_PRICE_PER_SHARE_USD_LABEL,
              GBP_USD_LABEL, PRICE_PER_SHARE_GBP_LABEL, SALE_PRICE_PER_SHARE_GBP_LABEL,
              GAINS_LABEL, HOLDINGS_GBP_LABEL):
        out[c] = out[c].apply(lambda x: f"{x:.4f}" if pd.notnull(x) else "")
    out.to_csv(sys.stdout, index=False, columns=output_cols, float_format="%.0f")


# ---------- Event assembly (shared by CLI and GUI) ----------
# Column set every event row carries before matching.  Buys, Sells and
# WithholdingSells are all normalised to this shape so they can be concatenated.
_EVENT_COLS = [
    TYPE_LABEL, DATE_LABEL, DATE_DT,
    GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL,
    PRICE_PER_SHARE_USD_LABEL, SALE_PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL,
]


def _normalise_releases(releases: pd.DataFrame) -> pd.DataFrame:
    """Coerce a releases table (CLI CSV or GUI DataFrame) into Buy rows."""
    rel = releases.copy()
    if DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[DATE_LABEL].astype(str).str.replace("/", "-")
    elif RELEASE_DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[RELEASE_DATE_LABEL].astype(str).str.replace("/", "-")
    else:
        raise ValueError("Releases must include a 'Date' or 'Release Date' column.")

    for col in (GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL):
        if col not in rel.columns:
            raise ValueError(f"Releases missing required column: {col}")

    rel[DATE_DT] = rel[DATE_LABEL].apply(parse_date_ymd)

    # Sale price per share is present only for releases where the broker sold the
    # withheld shares on the market (rather than net-settling at market value).
    if SALE_PRICE_PER_SHARE_USD_LABEL not in rel.columns:
        rel[SALE_PRICE_PER_SHARE_USD_LABEL] = float("nan")

    rel[TYPE_LABEL] = BUY_TYPE
    return rel


def _normalise_exrates(exrates: pd.DataFrame) -> pd.DataFrame:
    """Ensure the exchange-rate table carries parsed Start_dt / End_dt columns."""
    xr = exrates.copy()
    if "Start_dt" not in xr.columns:
        xr["Start_dt"] = xr[START_DATE_LABEL].apply(parse_date_dmy)
    if "End_dt" not in xr.columns:
        xr["End_dt"] = xr[END_DATE_LABEL].apply(parse_date_dmy)
    return xr


def _withholding_sell_rows(rel: pd.DataFrame) -> list[dict]:
    """One WithholdingSell row per release with shares withheld to cover tax."""
    ws_rows = []
    for _, row in rel.iterrows():
        withheld = require_float(row[SOLD_LABEL], SOLD_LABEL, row[DATE_LABEL])
        if withheld <= 0:
            continue
        # Leave the sale price absent (NaN) for net-settled releases rather than
        # back-filling it with the market value.  The gain calculation already
        # treats a missing sale price as a zero-gain net-settlement, and keeping
        # it absent lets _is_sell_to_cover tell the two methods apart for display.
        ws_rows.append({
            TYPE_LABEL:                     WITHHOLDING_SELL_TYPE,
            DATE_LABEL:                     row[DATE_LABEL],
            DATE_DT:                        row[DATE_DT],
            GRANTED_LABEL:                  0.0,
            SOLD_LABEL:                     withheld,
            ISSUED_LABEL:                   0.0,
            PRICE_PER_SHARE_USD_LABEL:      row[PRICE_PER_SHARE_USD_LABEL],
            SALE_PRICE_PER_SHARE_USD_LABEL: row[SALE_PRICE_PER_SHARE_USD_LABEL],
            GBP_USD_LABEL:                  row[GBP_USD_LABEL],
        })
    return ws_rows


def build_events(releases: pd.DataFrame,
                 sales: "pd.DataFrame | None",
                 exrates: pd.DataFrame) -> pd.DataFrame:
    """
    Assemble the full event timeline and run the HS284 matching, returning a
    DataFrame with one row per Buy / Sell / WithholdingSell and every derived
    column (GBP prices, gains, Section 104 holdings, owned shares, average cost
    and matching notes).

    This is the single source of truth shared by the CLI (`main`) and the
    Streamlit GUI, so the two cannot drift apart.

    `sales` is an already-normalised table with at least Date, Sold and
    Price per share ($) columns; an optional `Type` column may mark rows as
    `Buy` (a generic acquisition that enters the pool) — anything else is a Sell.
    """
    xr = _normalise_exrates(exrates)

    rel = _normalise_releases(releases)
    rel[GBP_USD_LABEL] = attach_rate(rel[DATE_DT], xr)
    rel = rel[_EVENT_COLS]

    ws_rows = _withholding_sell_rows(rel)

    events_parts = [rel]

    if sales is not None and not sales.empty:
        s = sales.copy()
        s[DATE_LABEL] = s[DATE_LABEL].astype(str).str.replace("/", "-")
        s[DATE_DT]    = s[DATE_LABEL].apply(parse_date_ymd)
        s[GBP_USD_LABEL] = attach_rate(s[DATE_DT], xr)
        if TYPE_LABEL not in s.columns:
            s[TYPE_LABEL] = SELL_TYPE
        else:
            s[TYPE_LABEL] = s[TYPE_LABEL].fillna(SELL_TYPE)
        # A generic Buy acquisition (e.g. ESPP / open-market purchase) enters the
        # pool as Issued shares at its own cost basis; a Sell issues nothing and
        # disposes of the quantity.  The input carries the quantity in SOLD_LABEL
        # for both, so route it to the right column per row type.
        is_buy = s[TYPE_LABEL] == BUY_TYPE
        qty = s[SOLD_LABEL]
        s[GRANTED_LABEL] = qty.where(is_buy, 0.0)
        s[ISSUED_LABEL]  = qty.where(is_buy, 0.0)
        s[SOLD_LABEL]    = qty.where(~is_buy, 0.0)
        if SALE_PRICE_PER_SHARE_USD_LABEL not in s.columns:
            s[SALE_PRICE_PER_SHARE_USD_LABEL] = float("nan")
        s = s[_EVENT_COLS]
        events_parts.append(s)

    if ws_rows:
        events_parts.append(pd.DataFrame(ws_rows))

    events = pd.concat(events_parts, ignore_index=True)

    # Sort: date first; within a date, Buys before WithholdingSells before Sells
    # so same-day matching sees the acquisition before any disposal.
    type_order = {BUY_TYPE: 0, WITHHOLDING_SELL_TYPE: 1, SELL_TYPE: 2}
    events["_sort_type"] = events[TYPE_LABEL].map(type_order).fillna(9)
    events = (events
              .sort_values([DATE_DT, "_sort_type"], kind="stable")
              .drop(columns=["_sort_type"])
              .reset_index(drop=True))

    # GBP prices
    events[PRICE_PER_SHARE_GBP_LABEL] = (
        events[PRICE_PER_SHARE_USD_LABEL] / events[GBP_USD_LABEL]
    )
    events[SALE_PRICE_PER_SHARE_GBP_LABEL] = (
        events[SALE_PRICE_PER_SHARE_USD_LABEL] / events[GBP_USD_LABEL]
    )

    # HMRC matching + Section 104 pool.  The matching runs at full floating-point
    # precision; only the *reported* monetary figures are quantised to pennies
    # (2 dp GBP).  Keeping the internal pool unrounded means rounding never
    # accumulates from one disposal to the next.
    gains, holdings, matching_notes, owned = get_gains_and_holdings(events)
    holdings_series = pd.Series(holdings, index=events.index)
    owned_series    = pd.Series(owned, index=events.index)

    events[GAINS_LABEL]        = pd.Series(gains, index=events.index).round(2)
    events[HOLDINGS_GBP_LABEL] = holdings_series.round(2)
    events[OWNED_SHARES_LABEL] = owned
    events[MATCHING_LABEL]     = matching_notes

    # Section 104 weighted-average cost per share, from the unrounded pool cost.
    events[AVG_COST_GBP_LABEL] = holdings_series.where(
        owned_series > 1e-9
    ) / owned_series.where(owned_series > 1e-9)

    return events


# ---------- Main ----------
def main(argv):
    p = argparse.ArgumentParser(description="Calculate combined cost basis with optional sales.")
    p.add_argument("--releases", "-r", required=True,
                   help="Stock releases CSV (from parse-stock-releases.py)")
    p.add_argument("--exrates",  "-x", required=True,
                   help="Exchange rates CSV (Start/End Date in DD/MM/YYYY)")
    p.add_argument("--sales",    "-s", help="Sales CSV (optional)")
    args = p.parse_args(argv[1:])

    rel   = pd.read_csv(args.releases)
    xr    = pd.read_csv(args.exrates)
    sales = load_sales(args.sales) if args.sales else None

    events = build_events(rel, sales, xr)

    print_events(events)
    print_tax_year_summary(events)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
