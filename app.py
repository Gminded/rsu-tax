import importlib.util
import io
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Module loading ──────────────────────────────────────────────────────────────
_BIN = Path(__file__).parent / "bin"
sys.path.insert(0, str(_BIN))

from parse_pdf import parse_pdf      # noqa: E402
from combine import get_usd_row      # noqa: E402

def _load_module(filename: str):
    path = _BIN / filename
    name = filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ccb = _load_module("calculate-cost-basis.py")

# ── Paths ───────────────────────────────────────────────────────────────────────
_ROOT      = Path(__file__).parent
_EXRATES_DIR = _ROOT / "monthly-exchange-rates-by-hmrc"
_SALES_CSV   = _ROOT / "sales" / "sales.csv"

_RELEASE_COLS = ["Release Date", "Granted", "Sold", "Issued", "Price per share ($)"]
_SALES_COLS   = ["Date", "Shares", "Price per share ($)"]
_XR_FIELDS    = ["Country/Territories", "Currency", "Currency code",
                 "Currency units per £1", "Start Date", "End Date"]

# ── Page config ─────────────────────────────────────────────────────────────────
st.set_page_config(page_title="HMRC Capital Gains", layout="wide")
st.title("HMRC Capital Gains Calculator")
st.caption(
    "Calculates RSU gains and losses for UK Self Assessment, implementing the "
    "HS284 share-identification rules: same-day → 30-day → Section 104 pool."
)

# ── Session-state defaults ───────────────────────────────────────────────────────
if "releases_df" not in st.session_state:
    st.session_state.releases_df = pd.DataFrame(columns=_RELEASE_COLS)
if "parsed_pdf_keys" not in st.session_state:
    st.session_state.parsed_pdf_keys = set()
if "results" not in st.session_state:
    st.session_state.results = None

if "sales_df" not in st.session_state:
    if _SALES_CSV.exists():
        raw = pd.read_csv(_SALES_CSV)
        numeric_shares = pd.to_numeric(raw.get("Shares", pd.Series(dtype=float)), errors="coerce")
        valid = raw[numeric_shares.fillna(0) > 0].reset_index(drop=True)
        st.session_state.sales_df = valid if not valid.empty else pd.DataFrame(columns=_SALES_COLS)
    else:
        st.session_state.sales_df = pd.DataFrame(columns=_SALES_COLS)


# ── Helpers ──────────────────────────────────────────────────────────────────────

@st.cache_data
def _load_default_exrates() -> pd.DataFrame | None:
    """Load and combine all USD rows from the bundled HMRC monthly rate CSVs."""
    if not _EXRATES_DIR.exists():
        return None
    rows = []
    for p in sorted(_EXRATES_DIR.glob("*.csv")):
        row = get_usd_row(p)
        if row:
            rows.append(dict(zip(_XR_FIELDS, row)))
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Currency units per £1"] = pd.to_numeric(df["Currency units per £1"], errors="coerce")
    return df


def _tax_year_summary(events: pd.DataFrame) -> pd.DataFrame:
    disposals = events[
        events[ccb.TYPE_LABEL].isin([ccb.SELL_TYPE, ccb.WITHHOLDING_SELL_TYPE])
    ].copy()
    if disposals.empty:
        return pd.DataFrame(columns=["Tax year", "Disposals", "Net gain / loss (£)"])
    disposals["_ty"] = disposals[ccb.DATE_DT].apply(ccb._tax_year_label)
    summary = (
        disposals.groupby("_ty")[ccb.GAINS_LABEL]
        .agg(Disposals="count", **{"Net gain / loss (£)": "sum"})
        .reset_index()
        .rename(columns={"_ty": "Tax year"})
        .sort_values("Tax year")
    )
    total = pd.DataFrame([{
        "Tax year": "TOTAL",
        "Disposals": int(summary["Disposals"].sum()),
        "Net gain / loss (£)": summary["Net gain / loss (£)"].sum(),
    }])
    return pd.concat([summary, total], ignore_index=True)


