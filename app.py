import importlib.util
import io
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Module loading ──────────────────────────────────────────────────────────────
_BIN = Path(__file__).parent / "scripts"
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

_RELEASE_COLS = ["Release Date", "Granted", "Sold", "Issued", "Price per share ($)",
                 "Award Date", "Award Number"]
# A release is uniquely identified by its grant (Award Number) and the date it
# vested. Two different grants can vest on the same date with identical share
# counts and price, so we must NOT deduplicate on (Release Date, Granted) alone.
_RELEASE_KEY  = ["Release Date", "Award Number"]
_SALES_COLS   = ["Date", "Type", "Shares", "Price per share ($)"]
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

def _ensure_type_col(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee a 'Type' column (defaulting to Sell) for older sales files."""
    if "Type" not in df.columns:
        df = df.copy()
        df["Type"] = ccb.SELL_TYPE
    else:
        df["Type"] = df["Type"].fillna(ccb.SELL_TYPE)
    return df[[c for c in _SALES_COLS if c in df.columns]
              + [c for c in df.columns if c not in _SALES_COLS]]


if "sales_df" not in st.session_state:
    if _SALES_CSV.exists():
        raw = pd.read_csv(_SALES_CSV)
        numeric_shares = pd.to_numeric(raw.get("Shares", pd.Series(dtype=float)), errors="coerce")
        valid = raw[numeric_shares.fillna(0) > 0].reset_index(drop=True)
        st.session_state.sales_df = (
            _ensure_type_col(valid) if not valid.empty
            else pd.DataFrame(columns=_SALES_COLS)
        )
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


def _parse_pdf_path(path: Path) -> dict:
    """Parse a single release-confirmation PDF into a releases-table row."""
    result = parse_pdf(path)
    return {k: result.get(k) for k in _RELEASE_COLS}


def _merge_releases(new_rows: list[dict]) -> int:
    """
    Merge freshly parsed release rows into session state, deduplicating on
    (Release Date, Award Number).  Returns the number of rows actually added.
    """
    if not new_rows:
        return 0
    before = len(st.session_state.releases_df)
    new_df = pd.DataFrame(new_rows, columns=_RELEASE_COLS)
    existing = st.session_state.releases_df
    parts = [existing, new_df] if not existing.empty else [new_df]
    combined = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates(subset=_RELEASE_KEY, keep="last")
        .sort_values("Release Date")
        .reset_index(drop=True)
    )
    st.session_state.releases_df = combined
    st.session_state.results = None  # invalidate previous results
    return len(combined) - before


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
    """Run the shared engine pipeline on DataFrames already in memory.

    Both the CLI (`calculate-cost-basis.py main`) and this GUI delegate to
    `ccb.build_events`, so the two interfaces can never drift apart.
    """
    # Normalise the GUI sales table (Date / Shares / Price per share ($)) into the
    # engine's sales shape, dropping blank or zero-share rows.
    sales = None
    valid_sales = sales_df.dropna(how="all")
    numeric_shares = pd.to_numeric(
        valid_sales.get("Shares", pd.Series(dtype=float)), errors="coerce"
    )
    valid_sales = valid_sales[numeric_shares.fillna(0) > 0]
    if not valid_sales.empty:
        sales = pd.DataFrame({
            ccb.DATE_LABEL:                valid_sales["Date"].astype(str),
            ccb.SOLD_LABEL:                pd.to_numeric(valid_sales["Shares"]),
            ccb.PRICE_PER_SHARE_USD_LABEL: pd.to_numeric(valid_sales["Price per share ($)"]),
        })
        # Optional Type column marks generic acquisitions (Buy) vs disposals (Sell).
        if "Type" in valid_sales.columns:
            sales[ccb.TYPE_LABEL] = (
                valid_sales["Type"].apply(ccb._classify_type).values
            )

    return ccb.build_events(releases_df, sales, exrates_df)


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
                    new_rows.append(_parse_pdf_path(tmp_path))
                except Exception as exc:
                    errors[uf.name] = str(exc)
                finally:
                    if tmp_path and tmp_path.exists():
                        tmp_path.unlink()

            for fname, msg in errors.items():
                st.error(f"**{fname}**: {msg}")

            _merge_releases(new_rows)
            st.session_state.parsed_pdf_keys = current_keys

    # ── Load every PDF in a folder ────────────────────────────────────────────
    st.caption("…or load every PDF in a folder on this machine:")
    folder_col, btn_col = st.columns([4, 1])
    default_dir = _ROOT / "release-confirmations"
    folder_str = folder_col.text_input(
        "Folder path",
        value=str(default_dir) if default_dir.exists() else "",
        label_visibility="collapsed",
        placeholder="/path/to/release-confirmations",
    )
    if btn_col.button("Load folder", width="stretch"):
        folder = Path(folder_str).expanduser()
        if not folder.is_dir():
            st.error(f"Not a folder: {folder}")
        else:
            pdfs = sorted(folder.glob("*.pdf"))
            if not pdfs:
                st.warning(f"No PDF files found in {folder}.")
            else:
                new_rows, errors = [], {}
                for p in pdfs:
                    try:
                        new_rows.append(_parse_pdf_path(p))
                    except Exception as exc:
                        errors[p.name] = str(exc)
                for fname, msg in errors.items():
                    st.error(f"**{fname}**: {msg}")
                added = _merge_releases(new_rows)
                st.success(
                    f"Parsed {len(new_rows)} PDF(s) from folder; "
                    f"added {added} new release(s)."
                )
                st.session_state.pop("releases_editor", None)
                st.rerun()

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
                "Award Date": st.column_config.TextColumn(
                    "Award Date", help="Date the grant was awarded (YYYY-MM-DD)"
                ),
                "Award Number": st.column_config.TextColumn(
                    "Award #",
                    help="Grant identifier. Distinguishes grants that vest on the "
                         "same date with identical share counts."
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


with st.expander("Sales & other acquisitions", expanded=False):
    st.caption(
        "Transactions other than RSU releases. Set **Type** to *Sell* for a "
        "disposal, or *Buy* for a generic acquisition (ESPP, open-market "
        "purchase, option exercise) so it joins the same Section 104 pool. "
        "One row per transaction — date in YYYY-MM-DD, price in USD. "
        f"Saved to and auto-loaded from `{_SALES_CSV.relative_to(_ROOT)}`."
    )
    edited_sales = st.data_editor(
        st.session_state.sales_df,
        key="sales_editor",
        num_rows="dynamic",
        width="stretch",
        column_config={
            "Date": st.column_config.TextColumn(
                "Date",
                help="Transaction date in YYYY-MM-DD format, e.g. 2024-06-01",
                default="YYYY-MM-DD",
            ),
            "Type": st.column_config.SelectboxColumn(
                "Type",
                help="Sell = disposal; Buy = generic acquisition into the pool",
                options=[ccb.SELL_TYPE, ccb.BUY_TYPE],
                default=ccb.SELL_TYPE,
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

    if st.button("Save sales", type="secondary"):
        to_save = edited_sales.dropna(how="all")
        shares_num = pd.to_numeric(
            to_save.get("Shares", pd.Series(dtype=float)), errors="coerce"
        )
        to_save = to_save[shares_num.fillna(0) > 0].reset_index(drop=True)
        _SALES_CSV.parent.mkdir(parents=True, exist_ok=True)
        to_save.to_csv(_SALES_CSV, index=False)
        st.session_state.sales_df = to_save
        st.session_state.results = None
        st.success(f"Saved {len(to_save)} sale(s) to {_SALES_CSV.relative_to(_ROOT)}.")


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
# Calculation (runs automatically whenever the inputs change)
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
    st.session_state.results = None
else:
    try:
        st.session_state.results = _run_calculation(
            edited_releases, edited_sales, combined_xr
        )
    except Exception as exc:
        # In-progress edits (e.g. a half-typed date) can transiently fail —
        # surface the reason but keep the last good results on screen.
        st.warning(f"Could not recalculate yet: {exc}")


# ════════════════════════════════════════════════════════════════════════════════
# Results
# ════════════════════════════════════════════════════════════════════════════════
if st.session_state.results is not None:
    events = st.session_state.results

    # ── Tax year summary ────────────────────────────────────────────────────────
    st.subheader("Capital Gains by UK Tax Year")
    st.caption(
        "Annual exempt amount and prior-year losses not applied. "
        f"{ccb.FX_PROVENANCE_NOTE}"
    )

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
        ccb.PRICE_PER_SHARE_GBP_LABEL,
        ccb.OWNED_SHARES_LABEL, ccb.AVG_COST_GBP_LABEL, ccb.HOLDINGS_GBP_LABEL,
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
                ccb.OWNED_SHARES_LABEL:        "{:,.0f}",
                ccb.AVG_COST_GBP_LABEL:        "£{:.4f}",
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
