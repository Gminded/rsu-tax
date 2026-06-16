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

import sys, argparse
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

# ---------- Date parsing ----------
def parse_date_ymd(s: str) -> datetime:
    s = str(s).strip().replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d")

def parse_date_dmy(s: str) -> datetime:
    return datetime.strptime(str(s).strip(), "%d/%m/%Y")

# ---------- Exchange rate lookup (inclusive ranges) ----------
def attach_rate(df_dates: pd.Series, exrates_df: pd.DataFrame) -> pd.Series:
    def find_rate(d):
        for _, r in exrates_df.iterrows():
            if r["Start_dt"] <= d <= r["End_dt"]:
                return r["Currency units per £1"]
        return None
    return df_dates.apply(find_rate)

# ---------- Sales CSV helpers ----------
CANDIDATE_DATE   = ["date", "sale date", "transaction date"]
CANDIDATE_SHARES = ["shares", "quantity", "units", "issued", "shares sold", "qty"]
CANDIDATE_PRICE  = ["price per share ($)", "priceusd", "price", "sale price", "sale price ($)"]

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

def load_sales(sales_csv: str) -> pd.DataFrame:
    s = pd.read_csv(sales_csv)
    cols = list(s.columns)
    date_col   = _find_col(cols, CANDIDATE_DATE)
    shares_col = _find_col(cols, CANDIDATE_SHARES)
    price_col  = _find_col(cols, CANDIDATE_PRICE)
    if not date_col or not shares_col or not price_col:
        raise ValueError(
            f"sales.csv missing recognizable columns. "
            f"Found: {cols}. Need Date~{CANDIDATE_DATE}, "
            f"Shares~{CANDIDATE_SHARES}, Price~{CANDIDATE_PRICE}"
        )
    return pd.DataFrame({
        DATE_LABEL:                s[date_col].astype(str).str.replace("/", "-"),
        SOLD_LABEL:                s[shares_col].astype(float),
        PRICE_PER_SHARE_USD_LABEL: s[price_col].astype(float),
    })


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
        sell_units = float(rec[SOLD_LABEL] or 0.0)
        price_gbp  = float(rec[PRICE_PER_SHARE_GBP_LABEL] or 0.0)
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
            avail   = float(buy[ISSUED_LABEL] or 0.0) - buy_consumed[j]
            matched = min(remaining, avail)
            if matched <= 0:
                continue
            buy_consumed[j] += matched
            remaining        -= matched
            matched_cost     += matched * float(buy[PRICE_PER_SHARE_GBP_LABEL] or 0.0)
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
                avail   = float(buy[ISSUED_LABEL] or 0.0) - buy_consumed[j]
                matched = min(remaining, avail)
                if matched <= 0:
                    continue
                buy_consumed[j] += matched
                remaining        -= matched
                matched_cost     += matched * float(buy[PRICE_PER_SHARE_GBP_LABEL] or 0.0)
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
        price_gbp = float(rec[PRICE_PER_SHARE_GBP_LABEL] or 0.0)

        if typ == BUY_TYPE:
            issued     = float(rec[ISSUED_LABEL] or 0.0)
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
            # These shares were never in the pool; gain is zero by design
            # (cost = proceeds = market value on release date under same-day rule).
            gains.append(0.0)
            pool_costs_out.append(pool_cost)
            pool_units_out.append(pool_units)
            matching_notes.append("same-day rule (tax withholding)")

        else:  # SELL_TYPE
            info         = sell_info.get(i, {})
            pool_to_draw = info.get("pool_units", float(rec[SOLD_LABEL] or 0.0))
            pre_cost     = info.get("pre_pool_cost", 0.0)
            proceeds     = info.get("proceeds", float(rec[SOLD_LABEL] or 0.0) * price_gbp)
            notes        = list(info.get("notes", []))

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
def _tax_year_label(dt: datetime) -> str:
    if dt.month > 4 or (dt.month == 4 and dt.day >= 6):
        return f"{dt.year}/{(dt.year + 1) % 100:02d}"
    return f"{dt.year - 1}/{dt.year % 100:02d}"

def print_tax_year_summary(events: pd.DataFrame) -> None:
    disposals = events[events[TYPE_LABEL].isin([SELL_TYPE, WITHHOLDING_SELL_TYPE])].copy()
    if disposals.empty:
        return

    disposals["_ty"] = disposals[DATE_DT].apply(_tax_year_label)
    summary = disposals.groupby("_ty")[GAINS_LABEL].agg(
        total_gain="sum",
        n_disposals="count",
    )

    print("\n=== Capital Gains / Losses by UK Tax Year ===", file=sys.stderr)
    print(f"  {'Tax year':<12}  {'Disposals':>10}  {'Net gain/loss (GBP)':>22}", file=sys.stderr)
    print(f"  {'-'*12}  {'-'*10}  {'-'*22}", file=sys.stderr)
    grand_total = 0.0
    for ty, row in sorted(summary.iterrows()):
        gain = row["total_gain"]
        grand_total += gain
        print(f"  {ty:<12}  {int(row['n_disposals']):>10}  £{gain:>21,.2f}", file=sys.stderr)
    print(f"  {'TOTAL':<12}  {'':>10}  £{grand_total:>21,.2f}", file=sys.stderr)
    print("  (Annual exempt amount and prior-year losses not applied)", file=sys.stderr)
    print("", file=sys.stderr)