def _colour_gain(val):
    if not isinstance(val, (int, float)):
        return ""
    if val < 0:
        return "color: #c0392b"
    if val > 0:
        return "color: #27ae60"
    return ""


def _run_calculation(releases_df, sales_df, exrates_df) -> pd.DataFrame:
    """Replicate calculate-cost-basis main() using DataFrames already in memory."""
    rel = releases_df.copy()

    if "Release Date" in rel.columns:
        rel[ccb.DATE_LABEL] = rel["Release Date"].astype(str).str.replace("/", "-")
    elif ccb.DATE_LABEL in rel.columns:
        rel[ccb.DATE_LABEL] = rel[ccb.DATE_LABEL].astype(str).str.replace("/", "-")
    else:
        raise ValueError("Releases table must have a 'Release Date' column.")

    rel[ccb.DATE_DT] = rel[ccb.DATE_LABEL].apply(ccb.parse_date_ymd)

    xr = exrates_df.copy()
    xr["Start_dt"] = xr["Start Date"].apply(ccb.parse_date_dmy)
    xr["End_dt"]   = xr["End Date"].apply(ccb.parse_date_dmy)

    rel[ccb.GBP_USD_LABEL] = ccb.attach_rate(rel[ccb.DATE_DT], xr)
    rel[ccb.TYPE_LABEL]    = ccb.BUY_TYPE

    _ev_cols = [
        ccb.TYPE_LABEL, ccb.DATE_LABEL, ccb.DATE_DT,
        ccb.GRANTED_LABEL, ccb.SOLD_LABEL, ccb.ISSUED_LABEL,
        ccb.PRICE_PER_SHARE_USD_LABEL, ccb.GBP_USD_LABEL,
    ]
    rel = rel[_ev_cols]

    # WithholdingSell rows — broker selling shares to cover income-tax on vests
    ws_rows = []
    for _, row in rel.iterrows():
        withheld = float(row[ccb.SOLD_LABEL] or 0.0)
        if withheld > 0:
            ws_rows.append({
                ccb.TYPE_LABEL:                ccb.WITHHOLDING_SELL_TYPE,
                ccb.DATE_LABEL:                row[ccb.DATE_LABEL],
                ccb.DATE_DT:                   row[ccb.DATE_DT],
                ccb.GRANTED_LABEL:             0.0,
                ccb.SOLD_LABEL:                withheld,
                ccb.ISSUED_LABEL:              0.0,
                ccb.PRICE_PER_SHARE_USD_LABEL: row[ccb.PRICE_PER_SHARE_USD_LABEL],
                ccb.GBP_USD_LABEL:             row[ccb.GBP_USD_LABEL],
            })

    events_parts = [rel]

    valid_sales = sales_df.dropna(how="all")
    numeric_shares = pd.to_numeric(valid_sales.get("Shares", pd.Series(dtype=float)), errors="coerce")
    valid_sales = valid_sales[numeric_shares.fillna(0) > 0]
    if not valid_sales.empty:
        s = pd.DataFrame({
            ccb.DATE_LABEL:                valid_sales["Date"].astype(str).str.replace("/", "-"),
            ccb.SOLD_LABEL:                pd.to_numeric(valid_sales["Shares"]),
            ccb.PRICE_PER_SHARE_USD_LABEL: pd.to_numeric(valid_sales["Price per share ($)"]),
        })
        s[ccb.DATE_DT]       = s[ccb.DATE_LABEL].apply(ccb.parse_date_ymd)
        s[ccb.GBP_USD_LABEL] = ccb.attach_rate(s[ccb.DATE_DT], xr)
        s[ccb.GRANTED_LABEL] = 0.0
        s[ccb.ISSUED_LABEL]  = 0.0
        s[ccb.TYPE_LABEL]    = ccb.SELL_TYPE
        s = s[_ev_cols]
        events_parts.append(s)

    if ws_rows:
        events_parts.append(pd.DataFrame(ws_rows))

    events = pd.concat(events_parts, ignore_index=True)
    type_order = {ccb.BUY_TYPE: 0, ccb.WITHHOLDING_SELL_TYPE: 1, ccb.SELL_TYPE: 2}
    events["_sort_type"] = events[ccb.TYPE_LABEL].map(type_order).fillna(9)
    events = (
        events
        .sort_values([ccb.DATE_DT, "_sort_type"], kind="stable")
        .drop(columns=["_sort_type"])
        .reset_index(drop=True)
    )

    events[ccb.PRICE_PER_SHARE_GBP_LABEL] = (
        events[ccb.PRICE_PER_SHARE_USD_LABEL] / events[ccb.GBP_USD_LABEL]
    )

    gains, holdings, matching_notes = ccb.get_gains_and_holdings(events)
    events[ccb.GAINS_LABEL]        = gains
    events[ccb.HOLDINGS_GBP_LABEL] = holdings
    events[ccb.MATCHING_LABEL]     = matching_notes

    return events


# ════════════════════════════════════════════════════════════════════════════════
# Input sections
# ════════════════════════════════════════════════════════════════════════════════

# Initialise with the committed base; reassigned by each data_editor below.
# We do NOT sync editor outputs back to session_state on every rerun — that
# would change the base DataFrame, resetting the editor's delta state and
# causing in-progress edits to vanish after each Tab/Enter keystroke.
edited_releases: pd.DataFrame = st.session_state.releases_df
edited_sales: pd.DataFrame    = st.session_state.sales_df

releases_count = len(st.session_state.releases_df)
with st.expander(
    f"RSU Releases — {releases_count} loaded" if releases_count else "RSU Releases",
    expanded=(releases_count == 0),
):
    uploaded_pdfs = st.file_uploader(
        "Upload e*trade release confirmation PDFs",
        type="pdf",
        accept_multiple_files=True,
        help=(
            "e*trade → At Work → My Account → Benefit History → "
            "Restricted Stock (RS) → View Confirmation of Release"
        ),
    )

    if uploaded_pdfs:
        current_keys = {(f.name, f.size) for f in uploaded_pdfs}
        new_keys = current_keys - st.session_state.parsed_pdf_keys
        if new_keys:
            new_files = [f for f in uploaded_pdfs if (f.name, f.size) in new_keys]
            new_rows, errors = [], {}
            for uf in new_files:
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(uf.read())
                        tmp_path = Path(tmp.name)
                    result = parse_pdf(tmp_path)
                    new_rows.append({k: result.get(k) for k in _RELEASE_COLS})
                except Exception as exc:
                    errors[uf.name] = str(exc)
                finally:
                    if tmp_path and tmp_path.exists():
                        tmp_path.unlink()

            for fname, msg in errors.items():
                st.error(f"**{fname}**: {msg}")

            if new_rows:
                new_df = (
                    pd.DataFrame(new_rows)
                    .sort_values("Release Date")
                    .reset_index(drop=True)
                )
                combined = (
                    pd.concat([st.session_state.releases_df, new_df])
                    .drop_duplicates(subset=["Release Date", "Granted"], keep="last")
                    .sort_values("Release Date")
                    .reset_index(drop=True)
                )
                st.session_state.releases_df = combined
                st.session_state.results = None  # invalidate previous results

            st.session_state.parsed_pdf_keys = current_keys

    if not st.session_state.releases_df.empty:
        edited_releases = st.data_editor(
            st.session_state.releases_df,
            key="releases_editor",
            num_rows="dynamic",
            width="stretch",
            column_config={
                "Release Date": st.column_config.TextColumn(
                    "Release Date", help="YYYY-MM-DD, e.g. 2024-04-01"
                ),
                "Granted": st.column_config.NumberColumn(
                    "Granted", step=1, min_value=0,
                    help="Total shares granted (before withholding)"
                ),
                "Sold": st.column_config.NumberColumn(
                    "Withheld", step=1, min_value=0,
                    help="Shares sold by the broker to cover income tax on vest"
                ),
                "Issued": st.column_config.NumberColumn(
                    "Issued", step=1, min_value=0,
                    help="Shares actually issued to you (Granted minus Withheld)"
                ),
                "Price per share ($)": st.column_config.NumberColumn(
                    "Price / share ($)", format="$%.4f", step=0.0001, min_value=0.0,
                    help="Market value per share on the release date in USD, e.g. 12.3456"
                ),
            },
        )

        if st.button("Clear all releases", type="secondary"):
            st.session_state.releases_df = pd.DataFrame(columns=_RELEASE_COLS)
            st.session_state.parsed_pdf_keys = set()
            st.session_state.results = None
            st.session_state.pop("releases_editor", None)
            st.rerun()
    else:
        st.info("Upload one or more PDF release confirmation files to get started.")