# ---------- Output ----------
def print_events(events: pd.DataFrame) -> None:
    output_cols = [
        TYPE_LABEL, DATE_LABEL, GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL,
        PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL, PRICE_PER_SHARE_GBP_LABEL,
        HOLDINGS_GBP_LABEL, GAINS_LABEL, MATCHING_LABEL,
    ]
    out = events.copy()
    for c in (PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL,
              PRICE_PER_SHARE_GBP_LABEL, GAINS_LABEL, HOLDINGS_GBP_LABEL):
        out[c] = out[c].apply(lambda x: f"{x:.4f}" if pd.notnull(x) else "")
    out.to_csv(sys.stdout, index=False, columns=output_cols, float_format="%.0f")


# ---------- Main ----------
def main(argv):
    p = argparse.ArgumentParser(description="Calculate combined cost basis with optional sales.")
    p.add_argument("--releases", "-r", required=True,
                   help="Stock releases CSV (from parse-stock-releases.py)")
    p.add_argument("--exrates",  "-x", required=True,
                   help="Exchange rates CSV (Start/End Date in DD/MM/YYYY)")
    p.add_argument("--sales",    "-s", help="Sales CSV (optional)")
    args = p.parse_args(argv[1:])

    # --- Load releases ---
    rel = pd.read_csv(args.releases)

    if DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[DATE_LABEL].astype(str).str.replace("/", "-")
    elif RELEASE_DATE_LABEL in rel.columns:
        rel[DATE_LABEL] = rel[RELEASE_DATE_LABEL].astype(str).str.replace("/", "-")
    else:
        raise ValueError("Releases CSV must include 'Date' or 'Release Date' column.")

    required_rel = [GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL, PRICE_PER_SHARE_USD_LABEL]
    for col in required_rel:
        if col not in rel.columns:
            raise ValueError(f"Releases CSV missing required column: {col}")

    rel[DATE_DT] = rel[DATE_LABEL].apply(parse_date_ymd)

    # --- Load exchange rates ---
    xr = pd.read_csv(args.exrates)
    xr["Start_dt"] = xr[START_DATE_LABEL].apply(parse_date_dmy)
    xr["End_dt"]   = xr[END_DATE_LABEL].apply(parse_date_dmy)

    rel[GBP_USD_LABEL] = attach_rate(rel[DATE_DT], xr)
    rel[TYPE_LABEL]    = BUY_TYPE
    rel = rel[[TYPE_LABEL, DATE_LABEL, DATE_DT,
               GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL,
               PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL]]

    # --- Withholding-sell rows ---
    # When RSUs vest, the broker sells some shares to cover income tax on your behalf.
    # These are CGT disposals (gain = 0 since cost = proceeds = market value on same day)
    # and should be visible in the output for HMRC reporting purposes.
    ws_rows = []
    for _, row in rel.iterrows():
        withheld = float(row[SOLD_LABEL] or 0.0)
        if withheld > 0:
            ws_rows.append({
                TYPE_LABEL:                WITHHOLDING_SELL_TYPE,
                DATE_LABEL:                row[DATE_LABEL],
                DATE_DT:                   row[DATE_DT],
                GRANTED_LABEL:             0.0,
                SOLD_LABEL:                withheld,
                ISSUED_LABEL:              0.0,
                PRICE_PER_SHARE_USD_LABEL: row[PRICE_PER_SHARE_USD_LABEL],
                GBP_USD_LABEL:             row[GBP_USD_LABEL],
            })

    # --- Sales (optional) ---
    if args.sales:
        s = load_sales(args.sales)
        s[DATE_DT]      = s[DATE_LABEL].apply(parse_date_ymd)
        s[GBP_USD_LABEL] = attach_rate(s[DATE_DT], xr)
        s[GRANTED_LABEL] = 0.0
        s[ISSUED_LABEL]  = 0.0
        s[TYPE_LABEL]    = SELL_TYPE
        s = s[[TYPE_LABEL, DATE_LABEL, DATE_DT,
               GRANTED_LABEL, SOLD_LABEL, ISSUED_LABEL,
               PRICE_PER_SHARE_USD_LABEL, GBP_USD_LABEL]]
        events_parts = [rel, s]
    else:
        events_parts = [rel]

    if ws_rows:
        events_parts.append(pd.DataFrame(ws_rows))

    # --- Combine & sort (stable sort preserves same-day order: Buy before Sell) ---
    events = pd.concat(events_parts, ignore_index=True)
    # Sort: date first; within same date, Buys before Sells/WithholdingSells so that
    # same-day matching correctly sees the acquisition before any disposal.
    type_order = {BUY_TYPE: 0, WITHHOLDING_SELL_TYPE: 1, SELL_TYPE: 2}
    events["_sort_type"] = events[TYPE_LABEL].map(type_order).fillna(9)
    events = (events
              .sort_values([DATE_DT, "_sort_type"], kind="stable")
              .drop(columns=["_sort_type"])
              .reset_index(drop=True))

    # --- GBP prices ---
    events[PRICE_PER_SHARE_GBP_LABEL] = (
        events[PRICE_PER_SHARE_USD_LABEL] / events[GBP_USD_LABEL]
    )

    # --- HMRC matching + pool calculation ---
    gains, holdings, matching_notes, _ = get_gains_and_holdings(events)
    events[GAINS_LABEL]   = gains
    events[HOLDINGS_GBP_LABEL] = holdings
    events[MATCHING_LABEL]     = matching_notes

    print_events(events)
    print_tax_year_summary(events)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