with st.expander("Sales", expanded=False):
    st.caption(
        "Voluntary sell transactions (not tax-withholding). "
        "One row per sale — date in YYYY-MM-DD, price in USD."
    )
    edited_sales = st.data_editor(
        st.session_state.sales_df,
        key="sales_editor",
        num_rows="dynamic",
        width="stretch",
        column_config={
            "Date": st.column_config.TextColumn(
                "Date",
                help="Date of sale in YYYY-MM-DD format, e.g. 2024-06-01",
                default="YYYY-MM-DD",
            ),
            "Shares": st.column_config.NumberColumn(
                "Shares",
                help="Number of shares sold, e.g. 100",
                min_value=0.0,
                step=1.0,
                default=0.0,
            ),
            "Price per share ($)": st.column_config.NumberColumn(
                "Price / share ($)",
                help="Sale price per share in USD, e.g. 12.3456",
                format="$%.4f",
                step=0.0001,
                min_value=0.0,
                default=0.0,
            ),
        },
    )


default_xr = _load_default_exrates()
extra_xr: pd.DataFrame | None = None

with st.expander("Exchange Rates", expanded=(default_xr is None)):
    if default_xr is not None:
        try:
            all_dates = pd.concat([
                pd.to_datetime(default_xr["Start Date"], dayfirst=True),
                pd.to_datetime(default_xr["End Date"],   dayfirst=True),
            ])
            from_label = all_dates.min().strftime("%b %Y")
            to_label   = all_dates.max().strftime("%b %Y")
            st.success(
                f"Included HMRC rates cover **{from_label} – {to_label}** "
                f"({len(default_xr)} months)."
            )
        except Exception:
            st.success(f"Loaded {len(default_xr)} monthly rate(s) from included dataset.")
    else:
        st.warning(
            "No built-in exchange rates found. "
            "Upload HMRC monthly exchange rate CSVs below."
        )

    extra_files = st.file_uploader(
        "Upload additional HMRC monthly exchange rate CSVs (optional)",
        type="csv",
        accept_multiple_files=True,
        help="Download from https://www.trade-tariff.service.gov.uk/exchange_rates",
    )
    if extra_files:
        extra_rows = []
        for uf in extra_files:
            content = uf.read().decode("ISO-8859-1")
            for line in content.splitlines():
                if "USA,Dollar,USD" in line:
                    extra_rows.append(dict(zip(_XR_FIELDS, line.split(","))))
                    break
        if extra_rows:
            extra_xr = pd.DataFrame(extra_rows)
            extra_xr["Currency units per £1"] = pd.to_numeric(
                extra_xr["Currency units per £1"], errors="coerce"
            )
            st.info(f"Added {len(extra_rows)} rate(s) from uploaded file(s).")

# Merge base + extra exchange rates
if default_xr is not None and extra_xr is not None:
    combined_xr = pd.concat([default_xr, extra_xr], ignore_index=True)
elif default_xr is not None:
    combined_xr = default_xr
elif extra_xr is not None:
    combined_xr = extra_xr
else:
    combined_xr = None


# ════════════════════════════════════════════════════════════════════════════════
# Calculate button
# ════════════════════════════════════════════════════════════════════════════════
st.divider()

can_calculate = not edited_releases.empty and combined_xr is not None
if not can_calculate:
    missing = []
    if edited_releases.empty:
        missing.append("RSU releases (upload PDFs above)")
    if combined_xr is None:
        missing.append("exchange rates")
    st.info(f"Waiting for: {', '.join(missing)}.")

if st.button("Calculate Gains & Losses", type="primary", disabled=not can_calculate):
    try:
        with st.spinner("Applying HMRC share-identification rules…"):
            events = _run_calculation(edited_releases, edited_sales, combined_xr)
        st.session_state.results = events
    except Exception as exc:
        st.error(f"Calculation failed: {exc}")
        st.session_state.results = None


# ════════════════════════════════════════════════════════════════════════════════
# Results
# ════════════════════════════════════════════════════════════════════════════════
if st.session_state.results is not None:
    events = st.session_state.results

    # ── Tax year summary ────────────────────────────────────────────────────────
    st.subheader("Capital Gains by UK Tax Year")
    st.caption("Annual exempt amount and prior-year losses not applied.")

    ty_df = _tax_year_summary(events)
    st.dataframe(
        ty_df.style
        .map(_colour_gain, subset=["Net gain / loss (£)"])
        .format({"Net gain / loss (£)": "£{:,.2f}", "Disposals": "{:,}"}, na_rep=""),
        width="stretch",
        hide_index=True,
    )

    # ── Full event timeline ─────────────────────────────────────────────────────
    st.subheader("Full Event Timeline")

    output_cols = [
        ccb.TYPE_LABEL, ccb.DATE_LABEL,
        ccb.GRANTED_LABEL, ccb.SOLD_LABEL, ccb.ISSUED_LABEL,
        ccb.PRICE_PER_SHARE_USD_LABEL, ccb.GBP_USD_LABEL,
        ccb.PRICE_PER_SHARE_GBP_LABEL, ccb.HOLDINGS_GBP_LABEL,
        ccb.GAINS_LABEL, ccb.MATCHING_LABEL,
    ]
    display = events[output_cols].copy()
    # Withheld shares already appear in the WithholdingSell row — blank them out
    # on the Buy row so they don't look like a double disposal.
    display.loc[display[ccb.TYPE_LABEL] == ccb.BUY_TYPE, ccb.SOLD_LABEL] = float("nan")

    st.dataframe(
        display.style
        .map(_colour_gain, subset=[ccb.GAINS_LABEL])
        .format(
            {
                ccb.PRICE_PER_SHARE_USD_LABEL: "${:.4f}",
                ccb.GBP_USD_LABEL:             "{:.4f}",
                ccb.PRICE_PER_SHARE_GBP_LABEL: "£{:.4f}",
                ccb.HOLDINGS_GBP_LABEL:        "£{:,.2f}",
                ccb.GAINS_LABEL:               "£{:,.2f}",
                ccb.GRANTED_LABEL:             "{:.0f}",
                ccb.SOLD_LABEL:                "{:.0f}",
                ccb.ISSUED_LABEL:              "{:.0f}",
            },
            na_rep="",
        ),
        width="stretch",
        hide_index=True,
    )

    # ── Download ────────────────────────────────────────────────────────────────
    csv_buf = io.StringIO()
    display.to_csv(csv_buf, index=False, float_format="%.4f")
    st.download_button(
        label="Download CSV",
        data=csv_buf.getvalue().encode(),
        file_name="cost-basis.csv",
        mime="text/csv",
    )
